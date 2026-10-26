"""The console's YouTube search (GET /api/yt/search, server/relay/ytsearch.py).

Everything runs against canned answers of the YouTube node's /yt/search (the shape
ytnode.search_summary / channel_summary / playlist_search_summary produce), passed
through the real youtube.YouTube client so the node's HTTP errors are classified the
way production sees them. No network, no yt-dlp.
"""

import json
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from server import now_playing
from server.relay import events, web, youtube, ytsearch
from server.relay.bilibili import SourceError
from server.relay.youtube import NodeError, YouTube
from server.tenancy import files as device_files
from server.tenancy.accounts import Accounts

from test_relay_subs import PNG, _Server
from test_relay_web import KEY, FakeClient, FakeProc

UC = "UCSJ4gkVC6NrvII8umztf0Ow"
PL = "PLvWg2OZGt8Huee65hzUZ4Md3vMKZMnTYw"
VIDEOS = {"videos": [
    {"id": "n61ULEU7CO0", "title": "Lofi Girl - 1 A.M  Study Session", "duration": 3615, "uploader": "Lofi Girl",
     "views": 51000000, "channel_id": UC, "date": 1600000000},
    {"id": "4xDzrJKXOOY", "title": "synthwave radio", "duration": 1801, "uploader": "Lofi Girl"},   # an old node
    {"id": "n61ULEU7CO0", "title": "duplicate", "duration": 10},
    {"id": "bad", "title": "bad id", "duration": 60},
    {"id": "noduration1", "title": "no duration", "duration": 0},
    {"id": "notitle0001", "title": "  ", "duration": 60},
    {"id": "badchannel1", "title": "odd channel id", "duration": 60, "channel_id": "--exec", "views": True},
]}
CHANNELS = {"channels": [
    {"id": UC, "name": "Lofi Girl", "fans": 15200000, "videos": 0},
    {"id": UC, "name": "again"},
    {"id": "UCbad", "name": "bad"},
    {"id": "UCOxqgCwgOqC2lMqC5PYz_Dg", "name": "lofi geek", "fans": None, "videos": 120},
]}
PLAYLISTS = {"playlists": [
    {"id": PL, "title": "灌篮高手 全集", "channel": "动画", "count": 101,
     "thumb": "https://i.ytimg.com/vi/n61ULEU7CO0/hqdefault.jpg"},
    {"id": "PLnothumbAAAAAAAAA", "title": "no thumb", "thumb": "https://evil.example/vi/x.jpg"},
    {"id": "notaplaylist", "title": "x"},
]}


class FakeNode:
    """The node's JSON API, answering like ytnode.py (canned)."""

    def __init__(self):
        self.calls = []
        self.fail = None           # an exception to raise for /yt/search
        self.gate = None           # an Event /yt/search waits on (a slow yt-dlp)

    def __call__(self, path, params, timeout=None):
        self.calls.append((path, dict(params), timeout))
        if path == "/yt/search":
            if self.gate is not None:
                self.gate.wait(5)
            if self.fail is not None:
                raise self.fail
            return {"video": VIDEOS, "channel": CHANNELS, "playlist": PLAYLISTS}[params["kind"]]
        if path == "/yt/info":
            return {"id": params["id"], "title": "Lofi Girl - 1 A.M Study Session", "duration": 3615,
                    "channel_url": youtube.channel_url(UC)}
        if path == "/yt/list":
            return {"name": "Lofi Girl", "videos": [{"id": "n61ULEU7CO0", "title": "a", "duration": 3615},
                                                    {"id": "4xDzrJKXOOY", "title": "b", "duration": 1801}]}
        if path == "/yt/playlist":
            return {"name": "灌篮高手", "videos": [{"id": "slamdunk001", "title": "1", "duration": 1400},
                                                 {"id": "slamdunk002", "title": "2", "duration": 1400}]}
        if path == "/yt/health":
            return {"state": "ok"}
        raise AssertionError(path)

    def searches(self):
        return [c for c in self.calls if c[0] == "/yt/search"]


class ShapeTests(unittest.TestCase):
    def test_videos(self):
        items = ytsearch.shape("video", VIDEOS)
        self.assertEqual([i["id"] for i in items], ["n61ULEU7CO0", "4xDzrJKXOOY", "badchannel1"])
        self.assertEqual(items[0], {
            "kind": "video", "id": "n61ULEU7CO0", "title": "Lofi Girl - 1 A.M Study Session", "channel": "Lofi Girl",
            "channel_id": UC, "channel_link": "https://www.youtube.com/channel/" + UC, "duration": 3615,
            "views": 51000000, "date": 1600000000, "cover": "https://i.ytimg.com/vi/n61ULEU7CO0/mqdefault.jpg",
            "link": "https://www.youtube.com/watch?v=n61ULEU7CO0"})
        # An old node (no full=1 support): no views, date or channel id, still playable.
        self.assertEqual({k: items[1][k] for k in ("views", "date", "channel_id", "channel_link")},
                         {"views": 0, "date": 0, "channel_id": "", "channel_link": ""})
        self.assertEqual((items[2]["channel_id"], items[2]["views"]), ("", 0))

    def test_no_covers_on_the_cloud(self):
        self.assertEqual({i["cover"] for i in ytsearch.shape("video", VIDEOS, covers=False)}, {""})
        self.assertEqual({i["cover"] for i in ytsearch.shape("playlist", PLAYLISTS, covers=False)}, {""})

    def test_channels_and_playlists(self):
        self.assertEqual(ytsearch.shape("channel", CHANNELS), [
            {"kind": "channel", "id": UC, "name": "Lofi Girl", "fans": 15200000, "videos": 0,
             "link": "https://www.youtube.com/channel/" + UC},
            {"kind": "channel", "id": "UCOxqgCwgOqC2lMqC5PYz_Dg", "name": "lofi geek", "fans": 0, "videos": 120,
             "link": "https://www.youtube.com/channel/UCOxqgCwgOqC2lMqC5PYz_Dg"}])
        items = ytsearch.shape("playlist", PLAYLISTS)
        self.assertEqual(items[0], {"kind": "playlist", "id": PL, "title": "灌篮高手 全集", "channel": "动画",
                                    "count": 101, "cover": "https://i.ytimg.com/vi/n61ULEU7CO0/hqdefault.jpg",
                                    "link": "https://www.youtube.com/playlist?list=" + PL})
        self.assertEqual(items[1]["cover"], "", "only i.ytimg.com thumbnails")
        self.assertEqual(len(items), 2)

    def test_garbage(self):
        for answer in ({}, {"videos": None}, {"videos": ["x", 3]}, {"videos": [{"id": None}]}):
            self.assertEqual(ytsearch.shape("video", answer), [], answer)
        many = {"videos": [{"id": f"vid{n:08d}", "title": "t", "duration": 5} for n in range(40)]}
        self.assertEqual(len(ytsearch.shape("video", many)), ytsearch.COUNT)


class ClientTests(unittest.TestCase):
    def test_browse_asks_the_node_for_the_full_answer(self):
        node = FakeNode()
        YouTube(fetch_json=node).browse("lofi", "channel", 20, 25.0)
        self.assertEqual(node.calls, [("/yt/search", {"q": "lofi", "n": 20, "kind": "channel", "full": 1}, 25.0)])

    def test_node_status_kept_on_the_error(self):
        node = FakeNode()
        node.fail = NodeError(503, '{"error": "busy"}')
        with self.assertRaises(SourceError) as caught:
            YouTube(fetch_json=node).browse("lofi")
        self.assertEqual((caught.exception.status, caught.exception.reason), (503, "busy"))
        node.fail = ConnectionRefusedError(61, "refused")
        with self.assertRaises(SourceError) as caught:
            YouTube(fetch_json=node).browse("lofi")
        self.assertFalse(hasattr(caught.exception, "status"))


class SearchTests(unittest.TestCase):
    def setUp(self):
        self.now = 1000.0
        self.node = FakeNode()
        self.client = YouTube(fetch_json=self.node)
        self.search = ytsearch.YtSearch(clock=lambda: self.now)

    def ask(self, q="lofi", kind="video", who=("u", 1), place="local"):
        return self.search.search(self.client, who, q, kind, place)

    def test_cache(self):
        first = self.ask("Lofi  girl")
        self.assertTrue(first["ok"])
        self.assertEqual((first["q"], first["kind"], len(first["items"])), ("Lofi girl", "video", 3))
        self.now += 1
        self.assertEqual(self.ask("lofi girl", who=("u", 2)), first, "same words, any case, from the cache")
        self.assertEqual(len(self.node.searches()), 1)
        self.now += 5
        self.assertTrue(self.ask("lofi girl", kind="channel")["ok"])
        self.assertEqual(len(self.node.searches()), 2, "another kind is another search")
        self.now += ytsearch.CACHE_S + 1
        self.ask("lofi girl")
        self.assertEqual(len(self.node.searches()), 3, "ten minutes later it is asked again")

    def test_rate_limits(self):
        self.assertTrue(self.ask("a")["ok"])
        self.now += 1
        answer = self.ask("b")
        self.assertEqual((answer["ok"], answer["reason"], answer["retry"]), (False, "rate", ytsearch.GAP_S))
        self.assertTrue(self.ask("b", who=("u", 2))["ok"], "the gap is per person")
        self.now += ytsearch.GAP_S
        self.assertTrue(self.ask("c")["ok"])
        for n in range(ytsearch.PER_MINUTE - 3):
            self.assertTrue(self.ask(f"q{n}", who=("many", n))["ok"])
        answer = self.ask("one more", who=("late", 0))
        self.assertEqual((answer["reason"], "retry" in answer), ("rate", False))
        self.assertIn("一分钟", answer["error"])
        self.now += 61
        self.assertTrue(self.ask("one more", who=("late", 0))["ok"])

    def test_bad_requests_never_reach_the_node(self):
        for q, kind in (("", "video"), ("   ", "video"), ("x" * 61, "video"), ("lofi", "live"), ("a\x01b", "video")):
            answer = self.ask(q, kind)
            self.assertEqual((answer["ok"], answer["reason"]), (False, "bad"), (q, kind))
        self.assertEqual(self.node.calls, [])

    def test_off(self):
        answer = self.search.search(None, ("u", 1), "lofi", "video", "selfhost")
        self.assertEqual((answer["reason"], answer["error"]),
                         ("off", "YouTube 还没开启：去「设置 → YouTube」开启本机模式并填代理"))
        self.assertEqual(self.search.search(None, ("u", 1), "lofi", "video", "cloud")["error"], ytsearch.OFF_CLOUD)

    def test_errors_are_explained_and_not_cached(self):
        cases = [
            (NodeError(503, '{"error": "busy"}'), "local", "busy", ytsearch.BUSY),
            (NodeError(503, '{"error": "tools missing"}'), "local", "tools", ytsearch.TOOLS),
            (NodeError(503, '{"error": "ERROR: Sign in to confirm you’re not a bot"}'), "local", "login",
             ytsearch.LOGIN_LOCAL),
            (NodeError(503, '{"error": "ERROR: Sign in to confirm"}'), "cloud", "login", ytsearch.LOGIN_CLOUD),
            (NodeError(504, '{"error": "yt-dlp timed out"}'), "local", "slow", ytsearch.SLOW),
            (TimeoutError("timed out"), "local", "slow", ytsearch.SLOW),
            (ConnectionRefusedError(61, "Connection refused"), "local", "down", ytsearch.DOWN_LOCAL_NODE),
            (ConnectionRefusedError(61, "Connection refused"), "cloud", "down", ytsearch.DOWN_CLOUD),
            (NodeError(502, '{"error": "ERROR: Unable to download: Tunnel connection failed"}'), "local", "down",
             ytsearch.DOWN_LOCAL),
            (ValueError("not JSON"), "local", "down", ytsearch.DOWN_LOCAL_NODE),
        ]
        for n, (failure, place, reason, text) in enumerate(cases):
            self.node.fail = failure
            answer = self.ask(f"q{n}", who=("u", n), place=place)
            self.assertEqual((answer["ok"], answer["reason"], answer["error"]), (False, reason, text), failure)
            self.assertEqual("retry" in answer, reason == "busy")
        self.node.fail = None
        self.now += ytsearch.GAP_S
        self.assertTrue(self.ask("q0", who=("u", 0))["ok"], "a failure is not cached")

    def test_playlist_on_an_old_node(self):
        self.node.fail = NodeError(400, '{"error": "bad query"}')
        answer = self.ask(kind="playlist")
        self.assertEqual((answer["reason"], answer["error"]), ("unsupported", ytsearch.UNSUPPORTED))

    def test_one_search_at_a_time_and_the_same_one_is_shared(self):
        self.node.gate = threading.Event()
        answers = {}
        first = threading.Thread(target=lambda: answers.setdefault("first", self.ask("slow", who=("u", 1))))
        first.start()
        for _ in range(200):
            if self.node.searches():
                break
            threading.Event().wait(0.01)
        other = self.ask("something else", who=("u", 2))
        self.assertEqual((other["reason"], other["error"], other["retry"]),
                         ("busy", ytsearch.RUNNING, ytsearch.BUSY_RETRY_S), "no queue behind the node")
        same = threading.Thread(target=lambda: answers.setdefault("same", self.ask("SLOW", who=("u", 3))))
        same.start()
        threading.Event().wait(0.05)
        self.node.gate.set()
        first.join(5)
        same.join(5)
        self.assertTrue(answers["first"]["ok"])
        self.assertEqual(answers["same"], answers["first"])
        self.assertEqual(len(self.node.searches()), 1, "the second asker waited for the first")


ALICE_DEVICE = "aaaaaaaaaaa1"


class EndpointTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.now = 1000.0
        self.tick = 0.0
        self.accounts = Accounts(root / "tv.db", clock=lambda: self.now)
        self.alice = self.accounts.create_user("Alice")
        row = self.accounts.register_device(ALICE_DEVICE, "xingzhi-cube-154", "s" * 43)
        self.accounts.bind(row["claim_code"], self.alice)
        self.devices_dir = root / "devices"
        self.node = FakeNode()
        self.relay = web.Relay(None, FakeClient(), None, "/opt/ff", KEY, accounts=self.accounts,
                               devices_dir=self.devices_dir, youtube=YouTube(fetch_json=self.node),
                               log=lambda _m: None, clock=lambda: self.now, monotonic=lambda: self.tick,
                               starter=lambda *a: FakeProc(), pumper=lambda *a: "ended")
        self.relay.bili_search.fetch = lambda url, timeout: ("image/png", PNG)
        self.relay.sync_devices(force=True)
        self.server = _Server()
        self.server.start(self.relay)
        self.cookies = {"alice": "tvs=" + self.accounts.new_session(self.alice),
                        "guest": "tvs=" + self.accounts.new_session(None, "guest", device_id=ALICE_DEVICE),
                        "nobody": "tvs=not-a-session"}

    def tearDown(self):
        self.server.stop()
        self.accounts.close()
        self.tmp.cleanup()

    def call(self, method, path, body=None, who="alice"):
        status, _, payload = self.server.raw(method, path, body if body is not None else
                                             ({} if method == "POST" else None), {"Cookie": self.cookies[who]})
        return status, (json.loads(payload) if status == 200 and payload[:1] == b"{" else payload)

    def test_needs_an_account(self):
        self.assertTrue(self.call("GET", "/api/yt/search?q=lofi")[1]["ok"])
        for who in ("guest", "nobody"):
            self.assertEqual(self.call("GET", "/api/yt/search?q=lofi2", who=who)[0], 404, who)
        self.assertEqual(len(self.node.searches()), 1)

    def test_cloud_has_no_covers_local_mode_does(self):
        status, answer = self.call("GET", "/api/yt/search?q=lofi")
        self.assertEqual((status, answer["kind"]), (200, "video"))
        self.assertEqual({i["cover"] for i in answer["items"]}, {""}, "the cloud relay never fetches from YouTube")
        self.relay.youtube_kind = "local"
        self.tick += ytsearch.GAP_S
        answer = self.call("GET", "/api/yt/search?q=lofi&kind=playlist")[1]
        self.assertTrue(answer["items"][0]["cover"].startswith("https://i.ytimg.com/vi/"))
        # The cover goes through the thumbnail proxy, which allowlists i.ytimg.com.
        thumb = "/api/bili/thumb?u=" + answer["items"][0]["cover"]
        fetched = []
        self.relay.bili_search.fetch_youtube = lambda url, timeout, proxy: (fetched.append(url), ("image/jpeg", PNG))[1]
        self.assertEqual(self.call("GET", thumb)[0], 200)
        self.assertEqual(fetched, ["https://i.ytimg.com/vi/n61ULEU7CO0/hqdefault.jpg"])

    def test_off_on_a_home_server(self):
        self.relay.clients["youtube"] = None
        answer = self.call("GET", "/api/yt/search?q=lofi")[1]
        self.assertEqual(answer["reason"], "off")
        self.assertEqual(answer["error"], ytsearch.OFF_CLOUD)
        self.assertEqual(ytsearch.place_of(self.relay), "cloud")
        self.assertEqual(ytsearch.place_of(types.SimpleNamespace(selfhost=object(), youtube_kind="")), "selfhost")
        self.assertEqual(ytsearch.place_of(types.SimpleNamespace(selfhost=object(), youtube_kind="local")), "local")

    def test_busy_node(self):
        self.node.fail = NodeError(503, '{"error": "busy"}')
        answer = self.call("GET", "/api/yt/search?q=lofi")[1]
        self.assertEqual((answer["ok"], answer["error"], answer["retry"]), (False, "YouTube 节点正忙，稍后再试", 5))

    def test_results_play_and_subscribe_through_the_existing_paths(self):
        video = self.call("GET", "/api/yt/search?q=lofi")[1]["items"][0]
        self.tick += ytsearch.GAP_S
        channel = self.call("GET", "/api/yt/search?q=lofi&kind=channel")[1]["items"][0]
        self.tick += ytsearch.GAP_S
        playlist = self.call("GET", "/api/yt/search?q=slam%20dunk&kind=playlist")[1]["items"][0]
        # 播放: the page's play() posts {kind: "cast", url: item.link} to the chosen TV.
        files = device_files.device_files(self.devices_dir, ALICE_DEVICE)
        now_playing.write(files.now, "ch000", True, True, now=self.now, since=self.now)
        status, answer = self.call("POST", "/api/play", {"target": ALICE_DEVICE,
                                                         "item": {"kind": "cast", "url": video["link"]}})
        self.assertTrue(answer["ok"], answer)
        self.assertIn(("/yt/info", {"id": "n61ULEU7CO0"}, None), self.node.calls)
        self.assertEqual(events.read_event(files.events)[1], "cast")
        # 订阅频道 from a video row and from a channel row: the same channel link.
        self.assertEqual(video["channel_link"], channel["link"])
        answer = self.call("POST", "/api/creators", {"url": channel["link"]})[1]
        self.assertTrue(answer["ok"], answer)
        creators = self.call("GET", "/api/state")[1]["creators"]
        self.assertEqual([(c["site"], c["source"]) for c in creators], [("youtube", channel["link"])],
                         "the page marks the row 已订阅 by this source")
        # 加为剧集 for a playlist.
        answer = self.call("POST", "/api/series", {"url": playlist["link"], "name": ""})[1]
        self.assertTrue(answer["ok"], answer)
        series = self.call("GET", "/api/state")[1]["series"]
        self.assertEqual([(s["site"], s["source"]) for s in series], [("youtube", PL)])


class ConsolePageTests(unittest.TestCase):
    """The search page's wiring, read from the shipped files (no browser in CI)."""

    @classmethod
    def setUpClass(cls):
        assets = Path(__file__).resolve().parents[1] / "server" / "relay" / "assets"
        cls.html = (assets / "console.html").read_text(encoding="utf-8")
        cls.js = (assets / "console.js").read_text(encoding="utf-8")

    def test_source_switch_and_kinds(self):
        self.assertIn('id="srcSwitch"', self.html)
        for kind in ("video", "channel", "playlist"):
            self.assertIn(f'<button data-kind="{kind}"', self.html)
        self.assertIn('lsGet("console.search.src"', self.js, "the choice is remembered per browser")

    def test_actions_use_the_existing_endpoints(self):
        self.assertIn('"api/yt/search?"', self.js)
        self.assertIn('play({ kind: "cast", url: item.link, title: item.title }', self.js)
        self.assertIn('call("POST", "api/creators", { url: link })', self.js)
        self.assertIn('call("POST", "api/series", { url: item.link, name: "" })', self.js)
        self.assertIn("YouTube 还没开启：去「设置 → YouTube」开启本机模式并填代理", self.js)
        self.assertIn("需要导入 cookies", self.js)


if __name__ == "__main__":
    unittest.main()
