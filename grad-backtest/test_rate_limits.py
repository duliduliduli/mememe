"""Operational fixes from live sessions: provider/Jupiter 429s, dust bags occupying entry
slots, and swaps that need rebuilding when an RPC node has not seen Jupiter's blockhash."""
import os
import io
import json
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


class ProviderDiscoveryProtectionTests(unittest.TestCase):
    def test_rate_limit_circuit_breaker_grows_exponentially(self):
        executor, p = fresh()
        self.addCleanup(p.stop)
        ex = executor.Executor(executor.Config())
        with mock.patch.object(executor, "now_ts", return_value=1000.0):
            self.assertEqual(ex.note_provider_rate_limit(), 30.0)
        self.assertEqual(ex._provider_cooldown_until, 1030.0)
        with mock.patch.object(executor, "now_ts", return_value=1030.0):
            self.assertEqual(ex.note_provider_rate_limit(), 60.0)
        self.assertEqual(ex._provider_cooldown_until, 1090.0)

    def test_rate_limit_starts_cooldown_instead_of_hammering_every_cycle(self):
        executor, p = fresh()
        self.addCleanup(p.stop)
        ex = executor.Executor(executor.Config())
        call = mock.Mock(side_effect=http_error(429))
        with mock.patch.object(ex.rpc, "call", call), mock.patch.object(executor, "now_ts", return_value=1000.0):
            ex.poll_graduations()
            ex.poll_graduations()
        self.assertEqual(call.call_count, 1)
        self.assertEqual(ex._provider_cooldown_until, 1030.0)

    def test_exhausted_provider_starts_the_same_cooldown(self):
        executor, p = fresh()
        self.addCleanup(p.stop)
        ex = executor.Executor(executor.Config())
        call = mock.Mock(side_effect=http_error(403))
        with mock.patch.object(ex.rpc, "call", call), mock.patch.object(executor, "now_ts", return_value=1000.0):
            ex.poll_graduations()
            ex.poll_graduations()
        self.assertEqual(call.call_count, 1)
        self.assertEqual(ex._provider_cooldown_until, 1030.0)

    def test_same_mint_is_not_queued_twice(self):
        executor, p = fresh()
        self.addCleanup(p.stop)
        ex = executor.Executor(executor.Config())
        ex.pending.append({"mint": "m", "graduated_ts": 990, "enter_at": 1020})
        ex.rpc.call = mock.Mock(return_value=[{"signature": "new-sig", "blockTime": 998, "err": None}])
        ex.rpc.transaction = mock.Mock(return_value={"transaction": {}})
        with mock.patch.object(executor, "candidate_mints_from_rpc_transaction", return_value=["m"]), mock.patch.object(
            executor, "now_ts", return_value=1000.0
        ):
            ex.poll_graduations()
        self.assertEqual([item["mint"] for item in ex.pending], ["m"])

    def test_config_accepts_standard_rpc_without_helius(self):
        executor, p = fresh(HELIUS_API_KEY="", RPC_URL="https://rpc.example/key")
        self.addCleanup(p.stop)
        cfg = executor.Config()
        cfg.validate()
        self.assertEqual(cfg.rpc_urls, ["https://rpc.example/key"])
        self.assertEqual(cfg.transaction_history_mode, "raw")

    def test_keyless_defaults_ignore_existing_helius_key(self):
        executor, p = fresh(HELIUS_API_KEY="exhausted", RPC_URL="", RPC_URLS="",
                            RPC_WS_URL="", RPC_WS_URLS="")
        self.addCleanup(p.stop)
        cfg = executor.Config()
        cfg.validate()
        self.assertEqual(cfg.rpc_urls, list(executor.DEFAULT_RPC_URLS))
        self.assertEqual(cfg.rpc_ws_urls, [executor.websocket_url(u) for u in cfg.rpc_urls])
        self.assertFalse(any("helius" in u for u in cfg.rpc_urls))

    def test_explicit_endpoint_lists_override_defaults(self):
        executor, p = fresh(RPC_URL="https://ignored.example", RPC_URLS="https://custom.example",
                            RPC_WS_URL="wss://ignored.example", RPC_WS_URLS="wss://custom-ws.example")
        self.addCleanup(p.stop)
        cfg = executor.Config()
        self.assertEqual(cfg.rpc_urls, ["https://custom.example"])
        self.assertEqual(cfg.rpc_ws_urls, ["wss://custom-ws.example"])

    def test_websocket_429_uses_backup_without_waiting(self):
        executor, p = fresh(RPC_URLS="https://one.example,https://two.example",
                            RPC_WS_URL="", RPC_WS_URLS="")
        self.addCleanup(p.stop)
        stream = executor.MigrationStream(executor.Config())
        conn = mock.Mock()
        def acknowledged():
            stream.stop_event.set()
            return json.dumps({"jsonrpc": "2.0", "id": 1, "result": 123})
        conn.recv.side_effect = acknowledged
        with mock.patch.object(executor.websocket, "create_connection", side_effect=[http_error(429), conn]) as connect, mock.patch.object(executor, "log"), mock.patch.object(stream.stop_event, "wait") as wait:
            stream._run()
        self.assertEqual([c.args[0] for c in connect.call_args_list],
                         ["wss://one.example", "wss://two.example"])
        wait.assert_not_called()
        conn.close.assert_called_once()

    def test_websocket_all_unavailable_backs_off_per_endpoint(self):
        executor, p = fresh(RPC_URLS="https://one.example,https://two.example",
                            RPC_WS_URL="", RPC_WS_URLS="")
        self.addCleanup(p.stop)
        stream = executor.MigrationStream(executor.Config())
        clock = [1000.0]
        waits = []
        def advance(delay):
            waits.append(delay)
            clock[0] += delay
            if len(waits) == 3:
                stream.stop_event.set()
        with mock.patch.object(executor.websocket, "create_connection", side_effect=http_error(429)) as connect, mock.patch.object(executor.time, "monotonic", side_effect=lambda: clock[0]), mock.patch.object(stream.stop_event, "wait", side_effect=advance), mock.patch.object(executor, "log"):
            stream._run()
        self.assertEqual(waits, [30.0, 60.0, 120.0])
        self.assertEqual(connect.call_count, 6)

    def test_rpc_fails_over_from_rate_limited_primary(self):
        executor, p = fresh(HELIUS_API_KEY="", RPC_URLS="https://one.example,https://two.example")
        self.addCleanup(p.stop)
        rpc = executor.Rpc(executor.Config())
        calls = []

        def post(url, **kwargs):
            calls.append(url)
            if url == "https://one.example":
                raise http_error(429)
            response = mock.Mock()
            response.raise_for_status.return_value = None
            response.json.return_value = {"result": {"value": 7}}
            return response

        rpc.session.post = post
        self.assertEqual(rpc.call("getBalance", ["wallet"]), {"value": 7})
        self.assertEqual(calls, ["https://one.example", "https://two.example"])
        self.assertEqual(rpc._active_endpoint, 1)

    def test_rpc_fails_over_from_malformed_primary_response(self):
        executor, p = fresh(HELIUS_API_KEY="", RPC_URLS="https://one.example,https://two.example")
        self.addCleanup(p.stop)
        rpc = executor.Rpc(executor.Config())

        def post(url, **kwargs):
            response = mock.Mock()
            response.raise_for_status.return_value = None
            response.json.return_value = None if url == "https://one.example" else {"result": {"value": 8}}
            return response

        rpc.session.post = post
        self.assertEqual(rpc.call("getBalance", ["wallet"]), {"value": 8})

    def test_rpc_batch_fails_over_from_json_rate_limit(self):
        executor, p = fresh(HELIUS_API_KEY="", RPC_URLS="https://one.example,https://two.example")
        self.addCleanup(p.stop)
        rpc = executor.Rpc(executor.Config())
        calls = []

        def post(url, **kwargs):
            calls.append(url)
            response = mock.Mock()
            response.raise_for_status.return_value = None
            response.json.return_value = (
                [{"jsonrpc": "2.0", "id": 1, "error": {"code": -32005, "message": "Too many requests"}}]
                if url == "https://one.example"
                else [{"jsonrpc": "2.0", "id": 1, "result": {"value": 9}}]
            )
            return response

        rpc.session.post = post
        self.assertEqual(rpc.batch_call([("getBalance", ["wallet"])]), [{"value": 9}])
        self.assertEqual(calls, ["https://one.example", "https://two.example"])

    def test_print_config_redacts_all_provider_credentials(self):
        secret = "secret-path-key"
        executor, p = fresh(
            HELIUS_API_KEY="secret-helius-key",
            RPC_URL=f"https://provider.example/{secret}?api-key=also-secret",
        )
        self.addCleanup(p.stop)
        output = io.StringIO()
        with mock.patch.object(executor.sys, "argv", ["executor.py", "--print-config"]), mock.patch(
            "sys.stdout", output
        ):
            executor.main()
        text = output.getvalue()
        self.assertNotIn(secret, text)
        self.assertNotIn("secret-helius-key", text)
        parsed = json.loads(text)
        self.assertEqual(parsed["rpc_urls"], ["https://provider.example/…"])

    def test_raw_transaction_parser_finds_mint_and_transfers(self):
        executor, p = fresh()
        self.addCleanup(p.stop)
        mint = "Mint111111111111111111111111111111111111111"
        dex = next(iter(executor.DEX_PROGRAMS))
        tx = {
            "slot": 10,
            "blockTime": 1000,
            "transaction": {
                "signatures": ["sig"],
                "message": {
                    "accountKeys": [
                        {"pubkey": "buyer", "signer": True},
                        {"pubkey": dex, "signer": False},
                        {"pubkey": "vault", "signer": False},
                        {"pubkey": "buyer-token", "signer": False},
                    ],
                    "instructions": [
                        {
                            "program": "system",
                            "parsed": {
                                "type": "transfer",
                                "info": {"source": "funder", "destination": "buyer", "lamports": 200000},
                            },
                        },
                        {"programId": dex, "accounts": [], "data": "x"},
                    ],
                },
            },
            "meta": {
                "preTokenBalances": [
                    {"accountIndex": 2, "mint": mint, "owner": "vault-owner", "uiTokenAmount": {"amount": "100", "decimals": 0}},
                    {"accountIndex": 3, "mint": mint, "owner": "buyer", "uiTokenAmount": {"amount": "0", "decimals": 0}},
                ],
                "postTokenBalances": [
                    {"accountIndex": 2, "mint": mint, "owner": "vault-owner", "uiTokenAmount": {"amount": "40", "decimals": 0}},
                    {"accountIndex": 3, "mint": mint, "owner": "buyer", "uiTokenAmount": {"amount": "60", "decimals": 0}},
                ],
            },
        }
        self.assertEqual(executor.candidate_mints_from_rpc_transaction(tx), [mint])
        normalized = executor.normalize_rpc_transaction(tx)
        self.assertEqual(normalized["type"], "SWAP")
        self.assertEqual(normalized["feePayer"], "buyer")
        self.assertEqual(normalized["nativeTransfers"][0]["amount"], 200000)
        self.assertTrue(any(t["mint"] == mint and t["toUserAccount"] == "buyer" for t in normalized["tokenTransfers"]))


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
