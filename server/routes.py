"""一个直播台的几条线路（备用地址）：存在哪、按什么顺序试、坏了怎么说给人听。

网上的直播源列表里一个台常常有好几条线路（同名的几行，TXT 列表里一行用 # 隔开的几个地址），单条线路
随时会失效（403、超时、播一会儿就断）。频道表 channels.txt 的格式不变（id | 名字 | 地址 [| UA]，那个
地址是主线路，设备协议、live.parse_channels 都不认识别的），备用线路存在旁边的 <频道表>.meta.json 里，
和台标、tvg-id 放在一起（server/relay/channeltable.py）。每个台一项：

    "ch003": {"tvg_id": "CCTV2", "logo": "…",
              "routes": [{"url": "https://…", "ua": "…"}, …],     # 备用线路，不含主线路，至多 BACKUPS_MAX 条
              "good": "<指纹>",                                   # 上次放得起来的那条（电脑上看和电视服务都写）
              "checks": {"<指纹>": {"ok": true, "why": "", "at": 1760000000, "ms": 800}},  # 「检测线路」的结果（总后台点的、后台自检的、放的时候明确坏了的）
              "starts": {"pc": {"<指纹>": 4200}, "tv": {"<指纹>": 6100}},  # 真放起来用了多久（毫秒，见 speeds_of）
              "lenient": ["<指纹>"],                              # 分片地址不标准、要放宽 ffmpeg 检查的线路（LENIENT_*）
              "quality": {"<指纹>": {"pace": 0.6, "pace_at": …,     # 真实速度（几倍实时，remember_pace）
                                     "live": true, "still": false,  # 画面隔了很久一模一样（循环录像、静止画面）
                                     "odd": [false, true, true]}}}  # 最近几次和同台其他线路的画面对不上（server/relay/linequality.py）

指纹是地址的 SHA-256 前 12 位：meta 里不重复存地址，地址里常有会话参数，也不进日志。这个文件坏了、没有
都不要紧：只有主线路，和以前一样。

只用标准库：电视服务（server/live.py、server/tv_server.py）、中转（server/relay/watch.py、admin.py）都用它。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from pathlib import Path

# 一个台至多几条线路（主线路 + 备用）。
ROUTES_MAX = 5
BACKUPS_MAX = ROUTES_MAX - 1
# 和 server/relay/channeltable.py 的 URL_MAX、AGENT_MAX 一样。
URL_MAX = 400
AGENT_MAX = 120
WHY_MAX = 60
META_SUFFIX = ".meta.json"
# 一条线路起播以后至少放了这么久，再断才算「放过一阵的线路断了」（先在它上面重试）；不到这么久就断
# 和起不来一样，换下一条。
EARLY_S = 45.0
# 快多少才不再先试上次好的那条：别的线路的用时不到它的 FASTER 倍（快 40% 以上）。差得不多的多半是网络抖动，
# 不为它换来换去。
FASTER = 0.6
# 真放起来的用时（starts）变了这么多（相对上次存的）才写文件；内存里每次都记。
START_CHANGE = 0.2
START_KINDS = ("pc", "tv")
MS_MAX = 600_000
# 同一个进程里改 meta 文件的人排队（总后台保存、检测线路、记住好线路）；跨进程（电视服务和中转）靠整份
# 原子替换，偶尔丢一次「上次好的线路」不要紧。
META_LOCK = threading.Lock()


def fingerprint(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:12]


def _bad_text(value: str) -> bool:
    return any(ord(c) < 32 for c in value) or "|" in value


def clean_route(item) -> dict | None:
    """{"url", "ua"?} 或 None（不合格）。"""
    if not isinstance(item, dict):
        return None
    url, agent = item.get("url"), item.get("ua", item.get("agent", ""))
    if not isinstance(url, str) or not isinstance(agent, str):
        return None
    url, agent = url.strip(), " ".join(agent.split())
    if not url.startswith(("http://", "https://")) or len(url) > URL_MAX or " " in url or _bad_text(url):
        return None
    if len(agent) > AGENT_MAX or _bad_text(agent):
        agent = ""
    return {"url": url, "ua": agent} if agent else {"url": url}


def clean_routes(items, primary: str = "") -> list[dict]:
    """备用线路：去掉不合格的、和主线路一样的、重复的，至多 BACKUPS_MAX 条。"""
    found, seen = [], {primary}
    for item in items if isinstance(items, list) else []:
        route = clean_route(item)
        if route is None or route["url"] in seen:
            continue
        seen.add(route["url"])
        found.append(route)
        if len(found) >= BACKUPS_MAX:
            break
    return found


def bad_routes(items) -> list[int]:
    """页面发来的备用线路里不合格的是第几条（从 1 数）。"""
    return [n for n, item in enumerate(items if isinstance(items, list) else [], 1) if clean_route(item) is None]


# ---------------------------------------------------------------- meta 文件

def meta_path(path) -> Path:
    path = Path(path)
    return path.with_name(path.name + META_SUFFIX)


def read_meta(path) -> dict:
    """整份 meta（{台 id: {…}}，只留是字典的项）；没有、坏了是 {}。"""
    if path is None:
        return {}
    try:
        data = json.loads(meta_path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, dict)}


def write_meta(path, data: dict) -> None:
    """整份原子替换；没有一项时删掉文件。"""
    target = meta_path(path)
    data = {k: v for k, v in data.items() if v}
    if not data:
        try:
            target.unlink()
        except FileNotFoundError:
            pass
        return
    tmp = target.with_name(f"{target.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=0, sort_keys=True), encoding="utf-8")
    os.chmod(tmp, 0o644)
    os.replace(tmp, target)


def update_meta(path, change) -> bool:
    """读、改（change(data) 原地改）、有变化才写。写不了（只读目录）返回 False，不抛。"""
    if path is None:
        return False
    with META_LOCK:
        data = read_meta(path)
        before = json.dumps(data, sort_keys=True)
        change(data)
        if json.dumps(data, sort_keys=True) == before:
            return True
        try:
            write_meta(path, data)
        except OSError:
            return False
    return True


def backups_of(entry) -> list[dict]:
    return clean_routes(entry.get("routes")) if isinstance(entry, dict) else []


def table_routes(path, cid: str, url: str, agent: str = "", default_agent: str = "") -> list[tuple[str, str]]:
    """这个台的所有线路 [(地址, UA)]，频道表的顺序：主线路（channels.txt 里的）在前，再是 meta 里的备用线路。
    没写 UA 的用 default_agent。"""
    entry = read_meta(path).get(cid, {}) if path is not None else {}
    routes = [(url, agent or default_agent)]
    for route in clean_routes(entry.get("routes"), url):
        routes.append((route["url"], route.get("ua") or default_agent))
    return routes


def good_of(path, cid: str) -> str:
    """上次放得起来的那条线路的指纹（没有时空串）。"""
    return good_in(read_meta(path).get(cid, {}) if path is not None else {})


def good_in(entry) -> str:
    good = entry.get("good") if isinstance(entry, dict) else ""
    return good if isinstance(good, str) and re.fullmatch(r"[0-9a-f]{12}", good) else ""


def entry_of(path, cid: str) -> dict:
    """meta 里这个台的那一项（没有时 {}）。"""
    entry = read_meta(path).get(cid, {}) if path is not None else {}
    return entry if isinstance(entry, dict) else {}


def _ms(value) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and 0 < value <= MS_MAX else None


def speeds_of(entry, kind: str = "pc") -> dict[str, int]:
    """每条线路（指纹）多快（毫秒）：真放起来过的（starts[kind]：电脑上看从起 ffmpeg 到出第一段，电视服务从开会话到
    缓冲够）用它；没放过的用上次「检测线路」能连上时的用时（取列表和一个片段的头几 KB）。检测的用时比真放起来短
    （不起 ffmpeg），所以只检测过的快线路会先被试一次，放过以后按真的用时排——慢的自然排到后面。"""
    speeds: dict[str, int] = {}
    if not isinstance(entry, dict):
        return speeds
    checks = entry.get("checks") if isinstance(entry.get("checks"), dict) else {}
    for mark, found in checks.items():
        if isinstance(found, dict) and found.get("ok") is True and _ms(found.get("ms")):
            speeds[mark] = found["ms"]
    speeds.update(starts_in(entry, kind))
    return speeds


def starts_in(entry, kind: str = "pc") -> dict[str, int]:
    """meta 里这个台各条线路真放起来的用时（毫秒，指纹 → 毫秒），kind 是 "pc" 或 "tv"。"""
    starts = entry.get("starts") if isinstance(entry, dict) and isinstance(entry.get("starts"), dict) else {}
    played = starts.get(kind) if isinstance(starts.get(kind), dict) else {}
    return {mark: ms for mark, ms in played.items() if _ms(ms)}


def preferred(routes: list[tuple[str, str]], good: str = "", bad: set[str] | frozenset = frozenset(),
              speeds: dict[str, int] | None = None, weak: set[str] | frozenset = frozenset()) -> list[int]:
    """试线路的顺序（下标）：
    1. 上次好的那条先试——除非另有一条没坏的线路比它快很多（用时不到它的 FASTER 倍；它的用时不知道时照旧先试它），
       或者它实测跟不上实时（weak）；
    2. 别的按用时（speeds，指纹 → 毫秒）从快到慢，不知道用时的在后面，同样快的、都不知道的按频道表的顺序；
    3. 实测跟不上实时的（weak，weak_in）排在这些后面；
    4. 最近坏过的、画面可疑的（bad，指纹，调用方并上 suspect_in）放最后。"""
    speeds = speeds or {}
    marks = [fingerprint(url) for url, _ in routes]
    order = list(range(len(routes)))
    speed = [speeds.get(m) for m in marks]
    sticky = good in marks and good not in bad and good not in weak
    if sticky:
        mine = speed[marks.index(good)]
        known = [s for i, s in enumerate(speed) if s is not None and marks[i] not in bad and marks[i] != good]
        if mine is not None and known and min(known) <= mine * FASTER:
            sticky = False
    return sorted(order, key=lambda i: (marks[i] in bad, marks[i] in weak, not (sticky and marks[i] == good),
                                        speed[i] is None, speed[i] or 0, i))


def blend(old: int | None, ms: int) -> int:
    """新量的用时和以前的各占一半（一次网络抖动不至于翻盘）；以前没有时就是这一次。"""
    return int(ms) if not old else int(round((old + ms) / 2))


def remember_start(path, cid: str, url: str, ms: int, kind: str = "pc") -> bool:
    """记下这条线路真放起来用了多久（毫秒，调用方已经和以前的 blend 过）。和存着的差不到 START_CHANGE 不写文件。"""
    ms = _ms(int(ms)) if isinstance(ms, (int, float)) else None
    if path is None or not url or ms is None or kind not in START_KINDS:
        return False
    mark = fingerprint(url)
    stored = starts_in(entry_of(path, cid), kind).get(mark)
    if stored and abs(ms - stored) < stored * START_CHANGE:
        return False

    def change(data: dict) -> None:
        entry = data.setdefault(cid, {})
        starts = entry.get("starts") if isinstance(entry.get("starts"), dict) else {}
        mine = starts.get(kind) if isinstance(starts.get(kind), dict) else {}
        mine[mark] = ms
        starts[kind] = mine
        entry["starts"] = starts

    return update_meta(path, change)


def remember_good(path, cid: str, url: str) -> bool:
    """记住这个台放得起来的线路（和上次一样就不写文件）。只记频道表里有的线路。"""
    mark = fingerprint(url)

    def change(data: dict) -> None:
        data.setdefault(cid, {})["good"] = mark

    if path is None or not url or good_of(path, cid) == mark:
        return False
    return update_meta(path, change)


# ---------------------------------------------------------------- 检测结果（checks）

# 一放就能认定线路坏了的原因（routes.classify 的分类）：源站明说拒绝、要登录、找不到。超时、中途断开这类
# 可能是一时的网络问题，不算。
DEFINITIVE = ("403", "401", "404")


def definitive(text: str) -> bool:
    """ffmpeg / HTTP 的原话说的是不是「这条线路坏了」（DEFINITIVE），而不是一时连不上。"""
    return classify(text) in DEFINITIVE


def bad_in(entry) -> set[str]:
    """meta 里这个台上次检测连不上的线路（指纹）：排线路顺序时放最后（preferred 的 bad）。"""
    checks = entry.get("checks") if isinstance(entry, dict) and isinstance(entry.get("checks"), dict) else {}
    return {mark for mark, found in checks.items() if isinstance(found, dict) and found.get("ok") is False}


def record_checks(path, cid: str, results, keep: set[str] | None = None) -> bool:
    """把检测结果写进 meta 的 checks：results 是 [(指纹, ok, why, ms, at)]。keep 给了时只留这些线路的结果
    （频道表里已经没有的线路的旧结果去掉）。连不上的线路：以前真放起来的用时不再算数（不然排序时它还显得快），
    它是上次好的那条时也不再先试它。"""
    return record_many(path, {cid: list(results)}, {cid: keep} if keep is not None else None)


def record_many(path, found: dict, keep: dict | None = None) -> bool:
    """几个台的检测结果一次写进 meta（{台 id: [(指纹, ok, why, ms, at)]}，后台自检攒一阵写一次，不必每测一条就
    重写整个文件）。keep：{台 id: 只留哪些线路的结果}。"""
    if path is None or not found:
        return False

    def change(data: dict) -> None:
        for cid, results in found.items():
            _apply_checks(data, cid, results, (keep or {}).get(cid))

    return update_meta(path, change)


def _apply_checks(data: dict, cid: str, results, keep: set[str] | None) -> None:
    entry = data.setdefault(cid, {})
    checks = entry.get("checks") if isinstance(entry.get("checks"), dict) else {}
    checks = {k: v for k, v in checks.items() if keep is None or k in keep}
    starts = entry.get("starts") if isinstance(entry.get("starts"), dict) else {}
    for mark, ok, why, ms, at in results:
        checks[mark] = {"ok": bool(ok), "why": str(why or "")[:WHY_MAX], "at": int(at)}
        if ok and _ms(int(ms or 0)):
            # 用时（毫秒）：从开始取列表到拿到片段的头几 KB，排线路顺序用（speeds_of）。
            checks[mark]["ms"] = max(1, int(ms))
        if not ok:
            for played in starts.values():
                if isinstance(played, dict):
                    played.pop(mark, None)
            if entry.get("good") == mark:
                entry.pop("good", None)
    entry["checks"] = checks
    starts = {k: v for k, v in starts.items() if isinstance(v, dict) and v}
    if starts:
        entry["starts"] = starts
    else:
        entry.pop("starts", None)


def mark_failed(path, cid: str, url: str, why: str, at: float) -> bool:
    """放的时候这条线路明确坏了（403 / 404……，definitive）：马上记成检测连不上，下次起播排在最后，
    后台的自检（server/relay/linecheck.py）半小时后再测它。"""
    if path is None or not url:
        return False
    return record_checks(path, cid, [(fingerprint(url), False, why, 0, at)])


def mark_ok(path, cid: str, url: str, at: float) -> bool:
    """这条线路真放起来了：上次检测说它连不上的，改成能连上（不然这个台还显示「暂时打不开」）。
    原来就不是「连不上」的不写文件。"""
    if path is None or not url:
        return False
    mark = fingerprint(url)
    if mark not in bad_in(entry_of(path, cid)):
        return False
    return record_checks(path, cid, [(mark, True, "", 0, at)])


# ---------------------------------------------------------------- 线路的质量：真实速度、画面（linequality.py 量）
# 「检测线路」只量连上要多久；能连上不等于跟得上实时（量过一条连得很快、实际只有 0.2–0.7 倍的 CCTV1），
# 公开列表里还有台名标错的、只放购物广告的、放旧录像的。这里记的是量到的：
# - pace：几倍实时（后台每天实拉最新的一段，和电脑上真放时量到的，remember_pace，和以前的各占一半）。
#   PACE_FRESH_S 内量的、不到 PACE_WEAK 的算慢（weak_in），排在能用的线路后面。
# - still：同一条线路隔了很久截的画面一模一样（循环的录像、静止的画面）；odd：最近 ODD_SEEN 次里有 ODD_MIN 次（现在是每次都）和同台
#   其他线路（它们彼此一样）的画面对不上（台名标错、冒充的）。这两种算可疑（suspect_in），排到最后，总后台标出来，
#   不自动删。

PACE_WEAK = 0.8
PACE_FRESH_S = 3 * 86400.0
PACE_CHANGE = 0.1
ODD_SEEN = 3
ODD_MIN = 3


def quality_in(entry) -> dict[str, dict]:
    found = entry.get("quality") if isinstance(entry, dict) else None
    return {k: v for k, v in found.items() if isinstance(v, dict)} if isinstance(found, dict) else {}


def _pace(value) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value < 100 else None


def weak_in(entry, now: float | None = None) -> set[str]:
    """量过（PACE_FRESH_S 内）跟不上实时的线路（指纹）。"""
    now = time.time() if now is None else now
    return {mark for mark, q in quality_in(entry).items()
            if _pace(q.get("pace")) is not None and q["pace"] < PACE_WEAK
            and isinstance(q.get("pace_at"), (int, float)) and now - q["pace_at"] < PACE_FRESH_S}


def odd_count(q: dict) -> int:
    seen = q.get("odd") if isinstance(q.get("odd"), list) else []
    return sum(1 for x in seen[-ODD_SEEN:] if x is True)


def suspect_in(entry) -> set[str]:
    """画面可疑的线路（指纹）：隔了很久画面一模一样，或者最近几次和同台其他线路对不上。"""
    return {mark for mark, q in quality_in(entry).items() if q.get("still") is True or odd_count(q) >= ODD_MIN}


def remember_pace(path, cid: str, url: str, pace: float, at: float) -> bool:
    """记下这条线路量到的速度（几倍实时），和 PACE_FRESH_S 内量过的各占一半；变得不多、量得不久的不写文件。"""
    pace = _pace(pace)
    if path is None or not url or pace is None:
        return False
    mark = fingerprint(url)
    old = quality_in(entry_of(path, cid)).get(mark, {})
    before = _pace(old.get("pace")) if isinstance(old.get("pace_at"), (int, float)) and at - old["pace_at"] < PACE_FRESH_S else None
    value = round(pace if before is None else (before + pace) / 2, 2)
    if before is not None and round(abs(value - before), 2) < PACE_CHANGE and at - old["pace_at"] < 6 * 3600:
        return False

    def change(data: dict) -> None:
        entry = data.setdefault(cid, {})
        quality = quality_in(entry)
        quality.setdefault(mark, {}).update(pace=value, pace_at=int(at))
        entry["quality"] = quality
    return update_meta(path, change)


def record_quality(path, found: dict) -> bool:
    """几个台的画面结论一次写进 meta：{台 id: {指纹: {要改的字段}}}（odd 给的是这一次的结果，接在后面、只留最近
    ODD_SEEN 次；None 表示这次判断不了，不记）。"""
    if path is None or not found:
        return False

    def change(data: dict) -> None:
        for cid, lines in found.items():
            entry = data.setdefault(cid, {})
            quality = quality_in(entry)
            for mark, fields in lines.items():
                q = quality.setdefault(mark, {})
                for key, value in fields.items():
                    if key == "odd":
                        if value is not None:
                            seen = q.get("odd") if isinstance(q.get("odd"), list) else []
                            q["odd"] = (seen + [bool(value)])[-ODD_SEEN:]
                    elif value is None:
                        q.pop(key, None)
                    else:
                        q[key] = value
            entry["quality"] = quality
    return update_meta(path, change)


# ---------------------------------------------------------------- 分片地址不标准的线路
# 有的 HLS 片段地址不带标准扩展名（/api/fhz、.php?id=…，转发站常这样）：ffmpeg 7.1 起默认不打开，报
# 「…allowed_segment_extensions…」。这样的线路记在 meta 的 lenient 里，打开时加 LENIENT_OPTION；没加
# -protocol_whitelist 的地方（电视服务、取封面）同时只许网络协议（LENIENT_ARGS）：ffmpeg 加这道检查是怕
# 列表把片段指到本机文件、或者当成别的格式去解，只许网络协议就读不到本机文件。别的线路照旧。

LENIENT_ERROR = "allowed_segment_extensions"
LENIENT_OPTION = ("-extension_picky", "0")
LENIENT_ARGS = ("-protocol_whitelist", "http,https,tls,tcp,crypto,httpproxy", *LENIENT_OPTION)


def lenient_in(entry) -> set[str]:
    found = entry.get("lenient") if isinstance(entry, dict) else None
    return {m for m in found if isinstance(m, str)} if isinstance(found, list) else set()


def is_lenient(path, url: str) -> bool:
    """这条地址（哪个台的都算）记过要放宽检查。没有频道表、读不了时 False。"""
    if path is None or not url:
        return False
    mark = fingerprint(url)
    try:
        return any(mark in lenient_in(entry) for entry in read_meta(path).values())
    except (OSError, ValueError):
        return False


def remember_lenient(path, cid: str, url: str) -> bool:
    """记下这条线路要放宽检查（已经记过的不写文件）。"""
    if path is None or not url:
        return False
    mark = fingerprint(url)

    def change(data: dict) -> None:
        entry = data.setdefault(cid, {})
        marks = lenient_in(entry)
        if mark not in marks:
            entry["lenient"] = sorted(marks | {mark})
    return update_meta(path, change)


# ---------------------------------------------------------------- 按顺序换线路

class RouteCycle:
    """一个会话里试线路的顺序和次数（电视服务用；电脑上看在 watch.py 里按 preferred 顺序走一遍）。

    - current：现在用哪条（地址, UA）；index 是它在频道表里是第几条（从 0 数）。
    - started()：这条线路出画面了。
    - failed()：这条线路起不来，或者断了。起播后放了 early_s 秒以上才断的：先在同一条上重试（返回 False，
      没换）；否则换下一条（返回 True）。一轮都试过了（rounds 轮）exhausted 为真，之后照样轮着给，
      由调用方的期限（reconnect.hold 的 deadline）收尾，不会无限地快速重试。"""

    def __init__(self, routes: list[tuple[str, str]], good: str = "", clock=time.monotonic,
                 early_s: float = EARLY_S, speeds: dict[str, int] | None = None,
                 bad: set[str] | frozenset = frozenset(), weak: set[str] | frozenset = frozenset()):
        self.routes = list(routes) or [("", "")]
        # bad：上次检测连不上的、画面可疑的线路（bad_in、suspect_in），排在最后；weak：实测跟不上实时的，排在倒数。
        self.order = preferred(self.routes, good, bad, speeds=speeds, weak=weak)
        self.clock = clock
        self.early_s = early_s
        self.pos = 0
        self.failures = 0
        self._since: float | None = None

    @property
    def index(self) -> int:
        return self.order[self.pos % len(self.order)]

    @property
    def current(self) -> tuple[str, str]:
        return self.routes[self.index]

    @property
    def count(self) -> int:
        return len(self.routes)

    @property
    def exhausted(self) -> bool:
        return self.failures >= len(self.routes)

    def started(self) -> None:
        self._since = self.clock()
        self.failures = 0

    def failed(self) -> bool:
        since, self._since = self._since, None
        if since is not None and self.clock() - since >= self.early_s:
            return False
        self.failures += 1
        if len(self.routes) > 1:
            self.pos += 1
            return True
        return False

    def extra_s(self, per_route_s: float, cap_s: float = 120.0) -> float:
        """多条线路时，重连的期限多给这么久（每多一条线路 per_route_s 秒，至多 cap_s）。"""
        return min(cap_s, per_route_s * (len(self.routes) - 1))


# ---------------------------------------------------------------- 出错了怎么说

REASONS = {
    "403": "线路被源站拒绝（403），可能已失效",
    "401": "线路要登录（401），可能已失效",
    "404": "源站找不到这个台（404）",
    "5xx": "源站出错（5xx），可能暂时不行",
    "timeout": "源站超时",
    "eof": "源站中途断开",
    "dns": "找不到源站（域名解析不了）",
    "refused": "源站拒绝连接",
    "tls": "和源站的加密连接失败",
    "invalid": "源站给的不是能放的直播流",
    "empty": "源站的列表里没有能放的片段",
    "other": "直播源打不开",
}
# 先认 HTTP 状态码，再认网络上的说法。ffmpeg 的原话例如「Server returned 403 Forbidden (access denied)」、
# 「Error reading HTTP response: Operation timed out」、「Error reading HTTP response: End of file」。
_PATTERNS = (
    ("403", re.compile(r"\b403\b|forbidden|access denied", re.I)),
    ("401", re.compile(r"\b401\b|unauthorized", re.I)),
    ("404", re.compile(r"\b40[47]\b|\b410\b|not found", re.I)),
    ("5xx", re.compile(r"\b5\d\d\b|\b5xx\b|server error", re.I)),
    ("dns", re.compile(r"resolve|name or service not known|nodename nor servname|getaddrinfo|"
                       r"name resolution|no address associated", re.I)),
    ("tls", re.compile(r"\bssl\b|\btls\b|certificate|handshake", re.I)),
    ("refused", re.compile(r"connection refused|refused", re.I)),
    ("timeout", re.compile(r"timed out|timeout|没有出片段", re.I)),
    ("eof", re.compile(r"end of file|ends prematurely|connection reset|reset by peer|incompleteread|incomplete read|"
                       r"broken pipe|中途断开|\beof\b", re.I)),
    ("invalid", re.compile(r"invalid data|could not find codec|no streams|没有画面也没有声音|not a playlist", re.I)),
    ("empty", re.compile(r"no segments|没有片段", re.I)),
)
_URL = re.compile(r"[a-z][a-z0-9+.-]*://\S+", re.I)


def redact(text: str) -> str:
    """日志里的原话：地址换成 <地址>（地址里常有会话参数、令牌）。"""
    return _URL.sub("<地址>", str(text or "")).strip()[:300]


def classify(text: str) -> str:
    text = redact(text)
    for code, pattern in _PATTERNS:
        if pattern.search(text):
            return code
    return "other"


def telling(lines) -> str:
    """几行原话里最说明问题的一行（按上面的先后：状态码先于网络的说法）；都认不出时空串。"""
    lines = [redact(line) for line in lines if line]
    for _code, pattern in _PATTERNS:
        for line in lines:
            if pattern.search(line):
                return line
    return ""


def friendly(text: str) -> str:
    """ffmpeg / HTTP 的原话 → 给人看的一句中文（不带原话）。"""
    return REASONS[classify(text)]


ALL_FAILED = "这个台暂时打不开"


def all_failed(last: str, count: int, checking: bool = False) -> str:
    """所有线路都不行时说的话，短：原因在日志里。checking：后台在自动检测线路（server/relay/linecheck.py），
    不用让人去总后台点「检测」。"""
    if checking:
        return f"{ALL_FAILED}，已经在自动检测"
    if count <= 1:
        return f"{ALL_FAILED}：{friendly(last)}"
    return f"{ALL_FAILED}：{count} 条线路都不通"


def switching(name: str, failed: int, reason: str, following: int) -> str:
    """「CCTV2 线路 1 失效（源站超时），正在换线路 2…」：线路按频道表里的顺序从 1 数。"""
    return f"{name} 线路 {failed} 失效（{friendly(reason)}），正在换线路 {following}…"
