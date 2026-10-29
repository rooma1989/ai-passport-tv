"""My library: the whole-file cache of cloud files (server/relay/libraryfull.py, library.py `_serve_full`).

Every play and every seek of a cloud file restarts ffmpeg, which has to read several MB from the
drive before the first picture; the drive's speed varies a lot, so a seek takes 2-16 s. While an
item plays, one background downloader fetches the whole file to local disk so that later seeks
into the downloaded part are served locally.

What is checked. Against a fake drive (an opener over bytes in memory, no sleeps): the downloader
fetches in CHUNK range requests from playback's position to the end, then wraps from the start up
to where it began, and logs one line when the file is whole; it yields at once while playback
reads from the drive itself (mid-chunk too, keeping what it wrote) and continues from playback's
latest position afterwards; it pauses after IDLE_S without readers and resumes when played again;
playing another item switches it (the old partial data stays); a changed size or mtime, or a drive
answer whose total disagrees, drops the old data; the budget evicts the least recently used other
files and never the current one; the free-space floor stops it; limit 0 deletes everything; files
are mode 600 in a 700 folder and a fresh cache finds them again. Through the raw endpoint over the
tiny WebDAV server of test_relay_library.py: a request starting in downloaded data is answered from
the local file and continues from the drive in the same answer (that part written in too); with the
downloader running, playing from the start leaves the whole file local and a second play does not
touch the drive; a file changed on the drive drops the data; the warm-up's probe and direct browser
plays never start the downloader; the view gives each item's cached fraction and the setting; the
admin endpoint changes the setting (admin only) and off deletes the data. No URL or password is logged.
"""

from __future__ import annotations

import http.client
import os
import random
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from server.proxyenv import loopback_bypass
from server.relay import library, librarycache, libraryfull

import test_relay_library as lib_base
import test_relay_library_cache as cache_base
import test_relay_watch as watch_base

KB = 1024
GB = libraryfull.GB
DATA = random.Random(3).randbytes(5000)
ITEM = {"id": "0123456789abcdef", "size": len(DATA), "mtime": 7, "path": "剧/剧 S01E01.mkv"}


class Upstream:
    def __init__(self, data: bytes, at: int, hook=None):
        self.data, self.at, self.pos, self.hook, self.closed = data, at, 0, hook, False

    def read(self, n: int) -> bytes:
        if self.hook is not None:
            self.hook(self.at + self.pos)
        chunk = self.data[self.pos:self.pos + n]
        self.pos += len(chunk)
        return chunk

    def close(self):
        self.closed = True


class Drive:
    """The opener: answers [start, last] of the item's bytes and remembers what it was asked."""

    def __init__(self, files: dict[str, bytes]):
        self.files, self.asked, self.hook, self.fail = files, [], None, None

    def __call__(self, item, start, last):
        self.asked.append((item["id"][:2], start, last))
        if self.fail is not None:
            raise self.fail
        return Upstream(self.files[item["id"]][start:last + 1], start, self.hook)


def item(iid: str, data: bytes, mtime: int = 7) -> dict:
    return {"id": iid, "size": len(data), "mtime": mtime, "path": "x"}


# ---------------------------------------------------------------- the downloader, against a fake drive

class DownloaderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "library-full"
        self.logs = []
        self.now = [1000.0]
        self.free = [100 * GB]
        self.drive = Drive({ITEM["id"]: DATA})
        self.cache = self.make()

    def tearDown(self):
        self.tmp.cleanup()

    def make(self, limit=30 * GB, alive=None):
        cache = libraryfull.FullCache(self.root, opener=self.drive, alive=alive, log=self.logs.append,
                                      clock=lambda: self.now[0], limit=limit, disk_free=lambda: self.free[0],
                                      background=False)
        cache.chunk, cache.piece = 1000, 250
        return cache

    def asked(self):
        got = [(a, b) for _, a, b in self.drive.asked]
        self.drive.asked.clear()
        return got

    def runs(self, cache=None, of=ITEM):
        cache = cache or self.cache
        with cache._lock:
            found = cache._files.get(cache.key_of_item(of))
            return [list(r) for r in found.runs] if found is not None else []

    def drain(self):
        while self.cache.step():
            pass

    def test_runs(self):
        runs = []
        for a, b in ((10, 20), (30, 40), (20, 30), (0, 5), (50, 60), (45, 52)):
            libraryfull.add_run(runs, a, b)
        self.assertEqual(runs, [[0, 5], [10, 40], [45, 60]])
        self.assertEqual(libraryfull.run_end(runs, 12), 40)
        self.assertEqual(libraryfull.run_end(runs, 7), 7)
        self.assertEqual(libraryfull.gap_after(runs, 12, 100), (40, 45))
        self.assertEqual(libraryfull.next_gap([[0, 5], [10, 100]], 50, 100), (5, 10), "wraps to the start")
        self.assertIsNone(libraryfull.next_gap([[0, 100]], 50, 100))

    def test_from_playback_to_the_end_then_wraps(self):
        self.cache.touch(ITEM, 2500, "剧 第 1 集")
        self.drain()
        self.assertEqual(self.asked(), [(2500, 3499), (3500, 4499), (4500, 4999), (0, 999), (1000, 1999),
                                        (2000, 2499)])
        self.assertEqual(self.runs(), [[0, len(DATA)]])
        key = self.cache.key_of_item(ITEM)
        self.assertEqual((self.root / f"{key}.bin").read_bytes(), DATA)
        self.assertEqual(self.cache.fractions(), {key: 1.0})
        self.assertEqual([line for line in self.logs if "整集缓存好了" in line], ["片库：「剧 第 1 集」整集缓存好了"])
        self.assertFalse(self.cache.step(), "complete: stops")
        self.assertEqual(self.asked(), [])
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o700)
        for path in self.root.iterdir():
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600, path.name)
        again = self.make()
        self.assertEqual(again.fractions(), {key: 1.0}, "a restarted relay finds it again")

    def test_yields_to_playback_and_continues_from_its_position(self):
        self.cache.touch(ITEM, 0)
        self.assertTrue(self.cache.step())
        self.assertEqual(self.asked(), [(0, 999)])
        with self.cache.on_drive():
            self.assertFalse(self.cache.step(), "playback reads from the drive: the downloader waits")
            # Playback jumped to 3000 and read a while from the drive itself; what it read is written in.
            tee = self.cache.tee(ITEM, 3000)
            tee.write(DATA[3000:3200])
            tee.write(DATA[3200:3500])
            tee.close()
        self.assertEqual(self.asked(), [])
        self.assertEqual(self.runs(), [[0, 1000], [3000, 3500]])
        self.assertTrue(self.cache.step())
        self.assertEqual(self.asked(), [(3500, 4499)], "from the run that holds playback's position")
        # Playback starts reading from the drive in the middle of a chunk: the downloader stops, keeps what it wrote.
        held = []

        def hook(pos):
            if pos == 4750 and not held:
                held.append(self.cache.on_drive())
                held[0].__enter__()
        self.drive.hook = hook
        self.assertTrue(self.cache.step())
        self.assertEqual(self.asked(), [(4500, 4999)])
        self.assertEqual(self.runs(), [[0, 1000], [3000, 5000]], "the piece read before it noticed is kept")
        self.drive.hook = None
        held[0].__exit__(None, None, None)
        # Playback jumps to 1500 (not downloaded): the downloader starts there, then fills what is left before it.
        self.cache.seen(ITEM, 1500)
        self.drain()
        self.assertEqual(self.asked(), [(1500, 2499), (2500, 2999), (1000, 1499)])
        self.assertEqual(self.runs(), [[0, len(DATA)]])

    def test_pauses_when_nobody_reads_and_resumes_when_played(self):
        self.cache.touch(ITEM, 0)
        self.assertTrue(self.cache.step())
        self.now[0] += libraryfull.IDLE_S - 1
        self.assertTrue(self.cache.step())
        self.now[0] += libraryfull.IDLE_S + 1
        self.assertFalse(self.cache.step(), "ten minutes without playback")
        with self.cache.reading(ITEM):
            self.now[0] += 2 * libraryfull.IDLE_S
            self.assertTrue(self.cache.step(), "a request that stays open counts as playing")
        self.now[0] += libraryfull.IDLE_S + 1
        self.assertFalse(self.cache.step())
        self.cache.touch(ITEM, 3000)
        self.assertTrue(self.cache.step())
        self.assertEqual(self.asked(), [(0, 999), (1000, 1999), (2000, 2999), (3000, 3999)])

    def test_another_item_switches_and_the_old_part_stays(self):
        other = item("fedcba9876543210", DATA[::-1])
        self.drive.files[other["id"]] = DATA[::-1]
        self.cache.touch(ITEM, 0, "甲")
        self.cache.step()
        self.cache.touch(other, 0, "乙")
        self.drain()
        self.assertEqual(self.runs(), [[0, 1000]])
        self.assertEqual(self.runs(of=other), [[0, len(DATA)]])
        self.assertEqual(set(self.cache.fractions().values()), {0.2, 1.0})

    def test_changed_file_drops_the_old_data(self):
        self.cache.touch(ITEM, 0)
        self.cache.step()
        old = self.cache.key_of_item(ITEM)
        self.assertTrue((self.root / f"{old}.bin").exists())
        changed = dict(ITEM, mtime=8)
        self.cache.touch(changed, 0)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), [], "the old size / mtime is gone")
        self.cache.step()
        self.assertEqual(self.runs(of=changed), [[0, 1000]])
        # A rescan that no longer has it (alive says None) drops it too.
        self.assertEqual(self.make(alive=lambda _iid: None).fractions(), {})
        self.assertEqual(list(self.root.iterdir()), [])
        # The drive's total disagrees (changed before a rescan): dropped, not downloaded again.
        self.cache = self.make()
        self.cache.touch(ITEM, 0, "剧")
        self.cache.step()
        self.drive.fail = libraryfull.Changed()
        self.assertFalse(self.cache.step())
        self.assertEqual(self.cache.fractions(), {})
        self.assertIn("片库：「剧」在网盘上变了，整集缓存不要了（重新扫描一下片库）", self.logs)
        self.drive.fail = None
        self.assertFalse(self.cache.step())

    def test_drive_errors_back_off(self):
        self.cache.touch(ITEM, 0, "剧")
        self.drive.fail = OSError("timed out")
        self.assertFalse(self.cache.step())
        self.drive.fail = None
        self.assertFalse(self.cache.step(), "resting after an error")
        self.assertFalse(self.cache.can_fill(ITEM))
        with self.cache._lock:
            self.cache._job.after = 0
        self.assertTrue(self.cache.step())
        self.assertEqual(len([line for line in self.logs if "读不了" in line]), 1)

    def test_budget_evicts_least_recently_used_never_the_current(self):
        a, b, c = (item(f"{n}" * 16, bytes([n]) * 1000) for n in (1, 2, 3))
        for one in (a, b, c):
            self.drive.files[one["id"]] = bytes([int(one["id"][0])]) * 1000
        self.cache = self.make(limit=2500)
        for n, one in enumerate((a, b), 1):
            self.now[0] += 1
            self.cache.touch(one, 0, f"片{n}")
            self.drain()
        self.now[0] += 1
        self.cache.touch(c, 0, "片3")
        self.drain()
        self.assertEqual(self.runs(of=a), [], "the least recently used went first")
        self.assertEqual(self.runs(of=b), [[0, 1000]])
        self.assertEqual(self.runs(of=c), [[0, 1000]])
        self.assertIn("片库：整集缓存满了，删掉了「片1」的", self.logs)
        self.assertFalse((self.root / f"{self.cache.key_of_item(a)}.bin").exists())
        # A file bigger than what is left: the others go, the current one never does.
        big = item("4" * 16, b"\4" * 2000)
        self.drive.files[big["id"]] = b"\4" * 2000
        self.cache.limit = 1500
        self.now[0] += 1
        with self.cache.reading(c):
            self.cache.touch(big, 0, "大片")
            self.drain()
        self.assertEqual(self.runs(of=c), [[0, 1000]], "being read: kept")
        self.assertEqual(self.runs(of=b), [])
        self.assertEqual(self.runs(of=big), [], "no room even for one chunk while the other is read")
        self.cache.touch(big, 0, "大片")
        self.drain()
        self.assertEqual(self.runs(of=big), [[0, 1000]])
        self.assertEqual(self.runs(of=c), [])
        self.assertIn("片库：「大片」没缓存完，整集缓存满了", self.logs)
        self.assertLessEqual(self.cache.total(), 1500)

    def test_free_space_floor(self):
        self.free[0] = libraryfull.FREE_MIN + 500
        self.cache.touch(ITEM, 0, "剧")
        self.assertFalse(self.cache.step())
        self.assertEqual(self.asked(), [])
        self.assertIn("片库：「剧」没缓存完，磁盘快满了", self.logs)
        self.assertIsNone(self.cache.tee(ITEM, 0), "playback does not write in either")
        self.free[0] = 100 * GB
        self.cache._disk = (float("-inf"), 0)
        self.cache.touch(ITEM, 0, "剧")
        self.assertTrue(self.cache.step(), "played again with room: goes on")

    def test_off_deletes_everything(self):
        self.cache.touch(ITEM, 0)
        self.cache.step()
        self.assertTrue(any(self.root.iterdir()))
        self.cache.set_limit(0)
        self.assertEqual(list(self.root.iterdir()), [])
        self.cache.touch(ITEM, 0)
        self.assertFalse(self.cache.step())
        self.assertEqual(self.asked(), [(0, 999)])
        self.assertIsNone(self.cache.tee(ITEM, 0))

    def test_waiting_reader_gets_what_the_thread_writes(self):
        self.cache.background = True
        gate = threading.Event()
        self.drive.hook = lambda pos: gate.wait(5)
        self.cache.touch(ITEM, 2000)
        try:
            self.assertEqual(self.cache.next_at(ITEM), 2000)
            self.assertFalse(self.cache.wait(ITEM, 2000, 0.05), "nothing written yet")
            gate.set()
            self.assertTrue(self.cache.wait(ITEM, 2000, 5))
            self.assertIsNotNone(self.cache.local(ITEM, 2000))
        finally:
            self.cache.stop()
        self.assertFalse(self.cache._thread.is_alive())


# ---------------------------------------------------------------- through the raw endpoint

MKV = random.Random(5).randbytes(3 * 1024 * KB)
FILES = {"剧集/剧/剧 S01E01.mkv": MKV}


class Front(cache_base.RawFront):
    raw, warm = True, False

    def do_GET(self):
        self.lib.serve_file(self, self.path.rsplit("/", 1)[-1], raw=self.raw, warm=self.warm)


class ServeBase(unittest.TestCase):
    def setUp(self):
        # Keep the index cache out of the middle of this small file (its tail region would be the last 8 MB).
        for name, value in (("TAIL", 16 * KB), ("HEAD_OTHER", 16 * KB)):
            patcher = mock.patch.object(librarycache, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.dav, self.seen = lib_base.dav_server(FILES)
        self.dav.handle_error = lambda *_args: None
        self.logs = []
        self.lib = lib_base.quiet(self.root, self.logs)
        self.url = f"http://127.0.0.1:{self.dav.server_address[1]}/dav"
        self.assertTrue(self.lib.add({"kind": "webdav", "name": "网盘", "url": self.url, "user": "family",
                                      "password": lib_base.PASSWORD})["ok"])
        self.item = self.lib.items()[0]
        self.iid = self.item["id"]
        self.lib.full.chunk, self.lib.full.burst, self.lib.full.piece = 256 * KB, 512 * KB, 64 * KB
        self.fronts = []

    def tearDown(self):
        self.lib.close()
        for server in self.fronts + [self.dav]:
            server.shutdown()
            server.server_close()
        self.tmp.cleanup()

    def front(self, **kind):
        server = ThreadingHTTPServer(("127.0.0.1", 0), type("F", (Front,), {"lib": self.lib, **kind}))
        threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.fronts.append(server)
        return server.server_address[1]

    def get(self, wanted, port=None):
        connection = http.client.HTTPConnection("127.0.0.1", port or self.front(), timeout=20)
        connection.request("GET", f"/raw/{self.iid}", headers={"Range": f"bytes={wanted}"})
        response = connection.getresponse()
        try:
            data = response.read()
        except http.client.IncompleteRead as cut:       # the relay closed the connection early
            data = cut.partial
        connection.close()
        return response, data

    def drive(self):
        got = [r for m, _, _, r in self.seen.seen if m == "GET"]
        self.seen.seen.clear()
        return got

    def runs(self):
        with self.lib.full._lock:
            found = self.lib.full._files.get(self.lib.full.key_of_item(self.item))
            return [list(r) for r in found.runs] if found is not None else []

    def prefill(self, start, end):
        self.lib.full.touch(self.item, start)
        tee = self.lib.full.tee(self.item, start)
        tee.write(MKV[start:end])
        tee.close()


class ServeTests(ServeBase):
    def test_local_then_the_drive_in_the_same_answer(self):
        self.prefill(1024 * KB, 2048 * KB)
        self.drive()
        response, data = self.get(f"{1536 * KB}-{2560 * KB - 1}")
        self.assertEqual((response.status, response.getheader("Content-Range"), data == MKV[1536 * KB:2560 * KB]),
                         (206, f"bytes {1536 * KB}-{2560 * KB - 1}/{len(MKV)}", True))
        self.assertEqual(self.drive(), [f"bytes={2048 * KB}-{2560 * KB - 1}"], "only from where the local data ends")
        self.assertEqual(self.runs(), [[1024 * KB, 2560 * KB]], "what came from the drive is written in too")
        response, data = self.get(f"{1024 * KB}-{2560 * KB - 1}")
        self.assertEqual(data, MKV[1024 * KB:2560 * KB])
        self.assertEqual(self.drive(), [], "all local")
        self.assertEqual(self.lib.bytes_read(self.iid), 2560 * KB, "the start-up progress counts both")
        self.assertNotIn(self.url, "\n".join(self.logs))

    def test_with_the_downloader_the_whole_file_ends_up_local(self):
        self.lib.full.background = True
        response, data = self.get("0-")
        self.assertEqual((response.status, len(data), data == MKV), (206, len(MKV), True))
        for _ in range(500):
            if self.lib.full.fractions().get(self.lib.full.key_of_item(self.item)) == 1.0:
                break
            time.sleep(0.01)
        self.assertEqual(self.runs(), [[0, len(MKV)]])
        self.assertIn("片库：「剧 第 1 集」整集缓存好了", self.logs)
        asked = self.drive()
        self.assertTrue(asked)
        response, data = self.get(f"{len(MKV) // 2}-")
        self.assertEqual(data, MKV[len(MKV) // 2:])
        self.assertEqual(self.drive(), [], "a seek after that never touches the drive")
        key = self.lib.full.key_of_item(self.item)
        self.assertEqual((self.root / "library-full" / f"{key}.bin").read_bytes(), MKV)

    def test_a_file_changed_on_the_drive_drops_the_data(self):
        self.prefill(0, 512 * KB)
        self.seen.files["剧集/剧/剧 S01E01.mkv"] = MKV + b"more"
        response, data = self.get(f"{256 * KB}-{1024 * KB - 1}")
        self.assertEqual(data, MKV[256 * KB:512 * KB], "the local part, then the connection closes")
        self.assertEqual(self.runs(), [])
        self.assertEqual(self.lib.full.fractions(), {})
        self.assertTrue(any("在网盘上变了" in line for line in self.logs))

    def test_warm_up_probe_and_direct_plays_do_not_start_it(self):
        for kind in ({"warm": True}, {"raw": False}):
            response, data = self.get(f"{100 * KB}-{200 * KB - 1}", self.front(**kind))
            self.assertEqual(data, MKV[100 * KB:200 * KB])
            self.assertIsNone(self.lib.full._job, kind)
            self.assertEqual(self.lib.full.fractions(), {})

    def test_view_and_setting(self):
        self.assertEqual(self.lib.full_gb(), libraryfull.DEFAULT_GB)
        self.prefill(0, 768 * KB)
        view = self.lib.view()
        self.assertEqual((view["full_gb"], view["full_used"]), (30, 768 * KB))
        card = view["shows"][0]["episodes"][0]
        self.assertEqual(card["cached"], 0.25)
        self.assertEqual(self.lib.set_full_gb(31)["ok"], False)
        self.assertEqual(self.lib.set_full_gb(True)["ok"], False)
        self.assertTrue(self.lib.set_full_gb(10)["ok"])
        self.lib.scan_now(self.lib._load_sources()[0]["id"])           # saving the sources keeps the setting
        self.assertEqual(lib_base.quiet(self.root).full_gb(), 10)
        self.assertEqual(self.lib.full.total(), 768 * KB)
        self.assertTrue(self.lib.set_full_gb(0)["ok"])
        self.assertEqual(list((self.root / "library-full").iterdir()), [])
        self.assertNotIn("cached", self.lib.view()["shows"][0]["episodes"][0])
        # Off: the raw endpoint is the index cache's path again, nothing is written here.
        self.drive()
        response, data = self.get(f"{100 * KB}-{200 * KB - 1}")
        self.assertEqual(data, MKV[100 * KB:200 * KB])
        self.assertEqual(self.drive(), [f"bytes={100 * KB}-{200 * KB - 1}"])
        self.assertEqual(list((self.root / "library-full").iterdir()), [])
        self.assertIn("片库的整集缓存关了，已经缓存的删掉了", self.logs)


class RealFfmpegTests(ServeBase):
    """A real ffmpeg reading through the raw endpoint with the downloader running (skipped without ffmpeg)."""

    def test_ffmpeg_reads_while_it_downloads_and_then_never_asks_the_drive(self):
        if not (cache_base.FFMPEG and cache_base.FFPROBE):
            self.skipTest("needs ffmpeg and ffprobe")
        made = self.root / "real.mkv"
        subprocess.run([cache_base.FFMPEG, "-v", "error", "-nostdin", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=25",
                        "-t", "20", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "10", "-g", "50", str(made)],
                       check=True)
        data = made.read_bytes()
        self.seen.files["剧集/剧/剧 S01E01.mkv"] = data
        self.lib.scan_now(self.lib._load_sources()[0]["id"])
        self.item = self.lib.items()[0]
        self.assertEqual(self.item["size"], len(data))
        self.lib.full.background = True
        url = f"http://127.0.0.1:{self.front()}/raw/{self.iid}"

        def copy(*before):
            return subprocess.run([cache_base.FFMPEG, "-v", "error", "-nostdin", *before, "-i", url, "-c", "copy",
                                   "-f", "null", "-"], capture_output=True, timeout=60,
                                  env=loopback_bypass(os.environ))

        first = copy()
        self.assertEqual(first.returncode, 0, first.stderr)
        for _ in range(500):
            if self.lib.full.fractions().get(self.lib.full.key_of_item(self.item)) == 1.0:
                break
            time.sleep(0.01)
        self.assertEqual(self.runs(), [[0, len(data)]])
        self.drive()
        seek = copy("-ss", "12")
        self.assertEqual(seek.returncode, 0, seek.stderr)
        self.assertEqual(self.drive(), [], "a seek is served from the local file")
        key = self.lib.full.key_of_item(self.item)
        self.assertEqual((self.root / "library-full" / f"{key}.bin").read_bytes(), data)


class EndpointTests(unittest.TestCase):
    selfhost = True
    setUp_relay = watch_base.RelayWatchTests.setUp
    tearDown_relay = watch_base.RelayWatchTests.tearDown
    online = watch_base.RelayWatchTests.online
    request = lib_base.ApiTests.request
    call = lib_base.ApiTests.call

    def setUp(self):
        self.setUp_relay()
        self.cookies["admin"] = "tvs=" + self.accounts.new_session(self.admin, "pc")
        self.relay.library = lib_base.quiet(Path(self.accounts.path).parent)

    def tearDown(self):
        self.tearDown_relay()

    def test_admin_changes_the_setting(self):
        path = "/tv/api/admin/library/settings"
        for who in ("alice", "guest", None):
            self.assertEqual(self.call(who, "POST", path, {"full_gb": 10})[0], 404)
        self.assertEqual(self.call("admin", "POST", path, {"full_gb": 7})[0], 400)
        status, answer = self.call("admin", "POST", path, {"full_gb": 100})
        self.assertEqual((status, answer["ok"], answer["full_gb"]), (200, True, 100))
        self.assertEqual(self.call("alice", "GET", "/tv/api/library")[1]["full_gb"], 100)
        self.assertEqual(self.call("admin", "GET", "/tv/api/admin/library")[1]["full_gb"], 100)
        self.assertTrue(self.call("admin", "POST", path, {"full_gb": 0})[1]["ok"])
        self.assertEqual(self.relay.library.full.limit, 0)
        actions = [r["action"] for r in self.accounts.audit_rows(100) if r["action"] == "library.settings"]
        self.assertEqual(len(actions), 2)


if __name__ == "__main__":
    unittest.main()
