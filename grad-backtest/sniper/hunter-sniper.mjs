#!/usr/bin/env node
/**
 * hunter-sniper.mjs — watch one X account for a contract address and buy it once.
 *
 * Sources (first configured wins, both can run):
 *   - X API v2 (TWITTER_BEARER_TOKEN): recent search `from:<handle>`, fastest and exact.
 *   - xAI Grok (XAI_API_KEY): the Responses API with the x_search tool restricted to the
 *     handle. This is NOT the X data API; it is a model reading X for us, so every post it
 *     reports is verified against X's public oEmbed endpoint (or a second independent Grok
 *     read) before a single lamport or wei moves.
 *
 * Chains: Base and Ethereum via 0x Swap API v2 (SNIPER_EVM_PRIVATE_KEY + ZEROEX_API_KEY),
 *         Solana via Jupiter (WALLET_PRIVATE_KEY + RPC_URL, the same variables the rest of
 *         this service already uses). A candidate address is bought on whichever chain it
 *         exists on as a contract / token mint. No honeypot checks by design (max speed).
 *
 * Safety that costs no latency: hard spend caps, buy exactly once ever (state file),
 * ignore posts older than the process start, DRY RUN unless SNIPER_DRY_RUN=false.
 * Keys are read from the environment only and never logged.
 */
import fs from 'node:fs';
import path from 'node:path';
import { ethers } from 'ethers';
import { Connection, Keypair, PublicKey, VersionedTransaction, LAMPORTS_PER_SOL } from '@solana/web3.js';
import bs58 from 'bs58';
import {
  collectResponseText, extractCandidates, oembedText, parseTweetsJson, statusRefs, tweetTimeMs,
} from './lib.mjs';

// ---------------------------------------------------------------------------------------
// Config
// ---------------------------------------------------------------------------------------
const env = (k, d = '') => (process.env[k] ?? d);
const num = (k, d) => { const v = Number(env(k, d)); return Number.isFinite(v) ? v : d; };
const flag = (k, d) => { const v = env(k, '').trim().toLowerCase(); return v === '' ? d : !['0', 'false', 'no', 'off'].includes(v); };
const DATA_DIR = env('DATA_DIR', path.resolve('data'));
const BS58 = bs58.default || bs58;

/** RPC endpoints to try for an EVM chain, best first: an explicit variable, then the same
 *  Alchemy app the Solana lanes already use (Alchemy keys work across chains; only the
 *  subdomain changes), then a public endpoint. The first one that answers with the right
 *  chain id wins at startup. */
function evmRpcCandidates(chainId) {
  const explicit = env(chainId === 8453 ? 'BASE_RPC' : 'ETH_RPC').trim();
  const out = explicit ? [explicit] : [];
  const solanaRpc = (env('RPC_URLS') || env('RPC_URL') || '').split(/[,\n]/).map((s) => s.trim()).find((u) => /solana-mainnet\.g\.alchemy\.com/.test(u));
  if (solanaRpc) out.push(solanaRpc.replace('solana-mainnet.g.alchemy.com', chainId === 8453 ? 'base-mainnet.g.alchemy.com' : 'eth-mainnet.g.alchemy.com'));
  out.push(chainId === 8453 ? 'https://mainnet.base.org' : 'https://ethereum-rpc.publicnode.com');
  return [...new Set(out)];
}

const CFG = {
  handle: env('SNIPER_TARGET', env('TARGET_USERNAME', 'hunterbiden')).replace(/^@/, '').toLowerCase(),
  pollMs: Math.max(3000, num('SNIPER_POLL_MS', num('POLL_MS', 10000))),
  dryRun: !(env('SNIPER_DRY_RUN', env('DRY_RUN', 'true')).trim().toLowerCase() === 'false'
            || env('SNIPER_DRY_RUN', env('DRY_RUN', '')).trim() === '0'),
  xaiKey: env('XAI_API_KEY').trim(),
  xaiModel: env('SNIPER_MODEL', 'grok-4.6'),
  xaiBase: env('XAI_BASE_URL', 'https://api.x.ai').replace(/\/$/, ''),
  bearer: env('TWITTER_BEARER_TOKEN').trim(),
  verify: env('SNIPER_VERIFY', 'auto').toLowerCase(),          // auto | oembed | grok | none
  trustUnverified: flag('SNIPER_TRUST_UNVERIFIED', false),
  maxAgeS: num('SNIPER_MAX_POST_AGE_S', 900),                   // never act on posts older than this
  routeWaitS: num('SNIPER_ROUTE_WAIT_S', 240),                  // keep asking for a route this long
  slippageBps: num('SNIPER_SLIPPAGE_BPS', num('SLIPPAGE_BPS', 1500)),
  maxSlippageBps: num('SNIPER_MAX_SLIPPAGE_BPS', 3000),
  webhook: env('DISCORD_WEBHOOK').trim(),
  stateFile: env('SNIPER_STATE_FILE', path.join(DATA_DIR, 'sniper-state.json')),
  logFile: env('SNIPER_LOG_FILE', path.join(DATA_DIR, 'sniper.log')),
  evm: {
    key: env('SNIPER_EVM_PRIVATE_KEY', env('PRIVATE_KEY')).trim(),
    spendEth: env('SNIPER_SPEND_ETH', env('SPEND_ETH', '0.1')),
    zeroExKey: env('ZEROEX_API_KEY').trim(),                    // optional: 0x is tried first when present
    priorityGwei: num('SNIPER_PRIORITY_FEE_GWEI', num('PRIORITY_FEE_GWEI', 3)),
    rpcs: { 8453: evmRpcCandidates(8453), 1: evmRpcCandidates(1) },
  },
  sol: {
    key: env('SNIPER_SOL_PRIVATE_KEY', env('WALLET_PRIVATE_KEY')).trim(),
    spendSol: num('SNIPER_SPEND_SOL', 0.3),
    rpc: (env('SNIPER_SOL_RPC') || env('RPC_URLS') || env('RPC_URL') || 'https://api.mainnet-beta.solana.com').split(/[,\n]/)[0].trim(),
    jupiter: env('JUPITER_BASE_URL', 'https://lite-api.jup.ag/swap/v1').replace(/\/$/, ''),
    priorityLamports: num('SNIPER_SOL_PRIORITY_LAMPORTS', 1_000_000),
  },
};

const NATIVE_ETH = '0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE';
const WSOL = 'So11111111111111111111111111111111111111112';
const TOKEN_PROGRAMS = new Set([
  'TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA', 'TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb',
]);
const CHAIN_NAMES = { 8453: 'Base', 1: 'Ethereum', sol: 'Solana' };
const STARTED_AT = Date.now();

// ---------------------------------------------------------------------------------------
// Logging / state
// ---------------------------------------------------------------------------------------
function redact(s) {
  return String(s).replace(/api-key=[^&\s]+/gi, 'api-key=***').replace(/Bearer [A-Za-z0-9._-]+/g, 'Bearer ***');
}
function log(msg) {
  const line = `${new Date().toISOString()} SNIPE ${redact(msg)}`;
  process.stdout.write(line + '\n');
  try { fs.mkdirSync(path.dirname(CFG.logFile), { recursive: true }); fs.appendFileSync(CFG.logFile, line + '\n'); } catch { /* stdout still has it */ }
}
function loadState() {
  try {
    const s = JSON.parse(fs.readFileSync(CFG.stateFile, 'utf8'));
    return { seen: [], bought: false, sinceId: null, pending: {}, ...s };
  } catch { return { seen: [], bought: false, sinceId: null, pending: {} }; }
}
function saveState(s) {
  fs.mkdirSync(path.dirname(CFG.stateFile), { recursive: true });
  const tmp = `${CFG.stateFile}.tmp`;
  fs.writeFileSync(tmp, JSON.stringify(s, null, 1));
  fs.renameSync(tmp, CFG.stateFile);
}
const state = loadState();
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function notify(msg) {
  log(`[notify] ${msg}`);
  if (!CFG.webhook) return;
  try {
    await fetch(CFG.webhook, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ content: msg.slice(0, 1900) }) });
  } catch (e) { log(`[notify] webhook failed: ${e.message}`); }
}

async function fetchJson(url, opts = {}, timeoutMs = 30000) {
  const ctl = new AbortController();
  const t = setTimeout(() => ctl.abort(), timeoutMs);
  try {
    const r = await fetch(url, { ...opts, signal: ctl.signal });
    const text = await r.text();
    let json = null;
    try { json = JSON.parse(text); } catch { /* keep text */ }
    return { ok: r.ok, status: r.status, headers: r.headers, json, text };
  } finally { clearTimeout(t); }
}

// ---------------------------------------------------------------------------------------
// Wallets
// ---------------------------------------------------------------------------------------
const evm = { providers: {}, wallets: {}, rpc: {} };

/** Accepts a hex private key or a 12/24-word secret recovery phrase (MetaMask's first
 *  account, derivation m/44'/60'/0'/0/N with N from SNIPER_EVM_ACCOUNT_INDEX, default 0). */
function evmSigner() {
  const raw = CFG.evm.key.replace(/^["']|["']$/g, '').trim();
  const words = raw.split(/\s+/);
  if (words.length >= 12 && !/^0x/i.test(raw)) {
    const index = num('SNIPER_EVM_ACCOUNT_INDEX', 0);
    return ethers.HDNodeWallet.fromPhrase(words.join(' ').toLowerCase(), undefined, `m/44'/60'/0'/0/${index}`);
  }
  return new ethers.Wallet(/^0x/i.test(raw) ? raw : `0x${raw}`);
}

async function connectEvm() {
  if (!CFG.evm.key) return;
  let signer;
  try { signer = evmSigner(); } catch (e) { log(`[rpc] SNIPER_EVM_PRIVATE_KEY is not a valid private key or recovery phrase (${e.shortMessage || e.message}); EVM buys are off`); return; }
  for (const [id, candidates] of Object.entries(CFG.evm.rpcs)) {
    const chainId = Number(id);
    for (const url of candidates) {
      const provider = new ethers.JsonRpcProvider(url, chainId, { staticNetwork: true });
      try {
        const ctl = new Promise((_, rej) => setTimeout(() => rej(new Error('timeout')), 8000));
        await Promise.race([provider.getBlockNumber(), ctl]);
        evm.providers[chainId] = provider;
        evm.wallets[chainId] = signer.connect(provider);
        evm.rpc[chainId] = url;
        break;
      } catch (e) {
        log(`[rpc] ${CHAIN_NAMES[chainId]} endpoint ${redact(url)} unusable (${e.message}); trying next`);
        provider.destroy();
      }
    }
    if (!evm.providers[chainId]) log(`[rpc] no working ${CHAIN_NAMES[chainId]} endpoint; ${CHAIN_NAMES[chainId]} buys are off`);
  }
}
const sol = { connection: null, keypair: null };
if (CFG.sol.key) {
  sol.keypair = Keypair.fromSecretKey(BS58.decode(CFG.sol.key));
  sol.connection = new Connection(CFG.sol.rpc, { commitment: 'confirmed' });
}
const evmEnabled = () => Boolean(CFG.evm.key && Object.keys(evm.providers).length);
const solEnabled = () => Boolean(sol.keypair);

// ---------------------------------------------------------------------------------------
// Sources
// ---------------------------------------------------------------------------------------
let xUserId = null;

async function xGet(url) {
  const r = await fetchJson(url, { headers: { Authorization: `Bearer ${CFG.bearer}` } });
  if (r.status === 429) {
    const reset = Number(r.headers.get('x-rate-limit-reset') || 0) * 1000;
    const waitMs = Math.min(Math.max(reset - Date.now(), 15000), 15 * 60 * 1000);
    log(`[x] rate limited, backing off ${(waitMs / 1000) | 0}s`);
    await sleep(waitMs);
    return xGet(url);
  }
  if (!r.ok) throw new Error(`X API ${r.status}: ${r.text.slice(0, 200)}`);
  return r.json;
}

async function xApiTweets() {
  if (!xUserId) {
    const d = await xGet(`https://api.x.com/2/users/by/username/${CFG.handle}`);
    xUserId = d?.data?.id;
    log(`[x] watching @${CFG.handle} (id ${xUserId})`);
  }
  const params = new URLSearchParams({
    query: `from:${CFG.handle} -is:retweet`, 'tweet.fields': 'created_at,entities,author_id', max_results: '10',
  });
  if (state.sinceId) params.set('since_id', state.sinceId);
  const d = await xGet(`https://api.x.com/2/tweets/search/recent?${params}`);
  const tweets = (d?.data || []).filter((t) => t.author_id === xUserId);
  if (d?.meta?.newest_id) { state.sinceId = d.meta.newest_id; saveState(state); }
  return tweets.map((t) => {
    let blob = t.text || '';
    for (const u of t.entities?.urls || []) blob += ` ${u.expanded_url || ''} ${u.unwound_url || ''}`;
    return { id: t.id, url: `https://x.com/${CFG.handle}/status/${t.id}`, text: blob, created_at: t.created_at, handle: CFG.handle, source: 'x-api', verified: true };
  });
}

const GROK_SYSTEM = 'You are a monitoring tool, not a chat assistant. You answer ONLY with the JSON requested. '
  + 'Never invent, paraphrase or summarize posts: copy their text verbatim, including every URL and every token contract address. '
  + 'If you cannot find a matching post with certainty, return [].';

async function grokRequest(userPrompt, timeoutMs = 45000, fromDate = null) {
  const today = new Date();
  fromDate = fromDate || new Date(today.getTime() - 24 * 3600 * 1000).toISOString().slice(0, 10);
  // Citations are returned by default on the Responses API; optional arguments are dropped
  // one by one if the API says it does not support them, so a schema change never stalls us.
  const body = {
    model: CFG.xaiModel,
    input: [{ role: 'system', content: GROK_SYSTEM }, { role: 'user', content: userPrompt }],
    // Image understanding: a contract address posted as a screenshot still gets read.
    tools: [{ type: 'x_search', allowed_x_handles: [CFG.handle], from_date: fromDate, enable_image_understanding: true }],
    temperature: 0,
    store: false,
  };
  const optional = ['temperature', 'store'];
  for (;;) {
    const r = await fetchJson(`${CFG.xaiBase}/v1/responses`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${CFG.xaiKey}` },
      body: JSON.stringify(body),
    }, timeoutMs);
    if (r.status === 429) throw Object.assign(new Error('xAI rate limited'), { retryAfterMs: 20000 });
    if (r.status === 400 && /not supported/i.test(r.text)) {
      const culprit = optional.find((k) => k in body && new RegExp(`"${k}"`).test(r.text))
        || (/"from_date"|"allowed_x_handles"|"enable_image_understanding"/.test(r.text) ? 'tool-args' : null);
      if (culprit === 'tool-args' && /"enable_image_understanding"/.test(r.text) && body.tools[0].enable_image_understanding !== undefined) {
        delete body.tools[0].enable_image_understanding; log('[grok] API rejected enable_image_understanding; retrying without it'); continue;
      }
      if (culprit === 'tool-args' && body.tools[0].from_date) { delete body.tools[0].from_date; log('[grok] API rejected from_date; retrying without it'); continue; }
      if (culprit && culprit !== 'tool-args') { delete body[culprit]; log(`[grok] API rejected "${culprit}"; retrying without it`); continue; }
    }
    if (!r.ok) throw new Error(`xAI ${r.status}: ${r.text.slice(0, 300)}`);
    return collectResponseText(r.json ?? {});
  }
}

/** One-time startup read of the account's latest post, any age: proves the model can see
 *  the account before we rely on it. Never acted on (it is older than the start). */
async function grokProbe() {
  const prompt = `Using x_search, find the single most recent post published by @${CFG.handle} (any date within the last 30 days). `
    + 'Reply with ONLY a JSON array with that one post, no prose, no markdown: '
    + '[{"id":"<numeric status id>","url":"https://x.com/' + CFG.handle + '/status/<id>","created_at":"<ISO 8601 UTC>","text":"<full verbatim text>"}]. Return [] if none.';
  const fromDate = new Date(Date.now() - 30 * 24 * 3600 * 1000).toISOString().slice(0, 10);
  const { text, urls } = await grokRequest(prompt, 60000, fromDate);
  const rows = parseTweetsJson(text);
  const row = rows[0] || (statusRefs(urls.join(' ')).find((r) => r.handle === CFG.handle) ? { id: statusRefs(urls.join(' ')).find((r) => r.handle === CFG.handle).id, text: '' } : null);
  if (!row) { log(`[probe] Grok returned no post for @${CFG.handle}; raw: ${text.replace(/\s+/g, ' ').slice(0, 200)}`); return; }
  const ageMin = ((Date.now() - (tweetTimeMs(row) ?? Date.now())) / 60000).toFixed(0);
  log(`[probe] Grok can read @${CFG.handle}: latest post ${row.id} (${ageMin} min old): ${(row.text || '').replace(/\s+/g, ' ').slice(0, 140)}`);
  // Only an old post is retired here; a post fresh enough to act on stays for the poll loop.
  if (Number(ageMin) * 60 > CFG.maxAgeS && !state.seen.includes(row.id)) { state.seen.push(row.id); saveState(state); }
}

async function grokTweets() {
  const sinceIso = new Date(STARTED_AT - CFG.maxAgeS * 1000).toISOString();
  const prompt = `Using x_search, find every post published by @${CFG.handle} after ${sinceIso} (UTC), newest first, at most 10. `
    + 'Reply with ONLY a JSON array, no prose, no markdown: '
    + '[{"id":"<numeric status id>","url":"https://x.com/' + CFG.handle + '/status/<id>","created_at":"<ISO 8601 UTC>","text":"<full verbatim post text with every URL and address exactly as written>"}]. '
    + 'If a post shows a contract address only inside an attached image, append that address to the text field exactly as it appears. '
    + 'Return [] if there are none.';
  const { text, urls } = await grokRequest(prompt);
  const tweets = parseTweetsJson(text).filter((t) => !t.handle || t.handle === CFG.handle);
  for (const ref of statusRefs(urls.join(' '))) {
    if (ref.handle === CFG.handle && !tweets.some((t) => t.id === ref.id)) {
      tweets.push({ id: ref.id, url: `https://x.com/${CFG.handle}/status/${ref.id}`, text: '', created_at: '', handle: CFG.handle });
    }
  }
  return tweets.map((t) => ({ ...t, source: 'grok', verified: false }));
}

// ---------------------------------------------------------------------------------------
// Verification: the model may misread or invent; X's own render of the post may not.
// ---------------------------------------------------------------------------------------
async function expandUrl(url, hops = 3) {
  let current = url;
  for (let i = 0; i < hops; i++) {
    if (!/^https?:\/\/(t\.co|bit\.ly|tinyurl\.com)\//i.test(current)) return current;
    try {
      const ctl = new AbortController();
      const t = setTimeout(() => ctl.abort(), 8000);
      const r = await fetch(current, { method: 'HEAD', redirect: 'manual', signal: ctl.signal });
      clearTimeout(t);
      const loc = r.headers.get('location');
      if (!loc) return current;
      current = new URL(loc, current).toString();
    } catch { return current; }
  }
  return current;
}

async function oembedVerify(tweet) {
  const url = `https://publish.x.com/oembed?url=${encodeURIComponent(`https://x.com/${CFG.handle}/status/${tweet.id}`)}&omit_script=1&dnt=1`;
  const r = await fetchJson(url, {}, 10000);
  if (!r.ok || !r.json?.html) throw new Error(`oembed ${r.status}`);
  const author = String(r.json.author_url || '').toLowerCase();
  if (!author.endsWith(`/${CFG.handle}`)) throw new Error(`oembed author mismatch: ${author}`);
  const { text, hrefs } = oembedText(r.json.html);
  const expanded = [];
  for (const h of hrefs) {
    if (/^https?:\/\/(x|twitter)\.com\//i.test(h)) continue;
    expanded.push(await expandUrl(h));
  }
  return `${text}\n${expanded.join('\n')}`;
}

async function grokVerify(tweet) {
  const prompt = `Open https://x.com/${CFG.handle}/status/${tweet.id} with x_search and reply with ONLY a JSON array containing that one post: `
    + '[{"id":"<numeric status id>","url":"<url>","created_at":"<ISO 8601 UTC>","text":"<full verbatim text with every URL and address exactly as written>"}]. '
    + 'Return [] if the post does not exist or you are not certain of its exact text.';
  const { text } = await grokRequest(prompt);
  const rows = parseTweetsJson(text);
  const row = rows.find((r) => r.id === tweet.id);
  if (!row) throw new Error('second read did not return the post');
  return row.text;
}

/** Returns {verified, text} where text is the trusted blob to extract addresses from. */
async function verifyTweet(tweet) {
  if (tweet.verified || CFG.verify === 'none') return { verified: true, text: tweet.text, how: tweet.source };
  const errors = [];
  if (CFG.verify === 'auto' || CFG.verify === 'oembed') {
    try { return { verified: true, text: await oembedVerify(tweet), how: 'oembed' }; } catch (e) { errors.push(`oembed: ${e.message}`); }
  }
  if (CFG.verify === 'auto' || CFG.verify === 'grok') {
    try {
      const second = await grokVerify(tweet);
      const a = extractCandidates(tweet.text), b = extractCandidates(second);
      const agree = [...a.evm.filter((x) => b.evm.includes(x)), ...a.solana.filter((x) => b.solana.includes(x))];
      if (agree.length) return { verified: true, text: agree.join(' '), how: 'grok-x2' };
      if (!a.evm.length && !a.solana.length && !b.evm.length && !b.solana.length) return { verified: true, text: second, how: 'grok-x2-empty' };
      errors.push('grok: two reads disagree on the address');
    } catch (e) { errors.push(`grok: ${e.message}`); }
  }
  return { verified: false, text: tweet.text, how: errors.join('; ') };
}

// ---------------------------------------------------------------------------------------
// Chain detection
// ---------------------------------------------------------------------------------------
async function detectEvm(addr) {
  if (!Object.keys(evm.providers).length) return null;
  const checks = await Promise.all(Object.entries(evm.providers).map(async ([id, p]) => {
    try { return [Number(id), await p.getCode(addr)]; } catch (e) { log(`[detect] chain ${id} getCode failed: ${e.message}`); return [Number(id), '0x']; }
  }));
  const hit = checks.find(([, code]) => code && code !== '0x');
  return hit ? hit[0] : null;
}

async function detectSolanaMint(addr) {
  if (!sol.connection) return false;
  try {
    const info = await sol.connection.getAccountInfo(new PublicKey(addr));
    return Boolean(info && TOKEN_PROGRAMS.has(info.owner.toBase58()) && info.data.length >= 82);
  } catch (e) { log(`[detect] solana getAccountInfo failed: ${e.message}`); return false; }
}

async function resolveTarget(blob) {
  const { evm: evmAddrs, solana: solAddrs } = extractCandidates(blob);
  for (const a of evmAddrs) {
    if (!ethers.isAddress(a)) continue;
    const chainId = evmEnabled() ? await detectEvm(a) : null;
    if (chainId) return { chain: chainId, address: ethers.getAddress(a) };
    if (!evmEnabled()) log(`[detect] EVM address ${a} seen but EVM buying is not configured (SNIPER_EVM_PRIVATE_KEY)`);
  }
  for (const m of solAddrs) {
    if (m === WSOL) continue;
    if (solEnabled() && await detectSolanaMint(m)) return { chain: 'sol', address: m };
    if (!solEnabled()) log(`[detect] Solana address ${m} seen but Solana buying is not configured (WALLET_PRIVATE_KEY)`);
  }
  return null;
}

// ---------------------------------------------------------------------------------------
// Buying
// ---------------------------------------------------------------------------------------
const isRouteError = (msg) => /no route|liquidityAvailable|INSUFFICIENT_LIQUIDITY|no liquidity|COULD_NOT_FIND_ANY_ROUTE|not tradable|route not found|TOKEN_NOT_TRADABLE/i.test(msg);
const isSlippageError = (msg) => /slippage|0x1771|6001|price impact|Custom":6001|exceeds desired/i.test(msg);

async function withRouteRetry(fn, label) {
  const deadline = Date.now() + CFG.routeWaitS * 1000;
  let slippage = CFG.slippageBps;
  let attempt = 0;
  for (;;) {
    attempt += 1;
    try { return await fn(slippage, attempt); } catch (e) {
      const msg = String(e.message || e);
      if (isSlippageError(msg) && slippage < CFG.maxSlippageBps) {
        slippage = Math.min(CFG.maxSlippageBps, slippage + 500);
        log(`[${label}] slippage error, retrying at ${slippage} bps: ${msg.slice(0, 160)}`);
      } else if (isRouteError(msg) && Date.now() < deadline) {
        log(`[${label}] no route yet (attempt ${attempt}), retrying: ${msg.slice(0, 160)}`);
        await sleep(1500);
      } else if (Date.now() < deadline && attempt < 6) {
        log(`[${label}] attempt ${attempt} failed, retrying: ${msg.slice(0, 200)}`);
        await sleep(1500);
      } else {
        throw e;
      }
    }
  }
}

const KYBER_CHAINS = { 8453: 'base', 1: 'ethereum' };

const ERC20_ABI = [
  'function balanceOf(address) view returns (uint256)',
  'function allowance(address,address) view returns (uint256)',
  'function approve(address,uint256) returns (bool)',
];

/** 0x Swap API v2 (needs ZEROEX_API_KEY). Returns {to, data, value, gas, buyAmount, via}. */
async function quote0x(chainId, tokenIn, tokenOut, sellAmount, taker, slippageBps) {
  const q = new URLSearchParams({ chainId: String(chainId), sellToken: tokenIn, buyToken: tokenOut, sellAmount, taker, slippageBps: String(slippageBps) });
  const r = await fetchJson(`https://api.0x.org/swap/allowance-holder/quote?${q}`, { headers: { '0x-api-key': CFG.evm.zeroExKey, '0x-version': 'v2' } }, 15000);
  if (!r.ok) throw new Error(`0x quote ${r.status}: ${r.text.slice(0, 300)}`);
  const quote = r.json || {};
  if (quote.liquidityAvailable === false || !quote.transaction) throw new Error(`no route: 0x ${r.text.slice(0, 200)}`);
  const spender = quote.issues?.allowance?.spender || quote.transaction.to;
  return { to: quote.transaction.to, data: quote.transaction.data, value: tokenIn === NATIVE_ETH ? BigInt(quote.transaction.value || sellAmount) : 0n, gas: BigInt(quote.transaction.gas || 0), buyAmount: quote.buyAmount, spender, via: '0x' };
}

/** KyberSwap aggregator (no key): routes across Uniswap v2/v3/v4, Aerodrome and the rest. */
async function quoteKyber(chainId, tokenIn, tokenOut, sellAmount, taker, slippageBps) {
  const chain = KYBER_CHAINS[chainId];
  const headers = { 'x-client-id': 'mememe-sniper', 'Content-Type': 'application/json' };
  const q = new URLSearchParams({ tokenIn, tokenOut, amountIn: sellAmount, gasInclude: 'true' });
  const r = await fetchJson(`https://aggregator-api.kyberswap.com/${chain}/api/v1/routes?${q}`, { headers }, 15000);
  const summary = r.json?.data?.routeSummary;
  if (!r.ok || !summary || !r.json?.data?.routerAddress) throw new Error(`no route: kyber routes ${r.status} ${r.text.slice(0, 200)}`);
  const build = await fetchJson(`https://aggregator-api.kyberswap.com/${chain}/api/v1/route/build`, {
    method: 'POST', headers,
    body: JSON.stringify({ routeSummary: summary, sender: taker, recipient: taker, slippageTolerance: slippageBps, deadline: Math.floor(Date.now() / 1000) + 120, source: 'mememe-sniper', enableGasEstimation: false }),
  }, 15000);
  const d = build.json?.data;
  if (!build.ok || !d?.data || !d?.routerAddress) throw new Error(`kyber build ${build.status}: ${build.text.slice(0, 300)}`);
  return { to: d.routerAddress, data: d.data, value: tokenIn === NATIVE_ETH ? BigInt(d.amountIn || sellAmount) : 0n, gas: BigInt(d.gas || 0), buyAmount: d.amountOut || summary.amountOut, spender: d.routerAddress, via: 'kyberswap' };
}

async function quoteEvm(chainId, tokenIn, tokenOut, sellAmount, taker, slippageBps) {
  const aggregators = [...(CFG.evm.zeroExKey ? [quote0x] : []), quoteKyber];
  const errors = [];
  for (const agg of aggregators) {
    try { return await agg(chainId, tokenIn, tokenOut, sellAmount, taker, slippageBps); } catch (e) { errors.push(e.message); }
  }
  throw new Error(errors.join(' | '));
}

/** Sign and send an aggregator transaction; returns the receipt. Approves the spender first
 *  when the input is an ERC20 the router may not pull yet. */
async function sendEvmSwap(chainId, tokenIn, sellAmount, quote, label) {
  const wallet = evm.wallets[chainId];
  const provider = evm.providers[chainId];
  const fee = await provider.getFeeData();
  const priority = ethers.parseUnits(String(CFG.evm.priorityGwei), 'gwei');
  const base = fee.maxFeePerGas || fee.gasPrice || priority;
  const gasFields = { maxPriorityFeePerGas: priority, maxFeePerGas: base * 2n + priority, chainId };
  if (tokenIn !== NATIVE_ETH) {
    const erc20 = new ethers.Contract(tokenIn, ERC20_ABI, wallet);
    const allowance = await erc20.allowance(wallet.address, quote.spender);
    if (allowance < BigInt(sellAmount)) {
      const approval = await erc20.approve(quote.spender, ethers.MaxUint256, gasFields);
      log(`[${label}] approving ${quote.spender} to spend the token: ${approval.hash}`);
      const rc = await approval.wait(1);
      if (rc.status !== 1) throw new Error(`approve ${approval.hash} reverted`);
    }
  }
  let gasLimit = quote.gas > 0n ? quote.gas * 13n / 10n : 0n;
  if (gasLimit === 0n) {
    try { gasLimit = (await provider.estimateGas({ to: quote.to, data: quote.data, value: quote.value, from: wallet.address })) * 13n / 10n; } catch (e) {
      throw new Error(`no route: simulation failed via ${quote.via}: ${String(e.shortMessage || e.message).slice(0, 160)}`);
    }
  }
  const sent = await wallet.sendTransaction({ to: quote.to, data: quote.data, value: quote.value, gasLimit, ...gasFields });
  log(`[${label}] ${CHAIN_NAMES[chainId]} tx sent ${sent.hash} via ${quote.via}`);
  const receipt = await sent.wait(1);
  if (receipt.status !== 1) throw new Error(`tx ${sent.hash} reverted`);
  log(`[${label}] confirmed in block ${receipt.blockNumber}`);
  return { hash: sent.hash, receipt };
}

async function buyEvm(chainId, tokenAddr) {
  const wallet = evm.wallets[chainId];
  const sellAmount = ethers.parseEther(CFG.evm.spendEth).toString();
  return withRouteRetry(async (slippageBps, attempt) => {
    const quote = await quoteEvm(chainId, NATIVE_ETH, tokenAddr, sellAmount, wallet.address, slippageBps);
    if (CFG.dryRun) {
      log(`[DRY RUN] would buy ${tokenAddr} on ${CHAIN_NAMES[chainId]} for ${CFG.evm.spendEth} ETH via ${quote.via}; est. out ${quote.buyAmount} (attempt ${attempt})`);
      return { hash: 'DRY_RUN', chain: CHAIN_NAMES[chainId], spend: `${CFG.evm.spendEth} ETH`, spentRaw: sellAmount };
    }
    const { hash } = await sendEvmSwap(chainId, NATIVE_ETH, sellAmount, quote, 'buy');
    return { hash, chain: CHAIN_NAMES[chainId], spend: `${CFG.evm.spendEth} ETH`, spentRaw: sellAmount };
  }, `swap:${CHAIN_NAMES[chainId]}`);
}

async function sellEvm(pos, amountRaw) {
  const wallet = evm.wallets[pos.chain];
  return withRouteRetry(async (slippageBps) => {
    const quote = await quoteEvm(pos.chain, pos.address, NATIVE_ETH, amountRaw.toString(), wallet.address, slippageBps);
    const { hash } = await sendEvmSwap(pos.chain, pos.address, amountRaw.toString(), quote, 'sell');
    return { hash, outRaw: String(quote.buyAmount), via: quote.via };
  }, `sell:${CHAIN_NAMES[pos.chain]}`);
}

async function jupiterQuote(inputMint, outputMint, amountRaw, slippageBps) {
  const q = new URLSearchParams({ inputMint, outputMint, amount: String(amountRaw), slippageBps: String(slippageBps), restrictIntermediateTokens: 'true' });
  const quote = await fetchJson(`${CFG.sol.jupiter}/quote?${q}`, {}, 12000);
  if (!quote.ok || !quote.json?.outAmount) throw new Error(`no route: jupiter quote ${quote.status} ${quote.text.slice(0, 200)}`);
  return quote.json;
}

async function jupiterSend(quoteJson, label, attempt) {
  const swap = await fetchJson(`${CFG.sol.jupiter}/swap`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      quoteResponse: quoteJson, userPublicKey: sol.keypair.publicKey.toBase58(), wrapAndUnwrapSol: true,
      dynamicComputeUnitLimit: true, dynamicSlippage: false,
      prioritizationFeeLamports: { priorityLevelWithMaxLamports: { maxLamports: CFG.sol.priorityLamports, priorityLevel: 'veryHigh' } },
    }),
  }, 15000);
  if (!swap.ok || !swap.json?.swapTransaction) throw new Error(`jupiter swap ${swap.status}: ${swap.text.slice(0, 200)}`);
  const tx = VersionedTransaction.deserialize(Buffer.from(swap.json.swapTransaction, 'base64'));
  tx.sign([sol.keypair]);
  const sig = await sol.connection.sendRawTransaction(tx.serialize(), { skipPreflight: true, maxRetries: 3 });
  log(`[${label}] Solana tx sent ${sig} (attempt ${attempt})`);
  const conf = await sol.connection.confirmTransaction({ signature: sig, blockhash: tx.message.recentBlockhash, lastValidBlockHeight: swap.json.lastValidBlockHeight }, 'confirmed');
  if (conf.value.err) throw new Error(`tx ${sig} failed: ${JSON.stringify(conf.value.err)}`);
  log(`[${label}] confirmed ${sig}`);
  return sig;
}

async function buySolana(mint) {
  const lamports = Math.floor(CFG.sol.spendSol * LAMPORTS_PER_SOL);
  return withRouteRetry(async (slippageBps, attempt) => {
    const quote = await jupiterQuote(WSOL, mint, lamports, slippageBps);
    if (CFG.dryRun) {
      log(`[DRY RUN] would buy ${mint} on Solana for ${CFG.sol.spendSol} SOL; est. out ${quote.outAmount} (raw) via ${(quote.routePlan || []).map((p) => p.swapInfo?.label).join('>')}`);
      return { hash: 'DRY_RUN', chain: 'Solana', spend: `${CFG.sol.spendSol} SOL`, spentRaw: String(lamports) };
    }
    const sig = await jupiterSend(quote, 'buy', attempt);
    return { hash: sig, chain: 'Solana', spend: `${CFG.sol.spendSol} SOL`, spentRaw: String(lamports) };
  }, 'jupiter');
}

async function sellSolana(pos, amountRaw) {
  return withRouteRetry(async (slippageBps, attempt) => {
    const quote = await jupiterQuote(pos.address, WSOL, amountRaw, slippageBps);
    const sig = await jupiterSend(quote, 'sell', attempt);
    return { hash: sig, outRaw: String(quote.outAmount), via: 'jupiter' };
  }, 'jupiter-sell');
}

async function buy(target) {
  return target.chain === 'sol' ? buySolana(target.address) : buyEvm(target.chain, target.address);
}

// ---------------------------------------------------------------------------------------
// Take profit: after the buy, watch what the whole bag sells for and exit everything at
// SNIPER_TAKE_PROFIT_X times the spend (quote net of impact, so the number is executable).
// $DATA_DIR/sniper-sell.json (POST /api/sniper/sell) forces the sale at any price.
// ---------------------------------------------------------------------------------------
const TAKE_PROFIT_X = num('SNIPER_TAKE_PROFIT_X', 10);
const TP_CHECK_MS = Math.max(5000, num('SNIPER_TP_CHECK_MS', 15000));
const SELL_FILE = path.join(DATA_DIR, 'sniper-sell.json');
const tp = { lastCheck: 0, lastLogged: null, multiple: null, quoteErrors: 0 };

async function tokenBalance(pos) {
  if (pos.chain === 'sol') {
    const accounts = await sol.connection.getParsedTokenAccountsByOwner(sol.keypair.publicKey, { mint: new PublicKey(pos.address) });
    return accounts.value.reduce((s, a) => s + BigInt(a.account.data.parsed?.info?.tokenAmount?.amount || 0), 0n);
  }
  const erc20 = new ethers.Contract(pos.address, ERC20_ABI, evm.providers[pos.chain]);
  return BigInt(await erc20.balanceOf(evm.wallets[pos.chain].address));
}

async function sellValueRaw(pos, amountRaw) {
  if (pos.chain === 'sol') return BigInt((await jupiterQuote(pos.address, WSOL, amountRaw, CFG.slippageBps)).outAmount);
  const wallet = evm.wallets[pos.chain];
  return BigInt((await quoteEvm(pos.chain, pos.address, NATIVE_ETH, amountRaw.toString(), wallet.address, CFG.slippageBps)).buyAmount);
}

function openPosition(target, res, tweetId) {
  state.position = {
    chain: target.chain, chainName: CHAIN_NAMES[target.chain], address: target.address, spentRaw: String(res.spentRaw), spend: res.spend,
    buyHash: res.hash, tweet: tweetId, openedAt: new Date().toISOString(), takeProfitX: TAKE_PROFIT_X, sold: null,
  };
  saveState(state);
  log(`[tp] watching ${target.address} on ${CHAIN_NAMES[target.chain]}: sell everything at ${TAKE_PROFIT_X}x of ${res.spend}`);
}

function fmtNative(pos, raw) {
  return pos.chain === 'sol' ? `${(Number(raw) / LAMPORTS_PER_SOL).toFixed(4)} SOL` : `${Number(ethers.formatEther(raw)).toFixed(5)} ETH`;
}

async function checkTakeProfit() {
  const pos = state.position;
  if (!pos || pos.sold) return;
  const manual = fs.existsSync(SELL_FILE);
  if (!manual && Date.now() - tp.lastCheck < TP_CHECK_MS) return;
  tp.lastCheck = Date.now();
  if (pos.chain === 'sol' ? !solEnabled() : !evm.providers[pos.chain]) return;
  let balance;
  try { balance = await tokenBalance(pos); } catch (e) { log(`[tp] balance read failed: ${String(e.message).slice(0, 120)}`); return; }
  if (balance <= 0n) {
    log(`[tp] wallet no longer holds ${pos.address}; the bag was sold or moved outside this bot. Watch ends.`);
    pos.sold = { hash: 'external', at: new Date().toISOString() }; saveState(state);
    if (manual) { try { fs.unlinkSync(SELL_FILE); } catch { /* gone */ } }
    return;
  }
  let value;
  try { value = await sellValueRaw(pos, balance); tp.quoteErrors = 0; } catch (e) {
    tp.quoteErrors += 1;
    if (tp.quoteErrors === 1 || tp.quoteErrors % 20 === 0) log(`[tp] no sell quote yet (${tp.quoteErrors}x): ${String(e.message).slice(0, 160)}`);
    if (!manual) return;
  }
  if (value !== undefined) {
    tp.multiple = Number(value) / Number(pos.spentRaw);
    if (tp.lastLogged === null || Math.abs(tp.multiple - tp.lastLogged) >= Math.max(0.25, tp.lastLogged * 0.25)) {
      log(`[tp] ${pos.address.slice(0, 10)} bag sells for ${fmtNative(pos, value)} = ${tp.multiple.toFixed(2)}x of ${pos.spend} (target ${pos.takeProfitX}x)`);
      tp.lastLogged = tp.multiple;
    }
  }
  if (!manual && !(tp.multiple >= pos.takeProfitX)) return;
  const why = manual ? 'manual sell requested' : `${tp.multiple.toFixed(2)}x >= ${pos.takeProfitX}x take profit`;
  await notify(`💰 SELLING all ${pos.address} on ${pos.chainName}: ${why}`);
  try {
    const res = pos.chain === 'sol' ? await sellSolana(pos, balance) : await sellEvm(pos, balance);
    pos.sold = { hash: res.hash, outRaw: res.outRaw, multiple: Number(res.outRaw) / Number(pos.spentRaw), at: new Date().toISOString(), why };
    saveState(state);
    await notify(`✅ SOLD ${pos.address} on ${pos.chainName} for about ${fmtNative(pos, res.outRaw)} (${pos.sold.multiple.toFixed(2)}x): ${res.hash}`);
  } catch (e) {
    await notify(`❌ Sell failed for ${pos.address}: ${String(e.message || e).slice(0, 300)}; will retry next check`);
  } finally {
    if (manual) { try { fs.unlinkSync(SELL_FILE); } catch { /* gone */ } }
  }
}

/** Buy-once guard that survives a lost state file: the wallet already holding the token
 *  means it was bought. Unknown (RPC error) counts as not held. */
async function alreadyHolds(target) {
  try {
    if (target.chain === 'sol') {
      const accounts = await sol.connection.getParsedTokenAccountsByOwner(sol.keypair.publicKey, { mint: new PublicKey(target.address) });
      return accounts.value.some((a) => Number(a.account.data.parsed?.info?.tokenAmount?.amount || 0) > 0);
    }
    const erc20 = new ethers.Contract(target.address, ['function balanceOf(address) view returns (uint256)'], evm.providers[target.chain]);
    return (await erc20.balanceOf(evm.wallets[target.chain].address)) > 0n;
  } catch (e) { log(`[buy] holdings check failed (${String(e.message).slice(0, 120)}); continuing`); return false; }
}

/** Run the whole buy path for a piece of text (address extraction, chain detection, quote)
 *  in DRY RUN, whatever the live setting. Triggered by SNIPER_TEST_TEXT at startup or by
 *  POST /api/sniper/test, which drops $DATA_DIR/sniper-test.json for the next poll. */
async function runTest(text, origin) {
  log(`[test] ${origin}: exercising the buy path in DRY RUN for: ${String(text).replace(/\s+/g, ' ').slice(0, 160)}`);
  const cands = extractCandidates(text);
  if (!cands.evm.length && !cands.solana.length) { log('[test] FAILED: no contract address found in that text'); return; }
  const target = await resolveTarget(text);
  if (!target) { log(`[test] FAILED: not a live contract on any configured chain: ${[...cands.evm, ...cands.solana].join(', ')}`); return; }
  log(`[test] resolved ${target.address} on ${CHAIN_NAMES[target.chain]}; requesting a quote for ${target.chain === 'sol' ? `${CFG.sol.spendSol} SOL` : `${CFG.evm.spendEth} ETH`}`);
  const saved = { dryRun: CFG.dryRun, routeWaitS: CFG.routeWaitS };
  CFG.dryRun = true; CFG.routeWaitS = 20;
  try {
    const res = await buy(target);
    log(`[test] OK: ${res.chain} buy path works end to end (would spend ${res.spend}); nothing was sent`);
  } catch (e) {
    log(`[test] FAILED at the quote/transaction step: ${String(e.message || e).slice(0, 300)}`);
  } finally { CFG.dryRun = saved.dryRun; CFG.routeWaitS = saved.routeWaitS; }
}

const TEST_FILE = path.join(DATA_DIR, 'sniper-test.json');
async function runQueuedTest() {
  let text = null;
  try {
    if (!fs.existsSync(TEST_FILE)) return;
    text = JSON.parse(fs.readFileSync(TEST_FILE, 'utf8')).text;
    fs.unlinkSync(TEST_FILE);
  } catch (e) { log(`[test] could not read ${TEST_FILE}: ${e.message}`); try { fs.unlinkSync(TEST_FILE); } catch { /* gone */ } return; }
  if (text) await runTest(text, 'api');
}

// ---------------------------------------------------------------------------------------
// Main loop
// ---------------------------------------------------------------------------------------
async function handleTweet(tweet) {
  const ageS = (Date.now() - (tweetTimeMs(tweet) ?? Date.now())) / 1000;
  log(`[post] ${tweet.id} via ${tweet.source} age ${ageS.toFixed(0)}s: ${(tweet.text || '(text pending verification)').replace(/\s+/g, ' ').slice(0, 160)}`);
  if (ageS > CFG.maxAgeS) { log(`[skip] ${tweet.id} is older than ${CFG.maxAgeS}s`); return true; }
  const quick = extractCandidates(tweet.text);
  const v = await verifyTweet(tweet);
  if (!v.verified) {
    if (!quick.evm.length && !quick.solana.length) { log(`[skip] ${tweet.id}: no address in the model's read and X did not confirm (${v.how})`); return false; }
    if (!CFG.trustUnverified) {
      await notify(`⚠️ @${CFG.handle} post ${tweet.id} looks like it has an address but could not be verified (${v.how}). NOT buying. Set SNIPER_TRUST_UNVERIFIED=1 to buy on the model's word alone.`);
      return false;
    }
    log(`[verify] ${tweet.id} unverified but SNIPER_TRUST_UNVERIFIED=1`);
  } else {
    log(`[verify] ${tweet.id} confirmed via ${v.how}`);
  }
  const blob = `${v.text}\n${tweet.text || ''}`;
  const cands = extractCandidates(v.verified ? v.text : blob);
  if (!cands.evm.length && !cands.solana.length) { log(`[skip] ${tweet.id}: no contract address`); return true; }
  if (state.bought) { log(`[skip] already bought once (${state.buy?.hash}); ignoring ${tweet.id}`); return true; }
  const target = await resolveTarget(v.verified ? v.text : blob);
  if (!target) {
    await notify(`Address in @${CFG.handle} post ${tweet.id} (${[...cands.evm, ...cands.solana].join(', ')}) is not a live contract on Base/Ethereum/Solana yet. Will keep checking.`);
    return false;
  }
  if (!CFG.dryRun && await alreadyHolds(target)) {
    state.bought = true; state.buy = { hash: 'already-held', address: target.address, chain: CHAIN_NAMES[target.chain], tweet: tweet.id, at: new Date().toISOString() };
    saveState(state);
    await notify(`Wallet already holds ${target.address} on ${CHAIN_NAMES[target.chain]}; treating it as bought and not buying again.`);
    if (!state.position) {
      const spentRaw = target.chain === 'sol' ? String(Math.floor(CFG.sol.spendSol * LAMPORTS_PER_SOL)) : ethers.parseEther(CFG.evm.spendEth).toString();
      openPosition(target, { hash: 'already-held', spentRaw, spend: target.chain === 'sol' ? `${CFG.sol.spendSol} SOL` : `${CFG.evm.spendEth} ETH` }, tweet.id);
    }
    return true;
  }
  await notify(`🚨 SNIPING ${target.address} on ${CHAIN_NAMES[target.chain]} from post ${tweet.url || tweet.id}${CFG.dryRun ? ' (DRY RUN)' : ''}`);
  try {
    const res = await buy(target);
    if (!CFG.dryRun) {
      state.bought = true; state.buy = { ...res, address: target.address, tweet: tweet.id, at: new Date().toISOString() }; saveState(state);
      openPosition(target, res, tweet.id);
    }
    await notify(`✅ ${CFG.dryRun ? 'DRY RUN ' : ''}bought ${target.address} on ${res.chain} for ${res.spend}: ${res.hash}`);
  } catch (e) {
    await notify(`❌ Buy failed for ${target.address} on ${CHAIN_NAMES[target.chain]}: ${String(e.message || e).slice(0, 300)}`);
    return false;
  }
  return true;
}

let backoffMs = 0;
const stats = { polls: 0, ok: 0, errors: 0, posts: 0, lastHeartbeat: Date.now() };
const HEARTBEAT_MS = Math.max(60000, num('SNIPER_HEARTBEAT_MS', 300000));

function heartbeat() {
  if (Date.now() - stats.lastHeartbeat < HEARTBEAT_MS) return;
  stats.lastHeartbeat = Date.now();
  const pos = state.position;
  const posNote = pos ? (pos.sold ? `; sold ${pos.address.slice(0, 10)} at ${pos.sold.multiple ? pos.sold.multiple.toFixed(2) + 'x' : pos.sold.hash}`
    : `; holding ${pos.address.slice(0, 10)} on ${pos.chainName} at ${tp.multiple == null ? '?' : tp.multiple.toFixed(2) + 'x'} (sell at ${pos.takeProfitX}x)`) : '';
  log(`[heartbeat] ${stats.polls} polls, ${stats.ok} ok, ${stats.errors} errors, ${stats.posts} posts seen, `
    + `${Object.keys(state.pending).length} pending; dryRun=${CFG.dryRun} bought=${state.bought}${posNote}`);
}

async function tick() {
  await runQueuedTest();
  try { await checkTakeProfit(); } catch (e) { log(`[tp] ${String(e.message || e).slice(0, 200)}`); }
  const tweets = [];
  stats.polls += 1;
  if (CFG.bearer) {
    try { tweets.push(...await xApiTweets()); stats.ok += 1; } catch (e) { stats.errors += 1; log(`[x] ${e.message}`); }
  }
  if (CFG.xaiKey && (!CFG.bearer || tweets.length === 0)) {
    try { tweets.push(...await grokTweets()); backoffMs = 0; stats.ok += 1; } catch (e) {
      stats.errors += 1;
      backoffMs = e.retryAfterMs || Math.min(60000, (backoffMs || 5000) * 2);
      log(`[grok] ${e.message} (backing off ${backoffMs / 1000}s)`);
    }
  }
  stats.posts += tweets.filter((t) => !state.seen.includes(t.id)).length;
  heartbeat();
  // Retry posts that were seen but not settled (unverified, no route yet, buy failed).
  for (const [id, entry] of Object.entries(state.pending)) {
    if (!tweets.some((t) => t.id === id)) tweets.push(entry.tweet);
  }
  for (const t of tweets) {
    const pending = state.pending[t.id];
    if (state.seen.includes(t.id) && !pending) continue;
    if (!state.seen.includes(t.id)) { state.seen.push(t.id); if (state.seen.length > 500) state.seen.splice(0, state.seen.length - 500); }
    const merged = pending ? { ...pending.tweet, ...t, text: t.text || pending.tweet.text } : t;
    let settled = false;
    try { settled = await handleTweet(merged); } catch (e) { log(`[post] ${t.id} error: ${e.message}`); }
    if (settled) delete state.pending[t.id];
    else {
      const tries = (pending?.tries || 0) + 1;
      if (tries >= Math.ceil((CFG.routeWaitS * 1000) / CFG.pollMs) + 20) { log(`[post] ${t.id}: giving up after ${tries} tries`); delete state.pending[t.id]; }
      else state.pending[t.id] = { tweet: merged, tries };
    }
    saveState(state);
  }
}

async function main() {
  if (!CFG.xaiKey && !CFG.bearer) { log('no source configured: set XAI_API_KEY (Grok x_search) or TWITTER_BEARER_TOKEN (X API v2)'); process.exit(1); }
  log(`watching @${CFG.handle} every ${CFG.pollMs / 1000}s via ${[CFG.bearer && 'X API', CFG.xaiKey && `Grok ${CFG.xaiModel}`].filter(Boolean).join(' + ')}; `
    + `dryRun=${CFG.dryRun} verify=${CFG.verify} routeWait=${CFG.routeWaitS}s slippage=${CFG.slippageBps}-${CFG.maxSlippageBps}bps`);
  await connectEvm();
  if (!evmEnabled() && !solEnabled()) { log('no wallet configured: set WALLET_PRIVATE_KEY (Solana) and/or SNIPER_EVM_PRIVATE_KEY (Base/Ethereum)'); process.exit(1); }
  if (evmEnabled()) {
    const addr = Object.values(evm.wallets)[0].address;
    const bals = await Promise.all(Object.entries(evm.providers).map(async ([id, p]) => { try { return `${CHAIN_NAMES[id]} ${ethers.formatEther(await p.getBalance(addr))} ETH`; } catch { return `${CHAIN_NAMES[id]} ?`; } }));
    log(`EVM wallet ${addr}: ${bals.join(', ')}; spend cap ${CFG.evm.spendEth} ETH per buy; router ${CFG.evm.zeroExKey ? '0x then KyberSwap' : 'KyberSwap'}; rpc ${Object.entries(evm.rpc).map(([id, u]) => `${CHAIN_NAMES[id]}=${redact(u).replace(/\/v2\/.*/, '/v2/***')}`).join(' ')}`);
  } else log('EVM buying off (needs SNIPER_EVM_PRIVATE_KEY: a burner Base wallet private key)');
  if (solEnabled()) {
    let bal = '?';
    try { bal = ((await sol.connection.getBalance(sol.keypair.publicKey)) / LAMPORTS_PER_SOL).toFixed(4); } catch { /* keep ? */ }
    log(`Solana wallet ${sol.keypair.publicKey.toBase58()}: ${bal} SOL; spend cap ${CFG.sol.spendSol} SOL per buy`);
  } else log('Solana buying off (needs WALLET_PRIVATE_KEY)');
  if (state.bought) log(`already bought once: ${JSON.stringify(state.buy)} — delete ${CFG.stateFile} to arm again`);
  if (state.position && !state.position.sold) log(`[tp] resuming watch on ${state.position.address} (${state.position.chainName}); sell everything at ${state.position.takeProfitX}x of ${state.position.spend}`);
  log(`take profit: sell all at ${TAKE_PROFIT_X}x, checked every ${TP_CHECK_MS / 1000}s; POST /api/sniper/sell forces a sale`);
  if (CFG.dryRun) log('DRY RUN: nothing will be sent. Set SNIPER_DRY_RUN=false to go live.');
  if (CFG.xaiKey) {
    try { await grokProbe(); } catch (e) { log(`[probe] Grok read failed: ${e.message}`); }
  }
  if (env('SNIPER_TEST_TEXT').trim()) await runTest(env('SNIPER_TEST_TEXT'), 'SNIPER_TEST_TEXT');
  for (;;) {
    try { await tick(); } catch (e) { log(`[tick] ${e.message}`); }
    await sleep(CFG.pollMs + backoffMs);
  }
}

main().catch((e) => { log(`fatal: ${e.stack || e}`); process.exit(1); });
