"""Library → History and Bilibili sync (server/relay/accountfeeds.py, bilisearch.py).

Bilibili is never contacted. The first history page is tests/fixtures/bilibili/history_pgc.json (the shape of
/x/web-interface/history/cursor: archive and pgc items with history.epid, kid, part, progress, duration,
view_at, show_title); the second page, the season answer (/pgc/view/web/season?ep_id=) and the heartbeat
answer are written here in the shapes bilibili-API-collect documents. The cookie sentinels of
test_relay_bililogin must never appear in an answer or a log line.
"""

from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from server.relay import accountfeeds
from server.relay.state import Video

from test_relay_accountfeeds import FeedBili, FeedHarness
from test_relay_bililogin import JCT, SESSDATA

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "bilibili"
PAGE1 = json.loads((FIXTURES / "history_pgc.json").read_text(encoding="utf-8"))
PAGE1["data"]["cursor"] = {"max": 1, "view_at": 1761300000, "business": "archive", "ps": 30}
PAGE2 = {"code": 0, "data": {"cursor": {"max": 0, "view_at": 0, "business": "", "ps": 30}, "list": [
    {"title": "间谍过家家", "long_title": "秘密任务", "show_title": "第5话",
     "cover": "http://i0.hdslb.com/bfs/bangumi/spy.jpg",
     "history": {"oid": 600000005, "epid": 500005, "bvid": "BV1spy000005", "page": 5, "cid": 600005, "part": "5",
                 "business": "pgc", "dt": 2},
     "badge": "番剧", "kid": 42000, "view_at": 1761200000, "progress": 600, "duration": 1420},
    {"title": "多集视频", "cover": "http://i0.hdslb.com/bfs/archive/h3.jpg", "videos": 5,
     "history": {"oid": 3, "bvid": "BV1hist00003", "page": 3, "cid": 53, "part": "第三部分", "business": "archive"},
     "author_name": "戊", "view_at": 1761100000, "progress": 400, "duration": 900}]}}
SEASON = {"code": 0, "message": "success", "result": {
    "season_id": 41282, "type": 5, "title": "Doctor X 第七季",
    "episodes": [{"id": 472181, "aid": 506000001, "cid": 511009601, "title": "1"},
                 {"id": 472183, "aid": 506000003, "cid": 511009626, "title": "3", "long_title": ""}],
    "section": [{"episodes": [{"id": 472190, "aid": 506000090, "cid": 511009690, "title": "花絮"}]}]}}


class PagedBili(FeedBili):
    """FeedBili with a second history page (asked with the first page's cursor), the season answer and the
    playback heartbeat."""

    def __init__(self):
        super().__init__()
        self.history = PAGE1

    def __call__(self, url, cookie="", data=None, timeout=None):
        if "/history/cursor" in url and "max=" in url:
            self.calls.append((url, cookie, data))
            return PAGE2, {}
        if "/pgc/view/web/season" in url or "/click-interface/web/heartbeat" in url:
            self.calls.append((url, cookie, data))
            return (SEASON if "/season" in url else {"code": 0, "message": "0", "ttl": 1}), {}
        return super().__call__(url, cookie, data, timeout)


class ShapingTests(unittest.TestCase):
    def test_episode_labels(self):
        entries = {e["id"]: e for e in accountfeeds.bili_history(PAGE2["data"])}
        spy = entries["ss42000"]
        self.assertEqual((spy["head"], spy["label"], spy["n"]), ("间谍过家家", "第5话 秘密任务", "5"))
        self.assertEqual(spy["title"], "间谍过家家 第5话 秘密任务")
        self.assertEqual((entries["BV1hist00003"]["head"], entries["BV1hist00003"]["label"]), ("多集视频", "P3 第三部分"))
        # no show_title: built from the episode number; a film's 「正片」 is kept as it is
        self.assertEqual(accountfeeds.episode_label({"history": {"part": "7"}}), ("第7集", "7"))
        self.assertEqual(accountfeeds.episode_label({"show_title": "正片"}), ("正片", "正片"))

    def test_page_items_carry_the_two_lines_and_resume(self):
        items = {x["play"]["id"]: x for x in
                 (accountfeeds.media("bili_history", e) for e in accountfeeds.bili_history(PAGE1["data"]))}
        drama = items["ss41282"]
        self.assertEqual(drama["hist"], {"type": "pgc", "head": "Doctor X 第七季", "ep": "第3集", "badge": "电视剧"})
        self.assertEqual((drama["play"]["ep"], drama["play"]["start"]), (472183, 1234))
        self.assertEqual(drama["progress"]["position"], 1234)
        self.assertTrue(items["ss31126"]["progress"]["done"])
        self.assertEqual(items["BV1hist00001"]["hist"]["type"], "video")

    def test_cursor(self):
        self.assertEqual(accountfeeds.history_cursor(PAGE1["data"]),
                         {"max": 1, "view_at": 1761300000, "business": "archive"})
        self.assertIsNone(accountfeeds.history_cursor(PAGE2["data"]), "view_at 0: nothing more")
        self.assertIsNone(accountfeeds.history_cursor({"cursor": PAGE1["data"]["cursor"], "list": []}))
        odd = {"cursor": {"max": 5, "view_at": 9, "business": "x y"}, "list": [{}]}
        self.assertEqual(accountfeeds.history_cursor(odd)["business"], "")

    def test_watched_seasons_and_the_search_copy(self):
        seasons = accountfeeds.watched_seasons(accountfeeds.bili_history(PAGE1["data"]))
        self.assertEqual(seasons, {"ss41282": {"ep": 472183, "n": "3", "progress": 1234, "duration": 2702},
                                   "ss31126": {"ep": 333859, "n": "10", "progress": -1, "duration": 2880}})
        cached = {"ok": True, "kind": "video", "items": [{"kind": "video", "id": "BV1aaaaaaaaa"}],
                  "pgc": [{"kind": "pgc", "id": "ss41282", "title": "Doctor X 第七季"},
                          {"kind": "pgc", "id": "ss9", "title": "别的"}]}
        before = copy.deepcopy(cached)
        mine = accountfeeds.annotate_search(cached, seasons)
        self.assertEqual(cached, before, "the shared cached answer is not touched")
        self.assertEqual(mine["pgc"][0]["watched"], seasons["ss41282"])
        self.assertNotIn("watched", mine["pgc"][1])
        self.assertIs(mine["items"], cached["items"])
        ft = {"ok": True, "kind": "ft", "items": [{"kind": "pgc", "id": "ss31126"}]}
        self.assertEqual(accountfeeds.annotate_search(ft, seasons)["items"][0]["watched"]["progress"], -1)
        self.assertIs(accountfeeds.annotate_search(cached, {}), cached)


class HistoryPageTests(FeedHarness):
    def setUp(self):
        super().setUp()
        self.fake = PagedBili()
        self.relay.bili_accounts.fetch = self.fake

    def page(self, query="", user=None):
        return self.request("GET", "/tv/api/feeds/history" + query, user=user or self.kid)

    def test_login_card_and_strangers(self):
        self.assertEqual(self.request("GET", "/tv/api/feeds/history")[0], 404)
        status, answer = self.page()
        self.assertEqual((status, answer["state"], answer["items"]), (200, "login", []))
        self.assertTrue(answer["writeback"])
        self.assertEqual(self.fake.count("/history/cursor"), 0)

    def test_first_page_shares_the_cache_and_more_follows_the_cursor(self):
        self.sign_in(self.kid)
        answer = self.page("?fresh=1")[1]
        self.assertEqual(answer["state"], "ready", answer)
        self.assertEqual([x["play"]["id"] for x in answer["items"]], ["ss41282", "ss31126", "BV1hist00001"])
        self.assertEqual(answer["more"], {"max": 1, "view_at": 1761300000, "business": "archive"})
        # the home row uses the same fetch
        self.assertEqual(len(self.row("bili_history", self.kid)["items"]), 3)
        self.assertEqual(self.fake.count("/history/cursor"), 1)
        # opening the page again within FRESH_GAP_S does not ask again; after it, it does
        self.page("?fresh=1")
        self.assertEqual(self.fake.count("/history/cursor"), 1)
        self.mono.t += accountfeeds.FRESH_GAP_S
        self.page("?fresh=1")
        self.assertEqual(self.fake.count("/history/cursor"), 2)
        more = self.page("?max=1&view_at=1761300000&business=archive")[1]
        self.assertEqual([x["play"]["id"] for x in more["items"]], ["ss42000", "BV1hist00003"])
        self.assertIsNone(more["more"])
        self.assertEqual(more["items"][0]["hist"]["ep"], "第5话 秘密任务")
        self.assertEqual(more["items"][1]["play"]["part"], 3)
        url, cookie, _ = self.fake.calls[-1]
        for part in ("max=1", "view_at=1761300000", "business=archive", "ps=30"):
            self.assertIn(part, url)
        self.assertIn("SESSDATA=" + SESSDATA, cookie)
        for bad in ("?max=x&view_at=1", "?max=1&view_at=1&business=a%26b", "?max=1&view_at=-3"):
            self.assertFalse(self.page(bad)[1]["ok"], bad)
        self.assert_no_leak()

    def test_errors_are_a_state(self):
        self.sign_in(self.kid)
        self.fake.fail = {"code": -101, "message": "账号未登录"}
        answer = self.page()[1]
        self.assertEqual(answer["state"], "login")
        self.assertIn("重新扫码", answer["message"])


class SyncTests(FeedHarness):
    def setUp(self):
        super().setUp()
        self.fake = PagedBili()
        self.relay.bili_accounts.fetch = self.fake

    def beats(self):
        return [c for c in self.fake.calls if "/click-interface/web/heartbeat" in c[0]]

    def test_episodes_report_through_the_heartbeat(self):
        self.sign_in(self.kid)
        self.assertTrue(self.feeds.report(self.kid, "bilibili", "ep472183/511009626", 754, final=True))
        url, cookie, data = self.beats()[-1]
        self.assertEqual(url, "https://api.bilibili.com/x/click-interface/web/heartbeat")
        self.assertIn("SESSDATA=" + SESSDATA, cookie)
        self.assertEqual({k: data[k] for k in ("aid", "cid", "epid", "sid", "type", "sub_type", "played_time",
                                               "dt", "play_type", "csrf")},
                         {"aid": 506000003, "cid": 511009626, "epid": 472183, "sid": 41282, "type": 4,
                          "sub_type": 5, "played_time": 754, "dt": 2, "play_type": 0, "csrf": JCT})
        # an episode in a section (extras), and a bare ep id: the season is asked once per episode
        self.assertTrue(self.feeds.report(self.kid, "bilibili", "ep472190", 30, final=True))
        self.assertEqual(self.beats()[-1][2]["cid"], 511009690)
        self.mono.t += accountfeeds.REPORT_GAP_S
        self.feeds.report(self.kid, "bilibili", "ep472183/511009626", 900, final=True)
        self.assertEqual(self.fake.count("/pgc/view/web/season"), 2)
        # not in the season: nothing is sent
        before = len(self.beats())
        self.assertTrue(self.feeds.report(self.kid, "bilibili", "ep999/1", 30, final=True))
        self.assertEqual(len(self.beats()), before)
        self.assert_no_leak()

    def test_tv_series_episode_is_reported(self):
        self.sign_in(self.kid)
        dev = {"id": "d1", "owner": self.kid}
        self.relay._report_history(dev, Video(bvid="ep472183/511009626", title="第3集", duration=2702, pubdate=0),
                                   0, "ended", 2702)
        self.assertEqual(self.beats()[-1][2]["played_time"], 2702)

    def test_a_report_makes_the_history_fresh_again(self):
        self.sign_in(self.kid)
        self.request("GET", "/tv/api/feeds/history", user=self.kid)
        self.assertEqual(self.fake.count("/history/cursor"), 1)
        self.request("GET", "/tv/api/feeds/history", user=self.kid)
        self.assertEqual(self.fake.count("/history/cursor"), 1, "cached")
        self.feeds.report(self.kid, "bilibili", "BV1hist00001", 300, final=True)
        self.request("GET", "/tv/api/feeds/history", user=self.kid)
        self.assertEqual(self.fake.count("/history/cursor"), 2, "asked again after the report")

    def test_search_marks_the_seasons_watched(self):
        class Client:
            def browse(self, query, kind="video", page=1):
                pgc = [{"kind": "pgc", "id": "ss41282", "title": "Doctor X 第七季"},
                       {"kind": "pgc", "id": "ss7", "title": "没看过"}]
                return {"items": pgc if kind == "ft" else [], "page": 1, "pages": 1, "total": 2,
                        **({"pgc": pgc} if kind == "video" else {})}
        self.relay.clients["bilibili"] = Client()
        answer = self.request("GET", "/tv/api/bili/search?q=doctor", user=self.kid)[1]
        self.assertTrue(answer["ok"])
        self.assertNotIn("watched", answer["pgc"][0], "not logged in: nothing to mark")
        self.sign_in(self.kid)
        self.relay.bili_search._last.clear()
        answer = self.request("GET", "/tv/api/bili/search?q=doctor", user=self.kid)[1]
        self.assertEqual(answer["pgc"][0]["watched"], {"ep": 472183, "n": "3", "progress": 1234, "duration": 2702})
        self.assertNotIn("watched", answer["pgc"][1])
        # the scan follows the cursor: up to SEASON_SCAN items, here two pages
        self.assertEqual(self.fake.count("/history/cursor"), 2)
        self.mono.t += 2
        answer = self.request("GET", "/tv/api/bili/search?q=doctor&kind=ft", user=self.kid)[1]
        self.assertEqual(answer["items"][0]["watched"]["n"], "3")
        self.assertEqual(self.fake.count("/history/cursor"), 2, "cached for TTL_S")
        # another user's search (sharing the cached result) is not marked
        answer = self.request("GET", "/tv/api/bili/search?q=doctor", user=self.admin)[1]
        self.assertNotIn("watched", answer["pgc"][0])
        self.assert_no_leak()


if __name__ == "__main__":
    unittest.main()
