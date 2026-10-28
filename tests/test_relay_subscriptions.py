"""Subscribed live playlists (server/relay/subscriptions.py, Admin → 频道表 → 导入).

What is checked: channel names are matched leniently (case, spaces, dashes,
「综合」「HD」 and brackets ignored; CCTV by number; 4K kept apart) or by tvg-id;
a refresh fills free backup slots of channels already in the table, replaces
only backup lines whose check failed once the slots are full, never touches a
main line or a working line and never adds a line twice; channels not in the
table are counted and named, never added; subscriptions are stored next to the
table (mode 600) with at most SUBS_MAX, refuse bad and duplicate addresses,
remember failures and retry them after RETRY_S, successes after EVERY_S;
nothing is logged with an address; the admin endpoints add (also a listed
public preset), refresh and delete for admins only, and the cloud relay has no
subscriptions.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from server import routes
from server.relay import presets, subscriptions

import test_relay_admin as admin_base     # functions only: inheriting the TestCase would run its tests twice

TABLE = ("ch000 | CCTV1 | https://main/1.m3u8\n"
         "ch001 | 湖南卫视 | https://main/2.m3u8\n"
         "ch002 | 湖南卫视4K | https://main/3.m3u8\n")


def entry(name, url, routes_=(), tvg_id=""):
    found = {"name": name, "url": url, "agent": "", "routes": [{"url": u} for u in routes_]}
    if tvg_id:
        found["tvg_id"] = tvg_id
    return found


class KeyTests(unittest.TestCase):
    def test_names_that_mean_the_same_channel(self):
        key = subscriptions.channel_key
        self.assertEqual({key(n) for n in ("CCTV1", "CCTV-1 综合", "cctv 1 HD", "CCTV1(1080p)", "CCTV-01")}, {"cctv1"})
        self.assertEqual(key("CCTV5+ 体育赛事"), "cctv5+")
        self.assertNotEqual(key("CCTV5"), key("CCTV5+"))
        self.assertNotEqual(key("CCTV1"), key("CCTV10"))
        self.assertEqual(key("湖南卫视 HD"), key("湖南卫视"))
        self.assertEqual(key("湖南卫视高清"), key("湖南卫视"))
        self.assertNotEqual(key("湖南卫视4K"), key("湖南卫视"))
        self.assertNotEqual(key("CCTV4K"), key("CCTV4"))


class MergeTests(unittest.TestCase):
    def setUp(self):
        self.rows = [{"id": "ch000", "name": "CCTV1", "url": "https://main/1.m3u8"},
                     {"id": "ch001", "name": "湖南卫视", "url": "https://main/2.m3u8"}]

    def test_new_lines_fill_free_slots_without_duplicates(self):
        meta = {"ch000": {"routes": [{"url": "https://old/1"}]}}
        result = subscriptions.merge_lines(meta, self.rows, [
            entry("CCTV-1 综合", "https://new/1", ["https://old/1", "https://main/1.m3u8", "https://new/2"]),
            entry("cctv1 HD", "https://new/1")])
        self.assertEqual([r["url"] for r in meta["ch000"]["routes"]], ["https://old/1", "https://new/1", "https://new/2"])
        self.assertEqual((result["channels"], result["added"], result["replaced"]), (1, 2, 0))

    def test_full_slots_only_replace_lines_that_failed(self):
        backups = [{"url": f"https://b/{i}"} for i in range(routes.BACKUPS_MAX)]
        meta = {"ch000": {"routes": backups, "checks": {
            routes.fingerprint("https://b/1"): {"ok": False, "why": "x", "at": 1},
            routes.fingerprint("https://b/2"): {"ok": True, "why": "", "at": 1}}}}
        result = subscriptions.merge_lines(meta, self.rows, [entry("CCTV1", "https://n/1", ["https://n/2"])])
        urls = [r["url"] for r in meta["ch000"]["routes"]]
        self.assertEqual(urls, ["https://b/0", "https://n/1", "https://b/2", "https://b/3"])
        self.assertEqual((result["added"], result["replaced"]), (0, 1))

    def test_channels_not_in_the_table_are_counted_not_added(self):
        meta = {}
        result = subscriptions.merge_lines(meta, self.rows, [entry("东方卫视", "https://n/9"), entry("东方卫视 HD", "https://n/8"),
                                                             entry("北京卫视", "https://n/7")])
        self.assertEqual(meta, {})
        self.assertEqual((result["new"], result["new_names"]), (2, ["东方卫视", "北京卫视"]))

    def test_tvg_id_matches_too_and_main_lines_are_left_alone(self):
        meta = {"ch001": {"tvg_id": "HunanTV"}}
        subscriptions.merge_lines(meta, self.rows, [entry("Hunan Satellite", "https://n/hn", tvg_id="hunantv")])
        self.assertEqual(meta["ch001"]["routes"], [{"url": "https://n/hn"}])
        self.assertEqual(self.rows[1]["url"], "https://main/2.m3u8")


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.table = Path(self.tmp.name) / "channels.txt"
        self.table.write_text(TABLE, encoding="utf-8")
        self.t = 1_000_000.0
        self.logs: list[str] = []
        self.answer = {"ok": True, "total": 2, "entries": [entry("CCTV1 HD", "https://sub/1?token=s3cret"),
                                                            entry("东方卫视", "https://sub/9")]}
        self.asked: list[dict] = []
        self.subs = subscriptions.Subscriptions(lambda: self.table, read=self.read, clock=lambda: self.t,
                                                log=self.logs.append)

    def read(self, body):
        self.asked.append(body)
        return self.answer

    def test_adding_reads_at_once_and_merges(self):
        answer = self.subs.add("https://lists.example/cn.m3u?token=s3cret", "")
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(answer["result"]["added"], 1)
        self.assertEqual(answer["result"]["new"], 1)
        self.assertEqual(routes.entry_of(self.table, "ch000")["routes"], [{"url": "https://sub/1?token=s3cret"}])
        view = self.subs.view()["subs"][0]
        self.assertEqual((view["name"], view["ok"], view["last_at"]), ("lists.example", True, int(self.t)))
        mode = stat.S_IMODE(os.stat(subscriptions.subs_path(self.table)).st_mode)
        self.assertEqual(mode, 0o600)
        self.assertTrue(self.logs, "a refresh that added lines says so")
        self.assertFalse(any("s3cret" in m or "https://" in m or "/cn.m3u" in m for m in self.logs), self.logs)

    def test_bad_duplicate_and_too_many(self):
        self.assertFalse(self.subs.add("ftp://x/list", "")["ok"])
        self.assertFalse(self.subs.add("https://x/a b", "")["ok"])
        self.assertTrue(self.subs.add("https://x/1", "一")["ok"])
        self.assertEqual(self.subs.add("https://x/1", "")["error"], "已经订阅过这个地址了")
        for i in range(2, subscriptions.SUBS_MAX + 1):
            self.assertTrue(self.subs.add(f"https://x/{i}", "")["ok"])
        self.assertIn("最多", self.subs.add("https://x/99", "")["error"])

    def test_schedule_failures_and_removal(self):
        sid = self.subs.add("https://x/1", "一")["result"] and self.subs.view()["subs"][0]["id"]
        self.assertEqual(self.subs.due(), [])
        self.t += subscriptions.EVERY_S
        self.assertEqual(self.subs.due(), [sid])
        self.answer = {"ok": False, "error": "取不到这个地址（HTTP 404）"}
        self.assertFalse(self.subs.refresh(sid)["ok"])
        self.assertEqual(self.subs.view()["subs"][0]["error"], "取不到这个地址（HTTP 404）")
        self.assertEqual(self.subs.due(), [])
        self.t += subscriptions.RETRY_S
        self.assertEqual(self.subs.due(), [sid], "a failed list is retried sooner")
        self.assertTrue(self.subs.remove(sid)["ok"])
        self.assertEqual(self.subs.view()["subs"], [])
        self.assertFalse(self.subs.remove(sid)["ok"])
        self.assertEqual(routes.entry_of(self.table, "ch000")["routes"], [{"url": "https://sub/1?token=s3cret"}],
                         "lines already added stay")


class EndpointTests(unittest.TestCase):
    """The admin API fixture (a cloud relay), with a subscription store attached by hand where needed."""
    setUp = admin_base.AdminApiTests.setUp
    tearDown = admin_base.AdminApiTests.tearDown
    raw = admin_base.AdminApiTests.raw
    call = admin_base.AdminApiTests.call
    audits = admin_base.AdminApiTests.audits

    def test_cloud_relay_has_no_subscriptions(self):
        status, answer = self.call("admin", "GET", "/api/admin/subs")
        self.assertEqual(status, 200)
        self.assertFalse(answer["ok"])
        self.assertFalse(self.call("admin", "POST", "/api/admin/subs", {"url": "https://x/1"})[1]["ok"])

    def test_admins_add_refresh_and_delete(self):
        read = []
        self.relay.subscriptions = subscriptions.Subscriptions(
            lambda: self.main, read=lambda body: read.append(body) or {"ok": True, "total": 1,
                                                                       "entries": [entry("CCTV-1", "https://s/1")]},
            log=lambda _m: None)
        self.assertEqual(self.call("alice", "GET", "/api/admin/subs")[0], 404)
        self.assertEqual(self.call("alice", "POST", "/api/admin/subs", {"url": "https://x/1"})[0], 404)
        status, answer = self.call("admin", "POST", "/api/admin/subs", {"url": "https://x/1", "name": "我的列表"})
        self.assertTrue(answer["ok"], answer)
        preset = presets.PRESETS[0]
        status, answer = self.call("admin", "POST", "/api/admin/subs", {"preset": preset["id"], "mirror": 0})
        self.assertTrue(answer["ok"], answer)
        subs = self.call("admin", "GET", "/api/admin/subs")[1]["subs"]
        self.assertEqual([s["name"] for s in subs], ["我的列表", preset["name"]])
        self.assertEqual(read[-1], {"url": preset["url"]})
        self.assertFalse(self.call("admin", "POST", "/api/admin/subs", {"preset": "nope"})[1]["ok"])
        self.assertTrue(self.call("admin", "POST", "/api/admin/subs/refresh", {"id": subs[0]["id"]})[1]["ok"])
        self.assertTrue(self.call("admin", "POST", "/api/admin/subs/delete", {"id": subs[0]["id"]})[1]["ok"])
        self.assertEqual(len(self.call("admin", "GET", "/api/admin/subs")[1]["subs"]), 1)
        self.assertEqual(len(self.audits("channels.subscribe")), 2)
        self.assertEqual(len(self.audits("channels.unsubscribe")), 1)


if __name__ == "__main__":
    unittest.main()
