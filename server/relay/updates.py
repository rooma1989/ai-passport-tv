"""电脑版上的「更新」（管理员；电脑客户端才有）：读核心的更新状态、请它马上查、请托盘装。

设计：docs/superpowers/specs/2026-11-05-pc-client-updates-design.md 第 3、4 节。查和下在核心（server/selfhost/updater.py，
写 run/update.json），装在托盘（desktop/update.py）；中转和它们之间只用 run/ 里的文件，和「设置 → YouTube」一样：

    GET  /api/update             {"on", 状态…}：on 为假是这个客户端不自动更新（从源代码跑的、Docker 自建）
    POST /api/update/check       放 run/update-check，核心马上查
    POST /api/update/install     {"force"?}：有人在电脑上看、不带 force 时回答 {"ok": false, "busy": n}，页面问一句再带
                                 force 发；否则放 run/update-install，核心标上 install，托盘看到就去装、重启

只有管理员（自建时就是装软件的那个人），别人 404。改东西的请求由 web.py 先查同源。
"""

from __future__ import annotations

import json
from pathlib import Path

PREFIX = "/api/update"
STATUS_FILE = "update.json"
CHECK_REQUEST = "update-check"
INSTALL_REQUEST = "update-install"
FIELDS = ("state", "version", "notes", "date", "done", "total", "error", "checked", "install", "current", "result")


def _run_dir(relay) -> Path | None:
    data = getattr(relay, "selfhost_data", None)
    return Path(data) / "run" if data is not None else None


def _admin(handler, relay) -> bool:
    if relay.selfhost is None or _run_dir(relay) is None:
        return False
    caller = handler._caller()
    return caller is not None and caller.role == "admin" and not caller.guest


def status(relay) -> dict:
    run = _run_dir(relay)
    try:
        data = json.loads((run / STATUS_FILE).read_text(encoding="utf-8")) if run is not None else None
    except (OSError, ValueError):
        data = None
    if not isinstance(data, dict):
        return {"ok": True, "on": False}
    answer = {k: data[k] for k in FIELDS if k in data}
    answer.update(ok=True, on=True, available=data.get("state") == "ready")
    return answer


def watching(relay) -> int:
    """在电脑上看的有几路（watch.py 的流、点播的会话）；小电视不算：它们重启后自己接着放。"""
    count = 0
    if getattr(relay, "watch", None) is not None:
        count += len(relay.watch.streams())
    if getattr(relay, "vod", None) is not None:
        count += len(relay.vod.sessions())
    return count


def _request(relay, name: str) -> bool:
    run = _run_dir(relay)
    try:
        run.mkdir(parents=True, exist_ok=True)
        (run / name).write_text("", encoding="utf-8")
        return True
    except OSError:
        return False


def get(handler, relay, path: str) -> bool:
    if path != PREFIX:
        return False
    if not _admin(handler, relay):
        handler._not_found()
        return True
    handler._json(status(relay))
    return True


def post(handler, relay, path: str) -> bool:
    if path not in (PREFIX + "/check", PREFIX + "/install"):
        return False
    body = handler._body() or {}
    if not _admin(handler, relay):
        handler._not_found()
        return True
    if path.endswith("/check"):
        ok = _request(relay, CHECK_REQUEST)
        handler._json({"ok": ok} if ok else {"ok": False, "error": "写不了请求文件"})
        return True
    current = status(relay)
    if not current.get("available"):
        handler._json({"ok": False, "error": "新版本还没下好"})
        return True
    busy = watching(relay)
    if busy and not (isinstance(body, dict) and body.get("force") is True):
        handler._json({"ok": False, "busy": busy, "error": f"有 {busy} 路正在这台电脑上看，更新会中断它们"})
        return True
    if not _request(relay, INSTALL_REQUEST):
        handler._json({"ok": False, "error": "写不了请求文件"})
        return True
    relay.log(f"网页上点了「更新并重启」（{current.get('version')}）")
    handler._json({"ok": True, "version": current.get("version")})
    return True
