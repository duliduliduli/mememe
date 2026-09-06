"""Forward paper engine: live snapshots, live regimes, shadow positions, no transactions.

Runs the screener on a schedule, records snapshots every poll, feeds them to the same
strategy engine replay uses, persists state between runs and writes daily net-liquidation
reports. A restart resumes from DATA_DIR/mm_paper/state.pkl."""
from __future__ import annotations

import csv
import pickle
import time
from pathlib import Path
from typing import Any

from .config import MMConfig
from .recorder import Recorder
from .replay import ReplayEngine, portfolio_metrics
from .screener import Screener, append_rows, load_universe, utc_iso
from .sources import Sources
from .strategy import default_strategies

EVENT_COLUMNS = ["ts", "recorded_at", "strategy", "mint", "action", "reason", "price", "value", "detail"]
NLV_COLUMNS = ["ts", "recorded_at", "strategy", "nlv", "cash", "exposure", "fees_earned", "costs_paid", "trades",
               "open_positions", "daily_pnl"]
REGIME_COLUMNS = ["ts", "recorded_at", "mint", "regime", "sigma_hourly", "drift_pct", "volume_ratio",
                  "liquidity_change_pct", "participation_ratio", "flow_imbalance", "reasons"]


class PaperEngine:
    def __init__(self, cfg: MMConfig, sources: Sources | None = None, log=print) -> None:
        self.cfg = cfg
        self.src = sources or Sources(cfg)
        self.log = log
        self.screener = Screener(cfg, self.src)
        self.recorder = Recorder(cfg, self.src)
        self.state_file = cfg.paper_dir / "state.pkl"
        self.engine = self._load_engine()
        self.universe: list[dict[str, Any]] = load_universe(cfg.universe_file)
        # A restart inside the refresh window reuses the universe file instead of re-screening.
        self.universe_refreshed = cfg.universe_file.stat().st_mtime if self.universe else 0.0
        self.events_written = {s.name: len(s.portfolio.events) for s in self.engine.strategies}

    def _load_engine(self) -> ReplayEngine:
        if self.state_file.exists():
            try:
                with self.state_file.open("rb") as fh:
                    engine = pickle.load(fh)
                engine.cfg = self.cfg
                for strategy in engine.strategies:
                    strategy.cfg = self.cfg
                self.log(f"resumed paper state with {len(engine.strategies)} strategies")
                return engine
            except Exception as exc:  # noqa: BLE001
                self.log(f"paper state unreadable ({exc}); starting fresh")
        return ReplayEngine(self.cfg, default_strategies(self.cfg, self.cfg.bankroll_usd), self.cfg.bankroll_usd)

    def save(self) -> None:
        self.cfg.paper_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.state_file.with_suffix(".tmp")
        with tmp.open("wb") as fh:
            pickle.dump(self.engine, fh)
        tmp.replace(self.state_file)

    def refresh_universe(self, force: bool = False) -> None:
        due = time.time() - self.universe_refreshed >= self.cfg.universe_refresh_minutes * 60
        if not (force or due or not self.universe):
            return
        accepted, rejected = self.screener.run(write=True)
        self.universe = load_universe(self.cfg.universe_file)
        self.universe_refreshed = time.time()
        self.log(f"universe refreshed: {len(accepted)} accepted, {len(rejected)} rejected, "
                 f"{sum(1 for c in accepted if c.dlmm_pool)} with DLMM pools")

    def tick(self) -> None:
        now = time.time()
        rows = self.recorder.tick(self.universe)
        regime_rows = []
        for row in rows:
            regime = self.engine.feed(row, now=now)
            regime_rows.append({"ts": row["ts"], "recorded_at": row["recorded_at"], "mint": row["mint"], "regime": regime.label,
                                "sigma_hourly": round(regime.sigma_hourly, 5), "drift_pct": round(regime.drift_pct, 3),
                                "volume_ratio": regime.volume_ratio, "liquidity_change_pct": regime.liquidity_change_pct,
                                "participation_ratio": regime.participation_ratio, "flow_imbalance": regime.flow_imbalance,
                                "reasons": "; ".join(regime.reasons)})
        append_rows(self.cfg.paper_dir / "regimes.csv", REGIME_COLUMNS, regime_rows)
        marks = self.engine.mark(now)
        stamp = utc_iso(now)
        nlv_rows, event_rows = [], []
        for strategy in self.engine.strategies:
            p = strategy.portfolio
            nlv_rows.append({"ts": round(now, 3), "recorded_at": stamp, "strategy": strategy.name, "nlv": round(marks[strategy.name], 4),
                             "cash": round(p.cash, 4), "exposure": round(p.exposure(self.engine.prices), 4),
                             "fees_earned": round(p.fees_earned, 4), "costs_paid": round(p.costs_paid, 4), "trades": p.trades,
                             "open_positions": len(p.lp) + len(p.spot), "daily_pnl": round(p.daily_pnl, 4)})
            new = p.events[self.events_written.get(strategy.name, 0):]
            for e in new:
                event_rows.append({"ts": e.ts, "recorded_at": utc_iso(e.ts), "strategy": e.strategy, "mint": e.mint, "action": e.action,
                                   "reason": e.reason, "price": e.price, "value": round(e.value, 4), "detail": e.detail})
                self.log(f"[{strategy.name}] {e.action} {e.mint[:8]} {e.reason} value=${e.value:.2f} {e.detail}")
            self.events_written[strategy.name] = len(p.events)
        append_rows(self.cfg.paper_dir / "nlv.csv", NLV_COLUMNS, nlv_rows)
        append_rows(self.cfg.paper_dir / "events.csv", EVENT_COLUMNS, event_rows)
        self.save()
        summary = ", ".join(f"{n}={v:.2f}" for n, v in marks.items())
        self.log(f"tick: {len(rows)} snapshots; NLV {summary}")

    def run(self, hours: float) -> None:
        deadline = time.time() + hours * 3600
        self.refresh_universe(force=not self.universe)
        while True:
            started = time.time()
            self.refresh_universe()
            if self.universe:
                self.tick()
            else:
                self.log("universe empty after screening; nothing to record this tick")
            if time.time() >= deadline:
                break
            time.sleep(max(1.0, self.cfg.poll_seconds - (time.time() - started)))
        self.log("paper run finished")

    def report(self) -> dict[str, Any]:
        return {s.name: portfolio_metrics(s.portfolio, self.engine.starting_cash) for s in self.engine.strategies}
