"""Listening on the phone, end to end on the host: the media server's send loop follows the ledger,
the firmware is told to mute, old firmware is untouched, and the relay hands out addresses only to
the people who may control the device.

The send loop is driven over a socket pair with a queue-only channel (no picture), the way
test_tv_server.py's WaitingForTheSoundTests does; the relay over real sockets with the fakes of
test_relay_tenants.py.
"""

import collections
import socket
import struct
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from server import listen, now_playing
from server.media import AUDIO_BYTES, AUDIO_CHUNK_MS, FRAME_COUNT, Media
from server.protocol import Kind, json_object, receive_packet
from server.reconnect import SILENCE
from server.relay.tenants import Caller
from server.timeline import SessionClock
from server.tv_server import AVServer, _skip_audio_to

import test_relay_tenants
from test_listen import FakeEncoder
from test_relay_tenants import ALICE_DEVICE, BOB_DEVICE
from test_relay_web import KEY
from test_tv_server import TEST_TOKEN, indexed_frame


class QueueChannel:
    """A LiveChannel stand-in: sound arrives in real time, numbered in its first four bytes; no picture."""

    def __init__(self, seconds: float):
        self.lock = threading.Lock()
        self.audio = collections.deque()
        self.audio_content = collections.deque()
        self.video = collections.deque(maxlen=4)
        self.session_clock = SessionClock(chunk_ms=AUDIO_CHUNK_MS)
        self.made = 0
        self.dropped_video = 0
        self.skipped_audio = 0
        for _ in range(int(seconds * 1000 / AUDIO_CHUNK_MS)):
            self.push()

    def push(self):
        with self.lock:
            self.audio.append(struct.pack("<I", self.made) + b"\x55" * (AUDIO_BYTES - 4))
            self.audio_content.append(float(self.made * AUDIO_CHUNK_MS))
            self.made += 1

    def pop_audio(self):
        with self.lock:
            if not self.audio:
                return None
            content = self.audio_content.popleft()
            return self.audio.popleft(), self.session_clock.audio(content)

    def pop_video(self, keep=0):
        return None

    def video_pending(self):
        return False

    def audio_pending(self):
        with self.lock:
            return bool(self.audio)

    def has_data(self):
        return self.audio_pending()

    def failure(self):
        return None

    def picture_lag_s(self):
        return 0.0


def run_session(capable: bool, listening: bool, seconds: float = 2.6, close_after: float | None = None):
    """Run the legacy send loop for `seconds` with a real-time source; returns the packets the device got."""
    server = AVServer(Media(bytes(320000), (indexed_frame(9),) * FRAME_COUNT), TEST_TOKEN, port=0,
                      duration_ms=int(seconds * 1000))
    channel = QueueChannel(2.0)
    service = listen.ListenService(b"k" * 32, "xingzhi", lambda: "ffmpeg", lambda: server.live_channel,
                                   is_relay=lambda c: True, lead_s=1.0, logger=lambda _m: None,
                                   encoder_factory=FakeEncoder, prefill_s=0.2)
    server.listen = service
    server.live_channel = channel
    server.device_listen = capable
    service.bind("d0", capable)
    if listening:
        service.streams("d0", True)
    stop = threading.Event()

    def produce():
        started, made = time.monotonic(), channel.made
        while not stop.is_set():
            due = made + int((time.monotonic() - started) * 1000 / AUDIO_CHUNK_MS)
            while channel.made < due:
                channel.push()
            time.sleep(0.01)

    def close_later():
        time.sleep(close_after)
        service.close("test")

    left, right = socket.socketpair()
    left.setblocking(False)
    packets = []

    def read():
        while True:
            try:
                packet = receive_packet(right, timeout=1.0)
            except TimeoutError:
                continue
            except (EOFError, OSError):
                return
            packets.append((time.monotonic(), packet))
            if packet.kind == Kind.END:
                return

    threads = [threading.Thread(target=produce, daemon=True), threading.Thread(target=service._run, daemon=True),
               threading.Thread(target=read, daemon=True)]
    if close_after is not None:
        threads.append(threading.Thread(target=close_later, daemon=True))
    for thread in threads:
        thread.start()
    started = time.monotonic()
    try:
        server._pace_live(left, channel, 1)
        threads[2].join(timeout=3)
    finally:
        stop.set()
        service._stop.set()
        left.close()
        right.close()
    return started, packets, service


class SendLoopTests(unittest.TestCase):
    def pcm(self, packets):
        return [(at, p) for at, p in packets if p.kind == Kind.PCM]

    def switches(self, packets):
        return [json_object(p.payload) for _, p in packets if p.kind == Kind.SWITCH]

    def test_listening_device_is_muted_held_back_then_follows_the_phone(self):
        started, packets, service = run_session(capable=True, listening=True)
        self.assertEqual(self.switches(packets)[0], {"mute": 1})
        audio = self.pcm(packets)
        stamps = [p.pts_ms for _, p in audio]
        # Fillers and real chunks alike: the wire clock never jumps, which the firmware requires.
        self.assertEqual(stamps, list(range(0, AUDIO_CHUNK_MS * len(stamps), AUDIO_CHUNK_MS)))
        self.assertGreater(len(stamps), 40)
        # Early on the device is a second ahead of the schedule: it gets silence, nothing is dequeued.
        self.assertEqual(audio[0][1].payload, SILENCE)
        real = [(at, struct.unpack("<I", p.payload[:4])[0]) for at, p in audio if p.payload != SILENCE]
        self.assertTrue(real, "the device reached the phone's schedule")
        # It then plays what the phone had, in order and from where the phone started (nothing skipped).
        numbers = [n for _, n in real]
        self.assertEqual(numbers, list(range(numbers[0], numbers[0] + len(numbers))))
        self.assertEqual(numbers[0], 0)
        # The phone's stream starts prefill (0.2 s) in the past and the device plays it H (1 s) later;
        # the sender works 0.6 s ahead of the device, so the first real chunk goes out ~0.2 s in.
        self.assertGreater(real[0][0] - started, 0.1)
        self.assertGreater(service.stream.ledger.fillers, 5)

    def test_mute_lifted_when_nobody_listens_any_more(self):
        _, packets, _ = run_session(capable=True, listening=True, close_after=1.0)
        self.assertEqual(self.switches(packets), [{"mute": 1}, {"mute": 0}])

    def test_old_firmware_gets_no_mute_and_plays_the_queue_as_before(self):
        _, packets, service = run_session(capable=False, listening=False)
        self.assertEqual(self.switches(packets), [])
        audio = self.pcm(packets)
        self.assertNotIn(SILENCE, [p.payload for _, p in audio])
        numbers = [struct.unpack("<I", p.payload[:4])[0] for _, p in audio]
        self.assertEqual(numbers, list(range(len(numbers))))
        self.assertIsNone(service.streams("d0", True), "no stream for firmware that cannot mute")

    def test_capable_firmware_nobody_listening_is_unchanged(self):
        _, packets, _ = run_session(capable=True, listening=False)
        self.assertEqual(self.switches(packets), [])
        self.assertNotIn(SILENCE, [p.payload for _, p in self.pcm(packets)])


class SkipTests(unittest.TestCase):
    def test_late_device_drops_older_chunks(self):
        channel = QueueChannel(0.4)                      # contents 0, 40 .. 360
        self.assertTrue(_skip_audio_to(channel, 120.0))
        self.assertEqual(channel.audio_content[0], 120.0)
        self.assertEqual(channel.skipped_audio, 3)
        self.assertFalse(_skip_audio_to(channel, 1000.0))    # gone past the end: silence instead
        self.assertEqual(len(channel.audio), 0)


class ConfigTests(unittest.TestCase):
    def test_a_session_that_starts_while_someone_listens_starts_muted(self):
        server = AVServer(Media(bytes(320000), (indexed_frame(9),) * FRAME_COUNT), TEST_TOKEN, port=0)
        service = listen.ListenService(b"k" * 32, "passport", lambda: "ffmpeg", lambda: None,
                                       logger=lambda _m: None, encoder_factory=FakeEncoder)
        server.listen = service
        server.device_listen = True
        service.bind("d1", True)
        self.assertEqual(server._listen_config(), {})
        service.streams("d1", True)
        self.assertEqual(server._listen_config(), {"mute": 1})
        server.device_listen = False                     # firmware that never said "listen": 1
        self.assertEqual(server._listen_config(), {})


class NowPlayingTests(unittest.TestCase):
    def test_listen_record_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "now.json"
            now_playing.write(path, "ch000", True, True, now=100.0,
                              listen={"route": "xingzhi", "lead": 5.0, "ok": True, "on": True, "lag": 5.0})
            self.assertEqual(now_playing.read(path, now=101.0)["listen"],
                             {"route": "xingzhi", "lead": 5.0, "ok": True, "on": True, "lag": 5.0})
            now_playing.write(path, "ch000", True, True, now=100.0)
            self.assertIsNone(now_playing.read(path, now=101.0)["listen"])
            now_playing.write(path, "ch000", True, True, now=100.0, listen={"route": 5})
            self.assertIsNone(now_playing.read(path, now=101.0)["listen"])
            # Offline: nothing to listen to.
            now_playing.write(path, "ch000", True, True, now=100.0,
                              listen={"route": "xingzhi", "lead": 5.0, "ok": True, "on": False, "lag": 0})
            self.assertIsNone(now_playing.read(path, now=1000.0)["listen"])


class RelayListenTests(unittest.TestCase):
    """POST /api/listen authorises like the other device actions and signs for that device only."""

    # The relay fixture of test_relay_tenants.py (not its tests: the class is not imported by name).
    _base = test_relay_tenants.TenantWebTests
    setUp, tearDown, fake_start, fake_pump = _base.setUp, _base.tearDown, _base.fake_start, _base.fake_pump
    files, raw, call, guest_cookie = _base.files, _base.raw, _base.call, _base.guest_cookie

    def online(self, device, ok=True, on=False, lag=0.0, channel="ch000"):
        now_playing.write(self.files(device).now, channel, True, True, now=self.now, since=self.now,
                          device={"volume": 50, "brightness": 1, "steps": 5},
                          listen={"route": "xingzhi", "lead": 5.0, "ok": ok, "on": on, "lag": lag})

    def test_owner_gets_a_signed_address_the_media_server_accepts(self):
        self.online(ALICE_DEVICE)
        status, answer = self.call("alice", "POST", "/api/listen", {"device": ALICE_DEVICE})
        self.assertEqual(status, 200)
        self.assertTrue(answer["ok"])
        prefix = f"/tv/listen/xingzhi/{ALICE_DEVICE}/"
        self.assertTrue(answer["url"].startswith(prefix + "live.m3u8?t="))
        self.assertTrue(answer["bye"].startswith(prefix + "bye?t="))
        token = answer["url"].split("t=", 1)[1]
        # The media server derives the same secret from the relay's key file.
        media_secret = listen.derive_secret(KEY)
        self.assertTrue(listen.check_token(media_secret, ALICE_DEVICE, token, now=self.now))
        self.assertFalse(listen.check_token(media_secret, BOB_DEVICE, token, now=self.now))
        self.assertEqual(answer["lead"], 5.0)
        # The remote page sees that it is offered.
        _, state = self.call("alice", "GET", "/api/state")
        self.assertEqual(state["now"]["listen"], {"ok": True, "on": False, "lead": 5.0})

    def test_guest_of_that_device_may_listen(self):
        self.online(ALICE_DEVICE)
        guest = self.guest_cookie(ALICE_DEVICE)
        status, answer = self.call(guest, "POST", "/api/listen", {})
        self.assertEqual(status, 200)
        self.assertTrue(answer["ok"])
        self.assertIn(ALICE_DEVICE, answer["url"])

    def test_other_people_and_strangers_get_404(self):
        self.online(BOB_DEVICE)
        self.assertEqual(self.call("alice", "POST", "/api/listen", {"device": BOB_DEVICE})[0], 404)
        guest = self.guest_cookie(ALICE_DEVICE)
        self.assertEqual(self.call(guest, "POST", "/api/listen", {"device": BOB_DEVICE})[0], 404)
        self.assertEqual(self.raw("POST", "/api/listen", None, {})[0], 404)
        self.assertEqual(self.raw("POST", "/api/listen", "tvs=forged", {})[0], 404)
        self.assertEqual(self.raw("GET", "/api/listen/time")[0], 404)

    def test_refusals_say_why(self):
        status, answer = self.call("alice", "POST", "/api/listen", {"device": ALICE_DEVICE})
        self.assertIn("没在线", answer["error"])                      # offline
        now_playing.write(self.files(ALICE_DEVICE).now, "ch000", True, True, now=self.now, since=self.now)
        status, answer = self.call("alice", "POST", "/api/listen", {"device": ALICE_DEVICE})
        self.assertIn("服务器", answer["error"])                      # media server without TV_LISTEN_PORT
        self.online(ALICE_DEVICE, ok=False)
        status, answer = self.call("alice", "POST", "/api/listen", {"device": ALICE_DEVICE})
        self.assertIn("固件太旧", answer["error"])                    # firmware without "listen": 1

    def test_clock_for_the_phone(self):
        status, answer = self.call("alice", "GET", "/api/listen/time")
        self.assertEqual(status, 200)
        self.assertEqual(answer["t"], self.now * 1000)

    def test_progress_counts_the_extra_lag_while_listening(self):
        entry = {"offset": 100.0, "began": 0.0}
        self.relay.monotonic = lambda: 20.0
        self.online(ALICE_DEVICE, lag=0.0)
        dev = self.relay.find_device(ALICE_DEVICE, Caller(user=self.alice, role="user"))
        self.assertAlmostEqual(self.relay._position(entry, self.relay._lag(dev)), 116.0)
        self.online(ALICE_DEVICE, on=True, lag=5.0)
        self.assertAlmostEqual(self.relay._position(entry, self.relay._lag(dev)), 111.0)


if __name__ == "__main__":
    unittest.main()
