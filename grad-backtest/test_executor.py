import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock


class ExecutorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="grad-exec-test-")
        patcher = mock.patch.dict(os.environ, {"DATA_DIR": self.tmp})
        patcher.start()
        self.addCleanup(patcher.stop)
        import importlib
        import executor
        importlib.reload(executor)
        self.executor = executor
        self.cfg = executor.Config()

    def test_paper_is_default_mode(self):
        self.assertEqual(self.cfg.mode, "paper")

    def test_default_funder_coverage_is_30_percent(self):
        self.assertEqual(self.cfg.min_funder_coverage_pct, 30.0)

    def test_live_mode_requires_key(self):
        with mock.patch.dict(os.environ, {"EXECUTOR_MODE": "live", "HELIUS_API_KEY": "k", "MIGRATION_ADDRESS": "a"}):
            cfg = self.executor.Config()
            with self.assertRaises(SystemExit):
                cfg.validate()

    def test_sizing_guards(self):
        f = self.executor.position_size_usd
        cfg = self.cfg  # fraction 0.10, max $20, min $5, max 5 concurrent, daily limit $30
        self.assertEqual(f(cfg, 100.0, 0, 0.0), 10.0)
        self.assertEqual(f(cfg, 1000.0, 0, 0.0), 20.0)      # capped by MAX_POSITION_USD
        self.assertEqual(f(cfg, 30.0, 0, 0.0), 5.0)          # 10% is $3: a small account trades the $5 minimum
        self.assertEqual(f(cfg, 6.0, 0, 0.0), 0.0)           # $5 does not fit in the deployable share of $6
        self.assertEqual(f(cfg, 100.0, 4, 0.0), 10.0)        # one slot remains
        self.assertEqual(f(cfg, 100.0, 5, 0.0), 0.0)         # concurrency cap
        self.assertEqual(f(cfg, 100.0, 0, -31.0), 0.0)       # daily loss limit hit

    def test_decide_exit(self):
        d = self.executor.decide_exit
        cfg = self.cfg  # tp +75%, sl -30%, time stop 30m
        self.assertEqual(d(10.0, 17.6, 0, 60, cfg), "take_profit")
        self.assertEqual(d(10.0, 6.9, 0, 60, cfg), "stop_loss")
        self.assertEqual(d(10.0, 10.5, 0, 30 * 60 + 1, cfg), "time_stop")
        self.assertIsNone(d(10.0, 10.5, 0, 60, cfg))

    def test_trailing_stop_arms_only_after_the_position_was_up_enough(self):
        d = self.executor.decide_exit
        cfg = self.cfg
        cfg.trailing_stop = 0.25
        cfg.trailing_arm_gain = 0.30
        # CALI: peaked +12.6%, then fell 27% off that peak (-18% net). Not armed: no exit.
        self.assertIsNone(d(18.21, 14.96, 0, 60, cfg, peak_usd=20.51))
        # Same fall past the stop loss is still caught by the stop loss.
        self.assertEqual(d(18.21, 12.5, 0, 60, cfg, peak_usd=20.51), "stop_loss")
        # Up 35% at the peak, then 27% off it: armed, trailing stop fires.
        self.assertEqual(d(10.0, 9.85, 0, 60, cfg, peak_usd=13.5), "trailing_stop")
        cfg.trailing_arm_gain = 0.0                                  # 0 arms from entry (the old behaviour)
        self.assertEqual(d(18.21, 14.96, 0, 60, cfg, peak_usd=20.51), "trailing_stop")

    def test_state_roundtrip_and_daily_roll(self):
        state = self.executor.load_state(self.cfg)
        self.assertEqual(state["paper_balance_usd"], 100.0)
        state["daily"] = {"date": "2000-01-01", "realized_pnl_usd": -12.0}
        self.executor.save_state(state)
        loaded = self.executor.load_state(self.cfg)
        self.assertEqual(loaded["daily"]["realized_pnl_usd"], -12.0)
        self.executor.roll_daily(loaded)
        self.assertEqual(loaded["daily"]["realized_pnl_usd"], 0.0)

    def test_record_trade_writes_csv(self):
        self.executor.record_trade(
            {"opened_at": "a", "closed_at": "b", "mint": "m", "mode": "paper",
             "position_usd": 10, "exit_usd": 12, "net_return": 0.2, "exit_reason": "take_profit"}
        )
        text = (Path(self.tmp) / "live_trades.csv").read_text()
        self.assertIn("take_profit", text)
        self.assertIn("net_return", text.splitlines()[0])


if __name__ == "__main__":
    unittest.main()
