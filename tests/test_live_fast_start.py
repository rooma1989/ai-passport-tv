"""Faster live start on the TV, and one variant of a multi-bitrate source (server/hlsopen.py, server/live.py).

What is checked: the start code is shared, not copied (watch.py's LiveMemory,
resolve_playlist and LIVE_PROBE are hlsopen's; live.py's FPS probe uses the
same LIVE_PROBE); master playlists are parsed (RESOLUTION, BANDWIDTH, quoted
CODECS, AUDIO groups) and one variant is picked -- for the TV the smallest one
that is not upscaled onto the panel (a 4:3 variant covers a 16:9 panel by
height), the largest when all are smaller, none when the master gives no
sizes; for the PC the highest bitrate under PC_MAX_BPS and PC_MAX_HEIGHT, the
lowest when all are over -- never an audio-only variant unless it is the only
one, and never when the picked variant's audio is a separate playlist;
resolve_playlist with the TV's size returns the picked variant joined to the
address after redirects. The TV's decoder command probes network sources
briefly, keeps -re, and adds -readrate_initial_burst only when asked;
LIVE_BURST_S is capped at half the host queues; burst_supported asks ffmpeg
once per path. open_urls resolves once per line (also remembering "nothing to
resolve"), passes the panel size, and never resolves loopback (the relay's own
streams) or non-HTTP addresses; the FPS probe opens the resolved address but
caches under the table's. start() falls back to the table's address when the
remembered one no longer opens and forgets it (real ffmpeg, local fixture; no
network anywhere).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from server import frames, hlsopen, live  # noqa: E402
from server.relay import watch  # noqa: E402

from test_relay_prepare import opener  # noqa: E402

MASTER = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=6000000,RESOLUTION=1920x1080,CODECS="avc1.640028,mp4a.40.2"
hd/index.m3u8?sid=1
#EXT-X-STREAM-INF:BANDWIDTH=12000000,RESOLUTION=3840x2160,CODECS="avc1.640033,mp4a.40.2"
uhd/index.m3u8?sid=1
#EXT-X-STREAM-INF:BANDWIDTH=900000,RESOLUTION=640x360,CODECS="avc1.4d401e,mp4a.40.2"
sd/index.m3u8?sid=1
#EXT-X-STREAM-INF:BANDWIDTH=400000,RESOLUTION=320x180,CODECS="avc1.42c01e,mp4a.40.2"
ld/index.m3u8?sid=1
#EXT-X-STREAM-INF:BANDWIDTH=64000,CODECS="mp4a.40.2"
audio/index.m3u8?sid=1
"""


def variant(width=0, height=0, bandwidth=0, video=True, uri="x.m3u8", audio=""):
    return {"uri": uri, "width": width, "height": height, "bandwidth": bandwidth, "video": video, "audio": audio}


class SharedCodeTests(unittest.TestCase):
    def test_one_copy_of_the_start_code(self):
        self.assertIs(watch.LiveMemory, hlsopen.LiveMemory)
        self.assertIs(watch.resolve_playlist, hlsopen.resolve_playlist)
        self.assertIs(watch.resolved_playlist, hlsopen.resolved_playlist)
        self.assertIs(watch.LIVE_PROBE, hlsopen.LIVE_PROBE)
        self.assertIs(live.FPS_PROBE_ARGS, hlsopen.LIVE_PROBE)
        self.assertIsInstance(live.TV_MEMORY, hlsopen.LiveMemory)


class VariantTests(unittest.TestCase):
    def test_parsing(self):
        found = hlsopen.variants(MASTER)
        self.assertEqual([v["uri"] for v in found][:2], ["hd/index.m3u8?sid=1", "uhd/index.m3u8?sid=1"])
        self.assertEqual((found[0]["width"], found[0]["height"], found[0]["bandwidth"]), (1920, 1080, 6_000_000))
        self.assertTrue(found[0]["video"], "the comma inside the quoted CODECS is not an attribute separator")
        self.assertFalse(found[-1]["video"], "CODECS with only audio")
        self.assertEqual(found[-1]["width"], 0)

    def test_the_tv_takes_the_smallest_that_is_not_upscaled(self):
        chosen = hlsopen.pick_variant(hlsopen.variants(MASTER), at_least=(320, 180))
        self.assertEqual(chosen["uri"], "ld/index.m3u8?sid=1")
        chosen = hlsopen.pick_variant(hlsopen.variants(MASTER), at_least=(400, 300))
        self.assertEqual(chosen["uri"], "sd/index.m3u8?sid=1")

    def test_a_4_3_variant_covers_a_16_9_panel_by_height(self):
        choices = [variant(240, 180, 300_000, uri="a"), variant(640, 480, 900_000, uri="b")]
        self.assertEqual(hlsopen.pick_variant(choices, at_least=(320, 180))["uri"], "a")

    def test_the_tv_takes_the_largest_when_all_are_smaller(self):
        choices = [variant(160, 90, 100_000, uri="a"), variant(256, 144, 200_000, uri="b")]
        self.assertEqual(hlsopen.pick_variant(choices, at_least=(320, 180))["uri"], "b")

    def test_the_tv_does_not_guess_without_sizes(self):
        choices = [variant(bandwidth=2_000_000, uri="a"), variant(bandwidth=800_000, uri="b")]
        self.assertIsNone(hlsopen.pick_variant(choices, at_least=(320, 180)))

    def test_the_pc_takes_the_highest_under_the_caps(self):
        self.assertEqual(hlsopen.pick_variant(hlsopen.variants(MASTER))["uri"], "hd/index.m3u8?sid=1")
        choices = [variant(1280, 720, 9_000_000, uri="a"), variant(1920, 1080, 20_000_000, uri="b")]
        self.assertEqual(hlsopen.pick_variant(choices)["uri"], "a", "all over the cap: the lowest")
        choices = [variant(bandwidth=2_000_000, uri="a"), variant(bandwidth=800_000, uri="b")]
        self.assertEqual(hlsopen.pick_variant(choices)["uri"], "a", "no sizes: by bitrate")

    def test_audio_only_only_when_it_is_all_there_is(self):
        self.assertEqual(hlsopen.pick_variant([variant(bandwidth=64_000, video=False, uri="r")])["uri"], "r")
        choices = [variant(bandwidth=64_000, video=False, uri="r"), variant(bandwidth=5_000, uri="v")]
        self.assertEqual(hlsopen.pick_variant(choices, at_least=(320, 180))["uri"], "v")
        self.assertIsNone(hlsopen.pick_variant([]))

    def test_resolve_for_the_tv(self):
        found = hlsopen.resolve_playlist("https://x/live.php?id=1", "UA", at_least=(320, 180),
                                         urlopen=opener("https://cdn.example/live/master.m3u8", MASTER))
        self.assertEqual(found, "https://cdn.example/live/ld/index.m3u8?sid=1")

    def test_separate_audio_of_the_picked_variant_keeps_the_master(self):
        text = ('#EXTM3U\n#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aac",URI="a.m3u8"\n'
                '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="muxed",NAME="main"\n'
                '#EXT-X-STREAM-INF:BANDWIDTH=900000,RESOLUTION=640x360,AUDIO="aac"\nsd.m3u8\n'
                '#EXT-X-STREAM-INF:BANDWIDTH=400000,RESOLUTION=320x180,AUDIO="muxed"\nld.m3u8\n')
        given = "https://x/live.php?id=1"
        self.assertIsNone(hlsopen.resolve_playlist(given, urlopen=opener("https://c/m.m3u8", text)))
        self.assertEqual(hlsopen.resolve_playlist(given, at_least=(320, 180), urlopen=opener("https://c/m.m3u8", text)),
                         "https://c/ld.m3u8", "the TV's variant has its sound muxed in")
        subtitles = ('#EXTM3U\n#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="s",URI="s.m3u8"\n'
                     '#EXT-X-STREAM-INF:BANDWIDTH=900000,RESOLUTION=640x360\nsd.m3u8\n'
                     '#EXT-X-STREAM-INF:BANDWIDTH=400000,RESOLUTION=320x180\nld.m3u8\n')
        self.assertEqual(hlsopen.resolve_playlist(given, urlopen=opener("https://c/m.m3u8", subtitles)),
                         "https://c/sd.m3u8", "subtitles are never read")


class DecoderCommandTests(unittest.TestCase):
    URL = "http://example.invalid/x.m3u8"

    def test_network_sources_are_probed_briefly_and_still_paced(self):
        command = live.source_command(self.URL, 1, 2, "ffmpeg")
        at = command.index("-probesize")
        self.assertLess(at, command.index("-i"))
        self.assertEqual(command[at + 1], "1000000")
        self.assertEqual(command[command.index("-analyzeduration") + 1], "1000000")
        self.assertLess(command.index("-re"), command.index("-i"))
        self.assertNotIn("-readrate_initial_burst", command, "only when asked")

    def test_local_files_keep_the_default_probe(self):
        self.assertNotIn("-probesize", live.source_command("/tmp/x.ts", 1, 2, "ffmpeg"))

    def test_the_burst_is_an_input_option(self):
        command = live.source_command(self.URL, 1, 2, "ffmpeg", burst=4.0)
        at = command.index("-readrate_initial_burst")
        self.assertEqual(command[at + 1], "4")
        self.assertLess(at, command.index("-i"))
        self.assertIn("-re", command, "real time after the burst")

    def test_the_burst_never_fills_the_host_queues(self):
        self.assertGreaterEqual(live.LIVE_BURST_S, 0)
        self.assertLessEqual(live.LIVE_BURST_S, live.PCM_QUEUE_CHUNKS * live.AUDIO_CHUNK_MS / 1000 / 2)
        self.assertLessEqual(live.LIVE_BURST_S, live.VIDEO_QUEUE_SECONDS / 2)

    def test_burst_support_is_asked_once_per_ffmpeg(self):
        answers = {"/new/ffmpeg": b"-readrate_initial_burst <seconds>  ...", "/old/ffmpeg": b"-readrate <speed>"}
        calls = []

        def run(command, **kw):
            calls.append(command[0])
            return types.SimpleNamespace(stdout=answers[command[0]], stderr=b"")
        with patch.dict(live._BURST_SUPPORT, clear=True), patch.object(live.subprocess, "run", run):
            self.assertTrue(live.burst_supported("/new/ffmpeg"))
            self.assertFalse(live.burst_supported("/old/ffmpeg"))
            self.assertTrue(live.burst_supported("/new/ffmpeg"))
            self.assertEqual(calls, ["/new/ffmpeg", "/old/ffmpeg"])

        def missing(command, **kw):
            raise FileNotFoundError(command[0])
        with patch.dict(live._BURST_SUPPORT, clear=True), patch.object(live.subprocess, "run", missing):
            self.assertFalse(live.burst_supported("/none/ffmpeg"))


class OpenUrlTests(unittest.TestCase):
    URL = "https://x/live.php?id=1"
    FOUND = "https://cdn.example/live/ld/index.m3u8?sid=1"

    def setUp(self):
        self.asked = []
        self.answer = self.FOUND
        memory = hlsopen.LiveMemory()
        patches = [patch.object(live, "TV_MEMORY", memory),
                   patch.object(live, "RESOLVER", lambda url, ua, timeout, at_least:
                                self.asked.append((url, ua, at_least)) or self.answer)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_resolved_once_with_the_panel_size(self):
        self.assertEqual(live.open_urls(self.URL, "UA"), [self.FOUND, self.URL])
        self.assertEqual(live.open_urls(self.URL, "UA"), [self.FOUND, self.URL])
        self.assertEqual(self.asked, [(self.URL, "UA", (frames.WIDTH, frames.HEIGHT))])

    def test_nothing_to_resolve_is_remembered_too(self):
        self.answer = None
        self.assertEqual(live.open_urls(self.URL), [self.URL])
        self.assertEqual(live.open_urls(self.URL), [self.URL])
        self.assertEqual(len(self.asked), 1)

    def test_loopback_and_other_schemes_are_not_resolved(self):
        for url in ("http://127.0.0.1:8099/stream/up01", "http://localhost:8099/s", "http://[::1]:8099/s",
                    "rtmp://x/live", "/tmp/x.ts"):
            with self.subTest(url=url):
                self.assertEqual(live.open_urls(url), [url])
        self.assertEqual(self.asked, [])

    def test_the_fps_probe_opens_the_resolved_address_but_caches_the_tables(self):
        opened = []
        with patch.dict(live._FPS_CACHE, clear=True), patch.dict(live._FPS_MISSES, clear=True), \
                patch.object(live, "_probe_fps", lambda url, *a: opened.append(url) or 25):
            self.assertEqual(live.probe_source_fps(self.URL, "ffmpeg", target=self.FOUND), 25)
            self.assertEqual(live.probe_source_fps(self.URL, "ffmpeg", target="https://other/session"), 25)
            self.assertEqual(opened, [self.FOUND])
            self.assertIn(self.URL, live._FPS_CACHE)


@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg not available")
class FallbackTests(unittest.TestCase):
    def test_a_remembered_address_that_no_longer_opens_falls_back_and_is_forgotten(self):
        ffmpeg = shutil.which("ffmpeg")
        with tempfile.TemporaryDirectory(prefix="fast-start-") as tmp:
            src = str(Path(tmp) / "source.mkv")
            subprocess.run([ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                            "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=12:duration=2",
                            "-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=16000:duration=2",
                            "-c:v", "ffv1", "-threads", "1", "-c:a", "pcm_s16le", src],
                           check=True, timeout=15)
            gone = str(Path(tmp) / "expired.m3u8")
            memory = hlsopen.LiveMemory()
            memory.remember_url(("tv", src, ""), gone)
            with patch.dict(live.CHANNELS, {"fixture": src}), patch.object(live, "TV_MEMORY", memory), \
                    patch.object(live, "open_urls", lambda url, ua="": [gone, url]), \
                    patch.dict(os.environ, {"TV_FPS": "12"}):
                channel = live.LiveChannel(src, ffmpeg)
                channel.build_palette()
                try:
                    channel.start()
                    self.assertEqual(channel.open_url, src)
                    self.assertIsNone(memory.url(("tv", src, "")), "the expired address is forgotten")
                    deadline = time.monotonic() + 10
                    while time.monotonic() < deadline and not (channel.audio and channel.video):
                        time.sleep(0.05)
                    self.assertTrue(channel.audio and channel.video, "the table's address plays")
                finally:
                    channel.close()


if __name__ == "__main__":
    unittest.main()
