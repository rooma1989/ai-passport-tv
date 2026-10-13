"""管理员的命令行（阶段 3 的管理网页之前，管理只靠它和手机遥控上的几个按钮）。

    python3 -m server.tenancy.admin [--db /var/lib/ai-passport-tv/tv.db] <命令> …

命令：
    users                       列出用户
    devices                     列出所有设备（含未绑定、退役的）
    invite [--name 名字] [--admin]  生成一个邀请链接（7 天内有效，用一次）
    login-link <用户 id>        给这个用户一个手机登录链接（5 分钟内有效，用一次）：他的手机全退出了、
                                设备也不在手边时的最后办法（平时用设备状态页上的「恢复登录」）
    adopt <旧设备 id> <新 id>   旧设备刷了新固件：把旧记录的主人、遥控码、投屏位搬到真 id 上
    unbind <设备 id>            解绑（回到未绑定，换绑定码和遥控码）
    repair <设备 id>            10 分钟内接受这台设备的新密钥（擦过 flash 之后）
    ban-device / unban-device <设备 id>
    ban-user / unban-user <用户 id>
    legacy-token on|off         共享配对令牌（旧固件）开关
    audit [--limit N]           最近的操作记录

中转每 10 秒读一次数据库里的设备，命令改的东西不用重启中转就会生效。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from .accounts import Accounts
from .gate import LEGACY_TOKEN_KEY
from .phonelinks import PhoneLinks


def _when(at: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(at)) if at else "-"


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        # 管道下默认取本地编码；中文输出固定 UTF-8（和 tv_server.main 一样）。
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(prog="python3 -m server.tenancy.admin")
    parser.add_argument("--db", type=Path, default=Path("/var/lib/ai-passport-tv/tv.db"))
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("users")
    commands.add_parser("devices")
    invite = commands.add_parser("invite")
    invite.add_argument("--name", default="")
    invite.add_argument("--admin", action="store_true", help="被邀请的人也是管理员")
    commands.add_parser("login-link").add_argument("user_id", type=int)
    adopt = commands.add_parser("adopt")
    adopt.add_argument("legacy_id")
    adopt.add_argument("device_id")
    for name in ("unbind", "repair", "ban-device", "unban-device"):
        commands.add_parser(name).add_argument("device_id")
    for name in ("ban-user", "unban-user"):
        commands.add_parser(name).add_argument("user_id", type=int)
    legacy = commands.add_parser("legacy-token")
    legacy.add_argument("state", choices=("on", "off"))
    audit = commands.add_parser("audit")
    audit.add_argument("--limit", type=int, default=30)
    args = parser.parse_args(argv)

    if not args.db.is_file():
        print(f"{args.db} 不存在：先运行 python3 -m server.tenancy.migrate", file=sys.stderr)
        return 2
    accounts = Accounts(args.db)
    admin = accounts.admin_id()
    try:
        if args.command == "users":
            for user in accounts.users():
                owned = [d["id"] for d in accounts.devices_of(user["id"])]
                print(f"{user['id']:>4}  {user['role']:<5} {user['status']:<6} {user['name']:<12} "
                      f"设备 {', '.join(owned) or '-'}")
        elif args.command == "devices":
            owners = {row["id"]: row["owner"] for row in accounts.active_devices()}
            for d in accounts.all_devices():
                port = f"旧端口 {d['legacy_port']}" if d["legacy_port"] is not None else ""
                print(f"{d['id']:<16} {d['status']:<9} {d['model'] or '?':<17} {d['name'] or '-':<12} "
                      f"主人 {owners.get(d['id']) or '-':<4} 固件 {d['fw'] or '-':<16} "
                      f"最后连接 {_when(d['last_seen'])} {port}")
        elif args.command == "invite":
            code = accounts.create_invite(admin, "admin" if args.admin else "user", args.name)
            site = accounts.meta("public_url", "<site>")
            print(f"{site}/tv/i/{code}")
            print("7 天内有效，只能用一次。用微信发给对方，在手机浏览器里打开。")
        elif args.command == "login-link":
            link = PhoneLinks(accounts).create_for_admin(args.user_id, admin)
            site = accounts.meta("public_url", "").rstrip("/") or "<site>"
            name = accounts.user(args.user_id)["name"]
            print(f"{site.upper()}/TV/P/{link['token']}")
            print(f"以「{name}」登录一台手机：{link['ttl'] // 60} 分钟内有效，只能用一次。在那台手机上打开"
                  "（相机扫码或粘贴到 Safari），点「在这台手机上登录」。已记入操作记录。")
        elif args.command == "adopt":
            accounts.adopt(args.legacy_id, args.device_id, admin)
            print(f"{args.legacy_id} 的主人、遥控码和投屏位已经搬到 {args.device_id}；{args.legacy_id} 已退役。")
        elif args.command == "unbind":
            accounts.unbind(args.device_id, admin)
            print(f"{args.device_id} 已解绑，屏幕上会显示新的绑定码。")
        elif args.command == "repair":
            accounts.start_repair(args.device_id, admin)
            print(f"10 分钟内 {args.device_id} 用新密钥连上来就会被记住。")
        elif args.command in ("ban-device", "unban-device"):
            if args.command == "ban-device":
                status = "banned"
            else:
                status = "active" if accounts.owner(args.device_id) is not None else "unclaimed"
            print("已更新" if accounts.set_device_status(args.device_id, status, admin) else "没有这台设备")
        elif args.command in ("ban-user", "unban-user"):
            status = "banned" if args.command == "ban-user" else "active"
            print("已更新" if accounts.set_user_status(args.user_id, status, admin) else "没有这个用户")
        elif args.command == "legacy-token":
            accounts.set_meta(LEGACY_TOKEN_KEY, "1" if args.state == "on" else "0")
            accounts.audit("legacy_token." + args.state, admin)
            print(f"共享配对令牌已{'打开' if args.state == 'on' else '关闭'}（电视服务下一次连接生效）。")
        elif args.command == "audit":
            for row in reversed(accounts.audit_rows(args.limit)):
                print(f"{_when(row['at'])}  {row['action']:<22} 用户 {row['actor_user'] or '-':<4} "
                      f"{row['target_kind']} {row['target_id']} {row['actor_ip_prefix']} {row['detail']}")
    except ValueError as error:
        print(error, file=sys.stderr)
        return 1
    finally:
        accounts.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
