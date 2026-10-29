"""My library: warming new cloud files up in the background (server/relay/library.py `warm_step`).

The first play of a new cloud file used to fetch its index (the moov at the end of the owner's
21 GB MP4, an MKV's header, fonts and Cues) and probe it through the drive; only the second play
was fast. After a scan finds new or changed WebDAV items, a low-priority background run now reads
those regions through the index cache's own tee (librarycache.py) and probes each file once, so the
first play is like the second.

What is checked, over the tiny in-process WebDAV server of test_relay_library.py and the raw front
of test_relay_library_cache.py: the MKV SeekHead reader finds the Cues (and nothing for Cues in
front, no SeekHead, garbage); a run warms the new items newest first (the MP4's head up to the
media data plus SLACK and its whole moov, the MKV's head and its Cues from the SeekHead position,
the probe cached in the index), after which an ffmpeg-like open reads nothing from the drive and a
second run does nothing; nothing runs while someone watches, while a library file is being served
or was served within WARM_QUIET_S, or while a scan runs, and a run stops as soon as playback starts
and the next one carries on where it stopped; a run never reads more than WARM_BUDGET and never
the bulk of the media data; local folders are skipped; a drive error stops the run with a backoff,
and a file that failed WARM_TRIES times is left alone; the warm-up does not evict what was used in
the last WARM_KEEP_S; one log line per run, without the drive's address. With a real ffprobe
(skipped when it is missing) the warm-up's own probe goes through the loopback raw endpoint
without counting as someone playing, and the next probe of the file does not fetch the moov.
"""

from __future__ import annotations

import http.client
import os
import shutil
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
from server.relay import library, librarycache

import test_relay_library as lib_base
import test_relay_library_cache as cache_base

FFPROBE = shutil.which("ffprobe")
FFMPEG = shutil.which("ffmpeg")
KB = 1024
SLACK = librarycache.SLACK
MOOV_LAST, PAYLOAD, MOOV_AT = cache_base.MOOV_LAST, cache_base.PAYLOAD, cache_base.MOOV_AT
element, noise = cache_base.element, cache_base.noise
MEDIA = {"dur": 10.0, "start": 0.0, "fmt": "mov,mp4", "streams": [{"i": 0, "t": "video", "c": "h264"}]}


def mkv_with_seekhead(fonts: int, clusters: int, cues: int) -> tuple[bytes, int, int]:
    """EBML header, a Segment of unknown size whose SeekHead points at the Cues at the end (how mkvmerge
    writes it), the fonts, Clusters, Cues. Returns (bytes, where the first Cluster starts, where the Cues start)."""
    head = element(0x1A45DFA3, element(0x4282, b"matroska"))
    segment = element(0x18538067, b"", unknown=True)
    attachments = element(0x1941A469, noise(fonts, 6))
    body = b"".join(element(0x1F43B675, noise(clusters // 4, 7 + n)) for n in range(4))

    def seekhead(position: int) -> bytes:
        seek = element(0x53AB, b"\x1c\x53\xbb\x6b") + element(0x53AC, position.to_bytes(8, "big"))
        return element(0x114D9B74, element(0x4DBB, seek))

    in_segment = len(seekhead(0)) + len(attachments) + len(body)     # the Cues, relative to the Segment's data
    start = len(head) + len(segment)
    data = head + segment + seekhead(in_segment) + attachments + body + element(0x1C53BB6B, noise(cues, 11))
    return data, start + len(seekhead(0)) + len(attachments), start + in_segment


EPISODE, CLUSTER_AT, CUES_AT = mkv_with_seekhead(1024 * KB, 3 * 1024 * KB, 120 * KB)
FILES = {"电影/长片 (2020).mp4": MOOV_LAST, "剧集/剧/剧 S01E01.mkv": EPISODE}


class SeekHeadTests(unittest.TestCase):
    def cues(self, data, have=None):
        return librarycache.mkv_cues(lambda o, n: data[o:o + n], len(data) if have is None else have, len(data))

    def test_cues_from_the_seekhead(self):
        self.assertEqual(self.cues(EPISODE), CUES_AT)
        self.assertEqual(EPISODE[CUES_AT:CUES_AT + 4], b"\x1c\x53\xbb\x6b")
        self.assertIsNone(self.cues(EPISODE, have=40), "the SeekHead is not here yet")
        # The file of test_relay_library_cache.py has a SeekHead of noise: no Cues known.
        self.assertIsNone(self.cues(cache_base.EPISODE))
        self.assertIsNone(self.cues(noise(4096, 3)))
        self.assertIsNone(self.cues(MOOV_LAST), "an MP4 is not EBML")
        front = (element(0x1A45DFA3, b"") + element(0x18538067, b"", unknown=True) + element(0x1C53BB6B, b"c" * 9)
                 + element(0x1F43B675, b"v" * 99))
        self.assertIsNone(self.cues(front), "Cues in front are in the head already")


class WarmTests(unittest.TestCase):
    get = cache_base.CacheTests.get
    settle = cache_base.CacheTests.settle
    drive = cache_base.CacheTests.drive
    regions = cache_base.CacheTests.regions

    def setUp(self):
        patcher = mock.patch.object(library, "CONTINUE_WAIT_S", 1.0)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.dav, self.seen = lib_base.dav_server(FILES)
        self.dav.handle_error = lambda *_args: None
        self.logs = []
        self.lib = lib_base.quiet(self.root, self.logs)
        self.url = f"http://127.0.0.1:{self.dav.server_address[1]}/dav"
        answer = self.lib.add({"kind": "webdav", "name": "网盘", "url": self.url, "user": "family",
                               "password": lib_base.PASSWORD})
        self.assertTrue(answer["ok"], answer)
        self.ids = {i["path"]: i["id"] for i in self.lib.items()}
        self.film, self.episode = self.ids["电影/长片 (2020).mp4"], self.ids["剧集/剧/剧 S01E01.mkv"]
        front = type("Front", (cache_base.RawFront,), {"lib": self.lib})
        self.front = ThreadingHTTPServer(("127.0.0.1", 0), front)
        threading.Thread(target=self.front.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.probed = []
        self.fake_probe()
        self.drive()

    def tearDown(self):
        for server in (self.front, self.dav):
            server.shutdown()
            server.server_close()
        self.tmp.cleanup()

    def fake_probe(self, media=MEDIA):
        """No ffprobe here: the probe answers MEDIA (or None) and records what it was asked."""
        self.lib.ffmpeg, self.lib.secret, self.lib.port = "ffmpeg", b"k" * 32, lambda: 1

        def probe(iid, warm=False):
            self.probed.append((iid, warm))
            return dict(media) if media is not None else None

        self.lib.probe = probe

    def assert_warm_film(self):
        self.assertEqual(self.regions(self.film), [(0, PAYLOAD + SLACK, PAYLOAD + SLACK),
                                                   (MOOV_AT, len(MOOV_LAST) - MOOV_AT, len(MOOV_LAST))])

    # ---- warming ----

    def test_new_items_are_warmed_newest_first(self):
        with library._LOCK:
            self.lib._items[self.episode]["added"] += 10          # the episode came in later
        result = self.lib.warm_step()
        self.assertEqual((result["files"], result["stopped"]), (2, ""), result)
        self.assertEqual([i for i, _ in self.probed], [self.episode, self.film], "newest first")
        self.assertTrue(all(warm for _, warm in self.probed), "probed as the warm-up")
        # MP4: the head up to the media data plus SLACK and the whole moov, never the mdat.
        self.assert_warm_film()
        # MKV: the head up to the first Cluster plus SLACK and the Cues from where the SeekHead says.
        self.assertEqual(self.regions(self.episode), [(0, CLUSTER_AT + SLACK, CLUSTER_AT + SLACK),
                                                      (CUES_AT, len(EPISODE) - CUES_AT, len(EPISODE))])
        self.assertEqual(result["bytes"], self.lib.cache.total(), "read only what was kept")
        self.assertTrue(list((self.root / "library-cache").glob("*.bin")))
        # The probe is in the index: the first play does not probe again.
        self.assertEqual(self.lib.media_of(self.film)["streams"], MEDIA["streams"])
        self.assertEqual(len(self.probed), 2)
        # What ffmpeg asks on the first play now comes from the cache.
        self.drive()
        self.get(self.film, "0-", read=64 * KB)
        self.get(self.film, f"{MOOV_AT}-")
        self.get(self.film, f"{PAYLOAD}-{PAYLOAD + 256 * KB - 1}")
        self.get(self.episode, f"0-{CLUSTER_AT + 64 * KB - 1}")
        self.get(self.episode, f"{CUES_AT}-")
        self.assertEqual(self.drive(), [])
        # One line, no address; the next run has nothing to do and says nothing.
        lines = [line for line in self.logs if "预热" in line]
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("2 个", lines[0])
        self.assertNotIn(self.url, "\n".join(self.logs))
        self.assertNotIn("127.0.0.1", "\n".join(self.logs))
        library_quiet = mock.patch.object(library, "WARM_QUIET_S", 0.0)
        with library_quiet:
            self.assertEqual(self.lib.warm_step(), {"files": 0, "bytes": 0, "stopped": ""})
        self.assertEqual(len([line for line in self.logs if "预热" in line]), 1)

    def test_a_rescan_queues_new_and_changed_items_only(self):
        self.lib.warm_step()
        self.assertEqual(self.lib.warm_due(), [])
        sid = self.lib._load_sources()[0]["id"]
        self.seen.files["电影/新片 (2026).mp4"] = cache_base.mp4_moov_last(1024 * KB, 64 * KB, seed=3)
        self.seen.files["电影/长片 (2020).mp4"] = MOOV_LAST + b"x" * 10
        self.lib.scan_now(sid)
        ids = {i["path"]: i["id"] for i in self.lib.items()}
        self.assertEqual({i["id"] for i in self.lib.warm_due()}, {ids["电影/新片 (2026).mp4"], self.film})
        self.assertTrue(self.lib._warm_wake.is_set(), "the background run is woken")

    # ---- never in the way ----

    def test_nothing_while_someone_watches_or_a_file_is_served(self):
        self.lib.watching = lambda: True
        result = self.lib.warm_step()
        self.assertEqual((result["files"], result["bytes"], result["stopped"]), (0, 0, "有人在看"))
        self.lib.watching = lambda: False
        # A library file being read right now (ffmpeg playing it).
        connection = http.client.HTTPConnection("127.0.0.1", self.front.server_address[1], timeout=20)
        connection.request("GET", f"/raw/{self.film}", headers={"Range": "bytes=0-"})
        response = connection.getresponse()
        response.read(16 * KB)
        self.assertEqual(self.lib.warm_step()["stopped"], "有人在放片库里的片子")
        connection.close()
        self.settle()
        # Served a moment ago: still left alone for WARM_QUIET_S.
        self.assertEqual(self.lib.warm_step()["stopped"], "有人在放片库里的片子")
        # While a scan runs.
        with library._LOCK:
            self.lib._status["running"] = True
        with mock.patch.object(library, "WARM_QUIET_S", 0.0):
            self.assertEqual(self.lib.warm_step()["stopped"], "在扫描")
            with library._LOCK:
                self.lib._status["running"] = False
            self.drive()
            cached = self.lib.cache.total()
            self.assertEqual(self.lib.warm_step()["files"], 2)
        self.assertGreater(self.lib.cache.total(), cached)
        self.assertEqual(self.probed and self.probed[0][1], True)

    def test_stops_when_playback_starts_and_carries_on_later(self):
        asked = []

        def watching():
            asked.append(1)
            return len(asked) > 3                      # someone presses play during the first file

        self.lib.watching = watching
        with mock.patch.object(library, "WARM_QUIET_S", 0.0):
            result = self.lib.warm_step()
            self.assertEqual((result["files"], result["stopped"]), (0, "有人在看"), result)
            self.assertLessEqual(result["bytes"], 3 * library.CHUNK)
            self.assertEqual(self.probed, [])
            self.lib.watching = lambda: False
            result = self.lib.warm_step()
        self.assertEqual((result["files"], result["stopped"]), (2, ""), result)
        self.assert_warm_film()
        self.assertEqual(self.lib.cache.total(), sum(n for _, n, _ in self.regions(self.film))
                         + sum(n for _, n, _ in self.regions(self.episode)))

    def test_budget_per_run(self):
        budget = 600 * KB
        with mock.patch.object(library, "WARM_BUDGET", budget), mock.patch.object(library, "WARM_QUIET_S", 0.0):
            runs = []
            for _ in range(20):
                result = self.lib.warm_step()
                runs.append(result)
                if not result["stopped"]:
                    break
        self.assertEqual(runs[-1]["stopped"], "", runs)
        self.assertGreater(len(runs), 3)
        for result in runs:
            self.assertLessEqual(result["bytes"], budget + library.CHUNK, result)
        self.assertEqual(sum(r["files"] for r in runs), 2)
        self.assertEqual(sum(r["bytes"] for r in runs), self.lib.cache.total(), "nothing read twice")
        self.assert_warm_film()

    def test_local_folders_are_skipped(self):
        folder = self.root / "videos"
        lib_base.touch(folder / "本机.mp4", MOOV_LAST)
        self.assertTrue(self.lib.add({"kind": "local", "path": str(folder)})["ok"])
        local = next(i["id"] for i in self.lib.items() if i["path"] == "本机.mp4")
        with library._LOCK:
            self.lib._warm_wanted.add(local)               # even when asked to
        self.assertNotIn(local, {i["id"] for i in self.lib.warm_due()})
        with mock.patch.object(library, "WARM_QUIET_S", 0.0):
            self.assertEqual(self.lib.warm_step()["files"], 2)
        self.assertEqual(self.regions(local), [])
        self.assertNotIn(local, [i for i, _ in self.probed])

    def test_a_restart_warms_recent_unprobed_items(self):
        again = lib_base.quiet(self.root)
        self.assertEqual({i["id"] for i in again.warm_due()}, {self.film, self.episode})
        with library._LOCK:
            again._items[self.film]["added"] -= library.WARM_NEW_S + 60
        again._warm_seeded = False
        again._warm_wanted.clear()
        self.assertEqual([i["id"] for i in again.warm_due()], [self.episode], "only recent ones")

    # ---- errors and caps ----

    def test_a_drive_error_stops_the_run_and_backs_off(self):
        del self.seen.files["电影/长片 (2020).mp4"]
        del self.seen.files["剧集/剧/剧 S01E01.mkv"]
        with mock.patch.object(library, "WARM_QUIET_S", 0.0):
            result = self.lib.warm_step()
            self.assertEqual(result["files"], 0)
            self.assertIn("网盘上没有", result["stopped"])
            self.assertEqual(len(self.drive()), 1, "stopped at the first error")
            self.assertEqual(self.lib.warm_step()["stopped"], "网盘刚出过错，歇一会儿")
            self.assertEqual(self.drive(), [])
            first = self.lib._warm_backoff
            self.lib._warm_after = 0
            self.lib.warm_step()
            self.assertEqual(self.lib._warm_backoff, 2 * first, "longer each time")
            # Both files failed WARM_TRIES times in all? The newest failed twice: it is left alone now.
            self.lib._warm_after = 0
            self.lib.warm_step()
            self.lib._warm_after = 0
            self.lib.warm_step()
            self.assertEqual(self.lib.warm_due(), [])
        self.assertTrue(any("停下了" in line for line in self.logs))
        self.assertNotIn(self.url, "\n".join(self.logs))
        self.assertEqual(self.lib.cache.total(), 0)

    def test_a_failed_warm_probe_does_not_block_the_real_one(self):
        self.fake_probe(None)
        with mock.patch.object(library, "WARM_QUIET_S", 0.0):
            result = self.lib.warm_step()
        self.assertEqual(result["stopped"], "探测不了")
        self.fake_probe(MEDIA)
        self.assertIsNotNone(self.lib.media_of(self.film), "the play probes again at once")

    def test_never_evicts_what_was_used_recently(self):
        self.lib.cache = librarycache.IndexCache(self.root / "library-cache", alive=self.lib._now_of,
                                                 total_max=2 * librarycache.FILE_MAX)
        self.get(self.film, "0-", read=64 * KB)
        self.get(self.film, f"{MOOV_AT}-")
        kept = self.regions(self.film)
        self.lib.cache.total_max = self.lib.cache.total() + librarycache.FILE_MAX - 1
        with mock.patch.object(library, "WARM_QUIET_S", 0.0):
            result = self.lib.warm_step()
        self.assertEqual((result["files"], result["stopped"]), (0, "索引缓存快满了"))
        self.assertEqual(self.regions(self.film), kept)
        self.assertEqual(self.regions(self.episode), [])
        # Room again (or what is there is old): it goes on.
        self.lib.cache.total_max = 2 * librarycache.FILE_MAX
        with mock.patch.object(library, "WARM_QUIET_S", 0.0):
            self.assertEqual(self.lib.warm_step()["stopped"], "")
        self.assertEqual(self.regions(self.film)[1], kept[1], "the moov it had is kept as it was")


# ---------------------------------------------------------------- a real ffprobe through the raw endpoint

class RawRoute(cache_base.RawFront):
    """What the relay does with /tv/api/library/raw/<id>?t=...: library.raw (token, loopback, the w=1 flag)."""
    relay = None

    def _query(self):
        return {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).items()}

    def do_GET(self):
        path = urllib.parse.urlsplit(self.path).path
        if not library.raw(self, self.relay, path.removeprefix("/tv")):
            self._not_found()


@unittest.skipUnless(FFPROBE and FFMPEG, "needs ffmpeg and ffprobe")
class RealProbeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        made = self.root / "real.mp4"
        subprocess.run([FFMPEG, "-v", "error", "-nostdin", "-f", "lavfi", "-i", "testsrc=size=160x120:rate=25",
                        "-t", "20", "-c:v", "libx264", "-preset", "ultrafast", "-g", "25", str(made)], check=True)
        self.data = made.read_bytes()
        self.moov = librarycache.mp4_layout(lambda o, n: self.data[o:o + n], len(self.data), len(self.data))[1]
        self.assertIsNotNone(self.moov, "ffmpeg writes the moov at the end")
        self.dav, self.seen = lib_base.dav_server({"电影/真的.mp4": self.data, "电影/也是.mp4": self.data + b"\0" * 16})
        self.dav.handle_error = lambda *_args: None
        self.logs = []
        self.lib = lib_base.quiet(self.root, self.logs)
        self.assertTrue(self.lib.add({"kind": "webdav", "name": "网盘", "url": f"http://127.0.0.1:{self.dav.server_address[1]}/dav",
                                      "user": "family", "password": lib_base.PASSWORD})["ok"])
        relay = type("Relay", (), {"library": self.lib})()
        self.front = ThreadingHTTPServer(("127.0.0.1", 0), type("Route", (RawRoute,), {"relay": relay}))
        threading.Thread(target=self.front.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.lib.wire(FFMPEG, "k" * 32, lambda: self.front.server_address[1])

    def tearDown(self):
        for server in (self.front, self.dav):
            server.shutdown()
            server.server_close()
        self.tmp.cleanup()

    def test_warm_probe_through_the_raw_endpoint(self):
        with mock.patch.object(library, "WARM_QUIET_S", 30.0):
            result = self.lib.warm_step()
        self.assertEqual((result["files"], result["stopped"]), (2, ""), (result, self.logs))
        self.assertEqual(self.lib._serving, 0)
        self.assertEqual(self.lib._served_at, 0.0, "its own probe is not someone playing")
        for item in self.lib.items():
            self.assertEqual(item["media"]["streams"][0]["c"], "h264")
        self.seen.seen.clear()
        iid = next(i["id"] for i in self.lib.items() if i["path"] == "电影/真的.mp4")
        url = self.lib.raw_url(iid)
        done = subprocess.run([FFPROBE, "-v", "error", "-show_entries", "stream=codec_name", "-of", "json", url],
                              capture_output=True, timeout=60, env=loopback_bypass(os.environ))
        self.assertEqual(done.returncode, 0, done.stderr)
        asked = [r for m, _, _, r in self.seen.seen if m == "GET"]
        self.assertFalse(any(r.startswith(f"bytes={self.moov}-") for r in asked), asked)


if __name__ == "__main__":
    unittest.main()
