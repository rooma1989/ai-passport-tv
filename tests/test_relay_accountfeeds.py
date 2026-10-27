"""Account feeds on the household server / PC client (server/relay/accountfeeds.py, follows.py).

Neither Bilibili nor YouTube is contacted. The Bilibili answers below have the shapes of the web APIs
(index/top/feed/rcmd, history/cursor, polymer web-dynamic feed/all, relation/followings, history/report)
as documented by bilibili-API-collect; the YouTube answers have the shape yt-dlp's flat-playlist JSON gives
for :ytrec / :ythis / :ytsubs. The cookie values are the sentinels of test_relay_bililogin and must never
appear in an answer or a log line.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from server.relay import accountfeeds, follows, home, ytnode
from server.relay.bilibili import BLOCKED_HINT, SourceError
from server.relay.state import MAX_CREATORS, Video
from server.relay.youtube import YouTube

from test_relay_bililogin import JCT, NAV_OUT, SESSDATA, FakeBili, RelayHarness

UC1 = "UC" + "a" * 22
UC2 = "UC" + "b" * 22

RCMD = {"code": 0, "data": {"item": [
    {"id": 101, "bvid": "BV1rcmdaaaa1", "cid": 11, "goto": "av", "uri": "https://www.bilibili.com/video/BV1rcmdaaaa1",
     "pic": "http://i0.hdslb.com/bfs/archive/r1.jpg", "title": "推荐的 <一>", "duration": 300, "pubdate": 1760000000,
     "owner": {"mid": 2001, "name": "甲", "face": "https://i1.hdslb.com/bfs/face/a.jpg"},
     "stat": {"view": 10, "like": 1, "danmaku": 0}, "is_followed": 1,
     "rcmd_reason": {"content": "已关注", "reason_type": 1}},
    {"id": 0, "bvid": "", "goto": "ad", "title": "广告"},
    {"id": 5, "bvid": "", "goto": "live", "title": "直播间", "room_info": {}},
    {"id": 102, "bvid": "BV1rcmdaaaa2", "goto": "av", "pic": "https://evil.example/x.jpg", "title": "推荐二",
     "duration": 61, "pubdate": 1760000100, "owner": {"mid": 2002, "name": "乙", "face": ""}}]}}
HISTORY = {"code": 0, "data": {"cursor": {"max": 9, "view_at": 1760000900, "business": "archive", "ps": 30}, "list": [
    {"title": "看了一半", "cover": "http://i0.hdslb.com/bfs/archive/h1.jpg", "videos": 1,
     "history": {"oid": 1, "bvid": "BV1hist00001", "page": 1, "cid": 51, "part": "", "business": "archive", "dt": 2},
     "author_name": "丙", "author_face": "https://i0.hdslb.com/bfs/face/c.jpg", "author_mid": 3001,
     "view_at": 1760000900, "progress": 125, "duration": 600, "kid": 1},
    {"title": "看完了", "cover": "http://i0.hdslb.com/bfs/archive/h2.jpg", "videos": 1,
     "history": {"bvid": "BV1hist00002", "page": 1, "business": "archive"}, "author_name": "丁",
     "view_at": 1760000800, "progress": -1, "duration": 300},
    {"title": "多集视频", "cover": "http://i0.hdslb.com/bfs/archive/h3.jpg", "videos": 5,
     "history": {"bvid": "BV1hist00003", "page": 3, "cid": 53, "part": "第三部分", "business": "archive"},
     "author_name": "戊", "view_at": 1760000700, "progress": 400, "duration": 900},
    {"title": "海尔兄弟", "show_title": "第3话", "cover": "http://i0.hdslb.com/bfs/bangumi/p.jpg",
     "history": {"business": "pgc", "epid": 33, "bvid": "BV1pgc000009"}, "kid": 28, "view_at": 1760000600,
     "progress": 100, "duration": 1400},
    {"title": "直播间", "history": {"business": "live", "oid": 7}, "view_at": 1760000500},
    {"title": "一开头", "history": {"bvid": "BV1hist00004", "business": "archive"}, "progress": 10, "duration": 600,
     "view_at": 1760000400}]}}
DYNAMIC = {"code": 0, "data": {"has_more": True, "offset": "900", "items": [
    {"id_str": "1", "type": "DYNAMIC_TYPE_AV", "modules": {
        "module_author": {"mid": 1000, "name": "UP 主0", "face": "https://i0.hdslb.com/bfs/face/f0.jpg",
                          "pub_ts": 1760000500, "pub_action": "投稿了视频"},
        "module_dynamic": {"major": {"type": "MAJOR_TYPE_ARCHIVE", "archive": {
            "aid": "9", "bvid": "BV1dyna00001", "cover": "http://i0.hdslb.com/bfs/archive/d1.jpg",
            "title": "新视频", "duration_text": "12:34", "jump_url": "//www.bilibili.com/video/BV1dyna00001/"}}}}},
    {"id_str": "2", "type": "DYNAMIC_TYPE_DRAW", "modules": {
        "module_author": {"mid": 1001, "name": "UP 主1", "pub_ts": 1760000400},
        "module_dynamic": {"major": {"type": "MAJOR_TYPE_DRAW", "draw": {}}}}},
    {"id_str": "3", "type": "DYNAMIC_TYPE_AV", "modules": {
        "module_author": {"mid": 1002, "name": "UP 主2", "pub_ts": 1760000100},
        "module_dynamic": {"major": {"type": "MAJOR_TYPE_ARCHIVE", "archive": {
            "bvid": "BV1dyna00002", "title": "老一点的", "duration_text": "1:02:03"}}}}}]}}
VIEW = {"code": 0, "data": {"aid": 424242, "cid": 777, "bvid": "BV1hist00001", "title": "看了一半", "duration": 600}}


class FeedBili(FakeBili):
    """bililogin.request's stand-in with the feed endpoints added; the followings are self.mids."""

    def __init__(self):
        super().__init__()
        self.mids = [1000, 1001, 1002]
        self.rcmd, self.history, self.dynamic = RCMD, HISTORY, DYNAMIC
        self.fail = None
        self.follow_fail = None

    def count(self, part):
        return sum(part in url for url, _, _ in self.calls)

    def __call__(self, url, cookie="", data=None, timeout=None):
        feeds = ("/feed/rcmd", "/history/cursor", "/web-dynamic/", "/history/report", "/web-interface/view")
        if "/relation/followings" in url:
            self.calls.append((url, cookie, data))
            if self.follow_fail is not None:
                raise self.follow_fail
            page = int(url.split("pn=")[1].split("&")[0])
            size = 50
            rows = self.mids[(page - 1) * size:page * size]
            return {"code": 0, "data": {"total": len(self.mids), "list": [
                {"mid": m, "uname": f"UP 主{m - 1000}", "face": f"https://i0.hdslb.com/bfs/face/f{m}.jpg",
                 "sign": "签名"} for m in rows]}}, {}
        if any(part in url for part in feeds):
            self.calls.append((url, cookie, data))
            if self.fail is not None:
                if isinstance(self.fail, Exception):
                    raise self.fail
                return self.fail, {}
            if "/feed/rcmd" in url:
                return self.rcmd, {}
            if "/history/cursor" in url:
                return self.history, {}
            if "/web-dynamic/" in url:
                return self.dynamic, {}
            if "/web-interface/view" in url:
                return VIEW, {}
            return {"code": 0, "message": "0", "ttl": 1}, {}
        return super().__call__(url, cookie, data, timeout)


class FakeYT:
    """The relay's YouTube client (the node in local mode)."""

    def __init__(self):
        self.calls = []
        self.error = None
        self.subs = [{"id": UC1, "name": "频道一", "handle": "@one", "fans": 10},
                     {"id": UC2, "name": "频道二", "handle": "", "fans": 0}]
        self.feeds = {
            "rec": [{"id": "recrecrec01", "title": "YT 推荐", "duration": 100, "channel": "频道一", "channel_id": UC1,
                     "date": 0, "views": 5}],
            "history": [{"id": "hishishis01", "title": "YT 看过", "duration": 200, "channel": "频道二",
                         "channel_id": UC2, "date": 0, "views": 0}],
            "subs": [{"id": "subsubsub01", "title": "YT 新视频", "duration": 300, "channel": "频道一",
                      "channel_id": UC1, "date": 1760000300, "views": 0}]}

    def feed(self, kind, limit=30, timeout=None):
        self.calls.append(("feed", kind))
        if self.error is not None:
            raise self.error
        return list(self.feeds[kind])

    def subscriptions(self, timeout=None):
        self.calls.append(("subscriptions",))
        if self.error is not None:
            raise self.error
        return list(self.subs)

    def creator(self, channel):
        self.calls.append(("creator", channel))
        return "频道", [Video(bvid="ytvideo0001", title="v1", duration=60, pubdate=0, site="youtube")]

    def uploads(self, channel):
        return self.creator(channel)[1]

    def video(self, vid):
        return Video(bvid=vid, title="t" + vid, duration=60, pubdate=0, site="youtube")

    def health(self, timeout=None):
        return {"state": "ok", "at": 0, "version": "2026.10.21"}


# ---------------------------------------------------------------- shaping (pure)

class ShapingTests(unittest.TestCase):
    def test_recommendations_keep_videos_only(self):
        items = accountfeeds.bili_rcmd(RCMD["data"])
        self.assertEqual([e["id"] for e in items], ["BV1rcmdaaaa1", "BV1rcmdaaaa2"])
        first = items[0]
        self.assertEqual((first["title"], first["author"], first["author_id"], first["reason"], first["duration"]),
                         ("推荐的 <一>", "甲", "2001", "已关注", 300))
        self.assertEqual(first["cover"], "https://i0.hdslb.com/bfs/archive/r1.jpg")
        self.assertEqual(items[1]["cover"], "", "only Bilibili's image hosts go through the proxy")

    def test_history_positions_parts_and_bangumi(self):
        items = {e["id"]: e for e in accountfeeds.bili_history(HISTORY["data"])}
        self.assertEqual(list(items), ["BV1hist00001", "BV1hist00002", "BV1hist00003", "ss28", "BV1hist00004"])
        self.assertEqual((items["BV1hist00001"]["position"], items["BV1hist00001"]["done"]), (125, False))
        self.assertEqual((items["BV1hist00002"]["position"], items["BV1hist00002"]["done"]), (0, True))
        self.assertEqual(items["BV1hist00003"]["part"], 3)
        self.assertIn("P3", items["BV1hist00003"]["title"])
        self.assertFalse(items["ss28"]["tv"], "a bangumi season plays on the PC only")
        self.assertIn("第3话", items["ss28"]["title"])

    def test_history_items_resume_where_bilibili_says(self):
        shaped = [accountfeeds.media("bili_history", e) for e in accountfeeds.bili_history(HISTORY["data"])]
        by = {x["play"]["id"]: x for x in shaped}
        self.assertEqual(by["BV1hist00001"]["play"]["start"], 125)
        self.assertEqual(by["BV1hist00001"]["progress"]["position"], 125)
        self.assertNotIn("start", by["BV1hist00002"]["play"], "finished: from the start")
        self.assertTrue(by["BV1hist00002"]["progress"]["done"])
        self.assertNotIn("start", by["BV1hist00004"]["play"], "10 s in is not worth resuming")
        self.assertEqual(by["BV1hist00003"]["play"]["part"], 3)
        self.assertTrue(by["ss28"]["pc_only"])
        self.assertTrue(by["BV1hist00001"]["art"]["url"].startswith("api/bili/thumb?"))

    def test_dynamic_keeps_uploads(self):
        items = accountfeeds.bili_dynamic(DYNAMIC["data"])
        self.assertEqual([(e["id"], e["author_id"], e["duration"], e["date"]) for e in items],
                         [("BV1dyna00001", "1000", 754, 1760000500), ("BV1dyna00002", "1002", 3723, 1760000100)])

    def test_play_urls_are_built_from_ids_only(self):
        self.assertEqual(accountfeeds.play_url({"site": "bilibili", "id": "BV1hist00003", "part": 3}),
                         "https://www.bilibili.com/video/BV1hist00003?p=3")
        self.assertEqual(accountfeeds.play_url({"site": "bilibili", "id": "ss28"}),
                         "https://www.bilibili.com/bangumi/play/ss28")
        self.assertEqual(accountfeeds.play_url({"site": "youtube", "id": "recrecrec01"}),
                         "https://www.youtube.com/watch?v=recrecrec01")
        for bad in ({"site": "bilibili", "id": "https://evil.example/"}, {"site": "youtube", "id": "x" * 12},
                    {"site": "ftp", "id": "BV1hist00003"}, {"site": "bilibili", "id": 5}):
            self.assertIsNone(accountfeeds.play_url(bad), bad)
        self.assertEqual(home._pc_body({"kind": "feed", "site": "bilibili", "id": "BV1hist00001", "start": 125}),
                         {"url": "https://www.bilibili.com/video/BV1hist00001", "start": 125.0})
        self.assertIsNone(home._pc_body({"kind": "feed", "site": "bilibili", "id": "nope"}))


# ---------------------------------------------------------------- the YouTube node (yt-dlp 2026.10.21 keywords)

FEED_LISTING = {"_type": "playlist", "id": "recommended", "title": "recommended", "entries": [
    {"_type": "url", "ie_key": "Youtube", "id": "recrecrec01", "url": "https://www.youtube.com/watch?v=recrecrec01",
     "title": "A video", "duration": 125.0, "channel": "Chan", "channel_id": UC1, "view_count": 12,
     "thumbnails": [{"url": "https://i.ytimg.com/vi/recrecrec01/hqdefault.jpg?sqp=x"}], "timestamp": 1760000000},
    {"_type": "url", "id": "shortshort1", "url": "https://www.youtube.com/shorts/shortshort1", "title": "Short"},
    {"_type": "url", "id": "livelivel01", "title": "Live", "live_status": "is_live"},
    {"_type": "url", "id": "gonegonego1", "title": "[Private video]"},
    {"_type": "url", "id": "nodurnodur1", "title": "No duration", "channel_id": "bad"}]}


class NodeFeedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cookies = Path(self.tmp.name) / "cookies.txt"
        self.cookies.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
        self.runs = []
        self.answer = (0, json.dumps(FEED_LISTING), "")

        def run(args):
            self.runs.append(args)
            return self.answer
        tools = ytnode.Tools(ytdlp="/opt/yt-dlp", deno="/opt/deno", ffmpeg="/usr/bin/ffmpeg", cookies=str(self.cookies))
        self.node = ytnode.Node(tools, "k", run=run, popen=None, log=lambda _m: None)

    def tearDown(self):
        self.tmp.cleanup()

    def test_keywords_and_summary(self):
        code, data = self.node.feed("rec", 10)
        self.assertEqual(code, 200)
        self.assertEqual(self.runs[-1][-2:], ["--", ":ytrec"])
        self.assertIn("--cookies", self.runs[-1])
        self.assertEqual([v["id"] for v in data["videos"]], ["recrecrec01", "nodurnodur1"])
        first = data["videos"][0]
        self.assertEqual((first["duration"], first["channel_id"], first["date"], first["views"]),
                         (125, UC1, 1760000000, 12), "yt-dlp gives the duration as a float")
        self.assertEqual(data["videos"][1]["channel_id"], "")
        self.node.feed("history", 10)
        self.assertEqual(self.runs[-1][-1], ":ythis")
        self.node.feed("subs", 10)
        self.assertEqual(self.runs[-1][-1], ":ytsubs")
        self.assertIn("youtubetab:approximate_date", self.runs[-1])
        self.assertEqual(self.runs[-1][self.runs[-1].index("--playlist-end") + 1], "10")

    def test_login_needed(self):
        import dataclasses
        self.node.tools = dataclasses.replace(self.node.tools, cookies="")
        self.assertEqual(self.node.feed("history", 10), (401, "login: no cookies"))
        self.assertEqual(self.node.feed("subs", 10)[0], 401)
        self.assertEqual(self.node.feed("rec", 10)[0], 200, "recommendations work without cookies (generic)")
        self.node.tools = dataclasses.replace(self.node.tools, cookies=str(self.cookies))
        self.answer = (1, "", "ERROR: [youtube:history] history: The page requires authentication; use --cookies")
        self.assertEqual(self.node.feed("history", 10), (401, "login"))

    def test_client_maps_status_codes(self):
        def fetch(path, params, timeout=None):
            self.assertEqual(path, "/yt/feed")
            raise answers.pop(0)
        from server.relay.youtube import NodeError
        answers = [NodeError(401, "login"), NodeError(404, "Not Found"), NodeError(504, "yt-dlp timed out")]
        client = YouTube(fetch_json=fetch, config={"url": "http://127.0.0.1:9", "key": "k"})
        with self.assertRaises(SourceError) as caught:
            client.feed("history")
        self.assertEqual(caught.exception.reason, "login")
        with self.assertRaises(SourceError) as caught:
            client.feed("rec")
        self.assertEqual(caught.exception.reason, "unsupported")
        with self.assertRaises(SourceError) as caught:
            client.feed("subs")
        self.assertEqual(caught.exception.reason, "slow")
        good = YouTube(fetch_json=lambda p, q, timeout=None: {"videos": [
            {"id": "recrecrec01", "title": " A  b ", "duration": 5, "channel_id": "nope", "date": True}]},
            config={"url": "http://127.0.0.1:9", "key": "k"})
        self.assertEqual(good.feed("rec"), [{"id": "recrecrec01", "title": "A b", "duration": 5, "channel": "",
                                             "channel_id": "", "date": 0, "views": 0}])


# ---------------------------------------------------------------- the relay

class FeedHarness(RelayHarness):
    def setUp(self):
        super().setUp()
        self.fake = FeedBili()
        feeds = self.relay.account_feeds
        if self.relay.bili_accounts is not None:
            self.relay.bili_accounts.fetch = self.fake
        if feeds is None:
            return
        self.feeds = feeds
        feeds.background = False
        feeds.follows.background = False
        feeds.monotonic = self.mono
        feeds.follows.monotonic = self.mono
        self.yt = FakeYT()
        self.relay.clients["youtube"] = self.yt
        feeds.yt_cookies = lambda: True
        feeds.follows.yt_gate = lambda user: "" if feeds.follows._admin(user) else "hidden"

    def relay_caller(self, user):
        from server.relay.tenants import Caller
        return Caller(user=user, role="admin" if user == self.admin else "user")

    def row(self, rid, user, fresh=False):
        return self.request("GET", f"/tv/api/feeds/row?id={rid}" + ("&fresh=1" if fresh else ""), user=user)[1]


class CloudFeedTests(FeedHarness):
    selfhost = False

    def test_everything_is_404_on_the_cloud(self):
        self.assertIsNone(self.relay.account_feeds)
        for path in ("/tv/api/feeds", "/tv/api/feeds/row?id=bili_rec", "/tv/api/follows",
                     "/tv/api/follows/videos?key=bilibili:1000"):
            self.assertEqual(self.request("GET", path, user=self.admin)[0], 404, path)
        for path in ("/tv/api/feeds/settings", "/tv/api/follows/sync", "/tv/api/follows/tv"):
            self.assertEqual(self.request("POST", path, {}, user=self.admin)[0], 404, path)
        self.assertNotIn("feeds", self.request("GET", "/tv/api/state", user=self.admin)[1])


class FeedRowTests(FeedHarness):
    def test_guests_and_strangers_get_404(self):
        self.assertEqual(self.request("GET", "/tv/api/feeds")[0], 404)
        self.accounts.register_device("aabbccddeeff", "xingzhi-cube-154", "s" * 32)
        guest = self.accounts.new_session(None, "guest", device_id="aabbccddeeff")
        self.tokens["guest"] = guest
        self.assertEqual(self.request("GET", "/tv/api/feeds", user="guest")[0], 404)
        self.assertEqual(self.request("GET", "/tv/api/follows", user="guest")[0], 404)
        self.assertTrue(self.request("GET", "/tv/api/state", user=self.kid)[1]["feeds"])

    def test_not_logged_in_shows_a_login_card(self):
        overview = self.request("GET", "/tv/api/feeds", user=self.kid)[1]
        rows = {r["id"]: r for r in overview["rows"]}
        self.assertEqual(rows["bili_rec"]["state"], "login")
        self.assertEqual(rows["bili_rec"]["need"], "bili")
        self.assertEqual(rows["yt_rec"]["state"], "hidden", "YouTube's cookies are the admin's: not for the kid")
        self.assertEqual(overview["settings"], {"writeback": True, "autotv": False}, "sync is on until turned off")
        answer = self.row("bili_rec", self.kid)
        self.assertEqual((answer["state"], answer["items"]), ("login", []))
        self.assertIn("登录 B 站", answer["message"])
        self.assertEqual(self.fake.count("/feed/rcmd"), 0)

    def test_recommendations_signed_with_the_users_cookies_and_cached(self):
        self.sign_in(self.kid)
        answer = self.row("bili_rec", self.kid)
        self.assertEqual(answer["state"], "ready", answer)
        self.assertEqual([x["play"]["id"] for x in answer["items"]], ["BV1rcmdaaaa1", "BV1rcmdaaaa2"])
        x = answer["items"][0]
        self.assertEqual((x["kind"], x["source"], x["subtitle"], x["reason"]), ("video", "bilibili", "甲", "已关注"))
        self.assertEqual(x["play"], {"kind": "feed", "site": "bilibili", "id": "BV1rcmdaaaa1", "title": "推荐的 <一>"})
        url, cookie, _ = [c for c in self.fake.calls if "/feed/rcmd" in c[0]][-1]
        self.assertIn("/x/web-interface/wbi/index/top/feed/rcmd?", url)
        self.assertIn("w_rid=", url)
        self.assertIn("wts=", url)
        self.assertIn("SESSDATA=" + SESSDATA, cookie)
        # cached: the page asking again (and "refresh" too soon) does not reach Bilibili
        self.row("bili_rec", self.kid)
        self.mono.t += 10
        self.row("bili_rec", self.kid, fresh=True)
        self.assertEqual(self.fake.count("/feed/rcmd"), 1)
        self.mono.t += accountfeeds.FRESH_GAP_S
        self.row("bili_rec", self.kid, fresh=True)
        self.assertEqual(self.fake.count("/feed/rcmd"), 2, "refresh after the gap")
        self.mono.t += accountfeeds.TTL_S["bili_rec"] - 1
        self.row("bili_rec", self.kid)
        self.assertEqual(self.fake.count("/feed/rcmd"), 2)
        self.mono.t += 2
        self.row("bili_rec", self.kid)
        self.assertEqual(self.fake.count("/feed/rcmd"), 3, "stale after the TTL")
        self.assert_no_leak()

    def test_history_has_a_shorter_cache(self):
        self.sign_in(self.kid)
        answer = self.row("bili_history", self.kid)
        self.assertEqual(answer["items"][0]["play"]["start"], 125)
        self.assertEqual(answer["items"][0]["progress"]["device"], "B 站")
        url = [c[0] for c in self.fake.calls if "/history/cursor" in c[0]][-1]
        self.assertIn("ps=30", url)
        self.assertNotIn("w_rid", url)
        self.mono.t += accountfeeds.TTL_S["bili_history"] + 1
        self.row("bili_history", self.kid)
        self.assertEqual(self.fake.count("/history/cursor"), 2)

    def test_per_user_isolation(self):
        self.sign_in(self.kid)
        self.assertEqual(len(self.row("bili_rec", self.kid)["items"]), 2)
        self.assertEqual(self.row("bili_rec", self.admin)["state"], "login", "the admin has no Bilibili login")
        self.sign_in(self.admin)
        self.fake.rcmd = {"code": 0, "data": {"item": [dict(RCMD["data"]["item"][0], bvid="BV1adminrec1")]}}
        self.assertEqual([x["play"]["id"] for x in self.row("bili_rec", self.admin)["items"]], ["BV1adminrec1"])
        self.assertEqual([x["play"]["id"] for x in self.row("bili_rec", self.kid)["items"]],
                         ["BV1rcmdaaaa1", "BV1rcmdaaaa2"], "the kid keeps his own")
        self.request("POST", "/tv/api/feeds/settings", {"writeback": False}, self.kid)
        self.assertTrue(self.request("GET", "/tv/api/feeds", user=self.admin)[1]["settings"]["writeback"])
        self.assertFalse(self.request("GET", "/tv/api/feeds", user=self.kid)[1]["settings"]["writeback"])
        path = self.root / "feeds" / f"{self.kid}.json"
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)

    def test_blocked_network_backs_off(self):
        self.sign_in(self.kid)
        self.fake.fail = OSError("[SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation of protocol")
        answer = self.row("bili_rec", self.kid)
        self.assertEqual((answer["state"], answer["message"]), ("blocked", BLOCKED_HINT))
        self.row("bili_rec", self.kid)
        self.mono.t += accountfeeds.ERROR_GAP_S + 1
        self.row("bili_rec", self.kid, fresh=True)
        self.assertEqual(self.fake.count("/feed/rcmd"), 1, "blocked: wait BLOCKED_GAP_S before asking again")
        self.fake.fail = None
        self.mono.t += accountfeeds.BLOCKED_GAP_S
        self.assertEqual(self.row("bili_rec", self.kid)["state"], "ready")

    def test_rejected_cookies_mark_the_login_invalid(self):
        self.sign_in(self.kid)
        self.fake.fail = {"code": -101, "message": "账号未登录"}
        self.fake.nav = NAV_OUT
        answer = self.row("bili_history", self.kid)
        self.assertEqual(answer["state"], "login")
        self.assertIn("重新扫码", answer["message"])
        self.assertTrue(self.relay.bili_accounts.record(self.kid)["invalid"])
        self.assertEqual(self.row("bili_rec", self.kid)["state"], "login")

    def test_youtube_rows_for_the_admin_only(self):
        self.assertEqual(self.row("yt_rec", self.kid)["state"], "hidden")
        answer = self.row("yt_history", self.admin)
        self.assertEqual(answer["state"], "ready", answer)
        x = answer["items"][0]
        self.assertEqual((x["play"]["site"], x["play"]["id"], x["source"]), ("youtube", "hishishis01", "youtube"))
        self.assertTrue(x["art"]["url"].startswith("api/bili/thumb?"))
        self.assertEqual(self.yt.calls, [("feed", "history")])
        self.row("yt_history", self.admin)
        self.assertEqual(len(self.yt.calls), 1, "cached")
        self.feeds.yt_cookies = lambda: False
        answer = self.row("yt_rec", self.admin)
        self.assertEqual((answer["state"], answer["need"]), ("login", "youtube"))
        self.feeds.yt_cookies = lambda: True
        self.yt.error = SourceError("youtube /yt/feed: 401 login", reason="login")
        self.assertEqual(self.row("yt_rec", self.admin)["state"], "login")
        self.yt.error = SourceError("youtube /yt/feed: 404 Not Found", permanent=True, reason="unsupported")
        answer = self.row("yt_history", self.admin, fresh=False)
        self.assertEqual(answer["state"], "ready", "the cached history stays while the node is old")

    def test_youtube_loads_in_the_background(self):
        self.feeds.background = True
        import threading
        gate = threading.Event()
        feed = self.yt.feed
        self.yt.feed = lambda kind, n=30: (gate.wait(5), feed(kind, n))[1]
        first = self.row("yt_rec", self.admin)
        self.assertEqual((first["state"], first["loading"], first["items"]), ("loading", True, []),
                         "the page is answered at once while the node works")
        gate.set()
        for _ in range(100):
            answer = self.row("yt_rec", self.admin)
            if answer["state"] == "ready":
                break
            import time
            time.sleep(0.02)
        self.assertEqual(answer["state"], "ready")
        self.assertEqual(answer["items"][0]["play"]["id"], "recrecrec01")

    def test_updates_merge_both_sites_by_time_and_note_latest(self):
        self.sign_in(self.admin)
        answer = self.row("updates", self.admin)
        self.assertEqual([x["play"]["id"] for x in answer["items"]], ["BV1dyna00001", "subsubsub01", "BV1dyna00002"])
        self.assertIn("w_rid=", [c[0] for c in self.fake.calls if "/web-dynamic/" in c[0]][-1])
        self.assertIn("type=video", [c[0] for c in self.fake.calls if "/web-dynamic/" in c[0]][-1])
        listing = self.request("GET", "/tv/api/follows", user=self.admin)[1]
        latest = {i["key"]: i["latest"] for i in listing["items"]}
        self.assertEqual(latest["bilibili:1000"]["title"], "新视频")
        self.assertEqual(latest[f"youtube:{UC1}"]["id"], "subsubsub01")
        # the kid: Bilibili only (YouTube is the admin's)
        self.sign_in(self.kid)
        self.assertEqual([x["source"] for x in self.row("updates", self.kid)["items"]], ["bilibili", "bilibili"])

    def test_play_feed_items_to_the_pc_and_the_tv(self):
        self.sign_in(self.kid)
        seen = []
        with mock.patch.object(self.relay, "watch_direct", side_effect=lambda c, d, b: seen.append(b) or {"ok": True}):
            answer = self.request("POST", "/tv/api/play", {"target": "pc", "viewer": "abc", "item": {
                "kind": "feed", "site": "bilibili", "id": "BV1hist00001", "start": 125}}, self.kid)[1]
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(seen[-1]["url"], "https://www.bilibili.com/video/BV1hist00001")
        self.assertEqual(seen[-1]["start"], 125.0)
        dev = {"id": "aabbccddeeff", "name": "客厅", "owner": self.kid}
        casts = []
        with mock.patch.object(self.relay, "_cast_video",
                               side_effect=lambda v, c, d, position=0.0: casts.append((v, position)) or {"ok": True}):
            answer = accountfeeds.play_tv(self.relay, self.relay_caller(self.kid), dev,
                                          {"kind": "feed", "site": "bilibili", "id": "BV1hist00001", "start": 125})
            self.assertTrue(answer["ok"])
            self.assertEqual((casts[-1][0].bvid, casts[-1][1]), ("BV1hist00001", 125.0))
            answer = accountfeeds.play_tv(self.relay, self.relay_caller(self.kid), dev,
                                          {"kind": "feed", "site": "bilibili", "id": "ss28"})
            self.assertFalse(answer["ok"])
            self.assertIn("剧集", answer["error"])
            answer = accountfeeds.play_tv(self.relay, self.relay_caller(self.kid), dev,
                                          {"kind": "feed", "site": "youtube", "id": "recrecrec01"})
            self.assertEqual(casts[-1][0].site, "youtube")

class FollowTests(FeedHarness):
    def listing(self, user):
        return self.request("GET", "/tv/api/follows", user=user)[1]

    def test_login_syncs_unlimited_followings(self):
        self.fake.mids = list(range(1000, 1120))
        self.sign_in(self.kid)
        listed = self.listing(self.kid)
        self.assertEqual(len(listed["items"]), 120, "no 12-creator cap on the list itself")
        self.assertEqual(listed["sites"]["bilibili"]["state"], "ok")
        self.assertEqual(listed["sites"]["youtube"]["state"], "hidden")
        self.assertEqual(listed["tv"]["max"], MAX_CREATORS)
        self.assertEqual(self.fake.count("/relation/followings"), 3, "pages of 50 until done")
        self.assertTrue(all(i["tv"] is None for i in listed["items"]))

    def test_new_follows_appear_unfollows_disappear_and_tv_keeps_its_channel(self):
        self.sign_in(self.kid)
        answer = self.request("POST", "/tv/api/follows/tv", {"key": "bilibili:1001", "on": True}, self.kid)[1]
        self.assertTrue(answer["ok"], answer)
        cid = answer["tv"]
        self.assertEqual(self.relay.user_state(self.kid).get(cid).source, "1001")
        self.fake.mids = [1000, 1002, 1003]
        result = self.feeds.follows.sync(self.kid)
        self.assertEqual((result["added"], result["removed"], result["unfollowed"]),
                         (["bilibili:1003"], ["bilibili:1001"], ["bilibili:1001"]))
        listed = {i["key"]: i for i in self.listing(self.kid)["items"]}
        self.assertEqual(set(listed), {"bilibili:1000", "bilibili:1002", "bilibili:1003", "bilibili:1001"})
        self.assertTrue(listed["bilibili:1001"]["unfollowed"])
        self.assertEqual(listed["bilibili:1001"]["tv"], cid, "not silently removed from the TV")
        self.assertIsNotNone(self.relay.user_state(self.kid).get(cid))
        self.assertEqual(listed["bilibili:1003"]["tv"], None, "auto-TV is off by default")
        # taking it off the TV ends the mark
        self.request("POST", "/tv/api/follows/tv", {"key": "bilibili:1001", "on": False}, self.kid)
        self.assertIsNone(self.relay.user_state(self.kid).get(cid))
        self.assertNotIn("bilibili:1001", {i["key"] for i in self.listing(self.kid)["items"]})
        # following again ends a mark too
        self.request("POST", "/tv/api/follows/tv", {"key": "bilibili:1002", "on": True}, self.kid)
        self.fake.mids = [1000, 1003]
        self.feeds.follows.sync(self.kid)
        self.fake.mids = [1000, 1002, 1003]
        self.feeds.follows.sync(self.kid)
        listed = {i["key"]: i for i in self.listing(self.kid)["items"]}
        self.assertFalse(listed["bilibili:1002"]["unfollowed"])

    def test_a_failed_fetch_or_a_logout_is_not_an_unfollow(self):
        self.sign_in(self.kid)
        self.request("POST", "/tv/api/follows/tv", {"key": "bilibili:1000", "on": True}, self.kid)
        self.fake.follow_fail = OSError("down")
        self.feeds.follows.sync(self.kid)
        listed = self.listing(self.kid)
        self.assertEqual(len(listed["items"]), 3, "kept the last list")
        self.assertEqual(listed["sites"]["bilibili"]["state"], "error")
        self.fake.follow_fail = None
        self.request("POST", "/tv/api/bili/logout", {}, self.kid)
        listed = self.listing(self.kid)
        self.assertEqual(listed["items"], [], "logged out: the Bilibili list is not shown")
        self.assertEqual(listed["sites"]["bilibili"]["state"], "off")
        self.assertIn("message", listed)
        self.assertEqual(len([c for c in self.relay.user_state(self.kid).creators if c.kind == "up"]), 1,
                         "the TV channel stays")

    def test_tv_toggle_respects_the_cap_and_the_budget(self):
        self.sign_in(self.kid)
        state = self.relay.user_state(self.kid)
        for n in range(MAX_CREATORS):
            state.add("bilibili", str(5000 + n), f"手动{n}", [Video(bvid="BVx", title="x", duration=1, pubdate=0)], 0)
        state.save()
        answer = self.request("POST", "/tv/api/follows/tv", {"key": "bilibili:1000", "on": True}, self.kid)[1]
        self.assertFalse(answer["ok"])
        self.assertIn(f"最多 {MAX_CREATORS} 个", answer["error"])
        self.assertIn("内存小", answer["error"])
        state.remove("up01")
        state.save()
        with mock.patch.object(self.relay, "room_of", return_value={"fits": False, "bytes": 0, "channels": 0}):
            answer = self.request("POST", "/tv/api/follows/tv", {"key": "bilibili:1000", "on": True}, self.kid)[1]
        self.assertFalse(answer["ok"])
        self.assertIn("放不下", answer["error"])
        answer = self.request("POST", "/tv/api/follows/tv", {"key": "bilibili:1000", "on": True}, self.kid)[1]
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(sum(c.kind == "up" for c in state.creators), MAX_CREATORS)
        for bad in ({"key": "bilibili:abc", "on": True}, {"key": "bilibili:1000", "on": "yes"},
                    {"key": "bilibili:424242", "on": True}):
            self.assertFalse(self.request("POST", "/tv/api/follows/tv", bad, self.kid)[1]["ok"], bad)
        self.assertEqual(self.request("POST", "/tv/api/follows/tv", {"key": "bilibili:1001", "on": True},
                                      self.admin)[1]["ok"], False, "not in the admin's follows")

    def test_auto_tv_for_new_follows_until_full(self):
        self.sign_in(self.kid)          # first sync: never auto
        self.assertEqual(sum(c.kind == "up" for c in self.relay.user_state(self.kid).creators), 0)
        self.request("POST", "/tv/api/feeds/settings", {"autotv": True}, self.kid)
        state = self.relay.user_state(self.kid)
        for n in range(MAX_CREATORS - 1):
            state.add("bilibili", str(5000 + n), f"手动{n}", [Video(bvid="BVx", title="x", duration=1, pubdate=0)], 0)
        state.save()
        self.fake.mids = [1000, 1001, 1002, 1003, 1004]
        result = self.feeds.follows.sync(self.kid)
        self.assertEqual(result["auto_tv"], ["bilibili:1003"], "one slot left: the first new follow takes it")
        self.assertEqual(sum(c.kind == "up" for c in state.creators), MAX_CREATORS)

    def test_youtube_subscriptions_join_the_admins_list(self):
        self.sign_in(self.admin)
        listed = self.listing(self.admin)
        sites = {i["key"]: i["site"] for i in listed["items"]}
        self.assertEqual(sites[f"youtube:{UC1}"], "youtube")
        self.assertEqual(len(sites), 5)
        answer = self.request("POST", "/tv/api/follows/tv", {"key": f"youtube:{UC1}", "on": True}, self.admin)[1]
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(self.relay.user_state(self.admin).get(answer["tv"]).source,
                         f"https://www.youtube.com/channel/{UC1}")
        # an @handle channel added by hand matches the subscription
        state = self.relay.user_state(self.admin)
        state.add("youtube", "https://www.youtube.com/@one2", "手动", [Video(bvid="ytvideo0001", title="v", duration=1,
                                                                          pubdate=0, site="youtube")], 0)
        self.assertEqual(follows.tv_map(state, {f"youtube:{UC2}": {"site": "youtube", "handle": "@ONE2"}})
                         [f"youtube:{UC2}"], "up02")

    def test_sync_now_is_rate_limited_and_tick_runs_when_due(self):
        self.sign_in(self.kid)
        calls = self.fake.count("/relation/followings")
        answer = self.request("POST", "/tv/api/follows/sync", {}, self.kid)[1]
        self.assertIn("note", answer)
        self.assertEqual(self.fake.count("/relation/followings"), calls)
        self.mono.t += follows.SYNC_GAP_S
        self.request("POST", "/tv/api/follows/sync", {}, self.kid)
        self.assertEqual(self.fake.count("/relation/followings"), calls + 1)
        self.feeds.follows.tick()
        self.assertEqual(self.fake.count("/relation/followings"), calls + 1, "not due yet")
        now = self.relay.clock() + follows.SYNC_EVERY_S + 1
        self.feeds.follows.clock = lambda: now
        self.mono.t += follows.TICK_GAP_S
        self.feeds.follows.tick()
        self.assertEqual(self.fake.count("/relation/followings"), calls + 2, "six hours later: synced again")
        self.assertFalse(self.feeds.follows.due(self.admin), "no account at all: nothing to sync")

    def test_browse_a_creators_videos(self):
        self.sign_in(self.kid)
        answer = self.request("GET", "/tv/api/follows/videos?key=bilibili:1000", user=self.kid)[1]
        self.assertTrue(answer["ok"], answer)
        self.assertEqual([x["play"]["id"] for x in answer["items"]], ["new", "a", "b"])
        self.assertEqual(answer["items"][0]["play"]["kind"], "feed")
        self.assertFalse(self.request("GET", "/tv/api/follows/videos?key=bilibili:999", user=self.kid)[1]["ok"])


class WriteBackTests(FeedHarness):
    def reports(self):
        return [c for c in self.fake.calls if "/history/report" in c[0]]

    def test_on_by_default_and_off_once_turned_off(self):
        self.sign_in(self.kid)
        self.assertTrue(self.feeds.report(self.kid, "bilibili", "BV1hist00001", 300, final=True))
        self.assertEqual(len(self.reports()), 1)
        self.request("POST", "/tv/api/feeds/settings", {"writeback": False}, self.kid)
        self.assertFalse(self.feeds.report(self.kid, "bilibili", "BV1hist00001", 400, final=True))
        dev = {"id": "d1", "owner": self.kid}
        self.relay._report_history(dev, Video(bvid="BV1hist00001", title="t", duration=600, pubdate=0), 100,
                                   "client_gone", 20)
        self.assertEqual(len(self.reports()), 1)
        # changing another setting later does not turn it back on
        self.request("POST", "/tv/api/feeds/settings", {"autotv": True}, self.kid)
        self.assertFalse(self.feeds.store.settings(self.kid)["writeback"])

    def test_a_writeback_false_saved_only_as_the_old_default_is_not_a_choice(self):
        # before the default changed, turning on "auto TV" saved writeback: false with it
        path = self.root / "feeds" / f"{self.kid}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"user": self.kid, "settings": {"writeback": False, "autotv": True},
                                    "follows": {}}), encoding="utf-8")
        self.assertEqual(self.feeds.store.settings(self.kid), {"writeback": True, "autotv": True})

    def test_payload_when_on_and_throttled(self):
        self.sign_in(self.kid)
        self.request("POST", "/tv/api/feeds/settings", {"writeback": True}, self.kid)
        dev = {"id": "d1", "owner": self.kid}
        self.relay._report_history(dev, Video(bvid="BV1hist00001", title="t", duration=600, pubdate=0), 100,
                                   "client_gone", 20)
        url, cookie, data = self.reports()[-1]
        self.assertEqual(url, "https://api.bilibili.com/x/v2/history/report")
        self.assertEqual(data, {"aid": 424242, "cid": 777, "progress": 120, "platform": "web", "csrf": JCT})
        self.assertIn("SESSDATA=" + SESSDATA, cookie)
        # the PC reports every ~10 s: at most one per REPORT_GAP_S, except the final one
        self.assertFalse(self.feeds.report(self.kid, "bilibili", "BV1hist00001", 130))
        self.mono.t += accountfeeds.REPORT_GAP_S
        self.assertTrue(self.feeds.report(self.kid, "bilibili", "BV1hist00001", 190))
        self.assertTrue(self.feeds.report(self.kid, "bilibili", "BV1hist00001", 200, final=True))
        self.assertEqual(self.fake.count("/web-interface/view"), 1, "aid and cid are looked up once")
        # a part (BV/cid) reports that cid; a whole season and YouTube are never reported
        self.feeds.report(self.kid, "bilibili", "BV1hist00003/53", 50, final=True)
        self.assertEqual(self.reports()[-1][2]["cid"], 53)
        before = len(self.reports())
        self.assertFalse(self.feeds.report(self.kid, "bilibili", "ss28", 50, final=True))
        self.assertFalse(self.feeds.report(self.kid, "youtube", "recrecrec01", 50, final=True))
        self.assertFalse(self.feeds.report(self.admin, "bilibili", "BV1hist00001", 50, final=True),
                         "the admin has no Bilibili login")
        self.assertEqual(len(self.reports()), before)
        self.assert_no_leak()

    def test_pc_progress_reports_for_the_viewer(self):
        self.sign_in(self.kid)
        self.request("POST", "/tv/api/feeds/settings", {"writeback": True}, self.kid)
        caller = self.relay_caller(self.kid)
        cursor = {"bvid": "BV1hist00001", "site": "bilibili", "position": 333.0}
        with mock.patch.object(self.relay, "_pc_page", return_value=("v:bilibili:BV1hist00001", cursor)):
            self.relay._pc_history(caller, {"viewer": "abc"}, final=True)
        self.assertEqual(self.reports()[-1][2]["progress"], 333)


if __name__ == "__main__":
    unittest.main()
