"""Pending graduations survive a process restart via executor_state.json."""
import json
import os
import tempfile
import time
import unittest
from unittest import mock


def fresh(**env):
    tmp = tempfile.mkdtemp(prefix="grad-pending-test-")
    base = {"DATA_DIR": tmp, "HELIUS_API_KEY": "k", "MIGRATION_ADDRESS": "a", "EXECUTOR_MODE": "paper"}
    base.update(env)
    patcher = mock.patch.dict(os.environ, base)
    patcher.start()
    import importlib
    import executor
    importlib.reload(executor)
    return executor, patcher, tmp


class PendingPersistTests(unittest.TestCase):
    def test_pending_restored_from_state(self):
        executor, p, tmp = fresh()
        self.addCleanup(p.stop)
        future = time.time() + 10
        state_path = executor.STATE_FILE
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps({
            "mode": "paper",
            "paper_balance_usd": 100,
            "positions": [],
            "daily": {"date": "2026-09-04", "realized_pnl_usd": -4.5},
            "seen_signatures": ["sig1"],
            "pending": [{"mint": "Mint111", "graduated_ts": int(future - 30), "enter_at": future, "signature": "sig1"}],
            "draining": False,
        }))
        ex = executor.Executor(executor.Config())
        self.assertEqual(len(ex.pending), 1)
        self.assertEqual(ex.pending[0]["mint"], "Mint111")
        self.assertEqual(ex.state["daily"]["realized_pnl_usd"], -4.5)

    def test_stale_pending_dropped(self):
        executor, p, tmp = fresh()
        self.addCleanup(p.stop)
        old = time.time() - 200
        executor.STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        executor.STATE_FILE.write_text(json.dumps({
            "mode": "paper",
            "paper_balance_usd": 100,
            "positions": [],
            "daily": {"date": "2026-09-04", "realized_pnl_usd": 0},
            "seen_signatures": [],
            "pending": [{"mint": "StaleMint", "graduated_ts": int(old - 30), "enter_at": old, "signature": "sig"}],
            "draining": False,
        }))
        ex = executor.Executor(executor.Config())
        self.assertEqual(ex.pending, [])
        skips = (executor.SKIPS_FILE).read_text()
        self.assertIn("StaleMint", skips)
        self.assertIn("stale after restart", skips)


if __name__ == "__main__":
    unittest.main()
