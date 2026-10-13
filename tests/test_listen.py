"""Listening on the phone: the schedule the device follows, the HLS window, the tap, tokens, HTTP."""

import collections
import json
import os
import shutil
import struct
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server import adpcm, listen


def adts(payload: bytes) -> bytes:
    """A minimal ADTS frame around `payload` (header fields other than length are fixed)."""
    length = 7 + len(payload)
    header = bytes([0xFF, 0xF1, 0x60, 0x40 | ((length >> 11) & 0x03),
                    (length >> 3) & 0xFF, ((length & 0x07) << 5) | 0x1F, 0xFC])
    return header + payload


class FakeClock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


class FakeEncoder:
    def __init__(self, on_frames):
        self.on_frames = on_frames
        self.written = []
        self.closed = False

    def write(self, pcm):
        self.written.append(pcm)

    def close(self):
        self.closed = True


class FakeChannel:
    def __init__(self, count=0):
        self.lock = threading.Lock()
        self.audio = collections.deque()
        self.audio_content = collections.deque()
        self.next_key = 0
        for _ in range(count):
            self.push()

    def push(self):
        key, self.next_key = self.next_key, self.next_key + 40
        self.audio.append(struct.pack("<640h", *([key % 1000] * 640)))
        self.audio_content.append(key)

    def pop(self):
        self.audio.popleft()
        return self.audio_content.popleft()


class AdtsTests(unittest.TestCase):
    def test_splits_whole_frames_and_keeps_the_rest(self):
        one, two = adts(b"a" * 10), adts(b"b" * 300)
        buffer = bytearray(one + two + two[:5])
        self.assertEqual(listen.split_adts(buffer), [one, two])
        self.assertEqual(bytes(buffer), two[:5])
        buffer.extend(two[5:])
        self.assertEqual(listen.split_adts(buffer), [two])
        self.assertEqual(buffer, bytearray())

    def test_skips_garbage_to_the_next_sync_word(self):
        frame = adts(b"x" * 20)
        buffer = bytearray(b"\x00\x12junk" + frame)
        self.assertEqual(listen.split_adts(buffer), [frame])


class Id3Tests(unittest.TestCase):
    def test_priv_timestamp_and_pdt(self):
        tag = listen.id3_tag((1 << 33) + 90000, 1791789882105)
        self.assertEqual(tag[:6], b"ID3\x04\x00\x00")
        size = sum(b << (7 * (3 - n)) for n, b in enumerate(tag[6:10]))
        self.assertEqual(size, len(tag) - 10)
        owner = b"com.apple.streaming.transportStreamTimestamp\x00"
        at = tag.index(owner) + len(owner)
        self.assertEqual(struct.unpack(">Q", tag[at:at + 8])[0], 90000)  # wrapped to 33 bits
        self.assertIn(b"TXXX", tag)
        self.assertTrue(tag.endswith(b"\x03pdt\x001791789882105"))


class LedgerTests(unittest.TestCase):
    def test_due_counts_real_time(self):
        ledger = listen.Ledger(lead_s=5.0, start=100.0)
        self.assertEqual(ledger.due(100.0), 1)
        ledger.append(None)
        self.assertEqual(ledger.due(100.039), 0)
        self.assertEqual(ledger.due(100.2), 5)

    def test_early_device_gets_filler_late_device_skips(self):
        ledger = listen.Ledger(lead_s=5.0, start=0.0)
        for key in range(10):
            ledger.append(key)
        self.assertEqual(ledger.for_device(4.0), ("filler", None))      # 1 s early
        kind, entry = ledger.for_device(5.0)
        self.assertEqual((kind, entry.key), ("entry", 0))
        kind, entry = ledger.for_device(5.2)                            # 160 ms late
        self.assertEqual((kind, entry.key), ("entry", 5))
        self.assertEqual(ledger.skipped, 4)

    def test_a_device_at_any_phase_settles_without_oscillating(self):
        for phase in (0.0, 0.013, 0.021, 0.037):
            ledger = listen.Ledger(lead_s=6.0, start=0.0)
            for _ in range(300):          # within the ledger's history bound
                ledger.append(None)
            play_at, actions = 0.5 + phase, []
            for _ in range(400):
                kind, entry = ledger.for_device(play_at)
                if kind == "entry":
                    actions.append(play_at - ledger.target(entry))
                else:
                    actions.append(None)
                play_at += listen.CHUNK_S
            tail = actions[-100:]
            self.assertNotIn(None, tail, phase)
            self.assertTrue(all(abs(e) <= listen.DEFAULT_TOLERANCE_S for e in tail), phase)

    def test_keeps_a_bounded_history(self):
        ledger = listen.Ledger(lead_s=1.0, start=0.0, keep=10)
        for key in range(50):
            ledger.append(key)
        self.assertEqual(len(ledger.entries), 10)
        self.assertEqual(ledger.entries[0].key, 40)


class WindowTests(unittest.TestCase):
    def test_segments_dates_and_playlist(self):
        window = listen.HlsWindow(lambda sample: 1_000_000 + sample / listen.RATE, window=3,
                                  start_offset_s=4.5)
        frames = [adts(bytes([n % 256]) * 8) for n in range(16 * 5 + 3)]
        window.add(frames)
        self.assertEqual([s.seq for s in window.segments], [2, 3, 4])
        last = window.segments[-1]
        self.assertEqual(last.first_frame, 64)
        # The first output frame is the encoder's priming, so frame 64 starts at input sample 64*1024-1024.
        self.assertAlmostEqual(last.pdt, 1_000_000 + (64 * 1024 - 1024) / 16000)
        self.assertTrue(last.data.startswith(b"ID3"))
        self.assertTrue(last.data.endswith(frames[16 * 5 - 1]))
        text = window.playlist("t=abc")
        self.assertIn("#EXT-X-MEDIA-SEQUENCE:2\n", text)
        self.assertIn("#EXT-X-START:TIME-OFFSET=-4.500,PRECISE=YES", text)
        self.assertIn("#EXTINF:1.024,\n4.aac?t=abc\n", text)
        self.assertEqual(text.count("#EXT-X-PROGRAM-DATE-TIME:"), 3)
        self.assertIsNone(window.segment(1))
        self.assertEqual(window.segment(4), last.data)

    def test_dates_are_utc_with_milliseconds(self):
        self.assertEqual(listen.iso_ms(0.5), "1970-01-01T00:00:00.500Z")


class TapTests(unittest.TestCase):
    def test_waits_for_the_cushion_then_reads_ahead_without_popping(self):
        channel = FakeChannel(3)
        tap = listen.ChannelTap(channel, cushion_s=0.2)          # five chunks
        self.assertIsNone(tap.take(0.0))
        channel.push(); channel.push()
        keys = [tap.take(0.0)[0] for _ in range(4)]
        self.assertEqual(keys, [0, 40, 80, 120])
        self.assertEqual(len(channel.audio), 5)                  # nothing popped
        channel.pop(); channel.pop()                              # the device's sender moves on
        self.assertEqual(tap.take(0.0)[0], 160)
        self.assertIsNone(tap.take(0.0))                          # caught up with the source

    def test_a_long_dry_spell_primes_again(self):
        channel = FakeChannel(2)
        tap = listen.ChannelTap(channel, cushion_s=0.04)
        self.assertIsNotNone(tap.take(0.0))
        self.assertIsNotNone(tap.take(0.0))
        self.assertIsNone(tap.take(1.0))
        self.assertIsNone(tap.take(1.6))
        self.assertFalse(tap.primed)

    def test_adpcm_blocks_become_pcm(self):
        pcm = struct.pack("<640h", *[(n * 37) % 2000 - 1000 for n in range(640)])
        block = adpcm.Encoder().encode_pcm(pcm)
        self.assertEqual(len(listen.pcm_of(block)), listen.CHUNK_PCM_BYTES)
        self.assertIs(listen.pcm_of(pcm), pcm)


class StreamTests(unittest.TestCase):
    def make(self, **kwargs):
        clock = FakeClock()
        stream = listen.ListenStream(None, lead_s=5.0, clock=clock, wall=lambda: 2_000_000.0,
                                     encoder_factory=FakeEncoder, prefill_s=1.0, **kwargs)
        return stream, clock

    def test_prefill_is_silence_then_content_in_real_time(self):
        stream, clock = self.make()
        self.assertEqual(stream.pump(None), 26)                  # one second back, plus now
        self.assertEqual(stream.encoder.written, [listen.SILENCE] * 26)
        channel = FakeChannel(10)
        tap = listen.ChannelTap(channel, cushion_s=0.0)
        clock.now += 0.2
        self.assertEqual(stream.pump(tap), 5)
        self.assertEqual([e.key for e in list(stream.ledger.entries)[-5:]], [0, 40, 80, 120, 160])
        self.assertEqual(stream.taken, 5)

    def test_segment_dates_are_when_the_device_plays_them(self):
        stream, clock = self.make()
        # Entry 0 is published at start = now - 1 s; the device plays it lead_s later.
        wall_start = 2_000_000.0 - 1.0
        stream.window.add([adts(b"\x00") for _ in range(32)])
        second = stream.window.segments[1]
        self.assertAlmostEqual(second.pdt, wall_start + 5.0 + (16 * 1024 - 1024) / 16000)

    def test_idle_after_no_playlist_fetches(self):
        stream, clock = self.make()
        clock.now += listen.IDLE_S - 1
        self.assertFalse(stream.idle())
        clock.now += 2
        self.assertTrue(stream.idle())
        stream.touch()
        self.assertFalse(stream.idle())


class TokenTests(unittest.TestCase):
    def test_issue_and_check(self):
        secret = b"k" * 32
        token = listen.issue_token(secret, "d0", now=1000)
        self.assertTrue(listen.check_token(secret, "d0", token, now=1000))
        self.assertFalse(listen.check_token(secret, "d1", token, now=1000))
        self.assertFalse(listen.check_token(secret, "d0", token, now=1000 + listen.TOKEN_LIFETIME_S + 1))
        tampered = token[:-1] + ("Q" if token[-1] != "Q" else "A")
        self.assertFalse(listen.check_token(secret, "d0", tampered, now=1000))
        self.assertFalse(listen.check_token(secret, "d0", None, now=1000))
        self.assertTrue(listen.check_token(None, "d0", None))


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.secret = b"s" * 32
        self.stream = listen.ListenStream(None, lead_s=5.0, encoder_factory=FakeEncoder)
        self.stream.window.add([adts(b"\x01" * 4) for _ in range(48)])
        handler = listen.make_handler(lambda d, start: self.stream if d == "d0" else None, self.secret,
                                      wall=lambda: 1234.5)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def get(self, path, method="GET"):
        request = urllib.request.Request(self.base + path, method=method, data=b"" if method == "POST" else None)
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, response.headers.get("Content-Type"), response.read()
        except urllib.error.HTTPError as error:
            return error.code, None, b""

    def test_routes(self):
        token = listen.issue_token(self.secret, "d0")
        self.assertEqual(json.loads(self.get("/listen/time")[2]), {"t": 1234500.0})
        self.assertEqual(self.get("/listen/d0/live.m3u8")[0], 404)
        code, kind, body = self.get(f"/tv/listen/d0/live.m3u8?t={token}")
        self.assertEqual((code, kind), (200, "application/vnd.apple.mpegurl"))
        self.assertIn(f"2.aac?t={token}", body.decode())
        code, kind, body = self.get(f"/listen/d0/2.aac?t={token}")
        self.assertEqual((code, kind), (200, "audio/aac"))
        self.assertTrue(body.startswith(b"ID3"))
        self.assertEqual(self.get(f"/listen/d0/9.aac?t={token}")[0], 404)
        self.assertEqual(self.get(f"/listen/d1/live.m3u8?t={token}")[0], 404)
        self.assertFalse(self.stream.idle())
        self.assertEqual(self.get(f"/listen/d0/bye?t={token}", "POST")[0], 200)
        self.assertTrue(self.stream.idle())


class ServiceTests(unittest.TestCase):
    """ListenService: one device at a time, a stream only while someone listens, keys per channel."""

    def make(self, channel=None, relay=True):
        clock = FakeClock()
        self.channel = channel
        logs = []
        service = listen.ListenService(b"k" * 32, "xingzhi", lambda: "ffmpeg", lambda: self.channel,
                                       is_relay=lambda c: relay, lead_s=5.0, logger=logs.append, clock=clock,
                                       wall=lambda: 2_000_000.0, encoder_factory=FakeEncoder, prefill_s=0.0)
        return service, clock, logs

    def test_only_the_bound_capable_device_gets_a_stream(self):
        service, _, _ = self.make()
        self.assertIsNone(service.streams("aaaaaaaaaaa1", True))          # nothing bound yet
        service.bind("aaaaaaaaaaa1", capable=False)                         # old firmware
        self.assertIsNone(service.streams("aaaaaaaaaaa1", True))
        service.bind("aaaaaaaaaaa1", capable=True)
        self.assertIsNone(service.streams("bbbbbbbbbbb2", True))           # another device
        self.assertIsNone(service.streams("aaaaaaaaaaa1", False))          # a segment does not start one
        self.assertFalse(service.active)
        stream = service.streams("aaaaaaaaaaa1", True)
        self.assertIsNotNone(stream)
        self.assertTrue(service.active)
        self.assertIs(service.streams("aaaaaaaaaaa1", True), stream)       # one per device, shared
        service.bind("bbbbbbbbbbb2", capable=True)                          # the media server moved on
        self.assertFalse(service.active)
        self.assertTrue(stream.encoder.closed)

    def test_bad_device_ids_are_never_bound(self):
        service, _, _ = self.make()
        service.bind("../etc", capable=True)
        self.assertEqual(service.device, "")
        self.assertFalse(service.capable)

    def test_idle_shutdown_closes_the_encoder(self):
        service, clock, logs = self.make()
        service.bind("d0", capable=True)
        stream = service.streams("d0", True)
        stream.touch("phone1")
        clock.now += listen.IDLE_S - 1
        service.pump_once()
        self.assertTrue(service.active)
        clock.now += 2
        service.pump_once()
        self.assertFalse(service.active)
        self.assertTrue(stream.encoder.closed)
        self.assertTrue(any("nobody listening" in line for line in logs))

    def test_one_phone_stopping_leaves_the_others_listening(self):
        service, clock, _ = self.make()
        service.bind("d0", capable=True)
        stream = service.streams("d0", True)
        stream.bye("")                       # the opening request's anonymous entry
        stream.touch("phone1")
        stream.touch("phone2")
        stream.bye("phone1")
        service.pump_once()
        self.assertTrue(service.active)
        self.assertEqual(stream.listeners, 1)
        stream.bye("phone2")
        service.pump_once()
        self.assertFalse(service.active)

    def test_keys_carry_the_channel_generation(self):
        first = FakeChannel(40)
        service, clock, _ = self.make(first)
        service.bind("d0", capable=True)
        stream = service.streams("d0", True)
        service.pump_once()
        gen = service.gen_of(first)
        self.assertIsNotNone(gen)
        keys = [e.key for e in stream.ledger.entries]
        self.assertEqual(keys[0], (gen, 0))
        # The channel is replaced (channel switch, source restart): a new generation, old keys stay old.
        self.channel = FakeChannel(40)
        clock.now += 0.04
        service.pump_once()
        self.assertIsNone(service.gen_of(first))
        self.assertEqual(service.gen_of(self.channel), gen + 1)

    def test_for_device_answers_filler_silence_or_chunk(self):
        channel = FakeChannel(40)
        service, clock, _ = self.make(channel)
        self.assertIsNone(service.for_device(clock.now))                   # nobody listening
        service.bind("d0", capable=True)
        service.streams("d0", True)
        service.pump_once()
        first = service.stream.ledger.entries[0]
        target = service.stream.ledger.target(first)
        self.assertEqual(service.for_device(target - 1.0), ("filler", None))   # device early
        kind, key = service.for_device(target)
        self.assertEqual(kind, "chunk")
        self.assertEqual(key, (service.gen_of(channel), 0))
        # A live source's cushion is three seconds: with one second queued the tap gives silence.
        service2, clock2, _ = self.make(FakeChannel(25), relay=False)
        service2.bind("d0", capable=True)
        service2.streams("d0", True)
        service2.pump_once()
        entry = service2.stream.ledger.entries[0]
        self.assertEqual(service2.for_device(service2.stream.ledger.target(entry)), ("silence", None))

    def test_delta_is_the_median_of_position_reports(self):
        service, _, _ = self.make()
        service.bind("d0", capable=True)
        self.assertEqual(service.delta(), 0.0)
        for n in range(10):
            service.note_sent(n * 40, 100.0 + n * 0.04)
        service.note_pos(80, 100.08 + 0.030)           # 30 ms late
        service.note_pos(100, 100.10 + 0.050)          # 20 ms into chunk 80+... 50 ms late
        service.note_pos(200, 100.20 + 0.040)
        service.note_pos(4000, 999.0)                   # not a stamp it sent: ignored
        service.note_pos(120, 100.12 + 9.0)             # absurd: ignored
        self.assertAlmostEqual(service.delta(), 0.040)
        service.bind("d0", capable=True)                # a new session forgets the old clock
        self.assertEqual(service.delta(), 0.0)

    def test_status_for_now_playing(self):
        service, _, _ = self.make()
        service.bind("d0", capable=True)
        self.assertEqual(service.status(True, False),
                         {"route": "xingzhi", "lead": 5.0, "ok": True, "on": False, "lag": 0.0})
        service.streams("d0", True)
        self.assertEqual(service.status(True, True)["on"], True)
        self.assertEqual(service.status(True, True)["lag"], 5.0)

    def test_from_env(self):
        logs = []
        self.assertIsNone(listen.ListenService.from_env(lambda: "ff", lambda: None, environ={}, logger=logs.append))
        with tempfile.TemporaryDirectory() as tmp:
            key = Path(tmp) / "relay-key"
            key.write_text("relay-key-for-tests-only\n", encoding="utf-8")
            env = {"TV_LISTEN_PORT": "8198", "TV_LISTEN_ROUTE": "xingzhi", "TV_RELAY_KEY_FILE": str(key),
                   "TV_LISTEN_DEVICE": "d1"}
            service = listen.ListenService.from_env(lambda: "ff", lambda: None, environ=env, logger=logs.append)
            self.assertEqual((service.port, service.route, service.single_device, service.lead_s),
                             (8198, "xingzhi", "d1", listen.DEFAULT_LEAD_S))
            # The relay derives the same secret from its --key-file.
            self.assertEqual(service.secret, listen.derive_secret("relay-key-for-tests-only"))
            for broken in ({"TV_LISTEN_ROUTE": "../x"}, {"TV_RELAY_KEY_FILE": str(Path(tmp) / "missing")},
                           {"TV_LISTEN_PORT": "99999"}):
                self.assertIsNone(listen.ListenService.from_env(lambda: "ff", lambda: None,
                                                                environ=dict(env, **broken), logger=logs.append))
        self.assertTrue(all("relay-key-for-tests-only" not in line for line in logs))

    def test_serves_http_for_the_bound_device_only(self):
        service, _, _ = self.make()
        service.bind("aaaaaaaaaaa1", capable=True)
        port = service.start(0)
        self.addCleanup(service.shutdown)
        token = listen.issue_token(service.secret, "aaaaaaaaaaa1")
        url = f"http://127.0.0.1:{port}/listen/aaaaaaaaaaa1/live.m3u8?t={token}&c=abc123"
        with urllib.request.urlopen(url) as response:
            self.assertEqual(response.status, 200)
        self.assertTrue(service.active)
        self.assertIn("abc123", service.stream.clients)
        other = listen.issue_token(service.secret, "bbbbbbbbbbb2")
        with self.assertRaises(urllib.error.HTTPError):
            urllib.request.urlopen(f"http://127.0.0.1:{port}/listen/bbbbbbbbbbb2/live.m3u8?t={other}")


FFMPEG = os.environ.get("TV_LISTEN_FFMPEG") or shutil.which("ffmpeg")


@unittest.skipUnless(FFMPEG, "ffmpeg not found (set TV_LISTEN_FFMPEG to a binary to run)")
class EncoderTests(unittest.TestCase):
    def test_two_seconds_of_pcm_become_aac_frames(self):
        got, done = [], threading.Event()

        def on_frames(frames):
            got.extend(frames)

        encoder = listen.AacEncoder(FFMPEG, on_frames)
        for _ in range(50):
            encoder.write(listen.SILENCE)
        encoder.close()
        deadline = time.monotonic() + 5
        while len(got) < 31 and time.monotonic() < deadline:
            time.sleep(0.05)
        # 2 s = 32000 samples = 31.25 frames of 1024, plus the priming frame.
        self.assertGreaterEqual(len(got), 31)
        self.assertTrue(all(frame[:2] == b"\xff\xf1" for frame in got))


if __name__ == "__main__":
    unittest.main()
