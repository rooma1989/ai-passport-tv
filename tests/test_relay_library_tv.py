"""My library, step 3 (server/relay/library.py, web.py, home.py): playing on the TV and carrying on.

What is checked, without network and without a real ffmpeg: the TV's ffmpeg arguments
(library.tv_args) -- the owner's 4K HEVC Main 10 mkv is decoded (videotoolbox when the ffmpeg
has it, only on macOS), scaled to at most 640x360, encoded to H.264 and its E-AC-3 to AAC
stereo, written as MPEG-TS at the real-time pace from the offset; an 8-bit H.264 of at most
1080 lines is copied; an unprobed file takes the first video and the first audio if any; a file
without audio gets a silent track. Through the relay: "Play to" a TV puts the item into that
TV's cast slot and sends the cast event; when the TV service opens the cast channel, the relay's
ffmpeg reads only the loopback raw address with a valid token (never the WebDAV address or
password). Progress from the TV lands in pc-progress.json under lib:<id> with the TV's name,
feeds Continue watching (one row per show), resumes on the PC and on the TV, and is not
overwritten by an old PC cursor. A show goes on to the next episode when one ends, the remote's
next / previous move between episodes, the last episode returns like a cast, and a stream that
"ends" far from the end resumes instead. Guests can play nothing from the library and see no
library item on the home page.
"""

from __future__ import annotations

import io
import json
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from server.relay import events, home, library, web
from server.relay.state import RESUME_BACK_S
from server.tenancy import files as device_files

import test_relay_library as lib_base
import test_relay_library_play as play_base
import test_relay_watch as watch_base

s = play_base.s
ALICE_DEVICE = watch_base.ALICE_DEVICE
H264_1080 = {"dur": 5400.0, "start": 0.0, "fmt": "mov,mp4", "streams": [
    s(0, "video", "h264", profile="High", pix="yuv420p", width=1920, height=1080),
    s(1, "audio", "aac", profile="LC", channels=2)]}


def args_of(media, ext=".mkv", hw="", offset=0.0):
    return library.tv_args("/ff", "http://127.0.0.1:9/tv/api/library/raw/0123456789abcdef?t=1.x", offset, media, ext,
                           hwaccel=hw)


def value(args, flag):
    return args[args.index(flag) + 1]


class TvArgsTests(unittest.TestCase):
    def test_owner_4k_hevc_is_decoded_scaled_and_encoded(self):
        args = args_of(play_base.OWNER_MKV, hw="videotoolbox", offset=125)
        self.assertLess(args.index("-hwaccel"), args.index("-i"))
        self.assertEqual(value(args, "-hwaccel"), "videotoolbox")
        self.assertLess(args.index("-re"), args.index("-i"))
        self.assertEqual((value(args, "-ss"), value(args, "-i")),
                         ("125.000", "http://127.0.0.1:9/tv/api/library/raw/0123456789abcdef?t=1.x"))
        self.assertEqual([args[i + 1] for i, a in enumerate(args) if a == "-map"], ["0:0", "0:1"],
                         "the video, not the cover; the eac3 track")
        self.assertIn("scale=640:360:force_original_aspect_ratio=decrease", value(args, "-vf"))
        self.assertEqual((value(args, "-c:v"), value(args, "-preset"), value(args, "-fpsmax")),
                         ("libx264", "ultrafast", "30"))
        self.assertEqual((value(args, "-c:a"), value(args, "-ac")), ("aac", "2"))
        self.assertEqual(args[-3:], ["-f", "mpegts", "pipe:1"])
        self.assertEqual(args.count("-i"), 1)
        self.assertNotIn("-hwaccel", args_of(play_base.OWNER_MKV), "no hardware decoding unless asked")

    def test_small_h264_is_copied(self):
        args = args_of(H264_1080, ".mp4", hw="videotoolbox")
        self.assertEqual(value(args, "-c:v"), "copy")
        self.assertNotIn("-hwaccel", args, "nothing to decode")
        self.assertNotIn("-vf", args)
        tall = {"streams": [s(0, "video", "h264", profile="High", pix="yuv420p", width=3840, height=2160),
                            s(1, "audio", "dts", channels=6)]}
        self.assertEqual(value(args_of(tall), "-c:v"), "libx264", "4K H.264 is scaled down")
        deep = {"streams": [s(0, "video", "h264", profile="High 10", pix="yuv420p10le", height=1080)]}
        self.assertEqual(value(args_of(deep), "-c:v"), "libx264")

    def test_unprobed_and_silent_files(self):
        args = args_of(None)
        self.assertEqual([args[i + 1] for i, a in enumerate(args) if a == "-map"], ["0:v:0", "0:a:0?"])
        self.assertEqual(value(args, "-c:v"), "libx264")
        silent = args_of({"streams": [play_base.HEVC10]})
        self.assertIn("anullsrc=r=48000:cl=stereo", silent)
        self.assertEqual([silent[i + 1] for i, a in enumerate(silent) if a == "-map"], ["0:0", "1:a"])
        self.assertIn("-shortest", silent)

    def test_hardware_decoding_only_on_a_mac_whose_ffmpeg_has_it(self):
        ran = []

        def run(cmd, **_kw):
            ran.append(cmd)
            return type("Done", (), {"stdout": b"Hardware acceleration methods:\nvideotoolbox\n\n"})()

        library._HWACCELS.pop("/ff-mac", None)
        self.assertEqual(library.hwaccels("/ff-mac", run=run), frozenset({"videotoolbox"}))
        self.assertEqual(library.tv_hwaccel("/ff-mac", platform="darwin"), "videotoolbox")
        self.assertEqual(library.tv_hwaccel("/ff-mac", platform="linux"), "")
        self.assertEqual(len(ran), 1, "asked once per ffmpeg")
        library._HWACCELS["/ff-plain"] = frozenset()
        self.assertEqual(library.tv_hwaccel("/ff-plain", platform="darwin"), "")
        library._HWACCELS.pop("/ff-gone", None)
        self.assertEqual(library.hwaccels("/ff-gone", run=lambda *_a, **_k: (_ for _ in ()).throw(OSError())),
                         frozenset())


# ---------------------------------------------------------------- through the relay

class FakeHandler:
    def __init__(self):
        self.wfile = io.BytesIO()
        self.status = None

    def send_response(self, code):
        self.status = code

    def send_header(self, *_a):
        pass

    def end_headers(self):
        pass

    def send_error(self, code):
        self.status = code


class FakeProc:
    def __init__(self, args):
        self.args = args
        self.killed = False

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        return 0


class TvTests(play_base._Relay):
    def setUp(self):
        super().setUp()
        self.ids = self.add_both()
        self.lib = library.wired(self.relay)
        self.started: list[FakeProc] = []
        self.relay.library_starter = lambda args: self.started.append(FakeProc(args)) or self.started[-1]
        self.run_s, self.outcome = 300.0, "client_gone"

        def pump(proc, write, on_tick):
            write(b"\x47" * 188)
            on_tick(self.run_s)
            return self.outcome

        self.relay.pumper = pump
        self.alice_caller = web.Caller(user=self.alice, role="user", name="Alice")

    def media(self, path, duration):
        """Pretend the file was probed (the relay's ffmpeg is fake here)."""
        iid = self.ids[path]
        with library._LOCK:
            item = self.lib._items[iid]
            item["media"] = dict(play_base.OWNER_MKV, dur=float(duration), v=library.PROBE_VERSION,
                                 size=item.get("size"), mtime=item.get("mtime"))
        return iid

    def to_tv(self, iid, who="alice"):
        return self.call(who, "POST", "/tv/api/play", {"target": ALICE_DEVICE, "item": {"kind": "library", "id": iid}})

    def open_cast(self, run_s=None, outcome=None):
        """The TV service opens /ch/<device>/cast once; the fake pump ticks once and ends."""
        if run_s is not None:
            self.run_s = run_s
        if outcome is not None:
            self.outcome = outcome
        handler = FakeHandler()
        self.relay.play("cast", handler, ALICE_DEVICE)
        return handler

    def cast(self):
        return self.relay._cast_of(self.relay.find_device(ALICE_DEVICE, self.alice_caller))

    def events(self):
        return events.read_events(device_files.device_files(self.devices_dir, ALICE_DEVICE).events)

    def saved(self, iid):
        return self.relay.pc_progress.get(self.alice, "lib:" + iid)

    def continue_rows(self, who="alice"):
        self.relay._home_cache.clear()
        return self.call(who, "GET", "/tv/api/home")[1]

    def test_plays_on_the_tv_through_the_loopback_address_only(self):
        movie = self.media("电影/盗梦空间 (2010)/盗梦空间 (2010).mp4", 5400)
        status, answer = self.to_tv(movie)
        self.assertEqual(status, 200)
        self.assertTrue(answer["ok"], answer)
        self.assertEqual((answer["channel"], answer["target"], answer["title"]), ("cast", ALICE_DEVICE, "盗梦空间"))
        video = self.cast().cast
        self.assertEqual((video.site, video.bvid, video.duration), ("library", movie, 5400))
        self.assertEqual(self.events()[-1].action, "cast")
        info = self.relay.stream_info("cast", ALICE_DEVICE)
        self.assertEqual((info["title"], info["duration"]), ("盗梦空间", 5400))
        self.assertEqual(self.open_cast().status, 200)
        args = self.started[0].args
        url = value(args, "-i")
        prefix = f"http://127.0.0.1:{self.manage.server_address[1]}/tv/api/library/raw/{movie}?t="
        self.assertTrue(url.startswith(prefix), url)
        self.assertTrue(self.lib.raw_ok(movie, url[len(prefix):]))
        line = " ".join(args)
        self.assertNotIn(lib_base.PASSWORD, line)
        self.assertNotIn(self.url, line, "the WebDAV address never reaches ffmpeg")
        self.assertEqual(value(args, "-f"), "mpegts")
        # Not in the cast history (a library file cannot be replayed as a link).
        self.assertEqual(self.relay._account(self.alice_caller).history, [])
        # The TV-follow watch on the PC does not try to fetch it from a website.
        self.assertIsInstance(self.relay._video_source(("video", "cast", "library", movie), video, 0.0), str)
        missing = self.to_tv("0" * 16)[1]
        self.assertFalse(missing["ok"])

    def test_progress_feeds_continue_watching_and_resumes_everywhere(self):
        movie = self.media("Inception.2010.mp4", 5400)
        self.assertTrue(self.to_tv(movie)[1]["ok"])
        self.open_cast(run_s=300.0, outcome="client_gone")
        saved = self.saved(movie)
        # Ticks record the screen's place (PLAYBACK_LAG_S behind what was pushed); changes under
        # home.PROGRESS_STEP_S are not rewritten.
        self.assertEqual((saved["duration"], saved["site"]), (5400.0, "library"))
        self.assertAlmostEqual(saved["position"], 300.0, delta=home.PROGRESS_STEP_S)
        self.assertEqual(saved["device"], self.relay.find_device(ALICE_DEVICE, self.alice_caller)["name"])
        row = next(r for r in self.continue_rows()["continue"] if r["id"] == f"pc:lib:{movie}")
        self.assertEqual((row["play"], row["progress"]["position"], row["progress"]["device"]),
                         ({"kind": "library", "id": movie}, int(saved["position"]), saved["device"]))
        position = saved["position"]
        # On the PC it starts where the TV stopped.
        pc = self.watch(library=movie)
        self.assertEqual((pc["start"], pc["resume"]), (position, position))
        self.call("alice", "POST", "/tv/api/watch/progress",
                  {"viewer": "c0ffee00", "sid": pc["sid"], "position": 900.0, "duration": 5400.0})
        # Back on the TV: from the PC's place (minus the usual few seconds), and the PC's cursor no longer
        # writes its old position back over the TV's.
        again = self.to_tv(movie)[1]
        self.assertEqual(again["resume"], 900.0)
        self.open_cast(run_s=1200.0, outcome="client_gone")
        self.assertEqual(value(self.started[-1].args, "-ss"), f"{900.0 - RESUME_BACK_S:.3f}")
        self.continue_rows()
        self.assertAlmostEqual(self.saved(movie)["position"], 900.0 - RESUME_BACK_S + 1200.0, delta=home.PROGRESS_STEP_S)
        # Pausing records the screen's position.
        self.relay._store_position(self.relay.find_device(ALICE_DEVICE, self.alice_caller), "cast", self.cast().cast,
                                   2500.0)
        self.assertEqual(self.saved(movie)["position"], 2500.0)

    def test_a_show_goes_on_to_the_next_episode(self):
        first = self.media("家庭剧/S01E01.mp4", 1200)
        second = self.media("家庭剧/S01E02.mp4", 1300)
        answer = self.to_tv(first)[1]
        self.assertEqual(answer["title"], "家庭剧")
        self.assertEqual(self.relay.stream_info("cast", ALICE_DEVICE)["episode"], 1)
        self.open_cast(run_s=1200.0, outcome="ended")
        self.assertEqual(self.cast().cast.bvid, second, "the next episode is in the cast slot")
        self.assertFalse(self.cast().cast_done)
        last = self.events()[-1]
        self.assertEqual((last.action, last.channel, last.value, last.total), ("loading", "cast", 2, 2))
        self.assertIn(first, self.relay.pc_progress.done(self.alice, "lib:" + first))
        self.assertGreater(self.saved(first)["position"], 1200.0 - home.PROGRESS_END_S, "not in Continue watching")
        # The second "ends" after 100 s of 1300: the drive broke off; it carries on from there.
        self.open_cast(run_s=100.0, outcome="ended")
        self.assertEqual((self.cast().cast.bvid, self.cast().cast_done), (second, False))
        self.assertNotEqual(self.events()[-1].action, "return")
        self.assertAlmostEqual(self.saved(second)["position"], 100.0, delta=home.PROGRESS_STEP_S)
        # The drive cannot be read at all (the cloud-drive component still starting after a restart): the run
        # "ends" within seconds. That is broken, not watched: it stays on this episode instead of skipping on.
        self.relay.library._drive_failed[second] = time.monotonic()
        self.open_cast(run_s=2.0, outcome="ended")
        self.assertEqual((self.cast().cast.bvid, self.cast().cast_done), (second, False))
        self.assertNotIn(second, self.relay.pc_progress.done(self.alice, "lib:" + second))
        # Continue watching shows the show once (its latest episode).
        self.relay.pc_progress.put(self.alice, "lib:" + first, dict(self.saved(first), position=600.0))
        self.now += 10
        self.relay.pc_progress.put(self.alice, "lib:" + second, dict(self.saved(second), position=700.0))
        rows = [r for r in self.continue_rows()["continue"] if r["source"] == "library"]
        self.assertEqual([r["play"]["id"] for r in rows], [second])
        # The remote: previous goes back to episode 1, next to episode 2, next on the last one returns.
        self.online(ALICE_DEVICE, "cast")
        proc = FakeProc([])
        entry = {"proc": proc, "video": self.cast().cast, "offset": 0.0, "began": self.relay.monotonic(),
                 "at": self.relay.clock(), "abort": ""}
        self.relay._active[(ALICE_DEVICE, "cast")] = entry
        back = self.relay.previous(self.alice_caller, ALICE_DEVICE)
        self.assertTrue(back["ok"], back)
        self.assertEqual(self.cast().cast.bvid, first)
        self.assertTrue(entry["abort"] and proc.killed, "the running stream is cut")
        self.relay._active[(ALICE_DEVICE, "cast")] = dict(entry, video=self.cast().cast, abort="")
        self.assertTrue(self.relay.next(self.alice_caller, ALICE_DEVICE)["ok"])
        self.assertEqual(self.cast().cast.bvid, second)
        self.relay._active[(ALICE_DEVICE, "cast")] = dict(entry, video=self.cast().cast, abort="")
        self.assertTrue(self.relay.next(self.alice_caller, ALICE_DEVICE)["ok"])
        self.assertTrue(self.cast().cast_done)
        self.assertEqual(self.events()[-1].action, "return")
        self.relay._active.pop((ALICE_DEVICE, "cast"), None)
        # Paused on episode 2, previous: the pause ends and episode 1 opens.
        self.relay._paused[ALICE_DEVICE] = {"cid": "cast", "video": self.cast().cast, "position": 50.0,
                                            "at": self.relay.clock(), "sleep": False}
        self.assertTrue(self.relay.previous(self.alice_caller, ALICE_DEVICE)["ok"])
        self.assertEqual((self.cast().cast.bvid, self.events()[-1].action), (first, "resume"))
        self.assertNotIn(ALICE_DEVICE, self.relay._paused)

    def test_guests_never_get_library_items(self):
        movie = self.media("Inception.2010.mp4", 5400)
        self.assertTrue(self.to_tv(movie)[1]["ok"])
        self.open_cast(run_s=300.0)
        status, answer = self.to_tv(movie, who="guest")
        self.assertTrue(status == 404 or not answer["ok"], answer)
        guest_home = self.continue_rows("guest")
        self.assertEqual((guest_home["library_recent"], guest_home["library"]["enabled"]), ([], False))
        text = json.dumps(guest_home, ensure_ascii=False)
        self.assertNotIn(movie, text)
        self.assertNotIn("lib:", text)
        # Directly: the rows refuse a guest caller even if someone asks for them.
        guest = web.Caller(user=None, role="guest", device=ALICE_DEVICE)
        self.assertEqual(library.home_rows(self.relay, guest), [])
        self.assertFalse(library.play_tv(self.relay, guest, self.relay.find_device(ALICE_DEVICE, guest), movie)["ok"])
        alice_rows = self.continue_rows()["continue"]
        self.assertTrue(any(r["id"] == f"pc:lib:{movie}" for r in alice_rows))


if __name__ == "__main__":
    unittest.main()
