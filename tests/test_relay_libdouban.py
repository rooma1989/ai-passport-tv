"""My library's Douban tab (server/relay/libdouban.py, the Douban client additions in libmeta.py, Library.douban_index).

No network: every Douban answer is a small synthetic fixture with the structure of the real one (2026-11-03: the
mobile site's subject_collection items, a subject's hot interests and its recommendations), served by the fake
fetch of test_relay_libmeta.py that records each request.

What is checked: list, comment and recommendation parsing (both poster shapes, the year taken from card_subtitle,
other subject types and foreign image hosts dropped, a garbage or block answer is not a list); "in the library"
marking for a movie, a show and a season (TMDB ids never count; the season wins when a show and its first season
share an id); lists are cached, fetched in the background only (the page never waits), never refetched within the
TTL, kept when a refetch fails, and nothing is sent while Douban blocks us or online details are off; comments and
recommendations per Douban id the same way; posters only for ids the relay already knows, fetched once and then
served from disk; the wishlist per user (add only what the relay listed, remove, bounds, isolation, file mode 600,
refused when already in the library) and the home row once a scan or a match puts a wished title in the library;
who may call what (family yes, guests and signed-out callers 404 or ok: false, no library 404); the
console's sort and filter helpers for My library (console_libfilter_test.mjs) under Node.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from server.relay import libdouban, libmeta, library

import test_relay_library as lib_base
import test_relay_libmeta as meta_base
import test_relay_watch as watch_base

FakeDouban, wire, JPEG = meta_base.FakeDouban, meta_base.wire, meta_base.JPEG
IMG = "https://img{n}.doubanio.com/view/photo/m_ratio_poster/public/p{p}.jpg"


def movie_row(did, title, year, rating=8.1, shape="cover"):
    row = {"id": did, "title": title, "type": "movie", "year": str(year) if year else None,
           "rating": {"value": rating, "count": 10, "max": 10, "star_count": 4.0},
           "card_subtitle": f"{year or 2001} / 美国 / 剧情 / 导演甲 / 演员乙", "uri": f"douban://douban.com/movie/{did}"}
    if shape == "cover":
        row["cover"] = {"url": IMG.format(n=3, p=did), "width": 100, "height": 150, "shape": "rectangle"}
    else:
        row["pic"] = {"large": IMG.format(n=9, p=did), "normal": IMG.format(n=9, p=did).replace("m_ratio", "s_ratio")}
    return row


def collection(*rows, name="movie_showing"):
    return json.dumps({"start": 0, "count": len(rows), "total": 99, "subject_collection_items": list(rows),
                       "subject_collection": {"id": name, "name": "x"}}, ensure_ascii=False)


SHOWING = collection(movie_row("2000001", "某电影", 2010), movie_row("4000001", "新片", 2026),
                     {"id": "5", "type": "book", "title": "一本书"},
                     dict(movie_row("4000002", "外链", 2026), cover={"url": "https://evil.example.com/p.jpg"}))
HOT = collection(movie_row("4000003", "热门片", 2025, shape="pic"), name="movie_hot_gaia")
TV = collection(dict(movie_row("2000002", "X医生 第2季", 2013, shape="pic"), type="tv"),
                dict(movie_row("2000000", "X医生", 2012, shape="pic"), type="tv"), name="tv_hot")
TOP = collection(dict(movie_row("1292052", "肖申克的救赎", None, 9.7, shape="pic"), rank=1), name="movie_top250")
INTERESTS = json.dumps({"start": 0, "count": 3, "total": 3, "interests": [
    {"comment": "好看。", "create_time": "2008-02-27 21:43:23", "id": "1", "ip_location": "北京", "vote_count": 26538,
     "rating": {"count": 1, "max": 5, "star_count": 5, "value": 5},
     "user": {"id": "999", "uid": "secret-uid", "name": "如小果", "avatar": "https://img1.doubanio.com/icon/u1.jpg"}},
    {"comment": "没打分的。", "create_time": "2026-11-02 01:47:58", "id": "2", "vote_count": 3, "rating": None,
     "user": {"name": "乙"}},
    {"comment": "", "create_time": "2026-11-02", "id": "3", "user": {"name": "空的"}}]}, ensure_ascii=False)
RECS = json.dumps([dict(movie_row("2000001", "某电影", None, shape="pic"), year=None),
                   dict(movie_row("4000009", "推荐片", None, shape="pic")),
                   dict(movie_row("2000002", "X医生 第2季", None, shape="pic"), type="tv")], ensure_ascii=False)


def serve_lists(fake: FakeDouban) -> None:
    for name, body in (("movie_showing", SHOWING), ("movie_hot_gaia", HOT), ("tv_hot", TV), ("movie_top250", TOP)):
        fake.add(libmeta.COLLECTION_URL.format(name, libdouban.LIST_COUNT), body)
    for did in ("2000001", "4000001", "4000003", "2000002", "2000000", "1292052"):
        fake.add(IMG.format(n=3, p=did), JPEG)
        fake.add(IMG.format(n=9, p=did), JPEG)


def serve_subject(fake: FakeDouban, did: str) -> None:
    fake.add(libmeta.INTERESTS_URL.format(did, libdouban.COMMENTS), INTERESTS)
    fake.add(libmeta.RECOMMENDATIONS_URL.format(did, libdouban.RECOMMENDATIONS), RECS)


class Clock:
    def __init__(self):
        self.now = 1_800_000_000.0

    def __call__(self):
        return self.now


class Handler:
    """Enough of the relay's request handler for serve_poster."""

    def __init__(self):
        self.sent = []

    def _send(self, status, body, kind, headers=None):
        self.sent.append((status, body, kind))


def build(base: Path, clock: Clock) -> tuple[library.Library, FakeDouban, dict]:
    """A library with a movie (Douban 2000001), a show with two seasons (show 2000000, season 2 2000002) and a movie
    matched on TMDB (id 1292052, which is not a Douban id)."""
    root = base / "videos"
    for rel in ("某电影 (2010).mp4", "X医生 (2012)/S01/S01E01.mkv", "X医生 (2012)/S02/S02E01.mkv", "Shawshank (1994).mp4"):
        lib_base.touch(root / rel, lib_base.MP4)
    lib = lib_base.quiet(base / "data")
    fake = wire(lib, FakeDouban(), clock)
    serve_lists(fake)
    assert lib.add({"kind": "local", "path": str(root)})["ok"]
    titles = lib.titles()
    keys = {"movie": next(k for k, t in titles.items() if t["title"] == "某电影"),
            "tmdb": next(k for k, t in titles.items() if t["title"] == "Shawshank"),
            "show": "s" + lib.show_key("X医生")}
    put = lambda key, did, **extra: lib.meta.put(key, {"state": "matched", "manual": True, "id": did, "title": "t",
                                                        "q": "", "at": 1, **extra})
    put(keys["movie"], "2000001")
    put(keys["show"], "2000000")
    put(keys["show"] + "-2", "2000002")
    put(keys["tmdb"], "1292052", src="tmdb")
    return lib, fake, keys


def drain(lib: library.Library, limit: int = 30) -> int:
    steps = 0
    while lib.boards.step():
        steps += 1
        assert steps < limit
    return steps


# ---------------------------------------------------------------- parsing

class ParseTests(unittest.TestCase):
    def test_collection(self):
        found = libmeta.parse_collection(SHOWING)
        self.assertEqual([c["id"] for c in found], ["2000001", "4000001", "4000002"], "a book is not a film")
        self.assertEqual(found[0], {"id": "2000001", "title": "某电影", "year": 2010, "type": "movie", "rating": 8.1,
                                    "info": "美国 / 剧情 / 导演甲", "img": IMG.format(n=3, p="2000001")})
        self.assertEqual(found[2]["img"], "", "only doubanio.com images")
        top = libmeta.parse_collection(TOP)[0]
        self.assertEqual((top["year"], top["img"]), (2001, IMG.format(n=9, p="1292052")),
                         "Top250 has no year field: the one in card_subtitle; pic.large first")
        for garbage in (b"<html>sec.douban.com</html>", b"", b"[]", json.dumps({"msg": "invalid_request"}).encode()):
            self.assertIsNone(libmeta.parse_collection(garbage), garbage)
        self.assertEqual(libmeta.parse_collection(collection()), [])

    def test_interests_keep_no_user_identifiers(self):
        found = libmeta.parse_interests(INTERESTS)
        self.assertEqual(found, [{"author": "如小果", "text": "好看。", "stars": 5, "date": "2008-02-27", "votes": 26538},
                                 {"author": "乙", "text": "没打分的。", "stars": None, "date": "2026-11-02", "votes": 3}])
        self.assertNotIn("secret-uid", json.dumps(found))
        self.assertIsNone(libmeta.parse_interests(b"<html>"))
        self.assertIsNone(libmeta.parse_interests(b"[]"))

    def test_recommendations(self):
        found = libmeta.parse_recommendations(RECS)
        self.assertEqual([(c["id"], c["type"], c["year"]) for c in found],
                         [("2000001", "movie", 2001), ("4000009", "movie", 2001), ("2000002", "tv", 2001)])
        self.assertIsNone(libmeta.parse_recommendations(b"{}"))

    def test_douban_client_refuses_bad_names(self):
        douban = libmeta.Douban(fetch=FakeDouban(), clock=lambda: 0, sleep=lambda _s: None, log=lambda _m: None)
        with self.assertRaises(libmeta.Failed):
            douban.collection("../x", 3)
        with self.assertRaises(libmeta.Failed):
            douban.interests("12ab", 3)
        self.assertEqual(douban.requests, 0)


# ---------------------------------------------------------------- lists, marking, caching

class BoardsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = Clock()
        self.lib, self.fake, self.keys = build(Path(self.tmp.name), self.clock)
        self.logs = []
        self.lib.boards.log = self.logs.append

    def tearDown(self):
        self.tmp.cleanup()

    def items(self, view, name):
        return {c["id"]: c for c in next(r for r in view["lists"] if r["name"] == name)["items"]}

    def test_index_marks_movie_show_and_season(self):
        index = self.lib.douban_index()
        movie, show = self.keys["movie"], self.keys["show"]
        self.assertEqual(index["2000001"], {"key": movie, "item": movie[1:]})
        self.assertEqual(index["2000000"], {"key": show, "show": show[1:]})
        self.assertEqual(index["2000002"], {"key": show + "-2", "show": show[1:], "season": 2})
        self.assertNotIn("1292052", index, "a TMDB id is not a Douban id")
        self.lib.meta.put(show + "-1", {"state": "matched", "id": "2000000", "title": "X医生 第一季", "q": "", "at": 1})
        self.assertEqual(self.lib.douban_index()["2000000"]["season"], 1, "the same entry as the show: its season")
        self.lib.meta.put("m0123456789abcdef", {"state": "matched", "id": "7777777", "title": "gone", "q": "", "at": 1})
        self.assertNotIn("7777777", self.lib.douban_index(), "no longer in the library")

    def test_lists_load_in_the_background_and_are_marked(self):
        view = self.lib.boards.lists_view(1)
        self.assertEqual(self.fake.calls, [], "the page never waits on Douban")
        self.assertTrue(all(r["loading"] and not r["items"] for r in view["lists"]))
        self.assertEqual([r["title"] for r in view["lists"]], ["正在热映", "热门电影", "热门剧集", "高分 Top250"])
        self.assertEqual(drain(self.lib), 4)
        urls = self.fake.urls()
        self.assertEqual(len(urls), 4)
        for url, headers in self.fake.calls:
            name = url.split("/subject_collection/")[1].split("/")[0]
            self.assertEqual(headers["Referer"], libmeta.COLLECTION_REFERER.format(name))
            self.assertNotIn("Cookie", headers)
        view = self.lib.boards.lists_view(1)
        self.assertFalse(any(r["loading"] for r in view["lists"]))
        self.assertNotIn("doubanio", json.dumps(view, ensure_ascii=False), "the page never gets Douban image addresses")
        showing = self.items(view, "movie_showing")
        self.assertEqual(showing["2000001"]["have"], {"item": self.keys["movie"][1:]})
        self.assertEqual(showing["2000001"]["art"], "api/library/douban/poster/2000001")
        self.assertNotIn("have", showing["4000001"])
        self.assertEqual(showing["4000002"]["art"], "", "a foreign image host is dropped")
        tv = self.items(view, "tv_hot")
        self.assertEqual(tv["2000002"]["have"], {"show": self.keys["show"][1:], "season": 2})
        self.assertEqual(tv["2000000"]["have"], {"show": self.keys["show"][1:]})
        self.assertNotIn("have", self.items(view, "movie_top250")["1292052"], "TMDB-matched does not count")
        stored = self.lib.boards.lists_path
        self.assertEqual(stat.S_IMODE(os.stat(stored).st_mode), 0o600)

    def test_no_refetch_within_the_ttl(self):
        self.lib.boards.lists_view(1)
        drain(self.lib)
        calls = len(self.fake.calls)
        self.clock.now += libdouban.LIST_TTL_S - 60
        self.lib.boards.lists_view(1)
        self.assertEqual(drain(self.lib), 0)
        reborn = libdouban.Boards(self.lib)          # a restart reads the stored lists
        self.assertEqual(len(next(r for r in reborn._lists.values())["items"]) > 0, True)
        self.lib.boards = reborn
        self.lib.boards.lists_view(1)
        self.assertEqual(drain(self.lib), 0)
        self.assertEqual(len(self.fake.calls), calls)
        self.clock.now += 120
        view = self.lib.boards.lists_view(1)
        self.assertTrue(all(r["loading"] and r["items"] for r in view["lists"]), "old lists shown while refreshing")
        self.assertEqual(drain(self.lib), 4)

    def test_a_failed_refetch_keeps_the_old_list_and_waits(self):
        self.lib.boards.lists_view(1)
        drain(self.lib)
        self.clock.now += libdouban.LIST_TTL_S + 1
        self.fake.add(libmeta.COLLECTION_URL.format("movie_showing", libdouban.LIST_COUNT), "<html>维护中</html>")
        self.lib.boards.lists_view(1)
        drain(self.lib)
        self.assertTrue(any("看不懂" in m for m in self.logs), self.logs)
        view = self.lib.boards.lists_view(1)
        self.assertIn("2000001", self.items(view, "movie_showing"), "kept")
        calls = len(self.fake.calls)
        self.clock.now += libdouban.RETRY_S - 10
        self.lib.boards.lists_view(1)
        self.assertEqual(drain(self.lib), 0, "not again within RETRY_S")
        self.assertEqual(len(self.fake.calls), calls)

    def test_blocked_or_offline_sends_nothing(self):
        self.fake.blocked = True
        self.lib.boards.lists_view(1)
        drain(self.lib)
        self.assertEqual(len(self.fake.calls), 1, "the first block stops the rest")
        self.assertTrue(self.lib.douban.blocked())
        view = self.lib.boards.lists_view(1)
        self.assertTrue(view["blocked"])
        self.assertFalse(any(r["loading"] for r in view["lists"]))
        self.assertEqual(drain(self.lib), 0)
        self.assertEqual(self.lib.boards.subject_view(1, "2000001")["state"], "none")
        self.assertEqual(len(self.fake.calls), 1)
        self.fake.blocked = False
        self.clock.now += libmeta.BACKOFF_S + 1
        self.lib.set_online(False)
        view = self.lib.boards.lists_view(1)
        self.assertFalse(view["online"] or any(r["loading"] for r in view["lists"]))
        self.assertEqual(drain(self.lib), 0)
        self.assertEqual(len(self.fake.calls), 1, "online details off: not one request")

    def test_comments_and_recommendations(self):
        serve_subject(self.fake, "2000001")
        boards = self.lib.boards
        self.assertEqual(boards.subject_view(1, "2000001"), {"ok": True, "state": "loading", "comments": [], "recs": []})
        self.assertEqual(self.fake.calls, [])
        self.assertEqual(drain(self.lib), 1)
        self.assertEqual(len(self.fake.calls), 2, "comments and recommendations")
        view = boards.subject_view(1, "2000001")
        self.assertEqual((view["state"], [c["author"] for c in view["comments"]]), ("ready", ["如小果", "乙"]))
        recs = {c["id"]: c for c in view["recs"]}
        self.assertNotIn("2000001", recs, "not itself")
        self.assertEqual(recs["2000002"]["have"], {"show": self.keys["show"][1:], "season": 2})
        self.assertNotIn("have", recs["4000009"])
        self.assertNotIn("doubanio", json.dumps(view, ensure_ascii=False))
        self.clock.now += libdouban.SUBJECT_TTL_S - 60
        boards.subject_view(1, "2000001")
        self.assertEqual(drain(self.lib), 0, "cached for a day")
        self.assertEqual(boards.subject_view(1, "123")["state"], "loading")
        drain(self.lib)                                  # 404 from the fake: Failed
        self.assertEqual(boards.subject_view(1, "123")["state"], "none", "a failure hides the block")

    def test_subjects_are_bounded(self):
        with mock.patch.object(libdouban, "SUBJECTS_MAX", 2):
            for did in ("11", "12", "13"):
                serve_subject(self.fake, did)
                self.lib.boards.subject_view(1, did)
                drain(self.lib)
                self.clock.now += 5
            self.assertEqual(sorted(self.lib.boards._subjects), ["12", "13"])

    def test_queue_is_bounded(self):
        with mock.patch.object(libdouban, "QUEUE_MAX", 3):
            for did in range(10, 20):
                self.lib.boards.subject_view(1, str(did))
            self.assertEqual(len(self.lib.boards._queue), 3)

    def test_posters_only_for_known_ids_and_once(self):
        boards = self.lib.boards
        handler = Handler()
        boards.serve_poster(handler, "2000001")
        self.assertEqual(handler.sent[-1][0], 404, "not listed yet")
        self.assertEqual(self.fake.calls, [])
        boards.lists_view(1)
        drain(self.lib)
        calls = len(self.fake.calls)
        boards.serve_poster(handler, "2000001")
        self.assertEqual(handler.sent[-1], (200, JPEG, "image/jpeg"))
        self.assertEqual(self.fake.urls()[-1], IMG.format(n=3, p="2000001"))
        self.assertEqual(self.fake.calls[-1][1]["Referer"], libmeta.REFERER)
        boards.serve_poster(handler, "2000001")
        self.assertEqual(handler.sent[-1][0], 200)
        self.assertEqual(len(self.fake.calls), calls + 1, "the second time from disk")
        boards.serve_poster(handler, "4000002")
        self.assertEqual(handler.sent[-1][0], 404, "a foreign image host is never fetched")
        self.assertEqual(len(self.fake.calls), calls + 1)
        with mock.patch.object(libdouban, "IMG_FILES_MAX", 1):
            boards.serve_poster(handler, "4000001")
        self.assertEqual(sorted(p.name for p in boards.img_dir.iterdir()), ["4000001.jpg"], "bounded on disk")


# ---------------------------------------------------------------- wishlist

class WishTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = Clock()
        self.lib, self.fake, self.keys = build(Path(self.tmp.name), self.clock)
        self.lib.boards.lists_view(1)
        drain(self.lib)
        self.boards = self.lib.boards

    def tearDown(self):
        self.tmp.cleanup()

    def relay(self, user):
        return (types.SimpleNamespace(library=self.lib, forget_home=lambda _c: None),
                types.SimpleNamespace(guest=False, user=user))

    def test_add_remove_and_isolation(self):
        answer = self.boards.wish(1, {"id": "4000001", "on": True})
        self.assertTrue(answer["ok"], answer)
        self.assertEqual([w["id"] for w in answer["wish"]], ["4000001"])
        self.assertNotIn("doubanio", json.dumps(answer))
        self.assertTrue(self.boards.wish(1, {"id": "4000003", "on": True})["ok"])
        self.assertEqual([w["id"] for w in self.boards.wish_view(1)], ["4000003", "4000001"], "newest first")
        self.assertEqual(self.boards.wish_view(2), [], "each person has their own")
        view = self.boards.lists_view(1)
        self.assertTrue(next(c for r in view["lists"] for c in r["items"] if c["id"] == "4000001")["wished"])
        self.assertFalse(any(c.get("wished") for r in self.boards.lists_view(2)["lists"] for c in r["items"]))
        self.assertEqual(stat.S_IMODE(os.stat(self.boards.wishes.path).st_mode), 0o600)
        self.assertTrue(self.boards.wish(1, {"id": "4000001", "on": False})["ok"])
        self.assertEqual([w["id"] for w in self.boards.wish_view(1)], ["4000003"])
        reborn = libdouban.Wishes(self.boards.wishes.path, self.clock)
        self.assertEqual([w["id"] for w in reborn.rows(1)], ["4000003"], "kept across a restart")

    def test_refusals_and_bounds(self):
        self.assertFalse(self.boards.wish(1, {"id": "999999", "on": True})["ok"], "only what the relay listed")
        self.assertFalse(self.boards.wish(1, {"id": "2000001", "on": True})["ok"], "already in the library")
        self.assertFalse(self.boards.wish(1, {"id": "x", "on": True})["ok"])
        self.assertFalse(self.boards.wish(1, {"id": "4000001"})["ok"])
        self.assertFalse(self.boards.wish(1, {"id": "4000001", "on": True, "img": "https://evil.example.com"})
                         .get("wish", [{}])[0].get("art", "").startswith("http"))
        with mock.patch.object(libdouban, "WISH_MAX", 1):
            self.assertFalse(self.boards.wish(1, {"id": "4000003", "on": True})["ok"])
        with mock.patch.object(libdouban, "WISH_USERS_MAX", 1):
            self.assertFalse(self.boards.wish(2, {"id": "4000003", "on": True})["ok"])

    def test_a_wish_that_arrives_shows_on_home_until_opened(self):
        self.assertTrue(self.boards.wish(1, {"id": "4000001", "on": True})["ok"])
        self.assertTrue(self.boards.wish(2, {"id": "4000001", "on": True})["ok"])
        relay, caller = self.relay(1)
        self.assertEqual(libdouban.home_rows(relay, caller), [])
        # A scan finds the film and the matcher (or an admin) matches it to the wished Douban id.
        lib_base.touch(Path(self.tmp.name) / "videos" / "新片 (2026).mp4", lib_base.MP4)
        self.lib.scan()
        key = next(k for k, t in self.lib.titles().items() if t["title"] == "新片")
        self.lib.meta.put(key, {"state": "matched", "id": "4000001", "title": "新片", "q": "", "at": 1})
        rows = libdouban.home_rows(relay, caller)
        self.assertEqual([(r["wish"], r["have"], r["title"]) for r in rows], [("4000001", {"item": key[1:]}, "新片")])
        self.assertEqual(rows[0]["art"]["url"], "api/library/douban/poster/4000001")
        wish = self.boards.wish_view(1)[0]
        self.assertEqual(wish["have"], {"item": key[1:]}, "marked 「片库里有了」 in the wishlist")
        self.assertTrue(self.boards.wish(1, {"id": "4000001", "seen": True})["ok"])
        self.assertEqual(libdouban.home_rows(relay, caller), [], "opened: no more reminder")
        relay2, caller2 = self.relay(2)
        self.assertEqual(len(libdouban.home_rows(relay2, caller2)), 1, "someone else's reminder is their own")
        self.clock.now += libdouban.WISH_NEW_S + 1
        self.assertEqual(libdouban.home_rows(relay2, caller2), [], "an old arrival stops being news")
        guest = types.SimpleNamespace(guest=True, user=None)
        self.assertEqual(libdouban.home_rows(relay2, guest), [])


# ---------------------------------------------------------------- endpoints

class EndpointTests(unittest.TestCase):
    selfhost = True
    setUp_relay = watch_base.RelayWatchTests.setUp
    tearDown_relay = watch_base.RelayWatchTests.tearDown
    online = watch_base.RelayWatchTests.online
    request = lib_base.ApiTests.request
    call = lib_base.ApiTests.call

    def setUp(self):
        self.setUp_relay()
        self.clock = Clock()
        self.relay.library, self.fake, self.keys = build(Path(self.tmp.name), self.clock)
        serve_subject(self.fake, "2000001")

    def tearDown(self):
        self.tearDown_relay()

    def test_family_browses_and_keeps_their_own_wishes(self):
        status, view = self.call("alice", "GET", "/tv/api/library/douban")
        self.assertEqual(status, 200)
        self.assertTrue(all(r["loading"] for r in view["lists"]))
        drain(self.relay.library)
        view = self.call("alice", "GET", "/tv/api/library/douban")[1]
        self.assertEqual(len(view["lists"][0]["items"]), 3)
        status, response, body = self.request("GET", "/tv/api/library/douban/poster/2000001", "alice")
        self.assertEqual((status, response.getheader("Content-Type"), body), (200, "image/jpeg", JPEG))
        self.assertEqual(self.call("alice", "GET", "/tv/api/library/douban/subject/2000001")[1]["state"], "loading")
        drain(self.relay.library)
        self.assertEqual(self.call("alice", "GET", "/tv/api/library/douban/subject/2000001")[1]["state"], "ready")
        self.assertEqual(self.request("GET", "/tv/api/library/douban/subject/abc", "alice")[0], 404)
        answer = self.call("alice", "POST", "/tv/api/library/wish", {"id": "4000001", "on": True})[1]
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(self.call("bob", "GET", "/tv/api/library/douban")[1]["wish"], [])
        self.assertEqual([w["id"] for w in self.call("alice", "GET", "/tv/api/library/douban")[1]["wish"]], ["4000001"])
        home = self.call("alice", "GET", "/tv/api/home")[1]
        self.assertEqual(home["library_wished"], [])

    def test_guests_and_signed_out(self):
        drain(self.relay.library)
        for who in ("guest", None):
            for path in ("/tv/api/library/douban", "/tv/api/library/douban/subject/2000001",
                         "/tv/api/library/douban/poster/2000001"):
                self.assertEqual(self.request("GET", path, who)[0], 404, (who, path))
        status, answer = self.call("guest", "POST", "/tv/api/library/wish", {"id": "4000001", "on": True})
        self.assertEqual((status, answer["ok"]), (200, False))
        self.assertEqual(self.call(None, "POST", "/tv/api/library/wish", {"id": "4000001", "on": True})[0], 404)
        self.relay.library = None                       # the cloud relay has no library
        self.assertEqual(self.request("GET", "/tv/api/library/douban", "alice")[0], 404)
        self.assertEqual(self.call("alice", "POST", "/tv/api/library/wish", {"id": "4000001", "on": True})[0], 404)



@unittest.skipUnless(shutil.which("node"), "node is not installed")
class FilterJsTests(unittest.TestCase):
    """My library's sort and filter helpers in the console (rating, newest, title; genre and region) as pure JS."""

    def test_pure_functions(self):
        result = subprocess.run(["node", str(Path(__file__).resolve().parent / "console_libfilter_test.mjs")],
                                capture_output=True, text=True, encoding="utf-8", timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
