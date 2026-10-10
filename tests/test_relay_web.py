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
from server.relay import events, web
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

    def video_id(self, text):
        if self.down:
            raise BilibiliError("offline")
        match = re.search(r"/video/(BV\w{10})", text)
        return match.group(1) if match else None

    def video(self, bvid):
        return Video(bvid=bvid, title=f"t{bvid}", duration=600, pubdate=0)

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


class FakeYouTube:
    """The cstone node's client: never touched by Bilibili links."""

    def __init__(self):
        self.opened = []

    def creator_id(self, text):
        return "https://www.youtube.com/@ltt" if "youtube.com/@ltt" in text else None

    def creator(self, channel):
        return "LTT", [Video(bvid="ytvideo0001", title="y1", duration=600, pubdate=0, site="youtube"),
                       Video(bvid="ytvideo0002", title="y2", duration=600, pubdate=0, site="youtube")]

    def uploads(self, channel):
        return self.creator(channel)[1]

    def video_id(self, text):
        return "ytcast00001" if "youtu.be/ytcast00001" in text else None

    def video(self, video_id):
        return Video(bvid=video_id, title="yt cast", duration=60, pubdate=0, site="youtube")

    def open(self, video_id, offset):
        self.opened.append((video_id, offset))
        return FakeProc()

    state = "ok"

    def health(self):
        if self.state is None:
            raise BilibiliError("node down")
        return {"state": self.state, "at": 1.0, "version": "x"}


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
        self.events = root / "relay-events.json"
        self.youtube = FakeYouTube()
        self.main = root / "channels.txt"
        self.main.write_text("ch000 | CCTV1 | https://x/1.m3u8\nch001 | 湖南卫视 | https://x/2.m3u8\n"
                             "ch002 | 老高与小茉 | https://x/3.m3u8\n", encoding="utf-8")
        self.groups_file = root / "channel-groups.json"
        self.playing = root / "now-playing.json"
        self.remote_url = root / "remote-url.txt"
        self.relay = web.Relay(self.state, self.client, self.table, "/opt/ff", KEY,
                               events_out=self.events, youtube=self.youtube,
                               main_channels=self.main, groups_out=self.groups_file,
                               now_playing_in=self.playing, remote_url_out=self.remote_url,
                               public_url="https://iewww.com",
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

    def raw(self, method, path, headers=None, body=None):
        import http.client
        connection = http.client.HTTPConnection("127.0.0.1", self.manage.server_address[1], timeout=5)
        connection.request(method, path, body=body, headers=headers or {})
        answer = connection.getresponse()
        return answer, answer.read()

    def login_cookie(self, path):
        answer, _ = self.raw("GET", path)
        self.assertEqual(answer.status, 302)
        self.assertRegex(answer.getheader("Location"), r"^/tv/\?s=[A-Za-z0-9_-]{40,}$")
        self.last_location = answer.getheader("Location")
        cookie = answer.getheader("Set-Cookie")
        for part in ("HttpOnly", "Secure", "SameSite=Lax", "Path=/tv/", "Max-Age=15552000"):
            self.assertIn(part, cookie)
        return cookie.split(";")[0]

    def test_key_link_logs_in_and_cookie_opens_the_page(self):
        cookie = self.login_cookie(f"/?k={KEY}")
        answer, body = self.raw("GET", "/tv/", {"Cookie": cookie})
        self.assertEqual(answer.status, 200)
        self.assertIn("小智电视".encode(), body)
        answer, body = self.raw("GET", "/api/state", {"Cookie": cookie})
        self.assertEqual(answer.status, 200)
        self.assertEqual(self.raw("GET", "/tv/", {"Cookie": "tvs=forged"})[0].status, 404)

    def test_home_screen_url_carries_the_session(self):
        """An iPhone home-screen app starts without Safari's cookies."""
        self.login_cookie(f"/?k={KEY}")
        answer, body = self.raw("GET", self.last_location)
        self.assertEqual(answer.status, 200)
        cookie = answer.getheader("Set-Cookie").split(";")[0]
        self.assertEqual(self.raw("GET", "/api/state", {"Cookie": cookie})[0].status, 200)
        self.assertEqual(self.raw("GET", "/tv/?s=forged")[0].status, 404)

    def test_code_link_and_new_code(self):
        self.relay.write_remote_url()
        code = self.state.ensure_code()
        self.assertEqual(self.remote_url.read_text().strip(), f"HTTPS://IEWWW.COM/TV/R/{code}")
        cookie = self.login_cookie(f"/r/{code}")
        self.assertEqual(self.raw("GET", "/r/AAAAAAAAAAAAAAAAAAAA")[0].status, 404)
        answer, body = self.raw("POST", "/api/code", {"Cookie": cookie, "Content-Type": "application/json"}, b"{}")
        self.assertEqual(json.loads(body), {"ok": True})
        self.assertNotIn(code, self.remote_url.read_text())
        self.assertEqual(self.raw("GET", "/api/state", {"Cookie": cookie})[0].status, 404)
        self.assertEqual(self.raw("GET", f"/r/{code}")[0].status, 404)

    def test_assets_need_no_login(self):
        answer, body = self.raw("GET", "/manifest.webmanifest")
        self.assertEqual(answer.status, 200)
        self.assertEqual(json.loads(body)["scope"], "/tv/")
        self.assertNotIn("start_url", json.loads(body))
        self.assertEqual(self.raw("GET", "/icon-180.png")[1][:4], b"\x89PNG")

    # ---- remote ----

    def test_state_groups_and_now_playing(self):
        from server import now_playing
        self.relay.add_creator("https://space.bilibili.com/42")
        now_playing.write(self.playing, "ch001", True, True, now=self.now, since=self.now)
        state = self.call("GET", "/api/state")
        names = [g["name"] for g in state["groups"]]
        self.assertEqual(names, ["我的投屏", "UP 主", "央视", "卫视", "其他"])
        self.assertEqual(state["now"], {"channel": "ch001", "online": True, "switch": True,
                                        "group": "卫视", "name": "湖南卫视", "n": 1, "title": "",
                                        "since": self.now})
        creators = next(g for g in state["groups"] if g["name"] == "UP 主")["channels"]
        self.assertEqual(creators, [{"id": "up01", "name": "UP42", "n": 1}])

    def test_switch(self):
        from server import now_playing
        self.assertEqual(self.call("POST", "/api/switch", {"channel": "ch000"})["error"], "小智没在线")
        now_playing.write(self.playing, "ch001", True, False, now=self.now)
        self.assertIn("手动换台", self.call("POST", "/api/switch", {"channel": "ch000"})["error"])
        now_playing.write(self.playing, "ch001", True, True, now=self.now)
        self.assertEqual(self.call("POST", "/api/switch", {"channel": "nope"})["error"], "没有这个台")
        self.assertEqual(self.call("POST", "/api/switch", {"channel": "ch000"}), {"ok": True, "at": self.now})
        self.assertEqual(events.read_event(self.events), (1, "switch", "ch000"))

    def test_move_a_channel_to_another_group(self):
        self.assertEqual(self.call("POST", "/api/groups", {"channel": "ch002", "group": "音乐"}), {"ok": True})
        groups = {g["name"]: [c["id"] for c in g["channels"]] for g in self.call("GET", "/api/state")["groups"]}
        self.assertEqual(groups["音乐"], ["ch002"])
        self.assertNotIn("其他", groups)
        self.call("POST", "/api/groups", {"channel": "ch002", "group": None})
        self.assertEqual(json.loads(self.groups_file.read_text()), {})
        self.assertFalse(self.call("POST", "/api/groups", {"channel": "cast", "group": "音乐"})["ok"])
        self.assertFalse(self.call("POST", "/api/groups", {"channel": "ch002", "group": "UP 主"})["ok"])

    def test_history_replay_and_delete(self):
        self.call("POST", "/api/cast", {"url": "https://www.bilibili.com/video/BV1aZpw6DEKJ"})
        self.now += 10
        self.call("POST", "/api/cast", {"url": "https://youtu.be/ytcast00001"})
        history = self.call("GET", "/api/state")["history"]
        self.assertEqual([(h["site"], h["id"]) for h in history],
                         [("youtube", "ytcast00001"), ("bilibili", "BV1aZpw6DEKJ")])
        answer = self.call("POST", "/api/history/play", {"site": "bilibili", "id": "BV1aZpw6DEKJ"})
        self.assertEqual(answer, {"ok": True, "title": "tBV1aZpw6DEKJ", "at": self.now})
        self.assertEqual(self.state.cast.bvid, "BV1aZpw6DEKJ")
        self.assertEqual(self.state.history[0]["id"], "BV1aZpw6DEKJ")
        self.assertEqual(self.call("DELETE", "/api/history/youtube/ytcast00001"), {"ok": True})
        self.assertEqual(len(self.state.history), 1)
        self.assertFalse(self.call("POST", "/api/history/play", {"site": "youtube", "id": "ytcast00001"})["ok"])

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

    # ---- cast ----

    def test_cast_switches_plays_and_returns(self):
        answer = self.call("POST", "/api/cast", {"url": "https://www.bilibili.com/video/BV1aZpw6DEKJ"})
        self.assertEqual(answer, {"ok": True, "title": "tBV1aZpw6DEKJ", "at": self.now})
        self.assertEqual(events.read_event(self.events), (1, "cast", ""))
        self.assertEqual(self.call("GET", "/api/state")["cast"],
                         {"title": "tBV1aZpw6DEKJ", "done": False})
        self.outcome = "client_gone"
        self.stream("cast")
        self.assertEqual(self.started[-1], ("https://media.example/BV1aZpw6DEKJ.mp4", 0.0))
        self.assertAlmostEqual(self.state.cast_position, 30.0, delta=1.0)
        self.outcome = "ended"
        self.stream("cast")
        self.assertEqual(self.started[-1][1], 20.0)
        self.assertTrue(self.state.cast_done)
        self.assertEqual(events.read_event(self.events), (2, "return", ""))
        # Chosen by hand afterwards, it plays again from the start.
        self.stream("cast")
        self.assertEqual(self.started[-1][1], 0.0)

    def test_cast_errors(self):
        self.assertFalse(self.call("POST", "/api/cast", {"url": "https://space.bilibili.com/42"})["ok"])
        self.assertEqual(self.stream("cast")[0], 404)
        self.assertIsNone(events.read_event(self.events))
        self.client.down = True
        offline = self.call("POST", "/api/cast", {"url": "https://www.bilibili.com/video/BV1aZpw6DEKJ"})
        self.assertIn("稍后", offline["error"])

    # ---- YouTube ----

    def test_youtube_creator_streams_through_the_node(self):
        answer = self.call("POST", "/api/creators", {"url": "https://www.youtube.com/@ltt/videos"})
        self.assertEqual(answer, {"ok": True, "creator": {"id": "up01", "name": "LTT"}})
        self.assertEqual(self.state.get("up01").site, "youtube")
        self.assertEqual(self.call("GET", "/api/state")["creators"][0]["site"], "youtube")
        self.stream("up01")
        self.assertEqual(self.youtube.opened, [("ytvideo0001", 0.0)])
        self.assertEqual(self.started, [])
        self.assertEqual(self.state.get("up01").watched, ["ytvideo0001"])

    def test_youtube_cast(self):
        answer = self.call("POST", "/api/cast", {"url": "https://youtu.be/ytcast00001"})
        self.assertEqual(answer, {"ok": True, "title": "yt cast", "at": self.now})
        self.stream("cast")
        self.assertEqual(self.youtube.opened, [("ytcast00001", 0.0)])
        self.assertEqual(self.state.cast.site, "youtube")

    def test_youtube_health_is_cached_and_reported(self):
        self.youtube.state = "login"
        self.assertEqual(self.call("GET", "/api/state")["youtube"], {"state": "login"})
        self.youtube.state = "ok"
        self.assertEqual(self.call("GET", "/api/state")["youtube"], {"state": "login"},
                         "asked at most once a minute")
        self.now += 61
        self.assertEqual(self.call("GET", "/api/state")["youtube"], {"state": "ok"})
        self.youtube.state = None
        self.now += 61
        self.assertEqual(self.call("GET", "/api/state")["youtube"], {"state": "unreachable"})
        self.relay.clients["youtube"] = None
        self.assertNotIn("youtube", self.call("GET", "/api/state"))

    def test_youtube_not_configured(self):
        self.relay.clients["youtube"] = None
        answer = self.call("POST", "/api/cast", {"url": "https://youtu.be/ytcast00001"})
        self.assertFalse(answer["ok"])
        self.assertIn("YouTube", answer["error"])

    def test_unknown_channel(self):
        self.assertEqual(self.stream("up09")[0], 404)
        self.assertEqual(self.status(f"http://127.0.0.1:{self.streams.server_address[1]}/x")[0], 404)


class PageFieldTests(unittest.TestCase):
    """The page's script and the API agree on field names (see test_channel_config.py)."""

    def test_page_fields_are_served(self):
        script = web.PAGE.split("<script>", 1)[1]
        used = set(re.findall(r"\b(?:data|now|data\.now|data\.youtube|g|c|h|cr|answer|answer\.creator)\.(\w+)", script))
        used -= {"length", "find", "map", "includes"}
        served = {"now", "groups", "creators", "cast", "history", "group_names", "youtube", "state",
                  "channel", "online", "switch", "group", "name", "n", "title", "channels", "id",
                  "site", "at", "since", "left", "total", "ok", "error", "creator"}
        self.assertTrue(used, "no field reads found; the pattern no longer matches the page")
        self.assertLessEqual(used, served)


if __name__ == "__main__":
    unittest.main()
