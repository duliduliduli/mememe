FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY grad_backtest.py test_grad_backtest.py ./
COPY data/graduations.example.csv data/

ENV PYTHONUNBUFFERED=1

# BACKTEST_COMMAND holds the subcommand and flags, e.g.:
#   run --limit 5
#   collect --count 500 --start-time 2026-08-01T00:00:00Z
CMD ["sh", "-c", "python grad_backtest.py ${BACKTEST_COMMAND:-run}"]
