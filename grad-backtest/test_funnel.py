"""The copy funnel (watchlist -> live set -> one position per coin) and the Jev gate: the
acceptance list of the funnel spec, plus the Jev client against the TypeSafe response shape."""
import csv
import json
import os
import time
import unittest
from unittest import mock

import test_scout  # noqa: F401  (sets the scout test environment before scout is imported)
import jev
import scout
from test_copy import MINT, OTHER, SOL, WALLET, tx
from test_scout import DAY, THIRD, good_shadow_trades

FOURTH = "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1"


def answers(choice="buy", p=0.8, dump=0.2):
    probs = {"buy": 0.1, "watch": 0.1, "skip": 0.1}
    probs[choice] = p
    return {"model": "jev-1.13", "usage": {"input_tokens": 90, "output_tokens": 3},
            "answers": {"action": {"type": "choice", "choice": choice, "confidence": p, "probabilities": probs},
                        "dump_risk": {"type": "noul", "noul": dump}}}


class FunnelTests(unittest.TestCase):
    make, seed, fake_market = test_scout.LaneTests.make, test_scout.LaneTests.seed, test_scout.LaneTests.fake_market
    buy = test_scout.ConvergenceTests.buy

    def setup(self, **env):
        env.setdefault("SCOUT_QUOTE_BUDGET", "50")
        env.setdefault("PAPER_BALANCE_USD", "200")
        executor, ex = self.make(**env)
        ex.state["paper_balance_usd"] = 200.0
        self.fake_market(ex)
        ex.scout.reset_quote_budget()
        return executor, ex

    # -- watchlist ---------------------------------------------------------------------------
    def test_watchlist_holds_hundreds_polls_a_slice_and_never_buys_on_one_wallet(self):
        executor, ex = self.setup(SCOUT_WATCH_POLL_WALLETS="5")
        lane = ex.scout
        for i in range(300):
            lane.st["candidates"][f"W{i:03d}"] = {"address": f"W{i:03d}", "state": "shadow", "evaluation": {"score": {"total": i}}}
        self.assertEqual(len(lane.watchlist()), 300)
        polled = [lane.shadow_wallets() for _ in range(3)]
        self.assertTrue(all(len(p) == 5 for p in polled))
        self.assertEqual(len({w for p in polled for w in p}), 15)               # rotation, not the same five
        self.seed(ex, OTHER, state="shadow")
        self.buy(lane, OTHER, "lone")
        self.assertEqual(ex.state["positions"], [])                            # a watchlist hit alone never buys
        self.assertTrue(any(p["wallet"] == OTHER for p in lane.st["positions"]))  # it is paper traded

    # -- confluence --------------------------------------------------------------------------
    def test_two_watched_wallets_within_three_minutes_make_exactly_one_order(self):
        executor, ex = self.setup()
        for w in (OTHER, THIRD, FOURTH):
            self.seed(ex, w, state="shadow")
        now = time.time()
        self.buy(ex.scout, OTHER, "a", ts=now - 100)
        self.assertEqual(ex.state["positions"], [])
        self.buy(ex.scout, THIRD, "b", ts=now - 5)
        self.buy(ex.scout, FOURTH, "c", ts=now)                                 # a third wallet adds confidence, not an order
        live = [p for p in ex.state["positions"] if p.get("mint") == MINT]
        self.assertEqual(len(live), 1)
        self.assertTrue(live[0]["convergence"])
        self.assertEqual(ex.scout.cfg.convergence_window_seconds, 180.0)

    def test_buys_further_apart_than_the_window_are_no_confluence(self):
        executor, ex = self.setup()
        self.seed(ex, OTHER, state="shadow")
        self.seed(ex, THIRD, state="shadow")
        now = time.time()
        self.buy(ex.scout, OTHER, "a", ts=now - 200)
        self.buy(ex.scout, THIRD, "b", ts=now)
        self.assertEqual(ex.scout.st["convergence_events"], [])
        self.assertEqual(ex.state["positions"], [])

    def test_small_buys_count_toward_confluence_but_open_nothing_alone(self):
        executor, ex = self.setup()
        self.seed(ex, OTHER, state="shadow")
        self.seed(ex, THIRD, state="shadow")
        self.buy(ex.scout, OTHER, "a", usd=150.0)                                # under the $300 lone-copy minimum
        self.assertEqual(ex.scout.st["positions"], [])
        self.buy(ex.scout, THIRD, "b", usd=150.0)
        self.assertEqual(len(ex.scout.st["convergence_events"]), 1)
        self.buy(ex.scout, FOURTH, "c", mint="Other", usd=50.0)                   # under CONFLUENCE_MIN_SOURCE_USD: not even noted
        self.assertNotIn("Other", ex.scout.st.get("recent_buys") or {})

    def test_a_live_set_buy_and_a_watchlist_buy_converge_into_one_order(self):
        executor, ex = self.setup(COPY_WALLETS=f"{WALLET},{OTHER}", COPY_WATCH_ONLY=OTHER[:8])
        self.seed(ex, THIRD, state="shadow")
        ex.copy_handle_event(OTHER, "src1", int(time.time()), tx(10.0, 5.0, 0, 1000, owner=OTHER), SOL, True)
        self.assertEqual(ex.state["positions"], [])                             # watch-only: not copied on its own
        rows = list(csv.DictReader(open(os.path.join(os.environ["DATA_DIR"], "copy_signals.csv"))))
        self.assertEqual(rows[-1]["reason"], "watch_only")
        self.buy(ex.scout, THIRD, "b")
        live = [p for p in ex.state["positions"] if p.get("mint") == MINT]
        self.assertEqual(len(live), 1)
        self.assertEqual(sorted(live[0]["convergence_wallets"]), sorted([OTHER, THIRD]))

    def test_a_member_sale_before_our_entry_does_not_close_the_live_position(self):
        executor, ex = self.setup()
        self.seed(ex, OTHER, state="shadow")
        self.seed(ex, THIRD, state="shadow")
        now = time.time()
        self.buy(ex.scout, OTHER, "a", ts=now - 20)
        self.buy(ex.scout, THIRD, "b", ts=now - 10)
        live = [p for p in ex.state["positions"] if p.get("convergence")]
        self.assertEqual(len(live), 1)
        closed = []
        ex.copy_execute_exit = lambda pos, wallet, sig, fraction, target, sol_price, attempt=1: closed.append(wallet) or True
        ex.scout.shadow_follow_sell(THIRD, MINT, 1.0, SOL, "old-sell", int(now - 120))   # sold before we bought
        self.assertEqual(closed, [])
        ex.scout.shadow_follow_sell(THIRD, MINT, 1.0, SOL, "new-sell", int(now + 5))
        self.assertEqual(closed, [THIRD])

    def test_an_empty_balance_right_after_our_buy_is_retried_not_booked_as_a_loss(self):
        executor, ex = self.setup()
        ex.cfg.mode = "live"
        pos = {"mint": MINT, "tokens": 1000, "position_usd": 8.0, "peak_usd": 8.0, "opened_ts": time.time() - 2,
               "opened_at": "t", "copy": WALLET, "buy_signature": "sig"}
        ex.state["positions"] = [pos]
        ex.sellable = lambda mint, tracked: 0
        with self.assertRaises(RuntimeError):
            ex.close_position(pos, "copy_sell", SOL)
        self.assertEqual(ex.state["positions"], [pos])
        self.assertEqual(ex.state["daily"]["realized_pnl_usd"], 0.0)

    # -- scout: hold time and reject counts ---------------------------------------------------
    def test_hold_time_is_a_five_minute_gate_and_only_sniper_farms_are_rejected(self):
        executor, ex = self.setup()
        cand = self.seed(ex, OTHER, state="shadow")
        cand["history"]["metrics_30d"]["median_hold_minutes"] = 8.0
        ev = scout.evaluate_candidate(cand, ex.scout.cfg, time.time(), 300.0)
        self.assertEqual(ev["gates"]["hold_time"]["status"], "pass")            # 8 min: the old 30-min rule is gone
        self.assertEqual(ev["gates"]["sniper"]["status"], "pass")
        cand["history"]["metrics_30d"]["median_hold_minutes"] = 0.5            # 30 s: a sniper farm
        ex.scout.evaluate_all()
        self.assertEqual(cand["state"], "rejected")
        report = scout.candidate_report(ex.scout.st, ex.scout.cfg, time.time())
        self.assertEqual(report["rejected_by"], {"sniper": 1})
        self.assertIn("sniper", report["gate_failures"])
        self.assertIn("hold_time", report["gate_failures"])

    def test_a_measured_sniper_is_never_copied(self):
        executor, ex = self.setup()
        cand = self.seed(ex, WALLET, state="shadow")
        cand["history"]["metrics_30d"]["median_hold_minutes"] = 0.1              # 6 s median hold
        self.assertTrue(ex.scout.entry_allowed(WALLET)[0])                       # the owner's wallet: always copied
        scouted = self.seed(ex, OTHER, state="live")
        scouted["history"]["metrics_30d"]["median_hold_minutes"] = 0.1
        allowed, why = ex.scout.entry_allowed(OTHER)
        self.assertFalse(allowed)
        self.assertIn("sniper", why)

    def test_a_rejected_wallet_opens_no_new_paper_trades(self):
        executor, ex = self.setup()
        cand = self.seed(ex, OTHER, state="shadow")
        self.buy(ex.scout, OTHER, "before")
        self.assertEqual(len(ex.scout.st["positions"]), 1)
        cand["state"] = "rejected"
        self.buy(ex.scout, OTHER, "after", mint="Other")
        self.assertEqual(len(ex.scout.st["positions"]), 1)                     # the open one still follows its sells
        self.assertNotIn("Other", ex.scout.st.get("recent_buys") or {})

    # -- promote / demote ---------------------------------------------------------------------
    def test_a_configured_wallet_red_for_us_is_still_always_copied(self):
        executor, ex = self.setup()
        for _ in range(15):
            ex.note_copy_pnl({"copy": WALLET}, -1.0, closed=True)
        self.assertTrue(ex.scout.entry_allowed(WALLET)[0])
        self.assertEqual(ex.scout.st["live"][WALLET]["fills"], 15)               # still tracked, for the report
        entered = []
        ex.enter_with_retry = lambda item, sol_price: entered.append(item)
        ex.copy_handle_event(WALLET, "buy1", int(time.time()), tx(10.0, 5.0, 0, 1000), SOL, True)
        self.assertEqual(len(entered), 1)
        ex.state["positions"] = [{"mint": MINT, "tokens": 1000, "position_usd": 8.0, "last_value_usd": 8.0, "peak_usd": 8.0,
                                  "opened_ts": time.time() - 600, "opened_at": "t", "copy": WALLET, "buy_signature": "b"}]
        exits = []
        ex.copy_execute_exit = lambda pos, wallet, sig, fraction, target, sol_price, attempt=1: exits.append((wallet, target)) or True
        ex.copy_handle_event(WALLET, "sell1", int(time.time()), tx(5.0, 9.0, 1000, 0), SOL, True)
        self.assertEqual(exits, [(WALLET, 0)])
        report = scout.funnel_report(ex.scout.st, ex.scout.cfg, time.time(), [WALLET])
        self.assertEqual(report["live_set"]["configured"][0]["status"], "always_copied")

    def test_a_green_wallet_is_not_demoted_after_ten_fills(self):
        executor, ex = self.setup()
        for i in range(12):
            ex.note_copy_pnl({"copy": WALLET}, 2.0 if i % 2 else -1.0, closed=True)
        self.assertTrue(ex.scout.entry_allowed(WALLET)[0])

    def test_scouted_live_wallet_red_for_us_goes_back_to_the_watchlist(self):
        executor, ex = self.setup(SCOUT_LIVE="1", SCOUT_LIVE_LOSS_BUDGET_USD="100")
        cand = self.seed(ex, OTHER, state="live")
        for _ in range(10):
            ex.note_copy_pnl({"copy": OTHER}, -1.0, closed=True)
        self.assertEqual(cand["state"], "shadow")
        self.assertEqual(ex.scout.live_wallets(), {})
        self.assertIn(OTHER, ex.scout.watchlist())

    def test_paper_green_promotes_and_coming_back_needs_new_paper_fills(self):
        executor, ex = self.setup(SCOUT_LIVE="1", SCOUT_LIVE_LOSS_BUDGET_USD="50")
        cand = self.seed(ex, OTHER, state="shadow")
        cand["stats"]["7d"]["realized_profit"] = -5.0                           # no history route
        ex.scout.evaluate_all()
        ex.scout.evaluate_all()
        self.assertEqual(cand["state"], "live")
        self.assertEqual(cand["route"], "paper")
        now = time.time()
        ex.scout.demote(OTHER, "test")
        self.assertEqual(cand["state"], "shadow")
        ex.scout.st["trades"] = good_shadow_trades(OTHER, now - 20 * DAY)        # every paper fill predates the demotion
        cand["demoted_ts"] = ex.scout.st["live"][OTHER]["demoted_ts"] = now - 2 * DAY
        ex.scout.evaluate_all()
        self.assertEqual(cand["state"], "shadow")
        self.assertIn("0 paper fills", cand["paper"]["detail"])
        ex.scout.st["trades"] += [dict(t, opened_ts=now - DAY + i * 60, closed_ts=now - DAY + i * 60 + 600)
                                  for i, t in enumerate(good_shadow_trades(OTHER, now, n=8))]
        ex.scout.evaluate_all()
        ex.scout.evaluate_all()
        self.assertEqual(cand["state"], "live")

    def test_an_idle_live_wallet_is_demoted(self):
        executor, ex = self.setup(SCOUT_LIVE="1", SCOUT_LIVE_LOSS_BUDGET_USD="50")
        cand = self.seed(ex, OTHER, state="live")
        old = time.time() - 10 * DAY
        for row in cand["stats"].values():
            row["last_timestamp"] = int(old)
        ex.scout.st["first_watched"] = {OTHER: old}
        ex.scout.evaluate_all()
        self.assertEqual(cand["state"], "shadow")
        self.assertIn("no trade in 7 days", ex.scout.st["live"][OTHER]["demoted_reason"])

    def test_live_set_is_capped(self):
        executor, ex = self.setup(SCOUT_LIVE="1", SCOUT_LIVE_LOSS_BUDGET_USD="50", LIVE_COPY_MAX="2")
        self.seed(ex, OTHER, state="shadow")
        self.seed(ex, THIRD, state="shadow")
        ex.scout.evaluate_all()
        ex.scout.evaluate_all()
        live = [c for c in ex.scout.st["candidates"].values() if c["state"] == "live"]
        self.assertEqual(len(live), 1)                                          # the configured wallet holds the other seat

    # -- sizing, daily stop, polling ----------------------------------------------------------
    def test_funnel_defaults_and_daily_stop_counting_open_losses(self):
        executor, ex = self.setup()
        self.assertEqual(ex.cfg.max_position_usd, 8.0)
        self.assertEqual(ex.cfg.daily_loss_limit_usd, 10.0)
        ex.state["daily"]["realized_pnl_usd"] = -3.0
        ex.state["positions"] = [{"mint": "m1", "tokens": 1, "position_usd": 10.0, "last_value_usd": 2.5, "peak_usd": 10.0,
                                  "opened_ts": time.time(), "opened_at": "t", "copy": WALLET},
                                 {"mint": "m2", "tokens": 1, "position_usd": 10.0, "last_value_usd": 30.0, "peak_usd": 30.0,
                                  "opened_ts": time.time(), "opened_at": "t", "copy": WALLET}]
        self.assertEqual(ex.daily_pnl_usd(), -10.5)                              # the winner does not offset the loser
        skips = []
        ex.skip = lambda mint, reason: skips.append(reason)
        ex.try_enter({"mint": "m3", "graduated_ts": time.time(), "enter_at": time.time(), "copy": WALLET, "copy_buy_usd": 500}, SOL)
        self.assertTrue(skips and "sizing guards" in skips[0], skips)

    def test_copy_poll_round_rotates_and_puts_held_wallets_first(self):
        executor, ex = self.setup()
        wallets = [f"L{i:02d}" for i in range(25)]
        ex.state["positions"] = [{"mint": "m", "copy": "L20"}]
        first = ex.copy_poll_round(wallets)
        self.assertEqual(len(first), 10)
        self.assertEqual(first[0], "L20")
        second = ex.copy_poll_round(wallets)
        self.assertEqual(second[0], "L20")
        self.assertEqual(len(set(first[1:]) & set(second[1:])), 0)
        self.assertEqual(ex.copy_poll_round(wallets[:4]), wallets[:4])

    def test_funnel_report_has_the_numbers_the_spec_asks_for(self):
        executor, ex = self.setup()
        self.seed(ex, OTHER, state="shadow")
        report = ex.scout.report()
        f = report["funnel"]
        for key in ("watchlist", "live_set", "paper_pnl_by_wallet", "confluence_today", "overlap_candidates", "jev"):
            self.assertIn(key, f)
        self.assertEqual(f["watchlist"]["max"], 400)
        self.assertEqual(f["live_set"]["max"], 40)
        self.assertTrue(any(r["wallet"] == OTHER for r in f["paper_pnl_by_wallet"]))
        self.assertIn("gate_failures", report)


class HoldModeTests(unittest.TestCase):
    make, fake_market = test_scout.LaneTests.make, test_scout.LaneTests.fake_market
    FRANK = "498g1rVnFcnjBjpfw1xyqA1WvgQXUU8RWuELjxkjAayQ"

    def setup(self, **env):
        env.setdefault("PAPER_BALANCE_USD", "200")
        env.setdefault("COPY_WALLETS", f"{WALLET},{self.FRANK}")
        executor, ex = self.make(**env)
        ex.state["paper_balance_usd"] = 200.0
        self.fake_market(ex)
        return executor, ex

    def test_frankdegods_is_copied_in_hold_mode_not_watch_only(self):
        executor, ex = self.setup()
        self.assertTrue(ex.scout.entry_allowed(self.FRANK)[0])
        self.assertEqual(ex.copy_hold_mode(self.FRANK), (True, "COPY_HOLD_WALLETS"))
        self.assertEqual(ex.copy_hold_mode(WALLET), (False, ""))
        ex.copy_handle_event(self.FRANK, "fbuy", int(time.time()), tx(10.0, 5.0, 0, 1000, owner=self.FRANK), SOL, True)
        pos = ex.state["positions"][0]
        self.assertTrue(pos["hold_with_source"])
        self.assertIsNone(pos["ladder"])
        xcfg = ex.exit_cfg(pos)
        self.assertEqual(xcfg.take_profit, float("inf"))
        self.assertEqual(xcfg.stop_loss, 0.40)
        self.assertEqual(xcfg.trailing_stop, 0.0)
        self.assertEqual(xcfg.breakeven_arm, 0.0)
        self.assertEqual(xcfg.time_stop_minutes, 7 * 1440)
        basis = pos["position_usd"]
        now = time.time()
        # +100% then a 45% pullback from the peak, and a trip back to entry: nothing fires.
        self.assertIsNone(executor.decide_exit(basis, basis * 2.0, now - 86400, now, xcfg, basis * 2.0))
        self.assertIsNone(executor.decide_exit(basis, basis * 1.1, now - 86400, now, xcfg, basis * 2.0))
        self.assertIsNone(executor.decide_exit(basis, basis * 0.99, now - 3 * 86400, now, xcfg, basis * 1.3))
        self.assertEqual(executor.decide_exit(basis, basis * 0.59, now, now, xcfg, basis), "stop_loss")
        # The wallet's own sells are the exit: a trim trims ours, a full sale closes it.
        exits = []
        ex.copy_execute_exit = lambda p, wallet, sig, fraction, target, sol_price, attempt=1: exits.append(target) or True
        pos["last_value_usd"] = basis * 2
        ex.copy_handle_event(self.FRANK, "ftrim", int(time.time()) + 5, tx(5.0, 6.0, 1000, 700, owner=self.FRANK), SOL, True)
        ex.copy_handle_event(self.FRANK, "fsell", int(time.time()) + 6, tx(6.0, 9.0, 700, 0, owner=self.FRANK), SOL, True)
        self.assertEqual(exits[-1], 0)
        self.assertEqual(len(exits), 2)

    def test_long_measured_hold_turns_hold_mode_on(self):
        executor, ex = self.setup(COPY_HOLD_WALLETS="")
        ex.state["gmgn_verdicts"] = {WALLET: {"verdict": "copy", "hold_hours": 40.0}}
        self.assertEqual(ex.copy_hold_mode(WALLET), (True, "GMGN average hold 40h"))
        ex.state["gmgn_verdicts"] = {WALLET: {"verdict": "copy", "hold_hours": 2.0}}
        self.assertFalse(ex.copy_hold_mode(WALLET)[0])
        self.assertFalse(ex.copy_hold_mode(self.FRANK)[0])                    # unlisted and unmeasured


class OverlapTests(unittest.TestCase):
    def pools(self, now):
        def pool(mint, age_h, mcap):
            created = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - age_h * 3600))
            return {"attributes": {"name": mint, "pool_created_at": created, "market_cap_usd": str(mcap)},
                    "relationships": {"base_token": {"data": {"id": f"solana_{mint}"}}, "dex": {"data": {"id": "pumpswap"}}}}
        return {"data": [pool("Runner1", 10, 900_000), pool("Runner2", 40, 400_000), pool("Old", 100, 5_000_000),
                         pool("Small", 5, 90_000), pool("Runner1", 10, 850_000)]}

    def test_runners_are_young_and_big_one_row_per_mint(self):
        cfg = test_scout.cfg_with(SCOUT_OVERLAP="1")
        now = time.time()
        runners, errors = scout.fetch_runners(cfg, now, http_get=lambda url: self.pools(now))
        self.assertEqual([r["mint"] for r in runners], ["Runner1", "Runner2"])
        self.assertEqual(runners[0]["mcap_usd"], 900_000)
        self.assertEqual(errors, [])
        runners, errors = scout.fetch_runners(cfg, now, http_get=lambda url: (_ for _ in ()).throw(RuntimeError("429")))
        self.assertEqual(runners, [])
        self.assertEqual(len(errors), 6)

    def test_wallets_ranked_by_runners_hit_without_snipers(self):
        cfg = test_scout.cfg_with(SCOUT_OVERLAP="1")
        traders = {
            "R1": [{"address": "A"}, {"address": "B"}, {"address": "S", "tags": ["sniper"]},
                   {"address": "E", "start_holding_at": 1002, "token_created_at": 1000}],
            "R2": [{"address": "A"}, {"address": "S", "tags": ["sniper"]}, {"address": "E", "start_holding_at": 1002, "token_created_at": 1000}],
            "R3": [{"address": "A"}, {"address": "B"}, {"address": "T", "transfer_in": True}],
        }
        ranked = scout.overlap_rank(traders, cfg)
        self.assertEqual([(r["address"], r["hits"]) for r in ranked], [("A", 3), ("B", 2)])

    def test_discovery_feeds_overlap_finds_into_the_watchlist_first(self):
        now = time.time()
        cfg = test_scout.cfg_with(SCOUT_OVERLAP="1", SCOUT_ENRICH_PER_CYCLE="1")

        class Client:
            pause_seconds = 0
            def smart_money(self, chain, limit): return [{"maker": "Feed1"}]
            def kol(self, chain, limit): return []
            def market_rank(self, chain, limit): return []
            def top_traders(self, chain, token, tag=None, limit=20):
                return [{"address": "Grinder"}] + ([{"address": "Once"}] if token == "R1" else [])

        enriched = []
        runners = [{"mint": "R1", "mcap_usd": 900_000}, {"mint": "R2", "mcap_usd": 500_000}]
        with mock.patch.object(scout, "fetch_runners", lambda cfg, now: (runners, [])), \
                mock.patch.object(scout, "enrich_wallet", lambda client, cfg, a, now, m, b: (enriched.append(a), ({}, 1))[1]):
            out = scout.discover_and_enrich(Client(), cfg, {}, [], [], now, 300.0)
        self.assertEqual(out["overlap"][0], {"address": "Grinder", "hits": 2, "runners": ["R1", "R2"], "tags": []})
        self.assertEqual(enriched, ["Grinder"])                                  # the overlap find is enriched first
        self.assertIn("overlap:2", [s["source"] for s in out["candidates"]["Grinder"]["sources"]])


class JevClientTests(unittest.TestCase):
    def client(self, responses, **env):
        env.setdefault("OPENROUTER_API_KEY", "or-key")
        calls = []

        def post(url, key, body, timeout):
            calls.append((url, key, body, timeout))
            r = responses.pop(0) if responses else answers()
            if isinstance(r, Exception):
                raise r
            return r
        with mock.patch.dict(os.environ, env):
            cfg = jev.JevConfig()
        clock = [1000.0]
        return jev.Jev(cfg, stats={}, post=post, clock=lambda: clock[0]), calls, clock

    def test_request_shape_and_buy_rule(self):
        client, calls, _ = self.client([answers("buy", 0.8, 0.2)])
        decision, detail = client.entry({"mint": "M", "our_ticket_usd": 8}, "M")
        self.assertEqual(decision, "buy")
        url, key, body, timeout = calls[0]
        self.assertEqual(url, jev.OPENROUTER_URL)
        self.assertEqual(key, "or-key")
        self.assertEqual(body["model"], "jev-1.13")
        self.assertEqual(set(body["questions"]), {"action", "dump_risk"})
        self.assertEqual(body["questions"]["action"]["type"], "choice")
        self.assertEqual(body["questions"]["dump_risk"]["type"], "noul")
        self.assertAlmostEqual(timeout, 0.6)
        self.assertIn("p_buy=0.80", detail)

    def test_low_probability_high_dump_risk_or_watch_is_no_buy(self):
        for resp in (answers("buy", 0.55, 0.2), answers("buy", 0.9, 0.7), answers("watch", 0.9, 0.1)):
            client, _, _ = self.client([resp])
            self.assertEqual(client.entry({"mint": "M"}, "M")[0], "skip")

    def test_errors_are_no_decision_and_fall_back_to_typesafe(self):
        client, calls, _ = self.client([TimeoutError("timed out"), answers("buy", 0.9, 0.1)], TYPESAFE_API_KEY="ts-key")
        self.assertEqual(client.entry({"mint": "M"}, "M")[0], "buy")
        self.assertEqual([c[0] for c in calls], [jev.OPENROUTER_URL, jev.TYPESAFE_URL])
        self.assertEqual(calls[1][2]["model"], "jev-1.13.0")
        self.assertEqual(client.stats["timeouts"], 1)
        client, calls, _ = self.client([RuntimeError("HTTP 500")])
        self.assertEqual(client.entry({"mint": "M"}, "M")[0], "no_decision")

    def test_cache_and_rate_cap(self):
        client, calls, clock = self.client([], JEV_MAX_CALLS_PER_MIN="2")
        client.entry({"mint": "M"}, "M")
        client.entry({"mint": "M"}, "M")                                          # same coin inside 15 s: cached
        self.assertEqual(len(calls), 1)
        client.entry({"mint": "N"}, "N")
        self.assertEqual(client.entry({"mint": "P"}, "P")[0], "no_decision")      # third call in a minute
        self.assertEqual(client.stats["rate_capped"], 1)
        clock[0] += 61
        self.assertEqual(client.entry({"mint": "P"}, "P")[0], "buy")

    def test_no_key_means_the_gate_is_off(self):
        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "", "TYPESAFE_API_KEY": ""}):
            cfg = jev.JevConfig()
        self.assertFalse(cfg.active)
        self.assertIn("neither OPENROUTER_API_KEY nor TYPESAFE_API_KEY", cfg.describe())


class JevGateTests(unittest.TestCase):
    make, fake_market = test_scout.LaneTests.make, test_scout.LaneTests.fake_market

    def setup(self, post, **env):
        env.setdefault("OPENROUTER_API_KEY", "or-key")
        env.setdefault("PAPER_BALANCE_USD", "200")
        executor, ex = self.make(**env)
        ex.state["paper_balance_usd"] = 200.0
        self.fake_market(ex)
        ex.rpc.token_supply_details = lambda mint: (1e9, 6, 10**15)
        self.calls = []
        ex.jev.post = lambda url, key, body, timeout: (self.calls.append(body), post(body))[1]
        self.skips = []
        ex.skip = lambda mint, reason: self.skips.append(reason)
        return executor, ex

    def enter(self, ex):
        ex.try_enter({"mint": MINT, "graduated_ts": time.time(), "enter_at": time.time(), "copy": WALLET, "copy_buy_usd": 500}, SOL)

    def test_jev_skip_blocks_after_the_hard_filters_and_is_logged(self):
        executor, ex = self.setup(lambda body: answers("skip", 0.7, 0.8))
        self.enter(ex)
        self.assertEqual(ex.state["positions"], [])
        self.assertTrue(self.skips[0].startswith("jev: choice=skip"), self.skips)
        state = self.calls[0]["state"]
        self.assertEqual(state["mint"], MINT)
        self.assertEqual(state["source_wallet"], WALLET)
        self.assertEqual(state["our_ticket_usd"], 8.0)
        self.assertIn("round_trip_pct", state)
        rows = list(csv.DictReader(open(os.path.join(os.environ["DATA_DIR"], "jev_calls.csv"))))
        self.assertIn('"skip"', rows[-1]["answers"])
        self.assertEqual(ex.state["jev"]["skip"], 1)

    def test_jev_buy_lets_the_entry_through(self):
        executor, ex = self.setup(lambda body: answers("buy", 0.9, 0.1))
        self.enter(ex)
        self.assertEqual(len(ex.state["positions"]), 1)
        self.assertEqual(ex.state["jev"]["buy"], 1)

    def test_no_decision_fails_closed_unless_fail_open(self):
        def boom(body):
            raise TimeoutError("timed out")
        executor, ex = self.setup(boom)
        self.enter(ex)
        self.assertEqual(ex.state["positions"], [])
        self.assertIn("no decision", self.skips[0])
        executor, ex = self.setup(boom, JEV_FAIL_OPEN="1")
        self.enter(ex)
        self.assertEqual(len(ex.state["positions"]), 1)

    def test_without_a_key_the_hard_filters_alone_decide(self):
        executor, ex = self.setup(lambda body: answers("skip"), OPENROUTER_API_KEY="", JEV_ENABLED="1")
        self.enter(ex)
        self.assertEqual(len(ex.state["positions"]), 1)
        self.assertEqual(self.calls, [])
        executor, ex = self.setup(lambda body: answers("skip"), JEV_ENABLED="0")
        self.enter(ex)
        self.assertEqual(len(ex.state["positions"]), 1)
        self.assertEqual(self.calls, [])

    def test_copied_sells_never_wait_on_jev(self):
        executor, ex = self.setup(lambda body: answers("skip"))
        ex.state["positions"] = [{"mint": MINT, "tokens": 1000, "position_usd": 8.0, "last_value_usd": 8.0, "peak_usd": 8.0,
                                  "opened_ts": time.time() - 600, "opened_at": "t", "copy": WALLET, "buy_signature": "b"}]
        exits = []
        ex.copy_execute_exit = lambda pos, wallet, sig, fraction, target, sol_price, attempt=1: exits.append(target) or True
        ex.copy_handle_event(WALLET, "sell1", int(time.time()), tx(5.0, 9.0, 1000, 0), SOL, True)
        self.assertEqual(exits, [0])
        self.assertEqual(self.calls, [])

    def test_exit_vote_is_opt_in_and_sells_on_a_confident_sell(self):
        sell = {"model": "jev", "usage": {}, "answers": {"action": {"type": "choice", "choice": "sell", "confidence": 0.8,
                                                                     "probabilities": {"sell": 0.8, "hold": 0.2}}}}
        executor, ex = self.setup(lambda body: sell)
        pos = {"mint": MINT, "tokens": 1000, "position_usd": 8.0, "last_value_usd": 6.0, "peak_usd": 9.0,
               "opened_ts": time.time() - 600, "opened_at": "t", "copy": WALLET, "buy_signature": "b"}
        ex.state["positions"] = [pos]
        closed = []
        ex.close_position = lambda p, reason, sol_price: closed.append(reason)
        ex.jev_exit_review(SOL)
        self.assertEqual(closed, [])                                           # off by default
        ex.jev.cfg.exit_enabled = True
        ex.jev_exit_review(SOL)
        self.assertEqual(closed, ["jev_sell"])
        self.assertEqual(set(self.calls[0]["questions"]), {"action"})
        ex.jev_exit_review(SOL)                                                 # asked at most every 20 s
        self.assertEqual(len(closed), 1)


if __name__ == "__main__":
    unittest.main()
