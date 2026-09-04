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
        self.assertIsNone(g(self.cfg, 1000.0, 1030.0, 4.9))
        self.assertIn("price impact", g(self.cfg, 1000.0, 1030.0, 5.1))
        self.assertIsNone(g(self.cfg, 1000.0, 1030.0, None))  # missing impact doesn't block

    def test_quote_price_impact_parsing(self):
        f = self.executor.quote_price_impact_pct
        self.assertAlmostEqual(f({"priceImpactPct": "0.023"}), 2.3)
        self.assertAlmostEqual(f({"priceImpactPct": -0.01}), 1.0)
        self.assertIsNone(f({}))
        self.assertIsNone(f({"priceImpactPct": "garbage"}))

    def test_skip_log_written(self):
        self.executor.record_skip("mintX", "test reason")
        rows = list(csv.reader(open(Path(self.tmp) / "skips.csv")))
        self.assertEqual(rows[0], ["timestamp", "mint", "reason"])
        self.assertEqual(rows[1][1:], ["mintX", "test reason"])

    def test_record_trade_respects_existing_header(self):
        old_header = ["opened_at", "mint", "net_return"]
        trades = Path(self.tmp) / "live_trades.csv"
        trades.write_text(",".join(old_header) + "\n")
        self.executor.record_trade({"opened_at": "t1", "mint": "m", "net_return": 0.1, "entry_price_impact_pct": 2.0})
        rows = list(csv.reader(open(trades)))
        self.assertEqual(len(rows[1]), 3)  # no misaligned extra columns


if __name__ == "__main__":
    unittest.main()
