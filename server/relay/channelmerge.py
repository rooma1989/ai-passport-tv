"""总后台频道表的「合并重复的台」：同一个台在表里有两份（比如两个「CCTV5」），搜索和列表里就出现两次。

- 找（duplicates）：台名按订阅并线路时的规整（subscriptions.channel_key：不分大小写、空格横线，「CCTV-1 综合」
  「CCTV1 HD」算同一个，4K 台、CCTV4 欧洲 / 美洲版另算）相同的，或者节目单编号（tvg-id，不分大小写）相同的，
  连成一组。只是 tvg-id 相同、名字不一样的组默认不勾（列表的 tvg-id 常常写错），名字相同的默认勾上。
- 每组默认留下的（keep）：能用的线路（检测能连上、没量出跟不上实时或画面可疑）最多的，再看能连上的、线路总数、
  有没有台标、有没有节目单编号，都一样时留表里靠前的。管理员可以换。
- 合并（plan）：留下的台的线路和别的台的线路去重后按质量排（能用的 > 能连上但质量可疑的 > 没测过的 > 连不上的，
  同一档里留下的台原来的主线路在前，再按真放起来的用时），最多 routes.ROUTES_MAX 条，第一条当主线路；
  留下的台空着的台标、节目单编号、节目单里的台名用别的台的补上（channeltable.fill_meta，有的从不换）；
  别的台从表里删掉。整个走平常的保存（admin.save_channels：先备份 channels.txt 和旁边的 meta，再用电视服务的
  解析器查一遍），从不自动合并。
- 保存以后（carry）：并过来的线路的检测记录、量到的质量、真放起来的用时、要放宽检查的标记跟着线路搬到留下的台
  （merge_meta 只认这个台原来的记录）；总后台的分组、每个人调的分组、「常看」、节目单的手动对台由 admin.py 搬。
"""

from __future__ import annotations

from .. import routes
from . import channeltable, subscriptions

# 一个页面一次最多发这么多组（表最多 channel_groups.TABLE_MAX 个台，重复的组远少于此）。
GROUPS_MAX = 400
_SLOW_MS = 10 ** 9


def _lines(row: dict, entry: dict) -> list[dict]:
    return [{"url": row["url"], "ua": row["agent"]}] + [
        {"url": b["url"], "ua": b.get("ua", "")} for b in routes.clean_routes(entry.get("routes"), row["url"])]


def _grade(entry: dict, url: str) -> tuple[int, int]:
    """(档次, 用时)：0 能用、1 能连上但质量可疑、2 没测过、3 连不上；用时是真放起来的（没有时很大）。"""
    mark = routes.fingerprint(url)
    checks = entry.get("checks") if isinstance(entry.get("checks"), dict) else {}
    found = checks.get(mark)
    ms = routes.speeds_of(entry).get(mark) or _SLOW_MS
    if not isinstance(found, dict) or not isinstance(found.get("ok"), bool):
        return 2, ms
    if not found["ok"]:
        return 3, ms
    if mark in routes.weak_in(entry) or mark in routes.suspect_in(entry):
        return 1, ms
    return 0, ms


def _profile(row: dict, entry: dict, position: int) -> dict:
    lines = _lines(row, entry)
    grades = [_grade(entry, line["url"])[0] for line in lines]
    own = channeltable.clean_meta(entry)
    return {"id": row["id"], "name": row["name"], "n": position + 1, "lines": len(lines),
            "good": grades.count(0), "ok": grades.count(0) + grades.count(1), "bad": grades.count(3),
            "logo": bool(own.get("logo")), "tvg_id": own.get("tvg_id", "")}


def _rank(item: dict) -> tuple:
    return (-item["good"], -item["ok"], -item["lines"], not item["logo"], not item["tvg_id"], item["n"])


def duplicates(path, rows: list[dict], meta: dict | None = None) -> list[dict]:
    """[{key, why: "name" / "tvg" / "both", tick, keep, channels: [{id, name, n, lines, good, ok, bad, logo, tvg_id}]}]：
    channels 按默认留下的在前排；只看表和 meta，不改文件。"""
    meta = routes.read_meta(path) if meta is None else meta
    parent = {row["id"]: row["id"] for row in rows}

    def find(cid: str) -> str:
        while parent[cid] != cid:
            parent[cid] = parent[parent[cid]]
            cid = parent[cid]
        return cid

    by_name: dict[str, str] = {}
    by_tvg: dict[str, str] = {}
    names: dict[str, str] = {}
    for row in rows:
        key = subscriptions.channel_key(row["name"])
        names[row["id"]] = key
        if key:
            first = by_name.setdefault(key, row["id"])
            parent[find(row["id"])] = find(first)
        tvg = channeltable.clean_meta(meta.get(row["id"]) or {}).get("tvg_id", "").casefold()
        if tvg:
            first = by_tvg.setdefault(tvg, row["id"])
            parent[find(row["id"])] = find(first)
    members: dict[str, list[int]] = {}
    for n, row in enumerate(rows):
        members.setdefault(find(row["id"]), []).append(n)
    found = []
    for indexes in members.values():
        if len(indexes) < 2:
            continue
        items = sorted((_profile(rows[n], meta.get(rows[n]["id"]) or {}, n) for n in indexes), key=_rank)
        same_name = len({names[item["id"]] for item in items}) == 1
        tvgs = {item["tvg_id"].casefold() for item in items}
        same_tvg = len(tvgs) == 1 and "" not in tvgs
        found.append({"key": rows[indexes[0]]["name"], "why": "both" if same_name and same_tvg else
                      "name" if same_name else "tvg", "tick": same_name, "keep": items[0]["id"], "channels": items})
    found.sort(key=lambda group: min(item["n"] for item in group["channels"]))
    return found[:GROUPS_MAX]


def plan(path, rows: list[dict], picks, meta: dict | None = None) -> dict:
    """picks: [{keep: 台 id, drop: [台 id…]}]（每组的 drop 要是 duplicates 找到的同一组里的）→
    {edits: 给 admin.save_channels 的 rows, merged: [{keep, name, drop: [{id, name}], lines, lost, filled}],
    carry: {留下的 id: [删掉的 id…]}}。格式不对、对不上时 ValueError（中文，给人看）。"""
    meta = routes.read_meta(path) if meta is None else meta
    if not isinstance(picks, list) or not picks:
        raise ValueError("没有选要合并的台")
    by_id = {row["id"]: row for row in rows}
    group_of = {}
    for n, group in enumerate(duplicates(path, rows, meta)):
        for item in group["channels"]:
            group_of[item["id"]] = n
    used: set[str] = set()
    edits_of: dict[str, dict] = {}
    dropped: set[str] = set()
    merged, carry = [], {}
    for pick in picks:
        keep = pick.get("keep") if isinstance(pick, dict) else None
        drop = pick.get("drop") if isinstance(pick, dict) else None
        if not isinstance(keep, str) or not isinstance(drop, list) or not drop or \
                not all(isinstance(d, str) for d in drop):
            raise ValueError("请求格式不对")
        for cid in [keep] + drop:
            if cid not in by_id:
                raise ValueError(f"没有 {cid} 这个台（可能别人刚改过频道表，请刷新）")
            if cid in used:
                raise ValueError(f"{by_id[cid]['name']}（{cid}）出现了两次")
            used.add(cid)
            if group_of.get(cid) is None or group_of[cid] != group_of.get(keep):
                raise ValueError(f"「{by_id[cid]['name']}」和「{by_id[keep]['name']}」不像是同一个台（请刷新）")
        row = by_id[keep]
        entry = meta.get(keep) or {}
        members = [meta.get(cid) or {} for cid in [keep] + drop]
        candidates, seen = [], set()
        for cid, source in zip([keep] + drop, members):
            for line in _lines(by_id[cid], source):
                if line["url"] in seen:
                    continue
                seen.add(line["url"])
                # 同一个地址在几个台里都有时，用记录最好的那个台的（留下的台没测过、别的台测过能用的，算能用）。
                grade, ms = min(_grade(other, line["url"]) for other in members)
                candidates.append(((grade, line["url"] != row["url"], ms, len(candidates)), line))
        candidates.sort(key=lambda pair: pair[0])
        lines = [line for _, line in candidates[:routes.ROUTES_MAX]]
        item = {"id": keep, "url": lines[0]["url"], "agent": lines[0]["ua"],
                "routes": [{"url": x["url"], "ua": x["ua"]} if x["ua"] else {"url": x["url"]} for x in lines[1:]]}
        own = channeltable.clean_meta(entry)
        filled = []
        for cid in drop:
            filled += channeltable.fill_meta(own, meta.get(cid) or {})
        for field in filled:
            item[field] = own[field]
        edits_of[keep] = item
        dropped.update(drop)
        carry[keep] = list(drop)
        merged.append({"keep": keep, "name": row["name"], "drop": [{"id": d, "name": by_id[d]["name"]} for d in drop],
                       "lines": len(lines), "lost": max(0, len(candidates) - len(lines)), "filled": filled})
    edits = [edits_of.get(row["id"], row["id"]) for row in rows if row["id"] not in dropped]
    return {"edits": edits, "merged": merged, "carry": carry}


def carry_meta(data: dict, old: dict, carry: dict[str, list[str]], primary: dict[str, str]) -> None:
    """保存以后（routes.update_meta 里调，原地改 data）：并过来的线路的检测记录（checks）、量到的质量（quality）、
    真放起来的用时（starts）、要放宽检查的标记（lenient）从删掉的台（old 是保存前的 meta）搬到留下的台；
    primary 是留下的台现在的主线路。只搬留下的台现在还有的线路，留下的台自己有的记录不换。"""
    for keep, drops in carry.items():
        entry = data.setdefault(keep, {})
        marks = {routes.fingerprint(primary.get(keep, ""))} | \
            {routes.fingerprint(b["url"]) for b in routes.clean_routes(entry.get("routes"))}
        for cid in drops:
            source = old.get(cid)
            if not isinstance(source, dict):
                continue
            for field, found in (("checks", source.get("checks")), ("quality", routes.quality_in(source))):
                if not isinstance(found, dict) or not isinstance(entry.get(field, {}), dict):
                    continue
                have = entry.setdefault(field, {})
                for mark, record in found.items():
                    if mark in marks and mark not in have and isinstance(record, dict):
                        have[mark] = record
                if not have:
                    entry.pop(field)
            for kind in routes.START_KINDS:
                found = routes.starts_in(source, kind)
                if not found or not isinstance(entry.get("starts", {}), dict):
                    continue
                starts = entry.setdefault("starts", {})
                have = starts.get(kind) if isinstance(starts.get(kind), dict) else {}
                have.update({mark: ms for mark, ms in found.items() if mark in marks and mark not in have})
                if have:
                    starts[kind] = have
                if not starts:
                    entry.pop("starts")
            lenient = sorted((routes.lenient_in(entry) | routes.lenient_in(source)) & marks)
            if lenient:
                entry["lenient"] = lenient
        if not entry:
            data.pop(keep)
