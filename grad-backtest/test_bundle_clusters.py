"""Split-wallet / funding-cluster guards."""
import os
import tempfile
import unittest
from unittest import mock


def fresh(**env):
    tmp = tempfile.mkdtemp(prefix="grad-cluster-test-")
    base = {"DATA_DIR": tmp, "HELIUS_API_KEY": "k", "MIGRATION_ADDRESS": "a", "EXECUTOR_MODE": "paper"}
    base.update(env)
    patcher = mock.patch.dict(os.environ, base)
    patcher.start()
    import importlib
    import executor
    importlib.reload(executor)
    return executor, patcher


class ClusterMathTests(unittest.TestCase):
    def test_five_wallets_same_funder_cluster(self):
        executor, p = fresh()
        self.addCleanup(p.stop)
        funders = {f"w{i}": "origin" for i in range(5)}
        clusters = executor.cluster_wallets(funders, set())
        self.assertEqual(len(clusters), 1)
        amounts = {f"w{i}": 0.11 for i in range(5)}
        pct, members = executor.cluster_supply_pct(clusters, amounts, 1.0)
        self.assertAlmostEqual(pct, 55.0, places=1)
        self.assertEqual(len(members), 5)

    def test_cex_funder_is_not_a_cluster(self):
        executor, p = fresh()
        self.addCleanup(p.stop)
        self.assertEqual(executor.cluster_wallets({f"w{i}": "CEX" for i in range(5)}, {"CEX"}), [])

    def test_same_slot_bundle_pct(self):
        executor, p = fresh()
        self.addCleanup(p.stop)
        buys = [{"wallet": f"w{i}", "slot": 10, "amount": 0.08} for i in range(6)]
        pct, slot, n = executor.bundle_slot_pct(buys, 1.0)
        self.assertEqual((slot, n), (10, 6))
        self.assertAlmostEqual(pct, 48.0, places=1)


class GuardReasonTests(unittest.TestCase):
    def test_log_only_never_skips_cluster(self):
        executor, p = fresh(BUNDLE_LOG_ONLY="1")
        self.addCleanup(p.stop)
        cfg = executor.Config()
        self.assertIsNone(executor.entry_guard_reason(cfg, 1000, 1030, 1.0, 64_000, 600, 11.0, cluster_pct=55.0, cluster_wallets=["Abcd1234"]))

    def test_cluster_skips_when_log_only_off(self):
        executor, p = fresh(BUNDLE_LOG_ONLY="0")
        self.addCleanup(p.stop)
        cfg = executor.Config()
        reason = executor.entry_guard_reason(cfg, 1000, 1030, 1.0, 64_000, 600, 11.0, cluster_pct=55.0, cluster_wallets=["Abcd1234xyz"])
        self.assertIn("funding cluster", reason)


if __name__ == "__main__":
    unittest.main()
