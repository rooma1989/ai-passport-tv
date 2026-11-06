"""电脑客户端的更新：核心查新版本、在后台下好、校验（托盘负责装，见 desktop/update.py）。

设计：docs/superpowers/specs/2026-11-05-pc-client-updates-design.md。

- 版本清单 rootv-update.json 和旁边的 rootv-update.json.sig（主人的更新签名密钥签的 ed25519，公钥是 PUBLIC_KEY）。
  先试七牛的镜像（MIRROR，打包时填；空着就不试），再试 GitHub Releases。两个都只当搬运，只认签名对得上、
  版本比正在跑的新、日期不早于上次认过的清单。
- 只下本平台（platform_key）的那个文件，到 <数据目录>/updates/<版本>/，边下边算 SHA-256，断了接着下（.part），
  不对就删掉重下（RETRIES 次）；下好写 ready.json（托盘照着装）。
- 状态写进 run/update.json（中转的 /api/update 读它给网页；控制接口的 status 里也有）。网页的「立即检查」「更新并
  重启」放请求文件 run/update-check、run/update-install（和「设置 → YouTube」的 refresh-request 一样），这里看到就做：
  检查马上查；更新只是标上 install（托盘每几秒问一次状态，看到了就去装）。
- 只在打包好的客户端里查（从源代码跑的开发环境、Docker 自建不查，免得把开发中的版本换掉）；环境变量
  ROOTV_UPDATE_FORCE=1 时也查（测试），ROOTV_UPDATE_SOURCES 换下载源（逗号分隔的地址，和镜像一样的目录结构）。

取 GitHub 走「设置 → YouTube」的 http 代理（有的话），镜像直连。只用标准库。
"""

from __future__ import annotations

import hashlib
import json
import os
import platform as platform_module
import re
import shutil
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

# 主人的更新签名公钥（tools/publish_update.py keygen 生成；私钥在主人电脑的 ~/.config/xiaozhi-tv/update-signing.key）。
PUBLIC_KEY = "088b6367e140c7b180f343cc32490c16be09a4af5b259b847572534e6e3793a6"
# 七牛镜像的根地址：清单在 <根>/latest/，安装包在 <根>/<版本>/。空着就只用 GitHub。CDN 还没配 https 证书，先用 http：
# 清单有签名、安装包有 SHA-256，被中途改了也只是校验不过、换 GitHub 下。
MIRROR = "http://cdn.tv.iewww.com/rootv"
GITHUB = "https://github.com/rooma1989/ai-passport-tv/releases"
MANIFEST = "rootv-update.json"
SIGNATURE = MANIFEST + ".sig"
FIRST_DELAY_S = 600
EVERY_S = 24 * 3600
# 失败了（网络不通）隔多久再试。
RETRY_S = 3600
RETRIES = 3
MANIFEST_MAX = 256 * 1024
FILE_MAX = 2 * 1024 * 1024 * 1024
# 下载时至少给磁盘留这么多空间。
SPARE_BYTES = 200 * 1024 * 1024
TIMEOUT_S = 30
CHUNK = 256 * 1024
STATUS_FILE = "update.json"
CHECK_REQUEST = "update-check"
INSTALL_REQUEST = "update-install"
FORCE_ENV = "ROOTV_UPDATE_FORCE"
SOURCES_ENV = "ROOTV_UPDATE_SOURCES"
PLATFORMS = ("macos-arm64", "macos-x86_64", "windows-x64", "windows-x64-portable", "linux-x86_64")
_VERSION = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_NAME = re.compile(r"^[A-Za-z0-9._-]{1,120}$")
_SHA = re.compile(r"^[0-9a-f]{64}$")


class UpdateError(Exception):
    """查或下失败；消息是给人看的。"""


# ---- 版本 ----

def parse_version(text: str):
    """"0.3.9-beta" → 比大小用的键；看不懂时 None。正式版比同号的预发布版新（0.4.0 > 0.4.0-beta），
    预发布按点分的段比（数字段按数字，0.4.0-beta.2 > 0.4.0-beta），".dev"（从源代码跑）比同号的预发布旧。"""
    found = _VERSION.match((text or "").strip())
    if not found:
        return None
    major, minor, patch, pre = found.groups()
    pieces = pre.split(".") if pre else []
    dev = bool(pieces) and pieces[-1] == "dev"
    if dev:
        pieces = pieces[:-1]
    parts = tuple((0, int(part), "") if part.isdigit() else (1, 0, part) for part in pieces)
    # (主, 次, 修订, 正式版为 1, 预发布的各段, 不是 .dev 为 1)
    return (int(major), int(minor), int(patch), 0 if parts else 1, parts, 0 if dev else 1)


def newer(candidate: str, current: str) -> bool:
    a, b = parse_version(candidate), parse_version(current)
    return a is not None and (b is None or a > b)


def platform_key(system: str | None = None, machine: str | None = None, portable: bool = False,
                 lite: bool = False) -> str:
    """这个客户端认清单里的哪一项（macos-arm64、windows-x64-portable、linux-x86_64…，精简版加 -lite）；
    不支持的系统空串。"""
    system = (system or platform_module.system()).lower()
    machine = (machine or platform_module.machine()).lower()
    if system == "darwin":
        key = "macos-arm64" if machine in ("arm64", "aarch64") else "macos-x86_64"
    elif system == "windows":
        key = "windows-x64-portable" if portable else "windows-x64"
    elif system == "linux" and machine in ("x86_64", "amd64"):
        key = "linux-x86_64"
    else:
        return ""
    return key + ("-lite" if lite else "")


# ---- 清单 ----

def verify_manifest(data: bytes, signature: bytes, public_hex: str) -> dict:
    """签名对得上、格式对的清单（dict）；不然 UpdateError。signature 是十六进制文字（.sig 文件的内容）。"""
    from .. import ed25519
    if not public_hex:
        raise UpdateError("这一版没有内置更新签名公钥")
    try:
        public = bytes.fromhex(public_hex)
        sig = bytes.fromhex(signature.decode("ascii").strip())
    except (ValueError, UnicodeDecodeError):
        raise UpdateError("签名文件格式不对") from None
    if len(sig) != 64 or not ed25519.verify(public, data, sig):
        raise UpdateError("版本清单的签名对不上（可能被改过），不用它")
    try:
        manifest = json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise UpdateError("版本清单不是 JSON") from None
    check_manifest(manifest)
    return manifest


def check_manifest(manifest) -> None:
    if not isinstance(manifest, dict) or parse_version(str(manifest.get("version") or "")) is None \
            or not _DATE.match(str(manifest.get("date") or "")) or not isinstance(manifest.get("files"), dict) \
            or not isinstance(manifest.get("notes", ""), str):
        raise UpdateError("版本清单格式不对")
    for key, entry in manifest["files"].items():
        if not isinstance(entry, dict) or not _NAME.match(str(entry.get("name") or "")) \
                or not _SHA.match(str(entry.get("sha256") or "")) \
                or not isinstance(entry.get("size"), int) or not 0 < entry["size"] <= FILE_MAX:
            raise UpdateError(f"版本清单里 {key} 那一项格式不对")


def pick_file(manifest: dict, key: str) -> dict | None:
    entry = manifest.get("files", {}).get(key) if key else None
    return dict(entry) if isinstance(entry, dict) else None


# ---- 下载源 ----

class Source:
    """一个下载源：清单在哪、某个版本的某个文件在哪。kind 是 mirror（七牛和测试用的源）或 github。"""

    def __init__(self, kind: str, base: str):
        self.kind, self.base = kind, base.rstrip("/")

    def manifest_url(self, name: str = MANIFEST) -> str:
        if self.kind == "github":
            return f"{self.base}/latest/download/{name}"
        return f"{self.base}/latest/{name}"

    def file_url(self, version: str, name: str) -> str:
        if self.kind == "github":
            return f"{self.base}/download/v{version}/{name}"
        return f"{self.base}/{version}/{name}"

    @property
    def label(self) -> str:
        return "GitHub" if self.kind == "github" else "镜像"


def default_sources(environ=os.environ) -> list[Source]:
    given = [s.strip() for s in environ.get(SOURCES_ENV, "").split(",") if s.strip()]
    if given:
        return [Source("mirror", s) for s in given]
    found = [Source("mirror", MIRROR)] if MIRROR else []
    return found + [Source("github", GITHUB)]


def opener_for(source: Source, proxy: str = ""):
    """镜像直连（不走环境变量、系统的代理）；GitHub 走给的 http 代理，没有时照 urllib 的默认（环境变量、系统代理）。"""
    if source.kind != "github":
        return urllib.request.build_opener(urllib.request.ProxyHandler({})).open
    if proxy.startswith(("http://", "https://")):
        return urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy, "https": proxy})).open
    return urllib.request.build_opener().open


def _request(url: str, start: int = 0) -> urllib.request.Request:
    headers = {"User-Agent": "RooTV-updater", "Accept-Encoding": "identity"}
    if start:
        headers["Range"] = f"bytes={start}-"
    return urllib.request.Request(url, headers=headers)


def _read_small(open_, url: str, limit: int) -> bytes:
    with open_(_request(url), timeout=TIMEOUT_S) as answer:
        data = answer.read(limit + 1)
    if len(data) > limit:
        raise UpdateError("清单太大了")
    return data


# ---- 更新器 ----

def windows_portable(executable: str | None = None) -> bool:
    """Windows 的免安装版：程序旁边（往上两层以内）没有 Inno Setup 的卸载程序 unins*.exe。别的系统 False。"""
    import sys
    if platform_module.system() != "Windows":
        return False
    here = Path(executable or sys.executable).resolve()
    for folder in list(here.parents)[:3]:
        if any(folder.glob("unins*.exe")):
            return False
    return True


def enabled(frozen: bool, environ=os.environ) -> bool:
    return frozen or environ.get(FORCE_ENV) == "1"


class Updater:
    """root：数据目录；run_dir：run/。opener(source) 给取东西用的 open 函数（测试换掉）。"""

    def __init__(self, root: Path, run_dir: Path, version: str, log=print, key: str | None = None,
                 sources: list[Source] | None = None, platform: str | None = None, proxy=lambda: "",
                 opener=None, clock=time.time, free_bytes=None):
        self.root, self.run_dir, self.version, self.log = Path(root), Path(run_dir), version, log
        self.dir = self.root / "updates"
        self.key = PUBLIC_KEY if key is None else key
        self.sources = default_sources() if sources is None else sources
        self.platform = platform_key() if platform is None else platform
        self.proxy = proxy
        self.opener = opener or (lambda source: opener_for(source, self._proxy()))
        self.clock = clock
        self.free_bytes = free_bytes or (lambda path: shutil.disk_usage(path).free)
        self._lock = threading.Lock()
        self._busy = threading.Lock()
        self.state = {"state": "latest", "version": "", "notes": "", "date": "", "done": 0, "total": 0,
                      "error": "", "checked": 0.0, "install": False, "current": version, "result": ""}

    def _proxy(self) -> str:
        try:
            return self.proxy() or ""
        except Exception:  # noqa: BLE001 —— 读不了设置就不用代理
            return ""

    # ---- 状态 ----

    def snapshot(self) -> dict:
        with self._lock:
            found = dict(self.state)
        found["available"] = found["state"] == "ready"
        return found

    def _set(self, **changes) -> None:
        with self._lock:
            self.state.update(changes)
            data = dict(self.state)
        data["available"] = data["state"] == "ready"
        try:
            self.run_dir.mkdir(parents=True, exist_ok=True)
            path = self.run_dir / STATUS_FILE
            tmp = path.with_name(STATUS_FILE + ".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, path)
        except OSError:
            pass

    # ---- 启动时 ----

    def start(self) -> None:
        """新版本第一次启动：记一行「从 x 更新到 y」，清掉旧的下载；上次的更新没成功（替换脚本写的 result.json）
        就说出来；已经下好、还比自己新的那一份接着用。"""
        last = self.dir / "last-version"
        try:
            before = last.read_text(encoding="utf-8").strip()
        except OSError:
            before = ""
        if before and before != self.version:
            self.log(f"小鼠电视从 {before} 更新到了 {self.version}")
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            last.write_text(self.version + "\n", encoding="utf-8")
        except OSError:
            pass
        result = self._read_json(self.dir / "result.json")
        if result is not None:
            try:
                (self.dir / "result.json").unlink()
            except OSError:
                pass
            if not result.get("ok") and result.get("to"):
                message = f"更新到 {result['to']} 没成功，还在用 {self.version}" + (
                    f"（{result['error']}）" if result.get("error") else "")
                self.log(message)
                self._set(result=message)
        ready = self.ready()
        for child in (self.dir.iterdir() if self.dir.is_dir() else ()):
            if child.is_dir() and (ready is None or child.name != ready["version"]):
                shutil.rmtree(child, ignore_errors=True)
        if ready is not None:
            self._set(state="ready", version=ready["version"], notes=ready.get("notes", ""),
                      date=ready.get("date", ""), done=ready["size"], total=ready["size"])
        else:
            self._set()

    @staticmethod
    def _read_json(path: Path) -> dict | None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def ready(self) -> dict | None:
        """已经下好、校验过、比正在跑的新的那一份（ready.json 的内容），没有时 None。"""
        if not self.dir.is_dir():
            return None
        best = None
        for child in self.dir.iterdir():
            info = self._read_json(child / "ready.json") if child.is_dir() else None
            if info is None or not newer(str(info.get("version") or ""), self.version):
                continue
            if not (child / str(info.get("name") or "")).is_file():
                continue
            if best is None or newer(info["version"], best["version"]):
                best = dict(info, path=str(child / info["name"]))
        return best

    # ---- 查 ----

    def _accepted_date(self) -> str:
        info = self._read_json(self.dir / "accepted.json") or {}
        return str(info.get("date") or "")

    def fetch_manifest(self) -> tuple[dict, Source]:
        """按顺序试下载源，给第一份签名对、版本和日期都说得过去的清单。都不行时 UpdateError（最后一个原因）。"""
        last = UpdateError("没有下载源")
        for source in self.sources:
            open_ = self.opener(source)
            try:
                data = _read_small(open_, source.manifest_url(), MANIFEST_MAX)
                sig = _read_small(open_, source.manifest_url(SIGNATURE), 1024)
                manifest = verify_manifest(data, sig, self.key)
            except UpdateError as error:
                self.log(f"更新：{source.label}的版本清单不能用：{error}")
                last = error
                continue
            except (OSError, ValueError, urllib.error.URLError) as error:
                self.log(f"更新：连不上{source.label}（{type(error).__name__}）")
                last = UpdateError(f"连不上{source.label}")
                continue
            if manifest["date"] < self._accepted_date():
                self.log(f"更新：{source.label}给的是比上次还旧的清单（{manifest['date']}），不用它")
                last = UpdateError("版本清单比上次的旧")
                continue
            return manifest, source
        raise last

    def check(self) -> dict:
        """查一次，有新版本就下好。返回状态。一次只做一个（另一个正在做时直接给现在的状态）。"""
        if not self._busy.acquire(blocking=False):
            return self.snapshot()
        try:
            return self._check()
        finally:
            self._busy.release()

    def _check(self) -> dict:
        if not self.platform:
            self._set(state="latest", error="这个系统不支持自动更新", checked=self.clock())
            return self.snapshot()
        before = self.snapshot()
        self._set(state="checking", error="")
        try:
            manifest, source = self.fetch_manifest()
        except UpdateError as error:
            restore = before["state"] if before["state"] == "ready" else "failed"
            self._set(state=restore, error=str(error), checked=self.clock())
            return self.snapshot()
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            (self.dir / "accepted.json").write_text(json.dumps({"date": manifest["date"],
                                                                "version": manifest["version"]}), encoding="utf-8")
        except OSError:
            pass
        version = manifest["version"]
        if not newer(version, self.version):
            self._set(state="latest", version="", notes="", date="", error="", checked=self.clock(),
                      done=0, total=0)
            return self.snapshot()
        entry = pick_file(manifest, self.platform)
        if entry is None:
            self._set(state="failed", version=version, error=f"新版本 {version} 没有这个系统（{self.platform}）的安装包",
                      checked=self.clock())
            return self.snapshot()
        ready = self.ready()
        if ready is not None and ready["version"] == version and ready["sha256"] == entry["sha256"]:
            self._set(state="ready", version=version, notes=manifest.get("notes", ""), date=manifest["date"],
                      checked=self.clock())
            return self.snapshot()
        self._set(state="downloading", version=version, notes=manifest.get("notes", ""), date=manifest["date"],
                  done=0, total=entry["size"], checked=self.clock())
        try:
            path = self.download(manifest, entry, source)
        except UpdateError as error:
            self.log(f"更新：下载 {version} 没成：{error}")
            self._set(state="failed", error=str(error))
            return self.snapshot()
        info = {"version": version, "date": manifest["date"], "notes": manifest.get("notes", ""),
                "platform": self.platform, "name": entry["name"], "size": entry["size"], "sha256": entry["sha256"]}
        (path.parent / "ready.json").write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
        self.log(f"更新：新版本 {version} 下好了，等人点「更新并重启」")
        self._set(state="ready", done=entry["size"], total=entry["size"])
        return self.snapshot()

    # ---- 下 ----

    def download(self, manifest: dict, entry: dict, first: Source) -> Path:
        """下到 updates/<版本>/<文件名>，SHA-256 对上才算好。先用给出清单的那个源，再试别的；断了接着下。"""
        folder = self.dir / manifest["version"]
        folder.mkdir(parents=True, exist_ok=True)
        final = folder / entry["name"]
        part = folder / (entry["name"] + ".part")
        try:
            free = self.free_bytes(folder)
        except OSError:
            free = None
        if free is not None and free < entry["size"] + SPARE_BYTES:
            raise UpdateError("磁盘空间不够，先不下")
        order = [first] + [s for s in self.sources if s is not first]
        last = UpdateError("下不下来")
        for attempt in range(RETRIES):
            source = order[attempt % len(order)]
            try:
                self._fetch_file(source, manifest["version"], entry, part)
            except (OSError, ValueError, urllib.error.URLError) as error:
                last = UpdateError(f"从{source.label}下载断了（{type(error).__name__}）")
                self.log(f"更新：{last}，接着试")
                continue
            digest = _sha256(part)
            if digest != entry["sha256"] or part.stat().st_size != entry["size"]:
                part.unlink(missing_ok=True)
                last = UpdateError("下载的文件校验不对（SHA-256），删掉重下")
                self.log(f"更新：{last}")
                continue
            os.replace(part, final)
            return final
        raise last

    def _fetch_file(self, source: Source, version: str, entry: dict, part: Path) -> None:
        have = part.stat().st_size if part.exists() else 0
        if have >= entry["size"]:
            return
        open_ = self.opener(source)
        with open_(_request(source.file_url(version, entry["name"]), have), timeout=TIMEOUT_S) as answer:
            status = getattr(answer, "status", None) or answer.getcode()
            if have and status != 206:
                have = 0            # 不认 Range：从头来
            with open(part, "ab" if have else "wb") as out:
                while True:
                    chunk = answer.read(CHUNK)
                    if not chunk:
                        break
                    have += len(chunk)
                    if have > entry["size"]:
                        raise ValueError("比清单里说的大")
                    out.write(chunk)
                    self._set(done=have)
        if have < entry["size"]:
            raise OSError("没下完")

    # ---- 网页的请求、后台 ----

    def take_requests(self) -> None:
        """run/update-check：马上查；run/update-install：已经下好时标上 install，托盘看到就去装。"""
        check = self.run_dir / CHECK_REQUEST
        install = self.run_dir / INSTALL_REQUEST
        if install.exists():
            install.unlink(missing_ok=True)
            if self.snapshot()["state"] == "ready":
                self.log("更新：网页上点了「更新并重启」，等托盘去装")
                self._set(install=True)
        if check.exists():
            check.unlink(missing_ok=True)
            threading.Thread(target=self.check, daemon=True, name="update-check").start()

    def ask_install(self) -> None:
        self._set(install=True)

    def clear_install(self) -> None:
        self._set(install=False)

    def run(self, stop: threading.Event, first_delay: float = FIRST_DELAY_S, poll: float = 2.0) -> None:
        """后台线程：FIRST_DELAY_S 后查第一次，之后每 EVERY_S（失败了每 RETRY_S）查；每 poll 秒看一次请求文件。"""
        due = self.clock() + first_delay
        while not stop.wait(poll):
            self.take_requests()
            if self.clock() >= due:
                found = self.check()
                due = self.clock() + (RETRY_S if found["state"] == "failed" else EVERY_S)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def start(layout, version: str, log, frozen: bool, stop: threading.Event, proxy=lambda: "",
          lite: bool = False, portable: bool = False) -> Updater | None:
    """desktop_main 用：打包好的客户端才开（enabled），返回 Updater（控制接口、状态用），否则 None。"""
    if not enabled(frozen):
        return None
    updater = Updater(layout.root, layout.run, version, log, proxy=proxy,
                      platform=platform_key(portable=portable, lite=lite))
    updater.start()
    if not updater.key:
        log("更新：这一版没有内置更新签名公钥，不查更新")
        updater._set(state="latest", error="这一版不能自动更新")
        return updater
    threading.Thread(target=updater.run, args=(stop,), daemon=True, name="updater").start()
    return updater
