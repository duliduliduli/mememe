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
`MIN_SOL_RESERVE` kept for fees, and `SLIPPAGE_BPS` (300) on every swap.

Entry guards, applied before every buy:

- **Sellability** — a reverse (sell) route must exist for the token, or the
  entry is skipped as a possible honeypot.
- **Price impact** — entries with quoted impact above `MAX_PRICE_IMPACT_PCT`
  (default 5%) are skipped: the pool is too thin for our size and the real
  round-trip cost would eat the trade.
- **Staleness** — an entry more than `MAX_ENTRY_LATENESS_SECONDS` (60s) past
  its target time is skipped; a late entry is not the trade the backtest models.

Every skipped opportunity is recorded to `skips.csv` with its reason, and each
trade records its quoted entry price impact, so filters can be tuned from data.

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
