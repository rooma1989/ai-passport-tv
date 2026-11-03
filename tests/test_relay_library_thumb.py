"""My library: a picture per episode (server/relay/librarythumb.py, library.py `_thumb_of`, the raw endpoint's
thumbnail mode).

Every episode row on a show's detail page used to show the same drawn placeholder (or the show folder's poster).
Now each episode has its own picture: a Kodi-style image next to the file (<stem>-thumb.jpg, <stem>.jpg) when
there is one, otherwise one frame grabbed from the episode by a background job while nobody plays or watches.

What is checked. Side images: the episode's own image wins and is served lazily; a folder poster stays the show's
art and is never used for an episode; an episode with neither has no art (the page draws its placeholder) and is
queued for a grab. The queue: a show whose detail page opens goes first; an admin refresh drops the current picture
and starts at a later position; nothing runs while a library file plays or someone watches, and real playback
stops a running grab; a failure keeps the placeholder and is retried only after a backoff. The raw endpoint in
thumbnail mode reads the drive in small pieces, keeps the index regions but never writes the whole-file cache,
does not count as playback and stops when told. With a real ffmpeg (skipped without ffmpeg and ffprobe): a grab
skips a black opening and lands on a picture, stored as library-thumb/<id>-<size>-<mtime>.jpg (600, folder 700),
shown on the episode card and served; a changed file drops it; a broken file leaves the placeholder; a grab over
the WebDAV drive works and leaves the whole-file cache alone. The admin endpoint and the picture endpoint are for
the right people only. No URL or path is logged.
"""

from __future__ import annotations

import os
import stat
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
from server.relay import library, librarythumb

import test_relay_library as lib_base
import test_relay_library_cache as cache_base
import test_relay_library_full as full_base

KB = 1024
JPEG = b"\xff\xd8\xff\xe0" + b"j" * 100


class Clock:
    def __init__(self):
        self.now = 1_800_000_000.0

    def __call__(self):
        return self.now


class RawRouter(cache_base.RawFront):
    """library.raw without the loopback check: /tv/api/library/raw/<id>?t=…[&w=1][&p=1][&g=1]."""

    def do_GET(self):
        url = urllib.parse.urlsplit(self.path)
        query = dict(urllib.parse.parse_qsl(url.query))
        iid = url.path.rsplit("/", 1)[-1]
        if not self.lib.raw_ok(iid, query.get("t")):
            self._not_found()
            return
        self.lib.serve_file(self, iid, raw=True, warm=query.get("w") == "1", pre=query.get("p") == "1",
                            thumb=query.get("g") == "1")


class Answer:
    """Enough of a request handler for serve(): records what was sent."""

    def __init__(self):
        self.sent = None

    def _send(self, code, body, kind, headers=None):
        self.sent = (code, body, kind)

    def _not_found(self):
        self.sent = (404, b"", "")


def make_clip(path: Path, black_s: int, picture_s: int, size: str = "320x180") -> bytes:
    """A small H.264 MKV: black_s seconds of black, then picture_s seconds of a test pattern; a keyframe each second."""
    subprocess.run([cache_base.FFMPEG, "-v", "error", "-nostdin", "-y",
                    "-f", "lavfi", "-i", f"color=c=black:s={size}:r=25:d={black_s}",
                    "-f", "lavfi", "-i", f"testsrc=size={size}:rate=25:d={picture_s}",
                    "-filter_complex", "[0:v][1:v]concat=n=2:v=1[v]", "-map", "[v]",
                    "-c:v", "libx264", "-preset", "ultrafast", "-g", "25", "-pix_fmt", "yuv420p", str(path)],
                   check=True)
    return path.read_bytes()


# ---------------------------------------------------------------- where the picture comes from

class SidecarTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.root = root = base / "videos"
        show = root / "Shows" / "Friends"
        lib_base.touch(show / "poster.jpg", b"\xff\xd8show")
        for n in (1, 2, 3):
            lib_base.touch(show / f"Friends S01E0{n}.mkv", lib_base.MP4)
        lib_base.touch(show / "Friends S01E01-thumb.jpg", b"\xff\xd8one")
        lib_base.touch(show / "Friends S01E02.png", b"\x89PNGtwo")
        lib_base.touch(root / "Movies" / "Inception (2010).mkv", lib_base.MP4)
        lib_base.touch(root / "Movies" / "Inception (2010).jpg", b"\xff\xd8movie")
        self.logs = []
        self.lib = lib_base.quiet(base / "data", self.logs)
        self.assertTrue(self.lib.add({"kind": "local", "path": str(root)})["ok"])
        self.items = {i["path"].rsplit("/", 1)[-1]: i for i in self.lib.items()}

    def tearDown(self):
        self.lib.close()
        self.tmp.cleanup()

    def test_own_image_wins_and_the_folder_poster_stays_the_shows(self):
        one, two, three = (self.items[f"Friends S01E0{n}.mkv"] for n in (1, 2, 3))
        self.assertEqual((one["thumb"], two["thumb"], three["thumb"]),
                         ("Shows/Friends/Friends S01E01-thumb.jpg", "Shows/Friends/Friends S01E02.png", ""))
        self.assertEqual({i["poster"] for i in (one, two, three)}, {"Shows/Friends/poster.jpg"},
                         "the episode's own image is not the show's poster")
        self.assertEqual(self.items["Inception (2010).mkv"]["poster"], "Movies/Inception (2010).jpg",
                         "a movie's <stem>.jpg is still its poster")
        show = self.lib.view()["shows"][0]
        self.assertEqual(show["art"], f"api/library/art/{one['id']}", "the show card: the folder poster")
        cards = {c["id"]: c["art"] for c in show["episodes"]}
        self.assertEqual(cards, {one["id"]: f"api/library/thumb/{one['id']}",
                                 two["id"]: f"api/library/thumb/{two['id']}", three["id"]: ""},
                         "no own picture yet: the page's placeholder, never the show poster")
        answer = Answer()
        self.lib.thumbs.serve(answer, one["id"])
        self.assertEqual(answer.sent, (200, b"\xff\xd8one", "image/jpeg"))
        self.lib.thumbs.serve(answer, two["id"])
        self.assertEqual(answer.sent, (200, b"\x89PNGtwo", "image/png"))
        for iid in (three["id"], self.items["Inception (2010).mkv"]["id"], "0" * 16):
            self.lib.thumbs.serve(answer, iid)
            self.assertEqual(answer.sent[0], 404, iid)
        self.assertEqual([i["id"] for i in self.lib.thumbs.due()], [three["id"]], "only the one without a picture")

    def test_a_grabbed_picture_shows_and_goes_when_an_image_appears(self):
        three = self.items["Friends S01E03.mkv"]
        thumbs = self.lib.thumbs
        thumbs.root.mkdir(parents=True)
        (thumbs.root / f"{thumbs.key_of(three)}.jpg").write_bytes(JPEG)
        thumbs._have = None                         # as after a restart: listed from the folder
        card = next(c for c in self.lib.view()["shows"][0]["episodes"] if c["id"] == three["id"])
        self.assertTrue(card["art"].startswith(f"api/library/thumb/{three['id']}?v="), card["art"])
        answer = Answer()
        thumbs.serve(answer, three["id"])
        self.assertEqual(answer.sent, (200, JPEG, "image/jpeg"))
        self.assertEqual(thumbs.due(), [])
        lib_base.touch(self.root / "Shows" / "Friends" / "Friends S01E03-thumb.webp", b"RIFFthree")
        self.lib.scan_now(self.lib._load_sources()[0]["id"])
        self.assertEqual(list(thumbs.root.iterdir()), [], "an image next to the file replaces the grabbed one")
        thumbs.serve(answer, three["id"])
        self.assertEqual(answer.sent, (200, b"RIFFthree", "image/webp"))


# ---------------------------------------------------------------- the queue and who gives way

class QueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        root = base / "videos"
        for rel in ("剧/某剧/S01E01.mkv", "剧/某剧/S01E02.mkv", "剧/另一部/S01E01.mkv", "电影/甲 (2020).mkv"):
            lib_base.touch(root / rel, lib_base.MP4)
        self.clock = Clock()
        self.logs = []
        self.lib = library.Library(base / "data", log=self.logs.append, clock=self.clock, background=False)
        self.lib.pace = False
        self.assertTrue(self.lib.add({"kind": "local", "path": str(root)})["ok"])
        self.ids = {i["path"]: i["id"] for i in self.lib.items()}
        self.lib.wire("ffmpeg-for-tests", "k" * 32, lambda: 1)
        self.lib._served_at = -1e9                  # nothing played lately
        self.thumbs = self.lib.thumbs

    def tearDown(self):
        self.lib.close()
        self.tmp.cleanup()

    def test_order_bump_and_refresh(self):
        due = [i["path"] for i in self.thumbs.due()]
        self.assertEqual(sorted(due), sorted(p for p in self.ids if p.startswith("剧/")), "episodes only")
        self.assertEqual(self.thumbs.bump(self.lib.show_key("另一部")), 1)
        self.assertEqual(self.thumbs.due()[0]["path"], "剧/另一部/S01E01.mkv", "the open show goes first")
        self.assertEqual(self.thumbs.bump("0" * 12), 0)
        iid = self.ids["剧/某剧/S01E02.mkv"]
        key = self.thumbs.key_of(self.lib.item(iid))
        self.thumbs.root.mkdir(parents=True)
        (self.thumbs.root / f"{key}.jpg").write_bytes(JPEG)
        self.thumbs._have = None
        self.assertNotIn(iid, [i["id"] for i in self.thumbs.due()])
        self.assertEqual(self.thumbs.refresh(iid), {"ok": True, "queued": True})
        self.assertFalse((self.thumbs.root / f"{key}.jpg").exists(), "the old picture is dropped")
        self.assertEqual(self.thumbs.due()[0]["id"], iid, "and it is grabbed first")
        self.assertEqual(self.thumbs._shift[iid], 1, "from the next position, not the same frame")
        self.assertEqual(librarythumb.spots_of(3000, 1)[0], librarythumb.spots_of(3000)[1])
        self.assertFalse(self.thumbs.refresh(self.ids["电影/甲 (2020).mkv"])["ok"], "movies have posters")
        self.assertFalse(self.thumbs.refresh("../etc")["ok"])

    def test_nothing_runs_while_something_plays_or_someone_watches(self):
        with mock.patch.object(self.thumbs, "grab", return_value=("done", 0)) as grab:
            self.lib._serving = 1
            self.assertEqual(self.thumbs.step()["stopped"], "有人在放片库里的片子")
            self.lib._serving = 0
            self.lib._served_at = self.clock()
            self.assertEqual(self.thumbs.step()["stopped"], "有人在放片库里的片子", "just finished: quiet a while")
            self.lib._served_at = -1e9
            self.lib.watching = lambda: True
            self.assertEqual(self.thumbs.step()["stopped"], "有人在看")
            self.lib.watching = None
            self.lib.pre._job = {"iid": "x", "why": "detail", "stop": False}
            self.assertEqual(self.thumbs.step()["stopped"], "在预读")
            self.lib.pre._job = None
            self.assertEqual(grab.call_count, 0)
            self.assertEqual(self.thumbs.step()["files"], 3)
        # While a grab runs the warm-up and the background pre-read wait.
        self.thumbs._job = {"iid": "x", "stop": False, "drive": False}
        self.assertEqual(self.lib.warm_blocked(), "在截分集封面")
        # Real playback stops it at once.
        answer = Answer()
        self.lib.serve_file(answer, "0" * 16)
        self.assertTrue(self.thumbs._job["stop"])
        self.assertTrue(self.thumbs.stopping())
        self.thumbs._job = None

    def test_a_failure_keeps_the_placeholder_and_backs_off(self):
        iid = self.ids["剧/某剧/S01E01.mkv"]
        with mock.patch.object(self.thumbs, "grab", return_value=("failed", 0)):
            result = self.thumbs.step()
        self.assertEqual((result["files"], result["failed"]), (0, 3))
        card = next(c for s in self.lib.view()["shows"] for c in s["episodes"] if c["id"] == iid)
        self.assertEqual(card["art"], "")
        self.assertEqual(self.thumbs.due(), [], "not retried straight away")
        self.assertTrue(any("3 集没截成" in line for line in self.logs), self.logs)
        self.clock.now += librarythumb.RETRY_S + 1
        self.assertEqual(len(self.thumbs.due()), 3, "retried after the backoff")
        with mock.patch.object(self.thumbs, "grab", return_value=("failed", 0)):
            self.thumbs.step()
        self.clock.now += librarythumb.RETRY_S + 1
        self.assertEqual(self.thumbs.due(), [], "the second time it waits twice as long")

    def test_drive_errors_pause_the_round(self):
        with mock.patch.object(self.thumbs, "grab", return_value=("drive", 0)) as grab:
            self.assertEqual(self.thumbs.step()["stopped"], "网盘出错了")
            self.assertEqual(self.thumbs.step()["stopped"], "网盘刚出过错，歇一会儿")
            self.assertEqual(grab.call_count, 1)

    def test_positions_and_arguments(self):
        self.assertEqual(librarythumb.spots_of(2700), [270.0, 675.0, 1350.0])
        self.assertEqual(librarythumb.spots_of(600), [90.0, 180.0, 300.0], "the opening minutes are skipped")
        self.assertEqual(librarythumb.spots_of(10000)[:2], [600.0, 1200.0], "bounded")
        self.assertEqual(librarythumb.spots_of(8), [0.8, 2.0, 4.0], "short clips by share")
        self.assertEqual(librarythumb.spots_of(0), [90.0, 10.0])
        args = librarythumb.thumb_args("ffmpeg", "http://127.0.0.1:1/raw", 270, 0, "/x/out.jpg")
        joined = " ".join(args)
        for part in ("-skip_frame nokey", "-noaccurate_seek", "-ss 270.000", "-threads 1", "-frames:v 1", "-map 0:0",
                     "scale=480:-2", "signalstats", "-q:v 4"):
            self.assertIn(part, joined)
        self.assertLess(args.index("-ss"), args.index("-i"), "seek on the input: only around one keyframe is read")
        hdr = " ".join(librarythumb.thumb_args("ffmpeg", "u", 1, 0, "o", tonemap="vt"))
        self.assertIn("-init_hw_device", hdr)
        self.assertIn("scale_vt=w=480", hdr)
        self.assertIn("intent=perceptual", " ".join(librarythumb.thumb_args("ffmpeg", "u", 1, 0, "o", tonemap="sws")))
        self.assertTrue(librarythumb.looks_blank({"YLOW": 0, "YHIGH": 20}, 9000), "dark")
        self.assertTrue(librarythumb.looks_blank({"YLOW": 120, "YHIGH": 126}, 9000), "flat")
        self.assertFalse(librarythumb.looks_blank({"YLOW": 10, "YHIGH": 200}, 9000))
        self.assertTrue(librarythumb.looks_blank({}, 1000), "no stats: by size")


# ---------------------------------------------------------------- the raw endpoint in thumbnail mode

class ThumbFront(cache_base.RawFront):
    def do_GET(self):
        self.lib.serve_file(self, self.path.rsplit("/", 1)[-1], raw=True, thumb=True)


class ServeThumbTests(full_base.ServeBase):
    def get_thumb(self, wanted):
        server = ThreadingHTTPServer(("127.0.0.1", 0), type("T", (ThumbFront,), {"lib": self.lib}))
        threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.fronts.append(server)
        return self.get(wanted, server.server_address[1])

    def test_reads_the_drive_quietly_and_leaves_the_whole_file_cache_alone(self):
        self.lib.thumbs._job = {"iid": self.iid, "stop": False, "drive": False}
        self.drive()
        piece = library.THUMB_PIECE
        response, data = self.get_thumb(f"{512 * KB}-{2560 * KB + 99}")
        self.assertEqual((response.status, data == full_base.MKV[512 * KB:2560 * KB + 100]), (206, True))
        self.assertEqual(self.drive(), [f"bytes={512 * KB}-{512 * KB + piece - 1}",
                                        f"bytes={512 * KB + piece}-{2560 * KB + 99}"], "one small piece at a time")
        self.assertEqual(self.runs(), [], "not written into the whole-file cache")
        self.assertIsNone(self.lib.full._job, "the downloader is not pointed at it")
        self.assertEqual(self.lib._serving, 0, "not counted as somebody playing")
        self.assertEqual(self.lib.thumbs._read, 2048 * KB + 100, "counted as the grab's drive bytes")
        self.lib.thumbs._job = None

    def test_a_stopped_grab_is_cut(self):
        self.lib.thumbs._job = {"iid": self.iid, "stop": True, "drive": False}
        response, _ = self.get_thumb("0-")
        self.assertEqual((response.status, self.drive()), (503, []))
        self.lib.thumbs._job = None


# ---------------------------------------------------------------- with a real ffmpeg

@unittest.skipUnless(cache_base.FFMPEG and cache_base.FFPROBE, "needs ffmpeg and ffprobe")
class RealFfmpegTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.root = base / "videos"
        self.clip = self.root / "剧" / "某剧" / "某剧 S01E01.mkv"
        self.clip.parent.mkdir(parents=True)
        make_clip(self.clip, 3, 5)
        self.logs = []
        self.lib = lib_base.quiet(base / "data", self.logs)
        self.assertTrue(self.lib.add({"kind": "local", "path": str(self.root)})["ok"])
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), type("R", (RawRouter,), {"lib": self.lib}))
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.lib.wire(cache_base.FFMPEG, "k" * 32, lambda: self.server.server_address[1])
        self.lib._served_at = -1e9
        self.thumbs = self.lib.thumbs
        self.tried = []
        run = self.thumbs._run

        def spy(item, video, at, tonemap):
            found = run(item, video, at, tonemap)
            self.tried.append((at, librarythumb.looks_blank(found[3], found[2]) if found[0] == "done" else None))
            return found

        self.thumbs._run = spy

    def tearDown(self):
        self.lib.close()
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def card(self):
        return self.lib.view()["shows"][0]["episodes"][0]

    def test_skips_the_black_opening_stores_and_serves_the_picture(self):
        result = self.thumbs.step()
        self.assertEqual((result["files"], result["failed"]), (1, 0), (result, self.logs))
        self.assertEqual(self.tried, [(0.8, True), (2.0, True), (4.0, False)], "black frames are passed over")
        item = self.lib.items()[0]
        path = self.thumbs.root / f"{item['id']}-{item['size']}-{item['mtime']}.jpg"
        self.assertTrue(path.is_file())
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.thumbs.root.stat().st_mode), 0o700)
        self.assertEqual([p.name for p in self.thumbs.root.iterdir()], [path.name], "no temporary file left")
        self.assertTrue(self.card()["art"].startswith(f"api/library/thumb/{item['id']}?v="))
        answer = Answer()
        self.thumbs.serve(answer, item["id"])
        self.assertEqual((answer.sent[0], answer.sent[1][:2], answer.sent[2]), (200, b"\xff\xd8", "image/jpeg"))
        line = next(m for m in self.logs if "片库分集封面" in m)
        self.assertIn("截好了 1 集", line)
        self.assertNotIn(str(self.root), "\n".join(self.logs))
        self.assertNotIn("127.0.0.1", "\n".join(self.logs))
        self.assertEqual(self.thumbs.due(), [], "kept: not grabbed again")
        # The file changes (same name, new content): the old picture goes at the next scan.
        make_clip(self.clip, 1, 6)
        self.lib.scan_now(self.lib._load_sources()[0]["id"])
        self.assertFalse(path.exists())
        self.assertEqual(self.card()["art"], "")
        self.assertEqual(len(self.thumbs.due()), 1)

    def test_a_broken_file_leaves_the_placeholder(self):
        self.clip.write_bytes(b"\x1aE\xdf\xa3" + bytes(4000))
        self.lib.scan_now(self.lib._load_sources()[0]["id"])
        result = self.thumbs.step()
        self.assertEqual((result["files"], result["failed"]), (0, 1), result)
        self.assertEqual(self.card()["art"], "")
        self.assertFalse(self.thumbs.root.exists() and any(self.thumbs.root.iterdir()))
        self.assertEqual(self.thumbs.due(), [], "waits before it tries again")


@unittest.skipUnless(cache_base.FFMPEG and cache_base.FFPROBE, "needs ffmpeg and ffprobe")
class RealDriveTests(full_base.ServeBase):
    def test_a_grab_over_the_drive(self):
        made = self.root / "real.mkv"
        data = make_clip(made, 0, 30)
        self.seen.files["剧集/剧/剧 S01E01.mkv"] = data
        self.lib.scan_now(self.lib._load_sources()[0]["id"])
        self.item = self.lib.items()[0]
        server = ThreadingHTTPServer(("127.0.0.1", 0), type("R", (RawRouter,), {"lib": self.lib}))
        threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.fronts.append(server)
        self.lib.wire(cache_base.FFMPEG, "k" * 32, lambda: server.server_address[1])
        self.lib._served_at = -1e9
        self.drive()
        result = self.lib.thumbs.step()
        self.assertEqual(result["files"], 1, (result, self.logs))
        self.assertTrue(self.drive(), "read from the drive")
        self.assertEqual(self.runs(), [], "the whole-file cache is not written")
        self.assertIsNone(self.lib.full._job)
        self.assertTrue(self.lib.view()["shows"][0]["episodes"][0]["art"])
        self.assertNotIn(self.url, "\n".join(self.logs))


# ---------------------------------------------------------------- the endpoints

class EndpointTests(unittest.TestCase):
    selfhost = True
    setUp = full_base.EndpointTests.setUp
    tearDown = full_base.EndpointTests.tearDown
    setUp_relay = full_base.EndpointTests.setUp_relay
    tearDown_relay = full_base.EndpointTests.tearDown_relay
    online = full_base.EndpointTests.online
    request = full_base.EndpointTests.request
    call = full_base.EndpointTests.call

    def test_refresh_is_for_admins_and_pictures_for_the_family(self):
        lib = self.relay.library
        root = Path(lib.dir) / "videos"                 # inside the test relay's temporary data folder
        lib_base.touch(root / "剧" / "某剧" / "S01E01.mkv", lib_base.MP4)
        self.assertTrue(lib.add({"kind": "local", "path": str(root)})["ok"])
        iid = lib.items()[0]["id"]
        path = "/tv/api/admin/library/thumb"
        for who in ("alice", "guest", None):
            self.assertEqual(self.call(who, "POST", path, {"id": iid})[0], 404)
        status, answer = self.call("admin", "POST", path, {"id": iid})
        self.assertEqual((status, answer), (200, {"ok": True, "queued": True}))
        self.assertFalse(self.call("admin", "POST", path, {"id": 5})[1]["ok"])
        actions = [r["action"] for r in self.accounts.audit_rows(100) if r["action"] == "library.thumb"]
        self.assertEqual(len(actions), 1)
        key = lib.thumbs.key_of(lib.item(iid))
        lib.thumbs.root.mkdir(parents=True, exist_ok=True)
        (lib.thumbs.root / f"{key}.jpg").write_bytes(JPEG)
        lib.thumbs._have = None
        status, _, body = self.request("GET", f"/tv/api/library/thumb/{iid}", "alice")
        self.assertEqual((status, body), (200, JPEG))
        self.assertEqual(self.request("GET", f"/tv/api/library/thumb/{iid}", "guest")[0], 404)
        self.assertIn(self.request("GET", f"/tv/api/library/thumb/{iid}")[0], (401, 404), "signed out")
        # Opening the show's detail page puts its episodes first.
        with mock.patch.object(lib.thumbs, "bump", return_value=1) as bump:
            self.call("alice", "POST", "/tv/api/library/preread", {"show": lib.show_key("某剧")})
        self.assertEqual(bump.call_args[0], (lib.show_key("某剧"),))


if __name__ == "__main__":
    unittest.main()
