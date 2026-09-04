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


class MultiWalletBundleGuardTests(unittest.TestCase):
    def test_defaults_and_legacy_fraction_env(self):
        executor, p = fresh(MAX_CLUSTER_PCT="0.30")
        self.addCleanup(p.stop)
        cfg = executor.Config()
        self.assertEqual(cfg.max_bundle_slot_pct, 30)
        self.assertEqual(cfg.max_cluster_pct, 30)
        self.assertEqual(cfg.max_ancestry_cluster_pct, 20)
        self.assertEqual(cfg.max_transfer_cluster_pct, 12)
        self.assertEqual(cfg.max_coordinated_buy_pct, 20)
        self.assertEqual(cfg.max_repeat_cohort_pct, 12)
        self.assertEqual(cfg.max_dev_cluster_pct, 15)
        self.assertEqual(cfg.max_top10_wallet_pct, 50)
        self.assertEqual(cfg.max_early_buy_pct, 30)
        self.assertEqual(cfg.min_funder_lookup_pct, 80)
        self.assertEqual(cfg.bundle_max_wallets, 50)
        self.assertEqual(cfg.bundle_funder_max_wallets, 20)
        self.assertTrue(cfg.bundle_fail_closed)

    def test_mike_apeson_pattern_is_blocked_by_transfer_graph(self):
        """Regression: immediate funders looked unrelated at 34.8% coverage, but the
        supply-weighted token-transfer graph showed the coordinated holder cohort."""
        executor, p = fresh()
        self.addCleanup(p.stop)
        cfg = executor.Config()
        bundle = {
            "complete": True,
            "cluster_pct": 0.0,
            "ancestry_cluster_pct": 0.0,
            "transfer_cluster_pct": 24.0,
            "coordinated_buy_pct": 8.0,
            "dev_cluster_pct": 0.0,
            "top10_wallet_pct": 17.4,
            "early_buy_pct": 0.0,
            "funder_coverage_pct": 34.8,
        }
        reason = executor.entry_guard_reason(cfg, GRAD, ENTRY, 1.0, 64_000, 600, 8, bundle)
        self.assertIn("token-transfer cluster", reason)
        self.assertIn("partial-coverage", reason)

    def test_partial_coverage_tightens_every_bundle_limit(self):
        executor, p = fresh()
        self.addCleanup(p.stop)
        cfg = executor.Config()
        bundle = {"complete": True, "cluster_pct": 22.0, "funder_coverage_pct": 35.0}
        reason = executor.entry_guard_reason(cfg, GRAD, ENTRY, 1.0, 64_000, 600, 8, bundle)
        self.assertIn("20.1%", reason)
        bundle["funder_coverage_pct"] = 80.0
        self.assertIsNone(executor.entry_guard_reason(cfg, GRAD, ENTRY, 1.0, 64_000, 600, 8, bundle))

    def test_repeat_launch_cohort_is_blocked(self):
        executor, p = fresh()
        self.addCleanup(p.stop)
        cfg = executor.Config()
        bundle = {"complete": True, "repeat_cohort_pct": 16.0, "funder_coverage_pct": 100.0}
        reason = executor.entry_guard_reason(cfg, GRAD, ENTRY, 1.0, 64_000, 600, 8, bundle)
        self.assertIn("repeat-launch wallet cohort", reason)

    def test_connected_cluster_is_blocked(self):
        executor, p = fresh()
        self.addCleanup(p.stop)
        cfg = executor.Config()
        bundle = {"complete": True, "cluster_pct": 54.0}
        reason = executor.entry_guard_reason(cfg, GRAD, ENTRY, 1.0, 64_000, 600, 8, bundle)
        self.assertIn("connected funding cluster", reason)
        self.assertIn("54.0%", reason)

    def test_same_slot_dev_top10_and_early_thresholds(self):
        executor, p = fresh()
        self.addCleanup(p.stop)
        cfg = executor.Config()
        cases = [
            ({"complete": True, "bundle_slot_pct": 31}, "same-slot"),
            ({"complete": True, "dev_cluster_pct": 16}, "creator-linked"),
            ({"complete": True, "top10_wallet_pct": 51}, "top ten"),
            ({"complete": True, "early_buy_pct": 31}, "first three slots"),
        ]
        for bundle, expected in cases:
            with self.subTest(expected=expected):
                reason = executor.entry_guard_reason(cfg, GRAD, ENTRY, 1.0, 64_000, 600, 8, bundle)
                self.assertIn(expected, reason)

    def test_incomplete_bundle_data_fails_closed(self):
        executor, p = fresh()
        self.addCleanup(p.stop)
        cfg = executor.Config()
        reason = executor.entry_guard_reason(
            cfg, GRAD, ENTRY, 1.0, 64_000, 600, 8,
            {"complete": False, "error": "funder coverage 20.0%"},
        )
        self.assertIn("bundle data unavailable", reason)
        self.assertIn("coverage", reason)


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
    def test_helius_das_expands_holder_sample_beyond_twenty(self):
        executor, p = fresh()
        self.addCleanup(p.stop)
        rows = [{"owner": f"w{i}", "amount": str(100 - i)} for i in range(50)]
        owners = {"value": [{"owner": executor.SYSTEM_PROGRAM} for _ in rows]}
        fake = FakeRpc(executor, {"getTokenAccounts": {"token_accounts": rows}, "getMultipleAccounts": owners})
        holders = fake.rpc.plain_wallet_holders("m", limit=50)
        self.assertEqual(len(holders), 50)
        self.assertFalse(any(method == "getTokenLargestAccounts" for method, _ in fake.calls))

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
        multiple = [params for method, params in fake.calls if method == "getMultipleAccounts"]
        self.assertEqual(multiple[0][0], ["ta_pool", "ta_dev", "ta_mayhem", "ta_small"])

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


class WalletGraphCacheTests(unittest.TestCase):
    def test_positive_funder_survives_rpc_recreation(self):
        executor, p = fresh()
        self.addCleanup(p.stop)
        cfg = executor.Config()
        first = executor.Rpc(cfg)
        first.origin_funder = lambda wallet, before: "treasury"
        self.assertEqual(first.cached_origin_funder("w", GRAD), "treasury")
        first.save_wallet_graph_cache()

        second = executor.Rpc(cfg)
        second.origin_funder = lambda wallet, before: (_ for _ in ()).throw(AssertionError("cache miss"))
        self.assertEqual(second.cached_origin_funder("w", GRAD), "treasury")

    def test_lookup_completion_distinguishes_no_funder_from_failure(self):
        executor, p = fresh()
        self.addCleanup(p.stop)
        rpc = executor.Rpc(executor.Config())

        def lookup(wallet, _before):
            if wallet == "failed":
                raise RuntimeError("provider timeout")
            return "treasury" if wallet == "funded" else None

        rpc.cached_origin_funder = lookup
        funders, completed = rpc._lookup_funders({"funded", "unfunded", "failed"}, GRAD)
        self.assertEqual(funders["funded"], "treasury")
        self.assertIsNone(funders["unfunded"])
        self.assertIsNone(funders["failed"])
        self.assertEqual(completed, {"funded", "unfunded"})


class BundleSnapshotTests(unittest.TestCase):
    def test_shared_funder_and_same_slot_are_measured(self):
        executor, p = fresh()
        self.addCleanup(p.stop)
        cfg = executor.Config()
        rpc = executor.Rpc(cfg)
        wallets = [f"w{i}" for i in range(6)]
        largest = {"value": [{"address": f"ta{i}", "amount": "9"} for i in range(6)]}
        token_accounts = {"value": [token_account(wallet) for wallet in wallets]}
        owner_accounts = {"value": [{"owner": executor.SYSTEM_PROGRAM} for _ in wallets]}
        fake = FakeRpc(executor, {
            "getTokenLargestAccounts": largest,
            "getMultipleAccounts": [token_accounts, owner_accounts],
        })
        rpc.call = fake.call
        rpc.enhanced_transactions = lambda address, **params: [{
            "slot": 10,
            "feePayer": "dev",
            "tokenTransfers": [
                {"mint": "m", "toUserAccount": wallet, "tokenAmount": 0.09, "decimals": 2}
                for wallet in wallets
            ],
        }]
        rpc.origin_funder = lambda wallet, before: "dev"
        snapshot = rpc.bundle_snapshot("m", 1.0, 2, 900, 1000)
        self.assertTrue(snapshot["complete"])
        self.assertAlmostEqual(snapshot["bundle_slot_pct"], 54.0)
        self.assertAlmostEqual(snapshot["cluster_pct"], 54.0)
        self.assertAlmostEqual(snapshot["ancestry_cluster_pct"], 54.0)
        self.assertAlmostEqual(snapshot["coordinated_buy_pct"], 54.0)
        self.assertAlmostEqual(snapshot["dev_cluster_pct"], 54.0)
        self.assertAlmostEqual(snapshot["top10_wallet_pct"], 54.0)
        self.assertAlmostEqual(snapshot["early_buy_pct"], 54.0)
        self.assertAlmostEqual(snapshot["funder_coverage_pct"], 100.0)
        self.assertAlmostEqual(snapshot["funder_lookup_pct"], 100.0)
        self.assertEqual(snapshot["funder_sample_count"], 6)
        self.assertEqual(snapshot["holder_sample_count"], 6)
        self.assertEqual(snapshot["bundle_confidence"], "high")

    def test_funding_coverage_uses_top_twenty_but_graph_keeps_fifty(self):
        executor, p = fresh()
        self.addCleanup(p.stop)
        rpc = executor.Rpc(executor.Config())
        holders = [(f"w{i}", 100 - i) for i in range(50)]
        rpc.plain_wallet_holders = lambda mint, exclude, limit: holders
        rpc.enhanced_transactions = lambda address, **params: [{
            "slot": 10,
            "feePayer": "dev",
            "tokenTransfers": [
                {"mint": "m", "toUserAccount": wallet, "tokenAmount": raw, "decimals": 0}
                for wallet, raw in holders
            ],
        }]
        rpc.origin_funder = lambda wallet, before: "treasury" if wallet.startswith("w") else None

        snapshot = rpc.bundle_snapshot("m", 10_000.0, 0, 900, 1000)
        self.assertTrue(snapshot["complete"])
        self.assertEqual(snapshot["holder_sample_count"], 50)
        self.assertEqual(snapshot["funder_sample_count"], 20)
        self.assertAlmostEqual(snapshot["funder_coverage_pct"], 100.0)
        self.assertAlmostEqual(snapshot["funder_lookup_pct"], 100.0)

    def test_completed_but_unidentifiable_funding_is_insufficient_not_timeout(self):
        executor, p = fresh()
        self.addCleanup(p.stop)
        rpc = executor.Rpc(executor.Config())
        holders = [("w1", 60), ("w2", 40)]
        rpc.plain_wallet_holders = lambda mint, exclude, limit: holders
        rpc.enhanced_transactions = lambda address, **params: [{
            "slot": 10,
            "feePayer": "dev",
            "tokenTransfers": [
                {"mint": "m", "toUserAccount": wallet, "tokenAmount": raw, "decimals": 0}
                for wallet, raw in holders
            ],
        }]
        rpc.origin_funder = lambda wallet, before: None

        snapshot = rpc.bundle_snapshot("m", 1_000.0, 0, 900, 1000)
        self.assertFalse(snapshot["complete"])
        self.assertAlmostEqual(snapshot["funder_lookup_pct"], 100.0)
        self.assertEqual(snapshot["bundle_confidence"], "insufficient")
        self.assertIn("funder coverage 0.0%", snapshot["error"])


class EntryIntegrationTests(unittest.TestCase):
    """Paper-mode try_enter with SOLL's numbers: every lookup answers, the floor rejects it."""

    def setUp(self):
        self.executor, p = fresh()
        self.addCleanup(p.stop)
        self.ex = self.executor.Executor(self.executor.Config())
        self.ex.state["paper_balance_usd"] = 100.0

    def wire(self, supply_ui=2e9, out_tokens=23_700_000, created=GRAD - 29, holder_amount=0):
        ex = self.ex
        ex.jup.quote = lambda a, b, amt, **kw: {
            "outAmount": str(out_tokens * 10**6) if b != self.executor.WSOL else "95000000",
            "priceImpactPct": "0.01",
        }
        responses = {
            "getTokenSupply": {"value": {"uiAmountString": str(supply_ui), "decimals": 6}},
            "getSignaturesForAddress": [sigs([GRAD, created])],
            "getTokenLargestAccounts": {"value": [{"address": "ta", "amount": str(holder_amount * 10**6)}]},
            "getMultipleAccounts": [{"value": [token_account("whale")]}, {"value": [{"owner": self.executor.SYSTEM_PROGRAM}]}],
        }
        FakeRpcLike = FakeRpc(self.executor, responses)
        ex.rpc.call = FakeRpcLike.call
        ex.rpc.bundle_snapshot = lambda *args, **kwargs: {
            "complete": True,
            "bundle_slot_pct": 5.0,
            "cluster_pct": 5.0,
            "dev_cluster_pct": 2.0,
            "top10_wallet_pct": 20.0,
            "early_buy_pct": 5.0,
            "funder_coverage_pct": 100.0,
        }
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

    def test_lookup_failure_blocks_when_bundle_data_is_mandatory(self):
        self.wire(out_tokens=150_000, created=GRAD - 3600)
        self.ex.rpc.call = lambda m, p: (_ for _ in ()).throw(RuntimeError("rpc down"))
        skips = self.enter()
        self.assertIn("bundle data unavailable", skips)
        self.assertEqual(self.ex.state["positions"], [])

    def test_connected_cluster_snapshot_blocks_entry(self):
        self.wire(out_tokens=150_000, created=GRAD - 3600)
        self.ex.rpc.bundle_snapshot = lambda *args, **kwargs: {
            "complete": True,
            "cluster_pct": 54.0,
            "funder_coverage_pct": 100.0,
        }
        skips = self.enter()
        self.assertIn("connected funding cluster", skips)
        self.assertEqual(self.ex.state["positions"], [])

    def test_log_only_canary_records_warning_but_enters(self):
        self.ex.cfg.bundle_log_only = True
        self.wire(out_tokens=150_000, created=GRAD - 3600)
        self.ex.rpc.bundle_snapshot = lambda *args, **kwargs: {
            "complete": True,
            "cluster_pct": 54.0,
            "funder_coverage_pct": 100.0,
        }
        skips = self.enter()
        self.assertEqual(skips, "")
        self.assertEqual(len(self.ex.state["positions"]), 1)

    def test_weak_reverse_liquidity_is_rejected(self):
        self.wire(out_tokens=150_000, created=GRAD - 3600, holder_amount=100_000_000)
        original = self.ex.jup.quote
        self.ex.jup.quote = lambda a, b, amt, **kw: (
            {"outAmount": "50000000", "priceImpactPct": "0.01"}
            if b == self.executor.WSOL else original(a, b, amt, **kw)
        )
        skips = self.enter()
        self.assertIn("round-trip liquidity returns 50.0%", skips)
        self.assertEqual(self.ex.state["positions"], [])


if __name__ == "__main__":
    unittest.main()
