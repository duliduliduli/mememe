"""GMGN wallet scouting: discovery, qualification gates, shadow evaluation and controlled
promotion. The lane never trades; the acceptance tests below are the ones the handoff spec
names, plus unit tests for the pure analytics."""
import json
import os
import tempfile
import time
import unittest
from unittest import mock

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="grad-scout-test-"))
# The tests below exercise the full policy (every gate decides). The fast track, on by default
# in production, has its own tests that switch it on explicitly.
os.environ["SCOUT_FAST_TRACK"] = "0"
os.environ["GMGN_UNITS_PER_SECOND"] = "100000"      # the shared GMGN limiter never waits in tests
os.environ["SCOUT_LIVE"] = "0"                      # promotion is on in production; tests that need it set it explicitly

import scout
from test_copy import MINT, OTHER, SOL, WALLET, WSOL, fresh, tx

DAY = 86400.0
NOW = 1_800_000_000.0
THIRD = "9xQeWvG816bUx9EPjHmaT23yvVM2ZWbrrpZb9PusVFin"


def cfg_with(**env):
    with mock.patch.dict(os.environ, {k: str(v) for k, v in env.items()}):
        return scout.ScoutConfig()


def activity(events):
    """GMGN activity rows from (event_type, token, ts, amount, usd) tuples."""
    return [{"event_type": kind, "token": {"address": token}, "timestamp": ts, "token_amount": amount, "cost_usd": usd, "gas_usd": 0.01}
            for kind, token, ts, amount, usd in events]


def good_episodes(now=NOW, tokens=40, winners=30, first_buy=500.0):
    """Closed episodes spread over the last 30 days: PF 3, no outlier, two-hour holds."""
    out = []
    for i in range(tokens):
        opened = now - (i % 28 + 1) * DAY - 3600 * (i % 5)
        win = i < winners
        out.append({"token": f"tok{i}", "opened_ts": opened, "first_buy_usd": first_buy, "cost_usd": first_buy,
                    "proceeds_usd": first_buy + (100.0 if win else -100.0), "fees_usd": 0.0, "adds": 0, "sells": 1,
                    "first_material_sell_ts": opened + 7200, "closed_ts": opened + 7200, "closed": True,
                    "pnl_usd": 100.0 if win else -100.0})
    return out


def good_shadow_trades(wallet, now=NOW, n=36, tokens=20, cost=10.0):
    """36 closed shadow trades over 16 days and 20 tokens, three winners per loser, each open
    one hour with two overlapping, stress a little worse than baseline."""
    out = []
    for i in range(n):
        opened = now - 16 * DAY + i * (16 * DAY / n)
        loser = i % 4 == 3
        pnl = -1.0 if loser else 2.0
        out.append({"wallet": wallet, "mint": f"tok{i % tokens}", "signature": f"sig{i}", "source_usd": 500, "opened_ts": opened,
                    "closed_ts": opened + 3600, "cost_usd": cost, "pnl_base": pnl, "pnl_stress": pnl - 0.2, "reason": "ladder_1.4x",
                    "portfolio_admitted": True})
    return out


def good_candidate(address, now=NOW, configured=False, **over):
    cand = {
        "address": address, "state": "shadow", "state_since": now - 20 * DAY, "discovered_at": now - 20 * DAY, "lifecycle": [],
        "sources": [{"source": "configured" if configured else "smartmoney", "ts": now - 20 * DAY}], "last_refresh": now - 3600,
        "tags": ["smart_degen"], "profile": {"fund_from_address": "", "followers_count": 1000, "is_blue_verified": False},
        "exposure": {"known": True, "followers": 1000, "verified": False},
        "stats": {p: {"realized_profit": 2000.0, "buys": 60, "sells": 60, "bought_cost": 20000.0, "winrate": 0.7, "token_num": 40,
                      "last_timestamp": int(now - 3600)}
                  for p in ("7d", "30d", "all")},
        "holdings": {"tokens": 5, "transfer_in_tokens": 0, "transfer_in_cost_usd": 0.0, "open_loss_usd": 50.0},
        "rank_snapshots": [{"ts": now - d * DAY, "scope": "global", "list": "gmgn:smart_degen", "rank": 12, "population": 0}
                           for d in (0, 3, 7)],
        "risk_flags": [],
        "history": {"coverage_days": 90.0, "events": 500, "truncated": False, "unmatched_sells": 0, "episodes": []},
    }
    cand["history"]["metrics_30d"] = scout.history_metrics(good_episodes(now), now, 30, 300.0, set(), 20000.0)
    cand["history"]["metrics_7d"] = scout.history_metrics(good_episodes(now), now, 7, 300.0, set(), 20000.0)
    cand["shadow"] = scout.shadow_metrics(good_shadow_trades(address, now), now, now - 16 * DAY)
    cand.update(over)
    return cand


class EpisodeTests(unittest.TestCase):
    def test_episodes_open_on_first_buy_and_close_at_dust(self):
        rows = activity([("buy", "A", 100, 1000, 300), ("buy", "A", 200, 500, 150), ("sell", "A", 400, 1200, 700),
                         ("sell", "A", 500, 290, 100), ("buy", "A", 900, 100, 50), ("sell", "B", 950, 10, 20)])
        built = scout.episodes_from_activity(rows, 0.02)
        eps = built["episodes"]
        self.assertEqual(len(eps), 2)
        closed = eps[0]
        self.assertTrue(closed["closed"])
        self.assertEqual(closed["adds"], 1)
        self.assertEqual(closed["first_buy_usd"], 300)
        self.assertAlmostEqual(closed["pnl_usd"], 800 - 450 - 0.04)
        self.assertEqual(closed["first_material_sell_ts"], 400)
        self.assertEqual(closed["closed_ts"], 500)                                   # 10 of 1500 peak left: dust
        self.assertFalse(eps[1]["closed"])                                           # re-entry still open
        self.assertIsNone(eps[1]["pnl_usd"])
        self.assertEqual(built["unmatched_sells"], 1)                                # B sold with no observed buy
        self.assertEqual(built["unmatched_sell_usd"], 20.0)

    def test_history_metrics_excludes_transferred_inventory(self):
        eps = good_episodes()
        eps.append({"token": "gift", "opened_ts": NOW - 2 * DAY, "first_buy_usd": 400, "cost_usd": 400, "proceeds_usd": 9400, "fees_usd": 0,
                    "adds": 0, "sells": 1, "first_material_sell_ts": NOW - DAY, "closed_ts": NOW - DAY, "closed": True, "pnl_usd": 9000.0})
        with_gift = scout.history_metrics(eps, NOW, 30, 300.0, set(), 20000.0)
        without = scout.history_metrics(eps, NOW, 30, 300.0, {"gift"}, 20000.0)
        self.assertEqual(with_gift["net_pnl_usd"], 3000 - 1000 + 9000)
        self.assertEqual(without["net_pnl_usd"], 2000.0)
        self.assertEqual(without["excluded_episodes"], 1)
        self.assertEqual(without["closed_episodes"], 40)
        self.assertEqual(without["profit_factor"], 3.0)
        self.assertEqual(without["median_hold_minutes"], 120.0)
        self.assertEqual(without["fast_exit_fraction"], 0.0)
        self.assertEqual(without["drawdown_basis"], "realized episode equity, open positions not marked")

    def test_profit_factor_is_finite_and_json_safe(self):
        self.assertEqual(scout.profit_factor(100.0, 0.0), scout.PROFIT_FACTOR_CAP)
        self.assertIsNone(scout.profit_factor(0.0, 0.0))
        self.assertEqual(scout.profit_factor(300.0, 100.0), 3.0)
        m = scout.history_metrics(good_episodes(winners=40), NOW, 30, 300.0, set(), 20000.0)
        json.dumps(m, allow_nan=False)

    def test_bootstrap_lower_bound_resamples_clusters(self):
        self.assertIsNone(scout.cluster_bootstrap_lower_bound([[0.1, 0.2]]))            # one cluster is not evidence
        good = [[0.2, 0.3], [0.1], [0.25, 0.2], [0.15], [0.3], [0.2, 0.1]]
        self.assertGreater(scout.cluster_bootstrap_lower_bound(good), 0)
        mixed = [[0.5], [-0.4], [0.3], [-0.5], [0.1], [-0.2]]
        self.assertLess(scout.cluster_bootstrap_lower_bound(mixed), 0)
        self.assertEqual(scout.cluster_bootstrap_lower_bound(good), scout.cluster_bootstrap_lower_bound(good))   # seeded

    def test_shadow_metrics(self):
        m = scout.shadow_metrics(good_shadow_trades(WALLET), NOW, NOW - 16 * DAY)
        self.assertEqual(m["trades"], 36)
        self.assertEqual(m["distinct_tokens"], 20)
        self.assertGreaterEqual(m["active_days"], 7)
        self.assertEqual(m["calendar_days"], 16.0)
        self.assertEqual(m["net_base_usd"], 27 * 2 - 9)
        self.assertAlmostEqual(m["net_stress_usd"], 45 - 36 * 0.2, places=2)
        self.assertEqual(m["stress_coverage"], 1.0)
        self.assertEqual(m["profit_factor"], 6.0)
        self.assertEqual(m["drawdown_usd"], 1.0)
        self.assertLessEqual(m["drawdown_fraction"], 0.15)
        self.assertGreater(m["bootstrap_lower_bound"], 0)
        self.assertEqual(scout.shadow_metrics([], NOW, NOW - DAY)["trades"], 0)


class LeaderboardTests(unittest.TestCase):
    def test_token_lists_never_count_and_missing_is_not_a_pass(self):
        cfg = scout.ScoutConfig()
        token_only = [{"ts": NOW - d * DAY, "scope": "token", "list": "top_traders:abc", "rank": 1, "population": 20} for d in range(10)]
        status = scout.leaderboard_status(token_only, cfg, NOW)
        self.assertEqual(status["status"], "missing")
        self.assertEqual(status["token_list_snapshots"], 10)
        self.assertEqual(scout.leaderboard_status([], cfg, NOW)["status"], "missing")

    def test_global_rank_needs_three_days_over_a_week(self):
        cfg = scout.ScoutConfig()
        two_days = [{"ts": NOW - d * DAY, "scope": "global", "list": "gmgn:x", "rank": 5, "population": 0} for d in (0, 8)]
        self.assertEqual(scout.leaderboard_status(two_days, cfg, NOW)["status"], "fail")
        three_close = [{"ts": NOW - d * DAY, "scope": "global", "list": "gmgn:x", "rank": 5, "population": 0} for d in (0, 1, 2)]
        self.assertEqual(scout.leaderboard_status(three_close, cfg, NOW)["status"], "fail")          # span 2 days < 7
        good = [{"ts": NOW - d * DAY, "scope": "global", "list": "gmgn:x", "rank": 5, "population": 0} for d in (0, 4, 7)]
        self.assertEqual(scout.leaderboard_status(good, cfg, NOW)["status"], "pass")
        low = [{"ts": NOW - d * DAY, "scope": "global", "list": "gmgn:x", "rank": 150, "population": 0} for d in (0, 4, 7)]
        self.assertEqual(scout.leaderboard_status(low, cfg, NOW)["status"], "fail")                 # rank 150 > top 100
        pct = [{"ts": NOW - d * DAY, "scope": "global", "list": "gmgn:x", "rank": 150, "population": 10000} for d in (0, 4, 7)]
        self.assertEqual(scout.leaderboard_status(pct, cfg, NOW)["status"], "pass")                 # 150 of 10,000 is top 5%


class GateTests(unittest.TestCase):
    def test_a_complete_candidate_qualifies(self):
        cand = good_candidate(WALLET)
        ev = scout.evaluate_candidate(cand, scout.ScoutConfig(), NOW, 300.0)
        self.assertEqual(ev["failed"], [], ev["gates"])
        self.assertEqual(ev["missing"], [], ev["gates"])
        self.assertTrue(ev["qualified"])
        self.assertEqual(ev["policy_version"], scout.policy_key(scout.ScoutConfig()))
        self.assertGreater(ev["score"]["total"], 50)
        self.assertEqual(sum(ev["score"]["weights"].values()), 100)

    def test_followed_but_weak_wallet_cannot_qualify(self):
        cand = good_candidate(WALLET, configured=True)
        cand["stats"]["30d"]["realized_profit"] = -120.0
        cand["history"]["metrics_30d"] = scout.history_metrics(good_episodes(winners=12), NOW, 30, 300.0, set(), 20000.0)
        ev = scout.evaluate_candidate(cand, scout.ScoutConfig(), NOW, 300.0)
        self.assertFalse(ev["qualified"])
        self.assertIn("pnl_30d", ev["failed"])
        self.assertIn("profit_factor_30d", ev["failed"])
        self.assertIn("outlier_30d", ev["failed"])

    def test_missing_evidence_cannot_qualify(self):
        cand = good_candidate(WALLET)
        cand["rank_snapshots"] = []                                           # no leaderboard evidence at all
        ev = scout.evaluate_candidate(cand, scout.ScoutConfig(), NOW, 300.0)
        self.assertEqual(ev["failed"], [])
        self.assertEqual(ev["missing"], ["leaderboard"])
        self.assertFalse(ev["qualified"])
        cand = good_candidate(WALLET)
        cand["history"] = {"coverage_days": 20.0, "truncated": True, "events": 800}
        ev = scout.evaluate_candidate(cand, scout.ScoutConfig(), NOW, 300.0)
        self.assertIn("history_days", ev["missing"])
        self.assertIn("episodes_30d", ev["missing"])
        self.assertFalse(ev["qualified"])
        cand = good_candidate(WALLET)
        cand["shadow"] = scout.shadow_metrics(good_shadow_trades(WALLET)[:10], NOW, NOW - 3 * DAY)
        ev = scout.evaluate_candidate(cand, scout.ScoutConfig(), NOW, 300.0)
        self.assertIn("shadow_sample", ev["missing"])
        self.assertIn("shadow_bootstrap", ev["missing"])
        self.assertFalse(ev["qualified"])

    def test_one_token_jackpot_fails_outlier_and_concentration(self):
        eps = good_episodes(winners=1)
        eps[0]["proceeds_usd"], eps[0]["pnl_usd"] = 20500.0, 20000.0            # one huge win, 39 small losses
        cand = good_candidate(WALLET)
        cand["history"]["metrics_30d"] = scout.history_metrics(eps, NOW, 30, 300.0, set(), 20000.0)
        ev = scout.evaluate_candidate(cand, scout.ScoutConfig(), NOW, 300.0)
        self.assertIn("outlier_30d", ev["failed"])
        self.assertIn("best_token_share", ev["failed"])
        self.assertFalse(ev["qualified"])

    def test_transfer_in_profits_are_excluded_from_qualification(self):
        eps = good_episodes(winners=10)
        eps.append({"token": "gift", "opened_ts": NOW - 2 * DAY, "first_buy_usd": 400, "cost_usd": 400, "proceeds_usd": 9400, "fees_usd": 0,
                    "adds": 0, "sells": 1, "first_material_sell_ts": NOW - DAY, "closed_ts": NOW - DAY, "closed": True, "pnl_usd": 9000.0})
        cand = good_candidate(WALLET)
        cand["history"]["metrics_30d"] = scout.history_metrics(eps, NOW, 30, 300.0, {"gift"}, 20000.0)
        cand["holdings"]["transfer_in_tokens"] = 1
        ev = scout.evaluate_candidate(cand, scout.ScoutConfig(), NOW, 300.0)
        self.assertIn("profit_factor_30d", ev["failed"])                        # without the gift: 10 wins / 30 losses
        self.assertIn("outlier_30d", ev["failed"])
        self.assertFalse(ev["qualified"])

    def test_stress_failure_blocks_promotion(self):
        trades = good_shadow_trades(WALLET)
        for t in trades:
            t["pnl_stress"] = t["pnl_base"] - 1.5                               # 20s later every fill is worse
        cand = good_candidate(WALLET)
        cand["shadow"] = scout.shadow_metrics(trades, NOW, NOW - 16 * DAY)
        ev = scout.evaluate_candidate(cand, scout.ScoutConfig(), NOW, 300.0)
        self.assertEqual(ev["gates"]["shadow_baseline"]["status"], "pass")
        self.assertEqual(ev["gates"]["shadow_stress"]["status"], "fail")
        self.assertFalse(ev["qualified"])

    def test_bad_tags_and_open_losses(self):
        cand = good_candidate(WALLET, tags=["bundler"])
        ev = scout.evaluate_candidate(cand, scout.ScoutConfig(), NOW, 300.0)
        self.assertEqual(ev["gates"]["tags"]["status"], "fail")
        cand = good_candidate(WALLET)
        cand["holdings"]["open_loss_usd"] = 5000.0                               # more open losses than 30d realized
        ev = scout.evaluate_candidate(cand, scout.ScoutConfig(), NOW, 300.0)
        self.assertEqual(ev["gates"]["open_inventory"]["status"], "fail")

    def test_relationship_clusters(self):
        a, b, c = good_candidate(WALLET), good_candidate(OTHER), good_candidate(THIRD)
        a["profile"]["fund_from_address"] = b["profile"]["fund_from_address"] = "Funder1"
        sync = [{"token": f"t{i}", "opened_ts": NOW - i * DAY} for i in range(3)]
        a["history"]["episodes"] = sync
        c["history"]["episodes"] = [dict(e, opened_ts=e["opened_ts"] + 60) for e in sync]
        clusters = scout.relationship_clusters({WALLET: a, OTHER: b, THIRD: c})
        self.assertEqual(clusters[WALLET]["cluster"], clusters[OTHER]["cluster"])
        self.assertEqual(clusters[WALLET]["cluster"], clusters[THIRD]["cluster"])
        self.assertEqual(clusters[OTHER]["confidence"], "low")
        self.assertEqual(clusters[THIRD]["confidence"], "medium")
        self.assertEqual(clusters[WALLET]["size"], 3)
        alone = scout.relationship_clusters({WALLET: good_candidate(WALLET)})
        self.assertIsNone(alone[WALLET]["cluster"])


class ConfigTests(unittest.TestCase):
    def test_defaults_are_shadow_with_promotion_off(self):
        with mock.patch.dict(os.environ, {"GMGN_API_KEY": ""}):
            for key in list(os.environ):
                if key.startswith("SCOUT_"):
                    del os.environ[key]
            cfg = scout.ScoutConfig()
            self.assertEqual(cfg.mode, "shadow")
            self.assertTrue(cfg.live)                                             # promotion on: fast track + tripwire + budget guard it
            self.assertTrue(cfg.elite_only)                                       # unproven configured wallets are not copied
            self.assertFalse(cfg_with(SCOUT_ELITE_ONLY="0").elite_only)
            self.assertTrue(cfg.convergence)
            self.assertEqual(cfg.convergence_min_wallets, 2)
            self.assertEqual(cfg.convergence_window_seconds, 600.0)
            self.assertEqual(cfg.max_live, 3)
            self.assertEqual(cfg.live_size, 0.5)
            self.assertEqual(cfg.max_open_positions, 3)
            self.assertEqual(cfg.live_loss_budget_usd, 25.0)
            self.assertTrue(cfg.fast_track)
            self.assertEqual(cfg.fast_track_min_history_days, 15.0)
            self.assertEqual(cfg.fast_track_dense_episodes, 30)
            self.assertEqual(cfg.fast_track_recent_days, 7.0)
            self.assertEqual(cfg.discovery_hours, 2.0)
            self.assertEqual(cfg.enrich_per_cycle, 12)
            self.assertEqual(cfg.pause_seconds, 2.0)
            self.assertEqual(cfg.rate_limit_cooldown_hours, 0.25)
            self.assertEqual(cfg.tripwire_trades, 10)
            self.assertFalse(cfg.enabled)                                        # no key: nothing runs
        self.assertTrue(cfg_with(GMGN_API_KEY="k").enabled)
        self.assertFalse(cfg_with(GMGN_API_KEY="k", SCOUT_MODE="off").enabled)


class LaneTests(unittest.TestCase):
    def make(self, **env):
        env.setdefault("GMGN_API_KEY", "k")
        executor, p = fresh(**env)
        self.addCleanup(p.stop)
        ex = executor.Executor(executor.Config())
        self.assertIsNotNone(ex.scout)
        return executor, ex

    def seed(self, ex, address, state="qualified", evaluated_at=None, **over):
        lane = ex.scout
        now = time.time()
        cand = good_candidate(address, now=now, **over)
        cand["state"] = state
        cand["evaluation"] = scout.evaluate_candidate(cand, lane.cfg, now, 300.0)
        if evaluated_at is not None:
            cand["evaluation"]["evaluated_at"] = evaluated_at
        lane.st["candidates"][address] = cand
        lane.st["trades"].extend(good_shadow_trades(address, now))                 # evaluate_all rebuilds shadow metrics from here
        lane.st["signals"].append({"ts": now - 16 * DAY, "wallet": address, "status": "shadow_opened"})
        return cand

    # -- promotion -------------------------------------------------------------------------
    def test_empty_qualified_set_stays_empty(self):
        executor, ex = self.make(SCOUT_LIVE="1", SCOUT_LIVE_LOSS_BUDGET_USD="50")
        self.seed(ex, OTHER, state="shadow")
        ex.scout.st["candidates"][OTHER]["rank_snapshots"] = []
        ex.scout.evaluate_all()
        self.assertEqual(ex.scout.st["candidates"][OTHER]["state"], "shadow")
        self.assertEqual(ex.scout.live_wallets(), {})
        self.assertEqual(ex.followed_wallets(), [WALLET])
        self.assertIn("no wallet passes every mandatory gate", ex.scout.st["last_selection"]["reason"])

    def test_promotion_needs_switch_and_budget(self):
        executor, ex = self.make(SCOUT_LIVE_LOSS_BUDGET_USD="0")                 # SCOUT_LIVE unset, budget cleared
        self.seed(ex, OTHER, state="shadow")
        ex.scout.evaluate_all()
        self.assertEqual(ex.scout.st["candidates"][OTHER]["state"], "qualified")
        self.assertEqual(ex.scout.live_wallets(), {})
        self.assertIn("SCOUT_LIVE=0", ex.scout.st["last_selection"]["reason"])
        ex.scout.cfg.live = True                                                  # switch on, budget still 0
        ex.scout.evaluate_all()
        self.assertEqual(ex.scout.st["candidates"][OTHER]["state"], "qualified")
        self.assertIn("SCOUT_LIVE_LOSS_BUDGET_USD", ex.scout.st["last_selection"]["reason"])
        ex.scout.cfg.live_loss_budget_usd = 50.0
        ex.scout.evaluate_all()
        self.assertEqual(ex.scout.st["candidates"][OTHER]["state"], "live")
        self.assertEqual(ex.scout.live_wallets(), {OTHER: {"size": 0.5, "min_usd": 300.0}})
        self.assertEqual(ex.followed_wallets(), [WALLET, OTHER])

    def test_related_wallets_one_live_per_cluster(self):
        executor, ex = self.make(SCOUT_LIVE="1", SCOUT_LIVE_LOSS_BUDGET_USD="50")
        a, b = self.seed(ex, OTHER, state="shadow"), self.seed(ex, THIRD, state="shadow")
        sync = [{"token": f"t{i}", "opened_ts": time.time() - i * DAY} for i in range(3)]
        a["history"]["episodes"], b["history"]["episodes"] = sync, [dict(e, opened_ts=e["opened_ts"] + 30) for e in sync]
        clusters = scout.relationship_clusters(ex.scout.st["candidates"])
        for addr, c in ex.scout.st["candidates"].items():
            c["relationship"] = clusters[addr]
        ex.scout.evaluate_all()          # qualified
        ex.scout.evaluate_all()          # promoted
        states = sorted(c["state"] for c in ex.scout.st["candidates"].values())
        self.assertEqual(states, ["live", "qualified"])
        self.assertEqual(len(ex.scout.live_wallets()), 1)

    def test_max_live_and_configured_wallets_are_never_promoted(self):
        executor, ex = self.make(SCOUT_LIVE="1", SCOUT_LIVE_LOSS_BUDGET_USD="50", SCOUT_MAX_LIVE="1")
        self.seed(ex, WALLET, state="qualified", configured=True)
        self.seed(ex, OTHER, state="qualified")
        self.seed(ex, THIRD, state="qualified")
        ex.scout.select_live()
        live = [a for a, c in ex.scout.st["candidates"].items() if c["state"] == "live"]
        self.assertEqual(len(live), 1)
        self.assertNotIn(WALLET, live)

    def test_live_loss_budget_pauses_the_wallet(self):
        executor, ex = self.make(SCOUT_LIVE="1", SCOUT_LIVE_LOSS_BUDGET_USD="20")
        self.seed(ex, OTHER, state="live")
        ex.note_copy_pnl({"copy": OTHER}, -8.0)
        self.assertEqual(ex.scout.st["candidates"][OTHER]["state"], "live")
        ex.note_copy_pnl({"copy": OTHER}, -13.0)
        self.assertEqual(ex.scout.st["candidates"][OTHER]["state"], "paused")
        self.assertEqual(ex.scout.live_wallets(), {})
        self.assertEqual(ex.scout.st["live"][OTHER]["realized_pnl_usd"], -21.0)
        ex.scout.evaluate_all()                                                   # still qualified on paper: budget keeps it paused
        self.assertEqual(ex.scout.st["candidates"][OTHER]["state"], "paused")

    def test_demotion_when_a_gate_stops_passing(self):
        executor, ex = self.make(SCOUT_LIVE="1", SCOUT_LIVE_LOSS_BUDGET_USD="50")
        cand = self.seed(ex, OTHER, state="live")
        cand["stats"]["7d"]["realized_profit"] = -5.0
        ex.scout.evaluate_all()
        self.assertEqual(cand["state"], "paused")
        self.assertIn("pnl_7d", cand["lifecycle"][-1]["reason"])
        cand["stats"]["7d"]["realized_profit"] = 50.0
        ex.scout.evaluate_all()
        self.assertEqual(cand["state"], "paused")                                 # requalification cooldown
        cand["demoted_ts"] = time.time() - 2 * DAY
        ex.scout.evaluate_all()
        self.assertEqual(cand["state"], "live")                                   # requalified and re-promoted

    # -- the production copy lane ----------------------------------------------------------
    def test_scouted_live_wallet_is_copied_at_reduced_size(self):
        executor, ex = self.make(SCOUT_LIVE="1", SCOUT_LIVE_LOSS_BUDGET_USD="50")
        self.seed(ex, OTHER, state="live")
        entered = []
        ex.enter_with_retry = lambda item, sol_price: entered.append(item)
        ex.copy_handle_event(OTHER, "sig1", int(time.time()), tx(10.0, 5.0, 0, 1000, owner=OTHER), SOL, True)
        self.assertEqual(len(entered), 1)
        self.assertEqual(entered[0]["copy_size"], 0.5)
        self.assertTrue(entered[0]["copy_scouted"])
        self.assertEqual(ex.copy_wallet_terms(WALLET), (300.0, 1.0, False))

    def test_scouted_size_below_minimum_is_skipped_not_rounded_up(self):
        executor, ex = self.make(SCOUT_LIVE="1", SCOUT_LIVE_LOSS_BUDGET_USD="50", MAX_POSITION_USD="12", MIN_POSITION_USD="5")
        ex.state["paper_balance_usd"] = 120.0
        skips = []
        ex.skip = lambda mint, why, **kw: skips.append(why)
        ex.try_enter({"mint": MINT, "graduated_ts": time.time(), "enter_at": time.time(), "copy": OTHER, "copy_size": 0.25,
                      "copy_scouted": True}, SOL)
        self.assertEqual(ex.state["positions"], [])
        self.assertTrue(skips and "below the $5.00 minimum" in skips[-1], skips)

    def test_scouted_wallets_share_a_small_pool_of_slots(self):
        executor, ex = self.make(SCOUT_LIVE="1", SCOUT_MAX_OPEN_POSITIONS="2", SCOUT_ELITE_ONLY="0")
        self.seed(ex, OTHER, state="live")
        entered = []
        ex.enter_with_retry = lambda item, sol_price: entered.append(item)
        ex.state["positions"] = [{"mint": f"M{i}", "tokens": 1, "position_usd": 5.0, "opened_ts": 0, "opened_at": "t", "peak_usd": 5.0,
                                  "buy_signature": "", "copy": OTHER, "copy_scouted": True} for i in range(2)]
        ex.copy_handle_event(OTHER, "buy1", int(time.time()), tx(10.0, 5.0, 0, 1000, owner=OTHER), SOL, True)
        self.assertEqual(entered, [])                                             # two scouted positions open: full
        ex.copy_handle_event(WALLET, "buy2", int(time.time()), tx(10.0, 5.0, 0, 1000, mint="Other111"), SOL, True)
        self.assertEqual(len(entered), 1)                                         # configured wallets are not capped by it
        ex.state["positions"].pop()
        ex.copy_handle_event(OTHER, "buy3", int(time.time()), tx(10.0, 5.0, 0, 1000, owner=OTHER, mint="Other222"), SOL, True)
        self.assertEqual(len(entered), 2)                                         # a slot freed: mirrored
        rows = list(__import__("csv").DictReader(open(os.path.join(os.environ["DATA_DIR"], "copy_signals.csv"))))
        self.assertEqual([r["reason"] for r in rows if r["status"] == "blocked"], ["scout_slots"])

    def test_stale_elite_only_blocks_buys_but_never_sells(self):
        executor, ex = self.make(SCOUT_ELITE_ONLY="1")
        self.seed(ex, WALLET, state="qualified", evaluated_at=time.time() - 3 * DAY, configured=True)
        entered, sells = [], []
        ex.enter_with_retry = lambda item, sol_price: entered.append(item)
        ex.copy_handle_sell = lambda *a, **k: sells.append(a)
        ex.copy_handle_event(WALLET, "buy1", int(time.time()), tx(10.0, 5.0, 0, 1000), SOL, True)
        self.assertEqual(entered, [])                                             # stale qualification: no new buy
        ex.copy_handle_event(WALLET, "sell1", int(time.time()), tx(5.0, 9.0, 1000, 0), SOL, True)
        self.assertEqual(len(sells), 1)                                           # sells are always followed
        ex.scout.st["candidates"][WALLET]["evaluation"]["evaluated_at"] = time.time()
        ex.copy_handle_event(WALLET, "buy2", int(time.time()), tx(10.0, 5.0, 0, 1000), SOL, True)
        self.assertEqual(len(entered), 1)                                         # fresh: mirrored
        ex.scout.st["candidates"][WALLET]["state"] = "shadow"
        ex.copy_handle_event(WALLET, "buy3", int(time.time()), tx(10.0, 5.0, 0, 1000), SOL, True)
        self.assertEqual(len(entered), 1)                                         # not qualified: blocked
        rows = list(__import__("csv").DictReader(open(os.path.join(os.environ["DATA_DIR"], "copy_signals.csv"))))
        self.assertEqual([r["reason"] for r in rows if r["status"] == "blocked"], ["elite_only", "elite_only"])

    def test_elite_only_off_changes_nothing(self):
        executor, ex = self.make(SCOUT_ELITE_ONLY="0")
        entered = []
        ex.enter_with_retry = lambda item, sol_price: entered.append(item)
        ex.copy_handle_event(WALLET, "buy1", int(time.time()), tx(10.0, 5.0, 0, 1000), SOL, True)
        self.assertEqual(len(entered), 1)
        self.assertFalse(entered[0]["copy_scouted"])

    def test_demotion_preserves_position_management_and_sell_monitoring(self):
        executor, ex = self.make(SCOUT_LIVE="1", SCOUT_LIVE_LOSS_BUDGET_USD="50")
        cand = self.seed(ex, OTHER, state="live")
        ex.state["positions"] = [{"mint": MINT, "tokens": 1000, "position_usd": 10.0, "opened_ts": time.time(), "opened_at": "t",
                                  "peak_usd": 10.0, "buy_signature": "", "copy": OTHER, "copy_scouted": True, "position_id": "p1"}]
        ex.scout.transition(cand, "paused", "test demotion")
        self.assertEqual(ex.scout.live_wallets(), {})
        self.assertEqual(ex.followed_wallets(), [WALLET, OTHER])                   # still polled for its sells
        self.assertEqual(ex.followed_wallets_for_buys(), [WALLET])
        entered, sells = [], []
        ex.enter_with_retry = lambda item, sol_price: entered.append(item)
        ex.copy_handle_sell = lambda *a, **k: sells.append(a)
        ex.copy_handle_event(OTHER, "buy1", int(time.time()), tx(10.0, 5.0, 0, 1000, owner=OTHER, mint="Other111"), SOL, True)
        self.assertEqual(entered, [])
        ex.copy_handle_event(OTHER, "sell1", int(time.time()), tx(5.0, 9.0, 1000, 0, owner=OTHER), SOL, True)
        self.assertEqual(len(sells), 1)
        ex.state["positions"] = []
        self.assertEqual(ex.followed_wallets(), [WALLET])                          # closed: dropped

    def test_removing_a_configured_wallet_still_drops_it(self):
        executor, ex = self.make()
        ex.state["positions"] = [{"mint": MINT, "tokens": 1, "position_usd": 1.0, "opened_ts": 0, "opened_at": "t", "peak_usd": 1.0,
                                  "buy_signature": "", "copy": OTHER}]
        self.assertEqual(ex.followed_wallets(), [WALLET])

    # -- shadow execution --------------------------------------------------------------------
    def fake_market(self, ex, price_ratio=0.95, impact="0.005"):
        """Buy quotes fill 1000 tokens; sell quotes return `price_ratio` of the SOL paid."""
        self.paid = {}
        self.quotes = []
        def quote(inp, out, amount, **kw):
            self.quotes.append((inp, out, amount))
            if inp == WSOL:
                self.paid[out] = amount
                return {"outAmount": "1000", "priceImpactPct": impact}
            return {"outAmount": str(int(self.paid.get(inp, amount) * self.price_ratio)), "priceImpactPct": "0.001"}
        self.price_ratio = price_ratio
        ex.jup.quote = quote
        ex.execute_swap = lambda *a, **k: (_ for _ in ()).throw(AssertionError("shadow must never swap"))
        ex.token_prices = lambda mints: {}

    def test_shadow_buy_and_sell_never_touch_the_wallet(self):
        executor, ex = self.make()
        self.seed(ex, OTHER, state="shadow")
        self.fake_market(ex)
        lane = ex.scout
        lane.reset_quote_budget()
        swap = {"side": "buy", "mint": MINT, "tokens": 1000, "sol": 5.0, "stable_usd": 0.0, "pre_tokens": 0}
        balance = ex.state["paper_balance_usd"]
        lane.shadow_handle_buy(OTHER, "sig1", int(time.time()) - 3, swap, 500.0, 3.0, SOL)
        self.assertEqual(len(lane.st["positions"]), 1)
        pos = lane.st["positions"][0]
        self.assertEqual(pos["tokens"], 1000)
        self.assertTrue(pos["portfolio_admitted"])
        self.assertEqual(pos["entry_round_trip_pct"], 95.0)
        self.assertEqual(ex.state["positions"], [])                              # nothing real opened
        self.assertEqual(ex.state["paper_balance_usd"], balance)
        self.assertEqual(lane.st["signals"][-1]["status"], "shadow_opened")
        # A second first-buy of the same coin is already held; an add is not a first buy.
        lane.shadow_handle_buy(OTHER, "sig2", int(time.time()), swap, 500.0, 1.0, SOL)
        self.assertEqual(lane.st["signals"][-1]["reason"], "already_held")
        lane.shadow_handle_buy(OTHER, "sig3", int(time.time()), dict(swap, mint="M2", pre_tokens=900), 500.0, 1.0, SOL)
        self.assertEqual(lane.st["signals"][-1]["reason"], "add")
        # The source sells everything: the shadow position closes at the executable quote.
        lane.shadow_follow_sell(OTHER, MINT, 1.0, SOL, "sell1")
        self.assertEqual(lane.st["positions"], [])
        trade = lane.st["trades"][-1]
        self.assertEqual(trade["reason"], "copy_sell")
        self.assertAlmostEqual(trade["pnl_base"], pos["size_usd"] * 0.95 - lane.cfg.fee_usd - pos["cost_usd"], places=4)
        self.assertIsNone(trade["pnl_stress"])                                    # no stress quote yet
        self.assertEqual(ex.state["paper_balance_usd"], balance)
        self.assertTrue(os.path.exists(os.path.join(os.environ["DATA_DIR"], "scout_shadow_trades.csv")))

    def test_shadow_exits_follow_the_production_rules(self):
        executor, ex = self.make(COPY_LADDER="2:40,3:30,5:30", COPY_STOP_LOSS="0.30")
        self.seed(ex, OTHER, state="shadow")
        self.fake_market(ex)
        lane = ex.scout
        lane.reset_quote_budget()
        swap = {"side": "buy", "mint": MINT, "tokens": 1000, "sol": 5.0, "stable_usd": 0.0, "pre_tokens": 0}
        lane.shadow_handle_buy(OTHER, "sig1", int(time.time()), swap, 500.0, 1.0, SOL)
        pos = lane.st["positions"][0]
        pos["stress_due_ts"] = 0                                                  # stress quote due now
        lane.reset_quote_budget()
        self.price_ratio = 2.2                                                     # 2.2x: first rung
        lane.manage_shadow_positions(SOL)
        self.assertIsNotNone(pos["tokens_stress"])
        self.assertEqual(pos["tokens"], 600)
        self.assertEqual(len(pos["partials"]), 1)
        lane.reset_quote_budget()
        self.price_ratio = 0.5                                                     # then it collapses: stop loss on the rest
        lane.manage_shadow_positions(SOL)
        self.assertEqual(lane.st["positions"], [])
        trade = lane.st["trades"][-1]
        self.assertEqual(trade["reason"], "trailing_stop")                       # armed by the rung, like production
        self.assertIsNotNone(trade["pnl_stress"])
        self.assertEqual(ex.state["positions"], [])

    def test_quote_budget_and_pending_exits(self):
        executor, ex = self.make(SCOUT_QUOTE_BUDGET="2")
        self.seed(ex, OTHER, state="shadow")
        self.fake_market(ex)
        lane = ex.scout
        lane.reset_quote_budget()
        swap = {"side": "buy", "mint": MINT, "tokens": 1000, "sol": 5.0, "stable_usd": 0.0, "pre_tokens": 0}
        lane.shadow_handle_buy(OTHER, "sig1", int(time.time()), swap, 500.0, 1.0, SOL)
        lane.shadow_handle_buy(OTHER, "sig2", int(time.time()), dict(swap, mint="M2"), 500.0, 1.0, SOL)
        self.assertEqual(lane.st["signals"][-1]["reason"], "missed:quote_budget")
        self.assertEqual(len(lane.st["positions"]), 1)
        lane.shadow_follow_sell(OTHER, MINT, 1.0, SOL, "sell1")                   # budget spent: deferred, not lost
        self.assertEqual(lane.st["positions"][0]["pending_exit"]["reason"], "copy_sell")
        lane.reset_quote_budget()
        lane.manage_shadow_positions(SOL)
        self.assertEqual(lane.st["positions"], [])
        self.assertEqual(lane.st["trades"][-1]["reason"], "copy_sell")

    def test_shadow_poll_uses_separate_state_and_budget(self):
        executor, ex = self.make(SCOUT_DECODE_BUDGET="1")
        self.seed(ex, OTHER, state="shadow")
        self.fake_market(ex)
        now = int(time.time())
        sigs = [{"signature": "new2", "blockTime": now - 2}, {"signature": "new1", "blockTime": now - 5}, {"signature": "old", "blockTime": now - 9999}]
        ex.rpc.call = lambda method, params, timeout=None: sigs if method == "getSignaturesForAddress" else None
        txs = {"new1": tx(10.0, 5.0, 0, 1000, owner=OTHER), "new2": tx(10.0, 5.0, 0, 1000, owner=OTHER, mint="M2")}
        ex.rpc.transaction = lambda sig: txs.get(sig)
        lane = ex.scout
        lane.reset_quote_budget()
        lane.poll_shadow_wallets(SOL)
        self.assertIn("old", lane.st["seen"][OTHER])
        self.assertEqual(len(lane.st["positions"]), 1)                            # budget 1: one decoded
        self.assertEqual(len(lane.st["inbox"][OTHER]), 1)                         # the other waits
        self.assertNotIn(OTHER, ex.state.get("copy_seen", {}))                    # production state untouched
        self.assertNotIn("copy_inbox", ex.state)
        lane.reset_quote_budget()
        lane.poll_shadow_wallets(SOL)
        self.assertEqual(len(lane.st["positions"]), 2)

    # -- resilience --------------------------------------------------------------------------
    def test_restart_preserves_shadow_state(self):
        executor, ex = self.make()
        self.seed(ex, OTHER, state="shadow")
        ex.scout.st["positions"].append({"id": "x", "wallet": OTHER, "mint": MINT, "tokens": 5})
        ex.scout.st["trades"].append({"wallet": OTHER, "mint": MINT, "pnl_base": 1.0, "closed_ts": time.time()})
        executor.save_state(ex.state)
        again = executor.Executor(executor.Config())
        self.assertEqual(again.scout.st["candidates"][OTHER]["state"], "shadow")
        self.assertEqual(len(again.scout.st["positions"]), 1)
        self.assertEqual(len(again.scout.st["trades"]), 37)                         # 36 seeded + 1
        self.assertEqual(again.scout.st["policy_version"], scout.policy_key(again.scout.cfg))

    def test_scout_failure_never_stops_position_management(self):
        executor, ex = self.make()
        managed, polled = [], []
        ex.sol_price_usd = lambda: SOL
        ex.manage_positions = lambda price, panic: managed.append(price)
        ex.poll_copy_wallets = lambda price, allow_buys=True: polled.append(allow_buys)
        ex.state["positions"] = [{"mint": MINT, "tokens": 1, "position_usd": 1.0, "opened_ts": 0, "opened_at": "t", "peak_usd": 1.0,
                                  "buy_signature": "", "copy": WALLET}]
        def boom(sol_price):
            raise RuntimeError("scout exploded")
        ex.scout.tick = boom
        ex.run_cycle()
        self.assertEqual(managed, [SOL])
        self.assertEqual(polled, [True])
        self.assertTrue(executor.STATE_FILE.exists())
        ex.run_cycle()
        self.assertEqual(len(managed), 2)

    def test_scout_off_or_keyless_is_inert(self):
        executor, ex = self.make(GMGN_API_KEY="")
        self.assertFalse(ex.scout.enabled)
        self.assertEqual(ex.scout.heartbeat(), "no-key")
        self.assertIn("off", ex.scout.startup_line())
        ex.scout.tick(SOL)                                                        # nothing polled, nothing raised
        self.assertEqual(ex.scout.st["candidates"], {})
        executor, ex = self.make(SCOUT_MODE="off")
        self.assertEqual(ex.scout.heartbeat(), "off")
        self.assertEqual(ex.followed_wallets(), [WALLET])

    def test_heartbeat_and_startup_line(self):
        executor, ex = self.make(SCOUT_LIVE_LOSS_BUDGET_USD="0")
        self.seed(ex, OTHER, state="shadow")
        self.assertIn("shadow:cand=1,shadow=1", ex.scout.heartbeat())
        line = ex.scout.startup_line()
        self.assertIn("live_promotion=off", line)
        self.assertIn("track=full", line)
        self.assertIn("promotion refused until set", line)
        self.assertIn(scout.POLICY_VERSION, line)

    # -- fast track --------------------------------------------------------------------------
    def test_fast_track_qualifies_on_history_alone_but_never_promotes_with_the_switch_off(self):
        executor, ex = self.make(SCOUT_FAST_TRACK="1")
        cand = self.seed(ex, OTHER, state="shadow")
        cand["rank_snapshots"] = []                                               # no leaderboard evidence
        cand["holdings"] = None                                                   # holdings need signed auth
        cand["history"]["coverage_days"] = 16.0
        ex.scout.st["trades"] = []                                                # and no shadow sample at all
        ex.scout.evaluate_all()
        ev = cand["evaluation"]
        self.assertEqual(ev["track"], "fast")
        self.assertIn("leaderboard", ev["waived"])
        self.assertIn("shadow_sample", ev["waived"])
        self.assertEqual(ev["failed"], [])
        self.assertEqual(ev["missing"], [])
        self.assertTrue(ev["qualified"])
        self.assertEqual(cand["state"], "qualified")
        self.assertEqual(ex.scout.live_wallets(), {})                             # SCOUT_LIVE still off
        self.assertIn("SCOUT_LIVE=0", ex.scout.st["last_selection"]["reason"])
        # The history gates still decide: a jackpot wallet or a thin one does not pass.
        cand["history"]["coverage_days"] = 9.0
        ex.scout.evaluate_all()
        self.assertIn("history_days_fast", cand["evaluation"]["failed"])
        self.assertEqual(cand["state"], "shadow")
        cand["history"]["coverage_days"] = 16.0
        cand["stats"]["7d"]["realized_profit"] = -1.0
        ex.scout.evaluate_all()
        self.assertIn("pnl_7d", cand["evaluation"]["failed"])

    def test_fast_track_promotes_with_switch_and_budget_and_tripwire_pauses(self):
        executor, ex = self.make(SCOUT_FAST_TRACK="1", SCOUT_LIVE="1")            # budget defaults to 25
        cand = self.seed(ex, OTHER, state="shadow")
        cand["rank_snapshots"], cand["holdings"] = [], None
        ex.scout.st["trades"] = []
        ex.scout.evaluate_all()                                                   # qualified
        ex.scout.evaluate_all()                                                   # promoted
        self.assertEqual(cand["state"], "live")
        self.assertEqual(ex.scout.live_wallets(), {OTHER: {"size": 0.5, "min_usd": 300.0}})
        self.assertIn("budget 25 USD", cand["lifecycle"][-1]["reason"])
        # Shadow keeps scoring the live wallet: ten losing paper trades trip it.
        for t in good_shadow_trades(OTHER, time.time(), n=10):
            t["pnl_base"] = -0.5
            ex.scout.st["trades"].append(t)
        ex.scout.evaluate_all()
        self.assertEqual(cand["state"], "paused")
        self.assertIn("tripwire", cand["lifecycle"][-1]["reason"])
        self.assertEqual(ex.scout.live_wallets(), {})

    def test_fast_track_only_asks_that_the_wallet_traded_this_week(self):
        cfg = cfg_with(GMGN_API_KEY="k", SCOUT_FAST_TRACK="1")
        cand = good_candidate(OTHER)
        cand["history"]["metrics_30d"] = dict(cand["history"]["metrics_30d"], active_days=2)   # two busy days
        ev = scout.evaluate_candidate(cand, cfg, NOW, 300.0)
        self.assertEqual(ev["gates"]["active_days_30d"]["status"], "pass")
        self.assertIn("last trade 0.0 days ago", ev["gates"]["active_days_30d"]["detail"])
        for row in cand["stats"].values():
            row["last_timestamp"] = int(NOW - 9 * DAY) * 1000                      # milliseconds, gone quiet
        ev = scout.evaluate_candidate(cand, cfg, NOW, 300.0)
        self.assertEqual(ev["gates"]["active_days_30d"]["status"], "fail")
        self.assertIn("9.0 days ago", ev["gates"]["active_days_30d"]["detail"])
        cand["history"]["episodes"] = [{"token": "t", "opened_ts": NOW - 3 * DAY, "closed_ts": NOW - 2 * DAY}]
        self.assertEqual(scout.evaluate_candidate(cand, cfg, NOW, 300.0)["gates"]["active_days_30d"]["status"], "pass")
        # The full policy still counts active days.
        full = cfg_with(GMGN_API_KEY="k", SCOUT_FAST_TRACK="0")
        self.assertEqual(scout.evaluate_candidate(cand, full, NOW, 300.0)["gates"]["active_days_30d"]["status"], "fail")

    def test_dense_history_counts_when_the_page_cap_cut_the_window_short(self):
        """A wallet trading hundreds of times a day can never show 15 calendar days inside the
        activity page cap. Its episodes are the evidence, so they qualify it instead."""
        cfg = cfg_with(GMGN_API_KEY="k", SCOUT_FAST_TRACK="1")
        cand = good_candidate(OTHER)
        cand["rank_snapshots"], cand["holdings"] = [], None
        cand["history"].update({"coverage_days": 6.0, "truncated": True})          # 800 events, 6 days
        ev = scout.evaluate_candidate(cand, cfg, NOW, 300.0)
        self.assertEqual(ev["gates"]["history_days_fast"]["status"], "pass")       # 40 closed episodes
        self.assertIn("dense evidence", ev["gates"]["history_days_fast"]["detail"])
        self.assertTrue(ev["qualified"])
        # Thin and truncated is still missing evidence, not a pass.
        cand["history"]["metrics_30d"] = scout.history_metrics(good_episodes(tokens=12, winners=9), NOW, 30, 300.0, set(), 20000.0)
        ev = scout.evaluate_candidate(cand, cfg, NOW, 300.0)
        self.assertEqual(ev["gates"]["history_days_fast"]["status"], "missing")
        self.assertFalse(ev["qualified"])
        # A short history that was NOT truncated is a fail: the wallet is simply too new.
        cand["history"]["metrics_30d"] = scout.history_metrics(good_episodes(), NOW, 30, 300.0, set(), 20000.0)
        cand["history"]["truncated"] = False
        ev = scout.evaluate_candidate(cand, cfg, NOW, 300.0)
        self.assertEqual(ev["gates"]["history_days_fast"]["status"], "fail")

    def test_full_policy_still_needs_every_gate(self):
        cand = good_candidate(OTHER)
        cand["rank_snapshots"] = []
        ev = scout.evaluate_candidate(cand, cfg_with(SCOUT_FAST_TRACK="0"), NOW, 300.0)
        self.assertEqual(ev["track"], "full")
        self.assertEqual(ev["missing"], ["leaderboard"])
        self.assertFalse(ev["qualified"])

    def test_policy_change_re_evaluates_known_candidates_at_startup(self):
        executor, ex = self.make(SCOUT_FAST_TRACK="1")
        lane = ex.scout
        cand = self.seed(ex, OTHER, state="shadow")
        cand["rank_snapshots"], cand["holdings"] = [], None
        cand["evaluation"] = {"qualified": False, "policy_version": "old", "gates": {}, "failed": [], "missing": ["leaderboard"], "evaluated_at": 0}
        lane.st["policy_version"] = "2000-01-01.0"                                # state written by an older deploy
        lane.maybe_discover = lambda force=False: None                            # no network
        lane.tick(SOL)
        self.assertEqual(lane.st["policy_version"], scout.policy_key(lane.cfg))
        self.assertEqual(cand["evaluation"]["policy_version"], scout.policy_key(lane.cfg))
        self.assertEqual(cand["state"], "qualified")                              # scored under the fast track right away
        lane.st["candidates"][OTHER]["state"] = "shadow"
        lane.tick(SOL)                                                            # same policy: not re-scored every tick
        self.assertEqual(cand["state"], "shadow")

    def test_changing_a_threshold_re_scores_without_touching_POLICY_VERSION(self):
        """A deploy that only moves a gate threshold must not leave stale verdicts behind:
        the stored policy key fingerprints the thresholds, not just the version string."""
        base = cfg_with(GMGN_API_KEY="k")
        looser = cfg_with(GMGN_API_KEY="k", SCOUT_FAST_TRACK_DENSE_EPISODES="99")
        self.assertNotEqual(scout.policy_key(base), scout.policy_key(looser))
        self.assertTrue(scout.policy_key(base).startswith(scout.POLICY_VERSION))
        executor, ex = self.make(SCOUT_FAST_TRACK="1")
        lane = ex.scout
        cand = self.seed(ex, OTHER, state="shadow")
        cand["rank_snapshots"], cand["holdings"] = [], None
        lane.st["policy_version"] = scout.POLICY_VERSION + "+deadbeef"            # same version, other thresholds
        lane.maybe_discover = lambda force=False: None
        lane.tick(SOL)
        self.assertEqual(lane.st["policy_version"], scout.policy_key(lane.cfg))
        self.assertEqual(cand["evaluation"]["policy_version"], scout.policy_key(lane.cfg))
        self.assertEqual(cand["state"], "qualified")

    def test_rate_limited_cycle_pauses_discovery_for_the_cooldown(self):
        executor, ex = self.make()
        lane = ex.scout
        lane.st["candidates"][OTHER] = {"address": OTHER, "state": "discovered", "sources": [], "rank_snapshots": [],
                                        "lifecycle": [], "last_refresh": time.time()}
        lane._result = ("ok", {"candidates": {}, "token_sample": [], "units": 9, "errors": ["x: rate limit"], "rate_limited": True})
        lane.apply_discovery()
        self.assertGreater(lane.st["rate_limited_until"], time.time())
        with mock.patch("threading.Thread") as thread:
            lane.st["attempt_ts"] = 0
            lane.maybe_discover()
            self.assertEqual(thread.call_count, 0)                                # still cooling: no new cycle
            lane.st["rate_limited_until"] = time.time() - 1                        # cooldown elapsed
            lane.st["discovered_ts"] = time.time() - 3 * 3600                      # and a cycle is due again
            lane.maybe_discover()
            self.assertEqual(thread.call_count, 1)
        lane._result = ("ok", {"candidates": {}, "token_sample": [], "units": 9, "errors": [], "rate_limited": False})
        lane.apply_discovery()
        self.assertNotIn("rate_limited_until", lane.st)                            # a clean cycle clears it

    def test_a_new_find_takes_the_slot_of_the_weakest_enriched_wallet_when_full(self):
        executor, ex = self.make(SCOUT_MAX_CANDIDATES="3", SCOUT_MAX_SHADOW="1")
        lane = ex.scout
        now = time.time()
        def cand(address, state, score, refreshed=True):
            c = {"address": address, "state": state, "sources": [], "rank_snapshots": [], "lifecycle": [], "discovered_at": now,
                 "evaluation": {"failed": ["pnl_30d"], "score": {"total": score}}}
            if refreshed:
                c["last_refresh"] = now
            return c
        lane.st["candidates"] = {"Strong": cand("Strong", "shadow", 80), "Weak": cand("Weak", "shadow", 10),
                                 "Waiting": cand("Waiting", "discovered", 0, refreshed=False)}
        self.assertEqual(lane.evictable(), ["Weak"])        # Strong is polled; Waiting never had its turn
        lane._result = ("ok", {"candidates": {"Newcomer": {"sources": [{"source": "smartmoney", "ts": now}], "rank_snapshots": []}},
                               "token_sample": [], "units": 1, "errors": []})
        lane.apply_discovery()
        self.assertEqual(sorted(lane.st["candidates"]), ["Newcomer", "Strong", "Waiting"])

    def test_a_pause_stored_under_the_old_longer_setting_is_cut_back(self):
        executor, ex = self.make()
        lane = ex.scout
        lane.st["rate_limited_until"] = time.time() + 3 * 3600                    # set by the old 3h default
        with mock.patch("threading.Thread") as thread:
            lane.maybe_discover()
            self.assertEqual(thread.call_count, 0)
        self.assertLessEqual(lane.st["rate_limited_until"], time.time() + 15 * 60 + 1)

    def test_discovery_reruns_at_retry_interval_until_something_is_enriched(self):
        executor, ex = self.make()
        lane = ex.scout
        started = []
        lane.discover_and_enrich_async = None
        def fake_start(force=False):
            started.append(1)
        lane.st["discovered_ts"] = time.time()                                    # a cycle just ran ...
        lane.st["attempt_ts"] = time.time() - 3600
        lane.st["candidates"][OTHER] = {"address": OTHER, "state": "discovered", "sources": [], "rank_snapshots": [], "lifecycle": []}
        with mock.patch("threading.Thread") as thread:
            lane.maybe_discover()                                                 # ... but enriched nothing: due again
            self.assertEqual(thread.call_count, 1)
            lane.st["attempt_ts"] = time.time() - 3600
            lane.st["candidates"][OTHER]["last_refresh"] = time.time()
            lane.maybe_discover()                                                 # something enriched: wait the full period
            self.assertEqual(thread.call_count, 1)

    def test_discovery_result_is_applied_on_the_main_thread(self):
        executor, ex = self.make()
        lane = ex.scout
        payload = {"candidates": {OTHER: {"sources": [{"source": "kol", "ts": time.time()}], "rank_snapshots": [], "tags": ["kol"],
                                          "stats": {"7d": {"realized_profit": 1.0}}, "last_refresh": time.time()}},
                   "token_sample": [MINT], "units": 12, "errors": ["enrich abc: boom"]}
        lane._result = ("ok", payload)
        lane.apply_discovery()
        cand = lane.st["candidates"][OTHER]
        self.assertEqual(cand["state"], "shadow")                                 # enriched -> research -> shadow
        self.assertEqual(cand["source_kinds"], ["kol"])
        self.assertEqual(lane.st["errors"][-1]["why"], "enrich abc: boom")
        self.assertFalse(cand["evaluation"]["qualified"])
        lane._result = ("error", "GMGN down")
        lane.apply_discovery()
        self.assertEqual(lane.st["errors"][-1]["what"], "discovery")
        report = lane.report()
        self.assertEqual(report["candidates"][0]["address"], OTHER)
        json.dumps(report, allow_nan=False)


class DiscoveryTests(unittest.TestCase):
    def test_discover_and_enrich_with_a_fake_client(self):
        class Client:
            pause_seconds = 0.0
            calls = []
            def smart_money(self, chain, limit):
                return [{"maker": OTHER, "maker_info": {"tags": ["smart_degen"]}}]
            def kol(self, chain, limit):
                return [{"maker": THIRD, "maker_info": {"tags": ["kol"]}}, {"maker": WALLET, "maker_info": {"tags": ["kol"]}}]
            def market_rank(self, chain, limit):
                return [{"address": "Trend1"}]
            def top_traders(self, chain, token, tag=None, limit=20):
                return [{"address": OTHER, "profit": 5, "transfer_in": False}, {"address": "Susp", "profit": 9, "is_suspicious": True}]
            def wallet_stats(self, chain, wallets, period):
                return [{"realized_profit": 100.0, "realized_profit_pnl": 0.1, "buy": 10, "sell": 8, "bought_cost": 1000.0, "sold_income": 1100.0,
                         "last_timestamp": int(NOW), "pnl_stat": {"token_num": 4, "winrate": 0.5, "avg_holding_period": 3600},
                         "common": {"tags": ["smart_degen"], "tag_rank": {"smart_degen": 0}, "twitter_username": "", "followers_count": 0,
                                    "fund_from_address": "Funder", "created_token_count": 0}}]
            def wallet_holdings(self, chain, wallet, limit=100):
                return [{"token": {"token_address": "tok0"}, "history_transfer_in_amount": 500, "history_bought_amount": 1000,
                         "history_transfer_in_cost": 50.0, "unrealized_profit": -20.0}]
            def wallet_activity(self, chain, wallet, limit=20, cursor=None):
                self.calls.append(cursor)
                if cursor is None:
                    return [{"event_type": "buy", "timestamp": int(NOW) - 3600, "token": {"address": "tok1"}, "token_amount": 10, "cost_usd": 400, "gas_usd": 0.1}], "c1"
                return [{"event_type": "sell", "timestamp": int(NOW) - 100 * 86400, "token": {"address": "tok1"}, "token_amount": 10, "cost_usd": 450, "gas_usd": 0.1}], None
        cfg = cfg_with(GMGN_API_KEY="k", SCOUT_ENRICH_PER_CYCLE="2")
        result = scout.discover_and_enrich(Client(), cfg, {}, [WALLET], [MINT], NOW, 300.0)
        cands = result["candidates"]
        self.assertEqual([s["source"] for s in cands[WALLET]["sources"]], ["configured"])   # evaluated by the same policy, never "discovered"
        self.assertIn("stats", cands[WALLET])
        self.assertNotIn("Susp", cands)
        self.assertIn(OTHER, cands)
        self.assertEqual(sorted(s["source"] for s in cands[OTHER]["sources"]), ["smartmoney", f"top_traders:{MINT[:8]}", "top_traders:Trend1"])
        self.assertEqual(cands[OTHER]["rank_snapshots"][0]["scope"], "token")
        self.assertIn("stats", cands[OTHER])
        self.assertEqual(cands[OTHER]["holdings"]["transfer_in_tokens"], 1)
        self.assertEqual(cands[OTHER]["history"]["events"], 2)
        self.assertGreaterEqual(cands[OTHER]["history"]["coverage_days"], 99)
        self.assertEqual(cands[OTHER]["rank_snapshots"][-1]["scope"], "token")    # tag_rank 0: no global snapshot
        self.assertTrue(any("transferred-in" in f["flag"] for f in cands[OTHER]["risk_flags"]))
        self.assertIsNone(cands[THIRD].get("last_refresh"))                        # two enrichment slots: configured + first new
        self.assertEqual(result["token_sample"], [MINT, "Trend1"])
        self.assertGreater(result["units"], 0)
        self.assertEqual(result["errors"], [])

    def test_a_rate_limit_stops_the_cycle_instead_of_extending_the_ban(self):
        calls = []
        class Client:
            pause_seconds = 0.0
            def smart_money(self, chain, limit):
                calls.append("smart_money")
                raise RuntimeError("GET /v1/user/smartmoney: IP is temporarily banned due to repeated rate limit violations")
            def kol(self, chain, limit): calls.append("kol"); return []
            def market_rank(self, chain, limit): calls.append("market_rank"); return []
            def top_traders(self, chain, token, tag=None, limit=20): calls.append("top_traders"); return []
            def wallet_stats(self, chain, wallets, period): calls.append("wallet_stats"); return []
        out = scout.discover_and_enrich(Client(), cfg_with(GMGN_API_KEY="k"), {}, [], [], NOW, 300.0)
        self.assertTrue(out["rate_limited"])
        self.assertEqual(calls, ["smart_money"])                                  # nothing after the ban
        self.assertTrue(scout.rate_limited(RuntimeError("429 Too Many Requests")))
        self.assertFalse(scout.rate_limited(RuntimeError("missing signature")))

    def test_the_backlog_is_enriched_even_when_the_feeds_do_not_resurface_it(self):
        """A wallet discovered once and never enriched must not wait for a feed to mention it
        again: the queue is drawn from everything tracked, not only from this cycle's finds."""
        enriched = []
        class Client:
            pause_seconds = 0.0
            def smart_money(self, chain, limit): return [{"maker": "FeedWalletAAAA"}]
            def kol(self, chain, limit): return []
            def market_rank(self, chain, limit): return []
            def top_traders(self, chain, token, tag=None, limit=20): return []
            def wallet_stats(self, chain, wallets, period):
                enriched.append(wallets[0])
                return [{"realized_profit": 1.0, "pnl_stat": {}, "common": {"tags": []}}]
            def wallet_holdings(self, chain, wallet, limit=100): return []
            def wallet_activity(self, chain, wallet, limit=20, cursor=None): return [], None
        cfg = cfg_with(GMGN_API_KEY="k", SCOUT_ENRICH_PER_CYCLE="4")
        known = {
            "BacklogOld": {"address": "BacklogOld", "state": "discovered", "discovered_at": NOW - 900},
            "BacklogNew": {"address": "BacklogNew", "state": "discovered", "discovered_at": NOW - 100},
            "RejectedOne": {"address": "RejectedOne", "state": "rejected", "discovered_at": NOW - 900},
            "FreshlyDone": {"address": "FreshlyDone", "state": "shadow", "last_refresh": NOW - 60},
        }
        scout.discover_and_enrich(Client(), cfg, known, [], [], NOW, 300.0)
        self.assertIn("FeedWalletAAAA", enriched)                                 # found this cycle
        self.assertIn("BacklogOld", enriched)                                     # never enriched, oldest first
        self.assertIn("BacklogNew", enriched)
        self.assertLess(enriched.index("BacklogOld"), enriched.index("BacklogNew"))
        self.assertNotIn("RejectedOne", enriched)                                 # rejected wallets cost no units
        self.assertNotIn("FreshlyDone", enriched)                                 # refreshed a minute ago

    def test_a_full_tracker_spends_the_budget_on_the_backlog_not_on_finds_it_would_drop(self):
        enriched = []
        class Client:
            pause_seconds = 0.0
            def smart_money(self, chain, limit): return [{"maker": "NewFindAAAA"}, {"maker": "NewFindBBBB"}]
            def kol(self, chain, limit): return []
            def market_rank(self, chain, limit): return []
            def top_traders(self, chain, token, tag=None, limit=20): return []
            def wallet_stats(self, chain, wallets, period):
                enriched.append(wallets[0])
                return [{"realized_profit": 1.0, "pnl_stat": {}, "common": {"tags": []}}]
            def wallet_holdings(self, chain, wallet, limit=100): return []
            def wallet_activity(self, chain, wallet, limit=20, cursor=None): return [], None
        cfg = cfg_with(GMGN_API_KEY="k", SCOUT_ENRICH_PER_CYCLE="2")
        known = {"Backlog1": {"address": "Backlog1", "state": "discovered", "discovered_at": NOW - 900},
                 "Backlog2": {"address": "Backlog2", "state": "discovered", "discovered_at": NOW - 800}}
        scout.discover_and_enrich(Client(), cfg, known, [], [], NOW, 300.0, room=0)
        self.assertEqual(sorted(set(enriched)), ["Backlog1", "Backlog2"])        # backlog first, finds wait
        enriched.clear()
        cfg = cfg_with(GMGN_API_KEY="k", SCOUT_ENRICH_PER_CYCLE="4")
        scout.discover_and_enrich(Client(), cfg, known, [], [], NOW, 300.0, room=1)
        self.assertEqual(sorted(set(enriched)), ["Backlog1", "Backlog2", "NewFindAAAA"])   # one slot: one find

    def test_holdings_needing_signed_auth_do_not_stop_enrichment(self):
        class Client:
            pause_seconds = 0.0
            holdings_calls = 0
            def wallet_stats(self, chain, wallets, period):
                return [{"realized_profit": 100.0, "buy": 10, "sell": 8, "bought_cost": 1000.0, "pnl_stat": {}, "common": {"tags": []}}]
            def wallet_holdings(self, chain, wallet, limit=100):
                Client.holdings_calls += 1
                raise RuntimeError("GET /v1/user/wallet_holdings: missing signature")
            def wallet_activity(self, chain, wallet, limit=20, cursor=None):
                return [{"event_type": "buy", "timestamp": int(NOW) - 100 * 86400, "token": {"address": "t"}, "token_amount": 1, "cost_usd": 400}], None
        cfg = cfg_with(GMGN_API_KEY="k")
        client = Client()
        out, units = scout.enrich_wallet(client, cfg, OTHER, NOW, 300.0, 600)
        self.assertIsNone(out["holdings"])
        self.assertIn("missing signature", out["holdings_error"])
        self.assertEqual(out["history"]["events"], 1)                            # enrichment carried on
        self.assertTrue(any("holdings unavailable" in f["flag"] for f in out["risk_flags"]))
        out2, _ = scout.enrich_wallet(client, cfg, THIRD, NOW, 300.0, 600)
        self.assertEqual(Client.holdings_calls, 1)                                # not retried for every wallet this cycle
        self.assertIn("signed auth", out2["holdings_error"])
        cand = good_candidate(OTHER, holdings=None)
        ev = scout.evaluate_candidate(cand, cfg, NOW, 300.0)
        self.assertEqual(ev["gates"]["open_inventory"]["status"], "missing")
        self.assertFalse(ev["qualified"])


class GmgnClientTests(unittest.TestCase):
    def test_new_endpoints(self):
        import gmgn
        from test_gmgn import FakeResponse
        client = gmgn.Gmgn(api_key="k")
        calls = []
        def fake(method, url, params=None, json=None, headers=None, timeout=None):
            calls.append((url, dict(params)))
            if url.endswith("/market/rank"):
                return FakeResponse({"code": 0, "data": {"code": 0, "data": {"rank": [{"address": "T1"}, "junk"]}}})
            if url.endswith("/wallet_activity"):
                return FakeResponse({"code": 0, "data": {"activities": [{"event_type": "buy"}], "next": "abc"}})
            if url.endswith("/wallet_holdings"):
                return FakeResponse({"code": 0, "data": {"list": [{"token": {"token_address": "T1"}}], "next": "z"}})   # real shape (probed)
            return FakeResponse({"code": 0, "data": [{"maker": "W"}]})
        with mock.patch.object(client.session, "request", side_effect=fake):
            self.assertEqual(client.market_rank("sol", 5), [{"address": "T1"}])
            rows, nxt = client.wallet_activity("sol", "W", 20, None)
            self.assertEqual((rows, nxt), ([{"event_type": "buy"}], "abc"))
            self.assertEqual(calls[-1][1]["wallet_address"], "W")
            self.assertNotIn("cursor", calls[-1][1])
            client.wallet_activity("sol", "W", 20, "abc")
            self.assertEqual(calls[-1][1]["cursor"], "abc")
            self.assertEqual(client.wallet_holdings("sol", "W"), [{"token": {"token_address": "T1"}}])
            self.assertEqual(calls[-1][1]["wallet_address"], "W")
            self.assertEqual(client.kol("sol", 10), [{"maker": "W"}])
            self.assertTrue(calls[-1][0].endswith("/v1/user/kol"))


class ServerTests(unittest.TestCase):
    def test_scout_endpoints_read_only(self):
        from fastapi.testclient import TestClient
        import server
        client = TestClient(server.app)
        state = {"scout": {"candidates": {OTHER: good_candidate(OTHER)}, "trades": [{"wallet": OTHER, "pnl_base": 1.0}],
                           "signals": [{"wallet": OTHER, "status": "shadow_opened"}], "positions": [], "live": {},
                           "last_selection": {"reason": "no wallet passes every mandatory gate"}}}
        state["scout"]["candidates"][OTHER]["evaluation"] = scout.evaluate_candidate(state["scout"]["candidates"][OTHER], scout.ScoutConfig(), NOW, 300.0)
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(server, "EXECUTOR_STATE", __import__("pathlib").Path(tmp) / "s.json"):
            self.assertEqual(client.get("/api/scout").json()["candidates"], [])
            server.EXECUTOR_STATE.write_text(json.dumps(state))
            body = client.get("/api/scout").json()
            self.assertEqual(body["candidates"][0]["address"], OTHER)
            self.assertEqual(body["last_selection"]["reason"], "no wallet passes every mandatory gate")
            detail = client.get(f"/api/scout/{OTHER}").json()
            self.assertEqual(len(detail["shadow_trades"]), 1)
            self.assertEqual(detail["candidate"]["state"], "shadow")
            self.assertEqual(client.get("/api/scout/nobody").status_code, 404)
        with mock.patch.dict(os.environ, {"SCOUT_LIVE": "1"}):
            self.assertEqual(client.get("/healthz").json()["settings"]["SCOUT_LIVE"], "1")


if __name__ == "__main__":
    unittest.main()


class ConvergenceTests(unittest.TestCase):
    """Several independent tracked wallets entering one coin inside the window is one shadow
    signal of its own, judged beside single-wallet copying and never traded live."""
    make, seed, fake_market = LaneTests.make, LaneTests.seed, LaneTests.fake_market

    def buy(self, lane, wallet, sig, mint=MINT, ts=None, usd=500.0):
        swap = {"side": "buy", "mint": mint, "tokens": 1000, "sol": 5.0, "stable_usd": 0.0, "pre_tokens": 0}
        lane.shadow_handle_buy(wallet, sig, int(ts or time.time()), swap, usd, 1.0, SOL)

    def test_two_independent_wallets_in_the_window_open_one_convergence_position(self):
        executor, ex = self.make(SCOUT_QUOTE_BUDGET="20")
        self.seed(ex, OTHER, state="shadow")
        self.seed(ex, THIRD, state="shadow")
        self.fake_market(ex)
        lane = ex.scout
        lane.reset_quote_budget()
        self.buy(lane, OTHER, "s1")
        self.assertEqual(lane.st["convergence_events"], [])                      # one wallet is not a convergence
        self.buy(lane, THIRD, "s2")
        events = lane.st["convergence_events"]
        self.assertEqual(len(events), 1)
        self.assertEqual(sorted(events[0]["wallets"]), sorted([OTHER, THIRD]))
        self.assertEqual(events[0]["independent"], 2)
        self.assertTrue(events[0]["shadow_opened"])
        conv = [p for p in lane.st["positions"] if p.get("kind") == "convergence"]
        self.assertEqual(len(conv), 1)
        self.assertEqual(conv[0]["wallet"], scout.CONVERGENCE)
        self.assertEqual(conv[0]["source_usd"], 1000)                           # both source buys combined
        self.assertEqual(len(lane.st["positions"]), 3)                         # two single-wallet shadows + the group
        self.assertEqual(ex.state["positions"], [])                             # nothing real
        self.assertNotIn(scout.CONVERGENCE, lane.shadow_wallets())              # never polled as a wallet
        # A third buyer inside the cooldown does not open a second one.
        self.buy(lane, WALLET, "s3")
        self.assertEqual(len(lane.st["convergence_events"]), 1)
        self.assertTrue(os.path.exists(os.path.join(os.environ["DATA_DIR"], "scout_convergence.csv")))

    def test_related_wallets_count_as_one_participant(self):
        executor, ex = self.make(SCOUT_QUOTE_BUDGET="20")
        a, b = self.seed(ex, OTHER, state="shadow"), self.seed(ex, THIRD, state="shadow")
        a["relationship"] = {"cluster": "grp1", "confidence": "medium", "evidence": ["synchronised buys"]}
        b["relationship"] = {"cluster": "grp1", "confidence": "medium", "evidence": ["synchronised buys"]}
        self.fake_market(ex)
        lane = ex.scout
        lane.reset_quote_budget()
        self.buy(lane, OTHER, "s1")
        self.buy(lane, THIRD, "s2")
        self.assertEqual(lane.st["convergence_events"], [])
        self.assertFalse(any(p.get("kind") == "convergence" for p in lane.st["positions"]))

    def test_buys_outside_the_window_do_not_converge(self):
        executor, ex = self.make(SCOUT_QUOTE_BUDGET="20", SCOUT_CONVERGENCE_WINDOW_MINUTES="5")
        self.seed(ex, OTHER, state="shadow")
        self.seed(ex, THIRD, state="shadow")
        self.fake_market(ex)
        lane = ex.scout
        lane.reset_quote_budget()
        now = time.time()
        self.buy(lane, OTHER, "s1", ts=now - 20 * 60)
        self.buy(lane, THIRD, "s2", ts=now - 10)
        self.assertEqual(lane.st["convergence_events"], [])

    def test_first_member_full_exit_closes_the_group_and_reports_separately(self):
        executor, ex = self.make(SCOUT_QUOTE_BUDGET="20")
        self.seed(ex, OTHER, state="shadow")
        self.seed(ex, THIRD, state="shadow")
        self.fake_market(ex)
        lane = ex.scout
        lane.reset_quote_budget()
        self.buy(lane, OTHER, "s1")
        self.buy(lane, THIRD, "s2")
        lane.shadow_follow_sell(THIRD, MINT, 0.3, SOL, "sell0")                 # a trim does not end it
        self.assertTrue(any(p.get("kind") == "convergence" for p in lane.st["positions"]))
        lane.shadow_follow_sell(THIRD, MINT, 1.0, SOL, "sell1")
        conv_trades = [t for t in lane.st["trades"] if t.get("kind") == "convergence"]
        self.assertEqual(len(conv_trades), 1)
        self.assertEqual(conv_trades[0]["reason"], "convergence_member_sell")
        self.assertEqual(conv_trades[0]["exit_wallet"], THIRD)
        self.assertFalse(any(p.get("kind") == "convergence" for p in lane.st["positions"]))
        self.assertTrue(os.path.exists(os.path.join(os.environ["DATA_DIR"], "scout_convergence_trades.csv")))
        # The group's trades never count toward a wallet's own shadow record, and the report
        # shows them beside single-wallet copying.
        lane.evaluate_all()
        self.assertNotIn(scout.CONVERGENCE, lane.st["candidates"])
        report = lane.report()["convergence"]
        self.assertEqual(report["events"], 1)
        self.assertEqual(report["shadow"]["trades"], 1)
        self.assertGreaterEqual(report["single_wallet_shadow"]["trades"], 1)
        self.assertIn("conv=1/0/1", lane.heartbeat())

    def test_convergence_can_be_switched_off(self):
        executor, ex = self.make(SCOUT_QUOTE_BUDGET="20", SCOUT_CONVERGENCE="0")
        self.seed(ex, OTHER, state="shadow")
        self.seed(ex, THIRD, state="shadow")
        self.fake_market(ex)
        lane = ex.scout
        lane.reset_quote_budget()
        self.buy(lane, OTHER, "s1")
        self.buy(lane, THIRD, "s2")
        self.assertEqual(lane.st["convergence_events"], [])


class ExpectancyTests(unittest.TestCase):
    def test_live_fills_are_attributed_to_their_source_wallets(self):
        import copy_expectancy
        d = tempfile.mkdtemp(prefix="grad-expectancy-test-")
        with open(os.path.join(d, "live_trades.csv"), "w") as fh:
            fh.write("opened_at,closed_at,mint,position_usd,exit_usd,net_return,exit_reason,buy_signature\n"
                     "t,t,m1,16.0,10.8,-0.325,stop_loss,buyA\n"
                     "t,t,m2,16.0,20.0,0.25,ladder_1.4x,buyB\n"
                     "t,t,m3,16.0,13.0,-0.1875,copy_sell,unknown\n")
        with open(os.path.join(d, "copy_signals.csv"), "w") as fh:
            fh.write("timestamp,wallet,status,source_usd,fill_signature\n"
                     f"t,{WALLET},filled,5300,buyA\n"
                     f"t,{OTHER},filled,4900,buyB\n"
                     f"t,{WALLET},blocked,200,\n")
        with open(os.path.join(d, "scout_convergence_trades.csv"), "w") as fh:
            fh.write("opened_at,closed_at,mint,cost_usd,pnl_base,pnl_stress\n"
                     "t,t,m9,16.0,4.0,3.5\n")
        report = copy_expectancy.expectancy(__import__("pathlib").Path(d))
        by_wallet = {r["wallet"]: r for r in report["live"]["by_wallet"]}
        self.assertAlmostEqual(by_wallet[WALLET]["net_pnl_usd"], -5.2)
        self.assertEqual(by_wallet[WALLET]["signals_blocked"], 1)
        self.assertEqual(by_wallet[OTHER]["win_rate"], 1.0)
        self.assertEqual(report["live"]["unattributed"]["trades"], 1)
        self.assertEqual(report["live"]["overall"]["trades"], 3)
        self.assertEqual(report["live"]["by_exit_reason"]["stop_loss"]["trades"], 1)
        self.assertEqual(report["convergence"]["trades"], 1)
        self.assertEqual(report["convergence"]["net_pnl_stress_usd"], 3.5)
        self.assertEqual(report["shadow"]["overall"], {"trades": 0})
