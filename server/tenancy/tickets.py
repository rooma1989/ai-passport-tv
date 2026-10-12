"""电脑扫码登录（多用户阶段 2）：登录票据、6 位登录码、长轮询，和几个简单的限流。

流程（设计文档第 5.2 节）：
1. 电脑打开 /tv/console，没有登录：要一张票据。票据有三样东西——
   ticket（26 位，放进二维码 HTTPS://<网站>/TV/L/<ticket>）、code（6 位，显示在屏幕上给人输入）、
   poll（只留在电脑的页面里，用来等结果）。二维码被别人拍走也拿不到登录：等结果要用 poll。
2. 已登录的手机扫码（或在遥控里输入 6 位码）：看到电脑的浏览器和网段，点确认 → confirmed。
3. 电脑拿 poll 长轮询：确认了就一次性换成电脑会话（30 天），票据作废（used）。

票据 2 分钟过期、只能用一次；库里只存三样东西的 SHA-256（6 位码除外：它只在 2 分钟内、只对已登录的
用户有用，并且按用户限流）。访客会话不能确认。
"""

from __future__ import annotations

import re
import secrets
import threading
import time
from collections import deque

from . import db

TICKET_S = 120
# 电脑一次等多久（nginx 的 proxy_read_timeout 是 60 秒）。
POLL_WAIT_S = 25
# 同时挂着的长轮询上限，再多的立刻回答「还在等」（电脑 2 秒后再来）：一个线程一个请求。
MAX_POLLERS = 32
TICKET_LEN = 26                                   # 130 位，和 db.CODE_ALPHABET 同一套字母
SHORT_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # 去掉了容易看错的 I L O 0 1
SHORT_LEN = 6
MAX_PENDING = 100
# 过期的票据留一小时（审计看得到），之后删掉。
KEEP_S = 3600
# 每个网段：10 分钟里最多要 12 张票据（页面开着时每 2 分钟自动换一张，最多换 5 次）。
CREATE_LIMIT = (12, 600)
# 每个用户（和每个网段）：10 分钟里最多输错 10 次登录码。
GUESS_LIMIT = (10, 600)

_SHORT = re.compile(r"^[A-Z0-9]{6}$")
_TICKET = re.compile(r"^[A-Z2-7]{26}$")


class Limited(Exception):
    """太频繁：消息直接给用户看。"""


class RateLimit:
    """滑动窗口：window_s 秒里每个键最多 limit 次。只在内存里，重启清零。"""

    def __init__(self, limit: int, window_s: float, clock=time.time):
        self.limit, self.window_s, self.clock = limit, window_s, clock
        self.hits: dict[str, deque] = {}
        self.lock = threading.Lock()

    def _recent(self, key: str) -> deque:
        now = self.clock()
        if len(self.hits) > 4096:
            self.hits = {k: v for k, v in self.hits.items() if v and now - v[-1] < self.window_s}
        times = self.hits.setdefault(key, deque())
        while times and now - times[0] >= self.window_s:
            times.popleft()
        return times

    def blocked(self, key: str) -> bool:
        with self.lock:
            return len(self._recent(key)) >= self.limit

    def hit(self, key: str) -> bool:
        """记一次；已经到上限时不记，返回 False。"""
        with self.lock:
            times = self._recent(key)
            if len(times) >= self.limit:
                return False
            times.append(self.clock())
            return True


def normalize_code(text: str | None) -> str:
    """用户输入的登录码：去掉空格和横线，转大写；格式不对返回空串。"""
    code = re.sub(r"[\s-]", "", str(text or "")).upper()
    return code if _SHORT.match(code) else ""


def show_code(code: str) -> str:
    """K7P4QX → K7P-4QX：屏幕上和手机上都这样显示。"""
    return f"{code[:3]}-{code[3:]}" if len(code) == SHORT_LEN else code


def describe_agent(agent: str) -> str:
    """User-Agent 的大意：「Chrome · macOS」。给手机确认页看，免得点了别人的二维码。"""
    agent = agent or ""
    browser = next((name for pattern, name in (
        (r"MicroMessenger", "微信"), (r"Edg/", "Edge"), (r"OPR/|Opera", "Opera"), (r"Firefox/", "Firefox"),
        (r"Chrome/|CriOS/", "Chrome"), (r"Safari/", "Safari")) if re.search(pattern, agent)), "浏览器")
    system = next((name for pattern, name in (
        (r"iPhone|iPad", "iOS"), (r"Android", "Android"), (r"Windows", "Windows"), (r"Mac OS X|Macintosh", "macOS"),
        (r"CrOS", "ChromeOS"), (r"Linux", "Linux")) if re.search(pattern, agent)), "")
    return f"{browser} · {system}" if system else browser


class LoginTickets:
    def __init__(self, accounts, wait_s: float = POLL_WAIT_S, monotonic=time.monotonic):
        self.accounts = accounts
        self.clock = accounts.clock
        self.monotonic = monotonic
        self.wait_s = wait_s
        self.changed = threading.Condition()
        self.pollers = 0
        self.create_limit = RateLimit(*CREATE_LIMIT, clock=self.clock)
        self.guess_limit = RateLimit(*GUESS_LIMIT, clock=self.clock)

    # ---- 电脑 ----

    def create(self, ip: str = "", user_agent: str = "") -> dict:
        """新票据：{ticket, poll, code, ttl}。太频繁时 Limited。"""
        prefix = db.ip_prefix(ip) or "?"
        if not self.create_limit.hit(prefix):
            raise Limited("刷新得太频繁了，请过几分钟再试")
        accounts = self.accounts
        now = self.clock()
        ticket = "".join(secrets.choice(db.CODE_ALPHABET) for _ in range(TICKET_LEN))
        poll = secrets.token_urlsafe(32)
        with accounts.lock, db.transaction(accounts.conn) as conn:
            conn.execute("DELETE FROM login_ticket WHERE expires_at < ?", (now - KEEP_S,))
            live = {row["short_code"] for row in conn.execute(
                "SELECT short_code FROM login_ticket WHERE expires_at > ? AND state != 'used'", (now,))}
            if len(live) >= MAX_PENDING:
                raise Limited("现在登录的人太多了，请过一会儿再试")
            code = ""
            while not code or code in live:
                code = "".join(secrets.choice(SHORT_ALPHABET) for _ in range(SHORT_LEN))
            conn.execute("INSERT INTO login_ticket(ticket_sha256, short_code, created_at, expires_at, state, "
                         "user_id, pc_ip_prefix, pc_user_agent, poll_sha256) "
                         "VALUES (?, ?, ?, ?, 'pending', NULL, ?, ?, ?)",
                         (db.sha256(ticket), code, now, now + TICKET_S, db.ip_prefix(ip),
                          (user_agent or "")[:160], db.sha256(poll)))
        return {"ticket": ticket, "poll": poll, "code": show_code(code), "ttl": TICKET_S}

    def poll(self, poll: str) -> tuple[str, int | None]:
        """等手机确认，最多 wait_s 秒。返回 (状态, 用户)：

        ok（确认了，票据就此作废；只会回答一次）/ pending / denied（手机上点了取消）/ expired。
        """
        if not isinstance(poll, str) or not 20 <= len(poll) <= 64:
            return "expired", None
        with self.changed:
            wait = self.wait_s if self.pollers < MAX_POLLERS else 0
            self.pollers += 1
        deadline = self.monotonic() + wait
        try:
            while True:
                status, user = self._claim(poll)
                remaining = deadline - self.monotonic()
                if status != "pending" or remaining <= 0:
                    return status, user
                with self.changed:
                    self.changed.wait(min(remaining, 2.0))
        finally:
            with self.changed:
                self.pollers -= 1

    def _claim(self, poll: str) -> tuple[str, int | None]:
        accounts = self.accounts
        with accounts.lock:
            row = accounts.conn.execute("SELECT * FROM login_ticket WHERE poll_sha256=?",
                                        (db.sha256(poll),)).fetchone()
            if row is None or row["expires_at"] <= self.clock():
                return "expired", None
            if row["state"] == "pending":
                return "pending", None
            if row["state"] == "used":
                return ("expired" if row["user_id"] is not None else "denied"), None
            claimed = accounts.conn.execute("UPDATE login_ticket SET state='used' WHERE ticket_sha256=? "
                                            "AND state='confirmed'", (row["ticket_sha256"],)).rowcount
        return ("ok", row["user_id"]) if claimed == 1 else ("expired", None)

    # ---- 手机 ----

    def _pending(self, ticket: str | None = None, code: str | None = None):
        now = self.clock()
        if ticket:
            ticket = ticket.upper()
            if not _TICKET.match(ticket):
                return None
            row = self.accounts.conn.execute("SELECT * FROM login_ticket WHERE ticket_sha256=?",
                                             (db.sha256(ticket),)).fetchone()
        else:
            code = normalize_code(code)
            if not code:
                return None
            row = self.accounts.conn.execute(
                "SELECT * FROM login_ticket WHERE short_code=? AND state='pending' AND expires_at > ? "
                "ORDER BY created_at DESC LIMIT 1", (code, now)).fetchone()
        if row is None or row["state"] != "pending" or row["expires_at"] <= now:
            return None
        return row

    def guess_blocked(self, keys) -> bool:
        return any(self.guess_limit.blocked(key) for key in keys)

    def guess_missed(self, keys) -> None:
        for key in keys:
            self.guess_limit.hit(key)

    def info(self, ticket: str | None = None, code: str | None = None) -> dict | None:
        """手机确认页要显示的：电脑的浏览器、网段、登录码、还剩几秒。票据无效时 None。"""
        with self.accounts.lock:
            row = self._pending(ticket, code)
        if row is None:
            return None
        return {"code": show_code(row["short_code"]), "browser": describe_agent(row["pc_user_agent"]),
                "network": row["pc_ip_prefix"], "expires_in": max(0, int(row["expires_at"] - self.clock()))}

    def answer(self, user_id: int, accept: bool, ticket: str | None = None, code: str | None = None,
               ip: str = "") -> bool:
        """手机上点了确认（accept）或取消。票据无效时 False。"""
        accounts = self.accounts
        with accounts.lock, db.transaction(accounts.conn):
            row = self._pending(ticket, code)
            if row is None:
                return False
            if accept:
                accounts.conn.execute("UPDATE login_ticket SET state='confirmed', user_id=? WHERE ticket_sha256=?",
                                      (user_id, row["ticket_sha256"]))
            else:
                accounts.conn.execute("UPDATE login_ticket SET state='used', user_id=NULL WHERE ticket_sha256=?",
                                      (row["ticket_sha256"],))
        accounts.audit("login.pc.confirm" if accept else "login.pc.deny", user_id, ip, "pc", "",
                       f"pc={row['pc_ip_prefix']} {describe_agent(row['pc_user_agent'])}")
        with self.changed:
            self.changed.notify_all()
        return True
