"""My library: how many episodes a show has, several files of one episode, and grouping videos whose names lost
their show (server/relay/library.py).

What is checked: a show's card and detail count distinct (season, episode) pairs, never files -- two versions of
every episode in two folders give "22", a gap (episodes 1 and 3) gives 2 and an episode number it could not read is
listed but not counted; season 1 and season 2's episode 1 stay apart, a special (season 0) is not merged with
season 1's episode of the same number; the episode list folds each episode's files into one row with ``versions`` (the playable, bigger picture first);
next / previous episode skips the other versions of the same episode and stays in the same folder; the TV's
"episode n of m" counts episodes, not files. File names that are only an abbreviation and a number
(``MTJBYDD-01.mp4``, what is left when only the videos of a Quark share are saved, without the folder that named the
show) become a "pending" show when three or more in one folder share the abbreviation with distinct numbers; camera
names (IMG_001), two numbers the same, or fewer than three stay other videos, and a pending show is never sent to
Douban automatically. An admin assigns videos to a show (``assign``): all or nothing, the same request twice gives the
same result, a rescan keeps the assignment (also when the parsing rules change), clearing it reparses the files; the
admin endpoint is admin-only and audited. The page's pure helpers are tested by console_libassign_test.mjs (run here
under Node).
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from server.relay import library  # noqa: E402
import test_relay_library as base  # noqa: E402
from test_relay_library import quiet, touch  # noqa: E402


class PrefixNameTests(unittest.TestCase):
    def test_prefix_of(self):
        self.assertEqual(library.prefix_of("MTJBYDD-01"), ("MTJBYDD", 1))
        self.assertEqual(library.prefix_of("mtjbydd.02.1080p"), ("mtjbydd", 2))
        self.assertEqual(library.prefix_of("ABC 3"), ("ABC", 3))
        self.assertIsNone(library.prefix_of("IMG_001"))
        self.assertIsNone(library.prefix_of("VID-12"))
        self.assertIsNone(library.prefix_of("Inception.2010"))
        self.assertIsNone(library.prefix_of("MTJBYDD-0001"))
        self.assertIsNone(library.prefix_of("耶稣传-01"))

    def rows(self, names, folder="转存"):
        rows = []
        for name in names:
            found = library.parse_name(f"{folder}/{name}")
            rows.append({"path": f"{folder}/{name}", "kind": found["kind"], "title": found["title"] or name,
                         "year": found["year"], "show": found["show"], "season": found["season"],
                         "episode": found["episode"]})
        library.regroup(rows)
        return rows

    def test_three_or_more_with_distinct_numbers_become_a_pending_show(self):
        rows = self.rows([f"MTJBYDD-{n:02d}.mp4" for n in range(1, 11)] + ["福音的故事 第一部.mp4", "IMG_0001.mov"])
        shows = [r for r in rows if r["kind"] == "episode"]
        self.assertEqual(len(shows), 10)
        self.assertEqual({r["show"] for r in shows}, {"MTJBYDD"})
        self.assertTrue(all(r.get("pending") for r in shows))
        self.assertEqual(sorted(r["episode"] for r in shows), list(range(1, 11)))
        others = [r for r in rows if r["kind"] != "episode"]
        self.assertEqual(len(others), 2)                     # the standalone videos stay standalone
        self.assertFalse(any(r.get("pending") for r in others))

    def test_too_few_repeated_numbers_or_different_prefixes_stay_videos(self):
        for names in (["AB-01.mp4", "AB-02.mp4"],                       # only two
                      ["AB-01.mp4", "AB-01.mkv", "AB-02.mp4"],          # a number twice
                      ["AB-01.mp4", "CD-02.mp4", "EF-03.mp4"]):         # three different abbreviations
            rows = self.rows(names)
            self.assertFalse(any(r["kind"] == "episode" for r in rows), names)

    def test_real_episode_names_are_not_marked_pending(self):
        rows = self.rows([f"蒸蒸日上.第{n}集.mp4" for n in range(1, 4)], folder="蒸蒸日上")
        self.assertTrue(all(r["kind"] == "episode" and not r.get("pending") for r in rows))


class CountAndVersionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.root = base / "videos"
        self.lib = quiet(base / "data")

    def tearDown(self):
        self.tmp.cleanup()

    def scan(self):
        if not self.lib._load_sources():
            answer = self.lib.add({"kind": "local", "name": "电脑", "path": str(self.root)})
            self.assertTrue(answer["ok"], answer)
        else:
            self.lib.scan_now(self.lib._load_sources()[0]["id"])

    def show(self, title):
        return next(s for s in self.lib.view()["shows"] if s["title"] == title)

    def test_two_folders_of_the_same_episodes_count_once(self):
        for n in range(1, 23):
            touch(self.root / "蒸蒸日上" / "4K" / f"蒸蒸日上.S01E{n:02d}.mkv", b"x" * 20)
            touch(self.root / "蒸蒸日上" / "1080P" / f"蒸蒸日上.S01E{n:02d}.mp4")
        self.scan()
        show = self.show("蒸蒸日上")
        self.assertEqual((show["count"], show["files"], len(show["episodes"])), (22, 44, 22))
        first = show["episodes"][0]
        self.assertEqual(len(first["versions"]), 2)
        self.assertEqual({v["folder"] for v in first["versions"]}, {"蒸蒸日上/4K", "蒸蒸日上/1080P"})
        self.assertEqual(first["id"], first["versions"][0]["id"])
        self.assertTrue(all("versions" not in v for v in first["versions"]))
        # the same file scanned again does not add anything
        self.scan()
        again = self.show("蒸蒸日上")
        self.assertEqual((again["count"], again["files"]), (22, 44))

    def test_gaps_seasons_and_specials(self):
        touch(self.root / "某剧" / "某剧.S01E01.mp4")
        touch(self.root / "某剧" / "某剧.S01E03.mp4")
        touch(self.root / "某剧" / "某剧.S02E01.mp4")
        touch(self.root / "某剧" / "某剧.S00E01.mp4")
        self.scan()
        show = self.show("某剧")
        self.assertEqual(show["count"], 4)                   # S01E01, S01E03, S02E01, S00E01 -- not 3 (the top number)
        self.assertEqual(len(show["episodes"]), 4)
        self.assertFalse(any(e.get("versions") for e in show["episodes"]))
        # the season picker files specials under season 1 (as before), but they are their own episode there
        rows = {r["n"]: r["count"] for r in show["season_rows"]}
        self.assertEqual(rows, {1: 3, 2: 1})
        self.assertEqual([(e["season"], e["episode"]) for e in show["episodes"]], [(0, 1), (1, 1), (1, 3), (2, 1)])

    def test_logical_count_ignores_unknown_numbers(self):
        cards = [{"id": "a", "season": None, "episode": 1}, {"id": "b", "season": 1, "episode": 1},
                 {"id": "c", "season": None, "episode": None}]
        self.assertEqual(library.Library.logical_count(cards), 1)
        self.assertEqual(len(library.Library.fold_versions(
            [dict(c, playable=True, height=0, folder="", file=c["id"]) for c in cards])), 2)

    def test_next_episode_skips_versions_and_keeps_the_folder(self):
        for n in (1, 2, 3):
            touch(self.root / "剧" / "A" / f"剧.E{n:02d}.mp4")
            touch(self.root / "剧" / "B" / f"剧.E{n:02d}.mp4")
        self.scan()
        items = {i["path"]: i for i in self.lib.items()}
        one_b = items["剧/B/剧.E01.mp4"]
        self.assertEqual(self.lib.neighbor(one_b, 1)["path"], "剧/B/剧.E02.mp4")
        self.assertEqual(self.lib.neighbor(items["剧/A/剧.E02.mp4"], -1)["path"], "剧/A/剧.E01.mp4")
        self.assertIsNone(self.lib.neighbor(items["剧/A/剧.E03.mp4"], 1))
        relay = type("R", (), {"library": self.lib})()
        video = type("V", (), {"bvid": one_b["id"]})()
        self.assertEqual(library.tv_episode(relay, video), {"episode": 1, "total": 3})


class AssignTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.root = base / "videos"
        self.data = base / "data"
        for n in range(1, 6):
            touch(self.root / "转存" / f"MTJBYDD-{n:02d}.mp4")
        touch(self.root / "转存" / "福音的故事.mp4")
        touch(self.root / "转存" / "xx_clip_a.mp4")
        self.lib = quiet(self.data)
        answer = self.lib.add({"kind": "local", "name": "电脑", "path": str(self.root)})
        self.assertTrue(answer["ok"], answer)
        self.sid = answer["source"]["id"]

    def tearDown(self):
        self.tmp.cleanup()

    def ids(self):
        return {i["path"].rpartition("/")[2]: i["id"] for i in self.lib.items()}

    def test_pending_show_is_listed_and_not_looked_up(self):
        view = self.lib.view()
        pending = [s for s in view["shows"] if s["pending"]]
        self.assertEqual([(s["title"], s["count"]) for s in pending], [("MTJBYDD", 5)])
        self.assertEqual(sorted(c["file"] for c in view["videos"]), ["xx_clip_a.mp4", "福音的故事.mp4"])
        self.lib.set_online(True)
        self.lib._meta_can = lambda: True
        due = self.lib.meta_due()
        self.assertNotIn("s" + library.Library.show_key("MTJBYDD"), due)

    def test_assign_survives_rescan_and_can_be_undone(self):
        ids = self.ids()
        rows = [{"id": ids[f"MTJBYDD-{n:02d}.mp4"], "episode": n} for n in range(1, 6)]
        rows.append({"id": ids["xx_clip_a.mp4"], "episode": 6})
        answer = self.lib.assign({"show": "  蒸蒸 日上 ", "season": None, "items": rows})
        self.assertTrue(answer["ok"], answer)
        self.assertEqual((answer["count"], answer["show"]), (6, library.Library.show_key("蒸蒸 日上")))
        again = self.lib.assign({"show": "蒸蒸 日上", "items": rows})   # the same request twice: nothing doubles
        self.assertTrue(again["ok"])

        def check():
            view = self.lib.view()
            show = next(s for s in view["shows"] if s["title"] == "蒸蒸 日上")
            self.assertEqual((show["count"], show["files"], show["pending"]), (6, 6, False))
            self.assertTrue(all(e["assigned"] for e in show["episodes"]))
            self.assertFalse(any(s["pending"] for s in view["shows"]))
            self.assertEqual([c["file"] for c in view["videos"]], ["福音的故事.mp4"])
        check()
        self.lib.scan_now(self.sid)
        check()
        # parsing rules change: everything is reparsed, the manual choice still wins
        with library._LOCK:
            for item in self.lib._items.values():
                item["pv"] = 0
        self.lib.scan_now(self.sid)
        check()
        store = json.loads((self.data / "library-assign.json").read_text(encoding="utf-8"))
        self.assertEqual(len(store["items"]), 6)

        undo = self.lib.assign({"clear": [ids["xx_clip_a.mp4"]]})
        self.assertEqual(undo, {"ok": True, "count": 1})
        self.lib.scan_now(self.sid)
        view = self.lib.view()
        self.assertEqual(sorted(c["file"] for c in view["videos"]), ["xx_clip_a.mp4", "福音的故事.mp4"])
        show = next(s for s in view["shows"] if s["title"] == "蒸蒸 日上")
        self.assertEqual(show["count"], 5)

    def test_refusals_change_nothing(self):
        ids = self.ids()
        good = {"id": ids["MTJBYDD-01.mp4"], "episode": 1}
        for body, error in (({"show": "", "items": [good]}, "写一个剧名"),
                            ({"show": "x" * 200, "items": [good]}, "剧名太长了"),
                            ({"show": "剧", "items": []}, "没有选视频"),
                            ({"show": "剧", "season": 100, "items": [good]}, "季数不对"),
                            ({"show": "剧", "items": [good, {"id": ids["MTJBYDD-02.mp4"], "episode": 0}]}, "集号不对"),
                            ({"show": "剧", "items": [good, good]}, "同一个视频选了两次"),
                            ({"show": "剧", "items": [good, {"id": "0" * 16, "episode": 2}]}, "已经不在片库里"),
                            ({"clear": ["nope"]}, "请求格式不对")):
            answer = self.lib.assign(body)
            self.assertFalse(answer["ok"], body)
            self.assertIn(error, answer["error"])
        self.assertFalse((self.data / "library-assign.json").exists())
        self.assertTrue(all(not i.get("assigned") for i in self.lib.items()))

    def test_assigning_onto_an_existing_show_adds_versions(self):
        touch(self.root / "蒸蒸日上" / "蒸蒸日上.第1集.mp4")
        touch(self.root / "蒸蒸日上" / "蒸蒸日上.第2集.mp4")
        self.lib.scan_now(self.sid)
        ids = self.ids()
        answer = self.lib.assign({"show": "蒸蒸日上", "items": [{"id": ids["MTJBYDD-01.mp4"], "episode": 1},
                                                                  {"id": ids["MTJBYDD-03.mp4"], "episode": 3}]})
        self.assertTrue(answer["ok"], answer)
        show = next(s for s in self.lib.view()["shows"] if s["title"] == "蒸蒸日上")
        self.assertEqual((show["count"], show["files"]), (3, 4))
        self.assertEqual(len(show["episodes"][0]["versions"]), 2)


class AssignApiTests(unittest.TestCase):
    """POST /tv/api/admin/library/assign through the relay's real HTTP port: admins only, audited."""
    setUp = base.ApiTests.setUp
    tearDown = base.ApiTests.tearDown
    setUp_relay = base.ApiTests.setUp_relay
    tearDown_relay = base.ApiTests.tearDown_relay
    online = base.ApiTests.online
    request = base.ApiTests.request
    call = base.ApiTests.call
    selfhost = True

    def test_admin_only_and_audited(self):
        answer = self.call("admin", "POST", "/tv/api/admin/library", {"kind": "local", "path": str(self.root)})[1]
        self.assertTrue(answer["ok"], answer)
        clip = next(i["id"] for i in self.relay.library.items() if i["path"] == "Clip.mkv")
        body = {"show": "片段集", "items": [{"id": clip, "episode": 1}]}
        for who in ("alice", "guest"):
            self.assertEqual(self.call(who, "POST", "/tv/api/admin/library/assign", body)[0], 404)
        self.assertFalse(self.relay.library.item(clip).get("assigned"))
        status, answer = self.call("admin", "POST", "/tv/api/admin/library/assign", body)
        self.assertEqual(status, 200)
        self.assertTrue(answer["ok"], answer)
        view = self.call("alice", "GET", "/tv/api/library")[1]
        show = next(s for s in view["shows"] if s["title"] == "片段集")
        self.assertEqual((show["count"], show["episodes"][0]["file"]), (1, "Clip.mkv"))
        self.assertTrue(self.call("admin", "POST", "/tv/api/admin/library/assign", {"clear": [clip]})[1]["ok"])
        actions = [r["action"] for r in self.accounts.audit_rows(100) if r["action"].startswith("library.")]
        self.assertEqual(sorted(actions), ["library.add", "library.assign", "library.unassign"])


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class AssignJsTests(unittest.TestCase):
    """The console's helpers for episode counts, version labels and the assign panel, as pure JS."""

    def test_pure_functions(self):
        result = subprocess.run(["node", str(Path(__file__).resolve().parent / "console_libassign_test.mjs")],
                                capture_output=True, text=True, encoding="utf-8", timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
