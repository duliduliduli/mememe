# Root-level build so Railway (and any container host) works without setting
# a Root Directory — it builds the grad-backtest app. Identical behavior to
# grad-backtest/Dockerfile, which remains for builds rooted in that folder.
FROM python:3.12-slim

WORKDIR /app

COPY grad-backtest/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy every Python module so newly added executor dependencies cannot be
# omitted from the container by an outdated hand-maintained file list.
COPY grad-backtest/*.py ./
COPY grad-backtest/mm mm
COPY grad-backtest/static static
COPY grad-backtest/data/graduations.example.csv data/

# Catch missing runtime modules during the image build instead of after
# Railway has already started the web server and autostarted the executor.
RUN python -c "import bundle_analysis, executor, mm.paper"

ENV PYTHONUNBUFFERED=1
# All scripts read/write here; mount a Railway volume at /data to persist
# graduations, the OHLCV cache, results, and the dashboard's job log.
ENV DATA_DIR=/data
RUN mkdir -p /data

# Default: serve the public dashboard (which can launch jobs itself when
# ADMIN_TOKEN is set). Setting BACKTEST_COMMAND instead runs one batch job
# and exits, e.g. BACKTEST_COMMAND="run --limit 5".
CMD ["sh", "-c", "if [ -n \"$BACKTEST_COMMAND\" ]; then python grad_backtest.py $BACKTEST_COMMAND; else uvicorn server:app --host 0.0.0.0 --port ${PORT:-8000}; fi"]
