# Pump.fun Graduation Bot — complete reference

A research-and-execution system for one trade: buy a Pump.fun token shortly
after it graduates (migrates from the bonding curve to a PumpSwap or Raydium
pool), then exit on take-profit, stop-loss, trailing stop, or time stop.

It has three parts that share one data directory:

| Part | Files | What it does |
|---|---|---|
| Research pipeline | `grad_backtest.py`, `optimize.py`, `position_sizing.py` | Collects historical graduations, fetches price paths, simulates the strategy, sweeps parameters honestly, sizes positions from the results. |
| Executor | `executor.py` | Detects graduations live, applies entry guards, buys via Jupiter, manages exits, moon bags, stuck positions, wallet reconciliation, rent reclaim. Paper or live. |
| Dashboard + API | `server.py`, `static/index.html` | Public read-only web page over the result files; token-gated endpoints to launch jobs and start/stop/panic the executor. |

This is research software. The executor moves real money when told to; nothing
here is investment advice, and the backtest exists to tell you whether the
strategy has an edge before you find out with a wallet.

---

## Table of contents

1. [Architecture and data flow](#1-architecture-and-data-flow)
2. [Repository layout](#2-repository-layout)
3. [Quick start](#3-quick-start)
4. [Configuration reference (every environment variable)](#4-configuration-reference)
5. [The strategy](#5-the-strategy)
6. [Executor in depth](#6-executor-in-depth)
   - 6.1 Startup sequence
   - 6.2 Main loop
   - 6.3 Detection
   - 6.4 Entry pipeline and guard order
   - 6.5 Position management and exit order
   - 6.6 Scale-out
   - 6.7 Moon bags
   - 6.8 Stuck positions
   - 6.9 Wallet reconciliation and rent reclaim
   - 6.10 Rate limits and transaction retries
   - 6.11 Control: start, stop, panic
   - 6.12 State file schema
   - 6.13 Files written and the trade CSV
   - 6.14 Log line reference
7. [Dashboard and HTTP API](#7-dashboard-and-http-api)
8. [Research pipeline](#8-research-pipeline)
   - 8.1 `collect`
   - 8.2 `run` and the simulation rules
   - 8.3 Outputs
   - 8.4 `optimize.py`
   - 8.5 `position_sizing.py`
   - 8.6 Market-making research track (`mm/`)
9. [Deployment](#9-deployment)
10. [Tests](#10-tests)
11. [Function-by-function reference](#11-function-by-function-reference)
12. [Behavioural notes, limitations, and lessons from live trading](#12-behavioural-notes-limitations-and-lessons-from-live-trading)
13. [Security](#13-security)

---

## 1. Architecture and data flow

```
            Helius enhanced API       Standard RPC + WebSocket
             (research collector)       (live executor)
                    │                         │
   grad_backtest.py collect         executor.py poll_graduations
          │                                │
   graduations.csv                  pending graduations (in memory)
          │                                │  +ENTRY_DELAY_SECONDS
   grad_backtest.py run             entry guards → Jupiter buy → position
   (GeckoTerminal pools + OHLCV)           │
          │                         manage_positions every POLL_SECONDS
   cache/*.json, trade_results.csv,        │  (Jupiter sell quotes)
   summary.json, errors.csv …       exits → live_trades.csv, executor_state.json
          │                                │
   optimize.py / position_sizing.py        │
          │                                │
          └──────────── DATA_DIR ──────────┘
                          │
                   server.py (FastAPI)
                          │
                 static/index.html (dashboard)
```

Everything reads and writes under `DATA_DIR` (default `data`, `/data` in the
container). On Railway that directory must be a mounted volume or every deploy
starts empty; see [Deployment](#9-deployment).

External services:

| Service | Used by | For |
|---|---|---|
| Helius enhanced transactions | collector only; optional executor compatibility mode | Building the historical graduation dataset; optional normalized history when `TRANSACTION_HISTORY_MODE=helius` |
| Standard Solana JSON-RPC + WebSocket (`RPC_URL(S)`) | executor | Streaming/catching up graduations, raw transaction history, balances, holder data, sending, and confirmation |
| Jupiter swap API (`JUPITER_BASE_URL`, default lite-api v1) | executor | Quotes (buy, sell, valuation, SOL price) and swap transactions |
| GeckoTerminal (`api.geckoterminal.com/api/v2`) | backtester | Pool discovery and OHLCV candles |

---

## 2. Repository layout

```
mememe/
├── Dockerfile                  root-level image so Railway builds with no Root Directory setting
├── railway.json                Dockerfile builder, ON_FAILURE restart (5 retries), /healthz healthcheck
├── .dockerignore
├── README.md                   short pointer to this file
├── .github/workflows/
│   ├── tests.yml               unit tests on push/PR touching grad-backtest/
│   └── backtest.yml            manual "Run backtest in the cloud" workflow with cached OHLCV
└── grad-backtest/
    ├── executor.py             live/paper executor (section 6)
    ├── server.py               FastAPI dashboard + control API (section 7)
    ├── static/index.html       dashboard page
    ├── grad_backtest.py        collector + backtester (section 8)
    ├── optimize.py             parameter sweep with train/validation split
    ├── position_sizing.py      bootstrap Monte Carlo sizing
    ├── mm/                     market-making research track: screener, recorder, replay, paper (8.6)
    ├── requirements.txt        requests, websocket-client, pandas, fastapi, uvicorn, httpx, solders
    ├── Dockerfile, railway.json   same as root, for builds rooted here
    ├── .env.example            every variable with a comment
    ├── data/graduations.example.csv
    └── test_*.py               153 unit tests (section 10)
```

---

## 3. Quick start

### Local, research only

```bash
cd grad-backtest
python3 -m venv .venv && source .venv/bin/activate
python -m pip install -r requirements.txt
export HELIUS_API_KEY='…' MIGRATION_ADDRESS='…'
python grad_backtest.py collect --count 200
python grad_backtest.py run --limit 5      # dress rehearsal
python grad_backtest.py run                # full sample
python optimize.py                         # parameter sweep from the cache
python position_sizing.py --balance 100    # sizing from trade_results.csv
python server.py                           # dashboard on :8000
```

### Paper trading

```bash
export EXECUTOR_MODE=paper MIGRATION_ADDRESS='…' RPC_URL='https://your-provider.example/key'
python executor.py
```

Real detection, real Jupiter quotes, simulated fills against a
`START_BALANCE` paper balance. No wallet involved.

### Live trading

```bash
export EXECUTOR_MODE=live MIGRATION_ADDRESS='…' RPC_URL='https://your-provider.example/key'
export WALLET_PRIVATE_KEY='base58 key of a burner wallet'
python executor.py
```

Or set the same variables on Railway with `EXECUTOR_AUTOSTART=1` and the
dashboard container launches the executor on boot. Read
[Security](#13-security) before doing this.

---

## 4. Configuration reference

Every variable, its default, and what reads it. A variable that is not set
uses its default; you only add one to change it. `executor.py --print-config`
prints the values in effect (minus the key). The executor's first log line also
prints the important ones.

### 4.1 Credentials and endpoints

| Variable | Default | Read by | Meaning |
|---|---|---|---|
| `HELIUS_API_KEY` | (none) | collector; optional enhanced history | Required by `grad_backtest.py collect` or `TRANSACTION_HISTORY_MODE=helius`. Does not select the executor RPC endpoint. |
| `MIGRATION_ADDRESS` | (none, required) | collector, executor | Pump.fun's migration authority. Public, not a secret; deliberately not hard-coded because it has changed over time. |
| `RPC_URL` | PublicNode + Solana public mainnet | executor | Optional explicit standard Solana JSON-RPC endpoint. With no RPC variables, uses `https://solana-rpc.publicnode.com` then `https://api.mainnet.solana.com`, without API keys. |
| `RPC_URLS` | `RPC_URL` | executor | Comma/newline-separated standard RPC endpoints. Calls automatically fail over on rate limits, timeouts, connection failures, 5xx responses, or unhealthy-node errors. |
| `RPC_WS_URL`, `RPC_WS_URLS` | derived from RPC HTTPS URL(s) | executor | Optional provider WebSocket endpoint(s), in the same order as `RPC_URLS`. |
| `TRANSACTION_HISTORY_MODE` | `raw` | executor | `raw` reconstructs the bundle detector's inputs from standard RPC. `helius` retains the enhanced-history compatibility path and requires `HELIUS_API_KEY`. |
| `RPC_DAS_ENABLED` | `0` | executor | Enable only for a provider implementing Metaplex DAS `getTokenAccounts`; otherwise holder analysis uses portable standard RPC top-20 data. |
| `JUPITER_BASE_URL` | `https://lite-api.jup.ag/swap/v1` | executor | Jupiter quote/swap base. |
| `WALLET_PRIVATE_KEY` | (none; required in live mode) | executor | Base58 private key of a burner wallet. Never a seed phrase, never a main wallet. |
| `ADMIN_TOKEN` | (none) | server | Password for `POST /api/run` and `POST /api/executor/*`, sent as header `x-admin-token`. Unset means those endpoints return 503. |
| `LOG_VIEWER_TOKEN` | `ADMIN_TOKEN` | server | Optional separate password for the protected phone log viewer. HTTP Basic username is `admin`; the password never appears in the URL. |
| `LOG_VIEWER_MAX_BYTES` | `12000000` | server | Maximum tail of `executor.log` loaded by one viewer request (clamped to 1–50 MB). |
| `DATA_DIR` | `data` (`/data` in the container) | everything | Where all files live. |
| `PORT` | `8000` | server | Listen port. |
| `GECKO_REQUESTS_PER_MINUTE` | `9` | backtester | GeckoTerminal pacing for the keyless API. |

### 4.2 Executor mode and sizing

| Variable | Default | Meaning |
|---|---|---|
| `EXECUTOR_MODE` | `paper` | `paper` or `live`. |
| `EXECUTOR_AUTOSTART` | `0` | `1` makes the dashboard container launch the executor at boot. |
| `/healthz` | | Returns the deployed commit, the raw values of every variable that decides what the copy lane does (slots, sizing, minimums, first-buy rule, sell scope, GMGN gate, exits, `HOLD_MINTS`, `PANIC`), the followed wallets as prefixes and whether the GMGN key is set. No secrets. The first place to look when the bot skipped something: a value shown here is an override of the code default. |
| `GMGN_API_KEY` | (empty) | Read-only GMGN OpenAPI key (create one at gmgn.ai/ai with Enable Reading only). At startup the executor screens every `COPY_WALLETS` entry through GMGN and logs its 30-day realized profit, win rate, trade counts and tags with a copy / skip / thin verdict. The dashboard gains `GET /api/gmgn/screen?wallets=a,b` (same verdicts on demand) and `GET /api/gmgn/traders?token=<mint>&tag=smart_degen` (the wallets that made money on a coin, to pick new ones to copy). No private key is needed or used. |
| `HOLD_MINTS` | (empty) | Comma-separated mints the executor never touches: not adopted at startup, not swept, not sold by panic or a followed wallet's sell, never entered. For tokens bought by hand in the bot's wallet and kept on purpose. |
| `PANIC` | `0` | `1` sells every position and moon bag at market as soon as the executor starts (leftovers the reconcile adopts included), then drains with no new buys. The variable-only way to "sell everything and stop"; delete it once the log says `panic complete`, then set `EXECUTOR_AUTOSTART=0` to keep the bot off. |
| `START_BALANCE` | `100` | Initial paper balance for a fresh state file; also the dashboard equity-curve start. |
| `ACCOUNT_FRACTION` | `0.10` | Fraction of equity per position. Equity in live mode is spendable SOL (balance minus `MIN_SOL_RESERVE`) times the SOL price. |
| `MAX_POSITION_USD` | `20` | Hard cap on position size. |
| `MIN_POSITION_USD` | `5` | If the computed size is below this, no entry. Lower it if you want small fractional sizing on a small account. |
| `MAX_CONCURRENT_POSITIONS` | `5` | Maximum bot-opened positions at once. Adopted holdings do not count. |
| `DAILY_LOSS_LIMIT_USD` | `0` | Once realized P&L for the UTC day is at or below minus this, no new entries until the next UTC day. Measured from quoted closes and reset by a restart. `0` (the default) never pauses: a copy book of small clips takes strings of losses before a runner pays, so set this only as a circuit breaker well above a normal bad day. |
| `MIN_SOL_RESERVE` | `0.05` | SOL kept back for fees when computing live equity. |

### 4.3 Strategy parameters

| Variable | Default | Meaning |
|---|---|---|
| `TAKE_PROFIT` | `0.75` | Exit when the sell-quote value reaches basis × (1 + TP). |
| `STOP_LOSS` | `0.30` | Exit when the value falls to basis × (1 − SL). |
| `TRAILING_STOP` | `0` | Exit when the value falls this fraction from its post-entry peak. `0` disables. |
| `TIME_STOP_MINUTES` | `30` | Exit at this age regardless of P&L. |
| `SCALE_OUT_AT` | `0` | Gain at which a partial sell happens. `0` disables. |
| `SCALE_OUT_FRACTION` | `0.5` | Fraction sold at the scale-out (clamped to 0.9). |
| `ENTRY_DELAY_SECONDS` | `30` | Wait after the migration transaction before buying. |
| `MAX_ENTRY_AGE_SECONDS` | `120` | A graduation older than this when first seen is skipped. |
| `MAX_ENTRY_LATENESS_SECONDS` | `60` | An entry attempt more than this past its target time is skipped. |
| `POLL_SECONDS` | `5` | Main loop period. |
| `DISCOVERY_MODE` | `websocket` | WebSocket subscription plus periodic RPC catch-up; `poll` disables the stream. |
| `DISCOVERY_CATCHUP_SECONDS` | `30` | Interval for standard `getSignaturesForAddress` catch-up, including after WebSocket reconnects. |
| `DISCOVERY_POLL_LIMIT` | `10` | Number of recent migration signatures inspected per catch-up. |
| `DISCOVERY_TIMEOUT_SECONDS` | `5` | Maximum time an entry-discovery RPC request may delay the next exit check. |
| `RPC_TIMEOUT_SECONDS` | `8` | Default timeout per standard RPC request. |
| `RPC_BATCH_SIZE` | `20` | Maximum standard JSON-RPC history requests per batch. |
| `RPC_BACKOFF_MAX_SECONDS` | `900` | Maximum entry/discovery provider circuit-breaker delay. |
| `RAW_HISTORY_SIGNATURE_LIMIT` | `40` | Maximum signatures reconstructed for a bundle-history query. |
| `RAW_FUNDER_SIGNATURE_LIMIT` | `25` | Maximum oldest successful wallet transactions inspected when identifying its funder, bounded by the raw history sample limit. |
| `MAX_ENTRIES_PER_CYCLE` | `1` | Maximum due graduations analyzed per loop, preventing entry bursts from starving exits. |

### 4.4 Entry guards

| Variable | Default | Meaning |
|---|---|---|
| `MAX_PRICE_IMPACT_PCT` | `10` | Skip if the buy quote's price impact exceeds this. Raised from 5 on 2026-09-05: on a $5 order 5% is $0.25, and drained pools show 30–79%. |
| `MIN_ENTRY_ROUND_TRIP_PCT` | `80` | Skip unless an immediate executable sell quote returns at least this percentage of the proposed input. |
| `MAX_ENTRY_MARKET_CAP_USD` | `0` | Skip if the implied entry market cap is above this. Off by default since 2026-09-05: a $2M ceiling rejected $13M–$26M graduations carrying $300K–$440K of real liquidity. |
| `MIN_ENTRY_MARKET_CAP_USD` | `25000` | Skip if the implied entry market cap is below this. `0` disables. |
| `MIN_CURVE_AGE_SECONDS` | `120` | Skip if the token graduated less than this many seconds after it was created. `0` disables. |
| `MAX_TOP_HOLDER_PCT` | `20` | Skip if the largest plain-wallet holder owns more than this share of supply. `0` disables. |
| `MIN_CURVE_TRANSACTIONS` | `150` | Skip if the bonding curve filled with fewer successful transactions than this between creation and graduation (one-party fill). Rugs seen live: 2, 9, 15; organic: 311–2,293. Signature metadata only. `0` disables. |
| `MAX_EARLY_SELL_PCT` | `3` | Skip if any single plain wallet has already sold more than this share of supply into the pool since migration (the dump started before entry). `0` disables. |
| `MAX_CREATOR_PRIOR_LAUNCHES` | `3` | Skip if the mint's creator has more prior Pump.fun launches than this in a bounded sample of its history (launch factory). `0` disables. |
| `MAX_CREATOR_HOLD_PCT` | `5` | Skip if the creator still holds more than this share of supply at entry. `0` disables. |
| `CREATOR_HISTORY_SIGNATURES` / `CREATOR_HISTORY_DECODE_LIMIT` | `100` / `60` | Creator history sample: signatures fetched before the launch, and how many are decoded to count Create instructions. |
| `BOOST_WINDOW_SECONDS` | `300` | Pump.fun BOOST buy-and-burn window after migration. Not a guard: every position, trade row and skip row records whether entry and exit fell inside it. |

Bundle graph defaults: inspect up to 50 holders and trace funding for the largest 20; block same-slot (`30%`), direct-funder
(`30%`), two-hop ancestry (`20%`), unpaid token-transfer (`12%`), coordinated
12-slot acquisition (`20%`), repeat-launch cohort (`12%`), creator-linked (`15%`),
top-ten (`50%`), and first-three-slot (`30%`) concentration. Funding lookup completion
below `30%` or identifiable-funder coverage below `30%` fails closed. Coverage from
`30%` through `60%` is classified as partial and multiplies every limit by `0.67`;
higher coverage uses the normal limits. Configure those controls with
`BUNDLE_MAX_WALLETS`, `BUNDLE_FUNDER_MAX_WALLETS`, `BUNDLE_LOOKUP_WORKERS`,
`MIN_FUNDER_LOOKUP_PCT`, and
`MIN_FUNDER_COVERAGE_PCT`. Wallet funders and launch appearances are cached in
`DATA_DIR/wallet_graph_cache.json`.

RPC rate limits and provider outages first fail over across `RPC_URLS`. If every
provider is unavailable, new-entry work opens an exponential circuit breaker (30
seconds, then 60/120/240 seconds, capped by `RPC_BACKOFF_MAX_SECONDS`, default
900). Exit monitoring remains first in the loop and continues attempting RPC
failover. If startup is already provider-limited, wallet reconciliation is deferred
and retried after a minimum five-minute cooldown.

With no endpoint variables set, the executor uses the keyless mainnet endpoints
published by [PublicNode](https://solana.publicnode.com/) and
[Solana](https://solana.com/docs/references/clusters), in that order. An existing
`HELIUS_API_KEY` does not override these defaults. HTTP endpoints and successful
WebSocket connections are logged by hostname with credentials removed.
WebSocket failures put each endpoint on its own 30/60/120-second cooldown, capped
at `RPC_BACKOFF_MAX_SECONDS`; another available endpoint is tried immediately.
When all are unavailable, the thread waits interruptibly until one cooldown ends.
These shared services can throttle or restrict history and have no guaranteed
capacity for this bot. Incomplete bundle data still blocks entries. Explicit
`RPC_URLS`/`RPC_URL` and `RPC_WS_URLS`/`RPC_WS_URL` override the defaults, with plural
variables taking precedence. Use provider-issued endpoints for sustained operation.

### 4.5 Swaps, slippage, retries

Discovery now decodes Pump's `migrate`/`migrate_v2` instructions and requires a
matching PumpSwap `create_pool` (same base mint, quote mint, pool and LP mint).
Layouts come from the official [Pump IDL](https://github.com/pump-fun/pump-public-docs/blob/main/idl/pump.json)
and [PumpSwap IDL](https://github.com/pump-fun/pump-public-docs/blob/main/idl/pump_amm.json).
Transfers, ordinary swaps and repeat migrations without pool creation are not new
graduations. Unknown layouts are skipped. A reduced real migration fixture is
covered in `test_rate_limits.py`.

Provider failure cooldowns apply per endpoint and RPC method: 15 minutes for
401/403 responses, 30 seconds for transient failures. Another available provider
is tried, and blocked holder queries do not disable wallet reads on that endpoint.
Fresh holder snapshots are shared for 10 seconds between top-holder and bundle
checks. Excessive quote impact/stale entries are rejected before expensive metadata.

Raw bundle history follows the Pump bonding-curve PDA. Creation time requires a
successful Pump create/create_v2 instruction with matching mint and curve accounts.
Recent activity is never substituted for creation. Older activity alone establishes
only a lower bound, and an unverified creation remains unknown. Creator attribution
in raw bundle analysis uses the instruction's creator argument, not the fee payer.
Raw curve history searches up to three 1,000-signature pages toward the requested
window, independently of the small funding sample size. Failed transactions and
out-of-window signatures do not consume the full-transaction decode budget.
Up to 1,000 successful transactions in the window are decoded in batches; larger
windows remain unknown rather than being silently sampled. Signature search has
a five-second scheduling budget and decoding checks an adaptive 8–20 second budget between
batches (an in-flight request/provider retry can overrun these budgets).
Oldest-first funding lookups search up to three 1,000-signature pages and must reach
the history boundary before decoding their configured oldest sample. A recent sample
cannot stand in for the original funder. Cached results are scoped to the history
semantics, sample size and exact cutoff; older cache entries are ignored.
`HISTORY` logs report pages, signatures and selected transactions;
`window=covered` confirms signature traversal, not completion of transaction decoding
or the subsequent bundle/funder checks. Exhausted budgets, missing timestamps and
missing responses remain incomplete data and block entry with fail-closed enabled.
Funding ancestry remains a bounded sample, not a complete wallet history.

`holder_target` is a requested maximum. With DAS disabled, standard RPC returns
the largest 20 token accounts, potentially representing fewer wallets. `HOLDERS`
reports the observed wallet count; this is not equivalent to a 50-wallet DAS sample.
`VALUATION` logs the raw quote output, raw total supply, UI values, and decimals.
The guard computes executable quote-implied fully diluted valuation directly as
`USD in × raw supply ÷ raw tokens out`; token decimals cancel, and inconsistent
RPC raw/UI supply fields are rejected. This is FDV from total minted supply, not
circulating market cap, so deliberately oversized supply can still produce a
genuinely large value without indicating a decimal-conversion bug.

Validation includes a saved on-chain create_v2 instruction and deterministic
oldest-history/cache regression tests. Full replay equivalence with earlier accepted
Helius trades is not established: archived Helius responses and contemporaneous
quotes are still required. Raw token balance changes also remain an approximation
of individual transfers. These limitations must be resolved before claiming full
provider equivalence; unit-test success alone does not establish trading performance.

**Provider access remains required:** on September 5, 2026, a live PublicNode
`getTokenLargestAccounts` request returned HTTP 403 stating that indexed requests
require a personal token. Solana's public endpoint returned HTTP 429 for that method.
dRPC's documented public Solana endpoint rejected access as unavailable on the free
plan. Keyless balance reads/streaming therefore do not establish holder-query access.
The code cannot lift provider restrictions: use an authorized provider endpoint
supporting holder queries and transaction history through `RPC_URL(S)`.
Missing bundle evidence continues to block entries; these changes do not restore
Helius credits or guarantee trading through shared public infrastructure.

Operational logs are enabled without additional environment variables:

- `HEARTBEAT` every 30 seconds while the main loop advances: subscription status,
  cumulative notifications/errors/dropped hints, queue depth, last notification and
  successful HTTP scan ages, pending entries, tracked bags, draining and cooldown status.
- `SCAN start`, `SCAN fetched`, `SCAN complete` for each HTTP catch-up, including empty
  results, already-seen signatures, failed transactions, newest transaction age, host,
  and elapsed time. A fetch start without completion localizes a stalled operation.
- `STREAM` summarizes queued WebSocket signatures drained by the executor.
- `DECODE fetching` and `DECODE result` identify missing transaction data/timestamps,
  no candidate mint, ambiguous candidates, duplicate holdings, old events, or queued entries.
- `ENTRY checking` records evaluation start; existing `SKIP`/`BUNDLE`/trade logs record decisions.
- `POSITION` every 30 seconds per managed holding reports its existing executable sell
  quote, remaining basis, take-profit/stop-loss values, peak, scale-out state, and age.

These messages add no network calls and do not change entry or exit decisions.
Heartbeat silence can mean the main loop is blocked; a connected WebSocket alone does
not establish that graduations are being detected. Counters reset on process restart.

| Variable | Default | Meaning |
|---|---|---|
| `SLIPPAGE_BPS` | `1000` | Buy slippage tolerance (10%). Also used for position valuation quotes. |
| `SELL_SLIPPAGE_BPS` | `1500` | Sell slippage tolerance (15%) for every real sell. |
| `ENTRY_RETRIES` | `2` | Extra entry attempts after the first one fails. |
| `ENTRY_RETRY_SECONDS` | `3` | Sleep between entry attempts. |

### 4.6 Moon bags

| Variable | Default | Meaning |
|---|---|---|
| `MOON_BAG` | `0.10` | Fraction of tokens kept at a winning exit (clamped to 0.5), the lottery ticket for a 100x. `0` disables. |
| `MOON_BAG_WINNERS_ONLY` | `1` | `1` keeps a bag only when the exit was profitable; `0` keeps one on every exit (28 bags from losing exits in one day were worth 42% of what was kept eight hours later, none had doubled). |
| `MOON_BAG_TARGET_X` | `100` | Sell a bag once worth this multiple of the value it was kept at. `0` holds forever. |
| `MOON_BAG_CHECK_SECONDS` | `180` | How often bags are re-quoted (each bag is one Jupiter request; bags wait for 100x, so minutes are fine). |
| `MIN_MOON_BAG_USD` | `0.5` | A would-be bag worth less than this is sold with the rest. |
| `MOON_BAG_DEAD_PCT` | `10` | A bag worth less than this percent of its kept value is burned and its account closed (rent back). `0` never burns a bag. |
| `DUST_SWEEP_BELOW_USD` | `0.25` | At startup, an untracked holding worth less than this (about one token account's rent) is burned and its account closed. `0` disables. |
| `RUNNER_ENABLED` | `1` | Runner mode: every graduation goes on a watchlist and is bought once its market cap grows into the swing band with momentum. |
| `RUNNER_ONLY` | `0` | `1` turns the at-graduation entry off so only runners are traded. |
| `RUNNER_MIN_MARKET_CAP_USD` / `RUNNER_MAX_MARKET_CAP_USD` | `400000` / `4000000` | The swing band. |
| `RUNNER_MIN_GAIN_PCT` / `RUNNER_MOMENTUM_MINUTES` | `10` / `15` | Enter only when the cap is up this much from its low of the last N minutes. |
| `RUNNER_WATCH_HOURS` / `RUNNER_CHECK_SECONDS` / `RUNNER_MAX_WATCH` | `6` / `60` / `100` | How long a graduation is watched, how often the watchlist is priced (one batched Jupiter price call per 50 mints), and the watchlist size. |
| `RUNNER_TAKE_PROFIT` / `RUNNER_STOP_LOSS` / `RUNNER_TRAILING_STOP` / `RUNNER_TIME_STOP_MINUTES` | `1.0` / `0.30` / `0.25` / `240` | Exit thresholds for runner positions; graduation positions keep the plain `TAKE_PROFIT` family. |
| `COPY_WALLETS` | (empty) | Comma-separated wallet addresses whose buys are mirrored at the usual position size. `address:500` gives that wallet its own minimum buy size (a whale's $100 buys are pocket change to it; only its real bets are copied); `address:500:0.5` (or `:50%`) also copies that wallet at half the usual position size, never below `MIN_POSITION_USD`. Each is polled every `COPY_POLL_SECONDS` (3); the first poll records a baseline, mirroring only trades still inside `COPY_MAX_TX_AGE_SECONDS`. Buys paid in SOL, wrapped SOL, USDC or USDT are all recognised. |
| `ADOPT_AS_BAG_BELOW_USD` | half of `MIN_POSITION_USD` ($2.50) | At startup, an untracked holding worth less than this (but at least `MIN_ADOPT_USD`) is adopted as a moon bag instead of a position: moon bags left from before a redeploy no longer fill the slots or block a fresh copy of the same coin. |
| `COPY_ROTATE_MIN_AGE_MINUTES` | `20` | A position younger than this keeps its slot; the new copied buy is skipped instead of rotating it out (adopted leftovers always rotate first). |
| `COPY_ROTATE` | `0` | `1`: when every slot is full and a followed wallet buys, sell our oldest position (moon bag kept) and give the slot to the new coin. Off by default: rotation sold coins at whatever price they were at to chase the next one. `0` skips the new buy instead. Never rotates once the daily loss limit is hit. In copy-only mode positions adopted after a redeploy count as held (and rotate first) and use the copy exits. |
| `PRICE_FIRST_VALUATION` / `PRICE_FIRST_MARGIN_PCT` | `1` / `8` | Value open positions from Jupiter's batched price feed (one request for all of them) and only ask for a real sell quote when an exit, rung or scale-out is within this margin of firing. `0` quotes every position every check. |
| `POSITION_CHECK_SECONDS` | `8` in copy-only mode, else `0` | How often each open position is re-quoted for its exits. Every quote is a Jupiter request, and the keyless tier rate-limits a busy loop, which only delays exits. |
| `ACCOUNT_FRACTION` / `MAX_CONCURRENT_POSITIONS` (copy-only) | `0.08` / `10` | In copy-only mode each copy is 8% of the whole account (free SOL plus open positions) and up to ten can be open at once. |
| `MAX_DEPLOYED_FRACTION` | `0.80` | Never more than this share of the account in open positions; a copy that would cross it is skipped. |
| `COPY_ONLY` | `1` when `COPY_WALLETS` is set | Copy trading is the only lane: graduation discovery and the runner watchlist are off. `0` runs every lane. |

#### EVM copy lane (Robinhood Chain, Base, BNB Chain)

A second copy lane (`evm/` package, `python -m evm`, autostarted by the server) mirrors wallets on EVM chains with its own wallet. It watches each followed address's ERC-20 `Transfer` logs every `COPY_POLL_SECONDS`: tokens arriving are a buy (valued by quoting them back to the native coin), tokens leaving are a sell. Sizing (`ACCOUNT_FRACTION`, `MAX_POSITION_USD`, `MIN_POSITION_USD`, `MAX_CONCURRENT_POSITIONS`, `DAILY_LOSS_LIMIT_USD`), the copy rules (`COPY_MIN_BUY_USD`, `COPY_MAX_TX_AGE_SECONDS`, `COPY_FOLLOW_SELLS`, `COPY_ROTATE`, `COPY_LADDER`, `COPY_TAKE_PROFIT`, `COPY_STOP_LOSS`, `COPY_TRAILING_STOP`, `COPY_TIME_STOP_MINUTES`) and the moon bag (`MOON_BAG`, `MOON_BAG_TARGET_X`, `MIN_MOON_BAG_USD`) are the same variables as the Solana lane, applied to this lane's own equity (native balances across its chains plus open positions). State lives in `DATA_DIR/evm_state.json` (mount a volume at `/data` so positions survive redeploys), trades in `DATA_DIR/evm_trades.csv`; `/api/evm` shows both. `DATA_DIR/evm.stop` drains, `DATA_DIR/evm.panic` sells everything.

| Variable | Default | Meaning |
|---|---|---|
| `EVM_COPY_WALLETS` | (empty) | Comma-separated `0x…` addresses to mirror on every configured chain; `address:500` sets that wallet's own minimum buy size, `address:500:0.5` also copies it at half size. Empty means the lane does not start. |
| `EVM_PRIVATE_KEY` | (empty) | The EVM burner wallet: a hex private key (MetaMask → Show private key) or its 12/24-word recovery phrase (first account is used). Required for live mode. Never the Solana key. |
| `EVM_MODE` | `EXECUTOR_MODE` | `paper` or `live` for this lane; defaults to the Solana executor's mode so one setting rules both. |
| `EVM_CHAINS` | `robinhood,base,bnb` | Chains to watch and trade. Fund the wallet with ETH on Robinhood Chain (chain 4663) and Base, and BNB on BNB Chain; `gas_reserve` of 0.002 ETH / 0.005 BNB is never spent on buys. |
| `UNISWAP_API_KEY` | (empty) | Uniswap Trading API key (free at developers.uniswap.org/dashboard). Routes through Uniswap V2/V3/V4, which is how Robinhood Chain launchpad graduates (Pons, Bags, pools.trade → Uniswap V4 pools) are reached. Without it the lane uses the on-chain V3/V2 routers only. |
| `EVM_SLIPPAGE_PCT` / `EVM_CHECK_SECONDS` | `10` / `5` | Swap slippage tolerance; how often open positions are re-valued. |
| `EVM_<CHAIN>_RPC_URL` | public endpoints | Override a chain's RPC (e.g. `EVM_BASE_RPC_URL` for an Alchemy Base endpoint). `EVM_<CHAIN>_V3_QUOTER`, `_V3_ROUTER`, `_V2_ROUTER`, `_WRAPPED_NATIVE`, `_STABLE` override the contract table. |
| `EVM_AUTOSTART` | `1` | `0` keeps the server from launching the lane. |
| `COPY_MIN_BUY_USD` / `COPY_MAX_TX_AGE_SECONDS` | `300` / `90` | Ignore a copied wallet's buys below this size (only its conviction buys are mirrored; its $10-$100 sprays drove sixty losing round trips in six hours), or older than this when detected. Position size is the normal `ACCOUNT_FRACTION` of equity. |
| `COPY_FIRST_BUY_ONLY` / `COPY_ADD_DUST_RATIO` | `1` / `0.02` | Mirror only a wallet's first buy of a coin: its stack before the swap was empty (Solana: the transaction's pre-balance; EVM: balance now minus what arrived), or dust under 2% of what it just bought. A buy into a coin it already holds is an add, usually averaging down, and is logged and skipped. The 2% test is relative on purpose: a wallet left with a dust remainder (say 1,000 units before a 100,000-unit buy) is treated as starting fresh; set the ratio to `0` for a strict empty-stack rule. A wallet that sold out completely and buys again is copied again. Every qualifying buy is recorded in `DATA_DIR/copy_signals.csv` (`GET /api/copy/signals`) with a `signal_id`, the source trade's size (`source_usd`) and time, the GMGN verdict known at the time, and a `status`: `blocked` with the reason (add, already_held, no_slot, gmgn_skip, draining), or `attempted` followed by `filled` (our own `fill_usd`, `fill_tokens`, `fill_signature`, `delay_s`) or `failed` with the entry's skip reason. Positions carry their source signature (`copy_signature`) so fills and exits link back to the signal. `0` copies adds too. |
| `COPY_DECODE_BUDGET` / `COPY_MAX_DECODE_ATTEMPTS` / `COPY_MAX_EXIT_ATTEMPTS` | `40` / `5` / `10` | Source-event bookkeeping. Discovery and decoding are separate: every unseen signature is first queued in a durable per-wallet inbox (`state.copy_inbox`), then at most this many are decoded per poll, oldest first, so a catch-up never starves the 8-second exit checks and a moved cursor or a restart can never lose a discovered event. A fetch that fails or returns nothing is kept in `state.copy_unresolved` and retried with backoff up to the attempt limit, then parked in `state.copy_failed` (visible in `/api/live`). A copied close or trim that fails is kept in `state.copy_pending_exits` bound to the position's immutable `position_id` and retried until done or the attempt limit; it is retired the moment that exact position is gone or already at the target, so a later re-entry into the same coin is never touched by an old intent. A backlog longer than three pages keeps a per-wallet cursor in `state.copy_backfill` and later polls keep paging until known ground. While draining (`executor.stop`), the followed wallets are still polled: sells are followed and pending exits retried, only new entries are refused. |
| `COPY_SELL_SCOPE` | `any` | Whose sells we follow for a held coin: `any` followed wallet (a sell by any of them closes or trims ours, adopted positions included), or `source` (only the wallet whose buy we copied; adopted positions then rely on their own exits). Sells are acted on however late they are seen; only buys have the staleness limit. A sale that predates our own entry into the coin (an earlier episode of theirs) is not applied to the newer position; adopted positions, whose acquisition time is unknown, do follow it. |
| `COPY_GMGN_GATE` / `GMGN_REFRESH_HOURS` | `shadow` / `24` | GMGN's per-wallet verdict as an entry gate. `shadow` mirrors anyway but logs what would be blocked and records the verdict on each signal; `enforce` refuses buys from wallets GMGN marks skip (their sells are still followed and their open positions still managed); `off` ignores it. Verdicts are refreshed every `GMGN_REFRESH_HOURS` after a success and retried every `GMGN_RETRY_MINUTES` (60) after a failure, on a worker thread so no exit check or panic ever waits on GMGN; a failure keeps the previous verdicts. In `enforce` mode a verdict older than three refresh intervals is treated as unknown and not enforced. |
| `COPY_FAST` / `COPY_FOLLOW_SELLS` | `1` / `1` | Skip the slow holder/bundle analysis on copied entries (impact and round-trip checks still run); follow the followed wallets' sells of any coin we hold, whichever lane bought it: a sale of at least `COPY_FULL_SELL_FRACTION` (`0.8`) of their stack closes our position (moon bag applies), a smaller one trims ours by the same share (trims worth under $1 are skipped). |
| `COPY_TAKE_PROFIT` / `COPY_STOP_LOSS` / `COPY_TRAILING_STOP` / `COPY_TIME_STOP_MINUTES` | `0.75` / `0.30` / `0.25` / `1440` | Exit thresholds for copied positions between the wallet's own sell and ours. With a ladder the take profit only backstops its top rung. |
| `TRAILING_ARM_GAIN` | `0.30` | Every trailing stop (graduation, runner, copy, EVM) arms only once the position has been up at least this much from its cost basis. Before that the stop loss is the only downside exit, so a coin that popped 12% and pulled back is not sold at −18%. `0` arms from entry. |
| `COPY_LADDER` | `1.4:40,1.8:30,3:30` | Phase profit out on copied positions: sell that percent of the entry tokens once the price reaches that multiple of the entry price (40% at +40%, 30% at +80%, the rest at 3x). The last rung closes the position (moon bag applies). Empty disables it. |
| `SCOUT_MODE` | `shadow` | GMGN wallet scouting (`scout.py`; needs `GMGN_API_KEY`, otherwise inert). Every `SCOUT_DISCOVERY_HOURS` (6) a worker thread pulls candidate wallets from GMGN's smart-money and KOL feeds and from the top traders of a rotating token sample (what the followed wallets bought, what we traded, GMGN's trending list), then enriches up to `SCOUT_ENRICH_PER_CYCLE` (8) wallets per cycle with 7d/30d/all stats, holdings (transferred-in inventory, open losses) and up to `SCOUT_ACTIVITY_PAGES` (40) pages of buy/sell history, under `SCOUT_GMGN_UNITS_PER_CYCLE` (600) request units. Candidates then move `discovered → research → shadow`: their first buys are followed on-chain with real Jupiter buy quotes (baseline, then the same quote again `SCOUT_STRESS_SECONDS` = 20 s later), the same first-buy rule, sizing, deployment cap, ladder and exits as production, and a virtual sell quote only when an exit is due. Nothing is ever swapped: shadow trades are recorded in the state and `scout_shadow_trades.csv`. `off` disables the lane. |
| `SCOUT_LIVE` / `SCOUT_MAX_LIVE` / `SCOUT_LIVE_SIZE` / `SCOUT_LIVE_LOSS_BUDGET_USD` | `0` / `3` / `0.25` / `0` | Promotion switch, off by default: with `SCOUT_LIVE=1` **and** a loss budget above 0, wallets that pass every mandatory gate (below) are promoted to `live`, at most `SCOUT_MAX_LIVE`, one per evidenced wallet cluster, and the copy lane mirrors their first buys at `SCOUT_LIVE_SIZE` of the usual size (a size under `MIN_POSITION_USD` is skipped, never rounded up). Realized P&L of their positions and moon bags counts against the budget; at −budget the wallet is `paused` (open positions stay managed, its sells stay followed). `SCOUT_MAX_QUALIFICATION_AGE_HOURS` (48) stales a qualification; `SCOUT_REQUALIFY_HOURS` (24) is the cooldown after a demotion. |
| `SCOUT_ELITE_ONLY` | `0` | `1` also gates the configured `COPY_WALLETS` entries: a buy is mirrored only while the wallet is `qualified` or `live` with a fresh evaluation (sells of held coins and open positions are never affected; the signal is recorded as `blocked: elite_only`). With the default thresholds this blocks every buy until a wallet passes all gates, so leave it off unless that is the intent. |
| `SCOUT_MIN_HISTORY_DAYS` / `SCOUT_MIN_EPISODES_30D` / `SCOUT_MIN_TOKENS_30D` / `SCOUT_MIN_ACTIVE_DAYS_30D` / `SCOUT_MIN_PROFIT_FACTOR` / `SCOUT_MAX_BEST_TOKEN_SHARE` / `SCOUT_MAX_DRAWDOWN` / `SCOUT_MIN_MEDIAN_HOLD_MINUTES` / `SCOUT_MAX_FAST_EXIT_FRACTION` | `60` / `30` / `20` / `10` / `1.5` / `0.4` / `0.25` / `30` / `0.1` | Historical qualification gates on GMGN evidence, all mandatory: positive realized P&L over 7d, 30d and all time; enough history, closed episodes (a buy from an empty stack to a stack back at dust, P&L net of fees), tokens and active days in 30 days; profit factor; net still positive without the best token and the best token at most that share of gross profit; drawdown of realized episode equity (open positions are not marked, which the report says); median time to the first material sell and the share of first buys exited within 60 s, over first buys of at least `COPY_MIN_BUY_USD`; open unrealized losses not larger than 30-day realized profit; tokens whose inventory arrived by transfer are excluded from profitability; wash-trading, bundler and MEV tags reject the wallet. A gate whose evidence GMGN does not provide is `missing`, and `missing` never qualifies. |
| `SCOUT_LEADERBOARD_TOP_FRACTION` / `SCOUT_LEADERBOARD_TOP_N` / `SCOUT_LEADERBOARD_MIN_SNAPSHOTS` / `SCOUT_LEADERBOARD_SPAN_DAYS` | `0.05` / `100` / `3` / `7` | The persistent-leaderboard gate: a global rank within the top 5% (or top 100) on at least 3 daily snapshots spanning 7 days. Only global rank evidence counts; a token's top-trader list never does. GMGN's OpenAPI exposes no wallet leaderboard and its `tag_rank` came back 0 on every wallet probed, so this gate is `missing` for every candidate today and no wallet can qualify until GMGN provides rank data. The report states this instead of pretending. |
| `SCOUT_SHADOW_MIN_DAYS` / `SCOUT_SHADOW_MIN_TRADES` / `SCOUT_SHADOW_MIN_TOKENS` / `SCOUT_SHADOW_MIN_ACTIVE_DAYS` / `SCOUT_SHADOW_MIN_PROFIT_FACTOR` / `SCOUT_SHADOW_MAX_DRAWDOWN` | `14` / `30` / `15` / `7` / `1.3` / `0.15` | Forward (shadow) gates: enough closed shadow trades over enough days and tokens, positive net after fees at the baseline latency **and** at 20 s extra latency, profit factor, drawdown against the peak capital the shadow trades tied up, net still positive without the best token, and a positive 95% bootstrap lower bound on the mean trade return resampled by token and by day clusters. `SCOUT_MAX_SHADOW` (25) wallets are polled on-chain at a time under `SCOUT_DECODE_BUDGET` (20) transactions and `SCOUT_QUOTE_BUDGET` (6) Jupiter quotes per poll every `SCOUT_POLL_SECONDS` (10), all separate from the production copy lane's budgets. |

### 4.7 Restart safety and housekeeping (live mode)

| Variable | Default | Meaning |
|---|---|---|
| `MIN_ADOPT_USD` | `1.0` | Untracked holdings worth at least this are adopted as managed positions at startup. |
| `CLOSE_EMPTY_ACCOUNTS` | `1` | Close empty token accounts at startup and after full sells to reclaim rent. |
| `STUCK_AFTER_MINUTES` | `15` | Minutes past the time stop after which three consecutive sell failures move a position to the stuck list. |

### 4.8 Dashboard equity simulation

| Variable | Default | Meaning |
|---|---|---|
| `FIXED_FEE_PER_SIDE` | `0.10` | Flat USD fee per swap in the dashboard's simulated equity curve and the sizing job. |
| `ACCOUNT_FRACTION` | `0.10` | Fraction per trade in that curve, overridden by `sizing_summary.json`'s recommendation when present. |

### 4.9 Batch mode

| Variable | Default | Meaning |
|---|---|---|
| `BACKTEST_COMMAND` | (none) | If set (e.g. `run --limit 5`) the container runs that `grad_backtest.py` command once and exits instead of serving the dashboard. |

---

## 5. The strategy

**Signal.** A transaction on the Pump.fun migration address that moves exactly
one non-quote token. That token just graduated.

**Entry.** `ENTRY_DELAY_SECONDS` (30 s) after the migration, buy with
`min(equity × ACCOUNT_FRACTION, MAX_POSITION_USD)` of SOL via Jupiter, if
every guard in section 6.4 passes.

**Exit.** Every `POLL_SECONDS`, get a Jupiter sell quote for the whole
position. That executable value, not a chart price, is compared against the
basis: take profit at +`TAKE_PROFIT`, stop loss at −`STOP_LOSS`, trailing stop
at `TRAILING_STOP` below the peak, time stop at `TIME_STOP_MINUTES`. Optional
scale-out banks part of a winner early; optional moon bag keeps a slice of a
winner riding.

**Sizing.** Fractional by default, with a floor and a cap. The floor exists
because a Solana round trip has real fixed costs: about a cent of network fees
plus 0.002 SOL of token-account rent, which the executor now reclaims after
every full sell.

**What the numbers said so far.** One overnight live session (52 closed round
trips, $5 positions) had a 37% win rate, average winner +33%, average loser
−32%. Break-even at that win rate needs winners to average about +56%. The
losers were dominated by bundled launches that dumped within a minute of
migrating; the guards in 6.4 target exactly that profile. Nothing else in this
document should be read as a claim that the strategy is profitable.

---

## 6. Executor in depth

`executor.py` is a single process with one loop. It keeps its state in
`executor_state.json`, appends every closed trade to `live_trades.csv`, every
skipped opportunity to `skips.csv`, and every log line to `executor.log` (and
stdout, which is what Railway shows).

### 6.1 Startup sequence

1. `Config()` reads every variable; `validate()` requires a standard RPC
   endpoint and migration address, plus the burner-wallet key in live mode.
2. `load_state()` reads `executor_state.json` or creates a fresh state.
3. The first log line prints the parameters in effect:
   ```
   executor starting: mode=live fraction=0.05 max_pos=$100.0 tp=+75% sl=-30% time_stop=60m trail=20%
   scale_out=40%x50% moon_bag=5%(winners only)@100x(min$0.50,dead<5%) slippage=1000/1500bps(buy/sell)
   mcap=$25,000-$2,000,000 curve_age>=120s top_holder<=20% adopt>=$1.0 stuck_after=15m retries=2 daily_loss_limit=$25.0
   ```
4. Live mode: logs the wallet address and SOL balance, and warns if the
   balance is at or below the fee reserve.
5. If the state file has open positions: `resuming N open position(s) from state file`.
6. Live mode: `reconcile_wallet()` (section 6.9).

### 6.2 Main loop

Each iteration, wrapped so one exception logs `ERROR loop: …` and the loop
continues after `POLL_SECONDS`:

1. Roll the daily P&L counter if the UTC date changed.
2. Read the flag files: `executor.panic` means panic; `executor.stop` or panic means draining.
3. Fetch the SOL price when needed and manage every open position first (6.5).
4. Manage moon bags, stuck positions, and panic liquidation before doing any entry work.
5. If not draining, drain WebSocket graduation hints and run standard-RPC catch-up when due (6.3), bounded by `DISCOVERY_TIMEOUT_SECONDS`.
6. Collect due graduations and analyze at most `MAX_ENTRIES_PER_CYCLE`; later items remain queued.
7. Poll the followed wallets (configured `COPY_WALLETS`, scouted wallets promoted to `live`, and any scouted wallet a still-open position was copied from), while draining too so sells keep being followed.
8. Tick the wallet scout (`scout.py`) under its own request budgets; a failure there is logged and never reaches steps 3 and 4.
9. Save state.

Pending graduations are persisted in `executor_state.json`. On restart, entries
later than `MAX_ENTRY_LATENESS_SECONDS` are discarded rather than replayed.

### 6.3 Detection

At startup a background `logsSubscribe` WebSocket watches `MIGRATION_ADDRESS`.
`poll_graduations()` drains those signature hints, and every
`DISCOVERY_CATCHUP_SECONDS` calls standard `getSignaturesForAddress` so a reconnect
does not silently lose launches. Each unseen signature is fetched with parsed
standard `getTransaction` data:

- Extract candidate mints from parsed instructions and pre/post token balances, excluding WSOL, USDC, and USDT.
- Exactly one candidate is a graduation. Two or more logs `SKIP …: ambiguous graduation tx …`. Zero is ignored.
- If the transaction is older than `MAX_ENTRY_AGE_SECONDS`, `SKIP …: graduation too old at detection (Ns)`.
- Otherwise queue it with `enter_at = timestamp + ENTRY_DELAY_SECONDS` and log `DETECTED graduation <mint> (age Ns, entering at +30s)`.

The "too old" skips that appear right after a restart are the backlog from the
downtime; they are expected.

### 6.4 Entry pipeline and guard order

`enter_with_retry` calls `try_enter` up to `1 + ENTRY_RETRIES` times. An
exception (a rejected swap, a bad quote) triggers a retry after
`ENTRY_RETRY_SECONDS`; a `SKIP` is a decision and is not retried. Every guard
runs again on every attempt, so the lateness window still bounds how late an
entry can land.

`try_enter`, in order:

1. **Sizing guards** (`position_size_usd`): open bot-opened positions must be below `MAX_CONCURRENT_POSITIONS` (adopted holdings don't count); daily realized P&L must be above −`DAILY_LOSS_LIMIT_USD` when that is set (0 skips the check); size = `min(equity × ACCOUNT_FRACTION, MAX_POSITION_USD)` must be at least `MIN_POSITION_USD` and at most equity. Failure: `SKIP …: sizing guards (open=N, daily_pnl=X)`.
2. **Buy quote** WSOL → token for the sized amount at `SLIPPAGE_BPS`. A zero-token quote raises and is retried.
3. **Metadata lookups** (`entry_metadata`):
   - raw token supply → executable quote-implied FDV = USD in × raw supply ÷ raw tokens out; raw units cancel so decimals cannot skew the result;
   - mint creation time via `getSignaturesForAddress` with early stop → curve age = graduation − creation;
   - largest plain-wallet holder via `getTokenLargestAccounts` plus two `getMultipleAccounts` calls, ignoring program-owned accounts (the pool, the bonding curve, the Mayhem vault) and our own wallet.
   - a mandatory bundle graph over up to 50 plain-wallet holders. It combines same-slot and short-window purchases, first-three-slot purchases, top-ten concentration, direct and two-hop non-CEX funding ancestry, unpaid wallet-to-wallet token distributions, creator linkage, and wallet cohorts previously seen together. The broad 50-holder sample feeds transfer, coordination, repeat-cohort and concentration checks; funding ancestry is traced over the largest 20 holders so adding small holders does not dilute coverage. Lookup completion is measured separately from identifiable-funder coverage. With `BUNDLE_FAIL_CLOSED=1`, missing supply, creation history, purchase history, less than 80% lookup completion, or less than 30% funder coverage skips the entry; 30–60% coverage applies stricter thresholds.
4. **`entry_guard_reason`**, first hit wins:
   1. lateness > `MAX_ENTRY_LATENESS_SECONDS` → `stale entry: Ns past target`
   2. price impact > `MAX_PRICE_IMPACT_PCT` → `price impact X% > 10.0% (pool too thin for our size)`
   3. market cap > `MAX_ENTRY_MARKET_CAP_USD` → `market cap $X > $Y (already pumped far past graduation)`
   4. market cap < `MIN_ENTRY_MARKET_CAP_USD` → `market cap $X < $Y (already dumped since graduation)`
   5. curve age < `MIN_CURVE_AGE_SECONDS` → `graduated Ns after creation < 120s (observed history does not meet minimum curve age)`
   6. curve transactions < `MIN_CURVE_TRANSACTIONS` → `curve filled with N successful transactions < 150 (one-party fill)`
   7. top holder > `MAX_TOP_HOLDER_PCT` → `top wallet holds X% of supply > 20% (one holder can dump the pool) [wallet]`
   8. creator holding > `MAX_CREATOR_HOLD_PCT` → `creator still holds X% of supply > 5% (the wallet that dumps)`
   9. creator launches > `MAX_CREATOR_PRIOR_LAUNCHES` → `creator launched N prior Pump.fun tokens > 3 (launch factory)`
   10. early seller > `MAX_EARLY_SELL_PCT` → `a wallet already sold X% of supply since migration > 3% (dump started before entry) [wallet]`
   11. incomplete mandatory bundle data → `bundle data unavailable (…)`. A curve window busier than the decode budget is no longer refused: the earliest rows are decoded and the snapshot reports `history_total` / `history_decoded` (log line shows `sampled=`).
   8. same-slot holdings > `MAX_BUNDLE_SLOT_PCT`
   9. largest connected funding cluster > `MAX_CLUSTER_PCT`
   10. two-hop ancestry cluster > `MAX_ANCESTRY_CLUSTER_PCT`
   11. unpaid token-transfer cluster > `MAX_TRANSFER_CLUSTER_PCT`
   12. coordinated rolling-slot burst > `MAX_COORDINATED_BUY_PCT`
   13. repeat-launch cohort > `MAX_REPEAT_COHORT_PCT`
   14. creator-linked cluster > `MAX_DEV_CLUSTER_PCT`
   15. top-ten wallet concentration > `MAX_TOP10_WALLET_PCT`
   16. first-three-slot purchases > `MAX_EARLY_BUY_PCT`
5. **Executable liquidity guard**: a reverse quote token → WSOL must succeed and return at least `MIN_ENTRY_ROUND_TRIP_PCT` of the proposed input.
6. **Execute.** Live: build, sign, send, confirm (6.10). If the swap reports failure, wait 15 s and check the wallet; if tokens landed anyway the position is adopted with `buy_signature = "unconfirmed"`. Paper: debit the paper balance.
7. Record the position (6.12) and log `ENTER <mint> $X (live <sig>… | paper fill)`.

Why those guards exist:

- **Price impact**: a thin pool makes the round trip cost more than the trade can earn.
- **Ceiling**: a token at $1M+ thirty seconds after a $69K graduation was pumped by a bundle before we arrived, and that buyer dumps into whoever follows. Note that on the overnight sample the over-$300K band was actually the best-performing bucket on closed trades, so this threshold is under review; $2M is the current live setting.
- **Floor**: a token far below the graduation cap a minute later was already dumped into its own pool. The motivating case (SOLL) had its creator sell 78% of supply 24 s after migration; the bot bought at a $450 cap. Sub-$30K entries went 1 for 7 overnight.
- **Curve age**: filling a whole bonding curve in seconds takes one buyer. SOLL graduated 29 s after creation with six buyers.
- **Holder concentration**: the wallet that holds a big slice at entry is the one that dumps. SOLL's creator held 59% at graduation.
- **Split-wallet concentration**: checking only the largest individual wallet misses operators who divide supply among many wallets. The live gate reconstructs funding ancestry, token distributions, coordinated purchase bursts, and repeat-launch cohorts, then blocks their combined supply percentage. `BUNDLE_LOG_ONLY=1` is available only as an explicit canary mode; the default is enforcement.
- **Canonical PumpSwap liquidity**: Pump.fun burns the LP tokens received when a coin graduates, so a generic “LP locked” badge is not an additional safety signal for canonical graduations. The bot instead requires two-way executable Jupiter liquidity immediately before signing the buy.

### 6.5 Position management and exit order

`manage_positions` runs every loop for every open position:

1. Sell-quote the whole position (at `SLIPPAGE_BPS`; the real sell uses `SELL_SLIPPAGE_BPS`). Update `peak_usd` and `last_value_usd`.
2. **Scale-out check** (before any exit): if enabled, not yet done, and value ≥ basis × (1 + `SCALE_OUT_AT`) → `scale_out` and re-evaluate next loop.
3. **Exit decision** (`decide_exit`), first hit wins:
   1. panic flag → `panic`
   2. value ≥ basis × (1 + `TAKE_PROFIT`) → `take_profit`
   3. value ≤ basis × (1 − `STOP_LOSS`) → `stop_loss`
   4. `TRAILING_STOP` > 0 and value ≤ peak × (1 − trail) → `trailing_stop`
   5. age ≥ `TIME_STOP_MINUTES` → `time_stop`
4. On an exit reason, `close_position` (6.7 decides the moon bag, then sells, records, reclaims rent). On success the failure counter resets.
5. On any exception: `WARN managing <mint>: …`, increment `sell_failures`, and possibly move to stuck (6.8).

Live-mode edge case: if the wallet holds zero tokens when a close is
attempted (a previous sell landed after its confirmation timeout, or you sold
manually), the position is closed at its last quoted value with reason
`<reason>_unconfirmed` and a warning to verify on an explorer.

### 6.6 Scale-out

At `SCALE_OUT_AT`, sell `SCALE_OUT_FRACTION` of the tokens. The remainder keeps
its cost basis reduced proportionally and its peak scaled down, so every
threshold stays at the same token price. Recorded as its own `scale_out` row.
The cost is a second sell leg per winner and a smaller share riding to the
full target.

### 6.7 Moon bags

At a non-panic exit, `close_position` may keep `MOON_BAG` of the tokens:

- not if `MOON_BAG_WINNERS_ONLY` is on and the last quoted value was not above basis;
- not if the kept slice would be worth less than `MIN_MOON_BAG_USD` (its rent would exceed it).

The bag is recorded with `cost_usd` (its share of the basis), `kept_usd` (what
selling it would have returned at the exit), and the parent exit reason. Log:
`MOONBAG <mint>: keeping 5% (N tokens, worth $X now, sells at 100x ($Y))`.

`manage_moon_bags` runs every `MOON_BAG_CHECK_SECONDS`: each bag is sell-quoted
(silently skipped if unquotable), `last_value_usd`, `peak_usd`, and `last_x`
are updated, and then:

- value ≥ kept × `MOON_BAG_TARGET_X` → sold, `MOONBAG TARGET …`, trade row `moon_bag_target`;
- value < kept × `MOON_BAG_DEAD_PCT`% → burned and the account closed, `MOONBAG DEAD …`, trade row `moon_bag_dead` at −100% of `cost_usd`.

Panic liquidates every bag. Bags live in the state file, so without a
persistent volume a redeploy forgets them and the restart adopts the tokens as
an ordinary position that time-stops out within the hour.

### 6.8 Stuck positions

If a position's sells keep failing (three or more consecutive exceptions) and
it is more than `TIME_STOP_MINUTES + STUCK_AFTER_MINUTES` old, it is moved
from `positions` to `stuck` so it stops occupying a slot. The dashboard lists
it, panic still tries to sell it, and the log says to sell it manually.

### 6.9 Wallet reconciliation and rent reclaim

Every token account on Solana holds 0.00203928 SOL of rent until it is closed.
A night of trading without cleanup left 59 empty accounts holding ~0.12 SOL.

`reconcile_wallet` runs once at live startup, over every token account the
wallet owns (both Token and Token-2022):

- quote tokens (WSOL, USDC, USDT) are ignored;
- empty accounts are closed (one per second to respect rate limits);
- untracked holdings are sell-quoted; if unquotable they are left alone with a `WARN … held but not quotable`; if worth less than `MIN_ADOPT_USD` they are left alone; otherwise they are adopted as a managed position with basis = current value and the clock starting now (`ADOPTED untracked holding …`).

Adopted positions are managed normally but do not consume an entry slot. Note
that adoption loses the original entry price and peak, which is why the `/data`
volume matters.

After every full live sell, `reclaim_rent` re-reads the exact token account at
confirmed commitment, burns any real remaining dust, and closes the account
(`RENT reclaimed ~0.0020 SOL …`). The confirmed re-read prevents a stale
pre-sell balance from generating a failed burn transaction.

### 6.10 Rate limits and transaction retries

- Every RPC and Jupiter call goes through `with_backoff`: on HTTP 429 it sleeps 0.5, 1, 2, 4 s and retries, then tries once more; any other error is raised immediately.
- The SOL price is cached 30 s.
- `execute_swap` builds the transaction from the quote, signs, sends with preflight and up to 3 RPC retries, then polls `getSignatureStatuses` every 2 s for up to 60 s. If the send is rejected with `BlockhashNotFound` it rebuilds once on the same quote.
- Jupiter error 6001 (`0x1771`) is slippage exceeded: the price moved more than the tolerance between quote and execution. Error 6000 is a route that no longer exists. Both are logged as one readable line.

### 6.11 Control: start, stop, panic

The server (section 7) starts the executor as a child process and controls it
through flag files in `DATA_DIR`:

| Action | Mechanism | Effect |
|---|---|---|
| Start | `POST /api/executor/start` or `EXECUTOR_AUTOSTART=1` | Removes `executor.stop`, launches `executor.py`. |
| Stop (drain) | `POST /api/executor/stop` → `executor.stop` | No new detections or entries; open positions are still managed to their exits. |
| Panic | `POST /api/executor/panic` → `executor.panic` | Every position, moon bag, and stuck position is market-sold; then the executor drains. |

There is no kill endpoint. To stop the process, stop the container.

### 6.12 State file schema

`DATA_DIR/executor_state.json`, written atomically after every loop:

```
{
  "mode": "paper" | "live",
  "paper_balance_usd": float,
  "positions": [ position … ],
  "moon_bags": [ moon_bag … ],
  "stuck": [ position + "stuck_at" … ],
  "daily": { "date": "YYYY-MM-DD", "realized_pnl_usd": float },
  "seen_signatures": [ last 500 migration signatures ],
  "moon_bags_checked_ts": float,
  "draining": bool,
  "updated_at": "ISO Z"
}

position = {
  "mint", "tokens" (raw), "position_usd" (basis), "opened_ts", "opened_at",
  "graduated_at" (null when adopted), "buy_signature" ("" paper | sig | "unconfirmed" | "adopted"),
  "entry_price_impact_pct", "entry_market_cap_usd", "entry_curve_age_seconds", "entry_top_holder_pct",
  "peak_usd", "last_value_usd", "sell_failures", "last_sell_error", "scaled_out", "adopted"
}

moon_bag = {
  "mint", "tokens", "cost_usd", "kept_usd", "peak_usd", "created_at", "from_exit",
  "last_value_usd", "last_x"
}
```

### 6.13 Files written and the trade CSV

| File | Written by | Contents |
|---|---|---|
| `executor.log` | every `log()` | `<ISO Z> <message>` per line |
| `executor_state.json` | `save_state` | schema above |
| `live_trades.csv` | closes, scale-outs, bag sells, dead-bag burns | one row per exit leg |
| `skips.csv` | `skip()` | `timestamp,mint,reason` plus everything known at the time: seconds after graduation, BOOST window, market cap, impact, curve age, curve transaction count, top holder, creator, creator launches and holding, early seller, bundle metrics, history sample size, round trip |
| `scout_signals.csv`, `scout_shadow_trades.csv` | `scout.py` | every first buy a shadowed wallet made and what the shadow did with it; every closed shadow trade with baseline and 20 s-stress P&L |
| `executor.stop`, `executor.panic` | server / executor | empty flag files |

`live_trades.csv` columns:

```
opened_at, closed_at, mint, mode, position_usd, exit_usd, net_return, exit_reason,
buy_signature, sell_signature, entry_price_impact_pct, peak_gain_pct,
entry_market_cap_usd, entry_curve_age_seconds, entry_top_holder_pct
```

An existing file keeps its original header; new columns are only written to
new files. `exit_reason` values: `take_profit`, `stop_loss`, `trailing_stop`,
`time_stop`, `panic`, any of those with `_unconfirmed`, `scale_out`,
`moon_bag_target`, `moon_bag_dead`, `panic_moon_bag`, `panic_stuck`.

### 6.14 Log line reference

| Line | Meaning |
|---|---|
| `DETECTED graduation <mint> (age Ns, entering at +30s)` | Queued for entry. |
| `SKIP <mint>: <reason>` | Passed on, with the reason; also in `skips.csv`. |
| `ENTER <mint> $X (live <sig>…)` | Bought. |
| `SCALE-OUT <mint>: sold 50% for $X (+Y%); remainder basis $Z` | Partial take-profit. |
| `EXIT <mint> <reason> $X (+Y%, peaked +Z%)` | Closed. |
| `MOONBAG <mint>: keeping 5% (…)` | A bag was kept. |
| `MOONBAG TARGET <mint>: worth $V = Nx the $K kept; sold for $P` | A bag hit its multiple. |
| `MOONBAG DEAD <mint>: worth $V vs $K kept; burned, rent reclaimed` | A bag died; rent back. |
| `RENT reclaimed ~0.0020 SOL from <mint>'s token account` | Account closed after a sell. |
| `ADOPTED untracked holding <mint> worth $X; managing it from here` | Startup found tokens it didn't know about. |
| `wallet reconciled: adopted N position(s), closed M empty token account(s) (~X SOL rent)` | Startup summary. |
| `STUCK <mint>: N consecutive sell failures past its time stop … Slot freed` | Moved to the stuck list. |
| `WARN entry <mint> attempt a/n failed: …; retrying` | Swap rejected; retrying. |
| `WARN managing <mint>: …` | A sell or quote failed this loop. |
| `WARN <mint>: market cap / curve age / holder concentration check unavailable` | A guard's lookup failed; that guard was skipped for this entry. |
| `WARN <mint>: held but not quotable (…); leaving it alone` | Startup found a token with no Jupiter route. |
| `ERROR loop: …` | The whole iteration failed; the loop continues. |
| `panic complete: all positions and moon bags closed, executor draining` | Panic finished. |

---

## 7. Dashboard and HTTP API

`server.py` is a FastAPI app. `GET /` serves `static/index.html`, which polls
five endpoints every 15 seconds. The page has no admin controls; the POST
endpoints are for `curl` and the like.

### Endpoints

| Method and path | Auth | Returns |
|---|---|---|
| `GET /healthz` | none | `{"status":"ok"}` (Railway healthcheck) |
| `GET /` | none | the dashboard |
| `GET /logs` | HTTP Basic | phone-friendly runtime-log viewer with 1h/6h/24h/7d ranges, common bot filters, free-text search, refresh, and Copy All |
| `GET /api/runtime-logs?hours=6&q=BUNDLE&limit=20000` | HTTP Basic | timestamp-filtered JSON/text from `DATA_DIR/executor.log`; limited to 7 days and 20,000 returned lines |
| `GET /api/overview` | none | backtest stats, exit-reason counts, every net return, simulated equity curve, `summary.json`, `sizing_summary.json`, job status |
| `GET /api/trades?limit=200` | none | rows of `trade_results.csv`, newest first |
| `GET /api/errors?limit=200` | none | last rows of `errors.csv` |
| `GET /api/activity?lines=100` | none | last lines of `run.log` plus job status |
| `GET /api/scout` | none | wallet scouting report: every candidate's lifecycle state, discovery sources, leaderboard evidence, verified exposure, history coverage, net P&L, profit factor, win rates (token- and episode-level), drawdown with its basis, best-token concentration, transfer-in exclusions, copyability numbers, shadow results (baseline and stress), risk flags with confidence, relationship cluster, and the last promotion decision. Read-only; built from the executor state. |
| `GET /api/scout/{address}` | none | one candidate in full with its shadow trades, shadow signals and live loss-budget book |
| `GET /api/live?limit=100` | none | executor running/draining flags, mode, full state JSON, `live_trades.csv` rows, closed count, win rate, realized P&L, last 60 lines of `executor.log` |
| `POST /api/run` | admin | body `{"stage": "collect"\|"run"\|"sizing"\|"optimize", "extra_args": "…"}`; launches one background job (409 if one is running); output appended to `run.log` |
| `POST /api/executor/start` | admin | launches the executor (409 if running) |
| `POST /api/executor/stop` | admin | touches `executor.stop` |
| `POST /api/executor/panic` | admin | touches `executor.panic` |

Admin auth: header `x-admin-token` compared to `ADMIN_TOKEN` with a
constant-time comparison; 503 if the token is unset, 401 on mismatch. Job
arguments that try to pass `--helius-api-key` are rejected; credentials go
through the environment.

```bash
TOK=…; URL=https://your-app.up.railway.app
curl -X POST $URL/api/run -H "x-admin-token: $TOK" -H "content-type: application/json" -d '{"stage":"run","extra_args":"--limit 5"}'
curl -X POST $URL/api/executor/start -H "x-admin-token: $TOK"
curl -X POST $URL/api/executor/stop  -H "x-admin-token: $TOK"
curl -X POST $URL/api/executor/panic -H "x-admin-token: $TOK"
```

### Dashboard panels

- **Header**: job pill (running stage or idle) and the modification time of `trade_results.csv`.
- **KPI tiles**: simulated balance, trade count (with skipped-token count), win rate, median and mean net return per trade, position size in use (from the sizing recommendation when one exists).
- **Simulated equity**: the backtest's trades replayed at the account fraction with `FIXED_FEE_PER_SIDE` charged twice per trade; hover for the trade.
- **Net return distribution**: histogram of net returns after costs.
- **Live executor**: shown when the executor is running or has state. Pill (running / draining / stopped), mode badge (`LIVE MONEY` in red, or paper), open positions with sizes, moon bags with their current multiple, stuck positions, realized P&L or paper balance, closed trades, win rate, today's P&L, and the 15 most recent closed trades.
- **Recent trades**: the backtest's newest 25 rows.
- **Activity log**: tail of `run.log`, then a tail of `executor.log`.
- **Skipped tokens**: the backtest's `errors.csv`.

The page follows the viewer's light/dark preference and an explicit
`data-theme` override. Everyone who can reach the URL sees the numbers; the
dashboard exposes no credentials. The log viewer uses HTTP Basic so mobile
Safari can remember the login: username `admin`, password `LOG_VIEWER_TOKEN`
(or `ADMIN_TOKEN` when no separate viewer token is configured). Responses are
marked `no-store`, the secret is never accepted in the URL, and the page is
read-only. Historical availability follows `executor.log`: mount `DATA_DIR` at
`/data` to keep it across redeploys. It intentionally shows bot runtime lines,
not Railway build logs or Uvicorn's own stdout.

---

## 8. Research pipeline

### 8.1 `collect`

```bash
python grad_backtest.py collect --count 500 --start-time 2026-08-01T00:00:00Z --end-time 2026-08-08T00:00:00Z
```

Pages backwards through the migration address's enhanced transactions (100
per page, finalized). Every transaction is a candidate; the graduated token is
the one non-quote mint it moved. One row per (mint, signature).
`extraction_status` is `confirmed` when exactly one candidate mint was present,
otherwise `needs_review` (the pricing step skips those unless
`--include-needs-review`). Output `graduations.csv` with columns
`mint_address, graduation_timestamp, tx_signature, extraction_status,
candidate_count_in_tx, helius_source, helius_type`.

Any CSV with `mint_address` and `graduation_timestamp` works as input to `run`.

### 8.2 `run` and the simulation rules

```bash
python grad_backtest.py run --limit 5     # then without --limit
```

Per token:

1. **Pool discovery** on GeckoTerminal: pools for the mint whose dex id contains `pump` or `raydium` and that were created within 6 hours of the graduation; closest creation time wins, PumpSwap over Raydium, then higher liquidity.
2. **Price data**: up to 4,000 one-minute candles from graduation to +24 h, plus eight 30-second candles around the entry target. Cached in `cache/<mint>.json` so a rerun with different parameters makes no API calls (`--refresh` refetches).
3. **Entry**: open of the first 30-second candle at or after graduation + `--entry-delay-seconds`, tolerance 90 s. The entry candle timestamp is recorded so latency can be audited.
4. **Run-up filter** (`--max-entry-runup`): skip tokens whose entry price is more than that fraction above the graduation price.
5. **`simulate_trade`** over the one-minute candles from entry to the time stop. Within each candle, in this order, first hit wins:
   1. take-profit and stop-loss both touched → exit at the stop price, reason `sl_ambiguous` (intraminute order is unknowable, so the adverse order is assumed and flagged);
   2. stop-loss touched → `stop_loss`;
   3. trailing stop touched (only if its price is above the stop price; the peak is updated only after a candle, so a candle's own high never arms the stop its own low triggers) → `trailing_stop`, flagged ambiguous if take-profit was also touched;
   4. scale-out level touched → recorded as filled, simulation continues;
   5. take-profit touched → `take_profit`;
   6. otherwise update the peak.
   If nothing hits, exit at the close of the last candle before the deadline, reason `time_stop`.
6. **Blending**: a moon bag is valued at the close of the last candle in the 24-hour path for every exit reason; a scale-out leg is blended in at its fill price. Costs (`--side-cost`, default 3% per side, 5.83% round trip at a flat price) are applied to the blended exit.
7. **Benchmarks**: a plain 30-minute hold, and price snapshots at migration, +1 m, +5 m, +30 m, +2 h, +24 h.

Flags: `--take-profit 0.75`, `--stop-loss 0.30`, `--trailing-stop 0`,
`--moon-bag 0`, `--scale-out-at 0`, `--scale-out-fraction 0.5`,
`--max-entry-runup 0`, `--time-stop-minutes 30`, `--side-cost 0.03`,
`--entry-delay-seconds 30`, `--requests-per-minute 9`, `--limit`, `--refresh`,
`--include-needs-review`, `--input`, `--output-dir`.

### 8.3 Outputs

| File | Contents |
|---|---|
| `trade_results.csv` | one row per token: mint, graduation and entry timestamps and prices, exit timestamp/price/reason, gross and net return, same-candle ambiguity flag, moon bag fraction and price, scale-out price |
| `hold_30m_results.csv` | the hold benchmark |
| `price_snapshots.csv` | the six snapshots per token |
| `errors.csv` | tokens that failed pool discovery, pricing, or the run-up filter, with the reason |
| `summary.json` | median, mean, win rate, sum; top-five concentration of profit and the stats with the top five removed; strategy vs hold; the three yes/no questions (median positive after costs, beats hold, positive without the top five); ambiguous-bar count; all parameters |
| `cache/<mint>.json` | pool address, dex id, minute path, entry candles |

All four CSVs are rewritten after every token, so a long run can be stopped
and resumed.

Caveats: GeckoTerminal candles are aggregated market data, not executable
quotes for your size. Empty intervals are filled from the previous close by
the API. A token with missing early candles fails rather than silently using a
distant price. Survivorship and data-availability bias remain possible.

### 8.4 `optimize.py`

```bash
python optimize.py
python optimize.py --take-profits 0.5,0.75 --moon-bags 0,0.1 --max-entry-runup 2.0
```

Sweeps the grid (defaults: take-profit 0.4/0.5/0.75/1.0/1.5, stop-loss
0.2/0.3/0.4, time stop 15/30/60, trailing 0/0.2/0.3, moon bag 0/0.15, scale-out
0/0.4; 486 valid combinations) against every cached token with zero API calls.
The sample is split chronologically, 70% train / 30% validation. Combos are
ranked on train by median net return (mean as tiebreak); the top five are then
scored on the validation tokens the sweep never saw, next to the baseline
(TP 0.75 / SL 0.30 / 30 m). The verdict flags overfitting when the train
median exceeds validation by more than 5 points. It refuses to run on fewer
than 30 cached tokens and warns under 50 in train. Output
`optimize_summary.json`.

Judge combos by the validation column. Parameters are never tuned on a handful
of charts: the tokens you noticed are the ones that moved.

### 8.5 `position_sizing.py`

```bash
python position_sizing.py --balance 100 --fixed-fee-per-side 0.10 --min-position 5
```

Bootstraps 2,000 paths of 200 trades each (with replacement) from
`trade_results.csv` for fractions 2% to 50% of balance, charging a fixed USD
fee per side on top of the proportional costs already inside `net_return`.
Per fraction: median, 5th and 95th percentile final balance, median max
drawdown, ruin rate (balance below `--ruin-threshold`, default $5), median log
growth, and how often the `--min-position` floor bound. Recommends the
fraction with the best median log growth among those with ruin rate at or
below `--max-ruin-rate` (5%). Refuses on fewer than 20 trades and warns when
the mean net return is not positive, because no sizing fixes a negative edge.
Output `sizing_summary.json`, which the dashboard picks up as its position
size. Trade half the recommendation live at first.

---

### 8.6 Market-making research track (`mm/`)

A separate lane that evaluates the alternative in the market-making brief: selective
Meteora DLMM liquidity provision and cost-aware momentum on *established* memes (30+ days,
$100K+ pools), instead of fresh graduates. It shares `DATA_DIR` and the wallet with the
executor and prefixes every file `mm_`.

```bash
python -m mm costs        # break-even table
python -m mm screen       # universe + reject log
python -m mm record --hours 6
python -m mm replay       # every strategy, full cost accounting
python -m mm paper --hours 168
python -m mm live         # real DLMM ranges + Jupiter swaps for adaptive_dlmm and momentum
```

In copy-only deployments (`COPY_WALLETS` set) the lane stays off unless `MM_AUTOSTART=1` is set explicitly: its universe refresh quotes dozens of tokens through Jupiter's keyless tier and starved the copy lane. On Railway, `MM_AUTOSTART=1` with `MM_MODE=paper|live` launches the lane next to the
executor; `/api/mm/status`, `/api/mm/stop` and `/api/mm/panic` control it. Live mode needs
the Node sidecar (`mm/sidecar`, built into the image) and `WALLET_PRIVATE_KEY`; capital is
capped by `MM_BANKROLL_USD`. Design, the edge gate, the inventory rule, live execution and
kill switches are documented in [`mm/README.md`](mm/README.md).

## 9. Deployment

### Railway (dashboard + executor, recommended)

1. New project → Deploy from GitHub repo → this repo. The root `Dockerfile` builds `grad-backtest/` with no Root Directory setting.
2. **Variables**: `MIGRATION_ADDRESS`, `ADMIN_TOKEN` (RPC endpoints are built in; optionally override with `RPC_URL` or `RPC_URLS`), then the executor variables you want to change from their defaults (section 4). For live trading: `EXECUTOR_MODE=live`, `EXECUTOR_AUTOSTART=1`, `WALLET_PRIVATE_KEY` (mark it sealed). `HELIUS_API_KEY` is only needed for historical collection or the optional Helius compatibility mode.
3. **Volume** mounted at `/data`. Without it every deploy starts with an empty filesystem: the OHLCV cache, results, trade history, and the executor's state (open positions' entry prices and peaks, moon bags) are lost, and the restart has to adopt whatever is in the wallet at current value.
4. **Networking → Generate Domain** for the public dashboard.
5. Every push to `main` redeploys, which restarts the executor. Each restart costs several minutes of not trading plus the state loss above. Set **Settings → Watch Paths** to something like `/grad-backtest/**/*.py`, `/grad-backtest/static/**`, `/Dockerfile` so documentation-only commits do not redeploy.

`railway.json`: Dockerfile builder, restart on failure up to 5 times,
healthcheck at `/healthz`.

### Railway batch mode

Set `BACKTEST_COMMAND` (e.g. `run --limit 5`) and the container runs that once
and exits. Set the restart policy to Never or Railway will re-run it forever,
and ignore the healthcheck. Results land on the volume.

### GitHub Actions

`.github/workflows/backtest.yml` (Actions → "Run backtest in the cloud") runs
`collect`, `run`, or both with `HELIUS_API_KEY` and `MIGRATION_ADDRESS` from
repository secrets, caches `data/cache` and `graduations.csv` between runs,
and uploads the results as an artifact. Jobs are capped at 6 hours.
`.github/workflows/tests.yml` runs the unit tests on every push and pull
request that touches `grad-backtest/`.

### Any container host

```bash
docker build -t grad-backtest .
docker run --rm -p 8000:8000 -e RPC_URL=https://provider.example/key -e MIGRATION_ADDRESS=… -v "$PWD/data:/data" grad-backtest
```

---

## 10. Tests

```bash
cd grad-backtest && python -m unittest -v
```

| File | Covers |
|---|---|
| `test_grad_backtest.py` | candle selection, cost math, simulate_trade barrier ordering |
| `test_scale_out.py` | scale-out in the simulator and the executor |
| `test_optimize.py` | dataset loading, split, grid, run-up filter |
| `test_position_sizing.py` | fee math, ruin detection, min-position floor, CLI end to end |
| `test_server.py` | endpoints, admin gating, job launching |
| `test_executor.py` | decide_exit, sizing, trailing stop |
| `test_executor_guards.py` | lateness, price impact, skip and trade CSV writing |
| `test_slippage_retry.py` | entry retry, error classification |
| `test_market_cap.py` | implied market cap math and ceiling |
| `test_bundle_guards.py` | floor, curve age, holder concentration, RPC helpers, try_enter integration |
| `test_reconcile.py` | account close/burn transactions, adoption, stuck handling, panic |
| `test_rate_limits.py` | 429 backoff, price cache, adopted slots, blockhash retry |
| `test_moon_bag_target.py` | winners-only, target multiple, dead-bag burn, minimum size, redaction |
| `test_scout.py` | wallet scouting: episodes, gates, leaderboard evidence, clustered bootstrap, relationship clusters, promotion switch and budget, one live wallet per cluster, elite-only gate, demotion keeps sells followed, shadow trades never swap, separate budgets, restart safety, scout failures never reach position management |

---

## 11. Function-by-function reference

### 11.1 `executor.py`

Module constants: `DATA_DIR`, `STATE_FILE`, `TRADES_FILE`, `LOG_FILE`,
`STOP_FLAG`, `PANIC_FLAG`, `USDC`, `LAMPORTS`, `TOKEN_PROGRAM`,
`TOKEN_2022_PROGRAM`, `TOKEN_ACCOUNT_RENT_SOL`, `SYSTEM_PROGRAM`, `SKIPS_FILE`,
`TRADE_COLUMNS`, `RATE_LIMIT_BACKOFF`.

Module functions:

- `now_ts()` — single source of "now".
- `is_rate_limited(exc)` — true for HTTP 429.
- `with_backoff(fn, what)` — retry `fn` on 429 with the backoff schedule; re-raise anything else.
- `utc_iso(epoch=None)` — ISO-8601 UTC with `Z`.
- `log(message)` — timestamp, print, append to `executor.log`.
- `load_state(cfg)` / `save_state(state)` — read the state file or build a fresh one; write atomically, trimming `seen_signatures` to 500.
- `roll_daily(state)` — reset the daily P&L on a new UTC date.
- `record_trade(row)` — append to `live_trades.csv`. A file with an older header (missing columns now recorded) is rotated to `live_trades.<timestamp>.csv` first; a file with extra columns keeps its own header.
- `record_skip(mint, reason, meta)` — append to `skips.csv` with the entry metadata known at the time; same rotation rule.
- `quote_price_impact_pct(quote)` — Jupiter's fraction as a percent.
- `describe_error(exc)` — redact API keys, name Jupiter 6001/6000, truncate.
- `entry_market_cap_usd(size_usd, out_amount_raw, supply_ui, decimals, supply_raw=...)` — executable quote-implied FDV; production uses raw supply so decimals cancel.
- `entry_guard_reason(cfg, graduated_ts, now, impact, market_cap, curve_age, top_holder_pct)` — the six ordered guards; `None` inputs never block.
- `position_size_usd(cfg, equity_usd, open_positions, daily_pnl)` — sizing with concurrency, daily-loss, min, max, and equity checks.
- `decide_exit(entry_usd, current_usd, opened_ts, now, cfg, peak_usd)` — take-profit, stop-loss, trailing, time stop.
- `main()` — `--print-config` or run.

`class Config` — reads every variable (section 4); `validate()` enforces the required ones.

`class Rpc` — JSON-RPC client with backoff: `call`, `sol_balance`, `token_balance`, `token_accounts(owner, mint=None)`, `token_supply`, `mint_first_seen(mint, stop_before_ts, max_pages=3)`, `top_wallet_holder(mint, exclude)`, `send_raw`, `confirmed(signature, timeout_s=60)`.

`class Jupiter` — `quote(input, output, amount, slippage_bps=None)` and `swap_transaction(quote, pubkey)`, both with backoff; the swap is built with wrap/unwrap SOL, dynamic compute limit, and automatic priority fee.

`class Wallet` — live-only signer: `sign(raw_tx)` for Jupiter's versioned transactions; `sign_instructions(instructions, blockhash)` for burn/close transactions.

`class Executor` — `sol_price_usd` (30 s cache), `equity_usd`, `poll_graduations`, `execute_swap`, `skip`, `entry_metadata`, `try_enter`, `enter_with_retry`, `close_position`, `_record_close`, `manage_positions`, `scale_out`, `close_token_account(account, program, mint, burn_amount=0)`, `reclaim_rent(mint)`, `reconcile_wallet(sol_price)`, `sell_bag(bag, key, reason, sol_price, quote=None)`, `manage_moon_bags(sol_price)`, `burn_dead_bag(bag, value)`, `liquidate_bags(sol_price, key, reason)`, `run`.

### 11.2 `server.py`

Helpers: `read_csv`, `read_json`, `file_mtime`, `frame_records`,
`equity_curve(trades, fraction)` (balance += balance × fraction × net_return −
2 × fixed fee, clamped at zero). Endpoints as in section 7. Internals:
`_run_job(argv, stage)` runs the subprocess in a daemon thread appending to
`run.log` with start/finish markers; `_require_admin(request)`;
`_executor_running()`; `_start_executor()`; `maybe_autostart_executor()`
startup hook.

### 11.3 `grad_backtest.py`

Constants: `DATA_DIR`, `WSOL`, `USDC`, `USDT`, `KNOWN_QUOTES`, `GECKO_BASE`,
`HELIUS_BASE`, `SUPPORTED_DEX_HINTS`. Functions: `utc_iso`, `parse_timestamp`,
`candidate_mints(tx)`, `collect_graduations(args)`, `price_at_or_after(candles,
target, tolerance)`, `apply_costs(entry, exit, side_cost)`, `simulate_trade(…)`,
`snapshot_rows`, `make_summary`, `run_backtest(args)`, `build_parser`, `main`.
Classes: `ApiClient` (paced GET with retry on 429/5xx), `GeckoTerminal`
(`select_pool`, `ohlcv`, `minute_path`, `entry_candles`), dataclasses `Candle`
and `TradeResult`.

### 11.4 `optimize.py`

`parse_grid`, `load_dataset(input_csv, cache_dir, entry_delay,
max_entry_runup=0)`, `evaluate(dataset, combo, side_cost)`, `main`.

### 11.5 `position_sizing.py`

`trade_pnl(position, net_return, fixed_fee_per_side)`, `simulate_path(…)`,
`evaluate_fraction(returns, fraction, args, rng)`, `fee_reality(args,
side_cost)`, `main`.

---

## 12. Behavioural notes, limitations, and lessons from live trading

Things that are true of the code and easy to get wrong:

- Position valuation uses the buy slippage setting for its quote; real sells use the sell setting. The recorded exit value comes from the sell quote.
- The daily loss limit measures quote-based closed P&L, resets at UTC midnight, and is reset by a restart. It never fired during the overnight session because of the restarts.
- Detection uses WebSocket signature hints plus a configurable 30-second, 10-signature RPC catch-up. A provider outage longer than the catch-up window can still miss launches, which is safer than buying stale ones.
- Pending graduations are persisted; a restart retains them but discards any that exceed the lateness window.
- Adopted positions have no entry price or peak history; they are managed from their adoption value.
- `record_trade` and `record_skip` rotate a file whose header predates the current columns (`<name>.<timestamp>.csv`), so a redeploy with new columns starts a fresh file and the dashboard shows only rows since then.
- The dashboard's paper-balance colour compares against a hard-coded 100, not `START_BALANCE`.
- GeckoTerminal's 30-second candle endpoint may require a paid plan from some networks (a 401 was observed), in which case the backtest's entry step fails for every token.

What the live sessions taught, in order:

1. Three percent slippage rejected most swaps on pools seconds old (Jupiter 6001). Buys now use 10%, sells 15%, with retries.
2. A confirmation timeout does not mean the swap failed. Entries check the wallet before giving up.
3. Restarts without a volume orphaned six positions and cost more than the strategy did. Reconciliation now adopts them, but the volume is still the real fix.
4. Fifty-nine empty token accounts held 0.12 SOL of rent. Every full sell now closes its account.
5. Bundled launches (creator buys the curve, graduates in seconds, dumps into the pool) were the dominant loser. The floor, curve-age, and holder guards target that profile; the market-cap ceiling turned out to block the best-performing bucket and is now set high.
6. Adopted dust bags filled every entry slot after a restart. They no longer count.
7. RPC providers and Jupiter rate-limit startup bursts. RPC calls fail over across configured providers; entry discovery backs off if all providers are unavailable, and the SOL price is cached.
8. Moon bags kept on losers just lock rent. Bags are winners-only, have a minimum size, a target multiple, and a dead-bag burn.

---

## 13. Security

- **Never** paste a private key or seed phrase into chat, a commit, an issue, or this README. The key goes only in the `WALLET_PRIVATE_KEY` environment variable, ideally sealed.
- Use a dedicated burner wallet holding only what you can lose. The executor signs anything Jupiter builds for the configured wallet.
- `MIGRATION_ADDRESS` is public. `ADMIN_TOKEN` is a password you invent; anyone with it can start the executor or panic-sell your positions.
- Error text redacts `api-key` query parameters, but logs still contain wallet addresses and trade details. Treat the dashboard's numbers as public once deployed.
- `--print-config` omits the wallet key; nothing else in the codebase prints it.
