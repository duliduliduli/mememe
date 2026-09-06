"""Market-state classification from recorded snapshots.

Regimes follow the specification's list. Classification is deterministic and only uses
information present at the classification time, so replay and paper produce identical
labels for identical series."""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from .config import MMConfig

SIDEWAYS = "SIDEWAYS"
UP_DRIFT = "UP_DRIFT"
DECLINE = "DECLINE"
PUMP_JUMP = "PUMP_JUMP"
CRASH = "CRASH"
CHOP = "CHOP"
DECAY = "DECAY"
LIQUIDITY_WITHDRAWAL = "LIQUIDITY_WITHDRAWAL"
STALE = "STALE"
WARMUP = "WARMUP"


@dataclass
class Regime:
    label: str
    sigma_hourly: float = 0.0
    drift_pct: float = 0.0
    last_return_sigma: float = 0.0
    volume_ratio: float | None = None
    liquidity_change_pct: float | None = None
    participation_ratio: float | None = None
    flow_imbalance: float | None = None
    reversals: int = 0
    reasons: list[str] = field(default_factory=list)

    @property
    def allows_entry(self) -> bool:
        return self.label in (SIDEWAYS, CHOP, UP_DRIFT)

    @property
    def forces_exit(self) -> bool:
        return self.label in (CRASH, LIQUIDITY_WITHDRAWAL, STALE, DECAY)


def _f(row: dict[str, Any], key: str) -> float | None:
    value = row.get(key)
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def realized_sigma_hourly(rows: list[dict[str, Any]]) -> float:
    """Sample standard deviation of log returns, scaled to one hour from the median spacing."""
    prices = [(_f(r, "ts"), _f(r, "price_usd")) for r in rows]
    prices = [(t, p) for t, p in prices if t and p and p > 0]
    if len(prices) < 3:
        return 0.0
    rets = [math.log(prices[i][1] / prices[i - 1][1]) for i in range(1, len(prices))]
    gaps = sorted(prices[i][0] - prices[i - 1][0] for i in range(1, len(prices)))
    median_gap = gaps[len(gaps) // 2] or 1.0
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / max(1, len(rets) - 1)
    return math.sqrt(var) * math.sqrt(3600.0 / median_gap)


def classify(rows: list[dict[str, Any]], cfg: MMConfig, now: float | None = None) -> Regime:
    """Classify the most recent state given a chronological snapshot series."""
    rows = [r for r in rows if _f(r, "price_usd")]
    if len(rows) < max(5, cfg.lookback_points // 3):
        return Regime(WARMUP, reasons=[f"{len(rows)} points"])
    window = rows[-cfg.lookback_points:]
    latest = window[-1]
    ts = _f(latest, "ts") or 0.0
    if now is not None and now - ts > cfg.stale_data_seconds:
        return Regime(STALE, reasons=[f"last snapshot {now - ts:.0f}s old"])

    sigma = realized_sigma_hourly(window)
    first, last = _f(window[0], "price_usd") or 0, _f(latest, "price_usd") or 0
    drift_pct = (last / first - 1) * 100 if first else 0.0
    prev = _f(window[-2], "price_usd") or last
    last_ret = math.log(last / prev) if prev and last else 0.0
    gap = (ts - (_f(window[-2], "ts") or ts)) or 1.0
    per_step_sigma = sigma * math.sqrt(gap / 3600.0) if sigma else 0.0
    last_return_sigma = last_ret / per_step_sigma if per_step_sigma else 0.0

    half = max(2, len(window) // 2)
    def mean(key: str, part: list[dict[str, Any]]) -> float | None:
        vals = [v for v in (_f(r, key) for r in part) if v is not None]
        return sum(vals) / len(vals) if vals else None
    early_vol, late_vol = mean("volume_1h", window[:half]), mean("volume_1h", window[half:])
    volume_ratio = late_vol / early_vol if early_vol and late_vol is not None else None
    early_liq, late_liq = _f(window[0], "pool_liquidity_usd"), _f(latest, "pool_liquidity_usd")
    liq_change = (late_liq / early_liq - 1) * 100 if early_liq and late_liq is not None else None
    early_tr, late_tr = mean("traders_1h", window[:half]), mean("traders_1h", window[half:])
    participation_ratio = late_tr / early_tr if early_tr and late_tr is not None else None
    buy, sell = _f(latest, "buy_volume_1h"), _f(latest, "sell_volume_1h")
    flow = (buy - sell) / (buy + sell) if buy is not None and sell is not None and buy + sell > 0 else None

    reversals = 0
    signs = []
    for i in range(1, len(window)):
        a, b = _f(window[i - 1], "price_usd"), _f(window[i], "price_usd")
        if a and b and a != b:
            signs.append(1 if b > a else -1)
    reversals = sum(1 for i in range(1, len(signs)) if signs[i] != signs[i - 1])

    regime = Regime(SIDEWAYS, sigma, drift_pct, last_return_sigma, volume_ratio, liq_change,
                    participation_ratio, flow, reversals)
    if liq_change is not None and liq_change <= -cfg.liquidity_drop_pct:
        regime.label = LIQUIDITY_WITHDRAWAL
        regime.reasons.append(f"pool liquidity {liq_change:.0f}% over lookback")
    elif drift_pct <= cfg.crash_pct:
        regime.label = CRASH
        regime.reasons.append(f"price {drift_pct:.0f}% over lookback")
    elif abs(last_return_sigma) >= cfg.jump_sigma and per_step_sigma > 0:
        regime.label = PUMP_JUMP
        regime.reasons.append(f"last move {last_return_sigma:.1f} sigma")
    elif (volume_ratio is not None and volume_ratio <= cfg.volume_decay_ratio) or \
         (participation_ratio is not None and participation_ratio <= cfg.participation_drop_ratio):
        regime.label = DECAY
        regime.reasons.append(f"volume ratio {volume_ratio}, participation ratio {participation_ratio}")
    elif drift_pct <= cfg.decline_drift_pct:
        regime.label = DECLINE
        regime.reasons.append(f"price {drift_pct:.1f}% over lookback")
    elif drift_pct >= cfg.rally_drift_pct:
        regime.label = UP_DRIFT
        regime.reasons.append(f"price +{drift_pct:.1f}% over lookback")
    elif signs and reversals >= 0.6 * len(signs):
        regime.label = CHOP
        regime.reasons.append(f"{reversals} reversals in {len(signs)} moves")
    return regime
