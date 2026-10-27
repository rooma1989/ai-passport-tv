"""YouTube on-demand playback on the PC (server/relay/ytvod.py, the DASH half of vod.py).

What is checked:

- sidx: version 0 and 1 boxes, the init segment ending at moov, a file read from its head (more
  bytes asked when the index is longer than the first read, nothing asked past the end), the one-request
  path when yt-dlp gives init_range/index_range, and what is refused (hierarchical sidx, moof before any
  sidx). A real ffmpeg DASH file is parsed too when ffmpeg is installed.
- Format choice: the best H.264 video-only at or below the chosen quality plus the original-language,
  non-DRC AAC; the lowest above when none is below; the quality menu; VP9/AV1-only or no MediaSource falls
  back to the 360p progressive file (format 18); nothing playable is None; the same formats are found
  again after re-resolving.
- The resolve cache: expiry from the googlevideo `expire` parameter minus the margin, LRU bound, a play
  waiting for a prefetch of the same video, a play of another video not waiting for a prefetch.
- The prefetcher: newest first, de-duplicated, bounded, a per-minute cap, paused while a play resolves,
  a busy node retried a few times.
- The node: /yt/formats answers the slim format list (direct files only, ranges passed through), a
  prefetch is refused while yt-dlp is busy and killed when a real request arrives (and a real request for
  the same video waits for it), fresh=1 skips the cache, the cache is bounded; an old node without the path
  makes YouTube.formats say "unsupported".
- The upstream fetcher through a local HTTP CONNECT proxy: Range passes through, connections are
  reused, redirects are followed, 4xx becomes HTTPError; vod.py serves the v/a tracks through it and
  re-resolves once on 403.
- The relay: POST /tv/api/watch answers mode "dash" with the base URL, the quality menu and the start;
  the manifest and track ranges come through the relay port; no MediaSource ("nodash") gives the
  progressive file; VP9-only gives the progressive file; an old node, a cstone node (not local mode)
  or an old console stay on HLS; a deleted video says so; another quality is another stream;
  POST /tv/api/yt/prefetch validates and queues; Play to this PC passes dash/ytq.
- The page's segment math (tests/console_dash_test.mjs under Node).

Everything runs on 127.0.0.1; YouTube is never contacted.
"""

from __future__ import annotations

import base64
import json
import select
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from server.relay import vod, web, ytnode, ytvod
from server.relay.bilibili import SourceError
from server.relay.state import Video
from server.relay.youtube import YouTube

import test_relay_vod as tv
import test_relay_watch as tw
from test_relay_web import FakeYouTube

FFMPEG = shutil.which("ffmpeg")


# ---------------------------------------------------------------- synthetic DASH files

def box(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I4s", 8 + len(payload), kind) + payload


def sidx(refs, timescale=1000, earliest=0, first=0, version=0) -> bytes:
    """refs: [(referenced_size, duration[, reference_type])]."""
    body = bytes([version, 0, 0, 0]) + struct.pack(">II", 1, timescale)
    body += struct.pack(">II", earliest, first) if version == 0 else struct.pack(">QQ", earliest, first)
    body += struct.pack(">HH", 0, len(refs))
    for ref in refs:
        size, duration = ref[0], ref[1]
        kind = ref[2] if len(ref) > 2 else 0
        body += struct.pack(">III", (kind << 31) | size, duration, 0x90000000)
    return box(b"sidx", body)


def dash_file(sizes, duration_ms=5000, timescale=1000, moov_pad=600, version=0, earliest=0):
    """ftyp + moov + sidx + one mdat per segment; returns (bytes, init end, sidx start, segment offsets)."""
    head = box(b"ftyp", b"dash\0\0\0\0iso6avc1") + box(b"moov", b"\0" * moov_pad)
    index = sidx([(s, duration_ms * timescale // 1000) for s in sizes], timescale=timescale, version=version,
                 earliest=earliest)
    segments = b"".join(box(b"mdat", bytes([n % 251]) * (s - 8)) for n, s in enumerate(sizes))
    offsets = [len(head) + len(index)]
    for s in sizes:
        offsets.append(offsets[-1] + s)
    return head + index + segments, len(head) - 1, len(head), offsets


def fetcher_of(data: bytes, calls: list):
    def fetch(start, end):
        calls.append((start, end))
        return data[start:end + 1]
    return fetch


class SidxTests(unittest.TestCase):
    def test_version_0_and_1(self):
        for version in (0, 1):
            data, init_end, at, offsets = dash_file([1000, 2000, 1500], version=version, earliest=67)
            calls = []
            index = ytvod.read_index(fetcher_of(data, calls))
            self.assertEqual(index.init, (0, init_end))
            self.assertEqual(index.offsets, tuple(offsets))
            self.assertEqual(index.times, (0.067, 5.067, 10.067, 15.067))
            self.assertEqual((index.count, index.duration), (3, 15.067))
            self.assertEqual(calls, [(0, ytvod.HEAD_BYTES - 1)], "one read of the head, the file is shorter")
            self.assertEqual(index.head, data[:init_end + 1], "the init segment is kept for the manifest")

    def test_a_long_index_is_read_in_more_requests(self):
        sizes = [100] * 9000          # 108 KB of sidx: longer than the first read
        data, _, _, offsets = dash_file(sizes, duration_ms=1000)
        calls = []
        index = ytvod.read_index(fetcher_of(data, calls))
        self.assertEqual(index.count, 9000)
        self.assertEqual(index.offsets[-1], offsets[-1])
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1][0], ytvod.HEAD_BYTES)

    def test_ranges_from_yt_dlp_take_one_request(self):
        data, init_end, at, offsets = dash_file([1000, 1000])
        index_end = offsets[0] - 1
        calls = []
        index = ytvod.read_index(fetcher_of(data, calls), init_range=(0, init_end), index_range=(at, index_end))
        self.assertEqual(calls, [(0, index_end)])
        self.assertEqual((index.init, index.offsets), ((0, init_end), tuple(offsets)))
        self.assertEqual(index.head, data[:init_end + 1])
        saved, ytvod.INIT_INLINE_MAX = ytvod.INIT_INLINE_MAX, init_end
        try:
            self.assertEqual(ytvod.read_index(fetcher_of(data, []), init_range=(0, init_end),
                                              index_range=(at, index_end)).head, b"", "too big to carry")
        finally:
            ytvod.INIT_INLINE_MAX = saved

    def test_refused_indexes(self):
        head = box(b"ftyp", b"x" * 8) + box(b"moov", b"\0" * 10)
        nested = head + sidx([(500, 1000, 1)])
        with self.assertRaisesRegex(ytvod.BadIndex, "分层"):
            ytvod.read_index(fetcher_of(nested, []))
        no_index = head + box(b"moof", b"\0" * 20) + box(b"mdat", b"\0" * 20)
        with self.assertRaisesRegex(ytvod.BadIndex, "没有 sidx"):
            ytvod.read_index(fetcher_of(no_index, []))
        with self.assertRaises(ytvod.BadIndex):
            ytvod.read_index(fetcher_of(box(b"ftyp", b"x") + sidx([(10, 10)]), []))   # sidx before moov
        with self.assertRaises(ytvod.BadIndex):
            ytvod.read_index(fetcher_of(b"", []))
        with self.assertRaises(ytvod.BadIndex):
            ytvod.parse_sidx(sidx([(10, 10)])[:30], 0)

    @unittest.skipUnless(FFMPEG, "ffmpeg is not installed")
    def test_a_real_ffmpeg_dash_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "v.mp4"
            done = subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                                   "testsrc=size=160x90:rate=10:duration=12", "-c:v", "libx264", "-preset",
                                   "ultrafast", "-pix_fmt", "yuv420p", "-g", "20", "-keyint_min", "20",
                                   "-sc_threshold", "0", "-an", "-movflags", "+dash+global_sidx", "-f", "mp4",
                                   str(out)], capture_output=True, timeout=60)
            if done.returncode != 0:
                self.skipTest("this ffmpeg cannot write DASH files")
            data = out.read_bytes()
        index = ytvod.read_index(fetcher_of(data, []))
        self.assertGreaterEqual(index.count, 5)
        self.assertAlmostEqual(index.duration, 12, delta=0.5)
        self.assertEqual(data[index.init[1] + 5:index.init[1] + 9], b"sidx", "init ends right before sidx")
        for start in index.offsets[:-1]:
            self.assertEqual(data[start + 4:start + 8], b"moof", "every segment starts with a fragment")


# ---------------------------------------------------------------- choosing formats

def fmt(fid, vcodec="none", acodec="none", height=0, ext="mp4", tbr=0, fps=30, **extra):
    width = height * 16 // 9 if height else 0
    entry = {"format_id": fid, "ext": ext, "vcodec": vcodec, "acodec": acodec, "url": f"https://v.example/{fid}?expire=2000000000",
             "protocol": "https", "tbr": tbr, "headers": {"User-Agent": "UA"}}
    if height:
        entry.update(width=width, height=height, fps=fps, format_note=f"{height}p")
    entry.update(extra)
    return entry


FORMATS = [
    fmt("18", "avc1.42001E", "mp4a.40.2", 360, tbr=600),
    fmt("134", "avc1.4d401e", height=360, tbr=600), fmt("135", "avc1.4d401f", height=480, tbr=1000),
    fmt("136", "avc1.4d401f", height=720, tbr=2000), fmt("298", "avc1.4d4020", height=720, tbr=3000, fps=60),
    fmt("137", "avc1.640028", height=1080, tbr=4000),
    fmt("248", "vp9", height=1080, ext="webm"), fmt("399", "av01.0.08M.08", height=1080),
    fmt("140", acodec="mp4a.40.2", ext="m4a", abr=129), fmt("139", acodec="mp4a.40.5", ext="m4a", abr=49),
    fmt("140-drc", acodec="mp4a.40.2", ext="m4a", abr=129),
    fmt("140-dub", acodec="mp4a.40.2", ext="m4a", abr=160, language_preference=-1),
    fmt("251", acodec="opus", ext="webm", abr=130),
    fmt("hls", "avc1", "mp4a", 720, protocol="m3u8_native"),
]


class ChooseTests(unittest.TestCase):
    def test_best_h264_at_or_below_the_quality_and_the_original_aac(self):
        for quality, expected in (("1080p", "137"), ("720p", "298"), ("480p", "135"), ("360p", "134")):
            choice, why = ytvod.choose(FORMATS, quality)
            self.assertEqual((choice.kind, choice.video["format_id"], choice.audio["format_id"], why),
                             ("dash", expected, "140", ""), quality)
        self.assertEqual(choice.choices, ("360p", "480p", "720p", "1080p"))
        self.assertEqual(choice.variant, "dash:134+140")
        self.assertEqual(ytvod.choose(FORMATS, "720p")[0].current, "720p")
        # Not on the menu: the default.
        self.assertEqual(ytvod.choose(FORMATS, "4320p")[0].video["format_id"], "298")

    def test_lowest_above_when_none_is_below(self):
        only_big = [f for f in FORMATS if f["format_id"] not in ("134", "135")]
        choice, _ = ytvod.choose(only_big, "360p")
        self.assertEqual(choice.video["format_id"], "136", "the lowest above, at its lower frame rate")
        self.assertEqual(choice.choices, ("720p", "1080p"))

    def test_vertical_video_counts_the_short_side(self):
        tall = [dict(fmt("137v", "avc1.640028"), width=1080, height=1920), fmt("140", acodec="mp4a.40.2", ext="m4a")]
        choice, _ = ytvod.choose(tall, "1080p")
        self.assertEqual((choice.video["format_id"], choice.current), ("137v", "1080p"))

    def test_fallbacks(self):
        no_h264 = [f for f in FORMATS if not f["vcodec"].startswith("avc1") or f["format_id"] == "18"]
        choice, why = ytvod.choose(no_h264, "1080p")
        self.assertEqual((choice.kind, choice.muxed["format_id"], choice.current), ("progressive", "18", "360p"))
        self.assertIn("av01", why)
        self.assertIn("vp9", why)
        choice, why = ytvod.choose(FORMATS, "1080p", dash=False)
        self.assertEqual((choice.kind, choice.variant, why), ("progressive", "file:18", ""))
        no_aac = [f for f in FORMATS if not f["acodec"].startswith("mp4a")]
        choice, why = ytvod.choose(no_aac, "720p")
        self.assertIsNone(choice)
        self.assertIn("AAC", why)
        self.assertEqual(ytvod.choose([], "720p")[0], None)

    def test_same_formats_after_re_resolving(self):
        choice, _ = ytvod.choose(FORMATS, "720p")
        again = [dict(f, url=f["url"] + "&n=2") for f in FORMATS]
        same = ytvod.same_choice(again, choice)
        self.assertEqual((same.video["url"], same.audio["format_id"]), (again[4]["url"], "140"))
        self.assertIsNone(ytvod.same_choice([f for f in again if f["format_id"] != "298"], choice))
        file = ytvod.choose(FORMATS, "720p", dash=False)[0]
        self.assertEqual(ytvod.same_choice(again, file).muxed["url"], again[0]["url"])

    def test_tracks_carry_mime_size_and_ranges(self):
        f = dict(fmt("137", "avc1.640028", height=1080, filesize=123), init_range=[0, 740], index_range=[741, 3000])
        track = ytvod.track_of(f, "v")
        self.assertEqual((track.mime, track.size, track.itag, track.init, track.index, track.height),
                         ('video/mp4; codecs="avc1.640028"', 123, "137", (0, 740), (741, 3000), 1080))
        self.assertEqual(ytvod.track_of(fmt("140", acodec="mp4a.40.2", ext="m4a"), "a").mime,
                         'audio/mp4; codecs="mp4a.40.2"')

    def test_expiry(self):
        self.assertEqual(ytvod.expires_at("https://r1.googlevideo.com/videoplayback?expire=1793000000&ei=x"), 1793000000)
        self.assertEqual(ytvod.expires_at("https://m.googlevideo.com/videoplayback/expire/1793000000/ei/x"), 1793000000)
        self.assertIsNone(ytvod.expires_at("https://v.example/a?expired=1"))
        now = 1_793_000_000 - 4 * 3600
        info = {"formats": [{"url": "https://x/?expire=1793000000"}, {"url": "https://x/?expire=1792999000"}]}
        self.assertEqual(ytvod.keep_until(info, now), 1792999000 - ytvod.EXPIRE_MARGIN_S)
        self.assertEqual(ytvod.keep_until({"formats": [{"url": "https://x/?expire=9999999999"}]}, now),
                         now + ytvod.RESOLVE_MAX_AGE_S)
        self.assertEqual(ytvod.keep_until({"formats": []}, now), now + ytvod.NO_EXPIRE_S)


# ---------------------------------------------------------------- the resolve cache and the prefetcher

class FakeFormatsClient:
    """YouTube.formats stand-in: answers per id, can block prefetches until released, records calls."""

    def __init__(self, expire=None):
        self.calls = []
        self.gate = threading.Event()
        self.gate.set()
        self.started = threading.Event()
        self.busy = set()
        self.expire = expire

    def formats(self, video_id, prefetch=False, fresh=False, timeout=None):
        self.calls.append((video_id, prefetch, fresh))
        if prefetch:
            self.started.set()
            self.gate.wait(5)
            if video_id in self.busy:
                raise SourceError("youtube /yt/formats: 503 {\"error\": \"busy\"}", reason="busy")
        expire = self.expire or int(time.time()) + 6 * 3600
        return {"id": video_id, "title": video_id, "duration": 15,
                "formats": [dict(f, url=f["url"].replace("2000000000", str(expire)) + f"&id={video_id}&n={len(self.calls)}")
                            for f in FORMATS]}


class ResolverTests(unittest.TestCase):
    def setUp(self):
        self.now = 1_793_000_000.0
        self.client = FakeFormatsClient(expire=int(self.now) + 3 * 3600)
        self.resolver = ytvod.Resolver(lambda: self.client, clock=lambda: self.now, log=lambda _m: None, size=3)

    def test_cached_until_the_address_expires_minus_the_margin(self):
        first = self.resolver.resolve("aaaaaaaaaaa")
        self.assertIs(self.resolver.resolve("aaaaaaaaaaa"), first)
        self.now += 3 * 3600 - ytvod.EXPIRE_MARGIN_S - 1
        self.assertIs(self.resolver.resolve("aaaaaaaaaaa"), first)
        self.now += 2
        self.assertIsNot(self.resolver.resolve("aaaaaaaaaaa"), first)
        self.assertEqual(len(self.client.calls), 2)
        self.resolver.resolve("aaaaaaaaaaa", fresh=True)
        self.assertEqual(self.client.calls[-1], ("aaaaaaaaaaa", False, True))

    def test_lru_bound(self):
        for vid in ("aaaaaaaaaaa", "bbbbbbbbbbb", "ccccccccccc"):
            self.resolver.resolve(vid)
        self.resolver.resolve("aaaaaaaaaaa")             # touched: newest
        self.resolver.resolve("ddddddddddd")             # evicts bbb
        self.assertIsNotNone(self.resolver.cached("aaaaaaaaaaa"))
        self.assertIsNone(self.resolver.cached("bbbbbbbbbbb"))

    def test_a_play_waits_for_the_prefetch_of_the_same_video(self):
        self.client.gate.clear()
        worker = threading.Thread(target=self.resolver.prefetch, args=("aaaaaaaaaaa",))
        worker.start()
        self.assertTrue(self.client.started.wait(5))
        got = []
        play = threading.Thread(target=lambda: got.append(self.resolver.resolve("aaaaaaaaaaa")))
        play.start()
        time.sleep(0.1)
        self.assertTrue(self.resolver.busy(), "a play is resolving: prefetching pauses")
        self.client.gate.set()
        play.join(5)
        worker.join(5)
        self.assertEqual([c[:2] for c in self.client.calls], [("aaaaaaaaaaa", True)], "one resolution for both")
        self.assertEqual(got[0]["id"], "aaaaaaaaaaa")
        self.assertFalse(self.resolver.busy())

    def test_a_play_of_another_video_does_not_wait(self):
        self.client.gate.clear()
        worker = threading.Thread(target=self.resolver.prefetch, args=("aaaaaaaaaaa",))
        worker.start()
        self.assertTrue(self.client.started.wait(5))
        started = time.monotonic()
        self.resolver.resolve("bbbbbbbbbbb")
        self.assertLess(time.monotonic() - started, 1)
        self.client.gate.set()
        worker.join(5)

    def test_a_busy_prefetch_says_so_and_a_failed_play_lets_the_waiter_retry(self):
        self.client.busy.add("aaaaaaaaaaa")
        self.assertEqual(self.resolver.prefetch("aaaaaaaaaaa"), ytvod.BUSY)
        self.assertIsNone(self.resolver.cached("aaaaaaaaaaa"))
        self.client.busy.clear()
        self.assertEqual(self.resolver.prefetch("aaaaaaaaaaa")["id"], "aaaaaaaaaaa")
        self.assertIsNone(self.resolver.prefetch("aaaaaaaaaaa"), "already cached: nothing to do")


class PrefetcherTests(unittest.TestCase):
    def make(self, busy=lambda: False, per_minute=12, outcomes=None):
        self.done, self.release = [], threading.Event()
        outcomes = outcomes or {}

        def work(vid, quality):
            self.release.wait(5)
            self.done.append((vid, quality))
            answers = outcomes.get(vid)
            return answers.pop(0) if answers else None

        return ytvod.Prefetcher(work, busy, log=lambda _m: None, per_minute=per_minute, sleep=lambda s: time.sleep(0.01))

    def test_newest_first_deduplicated_and_bounded(self):
        prefetcher = self.make()
        prefetcher.want(["aaaaaaaaaaa", "bbbbbbbbbbb"], "720p")
        time.sleep(0.05)       # the worker has taken aaa and waits
        prefetcher.want(["ccccccccccc", "bbbbbbbbbbb", "bad", "ccccccccccc"], "1080p")
        self.assertEqual(prefetcher.pending(), ["ccccccccccc", "bbbbbbbbbbb"])
        prefetcher.want([f"x{n:010d}" for n in range(6)])
        prefetcher.want([f"y{n:010d}" for n in range(6)])
        self.assertEqual(len(prefetcher.pending()), ytvod.PREFETCH_QUEUE)
        self.assertEqual(prefetcher.pending()[0], "y0000000000")
        self.release.set()
        self.assertTrue(prefetcher.wait_idle(5))
        self.assertEqual(self.done[0], ("aaaaaaaaaaa", "720p"))
        self.assertEqual(len(self.done), 1 + ytvod.PREFETCH_QUEUE)

    def test_per_minute_cap(self):
        prefetcher = self.make(per_minute=2)
        self.release.set()
        prefetcher.want(["aaaaaaaaaaa", "bbbbbbbbbbb", "ccccccccccc"])
        self.assertTrue(prefetcher.wait_idle(5))
        self.assertEqual([d[0] for d in self.done], ["aaaaaaaaaaa", "bbbbbbbbbbb"])

    def test_paused_while_a_play_resolves(self):
        playing = threading.Event()
        playing.set()
        prefetcher = self.make(busy=playing.is_set)
        self.release.set()
        prefetcher.want(["aaaaaaaaaaa"])
        time.sleep(0.2)
        self.assertEqual(self.done, [], "waits while a play resolves")
        playing.clear()
        self.assertTrue(prefetcher.wait_idle(5))
        self.assertEqual(self.done, [("aaaaaaaaaaa", ytvod.DEFAULT_QUALITY)])

    def test_a_busy_node_is_retried_a_few_times(self):
        prefetcher = self.make(outcomes={"aaaaaaaaaaa": [ytvod.BUSY] * 9})
        self.release.set()
        prefetcher.want(["aaaaaaaaaaa", "bbbbbbbbbbb"])
        self.assertTrue(prefetcher.wait_idle(5))
        self.assertEqual([d[0] for d in self.done], ["aaaaaaaaaaa"] * ytvod.BUSY_TRIES + ["bbbbbbbbbbb"])


# ---------------------------------------------------------------- the node

INFO = {"id": "aaaaaaaaaaa", "title": "t", "duration": 15, "channel_url": "https://www.youtube.com/channel/UCx",
        "_version": {"version": "2026.10.21"}, "automatic_captions": {"en": ["x" * 1000]},
        "requested_formats": [{"url": "https://v.example/134", "http_headers": {"User-Agent": "UA"}}],
        "formats": [dict(f, http_headers=f["headers"]) for f in FORMATS[:-1]]
                   + [dict(FORMATS[-1]), {"format_id": "drm", "url": "https://v.example/d", "has_drm": True}]}
INFO["formats"][5]["init_range"] = {"start": 0, "end": 740}
INFO["formats"][5]["index_range"] = "741-3000"


class FakeYtdlp:
    """A killable yt-dlp for the node's prefetch (Node.start)."""

    def __init__(self, owner, args):
        self.owner, self.vid = owner, args[-1][-11:]
        self.killed = threading.Event()
        self.returncode = None
        owner.started.set()

    def communicate(self, timeout=None):
        if self.killed.wait(self.owner.seconds):
            self.returncode = -9
            return "", ""
        self.returncode = 0
        return json.dumps(dict(INFO, id=self.vid)), ""

    def kill(self):
        self.killed.set()


class NodeTests(unittest.TestCase):
    def setUp(self):
        self.runs = []
        self.seconds = 2.0
        self.started = threading.Event()

        def run(args):
            self.runs.append(args[-1][-11:])
            return 0, json.dumps(dict(INFO, id=args[-1][-11:])), ""

        self.node = ytnode.Node(ytnode.Tools("y", "d", "f", ""), "k" * 20,
                                run=run, start=lambda args: FakeYtdlp(self, args), log=lambda _m: None)
        self.server = ytnode.make_server(self.node, "127.0.0.1", 0, None)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.client = YouTube(config={"url": f"http://127.0.0.1:{self.server.server_address[1]}", "key": "k" * 20})

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def test_formats_answer(self):
        answer = self.client.formats("aaaaaaaaaaa")
        self.assertEqual((answer["id"], answer["title"], answer["duration"]), ("aaaaaaaaaaa", "t", 15))
        ids = [f["format_id"] for f in answer["formats"]]
        self.assertNotIn("hls", ids, "only direct files")
        self.assertNotIn("drm", ids)
        f137 = next(f for f in answer["formats"] if f["format_id"] == "137")
        self.assertEqual((f137["init_range"], f137["index_range"], f137["headers"]), ([0, 740], [741, 3000], {"User-Agent": "UA"}))
        self.assertNotIn("automatic_captions", self.node._info["aaaaaaaaaaa"][1], "the cache keeps the slim info")
        # /yt/info and /yt/stream share it.
        self.assertEqual(self.client.video("aaaaaaaaaaa").title, "t")
        self.assertEqual(self.runs, ["aaaaaaaaaaa"])
        self.assertEqual(ytnode.stream_inputs(self.node._info["aaaaaaaaaaa"][1]), [("https://v.example/134", {"User-Agent": "UA"})])
        # fresh=1 resolves again.
        self.client.formats("aaaaaaaaaaa", fresh=True)
        self.assertEqual(self.runs, ["aaaaaaaaaaa", "aaaaaaaaaaa"])

    def test_cache_is_bounded(self):
        saved, ytnode.INFO_CACHE_MAX = ytnode.INFO_CACHE_MAX, 2
        try:
            for vid in ("aaaaaaaaaaa", "bbbbbbbbbbb", "ccccccccccc"):
                self.node.video_info(vid)
            self.assertEqual(set(self.node._info), {"bbbbbbbbbbb", "ccccccccccc"})
        finally:
            ytnode.INFO_CACHE_MAX = saved

    def test_prefetch_is_refused_while_busy(self):
        with self.node._ytdlp:
            with self.assertRaises(SourceError) as caught:
                self.client.formats("aaaaaaaaaaa", prefetch=True, timeout=5)
        self.assertEqual(caught.exception.reason, "busy")
        self.assertEqual(self.client.formats("aaaaaaaaaaa", prefetch=True)["id"], "aaaaaaaaaaa")

    def test_a_real_request_kills_a_running_prefetch(self):
        self.seconds = 10
        failed = []
        worker = threading.Thread(target=lambda: self._prefetch(failed))
        worker.start()
        self.assertTrue(self.started.wait(5))
        started = time.monotonic()
        self.assertEqual(self.client.formats("bbbbbbbbbbb")["id"], "bbbbbbbbbbb")
        self.assertLess(time.monotonic() - started, 3, "the play did not wait for the prefetch")
        worker.join(5)
        self.assertEqual(failed, ["busy"])
        self.assertNotIn("aaaaaaaaaaa", self.node._info)
        # A search also makes it give way.
        self.started.clear()
        worker = threading.Thread(target=lambda: self._prefetch(failed))
        worker.start()
        self.assertTrue(self.started.wait(5))
        self.node.run = lambda args: (0, json.dumps({"entries": []}), "")
        self.assertEqual(self.node.search("x", 3)[0], 200)
        worker.join(5)
        self.assertEqual(failed, ["busy", "busy"])

    def test_a_real_request_for_the_same_video_waits_for_the_prefetch(self):
        self.seconds = 0.5
        failed = []
        worker = threading.Thread(target=lambda: self._prefetch(failed))
        worker.start()
        self.assertTrue(self.started.wait(5))
        self.assertEqual(self.client.formats("aaaaaaaaaaa")["id"], "aaaaaaaaaaa")
        worker.join(5)
        self.assertEqual((failed, self.runs), ([], []), "the prefetch's answer served the play")

    def _prefetch(self, failed):
        try:
            self.client.formats("aaaaaaaaaaa", prefetch=True)
        except SourceError as error:
            failed.append(error.reason)

    def test_an_old_node_is_unsupported(self):
        class Old(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(404)
                self.send_header("Content-Length", "9")
                self.end_headers()
                self.wfile.write(b"Not Found")

            def log_message(self, *_a):
                pass

        old = ThreadingHTTPServer(("127.0.0.1", 0), Old)
        threading.Thread(target=old.serve_forever, daemon=True).start()
        try:
            client = YouTube(config={"url": f"http://127.0.0.1:{old.server_address[1]}", "key": "k"})
            with self.assertRaises(SourceError) as caught:
                client.formats("aaaaaaaaaaa")
            self.assertEqual((caught.exception.reason, caught.exception.permanent), ("unsupported", False))
        finally:
            old.shutdown()
            old.server_close()
        # A deleted video on a new node is permanent.
        self.node.run = lambda args: (1, "", "ERROR: [youtube] x: Video unavailable")
        with self.assertRaises(SourceError) as caught:
            self.client.formats("ggggggggggg")
        self.assertTrue(caught.exception.permanent)


class LocalLaneTests(unittest.TestCase):
    """Local mode: resolving a video has its own yt-dlp lane, so a play never queues behind a feed or a search."""

    def setUp(self):
        self.runs = []
        self.seconds = 2.0
        self.started = threading.Event()
        tools = ytnode.Tools("y", "d", "f", "")

        def run(args):
            self.runs.append(args[-1][-11:])
            return 0, json.dumps(dict(INFO, id=args[-1][-11:])), ""

        self.node = ytnode.Node(tools, "k" * 20, run=run, start=lambda args: FakeYtdlp(self, args),
                                log=lambda _m: None, tools_dir="unused")
        self.node.current_tools = lambda: tools

    def test_a_play_does_not_wait_for_a_feed(self):
        with self.node._ytdlp:          # a feed, a follow sync or a search is running
            started = time.monotonic()
            code, info = self.node.video_info("aaaaaaaaaaa")
            self.assertEqual((code, info["id"]), (200, "aaaaaaaaaaa"))
            self.assertLess(time.monotonic() - started, 1)
            # A prefetch is not refused either.
            self.assertEqual(self.node.video_info("bbbbbbbbbbb", prefetch=True)[0], 200)
        # And a search still waits only for its own lane.
        with self.node._play:
            self.node.run = lambda args: (0, json.dumps({"entries": []}), "")
            self.assertEqual(self.node.search("x", 3)[0], 200)

    def test_a_search_does_not_kill_a_prefetch(self):
        self.seconds = 0.5
        outcome = []
        worker = threading.Thread(target=lambda: outcome.append(self.node.video_info("ccccccccccc", prefetch=True)[0]))
        worker.start()
        self.assertTrue(self.started.wait(5))
        self.node.run = lambda args: (0, json.dumps({"entries": []}), "")
        self.assertEqual(self.node.search("x", 3)[0], 200)
        worker.join(5)
        self.assertEqual(outcome, [200], "the prefetch ran to the end")
        self.assertIn("ccccccccccc", self.node._info)

    def test_the_cloud_node_keeps_one_lane(self):
        node = ytnode.Node(ytnode.Tools("y", "d", "f", ""), "k" * 20, log=lambda _m: None)
        self.assertIs(node._play, node._ytdlp)


# ---------------------------------------------------------------- the upstream through a proxy

class ConnectProxy:
    """A local HTTP CONNECT proxy that counts tunnels (the user's [youtube] proxy stand-in)."""

    def __init__(self):
        self.tunnels = 0
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_CONNECT(self):
                owner.tunnels += 1
                host, _, port = self.path.rpartition(":")
                upstream = socket.create_connection((host, int(port)))
                self.send_response(200, "Connection established")
                self.end_headers()
                pair = [self.connection, upstream]
                try:
                    while True:
                        ready, _, _ = select.select(pair, [], [], 30)
                        if not ready:
                            return
                        for sock in ready:
                            data = sock.recv(65536)
                            if not data:
                                return
                            (upstream if sock is self.connection else self.connection).sendall(data)
                except OSError:
                    pass
                finally:
                    upstream.close()

            def log_message(self, *_a):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


VIDEO, _, _, VIDEO_OFFSETS = dash_file([40_000, 50_000, 30_000], duration_ms=5000)
AUDIO, _, _, AUDIO_OFFSETS = dash_file([8_000, 8_000], duration_ms=10000)


class FetcherTests(unittest.TestCase):
    def setUp(self):
        self.upstream = tv.Upstream()
        self.upstream.files.update({"/v.mp4": VIDEO, "/a.m4a": AUDIO})
        self.proxy = ConnectProxy()
        self.fetcher = ytvod.Fetcher(self.proxy.url)

    def tearDown(self):
        self.fetcher.close()
        self.proxy.close()
        self.upstream.close()

    def get(self, path, wanted=None, **headers):
        if wanted:
            headers["Range"] = wanted
        return self.fetcher(urllib.request.Request(self.upstream.base + path, headers=headers), 10)

    def test_ranges_through_the_proxy_on_one_connection(self):
        for start in (0, 1000, 5000):
            answer = self.get("/v.mp4", f"bytes={start}-{start + 999}", **{"User-Agent": "UA"})
            self.assertEqual((answer.status, answer.read()), (206, VIDEO[start:start + 1000]))
            self.assertEqual(answer.headers["Content-Range"], f"bytes {start}-{start + 999}/{len(VIDEO)}")
            answer.close()
        self.assertEqual(self.upstream.requests[-1]["range"], "bytes=5000-5999")
        self.assertEqual(self.upstream.requests[-1]["user-agent"], "UA")
        self.assertEqual((self.proxy.tunnels, self.fetcher.connects), (1, 1), "the tunnel is reused")
        # A response not read to the end is not put back.
        half = self.get("/v.mp4")
        half.read(10)
        half.close()
        self.get("/v.mp4", "bytes=0-9").close()
        self.assertEqual(self.fetcher.connects, 2)

    def test_errors_and_redirects(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.get("/missing")
        self.assertEqual(caught.exception.code, 404)
        self.upstream.forbid = 1
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.get("/v.mp4")
        self.assertEqual(caught.exception.code, 403)

        class Hop(BaseHTTPRequestHandler):
            def do_GET(handler):
                handler.send_response(302)
                handler.send_header("Location", self.upstream.base + "/a.m4a")
                handler.send_header("Content-Length", "0")
                handler.end_headers()

            def log_message(self, *_a):
                pass

        hop = ThreadingHTTPServer(("127.0.0.1", 0), Hop)
        threading.Thread(target=hop.serve_forever, daemon=True).start()
        try:
            answer = self.fetcher(urllib.request.Request(f"http://127.0.0.1:{hop.server_address[1]}/x",
                                                         headers={"Range": "bytes=0-99"}), 10)
            self.assertEqual(answer.read(), AUDIO[:100])
            answer.close()
        finally:
            hop.shutdown()
            hop.server_close()

    def test_vod_serves_tracks_and_the_manifest_through_it(self):
        resolved = []

        def media(n):
            return vod.Media(url="", tracks={
                "v": vod.Track(url=f"{self.upstream.base}/v.mp4?n={n}", mime='video/mp4; codecs="avc1.640028"', itag="137"),
                "a": vod.Track(url=f"{self.upstream.base}/a.m4a?n={n}", mime='audio/mp4; codecs="mp4a.40.2"', itag="140")})

        def resolve():
            resolved.append(1)
            return media(len(resolved))

        yt = ytvod.YtVod(lambda: None, proxy=lambda: self.proxy.url, log=lambda _m: None)
        service = vod.VodService(b"v" * 32, log=lambda _m: None, background=False)
        source = vod.VodSource(("video", "cast", "youtube", "aaaaaaaaaaa"), resolve=resolve, site="youtube",
                               headers=(("User-Agent", "UA"),), first=media(0), opener=yt.fetcher(), dash=True,
                               manifest=yt.manifest)
        session = service.open("pc1-aa", source, user=1)
        handler = tv.FakeHandler({"Range": f"bytes={VIDEO_OFFSETS[1]}-{VIDEO_OFFSETS[2] - 1}"})
        service.serve(handler, session, "v")
        self.assertEqual((handler.status, bytes(handler.body)), (206, VIDEO[VIDEO_OFFSETS[1]:VIDEO_OFFSETS[2]]))
        self.assertEqual(self.upstream.requests[-1]["user-agent"], "UA")
        handler = tv.FakeHandler({"Range": "bytes=0-99"})
        service.serve(handler, session, "a")
        self.assertEqual((bytes(handler.body), handler.sent["Content-Type"]), (AUDIO[:100], "video/mp4"))
        self.assertEqual(self.proxy.tunnels, 1, "both tracks over the same pooled connection")
        handler = tv.FakeHandler()
        service.serve(handler, session, "media")
        self.assertEqual(handler.status, 404, "no progressive file in a DASH stream")
        # The manifest: an expired address (403) is re-resolved once.
        self.upstream.forbid = 1
        handler = tv.FakeHandler()
        service.manifest(handler, session)
        self.assertEqual(handler.status, 200, bytes(handler.body))
        data = json.loads(bytes(handler.body))
        self.assertEqual(resolved, [1])
        self.assertEqual(data["tracks"]["v"]["o"], VIDEO_OFFSETS)
        self.assertEqual(data["tracks"]["a"]["t"], [0.0, 10.0, 20.0])
        self.assertEqual(data["tracks"]["v"]["mime"], 'video/mp4; codecs="avc1.640028"')
        self.assertEqual(data["duration"], 20.0)
        # The init segments ride along (base64), already read with the index: no extra fetch for them.
        for kind, body in (("v", VIDEO), ("a", AUDIO)):
            end = data["tracks"][kind]["init"][1]
            self.assertEqual(base64.b64decode(data["tracks"][kind]["ib"]), body[:end + 1])
        # Cached by (video, format): no upstream request the second time.
        before = len(self.upstream.requests)
        service.manifest(tv.FakeHandler(), session)
        self.assertEqual(len(self.upstream.requests), before)
        # Not a DASH stream: no manifest.
        plain = service.open("pc1-bb", vod.VodSource(("video", "cast", "youtube", "b"), resolve=resolve), user=1)
        handler = tv.FakeHandler()
        handler._not_found = lambda: handler._send(404, b"", "text/html")
        service.manifest(handler, plain)
        self.assertEqual(handler.status, 404)
        service.stop_all()
        yt.fetcher().close()

    def test_a_broken_index_answers_502(self):
        self.upstream.files["/junk.mp4"] = b"\0" * 5000
        yt = ytvod.YtVod(lambda: None, log=lambda _m: None)
        service = vod.VodService(b"v" * 32, log=lambda _m: None, background=False)
        junk = vod.Track(url=f"{self.upstream.base}/junk.mp4", mime="video/mp4", itag="1")
        source = vod.VodSource(("video", "cast", "youtube", "c"), resolve=lambda: None, site="youtube",
                               first=vod.Media(url="", tracks={"v": junk, "a": junk}), opener=yt.fetcher(),
                               dash=True, manifest=yt.manifest)
        session = service.open("pc1-cc", source, user=1)
        handler = tv.FakeHandler()
        service.manifest(handler, session)
        self.assertEqual(handler.status, 502)
        self.assertIn("索引", bytes(handler.body).decode())
        service.stop_all()


# ---------------------------------------------------------------- through the relay

class FakeYtFormats(FakeYouTube):
    """FakeYouTube with the node's /yt/formats pointing at the local upstream."""

    def __init__(self, base):
        super().__init__()
        self.base = base
        self.asked = []
        self.only = None
        self.fail = None

    def formats(self, video_id, prefetch=False, fresh=False, timeout=None):
        self.asked.append((video_id, prefetch, fresh))
        if self.fail is not None:
            raise self.fail
        files = {"134": "/v.mp4", "136": "/v.mp4", "137": "/v.mp4", "140": "/a.m4a", "18": "/clip.mp4"}
        formats = [dict(f, url=f"{self.base}{files[f['format_id']]}?id={video_id}&expire={int(time.time()) + 20000}")
                   for f in FORMATS if f["format_id"] in files]
        if self.only is not None:
            formats = [f for f in formats if f["format_id"] in self.only]
        return {"id": video_id, "title": "yt cast", "duration": 60, "formats": formats}


class RelayYtVodTests(unittest.TestCase):
    selfhost = True
    setUp_relay = tw.RelayWatchTests.setUp
    tearDown_relay = tw.RelayWatchTests.tearDown
    online = tw.RelayWatchTests.online
    request = tw.RelayWatchTests.request
    media = tv.RelayVodTests.media

    def setUp(self):
        self.setUp_relay()
        self.upstream = tv.Upstream()
        self.upstream.files.update({"/v.mp4": VIDEO, "/a.m4a": AUDIO})
        self.proxy = ConnectProxy()
        self.youtube = FakeYtFormats(self.upstream.base)
        self.relay.clients["youtube"] = self.youtube
        self.relay.youtube_kind = "local"
        self.relay.youtube_proxy = lambda: self.proxy.url
        self.relay.vod.opener = tv.no_proxy_open
        self.carol = self.accounts.create_user("Carol")
        self.cookies["carol"] = "tvs=" + self.accounts.new_session(self.carol, "pc")
        for vid in ("ytcast00001", "ytcast00002"):
            self.relay.user_state(self.carol).remember(Video(bvid=vid, title="yt", duration=15, pubdate=0, site="youtube"),
                                                       at=self.now)

    def tearDown(self):
        self.relay.vod.stop_all()
        self.tearDown_relay()
        self.upstream.close()
        self.proxy.close()

    def watch(self, vid="ytcast00001", **body):
        body.setdefault("viewer", "c0ffee00")
        body.setdefault("vod", True)
        status, _, raw = self.request("POST", "/tv/api/watch", "carol", dict(body, video={"site": "youtube", "id": vid}))
        return json.loads(raw)

    def test_dash_through_the_relay(self):
        answer = self.watch(dash=True, ytq="1080p", start=7)
        self.assertTrue(answer["ok"], answer)
        self.assertEqual((answer["mode"], answer["start"], answer["duration"], answer["seekable"]), ("dash", 7.0, 60, True))
        self.assertRegex(answer["url"], r"^vod/[0-9a-f]{16}/\d+\.[A-Za-z0-9_-]+/$")
        self.assertEqual(answer["quality"], {"current": "1080p", "choices": ["360p", "720p", "1080p"], "site": "youtube"})
        self.assertEqual(self.popen.procs, [], "no ffmpeg")
        self.assertEqual(self.youtube.opened, [], "no node stream")
        code, _, raw = self.media(answer["url"] + "manifest")
        manifest = json.loads(raw)
        self.assertEqual((code, manifest["tracks"]["v"]["o"], manifest["tracks"]["a"]["o"]), (200, VIDEO_OFFSETS, AUDIO_OFFSETS))
        code, headers, data = self.media(answer["url"] + "v", f"bytes={VIDEO_OFFSETS[0]}-{VIDEO_OFFSETS[1] - 1}")
        self.assertEqual((code, data), (206, VIDEO[VIDEO_OFFSETS[0]:VIDEO_OFFSETS[1]]))
        code, headers, data = self.media(answer["url"] + "a", "bytes=0-99")
        self.assertEqual((code, data), (206, AUDIO[:100]))
        self.assertGreaterEqual(self.proxy.tunnels, 1, "googlevideo through the YouTube proxy")
        # The same video and quality again keeps the stream; another quality is another one.
        again = self.watch(dash=True, ytq="1080p", start=9)
        self.assertEqual((again["sid"], again["start"]), (answer["sid"], 9.0))
        other = self.watch(dash=True, ytq="360p", start=9)
        self.assertNotEqual(other["sid"], answer["sid"])
        self.assertEqual(other["quality"]["current"], "360p")
        self.assertEqual(len(self.relay.vod.sessions()), 1)
        self.assertEqual(len([a for a in self.youtube.asked if not a[1]]), 1, "resolved once, then cached")

    def test_fallbacks(self):
        # No MediaSource on the page: the progressive file, played by <video>.
        answer = self.watch(dash=False)
        self.assertEqual(answer["mode"], "vod")
        self.assertTrue(answer["url"].endswith("/media"))
        self.assertNotIn("quality", answer)
        code, _, data = self.media(answer["url"], "bytes=0-99")
        self.assertEqual((code, data), (206, tv.CLIP[:100]))
        # Only VP9/AV1 video: the progressive file too.
        self.youtube.only = {"18", "140"}
        self.assertEqual(self.watch("ytcast00002", dash=True)["mode"], "vod")
        # Nothing the browser can play: HLS through the node's stream.
        self.relay.ytvod.resolver.forget("ytcast00002")
        self.youtube.only = {"140"}
        self.assertEqual(self.watch("ytcast00002", dash=True)["mode"], "hls")
        # An old console (no vod flag): HLS.
        self.assertEqual(self.watch("ytcast00001", vod=False, dash=True)["mode"], "hls")

    def test_old_node_cstone_node_and_deleted_video(self):
        self.youtube.fail = SourceError("youtube /yt/formats: 404 Not Found", reason="unsupported")
        self.assertEqual(self.watch(dash=True)["mode"], "hls")
        self.youtube.fail = SourceError("youtube /yt/formats: 503 slow")
        self.assertEqual(self.watch(dash=True)["mode"], "hls")
        self.youtube.fail = SourceError("youtube /yt/formats: 404 Video unavailable", permanent=True)
        answer = self.watch(dash=True)
        self.assertFalse(answer["ok"])
        self.assertIn("看不了", answer["error"])
        self.youtube.fail = None
        self.relay.youtube_kind = "node"
        self.assertEqual(self.watch(dash=True)["mode"], "hls", "addresses bound to cstone's IP")
        self.assertEqual(self.youtube.asked[-1][0], "ytcast00001")
        # The title and duration come from the same resolution (no /yt/info): the deleted video was asked twice,
        # once for its information (a replay from the history goes on without it) and once to play.
        self.assertEqual(len(self.youtube.asked), 4)

    def test_prefetch_endpoint(self):
        def post(body, who="carol"):
            status, _, raw = self.request("POST", "/tv/api/yt/prefetch", who, body)
            return status, json.loads(raw) if status == 200 else None

        status, answer = post({"ids": ["ytcast00001", "ytcast00002"], "q": "480p"})
        self.assertEqual(answer, {"ok": True, "queued": 2})
        self.assertTrue(self.relay.ytvod.prefetcher.wait_idle(10))
        self.assertEqual(sorted(a[0] for a in self.youtube.asked if a[1]), ["ytcast00001", "ytcast00002"])
        self.assertIsNotNone(self.relay.ytvod.resolver.cached("ytcast00001"))
        self.assertIsNotNone(self.relay.ytvod.indexes.get(("ytcast00001", "135")) or
                             self.relay.ytvod.indexes.get(("ytcast00001", "134")), "the index is warmed too")
        # Then a play needs no resolution.
        before = len(self.youtube.asked)
        self.assertEqual(self.watch(dash=True, ytq="360p")["mode"], "dash")
        self.assertEqual(len(self.youtube.asked), before)
        for bad in ({"ids": []}, {"ids": ["x"]}, {"ids": "ytcast00001"}, {"ids": ["ytcast00001"] * 7}):
            self.assertFalse(post(bad)[1]["ok"], bad)
        self.assertEqual(post({"ids": ["ytcast00001"]}, who="guest")[1]["ok"], False)
        self.relay.youtube_kind = "node"
        self.assertEqual(post({"ids": ["ytcast00001"]})[1], {"ok": True, "off": True, "queued": 0})

    def test_play_to_this_pc_passes_dash_and_quality(self):
        status, _, raw = self.request("POST", "/tv/api/play", "carol",
                                      {"target": "pc", "viewer": "c0ffee00", "vod": True, "dash": True, "ytq": "360p",
                                       "item": {"kind": "cast", "url": "https://youtu.be/ytcast00001", "title": "yt"}})
        answer = json.loads(raw)
        self.assertEqual((answer.get("mode"), answer.get("quality", {}).get("current")), ("dash", "360p"), answer)

    def test_a_prefetched_link_starts_without_asking_the_node_again(self):
        self.relay.ytvod.resolver.resolve("ytcast00001")        # what a prefetch leaves behind
        asked, infos = len(self.youtube.asked), []
        self.youtube.video = lambda vid: infos.append(vid)
        status, _, raw = self.request("POST", "/tv/api/play", "carol",
                                      {"target": "pc", "viewer": "c0ffee00", "vod": True, "dash": True,
                                       "item": {"kind": "cast", "url": "https://youtu.be/ytcast00001", "title": "yt"}})
        answer = json.loads(raw)
        self.assertEqual((answer.get("mode"), answer.get("duration")), ("dash", 60), answer)
        self.assertEqual((infos, len(self.youtube.asked)), ([], asked), "no /yt/info, no second resolution")
        entry = self.relay.user_state(self.carol).history_entry("youtube", "ytcast00001")
        self.assertEqual(entry["title"], "yt cast", "the history keeps the title from the resolution")

    def test_prefer_keeps_comparing_like_the_old_string(self):
        prefer = web._prefer({"vod": True, "quality": "1080p", "dash": True, "ytq": "480p"})
        self.assertEqual((prefer, prefer.dash, prefer.yt), ("1080p", True, "480p"))
        prefer = web._prefer({"vod": True, "ytq": "8k"})
        self.assertEqual((prefer, prefer.dash, prefer.yt), ("720p", False, ytvod.DEFAULT_QUALITY))
        self.assertIsNone(web._prefer({"dash": True}))


class CloudTests(unittest.TestCase):
    def test_cloud_relay_has_no_youtube_vod(self):
        relay = web.Relay(None, tv.FakeClient(), None, "/opt/ff", "k" * 32, log=lambda _m: None)
        self.assertIsNone(relay.ytvod)
        with self.assertRaises(web.NotFound):
            relay.yt_prefetch(web.Caller(user=1, role="user"), {"ids": ["ytcast00001"]})


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class PageLogicTests(unittest.TestCase):
    def test_console_dash_logic(self):
        result = subprocess.run(["node", str(ROOT / "tests" / "console_dash_test.mjs")], capture_output=True,
                                text=True, encoding="utf-8", timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
