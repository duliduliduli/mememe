import random
import unittest

from position_sizing import simulate_path, trade_pnl


class SizingTests(unittest.TestCase):
    def test_fixed_fees_hit_small_positions_harder(self):
        # Same +10% net return: $10 position loses most of it to fixed fees, $100 keeps it.
        self.assertAlmostEqual(trade_pnl(10.0, 0.10, 0.25), 0.5)
        self.assertAlmostEqual(trade_pnl(100.0, 0.10, 0.25), 9.5)

    def test_flat_returns_still_bleed_fixed_fees(self):
        rng = random.Random(1)
        path = simulate_path(
            [0.0], trades=10, balance=100.0, fraction=0.25,
            fixed_fee_per_side=0.10, min_position=5.0, ruin_threshold=1.0, rng=rng,
        )
        self.assertAlmostEqual(path["final"], 98.0)
        self.assertEqual(path["ruined"], 0.0)

    def test_ruin_detected(self):
        rng = random.Random(1)
        path = simulate_path(
            [-0.9], trades=10, balance=100.0, fraction=1.0,
            fixed_fee_per_side=0.10, min_position=5.0, ruin_threshold=5.0, rng=rng,
        )
        self.assertEqual(path["ruined"], 1.0)
        self.assertEqual(path["final"], 0.0)

    def test_min_position_floor_binds(self):
        rng = random.Random(1)
        path = simulate_path(
            [0.0], trades=4, balance=100.0, fraction=0.01,
            fixed_fee_per_side=0.0, min_position=10.0, ruin_threshold=1.0, rng=rng,
        )
        self.assertEqual(path["floor_hits"], 4)


if __name__ == "__main__":
    unittest.main()


class CliSmokeTests(unittest.TestCase):
    def test_cli_runs_end_to_end(self):
        """Regression: main() used os.getenv without importing os and crashed on launch."""
        import csv
        import json
        import os
        import subprocess
        import sys
        import tempfile

        tmp = tempfile.mkdtemp(prefix="grad-sizing-cli-")
        rows = [0.5, -0.3, 0.2, -0.3, 0.75, -0.3, 0.1, -0.2, 0.6, -0.3] * 3
        with open(os.path.join(tmp, "trade_results.csv"), "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["mint_address", "net_return"])
            for i, r in enumerate(rows):
                w.writerow([f"m{i}", r])
        out = os.path.join(tmp, "sizing.json")
        proc = subprocess.run(
            [sys.executable, "position_sizing.py", "--input", os.path.join(tmp, "trade_results.csv"),
             "--output", out, "--paths", "50"],
            capture_output=True, text=True, env={**os.environ, "DATA_DIR": tmp},
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("grid", json.load(open(out)))
