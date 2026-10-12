"""每台设备的文件：中转写、电视服务读（或反过来），放在 devices/<设备 id>/ 下。

和单用户时一样是文件，只是每台一套：电视服务那一侧的读写方式完全不变
（server/now_playing.py、server/relay/events.py、server/live.py、server/channel_groups.py）。
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import NamedTuple

# 新固件报的设备 id 是出厂 MAC（12 位小写十六进制）；迁移来的旧设备是 legacy-xingzhi 之类。
DEVICE_ID = re.compile(r"^(?:[0-9a-f]{12}|legacy-[a-z0-9]{1,20})$")


class DeviceFiles(NamedTuple):
    now: Path          # 电视服务写：正在播放（now-playing.json）
    events: Path       # 中转写：投屏、换台、暂停……（relay-events.json）
    channels: Path     # 中转写：这台设备的 UP 主、剧集、投屏台（relay-channels.txt）
    groups: Path       # 中转写：设备主人调整过的分组（channel-groups.json）
    remote: Path       # 中转写：状态页上的二维码地址（remote-url.txt）


def device_files(devices_dir: Path | str, device_id: str) -> DeviceFiles:
    if not DEVICE_ID.match(device_id or ""):
        raise ValueError(f"bad device id {device_id!r}")
    base = Path(devices_dir) / device_id
    return DeviceFiles(now=base / "now-playing.json", events=base / "relay-events.json",
                       channels=base / "relay-channels.txt", groups=base / "channel-groups.json",
                       remote=base / "remote-url.txt")


def ensure_dir(devices_dir: Path | str, device_id: str) -> DeviceFiles:
    files = device_files(devices_dir, device_id)
    base = files.now.parent
    base.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(base, 0o750)
    except OSError:
        pass
    return files
