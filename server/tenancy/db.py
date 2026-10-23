"""数据库文件：表结构、连接方式和几个小工具（哈希、码、IP 前缀）。

一个文件（默认 /var/lib/ai-passport-tv/tv.db），WAL 模式：中转和电视服务两个进程同时读，
写很少且都很短，busy_timeout 盖住偶尔的重叠。密钥、会话、邀请码只存 SHA-256。
"""

from __future__ import annotations

import hashlib
import ipaddress
import os
import secrets
import sqlite3
from pathlib import Path

SCHEMA_VERSION = 1
# 码和遥控码同一套字母（server/relay/state.py 的 CODE_ALPHABET）：全大写 base32，二维码可用字母数字模式。
CODE_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"
CODE_LENGTH = 20
# 自建 / 电脑客户端（meta.selfhost = 1，server/selfhost/bootstrap.py 写的）的绑定码：6 位数字，家里人好输。
# 只用于绑定码；遥控码、邀请码还是 20 位。暴力猜由中转的错码限流挡（server/tenancy/claimguard.py）。
CLAIM_DIGITS = 6
# 6 位数字撞上已有的绑定码时重抽几次；都撞上（不会发生）就退回 20 位的码。
CLAIM_TRIES = 32
BUSY_TIMEOUT_MS = 5000

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS user (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('admin', 'user')),
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'banned')),
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS session (
    token_sha256 BLOB PRIMARY KEY,
    user_id INTEGER REFERENCES user(id),
    kind TEXT NOT NULL CHECK (kind IN ('phone', 'pc', 'guest')),
    device_id TEXT,
    created_at REAL NOT NULL,
    last_seen REAL NOT NULL,
    expires_at REAL NOT NULL,
    user_agent TEXT NOT NULL DEFAULT '',
    ip_prefix TEXT NOT NULL DEFAULT '',
    via TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS session_user ON session(user_id);
CREATE INDEX IF NOT EXISTS session_device ON session(device_id);
CREATE TABLE IF NOT EXISTS invite (
    code_sha256 BLOB PRIMARY KEY,
    created_by INTEGER REFERENCES user(id),
    role TEXT NOT NULL CHECK (role IN ('admin', 'user')),
    name TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    used_by INTEGER REFERENCES user(id),
    used_at REAL
);
CREATE TABLE IF NOT EXISTS login_ticket (
    ticket_sha256 BLOB PRIMARY KEY,
    short_code TEXT NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('pending', 'confirmed', 'used')),
    user_id INTEGER REFERENCES user(id),
    pc_ip_prefix TEXT NOT NULL DEFAULT '',
    pc_user_agent TEXT NOT NULL DEFAULT '',
    poll_sha256 BLOB
);
-- 让手机登录（电脑版「让手机登录」）：一次性的手机登录链接 /TV/P/<token>，5 分钟、只能用一次。
-- 令牌、8 位码、电脑等结果用的 poll 都只存 SHA-256。新表：旧库打开时自动建，版本号不变。
CREATE TABLE IF NOT EXISTS phone_link (
    token_sha256 BLOB PRIMARY KEY,
    code_sha256 BLOB NOT NULL,
    poll_sha256 BLOB NOT NULL,
    user_id INTEGER NOT NULL REFERENCES user(id),
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    used_at REAL,
    pc_ip_prefix TEXT NOT NULL DEFAULT '',
    pc_user_agent TEXT NOT NULL DEFAULT '',
    phone_ip_prefix TEXT NOT NULL DEFAULT '',
    phone_user_agent TEXT NOT NULL DEFAULT '',
    origin TEXT NOT NULL DEFAULT 'pc',
    device_id TEXT
);
CREATE INDEX IF NOT EXISTS phone_link_code ON phone_link(code_sha256);
CREATE INDEX IF NOT EXISTS phone_link_poll ON phone_link(poll_sha256);
CREATE TABLE IF NOT EXISTS device (
    id TEXT PRIMARY KEY,
    model TEXT NOT NULL DEFAULT '',
    secret_sha256 BLOB,
    status TEXT NOT NULL CHECK (status IN ('unclaimed', 'active', 'banned', 'repair', 'retired')),
    name TEXT NOT NULL DEFAULT '',
    claim_code TEXT UNIQUE,
    remote_code TEXT UNIQUE,
    fw TEXT NOT NULL DEFAULT '',
    legacy_port INTEGER,
    repair_until REAL,
    created_at REAL NOT NULL,
    last_seen REAL NOT NULL DEFAULT 0,
    last_ip_prefix TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS device_member (
    device_id TEXT NOT NULL REFERENCES device(id),
    user_id INTEGER NOT NULL REFERENCES user(id),
    role TEXT NOT NULL CHECK (role IN ('owner', 'member')),
    PRIMARY KEY (device_id, user_id)
);
CREATE TABLE IF NOT EXISTS user_state (
    user_id INTEGER PRIMARY KEY REFERENCES user(id),
    data TEXT NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS device_state (
    device_id TEXT PRIMARY KEY REFERENCES device(id),
    data TEXT NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS source_cache (
    site TEXT NOT NULL,
    source TEXT NOT NULL,
    kind TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    videos TEXT NOT NULL,
    fetched_at REAL NOT NULL,
    PRIMARY KEY (site, source, kind)
);
-- 手机/电脑管理设备保存的 Wi-Fi（server/tenancy/wifi.py）。键是中转看到的设备 id。
-- 只存 Wi-Fi 名字，从不存密码；pub 是设备加密钥匙的公钥（首次信任）。
CREATE TABLE IF NOT EXISTS device_wifi (
    device_id TEXT PRIMARY KEY,
    pub BLOB,
    pub_mismatch_at REAL NOT NULL DEFAULT 0,
    networks TEXT NOT NULL DEFAULT '[]',
    current TEXT NOT NULL DEFAULT '',
    reported_at REAL NOT NULL DEFAULT 0,
    last_seq INTEGER NOT NULL DEFAULT 0,
    pending TEXT NOT NULL DEFAULT '',
    ack_seq INTEGER NOT NULL DEFAULT 0,
    ack_ok INTEGER NOT NULL DEFAULT 0,
    ack_error TEXT NOT NULL DEFAULT '',
    ack_at REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY,
    at REAL NOT NULL,
    actor_user INTEGER,
    actor_ip_prefix TEXT NOT NULL DEFAULT '',
    action TEXT NOT NULL,
    target_kind TEXT NOT NULL DEFAULT '',
    target_id TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT ''
);
"""


def connect(path: Path | str) -> sqlite3.Connection:
    """打开（必要时新建）数据库。自动提交；多条语句要一起成功的地方用 transaction()。"""
    path = Path(path)
    fresh = not path.exists()
    conn = sqlite3.connect(str(path), timeout=BUSY_TIMEOUT_MS / 1000,
                           isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    _add_columns(conn)
    if fresh:
        # 里面有会话和设备密钥的哈希：只给服务账号和它的组读。
        try:
            os.chmod(path, 0o640)
        except OSError:
            pass
    row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    if row is None:
        conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)",
                     (str(SCHEMA_VERSION),))
    elif int(row["value"]) > SCHEMA_VERSION:
        conn.close()
        raise RuntimeError(f"{path}: 数据库是更新的版本（{row['value']}），这份代码只认 {SCHEMA_VERSION}")
    return conn


# 后来加的列：(表, 列, 类型)。旧库打开时补上（ALTER TABLE ADD COLUMN），已有的数据不动。
# 阶段 2：电脑登录票据的轮询密钥（只在电脑上，二维码里是另一个），见 tickets.py。
# 恢复登录：手机登录链接是谁要的（pc 电脑版、device 设备上的「恢复登录」、admin 管理员命令行）和
# 哪台设备（phonelinks.py）；会话是怎么来的（via：recover 设备恢复、admin 管理员链接、signup 扫绑定码注册）。
# 阶段 3：管理员在总后台看过一个自己注册的用户、点了「知道了」的时间（manage.py；NULL 是还没看过）。
ADDED_COLUMNS = (("login_ticket", "poll_sha256", "BLOB"),
                 ("phone_link", "origin", "TEXT NOT NULL DEFAULT 'pc'"),
                 ("phone_link", "device_id", "TEXT"),
                 ("session", "via", "TEXT NOT NULL DEFAULT ''"),
                 ("user", "reviewed_at", "REAL"))


def _add_columns(conn: sqlite3.Connection) -> None:
    for table, column, kind in ADDED_COLUMNS:
        present = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column in present:
            continue
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
        except sqlite3.OperationalError as error:
            # 另一个进程（电视服务）刚好同时补上了。
            if "duplicate column" not in str(error):
                raise


class transaction:
    """with transaction(conn): ... —— BEGIN IMMEDIATE，出错回滚。"""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def __enter__(self):
        self.conn.execute("BEGIN IMMEDIATE")
        return self.conn

    def __exit__(self, kind, _value, _tb):
        self.conn.execute("ROLLBACK" if kind else "COMMIT")
        return False


def sha256(text: str) -> bytes:
    return hashlib.sha256(text.encode("utf-8")).digest()


def new_code() -> str:
    """20 位、100 位随机的码：遥控码、绑定码、邀请码。"""
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))


def _draw_digits() -> str:
    return "".join(secrets.choice("0123456789") for _ in range(CLAIM_DIGITS))


def is_short_claim(code: str | None) -> bool:
    return bool(code) and len(code) == CLAIM_DIGITS and code.isascii() and code.isdigit()


def new_short_claim(conn: sqlite3.Connection, draw=_draw_digits, tries: int = CLAIM_TRIES) -> str:
    """6 位数字的绑定码，和库里已有的（device.claim_code 是 UNIQUE）都不一样。

    调用方在写事务里调用并在同一个事务里写进去，别的进程（另一个电视服务）就插不进来。"""
    for _ in range(tries):
        code = draw()
        if conn.execute("SELECT 1 FROM device WHERE claim_code=?", (code,)).fetchone() is None:
            return code
    return new_code()


def ip_prefix(address: str | None) -> str:
    """只留网段：IPv4 /24，IPv6 /48；不认识的地址记空。"""
    try:
        ip = ipaddress.ip_address((address or "").strip())
    except ValueError:
        return ""
    bits = 24 if ip.version == 4 else 48
    return str(ipaddress.ip_network(f"{ip}/{bits}", strict=False))
