import base64
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="grad-dash-test-"))

from fastapi.testclient import TestClient

import server


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(server.app)
        self.data_dir = Path(os.environ["DATA_DIR"])

    def test_health(self):
        self.assertEqual(self.client.get("/healthz").json(), {"status": "ok"})

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


if __name__ == "__main__":
    unittest.main()
