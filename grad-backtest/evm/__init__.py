"""EVM copy-trading lane: mirror followed wallets on Robinhood Chain, Base and BNB Chain.

Run with `python -m evm`. Configuration is read from the environment (see README.md,
"EVM copy lane"). The lane keeps its own state in DATA_DIR/evm_state.json and trades in
DATA_DIR/evm_trades.csv, and logs to DATA_DIR/evm.log as well as stdout.
"""
