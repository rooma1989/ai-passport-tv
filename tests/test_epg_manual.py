"""The programme guide's per-source timezone, matching by hand, and logo fallback
(server/relay/epg.py, server/relay/logos.py).

- Times without an offset follow the source's zone (Beijing time by default);
  a source can also be told to ignore the offsets it writes.
- A channel that does not match automatically can be pinned to an EPG channel
  by hand. The pin is kept in the accounts database by channel id and name, so it
  survives a reimport that renumbers the table, and it beats automatic matching.
- A channel without a logo in the table uses the matched EPG channel's
  <icon src>, fetched through the same logo cache rules.

Nothing here touches the network: downloads go through a fake opener.
"""

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
from server.relay import epg, logos, web  # noqa: E402
from server.tenancy.accounts import Accounts  # noqa: E402

from test_epg import NOW, TABLE, Answer, guide_xml, stamp  # noqa: E402
from test_relay_web import KEY, FakeClient  # noqa: E402

URL = "https://epg.example.com/e.xml"
OTHER = "https://epg2.example.com/f.xml"
PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 32


def local(offset_s, hours):
    """XMLTV time for NOW + offset_s written as the wall clock of a zone `hours` east of UTC, without an offset."""
    import time
    return time.strftime("%Y%m%d%H%M%S", time.gmtime(NOW + offset_s + hours * 3600))


def channel_xml(channels, programmes):
    """channels: [(id, [names], icon or "")]; programmes: [(id, start, stop, title)]."""
    body = "".join(f'<channel id="{cid}">' + "".join(f"<display-name>{n}</display-name>" for n in names) +
                   (f'<icon src="{icon}"/>' if icon else "") + "</channel>" for cid, names, icon in channels)
    body += "".join(f'<programme channel="{cid}" start="{start}" stop="{stop}"><title>{title}</title></programme>'
                    for cid, start, stop, title in programmes)
    return f'<?xml version="1.0" encoding="UTF-8"?><tv>{body}</tv>'.encode("utf-8")


class ZoneTests(unittest.TestCase):
    def test_times_without_offset_follow_the_zone(self):
        self.assertEqual(epg.parse_time(local(0, 8)), NOW, "Beijing time by default")
        self.assertEqual(epg.parse_time(local(0, 9), "Asia/Tokyo"), NOW)
        self.assertEqual(epg.parse_time(local(0, 0), "UTC"), NOW)
        # 2026-10-24 is still summer time in New York (UTC-4), and winter time is UTC-5.
        self.assertEqual(epg.parse_time(local(0, -4), "America/New_York"), NOW)
        self.assertEqual(epg.parse_time("20261215120000", "America/New_York"),
                         epg.parse_time("20261215170000 +0000"))
        self.assertEqual(epg.parse_time(local(0, 8), "Nowhere/Unknown"), NOW, "unknown zones are Beijing time")

    def test_written_offsets_win_unless_ignored(self):
        self.assertEqual(epg.parse_time(stamp(0), "Asia/Tokyo"), NOW)
        # A source that writes +0000 although its times are Beijing time.
        wrong = local(0, 8) + " +0000"
        self.assertEqual(epg.parse_time(wrong, "Asia/Shanghai", ignore_offset=True), NOW)
        self.assertEqual(epg.parse_time(wrong), NOW + 8 * 3600)

    def test_parse_counts_offsets_and_uses_the_zone(self):
        matcher = epg.Matcher([("ch000", ["", "", "CCTV1"])])
        data = guide_xml(("CCTV1", local(-60, 9), local(600, 9), "新闻"))
        result = epg.parse(io.BytesIO(data), matcher, NOW - 3600, NOW + 86400, zone="Asia/Tokyo")
        self.assertEqual(result["programmes"]["ch000"], [(NOW - 60, NOW + 600, "新闻")])
        self.assertEqual(result["offsets"], {"with": 0, "without": 1})
        data = guide_xml(("CCTV1", stamp(-60), stamp(600), "新闻"))
        result = epg.parse(io.BytesIO(data), matcher, NOW - 3600, NOW + 86400, zone="Asia/Tokyo")
        self.assertEqual(result["programmes"]["ch000"][0][0], NOW - 60)
        self.assertEqual(result["offsets"], {"with": 1, "without": 0})

    def test_icons_and_manual_in_parse(self):
        matcher = epg.Matcher([("ch000", ["", "", "CCTV1"])])
        data = channel_xml([("c1", ["CCTV-1"], "https://img.example.com/c1.png"),
                            ("odd", ["某个奇怪的名字"], "javascript:alert(1)")],
                           [("c1", stamp(-60), stamp(600), "新闻"), ("odd", stamp(-60), stamp(600), "地方新闻")])
        result = epg.parse(io.BytesIO(data), matcher, NOW - 3600, NOW + 86400, manual={"odd": ["ch004"]})
        self.assertEqual(result["programmes"]["ch004"][0][2], "地方新闻")
        self.assertEqual(result["icons"], {"ch000": "https://img.example.com/c1.png"}, "only http(s) icons")
        self.assertEqual(result["names"]["odd"], ["odd", "某个奇怪的名字"])


class PinTests(unittest.TestCase):
    def test_id_then_name_then_id_alone(self):
        manual = {"ch004": {"source": "", "epg": "a", "name": "某地新闻"},
                  "ch009": {"source": URL, "epg": "b", "name": "旧名字"}}
        rows = [("ch004", ["", "", "某地新闻"]), ("ch009", ["", "", "新名字"])]
        self.assertEqual(epg.pins_of(rows, manual), {"ch004": ("", "a"), "ch009": (URL, "b")})
        # A reimport renumbered the table: found again by name, and ch004 (now another channel) is left alone.
        rows = [("ch004", ["", "", "别的台"]), ("ch020", ["", "", "某地新闻"])]
        self.assertEqual(epg.pins_of(rows, manual), {"ch020": ("", "a")})


class GuideManualTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.table = root / "channels.txt"
        self.table.write_text(TABLE, encoding="utf-8")
        self.files = {
            URL: channel_xml([("cctv1", ["CCTV-1综合"], "https://img.example.com/1.png"),
                              ("hn", ["芒果台"], "https://img.example.com/hn.png"),
                              ("local", ["本地新闻综合"], "")],
                             [("cctv1", stamp(-600), stamp(1200), "新闻联播"),
                              ("hn", stamp(-600), stamp(1200), "快乐大本营"), ("hn", stamp(1200), stamp(3000), "天天向上"),
                              ("local", stamp(0), stamp(3600), "本地")]),
            OTHER: channel_xml([("CCTV1", ["CCTV1"], "https://img2.example.com/1.png")],
                               [("CCTV1", local(-600, 9), local(1200, 9), "东京的新闻")]),
        }
        self.guide = epg.Guide(root / "epg", self.table, log=lambda _m: None, clock=lambda: NOW,
                               opener=lambda request, timeout: Answer(gzip.compress(self.files[request.full_url])))

    def tearDown(self):
        self.tmp.cleanup()

    def test_pin_beats_automatic_matching_and_shows_up_in_search(self):
        self.guide.configure([URL], start=False)
        self.guide.tick()
        coverage = self.guide.status()["coverage"]
        self.assertIn({"id": "ch001", "name": "湖南卫视"}, coverage["unmatched"])
        self.assertEqual(self.guide.icons()["ch000"], "https://img.example.com/1.png")
        hits = self.guide.search("芒果")
        self.assertEqual(hits[0], {"source": URL, "id": "hn", "names": ["芒果台"]})
        self.assertEqual(self.guide.search("cctv1")[0]["id"], "cctv1", "normalised names match too")
        self.assertEqual(self.guide.search(""), [])
        self.assertEqual([p["title"] for p in self.guide.preview(URL, "hn")], ["快乐大本营", "天天向上"])
        self.assertIsNone(self.guide.preview(OTHER, "hn"), "not an enabled source")
        # Pin 湖南卫视 to 芒果台 and CCTV-1 to the local news: the pin wins over the automatic match.
        self.guide.configure([URL], manual={"ch001": {"source": URL, "epg": "hn", "name": "湖南卫视"},
                                            "ch000": {"source": "", "epg": "local", "name": "CCTV-1 综合"}},
                             start=False)
        self.guide.tick()
        found = self.guide.lookup(["ch000", "ch001"])
        self.assertEqual(found["ch001"]["now"]["title"], "快乐大本营")
        self.assertEqual(found["ch000"]["now"]["title"], "本地")
        self.assertEqual(self.guide.icons()["ch001"], "https://img.example.com/hn.png")
        self.assertNotIn("ch000", self.guide.icons(), "the pinned EPG channel has no icon")
        coverage = self.guide.status()["coverage"]
        self.assertNotIn("ch001", [u["id"] for u in coverage["unmatched"]])
        self.assertEqual(coverage["pinned"]["ch001"], {"source": URL, "epg": "hn", "ok": True})

    def test_pinned_source_wins_and_zones_apply_per_source(self):
        self.guide.configure([URL, OTHER], start=False, zones={OTHER: {"zone": "Asia/Tokyo", "ignore": False}})
        self.guide.tick()
        self.assertEqual(self.guide.lookup(["ch000"])["ch000"]["now"]["title"], "新闻联播", "the first source wins")
        self.guide.configure([URL, OTHER], start=False,
                             manual={"ch000": {"source": OTHER, "epg": "CCTV1", "name": "CCTV-1 综合"}})
        self.guide.tick()
        now = self.guide.lookup(["ch000"])["ch000"]["now"]
        self.assertEqual(now, {"title": "东京的新闻", "start": NOW - 600, "end": NOW + 1200})
        self.assertEqual(self.guide.icons()["ch000"], "https://img2.example.com/1.png", "logo from the same source")
        status = {s["url"]: s for s in self.guide.status()["sources"]}
        self.assertEqual(status[OTHER]["offsets"], {"with": 0, "without": 1})


class EpgManualApiTests(unittest.TestCase):
    """Over real sockets: admin only; the pin and the zones are stored and survive a restart."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.accounts = Accounts(self.root / "tv.db")
        self.admin = self.accounts.create_user("管理员", "admin")
        self.alice = self.accounts.create_user("Alice")
        self.main = self.root / "channels.txt"
        self.main.write_text(TABLE, encoding="utf-8")
        self.data = channel_xml([("hn", ["芒果台"], "https://img.example.com/hn.png")],
                                [("hn", local(-60, 8), local(5400, 8), "快乐大本营")])
        self.relay, self.manage = self.start()
        self.cookies = {"admin": "tvs=" + self.accounts.new_session(self.admin),
                        "alice": "tvs=" + self.accounts.new_session(self.alice), "nobody": ""}

    def start(self):
        relay = web.Relay(None, FakeClient(), None, "/opt/ff", KEY, main_channels=self.main, accounts=self.accounts,
                          devices_dir=self.root / "devices", log=lambda _m: None, clock=lambda: NOW)
        guide = epg.attach(relay, self.root / "epg")
        guide.autostart = False
        guide.opener = lambda request, timeout: Answer(self.data)
        _streams, manage = web.make_servers(relay, 0, 0)
        _streams.server_close()
        threading.Thread(target=manage.serve_forever, daemon=True).start()
        return relay, manage

    def tearDown(self):
        self.relay.guide.configure([], start=False)
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

    def test_admin_only(self):
        for who in ("alice", "nobody"):
            self.assertEqual(self.call(who, "GET", "/api/admin/epg/search?q=x")[0], 404, who)
            self.assertEqual(self.call(who, "GET", f"/api/admin/epg/preview?source={URL}&id=hn")[0], 404, who)
            self.assertEqual(self.call(who, "POST", "/api/admin/epg/assign",
                                       {"channel": "ch001", "source": URL, "epg": "hn"})[0], 404, who)
        self.assertEqual(self.accounts.meta(epg.MANUAL_KEY), "")

    def test_assign_zones_and_restart(self):
        self.assertFalse(self.call("admin", "GET", "/api/admin/epg/search?q=x")[1]["ok"], "guide is off")
        answer = self.call("admin", "POST", "/api/admin/epg",
                           {"sources": [URL], "zones": {URL: {"zone": "Asia/Tokyo", "ignore": True}}})[1]
        self.assertTrue(answer["ok"])
        self.assertFalse(self.call("admin", "POST", "/api/admin/epg",
                                   {"sources": [URL], "zones": {URL: {"zone": "Mars/Base"}}})[1]["ok"])
        self.relay.guide.tick()
        view = self.call("admin", "GET", "/api/admin/epg")[1]
        self.assertEqual(view["zones"], {URL: {"zone": "Asia/Tokyo", "ignore": True}})
        self.assertIn({"id": "Asia/Shanghai", "label": "北京时间"}, view["zone_choices"])
        self.assertIn({"id": "ch001", "name": "湖南卫视"}, view["coverage"]["unmatched"])
        results = self.call("admin", "GET", "/api/admin/epg/search?q=" + urllib.parse.quote("芒果"))[1]["results"]
        self.assertEqual(results, [{"source": URL, "id": "hn", "names": ["芒果台"]}])
        # Tokyo time is an hour ahead of the Beijing wall clock the file is written in.
        preview = self.call("admin", "GET", "/api/admin/epg/preview?source=" + urllib.parse.quote(URL) + "&id=hn")[1]
        self.assertEqual(preview["programmes"], [{"title": "快乐大本营", "start": NOW - 60 - 3600, "end": NOW + 5400 - 3600}])
        for bad in ({"channel": "ch404", "source": URL, "epg": "hn"}, {"channel": "ch001", "source": OTHER, "epg": "hn"},
                    {"channel": "ch001", "source": URL, "epg": "nope"}, {"channel": 1}):
            self.assertFalse(self.call("admin", "POST", "/api/admin/epg/assign", bad)[1]["ok"], bad)
        # Back to Beijing time, then pin.
        self.assertTrue(self.call("admin", "POST", "/api/admin/epg", {"sources": [URL], "zones": {}})[1]["ok"])
        self.assertEqual(json.loads(self.accounts.meta(epg.ZONES_KEY)), {URL: {"zone": "Asia/Tokyo", "ignore": True}},
                         "zones not sent are kept")
        self.call("admin", "POST", "/api/admin/epg", {"sources": [URL], "zones": {URL: {"zone": "Asia/Shanghai"}}})
        self.assertEqual(json.loads(self.accounts.meta(epg.ZONES_KEY)), {}, "the default is not stored")
        self.assertTrue(self.call("admin", "POST", "/api/admin/epg/assign",
                                  {"channel": "ch001", "source": URL, "epg": "hn"})[1]["ok"])
        self.assertIn("epg.assign", [r["action"] for r in self.accounts.audit_rows()])
        self.relay.guide.tick()
        self.assertEqual(self.call("alice", "GET", "/api/epg?channels=ch001")[1]["channels"]["ch001"]["now"],
                         {"title": "快乐大本营", "start": NOW - 60, "end": NOW + 5400})
        manual = self.call("admin", "GET", "/api/admin/epg")[1]["manual"]
        self.assertEqual(manual, [{"id": "ch001", "name": "湖南卫视", "source": URL, "epg": "hn", "epg_name": "芒果台",
                                   "ok": True}])
        # A restart reads the pin back; a reimport that renumbers 湖南卫视 keeps it by name.
        self.relay.guide.configure([], start=False)
        self.manage.shutdown()
        self.manage.server_close()
        self.main.write_text(TABLE.replace("ch001 | 湖南卫视", "ch007 | 湖南卫视"), encoding="utf-8")
        self.relay, self.manage = self.start()
        self.relay.guide.tick()
        self.assertEqual(self.call("alice", "GET", "/api/epg?channels=ch007")[1]["channels"]["ch007"]["now"]["title"],
                         "快乐大本营")
        # Unpinning goes back to automatic matching.
        self.assertTrue(self.call("admin", "POST", "/api/admin/epg/assign", {"channel": "ch007", "epg": ""})[1]["ok"])
        self.relay.guide.tick()
        self.assertNotIn("ch007", self.call("alice", "GET", "/api/epg?channels=ch007")[1]["channels"])


class LogoFallbackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.table = root / "channels.txt"
        self.table.write_text(TABLE, encoding="utf-8")
        (root / "channels.txt.meta.json").write_text(json.dumps({"ch000": {"logo": "https://own.example.com/1.png"}}),
                                                     encoding="utf-8")
        self.fetched = []
        self.icons = {"ch000": "https://epg.example.com/1.png", "ch001": "https://epg.example.com/hn.png",
                      "ch002": "https://epg.example.com/svg"}
        self.cache = logos.LogoCache(root / "logos", self.table, fetcher=self.fetch, extra=lambda: self.icons)

    def tearDown(self):
        self.tmp.cleanup()

    def fetch(self, url):
        self.fetched.append(url)
        return b"<svg onload=alert(1)>" if url.endswith("svg") else PNG

    def test_table_logo_first_then_the_guide_icon(self):
        self.assertEqual(self.cache.get("ch000"), ("image/png", PNG))
        self.assertEqual(self.fetched, ["https://own.example.com/1.png"], "the table's own logo wins")
        self.assertEqual(self.cache.get("ch001"), ("image/png", PNG))
        self.assertEqual(self.fetched[-1], "https://epg.example.com/hn.png")
        self.assertIsNone(self.cache.get("ch002"), "only images are accepted")
        self.assertIsNone(self.cache.get("ch003"))
        self.assertEqual(self.cache.known(), {"ch000", "ch001", "ch002"})

    def test_guide_icons_go_through_the_public_address_check(self):
        resolve = lambda host, port, proto=0: [(2, 1, 6, "", ("192.168.1.1", port))]  # noqa: E731
        self.assertFalse(logos.allowed_url(self.icons["ch001"], resolve=resolve))
        cache = logos.LogoCache(Path(self.tmp.name) / "l2", self.table,
                                fetcher=lambda url: logos.fetch(url, check=lambda u: logos.allowed_url(u, resolve)),
                                extra=lambda: self.icons)
        self.assertIsNone(cache.get("ch001"), "a guide icon on the LAN is not fetched")


if __name__ == "__main__":
    unittest.main()
