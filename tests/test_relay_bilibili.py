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


if __name__ == "__main__":
    unittest.main()
