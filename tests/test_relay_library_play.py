"""My library, step 2 (server/relay/library.py, watch.py): every video plays on the PC.

What is checked, without network: the decision table (library.plan) -- an mp4 with H.264 and
AAC first plays directly; the owner's two real cases (an mp4 with HEVC Main 10 and eac3, dts,
aac in that order; an mkv with HEVC Main 10, eac3 5.1, an ASS track, an attached mjpeg cover and
fonts) are remuxed with the video copied when the page says it can play HEVC and transcoded when
it cannot, the cover is never taken for the video, an existing AAC is copied (not from MPEG-TS),
otherwise the default audio is transcoded; Hi10P H.264 and Rext HEVC are transcoded; a remote
mp4 with text subtitles is remuxed (subtitles are extracted while streaming); a file with
nothing playable is refused; an unprobed file is guessed from its extension. Both probe parsers
(ffprobe JSON and ffmpeg's own input dump, which is all the PC client's downloaded ffmpeg has)
read the same streams. The ffmpeg arguments: explicit maps of only the chosen video and audio
(no subtitle, attachment or cover map in the media output), -tag:v hvc1, copy / encode as
decided, the exact timeline (-copyts, +frag_discont, output offset), the loopback token URL as
the only input and no WebDAV password anywhere; live channels keep their old arguments and
still copy only H.264. The CODECS string of HEVC comes from the hvcC (a synthetic one and a real
fMP4 init segment made by ffmpeg). A library stream reads at most VOD_AHEAD_SEGMENTS ahead of the
player, takes its ORIGIN from the offset rather than the first tfdt, and answers the playlist with
503 and the reading progress while the first segment builds. The loopback endpoint (/api/library/raw) refuses other hosts,
forwarded requests, missing, wrong and expired tokens. Subtitles: sidecar discovery and
language, SRT in GB18030, ASS dialogue lines, WebVTT round trip, image subtitles listed as
unavailable, the subtitle endpoint is family-only. With real ffmpeg and libx265 (skipped
otherwise): a local mkv remuxes to hvc1 + AAC through the relay's own HTTP port, its ASS track
comes back as WebVTT, and over WebDAV the subtitles are extracted while the video streams.
"""

from __future__ import annotations

import http.client
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from server import listen
from server.relay import library, watch

import test_relay_library as lib_base
import test_relay_watch as watch_base

FFMPEG = shutil.which("ffmpeg")


def has_x265() -> bool:
    if not FFMPEG:
        return False
    found = subprocess.run([FFMPEG, "-hide_banner", "-encoders"], capture_output=True, encoding="utf-8", errors="replace").stdout
    return "libx265" in found and " eac3 " in found


def s(i, t, c, **extra) -> dict:
    return library._stream_row(i, t, c, **extra)


HEVC10 = s(0, "video", "hevc", profile="Main 10", pix="yuv420p10le", width=3840, height=1616, default=True)
# The owner's 21 GB mp4: HEVC Main 10, then eac3 5.1, dts 5.1, aac stereo.
OWNER_MP4 = {"dur": 7200.0, "start": 0.0, "fmt": "mov,mp4,m4a,3gp,3g2,mj2", "streams": [
    HEVC10, s(1, "audio", "eac3", channels=6, default=True, lang="eng"), s(2, "audio", "dts", profile="DTS", channels=6),
    s(3, "audio", "aac", profile="LC", channels=2, lang="chi")]}
# The owner's mkv episodes: HEVC Main 10 4K, eac3 (Atmos) 5.1, ASS, an attached mjpeg cover.
OWNER_MKV = {"dur": 3600.0, "start": 0.0, "fmt": "matroska,webm", "streams": [
    s(0, "video", "hevc", profile="Main 10", pix="yuv420p10le", width=3840, height=2160),
    s(1, "audio", "eac3", channels=6, default=True), s(2, "subtitle", "ass", lang="chi", title="简体"),
    s(3, "video", "mjpeg", profile="Baseline", pix="yuvj420p", pic=True)]}
H264_AAC = {"dur": 60.0, "start": 0.0, "fmt": "mov,mp4", "streams": [
    s(0, "video", "h264", profile="High", pix="yuv420p"), s(1, "audio", "aac", profile="LC", channels=2)]}
MAC_CHROME = {"hevc": True, "hevc10": True, "direct_hevc": True, "direct_hevc10": True}
MSE_ONLY = {"hevc": True, "hevc10": True}

OWNER_MKV_DUMP = """Input #0, matroska,webm, from 'http://127.0.0.1:8100/tv/api/library/raw/0123456789abcdef?t=1.x':
  Metadata:
    title           : Show S01E01
  Duration: 01:00:00.04, start: 0.000000, bitrate: 20123 kb/s
  Stream #0:0: Video: hevc (Main 10), yuv420p10le(tv, bt2020nc/bt2020/smpte2084), 3840x2160 [SAR 1:1 DAR 16:9], 23.98 fps, 23.98 tbr, 1k tbn
    Metadata:
      BPS             : 19000000
  Stream #0:1(eng): Audio: eac3 (Dolby Digital Plus + Dolby Atmos), 48000 Hz, 5.1(side), fltp, 768 kb/s (default)
  Stream #0:2(chi): Subtitle: ass (ssa)
    Metadata:
      title           : 简体
  Stream #0:3: Video: mjpeg (Baseline), yuvj420p(pc, bt470bg/unknown/unknown), 600x900 [SAR 1:1 DAR 2:3], 90k tbr, 90k tbn (attached pic)
    Metadata:
      filename        : cover.jpg
  Stream #0:4: Attachment: ttf
    Metadata:
      filename        : font.ttf
At least one output file must be specified
"""


# ---------------------------------------------------------------- deciding

class PlanTests(unittest.TestCase):
    def test_direct_only_for_mp4_with_playable_video_and_first_audio(self):
        self.assertEqual(library.plan(H264_AAC, ".mp4", {})["how"], "direct")
        self.assertEqual(library.plan(H264_AAC, ".mov", MAC_CHROME)["how"], "direct")
        self.assertEqual(library.plan(H264_AAC, ".mkv", MAC_CHROME)["how"], "remux", "mkv is never direct")
        hevc_aac = {"streams": [HEVC10, s(1, "audio", "aac")]}
        self.assertEqual(library.plan(hevc_aac, ".mp4", MAC_CHROME)["how"], "direct")
        self.assertEqual(library.plan(hevc_aac, ".mp4", MSE_ONLY)["how"], "remux", "<video> said no HEVC")
        self.assertEqual(library.plan(H264_AAC, ".mp4", {}, remux=True)["how"], "remux", "direct failed in the page")

    def test_owner_mp4_with_eac3_first_is_remuxed_with_its_aac(self):
        got = library.plan(OWNER_MP4, ".mp4", MAC_CHROME)
        self.assertEqual((got["how"], got["video"], got["vcopy"], got["tag"], got["audio"], got["acopy"]),
                         ("remux", 0, True, "hvc1", 3, True), "Chrome plays only the first audio track (eac3: silent)")
        old = library.plan(OWNER_MP4, ".mp4", {})
        self.assertEqual((old["vcopy"], old["tag"], old["audio"], old["acopy"]), (False, "", 3, True),
                         "a page that says nothing about HEVC gets H.264")

    def test_owner_mkv_skips_the_cover_and_transcodes_eac3(self):
        got = library.plan(OWNER_MKV, ".mkv", MAC_CHROME)
        self.assertEqual((got["how"], got["video"], got["vcopy"], got["tag"], got["audio"], got["acopy"]),
                         ("remux", 0, True, "hvc1", 1, False))
        covered = {"streams": [s(0, "video", "mjpeg", pic=True)] + OWNER_MKV["streams"][:2]}
        covered["streams"][1] = dict(covered["streams"][1], i=1)
        covered["streams"][2] = dict(covered["streams"][2], i=2)
        self.assertEqual(library.plan(covered, ".mkv", MAC_CHROME)["video"], 1, "a cover first is still not the video")
        main8 = {"streams": [s(0, "video", "hevc", profile="Main", pix="yuv420p"), s(1, "audio", "ac3")]}
        self.assertTrue(library.plan(main8, ".mkv", {"hevc": True})["vcopy"])
        self.assertFalse(library.plan(main8, ".mkv", {"hevc10": True, "hevc": False})["vcopy"])

    def test_audio_choice(self):
        default_aac = {"streams": [HEVC10, s(1, "audio", "aac"), s(2, "audio", "aac", default=True)]}
        self.assertEqual(library.audio_choice(default_aac, ".mkv")[0]["i"], 2, "the default AAC first")
        self.assertEqual(library.audio_choice(default_aac, ".ts"), (default_aac["streams"][2], False),
                         "AAC from MPEG-TS has ADTS headers: encoded")
        others = {"streams": [HEVC10, s(1, "audio", "dts"), s(2, "audio", "truehd", default=True), s(3, "audio", "mp3")]}
        self.assertEqual(library.audio_choice(others, ".mkv"), (others["streams"][2], False), "the default track, encoded")
        self.assertEqual(library.audio_choice({"streams": [HEVC10]}, ".mkv"), (None, False))

    def test_codecs_the_browser_cannot_play_are_transcoded(self):
        for stream in (s(0, "video", "h264", profile="High 10", pix="yuv420p10le"),
                       s(0, "video", "h264", profile="High 4:4:4 Predictive"),
                       s(0, "video", "hevc", profile="Rext", pix="yuv444p"),
                       s(0, "video", "mpeg2video"), s(0, "video", "rv40"), s(0, "video", "vc1")):
            got = library.plan({"streams": [stream, s(1, "audio", "aac")]}, ".mkv", MAC_CHROME)
            self.assertEqual((got["how"], got["vcopy"]), ("remux", False), stream)

    def test_remote_text_subtitles_avoid_direct_and_nothing_playable_is_refused(self):
        with_subs = {"streams": H264_AAC["streams"] + [s(2, "subtitle", "mov_text")]}
        self.assertEqual(library.plan(with_subs, ".mp4", {}, remote=False)["how"], "direct")
        self.assertEqual(library.plan(with_subs, ".mp4", {}, remote=True)["how"], "remux")
        pgs = {"streams": H264_AAC["streams"] + [s(2, "subtitle", "hdmv_pgs_subtitle")]}
        self.assertEqual(library.plan(pgs, ".mp4", {}, remote=True)["how"], "direct", "image subs are not extracted")
        nothing = {"streams": [s(0, "subtitle", "ass"), s(1, "video", "mjpeg", pic=True)]}
        self.assertEqual(library.plan(nothing, ".mkv", MAC_CHROME)["how"], "none")
        self.assertFalse(library.Library.playable({"ext": ".mkv", "media": nothing}))
        self.assertTrue(library.Library.playable({"ext": ".mkv"}))

    def test_unprobed_files_are_guessed_from_the_extension(self):
        self.assertEqual(library.plan(None, ".mp4", {})["how"], "direct")
        generic = library.plan(None, ".mkv", MAC_CHROME)
        self.assertEqual((generic["how"], generic.get("generic"), generic["tag"]), ("remux", True, ""))

    def test_caps_are_strict_booleans(self):
        self.assertEqual(library.caps_of({"hevc": "yes", "hevc10": True, "x": True}),
                         {"hevc": False, "hevc10": True, "direct_hevc": False, "direct_hevc10": False})
        self.assertEqual(library.caps_of(None), dict.fromkeys(library.CAPS, False))


class ProbeParseTests(unittest.TestCase):
    def test_ffmpeg_dump(self):
        got = library.parse_ffmpeg_dump(OWNER_MKV_DUMP)
        self.assertAlmostEqual(got["dur"], 3600.04)
        self.assertEqual(got["fmt"], "matroska,webm")
        self.assertEqual([(x["i"], x["t"], x["c"]) for x in got["streams"]],
                         [(0, "video", "hevc"), (1, "audio", "eac3"), (2, "subtitle", "ass"), (3, "video", "mjpeg")])
        video, audio, sub, cover = got["streams"]
        self.assertEqual((video["p"], video["pix"], video["w"], video["h"]), ("Main 10", "yuv420p10le", 3840, 2160))
        self.assertEqual((audio["lang"], audio["ch"], audio.get("def")), ("eng", 6, True))
        self.assertEqual((sub["lang"], sub["title"]), ("chi", "简体"))
        self.assertTrue(cover["pic"])
        self.assertEqual(library.plan(got, ".mkv", MAC_CHROME)["video"], 0)
        self.assertIsNone(library.parse_ffmpeg_dump("http://x: Connection refused\n"))

    def test_ffprobe_json(self):
        data = json.dumps({"format": {"format_name": "mov,mp4", "duration": "7200.5", "start_time": "0.000000"},
                           "streams": [
                               {"index": 0, "codec_type": "video", "codec_name": "hevc", "profile": "Main 10",
                                "pix_fmt": "yuv420p10le", "width": 3840, "height": 1616, "disposition": {"default": 1}},
                               {"index": 1, "codec_type": "audio", "codec_name": "eac3", "channels": 6,
                                "disposition": {"default": 1}, "tags": {"language": "eng"}},
                               {"index": 2, "codec_type": "attachment", "codec_name": "ttf"},
                               {"index": 3, "codec_type": "audio", "codec_name": "aac", "profile": "LC", "channels": 2}]})
        got = library.parse_probe_json(data)
        self.assertEqual((got["dur"], got["fmt"]), (7200.5, "mov,mp4"))
        self.assertEqual([x["i"] for x in got["streams"]], [0, 1, 3], "attachments are not kept")
        self.assertEqual(library.plan(got, ".mp4", MAC_CHROME)["audio"], 3)
        self.assertIsNone(library.parse_probe_json(b"not json"))

    @unittest.skipUnless(FFMPEG and shutil.which("ffprobe"), "needs ffmpeg and ffprobe")
    def test_both_parsers_agree_on_a_real_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "a.mkv")
            subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                            "testsrc2=size=160x90:rate=25:duration=1", "-f", "lavfi", "-i", "sine=duration=1",
                            "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-metadata:s:a:0", "language=jpn",
                            path], check=True)
            dump = subprocess.run([FFMPEG, "-hide_banner", "-i", path], capture_output=True, encoding="utf-8", errors="replace").stderr
            probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                                    "format=format_name,duration,start_time:stream=index,codec_type,codec_name,profile,"
                                    "pix_fmt,width,height,channels:stream_disposition=default,forced,attached_pic:"
                                    "stream_tags=language,title", "-of", "json", path], capture_output=True).stdout
        self.assertEqual(library.parse_ffmpeg_dump(dump)["streams"], library.parse_probe_json(probe)["streams"])


# ---------------------------------------------------------------- ffmpeg

class FfmpegArgsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.lib = lib_base.quiet(Path(self.tmp.name))
        self.lib.wire("/usr/bin/ffmpeg", "k" * 32, lambda: 18310)
        self.item = {"id": "0123456789abcdef", "source": "aaaaaaaa", "path": "S01/E01.mkv", "ext": ".mkv"}

    def tearDown(self):
        self.tmp.cleanup()

    def args(self, media, caps, offset=0.0, remote=False):
        decision = library.plan(media, self.item["ext"], caps)
        self.lib.remote = lambda _item: remote
        source = self.lib.watch_source(self.item, media, decision, offset, "E01")
        url = source.url()
        return watch.ffmpeg_args("/usr/bin/ffmpeg", source, url, watch.WatchService.mode_of(None, source)), source, url

    def test_owner_mkv_maps_only_video_and_audio_with_hvc1(self):
        args, source, url = self.args(OWNER_MKV, MAC_CHROME, offset=125.5)
        self.assertRegex(url, r"^http://127\.0\.0\.1:18310/tv/api/library/raw/0123456789abcdef\?t=\d+\.[\w-]+$")
        self.assertEqual(args[args.index("-i") + 1], url, "the only input is the loopback token URL")
        maps = [args[i + 1] for i, a in enumerate(args) if a == "-map"]
        self.assertEqual(maps, ["0:0", "0:1"], "no subtitle, attachment or cover in the output")
        self.assertEqual(args[args.index("-c:v") + 1], "copy")
        self.assertEqual(args[args.index("-tag:v") + 1], "hvc1")
        self.assertEqual(args[args.index("-c:a") + 1:args.index("-c:a") + 4], ["aac", "-b:a", "192k"])
        self.assertIn("-sn", args)
        self.assertEqual(args[args.index("-ss") + 1], "125.500")
        self.assertIn("-copyts", args)
        self.assertEqual(args[args.index("-output_ts_offset") + 1], str(watch.EXACT_TS_OFFSET))
        self.assertTrue(args[args.index("-movflags") + 1].endswith("+frag_discont"))
        self.assertNotIn("-re", args, "a library file is read ahead of the player (bounded)")
        self.assertEqual(args[-1], "pipe:1", "local files have no subtitle side output")
        self.assertEqual(source.copy_video, ("h264", "hevc"))
        self.assertTrue(source.bounded and source.exact)

    def test_transcode_and_aac_copy(self):
        args, _, _ = self.args(OWNER_MP4, {}, offset=0)
        self.assertEqual([args[i + 1] for i, a in enumerate(args) if a == "-map"], ["0:0", "0:3"])
        self.assertEqual(args[args.index("-c:v") + 1], "libx264")
        self.assertNotIn("-tag:v", args)
        self.assertEqual(args[args.index("-c:a") + 1], "copy")
        self.assertNotIn("-ss", args)

    def test_remote_text_subtitles_get_their_own_output_and_no_password(self):
        args, source, _ = self.args(OWNER_MKV, MAC_CHROME, remote=True)
        main = args.index("pipe:1")
        self.assertEqual([args[i + 1] for i, a in enumerate(args[:main]) if a == "-map"], ["0:0", "0:1"])
        tail = args[main + 1:]
        self.assertEqual(tail[:6], ["-map", "0:2", "-c:s", "webvtt", "-flush_packets", "1"])
        self.assertTrue(tail[-1].endswith("0123456789abcdef-s2.live.vtt"))
        self.assertEqual(args[1:4], ["-hide_banner", "-nostats", "-loglevel"])
        self.assertIn("-y", args)
        joined = " ".join(args)
        self.assertNotIn("@", joined)
        self.assertNotIn("password", joined.lower())

    def test_live_channels_keep_their_arguments(self):
        live = watch.Source(("live", "ch1", "https://x/1.m3u8", "UA"), url="https://x/1.m3u8", paced=False)
        args = watch.ffmpeg_args("ffmpeg", live, "https://x/1.m3u8", watch.FIRST_MODE)
        self.assertEqual([args[i + 1] for i, a in enumerate(args) if a == "-map"], ["0:V:0?", "0:a:0?"])
        for flag in ("-copyts", "-tag:v", "-output_ts_offset", "-y"):
            self.assertNotIn(flag, args)
        self.assertNotIn("frag_discont", " ".join(args))
        self.assertFalse(watch.fits(watch.FIRST_MODE, "hevc", "aac"), "live still copies only H.264")
        self.assertEqual(watch.mode_for("hevc", "aac"), watch.Mode("encode", "encode"))
        self.assertTrue(watch.fits(watch.FIRST_MODE, "hevc", "aac", ("h264", "hevc")))
        self.assertEqual(watch.mode_for("hevc", "eac3", ("h264", "hevc")), watch.Mode("copy", "encode"))


class BoundedStreamTests(unittest.TestCase):
    """A library stream runs ahead of the player, but only VOD_AHEAD_* ahead; while the first segment is being
    built the playlist answers 503 with the progress text."""

    def stream(self, data, progress=None):
        popen = watch_base.Popen(data=data)
        source = watch.Source(("library", "0123456789abcdef", "t"), url="http://127.0.0.1:1/tv/api/library/raw/x?t=1",
                              paced=False, bounded=True, exact=True, offset=30.0, start=30.0, media_start=1.5,
                              mode=watch.Mode("copy", "encode"), copy_video=("h264", "hevc"), progress=progress)
        stream = watch.WatchStream("0" * 16, "pc1-x", source, "ffmpeg", log=lambda _m: None, popen=popen,
                                   mode=source.mode)
        self.addCleanup(stream.stop)
        return stream, popen

    def wait(self, check):
        deadline = time.time() + 5
        while time.time() < deadline and not check():
            time.sleep(0.02)
        return check()

    def test_reads_ahead_only_so_far(self):
        data = watch_base.FTYP + watch_base.moov() + watch_base.fragment(2.0) * (watch.VOD_AHEAD_SEGMENTS + 4)
        stream, popen = self.stream(data)
        stream.start()
        self.assertTrue(self.wait(lambda: stream.window.next_seq == watch.VOD_AHEAD_SEGMENTS))
        time.sleep(0.2)
        self.assertEqual(stream.window.next_seq, watch.VOD_AHEAD_SEGMENTS, "held until the player takes one")
        self.assertEqual(stream.window.origin, 30.0 + 1.5 + watch.EXACT_TS_OFFSET, "origin from the offset, not tfdt")
        stream.fetched(1)
        self.assertTrue(self.wait(lambda: stream.window.next_seq == watch.VOD_AHEAD_SEGMENTS + 2))
        self.assertNotIn("-re", popen.procs[0].args)

    def test_progress_while_the_first_segment_builds(self):
        stream, _ = self.stream(b"", progress=lambda: "正在读取文件…已读 12 MB")
        stream.start()
        sent = []
        handler = SimpleNamespace(_send=lambda code, body, kind, headers=None: sent.append((code, body.decode(), headers)),
                                  _not_found=lambda: sent.append(404))
        relay = SimpleNamespace(watch=SimpleNamespace(find=lambda sid, token: stream))
        started = time.time()
        watch.get(handler, relay, f"/watch/{'0' * 16}/1.{'a' * 20}/index.m3u8")
        self.assertLess(time.time() - started, watch.READY_WAIT_S)
        self.assertEqual(sent, [(503, "正在读取文件…已读 12 MB", {"X-RTV-Notice": "1"})])


class HevcCodecTests(unittest.TestCase):
    def test_from_hvcc(self):
        # Main 10, Main tier, level 5.1, compatibility flag 2, constraints progressive + frame-only.
        hvcc = bytes([1, 0x02, 0x20, 0, 0, 0, 0xB0, 0, 0, 0, 0, 0, 153]) + b"\0" * 10
        self.assertEqual(watch.hevc_codec("hvc1", hvcc), "hvc1.2.4.L153.B0")
        main = bytes([1, 0x01, 0x60, 0, 0, 0, 0x90, 0, 0, 0, 0, 0, 120])
        self.assertEqual(watch.hevc_codec("hvc1", main), "hvc1.1.6.L120.90")
        high_tier = bytes([1, 0x22, 0x20, 0, 0, 0, 0, 0, 0, 0, 0, 0, 156])
        self.assertEqual(watch.hevc_codec("hev1", high_tier), "hev1.2.4.H156")
        self.assertEqual(watch.hevc_codec("hvc1", b"\x01"), "hvc1.1.6.L93.B0", "a broken hvcC gets a safe default")

    @unittest.skipUnless(has_x265(), "needs ffmpeg with libx265")
    def test_from_a_real_init_segment(self):
        out = subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                              "testsrc2=size=320x180:rate=25:duration=1", "-f", "lavfi", "-i", "sine=duration=1",
                              "-c:v", "libx265", "-preset", "ultrafast", "-pix_fmt", "yuv420p10le",
                              "-x265-params", "log-level=error", "-tag:v", "hvc1", "-c:a", "aac",
                              "-movflags", "frag_keyframe+empty_moov+default_base_moof", "-f", "mp4", "pipe:1"],
                             capture_output=True, check=True).stdout
        moov = out[out.find(b"moov") - 4:]
        tracks = watch.init_info(moov[:int.from_bytes(moov[:4], "big")])
        codecs = watch.codecs_of(tracks)
        self.assertRegex(codecs, r"^hvc1\.2\.4\.L\d+\.[0-9A-F]+(\.[0-9A-F]+)*,mp4a\.40\.2$")


# ---------------------------------------------------------------- subtitles

class SubtitleTests(unittest.TestCase):
    def test_sidecars_are_found_next_to_the_video(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name) / "v"
        lib_base.touch(root / "Movie (2020).mkv", b"x")
        lib_base.touch(root / "Movie (2020).zh.srt", b"1")
        lib_base.touch(root / "Movie (2020).简体.ass", b"1")
        lib_base.touch(root / "Movie (2020).srt", b"1")
        lib_base.touch(root / "Other.srt", b"1")
        lib_base.touch(root / "Movie (2020).nfo", b"<movie/>")
        lib = lib_base.quiet(Path(tmp.name) / "data")
        self.assertTrue(lib.add({"kind": "local", "path": str(root)})["ok"])
        item = lib.items()[0]
        self.assertEqual(item["subs"], ["Movie (2020).srt", "Movie (2020).zh.srt", "Movie (2020).简体.ass"])
        tracks = library.subtitle_tracks(item, {"streams": [s(2, "subtitle", "ass", lang="eng"),
                                                            s(3, "subtitle", "hdmv_pgs_subtitle", lang="chi")]})
        self.assertEqual([(t["n"], t["kind"], t["ok"]) for t in tracks],
                         [(0, "embedded", True), (1, "embedded", False), (2, "file", True), (3, "file", True),
                          (4, "file", True)])
        labels = [library.subtitle_label(t) for t in tracks]
        self.assertEqual(labels, ["英文", "中文（图片字幕，放不了）", "字幕 3（外挂）", "中文（外挂）", "简体（外挂）"])

    def test_srt_in_gb18030_and_ass_become_webvtt(self):
        srt = "1\r\n00:00:01,500 --> 00:00:03,000\r\n第一句\r\n\r\n2\r\n00:01:02,000 --> 00:01:04,250\r\n<i>第二句</i>\r\n"
        vtt = library.sidecar_vtt(srt.encode("gb18030"), ".srt")
        self.assertTrue(vtt.startswith("WEBVTT\n"))
        self.assertEqual(library.parse_vtt(vtt), [(1.5, 3.0, "第一句"), (62.0, 64.25, "<i>第二句</i>")])
        ass = ("[Script Info]\nScriptType: v4.00+\n\n[Events]\n"
               "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
               "Dialogue: 0,0:00:10.00,0:00:12.50,Default,,0,0,0,,{\\b1}你好，世界\\N第二行\n"
               "Dialogue: 0,0:00:05.00,0:00:06.00,Default,,0,0,0,,{\\p1}m 0 0 l 10 10{\\p0}\n"
               "Dialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,a < b\n")
        self.assertEqual(library.parse_vtt(library.sidecar_vtt(("﻿" + ass).encode("utf-8"), ".ass")),
                         [(1.0, 2.0, "a &lt; b"), (10.0, 12.5, "你好，世界\n第二行")])
        self.assertEqual(library.decode_text("字幕".encode("utf-16")), "字幕")
        self.assertEqual(library.write_vtt([(3725.5, 3726.0, "x")]), "WEBVTT\n\n01:02:05.500 --> 01:02:06.000\nx\n\n")


# ---------------------------------------------------------------- through the relay

class _Relay(unittest.TestCase):
    """The relay, sources and helpers of test_relay_library.ApiTests (borrowed, so its tests do not run twice)."""
    selfhost = True
    setUp_relay = watch_base.RelayWatchTests.setUp
    tearDown_relay = watch_base.RelayWatchTests.tearDown
    online = watch_base.RelayWatchTests.online
    setUp = lib_base.ApiTests.setUp
    tearDown = lib_base.ApiTests.tearDown
    request = lib_base.ApiTests.request
    call = lib_base.ApiTests.call
    add_both = lib_base.ApiTests.add_both
    watch = lib_base.ApiTests.watch


class EndpointTests(_Relay):
    def setUp(self):
        super().setUp()
        lib_base.touch(self.root / "Inception.2010.zh.srt", "1\n00:00:01,000 --> 00:00:02,000\n你好\n".encode("gb18030"))
        self.ids = self.add_both()
        self.movie = self.ids["Inception.2010.mp4"]
        self.lib = library.wired(self.relay)

    def raw(self, iid, token, headers=None):
        return self.request("GET", f"/tv/api/library/raw/{iid}?t={token}", headers=headers)

    def test_loopback_endpoint_needs_a_good_token_from_this_machine(self):
        good = self.lib.raw_token(self.movie)
        status, response, raw = self.raw(self.movie, good, {"Range": "bytes=0-99"})
        self.assertEqual((status, raw), (206, lib_base.MP4[:100]))
        self.assertGreaterEqual(self.lib.bytes_read(self.movie), 100)
        other = self.ids["家庭剧/S01E01.mp4"]
        expired = listen.issue_token(self.lib.secret, f"libraw/{self.movie}", now=time.time() - 7 * 3600,
                                     lifetime_s=library.RAW_LIFETIME_S)
        for iid, token in ((self.movie, ""), (self.movie, "1.abc"), (other, good), (self.movie, expired)):
            self.assertEqual(self.raw(iid, token)[0], 404, (iid, token))
        self.assertEqual(self.raw(self.movie, good, {"X-Real-IP": "192.168.1.9"})[0], 404, "came through the front")
        self.assertEqual(self.raw(self.movie, good, {"X-Forwarded-Proto": "https"})[0], 404)
        # Another host (the relay only listens on 127.0.0.1, so ask the check directly).
        sent = []
        handler = SimpleNamespace(client_address=("192.168.1.5", 5000), headers={}, _query=lambda: {"t": good},
                                  _not_found=lambda: sent.append(404))
        self.assertTrue(library.raw(handler, self.relay, f"/api/library/raw/{self.movie}"))
        self.assertEqual(sent, [404])
        # A signed-in family member does not get it either: the token is for the relay's own ffmpeg.
        self.assertEqual(self.request("GET", f"/tv/api/library/raw/{self.movie}", "alice")[0], 404)

    def test_subtitles_are_family_only_and_answer_carries_them(self):
        path = f"/tv/api/library/subs/{self.movie}/0.vtt"
        for who in ("guest", None):
            self.assertEqual(self.request("GET", path, who)[0], 404, who)
        status, response, raw = self.request("GET", path, "alice")
        self.assertEqual((status, response.getheader("Content-Type"), response.getheader("X-RTV-Partial")),
                         (200, "text/vtt; charset=utf-8", "0"))
        self.assertEqual(library.parse_vtt(raw.decode()), [(1.0, 2.0, "你好")])
        self.assertEqual(self.request("GET", f"/tv/api/library/subs/{self.movie}/1.vtt", "alice")[0], 404)
        status, _, raw = self.request("GET", path + "?after=5", "alice")
        self.assertEqual(library.parse_vtt(raw.decode()), [], "after: only cues that end later")
        answer = self.watch(library=self.movie, caps={"hevc": True})
        self.assertEqual(answer["subs"], [{"n": 0, "label": "中文（外挂）", "lang": "zh", "ok": True, "def": False}])
        self.assertEqual(answer["subs_url"], f"api/library/subs/{self.movie}/")


@unittest.skipUnless(has_x265(), "needs ffmpeg with libx265 and eac3")
class RealRemuxTests(_Relay):
    """A generated mkv like the owner's episodes, played through the relay's real HTTP port and real ffmpeg."""

    @classmethod
    def setUpClass(cls):
        cls.media = tempfile.TemporaryDirectory()
        root = Path(cls.media.name)
        ass = ("[Script Info]\nScriptType: v4.00+\n\n[V4+ Styles]\nFormat: Name, Fontname, Fontsize\nStyle: Default,Arial,20\n\n"
               "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
               + "".join(f"Dialogue: 0,0:00:{t:02d}.00,0:00:{t + 1:02d}.50,Default,,0,0,0,,第{t}秒\n" for t in range(0, 6)))
        (root / "s.ass").write_text(ass, encoding="utf-8")
        subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=red:s=60x90",
                        "-frames:v", "1", str(root / "cover.jpg")], check=True)
        subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                        "testsrc2=size=160x90:rate=25:duration=6", "-f", "lavfi", "-i",
                        "aevalsrc=0.1*sin(440*2*PI*t)|0.1*sin(550*2*PI*t)|0|0|0|0:c=5.1:s=48000:d=6",
                        "-i", str(root / "s.ass"), "-map", "0:v", "-map", "1:a", "-map", "2:s",
                        "-c:v", "libx265", "-preset", "ultrafast", "-pix_fmt", "yuv420p10le",
                        "-x265-params", "keyint=50:log-level=error", "-c:a", "eac3", "-c:s", "ass",
                        "-attach", str(root / "cover.jpg"), "-metadata:s:t:0", "mimetype=image/jpeg",
                        "-metadata:s:t:0", "filename=cover.jpg", str(root / "ep.mkv")], check=True)
        cls.mkv = (root / "ep.mkv").read_bytes()

    @classmethod
    def tearDownClass(cls):
        cls.media.cleanup()

    def setUp(self):
        super().setUp()
        lib_base.touch(self.root / "剧" / "剧 S01E01.mkv", self.mkv)
        self.handler.files["剧集/剧 S01E01.mkv"] = self.mkv
        self.relay.ffmpeg = FFMPEG
        self.relay.watch.ffmpeg = FFMPEG
        self.relay.watch.popen = subprocess.Popen
        self.ids = self.add_both()

    def play(self, iid):
        answer = self.watch(library=iid, caps={"hevc": True, "hevc10": True})
        self.assertEqual((answer["ok"], answer["mode"]), (True, "hls"), answer)
        base = "/tv/" + answer["url"]
        for _ in range(60):
            status, _, master = self.request("GET", base + "master.m3u8")
            if status == 200:
                break
        self.assertEqual(status, 200, master)
        _, _, init = self.request("GET", base + "init.mp4")
        _, _, playlist = self.request("GET", base + "index.m3u8")
        return answer, master.decode(), init, playlist.decode(), base

    def test_local_mkv_is_remuxed_to_hvc1_and_aac(self):
        iid = self.ids["剧/剧 S01E01.mkv"]
        answer, master, init, playlist, base = self.play(iid)
        self.assertRegex(master, r'CODECS="hvc1\.2\.4\.L\d+\.[0-9A-F.]+,mp4a\.40\.2"')
        self.assertIn(f"#EXT-X-RTV-ORIGIN:{watch.EXACT_TS_OFFSET:.3f}", playlist)
        self.assertEqual(answer["duration"], 6)
        media = self.relay.library.item(iid)["media"]
        self.assertEqual([x["c"] for x in media["streams"]], ["hevc", "eac3", "ass", "mjpeg"], "probed once, cached")
        status, _, segment = self.request("GET", base + "0.m4s")
        self.assertEqual(status, 200)
        self.assertIn(b"moof", segment[:64])
        self.assertEqual([x["label"] for x in answer["subs"]], ["字幕 1"])
        status, response, vtt = self.request("GET", f"/tv/api/library/subs/{iid}/0.vtt", "alice")
        self.assertEqual((status, response.getheader("X-RTV-Partial")), (200, "0"))
        self.assertEqual([c[2] for c in library.parse_vtt(vtt.decode())][:3], ["第0秒", "第1秒", "第2秒"])

    def test_webdav_subtitles_are_extracted_while_streaming(self):
        iid = self.ids["剧集/剧 S01E01.mkv"]
        answer, master, init, playlist, base = self.play(iid)
        self.assertTrue(answer["subs"][0]["live"])
        stream = next(s for s in self.relay.watch.streams() if s.sid == answer["sid"])
        args = stream._proc.args
        self.assertNotIn(lib_base.PASSWORD, " ".join(args))
        self.assertNotIn(self.url, " ".join(args), "ffmpeg never sees the WebDAV address")
        cues = []
        for seq in range(6):
            self.request("GET", base + f"{seq}.m4s")
            status, response, vtt = self.request("GET", f"/tv/api/library/subs/{iid}/0.vtt", "alice")
            self.assertEqual((status, response.getheader("X-RTV-Partial")), (200, "1"))
            cues = library.parse_vtt(vtt.decode())
            if len(cues) >= 3:
                break
            time.sleep(0.3)
        self.assertGreaterEqual(len(cues), 3, cues)
        self.assertEqual(cues[0], (0.0, 1.5, "第0秒"), "times are the file's own, not shifted")


if __name__ == "__main__":
    unittest.main()
