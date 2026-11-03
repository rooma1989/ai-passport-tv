"""电脑版首页大图后面的壁纸（家庭服务器和电脑客户端；云端没有，接口一律 404）。

主人觉得首页太素，要一张随机的壁纸垫在大图后面。壁纸来自 GitHub 上的 dharmx/walls（约 9.4k 星，一千多张插画、
风景类壁纸，按主题分文件夹；仓库没写许可证，所以页面上一直写着「壁纸：dharmx/walls」和主题、文件名）。

    GET /api/home/wallpaper[?not=<id>]   随便挑一张：{ok, wallpaper: {id, url, credit, theme, name, w, h} 或 null,
                                         count, filling}；not 是现在这张（「换一张」不再给它）
    GET /api/home/wallpaper/<id>         图片本身（id 是内容的 SHA-256 前 16 位，同一个地址永远是同一张，缓存很久）

- 浏览器从不连 GitHub（隐私；raw.githubusercontent.com 在国内又慢又常被墙）：文件列表和图片都是中转去取的，
  经「设置 → YouTube」里填的代理（和片库的 TMDB 一样，library._proxy、libmeta.tmdb_fetch），没填就看环境变量。
  发出去的只有两种请求：git trees 接口（文件列表）和图片的 GET，不带任何用户的东西（没有 cookies、没有用户名）。
- 文件列表：GET https://api.github.com/repos/dharmx/walls/git/trees/main?recursive=1（不登录每小时 60 次，
  我们每 LIST_TTL_S 秒一次），只留图片（jpg、jpeg、png、webp），不要 SKIP_THEMES 里的文件夹、太小（多半是图标、
  花纹）和太大（> FILE_MAX）的；存在数据目录的 wallpapers/list.json（600）。truncated（列表太长没给全）时
  用给了的那部分。
- 图片：先 raw.githubusercontent.com，不行再 jsDelivr（cdn.jsdelivr.net/gh/…）；看文件头是真图片、量出宽高，
  要横图（宽高比至少 MIN_RATIO）、够大（宽至少 MIN_W）；不合格的记进 list.json 的 rejected，以后不再挑它。
- 本地的池子：至多 POOL_MAX 张、合计至多 POOL_BYTES 字节，存在 wallpapers/<id>.<扩展名>（600），清单是
  pool.json。后台一个线程一次取一张、两张之间隔 GAP_S 秒；满了以后每 ROTATE_S 秒换掉最旧的一张。请求路径上
  从不去取：池子空着时回答 null（页面照原来的样子），后台开始补。最近 IDLE_S 秒里没人要过壁纸、电脑上有人在看
  （和片库预热一样不抢网）时不取。线程在第一次有人要壁纸时才起来：关了壁纸的家里，中转一次也不连 GitHub。
- 开关在电脑版「设置 → 通用 → 外观」（记在这个浏览器里，和深色、浅色一样）；关了页面就不来问。

只用标准库。
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import re
import struct
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Callable

REPO = "dharmx/walls"
# 仓库的默认分支（2026-11-03 用 GitHub 的仓库接口看过：main；仓库 2024-03 以后没再更新）。
BRANCH = "main"
TREE_URL = f"https://api.github.com/repos/{REPO}/git/trees/{BRANCH}?recursive=1"
IMAGE_URLS = (f"https://raw.githubusercontent.com/{REPO}/{BRANCH}/{{path}}",
              f"https://cdn.jsdelivr.net/gh/{REPO}@{BRANCH}/{{path}}")
CREDIT = "壁纸：dharmx/walls"
UA = "RooTV-wallpaper"
PREFIX = "/api/home/wallpaper"

# 文件列表：多久重新取一次；取不到时多久以后再试；回答至多这么大（2026-11 实测 1774 项、约 530 KB）。
LIST_TTL_S = 7 * 86400
LIST_RETRY_S = 6 * 3600
LIST_CAP = 8 * 1024 * 1024
# 池子：至多几张、合计至多多少字节；一张至多多大、至少多大（太小的多半是图标、花纹）。
POOL_MAX = 12
POOL_BYTES = 60 * 1024 * 1024
FILE_MAX = 8 * 1024 * 1024
FILE_MIN = 100 * 1024
# 要横图、够大：宽高比至少 MIN_RATIO，宽至少 MIN_W、高至少 MIN_H。
MIN_RATIO = 1.25
MIN_W = 1280
MIN_H = 640
# 后台：补池子时两张之间隔几秒；取失败了歇几秒（连着失败，加倍，至多 FAIL_MAX_S）；满了以后多久换掉最旧的一张；
# 多久没人要过壁纸就不再取；电脑上有人在看时过几秒再看。
GAP_S = 20
FAIL_GAP_S = 600
FAIL_MAX_S = 6 * 3600
ROTATE_S = 12 * 3600
IDLE_S = 3 * 86400
BUSY_S = 120
TIMEOUT_S = 20
REJECT_KEEP = 500
# 这些文件夹不要：动图和视频、图标和台标、平铺的花纹、杂项；还有家里的小孩看了可能害怕的几个主题。
SKIP_THEMES = frozenset({".github", "animated", "logo", "devicons", "tile", "spam",
                         "weirdcore", "dreamcore", "halloween", "apocalypse", "decay"})

_PATH = re.compile(r"^([A-Za-z0-9._-]{1,40})/([A-Za-z0-9._'-]{1,160})\.(jpe?g|png|webp)$", re.IGNORECASE)
_ID = re.compile(r"^[0-9a-f]{16}$")
_FILE = re.compile(r"^([0-9a-f]{16})\.(jpg|png|webp)$")
EXT = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}


# ---------------------------------------------------------------- 纯函数（测试直接喂样子）

def tree_files(answer) -> list[list]:
    """git trees 接口的回答 → [[路径, 字节数], …]：只要图片、主题文件夹下一层的、不在 SKIP_THEMES 里的、
    大小在 FILE_MIN 和 FILE_MAX 之间的。"""
    found = []
    tree = answer.get("tree") if isinstance(answer, dict) else None
    for t in tree if isinstance(tree, list) else []:
        if not isinstance(t, dict) or t.get("type") != "blob":
            continue
        path, size = str(t.get("path") or ""), t.get("size")
        match = _PATH.match(path)
        if not match or match.group(1).lower() in SKIP_THEMES:
            continue
        if not isinstance(size, int) or isinstance(size, bool) or not FILE_MIN <= size <= FILE_MAX:
            continue
        found.append([path, size])
    return found


def image_size(data: bytes) -> tuple[str, int, int] | None:
    """文件头 → (类型, 宽, 高)：JPEG、PNG、WebP；不是图片、读不出尺寸时 None。只看头，不解码。"""
    if data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR" and len(data) >= 24:
        w, h = struct.unpack(">II", data[16:24])
        return "image/png", w, h
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP" and len(data) >= 30:
        chunk = data[12:16]
        if chunk == b"VP8X":
            return "image/webp", 1 + int.from_bytes(data[24:27], "little"), 1 + int.from_bytes(data[27:30], "little")
        if chunk == b"VP8L" and data[20:21] == b"\x2f":
            bits = int.from_bytes(data[21:25], "little")
            return "image/webp", (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
        if chunk == b"VP8 " and data[23:26] == b"\x9d\x01\x2a":
            w, h = struct.unpack("<HH", data[26:30])
            return "image/webp", w & 0x3FFF, h & 0x3FFF
        return None
    if data[:2] != b"\xff\xd8":
        return None
    i = 2
    while i + 9 < len(data):
        if data[i] != 0xFF:
            return None
        marker = data[i + 1]
        if marker == 0xFF:            # 填充
            i += 1
            continue
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        length = int.from_bytes(data[i + 2:i + 4], "big")
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            h, w = struct.unpack(">HH", data[i + 5:i + 9])
            return "image/jpeg", w, h
        if length < 2:
            return None
        i += 2 + length
    return None


def usable(w: int, h: int) -> bool:
    """横图、够大。"""
    return w >= MIN_W and h >= MIN_H and w >= h * MIN_RATIO


def nice_name(path: str) -> tuple[str, str]:
    """路径 → (主题, 好看一点的名字)：「nature/a_misty_mountain-wallpaper.jpg」→（nature, a misty mountain wallpaper）。"""
    theme, _, name = path.partition("/")
    stem = name.rsplit(".", 1)[0]
    words = " ".join(re.sub(r"[_\-.]+", " ", stem).split())
    return theme, (words if len(words) <= 60 else words[:59] + "…")


def _http_fetch(url: str, headers: dict, cap: int, timeout: float, proxy: str) -> tuple[int, bytes]:
    from .libmeta import tmdb_fetch
    return tmdb_fetch(url, headers, cap, timeout, proxy, site="GitHub")


# ---------------------------------------------------------------- 池子

class Walls:
    """一个中转一个。fetch(url, headers, cap, timeout, proxy) → (状态, 至多 cap+1 字节)，出错抛异常（测试换掉）。"""

    def __init__(self, root: Path, fetch: Callable | None = None, clock=time.time, log: Callable[[str], None] = print,
                 proxy: Callable[[], str] = lambda: "", busy: Callable[[], bool] = lambda: False,
                 background: bool = True, rand: random.Random | None = None):
        self.root = Path(root)
        self.fetch = fetch or _http_fetch
        self.clock = clock
        self.log = log
        self.proxy = proxy
        self.busy = busy
        self.background = background
        self.rand = rand or random.Random()
        self.lock = threading.RLock()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self.asked = 0.0
        self._list_retry = 0.0
        self._fails = 0
        self._rest_until = 0.0
        self.requests = 0
        self._list = self._read("list.json")
        pool = self._read("pool.json")
        self._pool: list[dict] = [p for p in pool.get("items") or [] if self._good_entry(p)]
        # 满了以后从最新的那张算起，过 ROTATE_S 秒再换（重启不会马上换一张）。
        self._last_swap = max([float(p.get("at") or 0) for p in self._pool] or [0.0])
        self._tidy()

    # ---- 存 ----

    def _read(self, name: str) -> dict:
        try:
            data = json.loads((self.root / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _write(self, name: str, data: bytes) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.root, 0o700)
        except OSError:
            pass
        target = self.root / name
        tmp = target.with_name(f".{name}.{os.getpid()}.{threading.get_ident()}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
            os.chmod(tmp, 0o600)
            os.replace(tmp, target)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def _save_json(self, name: str, data: dict) -> None:
        self._write(name, json.dumps(data, ensure_ascii=False).encode("utf-8"))

    def _good_entry(self, p) -> bool:
        return (isinstance(p, dict) and _ID.match(str(p.get("id") or "")) is not None
                and p.get("mime") in EXT and _PATH.match(str(p.get("path") or "")) is not None
                and (self.root / f"{p['id']}.{EXT[p['mime']]}").is_file())

    def _tidy(self) -> None:
        """清单里没有的图片文件删掉（上次换的时候断了电之类）。"""
        keep = {f"{p['id']}.{EXT[p['mime']]}" for p in self._pool}
        try:
            names = [n.name for n in self.root.iterdir()]
        except OSError:
            return
        for name in names:
            if _FILE.match(name) and name not in keep:
                try:
                    (self.root / name).unlink()
                except OSError:
                    pass

    # ---- 给页面 ----

    def pick(self, avoid: str = "") -> dict | None:
        """随便挑一张（有别的就不挑 avoid）；顺便叫后台去补。"""
        with self.lock:
            self.asked = self.clock()
            pool = [p for p in self._pool if p["id"] != avoid] or list(self._pool)
            chosen = dict(self.rand.choice(pool)) if pool else None
        self.kick()
        if chosen is None:
            return None
        theme, name = nice_name(chosen["path"])
        return {"id": chosen["id"], "url": f"api/home/wallpaper/{chosen['id']}", "credit": CREDIT,
                "theme": theme, "name": name, "w": chosen.get("w") or 0, "h": chosen.get("h") or 0}

    def answer(self, avoid: str = "") -> dict:
        found = self.pick(avoid)
        with self.lock:
            count = len(self._pool)
        return {"ok": True, "wallpaper": found, "count": count, "filling": count < POOL_MAX}

    def image(self, wid: str) -> tuple[str, bytes] | None:
        if not _ID.match(wid or ""):
            return None
        with self.lock:
            entry = next((p for p in self._pool if p["id"] == wid), None)
        if entry is None:
            return None
        try:
            return entry["mime"], (self.root / f"{wid}.{EXT[entry['mime']]}").read_bytes()
        except OSError:
            return None

    # ---- 后台 ----

    def kick(self) -> None:
        """有人要壁纸了：后台线程还没起来就起来，正在等就叫醒它。"""
        if not self.background:
            return
        with self.lock:
            if self._thread is None:
                self._thread = threading.Thread(target=self.run_forever, name="wallpaper", daemon=True)
                self._thread.start()
                return
        self._wake.set()

    def run_forever(self) -> None:
        while True:
            try:
                delay = self.step()
            except Exception as error:  # noqa: BLE001 —— 后台线程不能死
                self.log(f"首页壁纸出错：{type(error).__name__}")
                delay = FAIL_GAP_S
            self._wake.wait(max(1.0, delay))
            self._wake.clear()

    def step(self) -> float:
        """做一步（取列表、或者取一张），返回过几秒再来。"""
        now = self.clock()
        with self.lock:
            count, idle = len(self._pool), now - self.asked > IDLE_S
        if idle:
            return 3600
        if now < self._rest_until:
            return self._rest_until - now
        if self.busy():
            return BUSY_S
        if not self._list_fresh(now) and now >= self._list_retry:
            self._refresh_list(now)
        if not self._list.get("files"):
            return max(60.0, self._list_retry - now)
        if count < POOL_MAX:
            if not self._add_one():
                return self._failed()
            self._last_swap = now
            return GAP_S
        if now - self._last_swap >= ROTATE_S:
            if self._add_one():
                self._last_swap = now
                return ROTATE_S
            return self._failed()
        return ROTATE_S - (now - self._last_swap)

    def _failed(self) -> float:
        self._fails += 1
        gap = min(FAIL_MAX_S, FAIL_GAP_S * 2 ** (self._fails - 1))
        self._rest_until = self.clock() + gap
        return gap

    def _list_fresh(self, now: float) -> bool:
        at = self._list.get("at")
        return bool(self._list.get("files")) and isinstance(at, (int, float)) and 0 <= now - at < LIST_TTL_S

    def _get(self, url: str, cap: int, accept: str) -> tuple[int, bytes]:
        self.requests += 1
        return self.fetch(url, {"User-Agent": UA, "Accept": accept}, cap, TIMEOUT_S, self.proxy() or "")

    def _refresh_list(self, now: float) -> bool:
        try:
            status, body = self._get(TREE_URL, LIST_CAP, "application/vnd.github+json")
            answer = json.loads(body) if status == 200 and len(body) <= LIST_CAP else None
        except Exception as error:  # noqa: BLE001 —— 连不上、回答不对：留着旧的列表，过一会儿再试
            self.log(f"首页壁纸：取不到 GitHub 上的文件列表（{type(error).__name__}）")
            answer, status = None, 0
        files = tree_files(answer) if isinstance(answer, dict) else []
        if not files:
            if status:
                self.log(f"首页壁纸：GitHub 的文件列表不能用（HTTP {status}）")
            self._list_retry = now + LIST_RETRY_S
            return False
        if answer.get("truncated"):
            self.log(f"首页壁纸：GitHub 的文件列表没给全，先用给了的 {len(files)} 张")
        rejected = [p for p in self._list.get("rejected") or [] if isinstance(p, str)][-REJECT_KEEP:]
        self._list = {"at": now, "sha": str(answer.get("sha") or "")[:40], "files": files, "rejected": rejected}
        self._list_retry = now + LIST_RETRY_S
        self._save_json("list.json", self._list)
        self.log(f"首页壁纸：GitHub 上的文件列表有 {len(files)} 张能用")
        return True

    def _candidates(self) -> list[list]:
        with self.lock:
            have = {p["path"] for p in self._pool}
        bad = set(self._list.get("rejected") or [])
        return [f for f in self._list.get("files") or [] if isinstance(f, list) and len(f) == 2
                and f[0] not in have and f[0] not in bad]

    def _reject(self, path: str) -> None:
        rejected = [p for p in self._list.get("rejected") or [] if p != path] + [path]
        self._list["rejected"] = rejected[-REJECT_KEEP:]
        self._save_json("list.json", self._list)

    def _add_one(self) -> bool:
        """挑一张池子里没有的，下载、检查、存下；满了就去掉最旧的。返回成没成（不合格的图不算失败）。"""
        files = self._candidates()
        if not files:
            return True
        path, size = self.rand.choice(files)
        if not _PATH.match(path):
            return True
        quoted = urllib.parse.quote(path, safe="/")
        data = None
        for template in IMAGE_URLS:
            try:
                status, body = self._get(template.format(path=quoted), FILE_MAX, "image/*")
            except Exception as error:  # noqa: BLE001 —— 这个来源不行，换下一个
                self.log(f"首页壁纸：下载失败（{urllib.parse.urlsplit(template).hostname}，{type(error).__name__}）")
                continue
            if status == 200 and 0 < len(body) <= FILE_MAX:
                data = body
                break
            if status in (403, 404, 410) or len(body) > FILE_MAX:
                continue
            self.log(f"首页壁纸：下载失败（{urllib.parse.urlsplit(template).hostname}，HTTP {status}）")
        if data is None:
            return False
        self._fails = 0
        found = image_size(data)
        if found is None or not usable(found[1], found[2]):
            self._reject(path)
            return True
        mime, w, h = found
        wid = hashlib.sha256(data).hexdigest()[:16]
        self._write(f"{wid}.{EXT[mime]}", data)
        with self.lock:
            self._pool = [p for p in self._pool if p["id"] != wid and p["path"] != path]
            self._pool.append({"id": wid, "path": path, "mime": mime, "w": w, "h": h, "bytes": len(data),
                               "at": self.clock()})
            dropped = []
            while len(self._pool) > POOL_MAX or (len(self._pool) > 1 and
                                                 sum(p["bytes"] for p in self._pool) > POOL_BYTES):
                oldest = min(self._pool[:-1], key=lambda p: p["at"])
                self._pool.remove(oldest)
                dropped.append(oldest)
            self._save_json("pool.json", {"items": self._pool})
        for p in dropped:
            try:
                (self.root / f"{p['id']}.{EXT[p['mime']]}").unlink()
            except OSError:
                pass
        return True


# ---------------------------------------------------------------- 挂上、web.py 转过来的请求

def attach(relay) -> Walls | None:
    """web.Relay 建好时：家庭服务器和电脑客户端才有，云端为 None。存在数据目录的 wallpapers/。"""
    if getattr(relay, "selfhost", None) is None or getattr(relay, "accounts", None) is None:
        return None
    from .library import _proxy, _watching
    return Walls(Path(relay.accounts.path).parent / "wallpapers", clock=relay.clock,
                 log=lambda message: relay.log(message), proxy=lambda: _proxy(relay), busy=lambda: _watching(relay))


def get(handler, relay, path: str) -> bool:
    if path != PREFIX and not path.startswith(PREFIX + "/"):
        return False
    walls = getattr(relay, "wallpapers", None)
    caller = handler._caller() if walls is not None else None
    if caller is None or caller.guest:
        handler._not_found()
        return True
    if path == PREFIX:
        handler._json(walls.answer(str(handler._query().get("not") or "")), headers={"Cache-Control": "no-store"})
        return True
    found = walls.image(path[len(PREFIX) + 1:])
    if found is None:
        handler._send(404, b"", "text/plain; charset=utf-8", {"Cache-Control": "no-store"})
    else:
        # 地址里的 id 就是内容的指纹：同一个地址永远是同一张图。
        handler._send(200, found[1], found[0], {"Cache-Control": "private, max-age=2592000, immutable"})
    return True
