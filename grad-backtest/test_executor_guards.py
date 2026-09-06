import csv
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="grad-guard-test-")
        patcher = mock.patch.dict(os.environ, {"DATA_DIR": self.tmp})
        patcher.start()
        self.addCleanup(patcher.stop)
        import importlib
        import executor
        importlib.reload(executor)
        self.executor = executor
        self.cfg = executor.Config()  # entry delay 30s, lateness 60s, impact 5%

    def test_stale_entry_rejected(self):
        g = self.executor.entry_guard_reason
        self.assertIsNone(g(self.cfg, 1000.0, 1000.0 + 30, 1.0))
        self.assertIsNone(g(self.cfg, 1000.0, 1000.0 + 89, 1.0))
        self.assertIn("stale", g(self.cfg, 1000.0, 1000.0 + 91, 1.0))

    def test_price_impact_rejected(self):
        g = self.executor.entry_guard_reason
        self.assertEqual(self.cfg.max_price_impact_pct, 10.0)  # $5 orders: 5% was $0.25 and cost ARGOS
        self.assertIsNone(g(self.cfg, 1000.0, 1030.0, 9.9))
        self.assertIn("price impact", g(self.cfg, 1000.0, 1030.0, 10.1))
        self.assertIsNone(g(self.cfg, 1000.0, 1030.0, None))  # missing impact doesn't block

    def test_quote_price_impact_parsing(self):
        f = self.executor.quote_price_impact_pct
        self.assertAlmostEqual(f({"priceImpactPct": "0.023"}), 2.3)
        self.assertAlmostEqual(f({"priceImpactPct": -0.01}), 1.0)
        self.assertIsNone(f({}))
        self.assertIsNone(f({"priceImpactPct": "garbage"}))

    def test_skip_log_written(self):
        self.executor.record_skip("mintX", "test reason", {"market_cap_usd": 12345.678, "curve_tx_count": 9, "ignored": 1})
        rows = list(csv.DictReader(open(Path(self.tmp) / "skips.csv")))
        self.assertEqual(list(rows[0].keys()), self.executor.SKIP_COLUMNS)
        self.assertEqual((rows[0]["mint"], rows[0]["reason"]), ("mintX", "test reason"))
        self.assertEqual(rows[0]["market_cap_usd"], "12345.68")
        self.assertEqual(rows[0]["curve_tx_count"], "9")
        self.assertNotIn("ignored", rows[0])

    def test_record_trade_rotates_older_header_and_keeps_wider_one(self):
        # An older file (header missing columns we now record) is rotated aside so no metadata
        # is dropped; the new file carries the full header and an aligned row.
        old_header = ["opened_at", "mint", "net_return"]
        trades = Path(self.tmp) / "live_trades.csv"
        trades.write_text(",".join(old_header) + "\n")
        self.executor.record_trade({"opened_at": "t1", "mint": "m", "net_return": 0.1, "entry_price_impact_pct": 2.0})
        rows = list(csv.reader(open(trades)))
        self.assertEqual(rows[0], self.executor.TRADE_COLUMNS)
        self.assertEqual(len(rows[1]), len(self.executor.TRADE_COLUMNS))
        rotated = [p for p in Path(self.tmp).glob("live_trades.*.csv")]
        self.assertEqual(len(rotated), 1)
        self.assertEqual(rotated[0].read_text().strip(), ",".join(old_header))
        # A file with extra/reordered columns keeps its own header: never misalign a row.
        wider = self.executor.TRADE_COLUMNS + ["custom"]
        trades.write_text(",".join(wider) + "\n")
        self.executor.record_trade({"opened_at": "t2", "mint": "m"})
        rows = list(csv.reader(open(trades)))
        self.assertEqual(rows[0], wider)
        self.assertEqual(len(rows[1]), len(wider))


if __name__ == "__main__":
    unittest.main()


class TrailingStopTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="grad-trail-test-")
        patcher = mock.patch.dict(os.environ, {"DATA_DIR": self.tmp, "TRAILING_STOP": "0.20"})
        patcher.start()
        self.addCleanup(patcher.stop)
        import importlib
        import executor
        importlib.reload(executor)
        self.executor = executor
        self.cfg = executor.Config()

    def test_trailing_stop_triggers_off_peak(self):
        d = self.executor.decide_exit
        # entry $10, ran to peak $16, now $12.5 = 21.9% off peak -> trailing exit
        self.assertEqual(d(10.0, 12.5, 0, 60, self.cfg, peak_usd=16.0), "trailing_stop")
        # $13.0 is only 18.75% off peak -> hold
        self.assertIsNone(d(10.0, 13.0, 0, 60, self.cfg, peak_usd=16.0))
        # TP still wins when reached
        self.assertEqual(d(10.0, 17.6, 0, 60, self.cfg, peak_usd=17.6), "take_profit")

    def test_disabled_by_default(self):
        with mock.patch.dict(os.environ, {"TRAILING_STOP": "0"}):
            cfg = self.executor.Config()
            self.assertIsNone(self.executor.decide_exit(10.0, 5.1 + 2, 0, 60, cfg, peak_usd=100.0))

    def test_backtest_trailing_stop(self):
        from grad_backtest import Candle, simulate_trade
        candles = [
            Candle(100, 100, 150, 99, 150, 1),   # peak becomes 150
            Candle(160, 150, 180, 150, 175, 1),  # peak becomes 180
            Candle(220, 175, 176, 130, 135, 1),  # low 130 <= 180*0.8=144 -> trail exit
        ]
        result = simulate_trade("m", 0, "p", "pump", 100, 100, candles, 1.00, 0.30, 30, 0.03, trailing_stop=0.20)
        self.assertEqual(result.exit_reason, "trailing_stop")
        self.assertAlmostEqual(result.exit_price, 144.0)

    def test_backtest_unchanged_when_disabled(self):
        from grad_backtest import Candle, simulate_trade
        candles = [Candle(100, 100, 180, 99, 170, 1)]
        result = simulate_trade("m", 0, "p", "pump", 100, 100, candles, 0.75, 0.30, 30, 0.03)
        self.assertEqual(result.exit_reason, "take_profit")


class MoonBagTests(unittest.TestCase):
    def test_backtest_moon_bag_blends_late_price(self):
        from grad_backtest import Candle, apply_costs, simulate_trade
        candles = [
            Candle(100, 100, 180, 99, 170, 1),      # TP +75% hits at 175
            Candle(160, 170, 320, 160, 300, 1),     # later path
            Candle(86000, 300, 310, 290, 300, 1),   # ~24h close at 300
        ]
        result = simulate_trade("m", 0, "p", "pump", 100, 100, candles, 0.75, 0.30, 30, 0.03, moon_bag=0.15)
        self.assertEqual(result.exit_reason, "take_profit")
        self.assertEqual(result.moon_bag_price, 300)
        expected = apply_costs(100, 175 * 0.85 + 300 * 0.15, 0.03)[1]
        self.assertAlmostEqual(result.net_return, expected)

    def test_backtest_moon_bag_disabled_by_default(self):
        from grad_backtest import Candle, simulate_trade
        candles = [Candle(100, 100, 180, 99, 170, 1)]
        result = simulate_trade("m", 0, "p", "pump", 100, 100, candles, 0.75, 0.30, 30, 0.03)
        self.assertEqual(result.moon_bag_fraction, 0.0)
        self.assertEqual(result.exit_price, 175)

    def test_executor_moon_bag_config_clamped(self):
        with mock.patch.dict(os.environ, {"MOON_BAG": "0.9"}):
            import importlib
            import executor
            importlib.reload(executor)
            self.assertEqual(executor.Config().moon_bag, 0.5)
        with mock.patch.dict(os.environ, {"MOON_BAG": "-1"}):
            import importlib
            import executor
            importlib.reload(executor)
            self.assertEqual(executor.Config().moon_bag, 0.0)
