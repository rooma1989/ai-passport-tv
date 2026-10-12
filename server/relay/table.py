"""生成给电视服务读的 relay-channels.txt（格式同 channels.txt）。"""

from __future__ import annotations

import os
from pathlib import Path

STREAM_BASE = "http://127.0.0.1:8099/ch/"
# 投屏台总在表里，排第一：设备只能被切到它频道列表里已有的 id。
CAST_ID = "cast"
CAST_LABEL = "我的投屏"
# 频道名进设备的 CONFIG 包，每个汉字 JSON 转义后占 6 字节；截短以免整张表超过 TV_CONTROL_MAX。
# 按显示宽度截：汉字等非 ASCII 字符算 2 格，ASCII 算 1 格，即 10 个汉字或 20 个字母。
NAME_COLUMNS = 20


def label(creator) -> str:
    """设备频道列表里显示的名字。"""
    return label_text(creator.name) or creator.id


def label_text(name: str) -> str:
    """名字截到 NAME_COLUMNS 格；去掉会破坏表格式的竖线。"""
    name = " ".join(name.replace("|", " ").split())
    kept, used = [], 0
    for char in name:
        width = 1 if ord(char) < 128 else 2
        if used + width > NAME_COLUMNS:
            break
        kept.append(char)
        used += width
    return "".join(kept).strip()


def render(creators, base: str = STREAM_BASE) -> str:
    """base：播放流地址的前缀；多用户时带上设备 id（STREAM_BASE + "<设备 id>/"）。"""
    lines = ["# 由视频中转生成，不要手改。每行： id | 显示名 | 地址",
             f"{CAST_ID} | {CAST_LABEL} | {base}{CAST_ID}"]
    lines += [f"{c.id} | {label(c)} | {base}{c.id}" for c in creators]
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
