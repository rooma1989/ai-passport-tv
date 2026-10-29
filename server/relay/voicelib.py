"""语音点播片库：小电视上说「放苏里南」「苏里南第三集」「接着看」，放的是自己片库（library.py）里的视频。

是哪一部由电视服务认（server/voice/intent.py 的「片库」一节，按语音目录里的名字做模糊匹配），这里只管两件事：

- 目录（GET /api/voice/catalog 的 library，voicecmds.catalog 加上）：设备主人能看的片子——一部剧（各集一起，
  id 是 s<剧名哈希>，和 library.meta_key 一样）、一部电影或一个其他视频（m<条目 id>）——每部的名字（对上豆瓣的
  用豆瓣的片名）、别的叫法（文件名猜的片名、英文名、豆瓣的原名和又名）、剧有几集；主人「继续观看」里最近的那一个
  （continue）；投屏位里正在放的片库视频（playing：哪一部、第几集，「第 3 集」「上一集」用）。看过的、新加的
  在前，总共至多 CATALOG_BYTES 字节（电视服务读回答至多 256 KB，片子再多也不能把整个目录撑坏）。
- POST /api/voice/library {device, id, episode, season, continue}：放到这台设备上，和电脑版「播放到」同一条路
  （library.play_tv：放进投屏位，进度记在 pc-progress.json 的 lib:<条目 id>）。电影从上次看到的地方接着放；剧
  没说第几集时接着看：最近看的那一集没看完就接着放它，看完了放下一集，一集都没看过从第 1 集，全看完了从第 1 集
  重看。第几集按集号认（说了第几季只在那一季里找；没说时正在放这部剧就先找同一季），集号对不上时按顺序数。
  continue（「接着看」）放主人「继续观看」里最近的那一个，和首页那一排同一个规则（home._continue_rows）。

谁能用：片库只给家里人。语音命令在中转这边按那台设备的访客来（web.py 的 _DEVICE_SCOPED），片库按设备主人放：
只有电视服务（带密钥）替设备问的、或登录的家里人才有 library；扫设备码进来的访客手机永远没有。设备没绑定账号、
主人被停用、中转没有片库（云端）时目录里没有 library，接口回答「片库只给家里人看」。进度记在主人名下。
"""

from __future__ import annotations

import json
import re

from .library import Library, play_tv
from .table import CAST_ID
from .tenants import LEGACY, Caller

# 目录里片子的名字总共至多这么多字节、这么多部；每部至多这么多个叫法、每个叫法至多这么多个字。
CATALOG_BYTES = 64 * 1024
TITLES_MAX = 800
NAMES_MAX = 6
NAME_MAX = 40
REPLY_NO_ACCESS = "片库只给家里人看"
REPLY_NO_DEVICE = "没有这台设备"
REPLY_FORMAT = "请求格式不对"
REPLY_GONE = "片库里没有这一部了（可能刚重新扫描过）"
REPLY_NO_CONTINUE = "「继续观看」里没有片库的视频"
_KEY = re.compile(r"^(?:s[0-9a-f]{12}|m[0-9a-f]{16})$")


def owner_of(relay, dev: dict | None) -> Caller | None:
    """这台设备能用片库时，按设备主人来的调用者（进度记在他名下）；不能用为 None。"""
    library = getattr(relay, "library", None)
    accounts = getattr(relay, "accounts", None)
    if library is None or accounts is None or not relay.tenant or dev is None or dev.get("owner") is None:
        return None
    user = accounts.user(dev["owner"])
    if not user or user.get("status") != "active" or user.get("role") not in ("admin", "user"):
        return None
    return Caller(user=dev["owner"], role=user["role"], name=user.get("name") or "")


def allowed(caller: Caller, keyed: bool) -> bool:
    """问的是电视服务（带密钥，替说话的那台设备问），或登录的家里人；访客手机不行。"""
    return keyed or (not caller.guest and caller.user is not None)


def _groups(library) -> dict[str, list[dict]]:
    """片子的键（meta_key）→ 条目；剧的各集按季、集、路径排好（和 library.episodes_of 一样，第几集对得上设备上画的）。"""
    groups: dict[str, list[dict]] = {}
    for item in library.items():
        groups.setdefault(library.meta_key(item), []).append(item)
    for items in groups.values():
        items.sort(key=lambda i: (i.get("season") or 0, i.get("episode") or 0, i["path"]))
    return groups


def _kind(items: list[dict]) -> str:
    return "show" if items[0].get("kind") == "episode" else (items[0].get("kind") or "video")


def _names(library, items: list[dict], entry: dict | None) -> list[str]:
    """一部片子的叫法：先是页面上显示的名字，再是文件名猜的、英文名、豆瓣的片名、原名、又名。"""
    first = items[0]
    episode = first.get("kind") == "episode"
    names = [library.shown(first)["name"], first.get("show") if episode else first.get("title")]
    names += [i.get("alt") for i in items]
    if entry and entry.get("state") == "matched":
        names += [entry.get("title"), entry.get("original")] + list(entry.get("aka") or [])
    out: list[str] = []
    for name in names:
        name = " ".join(name.split())[:NAME_MAX] if isinstance(name, str) else ""
        if name and name not in out:
            out.append(name)
    return out[:NAMES_MAX]


def _watched(relay, library, user: int) -> dict[str, float]:
    """片子的键 → 主人最近一次看它的时刻（pc-progress.json 的 lib:<条目 id>）。"""
    store = getattr(relay, "pc_progress", None)
    seen: dict[str, float] = {}
    if store is None:
        return seen
    for key, rec in store.rows(user):
        item = library.item(key[4:]) if key.startswith("lib:") else None
        if item is not None:
            meta = library.meta_key(item)
            seen[meta] = max(seen.get(meta, 0.0), float(rec.get("at") or 0))
    return seen


def _place(groups: dict[str, list[dict]], library, item: dict) -> dict:
    """item 是哪一部的第几集：{"id", "kind", "episode", "total"}（电影的 episode、total 为 0）。"""
    key = library.meta_key(item)
    items = groups.get(key) or [item]
    if _kind(items) != "show":
        return {"id": key, "kind": _kind(items), "episode": 0, "total": 0}
    n = next((n for n, i in enumerate(items, 1) if i["id"] == item["id"]), 0)
    return {"id": key, "kind": "show", "episode": n, "total": len(items)}


def continue_item(relay, library, user: int) -> dict | None:
    """主人「继续观看」里最近的片库视频（和首页那一排同一个规则：看了一会儿、还没看完、条目还在）。"""
    from . import home         # home 引用 voicecmds，voicecmds 引用这里：用到时再引
    store = getattr(relay, "pc_progress", None)
    if store is None:
        return None
    for key, rec in store.rows(user):
        if not key.startswith("lib:"):
            continue
        position, duration = float(rec.get("position") or 0), float(rec.get("duration") or 0)
        if position < home.PROGRESS_MIN_S or (duration and position > duration - home.PROGRESS_END_S):
            continue
        item = library.item(key[4:])
        if item is not None and Library.playable(item):
            return item
    return None


def _on_cast(relay, dev: dict, library) -> dict | None:
    """投屏位里正在放的片库条目；设备不在投屏台上、放的不是片库的视频为 None。"""
    playing = relay._playing(dev)
    if not playing["online"] or playing["channel"] != CAST_ID:
        return None
    slot = relay._cast_of(dev)
    with slot.lock:
        video = slot.cast
    if video is None or video.site != "library":
        return None
    return library.item(video.bvid)


def catalog(relay, dev: dict | None, caller: Caller = LEGACY, keyed: bool = False) -> dict:
    """语音目录里片库的一段：{"library": {"titles", "continue", "playing"}}；不能用片库时为空。"""
    owner = owner_of(relay, dev) if allowed(caller, keyed) else None
    if owner is None:
        return {}
    library = relay.library
    groups = _groups(library)
    entries = library.meta.all()
    watched = _watched(relay, library, owner.user)
    order = sorted(groups.items(), key=lambda kv: (-watched.get(kv[0], 0.0),
                                                   -max(i.get("added") or 0 for i in kv[1]), kv[0]))
    titles, used = [], 0
    for key, items in order:
        if not any(Library.playable(i) for i in items):
            continue
        names = _names(library, items, entries.get(key))
        if not names:
            continue
        row = {"id": key, "kind": _kind(items), "name": names[0], "aka": names[1:]}
        if row["kind"] == "show":
            row["total"] = len(items)
        size = len(json.dumps(row, ensure_ascii=False).encode("utf-8")) + 1
        if used + size > CATALOG_BYTES or len(titles) >= TITLES_MAX:
            break
        titles.append(row)
        used += size
    names = {row["id"]: row["name"] for row in titles}

    def view(item: dict | None) -> dict | None:
        if item is None:
            return None
        place = _place(groups, library, item)
        return dict(place, name=names.get(place["id"]) or library.shown(item)["name"] or item.get("title") or "")

    return {"library": {"titles": titles, "continue": view(continue_item(relay, library, owner.user)),
                        "playing": view(_on_cast(relay, dev, library))}}


def _whole(value) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and 0 < value < 10_000 else 0


def _episode(items: list[dict], n: int, season: int, current: dict | None) -> dict | None:
    """第 n 集：集号对得上的（说了第几季只在那一季里找；没说时正在放这部剧就先找同一季，再找最前面那季）；
    集号对不上（文件名里没写集号）时按顺序数第 n 个。"""
    same = [i for i in items if (i.get("episode") or 0) == n]
    if season:
        same = [i for i in same if (i.get("season") or 1) == season]
        return same[0] if same else None
    if same:
        here = current.get("season") if current is not None and current in items else None
        return next((i for i in same if i.get("season") == here), same[0])
    return items[n - 1] if 1 <= n <= len(items) else None


def _resume(relay, user: int, items: list[dict]) -> tuple[dict, str]:
    """剧没说第几集：(要放的一集, 附带的话)。最近看的那一集没看完接着放，看完了放下一集，没看过从第 1 集。"""
    from . import home
    store = getattr(relay, "pc_progress", None)
    playable = [i for i in items if Library.playable(i)] or items
    latest, latest_at, record = None, -1.0, None
    for item in playable:
        rec = store.get(user, "lib:" + item["id"]) if store is not None else None
        if rec is not None and float(rec.get("at") or 0) > latest_at:
            latest, latest_at, record = item, float(rec.get("at") or 0), rec
    if latest is None:
        return playable[0], ""
    position, duration = float(record.get("position") or 0), float(record.get("duration") or 0)
    done = latest["id"] in (record.get("done") or []) or bool(duration and position > duration - home.PROGRESS_END_S)
    if not done:
        return latest, ""
    at = playable.index(latest)
    if at + 1 < len(playable):
        return playable[at + 1], ""
    return playable[0], "已经看完了，从第 1 集重看"


def play(relay, key, caller: Caller = LEGACY, body: dict | None = None, keyed: bool = False) -> dict:
    """POST /api/voice/library：把一部片子放到点名的设备上（library.play_tv），回答里加上名字和第几集。"""
    body = body or {}
    dev = relay.find_device(key, caller)
    if dev is None:
        return {"ok": False, "error": REPLY_NO_DEVICE}
    owner = owner_of(relay, dev) if allowed(caller, keyed) else None
    if owner is None:
        return {"ok": False, "error": REPLY_NO_ACCESS}
    if not relay._playing(dev)["online"]:
        return {"ok": False, "error": f"{dev['name']}没在线"}
    library = relay.library
    groups = _groups(library)
    note = ""
    if body.get("continue") is True:
        item = continue_item(relay, library, owner.user)
        if item is None:
            return {"ok": False, "error": REPLY_NO_CONTINUE}
    else:
        tid = body.get("id")
        if not isinstance(tid, str) or not _KEY.match(tid):
            return {"ok": False, "error": REPLY_FORMAT}
        items = groups.get(tid)
        if not items:
            return {"ok": False, "error": REPLY_GONE}
        n, season = _whole(body.get("episode")), _whole(body.get("season"))
        if _kind(items) != "show":
            item = items[0]
        elif n:
            item = _episode(items, n, season, _on_cast(relay, dev, library))
            if item is None:
                name = library.shown(items[0])["name"]
                where = f"第 {season} 季第 {n} 集" if season else f"第 {n} 集"
                return {"ok": False, "error": f"《{name}》没有{where}"}
        else:
            item, note = _resume(relay, owner.user, items)
    answer = play_tv(relay, owner, dev, item["id"])
    if not answer.get("ok"):
        return answer
    place = _place(groups, library, item)
    answer.update(name=library.shown(item)["name"] or item.get("title") or "", kind=place["kind"],
                  episode=place["episode"], total=place["total"])
    if note:
        answer["note"] = note
    return answer
