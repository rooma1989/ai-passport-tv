"""YouTube 本机模式要的两个程序：yt-dlp 和 deno。不随安装包发，第一次用（或点「安装 YouTube 组件」）时下载到
<数据目录>/youtube/bin/，以后「检查更新」只更新 yt-dlp（YouTube 改得勤，旧的 yt-dlp 几周就不灵了）。

从哪来：两个项目自己在 GitHub 上发布的文件（yt-dlp：纯 Python 的 zipapp，发布页上就叫「yt-dlp」，各个系统
同一个文件，Unlicense；deno：每个平台一个官方 zip，MIT）。yt-dlp 用这台机器自己的 Python 跑（打包的电脑客户端
是核心自带的那个，见 tools/ytdlp_run.py 和 ytnode.ytdlp_command）。以前下的是单文件版（yt-dlp_macos、
yt-dlp.exe、yt-dlp_linux），那种每次启动都先解压一整个 Python：在 Mac 上一次 --version 要 37 秒，搜索总超时。
装着旧版的，下次启动（或保存设置、点安装、检查更新）时换成 zipapp（版本不变低），换好后删掉旧文件。

zipapp 下好、核对过后存成 yt-dlp-<版本>.pyz，再在旁边解开成 yt-dlp-<版本>/ 并用同一个 Python 编译一遍：
从 zip 里导入每次都要重新编译，一次搜索多花一两秒；解开的文件夹有 .pyc。文件夹坏了、没有时照样直接跑 .pyz。可以经用户的代理下载（就是 [youtube] proxy），也可以给一个镜像前缀
（下载地址 = 前缀 + /yt-dlp/yt-dlp/releases/download/…，GitHub 加速站一般写成 https://加速站/https://github.com）。

怎么保证下到的是对的：
- 第一次安装：版本和每个文件的 SHA-256 都写死在下面（取自两个项目发布页公布的 SHA2-256SUMS 和 .sha256sum），
  对不上就删掉、什么都不运行。镜像、代理换掉文件也没用。
- 更新 yt-dlp：先问最新的版本号，再取那个版本发布的 SHA2-256SUMS，按里面的值核对。这时信任的是 GitHub 本身
  （经镜像时是镜像），没有核对 SHA2-256SUMS.sig 的 GPG 签名。

装好的记在 bin/components.json（ytnode.read_components 读它）：{"yt-dlp": {"version", "file", "sha256"}, "deno": …}。
文件名带版本，换版本时先写好新文件（和解开的文件夹）、再改 components.json、最后删旧文件，节点任何时候都只会
看到完整的一套。
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import platform
import re
import shutil
import ssl
import stat
import subprocess
import sys
import threading
import time
import urllib.parse
import zipfile
from pathlib import Path

from ..relay import ytnode

YTDLP_VERSION = "2026.10.21"
# 纯 Python 的 zipapp（https://github.com/yt-dlp/yt-dlp/releases/download/2026.10.21/SHA2-256SUMS 里的「yt-dlp」）。
YTDLP_ASSET = "yt-dlp"
YTDLP_SHA256 = "b7af77f686e95f7845ce8d09d21957718febb8b48b30555adb9dd2292ad7bf14"
DENO_VERSION = "2.9.8"
# 文件名 → SHA-256（v2.9.8 发布页每个 zip 旁边的 .zip.sha256sum）。
DENO_SHA256 = {
    "deno-aarch64-apple-darwin.zip": "9f0bf660393da2eaff044cdc7444cb08c73f235849b61d224d825a194ce7c98e",
    "deno-x86_64-apple-darwin.zip": "4b2f5edd2639b7470f034238b321d7002d8dfb4726d8558f52c17afac370eb2c",
    "deno-x86_64-pc-windows-msvc.zip": "cba84b26e0df5ae7f74fc66b86a95b866872a86292ebd84fa1e959927edd93bf",
    "deno-aarch64-pc-windows-msvc.zip": "b413b92b735b50c198e8fe3b85af1aa50e29d5f72e229aab701d132286e14cef",
    "deno-x86_64-unknown-linux-gnu.zip": "e890975bc2549807bbcc0b584ffafda72cc7c93349dd99c80483dd784f6c4609",
    "deno-aarch64-unknown-linux-gnu.zip": "d49596c20b9788cf8f5d8ddf644103d4bde56ed239753f0bdb92ba9361d51905",
}
# (platform.system(), platform.machine()) → (yt-dlp 的文件, deno 的 zip)。yt-dlp 各个系统同一个 zipapp。
# 没有的（32 位 Windows、armv7 的 Linux）deno 不发布，本机模式用不了，只能用「自己的节点」。
PLATFORMS = {
    ("Darwin", "arm64"): (YTDLP_ASSET, "deno-aarch64-apple-darwin.zip"),
    ("Darwin", "x86_64"): (YTDLP_ASSET, "deno-x86_64-apple-darwin.zip"),
    ("Windows", "AMD64"): (YTDLP_ASSET, "deno-x86_64-pc-windows-msvc.zip"),
    ("Windows", "ARM64"): (YTDLP_ASSET, "deno-aarch64-pc-windows-msvc.zip"),
    ("Linux", "x86_64"): (YTDLP_ASSET, "deno-x86_64-unknown-linux-gnu.zip"),
    ("Linux", "aarch64"): (YTDLP_ASSET, "deno-aarch64-unknown-linux-gnu.zip"),
}
GITHUB = "https://github.com"
YTDLP_REPO = "yt-dlp/yt-dlp"
DENO_REPO = "denoland/deno"
SUMS = "SHA2-256SUMS"
_TAG = re.compile(r"^\d{4}\.\d{2}\.\d{2}(?:\.\d+)?$")
_SHA = re.compile(r"^[0-9a-f]{64}$")
DOWNLOAD_MAX = 150 * 1024 * 1024
BINARY_MAX = 400 * 1024 * 1024
# 解开 zipapp 时的上限（2026.10.21：1053 个文件、10 MB）。
UNPACK_FILES_MAX = 20000
UNPACK_MAX = 200 * 1024 * 1024
COMPILE_TIMEOUT_S = 300
SUMS_MAX = 64 * 1024
TIMEOUT_S = 60
REDIRECTS = 6
CHUNK = 256 * 1024
VERSION_TIMEOUT_S = 90
USER_AGENT = "xiaozhi-tv-ytlocal"


class ComponentError(Exception):
    """下载、核对、安装出错：消息给人看（中文），不带代理密码。"""


def platform_assets(system: str | None = None, machine: str | None = None) -> tuple[str, str] | None:
    system = platform.system() if system is None else system
    machine = platform.machine() if machine is None else machine
    if system == "Linux" and machine == "arm64":
        machine = "aarch64"
    return PLATFORMS.get((system, machine))


def _exe(name: str, system: str | None = None) -> str:
    return name + (".exe" if (system or platform.system()) == "Windows" else "")


def valid_mirror(base: str) -> bool:
    """镜像前缀：https:// 开头（测试时也认本机的 http://），不带查询和片段。"""
    try:
        parts = urllib.parse.urlsplit(base)
    except ValueError:
        return False
    if not parts.hostname or parts.query or parts.fragment or any(c.isspace() for c in base):
        return False
    return parts.scheme == "https" or (parts.scheme == "http" and parts.hostname in ("127.0.0.1", "localhost"))


def release_url(repo: str, tag: str, asset: str, mirror: str = "") -> str:
    return f"{(mirror or GITHUB).rstrip('/')}/{repo}/releases/download/{tag}/{asset}"


def version_key(tag: str) -> tuple[int, ...]:
    return tuple(int(part) for part in tag.split(".")) if _TAG.match(tag or "") else ()


def parse_sums(text: str) -> dict[str, str]:
    """SHA2-256SUMS 的内容 → {文件名: 哈希}。"""
    found = {}
    for line in text.splitlines():
        parts = line.strip().split()
        if len(parts) == 2 and _SHA.match(parts[0].lower()):
            found[parts[1].lstrip("*")] = parts[0].lower()
    return found


# ---- HTTP（可以经代理） ----

class _Response:
    def __init__(self, connection, response, url):
        self.connection, self.response, self.url = connection, response, url
        self.status = response.status

    def header(self, name: str) -> str:
        return self.response.getheader(name) or ""

    def read(self, size: int = -1) -> bytes:
        return self.response.read(size)

    def close(self) -> None:
        self.connection.close()


def _connect(parts, proxy: ytnode.Proxy | None, timeout: float):
    host = parts.hostname
    port = parts.port or (443 if parts.scheme == "https" else 80)
    if proxy is None:
        if parts.scheme == "https":
            return http.client.HTTPSConnection(host, port, timeout=timeout, context=ssl.create_default_context())
        return http.client.HTTPConnection(host, port, timeout=timeout)
    sock = ytnode.open_tunnel(proxy, host, port, timeout=timeout)
    sock.settimeout(timeout)
    if parts.scheme == "https":
        sock = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
        connection = http.client.HTTPSConnection(host, port, timeout=timeout)
    else:
        connection = http.client.HTTPConnection(host, port, timeout=timeout)
    connection.sock = sock
    return connection


def http_get(url: str, proxy: str = "", timeout: float = TIMEOUT_S, follow: bool = True) -> _Response:
    """GET，跟着跳转走（GitHub 的下载会跳到别的主机）；follow=False 时停在第一个跳转上。代理：http 用
    CONNECT，socks5 自己握手（ytnode.open_tunnel），所以 socks5 也能下载。"""
    parsed_proxy = ytnode.parse_proxy(proxy) if proxy else None
    for _ in range(REDIRECTS):
        parts = urllib.parse.urlsplit(url)
        if parts.scheme not in ("https", "http") or not parts.hostname:
            raise ComponentError("下载地址不对")
        connection = _connect(parts, parsed_proxy, timeout)
        path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        try:
            connection.request("GET", path, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
            response = connection.getresponse()
        except BaseException:
            connection.close()
            raise
        answer = _Response(connection, response, url)
        if follow and response.status in (301, 302, 303, 307, 308) and answer.header("Location"):
            location = urllib.parse.urljoin(url, answer.header("Location"))
            answer.close()
            if not location.startswith(("https://", "http://127.0.0.1", "http://localhost")):
                raise ComponentError("下载被转到了不安全的地址，已停止")
            url = location
            continue
        return answer
    raise ComponentError("下载地址跳转太多次")


def _network_error(error: BaseException) -> ComponentError:
    text = ytnode.hide_password(f"{type(error).__name__}: {error}")[:160]
    return ComponentError(f"下载失败（{text}）。在中国大陆通常要先在上面填好代理，或者填一个镜像")


def download(url: str, target: Path, sha256: str, proxy: str = "", on_progress=None, fetch=http_get,
             limit: int = DOWNLOAD_MAX) -> None:
    """下载到 target（边下边算哈希），哈希对不上就删掉并报错。target 是临时文件名，调用方再改名。"""
    try:
        answer = fetch(url, proxy)
    except ComponentError:
        raise
    except (OSError, ValueError, http.client.HTTPException) as error:
        raise _network_error(error) from None
    digest = hashlib.sha256()
    received = 0
    try:
        if answer.status != 200:
            raise ComponentError(f"下载失败：服务器回答 {answer.status}（{urllib.parse.urlsplit(url).hostname}）")
        length = answer.header("Content-Length")
        total = int(length) if length.isdigit() else 0
        if total > limit:
            raise ComponentError("要下载的文件大得不正常，已停止")
        with open(target, "wb") as sink:
            while chunk := answer.read(CHUNK):
                received += len(chunk)
                if received > limit:
                    raise ComponentError("要下载的文件大得不正常，已停止")
                digest.update(chunk)
                sink.write(chunk)
                if on_progress:
                    on_progress(received, total)
    except (OSError, http.client.HTTPException) as error:
        _remove(target)
        raise _network_error(error) from None
    except BaseException:
        _remove(target)
        raise
    finally:
        answer.close()
    if digest.hexdigest() != sha256:
        _remove(target)
        raise ComponentError("下载的文件校验值不符，已删掉，没有运行。可能是下载不完整或被替换了，请重试；"
                             "反复出现请换一个镜像或报告这个问题")


def _remove(path: Path) -> None:
    """删掉文件或文件夹（解开的 yt-dlp）；删不掉（Windows 上正在运行）就留着，下次再删。"""
    try:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()
    except OSError:
        pass


def _make_executable(path: Path) -> None:
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _run_version(path: Path) -> str:
    """装好后跑一次 --version（yt-dlp 的 .pyz 经 ytnode.ytdlp_command 用本机的 Python 跑）。"""
    kwargs = {"creationflags": 0x08000000} if os.name == "nt" else {}
    try:
        done = subprocess.run(ytnode.ytdlp_command(str(path)) + ["--version"], capture_output=True, text=True,
                              errors="replace", timeout=VERSION_TIMEOUT_S, stdin=subprocess.DEVNULL, **kwargs)
    except (OSError, subprocess.SubprocessError):
        return ""
    return (done.stdout or "").strip().splitlines()[0][:80] if done.returncode == 0 and done.stdout else ""


def _safe_member(name: str) -> bool:
    parts = name.replace("\\", "/").split("/")
    return bool(name) and not name.startswith(("/", "\\")) and ":" not in name \
        and not any(part in ("..", ".") for part in parts)


def unpack_zipapp(pyz: Path, folder: Path) -> None:
    """把核对过的 zipapp 解到 folder（先解到 folder.part 再改名）。内容不对时 ComponentError，什么都不留。"""
    part = folder.with_name(folder.name + ".part")
    _remove(part)
    try:
        with zipfile.ZipFile(pyz) as zipped:
            members = zipped.infolist()
            if len(members) > UNPACK_FILES_MAX or sum(m.file_size for m in members) > UNPACK_MAX:
                raise ComponentError("yt-dlp 的压缩包大得不正常，已停止")
            if not any(m.filename == "yt_dlp/__init__.py" for m in members):
                raise ComponentError("yt-dlp 的压缩包里没有 yt_dlp")
            for member in members:
                if not _safe_member(member.filename):
                    raise ComponentError("yt-dlp 的压缩包里有不安全的文件名，已停止")
            zipped.extractall(part)
    except zipfile.BadZipFile:
        _remove(part)
        raise ComponentError("yt-dlp 的压缩包坏了，请重试") from None
    except BaseException:
        _remove(part)
        raise
    _remove(folder)
    os.replace(part, folder)


def compile_command(folder: Path, frozen: bool | None = None, executable: str | None = None) -> list[str]:
    """用跑 yt-dlp 的同一个 Python 编译解开的文件夹（打包版：rootv-core __ytdlp --compile）。"""
    frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
    executable = executable or sys.executable
    if frozen:
        return [executable, ytnode.YTDLP_SUBCOMMAND, "--compile", str(folder)]
    return [executable, "-I", "-m", "compileall", "-q", str(folder)]


def _compile(folder: Path) -> bool:
    """另起一个进程编译（几秒的 CPU，不占中转的 GIL）。编译不了不要紧：照样能跑，只是慢一些。"""
    kwargs = {"creationflags": 0x08000000} if os.name == "nt" else {}
    try:
        done = subprocess.run(compile_command(folder), capture_output=True, timeout=COMPILE_TIMEOUT_S,
                              stdin=subprocess.DEVNULL, **kwargs)
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


class Components:
    """bin_dir 里的 yt-dlp 和 deno。一次只做一件事（安装、更新）；进度在 snapshot()。
    fetch：http_get（测试换掉它）；check_run：装好后跑一次 --version（测试换掉）；status_file：把 snapshot
    写给监督进程（本机控制接口的状态）。"""

    def __init__(self, bin_dir: Path, log=print, fetch=http_get, check_run=_run_version,
                 status_file: Path | None = None, system: str | None = None, machine: str | None = None,
                 compile_run=_compile):
        self.bin_dir = Path(bin_dir)
        self.log, self.fetch, self.check_run, self.compile_run = log, fetch, check_run, compile_run
        self.status_file = status_file
        self.system = system or platform.system()
        self.assets = platform_assets(self.system, machine)
        self._lock = threading.Lock()
        self.state, self.task, self.error, self.what = "idle", "", "", ""
        self.done = self.total = 0
        self.latest = ""
        self.checked_at = 0.0
        self._last_write = 0.0

    # ---- 状态 ----

    def installed(self) -> dict:
        try:
            data = json.loads((self.bin_dir / ytnode.COMPONENTS_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        found = {}
        for name in ("yt-dlp", "deno"):
            entry = data.get(name)
            if isinstance(entry, dict) and isinstance(entry.get("file"), str) \
                    and (self.bin_dir / os.path.basename(entry["file"])).is_file():
                found[name] = {"version": str(entry.get("version") or ""), "file": entry["file"],
                               "sha256": str(entry.get("sha256") or "")}
        return found

    def ready(self) -> bool:
        return ytnode.read_components(str(self.bin_dir)) is not None

    def legacy(self) -> bool:
        """装着的 yt-dlp 是以前的单文件版（每次启动很慢）：要换成 zipapp。"""
        entry = self.installed().get("yt-dlp")
        return entry is not None and not entry["file"].endswith(ytnode.ZIPAPP_SUFFIX)

    def snapshot(self) -> dict:
        installed = self.installed()
        with self._lock:
            current = installed.get("yt-dlp", {}).get("version", "")
            return {"state": self.state, "task": self.task, "what": self.what, "done": self.done,
                    "total": self.total, "error": self.error, "supported": self.assets is not None,
                    "ready": self.ready(), "legacy": self.legacy(),
                    "ytdlp": current, "deno": installed.get("deno", {}).get("version", ""),
                    "pinned": {"ytdlp": YTDLP_VERSION, "deno": DENO_VERSION},
                    "latest": self.latest, "checked_at": self.checked_at,
                    "update": bool(self.latest and current and version_key(self.latest) > version_key(current))}

    def _set(self, **changes) -> None:
        with self._lock:
            for name, value in changes.items():
                setattr(self, name, value)
        self._publish(force="state" in changes)

    def _publish(self, force: bool = False) -> None:
        if self.status_file is None:
            return
        now = time.monotonic()
        if not force and now - self._last_write < 1.0:
            return
        self._last_write = now
        try:
            tmp = self.status_file.with_name(self.status_file.name + ".tmp")
            tmp.write_text(json.dumps(self.snapshot(), ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.status_file)
        except OSError:
            pass

    # ---- 开始一件事 ----

    def start(self, action: str, proxy: str = "", mirror: str = "", wait: bool = False) -> bool:
        """action：install（装缺的）/ update（yt-dlp 更新到最新）。已经在做别的事时 False。"""
        if action not in ("install", "update"):
            raise ValueError(action)
        with self._lock:
            if self.state == "working":
                return False
            self.state, self.task, self.error, self.done, self.total, self.what = "working", action, "", 0, 0, ""
        self._publish(force=True)
        thread = threading.Thread(target=self._work, args=(action, proxy, mirror), daemon=True,
                                  name=f"yt-{action}")
        thread.start()
        if wait:
            thread.join()
        return True

    def _work(self, action: str, proxy: str, mirror: str) -> None:
        try:
            if self.assets is None:
                raise ComponentError(f"这台电脑（{self.system} / {platform.machine()}）没有官方的 deno，"
                                     "本机模式用不了；可以用「自己的节点」")
            self.bin_dir.mkdir(parents=True, exist_ok=True)
            self._clean_partial()
            if action == "install":
                self._install(proxy, mirror)
            else:
                self._update(proxy, mirror)
        except ComponentError as error:
            self.log(f"YouTube 组件：{error}")
            self._set(state="failed", error=str(error), what="")
            return
        except Exception as error:      # noqa: BLE001 —— 后台线程里的意外也要变成状态
            message = ytnode.hide_password(f"{type(error).__name__}: {error}")[:200]
            self.log(f"YouTube 组件出错：{message}")
            self._set(state="failed", error=message, what="")
            return
        self._set(state="done", what="")

    def _progress(self, done: int, total: int) -> None:
        with self._lock:
            self.done, self.total = done, total
        self._publish()

    def _clean_partial(self) -> None:
        for path in self.bin_dir.glob("*.part"):
            _remove(path)
        # 上次没删掉的旧版本（Windows 上删正在运行的程序会失败）。
        for component, entry in self.installed().items():
            self._prune(component, entry["file"])

    # ---- 安装 ----

    def _install(self, proxy: str, mirror: str) -> None:
        installed = self.installed()
        deno_asset = self.assets[1]
        entry = installed.get("yt-dlp")
        if entry is None or not entry["file"].endswith(ytnode.ZIPAPP_SUFFIX):
            version, sha256 = YTDLP_VERSION, YTDLP_SHA256
            if entry is not None:
                # 以前的单文件版换成 zipapp：版本不降（「检查更新」装过更新的就用那个版本的 zipapp）。
                if version_key(entry["version"]) > version_key(YTDLP_VERSION):
                    version = entry["version"]
                    sha256 = self._release_sha(version, proxy, mirror)
                self.log(f"yt-dlp 换成纯 Python 版（{entry['version'] or '?'} → {version}）：单文件版每次启动太慢")
            self._get_ytdlp(version, sha256, proxy, mirror)
        if "deno" not in installed:
            self._get_deno(deno_asset, proxy, mirror)
        if not self.ready():
            raise ComponentError("装完了却找不到 yt-dlp 或 deno（数据文件夹能写吗？）")
        self.log(f"YouTube 组件已就绪：yt-dlp {self.installed()['yt-dlp']['version']}，"
                 f"deno {self.installed()['deno']['version']}")

    def _get_ytdlp(self, version: str, sha256: str, proxy: str, mirror: str) -> None:
        self._set(what=f"yt-dlp {version}", done=0, total=0)
        self.log(f"正在下载 yt-dlp {version}（{YTDLP_ASSET}，纯 Python 版）…")
        name = f"yt-dlp-{version}{ytnode.ZIPAPP_SUFFIX}"
        part = self.bin_dir / (name + ".part")
        download(release_url(YTDLP_REPO, version, YTDLP_ASSET, mirror), part, sha256, proxy, self._progress,
                 self.fetch)
        target = self.bin_dir / name
        os.replace(part, target)
        folder = self.bin_dir / name[:-len(ytnode.ZIPAPP_SUFFIX)]
        self._set(what=f"准备 yt-dlp {version}")
        try:
            unpack_zipapp(target, folder)
            if not self.compile_run(folder):
                self.log("yt-dlp 编译没有完成（不影响使用，第一次会慢一点）")
        except ComponentError as error:
            # 解不开就直接跑 .pyz：慢一两秒，但能用。
            self.log(f"yt-dlp 没能解开（{error}），直接用压缩包")
            _remove(folder)
        if not self.check_run(target):
            _remove(target)
            _remove(folder)
            raise ComponentError("下载的 yt-dlp 运行不了（这台电脑的 Python 太旧，或被安全软件拦住了）")
        self._record("yt-dlp", version, name, sha256)

    def _get_deno(self, asset: str, proxy: str, mirror: str) -> None:
        version = DENO_VERSION
        self._set(what=f"deno {version}", done=0, total=0)
        self.log(f"正在下载 deno {version}（{asset}）…")
        archive = self.bin_dir / (asset + ".part")
        download(release_url(DENO_REPO, f"v{version}", asset, mirror), archive, DENO_SHA256[asset], proxy,
                 self._progress, self.fetch)
        name = _exe(f"deno-{version}", self.system)
        part = self.bin_dir / (name + ".part")
        try:
            with zipfile.ZipFile(archive) as zipped:
                members = [m for m in zipped.infolist() if m.filename in ("deno", "deno.exe")]
                if len(members) != 1 or members[0].file_size > BINARY_MAX:
                    raise ComponentError("deno 的压缩包内容不对，已停止")
                with zipped.open(members[0]) as source, open(part, "wb") as sink:
                    while chunk := source.read(CHUNK):
                        sink.write(chunk)
        except zipfile.BadZipFile:
            _remove(part)
            raise ComponentError("deno 的压缩包坏了，请重试") from None
        except BaseException:
            _remove(part)
            raise
        finally:
            _remove(archive)
        _make_executable(part)
        os.replace(part, self.bin_dir / name)
        if not self.check_run(self.bin_dir / name):
            _remove(self.bin_dir / name)
            raise ComponentError("下载的 deno 运行不了（系统拦住了，或这台电脑太旧）")
        self._record("deno", version, name, DENO_SHA256[asset])

    def _record(self, component: str, version: str, file: str, sha256: str) -> None:
        """改 components.json（原子地），再删掉这个组件的旧文件。"""
        path = self.bin_dir / ytnode.COMPONENTS_FILE
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        data[component] = {"version": version, "file": file, "sha256": sha256}
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        os.replace(tmp, path)
        self._prune(component, file)

    def _prune(self, component: str, file: str) -> None:
        """删掉这个组件别的版本（文件和解开的文件夹），留下 file 和它解开的文件夹。"""
        keep = {file}
        if file.endswith(ytnode.ZIPAPP_SUFFIX):
            keep.add(file[:-len(ytnode.ZIPAPP_SUFFIX)])
        prefix = "yt-dlp-" if component == "yt-dlp" else "deno-"
        for stale in self.bin_dir.glob(prefix + "*"):
            if stale.name not in keep and not stale.name.endswith((".part", ".json", ".tmp")):
                _remove(stale)

    # ---- 更新 yt-dlp ----

    def latest_tag(self, proxy: str = "", mirror: str = "") -> str:
        """GitHub 上 yt-dlp 最新的版本号（releases/latest 跳转到的 /tag/<版本>）。"""
        url = f"{(mirror or GITHUB).rstrip('/')}/{YTDLP_REPO}/releases/latest"
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
            raise ComponentError("问不到 yt-dlp 的最新版本（GitHub 的回答看不懂）")
        return tag

    def check_update(self, proxy: str = "", mirror: str = "") -> dict:
        """只问版本号，不下载：{"current", "latest", "update"}。"""
        latest = self.latest_tag(proxy, mirror)
        with self._lock:
            self.latest, self.checked_at = latest, time.time()
        current = self.installed().get("yt-dlp", {}).get("version", "")
        return {"current": current, "latest": latest,
                "update": bool(current) and version_key(latest) > version_key(current)}

    def _release_sha(self, tag: str, proxy: str, mirror: str) -> str:
        """那个版本发布的 SHA2-256SUMS 里 zipapp 的哈希（信任 GitHub，经镜像时是镜像）。"""
        self._set(what=f"{SUMS}（{tag}）")
        answer = None
        try:
            answer = self.fetch(release_url(YTDLP_REPO, tag, SUMS, mirror), proxy)
            if answer.status != 200:
                raise ComponentError(f"取不到 {tag} 的 {SUMS}（服务器回答 {answer.status}）")
            text = answer.read(SUMS_MAX + 1)
        except ComponentError:
            raise
        except (OSError, ValueError, http.client.HTTPException) as error:
            raise _network_error(error) from None
        finally:
            if answer is not None:
                answer.close()
        if len(text) > SUMS_MAX:
            raise ComponentError(f"{SUMS} 大得不正常，已停止")
        sha256 = parse_sums(text.decode("utf-8", "replace")).get(YTDLP_ASSET)
        if sha256 is None:
            raise ComponentError(f"{tag} 的 {SUMS} 里没有 {YTDLP_ASSET}")
        return sha256

    def _update(self, proxy: str, mirror: str) -> None:
        if not self.ready() or self.legacy():
            self._install(proxy, mirror)
        self._set(what="查最新版本")
        latest = self.latest_tag(proxy, mirror)
        with self._lock:
            self.latest, self.checked_at = latest, time.time()
        current = self.installed().get("yt-dlp", {}).get("version", "")
        if current and version_key(latest) <= version_key(current):
            self.log(f"yt-dlp {current} 已经是最新的")
            return
        sha256 = self._release_sha(latest, proxy, mirror)
        self._get_ytdlp(latest, sha256, proxy, mirror)
        self.log(f"yt-dlp 已更新：{current or '无'} → {latest}")
