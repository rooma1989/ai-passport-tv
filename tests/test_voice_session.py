"""The voice session: packet sequences in, results out, with the speech recogniser and the
relay replaced by fakes. Also the Unix-socket bridge to muse-asr, against a real socket.

See server/voice/session.py and asr.py.
"""

import http.server
import json
import math
import os
import socketserver
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server import adpcm
from server.protocol import (AUDIO_BYTES, VOICE_MAX_BLOCKS, VOICE_RESULT_MAX, Kind, Packet,
                             ProtocolError, json_bytes)
from server.voice import session as voice_session
from server.voice.asr import AsrClient, AsrError
from server.voice.relay_client import RelayClient, RelayError
from server.voice.session import VoiceService, encode_result

SESSION = 77
GROUPS = ["我的投屏", "UP 主", "央视", "卫视", "动画影视"]
CONFIG = {"groups": GROUPS, "channel_list": [
    {"id": "cast", "name": "我的投屏", "g": 0}, {"id": "up01", "name": "小Lin说", "g": 1},
    {"id": "ch000", "name": "CCTV1", "g": 2}, {"id": "ch004", "name": "CCTV5", "g": 2},
    {"id": "ch017", "name": "湖南卫视", "g": 3}, {"id": "sr01", "name": "熊出没", "g": 4},
    {"id": "sr02", "name": "熊出没之探险日", "g": 4}]}


def context(current="ch000", **device):
    status = {"volume": 50, "brightness": 2, "steps": 5, "audio_only": 0}
    status.update(device)
    return lambda: {"config": CONFIG, "current": current, "device": status}


def tone_blocks(count):
    """ADPCM blocks of a 440 Hz tone, encoded the way the device does."""
    encoder = adpcm.Encoder()
    blocks, pcm = [], []
    for b in range(count):
        samples = [int(8000 * math.sin(2 * math.pi * 440 * (b * 640 + i) / 16000)) for i in range(640)]
        pcm.extend(samples)
        blocks.append(encoder.encode(samples))
    return blocks, pcm


def ctl(op, uid, **extra):
    return Packet(Kind.VOICE_CTL, SESSION, 0, 0, json_bytes(dict(op=op, id=uid, **extra)))


def audio(block, n):
    return Packet(Kind.VOICE, SESSION, 0, n * 40, block)


class FakeAsr:
    def __init__(self, text="我要看湖南卫视"):
        self.text = text
        self.calls = []
        self.error = None
        self.gate = None

    def transcribe(self, pcm, rate):
        if self.gate is not None:
            self.gate.wait(5)
        self.calls.append((pcm, rate))
        if self.error:
            raise AsrError(self.error)
        return self.text


class FakeRelay:
    def __init__(self, playing="ch000"):
        self.calls = []
        self.down = False
        self.answers = {}
        self.playing = playing

    def catalog(self):
        if self.down:
            raise RelayError("relay unreachable: ConnectionRefusedError")
        return {"ok": True, "series": [{"id": "sr01", "name": "熊出没", "total": 52, "next": 1},
                                       {"id": "sr02", "name": "熊出没之探险日记", "total": 26, "next": 1}],
                "creators": [{"id": "up01", "name": "小Lin说"}],
                "playing": {"channel": self.playing, "episode": 0, "paused": False}}

    def _do(self, name, *args):
        if self.down:
            raise RelayError("relay unreachable: ConnectionRefusedError")
        self.calls.append((name,) + args)
        return self.answers.get(name, {"ok": True})

    def play_episode(self, series, episode):
        return self._do("episode", series, episode)

    def pause(self):
        return self._do("pause")

    def resume(self):
        return self._do("resume")

    def next(self):
        return self._do("next")

    def previous(self):
        return self._do("previous")

    def seek(self, delta=None, position=None):
        return self._do("seek", delta, position)


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.asr = FakeAsr()
        self.relay = FakeRelay()
        self.log = []
        self.voice = VoiceService(self.asr, self.relay, self.log.append, threaded=False)

    def say(self, blocks=25, uid=1, ctx=None, **begin):
        ctx = ctx or context()
        params = dict(rate=16000, codec="adpcm")
        params.update(begin)
        self.voice.take(ctl("begin", uid, **params), ctx)
        for n, block in enumerate(tone_blocks(blocks)[0]):
            self.voice.take(audio(block, n), ctx)
        self.voice.take(ctl("end", uid), ctx)
        return [json.loads(raw) for raw in self.voice.results()]

    def test_utterance_is_decoded_and_recognised(self):
        results = self.say(25)
        pcm, rate = self.asr.calls[0]
        self.assertEqual(rate, 16000)
        self.assertEqual(len(pcm), 25 * 640 * 2)
        expected = b"".join(adpcm.decode_block(b) for b in tone_blocks(25)[0])
        self.assertEqual(pcm, expected)
        self.assertEqual(results, [{"id": 1, "state": "final", "reply": "换台中…", "intent": "channel",
                                    "heard": "我要看湖南卫视", "do": {"channel": "ch017"},
                                    "label": "湖南卫视", "lead": "05"}])

    def test_ten_second_cap(self):
        self.say(VOICE_MAX_BLOCKS + 10)
        self.assertEqual(len(self.asr.calls[0][0]), VOICE_MAX_BLOCKS * 1280)
        self.assertIn("dropped=10", self.log[-1])

    def test_cancel_before_end(self):
        ctx = context()
        self.voice.take(ctl("begin", 3, rate=16000, codec="adpcm"), ctx)
        self.voice.take(audio(tone_blocks(1)[0][0], 0), ctx)
        self.voice.take(ctl("cancel", 3), ctx)
        self.voice.take(ctl("end", 3), ctx)
        self.assertEqual(self.asr.calls, [])
        self.assertEqual(self.voice.results(), [])

    def test_cancel_while_recognising_drops_the_result(self):
        voice = VoiceService(self.asr, self.relay, self.log.append, threaded=True)
        self.asr.gate = threading.Event()
        ctx = context()
        voice.take(ctl("begin", 4, rate=16000, codec="adpcm"), ctx)
        voice.take(audio(tone_blocks(1)[0][0], 0), ctx)
        voice.take(ctl("end", 4), ctx)
        voice.take(ctl("cancel", 4), ctx)
        self.asr.gate.set()
        voice.wait_idle()
        self.assertEqual(len(self.asr.calls), 1)
        self.assertEqual(voice.results(), [])

    def test_result_after_a_new_session_is_dropped(self):
        voice = VoiceService(self.asr, self.relay, self.log.append, threaded=True)
        self.asr.gate = threading.Event()
        ctx = context()
        voice.take(ctl("begin", 5, rate=16000, codec="adpcm"), ctx)
        voice.take(audio(tone_blocks(1)[0][0], 0), ctx)
        voice.take(ctl("end", 5), ctx)
        voice.reset()
        self.asr.gate.set()
        voice.wait_idle()
        self.assertEqual(voice.results(), [])

    def test_a_new_begin_supersedes_the_old_utterance(self):
        voice = VoiceService(self.asr, self.relay, self.log.append, threaded=True)
        self.asr.gate = threading.Event()
        ctx = context()
        voice.take(ctl("begin", 6, rate=16000, codec="adpcm"), ctx)
        voice.take(audio(tone_blocks(1)[0][0], 0), ctx)
        voice.take(ctl("end", 6), ctx)
        voice.take(ctl("begin", 7, rate=16000, codec="adpcm"), ctx)
        voice.take(audio(tone_blocks(1)[0][0], 0), ctx)
        voice.take(ctl("end", 7), ctx)
        self.asr.gate.set()
        voice.wait_idle()
        ids = sorted(json.loads(r)["id"] for r in voice.results())
        self.assertEqual(ids, [6, 7], "6 ended before 7 began, so both answer; the device keeps 7")

    def test_asr_error_is_an_error_result(self):
        self.asr.error = "asr unreachable: FileNotFoundError"
        results = self.say()
        self.assertEqual(results, [{"id": 1, "state": "error", "reply": voice_session.REPLY_ASR_DOWN}])
        self.assertIn("asr_error", self.log[-1])

    def test_voice_off_answers_at_once(self):
        voice = VoiceService(None, None, threaded=False)
        self.assertEqual(voice.config(), {})
        self.assertEqual(VoiceService(self.asr, threaded=False).config(), {"voice": {"max_s": 10}})
        ctx = context()
        voice.take(ctl("begin", 9, rate=16000, codec="adpcm"), ctx)
        voice.take(ctl("end", 9), ctx)
        self.assertEqual(json.loads(voice.results()[0])["reply"], voice_session.REPLY_NO_VOICE)

    def test_wrong_format_is_refused(self):
        results = self.say(2, rate=8000)
        self.assertEqual(results[0]["state"], "error")
        self.assertEqual(self.asr.calls, [])

    def test_stray_packets_are_ignored(self):
        ctx = context()
        self.voice.take(audio(tone_blocks(1)[0][0], 0), ctx)
        self.voice.take(ctl("end", 12), ctx)
        self.voice.take(ctl("dance", 12), ctx)
        self.voice.take(Packet(Kind.VOICE_CTL, SESSION, 0, 0, json_bytes({"op": "begin", "id": "x"})), ctx)
        self.assertEqual(self.voice.ignored, 4)
        self.assertEqual(self.voice.results(), [])
        with self.assertRaises(ProtocolError):
            self.voice.take(Packet(Kind.VOICE_CTL, SESSION, 0, 0, b"{not json"), ctx)

    def test_empty_utterance_says_so(self):
        results = self.say(0)
        self.assertEqual(results[0]["reply"], "没听清，再说一次？")
        self.assertEqual(results[0]["intent"], "none")

    def test_transcripts_are_logged_only_when_asked(self):
        self.say()
        self.assertNotIn("湖南", "\n".join(self.log))
        log = []
        voice = VoiceService(self.asr, self.relay, log.append, log_text=True, threaded=False)
        self.voice = voice
        self.say()
        self.assertIn("heard='我要看湖南卫视'", log[-1])

    def test_device_settings(self):
        self.asr.text = "大声一点"
        self.assertEqual(self.say()[0]["do"], {"volume": 60})
        self.asr.text = "亮一点"
        self.assertEqual(self.say()[0]["do"], {"brightness": 1})
        self.asr.text = "只听声音"
        self.assertEqual(self.say()[0]["do"], {"audio_only": 1})
        self.asr.text = "打开状态页"
        self.assertEqual(self.say()[0]["do"], {"page": "status"})

    def test_mute_then_unmute_restores_the_volume(self):
        self.asr.text = "静音"
        self.assertEqual(self.say(ctx=context(volume=70))[0]["do"], {"volume": 0})
        self.asr.text = "取消静音"
        self.assertEqual(self.say(ctx=context(volume=0))[0]["do"], {"volume": 70})

    def test_setup_asks_first(self):
        self.asr.text = "我要配网"
        result = self.say()[0]
        self.assertNotIn("do", result)
        self.assertEqual(result["choices"], [{"label": "进入配网", "do": {"setup": 1}},
                                             {"label": "算了", "do": {"page": "close"}}])

    def test_episode_goes_to_the_relay_then_switches(self):
        self.asr.text = "熊出没第三集"
        result = self.say()[0]
        self.assertEqual(self.relay.calls, [("episode", "sr01", 3)])
        self.assertEqual(result["do"], {"channel": "sr01"})
        self.assertEqual((result["label"], result["lead"], result["reply"]), ("熊出没 第3集", "03", "从第 3 集开始播"))

    def test_episode_on_the_same_channel_needs_no_switch(self):
        self.asr.text = "第五集"
        self.relay.answers["episode"] = {"ok": True, "cut": True, "episode": 5}
        result = self.say(ctx=context("sr01"))[0]
        self.assertEqual(self.relay.calls, [("episode", "sr01", 5)])
        self.assertNotIn("do", result)

    def test_relay_refusal_is_shown(self):
        self.asr.text = "上一集"
        self.relay.answers["previous"] = {"ok": False, "error": "已经是第 1 集"}
        result = self.say(ctx=context("sr01"))[0]
        self.assertEqual((result["reply"], result["intent"]), ("已经是第 1 集", "previous"))

    def test_pause_and_seek_go_to_the_relay(self):
        self.asr.text = "暂停"
        self.assertEqual(self.say(ctx=context("up01"))[0]["reply"], "暂停")
        self.asr.text = "快进一分钟"
        self.say(ctx=context("up01"))
        self.assertEqual(self.relay.calls, [("pause",), ("seek", 60, None)])

    def test_relay_down(self):
        self.asr.text = "暂停"
        self.relay.down = True
        result = self.say(ctx=context("up01"))[0]
        self.assertEqual(result["reply"], voice_session.REPLY_RELAY_DOWN)
        self.assertIn("relay_catalog=relay unreachable", self.log[-1])
        # Channels still work without the relay: they come from CONFIG.
        self.asr.text = "熊出没"
        self.assertEqual(self.say()[0]["do"], {"channel": "sr01"})

    def test_no_relay_configured(self):
        voice = VoiceService(self.asr, None, threaded=False)
        self.voice = voice
        self.asr.text = "暂停"
        self.assertEqual(self.say(ctx=context("up01"))[0]["reply"], voice_session.REPLY_NO_RELAY)

    def test_pick_an_episode_choice(self):
        self.asr.text = "熊出没探险第三集"
        result = self.say()[0]
        refs = [c["ref"] for c in result["choices"]]
        self.assertEqual(sorted(refs), ["ep:sr01:3", "ep:sr02:3"])
        ctx = context()
        self.voice.take(ctl("pick", 2, ref="ep:sr02:3"), ctx)
        picked = json.loads(self.voice.results()[0])
        self.assertEqual(self.relay.calls, [("episode", "sr02", 3)])
        self.assertEqual((picked["id"], picked["do"], picked["lead"]), (2, {"channel": "sr02"}, "03"))
        # A ref the server never offered is refused without a relay call.
        self.voice.take(ctl("pick", 3, ref="ep:sr09:1"), ctx)
        self.assertEqual(json.loads(self.voice.results()[0])["reply"], "这个选项已经过期了")
        self.assertEqual(len(self.relay.calls), 1)

    def test_result_fits_the_device(self):
        long = {"id": 1, "state": "final", "heard": "长" * 400, "reply": "找到",
                "choices": [{"label": "很长的名字" * 4, "lead": "01", "do": {"channel": "ch000"}}] * 6}
        raw = encode_result(long)
        self.assertLessEqual(len(raw), VOICE_RESULT_MAX)
        self.assertTrue(raw.isascii())
        self.assertGreaterEqual(len(json.loads(raw)["choices"]), 1)

    def test_wav_corpus_is_off_by_default_and_works_when_set(self):
        self.assertIsNone(self.voice.wav_dir)
        with tempfile.TemporaryDirectory() as folder:
            self.voice = VoiceService(self.asr, self.relay, threaded=False, wav_dir=Path(folder))
            self.say(5)
            saved = list(Path(folder).glob("voice-*.wav"))
            self.assertEqual(len(saved), 1)

    def test_from_env(self):
        voice = VoiceService.from_env(self.log.append, environ={})
        self.assertFalse(voice.enabled)
        self.assertIsNone(voice.relay)
        with tempfile.TemporaryDirectory() as folder:
            key = Path(folder) / "relay-key"
            key.write_text("k" * 20 + "\n")
            voice = VoiceService.from_env(self.log.append, environ={
                "TV_ASR_SOCKET": "/run/muse-asr/asr.sock", "TV_RELAY_KEY_FILE": str(key),
                "TV_RELAY_DEVICE": "Passport", "TV_VOICE_LOG_TEXT": "1"})
            self.assertTrue(voice.enabled and voice.log_text)
            self.assertEqual(voice.relay.device, "Passport")
            self.assertNotIn("k" * 20, "\n".join(self.log), "the key never reaches the log")
            voice = VoiceService.from_env(self.log.append, environ={
                "TV_ASR_SOCKET": "x", "TV_RELAY_KEY_FILE": str(key), "TV_RELAY_DEVICE": "Passport",
                "TV_RELAY_MANAGE_URL": "http://203.0.113.9:8100"})
            self.assertIsNone(voice.relay, "the key is only sent to this host")


class _AsrHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self):
        length = int(self.headers["Content-Length"])
        body = self.rfile.read(length)
        self.server.seen.append((self.path, self.headers.get("X-Sample-Rate"), len(body)))
        status, answer = self.server.answer
        if status == "slow":
            self.server.release.wait(5)
            status = 200
        self.send_response(status)
        self.send_header("Content-Length", str(len(answer)))
        self.end_headers()
        self.wfile.write(answer)

    def address_string(self):
        return "unix"

    def log_message(self, *_args):
        pass


class _UnixHttp(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        pass            # the timeout test hangs up on purpose


class AsrBridgeTests(unittest.TestCase):
    """The client half of the bridge, against a real Unix socket (the muse-asr side is
    tested in its own repository)."""

    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix="asr", dir="/tmp" if os.path.isdir("/tmp") else None)
        self.path = os.path.join(self.folder, "asr.sock")
        self.server = _UnixHttp(self.path, _AsrHandler)
        self.server.seen = []
        self.server.answer = (200, json.dumps({"text": "湖南卫视"}, ensure_ascii=False).encode())
        self.server.release = threading.Event()
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.release.set()
        self.server.shutdown()
        self.server.server_close()
        os.unlink(self.path)
        os.rmdir(self.folder)

    def test_transcribe(self):
        text = AsrClient(self.path).transcribe(b"\x01\x00" * 16000)
        self.assertEqual(text, "湖南卫视")
        self.assertEqual(self.server.seen, [("/asr", "16000", 32000)])

    def test_empty_audio_is_not_sent(self):
        self.assertEqual(AsrClient(self.path).transcribe(b""), "")
        self.assertEqual(self.server.seen, [])

    def test_errors(self):
        self.server.answer = (500, b"error\n")
        with self.assertRaises(AsrError):
            AsrClient(self.path).transcribe(b"\0\0")
        self.server.answer = (200, b"not json")
        with self.assertRaises(AsrError):
            AsrClient(self.path).transcribe(b"\0\0")
        self.server.answer = (200, b'{"txt": 1}')
        with self.assertRaises(AsrError):
            AsrClient(self.path).transcribe(b"\0\0")
        with self.assertRaises(AsrError):
            AsrClient(self.path + ".missing").transcribe(b"\0\0")
        with self.assertRaises(AsrError):
            AsrClient(self.path).transcribe(b"\0" * 3)

    def test_timeout(self):
        self.server.answer = ("slow", b'{"text": ""}')
        with self.assertRaises(AsrError):
            AsrClient(self.path, timeout=0.2).transcribe(b"\0\0")

    def test_through_the_session(self):
        voice = VoiceService(AsrClient(self.path), None, threaded=False)
        ctx = context()
        voice.take(ctl("begin", 1, rate=16000, codec="adpcm"), ctx)
        for n, block in enumerate(tone_blocks(10)[0]):
            voice.take(audio(block, n), ctx)
        voice.take(ctl("end", 1), ctx)
        result = json.loads(voice.results()[0])
        self.assertEqual(result["do"], {"channel": "ch017"})
        self.assertEqual(self.server.seen, [("/asr", "16000", 10 * 1280)])


class RelayClientTests(unittest.TestCase):
    def test_only_loopback(self):
        for url in ("http://203.0.113.9:8100", "https://127.0.0.1:8100", "http://example.com"):
            with self.assertRaises(ValueError):
                RelayClient(url, "key", "Passport")
        RelayClient("http://localhost:8100/", "key", "Passport")

    def test_unreachable(self):
        client = RelayClient("http://127.0.0.1:9", "key", "Passport", timeout=0.5)
        with self.assertRaises(RelayError):
            client.catalog()


if __name__ == "__main__":
    unittest.main()
