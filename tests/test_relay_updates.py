"""The console's update endpoints (server/relay/updates.py): admin only, the core's
status file, the request files, and asking first when someone is watching on the PC."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from server.relay import updates

import test_relay_watch as tw


class UpdateEndpointTests(unittest.TestCase):
    selfhost = True
    setUp_relay = tw.RelayWatchTests.setUp
    tearDown_relay = tw.RelayWatchTests.tearDown
    online = tw.RelayWatchTests.online
    request = tw.RelayWatchTests.request

    def setUp(self):
        self.setUp_relay()
        self.data = tempfile.TemporaryDirectory()
        self.relay.selfhost_data = Path(self.data.name)
        self.run = Path(self.data.name) / "run"
        self.run.mkdir()
        self.cookies["admin"] = "tvs=" + self.accounts.new_session(self.admin, "pc")

    def tearDown(self):
        self.tearDown_relay()
        self.data.cleanup()

    def status(self, **state):
        (self.run / "update.json").write_text(json.dumps(dict({"state": "latest"}, **state)), encoding="utf-8")

    def call(self, method, path, who="admin", body=None):
        status, _, raw = self.request(method, "/tv/api/update" + path, who, body)
        return status, json.loads(raw) if raw.startswith(b"{") else raw

    def test_admin_only_and_off(self):
        self.assertEqual(self.call("GET", "", "alice")[0], 404)
        self.assertEqual(self.call("POST", "/check", "alice", {})[0], 404)
        self.assertEqual(self.call("GET", "")[1], {"ok": True, "on": False}, "no status file: a checkout")

    def test_status_check_and_install(self):
        self.status(state="ready", version="0.4.0-beta", notes="新", current="0.3.8-beta", secret="x")
        answer = self.call("GET", "")[1]
        self.assertEqual((answer["on"], answer["available"], answer["version"]), (True, True, "0.4.0-beta"))
        self.assertNotIn("secret", answer, "only the known fields")
        self.assertTrue(self.call("POST", "/check", body={})[1]["ok"])
        self.assertTrue((self.run / updates.CHECK_REQUEST).exists())
        self.assertTrue(self.call("POST", "/install", body={})[1]["ok"])
        self.assertTrue((self.run / updates.INSTALL_REQUEST).exists())

    def test_install_asks_when_someone_watches(self):
        self.status(state="ready", version="0.4.0-beta")
        self.relay.watch.streams = lambda: ["one"]
        answer = self.call("POST", "/install", body={})[1]
        self.assertEqual((answer["ok"], answer["busy"]), (False, 1))
        self.assertFalse((self.run / updates.INSTALL_REQUEST).exists())
        self.assertTrue(self.call("POST", "/install", body={"force": True})[1]["ok"])

    def test_nothing_to_install(self):
        self.status(state="downloading", version="0.4.0-beta")
        self.assertFalse(self.call("POST", "/install", body={})[1]["ok"])


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class ConsoleUpdateTests(unittest.TestCase):
    def test_console_update_line(self):
        result = subprocess.run(["node", str(ROOT / "tests" / "console_update_test.mjs")],
                                capture_output=True, text=True, encoding="utf-8", timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
