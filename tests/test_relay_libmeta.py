"""My library's online details from Douban (server/relay/libmeta.py and its use in library.py).

No network: every Douban answer here is a small synthetic fixture written by hand with the same
structure as the real one (the suggest JSON, the mobile subject JSON, a subject page with its
ld+json and info block), served by a fake fetch that records each request.

What is checked: the owner's real paths parse into what Douban knows (「苏.里.南 (2022) 4K…」 with
English episode files → the show 苏里南, 2022, alt title Narco-Saints; a junk file name in an
obfuscated folder → the folder's title and year; the Quark promo clip stays an "other video");
suggest, mobile and subject-page parsing (TV detected from the episode count, ld+json with raw
newlines, image addresses only from doubanio.com); the matching rules (exact normalized title or
original title, year and type must agree, ambiguity refused, the English title accepted through
the subject's aka); politeness (one request at a time, at least GAP_S apart) and the hour-long
back-off on 403, 429, a sec.douban.com redirect or the "abnormal request" page, during which
nothing is sent; posters are checked to be images and capped; the background matcher only
sends titles of movies and shows (never "other videos"), stores misses and retries them later,
keeps details that were cut short by a block as partial and completes them later; manual
choices and "do not match" are never overwritten by the automatic matcher; a local .nfo title
and a local poster win over Douban; with online details off nothing is sent; the endpoints:
family members see the details and the stored poster, guests and signed-out callers get 404,
and only admins search, pick, clear, refresh and switch the setting (audited).
"""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from server.relay import libmeta, library

import test_relay_library as lib_base
import test_relay_watch as watch_base

SURINAM = "夸克/来自：分享/苏.里.南 (2022) 4K.HDR.NF高码版 内封精修官译简中字幕 收藏版片源"
EPISODE = "Narco-Saints.S01E0{n}.2160p.NF.WEB-DL.DDP.5.1.Atmos.HDR.H.265-年糕.mkv"
MASKED = "夸克/来自：分享/10J间G敢S死D队2026/4K HQ SDR 高码率 DDP DTS.mp4"
PROMO = "夸克/夸克网盘能干什么.mp4"
JPEG = b"\xff\xd8\xff\xe0" + b"\x00\x10JFIF" + bytes(200)
PNG = b"\x89PNG\r\n\x1a\n" + bytes(64)

# ---------------------------------------------------------------- synthetic Douban answers

SUGGEST_SURINAM = json.dumps([
    {"episode": "6", "img": "https://img9.doubanio.com/view/photo/s_ratio_poster/public/p100.jpg", "title": "苏里南",
     "url": "https://movie.douban.com/subject/1000001/?suggest=x", "type": "movie", "year": "2022",
     "sub_title": "수리남", "id": "1000001"},
    {"episode": "", "img": "https://img9.doubanio.com/view/photo/s_ratio_poster/public/p101.jpg", "title": "苏里南的夏天",
     "url": "https://movie.douban.com/subject/1000002/", "type": "movie", "year": "2019", "sub_title": "", "id": "1000002"},
    {"episode": "", "img": "https://evil.example.com/x.jpg", "title": "某人", "type": "celebrity", "year": "",
     "sub_title": "", "id": "1000003"},
], ensure_ascii=False)

MOBILE_SURINAM = json.dumps({
    "id": "1000001", "title": "苏里南", "original_title": "수리남", "year": "2022", "type": "tv", "is_tv": True,
    "episodes_count": 6, "genres": ["剧情", "动作", "惊悚"], "rating": {"count": 1234, "max": 10, "value": 6.9},
    "pic": {"large": "https://img9.doubanio.com/view/photo/m_ratio_poster/public/p100.jpg",
            "normal": "https://img9.doubanio.com/view/photo/s_ratio_poster/public/p100.jpg"},
    "directors": [{"name": "导演甲"}], "actors": [{"name": f"演员{i}"} for i in range(12)],
    "countries": ["韩国"], "intro": "一个商人接下了政府的秘密任务。", "aka": ["毒枭圣徒", "Narco-Saints"],
    "pubdate": ["2022-09-09(韩国)"]}, ensure_ascii=False)

SUBJECT_PAGE = """<!DOCTYPE html><html><head><title>某电影 (2010)</title>
<script type="application/ld+json">
{
  "@context": "http://schema.org",
  "name": "某电影 Some Film",
  "url": "/subject/2000001/",
  "image": "https://img9.doubanio.com/view/photo/s_ratio_poster/public/p200.jpg",
  "director": [{"@type": "Person", "url": "/celebrity/1/", "name": "导演乙 Director B"}],
  "actor": [{"@type": "Person", "url": "/celebrity/2/", "name": "演员丙"}],
  "datePublished": "2010-07-16",
  "genre": ["\\u79d1\\u5e7b", "\\u60ac\\u7591"],
  "@type": "Movie",
  "description": "第一行
第二行",
  "aggregateRating": {"@type": "AggregateRating", "ratingCount": "999", "ratingValue": "9.4"}
}
</script></head><body><h1><span property="v:itemreviewed">某电影 Some Film</span> <span class="year">(2010)</span></h1>
<div id="info"><span class="pl">又名:</span> 另一个名字 / Another Name<br/></div></body></html>"""


class FakeDouban:
    """fetch(url, headers, cap) → (状态, 跳转地址, 内容)：按地址回答，记下每个请求。"""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.pages: dict[str, tuple[int, str, bytes]] = {}
        self.blocked = False

    def add(self, url: str, body, status: int = 200, location: str = "") -> None:
        self.pages[url] = (status, location, body.encode("utf-8") if isinstance(body, str) else body)

    def suggest(self, query: str, body) -> None:
        import urllib.parse
        self.add(libmeta.SUGGEST_URL + urllib.parse.quote(query), body)

    def __call__(self, url, headers, cap):
        self.calls.append((url, headers))
        if self.blocked:
            return 302, "https://sec.douban.com/c?r=x", b""
        if url.startswith(libmeta.SUGGEST_URL) and url not in self.pages:
            return 200, "", b"[]"
        return self.pages.get(url, (404, "", b"not found"))

    def urls(self) -> list[str]:
        return [u for u, _ in self.calls]


def surinam(fake: FakeDouban) -> None:
    fake.suggest("苏里南", SUGGEST_SURINAM)
    fake.add(libmeta.MOBILE_URL.format("1000001"), MOBILE_SURINAM)
    fake.add("https://img9.doubanio.com/view/photo/m_ratio_poster/public/p100.jpg", JPEG)
    fake.add("https://img9.doubanio.com/view/photo/s_ratio_poster/public/p100.jpg", JPEG)


def wire(lib: library.Library, fake: FakeDouban, now=None) -> FakeDouban:
    clock = now or (lambda: 1_800_000_000.0)
    lib.douban = libmeta.Douban(fetch=fake, clock=clock, sleep=lambda _s: None, log=lambda _m: None)
    lib.clock = clock
    return fake


# ---------------------------------------------------------------- names

class OwnerPathTests(unittest.TestCase):
    def test_owner_paths(self):
        found = library.parse_name(f"{SURINAM}/{EPISODE.format(n=1)}")
        self.assertEqual({k: found[k] for k in ("kind", "show", "year", "season", "episode", "alt", "title")},
                         {"kind": "episode", "show": "苏里南", "year": 2022, "season": 1, "episode": 1,
                          "alt": "Narco-Saints", "title": "第 1 集"})
        found = library.parse_name(MASKED)
        self.assertEqual((found["kind"], found["title"], found["year"]), ("movie", "10间敢死队", 2026))
        found = library.parse_name(PROMO)
        self.assertEqual((found["kind"], found["title"], found["year"]), ("video", "夸克网盘能干什么", None))
        # Saved later the same day: tags and a stray repeated character before the year, an English scene name.
        found = library.parse_name("夸克/来自：分享/铁拳教育 4K高清完整版.育（2026）NF DV&HDR/Teach.You.S01E01.mkv")
        self.assertEqual((found["kind"], found["show"], found["year"], found["alt"]), ("episode", "铁拳教育", 2026, "Teach You"))
        # A garbled file name in a folder named after the film (a copy number "(2)" from saving twice).
        found = library.parse_name("夸克/来自：分享/神探之痕迹(2)/SHEN-碳.TC1080P.mp4")
        self.assertEqual((found["kind"], found["title"], found["year"]), ("movie", "神探之痕迹", None))
        self.assertTrue(library.junk_only("TC1080P"))
        self.assertEqual(library.clean_title("什么意思夫妇(2)(1)"), "什么意思夫妇")

    def test_cleaning(self):
        self.assertEqual(library.unmask("苏.里.南 (2022)"), "苏里南 (2022)")
        self.assertEqual(library.unmask("苏 里 南"), "苏里南")
        self.assertEqual(library.unmask("2024 春节 聚会"), "2024 春节 聚会", "real words keep their spaces")
        self.assertEqual(library.unmask("10J间G敢S死D队2026"), "10间敢死队2026")
        self.assertEqual(library.unmask("A计划"), "A计划", "one letter before CJK is a real title")
        self.assertTrue(library.junk_only("4K HQ SDR 高码率 DDP DTS"))
        self.assertTrue(library.junk_only("2160p.NF.WEB-DL.DDP.5.1.Atmos.HDR.H.265"))
        self.assertFalse(library.junk_only("Narco-Saints"))
        self.assertFalse(library.junk_only("夸克网盘能干什么"))
        # 中文标签挨着英文标签也能截掉；年份前的才是片名。
        self.assertEqual(library.parse_name("流浪地球 4K.HDR.NF高码版.mkv")["title"], "流浪地球")
        self.assertEqual(library.parse_name("电影/流浪地球 内封简中字幕.mkv")["title"], "流浪地球")
        # 英文名的电影在带年份的中文文件夹里：片名用中文的；文件夹没有年份（分类文件夹）就不换。
        found = library.parse_name("盗梦空间 (2010)/Inception.2010.1080p.mkv")
        self.assertEqual((found["title"], found["alt"], found["year"]), ("盗梦空间", "Inception", 2010))
        found = library.parse_name("华语电影/Inception.2010.1080p.mkv")
        self.assertEqual((found["title"], found["alt"]), ("Inception", ""))
        # 只有标记的文件名在分类文件夹里：分类文件夹不算片名，再往上找。
        found = library.parse_name("黑客帝国 (1999)/电影/1080p.x264.mkv")
        self.assertEqual((found["title"], found["year"]), ("黑客帝国", 1999))


# ---------------------------------------------------------------- parsing and matching

class ParseTests(unittest.TestCase):
    def test_invisible_marks_are_dropped(self):
        found = libmeta.parse_search(json.dumps({"items": [{"target_type": "movie", "target": {
            "id": "1000123", "title": "神探之痕迹\u200e", "year": "2026"}}]}))
        self.assertEqual(found[0]["title"], "神探之痕迹")

    def test_suggest(self):
        found = libmeta.parse_suggest(SUGGEST_SURINAM)
        self.assertEqual([(c["id"], c["type"], c["year"], c["episodes"]) for c in found],
                         [("1000001", "tv", 2022, 6), ("1000002", "movie", 2019, None)])
        self.assertEqual(found[0]["sub_title"], "수리남")
        self.assertTrue(found[0]["img"].startswith("https://img9.doubanio.com/"))
        self.assertEqual(libmeta.parse_suggest(b"<html>"), [])
        self.assertEqual(libmeta.parse_suggest(json.dumps([{"id": "../x", "title": "a"}])), [])

    def test_mobile(self):
        found = libmeta.parse_mobile(MOBILE_SURINAM)
        self.assertEqual((found["title"], found["original"], found["year"], found["type"], found["rating"],
                          found["episodes"]), ("苏里南", "수리남", 2022, "tv", 6.9, 6))
        self.assertEqual(found["genres"], ["剧情", "动作", "惊悚"])
        self.assertEqual(len(found["actors"]), libmeta.PEOPLE_MAX)
        self.assertIn("Narco-Saints", found["aka"])
        self.assertIn("m_ratio_poster", found["img"])
        self.assertIsNone(libmeta.parse_mobile(b'{"msg": "error"}'))

    def test_subject_page(self):
        found = libmeta.parse_subject(SUBJECT_PAGE)
        self.assertEqual((found["id"], found["title"], found["original"], found["year"], found["type"], found["rating"]),
                         ("2000001", "某电影", "Some Film", 2010, "movie", 9.4))
        self.assertEqual(found["genres"], ["科幻", "悬疑"])
        self.assertEqual(found["directors"], ["导演乙 Director B"])
        self.assertEqual(found["plot"], "第一行 第二行")
        self.assertEqual(found["aka"], ["另一个名字", "Another Name"])
        self.assertIn("m_ratio_poster", found["img"])
        tv = SUBJECT_PAGE.replace('<div id="info">', '<div id="info"><span class="pl">集数:</span> 16<br/>')
        self.assertEqual((libmeta.parse_subject(tv)["type"], libmeta.parse_subject(tv)["episodes"]), ("tv", 16))
        self.assertIsNone(libmeta.parse_subject("<html>no data</html>"))

    def test_image_checks(self):
        self.assertEqual(libmeta.image_kind(JPEG), "jpg")
        self.assertEqual(libmeta.image_kind(PNG), "png")
        self.assertEqual(libmeta.image_kind(b"RIFF\x00\x00\x00\x00WEBPVP8 "), "webp")
        self.assertEqual(libmeta.image_kind(b"<html>"), "")
        self.assertTrue(libmeta.good_image_url("https://img3.doubanio.com/view/photo/s_ratio_poster/public/p1.jpg"))
        for bad in ("http://img3.doubanio.com/x.jpg", "https://img3.doubanio.com.evil.com/x.jpg",
                    "https://evil.com/img3.doubanio.com/x.jpg", "file:///etc/passwd", None):
            self.assertFalse(libmeta.good_image_url(bad), bad)

    def test_pick(self):
        cands = libmeta.parse_suggest(SUGGEST_SURINAM)
        self.assertEqual(libmeta.pick(cands, ["苏里南"], 2022, "tv")["id"], "1000001")
        self.assertEqual(libmeta.pick(cands, ["苏 里 南"], None, "tv")["id"], "1000001", "normalized, no year needed")
        self.assertEqual(libmeta.pick(cands, ["", "수리남"], 2022, "tv")["id"], "1000001", "original title")
        self.assertIsNone(libmeta.pick(cands, ["苏里南"], 2021, "tv"), "year must agree")
        self.assertIsNone(libmeta.pick(cands, ["苏里南"], 2022, "movie"), "type must agree")
        self.assertIsNone(libmeta.pick(cands, ["苏里"], 2022, "tv"), "no partial titles")
        twins = [dict(cands[0]), dict(cands[0], id="1000009")]
        self.assertIsNone(libmeta.pick(twins, ["苏里南"], 2022, "tv"), "two alike → not sure")


class PolitenessTests(unittest.TestCase):
    def setUp(self):
        self.now = 1_800_000_000.0
        self.slept: list[float] = []
        self.fake = FakeDouban()
        surinam(self.fake)
        self.douban = libmeta.Douban(fetch=self.fake, clock=lambda: self.now, sleep=self.sleep, log=lambda _m: None)

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds

    def test_gap_headers_and_no_cookies(self):
        self.douban.suggest("苏里南")
        self.douban.details("1000001")
        self.assertEqual(len(self.slept), 1)
        self.assertGreaterEqual(self.slept[0], libmeta.GAP_S - 0.01)
        for _, headers in self.fake.calls:
            self.assertIn("Mozilla/5.0", headers["User-Agent"])
            self.assertTrue(headers["Referer"].startswith("https://m"))
            self.assertNotIn("Cookie", headers)

    def test_backoff(self):
        for status, location, body in ((403, "", b""), (429, "", b""), (302, "https://sec.douban.com/c?r=1", b""),
                                       (200, "", "<p>检测到有异常请求从你的 IP 发出</p>".encode())):
            self.fake.pages[libmeta.MOBILE_URL.format("7")] = (status, location, body)
            self.douban.blocked_until = 0
            calls = len(self.fake.calls)
            with self.assertRaises(libmeta.Blocked):
                self.douban.details("7")
            self.assertEqual(len(self.fake.calls), calls + 1, "the subject page is not tried while blocked")
            self.assertTrue(self.douban.blocked())
            with self.assertRaises(libmeta.Blocked):
                self.douban.suggest("苏里南")
            self.assertEqual(len(self.fake.calls), calls + 1, "nothing is sent during the back-off")
        self.now += libmeta.BACKOFF_S + 1
        self.assertEqual(self.douban.suggest("苏里南")[0]["id"], "1000001")

    def test_subject_page_fallback_and_image_cap(self):
        self.fake.add(libmeta.MOBILE_URL.format("2000001"), "gone", status=404)
        self.fake.add(libmeta.SUBJECT_URL.format("2000001"), SUBJECT_PAGE)
        self.assertEqual(self.douban.details("2000001")["title"], "某电影")
        url = "https://img9.doubanio.com/view/photo/m_ratio_poster/public/p300.jpg"
        self.fake.add(url, b"<html>not an image</html>")
        with self.assertRaises(libmeta.Failed):
            self.douban.image(url)
        self.fake.add(url, JPEG + bytes(libmeta.POSTER_MAX))
        with self.assertRaises(libmeta.Failed):
            self.douban.image(url)
        with self.assertRaises(libmeta.Failed):
            self.douban.image("https://evil.example.com/p.jpg")
        self.assertFalse(any("evil" in u for u in self.fake.urls()))


# ---------------------------------------------------------------- the library's matcher

class MatcherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.root = base / "videos"
        for n in range(1, 4):
            lib_base.touch(self.root / SURINAM / EPISODE.format(n=n), lib_base.MP4)
        lib_base.touch(self.root / MASKED, lib_base.MP4)
        lib_base.touch(self.root / PROMO, lib_base.MP4)
        self.now = 1_800_000_000.0
        self.lib = lib_base.quiet(base / "data")
        self.fake = wire(self.lib, FakeDouban(), lambda: self.now)
        surinam(self.fake)
        self.assertTrue(self.lib.add({"kind": "local", "path": str(self.root)})["ok"])

    def tearDown(self):
        self.tmp.cleanup()

    def run_all(self):
        steps = 0
        while self.lib.meta_step():
            steps += 1
            self.assertLess(steps, 20)
        return steps

    def view(self):
        view = self.lib.view()
        return view, {c["guess"]: c for c in view["movies"] + view["videos"]}, {s["guess"]: s for s in view["shows"]}

    def test_matches_the_show_misses_the_masked_film_never_sends_the_promo(self):
        self.assertEqual(self.lib.view()["pending"], 2, "the page polls while titles are pending")
        self.assertEqual(self.run_all(), 2)
        self.assertEqual(self.lib.view()["pending"], 0)
        sent = " ".join(self.fake.urls())
        self.assertNotIn("%E5%A4%B8%E5%85%8B", sent, "an 'other video' title is never sent automatically")
        view, cards, shows = self.view()
        show = shows["苏里南"]
        self.assertEqual((show["title"], show["year"], show["count"]), ("苏里南", 2022, 3))
        self.assertEqual(show["meta"]["rating"], 6.9)
        self.assertEqual(show["meta"]["actors"][:2], ["演员0", "演员1"])
        self.assertTrue(show["art"].startswith(f"api/library/meta-art/{show['key']}"))
        poster = self.lib.art_dir / "douban-1000001.jpg"
        self.assertEqual(poster.read_bytes(), JPEG)
        masked = cards["10间敢死队"]
        self.assertEqual((masked["meta"]["state"], masked["title"], masked["art"]), ("miss", "10间敢死队", ""))
        self.assertIsNone(cards["夸克网盘能干什么"]["meta"])
        meta_file = self.lib.dir / "library-meta.json"
        self.assertEqual(stat.S_IMODE(os.stat(meta_file).st_mode), 0o600)
        # 重新扫描不丢资料，也不再查。
        calls = len(self.fake.calls)
        self.lib.scan()
        self.assertEqual(self.run_all(), 0)
        self.assertEqual(len(self.fake.calls), calls)
        self.assertEqual(self.view()[2]["苏里南"]["title"], "苏里南")
        # 没对上的过几天再试一次。
        self.now += libmeta.MISS_RETRY_S + 1
        self.assertEqual(self.lib.meta_due(), ["m" + cards["10间敢死队"]["id"]])

    def test_titles_parsed_the_old_way_wait_for_a_rescan(self):
        """An index from before the name cleanup still says "Narco-Saints": don't send that to Douban; scan first."""
        with library._LOCK:
            for item in self.lib._items.values():
                item["pv"] = library.PARSE_VERSION - 1
                if item["kind"] == "episode":
                    item["show"], item["alt"], item["year"] = "Narco-Saints", "", None
        self.lib._save_sources([{**s, "last_scan": self.now} for s in self.lib._load_sources()])
        self.assertEqual(self.lib.meta_due(), [], "nothing is looked up before the rescan")
        self.assertEqual(self.lib.due(), [self.lib._load_sources()[0]["id"]], "the source is rescanned at once")
        self.lib.scan()
        self.assertEqual(self.run_all(), 2)
        self.assertNotIn("Narco-Saints", " ".join(self.fake.urls()), "looked up as 苏里南, found at once")
        self.assertEqual(self.view()[2]["苏里南"]["meta"]["rating"], 6.9)

    def test_empty_suggest_falls_back_to_the_mobile_search(self):
        """Without cookies subject_suggest often answers [] for everything; the mobile search still answers."""
        import urllib.parse
        del self.fake.pages[libmeta.SUGGEST_URL + urllib.parse.quote("苏里南")]
        search = {"count": 2, "items": [
            {"layout": "subject", "target_type": "movie", "target": {"id": "1000099", "title": "苏里南之子", "year": "2001"}},
            {"layout": "subject", "target_type": "tv", "target": {
                "id": "1000001", "title": "苏里南", "year": "2022",
                "cover_url": "https://img9.doubanio.com/view/photo/m_ratio_poster/public/p100.jpg"}}]}
        self.fake.add(libmeta.SEARCH_URL + urllib.parse.quote("苏里南"), json.dumps(search))
        self.run_all()
        self.assertEqual(self.view()[2]["苏里南"]["meta"]["rating"], 6.9)
        self.assertEqual([c["id"] for c in libmeta.parse_search(json.dumps(search))], ["1000099", "1000001"])
        self.assertEqual(libmeta.parse_search(b"<html>"), [])

    def test_unrelated_suggestions_fall_back_to_the_mobile_search(self):
        """subject_suggest gives a few popular hits ("龙" gave "欢迎来龙餐馆"); the fuller mobile search is tried next."""
        import urllib.parse
        self.fake.suggest("苏里南", json.dumps([{"id": "1000098", "title": "苏里南之子", "type": "movie", "year": "2001"}]))
        search = {"items": [{"target_type": "tv", "target": {"id": "1000001", "title": "苏里南", "year": "2022"}}]}
        self.fake.add(libmeta.SEARCH_URL + urllib.parse.quote("苏里南"), json.dumps(search))
        self.run_all()
        self.assertEqual(self.view()[2]["苏里南"]["meta"]["rating"], 6.9)

    def test_english_title_through_aka(self):
        # 中文名查不到（文件夹里是另一个写法）：英文名查出来的候选又名里有 Narco-Saints 才认。
        os.rename(self.root / SURINAM, self.root / "夸克/来自：分享/毒枭圣徒 (2022)")
        self.lib.scan()
        self.fake.suggest("Narco-Saints", SUGGEST_SURINAM)
        self.run_all()
        shows = self.view()[2]
        self.assertEqual(shows["毒枭圣徒"]["title"], "苏里南")
        self.assertEqual(shows["毒枭圣徒"]["meta"]["id"], "1000001")

    def test_blocked_details_are_kept_partial_and_completed_later(self):
        self.fake.add(libmeta.MOBILE_URL.format("1000001"), "", status=403)
        self.run_all()
        show = self.view()[2]["苏里南"]
        self.assertEqual((show["title"], show["meta"]["partial"]), ("苏里南", True), "suggest data kept")
        self.assertTrue(self.lib.douban.blocked())
        self.assertFalse(self.lib.meta_step(), "nothing while blocked")
        surinam(self.fake)
        self.now += libmeta.PARTIAL_RETRY_S + 1
        self.run_all()
        show = self.view()[2]["苏里南"]
        self.assertEqual((show["meta"]["partial"], show["meta"]["rating"]), (False, 6.9))

    def test_manual_choices_are_sticky(self):
        self.run_all()
        _, cards, shows = self.view()
        masked = cards["10间敢死队"]
        self.fake.suggest("敢死队", json.dumps([{"episode": "", "img": "https://img9.doubanio.com/view/photo/s_ratio_poster/public/p500.jpg",
                                               "title": "敢死队", "type": "movie", "year": "2010", "sub_title": "The Expendables",
                                               "id": "3000001"}]))
        self.fake.add(libmeta.MOBILE_URL.format("3000001"), json.dumps(
            {"id": "3000001", "title": "敢死队", "year": "2010", "type": "movie", "rating": {"value": 7.0, "count": 5}}))
        self.fake.add("https://img9.doubanio.com/view/photo/m_ratio_poster/public/p500.jpg", PNG)
        found = self.lib.search("敢死队")
        self.assertEqual(found["candidates"][0]["thumb"], "api/admin/library/douban/thumb/3000001")
        self.assertTrue(self.lib.match(masked["key"], "3000001")["ok"])
        # 不匹配：剧也不用豆瓣的了。
        self.assertTrue(self.lib.unmatch(shows["苏里南"]["key"])["ok"])
        # 文件名的解析变了（PARSE_VERSION、改名）、过了很久：自动的都不碰手动的。
        self.now += libmeta.MISS_RETRY_S * 10
        self.lib.scan()
        self.assertEqual(self.lib.meta_due(), [])
        _, cards, shows = self.view()
        self.assertEqual((cards["10间敢死队"]["title"], cards["10间敢死队"]["meta"]["manual"]), ("敢死队", True))
        self.assertTrue(cards["10间敢死队"]["art"].startswith("api/library/meta-art/"))
        self.assertTrue((self.lib.art_dir / "douban-3000001.png").is_file())
        self.assertIn(cards["10间敢死队"], self.view()[0]["movies"])
        self.assertEqual((shows["苏里南"]["meta"]["state"], shows["苏里南"]["art"]), ("none", ""))
        # 重新取：对上了的重新取详细资料，手动的标记留着。
        self.assertTrue(self.lib.refresh(cards["10间敢死队"]["key"])["meta"]["manual"])

    def test_local_nfo_and_poster_win(self):
        folder = self.root / SURINAM
        lib_base.touch(folder / "poster.jpg", b"\xff\xd8local")
        lib_base.touch(self.root / "夸克/来自：分享/10J间G敢S死D队2026/4K HQ SDR 高码率 DDP DTS.nfo",
                       "<movie><title>本地的片名</title><year>2026</year></movie>".encode())
        lib_base.touch(self.root / MASKED, lib_base.MP4[:5000])     # 换了文件（大小变了）才重新读 .nfo
        self.lib.scan()
        self.fake.suggest("本地的片名", json.dumps([{"episode": "", "title": "本地的片名", "type": "movie", "year": "2026",
                                                   "sub_title": "", "id": "4000001", "img": ""}]))
        self.fake.add(libmeta.MOBILE_URL.format("4000001"), json.dumps(
            {"id": "4000001", "title": "豆瓣的片名", "year": "2026", "type": "movie", "intro": "豆瓣的简介"}))
        self.run_all()
        _, cards, shows = self.view()
        card = cards["本地的片名"]
        self.assertEqual((card["title"], card["plot"]), ("本地的片名", "豆瓣的简介"), "nfo title wins, plot fills in")
        self.assertTrue(shows["苏里南"]["art"].startswith("api/library/art/"), "local poster wins")
        self.assertEqual(shows["苏里南"]["meta"]["rating"], 6.9)

    def test_offline_sends_nothing(self):
        self.assertTrue(self.lib.set_online(False)["ok"])
        self.assertEqual(self.run_all(), 0)
        self.assertFalse(self.lib.search("苏里南")["ok"])
        self.assertFalse(self.lib.refresh("s" + library.Library.show_key("苏里南"))["ok"])
        self.assertEqual(self.fake.calls, [])
        self.assertFalse(self.lib.view()["online"])
        # 开关和来源存在一起；加、删来源不会把它弄丢。
        self.assertFalse(json.loads((self.lib.dir / "library.json").read_text())["online"])
        self.lib.scan()
        self.assertFalse(self.lib.online())
        self.lib.set_online(True)
        self.assertEqual(self.run_all(), 2)


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
        self.cookies["admin"] = "tvs=" + self.accounts.new_session(self.admin, "pc")
        self.root = Path(self.tmp.name) / "videos"
        for n in range(1, 3):
            lib_base.touch(self.root / SURINAM / EPISODE.format(n=n), lib_base.MP4)
        lib_base.touch(self.root / MASKED, lib_base.MP4)
        self.relay.library = lib_base.quiet(Path(self.accounts.path).parent)
        self.fake = wire(self.relay.library, FakeDouban())
        surinam(self.fake)
        self.assertTrue(self.relay.library.add({"kind": "local", "path": str(self.root)})["ok"])
        while self.relay.library.meta_step():
            pass

    def tearDown(self):
        self.tearDown_relay()

    def test_family_reads_admin_changes(self):
        view = self.call("alice", "GET", "/tv/api/library")[1]
        show = view["shows"][0]
        self.assertEqual((show["title"], show["meta"]["rating"]), ("苏里南", 6.9))
        self.assertNotIn("doubanio", json.dumps(view), "the page never gets Douban image addresses")
        art = show["art"].split("?")[0]
        status, response, body = self.request("GET", "/tv/" + art, "alice")
        self.assertEqual((status, response.getheader("Content-Type"), body), (200, "image/jpeg", JPEG))
        for who in ("guest", None):
            self.assertEqual(self.request("GET", "/tv/" + art, who)[0], 404)
            self.assertEqual(self.request("GET", "/tv/api/library", who)[0], 404)
        key = view["movies"][0]["key"]
        changes = (("GET", "/tv/api/admin/library/douban?q=%E8%8B%8F%E9%87%8C%E5%8D%97", None),
                   ("POST", "/tv/api/admin/library/match", {"key": key, "id": "1000001"}),
                   ("POST", "/tv/api/admin/library/match", {"key": key, "none": True}),
                   ("POST", "/tv/api/admin/library/meta/refresh", {"key": key}),
                   ("POST", "/tv/api/admin/library/settings", {"online": False}))
        for who in ("alice", "guest", None):
            for method, path, body in changes:
                self.assertEqual(self.call(who, method, path, body)[0], 404, (who, path))
        calls = len(self.fake.calls)
        found = self.call("admin", "GET", "/tv/api/admin/library/douban?q=%E8%8B%8F%E9%87%8C%E5%8D%97")[1]
        self.assertEqual(found["candidates"][0]["id"], "1000001")
        self.assertEqual(self.request("GET", "/tv/" + found["candidates"][0]["thumb"], "alice")[0], 404)
        status, _, body = self.request("GET", "/tv/" + found["candidates"][0]["thumb"], "admin")
        self.assertEqual((status, body[:3]), (200, JPEG[:3]))
        self.assertEqual(self.request("GET", "/tv/api/admin/library/douban/thumb/999", "admin")[0], 404, "only seen ones")
        self.assertGreater(len(self.fake.calls), calls)
        answer = self.call("admin", "POST", "/tv/api/admin/library/match", {"key": key, "id": "1000001"})[1]
        self.assertTrue(answer["ok"] and answer["meta"]["manual"], answer)
        self.assertFalse(self.call("admin", "POST", "/tv/api/admin/library/match", {"key": "../x", "id": "1"})[1]["ok"])
        self.assertTrue(self.call("admin", "POST", "/tv/api/admin/library/match", {"key": key, "none": True})[1]["ok"])
        self.assertTrue(self.call("admin", "POST", "/tv/api/admin/library/settings", {"online": False})[1]["ok"])
        self.assertFalse(self.call("admin", "GET", "/tv/api/admin/library")[1]["online"])
        actions = sorted(r["action"] for r in self.accounts.audit_rows(100) if r["action"].startswith("library."))
        self.assertEqual(actions, ["library.match", "library.settings", "library.unmatch"])


if __name__ == "__main__":
    unittest.main()
