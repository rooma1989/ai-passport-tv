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
# 收藏夹台（中转的 fv01…，P1c）：B 站收藏夹，一个收藏夹一个台。
FAVS = "B站收藏"
GROUPS = (CAST, CREATORS, FAVS, "央视", "卫视", "港澳台", "国际", "音乐", "动画影视", "节目回看", "其他")
# 只按台 id 自动归的组：投屏、UP 主、收藏夹。这几组的台不能挪走，手机/总后台上也不能把别的台挑进来。
FIXED = (CAST, CREATORS, FAVS)
ASSIGNABLE = tuple(g for g in GROUPS if g not in FIXED)
OTHER = "其他"
# 剧集台（中转的 sr01…）默认放的组；和直播台一样可以在手机上改。
SERIES = "动画影视"
# 晚会、春晚这类录像（「2019春晚」「央视春晚 1983」）：不是直播，单独一组，不和直播台挤在「其他」里。
REPLAYS = "节目回看"
# 设备的频道列表放在一个 CONFIG 包里：包长和台数的上限，与固件 main/av_protocol.h 的
# AV_CONTROL_MAX、AV_CHANNEL_MAX 一致。中转加台前也按这个算，免得列表超长、设备一个台都收不到。
CONFIG_MAX = 7168
CHANNEL_MAX = 128
# CONFIG 里除了频道列表还有格式、会话、遥控地址、正在播的集等：按频道列表算字节时给它们留这么多。
CONFIG_SPARE_BYTES = 512
# 全局频道表（总后台的频道表、channels.txt）最多这么多个台。电脑上看不受设备上限约束，全表都能看；
# 每台设备只收得下前面一部分（device_list），按顺序截断。
TABLE_MAX = 1000
# 中转的台（UP 主 up…、剧集 sr…）在设备列表里的名字后面带上网站：「王志安(油管)」「熊出没(B站)」。
# 设备只拿到名字，只能这样标；括号是半角的（JSON 里各占 1 字节），整个标记 9 / 14 字节。
# 括号里的说明匹配台名时本来就不算（server/voice/match.strip_parens），unmark() 再把它拆回网站。
SITE_MARKS = {"bilibili": "B站", "youtube": "油管"}
# 中转的台的 id 前缀：UP 主 up…、剧集 sr…、收藏夹 fv…（后面跟数字）。
RELAY_PREFIXES = ("up", "sr", "fv")
# 设备菜单里能「下一个视频」的台：中转的台和投屏台（CONFIG 的 "next"，固件按前缀认）。
NEXT_PREFIXES = RELAY_PREFIXES + ("cast",)


def mark(site: str) -> str:
    """名字后面接的网站标记，例如「(油管)」；不认识的网站为空。"""
    word = SITE_MARKS.get(site, "")
    return f"({word})" if word else ""


def unmark(channel_id: str, name: str) -> tuple[str, str]:
    """设备列表里的名字 → (不带标记的名字, 网站)；没有标记（直播台、旧版中转）时网站为空。"""
    if channel_id[:2] in RELAY_PREFIXES and channel_id[2:].isdigit():
        for site in SITE_MARKS:
            suffix = mark(site)
            if name.endswith(suffix) and len(name) > len(suffix):
                return name[:-len(suffix)].rstrip(), site
    return name, ""

# 港澳台电视台的名字片段；先于「卫视」判断，香港卫视因此归这里。
_HK_TW = ("香港", "凤凰", "TVBS", "东森", "华视", "民视", "三立", "中视", "中天", "台视", "耀才")
_MUSIC = ("精选", "音乐")
_FILMS = ("猫和老鼠", "哆啦A梦", "海绵宝宝", "七龙珠", "中华小当家", "周星驰", "让子弹飞", "电影")
# 录像的名字片段：先于「央视」判断，「CCTV春晚1990」也归「节目回看」。
_REPLAYS = ("春晚", "春节联欢晚会", "春节晚会", "元宵晚会", "中秋晚会", "联欢晚会", "回看")
# 带中文说明的国际台，只看开头。
_WORLD_PREFIXES = ("CGTN", "CNA", "NHK", "BBC", "RT ", "TRT", "ABC", "Al Jazeera", "France 24")


def classify(channel_id: str, name: str) -> str:
    if channel_id == "cast":
        return CAST
    if channel_id.startswith("up") and channel_id[2:].isdigit():
        return CREATORS
    if channel_id.startswith("fv") and channel_id[2:].isdigit():
        return FAVS
    if channel_id.startswith("sr") and channel_id[2:].isdigit():
        return SERIES
    if any(part in name for part in _REPLAYS):
        return REPLAYS
    if name.upper().startswith("CCTV"):
        return "央视"
    if any(part in name for part in _HK_TW):
        return "港澳台"
    # 地方台的 4K 频道（河北4K、苏州4K……）也放这里，比放进「其他」好找。英文名的播放列表
    # （iptv-org 等）把卫视写成「Hunan Satellite TV」「Ningxia Satellite Channel」。
    if "卫视" in name or name.endswith("4K") or "Satellite" in name:
        return "卫视"
    if any(part in name for part in _MUSIC):
        return "音乐"
    if any(part in name for part in _FILMS):
        return "动画影视"
    if name.startswith(_WORLD_PREFIXES) or (name and all(ord(c) < 128 for c in name)):
        return "国际"
    return OTHER


# 播放列表自己写的分组名（M3U 的 group-title、TXT 的「分组,#genre#」）→ 我们的分组：只对意思相同的
# （「央视频道」「CCTV」「📺央视」都是央视）；「体育」「地方台」「4K」这类我们没有对应分组的不对，退回按台名归类。
# 按顺序比，先比到的算：「港澳台」在「卫视」前面（「香港卫视」那组归港澳台）。比的是去掉符号、表情和空格、
# 不分大小写以后的分组名里有没有这些字。
_TITLE_GROUPS = (
    (REPLAYS, ("春晚", "晚会", "回看")),
    ("港澳台", ("港澳台", "港澳", "港台", "香港", "澳门", "台湾", "hongkong", "taiwan", "macau")),
    ("央视", ("央视", "cctv", "中央台", "中央电视")),
    ("卫视", ("卫视", "satellite")),
    ("音乐", ("音乐", "mtv", "music")),
    ("动画影视", ("动画", "动漫", "卡通", "少儿", "儿童", "电影", "影视", "影院", "剧场", "电视剧", "movie", "film",
                 "cartoon", "anime", "kids", "animation")),
    ("国际", ("国际", "海外", "境外", "国外", "international", "world")),
)
TITLE_MAX = 40


def from_title(title) -> str:
    """播放列表的分组名对到 GROUPS 里的一个（不含投屏和 UP 主）；对不上（或没写）是空串。"""
    if not isinstance(title, str):
        return ""
    plain = "".join(c for c in title[:TITLE_MAX * 2] if c.isalnum()).casefold()
    if not plain:
        return ""
    for group, words in _TITLE_GROUPS:
        if any(word in plain for word in words):
            return group
    return ""


def group_of(channel_id: str, name: str, overrides: dict[str, str]) -> str:
    automatic = classify(channel_id, name)
    # 投屏、UP 主和收藏夹的台固定在自己的组里（留着的旧调整也不管），手动调整只对直播台和剧集台生效。
    if automatic in FIXED:
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


# 空列表的 config_bytes；每多一个台加上它那一项和一个逗号（第一个台没有逗号）。
_EMPTY_BYTES = config_bytes([])


def _entry_bytes(channel_id: str, name: str) -> int:
    return len(json.dumps({"id": channel_id, "name": name, "g": 0}, ensure_ascii=True, separators=(",", ":")))


def _prefix(sizes: list[int], budget: int, limit: int) -> int:
    """sizes 从头取几项，config_bytes 不超过 budget、个数不超过 limit。"""
    total, count = _EMPTY_BYTES, 0
    for size in sizes:
        total += size + (1 if count else 0)
        if count >= limit or total > budget:
            break
        count += 1
    return count


DEVICE_BUDGET = CONFIG_MAX - CONFIG_SPARE_BYTES


# 放不下时直播台的名字按 CONFIG 里占的字节缩短（汉字在 JSON 里是 \\uXXXX，一个 6 字节；字母 1 字节）：
# 先截占地方的中文长名，短的英文名（CCTV10）几乎不占地方，不动。最多缩到 NAME_LEAST_BYTES（4 个汉字），
# 从 NAME_MOST_BYTES（16 个汉字）开始试。按字数截曾把 CCTV10…CCTV14 都截成了「CCTV1」。
NAME_LEAST_BYTES = 4 * 6
NAME_MOST_BYTES = 16 * 6
_OPEN = "(（[【《"


def _name_bytes(text: str) -> int:
    return len(json.dumps(text, ensure_ascii=True)) - 2


# 一个字在 CONFIG 里占几字节（json.dumps(ensure_ascii=True) 的转义）：可打印 ASCII 1，引号和反斜杠 2，
# \b\f\n\r\t 2，其余控制字符和非 ASCII 6（\uXXXX），BMP 以外的 12（两个 \uXXXX）。和 _name_bytes 一样，只是快。
_TWO = frozenset('"\\\b\f\n\r\t')


def _char_bytes(char: str) -> int:
    code = ord(char)
    if char in _TWO:
        return 2
    if 0x20 <= code <= 0x7E:
        return 1
    return 12 if code > 0xFFFF else 6


def _cut(name: str, most: int) -> str:
    """name 在 CONFIG 里不超过 most 字节的最长前缀；截在没配对的括号里时连括号一起去掉（「RT Documentary（英」
    → 「RT Documentary」），去掉后什么都不剩就留着括号前的半截。"""
    used, end = 0, len(name)
    for at, char in enumerate(name):
        used += _char_bytes(char)
        if used > most:
            end = at
            break
    if end == len(name):
        return name
    cut = name[:end].rstrip()
    opened = max(cut.rfind(c) for c in _OPEN)
    if opened > 0 and not any(c in cut[opened:] for c in ")）]】》"):
        cut = cut[:opened].rstrip() or cut
    return cut


def _relay(channel_id: str) -> bool:
    return channel_id == "cast" or (channel_id[:2] in RELAY_PREFIXES and channel_id[2:].isdigit())


def _shortened(names: list[tuple[str, str]], most: int) -> list[tuple[str, str]]:
    """直播台的名字缩到 CONFIG 里不超过 most 字节（中转的台、投屏台不动：它们的名字已经限过长，还带网站标记）。
    缩完和别的台重名的（「湖南卫视4K」→「湖南卫视」）不缩，宁可多占几个字节也不让两个台同名。"""
    cuts = [v if _relay(k) else _cut(v, most) for k, v in names]
    seen: dict[str, int] = {}
    for v in cuts:
        seen[v] = seen.get(v, 0) + 1
    out = []
    for (k, v), c in zip(names, cuts):
        out.append((k, v if c != v and seen[c] > 1 else c))
    return out


def _fit_count(rows: list[tuple[str, str]], budget: int, limit: int) -> int:
    """rows 从头取几个台放得下（_prefix；字节按需才算，取满就停）。"""
    return _prefix((_entry_bytes(k, v) for k, v in rows), budget, limit)


def _fit_names(names: list[tuple[str, str]], budget: int, limit: int, keep: str = "") -> list[tuple[str, str]]:
    """放得下的台数能因缩短直播台的名字变多时才缩，缩得尽量少；否则原样。

    只看可能发出去的台：前 limit 个，加上排在后面的 keep（device_list 把它接在最后）。n_full 是名字不缩时放得下
    几个台，n_floor 是全缩到 NAME_LEAST_BYTES 时几个；一样多（比如卡在台数上限 limit 上）就不缩。不然二分找
    放得下 n_floor 个台的最大字节上限：上限越大名字越长，放得下的台数不增——除了缩完会重名而不缩的台（上限越小
    重名的越多，它们反而变回全名），所以不保证严格单调；二分的结果最后再验一遍，不对就退回 NAME_LEAST_BYTES，
    那样至少和原来一样多。"""
    head = len(names) if len(names) <= limit else limit
    at = next((i for i, (k, _) in enumerate(names) if k == keep), -1) if keep else -1
    tail = [at] if at >= head else []
    rows = names[:head] + [names[i] for i in tail]
    n_full = _fit_count(rows[:head], budget, limit)
    if n_full == len(names) or n_full == limit:
        return names
    floor = _shortened(rows, NAME_LEAST_BYTES)
    n_floor = _fit_count(floor[:head], budget, limit)
    if n_floor <= n_full:
        return names
    low, high, best = NAME_LEAST_BYTES, NAME_MOST_BYTES, floor
    while low < high:                     # 找最大的 most：_fit_count(_shortened(rows, most)) >= n_floor
        mid = (low + high + 1) // 2
        tried = _shortened(rows, mid)
        if _fit_count(tried[:head], budget, limit) >= n_floor:
            low, best = mid, tried
        else:
            high = mid - 1
    if _fit_count(best[:head], budget, limit) < n_floor:
        best = floor
    out = best[:head] + names[head:]
    for i, row in zip(tail, best[head:]):
        out[i] = row
    return out


def device_list(names, keep: str = "", budget: int = DEVICE_BUDGET,
                limit: int = CHANNEL_MAX) -> list[tuple[str, str]]:
    """设备收得下的那部分频道列表：按顺序取，取到再多一个台就超过 limit 个或 config_bytes 超过 budget 为止。

    names 已经是优先的顺序（投屏台、自己的 UP 主和剧集在前，再是全局表）。keep：正在播的台，排在截断处
    之后时也留着，接在最后（占掉前面最后一两个台的位子），设备上的「正在播」和换台序号才对得上；
    它一个都放不下时不管它。同样的输入总是同样的结果。"""
    names = _fit_names(list(names), budget, limit, keep)
    sizes = [_entry_bytes(k, v) for k, v in names]
    count = _prefix(sizes, budget, limit)
    at = next((i for i, (k, _) in enumerate(names) if k == keep), -1) if keep else -1
    if at < count:
        return names[:count]
    extra = sizes[at] + 1
    if count == 0 or _EMPTY_BYTES + sizes[at] > budget:
        return names[:count]
    return names[:_prefix(sizes, budget - extra, limit - 1)] + [names[at]]


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
