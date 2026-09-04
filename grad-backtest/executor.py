#!/usr/bin/env python3
"""Bootstrap the last known-good executor from git history, then apply
pending-persist + log-only bundle hooks.

This exists because a bad push replaced executor.py on main with a stub.
Once the full file can be committed normally, this bootstrap can go away.
"""
from __future__ import annotations

import os
import urllib.request

BASE_COMMIT = "c85a56cdd8b57a04bd492649e607ab6dcd25d14a"
BASE_URL = (
    "https://raw.githubusercontent.com/duliduliduli/mememe/"
    f"{BASE_COMMIT}/grad-backtest/executor.py"
)

_src = urllib.request.urlopen(BASE_URL, timeout=30).read()
_ns = dict(globals())
_ns["__name__"] = "executor_base"
exec(compile(_src, "executor_base.py", "exec"), _ns)
for k, v in _ns.items():
    if k != "__name__":
        globals()[k] = v

_orig_config_init = Config.__init__
_orig_guard = entry_guard_reason
_orig_try_enter = Executor.try_enter
_orig_run = Executor.run


def _config_init(self) -> None:
    _orig_config_init(self)
    self.max_bundle_slot_pct = float(os.getenv("MAX_BUNDLE_SLOT_PCT", "0.30"))
    self.max_cluster_pct = float(os.getenv("MAX_CLUSTER_PCT", "0.30"))
    self.max_dev_cluster_pct = float(os.getenv("MAX_DEV_CLUSTER_PCT", "0.15"))
    self.max_top10_wallet_pct = float(os.getenv("MAX_TOP10_WALLET_PCT", "0.50"))
    self.bundle_lookup_timeout_ms = float(os.getenv("BUNDLE_LOOKUP_TIMEOUT_MS", "1500"))
    self.bundle_max_wallets = int(os.getenv("BUNDLE_MAX_WALLETS", "20"))
    self.bundle_log_only = os.getenv("BUNDLE_LOG_ONLY", "1") == "1"
    self.helius_poll_limit = int(os.getenv("HELIUS_POLL_LIMIT", "10"))
    self.catchup_poll_limit = int(os.getenv("CATCHUP_POLL_LIMIT", "50"))


def _guard(cfg, graduated_ts, now, price_impact_pct, market_cap_usd=None,
           curve_age_seconds=None, top_holder_pct=None, top10_wallet_pct=None,
           bundle_slot_pct=None, cluster_pct=None, dev_cluster_pct=None,
           bundle_slot=None, bundle_wallet_count=None, cluster_wallets=None):
    reason = _orig_guard(cfg, graduated_ts, now, price_impact_pct, market_cap_usd,
                         curve_age_seconds, top_holder_pct)
    if reason:
        return reason
    if getattr(cfg, "bundle_log_only", True):
        return None
    if cluster_pct is not None and getattr(cfg, "max_cluster_pct", 0) > 0 and cluster_pct > cfg.max_cluster_pct * 100:
        return f"funding cluster {cluster_pct:.0f}% > {cfg.max_cluster_pct * 100:.0f}%"
    if bundle_slot_pct is not None and getattr(cfg, "max_bundle_slot_pct", 0) > 0 and bundle_slot_pct > cfg.max_bundle_slot_pct * 100:
        return f"bundle slot {bundle_slot_pct:.0f}% > {cfg.max_bundle_slot_pct * 100:.0f}%"
    return None


def _executor_init(self, cfg):
    cfg.validate()
    self.cfg = cfg
    self.rpc = Rpc(cfg)
    self.jup = Jupiter(cfg)
    self.wallet = Wallet(cfg) if cfg.mode == "live" else None
    self.state = load_state(cfg)
    self.state["mode"] = cfg.mode
    self.state.setdefault("pending", [])
    self.pending = list(self.state.get("pending") or [])
    self._funder_cache = {}
    self._catchup_done = False
    kept = []
    for item in self.pending:
        if now_ts() - float(item.get("enter_at") or 0) > cfg.max_entry_lateness_seconds:
            self.skip(item.get("mint", "?"), "stale after restart")
            continue
        kept.append(item)
    self.pending = kept
    self.state["pending"] = self.pending
    daily = self.state.get("daily") or {}
    log(f"resuming daily PnL ${float(daily.get('realized_pnl_usd') or 0):.2f} for {daily.get('date', '?')}")


def _poll(self):
    url = f"https://api-mainnet.helius-rpc.com/v0/addresses/{self.cfg.migration_address}/transactions"
    limit = self.cfg.helius_poll_limit if getattr(self, "_catchup_done", False) else self.cfg.catchup_poll_limit
    try:
        import requests as _requests
        batch = _requests.get(url, params={"api-key": self.cfg.helius_api_key, "limit": limit}, timeout=20).json()
        self._catchup_done = True
    except Exception as exc:
        log(f"WARN helius poll failed: {exc}")
        return
    if not isinstance(batch, list):
        log(f"WARN helius poll unexpected response: {str(batch)[:200]}")
        return
    for tx in batch:
        signature = tx.get("signature") or ""
        timestamp = tx.get("timestamp") or tx.get("blockTime")
        if not signature or not timestamp or signature in self.state["seen_signatures"]:
            continue
        self.state["seen_signatures"].append(signature)
        mints = candidate_mints(tx)
        if len(mints) != 1:
            if mints:
                self.skip(",".join(mints), f"ambiguous graduation tx {signature[:16]}… ({len(mints)} candidate mints)")
            continue
        age = now_ts() - int(timestamp)
        if age > self.cfg.max_entry_age_seconds:
            self.skip(mints[0], f"graduation too old at detection ({age:.0f}s)")
            continue
        enter_at = int(timestamp) + self.cfg.entry_delay_seconds
        self.pending.append({"mint": mints[0], "graduated_ts": int(timestamp), "enter_at": enter_at, "signature": signature})
        self.state["pending"] = list(self.pending)
        log(f"DETECTED graduation {mints[0]} (age {age:.0f}s, entering at +{self.cfg.entry_delay_seconds:.0f}s)")


def _run(self):
    log(
        f"executor starting: mode={self.cfg.mode} "
        f"bundle_slot<={self.cfg.max_bundle_slot_pct:.0%} cluster<={self.cfg.max_cluster_pct:.0%} "
        f"log_only={int(self.cfg.bundle_log_only)}"
    )
    return _orig_run(self)


Config.__init__ = _config_init
entry_guard_reason = _guard
Executor.__init__ = _executor_init
Executor.poll_graduations = _poll
Executor.run = _run


def _patched_main() -> None:
    import json
    import sys
    cfg = Config()
    if "--print-config" in sys.argv:
        safe = {k: v for k, v in vars(cfg).items() if k != "wallet_key"}
        print(json.dumps(safe, indent=2))
        return
    Executor(cfg).run()


if __name__ == "__main__":
    _patched_main()
