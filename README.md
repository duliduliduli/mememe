# mememe

## grad-backtest

Research pipeline that backtests Pump.fun tokens after graduation (migration to
PumpSwap/Raydium): collects graduation candidates via Helius, pulls historical
OHLCV from GeckoTerminal, and simulates a +75% TP / -30% SL / 30-minute time-stop
strategy with configurable friction costs.

- Code and full usage docs: [`grad-backtest/`](grad-backtest/README.md)
- Market-making research track for established memes (screener, recorder, replay,
  paper engine; no live trading): [`grad-backtest/mm/`](grad-backtest/mm/README.md)
- Public web dashboard (`server.py` + `static/`) showing equity curve, win rate,
  return distribution, recent trades, and a live activity log — deployable to
  Railway with a public URL.
- Deploying to Railway: point it at this repo and it builds from the root
  `Dockerfile` with no configuration (no Root Directory setting needed). Add a
  volume at `/data` and the env vars, then generate a public domain for the
  dashboard.
- Run it in the cloud: the **"Run backtest in the cloud"** GitHub Actions workflow
  (results download as artifacts), or deploy `grad-backtest/` to Railway using the
  bundled `Dockerfile` + `railway.json`. Both need `HELIUS_API_KEY` and
  `MIGRATION_ADDRESS` configured as secrets/variables — see the
  [deployment section](grad-backtest/README.md#running-in-the-cloud).

This is research software, not investment advice or a production trading executor.
