"""Library shows: versions and play order (server/relay/libversions.py, library.py, web.py's _pc_library, voicelib.py).

What is checked, following docs/rootv-episode-versions (the 蒸蒸日上 report: "episode 5 / 44" while playing episode 3,
and "next" playing the other file of episode 3 first):
- a show's files are grouped into versions by their folder, season folders stripped so one version spans seasons;
  two folders of 22 episodes are two versions of 22 each, the show still has 22 episodes and 44 files;
- the PC player's episode list (``parts``) is the playing file's version and season only, each row carries the real
  episode number (``ep``, ``label``) and ``count`` is the number of distinct episodes in it -- never 44, never a list
  position; the other versions come along in ``others`` for switching;
- next / previous (PC, TV, voice) stay in the version and season, go by the real episode number, skip a missing
  episode (A has 3 and 5, B has 4: A3 -> A5, never B4) and unplayable files, stop at the end (no other version or
  season); episodes 3, 5, 8 are labelled 3, 5, 8 and counted 3;
- two files of one episode in one version are a conflict (both kept and listed, nothing dropped);
- an admin can move files to another or a new version and rename a version; it survives a rescan, "auto" undoes it;
- a file with no episode number stays playable and visible but is not counted and never a next episode.
The page's pure helpers are tested by console_libver_test.mjs (run here under Node).
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from server.relay import library, libversions  # noqa: E402
from test_relay_library import quiet, touch  # noqa: E402
import test_relay_library as lib_base  # noqa: E402
import test_relay_watch as watch_base  # noqa: E402


class FolderTests(unittest.TestCase):
    def test_season_folders_are_stripped(self):
        vf = library.version_folder
        self.assertEqual(vf("剧/4K/Season 1/剧.S01E01.mkv", "剧"), "剧/4K")
        self.assertEqual(vf("剧/4K/S02/x.mkv", "剧"), "剧/4K")
        self.assertEqual(vf("剧/第二季/x.mkv", "剧"), "剧")
        self.assertEqual(vf("剧 第二季/x.mkv", "剧"), "")            # only the show's name and the season
        self.assertEqual(vf("剧 第二季 4K/x.mkv", "剧"), "剧 第二季 4K")  # more than that: a version of its own
        self.assertEqual(vf("x.mkv", "剧"), "")

    def test_gap_message(self):
        self.assertEqual(libversions.gap(3, 5), "第 4 集缺失，下一集为第 5 集")
        self.assertEqual(libversions.gap(3, 7), "第 4–6 集缺失，下一集为第 7 集")
        self.assertEqual(libversions.gap(5, 3), "第 4 集缺失，上一集为第 3 集")
        self.assertEqual(libversions.gap(3, 4), "")

    def test_labels_tell_versions_apart(self):
        rows = [{"id": "1", "ver": "a1", "vfolder": "A/剧/4K", "source": "网盘", "episode": 1},
                {"id": "2", "ver": "a2", "vfolder": "B/剧/4K", "source": "网盘", "episode": 1},
                {"id": "3", "ver": "a3", "vfolder": "", "source": "电脑", "episode": 1}]
        labels = sorted(v["label"] for v in libversions.summarize(rows))
        self.assertEqual(labels, ["A/剧/4K", "B/剧/4K", "根目录"])


class VersionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.root = base / "videos"
        self.lib = quiet(base / "data")

    def tearDown(self):
        self.tmp.cleanup()

    def scan(self):
        if not self.lib._load_sources():
            self.assertTrue(self.lib.add({"kind": "local", "name": "电脑", "path": str(self.root)})["ok"])
        else:
            self.lib.scan_now(self.lib._load_sources()[0]["id"])

    def show(self, title):
        return next(s for s in self.lib.view()["shows"] if s["title"] == title)

    def items(self):
        return {i["path"]: i for i in self.lib.items()}

    def two_folders(self):
        for n in range(1, 23):
            touch(self.root / "蒸蒸日上" / "A" / f"蒸蒸日上.S01E{n:02d}.mp4")
            touch(self.root / "蒸蒸日上" / "B" / f"蒸蒸日上.S01E{n:02d}.mp4")
        self.scan()

    def test_two_folders_are_two_versions_of_22(self):
        self.two_folders()
        show = self.show("蒸蒸日上")
        self.assertEqual((show["count"], show["files"]), (22, 44))
        self.assertEqual([(v["label"], v["count"], v["files"], v["conflicts"]) for v in show["versions"]],
                         [("A", 22, 22, []), ("B", 22, 22, [])])
        a3 = self.items()["蒸蒸日上/A/蒸蒸日上.S01E03.mp4"]
        parts = self.lib.play_parts(a3)
        self.assertEqual(parts["count"], 22)
        self.assertEqual(len(parts["items"]), 22)
        here = next(r for r in parts["items"] if r["id"] == a3["id"])
        self.assertEqual((here["ep"], here["label"]), (3, "第 3 集"))
        self.assertEqual(parts["label"], "A")
        self.assertEqual([(o["label"], o["count"]) for o in parts["others"]], [("B", 22)])
        self.assertTrue(all(r["id"] != a3["id"] for o in parts["others"] for r in o["items"]))
        # next / previous stay in A
        items = self.items()
        self.assertEqual(self.lib.neighbor(a3, 1)["path"], "蒸蒸日上/A/蒸蒸日上.S01E04.mp4")
        self.assertEqual(self.lib.neighbor(a3, -1)["path"], "蒸蒸日上/A/蒸蒸日上.S01E02.mp4")
        self.assertIsNone(self.lib.neighbor(items["蒸蒸日上/A/蒸蒸日上.S01E01.mp4"], -1))
        self.assertIsNone(self.lib.neighbor(items["蒸蒸日上/B/蒸蒸日上.S01E22.mp4"], 1))
        self.assertEqual(self.lib.place(a3), {"episode": 3, "total": 22})
        relay = type("R", (), {"library": self.lib})()
        self.assertEqual(library.tv_episode(relay, type("V", (), {"bvid": a3["id"]})()), {"episode": 3, "total": 22})

    def test_missing_episode_is_skipped_inside_the_version(self):
        for n in (3, 5):
            touch(self.root / "剧" / "A" / f"剧.E{n:02d}.mp4")
        touch(self.root / "剧" / "B" / "剧.E04.mp4")
        self.scan()
        items = self.items()
        a3 = items["剧/A/剧.E03.mp4"]
        self.assertEqual(self.lib.neighbor(a3, 1)["path"], "剧/A/剧.E05.mp4")
        self.assertEqual(self.lib.neighbor(items["剧/A/剧.E05.mp4"], -1)["path"], "剧/A/剧.E03.mp4")

    def test_non_contiguous_numbers_are_real(self):
        for n in (3, 5, 8):
            touch(self.root / "剧" / f"剧.E{n:02d}.mp4")
        self.scan()
        parts = self.lib.play_parts(self.items()["剧/剧.E05.mp4"])
        self.assertEqual([(r["ep"], r["label"]) for r in parts["items"]], [(3, "第 3 集"), (5, "第 5 集"), (8, "第 8 集")])
        self.assertEqual(parts["count"], 3)
        self.assertEqual(parts["others"], [])

    def test_seasons_and_specials_do_not_mix(self):
        for s, n in ((1, 1), (1, 2), (2, 1), (0, 1)):
            touch(self.root / "剧" / f"Season {s}" / f"剧.S{s:02d}E{n:02d}.mp4")
        self.scan()
        show = self.show("剧")
        self.assertEqual(len(show["versions"]), 1)          # season folders under one folder: one version
        self.assertEqual(show["versions"][0]["seasons"], [0, 1, 2])
        items = self.items()
        last = items["剧/Season 1/剧.S01E02.mp4"]
        self.assertIsNone(self.lib.neighbor(last, 1))        # never into season 2
        parts = self.lib.play_parts(last)
        self.assertEqual([r["label"] for r in parts["items"]], ["第 1 季 第 1 集", "第 1 季 第 2 集"])
        special = self.lib.play_parts(items["剧/Season 0/剧.S00E01.mp4"])
        self.assertIsNone(special)                           # alone in its season, no other version

    def test_two_files_of_one_episode_in_one_folder_are_a_conflict(self):
        touch(self.root / "剧" / "剧.E01.mp4")
        touch(self.root / "剧" / "剧.E01.mkv")
        touch(self.root / "剧" / "剧.E02.mp4")
        self.scan()
        show = self.show("剧")
        self.assertEqual(len(show["versions"]), 1)
        v = show["versions"][0]
        self.assertEqual((v["count"], v["files"], v["conflicts"]), (2, 3, [[1, 1]]))
        parts = self.lib.play_parts(self.items()["剧/剧.E02.mp4"])
        self.assertEqual([r["choices"] for r in parts["items"]], [2, 1])

    def test_unknown_numbers_stay_visible_but_out_of_the_order(self):
        touch(self.root / "剧" / "剧.E01.mp4")
        touch(self.root / "剧" / "剧.E02.mp4")
        self.scan()
        items = self.items()
        odd = dict(items["剧/剧.E02.mp4"])
        # an episode whose number could not be read (a manual or older index can hold one)
        with library._LOCK:
            self.lib._items[odd["id"]]["episode"] = None
        one = self.items()["剧/剧.E01.mp4"]
        self.assertIsNone(self.lib.neighbor(one, 1))
        parts = self.lib.play_parts(one)
        self.assertEqual([(r["ep"], r["label"]) for r in parts["items"]], [(1, "第 1 集"), (None, "集号未知")])
        self.assertEqual(parts["count"], 1)

    def test_manual_versions_survive_a_rescan(self):
        self.two_folders()
        items = self.items()
        moved = [items[f"蒸蒸日上/B/蒸蒸日上.S01E{n:02d}.mp4"]["id"] for n in (1, 2)]
        bad = self.lib.set_versions({"items": [{"id": moved[0], "v": "new"}]})
        self.assertFalse(bad["ok"])                          # a new version needs a name
        answer = self.lib.set_versions({"items": [{"id": i, "v": "new"} for i in moved], "name": "导演剪辑版"})
        self.assertTrue(answer["ok"], answer)
        self.scan()
        show = self.show("蒸蒸日上")
        found = {v["label"]: (v["count"], v["manual"]) for v in show["versions"]}
        self.assertEqual(found, {"A": (22, False), "B": (20, False), "导演剪辑版": (2, True)})
        b3 = self.items()["蒸蒸日上/B/蒸蒸日上.S01E03.mp4"]
        self.assertIsNone(self.lib.neighbor(b3, -1))         # B's episode 2 moved away
        a_key = next(v["key"] for v in show["versions"] if v["label"] == "A")
        self.assertTrue(self.lib.set_versions({"rename": a_key, "name": "蓝光"})["ok"])
        self.assertIn("蓝光", {v["label"] for v in self.show("蒸蒸日上")["versions"]})
        self.assertTrue(self.lib.set_versions({"items": [{"id": i, "v": "auto"} for i in moved]})["ok"])
        labels = {v["label"]: v["count"] for v in self.show("蒸蒸日上")["versions"]}
        self.assertEqual(labels, {"蓝光": 22, "B": 22})
        self.assertFalse(self.lib.set_versions({"items": [{"id": "0" * 16, "v": "auto"}]})["ok"])
        self.assertFalse(self.lib.set_versions({"items": [{"id": moved[0], "v": "zzz"}]})["ok"])


class PlayApiTests(unittest.TestCase):
    """POST /tv/api/watch {library}: the episode list the PC player gets."""
    selfhost = True
    setUp_relay = watch_base.RelayWatchTests.setUp
    tearDown_relay = watch_base.RelayWatchTests.tearDown
    online = watch_base.RelayWatchTests.online
    request = lib_base.ApiTests.request
    call = lib_base.ApiTests.call

    def setUp(self):
        self.setUp_relay()
        root = Path(self.tmp.name) / "videos"
        for n in range(1, 23):
            lib_base.touch(root / "蒸蒸日上" / "A" / f"蒸蒸日上.S01E{n:02d}.mp4", lib_base.MP4)
            lib_base.touch(root / "蒸蒸日上" / "B" / f"蒸蒸日上.S01E{n:02d}.mp4", lib_base.MP4)
        self.relay.library = lib_base.quiet(Path(self.accounts.path).parent)
        self.assertTrue(self.relay.library.add({"kind": "local", "name": "电脑", "path": str(root)})["ok"])
        self.ids = {i["path"]: i["id"] for i in self.relay.library.items()}

    def tearDown(self):
        self.tearDown_relay()

    def test_parts_are_the_version_with_real_numbers(self):
        a3 = self.ids["蒸蒸日上/A/蒸蒸日上.S01E03.mp4"]
        status, answer = self.call("alice", "POST", "/tv/api/watch", {"library": a3, "viewer": "c0ffee00"})
        self.assertEqual(status, 200)
        self.assertTrue(answer["ok"], answer)
        parts = answer["parts"]
        self.assertEqual(parts["kind"], "library")
        self.assertEqual(parts["count"], 22)
        self.assertEqual(len(parts["items"]), 22)
        cur = next(r for r in parts["items"] if r["n"] == parts["current"])
        self.assertEqual((cur["id"], cur["ep"], cur["label"]), (a3, 3, "第 3 集"))
        nxt = next(r for r in parts["items"] if r["n"] == parts["current"] + 1)
        self.assertEqual(nxt["id"], self.ids["蒸蒸日上/A/蒸蒸日上.S01E04.mp4"])
        self.assertEqual(len(parts["others"]), 1)
        self.assertEqual(parts["others"][0]["count"], 22)



class AdminApiTests(unittest.TestCase):
    """POST /tv/api/admin/library/versions through the relay's real HTTP port: admins only."""
    setUp = lib_base.ApiTests.setUp
    tearDown = lib_base.ApiTests.tearDown
    setUp_relay = lib_base.ApiTests.setUp_relay
    tearDown_relay = lib_base.ApiTests.tearDown_relay
    online = lib_base.ApiTests.online
    request = lib_base.ApiTests.request
    call = lib_base.ApiTests.call
    selfhost = True

    def test_admin_only(self):
        for n in (1, 2):
            touch(self.root / "剧" / f"剧.E{n:02d}.mp4")
        self.assertTrue(self.call("admin", "POST", "/tv/api/admin/library", {"kind": "local", "path": str(self.root)})[1]["ok"])
        one = next(i["id"] for i in self.relay.library.items() if i["path"] == "剧/剧.E01.mp4")
        body = {"items": [{"id": one, "v": "new"}], "name": "单独"}
        for who in ("alice", "guest"):
            self.assertEqual(self.call(who, "POST", "/tv/api/admin/library/versions", body)[0], 404)
        status, answer = self.call("admin", "POST", "/tv/api/admin/library/versions", body)
        self.assertEqual(status, 200)
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(self.relay.library.versions.load()["items"], {one: answer["key"]})


class LibVerJsTests(unittest.TestCase):
    def test_pure_functions(self):
        result = subprocess.run(["node", str(Path(__file__).resolve().parent / "console_libver_test.mjs")],
                                capture_output=True, text=True, encoding="utf-8", timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
