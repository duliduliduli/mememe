"""Guards added after SOLL: a Mayhem launch whose creator bought 44% of the curve in the creation
transaction, graduated 29 seconds later with six buyers, dumped 78% of supply 24 seconds after
migration, and was bought by the bot at a $450 market cap 45 seconds after that."""
import os
import tempfile
import unittest
from unittest import mock


def fresh(**env):
    tmp = tempfile.mkdtemp(prefix="grad-bundle-test-")
    base = {"DATA_DIR": tmp, "HELIUS_API_KEY": "k", "MIGRATION_ADDRESS": "a", "EXECUTOR_MODE": "paper"}
    base.update(env)
    patcher = mock.patch.dict(os.environ, base)
    patcher.start()
    import importlib
    import executor
    importlib.reload(executor)
    return executor, patcher


GRAD = 1000.0
ENTRY = GRAD + 30


class FloorGuardTests(unittest.TestCase):
    def test_defaults(self):
        executor, p = fresh()
        self.addCleanup(p.stop)
        cfg = executor.Config()
        self.assertEqual(cfg.min_entry_market_cap_usd, 25_000)
        self.assertEqual(cfg.min_curve_age_seconds, 120)
        self.assertEqual(cfg.max_top_holder_pct, 20)

    def test_soll_cap_blocked_and_graduation_cap_passes(self):
        executor, p = fresh()
        self.addCleanup(p.stop)
        cfg = executor.Config()
        reason = executor.entry_guard_reason(cfg, GRAD, ENTRY, 1.0, 450)
        self.assertIn("< $25,000", reason)
        self.assertIn("dumped", reason)
        self.assertIsNone(executor.entry_guard_reason(cfg, GRAD, ENTRY, 1.0, 64_000))
        self.assertIsNone(executor.entry_guard_reason(cfg, GRAD, ENTRY, 1.0, 25_000))

    def test_floor_zero_disables_and_unknown_never_blocks(self):
        executor, p = fresh(MIN_ENTRY_MARKET_CAP_USD="0")
        self.addCleanup(p.stop)
        cfg = executor.Config()
        self.assertIsNone(executor.entry_guard_reason(cfg, GRAD, ENTRY, 1.0, 450))
        self.assertIsNone(executor.entry_guard_reason(cfg, GRAD, ENTRY, 1.0, None))

    def test_ceiling_still_applies_above_floor(self):
        executor, p = fresh()
        self.addCleanup(p.stop)
        cfg = executor.Config()
        self.assertIn("pumped", executor.entry_guard_reason(cfg, GRAD, ENTRY, 1.0, 5_000_000))


class CurveAgeGuardTests(unittest.TestCase):
    def test_29_second_curve_blocked(self):
        executor, p = fresh()
        self.addCleanup(p.stop)
        cfg = executor.Config()
        reason = executor.entry_guard_reason(cfg, GRAD, ENTRY, 1.0, 64_000, curve_age_seconds=29)
        self.assertIn("29s after creation", reason)
        self.assertIn("bundle", reason)

    def test_slow_curve_passes(self):
        executor, p = fresh()
        self.addCleanup(p.stop)
        cfg = executor.Config()
        self.assertIsNone(executor.entry_guard_reason(cfg, GRAD, ENTRY, 1.0, 64_000, curve_age_seconds=1800))
        self.assertIsNone(executor.entry_guard_reason(cfg, GRAD, ENTRY, 1.0, 64_000, curve_age_seconds=120))

    def test_unknown_age_and_zero_setting_never_block(self):
        executor, p = fresh()
        self.addCleanup(p.stop)
        cfg = executor.Config()
        self.assertIsNone(executor.entry_guard_reason(cfg, GRAD, ENTRY, 1.0, 64_000, curve_age_seconds=None))
        cfg.min_curve_age_seconds = 0
        self.assertIsNone(executor.entry_guard_reason(cfg, GRAD, ENTRY, 1.0, 64_000, curve_age_seconds=5))


class TopHolderGuardTests(unittest.TestCase):
    def test_creator_holding_59_pct_blocked(self):
        executor, p = fresh()
        self.addCleanup(p.stop)
        cfg = executor.Config()
        reason = executor.entry_guard_reason(cfg, GRAD, ENTRY, 1.0, 64_000, 600, top_holder_pct=59.0)
        self.assertIn("59.0% of supply", reason)
        self.assertIn("dump", reason)

    def test_distributed_token_passes(self):
        executor, p = fresh()
        self.addCleanup(p.stop)
        cfg = executor.Config()
        self.assertIsNone(executor.entry_guard_reason(cfg, GRAD, ENTRY, 1.0, 64_000, 600, top_holder_pct=8.0))
        self.assertIsNone(executor.entry_guard_reason(cfg, GRAD, ENTRY, 1.0, 64_000, 600, top_holder_pct=None))

    def test_env_override(self):
        executor, p = fresh(MAX_TOP_HOLDER_PCT="35")
        self.addCleanup(p.stop)
        cfg = executor.Config()
        self.assertIsNone(executor.entry_guard_reason(cfg, GRAD, ENTRY, 1.0, 64_000, 600, top_holder_pct=30))
        self.assertIsNotNone(executor.entry_guard_reason(cfg, GRAD, ENTRY, 1.0, 64_000, 600, top_holder_pct=40))

    def test_guard_order(self):
        executor, p = fresh()
        self.addCleanup(p.stop)
        cfg = executor.Config()
        # earlier, cheaper checks win: stale > impact > ceiling > floor > age > holder
        self.assertIn("stale", executor.entry_guard_reason(cfg, GRAD, ENTRY + 61, 1.0, 450, 5, 90))
        self.assertIn("market cap", executor.entry_guard_reason(cfg, GRAD, ENTRY, 1.0, 450, 5, 90))
        self.assertIn("after creation", executor.entry_guard_reason(cfg, GRAD, ENTRY, 1.0, 64_000, 5, 90))
        self.assertIn("top wallet", executor.entry_guard_reason(cfg, GRAD, ENTRY, 1.0, 64_000, 600, 90))


class FakeRpc:
    """Scripted responses keyed by method; records calls."""

    def __init__(self, executor, responses):
        self.calls = []
        self.responses = responses
        self.rpc = executor.Rpc(executor.Config())
        self.rpc.call = self.call

    def call(self, method, params):
        self.calls.append((method, params))
        r = self.responses[method]
        return r.pop(0) if isinstance(r, list) else r


def sigs(times, n=None):
    return [{"signature": f"s{i}", "blockTime": t} for i, t in enumerate(times)][: n or len(times)]


class MintFirstSeenTests(unittest.TestCase):
    def test_short_history_returns_creation_time(self):
        executor, p = fresh()
        self.addCleanup(p.stop)
        fake = FakeRpc(executor, {"getSignaturesForAddress": [sigs([1029, 1015, 1000])]})
        self.assertEqual(fake.rpc.mint_first_seen("m", 880), 1000)
        self.assertEqual(len(fake.calls), 1)

    def test_stops_early_once_old_enough(self):
        executor, p = fresh()
        self.addCleanup(p.stop)
        # a full page whose oldest entry already predates the cutoff: one call, no paging
        page = sigs([2000 - i for i in range(1000)])
        fake = FakeRpc(executor, {"getSignaturesForAddress": [page]})
        seen = fake.rpc.mint_first_seen("m", 1500)
        self.assertEqual(seen, 1001)
        self.assertEqual(len(fake.calls), 1)

    def test_pages_then_gives_up_as_unknown(self):
        executor, p = fresh()
        self.addCleanup(p.stop)
        page = sigs([5000] * 1000)  # busy token, every page still newer than the cutoff
        fake = FakeRpc(executor, {"getSignaturesForAddress": [page, page, page, page]})
        self.assertIsNone(fake.rpc.mint_first_seen("m", 100))
        self.assertEqual(len(fake.calls), 3)
        self.assertEqual(fake.calls[1][1][1]["before"], "s999")

    def test_pages_to_the_end(self):
        executor, p = fresh()
        self.addCleanup(p.stop)
        full = sigs([3000 - i for i in range(1000)])
        tail = sigs([1900, 1850])
        fake = FakeRpc(executor, {"getSignaturesForAddress": [full, tail]})
        self.assertEqual(fake.rpc.mint_first_seen("m", 100), 1850)
        self.assertEqual(len(fake.calls), 2)


def token_account(owner):
    return {"owner": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA", "data": {"parsed": {"info": {"owner": owner}}}}


class TopWalletHolderTests(unittest.TestCase):
    def test_pool_and_program_vaults_ignored(self):
        executor, p = fresh()
        self.addCleanup(p.stop)
        largest = {"value": [
            {"address": "ta_pool", "amount": "1940000000000000"},
            {"address": "ta_dev", "amount": "1180000000000000"},
            {"address": "ta_mayhem", "amount": "500000000000000"},
            {"address": "ta_small", "amount": "23000000000000"},
        ]}
        accounts = {"value": [token_account("pool"), token_account("dev"), token_account("mayhem"), token_account("small")]}
        owner_accounts = {"value": [
            {"owner": "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"},  # PumpSwap pool PDA
            {"owner": executor.SYSTEM_PROGRAM},                     # the creator's wallet
            {"owner": "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"},  # pump.fun-owned vault
            None,                                                   # unfunded wallet
        ]}
        fake = FakeRpc(executor, {"getTokenLargestAccounts": largest, "getMultipleAccounts": [accounts, owner_accounts]})
        self.assertEqual(fake.rpc.top_wallet_holder("m"), ("dev", 1180000000000000))
        self.assertEqual(fake.calls[1][1][0], ["ta_pool", "ta_dev", "ta_mayhem", "ta_small"])

    def test_excludes_our_wallet_and_handles_empty(self):
        executor, p = fresh()
        self.addCleanup(p.stop)
        largest = {"value": [{"address": "ta_us", "amount": "50"}, {"address": "ta_them", "amount": "40"}]}
        accounts = {"value": [token_account("us"), token_account("them")]}
        owners = {"value": [{"owner": executor.SYSTEM_PROGRAM}, {"owner": executor.SYSTEM_PROGRAM}]}
        fake = FakeRpc(executor, {"getTokenLargestAccounts": largest, "getMultipleAccounts": [accounts, owners]})
        self.assertEqual(fake.rpc.top_wallet_holder("m", {"us"}), ("them", 40))
        fake = FakeRpc(executor, {"getTokenLargestAccounts": {"value": []}})
        self.assertIsNone(fake.rpc.top_wallet_holder("m"))


class EntryIntegrationTests(unittest.TestCase):
    """Paper-mode try_enter with SOLL's numbers: every lookup answers, the floor rejects it."""

    def setUp(self):
        self.executor, p = fresh()
        self.addCleanup(p.stop)
        self.ex = self.executor.Executor(self.executor.Config())
        self.ex.state["paper_balance_usd"] = 100.0

    def wire(self, supply_ui=2e9, out_tokens=23_700_000, created=GRAD - 29, holder_amount=0):
        ex = self.ex
        ex.jup.quote = lambda a, b, amt, **kw: {"outAmount": str(out_tokens * 10**6) if b != self.executor.WSOL else "1000000", "priceImpactPct": "0.01"}
        responses = {
            "getTokenSupply": {"value": {"uiAmountString": str(supply_ui), "decimals": 6}},
            "getSignaturesForAddress": [sigs([GRAD, created])],
            "getTokenLargestAccounts": {"value": [{"address": "ta", "amount": str(holder_amount * 10**6)}]},
            "getMultipleAccounts": [{"value": [token_account("whale")]}, {"value": [{"owner": self.executor.SYSTEM_PROGRAM}]}],
        }
        FakeRpcLike = FakeRpc(self.executor, responses)
        ex.rpc.call = FakeRpcLike.call
        return FakeRpcLike

    def enter(self):
        with mock.patch.object(self.executor, "now_ts", return_value=ENTRY):
            self.ex.try_enter({"mint": "soll", "graduated_ts": GRAD}, 100.0)
        return open(os.path.join(os.environ["DATA_DIR"], "skips.csv")).read() if os.path.exists(os.path.join(os.environ["DATA_DIR"], "skips.csv")) else ""

    def test_soll_skipped_on_market_cap_floor(self):
        # $5 (min position on a $100 paper account at 10%... sized to $10) buys 23.7M of 2B => ~$850 cap
        self.wire()
        skips = self.enter()
        self.assertIn("market cap", skips)
        self.assertIn("dumped", skips)
        self.assertEqual(self.ex.state["positions"], [])

    def test_fast_curve_skipped_when_cap_is_fine(self):
        self.wire(out_tokens=150_000, created=GRAD - 29)  # $10 -> 150k tokens of 2B => ~$133k cap
        skips = self.enter()
        self.assertIn("29s after creation", skips)

    def test_whale_skipped(self):
        self.wire(out_tokens=150_000, created=GRAD - 3600, holder_amount=1_180_000_000)  # 59%
        skips = self.enter()
        self.assertIn("top wallet holds 59.0%", skips)
        self.assertIn("[whale]", skips)

    def test_clean_token_enters_with_metadata_recorded(self):
        self.wire(out_tokens=150_000, created=GRAD - 3600, holder_amount=100_000_000)  # 5%
        skips = self.enter()
        self.assertEqual(skips, "")
        pos = self.ex.state["positions"][0]
        self.assertEqual(pos["entry_curve_age_seconds"], 3600)
        self.assertEqual(pos["entry_top_holder_pct"], 5.0)
        self.assertGreater(pos["entry_market_cap_usd"], 25_000)

    def test_lookup_failure_never_blocks(self):
        self.wire(out_tokens=150_000, created=GRAD - 3600)
        self.ex.rpc.call = lambda m, p: (_ for _ in ()).throw(RuntimeError("rpc down"))
        skips = self.enter()
        self.assertEqual(skips, "")
        self.assertEqual(len(self.ex.state["positions"]), 1)


if __name__ == "__main__":
    unittest.main()
