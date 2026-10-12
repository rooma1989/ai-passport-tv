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


if __name__ == "__main__":
    unittest.main()
