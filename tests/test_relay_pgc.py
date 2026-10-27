"""Official Bilibili dramas and anime (PGC) in the console: search results and the home page's history.

The search fixtures in tests/fixtures/bilibili/ are trimmed from real anonymous answers
(2026-10-27) of /x/web-interface/search/type with search_type media_ft (keyword
"Doctor X": seven seasons, while search_type video only finds uploaded clips and
collections) and media_bangumi. (The all-in-one search /x/web-interface/wbi/search/all/v2
has these too, but anonymously it soon answers code 0 with only a v_voucher, so it is
not used.) history_pgc.json follows the shape of /x/web-interface/history/cursor:
history.epid is the episode that was watched, kid the season, progress its seconds
(-1 = finished). No network: the owner's router may block Bilibili.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from server.relay import accountfeeds, bilibili, home
from server.relay.bilibili import Bilibili, BilibiliError, MemberOnly

from test_relay_bilibili import SPI
from test_relay_subs import SEARCH_VIDEOS, SearchFetch

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "bilibili"


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


MEDIA_FT = fixture("search_media_ft.json")
MEDIA_BANGUMI = fixture("search_media_bangumi.json")
HISTORY = fixture("history_pgc.json")


class ShapeTests(unittest.TestCase):
    def test_media_ft_seasons(self):
        items = bilibili.browse_pgc(MEDIA_FT["data"])
        self.assertEqual([i["id"] for i in items],
                         ["ss41282", "ss31126", "ss22417", "ss23735", "ss29071", "ss29037", "ss29036"])
        first = items[0]
        self.assertEqual(first["kind"], "pgc")
        self.assertEqual(first["title"], "Doctor X 第七季", "the <em> highlight is stripped")
        self.assertIn("ドクターX", first["org_title"])
        self.assertEqual((first["badge"], first["year"], first["areas"], first["show"], first["eps"], first["score"]),
                         ("电视剧", 2021, "日本", "全10集", 10, 9.8))
        self.assertEqual(first["tags"], ["独家"])
        self.assertEqual(first["cover"],
                         "https://i0.hdslb.com/bfs/bangumi/image/5193bd1dcd9a8fd279e5b90f9c76aef8710f1803.jpg")
        self.assertEqual(first["link"], "https://www.bilibili.com/bangumi/play/ss41282")
        self.assertTrue(first["desc"])

    def test_media_bangumi_seasons_carry_the_member_tag(self):
        items = bilibili.browse_pgc(MEDIA_BANGUMI["data"])
        self.assertTrue(items)
        self.assertTrue(all(i["badge"] == "番剧" and "大会员" in i["tags"] for i in items))
        upcoming = items[0]     # not aired yet: no score, no date
        self.assertEqual((upcoming["score"], upcoming["year"], upcoming["show"]), (0, 0, ""))

    def test_bad_entries_are_dropped(self):
        rows = [{"season_id": True, "title": "布尔"}, {"season_id": 0, "title": "零"}, {"season_id": 5, "title": ""},
                "x", {"season_id": 6, "title": "<em>好</em>", "media_score": {"score": 99},
                      "cover": "https://evil.example/bfs/a.jpg", "badges": [{"text": "大会员"}, {"text": "大会员"}, 3]},
                {"season_id": 6, "title": "重复"}]
        items = bilibili.browse_pgc({"result": rows})
        self.assertEqual(len(items), 1)
        self.assertEqual((items[0]["title"], items[0]["score"], items[0]["cover"], items[0]["tags"]),
                         ("好", 0, "", ["大会员"]))


class CardFieldTests(unittest.TestCase):
    """Fields for the Bilibili-style card: cast, staff, votes, date and the episode buttons with 会员 marks."""

    def test_cast_votes_date_and_episodes(self):
        data = {"result": [{"season_id": 41282, "title": "<em class=\"keyword\">Doctor X</em> 第七季",
                            "cv": "米仓凉子\n内田有纪\n\n岸部一德", "staff": "导演：田村直己\n编剧：中园美保",
                            "media_score": {"score": 9.8, "user_count": 1087}, "pubtime": 1634140800,
                            "eps": [{"id": 472181, "index_title": "1", "badges": None},
                                    {"id": 472182, "index_title": "2", "badges": [{"text": "会员"}]},
                                    {"id": "bad"}, {"id": 472183, "title": "3"}]}]}
        item = bilibili.browse_pgc(data)[0]
        self.assertEqual(item["title"], "Doctor X 第七季")
        self.assertEqual(item["cast"], "米仓凉子 内田有纪 岸部一德")
        self.assertEqual(item["staff"], "导演：田村直己 编剧：中园美保")
        self.assertEqual((item["votes"], item["score"], item["date"]), (1087, 9.8, "2021-10-13"))
        self.assertEqual(item["episodes"], [{"ep": 472181, "n": "1", "vip": False}, {"ep": 472182, "n": "2", "vip": True},
                                            {"ep": 472183, "n": "3", "vip": False}])

    def test_long_lists_are_capped(self):
        data = {"result": [{"season_id": 1, "title": "长剧", "cv": "\n".join(f"演员{i}" for i in range(50)),
                            "eps": [{"id": i + 1, "index_title": str(i + 1)} for i in range(200)]}]}
        item = bilibili.browse_pgc(data)[0]
        self.assertLessEqual(len(item["cast"]), bilibili.PGC_CAST_MAX)
        self.assertEqual(len(item["episodes"]), bilibili.PGC_EPISODES)


class BrowseTests(unittest.TestCase):
    def answers(self, **extra):
        return {"finger/spi": SPI, "search_type=video": SEARCH_VIDEOS, "search_type=media_ft": MEDIA_FT,
                "search_type=media_bangumi": MEDIA_BANGUMI, **extra}

    def test_video_first_page_puts_the_official_series_first(self):
        fetch = SearchFetch(self.answers())
        found = Bilibili(fetch_json=fetch).browse("Doctor X", "video", 1)
        kinds = [url.split("search_type=")[1].split("&")[0] for url, _ in fetch.calls if "search_type=" in url]
        self.assertEqual(kinds, ["video", "media_ft", "media_bangumi"])
        self.assertTrue(all("/x/web-interface/search/type?" in url for url, _ in fetch.calls[1:]))
        self.assertEqual(len(found["items"]), 3)
        ids = [i["id"] for i in found["pgc"]]
        dramas = [f"ss{r['season_id']}" for r in MEDIA_FT["data"]["result"]][:bilibili.PGC_TOP]
        self.assertEqual(len(dramas), 7, "all seven seasons, like Bilibili's own page")
        self.assertEqual(ids[:len(dramas)], dramas, "dramas first")
        self.assertEqual(len(ids), len(dramas) + min(bilibili.PGC_TOP, len(MEDIA_BANGUMI["data"]["result"])))
        self.assertEqual(found["pgc"][-1]["badge"], "番剧")

    def test_a_failed_series_lookup_keeps_the_videos(self):
        fetch = SearchFetch(self.answers(**{"search_type=media_ft": {"code": -412, "message": "请求被拦截"},
                                            "search_type=media_bangumi": TimeoutError("timed out")}))
        found = Bilibili(fetch_json=fetch).browse("舒克", "video", 1)
        self.assertEqual((len(found["items"]), found["pgc"]), (3, []))

    def test_later_pages_and_other_kinds_do_not_look_up_series(self):
        fetch = SearchFetch(self.answers())
        found = Bilibili(fetch_json=fetch).browse("舒克", "video", 2)
        self.assertNotIn("pgc", found)
        self.assertFalse(any("media_" in url for url, _ in fetch.calls))

    def test_drama_and_anime_kinds(self):
        fetch = SearchFetch({"finger/spi": SPI, "search/type": MEDIA_FT})
        found = Bilibili(fetch_json=fetch).browse("X医生", "ft", 1)
        self.assertIn("search_type=media_ft", fetch.calls[-1][0])
        self.assertEqual(len(found["items"]), 7)
        self.assertEqual(found["items"][0]["kind"], "pgc")
        self.assertNotIn("pgc", found)
        fetch.answers["search/type"] = MEDIA_BANGUMI
        found = Bilibili(fetch_json=fetch).browse("间谍过家家", "bangumi", 1)
        self.assertIn("search_type=media_bangumi", fetch.calls[-1][0])
        self.assertEqual(found["items"][0]["badge"], "番剧")


class RefusalTests(unittest.TestCase):
    def test_member_and_area_messages(self):
        self.assertEqual(bilibili.pgc_refusal(BilibiliError("ss1: 没有免费的集（都要会员）", permanent=True)),
                         bilibili.MEMBER_HINT)
        self.assertEqual(bilibili.pgc_refusal(MemberOnly("ep1: 只能试看")), bilibili.MEMBER_HINT)
        self.assertEqual(bilibili.pgc_refusal(BilibiliError("/pgc/view/web/season: code=-10403 抱歉您所在地区不可观看！",
                                                            permanent=True)), bilibili.AREA_HINT)
        self.assertEqual(bilibili.pgc_refusal(BilibiliError("code=-404 啥都木有", permanent=True)), "")


class HistoryTests(unittest.TestCase):
    """首页「B 站观看记录」里的番剧：打开的是看的那一集，从看到的地方起。"""

    def test_the_watched_episode_and_its_position(self):
        items = {e["id"]: e for e in accountfeeds.bili_history(HISTORY["data"])}
        self.assertEqual(list(items), ["ss41282", "ss31126", "BV1hist00001"])
        self.assertEqual((items["ss41282"]["ep"], items["ss41282"]["position"]), (472183, 1234))
        self.assertIn("第3集", items["ss41282"]["title"])
        self.assertEqual(items["BV1hist00001"]["ep"], 0)
        shaped = {x["play"]["id"]: x for x in (accountfeeds.media("bili_history", e) for e in items.values())}
        play = shaped["ss41282"]["play"]
        self.assertEqual((play["ep"], play["start"]), (472183, 1234))
        self.assertEqual(accountfeeds.play_url(play), "https://www.bilibili.com/bangumi/play/ep472183")
        self.assertEqual(home._pc_body(play), {"url": "https://www.bilibili.com/bangumi/play/ep472183",
                                               "start": 1234.0})
        finished = shaped["ss31126"]["play"]
        self.assertEqual(finished["ep"], 333859)
        self.assertNotIn("start", finished, "finished: that episode from the start")
        self.assertEqual(home._pc_body(finished), {"url": "https://www.bilibili.com/bangumi/play/ep333859"})
        self.assertNotIn("ep", shaped["BV1hist00001"]["play"])

    def test_the_episode_id_is_checked(self):
        for bad in (True, "472183", -1, 0, 10 ** 12, 1.5):
            self.assertEqual(accountfeeds.play_url({"site": "bilibili", "id": "ss41282", "ep": bad}),
                             "https://www.bilibili.com/bangumi/play/ss41282", bad)


if __name__ == "__main__":
    unittest.main()
