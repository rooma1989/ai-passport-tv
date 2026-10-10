"""电视服务写、中转读的「正在播放」状态：哪个台、设备在不在线、支不支持切台。

会话开始、结束时写，播放中每 10 秒刷新一次；标为在线但超过 30 秒没刷新，就当作已离线
（设备断网时会话可能要过一会儿才结束）。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

REFRESH_S = 10.0
STALE_S = 30.0


def write(path: Path | None, channel: str, connected: bool, switch: bool,
          now: float | None = None) -> None:
    if path is None:
        return
    path = Path(path)
    record = {"channel": channel, "connected": bool(connected), "switch": bool(switch),
              "at": time.time() if now is None else now}
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(json.dumps(record), encoding="utf-8")
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except OSError:
        # 只是给手机网页看的状态，写不了不影响播放。
        pass


def read(path: Path | None, now: float | None = None) -> dict:
    """{"channel", "online", "switch"}；读不到就是不在线。"""
    now = time.time() if now is None else now
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8")) if path else {}
        at = float(data.get("at", 0))
    except (OSError, ValueError, TypeError, AttributeError):
        data, at = {}, 0.0
    online = bool(data.get("connected")) and now - at < STALE_S
    return {"channel": str(data.get("channel") or ""), "online": online,
            "switch": bool(data.get("switch")) and online}
