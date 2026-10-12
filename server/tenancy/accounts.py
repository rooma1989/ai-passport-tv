"""账号、会话、邀请、设备、成员、每个用户和每台设备的状态、共享的视频列表缓存、操作记录。

一个 Accounts 对象持有一条连接，所有方法在同一把锁里执行：中转是多线程的 HTTP 服务，
请求都很短，排队比连接池简单。电视服务进程另开自己的 Accounts（见 gate.py）。

错误给用户看的部分用中文（ValueError 的消息会原样显示在手机上）。
"""

from __future__ import annotations

import json
import re
import secrets
import threading
import time
from pathlib import Path

from . import db

# 会话有效期：手机和以前一样 180 天；电脑（阶段 2）和访客 30 天。
SESSION_S = {"phone": 180 * 24 * 3600, "pc": 30 * 24 * 3600, "guest": 30 * 24 * 3600}
# 会话的「最后使用时间」最多每分钟写一次，免得每次刷新都写库。
LAST_SEEN_STEP_S = 60
INVITE_S = 7 * 24 * 3600
# 重新配对的窗口：这段时间里设备用新的密钥连上来，就记住新的。
REPAIR_S = 10 * 60
NAME_MAX = 20
MODEL_NAMES = {"xingzhi-cube-154": "小智", "ai-passport": "Passport"}
_MODEL = re.compile(r"^[a-z0-9-]{1,32}$")


def _clean_name(name: str) -> str:
    return " ".join(str(name or "").split())[:NAME_MAX]


def default_device_name(model: str, device_id: str) -> str:
    return f"{MODEL_NAMES.get(model, '小电视')} {device_id[-4:].upper()}"


class Accounts:
    def __init__(self, path: Path | str, clock=time.time):
        self.path = Path(path)
        self.clock = clock
        self.conn = db.connect(self.path)
        self.lock = threading.RLock()

    def close(self) -> None:
        with self.lock:
            self.conn.close()

    def _one(self, sql: str, args=()) -> dict | None:
        with self.lock:
            row = self.conn.execute(sql, args).fetchone()
        return dict(row) if row is not None else None

    def _all(self, sql: str, args=()) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def _run(self, sql: str, args=()) -> int:
        with self.lock:
            return self.conn.execute(sql, args).rowcount

    # ---- 设置 ----

    def meta(self, key: str, default: str = "") -> str:
        row = self._one("SELECT value FROM meta WHERE key=?", (key,))
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        self._run("INSERT INTO meta(key, value) VALUES (?, ?) "
                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))

    def is_empty(self) -> bool:
        """没有用户也没有设备：迁移只在空库上跑。"""
        return self._one("SELECT (SELECT COUNT(*) FROM user) + (SELECT COUNT(*) FROM device) AS n")["n"] == 0

    # ---- 用户 ----

    def create_user(self, name: str, role: str = "user") -> int:
        if role not in ("admin", "user"):
            raise ValueError("没有这个角色")
        with self.lock:
            cursor = self.conn.execute("INSERT INTO user(name, role, status, created_at) VALUES (?, ?, 'active', ?)",
                                       (_clean_name(name) or "用户", role, self.clock()))
            return int(cursor.lastrowid)

    def user(self, user_id: int) -> dict | None:
        return self._one("SELECT * FROM user WHERE id=?", (user_id,))

    def users(self) -> list[dict]:
        return self._all("SELECT * FROM user ORDER BY id")

    def admin_id(self) -> int | None:
        row = self._one("SELECT id FROM user WHERE role='admin' AND status='active' ORDER BY id LIMIT 1")
        return row["id"] if row else None

    def set_user_status(self, user_id: int, status: str, actor: int | None = None) -> bool:
        if status not in ("active", "banned"):
            raise ValueError("没有这个状态")
        with self.lock, db.transaction(self.conn):
            changed = self.conn.execute("UPDATE user SET status=? WHERE id=?", (status, user_id)).rowcount
            if changed and status == "banned":
                # 封禁同时结束这个人的所有会话。
                self.conn.execute("DELETE FROM session WHERE user_id=?", (user_id,))
        if changed:
            self.audit("user." + ("ban" if status == "banned" else "unban"), actor, target_kind="user",
                       target_id=str(user_id))
        return bool(changed)

    # ---- 会话 ----

    def new_session(self, user_id: int | None, kind: str = "phone", device_id: str | None = None,
                    user_agent: str = "", ip: str = "") -> str:
        if kind not in SESSION_S:
            raise ValueError(f"bad session kind {kind!r}")
        if (kind == "guest") != (device_id is not None and user_id is None):
            raise ValueError("a guest session has a device and no user")
        token = secrets.token_urlsafe(32)
        now = self.clock()
        self._run("INSERT INTO session(token_sha256, user_id, kind, device_id, created_at, last_seen, "
                  "expires_at, user_agent, ip_prefix) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                  (db.sha256(token), user_id, kind, device_id, now, now, now + SESSION_S[kind],
                   (user_agent or "")[:160], db.ip_prefix(ip)))
        return token

    def import_session(self, token: str, user_id: int, created: float) -> bool:
        """迁移：旧状态文件里的手机会话（明文）只存哈希，有效期从原来的登录时间算。"""
        with self.lock:
            cursor = self.conn.execute(
                "INSERT OR IGNORE INTO session(token_sha256, user_id, kind, device_id, created_at, last_seen, "
                "expires_at) VALUES (?, ?, 'phone', NULL, ?, ?, ?)",
                (db.sha256(token), user_id, created, created, created + SESSION_S["phone"]))
            return cursor.rowcount == 1

    def session(self, token: str | None) -> dict | None:
        """有效的会话：{user_id, kind, device_id, role, name}；访客的 role 是 guest。"""
        if not token or len(token) > 128:
            return None
        now = self.clock()
        row = self._one(
            "SELECT s.token_sha256, s.user_id, s.kind, s.device_id, s.expires_at, s.last_seen, "
            "u.role, u.status AS user_status, u.name, d.status AS device_status "
            "FROM session s LEFT JOIN user u ON u.id=s.user_id LEFT JOIN device d ON d.id=s.device_id "
            "WHERE s.token_sha256=?", (db.sha256(token),))
        if row is None or row["expires_at"] <= now:
            return None
        if row["kind"] == "guest":
            if row["device_status"] != "active":
                return None
            role, name = "guest", "访客"
        else:
            if row["user_status"] != "active":
                return None
            role, name = row["role"], row["name"]
        if now - row["last_seen"] >= LAST_SEEN_STEP_S:
            self._run("UPDATE session SET last_seen=? WHERE token_sha256=?", (now, row["token_sha256"]))
        return {"user_id": row["user_id"], "kind": row["kind"], "device_id": row["device_id"],
                "role": role, "name": name}

    def revoke_session(self, token: str) -> bool:
        return self._run("DELETE FROM session WHERE token_sha256=?", (db.sha256(token),)) > 0

    def sessions_of(self, user_id: int) -> list[dict]:
        return self._all("SELECT kind, device_id, created_at, last_seen, expires_at, user_agent, ip_prefix "
                         "FROM session WHERE user_id=? ORDER BY last_seen DESC", (user_id,))

    # 阶段 2：电脑版「设置」里的登录列表。id 是令牌哈希的前 16 个十六进制数字：认得出是哪一个，
    # 又推不出令牌本身。列出自己的会话，加上自己（主人）设备上的访客会话。

    def session_list(self, user_id: int, current: str | None = None) -> list[dict]:
        mine = db.sha256(current) if current else b""
        rows = self._all(
            "SELECT s.token_sha256, s.kind, s.device_id, s.created_at, s.last_seen, s.user_agent, s.ip_prefix, "
            "d.name AS device_name FROM session s LEFT JOIN device d ON d.id=s.device_id "
            "WHERE s.expires_at > ? AND (s.user_id=? OR (s.kind='guest' AND s.device_id IN "
            "(SELECT device_id FROM device_member WHERE user_id=? AND role='owner'))) "
            "ORDER BY s.last_seen DESC", (self.clock(), user_id, user_id))
        for row in rows:
            token_hash = row.pop("token_sha256")
            row["id"] = token_hash.hex()[:16]
            row["current"] = token_hash == mine
        return rows

    def revoke_session_id(self, user_id: int, session_id: str) -> bool:
        """退出某一个：自己的，或自己设备上的访客。"""
        if not re.fullmatch(r"[0-9a-f]{16}", session_id or ""):
            return False
        removed = self._run(
            "DELETE FROM session WHERE substr(lower(hex(token_sha256)), 1, 16)=? AND (user_id=? OR "
            "(kind='guest' AND device_id IN (SELECT device_id FROM device_member WHERE user_id=? AND role='owner')))",
            (session_id, user_id, user_id))
        if removed:
            self.audit("session.revoke", user_id)
        return removed > 0

    def revoke_other_sessions(self, user_id: int, current: str) -> int:
        """退出自己别的所有登录（手机、电脑），留下现在这个；访客不动。"""
        removed = self._run("DELETE FROM session WHERE user_id=? AND token_sha256 != ?", (user_id, db.sha256(current)))
        self.audit("session.revoke_others", user_id, detail=f"n={removed}")
        return removed

    def count_sessions(self) -> int:
        return self._one("SELECT COUNT(*) AS n FROM session")["n"]

    # ---- 邀请 ----

    def create_invite(self, created_by: int | None, role: str = "user", name: str = "",
                      ttl_s: float = INVITE_S) -> str:
        if role not in ("admin", "user"):
            raise ValueError("没有这个角色")
        code = db.new_code()
        now = self.clock()
        self._run("INSERT INTO invite(code_sha256, created_by, role, name, created_at, expires_at) "
                  "VALUES (?, ?, ?, ?, ?, ?)", (db.sha256(code), created_by, role, _clean_name(name),
                                                now, now + ttl_s))
        self.audit("invite.create", created_by, target_kind="invite", detail=f"role={role}")
        return code

    def invite(self, code: str | None) -> dict | None:
        """还能用的邀请：没过期、没用过。"""
        if not code or len(code) > 64:
            return None
        row = self._one("SELECT * FROM invite WHERE code_sha256=?", (db.sha256(code.upper()),))
        if row is None or row["used_by"] is not None or row["expires_at"] <= self.clock():
            return None
        return {"role": row["role"], "name": row["name"], "expires_at": row["expires_at"]}

    def invite_list(self, limit: int = 30) -> list[dict]:
        """最近的邀请（管理员的电脑版「邀请」页）：名字、时间、谁用了；不含码本身（库里也没有）。"""
        return self._all("SELECT i.name, i.role, i.created_at, i.expires_at, i.used_at, u.name AS used_by_name "
                         "FROM invite i LEFT JOIN user u ON u.id=i.used_by ORDER BY i.created_at DESC LIMIT ?",
                         (limit,))

    def accept_invite(self, code: str, name: str, user_agent: str = "", ip: str = "") -> tuple[int, str]:
        """用掉邀请：建用户、发一个手机会话。返回 (用户 id, 会话)。邀请无效时 ValueError。"""
        name = _clean_name(name)
        with self.lock, db.transaction(self.conn):
            row = self.conn.execute("SELECT * FROM invite WHERE code_sha256=?",
                                    (db.sha256((code or "").upper()),)).fetchone()
            if row is None or row["used_by"] is not None or row["expires_at"] <= self.clock():
                raise ValueError("这个邀请已经用过或过期了，请向管理员要一个新的")
            name = name or row["name"] or "家人"
            now = self.clock()
            user_id = int(self.conn.execute(
                "INSERT INTO user(name, role, status, created_at) VALUES (?, ?, 'active', ?)",
                (name, row["role"], now)).lastrowid)
            self.conn.execute("UPDATE invite SET used_by=?, used_at=? WHERE code_sha256=?",
                              (user_id, now, row["code_sha256"]))
        token = self.new_session(user_id, "phone", user_agent=user_agent, ip=ip)
        self.audit("invite.accept", user_id, ip, "user", str(user_id))
        return user_id, token

    # ---- 设备 ----

    def device(self, device_id: str) -> dict | None:
        return self._one("SELECT * FROM device WHERE id=?", (device_id,))

    def all_devices(self) -> list[dict]:
        return self._all("SELECT * FROM device ORDER BY created_at, id")

    def active_devices(self) -> list[dict]:
        """已绑定、在用的设备，带主人：中转据此给每台写频道表。"""
        return self._all("SELECT d.*, m.user_id AS owner FROM device d "
                         "LEFT JOIN device_member m ON m.device_id=d.id AND m.role='owner' "
                         "WHERE d.status='active' ORDER BY d.created_at, d.id")

    def devices_of(self, user_id: int) -> list[dict]:
        return self._all("SELECT d.*, m.role AS member_role, "
                         "(SELECT user_id FROM device_member o WHERE o.device_id=d.id AND o.role='owner') AS owner "
                         "FROM device d JOIN device_member m ON m.device_id=d.id "
                         "WHERE m.user_id=? AND d.status='active' ORDER BY d.created_at, d.id", (user_id,))

    def owner(self, device_id: str) -> int | None:
        row = self._one("SELECT user_id FROM device_member WHERE device_id=? AND role='owner'", (device_id,))
        return row["user_id"] if row else None

    def member_role(self, device_id: str, user_id: int) -> str | None:
        row = self._one("SELECT role FROM device_member WHERE device_id=? AND user_id=?", (device_id, user_id))
        return row["role"] if row else None

    def device_by_remote_code(self, code: str) -> dict | None:
        if not code:
            return None
        return self._one("SELECT * FROM device WHERE remote_code=? AND status='active'", (code.upper(),))

    def device_by_claim(self, code: str) -> dict | None:
        if not code:
            return None
        return self._one("SELECT * FROM device WHERE claim_code=? AND status='unclaimed'", (code.upper(),))

    def add_legacy_device(self, device_id: str, port: int, model: str, name: str, owner: int,
                          remote_code: str = "") -> None:
        """迁移：旧固件（只有共享令牌、没有设备 id）在某个端口上的那台。"""
        with self.lock, db.transaction(self.conn):
            self.conn.execute(
                "INSERT INTO device(id, model, secret_sha256, status, name, remote_code, legacy_port, created_at) "
                "VALUES (?, ?, NULL, 'active', ?, ?, ?, ?)",
                (device_id, model, _clean_name(name), remote_code or db.new_code(), port, self.clock()))
            self.conn.execute("INSERT INTO device_member(device_id, user_id, role) VALUES (?, ?, 'owner')",
                              (device_id, owner))

    def legacy_device(self, port: int) -> dict | None:
        return self._one("SELECT * FROM device WHERE legacy_port=? AND status!='retired' ORDER BY created_at LIMIT 1",
                         (port,))

    def register_device(self, device_id: str, model: str, secret: str, fw: str = "", ip: str = "") -> dict:
        """第一次连上来的新设备：记下密钥的哈希（首次信任），等人扫码绑定。"""
        if not _MODEL.match(model or ""):
            model = ""
        now = self.clock()
        with self.lock:
            self.conn.execute(
                "INSERT OR IGNORE INTO device(id, model, secret_sha256, status, claim_code, remote_code, fw, "
                "created_at, last_seen, last_ip_prefix) VALUES (?, ?, ?, 'unclaimed', ?, ?, ?, ?, ?, ?)",
                (device_id, model, db.sha256(secret), db.new_code(), db.new_code(), fw[:64], now, now,
                 db.ip_prefix(ip)))
        self.audit("device.register", None, ip, "device", device_id, f"model={model}")
        return self.device(device_id)

    def touch_device(self, device_id: str, fw: str | None = None, model: str | None = None, ip: str = "") -> None:
        sets, args = ["last_seen=?"], [self.clock()]
        if fw:
            sets.append("fw=?")
            args.append(fw[:64])
        if model and _MODEL.match(model):
            sets.append("model=?")
            args.append(model)
        if ip:
            sets.append("last_ip_prefix=?")
            args.append(db.ip_prefix(ip))
        self._run(f"UPDATE device SET {', '.join(sets)} WHERE id=?", (*args, device_id))

    def pin_secret(self, device_id: str, secret: str) -> None:
        """重新配对窗口里来了新密钥：记住它，窗口关掉。"""
        self._run("UPDATE device SET secret_sha256=?, repair_until=NULL WHERE id=?",
                  (db.sha256(secret), device_id))
        self.audit("device.repaired", None, "", "device", device_id)

    def bind(self, claim: str, user_id: int, ip: str = "") -> dict:
        """扫码绑定：设备归这个用户。返回设备；码不对时 ValueError。"""
        with self.lock, db.transaction(self.conn):
            row = self.conn.execute("SELECT * FROM device WHERE claim_code=? AND status='unclaimed'",
                                    ((claim or "").upper(),)).fetchone()
            if row is None:
                raise ValueError("这个绑定码不对，或者设备已经绑定过了")
            name = default_device_name(row["model"], row["id"])
            self.conn.execute("UPDATE device SET status='active', claim_code=NULL, name=? WHERE id=?",
                              (name, row["id"]))
            self.conn.execute("DELETE FROM device_member WHERE device_id=?", (row["id"],))
            self.conn.execute("INSERT INTO device_member(device_id, user_id, role) VALUES (?, ?, 'owner')",
                              (row["id"], user_id))
        self.audit("device.bind", user_id, ip, "device", row["id"])
        return self.device(row["id"])

    def unbind(self, device_id: str, actor: int | None, ip: str = "") -> None:
        """解绑：回到未绑定、换新的绑定码和遥控码，访客会话作废，设备状态（投屏）删掉；主人的订阅不动。"""
        with self.lock, db.transaction(self.conn):
            row = self.conn.execute("SELECT * FROM device WHERE id=?", (device_id,)).fetchone()
            if row is None or row["status"] != "active":
                raise ValueError("没有这台设备")
            if row["legacy_port"] is not None:
                raise ValueError("旧固件的设备不能解绑：先给它刷新固件")
            self.conn.execute("UPDATE device SET status='unclaimed', claim_code=?, remote_code=? WHERE id=?",
                              (db.new_code(), db.new_code(), device_id))
            self.conn.execute("DELETE FROM device_member WHERE device_id=?", (device_id,))
            self.conn.execute("DELETE FROM device_state WHERE device_id=?", (device_id,))
            self.conn.execute("DELETE FROM session WHERE device_id=?", (device_id,))
        self.audit("device.unbind", actor, ip, "device", device_id)

    def rename(self, device_id: str, name: str, actor: int | None) -> str:
        name = _clean_name(name)
        if not name:
            raise ValueError("名字不能是空的")
        self._run("UPDATE device SET name=? WHERE id=?", (name, device_id))
        self.audit("device.rename", actor, "", "device", device_id)
        return name

    def start_repair(self, device_id: str, actor: int | None) -> float:
        until = self.clock() + REPAIR_S
        self._run("UPDATE device SET repair_until=? WHERE id=?", (until, device_id))
        self.audit("device.repair", actor, "", "device", device_id)
        return until

    def set_device_status(self, device_id: str, status: str, actor: int | None) -> bool:
        if status not in ("active", "banned", "unclaimed"):
            raise ValueError("没有这个状态")
        changed = self._run("UPDATE device SET status=? WHERE id=?", (status, device_id))
        if changed and status == "banned":
            self._run("DELETE FROM session WHERE device_id=?", (device_id,))
        if changed:
            self.audit(f"device.{status}", actor, "", "device", device_id)
        return bool(changed)

    def new_remote_code(self, device_id: str, actor: int | None = None) -> str:
        """换遥控码：旧二维码失效，扫旧码进来的访客都要重新扫；主人和成员不受影响。"""
        code = db.new_code()
        with self.lock, db.transaction(self.conn):
            self.conn.execute("UPDATE device SET remote_code=? WHERE id=?", (code, device_id))
            self.conn.execute("DELETE FROM session WHERE device_id=? AND kind='guest'", (device_id,))
        self.audit("device.new_code", actor, "", "device", device_id)
        return code

    def adopt(self, legacy_id: str, real_id: str, actor: int | None = None) -> None:
        """旧设备刷了新固件、报上了真 id：把旧记录的主人、遥控码、投屏状态和名字搬过去，旧记录退役。"""
        with self.lock, db.transaction(self.conn):
            old = self.conn.execute("SELECT * FROM device WHERE id=?", (legacy_id,)).fetchone()
            new = self.conn.execute("SELECT * FROM device WHERE id=?", (real_id,)).fetchone()
            if old is None or old["legacy_port"] is None or old["status"] != "active":
                raise ValueError(f"{legacy_id} 不是在用的旧设备")
            if new is None or new["status"] not in ("unclaimed", "active") or new["legacy_port"] is not None:
                raise ValueError(f"{real_id} 还没连上来过，或者不能接收")
            code = old["remote_code"]
            self.conn.execute("UPDATE device SET status='retired', remote_code=NULL WHERE id=?", (legacy_id,))
            self.conn.execute("UPDATE device SET status='active', claim_code=NULL, remote_code=?, name=? "
                              "WHERE id=?", (code, old["name"], real_id))
            self.conn.execute("DELETE FROM device_member WHERE device_id=?", (real_id,))
            self.conn.execute("UPDATE device_member SET device_id=? WHERE device_id=?", (real_id, legacy_id))
            state = self.conn.execute("SELECT data FROM device_state WHERE device_id=?", (legacy_id,)).fetchone()
            if state is not None:
                self.conn.execute("INSERT INTO device_state(device_id, data, updated_at) VALUES (?, ?, ?) "
                                  "ON CONFLICT(device_id) DO UPDATE SET data=excluded.data, "
                                  "updated_at=excluded.updated_at", (real_id, state["data"], self.clock()))
            self.conn.execute("UPDATE session SET device_id=? WHERE device_id=?", (real_id, legacy_id))
        self.audit("device.adopt", actor, "", "device", real_id, f"from={legacy_id}")

    # ---- 状态 ----

    def load_user_state(self, user_id: int) -> dict | None:
        row = self._one("SELECT data FROM user_state WHERE user_id=?", (user_id,))
        return json.loads(row["data"]) if row else None

    def save_user_state(self, user_id: int, data: dict) -> None:
        self._run("INSERT INTO user_state(user_id, data, updated_at) VALUES (?, ?, ?) "
                  "ON CONFLICT(user_id) DO UPDATE SET data=excluded.data, updated_at=excluded.updated_at",
                  (user_id, json.dumps(data, ensure_ascii=False), self.clock()))

    def load_device_state(self, device_id: str) -> dict | None:
        row = self._one("SELECT data FROM device_state WHERE device_id=?", (device_id,))
        return json.loads(row["data"]) if row else None

    def save_device_state(self, device_id: str, data: dict) -> None:
        self._run("INSERT INTO device_state(device_id, data, updated_at) VALUES (?, ?, ?) "
                  "ON CONFLICT(device_id) DO UPDATE SET data=excluded.data, updated_at=excluded.updated_at",
                  (device_id, json.dumps(data, ensure_ascii=False), self.clock()))

    # ---- 共享的视频列表 ----

    def cached_source(self, site: str, source: str, kind: str) -> dict | None:
        row = self._one("SELECT name, videos, fetched_at FROM source_cache WHERE site=? AND source=? AND kind=?",
                        (site, source, kind))
        if row is None:
            return None
        return {"name": row["name"], "videos": json.loads(row["videos"]), "fetched_at": row["fetched_at"]}

    def cache_source(self, site: str, source: str, kind: str, name: str, videos: list[dict],
                     fetched_at: float) -> None:
        self._run("INSERT INTO source_cache(site, source, kind, name, videos, fetched_at) VALUES (?, ?, ?, ?, ?, ?) "
                  "ON CONFLICT(site, source, kind) DO UPDATE SET name=excluded.name, videos=excluded.videos, "
                  "fetched_at=excluded.fetched_at",
                  (site, source, kind, name, json.dumps(videos, ensure_ascii=False), fetched_at))

    # ---- 操作记录 ----

    def audit(self, action: str, actor: int | None = None, ip: str = "", target_kind: str = "",
              target_id: str = "", detail: str = "") -> None:
        self._run("INSERT INTO audit_log(at, actor_user, actor_ip_prefix, action, target_kind, target_id, detail) "
                  "VALUES (?, ?, ?, ?, ?, ?, ?)",
                  (self.clock(), actor, db.ip_prefix(ip) if ip and "/" not in ip else ip, action,
                   target_kind, target_id, detail[:200]))

    def audit_rows(self, limit: int = 50) -> list[dict]:
        return self._all("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,))
