"""「管理 → 系统 → 备份和恢复」的接口（家庭服务器和电脑客户端，中转要有 --selfhost-data）。打包、检查、替换都在
server/selfhost/backup.py，这里只管 HTTP、谁能用和操作记录。

    GET  /api/admin/backup                   能不能用、有没有监督进程、每天的自动备份、等着替换的、上次恢复的结果
    POST /api/admin/backup/export            {"logins": bool}：回答就是 zip（Content-Disposition 下载）
    POST /api/admin/backup/restore/upload    请求体是 zip 本身（不是 JSON）：检查，回答会替换哪些（summary）
    POST /api/admin/backup/restore           {"id"}：刚检查过的那份解到 run/restore/；有监督进程就由它停掉子进程、
                                             替换、再起来（restart: "auto"），没有就等下次启动（restart: "manual"）
    POST /api/admin/backup/restore/cancel    不恢复了：删掉上传的和解好的

只给管理员（别人一律 404，和别的 /api/admin/* 一样）；POST 只收本站页面发的（web.py 先过 console.same_origin）。
导出和恢复都进操作记录（action backup.export / backup.restore；替换完 backup.py 在恢复好的数据库里再记一条
backup.restored）。上传的文件只放在数据目录的 run/ 里，一次一份；文件内容、密码、cookies 从不进日志。
"""

from __future__ import annotations

import json
import os
import secrets
import threading
import time
from pathlib import Path

from ..selfhost import backup

PREFIX = "/api/admin/backup"
UPLOAD_PATH = PREFIX + "/restore/upload"
OFF = "只有家庭服务器和电脑客户端能导出、恢复设置"
# 监督进程每 5 秒写一次 run/status.json（supervisor.py）；太久没更新就当它不在（和 server/front.py 一样）。
STATUS_STALE_S = 30
_export_lock = threading.Lock()
_upload_lock = threading.Lock()


def root_of(relay) -> Path | None:
    data = getattr(relay, "selfhost_data", None)
    return Path(data) if relay.selfhost is not None and data else None


def supervised(root: Path, clock=time.time) -> bool:
    """有监督进程在跑（它会停掉子进程、替换、再起来）。"""
    try:
        status = json.loads((root / "run" / "status.json").read_text(encoding="utf-8"))
        return abs(clock() - float(status.get("at") or 0)) < STATUS_STALE_S
    except (OSError, ValueError, TypeError, AttributeError):
        return False


def _upload_info(root: Path) -> dict | None:
    try:
        info = json.loads((backup.run_dir(root) / backup.UPLOAD_INFO).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return info if isinstance(info, dict) and (backup.run_dir(root) / backup.UPLOAD).is_file() else None


def _drop_upload(root: Path) -> None:
    for name in (backup.UPLOAD, backup.UPLOAD_INFO):
        try:
            (backup.run_dir(root) / name).unlink()
        except OSError:
            pass


def view(relay) -> dict:
    root = root_of(relay)
    if root is None:
        return {"ok": False, "available": False, "error": OFF}
    staged = None
    if backup.pending(root):
        try:
            ready = json.loads((backup.stage_dir(root) / backup.READY).read_text(encoding="utf-8"))
            staged = {"created_at": ready["manifest"]["created_at"], "logins": bool(ready["manifest"].get("logins"))}
        except (OSError, ValueError, KeyError, TypeError):
            staged = {}
    upload = _upload_info(root)
    return {"ok": True, "available": True, "supervised": supervised(root, relay.clock),
            "daily": backup.daily_list(root)[:10], "keep_days": backup.DAILY_KEEP_DAYS,
            "pending": staged, "upload": upload.get("summary") if upload else None,
            "upload_id": upload.get("id") if upload else None, "last": backup.last_result(root),
            "limit_mb": backup.UPLOAD_MAX // 1048576}


def get(handler, relay, path: str, caller) -> bool:
    if path != PREFIX:
        return False
    handler._json(view(relay), headers={"Cache-Control": "no-store"})
    return True


def post(handler, relay, path: str) -> bool:
    """/api/admin/backup… 的 POST（admin.post 最先转过来：上传的请求体不是 JSON，也比别的接口大得多）。"""
    if not path.startswith(PREFIX + "/"):
        return False
    from . import admin
    if path == UPLOAD_PATH:
        caller = admin._admin(handler)
        if caller is None:
            handler._not_found()        # 没读请求体：_send 会关掉这条连接
            return True
        _upload(handler, relay, caller)
        return True
    body = admin._read_body(handler)
    caller = admin._admin(handler)
    if caller is None:
        handler._not_found()
        return True
    if body is None:
        handler._json({"ok": False, "error": "请求格式不对"}, 400)
        return True
    root = root_of(relay)
    if root is None:
        handler._json({"ok": False, "error": OFF})
        return True
    ip = handler._ip()
    if path == PREFIX + "/export":
        if not isinstance(body.get("logins", False), bool):
            handler._json({"ok": False, "error": "请求格式不对"}, 400)
            return True
        _export(handler, relay, root, bool(body.get("logins")), caller, ip)
    elif path == PREFIX + "/restore":
        _restore(handler, relay, root, body, caller, ip)
    elif path == PREFIX + "/restore/cancel":
        _drop_upload(root)
        dropped = backup.cancel(root)
        if dropped:
            relay.accounts.audit("backup.restore_cancel", caller.user, ip, "backup", "", "")
        handler._json({"ok": True})
    else:
        handler._not_found()
    return True


def _export(handler, relay, root: Path, logins: bool, caller, ip: str) -> None:
    if not _export_lock.acquire(blocking=False):
        handler._json({"ok": False, "error": "正在导出另一份，等一下再试"})
        return
    run = backup.run_dir(root)
    target = run / f".export-{secrets.token_hex(6)}.zip"
    try:
        run.mkdir(parents=True, exist_ok=True)
        try:
            manifest = backup.export(root, target, logins=logins, clock=relay.clock)
        except OSError as error:
            relay.log(f"导出设置失败：{type(error).__name__}")
            handler._json({"ok": False, "error": f"导出失败（{type(error).__name__}）"})
            return
        size = target.stat().st_size
        relay.accounts.audit("backup.export", caller.user, ip, "backup", "",
                             f"{len(manifest['files'])} 个文件" + ("，含登录信息" if logins else ""))
        relay.log(f"导出设置：{len(manifest['files'])} 个文件，{size // 1024} KB" + ("（含登录信息）" if logins else ""))
        handler.send_response(200)
        for name, value in (("Content-Type", "application/zip"), ("Content-Length", str(size)),
                            ("Content-Disposition", f'attachment; filename="{backup.export_name(manifest)}"'),
                            ("Cache-Control", "no-store"), ("Referrer-Policy", "no-referrer"),
                            ("X-Content-Type-Options", "nosniff")):
            handler.send_header(name, value)
        handler.end_headers()
        try:
            with open(target, "rb") as stream:
                for chunk in iter(lambda: stream.read(backup.CHUNK), b""):
                    handler.wfile.write(chunk)
        except OSError:
            handler.close_connection = True     # 浏览器中途断开
    finally:
        try:
            target.unlink()
        except OSError:
            pass
        _export_lock.release()


def _upload(handler, relay, caller) -> None:
    root = root_of(relay)
    try:
        length = int(handler.headers.get("Content-Length") or 0)
    except ValueError:
        length = -1
    if root is None or length <= 0 or length > backup.UPLOAD_MAX:
        # 不读这么大的请求体：回答后连接关掉。
        handler._json({"ok": False, "error": OFF if root is None else
                       f"备份文件太大（最多 {backup.UPLOAD_MAX // 1048576} MB）" if length > 0 else "没有收到文件"},
                      413 if length > backup.UPLOAD_MAX else 200)
        return
    if not _upload_lock.acquire(blocking=False):
        handler._json({"ok": False, "error": "正在检查另一份备份，等一下再试"})
        return
    run = backup.run_dir(root)
    part = run / (backup.UPLOAD + ".part")
    try:
        run.mkdir(parents=True, exist_ok=True)
        handler._read_body = True
        fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as out:
            left = length
            while left:
                chunk = handler.rfile.read(min(backup.CHUNK, left))
                if not chunk:
                    break
                out.write(chunk)
                left -= len(chunk)
        if left:
            handler.close_connection = True
            handler._json({"ok": False, "error": "文件没传完，再试一次"})
            return
        try:
            plan = backup.inspect(part)
        except backup.BackupError as error:
            handler._json({"ok": False, "error": str(error)})
            return
        _drop_upload(root)
        os.replace(part, run / backup.UPLOAD)
        info = {"id": secrets.token_urlsafe(12), "at": int(relay.clock()), "summary": backup.summary(plan, root)}
        (run / backup.UPLOAD_INFO).write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
        handler._json({"ok": True, "id": info["id"], "summary": info["summary"], "supervised": supervised(root, relay.clock)})
    except OSError as error:
        relay.log(f"收备份文件失败：{type(error).__name__}")
        handler._json({"ok": False, "error": f"存不下上传的文件（{type(error).__name__}）"})
    finally:
        try:
            part.unlink()
        except OSError:
            pass
        _upload_lock.release()


def _restore(handler, relay, root: Path, body: dict, caller, ip: str) -> None:
    info = _upload_info(root)
    if info is None or not isinstance(body.get("id"), str) or not secrets.compare_digest(body["id"], str(info.get("id"))):
        handler._json({"ok": False, "error": "没有刚检查过的备份：重新选一次文件"})
        return
    if not _upload_lock.acquire(blocking=False):
        handler._json({"ok": False, "error": "正在检查另一份备份，等一下再试"})
        return
    try:
        name = (relay.accounts.user(caller.user) or {}).get("name") or ""
        try:
            plan = backup.stage(backup.run_dir(root) / backup.UPLOAD, root, actor=name, clock=relay.clock, ready=False)
        except backup.BackupError as error:
            handler._json({"ok": False, "error": str(error)})
            return
        except OSError as error:
            backup.cancel(root)
            handler._json({"ok": False, "error": f"解不开备份（{type(error).__name__}）：现在的设置没有动"})
            return
        _drop_upload(root)
        created = time.strftime("%Y-%m-%d %H:%M", time.localtime(plan.manifest["created_at"]))
        relay.accounts.audit("backup.restore", caller.user, ip, "backup", "",
                             f"{created} 的备份" + ("，含登录信息" if plan.logins else ""))
        relay.log(f"从备份恢复设置：{created} 的备份已解好，等着替换")
        auto = supervised(root, relay.clock)
        # 先回答页面再放到位：监督进程一看到就会停掉中转。
        handler._json({"ok": True, "restart": "auto" if auto else "manual"})
        try:
            handler.wfile.flush()
        except OSError:
            pass
        try:
            backup.activate(root)
        except (OSError, backup.BackupError) as error:
            relay.log(f"从备份恢复设置：放不到位（{type(error).__name__}），没有恢复")
            backup.cancel(root)
    finally:
        _upload_lock.release()
