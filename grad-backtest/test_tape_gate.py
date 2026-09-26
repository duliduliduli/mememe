"""The runner lane's dead-tape gate. Band and momentum are read off one Jupiter price sample a
minute, which a single buy on a thin pool can fake, so an entry also has to show real trading
behind the move: successful transactions touching the mint, counted from signature metadata
alone. The gate reads only for tokens already about to enter, under a per-cycle budget, never
before the exits, and fails open whenever the tape cannot be read."""
import os
import tempfile
import unittest
from unittest import mock

SOL = 100.0
# With the 1e9-token supply make() stubs, 0.0003 is a $300k market cap (below the $400k-$4M band)
# and 0.0006 is $600k, so a token that climbs from one to the other is in band and up 100%.
LOW = 0.0003
HIGH = 0.0006


def fresh(**env):
    tmp = tempfile.mkdtemp(prefix="grad-tape-test-")
    base = {"DATA_DIR": tmp, "HELIUS_API_KEY": "k", "MIGRATION_ADDRESS": "a", "EXECUTOR_MODE": "paper"}
    base.update(env)
    patcher = mock.patch.dict(os.environ, base)
    patcher.start()
    import importlib
    import executor
    importlib.reload(executor)
    return executor, patcher


def tape(count, complete=True, window=300.0):
    return {"tx_count": count, "window_seconds": window, "newest_ts": 0.0, "oldest_ts": 0.0, "complete": complete}


def stub_tape(ex, reads, result):
    """Replace the tape reader with one that records (mint, window, limit) and answers `result`:
    a value, a callable of the mint, or an exception to raise."""
    def read(mint, window_seconds, limit=50):
        reads.append((mint, window_seconds, limit))
        if isinstance(result, BaseException):
            raise result
        return result(mint) if callable(result) else result
    ex.rpc.tape_activity = read


def sig(name, t, err=None):
    return {"signature": name, "blockTime": t, "err": err}


class TapeGateTests(unittest.TestCase):
    def make(self, **env):
        executor, p = fresh(**env)
        self.addCleanup(p.stop)
        ex = executor.Executor(executor.Config())
        ex.rpc.token_supply_details = lambda mint: (1_000_000_000.0, 6, 1_000_000_000 * 10 ** 6)
        self.entered = []
        ex.enter_with_retry = lambda item, sol_price: self.entered.append(item)
        self.reads = []
        # Bodies are never read while RUNNER_TAPE_UNIQUE_BUYERS is 0; the mock keeps it that way
        # (and off the network) if that ever changes.
        self.buyer_reads = ex.rpc.unique_buyers_since = mock.Mock(return_value=0)
        return executor, ex

    def watch(self, executor, ex, *mints):
        now = int(executor.now_ts())
        for mint in mints:
            ex.watch_runner(mint, now - 600)

    def feed(self, ex, *maps):
        """Serve one price map per watchlist check, repeating the last one."""
        queued = iter(maps)
        last = maps[-1]
        ex.runner_prices = lambda mints: next(queued, last)

    def check(self, ex):
        ex.state["watchlist_checked_ts"] = 0
        ex.manage_watchlist(SOL)

    def logged(self, executor, fn):
        lines = []
        with mock.patch.object(executor, "log", side_effect=lines.append):
            fn()
        return "\n".join(lines)

    def test_defaults(self):
        executor, p = fresh()
        self.addCleanup(p.stop)
        cfg = executor.Config()
        self.assertEqual(cfg.runner_min_tape_txs, 8)
        self.assertEqual(cfg.runner_tape_window_seconds, 300)
        self.assertEqual(cfg.runner_tape_budget, 3)
        self.assertEqual(cfg.runner_tape_unique_buyers, 0)

    def test_floor_and_window_come_from_config_and_the_floor_is_inclusive(self):
        executor, ex = self.make(RUNNER_CHECK_SECONDS="15", RUNNER_MIN_TAPE_TXS="3", RUNNER_TAPE_WINDOW_SECONDS="90")
        self.watch(executor, ex, "LIVE")
        stub_tape(ex, self.reads, tape(3, window=90.0))       # exactly the floor is enough
        self.feed(ex, {"LIVE": LOW}, {"LIVE": HIGH})
        self.check(ex)
        out = self.logged(executor, lambda: self.check(ex))

        self.assertEqual([e["mint"] for e in self.entered], ["LIVE"])
        self.assertEqual(self.reads, [("LIVE", 90.0, 25)])
        self.assertIn("(tape=3tx/90s); entering", out)

    def test_dead_tape_blocks_an_otherwise_valid_entry(self):
        executor, ex = self.make(RUNNER_CHECK_SECONDS="15")
        self.watch(executor, ex, "DEAD")
        stub_tape(ex, self.reads, tape(2))
        self.feed(ex, {"DEAD": LOW}, {"DEAD": HIGH})
        self.check(ex)
        out = self.logged(executor, lambda: self.check(ex))

        self.assertEqual(self.entered, [])
        self.assertEqual([w["mint"] for w in ex.state["watchlist"]], ["DEAD"])
        # one signature-metadata read for the token that was about to enter, over the configured
        # window, with a page deep enough that a busy tape cannot be cut off below the floor
        self.assertEqual(self.reads, [("DEAD", 300.0, 25)])
        self.buyer_reads.assert_not_called()
        self.assertIn("dead tape, 2 successful transaction(s) in the last 300s < 8; not entering", out)

    def test_live_tape_allows_the_entry_and_is_logged_on_it(self):
        executor, ex = self.make(RUNNER_CHECK_SECONDS="15")
        self.watch(executor, ex, "LIVE")
        stub_tape(ex, self.reads, tape(40))
        self.feed(ex, {"LIVE": LOW}, {"LIVE": HIGH})
        self.check(ex)
        out = self.logged(executor, lambda: self.check(ex))

        self.assertEqual([e["mint"] for e in self.entered], ["LIVE"])
        self.assertTrue(self.entered[0]["runner"])
        self.assertEqual([w["mint"] for w in ex.state["watchlist"]], [])
        self.assertIn("market cap $600,000 in band, +100% over 15m (tape=40tx/300s); entering", out)

    def test_a_page_still_full_of_recent_rows_is_a_lower_bound(self):
        executor, ex = self.make(RUNNER_CHECK_SECONDS="15")
        self.watch(executor, ex, "BUSY")
        stub_tape(ex, self.reads, tape(25, complete=False))
        self.feed(ex, {"BUSY": LOW}, {"BUSY": HIGH})
        self.check(ex)
        out = self.logged(executor, lambda: self.check(ex))

        self.assertEqual([e["mint"] for e in self.entered], ["BUSY"])
        self.assertIn("tape=25tx/300s+", out)

    def test_unreadable_tape_fails_open(self):
        executor, ex = self.make(RUNNER_CHECK_SECONDS="15")
        self.watch(executor, ex, "LIVE")
        stub_tape(ex, self.reads, RuntimeError("service unavailable: all providers cooling down"))
        self.feed(ex, {"LIVE": LOW}, {"LIVE": HIGH})
        self.check(ex)
        out = self.logged(executor, lambda: self.check(ex))

        self.assertEqual([e["mint"] for e in self.entered], ["LIVE"])
        self.assertIn("WARN RUNNER LIVE: tape read failed (service unavailable: all providers "
                      "cooling down); allowing the entry", out)
        self.assertIn("(tape=unreadable); entering", out)

    def test_missing_tape_data_fails_open(self):
        executor, ex = self.make(RUNNER_CHECK_SECONDS="15")
        self.watch(executor, ex, "LIVE")
        stub_tape(ex, self.reads, None)
        self.feed(ex, {"LIVE": LOW}, {"LIVE": HIGH})
        self.check(ex)
        out = self.logged(executor, lambda: self.check(ex))

        self.assertEqual([e["mint"] for e in self.entered], ["LIVE"])
        self.assertIn("WARN RUNNER LIVE: tape read returned no signature data; allowing the entry", out)
        self.assertIn("(tape=no-data); entering", out)

    def test_a_provider_cooldown_is_not_spent_on_tape_reads(self):
        executor, ex = self.make(RUNNER_CHECK_SECONDS="15")
        self.watch(executor, ex, "LIVE")
        ex._provider_cooldown_until = executor.now_ts() + 300
        stub_tape(ex, self.reads, tape(0))
        self.feed(ex, {"LIVE": LOW}, {"LIVE": HIGH})
        self.check(ex)
        out = self.logged(executor, lambda: self.check(ex))

        self.assertEqual([e["mint"] for e in self.entered], ["LIVE"])
        self.assertEqual(self.reads, [])
        self.assertIn("providers cooling down, tape unread; allowing the entry", out)

    def test_zero_threshold_disables_the_gate_entirely(self):
        executor, ex = self.make(RUNNER_CHECK_SECONDS="15", RUNNER_MIN_TAPE_TXS="0")
        self.watch(executor, ex, "DEAD")
        stub_tape(ex, self.reads, tape(0))
        self.feed(ex, {"DEAD": LOW}, {"DEAD": HIGH})
        self.check(ex)
        out = self.logged(executor, lambda: self.check(ex))

        self.assertEqual([e["mint"] for e in self.entered], ["DEAD"])
        self.assertEqual(self.reads, [])          # not one call: the gate is off, not failing open
        self.buyer_reads.assert_not_called()
        self.assertIn("(tape=off); entering", out)

    def test_only_tokens_about_to_enter_are_read(self):
        executor, ex = self.make(RUNNER_CHECK_SECONDS="15")
        self.watch(executor, ex, "BELOW", "ABOVE", "FLAT")
        stub_tape(ex, self.reads, tape(1000))
        self.feed(ex,
                  {"BELOW": 0.0001, "ABOVE": 0.005, "FLAT": HIGH},
                  {"BELOW": 0.0002, "ABOVE": 0.010, "FLAT": 0.00061})
        self.check(ex)
        self.check(ex)

        self.assertEqual(self.reads, [])
        self.assertEqual(self.entered, [])
        self.assertEqual(sorted(w["mint"] for w in ex.state["watchlist"]), ["ABOVE", "BELOW", "FLAT"])

    def test_a_dead_token_does_not_take_the_entry_slot_from_a_live_one(self):
        executor, ex = self.make(RUNNER_CHECK_SECONDS="15", MAX_ENTRIES_PER_CYCLE="1")
        self.watch(executor, ex, "DEAD", "LIVE")
        stub_tape(ex, self.reads, lambda mint: tape(1) if mint == "DEAD" else tape(40))
        self.feed(ex,
                  {"DEAD": LOW, "LIVE": LOW},
                  {"DEAD": 0.0009, "LIVE": HIGH})       # DEAD climbed further, so it ranks first
        self.check(ex)
        self.check(ex)

        self.assertEqual([e["mint"] for e in self.entered], ["LIVE"])
        self.assertEqual([m for m, _, _ in self.reads], ["DEAD", "LIVE"])
        self.assertEqual([w["mint"] for w in ex.state["watchlist"]], ["DEAD"])

    def test_reads_are_capped_per_cycle_and_a_refused_token_waits_out_its_window(self):
        executor, ex = self.make(RUNNER_CHECK_SECONDS="15", RUNNER_TAPE_BUDGET="1")
        self.watch(executor, ex, "A", "B", "C")
        stub_tape(ex, self.reads, tape(0))
        self.feed(ex,
                  {"A": LOW, "B": LOW, "C": LOW},
                  {"A": HIGH, "B": 0.0007, "C": 0.0008})  # gains 100%, 133%, 166%: C ranks first
        self.check(ex)
        out = self.logged(executor, lambda: self.check(ex))

        self.assertEqual(self.entered, [])
        self.assertEqual([m for m, _, _ in self.reads], ["C"])
        self.assertEqual(out.count("tape budget of 1 read(s) a cycle spent; holding the entry"), 2)

        # Next check: C is still inside the window it was refused for, so it costs nothing and the
        # single read goes to B.
        out = self.logged(executor, lambda: self.check(ex))
        self.assertEqual([m for m, _, _ in self.reads], ["C", "B"])
        self.assertEqual(out.count("tape budget of 1 read(s) a cycle spent; holding the entry"), 1)
        self.assertEqual(self.entered, [])

    def test_zero_budget_removes_the_cap(self):
        executor, ex = self.make(RUNNER_CHECK_SECONDS="15", RUNNER_TAPE_BUDGET="0")
        self.watch(executor, ex, "A", "B", "C")
        stub_tape(ex, self.reads, tape(0))
        self.feed(ex,
                  {"A": LOW, "B": LOW, "C": LOW},
                  {"A": HIGH, "B": 0.0007, "C": 0.0008})
        self.check(ex)
        self.check(ex)

        self.assertEqual([m for m, _, _ in self.reads], ["C", "B", "A"])
        self.assertEqual(self.entered, [])

    def test_a_token_whose_tape_wakes_up_enters_on_a_later_check(self):
        executor, ex = self.make(RUNNER_CHECK_SECONDS="15")
        clock = [executor.now_ts()]
        with mock.patch.object(executor, "now_ts", side_effect=lambda: clock[0]):
            self.watch(executor, ex, "WAKE")
            alive = [False]
            stub_tape(ex, self.reads, lambda mint: tape(30) if alive[0] else tape(1))
            self.feed(ex, {"WAKE": LOW}, {"WAKE": HIGH})
            self.check(ex)
            self.check(ex)                                  # refused: dead tape
            self.assertEqual(self.entered, [])
            self.assertEqual(len(self.reads), 1)

            alive[0] = True
            clock[0] += 400                                   # past RUNNER_TAPE_WINDOW_SECONDS
            self.check(ex)

        self.assertEqual([e["mint"] for e in self.entered], ["WAKE"])
        self.assertEqual(len(self.reads), 2)

    def test_tape_reads_never_precede_the_exits(self):
        executor, ex = self.make(RUNNER_CHECK_SECONDS="15")
        self.watch(executor, ex, "LIVE")
        ex.state["positions"] = [{"mint": "open"}]
        order = []
        ex.sol_price_usd = lambda: SOL
        ex.manage_positions = lambda price, panic: order.append("manage_positions")
        ex.poll_graduations = lambda: order.append("poll_graduations")
        ex.enter_with_retry = lambda item, price: order.append(f"enter:{item['mint']}")
        stub_tape(ex, self.reads, lambda mint: (order.append("tape"), tape(40))[1])
        self.feed(ex, {"LIVE": LOW}, {"LIVE": HIGH})

        ex.state["watchlist_checked_ts"] = 0
        ex.run_cycle()                                       # first sample only: nothing to gate
        self.assertEqual(order, ["manage_positions", "poll_graduations"])
        order.clear()

        ex.state["watchlist_checked_ts"] = 0
        ex.run_cycle()
        self.assertEqual(order, ["manage_positions", "poll_graduations", "tape", "enter:LIVE"])


class UniqueBuyerGateTests(unittest.TestCase):
    def make(self, **env):
        executor, p = fresh(**env)
        self.addCleanup(p.stop)
        ex = executor.Executor(executor.Config())
        ex.rpc.token_supply_details = lambda mint: (1_000_000_000.0, 6, 1_000_000_000 * 10 ** 6)
        ex.rpc.tape_activity = lambda mint, window_seconds, limit=50: tape(40)
        self.entered = []
        ex.enter_with_retry = lambda item, sol_price: self.entered.append(item)
        return executor, ex

    def run_entry(self, executor, ex):
        now = int(executor.now_ts())
        ex.watch_runner("SPAM", now - 600)
        feeds = iter([{"SPAM": LOW}, {"SPAM": HIGH}])
        ex.runner_prices = lambda mints: next(feeds)
        ex.state["watchlist_checked_ts"] = 0
        ex.manage_watchlist(SOL)
        ex.state["watchlist_checked_ts"] = 0
        lines = []
        with mock.patch.object(executor, "log", side_effect=lines.append):
            ex.manage_watchlist(SOL)
        return "\n".join(lines)

    def test_one_wallet_spamming_the_tape_is_refused(self):
        executor, ex = self.make(RUNNER_CHECK_SECONDS="15", RUNNER_MIN_TAPE_TXS="8", RUNNER_TAPE_UNIQUE_BUYERS="3")
        ex.rpc.unique_buyers_since = mock.Mock(return_value=1)
        out = self.run_entry(executor, ex)

        self.assertEqual(self.entered, [])
        self.assertEqual(ex.rpc.unique_buyers_since.call_count, 1)
        self.assertIn("1 unique buying wallet(s) in the last 300s < 3; not entering", out)

    def test_a_crowd_of_buyers_passes(self):
        executor, ex = self.make(RUNNER_CHECK_SECONDS="15", RUNNER_TAPE_UNIQUE_BUYERS="3")
        ex.rpc.unique_buyers_since = mock.Mock(return_value=12)
        out = self.run_entry(executor, ex)

        self.assertEqual([e["mint"] for e in self.entered], ["SPAM"])
        self.assertIn("(tape=40tx/300s buyers=12); entering", out)

    def test_unreadable_buyers_fail_open(self):
        executor, ex = self.make(RUNNER_CHECK_SECONDS="15", RUNNER_TAPE_UNIQUE_BUYERS="3")
        ex.rpc.unique_buyers_since = mock.Mock(side_effect=RuntimeError("rpc down"))
        out = self.run_entry(executor, ex)

        self.assertEqual([e["mint"] for e in self.entered], ["SPAM"])
        self.assertIn("WARN RUNNER SPAM: unique-buyer read failed (rpc down); allowing the entry", out)
        self.assertIn("(tape=40tx/300s); entering", out)

    def test_no_bodies_to_count_fails_open(self):
        executor, ex = self.make(RUNNER_CHECK_SECONDS="15", RUNNER_TAPE_UNIQUE_BUYERS="3")
        ex.rpc.unique_buyers_since = mock.Mock(return_value=None)
        out = self.run_entry(executor, ex)

        self.assertEqual([e["mint"] for e in self.entered], ["SPAM"])
        self.assertIn("no transaction bodies to count unique buyers; allowing the entry", out)

    def test_a_dead_signature_count_is_refused_before_any_body_is_read(self):
        executor, ex = self.make(RUNNER_CHECK_SECONDS="15", RUNNER_TAPE_UNIQUE_BUYERS="3")
        ex.rpc.tape_activity = lambda mint, window_seconds, limit=50: tape(2)
        ex.rpc.unique_buyers_since = mock.Mock(side_effect=AssertionError("bodies read unasked for"))
        self.run_entry(executor, ex)

        self.assertEqual(self.entered, [])
        ex.rpc.unique_buyers_since.assert_not_called()


class TapeReaderTests(unittest.TestCase):
    def rpc(self, executor, page):
        rpc = executor.Rpc(executor.Config())
        calls = []
        rpc.call = lambda method, params, timeout=None: (calls.append((method, params)), page)[1]
        return rpc, calls

    def test_counts_only_successful_transactions_inside_the_window(self):
        executor, p = fresh()
        self.addCleanup(p.stop)
        now = executor.now_ts()
        page = [sig("new", now - 5), sig("failed", now - 10, {"InstructionError": [0, "x"]}),
                sig("old", now - 900), sig("edge", now - 301)]
        rpc, calls = self.rpc(executor, page)

        tape_read = rpc.tape_activity("MINT", 300, limit=25)

        self.assertEqual(tape_read["tx_count"], 2)         # "new" and "edge", 2s of slack
        self.assertTrue(tape_read["complete"])             # the page reaches back past the window
        self.assertEqual(tape_read["window_seconds"], 300)
        self.assertEqual(calls, [("getSignaturesForAddress", ["MINT", {"limit": 25, "commitment": "confirmed"}])])

    def test_one_call_and_no_transaction_bodies(self):
        executor, p = fresh()
        self.addCleanup(p.stop)
        rpc, calls = self.rpc(executor, [sig("a", executor.now_ts())])
        rpc.batch_call = mock.Mock(side_effect=AssertionError("tape_activity must not read bodies"))

        rpc.tape_activity("MINT", 300, limit=25)

        self.assertEqual(len(calls), 1)
        rpc.batch_call.assert_not_called()

    def test_a_page_cut_off_inside_the_window_is_flagged_incomplete(self):
        executor, p = fresh()
        self.addCleanup(p.stop)
        now = executor.now_ts()
        rpc, _ = self.rpc(executor, [sig(f"s{i}", now - i) for i in range(4)])

        self.assertFalse(rpc.tape_activity("MINT", 300, limit=4)["complete"])
        self.assertTrue(rpc.tape_activity("MINT", 300, limit=5)["complete"])

    def test_no_usable_signature_data_returns_none(self):
        executor, p = fresh()
        self.addCleanup(p.stop)
        self.assertIsNone(self.rpc(executor, [])[0].tape_activity("MINT", 300))
        self.assertIsNone(self.rpc(executor, [{"signature": "a"}, {"signature": "b"}])[0].tape_activity("MINT", 300))

    def test_a_malformed_page_raises_rather_than_reading_as_dead(self):
        executor, p = fresh()
        self.addCleanup(p.stop)
        rpc, _ = self.rpc(executor, {"value": []})
        with self.assertRaisesRegex(RuntimeError, "mint signature data incomplete"):
            rpc.tape_activity("MINT", 300)

    def test_a_dead_token_answers_with_old_rows_and_counts_zero(self):
        executor, p = fresh()
        self.addCleanup(p.stop)
        now = executor.now_ts()
        rpc, _ = self.rpc(executor, [sig("last", now - 7200), sig("before", now - 9000)])

        read = rpc.tape_activity("MINT", 300, limit=25)

        self.assertEqual(read["tx_count"], 0)
        self.assertTrue(read["complete"])


class UniqueBuyerReaderTests(unittest.TestCase):
    def body(self, rows):
        pre, post = [], []
        for idx, (owner, before, after) in enumerate(rows):
            pre.append({"accountIndex": idx, "mint": "MINT", "owner": owner, "uiTokenAmount": {"amount": str(before)}})
            post.append({"accountIndex": idx, "mint": "MINT", "owner": owner, "uiTokenAmount": {"amount": str(after)}})
        return {"meta": {"err": None, "preTokenBalances": pre, "postTokenBalances": post}}

    def wire(self, executor, page, bodies, plain_wallets):
        rpc = executor.Rpc(executor.Config())
        calls = []

        def call(method, params, timeout=None):
            calls.append((method, params))
            if method == "getSignaturesForAddress":
                return page
            if method == "getMultipleAccounts":
                return {"value": [{"owner": executor.SYSTEM_PROGRAM if a in plain_wallets else "pAMM"}
                                  for a in params[0]]}
            raise AssertionError(method)

        rpc.call = call
        rpc.batch_call = lambda batch, timeout=None: (calls.append(("batch", batch)),
                                                      [bodies.get(c[1][0], {}) for c in batch])[1]
        return rpc, calls

    def test_counts_distinct_plain_wallets_and_drops_the_pool_vault(self):
        executor, p = fresh()
        self.addCleanup(p.stop)
        now = executor.now_ts()
        page = [sig("buy1", now - 10), sig("buy2", now - 20), sig("sell", now - 30),
                sig("failed", now - 40, {"err": 1}), sig("old", now - 9000)]
        bodies = {
            # The vault RECEIVES tokens on a sell and nets out positive here; counting it would
            # read carol's dump as a third buyer.
            "buy1": self.body([("vault", 100, 90), ("alice", 0, 10)]),
            "buy2": self.body([("vault", 90, 70), ("bob", 0, 20), ("alice", 10, 15)]),
            "sell": self.body([("vault", 70, 105), ("carol", 25, 0)]),
        }
        rpc, calls = self.wire(executor, page, bodies, plain_wallets={"alice", "bob", "carol"})

        self.assertEqual(rpc.unique_buyers_since("MINT", now - 300, limit=40), 2)
        self.assertEqual([m for m, _ in calls],
                         ["getSignaturesForAddress", "batch", "getMultipleAccounts"])
        self.assertEqual(len(calls[1][1]), 3)                           # only the in-window successes decoded
        self.assertEqual(calls[2][1][0], ["alice", "bob", "vault"])     # the seller is not a buyer

    def test_bodies_are_batched_at_the_rpc_batch_size(self):
        executor, p = fresh(RPC_BATCH_SIZE="2")
        self.addCleanup(p.stop)
        now = executor.now_ts()
        page = [sig(f"s{i}", now - i) for i in range(5)]
        bodies = {f"s{i}": self.body([(f"w{i}", 0, 10)]) for i in range(5)}
        rpc, calls = self.wire(executor, page, bodies, plain_wallets={f"w{i}" for i in range(5)})

        self.assertEqual(rpc.unique_buyers_since("MINT", now - 300, limit=40), 5)
        self.assertEqual([len(b) for m, b in calls if m == "batch"], [2, 2, 1])

    def test_nothing_in_the_window_is_unknown_not_zero(self):
        executor, p = fresh()
        self.addCleanup(p.stop)
        now = executor.now_ts()
        rpc, calls = self.wire(executor, [sig("old", now - 9000)], {}, set())
        rpc.batch_call = mock.Mock(side_effect=AssertionError("no rows, so no bodies to fetch"))

        self.assertIsNone(rpc.unique_buyers_since("MINT", now - 300))
        self.assertEqual([m for m, _ in calls], ["getSignaturesForAddress"])

    def test_only_sellers_needs_no_account_lookup(self):
        executor, p = fresh()
        self.addCleanup(p.stop)
        now = executor.now_ts()
        rpc, calls = self.wire(executor, [sig("dump", now - 10)],
                              {"dump": self.body([("alice", 25, 0)])}, set())

        self.assertEqual(rpc.unique_buyers_since("MINT", now - 300), 0)
        self.assertEqual([m for m, _ in calls], ["getSignaturesForAddress", "batch"])

    def test_a_malformed_page_raises(self):
        executor, p = fresh()
        self.addCleanup(p.stop)
        rpc, _ = self.wire(executor, None, {}, set())
        with self.assertRaisesRegex(RuntimeError, "mint signature data incomplete"):
            rpc.unique_buyers_since("MINT", executor.now_ts() - 300)


if __name__ == "__main__":
    unittest.main()
