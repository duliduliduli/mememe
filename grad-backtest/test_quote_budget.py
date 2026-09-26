"""Two drains on the shared Jupiter/RPC budget that exits depend on: a moon bag whose sell can
never land being retried every check forever, and a copy inbox full of source events too old to
ever be mirrored or applied. Both were observed live on 2026-09-26."""
import os
import tempfile
import unittest
from unittest import mock


def fresh(**env):
    tmp = tempfile.mkdtemp(prefix="grad-quote-budget-test-")
    base = {"DATA_DIR": tmp, "HELIUS_API_KEY": "k", "MIGRATION_ADDRESS": "a", "EXECUTOR_MODE": "paper"}
    base.update(env)
    patcher = mock.patch.dict(os.environ, base)
    patcher.start()
    import importlib
    import executor
    importlib.reload(executor)
    return executor, patcher


SOL = 100.0


def sol_quote(usd):
    return lambda i, o, amount, **kw: {"outAmount": str(int(usd / SOL * 1e9))}


def trailing_bag():
    """A bag kept at $1, peaked at $4, now worth $2: the 3x arm is met and $2 is under the 40%
    trail from $4, so a sell is due on every check."""
    return {"mint": "m", "tokens": 100, "kept_usd": 1.0, "cost_usd": 1.0,
            "peak_usd": 4.0, "trail_peak_usd": 4.0}


class MoonBagSellBackoffTests(unittest.TestCase):
    def make(self, **env):
        executor, p = fresh(**env)
        self.addCleanup(p.stop)
        ex = executor.Executor(executor.Config())
        bag = trailing_bag()
        ex.state["moon_bags"] = [bag]
        ex.jup.quote = sol_quote(2.0)
        self.attempts = []

        def fail(*args, **kwargs):
            self.attempts.append(args[2] if len(args) > 2 else None)
            raise RuntimeError("Transaction simulation failed: Custom 6025")

        ex.sell_bag = fail
        return executor, ex, bag

    def test_defaults(self):
        executor, p = fresh()
        self.addCleanup(p.stop)
        cfg = executor.Config()
        self.assertEqual(cfg.moon_bag_sell_retry_seconds, 900)
        self.assertEqual(cfg.moon_bag_sell_retry_max_seconds, 86400)

    def test_a_bag_that_cannot_be_sold_is_not_retried_every_check(self):
        executor, ex, bag = self.make()
        ex.manage_moon_bags(SOL)
        self.assertEqual(len(self.attempts), 1)
        self.assertEqual(bag["sell_failures"], 1)
        self.assertGreater(bag["sell_retry_ts"], executor.now_ts())
        # The next three checks all fall inside the 15-minute backoff: no further attempts, which
        # is the whole point. The live bot logged this same failure every 180s for hours.
        for _ in range(3):
            ex.state["moon_bags_checked_ts"] = 0
            ex.manage_moon_bags(SOL)
        self.assertEqual(len(self.attempts), 1)

    def test_the_bag_is_still_valued_while_backing_off(self):
        executor, ex, bag = self.make()
        ex.manage_moon_bags(SOL)
        ex.state["moon_bags_checked_ts"] = 0
        ex.jup.quote = sol_quote(2.5)
        ex.manage_moon_bags(SOL)
        self.assertEqual(len(self.attempts), 1, "still backing off")
        self.assertEqual(bag["last_value_usd"], 2.5, "valuation must keep updating")
        self.assertEqual(bag["peak_usd"], 4.0)

    def test_it_retries_after_the_backoff_and_the_delay_doubles(self):
        executor, ex, bag = self.make()
        ex.manage_moon_bags(SOL)
        first_delay = bag["sell_retry_ts"] - executor.now_ts()
        bag["sell_retry_ts"] = executor.now_ts() - 1
        ex.state["moon_bags_checked_ts"] = 0
        ex.manage_moon_bags(SOL)
        self.assertEqual(len(self.attempts), 2)
        self.assertEqual(bag["sell_failures"], 2)
        second_delay = bag["sell_retry_ts"] - executor.now_ts()
        self.assertAlmostEqual(second_delay, first_delay * 2, delta=2)

    def test_the_backoff_is_capped(self):
        executor, ex, bag = self.make()
        bag["sell_failures"] = 40
        ex.moon_bag_sell_failed(bag, RuntimeError("x"))
        self.assertEqual(bag["sell_failures"], 41)
        self.assertLessEqual(bag["sell_retry_ts"] - executor.now_ts(),
                             ex.cfg.moon_bag_sell_retry_max_seconds + 1)

    def test_the_cap_never_falls_below_the_base(self):
        executor, p = fresh(MOON_BAG_SELL_RETRY_SECONDS="3600", MOON_BAG_SELL_RETRY_MAX_SECONDS="60")
        self.addCleanup(p.stop)
        cfg = executor.Config()
        self.assertEqual(cfg.moon_bag_sell_retry_max_seconds, 3600)

    def test_zero_disables_the_backoff_and_restores_the_old_behaviour(self):
        executor, ex, bag = self.make(MOON_BAG_SELL_RETRY_SECONDS="0")
        for _ in range(3):
            ex.state["moon_bags_checked_ts"] = 0
            ex.manage_moon_bags(SOL)
        self.assertEqual(len(self.attempts), 3, "with the backoff off every check attempts the sell")

    def test_a_healthy_bag_is_never_delayed(self):
        executor, p = fresh()
        self.addCleanup(p.stop)
        ex = executor.Executor(executor.Config())
        self.assertTrue(ex.moon_bag_sell_due({}))
        self.assertTrue(ex.moon_bag_sell_due({"sell_retry_ts": executor.now_ts() - 1}))
        self.assertFalse(ex.moon_bag_sell_due({"sell_retry_ts": executor.now_ts() + 900}))


class EventCutoffTests(unittest.TestCase):
    def test_undated_and_recent_events_are_never_stale(self):
        executor, p = fresh()
        self.addCleanup(p.stop)
        cutoff = 1_000_000.0
        self.assertFalse(executor.event_before_cutoff(None, cutoff))
        self.assertFalse(executor.event_before_cutoff(0, cutoff))
        self.assertFalse(executor.event_before_cutoff(1_000_001, cutoff))

    def test_old_events_are_stale_and_no_cutoff_never_prunes(self):
        executor, p = fresh()
        self.addCleanup(p.stop)
        self.assertTrue(executor.event_before_cutoff(999_999, 1_000_000.0))
        self.assertFalse(executor.event_before_cutoff(1, None))


class CopyEventHorizonTests(unittest.TestCase):
    def test_the_default_horizon_is_the_longest_a_position_can_live(self):
        executor, p = fresh()
        self.addCleanup(p.stop)
        cfg = executor.Config()
        # 7 days in hold mode beats the 24 h copy time stop, so the horizon is 7 days. An older
        # sale cannot apply to any position we could still hold.
        self.assertEqual(cfg.copy_max_event_age_seconds, 7 * 86400)
        self.assertEqual(cfg.copy_max_event_age_seconds,
                         max(cfg.copy_time_stop_minutes * 60, cfg.copy_hold_max_days * 86400))

    def test_the_horizon_follows_a_longer_hold_window(self):
        executor, p = fresh(COPY_HOLD_MAX_DAYS="30")
        self.addCleanup(p.stop)
        self.assertEqual(executor.Config().copy_max_event_age_seconds, 30 * 86400)

    def test_zero_disables_it(self):
        executor, p = fresh(COPY_MAX_EVENT_AGE_SECONDS="0")
        self.addCleanup(p.stop)
        self.assertEqual(executor.Config().copy_max_event_age_seconds, 0.0)

    def test_explicit_override_wins(self):
        executor, p = fresh(COPY_MAX_EVENT_AGE_SECONDS="3600")
        self.addCleanup(p.stop)
        self.assertEqual(executor.Config().copy_max_event_age_seconds, 3600.0)

    def test_prune_drops_the_observed_fixukbsk_backlog(self):
        """FixukbsK carried ~15,000 queued events up to 43 days old, each ending in "too late to
        mirror", draining at about 24 a minute. None of them could ever be acted on."""
        executor, p = fresh()
        self.addCleanup(p.stop)
        ex = executor.Executor(executor.Config())
        now = executor.now_ts()
        horizon = ex.cfg.copy_max_event_age_seconds
        ex.state["copy_inbox"] = {"w": [
            {"signature": "ancient", "blockTime": int(now - 43 * 86400)},
            {"signature": "last-week", "blockTime": int(now - 8 * 86400)},
            {"signature": "recent", "blockTime": int(now - 30)},
            {"signature": "undated", "blockTime": None},
        ]}
        ex.prune_copy_inbox("w", now - horizon, horizon)
        self.assertEqual([e["signature"] for e in ex.state["copy_inbox"]["w"]],
                         ["recent", "undated"], "undated events are kept: they cannot be proved stale")

    def test_prune_is_a_noop_without_a_cutoff_and_when_nothing_is_stale(self):
        executor, p = fresh()
        self.addCleanup(p.stop)
        ex = executor.Executor(executor.Config())
        now = executor.now_ts()
        inbox = [{"signature": "recent", "blockTime": int(now - 30)}]
        ex.state["copy_inbox"] = {"w": list(inbox)}
        ex.prune_copy_inbox("w", None, 0.0)
        self.assertEqual(ex.state["copy_inbox"]["w"], inbox)
        ex.prune_copy_inbox("w", now - 604800, 604800.0)
        self.assertEqual(ex.state["copy_inbox"]["w"], inbox)

    def test_prune_tolerates_a_wallet_with_no_inbox(self):
        executor, p = fresh()
        self.addCleanup(p.stop)
        ex = executor.Executor(executor.Config())
        ex.prune_copy_inbox("nobody", executor.now_ts(), 604800.0)
        self.assertNotIn("nobody", ex.state.get("copy_inbox") or {})


if __name__ == "__main__":
    unittest.main()
