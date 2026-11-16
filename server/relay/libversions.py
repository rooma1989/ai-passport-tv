"""片库剧集的版本（library.py 用）：一部剧的视频文件按「版本」分组，先选版本再选集，上一集 / 下一集只在同一个版本、
同一季里按真实集号走。

为什么：同一部剧常有好几套文件（同一个网盘里的两个文件夹、4K 和 1080p、两个来源）。以前播放队列把这部剧的全部
文件按季、集、路径排成一串，序号当集号、文件数当总集数（「第 5 集 / 44」），下一集先放同一集的另一个文件。

- 版本的键（ver）：自动的是 "a" + 来源 id 和「版本文件夹」（视频所在的文件夹，去掉末尾的季文件夹「Season 2」「第二季」，
  一个版本可以跨好几季）的短哈希，重新扫描、重启都不变；文件夹改了名就是另一个键（页面提示重新选）。
- 手动的（管理员调整）存在 library-versions.json：{"items": {条目 id: 版本键}, "names": {版本键: 名字}}。条目可以指到
  已有的版本（自动的也行），或新建一个 "m" 开头的版本；names 也能给自动的版本起名。重新扫描不动它（条目 id 只跟
  来源和路径有关）。
- 一个版本里同一季同一集有不止一个文件：不挑一个悄悄丢掉别的，那一集标「待确认」（conflicts），页面列出每个候选，
  可以挑着放，管理员把多出来的指到别的版本。
- 认不出集号的文件留在它的版本里（unknown），能手动放，不算集数，不进上一集 / 下一集。

只用标准库；这里的函数都是纯的（Store 除外），tests/test_relay_libversions.py 单独测。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import threading
import time
from pathlib import Path

KEY = re.compile(r"^[am][0-9a-f]{10}$")
NAME_MAX = 40
ITEMS_MAX = 2000
_ID = re.compile(r"^[0-9a-f]{16}$")


def auto_key(source: str, folder: str) -> str:
    return "a" + hashlib.sha1(f"{source}\0{folder}".encode("utf-8")).hexdigest()[:10]


def season_of(row: dict) -> int:
    """不分季的算第 1 季；季 0 是特别篇。"""
    season = row.get("season")
    return 1 if season is None else int(season)


def res_label(height) -> str:
    """画面的大小：2160 → 4K（和 console.js 的 libRes 一样）。"""
    h = int(height or 0)
    if not h:
        return ""
    return "4K" if h >= 2000 else "2K" if h >= 1400 else "1080p" if h >= 1000 else "720p" if h >= 700 else f"{h}p"


def episode_label(season: int, episode, seasons: int) -> str:
    """一集怎么叫：「第 3 集」；有好几季时「第 2 季 第 3 集」；特别篇「特别篇 第 1 集」。认不出集号的空。"""
    if episode is None:
        return ""
    if season == 0:
        return f"特别篇 第 {episode} 集"
    return f"第 {season} 季 第 {episode} 集" if seasons > 1 else f"第 {episode} 集"


def preference(row: dict) -> tuple:
    """同一集好几个文件时默认放哪个：能放的、画面大的在前，再按文件夹、文件名（稳定）。"""
    return (not row.get("playable", True), -(row.get("height") or 0), row.get("folder") or "", row.get("file") or "",
            row.get("id") or "")


def order(row: dict) -> tuple:
    """一个版本里的排法：季（特别篇在前）、集号；认不出集号的放最后。"""
    episode = row.get("episode")
    return (episode is None, season_of(row), episode or 0) + preference(row)


def _labels(groups: list[dict]) -> None:
    """自动版本的名字：文件夹的最后一段（「蒸蒸日上/4K」→「4K」，来源的根是「根目录」）；重名的往上多带几段，
    还重名（两个来源里一样的路径）再带来源的名字。手动起了名字的不动。原地写 label。"""
    auto = [g for g in groups if not g.get("label")]
    depth = {id(g): 1 for g in auto}

    def name(g: dict) -> str:
        parts = [p for p in (g.get("folder") or "").split("/") if p]
        if not parts:
            return "根目录"
        return "/".join(parts[-depth[id(g)]:])

    for _ in range(8):
        seen: dict[str, list[dict]] = {}
        for g in auto:
            seen.setdefault(name(g), []).append(g)
        clash = [g for same in seen.values() if len(same) > 1 for g in same]
        grown = False
        for g in clash:
            if depth[id(g)] < len([p for p in (g.get("folder") or "").split("/") if p]):
                depth[id(g)] += 1
                grown = True
        if not grown:
            break
    seen = {}
    for g in auto:
        seen.setdefault(name(g), []).append(g)
    for label, same in seen.items():
        for g in same:
            g["label"] = f"{g.get('source') or '来源'} · {label}" if len(same) > 1 and g.get("source") else label
    # 起了一样名字的（手动的和自动的撞了）：后面的加上序号，不让两个一样的名字并排。
    taken: dict[str, int] = {}
    for g in groups:
        n = taken.get(g["label"], 0) + 1
        taken[g["label"]] = n
        if n > 1:
            g["label"] = f"{g['label']}（{n}）"


def summarize(rows: list[dict], names: dict[str, str] | None = None) -> list[dict]:
    """一部剧的各个文件（卡片：id、ver、vfolder、source、season、episode、playable、height、ext、file、folder）→ 版本：
    [{key, label, detail, manual, count, files, seasons, conflicts, unknown, ids}]，集多的在前，一样多的按名字。
    count 是认出集号的不同 (季, 集) 有几个；conflicts 是同一季同一集不止一个文件的 [季, 集]；ids 按 order 排好。"""
    names = names or {}
    groups: dict[str, dict] = {}
    for row in rows:
        key = row.get("ver") or ""
        g = groups.get(key)
        if g is None:
            g = groups[key] = {"key": key, "folder": row.get("vfolder") or "", "source": row.get("source") or "",
                               "label": names.get(key) or "", "manual": key.startswith("m"), "rows": []}
        g["rows"].append(row)
    out = list(groups.values())
    _labels(out)
    heights: list[set] = []
    exts: list[set] = []
    for g in out:
        rows_ = sorted(g["rows"], key=order)
        numbered: dict[tuple, int] = {}
        for r in rows_:
            if r.get("episode") is not None:
                k = (season_of(r), int(r["episode"]))
                numbered[k] = numbered.get(k, 0) + 1
        g.update(ids=[r["id"] for r in rows_], count=len(numbered), files=len(rows_),
                 seasons=sorted({s for s, _ in numbered}),
                 conflicts=[[s, e] for (s, e), n in sorted(numbered.items()) if n > 1],
                 unknown=sum(1 for r in rows_ if r.get("episode") is None))
        heights.append({res_label(r.get("height")) for r in rows_ if r.get("height")})
        exts.append({(r.get("ext") or "").upper() for r in rows_ if r.get("ext")})
    # 补充的信息只写分得开的：几个版本的画面（或格式）都一样就不写；探测过才知道画面，不知道的不猜。
    for i, g in enumerate(out):
        bits = []
        if len(out) > 1 and len({frozenset(h) for h in heights}) > 1 and heights[i]:
            bits.append("/".join(sorted(heights[i], key=lambda x: -_res_rank(x))))
        if len(out) > 1 and len({frozenset(e) for e in exts}) > 1 and exts[i]:
            bits.append("/".join(sorted(exts[i])))
        g["detail"] = " · ".join(bits)
        del g["rows"], g["folder"]
    out.sort(key=lambda g: (-g["count"], g["label"].casefold(), g["key"]))
    return out


def _res_rank(label: str) -> int:
    return {"4K": 2160, "2K": 1440, "1080p": 1080, "720p": 720}.get(label) or int(label.rstrip("p") or 0)


def queue(rows: list[dict], item: dict) -> list[dict]:
    """item 所在的版本、同一季的播放队列：认出集号的每集一个文件（item 自己那一集就是 item；别的集有几个候选时先挑
    和 item 在同一个文件夹里的，再按 preference），按集号排；后面是这个版本里认不出集号的（同一季或不分季的）。
    rows 是这部剧的全部文件（带 ver），item 也在里面。"""
    ver = item.get("ver")
    season = season_of(item)
    folder = item.get("folder")
    mine = [r for r in rows if r.get("ver") == ver]
    numbered: dict[int, list[dict]] = {}
    unknown = []
    for r in mine:
        if r.get("episode") is None:
            if r.get("season") is None or season_of(r) == season:
                unknown.append(r)
            continue
        if season_of(r) == season:
            numbered.setdefault(int(r["episode"]), []).append(r)
    out = []
    for episode in sorted(numbered):
        options = numbered[episode]
        pick = next((r for r in options if r["id"] == item["id"]), None) \
            or min(options, key=lambda r: (r.get("folder") != folder,) + preference(r))
        out.append(dict(pick, choices=len(options)))
    if item.get("episode") is None and not any(r["id"] == item["id"] for r in unknown):
        unknown.append(item)
    out += [dict(r, choices=1) for r in sorted(unknown, key=preference)]
    return out


def step(q: list[dict], item: dict, direction: int) -> dict | None:
    """队列里 item 的下一集（1）/ 上一集（-1）：只在认出集号、能放的里面走；item 认不出集号、或者到头了时 None。"""
    if item.get("episode") is None:
        return None
    eps = [r for r in q if r.get("episode") is not None]
    here = next((i for i, r in enumerate(eps) if r["id"] == item["id"]), None)
    if here is None:
        here = next((i for i, r in enumerate(eps) if int(r["episode"]) == int(item["episode"])), None)
    if here is None:
        return None
    i = here + direction
    while 0 <= i < len(eps):
        if eps[i].get("playable", True):
            return eps[i]
        i += direction
    return None


def gap(here, there) -> str:
    """从第 here 集到第 there 集中间缺了几集时的提示（「第 4 集缺失，下一集为第 5 集」）；不缺时空。"""
    if here is None or there is None:
        return ""
    lo, hi = sorted((int(here), int(there)))
    if hi - lo <= 1:
        return ""
    missing = f"第 {lo + 1} 集" if hi - lo == 2 else f"第 {lo + 1}–{hi - 1} 集"
    word = "下一集" if int(there) > int(here) else "上一集"
    return f"{missing}缺失，{word}为第 {there} 集"


class Store:
    """library-versions.json：管理员手动调整的版本。整份换掉写（600）。"""

    def __init__(self, path: Path, clock=time.time):
        self.path = Path(path)
        self.clock = clock
        self._lock = threading.Lock()

    def load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        items = data.get("items") if isinstance(data, dict) else None
        names = data.get("names") if isinstance(data, dict) else None
        return {"items": {k: v for k, v in (items or {}).items() if _ID.match(str(k)) and isinstance(v, str)
                          and KEY.match(v)} if isinstance(items, dict) else {},
                "names": {k: v for k, v in (names or {}).items() if KEY.match(str(k)) and isinstance(v, str)
                          and v.strip()} if isinstance(names, dict) else {}}

    def _save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)

    def change(self, body, known: set[str]) -> dict:
        """POST /api/admin/library/versions：
        - {items: [{id, v}], name?}：v 是已有的版本键、"auto"（回到自动识别的版本）或 "new"（新建一个版本，名字是 name）；
        - {rename: 版本键, name}：给一个版本起名字（name 空是去掉，回到按文件夹起的名字）。
        known 是片库里现在有的条目 id。一个请求要么全记下、要么一个不记。"""
        if not isinstance(body, dict):
            return {"ok": False, "error": "请求格式不对"}
        with self._lock:
            data = self.load()
            if "rename" in body:
                key = body.get("rename")
                name = " ".join(str(body.get("name") or "").split()) if isinstance(body.get("name"), (str, type(None))) else None
                if not isinstance(key, str) or not KEY.match(key) or name is None:
                    return {"ok": False, "error": "请求格式不对"}
                if len(name) > NAME_MAX:
                    return {"ok": False, "error": f"名字太长了（至多 {NAME_MAX} 个字）"}
                if name:
                    data["names"][key] = name
                else:
                    data["names"].pop(key, None)
                self._save(data)
                return {"ok": True, "key": key, "name": name}
            rows = body.get("items")
            if not isinstance(rows, list) or not rows or len(rows) > ITEMS_MAX:
                return {"ok": False, "error": "没有选视频"}
            wanted: dict[str, str] = {}
            for row in rows:
                iid = row.get("id") if isinstance(row, dict) else None
                v = row.get("v") if isinstance(row, dict) else None
                if not isinstance(iid, str) or not _ID.match(iid) or not isinstance(v, str) \
                        or not (v in ("auto", "new") or KEY.match(v)) or iid in wanted:
                    return {"ok": False, "error": "请求格式不对"}
                wanted[iid] = v
            if any(iid not in known for iid in wanted):
                return {"ok": False, "error": "有视频已经不在片库里了，刷新一下再试"}
            new_key = ""
            if "new" in wanted.values():
                name = " ".join(str(body.get("name") or "").split()) if isinstance(body.get("name"), str) else ""
                if not name:
                    return {"ok": False, "error": "给新版本写一个名字"}
                if len(name) > NAME_MAX:
                    return {"ok": False, "error": f"名字太长了（至多 {NAME_MAX} 个字）"}
                new_key = "m" + secrets.token_hex(5)
                data["names"][new_key] = name
            for iid, v in wanted.items():
                if v == "auto":
                    data["items"].pop(iid, None)
                else:
                    data["items"][iid] = new_key if v == "new" else v
            # 没有条目再指着的手动版本，名字也不留。
            used = set(data["items"].values())
            data["names"] = {k: n for k, n in data["names"].items() if not k.startswith("m") or k in used}
            self._save(data)
            return {"ok": True, "count": len(wanted), "key": new_key}
