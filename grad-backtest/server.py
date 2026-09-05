#!/usr/bin/env python3
"""Web dashboard for grad-backtest.

Serves a public, read-only dashboard over the result files that
`grad_backtest.py run` and `position_sizing.py` write into DATA_DIR,
and can launch those jobs in a background process so the whole thing
runs as one Railway service.

Writes are gated: POST /api/run requires ADMIN_TOKEN. Collection jobs use
HELIUS_API_KEY and MIGRATION_ADDRESS. A live executor additionally uses a sealed
burner-wallet key plus standard Solana RPC endpoint variables.
"""

from __future__ import annotations

import hmac
import json
import os
import shlex
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

DATA_DIR = Path(os.getenv("DATA_DIR", "data"))
STATIC_DIR = Path(__file__).parent / "static"
START_BALANCE = float(os.getenv("START_BALANCE", "100"))
ACCOUNT_FRACTION = float(os.getenv("ACCOUNT_FRACTION", "0.10"))
FIXED_FEE_PER_SIDE = float(os.getenv("FIXED_FEE_PER_SIDE", "0.10"))
LOG_FILE = DATA_DIR / "run.log"

ALLOWED_STAGES = {"collect", "run", "sizing", "optimize"}
EXECUTOR_STATE = DATA_DIR / "executor_state.json"
EXECUTOR_STOP = DATA_DIR / "executor.stop"
EXECUTOR_PANIC = DATA_DIR / "executor.panic"

app = FastAPI(title="grad-backtest dashboard", docs_url=None, redoc_url=None)

_job_lock = threading.Lock()
_job: dict[str, Any] = {"running": False, "stage": None, "started_at": None, "returncode": None}
_executor_proc: subprocess.Popen | None = None


def read_csv(name: str) -> pd.DataFrame:
    path = DATA_DIR / name
    if not path.exists():
        return pd.DataFrame()
    try:
        return pd.read_csv(path)
    except Exception:
        return pd.DataFrame()


def read_json(name: str) -> dict[str, Any] | None:
    path = DATA_DIR / name
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def file_mtime(name: str) -> str | None:
    path = DATA_DIR / name
    if not path.exists():
        return None
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def frame_records(frame: pd.DataFrame) -> list[dict[str, Any]]:
    if frame.empty:
        return []
    return json.loads(frame.to_json(orient="records"))


def equity_curve(trades: pd.DataFrame, fraction: float) -> list[dict[str, Any]]:
    """Simulated bankroll applying each historical trade at `fraction` of balance."""
    if trades.empty or "net_return" not in trades.columns:
        return []
    ordered = trades.dropna(subset=["net_return"]).copy()
    if "entry_timestamp" in ordered.columns:
        ordered = ordered.sort_values("entry_timestamp")
    balance = START_BALANCE
    points = [{"trade": 0, "timestamp": None, "balance": round(balance, 2)}]
    for i, row in enumerate(ordered.itertuples(index=False), start=1):
        balance += balance * fraction * float(row.net_return) - 2 * FIXED_FEE_PER_SIDE
        balance = max(balance, 0.0)
        points.append(
            {
                "trade": i,
                "timestamp": getattr(row, "entry_timestamp", None),
                "balance": round(balance, 2),
                "mint": getattr(row, "mint_address", None),
                "net_return": float(row.net_return),
            }
        )
        if balance <= 0:
            break
    return points


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/overview")
def overview() -> JSONResponse:
    trades = read_csv("trade_results.csv")
    errors = read_csv("errors.csv")
    summary = read_json("summary.json")
    sizing = read_json("sizing_summary.json")

    fraction = ACCOUNT_FRACTION
    if sizing and sizing.get("recommended"):
        fraction = float(sizing["recommended"]["fraction"])

    exit_reasons: dict[str, int] = {}
    returns: list[float] = []
    if not trades.empty and "net_return" in trades.columns:
        valid = trades.dropna(subset=["net_return"])
        returns = [float(r) for r in valid["net_return"]]
        if "exit_reason" in valid.columns:
            exit_reasons = valid["exit_reason"].value_counts().to_dict()

    wins = sum(r > 0 for r in returns)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "data_updated_at": file_mtime("trade_results.csv"),
        "job": dict(_job),
        "config": {
            "start_balance": START_BALANCE,
            "account_fraction": fraction,
            "fraction_source": "sizing_recommendation" if sizing and sizing.get("recommended") else "default",
            "fixed_fee_per_side": FIXED_FEE_PER_SIDE,
        },
        "stats": {
            "trade_count": len(returns),
            "win_rate": wins / len(returns) if returns else None,
            "median_net_return": float(pd.Series(returns).median()) if returns else None,
            "mean_net_return": float(pd.Series(returns).mean()) if returns else None,
            "best_trade": max(returns) if returns else None,
            "worst_trade": min(returns) if returns else None,
            "error_count": int(len(errors)),
        },
        "exit_reasons": exit_reasons,
        "returns": returns,
        "equity": equity_curve(trades, fraction),
        "summary": summary,
        "sizing": sizing,
    }
    return JSONResponse(payload)


@app.get("/api/trades")
def trades(limit: int = 200) -> JSONResponse:
    frame = read_csv("trade_results.csv")
    if not frame.empty and "entry_timestamp" in frame.columns:
        frame = frame.sort_values("entry_timestamp", ascending=False)
    return JSONResponse({"trades": frame_records(frame.head(limit))})


@app.get("/api/errors")
def errors(limit: int = 200) -> JSONResponse:
    return JSONResponse({"errors": frame_records(read_csv("errors.csv").tail(limit))})


@app.get("/api/activity")
def activity(lines: int = 100) -> JSONResponse:
    tail: list[str] = []
    if LOG_FILE.exists():
        tail = LOG_FILE.read_text(errors="replace").splitlines()[-max(1, min(lines, 1000)):]
    return JSONResponse({"job": dict(_job), "log": tail})


def _run_job(argv: list[str], stage: str) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open("a") as log:
        log.write(f"\n=== {datetime.now(timezone.utc).isoformat()} start {stage}: {' '.join(argv)} ===\n")
        log.flush()
        proc = subprocess.Popen(argv, stdout=log, stderr=subprocess.STDOUT, cwd=Path(__file__).parent)
        code = proc.wait()
        log.write(f"=== finished {stage} with exit code {code} ===\n")
    with _job_lock:
        _job.update({"running": False, "returncode": code})


@app.post("/api/run")
async def run_job(request: Request) -> JSONResponse:
    _require_admin(request)
    body = await request.json()
    stage = str(body.get("stage", ""))
    if stage not in ALLOWED_STAGES:
        raise HTTPException(400, f"stage must be one of {sorted(ALLOWED_STAGES)}")
    extra = shlex.split(str(body.get("extra_args", "")))
    if any(arg.startswith("--helius-api-key") for arg in extra):
        raise HTTPException(400, "pass credentials via environment variables, not job args")

    if stage == "sizing":
        argv = [sys.executable, "position_sizing.py", "--input", str(DATA_DIR / "trade_results.csv"),
                "--output", str(DATA_DIR / "sizing_summary.json"), "--balance", str(START_BALANCE),
                "--fixed-fee-per-side", str(FIXED_FEE_PER_SIDE), *extra]
    elif stage == "optimize":
        argv = [sys.executable, "optimize.py", *extra]
    else:
        argv = [sys.executable, "grad_backtest.py", stage, *extra]

    with _job_lock:
        if _job["running"]:
            raise HTTPException(409, f"a {_job['stage']} job is already running")
        _job.update(
            {
                "running": True,
                "stage": stage,
                "started_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "returncode": None,
            }
        )
    threading.Thread(target=_run_job, args=(argv, stage), daemon=True).start()
    return JSONResponse({"started": True, "stage": stage})


def _require_admin(request: Request) -> None:
    token = os.getenv("ADMIN_TOKEN", "")
    if not token:
        raise HTTPException(503, "ADMIN_TOKEN is not configured")
    if not hmac.compare_digest(request.headers.get("x-admin-token", ""), token):
        raise HTTPException(401, "bad token")


def _executor_running() -> bool:
    return _executor_proc is not None and _executor_proc.poll() is None


def _start_executor() -> None:
    global _executor_proc
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    EXECUTOR_STOP.unlink(missing_ok=True)
    # executor.py's own log() already appends every line to executor.log, so the
    # child's stdout/stderr are left inherited: activity and crash tracebacks show
    # up in the container log (Railway's deploy view) instead of vanishing into a
    # file, and lines stop being written to executor.log twice.
    _executor_proc = subprocess.Popen([sys.executable, "executor.py"], cwd=Path(__file__).parent)


@app.get("/api/live")
def live(limit: int = 100) -> JSONResponse:
    state = None
    if EXECUTOR_STATE.exists():
        try:
            state = json.loads(EXECUTOR_STATE.read_text())
        except Exception:
            state = None
    trades = read_csv("live_trades.csv")
    if not trades.empty and "closed_at" in trades.columns:
        trades = trades.sort_values("closed_at", ascending=False)
    log_tail: list[str] = []
    log_path = DATA_DIR / "executor.log"
    if log_path.exists():
        log_tail = log_path.read_text(errors="replace").splitlines()[-60:]
    returns = []
    if not trades.empty and "net_return" in trades.columns:
        returns = [float(r) for r in trades["net_return"].dropna()]
    return JSONResponse(
        {
            "running": _executor_running(),
            "draining": EXECUTOR_STOP.exists() or EXECUTOR_PANIC.exists(),
            "mode": (state or {}).get("mode"),
            "state": state,
            "trades": frame_records(trades.head(limit)),
            "stats": {
                "closed_count": len(returns),
                "win_rate": sum(r > 0 for r in returns) / len(returns) if returns else None,
                "realized_pnl_usd": float(sum(t.get("exit_usd", 0) - t.get("position_usd", 0) for t in frame_records(trades))) if len(returns) else 0.0,
            },
            "log": log_tail,
        }
    )


@app.post("/api/executor/start")
def executor_start(request: Request) -> JSONResponse:
    _require_admin(request)
    if _executor_running():
        raise HTTPException(409, "executor already running")
    _start_executor()
    return JSONResponse({"started": True, "mode": os.getenv("EXECUTOR_MODE", "paper")})


@app.post("/api/executor/stop")
def executor_stop(request: Request) -> JSONResponse:
    _require_admin(request)
    EXECUTOR_STOP.touch()
    return JSONResponse({"draining": True, "note": "no new entries; open positions still managed until closed"})


@app.post("/api/executor/panic")
def executor_panic(request: Request) -> JSONResponse:
    _require_admin(request)
    EXECUTOR_PANIC.touch()
    return JSONResponse({"panic": True, "note": "selling all open positions at market, then draining"})


@app.on_event("startup")
def maybe_autostart_executor() -> None:
    autostart = os.getenv("EXECUTOR_AUTOSTART", "0") == "1"
    mode = os.getenv("EXECUTOR_MODE", "paper")
    if autostart and not _executor_running():
        _start_executor()
        print(f"[server] autostart enabled: launched executor in {mode} mode", flush=True)
    elif not autostart:
        print(
            f"[server] EXECUTOR_AUTOSTART is not 1 — executor NOT started "
            f"(configured mode would be {mode}); POST /api/executor/start to run it",
            flush=True,
        )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
