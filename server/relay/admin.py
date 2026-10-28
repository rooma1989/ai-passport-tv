"""总后台（多用户阶段 3）：/api/admin/* 接口。页面是电脑版里管理员的「管理」（assets/console-admin.js，#/admin）；
旧地址 /tv/admin 由 console.py 转到那里。

设计见 docs/superpowers/specs/2026-10-12-multi-tenant-design.md 第 6 节。只在多用户（--db）时有；
web.py 的处理类在最前面转过来（get()/post() 返回 True 表示处理过了），和 console.py 一样。

谁能用：只有管理员（管理员的手机或电脑会话，或带密钥的请求）。每个接口都在服务端查角色：
不是管理员（家人、访客、没登录）一律 404，和不存在一样；电脑版也只给管理员显示「管理」。
POST 和电脑版一样只收本站页面发的（web.py 的 do_POST 先过 console.same_origin），
每个改东西的操作都写进操作记录（谁、什么时候、哪个网段、做了什么、对谁）。

页面：概览（设备在不在线、谁的、在播什么、流量；主机 CPU、内存、出站流量；YouTube 节点）、
用户（改名、封禁、删除前导出、一次性登录链接、他的登录）、设备（解绑、封禁、让它重连、重新配对）、
频道表（全局 channels.txt，见 channeltable.py）、操作记录、系统设置。
"""

from __future__ import annotations

import base64
import json
import os
import re
import threading
import time
from pathlib import Path

from .. import channel_groups, now_playing, routes
from ..tenancy import files as tenancy_files, phonelinks, signup
from ..tenancy.accounts import MODEL_NAMES, SESSION_S
from ..tenancy.gate import LEGACY_TOKEN_KEY
from ..tenancy.manage import AdminOps
from . import channeltable, console, epg, events, playlist, presets, qr, routecheck, subscriptions, table
from .ytnode import MAX_STREAMS

# 频道表一次发整张（没改的台只发 id）：比别的接口的 4 KB 大，但不超过 nginx 给 /tv/ 的 8 KB。
BODY_MAX = 8000
# 自建模式（没有 nginx，前面是 server/front.py，它给这个接口同样的上限）保存频道表时可以大得多：表最多
# channel_groups.TABLE_MAX 个台，一次从播放列表加几百个台，每个新台都带完整地址和备用线路，请求有几百 KB。
SELFHOST_CHANNELS_BODY_MAX = 2 * 1024 * 1024
# 自建模式的「导入播放列表」可以直接上传 M3U 文件的内容（playlist.py）；云端没有这个接口。
IMPORT_PATH = "/api/admin/channels/import"
_USER = re.compile(r"^/api/admin/users/(\d{1,9})(?:/(rename|ban|unban|reviewed|login-link|delete|export|"
                   r"sessions|sessions/revoke))?$")
_DEVICE = re.compile(r"^/api/admin/devices/([0-9a-f]{12}|legacy-[a-z0-9]{1,20})/"
                     r"(unbind|ban|unban|reconnect|repair|rename)$")
# 用户删除前的导出：数据库旁边的 exports/（和数据库一样只给服务账号和它的组读）。
EXPORT_DIR = "exports"
_channels_lock = threading.Lock()


def global_groups(relay) -> dict[str, str]:
    """总后台给全局直播台设的默认分组（manage.GLOBAL_GROUPS_KEY），读一次记在中转上。"""
    cached = getattr(relay, "_admin_groups", None)
    if cached is None:
        cached = relay._admin_groups = AdminOps(relay.accounts).global_groups() if relay.accounts else {}
    return cached


def _ops(relay) -> AdminOps:
    ops = getattr(relay, "_admin_ops", None)
    if ops is None:
        ops = relay._admin_ops = AdminOps(relay.accounts)
    return ops


def _admin(handler):
    """管理员的调用者；别人 None（调用方回 404）。"""
    caller = handler._caller()
    if caller is None or caller.role != "admin" or caller.user is None:
        return None
    return caller


def _read_body(handler, limit: int = BODY_MAX) -> dict | None:
    """读掉请求体（不管是谁都要读掉，keep-alive 上剩下的字节会粘到下一个请求前面）。"""
    try:
        length = int(handler.headers.get("Content-Length") or 0)
    except ValueError:
        handler.close_connection = True
        return None
    if length <= 0:
        return {}
    if length > limit:
        # 太大的不读：连接随后关掉，免得剩下的字节被当成下一个请求。
        handler.close_connection = True
        return None
    try:
        handler._read_body = True
        body = json.loads(handler.rfile.read(length))
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


# ---- GET ----

def get(handler, relay, path: str) -> bool:
    if not path.startswith("/api/admin/"):
        return False
    caller = _admin(handler)
    if caller is None:
        handler._not_found()
        return True
    query = handler._query()
    if path == "/api/admin/overview":
        handler._json(overview(relay, history=query.get("history") == "1"))
    elif path == "/api/admin/users":
        handler._json({"ok": True, "users": _ops(relay).users(), "invites": console.invites(relay), "me": caller.user})
    elif path == "/api/admin/devices":
        handler._json({"ok": True, "devices": devices(relay), "firmware": firmware_info()})
    elif path == "/api/admin/audit":
        _audit(handler, relay, query)
    elif path == "/api/admin/system":
        handler._json(system(relay))
    elif path == "/api/admin/channels":
        handler._json(channels(relay))
    elif path == "/api/admin/epg":
        handler._json(epg.admin_view(relay))
    elif path == "/api/admin/subs":
        subs = getattr(relay, "subscriptions", None)
        handler._json(subs.view() if subs is not None else {"ok": False, "error": "只有自建和电脑客户端能订阅直播源"})
    else:
        match = _USER.match(path)
        if match and match.group(2) == "sessions":
            handler._json({"ok": True, "sessions": _ops(relay).sessions(int(match.group(1)))})
        elif match and match.group(2) == "export":
            _export(handler, relay, int(match.group(1)), caller)
        else:
            handler._not_found()
    return True


def _audit(handler, relay, query: dict) -> None:
    def number(key):
        value = query.get(key) or ""
        return int(value) if value.isdigit() else None
    device = query.get("device") or None
    if device is not None and not tenancy_files.DEVICE_ID.match(device):
        device = "-"
    rows = _ops(relay).audit(user=number("user"), device=device, action=(query.get("action") or "")[:40] or None,
                             before=number("before"), limit=number("limit") or 50)
    handler._json({"ok": True, "rows": rows, "actions": _ops(relay).audit_actions(),
                   "next": rows[-1]["id"] if rows else None})


def _export(handler, relay, user_id: int, caller) -> None:
    data = _ops(relay).export(user_id)
    if data is None:
        handler._not_found()
        return
    relay.accounts.audit("user.export", caller.user, handler._ip(), "user", str(user_id))
    body = json.dumps(data, ensure_ascii=False, indent=1).encode("utf-8")
    handler._send(200, body, "application/json; charset=utf-8",
                  {"Content-Disposition": f'attachment; filename="user-{user_id}.json"'})


# ---- 概览 ----

def _device_rows(relay) -> list[dict]:
    """所有没退役的设备：状态、主人、在线、在播什么、这次会话多久、流量。"""
    accounts = relay.accounts
    now = relay.clock()
    owners = {row["device_id"]: row for row in accounts._all(
        "SELECT m.device_id, u.id, u.name, u.status FROM device_member m JOIN user u ON u.id=m.user_id "
        "WHERE m.role='owner'")}
    guests = {row["device_id"]: row["n"] for row in accounts._all(
        "SELECT device_id, COUNT(*) AS n FROM session WHERE kind='guest' AND expires_at > ? GROUP BY device_id",
        (now,))}
    live = {dev["id"]: dev for dev in relay.devices}
    mismatches = accounts.secret_mismatches()
    rows = []
    for row in accounts.all_devices():
        files = tenancy_files.device_files(relay.devices_dir, row["id"])
        playing = now_playing.read(files.now, now)
        owner = owners.get(row["id"])
        item = {"id": row["id"], "name": row["name"] or row["id"], "model": row["model"],
                "model_name": MODEL_NAMES.get(row["model"], row["model"] or "?"), "fw": row["fw"],
                "status": row["status"], "legacy": row["legacy_port"] is not None, "last_seen": row["last_seen"],
                "network": row["last_ip_prefix"], "created_at": row["created_at"],
                "owner": owner["id"] if owner else None, "owner_name": owner["name"] if owner else "",
                "owner_banned": bool(owner) and owner["status"] == "banned",
                "guests": guests.get(row["id"], 0), "online": playing["online"],
                "repair": bool(row["repair_until"] and row["repair_until"] > now)}
        # 被拒在「密钥对不上」之后再没正常连上过：多半是刷机清空了设置区，要主人点「重新配对」。
        refused_at = mismatches.get(row["id"])
        item["key_mismatch"] = bool(refused_at and refused_at > (row["last_seen"] or 0) and not item["repair"]
                                    and not playing["online"] and row["status"] in ("active", "unclaimed")
                                    and row["legacy_port"] is None)
        if playing["online"]:
            item.update(channel=playing["channel"], since=playing["since"],
                        session_s=max(0, int(now - playing["since"])) if playing["since"] else 0,
                        audio_only=bool((playing["device"] or {}).get("audio_only")))
            metrics = now_playing.read_metrics(files.now, now) or {}
            item.update(bytes_10s=metrics.get("bytes_10s"), fps=metrics.get("fps"),
                        target_rate=metrics.get("target_rate"))
            dev = live.get(row["id"])
            if dev is not None:
                try:
                    shown = relay.now(relay.groups(dev), dev)
                    item.update(channel_name=shown["name"], title=shown["title"])
                except Exception as error:      # noqa: BLE001 —— 概览少一个台名不要紧
                    relay.log(f"总后台：{row['id']} 正在播放读不出来：{type(error).__name__}")
        rows.append(item)
    return rows


def devices(relay) -> list[dict]:
    """设备页：在用的、未绑定的、封禁的在前，退役的旧记录放最后。"""
    return sorted(_device_rows(relay), key=lambda row: row["status"] == "retired")


# 固件版本：设备在 HELLO 里报 fw（gate.py 记进 device.fw）。家庭服务器带着网页刷机页和固件时
# （自托管包，设计文档 9.5），TV_FIRMWARE_MANIFEST 指向那套固件的 manifest.json，设备页拿每台
# 设备的 fw 和这次发布的版本比；TV_FLASHER_URL 是刷机页的地址（自托管包里是 /flash/）。
# 都没设（云主机）就只显示设备报的版本和怎么更新的说明。
FIRMWARE_MANIFEST_ENV = "TV_FIRMWARE_MANIFEST"
FLASHER_URL_ENV = "TV_FLASHER_URL"
_FLASHER_URL = re.compile(r"^(/(?!/)[A-Za-z0-9._~/-]*|https?://[A-Za-z0-9.:\[\]-]+(/[A-Za-z0-9._~/-]*)?)$")


def firmware_info(environ=None) -> dict:
    environ = os.environ if environ is None else environ
    url = environ.get(FLASHER_URL_ENV, "").strip()
    info = {"latest": {}, "version": "", "flasher": url if _FLASHER_URL.match(url) else ""}
    path = environ.get(FIRMWARE_MANIFEST_ENV, "").strip()
    if not path:
        return info
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return info
    if isinstance(data, dict) and isinstance(data.get("version"), str):
        info["version"] = data["version"][:32]
    for build in data.get("builds", []) if isinstance(data, dict) else []:
        if (isinstance(build, dict) and isinstance(build.get("board"), str)
                and isinstance(build.get("version"), str)):
            info["latest"][build["board"][:32]] = build["version"][:32]
    return info


def overview(relay, history: bool = False) -> dict:
    rows = [row for row in _device_rows(relay) if row["status"] != "retired"]
    stats = getattr(relay, "host_stats", None)
    host = {"available": bool(stats and stats.available)}
    if host["available"]:
        latest = stats.latest()
        if latest is not None:
            host.update(cpu=latest[1], mem_used=latest[2], mem_total=latest[3], rx=latest[4], tx=latest[5])
        host["processes"] = stats.processes()
        if history:
            # 曲线：分钟点（时刻, CPU %, 已用内存 MB, 发 字节/秒），一天最多 1440 个。
            host["history"] = [[int(p[0]), p[1], p[2], p[5]] for p in stats.history()]
    youtube = {"configured": relay.clients.get("youtube") is not None, "max_streams": MAX_STREAMS}
    if youtube["configured"]:
        youtube["state"] = relay.youtube_state()
        with relay._active_lock:
            youtube["streams"] = sum(1 for entry in relay._active.values()
                                     if getattr(entry["video"], "site", "") == "youtube")
    users = _ops(relay).users()
    return {"ok": True, "now": relay.clock(), "devices": rows, "host": host, "youtube": youtube,
            "counts": {"online": sum(r["online"] for r in rows), "devices": len(rows),
                       "users": len(users), "banned": sum(u["status"] == "banned" for u in users),
                       "review": sum(u["origin"] == "signup" and not u["reviewed_at"] for u in users)}}


# ---- 系统 ----

def system(relay) -> dict:
    accounts = relay.accounts
    path = accounts.path
    size = sum(p.stat().st_size for p in (path, Path(str(path) + "-wal")) if p.is_file())
    counts = accounts._one("SELECT (SELECT COUNT(*) FROM user) AS users, (SELECT COUNT(*) FROM device) AS devices, "
                           "(SELECT COUNT(*) FROM session WHERE expires_at > ?) AS sessions, "
                           "(SELECT COUNT(*) FROM audit_log) AS audit", (relay.clock(),))
    main = relay.main_channels
    backups = sorted(p.name for p in main.parent.glob(main.name + ".bak-*")) if main is not None else []
    stats = getattr(relay, "host_stats", None)
    return {"ok": True, "legacy_token": accounts.meta(LEGACY_TOKEN_KEY, "1") != "0",
            "selfhost": relay.selfhost is not None,
            "public_url": relay.public_url, "signup_limits": {"net": signup.NET_LIMIT[0], "all": signup.ALL_LIMIT[0]},
            "recover_limit": phonelinks.DEVICE_LIMIT[0], "sessions_days": {k: v // 86400 for k, v in SESSION_S.items()},
            "db": {"name": path.name, "bytes": size, **counts},
            "channels": {"file": str(main) if main else "", "backups": backups[-5:], "backup_count": len(backups)},
            "sampler": bool(stats and stats.available), "youtube": relay.clients.get("youtube") is not None,
            **_versions(relay)}


def _versions(relay) -> dict:
    """「系统」里的版本和电脑上看（只显示）：服务端的版本号、这次发布的固件版本、在电脑上看开没开、换台预热几路
    （自建 / 电脑客户端的 config.toml [pc] warm_channels；云端没有在电脑上看）。"""
    from ..selfhost.version import VERSION
    watch = getattr(relay, "watch", None)
    return {"version": VERSION, "firmware_version": firmware_info()["version"],
            "pc_watch": watch is not None, "pc_warm": int(getattr(watch, "warm_max", 0) or 0) if watch is not None else 0}


# ---- 频道表 ----

def _owners_rows(relay) -> list[tuple[str, list[tuple[str, str]]]]:
    """每个有在用设备的用户：(名字, 他自己的台：投屏台、UP 主、剧集)。设备收到的列表是这些再接上全局表。"""
    owners = {dev["owner"] for dev in relay.devices if dev["owner"] is not None}
    result = []
    for owner in sorted(owners):
        user = relay.accounts.user(owner)
        state = relay.user_state(owner)
        with state.lock:
            own = [(table.CAST_ID, table.CAST_LABEL)] + [(c.id, table.label(c)) for c in state.creators]
        result.append((user["name"] if user else str(owner), own))
    return result


def _fit(relay, rows: list[tuple[str, str]]) -> list[dict]:
    """每个用户的设备频道列表：整份放不放得下（fits，和加 UP 主时一样留 CONFIG_SPARE_BYTES 余量），放不下时
    小电视上显示几个台（shown，自己的台在前）、其中全局表的前几个（table_shown）。电视服务按同样的算法截断
    （channel_groups.device_list）；电脑上看不受这个限制。"""
    found = []
    for name, own in _owners_rows(relay) or [("", [(table.CAST_ID, table.CAST_LABEL)])]:
        listed = own + rows
        used = channel_groups.config_bytes(listed) + channel_groups.CONFIG_SPARE_BYTES
        shown = len(channel_groups.device_list(listed))
        found.append({"user": name, "channels": len(listed), "bytes": used,
                      "fits": shown == len(listed), "shown": shown,
                      "table_shown": max(0, shown - len(own))})
    return found


def _fit_warnings(fit: list[dict], total: int) -> list[str]:
    """放不下整张表的设备：小电视上只显示表里的前几个台（不是错误，电脑上照样全能看）。"""
    found = []
    for item in fit:
        if not item["fits"]:
            who = f"「{item['user']}」的" if item["user"] else ""
            found.append(f"{who}小电视上只显示前 {item['table_shown']} 个台（按表里的顺序，常看的往前挪）；"
                         f"表里一共 {total} 个，电脑上都能看")
    return found


def channels(relay) -> dict:
    path = relay.main_channels
    if path is None:
        return {"ok": True, "configured": False}
    current = channeltable.read(path)
    groups = global_groups(relay)
    lines = channeltable.routes_view(path, current["rows"])
    rows = [dict(row, group=groups.get(row["id"], ""), auto=channel_groups.classify(row["id"], row["name"]),
                 **lines[row["id"]])
            for row in current["rows"]]
    backups = sorted(p.name for p in path.parent.glob(path.name + ".bak-*"))
    return {"ok": True, "configured": True, "version": current["version"], "rows": rows, "bad": current["bad"],
            "groups": list(channel_groups.GROUPS[2:]), "limits": {"channels": channel_groups.CHANNEL_MAX,
                                                                  "bytes": channel_groups.CONFIG_MAX,
                                                                  "table": channel_groups.TABLE_MAX},
            "fit": _fit(relay, [(r["id"], r["name"]) for r in current["rows"]]), "backups": backups[-5:],
            "import": relay.selfhost is not None, "routes_max": routes.ROUTES_MAX,
            "subs_every_h": int(subscriptions.EVERY_S // 3600),
            # 后台在自动检测线路（linecheck.py，自建 / 电脑客户端）：上次测的时刻、全部线路不通的台。
            "auto": relay.linecheck.status() if getattr(relay, "linecheck", None) is not None else None,
            # 网上公开的第三方播放列表（只有列表地址）：和导入一样只在自建 / 电脑客户端里有。
            "presets": presets.public() if relay.selfhost is not None else []}


def save_channels(relay, body: dict, caller, ip: str = "") -> dict:
    """检查（dry）或保存频道表。body: {version, rows: [id 或 {id?, name, url, agent, group}], dry}。"""
    path = relay.main_channels
    if path is None:
        return {"ok": False, "error": "中转没有配置 --main-channels，没有全局频道表"}
    with _channels_lock:
        current = channeltable.read(path)
        if body.get("version") != current["version"]:
            return {"ok": False, "stale": True, "error": "频道表在你编辑的时候被改过了（别处保存或手动改了文件）：请刷新后重新改"}
        if current["bad"]:
            return {"ok": False, "error": f"文件里有看不懂的行（第 {', '.join(map(str, current['bad']))} 行），"
                                         "为了不把它们弄丢，请先在服务器上改好"}
        try:
            rows = channeltable.apply_edits(current["rows"], body.get("rows"))
        except ValueError as error:
            return {"ok": False, "error": str(error)}
        old_groups = global_groups(relay)
        groups = {}
        for item, row in zip(body["rows"], rows):
            group = item.get("group") if isinstance(item, dict) and "group" in item else old_groups.get(row["id"], "")
            if group and group not in channel_groups.GROUPS[2:]:
                return {"ok": False, "error": f"「{row['name']}」：没有「{group}」这个分组"}
            if group and group != channel_groups.classify(row["id"], row["name"]):
                groups[row["id"]] = group
        problems = channeltable.problems(rows)
        route_problems, routes_changed = _route_problems(body["rows"], rows)
        problems += route_problems
        # 设备收不下整张表不再挡保存：每台设备只收前一部分（电视服务截断），这里只提醒。
        fit = _fit(relay, [(r["id"], r["name"]) for r in rows])
        if problems:
            return {"ok": False, "error": problems[0], "problems": problems[:20], "fit": fit}
        text = channeltable.render(rows)
        checked = channeltable.media_check(text)
        if not checked.get("ok"):
            return {"ok": False, "error": checked.get("error") or "电视服务的检查没通过", "fit": fit}
        before = {row["id"]: row for row in current["rows"]}
        added = [r for r in rows if r["id"] not in before]
        removed = [r for r in current["rows"] if r["id"] not in {x["id"] for x in rows}]
        changed = [r for r in rows if r["id"] in before and (r["name"], r["url"], r["agent"]) !=
                   (before[r["id"]]["name"], before[r["id"]]["url"], before[r["id"]]["agent"])]
        moved = [r["id"] for r in rows if r["id"] in before] != [r["id"] for r in current["rows"]
                                                                  if r["id"] in {x["id"] for x in rows}]
        kept = {r["id"] for r in rows}
        summary = {"added": len(added), "removed": len(removed), "changed": len(changed), "moved": moved,
                   "groups": groups != {k: v for k, v in old_groups.items() if k in kept}, "total": len(rows),
                   "routes": routes_changed}
        warnings = _fit_warnings(fit, len(rows)) + (checked.get("warnings") or [])
        if body.get("dry") is True:
            return {"ok": True, "dry": True, "summary": summary, "warnings": warnings, "fit": fit}
        backup = channeltable.save(path, text, relay.clock)
        _save_meta(path, body["rows"], rows)
        _ops(relay).set_global_groups(groups)
        relay._admin_groups = groups
    detail = (f"{len(current['rows'])}→{len(rows)} 个台，+{len(added)} −{len(removed)} 改{len(changed)}"
              f"{' 换序' if moved else ''}；备份 {backup or '无'}")
    relay.accounts.audit("channels.save", caller.user, ip, "channels", path.name, detail)
    relay.log(f"总后台保存了频道表：{detail}")
    # 分组变了：每台设备的分组文件要重写（电视服务下次发频道列表时读）。
    relay.sync_devices(force=True)
    return {"ok": True, "version": channeltable.version_of(text), "backup": backup, "summary": summary,
            "warnings": warnings, "fit": fit}


def _save_meta(path: Path, items: list, rows: list[dict]) -> None:
    """频道表旁边的 meta（tvg-id、台标、备用线路，channeltable.merge_meta）。写不了不要紧（节目单退回按台名对、
    只有主线路），不挡保存。"""
    routes.update_meta(path, lambda data: channeltable.merge_meta(data, items, rows))


def _route_problems(items: list, rows: list[dict]) -> tuple[list[str], int]:
    """页面发来的备用线路的问题（中文），和改了备用线路的台数。"""
    found, changed = [], 0
    for n, (item, row) in enumerate(zip(items, rows), start=1):
        if not isinstance(item, dict) or "routes" not in item:
            continue
        given = item["routes"]
        label = f"第 {n} 行（{row['name'] or row['id']}）"
        if not isinstance(given, list):
            found.append(f"{label}：备用线路的格式不对")
            continue
        if len(given) > routes.BACKUPS_MAX:
            found.append(f"{label}：备用线路最多 {routes.BACKUPS_MAX} 条（连主线路一共 {routes.ROUTES_MAX} 条）")
        for k in routes.bad_routes(given):
            found.append(f"{label}：备用线路 {k} 的地址不对（要以 http:// 或 https:// 开头，不能有空格和竖线）")
        changed += 1
    return found, changed


# ---- 看一眼一条线路 ----

_PEEKS = threading.BoundedSemaphore(2)


def peek(relay, body: dict) -> dict:
    """{id, url} → {ok, jpeg: base64} 或 {ok: False, error}：url 要是这个台在频道表里已保存的线路之一。"""
    path = relay.main_channels
    cid, url = body.get("id"), body.get("url")
    if path is None or not isinstance(cid, str) or not isinstance(url, str):
        return {"ok": False, "error": "请求格式不对"}
    row = next((r for r in channeltable.read(path)["rows"] if r["id"] == cid), None)
    if row is None:
        return {"ok": False, "error": "没有这个台"}
    lines = routes.table_routes(path, cid, row["url"], row["agent"], playlist.DEFAULT_USER_AGENT)
    agent = next((a for u, a in lines if u == url), None)
    if agent is None:
        return {"ok": False, "error": "这条线路还没保存：先点「保存」"}
    if not _PEEKS.acquire(blocking=False):
        return {"ok": False, "error": "正在看别的线路，稍等"}
    try:
        from .linecheck import grab_frame
        frame = grab_frame(relay.ffmpeg, url, agent, lenient=routes.is_lenient(path, url))
    finally:
        _PEEKS.release()
    if frame is None:
        return {"ok": False, "error": "这条线路现在取不到画面"}
    return {"ok": True, "jpeg": base64.b64encode(frame).decode("ascii")}


# ---- POST ----

def post(handler, relay, path: str) -> bool:
    if not path.startswith("/api/admin/"):
        return False
    selfhost = relay.selfhost is not None
    limit = BODY_MAX
    if selfhost and path == IMPORT_PATH:
        limit = playlist.IMPORT_BODY_MAX
    elif selfhost and path == "/api/admin/channels":
        limit = SELFHOST_CHANNELS_BODY_MAX
    body = _read_body(handler, limit)
    caller = _admin(handler)
    if caller is None:
        handler._not_found()
        return True
    if body is None:
        handler._json({"ok": False, "error": "请求格式不对（或太大）"}, 400)
        return True
    ip = handler._ip()
    if path == "/api/admin/channels":
        handler._json(save_channels(relay, body, caller, ip))
        return True
    if path == "/api/admin/channels/check":
        # 检测一个台的线路（routecheck.py：取列表和一个片段的头几 KB，不开 ffmpeg）：云端和自建都有，
        # 只测频道表里已经保存的线路。
        cid = body.get("id")
        if relay.main_channels is None or not isinstance(cid, str) or not cid or len(cid) > 32:
            handler._json({"ok": False, "error": "请求格式不对"}, 400)
            return True
        handler._json(routecheck.check_channel(relay.main_channels, cid, log=relay.log))
        return True
    if path == "/api/admin/epg":
        # 节目单的来源（epg.py）：云端和自建都有，默认关着。
        handler._json(epg.admin_save(relay, body, caller, ip))
        return True
    if path == "/api/admin/epg/refresh":
        handler._json(epg.admin_refresh(relay, caller, ip))
        return True
    if selfhost and path == IMPORT_PATH:
        # 自建：读一份 M3U（地址、上传的内容或一个公开列表），列出里面的台给页面勾选；不存地址、不改频道表。
        answer = playlist.read(body)
        if answer.get("ok"):
            current = channeltable.read(relay.main_channels)["rows"] if relay.main_channels is not None else []
            playlist.mark(answer["entries"], current)
            preset = body.get("preset") if isinstance(body.get("preset"), str) else ""
            relay.accounts.audit("channels.import", caller.user, ip, "channels", preset[:40],
                                 f"{len(answer['entries'])} 个台")
        handler._json(answer)
        return True
    if path in ("/api/admin/subs", "/api/admin/subs/refresh", "/api/admin/subs/delete"):
        # 直播源订阅（subscriptions.py，自建 / 电脑客户端）：加一个（马上取一次）、现在更新、删掉。
        subs = getattr(relay, "subscriptions", None)
        if subs is None:
            handler._json({"ok": False, "error": "只有自建和电脑客户端能订阅直播源"})
            return True
        sid = body.get("id") if isinstance(body.get("id"), str) else ""
        if path == "/api/admin/subs":
            url = body.get("url") if isinstance(body.get("url"), str) else ""
            name = body.get("name") if isinstance(body.get("name"), str) else ""
            if "preset" in body:
                # 页面上列的公开列表（presets.py）：地址在服务器上，页面只给是哪个、第几个镜像。
                url = presets.url_of(body.get("preset"), body.get("mirror", 0)) or ""
                name = next((p["name"] for p in presets.PRESETS if p["id"] == body.get("preset")), "")
                if not url:
                    handler._json({"ok": False, "error": "没有这个播放列表（页面可能是旧的，请刷新）"})
                    return True
            answer = subs.add(url, name)
            action = "channels.subscribe"
        elif path == "/api/admin/subs/refresh":
            answer, action = subs.refresh(sid), "channels.subscription_refresh"
        else:
            answer, action = subs.remove(sid), "channels.unsubscribe"
        if answer.get("ok"):
            relay.accounts.audit(action, caller.user, ip, "channels", sid[:8], "")
        handler._json(answer)
        return True
    if selfhost and path == "/api/admin/channels/peek":
        # 自建：看一眼频道表里某个台的某条线路现在放的是什么（取一帧 JPEG）。线路质量的检查
        # （linequality.py）标出画面可疑的线路以后，管理员看一眼再决定删不删。只认表里已经保存的线路。
        handler._json(peek(relay, body))
        return True
    if selfhost and path == "/api/admin/channels/probe":
        # 自建：试一个地址能不能出画面（用 ffmpeg 取一帧）。
        handler._json(playlist.probe(body, relay.ffmpeg))
        return True
    if path == "/api/admin/invites":
        name = body.get("name") if isinstance(body.get("name"), str) else ""
        role = "admin" if body.get("admin") is True else "user"
        code = relay.accounts.create_invite(caller.user, role, name, ip=ip)
        link = f"{console._base(relay, handler)}/tv/i/{code}"
        handler._json({"ok": True, "link": link, "svg": qr.svg(link), "role": role})
        return True
    if path == "/api/admin/system/legacy-token":
        if not isinstance(body.get("on"), bool):
            handler._json({"ok": False, "error": "请求格式不对"}, 400)
            return True
        state = "on" if body["on"] else "off"
        relay.accounts.set_meta(LEGACY_TOKEN_KEY, "1" if body["on"] else "0")
        relay.accounts.audit("legacy_token." + state, caller.user, ip)
        handler._json({"ok": True})
        return True
    match = _USER.match(path)
    if match and match.group(2) not in (None, "sessions", "export"):
        handler._json(_user_action(handler, relay, int(match.group(1)), match.group(2), body, caller, ip))
        return True
    match = _DEVICE.match(path)
    if match:
        handler._json(_device_action(relay, match.group(1), match.group(2), body, caller, ip))
        return True
    handler._not_found()
    return True


def _last_admin(relay, user_id: int) -> bool:
    row = relay.accounts._one("SELECT COUNT(*) AS n FROM user WHERE role='admin' AND status='active' AND id!=?",
                              (user_id,))
    target = relay.accounts.user(user_id)
    return target is not None and target["role"] == "admin" and row["n"] == 0


def _kick(relay, device_id: str) -> None:
    """让设备马上重连（电视服务结束这次会话）：封禁、解绑之后它重新认一次，就被拒绝或看到绑定画面。"""
    try:
        files = tenancy_files.ensure_dir(relay.devices_dir, device_id)
        events.write_event(files.events, "reconnect")
    except (OSError, ValueError) as error:
        relay.log(f"{device_id} 写重连事件失败：{error}")


def _forget_device(relay, device_id: str) -> None:
    """中转内存里这台设备的东西：投屏位、手机选定、正在推的流。"""
    with relay._cache_lock:
        relay._casts.pop(device_id, None)
    relay._targets = {k: v for k, v in relay._targets.items() if v != device_id}
    relay._synced.pop(device_id, None)
    with relay._active_lock:
        cut = [(key, entry) for key, entry in relay._active.items() if key[0] == device_id]
    for key, entry in cut:
        relay._cut(key[1], entry, "设备解绑")


def _owned_devices(relay, user_id: int) -> list[str]:
    return [row["device_id"] for row in relay.accounts._all(
        "SELECT device_id FROM device_member WHERE user_id=? AND role='owner'", (user_id,))]


def _user_action(handler, relay, user_id: int, action: str, body: dict, caller, ip: str) -> dict:
    accounts, ops = relay.accounts, _ops(relay)
    user = accounts.user(user_id)
    if user is None:
        return {"ok": False, "error": "没有这个用户"}
    if action == "rename":
        name = body.get("name")
        if not isinstance(name, str):
            return {"ok": False, "error": "请求格式不对"}
        try:
            name = ops.rename_user(user_id, name, caller.user, ip)
        except ValueError as error:
            return {"ok": False, "error": str(error)}
        relay.log(f"总后台：用户 {user_id} 改名为 {name}")
        return {"ok": True, "name": name}
    if action in ("ban", "unban"):
        if action == "ban" and user_id == caller.user:
            return {"ok": False, "error": "不能封禁自己"}
        if action == "ban" and _last_admin(relay, user_id):
            return {"ok": False, "error": "这是最后一个管理员，不能封禁"}
        accounts.set_user_status(user_id, "banned" if action == "ban" else "active", caller.user, ip)
        if action == "ban":
            # 会话已经删了；他的设备马上断开，重连时被电视服务拒绝（gate.py 查主人的状态）。
            for device_id in _owned_devices(relay, user_id):
                _kick(relay, device_id)
        relay.log(f"总后台：用户 {user_id} {'封禁' if action == 'ban' else '解封'}")
        return {"ok": True}
    if action == "reviewed":
        ops.mark_reviewed(user_id, caller.user, ip)
        return {"ok": True}
    if action == "login-link":
        # 和命令行 `python3 -m server.tenancy.admin login-link <id>` 同一条路（PhoneLinks.create_for_admin）。
        try:
            made = relay.phone_links.create_for_admin(user_id, caller.user, ip)
        except ValueError as error:
            return {"ok": False, "error": str(error)}
        link = f"{console._base(relay, handler).upper()}/TV/P/{made['token']}"
        relay.log(f"总后台：给用户 {user_id} 生成了一次性登录链接")
        return {"ok": True, "link": link, "svg": qr.svg(link), "ttl": made["ttl"], "name": user["name"]}
    if action == "sessions/revoke":
        if body.get("all") is True:
            count = ops.revoke_all(user_id, caller.user, ip)
            return {"ok": True, "count": count}
        session_id = body.get("id")
        if not isinstance(session_id, str) or not ops.revoke_session(user_id, session_id, caller.user, ip):
            return {"ok": False, "error": "没有这个登录"}
        return {"ok": True}
    if action == "delete":
        return _delete_user(relay, user, body, caller, ip)
    return {"ok": False, "error": "没有这个操作"}


def _delete_user(relay, user: dict, body: dict, caller, ip: str) -> dict:
    """先导出（存一份在服务器上，也交给页面下载），再删。要输入用户的名字确认。"""
    if body.get("confirm") != user["name"]:
        return {"ok": False, "error": "请输入这个用户的名字确认删除"}
    # 删不了的先挡住（delete_user 里还会再查一遍）：免得白导出一份、白掐断他正在看的流。
    if user["id"] == caller.user:
        return {"ok": False, "error": "不能删除自己"}
    if _last_admin(relay, user["id"]):
        return {"ok": False, "error": "这是最后一个管理员，不能删除"}
    if relay.accounts._one("SELECT 1 AS x FROM device d JOIN device_member m ON m.device_id=d.id WHERE m.user_id=? "
                           "AND m.role='owner' AND d.legacy_port IS NOT NULL AND d.status!='retired'", (user["id"],)):
        return {"ok": False, "error": "这个用户有旧固件的设备（不能解绑）：先给设备刷新固件、接收到真 id，再删除"}
    ops = _ops(relay)
    data = ops.export(user["id"])
    folder = Path(relay.accounts.path).parent / EXPORT_DIR
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(relay.clock()))
    target = folder / f"user-{user['id']}-{stamp}.json"
    try:
        folder.mkdir(mode=0o750, exist_ok=True)
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.chmod(tmp, 0o640)
        os.replace(tmp, target)
    except OSError as error:
        return {"ok": False, "error": f"导出没能保存（{error.strerror or error}），没有删除"}
    # 中转正在给他的设备推的流先掐掉：流结束时会存进度，存到一个删掉的用户上就出错了。
    for device_id in _owned_devices(relay, user["id"]):
        _forget_device(relay, device_id)
    try:
        unbound = ops.delete_user(user["id"], caller.user, ip)
    except ValueError as error:
        return {"ok": False, "error": str(error)}
    with relay._cache_lock:
        relay._users.pop(user["id"], None)
    if getattr(relay, "bili_accounts", None) is not None:
        relay.bili_accounts.forget(user["id"])      # 他的 B 站登录（bililogin.py）也删掉
    if getattr(relay, "account_feeds", None) is not None:
        relay.account_feeds.forget(user["id"])     # 他的「我的关注」和首页几排的设置（accountfeeds.py）
    for device_id in unbound:
        _kick(relay, device_id)
    relay.sync_devices()
    relay.log(f"总后台：删除了用户 {user['id']}（导出在 {EXPORT_DIR}/{target.name}），解绑了 {len(unbound)} 台设备")
    return {"ok": True, "export": data, "file": f"{EXPORT_DIR}/{target.name}", "unbound": unbound}


def _device_action(relay, device_id: str, action: str, body: dict, caller, ip: str) -> dict:
    accounts = relay.accounts
    row = accounts.device(device_id)
    if row is None:
        return {"ok": False, "error": "没有这台设备"}
    if row["status"] == "retired":
        return {"ok": False, "error": "这台设备已经退役（旧记录，已经搬到新 id 上了）"}
    if action == "rename":
        name = body.get("name")
        if not isinstance(name, str):
            return {"ok": False, "error": "请求格式不对"}
        try:
            name = accounts.rename(device_id, name, caller.user, ip)
        except ValueError as error:
            return {"ok": False, "error": str(error)}
        relay.sync_devices()
        return {"ok": True, "name": name}
    if action == "unbind":
        try:
            accounts.unbind(device_id, caller.user, ip)
        except ValueError as error:
            return {"ok": False, "error": str(error)}
        _forget_device(relay, device_id)
        _kick(relay, device_id)
        relay.log(f"总后台：{device_id} 解绑")
        return {"ok": True}
    if action == "ban":
        if row["status"] == "banned":
            return {"ok": True}
        accounts.set_device_status(device_id, "banned", caller.user, ip)
        _forget_device(relay, device_id)
        _kick(relay, device_id)
        relay.log(f"总后台：{device_id} 封禁")
        return {"ok": True}
    if action == "unban":
        if row["status"] != "banned":
            return {"ok": True}
        status = "active" if accounts.owner(device_id) is not None else "unclaimed"
        accounts.set_device_status(device_id, status, caller.user, ip)
        relay.sync_devices()
        return {"ok": True, "status": status}
    if action == "reconnect":
        if row["status"] != "active":
            return {"ok": False, "error": "只有在用的设备能让它重连"}
        _kick(relay, device_id)
        accounts.audit("device.reconnect", caller.user, ip, "device", device_id)
        return {"ok": True, "note": "设备会在一两秒内断开再连上（旧固件不认这个命令）"}
    if action == "repair":
        if row["legacy_port"] is not None:
            return {"ok": False, "error": "旧固件的设备没有自己的密钥，不用重新配对"}
        accounts.start_repair(device_id, caller.user, ip)
        return {"ok": True, "note": "10 分钟内让小电视重新连上网络，它的新密钥会被记住"}
    return {"ok": False, "error": "没有这个操作"}

