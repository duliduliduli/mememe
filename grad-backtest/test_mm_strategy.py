"""State machine, inventory direction, risk controller and replay accounting on synthetic paths."""
import math
import random
import tempfile
import unittest
from pathlib import Path

from mm.config import MMConfig
from mm.costs import RangePosition, symmetric_range
from mm.regime import CRASH, LIQUIDITY_WITHDRAWAL, SIDEWAYS, STALE, Regime, classify
from mm.replay import replay
from mm.strategy import (PROVIDE, REDUCE, STAY_OUT, AdaptiveDLMM, FullRangeCPMM, Momentum, RiskController,
                         interval_fee_yield)

MINT = "Mint1111111111111111111111111111111111111111"
T0 = 1_800_000_000.0


def row(i, price, fees_cum=None, tvl=250_000.0, fee_tvl_24h=20.0, volume=5000.0, traders=100, liquidity=300_000.0,
        impact=0.05, errors=""):
    return {"ts": T0 + i * 60, "mint": MINT, "symbol": "MEME", "price_usd": price, "pool_liquidity_usd": liquidity,
            "dlmm_pool": "DLMM1111", "dlmm_tvl_usd": tvl, "dlmm_cum_fees": fees_cum if fees_cum is not None else i * 10.0,
            "dlmm_fees_1h": 600.0, "dlmm_fee_tvl_24h": fee_tvl_24h, "dlmm_protocol_fee_pct": 10.0, "dlmm_base_fee_pct": 1.0,
            "volume_1h": volume, "buy_volume_1h": volume / 2, "sell_volume_1h": volume / 2, "traders_1h": traders,
            "impact_at_max_position_pct": impact, "errors": errors}


def path(prices, **kw):
    return [row(i, p, **kw) for i, p in enumerate(prices)]


def sideways(n=80, seed=1, vol=0.004):
    rng = random.Random(seed)
    price, out = 1.0, []
    for _ in range(n):
        price *= math.exp(rng.gauss(0, vol))
        out.append(price)
    return out


class RegimeTests(unittest.TestCase):
    def setUp(self):
        self.cfg = MMConfig(data_dir=Path(tempfile.mkdtemp()))

    def test_sideways_and_stale(self):
        rows = path(sideways())
        self.assertEqual(classify(rows, self.cfg).label, SIDEWAYS)
        self.assertEqual(classify(rows, self.cfg, now=rows[-1]["ts"] + 10_000).label, STALE)

    def test_crash_and_liquidity_withdrawal(self):
        prices = [1.0 * (1 - 0.01 * i) for i in range(30)]
        self.assertEqual(classify(path(prices), self.cfg).label, CRASH)
        rows = path(sideways(40))
        for r in rows[-10:]:
            r["pool_liquidity_usd"] = 100_000.0
        self.assertEqual(classify(rows, self.cfg).label, LIQUIDITY_WITHDRAWAL)


class InventoryDirectionTests(unittest.TestCase):
    """When long too much meme the bot must reduce, never add bids (the Grok correction)."""

    def setUp(self):
        self.cfg = MMConfig(data_dir=Path(tempfile.mkdtemp()), reentry_cooldown_minutes=0, min_range_half_width_pct=5,
                            max_range_half_width_pct=5, model_error_pct=0.0)

    def test_provides_in_sideways_then_reduces_when_price_falls_into_token(self):
        strat = AdaptiveDLMM(self.cfg, 87.0)
        prices = sideways(40)
        regime = Regime(SIDEWAYS, sigma_hourly=0.02)
        for r in path(prices):
            strat.step(r, regime, {MINT: r["price_usd"]})
        self.assertEqual(strat.state[MINT], PROVIDE)
        pos = strat.portfolio.lp[MINT]
        self.assertGreater(pos.fees_earned, 0)
        # Price sinks 4.5%: position becomes mostly token -> REDUCE, no new bids.
        low = prices[-1] * 0.955
        strat.step(row(41, low), regime, {MINT: low})
        self.assertEqual(strat.state[MINT], REDUCE)
        self.assertNotIn(MINT, strat.portfolio.lp)
        self.assertEqual(strat.portfolio.events[-1].action, "REDUCE")
        self.assertLess(strat.portfolio.realized, 0)

    def test_price_above_range_withdraws_all_quote_without_loss(self):
        strat = AdaptiveDLMM(self.cfg, 87.0)
        regime = Regime(SIDEWAYS, sigma_hourly=0.02)
        prices = sideways(40)
        for r in path(prices):
            strat.step(r, regime, {MINT: r["price_usd"]})
        opened = strat.portfolio.lp[MINT]
        high = opened.rng.upper * 1.01
        strat.step(row(41, high), regime, {MINT: high})
        self.assertNotIn(MINT, strat.portfolio.lp)
        last = strat.portfolio.events[-1]
        self.assertEqual(last.action, "WITHDRAW")
        self.assertIn("price above range", last.reason)
        pnl = float(last.detail.split("pnl=")[1].split()[0])
        self.assertGreater(pnl, 0)

    def test_edge_gate_blocks_when_fees_cannot_cover_drift(self):
        strat = AdaptiveDLMM(self.cfg, 87.0)
        regime = Regime(SIDEWAYS, sigma_hourly=0.30)   # 30%/hour vol, 1%/day fee yield: no edge
        for r in path(sideways(40), fee_tvl_24h=1.0):
            strat.step(r, regime, {MINT: r["price_usd"]})
        self.assertNotIn(MINT, strat.portfolio.lp)


class RiskControllerTests(unittest.TestCase):
    def setUp(self):
        self.cfg = MMConfig(data_dir=Path(tempfile.mkdtemp()))

    def test_stops(self):
        strat = AdaptiveDLMM(self.cfg, 87.0)
        rc = RiskController(self.cfg)
        p = strat.portfolio
        self.assertIn("stale data", rc.stops(row(0, 1.0), Regime(STALE), p, {}))
        self.assertIn("crash", rc.stops(row(0, 1.0), Regime(CRASH), p, {}))
        self.assertTrue(any("exit impact" in s for s in rc.stops(row(0, 1.0, impact=5.0), Regime(SIDEWAYS), p, {})))
        self.assertTrue(any("data errors" in s for s in rc.stops(row(0, 1.0, errors="rpc down"), Regime(SIDEWAYS), p, {})))
        p.daily_pnl = -self.cfg.daily_loss_limit_usd
        self.assertIn("daily loss limit", rc.stops(row(0, 1.0), Regime(SIDEWAYS), p, {}))

    def test_missing_exit_quote_blocks_entry_but_does_not_force_exit(self):
        rc = RiskController(self.cfg)
        p = AdaptiveDLMM(self.cfg, 87.0).portfolio
        r = row(0, 1.0, impact=None, errors="exit ladder returned no usable quote")
        self.assertEqual(rc.stops(r, Regime(SIDEWAYS), p, {}), [])
        self.assertIn("no exit quote", rc.entry_blocks(r, Regime(SIDEWAYS), p, {}, 40.0))
        r = row(0, 1.0, impact=None, errors="exit ladder: 429; rpc down")
        self.assertTrue(any("rpc down" in s and "exit ladder" not in s for s in rc.stops(r, Regime(SIDEWAYS), p, {})))

    def test_adaptive_explains_why_it_stays_out(self):
        strat = AdaptiveDLMM(self.cfg, 87.0)
        self.assertIsNone(strat.why_out())
        # 50%/h realized vol: the drift term swamps any fee yield, so the gate says no.
        strat.step(row(0, 1.0, impact=0.05), Regime(SIDEWAYS, sigma_hourly=0.5), {})
        line = strat.why_out()
        self.assertIsNotNone(line)
        self.assertIn("STAY_OUT best", line)
        self.assertIn("drift", line)
        self.assertEqual(strat.portfolio.lp, {})

    def test_emergency_withdraw_on_stop(self):
        cfg = MMConfig(data_dir=Path(tempfile.mkdtemp()), reentry_cooldown_minutes=0, model_error_pct=0.0)
        strat = AdaptiveDLMM(cfg, 87.0)
        for r in path(sideways(40)):
            strat.step(r, Regime(SIDEWAYS, sigma_hourly=0.02), {MINT: r["price_usd"]})
        self.assertIn(MINT, strat.portfolio.lp)
        strat.step(row(41, 1.0), Regime(LIQUIDITY_WITHDRAWAL), {MINT: 1.0})
        self.assertEqual(strat.state[MINT], STAY_OUT)
        self.assertEqual(strat.portfolio.events[-1].action, "EMERGENCY_WITHDRAW")

    def test_exposure_caps_block_entry(self):
        cfg = MMConfig(data_dir=Path(tempfile.mkdtemp()), max_token_exposure_usd=5.0)
        rc = RiskController(cfg)
        blocks = rc.entry_blocks(row(0, 1.0), Regime(SIDEWAYS), AdaptiveDLMM(cfg, 87.0).portfolio, {}, 40.0)
        self.assertIn("token exposure cap", blocks)


class AccountingTests(unittest.TestCase):
    def test_interval_fee_yield_uses_cumulative_difference(self):
        prev, cur = row(0, 1.0, fees_cum=100.0), row(1, 1.0, fees_cum=125.0)
        self.assertAlmostEqual(interval_fee_yield(prev, cur), 25.0 / 250_000.0)
        cur["dlmm_cum_fees"] = None
        self.assertAlmostEqual(interval_fee_yield(prev, cur), 600.0 * 60 / 3600 / 250_000.0)

    def test_full_range_control_tracks_sqrt_rule_without_fees(self):
        cfg = MMConfig(data_dir=Path(tempfile.mkdtemp()))
        strat = FullRangeCPMM(cfg, 40.0)
        rows = path([1.0, 1.2, 1.5, 0.5], fees_cum=0.0)
        for r in rows:
            r["dlmm_cum_fees"] = 0.0
            r["dlmm_fees_1h"] = 0.0
            strat.step(r, Regime(SIDEWAYS), {MINT: r["price_usd"]})
        strat.finish(rows[-1], Regime(SIDEWAYS))
        self.assertAlmostEqual(strat.portfolio.cash, 40.0 * math.sqrt(0.5), places=3)

    def test_momentum_buys_breakout_and_trails_out(self):
        cfg = MMConfig(data_dir=Path(tempfile.mkdtemp()), momentum_lookback=5, reentry_cooldown_minutes=0)
        strat = Momentum(cfg, 87.0)
        # Buy at the +8% breakout, ride to 1.4, trail out at 1.2 (-14% from the peak). The pool's
        # 1% fee and measured impact are paid on both sides, so the run must be worth more than that.
        prices = [1.0] * 6 + [1.08, 1.3, 1.4, 1.2]
        rows = path(prices)
        for i, r in enumerate(rows):
            r["volume_1h"] = 20_000 if i >= 6 else 5000
            strat.step(r, Regime(SIDEWAYS), {MINT: r["price_usd"]})
        actions = [e.action for e in strat.portfolio.events]
        self.assertEqual(actions, ["BUY", "SELL"])
        self.assertGreater(strat.portfolio.realized, 0)

    def test_replay_report_covers_every_strategy_and_conserves_cash_strategy(self):
        cfg = MMConfig(data_dir=Path(tempfile.mkdtemp()), reentry_cooldown_minutes=0)
        report = replay(cfg, {MINT: path(sideways(120, seed=3))}, starting_cash=87.0)
        names = set(report["strategies"])
        self.assertEqual(names, {"adaptive_dlmm", "fixed_narrow_dlmm", "full_range_cpmm", "momentum", "hold_50_50", "cash"})
        self.assertEqual(report["strategies"]["cash"]["final_nlv"], 87.0)
        for metrics in report["strategies"].values():
            for key in ("max_drawdown_pct", "fees_earned", "costs_paid", "trades", "profit_factor", "pnl_by_mint"):
                self.assertIn(key, metrics)
        self.assertEqual(report["ranking"][0], max(report["strategies"], key=lambda k: report["strategies"][k]["final_nlv"]))


if __name__ == "__main__":
    unittest.main()
