"""Tests for the launch sniper lane: the server wiring in Python, and the Node parsing
helpers through `node --test` when a Node runtime is available (CI and the container)."""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import server

HERE = Path(__file__).parent
KEYS = ("XAI_API_KEY", "TWITTER_BEARER_TOKEN", "SNIPER_AUTOSTART", "SNIPER_DRY_RUN", "DRY_RUN", "SNIPER_STATE_FILE",
        "SNIPER_TARGET", "TARGET_USERNAME")


class SniperServerTests(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in KEYS}
        for k in KEYS:
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _autostart(self, **env):
        os.environ.update(env)
        started = []
        with patch.object(server, "_start_sniper", lambda: started.append(True)), \
                patch.object(server, "_sniper_running", lambda: False):
            server.maybe_autostart_sniper()
        return bool(started)

    def test_starts_only_when_a_source_key_exists(self):
        self.assertFalse(self._autostart())
        self.assertTrue(self._autostart(XAI_API_KEY="k"))
        self.assertTrue(self._autostart(TWITTER_BEARER_TOKEN="t"))

    def test_explicit_switch_wins(self):
        self.assertFalse(self._autostart(XAI_API_KEY="k", SNIPER_AUTOSTART="0"))
        self.assertTrue(self._autostart(SNIPER_AUTOSTART="1"))

    def test_dry_run_default_and_override(self):
        self.assertTrue(server._sniper_dry_run())
        os.environ["SNIPER_DRY_RUN"] = "false"
        self.assertFalse(server._sniper_dry_run())
        os.environ["SNIPER_DRY_RUN"] = "0"
        self.assertFalse(server._sniper_dry_run())

    def test_status_reads_state_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "s.json"
            state.write_text(json.dumps({"bought": True, "buy": {"hash": "abc", "chain": "Solana"}, "seen": ["1", "2"], "pending": {}}))
            os.environ.update({"SNIPER_STATE_FILE": str(state), "XAI_API_KEY": "k", "SNIPER_TARGET": "@HunterBiden"})
            body = json.loads(server.sniper_status().body)
        self.assertEqual(body["target"], "hunterbiden")
        self.assertEqual(body["source"], "grok")
        self.assertTrue(body["bought"])
        self.assertEqual(body["buy"]["hash"], "abc")
        self.assertEqual(body["seen_posts"], 2)
        self.assertTrue(body["dry_run"])


@unittest.skipUnless(shutil.which("node"), "node runtime not available")
class SniperNodeTests(unittest.TestCase):
    def test_parsing_helpers(self):
        proc = subprocess.run(["node", "--test", "lib.test.mjs"], cwd=HERE / "sniper", capture_output=True, text=True, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_main_script_parses(self):
        proc = subprocess.run(["node", "--check", "hunter-sniper.mjs"], cwd=HERE / "sniper", capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)


if __name__ == "__main__":
    unittest.main()
