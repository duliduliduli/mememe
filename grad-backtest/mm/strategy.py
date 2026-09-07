"""Strategy state machine, inventory controller, risk controller and shadow accounting.

The same classes drive counterfactual replay, the forward paper engine and live trading:
each strategy is fed one snapshot row at a time and keeps its own Portfolio. Fills come
from a Broker (PaperBroker by default, LiveBroker in live mode), so the accounting path is
identical everywhere and a strategy never sends a transaction itself."""
from __future__ import annotations

import math
import time as _time
from dataclasses import dataclass, field
from typing import Any

from .config import MMConfig
from .costs import RangePosition, in_range_probability, loss_versus_rebalancing_rate, projected_lp_edge, symmetric_range
from .execution import Broker, PaperBroker
from .regime import CRASH, DECAY, LIQUIDITY_WITHDRAWAL, STALE, Regime

OBSERVE = "OBSERVE"
PROVIDE = "PROVIDE_TWO_SIDED_LIQUIDITY"
REDUCE = "REDUCE_INVENTORY"
STAY_OUT = "STAY_OUT"


def _f(row: dict[str, Any], key: str, default: float | None = None) -> float | None:
    value = row.get(key)
    if value in (None, ""):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


# --- Accounting ----------------------------------------------------------------

@dataclass
class Event:
    ts: float
    mint: str
    strategy: str
    action: str
    reason: str
    price: float
    value: float
    detail: str = ""


@dataclass
class LpPosition:
    mint: str
    opened_ts: float
    rng: RangePosition
    entry_value: float
    entry_price: float
    fees_earned: float = 0.0
    costs_paid: float = 0.0
    peak_value: float = 0.0
    last_price: float = 0.0
    time_in_range_s: float = 0.0
    time_open_s: float = 0.0
    position_key: str = ""       # on-chain position account in live mode
    pool: str = ""
    rent_sol: float = 0.0
    signatures: list[str] = field(default_factory=list)

    def value(self, price: float) -> float:
        return self.rng.value(price) + self.fees_earned

    def token_fraction(self, price: float) -> float:
        return self.rng.token_fraction(price)


@dataclass
class SpotPosition:
    mint: str
    opened_ts: float
    tokens: float
    entry_value: float
    entry_price: float
    peak_price: float
    costs_paid: float = 0.0
    signature: str = ""

    def value(self, price: float) -> float:
        return self.tokens * price


@dataclass
class Portfolio:
    """Cash plus positions; NLV is cash + marked positions - liquidation cost."""
    strategy: str
    cash: float
    lp: dict[str, LpPosition] = field(default_factory=dict)
    spot: dict[str, SpotPosition] = field(default_factory=dict)
    realized: float = 0.0
    fees_earned: float = 0.0
    costs_paid: float = 0.0
    rebalances: int = 0
    trades: int = 0
    day_start_nlv: float | None = None
    day_key: str = ""
    daily_pnl: float = 0.0
    events: list[Event] = field(default_factory=list)
    pnl_by_mint: dict[str, float] = field(default_factory=dict)
    nlv_history: list[tuple[float, float]] = field(default_factory=list)
    worst_position: tuple[str, float] | None = None
    illiquid_inventory: float = 0.0

    def exposure(self, prices: dict[str, float]) -> float:
        total = 0.0
        for mint, pos in self.lp.items():
            price = prices.get(mint, pos.last_price)
            total += pos.rng.value(price) * pos.token_fraction(price)
        for mint, pos in self.spot.items():
            total += pos.value(prices.get(mint, pos.entry_price))
        return total

    def nlv(self, prices: dict[str, float], liquidation_pct: dict[str, float] | None = None) -> float:
        total = self.cash
        liq = liquidation_pct or {}
        for mint, pos in self.lp.items():
            price = prices.get(mint, pos.last_price)
            value = pos.value(price)
            total += value * (1 - liq.get(mint, 0.0) / 100 * pos.token_fraction(price))
        for mint, pos in self.spot.items():
            value = pos.value(prices.get(mint, pos.entry_price))
            total += value * (1 - liq.get(mint, 0.0) / 100)
        return total

    def mark(self, ts: float, prices: dict[str, float], liquidation_pct: dict[str, float] | None = None) -> float:
        value = self.nlv(prices, liquidation_pct)
        self.nlv_history.append((ts, value))
        if len(self.nlv_history) > 100_000:
            del self.nlv_history[: len(self.nlv_history) - 100_000]
        day = _time.strftime("%Y-%m-%d", _time.gmtime(ts))
        if day != self.day_key:
            self.day_key = day
            self.day_start_nlv = value
        self.daily_pnl = value - (self.day_start_nlv or value)
        return value

    def record_pnl(self, mint: str, pnl: float) -> None:
        self.pnl_by_mint[mint] = self.pnl_by_mint.get(mint, 0.0) + pnl
        if self.worst_position is None or pnl < self.worst_position[1]:
            self.worst_position = (mint, pnl)

    def log(self, ts: float, mint: str, action: str, reason: str, price: float, value: float, detail: str = "") -> None:
        self.events.append(Event(ts, mint, self.strategy, action, reason, price, value, detail))


# --- Shared helpers -------------------------------------------------------------

def lp_share_of_fees(row: dict[str, Any], cfg: MMConfig) -> float:
    protocol = _f(row, "dlmm_protocol_fee_pct")
    if protocol is not None and 0 <= protocol < 100:
        return 1 - protocol / 100
    return cfg.lp_fee_share


def interval_fee_yield(prev: dict[str, Any] | None, row: dict[str, Any]) -> float:
    """Pool fees earned over the interval as a fraction of pool TVL (all LPs, pre protocol cut)."""
    tvl = _f(row, "dlmm_tvl_usd") or 0.0
    if tvl <= 0:
        return 0.0
    cum_now, cum_prev = _f(row, "dlmm_cum_fees"), _f(prev or {}, "dlmm_cum_fees")
    if cum_now is not None and cum_prev is not None and cum_now >= cum_prev:
        return (cum_now - cum_prev) / tvl
    fees_1h = _f(row, "dlmm_fees_1h") or 0.0
    dt = (_f(row, "ts") or 0) - (_f(prev or {}, "ts") or 0) if prev else 0.0
    return fees_1h * max(dt, 0.0) / 3600.0 / tvl


SOFT_ERROR_PREFIXES = ("exit ladder",)


def hard_data_errors(row: dict[str, Any]) -> list[str]:
    """Snapshot errors that mean the row cannot be trusted. A missing exit quote is not one
    of them: it blocks new entries (the exit cost is unknown) but a position is not dumped
    at market because one quote request failed."""
    raw = str(row.get("errors") or "")
    return [e.strip() for e in raw.split(";") if e.strip() and not e.strip().startswith(SOFT_ERROR_PREFIXES)]


def exit_cost_pct(row: dict[str, Any], cfg: MMConfig, size_usd: float | None = None) -> float:
    """Impact plus one swap fee for liquidating `size_usd` of the token right now."""
    impact = _f(row, "impact_at_max_position_pct")
    if impact is None:
        return cfg.emergency_exit_impact_pct
    scale = (size_usd or cfg.max_position_usd) / cfg.max_position_usd
    fee = _f(row, "dlmm_base_fee_pct") or cfg.momentum_fee_pct_each_side
    return impact * scale + fee


# --- Risk controller ------------------------------------------------------------

class RiskController:
    """Has priority over strategy placement. Returns the reasons no exposure may be held."""

    def __init__(self, cfg: MMConfig) -> None:
        self.cfg = cfg

    def stops(self, row: dict[str, Any], regime: Regime, portfolio: Portfolio, prices: dict[str, float]) -> list[str]:
        cfg = self.cfg
        reasons = []
        if regime.label == STALE:
            reasons.append("stale data")
        if not _f(row, "price_usd"):
            reasons.append("no price")
        hard = hard_data_errors(row)
        if hard:
            reasons.append(f"data errors: {'; '.join(hard)}")
        if regime.label == LIQUIDITY_WITHDRAWAL:
            reasons.append("liquidity withdrawal")
        if regime.label == CRASH:
            reasons.append("crash")
        if regime.label == DECAY and regime.participation_ratio is not None and \
                regime.participation_ratio <= cfg.participation_drop_ratio:
            reasons.append("participation collapse")
        impact = _f(row, "impact_at_max_position_pct")
        if impact is not None and impact > cfg.emergency_exit_impact_pct:
            reasons.append(f"exit impact {impact:.2f}% > emergency {cfg.emergency_exit_impact_pct:.1f}%")
        if portfolio.daily_pnl <= -cfg.daily_loss_limit_usd:
            reasons.append("daily loss limit")
        return reasons

    def entry_blocks(self, row: dict[str, Any], regime: Regime, portfolio: Portfolio, prices: dict[str, float],
                     size_usd: float) -> list[str]:
        cfg = self.cfg
        reasons = self.stops(row, regime, portfolio, prices)
        if portfolio.exposure(prices) + size_usd * cfg.target_token_fraction > cfg.max_portfolio_exposure_usd:
            reasons.append("portfolio exposure cap")
        if size_usd * cfg.target_token_fraction > cfg.max_token_exposure_usd:
            reasons.append("token exposure cap")
        impact = _f(row, "impact_at_max_position_pct")
        if impact is None:
            reasons.append("no exit quote")
        elif impact > cfg.routine_exit_impact_pct:
            reasons.append(f"routine exit impact {impact:.2f}% > {cfg.routine_exit_impact_pct:.2f}%")
        if not regime.allows_entry:
            reasons.append(f"regime {regime.label}")
        return reasons


# --- Strategies ------------------------------------------------------------------

class Strategy:
    name = "base"

    def __init__(self, cfg: MMConfig, cash: float, broker: Broker | None = None) -> None:
        self.cfg = cfg
        self.broker: Broker = broker or PaperBroker(cfg)
        self.portfolio = Portfolio(self.name, cash)
        self.prev: dict[str, dict[str, Any]] = {}
        self.cooldown_until: dict[str, float] = {}
        self.state: dict[str, str] = {}

    def __getstate__(self) -> dict[str, Any]:
        # Brokers hold sessions and signers; the engine re-attaches them after unpickling.
        state = self.__dict__.copy()
        state["broker"] = None
        return state

    @property
    def is_live(self) -> bool:
        return getattr(self.broker, "name", "") == "live"

    def step(self, row: dict[str, Any], regime: Regime, prices: dict[str, float]) -> None:
        raise NotImplementedError

    def finish(self, row: dict[str, Any], regime: Regime) -> None:
        pass


class AdaptiveDLMM(Strategy):
    """Rank-1 candidate: moderate symmetric ranges, edge gate, inventory skew, stay-out state."""
    name = "adaptive_dlmm"

    def __init__(self, cfg: MMConfig, cash: float, half_width_pct: float | None = None, adaptive: bool = True,
                 broker: Broker | None = None) -> None:
        super().__init__(cfg, cash, broker)
        self.risk = RiskController(cfg)
        self.fixed_half_width = half_width_pct / 100 if half_width_pct else None
        self.adaptive = adaptive
        self.last_eval: dict[str, dict[str, Any]] = {}

    def half_width(self, regime: Regime) -> float:
        if self.fixed_half_width is not None:
            return self.fixed_half_width
        cfg = self.cfg
        raw = cfg.range_width_sigma * regime.sigma_hourly * math.sqrt(cfg.horizon_hours)
        return min(cfg.max_range_half_width_pct / 100, max(cfg.min_range_half_width_pct / 100, raw))

    def edge_components(self, row: dict[str, Any], regime: Regime, half_width: float, size: float) -> dict[str, float]:
        """The edge gate's terms as fractions of position value: fees earned in range, the
        loss-versus-rebalancing drift cost, fixed costs (setup, exit impact, opening swap)
        and the model-error haircut. edge = fees - drift - fixed - model."""
        cfg = self.cfg
        # Meteora reports fee_tvl_ratio in percent (0.2177 == 0.2177% per day).
        fee_yield_hour = (_f(row, "dlmm_fee_tvl_24h") or 0.0) / 100.0 / 24.0
        p_in = in_range_probability(half_width, regime.sigma_hourly, cfg.horizon_hours)
        fixed = cfg.tx_fee_usd * cfg.lp_setup_transactions
        fixed += size * cfg.target_token_fraction * exit_cost_pct(row, cfg, size * cfg.target_token_fraction) / 100
        fixed += size * 0.5 * (_f(row, "dlmm_base_fee_pct") or cfg.momentum_fee_pct_each_side) / 100  # opening swap
        fees = fee_yield_hour * lp_share_of_fees(row, cfg) * cfg.horizon_hours * p_in
        drift = loss_versus_rebalancing_rate(regime.sigma_hourly) * cfg.horizon_hours
        fixed_frac = fixed / size if size > 0 else math.inf
        model = cfg.model_error_pct / 100
        return {"edge": fees - drift - fixed_frac - model, "fees": fees, "drift": drift, "fixed": fixed_frac,
                "model": model, "p_in": p_in, "half_width": half_width, "size": size}

    def projected_edge(self, row: dict[str, Any], regime: Regime, half_width: float, size: float) -> float:
        return self.edge_components(row, regime, half_width, size)["edge"]

    def why_out(self) -> str | None:
        """One line explaining the current stay-out decision: the token with the best edge, its
        gate terms, and the blockers on every evaluated token. None when nothing was evaluated."""
        if not self.last_eval:
            return None
        best_mint, best = max(self.last_eval.items(), key=lambda kv: kv[1].get("edge", -math.inf))
        blocks: dict[str, int] = {}
        for ev in self.last_eval.values():
            for b in ev.get("blocks", []):
                key = b.split(" ")[0] if b.startswith(("regime", "routine", "exit")) else b
                blocks[key] = blocks.get(key, 0) + 1
        parts = [f"STAY_OUT best {best_mint[:8]} edge {best['edge'] * 100:+.2f}%/{self.cfg.horizon_hours:.0f}h"]
        if "fees" in best:
            parts.append(f"(fees {best['fees'] * 100:+.2f} drift {-best['drift'] * 100:+.2f} fixed {-best['fixed'] * 100:+.2f} "
                         f"model {-best['model'] * 100:+.2f}; p_in {best['p_in']:.2f} half_width {best['half_width']:.1%} "
                         f"sigma_h {best['sigma_hourly']:.2%} regime {best['regime']})")
        if blocks:
            parts.append("blocked: " + ", ".join(f"{k}x{v}" for k, v in sorted(blocks.items(), key=lambda kv: -kv[1])))
        parts.append(f"tokens {len(self.last_eval)}, positive edge {sum(1 for e in self.last_eval.values() if e.get('edge', 0) > 0)}")
        return " ".join(parts)

    def _accrue(self, pos: LpPosition, prev: dict[str, Any] | None, row: dict[str, Any], price: float) -> None:
        ts, prev_ts = _f(row, "ts") or 0.0, _f(prev or {}, "ts") or (_f(row, "ts") or 0.0)
        dt = max(0.0, ts - prev_ts)
        pos.time_open_s += dt
        if pos.rng.in_range(price):
            pos.time_in_range_s += dt
            fee = interval_fee_yield(prev, row) * lp_share_of_fees(row, self.cfg) * pos.rng.value(price)
            pos.fees_earned += fee
            self.portfolio.fees_earned += fee
        pos.last_price = price
        pos.peak_value = max(pos.peak_value, pos.value(price))

    def _close(self, pos: LpPosition, row: dict[str, Any], price: float, reason: str, action: str = "WITHDRAW") -> bool:
        cfg = self.cfg
        ts = _f(row, "ts") or 0.0
        try:
            fill = self.broker.close_lp(row, pos, price)
        except Exception as exc:  # noqa: BLE001 - keep the position and retry next tick
            self.portfolio.log(ts, pos.mint, "CLOSE_FAILED", f"{reason}: {exc}", price, pos.value(price))
            return False
        # Real claimed fees replace the accrued estimate; paper returns the estimate itself.
        self.portfolio.fees_earned += fill.fees_usd - pos.fees_earned
        pos.fees_earned = fill.fees_usd
        proceeds, cost = fill.proceeds_usd, fill.cost_usd
        pnl = proceeds - pos.entry_value
        pos.costs_paid += cost
        self.portfolio.costs_paid += cost
        self.portfolio.cash += proceeds
        self.portfolio.realized += pnl
        self.portfolio.record_pnl(pos.mint, pnl)
        self.portfolio.trades += 1
        self.portfolio.lp.pop(pos.mint, None)
        self.portfolio.log(ts, pos.mint, action, reason, price, proceeds,
                           f"pnl={pnl:.4f} fees={pos.fees_earned:.4f} cost={cost:.4f} "
                           f"in_range={pos.time_in_range_s / max(1, pos.time_open_s):.2f}"
                           + (f" sigs={','.join(s for s in fill.signatures if s)}" if fill.signatures else ""))
        self.cooldown_until[pos.mint] = ts + cfg.reentry_cooldown_minutes * 60
        return True

    def step(self, row: dict[str, Any], regime: Regime, prices: dict[str, float]) -> None:
        cfg = self.cfg
        mint = row["mint"]
        prev = self.prev.get(mint)
        price = _f(row, "price_usd") or 0.0
        ts = _f(row, "ts") or 0.0
        pos = self.portfolio.lp.get(mint)
        if pos and price > 0:
            self._accrue(pos, prev, row, price)
        stops = self.risk.stops(row, regime, self.portfolio, prices)
        if pos:
            if stops:
                self.state[mint] = STAY_OUT
                self._close(pos, row, price or pos.last_price, "; ".join(stops), "EMERGENCY_WITHDRAW")
            elif price >= pos.rng.upper:
                # Fully converted to quote: nothing left to lose, nothing left to earn.
                self.state[mint] = OBSERVE
                self._close(pos, row, price, "price above range (all quote)")
            elif pos.token_fraction(price) > cfg.target_token_fraction + cfg.inventory_band:
                # Too long the meme: withdraw and cut inventory; never add bids.
                self.state[mint] = REDUCE
                self._close(pos, row, price, f"inventory {pos.token_fraction(price):.0%} token > target+band", "REDUCE")
            elif regime.forces_exit or regime.label == "DECLINE":
                self.state[mint] = STAY_OUT if regime.forces_exit else OBSERVE
                self._close(pos, row, price, f"regime {regime.label}")
            else:
                self.state[mint] = PROVIDE
        else:
            self.state[mint] = STAY_OUT if stops else OBSERVE
            ev: dict[str, Any] = {"regime": regime.label, "sigma_hourly": regime.sigma_hourly, "blocks": list(stops)}
            if not self.adaptive_ok(row):
                ev["blocks"].append("no DLMM pool" if not row.get("dlmm_pool") else "pool liquidity")
            elif ts < self.cooldown_until.get(mint, 0.0):
                ev["blocks"].append("cooldown")
            if not stops and price > 0 and ts >= self.cooldown_until.get(mint, 0.0) and self.adaptive_ok(row):
                size = min(cfg.max_position_usd, self.portfolio.cash * 0.95)
                blocks = self.risk.entry_blocks(row, regime, self.portfolio, prices, size)
                ev["blocks"] = list(blocks)
                if size >= 1.0:
                    hw = self.half_width(regime)
                    ev.update(self.edge_components(row, regime, hw, size))
                    if not blocks and ev["edge"] > 0:
                        self._open(row, price, hw, size, ev["edge"])
            self.last_eval[mint] = ev
        self.prev[mint] = row

    def adaptive_ok(self, row: dict[str, Any]) -> bool:
        return bool(row.get("dlmm_pool")) and (_f(row, "dlmm_tvl_usd") or 0) >= self.cfg.min_pool_liquidity_usd

    def _open(self, row: dict[str, Any], price: float, half_width: float, size: float, edge: float) -> None:
        cfg = self.cfg
        mint, ts = row["mint"], _f(row, "ts") or 0.0
        lower, upper = symmetric_range(price, half_width)
        size = self.broker.deployable_usd(size)
        if size < 1.0:
            self.portfolio.log(ts, mint, "OPEN_SKIPPED", "no deployable capital", price, size)
            self.cooldown_until[mint] = ts + cfg.reentry_cooldown_minutes * 60
            return
        try:
            fill = self.broker.open_lp(row, size, half_width, lower, upper)
        except Exception as exc:  # noqa: BLE001
            self.portfolio.log(ts, mint, "OPEN_FAILED", str(exc), price, size)
            self.cooldown_until[mint] = ts + cfg.reentry_cooldown_minutes * 60
            return
        rng = RangePosition.open(fill.deployed_usd, price, fill.lower_usd, fill.upper_usd)
        pos = LpPosition(mint, ts, rng, size, price, costs_paid=fill.cost_usd, last_price=price,
                         position_key=fill.position_key, pool=row.get("dlmm_pool") or "", rent_sol=fill.rent_sol,
                         signatures=list(fill.signatures))
        pos.peak_value = pos.value(price)
        self.portfolio.cash -= size
        self.portfolio.costs_paid += fill.cost_usd
        self.portfolio.lp[mint] = pos
        self.portfolio.trades += 1
        self.state[mint] = PROVIDE
        self.portfolio.log(ts, mint, "OPEN", f"edge {edge * 100:.2f}% over {cfg.horizon_hours:.0f}h", price, size,
                           f"range=[{fill.lower_usd:.6g},{fill.upper_usd:.6g}] half_width={half_width:.1%}"
                           + (f" position={fill.position_key}" if self.is_live else ""))

    def finish(self, row: dict[str, Any], regime: Regime) -> None:
        for pos in list(self.portfolio.lp.values()):
            self._close(pos, row, pos.last_price, "end of data", "FINAL_MARK")


class FixedNarrowDLMM(AdaptiveDLMM):
    """Benchmark: fixed narrow range with immediate recentering; documented as a control."""
    name = "fixed_narrow_dlmm"

    def __init__(self, cfg: MMConfig, cash: float, broker: Broker | None = None) -> None:
        super().__init__(cfg, cash, half_width_pct=5.0, broker=broker)

    def step(self, row, regime, prices):
        super().step(row, regime, prices)
        self.cooldown_until[row["mint"]] = 0.0

    def projected_edge(self, row, regime, half_width, size):
        return 1.0  # the control never gates; it always recenters


class FullRangeCPMM(Strategy):
    """Control: balanced full-range position opened on the first usable row, held to the end.

    Fee share relative to the concentrated strategies is scaled down by the capital efficiency
    of the widest adaptive range (a full-range dollar backs far less active liquidity)."""
    name = "full_range_cpmm"

    def step(self, row, regime, prices):
        cfg = self.cfg
        mint, price = row["mint"], _f(row, "price_usd") or 0.0
        prev = self.prev.get(mint)
        pos = self.portfolio.lp.get(mint)
        if pos is None and price > 0 and row.get("dlmm_pool") and self.portfolio.cash >= 1.0 and mint not in self.state:
            size = min(cfg.max_position_usd, self.portfolio.cash)
            rng = RangePosition(1.0, price * 1e-9, price * 1e9)
            unit_value = rng.value(price)
            rng = RangePosition(size / unit_value, rng.lower, rng.upper)
            pos = LpPosition(mint, _f(row, "ts") or 0.0, rng, size, price, last_price=price)
            self.portfolio.cash -= size
            self.portfolio.lp[mint] = pos
            self.portfolio.trades += 1
            self.state[mint] = PROVIDE
            self.portfolio.log(pos.opened_ts, mint, "OPEN", "full-range control", price, size)
        if pos and price > 0:
            efficiency = RangePosition(1.0, *symmetric_range(1.0, cfg.max_range_half_width_pct / 100)).capital_efficiency()
            fee = interval_fee_yield(prev, row) * lp_share_of_fees(row, cfg) * pos.rng.value(price) / efficiency
            pos.fees_earned += fee
            self.portfolio.fees_earned += fee
            pos.last_price = price
        self.prev[mint] = row

    def finish(self, row, regime):
        for pos in list(self.portfolio.lp.values()):
            proceeds = pos.value(pos.last_price)
            pnl = proceeds - pos.entry_value
            self.portfolio.cash += proceeds
            self.portfolio.realized += pnl
            self.portfolio.record_pnl(pos.mint, pnl)
            self.portfolio.lp.pop(pos.mint)
            self.portfolio.log(_f(row, "ts") or 0.0, pos.mint, "FINAL_MARK", "end of data", pos.last_price, proceeds, f"pnl={pnl:.4f}")


class Momentum(Strategy):
    """Rank-3 challenger: breakout above the lookback high with volume confirmation, trailing stop."""
    name = "momentum"

    def __init__(self, cfg: MMConfig, cash: float, broker: Broker | None = None) -> None:
        super().__init__(cfg, cash, broker)
        self.risk = RiskController(cfg)
        self.history: dict[str, list[dict[str, Any]]] = {}

    def step(self, row, regime, prices):
        cfg = self.cfg
        mint, price, ts = row["mint"], _f(row, "price_usd") or 0.0, _f(row, "ts") or 0.0
        hist = self.history.setdefault(mint, [])
        pos = self.portfolio.spot.get(mint)
        stops = self.risk.stops(row, regime, self.portfolio, prices)
        if pos and price > 0:
            pos.peak_price = max(pos.peak_price, price)
            drawdown = (price / pos.peak_price - 1) * 100
            if stops or drawdown <= -cfg.momentum_trail_pct or regime.label in ("DECLINE", "CRASH"):
                self._close(pos, row, price, "; ".join(stops) if stops else f"trail {drawdown:.1f}%")
        elif price > 0 and len(hist) >= cfg.momentum_lookback and not stops and ts >= self.cooldown_until.get(mint, 0):
            window = hist[-cfg.momentum_lookback:]
            high = max(_f(r, "price_usd") or 0 for r in window)
            vols = [v for v in (_f(r, "volume_1h") for r in window) if v is not None]
            mean_vol = sum(vols) / len(vols) if vols else 0.0
            vol_now = _f(row, "volume_1h") or 0.0
            size = min(cfg.max_position_usd, self.portfolio.cash * 0.95)
            blocks = self.risk.entry_blocks(row, regime, self.portfolio, prices, size / max(cfg.target_token_fraction, 1e-9))
            if price >= high * (1 + cfg.momentum_breakout_pct / 100) and vol_now >= cfg.momentum_volume_confirm * mean_vol \
                    and not blocks and size >= 1.0:
                self._open(row, price, size, f"breakout > {high:.6g} with volume x{vol_now / mean_vol if mean_vol else 0:.1f}")
        hist.append(row)
        if len(hist) > cfg.momentum_lookback * 4:
            del hist[: len(hist) - cfg.momentum_lookback * 4]
        self.prev[mint] = row

    def _open(self, row: dict[str, Any], price: float, size: float, reason: str) -> None:
        cfg = self.cfg
        mint, ts = row["mint"], _f(row, "ts") or 0.0
        size = self.broker.deployable_usd(size)
        if size < 1.0:
            self.portfolio.log(ts, mint, "BUY_SKIPPED", "no deployable capital", price, size)
            self.cooldown_until[mint] = ts + cfg.reentry_cooldown_minutes * 60
            return
        try:
            fill = self.broker.buy(row, size)
        except Exception as exc:  # noqa: BLE001
            self.portfolio.log(ts, mint, "BUY_FAILED", str(exc), price, size)
            self.cooldown_until[mint] = ts + cfg.reentry_cooldown_minutes * 60
            return
        self.portfolio.spot[mint] = SpotPosition(mint, ts, fill.tokens, fill.value_usd, price, price,
                                                 costs_paid=fill.cost_usd, signature=fill.signature)
        self.portfolio.cash -= fill.value_usd
        self.portfolio.costs_paid += fill.cost_usd
        self.portfolio.trades += 1
        self.portfolio.log(ts, mint, "BUY", reason, price, fill.value_usd,
                           f"tokens={fill.tokens:.6g}" + (f" sig={fill.signature}" if fill.signature else ""))

    def _close(self, pos: SpotPosition, row, price, reason) -> bool:
        cfg = self.cfg
        ts = _f(row, "ts") or 0.0
        try:
            fill = self.broker.sell(row, pos.tokens, price)
        except Exception as exc:  # noqa: BLE001
            self.portfolio.log(ts, pos.mint, "SELL_FAILED", f"{reason}: {exc}", price, pos.value(price))
            return False
        proceeds, fee = fill.value_usd, fill.cost_usd
        pnl = proceeds - pos.entry_value
        self.portfolio.cash += proceeds
        self.portfolio.costs_paid += fee
        self.portfolio.realized += pnl
        self.portfolio.record_pnl(pos.mint, pnl)
        self.portfolio.trades += 1
        self.portfolio.spot.pop(pos.mint, None)
        self.cooldown_until[pos.mint] = ts + cfg.reentry_cooldown_minutes * 60
        self.portfolio.log(ts, pos.mint, "SELL", reason, price, proceeds,
                           f"pnl={pnl:.4f} cost={fee:.4f}" + (f" sig={fill.signature}" if fill.signature else ""))
        return True

    def finish(self, row, regime):
        for pos in list(self.portfolio.spot.values()):
            last = self.prev.get(pos.mint, row)
            self._close(pos, last, _f(last, "price_usd") or pos.entry_price, "end of data")


class Hold(Strategy):
    """Benchmark: buy the starting token allocation on the first row and hold."""
    name = "hold_50_50"

    def step(self, row, regime, prices):
        mint, price = row["mint"], _f(row, "price_usd") or 0.0
        if mint not in self.state and price > 0 and self.portfolio.cash >= 1.0:
            size = min(self.cfg.max_position_usd, self.portfolio.cash) * self.cfg.target_token_fraction
            self.portfolio.spot[mint] = SpotPosition(mint, _f(row, "ts") or 0.0, size / price, size, price, price)
            self.portfolio.cash -= size
            self.state[mint] = "HELD"
        self.prev[mint] = row

    def finish(self, row, regime):
        for pos in list(self.portfolio.spot.values()):
            price = _f(self.prev.get(pos.mint, row), "price_usd") or pos.entry_price
            proceeds = pos.value(price)
            self.portfolio.cash += proceeds
            self.portfolio.record_pnl(pos.mint, proceeds - pos.entry_value)
            self.portfolio.spot.pop(pos.mint)


class Cash(Strategy):
    name = "cash"

    def step(self, row, regime, prices):
        self.prev[row["mint"]] = row


def default_strategies(cfg: MMConfig, cash: float, live_broker: Broker | None = None) -> list[Strategy]:
    """Replay/paper: every strategy on paper. Live: the two candidates trade through `live_broker`
    while the controls and benchmarks keep running as shadows on the same rows."""
    return [
        AdaptiveDLMM(cfg, cash, broker=live_broker),
        FixedNarrowDLMM(cfg, cash),
        FullRangeCPMM(cfg, cash),
        Momentum(cfg, cash, broker=live_broker),
        Hold(cfg, cash),
        Cash(cfg, cash),
    ]
