"""设备保存的 Wi-Fi：手机遥控页、电脑版「设备」页的「Wi-Fi」（多用户模式）。

三处都用这里（设计见 docs/superpowers/specs/2026-10-13-wifi-setup-design.md）：

- 电视服务（gate.py / tv_server.py）：HELLO 里的 "wkey"（设备公钥，首次信任）、"wifi_saved"（保存的
  Wi-Fi 名字，按优先顺序）、"wifi"."ssid"（正在用的）记下来；STATUS 里设备对一次修改的回答（ack）记下来。
- 中转（server/relay/wifi.py）：主人点了「添加/删除/上移/立即切换」，用设备公钥加密成一条事件发出去，
  记下「已发出、等回答」；手机页面轮询这里的状态，显示「已保存」「设备不在线」「设备没有回应」。

数据库里只有 Wi-Fi 名字，从来没有密码：密码只在中转内存里停留到加密完。
"""

from __future__ import annotations

import json
import time

from .. import wifi_crypto

NETWORKS_MAX = 10
NAME_MAX = 100          # 显示名（非文本字节已换成 ?）；SSID 本身最多 32 字节
# 发出去以后多久还没回答就算「设备没有回应」。正常几百毫秒：电视服务每秒看一次事件，设备解密一次约 0.1 秒。
ACK_TIMEOUT_S = 20.0
# 事件在电视服务那里多久以后不再发：设备当时不在线，过后再上线时不该突然执行几分钟前的操作。
EVENT_MAX_AGE_S = 30.0
OPS = ("add", "remove", "up", "switch")

# 设备回答里的错误码 → 给人看的话（av_wifi_ops.h 的 av_wifi_err_code）。
ERRORS = {
    "full": "已经存满 10 个 Wi-Fi，请先删掉一个",
    "changed": "小电视上的列表刚变过，请刷新后再试",
    "replay": "这个操作已经做过了",
    "key": "小电视不认这条消息的钥匙：在小电视上重新配一次网（进入配网模式保存）后再试",
    "bad": "小电视看不懂这条消息（固件太旧？）",
    "store": "小电视保存失败，请再试一次",
    "busy": "小电视正在处理上一个操作，请稍后再试",
}


def _clean_names(value) -> list[str] | None:
    if not isinstance(value, list):
        return None
    names = []
    for item in value[:NETWORKS_MAX]:
        if not isinstance(item, str):
            return None
        names.append(item[:NAME_MAX])
    return names


def _row(accounts, device_id: str) -> dict | None:
    return accounts._one("SELECT * FROM device_wifi WHERE device_id=?", (device_id,))


def _ensure(accounts, device_id: str) -> None:
    accounts._run("INSERT OR IGNORE INTO device_wifi(device_id) VALUES (?)", (device_id,))


def hello(accounts, device_id: str, greeting: dict, repin: bool = False, logger=print) -> str:
    """设备 HELLO：记公钥（第一次见到时）、保存的列表、正在用的。返回 "off" / "pinned" / "ok" / "mismatch"。

    repin：重新配对窗口里（设备刷过机，密钥都换了），新公钥直接替换旧的。"""
    if greeting.get("wifimgr") != 1:
        return "off"
    try:
        pub = wifi_crypto.b64d(greeting.get("wkey"))
    except wifi_crypto.CryptoError:
        return "off"
    if len(pub) != 32:
        return "off"
    names = _clean_names(greeting.get("wifi_saved"))
    wifi = greeting.get("wifi") if isinstance(greeting.get("wifi"), dict) else {}
    current = wifi.get("ssid") if isinstance(wifi.get("ssid"), str) else ""
    now = accounts.clock()
    _ensure(accounts, device_id)
    row = _row(accounts, device_id)
    status = "ok"
    if row["pub"] is None or repin:
        accounts._run("UPDATE device_wifi SET pub=?, pub_mismatch_at=0, last_seq=0 WHERE device_id=?",
                      (pub, device_id))
        status = "pinned"
        if row["pub"] is not None:
            logger(f"device {device_id} wifi key re-pinned")
    elif bytes(row["pub"]) != pub:
        # 公钥变了却不在重新配对窗口里：照旧用记住的那把，Wi-Fi 管理先停用（页面提示重新配对）。
        accounts._run("UPDATE device_wifi SET pub_mismatch_at=? WHERE device_id=?", (now, device_id))
        if not row["pub_mismatch_at"]:
            logger(f"device {device_id} wifi key changed; remote Wi-Fi refused until re-paired")
        status = "mismatch"
    elif row["pub_mismatch_at"]:
        accounts._run("UPDATE device_wifi SET pub_mismatch_at=0 WHERE device_id=?", (device_id,))
    if names is not None:
        accounts._run("UPDATE device_wifi SET networks=?, current=?, reported_at=? WHERE device_id=?",
                      (json.dumps(names, ensure_ascii=False), current[:NAME_MAX], now, device_id))
    return status


def ack(accounts, device_id: str, report: dict) -> bool:
    """STATUS 里的 {"wifi": {"ack", "ok", "err", "saved", "cur"}}：设备对一次修改的回答。"""
    if not isinstance(report, dict) or _row(accounts, device_id) is None:
        return False
    seq = report.get("ack")
    if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
        return False
    error = report.get("err") if isinstance(report.get("err"), str) else "bad"
    names = _clean_names(report.get("saved"))
    current = report.get("cur") if isinstance(report.get("cur"), str) else ""
    now = accounts.clock()
    accounts._run("UPDATE device_wifi SET ack_seq=?, ack_ok=?, ack_error=?, ack_at=? WHERE device_id=?",
                  (seq, 1 if report.get("ok") == 1 else 0, error[:16], now, device_id))
    if names is not None:
        accounts._run("UPDATE device_wifi SET networks=?, current=?, reported_at=? WHERE device_id=?",
                      (json.dumps(names, ensure_ascii=False), current[:NAME_MAX], now, device_id))
    return True


def device_key(accounts, device_id: str) -> bytes | None:
    row = _row(accounts, device_id)
    return bytes(row["pub"]) if row is not None and row["pub"] is not None else None


def next_seq(accounts, device_id: str) -> int:
    """下一条消息的序号：中转的毫秒时钟，且比上一条大。"""
    row = _row(accounts, device_id)
    last = row["last_seq"] if row is not None else 0
    return max(int(accounts.clock() * 1000), last + 1)


def begin(accounts, device_id: str, seq: int, op: str, ssid: str) -> None:
    """记下这条刚发出的修改，等设备回答。"""
    pending = {"seq": seq, "op": op, "ssid": ssid[:NAME_MAX], "at": accounts.clock()}
    accounts._run("UPDATE device_wifi SET last_seq=?, pending=? WHERE device_id=?",
                  (seq, json.dumps(pending, ensure_ascii=False), device_id))


def pending_state(row: dict, now: float) -> dict | None:
    """最近一次修改到哪一步了：sent（等回答）、saved、failed（带 error）、timeout。"""
    try:
        pending = json.loads(row["pending"]) if row["pending"] else None
    except ValueError:
        pending = None
    if not isinstance(pending, dict):
        return None
    out = {k: pending.get(k) for k in ("seq", "op", "ssid", "at")}
    answered = row["ack_seq"] >= pending["seq"] or (row["ack_seq"] == 0 and row["ack_at"] >= pending["at"])
    if answered and row["ack_ok"] and row["ack_seq"] >= pending["seq"]:
        out["state"] = "saved"
    elif answered:
        out["state"] = "failed"
        out["error"] = ERRORS.get(row["ack_error"], "小电视没有保存（" + row["ack_error"] + "）")
    elif now - pending["at"] > ACK_TIMEOUT_S:
        out["state"] = "timeout"
        out["error"] = "小电视没有回应：可能刚断网，或者固件不支持。稍后刷新看看"
    else:
        out["state"] = "sent"
    return out


def state(accounts, device_id: str, now: float | None = None) -> dict:
    """给页面的：支不支持、保存的列表（只有名字）、正在用的、最近一次修改的结果。"""
    now = accounts.clock() if now is None else now
    row = _row(accounts, device_id)
    if row is None or row["pub"] is None:
        return {"supported": False, "networks": [], "current": "", "pending": None, "key_changed": False}
    try:
        names = json.loads(row["networks"])
    except ValueError:
        names = []
    return {"supported": True, "networks": names if isinstance(names, list) else [], "current": row["current"],
            "reported_at": row["reported_at"], "pending": pending_state(row, now),
            "key_changed": bool(row["pub_mismatch_at"])}


def fresh(at: float, now: float | None = None) -> bool:
    """事件还新吗（电视服务发给设备前看一眼）。"""
    now = time.time() if now is None else now
    return 0 <= now - at <= EVENT_MAX_AGE_S
