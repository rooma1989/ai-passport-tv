"""Separate budgets for watching on the PC (server/relay/watch.py, vod.py).

What is checked: streams that only copy/remux the picture (and VOD sessions
and warm streams) share MAX_COPY, streams that transcode the picture have
their own MAX_ENCODE; a refusal says which limit was hit (Busy.limit,
busy_message, web.py's _watch_busy); a stream that starts as copy and then
finds it must transcode takes a transcode slot only at that moment (freeing
its copy slot), and stops with the "transcode limit" message when there is
none; VOD counts against the copy budget, never the transcode one; warm
streams still give way before anybody is refused, also when transcoding
streams are running; the per-user limit counts every kind; and the
self-host config's [pc] copy_streams / transcode_streams reach the relay as
--pc-copy / --pc-transcode.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from server.relay import vod, watch  # noqa: E402
from test_relay_watch import FakeProc, KEY, LIVE_DATA  # noqa: E402

HEVC_DUMP = ["Input #0, hls, from 'https://x/hevc.m3u8':",
             "  Stream #0:0: Video: hevc (Main), yuv420p, 1920x1080",
             "  Stream #0:1: Audio: aac (LC), 48000 Hz, stereo",
             "Stream mapping:"]
ENCODE = watch.Mode("encode", "encode")


class Popen:
    """ffmpeg: a source whose address says "hevc" prints an HEVC input dump, others H.264."""

    def __init__(self):
        self.procs: list[FakeProc] = []

    def __call__(self, args, **_kwargs):
        hevc = any("hevc" in str(a) for a in args)
        proc = FakeProc(args, lines=HEVC_DUMP if hevc else None, data=LIVE_DATA)
        self.procs.append(proc)
        return proc


def live(n: int, hevc: bool = False) -> watch.Source:
    url = f"https://x/{'hevc' if hevc else 'h264'}{n}.m3u8"
    return watch.Source(("live", f"ch{n:03d}", url, "UA"), url=url, user_agent="UA")


def library(n: int) -> watch.Source:
    """A library file already probed as HEVC the browser cannot play: known to transcode before it starts."""
    url = f"https://x/hevc-file{n}.mkv"
    return watch.Source(("library", f"f{n}"), url=url, mode=ENCODE)


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.t = 100.0
        self.logs: list[str] = []
        self.popen = Popen()
        self.service = watch.WatchService("ffmpeg", watch.derive_secret(KEY), log=self.logs.append,
                                          clock=lambda: self.t, popen=self.popen, background=False,
                                          max_per_user=99, warm_max=2)

    def tearDown(self):
        self.service.stop_all()

    def running(self, encoding: bool) -> int:
        return sum(1 for s in self.service.streams() if s.alive and s.encoding == encoding)

    def test_defaults(self):
        self.assertEqual((watch.MAX_COPY, watch.MAX_ENCODE), (6, 2))
        self.assertEqual((self.service.max_copy, self.service.max_encode), (6, 2))

    def test_copy_and_transcode_are_counted_separately(self):
        for n in range(watch.MAX_COPY):
            self.service.open(f"pc{n}-aaaaaaaa", live(n), user=n)
        with self.assertRaises(watch.Busy) as caught:
            self.service.open("pc9-aaaaaaaa", live(9), user=9)
        self.assertEqual((caught.exception.limit, caught.exception.cap), ("copy", 6))
        self.assertFalse(caught.exception.per_user)
        # Six copies running: transcoding streams still fit, up to their own limit.
        first = self.service.open("pc10-aaaaaaaa", library(1), user=10)
        self.service.open("pc11-aaaaaaaa", library(2), user=11)
        self.assertTrue(first.encoding)
        self.assertEqual((self.running(False), self.running(True)), (6, 2))
        with self.assertRaises(watch.Busy) as caught:
            self.service.open("pc12-aaaaaaaa", library(3), user=12)
        self.assertEqual((caught.exception.limit, caught.exception.cap), ("encode", 2))
        # Closing a transcoding stream frees a transcode slot, not a copy one.
        self.service.close("pc10-aaaaaaaa")
        self.service.open("pc12-aaaaaaaa", library(3), user=12)
        with self.assertRaises(watch.Busy):
            self.service.open("pc13-aaaaaaaa", live(13), user=13)

    def test_the_same_slot_switching_kind_is_not_counted_twice(self):
        self.service.max_encode = 1
        self.service.open("pc1-aaaaaaaa", library(1), user=1)
        # The page that holds the only transcode slot may switch to another file that transcodes.
        self.assertTrue(self.service.open("pc1-aaaaaaaa", library(2), user=1, restart=True).encoding)

    def test_copy_falling_back_to_transcode_takes_a_transcode_slot_then(self):
        stream = self.service.open("pc1-aaaaaaaa", live(1, hevc=True), user=1)
        self.assertFalse(stream.encoding, "admitted as copy: nothing says it needs transcoding yet")
        self.assertTrue(stream.wait_ready(5))
        self.assertTrue(stream.encoding)
        self.assertEqual(len(self.popen.procs), 2, "restarted once, transcoding")
        args = self.popen.procs[1].args
        self.assertEqual(args[args.index("-c:v") + 1], "libx264")
        self.assertEqual((self.running(False), self.running(True)), (0, 1), "its copy slot was given back")
        # Remembered: the next open of that channel is counted as transcoding from the start.
        self.service.close("pc1-aaaaaaaa")
        again = self.service.open("pc2-aaaaaaaa", live(1, hevc=True), user=2)
        self.assertTrue(again.encoding)

    def test_fallback_without_a_transcode_slot_stops_and_says_why(self):
        self.service.open("pc1-aaaaaaaa", library(1), user=1)
        self.service.open("pc2-aaaaaaaa", library(2), user=2)
        stream = self.service.open("pc3-aaaaaaaa", live(3, hevc=True), user=3)
        self.assertFalse(stream.wait_ready(5))
        stream._thread.join(5)
        self.assertEqual(stream.error, watch.busy_message("encode", 2))
        self.assertIn("转码", stream.error)
        self.assertFalse(stream.alive)
        self.assertFalse(stream.encoding)
        self.assertEqual(len([p for p in self.popen.procs if "hevc3" in " ".join(p.args)]), 1,
                         "never started the transcoding ffmpeg")
        self.assertTrue(any("停了" in m and "转码" in m for m in self.logs))
        # Its copy slot is free again and the other two kept theirs.
        self.assertEqual((self.running(False), self.running(True)), (0, 2))

    def test_no_transcoding_at_all(self):
        self.service.max_encode = 0
        with self.assertRaises(watch.Busy) as caught:
            self.service.open("pc1-aaaaaaaa", library(1), user=1)
        self.assertEqual(caught.exception.limit, "encode")
        self.assertTrue(self.service.open("pc1-aaaaaaaa", live(1), user=1).alive, "copying still works")

    def test_per_user_counts_every_kind(self):
        self.service.max_per_user = 2
        self.service.open("pc1-aaaaaaaa", live(1), user=1)
        self.service.open("pc1-bbbbbbbb", library(1), user=1)
        for source in (live(2), library(2)):
            with self.assertRaises(watch.Busy) as caught:
                self.service.open("pc1-cccccccc", source, user=1)
            self.assertTrue(caught.exception.per_user)
            self.assertEqual((caught.exception.limit, caught.exception.cap), ("user", 2))

    def test_warm_streams_give_way_to_copies_and_ignore_transcoding(self):
        self.service.max_copy = 3
        self.service.open("pc1-aaaaaaaa", library(1), user=1)
        self.service.open("pc2-aaaaaaaa", library(2), user=2)
        self.service.open("pc5-aaaaaaaa", live(5), user=5)
        self.assertEqual(self.service.warm(5, live(6)), "started")
        self.t += 1
        self.assertEqual(self.service.warm(5, live(7)), "started", "transcoding streams leave the copy budget alone")
        self.assertEqual(self.service.warm(5, live(8)), "started", "replaces the oldest (warm_max)")
        self.assertEqual(len(self.service.warm_streams()), 2)
        self.service.open("pc3-aaaaaaaa", live(3), user=3)
        self.assertEqual(len(self.service.warm_streams()), 1, "a warm stream made room")
        self.service.open("pc4-aaaaaaaa", live(4), user=4)
        self.assertEqual(self.service.warm_streams(), [])
        self.assertEqual(self.service.warm(5, live(9)), "full")
        with self.assertRaises(watch.Busy) as caught:
            self.service.open("pc9-aaaaaaaa", live(9), user=9)
        self.assertEqual(caught.exception.limit, "copy")

    def test_messages_say_which_limit(self):
        self.assertEqual(watch.busy_message("copy", 6), "已经有 6 路在电脑上看了，先关掉一路")
        self.assertIn("同时转码的 2 路已经满了", watch.busy_message("encode", 2))
        self.assertEqual(watch.busy_message("user", 2), "你已经在 2 个地方在电脑上看了，先关掉一个")


class VodBudgetTests(unittest.TestCase):
    def setUp(self):
        self.watch = watch.WatchService("ffmpeg", watch.derive_secret(KEY), log=lambda _m: None,
                                        popen=Popen(), background=False, max_per_user=99)
        self.vod = vod.VodService(b"v" * 32, watch=self.watch, log=lambda _m: None, background=False)

    def tearDown(self):
        self.watch.stop_all()

    @staticmethod
    def video(n: int) -> vod.VodSource:
        return vod.VodSource(("video", "up01", "bilibili", f"BV{n}"), resolve=lambda: (f"https://v/{n}.mp4", ""),
                             label=f"v{n}")

    def test_vod_uses_the_copy_budget_only(self):
        self.watch.max_copy = 2
        self.vod.open("pc1-aaaaaaaa", self.video(1), user=1)
        self.watch.open("pc2-aaaaaaaa", library(1), user=2)
        self.watch.open("pc3-aaaaaaaa", library(2), user=3)
        self.watch.open("pc4-aaaaaaaa", live(4), user=4)
        with self.assertRaises(watch.Busy) as caught:
            self.vod.open("pc5-aaaaaaaa", self.video(5), user=5)
        self.assertEqual((caught.exception.limit, caught.exception.cap), ("copy", 2))
        with self.assertRaises(watch.Busy) as caught:
            self.watch.open("pc5-aaaaaaaa", live(5), user=5)
        self.assertEqual(caught.exception.limit, "copy")
        # The same page switching from HLS to VOD frees its own slot first.
        self.vod.open("pc4-aaaaaaaa", self.video(4), user=4)


class ConfigTests(unittest.TestCase):
    def test_pc_stream_settings(self):
        from server.selfhost import config
        from server.selfhost.__main__ import pc_stream_caps
        default = config.parse(config.DEFAULT_TEXT)
        self.assertEqual((default.pc_copy, default.pc_transcode), (watch.MAX_COPY, watch.MAX_ENCODE))
        self.assertEqual(config.parse("").pc_copy, watch.MAX_COPY, "files written before this setting existed")
        self.assertEqual(pc_stream_caps(default), [], "defaults are not passed on")
        mine = config.parse("[pc]\ncopy_streams = 4\ntranscode_streams = 0\n")
        self.assertEqual(pc_stream_caps(mine), ["--pc-copy", "4", "--pc-transcode", "0"])
        for bad in ("copy_streams = 0", "transcode_streams = -1", "copy_streams = 99", 'copy_streams = "6"'):
            with self.assertRaises(config.ConfigError):
                config.parse(f"[pc]\n{bad}\n")


if __name__ == "__main__":
    unittest.main()
