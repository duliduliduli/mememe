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
        executor, ex = self.make()
        now = int(executor.now_ts())
        self.sigs = [{"signature": "old1", "blockTime": now - 5}]
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

    def test_copy_positions_use_copy_exits(self):
        executor, ex = self.make(COPY_TAKE_PROFIT="0.5", TAKE_PROFIT="0.75")
        self.assertEqual(ex.exit_cfg({"mint": "x", "copy": WALLET}).take_profit, 0.5)
        self.assertEqual(ex.exit_cfg({"mint": "x"}).take_profit, 0.75)


if __name__ == "__main__":
    unittest.main()
