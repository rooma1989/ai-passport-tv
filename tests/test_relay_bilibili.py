"""Bilibili client: links, API payloads, and errors.

The payloads are trimmed from real answers recorded during the spike. Only the
unsigned endpoints are used, because Bilibili answers 412 to the server address
for pages and for the signed list API.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server.relay import bilibili
from server.relay.bilibili import Bilibili, BilibiliError

SPI = {"code": 0, "data": {"b_3": "B3-COOKIE", "b_4": "x"}}
CARD = {"code": 0, "data": {"card": {"mid": "546195", "name": "老番茄"}}}
LIST = {"code": 0, "data": {"archives": [
    {"bvid": "BV1aZpw6DEKJ", "title": "第一次去美国", "duration": 689, "pubdate": 1759700000},
    {"bvid": "BV1feaH63Epk", "title": "五个乐器", "duration": 727, "pubdate": 1759200000},
]}}
VIEW = {"code": 0, "data": {"cid": 42511303711, "owner": {"mid": 546195, "name": "老番茄"},
                            "title": "第一次去美国", "duration": 689}}
PLAY = {"code": 0, "data": {"quality": 16, "durl": [{"url": "https://upos.example/v.mp4"}]}}
# 番剧（海尔兄弟，季 2307）：第 2 集要会员。
SEASON = {"code": 0, "result": {"season_id": 2307, "season_title": "海尔兄弟", "episodes": [
    {"id": 52309, "cid": 3452734, "title": "1", "long_title": "海尔归来", "show_title": "第1话 海尔归来",
     "status": 2, "badge": "", "duration": 659000},
    {"id": 52310, "cid": 3452735, "title": "2", "long_title": "x", "status": 13, "badge": "会员",
     "duration": 600000},
    {"id": 52311, "cid": 3452736, "title": "3", "long_title": "火山", "status": 2, "badge": "",
     "duration": 640000},
]}}
PGC_PLAY = {"code": 0, "result": {"is_preview": 0, "durl": [{"url": "https://upos.example/ep.mp4"}]}}
# 多集视频（舒克和贝塔，13 P，这里留 2 P）。
PAGES = {"code": 0, "data": {"cid": 1, "title": "【舒克和贝塔】4K修复全13集1989|上海美术电影制片厂",
                             "pages": [{"cid": 11, "page": 1, "part": "E01：名誉", "duration": 1225},
                                       {"cid": 12, "page": 2, "part": "E02：救援", "duration": 1200}]}}
UGC_SEASON = {"code": 0, "data": {"cid": 21, "title": "黑猫警长 第1集", "pages": [{"cid": 21, "page": 1}],
                                  "ugc_season": {"title": "黑猫警长全集", "sections": [{"episodes": [
                                      {"bvid": "BV1aaaaaaaaa", "cid": 21, "title": "第1集",
                                       "arc": {"duration": 900}},
                                      {"bvid": "BV1bbbbbbbbb", "cid": 22, "title": "第2集",
                                       "arc": {"duration": 910}}]}]}}}


class FakeFetch:
    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def __call__(self, url, cookie):
        self.calls.append((url, cookie))
        for prefix, answer in self.answers.items():
            if prefix in url:
                if isinstance(answer, Exception):
                    raise answer
                return answer
        raise AssertionError(f"unexpected url {url}")


# Voice search (search_type=video): trimmed from a real answer for 猫和老鼠 (keyword highlighting in
# <em>, durations as "m:s" with minutes past 60), plus the kinds that cannot be cast.
SEARCH_VIDEO = {"code": 0, "message": "OK", "data": {"page": 1, "pagesize": 20, "numResults": 1000, "result": [
    {"type": "video", "bvid": "BV1yi7rzDEWc", "title": "经典欣赏～《<em class=\"keyword\">猫和老鼠</em>》",
     "author": "猫咪老鼠大王", "duration": "1704:3", "is_pay": 0, "is_charge_video": 0, "badgepay": False,
     "arcurl": "http://www.bilibili.com/video/av114608736116096"},
    {"type": "video", "bvid": "BV1EhAaeXEYu", "title": "20分钟纯享版 | <em class=\"keyword\">猫和老鼠</em> &amp; 朋友",
     "author": "猫和老鼠", "duration": "19:57", "is_pay": 0, "is_charge_video": 0, "badgepay": False,
     "arcurl": "http://www.bilibili.com/video/av114024301724562"},
    {"type": "video", "bvid": "BV1chargeAAA", "title": "充电专属", "author": "x", "duration": "3:00",
     "is_pay": 0, "is_charge_video": 1, "arcurl": "http://www.bilibili.com/video/av1"},
    {"type": "video", "bvid": "BV1payAAAAAA", "title": "付费", "author": "x", "duration": "3:00",
     "is_pay": 1, "is_charge_video": 0, "arcurl": "http://www.bilibili.com/video/av2"},
    {"type": "video", "bvid": "BV1badgeAAAA", "title": "付费角标", "author": "x", "duration": "3:00",
     "badgepay": True, "arcurl": "http://www.bilibili.com/video/av3"},
    {"type": "video", "bvid": "BV1bangumiAA", "title": "番剧", "author": "x", "duration": "24:00",
     "arcurl": "https://www.bilibili.com/bangumi/play/ep1"},
    {"type": "video", "bvid": "BV1cheeseAAA", "title": "课程", "author": "x", "duration": "24:00",
     "arcurl": "https://www.bilibili.com/cheese/play/ep1"},
    {"type": "video", "bvid": "BV1liveAAAAA", "title": "直播", "author": "x", "duration": "1:00",
     "is_live_room_inline": 1, "arcurl": "https://live.bilibili.com/21852"},
    {"type": "video", "bvid": "BV1nodurAAAA", "title": "时长不明", "author": "x", "duration": "",
     "arcurl": "http://www.bilibili.com/video/av4"},
    {"type": "video", "bvid": "BV1yi7rzDEWc", "title": "重复", "author": "x", "duration": "1:00"},
    {"type": "video", "bvid": "not-a-bvid", "title": "坏 id", "author": "x", "duration": "1:00"},
    {"type": "ketang", "bvid": "BV1ketangAAA", "title": "课堂", "duration": "1:00"},
    {"type": "video", "bvid": "BV1m9pT6fEHu", "title": "<em class=\"keyword\">猫和老鼠</em> | 鼠鼠的游泳教学",
     "author": "猫和老鼠", "duration": "4:40", "is_pay": 0, "is_charge_video": 0,
     "arcurl": "http://www.bilibili.com/video/av117398451653392"},
]}}
# Voice search (search_type=bili_user) for 老番茄: the newest uploads come along in "res".
SEARCH_USER = {"code": 0, "message": "OK", "data": {"page": 1, "result": [
    {"type": "bili_user", "mid": 546195, "uname": "老番茄", "usign": "新浪微博：_老番茄_", "fans": 20840107,
     "videos": 680, "level": 6, "is_upuser": 1, "is_live": 0, "res": [
         {"bvid": "BV1chargeBBB", "title": "充电专属的新视频", "duration": "10:00", "is_pay": 0,
          "is_charge_video": 1, "arcurl": "http://www.bilibili.com/video/av5"},
         {"bvid": "BV1iqpt6cEMU", "title": "超市里……未检测到人脸……", "duration": "48:13", "is_pay": 0,
          "is_charge_video": 0, "arcurl": "http://www.bilibili.com/video/av117411353204712"}]},
    {"type": "bili_user", "mid": 609013982, "uname": "PPOMO<em class=\"keyword\">老番茄</em>", "fans": 96724,
     "videos": 96, "res": []},
    {"type": "bili_user", "mid": 1, "uname": "没有投稿", "fans": 3, "videos": 0, "res": []},
    {"type": "bili_user", "mid": "x1", "uname": "坏 mid", "fans": 3, "videos": 5},
    {"type": "video", "bvid": "BV1yi7rzDEWc", "title": "不是 UP 主"},
]}}


class SearchFetch(FakeFetch):
    """FakeFetch that also takes the timeout the voice search passes."""

    def __init__(self, answers):
        super().__init__(answers)
        self.timeouts = []

    def __call__(self, url, cookie, timeout=None):
        self.timeouts.append(timeout)
        return super().__call__(url, cookie)


class LinkTests(unittest.TestCase):
    def test_space_links(self):
        for text in ["https://space.bilibili.com/546195",
                     "https://space.bilibili.com/546195/video?tid=0",
                     "space.bilibili.com/546195/",
                     "【老番茄的个人空间】 https://m.bilibili.com/space/546195 快来看"]:
            self.assertEqual(bilibili.parse_creator_link(text), "546195", text)

    def test_rejected_hosts(self):
        for text in ["https://space.bilibili.com.evil.cn/546195",
                     "https://evil.cn/space.bilibili.com/546195",
                     "https://space.bilibili.com/abc", "546195", ""]:
            self.assertIsNone(bilibili.parse_creator_link(text), text)

    def test_video_links(self):
        for text in ["https://www.bilibili.com/video/BV1aZpw6DEKJ/?spm_id_from=333",
                     "https://m.bilibili.com/video/BV1aZpw6DEKJ",
                     "看这个 https://www.bilibili.com/video/BV1aZpw6DEKJ"]:
            self.assertEqual(bilibili.parse_video_link(text), "BV1aZpw6DEKJ", text)
        self.assertIsNone(bilibili.parse_video_link("https://www.bilibili.com/video/av170001"))

    def test_short_link(self):
        self.assertEqual(bilibili.short_link("【标题】 https://b23.tv/AbC123 "), "https://b23.tv/AbC123")
        self.assertIsNone(bilibili.short_link("https://b23.tv.evil.cn/AbC123"))


class ClientTests(unittest.TestCase):
    def client(self, **extra):
        answers = {"finger/spi": SPI, "web-interface/card": CARD, "space/arc/list": LIST,
                   "web-interface/view": VIEW, "player/playurl": PLAY}
        answers.update(extra)
        self.fetch = FakeFetch(answers)
        return Bilibili(fetch_json=self.fetch, resolve=lambda url: "https://space.bilibili.com/7/dynamic")

    def test_creator_name_and_uploads(self):
        name, videos = self.client().creator("546195")
        self.assertEqual(name, "老番茄")
        self.assertEqual([v.bvid for v in videos], ["BV1aZpw6DEKJ", "BV1feaH63Epk"])
        self.assertEqual(videos[0].duration, 689)
        self.assertIn("mid=546195", self.fetch.calls[-1][0])
        self.assertIn("ps=20", self.fetch.calls[-1][0])
        self.assertEqual(self.fetch.calls[-1][1], "buvid3=B3-COOKIE")

    def test_media_url(self):
        self.assertEqual(self.client().media_url("BV1aZpw6DEKJ"), "https://upos.example/v.mp4")
        play = [u for u, _ in self.fetch.calls if "playurl" in u][0]
        for part in ["bvid=BV1aZpw6DEKJ", "cid=42511303711", "qn=16", "fnval=1", "platform=html5"]:
            self.assertIn(part, play)

    def test_error_code(self):
        client = self.client(**{"player/playurl": {"code": -404, "message": "啥都木有"}})
        with self.assertRaises(BilibiliError) as caught:
            client.media_url("BV1aZpw6DEKJ")
        self.assertTrue(caught.exception.permanent)

    def test_missing_fields(self):
        client = self.client(**{"player/playurl": {"code": 0, "data": {"durl": []}}})
        with self.assertRaises(BilibiliError):
            client.media_url("BV1aZpw6DEKJ")

    def test_network_error_wrapped_and_cookie_refreshed(self):
        client = self.client(**{"space/arc/list": OSError("timeout")})
        with self.assertRaises(BilibiliError) as caught:
            client.uploads("546195")
        self.assertFalse(caught.exception.permanent)
        self.fetch.answers["space/arc/list"] = LIST
        client.uploads("546195")
        self.assertEqual(sum("finger/spi" in u for u, _ in self.fetch.calls), 2)

    def test_creator_from_links(self):
        client = self.client()
        self.assertEqual(client.creator_id("https://space.bilibili.com/546195"), "546195")
        self.assertEqual(client.creator_id("https://www.bilibili.com/video/BV1aZpw6DEKJ"), "546195")
        self.assertEqual(client.creator_id("https://b23.tv/AbC123"), "7")
        self.assertIsNone(client.creator_id("https://example.com/"))


    def test_uploads_page_keeps_covers_and_the_creators_face(self):
        listing = {"code": 0, "data": {"page": {"count": 681, "pn": 2, "ps": 30}, "archives": [
            {"bvid": "BV1aZpw6DEKJ", "title": "第一次去美国", "duration": 689, "pubdate": 1759700000,
             "pic": "http://i2.hdslb.com/bfs/archive/e858b616.jpg",
             "author": {"mid": 546195, "name": "老番茄", "face": "http://i0.hdslb.com/bfs/face/bc5ca101.jpg"}},
            {"bvid": "BV1feaH63Epk", "title": "合作", "duration": 727, "pubdate": 1759200000,
             "pic": "https://evil.example/x.jpg", "author": {"mid": 9, "face": "http://i0.hdslb.com/bfs/face/other.jpg"}},
        ]}}
        client = self.client(**{"space/arc/list": listing})
        videos, total = client.uploads_page("546195", 2, 30)
        self.assertEqual(([v.bvid for v in videos], total), (["BV1aZpw6DEKJ", "BV1feaH63Epk"], 681))
        self.assertIn("pn=2", self.fetch.calls[-1][0])
        self.assertIn("ps=30", self.fetch.calls[-1][0])
        self.assertEqual(client.cover("BV1aZpw6DEKJ"), "https://i2.hdslb.com/bfs/archive/e858b616.jpg",
                         "http:// cover from the list is kept as https")
        self.assertEqual(client.cover("BV1feaH63Epk"), "", "only Bilibili's image host")
        self.assertEqual(client.face("546195"), "https://i0.hdslb.com/bfs/face/bc5ca101.jpg",
                         "the uploader's own face, not a co-author's")
        self.assertEqual(client.upload_total("546195"), 681)
        self.assertEqual([v.bvid for v in client.uploads("546195")], ["BV1aZpw6DEKJ", "BV1feaH63Epk"])
        self.assertIn("pn=1", self.fetch.calls[-1][0])

    def test_video_lookup(self):
        client = self.client()
        video = client.video("BV1aZpw6DEKJ")
        self.assertEqual((video.bvid, video.title, video.duration), ("BV1aZpw6DEKJ", "第一次去美国", 689))
        self.assertEqual(client.video_id("看 https://www.bilibili.com/video/BV1aZpw6DEKJ"), "BV1aZpw6DEKJ")
        self.assertIsNone(client.video_id("https://space.bilibili.com/546195"))

    def test_video_id_from_short_link(self):
        client = Bilibili(fetch_json=FakeFetch({}),
                          resolve=lambda url: "https://www.bilibili.com/video/BV1feaH63Epk?x=1")
        self.assertEqual(client.video_id("https://b23.tv/AbC123"), "BV1feaH63Epk")


class SeriesTests(unittest.TestCase):
    def client(self, **extra):
        answers = {"finger/spi": SPI, "pgc/view/web/season": SEASON,
                   "pgc/player/web/playurl": PGC_PLAY, "pgc/review/user": {
                       "code": 0, "result": {"media": {"season_id": 2307}}},
                   "web-interface/view": PAGES, "player/playurl": PLAY}
        answers.update(extra)
        self.fetch = FakeFetch(answers)
        return Bilibili(fetch_json=self.fetch,
                        resolve=lambda url: "https://m.bilibili.com/bangumi/play/ss2307")

    def test_series_links(self):
        for text, source in [("https://www.bilibili.com/bangumi/play/ss2307?from=x", "ss2307"),
                             ("看 https://m.bilibili.com/bangumi/play/ep52309", "ep52309"),
                             ("https://www.bilibili.com/bangumi/media/md2307/", "md2307"),
                             ("https://www.bilibili.com/video/BV11L4y177EW?p=3", "BV11L4y177EW")]:
            self.assertEqual(bilibili.parse_series_link(text), source, text)
        for text in ["https://evil.cn/bangumi/play/ss2307", "https://www.bilibili.com/bangumi/play/xx1",
                     "https://space.bilibili.com/546195", ""]:
            self.assertIsNone(bilibili.parse_series_link(text), text)

    def test_names_from_titles(self):
        for title, name in [("【4K修复】阿凡提的故事 (1979) 14集全【上美】", "阿凡提的故事"),
                            ("【舒克和贝塔】4K修复全13集1989|上海美术电影制片厂", "舒克和贝塔"),
                            ("83版足球小将【辽艺国语配音】【1-125】", "83版足球小将"),
                            ("【合集】大头儿子和小头爸爸/大头儿子小头爸爸 151集全", "大头儿子和小头爸爸"),
                            ("【数码宝贝 第一季】全54集 国语超清中字（未删减版）", "数码宝贝 第一季"),
                            ("【合集】樱桃小丸子 第一季 台配 142集", "樱桃小丸子 第一季"),
                            ("【合集】蓝猫淘气3000问 恐龙系列", "蓝猫淘气3000问 恐龙系列"),
                            ("【480P/美国/动画】变形金刚1984 【中文语音】", "变形金刚"),
                            ("【720p】小鲤鱼历险记【2006】【完结】【无水印高清】", "小鲤鱼历险记"),
                            ("海尔兄弟", "海尔兄弟")]:
            self.assertEqual(bilibili.series_name(title), name, title)

    def test_every_bangumi_link_becomes_the_season(self):
        client = self.client()
        self.assertEqual(client.series_id("https://www.bilibili.com/bangumi/play/ss2307"), "ss2307")
        self.assertEqual(client.series_id("https://www.bilibili.com/bangumi/play/ep52311"), "ss2307")
        self.assertIn("ep_id=52311", self.fetch.calls[-1][0])
        self.assertEqual(client.series_id("https://www.bilibili.com/bangumi/media/md2307"), "ss2307")
        self.assertEqual(client.series_id("https://b23.tv/AbC123"), "ss2307")
        self.assertIsNone(client.series_id("https://example.com/"))

    def test_season_keeps_free_episodes_in_order(self):
        name, episodes = self.client().series("ss2307")
        self.assertEqual(name, "海尔兄弟")
        self.assertEqual([e.bvid for e in episodes], ["ep52309/3452734", "ep52311/3452736"])
        self.assertEqual((episodes[0].title, episodes[0].duration), ("第1话 海尔归来", 659))
        self.assertEqual(episodes[1].title, "第3集 火山")

    def test_season_without_free_episodes(self):
        paid = {"code": 0, "result": {"season_title": "x", "episodes": [SEASON["result"]["episodes"][1]]}}
        with self.assertRaises(BilibiliError) as caught:
            self.client(**{"pgc/view/web/season": paid}).series("ss1")
        self.assertTrue(caught.exception.permanent)

    def test_multipart_video(self):
        name, episodes = self.client().series("BV11L4y177EW")
        self.assertEqual(name, "舒克和贝塔")
        self.assertEqual([(e.bvid, e.title, e.duration) for e in episodes],
                         [("BV11L4y177EW/11", "E01：名誉", 1225), ("BV11L4y177EW/12", "E02：救援", 1200)])

    def test_ugc_collection(self):
        name, episodes = self.client(**{"web-interface/view": UGC_SEASON}).series("BV1aaaaaaaaa")
        self.assertEqual(name, "黑猫警长全集")
        self.assertEqual([e.bvid for e in episodes], ["BV1aaaaaaaaa/21", "BV1bbbbbbbbb/22"])

    def test_single_video_is_not_a_series(self):
        with self.assertRaises(BilibiliError) as caught:
            self.client(**{"web-interface/view": VIEW}).series("BV1aZpw6DEKJ")
        self.assertTrue(caught.exception.permanent)

    def test_episode_media_urls(self):
        client = self.client()
        self.assertEqual(client.media_url("ep52309/3452734"), "https://upos.example/ep.mp4")
        self.assertIn("ep_id=52309", self.fetch.calls[-1][0])
        self.assertIn("cid=3452734", self.fetch.calls[-1][0])
        self.assertEqual(client.media_url("BV11L4y177EW/12"), "https://upos.example/v.mp4")
        self.assertIn("cid=12", self.fetch.calls[-1][0])
        # 一集 id 直接带着 cid，不必再问 view 接口。
        self.assertFalse(any("web-interface/view" in u for u, _ in self.fetch.calls))

    def test_preview_only_episode_is_permanent(self):
        preview = {"code": 0, "result": {"is_preview": 1, "durl": [{"url": "https://upos.example/3min.mp4"}]}}
        with self.assertRaises(BilibiliError) as caught:
            self.client(**{"pgc/player/web/playurl": preview}).media_url("ep52310/3452735")
        self.assertTrue(caught.exception.permanent)


class SearchTests(unittest.TestCase):
    def client(self, **extra):
        answers = {"finger/spi": SPI, "search_type=video": SEARCH_VIDEO, "search_type=bili_user": SEARCH_USER}
        answers.update(extra)
        self.fetch = SearchFetch(answers)
        return Bilibili(fetch_json=self.fetch)

    def test_videos(self):
        hits = self.client().search("猫和老鼠")
        self.assertEqual(hits, [
            {"id": "BV1yi7rzDEWc", "title": "经典欣赏～《猫和老鼠》", "uploader": "猫咪老鼠大王",
             "duration": 1704 * 60 + 3},
            {"id": "BV1EhAaeXEYu", "title": "20分钟纯享版 | 猫和老鼠 & 朋友", "uploader": "猫和老鼠",
             "duration": 19 * 60 + 57},
            {"id": "BV1m9pT6fEHu", "title": "猫和老鼠 | 鼠鼠的游泳教学", "uploader": "猫和老鼠", "duration": 280}])
        url = self.fetch.calls[-1][0]
        self.assertTrue(url.startswith(f"{bilibili.API}/x/web-interface/search/type?"), url)
        for part in ("search_type=video", "keyword=%E7%8C%AB%E5%92%8C%E8%80%81%E9%BC%A0", "page=1"):
            self.assertIn(part, url)
        self.assertNotIn("w_rid", url, "the unsigned API: no wbi signature")
        self.assertEqual(self.fetch.calls[-1][1], "buvid3=B3-COOKIE")

    def test_limit(self):
        self.assertEqual([h["id"] for h in self.client().search("猫和老鼠", limit=1)], ["BV1yi7rzDEWc"])

    def test_creators(self):
        hits = self.client().search_creators("老番茄")
        self.assertEqual(hits, [
            {"id": "546195", "name": "老番茄", "videos": 680, "fans": 20840107,
             "latest": {"id": "BV1iqpt6cEMU", "title": "超市里……未检测到人脸……", "duration": 48 * 60 + 13}},
            {"id": "609013982", "name": "PPOMO老番茄", "videos": 96, "fans": 96724, "latest": None}])
        self.assertIn("search_type=bili_user", self.fetch.calls[-1][0])

    def test_the_whole_search_keeps_to_its_time(self):
        """buvid3 and the search share the time the voice reply can wait."""
        client = self.client()
        client.search("猫和老鼠", timeout=3.0)
        self.assertEqual(len(self.fetch.timeouts), 2)
        self.assertTrue(all(0 < t <= 3.0 for t in self.fetch.timeouts), self.fetch.timeouts)
        self.assertLessEqual(self.fetch.timeouts[1], self.fetch.timeouts[0])
        # Other calls keep their own default.
        self.assertEqual(Bilibili(fetch_json=lambda url, cookie: SPI if "spi" in url else LIST)
                         .uploads("546195")[0].bvid, "BV1aZpw6DEKJ")

    def test_slow_and_refused(self):
        with self.assertRaises(BilibiliError) as caught:
            self.client(**{"search_type=video": TimeoutError("timed out")}).search("x")
        self.assertEqual(caught.exception.reason, "slow")
        with self.assertRaises(BilibiliError) as caught:
            self.client(**{"search_type=video": OSError("connection reset")}).search("x")
        self.assertEqual(caught.exception.reason, "")
        with self.assertRaises(BilibiliError) as caught:
            self.client(**{"search_type=video": {"code": -412, "message": "请求被拦截"}}).search("x")
        self.assertEqual(caught.exception.reason, "")

    def test_parsing_helpers(self):
        self.assertEqual(bilibili.parse_duration("1:02:03"), 3723)
        self.assertEqual(bilibili.parse_duration("3:2"), 182)
        self.assertEqual(bilibili.parse_duration(95), 95)
        for bad in ("", None, "abc", "1:x", "1:2:3:4", True):
            self.assertEqual(bilibili.parse_duration(bad), 0, bad)
        self.assertEqual(bilibili.clean_title('<em class="keyword">猫</em>和&lt;老鼠&gt;\n'), "猫和<老鼠>")
        self.assertEqual(bilibili.search_hits({}), [])
        self.assertEqual(bilibili.creator_hits({"result": None}), [])


if __name__ == "__main__":
    unittest.main()


class QualityTests(unittest.TestCase):
    """清晰度：云端 360p（qn=16），家庭服务器 720p（qn=64），见 relay/__main__.py 的 _quality()。"""

    def test_playurl_asks_for_the_configured_quality(self):
        from server.relay.bilibili import Bilibili
        asked = []

        def fetch(url, *args, **kwargs):
            asked.append(url)
            if "/view" in url:
                return {"code": 0, "data": {"cid": 7}}
            return {"code": 0, "data": {"durl": [{"url": "https://example.com/v.mp4"}]}}

        client = Bilibili(fetch_json=fetch, quality=64)
        client._cookie = "buvid3=x"
        self.assertEqual(client.media_url("BV1xx411c7mD"), "https://example.com/v.mp4")
        self.assertIn("qn=64", [u for u in asked if "playurl" in u][0])

    def test_unknown_quality_falls_back_to_default(self):
        from server.relay.bilibili import DEFAULT_QUALITY, Bilibili
        self.assertEqual(Bilibili(quality=120).quality, DEFAULT_QUALITY)

    def test_relay_picks_quality_by_mode(self):
        from types import SimpleNamespace
        from server.relay.__main__ import _quality
        self.assertEqual(_quality(SimpleNamespace(bilibili_quality=None, selfhost=False)), 16)
        self.assertEqual(_quality(SimpleNamespace(bilibili_quality=None, selfhost=True)), 64)
        self.assertEqual(_quality(SimpleNamespace(bilibili_quality=32, selfhost=True)), 32)
