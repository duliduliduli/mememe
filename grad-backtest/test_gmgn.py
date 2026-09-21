import json
import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="grad-gmgn-test-"))

import gmgn


class FakeResponse:
    def __init__(self, payload, status=200, headers=None):
        self.payload, self.status_code, self.headers = payload, status, headers or {}

    def json(self):
        return self.payload


def stats_row(wallet, realized, winrate, buys, sells, tags=None):
    return {"wallet_address": wallet, "realized_profit": realized, "winrate": winrate, "buy_count": buys,
            "sell_count": sells, "pnl": realized / 1000, "total_cost": 1000, "common": {"tags": tags or []}}


class ClientTests(unittest.TestCase):
    def test_request_carries_key_timestamp_and_client_id(self):
        client = gmgn.Gmgn(api_key="k123")
        calls = []
        client.session.request = lambda method, url, **kw: (calls.append((method, url, kw)), FakeResponse({"code": 0, "data": {"list": [{"wallet_address": "w1"}]}}))[1]
        rows = client.wallet_stats("sol", ["w1", "w2"], "30d")
        self.assertEqual(rows, [{"wallet_address": "w1"}])
        method, url, kw = calls[0]
        self.assertEqual((method, url), ("GET", "https://openapi.gmgn.ai/v1/user/wallet_stats"))
        self.assertEqual(kw["headers"]["X-APIKEY"], "k123")
        params = dict((k, v) for k, v in kw["params"] if k not in ("wallet_address",))
        self.assertEqual(params["chain"], "sol")
        self.assertEqual(params["period"], "30d")
        self.assertIn("timestamp", params)
        self.assertIn("client_id", params)
        self.assertEqual([v for k, v in kw["params"] if k == "wallet_address"], ["w1", "w2"])

    def test_api_error_and_rate_limit_retry(self):
        client = gmgn.Gmgn(api_key="k")
        responses = [FakeResponse({"code": 429, "message": "RATE_LIMIT_EXCEEDED", "upgrade_url": "https://gmgn.ai/ai"}, 429),
                     FakeResponse({"code": 0, "data": []})]
        client.session.request = lambda *a, **k: responses.pop(0)
        with mock.patch.object(gmgn.time, "sleep"):
            self.assertEqual(client.smart_money("sol"), [])
        client.session.request = lambda *a, **k: FakeResponse({"code": 40001, "message": "invalid api key"}, 401)
        with self.assertRaises(gmgn.GmgnError):
            client.top_traders("sol", "Mint")
        with self.assertRaises(gmgn.GmgnError):
            gmgn.Gmgn(api_key="")

    def test_top_traders_query(self):
        client = gmgn.Gmgn(api_key="k")
        calls = []
        client.session.request = lambda method, url, **kw: (calls.append((url, dict(kw["params"]))), FakeResponse({"code": 0, "data": {"list": [
            {"address": "trader1", "realized_profit": 1234.5, "profit_change": 2.1, "history_bought_cost": 500, "tags": ["smart_degen"], "maker_token_tags": ["top_holder"], "name": "Bob"}]}}))[1]
        rows = client.top_traders("sol", "Mint", tag="smart_degen", limit=5)
        self.assertTrue(calls[0][0].endswith("/v1/market/token_top_traders"))
        self.assertEqual(calls[0][1]["tag"], "smart_degen")
        self.assertEqual(calls[0][1]["limit"], "5")
        summary = gmgn.traders_summary(rows)
        self.assertEqual(summary[0]["wallet"], "trader1")
        self.assertEqual(summary[0]["tags"], ["smart_degen", "top_holder"])
        self.assertEqual(summary[0]["realized_profit"], 1234.5)


class ScreenTests(unittest.TestCase):
    def test_verdicts(self):
        rows = [stats_row("winner", 5000, 0.62, 40, 35, ["smart_money"]),
                stats_row("loser", -800, 0.30, 60, 50),
                stats_row("spammer", 900, 0.55, 2400, 900),        # 80 buys a day
                stats_row("thin", 50, 1.0, 2, 1),
                stats_row("bot", 700, 0.7, 100, 90, ["bundler"])]
        results = {r["wallet"]: r for r in gmgn.screen_rows(rows, 30)}
        self.assertEqual(results["winner"]["verdict"], "copy")
        self.assertEqual(results["loser"]["verdict"], "skip")
        self.assertIn("lost $800", results["loser"]["why"])
        self.assertEqual(results["spammer"]["verdict"], "skip")
        self.assertIn("buys a day", results["spammer"]["why"])
        self.assertEqual(results["thin"]["verdict"], "thin")
        self.assertEqual(results["bot"]["verdict"], "skip")
        self.assertIn("bundler", results["bot"]["why"])

    def test_screen_keeps_request_order_and_reports_missing(self):
        client = gmgn.Gmgn(api_key="k")
        client.wallet_stats = lambda chain, wallets, period: [stats_row("b", 10, 0.5, 20, 10), stats_row("a", 20, 0.5, 20, 10)]
        results = gmgn.screen(client, ["a", "b", "c"])
        self.assertEqual([r["wallet"] for r in results], ["a", "b", "c"])
        self.assertEqual(results[2]["verdict"], "unknown")
        lines = gmgn.screen_lines(results)
        self.assertTrue(lines[0].startswith("GMGN OK   a:"))
        self.assertTrue(lines[2].startswith("GMGN ??   c:"))


class ExecutorHookTests(unittest.TestCase):
    def test_startup_screening_logs_and_warns(self):
        from test_copy import fresh, WALLET, OTHER
        executor, p = fresh(GMGN_API_KEY="k", COPY_WALLETS=f"{WALLET}, {OTHER}")
        self.addCleanup(p.stop)
        ex = executor.Executor(executor.Config())
        logged = []
        with mock.patch.object(gmgn.Gmgn, "wallet_stats", lambda self, chain, wallets, period: [stats_row(WALLET, 900, 0.6, 30, 20), stats_row(OTHER, -50, 0.2, 30, 20)]), \
             mock.patch.object(executor, "log", lambda msg: logged.append(msg)):
            ex.screen_copy_wallets(wait=True)
        self.assertTrue(any(f"GMGN OK   {WALLET}" in m for m in logged), logged)
        self.assertEqual(ex.state["gmgn_verdicts"][OTHER]["verdict"], "skip")
        self.assertGreater(float(ex.state["gmgn_success_ts"]), 0)
        self.assertTrue(any("not worth copying" in m and OTHER[:8] in m for m in logged), logged)
        # No key: nothing happens, nothing breaks.
        executor2, p2 = fresh(GMGN_API_KEY="")
        self.addCleanup(p2.stop)
        logged.clear()
        with mock.patch.object(executor2, "log", lambda msg: logged.append(msg)):
            executor2.Executor(executor2.Config()).screen_copy_wallets(wait=True)
        self.assertEqual(logged, [])

    def test_failed_screening_keeps_old_verdicts_and_retries_sooner(self):
        from test_copy import fresh, WALLET
        executor, p = fresh(GMGN_API_KEY="k", COPY_WALLETS=WALLET, GMGN_RETRY_MINUTES="60", GMGN_REFRESH_HOURS="24")
        self.addCleanup(p.stop)
        ex = executor.Executor(executor.Config())
        ex.state["gmgn_verdicts"] = {WALLET: {"verdict": "copy", "why": "old", "evaluated_at": "2026-09-20T00:00:00Z"}}
        logged = []
        def boom(self, chain, wallets, period):
            raise RuntimeError("GMGN 500")
        with mock.patch.object(gmgn.Gmgn, "wallet_stats", boom), mock.patch.object(executor, "log", lambda m: logged.append(m)):
            ex.screen_copy_wallets(wait=True)
        self.assertEqual(ex.state["gmgn_verdicts"][WALLET]["verdict"], "copy")     # previous verdicts kept
        self.assertTrue(any("keeping the previous verdicts" in m for m in logged), logged)
        self.assertGreater(float(ex.state["gmgn_attempt_ts"]), 0)
        self.assertEqual(float(ex.state.get("gmgn_success_ts") or 0), 0)
        # Next attempt is due after the retry interval, not the full refresh interval.
        started = []
        ex.screen_copy_wallets = lambda wait=False: started.append(1)
        ex.maybe_refresh_gmgn()
        self.assertEqual(started, [])
        ex.state["gmgn_attempt_ts"] = executor.now_ts() - 61 * 60
        ex.maybe_refresh_gmgn()
        self.assertEqual(started, [1])


class ServerEndpointTests(unittest.TestCase):
    def test_endpoints(self):
        from fastapi.testclient import TestClient
        import server
        client = TestClient(server.app)
        with mock.patch.dict(os.environ, {"GMGN_API_KEY": ""}):
            self.assertEqual(client.get("/api/gmgn/screen?wallets=a").status_code, 503)
        with mock.patch.dict(os.environ, {"GMGN_API_KEY": "k"}), \
             mock.patch.object(gmgn.Gmgn, "wallet_stats", lambda self, chain, wallets, period: [stats_row("a", 100, 0.5, 20, 10)]), \
             mock.patch.object(gmgn.Gmgn, "top_traders", lambda self, chain, token, tag=None, **kw: [{"address": "t1", "realized_profit": 5, "tags": ["renowned"]}]):
            self.assertEqual(client.get("/api/gmgn/screen").status_code, 400)
            body = client.get("/api/gmgn/screen?wallets=a, b").json()
            self.assertEqual([r["wallet"] for r in body["results"]], ["a", "b"])
            self.assertEqual(body["results"][0]["verdict"], "copy")
            self.assertEqual(body["results"][1]["verdict"], "unknown")
            traders = client.get("/api/gmgn/traders?token=Mint&tag=renowned").json()
            self.assertEqual(traders["traders"][0]["wallet"], "t1")
            self.assertEqual(client.get("/api/gmgn/traders").status_code, 400)


if __name__ == "__main__":
    unittest.main()
