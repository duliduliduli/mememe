"""Moon bags: kept only on winning exits, sold at a target multiple, still panic-liquidated."""
import os
import tempfile
import unittest
from unittest import mock


def fresh(**env):
    tmp = tempfile.mkdtemp(prefix="grad-moonbag-test-")
    base = {"DATA_DIR": tmp, "HELIUS_API_KEY": "k", "MIGRATION_ADDRESS": "a", "EXECUTOR_MODE": "paper", "MOON_BAG": "0.2"}
    base.update(env)
    patcher = mock.patch.dict(os.environ, base)
    patcher.start()
    import importlib
    import executor
    importlib.reload(executor)
    return executor, patcher


SOL = 100.0


def sol_quote(usd):
    """Quote returning `usd` worth of SOL for whatever amount is asked."""
    return lambda i, o, amount, **kw: {"outAmount": str(int(usd / SOL * 1e9))}


def position(last_value=None):
    pos = {"mint": "m", "tokens": 1000, "position_usd": 5.0, "opened_ts": 0, "opened_at": "t", "peak_usd": 5.0, "buy_signature": ""}
    if last_value is not None:
        pos["last_value_usd"] = last_value
    return pos


class WinnersOnlyTests(unittest.TestCase):
    def test_defaults(self):
        executor, p = fresh()
        self.addCleanup(p.stop)
        cfg = executor.Config()
        # No exit sells everything by default: a bag stays, winners or losers, never burned.
        import os as _os
        self.assertAlmostEqual(cfg.moon_bag, float(_os.environ.get("MOON_BAG", "0.10")))
        self.assertTrue(cfg.moon_bag_winners_only)
        self.assertEqual(cfg.moon_bag_dead_pct, 10)
        self.assertEqual(cfg.moon_bag_target_x, 100)
        self.assertEqual(cfg.moon_bag_check_seconds, 180)

    def test_bag_kept_on_winning_exit(self):
        executor, p = fresh()
        self.addCleanup(p.stop)
        ex = executor.Executor(executor.Config())
        ex.state["positions"] = [position(last_value=8.75)]
        ex.jup.quote = sol_quote(7.0)  # selling 80% of the tokens returns $7
        ex.close_position(ex.state["positions"][0], "take_profit", SOL)
        bags = ex.state["moon_bags"]
        self.assertEqual(len(bags), 1)
        self.assertEqual(bags[0]["tokens"], 200)
        self.assertAlmostEqual(bags[0]["kept_usd"], 1.75)  # 7.0 * 200 / 800
        self.assertEqual(bags[0]["cost_usd"], 1.0)

    def test_no_bag_on_losing_exit(self):
        executor, p = fresh(MOON_BAG_WINNERS_ONLY="1")
        self.addCleanup(p.stop)
        ex = executor.Executor(executor.Config())
        ex.state["positions"] = [position(last_value=3.4)]
        asked = []
        ex.jup.quote = lambda i, o, amount, **kw: (asked.append(amount), {"outAmount": str(int(3.4 / SOL * 1e9))})[1]
        ex.close_position(ex.state["positions"][0], "stop_loss", SOL)
        self.assertEqual(ex.state.get("moon_bags", []), [])
        self.assertEqual(asked, [1000])  # every token sold

    def test_unknown_value_counts_as_loser(self):
        executor, p = fresh()
        self.addCleanup(p.stop)
        ex = executor.Executor(executor.Config())
        ex.state["positions"] = [position()]
        ex.jup.quote = sol_quote(9.0)
        ex.close_position(ex.state["positions"][0], "time_stop", SOL)
        self.assertEqual(ex.state.get("moon_bags", []), [])

    def test_winners_only_off_keeps_bag_on_loser(self):
        executor, p = fresh(MOON_BAG_WINNERS_ONLY="0")
        self.addCleanup(p.stop)
        ex = executor.Executor(executor.Config())
        ex.state["positions"] = [position(last_value=3.4)]
        ex.jup.quote = sol_quote(2.7)
        ex.close_position(ex.state["positions"][0], "stop_loss", SOL)
        self.assertEqual(len(ex.state["moon_bags"]), 1)


def bag(kept=1.75, tokens=200):
    return {"mint": "m", "tokens": tokens, "cost_usd": 1.0, "kept_usd": kept, "peak_usd": kept, "created_at": "t", "from_exit": "take_profit"}


class TargetTests(unittest.TestCase):
    def setUp(self):
        self.executor, p = fresh()
        self.addCleanup(p.stop)
        self.ex = self.executor.Executor(self.executor.Config())
        self.ex.state["moon_bags"] = [bag()]
        self.ex.state["paper_balance_usd"] = 100.0

    def test_sells_at_target_multiple(self):
        self.ex.jup.quote = sol_quote(175.0)  # 100x of $1.75
        self.ex.manage_moon_bags(SOL)
        self.assertEqual(self.ex.state["moon_bags"], [])
        self.assertAlmostEqual(self.ex.state["paper_balance_usd"], 275.0)
        self.assertAlmostEqual(self.ex.state["daily"]["realized_pnl_usd"], 174.0)  # vs $1 cost basis
        trades = open(os.path.join(os.environ["DATA_DIR"], "live_trades.csv")).read()
        self.assertIn("moon_bag_target", trades)

    def test_holds_below_target_and_tracks_progress(self):
        self.ex.jup.quote = sol_quote(87.5)  # 50x
        self.ex.manage_moon_bags(SOL)
        b = self.ex.state["moon_bags"][0]
        self.assertEqual(len(self.ex.state["moon_bags"]), 1)
        self.assertEqual(b["last_x"], 50.0)
        self.assertEqual(b["peak_usd"], 87.5)
        self.assertEqual(b["last_value_usd"], 87.5)

    def test_check_interval_respected(self):
        calls = []
        self.ex.jup.quote = lambda *a, **kw: (calls.append(1), {"outAmount": str(int(1.0 / SOL * 1e9))})[1]  # alive, below target
        with mock.patch.object(self.executor, "now_ts", return_value=1000.0):
            self.ex.manage_moon_bags(SOL)
            self.ex.manage_moon_bags(SOL)
        with mock.patch.object(self.executor, "now_ts", return_value=1181.0):
            self.ex.manage_moon_bags(SOL)
        self.assertEqual(len(calls), 2)

    def test_target_zero_holds_forever(self):
        self.ex.cfg.moon_bag_target_x = 0
        self.ex.jup.quote = sol_quote(1_000_000.0)
        self.ex.manage_moon_bags(SOL)
        self.assertEqual(len(self.ex.state["moon_bags"]), 1)

    def test_unquotable_bag_is_skipped_quietly(self):
        self.ex.jup.quote = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("no route"))
        self.ex.manage_moon_bags(SOL)
        self.assertEqual(len(self.ex.state["moon_bags"]), 1)

    def test_panic_still_liquidates_bags(self):
        self.ex.jup.quote = sol_quote(0.5)
        self.ex.liquidate_bags(SOL, "moon_bags", "panic_moon_bag")
        self.assertEqual(self.ex.state["moon_bags"], [])
        self.assertAlmostEqual(self.ex.state["paper_balance_usd"], 100.5)


class CleanupTests(unittest.TestCase):
    def test_tiny_bag_not_kept(self):
        executor, p = fresh(MOON_BAG="0.05")
        self.addCleanup(p.stop)
        ex = executor.Executor(executor.Config())
        ex.state["positions"] = [position(last_value=7.0)]  # 5% of $7 = $0.35 < $0.50
        ex.jup.quote = sol_quote(7.0)
        ex.close_position(ex.state["positions"][0], "take_profit", SOL)
        self.assertEqual(ex.state.get("moon_bags", []), [])

    def test_bag_kept_when_above_minimum(self):
        executor, p = fresh(MOON_BAG="0.05", MIN_MOON_BAG_USD="0.25")
        self.addCleanup(p.stop)
        ex = executor.Executor(executor.Config())
        ex.state["positions"] = [position(last_value=7.0)]
        ex.jup.quote = sol_quote(6.65)
        ex.close_position(ex.state["positions"][0], "take_profit", SOL)
        self.assertEqual(len(ex.state["moon_bags"]), 1)

    def test_dead_bag_burned_and_recorded(self):
        executor, p = fresh(MOON_BAG_DEAD_PCT="5")
        self.addCleanup(p.stop)
        ex = executor.Executor(executor.Config())
        ex.state["moon_bags"] = [bag(kept=1.75)]
        ex.jup.quote = sol_quote(0.05)  # under 5% of $1.75
        ex.manage_moon_bags(SOL)
        self.assertEqual(ex.state["moon_bags"], [])
        trades = open(os.path.join(os.environ["DATA_DIR"], "live_trades.csv")).read()
        self.assertIn("moon_bag_dead", trades)
        self.assertAlmostEqual(ex.state["daily"]["realized_pnl_usd"], -1.0)

    def test_dead_check_disabled_by_zero(self):
        executor, p = fresh(MOON_BAG_DEAD_PCT="0")
        self.addCleanup(p.stop)
        ex = executor.Executor(executor.Config())
        ex.state["moon_bags"] = [bag(kept=1.75)]
        ex.jup.quote = sol_quote(0.01)
        ex.manage_moon_bags(SOL)
        self.assertEqual(len(ex.state["moon_bags"]), 1)

    def test_error_text_redacts_api_key(self):
        executor, p = fresh()
        self.addCleanup(p.stop)
        text = executor.describe_error(RuntimeError("429 for url: https://rpc.example/?api-key=SECRET123&x=1"))
        self.assertNotIn("SECRET123", text)
        self.assertEqual(text, "429 for url: https://rpc.example/…")


if __name__ == "__main__":
    unittest.main()
