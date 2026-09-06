"""Live execution layer: broker fills come from what the chain reports, engine control flags."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from mm.config import MMConfig, WSOL
from mm.execution import LAMPORTS, LiveBroker, PaperBroker, Sidecar
from mm.regime import Regime, SIDEWAYS
from mm.strategy import AdaptiveDLMM, LpPosition, Momentum, SpotPosition, default_strategies
from mm.costs import RangePosition

MINT = "Dz9mQ9NzkBcCsuGPFJ3r1bS4wgqKMHBPiVuniW8Mbonk"
POOL = "8ztFxjFPfVUtEf4SLSapcFj8GW2dxyUA9no2bLPq7H7V"


def row(price=0.01, **kw):
    base = {"ts": 1_800_000_000.0, "mint": MINT, "price_usd": price, "dlmm_pool": POOL, "dlmm_tvl_usd": 500_000.0,
            "dlmm_base_fee_pct": 1.0, "dlmm_protocol_fee_pct": 10.0, "dlmm_fee_tvl_24h": 20.0,
            "impact_at_max_position_pct": 0.05, "errors": ""}
    base.update(kw)
    return base


class FakeSigner:
    pubkey = "Fq1L2WWQZCRoLmF65xFXVfbkEAv6N29pagnidt7i9aJf"

    def sign(self, raw):
        return raw


def make_broker(cfg, sol_balance=1.0, token_balance=0, sol_price=100.0):
    src = MagicMock()
    src.jupiter.sol_price.return_value = sol_price
    src.jupiter.token.return_value = {"id": MINT, "decimals": 6}
    sidecar = MagicMock(spec=Sidecar)
    broker = LiveBroker(cfg, src, FakeSigner(), sidecar, log=lambda m: None)
    state = {"sol": sol_balance, "tokens": token_balance}

    def rpc(method, params):
        if method == "getBalance":
            return {"value": int(state["sol"] * LAMPORTS)}
        if method == "getTokenAccountsByOwner":
            return {"value": [{"account": {"data": {"parsed": {"info": {"tokenAmount": {"amount": str(state["tokens"])}}}}}}]}
        raise AssertionError(method)
    broker.rpc = rpc
    swaps = []

    def swap(input_mint, output_mint, amount_raw):
        swaps.append((input_mint, output_mint, amount_raw))
        if input_mint == WSOL:
            state["tokens"] += amount_raw * 10          # 0.01 USD per token at $100 SOL: 1 lamport-> 10 raw
            return amount_raw * 10, "SIG_BUY"
        state["tokens"] = 0
        return amount_raw // 10, "SIG_SELL"
    broker.swap = swap
    broker._state = state
    broker._swaps = swaps
    return broker, sidecar


class LiveBrokerTests(unittest.TestCase):
    def setUp(self):
        self.cfg = MMConfig(data_dir=Path(tempfile.mkdtemp()), bankroll_usd=87.0, max_position_usd=40.0, gas_reserve_sol=0.02)

    def test_deployable_capital_respects_bankroll_gas_reserve_and_rent(self):
        broker, _ = make_broker(self.cfg, sol_balance=0.5)   # $50 in wallet
        # 0.5 - 0.02 gas - 0.06 rent = 0.42 SOL = $42 > $40 requested
        self.assertAlmostEqual(broker.deployable_usd(40.0), 40.0)
        broker.deployed_usd = 60.0                            # bankroll remaining 27
        self.assertAlmostEqual(broker.deployable_usd(40.0), 27.0)
        broker._state["sol"] = 0.1                            # $2 after reserves
        self.assertAlmostEqual(broker.deployable_usd(40.0), 2.0)
        broker.rpc = lambda *a: (_ for _ in ()).throw(RuntimeError("rpc down"))
        self.assertEqual(broker.deployable_usd(40.0), 0.0)   # unreadable balance refuses

    def test_open_lp_buys_half_then_opens_range_from_sidecar_bins(self):
        broker, sidecar = make_broker(self.cfg)
        sidecar.post.return_value = {"position": "POSKEY", "signature": "SIG_OPEN", "minBinId": -10, "maxBinId": 10,
                                     "lowerPrice": 0.000095, "upperPrice": 0.000105, "rentSol": 0.0574}
        fill = broker.open_lp(row(), 40.0, 0.05, 0.0095, 0.0105)
        self.assertEqual(broker._swaps[0][:2], (WSOL, MINT))
        self.assertEqual(broker._swaps[0][2], int(20 / 100 * LAMPORTS))          # half the size in lamports
        kwargs = sidecar.post.call_args.kwargs
        self.assertEqual(kwargs["pool"], POOL)
        self.assertEqual(kwargs["quoteAmountRaw"], str(int(20 / 100 * LAMPORTS)))
        self.assertEqual(kwargs["tokenAmountRaw"], str(broker._state["tokens"]))
        self.assertEqual(fill.position_key, "POSKEY")
        self.assertAlmostEqual(fill.lower_usd, 0.0095)                           # pool price * SOL price
        self.assertAlmostEqual(fill.upper_usd, 0.0105)
        self.assertEqual(fill.signatures, ["SIG_BUY", "SIG_OPEN"])
        self.assertAlmostEqual(broker.deployed_usd, 40.0)
        self.assertLess(fill.deployed_usd, 40.0)                                 # opening swap fee deducted

    def test_close_lp_returns_chain_amounts_not_model_value(self):
        broker, sidecar = make_broker(self.cfg, token_balance=0)
        sidecar.post.return_value = {"quoteAmountRaw": str(int(0.15 * LAMPORTS)), "feeQuoteRaw": str(int(0.01 * LAMPORTS)),
                                     "tokenAmountRaw": "1500000", "feeTokenRaw": "0", "signatures": ["SIG_CLOSE"]}
        broker._state["tokens"] = 1_500_000                    # tokens came back to the wallet on close
        pos = LpPosition(MINT, 0.0, RangePosition.open(39.0, 0.01, 0.0095, 0.0105), 40.0, 0.01, position_key="POSKEY", pool=POOL)
        broker.deployed_usd = 40.0
        fill = broker.close_lp(row(), pos, 0.01)
        self.assertEqual(broker._swaps[-1][:2], (MINT, WSOL))
        # 0.16 SOL quote+fee back + 150000 lamports from the sell = 0.16015 SOL * $100
        self.assertAlmostEqual(fill.proceeds_usd, (0.16 + 150_000 / LAMPORTS) * 100, places=6)
        self.assertAlmostEqual(fill.fees_usd, 1.0)
        self.assertEqual(fill.signatures, ["SIG_CLOSE", "SIG_SELL"])
        self.assertEqual(broker.deployed_usd, 0.0)

    def test_sell_refuses_without_balance(self):
        broker, _ = make_broker(self.cfg, token_balance=0)
        with self.assertRaises(Exception):
            broker.sell(row(), 100.0, 0.01)


class StrategyWithLiveBrokerTests(unittest.TestCase):
    def setUp(self):
        self.cfg = MMConfig(data_dir=Path(tempfile.mkdtemp()), reentry_cooldown_minutes=30)

    def test_failed_open_logs_and_cools_down_without_touching_cash(self):
        broker = MagicMock()
        broker.name = "live"
        broker.deployable_usd.return_value = 40.0
        broker.open_lp.side_effect = RuntimeError("sidecar: simulation failed")
        strat = AdaptiveDLMM(self.cfg, 87.0, broker=broker)
        strat._open(row(), 0.01, 0.05, 40.0, 0.01)
        self.assertEqual(strat.portfolio.cash, 87.0)
        self.assertNotIn(MINT, strat.portfolio.lp)
        self.assertEqual(strat.portfolio.events[-1].action, "OPEN_FAILED")
        self.assertGreater(strat.cooldown_until[MINT], row()["ts"])

    def test_failed_close_keeps_the_position_for_retry(self):
        broker = MagicMock()
        broker.name = "live"
        broker.close_lp.side_effect = RuntimeError("sidecar: 429")
        strat = AdaptiveDLMM(self.cfg, 47.0, broker=broker)
        strat.portfolio.lp[MINT] = LpPosition(MINT, 0.0, RangePosition.open(39.0, 0.01, 0.0095, 0.0105), 40.0, 0.01,
                                              position_key="POSKEY", pool=POOL)
        self.assertFalse(strat._close(strat.portfolio.lp[MINT], row(), 0.01, "test"))
        self.assertIn(MINT, strat.portfolio.lp)
        self.assertEqual(strat.portfolio.events[-1].action, "CLOSE_FAILED")

    def test_live_fees_replace_the_accrued_estimate_on_close(self):
        broker = MagicMock()
        broker.name = "live"
        broker.close_lp.return_value = MagicMock(proceeds_usd=41.0, fees_usd=0.7, cost_usd=0.05, signatures=["S"])
        strat = AdaptiveDLMM(self.cfg, 47.0, broker=broker)
        pos = LpPosition(MINT, 0.0, RangePosition.open(39.0, 0.01, 0.0095, 0.0105), 40.0, 0.01, fees_earned=0.2)
        strat.portfolio.lp[MINT] = pos
        strat.portfolio.fees_earned = 0.2
        strat._close(pos, row(), 0.01, "test")
        self.assertAlmostEqual(strat.portfolio.fees_earned, 0.7)
        self.assertAlmostEqual(strat.portfolio.cash, 47.0 + 41.0)
        self.assertAlmostEqual(strat.portfolio.realized, 1.0)

    def test_momentum_spot_fill_uses_broker_amounts(self):
        broker = MagicMock()
        broker.name = "live"
        broker.deployable_usd.return_value = 40.0
        broker.buy.return_value = MagicMock(tokens=3900.0, value_usd=40.0, cost_usd=0.41, signature="SIG")
        strat = Momentum(self.cfg, 87.0, broker=broker)
        strat._open(row(), 0.01, 40.0, "breakout")
        self.assertEqual(strat.portfolio.spot[MINT].tokens, 3900.0)
        self.assertAlmostEqual(strat.portfolio.cash, 47.0)
        self.assertEqual(strat.portfolio.events[-1].action, "BUY")

    def test_default_strategies_route_only_the_candidates_live(self):
        live = MagicMock()
        live.name = "live"
        strategies = default_strategies(self.cfg, 87.0, live_broker=live)
        names = {s.name: s.broker for s in strategies}
        self.assertIs(names["adaptive_dlmm"], live)
        self.assertIs(names["momentum"], live)
        self.assertIsInstance(names["fixed_narrow_dlmm"], PaperBroker)
        self.assertIsInstance(names["full_range_cpmm"], PaperBroker)

    def test_pickling_drops_brokers_so_engines_reattach_them(self):
        import pickle
        strat = Momentum(self.cfg, 87.0, broker=MagicMock())
        restored = pickle.loads(pickle.dumps(strat))
        self.assertIsNone(restored.broker)


class LiveEngineTests(unittest.TestCase):
    def make_engine(self):
        from mm.live import LiveEngine
        cfg = MMConfig(data_dir=Path(tempfile.mkdtemp()), poll_seconds=1)
        sidecar = MagicMock(spec=Sidecar)
        sidecar.health.return_value = {"ok": True, "wallet": FakeSigner.pubkey}
        sidecar.get.return_value = {"tracked": [], "discovered": []}
        src = MagicMock()
        src.jupiter.sol_price.return_value = 100.0
        src.jupiter.token.return_value = {"id": MINT, "decimals": 6}
        engine = LiveEngine.__new__(LiveEngine)
        engine.signer = FakeSigner()
        engine.sidecar_proc = None
        engine.sidecar = sidecar
        engine.sources = src
        engine.broker = LiveBroker(cfg, src, engine.signer, sidecar, log=lambda m: None)
        engine.broker.rpc = lambda method, params: {"value": int(0.5 * LAMPORTS)} if method == "getBalance" else {"value": []}
        engine.draining = False
        from mm.paper import PaperEngine
        PaperEngine.__init__(engine, cfg, src, log=lambda m: None)
        return engine, cfg, sidecar

    def test_reconcile_drops_positions_missing_on_chain(self):
        engine, cfg, sidecar = self.make_engine()
        adaptive = next(s for s in engine.engine.strategies if s.name == "adaptive_dlmm")
        adaptive.portfolio.lp[MINT] = LpPosition(MINT, 0.0, RangePosition.open(39.0, 0.01, 0.0095, 0.0105), 40.0, 0.01,
                                                 position_key="GONE", pool=POOL, last_price=0.01)
        adaptive.portfolio.cash = 47.0
        sidecar.get.return_value = {"tracked": [{"position": "GONE", "missing": True}], "discovered": []}
        engine.reconcile()
        self.assertNotIn(MINT, adaptive.portfolio.lp)
        self.assertEqual(adaptive.portfolio.events[-1].action, "CLOSE_EXTERNAL")
        self.assertEqual(engine.broker.deployed_usd, 0.0)

    def test_reconcile_keeps_live_positions_and_counts_deployed_capital(self):
        engine, cfg, sidecar = self.make_engine()
        adaptive = next(s for s in engine.engine.strategies if s.name == "adaptive_dlmm")
        adaptive.portfolio.lp[MINT] = LpPosition(MINT, 0.0, RangePosition.open(39.0, 0.01, 0.0095, 0.0105), 40.0, 0.01,
                                                 position_key="ALIVE", pool=POOL, last_price=0.01)
        sidecar.get.return_value = {"tracked": [{"position": "ALIVE", "lowerBinId": -5, "upperBinId": 5}], "discovered": []}
        engine.reconcile()
        self.assertIn(MINT, adaptive.portfolio.lp)
        self.assertAlmostEqual(engine.broker.deployed_usd, 40.0)

    def test_panic_flag_closes_live_positions_then_drains(self):
        engine, cfg, sidecar = self.make_engine()
        momentum = next(s for s in engine.engine.strategies if s.name == "momentum")
        momentum.portfolio.spot[MINT] = SpotPosition(MINT, 0.0, 3900.0, 40.0, 0.01, 0.01)
        momentum.broker = MagicMock(name="live")
        momentum.broker.name = "live"
        momentum.broker.sell.return_value = MagicMock(tokens=3900.0, value_usd=38.0, cost_usd=0.4, signature="S")
        engine.engine.history[MINT] = [row()]
        engine.engine.prices[MINT] = 0.01
        cfg.panic_flag.touch()
        engine.check_flags()
        self.assertNotIn(MINT, momentum.portfolio.spot)
        self.assertFalse(cfg.panic_flag.exists())
        self.assertTrue(cfg.stop_flag.exists())
        self.assertTrue(engine.draining)
        self.assertTrue(engine.broker.draining)


if __name__ == "__main__":
    unittest.main()
