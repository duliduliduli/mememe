"""Live engine: the paper engine with real fills for the two candidate strategies.

`adaptive_dlmm` and `momentum` trade through LiveBroker (Jupiter swaps signed with
WALLET_PRIVATE_KEY, Meteora DLMM positions through the Node sidecar); the controls and
benchmarks keep running as shadows on the same rows so the comparison the brief asks for
is produced by the live run itself. State persists under DATA_DIR/mm_live and on-chain
positions are reconciled on every start.

Kill switches (files under DATA_DIR): mm.stop drains (no new entries, positions still
managed); mm.panic closes every live position at market, then drains."""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .config import MMConfig
from .execution import LiveBroker, Sidecar, Signer
from .paper import PaperEngine
from .replay import ReplayEngine
from .sources import HttpError, Sources
from .strategy import AdaptiveDLMM, Momentum, default_strategies

SIDECAR_DIR = Path(__file__).resolve().parent / "sidecar"


class LiveEngine(PaperEngine):
    mode = "live"

    def __init__(self, cfg: MMConfig, sources: Sources | None = None, log=print,
                 signer: Signer | None = None, sidecar: Sidecar | None = None) -> None:
        key = os.getenv("WALLET_PRIVATE_KEY", "")
        if signer is None and not key:
            raise SystemExit("MM live mode requires WALLET_PRIVATE_KEY (the executor's burner wallet)")
        self.signer = signer or Signer(key)
        self.sidecar_proc: subprocess.Popen | None = None
        self.sidecar = sidecar or self._connect_sidecar(cfg, log)
        self.sources = sources or Sources(cfg)
        self.broker = LiveBroker(cfg, self.sources, self.signer, self.sidecar, log=log)
        self.draining = False
        super().__init__(cfg, self.sources, log)
        self.reconcile()

    # -- wiring ----------------------------------------------------------------------
    @property
    def out_dir(self) -> Path:
        return self.cfg.live_dir

    def _new_engine(self) -> ReplayEngine:
        return ReplayEngine(self.cfg, default_strategies(self.cfg, self.cfg.bankroll_usd, live_broker=self.broker),
                            self.cfg.bankroll_usd)

    def _attach_brokers(self, engine: ReplayEngine) -> None:
        super()._attach_brokers(engine)
        for strategy in engine.strategies:
            if isinstance(strategy, (AdaptiveDLMM, Momentum)) and type(strategy) in (AdaptiveDLMM, Momentum):
                strategy.broker = self.broker

    def _connect_sidecar(self, cfg: MMConfig, log) -> Sidecar:
        url = cfg.sidecar_url or f"http://127.0.0.1:{cfg.sidecar_port}"
        sidecar = Sidecar(url)
        try:
            health = sidecar.health()
            log(f"[live] sidecar already running at {url}: wallet={health.get('wallet')}")
            return sidecar
        except Exception:  # noqa: BLE001
            pass
        if cfg.sidecar_url:
            raise SystemExit(f"MM_SIDECAR_URL={cfg.sidecar_url} is not answering /health")
        if not (SIDECAR_DIR / "node_modules").exists():
            raise SystemExit(f"sidecar dependencies missing: run `npm ci` in {SIDECAR_DIR}")
        env = dict(os.environ)
        env.setdefault("MM_SIDECAR_PORT", str(cfg.sidecar_port))
        self.sidecar_proc = subprocess.Popen(["node", "server.js"], cwd=SIDECAR_DIR, env=env,
                                             stdout=sys.stdout, stderr=subprocess.STDOUT)
        deadline = time.time() + 45
        while time.time() < deadline:
            try:
                health = sidecar.health()
                log(f"[live] sidecar started (pid {self.sidecar_proc.pid}) wallet={health.get('wallet')} rpc={health.get('rpc')}")
                if health.get("wallet") and health["wallet"] != self.signer.pubkey:
                    raise SystemExit("sidecar wallet does not match WALLET_PRIVATE_KEY")
                return sidecar
            except HttpError:
                time.sleep(1)
            except Exception:  # noqa: BLE001 - connection refused while booting
                time.sleep(1)
        raise SystemExit("sidecar did not become healthy within 45s")

    def close(self) -> None:
        if self.sidecar_proc and self.sidecar_proc.poll() is None:
            self.sidecar_proc.terminate()

    # -- reconciliation ----------------------------------------------------------------
    def live_strategies(self) -> list[Any]:
        return [s for s in self.engine.strategies if getattr(s, "is_live", False)]

    def reconcile(self) -> None:
        """Compare persisted live positions with the chain; drop what no longer exists."""
        deployed = 0.0
        for strategy in self.live_strategies():
            p = strategy.portfolio
            for mint, pos in list(p.lp.items()):
                if not pos.position_key or not pos.pool:
                    continue
                try:
                    view = self.sidecar.get("/positions", address=pos.pool, keys=pos.position_key)
                except Exception as exc:  # noqa: BLE001
                    self.log(f"[live] reconcile: could not read {pos.position_key}: {exc}; keeping position")
                    deployed += pos.entry_value
                    continue
                tracked = (view.get("tracked") or [{}])[0]
                if tracked.get("missing"):
                    credit = pos.value(pos.last_price)
                    p.cash += credit
                    p.record_pnl(mint, credit - pos.entry_value)
                    p.lp.pop(mint)
                    p.log(time.time(), mint, "CLOSE_EXTERNAL", "position missing on chain; credited last model value (unverified)",
                          pos.last_price, credit)
                    self.log(f"[live] reconcile: {mint[:8]} position {pos.position_key} is gone on chain; credited ${credit:.2f} unverified")
                else:
                    deployed += pos.entry_value
                    self.log(f"[live] reconcile: {mint[:8]} position {pos.position_key} alive bins=[{tracked.get('lowerBinId')},{tracked.get('upperBinId')}] "
                             f"token_raw={tracked.get('tokenAmountRaw')} quote_raw={tracked.get('quoteAmountRaw')}")
            for mint, pos in list(p.spot.items()):
                try:
                    balance = self.broker.token_balance_raw(mint)
                except Exception as exc:  # noqa: BLE001
                    self.log(f"[live] reconcile: balance check for {mint[:8]} failed: {exc}; keeping position")
                    deployed += pos.entry_value
                    continue
                if balance <= 0:
                    p.spot.pop(mint)
                    p.log(time.time(), mint, "CLOSE_EXTERNAL", "no token balance on chain; position dropped", pos.entry_price, 0.0)
                    self.log(f"[live] reconcile: {mint[:8]} spot position has no balance; dropped")
                else:
                    deployed += pos.entry_value
        self.broker.deployed_usd = deployed
        try:
            sol = self.broker.sol_balance()
            self.log(f"[live] wallet {self.signer.pubkey} holds {sol:.4f} SOL; live capital deployed ${deployed:.2f} of ${self.cfg.bankroll_usd:.2f} bankroll")
        except Exception as exc:  # noqa: BLE001
            self.log(f"[live] wallet balance unavailable: {exc}")
        for entry in self.universe:
            pool = entry.get("dlmm_pool")
            if not pool:
                continue
            try:
                view = self.sidecar.get("/positions", address=pool, discover="1")
            except Exception:  # noqa: BLE001
                continue
            known = {pos.position_key for s in self.live_strategies() for pos in s.portfolio.lp.values()}
            for found in view.get("discovered") or []:
                if found.get("position") not in known:
                    self.log(f"[live] untracked DLMM position {found['position']} in pool {pool[:8]} (not managed by this engine)")

    # -- control -----------------------------------------------------------------------
    def check_flags(self) -> None:
        cfg = self.cfg
        if cfg.panic_flag.exists():
            self.log("[live] PANIC flag: closing every live position at market")
            now = time.time()
            for strategy in self.live_strategies():
                p = strategy.portfolio
                for mint, pos in list(p.lp.items()):
                    row = self.engine.history.get(mint, [{}])[-1] or {"mint": mint, "ts": now}
                    strategy._close(pos, row, self.engine.prices.get(mint, pos.last_price), "panic", "EMERGENCY_WITHDRAW")
                for mint, pos in list(p.spot.items()):
                    row = self.engine.history.get(mint, [{}])[-1] or {"mint": mint, "ts": now}
                    strategy._close(pos, row, self.engine.prices.get(mint, pos.entry_price), "panic")
            cfg.panic_flag.unlink(missing_ok=True)
            cfg.stop_flag.touch()
        draining = cfg.stop_flag.exists()
        if draining != self.draining:
            self.draining = draining
            self.log("[live] STOP flag: draining, no new entries" if draining else "[live] stop flag cleared: entries enabled")
        self.broker.draining = draining

    def tick(self) -> None:
        self.check_flags()
        super().tick()
        try:
            sol = self.broker.sol_balance()
            self.log(f"[live] wallet {sol:.4f} SOL, deployed ${self.broker.deployed_usd:.2f}, draining={self.draining}")
        except Exception as exc:  # noqa: BLE001
            self.log(f"[live] wallet balance unavailable: {exc}")

    def run(self, hours: float) -> None:
        try:
            super().run(hours)
        finally:
            self.save()
            self.close()
