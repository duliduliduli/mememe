# Launch sniper

Watches one X account for a token contract address and buys it exactly once, on
whichever chain the address is live on: Base or Ethereum through the 0x Swap API,
Solana through Jupiter. `server.py` starts it beside the other lanes as soon as a
post source key is present, so on Railway it needs no separate service.

## How a post becomes a buy

1. **Find posts.** With `TWITTER_BEARER_TOKEN` the lane polls X API v2 recent search
   (`from:<handle>`), which is fast and exact. With `XAI_API_KEY` it asks Grok through the
   Responses API with the `x_search` tool restricted to the handle and told to return the
   posts as verbatim JSON. Grok is a model reading X, not a data feed, so its output is
   never trusted on its own.
2. **Verify.** Every Grok-sourced post is fetched from X's public embed endpoint
   (`publish.x.com/oembed`), which returns the post's real text and links; t.co links are
   expanded so an address inside a pump.fun or Dexscreener URL still counts. If the embed
   is unreachable the lane asks Grok a second, independent time to quote that exact post
   and requires both reads to name the same address. Neither works: no buy, unless
   `SNIPER_TRUST_UNVERIFIED=1`.
3. **Resolve the chain.** Each candidate is checked on-chain: `getCode` on Base and
   Ethereum for 0x addresses, `getAccountInfo` owner is the SPL Token program for base58
   ones. Only a live contract or mint is bought. A post whose address is not live yet is
   retried every poll for `SNIPER_ROUTE_WAIT_S`.
4. **Buy.** A market buy for the fixed spend cap (`SNIPER_SPEND_SOL` / `SNIPER_SPEND_ETH`).
   "No route yet" is retried for `SNIPER_ROUTE_WAIT_S`; slippage failures step the
   tolerance up by 5% at a time to `SNIPER_MAX_SLIPPAGE_BPS`. The first confirmed buy
   writes `bought: true` to the state file and the lane never buys again until that file
   is deleted, so a hacked account posting ten addresses costs one spend cap, not ten.

Posts older than `SNIPER_MAX_POST_AGE_S` when first seen are ignored, so a restart never
buys yesterday's address. `SNIPER_DRY_RUN` is `true` until it is set to exactly `false`;
dry runs go through the whole pipeline including quotes and log what they would send.

## Variables

See `.env.example` (section "Launch sniper lane"). Minimum to go live on Solana with the
wallet this service already uses: `XAI_API_KEY`, `SNIPER_DRY_RUN=false`. Add
`SNIPER_EVM_PRIVATE_KEY` (a burner) and `ZEROEX_API_KEY` for Base and Ethereum.

## Endpoints and logs

- `GET /api/sniper/status`: running, source, dry_run, bought, last buy, posts seen.
- `POST /api/sniper/start`, `POST /api/sniper/stop` (admin token).
- Lines are prefixed `SNIPE` in the dashboard log viewer (`/api/runtime-logs`) and in
  `$DATA_DIR/sniper.log`. State lives in `$DATA_DIR/sniper-state.json`.

## Tests

`node --test lib.test.mjs` covers the parsing that the money path relies on: address
extraction (EVM and base58 Solana), the model's JSON, Responses API citations, embed HTML,
and post timestamps. `python -m unittest test_sniper` runs those plus the server wiring.
