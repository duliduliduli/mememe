import base64
import os
import tempfile
import unittest
from unittest import mock
from unittest.mock import patch
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="grad-dash-test-"))

from fastapi.testclient import TestClient

import server


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(server.app)
        self.data_dir = Path(os.environ["DATA_DIR"])

    def test_healthz_shows_commit_and_copy_settings_without_secrets(self):
        with mock.patch.dict(os.environ, {"RAILWAY_GIT_COMMIT_SHA": "abc123def", "MAX_CONCURRENT_POSITIONS": "5",
                                          "COPY_WALLETS": "FY6yG7cy886yAzndYue7Tb5q5mj3nWEHNi3PjGowd4Ns", "GMGN_API_KEY": "secret",
                                          "WALLET_PRIVATE_KEY": "never"}):
            body = self.client.get("/healthz").json()
        self.assertEqual(body["commit"], "abc123def")
        self.assertEqual(body["settings"]["MAX_CONCURRENT_POSITIONS"], "5")
        self.assertEqual(body["copy_wallets"], ["FY6yG7cy…"])
        self.assertTrue(body["gmgn_key_set"])
        self.assertNotIn("secret", str(body))
        self.assertNotIn("never", str(body))

    def test_evm_status_reports_unconfigured_lane(self):
        with mock.patch.dict(os.environ, {"EVM_COPY_WALLETS": "", "EVM_PRIVATE_KEY": ""}):
            resp = self.client.get("/api/evm")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertFalse(body["configured"])
        self.assertFalse(body["running"])
        with mock.patch.dict(os.environ, {"EVM_COPY_WALLETS": "0x1111111111111111111111111111111111111111", "EXECUTOR_MODE": "live", "EVM_PRIVATE_KEY": ""}):
            self.assertFalse(server._evm_configured())              # live needs a key
        with mock.patch.dict(os.environ, {"EVM_COPY_WALLETS": "0x1111111111111111111111111111111111111111", "EXECUTOR_MODE": "paper", "EVM_PRIVATE_KEY": ""}):
            self.assertTrue(server._evm_configured())

    def test_mm_lane_stays_off_in_copy_only_deployments(self):
        with mock.patch.dict(os.environ, {"COPY_WALLETS": "w1", "COPY_ONLY": "1"}, clear=False):
            os.environ.pop("MM_AUTOSTART", None)
            self.assertFalse(server._mm_autostart())
        with mock.patch.dict(os.environ, {"COPY_WALLETS": "w1", "MM_AUTOSTART": "1"}):
            self.assertTrue(server._mm_autostart())
        with mock.patch.dict(os.environ, {"COPY_WALLETS": ""}, clear=False):
            os.environ.pop("MM_AUTOSTART", None)
            self.assertTrue(server._mm_autostart())

    def test_health(self):
        self.assertEqual(self.client.get("/healthz").json()["status"], "ok")

    def test_overview_with_no_data(self):
        body = self.client.get("/api/overview").json()
        self.assertEqual(body["stats"]["trade_count"], 0)
        self.assertEqual(body["equity"], [])

    def test_overview_with_trades(self):
        (self.data_dir / "trade_results.csv").write_text(
            "mint_address,entry_timestamp,exit_reason,net_return\n"
            "mintA,2026-08-01T00:00:00Z,take_profit,0.6\n"
            "mintB,2026-08-01T01:00:00Z,stop_loss,-0.34\n"
        )
        body = self.client.get("/api/overview").json()
        self.assertEqual(body["stats"]["trade_count"], 2)
        self.assertEqual(body["stats"]["win_rate"], 0.5)
        self.assertEqual(len(body["equity"]), 3)
        self.assertEqual(body["exit_reasons"], {"take_profit": 1, "stop_loss": 1})

    def test_run_requires_token(self):
        self.assertEqual(self.client.post("/api/run", json={"stage": "run"}).status_code, 503)
        os.environ["ADMIN_TOKEN"] = "secret"
        try:
            resp = self.client.post("/api/run", json={"stage": "run"}, headers={"x-admin-token": "wrong"})
            self.assertEqual(resp.status_code, 401)
            resp = self.client.post("/api/run", json={"stage": "rm -rf"}, headers={"x-admin-token": "secret"})
            self.assertEqual(resp.status_code, 400)
        finally:
            del os.environ["ADMIN_TOKEN"]

    def test_log_viewer_requires_basic_auth(self):
        old_admin = os.environ.pop("ADMIN_TOKEN", None)
        old_viewer = os.environ.pop("LOG_VIEWER_TOKEN", None)
        try:
            self.assertEqual(self.client.get("/logs").status_code, 503)
            os.environ["LOG_VIEWER_TOKEN"] = "phone-secret"
            response = self.client.get("/logs")
            self.assertEqual(response.status_code, 401)
            self.assertIn("Basic", response.headers["www-authenticate"])
            self.assertEqual(response.headers["cache-control"], "no-store")
            wrong = base64.b64encode(b"admin:wrong").decode()
            self.assertEqual(self.client.get("/logs", headers={"Authorization": f"Basic {wrong}"}).status_code, 401)
            good = base64.b64encode(b"admin:phone-secret").decode()
            response = self.client.get("/logs", headers={"Authorization": f"Basic {good}"})
            self.assertEqual(response.status_code, 200)
            self.assertIn("Copy All", response.text)
        finally:
            os.environ.pop("LOG_VIEWER_TOKEN", None)
            if old_admin is not None:
                os.environ["ADMIN_TOKEN"] = old_admin
            if old_viewer is not None:
                os.environ["LOG_VIEWER_TOKEN"] = old_viewer

    def test_runtime_logs_filters_by_time_and_terms(self):
        old_viewer = os.environ.get("LOG_VIEWER_TOKEN")
        os.environ["LOG_VIEWER_TOKEN"] = "phone-secret"
        now = datetime.now(timezone.utc)
        fresh = (now - timedelta(minutes=10)).isoformat().replace("+00:00", "Z")
        old = (now - timedelta(hours=8)).isoformat().replace("+00:00", "Z")
        (self.data_dir / "executor.log").write_text(
            f"{old} BUNDLE old-entry\n"
            f"{fresh} HEARTBEAT fine\n"
            f"{fresh} BUNDLE mint-abc complete\n"
            f"{fresh} BUNDLE other complete\n"
        )
        auth = base64.b64encode(b"admin:phone-secret").decode()
        try:
            response = self.client.get(
                "/api/runtime-logs?hours=6&q=BUNDLE%20mint-abc",
                headers={"Authorization": f"Basic {auth}"},
            )
            self.assertEqual(response.status_code, 200)
            body = response.json()
            self.assertEqual(body["returned"], 1)
            self.assertIn("mint-abc", body["log"])
            self.assertNotIn("old-entry", body["log"])
            self.assertEqual(response.headers["cache-control"], "no-store")
        finally:
            if old_viewer is None:
                os.environ.pop("LOG_VIEWER_TOKEN", None)
            else:
                os.environ["LOG_VIEWER_TOKEN"] = old_viewer


class AutostartTests(unittest.TestCase):
    KEYS = ("EXECUTOR_AUTOSTART", "EXECUTOR_MODE", "WALLET_PRIVATE_KEY", "MM_AUTOSTART", "MM_MODE", "RUN_BOTH_LANES")

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in self.KEYS}
        for k in self.KEYS:
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _run(self, **env):
        os.environ.update(env)
        started = []
        with patch.object(server, "_start_executor", lambda: started.append(True)), \
                patch.object(server, "_executor_running", lambda: False):
            server.maybe_autostart_executor()
        return bool(started)

    def test_executor_stays_off_when_mm_lane_is_live(self):
        self.assertFalse(self._run(EXECUTOR_AUTOSTART="1", EXECUTOR_MODE="live", WALLET_PRIVATE_KEY="k", MM_MODE="live"))

    def test_run_both_lanes_restores_executor(self):
        self.assertTrue(self._run(EXECUTOR_AUTOSTART="1", EXECUTOR_MODE="live", WALLET_PRIVATE_KEY="k",
                                  MM_MODE="live", RUN_BOTH_LANES="1"))

    def test_executor_runs_when_mm_lane_is_paper_or_off(self):
        self.assertTrue(self._run(EXECUTOR_AUTOSTART="1", EXECUTOR_MODE="paper"))
        # A live executor with a wallet no longer promotes the mm lane to live by itself.
        self.assertTrue(self._run(EXECUTOR_AUTOSTART="1", EXECUTOR_MODE="live", WALLET_PRIVATE_KEY="k"))
        self.assertTrue(self._run(EXECUTOR_AUTOSTART="1", EXECUTOR_MODE="live", WALLET_PRIVATE_KEY="k",
                                  MM_AUTOSTART="0"))

    def test_executor_autostart_still_opt_in(self):
        self.assertFalse(self._run(EXECUTOR_MODE="live", WALLET_PRIVATE_KEY="k", RUN_BOTH_LANES="1"))


if __name__ == "__main__":
    unittest.main()


class ScoutFunnelEndpointTests(unittest.TestCase):
    def test_scout_report_carries_the_funnel_and_jev_counts(self):
        import json as _json
        client = TestClient(server.app)
        state = {"scout": {"candidates": {"W1": {"address": "W1", "state": "shadow", "evaluation": {"failed": ["hold_time"]}}},
                           "trades": [{"wallet": "W1", "closed_ts": 1, "pnl_base": 1.5, "pnl_stress": 1.0}]},
                 "jev": {"calls": 3, "buy": 1, "skip": 2}}
        with patch.object(server, "EXECUTOR_STATE", Path(os.environ["DATA_DIR"]) / "funnel_state.json"):
            server.EXECUTOR_STATE.write_text(_json.dumps(state))
            body = client.get("/api/scout").json()
        self.assertEqual(body["gate_failures"], {"hold_time": 1})
        self.assertEqual(body["funnel"]["watchlist"]["size"], 1)
        self.assertEqual(body["funnel"]["paper_pnl_by_wallet"][0]["net_base_usd"], 1.5)
        self.assertEqual(body["funnel"]["jev"], {"calls": 3, "buy": 1, "skip": 2})
        self.assertIn("jev", client.get("/healthz").json())


class ExpectancyEndpointTests(unittest.TestCase):
    def test_expectancy_endpoint_reads_the_data_dir(self):
        client = TestClient(server.app)
        (Path(os.environ["DATA_DIR"]) / "live_trades.csv").write_text(
            "opened_at,closed_at,mint,position_usd,exit_usd,net_return,exit_reason,buy_signature\nt,t,m,10,12,0.2,take_profit,sig\n")
        body = client.get("/api/copy/expectancy").json()
        self.assertEqual(body["live"]["overall"]["trades"], 1)
        self.assertEqual(body["live"]["unattributed"]["trades"], 1)
        self.assertIn("convergence", body)
