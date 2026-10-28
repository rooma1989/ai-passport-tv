"""My library: the index cache of cloud files (server/relay/librarycache.py, library.py `_serve_cached`).

Every play and every seek of a cloud file restarts ffmpeg, which reads the file through the
loopback endpoint (/api/library/raw) and has to read the same "index" bytes again first: the
moov of an MP4 (20.9 MB at the very end of the owner's 21 GB film), the header and attached
fonts of an MKV and its Cues near the end. The cache keeps only those regions.

What is checked, over the tiny in-process WebDAV server of test_relay_library.py and a minimal
HTTP front that calls Library.serve_file(raw=True) the way library.raw does: the MP4 layout
reader finds the start of the media data and what follows the mdat (32- and 64-bit sizes,
faststart, fragmented, not enough bytes yet, garbage), the MKV reader finds the first Cluster
behind a big Attachments element in a Segment of unknown size. With a moov at the end, the
first open (0-, the moov, the start of the media data -- the order ffmpeg and ffprobe really
ask in) caches the head up to the media data plus SLACK and the whole moov, and the same open
again reads nothing from the drive; a request that starts in the cache but runs past it gets the
rest from the drive in the same answer (and the cached region grows); a seek into the middle of
the mdat is never cached. An MKV caches the head up to the first Cluster plus SLACK and the tail
from the first request in the last TAIL bytes (the Cues); later tail requests elsewhere are not
cached. A changed file (size or mtime after a rescan) drops the old cache; a file that changed on
the drive before a rescan is noticed by the total in Content-Range, the cache dropped and the
connection closed. The per-file cap and the total cap (least recently used first) hold; files are
mode 600 in a 700 folder; a removed source's cache goes; local folders are never cached; no URL
or password is logged. With a real ffprobe (skipped when it is missing), a moov-at-the-end MP4
probes the same twice and the second time does not fetch the moov from the drive.
"""

from __future__ import annotations

import http.client
import json
import random
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from server.proxyenv import loopback_bypass
from server.relay import library, librarycache

import test_relay_library as lib_base

FFPROBE = shutil.which("ffprobe")
FFMPEG = shutil.which("ffmpeg")
SLACK = librarycache.SLACK
KB = 1024


def noise(n: int, seed: int) -> bytes:
    return random.Random(seed).randbytes(n)


def box(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I4s", 8 + len(payload), kind) + payload


def big_box(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I4sQ", 1, kind, 16 + len(payload)) + payload


def mp4_moov_last(mdat: int, moov: int, seed: int = 1) -> bytes:
    """ftyp, free, a 64-bit mdat, then the moov at the very end (how the owner's 21 GB film is laid out)."""
    return (box(b"ftyp", b"isom\0\0\2\0isomiso2avc1mp41") + box(b"free", b"") + big_box(b"mdat", noise(mdat, seed))
            + box(b"moov", noise(moov, seed + 1)))


def element(ident: int, payload: bytes, unknown: bool = False) -> bytes:
    raw = ident.to_bytes((ident.bit_length() + 7) // 8, "big")
    size = b"\x01\xff\xff\xff\xff\xff\xff\xff" if unknown else b"\x01" + len(payload).to_bytes(7, "big")
    return raw + size + payload


def mkv_episode(fonts: int, clusters: int, cues: int) -> tuple[bytes, int, int]:
    """EBML header, a Segment of unknown size: SeekHead, Attachments (the fonts), Clusters, Cues at the end.
    Returns (bytes, where the first Cluster starts, where the Cues start)."""
    head = element(0x1A45DFA3, element(0x4282, b"matroska"))
    body = element(0x114D9B74, noise(40, 5)) + element(0x1941A469, noise(fonts, 6))
    segment = element(0x18538067, b"", unknown=True)
    first = len(head) + len(segment) + len(body)
    for n in range(4):
        body += element(0x1F43B675, noise(clusters // 4, 7 + n))
    cues_at = len(head) + len(segment) + len(body)
    body += element(0x1C53BB6B, noise(cues, 11))
    return head + segment + body, first, cues_at


MOOV_LAST = mp4_moov_last(3 * 1024 * KB, 300 * KB)
PAYLOAD = 32 + 8 + 16                    # ftyp + free + the 64-bit mdat header
MOOV_AT = PAYLOAD + 3 * 1024 * KB
EPISODE, CLUSTER_AT, CUES_AT = mkv_episode(1024 * KB, 3 * 1024 * KB, 120 * KB)
FILES = {"电影/长片 (2020).mp4": MOOV_LAST, "剧集/剧/剧 S01E01.mkv": EPISODE}


# ---------------------------------------------------------------- reading the layout

class LayoutTests(unittest.TestCase):
    def layout(self, reader, data, have=None, size=None):
        return reader(lambda o, n: data[o:o + n], len(data) if have is None else have, size or len(data))

    def test_mp4(self):
        self.assertEqual(self.layout(librarycache.mp4_layout, MOOV_LAST), (PAYLOAD, MOOV_AT))
        fast = box(b"ftyp", b"isom") + box(b"moov", noise(5000, 2)) + box(b"mdat", noise(9000, 3))
        self.assertEqual(self.layout(librarycache.mp4_layout, fast), (12 + 5008 + 8, None), "mdat to the end")
        self.assertIsNone(self.layout(librarycache.mp4_layout, fast, have=1000), "the mdat header is not here yet")
        zero = box(b"ftyp", b"isom") + struct.pack(">I4s", 0, b"mdat") + noise(100, 1)
        self.assertEqual(self.layout(librarycache.mp4_layout, zero), (20, None), "size 0: to the end of the file")
        frag = box(b"ftyp", b"iso5") + box(b"moov", b"m" * 50) + box(b"moof", b"f" * 20) + box(b"mdat", b"d" * 99)
        self.assertEqual(self.layout(librarycache.mp4_layout, frag), (12 + 58, None), "fragmented: up to the moof")
        self.assertEqual(self.layout(librarycache.mp4_layout, noise(4096, 9)), librarycache.UNKNOWN)
        self.assertEqual(self.layout(librarycache.mp4_layout, box(b"ftyp", b"isom") + struct.pack(">I4s", 3, b"free")
                                     + b"x" * 64), librarycache.UNKNOWN, "a box smaller than its header")

    def test_mkv(self):
        self.assertEqual(self.layout(librarycache.mkv_layout, EPISODE), (CLUSTER_AT, None))
        self.assertGreater(CLUSTER_AT, 1024 * KB, "behind the fonts")
        self.assertIsNone(self.layout(librarycache.mkv_layout, EPISODE, have=CLUSTER_AT - 1))
        self.assertIsNone(self.layout(librarycache.mkv_layout, EPISODE, have=5))
        self.assertEqual(self.layout(librarycache.mkv_layout, noise(4096, 9)), librarycache.UNKNOWN)
        self.assertEqual(self.layout(librarycache.mkv_layout, MOOV_LAST), librarycache.UNKNOWN, "an MP4 is not EBML")


# ---------------------------------------------------------------- through serve_file(raw=True)

class RawFront(BaseHTTPRequestHandler):
    """What library.raw does after the token check: serve_file(handler, id, raw=True)."""
    protocol_version = "HTTP/1.1"
    lib: library.Library | None = None

    def log_message(self, *_args):
        pass

    def _send(self, code, body, kind, headers=None):
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _not_found(self):
        self._send(404, b"nope", "text/plain")

    def do_GET(self):
        self.lib.serve_file(self, self.path.rsplit("/", 1)[-1], raw=True)


class CacheTests(unittest.TestCase):
    def setUp(self):
        # A loaded test machine may take longer than 0.1 s to hang up; the drive must still not be asked.
        patcher = mock.patch.object(library, "CONTINUE_WAIT_S", 1.0)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.dav, self.seen = lib_base.dav_server(FILES)
        self.dav.handle_error = lambda *_args: None       # we hang up early on purpose (like ffmpeg)
        self.logs = []
        self.lib = lib_base.quiet(self.root, self.logs)
        url = f"http://127.0.0.1:{self.dav.server_address[1]}/dav"
        self.url = url
        answer = self.lib.add({"kind": "webdav", "name": "网盘", "url": url, "user": "family",
                               "password": lib_base.PASSWORD})
        self.assertTrue(answer["ok"], answer)
        self.ids = {i["path"]: i["id"] for i in self.lib.items()}
        self.film, self.episode = self.ids["电影/长片 (2020).mp4"], self.ids["剧集/剧/剧 S01E01.mkv"]
        front = type("Front", (RawFront,), {"lib": self.lib})
        self.front = ThreadingHTTPServer(("127.0.0.1", 0), front)
        threading.Thread(target=self.front.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    def tearDown(self):
        for server in (self.front, self.dav):
            server.shutdown()
            server.server_close()
        self.tmp.cleanup()

    def get(self, iid, wanted, read=None):
        """GET bytes=<wanted> the way ffmpeg does; read: stop after this many bytes and hang up."""
        connection = http.client.HTTPConnection("127.0.0.1", self.front.server_address[1], timeout=20)
        connection.request("GET", f"/raw/{iid}", headers={"Range": f"bytes={wanted}"})
        response = connection.getresponse()
        data = response.read(read) if read is not None else response.read()
        connection.close()
        self.settle()
        return response, data

    def settle(self):
        """Wait until the relay finished writing what it read (it notices the hang-up a moment later)."""
        for _ in range(200):
            with self.lib.cache._lock:
                if not any(e.busy() for e in self.lib.cache._entries.values()):
                    return
            time.sleep(0.01)
        self.fail("the cache kept writing")

    def drive(self):
        """The ranges the WebDAV server was asked for since the last call."""
        got = [r for m, _, _, r in self.seen.seen if m == "GET"]
        self.seen.seen.clear()
        return got

    def regions(self, iid):
        with self.lib.cache._lock:
            return sorted((r.start, r.length, r.end) for e in self.lib.cache._entries.values() if e.iid == iid
                          for r in e.regions)

    def open_like_ffmpeg(self, data=MOOV_LAST, iid=None):
        """What ffprobe and ffmpeg ask for a moov-at-the-end MP4 (measured): the head, the moov, the media data."""
        iid = iid or self.film
        response, head = self.get(iid, "0-", read=64 * KB)
        self.assertEqual((response.status, head), (206, data[:64 * KB]))
        response, moov = self.get(iid, f"{MOOV_AT}-")
        self.assertEqual((response.status, moov, response.getheader("Content-Range")),
                         (206, data[MOOV_AT:], f"bytes {MOOV_AT}-{len(data) - 1}/{len(data)}"))
        response, media = self.get(iid, f"{PAYLOAD}-{PAYLOAD + 256 * KB - 1}")
        self.assertEqual((response.status, media), (206, data[PAYLOAD:PAYLOAD + 256 * KB]))

    # ---- MP4 ----

    def test_moov_at_the_end_is_read_from_the_drive_once(self):
        self.open_like_ffmpeg()
        asked = self.drive()
        self.assertIn(f"bytes={MOOV_AT}-{len(MOOV_LAST) - 1}", asked)
        regions = self.regions(self.film)
        self.assertEqual(regions[1], (MOOV_AT, 300 * KB + 8, len(MOOV_LAST)), "the whole moov, nothing else")
        self.assertEqual(regions[0][2], PAYLOAD + SLACK, "the head stops SLACK after the media data starts")
        self.assertGreaterEqual(regions[0][1], PAYLOAD + 256 * KB)
        self.assertLessEqual(self.lib.cache.total(), PAYLOAD + SLACK + 300 * KB + 8, "never the bulk of the mdat")
        # The same open again does not touch the drive.
        self.open_like_ffmpeg()
        self.assertEqual(self.drive(), [])
        # The files are the owner's only.
        folder = self.root / "library-cache"
        self.assertEqual(stat.S_IMODE(folder.stat().st_mode), 0o700)
        for path in folder.iterdir():
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600, path.name)
        # A fresh Library (the relay restarted) finds the cache again.
        again = lib_base.quiet(self.root)
        self.assertEqual(sorted((r.start, r.length) for e in again.cache._entries.values() for r in e.regions),
                         [(s, n) for s, n, _ in regions])
        self.assertNotIn(self.url, "\n".join(self.logs))

    def test_past_the_cache_the_drive_continues_in_the_same_answer(self):
        self.get(self.film, f"0-{128 * KB - 1}")
        self.assertEqual(self.regions(self.film)[0][:2], (0, 128 * KB))
        self.drive()
        response, data = self.get(self.film, f"{64 * KB}-")
        self.assertEqual((response.status, len(data), data == MOOV_LAST[64 * KB:]), (206, len(MOOV_LAST) - 64 * KB, True))
        self.assertEqual(response.getheader("Content-Range"), f"bytes {64 * KB}-{len(MOOV_LAST) - 1}/{len(MOOV_LAST)}")
        self.assertEqual(self.drive(), [f"bytes={128 * KB}-{len(MOOV_LAST) - 1}"], "only from where the cache ends")
        self.assertEqual(self.regions(self.film)[0], (0, PAYLOAD + SLACK, PAYLOAD + SLACK), "grew to its end")
        response, data = self.get(self.film, f"{PAYLOAD + SLACK - 10}-{PAYLOAD + SLACK + 9}")
        self.assertEqual(data, MOOV_LAST[PAYLOAD + SLACK - 10:PAYLOAD + SLACK + 10])

    def test_a_seek_into_the_mdat_is_not_cached(self):
        self.open_like_ffmpeg()
        before = self.regions(self.film)
        self.drive()
        middle = PAYLOAD + 2 * 1024 * KB + 7
        response, data = self.get(self.film, f"{middle}-", read=200 * KB)
        self.assertEqual(data, MOOV_LAST[middle:middle + 200 * KB])
        self.assertEqual(self.regions(self.film), before)
        self.assertEqual(len(self.drive()), 1)

    # ---- MKV ----

    def test_mkv_head_up_to_the_first_cluster_and_the_cues(self):
        size = len(EPISODE)
        response, data = self.get(self.episode, f"0-{CLUSTER_AT + 64 * KB - 1}")
        self.assertEqual(data, EPISODE[:CLUSTER_AT + 64 * KB])
        response, data = self.get(self.episode, f"{CUES_AT}-")
        self.assertEqual(data, EPISODE[CUES_AT:])
        self.assertEqual(self.regions(self.episode), [(0, CLUSTER_AT + 64 * KB, CLUSTER_AT + SLACK),
                                                      (CUES_AT, size - CUES_AT, size)])
        self.drive()
        response, data = self.get(self.episode, f"0-{CLUSTER_AT + 64 * KB - 1}")
        self.assertEqual(data, EPISODE[:CLUSTER_AT + 64 * KB])
        response, data = self.get(self.episode, f"{CUES_AT + 100}-")
        self.assertEqual(data, EPISODE[CUES_AT + 100:])
        self.assertEqual(self.drive(), [], "header, fonts and Cues come from the cache")
        # A cluster near the end (in the last TAIL bytes, before the Cues) is not cached: one tail per file.
        late = CUES_AT - 300 * KB
        self.assertGreater(late, size - librarycache.TAIL)
        response, data = self.get(self.episode, f"{late}-{late + 99}")
        self.assertEqual(data, EPISODE[late:late + 100])
        self.assertEqual(len(self.regions(self.episode)), 2)

    # ---- changes ----

    def test_a_rescanned_change_drops_the_old_cache(self):
        self.open_like_ffmpeg()
        old = sorted(p.name for p in (self.root / "library-cache").iterdir())
        self.assertTrue(old)
        changed = mp4_moov_last(3 * 1024 * KB, 300 * KB, seed=40) + box(b"free", b"z" * 10)
        self.seen.files["电影/长片 (2020).mp4"] = changed
        self.lib.scan_now(self.lib._load_sources()[0]["id"])
        self.assertEqual(self.lib.item(self.film)["size"], len(changed))
        self.drive()
        response, data = self.get(self.film, "0-", read=64 * KB)
        self.assertEqual(data, changed[:64 * KB])
        self.assertTrue(self.drive(), "read from the drive again")
        now = sorted(p.name for p in (self.root / "library-cache").iterdir())
        self.assertFalse(set(old) & set(now), "the old files are gone")
        # Only the modification time changes: dropped as well.
        item = self.lib.item(self.film)
        self.assertIsNone(self.lib.cache.open(self.film, item["size"], item["mtime"] + 60, ".mp4", 0))
        self.assertEqual(self.lib.cache.total(), 0)

    def test_a_change_on_the_drive_before_a_rescan_is_noticed(self):
        self.get(self.film, f"0-{128 * KB - 1}")
        self.seen.files["电影/长片 (2020).mp4"] = MOOV_LAST + b"more"
        connection = http.client.HTTPConnection("127.0.0.1", self.front.server_address[1], timeout=20)
        connection.request("GET", f"/raw/{self.film}", headers={"Range": f"bytes=0-{256 * KB - 1}"})
        response = connection.getresponse()
        with self.assertRaises(http.client.IncompleteRead):
            response.read()
        connection.close()
        self.settle()
        self.assertEqual(self.lib.cache.total(), 0, "the cache of that file is dropped")
        self.assertTrue(any("变了" in line for line in self.logs))
        self.assertNotIn(self.url, "\n".join(self.logs))

    # ---- caps ----

    def test_per_file_and_total_caps(self):
        self.lib.cache = librarycache.IndexCache(self.root / "library-cache", alive=self.lib._now_of,
                                                 file_max=150 * KB, total_max=10 * 1024 * KB)
        response, data = self.get(self.film, f"0-{300 * KB - 1}")
        self.assertEqual(data, MOOV_LAST[:300 * KB], "the answer is whole")
        response, data = self.get(self.film, f"{MOOV_AT}-")
        self.assertEqual(data, MOOV_LAST[MOOV_AT:])
        self.assertEqual(self.regions(self.film), [(0, 150 * KB, PAYLOAD + SLACK)],
                         "a moov bigger than the per-file cap is not cached")

        files = {f"电影/片{n}.mp4": mp4_moov_last(2560 * KB, 200 * KB, seed=n) for n in range(3)}
        self.seen.files.update(files)
        self.lib.scan_now(self.lib._load_sources()[0]["id"])
        clock = iter(range(100, 200))
        self.lib.cache = librarycache.IndexCache(self.root / "library-cache", alive=self.lib._now_of,
                                                 clock=lambda: next(clock), total_max=600 * KB)
        self.lib.cache.drop(self.film)
        paths = {i["path"]: i["id"] for i in self.lib.items()}
        ids = [paths[p] for p in files]
        for iid, data in zip(ids, files.values()):
            moov = PAYLOAD + 2560 * KB
            self.get(iid, f"0-{64 * KB - 1}")
            self.assertEqual(self.get(iid, f"{moov}-")[1], data[moov:])
        self.assertLessEqual(self.lib.cache.total(), 600 * KB)
        self.assertEqual(self.regions(ids[0]), [], "the least recently used went first")
        self.assertEqual(len(self.regions(ids[1])), 2)
        self.assertEqual(len(self.regions(ids[2])), 2)
        # Using the second again makes the third the oldest.
        self.get(ids[1], f"0-{1023}")
        self.get(ids[0], f"0-{64 * KB - 1}")
        self.get(ids[0], f"{PAYLOAD + 2560 * KB}-")
        self.assertEqual(self.regions(ids[2]), [])
        self.assertEqual(len(self.regions(ids[1])), 2)

    def test_removing_the_source_drops_its_cache(self):
        self.open_like_ffmpeg()
        self.assertGreater(self.lib.cache.total(), 0)
        self.lib.remove(self.lib._load_sources()[0]["id"])
        self.assertEqual(self.lib.cache.total(), 0)
        self.assertEqual(list((self.root / "library-cache").iterdir()), [])

    def test_local_folders_are_not_cached(self):
        folder = self.root / "videos"
        lib_base.touch(folder / "本机.mp4", MOOV_LAST)
        self.assertTrue(self.lib.add({"kind": "local", "path": str(folder)})["ok"])
        local = next(i["id"] for i in self.lib.items() if i["path"] == "本机.mp4")
        self.assertEqual(self.get(local, f"{MOOV_AT}-")[1], MOOV_LAST[MOOV_AT:])
        self.assertEqual(self.lib.cache.total(), 0)

    # ---- a real ffprobe ----

    @unittest.skipUnless(FFPROBE and FFMPEG, "needs ffmpeg and ffprobe")
    def test_ffprobe_twice_reads_the_moov_from_the_drive_once(self):
        made = self.root / "real.mp4"
        subprocess.run([FFMPEG, "-v", "error", "-nostdin", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=25",
                        "-t", "150", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "6", "-g", "25", str(made)], check=True)
        data = made.read_bytes()
        layout = librarycache.mp4_layout(lambda o, n: data[o:o + n], len(data), len(data))
        self.assertIsNotNone(layout[1], "ffmpeg writes the moov at the end")
        self.seen.files["电影/真的.mp4"] = data
        self.lib.scan_now(self.lib._load_sources()[0]["id"])
        iid = next(i["id"] for i in self.lib.items() if i["path"] == "电影/真的.mp4")
        url = f"http://127.0.0.1:{self.front.server_address[1]}/raw/{iid}"

        def probe():
            return subprocess.run([FFPROBE, "-v", "error", "-show_entries", "stream=codec_name,nb_frames", "-of", "json",
                                   url], capture_output=True, timeout=60, env=loopback_bypass(__import__("os").environ))

        self.drive()
        first = probe()
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertIn(f"bytes={layout[1]}-", "\n".join(self.drive()))
        second = probe()
        self.assertEqual(json.loads(second.stdout), json.loads(first.stdout))
        self.assertNotIn(f"bytes={layout[1]}-", "\n".join(self.drive()), "the moov came from the cache")


if __name__ == "__main__":
    unittest.main()
