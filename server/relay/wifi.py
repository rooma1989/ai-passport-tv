"""手机遥控页、电脑版的「Wi-Fi」：看设备保存了哪些 Wi-Fi，添加、删除、上移、立即切换（多用户模式）。

- 只有设备主人（和是这台设备成员的管理员）能看能改，访客、家人成员都不行：Wi-Fi 的名字也算家里的事。
- 修改用设备的公钥加密（server/wifi_crypto.py），写成一条 wifi 事件；电视服务原样转给设备，只有设备解得开。
  密码只在这次请求的内存里，从不写进文件、数据库或日志。
- 设备在线才发：不在线时直接告诉页面「小电视不在线」，不排队——过几分钟设备上线时突然换网不合适。
- 页面发出后轮询 GET /api/wifi，看到 saved / failed / timeout 就停（server/tenancy/wifi.py 的 pending_state）。

GET  /api/wifi?device=<id>
POST /api/wifi {"device", "op": add|remove|up|switch, "ssid", "password"（只给 add）, "i"（remove/up/switch）}
"""

from __future__ import annotations

import json
from pathlib import Path

from .. import now_playing, wifi_crypto
from ..tenancy import wifi as device_wifi
from . import events
from .tenants import NotFound

PUB_META = "wifi_pub"       # 和 tv_server.WIFI_PUB_META 相同
SSID_BYTES = 32
PASSWORD_MAX = 64


def setup_key(relay, key_path: Path | None) -> None:
    """多用户启动时：读（或第一次生成）中转的 Wi-Fi 私钥，公钥写进数据库给电视服务放进 CONFIG。"""
    if relay.accounts is None:
        return
    path = key_path or Path(relay.accounts.path).parent / "wifi-key"
    relay.wifi_private = wifi_crypto.load_or_create_key(path)
    relay.accounts.set_meta(PUB_META, wifi_crypto.b64e(wifi_crypto.public_key(relay.wifi_private)))


def _device(relay, caller, device_id) -> dict:
    """主人（或是成员的管理员）的那台；别人一律 NotFound（404，和不存在一样）。"""
    if not relay.tenant or not isinstance(device_id, str) or not device_id:
        raise NotFound("wifi")
    relay._owned(device_id, caller)
    dev = next((d for d in relay.devices if d["id"] == device_id), None)
    if dev is None:
        raise NotFound(device_id)
    return dev


def get(relay, caller, device_id) -> dict:
    dev = _device(relay, caller, device_id)
    now = relay.clock()
    answer = device_wifi.state(relay.accounts, dev["id"], now)
    answer.update(ok=True, online=now_playing.read(dev["now"], now)["online"], name=dev["name"])
    return answer


def _valid_ssid(ssid) -> bool:
    return isinstance(ssid, str) and 0 < len(ssid.encode("utf-8")) <= SSID_BYTES and \
        all(ord(c) >= 32 for c in ssid)


def post(relay, caller, body: dict, ip: str = "") -> dict:
    dev = _device(relay, caller, body.get("device"))
    op, ssid = body.get("op"), body.get("ssid")
    if op not in device_wifi.OPS:
        return {"ok": False, "error": "请求格式不对"}
    fields = {"ssid": ssid}
    if op == "add":
        password = body.get("password") if body.get("password") is not None else ""
        if not _valid_ssid(ssid):
            return {"ok": False, "error": "Wi-Fi 名字要 1 到 32 个字节（汉字一个算 3 个）"}
        if not isinstance(password, str) or len(password) > PASSWORD_MAX or (0 < len(password) < 8) or \
                any(ord(c) < 32 for c in password):
            return {"ok": False, "error": "密码要 8 到 63 个字符（没有密码的 Wi-Fi 留空）"}
        fields["password"] = password
    else:
        index = body.get("i")
        if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < device_wifi.NETWORKS_MAX \
                or not isinstance(ssid, str) or len(ssid) > device_wifi.NAME_MAX:
            return {"ok": False, "error": "请求格式不对"}
        fields["i"] = index
    now = relay.clock()
    state = device_wifi.state(relay.accounts, dev["id"], now)
    if not state["supported"]:
        return {"ok": False, "error": "这台小电视的固件还不支持在手机上管理 Wi-Fi，请先升级"}
    if state["key_changed"]:
        return {"ok": False, "error": "小电视的钥匙变了（刷过机？）：先在「设备」里点「重新配对」，"
                                      "再让小电视重新连一次网"}
    pending = state["pending"]
    if pending and pending["state"] == "sent":
        return {"ok": False, "error": "上一个操作还没有回音，请稍等几秒"}
    if not now_playing.read(dev["now"], now)["online"]:
        return {"ok": False, "offline": True, "error": "小电视不在线：打开它、连上网以后再改"}
    private = getattr(relay, "wifi_private", None)
    public = device_wifi.device_key(relay.accounts, dev["id"])
    if private is None or public is None or dev.get("events") is None:
        return {"ok": False, "error": "服务器还没有准备好 Wi-Fi 钥匙"}
    seq = device_wifi.next_seq(relay.accounts, dev["id"])
    try:
        envelope = wifi_crypto.seal(private, public, wifi_crypto.plaintext(seq, op, **fields))
    except wifi_crypto.CryptoError:
        return {"ok": False, "error": "Wi-Fi 名字或密码太长"}
    finally:
        fields.pop("password", None)
    events.write_event(dev["events"], "wifi", value=int(now), text=json.dumps(envelope, separators=(",", ":")))
    device_wifi.begin(relay.accounts, dev["id"], seq, op, ssid if isinstance(ssid, str) else "")
    relay.accounts.audit(f"wifi.{op}", caller.user, ip, "device", dev["id"])
    relay.log(f"{dev['id']} Wi-Fi {op} 已发出")
    return {"ok": True, "seq": seq}
