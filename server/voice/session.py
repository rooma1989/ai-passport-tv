"""语音的会话部分：收设备的 VOICE_CTL / VOICE 包，松开后在工作线程里识别、理解、调中转，
把结果排队，由发送循环像 SWITCH 一样发出去（AVServer._voice_flush）。

发送循环从不等识别：take() 只记下音频，结果由 results() 一次取走。工作线程不碰套接字。

    TV_ASR_SOCKET       muse-asr 的 Unix 套接字（如 /run/muse-asr/asr.sock）；不设就不开语音，
                        CONFIG 里不给 voice，设备按住 OK 时显示「语音现在用不了」
    TV_VOICE_LOG_TEXT   1：日志里写识别出的文字（调阈值时用，调完关掉）；默认只写意图和耗时
    TV_VOICE_WAV_DIR    开发用：把每句话存成 WAV 到这个目录，做测试语料；默认不存，平时不要设

另见 asr.py（识别）、intent.py（理解）、relay_client.py（中转）。
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import wave
from pathlib import Path
from typing import Callable

from .. import adpcm
from ..protocol import (AUDIO_BYTES, VOICE_MAX_BLOCKS, VOICE_MAX_S, VOICE_RESULT_MAX, Kind,
                        Packet, ProtocolError, json_object)
from . import intent
from .asr import SAMPLE_RATE, AsrClient, AsrError
from .relay_client import RelayClient, RelayError

ASR_SOCKET_ENV = "TV_ASR_SOCKET"
LOG_TEXT_ENV = "TV_VOICE_LOG_TEXT"
WAV_DIR_ENV = "TV_VOICE_WAV_DIR"
# 结果里「听到的话」最多这么多字：设备顶栏只放得下一行。
HEARD_MAX = 40
REPLY_MAX = 40
_REF = re.compile(r"^ep:(sr\d{1,4}):(\d{1,4})$")
_ID_MAX = 0x7FFFFFFF

REPLY_ASR_DOWN = "服务器没回应，稍后再试"
REPLY_NO_VOICE = "这台服务器没有开语音"
REPLY_NO_RELAY = "这台电视没有连到视频中转"
REPLY_RELAY_DOWN = "视频中转没回应，稍后再试"
REPLY_FORMAT = "不支持这种音频格式"


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit - 1] + "…"


def encode_result(result: dict) -> bytes:
    """结果 JSON（ASCII，与其他控制包一样）；超过 VOICE_RESULT_MAX 时先少给候选，再截短听到的话。"""
    result = dict(result)
    while True:
        raw = json.dumps(result, separators=(",", ":"), ensure_ascii=True).encode("ascii")
        if len(raw) <= VOICE_RESULT_MAX:
            return raw
        if result.get("choices") and len(result["choices"]) > 1:
            result["choices"] = result["choices"][:-1]
        elif len(result.get("heard", "")) > 8:
            result["heard"] = _clip(result["heard"], len(result["heard"]) // 2)
        else:
            raise ValueError("voice result cannot fit")


class _Utterance:
    def __init__(self, uid: int, rate, codec, now: float):
        self.id = uid
        self.ok_format = rate == SAMPLE_RATE and codec == "adpcm"
        self.blocks: list[bytes] = []
        self.dropped = 0
        self.started = now


class VoiceService:
    """每个 AVServer 一个。设备换会话时 reset()，旧会话的工作线程结果作废。"""

    def __init__(self, asr: AsrClient | None, relay: RelayClient | None = None,
                 logger: Callable[[str], None] = lambda message: None,
                 log_text: bool = False, wav_dir: Path | None = None,
                 clock: Callable[[], float] = time.monotonic, threaded: bool = True):
        self.asr = asr
        self.relay = relay
        self.logger = logger
        self.log_text = log_text
        self.wav_dir = Path(wav_dir) if wav_dir else None
        self.clock = clock
        self.threaded = threaded
        self._lock = threading.Lock()
        self._generation = 0
        self._utterance: _Utterance | None = None
        self._results: list[bytes] = []
        self._cancelled: set[int] = set()
        # 最近一次给出的、要服务端去做的候选；pick 只认这些。
        self._refs: dict[str, dict] = {}
        self._unmute: int | None = None
        self._workers: list[threading.Thread] = []
        self.ignored = 0

    @classmethod
    def from_env(cls, logger: Callable[[str], None], environ=os.environ) -> "VoiceService":
        path = environ.get(ASR_SOCKET_ENV)
        asr = AsrClient(path) if path else None
        relay = None
        try:
            relay = RelayClient.from_env(environ)
        except (OSError, ValueError) as error:
            # 只记类名：路径和密钥都不进日志。
            logger(f"voice: relay disabled ({type(error).__name__})")
        service = cls(asr, relay, logger, log_text=environ.get(LOG_TEXT_ENV) == "1",
                      wav_dir=environ.get(WAV_DIR_ENV) or None)
        if asr is not None:
            logger(f"voice: on, relay {'on' if relay else 'off'}, "
                   f"transcripts {'logged' if service.log_text else 'not logged'}")
        return service

    @property
    def enabled(self) -> bool:
        return self.asr is not None

    def config(self) -> dict:
        """CONFIG 里的 voice：只有配置了识别服务才给，设备据此决定按住 OK 是不是说话。"""
        return {"voice": {"max_s": VOICE_MAX_S}} if self.enabled else {}

    def reset(self) -> None:
        with self._lock:
            self._generation += 1
            self._utterance = None
            self._results.clear()
            self._cancelled.clear()
            self._refs.clear()

    def results(self) -> list[bytes]:
        """取走排好的结果（编码好的 JSON）；发送循环每轮调用，不阻塞。"""
        with self._lock:
            taken, self._results = self._results, []
        return taken

    def wait_idle(self, timeout: float = 5.0) -> None:
        """测试用：等工作线程都结束。"""
        deadline = time.monotonic() + timeout
        for worker in list(self._workers):
            worker.join(max(0.0, deadline - time.monotonic()))

    # ---- 收包（发送循环的线程里调用，必须快） ----

    def take(self, packet: Packet, context: Callable[[], dict]) -> None:
        if packet.kind == Kind.VOICE:
            self._take_audio(packet)
        elif packet.kind == Kind.VOICE_CTL:
            self._take_control(json_object(packet.payload), context)
        else:
            raise ProtocolError("not a voice packet")

    def _take_audio(self, packet: Packet) -> None:
        utterance = self._utterance
        if utterance is None or len(packet.payload) != AUDIO_BYTES:
            self.ignored += 1
            return
        if len(utterance.blocks) >= VOICE_MAX_BLOCKS:
            utterance.dropped += 1
            return
        utterance.blocks.append(packet.payload)

    def _take_control(self, message: dict, context: Callable[[], dict]) -> None:
        op, uid = message.get("op"), message.get("id")
        if not isinstance(op, str) or type(uid) is not int or not 0 <= uid <= _ID_MAX:
            self.ignored += 1
            return
        now = self.clock()
        if op == "begin":
            with self._lock:
                previous = self._utterance
                if previous is not None:
                    self._cancelled.add(previous.id)
                if len(self._cancelled) > 64:
                    # 只有最近几句话的结果还可能在路上；更早的 id 不必再记。
                    self._cancelled = set(sorted(self._cancelled)[-32:])
                self._utterance = _Utterance(uid, message.get("rate"), message.get("codec"), now)
        elif op == "cancel":
            with self._lock:
                self._cancelled.add(uid)
                if self._utterance is not None and self._utterance.id == uid:
                    self._utterance = None
            self.logger(f"voice id={uid} cancelled")
        elif op == "end":
            utterance = self._utterance
            if utterance is None or utterance.id != uid:
                self.ignored += 1
                return
            self._utterance = None
            if not self.enabled:
                self._push(self._generation, uid, self._error(uid, REPLY_NO_VOICE))
            elif not utterance.ok_format:
                self._push(self._generation, uid, self._error(uid, REPLY_FORMAT))
            else:
                self._start(self._recognise, uid, utterance, context())
        elif op == "pick":
            ref = message.get("ref")
            offered = self._refs.get(ref) if isinstance(ref, str) else None
            if offered is None:
                self._push(self._generation, uid, self._final(uid, "", "这个选项已经过期了", "none"))
                return
            self._start(self._pick, uid, ref, context())
        else:
            self.ignored += 1

    # ---- 工作线程 ----

    def _start(self, target, uid: int, *args) -> None:
        generation = self._generation
        if not self.threaded:
            target(generation, uid, *args)
            return
        worker = threading.Thread(target=self._guarded, args=(target, generation, uid) + args,
                                  name=f"voice-{uid}", daemon=True)
        self._workers = [w for w in self._workers if w.is_alive()] + [worker]
        worker.start()

    def _guarded(self, target, generation: int, uid: int, *args) -> None:
        try:
            target(generation, uid, *args)
        except Exception as error:      # 一句话出错不能拖垮会话
            self.logger(f"voice id={uid} failed: {type(error).__name__}")
            self._push(generation, uid, self._error(uid, REPLY_ASR_DOWN))

    def _push(self, generation: int, uid: int, result: dict) -> None:
        payload = encode_result(result)
        with self._lock:
            if generation != self._generation or uid in self._cancelled:
                return
            self._results.append(payload)

    @staticmethod
    def _error(uid: int, reply: str) -> dict:
        return {"id": uid, "state": "error", "reply": reply}

    @staticmethod
    def _final(uid: int, heard: str, reply: str, kind: str) -> dict:
        result = {"id": uid, "state": "final", "reply": _clip(reply, REPLY_MAX), "intent": kind}
        if heard:
            result["heard"] = _clip(heard, HEARD_MAX)
        return result

    def _recognise(self, generation: int, uid: int, utterance: _Utterance, context: dict) -> None:
        started = time.monotonic()
        pcm = b"".join(adpcm.decode_block(block) for block in utterance.blocks)
        seconds = len(pcm) / 2 / SAMPLE_RATE
        self._save_wav(uid, pcm)
        try:
            text = self.asr.transcribe(pcm, SAMPLE_RATE) if pcm else ""
        except AsrError as error:
            self.logger(f"voice id={uid} audio_s={seconds:.1f} asr_error={error}")
            self._push(generation, uid, self._error(uid, REPLY_ASR_DOWN))
            return
        asr_ms = (time.monotonic() - started) * 1000
        catalog, relay_note = self._catalog(context)
        decision = intent.decide(text, catalog)
        result = self._execute(uid, text, decision, catalog)
        heard = f" heard={text!r}" if self.log_text else ""
        self.logger(f"voice id={uid} intent={decision.kind} audio_s={seconds:.1f} "
                    f"dropped={utterance.dropped} chars={len(text)} asr_ms={asr_ms:.0f} "
                    f"total_ms={(time.monotonic() - started) * 1000:.0f}{relay_note}{heard}")
        self._push(generation, uid, result)

    def _pick(self, generation: int, uid: int, ref: str, context: dict) -> None:
        found = _REF.match(ref)
        if not found:
            self._push(generation, uid, self._final(uid, "", "这个选项已经过期了", "none"))
            return
        catalog, _ = self._catalog(context)
        offered = self._refs.get(ref, {})
        series = intent.Series(found.group(1), offered.get("name", found.group(1)))
        decision = intent.Decision("episode", reply=f"从第 {int(found.group(2))} 集开始播",
                                   label=offered.get("label", ""), lead=f"{int(found.group(2)):02d}",
                                   relay=("episode", {"series": series.id, "episode": int(found.group(2))}))
        if catalog.current != series.id:
            decision.then_channel = series.id
        result = self._execute(uid, "", decision, catalog)
        self.logger(f"voice id={uid} pick intent=episode")
        self._push(generation, uid, result)

    def _catalog(self, context: dict) -> tuple[intent.Catalog, str]:
        """频道表来自 CONFIG（设备看到的序号和分组）；剧集、全名、正在播第几集来自中转。"""
        config = context.get("config") or {}
        groups = config.get("groups") or []
        listed = config.get("channel_list") or []
        device = context.get("device") or {}
        relay_data, note = {}, ""
        if self.relay is not None:
            try:
                relay_data = self.relay.catalog()
            except RelayError as error:
                note = f" relay_catalog={error}"
        full_names = {row.get("id"): row.get("name") for key in ("series", "creators")
                      for row in relay_data.get(key) or [] if isinstance(row, dict)}
        channels = []
        for n, row in enumerate(listed, 1):
            if not isinstance(row, dict) or not isinstance(row.get("id"), str):
                continue
            g = row.get("g")
            group = groups[g] if isinstance(g, int) and 0 <= g < len(groups) else ""
            full = full_names.get(row["id"])
            channels.append(intent.Channel(row["id"], str(row.get("name") or row["id"]), n, group,
                                           full if isinstance(full, str) else ""))
        listed_ids = {c.id for c in channels}
        series = [intent.Series(row["id"], str(row.get("name") or row["id"]),
                                row.get("total") if isinstance(row.get("total"), int) else 0)
                  for row in relay_data.get("series") or []
                  if isinstance(row, dict) and row.get("id") in listed_ids]
        playing = relay_data.get("playing") if isinstance(relay_data.get("playing"), dict) else {}
        current = str(context.get("current") or "")

        def number(key):
            value = device.get(key)
            return value if isinstance(value, int) and not isinstance(value, bool) else None

        catalog = intent.Catalog(
            channels, series, current,
            episode=playing.get("episode") if playing.get("channel") == current
            and isinstance(playing.get("episode"), int) else 0,
            paused=bool(playing.get("paused")) and playing.get("channel") == current,
            volume=number("volume"), brightness=number("brightness"), steps=number("steps"),
            audio_only=number("audio_only"), unmute_volume=self._unmute)
        return catalog, note

    def _execute(self, uid: int, heard: str, decision: intent.Decision, catalog: intent.Catalog) -> dict:
        reply, do = decision.reply, decision.do
        if decision.relay is not None:
            action, params = decision.relay
            if self.relay is None:
                return self._final(uid, heard, REPLY_NO_RELAY, decision.kind)
            try:
                if action == "episode":
                    answer = self.relay.play_episode(params["series"], params["episode"])
                elif action == "seek":
                    answer = self.relay.seek(params.get("delta"), params.get("position"))
                else:
                    answer = getattr(self.relay, action)()
            except RelayError as error:
                self.logger(f"voice id={uid} relay {action} error={error}")
                return self._final(uid, heard, REPLY_RELAY_DOWN, decision.kind)
            if not answer.get("ok"):
                error = answer.get("error")
                return self._final(uid, heard, error if isinstance(error, str) and error
                                   else "没做成，稍后再试", decision.kind)
            note = answer.get("note")
            if action == "episode" and isinstance(note, str) and note:
                reply = note
            if decision.then_channel and not answer.get("cut"):
                do = {"channel": decision.then_channel}
        result = self._final(uid, heard, reply, decision.kind)
        if do is not None:
            result["do"] = do
            if do.get("volume") == 0 and catalog.volume:
                self._unmute = catalog.volume
        if decision.choices:
            result["choices"] = decision.choices
            with self._lock:
                self._refs = {c["ref"]: {"label": c.get("label", "")}
                              for c in decision.choices if "ref" in c}
        if decision.label:
            result["label"] = decision.label
        if decision.lead:
            result["lead"] = decision.lead
        return result

    def _save_wav(self, uid: int, pcm: bytes) -> None:
        if self.wav_dir is None or not pcm:
            return
        try:
            self.wav_dir.mkdir(parents=True, exist_ok=True)
            name = self.wav_dir / f"voice-{time.strftime('%Y%m%d-%H%M%S')}-{uid}.wav"
            with wave.open(str(name), "wb") as out:
                out.setnchannels(1)
                out.setsampwidth(2)
                out.setframerate(SAMPLE_RATE)
                out.writeframes(pcm)
        except OSError as error:
            self.logger(f"voice id={uid} wav not saved: {type(error).__name__}")
