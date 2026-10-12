"""The relay's two HTTP services, driven over real sockets with fakes behind them.

Bilibili and ffmpeg are replaced; what is checked is the part that runs on the
server: the key, the API answers the page reads, the channel table, and what a
stream request does to the saved state.
"""

import io
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

    def series_id(self, text):
        if self.down:
            raise BilibiliError("offline")
        match = re.search(r"/bangumi/play/(ss\d+)", text)
        return match.group(1) if match else None

    def series(self, source):
        if source == "ss9":
            raise BilibiliError("ss9: 没有免费的集（都要会员）", permanent=True)
        return f"番剧{source}", [Video(bvid=f"ep{n}/{n}", title=f"第{n}集", duration=600, pubdate=0)
                                 for n in (1, 2, 3)]

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
        self.assertEqual(self.raw("GET", "/pets/cici_mood1.png")[1][:4], b"\x89PNG")

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
                                        "since": self.now, "video": None, "device": None})
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
        self.assertEqual(events.read_event(self.events), (1, "switch", "ch000", None, ""))

    def test_device_volume_and_brightness(self):
        from server import now_playing
        self.assertEqual(self.call("POST", "/api/device", {"volume": 50})["error"], "小智没在线")
        now_playing.write(self.playing, "ch001", True, True, now=self.now, since=self.now)
        self.assertIn("固件", self.call("POST", "/api/device", {"volume": 50})["error"])
        # The firmware's step 0 is its brightest; the phone counts upwards.
        now_playing.write(self.playing, "ch001", True, True, now=self.now, since=self.now,
                          device={"volume": 70, "brightness": 1, "steps": 5})
        self.assertEqual(self.call("GET", "/api/state")["now"]["device"],
                         {"volume": 70, "brightness": 3, "steps": 5})
        self.assertTrue(self.call("POST", "/api/device", {"volume": 47})["ok"])
        self.assertEqual(events.read_event(self.events), (1, "volume", "", 50, ""), "rounded to a step of 10")
        self.assertTrue(self.call("POST", "/api/device", {"brightness": 4})["ok"])
        self.assertEqual(events.read_event(self.events), (2, "brightness", "", 0, ""), "the brightest step")
        self.assertFalse(self.call("POST", "/api/device", {"brightness": 5})["ok"])
        self.assertFalse(self.call("POST", "/api/device", {"volume": 120})["ok"])
        self.assertFalse(self.call("POST", "/api/device", {})["ok"])

    def test_pet_from_the_phone(self):
        from server import now_playing
        self.assertEqual(self.call("POST", "/api/pet", {"pet": 3})["error"], "小智没在线")
        now_playing.write(self.playing, "ch001", True, True, now=self.now, since=self.now,
                          device={"volume": 70, "brightness": 1, "steps": 5})
        self.assertIn("固件", self.call("POST", "/api/pet", {"pet": 3})["error"])
        now_playing.write(self.playing, "ch001", True, True, now=self.now, since=self.now,
                          device={"volume": 70, "brightness": 1, "steps": 5, "pet": 2})
        self.assertEqual(self.call("GET", "/api/state")["now"]["device"]["pet"], 2, "the pet in use")
        self.assertTrue(self.call("POST", "/api/pet", {"pet": 10})["ok"])
        self.assertEqual(events.read_event(self.events), (1, "pet", "", 10, ""))
        for bad in (11, -1, "3", True, None):
            self.assertFalse(self.call("POST", "/api/pet", {"pet": bad})["ok"], bad)

    def test_audio_only_from_the_phone(self):
        from server import now_playing
        self.assertEqual(self.call("POST", "/api/audio", {"on": True})["error"], "小智没在线")
        now_playing.write(self.playing, "ch001", True, True, now=self.now, since=self.now,
                          device={"volume": 70, "brightness": 1, "steps": 5})
        self.assertIn("固件", self.call("POST", "/api/audio", {"on": True})["error"])
        now_playing.write(self.playing, "ch001", True, True, now=self.now, since=self.now,
                          device={"volume": 70, "brightness": 1, "steps": 5, "audio_only": 0})
        self.assertEqual(self.call("GET", "/api/state")["now"]["device"]["audio_only"], 0)
        self.assertTrue(self.call("POST", "/api/audio", {"on": True})["ok"])
        self.assertEqual(events.read_event(self.events), (1, "audio", "", 1, ""))
        self.assertFalse(self.call("POST", "/api/audio", {"on": "yes"})["ok"])

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

    # ---- series ----

    def test_series_added_played_in_order_and_removed(self):
        answer = self.call("POST", "/api/series", {"url": "https://www.bilibili.com/bangumi/play/ss7",
                                                   "name": " 海尔  兄弟 "})
        self.assertEqual(answer, {"ok": True, "series": {"id": "sr01", "name": "海尔 兄弟", "total": 3}})
        self.assertIn("sr01 | 海尔 兄弟 | http://127.0.0.1:8099/ch/sr01", self.table.read_text())
        state = self.call("GET", "/api/state")
        self.assertEqual(state["series"], [{"id": "sr01", "name": "海尔 兄弟", "episode": 1, "total": 3}])
        self.assertEqual(state["creators"], [])
        self.assertTrue(state["room"]["fits"])
        group = next(g for g in state["groups"] if g["name"] == "动画影视")
        self.assertEqual(group["channels"][0]["id"], "sr01")
        self.stream("sr01")
        self.stream("sr01")
        self.assertEqual([url for url, _ in self.started],
                         ["https://media.example/ep1/1.mp4", "https://media.example/ep2/2.mp4"])
        self.assertEqual(self.call("GET", "/api/state")["series"][0]["episode"], 3)
        self.assertEqual(self.call("DELETE", "/api/series/sr01"), {"ok": True})
        self.assertNotIn("sr01", self.table.read_text())

    def test_series_default_name_and_errors(self):
        answer = self.call("POST", "/api/series", {"url": "https://www.bilibili.com/bangumi/play/ss7"})
        self.assertEqual(answer["series"]["name"], "番剧ss7")
        again = self.call("POST", "/api/series", {"url": "https://www.bilibili.com/bangumi/play/ss7"})
        self.assertEqual(again, {"ok": False, "error": "这部剧已经添加过了"})
        paid = self.call("POST", "/api/series", {"url": "https://www.bilibili.com/bangumi/play/ss9"})
        self.assertEqual(paid["error"], "没有免费的集（都要会员）")
        self.assertFalse(self.call("POST", "/api/series", {"url": "https://example.com/"})["ok"])
        self.assertFalse(self.call("POST", "/api/series", {"url": "https://youtu.be/ytcast00001"})["ok"])
        self.client.down = True
        offline = self.call("POST", "/api/series", {"url": "https://www.bilibili.com/bangumi/play/ss8"})
        self.assertIn("稍后", offline["error"])

    def test_series_refused_when_the_device_list_is_full(self):
        # A live table already past the device's CONFIG packet: no room for another channel.
        rows = [f"ch{n:03d} | 很长很长很长的频道名字{n} | https://x/{n}.m3u8" for n in range(90)]
        self.main.write_text("\n".join(rows) + "\n", encoding="utf-8")
        self.assertFalse(self.relay.room()["fits"])
        answer = self.call("POST", "/api/series", {"url": "https://www.bilibili.com/bangumi/play/ss7"})
        self.assertIn("满了", answer["error"])
        self.assertIsNone(self.state.get("sr01"))

    def test_series_list_refreshed_twice_a_day(self):
        self.relay.add_series("https://www.bilibili.com/bangumi/play/ss7")
        self.state.get("sr01").videos = self.state.get("sr01").videos[:1]
        self.now += 31 * 60
        self.relay.refresh_if_stale("sr01")
        self.assertEqual(len(self.state.get("sr01").videos), 1)
        self.now += 12 * 3600
        self.relay.refresh_if_stale("sr01")
        self.assertEqual(len(self.state.get("sr01").videos), 3)

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
        self.assertEqual(events.read_event(self.events), (1, "cast", "", None, ""))
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
        self.assertEqual(events.read_event(self.events), (2, "return", "", None, ""))
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


class PlaybackControlTests(unittest.TestCase):
    """Seek and next cut the stream being served; the reopened one starts at the target."""

    def setUp(self):
        import threading as _threading
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.state = State(root / "relay-state.json")
        self.client = FakeClient()
        self.playing = root / "now-playing.json"
        self.events = root / "relay-events.json"
        self.started = []
        self.procs = []
        self.now = 1000.0
        self.mono = 50.0
        self.relay = web.Relay(self.state, self.client, root / "relay-channels.txt", "/opt/ff", KEY,
                               events_out=self.events, now_playing_in=self.playing,
                               log=lambda _m: None, clock=lambda: self.now,
                               monotonic=lambda: self.mono,
                               starter=self.fake_start, pumper=self.fake_pump)
        self.relay.add_creator("https://space.bilibili.com/42")
        from server import now_playing
        now_playing.write(self.playing, "up01", True, True, now=self.now, since=self.now)
        self._threading = _threading
        self.threads = []

    def tearDown(self):
        for proc in self.procs:
            proc.kill()
        # The play threads save state as they finish; let them, before the files go.
        for thread in self.threads:
            thread.join(5)
        self.tmp.cleanup()

    def fake_start(self, ffmpeg, url, offset):
        self.started.append((url, offset))
        proc = BlockingProc()
        self.procs.append(proc)
        return proc

    def fake_pump(self, proc, write, on_tick):
        proc.killed.wait(5)
        return "failed"

    def serve(self, cid="up01"):
        """Play the channel in a thread, as the TV server's decoder would."""
        class Handler:
            def __init__(self):
                self.wfile = io.BytesIO()
            def send_response(self, code): pass
            def send_header(self, *a): pass
            def end_headers(self): pass
            def send_error(self, code): self.error = code
        thread = self._threading.Thread(target=self.relay.play, args=(cid, Handler()), daemon=True)
        thread.start()
        self.threads.append(thread)
        for _ in range(100):
            if cid in self.relay._active:
                return thread
            import time as _time
            _time.sleep(0.01)
        self.fail("stream did not start")

    def test_progress_is_reported(self):
        self.serve()
        self.mono += 42
        video = self.relay.summary()["now"]["video"]
        lag = web.PLAYBACK_LAG_S
        self.assertEqual((video["position"], video["duration"], video["paused"]), (42.0 - lag, 600, False),
                         "what the device shows, behind what the relay has sent")
        self.assertTrue(video["moving"])
        self.mono = 50.0 + 1
        video = self.relay.summary()["now"]["video"]
        self.assertEqual((video["position"], video["moving"]), (0.0, False),
                         "never before the start, and not moving while the TV server buffers")
        self.assertEqual(video["started"], self.now)

    def test_seek_cuts_and_reopens_at_the_target(self):
        thread = self.serve()
        self.mono += 100 + web.PLAYBACK_LAG_S
        answer = self.relay.seek(delta=30)
        self.assertEqual((answer["ok"], answer["position"]), (True, 130.0))
        thread.join(2)
        self.assertTrue(self.procs[0].killed.is_set())
        self.assertEqual(self.state.get("up01").positions["a"], 130.0 + 10,
                         "the cut stream did not overwrite the target")
        self.serve()
        self.assertEqual(self.started[-1], ("https://media.example/a.mp4", 130.0))

    def test_quick_presses_add_up_before_the_new_stream(self):
        thread = self.serve()
        self.mono += 100 + web.PLAYBACK_LAG_S
        self.relay.seek(delta=30)
        thread.join(2)
        # The TV server has not reopened yet: the next press builds on the first.
        self.assertEqual(self.relay.summary()["now"]["video"]["position"], 130.0)
        self.assertEqual(self.relay.summary()["now"]["video"]["started"], 0.0)
        self.assertEqual(self.relay.seek(delta=30)["position"], 160.0)
        self.assertEqual(self.relay.seek(delta=-90)["position"], 70.0)
        self.serve()
        self.assertEqual(self.started[-1], ("https://media.example/a.mp4", 70.0))
        self.now += web.SEEK_BRIDGE_S + 1
        self.assertIsNotNone(self.relay.summary()["now"]["video"], "the new stream reports itself")

    def test_seek_and_next_tell_the_device(self):
        thread = self.serve()
        self.mono += 100 + web.PLAYBACK_LAG_S
        self.relay.seek(delta=-30)
        thread.join(2)
        last = events.read_event(self.events)
        self.assertEqual((last.action, last.value), ("seek", -30))
        self.serve()
        self.relay.next()
        self.assertEqual(events.read_event(self.events).action, "next")

    def test_seek_bounds(self):
        self.serve()
        self.mono += 10
        self.assertEqual(self.relay.seek(delta=-30)["position"], 0.0)
        self.serve()
        self.relay.seek(position=599)
        self.assertIn("a", self.state.get("up01").watched, "past the end is next")

    def test_next_marks_watched(self):
        thread = self.serve()
        self.assertTrue(self.relay.next()["ok"])
        thread.join(2)
        self.assertEqual(self.state.get("up01").watched, ["a"])
        self.serve()
        self.assertEqual(self.started[-1][0], "https://media.example/b.mp4")

    def test_next_on_a_cast_returns(self):
        from server import now_playing
        self.relay.cast("https://www.bilibili.com/video/BV1aZpw6DEKJ")
        now_playing.write(self.playing, "cast", True, True, now=self.now, since=self.now)
        self.serve("cast")
        self.assertTrue(self.relay.next()["ok"])
        self.assertEqual(events.read_event(self.events).action, "return")
        self.assertTrue(self.state.cast_done)

    def test_pause_and_resume_continue_exactly(self):
        thread = self.serve()
        self.mono += 50 + web.PLAYBACK_LAG_S
        answer = self.relay.pause()
        self.assertTrue(answer["ok"])
        thread.join(2)
        self.assertEqual(events.read_event(self.events).action, "pause")
        video = self.relay.summary()["now"]["video"]
        self.assertEqual((video["paused"], video["position"]), (True, 50.0))
        self.assertEqual(self.relay.open_channel("up01"), 503, "no stream while paused")
        self.assertTrue(self.relay.resume()["ok"])
        self.assertEqual(events.read_event(self.events).action, "resume")
        self.serve()
        self.assertEqual(self.started[-1], ("https://media.example/a.mp4", 50.0))

    def test_pause_expires_and_ends_on_another_channel(self):
        from server import now_playing
        self.serve()
        self.relay.pause()
        self.now += web.PAUSE_MAX_S + 1
        self.assertNotEqual(self.relay.open_channel("up01"), 503, "a long pause ends by itself")
        self.relay.pause()  # nothing playing now: refused
        self.serve()
        self.relay.pause()
        now_playing.write(self.playing, "ch000", True, True, now=self.now, since=self.now)
        self.assertNotEqual(self.relay.open_channel("up01"), 503, "switching away ends the pause")

    def test_nothing_to_control(self):
        from server import now_playing
        self.assertFalse(self.relay.seek(delta=30)["ok"])
        now_playing.write(self.playing, "ch000", True, True, now=self.now, since=self.now)
        self.assertFalse(self.relay.next()["ok"])


class BlockingProc:
    def __init__(self):
        import threading as _threading
        self.killed = _threading.Event()

    def kill(self):
        self.killed.set()

    def wait(self, timeout=None):
        return -9


class PageFieldTests(unittest.TestCase):
    """The page's script and the API agree on field names (see test_channel_config.py)."""

    def test_page_fields_are_served(self):
        script = web.PAGE.split("<script>", 1)[1]
        used = set(re.findall(r"\b(?:data|now|data\.now|data\.now\.video|data\.now\.device|data\.youtube|v|d|g|c|h|cr|sr|room|answer|answer\.creator|answer\.series)\.(\w+)", script))
        used -= {"length", "find", "map", "includes"}
        served = {"now", "groups", "creators", "cast", "history", "group_names", "youtube", "state",
                  "channel", "online", "switch", "group", "name", "n", "title", "channels", "id",
                  "site", "at", "since", "left", "total", "ok", "error", "creator",
                  "video", "position", "duration", "paused", "started", "moving",
                  "device", "volume", "brightness", "steps", "pet", "audio_only",
                  "series", "room", "fits", "episode"}
        self.assertTrue(used, "no field reads found; the pattern no longer matches the page")
        self.assertLessEqual(used, served)


if __name__ == "__main__":
    unittest.main()
