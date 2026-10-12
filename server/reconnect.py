"""上游断流时保持会话。

源停供时不断开设备：画面保持最后一帧并叠加 "RECONNECTING..."，同时持续补发静音以满足
设备的音频看门狗。重启源在工作线程中进行，因为启动会阻塞数秒而静音不能中断。
新源就绪后接续发送，断开期间的内容跳过，不回补。
"""

from __future__ import annotations

import os
import select
import threading
import time
from dataclasses import dataclass

from . import frames, overlay
from .live import AUDIO_MAX_LOOKAHEAD_MS, LiveChannel, LiveError
from .media import AUDIO_CHUNK_MS, AUDIO_LEAD_MS
from .protocol import (AUDIO_BYTES, VIDEO_CONTINUES, Kind, Packet, json_bytes,
                       send_packet)

# 会话停留在提示画面的最长时间，超过即结束。
RECONNECT_TIMEOUT_S = float(os.environ.get("TV_RECONNECT_TIMEOUT_S", "60"))
# 进程存活但这么久没有数据，视为卡住并替换。
STALL_RESTART_S = 5.0
# 暂停时多久重发一次 PAUSED 画面：要短于设备判定「画面停了」的 1.5 秒。
PAUSE_FRAME_S = 1.0
# 单次替换必须在此时间内产出预缓冲完成的新源。
ATTEMPT_TIMEOUT_S = 20.0
RETRY_DELAY_S = 2.0

_SILENCE = bytes(AUDIO_BYTES)


@dataclass
class WireState:
    """已发出的线上状态，使提示画面与静音能够接续。"""

    seq: int
    audio_pts: int
    audio_stamp: int = -AUDIO_CHUNK_MS
    video_stamp: int = -1


def _close_quietly(channel: LiveChannel) -> None:
    try:
        channel.close()
    except Exception:
        pass


def _close_background(channel: LiveChannel) -> None:
    """关闭会等待解码器退出，放到后台线程以免中断静音。"""
    threading.Thread(target=_close_quietly, args=(channel,), daemon=True).start()


class Restart:
    """在工作线程中启动的替换频道。"""

    def __init__(self, make):
        self.channel: LiveChannel = make()
        self.error: Exception | None = None
        self.started_at = time.monotonic()
        self._cancelled = False
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        try:
            self.channel.build_palette()
            self.channel.start()
        except Exception as error:
            self.error = error
        if self.error is not None or self._cancelled:
            _close_quietly(self.channel)

    @property
    def done(self) -> bool:
        return not self._thread.is_alive()

    def abandon(self) -> None:
        self._cancelled = True
        _close_background(self.channel)


def _tell_device(connection, session: int, reason: str) -> None:
    try:
        send_packet(connection, Packet(Kind.ERROR, session, 0, 0,
                                       json_bytes({"reason": reason})))
    except OSError:
        pass


def _send_notice(server, connection, session: int, wire: WireState,
                 shown: bytes | None, text: str = overlay.NOTICE_TEXT) -> bytes:
    """发送提示画面，返回设备此刻显示的内容。"""
    raw = overlay.reconnect_frame(shown, text)
    chosen = frames.choose_stripes(raw, shown, 0, 1 << 20)
    parts = frames.pack_stripes(chosen)
    stamp = max(wire.audio_stamp, wire.video_stamp + 1)
    deadline = time.monotonic() + server.frame_deadline_s
    for n, part in enumerate(parts):
        send_packet(connection,
                    Packet(Kind.JPEG, session, wire.seq, stamp, part,
                           VIDEO_CONTINUES if n else 0),
                    deadline=deadline)
        wire.seq += 1
        server.video_sent += 1
    wire.video_stamp = stamp
    return frames.apply_stripes(raw, shown, chosen)


def _send_silence(server, connection, session: int, wire: WireState,
                  origin: float) -> None:
    """为所有已到期的音频块补发静音，节奏与真实音频一致。"""
    for _ in range(32):
        lookahead = (wire.audio_pts - AUDIO_LEAD_MS) - (time.monotonic() - origin) * 1000
        if lookahead > AUDIO_MAX_LOOKAHEAD_MS:
            break
        if not select.select([], [connection], [], 0)[1]:
            break
        stamp = wire.audio_stamp + AUDIO_CHUNK_MS
        send_packet(connection, Packet(Kind.PCM, session, wire.seq, stamp, _SILENCE))
        wire.audio_stamp = stamp
        wire.audio_pts += AUDIO_CHUNK_MS
        wire.seq += 1
        server.audio_sent += 1


def _tell_paused(server, connection, session: int, wire: WireState, paused: bool,
                 title: str = "") -> None:
    """告诉自己会画暂停横条的固件：暂停了（带标题）或继续了。"""
    payload = {"paused": 1, "title": title[:48]} if paused else {"paused": 0}
    send_packet(connection, Packet(Kind.SWITCH, session, wire.seq, 0, json_bytes(payload)))
    wire.seq += 1


def _flush_voice(server, connection, session: int, wire: WireState) -> None:
    """暂停、断流重连时也把语音结果发出去（暂停中说「继续」最常见）。"""
    flush = getattr(server, "voice_flush", None)
    if flush is not None:
        wire.seq = flush(connection, session, wire.seq)


def hold(server, connection, session: int, channel: LiveChannel, wire: WireState,
         origin: float, since: float, restart_reason: str = "",
         paused: bool = False, pause_title: str = "") -> LiveChannel | None:
    """让设备停留在提示画面，直到源重新有数据。

    返回继续使用的频道：原频道已恢复或新启动的替换频道；设备离开时返回 None。
    `wire` 原地更新。自 `since` 起超过 `RECONNECT_TIMEOUT_S` 抛 LiveError。
    `restart_reason` 非空时立即替换 `channel`。

    `paused`：手机上暂停。先关掉视频源，持续补静音、每秒重发一次 PAUSED 画面（不让设备因为
    没画面而换成等待画面），直到 server.pause_verdict() 说继续；然后按重连的流程重启视频源，
    重连的超时从继续的那一刻算起。
    """
    deadline = since + RECONNECT_TIMEOUT_S
    restart: Restart | None = None
    attempts = 0
    retry_at = 0.0
    pending = restart_reason
    make = server.make_channel
    try:
        server.phase = "live_reconnect"
        text = overlay.PAUSE_TEXT if paused else overlay.NOTICE_TEXT
        # 会自己画暂停横条的固件：不发画面，屏幕保留暂停前的最后一帧。
        device_draws = paused and getattr(server, "device_pause", False)
        if device_draws:
            _tell_paused(server, connection, session, wire, True, pause_title)
            shown = getattr(channel, "shown", None)
        else:
            shown = _send_notice(server, connection, session, wire, getattr(channel, "shown", None), text)
            channel.shown = shown
        if paused:
            paused_at = time.monotonic()
            next_frame = paused_at + PAUSE_FRAME_S
            _close_background(channel)
            channel = None
            keep = getattr(server, "keep_now_playing", lambda: None)
            while not server.stop.is_set():
                server.phase = "live_paused"
                _send_silence(server, connection, session, wire, origin)
                keep()
                now = time.monotonic()
                _flush_voice(server, connection, session, wire)
                verdict = server.pause_verdict(paused_at)
                if verdict == "leave":
                    return None
                if verdict == "resume":
                    pending = "resumed"
                    deadline = now + RECONNECT_TIMEOUT_S
                    if device_draws:
                        _tell_paused(server, connection, session, wire, False)
                    break
                if now >= next_frame and not device_draws:
                    shown = _send_notice(server, connection, session, wire, shown, text)
                    next_frame = now + PAUSE_FRAME_S
                if not server._wait_until(connection, time.monotonic() + 0.02, session):
                    return None
            if server.stop.is_set():
                return None
            server.phase = "live_reconnect"
        while not server.stop.is_set():
            server.phase = "live_reconnect"
            now = time.monotonic()
            if now > deadline:
                _tell_device(connection, session, "source unavailable")
                raise LiveError(f"source unavailable for {RECONNECT_TIMEOUT_S:.0f} s")
            _send_silence(server, connection, session, wire, origin)
            _flush_voice(server, connection, session, wire)

            if restart is None:
                if channel is not None and not pending:
                    if channel.failure() is not None:
                        pending = "decoder_stopped"
                    elif now - since > STALL_RESTART_S:
                        pending = "stalled"
                    elif channel.video_pending() and len(channel.audio) >= 4:
                        return channel
                elif channel is None and not pending:
                    pending = "start_failed"
                if pending and now >= retry_at:
                    attempts += 1
                    server.logger(f"session={session} RECONNECT attempt={attempts} "
                                  f"reason={pending}")
                    if channel is not None:
                        _close_background(channel)
                        channel = None
                    restart = Restart(make)
                    pending = ""
            else:
                failed = ""
                if restart.done and restart.error is not None:
                    failed = type(restart.error).__name__
                elif restart.done and restart.channel.failure() is not None:
                    failed = "decoder_stopped"
                elif now - restart.started_at > ATTEMPT_TIMEOUT_S:
                    failed = "timeout"
                if failed:
                    server.logger(f"session={session} RECONNECT attempt={attempts} "
                                  f"failed={failed}")
                    restart.abandon()
                    restart = None
                    retry_at = now + RETRY_DELAY_S
                elif restart.done and restart.channel.prebuffered():
                    winner, restart = restart.channel, None
                    return winner

            getattr(server, "keep_now_playing", lambda: None)()
            # 手机要换台：结束会话，设备重连时拿到新台（AVServer.switch_waiting）。
            waiting = getattr(server, "switch_waiting", None)
            if waiting is not None and waiting():
                return None
            if not server._wait_until(connection, time.monotonic() + 0.01, session):
                return None
        return None
    finally:
        if restart is not None:
            restart.abandon()
