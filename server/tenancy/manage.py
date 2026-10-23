"""总后台（阶段 3，server/relay/admin.py）要的数据库操作：用户一览、改名、删除（先导出）、
某个用户的登录、操作记录查询、全局频道的默认分组。

命令行（admin.py）和电脑版已有的操作（封禁、解绑、邀请……）还在 accounts.py，这里只放总后台多出来的。
每个改东西的操作都写操作记录，记下管理员和他的网段。
"""

from __future__ import annotations

import json
import re

from . import db
from .accounts import _clean_name
from .tickets import describe_agent

AUDIT_PAGE_MAX = 200
# 全局直播频道的默认分组（总后台的频道表里设的）：{台 id: 分组名}，存在 meta 里。
# 每个用户在手机上调的分组盖在它上面（server/relay/web.py 的 _overrides）。
GLOBAL_GROUPS_KEY = "channel_groups"
_SESSION_ID = re.compile(r"^[0-9a-f]{16}$")


class AdminOps:
    def __init__(self, accounts):
        self.accounts = accounts

    # ---- 用户 ----

    def users(self) -> list[dict]:
        """所有用户：角色、状态、设备数、登录数、最后使用、怎么来的（signup 扫绑定码自己注册、invite 邀请、
        initial 迁移或命令行建的）、自己注册的有没有被看过。"""
        now = self.accounts.clock()
        rows = self.accounts._all(
            "SELECT u.id, u.name, u.role, u.status, u.created_at, u.reviewed_at, "
            "(SELECT COUNT(*) FROM device_member m JOIN device d ON d.id=m.device_id "
            " WHERE m.user_id=u.id AND m.role='owner' AND d.status!='retired') AS devices, "
            "(SELECT COUNT(*) FROM session s WHERE s.user_id=u.id AND s.expires_at > ?) AS sessions, "
            "(SELECT MAX(s.last_seen) FROM session s WHERE s.user_id=u.id) AS last_seen, "
            "EXISTS(SELECT 1 FROM audit_log a WHERE a.action='user.signup' AND a.target_kind='user' "
            "       AND a.target_id=CAST(u.id AS TEXT)) AS signup, "
            "EXISTS(SELECT 1 FROM invite i WHERE i.used_by=u.id) AS invited "
            "FROM user u ORDER BY u.id", (now,))
        for row in rows:
            signup, invited = row.pop("signup"), row.pop("invited")
            row["origin"] = "signup" if signup else "invite" if invited else "initial"
            row["last_seen"] = row["last_seen"] or 0
        return rows

    def rename_user(self, user_id: int, name: str, actor: int | None, ip: str = "") -> str:
        name = _clean_name(name)
        if not name:
            raise ValueError("名字不能是空的")
        if self.accounts.user(user_id) is None:
            raise ValueError("没有这个用户")
        old = self.accounts.user(user_id)["name"]
        self.accounts._run("UPDATE user SET name=? WHERE id=?", (name, user_id))
        self.accounts.audit("user.rename", actor, ip, "user", str(user_id), f"{old} → {name}")
        return name

    def mark_reviewed(self, user_id: int, actor: int | None, ip: str = "") -> bool:
        changed = self.accounts._run("UPDATE user SET reviewed_at=? WHERE id=?", (self.accounts.clock(), user_id))
        if changed:
            self.accounts.audit("user.reviewed", actor, ip, "user", str(user_id))
        return bool(changed)

    # ---- 某个用户的登录 ----

    def sessions(self, user_id: int) -> list[dict]:
        """这个用户的登录（手机、电脑），加上他设备上的访客；不含令牌（id 只是哈希的前 16 位）。"""
        rows = self.accounts.session_list(user_id)
        for row in rows:
            row["browser"] = describe_agent(row.pop("user_agent"))
            row.pop("current", None)
        return rows

    def revoke_session(self, user_id: int, session_id: str, actor: int | None, ip: str = "") -> bool:
        if not _SESSION_ID.match(session_id or ""):
            return False
        removed = self.accounts._run(
            "DELETE FROM session WHERE substr(lower(hex(token_sha256)), 1, 16)=? AND (user_id=? OR "
            "(kind='guest' AND device_id IN (SELECT device_id FROM device_member WHERE user_id=? AND role='owner')))",
            (session_id, user_id, user_id))
        if removed:
            self.accounts.audit("admin.session.revoke", actor, ip, "user", str(user_id))
        return removed > 0

    def revoke_all(self, user_id: int, actor: int | None, ip: str = "") -> int:
        """这个用户所有的登录（手机、电脑）都退出；他设备上的访客不动。"""
        removed = self.accounts._run("DELETE FROM session WHERE user_id=?", (user_id,))
        self.accounts.audit("admin.session.revoke_all", actor, ip, "user", str(user_id), f"n={removed}")
        return removed

    # ---- 导出、删除 ----

    def export(self, user_id: int) -> dict | None:
        """删除前的备份：用户、他的订阅和进度（user_state 原样）、他的设备、登录（不含令牌）、
        他用过和发出的邀请、和他有关的操作记录。没有这个用户时 None。"""
        accounts = self.accounts
        user = accounts.user(user_id)
        if user is None:
            return None
        devices = accounts._all(
            "SELECT d.id, d.model, d.name, d.status, d.fw, d.created_at, d.last_seen, m.role AS member_role "
            "FROM device d JOIN device_member m ON m.device_id=d.id WHERE m.user_id=? ORDER BY d.id", (user_id,))
        sessions = accounts._all("SELECT kind, device_id, created_at, last_seen, expires_at, user_agent, ip_prefix, via "
                                 "FROM session WHERE user_id=? ORDER BY created_at", (user_id,))
        invites = accounts._all("SELECT name, role, created_at, expires_at, used_at, "
                                "CASE WHEN used_by=? THEN 'accepted' ELSE 'created' END AS how "
                                "FROM invite WHERE used_by=? OR created_by=? ORDER BY created_at",
                                (user_id, user_id, user_id))
        audit = accounts._all("SELECT at, actor_user, actor_ip_prefix, action, target_kind, target_id, detail "
                              "FROM audit_log WHERE actor_user=? OR (target_kind='user' AND target_id=?) "
                              "ORDER BY id DESC LIMIT 500", (user_id, str(user_id)))
        return {"format": "ai-passport-tv user export", "version": 1, "exported_at": accounts.clock(),
                "user": user, "state": accounts.load_user_state(user_id), "devices": devices,
                "sessions": sessions, "invites": invites, "audit": audit}

    def delete_user(self, user_id: int, actor: int | None, ip: str = "") -> list[str]:
        """删除用户：他是主人的设备解绑（回到未绑定、换绑定码和遥控码，访客作废），然后删他的登录、
        登录链接、订阅和进度、成员关系和用户本身；邀请里只去掉对他的引用。返回解绑了的设备 id。

        不能删：自己、最后一个管理员、有旧固件设备的用户（旧设备不能解绑）。出错时 ValueError，什么都不改。
        调用方要先 export() 并保存好。"""
        accounts = self.accounts
        now = accounts.clock()
        with accounts.lock, db.transaction(accounts.conn) as conn:
            user = conn.execute("SELECT * FROM user WHERE id=?", (user_id,)).fetchone()
            if user is None:
                raise ValueError("没有这个用户")
            if user_id == actor:
                raise ValueError("不能删除自己")
            if user["role"] == "admin" and conn.execute(
                    "SELECT COUNT(*) AS n FROM user WHERE role='admin' AND status='active' AND id!=?",
                    (user_id,)).fetchone()["n"] == 0:
                raise ValueError("这是最后一个管理员，不能删除")
            owned = conn.execute("SELECT d.id, d.legacy_port, d.status FROM device d JOIN device_member m "
                                 "ON m.device_id=d.id WHERE m.user_id=? AND m.role='owner'", (user_id,)).fetchall()
            if any(row["legacy_port"] is not None and row["status"] != "retired" for row in owned):
                raise ValueError("这个用户有旧固件的设备（不能解绑）：先给设备刷新固件、接收到真 id，再删除")
            unbound = []
            for row in owned:
                if row["status"] in ("active", "banned", "repair"):
                    conn.execute("UPDATE device SET status='unclaimed', claim_code=?, remote_code=? WHERE id=?",
                                 (accounts._claim_code(conn), db.new_code(), row["id"]))
                    unbound.append(row["id"])
                conn.execute("DELETE FROM device_state WHERE device_id=?", (row["id"],))
                conn.execute("DELETE FROM session WHERE device_id=?", (row["id"],))
                conn.execute("DELETE FROM device_member WHERE device_id=?", (row["id"],))
            conn.execute("DELETE FROM device_member WHERE user_id=?", (user_id,))
            conn.execute("DELETE FROM session WHERE user_id=?", (user_id,))
            conn.execute("DELETE FROM phone_link WHERE user_id=?", (user_id,))
            conn.execute("DELETE FROM login_ticket WHERE user_id=?", (user_id,))
            conn.execute("UPDATE invite SET created_by=NULL WHERE created_by=?", (user_id,))
            # 用过的邀请去掉「谁用的」，同时让它过期：used_by 空了的邀请不能又变成能用的。
            conn.execute("UPDATE invite SET used_by=NULL, expires_at=MIN(expires_at, ?) WHERE used_by=?",
                         (now, user_id))
            conn.execute("DELETE FROM user_state WHERE user_id=?", (user_id,))
            conn.execute("DELETE FROM user WHERE id=?", (user_id,))
        for device_id in unbound:
            accounts.audit("device.unbind", actor, ip, "device", device_id, f"user {user_id} deleted")
        accounts.audit("user.delete", actor, ip, "user", str(user_id), user["name"])
        return unbound

    # ---- 操作记录 ----

    def audit(self, user: int | None = None, device: str | None = None, action: str | None = None,
              before: int | None = None, limit: int = 50) -> list[dict]:
        """按用户（做的人或对象是他）、设备、操作（前缀，如 device. ）筛，按 id 从新到旧翻页（before）。"""
        where, args = [], []
        if user is not None:
            where.append("(a.actor_user=? OR (a.target_kind='user' AND a.target_id=?))")
            args += [user, str(user)]
        if device:
            where.append("a.target_kind='device' AND a.target_id=?")
            args.append(device)
        if action:
            where.append("a.action LIKE ? ESCAPE '\\'")
            args.append(action.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
        if before is not None:
            where.append("a.id < ?")
            args.append(before)
        sql = ("SELECT a.*, u.name AS actor_name FROM audit_log a LEFT JOIN user u ON u.id=a.actor_user"
               + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY a.id DESC LIMIT ?")
        return self.accounts._all(sql, (*args, max(1, min(int(limit), AUDIT_PAGE_MAX))))

    def audit_actions(self) -> list[str]:
        return [row["action"] for row in self.accounts._all(
            "SELECT DISTINCT action FROM audit_log ORDER BY action LIMIT 300")]

    # ---- 全局频道的默认分组 ----

    def global_groups(self) -> dict[str, str]:
        try:
            data = json.loads(self.accounts.meta(GLOBAL_GROUPS_KEY, "{}"))
        except ValueError:
            return {}
        return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}

    def set_global_groups(self, groups: dict[str, str]) -> None:
        self.accounts.set_meta(GLOBAL_GROUPS_KEY, json.dumps(groups, ensure_ascii=False, sort_keys=True))
