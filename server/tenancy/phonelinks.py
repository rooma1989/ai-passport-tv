"""让手机登录：已登录的电脑版给手机一个一次性的登录链接（和电脑扫码登录反过来）。

为什么要有：手机上还没有登录过的遥控（没有桌面图标、没有邀请链接），电脑却登录着。
流程：
1. 电脑版点「让手机登录」：要一个链接。三样东西——token（26 位，放进二维码
   HTTPS://<网站>/TV/P/<token>，全大写好让二维码用字母数字模式）、code（8 位，给扫不了码时在
   <网站>/tv/p 里输入）、poll（只留在电脑页面里，看手机登录了没有）。同一个用户再要一个，旧的作废。
2. 手机（iPhone 相机扫码打开 Safari）看到「以 XX 的身份登录这台手机？」，点按钮（POST）才登录：
   链接预览、聊天软件的抓取只 GET，不会把链接用掉。登录得到的是这个用户普通的手机会话（不是访客）。
3. 链接 5 分钟过期、只能用一次；库里只存三样东西的 SHA-256。只有已登录的电脑（成员，不是访客、
   不是密钥）能要；按用户、按网段限流；输错 8 位码按网段和全局限流。创建和使用都记审计。

票据全中转通用：主站和 tv. 子域名上都能用（各自给自己的网站发 cookie）。

同一种链接另有两个来路（origin 列；落地页、用掉的方式都一样）：
- device：设备状态页上的「恢复登录」手势（没有登录着的手机也没有电脑时，设备本身就是凭据）。电视服务替
  已绑定设备的主人要一个，设备全屏显示二维码。每台设备每小时最多 3 个；这台设备新要的让它以前没用掉的作废。
  用这种链接登录的手机会话记作 via=recover，主人别的登录上会提示「有新手机通过设备恢复登录」。
- admin：管理员命令行 `python3 -m server.tenancy.admin login-link <用户 id>`（最后的办法）。
这两种都没有给人输入的 8 位码（库里放的是随机串的哈希，猜不中）。
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

# 设备恢复登录：每台设备每小时最多要这么多个（按库里的记录算，电视服务重启也不清零）。
DEVICE_LIMIT = (3, 3600)

_TOKEN = re.compile(r"^[A-Z2-7]{26}$")
_CODE = re.compile(r"^[A-Z0-9]{8}$")


def normalize_code(text: str | None) -> str:
    code = re.sub(r"[\s-]", "", str(text or "")).upper()
    return code if _CODE.match(code) else ""


def show_code(code: str) -> str:
    """K7P4QX2M → K7P4-QX2M。"""
    return f"{code[:4]}-{code[4:]}" if len(code) == CODE_LEN else code


class NotBound(ValueError):
    """设备没有绑定（或主人被封了）：没有人可以以主人身份登录。"""


class PhoneLinks:
    def __init__(self, accounts):
        self.accounts = accounts
        self.clock = accounts.clock
        self.create_user_limit = RateLimit(*CREATE_LIMIT, clock=self.clock)
        self.create_net_limit = RateLimit(*CREATE_NET_LIMIT, clock=self.clock)
        self.guess_limit = RateLimit(*GUESS_LIMIT, clock=self.clock)
        self.guess_all_limit = RateLimit(*GUESS_ALL_LIMIT, clock=self.clock)

    # ---- 电脑 ----

    def create(self, user_id: int, ip: str = "", user_agent: str = "") -> dict:
        """新链接：{token, code, poll, ttl}；这个用户以前没用掉的链接作废。太频繁时 Limited。"""
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
        accounts.audit("phone_link.create", user_id, ip, "user", str(user_id), describe_agent(user_agent))
        return {"token": token, "code": show_code(code), "poll": poll, "ttl": LINK_S}

    def status(self, user_id: int, poll: str | None) -> dict:
        """电脑看结果：{state: pending/used/expired, phone: 手机的浏览器（用掉了时）}。"""
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
        """落地页要显示的：谁的账号、哪台电脑发起的、还剩几秒。无效时 None（不把链接用掉）。"""
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

    # ---- 设备上的「恢复登录」、管理员的登录链接 ----

    def _insert(self, conn, user_id: int, origin: str, device_id: str | None, ip: str, now: float) -> str:
        """新的一行，返回令牌。没有 8 位码，也没有电脑等结果：两样都存随机串的哈希，用不上也猜不中。"""
        token = "".join(secrets.choice(db.CODE_ALPHABET) for _ in range(TOKEN_LEN))
        conn.execute("INSERT INTO phone_link(token_sha256, code_sha256, poll_sha256, user_id, created_at, "
                     "expires_at, pc_ip_prefix, pc_user_agent, origin, device_id) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?, '', ?, ?)",
                     (db.sha256(token), db.sha256(secrets.token_urlsafe(32)), db.sha256(secrets.token_urlsafe(32)),
                      user_id, now, now + LINK_S, db.ip_prefix(ip), origin, device_id))
        return token

    def create_for_device(self, device_id: str, ip: str = "") -> dict:
        """设备要「恢复登录」：给它的主人一个一次性登录链接 {token, ttl, user_id}。

        设备没绑定、主人被封时 NotBound；这台设备一小时里要满 DEVICE_LIMIT 个时 Limited。
        调用方（电视服务）已经按设备密钥认过这台设备。"""
        accounts = self.accounts
        now = self.clock()
        limit, window = DEVICE_LIMIT
        with accounts.lock, db.transaction(accounts.conn) as conn:
            row = conn.execute(
                "SELECT d.status, m.user_id, u.status AS user_status FROM device d "
                "JOIN device_member m ON m.device_id=d.id AND m.role='owner' JOIN user u ON u.id=m.user_id "
                "WHERE d.id=?", (device_id,)).fetchone()
            if row is None or row["status"] != "active" or row["user_status"] != "active":
                raise NotBound("这台设备还没有主人")
            recent = conn.execute("SELECT COUNT(*) AS n FROM phone_link WHERE origin='device' AND device_id=? "
                                  "AND created_at > ?", (device_id, now - window)).fetchone()["n"]
            if recent >= limit:
                raise Limited("恢复登录要得太频繁了，请一小时后再试")
            conn.execute("DELETE FROM phone_link WHERE expires_at < ?", (now - KEEP_S,))
            conn.execute("UPDATE phone_link SET expires_at=? WHERE origin='device' AND device_id=? "
                         "AND used_at IS NULL AND expires_at > ?", (now, device_id, now))
            user_id = row["user_id"]
            token = self._insert(conn, user_id, "device", device_id, ip, now)
        accounts.audit("phone_link.device", user_id, ip, "device", device_id, f"n={recent + 1}/{limit}")
        return {"token": token, "ttl": LINK_S, "user_id": user_id}

    def create_for_admin(self, user_id: int, actor: int | None = None) -> dict:
        """管理员命令行的 login-link：给这个用户一个一次性登录链接 {token, ttl}。没有这个用户（或被封）时 ValueError。"""
        accounts = self.accounts
        now = self.clock()
        with accounts.lock, db.transaction(accounts.conn) as conn:
            row = conn.execute("SELECT status FROM user WHERE id=?", (user_id,)).fetchone()
            if row is None or row["status"] != "active":
                raise ValueError("没有这个用户，或者他被封了")
            conn.execute("DELETE FROM phone_link WHERE expires_at < ?", (now - KEEP_S,))
            conn.execute("UPDATE phone_link SET expires_at=? WHERE origin='admin' AND user_id=? "
                         "AND used_at IS NULL AND expires_at > ?", (now, user_id, now))
            token = self._insert(conn, user_id, "admin", None, "", now)
        accounts.audit("phone_link.admin", actor, "", "user", str(user_id))
        return {"token": token, "ttl": LINK_S}

    def origin_of(self, token: str | None = None, code: str | None = None) -> dict:
        """一个链接是哪儿来的：{origin: pc/device/admin, device_id, device_name}（找不到时当作 pc）。
        落地页显示「谁发起的」、用掉之后给会话记来路，都用它；不改任何东西。"""
        if token is not None:
            token = str(token).upper()
            key, where = (db.sha256(token), "l.token_sha256=?") if _TOKEN.match(token) else (None, "")
        else:
            code = normalize_code(code)
            key, where = (db.sha256(code), "l.code_sha256=?") if code else (None, "")
        row = None if key is None else self.accounts._one(
            "SELECT l.origin, l.device_id, d.name AS device_name FROM phone_link l "
            f"LEFT JOIN device d ON d.id=l.device_id WHERE {where}", (key,))
        if row is None:
            return {"origin": "pc", "device_id": None, "device_name": ""}
        return {"origin": row["origin"] or "pc", "device_id": row["device_id"], "device_name": row["device_name"] or ""}
