"""Group names, recommended guides, logos and tvg ids of imported channels (checklist res-03 and ui-06).

What is checked:
- parsing: M3U `group-title` (quoted, unquoted, `a;b`), `#EXTGRP:`, the TXT
  `分组,#genre#` lines, and the header's `x-tvg-url` / `url-tvg` / `tvg-url`
  (comma lists, several header lines, only http(s), de-duplicated, capped);
- a list's group name maps onto one of our groups only when it means the same
  (央视频道 / CCTV / 📺央视 → 央视; 体育 / 地方 → nothing, so the name decides);
- an import answer carries `group`, `group_title`, `kind` (the mapped group
  first) and `epg_urls`; lines folded into one channel fill its empty tvg
  fields and group;
- merging a duplicate into a channel already in the table (subscription
  refresh) fills its empty logo / tvg-id / tvg-name and never overwrites one;
- the table save takes a kept channel's logo / tvg_id / tvg_name when given
  (an empty string clears it), keeps what was not given, refuses a bad logo
  address and counts the change; the table view returns the three fields;
- the recommended guides are remembered on import and subscription refresh,
  listed in the admin's guide view until added or dismissed, never switched on
  by themselves;
- the logo preview fetches through the logo cache's rules and stores the file;
- the new endpoints answer 404 to members, guests and nobody.
"""

from __future__ import annotations

import base64
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from server import channel_groups, m3u
from server.relay import admin, channeltable, epg, logos, playlist, subscriptions

import test_selfhost_relay as selfhost_base  # the harness only: importing the TestCase classes would run them twice

PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 40

M3U = ('#EXTM3U x-tvg-url="https://epg.example/a.xml, https://epg.example/b.xml.gz,ftp://no/x.xml"\n'
       '#EXTM3U url-tvg="https://epg.example/a.xml"\n'
       '#EXTINF:-1 tvg-id="CCTV1" tvg-logo="https://logo.example/1.png" group-title="📺央视频道",CCTV-1 综合\n'
       "http://a.example/1.m3u8\n"
       "#EXTINF:-1 tvg-name=湖南卫视 group-title=卫视;高清,湖南卫视\nhttp://a.example/2.m3u8\n"
       "#EXTINF:-1,写着 group-title=\"央视\" 的台\n#EXTGRP:体育\nhttp://a.example/3.m3u8\n"
       "#EXTINF:-1 group-title=\"\",没分组\nhttp://a.example/4.m3u8\n"
       '#EXTINF:-1 tvg-logo="https://logo.example/1b.png" tvg-name="CCTV-1",CCTV-1 综合\nhttp://b.example/1.m3u8\n')

TXT = ("央视频道,#genre#\nCCTV1,http://t.example/1#http://t.example/1b\nCCTV2,http://t.example/2\n"
       "地方台,#genre#\n北京新闻,http://t.example/bj\n")


class ParseTests(unittest.TestCase):
    def test_m3u_group_titles(self):
        found = m3u.parse_playlist(M3U)
        self.assertEqual([c["group"] for c in found], ["📺央视频道", "卫视", "体育", "", ""])
        self.assertEqual(found[2]["name"], '写着 group-title="央视" 的台', "an attribute inside the name is not one")

    def test_txt_genre_lines(self):
        found = m3u.parse_playlist(TXT)
        self.assertEqual([(c["name"], c["group"]) for c in found],
                         [("CCTV1", "央视频道"), ("CCTV2", "央视频道"), ("北京新闻", "地方台")])
        self.assertEqual(found[0]["routes"], ["http://t.example/1b"])

    def test_header_guide_addresses(self):
        self.assertEqual(m3u.epg_urls(M3U), ["https://epg.example/a.xml", "https://epg.example/b.xml.gz"])
        self.assertEqual(m3u.epg_urls("#EXTM3U tvg-url='http://e.example/x.xml'\n"), ["http://e.example/x.xml"])
        self.assertEqual(m3u.epg_urls(TXT), [])
        many = "#EXTM3U x-tvg-url=\"" + ",".join(f"https://e.example/{n}.xml" for n in range(10)) + "\"\n"
        self.assertEqual(len(m3u.epg_urls(many)), m3u.EPG_URLS_MAX)
        self.assertEqual(m3u.epg_urls('#EXTINF:-1 x-tvg-url="https://e.example/x.xml",台\nhttp://a/1\n'), [],
                         "only the #EXTM3U header recommends a guide")

    def test_group_names_map_only_when_they_mean_the_same(self):
        title = channel_groups.from_title
        for text in ("央视", "央视频道", "📺 CCTV", "cctv高清", "中央台"):
            self.assertEqual(title(text), "央视", text)
        self.assertEqual(title("卫视频道"), "卫视")
        self.assertEqual(title("香港卫视"), "港澳台", "Hong Kong before 卫视")
        self.assertEqual(title("港澳台"), "港澳台")
        self.assertEqual(title("少儿动画"), "动画影视")
        self.assertEqual(title("Music"), "音乐")
        self.assertEqual(title("海外频道"), "国际")
        for text in ("体育", "地方台", "4K", "", "🎉", None, 3):
            self.assertEqual(title(text), "", text)


class ReadTests(unittest.TestCase):
    def test_import_answer_carries_groups_and_guides(self):
        answer = playlist.read({"text": M3U})
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(answer["epg_urls"], ["https://epg.example/a.xml", "https://epg.example/b.xml.gz"])
        by_name = {e["name"]: e for e in answer["entries"]}
        cctv = by_name["CCTV-1 综合"]
        self.assertEqual((cctv["group"], cctv["group_title"]), ("央视", "📺央视频道"))
        self.assertEqual((cctv["tvg_id"], cctv["logo"]), ("CCTV1", "https://logo.example/1.png"),
                         "the first line's logo stays")
        self.assertEqual(cctv["tvg_name"], "CCTV-1", "an empty field is filled from the folded line")
        self.assertEqual([r["url"] for r in cctv["routes"]], ["http://b.example/1.m3u8"])
        self.assertEqual((by_name["湖南卫视"]["group"], by_name["没分组"]["group"]), ("卫视", ""))
        self.assertEqual(by_name['写着 group-title="央视" 的台']["group_title"], "体育")
        playlist.mark(answer["entries"], [])
        self.assertEqual(by_name["CCTV-1 综合"]["kind"], "央视")
        self.assertEqual(by_name["没分组"]["kind"], channel_groups.classify("", "没分组"))

    def test_mapped_group_wins_over_the_name_for_kind(self):
        answer = playlist.read({"text": "#EXTM3U\n#EXTINF:-1 group-title=\"少儿\",金鹰卡通\nhttp://a/1\n"})
        playlist.mark(answer["entries"], [])
        self.assertEqual(answer["entries"][0]["kind"], "动画影视")


class MergeFillTests(unittest.TestCase):
    ROWS = [{"id": "ch000", "name": "CCTV1", "url": "https://main/1"},
            {"id": "ch001", "name": "湖南卫视", "url": "https://main/2"}]

    def test_empty_fields_are_filled_and_set_ones_kept(self):
        meta = {"ch001": {"logo": "https://mine/hunan.png"}}
        entries = [{"name": "CCTV-1 综合", "url": "https://n/1", "agent": "", "tvg_id": "CCTV1",
                    "logo": "https://logo.example/1.png"},
                   {"name": "CCTV1 HD", "url": "https://n/1b", "agent": "", "logo": "https://logo.example/other.png",
                    "tvg_name": "CCTV-1"},
                   {"name": "湖南卫视", "url": "https://main/2", "agent": "", "logo": "https://logo.example/2.png",
                    "tvg_id": "HunanTV"},
                   {"name": "坏台标", "url": "https://n/x", "agent": "", "logo": "javascript:alert(1)"}]
        result = subscriptions.merge_lines(meta, self.ROWS, entries)
        self.assertEqual(meta["ch000"]["logo"], "https://logo.example/1.png", "first match fills, the next does not replace")
        self.assertEqual((meta["ch000"]["tvg_id"], meta["ch000"]["tvg_name"]), ("CCTV1", "CCTV-1"))
        self.assertEqual(meta["ch001"]["logo"], "https://mine/hunan.png", "never overwritten")
        self.assertEqual(meta["ch001"]["tvg_id"], "HunanTV")
        self.assertEqual(result["filled"], 2)
        self.assertEqual(result["new"], 1)
        again = subscriptions.merge_lines(meta, self.ROWS, entries)
        self.assertEqual(again["filled"], 0, "nothing left to fill")

    def test_fill_meta_and_problems(self):
        entry = {"tvg_id": "A"}
        self.assertEqual(channeltable.fill_meta(entry, {"tvg_id": "B", "logo": "https://l/x.png", "tvg_name": ""}), ["logo"])
        self.assertEqual(entry, {"tvg_id": "A", "logo": "https://l/x.png"})
        rows = [{"id": "ch000", "name": "一"}, {"id": "ch001", "name": "二"}]
        self.assertEqual(channeltable.meta_problems([{"id": "ch000", "logo": ""}, {"id": "ch001", "tvg_id": "x" * 65}], rows),
                         ["第 2 行（二）：节目单编号不对（最多 64 个字符）"])
        self.assertIn("台标地址不对", channeltable.meta_problems([{"logo": "ftp://x/y.png"}], rows[:1])[0])

    def test_table_save_edits_one_field_and_keeps_the_rest(self):
        data = {"ch000": {"tvg_id": "CCTV1", "logo": "https://l/1.png", "routes": [{"url": "https://b/1"}]}}
        rows = [{"id": "ch000", "name": "CCTV1", "url": "https://main/1"}]
        channeltable.merge_meta(data, [{"id": "ch000", "logo": "https://l/new.png"}], rows)
        self.assertEqual(data["ch000"]["logo"], "https://l/new.png")
        self.assertEqual(data["ch000"]["tvg_id"], "CCTV1")
        self.assertEqual(data["ch000"]["routes"], [{"url": "https://b/1"}])
        channeltable.merge_meta(data, [{"id": "ch000", "tvg_id": "", "tvg_name": "CCTV-1"}], rows)
        self.assertEqual({k: data["ch000"].get(k) for k in channeltable.META_FIELDS},
                         {"tvg_id": None, "tvg_name": "CCTV-1", "logo": "https://l/new.png"})
        channeltable.merge_meta(data, ["ch000"], rows)
        self.assertEqual(data["ch000"]["tvg_name"], "CCTV-1", "an id alone keeps everything")


class RefreshSuggestsTests(unittest.TestCase):
    def test_subscription_refresh_hands_the_guides_over(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            table = Path(tmp) / "channels.txt"
            table.write_text("ch000 | CCTV1 | https://main/1\n", encoding="utf-8")
            heard = []
            subs = subscriptions.Subscriptions(lambda: table, read=lambda body: playlist.read({"text": M3U}),
                                               log=lambda _m: None, suggest=lambda urls, origin: heard.append((urls, origin)))
            answer = subs.add("https://list.example/a.m3u", "我的列表")
            self.assertTrue(answer["ok"], answer)
            self.assertEqual(heard, [(["https://epg.example/a.xml", "https://epg.example/b.xml.gz"], "我的列表")])
            self.assertEqual(channeltable.meta_of(table)["ch000"],
                             {"tvg_id": "CCTV1", "tvg_name": "CCTV-1", "logo": "https://logo.example/1.png"})
            self.assertEqual(answer["result"]["filled"], 1)


class FakeIpTests(unittest.TestCase):
    def test_proxy_fake_ip_counts_as_public(self):
        def resolver(addresses):
            return lambda host, port, proto=0: [(0, 0, 6, "", (a, port)) for a in addresses]
        for address in ("198.18.0.17", "198.19.255.1", "2001:2::11"):
            self.assertTrue(logos.allowed_url("https://logo.example/a.png", resolve=resolver([address])), address)
        for address in ("198.20.0.1", "10.0.0.1", "192.168.1.1", "127.0.0.1"):
            self.assertEqual(logos.allowed_url("https://logo.example/a.png", resolve=resolver([address])),
                             address == "198.20.0.1", address)


class AdminEndpointTests(selfhost_base.RelayHarness):
    def setUp(self):
        super().setUp()
        root = Path(self.tmp.name)
        self.admin = self.accounts.create_user("管理员", "admin")
        member = self.accounts.create_user("家人")
        self.cookies = {"admin": "tvs=" + self.accounts.new_session(self.admin),
                        "member": "tvs=" + self.accounts.new_session(member), "nobody": ""}
        self.main.write_text("ch000 | CCTV1 | https://x/1.m3u8\nch001 | 湖南卫视 | https://x/2.m3u8\n", encoding="utf-8")
        self.guide = epg.attach(self.relay, root / "epg")
        self.guide.autostart = False
        self.fetched = []

        def fetcher(url):
            self.fetched.append(url)
            if "192.168." in url:
                raise ValueError("地址不允许")
            return b"<svg/>" if url.endswith(".svg") else PNG
        self.relay.logos = logos.LogoCache(root / "logos", self.main, fetcher=fetcher)

    def tearDown(self):
        self.guide.configure([], start=False)
        super().tearDown()

    def call(self, who, method, path, body=None):
        status, _, answer = self.request(method, "/tv" + path, body,
                                         headers={"Cookie": self.cookies[who]} if self.cookies[who] else {})
        return status, answer

    def test_new_endpoints_are_admin_only(self):
        for who in ("member", "nobody"):
            for path, body in (("/api/admin/channels/logo", {"url": "https://logo.example/a.png"}),
                               ("/api/admin/epg/dismiss", {"url": "https://epg.example/a.xml"})):
                self.assertEqual(self.call(who, "POST", path, body)[0], 404, (who, path))
        self.assertEqual(self.fetched, [], "a member's request fetches nothing")

    def test_logo_preview(self):
        status, answer = self.call("admin", "POST", "/api/admin/channels/logo", {"url": "https://logo.example/a.png"})
        self.assertEqual(status, 200)
        self.assertEqual(answer["image"], "data:image/png;base64," + base64.b64encode(PNG).decode())
        self.assertEqual(len(list((Path(self.tmp.name) / "logos").glob("*.png"))), 1, "stored in the logo cache")
        self.call("admin", "POST", "/api/admin/channels/logo", {"url": "https://logo.example/a.png"})
        self.assertEqual(self.fetched, ["https://logo.example/a.png"], "the second look comes from the cache")
        for url, words in (("http://192.168.1.1/a.png", "局域网"), ("https://logo.example/a.svg", "不是认得的图片"),
                           ("javascript:alert(1)", "http://"), ("", "请填")):
            answer = self.call("admin", "POST", "/api/admin/channels/logo", {"url": url})[1]
            self.assertFalse(answer["ok"], url)
            self.assertIn(words, answer["error"], url)

    def test_table_edits_logo_and_tvg_id(self):
        view = self.call("admin", "GET", "/api/admin/channels")[1]
        self.assertEqual({k: view["rows"][0][k] for k in channeltable.META_FIELDS}, {"tvg_id": "", "tvg_name": "", "logo": ""})
        body = {"version": view["version"], "rows": [{"id": "ch000", "logo": "https://logo.example/1.png", "tvg_id": "CCTV1"},
                                                     "ch001"]}
        status, answer = self.call("member", "POST", "/api/admin/channels", body)
        self.assertEqual(status, 404)
        answer = self.call("admin", "POST", "/api/admin/channels", body)[1]
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(answer["summary"]["meta"], 1)
        self.assertEqual(channeltable.meta_of(self.main), {"ch000": {"tvg_id": "CCTV1", "logo": "https://logo.example/1.png"}})
        view = self.call("admin", "GET", "/api/admin/channels")[1]
        self.assertEqual((view["rows"][0]["logo"], view["rows"][0]["tvg_id"]), ("https://logo.example/1.png", "CCTV1"))
        bad = {"version": view["version"], "rows": [{"id": "ch000", "logo": "file:///etc/passwd"}, "ch001"]}
        answer = self.call("admin", "POST", "/api/admin/channels", bad)[1]
        self.assertFalse(answer["ok"])
        self.assertIn("台标地址不对", answer["error"])
        clear = {"version": view["version"], "rows": [{"id": "ch000", "logo": ""}, "ch001"]}
        self.assertTrue(self.call("admin", "POST", "/api/admin/channels", clear)[1]["ok"])
        self.assertEqual(channeltable.meta_of(self.main), {"ch000": {"tvg_id": "CCTV1"}})

    def test_imported_new_channel_takes_the_list_group(self):
        entries = self.call("admin", "POST", "/api/admin/channels/import", {"text": M3U})[1]["entries"]
        new = next(e for e in entries if e["name"] == "湖南卫视")
        self.assertEqual(new["dup"], "name")
        version = self.call("admin", "GET", "/api/admin/channels")[1]["version"]
        extra = {"name": "少儿台", "url": "https://x/kids.m3u8", "agent": "", "group": channel_groups.from_title("少儿频道")}
        answer = self.call("admin", "POST", "/api/admin/channels", {"version": version, "rows": ["ch000", "ch001", extra]})[1]
        self.assertTrue(answer["ok"], answer)
        rows = self.call("admin", "GET", "/api/admin/channels")[1]["rows"]
        self.assertEqual((rows[2]["name"], rows[2]["group"]), ("少儿台", "动画影视"))
        self.assertEqual(rows[1]["group"], "", "the existing channel's group is untouched")

    def test_recommended_guides_are_listed_until_added_or_dismissed(self):
        self.assertEqual(self.call("admin", "GET", "/api/admin/epg")[1]["suggested"], [])
        answer = self.call("admin", "POST", "/api/admin/channels/import", {"text": M3U})[1]
        self.assertEqual(len(answer["epg_urls"]), 2)
        view = self.call("admin", "GET", "/api/admin/epg")[1]
        self.assertEqual([s["url"] for s in view["suggested"]], ["https://epg.example/a.xml", "https://epg.example/b.xml.gz"])
        self.assertEqual(view["suggested"][0]["from"], "上传的文件")
        self.assertEqual(view["sources"], [], "never switched on by itself")
        self.assertFalse(self.guide.enabled)
        # One click adds it (the page posts the sources); it leaves the suggestions.
        self.assertTrue(self.call("admin", "POST", "/api/admin/epg", {"sources": ["https://epg.example/a.xml"]})[1]["ok"])
        view = self.call("admin", "GET", "/api/admin/epg")[1]
        self.assertEqual([s["url"] for s in view["suggested"]], ["https://epg.example/b.xml.gz"])
        # Not wanted: gone, and a later import of the same list does not bring it back.
        self.assertTrue(self.call("admin", "POST", "/api/admin/epg/dismiss", {"url": "https://epg.example/b.xml.gz"})[1]["ok"])
        self.assertFalse(self.call("admin", "POST", "/api/admin/epg/dismiss", {"url": "https://epg.example/b.xml.gz"})[1]["ok"])
        self.assertIn("epg.dismiss", [r["action"] for r in self.accounts.audit_rows()])
        self.call("admin", "POST", "/api/admin/channels/import", {"text": M3U})
        self.assertEqual(self.call("admin", "GET", "/api/admin/epg")[1]["suggested"], [])
        stored = json.loads(self.accounts.meta(epg.SUGGEST_KEY))
        self.assertTrue(all(epg.valid_url(item["url"]) for item in stored))

    def test_suggestions_are_capped_and_newest_first(self):
        for n in range(epg.SUGGEST_MAX + 3):
            epg.suggest(self.relay, [f"https://epg.example/{n}.xml"], f"列表{n}")
        view = epg.admin_view(self.relay)
        self.assertEqual(len(view["suggested"]), epg.SUGGEST_MAX)
        self.assertEqual(view["suggested"][0]["url"], f"https://epg.example/{epg.SUGGEST_MAX + 2}.xml")
        self.assertEqual(epg.suggest(self.relay, ["not a url", "ftp://x/y"], "x"), 0)


if __name__ == "__main__":
    unittest.main()
