"""Counterfactual replay of recorded snapshots across every strategy, with full accounting.

Rows from all tokens are merged chronologically, each strategy sees the same sequence, and
the report carries the specification's required metrics per strategy."""
from __future__ import annotations

import json
import math
import time
from pathlib import Path
from typing import Any

from .config import MMConfig
from .recorder import load_snapshots, snapshot_path
from .regime import Regime, classify
from .strategy import Portfolio, Strategy, default_strategies, _f


def load_series(cfg: MMConfig, mints: list[str] | None = None) -> dict[str, list[dict[str, Any]]]:
    series: dict[str, list[dict[str, Any]]] = {}
    if mints:
        for mint in mints:
            rows = load_snapshots(cfg, mint)
            if rows:
                series[mint] = rows
        return series
    if not cfg.snapshots_dir.exists():
        return series
    for path in sorted(cfg.snapshots_dir.glob("*.csv")):
        rows = load_snapshots(cfg, path.stem)
        if rows:
            series[path.stem] = rows
    return series


def portfolio_metrics(p: Portfolio, starting_cash: float) -> dict[str, Any]:
    history = [v for _, v in p.nlv_history] or [starting_cash]
    peak, max_dd = history[0], 0.0
    for value in history:
        peak = max(peak, value)
        if peak > 0:
            max_dd = max(max_dd, (peak - value) / peak)
    days: dict[str, list[float]] = {}
    for ts, value in p.nlv_history:
        days.setdefault(time.strftime("%Y-%m-%d", time.gmtime(ts)), []).append(value)
    day_pnls = {}
    prev_close = starting_cash
    for day, values in sorted(days.items()):
        day_pnls[day] = values[-1] - prev_close
        prev_close = values[-1]
    wins = sum(v for v in p.pnl_by_mint.values() if v > 0)
    losses = -sum(v for v in p.pnl_by_mint.values() if v < 0)
    # Replay liquidates in finish() and marks once more, so the last mark equals cash there;
    # paper and live report mid-run, where open positions are part of the value.
    final = history[-1]
    return {
        "strategy": p.strategy,
        "starting_cash": starting_cash,
        "final_nlv": round(final, 4),
        "net_pnl": round(final - starting_cash, 4),
        "return_pct": round((final / starting_cash - 1) * 100, 3) if starting_cash else None,
        "max_drawdown_pct": round(max_dd * 100, 3),
        "worst_day": min(day_pnls.items(), key=lambda kv: kv[1]) if day_pnls else None,
        "worst_position": p.worst_position,
        "fees_earned": round(p.fees_earned, 4),
        "costs_paid": round(p.costs_paid, 4),
        "trades": p.trades,
        "profitable_days": sum(1 for v in day_pnls.values() if v > 0),
        "days": len(day_pnls),
        "profit_factor": round(wins / losses, 3) if losses > 0 else (math.inf if wins > 0 else None),
        "pnl_by_mint": {k: round(v, 4) for k, v in sorted(p.pnl_by_mint.items(), key=lambda kv: kv[1])},
        "events": len(p.events),
    }


class ReplayEngine:
    """Feeds rows to strategies one at a time; also the core of the paper engine."""

    def __init__(self, cfg: MMConfig, strategies: list[Strategy], starting_cash: float) -> None:
        self.cfg = cfg
        self.strategies = strategies
        self.starting_cash = starting_cash
        self.history: dict[str, list[dict[str, Any]]] = {}
        self.prices: dict[str, float] = {}
        self.liquidation: dict[str, float] = {}
        self.regimes: dict[str, Regime] = {}
        self.last_row: dict[str, Any] | None = None

    def feed(self, row: dict[str, Any], now: float | None = None) -> Regime:
        mint = row["mint"]
        hist = self.history.setdefault(mint, [])
        hist.append(row)
        if len(hist) > self.cfg.lookback_points * 3:
            del hist[: len(hist) - self.cfg.lookback_points * 3]
        regime = classify(hist, self.cfg, now)
        self.regimes[mint] = regime
        price = _f(row, "price_usd")
        if price:
            self.prices[mint] = price
        impact = _f(row, "impact_at_max_position_pct")
        if impact is not None:
            self.liquidation[mint] = impact
        for strategy in self.strategies:
            strategy.step(row, regime, self.prices)
        self.last_row = row
        return regime

    def mark(self, ts: float) -> dict[str, float]:
        return {s.name: s.portfolio.mark(ts, self.prices, self.liquidation) for s in self.strategies}

    def finish(self) -> None:
        if self.last_row is None:
            return
        for strategy in self.strategies:
            strategy.finish(self.last_row, self.regimes.get(self.last_row["mint"], Regime("WARMUP")))
            strategy.portfolio.mark(_f(self.last_row, "ts") or 0.0, self.prices, self.liquidation)

    def report(self) -> dict[str, Any]:
        return {s.name: portfolio_metrics(s.portfolio, self.starting_cash) for s in self.strategies}


def replay(cfg: MMConfig, series: dict[str, list[dict[str, Any]]], strategies: list[Strategy] | None = None,
           starting_cash: float | None = None) -> dict[str, Any]:
    cash = starting_cash if starting_cash is not None else cfg.bankroll_usd
    strategies = strategies or default_strategies(cfg, cash)
    engine = ReplayEngine(cfg, strategies, cash)
    merged = sorted((r for rows in series.values() for r in rows), key=lambda r: float(r.get("ts") or 0))
    last_ts = None
    for row in merged:
        ts = float(row.get("ts") or 0)
        if last_ts is not None and ts != last_ts:
            engine.mark(last_ts)
        engine.feed(row)
        last_ts = ts
    if last_ts is not None:
        engine.mark(last_ts)
    engine.finish()
    report = engine.report()
    span = (merged[-1]["ts"], merged[0]["ts"]) if merged else (None, None)
    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "rows": len(merged),
        "tokens": len(series),
        "from_ts": float(span[1]) if merged else None,
        "to_ts": float(span[0]) if merged else None,
        "hours": round((float(span[0]) - float(span[1])) / 3600, 2) if merged else 0,
        "starting_cash": cash,
        "strategies": report,
        "ranking": sorted(report.keys(), key=lambda k: report[k]["final_nlv"], reverse=True),
        "events": [e.__dict__ for s in strategies for e in s.portfolio.events],
    }


def write_report(cfg: MMConfig, report: dict[str, Any], name: str = "mm_replay_report.json") -> Path:
    path = cfg.data_dir / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=1, default=str))
    return path
