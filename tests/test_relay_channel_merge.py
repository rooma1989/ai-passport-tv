"""Merging duplicate channels in the admin channel table (server/relay/channelmerge.py, checklist r2-09).

What is checked:
- duplicates are found by the subscription merge's name key (CCTV5 / CCTV-5 体育 /
  cctv5 HD are one; CCTV5+, CCTV4 欧洲 and CCTV8K are not CCTV5 / CCTV4 / CCTV8)
  or by tvg-id; a group joined only by tvg-id is offered unticked;
- the suggested keeper is the one with the most usable lines, then lines that
  connect, then most lines, a logo, a tvg-id, and the earlier position;
- a merge keeps at most ROUTES_MAX lines, best first (usable > connects but
  weak > not checked > failing), drops a duplicate address, fills the keeper's
  empty logo / tvg fields without overwriting, removes the other channels, and
  goes through the ordinary save: dry run first, backups of channels.txt and
  its sidecar, a stale version refused, nothing changes on a dry run;
- the moved lines keep their check records and measured quality; the admin's
  group, each person's group overrides, the frequent-channel tally and a manual
  guide pin move from the removed channel to the kept one (the kept one's own
  are never replaced);
- picks that are not one detected group, unknown ids and ids used twice are
  refused; the endpoint is admin only.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from server import routes
from server.relay import admin, channelmerge, channeltable, epg, livetally, subscriptions

import test_selfhost_relay as selfhost_base  # the harness only

TABLE = ("ch000 | CCTV5 | https://a/5.m3u8\n"
         "ch001 | 湖南卫视 | https://a/hn.m3u8\n"
         "ch002 | CCTV-5 体育 | https://b/5.m3u8\n"
         "ch003 | CCTV5+ | https://a/5p.m3u8\n"
         "ch004 | cctv5 HD | https://c/5.m3u8\n"
         "ch005 | 芒果台 | https://b/hn.m3u8\n"
         "ch006 | CCTV-4 欧洲 | https://a/4e.m3u8\n"
         "ch007 | CCTV4 | https://a/4.m3u8\n")


def ok(at=100, ms=None):
    found = {"ok": True, "why": "", "at": at}
    if ms:
        found["ms"] = ms
    return found


def bad(at=100):
    return {"ok": False, "why": "403", "at": at}


def mark(url):
    return routes.fingerprint(url)


def meta():
    return {
        # CCTV5：主线路能用，两条备用：一条连不上，一条没测过。
        "ch000": {"routes": [{"url": "https://a/5b.m3u8"}, {"url": "https://a/5c.m3u8"}],
                  "checks": {mark("https://a/5.m3u8"): ok(ms=900), mark("https://a/5b.m3u8"): bad()}},
        # CCTV-5 体育：三条都能用，有台标；一条量过跟不上实时。
        "ch002": {"logo": "https://logo.example/5.png",
                  "routes": [{"url": "https://b/5b.m3u8"}, {"url": "https://a/5c.m3u8"}],
                  "checks": {mark("https://b/5.m3u8"): ok(ms=500), mark("https://b/5b.m3u8"): ok(ms=300),
                             mark("https://a/5c.m3u8"): ok(ms=400)},
                  "quality": {mark("https://b/5b.m3u8"): {"still": True}},
                  "starts": {"pc": {mark("https://b/5.m3u8"): 1500}}},
        # cctv5 HD：一条，有节目单编号。
        "ch004": {"tvg_id": "CCTV5", "checks": {mark("https://c/5.m3u8"): ok(ms=200)}},
        # 芒果台 和 湖南卫视 的节目单编号一样（名字不一样）。
        "ch001": {"tvg_id": "HunanTV"},
        "ch005": {"tvg_id": "hunantv", "logo": "https://logo.example/hn.png"},
    }


class DetectTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "channels.txt"
        self.path.write_text(TABLE, encoding="utf-8")
        routes.write_meta(self.path, meta())
        self.rows = channeltable.read(self.path)["rows"]

    def test_groups_and_keeper(self):
        found = channelmerge.duplicates(self.path, self.rows)
        self.assertEqual([sorted(c["id"] for c in g["channels"]) for g in found],
                         [["ch000", "ch002", "ch004"], ["ch001", "ch005"]],
                         "CCTV5+ and CCTV4 欧洲 are other channels")
        five, hunan = found
        self.assertEqual((five["why"], five["tick"], five["keep"]), ("name", True, "ch002"))
        self.assertEqual([c["id"] for c in five["channels"]], ["ch002", "ch000", "ch004"])
        self.assertEqual({k: five["channels"][0][k] for k in ("lines", "good", "ok", "bad", "logo")},
                         {"lines": 3, "good": 2, "ok": 3, "bad": 0, "logo": True})
        self.assertEqual((hunan["why"], hunan["tick"]), ("tvg", False), "tvg-id only: offered, not ticked")
        self.assertEqual(hunan["keep"], "ch005", "same lines: the one with a logo")

    def test_key_keeps_regional_and_uhd_channels_apart(self):
        key = subscriptions.channel_key
        self.assertNotEqual(key("CCTV-4 欧洲"), key("CCTV4"))
        self.assertNotEqual(key("CCTV4 美洲"), key("CCTV-4 欧洲"))
        self.assertNotEqual(key("CCTV8K"), key("CCTV8"))
        self.assertEqual(key("CCTV-4 欧洲 HD"), key("CCTV4欧洲"))

    def test_plan_lines_and_fields(self):
        plan = channelmerge.plan(self.path, self.rows, [{"keep": "ch000", "drop": ["ch002", "ch004"]}])
        item = next(x for x in plan["edits"] if isinstance(x, dict))
        lines = [item["url"]] + [r["url"] for r in item["routes"]]
        # 能用的按：留下的台的主线路先（同一档），再按用时；画面可疑的、没测过的、连不上的在后；最多 5 条。
        self.assertEqual(lines, ["https://a/5.m3u8", "https://c/5.m3u8", "https://a/5c.m3u8", "https://b/5.m3u8",
                                 "https://b/5b.m3u8"])
        self.assertEqual(len(lines), routes.ROUTES_MAX)
        self.assertEqual(plan["merged"][0]["lost"], 1, "the failing line did not fit")
        self.assertEqual((item["logo"], item["tvg_id"]), ("https://logo.example/5.png", "CCTV5"))
        self.assertEqual([x if isinstance(x, str) else x["id"] for x in plan["edits"]],
                         ["ch000", "ch001", "ch003", "ch005", "ch006", "ch007"])

    def test_dead_primary_gives_way(self):
        data = meta()
        data["ch000"]["checks"][mark("https://a/5.m3u8")] = bad()
        routes.write_meta(self.path, data)
        plan = channelmerge.plan(self.path, self.rows, [{"keep": "ch000", "drop": ["ch004"]}])
        item = plan["edits"][0]
        self.assertEqual(item["url"], "https://c/5.m3u8", "a usable line beats the kept channel's failing primary")
        self.assertNotIn("logo", item)

    def test_bad_picks(self):
        for picks, words in (([], "没有选"), ([{"keep": "ch000", "drop": ["ch001"]}], "不像是同一个台"),
                             ([{"keep": "ch000", "drop": ["ch099"]}], "没有 ch099"),
                             ([{"keep": "ch000", "drop": ["ch002"]}, {"keep": "ch004", "drop": ["ch002"]}], "两次"),
                             ([{"keep": "ch000", "drop": []}], "格式不对"), ("x", "没有选")):
            with self.assertRaises(ValueError) as caught:
                channelmerge.plan(self.path, self.rows, picks)
            self.assertIn(words, str(caught.exception), picks)


class EndpointTests(selfhost_base.RelayHarness):
    def setUp(self):
        super().setUp()
        root = Path(self.tmp.name)
        self.admin = self.accounts.create_user("管理员", "admin")
        self.member = self.accounts.create_user("家人")
        self.cookies = {"admin": "tvs=" + self.accounts.new_session(self.admin),
                        "member": "tvs=" + self.accounts.new_session(self.member)}
        self.main.write_text(TABLE, encoding="utf-8")
        routes.write_meta(self.main, meta())
        # 总后台给 CCTV-5 体育 设了分组；家人把 CCTV5（ch000）和 cctv5 HD（ch004）调了分组。
        admin.AdminOps(self.accounts).set_global_groups({"ch002": "国际"})
        self.relay._admin_groups = None
        state = self.relay.user_state(self.member)
        state.group_overrides = {"ch004": "其他", "ch001": "卫视"}
        state.save()
        self.relay.tally = livetally.LiveTally(root / "live-plays.json", clock=lambda: 1000.0)
        self.relay.tally.add(self.member, "ch004", 600)
        self.relay.tally.add(self.member, "ch000", 300)
        self.guide = epg.attach(self.relay, root / "epg")
        self.guide.autostart = False
        self.accounts.set_meta(epg.MANUAL_KEY, json.dumps({"ch004": {"source": "", "epg": "CCTV5", "name": "cctv5 HD"}}))

    def tearDown(self):
        self.guide.configure([], start=False)
        super().tearDown()

    def call(self, who, body):
        status, _, answer = self.request("POST", "/tv/api/admin/channels/merge", body,
                                         headers={"Cookie": self.cookies[who]})
        return status, answer

    def test_merge_end_to_end(self):
        view = self.request("GET", "/tv/api/admin/channels", headers={"Cookie": self.cookies["admin"]})[2]
        self.assertEqual([g["keep"] for g in view["dups"]], ["ch002", "ch005"])
        picks = [{"keep": "ch000", "drop": ["ch002", "ch004"]}]
        body = {"version": view["version"], "groups": picks, "dry": True}
        self.assertEqual(self.call("member", body)[0], 404)
        text, sidecar = self.main.read_text(encoding="utf-8"), routes.read_meta(self.main)
        answer = self.call("admin", body)[1]
        self.assertTrue(answer["ok"], answer)
        self.assertEqual((answer["dry"], answer["summary"]["removed"], answer["summary"]["total"]), (True, 2, 6))
        self.assertEqual((self.main.read_text(encoding="utf-8"), routes.read_meta(self.main)), (text, sidecar),
                         "a dry run changes nothing")
        self.assertEqual(self.call("admin", dict(body, version="old"))[1].get("stale"), True)

        answer = self.call("admin", dict(body, dry=False))[1]
        self.assertTrue(answer["ok"], answer)
        self.assertTrue(answer["backup"])
        self.assertTrue((self.main.parent / answer["backup"]).is_file())
        self.assertTrue((self.main.parent / (self.main.name + ".meta.json" + answer["backup"][len(self.main.name):])).is_file(),
                        "the sidecar is backed up with the same stamp")
        rows = channeltable.read(self.main)["rows"]
        self.assertEqual([r["id"] for r in rows], ["ch000", "ch001", "ch003", "ch005", "ch006", "ch007"])
        entry = routes.read_meta(self.main)["ch000"]
        self.assertEqual([r["url"] for r in entry["routes"]],
                         ["https://c/5.m3u8", "https://a/5c.m3u8", "https://b/5.m3u8", "https://b/5b.m3u8"])
        self.assertEqual((entry["logo"], entry["tvg_id"]), ("https://logo.example/5.png", "CCTV5"))
        self.assertEqual(entry["checks"][mark("https://c/5.m3u8")]["ms"], 200, "moved lines keep their checks")
        self.assertEqual(entry["checks"][mark("https://a/5.m3u8")]["ms"], 900, "the kept line keeps its own")
        self.assertEqual(entry["checks"][mark("https://a/5c.m3u8")]["ms"], 400)
        self.assertNotIn(mark("https://a/5b.m3u8"), entry["checks"], "the line that did not fit is gone")
        self.assertEqual(entry["quality"], {mark("https://b/5b.m3u8"): {"still": True}})
        self.assertEqual(entry["starts"], {"pc": {mark("https://b/5.m3u8"): 1500}})
        # 分组、常看、手动对台。
        self.assertEqual(admin.AdminOps(self.accounts).global_groups(), {"ch000": "国际"})
        self.assertEqual(self.relay.user_state(self.member).group_overrides, {"ch000": "其他", "ch001": "卫视"})
        self.assertEqual(self.relay.tally.top(self.member), ["ch000"])
        self.assertEqual(round(self.relay.tally._data[str(self.member)]["ch000"][0]), 900)
        self.assertEqual(epg.load_manual(self.accounts), {"ch000": {"source": "", "epg": "CCTV5", "name": "CCTV5"}})
        self.assertEqual(answer["moved"], {"groups": 1, "frequent": 1, "pins": 1})
        audit = self.accounts._all("SELECT action, detail FROM audit_log WHERE action LIKE 'channels.%' ORDER BY id")
        self.assertEqual([a["action"] for a in audit], ["channels.save", "channels.merge"])
        view = self.request("GET", "/tv/api/admin/channels", headers={"Cookie": self.cookies["admin"]})[2]
        self.assertEqual([g["keep"] for g in view["dups"]], ["ch005"], "only the tvg-id group is left")

    def test_keepers_own_overrides_win(self):
        state = self.relay.user_state(self.member)
        state.group_overrides = {"ch000": "央视", "ch004": "其他"}
        state.save()
        view = self.request("GET", "/tv/api/admin/channels", headers={"Cookie": self.cookies["admin"]})[2]
        answer = self.call("admin", {"version": view["version"], "groups": [{"keep": "ch000", "drop": ["ch004"]}]})[1]
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(self.relay.user_state(self.member).group_overrides, {"ch000": "央视"})


if __name__ == "__main__":
    unittest.main()
