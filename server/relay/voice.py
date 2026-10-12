"""给电视服务的语音功能用的两件事：按 device 字段指定动作作用在哪台设备上，和一份简短的目录。

语音命令必须作用在说话的那台设备上，而 Relay 的控制方法都按 target()（手机正在遥控的那台）
行事。所以 POST 接口多认一个可选的 device（名字或 id，见 find_device），在这次请求的线程里
临时把 target() 换成它（acting_for）。别的线程（播放流、手机的其他请求）不受影响。

GET /api/voice/catalog?device=…：剧集（名字、集数、下一次播第几集）、UP 主全名，和这台设备
正在播的台、第几集、是否暂停。设计见 docs/superpowers/specs/2026-10-12-voice-control-design.md 第 3 节。
"""

from __future__ import annotations

import contextlib
import threading

from .. import now_playing

_acting = threading.local()


def forced_target() -> dict | None:
    """本线程临时指定的设备；没有时为 None，target() 照旧。"""
    return getattr(_acting, "device", None)


def find_device(relay, key) -> dict | None:
    """按 id（d0、d1…）或名字（--device-name、--extra-device 的名字）找设备。"""
    if not isinstance(key, str) or not key:
        return None
    return (next((d for d in relay.devices if d["id"] == key), None)
            or next((d for d in relay.devices if d["name"] == key), None))


@contextlib.contextmanager
def acting_for(device: dict | None):
    """在这个 with 里，本线程的 relay.target() 返回 device；device 为 None 时什么都不改。"""
    if device is None:
        yield
        return
    previous = forced_target()
    _acting.device = device
    try:
        yield
    finally:
        _acting.device = previous


def catalog(relay, key) -> dict:
    device = find_device(relay, key)
    if device is None:
        return {"ok": False, "error": "没有这台设备"}
    with acting_for(device):
        playing = now_playing.read(device["now"], relay.clock())
        cid = playing["channel"]
        with relay.state.lock:
            series = [{"id": c.id, "name": c.name, "total": len(c.videos),
                       "next": relay.state.episode(c.id)}
                      for c in relay.state.creators if c.kind == "series"]
            creators = [{"id": c.id, "name": c.name}
                        for c in relay.state.creators if c.kind != "series"]
        episode = 0
        paused = False
        if playing["online"] and cid[:2] == "sr":
            episode = relay._episode_of(cid, relay._on_air(cid))
        if playing["online"]:
            paused = relay._paused_on(cid) is not None
    return {"ok": True, "device": {"id": device["id"], "name": device["name"], "online": playing["online"]},
            "series": series, "creators": creators,
            "playing": {"channel": cid, "episode": episode, "paused": paused}}
