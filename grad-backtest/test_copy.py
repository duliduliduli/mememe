"""Copy mode: mirror the buys of chosen wallets, decoded from their transactions' balance
changes, and follow their sells."""
import importlib
import os
import tempfile
import unittest
from unittest import mock

SOL = 100.0
WALLET = "Wallet111111111111111111111111111111111111"
OTHER = "Other1111111111111111111111111111111111111"
MINT = "Mint11111111111111111111111111111111111111"
WSOL = "So11111111111111111111111111111111111111112"


def fresh(**env):
    tmp = tempfile.mkdtemp(prefix="grad-copy-test-")
    base = {"DATA_DIR": tmp, "HELIUS_API_KEY": "k", "MIGRATION_ADDRESS": "a", "EXECUTOR_MODE": "paper", "COPY_WALLETS": WALLET}
    base.update(env)
    patcher = mock.patch.dict(os.environ, base)
    patcher.start()
    import executor
    importlib.reload(executor)
    return executor, patcher


def tx(sol_before, sol_after, token_before, token_after, owner=WALLET, mint=MINT, err=None):
    """A jsonParsed transaction where `owner` moved SOL and one token balance."""
    keys = [{"pubkey": owner}, {"pubkey": "Program111"}]
    def bal(amount):
        return [{"owner": owner, "mint": mint, "uiTokenAmount": {"amount": str(amount)}}] if amount is not None else []
    return {
        "transaction": {"message": {"accountKeys": keys}},
        "meta": {"err": err, "preBalances": [int(sol_before * 1e9), 0], "postBalances": [int(sol_after * 1e9), 0],
                 "preTokenBalances": bal(token_before), "postTokenBalances": bal(token_after)},
    }


class DecodeTests(unittest.TestCase):
    def test_buy_and_sell_from_balance_changes(self):
        executor, p = fresh()
        self.addCleanup(p.stop)
        buy = executor.wallet_swap_from_transaction(tx(10.0, 8.99, 0, 5_000_000), WALLET)
        self.assertEqual(buy["side"], "buy")
        self.assertEqual(buy["mint"], MINT)
        self.assertAlmostEqual(buy["sol"], 1.01)
        sell = executor.wallet_swap_from_transaction(tx(8.99, 9.8, 5_000_000, 1_000_000), WALLET)
        self.assertEqual(sell["side"], "sell")
        self.assertEqual(sell["tokens"], 4_000_000)
        self.assertIsNone(executor.wallet_swap_from_transaction(tx(10.0, 9.999, 5, 5), WALLET))   # no token change
        self.assertIsNone(executor.wallet_swap_from_transaction(tx(10.0, 9.0, 0, 5, owner=OTHER), WALLET))  # someone else
        self.assertIsNone(executor.wallet_swap_from_transaction(tx(10.0, 9.0, 0, 5, err={"x": 1}), WALLET))

    def test_wrapped_sol_counts_as_sol(self):
        executor, p = fresh()
        self.addCleanup(p.stop)
        t = tx(10.0, 9.999, 0, 7)
        t["meta"]["preTokenBalances"].append({"owner": WALLET, "mint": WSOL, "uiTokenAmount": {"amount": str(int(2e9))}})
        t["meta"]["postTokenBalances"].append({"owner": WALLET, "mint": WSOL, "uiTokenAmount": {"amount": str(int(0.5e9))}})
        swap = executor.wallet_swap_from_transaction(t, WALLET)
        self.assertEqual(swap["side"], "buy")
        self.assertAlmostEqual(swap["sol"], 1.501)


class CheckCadenceTests(unittest.TestCase):
    def test_copy_positions_are_quoted_every_few_seconds_not_every_cycle(self):
        executor, p = fresh(POSITION_CHECK_SECONDS="100")
        self.addCleanup(p.stop)
        ex = executor.Executor(executor.Config())
        import time as _time
        ex.state["positions"] = [{"mint": MINT, "tokens": 1000, "position_usd": 10.0, "opened_ts": _time.time(), "opened_at": "t",
                                  "peak_usd": 10.0, "buy_signature": "", "copy": WALLET}]
        calls = []
        ex.jup.quote = lambda mint, out, amount, **kw: (calls.append(mint), {"outAmount": str(int(amount * 0.01 / SOL * 1e9))})[1]
        ex.manage_positions(SOL, panic=False)
        ex.manage_positions(SOL, panic=False)
        self.assertEqual(len(calls), 1)
        ex.manage_positions(SOL, panic=True)          # panic ignores the cadence (quote + paper sell quote)
        self.assertGreaterEqual(len(calls), 2)
        p.stop()                                      # drop POSITION_CHECK_SECONDS=100 before testing the default
        executor, p = fresh(COPY_WALLETS="")
        self.addCleanup(p.stop)
        self.assertEqual(executor.Config().position_check_seconds, 0.0)


class PriceFirstTests(unittest.TestCase):
    def make(self):
        executor, p = fresh(POSITION_CHECK_SECONDS="0")
        self.addCleanup(p.stop)
        ex = executor.Executor(executor.Config())
        import time as _time
        self.pos = {"mint": MINT, "tokens": 1000, "position_usd": 10.0, "opened_ts": _time.time(), "opened_at": "t", "peak_usd": 10.0,
                    "buy_signature": "", "copy": WALLET, "entry_tokens": 1000, "entry_basis_usd": 10.0,
                    "ladder": [dict(r, done=False) for r in ex.cfg.copy_ladder]}
        ex.state["positions"] = [self.pos]
        self.quotes = []
        ex.jup.quote = lambda mint, out, amount, **kw: (self.quotes.append(mint), {"outAmount": str(int(amount * self.price / SOL * 1e9))})[1]
        ex.token_prices = lambda mints: {m: (self.price, 0) for m in mints}     # 0 decimals: 1000 tokens
        self.price = 0.01                                                       # $0.01/token = $10 = 1.0x
        return executor, ex

    def test_quiet_position_costs_no_quote(self):
        executor, ex = self.make()
        ex.manage_positions(SOL, panic=False)
        self.assertEqual(self.quotes, [])
        self.assertAlmostEqual(self.pos["last_value_usd"], 10.0)
        self.price = 0.015                                  # +50%: still under the 2x rung and the 75% tp
        ex.manage_positions(SOL, panic=False)
        self.assertEqual(self.quotes, [])
        self.assertAlmostEqual(self.pos["peak_usd"], 15.0)

    def test_near_a_trigger_the_real_quote_decides(self):
        executor, ex = self.make()
        self.price = 0.0195                                 # within 8% of the 2x rung -> quote
        ex.manage_positions(SOL, panic=False)
        self.assertEqual(self.quotes, [MINT])
        self.assertEqual(self.pos["tokens"], 1000)          # quote said 1.95x: rung not reached
        self.price = 0.021
        ex.manage_positions(SOL, panic=False)
        self.assertEqual(self.pos["tokens"], 600)           # rung taken on the quote
        self.price = 0.005                                  # -50%: stop loss via quote
        ex.manage_positions(SOL, panic=False)
        self.assertEqual(ex.state["positions"], [])

    def test_feed_failure_falls_back_to_quotes(self):
        executor, ex = self.make()
        def boom(mints):
            raise RuntimeError("feed down")
        ex.token_prices = boom
        ex.manage_positions(SOL, panic=False)
        self.assertEqual(self.quotes, [MINT])


class SolPriceTests(unittest.TestCase):
    def test_rate_limited_price_quote_keeps_the_last_price(self):
        executor, p = fresh()
        self.addCleanup(p.stop)
        ex = executor.Executor(executor.Config())
        ex.jup.quote = lambda *a, **k: {"outAmount": str(int(150 * 1e6))}
        self.assertEqual(ex.sol_price_usd(), 150.0)
        import requests
        resp = requests.Response(); resp.status_code = 429
        def limited(*a, **k):
            raise requests.HTTPError("429 Client Error: Too Many Requests", response=resp)
        ex.jup.quote = limited
        ex._sol_price = (0, 150.0)                     # cache expired
        self.assertEqual(ex.sol_price_usd(), 150.0)    # stale price instead of an aborted cycle


class LaneTests(unittest.TestCase):
    def test_copy_only_is_the_default_with_wallets(self):
        executor, p = fresh()
        self.addCleanup(p.stop)
        cfg = executor.Config()
        self.assertTrue(cfg.copy_only)
        self.assertEqual(cfg.max_concurrent, 3)                 # copy-only: fewer, bigger positions
        self.assertFalse(cfg.copy_rotate)
        self.assertEqual(cfg.copy_min_buy_usd, 300.0)
        executor, p = fresh(COPY_ONLY="0")
        self.addCleanup(p.stop)
        self.assertFalse(executor.Config().copy_only)
        executor, p = fresh(COPY_WALLETS="")
        self.addCleanup(p.stop)
        self.assertFalse(executor.Config().copy_only)

    def test_copy_only_skips_graduations_and_watchlist(self):
        executor, p = fresh()
        self.addCleanup(p.stop)
        ex = executor.Executor(executor.Config())
        calls = []
        ex.poll_graduations = lambda: calls.append("grad")
        ex.manage_watchlist = lambda price: calls.append("watch")
        ex.poll_copy_wallets = lambda price: calls.append("copy")
        ex.sol_price_usd = lambda: SOL
        ex.state["watchlist"] = [{"mint": "x"}]
        ex.run_cycle()
        self.assertEqual(calls, ["copy"])


class GuardTests(unittest.TestCase):
    def test_copy_entry_skips_lateness_and_cap_bands(self):
        executor, p = fresh()
        self.addCleanup(p.stop)
        cfg = executor.Config()
        late = 3 * 24 * 3600
        self.assertIsNone(executor.entry_guard_reason(cfg, 0, late, 1.0, 12_000_000, copy_trade=True))
        self.assertIsNone(executor.entry_guard_reason(cfg, 0, late, 1.0, 5_000, copy_trade=True))
        self.assertIn("price impact", executor.entry_guard_reason(cfg, 0, late, 40.0, 500_000, copy_trade=True))


class PollTests(unittest.TestCase):
    def make(self, **env):
        executor, p = fresh(**env)
        self.addCleanup(p.stop)
        ex = executor.Executor(executor.Config())
        self.sigs = []
        self.txs = {}
        ex.rpc.call = lambda method, params, timeout=None: list(self.sigs) if method == "getSignaturesForAddress" else None
        ex.rpc.transaction = lambda sig: self.txs.get(sig)
        self.entered = []
        ex.enter_with_retry = lambda item, sol_price: self.entered.append(item)
        return executor, ex

    def test_baseline_then_mirror_new_buys_only(self):
        executor, ex = self.make(COPY_MIN_BUY_USD="50")
        now = int(executor.now_ts())
        self.sigs = [{"signature": "old1", "blockTime": now - 600}]
        ex.poll_copy_wallets(SOL)
        self.assertEqual(self.entered, [])
        self.assertEqual(ex.state["copy_seen"][WALLET], ["old1"])
        self.sigs = [{"signature": "new1", "blockTime": now}, {"signature": "old1", "blockTime": now - 5}]
        self.txs["new1"] = tx(10.0, 5.0, 0, 9_000_000)       # 5 SOL = $500 buy
        ex.state["copy_polled_ts"] = 0
        ex.poll_copy_wallets(SOL)
        self.assertEqual([e["mint"] for e in self.entered], [MINT])
        self.assertEqual(self.entered[0]["copy"], WALLET)
        self.assertEqual(self.entered[0]["copy_buy_usd"], 500)
        ex.state["copy_polled_ts"] = 0
        ex.poll_copy_wallets(SOL)                             # same signatures: nothing new
        self.assertEqual(len(self.entered), 1)

    def test_first_poll_mirrors_a_buy_made_during_restart(self):
        executor, ex = self.make(COPY_MIN_BUY_USD="50")
        now = int(executor.now_ts())
        self.sigs = [{"signature": "fresh", "blockTime": now - 20}, {"signature": "old1", "blockTime": now - 600}]
        self.txs["fresh"] = tx(10.0, 5.0, 0, 9_000_000)
        ex.poll_copy_wallets(SOL)
        self.assertEqual([e["mint"] for e in self.entered], [MINT])
        self.assertIn("old1", ex.state["copy_seen"][WALLET])
        ex.state["copy_polled_ts"] = 0
        ex.poll_copy_wallets(SOL)
        self.assertEqual(len(self.entered), 1)

    def test_usdc_buys_are_sized_in_dollars(self):
        executor, ex = self.make(COPY_MIN_BUY_USD="50")
        now = int(executor.now_ts())
        self.sigs = [{"signature": "base", "blockTime": now - 600}]
        ex.poll_copy_wallets(SOL)
        t = tx(10.0, 9.99999, 0, 4_000_000)                 # only the fee in SOL...
        usdc = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
        t["meta"]["preTokenBalances"].append({"owner": WALLET, "mint": usdc, "uiTokenAmount": {"amount": str(250_000_000)}})
        t["meta"]["postTokenBalances"].append({"owner": WALLET, "mint": usdc, "uiTokenAmount": {"amount": str(150_000_000)}})
        self.txs["u1"] = t                                    # ...and 100 USDC for the token
        self.sigs = [{"signature": "u1", "blockTime": now}, {"signature": "base", "blockTime": now - 600}]
        ex.state["copy_polled_ts"] = 0
        ex.poll_copy_wallets(SOL)
        self.assertEqual(len(self.entered), 1)
        self.assertAlmostEqual(self.entered[0]["copy_buy_usd"], 100.001, places=2)

    def test_small_and_stale_buys_are_ignored(self):
        executor, ex = self.make(COPY_MIN_BUY_USD="300")
        now = int(executor.now_ts())
        self.sigs = [{"signature": "base", "blockTime": now}]
        ex.poll_copy_wallets(SOL)
        self.sigs = [{"signature": "small", "blockTime": now}, {"signature": "stale", "blockTime": now - 600}, {"signature": "base", "blockTime": now}]
        self.txs["small"] = tx(10.0, 9.0, 0, 1_000)           # $100
        self.txs["stale"] = tx(10.0, 0.0, 0, 1_000)           # $1000 but 10 minutes old
        ex.state["copy_polled_ts"] = 0
        ex.poll_copy_wallets(SOL)
        self.assertEqual(self.entered, [])

    def test_per_wallet_minimum_overrides_the_default(self):
        executor, ex = self.make(COPY_WALLETS=f"{WALLET}:400, {OTHER}", COPY_MIN_BUY_USD="50")
        self.assertEqual(ex.cfg.copy_wallets, (WALLET, OTHER))
        self.assertEqual(ex.cfg.copy_wallet_min_usd, {WALLET: 400.0})
        now = int(executor.now_ts())
        self.sigs = [{"signature": "base", "blockTime": now - 600}]
        ex.poll_copy_wallets(SOL)
        self.txs["b1"] = tx(10.0, 7.0, 0, 1_000)             # $300: under this wallet's $400 floor
        self.sigs = [{"signature": "b1", "blockTime": now}, {"signature": "base", "blockTime": now - 600}]
        ex.state["copy_polled_ts"] = 0
        ex.poll_copy_wallets(SOL)
        self.assertEqual(self.entered, [])
        self.txs["b2"] = tx(10.0, 5.0, 0, 1_000)             # $500: mirrored
        self.sigs = [{"signature": "b2", "blockTime": now}] + self.sigs
        ex.state["copy_polled_ts"] = 0
        ex.poll_copy_wallets(SOL)
        self.assertEqual([e["copy_buy_usd"] for e in self.entered], [500])
        self.assertEqual(executor.parse_wallet_list("a:$1k; b\nc:250")[1], {"a": 1000.0, "c": 250.0})

    def test_follow_sell_closes_our_copy(self):
        executor, ex = self.make()
        now = int(executor.now_ts())
        self.sigs = [{"signature": "base", "blockTime": now}]
        ex.poll_copy_wallets(SOL)
        ex.state["positions"] = [{"mint": MINT, "tokens": 10, "position_usd": 5.0, "opened_ts": now, "copy": WALLET, "peak_usd": 5.0}]
        closed = []
        ex.close_position = lambda pos, reason, sol_price: closed.append((pos["mint"], reason))
        self.sigs = [{"signature": "sell1", "blockTime": now}, {"signature": "base", "blockTime": now}]
        self.txs["sell1"] = tx(5.0, 9.0, 9_000_000, 0)
        ex.state["copy_polled_ts"] = 0
        ex.poll_copy_wallets(SOL)
        self.assertEqual(closed, [(MINT, "copy_sell")])

    def test_follow_sell_closes_any_position_in_that_coin(self):
        executor, ex = self.make()
        now = int(executor.now_ts())
        self.sigs = [{"signature": "base", "blockTime": now}]
        ex.poll_copy_wallets(SOL)
        ex.state["positions"] = [{"mint": MINT, "tokens": 10, "position_usd": 5.0, "opened_ts": now, "peak_usd": 5.0},   # graduation entry
                                 {"mint": "Other", "tokens": 10, "position_usd": 5.0, "opened_ts": now, "peak_usd": 5.0, "copy": WALLET}]
        closed = []
        ex.close_position = lambda pos, reason, sol_price: closed.append((pos["mint"], reason))
        self.sigs = [{"signature": "sell1", "blockTime": now}, {"signature": "base", "blockTime": now}]
        self.txs["sell1"] = tx(5.0, 9.0, 9_000_000, 0)
        ex.state["copy_polled_ts"] = 0
        ex.poll_copy_wallets(SOL)
        self.assertEqual(closed, [(MINT, "copy_sell")])

    def test_copy_positions_use_copy_exits(self):
        executor, ex = self.make(COPY_TAKE_PROFIT="0.5", TAKE_PROFIT="0.75", COPY_LADDER="")
        self.assertEqual(ex.exit_cfg({"mint": "x", "copy": WALLET}).take_profit, 0.5)
        self.assertEqual(ex.exit_cfg({"mint": "x"}).take_profit, 0.75)
        ladder = [{"x": 2, "pct": 40, "done": False}, {"x": 5, "pct": 60, "done": False}]
        self.assertEqual(ex.exit_cfg({"mint": "x", "copy": WALLET, "ladder": ladder}).take_profit, 4.0)


class RotateTests(unittest.TestCase):
    def make(self, **env):
        env.setdefault("MAX_CONCURRENT_POSITIONS", "2")
        env.setdefault("COPY_ONLY", "0")          # all lanes: adopted leftovers hold no slot
        env.setdefault("COPY_ROTATE_MIN_AGE_MINUTES", "0")
        env.setdefault("COPY_ROTATE", "1")
        executor, p = fresh(**env)
        self.addCleanup(p.stop)
        ex = executor.Executor(executor.Config())
        now = executor.now_ts()
        ex.state["positions"] = [
            {"mint": "Newer", "tokens": 1, "position_usd": 5.0, "opened_ts": now - 60, "opened_at": "b", "peak_usd": 5.0},
            {"mint": "Oldest", "tokens": 1, "position_usd": 5.0, "opened_ts": now - 600, "opened_at": "a", "peak_usd": 5.0},
            {"mint": "Bag", "tokens": 1, "position_usd": 2.0, "opened_ts": now - 9000, "opened_at": "z", "peak_usd": 2.0, "adopted": True},
        ]
        self.closed = []
        def close(pos, reason, sol_price):
            self.closed.append((pos["mint"], reason))
            ex.state["positions"].remove(pos)
        ex.close_position = close
        return executor, ex

    def test_sells_oldest_chosen_position_when_full(self):
        executor, ex = self.make()
        self.assertTrue(ex.rotate_for_copy("New", SOL))
        self.assertEqual(self.closed, [("Oldest", "rotate")])
        self.assertEqual([p["mint"] for p in ex.state["positions"]], ["Newer", "Bag"])

    def test_no_rotation_with_a_free_slot_or_when_disabled_or_after_daily_loss(self):
        executor, ex = self.make()
        ex.state["positions"].pop(0)
        self.assertTrue(ex.rotate_for_copy("New", SOL))
        self.assertEqual(self.closed, [])
        executor, ex = self.make(COPY_ROTATE="0")
        self.assertFalse(ex.rotate_for_copy("New", SOL))
        self.assertEqual(self.closed, [])
        executor, ex = self.make(DAILY_LOSS_LIMIT_USD="10")
        ex.state["daily"]["realized_pnl_usd"] = -12.0
        self.assertFalse(ex.rotate_for_copy("New", SOL))
        self.assertEqual(self.closed, [])

    def test_copy_only_counts_adopted_positions_and_rotates_them_first(self):
        executor, ex = self.make(MAX_CONCURRENT_POSITIONS="3", COPY_ONLY="1")   # 3 held including the adopted bag
        self.assertTrue(ex.cfg.copy_only)
        self.assertTrue(ex.rotate_for_copy("New", SOL))
        self.assertEqual(self.closed, [("Bag", "rotate")])
        self.assertEqual(ex.exit_cfg({"mint": "x", "adopted": True}).time_stop_minutes, ex.cfg.copy_time_stop_minutes)

    def test_young_positions_are_not_rotated_out(self):
        executor, ex = self.make(COPY_ROTATE_MIN_AGE_MINUTES="30")   # Oldest is 10 minutes old
        self.assertFalse(ex.rotate_for_copy("New", SOL))
        self.assertEqual(self.closed, [])
        executor, ex = self.make(COPY_ROTATE_MIN_AGE_MINUTES="5")
        self.assertTrue(ex.rotate_for_copy("New", SOL))
        self.assertEqual(self.closed, [("Oldest", "rotate")])

    def test_failed_sale_skips_the_new_buy(self):
        executor, ex = self.make()
        def boom(pos, reason, sol_price):
            raise RuntimeError("rpc down")
        ex.close_position = boom
        self.assertFalse(ex.rotate_for_copy("New", SOL))
        self.assertEqual(len(ex.state["positions"]), 3)


class LadderTests(unittest.TestCase):
    def make(self, **env):
        env.setdefault("POSITION_CHECK_SECONDS", "0")     # these tests step the price between calls
        executor, p = fresh(**env)
        self.addCleanup(p.stop)
        ex = executor.Executor(executor.Config())
        return executor, ex

    def test_parse(self):
        executor, ex = self.make()
        self.assertEqual(executor.parse_sell_ladder("5:30, 2x:40%,3:30,bad,0.5:10"),
                         [{"x": 2.0, "pct": 40.0}, {"x": 3.0, "pct": 30.0}, {"x": 5.0, "pct": 30.0}])
        self.assertEqual(executor.parse_sell_ladder(""), [])
        self.assertEqual([(r["x"], r["pct"]) for r in ex.cfg.copy_ladder], [(2.0, 40.0), (3.0, 30.0), (5.0, 30.0)])

    def position(self, ex, tokens=1000, basis=10.0):
        import time as _time
        pos = {"mint": MINT, "tokens": tokens, "position_usd": basis, "opened_ts": _time.time(), "opened_at": "t", "peak_usd": basis,
               "buy_signature": "", "copy": WALLET, "entry_tokens": tokens, "entry_basis_usd": basis,
               "ladder": [dict(r, done=False) for r in ex.cfg.copy_ladder]}
        ex.state["positions"] = [pos]
        return pos

    def test_phases_out_at_each_rung_and_closes_at_the_top(self):
        executor, ex = self.make()
        pos = self.position(ex)                     # 1000 tokens for $10: entry price $0.01
        price = {"v": 0.01}
        # quote: `amount` tokens -> lamports at the current price with SOL = $100
        ex.jup.quote = lambda mint, out, amount, **kw: {"outAmount": str(int(amount * price["v"] / SOL * 1e9))}
        ex.manage_positions(SOL, panic=False)       # 1.0x: nothing
        self.assertEqual(pos["tokens"], 1000)
        price["v"] = 0.021                          # 2.1x: first rung, sell 40% of entry tokens
        ex.manage_positions(SOL, panic=False)
        self.assertEqual(pos["tokens"], 600)
        self.assertTrue(pos["ladder"][0]["done"])
        self.assertAlmostEqual(ex.state["daily"]["realized_pnl_usd"], 400 * 0.021 - 4.0, places=6)
        price["v"] = 0.035                          # 3.5x: second rung, 30% of entry tokens = 300 more
        ex.manage_positions(SOL, panic=False)
        self.assertEqual(pos["tokens"], 300)
        closed = []
        ex.close_position = lambda p, reason, sol_price: closed.append(reason)
        price["v"] = 0.06                           # 6x: top rung closes the rest (moon bag inside close)
        ex.manage_positions(SOL, panic=False)
        self.assertEqual(closed, ["ladder_5x"])

    def test_jump_past_two_rungs_takes_both(self):
        executor, ex = self.make()
        pos = self.position(ex)
        ex.jup.quote = lambda mint, out, amount, **kw: {"outAmount": str(int(amount * 0.033 / SOL * 1e9))}  # 3.3x at once
        ex.manage_positions(SOL, panic=False)
        self.assertEqual(pos["tokens"], 300)        # 40% + 30% of the entry tokens sold together
        self.assertEqual([r["done"] for r in pos["ladder"]], [True, True, False])


if __name__ == "__main__":
    unittest.main()
