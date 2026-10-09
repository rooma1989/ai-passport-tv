"""The relay's two HTTP services, driven over real sockets with fakes behind them.

Bilibili and ffmpeg are replaced; what is checked is the part that runs on the
server: the key, the API answers the page reads, the channel table, and what a
stream request does to the saved state.
"""

import json
import re
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server.relay import web
from server.relay.bilibili import BilibiliError
from server.relay.state import State, Video

KEY = "test-key-0123456789"


def videos(*ids):
    return [Video(bvid=v, title=f"t{v}", duration=600, pubdate=1000 - n)
            for n, v in enumerate(ids)]


class FakeClient:
    def __init__(self):
        self.gone = set()
        self.down = False

    def creator_id(self, text):
        if self.down:
            raise BilibiliError("offline")
        match = re.search(r"space\.bilibili\.com/(\d+)", text)
        return match.group(1) if match else None

    def creator(self, mid):
        return f"UP{mid}", videos("a", "b", "c")

    def uploads(self, mid):
        return videos("new", "a", "b")

    def media_url(self, bvid):
        if self.down:
            raise BilibiliError("offline")
        if bvid in self.gone:
            raise BilibiliError("gone", permanent=True)
        return f"https://media.example/{bvid}.mp4"


class FakeProc:
    def kill(self):
        pass

    def wait(self, timeout=None):
        return 0


class WebTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.table = root / "relay-channels.txt"
        self.state = State(root / "relay-state.json")
        self.client = FakeClient()
        self.started = []
        self.outcome = "ended"
        self.now = 1000.0
        self.relay = web.Relay(self.state, self.client, self.table, "/opt/ff", KEY,
                               log=lambda _m: None, clock=lambda: self.now,
                               starter=self.fake_start, pumper=self.fake_pump)
        self.streams, self.manage = web.make_servers(self.relay, 0, 0)
        for server in (self.streams, self.manage):
            threading.Thread(target=server.serve_forever, daemon=True).start()

    def tearDown(self):
        for server in (self.streams, self.manage):
            server.shutdown()
            server.server_close()
        self.tmp.cleanup()

    def fake_start(self, ffmpeg, url, offset):
        self.started.append((url, offset))
        return FakeProc()

    def fake_pump(self, proc, write, on_tick):
        write(b"\x47" * 188)
        # A failure counted as "quick" is one that ends before any progress tick.
        if self.outcome != "failed":
            on_tick(30.0)
        return self.outcome

    def manage_url(self, path):
        return f"http://127.0.0.1:{self.manage.server_address[1]}{path}"

    def call(self, method, path, body=None, key=KEY):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self.manage_url(path), data=data, method=method)
        if key:
            request.add_header("X-Key", key)
        if data:
            request.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(request, timeout=5) as answer:
            return json.loads(answer.read())

    def status(self, url):
        try:
            with urllib.request.urlopen(url, timeout=5) as answer:
                return answer.status, answer.read()
        except urllib.error.HTTPError as error:
            return error.code, b""

    def stream(self, cid):
        return self.status(f"http://127.0.0.1:{self.streams.server_address[1]}/ch/{cid}")

    # ---- key ----

    def test_wrong_or_missing_key_is_404(self):
        for key in (None, "wrong"):
            with self.assertRaises(urllib.error.HTTPError) as caught:
                self.call("GET", "/api/state", key=key)
            self.assertEqual(caught.exception.code, 404)
        self.assertEqual(self.status(self.manage_url("/"))[0], 404)

    def test_page_with_key_in_query(self):
        code, body = self.status(self.manage_url(f"/?k={KEY}"))
        self.assertEqual(code, 200)
        self.assertIn("小智电视 UP 主".encode(), body)

    # ---- creators ----

    def test_add_and_remove_update_the_table(self):
        answer = self.call("POST", "/api/creators", {"url": "https://space.bilibili.com/42"})
        self.assertEqual(answer, {"ok": True, "creator": {"id": "up01", "name": "UP42"}})
        self.assertIn("up01 | UP42 | http://127.0.0.1:8099/ch/up01", self.table.read_text())
        state = self.call("GET", "/api/state")
        self.assertEqual(state["creators"][0]["left"], 3)
        self.assertEqual(self.call("DELETE", "/api/creators/up01"), {"ok": True})
        self.assertNotIn("up01", self.table.read_text())
        self.assertFalse(self.call("DELETE", "/api/creators/up01")["ok"])

    def test_add_errors(self):
        self.assertFalse(self.call("POST", "/api/creators", {"url": "https://example.com/"})["ok"])
        self.assertFalse(self.call("POST", "/api/creators", {"url": ""})["ok"])
        self.call("POST", "/api/creators", {"url": "https://space.bilibili.com/42"})
        again = self.call("POST", "/api/creators", {"url": "https://space.bilibili.com/42"})
        self.assertEqual(again["ok"], False)
        self.client.down = True
        offline = self.call("POST", "/api/creators", {"url": "https://space.bilibili.com/43"})
        self.assertIn("稍后", offline["error"])

    # ---- streams ----

    def add(self):
        self.relay.add_creator("https://space.bilibili.com/42")

    def test_stream_ended_marks_watched(self):
        self.add()
        code, body = self.stream("up01")
        self.assertEqual((code, body[:1]), (200, b"\x47"))
        self.assertEqual(self.started, [("https://media.example/a.mp4", 0.0)])
        self.assertEqual(self.state.get("up01").watched, ["a"])
        self.stream("up01")
        self.assertEqual(self.started[-1][0], "https://media.example/b.mp4")

    def test_stream_cut_short_saves_position(self):
        self.add()
        self.outcome = "client_gone"
        self.stream("up01")
        self.assertEqual(self.state.get("up01").watched, [])
        self.assertAlmostEqual(self.state.get("up01").positions["a"], 30.0, delta=1.0)
        reloaded = State(self.state.path)
        self.assertIn("a", reloaded.get("up01").positions)

    def test_gone_video_skipped_transient_not(self):
        self.add()
        self.client.gone = {"a"}
        self.stream("up01")
        self.assertEqual(self.started[-1][0], "https://media.example/b.mp4")
        self.client.down = True
        self.assertEqual(self.stream("up01")[0], 503)
        self.assertNotIn("c", self.state.get("up01").watched)

    def test_quick_failures_skip_after_three(self):
        self.add()
        self.outcome = "failed"
        for _ in range(web.MAX_QUICK_FAILURES):
            self.stream("up01")
        self.assertIn("a", self.state.get("up01").watched)

    def test_stale_list_refreshed(self):
        self.add()
        self.now += 31 * 60
        self.stream("up01")
        self.assertEqual(self.started[-1][0], "https://media.example/new.mp4")

    def test_unknown_channel(self):
        self.assertEqual(self.stream("up09")[0], 404)
        self.assertEqual(self.status(f"http://127.0.0.1:{self.streams.server_address[1]}/x")[0], 404)


class PageFieldTests(unittest.TestCase):
    """The page's script and the API agree on field names (see test_channel_config.py)."""

    def test_page_fields_are_served(self):
        script = web.PAGE.split("<script>", 1)[1]
        used = set(re.findall(r"\b(?:c|data|answer|answer\.creator)\.(\w+)", script))
        served = {"creators", "id", "name", "site", "left", "total", "ok", "error", "creator"}
        self.assertTrue(used, "no field reads found; the pattern no longer matches the page")
        self.assertLessEqual(used, served)


if __name__ == "__main__":
    unittest.main()
