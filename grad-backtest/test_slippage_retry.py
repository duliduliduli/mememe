import os
import tempfile
import unittest
from unittest import mock

RAW_6001 = (
    "RPC sendTransaction: {'code': -32002, 'message': 'Transaction simulation failed: Error processing "
    "Instruction 6: custom program error: 0x1771', 'data': {'err': {'InstructionError': [6, {'Custom': 6001}]}, "
    "'logs': ['Program JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4 failed: custom program error: 0x1771']}}"
    + "x" * 3000
)


def fresh_executor(**env):
    tmp = tempfile.mkdtemp(prefix="grad-slip-test-")
    base = {"DATA_DIR": tmp, "HELIUS_API_KEY": "k", "MIGRATION_ADDRESS": "a", "EXECUTOR_MODE": "paper"}
    base.update(env)
    patcher = mock.patch.dict(os.environ, base)
    patcher.start()
    import importlib
    import executor
    importlib.reload(executor)
    return executor, patcher


class DescribeErrorTests(unittest.TestCase):
    def test_slippage_code_is_recognised_and_compact(self):
        executor, p = fresh_executor()
        self.addCleanup(p.stop)
        text = executor.describe_error(RuntimeError(RAW_6001))
        self.assertIn("6001", text)
        self.assertIn("slippage", text)
        self.assertLess(len(text), 160)

    def test_unknown_errors_are_truncated(self):
        executor, p = fresh_executor()
        self.addCleanup(p.stop)
        text = executor.describe_error(RuntimeError("boom " * 200))
        self.assertLessEqual(len(text), 241)
        self.assertTrue(text.endswith("…"))


class SlippageConfigTests(unittest.TestCase):
    def test_defaults(self):
        executor, p = fresh_executor()
        self.addCleanup(p.stop)
        cfg = executor.Config()
        self.assertEqual(cfg.slippage_bps, 1000)
        self.assertEqual(cfg.sell_slippage_bps, 1500)
        self.assertEqual(cfg.entry_retries, 2)

    def test_overrides(self):
        executor, p = fresh_executor(SLIPPAGE_BPS="500", SELL_SLIPPAGE_BPS="2000", ENTRY_RETRIES="0")
        self.addCleanup(p.stop)
        cfg = executor.Config()
        self.assertEqual((cfg.slippage_bps, cfg.sell_slippage_bps, cfg.entry_retries), (500, 2000, 0))

    def test_quote_uses_per_call_slippage(self):
        executor, p = fresh_executor()
        self.addCleanup(p.stop)
        jup = executor.Jupiter(executor.Config())
        seen = []

        class Resp:
            def raise_for_status(self):
                pass

            def json(self):
                return {"outAmount": "1"}

        def fake_get(url, params=None, timeout=None):
            seen.append(params["slippageBps"])
            return Resp()

        jup.session.get = fake_get
        jup.quote("a", "b", 1)
        jup.quote("a", "b", 1, slippage_bps=1500)
        self.assertEqual(seen, [1000, 1500])


class EntryRetryTests(unittest.TestCase):
    def test_retries_then_succeeds(self):
        executor, p = fresh_executor(ENTRY_RETRY_SECONDS="0")
        self.addCleanup(p.stop)
        ex = executor.Executor(executor.Config())
        calls = []

        def flaky(item, sol_price):
            calls.append(1)
            if len(calls) < 3:
                raise RuntimeError(RAW_6001)

        ex.try_enter = flaky
        with mock.patch.object(executor.time, "sleep"):
            ex.enter_with_retry({"mint": "m", "graduated_ts": 0}, 100.0)
        self.assertEqual(len(calls), 3)

    def test_gives_up_after_retries_without_raising(self):
        executor, p = fresh_executor(ENTRY_RETRY_SECONDS="0")
        self.addCleanup(p.stop)
        ex = executor.Executor(executor.Config())
        calls = []

        def always_fails(item, sol_price):
            calls.append(1)
            raise RuntimeError(RAW_6001)

        ex.try_enter = always_fails
        with mock.patch.object(executor.time, "sleep"):
            ex.enter_with_retry({"mint": "m", "graduated_ts": 0}, 100.0)  # must not raise
        self.assertEqual(len(calls), 3)

    def test_no_retry_when_entry_is_skipped_not_failed(self):
        executor, p = fresh_executor(ENTRY_RETRY_SECONDS="0")
        self.addCleanup(p.stop)
        ex = executor.Executor(executor.Config())
        calls = []
        ex.try_enter = lambda item, sol_price: calls.append(1)  # guard skip returns normally
        ex.enter_with_retry({"mint": "m", "graduated_ts": 0}, 100.0)
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
