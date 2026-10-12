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
        self.slow = False
        self.found = 8

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

    def seek(self, delta=None, position=None, **share):
        return self._do("seek", delta, position, *([share] if share else []))

    # ---- voice search (server/relay/voice.py) ----

    def search(self, site, query, timeout, kind="video"):
        if self.slow:
            raise RelayError("relay timed out", slow=True)
        answer = self._do("search", site, kind, query)
        if "search" in self.answers:
            return answer
        if kind == "video":
            results = [{"id": f"BV1{n:09d}", "title": f"{query} {n}", "uploader": "猫咪", "duration": 60 + n}
                       for n in range(self.found)]
        else:
            results = [{"id": str(546195 + n), "name": f"{query}{n}", "fans": 20840107, "videos": 680,
                        "latest": {"id": "BV1iqpt6cEMU", "title": "最新的", "duration": 2893} if n == 0 else None}
                       for n in range(self.found)]
        return {"ok": True, "site": site, "kind": kind, "results": results}

    def cast_video(self, site, video_id, title="", duration=0):
        return self._do("cast", site, video_id, title, duration)

    def creator(self, site, creator_id, name, action, timeout):
        return self._do(action, site, creator_id, name)


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

    def test_seek_to_a_share_says_where(self):
        self.relay.answers["seek"] = {"ok": True, "position": 742.5}
        self.asr.text = "跳到一半"
        result = self.say(ctx=context("up01"))[0]
        self.assertEqual(self.relay.calls, [("seek", None, None, {"fraction": 0.5})])
        self.assertEqual(result["reply"], "跳到 12 分 22 秒")
        self.asr.text = "跳到最后"
        self.say(uid=2, ctx=context("up01"))
        self.assertEqual(self.relay.calls[-1], ("seek", None, None, {"from_end": 30}))
        # A spoken time keeps its own reply; past the end the relay's refusal is shown.
        self.asr.text = "跳到三分二十秒"
        self.assertEqual(self.say(uid=3, ctx=context("up01"))[0]["reply"], "跳到 3 分 20 秒")
        self.assertEqual(self.relay.calls[-1], ("seek", None, 200))
        self.relay.answers["seek"] = {"ok": False, "error": "这个视频只有 23 分 40 秒"}
        self.asr.text = "跳到一小时"
        self.assertEqual(self.say(uid=4, ctx=context("up01"))[0]["reply"], "这个视频只有 23 分 40 秒")
        self.relay.answers["seek"] = {"ok": False, "error": "不知道这个视频有多长，没法按比例跳"}
        self.asr.text = "跳到三分之一"
        self.assertEqual(self.say(uid=5, ctx=context("up01"))[0]["reply"], "不知道这个视频有多长，没法按比例跳")

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

    def test_list_a_category_and_page_through_it(self):
        """「央视有哪些台」: five channels and a "next page" row; picking it answers the same id."""
        config = {"groups": ["央视"], "channel_list": [{"id": f"ch{n:03d}", "name": f"CCTV{n + 1}", "g": 0}
                                                     for n in range(8)]}
        ctx = lambda: {"config": config, "current": "ch000", "device": {"volume": 50}}
        self.asr.text = "央视有哪些台"
        result = self.say(ctx=ctx)[0]
        self.assertEqual((result["intent"], result["reply"]), ("list", "央视有 8 个台"))
        self.assertEqual([c.get("do") for c in result["choices"][:5]],
                         [{"channel": f"ch{n:03d}"} for n in range(5)])
        last = result["choices"][5]
        self.assertEqual((last["label"], last["ref"]), ("下一页 · 还有 3 个", "ls:..2:5"))
        self.voice.take(ctl("pick", 1, ref=last["ref"]), ctx)
        page = json.loads(self.voice.results()[0])
        self.assertEqual((page["id"], page["intent"]), (1, "list"))
        self.assertEqual([c["do"]["channel"] for c in page["choices"]], ["ch005", "ch006", "ch007"])
        self.assertNotIn("do", page)
        self.assertEqual(self.relay.calls, [], "listing never calls the relay's actions")
        # The first page's ref is no longer offered once the second page replaced it.
        self.voice.take(ctl("pick", 1, ref="ls:..2:5"), ctx)
        self.assertEqual(json.loads(self.voice.results()[0])["reply"], "这个选项已经过期了")

    def test_sites_come_from_the_relay(self):
        catalog = self.relay.catalog

        def with_sites():
            data = catalog()
            for row in data["series"] + data["creators"]:
                row["site"] = "youtube" if row["id"] in ("up01", "sr02") else "bilibili"
            return data

        self.relay.catalog = with_sites
        self.asr.text = "我有哪些youtube频道告诉我。"
        result = self.say()[0]
        # Only the site's creators; its series need 剧 / 剧集.
        self.assertEqual((result["intent"], result["reply"]), ("list", "YouTube 频道有 1 个"))
        self.assertEqual(result["choices"], [{"label": "小Lin说", "lead": "02", "do": {"channel": "up01"}}])
        self.asr.text = "YouTube有哪些剧集"
        result = self.say(uid=3)[0]
        self.assertEqual(result["choices"], [{"label": "熊出没之探险日", "lead": "07", "do": {"channel": "sr02"}}])
        # An older relay without sites: say so instead of guessing.
        self.relay.catalog = catalog
        result = self.say(uid=2)[0]
        self.assertEqual(result["intent"], "none")
        self.assertIn("中转", result["reply"])

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

    def test_from_env_per_device(self):
        # Multi-user media server (TV_DB): the relay device is the one each session admits;
        # TV_RELAY_DEVICE is ignored, and an unbound device gets no relay at all.
        with tempfile.TemporaryDirectory() as folder:
            key = Path(folder) / "relay-key"
            key.write_text("k" * 20 + "\n")
            environ = {"TV_ASR_SOCKET": "x", "TV_RELAY_KEY_FILE": str(key), "TV_RELAY_DEVICE": "Passport"}
            voice = VoiceService.from_env(self.log.append, environ=environ, per_device=True)
            self.assertIsNone(voice.relay, "no device before the first HELLO")
            self.assertIn("TV_RELAY_DEVICE ignored", "\n".join(self.log))
            self.assertIn("relay per device", self.log[-1])
            voice.bind_device("legacy-passport")
            self.assertEqual(voice.relay.device, "legacy-passport")
            voice.bind_device("0123456789ab")
            self.assertEqual(voice.relay.device, "0123456789ab")
            voice.bind_device(None)
            self.assertIsNone(voice.relay)
            self.assertNotIn("k" * 20, "\n".join(self.log))
            voice = VoiceService.from_env(self.log.append, environ=dict(
                environ, TV_RELAY_MANAGE_URL="http://203.0.113.9:8100"), per_device=True)
            voice.bind_device("legacy-passport")
            self.assertIsNone(voice.relay, "the key is only sent to this host")
        # Single-user: bind_device() changes nothing.
        self.voice.bind_device("legacy-passport")
        self.assertIs(self.voice.relay, self.relay)

    def test_a_running_utterance_keeps_its_device(self):
        # The worker uses the relay client of the session it started in, even if the next
        # device is admitted while it runs.
        first, second = FakeRelay(), FakeRelay()
        gate = threading.Event()

        class SlowAsr(FakeAsr):
            def transcribe(inner, pcm, rate=16000):
                gate.wait(5)
                return "暂停"

        voice = VoiceService(SlowAsr(), first, self.log.append, threaded=True)
        voice._relay_for = {"A": first, "B": second}.get
        ctx = context("up01")
        voice.take(ctl("begin", 1, rate=16000, codec="adpcm"), ctx)
        for n, block in enumerate(tone_blocks(5)[0]):
            voice.take(audio(block, n), ctx)
        voice.take(ctl("end", 1), ctx)
        voice.bind_device("B")
        gate.set()
        voice.wait_idle()
        self.assertEqual(first.calls, [("pause",)])
        self.assertEqual(second.calls, [])


class SearchSessionTests(unittest.TestCase):
    """Voice search: results as choices, paging and picking by ref or by voice, the question when
    it is unclear what to search for, and a creator's two actions. The relay is a fake."""

    def setUp(self):
        self.asr = FakeAsr()
        self.relay = FakeRelay()
        self.now = 100.0
        self.voice = VoiceService(self.asr, self.relay, lambda _m: None, threaded=False, clock=lambda: self.now)
        self.uid = 0

    def say(self, text):
        self.asr.text = text
        self.uid += 1
        ctx = context()
        self.voice.take(ctl("begin", self.uid, rate=16000, codec="adpcm"), ctx)
        self.voice.take(audio(tone_blocks(1)[0][0], 0), ctx)
        self.voice.take(ctl("end", self.uid), ctx)
        results = [json.loads(raw) for raw in self.voice.results()]
        self.assertEqual(len(results), 1)
        return results[0]

    def pick(self, ref):
        self.uid += 1
        self.voice.take(ctl("pick", self.uid, ref=ref), context())
        return json.loads(self.voice.results()[0])

    def searches(self):
        return [c for c in self.relay.calls if c[0] == "search"]

    def test_results_page_by_page_then_cast(self):
        result = self.say("搜视频猫和老鼠")
        self.assertEqual(self.searches(), [("search", "bilibili", "video", "猫和老鼠")])
        self.assertEqual((result["intent"], result["reply"]), ("search", "找到 8 个B站视频"))
        self.assertNotIn("do", result)
        self.assertEqual(result["choices"][0], {"label": "猫和老鼠 0 · 1:00", "lead": "01", "ref": "sv:1:0"})
        self.assertEqual(result["choices"][5], {"label": "下一页 · 还有 3 个", "lead": "", "ref": "sp:1:5"})
        page = self.pick("sp:1:5")
        self.assertEqual(page["reply"], "第 6-8 个，共 8 个")
        self.assertEqual([c["lead"] for c in page["choices"]], ["06", "07", "08"])
        self.assertEqual(len(self.searches()), 1, "paging does not search again")
        cast = self.pick("sv:1:6")
        self.assertEqual(self.relay.calls[-1], ("cast", "bilibili", "BV1000000006", "猫和老鼠 6", 66))
        self.assertEqual((cast["intent"], cast["reply"], cast["label"]), ("cast", "投屏中…", "猫和老鼠 6"))
        self.assertNotIn("do", cast, "the relay's cast event switches the device, as for the phone")
        # The first page's refs went with it.
        self.assertEqual(self.pick("sp:1:5")["reply"], "这个选项已经过期了")

    def test_pick_and_page_by_voice(self):
        self.say("搜视频猫和老鼠")
        page = self.say("下一页")
        self.assertEqual(page["reply"], "第 6-8 个，共 8 个")
        self.assertEqual(self.say("还有吗")["reply"], "已经是最后一页了")
        self.say("搜视频猫和老鼠")
        self.say("下一页")
        # The leads count from the top (06…); the row on this page works too.
        self.assertEqual(self.say("第七个")["intent"], "cast")
        self.assertEqual(self.relay.calls[-1][2], "BV1000000006")
        self.say("搜视频猫和老鼠")
        self.say("播放第二个")
        self.assertEqual(self.relay.calls[-1][2], "BV1000000001")
        self.say("搜视频猫和老鼠")
        self.assertEqual(self.say("第三十个")["reply"], "没有第 30 个")

    def test_unclear_kind_asks_first(self):
        result = self.say("搜索李子柒")
        self.assertEqual(self.searches(), [], "it does not guess")
        self.assertEqual((result["intent"], result["reply"]), ("ask", "要搜视频，还是搜 UP 主？"))
        self.assertEqual([(c["label"], c["ref"]) for c in result["choices"]],
                         [("搜视频", "sq:1:v"), ("搜 UP 主", "sq:1:u")])
        result = self.pick("sq:1:u")
        self.assertEqual(self.searches(), [("search", "bilibili", "up", "李子柒")])
        self.assertEqual(result["reply"], "找到 8 个B站 UP 主")

    def test_question_answered_by_voice_keeps_the_site(self):
        self.say("油管搜一下lofi girl")
        result = self.say("视频")
        self.assertEqual(self.searches(), [("search", "youtube", "video", "lofi girl")])
        self.assertEqual(result["reply"], "找到 8 个YouTube 视频")
        self.say("搜索罗翔")
        self.say("UP主")
        self.assertEqual(self.searches()[-1], ("search", "bilibili", "up", "罗翔"))
        self.say("搜索罗翔")
        self.say("第二个")
        self.assertEqual(self.searches()[-1], ("search", "bilibili", "up", "罗翔"))

    def test_question_expires(self):
        self.say("搜索罗翔")
        self.now += voice_session.FOLLOW_UP_S + 1
        result = self.say("视频")
        self.assertEqual(self.searches(), [])
        self.assertNotEqual(result["intent"], "search")

    def test_saying_something_else_drops_the_question(self):
        self.say("搜索罗翔")
        self.assertEqual(self.say("湖南卫视")["do"], {"channel": "ch017"})
        self.say("视频")
        self.assertEqual(self.searches(), [])

    def test_ordinals_mean_channels_without_results_on_screen(self):
        self.assertEqual(self.say("第二个")["do"], {"channel": "up01"})
        self.say("搜视频猫和老鼠")
        self.say("湖南卫视")
        self.assertEqual(self.say("第二个")["do"], {"channel": "up01"}, "the results are gone")

    def test_creator_follow_and_latest(self):
        self.say("搜UP主老番茄")
        menu = self.pick("su:1:1")
        self.assertEqual((menu["intent"], menu["reply"]), ("creator", "「老番茄1」：订阅还是看最新？"))
        self.assertEqual([c["ref"] for c in menu["choices"]], ["sf:1:1", "sl:1:1"])
        done = self.pick("sf:1:1")
        self.assertEqual(self.relay.calls[-1], ("follow", "bilibili", "546196", "老番茄1"))
        self.assertEqual((done["intent"], done["reply"]), ("follow", "已订阅「老番茄1」"))
        # The newest upload came with the search: cast it straight away.
        self.say("搜UP主老番茄")
        self.say("第一个")
        self.say("播放最新视频")
        self.assertEqual(self.relay.calls[-1], ("cast", "bilibili", "BV1iqpt6cEMU", "最新的", 2893))
        # Without it the relay finds it.
        self.say("搜UP主老番茄")
        self.pick("su:3:2")
        self.relay.answers["latest"] = {"ok": True, "pending": True}
        result = self.say("第二个")
        self.assertEqual(self.relay.calls[-1], ("latest", "bilibili", "546197", "老番茄2"))
        self.assertEqual(result["reply"], "正在找最新视频，找到就播")

    def test_relay_refusals_are_shown(self):
        self.say("搜UP主老番茄")
        self.pick("su:1:0")
        self.relay.answers["follow"] = {"ok": False, "error": "最多只能订阅 12 个 UP 主"}
        result = self.pick("sf:1:0")
        self.assertEqual((result["intent"], result["reply"]), ("none", "最多只能订阅 12 个 UP 主"))
        self.relay.answers["search"] = {"ok": False, "error": "YouTube 搜索太慢了，稍后再试"}
        self.assertEqual(self.say("油管搜视频lofi")["reply"], "YouTube 搜索太慢了，稍后再试")

    def test_slow_or_missing_relay(self):
        self.relay.slow = True
        self.assertEqual(self.say("油管搜视频lofi")["reply"], "YouTube 搜索太慢了，稍后再试")
        self.assertEqual(self.say("搜视频猫和老鼠")["reply"], "B站搜索太慢了，稍后再试")
        self.relay.slow = False
        self.relay.down = True
        self.assertEqual(self.say("搜视频猫和老鼠")["reply"], voice_session.REPLY_RELAY_DOWN)
        self.voice.relay = None
        self.assertEqual(self.say("搜视频猫和老鼠")["reply"], voice_session.REPLY_NO_RELAY)

    def test_no_time_left_does_not_search(self):
        saved = voice_session.RESULT_BUDGET_S
        voice_session.RESULT_BUDGET_S = 0.5
        try:
            self.assertEqual(self.say("搜视频猫和老鼠")["reply"], "B站搜索太慢了，稍后再试")
        finally:
            voice_session.RESULT_BUDGET_S = saved
        self.assertEqual(self.searches(), [])

    def test_nothing_found(self):
        self.relay.found = 0
        result = self.say("搜视频没有的东西")
        self.assertEqual((result["intent"], result["reply"]), ("none", "B站没搜到「没有的东西」"))
        self.assertNotIn("choices", result)

    def test_stale_refs(self):
        self.say("搜视频猫和老鼠")
        self.say("搜视频熊出没")
        self.assertEqual(self.pick("sv:1:0")["reply"], "这个选项已经过期了")
        self.assertEqual(self.pick("sv:2:99")["reply"], "这个选项已经过期了")
        self.voice.reset()
        self.assertEqual(self.pick("sv:2:0")["reply"], "这个选项已经过期了")
        self.assertEqual([c for c in self.relay.calls if c[0] == "cast"], [])

    def test_results_fit_the_device(self):
        self.relay.answers["search"] = {"ok": True, "results": [
            {"id": f"BV1{n:09d}", "title": "很长的标题" * 20 + "🤓", "uploader": "长" * 50, "duration": 10 ** 6}
            for n in range(20)]}
        result = self.say("搜视频猫和老鼠")
        self.assertEqual(len(result["choices"]), 6)
        raw = encode_result(result)
        self.assertLessEqual(len(raw), VOICE_RESULT_MAX)
        for choice in result["choices"]:
            self.assertLessEqual(len(choice["label"].encode("utf-8")), 47)
            self.assertLessEqual(len(choice["ref"]), 31)


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

    def test_seek_bodies(self):
        client = RelayClient("http://127.0.0.1:9", "key", "Passport")
        sent = []
        client.post = lambda path, body=None: sent.append((path, body)) or {"ok": True}
        client.seek(delta=-30)
        client.seek(position=200)
        client.seek(fraction=0.5)
        client.seek(from_end=30)
        self.assertEqual(sent, [("/api/seek", {"delta": -30}), ("/api/seek", {"position": 200, "within": True}),
                                ("/api/seek", {"fraction": 0.5}), ("/api/seek", {"from_end": 30})])

    def test_unreachable(self):
        client = RelayClient("http://127.0.0.1:9", "key", "Passport", timeout=0.5)
        with self.assertRaises(RelayError):
            client.catalog()


if __name__ == "__main__":
    unittest.main()
