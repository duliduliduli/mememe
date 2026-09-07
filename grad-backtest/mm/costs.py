"""Pure cost and value models from the market-making specification.

Nothing here touches the network; every function is deterministic so replay and paper
results can be reconciled by hand. Fractions are used throughout (0.0025, not 0.25%)."""
from __future__ import annotations

import math
from dataclasses import dataclass


# --- Round-trip swap economics -------------------------------------------------

def round_trip_multiplier(f_b: float, f_s: float, d_b: float = 0.0, d_s: float = 0.0) -> float:
    """K = (1 - f_b)(1 - f_s)(1 - d_b)(1 - d_s): fraction of notional surviving a round trip."""
    return (1 - f_b) * (1 - f_s) * (1 - d_b) * (1 - d_s)


def final_proceeds(q: float, g: float, k: float, c: float = 0.0) -> float:
    return q * (1 + g) * k - c


def net_pnl(q: float, g: float, k: float, c: float = 0.0) -> float:
    return q * ((1 + g) * k - 1) - c


def break_even_gain(q: float, k: float, c: float = 0.0) -> float:
    """Reference-price increase at which a round trip returns exactly the starting notional."""
    if q <= 0 or k <= 0:
        return math.inf
    return (1 + c / q) / k - 1


# --- Constant-product exit impact ----------------------------------------------

def cpmm_exit(quote_reserve: float, inventory_value: float) -> tuple[float, float]:
    """Quote received and average impact when selling `inventory_value` (spot-marked) into a
    constant-product pool holding `quote_reserve` of the quote asset, fees ignored."""
    if quote_reserve <= 0 or inventory_value <= 0:
        return 0.0, 0.0
    received = quote_reserve * inventory_value / (quote_reserve + inventory_value)
    return received, inventory_value / (quote_reserve + inventory_value)


# --- Liquidity-provision value models -----------------------------------------

def lp_gross_fee_income(position_value: float, pool_liquidity: float, volume: float, lp_fee_rate: float) -> float:
    """Approximate pro-rata fee income: share * volume * LP fee rate (full-range assumption)."""
    if pool_liquidity <= 0:
        return 0.0
    return position_value / pool_liquidity * volume * lp_fee_rate


def full_range_lp_value(initial_value: float, price_ratio: float) -> float:
    """Balanced 50/50 constant-product position after the token price moves by `price_ratio`."""
    return initial_value * math.sqrt(max(price_ratio, 0.0))


def hold_value(initial_value: float, price_ratio: float, token_fraction: float = 0.5) -> float:
    return initial_value * ((1 - token_fraction) + token_fraction * price_ratio)


@dataclass(frozen=True)
class RangePosition:
    """Uniform-liquidity range position in Uniswap-v3 style coordinates.

    DLMM bins with a spot (flat) shape are approximated by uniform liquidity across the
    price range. Amounts are in token units (x) and quote units (y)."""

    liquidity: float
    lower: float
    upper: float

    @staticmethod
    def open(value_quote: float, price: float, lower: float, upper: float) -> "RangePosition":
        """Deposit `value_quote` of total value at `price`; the split between token and quote
        follows from where price sits inside [lower, upper]."""
        if not (0 < lower < upper) or price <= 0 or value_quote <= 0:
            raise ValueError("invalid range or price")
        unit = RangePosition(1.0, lower, upper)
        x, y = unit.amounts(price)
        per_unit_value = x * price + y
        return RangePosition(value_quote / per_unit_value, lower, upper)

    def amounts(self, price: float) -> tuple[float, float]:
        sa, sb = math.sqrt(self.lower), math.sqrt(self.upper)
        if price <= self.lower:
            return self.liquidity * (sb - sa) / (sa * sb), 0.0
        if price >= self.upper:
            return 0.0, self.liquidity * (sb - sa)
        sp = math.sqrt(price)
        return self.liquidity * (sb - sp) / (sp * sb), self.liquidity * (sp - sa)

    def value(self, price: float) -> float:
        x, y = self.amounts(price)
        return x * price + y

    def in_range(self, price: float) -> bool:
        return self.lower < price < self.upper

    def token_fraction(self, price: float) -> float:
        x, y = self.amounts(price)
        total = x * price + y
        return x * price / total if total > 0 else 0.0

    def capital_efficiency(self) -> float:
        """Liquidity per dollar relative to a full-range position with the same value at the
        range midpoint: 1 / (1 - (lower/upper) ** 0.25)."""
        ratio = (self.lower / self.upper) ** 0.25
        return 1.0 / (1.0 - ratio) if ratio < 1 else math.inf


def symmetric_range(price: float, half_width: float) -> tuple[float, float]:
    """Geometric range [price/(1+w), price*(1+w)] for a fractional half-width w."""
    return price / (1 + half_width), price * (1 + half_width)


def loss_versus_rebalancing_rate(sigma: float) -> float:
    """LVR for a constant-product position per unit time as a fraction of position value:
    sigma^2 / 8 (Milionis et al.). Multiply by capital efficiency for a range position."""
    return sigma * sigma / 8.0


def projected_lp_edge(
    fee_yield_per_hour: float,
    lp_share: float,
    sigma_hourly: float,
    horizon_hours: float,
    in_range_probability: float,
    fixed_costs: float,
    position_value: float,
    model_error: float,
) -> float:
    """Projected net edge (fraction of position value) of providing liquidity for the horizon.

    fee_yield_per_hour: pool fees / pool TVL per hour, measured from the data API
    lp_share:            fraction of pool fees paid to LPs (1 - protocol share) * position share
    sigma_hourly:        realized hourly log-return volatility
    fixed_costs:         setup + withdrawal transactions, exit impact, in quote units"""
    fee_income = fee_yield_per_hour * lp_share * horizon_hours * in_range_probability
    drift_cost = loss_versus_rebalancing_rate(sigma_hourly) * horizon_hours
    fixed = fixed_costs / position_value if position_value > 0 else math.inf
    return fee_income - drift_cost - fixed - model_error


def in_range_probability(half_width: float, sigma_hourly: float, horizon_hours: float) -> float:
    """Probability a driftless log-normal price is still inside a symmetric range at the
    horizon; uses the reflection principle for a two-sided barrier via a normal approximation."""
    if sigma_hourly <= 0 or horizon_hours <= 0:
        return 1.0
    z = math.log(1 + half_width) / (sigma_hourly * math.sqrt(horizon_hours))
    # P(max_t |W_t| < z) = sum_k (-1)^k [Phi((2k+1) z) - Phi((2k-1) z)]; seven terms converge
    # to double precision for every z the strategy produces.
    inside = sum((-1) ** k * (_phi((2 * k + 1) * z) - _phi((2 * k - 1) * z)) for k in range(-3, 4))
    return max(0.0, min(1.0, inside))


def _phi(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


# --- Per-trade expectancy ------------------------------------------------------

def expected_pnl(win_probability: float, avg_win: float, avg_loss: float) -> float:
    return win_probability * avg_win - (1 - win_probability) * avg_loss


def required_return_for_fixed_cost(monthly_cost: float, capital: float) -> float:
    return math.inf if capital <= 0 else monthly_cost / capital
