# Market-making research track (`mm/`)

A second strategy lane living next to the graduation executor. It answers the question in
the market-making brief: can selective liquidity provision on *established* Solana memes,
or a cost-aware momentum strategy on the same universe, beat the graduation sniper on
net-liquidation PnL after every cost?

It implements the brief's build sequence up to and including the forward paper engine.
It does not, and cannot, sign transactions. See "What is deliberately not built" below.

| Phase | Brief deliverable | Module | Status |
|---|---|---|---|
| 1 | Data recorder and pool screener | `screener.py`, `recorder.py`, `sources.py` | built, verified against live APIs |
| 2 | Counterfactual replay with full cost accounting | `replay.py`, `strategy.py`, `costs.py`, `regime.py` | built, unit-tested on synthetic paths and on live recordings |
| 3 | Forward paper engine | `paper.py` | built, resumable, writes daily NLV reports |
| 4 | Small live calibration | none | **not built**; gated on phase 2 and 3 results, see thresholds |

## Why this is a separate lane and not a replacement

The brief itself ranks fresh-graduation sniping last and says to keep it as a high-risk
baseline until comparable net results exist. Nothing in this package changes the executor.
Both lanes write to the same `DATA_DIR`, with all market-making files prefixed `mm_`.

Two facts from the brief decide the posture at the current bankroll:

- Fee income scales with position share. At $40 in a $250,000 pool the share is 0.016%;
  at 1% pool fees and $1M daily volume that is roughly $1.40 a day gross before inventory
  drift. The edge gate below is what decides whether that beats the drift.
- Fixed costs dominate small positions. A $40 range position pays one swap fee to reach a
  50/50 split, three to four transactions, and an exit swap; at a 1% pool fee that is about
  1.1% of the position before any fee income. The gate accounts for this explicitly, which
  is why most pools reject at $40 and would pass at $400.

## Running it

```bash
cd grad-backtest
python -m mm costs                 # break-even table from the brief
python -m mm screen                # universe -> data/mm_universe.csv, rejects -> data/mm_rejects.csv
python -m mm record --hours 6      # snapshots -> data/mm_snapshots/<mint>.csv every MM_POLL_SECONDS
python -m mm replay                # every strategy over every recording -> data/mm_replay_report.json
python -m mm paper --hours 168     # screen + record + shadow strategies, resumable, no transactions
```

No API keys are required. Sources are Jupiter lite-api (token facts, prices, executable
quotes), Meteora's DLMM data API (pool config, TVL, cumulative fees), DexScreener (pairs and
liquidity) and a public Solana RPC (mint authorities and Token-2022 extensions). Public RPCs
refuse `getTokenLargestAccounts`, so holder concentration comes from Jupiter's audit field.

## What each stage does

### Screener (`screener.py`)

Seeds from Jupiter's 24h top-traded and top-organic lists, drops majors, stables and wrapped
assets, then screens each token. Every reject is a row in `mm_rejects.csv` with the stage,
the measured value and the limit. Checks, in order:

1. **Age** from the first pool (Jupiter) or the oldest DexScreener pair, minimum 30 days.
2. **Liquidity**: best pool against SOL or USDC must hold `MM_MIN_POOL_LIQUIDITY_USD`;
   below the preferred band is a warning. Quote reserves are recorded separately from TVL.
3. **DLMM pool**: the deepest Meteora DLMM pool against an accepted quote, with bin step,
   base and dynamic fee, protocol share and 24h fee/TVL. Missing is a warning (momentum-only
   candidate) unless `MM_REQUIRE_DLMM_POOL=1`.
4. **Token controls** from the mint account: live mint or freeze authority, Token-2022
   permanent delegate, transfer hook, or a transfer fee above `MM_MAX_TRANSFER_FEE_BPS`
   all reject.
5. **Holders**: top-holder share and dev balance from Jupiter's audit.
6. **Participation**: 24h unique traders, organic score, signed-flow imbalance
   `|buy - sell| / (buy + sell)`, organic buyers.
7. **Exit test** on executable Jupiter sell quotes: a ladder of sizes is quoted and impact is
   measured against the smallest quote's unit price, not a displayed spot. The full maximum
   position must clear the routine band and the emergency band must hold
   `MM_EXIT_DEPTH_MULTIPLE` maximum positions.

Diagnostics recorded but never used as safety certificates: volume/TVL, liquidity/market cap.

### Recorder (`recorder.py`)

One row per token per poll: price, pool liquidity, quote reserves, the DLMM pool's
cumulative volume and fees (so per-interval fee income is a difference, not a rolling-window
estimate), dynamic fee, 1h signed flow and trader counts, the exit ladder, data age and any
source errors. A row with errors is a risk-controller stop, never silently used.

### Regimes (`regime.py`)

Deterministic labels from the trailing window: `SIDEWAYS`, `UP_DRIFT`, `DECLINE`,
`PUMP_JUMP`, `CRASH`, `CHOP`, `DECAY`, `LIQUIDITY_WITHDRAWAL`, `STALE`, `WARMUP`. Entry is
only allowed in `SIDEWAYS`, `CHOP` and `UP_DRIFT`; `CRASH`, `LIQUIDITY_WITHDRAWAL`,
`STALE` and `DECAY` force an exit.

### Strategy and risk (`strategy.py`)

States: `OBSERVE`, `PROVIDE_TWO_SIDED_LIQUIDITY`, `REDUCE_INVENTORY`, `STAY_OUT`.
Stay-out is a normal state.

Adaptive DLMM entry requires all of:

- no risk-controller stop (stale data, source errors, crash, liquidity withdrawal,
  participation collapse, exit impact above the emergency band, daily loss limit);
- exposure inside the token and portfolio caps, routine exit impact inside its band;
- a projected edge above zero over `MM_HORIZON_HOURS`:

```
edge = fee_yield_per_hour * lp_share * horizon * P(in range)
     - sigma_hourly^2 / 8 * horizon           # loss-versus-rebalancing drift
     - fixed_costs / position                 # setup swap, transactions, exit impact
     - model_error
```

`fee_yield_per_hour` is the pool's measured 24h fees / TVL divided by 24, `lp_share` is one
minus the pool's protocol share, and `P(in range)` is the two-sided barrier probability for
a driftless log-normal price over the horizon. The range half-width is
`MM_RANGE_WIDTH_SIGMA * sigma_hourly * sqrt(horizon)` clamped to
`[MM_MIN_RANGE_HALF_WIDTH_PCT, MM_MAX_RANGE_HALF_WIDTH_PCT]`.

Inventory rule, in the direction the brief corrects: when the position's token share
exceeds `target + band` the bot withdraws and reduces. It never lifts bids or widens asks
while long. Price leaving the range upward converts the position to quote and it is
withdrawn without loss; leaving downward is the loss case and triggers `REDUCE`.

Positions are Uniswap-v3-style uniform-liquidity ranges as an approximation of flat DLMM
bin shapes; fee income per interval is `position value * interval pool fee yield *
lp_share` while in range.

Strategies replayed side by side: `adaptive_dlmm` (rank 1), `fixed_narrow_dlmm` (±5%,
always recenters, control), `full_range_cpmm` (control, fee share scaled down by the
capital efficiency of the widest adaptive range), `momentum` (breakout over the lookback
high with volume confirmation, trailing stop, fees and impact both sides), `hold_50_50`
and `cash` benchmarks.

### Replay and paper (`replay.py`, `paper.py`)

Replay merges every recording chronologically and reports, per strategy: final NLV, net
PnL, return, max drawdown, worst day, worst position, fees earned, costs paid, trades,
profitable days, profit factor and PnL by token. The paper engine feeds the same engine live
rows, persists state to `mm_paper/state.pkl`, and writes `mm_paper/nlv.csv`,
`mm_paper/events.csv` and `mm_paper/regimes.csv`.

## Thresholds before any live sizing (phase 4)

All of the following, on held-out recordings and at least two weeks of paper:

- `adaptive_dlmm` or `momentum` beats `hold_50_50`, `cash` and the graduation executor's
  realized net return over the same window;
- max drawdown under 15% of bankroll and no single token contributing more than half of
  the profit;
- fees earned exceed costs paid by at least 2x on the LP strategies;
- paper exit impact inside the routine band on every withdrawal.

## What is deliberately not built

- **Live DLMM execution.** Meteora ships a TypeScript SDK for position creation, liquidity
  add/remove and fee claims; there is no maintained Python equivalent. Hand-building those
  instructions and testing them with real funds is outside a one-shot push. When phase 4
  is earned, the plan is a small Node sidecar exposed over HTTP that the paper engine's
  actions map onto one to one.
- **Live momentum execution.** The graduation executor's Jupiter swap path could carry it
  today, but the brief's validation plan comes first.
- **Swap-level streaming and bin-level state.** Snapshots are polled; fee attribution is
  pool-level. Bin-level replay needs an archive of DLMM account state that no public API
  serves.

## Tests

```bash
python -m unittest test_mm_costs test_mm_screener test_mm_strategy
```

`test_mm_costs` reproduces the brief's break-even, CPMM-impact and full-range tables to
the printed precision.
