import os
import tempfile
import time
import unittest
from unittest import mock


def fresh(**env):
    tmp = tempfile.mkdtemp(prefix="grad-profit-ratchet-test-")
    base = {"DATA_DIR": tmp, "HELIUS_API_KEY": "k", "MIGRATION_ADDRESS": "a", "EXECUTOR_MODE": "paper"}
    base.update(env)
    patcher = mock.patch.dict(os.environ, base)
    patcher.start()
    import importlib
    import executor
    importlib.reload(executor)
    return executor, executor.Executor(executor.Config()), patcher


class ParseProfitRatchetTests(unittest.TestCase):
    def test_parses_sorts_and_reads_percent_suffixes(self):
        import executor as ex_mod
        steps = ex_mod.parse_profit_ratchet("0.6:0.3,0.35:0.10,120%:70%")
        self.assertEqual([s["peak"] for s in steps], [0.35, 0.6, 1.2])
        self.assertEqual([s["floor"] for s in steps], [0.10, 0.3, 0.7])

    def test_drops_steps_that_would_fire_immediately_or_make_no_sense(self):
        import executor as ex_mod
        # A floor at or above its own peak would sell the moment the peak is set; a negative
        # floor is not a profit floor; a non-positive peak never arms.
        self.assertEqual(ex_mod.parse_profit_ratchet("0.5:0.5,0.5:0.9,-0.2:0.1,0:0.1"), [])

    def test_drops_malformed_entries_without_raising(self):
        import executor as ex_mod
        self.assertEqual(ex_mod.parse_profit_ratchet("0.35"), [])
        self.assertEqual(ex_mod.parse_profit_ratchet(""), [])
        self.assertEqual(ex_mod.parse_profit_ratchet("abc:def"), [])
        self.assertEqual(ex_mod.parse_profit_ratchet(None), [])

    def test_empty_disables_and_ratchet_text_reports_off(self):
        import executor as ex_mod
        self.assertEqual(ex_mod.parse_profit_ratchet(""), [])
        self.assertEqual(ex_mod.ratchet_text([]), "off")
        self.assertEqual(ex_mod.ratchet_text([{"peak": 0.35, "floor": 0.1}]), "+35%->+10%")


class RatchetFloorGainTests(unittest.TestCase):
    def setUp(self):
        import executor as ex_mod
        self.ex_mod = ex_mod
        self.steps = ex_mod.parse_profit_ratchet("0.35:0.10,0.60:0.30,1.20:0.70")

    def test_unarmed_below_the_first_step(self):
        self.assertIsNone(self.ex_mod.ratchet_floor_gain(self.steps, 0.34))

    def test_takes_the_highest_step_the_peak_has_reached(self):
        self.assertEqual(self.ex_mod.ratchet_floor_gain(self.steps, 0.35), 0.10)
        self.assertEqual(self.ex_mod.ratchet_floor_gain(self.steps, 0.59), 0.10)
        self.assertEqual(self.ex_mod.ratchet_floor_gain(self.steps, 0.60), 0.30)
        self.assertEqual(self.ex_mod.ratchet_floor_gain(self.steps, 5.00), 0.70)

    def test_floor_is_monotonic_in_the_peak(self):
        gains = [self.ex_mod.ratchet_floor_gain(self.steps, p / 100.0) or 0.0 for p in range(0, 300)]
        self.assertEqual(gains, sorted(gains), "the locked floor must never fall as the peak rises")


class DecideExitRatchetTests(unittest.TestCase):
    def setUp(self):
        # fresh() patches DATA_DIR and the rest of the environment before the module is reloaded,
        # so Config() reads defaults rather than whatever the host happens to export.
        self.ex_mod, _, patcher = fresh()
        self.addCleanup(patcher.stop)

    def cfg(self, **over):
        cfg = self.ex_mod.Config()
        cfg.take_profit = 0.75
        cfg.stop_loss = 0.30
        cfg.trailing_stop = 0.25
        cfg.trailing_arm_gain = 0.30
        cfg.breakeven_arm = 0.20
        cfg.breakeven_floor = 0.0
        cfg.time_stop_minutes = 1440
        cfg.profit_ratchet = self.ex_mod.parse_profit_ratchet("0.35:0.10,0.60:0.30,1.20:0.70")
        for key, value in over.items():
            setattr(cfg, key, value)
        return cfg

    def decide(self, basis, current, cfg, peak):
        now = time.time()
        return self.ex_mod.decide_exit(basis, current, now - 60, now, cfg, peak_usd=peak)

    def test_exits_at_the_locked_floor_once_the_peak_arms_a_step(self):
        # basis 3.50, peak 4.95 (+41.4%) arms the +35%->+10% step, so the floor is 3.85.
        self.assertEqual(self.decide(3.50, 3.80, self.cfg(), 4.95), "profit_floor")

    def test_holds_above_the_floor_and_never_caps_the_upside(self):
        # A position well above every floor is still open: the ratchet protects a gain that already
        # happened, it is not a take profit. take_profit=inf is the uncapped-runner state the ladder
        # leaves once every rung has sold.
        cfg = self.cfg(take_profit=float("inf"))
        self.assertIsNone(self.decide(3.50, 9.00, cfg, 9.50))

    def test_does_not_arm_before_the_first_step_is_reached(self):
        # breakeven_arm off so this isolates the ratchet: peak +20% is under the +35% first step,
        # so only the -30% stop loss protects the position.
        cfg = self.cfg(breakeven_arm=0.0)
        self.assertIsNone(self.decide(5.00, 4.40, cfg, 6.00))
        self.assertEqual(self.decide(5.00, 3.40, cfg, 6.00), "stop_loss")

    def test_ratchet_binds_where_it_locks_more_than_the_trailing_stop(self):
        # basis 5.00, peak 8.00 (+60%) arms +60%->+30%: the ratchet locks 6.50, while the 25% trail
        # only reaches 8.00*0.75 = 6.00. The ratchet is the higher floor, so it fires first.
        cfg = self.cfg()
        self.assertEqual(self.decide(5.00, 6.20, cfg, 8.00), "profit_floor")
        self.assertIsNone(self.decide(5.00, 6.60, cfg, 8.00))

    def test_trailing_stop_still_wins_where_it_locks_more_than_the_ratchet(self):
        # basis 5.00, peak 11.50 (+130%) arms +120%->+70%: the ratchet locks 8.50, but the 25%
        # trail reaches 11.50*0.75 = 8.625. decide_exit checks the trail first, and that is the
        # better exit -- the ratchet is a floor that supplements the trail, never one that
        # replaces a tighter stop with a looser one.
        self.assertEqual(self.decide(5.00, 8.00, self.cfg(), 11.50), "trailing_stop")

    def test_disabled_ratchet_changes_nothing(self):
        self.assertIsNone(self.decide(3.50, 3.80, self.cfg(profit_ratchet=[]), 4.95))

    def test_missing_peak_never_arms(self):
        self.assertIsNone(self.decide(3.50, 3.80, self.cfg(), None))


class ExitCfgRatchetTests(unittest.TestCase):
    def test_copied_position_carries_the_ratchet(self):
        executor, ex, p = fresh()
        self.addCleanup(p.stop)
        cfg = ex.exit_cfg({"copy": "wallet", "mint": "m"})
        self.assertTrue(cfg.profit_ratchet)

    def test_hold_mode_keeps_the_ratchet_while_dropping_the_other_floors(self):
        """Hold mode follows the wallet's sells with no take profit, no trailing stop and no
        breakeven floor. Without the ratchet a pop the wallet rides back down has nothing but the
        wide -COPY_HOLD_STOP_LOSS backstop for COPY_HOLD_MAX_DAYS."""
        executor, ex, p = fresh()
        self.addCleanup(p.stop)
        cfg = ex.exit_cfg({"copy": "wallet", "mint": "m", "hold_with_source": True})
        self.assertEqual(cfg.take_profit, float("inf"))
        self.assertEqual(cfg.trailing_stop, 0.0)
        self.assertEqual(cfg.breakeven_arm, 0.0)
        self.assertTrue(cfg.profit_ratchet, "hold mode must keep the ratchet")
        # And it actually fires: up +130%, fallen back to +30% -> the hold ratchet's +100%->+40%
        # step locks $7.00, so $6.50 exits instead of riding on toward the -40% stop.
        self.assertEqual(
            executor.decide_exit(5.00, 6.50, time.time() - 60, time.time(), cfg, peak_usd=11.50),
            "profit_floor",
        )
        # Above the locked floor the wallet's own sells are still the only thing that closes it.
        self.assertIsNone(
            executor.decide_exit(5.00, 8.00, time.time() - 60, time.time(), cfg, peak_usd=11.50)
        )

    def test_runner_lane_is_unaffected(self):
        executor, ex, p = fresh()
        self.addCleanup(p.stop)
        self.assertEqual(ex.exit_cfg({"runner": True, "mint": "m"}).profit_ratchet, [])

    def test_graduation_lane_is_unaffected(self):
        executor, ex, p = fresh()
        self.addCleanup(p.stop)
        self.assertEqual(ex.exit_cfg({"mint": "m"}).profit_ratchet, [])

    def test_env_var_configures_and_disables_it(self):
        executor, ex, p = fresh(COPY_PROFIT_RATCHET="0.5:0.2")
        self.addCleanup(p.stop)
        self.assertEqual(ex.cfg.copy_profit_ratchet, [{"peak": 0.5, "floor": 0.2}])
        executor, ex2, p2 = fresh(COPY_PROFIT_RATCHET="")
        self.addCleanup(p2.stop)
        self.assertEqual(ex2.cfg.copy_profit_ratchet, [])


class StopTextTests(unittest.TestCase):
    def test_reports_the_ratchet_floor_when_it_is_the_highest_stop(self):
        executor, ex, p = fresh()
        self.addCleanup(p.stop)
        cfg = ex.exit_cfg({"copy": "wallet", "mint": "m"})
        # basis 5.00, peak 11.50 (+130%) arms +120%->+70%: floor 8.50 beats the -30% stop.
        self.assertEqual(executor.stop_text(5.00, 11.50, cfg), "sl_value=$8.50(ratchet +70%)")

    def test_unarmed_position_still_prints_the_plain_stop(self):
        executor, ex, p = fresh()
        self.addCleanup(p.stop)
        cfg = ex.exit_cfg({"copy": "wallet", "mint": "m"})
        self.assertEqual(executor.stop_text(5.00, 5.00, cfg), "sl_value=$3.50")

    def test_breakeven_label_is_preserved_when_it_outranks_the_ratchet(self):
        executor, ex, p = fresh(COPY_PROFIT_RATCHET="")
        self.addCleanup(p.stop)
        cfg = ex.exit_cfg({"copy": "wallet", "mint": "m"})
        self.assertEqual(executor.stop_text(5.00, 6.50, cfg), "sl_value=$5.00(breakeven)")


class LiveTradeReplayTests(unittest.TestCase):
    def test_the_default_ratchet_deliberately_does_not_touch_the_J6rYa7tX_trade(self):
        """J6rYa7tX on 2026-09-26: entered $5.00, peaked $6.91 (+38%), the 1.4x rung sold 30% for
        $2.12 leaving basis $3.50 and peak $4.95, then the 25% trailing stop took the remainder out
        at $3.58. Total about $5.70 on $5.00, i.e. +14% in eight minutes -- a win, not a loss.

        The ratchet's first step is anchored at a +56% peak, above this trade's +41.4%, so the
        default leaves the trade exactly as it happened. That is intentional and it is the honest
        limit of this change: flooring the +12% to +56% band would have locked more here, but the
        same floor would shake out the runners COPY_RUNNER_TRAIL exists to ride, and the repo's own
        evidence is that coins which stalled under +30% did not come back while every coin past +56%
        won. Tightening that band is a tuning decision that needs live_trades.csv, not a guess."""
        executor, ex, p = fresh()
        self.addCleanup(p.stop)
        cfg = ex.exit_cfg({"copy": "wallet", "mint": "m",
                           "ladder": [{"x": 1.4, "pct": 30, "done": True},
                                      {"x": 1.8, "pct": 30, "done": False}]})
        basis, peak = 3.50, 4.95
        self.assertEqual(peak / basis - 1.0, 0.41428571428571437)
        self.assertIsNone(executor.ratchet_floor_gain(cfg.profit_ratchet, peak / basis - 1.0))
        # So the trailing stop still decides this trade, exactly as it did live.
        self.assertEqual(executor.decide_exit(basis, 3.58, time.time() - 60, time.time(),
                                              cfg, peak_usd=peak), "trailing_stop")

    def test_a_tighter_ratchet_would_have_locked_more_on_that_trade(self):
        """The knob exists: configured below the trade's peak, the ratchet outruns the 25% trail
        (which fires at $3.71) and locks $3.85 instead."""
        executor, ex, p = fresh(COPY_PROFIT_RATCHET="0.35:0.10,0.60:0.30,1.20:0.70")
        self.addCleanup(p.stop)
        cfg = ex.exit_cfg({"copy": "wallet", "mint": "m",
                           "ladder": [{"x": 1.4, "pct": 30, "done": True},
                                      {"x": 1.8, "pct": 30, "done": False}]})
        basis, peak = 3.50, 4.95
        self.assertEqual(executor.ratchet_floor_gain(cfg.profit_ratchet, peak / basis - 1.0), 0.10)
        self.assertGreater(basis * 1.10, 3.58, "the tighter floor beats the exit the bot took")
        self.assertEqual(executor.decide_exit(basis, basis * 1.10 - 0.05, time.time() - 60,
                                              time.time(), cfg, peak_usd=peak), "profit_floor")

    def test_the_ratchet_locks_more_than_the_trail_on_a_big_runner(self):
        """Where the ratchet is meant to earn its keep: a copy that ran to +150% and died. Both
        rungs have sold, so this is the uncapped runner state on the 40% trail, which only protects
        down to 12.50*0.60 = $7.50; the +120%->+70% step holds $8.50 instead."""
        executor, ex, p = fresh()
        self.addCleanup(p.stop)
        cfg = ex.exit_cfg({"copy": "wallet", "mint": "m",
                           "ladder": [{"x": 1.4, "pct": 30, "done": True},
                                      {"x": 1.8, "pct": 30, "done": True}]})
        basis, peak = 5.00, 12.50                      # +150%
        self.assertEqual(cfg.trailing_stop, 0.40)
        self.assertEqual(executor.ratchet_floor_gain(cfg.profit_ratchet, peak / basis - 1.0), 0.70)
        self.assertGreater(basis * 1.70, peak * (1.0 - cfg.trailing_stop),
                           "ratchet floor beats the 40% runner trail")
        self.assertEqual(executor.decide_exit(basis, basis * 1.70 - 0.10, time.time() - 60,
                                              time.time(), cfg, peak_usd=peak), "profit_floor")


if __name__ == "__main__":
    unittest.main()
