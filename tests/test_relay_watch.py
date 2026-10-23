"""Watching on the PC (server/relay/watch.py): the console plays the source of what a TV is showing.

What is checked: the stream URL's token is bound to the device and the stream
and expires; only the device's owner and members get a URL (not a guest, not
another user); the feature exists only on a self-hosted relay (--selfhost) --
the cloud relay answers 404 and the console gets no button; the source's
address and the headers it needs (User-Agent, Referer) reach ffmpeg, which never
reads local files; nobody watching for IDLE_S seconds stops ffmpeg; and the
fragmented MP4 that ffmpeg writes is cut into HLS segments with the right
durations and CODECS. The last part also runs a real ffmpeg on a few seconds of
generated video when ffmpeg is installed.
"""

from __future__ import annotations

import http.client
import io
import json
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from server import now_playing
from server.relay import bilibili, setup, watch, web
from server.relay.state import Video
from server.tenancy import files as device_files
from server.tenancy.accounts import Accounts

from test_relay_web import KEY, FakeClient, FakeYouTube

ALICE_DEVICE = "aaaaaaaaaaa1"
BOB_DEVICE = "bbbbbbbbbbb2"
SECRET = "s" * 43
FFMPEG = shutil.which("ffmpeg")


# ---------------------------------------------------------------- a fragmented MP4, by hand

def box(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I4s", 8 + len(payload), kind) + payload


def full(kind: bytes, payload: bytes, flags: int = 0) -> bytes:
    return box(kind, b"\0" + flags.to_bytes(3, "big") + payload)


def moov(timescale: int = 90000, video: bool = True) -> bytes:
    tkhd = full(b"tkhd", struct.pack(">III", 0, 0, 1) + bytes(68))
    mdhd = full(b"mdhd", struct.pack(">III", 0, 0, timescale) + bytes(8))
    hdlr = full(b"hdlr", struct.pack(">I", 0) + (b"vide" if video else b"soun") + bytes(12))
    if video:
        entry = box(b"avc1", bytes(78) + box(b"avcC", bytes([1, 0x64, 0x00, 0x1F, 0xFF])))
    else:
        # esds: ES_Descriptor(3) > DecoderConfigDescriptor(4) > DecoderSpecificInfo(5) = HE-AAC (5).
        dsi = bytes([0x05, 2, 5 << 3 | 1, 0x90])
        dcd = bytes([0x04, 13 + len(dsi), 0x40, 0x15]) + bytes(11) + dsi
        entry = box(b"mp4a", bytes(28) + full(b"esds", bytes([0x03, 3 + len(dcd), 0, 1, 0]) + dcd))
    stsd = full(b"stsd", struct.pack(">I", 1) + entry)
    trak = box(b"trak", tkhd + box(b"mdia", mdhd + hdlr + box(b"minf", box(b"stbl", stsd))))
    return box(b"moov", trak + box(b"mvex", full(b"trex", struct.pack(">IIIII", 1, 1, 0, 0, 0))))


def fragment(seconds: float, timescale: int = 90000, samples: int = 50, per_sample: bool = False) -> bytes:
    step = int(timescale * seconds / samples)
    if per_sample:
        traf = full(b"tfhd", struct.pack(">I", 1)) + \
            full(b"trun", struct.pack(">I", samples) + b"".join(struct.pack(">II", step, 10) for _ in range(samples)),
                 flags=0x300)
    else:
        traf = full(b"tfhd", struct.pack(">II", 1, step), flags=0x08) + full(b"trun", struct.pack(">I", samples))
    return box(b"moof", box(b"traf", traf)) + box(b"mdat", b"x" * 64)


FTYP = box(b"ftyp", b"isom\0\0\2\0isomiso6")
INPUT_DUMP = ["Input #0, hls, from 'https://x/1.m3u8':",
              "  Stream #0:0[0x100]: Video: h264 (High) ([27][0][0][0] / 0x001B), yuv420p, 1280x720",
              "  Stream #0:1[0x101](und): Audio: aac (LC) ([15][0][0][0] / 0x000F), 48000 Hz, stereo",
              "Stream mapping:"]


class FakeProc:
    """ffmpeg: prints the input dump, writes stdout, then (hold) keeps running until killed."""

    def __init__(self, args, lines=None, data=b"", hold=True, code=0):
        self.args = args
        self.stderr = io.BytesIO(("\n".join(INPUT_DUMP if lines is None else lines) + "\n").encode())
        self.killed = threading.Event()
        self.hold = hold
        self.code = code
        self.stdin = io.BytesIO()
        proc = self

        class Out(io.RawIOBase):
            buffer = io.BytesIO(data)

            def readable(self):
                return True

            def readinto(self, target):
                chunk = self.buffer.read(len(target))
                if not chunk and proc.hold:
                    proc.killed.wait(10)
                target[:len(chunk)] = chunk
                return len(chunk)

        self.stdout = io.BufferedReader(Out())

    def kill(self):
        self.killed.set()

    def wait(self, timeout=None):
        if self.hold:
            self.killed.wait(10)
        return -9 if self.killed.is_set() else self.code


class Popen:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.procs: list[FakeProc] = []

    def __call__(self, args, **_kwargs):
        proc = FakeProc(args, **self.kwargs)
        self.procs.append(proc)
        return proc


LIVE_DATA = FTYP + moov() + fragment(2.0) + fragment(2.5, per_sample=True)


# ---------------------------------------------------------------- pure parts

class FfmpegArgsTests(unittest.TestCase):
    def test_live_source_gets_its_user_agent_and_only_network_protocols(self):
        source = watch.Source(("live", "ch000"), url="https://x/1.m3u8", user_agent="AptvPlayer-UA")
        args = watch.ffmpeg_args("ffmpeg", source, source.url, watch.FIRST_MODE)
        self.assertEqual(args[args.index("-i") + 1], "https://x/1.m3u8")
        self.assertEqual(args[args.index("-user_agent") + 1], "AptvPlayer-UA")
        self.assertEqual(args[args.index("-protocol_whitelist") + 1], "http,https,tls,tcp,crypto,httpproxy")
        self.assertNotIn("file", args[args.index("-protocol_whitelist") + 1].split(","))
        self.assertIn("-nostdin", args)
        self.assertIn("-re", args)
        self.assertNotIn("-ss", args)
        self.assertNotIn("-headers", args)
        self.assertEqual(args[-3:], ["-f", "mp4", "pipe:1"], "the output is a pipe to the relay, never a file")
        self.assertEqual(args[args.index("-c:v") + 1], "copy")
        self.assertEqual(args[args.index("-c:a") + 1], "aac", "ADTS audio cannot be copied into fMP4's header")

    def test_bilibili_source_gets_referer_offset_and_pacing(self):
        source = watch.Source(("video", "up01", "bilibili", "BV1"), url=lambda: "never called here",
                              user_agent=bilibili.UA, referer=bilibili.REFERER, offset=42.7)
        args = watch.ffmpeg_args("ffmpeg", source, "https://media.example/BV1.mp4", watch.FIRST_MODE)
        self.assertEqual(args[args.index("-headers") + 1], f"Referer: {bilibili.REFERER}\r\n")
        self.assertEqual(args[args.index("-user_agent") + 1], bilibili.UA)
        self.assertEqual(args[args.index("-ss") + 1], "42")
        self.assertLess(args.index("-ss"), args.index("-i"), "input seeking")
        self.assertIn("-re", args)

    def test_piped_source_reads_stdin_only(self):
        source = watch.Source(("video", "cast", "youtube", "y"), opener=lambda: None, paced=False)
        args = watch.ffmpeg_args("ffmpeg", source, "", watch.FIRST_MODE)
        self.assertEqual(args[args.index("-i") + 1], "pipe:0")
        self.assertEqual(args[args.index("-protocol_whitelist") + 1], "pipe")
        self.assertNotIn("-nostdin", args)
        self.assertNotIn("-re", args)

    def test_transcoding_and_audio_only_modes(self):
        source = watch.Source(("live", "c"), url="http://x/a")
        args = watch.ffmpeg_args("ffmpeg", source, "http://x/a", watch.Mode("encode", "encode"))
        self.assertEqual(args[args.index("-c:v") + 1], "libx264")
        self.assertEqual(args[args.index("-c:a") + 1], "aac")
        self.assertIn("frag_keyframe", args[args.index("-movflags") + 1])
        args = watch.ffmpeg_args("ffmpeg", source, "http://x/a", watch.Mode(None, "encode"))
        self.assertNotIn("-c:v", args)
        self.assertNotIn("frag_keyframe", args[args.index("-movflags") + 1])
        self.assertEqual(args[args.index("-frag_duration") + 1], str(watch.FRAGMENT_US))

    def test_codecs_from_the_input_dump_decide_copy_or_encode(self):
        self.assertEqual(watch.input_codecs(INPUT_DUMP), (True, "h264", "aac"))
        self.assertEqual(watch.input_codecs(INPUT_DUMP[:2]), (False, "h264", None))
        cover = ["  Stream #0:0: Audio: mp3, 44100 Hz", "  Stream #0:1: Video: mjpeg, (attached pic)",
                 "Output #0, mp4, to 'pipe:1':"]
        self.assertEqual(watch.input_codecs(cover), (True, None, "mp3"))
        self.assertEqual(watch.mode_for("h264", "aac"), watch.Mode("copy", "encode"))
        self.assertEqual(watch.mode_for("h264", "aac"), watch.FIRST_MODE, "the usual source starts only once")
        self.assertEqual(watch.mode_for("hevc", "mp2"), watch.Mode("encode", "encode"))
        self.assertEqual(watch.mode_for(None, "mp3"), watch.Mode(None, "encode"))
        self.assertIsNone(watch.mode_for(None, None))

    def test_live_source_lookup(self):
        with tempfile.TemporaryDirectory() as tmp:
            table = Path(tmp) / "channels.txt"
            table.write_text("# x\nch000 | CCTV1 | https://x/1.m3u8 | MyUA\nch001 | 二 | http://x/2.m3u8\n"
                             "ch002 | 本机 | file:///etc/passwd\n", encoding="utf-8")
            self.assertEqual(watch.live_source(table, "ch000"), ("https://x/1.m3u8", "MyUA"))
            self.assertEqual(watch.live_source(table, "ch001"), ("http://x/2.m3u8", "AptvPlayer-UA"))
            self.assertIsNone(watch.live_source(table, "ch002"), "only http(s) addresses")
            self.assertIsNone(watch.live_source(table, "ch009"))
            self.assertIsNone(watch.live_source(None, "ch000"))


class Mp4Tests(unittest.TestCase):
    def test_init_info_and_codecs(self):
        tracks = watch.init_info(moov())
        self.assertEqual(tracks[1]["kind"], "vide")
        self.assertEqual(tracks[1]["timescale"], 90000)
        self.assertEqual(watch.codecs_of(tracks), "avc1.64001f")
        self.assertEqual(watch.codecs_of(watch.init_info(moov(48000, video=False))), "mp4a.40.5")

    def test_fragment_durations(self):
        tracks = watch.init_info(moov())
        self.assertAlmostEqual(watch.fragment_duration(fragment(2.0)[:fragment(2.0).index(b"mdat") - 4], tracks), 2.0)
        frag = fragment(2.5, per_sample=True)
        self.assertAlmostEqual(watch.fragment_duration(frag[:frag.index(b"mdat") - 4], tracks), 2.5)

    def test_window_playlist(self):
        window = watch.Window(keep=2)
        for seconds in (2.0, 2.4, 6.2):
            window.add(seconds, b"seg")
        text = window.playlist()
        self.assertIn("#EXT-X-MEDIA-SEQUENCE:1\n", text)
        self.assertIn("#EXT-X-TARGETDURATION:7\n", text)
        self.assertIn('#EXT-X-MAP:URI="init.mp4"', text)
        self.assertEqual([line for line in text.splitlines() if line.endswith(".m4s")], ["1.m4s", "2.m4s"])
        self.assertNotIn("ENDLIST", text)
        self.assertIsNone(window.segment(0))
        window.ended = True
        self.assertTrue(window.playlist().endswith("#EXT-X-ENDLIST\n"))
        self.assertIn('CODECS="avc1.640028,mp4a.40.2"', window.master())


# ---------------------------------------------------------------- streams and the service

class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.t = 100.0
        self.logs = []
        self.popen = Popen(data=LIVE_DATA)
        self.service = watch.WatchService("ffmpeg", watch.derive_secret(KEY), log=self.logs.append,
                                          clock=lambda: self.t, popen=self.popen, background=False)
        self.source = watch.Source(("live", "ch000", "https://x/1.m3u8", "UA"), url="https://x/1.m3u8", user_agent="UA")

    def tearDown(self):
        self.service.stop_all()

    def test_segments_reach_the_window(self):
        stream = self.service.open(ALICE_DEVICE, self.source)
        self.assertTrue(stream.wait_ready(5))
        deadline = time.monotonic() + 5
        while stream.window.next_seq < 2 and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual([round(d, 2) for _, d, _ in stream.window.segments], [2.0, 2.5])
        self.assertEqual(stream.window.codecs, "avc1.64001f")
        self.assertTrue(stream.window.init.startswith(FTYP))
        self.assertEqual(stream.mode, watch.FIRST_MODE)
        self.assertEqual(len(self.popen.procs), 1)

    def test_same_source_is_shared_and_a_new_one_replaces_it(self):
        first = self.service.open(ALICE_DEVICE, self.source)
        self.assertIs(self.service.open(ALICE_DEVICE, self.source), first, "a second viewer joins the same stream")
        self.assertTrue(first.wait_ready(5))
        other = watch.Source(("live", "ch001", "https://x/2.m3u8", "UA"), url="https://x/2.m3u8")
        second = self.service.open(ALICE_DEVICE, other)
        self.assertIsNot(second, first)
        self.assertTrue(first.stopped)
        self.assertTrue(self.popen.procs[0].killed.is_set(), "the old ffmpeg is gone")
        self.assertEqual(len(self.service.streams()), 1)

    def test_idle_streams_stop(self):
        stream = self.service.open(ALICE_DEVICE, self.source)
        self.assertTrue(stream.wait_ready(5))
        self.t += watch.IDLE_S - 1
        stream.touch()                      # the player fetched the playlist
        self.t += watch.IDLE_S - 1
        self.service.reap()
        self.assertFalse(stream.stopped, "fetched within IDLE_S")
        self.t += 2
        self.service.reap()
        self.assertTrue(stream.stopped)
        self.assertTrue(self.popen.procs[0].killed.is_set())
        self.assertEqual(self.service.streams(), [])
        self.assertTrue(any("没人看" in m for m in self.logs))

    def test_limit_on_streams(self):
        self.service.max_streams = 1
        self.service.open(ALICE_DEVICE, self.source)
        with self.assertRaises(watch.Busy):
            self.service.open(BOB_DEVICE, self.source)

    def test_non_h264_source_restarts_once_transcoding(self):
        lines = ["  Stream #0:0: Video: hevc (Main), yuv420p", "  Stream #0:1: Audio: mp2, 48000 Hz", "Stream mapping:"]
        self.service.popen = Popen(lines=lines, data=LIVE_DATA)
        stream = self.service.open(ALICE_DEVICE, self.source)
        self.assertTrue(stream.wait_ready(5))
        procs = self.service.popen.procs
        self.assertEqual(len(procs), 2)
        self.assertTrue(procs[0].killed.is_set())
        self.assertEqual(procs[1].args[procs[1].args.index("-c:v") + 1], "libx264")
        self.assertEqual(procs[1].args[procs[1].args.index("-c:a") + 1], "aac")

    def test_failure_reason_without_the_address(self):
        lines = ["[http @ 0x1234abcd] HTTP error 403 Forbidden for https://x/1.m3u8?token=abc", "Conversion failed!"]
        self.service.popen = Popen(lines=lines, hold=False, code=1)
        stream = self.service.open(ALICE_DEVICE, self.source)
        self.assertFalse(stream.wait_ready(5))
        stream._thread.join(5)
        self.assertEqual(stream.error, "HTTP error 403 Forbidden for <地址>")
        self.assertFalse(stream.alive)

    def test_tokens(self):
        stream = self.service.open(ALICE_DEVICE, self.source)
        token = self.service.token(stream)
        self.assertIs(self.service.find(stream.sid, token), stream)
        self.assertIsNone(self.service.find(stream.sid, token[:-1] + ("A" if token[-1] != "A" else "B")))
        self.assertIsNone(self.service.find("0" * 16, token))
        other = watch.WatchService("ffmpeg", watch.derive_secret("another-key-0123456789"))
        self.assertIsNone(other.find(stream.sid, token))
        listen_token = watch.listen_audio.issue_token(watch.listen_audio.derive_secret(KEY),
                                                      f"{ALICE_DEVICE}/{stream.sid}")
        self.assertIsNone(self.service.find(stream.sid, listen_token), "a listening token is not a watching token")
        self.service.wall = lambda: time.time() + watch.TOKEN_LIFETIME_S + 5
        self.assertIsNone(self.service.find(stream.sid, token), "expired")


# ---------------------------------------------------------------- over HTTP, in the relay

class RelayWatchTests(unittest.TestCase):
    selfhost = True

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.now = time.time()
        self.accounts = Accounts(root / "tv.db")
        self.admin = self.accounts.create_user("管理员", "admin")
        self.alice = self.accounts.create_user("Alice")
        self.bob = self.accounts.create_user("Bob")
        for device, user in ((ALICE_DEVICE, self.alice), (BOB_DEVICE, self.bob)):
            row = self.accounts.register_device(device, "xingzhi-cube-154", SECRET)
            self.accounts.bind(row["claim_code"], user)
        self.devices_dir = root / "devices"
        main = root / "channels.txt"
        main.write_text("ch000 | CCTV1 | https://x/1.m3u8 | MyUA\nch001 | 湖南卫视 | https://x/2.m3u8\n", encoding="utf-8")
        self.client, self.youtube = FakeClient(), FakeYouTube()
        self.relay = web.Relay(None, self.client, None, "/opt/ff", KEY, youtube=self.youtube, main_channels=main,
                               accounts=self.accounts, devices_dir=self.devices_dir, log=lambda _m: None,
                               clock=lambda: self.now, selfhost=setup.SelfHost() if self.selfhost else None)
        self.relay.sync_devices(force=True)
        if self.relay.watch is not None:
            self.popen = Popen(data=LIVE_DATA)
            self.relay.watch.popen = self.popen
        self.streams, self.manage = web.make_servers(self.relay, 0, 0)
        self.streams.server_close()
        threading.Thread(target=self.manage.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.cookies = {name: "tvs=" + self.accounts.new_session(uid, "pc") for name, uid in
                        (("alice", self.alice), ("bob", self.bob))}
        self.cookies["guest"] = "tvs=" + self.accounts.new_session(None, "guest", device_id=ALICE_DEVICE)
        self.online(ALICE_DEVICE, "ch000")

    def tearDown(self):
        if self.relay.watch is not None:
            self.relay.watch.stop_all()
        self.manage.shutdown()
        self.manage.server_close()
        self.accounts.close()
        self.tmp.cleanup()

    def online(self, device, channel):
        now_playing.write(device_files.device_files(self.devices_dir, device).now, channel, True, True,
                          now=self.now, since=self.now)

    def request(self, method, path, who=None, body=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.manage.server_address[1], timeout=20)
        headers = {"Cookie": self.cookies[who]} if who else {}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        connection.request(method, path, body=data, headers=headers)
        response = connection.getresponse()
        raw = response.read()
        connection.close()
        return response.status, response.getheader("Content-Type") or "", raw

    def start(self, who="alice", device=ALICE_DEVICE):
        status, _, raw = self.request("POST", "/tv/api/watch", who, {"device": device})
        return status, (json.loads(raw) if status == 200 else None)

    def test_owner_gets_a_url_that_plays(self):
        status, answer = self.start()
        self.assertEqual(status, 200)
        self.assertTrue(answer["ok"], answer)
        self.assertEqual((answer["channel"], answer["kind"], answer["title"]), ("ch000", "live", "CCTV1"))
        self.assertRegex(answer["url"], r"^watch/[0-9a-f]{16}/\d+\.[A-Za-z0-9_-]+/$")
        base = "/tv/" + answer["url"]
        status, kind, master = self.request("GET", base + "master.m3u8")
        self.assertEqual((status, kind), (200, "application/vnd.apple.mpegurl"))
        self.assertIn(b'CODECS="avc1.64001f"', master)
        status, _, playlist = self.request("GET", base + "index.m3u8")
        self.assertEqual(status, 200)
        self.assertIn(b"0.m4s", playlist)
        status, kind, init = self.request("GET", base + "init.mp4")
        self.assertEqual((status, kind), (200, "video/mp4"))
        self.assertTrue(init.startswith(FTYP))
        self.assertEqual(self.request("GET", base + "0.m4s")[0], 200)
        self.assertEqual(self.request("GET", base + "99.m4s")[0], 404)
        args = self.popen.procs[0].args
        self.assertEqual(args[args.index("-i") + 1], "https://x/1.m3u8")
        self.assertEqual(args[args.index("-user_agent") + 1], "MyUA")
        # A second request for the same channel joins the same stream.
        self.assertEqual(self.start()[1]["sid"], answer["sid"])
        self.assertEqual(len(self.popen.procs), 1)

    def test_token_is_required_and_bound(self):
        answer = self.start()[1]
        sid, token = answer["url"].split("/")[1:3]
        self.assertEqual(self.request("GET", f"/tv/watch/{sid}/{token}/index.m3u8")[0], 200)
        expires, _, mac = token.partition(".")
        forged = f"{int(expires) + 3600}.{mac}"
        for path in (f"/tv/watch/{sid}/{forged}/index.m3u8", f"/tv/watch/{'1' * 16}/{token}/index.m3u8",
                     f"/tv/watch/{sid}/{token}/../../api/state", f"/tv/watch/{sid}/index.m3u8"):
            self.assertEqual(self.request("GET", path)[0], 404, path)
        # A signed-in cookie is no substitute for the token.
        self.assertEqual(self.request("GET", f"/tv/watch/{sid}/x.y/index.m3u8", "alice")[0], 404)
        self.relay.watch.wall = lambda: time.time() + watch.TOKEN_LIFETIME_S + 60
        self.assertEqual(self.request("GET", f"/tv/watch/{sid}/{token}/index.m3u8")[0], 404, "expired")

    def test_only_owner_and_members(self):
        self.assertEqual(self.start("bob")[0], 404, "someone else's device does not exist for Bob")
        self.assertEqual(self.request("POST", "/tv/api/watch", None, {"device": ALICE_DEVICE})[0], 404)
        status, answer = self.start("guest")
        self.assertEqual(status, 200)
        self.assertFalse(answer["ok"])
        self.assertIn("访客", answer["error"])
        state = json.loads(self.request("GET", "/tv/api/state", "guest")[2])
        self.assertNotIn("watch", state, "no button for a guest")
        self.assertEqual(self.popen.procs, [])

    def test_offline_device_and_unknown_channel(self):
        answer = self.start("bob", BOB_DEVICE)[1]
        self.assertFalse(answer["ok"])
        self.assertIn("没在线", answer["error"])
        self.online(BOB_DEVICE, "ch404")
        self.assertIn("地址", self.start("bob", BOB_DEVICE)[1]["error"])

    def test_bilibili_video_starts_where_the_tv_is(self):
        self.online(ALICE_DEVICE, "up01")
        video = Video(bvid="BV1xx411c7mD", title="视频标题", duration=600, pubdate=0)
        dev = self.relay.find_device(ALICE_DEVICE, web.Caller(user=self.alice, role="user"))
        self.relay._paused[dev["slot"]] = {"cid": "up01", "video": video, "position": 75.4, "at": self.now}
        source = self.relay._watch_source(dev, "up01")
        self.assertEqual(source.key, ("video", "up01", "bilibili", "BV1xx411c7mD"))
        self.assertEqual(source.url(), "https://media.example/BV1xx411c7mD.mp4")
        args = watch.ffmpeg_args("ffmpeg", source, source.url(), watch.FIRST_MODE)
        self.assertEqual(args[args.index("-headers") + 1], "Referer: https://www.bilibili.com/\r\n")
        self.assertEqual(args[args.index("-user_agent") + 1], bilibili.UA)
        self.assertEqual(args[args.index("-ss") + 1], "75")
        # YouTube goes through the node, piped into ffmpeg.
        video = Video(bvid="ytvideo0001", title="y1", duration=600, pubdate=0, site="youtube")
        self.relay._paused[dev["slot"]]["video"] = video
        source = self.relay._watch_source(dev, "up01")
        self.assertIsNotNone(source.opener)
        source.opener()
        self.assertEqual(self.youtube.opened, [("ytvideo0001", 75.4)])

    def test_state_shows_the_button(self):
        state = json.loads(self.request("GET", "/tv/api/state", "alice")[2])
        self.assertIs(state.get("watch"), True)

    def test_console_allows_media(self):
        status, _, _ = self.request("GET", "/tv/console")
        self.assertEqual(status, 200)
        connection = http.client.HTTPConnection("127.0.0.1", self.manage.server_address[1], timeout=5)
        connection.request("GET", "/tv/console")
        response = connection.getresponse()
        policy = response.getheader("Content-Security-Policy")
        response.read()
        connection.close()
        self.assertIn("media-src 'self' blob:", policy)


class CloudRelayTests(RelayWatchTests):
    """The cloud relay (no --selfhost): the feature does not exist."""
    selfhost = False

    def test_owner_gets_a_url_that_plays(self):
        self.assertIsNone(self.relay.watch)
        self.assertEqual(self.start()[0], 404)
        self.assertEqual(self.request("GET", f"/tv/watch/{'1' * 16}/1.abcdefghijklmnopqrstuv/index.m3u8")[0], 404)

    def test_state_shows_the_button(self):
        state = json.loads(self.request("GET", "/tv/api/state", "alice")[2])
        self.assertNotIn("watch", state)

    def test_token_is_required_and_bound(self):
        pass

    def test_only_owner_and_members(self):
        self.assertEqual(self.start("guest")[0], 404)

    def test_offline_device_and_unknown_channel(self):
        self.assertEqual(self.start("bob", BOB_DEVICE)[0], 404)


# ---------------------------------------------------------------- with a real ffmpeg

@unittest.skipUnless(FFMPEG, "ffmpeg is not installed")
class RealFfmpegTests(unittest.TestCase):
    """A few seconds of tiny generated media, served over local HTTP like a channel."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        common = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                  "-i", "testsrc2=size=160x90:rate=25:duration=4", "-f", "lavfi", "-i", "sine=frequency=440:duration=4"]
        subprocess.run(common + ["-c:v", "libx264", "-preset", "ultrafast", "-g", "25", "-pix_fmt", "yuv420p",
                                 "-c:a", "aac", "-b:a", "48k", "-shortest",
                                 # moov first: the test server (http.server) cannot answer range requests
                                 "-movflags", "+faststart", str(root / "h264.mp4")], check=True)
        subprocess.run(common + ["-c:v", "mpeg2video", "-c:a", "mp2", "-shortest", "-f", "mpegts",
                                 str(root / "mpeg2.ts")], check=True)
        subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-i", str(root / "h264.mp4"),
                        "-c", "copy", "-f", "mpegts", str(root / "h264.ts")], check=True)
        cls.server = _QuietServer(("127.0.0.1", 0), partial(_Quiet, directory=str(root)))
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}/"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    def play(self, name):
        source = watch.Source(("live", name), url=self.base + name, user_agent="UA-test", paced=False)
        stream = watch.WatchStream("0" * 16, "dev", source, FFMPEG, log=lambda _m: None)
        stream.start()
        stream._thread.join(60)
        return stream

    def test_h264_is_copied(self):
        stream = self.play("h264.mp4")
        self.assertEqual(stream.error, "")
        self.assertEqual(stream.mode, watch.FIRST_MODE)
        self.assertTrue(stream.window.ended)
        self.assertTrue(stream.window.codecs.startswith("avc1."))
        self.assertAlmostEqual(sum(d for _, d, _ in stream.window.segments), 4.0, delta=0.3)

    def test_aac_from_mpegts_has_its_config_in_the_init_segment(self):
        # A live source's AAC has ADTS headers; copied, the empty moov would have no AudioSpecificConfig
        # and the browser could not decode it (seen in Chrome). Audio is always encoded.
        stream = self.play("h264.ts")
        self.assertEqual(stream.error, "")
        self.assertEqual(stream.mode, watch.FIRST_MODE)
        init = stream.window.init
        at = init.find(b"esds")
        size = struct.unpack(">I", init[at - 4:at])[0]
        self.assertEqual(watch._aac_object_type(init[at + 4:at - 4 + size]), 2)

    def test_mpeg2_is_transcoded(self):
        stream = self.play("mpeg2.ts")
        self.assertEqual(stream.error, "")
        self.assertEqual(stream.mode, watch.Mode("encode", "encode"))
        self.assertTrue(stream.window.codecs.startswith("avc1.") and stream.window.codecs.endswith("mp4a.40.2"))
        self.assertGreater(len(stream.window.segments), 1)


class _Quiet(SimpleHTTPRequestHandler):
    def log_message(self, *_args):
        pass


class _QuietServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        pass        # ffmpeg hangs up when it has read enough


if __name__ == "__main__":
    unittest.main()
