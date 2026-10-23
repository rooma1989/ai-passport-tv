"""未绑定的设备：不转码，只发一张写着绑定码的静止画面和静音。

自建时密钥对不上的设备（gate.py 的 refused）走同一条路，画面换成「KEY CHANGED / RE-PAIR THIS TV /
IN ADMIN」，CONFIG 里没有遥控地址也没有 "bound"；主人点了「重新配对」（或封禁它）会话就结束，设备重连。

CONFIG 里的 remote_url 换成绑定地址（HTTPS://<SITE>/TV/B/<码>），设备的状态页照常把它画成
二维码；新固件还会收到 "bound": 0（旧固件不认识这个键，忽略）。画面上同时写出绑定码（云端 20 位，
自建 6 位数字），不打开状态页也能在手机遥控的「绑定设备」里输入。

每隔几秒看一次数据库：一旦有人绑定了，就结束会话，设备马上重连，拿到主人的频道表；绑定码换了
（自建时错码太多，accounts.rotate_claims）也结束会话，重连后画面和二维码是新码。
"""

from __future__ import annotations

import secrets
import time

from .. import frames, media, overlay, reconnect
from ..protocol import VIDEO_CONTINUES, Kind, Packet, json_bytes, send_packet
from . import db

# 频道表里只有这一个台；设备在它上面换台也还是回到这张画面。
BIND_CHANNEL = "bind"
BIND_LABEL = "扫码绑定"
BIND_GROUP = "其他"
# 每秒重发一次画面：设备 1.5 秒没有画面就会换成等待画面（与 reconnect.PAUSE_FRAME_S 一致）。
FRAME_S = reconnect.PAUSE_FRAME_S
# 多久看一次是否已经绑定。
CHECK_S = 2.0
# 停在绑定画面最长多久；之后结束会话，设备会自己重连（绑定码不变）。
MAX_S = 10 * 60
# 密钥对不上的设备：提示画面上的字和那个唯一的「台」。
REFUSED_LINES = ["KEY CHANGED", "RE-PAIR THIS TV", "IN ADMIN"]
REFUSED_CHANNEL = "repair"
REFUSED_LABEL = "去后台重新配对"


def picture_lines(claim: str) -> list[str]:
    """画面上的几行：标题，然后是绑定码。6 位数字（自建）一行、3 位一组；20 位的分两行、每 5 个字一组。"""
    code = claim.upper()
    if db.is_short_claim(code):
        return ["BIND THIS TV", f"{code[0:3]} {code[3:6]}"]
    return ["BIND THIS TV", f"{code[0:5]} {code[5:10]}", f"{code[10:15]} {code[15:20]}"]


def config_payload(base: dict, session: int, claim_url: str) -> dict:
    payload = dict(base, session=session, channel=BIND_CHANNEL, groups=[BIND_GROUP],
                   channel_list=[{"id": BIND_CHANNEL, "name": BIND_LABEL, "g": 0}], bound=0)
    if claim_url:
        payload["remote_url"] = claim_url
    return payload


def refused_payload(base: dict, session: int) -> dict:
    """密钥对不上的设备的 CONFIG：只有一个「台」，没有遥控地址、没有 bound、没有语音。"""
    return dict(base, session=session, channel=REFUSED_CHANNEL, groups=[BIND_GROUP],
                channel_list=[{"id": REFUSED_CHANNEL, "name": REFUSED_LABEL, "g": 0}])


def _send_picture(server, connection, session: int, wire, raw: bytes, shown: bytes | None,
                  tick: int) -> bytes:
    chosen = frames.choose_stripes(raw, shown, tick, 1 << 20)
    stamp = max(wire.audio_stamp, wire.video_stamp + 1)
    deadline = time.monotonic() + server.frame_deadline_s
    for n, part in enumerate(frames.pack_stripes(chosen)):
        send_packet(connection, Packet(Kind.JPEG, session, wire.seq, stamp, part, VIDEO_CONTINUES if n else 0),
                    deadline=deadline)
        wire.seq += 1
        server.video_sent += 1
    wire.video_stamp = stamp
    return frames.apply_stripes(raw, shown, chosen)


def serve(server, connection, admission, gate, config: dict) -> None:
    """服务一个未绑定（或自建时密钥对不上）的设备的会话，直到它离开、被绑定（主人点了重新配对）或超时。
    config 是 tv_server.CONFIG。"""
    session = secrets.randbelow(0xFFFFFFFF) + 1
    server.session_id = session
    server.phase = "unbound_config"
    refused = getattr(admission, "refused", False)
    server.logger(f"session={session} state={'refused' if refused else 'unbound'} device={admission.device_id}")
    payload = refused_payload(config, session) if refused else config_payload(config, session, admission.claim_url)
    send_packet(connection, Packet(Kind.CONFIG, session, 0, 0, json_bytes(payload)))
    send_packet(connection, Packet(Kind.PALETTE, session, 1, 0, media.default_palette()))
    raw = overlay.lines_frame(REFUSED_LINES if refused else picture_lines(admission.claim))
    wire = reconnect.WireState(seq=2, audio_pts=0)
    started = time.monotonic()
    origin = started + media.AUDIO_LEAD_MS / 1000
    shown, tick, next_frame, next_check = None, 0, 0.0, started + CHECK_S
    while not server.stop.is_set():
        server.phase = "unbound"
        reconnect._send_silence(server, connection, session, wire, origin)
        now = time.monotonic()
        if now >= next_frame:
            shown = _send_picture(server, connection, session, wire, raw, shown, tick)
            tick += 1
            next_frame = now + FRAME_S
        if now >= next_check:
            next_check = now + CHECK_S
            if refused:
                if gate.refusal_over(admission.device_id):
                    server.logger(f"session={session} device={admission.device_id} re-pair started; reconnecting")
                    return
            else:
                if gate.is_bound(admission.device_id):
                    server.logger(f"session={session} device={admission.device_id} bound; reconnecting")
                    return
                if gate.claim_changed(admission.device_id, admission.claim):
                    server.logger(f"session={session} device={admission.device_id} new bind code; reconnecting")
                    return
        if now - started > MAX_S:
            return
        if not server._wait_until(connection, time.monotonic() + 0.02, session):
            return
