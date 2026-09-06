"""On-chain layers added 2026-09-05 after a 36-minute window with 24 checks and no entries: curve
activity floor, early-dump check, creator history and holding, BOOST-window tagging, skip-row
metadata, and the memo-instruction crash in the transaction normalizer."""
import csv
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock


def fresh(**env):
    tmp = tempfile.mkdtemp(prefix="grad-layers-test-")
    base = {"DATA_DIR": tmp, "MIGRATION_ADDRESS": "a", "EXECUTOR_MODE": "paper", "RPC_URL": "http://rpc.test"}
    base.update(env)
    patcher = mock.patch.dict(os.environ, base)
    patcher.start()
    import importlib
    import executor
    importlib.reload(executor)
    return executor, patcher


GRAD = 1000.0
MINT = "93kTd6r2xTuCN8vt2icgztXCBePgME1yznrJnb2ipump"  # SOLL, a real mint so the curve PDA derives
ENTRY = GRAD + 30


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.ex, p = fresh()
        self.addCleanup(p.stop)
        self.cfg = self.ex.Config()
        self.g = self.ex.entry_guard_reason

    def test_defaults(self):
        self.assertEqual(self.cfg.min_curve_transactions, 150)
        self.assertEqual(self.cfg.max_early_sell_pct, 3)
        self.assertEqual(self.cfg.max_creator_prior_launches, 3)
        self.assertEqual(self.cfg.max_creator_hold_pct, 5)
        self.assertEqual(self.cfg.boost_window_seconds, 300)
        self.assertEqual(self.cfg.max_price_impact_pct, 10)
        self.assertEqual(self.cfg.max_entry_market_cap_usd, 0)

    def test_curve_activity_floor(self):
        for count in (2, 9, 15, 149):
            reason = self.g(self.cfg, GRAD, ENTRY, 1.0, 60_000, 600, curve_tx_count=count)
            self.assertIn(f"curve filled with {count} successful transactions", reason)
            self.assertIn("one-party fill", reason)
        for count in (150, 311, 1561, 2293):
            self.assertIsNone(self.g(self.cfg, GRAD, ENTRY, 1.0, 60_000, 600, curve_tx_count=count))
        self.assertIsNone(self.g(self.cfg, GRAD, ENTRY, 1.0, 60_000, 600, curve_tx_count=None))
        self.cfg.min_curve_transactions = 0
        self.assertIsNone(self.g(self.cfg, GRAD, ENTRY, 1.0, 60_000, 600, curve_tx_count=2))

    def test_early_dump_blocks_and_names_the_seller(self):
        reason = self.g(self.cfg, GRAD, ENTRY, 1.0, 60_000, 600, early_sell_pct=78.0, early_seller="8h5ZMRmR")
        self.assertIn("already sold 78.0% of supply since migration > 3%", reason)
        self.assertIn("[8h5ZMRmR]", reason)
        self.assertIsNone(self.g(self.cfg, GRAD, ENTRY, 1.0, 60_000, 600, early_sell_pct=2.9))
        self.assertIsNone(self.g(self.cfg, GRAD, ENTRY, 1.0, 60_000, 600, early_sell_pct=None))

    def test_creator_checks(self):
        self.assertIn("launch factory", self.g(self.cfg, GRAD, ENTRY, 1.0, 60_000, 600, creator_prior_launches=4))
        self.assertIsNone(self.g(self.cfg, GRAD, ENTRY, 1.0, 60_000, 600, creator_prior_launches=3))
        self.assertIn("creator still holds 12.0%", self.g(self.cfg, GRAD, ENTRY, 1.0, 60_000, 600, creator_hold_pct=12.0))
        self.assertIsNone(self.g(self.cfg, GRAD, ENTRY, 1.0, 60_000, 600, creator_hold_pct=4.9))

    def test_order_cheap_checks_first(self):
        # stale > impact > cap > age > curve txs > top holder > creator hold > launches > early sell > bundle
        self.assertIn("stale", self.g(self.cfg, GRAD, ENTRY + 61, 1.0, 60_000, 5, 90, curve_tx_count=2, early_sell_pct=90))
        self.assertIn("after creation", self.g(self.cfg, GRAD, ENTRY, 1.0, 60_000, 5, 90, curve_tx_count=2, early_sell_pct=90))
        self.assertIn("one-party fill", self.g(self.cfg, GRAD, ENTRY, 1.0, 60_000, 600, 90, curve_tx_count=2, early_sell_pct=90))
        self.assertIn("top wallet", self.g(self.cfg, GRAD, ENTRY, 1.0, 60_000, 600, 90, curve_tx_count=500, creator_hold_pct=50, early_sell_pct=90))
        self.assertIn("creator still holds", self.g(self.cfg, GRAD, ENTRY, 1.0, 60_000, 600, 10, curve_tx_count=500, creator_hold_pct=50, creator_prior_launches=9, early_sell_pct=90))
        self.assertIn("launch factory", self.g(self.cfg, GRAD, ENTRY, 1.0, 60_000, 600, 10, curve_tx_count=500, creator_prior_launches=9, early_sell_pct=90))
        self.assertIn("already sold", self.g(self.cfg, GRAD, ENTRY, 1.0, 60_000, 600, 10, curve_tx_count=500, early_sell_pct=90))
        self.assertIn("bundle data unavailable", self.g(self.cfg, GRAD, ENTRY, 1.0, 60_000, 600, 10, {"complete": False}, curve_tx_count=500))


class NormalizerTests(unittest.TestCase):
    def test_memo_instruction_with_string_parsed_does_not_crash(self):
        ex, p = fresh()
        self.addCleanup(p.stop)
        tx = {
            "slot": 5, "blockTime": 100,
            "transaction": {"message": {"accountKeys": [{"pubkey": "payer"}], "instructions": [
                {"program": "spl-memo", "programId": "Memo", "parsed": "gm"},
                {"program": "system", "programId": "111", "parsed": {"type": "transfer",
                 "info": {"source": "a", "destination": "b", "lamports": 500000}}},
            ]}},
            "meta": {"err": None, "preTokenBalances": [], "postTokenBalances": [], "innerInstructions": []},
        }
        out = ex.normalize_rpc_transaction(tx, "sig")
        self.assertEqual(out["nativeTransfers"], [{"fromUserAccount": "a", "toUserAccount": "b", "amount": 500000}])


class FakeRpc:
    def __init__(self, executor, responses):
        self.calls = []
        self.responses = responses
        self.rpc = executor.Rpc(executor.Config())
        self.rpc.call = self.call
        self.rpc.batch_call = self.batch

    def call(self, method, params, timeout=None):
        self.calls.append((method, params))
        r = self.responses[method]
        return r.pop(0) if isinstance(r, list) else r

    def batch(self, calls, timeout=None):
        self.calls.append(("batch", calls))
        return [self.responses["bodies"].get(c[1][0], {}) for c in calls]


def sig(name, t, err=None):
    return {"signature": name, "blockTime": t, "err": err}


class CurveTransactionCountTests(unittest.TestCase):
    def test_counts_only_successful_in_window(self):
        ex, p = fresh()
        self.addCleanup(p.stop)
        page = [sig("late", 2000), sig("ok1", 900), sig("fail", 900, {"x": 1}), sig("ok2", 800), sig("early", 100)]
        fake = FakeRpc(ex, {"getSignaturesForAddress": [page]})
        self.assertEqual(fake.rpc.curve_transaction_count(MINT, created_ts=500, graduated_ts=1000), 2)
        self.assertEqual(len(fake.calls), 1)

    def test_pages_once_more_when_first_page_full(self):
        ex, p = fresh()
        self.addCleanup(p.stop)
        full = [sig(f"s{i}", 900) for i in range(1000)]
        rest = [sig("older", 850), sig("ancient", 10)]
        fake = FakeRpc(ex, {"getSignaturesForAddress": [full, rest]})
        self.assertEqual(fake.rpc.curve_transaction_count(MINT, 500, 1000), 1001)
        self.assertEqual(fake.calls[1][1][1]["before"], "s999")


class EarlySellerTests(unittest.TestCase):
    def body(self, owner_pre_post):
        pre, post = [], []
        for idx, (owner, a, b) in enumerate(owner_pre_post):
            pre.append({"accountIndex": idx, "mint": "MINT", "owner": owner, "uiTokenAmount": {"amount": str(a)}})
            post.append({"accountIndex": idx, "mint": "MINT", "owner": owner, "uiTokenAmount": {"amount": str(b)}})
        return {"meta": {"err": None, "preTokenBalances": pre, "postTokenBalances": post}}

    def test_creator_dump_found_and_pool_vault_ignored(self):
        ex, p = fresh()
        self.addCleanup(p.stop)
        responses = {
            "getSignaturesForAddress": [[sig("dump", 1050), sig("buy", 1040), sig("pre", 900)]],
            "bodies": {
                "dump": self.body([("creator", 1_566_000_000, 0), ("pool", 400_000_000, 1_966_000_000)]),
                "buy": self.body([("pool", 2_000_000_000, 1_990_000_000), ("buyer", 0, 10_000_000)]),
            },
            "getMultipleAccounts": {"value": [{"owner": ex.SYSTEM_PROGRAM}, {"owner": "pAMM"}]},
        }
        fake = FakeRpc(ex, responses)
        seller = fake.rpc.largest_seller_since("MINT", since_ts=1000, supply_raw=2_000_000_000)
        self.assertEqual(seller[0], "creator")
        self.assertAlmostEqual(seller[1], 78.3)
        self.assertEqual(len(fake.calls[1][1]), 2)  # only the two post-migration bodies decoded

    def test_only_pool_selling_means_no_seller(self):
        ex, p = fresh()
        self.addCleanup(p.stop)
        responses = {
            "getSignaturesForAddress": [[sig("buy", 1040)]],
            "bodies": {"buy": self.body([("pool", 2_000_000_000, 1_900_000_000), ("buyer", 0, 100_000_000)])},
            "getMultipleAccounts": {"value": [{"owner": "pAMM"}]},
        }
        fake = FakeRpc(ex, responses)
        self.assertIsNone(fake.rpc.largest_seller_since("MINT", 1000, 2_000_000_000))


class CreatorProfileTests(unittest.TestCase):
    def test_counts_prior_creates_and_caches(self):
        ex, p = fresh()
        self.addCleanup(p.stop)
        fake = FakeRpc(ex, {
            "getSignaturesForAddress": [[sig("now", 1000), sig("a", 900), sig("b", 800), sig("c", 700)]],
            "bodies": {"a": {"k": 1}, "b": {"k": 2}, "c": {"k": 3}},
        })
        creations = {1: [{"mint": "OTHER1", "creator": "dev"}], 2: [{"mint": "THIS", "creator": "dev"}], 3: [{"mint": "OTHER2", "creator": "someone-else"}]}
        with mock.patch.object(ex, "pump_creations", side_effect=lambda body: creations.get(body.get("k"), [])):
            profile = fake.rpc.creator_profile("dev", "THIS", before_ts=1000)
            self.assertEqual(profile["prior_launches"], 1)  # own other launch counts; this mint and others' don't
            self.assertEqual(profile["sampled_signatures"], 3)
            self.assertEqual(profile["first_seen_ts"], 700)
            again = fake.rpc.creator_profile("dev", "THIS", before_ts=1000)
        self.assertIs(again, profile)
        self.assertEqual(sum(1 for c in fake.calls if c[0] == "getSignaturesForAddress"), 1)


def token_account(owner):
    return {"owner": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA", "data": {"parsed": {"info": {"owner": owner}}}}


class HolderToleranceTests(unittest.TestCase):
    def test_a_closed_account_between_calls_is_tolerated(self):
        ex, p = fresh()
        self.addCleanup(p.stop)
        largest = {"value": [{"address": f"ta{i}", "amount": str(100 - i)} for i in range(10)]}
        accounts = {"value": [token_account(f"w{i}") if i != 3 else None for i in range(10)]}
        owners = {"value": [{"owner": ex.SYSTEM_PROGRAM}] * 9}
        fake = FakeRpc(ex, {"getTokenLargestAccounts": largest, "getMultipleAccounts": [accounts, owners]})
        rows = fake.rpc.plain_wallet_holders("m", limit=20)
        self.assertEqual(len(rows), 9)

    def test_mostly_missing_still_refuses(self):
        ex, p = fresh()
        self.addCleanup(p.stop)
        largest = {"value": [{"address": f"ta{i}", "amount": "1"} for i in range(10)]}
        accounts = {"value": [None] * 6 + [token_account(f"w{i}") for i in range(4)]}
        fake = FakeRpc(ex, {"getTokenLargestAccounts": largest, "getMultipleAccounts": [accounts]})
        with self.assertRaisesRegex(RuntimeError, "holder account data incomplete"):
            fake.rpc.plain_wallet_holders("m", limit=20)


class EntryIntegrationTests(unittest.TestCase):
    """try_enter with every lookup faked, checking the new stages, the recorded metadata and the
    BOOST tag on entry and exit."""

    def setUp(self):
        self.executor, p = fresh()
        self.addCleanup(p.stop)
        self.bot = self.executor.Executor(self.executor.Config())
        self.bot.state["paper_balance_usd"] = 100.0
        self.bot.jup.quote = lambda a, b, amt, **kw: {
            "outAmount": "150000000000" if b != self.executor.WSOL else "95000000",
            "priceImpactPct": "0.01",
        }
        rpc = mock.Mock()
        rpc.token_supply_details.return_value = (2e9, 6, int(2e9 * 10**6))
        rpc.mint_first_seen.return_value = GRAD - 3600
        rpc.curve_transaction_count.return_value = 640
        rpc.top_wallet_holder.return_value = ("holder", int(0.05 * 2e9 * 10**6))
        rpc.mint_creator.return_value = "dev"
        rpc.token_balance.return_value = int(0.01 * 2e9 * 10**6)
        rpc.creator_profile.return_value = {"prior_launches": 1, "sampled_signatures": 40}
        rpc.largest_seller_since.return_value = None
        rpc.bundle_snapshot.return_value = {
            "complete": True, "bundle_slot_pct": 5.0, "cluster_pct": 5.0, "dev_cluster_pct": 2.0,
            "top10_wallet_pct": 20.0, "early_buy_pct": 5.0, "funder_coverage_pct": 100.0,
            "bundle_confidence": "high", "history_total": 1486, "history_decoded": 1000,
        }
        self.bot.rpc = rpc
        self.rpc = rpc

    def enter(self, at=ENTRY):
        with mock.patch.object(self.executor, "now_ts", return_value=at):
            self.bot.try_enter({"mint": "mint", "graduated_ts": GRAD}, 100.0)

    def skips(self):
        path = Path(os.environ["DATA_DIR"]) / "skips.csv"
        return list(csv.DictReader(open(path))) if path.exists() else []

    def test_clean_launch_enters_with_full_metadata(self):
        self.enter()
        pos = self.bot.state["positions"][0]
        self.assertEqual(pos["entry_curve_tx_count"], 640)
        self.assertEqual(pos["entry_creator"], "dev")
        self.assertEqual(pos["entry_creator_prior_launches"], 1)
        self.assertEqual(pos["entry_creator_hold_pct"], 1.0)
        self.assertIsNone(pos["entry_early_sell_pct"])
        self.assertEqual(pos["entry_seconds_after_graduation"], 30)
        self.assertTrue(pos["entry_in_boost_window"])
        self.assertEqual((pos["entry_history_total"], pos["entry_history_decoded"]), (1486, 1000))
        self.assertEqual(self.skips(), [])

    def test_one_party_fill_skipped_with_metadata(self):
        self.rpc.curve_transaction_count.return_value = 9
        self.enter()
        row = self.skips()[0]
        self.assertIn("one-party fill", row["reason"])
        self.assertEqual(row["curve_tx_count"], "9")
        self.assertEqual(row["seconds_after_graduation"], "30")
        self.assertEqual(row["in_boost_window"], "True")
        self.assertEqual(row["curve_age_seconds"], "3600.0")
        self.rpc.top_wallet_holder.assert_not_called()  # rejected before holder work

    def test_creator_dump_skipped_and_named(self):
        self.rpc.largest_seller_since.return_value = ("dev", 78.3)
        self.enter()
        row = self.skips()[0]
        self.assertIn("already sold 78.3%", row["reason"])
        self.assertEqual(row["early_seller"], "dev")
        self.rpc.bundle_snapshot.assert_not_called()

    def test_factory_creator_and_heavy_holding_skipped(self):
        self.rpc.creator_profile.return_value = {"prior_launches": 12}
        self.enter()
        self.assertIn("launch factory", self.skips()[0]["reason"])
        self.bot.state["positions"] = []
        self.rpc.creator_profile.return_value = {"prior_launches": 0}
        self.rpc.token_balance.return_value = int(0.30 * 2e9 * 10**6)
        self.enter()
        self.assertIn("creator still holds 30.0%", self.skips()[1]["reason"])

    def test_lookup_failures_fail_open_for_new_layers(self):
        self.rpc.curve_transaction_count.side_effect = RuntimeError("rpc down")
        self.rpc.creator_profile.side_effect = RuntimeError("rpc down")
        self.rpc.largest_seller_since.side_effect = RuntimeError("rpc down")
        self.enter()
        self.assertEqual(len(self.bot.state["positions"]), 1)
        self.assertIsNone(self.bot.state["positions"][0]["entry_curve_tx_count"])

    def test_exit_records_boost_window_on_both_ends(self):
        self.enter()
        pos = self.bot.state["positions"][0]
        pos["last_value_usd"] = 20.0
        self.bot.jup.quote = lambda a, b, amt, **kw: {"outAmount": "200000000"}  # $20 at $100/SOL
        with mock.patch.object(self.executor, "now_ts", return_value=GRAD + 900):
            self.bot.close_position(pos, "take_profit", 100.0)
        trades = list(csv.DictReader(open(Path(os.environ["DATA_DIR"]) / "live_trades.csv")))
        row = trades[-1]
        self.assertEqual(row["entry_in_boost_window"], "True")
        self.assertEqual(row["exit_seconds_after_graduation"], "900")
        self.assertEqual(row["exit_in_boost_window"], "False")
        self.assertEqual(row["entry_curve_tx_count"], "640")


if __name__ == "__main__":
    unittest.main()
