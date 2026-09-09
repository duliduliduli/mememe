// Pure helpers for the tweet sniper. No network, no secrets, no dependencies, so
// `node --test` covers every parsing decision the money path relies on.

export const EVM_RE = /0x[a-fA-F0-9]{40}(?![a-fA-F0-9])/g;
// Base58, 32-44 chars: Solana mints are 32-byte keys. Anything shorter or longer is not one.
export const SOL_RE = /(?<![1-9A-HJ-NP-Za-km-z])[1-9A-HJ-NP-Za-km-z]{32,44}(?![1-9A-HJ-NP-Za-km-z])/g;
const STATUS_RE = /(?:https?:\/\/)?(?:www\.)?(?:x|twitter)\.com\/([A-Za-z0-9_]{1,15})\/status(?:es)?\/(\d{6,25})/g;

const B58 = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz';

/** Decode base58 to bytes; null when the string is not valid base58. */
export function base58Decode(str) {
  let n = 0n;
  for (const ch of str) {
    const v = B58.indexOf(ch);
    if (v < 0) return null;
    n = n * 58n + BigInt(v);
  }
  const bytes = [];
  while (n > 0n) { bytes.push(Number(n & 255n)); n >>= 8n; }
  for (const ch of str) { if (ch === '1') bytes.push(0); else break; }
  return Uint8Array.from(bytes.reverse());
}

/** Unique EVM addresses (lowercased, order of first appearance). */
export function extractEvmAddresses(text) {
  const out = [];
  for (const m of String(text || '').match(EVM_RE) || []) {
    const a = m.toLowerCase();
    if (!out.includes(a)) out.push(a);
  }
  return out;
}

/** Unique Solana public keys (32 bytes when decoded). EVM addresses are stripped first so
 *  a zero-free hex run never masquerades as base58; plain words never decode to 32 bytes. */
export function extractSolanaAddresses(text) {
  const cleaned = String(text || '').replace(EVM_RE, ' ');
  const out = [];
  for (const m of cleaned.match(SOL_RE) || []) {
    const bytes = base58Decode(m);
    if (bytes && bytes.length === 32 && !out.includes(m)) out.push(m);
  }
  return out;
}

/** {handle, id} for every X status URL found in the text (handles lowercased). */
export function statusRefs(text) {
  const refs = [];
  for (const m of String(text || '').matchAll(STATUS_RE)) {
    const ref = { handle: m[1].toLowerCase(), id: m[2] };
    if (!refs.some((r) => r.id === ref.id)) refs.push(ref);
  }
  return refs;
}

/** Parse the JSON array the model was told to return. Tolerates code fences and prose
 *  around the array; anything unparseable yields []. Rows without an id get one from
 *  their url. Only rows with a numeric id survive. */
export function parseTweetsJson(text) {
  const raw = String(text || '');
  let body = raw.replace(/```(?:json)?/gi, '').trim();
  const start = body.indexOf('[');
  const end = body.lastIndexOf(']');
  if (start < 0 || end <= start) return [];
  body = body.slice(start, end + 1);
  let rows;
  try { rows = JSON.parse(body); } catch { return []; }
  if (!Array.isArray(rows)) return [];
  const out = [];
  for (const r of rows) {
    if (!r || typeof r !== 'object') continue;
    let id = r.id != null ? String(r.id).replace(/\D/g, '') : '';
    const url = typeof r.url === 'string' ? r.url : '';
    if (!id && url) id = (statusRefs(url)[0] || {}).id || '';
    if (!/^\d{6,25}$/.test(id)) continue;
    if (out.some((t) => t.id === id)) continue;
    out.push({
      id,
      url: url || '',
      text: typeof r.text === 'string' ? r.text : '',
      created_at: typeof r.created_at === 'string' ? r.created_at : '',
      handle: typeof r.handle === 'string' ? r.handle.replace(/^@/, '').toLowerCase() : (statusRefs(url)[0] || {}).handle || '',
    });
  }
  return out;
}

/** Every output_text block and citation URL from a Responses API result, whatever the
 *  exact nesting: the shapes differ between output kinds and have shifted between releases. */
export function collectResponseText(resp) {
  const texts = [];
  const urls = new Set();
  const visit = (node, depth) => {
    if (!node || depth > 12) return;
    if (Array.isArray(node)) { node.forEach((n) => visit(n, depth + 1)); return; }
    if (typeof node !== 'object') return;
    if (typeof node.text === 'string' && (node.type === 'output_text' || node.type === 'text' || node.type == null)) texts.push(node.text);
    if (typeof node.output_text === 'string') texts.push(node.output_text);
    if (typeof node.url === 'string' && node.url.startsWith('http')) urls.add(node.url);
    for (const key of ['output', 'content', 'annotations', 'citations', 'message', 'choices', 'delta']) {
      if (node[key] != null) visit(node[key], depth + 1);
    }
  };
  visit(resp, 0);
  if (Array.isArray(resp?.citations)) resp.citations.forEach((c) => { if (typeof c === 'string') urls.add(c); });
  return { text: texts.join('\n'), urls: [...urls] };
}

const ENTITIES = { amp: '&', lt: '<', gt: '>', quot: '"', apos: "'", '#39': "'", nbsp: ' ' };

/** Visible text and hrefs from the oEmbed HTML. `<br>` and `</p>` become newlines. */
export function oembedText(html) {
  const raw = String(html || '');
  const hrefs = [...raw.matchAll(/href="([^"]+)"/g)].map((m) => decodeEntities(m[1]));
  const text = decodeEntities(raw
    .replace(/<script[\s\S]*?<\/script>/gi, ' ')
    .replace(/<br\s*\/?>/gi, '\n').replace(/<\/p>/gi, '\n')
    .replace(/<[^>]+>/g, ' ')).replace(/[ \t]+/g, ' ').trim();
  return { text, hrefs };
}

export function decodeEntities(s) {
  return String(s).replace(/&(#x[0-9a-f]+|#\d+|[a-z]+);/gi, (m, e) => {
    if (e[0] === '#') {
      const code = e[1].toLowerCase() === 'x' ? parseInt(e.slice(2), 16) : parseInt(e.slice(1), 10);
      return Number.isFinite(code) ? String.fromCodePoint(code) : m;
    }
    return ENTITIES[e.toLowerCase()] ?? m;
  });
}

/** Posts from X's public syndication timeline (the HTML behind embedded profile widgets).
 *  The page carries a __NEXT_DATA__ JSON blob; every object in it with id_str + full_text
 *  is a post. Retweets are skipped; only posts by `handle` (when given) are returned, with
 *  expanded URLs appended to the text so an address inside a link still counts. */
export function parseSyndicationTimeline(html, handle = '') {
  const raw = String(html || '');
  const want = String(handle || '').replace(/^@/, '').toLowerCase();
  let json = null;
  const m = raw.match(/<script[^>]*id="__NEXT_DATA__"[^>]*>([\s\S]*?)<\/script>/i);
  const body = m ? m[1] : raw;
  try { json = JSON.parse(body); } catch { return []; }
  const out = [];
  const seen = new Set();
  const visit = (node, depth) => {
    if (!node || depth > 20) return;
    if (Array.isArray(node)) { node.forEach((n) => visit(n, depth + 1)); return; }
    if (typeof node !== 'object') return;
    if (typeof node.id_str === 'string' && typeof node.full_text === 'string') {
      const author = String(node.user?.screen_name || '').toLowerCase();
      const isRetweet = Boolean(node.retweeted_status) || /^RT @/.test(node.full_text);
      if (!seen.has(node.id_str) && !isRetweet && (!want || author === want)) {
        seen.add(node.id_str);
        const urls = (node.entities?.urls || []).map((u) => u.expanded_url || u.unwound_url || '').filter(Boolean);
        out.push({
          id: node.id_str,
          url: `https://x.com/${author || want}/status/${node.id_str}`,
          text: [node.full_text, ...urls].join(' '),
          created_at: node.created_at ? new Date(node.created_at).toISOString() : '',
          handle: author || want,
        });
      }
    }
    for (const v of Object.values(node)) if (v && typeof v === 'object') visit(v, depth + 1);
  };
  visit(json, 0);
  return out.sort((a, b) => (a.id.length === b.id.length ? (a.id < b.id ? 1 : -1) : b.id.length - a.id.length));
}

/** Token X's tweet-result endpoint requires alongside a status id (the same derivation
 *  the official embed code uses). */
export function syndicationToken(id) {
  return ((Number(id) / 1e15) * Math.PI).toString(36).replace(/(0+|\.)/g, '');
}

/** A post from the tweet-result JSON (cdn.syndication.twimg.com/tweet-result). */
export function parseTweetResult(json, handle = '') {
  if (!json || typeof json !== 'object') return null;
  const id = String(json.id_str || json.id || '').replace(/\D/g, '');
  if (!id) return null;
  const author = String(json.user?.screen_name || '').toLowerCase();
  const want = String(handle || '').replace(/^@/, '').toLowerCase();
  if (want && author && author !== want) return null;
  const urls = (json.entities?.urls || []).map((u) => u.expanded_url || u.unwound_url || '').filter(Boolean);
  const media = (json.mediaDetails || json.entities?.media || []).map((u) => u.expanded_url || '').filter(Boolean);
  return {
    id, url: `https://x.com/${author || want}/status/${id}`, text: [json.text || json.full_text || '', ...urls, ...media].join(' '),
    created_at: json.created_at ? new Date(json.created_at).toISOString() : '', handle: author || want,
  };
}

/** Candidate contract addresses in a blob, EVM first then Solana, each unique. */
export function extractCandidates(blob) {
  return {
    evm: extractEvmAddresses(blob),
    solana: extractSolanaAddresses(blob),
  };
}

/** Tweet time from an ISO string or, failing that, from the snowflake id (ms since epoch
 *  = id >> 22 + 1288834974657). Returns ms or null. */
export function tweetTimeMs(tweet) {
  if (tweet?.created_at) {
    const t = Date.parse(tweet.created_at);
    if (Number.isFinite(t)) return t;
  }
  if (tweet?.id && /^\d+$/.test(tweet.id)) {
    try { return Number((BigInt(tweet.id) >> 22n) + 1288834974657n); } catch { /* fallthrough */ }
  }
  return null;
}
