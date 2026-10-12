"""面向直播频道的单客户端局域网服务器，仅依赖标准库，由 ffmpeg 转码。"""

from __future__ import annotations

import argparse
import errno
import hmac
import ipaddress
import os
import re
import secrets
import select
import signal
import socket
import stat
import sys
import threading
import time
from pathlib import Path

import enum
from typing import Any, Dict, List

class SessionState(str, enum.Enum):
    PLAYING = "PLAYING"
    STARVED_REBUFFERING = "STARVED_REBUFFERING"
    RECOVERING = "RECOVERING"
    CLOSED = "CLOSED"

from . import frames, live, netident
from .timeline import SourceState
from .rate import ADAPTIVE, ByteRate
from .live import (CHANNELS, CHANNEL_AGENTS, DEFAULT_CHANNEL, LiveChannel, LiveError,
                   AUDIO_MAX_LOOKAHEAD_MS, PREBUFFER_TIMEOUT_S, channel_list)
# 时序常量统一来自 media.py，live.py 同样从那里导入。
from .media import (AUDIO_CHUNK_MS, AUDIO_LEAD_MS, DURATION_MS, FPS,
                    START_DELAY_MS, VIDEO_LEAD_MS, schedule)
from .protocol import (AUDIO_PCM_BYTES, IO_TIMEOUT, VIDEO_CONTINUES, Kind, Packet,
                       ProtocolError, json_bytes, json_object, receive_packet,
                       send_packet, _write_slice)
from . import now_playing, reconnect
from .cast_switch import SwitchWatcher
from .tenancy import gate as tenancy_gate, unbound as tenancy_unbound

# 替代 --token-file 的环境变量名；只有令牌值需与固件一致，变量名无需一致。
TOKEN_ENV = "TV_PAIRING_TOKEN"
# 取值均为发送端实际使用的常量，不另存副本。
CONFIG = {"width": frames.WIDTH, "height": frames.HEIGHT, "fps": FPS,
          "sample_rate": 16000,
          "channels": 1, "sample_bits": 16, "audio_codec": "ima_adpcm",
          "audio_chunk_ms": AUDIO_CHUNK_MS,
          "video_max_bytes": frames.VIDEO_MAX,
          # 设备据此校验自身几何；两端不一致时拒绝通信，避免条带画到错误的行。
          "stripe_rows": frames.STRIPE_ROWS,
          "duration_ms": DURATION_MS,
          "start_delay_ms": START_DELAY_MS, "audio_lead_ms": AUDIO_LEAD_MS,
          "video_lead_ms": VIDEO_LEAD_MS}
PRIVATE_NETWORKS = tuple(ipaddress.ip_network(net) for net in
                         ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8"))

# 单帧写入时限（秒），超时即放弃会话；须小于设备约 3 秒无数据即断开的阈值。
FRAME_WRITE_DEADLINE_S = float(os.environ.get("TV_FRAME_DEADLINE_S", "2.5"))

# 套接字发送缓冲区大小；见 CLAUDE.md 关键设计决策 4。
SEND_BUFFER_BYTES = int(os.environ.get("TV_SEND_BUFFER", "16384"))

# 画面包每次写入的分片大小；分片之间让音频先发，见 CLAUDE.md 核心设计原则 2。
VIDEO_SLICE_BYTES = 4096

# 单次写入超过该秒数时记录其大小与所在位置；0 表示每次都记录。
PROBE_SLOW_S = float(os.environ.get("TV_PROBE_SLOW_S", "0.05"))

# 新会话等待首批媒体的时长，超时后设备停留在重连提示画面。
STARTUP_WAIT_S = min(PREBUFFER_TIMEOUT_S, 10)


WILDCARD_BIND = "0.0.0.0"


def local_ipv4(address: str) -> bool:
    try:
        ip = ipaddress.IPv4Address(address)
    except ipaddress.AddressValueError:
        return False
    return any(ip in network for network in PRIVATE_NETWORKS)


# 公网接入开关。默认只服务局域网设备；部署在云服务器上时设为 1，也接受公网地址的设备。
# 打开它必须同时配置配对令牌，否则服务拒绝启动：没有令牌的公网端口谁扫到谁就能用你的带宽。
ALLOW_PUBLIC_ENV = "TV_ALLOW_PUBLIC"
# 视频中转的投屏事件文件（server/relay/events.py）；不设则不做投屏切台。
RELAY_EVENTS_ENV = "TV_RELAY_EVENTS_FILE"
NOW_PLAYING_ENV = "TV_NOW_PLAYING_FILE"
# 中转写的手机遥控地址（大写，二维码用字母数字模式），随 CONFIG 发给设备画在状态页上。
REMOTE_URL_ENV = "TV_REMOTE_URL_FILE"
REMOTE_URL_MAX = 64
# 设备在 HELLO 里报的上次复位原因（esp_reset_reason_t），日志里写名字。
_RESET_REASONS = {1: "POWERON", 2: "EXT", 3: "SW", 4: "PANIC", 5: "INT_WDT", 6: "TASK_WDT",
                  7: "WDT", 8: "DEEPSLEEP", 9: "BROWNOUT", 10: "SDIO", 11: "USB", 12: "JTAG",
                  13: "EFUSE", 14: "PWR_GLITCH", 15: "CPU_LOCKUP"}
# 开机这么久以内的第一次连接，即使服务刚重启也算作「设备刚开机」。
FRESH_BOOT_S = 120


def reset_reason_name(code: int) -> str:
    return _RESET_REASONS.get(code, str(code))
# 暂停最长多久，之后自动继续（与中转的 PAUSE_MAX_S 一致）。
PAUSE_MAX_S = 30 * 60
_REMOTE_URL = re.compile(r"^HTTPS?://[A-Z0-9.:/%-]+$")


def peer_allowed(address: str, allow_public: bool) -> bool:
    return allow_public or local_ipv4(address)


def validate_token(token: str) -> bytes:
    if not isinstance(token, str) or not 16 <= len(token) <= 128:
        raise ValueError("pairing token must be 16..128 printable ASCII characters")
    if any(not 33 <= ord(char) <= 126 for char in token):
        raise ValueError("pairing token must be 16..128 printable ASCII characters")
    return token.encode("ascii")


def load_token(path: Path | None = None) -> bytes | None:
    """返回配对令牌；未配置时返回 None（不校验 HELLO 内容，由调用方提示）。"""
    environment = os.environ.get(TOKEN_ENV)
    if path is not None and environment is not None:
        raise ValueError("choose either token environment or token file")
    if path is None:
        if environment is None:
            return None
        return validate_token(environment)
    flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or
                stat.S_IMODE(info.st_mode) not in (0o400, 0o600)):
            raise ValueError("token file must be owner-only, regular, mode 0400 or 0600")
        raw = stream.read(130)
    # 允许一个末尾 LF，读取长度有上限。
    try:
        return validate_token(raw.removesuffix(b"\n").decode("ascii"))
    except UnicodeError:
        raise ValueError("token file must contain printable ASCII") from None


def authenticate(packet: Packet, token: bytes | None) -> None:
    """校验首个 HELLO；配置了令牌时同时校验令牌。"""
    if (packet.kind != Kind.HELLO or packet.session != 0 or
            packet.seq != 0 or packet.pts_ms != 0):
        raise ProtocolError("expected initial HELLO")
    hello = json_object(packet.payload)
    if type(hello.get("version")) is not int or hello["version"] != 1:
        raise ProtocolError("unsupported HELLO version")
    if token is None:
        # 未配置令牌：不检查 HELLO 内容，但上面已校验 HELLO 形状。
        return
    supplied = hello.get("token")
    if not isinstance(supplied, str):
        raise ProtocolError("authentication failed")
    try:
        supplied_bytes = validate_token(supplied)
    except ValueError:
        raise ProtocolError("authentication failed") from None
    if not hmac.compare_digest(supplied_bytes, token):
        raise ProtocolError("authentication failed")


class _MediaChannel:
    def __init__(self, media, *args, **kwargs):
        from .timeline import ContentTimeline, SessionClock, BASIS_COMMON_DECODE
        self.media = media
        self.palette = media.palette if hasattr(media, "palette") else bytes(768)
        self.fps = FPS
        self.url = ""
        self.timeline = ContentTimeline()
        self.timeline.calibrate(0.0, 0.0, basis=BASIS_COMMON_DECODE)
        self.failed = False
        self.dropped_video = 0
        self.skipped_audio = 0
        self.audio = [media.pcm[i:i+AUDIO_PCM_BYTES] for i in range(0, len(media.pcm), AUDIO_PCM_BYTES)] if hasattr(media, "pcm") else []
        self.video = list(media.frames) if hasattr(media, "frames") else []
        self.audio_content = [i * AUDIO_CHUNK_MS for i in range(len(self.audio))]
        self.video_content = [i * (1000.0 / FPS) for i in range(len(self.video))]
        self.session_clock = SessionClock(chunk_ms=AUDIO_CHUNK_MS)
        self.lock = threading.Lock()

    def build_palette(self, timeout=None):
        return self.palette

    def start(self):
        pass

    def stop(self):
        pass

    def has_data(self):
        return bool(self.audio or self.video)

    def prebuffered(self):
        return True

    def audio_pending(self):
        return bool(self.audio)

    def video_pending(self):
        return bool(self.video)

    def pop_audio(self):
        if self.audio:
            from . import adpcm
            chunk = self.audio.pop(0)
            if not hasattr(self, "_encoder"):
                self._encoder = adpcm.Encoder()
            wire = self._encoder.encode_pcm(chunk)
            ts = self.audio_content.pop(0)
            return wire, int(ts)
        return None

    def pop_video(self, keep=0):
        if self.video:
            chunk = self.video.pop(0)
            ts = self.video_content.pop(0)
            p = frames.frame_packets(chunk) if isinstance(chunk, bytes) else chunk
            return p, int(ts)
        return None

    def failure(self):
        return None

    def close(self):
        pass

    def picture_lag_s(self):
        return 0.0

    def diagnostics(self):
        return ""


class AVServer:
    """单线程负责接入与收发，多余连接直接关闭；阻塞操作均带时限，每 50 ms 轮询停止标志。"""

    def __init__(self, *args, **kwargs):
        is_legacy = False
        if len(args) >= 1:
            if not isinstance(args[0], (bytes, bytearray, memoryview)) and args[0] is not None:
                is_legacy = True
            elif args[0] is None and len(args) >= 4:
                is_legacy = True
            elif args[0] is None and len(args) >= 2 and isinstance(args[1], (bytes, bytearray)):
                is_legacy = True

        if is_legacy:
            self.media = args[0] if len(args) > 0 else kwargs.get("media")
            token = args[1] if len(args) > 1 else kwargs.get("token")
            bind = args[2] if len(args) > 2 else kwargs.get("bind", "127.0.0.1")
            port = kwargs.get("port", args[3] if len(args) > 3 else 8096)
            self.duration_ms = kwargs.get("duration_ms", args[4] if len(args) > 4 else 1800000)
            logger = kwargs.get("logger") or (args[5] if len(args) > 5 else None)
            self.live_enabled = self.media is None
        else:
            self.media = kwargs.get("media")
            token = args[0] if len(args) > 0 else kwargs.get("token")
            bind = args[1] if len(args) > 1 else kwargs.get("bind", "127.0.0.1")
            port = kwargs.get("port", args[2] if len(args) > 2 else 8096)
            self.duration_ms = kwargs.get("duration_ms", 1800000)
            logger = kwargs.get("logger") or (args[3] if len(args) > 3 else None)
            self.live_enabled = self.media is None
        if bind != WILDCARD_BIND and not local_ipv4(bind):
            raise ValueError("bind must be 0.0.0.0 or a loopback or RFC1918 IPv4 address")
        if token is not None:
            # 构造时即校验，格式错误的令牌在此报错；None 表示不校验。
            validate_token(token.decode("ascii"))
        self.token = token
        self.allow_public = os.environ.get(ALLOW_PUBLIC_ENV, "0") == "1"
        # 多用户：设置了设备数据库（TV_DB）就按设备 id 和密钥认设备（server/tenancy/gate.py）。
        tenancy_on = bool(os.environ.get(tenancy_gate.DB_ENV))
        if self.allow_public and token is None and not tenancy_on:
            raise ValueError(f"{ALLOW_PUBLIC_ENV}=1 requires a pairing token "
                             f"({TOKEN_ENV} or --token-file) or a device database "
                             f"({tenancy_gate.DB_ENV})")
        self.bind, self.port = bind, port
        self.stop = threading.Event()
        self.ready = threading.Event()
        self.listener: socket.socket | None = None
        self.completed = self.rejected = self.failed = self.dropped_video = 0
        # 握手阶段即关闭的连接（换频道、设备休眠、端口探测）单独计数，不计入 failed。
        self.abandoned = 0
        self.audio_sent = self.video_sent = 0
        self.session_id = 0
        self.phase = "idle"
        self.live_channel: LiveChannel | None = None
        # 空串发送音视频；audio 或 video 仅用于诊断，见 _pace_live。
        self.media_filter = ""
        self.channel_name = ""
        # 投屏切台：中转写事件文件，会话中每秒看一次；只对声明支持切台的设备发 SWITCH。
        events_file = os.environ.get(RELAY_EVENTS_ENV)
        self.switch_watcher = SwitchWatcher(Path(events_file) if events_file else None)
        self.device_switches = False
        self.device_status: dict | None = None
        self.device_pause = False
        self.device_hints = False
        # 只听声音的设备：这次会话不发画面（HELLO 里 "audio_only": 1）。
        self.device_audio_only = False
        # 上次握手时设备报的开机时长；变小说明设备重启过。
        self._device_uptime: int | None = None
        # 推流以外的阶段（起播等待、断流重连、两次会话之间）收到的换台，留到设备下次连上时给它。
        self.pending_switch = ""
        # 给手机网页看的「正在播放」；不设则不写。
        playing_file = os.environ.get(NOW_PLAYING_ENV)
        self.now_playing_path = Path(playing_file) if playing_file else None
        remote_file = os.environ.get(REMOTE_URL_ENV)
        self.remote_url_path = Path(remote_file) if remote_file else None
        self.live_note = ""
        self.ffmpeg = "ffmpeg"
        # 可注入，便于不依赖 ffmpeg 和网络测试发送循环。
        if is_legacy and args[0] is not None:
            self.channel_factory = lambda *a, **k: _MediaChannel(args[0], *a, **k)
        else:
            self.channel_factory = LiveChannel
        self.frame_deadline_s = FRAME_WRITE_DEADLINE_S
        self.logger = logger or (lambda message: None)
        self.tenancy = tenancy_gate.from_env(self.logger) if tenancy_on else None
        self.session_state: SessionState = SessionState.CLOSED
        self.state_history: List[Dict[str, Any]] = []
        self.session_limit_s: float | None = None
        self.fault_injector: Any | None = None

    def set_session_state(self, state: SessionState, session: int, reason: str = "", **kwargs: Any) -> None:
        """记录会话状态迁移及当时的发送计数。"""
        old_state = getattr(self, "session_state", SessionState.CLOSED)
        self.session_state = state
        now_m = time.monotonic()
        rec = {
            "timestamp": round(now_m, 4),
            "session": session,
            "from_state": old_state.value if isinstance(old_state, SessionState) else str(old_state),
            "to_state": state.value if isinstance(state, SessionState) else str(state),
            "reason": reason,
            "audio_sent": getattr(self, "audio_sent", 0),
            "video_sent": getattr(self, "video_sent", 0),
            "dropped_video": getattr(self, "dropped_video", 0),
            **kwargs
        }
        if not hasattr(self, "state_history") or self.state_history is None:
            self.state_history = []
        self.state_history.append(rec)
        detail_str = " ".join(f"{k}={v}" for k, v in kwargs.items())
        self.logger(
            f"session={session} STATE_CHANGE {rec['from_state']} -> {rec['to_state']} "
            f"reason='{reason}' {detail_str}".strip()
        )

    def serve(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            self.listener = listener
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind((self.bind, self.port))
            self.port = listener.getsockname()[1]
            # 设备每秒重试一次；服务单个连接期间需留有积压队列，否则重试被直接拒绝。
            listener.listen(8)
            listener.setblocking(False)
            self.ready.set()
            while not self.stop.is_set():
                if not select.select([listener], [], [], 0.05)[0]:
                    continue
                connection, peer = listener.accept()
                with connection:
                    if not peer_allowed(peer[0], self.allow_public):
                        self.rejected += 1
                        continue
                    connection.setblocking(False)
                    connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                    # 发送缓冲区只需容纳一个画面包；见 CLAUDE.md 关键设计决策 4。
                    connection.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF,
                                          SEND_BUFFER_BYTES)

                    connection.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
                    self.session_id = self.audio_sent = self.video_sent = 0
                    outcome, reason = "closed", "none"
                    self.phase = "hello_receive"
                    try:
                        self._session(connection)
                        self.completed += 1
                    except (OSError, EOFError, ProtocolError, LiveError) as error:
                        # 写入可能不完整，必须关闭连接；日志只含异常类名，不含载荷、令牌、对端地址。
                        # 鉴权之前断开计为放弃，之后断开计为失败。
                        if self.session_id == 0:
                            self.abandoned += 1
                            outcome = "abandoned"
                        else:
                            self.failed += 1
                            outcome = "failed"
                        reason = type(error).__name__
                        # 不在此补采 ffmpeg 诊断：_live_session 的 finally 已记录并清空 live_channel。
                    except Exception as error:
                        if self.session_id == 0:
                            self.abandoned += 1
                            outcome = "abandoned"
                        else:
                            self.failed += 1
                            outcome = "failed"
                        reason = type(error).__name__
                        import traceback
                        tb = traceback.extract_tb(error.__traceback__)
                        sanitized_tb = " -> ".join([f"{Path(f.filename).name}:{f.lineno}:{f.name}" for f in tb[-3:]])
                        self.live_note = f"unexpected={type(error).__name__}[{sanitized_tb}]"
                    finally:
                        # 仅记录代码内定义的阶段名与异常类名，不记录 str/repr(error)。
                        note = f" note={self.live_note}" if self.live_note else ""
                        self.logger(f"session={self.session_id} state={outcome} "
                                    f"reason={reason} phase={self.phase} "
                                    f"authenticated={int(self.session_id != 0)} "
                                    f"audio_sent={self.audio_sent} video_sent={self.video_sent}{note}")
                        self.live_note = ""
                # 拒绝旧会话期间积压的连接，不影响后续会话。
                self._reject_waiting()
        self.listener = None

    def _reject_waiting(self) -> None:
        assert self.listener is not None
        # 即使连接洪泛也限定处理次数。
        for _ in range(8):
            try:
                connection, _ = self.listener.accept()
            except BlockingIOError:
                return
            connection.close()
            self.rejected += 1

    def _device_left(self, connection: socket.socket, session: int) -> bool:
        """设备已结束本会话时返回 True：发来 END，或直接关闭连接。

        写入前及一帧的多次写入之间调用，使正常换频道不被计为错误。收到 END 以外的包抛 ProtocolError；
        无 END 的断开（EOFError）原样抛出，由 serve() 按握手是否完成分类。
        """
        if not select.select([connection], [], [], 0)[0]:
            return False
        try:
            packet = receive_packet(connection, IO_TIMEOUT, expected_session=session)
        except TimeoutError:
            # select 报告可读但数据不足一个完整包：设备仍在，继续会话。
            return False
        except EOFError:
            # 无 END 的断开原样抛出，由 serve() 分类。
            raise
        if packet.kind == Kind.STATUS:
            self._take_status(packet)
            return False
        if packet.kind != Kind.END:
            raise ProtocolError("only END or STATUS allowed while playing")
        return True

    def note_device_boot(self, greeting: dict, now: float | None = None) -> None:
        """设备重启了就记一行：用来分辨「Wi-Fi 抖了一下」和「设备自己重启了（原因）」。

        和「上次 HELLO 的开机时长 + 之后过去的时间」比，而不是只和上次的开机时长比：
        一次会话可能放了十几分钟，期间重启的设备再连上时开机时长早已超过上次 HELLO 里的数。"""
        uptime, reason = greeting.get("uptime_s"), greeting.get("reset_reason")
        if not isinstance(uptime, int):
            return
        now = time.monotonic() if now is None else now
        last, last_at = self._device_uptime, getattr(self, "_device_uptime_at", now)
        self._device_uptime, self._device_uptime_at = uptime, now
        expected = None if last is None else last + (now - last_at)
        # 留 5 秒余量：两端时钟和连接耗时都有误差。
        if (last is None and uptime < FRESH_BOOT_S) or (expected is not None and uptime < expected - 5):
            name = reset_reason_name(reason) if isinstance(reason, int) else "?"
            self.logger(f"device boot reason={name}({reason}) uptime={uptime}s")

    def note_device_wifi(self, wifi) -> None:
        """设备连的哪个 Wi-Fi、信号多强、哪个信道、发射功率多少：卡顿时对照日志用。"""
        if not isinstance(wifi, dict):
            return
        parts = []
        for key in ("ssid", "rssi", "ch", "tx"):
            value = wifi.get(key)
            if isinstance(value, (str, int, float)) and not isinstance(value, bool):
                parts.append(f"{key}={str(value)[:32]}")
        if parts:
            self.logger("device wifi " + " ".join(parts))

    def _take_status(self, packet: Packet) -> None:
        """设备的音量或亮度变了：记下来给手机看，会话照常。也每 30 秒报一次信号。"""
        report = json_object(packet.payload)
        rssi = report.get("rssi")
        if isinstance(rssi, int) and not isinstance(rssi, bool) and rssi:
            self.logger(f"device rssi={rssi}")
        if self.device_status is not None:
            for key in ("volume", "brightness", "pet"):
                if isinstance(report.get(key), int):
                    self.device_status[key] = report[key]
            self._write_now_playing(True)

    def keep_now_playing(self) -> None:
        """会话还在就每 REFRESH_S 秒刷新一次「正在播放」。推流、起播等待、断流重连、暂停
        都要调用：哪一处漏了，手机就会在 30 秒后把还连着的设备当成离线。"""
        now = time.monotonic()
        if now - getattr(self, "_now_playing_at", 0.0) >= now_playing.REFRESH_S:
            self._now_playing_at = now
            self._write_now_playing(True)

    def _write_now_playing(self, connected: bool) -> None:
        now_playing.write(self.now_playing_path, self.channel_name, connected, self.device_switches,
                          since=getattr(self, "_session_since", 0.0), device=self.device_status)

    def _wait_until(self, connection: socket.socket, target: float, session: int) -> bool:
        self.phase = "schedule_wait"
        while not self.stop.is_set():
            delay = max(0.0, min(0.05, target - time.monotonic()))
            watch = [connection]
            if self.listener is not None:
                watch.append(self.listener)
            readable = select.select(watch, [], [], delay)[0]
            if self.listener is not None and self.listener in readable:
                self._reject_waiting()
            if connection in readable:
                self.phase = "control_receive"
                try:
                    packet = receive_packet(connection, expected_session=session)
                except TimeoutError:
                    # select 报告可读但数据尚不可取：不是对端错误，继续等待。
                    self.phase = "schedule_wait"
                    continue
                if packet.kind == Kind.STATUS:
                    # 等待、重连、暂停中也可能调音量：记下后接着等。
                    self._take_status(packet)
                    self.phase = "schedule_wait"
                    continue
                if packet.kind != Kind.END:
                    raise ProtocolError("only END or STATUS allowed after HELLO")
                return False
            if time.monotonic() >= target:
                return True
        return False

    def pause_verdict(self, paused_at: float) -> str:
        """暂停中每轮调用：继续（resume）、结束会话（leave，有换台请求）或接着暂停（paused）。"""
        decision = self.switch_watcher.poll(self.channel_name, time.monotonic())
        if decision is not None:
            kind, target = decision
            if kind == "switch":
                self.pending_switch = target
            self.logger(f"paused: cast {kind} {target or self.channel_name}")
            return "leave"
        controls = self.switch_watcher.take_controls()
        resumed = any(kind == "resume" for kind, _ in controls)
        # 音量、亮度留到恢复播放后再发。
        self.switch_watcher.requeue([c for c in controls if c[0] in ("volume", "brightness", "pet", "audio")])
        if resumed:
            self.logger("resume")
            return "resume"
        if time.monotonic() - paused_at > PAUSE_MAX_S:
            self.logger("pause expired; resuming")
            return "resume"
        return "paused"

    def switch_waiting(self) -> bool:
        """起播等待、断流重连时轮询换台请求：有就记下并返回 True，由调用者结束会话。

        这两个阶段不便插入 SWITCH 包（序号与时间轴由重连逻辑管理），结束会话后设备
        立即重连，在 HELLO 时拿到新台。
        """
        if not self.device_switches:
            return False
        decision = self.switch_watcher.poll(self.channel_name, time.monotonic())
        if decision is None:
            return False
        kind, target = decision
        if kind == "switch":
            self.pending_switch = target
        self.logger(f"cast {kind} while waiting {target or self.channel_name}")
        return True

    def _channel_payload(self) -> dict:
        """CONFIG 里的频道部分：分组、频道列表，有遥控地址时再加 remote_url。"""
        payload = live.channel_config()
        if self.remote_url_path is not None:
            try:
                url = self.remote_url_path.read_text(encoding="ascii").strip()
            except (OSError, UnicodeError):
                url = ""
            if len(url) <= REMOTE_URL_MAX and _REMOTE_URL.match(url):
                payload["remote_url"] = url
        return payload

    def make_channel(self) -> LiveChannel:
        """为会话的频道 id 创建新的未启动频道。"""
        return self.channel_factory(CHANNELS[self.channel_name], self.ffmpeg,
                                    CHANNEL_AGENTS.get(self.channel_name, ""))

    def _live_session(self, connection: socket.socket, channel_id: str) -> None:
        """以单一单调时钟起点调度一个直播频道。

        换频道即新会话，每个会话独占一个转码进程与一个时间起点；
        视频晚于呈现时刻则丢弃，不得为视频阻塞音频。
        """
        self.phase = "live_start"
        # 频道对应的 User-Agent 一并传入：不少镜像只响应抓取时的播放器。
        channel = self.channel_factory(CHANNELS[channel_id], self.ffmpeg,
                                       CHANNEL_AGENTS.get(channel_id, ""))
        self.channel_name = channel_id
        try:
            session = secrets.randbelow(0xFFFFFFFF) + 1
            self.session_id = session
            # 先发 CONFIG 再向源请求数据：设备握手只给约 2 秒，先采样调色板会使握手超时。
            # CONFIG 只描述格式，与调色板无关。
            send_packet(connection, Packet(Kind.CONFIG, session, 0, 0,
                                           json_bytes(dict(CONFIG, session=session,
                                                           channel=channel_id,
                                                           **self._channel_payload()))))
            self.phase = "live_palette"
            # 首帧前必须有调色板，缺失则设备无色可用，画面全黑。
            try:
                channel.build_palette()
            except LiveError as error:
                # 明确告知设备失败，否则它会一直等待调色板。
                try:
                    send_packet(connection, Packet(Kind.ERROR, session, 0, 0,
                                                   json_bytes({"reason": "source unavailable"})))
                except OSError:
                    pass
                raise LiveError(str(error)) from None
            if not channel.palette:
                raise LiveError("channel has no palette")
            # start() 放在 try 内，Popen 失败时不泄漏管道与 stderr 文件。
            # 源起播失败不结束会话，设备停留在重连提示画面并重试。
            failed = ""
            try:
                channel.start()
            except (LiveError, OSError) as error:
                self.logger(f"session={session} source start failed: {type(error).__name__}")
                failed = "start_failed"
            if not failed:
                self.live_channel = channel
                # 校准失败只告警，不拒绝会话。
                if not channel.timeline.calibrated:
                    self.logger(
                        f"session={session} WARNING no clock calibration: "
                        f"{channel.timeline.calibration_note}; the picture will not "
                        f"be paired and no frames will be sent")
            # PALETTE 紧随 CONFIG、先于所有帧；每个会话按频道各发一次。
            send_packet(connection, Packet(Kind.PALETTE, session, 1, 0,
                                           channel.palette))
            self.logger(f"session={session} state=live channel={channel_id}")
            self._session_since = time.time()
            self._write_now_playing(True)
            self._now_playing_at = time.monotonic()
            self.phase = "live_prebuffer"
            deadline = time.monotonic() + STARTUP_WAIT_S
            while not failed and not channel.prebuffered() and time.monotonic() < deadline:
                if channel.failure():
                    failed = "decoder_stopped"
                    break
                # 轮询连接与停止标志，不整段休眠：否则停止请求被忽略，设备重连也要排队等待。
                if self.stop.is_set():
                    return
                if self.switch_waiting():
                    return
                self.keep_now_playing()
                # _wait_until 会改写 phase，调用后恢复，否则此处结束的会话会被记为调度等待。
                waiting = self._wait_until(connection, min(deadline, time.monotonic() + 0.02), session)
                self.phase = "live_prebuffer"
                if not waiting:
                    return
            if not failed and not channel.prebuffered():
                failed = "no_media"
            resume = None
            if failed:
                wire = reconnect.WireState(seq=2, audio_pts=0)
                now = time.monotonic()
                channel = reconnect.hold(self, connection, session, channel, wire,
                                         now + AUDIO_LEAD_MS / 1000, now,
                                         restart_reason=failed)
                if channel is None:
                    return
                self.live_channel = channel
                resume = wire
            self.set_session_state(SessionState.PLAYING, session, reason="session_started")
            self._pace_live(connection, channel, session, resume)
        finally:
            self._write_now_playing(False)
            self.set_session_state(SessionState.CLOSED, session, reason="session_closed",
                                   audio_sent=self.audio_sent, video_sent=self.video_sent)
            try:
                if channel.failure() is not None:
                    try:
                        self.live_note = channel.diagnostics()
                    except Exception as diag_err:
                        self.logger(f"session={session} WARNING: channel diagnostics failed: {diag_err}")
            finally:
                try:
                    channel.close()
                except Exception as close_err:
                    self.logger(f"session={session} WARNING: channel close failed: {close_err}")
                finally:
                    live, self.live_channel = self.live_channel, None
                    if live is not None and live is not channel:
                        try:
                            live.close()
                        except Exception as close_err:
                            self.logger(f"session={session} WARNING: channel close failed: {close_err}")

    def _pace_live(self, connection: socket.socket, channel: LiveChannel,
                   session: int, resume: reconnect.WireState | None = None) -> None:
        engine = os.environ.get("TV_LIVE_ENGINE", "legacy")
        if engine == "v2":
            from .live_sender import LiveSender
            LiveSender(self, connection, channel, session).run()
            return
        if engine != "legacy":
            raise ValueError("TV_LIVE_ENGINE must be legacy or v2")
        # 起点不得早于现在超过音频提前量，否则 ffmpeg 预填的数据会立即到期并突发发送。
        # 预缓冲深度不计入起点；开头的突发由 AUDIO_MAX_LOOKAHEAD_MS 与每帧一个时隙限制。
        # 起点取 START_DELAY_MS 与 AUDIO_LEAD_MS 的较大者，决定音频时间戳可领先时钟的幅度。
        origin = time.monotonic() + max(START_DELAY_MS, AUDIO_LEAD_MS) / 1000
        audio_pts = 0
        # 视频时间戳随帧由 pop_video() 给出，此处不维护。
        # 上一个画面发出时所在的时隙，-1 为尚无；时钟所在时隙超过它才可再发一帧。
        # 用时隙而非到期时间，慢速循环、暂停或突发都无法使其与当前时钟脱节。
        last_slot = -1
        # 帧率固定为源帧率；链路变慢时降低的是每帧字节目标（frames.encode_within），不是帧率；见 server/rate.py。
        # 无 fps 属性的替身频道按名义帧率调度。
        # TV_ADAPTIVE=0 时字节率也固定。
        fps = getattr(channel, "fps", FPS)
        controller = ByteRate(fps, adaptive=ADAPTIVE)
        # 同时保存在 server 上，供测试读取会话最终的字节率。
        self._last_controller = controller
        # 控制器每秒窗口的累计量：画面字节数、实际发出的帧数、最长单次写入、丢弃帧数。
        window_video_bytes = 0
        window_frames = 0
        window_worst_write = 0.0
        window_dropped = 0
        window_started = time.monotonic()
        self._recovery_start = 0.0
        # CONFIG 占序号 0，PALETTE 占 1，首个媒体包为 2；设备拒绝非连续序号。
        seq = 2
        last_report = 0.0
        last_audio = last_video = 0
        # 发送时间戳 = 频道时间戳 + stamp_base；断流期间提示帧与静音推进了设备时钟，
        # 换频道后的新进程靠它接续。
        stamp_base = 0
        last_audio_wire = -AUDIO_CHUNK_MS
        last_video_wire = -1
        if resume is not None:
            seq, audio_pts = resume.seq, resume.audio_pts
            last_audio_wire, last_video_wire = resume.audio_stamp, resume.video_stamp
            stamp_base = (last_audio_wire + AUDIO_CHUNK_MS
                          - int(round(channel.session_clock.audio_items * AUDIO_CHUNK_MS)))
            origin = time.monotonic() - (audio_pts - AUDIO_LEAD_MS) / 1000.0
        # 本循环最长无输出间隔及其间写入耗时；与设备侧报告对照可判断是哪一端停顿。
        gap_start = time.monotonic()
        max_send_gap = 0.0
        busy_us = 0.0

        def note_send(took: float) -> None:
            nonlocal gap_start, max_send_gap, busy_us
            at = time.monotonic()
            idle = at - gap_start
            if idle > max_send_gap:
                max_send_gap = idle
            busy_us += took * 1e6
            gap_start = at

        def hold_on_notice(empty_since: float, paused: bool = False, title: str = "") -> bool:
            """停留在重连提示画面，直到源重新有数据。

            会话结束（设备离开或要求停止）时返回 False。
            """
            nonlocal channel, seq, audio_pts, origin, now, slot_now, last_slot, stamp_base
            nonlocal last_audio_wire, last_video_wire, window_started, window_worst_write
            nonlocal window_video_bytes, window_frames, window_dropped
            self.set_session_state(
                SessionState.STARVED_REBUFFERING,
                session,
                reason="channel_audio_empty",
                empty_since=round(empty_since, 4)
            )
            wire = reconnect.WireState(seq, audio_pts, last_audio_wire, last_video_wire)
            held = reconnect.hold(self, connection, session, channel, wire, origin, empty_since,
                                  paused=paused, pause_title=title)
            if held is None:
                return False
            if held is not channel:
                channel = held
                self.live_channel = held
                if hasattr(channel, "set_video_rate"):
                    channel.set_video_rate(controller.rate)
            seq, audio_pts = wire.seq, wire.audio_pts
            last_audio_wire, last_video_wire = wire.audio_stamp, wire.video_stamp
            stamp_base = (last_audio_wire + AUDIO_CHUNK_MS
                          - int(round(channel.session_clock.audio_items * AUDIO_CHUNK_MS)))
            now = time.monotonic()
            origin = now - (audio_pts - AUDIO_LEAD_MS) / 1000.0
            slot_now = int((now - origin) * fps)
            last_slot = slot_now - 1
            self._recovery_start = now
            self.set_session_state(
                SessionState.RECOVERING,
                session,
                reason="channel_data_resumed",
                starve_duration_s=round(now - empty_since, 4),
                realigned_origin=round(origin, 4),
                audio_pts=audio_pts,
                slot_now=slot_now
            )
            window_started = now
            window_worst_write = 0.0
            window_video_bytes = 0
            window_frames = 0
            window_dropped = 0
            return True

        self.wire_bytes = 0
        while not self.stop.is_set():
            if (channel.failure() is not None and not channel.audio_pending()
                    and self.media_filter != "video"):
                if not hold_on_notice(time.monotonic()):
                    return
                continue
            now = time.monotonic()
            self._loops = getattr(self, "_loops", 0) + 1
            # 画面时间戳随帧由 pop_video() 给出（内容时间轴，见 server/timeline.py），不在本循环推算。
            # 设备按音频时钟呈现画面，时间戳超前的帧会占用接收缓冲直至音频欠载。
            audio_at = origin + (audio_pts - AUDIO_LEAD_MS) / 1000
            # 画面按自己的时隙发送，一个时隙为一个帧间隔；时钟所在时隙大于上次发送所在时隙即可发。
            # 不能与音频到期时刻绑定：音频优先，画面永远轮不到。
            # 也不用到期时间变量：把到期时间拉向当前时刻的写法会使判定每轮恒真。
            slot_now = int((now - origin) * fps)
            # 音频掌管时间轴且不丢弃；视频须在自己的时隙到期时发送，等音频队列排空会使视频饿死。
            # 限制音频领先墙钟的幅度，避免单次循环变慢后突发发送设备放不下的数据。
            audio_lookahead_ms = (audio_pts - AUDIO_LEAD_MS) - (now - origin) * 1000
            if audio_lookahead_ms < -400.0:
                # 写入背压使发送落后实时：重新锚定起点，消除负的领先量。
                origin = now - (audio_pts - AUDIO_LEAD_MS) / 1000.0
                slot_now = int((now - origin) * fps)
                last_slot = min(last_slot, slot_now - 1)
                audio_lookahead_ms = (audio_pts - AUDIO_LEAD_MS) - (now - origin) * 1000.0
            # 每秒一条跟踪日志，须放在其所用变量计算完成之后。
            if now - getattr(self, "_last_trace", now) >= 1.0:
                self._last_trace = now
                self.logger(
                    f"SEND t={now-origin:6.1f}s loops={self._loops} "
                    f"a={self.audio_sent} v={self.video_sent} "
                    f"qa={len(channel.audio)} qv={len(channel.video)} "
                    f"look={audio_lookahead_ms:7.0f} "
                    f"a_due={int(audio_at<=now)} "
                    f"v_due={int(slot_now > last_slot)}")
            # socket 须当前可写：阻塞表示设备队列已满，这是让发送跟随设备时钟的背压。
            # 到期的音频写不进去就留到下一轮，音频不丢，audio_pts 与 seq 都不前进。
            watch_read = [connection]
            if self.listener is not None:
                watch_read.append(self.listener)
            readable, writable_now, _ = select.select(watch_read, [connection], [], 0)
            if self.listener is not None and self.listener in readable:
                self._reject_waiting()
            if connection in readable:
                self.phase = "control_receive"
                # 设备发来的可能是 END（要走了）或 STATUS（音量、亮度变了，会话照常）。
                if self._device_left(connection, session):
                    return
            self.keep_now_playing()
            decision = self.switch_watcher.poll(self.channel_name, now)
            if decision is not None and self.device_switches:
                kind, target = decision
                self.logger(f"session={session} cast {kind} {target or self.channel_name}")
                if kind == "restart":
                    # 已在投屏台时来了新的投屏：结束会话，设备重连同一个台，中转给出新视频。
                    return
                if target in CHANNELS:
                    self.phase = "switch_send"
                    send_packet(connection, Packet(Kind.SWITCH, session, seq, 0,
                                                   json_bytes({"channel": target})))
                    seq += 1
            for kind, value in self.switch_watcher.take_controls():
                if kind == "pause":
                    # 手机上暂停：停在 PAUSED 画面，继续时重启视频源（reconnect.hold 的 paused 模式）。
                    self.logger(f"session={session} pause")
                    if not hold_on_notice(time.monotonic(), paused=True, title=str(value or "")):
                        return
                    now = time.monotonic()
                elif kind in ("volume", "brightness") and self.device_status is not None:
                    # 手机调音量、亮度：借用 SWITCH 包，内容不同；只认识 channel 的固件会忽略它。
                    self.phase = "control_send"
                    send_packet(connection, Packet(Kind.SWITCH, session, seq, 0,
                                                   json_bytes({kind: value})))
                    seq += 1
                elif kind == "audio" and self.device_status is not None and "audio_only" in self.device_status:
                    # 手机开关只听声音：设备存下来后自己重连，新 HELLO 决定发不发画面。
                    self.phase = "control_send"
                    send_packet(connection, Packet(Kind.SWITCH, session, seq, 0,
                                                   json_bytes({"audio_only": value})))
                    seq += 1
                elif kind == "pet" and self.device_status is not None and "pet" in self.device_status:
                    # 手机换宠物：设备换上、让它开心 2 秒，再报回来。
                    self.phase = "control_send"
                    send_packet(connection, Packet(Kind.SWITCH, session, seq, 0, json_bytes({"pet": value})))
                    seq += 1
                elif kind in ("seek", "next") and self.device_hints:
                    # 手机快进、下一个：设备画提示框，画面要停一两秒是正常的。
                    self.phase = "control_send"
                    send_packet(connection, Packet(Kind.SWITCH, session, seq, 0,
                                                   json_bytes({"seek": value} if kind == "seek" else {"next": 1})))
                    seq += 1
            if getattr(self, "duration_ms", None) is not None and (now - origin) * 1000 >= self.duration_ms:
                send_packet(connection, Packet(Kind.END, session, seq, 0))
                return
            writable = bool(writable_now)
            # 音频到期时严格优先于画面。让音频向画面让步的宽松规则会使音频近乎永远发不出去。
            # 不会饿死画面：循环频率远高于音频到期频率，每块音频至多让画面晚约 1 ms。
            self.wire_bytes = getattr(self, "wire_bytes", 0)
            # 是否允许音频早于其对应时刻发送。TV_AUDIO_FILL 默认开启：发送端把设备队列补到
            # AUDIO_MAX_LOOKAHEAD_MS 并保持；关闭则音频恰按实时放出，画面包占线期间设备队列
            # 得不到补充而欠载。
            audio_fill = os.environ.get("TV_AUDIO_FILL", "1") != "0"
            audio_due = (channel.audio_pending()
                         and audio_lookahead_ms <= AUDIO_MAX_LOOKAHEAD_MS
                         and (audio_fill or now >= audio_at))
            # 音频到期不阻塞画面：两者不用 if/elif，同一轮内先音频、后画面；
            # 否则画面只能在两块音频的空隙发送，而该空隙不存在。
            send_audio = audio_due and writable
            send_video = slot_now > last_slot and writable and channel.video_pending()
            # 仅诊断用：--media audio 或 video 关闭其中一路，以定位故障；默认两路都开。
            if self.media_filter == "audio":
                send_video = False
            elif self.media_filter == "video":
                # 画面按音频位置调度：手动推进音频时间轴而非冻结，否则所有帧同时到期。
                send_audio = False
                audio_pts += AUDIO_CHUNK_MS
            self._trace = getattr(self, "_trace", 0) + 1
            if os.environ.get("TV_TRACE") == "1":
                self.logger(
                    f"TRACE n={self._trace} t={(now-origin):6.2f}s "
                    f"due={int(audio_due)} wr={int(writable)} qa={len(channel.audio)} "
                    f"qv={len(channel.video)} look={audio_lookahead_ms:7.0f} "
                    f"sa={self.audio_sent} sv={self.video_sent} "
                    f"late={self.dropped_video} "
                    f"frameK={sum(self._frame_sizes[-3:]) // 1024 if getattr(self,'_frame_sizes',None) else 0} "
                    f"wire_kbps={(self.wire_bytes*8/1000)/max(0.001, now-origin):7.1f}")
            if send_audio:
                # 发送所有已到期的音频块，而非每轮一块：写画面包期间会积欠多个音频时隙，
                # 逐轮补发会使领先量持续为负。上限 32 块（640 ms）仅防止病态队列独占循环；
                # 同时受时钟和 AUDIO_MAX_LOOKAHEAD_MS 约束。
                for _ in range(32):
                    if not ((audio_fill or audio_at <= now)
                            and channel.audio_pending()
                            and audio_pts - AUDIO_LEAD_MS - (now - origin) * 1000
                                <= AUDIO_MAX_LOOKAHEAD_MS
                            and select.select([], [connection], [], 0)[1]):
                        break
                    taken = channel.pop_audio()
                    if taken is None:
                        break
                    block, stamp = taken
                    stamp += stamp_base
                    last_audio_wire = stamp
                    self.phase = "live_pcm_send"
                    send_packet(connection, Packet(Kind.PCM, session, seq, stamp, block))
                    self.wire_bytes += len(block) + 24
                    # stamp 是线上时间戳（来自声音自身的内容时间轴）；audio_pts 是发送端按已发块数
                    # 推算的下一块到期时刻。两者不合并。
                    audio_pts += AUDIO_CHUNK_MS
                    seq += 1
                    self.audio_sent += 1
                    audio_at = origin + (audio_pts - AUDIO_LEAD_MS) / 1000
            sent_something = send_audio
            if send_video:
                sent_something = True
                # 画面队列深度与音频一致，见 live.py 的 VIDEO_QUEUE_SECONDS。
                chosen = channel.pop_video()
                frame, video_pts = chosen if chosen is not None else (None, None)
                # 没有与队首声音匹配的帧不等于帧迟到：此时不消耗时隙，下一轮再取。
                if frame is None:
                    # 本轮无帧可发，仍须执行下面的等待、控制器更新与周期报告，不能 continue。
                    sent_something = False
                else:
                    # 时隙无论帧是否发出都已消耗：拥塞丢帧不能把时隙让给下一帧，否则积压会一次性放出。
                    # 取决策时的值，不重读时钟。
                    last_slot = slot_now
                    self._frame_sizes = getattr(self, "_frame_sizes", [])
                    self._frame_sizes.append(sum(len(p) for p in frame))
                    # 不做迟到否决：设备按到达即绘，略迟的帧仍值得发；丢弃会使索引跳进，引发持续丢帧。
                    # 陈旧度由队列限定：队列定长，满时丢最旧。
                    # 只听声音的设备不要画面：帧照样取出（节奏不变），只是不写。
                    if writable and not self.device_audio_only:
                        # 不可写时丢弃整帧而非尝试：发不完的写入会阻塞单线程循环及音频，也不得在线上留下半个包。
                        # 同一帧的各包要么全发要么不发。
                        self.phase = "live_video_send"
                        # 整帧一个写入时限 max(帧间隔, FRAME_WRITE_DEADLINE_S)，是失败上限而非调度；
                        # 须小于设备约 3 秒的断开阈值，且不能短到让正常帧在传输期间超时。
                        # 过期帧由调度器丢弃，慢帧只应损失该帧，不应结束会话。
                        frame_deadline = time.monotonic() + max(1.0 / fps, FRAME_WRITE_DEADLINE_S)
                        video_pts = max(video_pts + stamp_base, last_video_wire + 1)
                        last_video_wire = video_pts
                        for n, part in enumerate(frame):
                            # 帧内各次写入之间检查设备是否已离开，避免正常换频道表现为 broken pipe。
                            self.phase = "control_receive"
                            if self._device_left(connection, session):
                                return
                            # 画面包之间让音频通过：画面包被设备窗口卡住时会阻塞整帧时限，期间无音频发出而欠载。
                            # 各包自带长度与时间戳，格式不要求同一帧的包相邻，只要求单个包的字节连续。
                            # 补发已到期的全部音频块（上限 32），逐块补发会使欠额持续增长。
                            for _ in range(32):
                                now_in_frame = time.monotonic()
                                audio_at = origin + (audio_pts - AUDIO_LEAD_MS) / 1000
                                lookahead = ((audio_pts - AUDIO_LEAD_MS)
                                             - (now_in_frame - origin) * 1000)
                                if not ((audio_fill or now_in_frame >= audio_at)
                                        and channel.audio_pending()
                                        and lookahead <= AUDIO_MAX_LOOKAHEAD_MS
                                        and select.select([], [connection], [], 0)[1]):
                                    break
                                taken = channel.pop_audio()
                                if taken is None:
                                    break
                                block, stamp = taken
                                stamp += stamp_base
                                last_audio_wire = stamp
                                self.phase = "live_pcm_send"
                                _audio_t0 = time.monotonic()
                                send_packet(connection, Packet(Kind.PCM, session, seq,
                                                               stamp, block),
                                            deadline=frame_deadline)
                                note_send(time.monotonic() - _audio_t0)
                                audio_pts += AUDIO_CHUNK_MS
                                seq += 1
                                self.audio_sent += 1
                            self.phase = "live_video_send"
                            # 同一帧各包时间戳相同，除首包外以 VIDEO_CONTINUES 标志声明，否则设备按时间戳不前进而拒绝。
                            # 单个画面包必须连续写出，中间不能插入音频包：设备读完包头后按 length 读取负载，
                            # 插入的音频字节会被当作画面数据而结束会话。因此音频只在包之间发送，
                            # 包内用 _write_slice 分片写入，分片仅为让循环不被整包占住并能响应停止请求。
                            wire = Packet(Kind.JPEG, session, seq, video_pts, part,
                                          VIDEO_CONTINUES if n else 0).encode()
                            sent = 0
                            while sent < len(wire):
                                self.phase = "live_video_send"
                                data = memoryview(wire)[sent:sent + VIDEO_SLICE_BYTES]
                                _probe_t0 = time.monotonic()
                                if not _write_slice(connection, data, frame_deadline):
                                    raise TimeoutError("video packet deadline expired")
                                _probe_dt = time.monotonic() - _probe_t0
                                note_send(_probe_dt)
                                # 速率控制器按分片判断窗口：这是 socket 一次接受的单位，其耗时反映链路拥塞程度。
                                if getattr(self, "session_state", None) != SessionState.RECOVERING and _probe_dt > window_worst_write:
                                    window_worst_write = _probe_dt
                                if _probe_dt > PROBE_SLOW_S:
                                    self.logger(
                                        f"PROBE slice n={n} bytes={len(data)} "
                                        f"took_ms={_probe_dt * 1000:.0f} phase={self.phase}")
                                sent += len(data)
                            self.wire_bytes += len(part) + 24
                            # 只统计画面字节：声音固定占 32 kB/s，计入会使各频道看起来一样重。
                            window_video_bytes += len(part) + 24
                            seq += 1
                            self.video_sent += 1
                        # 每帧只计一次（在包循环之外）：控制器用窗口字节数除以该值得到每帧成本。
                        window_frames += 1
                        if getattr(self, "session_state", None) == SessionState.RECOVERING:
                            rec_start = getattr(self, "_recovery_start", window_started)
                            # 至少 1.5 秒且 10 帧后才退出 RECOVERING，等待链路波动平息。
                            if (now - rec_start >= 1.5) and window_frames >= 10:
                                self.set_session_state(
                                    SessionState.PLAYING,
                                    session,
                                    reason="recovered_to_steady_state",
                                    video_pts=video_pts,
                                    realigned_origin=round(origin, 4),
                                    current_fps=fps
                                )
                                window_started = time.monotonic()
                                window_worst_write = 0.0
                                window_video_bytes = 0
                                window_frames = 0
                                window_dropped = 0
                    elif not self.device_audio_only:
                        # 拥塞：放弃该帧以保住音频时间轴；时间戳取自时钟，丢帧只损失一张画面。
                        self.dropped_video += 1
                        window_dropped += 1

            if not sent_something:
                # 暂无到期内容：短暂休眠而非阻塞到下个时刻，以便及时发现 END 或断开。
                if not channel.has_data():
                    empty_since = time.monotonic()
                    empty_threshold = 0.5 if getattr(self, "session_state", None) == SessionState.RECOVERING else 0.3
                    # 先等待正常的块间到达，再判定断流。
                    while not channel.audio_pending() and self.media_filter != "video" and (time.monotonic() - empty_since < empty_threshold):
                        time.sleep(0.005)

                    if not channel.audio_pending() and self.media_filter != "video":
                        # 持续无数据：设备停留在提示画面。
                        if not hold_on_notice(empty_since):
                            return
                    else:
                        time.sleep(0.005)
                    window_frames = 0
                    window_dropped = 0
                else:
                    time.sleep(0.005)
            # 每秒决策一次，依据刚结束的窗口；窗口须长到容纳数帧，又短到能在设备放弃前多次决策。
            if now - window_started >= 1.0:
                # 先判断源是否仍在产出，再通知控制器：源停止与发送快于链路读数相同而应对相反。
                # 替身频道可能没有 source_state，缺省视为 FLOWING。
                if hasattr(channel, "source_state"):
                    source = channel.source_state(
                        queue_over_bound=len(channel.video) >= channel.video.maxlen)
                else:
                    source = SourceState.FLOWING
                # 仅用于周期报告，不作为控制器的总开关：停止的源仍可能有排队内容，
                # 其写入仍是下游容量的有效证据。
                self._last_source_state = source
                if getattr(self, "session_state", None) != SessionState.RECOVERING:
                    before = controller.rate
                    controller.observe(window_video_bytes,
                                       window_worst_write * 1000,
                                       window_dropped,
                                       frames=window_frames,
                                       window_s=now - window_started)
                    if controller.rate != before:
                        if hasattr(channel, "set_video_rate"):
                            channel.set_video_rate(controller.rate)
                        self.logger(f"RATE {controller.rate // 1000} kB/s: {controller.describe()}")
                window_video_bytes, window_worst_write = 0, 0.0
                window_dropped = window_frames = 0
                window_started = now
            if now - last_report >= 5:
                elapsed = now - last_report if last_report else 5.0
                self.logger(
                    f"live t={now-origin:6.1f}s fps={fps} "
                    f"rate={controller.rate // 1000}kB/s ({controller.reason}) source={getattr(self, '_last_source_state', '?')} "
                    # 画面与声音内容时钟在配对点的偏差；a_lag、v_lag 只是队列深度。
                    f"clock=({channel.session_clock.diagnostics()}) "
                    # 对齐所用的基准；getattr 防止报告本身结束会话（测试替身没有 timeline）。
                    f"basis=({getattr(getattr(channel, 'timeline', None), 'basis', None) or 'none'}) "
                    f"audio_q={len(channel.audio):4d} "
                    f"video_q={len(channel.video):4d} "
                    # 下一个包落后直播的内容秒数，音画两者应一致。
                    f"a_lag={len(channel.audio) * AUDIO_CHUNK_MS / 1000:5.1f}s "
                    f"v_lag={channel.picture_lag_s():5.1f}s "
                    f"lookahead_ms={audio_lookahead_ms:6.0f} "
                    f"audio_sent={self.audio_sent} video_sent={self.video_sent} "
                    f"audio_pps={(self.audio_sent - last_audio) / elapsed:5.1f} "
                    f"video_pps={(self.video_sent - last_video) / elapsed:5.1f} "
                    f"late_video={self.dropped_video} prod_drop={channel.dropped_video} "
                    f"send_gap_max_ms={max_send_gap * 1000:5.0f} "
                    f"busy={busy_us / 1e6:4.1f}s")
                last_report = now
                last_audio, last_video = self.audio_sent, self.video_sent
                max_send_gap = 0.0
                busy_us = 0.0
        # 服务端停止是正常结束，不是流故障。
        if self.stop.is_set():
            return
        raise TimeoutError("live session cancelled")

    def _admit(self, connection: socket.socket, hello: Packet) -> bool:
        """多用户：认出设备并换上它的文件；未绑定的设备在这里服务完（只给绑定画面），返回 True。"""
        authenticate(hello, None)
        try:
            peer = connection.getpeername()[0]
        except OSError:
            peer = ""
        admission = self.tenancy.admit(json_object(hello.payload), self.token, self.port, peer)
        if not admission.bound:
            tenancy_unbound.serve(self, connection, admission, self.tenancy, CONFIG)
            return True
        tenancy_gate.apply(self, admission.files)
        self.logger(f"device={admission.device_id}{' legacy' if admission.legacy else ''}")
        return False

    def _session(self, connection: socket.socket) -> None:
        self.phase = "hello_receive"
        hello = receive_packet(connection, expected_session=0)
        self.phase = "authenticate"
        if self.tenancy is None:
            authenticate(hello, self.token)
        else:
            if self._admit(connection, hello):
                return
        if self.live_enabled:
            # 视频中转增删 UP 主后会改写频道表；在会话之间重读，正在播放的会话不受影响。
            live.reload_channels_if_changed()
            # 旧固件不认识 SWITCH 包，收到会断开，所以只发给 HELLO 里声明了的设备。
            greeting = json_object(hello.payload)
            self.note_device_boot(greeting)
            self.note_device_wifi(greeting.get("wifi"))
            self.device_switches = greeting.get("switch") == 1
            # 新固件在 HELLO 里报音量、亮度；报了的才接受手机的调节。
            # 新固件自己画暂停横条（"pause": 1）；旧固件靠服务端发 PAUSED 画面。
            self.device_pause = greeting.get("pause") == 1
            # 新固件自己画快进、下一个的提示（"hint": 1）；别的固件不发，免得它把包当成坏数据。
            self.device_hints = greeting.get("hint") == 1
            self.device_audio_only = greeting.get("audio_only") == 1
            self.device_status = None
            if all(isinstance(greeting.get(k), int) for k in ("volume", "brightness", "brightness_steps")):
                self.device_status = {"volume": greeting["volume"], "brightness": greeting["brightness"],
                                      "steps": greeting["brightness_steps"]}
                # 会换宠物的固件在 HELLO 里报当前宠物。
                if isinstance(greeting.get("pet"), int):
                    self.device_status["pet"] = greeting["pet"]
                # 会只听声音的固件报当前是不是只听声音。
                if isinstance(greeting.get("audio_only"), int):
                    self.device_status["audio_only"] = greeting["audio_only"]
            # 设备按名选择频道，换频道即新连接；名称缺失或不在表内时回退到 --channel 指定的默认频道。
            # 重读后上次的频道可能已被删除，此时再退到表中的默认频道。
            requested = json_object(hello.payload).get("channel")
            if isinstance(requested, str) and requested in CHANNELS:
                channel_id = requested
            elif self.channel_name in CHANNELS:
                channel_id = self.channel_name
            else:
                channel_id = live.DEFAULT_CHANNEL
            # 手机在设备不在推流时要求换台：直接在 CONFIG 里给新台，设备按 CONFIG 的 channel 更新自己。
            if self.device_switches:
                decision = self.switch_watcher.poll(channel_id, time.monotonic(), force=True)
                if decision is not None and decision[0] == "switch":
                    self.pending_switch = decision[1]
                if self.pending_switch in CHANNELS:
                    self.logger(f"cast switch at connect {channel_id} -> {self.pending_switch}")
                    channel_id = self.pending_switch
            self.pending_switch = ""
            self._live_session(connection, channel_id)
            return

        session = secrets.randbelow(0xFFFFFFFF) + 1
        self.session_id = session
        self.logger(f"session={session} state=authenticated")
        self.phase = "config_send"
        send_packet(connection, Packet(Kind.CONFIG, session, 0, 0,
                                       json_bytes(dict(CONFIG, session=session, duration_ms=self.media.duration_ms))))
        send_packet(connection, Packet(Kind.PALETTE, session, 1, 0, self.media.palette))
        origin = time.monotonic() + 0.2
        seq = 2
        for due_ms, kind, pts, index in schedule(self.duration_ms, self.media.duration_ms):
            if not self._wait_until(connection, origin + due_ms / 1000, session):
                return
            self.phase = "control_receive"
            if self._device_left(connection, session):
                return
            now = time.monotonic()
            if kind == Kind.PCM and now > origin + pts / 1000 + 0.1:
                self.phase = "audio_schedule_late"
                raise TimeoutError("audio schedule stalled; reconnect for a new origin")
            if kind == Kind.JPEG and (self.device_audio_only or now > origin + (pts + 83) / 1000):
                self.dropped_video += 1
                continue
            if kind == Kind.PCM:
                self.phase = "pcm_send"
                send_packet(connection, Packet(Kind.PCM, session, seq, pts,
                                               self.media.audio_at(index)))
                seq += 1
                self.audio_sent += 1
            else:
                self.phase = "video_send"
                frame_deadline = time.monotonic() + IO_TIMEOUT
                for n, part in enumerate(self.media.frame_at(index)):
                    if n and not select.select([], [connection], [], frame_deadline - time.monotonic())[1]:
                        self.dropped_video += 1
                        break
                    if n and self._device_left(connection, session):
                        return
                    flags = VIDEO_CONTINUES if n else 0
                    send_packet(connection, Packet(Kind.JPEG, session, seq, pts, part, flags))
                    seq += 1
                self.video_sent += 1
        self.phase = "end_send"
        send_packet(connection, Packet(Kind.END, session, seq, 0))

def print_where_to_connect(bind: str, port: int, token: bytes | None) -> None:
    """启动后最后打印的内容：先给设备应填的地址，再给两条提示。"""
    shown = bind
    if bind == WILDCARD_BIND:
        detected = netident.lan_address()
        shown = detected if detected and local_ipv4(detected) else None
    for line in netident.describe(shown, port):
        print(line, flush=True)

    # 无令牌时同网段设备均可收看，提示之。
    if token is None:
        print("提示：本网络上的其他设备也能收看这台电脑转发的频道。", flush=True)

    # 关闭窗口即停止服务；窗口看起来像日志，用户不易联想到。
    print("提示：关掉这个窗口，服务就停止了，电视会中断。", flush=True)


def main(argv: list[str] | None = None) -> int:
    # 输出固定为 UTF-8：管道下默认取本地编码（Windows 中文为 GBK），log_stamp 按 UTF-8 解码会乱码。
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    live_parser = commands.add_parser("live", help="transcode one allowlisted public channel in real time")
    live_parser.add_argument("--channel", required=True, choices=sorted(CHANNELS))
    live_parser.add_argument("--ffmpeg", default="ffmpeg")
    # 仅诊断用：关闭一路以判断是画面还是声音的问题。
    live_parser.add_argument("--media", choices=("both", "audio", "video"),
                             default="both")
    live_parser.add_argument("--bind", default=WILDCARD_BIND,
                             help="address to listen on (default: all interfaces; peers outside private networks are refused)")
    live_parser.add_argument("--port", type=int, default=8096)
    live_parser.add_argument("--token-file", type=Path)
    args = parser.parse_args(argv)
    try:
        token = load_token(args.token_file)
        server = AVServer(token, args.bind, args.port,
                          logger=lambda message: print(message, flush=True))
        server.ffmpeg = args.ffmpeg
        server.media_filter = "" if args.media == "both" else args.media
        server.channel_name = args.channel
        # 信号处理器须设置接入循环实际读取的标志。
        for signum in (signal.SIGINT, signal.SIGTERM):
            signal.signal(signum, lambda *_: server.stop.set())
        # 每个设备连接启动独立转码进程；ffmpeg 退出只结束该会话，接入循环无需重启。
        print_where_to_connect(args.bind, args.port, token)
        server.serve()
        # 仅在出现异常时打印计数；握手期放弃的连接不计，换频道每次都会产生一个。
        if server.failed or server.rejected or server.dropped_video:
            print(f"已停止。出错 {server.failed} 次，拒绝 {server.rejected} 次，"
                  f"丢帧 {server.dropped_video} 次。", flush=True)
        else:
            print("已停止。", flush=True)
        return 0
    except OSError as error:
        # 绑定失败与令牌、媒体问题难以区分，端口被占用又像静默无操作；errno 不含路径与凭据。
        # 用 errno.EADDRINUSE 而非数值：各平台数值不同。
        if error.errno == errno.EADDRINUSE:
            print("端口 8096 已被占用——多半是上一次的程序还没关干净。", file=sys.stderr)
            print("把之前的窗口关掉，或重启电脑后再试。", file=sys.stderr)
        else:
            print(f"网络端口打不开（errno={error.errno}）。", file=sys.stderr)
        return 1
    except Exception:
        # 子进程错误、路径、环境变量可能含敏感值，不输出。
        print("程序没能启动。常见原因是 ffmpeg 缺失或频道表有问题；", file=sys.stderr)
        print("把上面最后几行输出发给项目的维护者可以定位。", file=sys.stderr)
        if os.environ.get("TV_TRACEBACK") == "1":
            import traceback
            traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
