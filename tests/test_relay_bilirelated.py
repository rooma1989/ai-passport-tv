"""B 站相关推荐（bilibili.Bilibili.related、bilisearch.BiliSearch.related / GET /api/bili/related）：播放器放完一个
B 站视频后的「接下来」。用记下来的接口回答格式，不连 B 站。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server.relay import bilibili, bilisearch

ANSWER = {"code": 0, "data": [
    {"bvid": "BV1aa411c7mA", "title": "相关一", "pic": "http://i0.hdslb.com/bfs/archive/a.jpg", "duration": 61,
     "owner": {"name": "UP 甲"}},
    {"bvid": "not-a-bvid", "title": "坏的"},
    {"bvid": "BV1bb411c7mB", "title": "相关二", "pic": "https://evil.example/x.jpg", "duration": 0, "owner": {}},
]}


class RelatedTests(unittest.TestCase):
    def test_client_parses(self):
        client = bilibili.Bilibili(fetch_json=lambda url, cookie, timeout=None: ANSWER if "related" in url
                                   else {"code": 0, "data": {"b_3": "x"}})
        found = client.related("BV1xx411c7mD")
        self.assertEqual([v["bvid"] for v in found], ["BV1aa411c7mA", "BV1bb411c7mB"])
        self.assertEqual(found[0]["pic"], "https://i0.hdslb.com/bfs/archive/a.jpg")
        self.assertEqual(found[1]["pic"], "", "covers off the image host are dropped")
        self.assertEqual(found[0]["owner"], "UP 甲")

    def test_cards_and_cache(self):
        calls = []

        class Client:
            def related(self, bvid):
                calls.append(bvid)
                return [{"bvid": "BV1aa411c7mA", "title": "相关一", "pic": "https://i0.hdslb.com/bfs/a.jpg",
                         "duration": 61, "owner": "UP 甲"}]
        now = [1000.0]
        search = bilisearch.BiliSearch(clock=lambda: now[0])
        answer = search.related(Client(), "BV1xx411c7mD")
        self.assertTrue(answer["ok"])
        card = answer["items"][0]
        self.assertEqual(card["play"], {"kind": "cast", "url": "https://www.bilibili.com/video/BV1aa411c7mA"})
        self.assertTrue(card["art"]["url"].startswith("api/bili/thumb?s=v&u=https%3A%2F%2Fi0.hdslb.com"))
        search.related(Client(), "BV1xx411c7mD")
        self.assertEqual(len(calls), 1, "cached")
        now[0] += bilisearch.RELATED_CACHE_S
        search.related(Client(), "BV1xx411c7mD")
        self.assertEqual(len(calls), 2)
        self.assertFalse(search.related(Client(), "nope")["ok"])

    def test_failure_is_quiet(self):
        class Down:
            def related(self, bvid):
                raise bilibili.SourceError("down")
        search = bilisearch.BiliSearch(log=lambda _m: None)
        self.assertEqual(search.related(Down(), "BV1xx411c7mD"), {"ok": False, "error": "取不到"})


if __name__ == "__main__":
    unittest.main()
