# Root-level build so Railway (and any container host) works without setting
# a Root Directory — it builds the grad-backtest app. Identical behavior to
# grad-backtest/Dockerfile, which remains for builds rooted in that folder.
FROM python:3.12-slim

WORKDIR /app

COPY grad-backtest/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY grad-backtest/grad_backtest.py grad-backtest/position_sizing.py grad-backtest/server.py \
     grad-backtest/executor.py grad-backtest/optimize.py grad-backtest/test_grad_backtest.py \
     grad-backtest/test_position_sizing.py grad-backtest/test_server.py \
     grad-backtest/test_executor.py grad-backtest/test_executor_guards.py \
     grad-backtest/test_optimize.py grad-backtest/test_scale_out.py grad-backtest/test_slippage_retry.py grad-backtest/test_market_cap.py grad-backtest/test_reconcile.py ./
COPY grad-backtest/static static
COPY grad-backtest/data/graduations.example.csv data/

ENV PYTHONUNBUFFERED=1
# All scripts read/write here; mount a Railway volume at /data to persist
# graduations, the OHLCV cache, results, and the dashboard's job log.
ENV DATA_DIR=/data
RUN mkdir -p /data

# Default: serve the public dashboard (which can launch jobs itself when
# ADMIN_TOKEN is set). Setting BACKTEST_COMMAND instead runs one batch job
# and exits, e.g. BACKTEST_COMMAND="run --limit 5".
CMD ["sh", "-c", "if [ -n \"$BACKTEST_COMMAND\" ]; then python grad_backtest.py $BACKTEST_COMMAND; else uvicorn server:app --host 0.0.0.0 --port ${PORT:-8000}; fi"]
