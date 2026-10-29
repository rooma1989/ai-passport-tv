"""直播源订阅（自建 / 电脑客户端）：记住导入过的播放列表地址，定时重新取，把里面的新线路并进频道表里已有的台。

网上的直播源列表常常更新（换地址、加线路），单条线路随时会失效；以前只能手动重新导入。订阅了以后：
- 每 EVERY_S 秒（失败了 RETRY_S 秒后）重新取一次（playlist.read，和「导入」读地址一样，至多 2 MB、45 秒）。
- 列表里的台按名字（channel_key：不分大小写、空格横线，「CCTV-1 综合」「CCTV1 HD」算同一个，4K 的另算）或
  tvg-id 对上频道表里的台：这个台还没有的地址先补进空着的备用线路（routes.BACKUPS_MAX 条），满了只换掉检测
  连不上的备用线路（routes.bad_in）；主线路和能用的线路从不动。新线路排在后面，后台自检（linecheck.py）测过、
  真放起来快的才往前排（routes.preferred）。
- 对上的台空着的 tvg-id、tvg-name、台标用列表里的补上（channeltable.fill_meta），有的从不换；分组不动。
- 频道表里没有的台不自动加（频道表会越撑越大）：记下有几个、叫什么，总后台「导入」里点一下列出来勾选。
- 列表文件头推荐的节目单（x-tvg-url）记成「推荐的节目单来源」（epg.suggest），不会自己打开。

订阅存在频道表旁边的 <频道表>.subs.json：地址只在本机、只给管理员看，日志只写订阅的名字，从不写地址
（列表地址里常带口令）。至多 SUBS_MAX 个。云端的中转没有这个功能（和「导入」一样）。
"""

from __future__ import annotations

import json
import os
import re
import secrets
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Callable

from .. import routes
from . import channeltable, epg, playlist

SUBS_MAX = 8
EVERY_S = 12 * 3600.0
RETRY_S = 3600.0
TICK_S = 600.0
START_DELAY_S = 120.0
NAME_MAX = 40
# 记下的「频道表里没有的台」至多这么多个名字（个数照实记）。
NEW_NAMES_MAX = 30
SUFFIX = ".subs.json"
_ID = re.compile(r"^[0-9a-f]{8}$")
_LOCK = threading.Lock()


def subs_path(table: Path) -> Path:
    table = Path(table)
    return table.with_name(table.name + SUFFIX)


def channel_key(name: str) -> str:
    """对台用的名字：playlist.name_key 再去掉括号里的说明和「高清」「HD」这类后缀；CCTV 只看台号
    （「CCTV-1 综合」→ cctv1，「CCTV5+ 体育赛事」→ cctv5+），带 4K 的另算一个台。"""
    key = playlist.name_key(name)
    key = re.sub(r"[(（\[【][^)）\]】]*[)）\]】]", "", key)
    four = "4k" in key
    found = re.match(r"cctv(5\+|\d{1,2})", key)
    if found:
        number = found.group(1)
        # CCTV4 的欧洲、美洲版和 CCTV8K 是别的台（和节目单的 epg.key_of 一样各算各的）。
        region = next((r for r in ("欧洲", "美洲", "亚洲") if r in key), "")
        uhd = "8k" if number == "8" and key[found.end():].startswith("k") else "4k" if four else ""
        return "cctv" + (number if number.endswith("+") else str(int(number))) + region + uhd
    while True:
        trimmed = re.sub(r"(超高清|高清|超清|标清|uhd|fhd|hd|频道)$", "", key)
        if trimmed == key or not trimmed:
            return key
        key = trimmed


def label_of(url: str) -> str:
    """没起名字的订阅：用地址的主机名（只给管理员看，不进日志）。"""
    host = urllib.parse.urlsplit(url).hostname or "播放列表"
    return host[:NAME_MAX]


def merge_lines(meta: dict, rows: list[dict], entries: list[dict]) -> dict:
    """把读出来的台（playlist.read 的 entries）并进 meta（原地改，routes.update_meta 里调）。
    回答 {channels: 对上了几个台, added: 补了几条, replaced: 换掉了几条坏的, filled: 补上台标或节目单编号的台数,
    new: 频道表里没有的台的个数, new_names: 前 NEW_NAMES_MAX 个的名字}。"""
    by_key: dict[str, dict] = {}
    by_tvg: dict[str, dict] = {}
    for row in rows:
        by_key.setdefault(channel_key(row["name"]), row)
        tvg = (meta.get(row["id"]) or {}).get("tvg_id")
        if isinstance(tvg, str) and tvg:
            by_tvg.setdefault(tvg.casefold(), row)
    wanted: dict[str, list[dict]] = {}
    new_names: list[str] = []
    new_count = 0
    seen_new: set[str] = set()
    filled: set[str] = set()
    for entry in entries:
        tvg = entry.get("tvg_id") if isinstance(entry.get("tvg_id"), str) else ""
        row = (by_tvg.get(tvg.casefold()) if tvg else None) or by_key.get(channel_key(entry["name"]))
        if row is None:
            key = channel_key(entry["name"])
            if key not in seen_new:
                seen_new.add(key)
                new_count += 1
                if len(new_names) < NEW_NAMES_MAX:
                    new_names.append(entry["name"])
            continue
        lines = [{"url": entry["url"], "ua": entry.get("agent", "")}] + list(entry.get("routes") or [])
        wanted.setdefault(row["id"], []).extend(lines)
        # 台标、节目单编号：这个台空着的才补，先对上的那一条先补。
        if channeltable.clean_meta(entry):
            target = meta.setdefault(row["id"], {})
            if channeltable.fill_meta(target, entry):
                filled.add(row["id"])
    added = replaced = 0
    for row in rows:
        if row["id"] not in wanted:
            continue
        entry = meta.setdefault(row["id"], {})
        backups = routes.clean_routes(entry.get("routes"), row["url"])
        have = {row["url"]} | {b["url"] for b in backups}
        fresh = []
        for line in wanted[row["id"]]:
            clean = routes.clean_route(line)
            if clean is not None and clean["url"] not in have:
                have.add(clean["url"])
                fresh.append(clean)
        if not fresh:
            continue
        while fresh and len(backups) < routes.BACKUPS_MAX:
            backups.append(fresh.pop(0))
            added += 1
        bad = routes.bad_in(entry)
        for i, backup in enumerate(backups):
            if not fresh:
                break
            if routes.fingerprint(backup["url"]) in bad:
                backups[i] = fresh.pop(0)
                replaced += 1
        entry["routes"] = backups
    return {"channels": len(wanted), "added": added, "replaced": replaced, "filled": len(filled), "new": new_count,
            "new_names": new_names}


class Subscriptions:
    """订阅的存取、手动和定时刷新。table() 给频道表的路径（没有时什么都不做）。"""

    def __init__(self, table: Callable[[], Path | None], read=playlist.read, clock=time.time,
                 log: Callable[[str], None] = print, suggest: Callable[[list, str], object] | None = None):
        self.table = table
        self.read = read
        self.clock = clock
        self.log = log
        # 列表推荐的节目单地址交给谁记（epg.suggest；没有时不记）。
        self.suggest = suggest
        self._busy: set[str] = set()

    # ---- 文件 ----

    def _load(self) -> list[dict]:
        table = self.table()
        if table is None:
            return []
        try:
            data = json.loads(subs_path(table).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        found = data.get("subs") if isinstance(data, dict) else None
        return [s for s in found if isinstance(s, dict) and _ID.match(str(s.get("id", "")))
                and isinstance(s.get("url"), str)] if isinstance(found, list) else []

    def _save(self, subs: list[dict]) -> None:
        target = subs_path(self.table())
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_text(json.dumps({"subs": subs}, ensure_ascii=False, indent=1), encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, target)

    def _update(self, sid: str, change: Callable[[dict], None]) -> dict | None:
        with _LOCK:
            subs = self._load()
            found = next((s for s in subs if s["id"] == sid), None)
            if found is None:
                return None
            change(found)
            self._save(subs)
            return dict(found)

    # ---- 给总后台 ----

    def view(self) -> dict:
        subs = self._load()
        return {"ok": True, "max": SUBS_MAX, "every_h": int(EVERY_S // 3600), "subs": [
            {k: s.get(k) for k in ("id", "name", "url", "added_at", "last_at", "ok", "error", "result")}
            | {"busy": s["id"] in self._busy} for s in subs]}

    def add(self, url: str, name: str = "") -> dict:
        if self.table() is None:
            return {"ok": False, "error": "没有频道表"}
        url = url.strip() if isinstance(url, str) else ""
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or len(url) > playlist.URL_FIELD_MAX \
                or any(c in url for c in " \n\r\t"):
            return {"ok": False, "error": "地址要以 http:// 或 https:// 开头"}
        name = " ".join(str(name or "").split())[:NAME_MAX] or label_of(url)
        with _LOCK:
            subs = self._load()
            if any(s["url"] == url for s in subs):
                return {"ok": False, "error": "已经订阅过这个地址了"}
            if len(subs) >= SUBS_MAX:
                return {"ok": False, "error": f"最多订阅 {SUBS_MAX} 个播放列表"}
            sub = {"id": secrets.token_hex(4), "name": name, "url": url, "added_at": int(self.clock()),
                   "last_at": 0, "ok": None, "error": "", "result": None}
            subs.append(sub)
            self._save(subs)
        return self.refresh(sub["id"])

    def remove(self, sid: str) -> dict:
        with _LOCK:
            subs = self._load()
            kept = [s for s in subs if s["id"] != sid]
            if len(kept) == len(subs):
                return {"ok": False, "error": "没有这个订阅"}
            self._save(kept)
        return {"ok": True}

    def refresh(self, sid: str) -> dict:
        """现在取一次、并进频道表。同一个订阅同时只取一次。"""
        sub = next((s for s in self._load() if s["id"] == sid), None)
        table = self.table()
        if sub is None or table is None:
            return {"ok": False, "error": "没有这个订阅"}
        with _LOCK:
            if sid in self._busy:
                return {"ok": False, "error": "正在更新，稍等"}
            self._busy.add(sid)
        try:
            answer = self.read({"url": sub["url"]})
            now = int(self.clock())
            if not answer.get("ok"):
                error = str(answer.get("error") or "取不到")[:120]
                self._update(sid, lambda s: s.update(last_at=now, ok=False, error=error))
                self.log(f"直播源订阅「{sub['name']}」没更新：{error.split('：')[0]}")
                return {"ok": False, "error": error}
            rows = channeltable.read(table)["rows"]
            result: dict = {}
            routes.update_meta(table, lambda meta: result.update(merge_lines(meta, rows, answer["entries"])))
            result["total"] = answer.get("total", len(answer["entries"]))
            if self.suggest is not None and answer.get("epg_urls"):
                self.suggest(answer["epg_urls"], sub["name"])
            self._update(sid, lambda s: s.update(last_at=now, ok=True, error="", result=result))
            if result["added"] or result["replaced"] or result["filled"]:
                self.log(f"直播源订阅「{sub['name']}」：{result['channels']} 个台对上，补了 {result['added']} 条线路，"
                         f"换掉 {result['replaced']} 条坏的，{result['filled']} 个台补上台标或节目单编号")
            return {"ok": True, "result": result}
        finally:
            with _LOCK:
                self._busy.discard(sid)

    # ---- 定时 ----

    def due(self) -> list[str]:
        now = self.clock()
        return [s["id"] for s in self._load()
                if now - (s.get("last_at") or 0) >= (EVERY_S if s.get("ok") is not False else RETRY_S)]

    def run_forever(self, sleep=time.sleep) -> None:
        sleep(START_DELAY_S)
        while True:
            for sid in self.due():
                try:
                    self.refresh(sid)
                except Exception as error:  # noqa: BLE001 —— 后台线程不能悄悄死
                    self.log(f"直播源订阅出错：{type(error).__name__}")
            sleep(TICK_S)


def attach(relay, start: bool = True) -> Subscriptions | None:
    """自建 / 电脑客户端：挂在 relay.subscriptions 上，开定时刷新。云端不建。"""
    if relay.selfhost is None or relay.main_channels is None:
        return None
    subs = Subscriptions(lambda: relay.main_channels, log=lambda message: relay.log(message),
                         suggest=lambda urls, origin: epg.suggest(relay, urls, origin))
    relay.subscriptions = subs
    if start:
        threading.Thread(target=subs.run_forever, name="subscriptions", daemon=True).start()
    return subs
