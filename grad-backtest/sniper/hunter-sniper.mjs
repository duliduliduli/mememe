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
    zeroExKey: env('ZEROEX_API_KEY').trim(),
    priorityGwei: num('SNIPER_PRIORITY_FEE_GWEI', num('PRIORITY_FEE_GWEI', 3)),
    rpcs: { 8453: env('BASE_RPC', 'https://mainnet.base.org'), 1: env('ETH_RPC', 'https://ethereum-rpc.publicnode.com') },
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
const evm = { providers: {}, wallets: {} };
if (CFG.evm.key) {
  for (const [id, url] of Object.entries(CFG.evm.rpcs)) {
    const chainId = Number(id);
    evm.providers[chainId] = new ethers.JsonRpcProvider(url, chainId, { staticNetwork: true });
    evm.wallets[chainId] = new ethers.Wallet(CFG.evm.key, evm.providers[chainId]);
  }
}
const sol = { connection: null, keypair: null };
if (CFG.sol.key) {
  sol.keypair = Keypair.fromSecretKey(BS58.decode(CFG.sol.key));
  sol.connection = new Connection(CFG.sol.rpc, { commitment: 'confirmed' });
}
const evmEnabled = () => Boolean(CFG.evm.key && CFG.evm.zeroExKey);
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

async function grokRequest(userPrompt, timeoutMs = 45000) {
  const today = new Date();
  const fromDate = new Date(today.getTime() - 24 * 3600 * 1000).toISOString().slice(0, 10);
  // Citations are returned by default on the Responses API; optional arguments are dropped
  // one by one if the API says it does not support them, so a schema change never stalls us.
  const body = {
    model: CFG.xaiModel,
    input: [{ role: 'system', content: GROK_SYSTEM }, { role: 'user', content: userPrompt }],
    tools: [{ type: 'x_search', allowed_x_handles: [CFG.handle], from_date: fromDate }],
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
        || (/"from_date"|"allowed_x_handles"/.test(r.text) ? 'tool-args' : null);
      if (culprit === 'tool-args' && body.tools[0].from_date) { delete body.tools[0].from_date; log('[grok] API rejected from_date; retrying without it'); continue; }
      if (culprit && culprit !== 'tool-args') { delete body[culprit]; log(`[grok] API rejected "${culprit}"; retrying without it`); continue; }
    }
    if (!r.ok) throw new Error(`xAI ${r.status}: ${r.text.slice(0, 300)}`);
    return collectResponseText(r.json ?? {});
  }
}

async function grokTweets() {
  const sinceIso = new Date(STARTED_AT - CFG.maxAgeS * 1000).toISOString();
  const prompt = `Using x_search, find every post published by @${CFG.handle} after ${sinceIso} (UTC), newest first, at most 10. `
    + 'Reply with ONLY a JSON array, no prose, no markdown: '
    + '[{"id":"<numeric status id>","url":"https://x.com/' + CFG.handle + '/status/<id>","created_at":"<ISO 8601 UTC>","text":"<full verbatim post text with every URL and address exactly as written>"}]. '
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
    if (!evmEnabled()) log(`[detect] EVM address ${a} seen but EVM buying is not configured (SNIPER_EVM_PRIVATE_KEY + ZEROEX_API_KEY)`);
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

async function buyEvm(chainId, tokenAddr) {
  const wallet = evm.wallets[chainId];
  const provider = evm.providers[chainId];
  const sellAmount = ethers.parseEther(CFG.evm.spendEth).toString();
  return withRouteRetry(async (slippageBps) => {
    const q = new URLSearchParams({ chainId: String(chainId), sellToken: NATIVE_ETH, buyToken: tokenAddr, sellAmount, taker: wallet.address, slippageBps: String(slippageBps) });
    const r = await fetchJson(`https://api.0x.org/swap/allowance-holder/quote?${q}`, { headers: { '0x-api-key': CFG.evm.zeroExKey, '0x-version': 'v2' } }, 15000);
    if (!r.ok) throw new Error(`0x quote ${r.status}: ${r.text.slice(0, 300)}`);
    const quote = r.json || {};
    if (quote.liquidityAvailable === false || !quote.transaction) throw new Error(`no route: ${r.text.slice(0, 200)}`);
    const fee = await provider.getFeeData();
    const priority = ethers.parseUnits(String(CFG.evm.priorityGwei), 'gwei');
    const base = fee.maxFeePerGas || fee.gasPrice || priority;
    const tx = {
      to: quote.transaction.to, data: quote.transaction.data, value: BigInt(quote.transaction.value || sellAmount),
      gasLimit: BigInt(quote.transaction.gas || 600000) * 13n / 10n, maxPriorityFeePerGas: priority, maxFeePerGas: base * 2n + priority, chainId,
    };
    if (CFG.dryRun) {
      log(`[DRY RUN] would buy ${tokenAddr} on ${CHAIN_NAMES[chainId]} for ${CFG.evm.spendEth} ETH; est. out ${quote.buyAmount}`);
      return { hash: 'DRY_RUN', chain: CHAIN_NAMES[chainId], spend: `${CFG.evm.spendEth} ETH` };
    }
    const sent = await wallet.sendTransaction(tx);
    log(`[buy] ${CHAIN_NAMES[chainId]} tx sent ${sent.hash}`);
    const receipt = await sent.wait(1);
    if (receipt.status !== 1) throw new Error(`tx ${sent.hash} reverted`);
    log(`[buy] confirmed in block ${receipt.blockNumber}`);
    return { hash: sent.hash, chain: CHAIN_NAMES[chainId], spend: `${CFG.evm.spendEth} ETH` };
  }, `0x:${CHAIN_NAMES[chainId]}`);
}

async function buySolana(mint) {
  const lamports = Math.floor(CFG.sol.spendSol * LAMPORTS_PER_SOL);
  return withRouteRetry(async (slippageBps, attempt) => {
    const q = new URLSearchParams({ inputMint: WSOL, outputMint: mint, amount: String(lamports), slippageBps: String(slippageBps), restrictIntermediateTokens: 'true' });
    const quote = await fetchJson(`${CFG.sol.jupiter}/quote?${q}`, {}, 12000);
    if (!quote.ok || !quote.json?.outAmount) throw new Error(`no route: jupiter quote ${quote.status} ${quote.text.slice(0, 200)}`);
    if (CFG.dryRun) {
      log(`[DRY RUN] would buy ${mint} on Solana for ${CFG.sol.spendSol} SOL; est. out ${quote.json.outAmount} (raw) via ${(quote.json.routePlan || []).map((p) => p.swapInfo?.label).join('>')}`);
      return { hash: 'DRY_RUN', chain: 'Solana', spend: `${CFG.sol.spendSol} SOL` };
    }
    const swap = await fetchJson(`${CFG.sol.jupiter}/swap`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        quoteResponse: quote.json, userPublicKey: sol.keypair.publicKey.toBase58(), wrapAndUnwrapSol: true,
        dynamicComputeUnitLimit: true, dynamicSlippage: false,
        prioritizationFeeLamports: { priorityLevelWithMaxLamports: { maxLamports: CFG.sol.priorityLamports, priorityLevel: 'veryHigh' } },
      }),
    }, 15000);
    if (!swap.ok || !swap.json?.swapTransaction) throw new Error(`jupiter swap ${swap.status}: ${swap.text.slice(0, 200)}`);
    const tx = VersionedTransaction.deserialize(Buffer.from(swap.json.swapTransaction, 'base64'));
    tx.sign([sol.keypair]);
    const sig = await sol.connection.sendRawTransaction(tx.serialize(), { skipPreflight: true, maxRetries: 3 });
    log(`[buy] Solana tx sent ${sig} (attempt ${attempt})`);
    const height = swap.json.lastValidBlockHeight;
    const conf = await sol.connection.confirmTransaction({ signature: sig, blockhash: tx.message.recentBlockhash, lastValidBlockHeight: height }, 'confirmed');
    if (conf.value.err) throw new Error(`tx ${sig} failed: ${JSON.stringify(conf.value.err)}`);
    log(`[buy] confirmed ${sig}`);
    return { hash: sig, chain: 'Solana', spend: `${CFG.sol.spendSol} SOL` };
  }, 'jupiter');
}

async function buy(target) {
  return target.chain === 'sol' ? buySolana(target.address) : buyEvm(target.chain, target.address);
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
  await notify(`🚨 SNIPING ${target.address} on ${CHAIN_NAMES[target.chain]} from post ${tweet.url || tweet.id}${CFG.dryRun ? ' (DRY RUN)' : ''}`);
  try {
    const res = await buy(target);
    if (!CFG.dryRun) { state.bought = true; state.buy = { ...res, address: target.address, tweet: tweet.id, at: new Date().toISOString() }; saveState(state); }
    await notify(`✅ ${CFG.dryRun ? 'DRY RUN ' : ''}bought ${target.address} on ${res.chain} for ${res.spend}: ${res.hash}`);
  } catch (e) {
    await notify(`❌ Buy failed for ${target.address} on ${CHAIN_NAMES[target.chain]}: ${String(e.message || e).slice(0, 300)}`);
    return false;
  }
  return true;
}

let backoffMs = 0;
async function tick() {
  const tweets = [];
  if (CFG.bearer) {
    try { tweets.push(...await xApiTweets()); } catch (e) { log(`[x] ${e.message}`); }
  }
  if (CFG.xaiKey && (!CFG.bearer || tweets.length === 0)) {
    try { tweets.push(...await grokTweets()); backoffMs = 0; } catch (e) {
      backoffMs = e.retryAfterMs || Math.min(60000, (backoffMs || 5000) * 2);
      log(`[grok] ${e.message} (backing off ${backoffMs / 1000}s)`);
    }
  }
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
  if (!evmEnabled() && !solEnabled()) { log('no wallet configured: set WALLET_PRIVATE_KEY (Solana) and/or SNIPER_EVM_PRIVATE_KEY + ZEROEX_API_KEY (Base/Ethereum)'); process.exit(1); }
  log(`watching @${CFG.handle} every ${CFG.pollMs / 1000}s via ${[CFG.bearer && 'X API', CFG.xaiKey && `Grok ${CFG.xaiModel}`].filter(Boolean).join(' + ')}; `
    + `dryRun=${CFG.dryRun} verify=${CFG.verify} routeWait=${CFG.routeWaitS}s slippage=${CFG.slippageBps}-${CFG.maxSlippageBps}bps`);
  if (evmEnabled()) {
    const addr = evm.wallets[1].address;
    const bals = await Promise.all(Object.entries(evm.providers).map(async ([id, p]) => { try { return `${CHAIN_NAMES[id]} ${ethers.formatEther(await p.getBalance(addr))} ETH`; } catch { return `${CHAIN_NAMES[id]} ?`; } }));
    log(`EVM wallet ${addr}: ${bals.join(', ')}; spend cap ${CFG.evm.spendEth} ETH per buy`);
  } else log('EVM buying off (needs SNIPER_EVM_PRIVATE_KEY + ZEROEX_API_KEY)');
  if (solEnabled()) {
    let bal = '?';
    try { bal = ((await sol.connection.getBalance(sol.keypair.publicKey)) / LAMPORTS_PER_SOL).toFixed(4); } catch { /* keep ? */ }
    log(`Solana wallet ${sol.keypair.publicKey.toBase58()}: ${bal} SOL; spend cap ${CFG.sol.spendSol} SOL per buy`);
  } else log('Solana buying off (needs WALLET_PRIVATE_KEY)');
  if (state.bought) log(`already bought once: ${JSON.stringify(state.buy)} — delete ${CFG.stateFile} to arm again`);
  if (CFG.dryRun) log('DRY RUN: nothing will be sent. Set SNIPER_DRY_RUN=false to go live.');
  for (;;) {
    try { await tick(); } catch (e) { log(`[tick] ${e.message}`); }
    await sleep(CFG.pollMs + backoffMs);
  }
}

main().catch((e) => { log(`fatal: ${e.stack || e}`); process.exit(1); });
