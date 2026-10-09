"""生成给电视服务读的 relay-channels.txt（格式同 channels.txt）。"""

from __future__ import annotations

import os
from pathlib import Path

STREAM_BASE = "http://127.0.0.1:8099/ch/"
# 频道名进设备的 CONFIG 包，每个汉字 JSON 转义后占 6 字节；截短以免整张表超过 TV_CONTROL_MAX。
NAME_MAX = 10


def _label(creator) -> str:
    name = " ".join(creator.name.replace("|", " ").split())[:NAME_MAX].strip()
    return name or creator.id


def render(creators) -> str:
    lines = ["# 由视频中转生成，不要手改。每行： id | 显示名 | 地址"]
    lines += [f"{c.id} | {_label(c)} | {STREAM_BASE}{c.id}" for c in creators]
    return "\n".join(lines) + "\n"


def write_if_changed(path: Path, text: str) -> bool:
    """内容没变就不写：电视服务按 mtime 判断是否重读。"""
    path = Path(path)
    try:
        if path.read_text(encoding="utf-8") == text:
            return False
    except FileNotFoundError:
        pass
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)
    return True
