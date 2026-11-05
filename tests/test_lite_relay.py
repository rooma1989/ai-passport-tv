"""The lite relay (docs/superpowers/specs/2026-11-04-minimal-product-design.md section 3):
which feature modules it attaches, which paths it refuses (410, never 404 -- the console
treats 404 as signed out), and that it tells the console its edition.
"""

from __future__ import annotations

import http.client
import json
import os
import shutil
import subprocess
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
from server.relay import follows, home, setup, web
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

    def test_lite_skips_youtube_epg_library_drives_and_subscriptions(self):
        self.assertEqual(self.attached("lite"), {"linecheck.attach", "livetally.attach"})


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

    def test_favourite_episodes_are_not_410(self):
        """精简版的选集抽屉：收藏夹台（fv）的集数表和点一集不是 410（没有这个台时是普通的「没有」）；剧集照旧 410。"""
        status, answer = self.request("GET", "/tv/api/series/fv01/episodes")
        self.assertNotEqual(status, 410, answer)
        status, answer = self.request("POST", "/tv/api/series/fv01/play", {"episode": 1})
        self.assertNotEqual(status, 410, answer)
        status, _ = self.request("GET", "/tv/api/series/sr01/episodes")
        self.assertEqual(status, 410 if self.which == "lite" else 200)

    def test_pack_admin_endpoints(self):
        status, answer = self.request("GET", "/tv/api/admin/pack")
        self.assertEqual(status, 200)
        self.assertEqual(answer, {"ok": True, "configured": False})
        status, answer = self.request("POST", "/tv/api/admin/pack/prefs", {"hidden": [], "order": []})
        self.assertEqual((status, answer), (200, {"ok": False, "error": "还没有频道包"}))

    def test_state_and_login_status_carry_the_edition(self):
        self.assertEqual(self.request("GET", "/tv/api/state")[1]["edition"], self.which)
        self.assertEqual(self.request("GET", "/tv/api/login/status")[1]["edition"], self.which)

    def test_no_pc_target_in_lite(self):
        """精简版没有在电脑上看：「播放到」不列这台电脑、不拿它当默认，/api/state 没有 watch，点名它也不放。"""
        status, answer = self.request("GET", "/tv/api/targets")
        self.assertEqual(status, 200)
        self.assertEqual(answer["targets"], [])
        self.assertEqual(answer["auto"], "")
        self.assertNotIn("watch", self.request("GET", "/tv/api/state")[1])
        status, answer = self.request("POST", "/tv/api/play", {"target": "pc", "item": {"kind": "live", "channel": "cctv1"}})
        self.assertEqual(status, 200)
        self.assertEqual((answer["ok"], answer["reason"], answer["error"]), (False, "off", edition.OFF_MESSAGE))
        # 没有小电视时点「播放」（不点名）：照旧说还没有绑定，页面带去绑定。
        answer = self.request("POST", "/tv/api/play", {"item": {"kind": "live", "channel": "cctv1"}})[1]
        self.assertEqual(answer["reason"], "no_device")

    def test_follows_message_without_youtube(self):
        status, answer = self.request("GET", "/tv/api/follows")
        self.assertEqual(status, 200)
        self.assertEqual(answer["message"], follows.NO_ACCOUNT_LITE)

    def test_youtube_is_not_offered(self):
        """精简版不提「设置 → YouTube」：/api/state 没有 youtube_off，粘贴 YouTube 链接直说不支持。"""
        self.assertNotIn("youtube_off", self.request("GET", "/tv/api/state")[1])
        for path in ("/tv/api/cast", "/tv/api/creators"):
            answer = self.request("POST", path, {"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"})[1]
            self.assertEqual((answer["ok"], answer["error"]), (False, web.YOUTUBE_OFF_LITE), path)
            self.assertNotIn("设置", answer["error"])


class FullWebTests(WebTests):
    which = "full"

    def test_blocked_paths_answer_410_with_json(self):
        status, _answer = self.request("GET", "/tv/api/admin/channels")
        self.assertEqual(status, 200, "the full edition keeps the channel editor")

    def test_pack_admin_endpoints(self):
        """完整版没有频道包的接口：和任何不认识的管理路径一样 404。"""
        self.assertEqual(self.request("GET", "/tv/api/admin/pack")[0], 404)
        self.assertEqual(self.request("POST", "/tv/api/admin/pack/prefs", {"hidden": [], "order": []})[0], 404)

    def test_no_pc_target_in_lite(self):
        """完整版照旧：自建时「这台电脑」在最后，没有小电视时它就是默认。"""
        answer = self.request("GET", "/tv/api/targets")[1]
        self.assertEqual([t["id"] for t in answer["targets"]], ["pc"])
        self.assertEqual(answer["targets"][0]["name"], home.PC_NAME)
        self.assertEqual(answer["auto"], "pc")
        self.assertTrue(self.request("GET", "/tv/api/state")[1]["watch"])

    def test_follows_message_without_youtube(self):
        self.assertEqual(self.request("GET", "/tv/api/follows")[1]["message"], follows.NO_ACCOUNT)

    def test_youtube_is_not_offered(self):
        """完整版照旧：自建没开 YouTube 时 /api/state 有 youtube_off，粘贴链接指向「设置 → YouTube」。"""
        self.assertIs(self.request("GET", "/tv/api/state")[1]["youtube_off"], True)
        for path in ("/tv/api/cast", "/tv/api/creators"):
            answer = self.request("POST", path, {"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"})[1]
            self.assertEqual((answer["ok"], answer["error"]), (False, web.YOUTUBE_OFF_SELFHOST), path)


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class ConsoleLiteTests(unittest.TestCase):
    def test_console_lite_navigation(self):
        result = subprocess.run(["node", str(ROOT / "tests" / "console_lite_test.mjs")], capture_output=True,
                                text=True, encoding="utf-8", timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
