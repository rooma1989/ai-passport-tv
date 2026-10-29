"""The library's second source of online details, TMDB (server/relay/libmeta.py Tmdb and its use in library.py).

No network and no real key: every TMDB answer is a small hand-written fixture with the shape documented
on developer.themoviedb.org (search/movie and search/tv: {page, results: [{id, title|name,
original_title|original_name, release_date|first_air_date, poster_path}]}; movie and tv details with
append_to_response=credits: genres [{id, name}], overview, number_of_episodes, created_by, origin_country,
credits {cast: [{name, order}], crew: [{name, job}]}), served by a fake fetch that records each request.
The keys used here are obviously fake strings in the documented formats (32 hex characters, or a
three-part token starting with "eyJ").

What is checked: the key is stored in the data directory's secrets/tmdb-key with mode 600 (folder 700),
is never in a page answer, an audit row or a log line, wrong-looking pastes are refused and an empty
paste clears it; requests carry the key as a Bearer header (token) or api_key (v3 key) and image requests
carry none; the fallback order (Douban first; TMDB only when Douban has no confident match, is blocked or
cannot be reached; a title Douban already said no to is asked of TMDB alone once a key is set; a TMDB
match made while Douban was blocked asks Douban again later and Douban wins); the matching rules (same
normalized title or original title, same year, same kind, exactly one; the English title through an
en-US search); posters (checked to be images, capped, stored once as tmdb-<kind>-<id>.<ext>, served
from the relay); 401 marks the key bad, 429 and repeated connection failures back off; the online-details
switch (nothing sent anywhere when off); manual search lists both sources labelled, TMDB thumbnails come
through the relay and a TMDB candidate can be picked; the base-address override only accepts loopback.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import unittest
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from server.relay import libmeta, library

import test_relay_library as lib_base
import test_relay_libmeta as douban_base
import test_relay_watch as watch_base

TOKEN = "eyJhbGciOiJIUzI1NiJ9.eyJhdWQiOiJmYWtlLWZvci10ZXN0cyJ9.ZmFrZS1zaWduYXR1cmUtZm9yLXRlc3Rz"
V3_KEY = "0123456789abcdef0123456789abcdef"
JPEG = douban_base.JPEG
PNG = douban_base.PNG

SEARCH_TV = {"page": 1, "total_pages": 1, "total_results": 2, "results": [
    {"adult": False, "id": 90001, "name": "苏里南", "original_name": "수리남", "first_air_date": "2022-09-09",
     "poster_path": "/surinamPoster1.jpg", "overview": "", "genre_ids": [18], "origin_country": ["KR"]},
    {"adult": False, "id": 90002, "name": "苏里南的夏天", "original_name": "苏里南的夏天", "first_air_date": "2022-01-01",
     "poster_path": None}]}
SEARCH_TV_EN = {"page": 1, "results": [
    {"id": 90001, "name": "Narco-Saints", "original_name": "수리남", "first_air_date": "2022-09-09",
     "poster_path": "/surinamPoster1.jpg"}]}
DETAILS_TV = {
    "id": 90001, "name": "苏里南", "original_name": "수리남", "first_air_date": "2022-09-09",
    "genres": [{"id": 18, "name": "剧情"}, {"id": 80, "name": "犯罪"}], "overview": "一个商人接下了政府的秘密任务。",
    "poster_path": "/surinamPoster1.jpg", "number_of_episodes": 6, "number_of_seasons": 1,
    "created_by": [{"id": 1, "credit_id": "x", "name": "Yoon Jong-bin", "gender": 2, "profile_path": None}],
    "origin_country": ["KR"], "production_countries": [{"iso_3166_1": "KR", "name": "South Korea"}],
    "vote_average": 7.84, "vote_count": 321,
    "credits": {"cast": [{"name": f"Actor {i}", "character": "x", "order": 11 - i} for i in range(12)],
                "crew": [{"name": "Someone", "job": "Writer", "department": "Writing"}]}}
SEARCH_MOVIE = {"page": 1, "results": [
    {"id": 70001, "title": "十间敢死队", "original_title": "Ten Brave", "release_date": "2026-02-01",
     "poster_path": "/tenBrave77.jpg", "adult": False}]}
DETAILS_MOVIE = {
    "id": 70001, "title": "十间敢死队", "original_title": "Ten Brave", "release_date": "2026-02-01",
    "genres": [{"id": 28, "name": "动作"}], "overview": "十个人。", "poster_path": "/tenBrave77.jpg",
    "vote_average": 6.1, "vote_count": 12, "production_countries": [{"iso_3166_1": "CN", "name": "China"}],
    "credits": {"cast": [{"name": "Lead", "order": 0}], "crew": [{"name": "Dir One", "job": "Director"},
                                                                 {"name": "Not Dir", "job": "Producer"}]}}


class FakeTmdb:
    """fetch(url, headers, cap, timeout, proxy) → (状态, 内容)：按路径和查询回答，记下每个请求。"""

    def __init__(self):
        self.calls: list[tuple[str, dict, str]] = []
        self.answers: dict[tuple, tuple[int, bytes]] = {}
        self.down = False
        # 每个请求发出时豆瓣已经收到了几个请求（看先后）。
        self.douban = None
        self.douban_before: list[int] = []

    def add(self, path: str, body, status: int = 200, **query) -> None:
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        self.answers[(path, tuple(sorted(query.items())))] = (status, data)

    def __call__(self, url, headers, cap, timeout, proxy):
        self.calls.append((url, dict(headers), proxy))
        self.douban_before.append(len(self.douban.calls) if self.douban is not None else -1)
        if self.down:
            raise libmeta.Failed("连不上 TMDB（TimeoutError）")
        parts = urllib.parse.urlsplit(url)
        query = {k: v for k, v in urllib.parse.parse_qsl(parts.query)
                 if k not in ("api_key", "include_adult", "page")}
        for (path, want), answer in self.answers.items():
            if parts.path.endswith(path) and all(query.get(k) == v for k, v in want):
                return answer
        if "/search/" in parts.path:
            return 200, b'{"page":1,"results":[]}'
        return 404, b""

    def paths(self) -> list[str]:
        return [urllib.parse.urlsplit(u).path for u, _, _ in self.calls]


def surinam_tmdb(fake: FakeTmdb) -> None:
    fake.add("/3/search/tv", SEARCH_TV, query="苏里南", language="zh-CN", first_air_date_year="2022")
    fake.add("/3/tv/90001", DETAILS_TV, language="zh-CN", append_to_response="credits")
    fake.add("/t/p/w500/surinamPoster1.jpg", JPEG)
    fake.add("/t/p/w185/surinamPoster1.jpg", JPEG)


def wire_tmdb(lib: library.Library, fake: FakeTmdb, clock, logs: list | None = None, key: str = TOKEN) -> FakeTmdb:
    lib.tmdb = libmeta.Tmdb(lib.dir / "secrets", fetch=fake, clock=clock, sleep=lambda _s: None,
                            log=(logs.append if logs is not None else lambda _m: None), env={})
    if key:
        assert lib.tmdb.set_key(key)["ok"]
    return fake


class ParseTests(unittest.TestCase):
    def test_search_and_details(self):
        found = libmeta.parse_tmdb_search(json.dumps(SEARCH_TV), "tv")
        self.assertEqual([(c["id"], c["tid"], c["src"], c["year"], c["type"]) for c in found],
                         [("tmdb:tv:90001", "90001", "tmdb", 2022, "tv"), ("tmdb:tv:90002", "90002", "tmdb", 2022, "tv")])
        self.assertEqual((found[0]["sub_title"], found[0]["img"], found[1]["sub_title"], found[1]["img"]),
                         ("수리남", "/surinamPoster1.jpg", "", ""))
        bad = {"results": [{"id": "1", "name": "x"}, {"id": True, "name": "x"}, {"id": 5, "name": ""},
                           {"id": 6, "name": "成人", "adult": True},
                           {"id": 7, "name": "ok", "poster_path": "https://evil.example.com/x.jpg"}]}
        self.assertEqual([(c["tid"], c["img"]) for c in libmeta.parse_tmdb_search(json.dumps(bad), "tv")], [("7", "")])
        self.assertEqual(libmeta.parse_tmdb_search(b"<html>", "movie"), [])
        tv = libmeta.parse_tmdb_details(json.dumps(DETAILS_TV), "tv")
        self.assertEqual((tv["id"], tv["src"], tv["title"], tv["original"], tv["year"], tv["episodes"], tv["rating"]),
                         ("90001", "tmdb", "苏里南", "수리남", 2022, 6, 7.8))
        self.assertEqual((tv["genres"], tv["countries"], tv["directors"]), (["剧情", "犯罪"], ["韩国"], ["Yoon Jong-bin"]))
        self.assertEqual(tv["actors"][:2], ["Actor 11", "Actor 10"], "cast in billing order")
        self.assertEqual(len(tv["actors"]), libmeta.PEOPLE_MAX)
        movie = libmeta.parse_tmdb_details(json.dumps(DETAILS_MOVIE), "movie")
        self.assertEqual((movie["directors"], movie["countries"], movie["episodes"], movie["aka"]),
                         (["Dir One"], ["中国大陆"], None, ["Ten Brave"]))
        self.assertIsNone(libmeta.parse_tmdb_details(b"{}", "movie"))

    def test_key_shapes_and_pick_ids(self):
        self.assertTrue(libmeta.tmdb_key_ok(TOKEN) and libmeta.tmdb_key_ok(V3_KEY))
        for bad in ("", "abc", V3_KEY.upper(), "https://api.themoviedb.org/3?api_key=" + V3_KEY, "eyJ.a.b", 5):
            self.assertFalse(libmeta.tmdb_key_ok(bad), bad)
        self.assertEqual(libmeta.tmdb_pick_id("tmdb:tv:90001"), ("tv", "90001"))
        for bad in ("90001", "tmdb:person:1", "tmdb:tv:../1", "tmdb:tv:"):
            self.assertIsNone(libmeta.tmdb_pick_id(bad))

    def test_base_override_only_loopback(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = libmeta.Tmdb(Path(tmp), env={"TV_TMDB_API": "http://127.0.0.1:18999/3",
                                                "TV_TMDB_IMAGE": "http://localhost:18999/t/p"})
            self.assertEqual((fake.api, fake.image_base), ("http://127.0.0.1:18999/3", "http://localhost:18999/t/p"))
            real = libmeta.Tmdb(Path(tmp), env={"TV_TMDB_API": "https://evil.example.com/3",
                                                "TV_TMDB_IMAGE": "http://10.0.0.1/t/p"})
            self.assertEqual((real.api, real.image_base), (libmeta.TMDB_API, libmeta.TMDB_IMAGE))


class KeyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.logs: list[str] = []
        self.now = 1_800_000_000.0
        self.lib = lib_base.quiet(Path(self.tmp.name) / "data", self.logs)
        self.fake = wire_tmdb(self.lib, FakeTmdb(), lambda: self.now, self.logs, key="")

    def tearDown(self):
        self.tmp.cleanup()

    def test_storage(self):
        self.assertEqual(self.lib.tmdb.state(), {"set": False, "error": ""})
        refused = self.lib.set_tmdb_key("my key is " + V3_KEY)
        self.assertFalse(refused["ok"])
        self.assertNotIn(V3_KEY, json.dumps(refused, ensure_ascii=False))
        answer = self.lib.set_tmdb_key(f"  {TOKEN}\n")
        self.assertEqual(answer, {"ok": True, "tmdb": {"set": True, "error": ""}})
        path = self.lib.dir / "secrets" / "tmdb-key"
        self.assertEqual(path.read_text().strip(), TOKEN)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(path.parent).st_mode), 0o700)
        for shown in (self.lib.view(), self.lib.admin_view()):
            self.assertNotIn(TOKEN[:20], json.dumps(shown, ensure_ascii=False))
        self.assertEqual(self.lib.admin_view()["tmdb"], {"set": True, "error": ""})
        self.assertTrue(self.lib.set_tmdb_key(V3_KEY)["ok"])
        self.assertTrue(self.lib.set_tmdb_key("")["ok"])
        self.assertFalse(path.exists())
        self.assertFalse(self.lib.tmdb.configured())
        self.assertFalse(any(TOKEN[:20] in m or V3_KEY in m for m in self.logs), self.logs)

    def test_how_the_key_is_sent(self):
        self.lib.set_tmdb_key(TOKEN)
        surinam_tmdb(self.fake)
        self.lib.tmdb.search("苏里南", "tv", 2022)
        self.lib.tmdb.image("/surinamPoster1.jpg")
        (api_url, api_headers, _), (img_url, img_headers, _) = self.fake.calls
        self.assertEqual(api_headers["Authorization"], "Bearer " + TOKEN)
        self.assertNotIn("api_key", api_url)
        self.assertNotIn("Authorization", img_headers)
        self.assertNotIn(TOKEN, img_url)
        self.lib.set_tmdb_key(V3_KEY)
        self.lib.tmdb.search("苏里南", "tv", 2022)
        url, headers, _ = self.fake.calls[-1]
        self.assertIn("api_key=" + V3_KEY, url)
        self.assertNotIn("Authorization", headers)
        # 「设置 → YouTube」的代理（attach 接上的 proxy()）交给每个请求。
        self.lib.tmdb.proxy = lambda: "socks5h://127.0.0.1:1080"
        self.lib.tmdb.search("苏里南", "tv")
        self.assertEqual(self.fake.calls[-1][2], "socks5h://127.0.0.1:1080")

    def test_fetch_failures_never_carry_the_address(self):
        import socket
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        with self.assertRaises(libmeta.Failed) as caught:
            libmeta.tmdb_fetch(f"http://127.0.0.1:{port}/3/search/tv?api_key={V3_KEY}", {}, 1000, 2, "")
        self.assertTrue(str(caught.exception).startswith("连不上 TMDB"))
        self.assertNotIn(V3_KEY, str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)
        with self.assertRaises(libmeta.Failed) as caught:
            libmeta.tmdb_fetch(f"https://127.0.0.1:{port}/3/x?api_key={V3_KEY}", {}, 1000, 2, "not a proxy")
        self.assertNotIn(V3_KEY, str(caught.exception))

    def test_bad_key_rate_limit_and_unreachable(self):
        self.lib.set_tmdb_key(TOKEN)
        self.fake.add("/3/search/tv", b"", status=401, query="x", language="zh-CN")
        with self.assertRaises(libmeta.Failed):
            self.lib.tmdb.search("x", "tv")
        self.assertFalse(self.lib.tmdb.on())
        self.assertEqual(self.lib.tmdb.state()["error"], "TMDB 不认这个密钥，重新粘贴一个")
        calls = len(self.fake.calls)
        with self.assertRaises(libmeta.Failed):
            self.lib.tmdb.search("y", "tv")
        self.assertEqual(len(self.fake.calls), calls, "a refused key is not sent again")
        self.lib.set_tmdb_key(TOKEN)
        self.assertTrue(self.lib.tmdb.on(), "pasting a key again clears the mark")
        self.fake.add("/3/search/tv", b"", status=429, query="z", language="zh-CN")
        with self.assertRaises(libmeta.Failed):
            self.lib.tmdb.search("z", "tv")
        self.assertFalse(self.lib.tmdb.on())
        self.now += libmeta.TMDB_BACKOFF_S + 1
        self.assertTrue(self.lib.tmdb.on())
        self.fake.down = True
        for _ in range(libmeta.TMDB_DOWN_AFTER):
            with self.assertRaises(libmeta.Failed):
                self.lib.tmdb.search("苏里南", "tv")
        self.assertFalse(self.lib.tmdb.on(), "rests after repeated timeouts")
        self.assertIn("暂时连不上", self.lib.tmdb.state()["error"])
        self.assertFalse(any(TOKEN[:20] in m for m in self.logs))

    def test_gap_between_requests(self):
        slept = []
        self.lib.set_tmdb_key(TOKEN)
        self.lib.tmdb.sleep = slept.append
        self.lib.tmdb.search("a", "tv")
        self.lib.tmdb.search("b", "movie")
        self.assertEqual(slept, [libmeta.TMDB_GAP_S])

    def test_poster_checks(self):
        self.lib.set_tmdb_key(TOKEN)
        self.fake.add("/t/p/w500/notImage1.jpg", b"<html>nope</html>")
        self.fake.add("/t/p/w500/hugePoster.jpg", JPEG + bytes(libmeta.POSTER_MAX))
        for path in ("/notImage1.jpg", "/hugePoster.jpg", "https://evil.example.com/a.jpg", "/../../etc.jpg"):
            with self.assertRaises(libmeta.Failed):
                self.lib.tmdb.image(path)


class FallbackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.root = base / "videos"
        for n in range(1, 3):
            lib_base.touch(self.root / douban_base.SURINAM / douban_base.EPISODE.format(n=n), lib_base.MP4)
        lib_base.touch(self.root / douban_base.MASKED, lib_base.MP4)
        lib_base.touch(self.root / douban_base.PROMO, lib_base.MP4)
        self.now = 1_800_000_000.0
        self.logs: list[str] = []
        self.lib = lib_base.quiet(base / "data", self.logs)
        self.douban = douban_base.wire(self.lib, douban_base.FakeDouban(), lambda: self.now)
        self.tmdb = wire_tmdb(self.lib, FakeTmdb(), lambda: self.now, self.logs)
        self.tmdb.douban = self.douban
        surinam_tmdb(self.tmdb)
        self.assertTrue(self.lib.add({"kind": "local", "path": str(self.root)})["ok"])

    def tearDown(self):
        self.tmp.cleanup()

    def run_all(self):
        steps = 0
        while self.lib.meta_step():
            steps += 1
            self.assertLess(steps, 20)
        return steps

    def shows(self):
        return {s["guess"]: s for s in self.lib.view()["shows"]}

    def cards(self):
        view = self.lib.view()
        return {c["guess"]: c for c in view["movies"] + view["videos"]}

    def test_douban_first_then_tmdb(self):
        self.run_all()
        show = self.shows()["苏里南"]
        self.assertEqual((show["title"], show["meta"]["src"], show["meta"]["rating"], show["meta"]["episodes"]),
                         ("苏里南", "tmdb", 7.8, 6))
        self.assertEqual(show["meta"]["plot"], "一个商人接下了政府的秘密任务。")
        self.assertEqual(show["meta"]["directors"], ["Yoon Jong-bin"])
        douban_first = [u for u, _ in self.douban.calls].index(
            libmeta.SUGGEST_URL + urllib.parse.quote("苏里南")) + 1
        tmdb_first = next(i for i, (u, _, _) in enumerate(self.tmdb.calls)
                          if "/search/tv" in u and "query=%E8%8B%8F%E9%87%8C%E5%8D%97" in u)
        self.assertGreaterEqual(self.tmdb.douban_before[tmdb_first], douban_first, "Douban is asked before TMDB")
        poster = self.lib.art_dir / "tmdb-tv-90001.jpg"
        self.assertEqual(poster.read_bytes(), JPEG)
        self.assertTrue(show["art"].startswith(f"api/library/meta-art/{show['key']}?v=t90001"))
        self.assertNotIn("surinamPoster1", json.dumps(self.lib.view()), "poster paths stay in the relay")
        sent = " ".join(u for u, _, _ in self.tmdb.calls)
        self.assertNotIn(urllib.parse.quote("夸克"), sent, "an 'other video' title is never sent")
        self.assertNotIn("%E5%A4%B8%E5%85%8B", sent)
        # 重新扫描、再跑一遍：不再查。
        calls = (len(self.douban.calls), len(self.tmdb.calls))
        self.lib.scan()
        self.assertEqual(self.run_all(), 0)
        self.assertEqual((len(self.douban.calls), len(self.tmdb.calls)), calls)
        self.assertTrue(any("对上了 TMDB" in m for m in self.logs))

    def test_douban_match_never_asks_tmdb(self):
        douban_base.surinam(self.douban)
        self.tmdb.answers.clear()
        self.run_all()
        self.assertEqual(self.shows()["苏里南"]["meta"]["src"], "douban")
        self.assertNotIn("/3/search/tv", self.tmdb.paths(), "the show matched on Douban is never sent to TMDB")
        # 敢死队豆瓣没对上：只有它去问了 TMDB。
        self.assertEqual(self.tmdb.paths(), ["/3/search/movie"])

    def test_matching_rules(self):
        # 年份不一样、种类不一样、两个一样像的：都不认。
        self.tmdb.answers.clear()
        self.tmdb.add("/3/search/tv", {"results": [
            {"id": 1, "name": "苏里南", "first_air_date": "2022-01-01"},
            {"id": 2, "name": "苏里南", "first_air_date": "2022-05-01"}]}, query="苏里南", language="zh-CN")
        self.tmdb.add("/3/search/movie", {"results": [
            {"id": 3, "title": "10间敢死队", "release_date": "2025-01-01"}]}, query="10间敢死队", language="zh-CN")
        self.run_all()
        self.assertEqual(self.shows()["苏里南"]["meta"]["state"], "miss")
        self.assertEqual(self.cards()["10间敢死队"]["meta"]["state"], "miss")
        self.assertIn("/3/search/tv", self.tmdb.paths())
        # 英文名再查一次（en-US）：标题是英文名、年份种类都对、只有一个才认。
        self.tmdb.add("/3/search/tv", SEARCH_TV_EN, query="Narco-Saints", language="en-US", first_air_date_year="2022")
        self.tmdb.add("/3/tv/90001", DETAILS_TV, language="zh-CN", append_to_response="credits")
        self.lib.refresh(self.shows()["苏里南"]["key"])
        show = self.shows()["苏里南"]
        self.assertEqual((show["title"], show["meta"]["src"], show["meta"]["id"]), ("苏里南", "tmdb", "90001"))
        queries = [dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(u).query)) for u, _, _ in self.tmdb.calls]
        self.assertIn({"query": "Narco-Saints", "language": "en-US", "include_adult": "false", "page": "1",
                       "first_air_date_year": "2022"}, queries)

    def test_douban_blocked_uses_tmdb_and_douban_wins_later(self):
        self.douban.blocked = True
        self.run_all()
        show = self.shows()["苏里南"]
        self.assertEqual(show["meta"]["src"], "tmdb")
        self.assertTrue(self.lib.douban.blocked())
        self.assertTrue(self.lib.meta.get(show["key"])["douban_skipped"])
        self.assertEqual(self.lib.view()["pending"], 0, "TMDB answered for both while Douban was blocked")
        # 豆瓣不拦了、过了几天：再问一次豆瓣，豆瓣对上了就换成豆瓣的。
        self.douban.blocked = False
        douban_base.surinam(self.douban)
        self.now += libmeta.MISS_RETRY_S + libmeta.BACKOFF_S
        self.assertIn(show["key"], self.lib.meta_due())
        self.run_all()
        self.assertEqual(self.shows()["苏里南"]["meta"]["src"], "douban")
        self.assertEqual(self.shows()["苏里南"]["meta"]["rating"], 6.9)

    def test_blocked_douban_without_tmdb_waits(self):
        self.lib.set_tmdb_key("")
        self.douban.blocked = True
        self.run_all()
        self.assertEqual(self.tmdb.calls, [])
        self.assertIsNone(self.shows()["苏里南"]["meta"])
        self.assertFalse(self.lib.meta_step(), "nothing while Douban is blocked and TMDB is off")

    def test_key_set_later_asks_tmdb_only(self):
        self.lib.set_tmdb_key("")
        self.run_all()
        self.assertEqual(self.shows()["苏里南"]["meta"]["state"], "miss")
        self.assertEqual(self.tmdb.calls, [])
        douban_calls = len(self.douban.calls)
        self.lib.set_tmdb_key(TOKEN)
        self.assertEqual(len(self.lib.meta_due()), 2)
        self.run_all()
        self.assertEqual(len(self.douban.calls), douban_calls, "Douban's fresh 'no' is not asked again")
        self.assertEqual(self.shows()["苏里南"]["meta"]["src"], "tmdb")
        masked = self.lib.meta.get(self.cards()["10间敢死队"]["key"])
        self.assertEqual((masked["state"], masked["tmdb"]), ("miss", True))
        self.assertEqual(self.lib.meta_due(), [])

    def test_unreachable_tmdb_retries_later(self):
        self.tmdb.down = True
        self.run_all()
        entry = self.lib.meta.get(self.shows()["苏里南"]["key"])
        self.assertEqual((entry["state"], entry["tmdb_failed"]), ("miss", True))
        self.tmdb.down = False
        self.assertEqual(self.lib.meta_due(), [])
        self.now += libmeta.PARTIAL_RETRY_S + 1
        self.run_all()
        self.assertEqual(self.shows()["苏里南"]["meta"]["src"], "tmdb")

    def test_details_cut_short_are_completed_from_tmdb(self):
        self.tmdb.answers.pop(("/3/tv/90001", (("append_to_response", "credits"), ("language", "zh-CN"))))
        self.run_all()
        show = self.shows()["苏里南"]
        self.assertEqual((show["title"], show["meta"]["partial"], show["meta"]["src"]), ("苏里南", True, "tmdb"))
        surinam_tmdb(self.tmdb)
        self.now += libmeta.PARTIAL_RETRY_S + 1
        self.run_all()
        show = self.shows()["苏里南"]
        self.assertEqual((show["meta"]["partial"], show["meta"]["rating"]), (False, 7.8))

    def test_offline_sends_nothing_anywhere(self):
        self.lib.set_online(False)
        self.assertEqual(self.run_all(), 0)
        self.assertFalse(self.lib.search("苏里南")["ok"])
        self.assertFalse(self.lib.match(self.shows()["苏里南"]["key"], "tmdb:tv:90001")["ok"])
        self.assertEqual((self.douban.calls, self.tmdb.calls), ([], []))

    def test_manual_search_labels_and_pick(self):
        self.tmdb.add("/3/search/tv", SEARCH_TV, query="苏里南", language="zh-CN")
        self.tmdb.add("/3/search/movie", {"results": [{"id": 70009, "title": "苏里南", "release_date": "1999-01-01",
                                                       "poster_path": "/oldMovie9.png"}]}, query="苏里南", language="zh-CN")
        douban_base.surinam(self.douban)
        found = self.lib.search("苏里南")
        self.assertTrue(found["ok"], found)
        labels = [(c["src"], c["id"]) for c in found["candidates"]]
        self.assertEqual(labels[:2], [("douban", "1000001"), ("douban", "1000002")], "Douban's first")
        self.assertEqual(labels[2:], [("tmdb", "tmdb:tv:90001"), ("tmdb", "tmdb:movie:70009"), ("tmdb", "tmdb:tv:90002")])
        self.assertEqual(found["candidates"][2]["thumb"], "api/admin/library/tmdb/thumb/tv-90001")
        self.assertEqual(found["candidates"][4]["thumb"], "")
        self.assertNotIn("surinamPoster1", json.dumps(found))
        self.assertEqual(self.lib.tmdb.thumb("tmdb:tv:90001"), ("jpg", JPEG))
        self.assertIsNone(self.lib.tmdb.thumb("tmdb:tv:12345"), "only candidates just seen")
        key = self.shows()["苏里南"]["key"]
        answer = self.lib.match(key, "tmdb:tv:90001")
        self.assertTrue(answer["ok"], answer)
        self.assertEqual((answer["meta"]["src"], answer["meta"]["manual"], answer["meta"]["id"]), ("tmdb", True, "90001"))
        self.assertEqual(self.lib.meta_due(), [self.cards()["10间敢死队"]["key"]], "manual choice is sticky")
        self.assertFalse(self.lib.match(key, "tmdb:person:1")["ok"])
        # 豆瓣拦着：只列 TMDB 的，说一句。
        self.douban.blocked = True
        self.lib.douban.blocked_until = self.now + 100
        found = self.lib.search("苏里南")
        self.assertTrue(found["ok"])
        self.assertEqual({c["src"] for c in found["candidates"]}, {"tmdb"})
        self.assertIn("下面只有 TMDB 的", found["notes"][0])
        # 没填 TMDB 的密钥：只有豆瓣，不往 TMDB 发。
        self.lib.set_tmdb_key("")
        calls = len(self.tmdb.calls)
        self.assertFalse(self.lib.search("苏里南")["ok"])
        self.assertEqual(len(self.tmdb.calls), calls)
        self.assertFalse(self.lib.match(key, "tmdb:tv:90001")["ok"])


class EndpointTests(unittest.TestCase):
    selfhost = True
    setUp_relay = watch_base.RelayWatchTests.setUp
    tearDown_relay = watch_base.RelayWatchTests.tearDown
    online = watch_base.RelayWatchTests.online
    request = lib_base.ApiTests.request
    call = lib_base.ApiTests.call

    def setUp(self):
        self.setUp_relay()
        self.cookies["admin"] = "tvs=" + self.accounts.new_session(self.admin, "pc")
        self.root = Path(self.tmp.name) / "videos"
        lib_base.touch(self.root / douban_base.SURINAM / douban_base.EPISODE.format(n=1), lib_base.MP4)
        self.logs: list[str] = []
        self.relay.library = lib_base.quiet(Path(self.accounts.path).parent, self.logs)
        self.douban = douban_base.wire(self.relay.library, douban_base.FakeDouban())
        self.tmdb = wire_tmdb(self.relay.library, FakeTmdb(), lambda: 1_800_000_000.0, self.logs, key="")
        surinam_tmdb(self.tmdb)
        self.tmdb.add("/3/search/tv", SEARCH_TV, query="苏里南", language="zh-CN")
        self.assertTrue(self.relay.library.add({"kind": "local", "path": str(self.root)})["ok"])

    def tearDown(self):
        self.tearDown_relay()

    def test_key_endpoint_and_thumbs(self):
        path = "/tv/api/admin/library/settings"
        for who in ("alice", "guest", None):
            self.assertEqual(self.call(who, "POST", path, {"tmdb_key": TOKEN})[0], 404, who)
        self.assertFalse(self.relay.library.tmdb.configured())
        self.assertEqual(self.call("admin", "POST", path, {"tmdb_key": 5})[0], 400)
        status, _, raw = self.request("POST", path, "admin", {"tmdb_key": TOKEN})
        self.assertEqual(status, 200)
        self.assertNotIn(TOKEN[:20].encode(), raw)
        self.assertEqual(json.loads(raw), {"ok": True, "tmdb": {"set": True, "error": ""}})
        view = self.call("admin", "GET", "/tv/api/library")[1]
        self.assertEqual(view["tmdb"], {"set": True, "error": ""})
        self.assertNotIn("tmdb", self.call("alice", "GET", "/tv/api/library")[1], "family members don't see the setting")
        _, _, raw = self.request("GET", "/tv/api/admin/library", "admin")
        self.assertNotIn(TOKEN[:20].encode(), raw)
        rows = [r for r in self.accounts.audit_rows(100) if r["action"] == "library.settings"]
        self.assertEqual(len(rows), 1)
        self.assertNotIn(TOKEN[:20], json.dumps(rows, ensure_ascii=False, default=str))
        found = self.call("admin", "GET", "/tv/api/admin/library/douban?q=%E8%8B%8F%E9%87%8C%E5%8D%97")[1]
        tmdb_rows = [c for c in found["candidates"] if c["src"] == "tmdb"]
        self.assertEqual(tmdb_rows[0]["id"], "tmdb:tv:90001")
        thumb = "/tv/" + tmdb_rows[0]["thumb"]
        self.assertEqual(self.request("GET", thumb, "alice")[0], 404)
        status, response, body = self.request("GET", thumb, "admin")
        self.assertEqual((status, response.getheader("Content-Type"), body), (200, "image/jpeg", JPEG))
        self.assertEqual(self.request("GET", "/tv/api/admin/library/tmdb/thumb/tv-1", "admin")[0], 404)
        key = self.call("admin", "GET", "/tv/api/library")[1]["shows"][0]["key"]
        answer = self.call("admin", "POST", "/tv/api/admin/library/match", {"key": key, "id": "tmdb:tv:90001"})[1]
        self.assertEqual((answer["ok"], answer["meta"]["src"]), (True, "tmdb"))
        show = self.call("alice", "GET", "/tv/api/library")[1]["shows"][0]
        status, response, body = self.request("GET", "/tv/" + show["art"].split("?")[0], "alice")
        self.assertEqual((status, body), (200, JPEG))
        self.assertTrue(self.call("admin", "POST", path, {"tmdb_key": ""})[1]["ok"])
        self.assertFalse((Path(self.accounts.path).parent / "secrets" / "tmdb-key").exists())
        self.assertFalse(any(TOKEN[:20] in m for m in self.logs))


if __name__ == "__main__":
    unittest.main()
