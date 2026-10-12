"""给电视服务的语音功能用的简短目录：GET /api/voice/catalog?device=…

语音命令必须作用在说话的那台设备上，而不是手机正在遥控的那台（Relay.target()）。所以电视服务
调中转时总是带 device（server/voice/relay_client.py）：
- 单用户（没有 --db）：device 是中转里的 id（d0、d1…）或名字（--device-name、--extra-device 的
  名字），即电视服务的 TV_RELAY_DEVICE；
- 多用户（--db）：device 是设备 id（legacy-xingzhi、legacy-passport 或 12 位 MAC），由电视服务在
  认出设备后填上；带密钥的这类请求按那台设备的访客处理（web.py 的 _DEVICE_SCOPED），只能动那一台，
  剧集和进度按设备主人的来。

目录内容：剧集（名字、网站、集数、下一次播第几集）、UP 主（全名、网站），和这台设备正在播的台、
第几集、是否暂停。网站（bilibili / youtube）用来回答「有哪些 YouTube 频道」。
设计见 docs/superpowers/specs/2026-10-12-voice-control-design.md 第 3 节。
"""

from __future__ import annotations

from .tenants import LEGACY, Caller


def catalog(relay, key, caller: Caller = LEGACY) -> dict:
    device = relay.find_device(key, caller)
    if device is None:
        return {"ok": False, "error": "没有这台设备"}
    state = relay._state_of(device)
    playing = relay._playing(device)
    cid = playing["channel"]
    with state.lock:
        series = [{"id": c.id, "name": c.name, "site": c.site, "total": len(c.videos),
                   "next": state.episode(c.id)} for c in state.creators if c.kind == "series"]
        creators = [{"id": c.id, "name": c.name, "site": c.site}
                    for c in state.creators if c.kind != "series"]
    episode = 0
    paused = False
    if playing["online"] and cid[:2] == "sr":
        episode = relay._episode_of(state, cid, relay._on_air(device, cid))
    if playing["online"]:
        paused = relay._paused_on(device, cid) is not None
    return {"ok": True, "device": {"id": device["id"], "name": device["name"], "online": playing["online"]},
            "series": series, "creators": creators,
            "playing": {"channel": cid, "episode": episode, "paused": paused}}
