import os
import tempfile
import time
import unittest
from unittest import mock

from solders.keypair import Keypair


def fresh(**env):
    tmp = tempfile.mkdtemp(prefix="grad-exit-priority-test-")
    base = {"DATA_DIR": tmp, "HELIUS_API_KEY": "k", "MIGRATION_ADDRESS": "a", "EXECUTOR_MODE": "paper"}
    base.update(env)
    patcher = mock.patch.dict(os.environ, base)
    patcher.start()
    import importlib
    import executor
    importlib.reload(executor)
    return executor, executor.Executor(executor.Config()), patcher


class ExitPriorityTests(unittest.TestCase):
    def test_open_position_is_managed_before_due_entry(self):
        executor, ex, p = fresh(MAX_ENTRIES_PER_CYCLE="1")
        self.addCleanup(p.stop)
        ex.state["positions"] = [{"mint": "open"}]
        ex.pending.extend([
            {"mint": "due-1", "graduated_ts": time.time(), "enter_at": 0},
            {"mint": "due-2", "graduated_ts": time.time(), "enter_at": 0},
        ])
        order = []
        ex.sol_price_usd = lambda: 100.0
        ex.manage_positions = lambda price, panic: order.append("manage")
        ex.poll_graduations = lambda: order.append("poll")
        ex.enter_with_retry = lambda item, price: order.append(f"enter:{item['mint']}")

        ex.run_cycle()

        self.assertEqual(order, ["manage", "poll", "enter:due-1"])
        self.assertEqual([item["mint"] for item in ex.pending], ["due-2"])
        self.assertEqual(ex.state["pending"], ex.pending)

    def test_draining_still_manages_but_never_enters(self):
        executor, ex, p = fresh()
        self.addCleanup(p.stop)
        ex.state["positions"] = [{"mint": "open"}]
        ex.pending.append({"mint": "due", "graduated_ts": time.time(), "enter_at": 0})
        executor.STOP_FLAG.parent.mkdir(parents=True, exist_ok=True)
        executor.STOP_FLAG.touch()
        order = []
        ex.sol_price_usd = lambda: 100.0
        ex.manage_positions = lambda price, panic: order.append("manage")
        ex.poll_graduations = lambda: order.append("poll")
        ex.enter_with_retry = lambda item, price: order.append("enter")

        ex.run_cycle()

        self.assertEqual(order, ["manage"])


class RentReclaimFreshBalanceTests(unittest.TestCase):
    def test_reclaim_uses_fresh_confirmed_balance_not_stale_listing(self):
        executor, ex, p = fresh(EXECUTOR_MODE="live", WALLET_PRIVATE_KEY=str(Keypair()))
        self.addCleanup(p.stop)
        mint = str(Keypair().pubkey())
        account = str(Keypair().pubkey())
        ex.rpc.token_accounts = lambda owner, mint=None: [{
            "pubkey": account,
            "program": executor.TOKEN_2022_PROGRAM,
            "mint": mint,
            "amount": 42_614_685_058,
            "decimals": 6,
        }]
        ex.rpc.token_account_balance = lambda address: 0
        closes = []
        ex.close_token_account = lambda address, program, mint, burn_amount=0: (
            closes.append(burn_amount), "sig"
        )[1]

        ex.reclaim_rent(mint)

        self.assertEqual(closes, [0])

    def test_targeted_balance_read_requests_confirmed_commitment(self):
        executor, ex, p = fresh()
        self.addCleanup(p.stop)
        calls = []
        ex.rpc.call = lambda method, params: (
            calls.append((method, params)), {"value": {"amount": "7"}}
        )[1]
        self.assertEqual(ex.rpc.token_account_balance("account"), 7)
        self.assertEqual(calls, [
            ("getTokenAccountBalance", ["account", {"commitment": "confirmed"}])
        ])


if __name__ == "__main__":
    unittest.main()
