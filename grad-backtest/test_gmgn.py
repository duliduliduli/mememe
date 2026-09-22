import json
import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="grad-gmgn-test-"))
os.environ["GMGN_UNITS_PER_SECOND"] = "100000"      # the shared limiter never waits in tests unless one sets a rate

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
    def setUp(self):
        gmgn.LIMITER.reset()
        self.addCleanup(gmgn.LIMITER.reset)

    def test_request_carries_key_timestamp_and_client_id(self):
        client = gmgn.Gmgn(api_key="k123")
        client.pause_seconds = 0
        calls = []
        client.session.request = lambda method, url, **kw: (calls.append((method, url, kw)), FakeResponse({"code": 0, "data": {"wallet_address": dict(kw["params"])["wallet_address"]}}))[1]
        rows = client.wallet_stats("sol", ["w1", "w2"], "30d")
        self.assertEqual(rows, [{"wallet_address": "w1"}, {"wallet_address": "w2"}])
        method, url, kw = calls[0]
        self.assertEqual((method, url), ("GET", "https://openapi.gmgn.ai/v1/user/wallet_stats"))
        self.assertEqual(kw["headers"]["X-APIKEY"], "k123")
        params = dict(kw["params"])
        self.assertEqual((params["chain"], params["period"], params["wallet_address"]), ("sol", "30d", "w1"))
        self.assertIn("timestamp", params)
        self.assertIn("client_id", params)
        self.assertNotEqual(dict(calls[0][2]["params"])["client_id"], dict(calls[1][2]["params"])["client_id"])

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


class LimiterTests(unittest.TestCase):
    """Every GMGN call in the process shares one budget, and a ban holds every caller."""

    def setUp(self):
        gmgn.LIMITER.reset()
        self.addCleanup(gmgn.LIMITER.reset)
        self.clock = [1000.0]
        self.slept = []
        def sleep(seconds):
            self.slept.append(round(seconds, 3))
            self.clock[0] += seconds
        for target, value in (("time", lambda: self.clock[0]), ("sleep", sleep)):
            patcher = mock.patch.object(gmgn.time, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def client(self, responses):
        c = gmgn.Gmgn(api_key="k")
        c.pause_seconds = 0
        c.session.request = lambda *a, **k: responses.pop(0)
        return c

    def test_separate_clients_share_one_budget(self):
        ok = lambda: FakeResponse({"code": 0, "data": {}})
        with mock.patch.dict(os.environ, {"GMGN_UNITS_PER_SECOND": "5"}):
            a, b = self.client([ok()]), self.client([ok(), ok()])
            a.request("GET", "/v1/user/wallet_stats")       # weight 3: the next call waits 0.6s
            b.request("GET", "/v1/user/wallet_stats")
            b.request("GET", "/v1/market/token_top_traders")  # after weight 3 again: 0.6s
        self.assertEqual(self.slept, [0.6, 0.6])

    def test_a_ban_holds_every_caller_until_it_lifts(self):
        banned = FakeResponse({"code": 429, "message": "IP is temporarily banned due to repeated rate limit violations",
                               "reset_at": 1100}, 429)
        a = self.client([banned, FakeResponse({"code": 0, "data": []})])
        self.assertEqual(a.smart_money("sol"), [])
        self.assertEqual(self.slept, [101.0])               # waited out the ban once, then retried
        self.clock[0] = 1050.0                              # another thread, mid-ban
        gmgn.LIMITER.block(1101.0)
        b = self.client([FakeResponse({"code": 0, "data": []})])
        b.kol("sol")
        self.assertEqual(self.slept[-1], 51.0)

    def test_ban_wait_is_capped_and_a_second_refusal_gives_up(self):
        refused = lambda: FakeResponse({"code": 429, "message": "banned", "reset_at": 1_000_000_000_000_000}, 429)
        c = self.client([refused(), refused()])
        with self.assertRaises(gmgn.GmgnError) as err:
            c.smart_money("sol")
        self.assertEqual(self.slept, [gmgn.MAX_BAN_WAIT_SECONDS])
        self.assertIn("rate limited", str(err.exception))
        import scout
        self.assertTrue(scout.rate_limited(err.exception))

    def test_reset_in_milliseconds_and_header(self):
        self.clock[0] = 1_800_000_000.0
        c = self.client([FakeResponse({"code": 429, "message": "slow", "reset_at": 1_800_000_010_000}, 429),
                         FakeResponse({"code": 0, "data": []})])
        c.smart_money("sol")
        self.assertEqual(self.slept, [11.0])
        c = self.client([FakeResponse({"code": 429, "message": "slow"}, 429, {"x-ratelimit-reset": "1800000030"}),
                         FakeResponse({"code": 0, "data": []})])
        c.smart_money("sol")
        self.assertEqual(self.slept[-1], 20.0)


REAL_ROW = {"wallet_address": "EC2f", "native_balance": "0", "realized_profit": "787901.678", "realized_profit_pnl": "0.505",
            "buy": 608, "sell": 792, "bought_cost": "2104329.36", "total_cost": "2405884.80",
            "pnl_stat": {"token_num": 293, "winrate": 0.3766, "avg_holding_period": 597867.39},
            "common": {"tags": ["padre", "arbitrager", "fomo"]}}


class RealShapeTests(unittest.TestCase):
    def test_wallet_stats_asks_once_per_wallet_and_parses_the_real_shape(self):
        client = gmgn.Gmgn(api_key="k")
        client.pause_seconds = 0
        calls = []
        def request(method, url, **kw):
            params = dict(kw["params"])
            calls.append(params["wallet_address"])
            return FakeResponse({"code": 0, "data": dict(REAL_ROW, wallet_address=params["wallet_address"])})
        client.session.request = request
        rows = client.wallet_stats("sol", ["a", "b"], "30d")
        self.assertEqual(calls, ["a", "b"])                                  # one request per wallet, not one with two
        self.assertEqual([r["wallet_address"] for r in rows], ["a", "b"])
        result = gmgn.screen_rows(rows, 30)[0]
        self.assertEqual(result["verdict"], "copy")
        self.assertAlmostEqual(result["winrate"], 0.3766)                    # from pnl_stat, not the top level
        self.assertEqual((result["buys"], result["sells"], result["tokens"]), (608, 792, 293))
        self.assertEqual(result["hold_hours"], 166.1)
        self.assertEqual(result["tags"], ["padre", "arbitrager", "fomo"])
        self.assertIn("38% of tokens won", result["why"])
        self.assertIn("avg hold 166h", result["why"])


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
