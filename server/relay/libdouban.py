"""片库的「豆瓣」页（M7）：豆瓣榜单、片库详情页的短评和推荐、本机的「想看」。不登录豆瓣，永远。

为什么这样做：片库里的片子已经对上了豆瓣（libmeta.py），家里人想知道「最近有什么好看的」「这部别人怎么说」「还有
什么类似的」，再把想看的记下来，等网盘里有了提醒一声。豆瓣手机版的几个接口不登录也照常回答，就用它们。

- 榜单（LISTS）：正在热映、热门电影、热门剧集、高分 Top250，各取前 LIST_COUNT 部（libmeta.Douban.collection）。
  页面打开「豆瓣」时只读存好的（library-douban/lists.json），过了 LIST_TTL_S 的排进后台的队，页面从不等豆瓣。
- 详情页的短评和推荐：对上了豆瓣的电影、剧（有好几季时是看着的那一季）打开时，页面再问一次
  GET api/library/douban/subject/<豆瓣 id>；有存的（subjects.json，SUBJECT_TTL_S 内）马上给，没有就排队、页面过一会
  儿再问。取不到就不显示这一块。
- 每一部都标「片库里有」：片库里对上了这个豆瓣 id 的电影、剧、剧的一季（Library.douban_index，TMDB 的不算），
  点了打开片库的详情页；没有的给「想看」。
- 想看（Wishes）：每个人自己的，存在 library-douban/wish.json（600），每人至多 WISH_MAX 部；只在这台中转上，从不
  同步到豆瓣账号（本来就没有）。页面只送豆瓣 id，片名、海报这些从存好的榜单、推荐里找（页面给的地址一概不收）。
  片库后来对上了一部想看的（扫描、后台查资料、手动匹配），下次打开首页时多一排「想看的片子到了」
  （home_rows，WISH_NEW_S 内、没点开过的），点开就不再提醒；「想看」列表里标「片库里有了」。
- 客气：都走 library.douban（libmeta.Douban：一次一个请求、至少隔 GAP_S、被拦了歇一个小时、不带 Cookie、不发
  任何关于这家人的东西）。后台一件一件做（队里至多 QUEUE_MAX 件），两件之间再歇 TASK_GAP_S；取不成的
  RETRY_S 内不再取同一件；片库的「在线资料」关着时一个请求也不发。
- 海报一律经中转（GET api/library/douban/poster/<豆瓣 id>）：只取存好的榜单、推荐、想看里有的那一张（地址早就
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

LISTS = (("movie_showing", "正在热映"), ("movie_hot_gaia", "热门电影"), ("tv_hot", "热门剧集"),
         ("movie_top250", "高分 Top250"))
LIST_COUNT = 30
COMMENTS = 6
RECOMMENDATIONS = 10
# 存好的榜单、一部的短评和推荐多久算旧（旧了照样先给，后台再取）；取不成的过这么久再试。
LIST_TTL_S = 6 * 3600.0
SUBJECT_TTL_S = 86400.0
RETRY_S = 1800.0
# 后台：两件事之间至少歇这么久（libmeta.Douban 自己还有 GAP_S）；没事时每 TICK_S 看一眼。
TASK_GAP_S = 8.0
TICK_S = 600.0
QUEUE_MAX = 12
# 存盘的上限：短评和推荐至多存这么多部（最久没用的先丢）；海报。
SUBJECTS_MAX = 200
IMG_FILES_MAX = 400
IMG_BYTES_MAX = 60 * 1024 * 1024
IMG_RETRY_S = 600.0
# 想看：每人至多这么多部、至多这么多个人；片库里有了以后首页提醒这么久。
WISH_MAX = 200
WISH_USERS_MAX = 64
WISH_NEW_S = 14 * 86400.0
_DOUBAN_ID = re.compile(r"^\d{1,12}$")
_IMG_FILE = re.compile(r"^(\d{1,12})\.(jpg|png|webp)$")
_SUBJECT = re.compile(r"^/api/library/douban/subject/(\d{1,12})$")
_POSTER = re.compile(r"^/api/library/douban/poster/(\d{1,12})$")
IMAGE_TYPES = {"jpg": "image/jpeg", "png": "image/png", "webp": "image/webp"}
# 存进想看的：和榜单卡片一样的几样（img 是豆瓣的图片地址，只给中转自己取海报用，从不给页面）。
_CARD_KEYS = ("id", "title", "year", "type", "rating", "info", "img")


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
    """榜单、短评和推荐的存盘、后台的队、海报；想看（self.wishes）。library 是 library.Library（用它的 douban、
    online、douban_index、clock，测试里会换掉，所以每次现取）。"""

    def __init__(self, library, log: Callable[[str], None] = print):
        self.lib = library
        self.log = log
        self.dir = Path(library.dir) / "library-douban"
        self.img_dir = self.dir / "img"
        self.lists_path = self.dir / "lists.json"
        self.subjects_path = self.dir / "subjects.json"
        self._lock = threading.RLock()
        self._lists: dict[str, dict] = self._load_lists()
        self._subjects: dict[str, dict] = self._load_subjects()
        self._queue: deque[tuple[str, str]] = deque()
        self._wake = threading.Event()
        self._img_failed: dict[str, float] = {}
        self.wishes = Wishes(self.dir / "wish.json", lambda: self.lib.clock())

    # ---- 存盘 ----

    def _load_lists(self) -> dict[str, dict]:
        found = {}
        names = {name for name, _ in LISTS}
        for name, entry in _read(self.lists_path).items():
            if name in names and isinstance(entry, dict):
                items = [c for c in (_card(r) for r in entry.get("items") or []) if c is not None]
                found[name] = {"at": float(entry.get("at") or 0), "tried": float(entry.get("tried") or 0), "items": items}
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

    def _save_lists(self) -> None:
        _write(self.lists_path, self._lists)

    def _save_subjects(self) -> None:
        if len(self._subjects) > SUBJECTS_MAX:
            order = sorted(self._subjects, key=lambda d: self._subjects[d].get("used") or self._subjects[d]["at"])
            for gone in order[:len(self._subjects) - SUBJECTS_MAX]:
                del self._subjects[gone]
        _write(self.subjects_path, self._subjects)

    # ---- 后台 ----

    def _due(self, entry: dict | None, ttl: float) -> bool:
        now = self.lib.clock()
        if entry is None:
            return True
        if now - (entry.get("tried") or 0) < RETRY_S:
            return False                        # 刚取过没取成：过一会儿再说
        return now - (entry.get("at") or 0) >= ttl

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

    def step(self) -> bool:
        """做队里的一件（往豆瓣发了请求时 True）；关着、拦着、没事做 False。"""
        if not self.can():
            return False
        with self._lock:
            while True:
                if not self._queue:
                    return False
                kind, key = self._queue.popleft()
                entry = (self._lists if kind == "list" else self._subjects).get(key)
                if self._due(entry, LIST_TTL_S if kind == "list" else SUBJECT_TTL_S):
                    break                       # 排队时已经取好了的跳过，不发请求
        douban = self.lib.douban
        try:
            if kind == "list":
                found = {"items": douban.collection(key, LIST_COUNT)}
            else:
                # 一部要两个请求：短评没取成就不取推荐（多半是被拦了）。
                found = {"comments": douban.interests(key, COMMENTS)}
                found["recs"] = douban.recommendations(key, RECOMMENDATIONS)
        except (libmeta.Blocked, libmeta.Failed) as error:
            with self._lock:
                store = self._lists if kind == "list" else self._subjects
                old = store.get(key)
                if old is not None:
                    old["tried"] = self.lib.clock()
                else:
                    store[key] = {"at": 0.0, "tried": self.lib.clock(), "items": []} if kind == "list" else \
                        {"at": 0.0, "tried": self.lib.clock(), "used": self.lib.clock(), "comments": [], "recs": []}
                self._save_lists() if kind == "list" else self._save_subjects()
            if isinstance(error, libmeta.Failed):
                self.log(f"豆瓣{'榜单' if kind == 'list' else '短评和推荐'}没取成：{error}")
            return True
        with self._lock:
            now = self.lib.clock()
            if kind == "list":
                self._lists[key] = {"at": now, "tried": 0.0, **found}
                self._save_lists()
            else:
                used = (self._subjects.get(key) or {}).get("used") or now
                self._subjects[key] = {"at": now, "tried": 0.0, "used": used, **found}
                self._save_subjects()
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
    def _shown(card: dict, index: dict, wished: set[str]) -> dict:
        """给页面的一部：没有豆瓣的图片地址（海报经中转），标着片库里有没有、想没想看。"""
        row = {k: card.get(k) for k in ("id", "title", "year", "type", "rating", "info") if card.get(k) not in (None, "")}
        row["art"] = poster_url(card["id"]) if card.get("img") else ""
        have = index.get(card["id"])
        if have is not None:
            row["have"] = {k: v for k, v in have.items() if k != "key"}
        if card["id"] in wished:
            row["wished"] = True
        return row

    def lists_view(self, user: int) -> dict:
        """GET /api/library/douban：几个榜单（存好的；旧了的排进后台的队，loading 为真时页面过一会儿再问）和这个人的想看。"""
        index = self.lib.douban_index()
        self.wishes.settle(user, index)
        wished = self.wishes.ids(user)
        can = self.can()
        rows = []
        for name, title in LISTS:
            with self._lock:
                entry = self._lists.get(name)
                items = list(entry["items"]) if entry else []
                due = self._due(entry, LIST_TTL_S)
            loading = bool(due and can and self.want("list", name))
            rows.append({"name": name, "title": title, "at": int(entry["at"]) if entry and entry["at"] else 0,
                         "loading": loading, "items": [self._shown(c, index, wished) for c in items]})
        return {"ok": True, "online": self.lib.online(), "blocked": self.lib.douban.blocked(), "lists": rows,
                "wish": self.wish_view(user, index)}

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

    def _known(self, did: str) -> dict | None:
        """存好的榜单、推荐、想看里的这一部（海报只替这些取）。"""
        with self._lock:
            for entry in self._lists.values():
                for card in entry["items"]:
                    if card["id"] == did:
                        return card
            for entry in self._subjects.values():
                for card in entry["recs"]:
                    if card["id"] == did:
                        return card
        return None

    def known(self, user: int, did: str) -> dict | None:
        found = self._known(did)
        if found is None:
            found = next((r for r in self.wishes.rows(user) if r["id"] == did), None)
        return found

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
        try:
            kind, data = self.lib.douban.image(url)
        except (libmeta.Blocked, libmeta.Failed):
            self._img_failed[did] = self.lib.clock()
            if len(self._img_failed) > 500:
                self._img_failed.clear()
            handler._send(404, b"", "text/plain; charset=utf-8", miss)
            return
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
        handler._json(boards.lists_view(caller.user))
        return True
    match = _SUBJECT.match(path)
    if match:
        handler._json(boards.subject_view(caller.user, match.group(1)))
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
