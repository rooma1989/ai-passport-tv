"""频道分组：按台名自动归类，手机上可以改单个台的分组。

电视服务把分组随频道列表发给设备（分组名一次，每台一个编号），中转在手机网页上按分组列出台。
分组表文件（channel-groups.json）只由中转写；坏文件或不认识的分组名一律忽略，退回按名字归类。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

CAST = "我的投屏"
CREATORS = "UP 主"
GROUPS = (CAST, CREATORS, "央视", "卫视", "港澳台", "国际", "音乐", "动画影视", "其他")
OTHER = "其他"
# 剧集台（中转的 sr01…）默认放的组；和直播台一样可以在手机上改。
SERIES = "动画影视"
# 设备的频道列表放在一个 CONFIG 包里：包长和台数的上限，与固件 main/av_protocol.h 的
# AV_CONTROL_MAX、AV_CHANNEL_MAX 一致。中转加台前也按这个算，免得列表超长、设备一个台都收不到。
CONFIG_MAX = 7168
CHANNEL_MAX = 128
# 中转的台（UP 主 up…、剧集 sr…）在设备列表里的名字后面带上网站：「王志安(油管)」「熊出没(B站)」。
# 设备只拿到名字，只能这样标；括号是半角的（JSON 里各占 1 字节），整个标记 9 / 14 字节。
# 括号里的说明匹配台名时本来就不算（server/voice/match.strip_parens），unmark() 再把它拆回网站。
SITE_MARKS = {"bilibili": "B站", "youtube": "油管"}
_RELAY_KINDS = ("up", "sr")


def mark(site: str) -> str:
    """名字后面接的网站标记，例如「(油管)」；不认识的网站为空。"""
    word = SITE_MARKS.get(site, "")
    return f"({word})" if word else ""


def unmark(channel_id: str, name: str) -> tuple[str, str]:
    """设备列表里的名字 → (不带标记的名字, 网站)；没有标记（直播台、旧版中转）时网站为空。"""
    if channel_id[:2] in _RELAY_KINDS and channel_id[2:].isdigit():
        for site in SITE_MARKS:
            suffix = mark(site)
            if name.endswith(suffix) and len(name) > len(suffix):
                return name[:-len(suffix)].rstrip(), site
    return name, ""

# 港澳台电视台的名字片段；先于「卫视」判断，香港卫视因此归这里。
_HK_TW = ("香港", "凤凰", "TVBS", "东森", "华视", "民视", "三立", "中视", "中天", "台视", "耀才")
_MUSIC = ("精选", "音乐")
_FILMS = ("猫和老鼠", "哆啦A梦", "海绵宝宝", "七龙珠", "中华小当家", "周星驰", "让子弹飞", "电影")
# 带中文说明的国际台，只看开头。
_WORLD_PREFIXES = ("CGTN", "CNA", "NHK", "BBC", "RT ", "TRT", "ABC", "Al Jazeera", "France 24")


def classify(channel_id: str, name: str) -> str:
    if channel_id == "cast":
        return CAST
    if channel_id.startswith("up") and channel_id[2:].isdigit():
        return CREATORS
    if channel_id.startswith("sr") and channel_id[2:].isdigit():
        return SERIES
    if name.upper().startswith("CCTV"):
        return "央视"
    if any(part in name for part in _HK_TW):
        return "港澳台"
    # 地方台的 4K 频道（河北4K、苏州4K……）也放这里，比放进「其他」好找。
    if "卫视" in name or name.endswith("4K"):
        return "卫视"
    if any(part in name for part in _MUSIC):
        return "音乐"
    if any(part in name for part in _FILMS):
        return "动画影视"
    if name.startswith(_WORLD_PREFIXES) or (name and all(ord(c) < 128 for c in name)):
        return "国际"
    return OTHER


def group_of(channel_id: str, name: str, overrides: dict[str, str]) -> str:
    automatic = classify(channel_id, name)
    # 投屏和 UP 主的台固定在自己的组里，手动调整只对直播台生效。
    if automatic in (CAST, CREATORS):
        return automatic
    return overrides.get(channel_id, automatic)


def load_overrides(path: Path | None) -> dict[str, str]:
    if path is None:
        return {}
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): v for k, v in data.items() if isinstance(v, str) and v in GROUPS}


def save_overrides(path: Path, overrides: dict[str, str]) -> None:
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(overrides, ensure_ascii=False, indent=1), encoding="utf-8")
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


def config_bytes(names) -> int:
    """频道列表下发给设备时 CONFIG 包大约的字节数（JSON 转义后每个汉字占 6 字节）。"""
    return len(json.dumps({"groups": list(GROUPS),
                           "channel_list": [{"id": k, "name": v, "g": 0} for k, v in names]},
                          ensure_ascii=True, separators=(",", ":")).encode("ascii"))


def read_table(path: Path) -> list[tuple[str, str]]:
    """频道表里的 (id, 显示名)；只为列出台用，格式检查由 live.parse_channels 负责。"""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return []
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) >= 3 and parts[0] and parts[1]:
            rows.append((parts[0], parts[1]))
    return rows
