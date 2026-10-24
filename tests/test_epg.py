"""The programme guide (server/relay/epg.py): XMLTV parsing, channel matching,
the download cache, the background refresh, and the APIs.

Nothing here touches the network: downloads go through a fake opener. The guide
is off by default; when it is on it keeps only the channels in the table and only
the programmes from an hour ago to a day ahead.
"""

import email.message
import gzip
import io
import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from server import m3u  # noqa: E402
from server.relay import admin, channeltable, epg, presets, web  # noqa: E402
from server.tenancy.accounts import Accounts  # noqa: E402

from test_relay_web import KEY, FakeClient  # noqa: E402

# 2026-10-24 19:00 +0800
NOW = 1792839600


def stamp(offset_s, tz="+0800"):
    """XMLTV time for NOW + offset_s, written in Beijing time (or UTC for tz="+0000")."""
    import time
    shift = 8 * 3600 if tz == "+0800" else 0
    return time.strftime("%Y%m%d%H%M%S", time.gmtime(NOW + offset_s + shift)) + (" " + tz if tz else "")


def guide_xml(*programmes, channels=()):
    body = "".join(f'<channel id="{cid}">' + "".join(f"<display-name>{n}</display-name>" for n in names) +
                   "</channel>" for cid, names in channels)
    body += "".join(f'<programme channel="{cid}" start="{start}"' + (f' stop="{stop}"' if stop else "") +
                    f"><title lang=\"zh\">{title}</title></programme>" for cid, start, stop, title in programmes)
    return f'<?xml version="1.0" encoding="UTF-8"?><tv>{body}</tv>'.encode("utf-8")


TABLE = ("ch000 | CCTV-1 综合 | https://x/1.m3u8\n"
         "ch001 | 湖南卫视 | https://x/2.m3u8\n"
         "ch002 | 东方卫视4K | https://x/3.m3u8\n"
         "ch003 | CCTV4K | https://x/4.m3u8\n"
         "ch004 | 某地新闻 | https://x/5.m3u8\n")


class TimeTests(unittest.TestCase):
    def test_timezones(self):
        self.assertEqual(epg.parse_time("20261024190000 +0800"), NOW)
        self.assertEqual(epg.parse_time("20261024110000 +0000"), NOW)
        self.assertEqual(epg.parse_time("20261024110000Z"), NOW)
        self.assertEqual(epg.parse_time("20261024060000 -0500"), NOW)
        self.assertEqual(epg.parse_time("20261024190000 +08:00"), NOW)
        self.assertEqual(epg.parse_time("202610241900 +0800"), NOW, "seconds are optional")
        self.assertEqual(epg.parse_time("20261024190000"), NOW, "no zone means Beijing time")

    def test_malformed(self):
        for text in ("", None, 20261024, "2026-10-24 19:00", "20261324190000 +0800", "20260231120000 +0800",
                     "20261024250000 +0800", "20261024190000 +2500", "20261024190000 junk"):
            self.assertIsNone(epg.parse_time(text), text)


class MatchTests(unittest.TestCase):
    def test_names_that_are_the_same_channel(self):
        for name in ("CCTV1", "CCTV-1", "cctv 1", "CCTV-1 综合", "CCTV1综合", "CCTV-1 HD", "ＣＣＴＶ１",
                     "CCTV1 (1080p)", "CCTV-1高清", "CCTV01"):
            self.assertEqual(epg.key_of(name)[0], "CCTV1", name)
        for name in ("CCTV5+", "CCTV-5+ 体育赛事", "CCTV5+体育赛事高清", "CCTV5PLUS"):
            self.assertEqual(epg.key_of(name)[0], "CCTV5+", name)
        self.assertEqual(epg.key_of("CCTV-5 体育")[0], "CCTV5")
        self.assertEqual(epg.key_of("CCTV4K 超高清")[0], "CCTV4K")
        self.assertEqual(epg.key_of("CCTV4欧洲")[0], "CCTV4欧洲")
        self.assertEqual(epg.key_of("CCTV-4 中文国际 美洲")[0], "CCTV4美洲")
        self.assertEqual(epg.key_of("CCTV17")[0], "CCTV17")
        for name in ("湖南卫视", "湖南卫视HD", "湖南卫视 高清", "湖南卫视[1080P]", "湖南卫视-频道"):
            self.assertEqual(epg.key_of(name)[0], "湖南卫视", name)
        self.assertEqual(epg.key_of("东方卫视 4K")[0], "东方卫视4K")
        self.assertEqual(epg.uhd_base("东方卫视4K"), "东方卫视")
        self.assertEqual(epg.uhd_base("CCTV4K"), "", "CCTV4K is its own channel")
        self.assertEqual(epg.uhd_base("湖南卫视"), "")

    def test_matcher_scores(self):
        matcher = epg.Matcher([("ch000", ["", "", "CCTV-1 综合"]), ("ch002", ["", "", "东方卫视4K"]),
                               ("ch003", ["", "", "CCTV4K"]), ("ch009", ["CCTV9", "", "纪录"])])
        self.assertEqual(matcher.match(["CCTV1"]), {"ch000": 2})
        self.assertEqual(matcher.match(["CCTV-1 综合"]), {"ch000": 0}, "same text is the best match")
        self.assertEqual(matcher.match(["东方卫视"]), {"ch002": 4}, "a 4K channel falls back to the plain one")
        self.assertEqual(matcher.match(["东方卫视 4K"]), {"ch002": 2})
        self.assertEqual(matcher.match(["CCTV4"]), {}, "CCTV4 is not CCTV4K")
        self.assertEqual(matcher.match(["CCTV4K"]), {"ch003": 0})
        self.assertEqual(matcher.match(["cctv9"]), {"ch009": 0}, "tvg-id from the playlist")
        self.assertEqual(matcher.match(["CCTV9-TVMAO-2026"]), {"ch009": 3}, "dropped words rank lower")
        self.assertEqual(matcher.match(["别的台", ""]), {})

    def test_owner_like_table(self):
        """CCTV1-17 and the satellite channels in the owner's style all find their programmes."""
        rows = [(f"ch{n:03d}", ["", "", f"CCTV{n}"]) for n in range(1, 18)]
        rows += [("ch100", ["", "", "湖南卫视"]), ("ch101", ["", "", "浙江卫视4K"]), ("ch102", ["", "", "江苏卫视"])]
        matcher = epg.Matcher(rows)
        guide_ids = [f"CCTV{n}" for n in range(1, 18)] + ["湖南卫视", "浙江卫视", "江苏卫视"]
        matched = set()
        for gid in guide_ids:
            matched |= set(matcher.match([gid]))
        self.assertEqual(matched, {cid for cid, _names in rows})


class ParseTests(unittest.TestCase):
    def setUp(self):
        self.matcher = epg.Matcher([("ch000", ["", "", "CCTV1"]), ("ch001", ["", "", "湖南卫视"])])

    def parse(self, data, **kwargs):
        return epg.parse(io.BytesIO(data), self.matcher, NOW - epg.WINDOW_BEFORE_S, NOW + epg.WINDOW_AFTER_S,
                         **kwargs)

    def test_keeps_only_table_channels_inside_the_window(self):
        data = guide_xml(("CCTV1", stamp(-7200), stamp(-3700), "太早"),
                         ("CCTV1", stamp(-1800), stamp(1800), "新闻联播"),
                         ("CCTV1", stamp(1800), stamp(3600), "焦点访谈"),
                         ("CCTV1", stamp(90000), stamp(93600), "后天"),
                         ("湖南卫视", stamp(0), stamp(3600), "快乐大本营"),
                         ("CCTV2", stamp(0), stamp(3600), "不在表里"),
                         channels=[("CCTV1", ["CCTV-1 综合"]), ("湖南卫视", ["湖南卫视"]), ("CCTV2", ["CCTV2"])])
        result = self.parse(data)
        self.assertEqual(result["error"], "")
        self.assertEqual(result["channels"], 3)
        self.assertEqual(result["seen"], 6)
        self.assertEqual([p[2] for p in result["programmes"]["ch000"]], ["新闻联播", "焦点访谈"])
        self.assertEqual(result["programmes"]["ch000"][0][:2], (NOW - 1800, NOW + 1800))
        self.assertEqual([p[2] for p in result["programmes"]["ch001"]], ["快乐大本营"])
        self.assertEqual(set(result["programmes"]), {"ch000", "ch001"})

    def test_gzip_and_utc_times(self):
        data = gzip.compress(guide_xml(("CCTV1", stamp(-60, "+0000"), stamp(600, "+0000"), "整点新闻")))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "guide.xml"
            path.write_bytes(data)
            handle, stream = epg.open_xml(path)
            try:
                result = epg.parse(stream, self.matcher, NOW - 3600, NOW + 86400)
            finally:
                handle.close()
        self.assertEqual(result["programmes"]["ch000"], [(NOW - 60, NOW + 600, "整点新闻")])

    def test_missing_stop_uses_the_next_start_and_duplicates_go(self):
        data = guide_xml(("CCTV1", stamp(0), "", "一"), ("CCTV1", stamp(0), "", "一（重复）"),
                         ("CCTV1", stamp(900), stamp(1800), "二"), ("CCTV1", stamp(1800), "", "最后一个，没有结束"))
        self.assertEqual(self.parse(data)["programmes"]["ch000"],
                         [(NOW, NOW + 900, "一"), (NOW + 900, NOW + 1800, "二")])

    def test_malformed_xml_keeps_what_came_before(self):
        good = guide_xml(("CCTV1", stamp(0), stamp(600), "前面的"))
        broken = good[:-5] + '<programme channel="CCTV1" start="x"><title>坏'.encode()
        result = self.parse(broken)
        self.assertIn("ParseError", result["error"])
        self.assertEqual([p[2] for p in result["programmes"]["ch000"]], ["前面的"])
        self.assertIn("ParseError", self.parse(b"not xml at all")["error"])
        # Bad times and empty titles are skipped, not fatal.
        result = self.parse(guide_xml(("CCTV1", "yesterday", stamp(600), "坏时间"), ("CCTV1", stamp(0), stamp(600), ""),
                                      ("CCTV1", stamp(600), stamp(0), "倒着的"), ("CCTV1", stamp(0), stamp(600), "好的")))
        self.assertEqual([p[2] for p in result["programmes"]["ch000"]], ["好的"])

    def test_size_limit_and_per_channel_cap(self):
        many = guide_xml(*[("CCTV1", stamp(n * 60), stamp(n * 60 + 60), f"第{n}条") for n in range(1000)])
        result = self.parse(many)
        self.assertEqual(len(result["programmes"]["ch000"]), epg.PER_CHANNEL_MAX)
        self.assertIn("太大", self.parse(many, limit=4096)["error"])
        bomb = gzip.compress(b"<tv>" + b" " * (2 * 1024 * 1024) + b"</tv>")
        self.assertIn("太大", epg.parse(gzip.GzipFile(fileobj=io.BytesIO(bomb)), self.matcher, 0, 1,
                                       limit=1024 * 1024)["error"])

    def test_programmes_before_their_channel_element_and_titles_are_clipped(self):
        data = guide_xml(("CCTV1", stamp(0), stamp(600), "长" * 200))
        self.assertEqual(len(self.parse(data)["programmes"]["ch000"][0][2]), epg.TITLE_MAX)

    def test_prefers_the_exact_channel_over_a_lookalike(self):
        data = guide_xml(("CCTV1-TVMAO", stamp(0), stamp(600), "别家的"), ("CCTV1", stamp(0), stamp(600), "这家的"))
        self.assertEqual(self.parse(data)["programmes"]["ch000"][0][2], "这家的")


class Answer(io.BytesIO):
    def __init__(self, data, headers=None):
        super().__init__(data)
        self.headers = email.message.Message()
        for name, value in (headers or {}).items():
            self.headers[name] = value


class FetchTests(unittest.TestCase):
    URL = "https://epg.example.com/e.xml"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name) / "epg"
        self.seen = []

    def tearDown(self):
        self.tmp.cleanup()

    def opener(self, data=b"<tv/>", status=200, headers=None):
        def open_(request, timeout):
            self.seen.append((request.full_url, dict(request.header_items()), timeout))
            if status != 200:
                raise urllib.error.HTTPError(request.full_url, status, "x", {}, io.BytesIO())
            return Answer(data, headers)
        return open_

    def test_conditional_get(self):
        info = epg.fetch(self.URL, self.folder, self.opener(b"<tv/>", headers={"ETag": 'W/"1"',
                                                                             "Last-Modified": "Sat, 24 Oct 2026"}))
        self.assertTrue(info["ok"] and info["changed"])
        self.assertEqual(info["bytes"], 5)
        first_headers = self.seen[0][1]
        self.assertNotIn("If-none-match", first_headers)
        self.assertEqual(first_headers["Accept-encoding"], "gzip")
        self.assertEqual(self.seen[0][2], epg.FETCH_TIMEOUT_S)
        info = epg.fetch(self.URL, self.folder, self.opener(status=304))
        self.assertTrue(info["ok"])
        self.assertFalse(info["changed"])
        self.assertEqual(self.seen[1][1]["If-none-match"], 'W/"1"')
        self.assertEqual(self.seen[1][1]["If-modified-since"], "Sat, 24 Oct 2026")
        self.assertEqual((self.folder / f"{epg.cache_name(self.URL)}.xml").read_bytes(), b"<tv/>")

    def test_failures_keep_the_old_file(self):
        epg.fetch(self.URL, self.folder, self.opener(b"<tv>old</tv>"))
        cached = self.folder / f"{epg.cache_name(self.URL)}.xml"
        for opener, word in ((self.opener(status=500), "HTTP 500"), (self.opener(b""), "空"),
                             (self.opener(b"x" * (epg.DOWNLOAD_MAX + 1)), "太大")):
            info = epg.fetch(self.URL, self.folder, opener)
            self.assertFalse(info["ok"])
            self.assertIn(word, info["error"])
            self.assertEqual(cached.read_bytes(), b"<tv>old</tv>")
        self.assertFalse(list(self.folder.glob("*.part")))

        def refused(request, timeout):
            raise urllib.error.URLError("timed out")
        self.assertIn("连不上", epg.fetch(self.URL, self.folder, refused)["error"])

    def test_slow_server(self):
        class Drip(Answer):
            def read(self, _n=-1):
                return b"<"
        clock = iter(range(0, 100000, 100))
        info = epg.fetch(self.URL, self.folder, lambda *_a, **_k: Drip(b""), monotonic=lambda: next(clock))
        self.assertIn("太慢", info["error"])

    def test_only_http(self):
        for url in ("ftp://x/e.xml", "file:///etc/passwd", "http://", "https://x/a b.xml", "javascript:1"):
            self.assertFalse(epg.fetch(url, self.folder, self.opener())["ok"], url)
        self.assertEqual(self.seen, [])
        handler = epg._HttpOnlyRedirect()
        request = urllib.request.Request(self.URL)
        self.assertIsNone(handler.redirect_request(request, None, 302, "", {}, "file:///etc/passwd"))
        self.assertIsNotNone(handler.redirect_request(request, None, 302, "", {}, "https://mirror.example/e.xml"))


class GuideTests(unittest.TestCase):
    URL = "https://epg.example.com/e.xml"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.table = root / "channels.txt"
        self.table.write_text(TABLE, encoding="utf-8")
        self.now = NOW
        self.downloads = 0
        self.data = guide_xml(("CCTV1", stamp(-600), stamp(1200), "新闻联播"), ("CCTV1", stamp(1200), stamp(3000), "天气预报"),
                              ("东方卫视", stamp(0), stamp(3600), "东方新闻"), ("CCTV4", stamp(0), stamp(3600), "中文国际"),
                              ("某地新闻台", stamp(0), stamp(3600), "本地"))
        self.guide = epg.Guide(root / "epg", self.table, log=lambda _m: None, clock=lambda: self.now,
                               opener=self.opener)

    def tearDown(self):
        self.tmp.cleanup()

    def opener(self, request, timeout):
        self.downloads += 1
        if request.get_header("If-none-match"):
            raise urllib.error.HTTPError(request.full_url, 304, "same", {}, io.BytesIO())
        return Answer(gzip.compress(self.data), {"ETag": '"v1"'})

    def test_off_by_default(self):
        self.assertFalse(self.guide.enabled)
        self.guide.tick()
        self.assertEqual(self.downloads, 0)
        self.assertEqual(self.guide.lookup(["ch000"]), {})

    def test_refresh_lookup_and_schedule(self):
        self.guide.configure([self.URL], start=False)
        self.guide.tick()
        self.assertEqual(self.downloads, 1)
        found = self.guide.lookup(["ch000", "ch002", "ch003", "ch004", "nope"])
        self.assertEqual(found["ch000"], {"now": {"title": "新闻联播", "start": NOW - 600, "end": NOW + 1200},
                                          "next": {"title": "天气预报", "start": NOW + 1200, "end": NOW + 3000}})
        self.assertEqual(found["ch002"]["now"]["title"], "东方新闻", "the 4K channel uses the plain one")
        self.assertNotIn("ch003", found, "CCTV4K does not borrow CCTV4")
        self.assertNotIn("ch004", found)
        status = self.guide.status()
        self.assertEqual(status["coverage"]["matched"], 2)
        self.assertEqual(status["coverage"]["total"], 5)
        self.assertIn("湖南卫视", status["coverage"]["missing"])
        self.assertTrue(status["sources"][0]["ok"])
        # Later the same hour: nothing is fetched again.
        self.now += 600
        self.guide.tick()
        self.assertEqual(self.downloads, 1)
        self.assertEqual(self.guide.lookup(["ch000"], NOW + 1300)["ch000"]["now"]["title"], "天气预报")
        # After REFRESH_S: asked again with the ETag, a 304, the old file is read again.
        self.now += epg.REFRESH_S
        self.guide.tick()
        self.assertEqual(self.downloads, 2)
        self.assertFalse(self.guide.status()["sources"][0]["changed"])

    def test_table_change_reparses_without_fetching(self):
        self.guide.configure([self.URL], start=False)
        self.guide.tick()
        self.assertNotIn("ch005", self.guide.lookup(["ch005"]))
        with self.table.open("a", encoding="utf-8") as table:
            table.write("ch005 | 某地 | https://x/6.m3u8\n")
        channeltable.save_meta(self.table, {"ch005": {"tvg_id": "某地新闻台"}})
        self.guide.tick()
        self.assertEqual(self.downloads, 1)
        self.assertEqual(self.guide.lookup(["ch005"])["ch005"]["now"]["title"], "本地", "matched by tvg-id")

    def test_failed_download_retries_sooner(self):
        def broken(request, timeout):
            self.downloads += 1
            raise urllib.error.URLError("down")
        self.guide.opener = broken
        self.guide.configure([self.URL], start=False)
        self.guide.tick()
        self.assertFalse(self.guide.status()["sources"][0]["ok"])
        self.now += epg.RETRY_S
        self.guide.tick()
        self.assertEqual(self.downloads, 2)

    def test_turning_off_frees_everything(self):
        self.guide.configure([self.URL], start=False)
        self.guide.tick()
        self.assertTrue(self.guide.lookup(["ch000"]))
        self.guide.configure([], start=False)
        self.assertEqual(self.guide.lookup(["ch000"]), {})
        self.assertEqual(self.guide.status()["sources"], [])

    def test_background_thread_stops_when_turned_off(self):
        self.guide.configure([self.URL])
        thread = self.guide._thread
        self.assertIsNotNone(thread)
        self.guide.configure([])
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertIsNone(self.guide._thread)

    def test_manual_refresh_is_rate_limited(self):
        clock = [1000.0]
        self.guide.monotonic = lambda: clock[0]
        self.assertTrue(self.guide.refresh_now())
        self.assertFalse(self.guide.refresh_now())
        clock[0] += epg.MANUAL_MIN_S
        self.assertTrue(self.guide.refresh_now())


class PlaylistMetaTests(unittest.TestCase):
    def test_m3u_tvg_attributes(self):
        text = ('#EXTM3U x-tvg-url="https://e/x.xml"\n'
                '#EXTINF:-1 tvg-id="CCTV1" tvg-name="CCTV-1" tvg-logo="https://l/1.png" group-title="央视",CCTV-1 综合\n'
                "http://a/1\n"
                "#EXTINF:-1 tvg-name=浙江卫视 tvg-logo=https://l/z.png group-title=综合,浙江卫视\nhttp://a/2\n"
                "#EXTINF:-1,写着 tvg-id=\"假的\" 的台\nhttp://a/3\nhttp://a/4.m3u8\n")
        found = m3u.parse_playlist(text)
        self.assertEqual([(c["tvg_id"], c["tvg_name"], c["logo"]) for c in found],
                         [("CCTV1", "CCTV-1", "https://l/1.png"), ("", "浙江卫视", "https://l/z.png"),
                          ("", "", ""), ("", "", "")])
        self.assertEqual(found[0]["name"], "CCTV-1 综合")

    def test_clean_meta(self):
        self.assertEqual(channeltable.clean_meta({"tvg_id": " CCTV1 ", "logo": "javascript:alert(1)", "x": 1}),
                         {"tvg_id": "CCTV1"})
        self.assertEqual(channeltable.clean_meta({"tvg_id": "x" * 65, "logo": 'https://a/"b.png'}), {})
        self.assertEqual(channeltable.clean_meta("ch000"), {})

    def test_meta_file_round_trip_and_bad_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            table = Path(tmp) / "channels.txt"
            self.assertEqual(channeltable.meta_of(table), {})
            channeltable.save_meta(table, {"ch000": {"tvg_id": "CCTV1"}, "ch001": {}})
            self.assertEqual(channeltable.meta_of(table), {"ch000": {"tvg_id": "CCTV1"}})
            channeltable.meta_path(table).write_text("{broken", encoding="utf-8")
            self.assertEqual(channeltable.meta_of(table), {})
            channeltable.save_meta(table, {})
            self.assertFalse(channeltable.meta_path(table).exists())


class EpgApiTests(unittest.TestCase):
    """Over real sockets: who may read the guide and change its sources; saving the table keeps tvg ids."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.accounts = Accounts(root / "tv.db")
        self.admin = self.accounts.create_user("管理员", "admin")
        self.alice = self.accounts.create_user("Alice")
        self.main = root / "channels.txt"
        self.main.write_text(TABLE, encoding="utf-8")
        self.relay = web.Relay(None, FakeClient(), None, "/opt/ff", KEY, main_channels=self.main,
                               accounts=self.accounts, devices_dir=root / "devices", log=lambda _m: None,
                               clock=lambda: NOW)
        self.guide = epg.attach(self.relay, root / "epg")
        self.guide.autostart = False
        self.guide.opener = lambda request, timeout: Answer(guide_xml(("CCTV1", stamp(-60), stamp(600), "新闻联播")))
        _streams, self.manage = web.make_servers(self.relay, 0, 0)
        _streams.server_close()
        threading.Thread(target=self.manage.serve_forever, daemon=True).start()
        row = self.accounts.register_device("aaaaaaaaaaa1", "xingzhi-cube-154", "s" * 43)
        self.accounts.bind(row["claim_code"], self.alice)
        guest = self.accounts.new_session(None, "guest", device_id="aaaaaaaaaaa1")
        self.cookies = {"admin": "tvs=" + self.accounts.new_session(self.admin),
                        "alice": "tvs=" + self.accounts.new_session(self.alice),
                        "guest": "tvs=" + guest, "nobody": ""}

    def tearDown(self):
        self.guide.configure([], start=False)
        self.manage.shutdown()
        self.manage.server_close()
        self.accounts.close()
        self.tmp.cleanup()

    def call(self, who, method, path, body=None):
        data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
        request = urllib.request.Request(f"http://127.0.0.1:{self.manage.server_address[1]}/tv{path}", data=data,
                                         method=method)
        if self.cookies[who]:
            request.add_header("Cookie", self.cookies[who])
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=10) as answer:
                return answer.status, json.loads(answer.read())
        except urllib.error.HTTPError as error:
            return error.code, None

    def test_off_by_default(self):
        self.assertIsNone(self.guide._thread)
        self.assertEqual(self.call("alice", "GET", "/api/epg"), (200, {"ok": True, "enabled": False, "channels": {}}))
        self.assertNotIn("epg", self.relay.summary())
        self.assertEqual(self.call("admin", "GET", "/api/admin/epg")[1]["sources"], [])

    def test_who_may_read_and_change(self):
        self.assertEqual(self.call("nobody", "GET", "/api/epg")[0], 404)
        for who in ("alice", "guest", "nobody"):
            self.assertEqual(self.call(who, "GET", "/api/admin/epg")[0], 404, who)
            self.assertEqual(self.call(who, "POST", "/api/admin/epg", {"sources": ["https://e/x.xml"]})[0], 404, who)
            self.assertEqual(self.call(who, "POST", "/api/admin/epg/refresh", {})[0], 404, who)
        self.assertFalse(self.guide.enabled)
        self.assertEqual(self.accounts.meta(epg.META_KEY), "")

    def test_admin_turns_it_on_and_everyone_sees_programmes(self):
        url = presets.EPG_PRESETS[0]["url"]
        for bad in ({"sources": "x"}, {"sources": ["ftp://e/x.xml"]}, {"sources": [f"https://e/{n}.xml" for n in range(4)]}):
            status, answer = self.call("admin", "POST", "/api/admin/epg", bad)
            self.assertFalse(answer["ok"], bad)
        status, answer = self.call("admin", "POST", "/api/admin/epg", {"sources": [url, url]})
        self.assertEqual((status, answer["sources"]), (200, [url]))
        self.assertEqual(epg.load_sources(self.accounts), [url])
        self.assertIn("epg.save", [r["action"] for r in self.accounts.audit_rows()])
        self.assertIsNone(self.guide._thread)
        self.guide.tick()
        for who in ("alice", "guest"):
            status, answer = self.call(who, "GET", "/api/epg?channels=ch000,ch001,bad%20id,ch000")
            self.assertEqual(status, 200)
            self.assertEqual(answer["channels"], {"ch000": {"now": {"title": "新闻联播", "start": NOW - 60,
                                                                    "end": NOW + 600}, "next": None}})
        self.assertEqual(set(self.call("alice", "GET", "/api/epg")[1]["channels"]), {"ch000"})
        self.assertTrue(self.relay.summary()["epg"])
        view = self.call("admin", "GET", "/api/admin/epg")[1]
        self.assertEqual(view["status"][0]["name"], presets.EPG_PRESETS[0]["name"])
        self.assertEqual(view["coverage"]["matched"], 1)
        self.assertNotIn("etag", view["status"][0])
        self.assertTrue(self.call("admin", "POST", "/api/admin/epg/refresh", {})[1]["ok"])
        self.assertFalse(self.call("admin", "POST", "/api/admin/epg/refresh", {})[1]["ok"], "rate limited")
        self.assertTrue(self.call("admin", "POST", "/api/admin/epg", {"sources": []})[1]["ok"])
        self.assertFalse(self.call("alice", "GET", "/api/epg")[1]["enabled"])

    def test_saving_the_table_keeps_tvg_ids_of_imported_rows(self):
        version = admin.channels(self.relay)["version"]
        caller = type("Caller", (), {"user": self.admin})()
        rows = ["ch000", "ch001", "ch002", "ch003",
                {"name": "导入的台", "url": "https://x/9.m3u8", "agent": "", "tvg_id": "CCTV13", "logo": "https://l/13.png"}]
        answer = admin.save_channels(self.relay, {"version": version, "rows": rows}, caller)
        self.assertTrue(answer["ok"], answer)
        meta = channeltable.meta_of(self.main)
        self.assertEqual(list(meta.values()), [{"tvg_id": "CCTV13", "logo": "https://l/13.png"}])
        new_id = next(iter(meta))
        answer = admin.save_channels(self.relay, {"version": answer["version"],
                                                  "rows": ["ch000", new_id, {"id": "ch001", "name": "湖南"}]}, caller)
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(channeltable.meta_of(self.main), {new_id: {"tvg_id": "CCTV13", "logo": "https://l/13.png"}})
        answer = admin.save_channels(self.relay, {"version": answer["version"], "rows": ["ch000"]}, caller)
        self.assertFalse(channeltable.meta_path(self.main).exists(), "deleted with its channel")


if __name__ == "__main__":
    unittest.main()
