import os
import tempfile
import time
import unittest
from unittest import mock

from solders.keypair import Keypair


def fresh(**env):
    tmp = tempfile.mkdtemp(prefix="grad-reconcile-test-")
    base = {"DATA_DIR": tmp, "HELIUS_API_KEY": "k", "MIGRATION_ADDRESS": "a", "EXECUTOR_MODE": "paper"}
    base.update(env)
    patcher = mock.patch.dict(os.environ, base)
    patcher.start()
    import importlib
    import executor
    importlib.reload(executor)
    return executor, patcher


def live_executor(**env):
    executor, p = fresh(EXECUTOR_MODE="live", WALLET_PRIVATE_KEY=str(Keypair()), **env)
    ex = executor.Executor(executor.Config())
    return executor, ex, p


def acct(mint, amount, program="TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"):
    return {"pubkey": str(Keypair().pubkey()), "program": program, "mint": mint, "amount": amount, "decimals": 6}


class BagBesidePositionTests(unittest.TestCase):
    """A moon bag of the same mint must not be counted as, or sold with, a new position."""
    def test_entry_counts_the_buys_delta_and_close_sells_only_the_position(self):
        executor, ex, p = live_executor()
        self.addCleanup(p.stop)
        mint = str(Keypair().pubkey())
        ex.rpc.token_balance = lambda owner, m: 37_000_000_000                   # 7.47 bag + 29.9 new
        tx = {"transaction": {"message": {"accountKeys": [{"pubkey": str(ex.wallet.pubkey)}]}},
              "meta": {"err": None, "preBalances": [10**9], "postBalances": [10**9 - 80_000_000],
                       "preTokenBalances": [{"owner": str(ex.wallet.pubkey), "mint": mint, "uiTokenAmount": {"amount": "7471270702"}}],
                       "postTokenBalances": [{"owner": str(ex.wallet.pubkey), "mint": mint, "uiTokenAmount": {"amount": "37373403324"}}]}}
        ex.rpc.transaction = lambda sig: tx
        self.assertEqual(ex.tokens_received("sig", mint), 29_902_132_622)
        self.assertEqual(ex.sellable(mint, 29_902_132_622), 29_902_132_622)    # not the 37.37 balance
        self.assertEqual(ex.sellable(mint, 50_000_000_000), 37_000_000_000)    # never more than held
        quoted = []
        ex.jup.quote = lambda m, out, amount, **kw: (quoted.append(amount), {"outAmount": "60000000"})[1]
        ex.execute_swap = lambda quote: "sellsig"
        ex.close_token_account = lambda *a, **k: "x"
        import time as _time
        pos = {"mint": mint, "tokens": 29_902_132_622, "position_usd": 8.13, "opened_ts": _time.time(), "opened_at": "t",
               "peak_usd": 12.0, "last_value_usd": 12.0, "buy_signature": "sig", "graduated_ts": None}   # a winner: keeps a bag
        ex.state["positions"] = [pos]
        ex.close_position(pos, "take_profit", 100.0)
        keep = int(29_902_132_622 * ex.cfg.moon_bag)
        self.assertEqual(quoted, [29_902_132_622 - keep])                       # the position minus its own bag; old bag untouched


class CloseAccountTests(unittest.TestCase):
    def test_close_builds_signed_tx_and_sends(self):
        executor, ex, p = live_executor()
        self.addCleanup(p.stop)
        sent = []
        ex.rpc.call = lambda m, params: {"value": {"blockhash": "11111111111111111111111111111111"}} if m == "getLatestBlockhash" else None
        ex.rpc.send_raw = lambda raw: (sent.append(raw), "sig123")[1]
        mint = str(Keypair().pubkey())
        sig = ex.close_token_account(str(Keypair().pubkey()), executor.TOKEN_PROGRAM, mint)
        self.assertEqual(sig, "sig123")
        self.assertEqual(len(sent), 1)
        self.assertGreater(len(sent[0]), 100)  # a real signed transaction

    def test_burn_included_when_dust_present(self):
        executor, ex, p = live_executor()
        self.addCleanup(p.stop)
        sizes = []
        ex.rpc.call = lambda m, params: {"value": {"blockhash": "11111111111111111111111111111111"}}
        ex.rpc.send_raw = lambda raw: (sizes.append(len(raw)), "s")[1]
        mint = str(Keypair().pubkey()); a = str(Keypair().pubkey())
        ex.close_token_account(a, executor.TOKEN_PROGRAM, mint, burn_amount=0)
        ex.close_token_account(a, executor.TOKEN_PROGRAM, mint, burn_amount=42)
        self.assertGreater(sizes[1], sizes[0])  # burn instruction adds bytes


class ReconcileTests(unittest.TestCase):
    def test_adopts_closes_and_skips_correctly(self):
        executor, ex, p = live_executor()
        self.addCleanup(p.stop)
        tracked, untracked, dust, usdc = (str(Keypair().pubkey()) for _ in range(4))
        ex.state["positions"] = [{"mint": tracked, "tokens": 5, "position_usd": 5.0, "opened_ts": 0, "opened_at": "t", "peak_usd": 5.0}]
        ex.rpc.token_accounts = lambda owner, mint=None: [
            acct(str(Keypair().pubkey()), 0),          # empty -> close
            acct(untracked, 1_000_000),                # worth $6 -> adopt
            acct(dust, 10),                            # worth ~0 -> leave
            acct(tracked, 5),                          # already managed -> skip
            acct(executor.USDC, 8390),                 # quote token -> skip
        ]
        # $6 for 1,000,000 raw; scale linearly
        ex.jup.quote = lambda i, o, amount, **kw: {"outAmount": str(int(amount * 60))}  # 1e6 -> 6e7 lamports = 0.06 SOL
        closed = []
        ex.close_token_account = lambda a, prog, mint, burn_amount=0: (closed.append(mint), "sig")[1]
        with mock.patch.object(executor.time, "sleep"):
            ex.reconcile_wallet(sol_price=100.0)
        mints = [q["mint"] for q in ex.state["positions"]]
        self.assertIn(untracked, mints)
        self.assertNotIn(dust, mints)
        self.assertNotIn(executor.USDC, mints)
        self.assertEqual(mints.count(tracked), 1)
        adopted = next(q for q in ex.state["positions"] if q["mint"] == untracked)
        self.assertTrue(adopted["adopted"])
        self.assertAlmostEqual(adopted["position_usd"], 6.0)
        self.assertEqual(len(closed), 2)  # the empty account closed and the worthless dust burned; $6 holding adopted

    def test_dust_is_burned_and_its_account_closed(self):
        executor, ex, p = live_executor()
        self.addCleanup(p.stop)
        dust, small = (str(Keypair().pubkey()) for _ in range(2))
        ex.rpc.token_accounts = lambda owner, mint=None: [acct(dust, 1_000), acct(small, 300_000)]
        ex.jup.quote = lambda i, o, amount, **kw: {"outAmount": str(int(amount * 60))}   # $0.006 and $1.80
        closed = []
        ex.close_token_account = lambda a, prog, mint, burn_amount=0: (closed.append((mint, burn_amount)), "sig")[1]
        with mock.patch.object(executor.time, "sleep"):
            ex.reconcile_wallet(sol_price=100.0)
        self.assertEqual(closed, [(dust, 1_000)])                     # burned; the $1.80 leftover became a bag instead
        self.assertEqual([b["mint"] for b in ex.state["moon_bags"]], [small])

    def test_small_leftovers_become_moon_bags_not_positions(self):
        executor, ex, p = live_executor()
        self.addCleanup(p.stop)
        leftover, real = (str(Keypair().pubkey()) for _ in range(2))
        ex.rpc.token_accounts = lambda owner, mint=None: [acct(leftover, 300_000), acct(real, 1_000_000)]
        ex.jup.quote = lambda i, o, amount, **kw: {"outAmount": str(int(amount * 60))}   # $1.80 and $6.00 at SOL=$100
        with mock.patch.object(executor.time, "sleep"):
            ex.reconcile_wallet(sol_price=100.0)
        self.assertEqual([q["mint"] for q in ex.state["positions"]], [real])
        bag = ex.state["moon_bags"][0]
        self.assertEqual(bag["mint"], leftover)
        self.assertAlmostEqual(bag["kept_usd"], 1.8)
        self.assertEqual(bag["from_exit"], "adopted")

    def test_reconcile_skipped_in_paper_mode(self):
        executor, p = fresh()
        self.addCleanup(p.stop)
        ex = executor.Executor(executor.Config())
        self.assertIsNone(ex.wallet)  # nothing to reconcile without a wallet


class StuckPositionTests(unittest.TestCase):
    def _executor_with_overdue_position(self):
        executor, p = fresh(TIME_STOP_MINUTES="1", STUCK_AFTER_MINUTES="1")
        ex = executor.Executor(executor.Config())
        ex.state["positions"] = [{"mint": "m", "tokens": 100, "position_usd": 5.0,
                                  "opened_ts": time.time() - 10 * 60, "opened_at": "t", "peak_usd": 5.0}]
        return executor, ex, p

    def test_repeated_failures_past_time_stop_move_to_stuck(self):
        executor, ex, p = self._executor_with_overdue_position()
        self.addCleanup(p.stop)
        def no_route(*a, **k):
            raise RuntimeError("no route: {}")
        ex.jup.quote = no_route
        for _ in range(3):
            ex.manage_positions(sol_price=100.0, panic=False)
        self.assertEqual(ex.state["positions"], [])
        self.assertEqual(len(ex.state["stuck"]), 1)
        self.assertEqual(ex.state["stuck"][0]["mint"], "m")
        self.assertEqual(ex.state["stuck"][0]["sell_failures"], 3)

    def test_two_failures_not_enough(self):
        executor, ex, p = self._executor_with_overdue_position()
        self.addCleanup(p.stop)
        def no_route(*a, **k):
            raise RuntimeError("no route")
        ex.jup.quote = no_route
        for _ in range(2):
            ex.manage_positions(sol_price=100.0, panic=False)
        self.assertEqual(len(ex.state["positions"]), 1)
        self.assertNotIn("stuck", ex.state)

    def test_healthy_hold_resets_failure_count(self):
        executor, p = fresh()
        self.addCleanup(p.stop)
        ex = executor.Executor(executor.Config())
        ex.state["positions"] = [{"mint": "m", "tokens": 100, "position_usd": 5.0, "opened_ts": time.time(),
                                  "opened_at": "t", "peak_usd": 5.0, "sell_failures": 2}]
        ex.jup.quote = lambda i, o, amount, **kw: {"outAmount": str(int(5.2 / 100 * 1e9))}  # $5.20, no exit
        ex.manage_positions(sol_price=100.0, panic=False)
        self.assertEqual(ex.state["positions"][0]["sell_failures"], 0)

    def test_panic_liquidates_stuck(self):
        executor, p = fresh()
        self.addCleanup(p.stop)
        ex = executor.Executor(executor.Config())
        ex.state["stuck"] = [{"mint": "m", "tokens": 100, "position_usd": 5.0, "opened_at": "t"}]
        ex.jup.quote = lambda i, o, amount, **kw: {"outAmount": str(int(3.0 / 100 * 1e9))}  # $3 back
        ex.liquidate_bags(100.0, "stuck", "panic_stuck")
        self.assertEqual(ex.state["stuck"], [])
        self.assertAlmostEqual(ex.state["paper_balance_usd"], 103.0)


if __name__ == "__main__":
    unittest.main()
