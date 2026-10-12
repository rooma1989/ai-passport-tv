"""A whole voice command through a live session over a real socket: HELLO, CONFIG with
"voice", the device streams an utterance between media packets, and the result comes back
as a VOICE_RESULT whose sequence number continues the session's own."""

import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from server import adpcm, live
from server.protocol import Kind, Packet, json_bytes, receive_packet, send_packet
from server.tv_server import AVServer
from server.voice.session import VoiceService


class FakeAsr:
    def __init__(self):
        self.calls = 0

    def transcribe(self, pcm, rate):
        self.calls += 1
        return "我要看湖南卫视"


class LiveVoiceTests(unittest.TestCase):
    def setUp(self):
        self.saved = (dict(live._LOADED), dict(live.CHANNELS), dict(live.CHANNEL_LABELS),
                      dict(live.CHANNEL_AGENTS), live.DEFAULT_CHANNEL)
        self.tmp = tempfile.TemporaryDirectory()
        main = Path(self.tmp.name) / "channels.txt"
        main.write_text("ch000 | CCTV1 | https://example.com/1.m3u8\n"
                        "ch001 | 湖南卫视 | https://example.com/2.m3u8\n", encoding="utf-8")
        live.load_channels(main, None)
        self.old_env = {k: os.environ.pop(k, None) for k in ("TV_RELAY_EVENTS_FILE", "TV_ASR_SOCKET")}

    def tearDown(self):
        loaded, channels, labels, agents, default = self.saved
        live._LOADED.clear(); live._LOADED.update(loaded)
        for table, saved in ((live.CHANNELS, channels), (live.CHANNEL_LABELS, labels),
                             (live.CHANNEL_AGENTS, agents)):
            table.clear(); table.update(saved)
        live.DEFAULT_CHANNEL = default
        for key, value in self.old_env.items():
            if value is not None:
                os.environ[key] = value
        self.tmp.cleanup()

    def run_device(self, voice):
        from test_live_transcode import StubFactory
        token = b"t" * 32
        server = AVServer(None, token, "127.0.0.1", 0, 60000, logger=lambda _m: None)
        server.live_enabled = True
        server.channel_factory = StubFactory()
        server.voice = voice
        thread = threading.Thread(target=server.serve, daemon=True)
        thread.start()
        self.assertTrue(server.ready.wait(5))
        client = socket.create_connection(("127.0.0.1", server.port), timeout=5)
        client.setblocking(False)
        received, config, spoke = [], None, False
        block = adpcm.Encoder().encode([0] * 640)
        start = time.monotonic()
        try:
            send_packet(client, Packet(Kind.HELLO, 0, 0, 0, json_bytes(
                {"version": 1, "token": token.decode(), "channel": "ch000", "switch": 1, "voice": 1,
                 "volume": 50, "brightness": 1, "brightness_steps": 5})))
            while time.monotonic() - start < 4.0:
                try:
                    packet = receive_packet(client, timeout=0.3,
                                            expected_session=config["session"] if config else None)
                except TimeoutError:
                    continue
                if packet.kind == Kind.CONFIG:
                    config = json.loads(packet.payload)
                received.append(packet)
                if packet.kind == Kind.VOICE_RESULT:
                    break
                if config and not spoke and packet.kind == Kind.PCM:
                    session = config["session"]
                    send_packet(client, Packet(Kind.VOICE_CTL, session, 0, 0, json_bytes(
                        {"op": "begin", "id": 21, "rate": 16000, "codec": "adpcm"})))
                    for n in range(12):
                        send_packet(client, Packet(Kind.VOICE, session, 0, n * 40, block))
                    send_packet(client, Packet(Kind.VOICE_CTL, session, 0, 0,
                                               json_bytes({"op": "end", "id": 21})))
                    spoke = True
        finally:
            server.stop.set()
            client.close()
            thread.join(timeout=3)
        return config, received

    def test_command_round_trip(self):
        asr = FakeAsr()
        config, received = self.run_device(VoiceService(asr, None))
        self.assertEqual(config["voice"], {"max_s": 10})
        results = [p for p in received if p.kind == Kind.VOICE_RESULT]
        self.assertEqual(len(results), 1, [p.kind for p in received][-10:])
        result = json.loads(results[0].payload)
        self.assertEqual((result["id"], result["do"]), (21, {"channel": "ch001"}))
        self.assertEqual(asr.calls, 1)
        # The result takes the next sequence number: the device refuses a gap.
        seqs = [p.seq for p in received]
        self.assertEqual(seqs, list(range(len(seqs))))

    def test_no_voice_key_without_asr(self):
        config, _ = self.run_device(VoiceService(None, None))
        self.assertNotIn("voice", config)


if __name__ == "__main__":
    unittest.main()
