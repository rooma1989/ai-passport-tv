"""Logo fallback from a public logo set (server/relay/logoset.py, checklist r2-08).

What is checked:
- channel names map onto the set's file names the way the guide matches names
  (CCTV-1 综合 / CCTV1 HD / ＣＣＴＶ－１ → CCTV1, CCTV-5+ 体育赛事 → CCTV5+, the
  CCTV4 Europe / America and CCTV4K / CCTV8K channels apart, 湖南卫视 HD → 湖南卫视),
  the alias table covers the common other names (上海卫视 → 东方卫视, 凤凰卫视 →
  凤凰卫视中文台, CCTV新闻 → CCTV13), a 4K channel without its own logo falls back
  to the plain one, tvg-name and tvg-id are tried after the name, and an
  unknown name gives nothing (no request is made for it);
- every one of CCTV1–17 and 5+, the 31 provincial satellite channels, CGTN and
  the Phoenix channels is found, written several common ways;
- the address is the jsDelivr mirror with the file name percent-encoded;
- the logo cache uses the set only for channels that have neither a table logo
  nor a guide icon, fetches it through the same fetcher (so the same public-
  address, image-type and size rules), says where each logo comes from, and
  asks for nothing from the set when it is switched off;
- the admin switch is stored, admin only, and the channel table view reports
  where a channel's logo comes from and how many channels the set knows.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from server.relay import channeltable, logos, logoset

import test_selfhost_relay as selfhost_base  # the harness only

PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 40
CCTV_WORDS = ("综合", "财经", "综艺", "中文国际", "体育", "电影", "国防军事", "电视剧", "纪录", "科教", "戏曲", "社会与法",
              "新闻", "少儿", "音乐", "奥林匹克", "农业农村")
SATELLITE = ("北京 天津 河北 山西 内蒙古 辽宁 吉林 黑龙江 东方 江苏 浙江 安徽 东南 江西 山东 河南 湖北 湖南 广东 广西 "
             "海南 重庆 四川 贵州 云南 西藏 陕西 甘肃 青海 宁夏 新疆").split()


class NameTests(unittest.TestCase):
    def test_spellings_map_to_one_file(self):
        f = logoset.file_for
        self.assertEqual({f(n) for n in ("CCTV1", "CCTV-1 综合", "cctv1 HD", "CCTV1高清", "ＣＣＴＶ－１", "[HD]CCTV1")},
                         {"CCTV1.png"})
        self.assertEqual(f("CCTV-5+ 体育赛事"), "CCTV5+.png")
        self.assertEqual(f("CCTV5"), "CCTV5.png")
        self.assertEqual(f("CCTV-4 欧洲"), "CCTV4欧洲.png")
        self.assertEqual(f("CCTV4 美洲"), "CCTV4美洲.png")
        self.assertEqual(f("CCTV4K 超高清"), "CCTV4K.png")
        self.assertEqual(f("CCTV8K"), "CCTV8K.png")
        self.assertEqual(f("湖南卫视 HD"), "湖南卫视.png")
        self.assertEqual(f("湖南卫视4K"), "湖南卫视.png", "no own 4K logo: the plain one")
        self.assertEqual(f("北京卫视4K"), "北京卫视4K.png", "its own 4K logo")
        self.assertEqual(f("上海卫视"), "东方卫视.png")
        self.assertEqual(f("福建东南卫视"), "东南卫视.png")
        self.assertEqual(f("凤凰卫视"), "凤凰卫视中文台.png")
        self.assertEqual(f("CCTV新闻"), "CCTV13.png")
        self.assertEqual(f("移动戏曲"), "移动戏曲.PNG", "a file that is not .png keeps its own name")
        self.assertEqual(f("没有这个台"), "")
        self.assertEqual(f("", None, "我的台"), "")
        self.assertEqual(f("我的台", "CCTV-2 财经"), "CCTV2.png", "tvg-name after the name")
        self.assertEqual(f("我的台", "", "CCTV3"), "CCTV3.png", "tvg-id last")

    def test_common_channels_are_all_found(self):
        names = [f"CCTV{i}" for i in range(1, 18)] + ["CCTV5+"]
        names += [f"CCTV-{i} {w}" for i, w in enumerate(CCTV_WORDS, 1)] + [f"CCTV{w}" for w in CCTV_WORDS]
        names += [s + "卫视" for s in SATELLITE] + [s + "卫视 HD" for s in SATELLITE] + [s + "卫视高清" for s in SATELLITE]
        names += ["CGTN", "CGTN 纪录", "凤凰卫视", "凤凰中文", "凤凰卫视中文台", "凤凰资讯", "凤凰卫视资讯台", "凤凰香港"]
        self.assertEqual(len(SATELLITE), 31)
        self.assertEqual([n for n in names if not logoset.file_for(n)], [])

    def test_url_is_the_mirror_with_the_name_encoded(self):
        self.assertEqual(logoset.url_for("CCTV-5+ 体育赛事"), logoset.BASE + "CCTV5%2B.png")
        url = logoset.url_for("湖南卫视")
        self.assertTrue(url.startswith("https://cdn.jsdelivr.net/gh/fanmingming/live@main/tv/"))
        self.assertEqual(urllib.parse.unquote(url.rsplit("/", 1)[1]), "湖南卫视.png")
        self.assertEqual(logoset.url_for("没有这个台"), "")


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.table = root / "channels.txt"
        self.table.write_text("ch000 | CCTV-1 综合 | https://x/1\nch001 | 湖南卫视 | https://x/2\n"
                              "ch002 | 我家的台 | https://x/3\nch003 | 东方卫视 | https://x/4\n", encoding="utf-8")
        channeltable.save_meta(self.table, {"ch001": {"logo": "https://own.example/hn.png"}})
        self.fetched = []
        self.on = True

        def fetcher(url):
            self.fetched.append(url)
            return PNG
        self.cache = logos.LogoCache(root / "logos", self.table, fetcher=fetcher,
                                     extra=lambda: {"ch003": "https://epg.example/df.png"}, public=lambda: self.on)

    def test_order_and_sources(self):
        self.assertEqual(self.cache.sources(), {"ch000": "set", "ch001": "table", "ch003": "epg"})
        self.assertEqual(self.cache.known(), {"ch000", "ch001", "ch003"})
        self.assertEqual(self.cache.get("ch000"), ("image/png", PNG))
        self.assertEqual(self.fetched, [logoset.BASE + "CCTV1.png"])
        self.cache.get("ch001")
        self.cache.get("ch003")
        self.assertEqual(self.fetched[1:], ["https://own.example/hn.png", "https://epg.example/df.png"])
        self.assertIsNone(self.cache.get("ch002"), "a name the set does not know: nothing, no request")
        self.assertEqual(len(self.fetched), 3)

    def test_same_rules_as_any_logo(self):
        self.cache.fetcher = lambda url: b"<svg onload=alert(1)>"
        self.assertIsNone(self.cache.get("ch000"), "only images")
        self.cache.fetcher = lambda url: b"\x89PNG\r\n\x1a\n" + b"\0" * logos.MAX_BYTES
        self.cache._failed.clear()
        self.assertIsNone(self.cache.get("ch000"), "size cap")
        self.assertFalse(logos.allowed_url(logoset.BASE + "CCTV1.png", resolve=lambda *a, **k: [(0, 0, 0, "", ("10.0.0.2", 443))]),
                         "the set's host resolving to a private address is refused like any other")

    def test_busy_is_not_the_same_as_no_logo(self):
        """The Live page asks for dozens of logos at once; a request that waits too long for a free slot gets a
        no-store 503 (pending), not the cacheable 404 that would keep the placeholder for an hour."""
        self.assertTrue(self.cache.pending("ch000"), "has an address, not fetched yet")
        self.assertFalse(self.cache.pending("ch002"), "no logo anywhere: a real 404")
        self.cache.get("ch000")
        self.assertFalse(self.cache.pending("ch000"), "stored")
        self.cache.fetcher = lambda url: (_ for _ in ()).throw(OSError("down"))
        self.assertIsNone(self.cache.get("ch003"))
        self.assertFalse(self.cache.pending("ch003"), "failed just now: wait RETRY_S like before")

    def test_switched_off_asks_nothing(self):
        self.on = False
        self.assertEqual(self.cache.sources(), {"ch001": "table", "ch003": "epg"})
        self.assertIsNone(self.cache.get("ch000"))
        self.assertEqual(self.fetched, [])

    def test_table_change_is_seen(self):
        self.assertNotIn("ch002", self.cache.known())
        text = self.table.read_text(encoding="utf-8").replace("我家的台", "CCTV13")
        self.table.write_text(text, encoding="utf-8")
        os.utime(self.table, (1, 2))
        self.assertIn("ch002", self.cache.known())


class AdminTests(selfhost_base.RelayHarness):
    def setUp(self):
        super().setUp()
        root = Path(self.tmp.name)
        self.admin = self.accounts.create_user("管理员", "admin")
        member = self.accounts.create_user("家人")
        self.cookies = {"admin": "tvs=" + self.accounts.new_session(self.admin),
                        "member": "tvs=" + self.accounts.new_session(member)}
        self.main.write_text("ch000 | CCTV1 | https://x/1.m3u8\nch001 | 我家的台 | https://x/2.m3u8\n", encoding="utf-8")
        self.fetched = []
        self.relay.logos = logos.LogoCache(root / "logos", self.main, fetcher=lambda url: self.fetched.append(url) or PNG,
                                           public=lambda: logoset.enabled(self.relay))

    def call(self, who, method, path, body=None):
        status, _, answer = self.request(method, "/tv" + path, body, headers={"Cookie": self.cookies[who]})
        return status, answer

    def test_switch_and_view(self):
        view = self.call("admin", "GET", "/api/admin/channels")[1]
        self.assertEqual(view["logoset"]["on"], True)
        self.assertEqual(view["logoset"]["hits"], 1)
        self.assertEqual([r["logo_from"] for r in view["rows"]], ["set", ""])
        self.assertEqual(self.call("member", "POST", "/api/admin/channels/logoset", {"on": False})[0], 404)
        self.assertEqual(self.call("admin", "POST", "/api/admin/channels/logoset", {"on": "no"})[0], 400)
        self.assertTrue(self.call("admin", "POST", "/api/admin/channels/logoset", {"on": False})[1]["ok"])
        self.assertEqual(self.accounts.meta(logoset.META_KEY), "0")
        view = self.call("admin", "GET", "/api/admin/channels")[1]
        self.assertEqual((view["logoset"]["on"], [r["logo_from"] for r in view["rows"]]), (False, ["", ""]))
        self.assertIsNone(self.relay.logos.get("ch000"))
        self.assertEqual(self.fetched, [])
        self.relay._logoset_on = None            # read back from the database, as after a restart
        self.assertFalse(logoset.enabled(self.relay))
        self.call("admin", "POST", "/api/admin/channels/logoset", {"on": True})
        self.assertIsNotNone(self.relay.logos.get("ch000"))
        self.assertEqual(self.fetched, [logoset.BASE + "CCTV1.png"])


if __name__ == "__main__":
    unittest.main()
