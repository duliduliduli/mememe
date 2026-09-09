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
  collectResponseText, extractCandidates, oembedText, parseSyndicationTimeline, parseTweetResult, parseTweetsJson,
  statusRefs, syndicationToken, tweetTimeMs,
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
  // X's public syndication timeline (what embedded profile widgets read): free, polled every
  // SNIPER_POLL_MS. Grok then only runs as a backup on its own interval, which is what keeps
  // the xAI bill down: every Grok poll is a paid X search.
  syndication: flag('SNIPER_SYNDICATION', true),
  grokBackupMs: Math.max(30000, num('SNIPER_GROK_BACKUP_MS', 300000)),   // when the free feed works
  grokIntervalMs: Math.max(10000, num('SNIPER_GROK_INTERVAL_MS', 30000)), // when it does not
  verify: env('SNIPER_VERIFY', 'auto').toLowerCase(),          // auto | oembed | grok | none
  trustUnverified: flag('SNIPER_TRUST_UNVERIFIED', false),
  maxAgeS: num('SNIPER_MAX_POST_AGE_S', 900),                   // never act on posts older than this
  routeWaitS: num('SNIPER_ROUTE_WAIT_S', 240),                  // keep asking for a route this long
  slippageBps: num('SNIPER_SLIPPAGE_BPS', num('SLIPPAGE_BPS', 1500)),
  maxSlippageBps: num('SNIPER_MAX_SLIPPAGE_BPS', 3000),
  webhook: env('DISCORD_WEBHOOK').trim(),
  stateFile: env('SNIPER_STATE_FILE', path.join(DATA_DIR, 'sniper-state.json')),
  // A known contract (scheduled TGE): buy the instant a route exists, no post needed. Posts
  // naming any other address are then ignored. SNIPER_LAUNCH_AT (ISO time) only tunes the
  // cadence: slow polls until SNIPER_LAUNCH_LEAD_S before it, fast polls from then on.
  targetContract: env('SNIPER_TARGET_CONTRACT').trim(),
  launchAt: Date.parse(env('SNIPER_LAUNCH_AT').trim()) || null,
  launchLeadS: num('SNIPER_LAUNCH_LEAD_S', 600),
  launchPollMs: Math.max(1000, num('SNIPER_LAUNCH_POLL_MS', 2000)),
  targetIdlePollMs: Math.max(5000, num('SNIPER_TARGET_IDLE_POLL_MS', 10000)),
  // Entry cap: skip the buy while the quoted price implies a fully diluted valuation above
  // this many USD, and buy the moment it dips under. 0 = buy at the first route.
  maxEntryFdvUsd: num('SNIPER_MAX_ENTRY_FDV_USD', 0),
  logFile: env('SNIPER_LOG_FILE', path.join(DATA_DIR, 'sniper.log')),
  // Spend = this share of the wallet's native balance at buy time, leaving the reserve for
  // gas. 0 switches to the fixed SNIPER_SPEND_ETH / SNIPER_SPEND_SOL amounts instead.
  spendPct: Math.min(100, Math.max(0, num('SNIPER_SPEND_PCT', 90))),
  evm: {
    key: env('SNIPER_EVM_PRIVATE_KEY', env('PRIVATE_KEY')).trim(),
    spendEth: env('SNIPER_SPEND_ETH', env('SPEND_ETH', '0.1')),
    gasReserveEth: env('SNIPER_GAS_RESERVE_ETH', '0.002'),
    zeroExKey: env('ZEROEX_API_KEY').trim(),                    // optional: 0x is tried first when present
    priorityGwei: num('SNIPER_PRIORITY_FEE_GWEI', num('PRIORITY_FEE_GWEI', 3)),
    rpcs: { 8453: evmRpcCandidates(8453), 1: evmRpcCandidates(1) },
  },
  sol: {
    key: env('SNIPER_SOL_PRIVATE_KEY', env('WALLET_PRIVATE_KEY')).trim(),
    spendSol: num('SNIPER_SPEND_SOL', 0.3),
    reserveSol: num('SNIPER_SOL_RESERVE', 0.03),
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

const BROWSER_UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36';
// X throttles the syndication feed per IP (HTTP 429): the interval doubles on a 429 up to
// two minutes and eases back toward the poll interval on success, so the lane asks as
// often as X allows instead of burning most polls on refusals.
const synd = { ok: 0, errors: 0, lastOkAt: 0, lastError: '', lastTry: 0, intervalMs: 0 };
const syndicationDue = () => Date.now() - synd.lastTry >= (synd.intervalMs || CFG.pollMs);

/** Posts from X's public syndication timeline for the handle. Free, no key, X's own data. */
async function syndicationTweets() {
  const urls = [
    `https://syndication.twitter.com/srv/timeline-profile/screen-name/${CFG.handle}`,
    `https://syndication.twitter.com/srv/timeline-profile/screen-name/${CFG.handle}?showReplies=false`,
  ];
  let lastErr = 'no response';
  synd.lastTry = Date.now();
  let throttled = false;
  for (const url of urls) {
    let r;
    try { r = await fetchJson(url, { headers: { 'User-Agent': BROWSER_UA, Accept: 'text/html,*/*' } }, 10000); } catch (e) { lastErr = e.message; continue; }
    if (r.status === 429) { throttled = true; lastErr = 'HTTP 429'; break; }
    if (!r.ok) { lastErr = `HTTP ${r.status}`; continue; }
    if (!/__NEXT_DATA__/.test(r.text)) { lastErr = 'no timeline data in response'; continue; }
    synd.ok += 1; synd.lastOkAt = Date.now();
    synd.intervalMs = Math.max(CFG.pollMs, Math.floor((synd.intervalMs || CFG.pollMs) * 0.75));
    return parseSyndicationTimeline(r.text, CFG.handle).map((t) => ({ ...t, source: 'x-timeline', verified: true }));
  }
  synd.errors += 1; synd.lastError = lastErr;
  if (throttled) synd.intervalMs = Math.min(120000, (synd.intervalMs || CFG.pollMs) * 2);
  throw new Error(lastErr);
}
const syndicationHealthy = () => synd.lastOkAt > 0 && Date.now() - synd.lastOkAt < 3 * (synd.intervalMs || CFG.pollMs) + 5000;

/** One post by id from X's tweet-result endpoint (also public): the verification used
 *  before the embed and the second Grok read. */
async function tweetResultVerify(tweet) {
  const url = `https://cdn.syndication.twimg.com/tweet-result?id=${tweet.id}&token=${syndicationToken(tweet.id)}&lang=en`;
  const r = await fetchJson(url, { headers: { 'User-Agent': BROWSER_UA } }, 10000);
  if (!r.ok || !r.json) throw new Error(`tweet-result ${r.status}`);
  const post = parseTweetResult(r.json, CFG.handle);
  if (!post) throw new Error('tweet-result: not this account, or the post is gone');
  return post.text;
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
  if (CFG.verify === 'auto') {
    try { return { verified: true, text: await tweetResultVerify(tweet), how: 'tweet-result' }; } catch (e) { errors.push(`tweet-result: ${e.message}`); }
  }
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

// Direct DEX routers, for a pool so new that no aggregator has indexed it yet (ETH -> token
// only; sells go through the aggregators, which have caught up by then). Base addresses are
// the canonical deployments; override with SNIPER_DEX_* if needed.
const DEX = {
  8453: {
    weth: env('SNIPER_DEX_WETH', '0x4200000000000000000000000000000000000006'),
    v3Router: env('SNIPER_DEX_UNIV3_ROUTER', '0x2626664c2603336E57B271c5C0b26F421741e481'),
    v3Quoter: env('SNIPER_DEX_UNIV3_QUOTER', '0x3d4e44Eb1374240CE5F1B871ab261CD16335B76a'),
    v2Router: env('SNIPER_DEX_UNIV2_ROUTER', '0x4752ba5DBc23f44D87826276BF6Fd6b1C372aD24'),
    aeroRouter: env('SNIPER_DEX_AERO_ROUTER', '0xcF77a3Ba9A5CA399B7c97c74d54e5b1Beb874E43'),
    aeroFactory: env('SNIPER_DEX_AERO_FACTORY', '0x420DD381b31aEf6683db6B902084cB0FFECe40Da'),
  },
};
const V3_QUOTER_ABI = ['function quoteExactInputSingle((address tokenIn,address tokenOut,uint256 amountIn,uint24 fee,uint160 sqrtPriceLimitX96)) returns (uint256 amountOut,uint160 sqrtPriceX96After,uint32 initializedTicksCrossed,uint256 gasEstimate)'];
const V3_ROUTER_ABI = ['function exactInputSingle((address tokenIn,address tokenOut,uint24 fee,address recipient,uint256 amountIn,uint256 amountOutMinimum,uint160 sqrtPriceLimitX96)) payable returns (uint256 amountOut)'];
const V2_ROUTER_ABI = [
  'function getAmountsOut(uint256 amountIn,address[] path) view returns (uint256[] amounts)',
  'function swapExactETHForTokensSupportingFeeOnTransferTokens(uint256 amountOutMin,address[] path,address to,uint256 deadline) payable',
];
const AERO_ROUTER_ABI = [
  'function getAmountsOut(uint256 amountIn,(address from,address to,bool stable,address factory)[] routes) view returns (uint256[] amounts)',
  'function swapExactETHForTokensSupportingFeeOnTransferTokens(uint256 amountOutMin,(address from,address to,bool stable,address factory)[] routes,address to,uint256 deadline) payable',
];

async function quoteDirect(chainId, tokenOut, sellAmount, taker, slippageBps) {
  const dex = DEX[chainId];
  if (!dex) throw new Error('no route: no direct DEX routers configured for this chain');
  const provider = evm.providers[chainId];
  const amountIn = BigInt(sellAmount);
  const minOut = (out) => out * BigInt(10000 - slippageBps) / 10000n;
  const deadline = Math.floor(Date.now() / 1000) + 90;
  const candidates = [];
  const quoter = new ethers.Contract(dex.v3Quoter, V3_QUOTER_ABI, provider);
  const v3 = new ethers.Interface(V3_ROUTER_ABI);
  await Promise.all([10000, 3000, 500, 100].map(async (fee) => {
    try {
      const [out] = await quoter.quoteExactInputSingle.staticCall({ tokenIn: dex.weth, tokenOut, amountIn, fee, sqrtPriceLimitX96: 0 });
      if (out > 0n) candidates.push({ out, via: `uniswap-v3:${fee}`, to: dex.v3Router, data: v3.encodeFunctionData('exactInputSingle', [{ tokenIn: dex.weth, tokenOut, fee, recipient: taker, amountIn, amountOutMinimum: minOut(out), sqrtPriceLimitX96: 0 }]) });
    } catch { /* no pool at this fee */ }
  }));
  try {
    const v2 = new ethers.Contract(dex.v2Router, V2_ROUTER_ABI, provider);
    const amounts = await v2.getAmountsOut(amountIn, [dex.weth, tokenOut]);
    const out = amounts[amounts.length - 1];
    if (out > 0n) candidates.push({ out, via: 'uniswap-v2', to: dex.v2Router, data: v2.interface.encodeFunctionData('swapExactETHForTokensSupportingFeeOnTransferTokens', [minOut(out), [dex.weth, tokenOut], taker, deadline]) });
  } catch { /* no v2 pair */ }
  try {
    const aero = new ethers.Contract(dex.aeroRouter, AERO_ROUTER_ABI, provider);
    const routes = [{ from: dex.weth, to: tokenOut, stable: false, factory: dex.aeroFactory }];
    const amounts = await aero.getAmountsOut(amountIn, routes);
    const out = amounts[amounts.length - 1];
    if (out > 0n) candidates.push({ out, via: 'aerodrome', to: dex.aeroRouter, data: aero.interface.encodeFunctionData('swapExactETHForTokensSupportingFeeOnTransferTokens', [minOut(out), routes, taker, deadline]) });
  } catch { /* no aero pool */ }
  if (!candidates.length) throw new Error('no route: no direct pool on uniswap v3/v2 or aerodrome');
  candidates.sort((a, b) => (a.out > b.out ? -1 : 1));
  const best = candidates[0];
  return { to: best.to, data: best.data, value: amountIn, gas: 0n, buyAmount: best.out.toString(), spender: null, via: best.via };
}

/** Best executable quote: aggregators first (0x when keyed, KyberSwap), direct routers for
 *  ETH buys as well; the largest output wins so a brand-new pool is caught before the
 *  aggregators index it. */
async function quoteEvm(chainId, tokenIn, tokenOut, sellAmount, taker, slippageBps, opts = {}) {
  const sources = opts.aggregators === false ? [] : [...(CFG.evm.zeroExKey ? [quote0x] : []), quoteKyber];
  if (tokenIn === NATIVE_ETH && DEX[chainId]) sources.push((c, ti, to, amt, tk, sl) => quoteDirect(c, to, amt, tk, sl));
  if (!sources.length) throw new Error('no route: no quote source for this pair');
  const results = await Promise.all(sources.map((fn) => fn(chainId, tokenIn, tokenOut, sellAmount, taker, slippageBps).then((q) => ({ q }), (e) => ({ e: e.message }))));
  const quotes = results.filter((r) => r.q).map((r) => r.q).sort((a, b) => (BigInt(a.buyAmount || 0) > BigInt(b.buyAmount || 0) ? -1 : 1));
  if (!quotes.length) throw new Error(results.map((r) => r.e).join(' | '));
  return quotes[0];
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

/** Wei to spend on an EVM buy right now: SNIPER_SPEND_PCT of the live balance, capped so the
 *  gas reserve stays; or the fixed SNIPER_SPEND_ETH when the percent is 0. */
async function evmSpendWei(chainId) {
  if (CFG.spendPct <= 0) return ethers.parseEther(CFG.evm.spendEth);
  const balance = await evm.providers[chainId].getBalance(evm.wallets[chainId].address);
  const reserve = ethers.parseEther(CFG.evm.gasReserveEth);
  const byPct = balance * BigInt(Math.round(CFG.spendPct * 100)) / 10000n;
  const spend = byPct < balance - reserve ? byPct : balance - reserve;
  if (spend <= 0n) throw new Error(`wallet holds ${ethers.formatEther(balance)} ETH on ${CHAIN_NAMES[chainId]}, below the ${CFG.evm.gasReserveEth} ETH gas reserve`);
  return spend;
}

async function solSpendLamports() {
  if (CFG.spendPct <= 0) return Math.floor(CFG.sol.spendSol * LAMPORTS_PER_SOL);
  const balance = await sol.connection.getBalance(sol.keypair.publicKey);
  const reserve = Math.floor(CFG.sol.reserveSol * LAMPORTS_PER_SOL);
  const spend = Math.min(Math.floor(balance * CFG.spendPct / 100), balance - reserve);
  if (spend <= 0) throw new Error(`wallet holds ${(balance / LAMPORTS_PER_SOL).toFixed(4)} SOL, below the ${CFG.sol.reserveSol} SOL reserve`);
  return spend;
}

function spendRule() {
  return CFG.spendPct > 0 ? `${CFG.spendPct}% of the wallet balance at buy time` : `fixed ${CFG.evm.spendEth} ETH / ${CFG.sol.spendSol} SOL`;
}

async function buyEvm(chainId, tokenAddr) {
  const wallet = evm.wallets[chainId];
  const sellAmount = (await evmSpendWei(chainId)).toString();
  const spend = `${Number(ethers.formatEther(sellAmount)).toFixed(5)} ETH`;
  return withRouteRetry(async (slippageBps, attempt) => {
    const quote = await quoteEvm(chainId, NATIVE_ETH, tokenAddr, sellAmount, wallet.address, slippageBps);
    if (CFG.dryRun) {
      log(`[DRY RUN] would buy ${tokenAddr} on ${CHAIN_NAMES[chainId]} for ${spend} via ${quote.via}; est. out ${quote.buyAmount} (attempt ${attempt})`);
      return { hash: 'DRY_RUN', chain: CHAIN_NAMES[chainId], spend, spentRaw: sellAmount };
    }
    const { hash } = await sendEvmSwap(chainId, NATIVE_ETH, sellAmount, quote, 'buy');
    return { hash, chain: CHAIN_NAMES[chainId], spend, spentRaw: sellAmount };
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
  const lamports = await solSpendLamports();
  const spend = `${(lamports / LAMPORTS_PER_SOL).toFixed(4)} SOL`;
  return withRouteRetry(async (slippageBps, attempt) => {
    const quote = await jupiterQuote(WSOL, mint, lamports, slippageBps);
    if (CFG.dryRun) {
      log(`[DRY RUN] would buy ${mint} on Solana for ${spend}; est. out ${quote.outAmount} (raw) via ${(quote.routePlan || []).map((p) => p.swapInfo?.label).join('>')}`);
      return { hash: 'DRY_RUN', chain: 'Solana', spend, spentRaw: String(lamports) };
    }
    const sig = await jupiterSend(quote, 'buy', attempt);
    return { hash: sig, chain: 'Solana', spend, spentRaw: String(lamports) };
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
// Take profit: after the buy, watch the token's price against the entry and sell in stages.
// SNIPER_TAKE_PROFIT_LADDER = "5:initial,10:80" means: at 5x sell just enough to get the
// initial spend back, at 10x sell 80% of what is left, then let the rest ride. Rungs are
// "<multiple>:<initial|percent>", comma separated, ascending. Price multiple = executable
// sell value of the current bag per token / entry price per token, so impact is included.
// $DATA_DIR/sniper-sell.json (POST /api/sniper/sell) sells everything left at any price.
// ---------------------------------------------------------------------------------------
function parseLadder(spec) {
  const rungs = [];
  for (const part of String(spec || '').split(',')) {
    const m = part.trim().match(/^(\d+(?:\.\d+)?)\s*[:x@]\s*(initial|\d+(?:\.\d+)?%?)$/i);
    if (!m) continue;
    const x = Number(m[1]);
    const sell = /^initial$/i.test(m[2]) ? 'initial' : Math.min(100, Math.max(1, Number(m[2].replace('%', ''))));
    if (x > 1) rungs.push({ x, sell });
  }
  return rungs.sort((a, b) => a.x - b.x);
}
const TAKE_PROFIT_LADDER = parseLadder(env('SNIPER_TAKE_PROFIT_LADDER', '3:initial,6:50,10:80'));
if (!TAKE_PROFIT_LADDER.length) TAKE_PROFIT_LADDER.push({ x: 3, sell: 'initial' }, { x: 6, sell: 50 }, { x: 10, sell: 80 });
const TP_CHECK_MS = Math.max(5000, num('SNIPER_TP_CHECK_MS', 15000));
const SELL_FILE = path.join(DATA_DIR, 'sniper-sell.json');
const tp = { lastCheck: 0, lastLogged: null, multiple: null, quoteErrors: 0 };

function describeLadder(rungs) {
  return rungs.map((r) => `at ${r.x}x sell ${r.sell === 'initial' ? 'the initial stake back' : `${r.sell}% of what is left`}`).join(', ') + ', then let the rest ride';
}

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
    buyHash: res.hash, tweet: tweetId, openedAt: new Date().toISOString(),
    ladder: TAKE_PROFIT_LADDER.map((r) => ({ ...r, done: null })), initialBalanceRaw: null, sales: [], sold: null,
  };
  saveState(state);
  log(`[tp] watching ${target.address} on ${CHAIN_NAMES[target.chain]}: ${describeLadder(TAKE_PROFIT_LADDER)}`);
}

/** The token amount the buy produced, read from the wallet (retried: balances can lag a
 *  block or two). Needed to turn bag value into a per-token price multiple. */
async function ensureInitialBalance(pos) {
  if (pos.initialBalanceRaw) return BigInt(pos.initialBalanceRaw);
  for (let i = 0; i < 6; i++) {
    try {
      const bal = await tokenBalance(pos);
      if (bal > 0n) {
        pos.initialBalanceRaw = bal.toString(); saveState(state);
        let detail = '';
        if (pos.chain !== 'sol') {
          try {
            const erc20 = new ethers.Contract(pos.address, [...ERC20_ABI, 'function decimals() view returns (uint8)', 'function totalSupply() view returns (uint256)'], evm.providers[pos.chain]);
            const [dec, supply] = await Promise.all([erc20.decimals(), erc20.totalSupply()]);
            const tokens = Number(bal) / 10 ** Number(dec);
            const priceEth = Number(ethers.formatEther(pos.spentRaw)) / tokens;
            const fdvEth = priceEth * Number(supply) / 10 ** Number(dec);
            detail = ` = ${tokens.toLocaleString('en-US', { maximumFractionDigits: 0 })} tokens; entry ${priceEth.toExponential(3)} ETH/token; FDV about ${fdvEth.toFixed(1)} ETH`;
          } catch { /* cosmetic */ }
        }
        log(`[tp] entry: ${bal} raw tokens for ${pos.spend}${detail}`);
        return bal;
      }
    } catch { /* retry */ }
    await sleep(2000);
  }
  return null;
}

function fmtNative(pos, raw) {
  return pos.chain === 'sol' ? `${(Number(raw) / LAMPORTS_PER_SOL).toFixed(4)} SOL` : `${Number(ethers.formatEther(raw)).toFixed(5)} ETH`;
}

async function checkTakeProfit() {
  const pos = state.position;
  if (!pos) return;
  const manual = fs.existsSync(SELL_FILE);
  if (pos.sold) { if (manual) { try { fs.unlinkSync(SELL_FILE); } catch { /* gone */ } } return; }
  if (!pos.ladder) pos.ladder = TAKE_PROFIT_LADDER.map((r) => ({ ...r, done: null }));
  const pending = pos.ladder.filter((r) => !r.done);
  // Every rung done and no manual request: the rest rides untouched.
  if (!pending.length && !manual) return;
  if (!manual && Date.now() - tp.lastCheck < TP_CHECK_MS) return;
  tp.lastCheck = Date.now();
  if (pos.chain === 'sol' ? !solEnabled() : !evm.providers[pos.chain]) return;
  const initial = await ensureInitialBalance(pos);
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
  let rung = null;
  if (value !== undefined && initial) {
    // Price multiple per token: (value / balance) / (spent / initial balance).
    tp.multiple = (Number(value) / Number(balance)) / (Number(pos.spentRaw) / Number(initial));
    const next = pending[0];
    if (tp.lastLogged === null || Math.abs(tp.multiple - tp.lastLogged) >= Math.max(0.25, tp.lastLogged * 0.25)) {
      log(`[tp] ${pos.address.slice(0, 10)} at ${tp.multiple.toFixed(2)}x entry; bag sells for ${fmtNative(pos, value)}`
        + (next ? `; next rung ${next.x}x (${next.sell === 'initial' ? 'initial back' : `${next.sell}%`})` : '; all rungs done, rest rides'));
      tp.lastLogged = tp.multiple;
    }
    // Take the highest rung the price has crossed; skipped lower rungs are folded into it.
    for (const r of pending) if (tp.multiple >= r.x) rung = r;
  }
  if (!manual && !rung) return;
  let amount;
  let label;
  if (manual) { amount = balance; label = 'everything left'; }
  else if (rung.sell === 'initial') {
    // Tokens whose sale returns the initial spend, from the executable quote of the whole bag.
    amount = balance * BigInt(pos.spentRaw) / value;
    amount = amount > balance ? balance : amount;
    label = `the initial stake (${(Number(amount) * 100 / Number(balance)).toFixed(1)}% of the bag)`;
  } else {
    amount = balance * BigInt(Math.round(rung.sell * 100)) / 10000n;
    label = `${rung.sell}% of what is left`;
  }
  if (amount <= 0n) { if (manual) { try { fs.unlinkSync(SELL_FILE); } catch { /* gone */ } } return; }
  const why = manual ? 'manual sell requested' : `${tp.multiple.toFixed(2)}x >= ${rung.x}x rung`;
  await notify(`💰 SELLING ${label} of ${pos.address} on ${pos.chainName}: ${why}`);
  try {
    const res = pos.chain === 'sol' ? await sellSolana(pos, amount) : await sellEvm(pos, amount);
    const sale = { hash: res.hash, outRaw: res.outRaw, amountRaw: amount.toString(), at: new Date().toISOString(), why, rung: rung ? rung.x : 'manual' };
    pos.sales.push(sale);
    if (manual) pos.sold = { ...sale, remainderRaw: '0' };
    else for (const r of pending) if (tp.multiple >= r.x) r.done = sale;
    saveState(state);
    const left = balance - amount;
    const recovered = pos.sales.reduce((s, x) => s + BigInt(x.outRaw || 0), 0n);
    await notify(`✅ SOLD ${label} of ${pos.address} on ${pos.chainName} for about ${fmtNative(pos, res.outRaw)}: ${res.hash}. `
      + `Cashed out so far ${fmtNative(pos, recovered)} against ${pos.spend} spent; ${left > 0n ? `${(Number(left) * 100 / Number(initial || balance)).toFixed(1)}% of the original bag still riding` : 'nothing left'}.`);
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
  log(`[test] resolved ${target.address} on ${CHAIN_NAMES[target.chain]}; requesting a quote (spend rule: ${spendRule()})`);
  const saved = { dryRun: CFG.dryRun, routeWaitS: CFG.routeWaitS };
  CFG.dryRun = true; CFG.routeWaitS = 20;
  try {
    const res = await buy(target);
    log(`[test] OK: ${res.chain} buy path works end to end (would spend ${res.spend}); nothing was sent`);
  } catch (e) {
    log(`[test] FAILED at the quote/transaction step: ${String(e.message || e).slice(0, 300)}`);
  } finally { CFG.dryRun = saved.dryRun; CFG.routeWaitS = saved.routeWaitS; }
}

/** State-file loss (a redeploy without a persistent volume) must not strand a bag: rebuild the
 *  position from chain history. Alchemy's asset-transfer index lists ERC20 tokens this
 *  wallet received; a token we still hold that arrived in a transaction we sent with ETH
 *  attached is our buy, and that ETH is the spend. Rungs already taken are inferred from
 *  how much of the original bag is left. */
async function recoverEvmPosition() {
  for (const [id, provider] of Object.entries(evm.providers)) {
    const chainId = Number(id);
    if (!/alchemy\.com/.test(evm.rpc[chainId] || '')) continue;
    const wallet = evm.wallets[chainId].address;
    let res;
    try {
      res = await provider.send('alchemy_getAssetTransfers', [{ fromBlock: '0x0', toAddress: wallet, category: ['erc20'], order: 'desc', maxCount: '0x19', withMetadata: true }]);
    } catch (e) { log(`[tp] chain history unavailable on ${CHAIN_NAMES[chainId]} (${String(e.message).slice(0, 100)}); no position recovery`); continue; }
    for (const t of res?.transfers || []) {
      const token = t.rawContract?.address;
      if (!token || !ethers.isAddress(token)) continue;
      const when = Date.parse(t.metadata?.blockTimestamp || '');
      if (when && Date.now() - when > 14 * 24 * 3600 * 1000) break;
      const address = ethers.getAddress(token);
      let balance;
      try { balance = await tokenBalance({ chain: chainId, address }); } catch { continue; }
      if (balance <= 0n) continue;
      let tx;
      try { tx = await provider.getTransaction(t.hash); } catch { tx = null; }
      if (!tx || tx.from.toLowerCase() !== wallet.toLowerCase() || tx.value <= 0n) continue;
      const initial = BigInt(t.rawContract.value || '0x0');
      const ladder = TAKE_PROFIT_LADDER.map((r) => ({ ...r, done: null }));
      const leftFrac = initial > 0n ? Number(balance) / Number(initial) : 1;
      // Infer rungs already taken: the initial-back rung leaves ~80% at 5x, the 80% rung far less.
      if (leftFrac < 0.9 && ladder[0]) ladder[0].done = { hash: 'inferred', why: `only ${(leftFrac * 100).toFixed(0)}% of the original bag remains` };
      if (leftFrac < 0.4 && ladder[1]) ladder[1].done = { hash: 'inferred', why: `only ${(leftFrac * 100).toFixed(0)}% of the original bag remains` };
      const spend = `${Number(ethers.formatEther(tx.value)).toFixed(5)} ETH`;
      state.position = {
        chain: chainId, chainName: CHAIN_NAMES[chainId], address, spentRaw: tx.value.toString(), spend, buyHash: t.hash, tweet: 'recovered',
        openedAt: t.metadata?.blockTimestamp || new Date().toISOString(), ladder, initialBalanceRaw: initial > 0n ? initial.toString() : null, sales: [], sold: null,
      };
      state.bought = true;
      state.buy = { hash: t.hash, chain: CHAIN_NAMES[chainId], address, spend, at: state.position.openedAt, recovered: true };
      saveState(state);
      log(`[tp] recovered position from chain history: ${address} on ${CHAIN_NAMES[chainId]}, bought for ${spend} in ${t.hash}; `
        + `${(leftFrac * 100).toFixed(0)}% of the original bag held; rungs already taken: ${ladder.filter((r) => r.done).length}/${ladder.length}`);
      return;
    }
  }
}

// ---------------------------------------------------------------------------------------
// Launch watch: a known contract is bought the moment any venue quotes a route for it.
// ---------------------------------------------------------------------------------------
const launch = { target: null, info: '', attempts: 0, lastTry: 0, lastLog: 0, routeErrors: 0, lastAggregatorAt: 0, supplyRaw: null, decimals: null, lastCapLog: 0 };
const USDC = { 8453: '0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913', 1: '0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48' };
const ethUsd = { price: 0, at: 0 };

/** ETH price in USD from a 1 ETH -> USDC aggregator quote, cached a minute. */
async function ethPriceUsd(chainId) {
  if (Date.now() - ethUsd.at < 60000 && ethUsd.price > 0) return ethUsd.price;
  const usdc = USDC[chainId] || USDC[8453];
  const q = await quoteKyber(USDC[chainId] ? chainId : 8453, NATIVE_ETH, usdc, ethers.parseEther('1').toString(), evm.wallets[chainId].address, 100);
  ethUsd.price = Number(q.buyAmount) / 1e6; ethUsd.at = Date.now();
  return ethUsd.price;
}

/** Fully diluted valuation in USD implied by a quote of `spendWei` for `outRaw` tokens. */
async function impliedFdvUsd(chainId, spendWei, outRaw) {
  if (!launch.supplyRaw || launch.decimals == null || !outRaw || BigInt(outRaw) === 0n) return null;
  const tokens = Number(outRaw) / 10 ** launch.decimals;
  const priceEth = Number(ethers.formatEther(spendWei)) / tokens;
  const supply = Number(launch.supplyRaw) / 10 ** launch.decimals;
  return priceEth * supply * await ethPriceUsd(chainId);
}

async function resolveLaunchTarget() {
  const addr = CFG.targetContract;
  if (!addr) return;
  if (ethers.isAddress(addr)) {
    const chainId = await detectEvm(addr);
    if (!chainId) { log(`[launch] ${addr} is not a contract on any configured EVM chain yet; will keep checking`); launch.target = { chain: null, address: ethers.getAddress(addr) }; return; }
    launch.target = { chain: chainId, address: ethers.getAddress(addr) };
    try {
      const erc20 = new ethers.Contract(addr, ['function name() view returns (string)', 'function symbol() view returns (string)', 'function decimals() view returns (uint8)', 'function totalSupply() view returns (uint256)'], evm.providers[chainId]);
      const [name, symbol, dec, supply] = await Promise.all([erc20.name(), erc20.symbol(), erc20.decimals(), erc20.totalSupply()]);
      launch.supplyRaw = supply.toString(); launch.decimals = Number(dec);
      launch.info = `${name} (${symbol}), supply ${(Number(supply) / 10 ** Number(dec)).toLocaleString('en-US', { maximumFractionDigits: 0 })}`
        + (CFG.maxEntryFdvUsd > 0 ? `; entry cap FDV $${CFG.maxEntryFdvUsd.toLocaleString('en-US')}` : '; no entry cap');
    } catch (e) { launch.info = `token details unreadable (${String(e.message).slice(0, 80)})`; }
    log(`[launch] watching contract ${launch.target.address} on ${CHAIN_NAMES[chainId]}: ${launch.info}`);
  } else if (solEnabled() && await detectSolanaMint(addr)) {
    launch.target = { chain: 'sol', address: addr };
    log(`[launch] watching mint ${addr} on Solana`);
  } else {
    log(`[launch] SNIPER_TARGET_CONTRACT ${addr} is neither an EVM address nor a live Solana mint; ignoring it`);
  }
}

function launchWindowOpen() {
  if (!CFG.launchAt) return true;
  return Date.now() >= CFG.launchAt - CFG.launchLeadS * 1000;
}

/** True when the loop should run at the fast cadence. */
function launchFastMode() {
  return Boolean(launch.target && !state.bought && launchWindowOpen());
}

async function launchCheck() {
  if (!launch.target || state.bought) return;
  const every = launchWindowOpen() ? CFG.launchPollMs : CFG.targetIdlePollMs;
  if (Date.now() - launch.lastTry < every) return;
  launch.lastTry = Date.now();
  launch.attempts += 1;
  if (!launch.target.chain) {
    const chainId = await detectEvm(launch.target.address);
    if (!chainId) return;
    launch.target.chain = chainId;
    log(`[launch] contract ${launch.target.address} is now live on ${CHAIN_NAMES[chainId]}`);
  }
  const t = launch.target;
  let quote = null;
  try {
    // Direct router reads are cheap RPC calls and run every check; the aggregator APIs are
    // asked at most every 10 s so hours of fast polling cannot get this IP rate-limited.
    const aggregators = !DEX[t.chain] || Date.now() - launch.lastAggregatorAt >= 10000;
    if (aggregators) launch.lastAggregatorAt = Date.now();
    if (t.chain === 'sol') quote = await jupiterQuote(WSOL, t.address, await solSpendLamports(), CFG.slippageBps);
    else quote = await quoteEvm(t.chain, NATIVE_ETH, t.address, (await evmSpendWei(t.chain)).toString(), evm.wallets[t.chain].address, CFG.slippageBps, { aggregators });
    launch.routeErrors = 0;
  } catch (e) {
    launch.routeErrors += 1;
    if (Date.now() - launch.lastLog > 600000) {
      launch.lastLog = Date.now();
      log(`[launch] no route yet for ${t.address} (${launch.attempts} checks, every ${every / 1000}s): ${String(e.message).slice(0, 140)}`);
    }
    return;
  }
  const out = quote.buyAmount || quote.outAmount;
  let fdvNote = '';
  if (t.chain !== 'sol') {
    try {
      const fdv = await impliedFdvUsd(t.chain, quote.value ?? (await evmSpendWei(t.chain)), out);
      if (fdv != null) {
        fdvNote = `, implied FDV $${Math.round(fdv).toLocaleString('en-US')}`;
        if (CFG.maxEntryFdvUsd > 0 && fdv > CFG.maxEntryFdvUsd) {
          if (Date.now() - launch.lastCapLog > 30000) {
            launch.lastCapLog = Date.now();
            log(`[launch] route exists but implied FDV $${Math.round(fdv).toLocaleString('en-US')} is above the $${CFG.maxEntryFdvUsd.toLocaleString('en-US')} entry cap; waiting for a dip (checking every ${every / 1000}s)`);
          }
          return;
        }
      }
    } catch (e) { fdvNote = ` (FDV check failed: ${String(e.message).slice(0, 80)})`; }
  }
  await notify(`🚀 LAUNCH: ${t.address} on ${CHAIN_NAMES[t.chain]} has a route (${quote.via || 'jupiter'}, est. ${out} raw out${fdvNote}). Buying now${CFG.dryRun ? ' (DRY RUN)' : ''}.`);
  if (!CFG.dryRun && await alreadyHolds(t)) {
    state.bought = true; state.buy = { hash: 'already-held', address: t.address, chain: CHAIN_NAMES[t.chain], tweet: 'launch', at: new Date().toISOString() };
    saveState(state);
    if (!state.position) {
      const spentRaw = t.chain === 'sol' ? String(await solSpendLamports()) : (await evmSpendWei(t.chain)).toString();
      openPosition(t, { hash: 'already-held', spentRaw, spend: t.chain === 'sol' ? `${(Number(spentRaw) / LAMPORTS_PER_SOL).toFixed(4)} SOL` : `${Number(ethers.formatEther(spentRaw)).toFixed(5)} ETH` }, 'launch');
    }
    await notify(`Wallet already holds ${t.address}; not buying again.`);
    return;
  }
  try {
    const res = await buy(t);
    if (!CFG.dryRun) {
      state.bought = true; state.buy = { ...res, address: t.address, tweet: 'launch', at: new Date().toISOString() }; saveState(state);
      openPosition(t, res, 'launch');
    }
    await notify(`✅ ${CFG.dryRun ? 'DRY RUN ' : ''}bought ${t.address} on ${res.chain} for ${res.spend}: ${res.hash}`);
  } catch (e) {
    await notify(`❌ Launch buy failed for ${t.address}: ${String(e.message || e).slice(0, 300)}; retrying on the next check`);
  }
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
  if (CFG.targetContract && target.address.toLowerCase() !== CFG.targetContract.toLowerCase()) {
    await notify(`Post ${tweet.id} names ${target.address}, but the configured launch contract is ${CFG.targetContract}. Ignoring it; the launch watch buys only the configured contract.`);
    return true;
  }
  if (!CFG.dryRun && await alreadyHolds(target)) {
    state.bought = true; state.buy = { hash: 'already-held', address: target.address, chain: CHAIN_NAMES[target.chain], tweet: tweet.id, at: new Date().toISOString() };
    saveState(state);
    await notify(`Wallet already holds ${target.address} on ${CHAIN_NAMES[target.chain]}; treating it as bought and not buying again.`);
    if (!state.position) {
      // The spend is unknown here (bought outside this run); assume the rule would have applied.
      let spentRaw;
      try { spentRaw = target.chain === 'sol' ? String(await solSpendLamports()) : (await evmSpendWei(target.chain)).toString(); } catch { spentRaw = target.chain === 'sol' ? String(Math.floor(CFG.sol.spendSol * LAMPORTS_PER_SOL)) : ethers.parseEther(CFG.evm.spendEth).toString(); }
      const spend = target.chain === 'sol' ? `${(Number(spentRaw) / LAMPORTS_PER_SOL).toFixed(4)} SOL` : `${Number(ethers.formatEther(spentRaw)).toFixed(5)} ETH`;
      openPosition(target, { hash: 'already-held', spentRaw, spend }, tweet.id);
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
const stats = { polls: 0, ok: 0, errors: 0, posts: 0, grok: 0, lastGrokAt: 0, lastHeartbeat: Date.now() };
const HEARTBEAT_MS = Math.max(60000, num('SNIPER_HEARTBEAT_MS', 300000));

function heartbeat() {
  if (Date.now() - stats.lastHeartbeat < HEARTBEAT_MS) return;
  stats.lastHeartbeat = Date.now();
  const pos = state.position;
  const rungsDone = pos?.ladder ? pos.ladder.filter((r) => r.done).length : 0;
  const posNote = pos ? (pos.sold ? `; ${pos.address.slice(0, 10)} fully sold (${pos.sold.hash})`
    : `; holding ${pos.address.slice(0, 10)} on ${pos.chainName} at ${tp.multiple == null ? '?' : tp.multiple.toFixed(2) + 'x'} entry, ${rungsDone}/${pos.ladder ? pos.ladder.length : 0} rungs taken`) : '';
  const feed = CFG.syndication ? `free X feed ${syndicationHealthy() ? 'OK' : 'DOWN'} (${synd.ok} ok/${synd.errors} err, every ${Math.round((synd.intervalMs || CFG.pollMs) / 1000)}s), ` : '';
  log(`[heartbeat] ${stats.polls} polls, ${stats.ok} ok, ${stats.errors} errors, ${stats.posts} posts seen, ${Object.keys(state.pending).length} pending; `
    + `${feed}Grok calls ${stats.grok}; dryRun=${CFG.dryRun} bought=${state.bought}${posNote}`);
}

async function tick() {
  await runQueuedTest();
  try { await checkTakeProfit(); } catch (e) { log(`[tp] ${String(e.message || e).slice(0, 200)}`); }
  const tweets = [];
  stats.polls += 1;
  let freeOk = false;
  if (CFG.bearer) {
    try { tweets.push(...await xApiTweets()); stats.ok += 1; freeOk = true; } catch (e) { stats.errors += 1; log(`[x] ${e.message}`); }
  }
  if (CFG.syndication && !freeOk) {
    if (!syndicationDue()) {
      freeOk = syndicationHealthy();   // between allowed reads the feed still counts as watching
    } else {
      try { tweets.push(...await syndicationTweets()); stats.ok += 1; freeOk = true; } catch (e) {
        stats.errors += 1;
        if (synd.errors === 1 || synd.errors % 60 === 0) log(`[x-timeline] free feed refused (${e.message}); now asking every ${Math.round((synd.intervalMs || CFG.pollMs) / 1000)}s; Grok covers every ${CFG.grokIntervalMs / 1000}s while it is down`);
      }
    }
  }
  // Grok is a paid search per call: a backup sweep when a free feed works, the only eyes
  // otherwise, and never more often than its interval (plus any backoff from the API).
  const grokEvery = freeOk ? CFG.grokBackupMs : CFG.grokIntervalMs;
  if (CFG.xaiKey && Date.now() - stats.lastGrokAt >= grokEvery + backoffMs) {
    stats.lastGrokAt = Date.now();
    stats.grok += 1;
    try { tweets.push(...await grokTweets()); backoffMs = 0; stats.ok += 1; } catch (e) {
      stats.errors += 1;
      backoffMs = e.retryAfterMs || Math.min(600000, (backoffMs || 30000) * 2);
      log(`[grok] ${e.message} (next Grok try in ${((grokEvery + backoffMs) / 1000) | 0}s${freeOk ? '; the free X feed is still watching' : ''})`);
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
  const sources = [CFG.bearer && 'X API', CFG.syndication && 'free X feed',
    CFG.xaiKey && `Grok ${CFG.xaiModel} as backup every ${CFG.grokBackupMs / 1000}s (every ${CFG.grokIntervalMs / 1000}s if the free feed fails)`].filter(Boolean);
  log(`watching @${CFG.handle} every ${CFG.pollMs / 1000}s via ${sources.join(' + ')}; `
    + `dryRun=${CFG.dryRun} verify=${CFG.verify} routeWait=${CFG.routeWaitS}s slippage=${CFG.slippageBps}-${CFG.maxSlippageBps}bps`);
  await connectEvm();
  if (!evmEnabled() && !solEnabled()) { log('no wallet configured: set WALLET_PRIVATE_KEY (Solana) and/or SNIPER_EVM_PRIVATE_KEY (Base/Ethereum)'); process.exit(1); }
  if (evmEnabled()) {
    const addr = Object.values(evm.wallets)[0].address;
    const bals = await Promise.all(Object.entries(evm.providers).map(async ([id, p]) => { try { return `${CHAIN_NAMES[id]} ${ethers.formatEther(await p.getBalance(addr))} ETH`; } catch { return `${CHAIN_NAMES[id]} ?`; } }));
    const now = [];
    for (const id of Object.keys(evm.providers)) { try { now.push(`${CHAIN_NAMES[id]} ${Number(ethers.formatEther(await evmSpendWei(Number(id)))).toFixed(5)} ETH`); } catch (e) { now.push(`${CHAIN_NAMES[id]} none (${e.message})`); } }
    log(`EVM wallet ${addr}: ${bals.join(', ')}; spend rule ${spendRule()} = right now ${now.join(', ')}; router ${CFG.evm.zeroExKey ? '0x then KyberSwap' : 'KyberSwap'}; rpc ${Object.entries(evm.rpc).map(([id, u]) => `${CHAIN_NAMES[id]}=${redact(u).replace(/\/v2\/.*/, '/v2/***')}`).join(' ')}`);
  } else log('EVM buying off (needs SNIPER_EVM_PRIVATE_KEY: a burner Base wallet private key)');
  if (solEnabled()) {
    let bal = '?';
    try { bal = ((await sol.connection.getBalance(sol.keypair.publicKey)) / LAMPORTS_PER_SOL).toFixed(4); } catch { /* keep ? */ }
    let now = '?';
    try { now = `${((await solSpendLamports()) / LAMPORTS_PER_SOL).toFixed(4)} SOL`; } catch (e) { now = `none (${e.message})`; }
    log(`Solana wallet ${sol.keypair.publicKey.toBase58()}: ${bal} SOL; spend rule ${spendRule()} = right now ${now}`);
  } else log('Solana buying off (needs WALLET_PRIVATE_KEY)');
  if (state.bought) log(`already bought once: ${JSON.stringify(state.buy)} — delete ${CFG.stateFile} to arm again`);
  if (!state.position && evmEnabled()) {
    try { await recoverEvmPosition(); } catch (e) { log(`[tp] position recovery failed: ${String(e.message).slice(0, 160)}`); }
  }
  if (state.position && !state.position.sold) {
    // The ladder in the environment wins over the one saved with the position, so the
    // owner can retune rungs after the buy; rungs already taken stay taken.
    const done = new Map((state.position.ladder || []).filter((r) => r.done).map((r) => [String(r.x), r.done]));
    const fresh = TAKE_PROFIT_LADDER.map((r) => ({ ...r, done: done.get(String(r.x)) || null }));
    const changed = JSON.stringify(fresh.map((r) => [r.x, r.sell])) !== JSON.stringify((state.position.ladder || []).map((r) => [r.x, r.sell]));
    if (changed) { state.position.ladder = fresh; saveState(state); log('[tp] ladder updated from SNIPER_TAKE_PROFIT_LADDER for the open position'); }
    log(`[tp] resuming watch on ${state.position.address} (${state.position.chainName}): ${describeLadder(state.position.ladder)}`);
  }
  log(`take profit ladder: ${describeLadder(TAKE_PROFIT_LADDER)}; checked every ${TP_CHECK_MS / 1000}s; POST /api/sniper/sell sells everything left`);
  if (CFG.dryRun) log('DRY RUN: nothing will be sent. Set SNIPER_DRY_RUN=false to go live.');
  if (CFG.syndication) {
    try {
      const posts = await syndicationTweets();
      const latest = posts[0];
      log(latest ? `[probe] free X feed works: latest post ${latest.id} (${((Date.now() - (tweetTimeMs(latest) ?? Date.now())) / 60000).toFixed(0)} min old): ${latest.text.replace(/\s+/g, ' ').slice(0, 140)}`
        : '[probe] free X feed answered but listed no posts for this account');
    } catch (e) { log(`[probe] free X feed unavailable (${e.message}); Grok will be the only eyes, every ${CFG.grokIntervalMs / 1000}s`); }
  }
  if (CFG.xaiKey) {
    try { await grokProbe(); } catch (e) { log(`[probe] Grok read failed: ${e.message}`); }
  }
  if (env('SNIPER_TEST_TEXT').trim()) await runTest(env('SNIPER_TEST_TEXT'), 'SNIPER_TEST_TEXT');
  await resolveLaunchTarget();
  if (launch.target) {
    log(`[launch] cadence: every ${CFG.launchPollMs / 1000}s${CFG.launchAt ? ` from ${new Date(CFG.launchAt - CFG.launchLeadS * 1000).toISOString()} (launch ${new Date(CFG.launchAt).toISOString()}), every ${CFG.targetIdlePollMs / 1000}s before that` : ' from now'}; posts naming any other address are ignored`);
  }
  let lastTweetPoll = 0;
  for (;;) {
    try {
      await launchCheck();
      if (Date.now() - lastTweetPoll >= CFG.pollMs) { lastTweetPoll = Date.now(); await tick(); }
    } catch (e) { log(`[tick] ${e.message}`); }
    await sleep(launchFastMode() ? CFG.launchPollMs : CFG.pollMs);
  }
}

main().catch((e) => { log(`fatal: ${e.stack || e}`); process.exit(1); });
