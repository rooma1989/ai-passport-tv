"""网盘组件：OpenList（https://github.com/OpenListTeam/OpenList，AGPL-3.0，Alist 的社区分支）。它把百度网盘、
夸克这些挂成一个 WebDAV，片库（server/relay/library.py）就能扫描、在电脑上放。不随安装包发：管理员在片库里点
「安装网盘组件」（或者「检查更新」）时下载到 <数据目录>/openlist/bin/，和 YouTube 的 yt-dlp、deno 一个做法
（ytcomponents.py：下载、代理、镜像、核对哈希都用那里的）。

从哪来：OpenList 自己在 GitHub 上发布的文件，每个平台一个（-lite 版：和完整版是同一个程序，只是网页里少了在线
编辑文本用的编辑器，小 15 MB）。Linux 用 musl 静态编译的那份（不挑系统的 glibc 版本，Docker 镜像里也能跑）。
压缩包里只有一个文件 openlist（Windows 是 openlist.exe）：只取它，别的什么都不解开；压缩包里有指到外面的名字、
链接、设备文件的，整个不要。

怎么保证下到的是对的：
- 第一次安装：版本、每个平台的文件名和 SHA-256 写死在下面（取自 GitHub 发布页每个文件的 digest，
  gh api repos/OpenListTeam/OpenList/releases/tags/v4.2.9；本机的那份另外下载下来算过一遍）。对不上就删掉、
  什么都不运行。镜像、代理换掉文件也没用。
- 「检查更新」：先问最新的版本号（releases/latest 跳转到的 /tag/<版本>），再问 GitHub 的接口那个版本每个文件的
  digest（api.github.com，不经镜像），按它核对。这时信任的是 GitHub 本身。
- 装好的记在 bin/components.json：{"openlist": {"version", "file", "sha256", "asset"}}。文件名带版本，换版本时先
  放好新文件、再原子地改 components.json、最后删旧文件；监督进程按 components.json 起，任何时候都只看到完整的一个。

怎么跑（监督进程的子进程 openlist，只在装好了而且没停用时有，见 __main__.build_children）：子进程是这个模块的
main()（python -m server.selfhost.openlist，打包版 rootv-core __openlist），它再起 openlist server：
- 只听 127.0.0.1：每次启动前改它的 config.json（scheme.address = 127.0.0.1、端口、关掉 HTTPS、unix 套接字、
  S3、FTP、SFTP；force = false），再用它认的环境变量（OPENLIST_ADDR、OPENLIST_HTTP_PORT……，env 优先于
  config.json）同样设一遍。两样都是照着 v4.2.9 的源代码（internal/bootstrap/config.go、internal/conf/config.go）
  和真跑一次（lsof 看它听的地址）确认的。
- 管理员 admin 的密码：安装时生成（secrets/openlist-admin，600），用它自己的命令行设：
  openlist admin set <密码> --data <数据目录>（数据库里还没有管理员时，同时给 OPENLIST_ADMIN_PASSWORD，
  它初始化时就按这个建出 admin——v4.2.9 没有这个变量时不建管理员，而是等网页上的安装向导，那样任何能连上
  这个端口的程序都能抢先设）。密码不进日志、不进命令行以外的地方；它第一次建管理员时会把密码打到标准输出，
  这里转发输出时把带 password / token 的行整行去掉。
- 监督进程停它：POSIX 上它和这个包装在同一个进程组里（监督进程结束整组）；监督进程被强行杀掉时这个包装读到
  标准输入的文件尾，先停 openlist 再退出（所以 tools/packaged_entry.py 不给 __openlist 装 parentwatch 的
  os._exit）。Windows 上作业对象把子孙一起结束。
- 起好以后（/ping 回答 pong）写 run/openlist.json（端口、版本），中转按它维护片库里的「网盘」来源。

只用标准库。
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import platform
import re
import secrets
import signal
import stat
import string
import subprocess
import sys
import tarfile
import threading
import time
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

from .ytcomponents import CHUNK, ComponentError, _network_error, _remove, download, http_get, release_url

VERSION = "v4.2.9"
REPO = "OpenListTeam/OpenList"
REPO_URL = "https://github.com/OpenListTeam/OpenList"
LICENSE = "AGPL-3.0"
API = "https://api.github.com"
# 文件名 → (SHA-256, 字节数)。v4.2.9 发布页每个文件的 digest（gh api …/releases/tags/v4.2.9）。
ASSETS = {
    "openlist-darwin-arm64-lite.tar.gz": ("a4241527f1f74e786ec33accb3cebc7008ea1ba5717a462b0af23a1ac6565e88", 36562945),
    "openlist-darwin-amd64-lite.tar.gz": ("72808a4436f20ec1d652c02389820f22bc19ae06199efa5aa6cb3587a0495941", 38791325),
    "openlist-windows-amd64-lite.zip": ("1b10524f9e76bca436f2dd4307b549cd4229aa0678618d3c9e8571b933d9c9d1", 39144164),
    "openlist-windows-arm64-lite.zip": ("9d9753315bd00db93bd8f18b30eeb7e6408c55c5344c3d8c58874da5fd84b4c3", 35700601),
    "openlist-linux-musl-amd64-lite.tar.gz": ("3fe61423fb46b76efe53ba571abaae7ada4b5a964bbfa116ae3080b17c76682f", 38777254),
    "openlist-linux-musl-arm64-lite.tar.gz": ("166d6cdf1e3a11d258083dbe4cc013f1ca70740b05f39c324930a724fa7a7256", 35696495),
}
# (platform.system(), platform.machine()) → 文件名。电脑客户端发的平台（docs/desktop）和 Windows ARM、Linux ARM。
PLATFORMS = {
    ("Darwin", "arm64"): "openlist-darwin-arm64-lite.tar.gz",
    ("Darwin", "x86_64"): "openlist-darwin-amd64-lite.tar.gz",
    ("Windows", "AMD64"): "openlist-windows-amd64-lite.zip",
    ("Windows", "ARM64"): "openlist-windows-arm64-lite.zip",
    ("Linux", "x86_64"): "openlist-linux-musl-amd64-lite.tar.gz",
    ("Linux", "aarch64"): "openlist-linux-musl-arm64-lite.tar.gz",
}
COMPONENTS_FILE = "components.json"
SETTINGS_FILE = "settings.json"
STATUS_FILE = "openlist.json"
SECRET = "openlist-admin"
USERNAME = "admin"
# 本机的端口：47444 空着就用它，否则随便一个空的（__main__.openlist_port）。
PORT = 47444
_TAG = re.compile(r"^v(\d{1,4})\.(\d{1,4})\.(\d{1,4})$")
_SHA = re.compile(r"^[0-9a-f]{64}$")
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
# 转发 openlist 的输出时整行去掉的（它第一次建管理员时打印密码）。
_SECRET_LINE = re.compile(r"(?i)password|passwd|token|secret|cookie")
DOWNLOAD_MAX = 120 * 1024 * 1024
BINARY_MAX = 400 * 1024 * 1024
ARCHIVE_MEMBERS_MAX = 64
RELEASE_JSON_MAX = 4 * 1024 * 1024
RUN_TIMEOUT_S = 60
STOP_WAIT_S = 8.0
PING_TIMEOUT_S = 2.0
PASSWORD_LENGTH = 24
_CREATE_NO_WINDOW = 0x08000000


def platform_asset(system: str | None = None, machine: str | None = None) -> str | None:
    system = platform.system() if system is None else system
    machine = platform.machine() if machine is None else machine
    if system == "Linux" and machine == "arm64":
        machine = "aarch64"
    if system == "Windows" and machine == "x86_64":
        machine = "AMD64"
    return PLATFORMS.get((system, machine))


def exe_name(system: str | None = None) -> str:
    return "openlist.exe" if (system or platform.system()) == "Windows" else "openlist"


def version_key(tag: str) -> tuple[int, ...]:
    match = _TAG.match(tag or "")
    return tuple(int(part) for part in match.groups()) if match else ()


def _run_kwargs() -> dict:
    return {"creationflags": _CREATE_NO_WINDOW} if os.name == "nt" else {}


# ---------------------------------------------------------------- 文件的位置

class Paths:
    """数据目录里网盘组件的东西：openlist/bin（程序）、openlist/data（它自己的数据库和 config.json）、
    openlist/settings.json（停用了没有）、secrets/openlist-admin（管理员密码）、run/openlist.json（在跑的端口）。"""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.home = self.root / "openlist"
        self.bin = self.home / "bin"
        self.data = self.home / "data"
        self.settings = self.home / SETTINGS_FILE
        self.secret = self.root / "secrets" / SECRET
        self.status = self.root / "run" / STATUS_FILE


# ---------------------------------------------------------------- 压缩包

def _safe_name(name: str) -> bool:
    parts = name.replace("\\", "/").split("/")
    return bool(name) and not name.startswith(("/", "\\")) and ":" not in name \
        and not any(part == ".." for part in parts) and "\0" not in name


def _wanted(name: str, exe: str) -> bool:
    """压缩包里的程序：最上面一层的 openlist（可以带 ./）。"""
    clean = name.replace("\\", "/")
    while clean.startswith("./"):
        clean = clean[2:]
    return clean == exe


def extract_binary(archive: Path, target: Path, exe: str) -> None:
    """从核对过的压缩包（.tar.gz 或 .zip）里只取出程序本身，写到 target。压缩包里有不安全的名字（绝对路径、
    ..、盘符）、链接、设备文件，或者没有 / 不止一个程序，就 ComponentError，target 不留。"""
    try:
        if archive.name.endswith(".zip") or zipfile.is_zipfile(archive):
            _extract_zip(archive, target, exe)
        else:
            _extract_tar(archive, target, exe)
    except BaseException:
        _remove(target)
        raise


def _copy_member(source, target: Path, limit: int) -> None:
    written = 0
    with open(target, "wb") as sink:
        while chunk := source.read(CHUNK):
            written += len(chunk)
            if written > limit:
                raise ComponentError("网盘组件的压缩包内容不对（程序大得不正常），已停止")
            sink.write(chunk)


def _extract_tar(archive: Path, target: Path, exe: str) -> None:
    try:
        with tarfile.open(archive, "r:gz") as packed:
            members = []
            for member in packed:
                members.append(member)
                if len(members) > ARCHIVE_MEMBERS_MAX:
                    raise ComponentError("网盘组件的压缩包内容不对（文件太多），已停止")
            for member in members:
                if not _safe_name(member.name) or member.issym() or member.islnk() or member.isdev() \
                        or member.isfifo() or not (member.isfile() or member.isdir()):
                    raise ComponentError("网盘组件的压缩包里有不安全的文件名或链接，已停止")
            found = [m for m in members if m.isfile() and _wanted(m.name, exe)]
            if len(found) != 1 or found[0].size > BINARY_MAX:
                raise ComponentError("网盘组件的压缩包里找不到程序，已停止")
            source = packed.extractfile(found[0])
            if source is None:
                raise ComponentError("网盘组件的压缩包里找不到程序，已停止")
            with source:
                _copy_member(source, target, BINARY_MAX)
    except (tarfile.TarError, EOFError, OSError) as error:
        # gzip 坏了是 OSError 的一种（BadGzipFile）；写不进去（盘满了）也在这里，说出是哪一种。
        raise ComponentError(f"解不开网盘组件的压缩包（{type(error).__name__}），请重试") from None


def _extract_zip(archive: Path, target: Path, exe: str) -> None:
    try:
        with zipfile.ZipFile(archive) as packed:
            members = packed.infolist()
            if len(members) > ARCHIVE_MEMBERS_MAX:
                raise ComponentError("网盘组件的压缩包内容不对（文件太多），已停止")
            for member in members:
                mode = member.external_attr >> 16
                if not _safe_name(member.filename) or stat.S_ISLNK(mode):
                    raise ComponentError("网盘组件的压缩包里有不安全的文件名或链接，已停止")
            found = [m for m in members if not m.is_dir() and _wanted(m.filename, exe)]
            if len(found) != 1 or found[0].file_size > BINARY_MAX:
                raise ComponentError("网盘组件的压缩包里找不到程序，已停止")
            with packed.open(found[0]) as source:
                _copy_member(source, target, BINARY_MAX)
    except (zipfile.BadZipFile, EOFError, OSError) as error:
        raise ComponentError(f"解不开网盘组件的压缩包（{type(error).__name__}），请重试") from None


# ---------------------------------------------------------------- 密码、设置、config.json

def read_password(paths: Paths) -> str:
    try:
        text = paths.secret.read_text(encoding="ascii").strip()
    except (OSError, UnicodeDecodeError):
        return ""
    return text if re.fullmatch(r"[A-Za-z0-9]{16,64}", text) else ""


def new_password() -> str:
    """只用字母和数字（以「-」开头的会被 openlist 的命令行当成选项）。"""
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(PASSWORD_LENGTH))


def ensure_password(paths: Paths) -> tuple[str, bool]:
    """(密码, 是不是新生成的)。存成 600，从不打印。"""
    found = read_password(paths)
    if found:
        return found, False
    from .bootstrap import _write_secret
    paths.secret.parent.mkdir(parents=True, exist_ok=True)
    password = new_password()
    _write_secret(paths.secret, password + "\n")
    return password, True


def enabled(paths: Paths) -> bool:
    """装好以后默认开着；管理员点「停用」后 false。"""
    try:
        data = json.loads(paths.settings.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return True
    return not (isinstance(data, dict) and data.get("enabled") is False)


def set_enabled(paths: Paths, on: bool) -> None:
    _write_json(paths.settings, {"enabled": bool(on)})


def _write_json(path: Path, data: dict, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.unlink()
    except OSError:
        pass
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(data, ensure_ascii=False, indent=1))
    os.replace(tmp, path)


def write_config(data_dir: Path, port: int) -> dict:
    """改 openlist 的 config.json（没有就新写一份只有这些项的，它启动时补齐别的）：只听 127.0.0.1 的 port，
    不开 HTTPS、unix 套接字、S3、FTP、SFTP；force = false（true 时它不看环境变量）；日志文件留得少一点。"""
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(data_dir, 0o700)
    except OSError:
        pass
    path = data_dir / "config.json"
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        config = {}
    if not isinstance(config, dict):
        config = {}
    config["force"] = False
    scheme = config.get("scheme") if isinstance(config.get("scheme"), dict) else {}
    scheme.update(address="127.0.0.1", http_port=int(port), https_port=-1, force_https=False, unix_file="",
                  enable_h3=False)
    config["scheme"] = scheme
    for name in ("s3", "ftp", "sftp"):
        part = config.get(name) if isinstance(config.get(name), dict) else {}
        part["enable"] = False
        config[name] = part
    log = config.get("log") if isinstance(config.get("log"), dict) else {}
    log.update(max_size=10, max_backups=3)
    config["log"] = log
    _write_json(path, config)
    return config


def child_env(port: int, password: str = "", base: dict | None = None) -> dict:
    """openlist 的环境变量：和 config.json 一样只听 127.0.0.1（env 优先），密码只在数据库里还没有管理员时用。"""
    env = dict(os.environ if base is None else base)
    env.pop("TV_WATCH_PARENT", None)
    env.update(OPENLIST_ADDR="127.0.0.1", OPENLIST_HTTP_PORT=str(int(port)), OPENLIST_HTTPS_PORT="-1",
               OPENLIST_FORCE_HTTPS="false", OPENLIST_UNIX_FILE="", OPENLIST_S3_ENABLE="false",
               OPENLIST_FTP_ENABLE="false", OPENLIST_SFTP_ENABLE="false")
    if password:
        env["OPENLIST_ADMIN_PASSWORD"] = password
    else:
        env.pop("OPENLIST_ADMIN_PASSWORD", None)
    return env


def run_version(binary: Path) -> str:
    """openlist version 的「Version: v4.2.9」；跑不了 ""。"""
    try:
        done = subprocess.run([str(binary), "version"], capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=RUN_TIMEOUT_S, stdin=subprocess.DEVNULL, **_run_kwargs())
    except (OSError, subprocess.SubprocessError):
        return ""
    if done.returncode != 0:
        return ""
    match = re.search(r"(?m)^Version:\s*(v\d+\.\d+\.\d+)\s*$", done.stdout or "")
    return match.group(1) if match else ""


def set_admin(binary: Path, data_dir: Path, port: int, password: str) -> bool:
    """openlist admin set <密码> --data <目录>：设 admin 的密码（openlist 在跑时它也会让在跑的那个丢掉缓存）。
    输出里有密码：整个丢掉，只看退出码和「has been updated」。"""
    write_config(data_dir, port)
    try:
        done = subprocess.run([str(binary), "admin", "set", password, "--data", str(data_dir)],
                              capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=RUN_TIMEOUT_S,
                              stdin=subprocess.DEVNULL, cwd=str(data_dir), env=child_env(port, password),
                              **_run_kwargs())
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0 and "has been updated" in (done.stdout or "")


def read_status(paths: Paths) -> dict:
    try:
        data = json.loads(paths.status.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def ping(port: int, timeout: float = PING_TIMEOUT_S) -> bool:
    """openlist 的 /ping 回答 pong（不走代理）。"""
    if not isinstance(port, int) or not 0 < port < 65536:
        return False
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        connection.request("GET", "/ping", headers={"User-Agent": "RooTV"})
        answer = connection.getresponse()
        return answer.status == 200 and answer.read(64).strip() == b"pong"
    except (OSError, http.client.HTTPException):
        return False
    finally:
        connection.close()


# ---------------------------------------------------------------- 安装、更新

class OpenList:
    """一个数据目录的网盘组件。一次只做一件事（安装、更新）；进度在 snapshot()。
    fetch：ytcomponents.http_get（测试换掉）；version_run：装好后跑 openlist version；admin_run：设管理员密码；
    refresh：装好、更新、停用后请监督进程按新的设置更新子进程。"""

    def __init__(self, data_root: Path, log=print, fetch=http_get, version_run=run_version, admin_run=set_admin,
                 refresh=None, system: str | None = None, machine: str | None = None):
        self.paths = Paths(data_root)
        self.log, self.fetch, self.version_run, self.admin_run = log, fetch, version_run, admin_run
        self.refresh = refresh if refresh is not None else self._request_refresh
        self.system = system or platform.system()
        self.asset = platform_asset(self.system, machine)
        self._lock = threading.Lock()
        self.state, self.task, self.error, self.what = "idle", "", "", ""
        self.done = self.total = 0
        self.latest = ""
        self.checked_at = 0.0

    def _request_refresh(self) -> None:
        from .ytlocal import request_refresh
        request_refresh(self.paths.root / "run")

    # ---- 状态 ----

    def installed(self) -> dict | None:
        try:
            data = json.loads((self.paths.bin / COMPONENTS_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        entry = data.get("openlist") if isinstance(data, dict) else None
        if not isinstance(entry, dict) or not isinstance(entry.get("file"), str):
            return None
        name = os.path.basename(entry["file"])
        if not name or not (self.paths.bin / name).is_file():
            return None
        return {"version": str(entry.get("version") or ""), "file": name, "sha256": str(entry.get("sha256") or ""),
                "asset": str(entry.get("asset") or "")}

    def binary(self) -> Path | None:
        entry = self.installed()
        return self.paths.bin / entry["file"] if entry else None

    def ready(self) -> bool:
        """装好了、没停用、有密码：监督进程该起它。"""
        return self.binary() is not None and enabled(self.paths) and bool(read_password(self.paths))

    def snapshot(self) -> dict:
        entry = self.installed()
        with self._lock:
            current = entry["version"] if entry else ""
            size = ASSETS[self.asset][1] if self.asset in ASSETS else 0
            return {"state": self.state, "task": self.task, "what": self.what, "done": self.done,
                    "total": self.total, "error": self.error, "supported": self.asset is not None,
                    "installed": entry is not None, "version": current, "pinned": VERSION, "size": size,
                    "enabled": enabled(self.paths), "latest": self.latest, "checked_at": self.checked_at,
                    "update": bool(self.latest and current and version_key(self.latest) > version_key(current))}

    def _set(self, **changes) -> None:
        with self._lock:
            for name, value in changes.items():
                setattr(self, name, value)

    def _progress(self, done: int, total: int) -> None:
        with self._lock:
            self.done, self.total = done, total

    # ---- 开始一件事 ----

    def start(self, action: str, proxy: str = "", mirror: str = "", wait: bool = False) -> bool:
        """action：install（没装就装，装了就重新启用）/ update（更新到最新）。已经在做别的事时 False。"""
        if action not in ("install", "update"):
            raise ValueError(action)
        with self._lock:
            if self.state == "working":
                return False
            self.state, self.task, self.error, self.done, self.total, self.what = "working", action, "", 0, 0, ""
        thread = threading.Thread(target=self._work, args=(action, proxy, mirror), daemon=True,
                                  name=f"openlist-{action}")
        thread.start()
        if wait:
            thread.join()
        return True

    def _work(self, action: str, proxy: str, mirror: str) -> None:
        try:
            if self.asset is None:
                raise ComponentError(f"这台电脑（{self.system} / {platform.machine()}）没有 OpenList 的官方版本")
            self.paths.bin.mkdir(parents=True, exist_ok=True)
            self._clean_partial()
            if action == "install":
                self._install(proxy, mirror)
            else:
                self._update(proxy, mirror)
        except ComponentError as error:
            self.log(f"网盘组件：{error}")
            self._set(state="failed", error=str(error), what="")
            return
        except Exception as error:      # noqa: BLE001 —— 后台线程里的意外也要变成状态
            message = f"{type(error).__name__}: {error}"[:200]
            self.log(f"网盘组件出错：{message}")
            self._set(state="failed", error=message, what="")
            return
        self._set(state="done", what="")

    def _clean_partial(self) -> None:
        for path in self.paths.bin.glob("*.part"):
            _remove(path)
        entry = self.installed()
        if entry:
            self._prune(entry["file"])

    def _install(self, proxy: str, mirror: str) -> None:
        if self.installed() is None:
            sha256 = ASSETS[self.asset][0]
            self._get(VERSION, sha256, proxy, mirror)
        self._admin()
        set_enabled(self.paths, True)
        self.refresh()
        self.log(f"网盘组件已就绪：OpenList {self.installed()['version']}")

    def _admin(self) -> None:
        """有密码就用它，没有就生成；用 openlist 自己的命令行设好（同一个密码设多少遍都一样）。"""
        password, fresh = ensure_password(self.paths)
        if fresh:
            self.log("生成了网盘组件的管理员密码（secrets/openlist-admin）。")
        binary = self.binary()
        port = read_status(self.paths).get("port") or PORT
        self._set(what="设管理员密码")
        if binary is None or not self.admin_run(binary, self.paths.data, int(port), password):
            raise ComponentError("网盘组件装好了，但设不了管理员密码（openlist admin set 没成功）")

    def reset_password(self) -> bool:
        """把 admin 的密码重新设成 secrets 里的（在 OpenList 的网页里改过密码、片库连不上时）。"""
        binary = self.binary()
        password = read_password(self.paths)
        if binary is None or not password:
            return False
        port = read_status(self.paths).get("port") or PORT
        return bool(self.admin_run(binary, self.paths.data, int(port), password))

    def _get(self, version: str, sha256: str, proxy: str, mirror: str) -> None:
        """下载、核对、只取出程序、跑一次 openlist version，再原子地记进 components.json。"""
        asset = self.asset
        self._set(what=f"OpenList {version}", done=0, total=0)
        self.log(f"正在下载网盘组件 OpenList {version}（{asset}）…")
        archive = self.paths.bin / (asset + ".part")
        download(release_url(REPO, version, asset, mirror), archive, sha256, proxy, self._progress, self.fetch,
                 limit=DOWNLOAD_MAX)
        name = f"openlist-{version}" + (".exe" if self.system == "Windows" else "")
        part = self.paths.bin / (name + ".part")
        self._set(what=f"解开 OpenList {version}")
        try:
            extract_binary(archive, part, exe_name(self.system))
        finally:
            _remove(archive)
        os.chmod(part, os.stat(part).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        target = self.paths.bin / name
        os.replace(part, target)
        found = self.version_run(target)
        if found != version:
            _remove(target)
            raise ComponentError("下载的 OpenList 运行不了（被系统或安全软件拦住了，或这台电脑太旧）"
                                 if not found else f"下载的 OpenList 说自己是 {found}，不是 {version}，没有用它")
        self._record(version, name, sha256, asset)

    def _record(self, version: str, file: str, sha256: str, asset: str) -> None:
        """原子地改 components.json，再删掉别的版本。"""
        _write_json(self.paths.bin / COMPONENTS_FILE,
                    {"openlist": {"version": version, "file": file, "sha256": sha256, "asset": asset}}, 0o644)
        self._prune(file)

    def _prune(self, keep: str) -> None:
        for stale in self.paths.bin.glob("openlist-*"):
            if stale.name != keep and not stale.name.endswith((".part", ".json", ".tmp")):
                _remove(stale)

    # ---- 更新 ----

    def latest_tag(self, proxy: str = "", mirror: str = "") -> str:
        url = f"{(mirror or 'https://github.com').rstrip('/')}/{REPO}/releases/latest"
        try:
            answer = self.fetch(url, proxy, follow=False)
        except ComponentError:
            raise
        except (OSError, ValueError, http.client.HTTPException) as error:
            raise _network_error(error) from None
        try:
            location = answer.header("Location") or answer.url
        finally:
            answer.close()
        tag = urllib.parse.urlsplit(location).path.rstrip("/").rsplit("/tag/", 1)[-1]
        if not _TAG.match(tag):
            raise ComponentError("问不到 OpenList 的最新版本（GitHub 的回答看不懂）")
        return tag

    def check_update(self, proxy: str = "", mirror: str = "") -> dict:
        latest = self.latest_tag(proxy, mirror)
        with self._lock:
            self.latest, self.checked_at = latest, time.time()
        current = (self.installed() or {}).get("version", "")
        return {"current": current, "latest": latest,
                "update": bool(current) and version_key(latest) > version_key(current)}

    def release_digest(self, tag: str, proxy: str = "") -> str:
        """GitHub 接口里那个版本这台电脑的文件的 SHA-256（digest 「sha256:…」）。不经镜像。"""
        self._set(what=f"取 {tag} 的校验值")
        answer = None
        try:
            answer = self.fetch(f"{API}/repos/{REPO}/releases/tags/{tag}", proxy)
            if answer.status != 200:
                raise ComponentError(f"取不到 {tag} 的校验值（GitHub 回答 {answer.status}）")
            raw = answer.read(RELEASE_JSON_MAX + 1)
        except ComponentError:
            raise
        except (OSError, ValueError, http.client.HTTPException) as error:
            raise _network_error(error) from None
        finally:
            if answer is not None:
                answer.close()
        if len(raw) > RELEASE_JSON_MAX:
            raise ComponentError("GitHub 的回答大得不正常，已停止")
        try:
            release = json.loads(raw)
        except ValueError:
            raise ComponentError("GitHub 的回答看不懂") from None
        for item in release.get("assets") or [] if isinstance(release, dict) else []:
            if isinstance(item, dict) and item.get("name") == self.asset:
                digest = str(item.get("digest") or "")
                value = digest[len("sha256:"):].lower() if digest.startswith("sha256:") else ""
                if _SHA.match(value):
                    return value
                raise ComponentError(f"{tag} 没有公布这个文件的校验值，没有更新")
        raise ComponentError(f"{tag} 里没有这台电脑的文件（{self.asset}）")

    def _update(self, proxy: str, mirror: str) -> None:
        if self.installed() is None:
            self._install(proxy, mirror)
            return
        self._set(what="查最新版本")
        latest = self.latest_tag(proxy, mirror)
        with self._lock:
            self.latest, self.checked_at = latest, time.time()
        current = self.installed()["version"]
        if version_key(latest) <= version_key(current):
            self.log(f"网盘组件 OpenList {current} 已经是最新的")
            return
        sha256 = self.release_digest(latest, proxy)
        self._get(latest, sha256, proxy, mirror)
        self.refresh()
        self.log(f"网盘组件已更新：OpenList {current} → {latest}")

    # ---- 停用 ----

    def disable(self) -> None:
        set_enabled(self.paths, False)
        self.refresh()
        self.log("网盘组件停用了")

    def enable(self) -> bool:
        if self.binary() is None or not read_password(self.paths):
            return False
        set_enabled(self.paths, True)
        self.refresh()
        self.log("网盘组件启用了")
        return True


# ---------------------------------------------------------------- 子进程（监督进程起的就是这个）

def clean_line(line: str, password: str = "") -> str | None:
    """openlist 的一行输出 → 给日志的（去掉颜色）；带密码、令牌的整行不要（None）。"""
    text = _ANSI.sub("", line).rstrip()
    if not text.strip():
        return None
    if (password and password in text) or _SECRET_LINE.search(text):
        return None
    return text


def _write_status(path: Path, **fields) -> None:
    try:
        _write_json(path, dict(fields, at=int(time.time())), 0o644)
    except OSError:
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m server.selfhost.openlist", description="小鼠电视的网盘组件（OpenList）")
    parser.add_argument("--bin", required=True, type=Path)
    parser.add_argument("--data", required=True, type=Path, help="openlist 自己的数据目录")
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--password-file", required=True, type=Path)
    parser.add_argument("--status-file", type=Path)
    args = parser.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    try:
        password = args.password_file.read_text(encoding="ascii").strip()
    except (OSError, UnicodeDecodeError):
        password = ""
    if not password:
        print("没有网盘组件的管理员密码（secrets/openlist-admin）：在片库里重新点「安装网盘组件」", flush=True)
        time.sleep(30)
        return 2
    if not args.bin.is_file():
        print("找不到网盘组件的程序：在片库里重新点「安装网盘组件」", flush=True)
        time.sleep(30)
        return 2
    write_config(args.data, args.port)
    stop = threading.Event()
    for name in ("SIGTERM", "SIGINT", "SIGHUP", "SIGBREAK"):
        if hasattr(signal, name):
            try:
                signal.signal(getattr(signal, name), lambda *_: stop.set())
            except (OSError, ValueError):
                pass
    if os.environ.get("TV_WATCH_PARENT") and os.name == "posix" and sys.stdin is not None:
        # 监督进程（电脑客户端）给的管道：读到文件尾就是它不在了——先停 openlist 再走。
        try:
            descriptor = sys.stdin.fileno()
        except (OSError, ValueError):
            descriptor = None
        if descriptor is not None:
            def watch() -> None:
                try:
                    while os.read(descriptor, 1):
                        pass
                except OSError:
                    pass
                stop.set()
            threading.Thread(target=watch, daemon=True, name="parent-watch").start()
    process = subprocess.Popen([str(args.bin), "server", "--data", str(args.data)], cwd=str(args.data),
                               env=child_env(args.port, password), stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                               errors="replace", bufsize=1, **_run_kwargs())

    def pump() -> None:
        try:
            for line in process.stdout:
                shown = clean_line(line, password)
                if shown is not None:
                    print(shown, flush=True)
        except (OSError, ValueError):
            pass

    threading.Thread(target=pump, daemon=True, name="openlist-out").start()
    version = run_version(args.bin) if args.status_file else ""
    up = False
    while not stop.is_set() and process.poll() is None:
        if not up and ping(args.port, 1.0):
            up = True
            if args.status_file:
                _write_status(args.status_file, state="running", port=args.port, version=version, pid=process.pid)
            print(f"网盘组件在 127.0.0.1:{args.port} 上跑起来了", flush=True)
        stop.wait(0.5)
    code = process.poll()
    if code is None:
        process.terminate()
        try:
            code = process.wait(timeout=STOP_WAIT_S)
        except subprocess.TimeoutExpired:
            process.kill()
            code = process.wait()
        code = 0
    if args.status_file:
        _write_status(args.status_file, state="stopped", port=args.port, version=version)
    return int(code or 0)


if __name__ == "__main__":
    sys.exit(main())
