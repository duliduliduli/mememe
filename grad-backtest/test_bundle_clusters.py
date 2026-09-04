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

    def test_creator_linked_cluster_and_top_ten_pct(self):
        funders = {"dev": "origin", "w1": "dev", "w2": "dev", "other": "elsewhere"}
        amounts = {"dev": 0.05, "w1": 0.07, "w2": 0.08, "other": 0.02}
        members = ba.related_holder_wallets(funders, "dev", set())
        self.assertEqual(set(members), {"dev", "w1", "w2"})
        self.assertAlmostEqual(ba.wallets_supply_pct(members, amounts, 1.0), 20.0)
        self.assertAlmostEqual(ba.top_wallets_supply_pct(amounts, 1.0, 3), 20.0)

    def test_two_hop_ancestry_finds_split_funders(self):
        ancestry = {f"w{i}": [f"relay{i}", "treasury"] for i in range(5)}
        clusters = ba.ancestry_clusters(ancestry, set())
        pct, members = ba.cluster_supply_pct(clusters, {f"w{i}": 0.09 for i in range(5)}, 1.0)
        self.assertAlmostEqual(pct, 45.0)
        self.assertEqual(set(members), set(ancestry))

    def test_distributor_transfer_cluster_counts_current_supply(self):
        holders = {f"w{i}" for i in range(6)} | {"unrelated"}
        edges = [("distributor", f"w{i}") for i in range(6)]
        clusters = ba.transfer_clusters(edges, holders)
        amounts = {f"w{i}": 0.07 for i in range(6)} | {"unrelated": 0.2}
        pct, members = ba.cluster_supply_pct(clusters, amounts, 1.0)
        self.assertAlmostEqual(pct, 42.0)
        self.assertEqual(set(members), {f"w{i}" for i in range(6)})

    def test_coordinated_rolling_window_spans_nearby_slots(self):
        buys = [
            {"wallet": "w1", "slot": 10, "amount": 0.08},
            {"wallet": "w2", "slot": 14, "amount": 0.09},
            {"wallet": "w3", "slot": 20, "amount": 0.07},
            {"wallet": "late", "slot": 40, "amount": 0.50},
        ]
        pct, slot, count = ba.coordinated_buy_pct(buys, 1.0, window_slots=12, min_wallets=3)
        self.assertAlmostEqual(pct, 24.0)
        self.assertEqual((slot, count), (10, 3))


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
