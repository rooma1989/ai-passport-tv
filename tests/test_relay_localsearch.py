"""The console's aggregate search (server/relay/localsearch.py, GET /api/search/local).

What is checked:
- Matching: channel names are compared the way the guide matches channels (cctv5, CCTV-5 and
  "CCTV5 体育" are the same channel), then by prefix and substring after dropping case, spaces and
  punctuation; M3U aliases (tvg-name) count; the exact channel ranks above "CCTV5+" and "CCTV5 4K".
- Guide titles from now to 24 hours ahead are found, on-air first, with channel and times; ones that
  already ended are not.
- Library titles: the parsed title, the English name, and the Douban title / original title / aka of a
  matched entry; a show is one result (all its episodes), opened by its show id; a movie by item id.
- The caller's own subscriptions (series, creators) by name.
- Sections are ordered by their best match ("新闻联播" puts the guide first, "CCTV5" the channels).
- Visibility over real sockets: signed out is a 404; a guest gets only live channels and the guide;
  each member sees only their own subscriptions.
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import types
import unittest
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from server.relay import epg, library, localsearch
from server.relay.tenants import Caller, NotFound

CHANNELS = """ch000 | CCTV1 综合 | https://x/1.m3u8
ch001 | CCTV5+ 体育赛事 | https://x/2.m3u8
ch002 | CCTV5 体育 | https://x/3.m3u8
ch003 | 湖南卫视 | https://x/4.m3u8
ch004 | 浙江卫视 | https://x/5.m3u8
ch005 | 五星体育 | https://x/6.m3u8
ch006 | CCTV5 4K | https://x/7.m3u8
"""


def write_table(root: Path) -> Path:
    table = root / "channels.txt"
    table.write_text(CHANNELS, encoding="utf-8")
    (root / "channels.txt.meta.json").write_text(json.dumps({"ch005": {"tvg_name": "SiTV 五星体育"}}), encoding="utf-8")
    return table


def guide_for(root: Path, table: Path, now: int) -> epg.Guide:
    guide = epg.Guide(root / "epg", table, log=lambda _m: None, clock=lambda: now)
    guide.sources = ["https://epg.example/e.xml"]           # on, without a thread or a download
    guide._data = {
        "ch000": [(now - 1800, now + 1800, "新闻联播"), (now + 3600, now + 7200, "焦点访谈"),
                  (now + 86400 - 600, now + 86400 + 600, "新闻联播")],
        "ch002": [(now - 7200, now - 3600, "体育新闻"), (now - 600, now + 600, "NBA 常规赛"),
                  (now + 600, now + 4200, "体育新闻")],
        "ch003": [(now + 7200, now + 9000, "快乐大本营")],
    }
    return guide


class Account:
    """The parts of web.State the search reads: creators under a lock, and the next episode of a series."""

    def __init__(self, creators):
        self.lock = threading.Lock()
        self.creators = creators

    def episode(self, cid):
        return 3 if cid == "sr01" else 0


def creator(cid, name, kind, n=5, site="bilibili"):
    return types.SimpleNamespace(id=cid, name=name, kind=kind, site=site, source="42", videos=[object()] * n)


class FakeRelay:
    def __init__(self, root: Path, now: int, account=None, lib=None):
        self.tenant = True
        self.main_channels = write_table(root)
        self.guide = guide_for(root, self.main_channels, now)
        self.library = lib
        self.logos = self.linecheck = None
        self.clients = {}
        self._now = now
        self.account = account

    def clock(self):
        return self._now

    def _account(self, caller):
        if caller.guest or caller.user is None or self.account is None:
            raise NotFound("account")
        return self.account


def library_with_titles(root: Path) -> library.Library:
    lib = library.Library(root / "lib", log=lambda _m: None, background=False)

    def item(iid, rel, **fields):
        row = {"id": iid, "source": "aaaaaaaa", "path": rel, "title": "", "year": None, "kind": "video", "show": "",
               "season": None, "episode": None, "alt": "", "plot": "", "size": 1, "mtime": 1, "ext": ".mp4",
               "playable": True, "poster": "", "subs": [], "pv": library.PARSE_VERSION, "added": 100}
        row.update(fields)
        return row
    rows = [item("0000000000000001", "电影/流浪地球 (2019).mp4", kind="movie", title="流浪地球", year=2019),
            item("0000000000000002", "Narco.Saints.2022.mkv", kind="movie", title="Narco Saints", alt="Narco Saints"),
            item("0000000000000003", "庆余年/第1集.mp4", kind="episode", show="庆余年", episode=1, title="第 1 集"),
            item("0000000000000004", "庆余年/第2集.mp4", kind="episode", show="庆余年", episode=2, title="第 2 集"),
            item("0000000000000005", "家庭/2024 春节 聚会.mp4", kind="video", title="2024 春节 聚会")]
    lib._items = {row["id"]: row for row in rows}
    lib.meta.put("m0000000000000002", {"state": "matched", "title": "苏里南", "original": "수리남",
                                        "aka": ["毒枭圣徒", "Narco-Saints"], "year": 2022})
    return lib


class MatchTests(unittest.TestCase):
    def test_normalised_text(self):
        self.assertEqual(localsearch.norm("CCTV-5 体育"), "cctv5体育")
        self.assertEqual(localsearch.norm("ＣＣＴＶ５"), "cctv5")
        self.assertEqual(localsearch.clean_query("  湖南   卫视 "), "湖南 卫视")
        self.assertEqual(len(localsearch.clean_query("x" * 100)), localsearch.QUERY_MAX)

    def test_scores(self):
        q = localsearch.Query("cctv5")
        self.assertEqual(q.score(["CCTV5 体育"], channel=True), localsearch.EXACT)
        self.assertEqual(q.score(["CCTV-5"], channel=True), localsearch.EXACT)
        self.assertEqual(q.score(["CCTV5+ 体育赛事"], channel=True), localsearch.PREFIX)
        self.assertIsNone(q.score(["湖南卫视"], channel=True))
        self.assertEqual(localsearch.Query("卫视").score(["湖南卫视"]), localsearch.INSIDE)
        self.assertEqual(localsearch.Query("流浪 地球").score(["流浪地球 2"]), localsearch.PREFIX)
        self.assertEqual(localsearch.Query("地球 流浪").score(["流浪地球"]), localsearch.WORDS)
        self.assertFalse(localsearch.Query(" -- "))


class SearchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.now = int(time.time())
        self.account = Account([creator("up01", "老番茄", "up", 20), creator("sr01", "庆余年 第二季", "series", 36),
                                creator("up02", "Linus Tech Tips", "up", 20, site="youtube")])
        self.relay = FakeRelay(self.root, self.now, self.account, library_with_titles(self.root))
        self.member = Caller(user=1, role="user")

    def tearDown(self):
        self.tmp.cleanup()

    def sections(self, q, caller=None):
        answer = localsearch.search(self.relay, caller or self.member, q)
        self.assertTrue(answer["ok"])
        return {s["id"]: s["items"] for s in answer["sections"]}, [s["id"] for s in answer["sections"]]

    def test_channels_by_name_alias_and_fuzzy(self):
        found, order = self.sections("cctv5")
        self.assertEqual(order[0], "live")
        self.assertEqual([x["channel"]["id"] for x in found["live"]], ["ch002", "ch001", "ch006"],
                         "the plain CCTV5 first, then CCTV5+ and the 4K one in table order")
        first = found["live"][0]
        self.assertEqual(first["play"], {"kind": "live", "channel": "ch002"})
        self.assertEqual(first["now"]["title"], "NBA 常规赛")
        self.assertEqual(first["next"]["title"], "体育新闻")
        found, _ = self.sections("卫视")
        self.assertEqual([x["title"] for x in found["live"]], ["湖南卫视", "浙江卫视"])
        found, _ = self.sections("sitv")
        self.assertEqual([x["channel"]["id"] for x in found["live"]], ["ch005"], "the M3U tvg-name counts")
        found, _ = self.sections("CCTV-1")
        self.assertEqual(found["live"][0]["channel"]["id"], "ch000")

    def test_guide_titles_now_and_next_day(self):
        found, order = self.sections("新闻联播")
        self.assertEqual(order[0], "epg", "an exact programme title puts the guide first")
        rows = found["epg"]
        self.assertEqual([(x["channel"]["id"], x["on_air"]) for x in rows], [("ch000", True), ("ch000", False)])
        self.assertEqual(rows[0]["play"], {"kind": "live", "channel": "ch000"})
        self.assertEqual(rows[0]["channel"]["name"], "CCTV1 综合")
        self.assertEqual((rows[1]["start"], rows[1]["end"]), (self.now + 86400 - 600, self.now + 86400 + 600))
        found, _ = self.sections("体育新闻")
        self.assertEqual([x["start"] for x in found["epg"]], [self.now + 600], "the one that already ended is gone")
        found, order = self.sections("体育")
        self.assertEqual(order[:2], ["epg", "live"], "a title starting with the word beats names that only contain it")
        self.relay.guide.sources = []
        found, _ = self.sections("新闻联播")
        self.assertNotIn("epg", found, "no guide, no programme section")

    def test_library_titles_douban_and_shows(self):
        found, _ = self.sections("毒枭圣徒")
        self.assertEqual(found["library"][0]["open"], {"item": "0000000000000002"})
        self.assertEqual(found["library"][0]["title"], "苏里南", "the Douban title is what is shown")
        for q in ("苏里南", "수리남", "narco saints"):
            self.assertEqual(self.sections(q)[0]["library"][0]["open"], {"item": "0000000000000002"}, q)
        found, _ = self.sections("庆余年")
        shows = [x for x in found["library"] if x["kind"] == "show"]
        self.assertEqual(len(shows), 1, "a show is one result, not one per episode")
        self.assertEqual(shows[0]["count"], 2)
        self.assertEqual(shows[0]["open"], {"show": self.relay.library.show_key("庆余年")})
        self.assertEqual(found["series"][0]["series"], {"id": "sr01"}, "the subscribed series of the same name too")
        found, _ = self.sections("流浪地球")
        self.assertEqual((found["library"][0]["kind"], found["library"][0]["year"]), ("movie", 2019))

    def test_subscriptions(self):
        found, order = self.sections("老番茄")
        self.assertEqual(order, ["up"])
        self.assertEqual(found["up"][0]["play"], {"kind": "channel", "channel": "up01"})
        found, _ = self.sections("linus")
        self.assertEqual(found["up"][0]["site"], "youtube")
        found, _ = self.sections("庆余年")
        series = found["series"][0]
        self.assertEqual((series["episode"], series["total"]), (3, 36))
        self.assertEqual(series["play"], {"kind": "episode", "channel": "sr01", "episode": 3})

    def test_guest_and_empty(self):
        guest = Caller(role="guest", device="aaaaaaaaaaa1")
        found, _ = self.sections("庆余年", guest)
        self.assertEqual(found, {}, "a guest sees neither the library nor anyone's subscriptions")
        found, _ = self.sections("cctv5", guest)
        self.assertIn("live", found)
        self.assertEqual(self.sections("   ")[1], [])
        self.relay.library = None
        found, _ = self.sections("流浪地球")
        self.assertNotIn("library", found, "no library on this relay (the cloud one)")


class EndpointTests(unittest.TestCase):
    """Over real sockets: who may ask, and that subscriptions are per person (the relay of test_relay_home)."""

    def setUp(self):
        from test_relay_home import HomeTests
        self.home = HomeTests("test_targets")
        self.home.setUp()

    def tearDown(self):
        self.home.tearDown()

    def test_visibility(self):
        h = self.home
        status, _, _ = h.request("GET", "/tv/api/search/local?q=cctv")
        self.assertEqual(status, 404, "signed out")
        h.relay.add_series("https://www.bilibili.com/bangumi/play/ss1", "", Caller(user=h.alice, role="user"))
        mine = h.call("GET", "/api/state")[1]["series"][0]["name"]
        q = urllib.parse.quote(mine[:2])
        status, answer = h.call("GET", "/api/search/local?q=" + q)
        self.assertEqual(status, 200)
        self.assertIn("series", [s["id"] for s in answer["sections"]])
        bob = h.call("GET", "/api/search/local?q=" + q, "bob")[1]
        self.assertNotIn("series", [s["id"] for s in bob["sections"]], "Alice's series are hers")
        guest = h.call("GET", "/api/search/local?q=" + q, "guest")[1]
        self.assertNotIn("series", [s["id"] for s in guest["sections"]])
        live = h.call("GET", "/api/search/local?q=%E6%B9%96%E5%8D%97", "guest")[1]
        self.assertEqual([s["id"] for s in live["sections"]], ["live"])
        self.assertEqual(live["sections"][0]["items"][0]["play"], {"kind": "live", "channel": "ch001"})


if __name__ == "__main__":
    unittest.main()
