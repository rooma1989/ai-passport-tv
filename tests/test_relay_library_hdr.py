"""My library: HDR videos are tone-mapped when they are turned into 8-bit H.264 (server/relay/library.py, watch.py).

What is checked, without a real ffmpeg: both probe parsers read the video's transfer and primaries
(the owner's HDR10 mkv dump says bt2020nc/bt2020/smpte2084; "unknown" is dropped; a dump with one
name means all three); is_hdr is true only for PQ and HLG. The TV's ffmpeg arguments get a
tone-mapping chain only for an HDR source and only with the chain the ffmpeg can run: vt keeps the
decoded frames on the GPU (one videotoolbox device for hwaccel, hwupload and scale_vt, scaled with
the 640x360 fit as expressions) and is used only with videotoolbox decoding and 4:2:0 video; sws
scales to 640x360 first and then maps with perceptual intent; an SDR source, a copied H.264 and an
ffmpeg that can run none of them keep the old arguments. Each chain is tried once per ffmpeg on a
small PQ sample and must raise the saturation clearly above the untouched output; errors count as
"cannot". Entries probed before the colour fields existed are probed once more, only when their
video is 10-bit, and a failure keeps the old result. On the PC, a transcoded HDR video gets the same
kind of chain at 720p, with the input options before -i; a copied HEVC is left to the browser.
With real ffmpeg and libx265 (skipped otherwise): a generated HDR10 clip probes as smpte2084, and
the chain this ffmpeg picks turns it into BT.709 with much more saturation than plain scaling.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from server.relay import library, watch

import test_relay_library as lib_base
import test_relay_library_play as play_base

s = play_base.s
HDR_MKV = {"dur": 3600.0, "start": 0.0, "fmt": "matroska,webm", "streams": [
    s(0, "video", "hevc", profile="Main 10", pix="yuv420p10le", width=3840, height=2160, trc="smpte2084",
      prim="bt2020"),
    s(1, "audio", "eac3", channels=6, default=True)]}
SDR10_MKV = {"dur": 1400.0, "start": 0.0, "fmt": "matroska,webm", "streams": [
    s(0, "video", "hevc", profile="Main 10", pix="yuv420p10le", width=1920, height=1080, trc="bt709", prim="bt709"),
    s(1, "audio", "aac", profile="LC", channels=2)]}
URL = "http://127.0.0.1:9/tv/api/library/raw/0123456789abcdef?t=1.x"


def value(args, flag):
    return args[args.index(flag) + 1]


class Ran:
    """A fake subprocess.run for the tone-mapping probe: the saturation each chain gives (None: ffmpeg fails)."""

    def __init__(self, base=45.5, vt=114.0, sws=90.0, zscale=None):
        self.sat = {"": base, "vt": vt, "sws": sws, "zscale": zscale}
        self.calls = []

    def __call__(self, args, **_kw):
        chain = value(args, "-vf")
        kind = "vt" if "scale_vt" in chain else "zscale" if "zscale" in chain else "sws" if "intent=" in chain else ""
        self.calls.append(kind)
        sat = self.sat[kind]
        out = f"frame:0\nlavfi.signalstats.YAVG=100\nlavfi.signalstats.SATAVG={sat}\n".encode() if sat else b""
        return type("Done", (), {"returncode": 0 if sat else 1, "stdout": out, "stderr": b""})()


def fresh(ffmpeg):
    for key in [k for k in library._TONEMAPS if k[0] == ffmpeg]:
        library._TONEMAPS.pop(key)
    library._TONEMAPS_BASE.pop(ffmpeg, None)


class ColourProbeTests(unittest.TestCase):
    def test_ffmpeg_dump_reads_transfer_and_primaries(self):
        video = library.first_video(library.parse_ffmpeg_dump(play_base.OWNER_MKV_DUMP))
        self.assertEqual((video["trc"], video["prim"]), ("smpte2084", "bt2020"))
        self.assertTrue(library.is_hdr(video))
        head = "Input #0, mov,mp4, from 'x':\n  Duration: 00:00:10.00, start: 0.000000, bitrate: 1 kb/s\n"
        one = library.parse_ffmpeg_dump(head + "  Stream #0:0: Video: h264 (High), yuv420p(tv, bt709, progressive), "
                                              "1920x1080, 25 fps\n")
        self.assertEqual((one["streams"][0]["trc"], one["streams"][0]["prim"]), ("bt709", "bt709"))
        hlg = library.parse_ffmpeg_dump(head + "  Stream #0:0: Video: hevc (Main 10), yuv420p10le(tv, "
                                              "bt2020nc/bt2020/arib-std-b67), 3840x2160, 50 fps\n")
        self.assertTrue(library.is_hdr(hlg["streams"][0]))
        plain = library.parse_ffmpeg_dump(head + "  Stream #0:0: Video: h264 (High), yuv420p(progressive), 640x360\n")
        self.assertNotIn("trc", plain["streams"][0])
        self.assertFalse(library.is_hdr(plain["streams"][0]))

    def test_ffprobe_json_reads_transfer_and_drops_unknown(self):
        data = json.dumps({"format": {"format_name": "matroska,webm", "duration": "60"}, "streams": [
            {"index": 0, "codec_type": "video", "codec_name": "hevc", "pix_fmt": "yuv420p10le",
             "color_transfer": "smpte2084", "color_primaries": "bt2020"},
            {"index": 1, "codec_type": "video", "codec_name": "h264", "color_transfer": "unknown"}]})
        got = library.parse_probe_json(data)["streams"]
        self.assertEqual((got[0]["trc"], got[0]["prim"]), ("smpte2084", "bt2020"))
        self.assertNotIn("trc", got[1])
        self.assertIn("color_transfer", library.PROBE_ENTRIES)

    def test_is_hdr(self):
        self.assertTrue(library.is_hdr(HDR_MKV["streams"][0]))
        self.assertFalse(library.is_hdr(SDR10_MKV["streams"][0]))
        self.assertFalse(library.is_hdr(play_base.OWNER_MKV["streams"][0]), "not probed for colour: SDR")
        self.assertFalse(library.is_hdr(None))


class TvToneMapArgsTests(unittest.TestCase):
    def test_vt_chain_keeps_frames_on_the_gpu(self):
        args = library.tv_args("/ff", URL, 0, HDR_MKV, ".mkv", hwaccel="videotoolbox", tonemap="vt")
        before = args[:args.index("-i")]
        self.assertEqual(value(before, "-init_hw_device"), "videotoolbox=" + library.VT_DEVICE)
        self.assertEqual(value(before, "-filter_hw_device"), library.VT_DEVICE)
        self.assertEqual(value(before, "-hwaccel_device"), library.VT_DEVICE)
        self.assertEqual(value(before, "-hwaccel_output_format"), "videotoolbox_vld")
        self.assertEqual(args.count("-hwaccel"), 1)
        vf = value(args, "-vf")
        self.assertTrue(vf.startswith("hwupload,scale_vt=w='if(gte(iw*360,ih*640),640,-2)'"
                                      ":h='if(gte(iw*360,ih*640),-2,360)'"), vf)
        self.assertIn("color_transfer=bt709", vf)
        self.assertTrue(vf.endswith("hwdownload,format=p010le,scale=out_color_matrix=bt709:out_range=tv,"
                                    "format=yuv420p"), vf)
        self.assertEqual(value(args, "-c:v"), "libx264")
        # 软解时（没有 videotoolbox 硬件解码）hwupload 把帧传上去：不要 -hwaccel。
        soft = library.tv_args("/ff", URL, 0, HDR_MKV, ".mkv", hwaccel="", tonemap="vt")
        self.assertIn("-init_hw_device", soft)
        self.assertNotIn("-hwaccel", soft)

    def test_sws_chain_scales_down_first(self):
        args = library.tv_args("/ff", URL, 0, HDR_MKV, ".mkv", hwaccel="videotoolbox", tonemap="sws")
        self.assertEqual(value(args, "-hwaccel"), "videotoolbox")
        self.assertNotIn("-init_hw_device", args)
        vf = value(args, "-vf")
        self.assertTrue(vf.startswith("scale=640:360:force_original_aspect_ratio=decrease"), vf)
        self.assertLess(vf.index("flags=bilinear"), vf.index("intent=perceptual"))
        self.assertIn("out_transfer=bt709:out_primaries=bt709", vf)
        self.assertTrue(vf.endswith(",format=yuv420p"))

    def test_no_chain_without_need_or_ability(self):
        plain = library.tv_args("/ff", URL, 0, HDR_MKV, ".mkv", hwaccel="videotoolbox")
        self.assertEqual(value(plain, "-vf"), "scale=640:360:force_original_aspect_ratio=decrease:force_divisible_by=2"
                                              ":flags=bilinear,format=yuv420p")
        copied = library.tv_args("/ff", URL, 0, dict(play_base.OWNER_MKV, streams=[
            s(0, "video", "h264", pix="yuv420p", height=1080, trc="smpte2084")]), ".mkv", tonemap="vt")
        self.assertEqual(value(copied, "-c:v"), "copy")
        self.assertNotIn("-init_hw_device", copied)
        audio_only = library.tv_args("/ff", URL, 0, {"streams": [s(0, "audio", "aac")]}, ".m4a", tonemap="sws")
        self.assertNotIn("intent=", value(audio_only, "-vf"), "the black frame is not tone-mapped")
        self.assertEqual(library.tv_args("/ff", URL, 0, HDR_MKV, ".mkv", tonemap="bogus"),
                         library.tv_args("/ff", URL, 0, HDR_MKV, ".mkv"))


class ToneMapChoiceTests(unittest.TestCase):
    def setUp(self):
        fresh("/ff-hdr")

    def tearDown(self):
        fresh("/ff-hdr")

    def test_prefers_vt_with_hardware_decoding_and_probes_once(self):
        ran = Ran()
        video = HDR_MKV["streams"][0]
        self.assertEqual(library.tonemap_for("/ff-hdr", video, "videotoolbox", run=ran), "vt")
        self.assertEqual(library.tonemap_for("/ff-hdr", video, "videotoolbox", run=ran), "vt")
        self.assertEqual(ran.calls, ["", "vt"], "the untouched baseline and vt, each once")
        self.assertEqual(library.tonemap_for("/ff-hdr", video, "", run=ran), "sws", "no videotoolbox: no vt")
        self.assertEqual(ran.calls, ["", "vt", "sws"])
        wide = dict(video, pix="yuv422p10le")
        self.assertEqual(library.tonemap_for("/ff-hdr", wide, "videotoolbox", run=ran), "sws", "vt only for 4:2:0")

    def test_falls_back_and_gives_up(self):
        ran = Ran(vt=None, sws=None, zscale=80.0)
        self.assertEqual(library.tonemap_for("/ff-hdr", HDR_MKV["streams"][0], "videotoolbox", run=ran), "zscale")
        fresh("/ff-hdr")
        weak = Ran(vt=50.0, sws=None, zscale=None)
        self.assertEqual(library.tonemap_for("/ff-hdr", HDR_MKV["streams"][0], "videotoolbox", run=weak), "",
                         "accepting the options is not enough: the colours have to change")
        fresh("/ff-hdr")

        def gone(*_a, **_k):
            raise OSError("no ffmpeg")
        self.assertEqual(library.tonemap_for("/ff-hdr", HDR_MKV["streams"][0], "videotoolbox", run=gone), "")
        self.assertFalse(library.tonemap_works("/ff-hdr", "sws", run=gone))

    def test_sdr_never_runs_anything(self):
        def boom(*_a, **_k):
            raise AssertionError("probed for an SDR video")
        self.assertEqual(library.tonemap_for("/ff-hdr", SDR10_MKV["streams"][0], "videotoolbox", run=boom), "")
        self.assertEqual(library.tonemap_for("/ff-hdr", None, "videotoolbox", run=boom), "")
        self.assertEqual(library.tonemap_for("", HDR_MKV["streams"][0], "videotoolbox", run=boom), "")


class WithColourTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.lib = lib_base.quiet(Path(self.tmp.name))
        self.iid = "0123456789abcdef"
        self.lib._items[self.iid] = {"id": self.iid, "source": "aaaaaaaa", "path": "S01/E01.mkv", "ext": ".mkv",
                                     "size": 10, "mtime": 1}
        self.probes = 0
        self.answer = dict(HDR_MKV)

        def probe(_iid):
            self.probes += 1
            return json.loads(json.dumps(self.answer)) if self.answer else None
        self.lib.probe = probe
        self.lib._save_index = lambda: None

    def tearDown(self):
        self.tmp.cleanup()

    def old(self, media):
        return dict(media, v=library.PROBE_VERSION, size=10, mtime=1)

    def test_old_ten_bit_entry_is_probed_once_more(self):
        old = self.old(play_base.OWNER_MKV)
        self.lib._items[self.iid]["media"] = old
        got = self.lib.with_color(self.iid, old)
        self.assertTrue(library.is_hdr(library.first_video(got)))
        self.assertTrue(got["color"])
        self.assertIs(self.lib._items[self.iid]["media"], got)
        self.assertIs(self.lib.with_color(self.iid, got), got)
        self.assertEqual(self.probes, 1)

    def test_failure_keeps_the_old_result_and_waits(self):
        old = self.old(play_base.OWNER_MKV)
        self.answer = None
        self.assertIs(self.lib.with_color(self.iid, old), old)
        self.assertIs(self.lib.with_color(self.iid, old), old)
        self.assertEqual(self.probes, 1, "not again within PROBE_RETRY_S")

    def test_eight_bit_and_new_entries_are_not_probed(self):
        eight = self.old(play_base.H264_AAC)
        self.assertIs(self.lib.with_color(self.iid, eight), eight)
        new = dict(self.old(SDR10_MKV), color=True)
        self.assertIs(self.lib.with_color(self.iid, new), new)
        self.assertIsNone(self.lib.with_color(self.iid, None))
        self.assertEqual(self.probes, 0)

    def test_new_probes_are_marked(self):
        self.lib.remote = lambda _item: False
        got = self.lib.media_of(self.iid)
        self.assertTrue(got["color"])
        self.assertEqual(self.probes, 1)


class PcToneMapTests(unittest.TestCase):
    FF = "/usr/bin/ffmpeg"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.lib = lib_base.quiet(Path(self.tmp.name))
        self.lib.wire(self.FF, "k" * 32, lambda: 18310)
        self.lib.remote = lambda _item: False
        self.item = {"id": "0123456789abcdef", "source": "aaaaaaaa", "path": "S01/E01.mkv", "ext": ".mkv"}
        self.saved_hw = library._HWACCELS.get(self.FF)
        fresh(self.FF)

    def tearDown(self):
        self.tmp.cleanup()
        fresh(self.FF)
        if self.saved_hw is None:
            library._HWACCELS.pop(self.FF, None)
        else:
            library._HWACCELS[self.FF] = self.saved_hw

    def args(self, media, caps):
        media = dict(media, color=True)
        decision = library.plan(media, ".mkv", caps)
        source = self.lib.watch_source(self.item, media, decision, 0.0, "E01")
        return watch.ffmpeg_args(self.FF, source, source.url(), watch.WatchService.mode_of(None, source)), source

    def test_transcoded_hdr_is_tone_mapped_at_720p(self):
        library._HWACCELS[self.FF] = frozenset()         # 没有硬件解码：软件的那条
        library._TONEMAPS_BASE[self.FF] = 45.0
        library._TONEMAPS[(self.FF, "sws")] = True
        args, source = self.args(HDR_MKV, {})
        vf = value(args, "-vf")
        self.assertTrue(vf.startswith(watch.ENCODE_SCALE + ",format=yuv420p10le,scale="), vf)
        self.assertIn("intent=perceptual", vf)
        self.assertEqual(value(args, "-c:v"), "libx264")
        self.assertIn("sws", source.key[2], "a different chain is a different stream")

    def test_vt_input_goes_before_the_input(self):
        source = watch.Source(("library", "x"), url=URL, paced=False, maps=("0:0",), exact=True,
                              video_filter=library.tonemap_vf("vt", watch.ENCODE_SCALE, library.PC_VT_SIZE),
                              video_input=tuple(library.tonemap_input("vt", "videotoolbox")))
        args = watch.ffmpeg_args(self.FF, source, URL, watch.Mode("encode", None))
        self.assertLess(args.index("-hwaccel_output_format"), args.index("-i"))
        self.assertIn("scale_vt=w=-2:h='min(720,ih)'", value(args, "-vf"))
        copied = watch.ffmpeg_args(self.FF, source, URL, watch.Mode("copy", None))
        self.assertNotIn("-init_hw_device", copied, "nothing to decode when copying")
        self.assertNotIn("-vf", copied)

    def test_sdr_copy_and_unknown_keep_the_old_arguments(self):
        library._HWACCELS[self.FF] = frozenset()
        library._TONEMAPS_BASE[self.FF] = 45.0
        library._TONEMAPS[(self.FF, "sws")] = True
        args, source = self.args(SDR10_MKV, {})
        self.assertEqual(value(args, "-vf"), watch.ENCODE_SCALE)
        self.assertEqual(source.video_input, ())
        copied, _ = self.args(HDR_MKV, play_base.MAC_CHROME)
        self.assertEqual(value(copied, "-c:v"), "copy", "the browser shows HDR itself")
        self.assertNotIn("-vf", copied)


@unittest.skipUnless(play_base.has_x265(), "needs ffmpeg with libx265")
class RealToneMapTests(unittest.TestCase):
    def test_hdr10_clip_comes_out_bt709_and_coloured(self):
        ff = play_base.FFMPEG
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "hdr10.mkv")
            subprocess.run(["nice", "-n", "15", ff, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                            "color=c=0xa05030:size=320x180:rate=10:duration=1", "-vf",
                            "format=yuv420p10le,setparams=color_primaries=bt2020:color_trc=smpte2084:"
                            "colorspace=bt2020nc:range=tv", "-c:v", "libx265", "-preset", "ultrafast", "-x265-params",
                            "log-level=error:hdr10=1:colorprim=bt2020:transfer=smpte2084:colormatrix=bt2020nc",
                            path], check=True)
            dump = subprocess.run([ff, "-hide_banner", "-i", path], capture_output=True, encoding="utf-8",
                                  errors="replace").stderr
            video = library.first_video(library.parse_ffmpeg_dump(dump))
            self.assertEqual(video["trc"], "smpte2084")
            kind = library.tonemap_for(ff, video, library.tv_hwaccel(ff))
            if not kind:
                self.skipTest("this ffmpeg can run no tone-mapping chain")

            def frame(chain, extra=()):
                out = subprocess.run(["nice", "-n", "15", ff, "-hide_banner", "-nostdin", "-loglevel", "info",
                                      *extra, "-i", path, "-frames:v", "1", "-vf",
                                      chain + ",showinfo,signalstats,metadata=print:file=-", "-f", "null", "-"],
                                     capture_output=True, encoding="utf-8", errors="replace")
                text = out.stdout + out.stderr
                return float(re.search(r"SATAVG=([0-9.]+)", text).group(1)), text

            plain, _ = frame("scale=160:90,format=yuv420p")
            mapped, text = frame(library.tonemap_vf(kind, "scale=160:90", "w=160:h=90"),
                                 library.tonemap_input(kind, library.tv_hwaccel(ff)))
        self.assertGreater(mapped, plain * library._TONEMAP_GAIN, (kind, plain, mapped))
        self.assertIn("color_trc:bt709", text)


if __name__ == "__main__":
    unittest.main()
