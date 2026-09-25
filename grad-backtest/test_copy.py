"""Copy mode: mirror the buys of chosen wallets, decoded from their transactions' balance
changes, and follow their sells."""
import importlib
import os
import tempfile
import unittest
from unittest import mock

SOL = 100.0
WALLET = "FY6yG7cy886yAzndYue7Tb5q5mj3nWEHNi3PjGowd4Ns"   # any real-shaped (32-byte base58) address
OTHER = "7u45jUVMBMGWe8Qw3ia1bCbDb1HkordapADgJc6kNaP4"
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
        self.assertFalse(buy["held_before"])
        self.assertTrue(executor.wallet_swap_from_transaction(tx(10.0, 8.99, 100, 5_000_100), WALLET)["held_before"])
        sell = executor.wallet_swap_from_transaction(tx(8.99, 9.8, 5_000_000, 1_000_000), WALLET)
        self.assertEqual(sell["side"], "sell")
        self.assertEqual(sell["tokens"], 4_000_000)
        self.assertAlmostEqual(sell["fraction"], 0.8)
        self.assertEqual(executor.wallet_swap_from_transaction(tx(8.99, 9.8, 5_000_000, 0), WALLET)["fraction"], 1.0)
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
        executor, p = fresh(POSITION_CHECK_SECONDS="0", COPY_LADDER="2:40,3:30,5:30")   # the price steps below assume these rungs
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


class DeploymentCapTests(unittest.TestCase):
    def test_size_is_a_share_of_the_whole_account_and_stops_at_the_cap(self):
        executor, p = fresh(PAPER_BALANCE_USD="40", MAX_POSITION_USD="20")   # $40 free; sizing formula, not the funnel cap
        self.addCleanup(p.stop)
        ex = executor.Executor(executor.Config())
        ex.state["paper_balance_usd"] = 40.0
        import time as _time
        ex.state["positions"] = [{"mint": f"m{i}", "tokens": 1, "position_usd": 8.0, "last_value_usd": 8.0, "opened_ts": _time.time(),
                                  "opened_at": "t", "peak_usd": 8.0, "copy": WALLET} for i in range(5)]   # $40 deployed
        self.assertAlmostEqual(ex.equity_usd(SOL), 80.0)          # free + positions
        entered = []
        ex.jup.quote = lambda *a, **k: (entered.append(a), {"outAmount": "1000000", "priceImpactPct": "0.01"})[1]
        ex.skip = lambda mint, reason: entered.append(("skip", reason))
        ex.try_enter({"mint": MINT, "graduated_ts": _time.time(), "enter_at": _time.time(), "copy": WALLET, "copy_buy_usd": 500}, SOL)
        reasons = [e[1] for e in entered if isinstance(e, tuple) and e and e[0] == "skip"]
        self.assertFalse(any("deployment cap" in r for r in reasons), reasons)   # $40 + $6.40 < 80% of $80: the cap allows it
        ex.state["positions"].append({"mint": "m9", "tokens": 1, "position_usd": 70.0, "last_value_usd": 70.0, "opened_ts": _time.time(),
                                      "opened_at": "t", "peak_usd": 70.0, "copy": WALLET})              # $110 deployed of $150: 73%, +8% crosses 80%
        entered.clear()
        ex.try_enter({"mint": "Other", "graduated_ts": _time.time(), "enter_at": _time.time(), "copy": WALLET, "copy_buy_usd": 500}, SOL)
        skips = [e for e in entered if isinstance(e, tuple) and e and e[0] == "skip"]
        self.assertTrue(skips and "deployment cap" in skips[0][1], entered)


class WalletSizeTests(unittest.TestCase):
    def test_a_wallet_copied_at_half_size_gets_half_the_position(self):
        executor, p = fresh(PAPER_BALANCE_USD="200", COPY_WALLETS=f"{WALLET}:300:0.5, {OTHER}", MAX_POSITION_USD="20")
        self.addCleanup(p.stop)
        ex = executor.Executor(executor.Config())
        ex.state["paper_balance_usd"] = 200.0                     # $200 equity: 8% is $16
        import time as _time
        quotes = []
        ex.jup.quote = lambda *a, **k: (quotes.append(a), {"outAmount": "1000000", "priceImpactPct": "0.01"})[1]
        ex.skip = lambda mint, reason: quotes.append(("skip", reason))
        ex.try_enter({"mint": MINT, "graduated_ts": _time.time(), "enter_at": _time.time(), "copy": WALLET,
                      "copy_buy_usd": 500, "copy_size": ex.cfg.copy_wallet_size[WALLET]}, SOL)
        self.assertAlmostEqual(quotes[0][2] / 1e9 * SOL, 8.0, places=2)      # half of $16
        quotes.clear()
        ex.try_enter({"mint": "Other", "graduated_ts": _time.time(), "enter_at": _time.time(), "copy": OTHER,
                      "copy_buy_usd": 500, "copy_size": ex.cfg.copy_wallet_size.get(OTHER, 1.0)}, SOL)
        self.assertAlmostEqual(quotes[0][2] / 1e9 * SOL, 16.0, places=2)     # the usual size
        ex.state["paper_balance_usd"] = 80.0                      # $80 equity: half of $6.40 is under the $5 minimum
        quotes.clear()
        ex.try_enter({"mint": "Third", "graduated_ts": _time.time(), "enter_at": _time.time(), "copy": WALLET,
                      "copy_buy_usd": 500, "copy_size": 0.5}, SOL)
        self.assertAlmostEqual(quotes[0][2] / 1e9 * SOL, ex.cfg.min_position_usd, places=2)

    def test_poll_passes_the_wallet_size_along(self):
        executor, p = fresh(COPY_WALLETS=f"{WALLET}:50:0.5")
        self.addCleanup(p.stop)
        ex = executor.Executor(executor.Config())
        now = int(executor.now_ts())
        sigs = [{"signature": "base", "blockTime": now - 600}]
        txs = {}
        ex.rpc.call = lambda method, params, timeout=None: list(sigs) if method == "getSignaturesForAddress" else None
        ex.rpc.transaction = lambda sig: txs.get(sig)
        entered = []
        ex.enter_with_retry = lambda item, sol_price: entered.append(item)
        ex.poll_copy_wallets(SOL)
        sigs.insert(0, {"signature": "buy", "blockTime": now})
        txs["buy"] = tx(10.0, 5.0, 0, 9_000_000)
        ex.state["copy_polled_ts"] = 0
        ex.poll_copy_wallets(SOL)
        self.assertEqual([(e["mint"], e["copy_size"]) for e in entered], [(MINT, 0.5)])


class BuiltinDefaultsTests(unittest.TestCase):
    """Settings that live in code so nothing has to be edited in Railway."""
    BIG = "498g1rVnFcnjBjpfw1xyqA1WvgQXUU8RWuELjxkjAayQ"

    def test_known_wallet_gets_its_terms_without_any_in_the_variable(self):
        executor, p = fresh(COPY_WALLETS=f"{self.BIG}, {WALLET}")
        self.addCleanup(p.stop)
        cfg = executor.Config()
        self.assertEqual((cfg.copy_wallet_min_usd[self.BIG], cfg.copy_wallet_size[self.BIG]), (300.0, 1.0))
        self.assertNotIn(WALLET, cfg.copy_wallet_size)
        self.assertEqual(cfg.copy_gmgn_gate, "shadow")          # configured wallets are always copied; GMGN only logs

    def test_terms_written_in_the_variable_still_win(self):
        executor, p = fresh(COPY_WALLETS=f"{self.BIG}:500:0.25")
        self.addCleanup(p.stop)
        cfg = executor.Config()
        self.assertEqual((cfg.copy_wallet_min_usd[self.BIG], cfg.copy_wallet_size[self.BIG]), (500.0, 0.25))

    def test_terms_only_apply_to_a_followed_wallet(self):
        executor, p = fresh(COPY_WALLETS=WALLET)
        self.addCleanup(p.stop)
        self.assertNotIn(self.BIG, executor.Config().copy_wallet_min_usd)


class WalletValidationTests(unittest.TestCase):
    def test_mistyped_address_is_dropped_at_startup(self):
        executor, p = fresh(COPY_WALLETS=f"9BMzTpSoShort, {WALLET}:300")
        self.addCleanup(p.stop)
        cfg = executor.Config()
        self.assertEqual(cfg.copy_wallets, (WALLET,))
        self.assertEqual(cfg.copy_wallet_min_usd, {WALLET: 300.0})
        self.assertTrue(executor.valid_solana_address(WALLET))
        self.assertFalse(executor.valid_solana_address("9BMzTpSoShort"))

    def test_small_account_trades_at_the_minimum_position(self):
        executor, p = fresh(PAPER_BALANCE_USD="45")
        self.addCleanup(p.stop)
        ex = executor.Executor(executor.Config())
        ex.state["paper_balance_usd"] = 45.0                      # 8% of $45 is $3.60: trade the $5 minimum instead
        quotes, skipped = [], []
        ex.jup.quote = lambda *a, **k: (quotes.append(a), {"outAmount": str(a[2]), "priceImpactPct": "0.01"})[1]   # 1:1 both ways
        ex.skip = lambda mint, reason: skipped.append(reason)
        import time as _time
        ex.try_enter({"mint": MINT, "graduated_ts": _time.time(), "enter_at": _time.time(), "copy": WALLET, "copy_buy_usd": 500}, SOL)
        self.assertEqual(skipped, [])
        self.assertAlmostEqual(quotes[0][2] / 1e9 * SOL, 5.0, places=2)
        self.assertEqual(executor.position_size_usd(ex.cfg, 45.0, 0, 0.0), 5.0)
        self.assertEqual(executor.position_size_usd(ex.cfg, 6.0, 0, 0.0), 0.0)      # $5 does not fit in 80% of $6
        ex.state["positions"] = []
        ex.state["paper_balance_usd"] = 6.0
        quotes.clear()
        ex.try_enter({"mint": "Other", "graduated_ts": _time.time(), "enter_at": _time.time(), "copy": WALLET, "copy_buy_usd": 500}, SOL)
        self.assertIn("minimum position does not fit", skipped[0])


class PanicOnStartTests(unittest.TestCase):
    def test_panic_variable_raises_the_flag_at_boot(self):
        executor, p = fresh(PANIC="1")
        self.addCleanup(p.stop)
        ex = executor.Executor(executor.Config())
        executor.PANIC_FLAG.unlink(missing_ok=True)
        ex.apply_startup_flags()
        self.assertTrue(executor.PANIC_FLAG.exists())
        # Panic completes only once the wallet reconcile is no longer pending, so adopted
        # leftovers are sold too.
        ex.state["positions"] = []
        ex._reconcile_pending = True
        ex.rpc.call = lambda *a, **k: None
        ex.run_cycle()
        self.assertTrue(executor.PANIC_FLAG.exists())
        ex._reconcile_pending = False
        ex.run_cycle()
        self.assertFalse(executor.PANIC_FLAG.exists())
        self.assertTrue(executor.STOP_FLAG.exists())
        executor, p = fresh(PANIC="0")
        self.addCleanup(p.stop)
        self.assertFalse(executor.Config().panic_on_start)


class LaneTests(unittest.TestCase):
    def test_copy_only_is_the_default_with_wallets(self):
        executor, p = fresh()
        self.addCleanup(p.stop)
        cfg = executor.Config()
        self.assertTrue(cfg.copy_only)
        self.assertEqual(cfg.max_concurrent, 15)                # copy-only: 8% of the account, fifteen at once
        self.assertAlmostEqual(cfg.account_fraction, 0.08)
        self.assertAlmostEqual(cfg.max_deployed_fraction, 0.80)
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
        ex.poll_copy_wallets = lambda price, allow_buys=True: calls.append(("copy", allow_buys))
        ex.sol_price_usd = lambda: SOL
        ex.state["watchlist"] = [{"mint": "x"}]
        ex.run_cycle()
        self.assertEqual(calls, [("copy", True)])
        # Draining still polls the followed wallets (their sells are followed) but allows no buys.
        executor.STOP_FLAG.touch()
        try:
            calls.clear()
            ex.run_cycle()
            self.assertEqual(calls, [("copy", False)])
        finally:
            executor.STOP_FLAG.unlink(missing_ok=True)


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
        ex.rpc.call = lambda method, params, timeout=None: list(self.sigs)[:params[1]["limit"]] if method == "getSignaturesForAddress" else None
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

    def test_burst_of_trades_between_polls_is_fully_processed(self):
        """Beqv-style bursts: more than ten trades land between two polls; every big buy in the
        burst must still be mirrored (the poll reads a 100-signature window, not 10)."""
        executor, ex = self.make(COPY_MIN_BUY_USD="300")
        now = int(executor.now_ts())
        self.sigs = [{"signature": "old1", "blockTime": now - 600}]
        ex.poll_copy_wallets(SOL)
        burst = [{"signature": f"b{i}", "blockTime": now - 30 + i} for i in range(25)]
        self.sigs = list(reversed(burst)) + [{"signature": "old1", "blockTime": now - 600}]
        for i in range(25):
            sol_after = 5.0 if i in (0, 12, 24) else 9.9                # three $500 buys among $10 clips
            self.txs[f"b{i}"] = tx(10.0, sol_after, 0, 9_000_000, mint=f"Mint{i:02d}" + "1" * 38)
        ex.state["copy_polled_ts"] = 0
        ex.poll_copy_wallets(SOL)
        self.assertEqual(len(self.entered), 3)
        self.assertEqual(sum(1 for s in ex.state["copy_seen"][WALLET] if s.startswith("b")), 25)

    def test_only_first_buys_are_mirrored_and_every_signal_is_recorded(self):
        executor, ex = self.make(COPY_MIN_BUY_USD="50")
        now = int(executor.now_ts())
        self.sigs = [{"signature": "old1", "blockTime": now - 600}]
        ex.poll_copy_wallets(SOL)
        mint_add, mint_dust, mint_first = ("Add" + "1" * 39, "Dust" + "1" * 38, "First" + "1" * 37)
        self.sigs = [{"signature": "b_first", "blockTime": now}, {"signature": "b_dust", "blockTime": now},
                     {"signature": "b_add", "blockTime": now}, {"signature": "old1", "blockTime": now - 600}]
        self.txs["b_add"] = tx(10.0, 5.0, 9_000_000, 10_000_000, mint=mint_add)      # held 9M, bought 1M: an add
        self.txs["b_dust"] = tx(10.0, 5.0, 10_000, 1_010_000, mint=mint_dust)        # 1% dust left over: a first buy
        self.txs["b_first"] = tx(10.0, 5.0, 0, 1_000_000, mint=mint_first)
        ex.state["copy_polled_ts"] = 0
        ex.poll_copy_wallets(SOL)
        self.assertEqual(sorted(e["mint"] for e in self.entered), sorted([mint_dust, mint_first]))
        import csv
        rows = list(csv.DictReader(open(executor.SIGNALS_FILE)))
        statuses = [(r["mint"], r["status"], r["reason"]) for r in rows]
        self.assertIn((mint_add, "blocked", "add"), statuses)
        self.assertIn((mint_first, "attempted", "mirrored"), statuses)
        self.assertIn((mint_first, "failed", "entry_failed"), statuses)     # entry is mocked away: no position appeared
        self.assertEqual(next(r for r in rows if r["mint"] == mint_add)["pre_pct"], "900.0")
        self.assertTrue(all(r["signal_id"].startswith(WALLET[:8] + ":") for r in rows))
        # With the rule off, adds are copied too.
        executor2, ex2 = self.make(COPY_MIN_BUY_USD="50", COPY_FIRST_BUY_ONLY="0")
        self.sigs = [{"signature": "old1", "blockTime": now - 600}]
        ex2.poll_copy_wallets(SOL)
        self.sigs = [{"signature": "b_add", "blockTime": now}, {"signature": "old1", "blockTime": now - 600}]
        self.txs["b_add"] = tx(10.0, 5.0, 9_000_000, 10_000_000, mint=mint_add)
        ex2.state["copy_polled_ts"] = 0
        ex2.poll_copy_wallets(SOL)
        self.assertEqual([e["mint"] for e in self.entered], [mint_add])

    def test_entry_exception_reason_reaches_the_signal_record(self):
        executor, ex = self.make(COPY_MIN_BUY_USD="50")
        now = int(executor.now_ts())
        self.sigs = [{"signature": "base", "blockTime": now - 600}]
        ex.poll_copy_wallets(SOL)
        def boom(item, sol_price):
            raise RuntimeError("Jupiter 429 rate limited")
        ex.try_enter = boom
        ex.cfg.entry_retries = 0
        del ex.enter_with_retry                                          # use the real retry wrapper
        self.sigs = [{"signature": "b1", "blockTime": now}] + self.sigs
        self.txs["b1"] = tx(10.0, 5.0, 0, 9_000_000)
        ex.state["copy_polled_ts"] = 0
        with mock.patch.object(executor.time, "sleep"):
            ex.poll_copy_wallets(SOL)
        import csv
        rows = list(csv.DictReader(open(executor.SIGNALS_FILE)))
        self.assertEqual(rows[-1]["status"], "failed")
        self.assertIn("Jupiter 429", rows[-1]["reason"])

    def test_old_sells_are_still_followed_but_old_buys_are_not(self):
        executor, ex = self.make()
        now = int(executor.now_ts())
        self.sigs = [{"signature": "base", "blockTime": now - 600}]
        ex.poll_copy_wallets(SOL)
        ex.state["positions"] = [{"mint": MINT, "tokens": 10, "position_usd": 5.0, "opened_ts": now - 1800, "copy": WALLET,
                                  "peak_usd": 5.0, "buy_signature": "ours"}]
        closed = []
        ex.close_position = lambda pos, reason, sol_price: closed.append(reason)
        self.sigs = [{"signature": "oldsell", "blockTime": now - 900}, {"signature": "oldbuy", "blockTime": now - 900}] + self.sigs
        self.txs["oldsell"] = tx(5.0, 9.0, 9_000_000, 0)
        self.txs["oldbuy"] = tx(10.0, 5.0, 0, 9_000_000, mint="Other" + "1" * 37)
        ex.state["copy_polled_ts"] = 0
        ex.poll_copy_wallets(SOL)
        self.assertEqual(closed, ["copy_sell"])             # a 15-minute-old sell still closes ours
        self.assertEqual(self.entered, [])                   # a 15-minute-old buy is not chased

    def test_transient_decode_failure_is_retried_not_dropped(self):
        executor, ex = self.make(COPY_MIN_BUY_USD="50")
        now = int(executor.now_ts())
        self.sigs = [{"signature": "base", "blockTime": now - 600}]
        ex.poll_copy_wallets(SOL)
        self.sigs = [{"signature": "flaky", "blockTime": now}] + self.sigs
        calls = {"n": 0}
        def transaction(sig):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("429 rate limited")
            return tx(10.0, 5.0, 0, 9_000_000)
        ex.rpc.transaction = transaction
        ex.state["copy_polled_ts"] = 0
        ex.poll_copy_wallets(SOL)
        self.assertEqual(self.entered, [])
        self.assertIn("flaky", ex.state["copy_unresolved"])            # durable, with a retry time
        self.assertEqual(ex.state["copy_unresolved"]["flaky"]["attempts"], 1)
        ex.state["copy_polled_ts"] = 0
        ex.poll_copy_wallets(SOL)
        self.assertEqual(self.entered, [])                              # not due yet (backoff)
        ex.state["copy_unresolved"]["flaky"]["next_ts"] = 0
        ex.state["copy_polled_ts"] = 0
        ex.poll_copy_wallets(SOL)
        self.assertEqual([e["mint"] for e in self.entered], [MINT])
        self.assertNotIn("flaky", ex.state["copy_unresolved"])

    def test_missing_transaction_is_unresolved_then_parked_as_failed(self):
        executor, ex = self.make(COPY_MAX_DECODE_ATTEMPTS="3")
        now = int(executor.now_ts())
        self.sigs = [{"signature": "base", "blockTime": now - 600}]
        ex.poll_copy_wallets(SOL)
        self.sigs = [{"signature": "ghost", "blockTime": now}] + self.sigs      # rpc returns None for it
        for _ in range(3):
            ex.state["copy_polled_ts"] = 0
            for e in ex.state.get("copy_unresolved", {}).values():
                e["next_ts"] = 0
            ex.poll_copy_wallets(SOL)
        self.assertNotIn("ghost", ex.state.get("copy_unresolved", {}))
        self.assertEqual([f["signature"] for f in ex.state["copy_failed"]], ["ghost"])
        self.assertEqual(ex.state["copy_failed"][0]["attempts"], 3)

    def test_pages_back_after_an_outage(self):
        executor, ex = self.make(COPY_MIN_BUY_USD="50")
        now = int(executor.now_ts())
        self.sigs = [{"signature": "base", "blockTime": now - 600}]
        ex.poll_copy_wallets(SOL)
        burst = [{"signature": f"n{i:03d}", "blockTime": now - i // 2} for i in range(130)]   # 130 unseen in 65s, newest first
        pages = {None: burst[:100], "n099": burst[100:] + [{"signature": "base", "blockTime": now - 600}]}
        def call(method, params, timeout=None):
            self.assertEqual(method, "getSignaturesForAddress")
            return list(pages[params[1].get("before")])
        ex.rpc.call = call
        self.txs["n129"] = tx(10.0, 5.0, 0, 9_000_000)                # the oldest of the burst is a $500 buy
        ex.state["copy_polled_ts"] = 0
        ex.poll_copy_wallets(SOL)
        self.assertIn("n129", ex.state["copy_seen"][WALLET])
        self.assertEqual([e["mint"] for e in self.entered], [MINT])

    def test_sell_scope_source_leaves_other_positions_to_their_own_exits(self):
        executor, ex = self.make(COPY_SELL_SCOPE="source")
        now = int(executor.now_ts())
        self.sigs = [{"signature": "base", "blockTime": now}]
        ex.poll_copy_wallets(SOL)
        ex.state["positions"] = [{"mint": MINT, "tokens": 10, "position_usd": 5.0, "opened_ts": now, "peak_usd": 5.0, "adopted": True},
                                 {"mint": "Other" + "1" * 37, "tokens": 10, "position_usd": 5.0, "opened_ts": now, "peak_usd": 5.0, "copy": OTHER}]
        closed = []
        ex.close_position = lambda pos, reason, sol_price: closed.append(pos["mint"])
        self.sigs = [{"signature": "sell1", "blockTime": now}, {"signature": "base", "blockTime": now}]
        self.txs["sell1"] = tx(5.0, 9.0, 9_000_000, 0)
        ex.state["copy_polled_ts"] = 0
        ex.poll_copy_wallets(SOL)
        self.assertEqual(closed, [])                          # WALLET is not the source of either position

    def test_gmgn_gate_shadow_then_enforce(self):
        executor, ex = self.make(COPY_MIN_BUY_USD="50", COPY_GMGN_GATE="shadow")
        now = int(executor.now_ts())
        self.sigs = [{"signature": "base", "blockTime": now - 600}]
        ex.poll_copy_wallets(SOL)
        ex.state["gmgn_verdicts"] = {WALLET: {"verdict": "skip", "why": "lost $800 in 30d", "evaluated_at": executor.utc_iso()}}
        self.sigs = [{"signature": "b1", "blockTime": now}] + self.sigs
        self.txs["b1"] = tx(10.0, 5.0, 0, 9_000_000)
        ex.state["copy_polled_ts"] = 0
        ex.poll_copy_wallets(SOL)
        self.assertEqual(len(self.entered), 1)                # shadow: mirrored anyway
        import csv
        rows = list(csv.DictReader(open(executor.SIGNALS_FILE)))
        self.assertEqual(rows[-1]["gmgn"], "skip")
        ex.cfg.copy_gmgn_gate = "enforce"
        self.sigs = [{"signature": "b2", "blockTime": now}] + self.sigs
        self.txs["b2"] = tx(10.0, 5.0, 0, 9_000_000, mint="Other" + "1" * 37)
        ex.state["copy_polled_ts"] = 0
        ex.poll_copy_wallets(SOL)
        self.assertEqual(len(self.entered), 1)                # enforce: blocked
        rows = list(csv.DictReader(open(executor.SIGNALS_FILE)))
        self.assertEqual((rows[-1]["status"], rows[-1]["reason"]), ("blocked", "gmgn_skip"))
        # A stale verdict is treated as unknown even in enforce mode.
        ex.state["gmgn_verdicts"][WALLET]["evaluated_at"] = "2026-01-01T00:00:00Z"
        self.sigs = [{"signature": "b3", "blockTime": now}] + self.sigs
        self.txs["b3"] = tx(10.0, 5.0, 0, 9_000_000, mint="Third" + "1" * 37)
        ex.state["copy_polled_ts"] = 0
        ex.poll_copy_wallets(SOL)
        self.assertEqual(len(self.entered), 2)

    def test_draining_follows_sells_but_opens_nothing(self):
        executor, ex = self.make(COPY_MIN_BUY_USD="50")
        now = int(executor.now_ts())
        self.sigs = [{"signature": "base", "blockTime": now - 600}]
        ex.poll_copy_wallets(SOL)
        ex.state["positions"] = [{"mint": MINT, "tokens": 10, "position_usd": 5.0, "opened_ts": now - 600, "copy": WALLET,
                                  "peak_usd": 5.0, "buy_signature": "ours"}]
        closed = []
        ex.close_position = lambda pos, reason, sol_price: closed.append(reason)
        self.sigs = [{"signature": "sell1", "blockTime": now}, {"signature": "buy1", "blockTime": now}] + self.sigs
        self.txs["sell1"] = tx(5.0, 9.0, 9_000_000, 0)
        self.txs["buy1"] = tx(10.0, 5.0, 0, 9_000_000, mint="Other" + "1" * 37)
        ex.state["copy_polled_ts"] = 0
        ex.poll_copy_wallets(SOL, allow_buys=False)
        self.assertEqual(closed, ["copy_sell"])
        self.assertEqual(self.entered, [])
        import csv
        rows = list(csv.DictReader(open(executor.SIGNALS_FILE)))
        self.assertEqual((rows[-1]["status"], rows[-1]["reason"]), ("blocked", "draining"))

    def test_sale_from_an_earlier_episode_does_not_close_a_newer_entry(self):
        executor, ex = self.make()
        now = int(executor.now_ts())
        self.sigs = [{"signature": "base", "blockTime": now - 600}]
        ex.poll_copy_wallets(SOL)
        ex.state["positions"] = [{"mint": MINT, "tokens": 10, "position_usd": 5.0, "opened_ts": now - 60, "copy": WALLET,
                                  "peak_usd": 5.0, "buy_signature": "ours"}]
        closed = []
        ex.close_position = lambda pos, reason, sol_price: closed.append(reason)
        self.sigs = [{"signature": "oldsale", "blockTime": now - 300}] + self.sigs      # they sold 5 minutes ago, we entered 1 minute ago
        self.txs["oldsale"] = tx(5.0, 9.0, 9_000_000, 0)
        ex.state["copy_polled_ts"] = 0
        ex.poll_copy_wallets(SOL)
        self.assertEqual(closed, [])
        # An adopted position has no known acquisition time: the sale is applied.
        ex.state["positions"] = [{"mint": MINT, "tokens": 10, "position_usd": 5.0, "opened_ts": now - 60,
                                  "peak_usd": 5.0, "buy_signature": "adopted", "adopted": True}]
        self.sigs = [{"signature": "oldsale2", "blockTime": now - 300}] + self.sigs
        self.txs["oldsale2"] = tx(5.0, 9.0, 9_000_000, 0)
        ex.state["copy_polled_ts"] = 0
        ex.poll_copy_wallets(SOL)
        self.assertEqual(closed, ["copy_sell"])

    def test_failed_copied_exit_is_kept_and_retried_until_done(self):
        executor, ex = self.make(COPY_MAX_EXIT_ATTEMPTS="3")
        now = int(executor.now_ts())
        self.sigs = [{"signature": "base", "blockTime": now - 600}]
        ex.poll_copy_wallets(SOL)
        ex.state["positions"] = [{"mint": MINT, "tokens": 1000, "position_usd": 10.0, "last_value_usd": 10.0, "opened_ts": now - 600,
                                  "copy": WALLET, "peak_usd": 10.0, "buy_signature": "ours"}]
        attempts = []
        def scale_out(pos, sol_price, frac=None, reason=""):
            attempts.append(round(frac, 2))
            if len(attempts) < 2:
                raise RuntimeError("Jupiter 429")
            pos["tokens"] = int(pos["tokens"] * (1 - frac))
        ex.scale_out = scale_out
        self.sigs = [{"signature": "trim", "blockTime": now}] + self.sigs
        self.txs["trim"] = tx(5.0, 6.0, 10_000_000, 7_000_000)          # they trimmed 30%
        ex.state["copy_polled_ts"] = 0
        ex.poll_copy_wallets(SOL)
        self.assertEqual(attempts, [0.3])
        self.assertEqual(len(ex.state["copy_pending_exits"]), 1)         # kept for retry
        ex.state["copy_pending_exits"][0]["next_ts"] = 0
        ex.state["copy_polled_ts"] = 0
        ex.poll_copy_wallets(SOL)
        self.assertEqual(attempts, [0.3, 0.3])
        self.assertEqual(ex.state["copy_pending_exits"], [])              # done and cleared
        self.assertEqual(ex.state["positions"][0]["tokens"], 700)
        ex.state["copy_polled_ts"] = 0
        ex.poll_copy_wallets(SOL)
        self.assertEqual(attempts, [0.3, 0.3])                            # never replayed

    def test_backlog_of_420_is_fully_processed_at_the_default_budget_with_a_restart(self):
        executor, ex = self.make(COPY_MIN_BUY_USD="50")                 # COPY_DECODE_BUDGET stays at its default of 40
        self.assertEqual(ex.cfg.copy_decode_budget, 40)
        now = int(executor.now_ts())
        self.sigs = [{"signature": "base", "blockTime": now - 6000}]
        ex.poll_copy_wallets(SOL)
        ex.state["positions"] = [{"mint": MINT, "tokens": 10, "position_usd": 5.0, "opened_ts": now - 9000, "copy": WALLET,
                                  "peak_usd": 5.0, "buy_signature": "ours"}]
        backlog = [{"signature": f"n{i:03d}", "blockTime": now - 10 * i} for i in range(420)]    # newest first, ~70 min of trades
        def page(before):
            if before is None:
                return backlog[:100]
            idx = next(i for i, r in enumerate(backlog) if r["signature"] == before)
            rows = backlog[idx + 1: idx + 101]
            return rows if rows else [{"signature": "base", "blockTime": now - 6000}]
        call = lambda method, params, timeout=None: list(page(params[1].get("before")))
        ex.rpc.call = call
        closed = []
        ex.close_position = lambda pos, reason, sol_price: (closed.append(pos["mint"]), ex.state["positions"].remove(pos))
        transaction = lambda sig: tx(5.0, 9.0, 9_000_000, 0) if sig == "n200" else {"meta": {}}   # n200, deep in the middle, is a full sell
        ex.rpc.transaction = transaction
        handled = set()
        original = ex.copy_handle_event
        def spy(wallet, sig, block_time, tx_, sol_price, allow_buys):
            handled.add(sig)
            return original(wallet, sig, block_time, tx_, sol_price, allow_buys)
        ex.copy_handle_event = spy
        for _ in range(4):
            ex.state["copy_polled_ts"] = 0
            ex.poll_copy_wallets(SOL)
        self.assertLess(len(handled), 420)                             # budgeted: not all in four polls
        executor.save_state(ex.state)                                  # restart midway through the queue
        ex2 = executor.Executor(executor.Config())
        ex2.rpc.call = call
        ex2.rpc.transaction = transaction
        ex2.close_position = lambda pos, reason, sol_price: (closed.append(pos["mint"]), ex2.state["positions"].remove(pos))
        original2 = ex2.copy_handle_event
        def spy2(wallet, sig, block_time, tx_, sol_price, allow_buys):
            handled.add(sig)
            return original2(wallet, sig, block_time, tx_, sol_price, allow_buys)
        ex2.copy_handle_event = spy2
        ex2.enter_with_retry = lambda item, sol_price: None
        for _ in range(20):
            ex2.state["copy_polled_ts"] = 0
            ex2.poll_copy_wallets(SOL)
        expected = {f"n{i:03d}" for i in range(420)}
        self.assertEqual(expected - handled, set())                    # every discovered event was decoded
        self.assertEqual(ex2.state["copy_inbox"].get(WALLET, []), [])
        self.assertEqual(ex2.state.get("copy_unresolved", {}), {})
        self.assertNotIn(WALLET, ex2.state.get("copy_backfill", {}))
        self.assertEqual(closed, [MINT])                               # the sell in the middle interval was followed

    def test_stale_pending_exit_never_touches_a_new_position_in_the_same_coin(self):
        executor, ex = self.make(COPY_MAX_EXIT_ATTEMPTS="5")
        now = int(executor.now_ts())
        self.sigs = [{"signature": "base", "blockTime": now - 600}]
        ex.poll_copy_wallets(SOL)
        old = {"mint": MINT, "tokens": 1000, "position_usd": 10.0, "opened_ts": now - 600, "copy": WALLET,
               "peak_usd": 10.0, "buy_signature": "OLD_ENTRY"}
        ex.state["positions"] = [old]
        closes = []
        def close_position(pos, reason, sol_price):
            closes.append(pos.get("buy_signature"))
            raise RuntimeError("Jupiter 429")
        ex.close_position = close_position
        self.sigs = [{"signature": "sell1", "blockTime": now}] + self.sigs
        self.txs["sell1"] = tx(5.0, 9.0, 9_000_000, 0)
        ex.state["copy_polled_ts"] = 0
        ex.poll_copy_wallets(SOL)
        self.assertEqual(closes, ["OLD_ENTRY"])
        self.assertEqual(len(ex.state["copy_pending_exits"]), 1)
        self.assertEqual(ex.state["copy_pending_exits"][0]["position_id"], old["position_id"])
        # The old position leaves through its own exits; the same coin is bought again.
        ex.state["positions"] = [{"mint": MINT, "tokens": 500, "position_usd": 8.0, "opened_ts": now, "copy": WALLET,
                                  "peak_usd": 8.0, "buy_signature": "NEW_ENTRY", "position_id": "fresh-id"}]
        ex.state["copy_pending_exits"][0]["next_ts"] = 0
        ex.state["copy_polled_ts"] = 0
        ex.poll_copy_wallets(SOL)
        self.assertEqual(closes, ["OLD_ENTRY"])                        # the new position was not touched
        self.assertEqual(ex.state["copy_pending_exits"], [])            # the stale intent was retired
        # An intent from before position identities existed is retired, never applied.
        ex.state["copy_pending_exits"] = [{"key": "x", "signature": "s", "wallet": WALLET, "mint": MINT, "fraction": 1.0,
                                           "target_tokens": 0, "attempts": 1, "next_ts": 0}]
        ex.state["copy_polled_ts"] = 0
        ex.poll_copy_wallets(SOL)
        self.assertEqual(closes, ["OLD_ENTRY"])
        self.assertEqual(ex.state["copy_pending_exits"], [])

    def test_old_signal_file_with_renamed_column_is_rotated_not_truncated(self):
        executor, ex = self.make()
        import csv
        old_header = ["timestamp", "wallet", "mint", "kind", "usd", "pre_pct", "mirrored", "reason", "signature", "gmgn"]
        executor.SIGNALS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with executor.SIGNALS_FILE.open("w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=old_header)
            w.writeheader()
            w.writerow({"timestamp": "2026-09-21T11:00:00Z", "wallet": WALLET, "mint": MINT, "kind": "first", "usd": 500,
                        "pre_pct": 0, "mirrored": 1, "reason": "mirrored", "signature": "old", "gmgn": ""})
        executor.record_copy_signal({"signal_id": "id1", "wallet": WALLET, "mint": MINT, "kind": "first", "status": "filled",
                                     "reason": "filled", "mirrored": 1, "source_usd": 500, "fill_usd": 17.6, "fill_tokens": 123,
                                     "fill_signature": "FILLSIG", "signature": "src", "gmgn": "copy"})
        rows = list(csv.DictReader(open(executor.SIGNALS_FILE)))
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["signal_id"], rows[0]["status"], rows[0]["source_usd"], rows[0]["fill_usd"],
                          rows[0]["fill_tokens"], rows[0]["fill_signature"]), ("id1", "filled", "500", "17.6", "123", "FILLSIG"))
        rotated = [p for p in executor.SIGNALS_FILE.parent.glob("copy_signals.*.csv")]
        self.assertEqual(len(rotated), 1)                                # the old file and its record are preserved
        old_rows = list(csv.DictReader(open(rotated[0])))
        self.assertEqual(old_rows[0]["usd"], "500")

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
        wallets, minimums, sizes = executor.parse_wallet_list("a:500:0.5, b::25%, c:300:x2, d:100:junk")
        self.assertEqual(wallets, ("a", "b", "c", "d"))
        self.assertEqual(minimums, {"a": 500.0, "c": 300.0, "d": 100.0})
        self.assertEqual(sizes, {"a": 0.5, "b": 0.25, "c": 2.0})

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

    def test_partial_sell_trims_our_position_by_the_same_share(self):
        executor, ex = self.make()
        now = int(executor.now_ts())
        self.sigs = [{"signature": "base", "blockTime": now}]
        ex.poll_copy_wallets(SOL)
        ex.state["positions"] = [{"mint": MINT, "tokens": 10, "position_usd": 8.0, "last_value_usd": 12.0,
                                  "opened_ts": now, "copy": WALLET, "peak_usd": 12.0}]
        closed, trimmed = [], []
        ex.close_position = lambda pos, reason, sol_price: closed.append(reason)
        ex.scale_out = lambda pos, sol_price, frac=None, reason="": trimmed.append((round(frac, 2), reason))
        self.sigs = [{"signature": "trim", "blockTime": now}, {"signature": "base", "blockTime": now}]
        self.txs["trim"] = tx(5.0, 6.0, 10_000_000, 7_000_000)          # sold 30% of its stack
        ex.state["copy_polled_ts"] = 0
        ex.poll_copy_wallets(SOL)
        self.assertEqual(trimmed, [(0.3, "copy_trim")])
        self.assertEqual(closed, [])
        # A trim of our position worth under a dollar is not worth the fees.
        ex.state["positions"][0]["last_value_usd"] = 2.0
        self.sigs = [{"signature": "tiny", "blockTime": now}] + self.sigs
        self.txs["tiny"] = tx(5.0, 6.0, 7_000_000, 6_000_000)            # sold ~14%
        ex.state["copy_polled_ts"] = 0
        ex.poll_copy_wallets(SOL)
        self.assertEqual(len(trimmed), 1)
        # Selling 80% or more of the stack closes ours.
        self.sigs = [{"signature": "most", "blockTime": now}] + self.sigs
        self.txs["most"] = tx(5.0, 6.0, 6_000_000, 1_000_000)
        ex.state["copy_polled_ts"] = 0
        ex.poll_copy_wallets(SOL)
        self.assertEqual(closed, ["copy_sell"])

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

    def test_daily_loss_limit_off_by_default_never_pauses(self):
        executor, ex = self.make()
        self.assertEqual(ex.cfg.daily_loss_limit_usd, 0.0)
        ex.state["daily"]["realized_pnl_usd"] = -500.0
        self.assertGreater(executor.position_size_usd(ex.cfg, 200.0, 0, -500.0), 0.0)
        ex.state["positions"].pop(0)
        self.assertTrue(ex.rotate_for_copy("New", SOL))
        executor, ex = self.make(DAILY_LOSS_LIMIT_USD="25")
        self.assertEqual(executor.position_size_usd(ex.cfg, 200.0, 0, -29.4), 0.0)
        self.assertGreater(executor.position_size_usd(ex.cfg, 200.0, 0, -24.0), 0.0)

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
        env.setdefault("COPY_LADDER", "2:40,3:30,5:30")   # the price steps assume these rungs
        executor, p = fresh(**env)
        self.addCleanup(p.stop)
        ex = executor.Executor(executor.Config())
        return executor, ex

    def test_parse(self):
        executor, p = fresh(POSITION_CHECK_SECONDS="0")                 # the built-in ladder, not the pinned one
        self.addCleanup(p.stop)
        self.assertEqual(executor.parse_sell_ladder("5:30, 2x:40%,3:30,bad,0.5:10"),
                         [{"x": 2.0, "pct": 40.0}, {"x": 3.0, "pct": 30.0}, {"x": 5.0, "pct": 30.0}])
        self.assertEqual(executor.parse_sell_ladder(""), [])
        # 30% at +40%, 30% at +80%; the remaining 40% rides as the runner (COPY_RUNNER_TRAIL).
        self.assertEqual([(r["x"], r["pct"]) for r in executor.Config().copy_ladder], [(1.4, 30.0), (1.8, 30.0)])

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

    def drive(self, ex, pos):
        """A price the test can move; quotes (not the feed) value the position every call."""
        price = {"v": 0.01}
        ex.token_prices = lambda mints: {}
        ex.jup.quote = lambda mint, out, amount, **kw: {"outAmount": str(int(amount * price["v"] / SOL * 1e9))}
        closed = []
        real_close = ex.close_position
        ex.close_position = lambda p, reason, sol_price: (closed.append(reason), real_close(p, reason, sol_price))
        def at(v):
            price["v"] = v
            ex.manage_positions(SOL, panic=False)
        return at, closed

    def test_runner_rides_past_the_last_rung_with_no_cap(self):
        executor, ex = self.make(COPY_LADDER="1.4:30,1.8:30")
        pos = self.position(ex)                        # 1000 tokens for $10: entry price $0.01
        at, closed = self.drive(ex, pos)
        at(0.015)                                      # 1.5x: 30% of the entry tokens
        self.assertEqual(pos["tokens"], 700)
        at(0.019)                                      # 1.9x: 30% more; the last rung no longer closes
        self.assertEqual(pos["tokens"], 400)
        self.assertEqual(closed, [])
        xcfg = ex.exit_cfg(pos)
        self.assertEqual(xcfg.take_profit, float("inf"))
        self.assertEqual(xcfg.trailing_stop, 0.40)
        self.assertEqual(executor.tp_text(4.0, xcfg), "tp_value=none(runner, trail 40%)")
        self.assertEqual(executor.stop_text(4.0, 7.6, xcfg), "sl_value=$4.00(breakeven)")
        at(0.10)                                       # 10x: nothing caps the runner
        self.assertIn(pos, ex.state["positions"])
        at(0.065)                                      # 35% off its peak: still riding
        self.assertIn(pos, ex.state["positions"])
        at(0.058)                                      # 42% off: the runner trail sells it
        self.assertEqual(closed, ["trailing_stop"])

    def test_runner_off_restores_the_closing_last_rung(self):
        executor, ex = self.make(COPY_LADDER="1.4:30,1.8:30", COPY_RUNNER_TRAIL="0")
        pos = self.position(ex)
        at, closed = self.drive(ex, pos)
        at(0.015)
        self.assertEqual(pos["tokens"], 700)
        at(0.019)
        self.assertEqual(closed, ["ladder_1.8x"])

    def test_breakeven_floor_after_a_pop_that_never_armed_the_trail(self):
        """The 8 losers of the first 38 positions that popped +12-30% and rode to -30%."""
        executor, ex = self.make(COPY_LADDER="")         # no rungs: the floor alone
        pos = self.position(ex)
        at, closed = self.drive(ex, pos)
        at(0.0125)                                     # +25%: above the +20% arm, below the +30% trail arm
        at(0.0101)                                     # back to +1%: still above entry
        self.assertEqual(closed, [])
        at(0.0099)                                     # below entry: out near cost, not at -30%
        self.assertEqual(closed, ["breakeven_stop"])

    def test_no_floor_below_the_arm_and_other_lanes_untouched(self):
        executor, ex = self.make(COPY_LADDER="")
        pos = self.position(ex)
        at, closed = self.drive(ex, pos)
        at(0.0115)                                     # +15%: below the +20% arm
        at(0.0090)                                     # -10%: ordinary meme noise, held
        self.assertEqual(closed, [])
        at(0.0069)
        self.assertEqual(closed, ["stop_loss"])
        base = executor.Config()                       # graduation lane: no floor
        self.assertIsNone(executor.decide_exit(10.0, 9.9, 0, 1, base, 12.5))

    def test_jump_past_two_rungs_takes_both(self):
        executor, ex = self.make()
        pos = self.position(ex)
        ex.jup.quote = lambda mint, out, amount, **kw: {"outAmount": str(int(amount * 0.033 / SOL * 1e9))}  # 3.3x at once
        ex.manage_positions(SOL, panic=False)
        self.assertEqual(pos["tokens"], 300)        # 40% + 30% of the entry tokens sold together
        self.assertEqual([r["done"] for r in pos["ladder"]], [True, True, False])


if __name__ == "__main__":
    unittest.main()


class SourceExitImpactTests(unittest.TestCase):
    """A followed wallet whose stack is large next to the pool takes the price down with it
    when it leaves; when that move alone would reach our stop, the buy is not mirrored."""

    def setUp(self):
        executor, p = fresh(PAPER_BALANCE_USD="200")
        self.addCleanup(p.stop)
        self.executor = executor
        self.ex = executor.Executor(executor.Config())
        self.ex.state["paper_balance_usd"] = 200.0
        self.skips = []
        self.ex.skip = lambda mint, reason: self.skips.append(reason)

    def quote(self, source_impact_pct, fail_source=False):
        source_tokens = 5_000_000_000
        def q(inp, out, amount, **kw):
            if inp == MINT and amount == source_tokens:
                if fail_source:
                    raise RuntimeError("quote unavailable")
                return {"outAmount": str(amount), "priceImpactPct": str(source_impact_pct / 100)}
            return {"outAmount": str(amount), "priceImpactPct": "0.001"}    # 1:1, our own size is tiny
        self.ex.jup.quote = q
        return source_tokens

    def enter(self, source_tokens):
        import time as _time
        self.ex.try_enter({"mint": MINT, "graduated_ts": _time.time(), "enter_at": _time.time(), "copy": WALLET,
                           "copy_buy_usd": 5300, "copy_tokens": source_tokens}, SOL)

    def test_a_source_whose_exit_would_hit_our_stop_is_not_mirrored(self):
        self.enter(self.quote(35.0))
        self.assertEqual(self.ex.state["positions"], [])
        self.assertTrue(self.skips and "source wallet's exit" in self.skips[0], self.skips)

    def test_a_source_with_a_small_footprint_is_mirrored_and_recorded(self):
        self.enter(self.quote(4.2))
        self.assertEqual(self.skips, [])
        self.assertEqual(len(self.ex.state["positions"]), 1)
        self.assertEqual(self.ex.state["positions"][0]["entry_source_exit_impact_pct"], 4.2)

    def test_threshold_defaults_to_the_copy_stop_loss_and_is_configurable(self):
        self.assertAlmostEqual(self.ex.cfg.copy_max_source_exit_impact_pct, self.ex.cfg.copy_stop_loss * 100)
        executor, p = fresh(COPY_MAX_SOURCE_EXIT_IMPACT_PCT="0")
        self.addCleanup(p.stop)
        self.assertEqual(executor.Config().copy_max_source_exit_impact_pct, 0.0)

    def test_a_failed_source_quote_fails_open(self):
        self.enter(self.quote(35.0, fail_source=True))
        self.assertEqual(self.skips, [])
        self.assertEqual(len(self.ex.state["positions"]), 1)
        self.assertIsNone(self.ex.state["positions"][0]["entry_source_exit_impact_pct"])

    def test_an_item_without_source_tokens_is_not_quoted(self):
        import time as _time
        calls = []
        self.ex.jup.quote = lambda inp, out, amount, **kw: (calls.append(amount), {"outAmount": str(amount), "priceImpactPct": "0.001"})[1]
        self.ex.try_enter({"mint": MINT, "graduated_ts": _time.time(), "enter_at": _time.time(), "copy": WALLET,
                           "copy_buy_usd": 5300}, SOL)
        self.assertEqual(len(self.ex.state["positions"]), 1)
        self.assertEqual(len(calls), 2)      # our buy quote and the round-trip sell quote only
