"""Split-wallet / funding-cluster math. Tests bundle_analysis.py so CI stays green
before executor.py gains the live wiring."""
import unittest

import bundle_analysis as ba


class ClusterMathTests(unittest.TestCase):
    def test_five_wallets_same_funder_cluster(self):
        funders = {f"w{i}": "origin" for i in range(5)}
        clusters = ba.cluster_wallets(funders, set())
        self.assertEqual(len(clusters), 1)
        amounts = {f"w{i}": 0.11 for i in range(5)}
        pct, members = ba.cluster_supply_pct(clusters, amounts, 1.0)
        self.assertAlmostEqual(pct, 55.0, places=1)
        self.assertEqual(len(members), 5)

    def test_cex_funder_is_not_a_cluster(self):
        funders = {f"w{i}": "CEX" for i in range(5)}
        self.assertEqual(ba.cluster_wallets(funders, {"CEX"}), [])

    def test_same_slot_bundle_pct(self):
        buys = [{"wallet": f"w{i}", "slot": 10, "amount": 0.08} for i in range(6)]
        pct, slot, n = ba.bundle_slot_pct(buys, 1.0)
        self.assertEqual((slot, n), (10, 6))
        self.assertAlmostEqual(pct, 48.0, places=1)


class GuardReasonShapeTests(unittest.TestCase):
    def test_runner_style_split_clears_single_holder_and_trips_cluster(self):
        funders = {f"w{i}": "cabal" for i in range(6)}
        clusters = ba.cluster_wallets(funders, set())
        amounts = {f"w{i}": 0.09 for i in range(6)}
        pct, _ = ba.cluster_supply_pct(clusters, amounts, 1.0)
        self.assertLess(max(amounts.values()) * 100, 20)
        self.assertGreater(pct, 30)


if __name__ == "__main__":
    unittest.main()
