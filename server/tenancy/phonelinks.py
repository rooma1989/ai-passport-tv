"""让手机登录：已登录的电脑版或手机遥控给一部新手机一个一次性的登录链接（和电脑扫码登录反过来）。

为什么要有：手机上还没有登录过的遥控（没有桌面图标、没有邀请链接），电脑或另一部手机却登录着
（比如换了新手机，或家里人想在两部手机上用同一个账号）。
流程：
1. 电脑版点「让手机登录」，或手机遥控「我的」里点「让另一部手机登录」：要一个链接。三样东西——token
   （26 位，放进二维码 HTTPS://<网站>/TV/P/<token>，全大写好让二维码用字母数字模式）、code（8 位，给扫不了码时在
   <网站>/tv/p 里输入）、poll（只留在发起的页面里，看新手机登录了没有）。同一个用户再要一个，旧的作废。
2. 手机（iPhone 相机扫码打开 Safari）看到「以 XX 的身份登录这台手机？」，点按钮（POST）才登录：
   链接预览、聊天软件的抓取只 GET，不会把链接用掉。登录得到的是这个用户普通的手机会话（不是访客）。
3. 链接 5 分钟过期、只能用一次；库里只存三样东西的 SHA-256。只有成员已登录的电脑或手机（不是访客、
   不是密钥）能要；按用户、按网段限流；输错 8 位码按网段和全局限流。创建和使用都记审计（创建时记下是
   电脑还是手机发起的）。列名里的 pc_ 是发起的那一端（以前只有电脑能发起）。

票据全中转通用：主站和 tv. 子域名上都能用（各自给自己的网站发 cookie）。
"""

from __future__ import annotations

import re
import secrets

from . import db
from .tickets import Limited, RateLimit, SHORT_ALPHABET, describe_agent

LINK_S = 300
TOKEN_LEN = 26
CODE_LEN = 8
# 每个用户 10 分钟里最多要 6 个链接，每个网段 12 个。
CREATE_LIMIT = (6, 600)
CREATE_NET_LIMIT = (12, 600)
# 输错（或用掉、过期的）码：每个网段 10 分钟 10 次，全中转 10 分钟 100 次。
GUESS_LIMIT = (10, 600)
GUESS_ALL_LIMIT = (100, 600)
KEEP_S = 3600

_TOKEN = re.compile(r"^[A-Z2-7]{26}$")
_CODE = re.compile(r"^[A-Z0-9]{8}$")


def normalize_code(text: str | None) -> str:
    code = re.sub(r"[\s-]", "", str(text or "")).upper()
    return code if _CODE.match(code) else ""


def show_code(code: str) -> str:
    """K7P4QX2M → K7P4-QX2M。"""
    return f"{code[:4]}-{code[4:]}" if len(code) == CODE_LEN else code


class PhoneLinks:
    def __init__(self, accounts):
        self.accounts = accounts
        self.clock = accounts.clock
        self.create_user_limit = RateLimit(*CREATE_LIMIT, clock=self.clock)
        self.create_net_limit = RateLimit(*CREATE_NET_LIMIT, clock=self.clock)
        self.guess_limit = RateLimit(*GUESS_LIMIT, clock=self.clock)
        self.guess_all_limit = RateLimit(*GUESS_ALL_LIMIT, clock=self.clock)

    # ---- 发起的一端（电脑或手机） ----

    def create(self, user_id: int, ip: str = "", user_agent: str = "", source: str = "pc") -> dict:
        """新链接：{token, code, poll, ttl}；这个用户以前没用掉的链接作废。太频繁时 Limited。
        source：发起的会话是 pc 还是 phone，只记进审计。"""
        prefix = db.ip_prefix(ip) or "?"
        if self.create_user_limit.blocked(f"u{user_id}") or not self.create_net_limit.hit(prefix):
            raise Limited("要得太频繁了，请过几分钟再试")
        self.create_user_limit.hit(f"u{user_id}")
        accounts = self.accounts
        now = self.clock()
        token = "".join(secrets.choice(db.CODE_ALPHABET) for _ in range(TOKEN_LEN))
        code = "".join(secrets.choice(SHORT_ALPHABET) for _ in range(CODE_LEN))
        poll = secrets.token_urlsafe(32)
        with accounts.lock, db.transaction(accounts.conn) as conn:
            conn.execute("DELETE FROM phone_link WHERE expires_at < ?", (now - KEEP_S,))
            conn.execute("UPDATE phone_link SET expires_at=? WHERE user_id=? AND used_at IS NULL AND expires_at > ?",
                         (now, user_id, now))
            conn.execute("INSERT INTO phone_link(token_sha256, code_sha256, poll_sha256, user_id, created_at, "
                         "expires_at, pc_ip_prefix, pc_user_agent) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                         (db.sha256(token), db.sha256(code), db.sha256(poll), user_id, now, now + LINK_S,
                          db.ip_prefix(ip), (user_agent or "")[:160]))
        accounts.audit("phone_link.create", user_id, ip, "user", str(user_id),
                       f"{source} · {describe_agent(user_agent)}")
        return {"token": token, "code": show_code(code), "poll": poll, "ttl": LINK_S}

    def status(self, user_id: int, poll: str | None) -> dict:
        """发起的一端看结果：{state: pending/used/expired, phone: 新手机的浏览器（用掉了时）}。"""
        if not isinstance(poll, str) or not 20 <= len(poll) <= 64:
            return {"state": "expired"}
        row = self.accounts._one("SELECT * FROM phone_link WHERE poll_sha256=? AND user_id=?",
                                 (db.sha256(poll), user_id))
        if row is None:
            return {"state": "expired"}
        if row["used_at"] is not None:
            return {"state": "used", "phone": describe_agent(row["phone_user_agent"]),
                    "network": row["phone_ip_prefix"]}
        if row["expires_at"] <= self.clock():
            return {"state": "expired"}
        return {"state": "pending", "expires_in": int(row["expires_at"] - self.clock())}

    # ---- 手机 ----

    def _keys(self, ip: str) -> tuple[str, str]:
        return (db.ip_prefix(ip) or "?", "*")

    def blocked(self, ip: str) -> bool:
        net, every = self._keys(ip)
        return self.guess_limit.blocked(net) or self.guess_all_limit.blocked(every)

    def missed(self, ip: str) -> None:
        net, every = self._keys(ip)
        self.guess_limit.hit(net)
        self.guess_all_limit.hit(every)

    def _live(self, token: str | None, code: str | None):
        """没用过、没过期、用户没被封的链接；格式不对或找不到时 None。"""
        if token is not None:
            token = str(token).upper()
            if not _TOKEN.match(token):
                return None
            where, key = "l.token_sha256=?", db.sha256(token)
        else:
            code = normalize_code(code)
            if not code:
                return None
            where, key = "l.code_sha256=?", db.sha256(code)
        row = self.accounts.conn.execute(
            "SELECT l.*, u.name, u.status FROM phone_link l JOIN user u ON u.id=l.user_id "
            f"WHERE {where} AND l.used_at IS NULL AND l.expires_at > ?", (key, self.clock())).fetchone()
        return row if row is not None and row["status"] == "active" else None

    def info(self, token: str | None = None, code: str | None = None) -> dict | None:
        """落地页要显示的：谁的账号、哪台电脑或手机发起的、还剩几秒。无效时 None（不把链接用掉）。"""
        with self.accounts.lock:
            row = self._live(token, code)
        if row is None:
            return None
        return {"name": row["name"], "pc": describe_agent(row["pc_user_agent"]), "pc_network": row["pc_ip_prefix"],
                "expires_in": max(0, int(row["expires_at"] - self.clock()))}

    def consume(self, token: str | None = None, code: str | None = None, ip: str = "",
                user_agent: str = "") -> int | None:
        """手机点了「登录」：把链接用掉，返回用户。无效时 None。只会成功一次。"""
        accounts = self.accounts
        with accounts.lock, db.transaction(accounts.conn) as conn:
            row = self._live(token, code)
            if row is None:
                return None
            used = conn.execute("UPDATE phone_link SET used_at=?, phone_ip_prefix=?, phone_user_agent=? "
                                "WHERE token_sha256=? AND used_at IS NULL",
                                (self.clock(), db.ip_prefix(ip), (user_agent or "")[:160],
                                 row["token_sha256"])).rowcount
        if used != 1:
            return None
        return row["user_id"]
