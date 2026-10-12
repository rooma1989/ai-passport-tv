"""语音的会话部分：收设备的 VOICE_CTL / VOICE 包，松开后在工作线程里识别、理解、调中转，
把结果排队，由发送循环像 SWITCH 一样发出去（AVServer._voice_flush）。

发送循环从不等识别：take() 只记下音频，结果由 results() 一次取走。工作线程不碰套接字。

    TV_ASR_SOCKET       muse-asr 的 Unix 套接字（如 /run/muse-asr/asr.sock）；不设就不开语音，
                        CONFIG 里不给 voice，设备按住 OK 时显示「语音现在用不了」
    TV_VOICE_LOG_TEXT   1：日志里写识别出的文字（调阈值时用，调完关掉）；默认只写意图和耗时
    TV_VOICE_WAV_DIR    开发用：把每句话存成 WAV 到这个目录，做测试语料；默认不存，平时不要设

语音搜索（「搜视频猫和老鼠」「搜索李子柒」）在这里记住最近一次搜到的东西：结果页用 ref 翻页、
选中（投屏视频、UP 主再选订阅或播最新），没说清搜什么时追问，面板还在时下一句话可以直接回答
（「视频」「UP主」「第二个」「下一页」「订阅」）。中转那边的接口见 server/relay/voice.py。

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
from .relay_client import DEVICE_ENV, RelayClient, RelayError

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
REPLY_EXPIRED = "这个选项已经过期了"

# ---- 语音搜索 ----
# 设备等一个语音结果 8 秒（固件 AV_VOICE_RESULT_TIMEOUT_MS，从说完或选中算起）；留一秒给发送。
RESULT_BUDGET_S = 7.0
# 剩下的时间不够这么多就不去搜了，直接说太慢。
SEARCH_MIN_S = 1.5
# 追问、结果页、选中 UP 主之后这么久内，下一句话可以直接回答（「视频」「第二个」「下一页」「订阅」）。
FOLLOW_UP_S = 30.0
SEARCH_RESULTS_MAX = 20
REPLY_SEARCH_SLOW = {"bilibili": "B站搜索太慢了，稍后再试", "youtube": "YouTube 搜索太慢了，稍后再试"}
REPLY_SITE_SLOW = {"bilibili": "B站太慢了，稍后再试", "youtube": "YouTube 太慢了，稍后再试"}
REPLY_CASTING = "投屏中…"


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
        # 多用户（TV_DB）：设备不是固定的，认出设备后 bind_device() 按它的 id 造中转客户端。
        self._relay_for: Callable[[str], RelayClient] | None = None
        # 最近一次搜索：{"gen", "site", "kind", "query", "results", "shown", "offset", "index", "at"}。
        # shown 是面板上正显示的（ask 追问、page 结果页、creator 选中的 UP 主，空：都不是了），
        # 只有这时下一句话才当成对它的回答。选项的 ref 带着 gen，换了一次搜索旧的就作废。
        self._search: dict | None = None
        self._search_gen = 0

    @classmethod
    def from_env(cls, logger: Callable[[str], None], environ=os.environ,
                 per_device: bool = False) -> "VoiceService":
        """per_device：多用户（电视服务设置了 TV_DB）。这时一个媒体单元先后服务不同的设备，
        中转里的设备就是这次会话认出来的那台（bind_device），不看 TV_RELAY_DEVICE。"""
        path = environ.get(ASR_SOCKET_ENV)
        asr = AsrClient(path) if path else None
        relay, relay_for = None, None
        try:
            if per_device:
                relay_for = RelayClient.factory_from_env(environ)
                if environ.get(DEVICE_ENV):
                    logger(f"voice: {DEVICE_ENV} ignored with a device database; "
                           "the relay device is the one each session admits")
            else:
                relay = RelayClient.from_env(environ)
        except (OSError, ValueError) as error:
            # 只记类名：路径和密钥都不进日志。
            logger(f"voice: relay disabled ({type(error).__name__})")
        service = cls(asr, relay, logger, log_text=environ.get(LOG_TEXT_ENV) == "1",
                      wav_dir=environ.get(WAV_DIR_ENV) or None)
        service._relay_for = relay_for
        if asr is not None:
            mode = "per device" if relay_for else "on" if relay else "off"
            logger(f"voice: on, relay {mode}, "
                   f"transcripts {'logged' if service.log_text else 'not logged'}")
        return service

    def bind_device(self, device_id: str | None) -> None:
        """多用户：这次会话服务的设备（tv_server 认出设备后调用；未绑定的设备给 None）。

        语音命令的中转动作只作用在这台上。单用户（TV_RELAY_DEVICE 固定）时什么都不做。
        正在跑的工作线程用的是它开始时的那个客户端（_start），不会被换到下一台设备上。"""
        if self._relay_for is None:
            return
        relay = self._relay_for(device_id) if device_id else None
        with self._lock:
            self.relay = relay

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
            self._search = None

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
                self._start(self._recognise, uid, utterance, context(), self.relay)
        elif op == "pick":
            ref = message.get("ref")
            offered = self._refs.get(ref) if isinstance(ref, str) else None
            if offered is None:
                self._push(self._generation, uid, self._final(uid, "", REPLY_EXPIRED, "none"))
                return
            self._start(self._pick, uid, ref, context(), self.relay)
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

    def _recognise(self, generation: int, uid: int, utterance: _Utterance, context: dict,
                   relay: RelayClient | None) -> None:
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
        catalog, relay_note = self._catalog(context, relay)
        # 面板上有搜索的追问或结果时，先看这句话是不是在回答它（「视频」「第二个」「下一页」）。
        step = self._follow_up(text)
        decision = intent.decide(text, catalog) if step is None else None
        if decision is not None and decision.search is not None:
            step = ("search", decision.search)
        if step is not None:
            decision = self._search_step(uid, step, relay, started + RESULT_BUDGET_S)
        result = self._execute(uid, text, decision, catalog, relay)
        heard = f" heard={text!r}" if self.log_text else ""
        self.logger(f"voice id={uid} intent={decision.kind} audio_s={seconds:.1f} "
                    f"dropped={utterance.dropped} chars={len(text)} asr_ms={asr_ms:.0f} "
                    f"total_ms={(time.monotonic() - started) * 1000:.0f}{relay_note}{heard}")
        self._push(generation, uid, result)

    def _pick(self, generation: int, uid: int, ref: str, context: dict,
              relay: RelayClient | None) -> None:
        started = time.monotonic()
        searched = intent.parse_search_ref(ref)
        if searched is not None:
            # 搜索结果、追问、选中的 UP 主（见 intent.py 的「搜索」一节）。
            step = self._step_of_ref(*searched)
            if step is None:
                self._push(generation, uid, self._final(uid, "", REPLY_EXPIRED, "none"))
                return
            decision = self._search_step(uid, step, relay, started + RESULT_BUDGET_S)
            catalog, _ = self._catalog(context, None)
            self.logger(f"voice id={uid} pick intent={decision.kind}")
            self._push(generation, uid, self._execute(uid, "", decision, catalog, relay))
            return
        if ref.startswith("ls:"):
            # 「下一页」或分组概览里的一组：按现在的目录重新列（设备只认这次的结果）。
            catalog, _ = self._catalog(context, relay)
            decision = intent.list_page(ref, catalog)
            if decision is None:
                self._push(generation, uid, self._final(uid, "", REPLY_EXPIRED, "none"))
                return
            result = self._execute(uid, "", decision, catalog, relay)
            self.logger(f"voice id={uid} pick intent={decision.kind}")
            self._push(generation, uid, result)
            return
        found = _REF.match(ref)
        if not found:
            self._push(generation, uid, self._final(uid, "", REPLY_EXPIRED, "none"))
            return
        catalog, _ = self._catalog(context, relay)
        offered = self._refs.get(ref, {})
        series = intent.Series(found.group(1), offered.get("name", found.group(1)))
        decision = intent.Decision("episode", reply=f"从第 {int(found.group(2))} 集开始播",
                                   label=offered.get("label", ""), lead=f"{int(found.group(2)):02d}",
                                   relay=("episode", {"series": series.id, "episode": int(found.group(2))}))
        if catalog.current != series.id:
            decision.then_channel = series.id
        result = self._execute(uid, "", decision, catalog, relay)
        self.logger(f"voice id={uid} pick intent=episode")
        self._push(generation, uid, result)

    def _catalog(self, context: dict, relay: RelayClient | None) -> tuple[intent.Catalog, str]:
        """频道表来自 CONFIG（设备看到的序号和分组）；剧集、全名、网站、正在播第几集来自中转。"""
        config = context.get("config") or {}
        groups = config.get("groups") or []
        listed = config.get("channel_list") or []
        device = context.get("device") or {}
        relay_data, note = {}, ""
        if relay is not None:
            try:
                relay_data = relay.catalog()
            except RelayError as error:
                note = f" relay_catalog={error}"
        relay_rows = [row for key in ("series", "creators") for row in relay_data.get(key) or []
                      if isinstance(row, dict)]
        full_names = {row.get("id"): row.get("name") for row in relay_rows}
        # 中转的台来自哪个网站（「有哪些 YouTube 频道」）；旧版中转不报，就是空的。
        sites = {row.get("id"): row.get("site") for row in relay_rows
                 if row.get("site") in ("bilibili", "youtube")}
        channels = []
        for n, row in enumerate(listed, 1):
            if not isinstance(row, dict) or not isinstance(row.get("id"), str):
                continue
            g = row.get("g")
            group = groups[g] if isinstance(g, int) and 0 <= g < len(groups) else ""
            full = full_names.get(row["id"])
            channels.append(intent.Channel(row["id"], str(row.get("name") or row["id"]), n, group,
                                           full if isinstance(full, str) else "",
                                           sites.get(row["id"], "")))
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

    def _execute(self, uid: int, heard: str, decision: intent.Decision, catalog: intent.Catalog,
                 relay: RelayClient | None) -> dict:
        reply, do = decision.reply, decision.do
        if decision.relay is not None:
            action, params = decision.relay
            if relay is None:
                return self._final(uid, heard, REPLY_NO_RELAY, decision.kind)
            try:
                if action == "episode":
                    answer = relay.play_episode(params["series"], params["episode"])
                elif action == "seek":
                    extra = {k: params[k] for k in ("fraction", "from_end") if k in params}
                    answer = relay.seek(params.get("delta"), params.get("position"), **extra)
                else:
                    answer = getattr(relay, action)()
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
            at = answer.get("position")
            if action == "seek" and ("fraction" in params or "from_end" in params) \
                    and isinstance(at, (int, float)) and not isinstance(at, bool):
                # 「跳到一半」：中转按视频的时长算出了跳到哪，说出来。
                reply = f"跳到 {intent.time_words(int(at))}"
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

    # ---- 语音搜索（intent.py 的「搜索」一节拼面板上的东西，这里记状态、调中转） ----

    def _current_search(self, fresh: bool = False) -> dict | None:
        """最近一次搜索；fresh：只要面板上还在显示它、没过 FOLLOW_UP_S 的。"""
        with self._lock:
            found = self._search
            if found is None or (fresh and (not found["shown"]
                                            or self.clock() - found["at"] > FOLLOW_UP_S)):
                return None
            return found

    def _show(self, found: dict, shown: str, **changes) -> None:
        with self._lock:
            found.update(changes, shown=shown, at=self.clock())

    def _follow_up(self, text: str) -> tuple | None:
        """这句话是不是在回答面板上的搜索追问、结果页或选中的 UP 主；是的话返回要做的一步。
        不是时作废它（用户说了别的，追问不再等回答）。"""
        found = self._current_search(fresh=True)
        if found is None:
            return None
        step = None
        if found["shown"] == "ask":
            answer = intent.search_answer(text)
            if answer is not None:
                step = ("search", {"site": answer[1] or found["site"], "kind": answer[0],
                                   "query": found["query"]})
        elif found["shown"] == "page":
            total, offset = len(found["results"]), found["offset"]
            on_page = total - offset if total - offset <= intent.CHOICES_MAX else intent.PAGE_SIZE
            n = intent.pick_number(text)
            if intent.wants_next_page(text):
                step = ("page", offset + on_page) if offset + on_page < total else ("last",)
            elif n is not None:
                # 行首的号是从头数的（第二页是 06…）；说本页第几个也行。
                index = (n - 1 if offset < n <= offset + on_page else offset + n - 1 if n <= on_page
                         else n - 1)
                step = ("item", index) if index < total else ("missing", n)
        elif found["shown"] == "creator":
            action = intent.creator_answer(text)
            if action is not None:
                step = (action, found["index"])
        if step is None:
            self._show(found, "")
        return step

    def _step_of_ref(self, op: str, generation: int, arg: str) -> tuple | None:
        """设备选中的搜索选项（intent.parse_search_ref）→ 要做的一步；不是这次搜索的为 None。"""
        found = self._current_search()
        if found is None or found["gen"] != generation:
            return None
        if op == "q":
            if arg not in ("v", "u"):
                return None
            return ("search", {"site": found["site"], "kind": "video" if arg == "v" else "up",
                               "query": found["query"]})
        if not arg.isdigit():
            return None
        return {"p": "page", "v": "item", "u": "item", "f": "follow", "l": "latest"}[op], int(arg)

    def _search_step(self, uid: int, step: tuple, relay: RelayClient | None, deadline: float) -> intent.Decision:
        """做搜索的一步，返回要发给设备的决定：搜（或追问）、翻页、选中一个、订阅、播最新。"""
        op = step[0]
        if relay is None:
            return intent.Decision("none", reply=REPLY_NO_RELAY)
        if op == "search":
            return self._run_search(uid, step[1], relay, deadline)
        found = self._current_search()
        if found is None:
            return intent.Decision("none", reply=REPLY_EXPIRED)
        site, kind, results = found["site"], found["kind"], found["results"]
        if op == "last":
            return intent.Decision("none", reply="已经是最后一页了")
        if op == "missing":
            return intent.Decision("none", reply=f"没有第 {step[1]} 个")
        if op == "page":
            offset = step[1] if 0 <= step[1] < len(results) else 0
            self._show(found, "page", offset=offset)
            return intent.search_page(site, kind, found["query"], results, offset, found["gen"])
        index = step[1]
        if not 0 <= index < len(results):
            return intent.Decision("none", reply=REPLY_EXPIRED)
        item = results[index]
        if op == "item" and kind == "video":
            self._show(found, "")
            return self._cast(uid, relay, site, item.get("id"), item.get("title"), item.get("duration"))
        if op == "item":
            self._show(found, "creator", index=index)
            return intent.creator_menu(item, index, found["gen"])
        self._show(found, "")
        latest = item.get("latest") if isinstance(item.get("latest"), dict) else None
        if op == "latest" and latest is not None:
            # B 站的 UP 主搜索顺带给了最新一个能投屏的投稿：不必再问一次。
            return self._cast(uid, relay, site, latest.get("id"), latest.get("title"), latest.get("duration"))
        return self._creator(uid, relay, site, item, op, deadline)

    def _run_search(self, uid: int, request: dict, relay: RelayClient, deadline: float) -> intent.Decision:
        site, kind, query = request["site"], request["kind"], request["query"]
        if not kind:
            # 没说是视频还是 UP 主：追问，记住词和网站。
            with self._lock:
                self._search_gen = self._search_gen % 9999 + 1
                self._search = {"gen": self._search_gen, "site": site, "kind": "", "query": query,
                                "results": [], "shown": "ask", "offset": 0, "index": 0, "at": self.clock()}
                generation = self._search_gen
            return intent.search_question(generation)
        left = deadline - time.monotonic()
        if left < SEARCH_MIN_S:
            return intent.Decision("none", reply=REPLY_SEARCH_SLOW[site])
        try:
            answer = relay.search(site, query, left, kind)
        except RelayError as error:
            self.logger(f"voice id={uid} relay search error={error}")
            return intent.Decision("none", reply=REPLY_SEARCH_SLOW[site] if error.slow else REPLY_RELAY_DOWN)
        if not answer.get("ok"):
            error = answer.get("error")
            return intent.Decision("none", reply=error if isinstance(error, str) and error else "没搜成，稍后再试")
        results = [r for r in answer.get("results") or []
                   if isinstance(r, dict) and isinstance(r.get("id"), str) and r["id"]][:SEARCH_RESULTS_MAX]
        self.logger(f"voice id={uid} search site={site} kind={kind} results={len(results)}")
        with self._lock:
            self._search_gen = self._search_gen % 9999 + 1
            self._search = {"gen": self._search_gen, "site": site, "kind": kind, "query": query,
                            "results": results, "shown": "page" if results else "", "offset": 0,
                            "index": 0, "at": self.clock()}
            generation = self._search_gen
        return intent.search_page(site, kind, query, results, 0, generation)

    def _cast(self, uid: int, relay: RelayClient, site: str, video_id, title, duration) -> intent.Decision:
        """投屏搜到的视频：中转让设备切到「我的投屏」，播完回到原来的台（和手机投屏一样）。"""
        title = title if isinstance(title, str) else ""
        duration = duration if isinstance(duration, int) and not isinstance(duration, bool) else 0
        try:
            answer = relay.cast_video(site, str(video_id or ""), title, duration)
        except RelayError as error:
            self.logger(f"voice id={uid} relay cast error={error}")
            return intent.Decision("none", reply=REPLY_SITE_SLOW[site] if error.slow else REPLY_RELAY_DOWN)
        if not answer.get("ok"):
            error = answer.get("error")
            return intent.Decision("none", reply=error if isinstance(error, str) and error else "没投成，稍后再试")
        shown = answer.get("title") if isinstance(answer.get("title"), str) else title
        return intent.Decision("cast", reply=REPLY_CASTING, label=intent.row_label(shown))

    def _creator(self, uid: int, relay: RelayClient, site: str, item: dict, action: str,
                 deadline: float) -> intent.Decision:
        """订阅搜到的 UP 主（频道），或播他最新的视频。中转来不及做完时先回答「正在…」。"""
        name = item.get("name") if isinstance(item.get("name"), str) else ""
        label = intent.row_label(name)
        left = max(SEARCH_MIN_S, deadline - time.monotonic())
        try:
            answer = relay.creator(site, str(item.get("id") or ""), name, action, left)
        except RelayError as error:
            self.logger(f"voice id={uid} relay creator {action} error={error}")
            return intent.Decision("none", reply=REPLY_SITE_SLOW[site] if error.slow else REPLY_RELAY_DOWN)
        if not answer.get("ok"):
            error = answer.get("error")
            return intent.Decision("none", reply=error if isinstance(error, str) and error else "没做成，稍后再试")
        if action == "latest":
            if answer.get("pending"):
                return intent.Decision("cast", reply="正在找最新视频，找到就播", label=label)
            shown = answer.get("title") if isinstance(answer.get("title"), str) else ""
            return intent.Decision("cast", reply=REPLY_CASTING, label=intent.row_label(shown) or label)
        if answer.get("pending"):
            return intent.Decision("follow", reply="正在订阅，稍后出现在频道列表里", label=label)
        added = answer.get("creator") if isinstance(answer.get("creator"), dict) else {}
        shown = added.get("name") if isinstance(added.get("name"), str) else name
        return intent.Decision("follow", reply=f"已订阅「{intent.row_label(shown)[:12]}」", label=label)

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
