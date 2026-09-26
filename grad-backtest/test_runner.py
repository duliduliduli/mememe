"""Runner mode: graduations sit on a watchlist and are bought once their market cap grows
into the swing band with momentum; runner positions exit on their own thresholds."""
import importlib
import os
import tempfile
import unittest
from unittest import mock

SOL = 100.0


def fresh(**env):
    tmp = tempfile.mkdtemp(prefix="grad-runner-test-")
    base = {"DATA_DIR": tmp, "HELIUS_API_KEY": "k", "MIGRATION_ADDRESS": "a", "EXECUTOR_MODE": "paper"}
    base.update(env)
    patcher = mock.patch.dict(os.environ, base)
    patcher.start()
    import executor
    importlib.reload(executor)
    return executor, patcher


class GuardTests(unittest.TestCase):
    def test_runner_skips_lateness_and_uses_its_own_band(self):
        executor, p = fresh()
        self.addCleanup(p.stop)
        cfg = executor.Config()
        late = 5 * 3600
        self.assertIn("stale entry", executor.entry_guard_reason(cfg, 0, late, 1.0, 800_000))
        self.assertIsNone(executor.entry_guard_reason(cfg, 0, late, 1.0, 800_000, runner=True))
        self.assertIn("runner band", executor.entry_guard_reason(cfg, 0, late, 1.0, 200_000, runner=True))
        self.assertIn("runner band", executor.entry_guard_reason(cfg, 0, late, 1.0, 9_000_000, runner=True))
        self.assertIn("price impact", executor.entry_guard_reason(cfg, 0, late, 50.0, 800_000, runner=True))

    def test_defaults(self):
        executor, p = fresh()
        self.addCleanup(p.stop)
        cfg = executor.Config()
        self.assertTrue(cfg.runner_enabled)
        self.assertFalse(cfg.runner_only)
        self.assertEqual(cfg.runner_min_market_cap_usd, 400_000)
        self.assertEqual(cfg.runner_max_market_cap_usd, 4_000_000)
        self.assertEqual(cfg.runner_time_stop_minutes, 240)


class WatchlistTests(unittest.TestCase):
    def make(self, **env):
        executor, p = fresh(**env)
        self.addCleanup(p.stop)
        ex = executor.Executor(executor.Config())
        ex.rpc.token_supply_details = lambda mint: (1_000_000_000.0, 6, 1_000_000_000 * 10 ** 6)
        # The dead-tape gate reads the chain; these tests price the watchlist from a stubbed feed
        # and must stay offline, so the tape is unreadable and the gate has to fail open. The
        # gate's own behaviour is covered in test_tape_gate.py.
        ex.rpc.tape_activity = lambda mint, window_seconds, limit=50: None
        return executor, ex

    def test_watch_is_bounded_and_deduplicated(self):
        executor, ex = self.make(RUNNER_MAX_WATCH="3")
        now = executor.now_ts()
        for i in range(5):
            ex.watch_runner(f"mint{i}", int(now) - i)
        ex.watch_runner("mint4", int(now))
        self.assertEqual([w["mint"] for w in ex.state["watchlist"]], ["mint4", "mint3", "mint2"])
        ex.watch_runner("old", int(now) - 7 * 3600)  # past the watch window
        self.assertEqual(len(ex.state["watchlist"]), 3)

    def test_enters_when_cap_climbs_into_band_with_momentum(self):
        executor, ex = self.make(RUNNER_CHECK_SECONDS="15")
        now = int(executor.now_ts())
        ex.watch_runner("RUN", now - 600)
        ex.watch_runner("FLAT", now - 600)
        entered = []
        ex.enter_with_retry = lambda item, sol_price: entered.append(item)
        # price per token in USD with a 1e9 supply: 0.0003 -> $300k, 0.0006 -> $600k
        feeds = iter([{"RUN": 0.0003, "FLAT": 0.0006}, {"RUN": 0.0006, "FLAT": 0.00061}])
        ex.runner_prices = lambda mints: next(feeds)
        ex.manage_watchlist(SOL)                       # first sample: RUN below band, FLAT in band but no history
        self.assertEqual(entered, [])
        ex.state["watchlist_checked_ts"] = 0           # make the next check due
        ex.manage_watchlist(SOL)                       # RUN doubled into the band; FLAT moved 2%
        self.assertEqual([e["mint"] for e in entered], ["RUN"])
        self.assertTrue(entered[0]["runner"])
        self.assertEqual(entered[0]["runner_market_cap_usd"], 600_000)
        self.assertEqual([w["mint"] for w in ex.state["watchlist"]], ["FLAT"])

    def test_dead_and_expired_tokens_leave_the_watchlist(self):
        executor, ex = self.make()
        now = int(executor.now_ts())
        ex.watch_runner("DEAD", now - 60)
        ex.watch_runner("OLD", now - 5 * 3600)
        ex.runner_prices = lambda mints: {}
        for _ in range(5):
            ex.state["watchlist_checked_ts"] = 0
            ex.manage_watchlist(SOL)
        self.assertEqual([w["mint"] for w in ex.state["watchlist"]], [])

    def test_runner_positions_exit_on_runner_thresholds(self):
        executor, ex = self.make(RUNNER_TAKE_PROFIT="1.0", TAKE_PROFIT="0.5")
        plain = {"mint": "A", "position_usd": 10.0, "opened_ts": 0}
        runner = {"mint": "B", "position_usd": 10.0, "opened_ts": 0, "runner": True}
        self.assertEqual(ex.exit_cfg(plain).take_profit, 0.5)
        self.assertEqual(ex.exit_cfg(runner).take_profit, 1.0)
        self.assertEqual(ex.exit_cfg(runner).time_stop_minutes, 240)


if __name__ == "__main__":
    unittest.main()
