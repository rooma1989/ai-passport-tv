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

from . import ed25519, routes
from .relay import channeltable

SCHEMA = 1
PUBLIC_KEY_HEX = ""          # 任务 11 填：主人频道签名密钥的公钥（公开的，可以进仓库）
ID_MAX = channeltable.ID_MAX
NAME_MAX = channeltable.NAME_MAX
URL_MAX = channeltable.URL_MAX
LINES_MAX = routes.BACKUPS_MAX + 1
PREFS_NAME = "channels-prefs.json"
STATE_NAME = "channels-pack.json"   # 上次用上的那份（原样，带签名），改偏好时用它重写频道表


class PackError(ValueError):
    """频道包不能用：消息给人看（中文）。"""


def wrap(pack: dict, secret: bytes) -> bytes:
    validate(pack)
    payload = json.dumps(pack, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    sig = ed25519.sign(secret, payload.encode("utf-8")).hex()
    return json.dumps({"v": SCHEMA, "payload": payload, "sig": sig}, ensure_ascii=False).encode("utf-8")


def unwrap(raw: bytes, public_hex: str) -> dict:
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
    return isinstance(value, str) and 0 < len(value) <= limit and "|" not in value \
        and not any(ord(c) < 32 for c in value)


def validate(pack) -> None:
    if not isinstance(pack, dict) or pack.get("v") != SCHEMA or not _text(pack.get("version"), 40):
        raise PackError("频道包的版本号不对")
    channels = pack.get("channels")
    if not isinstance(channels, list) or not channels:
        raise PackError("频道包里没有台")
    seen = set()
    for n, ch in enumerate(channels, start=1):
        if not isinstance(ch, dict):
            raise PackError(f"第 {n} 个台格式不对")
        cid = ch.get("id")
        if not _text(cid, ID_MAX) or not cid.isascii() or not cid.isprintable() or " " in cid or cid in seen:
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
            if "ua" in line and not _text(line["ua"], 200):
                raise PackError(f"台 {cid} 有一条线路的 User-Agent 不对")


def read_prefs(channels_path: Path) -> dict:
    try:
        data = json.loads((Path(channels_path).parent / PREFS_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"hidden": [], "order": []}
    hidden = [x for x in data.get("hidden", []) if isinstance(x, str)] if isinstance(data, dict) else []
    order = [x for x in data.get("order", []) if isinstance(x, str)] if isinstance(data, dict) else []
    return {"hidden": hidden, "order": order}


def ordered(pack: dict, prefs: dict) -> list[dict]:
    """包里的台按用户的顺序（没排过的接在后面，按包里的顺序），去掉隐藏的。"""
    by_id = {ch["id"]: ch for ch in pack["channels"]}
    order = [cid for cid in prefs.get("order", []) if cid in by_id]
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
