"""My library's play history (server/relay/library.py history / history_remove, home.PcProgress, home.finished).

What is checked, through the relay's real HTTP port and without network: GET /api/library/history lists the
caller's own library progress (pc-progress.json, lib:<id>) newest first, one row per episode (never merged per
show, unlike Continue watching), with the Douban-matched name (season words dropped when the show has several
seasons, the season then given separately), season / episode numbers, the episode's title, the art (the item's
own poster, else the season's Douban poster), position, duration, time, the TV's name when it was watched on a
TV, and "finished" (near the end, or marked done and not re-watched); items no longer in the library are skipped
but their records kept; Bilibili / YouTube records never appear. Guests and signed-out callers get 404, the cloud
relay has none. POST /api/library/history/remove {id} drops one record (and the PC cursor, so it is not written
back), {all: true} drops only that user's lib: records. Library records have their own cap and never push out
Bilibili ones. A finished item plays from the start on the PC (also when it stopped 5..30 s before the end, which
used to resume); a half-watched one resumes. The page's pure helpers are tested by console_libhist_test.mjs.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from server.relay import home, library, web

import test_relay_library as lib_base
import test_relay_watch as watch_base

MP4 = lib_base.MP4


class HistoryApiTests(unittest.TestCase):
    selfhost = True
    setUp_relay = watch_base.RelayWatchTests.setUp
    tearDown_relay = watch_base.RelayWatchTests.tearDown
    online = watch_base.RelayWatchTests.online
    request = lib_base.ApiTests.request
    call = lib_base.ApiTests.call

    def setUp(self):
        self.setUp_relay()
        root = Path(self.tmp.name) / "videos"
        lib_base.touch(root / "Inception.2010.mp4", MP4)
        lib_base.touch(root / "Inception.2010.jpg", b"\xff\xd8local")
        lib_base.touch(root / "Clip.mp4", MP4)
        lib_base.touch(root / "X医生" / "S01E01.mp4", MP4)
        lib_base.touch(root / "X医生" / "S01E02.mp4", MP4)
        lib_base.touch(root / "X医生" / "S02E05.mp4", MP4)
        self.relay.library = lib_base.quiet(Path(self.accounts.path).parent)
        answer = self.relay.library.add({"kind": "local", "name": "电脑", "path": str(root)})
        self.assertTrue(answer["ok"], answer)
        self.ids = {i["path"]: i["id"] for i in self.relay.library.items()}
        self.store = self.relay.pc_progress
        self.alice_caller = web.Caller(user=self.alice, role="user", name="Alice")

    def tearDown(self):
        self.tearDown_relay()

    def put(self, user, iid, position, duration, at, **extra):
        self.now = at
        self.store.put(user, "lib:" + iid, dict({"channel": "cast", "bvid": iid, "site": "library",
                                                  "position": position, "duration": duration}, **extra))

    def history(self, who="alice"):
        status, answer = self.call(who, "GET", "/tv/api/library/history")
        self.assertEqual(status, 200)
        self.assertTrue(answer["ok"], answer)
        return answer["items"]

    def watch(self, iid, **body):
        body.setdefault("viewer", "c0ffee00")
        return self.call("alice", "POST", "/tv/api/watch", dict(body, library=iid))[1]

    # ---- rows ----

    def test_rows_newest_first_one_per_episode(self):
        movie, clip = self.ids["Inception.2010.mp4"], self.ids["Clip.mp4"]
        e1, e2, e5 = (self.ids[f"X医生/{n}.mp4"] for n in ("S01E01", "S01E02", "S02E05"))
        gone = "f" * 16
        self.put(self.alice, e1, 2690.0, 2700.0, 100.0)                       # 离结尾 10 秒：看完了
        self.put(self.alice, e2, 600.0, 2700.0, 200.0, device="客厅的小电视")
        self.put(self.alice, e5, 300.0, 2700.0, 300.0)
        self.put(self.alice, movie, 1200.0, 8880.0, 400.0)
        self.put(self.alice, gone, 50.0, 100.0, 500.0)                        # 片库里已经没有了
        self.put(self.alice, clip, 10.0, 0.0, 50.0)
        self.store.mark_done(self.alice, "lib:" + clip, clip)                 # 记过看完了、不知道多长
        self.store.put(self.alice, "v:bilibili:BV1xx411c7mD", {"bvid": "BV1xx411c7mD", "position": 100})
        self.put(self.bob, movie, 99.0, 8880.0, 600.0)                        # 别人的
        rows = self.history()
        self.assertEqual([r["id"] for r in rows], [movie, e5, e2, e1, clip], "newest first, every episode its own row")
        by = {r["id"]: r for r in rows}
        self.assertEqual((by[movie]["kind"], by[movie]["name"], by[movie]["position"], by[movie]["duration"]),
                         ("movie", "Inception", 1200, 8880))
        self.assertEqual(by[movie]["art"], f"api/library/art/{movie}", "the movie's own poster")
        self.assertEqual(by[movie]["at"], 400.0)
        self.assertEqual((by[e5]["kind"], by[e5]["name"], by[e5]["season"], by[e5]["seasons"], by[e5]["episode"]),
                         ("episode", "X医生", 2, 2, 5))
        self.assertEqual((by[e1]["season"], by[e1]["episode"]), (1, 1))
        self.assertIn("title", by[e5])
        self.assertEqual(by[e2]["device"], "客厅的小电视")
        self.assertEqual(by[e5]["device"], "", "watched on the PC")
        self.assertEqual({i: by[i]["finished"] for i in by}, {movie: False, e5: False, e2: False, e1: True, clip: True})
        self.assertEqual(by[clip]["kind"], "video")
        self.assertTrue(all(r["playable"] for r in rows))
        self.assertIsNotNone(self.store.get(self.alice, "lib:" + gone), "a missing item's record is kept")
        self.assertNotIn("BV1xx411c7mD", json.dumps(rows), "only the library's records")
        self.assertEqual([r["id"] for r in self.history("bob")], [movie])
        self.assertEqual(self.history("bob")[0]["position"], 99)

    def test_douban_name_and_season_art(self):
        lib = self.relay.library
        e5 = self.ids["X医生/S02E05.mp4"]
        key = lib.meta_key(lib.item(e5))
        lib.meta.put(key, {"state": "matched", "manual": True, "id": "2000001", "title": "急诊科医生 第一季",
                           "q": "", "at": 1})
        lib.meta.put(key + "-2", {"state": "matched", "manual": True, "id": "2000002", "title": "急诊科医生 第二季",
                                  "img": "https://img9.doubanio.com/view/photo/s_ratio_poster/public/p2.jpg",
                                  "q": "", "at": 1})
        self.put(self.alice, e5, 300.0, 2700.0, 300.0)
        row = self.history()[0]
        self.assertEqual(row["name"], "急诊科医生", "Douban's name, its season word dropped (the season is separate)")
        self.assertEqual(row["season"], 2)
        self.assertTrue(row["art"].startswith(f"api/library/meta-art/{key}-2?"), row["art"])
        self.assertNotIn("doubanio", json.dumps(row), "art through the relay, never the outside address")

    def test_finished_rule(self):
        self.assertTrue(home.finished(2690, 2700))
        self.assertFalse(home.finished(2660, 2700))
        self.assertTrue(home.finished(10, 2700, done=True), "done and not re-watched")
        self.assertFalse(home.finished(900, 2700, done=True), "done, but re-watched half-way")
        self.assertTrue(home.finished(900, 0, done=True), "length unknown: the mark decides")
        self.assertFalse(home.finished(900, 0))

    # ---- who ----

    def test_guests_signed_out_and_cloud(self):
        movie = self.ids["Inception.2010.mp4"]
        self.put(self.alice, movie, 1200.0, 8880.0, 400.0)
        self.assertEqual(self.request("GET", "/tv/api/library/history", "guest")[0], 404)
        self.assertEqual(self.request("GET", "/tv/api/library/history")[0], 404)
        status, _, raw = self.request("POST", "/tv/api/library/history/remove", "guest", {"all": True})
        self.assertTrue(status == 404 or not json.loads(raw)["ok"])
        self.assertEqual(self.request("POST", "/tv/api/library/history/remove", None, {"all": True})[0], 404)
        self.assertIsNotNone(self.store.get(self.alice, "lib:" + movie), "nothing removed")
        self.relay.library = None
        self.assertEqual(self.request("GET", "/tv/api/library/history", "alice")[0], 404)
        self.assertEqual(self.call("alice", "POST", "/tv/api/library/history/remove", {"all": True})[0], 404)

    # ---- removing ----

    def test_remove_one_and_clear_only_library_records(self):
        movie, e1, e2 = self.ids["Inception.2010.mp4"], self.ids["X医生/S01E01.mp4"], self.ids["X医生/S01E02.mp4"]
        self.put(self.alice, movie, 1200.0, 8880.0, 100.0)
        self.put(self.alice, e1, 600.0, 2700.0, 200.0)
        self.put(self.alice, e2, 600.0, 2700.0, 300.0)
        self.put(self.bob, movie, 99.0, 8880.0, 400.0)
        self.store.put(self.alice, "v:bilibili:BV1xx411c7mD", {"bvid": "BV1xx411c7mD", "position": 100})
        self.store.put(self.alice, "sr01", {"bvid": "BV1yy411c7mD", "position": 100})
        # 电脑上放过 e1：游标不去掉的话，下一次 pc_sync 会把删掉的记录写回去。
        self.assertTrue(self.watch(e1)["ok"])
        self.assertEqual(self.call("alice", "POST", "/tv/api/library/history/remove", {"id": "nope"})[1]["ok"], False)
        answer = self.call("alice", "POST", "/tv/api/library/history/remove", {"id": e1})[1]
        self.assertEqual(answer, {"ok": True, "removed": 1})
        self.relay.pc_sync(self.alice)
        self.assertEqual([r["id"] for r in self.history()], [e2, movie])
        self.assertIsNone(self.store.get(self.alice, "lib:" + e1))
        answer = self.call("alice", "POST", "/tv/api/library/history/remove", {"all": True})[1]
        self.assertEqual(answer, {"ok": True, "removed": 2})
        self.assertEqual(self.history(), [])
        self.assertEqual(sorted(k for k, _ in self.store.rows(self.alice)), ["sr01", "v:bilibili:BV1xx411c7mD"],
                         "Bilibili / YouTube records stay")
        self.assertEqual([r["id"] for r in self.history("bob")], [movie], "someone else's stay")
        self.assertTrue((Path(self.tmp.name) / "videos" / "Inception.2010.mp4").exists(), "files are never touched")
        reread = home.PcProgress(self.store.path)
        self.assertEqual([k for k, _ in reread.rows(self.alice) if k.startswith("lib:")], [], "saved to disk")

    def test_library_records_have_their_own_cap(self):
        for n in range(home.PROGRESS_MAX):
            self.now = 1000.0 + n
            self.store.put(self.alice, f"v:bilibili:BV{n}", {"bvid": f"BV{n}", "position": 40})
        for n in range(60):
            self.now = 2000.0 + n
            self.store.put(self.alice, f"lib:{n:016x}", {"bvid": f"{n:016x}", "position": 40})
        keys = [k for k, _ in self.store.rows(self.alice)]
        self.assertEqual(sum(k.startswith("v:") for k in keys), home.PROGRESS_MAX, "library plays push out no Bilibili")
        self.assertEqual(sum(k.startswith("lib:") for k in keys), 60)
        self.assertEqual(self.store.forget_prefix(self.alice, "lib:"), 60)
        self.assertEqual(len(self.store.rows(self.alice)), home.PROGRESS_MAX)

    # ---- playing from the history ----

    def test_finished_plays_from_the_start_half_watched_resumes(self):
        movie, clip = self.ids["Inception.2010.mp4"], self.ids["Clip.mp4"]
        self.put(self.alice, movie, 8860.0, 8880.0, 100.0)          # 离结尾 20 秒：以前从 8860 接着放
        answer = self.watch(movie)
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(answer["start"], 0.0)
        self.assertNotIn("resume", answer)
        self.relay._pc_cursor.clear()
        self.put(self.alice, clip, 1000.0, 5400.0, 200.0)
        again = self.watch(clip)
        self.assertEqual((again["start"], again["resume"]), (1000.0, 1000.0))
        self.relay._pc_cursor.clear()
        self.store.mark_done(self.alice, "lib:" + clip, clip)
        self.put(self.alice, clip, 12.0, 5400.0, 300.0)                 # 看完了，又从头看了 12 秒
        self.assertEqual(self.watch(clip)["start"], 0.0)


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class LibHistJsTests(unittest.TestCase):
    """The page's pure helpers for the library rows (title line, "all" interleaving) under Node."""

    def test_pure_functions(self):
        result = subprocess.run(["node", str(Path(__file__).resolve().parent / "console_libhist_test.mjs")],
                                capture_output=True, text=True, encoding="utf-8", timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
