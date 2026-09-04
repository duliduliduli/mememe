"""Operational fixes from the first live session after the guards shipped: 429s from Helius and
Jupiter during startup reconciliation, adopted dust bags occupying every entry slot, and a sell
that needed three attempts because the RPC node had not seen Jupiter's blockhash."""
import os
import tempfile
import unittest
from unittest import mock

import requests
from solders.keypair import Keypair


def fresh(**env):
    tmp = tempfile.mkdtemp(prefix="grad-rate-test-")
    base = {"DATA_DIR": tmp, "HELIUS_API_KEY": "k", "MIGRATION_ADDRESS": "a", "EXECUTOR_MODE": "paper"}
    base.update(env)
    patcher = mock.patch.dict(os.environ, base)
    patcher.start()
    import importlib
    import executor
    importlib.reload(executor)
    return executor, patcher


def http_error(code):
    resp = requests.Response()
    resp.status_code = code
    return requests.HTTPError(f"{code} Client Error", response=resp)


class BackoffTests(unittest.TestCase):
    def setUp(self):
        self.executor, p = fresh()
        self.addCleanup(p.stop)
        self.sleep = mock.patch.object(self.executor.time, "sleep")
        self.sleep.start()
        self.addCleanup(self.sleep.stop)

    def test_retries_429_then_succeeds(self):
        calls = []

        def fn():
            calls.append(1)
            if len(calls) < 3:
                raise http_error(429)
            return "ok"

        self.assertEqual(self.executor.with_backoff(fn, "x"), "ok")
        self.assertEqual(len(calls), 3)

    def test_other_errors_raise_immediately(self):
        calls = []

        def fn():
            calls.append(1)
            raise http_error(400)

        with self.assertRaises(requests.HTTPError):
            self.executor.with_backoff(fn, "x")
        self.assertEqual(len(calls), 1)

    def test_gives_up_after_backoff_schedule(self):
        calls = []

        def fn():
            calls.append(1)
            raise http_error(429)

        with self.assertRaises(requests.HTTPError):
            self.executor.with_backoff(fn, "x")
        self.assertEqual(len(calls), len(self.executor.RATE_LIMIT_BACKOFF) + 1)

    def test_rpc_and_jupiter_use_backoff(self):
        ex = self.executor
        rpc = ex.Rpc(ex.Config())
        seq = [http_error(429), {"value": 7}]

        def post(*a, **kw):
            item = seq.pop(0)
            if isinstance(item, Exception):
                raise item
            resp = mock.Mock()
            resp.raise_for_status = lambda: None
            resp.json = lambda: {"result": item}
            return resp

        rpc.session.post = post
        self.assertEqual(rpc.call("getBalance", ["x"]), {"value": 7})
        jup = ex.Jupiter(ex.Config())
        seq2 = [http_error(429), {"outAmount": "5"}]

        def get(*a, **kw):
            item = seq2.pop(0)
            if isinstance(item, Exception):
                raise item
            resp = mock.Mock()
            resp.raise_for_status = lambda: None
            resp.json = lambda: item
            return resp

        jup.session.get = get
        self.assertEqual(jup.quote("a", "b", 1)["outAmount"], "5")


class SolPriceCacheTests(unittest.TestCase):
    def test_price_cached_for_30s(self):
        executor, p = fresh()
        self.addCleanup(p.stop)
        ex = executor.Executor(executor.Config())
        calls = []
        ex.jup.quote = lambda *a, **kw: (calls.append(1), {"outAmount": "100000000"})[1]
        with mock.patch.object(executor, "now_ts", return_value=1000.0):
            self.assertEqual(ex.sol_price_usd(), 100.0)
            self.assertEqual(ex.sol_price_usd(), 100.0)
        with mock.patch.object(executor, "now_ts", return_value=1031.0):
            ex.sol_price_usd()
        self.assertEqual(len(calls), 2)


class AdoptedSlotTests(unittest.TestCase):
    def test_adopted_positions_do_not_take_entry_slots(self):
        executor, p = fresh(MAX_CONCURRENT_POSITIONS="3")
        self.addCleanup(p.stop)
        ex = executor.Executor(executor.Config())
        ex.state["paper_balance_usd"] = 100.0
        ex.state["positions"] = [
            {"mint": f"bag{i}", "tokens": 1, "position_usd": 2.0, "opened_ts": 0, "peak_usd": 2.0, "adopted": True}
            for i in range(3)
        ]
        ex.jup.quote = lambda a, b, amt, **kw: {"outAmount": "150000000000", "priceImpactPct": "0.01"}
        ex.rpc.call = lambda m, params: {
            "getTokenSupply": {"value": {"uiAmountString": "1000000000", "decimals": 6}},
            "getSignaturesForAddress": [{"signature": "s", "blockTime": 1}],
            "getTokenLargestAccounts": {"value": []},
        }[m]
        ex.rpc.bundle_snapshot = lambda *args, **kwargs: {
            "complete": True,
            "bundle_slot_pct": 1.0,
            "cluster_pct": 1.0,
            "dev_cluster_pct": 0.0,
            "top10_wallet_pct": 10.0,
            "early_buy_pct": 1.0,
            "funder_coverage_pct": 100.0,
        }
        with mock.patch.object(executor, "now_ts", return_value=1030.0):
            ex.try_enter({"mint": "fresh", "graduated_ts": 1000.0}, 100.0)
        self.assertEqual([p["mint"] for p in ex.state["positions"]][-1], "fresh")
        # a fourth bot-opened position is still blocked by the slot limit
        ex.state["positions"] += [{"mint": "own", "tokens": 1, "position_usd": 5.0, "opened_ts": 0, "peak_usd": 5.0}] * 2
        with mock.patch.object(executor, "now_ts", return_value=1030.0):
            ex.try_enter({"mint": "fresh2", "graduated_ts": 1000.0}, 100.0)
        self.assertNotIn("fresh2", [p["mint"] for p in ex.state["positions"]])


class BlockhashRetryTests(unittest.TestCase):
    def test_fresh_swap_built_when_blockhash_unknown(self):
        executor, p = fresh(EXECUTOR_MODE="live", WALLET_PRIVATE_KEY=str(Keypair()))
        self.addCleanup(p.stop)
        mock.patch.object(executor.time, "sleep").start()
        self.addCleanup(mock.patch.stopall)
        ex = executor.Executor(executor.Config())
        builds = []
        ex.jup.swap_transaction = lambda q, pk: (builds.append(1), b"raw")[1]
        ex.wallet.sign = lambda raw: b"signed"
        sends = [RuntimeError("RPC sendTransaction: {'code': -32002, 'err': 'BlockhashNotFound'}"), "sig1"]

        def send(raw):
            item = sends.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        ex.rpc.send_raw = send
        ex.rpc.confirmed = lambda sig: True
        self.assertEqual(ex.execute_swap({"outAmount": "1"}), "sig1")
        self.assertEqual(len(builds), 2)

    def test_other_send_errors_are_not_retried(self):
        executor, p = fresh(EXECUTOR_MODE="live", WALLET_PRIVATE_KEY=str(Keypair()))
        self.addCleanup(p.stop)
        ex = executor.Executor(executor.Config())
        ex.jup.swap_transaction = lambda q, pk: b"raw"
        ex.wallet.sign = lambda raw: b"signed"
        ex.rpc.send_raw = lambda raw: (_ for _ in ()).throw(RuntimeError("custom 6001"))
        with self.assertRaises(RuntimeError):
            ex.execute_swap({"outAmount": "1"})


if __name__ == "__main__":
    unittest.main()
