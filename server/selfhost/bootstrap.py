"""启动前的准备：数据目录、密钥、数据库、一次性设置码；还有每晚的数据库备份。

设计见 docs/superpowers/specs/2026-10-15-self-hosting-design.md 第 4.3、4.5、10 节。一家人的全部东西都在
一个目录里（Docker 里挂在 /data）：

  config.toml   secrets/（0700：relay-key、wifi-key、setup-code、youtube-node.json）
  tv.db         devices/<id>/   channels.txt（一开始是空的）
  backups/      exports/        run/（状态文件、套接字；不用备份）   youtube/
  firmware/     网页刷机用的固件（manifest.json、SHA256SUMS、镜像；./run.sh --fetch-firmware 放进来）
"""

from __future__ import annotations

import os
import secrets
import sqlite3
import tempfile
import time
from pathlib import Path

from ..tenancy import db
from ..tenancy.accounts import Accounts
from ..tenancy.gate import LEGACY_TOKEN_KEY

DIRS = ("secrets", "devices", "backups", "exports", "run", "youtube", "firmware")
SETUP_CODE_LENGTH = 10
CHANNELS_HEADER = ("# 直播频道表：在总后台（/tv/admin）的「频道表」里添加或导入你自己的播放列表。\n"
                   "# 每行： id | 显示名 | 地址 | User-Agent（可选）\n")
# 数据库里的标记：这是自建的服务器；上次启动时的表结构版本（变了就先备份，见 pre_upgrade_backup）。
SELFHOST_META = "selfhost"
SCHEMA_META = "selfhost_schema"


class DataDirError(RuntimeError):
    """数据目录用不了：消息给人看。"""


class Layout:
    """数据目录里每样东西的位置。"""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.config = self.root / "config.toml"
        self.secrets = self.root / "secrets"
        self.relay_key = self.secrets / "relay-key"
        self.wifi_key = self.secrets / "wifi-key"
        self.setup_code = self.secrets / "setup-code"
        self.youtube_node = self.secrets / "youtube-node.json"
        self.db = self.root / "tv.db"
        self.devices = self.root / "devices"
        self.channels = self.root / "channels.txt"
        self.backups = self.root / "backups"
        self.run = self.root / "run"
        self.status = self.run / "status.json"
        self.firmware = self.root / "firmware"


# 镜像里带的那套固件（构建镜像时从最新的固件发布拷进来，packaging/docker/firmware/）。
IMAGE_FIRMWARE_ENV = "XIAOZHI_TV_FIRMWARE_DIR"


def firmware_dir(layout: Layout, image_copy: Path | None = None) -> Path | None:
    """网页刷机用哪一套固件：数据文件夹里的（用户自己放的、--fetch-firmware 下的）优先，
    也认把整个 xiaozhi-tv-flasher.zip 解压进去的样子（firmware/flash/firmware/），再是镜像里带的。
    有 manifest.json 才算；都没有时 None（刷机页会说没有固件）。"""
    candidates = [layout.firmware, layout.firmware / "flash" / "firmware"]
    if image_copy is not None:
        candidates.append(Path(image_copy))
    return next((c for c in candidates if (c / "manifest.json").is_file()), None)


def prepare(layout: Layout) -> None:
    """建目录、查能不能写、写出空的频道表。"""
    try:
        layout.root.mkdir(parents=True, exist_ok=True)
        for name in DIRS:
            (layout.root / name).mkdir(exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=layout.root, prefix=".write-test-"):
            pass
    except OSError as error:
        raise DataDirError(
            f"数据目录 {layout.root} 写不进去（{type(error).__name__}）。"
            "NAS 上多半是文件夹权限：把这个文件夹的读写权限给容器用的用户（PUID/PGID），"
            "或者在 compose.yaml 里把 PUID、PGID 改成这个文件夹主人的编号。") from None
    os.chmod(layout.secrets, 0o700)
    if not layout.channels.exists():
        layout.channels.write_text(CHANNELS_HEADER, encoding="utf-8")


def relay_key(layout: Layout) -> bool:
    """中转的密钥（「用手机听」的令牌、语音命令都从它来）：没有就生成；返回是不是新生成的。从不打印。"""
    if layout.relay_key.is_file() and len(layout.relay_key.read_text(encoding="utf-8").strip()) >= 16:
        return False
    _write_secret(layout.relay_key, secrets.token_urlsafe(32) + "\n")
    return True


def _write_secret(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="ascii") as handle:
        handle.write(text)
    os.replace(tmp, path)


def database(layout: Layout, clock=time.time) -> Accounts:
    """打开（或新建）数据库：关掉共享令牌、记下是自建的；表结构版本变了先备份一份。"""
    existed = layout.db.exists()
    accounts = Accounts(layout.db, clock=clock)
    accounts.set_meta(LEGACY_TOKEN_KEY, "0")
    accounts.set_meta(SELFHOST_META, "1")
    previous = accounts.meta(SCHEMA_META, "")
    if existed and previous and previous != str(db.SCHEMA_VERSION):
        backup(layout, accounts, name=f"tv-pre-{db.SCHEMA_VERSION}.db")
    accounts.set_meta(SCHEMA_META, str(db.SCHEMA_VERSION))
    return accounts


def new_setup_code() -> str:
    return "".join(secrets.choice(db.CODE_ALPHABET) for _ in range(SETUP_CODE_LENGTH))


def show_code(code: str) -> str:
    """K7P4QX2M9A → K7P4Q-X2M9A：日志里好认。"""
    return f"{code[:5]}-{code[5:]}"


def setup_code(layout: Layout, accounts: Accounts) -> str:
    """还没有管理员：写一个新的设置码（每次启动都换）并返回；已经有了：删掉旧文件，返回空串。"""
    if accounts.admin_id() is not None:
        try:
            layout.setup_code.unlink()
        except FileNotFoundError:
            pass
        return ""
    code = new_setup_code()
    _write_secret(layout.setup_code, code + "\n")
    return code


def backup(layout: Layout, accounts: Accounts | None = None, name: str = "", clock=time.time) -> Path:
    """用 SQLite 自己的在线备份（不用 sqlite3 命令行）拷一份数据库到 backups/。"""
    name = name or time.strftime("tv-%Y%m%d.db", time.localtime(clock()))
    target = layout.backups / name
    tmp = target.with_name(target.name + ".tmp")
    source = sqlite3.connect(str(layout.db))
    try:
        copy = sqlite3.connect(str(tmp))
        try:
            source.backup(copy)
        finally:
            copy.close()
    finally:
        source.close()
    os.chmod(tmp, 0o600)
    os.replace(tmp, target)
    return target


def prune_backups(layout: Layout, keep_days: int, clock=time.time) -> list[str]:
    """删掉超过 keep_days 天的每晚备份（tv-YYYYMMDD.db）；升级前的备份（tv-pre-*）不删。"""
    removed = []
    cutoff = time.strftime("%Y%m%d", time.localtime(clock() - keep_days * 86400))
    for path in sorted(layout.backups.glob("tv-[0-9]*.db")):
        stamp = path.stem[3:]
        if stamp.isdigit() and len(stamp) == 8 and stamp < cutoff:
            path.unlink()
            removed.append(path.name)
    return removed


def nightly_due(layout: Layout, clock=time.time, hour: int = 3) -> bool:
    """今天的备份还没做，而且已经过了凌晨 hour 点。"""
    now = time.localtime(clock())
    return now.tm_hour >= hour and not (layout.backups / time.strftime("tv-%Y%m%d.db", now)).exists()
