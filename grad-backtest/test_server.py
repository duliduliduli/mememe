import os
import tempfile
import unittest
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


if __name__ == "__main__":
    unittest.main()
