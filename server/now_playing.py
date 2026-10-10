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
# 会话刚结束的这段时间仍算在线：换台、断流重连时设备几秒内就会回来，
# 这时手机上的换台先记下，设备连上时直接给它新台（见 tv_server 的 pending_switch）。
GRACE_S = 15.0


def write(path: Path | None, channel: str, connected: bool, switch: bool,
          now: float | None = None, since: float = 0.0) -> None:
    """since：这次会话开始的时间。手机据此分辨小智是不是已经切到它要的台（新会话）。"""
    if path is None:
        return
    path = Path(path)
    record = {"channel": channel, "connected": bool(connected), "switch": bool(switch),
              "at": time.time() if now is None else now, "since": float(since)}
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(json.dumps(record), encoding="utf-8")
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except OSError:
        # 只是给手机网页看的状态，写不了不影响播放。
        pass


def read(path: Path | None, now: float | None = None) -> dict:
    """{"channel", "online", "switch", "since"}；读不到就是不在线。"""
    now = time.time() if now is None else now
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8")) if path else {}
        at = float(data.get("at", 0))
    except (OSError, ValueError, TypeError, AttributeError):
        data, at = {}, 0.0
    age = now - at
    online = age < (STALE_S if data.get("connected") else GRACE_S)
    try:
        since = float(data.get("since") or 0.0)
    except (TypeError, ValueError):
        since = 0.0
    return {"channel": str(data.get("channel") or ""), "online": online,
            "switch": bool(data.get("switch")) and online, "since": since}
