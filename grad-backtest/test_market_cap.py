import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock


def fresh_executor(**env):
    tmp = tempfile.mkdtemp(prefix="grad-mcap-test-")
    base = {"DATA_DIR": tmp, "HELIUS_API_KEY": "k", "MIGRATION_ADDRESS": "a", "EXECUTOR_MODE": "paper"}
    base.update(env)
    patcher = mock.patch.dict(os.environ, base)
    patcher.start()
    import importlib
    import executor
    importlib.reload(executor)
    return executor, patcher


class MarketCapMathTests(unittest.TestCase):
    def test_implied_market_cap(self):
        executor, p = fresh_executor()
        self.addCleanup(p.stop)
        # $5 buys 50,000 tokens (6 decimals) of a 1B-supply token => $0.0001/token => $100k cap
        cap = executor.entry_market_cap_usd(5.0, 50_000 * 10**6, 1_000_000_000, 6)
        self.assertAlmostEqual(cap, 100_000.0)

    def test_pumped_token_shows_huge_cap(self):
        executor, p = fresh_executor()
        self.addCleanup(p.stop)
        # $5 buys only 33 tokens => $0.15/token => $150M on 1B supply (the WOTF case)
        cap = executor.entry_market_cap_usd(5.0, 33 * 10**6, 1_000_000_000, 6)
        self.assertGreater(cap, 100_000_000)

    def test_raw_supply_path_is_decimal_invariant(self):
        executor, p = fresh_executor()
        self.addCleanup(p.stop)
        # The raw/raw calculation must give the same result regardless of a bogus UI
        # supply or decimal count supplied by a caller.
        expected = 100_000.0
        for decimals, bogus_ui in ((0, 1.0), (6, 999.0), (12, 1e30)):
            with self.subTest(decimals=decimals):
                cap = executor.entry_market_cap_usd(
                    5.0, 50_000 * 10**6, bogus_ui, decimals,
                    supply_raw=1_000_000_000 * 10**6,
                )
                self.assertAlmostEqual(cap, expected)

    def test_supply_details_rejects_inconsistent_rpc_fields(self):
        executor, p = fresh_executor()
        self.addCleanup(p.stop)
        rpc = executor.Rpc(executor.Config())
        rpc.call = mock.Mock(return_value={"value": {
            "amount": "1000000000", "decimals": 6, "uiAmountString": "1000000000"
        }})
        with self.assertRaisesRegex(RuntimeError, "inconsistent token supply"):
            rpc.token_supply_details("mint")

    def test_supply_details_accepts_exact_rpc_fields(self):
        executor, p = fresh_executor()
        self.addCleanup(p.stop)
        rpc = executor.Rpc(executor.Config())
        rpc.call = mock.Mock(return_value={"value": {
            "amount": "1000000000000000", "decimals": 6,
            "uiAmountString": "1000000000"
        }})
        self.assertEqual(rpc.token_supply_details("mint"),
                         (1_000_000_000.0, 6, 1_000_000_000_000_000))

    def test_degenerate_inputs_return_none(self):
        executor, p = fresh_executor()
        self.addCleanup(p.stop)
        self.assertIsNone(executor.entry_market_cap_usd(5.0, 0, 1e9, 6))
        self.assertIsNone(executor.entry_market_cap_usd(5.0, 1000, 0, 6))


class MarketCapGuardTests(unittest.TestCase):
    def test_ceiling_off_by_default_and_blocks_pumped_when_set(self):
        executor, p = fresh_executor()
        self.addCleanup(p.stop)
        self.assertEqual(executor.Config().max_entry_market_cap_usd, 0)  # off: it rejected the liquid cohort
        p.stop()
        executor, p = fresh_executor(MAX_ENTRY_MARKET_CAP_USD="300000")
        self.addCleanup(p.stop)
        cfg = executor.Config()
        self.assertEqual(cfg.max_entry_market_cap_usd, 300_000)
        now = 1000.0 + 30
        # a real graduation at $64k passes
        self.assertIsNone(executor.entry_guard_reason(cfg, 1000.0, now, 1.0, 64_000))
        # LEGO-shaped ($894k) and WOTF-shaped ($150M) are rejected
        for cap in (894_000, 150_000_000):
            reason = executor.entry_guard_reason(cfg, 1000.0, now, 1.0, cap)
            self.assertIsNotNone(reason)
            self.assertIn("market cap", reason)
            self.assertIn("pumped", reason)

    def test_unknown_market_cap_never_blocks(self):
        executor, p = fresh_executor()
        self.addCleanup(p.stop)
        cfg = executor.Config()
        self.assertIsNone(executor.entry_guard_reason(cfg, 1000.0, 1030.0, 1.0, None))

    def test_zero_disables(self):
        executor, p = fresh_executor(MAX_ENTRY_MARKET_CAP_USD="0")
        self.addCleanup(p.stop)
        cfg = executor.Config()
        self.assertIsNone(executor.entry_guard_reason(cfg, 1000.0, 1030.0, 1.0, 150_000_000))

    def test_env_override(self):
        executor, p = fresh_executor(MAX_ENTRY_MARKET_CAP_USD="1000000")
        self.addCleanup(p.stop)
        cfg = executor.Config()
        self.assertIsNone(executor.entry_guard_reason(cfg, 1000.0, 1030.0, 1.0, 894_000))
        self.assertIsNotNone(executor.entry_guard_reason(cfg, 1000.0, 1030.0, 1.0, 1_500_000))

    def test_guard_order_keeps_existing_checks_first(self):
        executor, p = fresh_executor()
        self.addCleanup(p.stop)
        cfg = executor.Config()
        # stale beats market cap; impact beats market cap
        self.assertIn("stale", executor.entry_guard_reason(cfg, 1000.0, 1000.0 + 30 + 61, 1.0, 5_000_000))
        self.assertIn("price impact", executor.entry_guard_reason(cfg, 1000.0, 1030.0, 40.0, 5_000_000))


class OptimizerRunupFilterTests(unittest.TestCase):
    def test_pumped_token_excluded_from_dataset(self):
        from grad_backtest import Candle
        from optimize import load_dataset

        tmp = Path(tempfile.mkdtemp(prefix="grad-runup-test-"))
        cache = tmp / "cache"
        cache.mkdir()

        def write(mint, grad_price, entry_price):
            (cache / f"{mint}.json").write_text(json.dumps({
                "pool_address": "p", "dex_id": "pump",
                "minute_path": [vars(Candle(1000, grad_price, grad_price, grad_price, grad_price, 1)),
                                vars(Candle(1060, entry_price, entry_price, entry_price, entry_price, 1))],
                "entry_candles": [vars(Candle(1030, entry_price, entry_price, entry_price, entry_price, 1))],
            }))

        write("calm", 100.0, 110.0)    # +10% by entry
        write("pumped", 100.0, 900.0)  # +800% by entry
        (tmp / "graduations.csv").write_text(
            "mint_address,graduation_timestamp,extraction_status\ncalm,1000,confirmed\npumped,1000,confirmed\n"
        )
        both = load_dataset(tmp / "graduations.csv", cache, 30)
        self.assertEqual({d["mint"] for d in both}, {"calm", "pumped"})
        filtered = load_dataset(tmp / "graduations.csv", cache, 30, max_entry_runup=2.0)
        self.assertEqual([d["mint"] for d in filtered], ["calm"])


if __name__ == "__main__":
    unittest.main()
