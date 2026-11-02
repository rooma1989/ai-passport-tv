"""My library: reading the start of a cloud video ahead of the click (server/relay/librarypre.py, library.py
`_serve_pre`).

A 4K file runs at 10-25 Mbit/s and the drive gives one connection 4-78 Mbit/s; when the drive is slower than the
file the PC player waits for 12 s of video, 30-40 s. Reading the first PRE_S seconds ahead into the whole-file cache
makes the start local.

What is checked. Which items are read in the background: the most recent "continue watching" entries of every user
(one per show, unfinished, from where they stopped) and then the next episode of recently watched shows, from 0; a
fixed number however big the library is. The raw endpoint in pre mode: served from the drive and written into the
whole-file cache without touching the downloader or counting as playback; a second read is local; a stop request
cuts it. Real playback starting stops a background or detail read (a "next" read only when it is that same item).
A detail request replaces an older detail request and pre-empts a background read; an episode near its end asks for
the next one. Files that play directly in the browser are not read. With a real ffmpeg: reading from 30 s leaves the
bytes that a playback from 30 s needs local, so that playback asks the drive for nothing. The page's endpoints:
POST /api/library/preread queues the item for a signed-in user, POST /api/library/started logs one line (no URL).
"""

from __future__ import annotations

import http.client
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.parse
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from server.proxyenv import loopback_bypass
from server.relay import home, library, librarypre

import test_relay_library as lib_base
import test_relay_library_cache as cache_base
import test_relay_library_full as full_base

KB = 1024


class Clock:
    def __init__(self):
        self.now = 1_800_000_000.0

    def __call__(self):
        self.now += 1
        return self.now


# ---------------------------------------------------------------- what to read

class TargetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        root = base / "videos"
        for rel in ("电影/甲 (2020).mkv", "电影/乙 (2021).mkv", "剧/某剧/S01E01.mkv", "剧/某剧/S01E02.mkv",
                    "剧/某剧/S01E03.mkv", "剧/另一部/S01E01.mkv", "剧/另一部/S01E02.mkv"):
            lib_base.touch(root / rel, lib_base.MP4)
        self.lib = lib_base.quiet(base / "data")
        self.assertTrue(self.lib.add({"kind": "local", "path": str(root)})["ok"])
        self.ids = {i["path"]: i["id"] for i in self.lib.items()}
        self.store = home.PcProgress(base / "pc-progress.json", clock=Clock())
        self.pre = self.lib.pre
        self.pre.progress = lambda: self.store
        self.pre._eligible = lambda item: bool(item)     # these are local files; the choice is what is tested

    def tearDown(self):
        self.tmp.cleanup()

    def put(self, user, rel, position, duration):
        iid = self.ids[rel]
        self.store.put(user, "lib:" + iid, {"bvid": iid, "site": "library", "position": position,
                                            "duration": duration})

    def test_continue_watching_then_the_next_episodes(self):
        self.put(1, "剧/某剧/S01E01.mkv", 2000, 2400)      # older episode of the same show
        self.put(1, "电影/乙 (2021).mkv", 3000, 3010)        # finished
        self.put(2, "剧/另一部/S01E01.mkv", 100, 1200)       # another user
        self.put(1, "剧/某剧/S01E02.mkv", 300, 2400)
        self.put(1, "电影/甲 (2020).mkv", 600, 7000)
        want = [("电影/甲 (2020).mkv", 600), ("剧/某剧/S01E02.mkv", 300), ("剧/另一部/S01E01.mkv", 100),
                ("剧/某剧/S01E03.mkv", 0), ("剧/另一部/S01E02.mkv", 0)]
        self.assertEqual(self.pre.targets(), [(self.ids[rel], at) for rel, at in want])

    def test_a_fixed_number_however_long_the_history(self):
        with mock.patch.object(librarypre, "CONTINUE_MAX", 2), mock.patch.object(librarypre, "NEXT_MAX", 1):
            self.put(1, "剧/某剧/S01E01.mkv", 100, 2400)
            self.put(1, "剧/另一部/S01E01.mkv", 100, 1200)
            self.put(1, "电影/甲 (2020).mkv", 100, 7000)
            self.put(1, "电影/乙 (2021).mkv", 100, 7000)
            found = self.pre.targets()
        self.assertEqual(found, [(self.ids["电影/乙 (2021).mkv"], 100), (self.ids["电影/甲 (2020).mkv"], 100),
                                 (self.ids["剧/另一部/S01E02.mkv"], 0)])

    def test_nothing_without_progress(self):
        self.pre.progress = lambda: None
        self.assertEqual(self.pre.targets(), [])

    def test_resume_position_of_one_user(self):
        self.put(1, "电影/甲 (2020).mkv", 600, 7000)
        self.put(1, "电影/乙 (2021).mkv", 6990, 7000)
        self.assertEqual(self.pre.resume_at(1, self.ids["电影/甲 (2020).mkv"]), 600)
        self.assertEqual(self.pre.resume_at(2, self.ids["电影/甲 (2020).mkv"]), 0, "someone else's progress")
        self.assertEqual(self.pre.resume_at(1, self.ids["电影/乙 (2021).mkv"]), 0, "finished: from the start")

    def test_which_episode_a_show_page_reads(self):
        first, second = self.ids["剧/某剧/S01E01.mkv"], self.ids["剧/某剧/S01E02.mkv"]
        show = self.lib.show_key("某剧")
        self.assertEqual(self.pre.pick(1, show), first, "never watched: the first episode")
        self.put(1, "剧/某剧/S01E01.mkv", 500, 2400)
        self.assertEqual(self.pre.pick(1, show), first, "half watched: that one")
        self.assertEqual(self.pre.pick(2, show), first, "someone else's progress does not count")
        self.put(1, "剧/某剧/S01E01.mkv", 2390, 2400)
        self.assertEqual(self.pre.pick(1, show), second, "finished: the next one")
        self.put(1, "剧/某剧/S01E03.mkv", 2390, 2400)
        self.assertIsNone(self.pre.pick(1, show), "the last one finished: nothing")
        self.assertIsNone(self.pre.pick(1, "0" * 12))

    def test_requests_and_who_gives_way(self):
        a, b = self.ids["电影/甲 (2020).mkv"], self.ids["电影/乙 (2021).mkv"]
        self.pre.want(a, 10, "detail")
        self.pre.want(b, 0, "detail")
        self.assertEqual(self.pre._urgent, [(b, 0.0, "detail")], "a newer detail page replaces the older one")
        # A background read gives way to a request.
        self.pre._job = {"iid": a, "why": "idle", "stop": False}
        self.pre.want(a, 0, "next")
        self.assertTrue(self.pre._job["stop"])
        # Real playback stops a background or detail read, and a "next" read only when it plays that very item.
        for why, playing, stops in (("idle", b, True), ("detail", b, True), ("next", b, False), ("next", a, True)):
            self.pre._job = {"iid": a, "why": why, "stop": False}
            self.pre.yield_to_playback(playing)
            self.assertEqual(self.pre._job["stop"], stops, (why, playing))
        self.pre._job = None

    def test_near_the_end_asks_for_the_next_episode(self):
        first, second = self.ids["剧/某剧/S01E01.mkv"], self.ids["剧/某剧/S01E02.mkv"]
        self.pre.near_end(first, 1000, 2400)
        self.assertEqual(self.pre._urgent, [])
        self.pre.near_end(first, 2400 - librarypre.NEXT_NEAR_S + 10, 2400)
        self.assertEqual(self.pre._urgent, [(second, 0.0, "next")])
        self.pre._urgent.clear()
        self.pre.near_end(self.ids["剧/某剧/S01E03.mkv"], 2390, 2400)
        self.pre.near_end(self.ids["电影/甲 (2020).mkv"], 6990, 7000)
        self.assertEqual(self.pre._urgent, [], "no episode after the last one, none for a movie")


class DirectTests(unittest.TestCase):
    def test_files_the_browser_plays_directly_are_not_read(self):
        def media(*streams):
            return {"streams": [dict(s, i=n) for n, s in enumerate(streams)]}
        h264 = {"t": "video", "c": "h264", "p": "High", "pix": "yuv420p"}
        hevc10 = {"t": "video", "c": "hevc", "p": "Main 10", "pix": "yuv420p10le"}
        aac, eac3 = {"t": "audio", "c": "aac"}, {"t": "audio", "c": "eac3"}
        direct = library.Library.likely_direct
        self.assertTrue(direct({"ext": ".mp4"}, media(h264, aac)))
        self.assertTrue(direct({"ext": ".mp4"}, media(hevc10, aac)))
        self.assertTrue(direct({"ext": ".mp4"}, None), "not probed yet: by the extension")
        self.assertFalse(direct({"ext": ".mp4"}, media(hevc10, eac3)), "eac3 is remuxed")
        self.assertFalse(direct({"ext": ".mkv"}, media(h264, aac)))
        self.assertFalse(direct({"ext": ".mp4"}, media(h264, aac, {"t": "subtitle", "c": "mov_text"})))


# ---------------------------------------------------------------- the raw endpoint in pre mode

class PreFront(cache_base.RawFront):
    def do_GET(self):
        self.lib.serve_file(self, self.path.rsplit("/", 1)[-1], raw=True, pre=True)


class ServePreTests(full_base.ServeBase):
    def get_pre(self, wanted):
        server = ThreadingHTTPServer(("127.0.0.1", 0), type("P", (PreFront,), {"lib": self.lib}))
        threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.fronts.append(server)
        return self.get(wanted, server.server_address[1])

    def test_reads_into_the_whole_file_cache_without_the_downloader(self):
        self.lib.pre._job = {"iid": self.iid, "why": "detail", "stop": False}
        self.drive()
        response, data = self.get_pre(f"{1024 * KB}-{1536 * KB - 1}")
        self.assertEqual((response.status, data == full_base.MKV[1024 * KB:1536 * KB]), (206, True))
        self.assertEqual(self.runs(), [[1024 * KB, 1536 * KB]], "written into the whole-file cache")
        self.assertIsNone(self.lib.full._job, "the downloader is not pointed at it")
        self.assertEqual(self.lib._serving, 0, "not counted as somebody playing")
        self.assertEqual(self.lib.pre._read, 512 * KB, "counted as the pre-read's drive bytes")
        self.assertTrue(self.drive())
        response, data = self.get_pre(f"{1024 * KB}-{1536 * KB - 1}")
        self.assertEqual((data == full_base.MKV[1024 * KB:1536 * KB], self.drive()), (True, []), "second time: local")
        # Real playback of the same range is now local too.
        response, data = self.get(f"{1024 * KB}-{1536 * KB - 1}")
        self.assertEqual((data == full_base.MKV[1024 * KB:1536 * KB], self.drive()), (True, []))
        self.lib.pre._job = None

    def test_a_stopped_read_is_cut(self):
        self.lib.pre._job = {"iid": self.iid, "why": "idle", "stop": True}
        response, data = self.get_pre("0-")
        self.assertEqual((response.status, self.drive()), (503, []))
        self.lib.pre._job = None


class RawRouter(cache_base.RawFront):
    """library.raw without the loopback check: /tv/api/library/raw/<id>?t=…[&w=1][&p=1]."""

    def do_GET(self):
        url = urllib.parse.urlsplit(self.path)
        query = dict(urllib.parse.parse_qsl(url.query))
        iid = url.path.rsplit("/", 1)[-1]
        if not self.lib.raw_ok(iid, query.get("t")):
            self._not_found()
            return
        self.lib.serve_file(self, iid, raw=True, warm=query.get("w") == "1", pre=query.get("p") == "1")


@unittest.skipUnless(cache_base.FFMPEG and cache_base.FFPROBE, "needs ffmpeg and ffprobe")
class RealFfmpegTests(full_base.ServeBase):
    def test_reading_from_30_s_makes_a_play_from_30_s_local(self):
        # The real piece (4 MB) is most of this small file; a 4K file has ~60 MB in PRE_S seconds.
        patcher = mock.patch.object(library, "PRE_PIECE", 64 * KB)
        patcher.start()
        self.addCleanup(patcher.stop)
        made = self.root / "real.mkv"
        subprocess.run([cache_base.FFMPEG, "-v", "error", "-nostdin", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=25",
                        "-t", "80", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "10", "-g", "50", str(made)],
                       check=True)
        data = made.read_bytes()
        self.seen.files["剧集/剧/剧 S01E01.mkv"] = data
        self.lib.scan_now(self.lib._load_sources()[0]["id"])
        self.item = self.lib.items()[0]
        server = ThreadingHTTPServer(("127.0.0.1", 0), type("R", (RawRouter,), {"lib": self.lib}))
        threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.fronts.append(server)
        self.lib.wire(cache_base.FFMPEG, "k" * 32, lambda: server.server_address[1])
        self.lib.ffmpeg = cache_base.FFMPEG
        self.assertEqual(self.lib.pre.run(self.iid, 30, "detail"), "done")
        have = sum(b - a for a, b in self.runs())
        self.assertGreater(have, 0)
        self.assertLess(have, len(data) * 0.6, "only around 30 s, not the whole file")
        self.assertTrue(any("片库预读（打开详情页）" in line and "从 0:30 起" in line for line in self.logs), self.logs)
        self.assertNotIn(self.url, "\n".join(self.logs))
        self.assertEqual(self.lib.pre.run(self.iid, 40, "detail"), "skip", "same 30 s bucket: read already")
        self.drive()
        play = subprocess.run([cache_base.FFMPEG, "-v", "error", "-nostdin", "-ss", "30", "-i",
                               self.lib.raw_url(self.iid), "-map", "0:V:0?", "-t", "12", "-c", "copy", "-f", "null",
                               "-"], capture_output=True, timeout=60, env=loopback_bypass(os.environ))
        self.assertEqual(play.returncode, 0, play.stderr)
        self.assertEqual(self.drive(), [], "a play from 30 s asks the drive for nothing")


# ---------------------------------------------------------------- the page's endpoints

class EndpointTests(unittest.TestCase):
    selfhost = True
    setUp = full_base.EndpointTests.setUp
    tearDown = full_base.EndpointTests.tearDown
    setUp_relay = full_base.EndpointTests.setUp_relay
    tearDown_relay = full_base.EndpointTests.tearDown_relay
    online = full_base.EndpointTests.online
    request = full_base.EndpointTests.request
    call = full_base.EndpointTests.call

    def test_preread_and_started(self):
        lib = self.relay.library
        logs = []
        self.relay.log = logs.append
        with mock.patch.object(lib.pre, "want", return_value=True) as want:
            status, answer = self.call("alice", "POST", "/tv/api/library/preread", {"id": "0123456789abcdef"})
            self.assertEqual((status, answer), (200, {"ok": True, "queued": True}))
            self.assertEqual(want.call_args[0], ("0123456789abcdef", 0.0, "detail"))
            self.assertFalse(self.call("alice", "POST", "/tv/api/library/preread", {"id": "../x"})[1]["ok"])
            self.assertFalse(self.call("guest", "POST", "/tv/api/library/preread", {"id": "0123456789abcdef"})[1]["ok"])
            self.assertEqual(want.call_count, 1, "a guest queues nothing")
        item = {"id": "0123456789abcdef", "title": "第 3 集", "kind": "episode", "show": "某剧", "size": 1,
                "mtime": 1, "path": "x", "source": "s"}
        with mock.patch.object(lib, "item", return_value=item):
            status, answer = self.call("alice", "POST", "/tv/api/library/started",
                                       {"id": "0123456789abcdef", "wait_ms": 31500, "speed": 0.4, "rate": 2_750_000,
                                        "rushed": True})
        self.assertEqual((status, answer["ok"]), (200, True))
        line = next(m for m in logs if "片库起播" in m)
        self.assertIn("等了 31.5 秒出画面", line)
        self.assertIn("片子 22 Mbit/s", line)
        self.assertIn("网盘约 9 Mbit/s（0.4 倍）", line)
        self.assertIn("开头没预读过", line)
        self.assertIn("按了「先放」", line)


if __name__ == "__main__":
    unittest.main()
