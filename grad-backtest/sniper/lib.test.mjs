import test from 'node:test';
import assert from 'node:assert/strict';
import {
  base58Decode, collectResponseText, extractCandidates, extractEvmAddresses, extractSolanaAddresses,
  oembedText, parseSyndicationTimeline, parseTweetResult, parseTweetsJson, statusRefs, syndicationToken, tweetTimeMs,
} from './lib.mjs';

const EVM = '0x4200000000000000000000000000000000000006';
const MINT = 'DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263'; // BONK

test('extracts EVM addresses once, case-insensitively', () => {
  const found = extractEvmAddresses(`CA: ${EVM} again ${EVM.toUpperCase().replace('0X', '0x')} and not 0x1234`);
  assert.deepEqual(found, [EVM.toLowerCase()]);
});

test('extracts Solana mints and ignores words, hex runs and short strings', () => {
  const text = `pump.fun/coin/${MINT} ${EVM} Somethingveryverylongbutnotbase58atall0 abcdefghijklmnopqrstuvwxyzABCDEFGH`;
  assert.deepEqual(extractSolanaAddresses(text), [MINT]);
  assert.equal(base58Decode(MINT).length, 32);
  assert.equal(base58Decode('0OIl'), null);
});

test('status refs come from x.com and twitter.com urls', () => {
  const refs = statusRefs('see https://x.com/HunterBiden/status/1234567890123 and twitter.com/other/status/99999999');
  assert.deepEqual(refs, [{ handle: 'hunterbiden', id: '1234567890123' }, { handle: 'other', id: '99999999' }]);
});

test('parses the model JSON with fences and prose, derives ids from urls', () => {
  const text = 'Here you go:\n```json\n[{"url":"https://x.com/hunterbiden/status/1111111111","text":"gm ' + MINT + '","created_at":"2026-09-07T10:00:00Z"},{"id":"not a post"}]\n```';
  const rows = parseTweetsJson(text);
  assert.equal(rows.length, 1);
  assert.equal(rows[0].id, '1111111111');
  assert.equal(rows[0].handle, 'hunterbiden');
  assert.deepEqual(extractCandidates(rows[0].text).solana, [MINT]);
  assert.deepEqual(parseTweetsJson('[]'), []);
  assert.deepEqual(parseTweetsJson('no json here'), []);
});

test('collects text and citation urls from a Responses API payload', () => {
  const resp = {
    output: [
      { type: 'x_search_call', status: 'completed' },
      { type: 'message', content: [{ type: 'output_text', text: '[{"id":"22"}]', annotations: [{ type: 'url_citation', url: 'https://x.com/hunterbiden/status/22' }] }] },
    ],
    citations: ['https://x.com/hunterbiden/status/23'],
  };
  const { text, urls } = collectResponseText(resp);
  assert.equal(text, '[{"id":"22"}]');
  assert.deepEqual(urls.sort(), ['https://x.com/hunterbiden/status/22', 'https://x.com/hunterbiden/status/23']);
});

test('oembed html yields visible text and hrefs', () => {
  const html = `<blockquote class="twitter-tweet"><p lang="en" dir="ltr">CA: ${EVM}<br>buy here <a href="https://t.co/abc">https://t.co/abc</a> &amp; go</p>&mdash; Hunter (@hunterbiden) <a href="https://twitter.com/hunterbiden/status/5?ref_src=x">September 7, 2026</a></blockquote>`;
  const { text, hrefs } = oembedText(html);
  assert.match(text, /CA: 0x4200/);
  assert.match(text, /& go/);
  assert.deepEqual(hrefs, ['https://t.co/abc', 'https://twitter.com/hunterbiden/status/5?ref_src=x']);
});

test('parses the syndication timeline, skipping retweets and other authors', () => {
  const data = {
    props: { pageProps: { timeline: { entries: [
      { content: { tweet: { id_str: '300', full_text: 'CA below', created_at: 'Tue Sep 09 12:00:00 +0000 2026', user: { screen_name: 'HunterBiden' },
        entities: { urls: [{ url: 'https://t.co/x', expanded_url: `https://basescan.org/token/${EVM}` }] } } } },
      { content: { tweet: { id_str: '299', full_text: 'RT @someone: not mine', user: { screen_name: 'hunterbiden' }, retweeted_status: {} } } },
      { content: { tweet: { id_str: '298', full_text: 'someone else', user: { screen_name: 'other' } } } },
    ] } } },
  };
  const html = `<html><script id="__NEXT_DATA__" type="application/json">${JSON.stringify(data)}</script></html>`;
  const posts = parseSyndicationTimeline(html, 'hunterbiden');
  assert.equal(posts.length, 1);
  assert.equal(posts[0].id, '300');
  assert.equal(posts[0].handle, 'hunterbiden');
  assert.deepEqual(extractCandidates(posts[0].text).evm, [EVM.toLowerCase()]);
  assert.equal(posts[0].created_at, '2026-09-09T12:00:00.000Z');
  assert.deepEqual(parseSyndicationTimeline('<html>nothing</html>', 'hunterbiden'), []);
});

test('tweet-result json and its token', () => {
  const post = parseTweetResult({ id_str: '1964000000000000000', text: 'gm', user: { screen_name: 'hunterbiden' }, entities: { urls: [{ expanded_url: 'https://pump.fun/coin/' + MINT }] } }, 'hunterbiden');
  assert.equal(post.id, '1964000000000000000');
  assert.deepEqual(extractCandidates(post.text).solana, [MINT]);
  assert.equal(parseTweetResult({ id_str: '1', text: 'x', user: { screen_name: 'other' } }, 'hunterbiden'), null);
  assert.match(syndicationToken('1964000000000000000'), /^[0-9a-z]+$/);
});

test('tweet time falls back to the snowflake id', () => {
  assert.equal(tweetTimeMs({ created_at: '2026-09-07T10:00:00Z' }), Date.parse('2026-09-07T10:00:00Z'));
  const ms = tweetTimeMs({ id: '1964000000000000000' });
  assert.ok(ms > Date.parse('2025-01-01') && ms < Date.parse('2027-01-01'));
  assert.equal(tweetTimeMs({}), null);
});
