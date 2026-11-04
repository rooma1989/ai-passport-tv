#!/usr/bin/env python3
"""主人用的频道包工具（docs/superpowers/specs/2026-11-04-minimal-product-design.md 第 4 节）。

  python3 tools/channelpack.py keygen --out ~/.config/xiaozhi-tv/channels-signing.key   # 只打印公钥
  python3 tools/channelpack.py from-table channels.txt --version 2026-11-04.1 --out pack.json
  python3 tools/channelpack.py sign pack.json --key ~/.config/xiaozhi-tv/channels-signing.key --out channels-pack.json
  python3 tools/channelpack.py verify channels-pack.json [--public <hex>]

私钥只在主人自己的电脑上，从不进仓库、不打印。收集线路、截帧、网页审核是 P1b 的维护工具。
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from server import channelpack, ed25519, routes  # noqa: E402
from tools.console import use_utf8  # noqa: E402

use_utf8()    # 提示是中文，Windows 控制台默认编码打不出来


def keygen(out: Path) -> int:
    out = out.expanduser()
    if out.exists():
        print(f"{out} 已经有了，不覆盖", file=sys.stderr)
        return 1
    out.parent.mkdir(parents=True, exist_ok=True)
    secret = secrets.token_bytes(32)
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(secret.hex() + "\n")
    print(ed25519.public_key(secret).hex())
    return 0


def _channel_of(raw: str, meta: dict) -> dict:
    parts = [p.strip() for p in raw.split("|")]
    if len(parts) < 3:
        raise channelpack.PackError("少于三列")
    cid, name, url = parts[:3]
    lines = [{"url": url, **({"ua": parts[3]} if len(parts) > 3 and parts[3] else {})}]
    entry = meta.get(cid, {})
    for route in routes.backups_of(entry):
        lines.append({"url": route["url"], **({"ua": route["ua"]} if route.get("ua") else {})})
    ch = {"id": cid, "name": name, "lines": lines[:channelpack.LINES_MAX]}
    for field in ("tvg_id", "logo"):
        if entry.get(field):
            ch[field] = entry[field]
    return ch


def from_table(table: Path, version: str, out: Path) -> int:
    problem = check_version(version)
    if problem:
        print(problem, file=sys.stderr)
        return 1
    meta = routes.read_meta(table)
    channels, seen, skipped = [], set(), []
    for raw in table.read_text(encoding="utf-8").splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        try:
            ch = _channel_of(raw, meta)
            if ch["id"] in seen:
                raise channelpack.PackError("id 重复")
            if len(channels) >= channelpack.CHANNELS_MAX:
                raise channelpack.PackError("台太多")
            # 单个台先过一遍和整包一样的检查；不合格的跳过，不让一行拖垮整包
            channelpack.validate({"v": channelpack.SCHEMA, "version": version, "channels": [ch]})
        except channelpack.PackError as error:
            skipped.append(raw)
            print(f"跳过：{raw.strip()[:100]}（{error}）", file=sys.stderr)
            continue
        seen.add(ch["id"])
        channels.append(ch)
    pack = {"v": channelpack.SCHEMA, "version": version, "channels": channels}
    channelpack.validate(pack)
    out.write_text(json.dumps(pack, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{len(channels)} 个台（跳过 {len(skipped)} 行）→ {out}")
    return 0


VERSION_RE = re.compile(r"([0-9]{4}-[0-9]{2}-[0-9]{2})\.[1-9][0-9]{0,8}", re.ASCII)
VERSION_HELP = "版本号要写成 年-月-日.第几份，例如 2026-11-04.1（日期是真的日期，第几份从 1 起）"


def check_version(version) -> str | None:
    """主人签的版本号必须是 YYYY-MM-DD.N：各家只会换到更大的版本，打错一个字（比如 20261-11-04.1）
    以后发的包就都用不上了。客户端那边（server/channelpack.validate）照旧只要求是几段数字。返回出错的话或 None。"""
    match = VERSION_RE.fullmatch(version) if isinstance(version, str) else None
    try:
        if match is None:
            raise ValueError
        datetime.date.fromisoformat(match.group(1))
    except ValueError:
        return f"版本号 {version!r} 不对：{VERSION_HELP}"
    return None


def sign(draft: Path, key: Path, out: Path) -> int:
    pack = json.loads(draft.read_text(encoding="utf-8"))
    problem = check_version(pack.get("version") if isinstance(pack, dict) else None)
    if problem:
        print(problem, file=sys.stderr)
        return 1
    secret = bytes.fromhex(key.expanduser().read_text().strip())
    out.write_bytes(channelpack.wrap(pack, secret))
    print(f"签好了：{pack['version']}，{len(pack['channels'])} 个台 → {out}")
    return 0


def verify(signed: Path, public: str) -> int:
    try:
        pack = channelpack.unwrap(signed.read_bytes(), public)
    except channelpack.PackError as error:
        print(f"不能用：{error}", file=sys.stderr)
        return 1
    print(f"签名正确：{pack['version']}，{len(pack['channels'])} 个台")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    k = sub.add_parser("keygen"); k.add_argument("--out", type=Path, required=True)
    t = sub.add_parser("from-table"); t.add_argument("table", type=Path); t.add_argument("--version", required=True)
    t.add_argument("--out", type=Path, required=True)
    s = sub.add_parser("sign"); s.add_argument("draft", type=Path); s.add_argument("--key", type=Path, required=True)
    s.add_argument("--out", type=Path, required=True)
    v = sub.add_parser("verify"); v.add_argument("signed", type=Path)
    v.add_argument("--public", default=channelpack.PUBLIC_KEY_HEX)
    a = ap.parse_args(argv)
    if a.cmd == "keygen":
        return keygen(a.out)
    if a.cmd == "from-table":
        return from_table(a.table, a.version, a.out)
    if a.cmd == "sign":
        return sign(a.draft, a.key, a.out)
    return verify(a.signed, a.public)


if __name__ == "__main__":
    sys.exit(main())
