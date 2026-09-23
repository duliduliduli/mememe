import os
import tempfile
import unittest
from unittest import mock

from solders.keypair import Keypair


def fresh(**env):
    tmp = tempfile.mkdtemp(prefix="grad-rent-reclaim-test-")
    base = {"DATA_DIR": tmp, "HELIUS_API_KEY": "k", "MIGRATION_ADDRESS": "a", "EXECUTOR_MODE": "paper"}
    base.update(env)
    patcher = mock.patch.dict(os.environ, base)
    patcher.start()
    import importlib
    import executor
    importlib.reload(executor)
    return executor, executor.Executor(executor.Config()), patcher


NON_NATIVE_HAS_BALANCE = RuntimeError(
    "RPC sendTransaction: {'code': -32002, 'message': 'Transaction simulation failed', "
    "'data': {'err': {'InstructionError': [0, {'Custom': 11}]}}}"
)


class SendPreflightTests(unittest.TestCase):
    def test_send_raw_preflights_at_confirmed_commitment(self):
        # The sell before a rent reclaim is only waited on to "confirmed"; the node's default
        # finalized preflight still sees the pre-sell balance and rejects the valid close.
        executor, ex, p = fresh()
        self.addCleanup(p.stop)
        calls = []
        ex.rpc.call = lambda method, params: (calls.append((method, params)), "sig")[1]
        self.assertEqual(ex.rpc.send_raw(b"tx"), "sig")
        method, params = calls[0]
        self.assertEqual(method, "sendTransaction")
        self.assertEqual(params[1]["preflightCommitment"], "confirmed")
        self.assertFalse(params[1]["skipPreflight"])


class RentReclaimRetryTests(unittest.TestCase):
    def setUp(self):
        self.executor, self.ex, p = fresh(EXECUTOR_MODE="live", WALLET_PRIVATE_KEY=str(Keypair()))
        self.addCleanup(p.stop)
        self.mint = str(Keypair().pubkey())
        self.account = str(Keypair().pubkey())
        self.ex.rpc.token_accounts = lambda owner, mint=None: [{
            "pubkey": self.account, "program": self.executor.TOKEN_PROGRAM, "mint": mint, "amount": 0, "decimals": 6,
        }]
        self.ex.rpc.token_account_balance = lambda address: 0
        self.closes = []

    def failing_then_ok(self, failures):
        outcomes = [NON_NATIVE_HAS_BALANCE] * failures + ["sig"]

        def close(address, program, mint, burn_amount=0):
            self.closes.append(burn_amount)
            out = outcomes.pop(0)
            if isinstance(out, Exception):
                raise out
            return out
        self.ex.close_token_account = close

    def test_failed_close_is_queued_and_retried_until_it_lands(self):
        self.failing_then_ok(1)
        self.assertFalse(self.ex.reclaim_rent(self.mint))
        pending = self.ex.state["rent_pending"]
        self.assertEqual([e["mint"] for e in pending], [self.mint])
        self.assertEqual(pending[0]["attempts"], 1)
        self.assertGreater(pending[0]["next_ts"], self.executor.now_ts())

        self.ex.retry_pending_rent()  # not due yet
        self.assertEqual(len(self.closes), 1)

        pending[0]["next_ts"] = 0
        self.ex.retry_pending_rent()
        self.assertEqual(len(self.closes), 2)
        self.assertEqual(self.ex.state["rent_pending"], [])

    def test_gives_up_after_the_last_attempt(self):
        self.failing_then_ok(self.executor.RENT_RECLAIM_ATTEMPTS + 5)
        self.ex.reclaim_rent(self.mint)
        for _ in range(self.executor.RENT_RECLAIM_ATTEMPTS):
            for entry in self.ex.state.get("rent_pending") or []:
                entry["next_ts"] = 0
            self.ex.retry_pending_rent()
        self.assertEqual(self.ex.state["rent_pending"], [])
        self.assertEqual(len(self.closes), self.executor.RENT_RECLAIM_ATTEMPTS)

    def test_run_cycle_retries_pending_rent_in_live_mode(self):
        self.failing_then_ok(1)
        self.ex.reclaim_rent(self.mint)
        self.ex.state["rent_pending"][0]["next_ts"] = 0
        self.ex.sol_price_usd = lambda: 100.0
        self.ex.poll_graduations = lambda: None
        self.ex.run_cycle()
        self.assertEqual(len(self.closes), 2)
        self.assertEqual(self.ex.state["rent_pending"], [])

    def test_a_deferred_close_is_dropped_when_the_coin_is_held_again(self):
        self.failing_then_ok(1)
        self.ex.reclaim_rent(self.mint)
        self.ex.state["rent_pending"][0]["next_ts"] = 0
        self.ex.state["positions"].append({"mint": self.mint, "tokens": 5, "position_usd": 1.0, "opened_ts": 0.0,
                                           "opened_at": "t", "peak_usd": 1.0})
        self.ex.retry_pending_rent()
        self.assertEqual(len(self.closes), 1)              # never burned the new position's tokens
        self.assertEqual(self.ex.state["rent_pending"], [])


if __name__ == "__main__":
    unittest.main()
