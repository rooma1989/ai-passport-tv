"""The lite relay (docs/superpowers/specs/2026-11-04-minimal-product-design.md section 3):
which feature modules it attaches, which paths it refuses (410, never 404 -- the console
treats 404 as signed out), and that it tells the console its edition.
"""

from __future__ import annotations

import http.client
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from server import edition
from server.relay import __main__ as relay_main
from server.relay import setup, web
from server.relay.bilibili import Bilibili
from server.tenancy.accounts import Accounts


class AttachTests(unittest.TestCase):
    NAMES = ("ytsettings.autostart", "ytupdate.attach", "epg.attach", "linecheck.attach", "subscriptions.attach",
             "library.attach", "clouddrive.attach", "livetally.attach")

    def attached(self, which: str) -> set[str]:
        called = set()
        patches = []
        for name in self.NAMES:
            module, func = name.split(".")
            target = f"server.relay.{module}.{func}"
            patches.append(mock.patch(target, side_effect=lambda *a, _n=name, **k: called.add(_n)))
        relay = SimpleNamespace(main_channels=None, log=lambda _m: None)
        args = SimpleNamespace(db=Path("/tmp/none/tv.db"))
        with mock.patch.dict(os.environ, {edition.ENV: which}):
            for p in patches:
                p.start()
            try:
                relay_main.attach_features(relay, args)
            finally:
                for p in patches:
                    p.stop()
        return called

    def test_full_attaches_everything(self):
        self.assertEqual(self.attached("full"), set(self.NAMES))

    def test_lite_skips_youtube_library_drives_and_subscriptions(self):
        self.assertEqual(self.attached("lite"), {"epg.attach", "linecheck.attach", "livetally.attach"})


KEY = "k" * 32


class WebTests(unittest.TestCase):
    which = "lite"

    def setUp(self):
        self.env = mock.patch.dict(os.environ, {edition.ENV: self.which})
        self.env.start()
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.accounts = Accounts(root / "tv.db")
        main = root / "channels.txt"
        main.write_text("# empty\n", encoding="utf-8")
        self.relay = web.Relay(None, Bilibili(), None, "/opt/ff", KEY, main_channels=main, accounts=self.accounts,
                               devices_dir=root / "devices", log=lambda _m: None,
                               selfhost=setup.SelfHost(setup_code_file=root / "code"))
        self.admin = self.accounts.create_user("管理员", "admin")
        self.token = self.accounts.new_session(self.admin, "pc")
        self.streams, self.manage = web.make_servers(self.relay, 0, 0)
        for server in (self.streams, self.manage):
            threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    def tearDown(self):
        for server in (self.streams, self.manage):
            server.shutdown()
            server.server_close()
        self.accounts.close()
        self.tmp.cleanup()
        self.env.stop()

    def request(self, method, path, body=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.manage.server_address[1], timeout=5)
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Cookie": "tvs=" + self.token}
        if data is not None:
            headers["Content-Type"] = "application/json"   # 不带 Origin/Sec-Fetch-Site：console.same_origin 照常放行
        connection.request(method, path, body=data, headers=headers)
        response = connection.getresponse()
        raw = response.read()
        connection.close()
        try:
            return response.status, json.loads(raw)
        except ValueError:
            return response.status, raw

    def test_blocked_paths_answer_410_with_json(self):
        for method, path in (("GET", "/tv/api/library"), ("GET", "/tv/api/youtube/status"),
                             ("POST", "/tv/api/watch/prepare"), ("GET", "/tv/api/admin/channels")):
            status, answer = self.request(method, path, {} if method == "POST" else None)
            self.assertEqual(status, 410, path)
            self.assertEqual(answer, {"ok": False, "error": edition.OFF_MESSAGE, "edition": "lite"}, path)

    def test_state_and_login_status_carry_the_edition(self):
        self.assertEqual(self.request("GET", "/tv/api/state")[1]["edition"], self.which)
        self.assertEqual(self.request("GET", "/tv/api/login/status")[1]["edition"], self.which)


class FullWebTests(WebTests):
    which = "full"

    def test_blocked_paths_answer_410_with_json(self):
        status, _answer = self.request("GET", "/tv/api/admin/channels")
        self.assertEqual(status, 200, "the full edition keeps the channel editor")


if __name__ == "__main__":
    unittest.main()
