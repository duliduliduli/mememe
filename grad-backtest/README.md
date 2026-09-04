# Graduation Backtest Starter

Research pipeline for Pump.fun tokens after migration to PumpSwap or Raydium:

1. Collect graduation candidates from a verified migration address with Helius.
2. Discover each token's migration pool and fetch historical OHLCV from GeckoTerminal.
3. Estimate entry at migration +30 seconds, then simulate +75% TP / -30% SL / 30-minute time stop.
4. Charge configurable costs on **both** entry and exit and write an audit-friendly summary.

This is research software, not investment advice or a production trading executor.

## Install

```bash
cd grad-backtest
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

Set the credentials without putting secrets in the code:

```bash
export HELIUS_API_KEY='your_key'
export MIGRATION_ADDRESS='current_verified_migration_authority'
```

The migration address is deliberately not hard-coded because Pump.fun has changed its migration path over time. Verify the address for the exact venue and week you are testing.

## Step 2: collect graduations

```bash
python grad_backtest.py collect \
  --count 500 \
  --start-time 2026-08-01T00:00:00Z \
  --end-time 2026-08-08T00:00:00Z
```

This writes `data/graduations.csv`. Transactions containing exactly one unknown non-quote mint are marked `confirmed`; transactions with multiple candidate mints are marked `needs_review`. Review those signatures by hand. The pricing command skips them unless `--include-needs-review` is supplied.

If you obtain a cleaner graduation list from Bitquery or another source, skip collection and provide a CSV containing:

```csv
mint_address,graduation_timestamp
TOKEN_MINT,2026-08-01T12:00:00Z
```

## Steps 3-4: prices and trade simulation

Start with five rows to validate the setup:

```bash
python grad_backtest.py run --limit 5
```

Then run the full file:

```bash
python grad_backtest.py run
```

Defaults:

- entry delay: 30 seconds
- take profit: +75%
- stop loss: -30%
- time stop: 30 minutes
- friction: 3% on entry and 3% on exit (5.83% loss if price is flat)
- API pace: 9 requests/minute for GeckoTerminal's keyless endpoint

Example sensitivity runs:

```bash
python grad_backtest.py run --output-dir data/cost_2pct --side-cost 0.02
python grad_backtest.py run --output-dir data/cost_4pct --side-cost 0.04
```

## Outputs

- `trade_results.csv`: strategy result per token
- `hold_30m_results.csv`: simple 30-minute hold comparison
- `price_snapshots.csv`: migration, +1m, +5m, +30m, +2h, +24h
- `summary.json`: median, mean, win rate, top-five concentration, top-five removal, and TP/SL vs hold
- `errors.csv`: skipped tokens and reasons
- `cache/`: resumable raw normalized candles per mint

## Methodology caveats

- GeckoTerminal OHLCV is aggregated market data, not executable quotes for your order size. The cost setting is only a coarse slippage/fee model.
- Entry uses the open of the first available 30-second candle at or after the +30-second target. It records that candle timestamp so latency can be audited.
- A one-minute candle does not reveal whether its high or low occurred first. When both TP and SL are crossed in the same candle, the backtest assumes the stop loss happened first and flags the row.
- Empty intervals are filled from the previous close by the API. Tokens with missing early candles fail instead of silently using a distant price.
- Pool discovery only accepts Pump/Raydium pools created within six hours of the migration timestamp.
- Survivorship and data-availability bias remain possible. Check failed/missing tokens, not just successful rows.
- At the public keyless limit, 500 tokens can take hours. The cache lets you stop and resume safely.

## Test

```bash
python -m unittest -v
```

## Parameter optimizer ("training" done honestly)

After one full `run` has populated the cache, `optimize.py` sweeps the whole
parameter grid — take-profit, stop-loss, time stop, trailing stop, moon bag —
against every cached token, with zero API calls:

```bash
python optimize.py                      # defaults: 270 combos
python optimize.py --take-profits 0.5,0.75 --moon-bags 0,0.1,0.2
```

It splits the sample chronologically (default 70% train / 30% validation),
ranks combos by train median net return, then reports how the winners perform
on the validation tokens the sweep never saw. **Judge combos by the validation
column.** A big train-vs-validation gap is the overfitting alarm — it means
the "winning" parameters memorized the past instead of finding an edge. This
is also why parameters are never tuned on a handful of hand-picked charts:
the tokens you noticed are the ones that moved.

Runs in the cloud via the dashboard: `{"stage": "optimize"}` (results land in
`optimize_summary.json` on the volume).

## Position sizing for a small account

`position_sizing.py` answers "what fraction of my balance should each trade use?"
from your actual backtest results instead of guesswork:

```bash
python position_sizing.py --balance 100 --fixed-fee-per-side 0.10
```

It bootstraps thousands of simulated trading sequences from
`data/trade_results.csv` for a grid of account fractions and reports the median
outcome, the 5th-percentile outcome, drawdown, and risk of ruin per fraction,
then recommends the fraction with the best median growth that keeps ruin risk
under 5% (writes `data/sizing_summary.json`).

Why fraction depends on account size at $100:

- Proportional costs (DEX fee + slippage, the `--side-cost` in the backtest)
  are the same at any size: at 3%/side you lose ~5.8% round trip on a flat price.
- Fixed costs (Solana base fee + priority fee/tip) do not shrink with position
  size. At ~$0.10/side, a $10 position pays an extra 2% round trip; a $50
  position pays 0.4%. That puts a hard floor under viable trade size, which is
  why `--min-position` exists and the report shows how often the floor binds.
- Bigger fractions grow faster when the edge is real, but variance and ruin risk
  explode past the Kelly point. The grid makes that trade-off visible.

To tune profit-taking together with sizing, run TP/SL sensitivity passes and size
each one — pick the combination whose sizing report has the best risk-adjusted
growth, not just the best median:

```bash
python grad_backtest.py run --output-dir data/tp50 --take-profit 0.50
python grad_backtest.py run --output-dir data/tp100 --take-profit 1.00
python position_sizing.py --input data/tp50/trade_results.csv --output data/tp50/sizing.json
python position_sizing.py --input data/tp100/trade_results.csv --output data/tp100/sizing.json
```

Caveats: if the mean net return per trade is not positive, the tool says so and
refuses to recommend — no sizing or TP level fixes a negative edge. Backtest
results overstate live performance (latency, slippage on real order sizes,
survivorship), so trade half the recommended fraction at first. And this repo
contains no live executor: never commit or paste wallet private keys anywhere;
if you later automate execution, use a dedicated burner wallet holding only what
you can lose, with the key supplied as a runtime environment variable.

## Live executor (paper by default)

`executor.py` trades the strategy in real time: it polls Helius for new
graduations, enters 30s after migration via Jupiter, and manages each position
against TP/SL/time-stop using executable Jupiter sell quotes. Two modes:

- **`EXECUTOR_MODE=paper`** (default): real detection, real quotes, simulated
  fills. No wallet, no key, no risk. Run this first — for days, not minutes —
  and judge the results on the dashboard's Live panel.
- **`EXECUTOR_MODE=live`**: signs and sends real swaps. Requires
  `WALLET_PRIVATE_KEY` — a **burner wallet's** exported private key (Phantom:
  account settings → Show private key for that one account). NEVER your seed
  phrase, never your main wallet, never more money than you can lose entirely.

Safety rails enforced in both modes: `ACCOUNT_FRACTION` (default 10%) capped by
`MAX_POSITION_USD` (default $20), `MAX_CONCURRENT_POSITIONS` (2),
`DAILY_LOSS_LIMIT_USD` (default $30 — halts new entries until next UTC day),
`MIN_SOL_RESERVE` kept for fees, and slippage caps on every swap (see below).

Slippage and retries (learned live): a pool that is seconds old moves several
percent in the ~1s between quoting and executing, and a 3% tolerance rejected
most swaps with Jupiter error 6001. Defaults are now `SLIPPAGE_BPS=1000` for
buys and `SELL_SLIPPAGE_BPS=1500` for sells — sells get more room because a
rejected sell in a falling market is the worst available outcome. A tolerance
is a ceiling, not a cost: fills still happen at market, the setting only
decides how far the price may move before the swap is refused. Entries that
fail are re-quoted and retried `ENTRY_RETRIES` times (default 2, `ENTRY_RETRY_SECONDS`
apart); every guard re-runs on each attempt, so the staleness window still
bounds how late an entry can land. Swap failures are logged as one readable
line (e.g. `Jupiter 6001: slippage tolerance exceeded`) instead of the raw
simulation dump.

Entry guards, applied before every buy:

- **Sellability** — a reverse (sell) route must exist for the token, or the
  entry is skipped as a possible honeypot.
- **Price impact** — entries with quoted impact above `MAX_PRICE_IMPACT_PCT`
  (default 5%) are skipped: the pool is too thin for our size and the real
  round-trip cost would eat the trade.
- **Market cap ceiling** — entries with an implied market cap above
  `MAX_ENTRY_MARKET_CAP_USD` (default $300,000) are skipped. Pump.fun tokens
  graduate near $69k and genuine ones sit around $30k–200k at entry; a token at
  $900k–$150M thirty seconds after migration was pumped by a bundled buy before
  we arrived, and that buyer dumps into whoever follows. The cap is computed
  from the actual buy quote (USD in ÷ tokens out × circulating supply, one
  `getTokenSupply` RPC call), so it reflects the price we would really pay. If
  the supply lookup fails the entry proceeds rather than blocking on metadata.
  The backtest equivalent is `--max-entry-runup` (skip tokens whose entry price
  is more than that fraction above the graduation price); `optimize.py` accepts
  the same flag as a dataset filter.
- **Market cap floor** — entries with an implied market cap below
  `MIN_ENTRY_MARKET_CAP_USD` (default $25,000) are skipped. Graduation is
  about $69k, so a token far below that a minute later was already dumped into
  its own pool. The case that motivated it: SOLL's creator sold 78% of supply
  24 seconds after migration and the bot then bought at a $450 cap; overnight,
  entries under $30k went 1 for 7. Uses the same supply lookup as the ceiling.
- **Curve age** — a token that graduated less than `MIN_CURVE_AGE_SECONDS`
  (default 120) after it was created is skipped. Filling a whole bonding curve
  in seconds takes one buyer, which is the definition of a bundle (SOLL:
  created to graduated in 29 seconds, six buyers). Creation time comes from the
  mint's signature history (`getSignaturesForAddress`, usually one call; the
  scan stops as soon as it sees a transaction older than the threshold, and a
  token too busy to conclude within three pages is treated as unknown, never
  rejected).
- **Holder concentration** — if the largest plain-wallet holder owns more than
  `MAX_TOP_HOLDER_PCT` (default 20%) of supply, the entry is skipped and the
  wallet is named in the skip reason. Program-owned accounts (the AMM pool,
  bonding curve, Mayhem vault) are not counted because they cannot dump on us;
  our own wallet is excluded. Three RPC calls (`getTokenLargestAccounts`, then
  the token accounts' owners, then whether each owner is a wallet or a
  program). SOLL's creator held 59% at graduation.
- **Staleness** — an entry more than `MAX_ENTRY_LATENESS_SECONDS` (60s) past
  its target time is skipped; a late entry is not the trade the backtest models.

Every one of the metadata guards fails open: if its lookup errors or times
out, a `WARN` is logged and that check is skipped for the entry, so an RPC
hiccup can neither block trading nor be mistaken for a clean token. Each
position and trade row records `entry_market_cap_usd`,
`entry_curve_age_seconds`, and `entry_top_holder_pct` so the guards can be
tuned from real outcomes later. Set any threshold to `0` to disable it.

Every skipped opportunity is recorded to `skips.csv` with its reason, and each
trade records its quoted entry price impact, so filters can be tuned from data.

### Trailing stop (off by default — backtest it first)

Many rugs bleed downward for minutes before the liquidity pull. A trailing stop
("exit when price falls X% from its post-entry peak") sells that fade instead of
riding it to the time stop. Both layers support it:

```bash
# Measure it against your collected sample before using it live:
python grad_backtest.py run --output-dir data/trail25 --trailing-stop 0.25
python grad_backtest.py run --output-dir data/trail35 --trailing-stop 0.35
python position_sizing.py --input data/trail25/trade_results.csv --output data/trail25/sizing.json
```

Then, if the numbers beat the plain TP/SL/time-stop run, set `TRAILING_STOP`
(e.g. `0.25`) on the executor. `0` (default) disables it. Tune it from the
backtest sample, not from one chart — a single example proves the mechanism,
not the parameter.

### Restart safety, rent reclaim, and stuck positions (live mode)

Learned from a night where restarts stranded six positions and 59 empty token
accounts held ~0.12 SOL of rent:

- **Wallet reconciliation at startup.** The executor scans every token account
  the wallet holds. Untracked holdings worth at least `MIN_ADOPT_USD` (default
  $1) are adopted as managed positions with basis = current value and the clock
  starting now, so a restart can never strand a bag again. Holdings under the
  threshold are left untouched (nothing the wallet held before the bot is ever
  burned), and quote tokens are ignored.
- **Rent reclaim.** Empty token accounts are closed at startup, and each full
  sell closes its account afterwards (burning any dust first — only on tokens
  the bot bought). Each close returns ~0.002 SOL. `CLOSE_EMPTY_ACCOUNTS=0`
  disables both.
- **Stuck positions.** A position whose sells keep failing for
  `STUCK_AFTER_MINUTES` (default 15) past its time stop, with at least three
  consecutive failures, is moved to `state.stuck` so it stops blocking a slot.
  It stays visible on the dashboard, `panic` still tries to liquidate it, and
  the log says to sell it manually.

### Scale-out / partial take-profit (off by default — backtest it first)

A position that reaches +40% and then rugs is worth nothing under a single
+75% take-profit. A scale-out banks part of the winner at a first target and
lets the rest ride to the full take-profit under the same rules:

```bash
python grad_backtest.py run --output-dir data/scale40 --scale-out-at 0.40 --scale-out-fraction 0.5
python optimize.py            # the sweep now includes --scale-out-ats 0,0.4
```

On the executor, `SCALE_OUT_AT=0.40` and `SCALE_OUT_FRACTION=0.5` sell half at
+40%; the remainder keeps the same take-profit, stop-loss and trailing rules on
its reduced cost basis, which leaves every threshold at the same token price.
Each scale-out is recorded in `live_trades.csv` as its own `scale_out` row.
The cost is a second sell leg per winner (extra fixed fees, which matter at
$5 positions) and a smaller share riding to the full target when a winner
keeps running. Whether that trade-off pays is exactly what the optimizer
measures; the WTF-shaped reversal it protects against is common enough that
it is worth measuring early.

Every closed trade now also records `peak_gain_pct`, the highest value the
position reached before exit, so a postmortem can see "peaked at +41%, exited
at −88%" directly from the trade log.

### Moon bag (off by default — backtest it first)

Some tokens dump past our exit and then rerun hours or days later. A moon bag
keeps a fraction of each position at the primary exit instead of selling all
of it. Backtest it — the simulation sells the kept fraction at the ~24h mark:

```bash
python grad_backtest.py run --output-dir data/mb15 --moon-bag 0.15
python grad_backtest.py run --output-dir data/mb15_trail --moon-bag 0.15 --trailing-stop 0.25
```

If it wins across the sample, set `MOON_BAG` (e.g. `0.15`, capped at `0.5`) on
the executor: each exit sells the rest and parks the kept tokens in the state
file's `moon_bags` list (shown on the dashboard's Live panel). Moon bags are
not actively managed; `panic` liquidates them along with everything else. Note
the cost: on stop-loss exits the kept fraction usually rides to ~zero, so the
24h-sale backtest number is the honest measure of whether the occasional
rerun pays for all the bags that die.

Control it through the dashboard API (all require the `x-admin-token` header):

```bash
curl -X POST .../api/executor/start -H "x-admin-token: $TOK"   # begin trading
curl -X POST .../api/executor/stop  -H "x-admin-token: $TOK"   # drain: no new buys, manage open positions
curl -X POST .../api/executor/panic -H "x-admin-token: $TOK"   # sell everything at market NOW
```

Set `EXECUTOR_AUTOSTART=1` on Railway so the executor restarts with the
container and resumes managing any open positions from `executor_state.json`.
Closed trades land in `live_trades.csv` and the dashboard shows a Live panel
(balance/PnL, open positions, closed trades, executor log) whenever the
executor has activity. Trades happen on the Solana blockchain via Jupiter
(`JUPITER_BASE_URL`, default `https://lite-api.jup.ag/swap/v1`); tokens land
at the wallet's address and are visible in any explorer.

Honest expectations: the backtest exists to tell you whether this strategy has
an edge. Running live before the backtest says yes means the safety rails are
limiting how fast you can lose, not making you money.

## Dashboard

`server.py` serves a public, read-only web dashboard over the result files:
simulated equity curve, win rate, net-return distribution, recent trades,
skipped tokens, and a live activity log of the running job. Run it locally:

```bash
python server.py            # http://localhost:8000
```

Environment knobs:

- `DATA_DIR` — where results live (default `data`)
- `START_BALANCE`, `FIXED_FEE_PER_SIDE`, `ACCOUNT_FRACTION` — parameters of the
  simulated equity curve shown on the dashboard; the fraction is overridden
  automatically by the recommendation in `data/sizing_summary.json` when present
- `ADMIN_TOKEN` — when set, `POST /api/run` (header `x-admin-token`) can launch
  `collect`, `run`, or `sizing` jobs in the background; without it, job
  launching is disabled and the dashboard is purely read-only

Everyone who can reach the URL can see your numbers — the dashboard exposes no
credentials, but treat the performance data itself as public once deployed.

## Running in the cloud

### GitHub Actions (no infrastructure needed)

The repo ships a manual workflow at `.github/workflows/backtest.yml`.

1. In the GitHub repo, go to **Settings → Secrets and variables → Actions** and add
   `HELIUS_API_KEY` and `MIGRATION_ADDRESS` as repository secrets.
2. Go to **Actions → "Run backtest in the cloud" → Run workflow**.
3. Pick a stage (`collect`, `run`, or `collect-then-run`) and optionally pass extra
   flags such as `--limit 5 --side-cost 0.02`.
4. When the job finishes, download `backtest-results-*` from the run's **Artifacts**
   section — it contains `trade_results.csv`, `summary.json`, and the rest.

The OHLCV cache and the collected `graduations.csv` are persisted between workflow
runs via the Actions cache, so you can `collect` once and then do several `run`
sensitivity passes. GitHub-hosted jobs are capped at 6 hours; at the keyless
GeckoTerminal rate a full 500-token run fits, but use `--limit` first to validate.

### Railway (public dashboard + job runner)

The folder includes a `Dockerfile` and `railway.json`. By default the container
serves the dashboard; it can also launch backtest jobs itself, so one Railway
service does everything.

1. Create a new Railway project → **Deploy from GitHub repo** and pick this repo.
2. In the service settings, set **Root Directory** to `grad-backtest`.
3. Add environment variables:
   - `HELIUS_API_KEY` and `MIGRATION_ADDRESS` (needed for `collect` jobs)
   - `ADMIN_TOKEN` — a long random string; required to launch jobs via the API
   - optionally `START_BALANCE`, `FIXED_FEE_PER_SIDE`, `GECKO_REQUESTS_PER_MINUTE`
4. Attach a **Volume** mounted at `/data` (the container's `DATA_DIR` default)
   so collected CSVs, the OHLCV cache, and results survive restarts and
   redeploys — without it, every redeploy starts from an empty filesystem.
5. Under **Settings → Networking**, click **Generate Domain** — that's your
   public dashboard URL.
6. Kick off jobs from anywhere:

```bash
curl -X POST https://YOUR-APP.up.railway.app/api/run \
  -H "x-admin-token: $ADMIN_TOKEN" -H "content-type: application/json" \
  -d '{"stage": "collect", "extra_args": "--count 500"}'
curl -X POST ... -d '{"stage": "run", "extra_args": "--limit 5"}'
curl -X POST ... -d '{"stage": "sizing"}'
```

The dashboard refreshes itself every 15 seconds and shows job progress live.

Before the first full batch, do a dress rehearsal: launch
`{"stage": "run", "extra_args": "--limit 5"}` and confirm trades appear on the
dashboard. That proves env vars, both APIs, and volume writes end to end —
much cheaper than discovering a bad API key three hours into a 500-token run.
The cache is resumable, so those five tokens aren't wasted work.

### Railway batch mode (no dashboard)

Set `BACKTEST_COMMAND` (e.g. `run --limit 5`, then `run` for the real batch)
and the container executes that once and exits instead of serving the
dashboard. Two extra settings matter in this mode:

- **Restart Policy → Never** (service settings). Railway treats a clean exit
  as a crash by default and would re-run the backtest forever, burning your
  Helius/GeckoTerminal quota. (`railway.json` ships ON_FAILURE for the
  dashboard mode, so override it in the UI for batch.)
- Remove or ignore the healthcheck — there is no HTTP server to probe.

Results land on the volume; read them with `railway ssh` and
`cat /data/summary.json`, or flip back to dashboard mode (unset
`BACKTEST_COMMAND`, redeploy) and view the same volume through the web UI.

The same image works on any container host (Fly.io, Cloud Run jobs, a plain VPS):

```bash
docker build -t grad-backtest grad-backtest/
docker run --rm -e HELIUS_API_KEY=... -e MIGRATION_ADDRESS=... \
  -e BACKTEST_COMMAND="run --limit 5" -v "$PWD/data:/app/data" grad-backtest
```
