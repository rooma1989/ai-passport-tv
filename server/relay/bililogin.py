"""B 站账号登录：家庭服务器和电脑客户端（中转带 --selfhost）上，每个用户可以扫码登录自己的 B 站账号。

云端没有这些路径（一律 404，电脑版不显示）：别人的 B 站 cookies 不该放在别人的服务器上。

    GET  /api/bili/account              登录了没有、昵称、头像、大会员（导航接口，缓存 NAV_CACHE_S 秒）
    POST /api/bili/login/start          要一个 B 站网页版登录二维码（SVG，server/relay/qr.py 画）
    POST /api/bili/login/poll           问一次扫码的结果：waiting / scanned / expired / done / error
    POST /api/bili/logout               马上删掉这个用户存的 cookies（再顺手请 B 站作废这次登录）
    GET  /api/bili/followings           自己关注的 UP 主（分页取完、缓存 FOLLOW_CACHE_S 秒），标出已订阅的
    POST /api/bili/followings/import    {"mids": [...]}：订阅选中的（受 UP 主个数和频道列表大小的限制）

登录用的是 B 站网页版的扫码登录（passport.bilibili.com/x/passport-login/web/qrcode/generate 和 poll）。
登录成功后存 SESSDATA、bili_jct、DedeUserID(__ckMd5)、sid 和 buvid3/4（refresh_token 也存，留给以后刷新 cookies），
存在数据目录的 secrets/bilibili/<用户 id>.json（目录 0700、文件 0600）。诊断包不带 secrets/，数据库备份也不带。
cookies 不进日志、不进操作记录、不进任何回答；回答里只有昵称、头像地址、大会员与否。

谁能用：自己（用户或管理员）管自己的；访客 404。每个用户只看得到、只用得到自己的登录。
用在哪：播放地址（bilibili.py 的 media_url 的 login）——小电视按设备主人的登录、电脑上看按看的人的登录（没有时按
设备主人的）；会员剧集（大会员登录时剧集列表留着会员集）。cookies 被 B 站拒绝（code -101、导航说没登录）时记成
「登录已失效」，播放退回不登录，页面上请他重新扫码。

别打扰 B 站：扫码结果最多每 POLL_GAP_S 秒真去问一次，二维码 QR_LIFETIME_S 秒后过期就不再问；导航、关注列表有缓存；
出错不重试，等页面上再点。
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

from . import qr
from .bilibili import API, REFERER, UA, SourceError, cover_url

PREFIX = "/api/bili/"
GETS = frozenset({"account", "followings"})
POSTS = frozenset({"login/start", "login/poll", "logout", "followings/import"})
PASSPORT = "https://passport.bilibili.com"
TIMEOUT_S = 8
MAX_ANSWER_BYTES = 2 * 1024 * 1024
# B 站的登录二维码 180 秒过期；页面每 2–3 秒问一次，中转最多每 2 秒真去问 B 站一次。
QR_LIFETIME_S = 180
POLL_GAP_S = 2.0
# 同一个人两次要二维码至少隔几秒。
START_GAP_S = 3.0
# 导航（昵称、头像、大会员）缓存多久；「刷新」至少隔多久真去问。
NAV_CACHE_S = 600
NAV_FRESH_GAP_S = 10
# 播放时发现会员集只给试看：最多这么久核对一次登录还在不在。
RECHECK_GAP_S = 600
# 关注列表：一页 50 个，最多取这么多页（1000 个），页与页之间歇一下；缓存多久。
FOLLOW_PAGE_SIZE = 50
FOLLOW_PAGES = 20
FOLLOW_GAP_S = 0.3
FOLLOW_CACHE_S = 600
# 导入：每个 UP 主要取一次投稿列表，之间歇一下。
IMPORT_GAP_S = 0.3
# 存下来的 cookie：这些名字，值只许这些字符（不能有换行、分号，免得拼进请求头出事）。
COOKIE_NAMES = ("SESSDATA", "bili_jct", "DedeUserID", "DedeUserID__ckMd5", "sid", "buvid3", "buvid4")
_COOKIE_VALUE = re.compile(r"^[A-Za-z0-9%_.,*:+/=-]{1,512}$")
_MID = re.compile(r"^[0-9]{1,20}$")
_TOKEN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")

UNREACHABLE = "B 站连不上：检查路由器或代理"
REFUSED = "B 站拒绝了这次请求（可能太频繁），请过几分钟再试"
INVALID = "登录已失效，请重新扫码"
STATES = {"waiting": "等待扫码", "scanned": "已扫码，请在手机上确认", "expired": "二维码过期",
          "done": "登录成功", "error": UNREACHABLE}
# 扫码结果接口里的 data.code。
POLL_CODES = {0: "done", 86101: "waiting", 86090: "scanned", 86038: "expired"}


def request(url: str, cookie: str = "", data: dict | None = None, timeout: float = TIMEOUT_S):
    """(JSON 回答, 回答里 Set-Cookie 的 {名字: 值})。data 不为 None 时是表单 POST。"""
    headers = {"User-Agent": UA, "Referer": REFERER}
    if cookie:
        headers["Cookie"] = cookie
    body = None
    if data is not None:
        body = urllib.parse.urlencode(data).encode("ascii")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=body, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as answer:
        cookies = {}
        for line in answer.headers.get_all("Set-Cookie") or []:
            name, _, rest = line.partition("=")
            cookies[name.strip()] = rest.split(";", 1)[0].strip()
        return json.loads(answer.read(MAX_ANSWER_BYTES)), cookies


class Login:
    """一个用户的 B 站登录，交给 bilibili.py 带进请求。repr 里没有 cookies（免得哪里打日志打出来）。"""

    __slots__ = ("user", "vip", "_cookies", "_on_rejected")

    def __init__(self, user: int, cookies: dict, vip: bool = False, on_rejected=None):
        self.user = user
        self.vip = bool(vip)
        self._cookies = {k: v for k, v in cookies.items() if k in COOKIE_NAMES}
        self._on_rejected = on_rejected

    def header(self) -> str:
        return "; ".join(f"{name}={self._cookies[name]}" for name in COOKIE_NAMES if name in self._cookies)

    @property
    def csrf(self) -> str:
        return self._cookies.get("bili_jct", "")

    def rejected(self) -> None:
        if self._on_rejected is not None:
            self._on_rejected()

    def __repr__(self) -> str:
        return f"<Login user={self.user} vip={self.vip}>"

    __str__ = __repr__


def clean_cookies(found: dict) -> dict:
    """只留认识的名字、像样的值。"""
    return {name: str(found[name]) for name in COOKIE_NAMES
            if name in found and _COOKIE_VALUE.match(str(found[name]))}


def cookies_from_url(url: str) -> dict:
    """扫码成功时 data.url 的查询串里也带着 cookies。值保持原样（SESSDATA 是编码过的，cookie 里也这样用）。"""
    query = urllib.parse.urlsplit(str(url or "")).query
    found = {}
    for part in query.split("&"):
        name, sep, value = part.partition("=")
        if sep and name in COOKIE_NAMES:
            found[name] = value
    return found


def profile_of(nav: dict) -> dict | None:
    """导航接口的 data → 给页面看的 {"mid", "name", "face", "vip", "vip_label", "level"}；没登录时 None。"""
    if not isinstance(nav, dict) or not nav.get("isLogin"):
        return None
    vip = nav.get("vipStatus") == 1 or (nav.get("vip") or {}).get("status") == 1
    label = ""
    if vip:
        label = str(((nav.get("vip_label") or {}).get("text")) or "大会员")[:12]
    level = (nav.get("level_info") or {}).get("current_level")
    mid = str(nav.get("mid") or "")
    return {"mid": mid if _MID.match(mid) else "", "name": str(nav.get("uname") or "")[:40],
            "face": cover_url(nav.get("face")), "vip": bool(vip), "vip_label": label,
            "level": level if isinstance(level, int) and not isinstance(level, bool) else 0}


class Store:
    """secrets/bilibili/<用户 id>.json：一个用户一个文件。"""

    def __init__(self, root: Path):
        self.root = Path(root)

    def path(self, user: int) -> Path:
        return self.root / f"{int(user)}.json"

    def load(self, user: int) -> dict | None:
        try:
            record = json.loads(self.path(user).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(record, dict) or record.get("user") != int(user):
            return None
        cookies = clean_cookies(record.get("cookies") or {})
        if "SESSDATA" not in cookies:
            return None
        record["cookies"] = cookies
        return record

    def save(self, user: int, record: dict) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.root, 0o700)
        except OSError:
            pass
        target = self.path(user)
        tmp = target.with_name(f".{target.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(dict(record, user=int(user)), handle, ensure_ascii=False)
            os.chmod(tmp, 0o600)
            os.replace(tmp, target)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def delete(self, user: int) -> bool:
        try:
            self.path(user).unlink()
            return True
        except FileNotFoundError:
            return False


class BiliAccounts:
    """一个中转里所有用户的 B 站登录：存储、扫码、导航、关注列表。"""

    def __init__(self, root: Path, fetch=request, clock=time.time, monotonic=time.monotonic, sleep=time.sleep,
                 log=lambda _m: None):
        self.store = Store(root)
        self.fetch = fetch
        self.clock = clock
        self.monotonic = monotonic
        self.sleep = sleep
        self.log = log
        self.lock = threading.RLock()
        self._records: dict[int, dict | None] = {}
        # 正在扫码的：用户 → {"key", "svg", "started", "polled", "state"}（qrcode_key 不出这个对象）。
        self._pending: dict[int, dict] = {}
        self._started: dict[int, float] = {}
        self._nav_fresh: dict[int, float] = {}
        self._rechecked: dict[int, float] = {}
        self._follows: dict[int, tuple[float, list[dict], int]] = {}
        # 同一个人同一时间只做一件要访问 B 站的事（导入、取关注列表）。
        self._busy: set[int] = set()

    # ---- 存的登录 ----

    def record(self, user: int) -> dict | None:
        with self.lock:
            if user not in self._records:
                self._records[user] = self.store.load(user)
            return self._records[user]

    def _save(self, user: int, record: dict) -> None:
        with self.lock:
            self.store.save(user, record)
            self._records[user] = self.store.load(user)

    def login_for(self, user: int | None) -> Login | None:
        """这个用户能用的登录；没登录、登录失效了是 None（播放退回不登录）。"""
        if user is None:
            return None
        record = self.record(user)
        if record is None or record.get("invalid"):
            return None
        profile = record.get("profile") or {}
        return Login(user, record["cookies"], vip=bool(profile.get("vip")),
                     on_rejected=lambda: self.mark_invalid(user))

    def status(self, user: int | None) -> dict:
        """/api/state 里的一小段：电脑版据此显示「B 站账号」和导入按钮。"""
        record = self.record(user) if user is not None else None
        if record is None:
            return {"login": False, "invalid": False, "vip": False}
        return {"login": True, "invalid": bool(record.get("invalid")),
                "vip": bool((record.get("profile") or {}).get("vip"))}

    def mark_invalid(self, user: int) -> None:
        with self.lock:
            record = self.record(user)
            if record is None or record.get("invalid"):
                return
            record = dict(record, invalid=True)
            try:
                self._save(user, record)
            except OSError:
                self._records[user] = record
            self._follows.pop(user, None)
        self.log(f"用户 {user} 的 B 站登录失效了，退回不登录")

    def forget(self, user: int) -> bool:
        """删掉这个用户的登录（退出登录、总后台删用户）。"""
        with self.lock:
            removed = self.store.delete(user)
            self._records.pop(user, None)
            self._pending.pop(user, None)
            self._follows.pop(user, None)
        return removed

    # ---- 导航 ----

    def _nav(self, cookie: str) -> dict:
        answer, _ = self.fetch(f"{API}/x/web-interface/nav", cookie)
        if not isinstance(answer, dict):
            raise ValueError("导航接口的回答不对")
        return answer

    def account(self, user: int, fresh: bool = False) -> dict:
        record = self.record(user)
        if record is None:
            return {"ok": True, "login": False}
        now = self.clock()
        stale = now - float(record.get("checked_at") or 0) >= NAV_CACHE_S
        if fresh and self.monotonic() - self._nav_fresh.get(user, -1e9) >= NAV_FRESH_GAP_S:
            stale = True
            self._nav_fresh[user] = self.monotonic()
        error = ""
        if stale and not record.get("invalid"):
            error = self._check(user, record)
            record = self.record(user) or record
        return self._describe(record, error)

    def _check(self, user: int, record: dict) -> str:
        """问一次导航：还登录着就更新昵称、大会员；说没登录就记成失效。返回给人看的错误（没有为空）。"""
        try:
            answer = self._nav(Login(user, record["cookies"]).header())
        except (OSError, ValueError) as error:
            self.log(f"用户 {user} 问 B 站导航失败：{type(error).__name__}")
            return UNREACHABLE
        profile = profile_of(answer.get("data")) if answer.get("code") == 0 else None
        if profile is None:
            if answer.get("code") in (0, -101):
                self.mark_invalid(user)
                return ""
            return REFUSED
        try:
            self._save(user, dict(record, profile=profile, checked_at=self.clock(), invalid=False))
        except OSError:
            pass
        return ""

    def recheck(self, user: int) -> bool:
        """会员集只给了试看：核对一次（最多每 RECHECK_GAP_S 秒）。返回登录是不是已经失效了。"""
        record = self.record(user)
        if record is None:
            return False
        if record.get("invalid"):
            return True
        if self.monotonic() - self._rechecked.get(user, -1e9) >= RECHECK_GAP_S:
            self._rechecked[user] = self.monotonic()
            self._check(user, record)
        record = self.record(user)
        return bool(record is not None and record.get("invalid"))

    def _describe(self, record: dict, error: str = "") -> dict:
        profile = record.get("profile") or {}
        answer = {"ok": True, "login": True, "invalid": bool(record.get("invalid")),
                  "name": profile.get("name") or "", "face": profile.get("face") or "",
                  "vip": bool(profile.get("vip")), "vip_label": profile.get("vip_label") or "",
                  "level": profile.get("level") or 0, "saved_at": record.get("saved_at") or 0}
        if answer["invalid"]:
            answer["message"] = INVALID
        if error:
            answer["error"] = error
        return answer

    # ---- 扫码登录 ----

    def start(self, user: int) -> dict:
        now = self.monotonic()
        with self.lock:
            if now - self._started.get(user, -1e9) < START_GAP_S:
                pending = self._pending.get(user)
                if pending is not None:
                    return {"ok": True, "svg": pending["svg"], "state": pending["state"],
                            "text": STATES[pending["state"]], "expires": self._left(pending)}
                return {"ok": False, "error": "点得太快了，过几秒再试"}
            self._started[user] = now
        try:
            answer, _ = self.fetch(f"{PASSPORT}/x/passport-login/web/qrcode/generate?source=main-fe-header", "")
            data = answer.get("data") if isinstance(answer, dict) and answer.get("code") == 0 else None
            key, url = str((data or {}).get("qrcode_key") or ""), str((data or {}).get("url") or "")
        except (OSError, ValueError, AttributeError) as error:
            self.log(f"用户 {user} 要 B 站登录二维码失败：{type(error).__name__}")
            return {"ok": False, "error": UNREACHABLE}
        if not _TOKEN.match(key) or not url.startswith("https://") or len(url) > 400:
            return {"ok": False, "error": REFUSED}
        pending = {"key": key, "svg": qr.svg(url), "started": now, "polled": -1e9, "state": "waiting"}
        with self.lock:
            self._pending[user] = pending
        return {"ok": True, "svg": pending["svg"], "state": "waiting", "text": STATES["waiting"],
                "expires": QR_LIFETIME_S}

    def _left(self, pending: dict) -> int:
        return max(0, int(QR_LIFETIME_S - (self.monotonic() - pending["started"])))

    def poll(self, user: int) -> dict:
        with self.lock:
            pending = self._pending.get(user)
            if pending is None:
                return {"ok": True, "state": "expired", "text": STATES["expired"]}
            now = self.monotonic()
            if now - pending["started"] >= QR_LIFETIME_S:
                self._pending.pop(user, None)
                return {"ok": True, "state": "expired", "text": STATES["expired"]}
            if now - pending["polled"] < POLL_GAP_S:
                return {"ok": True, "state": pending["state"], "text": STATES[pending["state"]],
                        "expires": self._left(pending)}
            pending["polled"] = now
            key = pending["key"]
        try:
            answer, set_cookies = self.fetch(
                f"{PASSPORT}/x/passport-login/web/qrcode/poll?{urllib.parse.urlencode({'qrcode_key': key})}", "")
            data = answer.get("data") if isinstance(answer, dict) and answer.get("code") == 0 else None
        except (OSError, ValueError, AttributeError) as error:
            self.log(f"用户 {user} 问 B 站扫码结果失败：{type(error).__name__}")
            return {"ok": True, "state": "error", "text": UNREACHABLE, "error": UNREACHABLE,
                    "expires": self._left(pending)}
        if not isinstance(data, dict):
            return {"ok": True, "state": "error", "text": REFUSED, "error": REFUSED, "expires": self._left(pending)}
        state = POLL_CODES.get(data.get("code"), "error")
        if state != "done":
            with self.lock:
                if state == "expired":
                    self._pending.pop(user, None)
                elif state != "error":
                    pending["state"] = state
            text = STATES[state] if state != "error" else REFUSED
            return {"ok": True, "state": state, "text": text, "expires": self._left(pending)}
        found = clean_cookies(dict(cookies_from_url(data.get("url")), **(set_cookies or {})))
        with self.lock:
            self._pending.pop(user, None)
        if "SESSDATA" not in found or "bili_jct" not in found:
            return {"ok": True, "state": "error", "text": REFUSED, "error": REFUSED}
        token = str(data.get("refresh_token") or "")
        self._finish(user, found, token if _TOKEN.match(token) else "")
        return dict(self.account(user), state="done", text=STATES["done"])

    def _finish(self, user: int, cookies: dict, refresh_token: str) -> None:
        """登录成功：顺手要 buvid3/4（网页版也有），问一次导航拿昵称和大会员，存起来。"""
        if "buvid3" not in cookies:
            try:
                answer, _ = self.fetch(f"{API}/x/frontend/finger/spi", "")
                spi = answer.get("data") or {}
                cookies.update(clean_cookies({"buvid3": spi.get("b_3"), "buvid4": spi.get("b_4")}))
            except (OSError, ValueError, AttributeError):
                pass
        record = {"version": 1, "cookies": cookies, "refresh_token": refresh_token, "saved_at": self.clock(),
                  "checked_at": 0, "invalid": False, "profile": {}}
        self._save(user, record)
        with self.lock:
            self._follows.pop(user, None)
        self._check(user, self.record(user) or record)
        self.log(f"用户 {user} 登录了 B 站")

    def logout(self, user: int) -> dict:
        record = self.record(user)
        removed = self.forget(user)
        if record is not None and not record.get("invalid"):
            # 顺手请 B 站作废这次登录（连不上也没关系：本机的已经删了）。
            login = Login(user, record["cookies"])
            threading.Thread(target=self._exit, args=(login,), daemon=True).start()
        if removed:
            self.log(f"用户 {user} 退出了 B 站登录")
        return {"ok": True, "login": False}

    def _exit(self, login: Login) -> None:
        try:
            self.fetch(f"{PASSPORT}/login/exit/v2", login.header(), data={"biliCSRF": login.csrf})
        except (OSError, ValueError):
            pass

    # ---- 关注的 UP 主 ----

    def followings(self, user: int) -> dict:
        """自己关注的 UP 主：[{"mid", "name", "face", "sign"}]。缓存 FOLLOW_CACHE_S 秒。"""
        record = self.record(user)
        if record is None or record.get("invalid"):
            return {"ok": False, "login": False, "error": INVALID if record else "先扫码登录 B 站"}
        mid = str(record["cookies"].get("DedeUserID") or (record.get("profile") or {}).get("mid") or "")
        if not _MID.match(mid):
            return {"ok": False, "error": INVALID}
        with self.lock:
            cached = self._follows.get(user)
            if cached is not None and self.monotonic() - cached[0] < FOLLOW_CACHE_S:
                return {"ok": True, "items": cached[1], "total": cached[2]}
            if user in self._busy:
                return {"ok": False, "error": "正在取关注列表，稍等"}
            self._busy.add(user)
        try:
            return self._fetch_followings(user, mid, Login(user, record["cookies"]))
        finally:
            with self.lock:
                self._busy.discard(user)

    def _fetch_followings(self, user: int, mid: str, login: Login) -> dict:
        items, seen, total = [], set(), 0
        for page in range(1, FOLLOW_PAGES + 1):
            if page > 1:
                self.sleep(FOLLOW_GAP_S)
            query = urllib.parse.urlencode({"vmid": mid, "pn": page, "ps": FOLLOW_PAGE_SIZE, "order": "desc"})
            try:
                answer, _ = self.fetch(f"{API}/x/relation/followings?{query}", login.header())
            except (OSError, ValueError) as error:
                self.log(f"用户 {user} 取 B 站关注列表失败：{type(error).__name__}")
                return {"ok": False, "error": UNREACHABLE}
            if not isinstance(answer, dict):
                return {"ok": False, "error": REFUSED}
            if answer.get("code") == -101:
                self.mark_invalid(user)
                return {"ok": False, "login": False, "error": INVALID}
            if answer.get("code") != 0:
                return {"ok": False, "error": REFUSED}
            data = answer.get("data") or {}
            rows = data.get("list") or []
            if isinstance(data.get("total"), int):
                total = data["total"]
            for row in rows:
                if not isinstance(row, dict):
                    continue
                up = str(row.get("mid") or "")
                if not _MID.match(up) or up in seen:
                    continue
                seen.add(up)
                items.append({"mid": up, "name": " ".join(str(row.get("uname") or "").split())[:40] or up,
                              "face": cover_url(row.get("face")),
                              "sign": " ".join(str(row.get("sign") or "").split())[:80]})
            if len(rows) < FOLLOW_PAGE_SIZE or (total and len(items) >= total):
                break
        total = max(total, len(items))
        with self.lock:
            self._follows[user] = (self.monotonic(), items, total)
        return {"ok": True, "items": items, "total": total}

    def import_followings(self, relay, caller, mids) -> dict:
        """订阅选中的 UP 主。UP 主最多 state.MAX_CREATORS 个、频道列表要放得下；超出的不加，说清楚。"""
        from .state import MAX_CREATORS
        from .table import label_for
        user = caller.user
        if not isinstance(mids, list) or not mids or len(mids) > 200 \
                or not all(isinstance(m, str) and _MID.match(m) for m in mids):
            return {"ok": False, "error": "请求格式不对"}
        listed = self.followings(user)
        if not listed.get("ok"):
            return listed
        names = {item["mid"]: item["name"] for item in listed["items"]}
        state = relay._account(caller)
        client = relay.clients.get("bilibili")
        with state.lock:
            have = {c.source for c in state.creators if c.site == "bilibili" and c.kind == "up"}
            count = sum(c.kind == "up" for c in state.creators)
        added, skipped = [], []
        wanted = [m for m in dict.fromkeys(mids)]
        with self.lock:
            if user in self._busy:
                return {"ok": False, "error": "正在导入，稍等"}
            self._busy.add(user)
        try:
            for n, mid in enumerate(wanted):
                name = names.get(mid)
                if name is None:
                    skipped.append({"mid": mid, "name": mid, "reason": "不在你的关注列表里"})
                    continue
                if mid in have:
                    skipped.append({"mid": mid, "name": name, "reason": "已经订阅了"})
                    continue
                if count >= MAX_CREATORS:
                    skipped.append({"mid": mid, "name": name, "reason": f"最多 {MAX_CREATORS} 个 UP 主"})
                    continue
                if not relay.room(label_for(name, "bilibili"), caller)["fits"]:
                    skipped.append({"mid": mid, "name": name, "reason": "小电视的频道列表满了"})
                    continue
                if added or n:
                    self.sleep(IMPORT_GAP_S)
                try:
                    videos = client.uploads(mid)
                except SourceError as error:
                    relay.log(f"导入 UP 主 {mid} 失败：{error}")
                    skipped.append({"mid": mid, "name": name,
                                    "reason": "B 站拒绝了请求" if getattr(error, "permanent", False) else UNREACHABLE})
                    if not getattr(error, "permanent", False):
                        # 连不上：后面的也不会好，别一个个等超时。
                        for rest in wanted[n + 1:]:
                            skipped.append({"mid": rest, "name": names.get(rest, rest), "reason": UNREACHABLE})
                        break
                    continue
                if not videos:
                    skipped.append({"mid": mid, "name": name, "reason": "没有投稿"})
                    continue
                try:
                    creator = state.add("bilibili", mid, name, videos, relay.clock())
                except ValueError as error:
                    skipped.append({"mid": mid, "name": name, "reason": str(error)})
                    continue
                state.set_total(creator.id, relay._upload_total("bilibili", mid))
                relay._cache_videos(creator, creator.videos, creator.fetched_at)
                have.add(mid)
                count += 1
                added.append({"mid": mid, "name": name, "id": creator.id})
        finally:
            with self.lock:
                self._busy.discard(user)
        if added:
            state.save()
            relay._tables_changed(state)
            relay.log(f"从 B 站关注导入了 {len(added)} 个 UP 主")
        return {"ok": True, "added": added, "skipped": skipped, "max": MAX_CREATORS}


# ---------------------------------------------------------------- web.py 转过来的请求

def _caller(handler, relay):
    """能用的调用者（自建、有账号、不是访客），否则 None。"""
    if relay.selfhost is None or getattr(relay, "bili_accounts", None) is None:
        return None
    caller = handler._caller()
    if caller is None or caller.guest or caller.user is None:
        return None
    return caller


def get(handler, relay, path: str) -> bool:
    if not path.startswith(PREFIX) or path[len(PREFIX):] not in GETS:
        return False
    caller = _caller(handler, relay)
    if caller is None:
        handler._not_found()
        return True
    accounts = relay.bili_accounts
    if path.endswith("/account"):
        handler._json(accounts.account(caller.user, fresh=handler._query().get("fresh") == "1"))
        return True
    answer = accounts.followings(caller.user)
    if answer.get("ok"):
        state = relay._account(caller)
        with state.lock:
            have = {c.source for c in state.creators if c.site == "bilibili" and c.kind == "up"}
            count = sum(c.kind == "up" for c in state.creators)
        from .state import MAX_CREATORS
        answer = dict(answer, items=[dict(item, subscribed=item["mid"] in have) for item in answer["items"]],
                      count=count, max=MAX_CREATORS, room=relay.room(caller=caller))
    handler._json(answer)
    return True


def _read_body(handler) -> dict | None:
    try:
        length = int(handler.headers.get("Content-Length") or 0)
    except ValueError:
        return None
    if length == 0:
        return {}
    if not 0 < length <= 16 * 1024:
        return None
    try:
        body = json.loads(handler.rfile.read(length))
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


def post(handler, relay, path: str) -> bool:
    if not path.startswith(PREFIX) or path[len(PREFIX):] not in POSTS:
        return False
    name = path[len(PREFIX):]
    body = _read_body(handler)
    caller = _caller(handler, relay)
    if caller is None:
        handler._not_found()
        return True
    if body is None:
        handler._json({"ok": False, "error": "请求格式不对"}, 400)
        return True
    accounts = relay.bili_accounts
    if name == "login/start":
        handler._json(accounts.start(caller.user))
    elif name == "login/poll":
        answer = accounts.poll(caller.user)
        if answer.get("state") == "done":
            relay.accounts.audit("bili.login", caller.user, handler._ip(), "user", str(caller.user))
        handler._json(answer)
    elif name == "logout":
        if accounts.record(caller.user) is not None:
            relay.accounts.audit("bili.logout", caller.user, handler._ip(), "user", str(caller.user))
        handler._json(accounts.logout(caller.user))
    else:
        handler._json(accounts.import_followings(relay, caller, body.get("mids")))
    return True
