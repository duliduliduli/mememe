import csv
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from grad_backtest import Candle, apply_costs, simulate_trade


class BacktestScaleOutTests(unittest.TestCase):
    def test_scale_out_then_take_profit_blends(self):
        candles = [
            Candle(100, 100, 150, 99, 145, 1),   # crosses +40% -> half sells at 140
            Candle(160, 145, 180, 140, 175, 1),  # remainder hits +75% TP at 175
        ]
        r = simulate_trade("m", 0, "p", "pump", 100, 100, candles, 0.75, 0.30, 30, 0.03,
                           scale_out_at=0.40, scale_out_fraction=0.5)
        self.assertEqual(r.exit_reason, "take_profit")
        self.assertEqual(r.scale_out_price, 140)
        self.assertAlmostEqual(r.net_return, apply_costs(100, 140 * 0.5 + 175 * 0.5, 0.03)[1])

    def test_scale_out_rescues_a_reversal(self):
        # The WTF shape: up 40%+, then collapses to the stop. Without scale-out this is -30%.
        candles = [
            Candle(100, 100, 150, 99, 145, 1),
            Candle(160, 145, 146, 60, 62, 1),
        ]
        plain = simulate_trade("m", 0, "p", "pump", 100, 100, candles, 0.75, 0.30, 30, 0.03)
        scaled = simulate_trade("m", 0, "p", "pump", 100, 100, candles, 0.75, 0.30, 30, 0.03,
                                scale_out_at=0.40, scale_out_fraction=0.5)
        self.assertEqual(plain.exit_price, 70)
        self.assertEqual(scaled.exit_reason, "stop_loss")
        self.assertAlmostEqual(scaled.gross_return, (140 * 0.5 + 70 * 0.5) / 100 - 1)  # +5% vs -30%
        self.assertGreater(scaled.net_return, plain.net_return)

    def test_same_candle_stop_wins_adversely(self):
        candles = [Candle(100, 100, 150, 60, 62, 1)]
        r = simulate_trade("m", 0, "p", "pump", 100, 100, candles, 0.75, 0.30, 30, 0.03, scale_out_at=0.40)
        self.assertEqual(r.exit_reason, "stop_loss")
        self.assertEqual(r.scale_out_price, 0.0)

    def test_disabled_by_default(self):
        candles = [Candle(100, 100, 180, 99, 170, 1)]
        r = simulate_trade("m", 0, "p", "pump", 100, 100, candles, 0.75, 0.30, 30, 0.03)
        self.assertEqual(r.scale_out_price, 0.0)
        self.assertEqual(r.exit_price, 175)


class ExecutorScaleOutTests(unittest.TestCase):
    def _executor(self, extra_env):
        self.tmp = tempfile.mkdtemp(prefix="grad-scale-test-")
        env = {"DATA_DIR": self.tmp, "HELIUS_API_KEY": "x", "MIGRATION_ADDRESS": "y", **extra_env}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        import importlib
        import executor
        importlib.reload(executor)
        self.executor = executor
        return executor.Executor(executor.Config())

    def _seed(self, ex, tokens=1000, position_usd=5.0):
        ex.state["positions"] = [{
            "mint": "MINT", "tokens": tokens, "position_usd": position_usd, "opened_ts": time.time(),
            "opened_at": "t0", "buy_signature": "", "peak_usd": position_usd,
        }]

    def _trades(self):
        with open(Path(self.tmp) / "live_trades.csv") as fh:
            return list(csv.DictReader(fh))

    def test_paper_scale_out_then_remainder_take_profit(self):
        ex = self._executor({"SCALE_OUT_AT": "0.4", "SCALE_OUT_FRACTION": "0.5"})
        self._seed(ex)
        start_balance = ex.state["paper_balance_usd"]
        # sol_price=100: 1000 tokens quote to 0.07 SOL = $7.00 (+40%) -> scale-out fires
        ex.jup.quote = lambda mint, out, amount, **kw: {"outAmount": str(amount * 70_000)}
        ex.manage_positions(100.0, panic=False)
        pos = ex.state["positions"][0]
        self.assertTrue(pos["scaled_out"])
        self.assertEqual(pos["tokens"], 500)
        self.assertAlmostEqual(pos["position_usd"], 2.5)
        self.assertAlmostEqual(ex.state["paper_balance_usd"], start_balance + 3.5)
        self.assertAlmostEqual(ex.state["daily"]["realized_pnl_usd"], 1.0)
        rows = self._trades()
        self.assertEqual(rows[0]["exit_reason"], "scale_out")
        self.assertAlmostEqual(float(rows[0]["net_return"]), 0.4)

        # Remainder now $3.50 on a $2.50 basis: +40%, below TP -> held, no second scale-out
        ex.manage_positions(100.0, panic=False)
        self.assertEqual(len(ex.state["positions"]), 1)
        self.assertEqual(len(self._trades()), 1)

        # Remainder reaches $4.40 (>= 2.5 * 1.75) -> take_profit on the reduced basis
        ex.jup.quote = lambda mint, out, amount, **kw: {"outAmount": str(amount * 88_000)}
        ex.manage_positions(100.0, panic=False)
        self.assertEqual(ex.state["positions"], [])
        rows = self._trades()
        self.assertEqual(rows[1]["exit_reason"], "take_profit")
        self.assertAlmostEqual(float(rows[1]["position_usd"]), 2.5)
        self.assertAlmostEqual(float(rows[1]["exit_usd"]), 4.4)
        self.assertEqual(rows[1]["peak_gain_pct"], "76.0")

    def test_scale_out_off_by_default(self):
        ex = self._executor({})
        self._seed(ex)
        ex.jup.quote = lambda mint, out, amount, **kw: {"outAmount": str(amount * 70_000)}  # +40%
        ex.manage_positions(100.0, panic=False)
        self.assertNotIn("scaled_out", ex.state["positions"][0])
        self.assertFalse((Path(self.tmp) / "live_trades.csv").exists())

    def test_live_unconfirmed_close_is_recorded(self):
        from solders.keypair import Keypair
        ex = self._executor({"EXECUTOR_MODE": "live", "WALLET_PRIVATE_KEY": str(Keypair())})
        self._seed(ex)
        ex.state["positions"][0]["last_value_usd"] = 0.62
        ex.rpc.token_balance = lambda owner, mint: 0  # sell already landed on-chain
        ex.close_position(ex.state["positions"][0], "stop_loss", 100.0)
        self.assertEqual(ex.state["positions"], [])
        rows = self._trades()
        self.assertEqual(rows[0]["exit_reason"], "stop_loss_unconfirmed")
        self.assertAlmostEqual(float(rows[0]["exit_usd"]), 0.62)
        self.assertAlmostEqual(ex.state["daily"]["realized_pnl_usd"], 0.62 - 5.0)


if __name__ == "__main__":
    unittest.main()
