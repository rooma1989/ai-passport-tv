"""「导出诊断信息」：一个 zip，给用户自己决定要不要发出去；什么都不上传。

设计见 docs/superpowers/specs/2026-10-19-pc-client-architecture-design.md 第 5.15 节。里面有：日志（core.log 和
它的轮转份、托盘的 tray.log）、config.toml、desktop.json、控制接口的状态、版本和系统、ffmpeg 的版本、端口检查、
Windows 的网络类型（专用 / 公用）。

不放：secrets/ 整个文件夹、数据库（tv.db 和备份）、devices/、频道表。每个文本文件进 zip 前再过一遍 desktop.redact
（日志写的时候已经过了一遍；这里再去掉用户目录里的用户名），所以里面没有令牌、会话、设置码，IP 只有前三段。
"""

from __future__ import annotations

import json
import os
import platform
import sys
import time
import zipfile
from pathlib import Path

from .desktop import _run, port_free, port_holder, redact
from .version import VERSION

LOG_NAMES = ("core.log", "tray.log")
FIXED_PORTS = (8097, 8098, 8099, 8100, 8198, 8199)


def default_dest(data_dir: Path) -> Path:
    """桌面（有的话），否则数据目录的 exports/。"""
    desktop = Path.home() / "Desktop"
    return desktop if desktop.is_dir() else data_dir / "exports"


def _network_profile() -> str:
    """Windows：每个连接是「专用」还是「公用」（公用时防火墙挡住设备）；只取类型，不取网络名字。"""
    if os.name != "nt":
        return "n/a"
    out = _run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                "(Get-NetConnectionProfile | ForEach-Object { $_.NetworkCategory }) -join ','"], timeout=8)
    return out.strip() or "unknown"


def _about(desktop) -> str:
    lines = [f"core {VERSION}", f"python {sys.version.split()[0]}", f"platform {platform.platform()}",
             f"machine {platform.machine()}", f"frozen {bool(getattr(sys, 'frozen', False))}",
             f"time {time.strftime('%Y-%m-%d %H:%M:%S %z')}"]
    ffmpeg = desktop.ffmpeg.snapshot()
    lines.append(f"ffmpeg {ffmpeg['state']} {ffmpeg['source']} {ffmpeg['version']}")
    lines.append(f"network_profile {_network_profile()}")
    lines.append("ports:")
    for port in (desktop.lan_port,) + FIXED_PORTS:
        if port_free(desktop.bind, port):
            lines.append(f"  {port} free")
        else:
            holder = port_holder(port)
            lines.append(f"  {port} in use{' by ' + holder if holder else ''}")
    return "\n".join(lines) + "\n"


def build(desktop, dest_dir: Path | None = None, clock=time.time) -> Path:
    """做诊断压缩包，返回它的路径。"""
    dest = Path(dest_dir) if dest_dir else default_dest(desktop.layout.root)
    dest.mkdir(parents=True, exist_ok=True)
    name = time.strftime("rootv-diagnostics-%Y%m%d-%H%M%S.zip", time.localtime(clock()))
    target = dest / name
    home = str(Path.home())

    def clean(text: str) -> str:
        return redact(text, home=home)

    tmp = target.with_name(target.name + ".part")
    with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("about.txt", clean(_about(desktop)))
        archive.writestr("status.json", clean(json.dumps(desktop.status(), ensure_ascii=False, indent=1)))
        for path in (desktop.layout.config, desktop.layout.root / "desktop.json"):
            try:
                archive.writestr(path.name, clean(path.read_text(encoding="utf-8", errors="replace")))
            except OSError:
                pass
        logs = Path(desktop.logs_dir)
        if logs.is_dir():
            for path in sorted(logs.iterdir()):
                if path.is_file() and path.name.split(".log")[0] + ".log" in LOG_NAMES:
                    try:
                        text = path.read_text(encoding="utf-8", errors="replace")
                    except OSError:
                        continue
                    archive.writestr(f"logs/{path.name}", clean(text))
    os.replace(tmp, target)
    return target
