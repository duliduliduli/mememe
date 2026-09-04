# mememe

## grad-backtest

Research pipeline that backtests Pump.fun tokens after graduation (migration to
PumpSwap/Raydium): collects graduation candidates via Helius, pulls historical
OHLCV from GeckoTerminal, and simulates a +75% TP / -30% SL / 30-minute time-stop
strategy with configurable friction costs.

- Code and full usage docs: [`grad-backtest/`](grad-backtest/README.md)
- Run it in the cloud: the **"Run backtest in the cloud"** GitHub Actions workflow
  (results download as artifacts), or deploy `grad-backtest/` to Railway using the
  bundled `Dockerfile` + `railway.json`. Both need `HELIUS_API_KEY` and
  `MIGRATION_ADDRESS` configured as secrets/variables — see the
  [deployment section](grad-backtest/README.md#running-in-the-cloud).

This is research software, not investment advice or a production trading executor.
