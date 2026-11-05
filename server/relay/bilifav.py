"""B 站收藏夹做成小电视上的台（P1c，docs/superpowers/specs/2026-11-04-minimal-product-design.md 第 5 节）。

一个收藏夹一个台：状态里是 kind="series"（按顺序播、每个频道记进度、12 小时刷新，和剧集一样），source
是 "fav<收藏夹 id>"，台 id 是 fv01…（state.FAV_PREFIX，最多 state.MAX_FAVS 个），设备上在「B站收藏」组。
收藏夹要这个人自己的 B 站登录才看得到（私密的也行），所以列表和内容都带他的登录；刷新见 web.refresh_if_stale。
"""

from __future__ import annotations

import re

from .bilibili import SourceError
from .bililogin import INVALID, PREFIX, UNREACHABLE, _caller, _read_body

GETS = frozenset({"favorites"})
POSTS = frozenset({"favorites/import"})
_ID = re.compile(r"^[0-9]{1,20}$")
IMPORT_MAX = 50


def _owner_mid(accounts, user: int) -> str:
    record = accounts.record(user) or {}
    mid = str((record.get("cookies") or {}).get("DedeUserID") or (record.get("profile") or {}).get("mid") or "")
    return mid if _ID.match(mid) else ""


def _subscribed(state) -> tuple[dict[str, str], int]:
    """已经加成台的收藏夹：{收藏夹 id: 台 id}，和收藏夹台的个数。"""
    from .state import FAV_PREFIX
    with state.lock:
        mine = [c for c in state.creators if c.kind == "series" and c.site == "bilibili"
                and c.id.startswith(FAV_PREFIX) and c.source.startswith("fav")]
    return {c.source[3:]: c.id for c in mine}, len(mine)


def folders(relay, caller) -> dict:
    from .state import MAX_FAVS
    login = relay.bili_login(caller.user)
    mid = _owner_mid(relay.bili_accounts, caller.user)
    if login is None or not mid:
        return {"ok": False, "login": False, "error": "先在「设置 → B 站账号」扫码登录 B 站"}
    try:
        items = relay._client("bilibili").favorite_folders(mid, login=login)
    except SourceError as error:
        relay.log(f"取收藏夹列表失败：{error}")
        return {"ok": False, "error": INVALID if "登录" in str(error) else UNREACHABLE}
    have, count = _subscribed(relay._account(caller))
    # 加过的带上台 id（channel）：控制台据此「移除」（DELETE api/creators/<台 id>）。
    return {"ok": True, "items": [dict(i, subscribed=True, channel=have[i["id"]]) if i["id"] in have
                                  else dict(i, subscribed=False) for i in items],
            "count": count, "max": MAX_FAVS, "room": relay.room(caller=caller)}


def import_folders(relay, caller, ids) -> dict:
    from .state import FAV_PREFIX, MAX_FAVS
    from .table import label_for
    if not isinstance(ids, list) or not ids or len(ids) > IMPORT_MAX \
            or not all(isinstance(i, str) and _ID.match(i) for i in ids):
        return {"ok": False, "error": "请求格式不对"}
    listed = folders(relay, caller)
    if not listed.get("ok"):
        return listed
    titles = {i["id"]: i["title"] for i in listed["items"]}
    login = relay.bili_login(caller.user)
    state = relay._account(caller)
    client = relay._client("bilibili")
    have, count = _subscribed(state)
    added, skipped = [], []
    for fid in dict.fromkeys(ids):
        title = titles.get(fid)
        if title is None:
            skipped.append({"id": fid, "title": fid, "reason": "不是你的收藏夹"})
            continue
        if fid in have:
            skipped.append({"id": fid, "title": title, "reason": "已经加过了"})
            continue
        if count >= MAX_FAVS:
            skipped.append({"id": fid, "title": title, "reason": f"最多 {MAX_FAVS} 个收藏夹"})
            continue
        if not relay.room(label_for(title, "bilibili"), caller)["fits"]:
            skipped.append({"id": fid, "title": title, "reason": "小电视的频道列表满了"})
            continue
        try:
            _name, videos = client.series(f"fav{fid}", login=login)
        except SourceError as error:
            relay.log(f"加收藏夹 {fid} 失败：{error}")
            skipped.append({"id": fid, "title": title, "reason": "收藏夹里没有能放的视频"
                            if getattr(error, "permanent", False) else UNREACHABLE})
            continue
        if not videos:
            skipped.append({"id": fid, "title": title, "reason": "收藏夹里没有能放的视频"})
            continue
        try:
            creator = state.add("bilibili", f"fav{fid}", title, videos, relay.clock(), kind="series",
                                prefix=FAV_PREFIX, most=MAX_FAVS)
        except ValueError as error:
            skipped.append({"id": fid, "title": title, "reason": str(error)})
            continue
        have[fid] = creator.id
        count += 1
        added.append({"id": fid, "title": title, "channel": creator.id})
    if added:
        state.save()
        relay._tables_changed(state)
        relay.log(f"从 B 站收藏夹加了 {len(added)} 个台")
    return {"ok": True, "added": added, "skipped": skipped, "max": MAX_FAVS}


def get(handler, relay, path: str) -> bool:
    if not path.startswith(PREFIX) or path[len(PREFIX):] not in GETS:
        return False
    caller = _caller(handler, relay)
    if caller is None:
        handler._not_found()
        return True
    handler._json(folders(relay, caller))
    return True


def post(handler, relay, path: str) -> bool:
    if not path.startswith(PREFIX) or path[len(PREFIX):] not in POSTS:
        return False
    body = _read_body(handler)
    caller = _caller(handler, relay)
    if caller is None:
        handler._not_found()
        return True
    if body is None:
        handler._json({"ok": False, "error": "请求格式不对"}, 400)
        return True
    handler._json(import_folders(relay, caller, body.get("ids")))
    return True
