FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY grad_backtest.py position_sizing.py server.py executor.py bundle_analysis.py optimize.py \
     test_grad_backtest.py test_position_sizing.py test_server.py \
     test_executor.py test_executor_guards.py test_optimize.py test_scale_out.py test_slippage_retry.py test_market_cap.py test_reconcile.py test_bundle_guards.py test_rate_limits.py test_moon_bag_target.py ./
COPY static static
COPY data/graduations.example.csv data/

# Catch missing runtime modules during the image build instead of after
# Railway has already started the web server and autostarted the executor.
RUN python -c "import bundle_analysis, executor"

ENV PYTHONUNBUFFERED=1
# All scripts read/write here; mount a Railway volume at /data to persist
# graduations, the OHLCV cache, results, and the dashboard's job log.
ENV DATA_DIR=/data
RUN mkdir -p /data

# Default: serve the public dashboard (which can launch jobs itself when
# ADMIN_TOKEN is set). Setting BACKTEST_COMMAND instead runs one batch job
# and exits, e.g. BACKTEST_COMMAND="run --limit 5".
CMD ["sh", "-c", "if [ -n \"$BACKTEST_COMMAND\" ]; then python grad_backtest.py $BACKTEST_COMMAND; else uvicorn server:app --host 0.0.0.0 --port ${PORT:-8000}; fi"]
