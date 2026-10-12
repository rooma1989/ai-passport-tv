"""把单用户的文件迁移进多用户数据库；也能反过来导出一份 relay-state.json（回滚用）。

    # 先演练：在临时库里完整跑一遍，打印两边的计数，什么都不写
    python3 -m server.tenancy.migrate --data-dir /var/lib/ai-passport-tv --dry-run
    # 真的迁移（数据库必须是空的；--replace 先把旧库改名留底再重建）
    python3 -m server.tenancy.migrate --data-dir /var/lib/ai-passport-tv --public-url https://<site>
    # 回滚前：把切换以后的进度导出成单用户的状态文件（写到新文件，旧文件不动）
    python3 -m server.tenancy.migrate --data-dir /var/lib/ai-passport-tv --export /tmp/relay-state.export.json

迁移只读旧文件，从不修改或删除它们：它们就是回滚。读之前和写完之后各算一遍 SHA-256，
对不上就报错。设计见 docs/superpowers/specs/2026-10-12-multi-tenant-design.md 第 9 节，
操作步骤见 docs/development/tenancy-phase1-runbook.md。

对应关系（设计第 4.4 节）：
- relay-state.json 的 creators（UP 主和剧集，连同看过的、进度）、history → 管理员的用户状态；
  creators 的视频列表也写进共享缓存（source_cache）。
- cast（投屏位）→ 两台旧设备各一份（以前两台共用一个投屏位）。
- sessions（明文）→ 会话表里只存哈希，管理员的手机会话，有效期从原来登录的时间算。
- code（扫码码）→ 小智那台旧设备的遥控码，贴着的二维码照样能扫。
- channel-groups.json → 管理员的分组调整。
- channels.txt → 不动：它仍是全局的直播频道表，只数一下有几个台。
- now-playing.json、relay-events.json → 复制到 devices/<旧设备 id>/。
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

from .. import channel_groups
from ..relay.state import CODE_ALPHABET, CODE_LENGTH, State
from . import files as device_files
from .accounts import Accounts

DEFAULT_DATA_DIR = Path("/var/lib/ai-passport-tv")
DEFAULT_PASSPORT_DIR = Path("/var/lib/ai-passport-tv-passport")
XINGZHI = ("legacy-xingzhi", 8098, "xingzhi-cube-154")
PASSPORT = ("legacy-passport", 8097, "ai-passport")


def _sha(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except FileNotFoundError:
        return "-"


def _counts_from_state(state: State) -> dict:
    with state.lock:
        creators = [c for c in state.creators if c.kind == "up"]
        series = [c for c in state.creators if c.kind == "series"]
        return {"creators": len(creators), "series": len(series),
                "videos": sum(len(c.videos) for c in state.creators),
                "watched": sum(len(c.watched) for c in state.creators),
                "positions": sum(len(c.positions) for c in state.creators),
                "history": len(state.history)}


def source_counts(state: State, groups: dict, channels_file: Path | None) -> dict:
    counts = _counts_from_state(state)
    counts.update(sessions=len(state.sessions), cast=int(state.cast is not None),
                  groups=len(groups), code=int(_valid_code(state.code)))
    counts["live_channels"] = len(channel_groups.read_table(channels_file)) if channels_file else 0
    return counts


def database_counts(accounts: Accounts, channels_file: Path | None, keep_code: str) -> dict:
    admin = accounts.admin_id()
    data = accounts.load_user_state(admin) or {"creators": []}
    state = State(store=_Fixed(data))
    counts = _counts_from_state(state)
    cast = accounts.load_device_state(XINGZHI[0]) or {}
    xingzhi = accounts.device(XINGZHI[0])
    counts.update(sessions=accounts.count_sessions(), cast=int(bool(cast.get("cast"))),
                  groups=len(data.get("group_overrides") or {}),
                  code=int(bool(keep_code) and xingzhi is not None and xingzhi["remote_code"] == keep_code))
    counts["live_channels"] = len(channel_groups.read_table(channels_file)) if channels_file else 0
    return counts


class _Fixed:
    def __init__(self, data):
        self.data = data

    def load(self):
        return self.data

    def save(self, data):
        self.data = data


def _valid_code(code: str) -> bool:
    return len(code) == CODE_LENGTH and all(c in CODE_ALPHABET for c in code)


def migrate(accounts: Accounts, state: State, groups: dict, devices_dir: Path, legacy: list[tuple],
            admin_name: str, public_url: str = "", copies: dict | None = None, log=print) -> None:
    """把读进来的单用户状态写进（空的）数据库，并给旧设备准备好文件目录。"""
    admin = accounts.create_user(admin_name, "admin")
    data = state.snapshot()
    user_state = {"creators": data["creators"], "history": data.get("history") or [],
                  "group_overrides": groups}
    accounts.save_user_state(admin, user_state)
    for creator in state.creators:
        accounts.cache_source(creator.site, creator.source, creator.kind, creator.name,
                              [dataclasses.asdict(v) for v in creator.videos], creator.fetched_at)
    for token, created in state.sessions.items():
        accounts.import_session(token, admin, created)
    cast = data.get("cast")
    for n, (device_id, port, model, name) in enumerate(legacy):
        # 原来的扫码码给第一台（小智）：它状态页上的二维码和贴着的打印件照样能扫。
        code = state.code if n == 0 and _valid_code(state.code) else ""
        accounts.add_legacy_device(device_id, port, model, name, admin, remote_code=code)
        if cast is not None:
            accounts.save_device_state(device_id, {"cast": cast})
        target = device_files.ensure_dir(devices_dir, device_id)
        for source, dest in ((copies or {}).get(device_id) or {}).items():
            if source.is_file():
                shutil.copyfile(source, getattr(target, dest))
    if public_url:
        accounts.set_meta("public_url", public_url.rstrip("/"))
    accounts.set_meta("legacy_token", "1")
    accounts.set_meta("migrated_at", str(time.time()))
    accounts.audit("migrate", admin, detail=f"creators={len(state.creators)} sessions={len(state.sessions)}")
    # 每台设备的频道表、分组、遥控地址：和中转启动时写的完全一样（用同一段代码）。
    from ..relay.web import Relay
    relay = Relay(None, None, None, "ffmpeg", "-" * 16, accounts=accounts, devices_dir=devices_dir,
                  public_url=public_url, log=log)
    relay.sync_devices(force=True)


def export(accounts: Accounts, base: Path | None, out: Path) -> dict:
    """数据库 → 单用户的 relay-state.json：管理员的订阅和历史、小智那台的投屏位。

    会话和扫码码取自 base（旧的状态文件，数据库里只有会话的哈希）：回滚后手机不用重新扫码。
    """
    if out.exists():
        raise ValueError(f"{out} 已经存在；导出只写新文件")
    admin = accounts.admin_id()
    data = accounts.load_user_state(admin) or {"creators": []}
    record = {"creators": data["creators"], "history": data.get("history") or []}
    device = accounts.device(XINGZHI[0])
    if device is not None and device["status"] == "retired":
        # 接收（adopt）以后小智的投屏位在新 id 下：找接收了它遥控码的那台不方便，按主人找第一台。
        rows = accounts.devices_of(admin)
        device = rows[0] if rows else None
    cast = (accounts.load_device_state(device["id"]) or {}).get("cast") if device is not None else None
    if cast:
        record["cast"] = cast
    old = json.loads(base.read_text(encoding="utf-8")) if base is not None and base.is_file() else {}
    record["sessions"] = old.get("sessions") or {}
    record["code"] = old.get("code") or ""
    # 和 State.save() 一样的格式和权限；先用 State 读一遍，确认旧中转读得懂。
    out.write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
    out.chmod(0o640)
    State(out)
    return {"creators": len(record["creators"]), "history": len(record["history"]),
            "cast": int("cast" in record), "sessions": len(record["sessions"])}


def _print_counts(before: dict, after: dict) -> bool:
    names = {"creators": "UP 主", "series": "剧集", "videos": "视频（列表里）", "watched": "看过的标记",
             "positions": "播放进度", "history": "投屏历史", "sessions": "手机会话", "cast": "投屏位",
             "groups": "分组调整", "code": "扫码码保留", "live_channels": "直播台（channels.txt）"}
    def pad(text: str, columns: int) -> str:
        # 汉字占两格，按显示宽度补空格，表格才对得齐。
        return text + " " * max(0, columns - sum(2 if ord(c) > 127 else 1 for c in text))

    same = True
    print(f"{pad('', 24)}{pad('旧文件', 8)}数据库")
    for key, label in names.items():
        mark = "" if before[key] == after[key] else "  ← 不一致"
        same = same and not mark
        print(f"{pad(label, 24)}{before[key]:<8}{after[key]:<8}{mark}")
    return same


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        # 管道下默认取本地编码；中文输出固定 UTF-8（和 tv_server.main 一样）。
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(prog="python3 -m server.tenancy.migrate", description=__doc__.split("\n\n")[0])
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR,
                        help="小智那台电视服务和中转的数据目录（relay-state.json 等所在）")
    parser.add_argument("--passport-dir", type=Path, default=DEFAULT_PASSPORT_DIR,
                        help="Passport 那台电视服务的数据目录（now-playing.json、relay-events.json）")
    parser.add_argument("--no-passport", action="store_true", help="没有 Passport 那台旧设备")
    parser.add_argument("--state", type=Path, help="默认 DATA_DIR/relay-state.json")
    parser.add_argument("--groups", type=Path, help="默认 DATA_DIR/channel-groups.json")
    parser.add_argument("--channels", type=Path, help="默认 DATA_DIR/channels.txt（只计数，不动）")
    parser.add_argument("--db", type=Path, help="默认 DATA_DIR/tv.db")
    parser.add_argument("--devices-dir", type=Path, help="默认 DATA_DIR/devices")
    parser.add_argument("--public-url", default="", help="网站地址，例如 https://example.com（写遥控二维码地址用）")
    parser.add_argument("--admin-name", default="管理员")
    parser.add_argument("--xingzhi-name", default="小智")
    parser.add_argument("--passport-name", default="Passport")
    parser.add_argument("--dry-run", action="store_true", help="在临时库里演练，打印计数，不写任何东西")
    parser.add_argument("--replace", action="store_true",
                        help="数据库不是空的时：先把它改名留底（tv.db.bak-<时间>），再重新迁移")
    parser.add_argument("--export", type=Path, metavar="OUT",
                        help="反过来：把数据库导出成单用户的状态文件 OUT（回滚用；OUT 不能已存在）")
    args = parser.parse_args(argv)

    data_dir = args.data_dir
    state_path = args.state or data_dir / "relay-state.json"
    groups_path = args.groups or data_dir / "channel-groups.json"
    channels_path = args.channels or data_dir / "channels.txt"
    db_path = args.db or data_dir / "tv.db"
    devices_dir = args.devices_dir or data_dir / "devices"

    if args.export is not None:
        if not db_path.is_file():
            print(f"{db_path} 不存在", file=sys.stderr)
            return 2
        try:
            counts = export(Accounts(db_path), state_path, args.export)
        except ValueError as error:
            print(error, file=sys.stderr)
            return 2
        print(f"已导出到 {args.export}：" + "，".join(f"{k} {v}" for k, v in counts.items()))
        print("回滚时停掉中转，把它放到 --state 指向的位置（先把原文件留底），再用旧的命令行启动。")
        return 0

    if not state_path.is_file():
        print(f"{state_path} 不存在：没有可迁移的状态", file=sys.stderr)
        return 2
    inputs = [state_path, groups_path, channels_path, data_dir / "now-playing.json", data_dir / "relay-events.json"]
    passport = not args.no_passport
    if passport:
        inputs += [args.passport_dir / "now-playing.json", args.passport_dir / "relay-events.json"]
    before = {path: _sha(path) for path in inputs}
    print("旧文件（只读，迁移前后的 SHA-256 必须相同）：")
    for path, digest in before.items():
        print(f"  {digest[:16] if digest != '-' else '（没有）':<18}{path}")

    try:
        state = State(state_path)
    except ValueError as error:
        print(error, file=sys.stderr)
        return 2
    groups = channel_groups.load_overrides(groups_path)
    legacy = [(*XINGZHI, args.xingzhi_name)] + ([(*PASSPORT, args.passport_name)] if passport else [])
    copies = {XINGZHI[0]: {data_dir / "now-playing.json": "now", data_dir / "relay-events.json": "events"}}
    if passport:
        copies[PASSPORT[0]] = {args.passport_dir / "now-playing.json": "now",
                               args.passport_dir / "relay-events.json": "events"}
    expected = source_counts(state, groups, channels_path if channels_path.is_file() else None)

    work = None
    if args.dry_run:
        work = Path(tempfile.mkdtemp(prefix="tv-migrate-"))
        target_db, target_devices = work / "tv.db", work / "devices"
    else:
        target_db, target_devices = db_path, devices_dir
        if target_db.exists():
            existing = Accounts(target_db)
            empty = existing.is_empty()
            existing.close()
            if not empty and not args.replace:
                print(f"{target_db} 里已经有数据：迁移只在空库上跑。要重来就加 --replace（旧库会改名留底）。",
                      file=sys.stderr)
                return 2
            if not empty:
                backup = target_db.with_name(f"{target_db.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
                for suffix in ("", "-wal", "-shm"):
                    extra = Path(str(target_db) + suffix)
                    if extra.exists():
                        extra.rename(Path(str(backup) + suffix))
                print(f"原来的数据库已改名为 {backup}")
    try:
        accounts = Accounts(target_db)
        migrate(accounts, state, groups, target_devices, legacy, args.admin_name, args.public_url, copies,
                log=lambda message: None)
        got = database_counts(accounts, channels_path if channels_path.is_file() else None,
                              state.code if _valid_code(state.code) else "")
        devices = accounts.all_devices()
        accounts.close()
        print()
        same = _print_counts(expected, got)
        print()
        print("设备：" + "；".join(f"{d['id']}（端口 {d['legacy_port']}，{d['name']}）" for d in devices))
        after = {path: _sha(path) for path in inputs}
        untouched = after == before
        print("旧文件没有改动。" if untouched else "旧文件被改动了！")
        if not (same and untouched):
            return 1
        if args.dry_run:
            print("演练完成：什么都没写。去掉 --dry-run 正式迁移。")
        else:
            print(f"已迁移到 {target_db}，设备文件在 {target_devices}。")
        return 0
    finally:
        if work is not None:
            shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
