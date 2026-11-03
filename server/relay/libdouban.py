"""片库的「豆瓣」页（M7）：豆瓣榜单、豆瓣的详情、片库详情页的短评和推荐、本机的「想看」。不登录豆瓣，永远。

为什么这样做：片库里的片子已经对上了豆瓣（libmeta.py），家里人想知道「最近有什么好看的」「这部别人怎么说」「还有
什么类似的」，再把想看的记下来，等网盘里有了提醒一声。豆瓣手机版的几个接口不登录也照常回答，就用它们。

- 榜单（LISTS，按 GROUPS 分成电影、剧集、综艺 · 动画三组）：正在热映、热门电影、一周口碑、高分 Top250；热门剧集、
  国产剧、美剧、韩剧、日剧、口碑剧集；热门综艺、动画（2026-11-03 实测这些名字都在，一次给到 100 部也照常回答）。
  先取第一页 PAGE 部，页面点「加载更多」再往后取一页（start = 已有的部数，libmeta.Douban.collection），每个榜单至多
  LIST_MAX 部（Top250 取得到 250），豆瓣说一共几部（total）就到几部为止。页面只问正看着的那个榜单：读存好的
  （library-douban/lists.json），过了 LIST_TTL_S 的把第一页排进后台的队重取（往后的页不留，要时再点），页面从不等豆瓣。
- 豆瓣的详情（GET api/library/douban/detail/<豆瓣 id>）：点开片库里没有的一部，看海报、片名和原名、年份、评分和评分
  人数、类型、地区、片长或集数、导演主演、简介。只取手机版的 subject/<id>（libmeta.Douban.mobile，从不碰会跳验证页的
  条目页），后台取，存在 details.json（至多 DETAILS_MAX 部、DETAIL_TTL_S）；还没取到时先给榜单、推荐里已经有的几样。
- 详情页的短评和推荐：对上了豆瓣的电影、剧（有好几季时是看着的那一季）打开时，页面再问一次
  GET api/library/douban/subject/<豆瓣 id>；有存的（subjects.json，SUBJECT_TTL_S 内）马上给，没有就排队、页面过一会
  儿再问。取不到就不显示这一块。豆瓣的详情下面也是这一块。
- 每一部都标「片库里有」：片库里对上了这个豆瓣 id 的电影、剧、剧的一季（Library.douban_index，TMDB 的不算），
  点了打开片库的详情页；没有的给「想看」。
- 想看（Wishes）：每个人自己的，存在 library-douban/wish.json（600），每人至多 WISH_MAX 部；只在这台中转上，从不
  同步到豆瓣账号（本来就没有）。页面只送豆瓣 id，片名、海报这些从存好的榜单、推荐里找（页面给的地址一概不收）。
  片库后来对上了一部想看的（扫描、后台查资料、手动匹配），下次打开首页时多一排「想看的片子到了」
  （home_rows，WISH_NEW_S 内、没点开过的），点开就不再提醒；「想看」列表里标「片库里有了」。
- 客气：都走 library.douban（libmeta.Douban：一次一个请求、至少隔 GAP_S、被拦了歇一个小时、不带 Cookie、不发
  任何关于这家人的东西）。后台一件一件做（队里至多 QUEUE_MAX 件），两件之间再歇 TASK_GAP_S；取不成的
  RETRY_S 内不再取同一件；片库的「在线资料」关着时一个请求也不发。
- 海报一律经中转（GET api/library/douban/poster/<豆瓣 id>）：只取存好的榜单、推荐、详情、想看里有的那一张（地址早就
  按 libmeta.good_image_url 只认 doubanio.com），存在 library-douban/img/（至多 IMG_FILES_MAX 张、IMG_BYTES_MAX
  字节，最旧的先删）。页面从不直接连豆瓣，也拿不到豆瓣的图片地址。
- 谁能看：登录的家里人（library.get 的规矩，访客、没登录、云端 404）；POST /api/library/wish 访客 ok: false。

只用标准库。
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable

from . import libmeta
from .tenants import NotFound

GROUPS = (("movie", "电影"), ("tv", "剧集"), ("more", "综艺 · 动画"))
LISTS = (("movie_showing", "正在热映", "movie"), ("movie_hot_gaia", "热门电影", "movie"),
         ("movie_weekly_best", "一周口碑", "movie"), ("movie_top250", "高分 Top250", "movie"),
         ("tv_hot", "热门剧集", "tv"), ("tv_domestic", "国产剧", "tv"), ("tv_american", "美剧", "tv"),
         ("tv_korean", "韩剧", "tv"), ("tv_japanese", "日剧", "tv"), ("tv_global_best_weekly", "口碑剧集", "tv"),
         ("show_hot", "热门综艺", "more"), ("tv_animation", "动画", "more"))
# 一页取几部（第一页、「加载更多」都是）；一个榜单至多存几部。
PAGE = 50
LIST_MAX = 250
COMMENTS = 6
RECOMMENDATIONS = 10
# 存好的榜单、一部的短评和推荐多久算旧（旧了照样先给，后台再取）；取不成的过这么久再试。
LIST_TTL_S = 6 * 3600.0
SUBJECT_TTL_S = 86400.0
DETAIL_TTL_S = 3 * 86400.0
RETRY_S = 1800.0
# 后台：两件事之间至少歇这么久（libmeta.Douban 自己还有 GAP_S）；没事时每 TICK_S 看一眼。
TASK_GAP_S = 8.0
TICK_S = 600.0
QUEUE_MAX = 12
# 存盘的上限：短评和推荐至多存这么多部（最久没用的先丢）；海报。
SUBJECTS_MAX = 200
DETAILS_MAX = 300
IMG_FILES_MAX = 400
IMG_BYTES_MAX = 60 * 1024 * 1024
IMG_RETRY_S = 600.0
# 同一时间至多这么多张海报在等豆瓣（豆瓣的请求一个一个来）：一屏几十张一起要时，别的先回 503（页面过一会儿再要），
# 不然榜单的下一页、详情都得排在几十张海报后面。
IMG_AT_ONCE = 3
# 想看：每人至多这么多部、至多这么多个人；片库里有了以后首页提醒这么久。
WISH_MAX = 200
WISH_USERS_MAX = 64
WISH_NEW_S = 14 * 86400.0
_DOUBAN_ID = re.compile(r"^\d{1,12}$")
_IMG_FILE = re.compile(r"^(\d{1,12})\.(jpg|png|webp)$")
_SUBJECT = re.compile(r"^/api/library/douban/subject/(\d{1,12})$")
_DETAIL = re.compile(r"^/api/library/douban/detail/(\d{1,12})$")
_POSTER = re.compile(r"^/api/library/douban/poster/(\d{1,12})$")
IMAGE_TYPES = {"jpg": "image/jpeg", "png": "image/png", "webp": "image/webp"}
# 存进想看的：和榜单卡片一样的几样（img 是豆瓣的图片地址，只给中转自己取海报用，从不给页面）。
_CARD_KEYS = ("id", "title", "year", "type", "rating", "info", "img")
# 详情存下来的（libmeta.parse_mobile 的几样）和给页面的（没有 img）。
_DETAIL_KEYS = ("id", "title", "original", "year", "type", "rating", "votes", "genres", "countries", "directors",
                "actors", "plot", "episodes", "durations", "languages", "pubdate", "aka", "img")
_DETAIL_VIEW = tuple(k for k in _DETAIL_KEYS if k != "img")


def _write(path: Path, data: dict) -> None:
    """整份换掉写，600（和 library.json 一样）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _read(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _card(row) -> dict | None:
    """存盘里的一部（自己写的，也可能被改坏了）→ 只留认得的几样。"""
    if not isinstance(row, dict) or not _DOUBAN_ID.match(str(row.get("id") or "")) or not row.get("title"):
        return None
    card = {k: row.get(k) for k in _CARD_KEYS}
    if not libmeta.good_image_url(card["img"]):
        card["img"] = ""
    return card


def _detail(row) -> dict | None:
    """一部的详情（parse_mobile 的，或存盘里读回来的）→ 只留认得的几样；不像样的 None。"""
    if not isinstance(row, dict) or not _DOUBAN_ID.match(str(row.get("id") or "")) or not row.get("title"):
        return None
    data = {k: row.get(k) for k in _DETAIL_KEYS if row.get(k) not in (None, "", [])}
    if not libmeta.good_image_url(data.get("img")):
        data.pop("img", None)
    return data


def poster_url(did: str) -> str:
    return f"api/library/douban/poster/{did}"


class Wishes:
    """wish.json：{"users": {"<用户 id>": [{id, title, year, type, rating, info, img, at, got, seen}]}}，新加的在前。
    got：片库第一次有了它的时刻；seen：看过「到了」的提醒。"""

    def __init__(self, path: Path, clock: Callable[[], float]):
        self.path = Path(path)
        self.clock = clock
        self._lock = threading.Lock()
        users = _read(self.path).get("users")
        self._users: dict[str, list[dict]] = {}
        for uid, rows in (users.items() if isinstance(users, dict) else ()):
            if isinstance(uid, str) and uid.isdigit() and isinstance(rows, list):
                kept = []
                for row in rows[:WISH_MAX]:
                    card = _card(row)
                    if card is not None:
                        card.update(at=int(row.get("at") or 0), got=int(row.get("got") or 0), seen=bool(row.get("seen")))
                        kept.append(card)
                self._users[uid] = kept

    def _save(self) -> None:
        _write(self.path, {"users": self._users})

    def rows(self, user: int) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._users.get(str(user), [])]

    def ids(self, user: int) -> set[str]:
        with self._lock:
            return {r["id"] for r in self._users.get(str(user), [])}

    def add(self, user: int, card: dict) -> dict:
        with self._lock:
            rows = self._users.get(str(user), [])
            if any(r["id"] == card["id"] for r in rows):
                return {"ok": True}
            if len(rows) >= WISH_MAX:
                return {"ok": False, "error": f"「想看」至多 {WISH_MAX} 部，先删掉几部不想看的"}
            if str(user) not in self._users and len(self._users) >= WISH_USERS_MAX:
                return {"ok": False, "error": "这台中转上记「想看」的人太多了"}
            row = {k: card.get(k) for k in _CARD_KEYS}
            row.update(at=int(self.clock()), got=0, seen=False)
            self._users[str(user)] = [row] + rows
            self._save()
            return {"ok": True}

    def remove(self, user: int, did: str) -> bool:
        with self._lock:
            rows = self._users.get(str(user), [])
            kept = [r for r in rows if r["id"] != did]
            if len(kept) == len(rows):
                return False
            if kept:
                self._users[str(user)] = kept
            else:
                self._users.pop(str(user), None)
            self._save()
            return True

    def settle(self, user: int, index: dict) -> None:
        """片库里有了的记下时刻（got，只记第一次）；又没了的（文件删了）清掉，以后再有了再提醒。"""
        changed = False
        now = int(self.clock())
        with self._lock:
            for row in self._users.get(str(user), []):
                have = row["id"] in index
                if have and not row.get("got"):
                    row.update(got=now, seen=False)
                    changed = True
                elif not have and row.get("got"):
                    row.update(got=0, seen=False)
                    changed = True
            if changed:
                self._save()

    def find(self, did: str) -> dict | None:
        """谁的想看里有这一部（中转替页面取海报时用）。"""
        with self._lock:
            for rows in self._users.values():
                for row in rows:
                    if row["id"] == did:
                        return dict(row)
        return None

    def seen(self, user: int, did: str) -> bool:
        with self._lock:
            for row in self._users.get(str(user), []):
                if row["id"] == did and row.get("got") and not row.get("seen"):
                    row["seen"] = True
                    self._save()
                    return True
        return False


class Boards:
    """榜单、详情、短评和推荐的存盘、后台的队、海报；想看（self.wishes）。library 是 library.Library（用它的 douban、
    online、douban_index、clock，测试里会换掉，所以每次现取）。"""

    def __init__(self, library, log: Callable[[str], None] = print):
        self.lib = library
        self.log = log
        self.dir = Path(library.dir) / "library-douban"
        self.img_dir = self.dir / "img"
        self.lists_path = self.dir / "lists.json"
        self.subjects_path = self.dir / "subjects.json"
        self.details_path = self.dir / "details.json"
        self._lock = threading.RLock()
        self._lists: dict[str, dict] = self._load_lists()
        self._subjects: dict[str, dict] = self._load_subjects()
        self._details: dict[str, dict] = self._load_details()
        self._queue: deque[tuple[str, str]] = deque()
        self._doing: tuple[str, str] | None = None      # 正在取的那一件（取出队了、还没取完）
        self._img_slots = threading.BoundedSemaphore(IMG_AT_ONCE)
        self._wake = threading.Event()
        self._img_failed: dict[str, float] = {}
        self.wishes = Wishes(self.dir / "wish.json", lambda: self.lib.clock())

    # ---- 存盘 ----

    def _load_lists(self) -> dict[str, dict]:
        found = {}
        names = {name for name, _, _ in LISTS}
        for name, entry in _read(self.lists_path).items():
            if name in names and isinstance(entry, dict):
                items = [c for c in (_card(r) for r in entry.get("items") or []) if c is not None][:LIST_MAX]
                total = entry.get("total")
                found[name] = {"at": float(entry.get("at") or 0), "tried": float(entry.get("tried") or 0), "items": items,
                               "total": total if isinstance(total, int) and not isinstance(total, bool) else None}
        return found

    def _load_subjects(self) -> dict[str, dict]:
        found = {}
        for did, entry in _read(self.subjects_path).items():
            if _DOUBAN_ID.match(did) and isinstance(entry, dict):
                comments = [c for c in entry.get("comments") or [] if isinstance(c, dict) and c.get("text")]
                recs = [c for c in (_card(r) for r in entry.get("recs") or []) if c is not None]
                found[did] = {"at": float(entry.get("at") or 0), "tried": float(entry.get("tried") or 0),
                              "used": float(entry.get("used") or 0), "comments": comments, "recs": recs}
        return found

    def _load_details(self) -> dict[str, dict]:
        found = {}
        for did, entry in _read(self.details_path).items():
            if _DOUBAN_ID.match(did) and isinstance(entry, dict):
                data = _detail(entry.get("data")) if entry.get("at") else {}
                if entry.get("at") and data is None:
                    continue
                found[did] = {"at": float(entry.get("at") or 0), "tried": float(entry.get("tried") or 0),
                              "used": float(entry.get("used") or 0), "data": data or {}}
        return found

    def _save_lists(self) -> None:
        _write(self.lists_path, self._lists)

    @staticmethod
    def _bound(store: dict[str, dict], cap: int) -> None:
        """至多 cap 部：最久没用的先丢。"""
        if len(store) > cap:
            order = sorted(store, key=lambda d: store[d].get("used") or store[d]["at"])
            for gone in order[:len(store) - cap]:
                del store[gone]

    def _save_subjects(self) -> None:
        self._bound(self._subjects, SUBJECTS_MAX)
        _write(self.subjects_path, self._subjects)

    def _save_details(self) -> None:
        self._bound(self._details, DETAILS_MAX)
        _write(self.details_path, self._details)

    # ---- 后台 ----

    def _due(self, entry: dict | None, ttl: float) -> bool:
        now = self.lib.clock()
        if entry is None:
            return True
        if now - (entry.get("tried") or 0) < RETRY_S:
            return False                        # 刚取过没取成：过一会儿再说
        return now - (entry.get("at") or 0) >= ttl

    @staticmethod
    def _cap(entry: dict) -> int:
        """这个榜单至多能有几部：豆瓣说的一共几部（没说时不限），再不超过 LIST_MAX。"""
        total = entry.get("total")
        return min(LIST_MAX, total) if isinstance(total, int) else LIST_MAX

    def _more_due(self, entry: dict | None) -> bool:
        """能往后再取一页：第一页取到了、还没到头、刚才没取失败。"""
        return bool(entry and entry["at"] and len(entry["items"]) < self._cap(entry)
                    and self.lib.clock() - (entry.get("tried") or 0) >= RETRY_S)

    def can(self) -> bool:
        """现在能往豆瓣发：片库的在线资料开着、豆瓣没拦着。"""
        return self.lib.online() and not self.lib.douban.blocked()

    def want(self, kind: str, key: str) -> bool:
        """排进后台的队（已经在队里的、队满了的不排）；在线资料关着时不排。"""
        if not self.lib.online():
            return False
        with self._lock:
            if (kind, key) in self._queue:
                return True
            if len(self._queue) >= QUEUE_MAX:
                return False
            self._queue.append((kind, key))
        self._wake.set()
        return True

    def _store(self, kind: str) -> tuple[dict, float]:
        return {"list": (self._lists, LIST_TTL_S), "more": (self._lists, LIST_TTL_S),
                "subject": (self._subjects, SUBJECT_TTL_S), "detail": (self._details, DETAIL_TTL_S)}[kind]

    def _save(self, kind: str) -> None:
        if kind in ("list", "more"):
            self._save_lists()
        elif kind == "subject":
            self._save_subjects()
        else:
            self._save_details()

    def step(self) -> bool:
        """做队里的一件（往豆瓣发了请求时 True）；关着、拦着、没事做 False。几件事：list（榜单的第一页，旧了重取）、
        more（榜单往后的一页）、subject（一部的短评和推荐）、detail（一部的详情）。"""
        if not self.can():
            return False
        with self._lock:
            while True:
                if not self._queue:
                    return False
                kind, key = self._queue.popleft()
                store, ttl = self._store(kind)
                if kind == "more" and self._more_due(store.get(key)):
                    start = len(store[key]["items"])
                    break
                if kind != "more" and self._due(store.get(key), ttl):
                    break                       # 排队时已经取好了的跳过，不发请求
            start = start if kind == "more" else 0
            self._doing = (kind, key)
        try:
            return self._fetch(kind, key, store, start)
        finally:
            with self._lock:
                self._doing = None

    def _fetch(self, kind: str, key: str, store: dict, start: int) -> bool:
        """step 取出来的一件：问豆瓣、存下来。"""
        douban = self.lib.douban
        try:
            if kind == "list":
                items, total = douban.collection(key, PAGE)
                found = {"items": items[:LIST_MAX], "total": total}
            elif kind == "more":
                items, total = douban.collection(key, PAGE, start)
            elif kind == "subject":
                # 一部要两个请求：短评没取成就不取推荐（多半是被拦了）。
                found = {"comments": douban.interests(key, COMMENTS)}
                found["recs"] = douban.recommendations(key, RECOMMENDATIONS)
            else:
                found = {"data": _detail(douban.mobile(key)) or {}}
        except (libmeta.Blocked, libmeta.Failed) as error:
            now = self.lib.clock()
            with self._lock:
                old = store.get(key)
                if old is not None:
                    old["tried"] = now
                else:
                    store[key] = {"at": 0.0, "tried": now, **{"list": {"items": [], "total": None},
                                                              "subject": {"used": now, "comments": [], "recs": []},
                                                              "detail": {"used": now, "data": {}}}[kind]}
                self._save(kind)
            if isinstance(error, libmeta.Failed):
                what = {"list": "榜单", "more": "榜单的下一页", "subject": "短评和推荐", "detail": "详情"}[kind]
                self.log(f"豆瓣{what}没取成：{error}")
            return True
        with self._lock:
            now = self.lib.clock()
            if kind == "list":
                self._lists[key] = {"at": now, "tried": 0.0, **found}
            elif kind == "more":
                entry = self._lists.get(key)
                if entry is None or len(entry["items"]) != start:
                    return True                 # 取的时候榜单重取过了：这一页对不上，不要了
                have = {c["id"] for c in entry["items"]}
                new = [c for c in items if c["id"] not in have]
                entry["items"] = (entry["items"] + new)[:LIST_MAX]
                if total is not None:
                    entry["total"] = total
                if not new:
                    entry["total"] = len(entry["items"])       # 再往后没有了（或者都是重复的）：到头
            else:
                used = (store.get(key) or {}).get("used") or now
                store[key] = {"at": now, "tried": 0.0, "used": used, **found}
            self._save(kind)
        return True

    def pending(self) -> bool:
        with self._lock:
            return bool(self._queue)

    def run(self, sleep=time.sleep) -> None:
        """后台线程：有事就一件一件做，每件之间歇 TASK_GAP_S。"""
        while True:
            self._wake.wait(TICK_S)
            self._wake.clear()
            try:
                while self.step():
                    sleep(TASK_GAP_S)
            except Exception as error:  # noqa: BLE001 —— 后台线程不能悄悄死
                self.log(f"豆瓣榜单出错：{type(error).__name__}")

    # ---- 给页面的 ----

    @staticmethod
    def _shown(card: dict, index: dict, wished: set[str], keys=("id", "title", "year", "type", "rating", "info")) -> dict:
        """给页面的一部：没有豆瓣的图片地址（海报经中转），标着片库里有没有、想没想看。"""
        row = {k: card.get(k) for k in keys if card.get(k) not in (None, "", [])}
        row["art"] = poster_url(card["id"]) if card.get("img") else ""
        have = index.get(card["id"])
        if have is not None:
            row["have"] = {k: v for k, v in have.items() if k != "key"}
        if card["id"] in wished:
            row["wished"] = True
        return row

    def lists_view(self, user: int, name: str | None = None) -> dict:
        """GET /api/library/douban[?list=<名字>]：各个榜单有几部、能不能再往后取，name 那一个带上全部的片子（存好的；
        旧了的排进后台的队，loading 为真时页面过一会儿再问）；还有这个人的想看。只有页面正看着的那个榜单会去豆瓣取。"""
        index = self.lib.douban_index()
        self.wishes.settle(user, index)
        wished = self.wishes.ids(user)
        can = self.can()
        rows = []
        for lname, title, group in LISTS:
            with self._lock:
                entry = self._lists.get(lname)
                items = list(entry["items"]) if entry and lname == name else []
                due = self._due(entry, LIST_TTL_S)
                more = self._more_due(entry)
                # 下一页还在排队、或者正在取：页面接着问（取出队了不算排队，不看这个页面就不刷新了）。
                paging = ("more", lname) in self._queue or self._doing == ("more", lname)
            loading = lname == name and can and (bool(due and self.want("list", lname)) or paging)
            row = {"name": lname, "title": title, "group": group, "at": int(entry["at"]) if entry and entry["at"] else 0,
                   "count": len(entry["items"]) if entry else 0, "more": more, "loading": loading}
            if lname == name:
                row["items"] = [self._shown(c, index, wished) for c in items]
            rows.append(row)
        return {"ok": True, "online": self.lib.online(), "blocked": self.lib.douban.blocked(),
                "groups": [{"id": g, "title": t} for g, t in GROUPS], "lists": rows, "wish": self.wish_view(user, index)}

    def more(self, name) -> dict:
        """POST /api/library/douban/more {list}：这个榜单往后再取一页（后台的队；页面接着问 lists_view）。"""
        if name not in {n for n, _, _ in LISTS}:
            return {"ok": False, "error": "没有这个榜单"}
        with self._lock:
            due = self._more_due(self._lists.get(name))
        if not due:
            return {"ok": True, "queued": False}
        if not self.can():
            return {"ok": False, "error": "豆瓣暂时不让查了，过一会儿再来" if self.lib.online() else "片库的在线资料关着"}
        return {"ok": True, "queued": self.want("more", name)}

    def wish_view(self, user: int, index: dict | None = None) -> list[dict]:
        index = self.lib.douban_index() if index is None else index
        rows = []
        for row in self.wishes.rows(user):
            shown = self._shown(row, index, {row["id"]})
            shown["at"] = row["at"]
            rows.append(shown)
        return rows

    def subject_view(self, user: int, did: str) -> dict:
        """GET /api/library/douban/subject/<id>：一部的热门短评和推荐。state：ready（有）、loading（排上了，过一会儿再问）、
        none（取不到：在线资料关着、被拦着、刚取过没取成）。"""
        with self._lock:
            entry = self._subjects.get(did)
            if entry is not None and entry["at"]:
                entry["used"] = self.lib.clock()     # 只在内存里记，下次存盘时一起写
            have = dict(entry) if entry is not None and entry["at"] else None
            due = self._due(entry, SUBJECT_TTL_S)
        if due and self.can():
            self.want("subject", did)
        if have is None:
            return {"ok": True, "state": "loading" if due and self.can() else "none", "comments": [], "recs": []}
        index = self.lib.douban_index()
        wished = self.wishes.ids(user)
        return {"ok": True, "state": "ready", "comments": have["comments"],
                "recs": [self._shown(c, index, wished) for c in have["recs"] if c["id"] != did]}

    def detail_view(self, user: int, did: str) -> dict:
        """GET /api/library/douban/detail/<id>：一部的详情（手机版的 subject，存 DETAIL_TTL_S）。还没取到时先给榜单、推荐、
        想看里已经有的那几样（片名、年份、评分、海报），state 是 loading（排上了）或 none（取不到）。"""
        with self._lock:
            entry = self._details.get(did)
            if entry is not None and entry["at"]:
                entry["used"] = self.lib.clock()
            data = dict(entry["data"]) if entry is not None and entry["at"] else None
            due = self._due(entry, DETAIL_TTL_S)
        if due and self.can():
            self.want("detail", did)
        index = self.lib.douban_index()
        wished = self.wishes.ids(user)
        if data is None:
            card = self._known(did) or self.wishes.find(did)
            return {"ok": True, "state": "loading" if due and self.can() else "none",
                    "detail": self._shown(card, index, wished) if card else None}
        return {"ok": True, "state": "ready", "detail": self._shown(data, index, wished, _DETAIL_VIEW)}

    def _known(self, did: str) -> dict | None:
        """存好的榜单、推荐、详情里的这一部（海报只替这些和想看里的取）。"""
        with self._lock:
            for entry in self._lists.values():
                for card in entry["items"]:
                    if card["id"] == did:
                        return dict(card)
            for entry in self._subjects.values():
                for card in entry["recs"]:
                    if card["id"] == did:
                        return dict(card)
            entry = self._details.get(did)
            if entry is not None and entry["data"].get("title"):
                return dict(entry["data"])
        return None

    def image_url(self, did: str) -> str:
        found = self._known(did) or self.wishes.find(did)
        url = (found or {}).get("img") or ""
        return url if libmeta.good_image_url(url) else ""

    def serve_poster(self, handler, did: str) -> None:
        """GET /api/library/douban/poster/<id>：存好的就给；没有时在线资料开着才去豆瓣取一次（只认 doubanio.com）。"""
        miss = {"Cache-Control": "private, max-age=600"}
        for kind, mime in IMAGE_TYPES.items():
            path = self.img_dir / f"{did}.{kind}"
            try:
                data = path.read_bytes()
            except OSError:
                continue
            handler._send(200, data, mime, {"Cache-Control": "private, max-age=86400"})
            return
        url = self.image_url(did)
        failed = self._img_failed.get(did)
        if not url or not self.can() or (failed is not None and self.lib.clock() - failed < IMG_RETRY_S):
            handler._send(404, b"", "text/plain; charset=utf-8", miss)
            return
        if not self._img_slots.acquire(blocking=False):
            handler._send(503, b"", "text/plain; charset=utf-8", {"Cache-Control": "no-store", "Retry-After": "5"})
            return
        try:
            kind, data = self.lib.douban.image(url)
        except (libmeta.Blocked, libmeta.Failed):
            self._img_failed[did] = self.lib.clock()
            if len(self._img_failed) > 500:
                self._img_failed.clear()
            handler._send(404, b"", "text/plain; charset=utf-8", miss)
            return
        finally:
            self._img_slots.release()
        try:
            self.img_dir.mkdir(parents=True, exist_ok=True)
            os.chmod(self.img_dir, 0o700)
            tmp = self.img_dir / f"{did}.{kind}.tmp"
            tmp.write_bytes(data)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.img_dir / f"{did}.{kind}")
            self._prune_images()
        except OSError:
            pass
        handler._send(200, data, IMAGE_TYPES[kind], {"Cache-Control": "private, max-age=86400"})

    def _prune_images(self) -> None:
        files = []
        for path in self.img_dir.iterdir():
            if not _IMG_FILE.match(path.name):
                continue
            try:
                info = path.stat()
            except OSError:
                continue
            files.append((info.st_mtime, info.st_size, path))
        total = sum(size for _, size, _ in files)
        count = len(files)
        for _, size, path in sorted(files):
            if total <= IMG_BYTES_MAX and count <= IMG_FILES_MAX:
                break
            try:
                path.unlink()
                total -= size
                count -= 1
            except OSError:
                pass

    # ---- 想看 ----

    def wish(self, user: int, body: dict) -> dict:
        """POST /api/library/wish {id, on} 加 / 删；{id, seen: true} 看过了「到了」的提醒。"""
        did = body.get("id")
        if not isinstance(did, str) or not _DOUBAN_ID.match(did):
            return {"ok": False, "error": "请求格式不对"}
        if body.get("seen") is True:
            self.wishes.seen(user, did)
            return {"ok": True}
        on = body.get("on")
        if not isinstance(on, bool):
            return {"ok": False, "error": "请求格式不对"}
        if not on:
            self.wishes.remove(user, did)
            return {"ok": True, "wish": self.wish_view(user)}
        index = self.lib.douban_index()
        if did in index:
            return {"ok": False, "error": "片库里已经有了"}
        card = self._known(did)
        if card is None:
            return {"ok": False, "error": "找不到这部片子，刷新一下再试"}
        answer = self.wishes.add(user, card)
        if answer.get("ok"):
            answer["wish"] = self.wish_view(user, index)
        return answer


# ---------------------------------------------------------------- HTTP、首页

def get(handler, library, caller, path: str) -> bool:
    """library.get 已经查过是登录的家里人（访客、没登录、云端 404）。"""
    boards = library.boards
    if path == "/api/library/douban":
        handler._json(boards.lists_view(caller.user, handler._query().get("list")))
        return True
    match = _SUBJECT.match(path)
    if match:
        handler._json(boards.subject_view(caller.user, match.group(1)))
        return True
    match = _DETAIL.match(path)
    if match:
        handler._json(boards.detail_view(caller.user, match.group(1)))
        return True
    match = _POSTER.match(path)
    if match:
        boards.serve_poster(handler, match.group(1))
        return True
    return False


def wish(relay, caller, body: dict) -> dict:
    """POST /api/library/wish：登录的家里人自己的想看；访客 ok: false，没有片库（云端）404。"""
    library = getattr(relay, "library", None)
    if caller.guest or caller.user is None:
        return {"ok": False, "error": "登录以后才能记「想看」"}
    if library is None:
        raise NotFound("library")
    answer = library.boards.wish(caller.user, body)
    relay.forget_home(caller)
    return answer


def more(relay, caller, body: dict) -> dict:
    """POST /api/library/douban/more {list}：榜单往后再取一页。登录的家里人；访客 ok: false，没有片库（云端）404。"""
    library = getattr(relay, "library", None)
    if caller.guest or caller.user is None:
        return {"ok": False, "error": "请求格式不对"}
    if library is None:
        raise NotFound("library")
    return library.boards.more(body.get("list"))


def home_rows(relay, caller) -> list[dict]:
    """首页「想看的片子到了」：这个人想看的、片库里有了（WISH_NEW_S 内）、还没点开过的。点开的是片库的详情页。"""
    library = getattr(relay, "library", None)
    if library is None or caller.guest or caller.user is None:
        return []
    boards = library.boards
    if not boards.wishes.rows(caller.user):
        return []
    index = library.douban_index()
    boards.wishes.settle(caller.user, index)
    now = library.clock()
    entries = None
    rows = []
    for row in sorted(boards.wishes.rows(caller.user), key=lambda r: -(r.get("got") or 0)):
        have = index.get(row["id"])
        if have is None or row.get("seen") or now - (row.get("got") or 0) >= WISH_NEW_S:
            continue
        if entries is None:
            entries = library.meta.all()
        entry = entries.get(have["key"]) or {}
        art = library._meta_art_url(have["key"], entry) if entry.get("poster") or entry.get("img") else \
            (poster_url(row["id"]) if row.get("img") else "")
        rows.append({"id": f"wish:{row['id']}", "kind": "wish", "title": row["title"],
                     "subtitle": "想看的到了" + (f" · {row['year']}" if row.get("year") else ""), "source": "library",
                     "art": {"url": art, "ratio": "poster"}, "wish": row["id"],
                     "have": {k: v for k, v in have.items() if k != "key"}})
    return rows
