"""Faster live channel switching on the PC (server/relay/watch.py, console.js).

What is checked: live sources are probed briefly (-probesize / -analyzeduration
before -i, only for live channels); each live channel's codec decision is
remembered so a channel that needs transcoding starts transcoding at once (one
ffmpeg, not two), and a stream that fell back to transcoding stays there; the
playlist a live source redirects to is remembered for RESOLVED_S and used next
time, forgotten and retried from the table's address when it no longer opens,
and never remembered when ffmpeg opened several playlists; warm streams (the
channel just switched away from, neighbours the page asks for) are adopted by
the next open of the same channel and user, expire after WARM_S, never exceed
warm_max, are counted in max_streams but stopped before anybody is refused,
are not kept for transcoding channels, and do not exist when warm_max is 0;
a live stream's speed over the last PACE_SPAN_S (stalls included), no new warm
stream while the user's live channel falls behind, and X-RTV-Slow on its playlist;
POST /api/watch/warm and the console's flag; the self-host config's
[pc] warm_channels and --pc-warm; and the player's pure live-start logic under
node (tests/console_live_test.mjs).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import time
import types
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from server.relay import watch

import test_relay_watch as base  # not the TestCase by name: discovery would run it twice
from test_relay_watch import ALICE_DEVICE, BOB_DEVICE, INPUT_DUMP, KEY, LIVE_DATA, FakeProc, Popen

HEVC_DUMP = ["  Stream #0:0: Video: hevc (Main), yuv420p", "  Stream #0:1: Audio: aac (LC), 48000 Hz",
             "Stream mapping:"]
TABLE_URL = "https://x/live.php?id=1"
VARIANT = "https://cdn.example/live/01.m3u8?sid=abc&token=t"


def live(cid: str = "ch000", url: str = TABLE_URL) -> watch.Source:
    return watch.Source(("live", cid, url, "UA"), url=url, user_agent="UA", label=cid, paced=False)


def wait_for(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


class ByInput:
    """popen whose fake ffmpeg depends on its input address (-i): {address: FakeProc kwargs}."""

    def __init__(self, default: dict, **by_url):
        self.default = default
        self.by_url = by_url
        self.procs: list[FakeProc] = []

    def __call__(self, args, **_kwargs):
        given = args[args.index("-i") + 1]
        proc = FakeProc(args, **self.by_url.get(given, self.default))
        self.procs.append(proc)
        return proc

    def inputs(self) -> list[str]:
        return [p.args[p.args.index("-i") + 1] for p in self.procs]


# ---------------------------------------------------------------- pure parts

class LiveArgsTests(unittest.TestCase):
    def test_live_sources_are_probed_briefly(self):
        args = watch.ffmpeg_args("ffmpeg", live(), TABLE_URL, watch.FIRST_MODE)
        at = args.index("-probesize")
        self.assertLess(at, args.index("-i"), "an input option")
        self.assertEqual(args[at + 1], "1000000")
        self.assertEqual(args[args.index("-analyzeduration") + 1], "1000000")
        self.assertNotIn("-re", args)
        self.assertNotIn("nobuffer", " ".join(args), "fragment timing changed with it; not used")

    def test_videos_and_piped_sources_keep_the_default_probe(self):
        video = watch.Source(("video", "up01", "bilibili", "BV1"), url="https://v/1.mp4", offset=10)
        self.assertNotIn("-probesize", watch.ffmpeg_args("ffmpeg", video, "https://v/1.mp4", watch.FIRST_MODE))
        piped = watch.Source(("live", "c"), opener=lambda: None, paced=False)
        self.assertNotIn("-probesize", watch.ffmpeg_args("ffmpeg", piped, "", watch.FIRST_MODE))

    def test_fits(self):
        copy, encode = watch.Mode("copy", "encode"), watch.Mode("encode", "encode")
        self.assertTrue(watch.fits(copy, "h264", "aac"))
        self.assertFalse(watch.fits(copy, "hevc", "aac"))
        self.assertTrue(watch.fits(encode, "hevc", "aac"))
        self.assertTrue(watch.fits(encode, "h264", "aac"), "a remembered or fallback transcode stays a transcode")
        self.assertFalse(watch.fits(copy, "h264", None), "no audio: a different mode")
        self.assertFalse(watch.fits(watch.Mode(None, "encode"), "h264", "aac"), "a picture appeared")
        self.assertTrue(watch.fits(watch.Mode(None, "encode"), None, "mp3"))

    def test_resolved_playlist_from_ffmpeg_lines(self):
        line = f"[hls @ 0x7f8a] Opening '{VARIANT}' for reading"
        self.assertEqual(watch.resolved_playlist(line, TABLE_URL), VARIANT)
        self.assertIsNone(watch.resolved_playlist("[hls @ 0x1] Opening 'https://cdn/1-4731986.ts?x=1' for reading",
                                                  TABLE_URL), "segments are not playlists")
        self.assertIsNone(watch.resolved_playlist(f"[hls @ 0x1] Opening '{TABLE_URL}.m3u8' for reading",
                                                  TABLE_URL + ".m3u8"), "the table's own address")
        self.assertIsNone(watch.resolved_playlist("Input #0, hls, from 'x':", TABLE_URL))

    def test_memory(self):
        now = [0.0]
        memory = watch.LiveMemory(clock=lambda: now[0], resolved_s=600)
        key = live().key
        memory.remember_mode(key, watch.Mode("encode", "encode"))
        memory.remember_url(key, VARIANT)
        self.assertEqual(memory.mode(key), watch.Mode("encode", "encode"))
        now[0] = 599
        self.assertEqual(memory.url(key), VARIANT)
        now[0] = 601
        self.assertIsNone(memory.url(key), "a session address is only trusted for a while")
        memory.remember_url(key, VARIANT)
        memory.forget_url(key)
        self.assertIsNone(memory.url(key))
        for n in range(watch.MODE_CACHE_MAX + 5):
            memory.remember_mode(("live", f"c{n}", "u", "a"), watch.FIRST_MODE)
            memory.remember_url(("live", f"c{n}", "u", "a"), VARIANT)
        self.assertEqual(len(memory._modes), watch.MODE_CACHE_MAX)
        self.assertEqual(len(memory._urls), watch.MODE_CACHE_MAX)


# ---------------------------------------------------------------- remembered codec decision and address

class MemoryServiceTests(unittest.TestCase):
    def setUp(self):
        self.t = 100.0
        self.logs: list[str] = []
        self.service = watch.WatchService("ffmpeg", watch.derive_secret(KEY), log=self.logs.append,
                                          clock=lambda: self.t, popen=Popen(data=LIVE_DATA), background=False)

    def tearDown(self):
        self.service.stop_all()

    def test_a_transcoding_channel_starts_transcoding_the_second_time(self):
        self.service.popen = Popen(lines=HEVC_DUMP, data=LIVE_DATA)
        first = self.service.open(ALICE_DEVICE, live())
        self.assertTrue(first.wait_ready(5))
        self.assertEqual(len(self.service.popen.procs), 2, "first time: copy, then the restart")
        self.service.close(ALICE_DEVICE)
        self.service.popen = Popen(lines=HEVC_DUMP, data=LIVE_DATA)
        second = self.service.open(ALICE_DEVICE, live())
        self.assertTrue(second.wait_ready(5))
        procs = self.service.popen.procs
        self.assertEqual(len(procs), 1, "remembered: one ffmpeg")
        self.assertEqual(procs[0].args[procs[0].args.index("-c:v") + 1], "libx264")
        self.assertEqual(second.mode, watch.Mode("encode", "encode"))

    def test_videos_are_not_remembered(self):
        video = watch.Source(("video", "up01", "bilibili", "BV1"), url="https://v/1.mp4")
        self.service.popen = Popen(lines=HEVC_DUMP, data=LIVE_DATA)
        self.assertTrue(self.service.open(ALICE_DEVICE, video).wait_ready(5))
        self.assertEqual(self.service.mode_of(video), watch.FIRST_MODE)

    def test_a_copy_failure_falls_back_to_transcoding_and_stays(self):
        # Copying fails without a fragment; the retry transcodes the same H.264 and must not flip back to copy.
        calls = []

        def popen(args, **_kwargs):
            calls.append(args[args.index("-c:v") + 1])
            proc = FakeProc(args, data=b"" if len(calls) == 1 else LIVE_DATA, hold=len(calls) > 1, code=1)
            return proc

        self.service.popen = popen
        stream = self.service.open(ALICE_DEVICE, live())
        self.assertTrue(stream.wait_ready(5))
        self.assertEqual(calls, ["copy", "libx264"])
        self.assertEqual(self.service.mode_of(live()).video, "encode")

    def test_the_resolved_playlist_is_remembered_and_used(self):
        dump = [f"[hls @ 0x1] Opening '{VARIANT}' for reading"] + INPUT_DUMP
        self.service.popen = ByInput(dict(lines=dump, data=LIVE_DATA))
        self.assertTrue(self.service.open(ALICE_DEVICE, live()).wait_ready(5))
        self.assertEqual(self.service.memory.url(live().key), VARIANT)
        self.service.close(ALICE_DEVICE)
        self.assertTrue(self.service.open(ALICE_DEVICE, live()).wait_ready(5))
        self.assertEqual(self.service.popen.inputs(), [TABLE_URL, VARIANT])
        self.assertFalse(any(VARIANT in m or "token" in m for m in self.logs), "never logged")
        # Another channel does not get it.
        self.service.close(ALICE_DEVICE)
        self.assertTrue(self.service.open(ALICE_DEVICE, live("ch001", "https://x/2")).wait_ready(5))
        self.assertEqual(self.service.popen.inputs()[-1], "https://x/2")

    def test_several_playlists_are_not_remembered(self):
        dump = [f"[hls @ 0x1] Opening '{VARIANT}' for reading",
                "[hls @ 0x1] Opening 'https://cdn.example/live/audio.m3u8?sid=abc' for reading"] + INPUT_DUMP
        self.service.popen = Popen(lines=dump, data=LIVE_DATA)
        self.assertTrue(self.service.open(ALICE_DEVICE, live()).wait_ready(5))
        self.assertIsNone(self.service.memory.url(live().key), "a separate audio playlist would be lost")

    def test_an_expired_address_is_forgotten_and_the_table_address_used(self):
        self.service.memory.remember_url(live().key, VARIANT)
        refused = ["[http @ 0x1] HTTP error 403 Forbidden", "Conversion failed!"]
        self.service.popen = ByInput(dict(data=LIVE_DATA), **{VARIANT: dict(lines=refused, hold=False, code=1)})
        stream = self.service.open(ALICE_DEVICE, live())
        self.assertTrue(stream.wait_ready(5))
        self.assertEqual(self.service.popen.inputs(), [VARIANT, TABLE_URL])
        self.assertEqual(stream.error, "")
        self.assertIsNone(self.service.memory.url(live().key))
        self.assertTrue(any("记住的地址打不开了" in m for m in self.logs))

    def test_a_failing_table_address_is_not_retried(self):
        refused = ["[http @ 0x1] HTTP error 403 Forbidden", "Conversion failed!"]
        self.service.popen = ByInput(dict(lines=refused, hold=False, code=1))
        stream = self.service.open(ALICE_DEVICE, live())
        self.assertFalse(stream.wait_ready(5))
        stream._thread.join(5)
        self.assertEqual(len(self.service.popen.procs), 1)
        self.assertIn("403", stream.error)


# ---------------------------------------------------------------- warm streams

class WarmTests(unittest.TestCase):
    def setUp(self):
        self.t = 100.0
        self.logs: list[str] = []
        self.popen = Popen(data=LIVE_DATA)
        self.service = watch.WatchService("ffmpeg", watch.derive_secret(KEY), log=self.logs.append,
                                          clock=lambda: self.t, popen=self.popen, background=False, warm_max=2)
        self.slot = "pc5-aaaaaaaa"

    def tearDown(self):
        self.service.stop_all()

    def alive(self) -> int:
        return sum(1 for s in self.service.streams() + self.service.warm_streams() if s.alive)

    def test_off_by_default(self):
        service = watch.WatchService("ffmpeg", b"k", log=lambda _m: None, popen=self.popen, background=False)
        self.assertEqual(service.warm(5, live()), "off")
        first = service.open(self.slot, live(), user=5)
        self.assertTrue(first.wait_ready(5))
        service.open(self.slot, live("ch001", "https://x/2"), user=5, keep_old=True)
        self.assertTrue(first.stopped, "nothing lingers without warm_max")
        self.assertEqual(service.warm_streams(), [])
        service.stop_all()

    def test_a_warmed_channel_is_adopted(self):
        self.assertEqual(self.service.warm(5, live("ch001", "https://x/2")), "started")
        self.assertEqual(self.service.warm(5, live("ch001", "https://x/2")), "warm", "asked again: kept longer")
        warm = self.service.warm_streams()[0]
        self.assertTrue(warm.wait_ready(5))
        stream = self.service.open(self.slot, live("ch001", "https://x/2"), user=5)
        self.assertIs(stream, warm)
        self.assertEqual(stream.device, self.slot, "the token is bound to the page's slot now")
        self.assertIs(self.service.find(stream.sid, self.service.token(stream)), stream)
        self.assertEqual(len(self.popen.procs), 1, "no second ffmpeg")
        self.assertEqual(self.service.warm_streams(), [])
        self.assertTrue(any("接上预热的" in m for m in self.logs))

    def test_another_users_warm_stream_is_not_adopted(self):
        self.service.warm(6, live("ch001", "https://x/2"))
        stream = self.service.open(self.slot, live("ch001", "https://x/2"), user=5)
        self.assertNotIn(stream, self.service.warm_streams())
        self.assertEqual(len(self.popen.procs), 2)

    def test_the_channel_switched_away_from_lingers_and_switching_back_is_instant(self):
        first = self.service.open(self.slot, live(), user=5)
        self.assertTrue(first.wait_ready(5))
        second = self.service.open(self.slot, live("ch001", "https://x/2"), user=5, keep_old=True)
        self.assertFalse(first.stopped)
        self.assertEqual(self.service.warm_streams(), [first])
        self.assertTrue(second.wait_ready(5))
        back = self.service.open(self.slot, live(), user=5, keep_old=True)
        self.assertIs(back, first)
        self.assertEqual(self.service.warm_streams(), [second], "and the other one lingers in turn")
        self.assertEqual(len(self.popen.procs), 2)

    def test_closing_or_a_plain_switch_does_not_linger(self):
        first = self.service.open(self.slot, live(), user=5)
        self.assertTrue(first.wait_ready(5))
        self.service.open(self.slot, live("ch001", "https://x/2"), user=5)
        self.assertTrue(first.stopped, "keep_old not given (following a TV)")
        self.service.close(self.slot)
        self.assertEqual(self.service.warm_streams(), [])

    def test_a_transcoding_channel_does_not_linger_or_warm(self):
        self.service.popen = Popen(lines=HEVC_DUMP, data=LIVE_DATA)
        first = self.service.open(self.slot, live(), user=5)
        self.assertTrue(first.wait_ready(5))
        self.service.open(self.slot, live("ch001", "https://x/2"), user=5, keep_old=True)
        self.assertTrue(first.stopped, "a transcode takes several cores")
        self.assertEqual(self.service.warm(5, live()), "encode", "remembered from the first time")

    def test_a_speculative_warm_stops_when_it_would_transcode(self):
        self.service.popen = Popen(lines=HEVC_DUMP, data=LIVE_DATA)
        self.assertEqual(self.service.warm(5, live("ch009", "https://x/9")), "started")
        warm = self.service.warm_streams()[0]
        warm._thread.join(5)
        self.assertTrue(warm.stopped)
        self.assertEqual(len(self.service.popen.procs), 1, "never started transcoding")
        self.assertEqual(self.service.mode_of(live("ch009", "https://x/9")).video, "encode",
                         "but the real start will transcode at once")
        self.service.reap()
        self.assertEqual(self.service.warm_streams(), [])

    def test_warm_streams_expire(self):
        self.service.warm(5, live("ch001", "https://x/2"))
        warm = self.service.warm_streams()[0]
        self.assertTrue(warm.wait_ready(5))
        self.t += watch.WARM_S - 1
        self.service.reap()
        self.assertFalse(warm.stopped)
        self.t += 2
        self.service.reap()
        self.assertTrue(warm.stopped)
        self.assertTrue(self.popen.procs[0].killed.is_set())
        self.assertEqual(self.service.warm_streams(), [])
        self.assertTrue(any("没人接上" in m for m in self.logs))

    def test_at_most_warm_max(self):
        for n in range(3):
            self.t += 1
            self.assertEqual(self.service.warm(5, live(f"ch00{n}", f"https://x/{n}")), "started")
        keys = {s.source.key[1] for s in self.service.warm_streams()}
        self.assertEqual(keys, {"ch001", "ch002"}, "the oldest made room")
        self.assertTrue(self.popen.procs[0].killed.is_set())

    def test_warm_streams_count_against_the_cap_but_never_refuse_anyone(self):
        self.service.open(self.slot, live(), user=5)
        self.service.warm(5, live("ch001", "https://x/1"))
        self.t += 1
        self.service.warm(5, live("ch002", "https://x/2"))
        self.assertEqual(self.alive(), 3)
        self.assertEqual(self.service.warm(5, live("ch003", "https://x/3")), "started", "replaced the oldest")
        self.assertEqual(self.alive(), 3)
        self.service.open(BOB_DEVICE, live("ch004", "https://x/4"), user=6)
        self.assertEqual(self.alive(), 3, "a warm stream made room")
        self.service.open(ALICE_DEVICE, live("ch005", "https://x/5"), user=7)
        self.assertEqual(self.service.warm_streams(), [])
        self.assertEqual(self.service.warm(5, live("ch006", "https://x/6")), "full")
        with self.assertRaises(watch.Busy):
            self.service.open("pc8-bbbbbbbb", live("ch007", "https://x/7"), user=8)

    def test_only_live_channels_and_not_what_is_playing(self):
        video = watch.Source(("video", "up01", "bilibili", "BV1"), url="https://v/1.mp4")
        self.assertEqual(self.service.warm(5, video), "live")
        self.service.open(self.slot, live(), user=5)
        self.assertEqual(self.service.warm(5, live()), "playing")

    def test_stop_all_stops_warm_streams(self):
        self.service.warm(5, live("ch001", "https://x/1"))
        warm = self.service.warm_streams()[0]
        self.service.stop_all()
        self.assertTrue(warm.stopped)


# ---------------------------------------------------------------- the relay's endpoint

class PaceTests(unittest.TestCase):
    """A slow source (one stream at 0.2-0.7x realtime, measured on CCTV1) is not made slower by warming its
    neighbours, which usually sit on the same source host."""

    def setUp(self):
        self.t = 100.0
        self.popen = Popen(data=LIVE_DATA)
        self.service = watch.WatchService("ffmpeg", watch.derive_secret(KEY), log=lambda _m: None,
                                          clock=lambda: self.t, popen=self.popen, background=False, warm_max=2)
        self.stream = self.service.open("pc5-aaaaaaaa", live(), user=5)
        self.assertTrue(self.stream.wait_ready(5))
        self.stream._pace.clear()

    def tearDown(self):
        self.service.stop_all()

    def produce(self, seconds: float, media_per_second: float) -> None:
        """Segments of 2 media seconds arriving at media_per_second x realtime."""
        produced = 0.0
        while produced < seconds * media_per_second:
            self.t += 2 / media_per_second
            produced += 2
            self.stream.window.total_s += 2
            self.stream._paced()

    def test_speed_needs_a_full_span_and_counts_a_stall(self):
        self.produce(20, 1.0)
        self.assertIsNone(self.stream.speed(), "not enough history yet")
        self.produce(40, 1.0)
        self.assertAlmostEqual(self.stream.speed(), 1.0, delta=0.1)
        self.assertFalse(self.stream.slow())
        self.t += 15  # nothing new for 15 s
        self.assertAlmostEqual(self.stream.speed(), 0.5, delta=0.1)
        self.assertTrue(self.stream.slow())

    def test_a_burst_at_start_does_not_hide_a_slow_source(self):
        self.stream.window.total_s += 12  # ffmpeg reads the playlist's segments at once
        self.stream._paced()
        self.produce(40, 0.5)
        self.assertAlmostEqual(self.stream.speed(), 0.5, delta=0.1)

    def test_no_warming_while_the_watched_channel_is_slow(self):
        self.produce(40, 0.5)
        self.assertEqual(self.service.warm(5, live("ch001", "https://x/2")), "slow")
        self.assertEqual(self.service.warm_streams(), [])
        self.assertEqual(self.service.warm(6, live("ch001", "https://x/2")), "started", "another user is not slowed")
        self.produce(40, 1.0)
        self.assertEqual(self.service.warm(5, live("ch002", "https://x/3")), "started")

    def test_the_playlist_says_slow(self):
        def headers():
            handler = types.SimpleNamespace(sent=None)
            handler._send = lambda code, body, kind, extra=None: setattr(handler, "sent", (code, body, kind, extra))
            watch.get(handler, types.SimpleNamespace(watch=self.service),
                      f"/watch/{self.stream.sid}/{self.service.token(self.stream)}/index.m3u8")
            return handler.sent[3]
        self.produce(40, 1.0)
        self.assertIsNone(headers())
        self.produce(40, 0.5)
        slow = headers()
        self.assertEqual(list(slow), ["X-RTV-Slow"])
        self.assertAlmostEqual(float(slow["X-RTV-Slow"]), 0.5, delta=0.1)


class WarmEndpointTests(unittest.TestCase):
    selfhost = True
    setUp_relay = base.RelayWatchTests.setUp
    tearDown = base.RelayWatchTests.tearDown
    online = base.RelayWatchTests.online
    request = base.RelayWatchTests.request

    def setUp(self):
        self.setUp_relay()
        if self.relay.watch is not None:
            self.relay.watch.warm_max = 2

    def warm(self, who="alice", **body):
        status, _, raw = self.request("POST", "/tv/api/watch/warm", who, body)
        return status, (json.loads(raw) if status == 200 else None)

    def test_warming_neighbours(self):
        status, answer = self.warm(channels=["ch001"])
        self.assertEqual(status, 200)
        self.assertEqual(answer, {"ok": True, "warm": {"ch001": "started"}})
        self.assertEqual(self.warm(channels=["ch001", "up01"])[1]["warm"], {"ch001": "warm", "up01": "live"})
        self.assertEqual(self.warm(channels=["ch404"])[1]["warm"], {"ch404": "live"})
        args = self.relay.watch.warm_streams()[0]
        self.assertEqual(args.source.key[:2], ("live", "ch001"))
        # Watching it now adopts the warm stream.
        status, _, raw = self.request("POST", "/tv/api/watch", "alice", {"channel": "ch001", "viewer": "c0ffee00"})
        answer = json.loads(raw)
        self.assertTrue(answer["ok"], answer)
        self.assertIs(next(s for s in self.relay.watch.streams() if s.sid == answer["sid"]), args)

    def test_bad_requests_and_guests(self):
        self.assertFalse(self.warm(channels="ch001")[1]["ok"])
        self.assertFalse(self.warm(channels=["ch000", "ch001", "ch002"])[1]["ok"])
        self.assertFalse(self.warm(who="guest", channels=["ch001"])[1]["ok"])

    def test_off(self):
        self.relay.watch.warm_max = 0
        self.assertEqual(self.warm(channels=["ch001"])[1], {"ok": True, "off": True, "warm": {}})
        status, _, raw = self.request("GET", "/tv/api/state", "alice")
        self.assertNotIn("watch_warm", json.loads(raw))

    def test_state_says_warming_is_on(self):
        status, _, raw = self.request("GET", "/tv/api/state", "alice")
        self.assertIs(json.loads(raw)["watch_warm"], True)

    def test_switching_channels_on_the_page_keeps_the_old_one_warm(self):
        first = json.loads(self.request("POST", "/tv/api/watch", "alice", {"channel": "ch000", "viewer": "c0ffee00"})[2])
        stream = next(s for s in self.relay.watch.streams() if s.sid == first["sid"])
        self.assertTrue(stream.wait_ready(5))
        self.request("POST", "/tv/api/watch", "alice", {"channel": "ch001", "viewer": "c0ffee00"})
        self.assertEqual(self.relay.watch.warm_streams(), [stream])
        back = json.loads(self.request("POST", "/tv/api/watch", "alice", {"channel": "ch000", "viewer": "c0ffee00"})[2])
        self.assertEqual(back["sid"], first["sid"], "the same stream, instantly")
        self.assertEqual(self.request("GET", "/tv/" + back["url"] + "index.m3u8")[0], 200, "with a token for the page")


class CloudWarmTests(unittest.TestCase):
    selfhost = False
    setUp = base.RelayWatchTests.setUp
    tearDown = base.RelayWatchTests.tearDown
    online = base.RelayWatchTests.online
    request = base.RelayWatchTests.request

    def test_no_warm_endpoint_on_the_cloud_relay(self):
        self.assertIsNone(self.relay.watch)
        status, _, _ = self.request("POST", "/tv/api/watch/warm", "alice", {"channels": ["ch001"]})
        self.assertEqual(status, 404)


# ---------------------------------------------------------------- self-host config

NEEDS_311 = unittest.skipIf(sys.version_info < (3, 11), "tomllib needs Python 3.11")


class ConfigTests(unittest.TestCase):
    @NEEDS_311
    def test_warm_channels_setting(self):
        from server.selfhost import config
        self.assertEqual(config.parse("").pc_warm, "auto", "files written before this setting existed")
        self.assertEqual(config.parse('[pc]\nwarm_channels = "off"\n').pc_warm, "off")
        self.assertEqual(config.parse(config.DEFAULT_TEXT).pc_warm, "auto")
        with self.assertRaises(config.ConfigError):
            config.parse('[pc]\nwarm_channels = "yes"\n')

    def test_how_many_warm_streams(self):
        from server.selfhost.__main__ import PC_WARM_MEMORY, pc_warm_streams
        from server.selfhost.config import Config
        self.assertEqual(pc_warm_streams(Config(pc_warm="off"), True, memory=64 << 30), 0)
        self.assertEqual(pc_warm_streams(Config(pc_warm="on"), False, memory=1 << 30), watch.WARM_MAX)
        self.assertEqual(pc_warm_streams(Config(), False, memory=PC_WARM_MEMORY), watch.WARM_MAX)
        self.assertEqual(pc_warm_streams(Config(), True, memory=PC_WARM_MEMORY - 1), 0, "never on a small host")
        with unittest.mock.patch("server.selfhost.__main__.physical_memory", return_value=None):
            self.assertEqual(pc_warm_streams(Config(), True), watch.WARM_MAX, "a PC client, memory unknown")
            self.assertEqual(pc_warm_streams(Config(), False), 0)

    @NEEDS_311
    def test_the_relay_gets_pc_warm(self):
        from server.selfhost import bootstrap, config
        from server.selfhost.__main__ import build_children
        with tempfile.TemporaryDirectory() as tmp:
            layout = bootstrap.Layout(Path(tmp))
            on = build_children(layout, config.Config(pc_warm="on"), "/f", "", "", log=lambda _m: None)[0].argv
            off = build_children(layout, config.Config(pc_warm="off"), "/f", "", "", log=lambda _m: None)[0].argv
        self.assertEqual(on[on.index("--pc-warm") + 1], str(watch.WARM_MAX))
        self.assertNotIn("--pc-warm", off)


# ---------------------------------------------------------------- the player's live-start logic

@unittest.skipUnless(shutil.which("node"), "node is not installed")
class PlayerLogicTests(unittest.TestCase):
    def test_console_live_logic(self):
        result = subprocess.run(["node", str(ROOT / "tests" / "console_live_test.mjs")], capture_output=True,
                                text=True, encoding="utf-8", timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
