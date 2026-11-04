"""精简版的内置频道包：主人签名发布，各家程序验签后写进 channels.txt 和它旁边的 meta。

设计见 docs/superpowers/specs/2026-11-04-minimal-product-design.md 第 4 节。文件是
{"v": 1, "payload": "<频道包 JSON 字符串>", "sig": "<Ed25519 签名的 hex>"}：签的是 payload 的 UTF-8 字节，
所以不用担心 JSON 重新排版。验签用 server/ed25519.py；签名只在主人电脑上做（tools/channelpack.py）。

写入频道表时：每个台的第一条线路是主线路（channels.txt 那一行），其余写进 meta 的 routes（server/routes.py）；
这家自己学到的 good、checks、starts、quality 不动；包里没有了的台连 meta 一起删掉。用户只能隐藏和排序
（同目录的 channels-prefs.json：{"hidden": [台 id], "order": [台 id]}）。
"""

from __future__ import annotations

import json
from pathlib import Path

from . import channel_groups, ed25519, routes
from .relay import channeltable

SCHEMA = 1
PUBLIC_KEY_HEX = ""          # 任务 11 填：主人频道签名密钥的公钥（公开的，可以进仓库）
ID_MAX = channeltable.ID_MAX
LINE_MAX = channeltable.LINE_MAX     # 频道表一行的字节上限
NAME_MAX = channeltable.NAME_MAX
URL_MAX = channeltable.URL_MAX
LINES_MAX = routes.BACKUPS_MAX + 1
AGENT_MAX = routes.AGENT_MAX
CHANNELS_MAX = channel_groups.TABLE_MAX + channel_groups.CHANNEL_MAX    # 再多电视服务读表时会悄悄丢掉；不 import server.live（它拖进 numpy），测试里核对没跑偏
PREFS_NAME = "channels-prefs.json"
STATE_NAME = "channels-pack.json"   # 上次用上的那份（原样，带签名），改偏好时用它重写频道表


class PackError(ValueError):
    """频道包不能用：消息给人看（中文）。"""


def wrap(pack: dict, secret: bytes) -> bytes:
    """校验后签名，返回信封的字节（只在主人电脑上用）。"""
    validate(pack)
    payload = json.dumps(pack, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    sig = ed25519.sign(secret, payload.encode("utf-8")).hex()
    return json.dumps({"v": SCHEMA, "payload": payload, "sig": sig}, ensure_ascii=False).encode("utf-8")


def unwrap(raw: bytes, public_hex: str) -> dict:
    """验签并校验从网上取来的字节，返回频道包；任何不对都只抛 PackError。"""
    try:
        envelope = json.loads(raw)
        payload, sig = envelope["payload"], bytes.fromhex(envelope["sig"])
        public = bytes.fromhex(public_hex)
    except (ValueError, KeyError, TypeError, RecursionError):   # 网上取来的字节：什么都可能是
        raise PackError("频道包格式不对") from None
    try:
        signed = isinstance(payload, str) and ed25519.verify(public, payload.encode("utf-8"), sig)
    except (ValueError, TypeError):   # 含单独的代理字符，utf-8 编不出来
        signed = False
    if not signed:
        raise PackError("频道包签名不对")
    try:
        pack = json.loads(payload)
    except (ValueError, RecursionError):
        raise PackError("频道包内容不是 JSON") from None
    validate(pack)
    return pack


def _text(value, limit: int) -> bool:
    """能原样写进频道表一格的文字：单行、首尾没有空白、没有 |、不超长。"""
    return isinstance(value, str) and 0 < len(value) <= limit and "|" not in value \
        and value == value.strip() and len(value.splitlines()) == 1 \
        and not any(ord(c) < 32 for c in value)


def validate(pack) -> None:
    """检查频道包的每一项；凡是通过的，写出来的 channels.txt 电视服务都读得进、读回来一样。"""
    if not isinstance(pack, dict) or pack.get("v") != SCHEMA or not _text(pack.get("version"), 40):
        raise PackError("频道包的版本号不对")
    channels = pack.get("channels")
    if not isinstance(channels, list) or not channels:
        raise PackError("频道包里没有台")
    if len(channels) > CHANNELS_MAX:
        raise PackError(f"频道包的台太多（最多 {CHANNELS_MAX} 个）")
    seen = set()
    for n, ch in enumerate(channels, start=1):
        if not isinstance(ch, dict):
            raise PackError(f"第 {n} 个台格式不对")
        cid = ch.get("id")
        if not _text(cid, ID_MAX) or cid.startswith("#") \
                or not all("!" <= c <= "~" for c in cid) or cid in seen:
            raise PackError(f"第 {n} 个台的 id 不对或重复")
        seen.add(cid)
        if not _text(ch.get("name"), NAME_MAX):
            raise PackError(f"台 {cid} 的名字不对")
        lines = ch.get("lines")
        if not isinstance(lines, list) or not 1 <= len(lines) <= LINES_MAX:
            raise PackError(f"台 {cid} 的线路数不对（1 到 {LINES_MAX} 条）")
        for line in lines:
            url = line.get("url") if isinstance(line, dict) else None
            if not _text(url, URL_MAX) or not url.startswith(("http://", "https://")) or " " in url:
                raise PackError(f"台 {cid} 有一条线路地址不对")
            if "ua" in line and not _text(line["ua"], AGENT_MAX):
                raise PackError(f"台 {cid} 有一条线路的 User-Agent 不对")
        for field in ("tvg_id", "logo"):
            if field in ch and channeltable.clean_meta({field: ch[field]}) != {field: ch[field]}:
                raise PackError(f"台 {cid} 的 {field} 不对")
        row = {"id": cid, "name": ch["name"], "url": lines[0]["url"], "agent": lines[0].get("ua", "")}
        if len(channeltable.line_of(row).encode("utf-8")) > LINE_MAX:
            raise PackError(f"台 {cid} 在频道表里的一行太长")


def read_prefs(channels_path: Path) -> dict:
    try:
        data = json.loads((Path(channels_path).parent / PREFS_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"hidden": [], "order": []}
    def ids(key):
        found = data.get(key, []) if isinstance(data, dict) else []
        return list(dict.fromkeys(x for x in found if isinstance(x, str))) if isinstance(found, list) else []
    return {"hidden": ids("hidden"), "order": ids("order")}


def ordered(pack: dict, prefs: dict) -> list[dict]:
    """包里的台按用户的顺序（没排过的接在后面，按包里的顺序），去掉隐藏的。"""
    by_id = {ch["id"]: ch for ch in pack["channels"]}
    order = list(dict.fromkeys(cid for cid in prefs.get("order", []) if cid in by_id))
    order += [ch["id"] for ch in pack["channels"] if ch["id"] not in order]
    hidden = set(prefs.get("hidden", []))
    return [by_id[cid] for cid in order if cid not in hidden]


def apply(pack: dict, channels_path: Path, prefs: dict) -> dict:
    """把频道包写进频道表和 meta（有备份，channeltable.save）。返回 {"version", "channels"}。"""
    channels_path = Path(channels_path)
    shown = ordered(pack, prefs)
    rows = [{"id": ch["id"], "name": ch["name"], "url": ch["lines"][0]["url"],
             "agent": ch["lines"][0].get("ua", "")} for ch in shown]
    text = channeltable.render(rows)
    keep = {ch["id"] for ch in pack["channels"]}
    by_id = {ch["id"]: ch for ch in pack["channels"]}

    def change(data: dict) -> None:
        for cid in [k for k in data if k not in keep]:
            del data[cid]
        for cid, ch in by_id.items():
            entry = data.setdefault(cid, {})
            backups = [{"url": l["url"], **({"ua": l["ua"]} if l.get("ua") else {})} for l in ch["lines"][1:]]
            if backups:
                entry["routes"] = backups
            else:
                entry.pop("routes", None)
            for field in ("tvg_id", "logo"):
                if ch.get(field):
                    entry[field] = ch[field]
                else:
                    entry.pop(field, None)
            if not entry:
                del data[cid]

    routes.update_meta(channels_path, change)
    current = channels_path.read_text(encoding="utf-8") if channels_path.is_file() else ""
    if current != text:
        channeltable.save(channels_path, text)
    return {"version": pack["version"], "channels": len(rows)}


def _write(target: Path, text: str | bytes) -> None:
    tmp = target.with_name(target.name + ".tmp")
    if isinstance(text, bytes):
        tmp.write_bytes(text)
    else:
        tmp.write_text(text, encoding="utf-8")
    tmp.replace(target)


def remember(raw: bytes, channels_path: Path) -> None:
    """记下上次用上的那份频道包（原样），改偏好时用它重写频道表。"""
    _write(Path(channels_path).parent / STATE_NAME, raw)


def remembered(channels_path: Path, public_hex: str) -> dict | None:
    try:
        return unwrap((Path(channels_path).parent / STATE_NAME).read_bytes(), public_hex)
    except (OSError, PackError):
        return None


def save_prefs(channels_path: Path, prefs: dict) -> None:
    _write(Path(channels_path).parent / PREFS_NAME, json.dumps(prefs, ensure_ascii=False))


def admin_view(channels_path: Path | None, public_hex: str) -> dict:
    """管理 → 频道：包的版本、所有台（用户的顺序）、哪些隐藏了。"""
    pack = remembered(channels_path, public_hex) if channels_path is not None else None
    if pack is None:
        return {"ok": True, "configured": False}
    prefs = read_prefs(channels_path)
    hidden = set(prefs["hidden"])
    everything = ordered(pack, {"order": prefs["order"], "hidden": []})
    return {"ok": True, "configured": True, "version": pack["version"],
            "channels": [{"id": ch["id"], "name": ch["name"], "hidden": ch["id"] in hidden} for ch in everything]}


def set_prefs(channels_path: Path | None, public_hex: str, body) -> dict:
    """保存隐藏和排序，并用记下的那份包重写频道表。"""
    pack = remembered(channels_path, public_hex) if channels_path is not None else None
    if pack is None:
        return {"ok": False, "error": "还没有频道包"}
    hidden = body.get("hidden") if isinstance(body, dict) else None
    order = body.get("order") if isinstance(body, dict) else None
    known = {ch["id"] for ch in pack["channels"]}
    if not isinstance(hidden, list) or not isinstance(order, list) \
            or not all(isinstance(x, str) and x in known for x in hidden + order):
        return {"ok": False, "error": "请求格式不对"}
    prefs = {"hidden": list(dict.fromkeys(hidden)), "order": list(dict.fromkeys(order))}
    save_prefs(channels_path, prefs)
    return {"ok": True, **apply(pack, channels_path, prefs)}
