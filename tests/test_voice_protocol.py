"""The three voice packet kinds on the TV socket, and how the media server takes them.

Kinds 10/11/12 (docs/superpowers/specs/2026-10-12-voice-control-design.md section 6):
VOICE_CTL and VOICE come from the device during any phase of a session; the
results go back with the session's next sequence number, like a SWITCH.
"""

import json
import math
import re
import socket
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server import adpcm, reconnect, tv_server
from server.protocol import (AUDIO_BYTES, CONTROL_MAX, VOICE_MAX_BLOCKS, VOICE_MAX_S, VOICE_RESULT_MAX,
                             Kind, Packet, ProtocolError, json_bytes, receive_packet, send_packet,
                             validate_length)
from server.voice.asr import AsrError
from server.voice.session import VoiceService

HEADER = Path(__file__).resolve().parents[1] / "main" / "av_protocol.h"


class KindTests(unittest.TestCase):
    def test_values(self):
        self.assertEqual((Kind.VOICE_CTL, Kind.VOICE, Kind.VOICE_RESULT), (10, 11, 12))
        self.assertEqual((VOICE_MAX_S, VOICE_MAX_BLOCKS, VOICE_RESULT_MAX), (10, 250, 2048))
        self.assertEqual(VOICE_MAX_BLOCKS * 40, VOICE_MAX_S * 1000)

    def test_lengths(self):
        validate_length(Kind.VOICE, AUDIO_BYTES)
        validate_length(Kind.VOICE_CTL, 1)
        validate_length(Kind.VOICE_CTL, CONTROL_MAX)
        validate_length(Kind.VOICE_RESULT, VOICE_RESULT_MAX)
        for kind, size in ((Kind.VOICE, AUDIO_BYTES - 1), (Kind.VOICE, 1280), (Kind.VOICE, 0),
                           (Kind.VOICE_CTL, 0), (Kind.VOICE_RESULT, 0),
                           (Kind.VOICE_RESULT, VOICE_RESULT_MAX + 1)):
            with self.subTest(kind=kind, size=size), self.assertRaises(ProtocolError):
                validate_length(kind, size)

    def test_round_trip(self):
        left, right = socket.socketpair()
        with left, right:
            packets = [Packet(Kind.VOICE_CTL, 5, 0, 0, json_bytes({"op": "begin", "id": 1})),
                       Packet(Kind.VOICE, 5, 0, 40, bytes(AUDIO_BYTES)),
                       Packet(Kind.VOICE_RESULT, 5, 9, 0, b'{"id":1,"state":"final"}')]
            for packet in packets:
                send_packet(left, packet)
            for packet in packets:
                self.assertEqual(receive_packet(right, expected_session=5), packet)

    def test_flags_are_refused(self):
        with self.assertRaises(ProtocolError):
            Packet(Kind.VOICE, 1, 0, 0, bytes(AUDIO_BYTES), flags=1).encode()

    def test_device_header_agrees_where_it_names_the_kinds(self):
        """main/av_protocol.h is the authority. The firmware branch adds AV_VOICE*; until it is
        merged the header lists only the older kinds, which must still agree."""
        text = HEADER.read_text(encoding="utf-8")
        body = re.search(r"enum\s*\{\s*(AV_HELLO[^}]*)\}", text).group(1)
        value = 0
        names = {}
        for item in (part.strip() for part in body.split(",")):
            if not item:
                continue
            name, _, explicit = item.partition("=")
            value = int(explicit) if explicit else value + 1
            names[name.strip()] = value
        python = {"AV_HELLO": Kind.HELLO, "AV_CONFIG": Kind.CONFIG, "AV_AUDIO": Kind.PCM,
                  "AV_VIDEO": Kind.JPEG, "AV_END": Kind.END, "AV_ERROR": Kind.ERROR,
                  "AV_PALETTE": Kind.PALETTE, "AV_SWITCH": Kind.SWITCH, "AV_STATUS": Kind.STATUS,
                  "AV_VOICE_CTL": Kind.VOICE_CTL, "AV_VOICE": Kind.VOICE,
                  "AV_VOICE_RESULT": Kind.VOICE_RESULT}
        for name, number in names.items():
            self.assertEqual(number, python[name], name)
        limit = re.search(r"#define\s+AV_VOICE_RESULT_MAX\s+(\d+)", text)
        if limit:
            self.assertEqual(int(limit.group(1)), VOICE_RESULT_MAX)


class AdpcmDecodeTests(unittest.TestCase):
    def test_block_round_trip(self):
        samples = [int(9000 * math.sin(i / 7)) for i in range(640)]
        block = adpcm.Encoder().encode(samples)
        self.assertEqual(len(block), AUDIO_BYTES)
        pcm = adpcm.decode_block(block)
        self.assertEqual(len(pcm), 1280)
        decoded = [int.from_bytes(pcm[i:i + 2], "little", signed=True) for i in range(0, 1280, 2)]
        self.assertEqual(decoded, adpcm.decode(block, 640))
        self.assertLess(max(abs(a - b) for a, b in zip(decoded[32:], samples[32:])), 1500)

    def test_bad_header(self):
        with self.assertRaises(ValueError):
            adpcm.decode_block(b"\0\0\x99\0" + bytes(320))
        with self.assertRaises(ValueError):
            adpcm.decode_block(b"\0")


class FakeAsr:
    def __init__(self, text="湖南卫视"):
        self.text = text

    def transcribe(self, pcm, rate):
        if self.text is None:
            raise AsrError("down")
        return self.text


class ServerHookTests(unittest.TestCase):
    """The hooks in tv_server.py: upstream voice packets during play and waits, results with
    the next sequence number, and CONFIG's voice key."""

    def setUp(self):
        self.server = tv_server.AVServer(None, "127.0.0.1", 0)
        self.server.voice = VoiceService(FakeAsr(), None, threaded=False)
        self.server.channel_name = "ch000"
        self.server.device_status = {"volume": 50, "brightness": 1, "steps": 5}
        self.device, self.conn = socket.socketpair()
        self.conn.setblocking(False)
        self.session = 4242

    def tearDown(self):
        self.device.close()
        self.conn.close()

    def speak(self, uid=1):
        send_packet(self.device, Packet(Kind.VOICE_CTL, self.session, 0, 0,
                                        json_bytes({"op": "begin", "id": uid, "rate": 16000, "codec": "adpcm"})))
        block = adpcm.Encoder().encode([0] * 640)
        for n in range(3):
            send_packet(self.device, Packet(Kind.VOICE, self.session, 0, n * 40, block))
        send_packet(self.device, Packet(Kind.VOICE_CTL, self.session, 0, 0, json_bytes({"op": "end", "id": uid})))

    def test_voice_packets_while_playing(self):
        self.speak()
        for _ in range(5):
            self.assertFalse(self.server._device_left(self.conn, self.session))
        self.assertEqual(self.server.voice_flush(self.conn, self.session, 17), 18)
        result = receive_packet(self.device, expected_session=self.session)
        self.assertEqual((result.kind, result.seq, result.pts_ms), (Kind.VOICE_RESULT, 17, 0))
        decoded = json.loads(result.payload)
        self.assertEqual(decoded["do"], {"channel": "ch017"})
        self.assertEqual(self.server.voice_flush(self.conn, self.session, 18), 18, "nothing more queued")

    def test_voice_packets_while_waiting(self):
        import time
        self.speak()
        self.assertTrue(self.server._wait_until(self.conn, time.monotonic() + 0.2, self.session))
        self.assertEqual(len(self.server.voice.results()), 1)

    def test_end_still_ends(self):
        send_packet(self.device, Packet(Kind.END, self.session, 0, 0))
        self.assertTrue(self.server._device_left(self.conn, self.session))

    def test_other_kinds_still_refused(self):
        send_packet(self.device, Packet(Kind.VOICE_RESULT, self.session, 0, 0, b"{}"))
        with self.assertRaises(ProtocolError):
            self.server._device_left(self.conn, self.session)

    def test_reconnect_hold_sends_results_with_its_sequence(self):
        self.speak()
        for _ in range(5):
            self.server._device_left(self.conn, self.session)
        wire = reconnect.WireState(seq=30, audio_pts=0)
        reconnect._flush_voice(self.server, self.conn, self.session, wire)
        self.assertEqual(wire.seq, 31)
        self.assertEqual(receive_packet(self.device, expected_session=self.session).seq, 30)

    def test_config_voice_key(self):
        self.assertEqual(self.server.voice.config(), {"voice": {"max_s": 10}})
        self.server.voice = VoiceService(None, None)
        self.assertEqual(self.server.voice.config(), {})

    def test_context_is_what_the_device_sees(self):
        context = self.server._voice_context()
        self.assertEqual(context["current"], "ch000")
        self.assertIn("channel_list", context["config"])
        self.assertEqual(context["device"]["volume"], 50)


if __name__ == "__main__":
    unittest.main()
