"""The console's 订阅 page: real upload counts, play on the TV, Bilibili search and cover thumbnails.

Everything runs against canned Bilibili answers (no network): the search payloads
follow the shape of the unsigned /x/web-interface/search/type API the voice search
already uses, trimmed to the fields the page shows.
"""

import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from server import now_playing
from server.relay import bilibili, bilisearch, events, web
from server.relay.bilibili import Bilibili, BilibiliError
from server.relay.state import State
from server.tenancy import files as device_files
from server.tenancy.accounts import Accounts

from test_relay_bilibili import CARD, LIST, SPI, FakeFetch
from test_relay_web import KEY, FakeClient, FakeProc

LIST_WITH_COUNT = {"code": 0, "data": {**LIST["data"], "page": {"num": 1, "size": 20, "count": 356}}}
CARD_WITH_COUNT = {"code": 0, "data": {**CARD["data"], "archive_count": 680}}

SEARCH_VIDEOS = {"code": 0, "data": {"page": 1, "numPages": 50, "numResults": 1000, "result": [
    {"type": "video", "bvid": "BV1xx411c7mD", "title": "【<em class=\"keyword\">舒克和贝塔</em>】4K修复全13集",
     "description": "上海美术电影制片厂 1989", "pic": "//i0.hdslb.com/bfs/archive/abc123.jpg", "author": "上美影",
     "mid": 20165629, "duration": "24:05", "play": 123456, "pubdate": 1700000000,
     "arcurl": "http://www.bilibili.com/video/av1"},
    {"type": "video", "bvid": "BV1yy411c7mE", "title": "舒克开飞机", "description": "", "pic": "https://evil.example/x.jpg",
     "author": "路人", "mid": "x", "duration": "3:00", "play": "1.5万", "arcurl": "http://www.bilibili.com/video/av2"},
    {"type": "video", "bvid": "BV1zz411c7mF", "title": "海尔兄弟 第1集", "pic": "http://i2.hdslb.com/bfs/archive/b.png",
     "author": "番剧", "mid": 1, "duration": "24:00", "play": "--",
     "arcurl": "https://www.bilibili.com/bangumi/play/ep52309"},
    {"type": "video", "bvid": "BV1liveAAAAA", "title": "直播", "is_live_room_inline": 1,
     "arcurl": "https://live.bilibili.com/1"},
    {"type": "video", "bvid": "BV1cheeseAAA", "title": "课程", "arcurl": "https://www.bilibili.com/cheese/play/ep1"},
    {"type": "video", "bvid": "BV1xx411c7mD", "title": "重复"},
    {"type": "ketang", "bvid": "BV1ketangAAA", "title": "课堂"},
]}}
SEARCH_USERS = {"code": 0, "data": {"page": 1, "numPages": 1, "numResults": 2, "result": [
    {"type": "bili_user", "mid": 546195, "uname": "<em class=\"keyword\">老番茄</em>", "usign": "新浪微博：_老番茄_",
     "upic": "//i1.hdslb.com/bfs/face/f.jpg", "fans": 20840107, "videos": 680},
    {"type": "bili_user", "mid": 1, "uname": "没有投稿", "fans": 3, "videos": 0},
]}}
SEARCH_BANGUMI = {"code": 0, "data": {"page": 1, "numPages": 1, "numResults": 1, "result": [
    {"type": "media_bangumi", "season_id": 2307, "media_id": 90, "title": "<em class=\"keyword\">海尔兄弟</em>",
     "cover": "http://i0.hdslb.com/bfs/bangumi/p.jpg", "desc": "经典科普动画", "ep_size": 52,
     "styles": "科普/冒险", "season_type_name": "番剧"},
    {"type": "media_bangumi", "season_id": "bad", "title": "坏 id"},
]}}


class SearchFetch(FakeFetch):
    def __call__(self, url, cookie, timeout=None):
        return super().__call__(url, cookie)


class CountTests(unittest.TestCase):
    def client(self, **answers):
        base = {"finger/spi": SPI, "web-interface/card": CARD, "space/arc/list": LIST}
        base.update(answers)
        self.fetch = SearchFetch(base)
        return Bilibili(fetch_json=self.fetch)

    def test_total_from_the_list_page(self):
        client = self.client(**{"space/arc/list": LIST_WITH_COUNT})
        self.assertEqual(len(client.uploads("546195")), 2, "still only the newest page is kept")
        self.assertEqual(client.upload_total("546195"), 356)
        self.assertEqual(client.upload_total("1"), 0, "unknown creators have no total")

    def test_total_falls_back_to_the_card(self):
        client = self.client(**{"web-interface/card": CARD_WITH_COUNT})
        client.creator("546195")
        self.assertEqual(client.upload_total("546195"), 680)

    def test_no_total_anywhere(self):
        client = self.client()
        client.creator("546195")
        self.assertEqual(client.upload_total("546195"), 0)

    def test_state_keeps_totals_and_old_readers_ignore_them(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "relay-state.json"
            state = State(path)
            creator = state.add("bilibili", "546195", "老番茄", [], 1.0)
            state.set_total(creator.id, 356)
            state.set_total(creator.id, 0)          # 0 = unknown, keeps the old value
            state.set_total("up99", 5)              # no such channel
            state.save()
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved["totals"], {"up01": 356})
            self.assertNotIn("totals", saved["creators"][0], "not in Creator: older relays would refuse the file")
            self.assertEqual(State(path).totals, {"up01": 356})
            state.remove("up01")
            state.save()
            self.assertNotIn("totals", json.loads(path.read_text(encoding="utf-8")))
            # A file written before this change, and a damaged totals entry, both load.
            path.write_text(json.dumps({"creators": [], "totals": {"up01": "x", "up02": -1}}), encoding="utf-8")
            self.assertEqual(State(path).totals, {})


class ShapeTests(unittest.TestCase):
    def test_cover_allowlist(self):
        ok = {"//i0.hdslb.com/bfs/archive/a_b-1.jpg": "https://i0.hdslb.com/bfs/archive/a_b-1.jpg",
              "http://i2.hdslb.com/bfs/face/x.PNG": "https://i2.hdslb.com/bfs/face/x.PNG",
              "https://s1.hdslb.com/bfs/static/y.webp": "https://s1.hdslb.com/bfs/static/y.webp"}
        for raw, url in ok.items():
            self.assertEqual(bilibili.cover_url(raw), url, raw)
        for raw in ["https://evil.example/bfs/a.jpg", "https://i0.hdslb.com.evil.example/bfs/a.jpg",
                    "https://evilhdslb.com/bfs/a.jpg", "https://i0.hdslb.com:8443/bfs/a.jpg",
                    "https://user@i0.hdslb.com/bfs/a.jpg", "https://i0.hdslb.com/bfs/a.jpg?x=1",
                    "https://i0.hdslb.com/other/a.jpg", "https://i0.hdslb.com/bfs/../a.jpg",
                    "https://i0.hdslb.com/bfs//a.jpg", "https://i0.hdslb.com/bfs/a.svg",
                    "ftp://i0.hdslb.com/bfs/a.jpg", "file:///bfs/a.jpg", "", None, "https://[::1]/bfs/a.jpg"]:
            self.assertEqual(bilibili.cover_url(raw), "", raw)

    def test_videos(self):
        items = bilibili.browse_videos(SEARCH_VIDEOS["data"])
        self.assertEqual([i["id"] for i in items], ["BV1xx411c7mD", "BV1yy411c7mE", "BV1zz411c7mF"])
        first, second, bangumi = items
        self.assertEqual(first, {"kind": "video", "id": "BV1xx411c7mD", "title": "【舒克和贝塔】4K修复全13集",
                                 "desc": "上海美术电影制片厂 1989",
                                 "cover": "https://i0.hdslb.com/bfs/archive/abc123.jpg", "author": "上美影",
                                 "mid": "20165629", "duration": 1445, "play": 123456, "pubdate": 1700000000,
                                 "series": True, "link": "https://www.bilibili.com/video/BV1xx411c7mD"})
        self.assertEqual((second["cover"], second["mid"], second["play"], second["series"]), ("", "", 15000, False))
        self.assertTrue(bangumi["series"])
        self.assertEqual(bangumi["link"], "https://www.bilibili.com/bangumi/play/ep52309")
        self.assertEqual(bangumi["play"], 0)

    def test_users_and_bangumi(self):
        self.assertEqual(bilibili.browse_users(SEARCH_USERS["data"]), [
            {"kind": "user", "id": "546195", "name": "老番茄", "desc": "新浪微博：_老番茄_",
             "cover": "https://i1.hdslb.com/bfs/face/f.jpg", "fans": 20840107, "videos": 680}])
        self.assertEqual(bilibili.browse_bangumi(SEARCH_BANGUMI["data"]), [
            {"kind": "bangumi", "id": "ss2307", "title": "海尔兄弟", "desc": "经典科普动画",
             "cover": "https://i0.hdslb.com/bfs/bangumi/p.jpg", "eps": 52, "styles": "科普/冒险", "badge": "番剧",
             "link": "https://www.bilibili.com/bangumi/play/ss2307"}])

    def test_browse_calls_the_unsigned_search(self):
        fetch = SearchFetch({"finger/spi": SPI, "search/type": SEARCH_VIDEOS})
        found = Bilibili(fetch_json=fetch).browse("舒克", "video", 3)
        url = fetch.calls[-1][0]
        self.assertIn("/x/web-interface/search/type?", url)
        for part in ("search_type=video", "page=3"):
            self.assertIn(part, url)
        self.assertEqual((found["page"], found["pages"], found["total"]), (3, bilibili.BROWSE_PAGES, 1000))
        fetch.answers["search/type"] = SEARCH_USERS
        Bilibili(fetch_json=fetch).browse("老番茄", "user")
        self.assertIn("search_type=bili_user", fetch.calls[-1][0])
        with self.assertRaises(ValueError):
            Bilibili(fetch_json=fetch).browse("x", "live")


class BrowseClient(FakeClient):
    """FakeClient plus the search the console uses and a total like Bilibili's."""

    def __init__(self):
        super().__init__()
        self.browsed = []
        self.fail = None

    def upload_total(self, mid):
        return 356

    def browse(self, query, kind, page):
        self.browsed.append((query, kind, page))
        if self.fail is not None:
            raise self.fail
        shape = {"video": bilibili.browse_videos, "user": bilibili.browse_users, "bangumi": bilibili.browse_bangumi}
        data = {"video": SEARCH_VIDEOS, "user": SEARCH_USERS, "bangumi": SEARCH_BANGUMI}[kind]["data"]
        return {"items": shape[kind](data), "page": page, "pages": 3, "total": 1000}


PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 100


class _Server:
    def start(self, relay):
        self.relay = relay
        self.streams, self.manage = web.make_servers(relay, 0, 0)
        for server in (self.streams, self.manage):
            threading.Thread(target=server.serve_forever, daemon=True).start()

    def stop(self):
        for server in (self.streams, self.manage):
            server.shutdown()
            server.server_close()

    def raw(self, method, path, body=None, headers=None):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(f"http://127.0.0.1:{self.manage.server_address[1]}{path}",
                                         data=data, method=method)
        for name, value in (headers or {}).items():
            request.add_header(name, value)
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=5) as answer:
                return answer.status, answer.headers, answer.read()
        except urllib.error.HTTPError as error:
            return error.code, error.headers, error.read()


class SingleUserTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.now = 1000.0
        self.tick = 0.0
        self.events = root / "relay-events.json"
        self.playing = root / "now-playing.json"
        self.client = BrowseClient()
        self.images = []
        self.relay = web.Relay(State(root / "relay-state.json"), self.client, root / "relay-channels.txt", "/opt/ff",
                               KEY, events_out=self.events, now_playing_in=self.playing,
                               log=lambda _m: None, clock=lambda: self.now, monotonic=lambda: self.tick,
                               starter=lambda *a: FakeProc(), pumper=lambda *a: "ended")
        self.relay.bili_search.fetch = self.fake_image
        self.server = _Server()
        self.server.start(self.relay)

    def tearDown(self):
        self.server.stop()
        self.tmp.cleanup()

    def fake_image(self, url, timeout):
        self.images.append(url)
        return "image/png", PNG

    def call(self, method, path, body=None, key=KEY):
        status, _, payload = self.server.raw(method, path, body if body is not None else
                                             ({} if method == "POST" else None), {"X-Key": key} if key else {})
        self.assertEqual(status, 200, path)
        return json.loads(payload)

    def test_summary_counts_the_creators_real_total(self):
        self.call("POST", "/api/creators", {"url": "https://space.bilibili.com/7"})
        creator = self.call("GET", "/api/state")["creators"][0]
        self.assertEqual({k: creator[k] for k in ("left", "total", "count", "source")},
                         {"left": 3, "total": 3, "count": 356, "source": "7"})
        self.assertEqual(self.relay.state.totals, {"up01": 356}, "remembered across restarts")

    def test_count_never_below_what_is_loaded(self):
        self.client.upload_total = lambda mid: 0
        self.call("POST", "/api/creators", {"url": "https://space.bilibili.com/7"})
        self.assertEqual(self.call("GET", "/api/state")["creators"][0]["count"], 3)

    def test_refresh_updates_the_total(self):
        self.call("POST", "/api/creators", {"url": "https://space.bilibili.com/7"})
        self.client.upload_total = lambda mid: 400
        self.now += 31 * 60
        self.relay.refresh_if_stale("up01")
        self.assertEqual(self.relay.state.totals["up01"], 400)

    def test_play_on_the_tv_uses_the_switch(self):
        self.call("POST", "/api/creators", {"url": "https://space.bilibili.com/7"})
        self.assertEqual(self.call("POST", "/api/subs/play", {"channel": "up01"})["error"], "小智没在线")
        now_playing.write(self.playing, "ch001", True, True, now=self.now, since=self.now)
        self.assertEqual(self.call("POST", "/api/subs/play", {"channel": "up09"})["error"], "没有这个台")
        self.assertEqual(self.call("POST", "/api/subs/play", {"channel": "ch000"})["error"], "没有这个台",
                         "only subscriptions, not live channels")
        self.assertEqual(self.call("POST", "/api/subs/play", {"channel": "up01", "device": "d0"}),
                         {"ok": True, "at": self.now, "channel": "up01", "name": "UP7"})
        self.assertEqual(events.read_event(self.events), (1, "switch", "up01", None, "", 0))
        self.assertEqual(self.server.raw("POST", "/api/subs/play", {"channel": "up01"})[0], 404, "needs the key")

    def test_search(self):
        answer = self.call("GET", "/api/bili/search?q=%E8%88%92%E5%85%8B&kind=video&page=2")
        self.assertTrue(answer["ok"])
        self.assertEqual((answer["q"], answer["kind"], answer["page"], answer["pages"]), ("舒克", "video", 2, 3))
        self.assertEqual(len(answer["items"]), 3)
        self.assertEqual(self.client.browsed, [("舒克", "video", 2)])
        # The same search again comes from the cache, even straight away.
        self.assertEqual(self.call("GET", "/api/bili/search?q=%E8%88%92%E5%85%8B&kind=video&page=2"), answer)
        self.assertEqual(len(self.client.browsed), 1)
        # A different search within a second is turned away without asking Bilibili.
        busy = self.call("GET", "/api/bili/search?q=x&kind=user")
        self.assertFalse(busy["ok"])
        self.assertEqual(busy["retry"], bilisearch.SEARCH_GAP_S)
        self.tick += 2
        self.assertTrue(self.call("GET", "/api/bili/search?q=x&kind=user")["ok"])
        for bad in ("q=&kind=video", "q=x&kind=live", "q=x&page=0", "q=x&page=99", "q=x&page=two",
                    "q=" + "x" * 41):
            self.assertFalse(self.call("GET", "/api/bili/search?" + bad)["ok"], bad)
        self.assertEqual(self.server.raw("GET", "/api/bili/search?q=x")[0], 404, "needs the key")

    def test_search_errors_say_what_to_check(self):
        self.client.fail = BilibiliError("search: URLError: unreachable")
        self.assertEqual(self.call("GET", "/api/bili/search?q=a")["error"], "B 站连不上：检查路由器或代理")
        self.tick += 2
        self.client.fail = BilibiliError("search: code=-412", permanent=True)
        self.assertIn("拒绝", self.call("GET", "/api/bili/search?q=b")["error"])

    def test_search_has_a_site_wide_limit(self):
        for n in range(bilisearch.SEARCH_PER_MINUTE):
            self.tick += 1.1
            self.assertTrue(self.relay.bili_search.search(self.client, ("u", n), f"q{n}", "video", 1)["ok"])
        self.tick += 1.1
        self.assertIn("一分钟", self.relay.bili_search.search(self.client, ("u", 99), "last", "video", 1)["error"])
        self.tick += 60
        self.assertTrue(self.relay.bili_search.search(self.client, ("u", 99), "last", "video", 1)["ok"])

    def test_thumbnail_proxy(self):
        url = "//i0.hdslb.com/bfs/archive/abc123.jpg"
        status, headers, body = self.server.raw("GET", "/api/bili/thumb?s=v&u=" + urllib.request.quote(url),
                                                headers={"X-Key": KEY})
        self.assertEqual((status, body), (200, PNG))
        self.assertEqual(headers["Content-Type"], "image/png")
        self.assertEqual(headers["Cache-Control"], "private, max-age=86400")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(self.images, ["https://i0.hdslb.com/bfs/archive/abc123.jpg@320w_200h_1c.webp"])
        self.server.raw("GET", "/api/bili/thumb?s=v&u=" + urllib.request.quote(url), headers={"X-Key": KEY})
        self.assertEqual(len(self.images), 1, "served from memory the second time")
        for bad in ("https://evil.example/bfs/a.jpg", "http://127.0.0.1:8099/ch/up01",
                    "https://i0.hdslb.com/bfs/a.jpg?x=1", "https://i0.hdslb.com.evil.example/bfs/a.jpg"):
            status = self.server.raw("GET", "/api/bili/thumb?u=" + urllib.request.quote(bad), headers={"X-Key": KEY})[0]
            self.assertEqual(status, 404, bad)
        self.assertEqual(self.server.raw("GET", "/api/bili/thumb?s=huge&u=" + urllib.request.quote(url),
                                         headers={"X-Key": KEY})[0], 404)
        self.assertEqual(len(self.images), 1, "nothing outside the allowlist is fetched")
        self.assertEqual(self.server.raw("GET", "/api/bili/thumb?u=" + urllib.request.quote(url))[0], 404,
                         "needs the key")

    def test_thumbnail_refuses_big_or_odd_answers(self):
        thumbs = bilisearch.BiliSearch(clock=lambda: self.tick)
        thumbs.fetch = lambda url, timeout: ("image/png", b"x" * (bilisearch.THUMB_MAX_BYTES + 1))
        self.assertEqual(thumbs.thumb("https://i0.hdslb.com/bfs/a.jpg", "v")[0], 502)
        thumbs.fetch = lambda url, timeout: ("text/html", b"<script>")
        self.assertEqual(thumbs.thumb("https://i0.hdslb.com/bfs/b.jpg", "f")[0], 502)
        calls = []

        def down(url, timeout):
            calls.append(url)
            raise urllib.error.URLError("unreachable")
        thumbs.fetch = down
        self.assertEqual(thumbs.thumb("https://i0.hdslb.com/bfs/c.jpg", "p")[0], 502)
        self.assertEqual(thumbs.thumb("https://i0.hdslb.com/bfs/d.jpg", "p")[0], 502)
        self.assertEqual(len(calls), 1, "after a network error the image host is left alone for a while")
        self.tick += bilisearch.THUMB_DOWN_S
        thumbs.thumb("https://i0.hdslb.com/bfs/d.jpg", "p")
        self.assertEqual(len(calls), 2)

    def test_no_redirects_are_followed(self):
        handler = bilisearch._NoRedirect()
        self.assertIsNone(handler.redirect_request(None, None, 302, "Found", {}, "https://evil.example/"))


ALICE_DEVICE = "aaaaaaaaaaa1"
BOB_DEVICE = "bbbbbbbbbbb2"


class TenantTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.now = 1000.0
        self.accounts = Accounts(root / "tv.db", clock=lambda: self.now)
        self.admin = self.accounts.create_user("管理员", "admin")
        self.alice = self.accounts.create_user("Alice")
        self.bob = self.accounts.create_user("Bob")
        for device, user in ((ALICE_DEVICE, self.alice), (BOB_DEVICE, self.bob)):
            row = self.accounts.register_device(device, "xingzhi-cube-154", "s" * 43)
            self.accounts.bind(row["claim_code"], user)
        self.devices_dir = root / "devices"
        self.client = BrowseClient()
        self.relay = web.Relay(None, self.client, None, "/opt/ff", KEY, accounts=self.accounts,
                               devices_dir=self.devices_dir, log=lambda _m: None, clock=lambda: self.now,
                               starter=lambda *a: FakeProc(), pumper=lambda *a: "ended")
        self.relay.bili_search.fetch = lambda url, timeout: ("image/png", PNG)
        self.relay.sync_devices(force=True)
        self.server = _Server()
        self.server.start(self.relay)
        self.cookies = {name: "tvs=" + self.accounts.new_session(uid) for name, uid in
                        (("alice", self.alice), ("bob", self.bob))}
        guest = self.accounts.new_session(None, "guest", device_id=ALICE_DEVICE)
        self.cookies["guest"] = "tvs=" + guest

    def tearDown(self):
        self.server.stop()
        self.accounts.close()
        self.tmp.cleanup()

    def files(self, device):
        return device_files.device_files(self.devices_dir, device)

    def online(self, device):
        now_playing.write(self.files(device).now, "ch000", True, True, now=self.now, since=self.now)

    def call(self, who, method, path, body=None):
        status, _, payload = self.server.raw(method, path, body if body is not None else
                                             ({} if method == "POST" else None), {"Cookie": self.cookies[who]})
        return status, (json.loads(payload) if status == 200 and payload[:1] == b"{" else payload)

    def test_counts_are_per_user(self):
        self.call("alice", "POST", "/api/creators", {"url": "https://space.bilibili.com/7"})
        self.assertEqual(self.call("alice", "GET", "/api/state")[1]["creators"][0]["count"], 356)
        self.assertEqual(self.call("bob", "GET", "/api/state")[1]["creators"], [])

    def test_play_only_on_your_own_device(self):
        self.call("alice", "POST", "/api/creators", {"url": "https://space.bilibili.com/7"})
        self.online(ALICE_DEVICE)
        self.online(BOB_DEVICE)
        status, answer = self.call("alice", "POST", "/api/subs/play", {"channel": "up01", "device": ALICE_DEVICE})
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(events.read_event(self.files(ALICE_DEVICE).events)[1:3], ("switch", "up01"))
        # Bob's device is not Alice's to see: 404, like every other device API.
        self.assertEqual(self.call("alice", "POST", "/api/subs/play", {"channel": "up01", "device": BOB_DEVICE})[0], 404)
        # Bob has no up01 of his own.
        self.assertEqual(self.call("bob", "POST", "/api/subs/play", {"channel": "up01"})[1]["error"], "没有这个台")
        # A guest has no subscriptions.
        self.assertEqual(self.call("guest", "POST", "/api/subs/play", {"channel": "up01"})[0], 404)

    def test_a_member_does_not_switch_the_owners_numbering(self):
        self.call("alice", "POST", "/api/creators", {"url": "https://space.bilibili.com/7"})
        self.call("bob", "POST", "/api/creators", {"url": "https://space.bilibili.com/8"})
        with self.accounts.lock:
            self.accounts.conn.execute("INSERT INTO device_member(device_id, user_id, role) VALUES (?, ?, 'member')",
                                       (ALICE_DEVICE, self.bob))
            self.accounts.conn.commit()
        self.online(ALICE_DEVICE)
        _, answer = self.call("bob", "POST", "/api/subs/play", {"channel": "up01", "device": ALICE_DEVICE})
        self.assertFalse(answer["ok"])
        self.assertIn("主人", answer["error"])
        self.assertIsNone(events.read_event(self.files(ALICE_DEVICE).events))

    def test_search_and_thumbnails_need_an_account(self):
        url = "/api/bili/thumb?u=" + urllib.request.quote("https://i0.hdslb.com/bfs/a.jpg")
        self.assertTrue(self.call("alice", "GET", "/api/bili/search?q=x")[1]["ok"])
        self.assertEqual(self.call("alice", "GET", url)[0], 200)
        for who in ("guest",):
            self.assertEqual(self.call(who, "GET", "/api/bili/search?q=y")[0], 404)
            self.assertEqual(self.call(who, "GET", url)[0], 404)
        self.cookies["nobody"] = "tvs=not-a-session"
        self.assertEqual(self.call("nobody", "GET", "/api/bili/search?q=y")[0], 404)
        self.assertEqual(self.call("nobody", "GET", url)[0], 404)


if __name__ == "__main__":
    unittest.main()
