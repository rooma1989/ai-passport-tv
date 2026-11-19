"""My library: an MP4 whose B-frames lost their display order is transcoded (server/relay/library.py).

Some releases are remuxed into MP4 without the ctts box: the video has B-frames (has_b_frames > 0)
but every packet's presentation time equals its decode time. The browser then gets frames whose
times go backwards and drops about a quarter of them, so the picture judders; copying the stream
into fragmented MP4 or MPEG-TS keeps the wrong times. What is checked, without network:
missing_order reads ffprobe's JSON (B-frames and pts == dts is missing; B-frames with real
offsets, no B-frames, too few packets and garbage are not); plan never plays such a video directly
or copies it on the PC, and tv_args transcodes it for the TV; with_order checks an mp4 / m4v / mov
with H.264 or HEVC once, marks the video stream noct only when it is broken, marks the media as
checked, keeps the old result and waits PROBE_RETRY_S when the check fails, and never checks mkv,
other codecs or entries already checked. With real ffmpeg and ffprobe (skipped otherwise): a
generated clip with B-frames whose ctts box was blanked is found broken, the same clip intact is
not, and the PC's transcoding arguments turn the broken clip into evenly spaced frames.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from server.relay import library  # noqa: E402

import test_relay_library as lib_base  # noqa: E402
import test_relay_library_play as play_base  # noqa: E402

s = play_base.s
MP4_1080 = {"dur": 2691.0, "start": 0.0, "fmt": "mov,mp4,m4a,3gp,3g2,mj2", "streams": [
    s(0, "video", "h264", profile="High", pix="yuv420p", width=1920, height=1080),
    s(1, "audio", "aac", profile="LC", channels=2)]}


def broken(media: dict) -> dict:
    out = json.loads(json.dumps(media))
    out["streams"][0]["noct"] = True
    return out


def packets(pairs, reorder=2) -> str:
    return json.dumps({"streams": [{"has_b_frames": reorder}], "packets": [{"pts": p, "dts": d} for p, d in pairs]})


class MissingOrderTests(unittest.TestCase):
    def test_b_frames_with_pts_equal_to_dts_are_missing(self):
        self.assertTrue(library.missing_order(packets([(n * 1001 - 2002, n * 1001 - 2002) for n in range(32)])))

    def test_real_offsets_no_b_frames_and_unclear_answers(self):
        good = [(0, -2002), (3003, -1001), (1001, 0), (2002, 1001)] * 4
        self.assertFalse(library.missing_order(packets(good)))
        self.assertFalse(library.missing_order(packets([(n, n) for n in range(32)], reorder=0)))
        self.assertIsNone(library.missing_order(packets([(n, n) for n in range(4)])), "too few packets")
        self.assertIsNone(library.missing_order("not json"))
        self.assertIsNone(library.missing_order(json.dumps({"packets": []})))


class PlanTests(unittest.TestCase):
    def test_pc_never_plays_or_copies_a_broken_video(self):
        self.assertEqual(library.plan(MP4_1080, ".mp4", play_base.MAC_CHROME)["how"], "direct")
        got = library.plan(broken(MP4_1080), ".mp4", play_base.MAC_CHROME)
        self.assertEqual((got["how"], got["vcopy"], got["acopy"]), ("remux", False, True))
        self.assertIn("缺显示顺序", got["note"])

    def test_tv_transcodes_a_broken_video(self):
        url = "http://127.0.0.1:9/tv/api/library/raw/0123456789abcdef?t=1.x"
        copied = library.tv_args("/ff", url, 0, MP4_1080, ".mp4")
        self.assertEqual(copied[copied.index("-c:v") + 1], "copy")
        encoded = library.tv_args("/ff", url, 0, broken(MP4_1080), ".mp4")
        self.assertNotEqual(encoded[encoded.index("-c:v") + 1], "copy")


class WithOrderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.lib = lib_base.quiet(Path(self.tmp.name))
        self.lib.ffmpeg = "/opt/ff/ffmpeg"
        self.iid = "0123456789abcdef"
        self.lib._items[self.iid] = {"id": self.iid, "source": "aaaaaaaa", "path": "S07/S07E06.mp4", "ext": ".mp4",
                                     "size": 10, "mtime": 1, "title": "第 6 集"}
        self.checks = 0
        self.answer: bool | None = True

        def probe_order(_iid):
            self.checks += 1
            return self.answer
        self.lib.probe_order = probe_order
        self.lib._save_index = lambda: None

    def tearDown(self):
        self.tmp.cleanup()

    def old(self, media, ext=".mp4"):
        self.lib._items[self.iid]["ext"] = ext
        media = dict(media, v=library.PROBE_VERSION, size=10, mtime=1)
        self.lib._items[self.iid]["media"] = media
        return media

    def test_broken_mp4_is_marked_once(self):
        old = self.old(MP4_1080)
        got = self.lib.with_order(self.iid, old)
        self.assertTrue(got["order"])
        self.assertTrue(library.first_video(got)["noct"])
        self.assertNotIn("noct", got["streams"][1])
        self.assertIs(self.lib._items[self.iid]["media"], got)
        self.assertIs(self.lib.with_order(self.iid, got), got)
        self.assertEqual(self.checks, 1)
        self.assertEqual(library.plan(got, ".mp4", play_base.MAC_CHROME)["vcopy"], False)

    def test_good_mp4_is_marked_checked_only(self):
        self.answer = False
        got = self.lib.with_order(self.iid, self.old(MP4_1080))
        self.assertTrue(got["order"])
        self.assertNotIn("noct", library.first_video(got))
        self.assertEqual(library.plan(got, ".mp4", play_base.MAC_CHROME)["how"], "direct")

    def test_failure_keeps_the_old_result_and_waits(self):
        self.answer = None
        old = self.old(MP4_1080)
        original = library._ffprobe_of
        library._ffprobe_of = lambda _ff: "/opt/ff/ffprobe"
        try:
            self.assertIs(self.lib.with_order(self.iid, old), old)
            self.assertIs(self.lib.with_order(self.iid, old), old)
        finally:
            library._ffprobe_of = original
        self.assertEqual(self.checks, 1, "not again within PROBE_RETRY_S")

    def test_no_ffprobe_marks_checked_and_plays_as_before(self):
        self.answer = None
        original = library._ffprobe_of
        library._ffprobe_of = lambda _ff: None
        try:
            got = self.lib.with_order(self.iid, self.old(MP4_1080))
        finally:
            library._ffprobe_of = original
        self.assertTrue(got["order"])
        self.assertNotIn("noct", library.first_video(got))

    def test_mkv_other_codecs_and_nothing_are_not_checked(self):
        mkv = self.old(MP4_1080, ext=".mkv")
        self.assertIs(self.lib.with_order(self.iid, mkv), mkv)
        mpeg2 = self.old({"streams": [s(0, "video", "mpeg2video"), s(1, "audio", "aac")]})
        self.assertIs(self.lib.with_order(self.iid, mpeg2), mpeg2)
        audio = self.old({"streams": [s(0, "audio", "aac")]})
        self.assertIs(self.lib.with_order(self.iid, audio), audio)
        self.assertIsNone(self.lib.with_order(self.iid, None))
        self.lib.ffmpeg = ""
        plain = self.old(MP4_1080)
        self.assertIs(self.lib.with_order(self.iid, plain), plain)
        self.assertEqual(self.checks, 0)


FFMPEG, FFPROBE = shutil.which("ffmpeg"), shutil.which("ffprobe")


@unittest.skipUnless(FFMPEG and FFPROBE and "libx264" in subprocess.run(
    [FFMPEG or "ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True, encoding="utf-8").stdout if FFMPEG else False,
    "needs ffmpeg with libx264 and ffprobe")
class RealFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.good, self.bad = self.dir / "good.mp4", self.dir / "bad.mp4"
        subprocess.run([FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=30000/1001:duration=6",
                        "-c:v", "libx264", "-bf", "3", "-g", "60", str(self.good)], check=True)
        data = bytearray(self.good.read_bytes())
        at = data.find(b"ctts")
        self.assertGreater(at, 0, "x264 with B-frames writes a ctts box")
        data[at:at + 4] = b"free"
        self.bad.write_bytes(bytes(data))

    def tearDown(self):
        self.tmp.cleanup()

    def check(self, path: Path) -> bool | None:
        done = subprocess.run([FFPROBE, "-v", "error", "-select_streams", "v:0", "-read_intervals",
                               f"%+#{library.ORDER_PACKETS}", "-show_entries", "stream=has_b_frames:packet=pts,dts",
                               "-of", "json", str(path)], capture_output=True, check=True)
        return library.missing_order(done.stdout)

    def test_blanked_ctts_is_found_and_transcoding_fixes_the_times(self):
        self.assertTrue(self.check(self.bad))
        self.assertFalse(self.check(self.good))
        out = self.dir / "out.mp4"
        subprocess.run([FFMPEG, "-v", "error", "-y", "-ss", "1.000", "-i", str(self.bad), "-copyts",
                        "-avoid_negative_ts", "disabled", "-output_ts_offset", "10", "-c:v", "libx264",
                        "-preset", "veryfast", "-pix_fmt", "yuv420p", "-movflags",
                        "frag_keyframe+empty_moov+default_base_moof+frag_discont", "-f", "mp4", str(out)], check=True)
        times = subprocess.run([FFPROBE, "-v", "error", "-show_entries", "frame=pts", "-of", "csv=p=0", str(out)],
                               capture_output=True, text=True, encoding="utf-8", check=True).stdout.split()
        times = [t.strip(",") for t in times if t.strip(",")]
        steps = {int(b) - int(a) for a, b in zip(times, times[1:])}
        self.assertEqual(len(steps), 1, f"evenly spaced frames, got steps {sorted(steps)[:5]}")
        self.assertGreater(steps.pop(), 0)


if __name__ == "__main__":
    unittest.main()
