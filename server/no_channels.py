"""自建模式下一个能放的台都没有：不转码，只发一张「还没有频道」的静止画面和静音。

新装的自建服务器（server/selfhost）频道表是空的，用户还没订 UP 主、没投屏，设备的频道表里
只有「我的投屏」而投屏位是空的。这时与其让设备停在「正在重连」，不如告诉人去手机上加台。
画法和未绑定设备的绑定画面一样（server/tenancy/unbound.py、overlay.lines_frame）。

每隔几秒看一次：频道表变了（加了直播台、订了 UP 主）、或者有了投屏，就结束会话，设备马上重连，
按新表播。只在 TV_NO_BUILTIN_CHANNELS=1（live.empty_allowed()）时用，云端的服务不走这里。
"""

from __future__ import annotations

import secrets
import time

from . import channel_groups, episode_info, live, media, overlay, reconnect
from .protocol import Kind, Packet, json_bytes, send_packet
from .relay.table import CAST_ID, CAST_LABEL
from .tenancy import unbound

# 点阵字体只有大写字母和数字（overlay._GLYPHS）。
LINES = ["NO CHANNELS YET", "ADD SOME", "ON YOUR PHONE"]
CHECK_S = 2.0
# 停在这张画面最长多久；之后结束会话，设备自己重连。
MAX_S = 10 * 60


def nothing_to_play(info=episode_info.fetch) -> bool:
    """频道表里除了投屏台没有别的台，投屏位也是空的（中转问不到「投的是什么」）。"""
    if any(cid != CAST_ID for cid in live.CHANNELS):
        return False
    url = episode_info.info_url(live.CHANNELS.get(CAST_ID))
    return url is None or info(url) is None


def serve(server, connection, config: dict, channel_id: str, info=episode_info.fetch) -> None:
    """服务一个会话，直到设备离开、有了能放的东西或超时。config 是 tv_server.CONFIG。"""
    session = secrets.randbelow(0xFFFFFFFF) + 1
    server.session_id = session
    server.phase = "no_channels_config"
    server.logger(f"session={session} state=no_channels")
    payload = dict(config, session=session, **server._channel_payload())
    if not payload.get("channel_list"):
        # 中转还没写出这台设备的频道表（刚绑定，最多等一轮同步）：给设备一个投屏台，别让它拿到空列表。
        payload.update(groups=[channel_groups.CAST], channel_list=[{"id": CAST_ID, "name": CAST_LABEL, "g": 0}])
        channel_id = channel_id or CAST_ID
    if channel_id:
        payload["channel"] = channel_id
    server.channel_name = channel_id
    send_packet(connection, Packet(Kind.CONFIG, session, 0, 0, json_bytes(payload)))
    send_packet(connection, Packet(Kind.PALETTE, session, 1, 0, media.default_palette()))
    raw = overlay.lines_frame(LINES)
    wire = reconnect.WireState(seq=2, audio_pts=0)
    started = time.monotonic()
    origin = started + media.AUDIO_LEAD_MS / 1000
    server._session_since = time.time()
    server._write_now_playing(True)
    shown, tick, next_frame, next_check = None, 0, 0.0, started + CHECK_S
    try:
        while not server.stop.is_set():
            server.phase = "no_channels"
            reconnect._send_silence(server, connection, session, wire, origin)
            server.keep_now_playing()
            now = time.monotonic()
            if now >= next_frame:
                shown = unbound._send_picture(server, connection, session, wire, raw, shown, tick)
                tick += 1
                next_frame = now + unbound.FRAME_S
            if now >= next_check:
                next_check = now + CHECK_S
                live.reload_channels_if_changed()
                if not nothing_to_play(info):
                    server.logger(f"session={session} something to play now; reconnecting")
                    return
            if now - started > MAX_S:
                return
            if not server._wait_until(connection, time.monotonic() + 0.02, session):
                return
    finally:
        server._write_now_playing(False)
