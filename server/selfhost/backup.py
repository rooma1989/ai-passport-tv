"""「导出 / 恢复全部设置」：数据目录里的设置和用户数据打成一个 zip（带清单），从这样的 zip 恢复，每天自动备份一份。

家庭服务器和电脑客户端的全部东西都在一个数据目录里（bootstrap.Layout）。备份只带设置和用户数据，不带能重新生成
的缓存和组件：

  总带着    tv.db（账号、设备、订阅、操作记录；用 SQLite 自己的在线备份拷一份）、config.toml、
            channels.txt 和它旁边的 .meta.json（线路、台标、节目单编号）、.subs.json（订阅的直播源）、
            devices/<id>/channel-groups.json（分组调整）、library.json（片库来源）、library-index.json、
            library-meta.json、library-art/（豆瓣海报）、pc-progress.json、live-plays.json、
            feeds/（首页账号几排的设置）、openlist/settings.json（网盘组件停用了没有）
  登录信息  secrets/（relay-key、wifi-key、ytnode-key、openlist-admin、bilibili/、youtube-node.json；
            一次性的 setup-code 从不带）、openlist/data/（网盘组件自己的数据库和 config.json，里面是网盘的登录；
            数据库同样用在线备份拷）、youtube/*.txt（YouTube cookies）
  从不带    library-cache/、library-full/（整集缓存）、library-subs/、covers/、logos/、epg/（下载的节目单，设置在 tv.db 里）、firmware/、
            bin/、youtube/bin/、openlist/bin/、openlist/data/temp|log/、run/、backups/、exports/、logs/、
            *.bak-*、desktop.json 和 tray.json（这台电脑自己的：开机启动、家里网络的身份）

「不含登录信息」（默认）时：上面「登录信息」一行不带；library.json 里来源的密码清空、config.toml 里 YouTube 代理
地址中的用户名密码去掉。恢复这样的备份时，这台机器现在的登录信息原样留着，片库来源的密码和代理的用户名密码
按来源 id / 代理地址从现在的文件补回去（merge_library、merge_config）。

加密：标准库里没有能用的对称加密（只有哈希），自己拼一个算法不可靠，所以「含登录信息」的备份不加密，页面上
明说这个文件里有密码和 cookies。

zip 的最上面一层有清单 rootv-backup.json：格式、版本、导出时间、程序版本、含不含登录信息、每个文件的路径、
大小、sha256。恢复分三步：
  1. inspect：查清单、哈希、路径（只认上面列出的路径，没有绝对路径、..、符号链接、加密项、重名），大小有上限。
  2. stage：解到 run/restore/ 里（边解边再算一遍哈希），最后写 ready.json 表示准备好了。正在跑的东西一样都不动。
  3. apply_pending：只在没有子进程在跑时做——监督进程（supervisor.py）看到 run/restore/ready.json 就先停掉所有
     子进程再做、做完再起；或者下次启动时、在打开数据库之前做（__main__.py、desktop_main.py）。先把现在的这些
     文件挪进 backups/pre-restore-<时间>/（同一个文件系统里改名），再把解好的文件挪到位；中间任何一步出错，
     把放上去的删掉、挪走的挪回来（不会只恢复一半）。结果写在 run/restore-result.json 给页面看。

每天自动备份（daily）：不含登录信息，backups/settings-YYYYMMDD.zip，留 DAILY_KEEP_DAYS 天（最新的一份总留着）；
文件的大小和修改时间都没变时不做，做了内容和上一份一样也不留。

命令行（没有监督进程时，例如只起了中转）：
  python3 -m server.selfhost.backup export --data <数据目录> [--logins] -o <文件>
  python3 -m server.selfhost.backup check <文件>
  python3 -m server.selfhost.backup apply --data <数据目录>     先停掉小鼠电视
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets as _secrets
import shutil
import sqlite3
import stat
import sys
import tempfile
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

FORMAT = "rootv-settings-backup"
FORMAT_VERSION = 1
MANIFEST = "rootv-backup.json"
# 上传的备份最多多大、解开后一共多大、一个文件多大、几个文件、清单多大。
UPLOAD_MAX = 256 * 1024 * 1024
TOTAL_MAX = 1024 * 1024 * 1024
FILE_MAX = 512 * 1024 * 1024
MEMBERS_MAX = 20000
MANIFEST_MAX = 8 * 1024 * 1024
CHUNK = 1024 * 1024
# 每天的自动备份留几天；恢复前留下的 pre-restore-* 留几份。
DAILY_KEEP_DAYS = 7
DAILY_HOUR = 3
PRE_RESTORE_KEEP = 5
DAILY_PREFIX = "settings-"
DAILY_STATE = "settings-last.json"
PRE_PREFIX = "pre-restore-"
STAGE_DIR = "restore"
READY = "ready.json"
RESULT = "restore-result.json"
UPLOAD = "restore-upload.zip"
UPLOAD_INFO = "restore-upload.json"

DB = "tv.db"
# 总带着的单个文件；DB_SIDE 只在恢复时随 tv.db 一起挪走（备份里从不带）。
BASE_FILES = (DB, "config.toml", "channels.txt", "channels.txt.meta.json", "channels.txt.subs.json",
              "library.json", "library-index.json", "library-meta.json", "pc-progress.json", "live-plays.json",
              "openlist/settings.json")
DB_SIDE = ("tv.db-wal", "tv.db-shm", "tv.db-journal")
# 总带着的文件夹（只认里面一层的文件）。
BASE_DIRS = ("library-art", "feeds")
DEVICE_FILE = "channel-groups.json"
# 登录信息：整个文件夹（可以有下一层），减去这些。
LOGIN_DIRS = ("secrets", "openlist/data")
LOGIN_SKIP_FILES = ("secrets/setup-code",)
LOGIN_SKIP_DIRS = ("openlist/data/temp", "openlist/data/log")
COOKIES_DIR = "youtube"
LOGIN_DEPTH = 4
_PART = re.compile(r"^[A-Za-z0-9_@+-][A-Za-z0-9._@+-]{0,127}$")
_DEVICE = re.compile(r"^(?:[0-9a-f]{12}|legacy-[a-z0-9]{1,20})$")
_SHA = re.compile(r"^[0-9a-f]{64}$")
_STAMP = re.compile(r"^settings-(\d{8})\.zip$")
_PROXY = re.compile(r"""(?m)^(\s*proxy\s*=\s*)(["'])([^"'\n]*)\2""")
_USERINFO = re.compile(r"^([A-Za-z][A-Za-z0-9+.-]*://)[^@/\s]*@")
SQLITE_MAGIC = b"SQLite format 3\x00"
SQLITE_SIDES = ("-wal", "-shm", "-journal")

# 页面上「会替换哪些」按这几组说。
GROUPS = (
    ("db", "账号、设备、订阅、节目单设置、操作记录（tv.db）"),
    ("config", "服务器设置（config.toml）"),
    ("channels", "直播频道表、备用线路、订阅的直播源"),
    ("groups", "各台小电视的分组调整"),
    ("library", "片库的来源和索引"),
    ("meta", "片库的豆瓣资料和海报"),
    ("progress", "电脑上的观看进度、常看的直播"),
    ("feeds", "首页「我的账号」几排的设置"),
    ("drive", "网盘组件的开关"),
    ("secrets", "密钥和登录（secrets/：B 站登录、YouTube 节点、网盘管理密码……）"),
    ("drivedata", "网盘组件的数据库（各个网盘的登录）"),
    ("cookies", "YouTube cookies"),
)


class BackupError(ValueError):
    """备份文件不对、或恢复不了：消息给人看。"""


# ---------------------------------------------------------------- 路径：哪些算、算哪一组

def _parts_ok(rel: str) -> bool:
    if not rel or len(rel) > 400 or rel.startswith("/") or "\\" in rel or "\0" in rel or ":" in rel:
        return False
    parts = rel.split("/")
    return all(_PART.match(p) and p not in (".", "..") for p in parts)


def _temp_name(name: str) -> bool:
    return name.endswith((".tmp", ".part")) or ".bak-" in name


def category(rel: str) -> tuple[str, bool] | None:
    """备份里的一个路径算哪一组、是不是登录信息；不认识的 None（这样的备份整个不收）。"""
    if not _parts_ok(rel) or _temp_name(rel.rsplit("/", 1)[-1]):
        return None
    parts = rel.split("/")
    if rel == DB:
        return "db", False
    if rel == "config.toml":
        return "config", False
    if rel.startswith("channels.txt"):
        return ("channels", False) if rel in BASE_FILES else None
    if rel in ("library.json", "library-index.json"):
        return "library", False
    if rel == "library-meta.json":
        return "meta", False
    if rel in ("pc-progress.json", "live-plays.json"):
        return "progress", False
    if rel == "openlist/settings.json":
        return "drive", False
    if len(parts) == 2 and parts[0] == "library-art":
        return "meta", False
    if len(parts) == 2 and parts[0] == "feeds":
        return "feeds", False
    if len(parts) == 3 and parts[0] == "devices" and _DEVICE.match(parts[1]) and parts[2] == DEVICE_FILE:
        return "groups", False
    if len(parts) == 2 and parts[0] == COOKIES_DIR and parts[1].endswith(".txt"):
        return "cookies", True
    if rel in LOGIN_SKIP_FILES or any(rel.startswith(d + "/") for d in LOGIN_SKIP_DIRS):
        return None
    if parts[0] == "secrets" and 2 <= len(parts) <= LOGIN_DEPTH:
        return "secrets", True
    if rel.startswith("openlist/data/") and 3 <= len(parts) <= LOGIN_DEPTH + 1:
        return "drivedata", True
    return None


def _regular(path: Path) -> bool:
    try:
        return stat.S_ISREG(path.lstat().st_mode)
    except OSError:
        return False


def _walk(root: Path, top: str, depth: int) -> list[str]:
    """top 下面的普通文件（不跟符号链接、不进符号链接的文件夹），最多 depth 层。"""
    found = []
    base = root / top
    try:
        if not stat.S_ISDIR(base.lstat().st_mode):
            return []
    except OSError:
        return []
    stack = [(base, top, 1)]
    while stack:
        folder, rel, level = stack.pop()
        try:
            entries = sorted(os.scandir(folder), key=lambda e: e.name)
        except OSError:
            continue
        for entry in entries:
            child = f"{rel}/{entry.name}"
            if entry.is_symlink():
                continue
            if entry.is_dir(follow_symlinks=False):
                if level < depth:
                    stack.append((Path(entry.path), child, level + 1))
            elif entry.is_file(follow_symlinks=False):
                found.append(child)
    return found


def collect(root: Path, logins: bool, moving: bool = False) -> list[str]:
    """数据目录里现在属于备份范围的文件（相对路径，排好序）。logins：连登录信息一起；
    moving：恢复时要先挪走的（多 tv.db 的 -wal、-shm，不管名字认不认识都算，免得旧的留在原处）。"""
    root = Path(root)
    found = [name for name in BASE_FILES if _regular(root / name)]
    if moving:
        found += [name for name in DB_SIDE if _regular(root / name)]
    for top in BASE_DIRS:
        found += _walk(root, top, 1)
    devices = root / "devices"
    if devices.is_dir() and not devices.is_symlink():
        for entry in sorted(os.scandir(devices), key=lambda e: e.name):
            rel = f"devices/{entry.name}/{DEVICE_FILE}"
            if _DEVICE.match(entry.name) and entry.is_dir(follow_symlinks=False) and _regular(root / rel):
                found.append(rel)
    if logins:
        for top in LOGIN_DIRS:
            found += _walk(root, top, LOGIN_DEPTH)
        found += [rel for rel in _walk(root, COOKIES_DIR, 1) if rel.endswith(".txt")]
    keep = []
    for rel in found:
        known = category(rel)
        if known is not None and (logins or not known[1]):
            keep.append(rel)
        elif moving and logins and rel.startswith(tuple(d + "/" for d in LOGIN_DIRS)) \
                and rel not in LOGIN_SKIP_FILES and not any(rel.startswith(d + "/") for d in LOGIN_SKIP_DIRS):
            # 恢复含登录信息的备份：这两个文件夹里名字不认识的（临时文件）也挪走，换成备份里的样子。
            keep.append(rel)
    if moving:
        keep += [rel for rel in DB_SIDE if rel in found and rel not in keep]
    return sorted(set(keep))


# ---------------------------------------------------------------- 去掉 / 补回登录信息

def scrub_library(data: bytes) -> bytes | None:
    """library.json 不带密码：来源的 password 清空。看不懂时 None（这个文件就不带了，免得带着密码）。"""
    try:
        config = json.loads(data.decode("utf-8"))
    except (UnicodeError, ValueError):
        return None
    if not isinstance(config, dict):
        return None
    for source in config.get("sources") or []:
        if isinstance(source, dict) and source.get("password"):
            source["password"] = ""
    return json.dumps(config, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def merge_library(restored: bytes, current: bytes | None) -> bytes:
    """恢复不含登录信息的 library.json：密码是空的来源，按 id（地址和用户名也一样时）用现在文件里的密码。"""
    try:
        config = json.loads(restored.decode("utf-8"))
        old = json.loads(current.decode("utf-8")) if current else {}
    except (UnicodeError, ValueError, AttributeError):
        return restored
    if not isinstance(config, dict) or not isinstance(old, dict):
        return restored
    known = {s.get("id"): s for s in old.get("sources") or [] if isinstance(s, dict)}
    changed = False
    for source in config.get("sources") or []:
        if not isinstance(source, dict) or source.get("password"):
            continue
        before = known.get(source.get("id"))
        if before and before.get("password") and (before.get("url"), before.get("user")) == \
                (source.get("url"), source.get("user")):
            source["password"] = before["password"]
            changed = True
    return json.dumps(config, ensure_ascii=False, separators=(",", ":")).encode("utf-8") if changed else restored


def _strip_userinfo(url: str) -> str:
    return _USERINFO.sub(r"\1", url)


def scrub_config(text: str) -> str:
    """config.toml 不带登录信息：[youtube] proxy 地址里的「用户名:密码@」去掉。"""
    return _PROXY.sub(lambda m: m.group(1) + m.group(2) + _strip_userinfo(m.group(3)) + m.group(2), text)


def merge_config(restored: str, current: str | None) -> str:
    """恢复不含登录信息的 config.toml：代理地址去掉用户名密码后和现在的一样，就用现在带着用户名密码的那个。"""
    if not current:
        return restored
    found = _PROXY.search(current)
    if not found or found.group(3) == _strip_userinfo(found.group(3)):
        return restored
    full = found.group(3)

    def put(match):
        value = match.group(3)
        return match.group(1) + match.group(2) + (full if value == _strip_userinfo(full) else value) + match.group(2)
    return _PROXY.sub(put, restored)


# ---------------------------------------------------------------- 导出

def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def snapshot_sqlite(source: Path, target: Path) -> None:
    """SQLite 自己的在线备份（别的进程开着它、有 WAL 也是一致的一份）。"""
    db = sqlite3.connect(f"file:{source}?mode=ro", uri=True, timeout=10)
    try:
        copy = sqlite3.connect(str(target))
        try:
            db.backup(copy)
        finally:
            copy.close()
    finally:
        db.close()


def _zip_info(rel: str, mtime: float) -> zipfile.ZipInfo:
    stamp = time.localtime(max(mtime, 315532800 + 86400))
    info = zipfile.ZipInfo(rel, date_time=stamp[:6])
    info.compress_type = zipfile.ZIP_STORED if rel.endswith((".jpg", ".jpeg", ".png", ".webp")) \
        else zipfile.ZIP_DEFLATED
    info.external_attr = (stat.S_IFREG | 0o600) << 16
    return info


def export(root: Path, target: Path, logins: bool = False, clock=time.time, version: str = "") -> dict:
    """把数据目录的设置打成 target（zip）；返回清单。target 的文件夹里会临时放数据库的拷贝。"""
    root, target = Path(root), Path(target)
    if not version:
        from .version import VERSION as version
    files, skipped = [], []
    work = Path(tempfile.mkdtemp(prefix=".backup-", dir=target.parent))
    try:
        wanted = collect(root, logins)
        with zipfile.ZipFile(target, "w", allowZip64=True) as archive:
            for rel in wanted:
                path = root / rel
                if rel.endswith(SQLITE_SIDES) and rel.rsplit("-", 1)[0] in wanted:
                    continue        # 数据库用在线备份拷，旁边的 -wal、-shm 已经在里面了，不能再带旧的
                try:
                    mtime = path.stat().st_mtime
                    source: Path | None = path
                    data: bytes | None = None
                    if rel == DB or (logins and rel.startswith("openlist/data/") and _is_sqlite(path)):
                        source = work / "db"
                        snapshot_sqlite(path, source)
                    elif rel == "library.json" and not logins:
                        data = scrub_library(path.read_bytes())
                        if data is None:
                            skipped.append(rel)
                            continue
                        source = None
                    elif rel == "config.toml" and not logins:
                        data = scrub_config(path.read_text(encoding="utf-8")).encode("utf-8")
                        source = None
                except FileNotFoundError:
                    continue        # 正好被删了（例如换了一份缓存）
                except (OSError, sqlite3.Error, UnicodeError):
                    skipped.append(rel)
                    continue
                digest, size = hashlib.sha256(), 0
                # 一个文件最多 FILE_MAX（512 MB），用不着 ZIP64 的本地头：系统自带的解压工具都认。
                with archive.open(_zip_info(rel, mtime), "w") as out:
                    if data is not None:
                        digest.update(data)
                        out.write(data)
                        size = len(data)
                    else:
                        with open(source, "rb") as stream:
                            for chunk in iter(lambda: stream.read(CHUNK), b""):
                                digest.update(chunk)
                                out.write(chunk)
                                size += len(chunk)
                files.append({"path": rel, "size": size, "sha256": digest.hexdigest()})
                if source is not None and source.parent == work:
                    source.unlink()
            now = clock()
            manifest = {"format": FORMAT, "version": FORMAT_VERSION, "app_version": version,
                        "created_at": int(now), "created": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
                        "logins": bool(logins), "files": files, "skipped": skipped}
            archive.writestr(_zip_info(MANIFEST, now), json.dumps(manifest, ensure_ascii=False, indent=1))
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return manifest


def _is_sqlite(path: Path) -> bool:
    try:
        with open(path, "rb") as stream:
            return stream.read(16) == SQLITE_MAGIC
    except OSError:
        return False


def export_name(manifest: dict) -> str:
    stamp = time.strftime("%Y%m%d-%H%M", time.localtime(manifest["created_at"]))
    return f"RooTV-backup-{stamp}" + ("-with-logins" if manifest["logins"] else "") + ".zip"


# ---------------------------------------------------------------- 检查

@dataclass
class Plan:
    manifest: dict
    files: dict = field(default_factory=dict)       # 路径 → {"size", "sha256"}

    @property
    def logins(self) -> bool:
        return bool(self.manifest.get("logins"))


def _reject(message: str):
    raise BackupError(message)


def inspect(path: Path) -> Plan:
    """查一个备份文件：清单、路径、大小、哈希都对才返回；不对抛 BackupError（说哪里不对）。"""
    path = Path(path)
    try:
        size = path.stat().st_size
    except OSError:
        _reject("读不到上传的文件")
    if size > UPLOAD_MAX:
        _reject(f"备份文件太大（最多 {UPLOAD_MAX // 1048576} MB）")
    if not zipfile.is_zipfile(path):
        _reject("这不是小鼠电视导出的备份（不是 zip 文件）")
    try:
        with zipfile.ZipFile(path) as archive:
            return _inspect(archive)
    except (zipfile.BadZipFile, zipfile.LargeZipFile, OSError, EOFError, NotImplementedError, RuntimeError) as error:
        raise BackupError(f"备份文件坏了（{type(error).__name__}）") from None


def _inspect(archive: zipfile.ZipFile) -> Plan:
    infos = archive.infolist()
    if len(infos) > MEMBERS_MAX + 1:
        _reject("备份里的文件太多")
    seen, lowered = {}, set()
    for info in infos:
        name = info.filename
        mode = info.external_attr >> 16
        kind = stat.S_IFMT(mode)
        if info.is_dir() or name.endswith("/"):
            _reject(f"备份里不该有文件夹项：{name[:80]}")
        if kind == stat.S_IFLNK:
            _reject(f"备份里有符号链接，不收：{name[:80]}")
        if kind not in (0, stat.S_IFREG):
            _reject(f"备份里有不是普通文件的项：{name[:80]}")
        if info.flag_bits & 0x1:
            _reject("备份里有加密的项，不收")
        if name != MANIFEST and not _parts_ok(name):
            _reject(f"备份里有不安全的路径（绝对路径、..、反斜杠之类）：{name[:80]}")
        if name in seen or name.lower() in lowered:
            _reject(f"备份里有重名的文件：{name[:80]}")
        seen[name] = info
        lowered.add(name.lower())
    info = seen.pop(MANIFEST, None)
    if info is None:
        _reject("备份里没有清单（rootv-backup.json）：不是小鼠电视导出的备份")
    if info.file_size > MANIFEST_MAX:
        _reject("备份的清单太大")
    try:
        manifest = json.loads(archive.read(MANIFEST).decode("utf-8"))
    except (UnicodeError, ValueError):
        _reject("备份的清单看不懂")
    if not isinstance(manifest, dict) or manifest.get("format") != FORMAT:
        _reject("备份的清单看不懂：不是小鼠电视的设置备份")
    version = manifest.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        _reject("备份的清单看不懂（没有版本）")
    if version > FORMAT_VERSION:
        _reject("这份备份是更新版本的小鼠电视导出的：先把小鼠电视更新到最新版再恢复")
    if not isinstance(manifest.get("logins"), bool) or not isinstance(manifest.get("files"), list) \
            or not isinstance(manifest.get("created_at"), (int, float)):
        _reject("备份的清单看不懂")
    logins = manifest["logins"]
    plan, total = Plan(manifest), 0
    for row in manifest["files"]:
        if not isinstance(row, dict):
            _reject("备份的清单看不懂")
        rel, size, digest = row.get("path"), row.get("size"), row.get("sha256")
        if not isinstance(rel, str) or not isinstance(size, int) or isinstance(size, bool) \
                or not isinstance(digest, str) or not _SHA.match(digest):
            _reject("备份的清单看不懂")
        known = category(rel)
        if known is None:
            _reject(f"备份里有不认识的路径：{rel[:80]}")
        if known[1] and not logins:
            _reject(f"清单说不含登录信息，却带着登录信息：{rel[:80]}")
        if rel in plan.files:
            _reject(f"清单里有重名的文件：{rel[:80]}")
        if rel not in seen:
            _reject(f"清单里列的文件不在备份里：{rel[:80]}")
        if not 0 <= size <= FILE_MAX or seen[rel].file_size != size:
            _reject(f"文件大小和清单对不上：{rel[:80]}")
        total += size
        if total > TOTAL_MAX:
            _reject(f"备份解开后太大（最多 {TOTAL_MAX // 1048576} MB）")
        plan.files[rel] = {"size": size, "sha256": digest}
    extra = sorted(set(seen) - set(plan.files))
    if extra:
        _reject(f"备份里有清单没列的文件：{extra[0][:80]}")
    if DB not in plan.files:
        _reject("备份里没有数据库（tv.db）")
    for rel, row in plan.files.items():
        _check_member(archive, rel, row, None)
    return plan


def _check_member(archive: zipfile.ZipFile, rel: str, row: dict, out) -> None:
    """读一个文件、比大小和哈希（out 不是 None 时同时写出去）。读多了也停（不信 zip 里写的大小）。"""
    digest, size, head = hashlib.sha256(), 0, b""
    with archive.open(rel) as stream:
        while True:
            chunk = stream.read(CHUNK)
            if not chunk:
                break
            size += len(chunk)
            if size > row["size"]:
                _reject(f"文件大小和清单对不上：{rel[:80]}")
            if len(head) < 16:
                head += chunk[:16 - len(head)]
            digest.update(chunk)
            if out is not None:
                out.write(chunk)
    if size != row["size"] or digest.hexdigest() != row["sha256"]:
        _reject(f"文件的校验和对不上（备份坏了或被改过）：{rel[:80]}")
    if rel == DB and head != SQLITE_MAGIC:
        _reject("备份里的 tv.db 不是 SQLite 数据库")


def summary(plan: Plan, root: Path) -> dict:
    """给页面看：备份的时间、版本、含不含登录信息，每组备份里有多少、现在有多少（会被替换掉的）。"""
    current = {rel: _size(Path(root) / rel) for rel in collect(root, plan.logins)}
    groups = []
    for key, label in GROUPS:
        theirs = [r for r in plan.files if category(r)[0] == key]
        ours = [r for r in current if category(r)[0] == key]
        if not theirs and not ours:
            continue
        groups.append({"key": key, "label": label, "logins": key in ("secrets", "drivedata", "cookies"),
                       "files": len(theirs), "bytes": sum(plan.files[r]["size"] for r in theirs),
                       "current_files": len(ours), "current_bytes": sum(current[r] for r in ours)})
    manifest = plan.manifest
    return {"created_at": manifest["created_at"], "app_version": str(manifest.get("app_version") or "")[:40],
            "logins": plan.logins, "files": len(plan.files),
            "bytes": sum(row["size"] for row in plan.files.values()), "groups": groups}


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


# ---------------------------------------------------------------- 准备恢复（解到 run/restore/）

def run_dir(root: Path) -> Path:
    return Path(root) / "run"


def stage_dir(root: Path) -> Path:
    return run_dir(root) / STAGE_DIR


def pending(root: Path) -> bool:
    """有一份解好、等着替换的备份（run/restore/ready.json）。"""
    return (stage_dir(root) / READY).is_file()


def activate(root: Path) -> None:
    """stage(…, ready=False) 解好的那份（run/restore.part/）改名成 run/restore/：监督进程看到就开始替换。"""
    part = run_dir(root) / (STAGE_DIR + ".part")
    if not (part / READY).is_file():
        raise BackupError("没有解好的备份")
    shutil.rmtree(stage_dir(root), ignore_errors=True)
    os.replace(part, stage_dir(root))


def stage(archive_path: Path, root: Path, actor: str = "", clock=time.time, ready: bool = True) -> Plan:
    """查过以后解到 run/restore/files/，最后写 ready.json。现在的数据一样都不动。
    ready=False：先留在 run/restore.part/，调用方回答完页面再 activate（监督进程一看到就会停掉中转）。"""
    plan = inspect(archive_path)
    run = run_dir(root)
    run.mkdir(parents=True, exist_ok=True)
    part = run / (STAGE_DIR + ".part")
    shutil.rmtree(part, ignore_errors=True)
    files = part / "files"
    files.mkdir(parents=True)
    os.chmod(part, 0o700)
    try:
        with zipfile.ZipFile(archive_path) as archive:
            for rel, row in plan.files.items():
                target = files / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as out:
                    _check_member(archive, rel, row, out)
        info = {"manifest": plan.manifest, "files": plan.files, "actor": str(actor)[:40], "staged_at": int(clock())}
        (part / READY).write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
        if ready:
            activate(root)
    except BaseException:
        shutil.rmtree(part, ignore_errors=True)
        raise
    return plan


def cancel(root: Path) -> bool:
    """不恢复了：删掉解好的那份。返回有没有删掉东西。"""
    found = False
    for folder in (stage_dir(root), run_dir(root) / (STAGE_DIR + ".part")):
        if folder.exists():
            shutil.rmtree(folder, ignore_errors=True)
            found = True
    return found


# ---------------------------------------------------------------- 替换（只在没有子进程在跑时）

def _place(source: Path, target: Path) -> None:
    """把一个文件挪到 target（同一个文件系统里改名）。单独一个函数：测试在这里造出错。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    os.replace(source, target)


def _pre_folder(root: Path, clock) -> Path:
    backups = Path(root) / "backups"
    backups.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(clock()))
    folder, n = backups / f"{PRE_PREFIX}{stamp}", 1
    while folder.exists():
        n += 1
        folder = backups / f"{PRE_PREFIX}{stamp}-{n}"
    folder.mkdir(mode=0o700)
    return folder


def apply_pending(root: Path, log=print, clock=time.time) -> dict | None:
    """有解好的备份就替换：现在的文件先挪进 backups/pre-restore-<时间>/，再把备份里的挪到位；出错全部挪回去。
    只能在没有子进程开着这些文件时调（监督进程停掉子进程以后，或启动时打开数据库之前）。
    返回结果（也写在 run/restore-result.json）；没有要恢复的 None。"""
    root = Path(root)
    folder = stage_dir(root)
    if not (folder / READY).is_file():
        return None
    result = {"ok": False, "at": int(clock()), "error": "", "backup": ""}
    try:
        ready = json.loads((folder / READY).read_text(encoding="utf-8"))
        manifest, files = ready["manifest"], ready["files"]
        logins = bool(manifest.get("logins"))
        result.update(created_at=manifest.get("created_at"), logins=logins, actor=ready.get("actor") or "")
        for rel, row in files.items():
            if category(rel) is None or (category(rel)[1] and not logins):
                raise BackupError(f"不认识的路径：{rel[:80]}")
            staged = folder / "files" / rel
            if not _regular(staged) or staged.stat().st_size != row["size"] or _sha256_file(staged) != row["sha256"]:
                raise BackupError(f"解好的文件对不上：{rel[:80]}")
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        result["error"] = error.args[0] if isinstance(error, BackupError) else f"解好的备份读不了（{type(error).__name__}）"
        log(f"恢复设置：没有恢复，{result['error']}")
        _finish(root, result)
        return result

    try:
        pre = _pre_folder(root, clock)
    except OSError as error:
        result["error"] = f"建不了备份文件夹 backups/（{type(error).__name__}），没有恢复"
        log(f"恢复设置：{result['error']}")
        _finish(root, result)
        return result
    result["backup"] = pre.name
    moved, placed = [], []
    try:
        current = collect(root, logins, moving=True)
        (pre / "restore.json").write_text(json.dumps({"from": manifest, "moved": current}, ensure_ascii=False),
                                          encoding="utf-8")
        for rel in current:
            _place(root / rel, pre / "files" / rel)
            moved.append(rel)
        for rel in sorted(files):
            target = root / rel
            if rel.startswith("secrets/"):
                (root / "secrets").mkdir(mode=0o700, exist_ok=True)
            _place(folder / "files" / rel, target)
            placed.append(rel)
            os.chmod(target, 0o600)
        if not logins:
            _merge_back(root, pre / "files", files)
    except Exception as error:      # noqa: BLE001 —— 不管什么错都要整个挪回去
        problems = _rollback(root, pre / "files", moved, placed)
        result["error"] = f"替换文件时出错（{type(error).__name__}），已经全部换回原来的"
        if problems:
            result["error"] += f"；有 {len(problems)} 个文件没换回去，在 backups/{pre.name}/ 里"
        log(f"恢复设置：{result['error']}：{error}")
        _finish(root, result)
        return result
    result["ok"] = True
    log(f"恢复设置：已从 {manifest.get('created', '')} 的备份恢复 {len(files)} 个文件；原来的在 backups/{pre.name}/")
    _audit(root, result, manifest, clock)
    _prune_pre(root)
    _finish(root, result)
    return result


def _merge_back(root: Path, old: Path, files: dict) -> None:
    """不含登录信息的备份：片库来源的密码、代理的用户名密码从原来的文件补回来。"""
    if "library.json" in files:
        path = root / "library.json"
        before = (old / "library.json").read_bytes() if (old / "library.json").is_file() else None
        merged = merge_library(path.read_bytes(), before)
        if merged != path.read_bytes():
            _write_private(path, merged)
    if "config.toml" in files:
        path = root / "config.toml"
        before = (old / "config.toml").read_text(encoding="utf-8") if (old / "config.toml").is_file() else None
        text = path.read_text(encoding="utf-8")
        merged = merge_config(text, before)
        if merged != text:
            _write_private(path, merged.encode("utf-8"))


def _write_private(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as out:
        out.write(data)
    os.replace(tmp, path)


def _rollback(root: Path, old: Path, moved: list[str], placed: list[str]) -> list[str]:
    """删掉放上去的、挪回挪走的；返回没挪回去的。"""
    for rel in reversed(placed):
        try:
            (root / rel).unlink()
        except OSError:
            pass
    problems = []
    for rel in reversed(moved):
        try:
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            os.replace(old / rel, root / rel)
        except OSError:
            problems.append(rel)
    return problems


def _finish(root: Path, result: dict) -> None:
    shutil.rmtree(stage_dir(root), ignore_errors=True)
    try:
        run = run_dir(root)
        run.mkdir(parents=True, exist_ok=True)
        tmp = run / (RESULT + ".tmp")
        tmp.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, run / RESULT)
    except OSError:
        pass


def last_result(root: Path) -> dict | None:
    try:
        data = json.loads((run_dir(root) / RESULT).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _audit(root: Path, result: dict, manifest: dict, clock) -> None:
    """恢复好的数据库里记一条（恢复的是导出时的数据库，页面上点「恢复」的那条记录不在里面）。不改表结构，
    出错也不要紧。"""
    try:
        db = sqlite3.connect(str(Path(root) / DB), timeout=5)
        try:
            detail = f"{manifest.get('created', '')} 的备份" + ("（含登录信息）" if result.get("logins") else "") + \
                (f"，{result['actor']} 操作" if result.get("actor") else "")
            db.execute("INSERT INTO audit_log(at, actor_user, actor_ip_prefix, action, target_kind, target_id, detail) "
                       "VALUES (?, NULL, '', 'backup.restored', 'backup', ?, ?)",
                       (clock(), result["backup"], detail[:200]))
            db.commit()
        finally:
            db.close()
    except sqlite3.Error:
        pass


def _prune_pre(root: Path) -> None:
    folders = sorted(p for p in (Path(root) / "backups").glob(PRE_PREFIX + "*") if p.is_dir())
    for old in folders[:-PRE_RESTORE_KEEP]:
        shutil.rmtree(old, ignore_errors=True)


# ---------------------------------------------------------------- 每天自动备份

def _fingerprint(root: Path) -> str:
    digest = hashlib.sha256()
    for rel in collect(root, False):
        try:
            info = (Path(root) / rel).stat()
        except OSError:
            continue
        digest.update(f"{rel}\0{info.st_size}\0{info.st_mtime_ns}\n".encode("utf-8"))
    return digest.hexdigest()


def _content(manifest: dict) -> str:
    return hashlib.sha256(json.dumps([[f["path"], f["sha256"]] for f in manifest["files"]]).encode()).hexdigest()


def daily_due(root: Path, clock=time.time, hour: int = DAILY_HOUR) -> bool:
    now = time.localtime(clock())
    name = DAILY_PREFIX + time.strftime("%Y%m%d", now) + ".zip"
    return now.tm_hour >= hour and not (Path(root) / "backups" / name).exists()


def daily(root: Path, keep_days: int = DAILY_KEEP_DAYS, clock=time.time) -> str:
    """每天的自动备份（不含登录信息）。返回 "made"（新做了一份）、"same"（和上一份一样，没留）、
    "unchanged"（文件都没动过，没做）。做完删掉超过 keep_days 天的（最新的一份总留着）。"""
    root = Path(root)
    backups = root / "backups"
    backups.mkdir(parents=True, exist_ok=True)
    state_path = backups / DAILY_STATE
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    if not isinstance(state, dict):
        state = {}
    last_file = backups / str(state.get("file") or "-")
    fingerprint = _fingerprint(root)
    outcome = "unchanged"
    if not (fingerprint == state.get("fingerprint") and last_file.is_file()):
        name = DAILY_PREFIX + time.strftime("%Y%m%d", time.localtime(clock())) + ".zip"
        tmp = backups / f".{name}.{_secrets.token_hex(4)}.part"
        try:
            manifest = export(root, tmp, logins=False, clock=clock)
            content = _content(manifest)
            if content == state.get("content") and last_file.is_file():
                tmp.unlink()
                outcome = "same"
            else:
                os.chmod(tmp, 0o600)
                os.replace(tmp, backups / name)
                state.update(file=name, content=content)
                outcome = "made"
        finally:
            if tmp.exists():
                tmp.unlink()
        state["fingerprint"] = fingerprint
        tmp_state = state_path.with_name(state_path.name + ".tmp")
        tmp_state.write_text(json.dumps(state), encoding="utf-8")
        os.replace(tmp_state, state_path)
    prune_daily(root, keep_days, clock)
    return outcome


def prune_daily(root: Path, keep_days: int = DAILY_KEEP_DAYS, clock=time.time) -> list[str]:
    cutoff = time.strftime("%Y%m%d", time.localtime(clock() - keep_days * 86400))
    found = sorted((m.group(1), p) for p in (Path(root) / "backups").glob(DAILY_PREFIX + "*.zip")
                   if (m := _STAMP.match(p.name)))
    removed = []
    for stamp, path in found[:-1]:
        if stamp < cutoff:
            path.unlink()
            removed.append(path.name)
    return removed


def daily_list(root: Path) -> list[dict]:
    rows = []
    for path in sorted((Path(root) / "backups").glob(DAILY_PREFIX + "*.zip"), reverse=True):
        if _STAMP.match(path.name):
            info = path.stat()
            rows.append({"name": path.name, "bytes": info.st_size, "at": int(info.st_mtime)})
    return rows


# ---------------------------------------------------------------- 命令行

def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(prog="python3 -m server.selfhost.backup", description="导出 / 恢复小鼠电视的设置")
    sub = parser.add_subparsers(dest="command", required=True)
    out = sub.add_parser("export", help="导出设置")
    out.add_argument("--data", type=Path, required=True)
    out.add_argument("--logins", action="store_true", help="连登录信息（密钥、网盘登录、cookies）一起；文件不加密")
    out.add_argument("-o", "--output", type=Path, required=True)
    check = sub.add_parser("check", help="查一个备份文件")
    check.add_argument("file", type=Path)
    apply = sub.add_parser("apply", help="替换成准备好的备份（先停掉小鼠电视）")
    apply.add_argument("--data", type=Path, required=True)
    apply.add_argument("--file", type=Path, help="先准备这个备份文件，再替换")
    args = parser.parse_args(argv)
    try:
        if args.command == "export":
            args.output.parent.mkdir(parents=True, exist_ok=True)
            manifest = export(args.data, args.output, logins=args.logins)
            print(f"导出了 {len(manifest['files'])} 个文件到 {args.output}"
                  + ("（含登录信息，文件没有加密，保管好）" if args.logins else ""))
        elif args.command == "check":
            plan = inspect(args.file)
            info = summary(plan, Path(tempfile.gettempdir()) / "rootv-none")
            print(f"备份没问题：{info['files']} 个文件，" + ("含" if plan.logins else "不含") + "登录信息")
        else:
            if args.file is not None:
                stage(args.file, args.data, actor="命令行")
            result = apply_pending(args.data)
            if result is None:
                print("没有准备好的备份（run/restore/）")
                return 1
            print("恢复好了" if result["ok"] else f"没有恢复：{result['error']}")
            return 0 if result["ok"] else 1
    except BackupError as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
