"""从这台电脑的浏览器读取 YouTube 登录（cookies），代替手动导出 cookies.txt。

YouTube 不给第三方工具扫码登录（yt-dlp 的 OAuth 2024 年被关掉了），cookies 就是登录。这里让 yt-dlp
（本机模式已经下载好的那个）用 --cookies-from-browser 从浏览器的 cookies 库里读出来，写进一个临时文件，
再只把 youtube.com、google.com 的条目存进 <数据目录>/youtube/cookies.txt（0600），临时文件马上删掉。

    yt-dlp --ignore-config --cookies-from-browser chrome:<配置目录> --cookies <临时文件>

不给网址：yt-dlp 不连网，只读 cookies；它会说「You must provide at least one URL」退出，退出前把读到的
cookies 写进 --cookies 的文件（YoutubeDL.close → save_cookies）。临时文件里是这个浏览器所有网站的 cookies，
所以放在数据目录里一个只有自己能进的临时目录（0700，子进程 umask 077），读完不管成败都整个删掉；
上次异常退出留下的也在下次读之前删掉。cookies 的值不进日志、不进回答、不进错误信息。

只认这几个浏览器，只认它们自己的数据目录里列出来的配置（页面传回来的配置名必须在列表里，不能是任意路径）：

    Chrome、Edge、Brave（Chromium 系）：<数据目录>/Local State 的 profile.info_cache（配置名、登录的 Google 账号）
    Firefox：profiles.ini（Name、Path）
    Safari：只在 macOS 上，没有多个配置

各系统的坑（页面上照着说）：macOS 读 Chrome、Edge、Brave 会弹钥匙串窗口（要点「允许」）；读 Safari 要给小鼠电视
「完全磁盘访问权限」；Windows 上新版 Chrome、Edge 的 cookies 只有浏览器自己能解开（App-Bound 加密），
浏览器开着时文件也被锁住。

谁能用、从哪能用由 server/relay/ytaccount.py 管（只在这台电脑上打开的电脑版、只给管理员）。
"""

from __future__ import annotations

import configparser
import json
import os
import platform
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from . import ytlocal

# 页面上的顺序。id 就是 yt-dlp 的浏览器名。
NAMES = {"chrome": "Chrome", "edge": "Edge", "firefox": "Firefox", "safari": "Safari", "brave": "Brave"}
CHROMIUM = ("chrome", "edge", "brave")
# 钥匙串窗口要等人点；yt-dlp_macos 是 PyInstaller 打的单文件，启动本身就要几秒。
READ_TIMEOUT_S = 180
TEMP_PREFIX = ".browser-read-"
# 登录了 YouTube 才有的 cookie（只有游客 cookies 时不覆盖原来的文件）。
LOGIN_NAMES = ("LOGIN_INFO", "SID", "__Secure-1PSID", "__Secure-3PSID", "SAPISID", "__Secure-3PAPISID")
PROFILE_MAX = 40
_NAME_MAX = 60


class BrowserError(Exception):
    """消息给人看（中文），不带 cookies。kind 是给日志的一个词（error_kind），不带原文。"""

    def __init__(self, message: str, kind: str = ""):
        super().__init__(message)
        self.kind = kind


# ---- 浏览器在哪 ----

def _system(system: str | None) -> str:
    return system or platform.system()


def chromium_dirs(system: str, home: Path, environ) -> dict[str, Path]:
    """Chrome、Edge、Brave 的用户数据目录（和 yt-dlp 的 _get_chromium_based_browser_settings 一样）。"""
    if system == "Windows":
        local = Path(environ.get("LOCALAPPDATA") or home / "AppData" / "Local")
        return {"chrome": local / "Google" / "Chrome" / "User Data",
                "edge": local / "Microsoft" / "Edge" / "User Data",
                "brave": local / "BraveSoftware" / "Brave-Browser" / "User Data"}
    if system == "Darwin":
        support = home / "Library" / "Application Support"
        return {"chrome": support / "Google" / "Chrome", "edge": support / "Microsoft Edge",
                "brave": support / "BraveSoftware" / "Brave-Browser"}
    config = Path(environ.get("XDG_CONFIG_HOME") or home / ".config")
    return {"chrome": config / "google-chrome", "edge": config / "microsoft-edge",
            "brave": config / "BraveSoftware" / "Brave-Browser"}


def firefox_dirs(system: str, home: Path, environ) -> list[Path]:
    """可能放 profiles.ini 的目录，按这个顺序找第一个有的。"""
    if system == "Windows":
        roaming = Path(environ.get("APPDATA") or home / "AppData" / "Roaming")
        return [roaming / "Mozilla" / "Firefox"]
    if system == "Darwin":
        return [home / "Library" / "Application Support" / "Firefox"]
    config = Path(environ.get("XDG_CONFIG_HOME") or home / ".config")
    return [config / "mozilla" / "firefox", home / ".mozilla" / "firefox",
            home / ".var" / "app" / "org.mozilla.firefox" / "config" / "mozilla" / "firefox",
            home / ".var" / "app" / "org.mozilla.firefox" / ".mozilla" / "firefox",
            home / "snap" / "firefox" / "common" / ".mozilla" / "firefox"]


def _clean(text, limit: int = _NAME_MAX) -> str:
    return " ".join(str(text or "").split())[:limit]


def chromium_profiles(local_state: str, base: Path, exists=os.path.isdir) -> list[dict]:
    """Local State 的 profile.info_cache → [{"id": 目录名, "name": 配置名, "account": 登录的 Google 账号}]。
    只列目录真在的；读不懂时有 Default 就只列 Default。"""
    try:
        data = json.loads(local_state)
    except ValueError:
        data = {}
    cache = ((data.get("profile") or {}).get("info_cache") if isinstance(data, dict) else None) or {}
    found = []
    if isinstance(cache, dict):
        for folder, info in cache.items():
            if not isinstance(folder, str) or not folder or folder.startswith(".") \
                    or "/" in folder or "\\" in folder or not exists(str(base / folder)):
                continue
            info = info if isinstance(info, dict) else {}
            name = _clean(info.get("name")) or folder
            gaia, email = _clean(info.get("gaia_name")), _clean(info.get("user_name"))
            account = f"{gaia}（{email}）" if gaia and email else gaia or email
            found.append({"id": folder, "name": name, "account": account})
    if not found and exists(str(base / "Default")):
        found.append({"id": "Default", "name": "Default", "account": ""})
    found.sort(key=lambda p: (p["id"] != "Default", _natural(p["id"])))
    return found[:PROFILE_MAX]


def _natural(text: str):
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", text)]


def firefox_profiles(ini_text: str, base: Path, exists=os.path.isdir) -> list[dict]:
    """profiles.ini → [{"id": ini 里的 Path, "name": Name, "account": ""}]，默认的排在前面。"""
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    try:
        parser.read_string(ini_text)
    except configparser.Error:
        return []
    defaults = {parser.get(s, "Default", fallback="") for s in parser.sections() if s.startswith("Install")}
    found = []
    for section in parser.sections():
        if not section.startswith("Profile"):
            continue
        path = parser.get(section, "Path", fallback="").strip()
        if not path:
            continue
        relative = parser.get(section, "IsRelative", fallback="1").strip() != "0"
        folder = base / path if relative else Path(path)
        if not exists(str(folder)):
            continue
        is_default = path in defaults or parser.get(section, "Default", fallback="") == "1"
        found.append({"id": path, "name": _clean(parser.get(section, "Name", fallback="")) or path,
                      "account": "", "default": is_default})
    found.sort(key=lambda p: (not p.pop("default"), _natural(p["name"])))
    return found[:PROFILE_MAX]


def _read(path: Path) -> str | None:
    try:
        if path.stat().st_size > 4 * 1024 * 1024:
            return None
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def detect(system: str | None = None, home: Path | None = None, environ=None,
           safari_app: Path = Path("/Applications/Safari.app")) -> list[dict]:
    """这台电脑上有的浏览器：[{"id", "name", "profiles": [...], "base": 数据目录}]，按 NAMES 的顺序。
    只看目录和配置清单（Local State、profiles.ini），不碰 cookies。"""
    system = _system(system)
    home = Path(home) if home is not None else Path.home()
    environ = os.environ if environ is None else environ
    found = {}
    for browser, base in chromium_dirs(system, home, environ).items():
        if not base.is_dir():
            continue
        state = _read(base / "Local State")
        profiles = chromium_profiles(state or "{}", base)
        if profiles:
            found[browser] = {"id": browser, "name": NAMES[browser], "profiles": profiles, "base": str(base)}
    for base in firefox_dirs(system, home, environ):
        ini = _read(base / "profiles.ini")
        if ini is None:
            continue
        profiles = firefox_profiles(ini, base)
        if profiles:
            found["firefox"] = {"id": "firefox", "name": NAMES["firefox"], "profiles": profiles, "base": str(base)}
            break
    if system == "Darwin" and Path(safari_app).is_dir():
        found["safari"] = {"id": "safari", "name": NAMES["safari"], "profiles": [], "base": ""}
    return [found[b] for b in NAMES if b in found]


def public(browsers: list[dict]) -> list[dict]:
    """给页面的：不带目录。"""
    return [{"id": b["id"], "name": b["name"],
             "profiles": [{k: p[k] for k in ("id", "name", "account")} for p in b["profiles"]]} for b in browsers]


def spec_for(browsers: list[dict], browser: str, profile: str) -> tuple[str, dict]:
    """页面选的浏览器和配置 → (yt-dlp 的 --cookies-from-browser 参数, 选中的配置)。只认 detect() 列出来的；
    配置用绝对路径交给 yt-dlp（yt-dlp 自己按同样的目录找，路径更不会弄错）。"""
    entry = next((b for b in browsers if b["id"] == browser), None)
    if entry is None:
        raise BrowserError("这台电脑上没有找到这个浏览器")
    if browser == "safari":
        return "safari", {"id": "", "name": "", "account": ""}
    if not entry["profiles"]:
        raise BrowserError("这个浏览器里没有找到配置")
    chosen = next((p for p in entry["profiles"] if p["id"] == profile), None) if profile else entry["profiles"][0]
    if chosen is None:
        raise BrowserError("没有这个浏览器配置：刷新页面再选")
    base = Path(entry["base"])
    folder = base / chosen["id"]
    if browser == "firefox" and Path(chosen["id"]).is_absolute():
        folder = Path(chosen["id"])
    return f"{browser}:{folder}", chosen


def extract_args(ytdlp: str, spec: str, out: Path) -> list[str]:
    from ..relay.ytnode import ytdlp_command
    # .pyz（纯 Python 版）经本机的 Python 跑；以前的单文件版直接跑（ytnode.ytdlp_command）。
    return ytdlp_command(ytdlp) + ["--ignore-config", "--cookies-from-browser", spec, "--cookies", str(out)]


# ---- 读出来的 cookies：只留 YouTube、Google ----

def filter_cookies(text: str, now: float | None = None) -> tuple[str, int, bool]:
    """yt-dlp 写的 cookies 文件（这个浏览器所有网站的）→ (只含 youtube.com 和 google.com 的 Netscape 文本,
    youtube.com 的有效条数, 是否登录了 YouTube)。google.com 只留主域名的（YouTube 登录用到的），
    放不进 ytlocal.COOKIES_MAX 时先舍掉 google.com 的。"""
    now = time.time() if now is None else now
    youtube, google, count, login = [], [], 0, False
    for raw in (text or "").replace("\r\n", "\n").split("\n"):
        line = raw.strip("\r")
        http_only = line.startswith("#HttpOnly_")
        if not line or (line.startswith("#") and not http_only):
            continue
        fields = line.split("\t")
        if len(fields) != 7 or "\x00" in line:
            continue
        domain = (fields[0][len("#HttpOnly_"):] if http_only else fields[0]).lower().lstrip(".")
        if not re.match(r"^-?\d+$", fields[4] or "0"):
            continue
        if domain == "youtube.com" or domain.endswith(".youtube.com"):
            youtube.append(line)
            expires = int(fields[4] or 0)
            if expires == 0 or expires > now:
                count += 1
                if fields[5] in LOGIN_NAMES:
                    login = True
        elif domain == "google.com":
            google.append(line)
    header = ytlocal.COOKIE_HEADER
    limit = ytlocal.COOKIES_MAX - 1024
    body = youtube + google
    while body and len((header + "\n".join(body) + "\n").encode("utf-8", "replace")) > limit:
        if google:
            google.pop()
        else:
            youtube.pop()
        body = youtube + google
    return header + "\n".join(body) + "\n", count, login


# ---- yt-dlp 失败时说人话 ----

def explain(stderr: str, browser: str, system: str | None = None) -> str:
    """yt-dlp 的错误输出 → 给人看的中文。只认已知的几种；不把原文给页面（原文里可能有路径之类）。"""
    system = _system(system)
    text = stderr or ""
    lower = text.lower()
    name = NAMES.get(browser, browser)
    if "sqlite3" in lower:
        return "这个 yt-dlp 读不了浏览器的 cookies 库（缺 sqlite3）：用「导入 cookies.txt」"
    if browser == "safari" and ("operation not permitted" in lower or "permission" in lower or "errno 1]" in lower):
        return ("macOS 不让读 Safari 的 cookies：打开「系统设置 → 隐私与安全性 → 完全磁盘访问权限」，打开「小鼠电视」"
                "（RooTV），然后退出、重新打开小鼠电视再试。不想给这个权限就用 Chrome、Firefox，或「导入 cookies.txt」")
    if ("could not find" in lower and "cookies database" in lower) or "could not find safari cookies" in lower:
        return f"没找到 {name} 的 cookies：这个浏览器（配置）可能还没用过，先用它打开一次 YouTube"
    if "could not copy chrome cookie database" in lower or ("database is locked" in lower):
        return f"{name} 开着时 cookies 文件被锁住了：完全退出 {name}（包括后台和托盘里的）再试"
    if "dpapi" in lower or "app-bound" in lower:
        return (f"新版 {name} 在 Windows 上的 cookies 只有浏览器自己能解开，yt-dlp 读不了："
                "换 Firefox，或者用「导入 cookies.txt」")
    if "find-generic-password" in lower:
        return (f"macOS 钥匙串没有允许读取「{name} Safe Storage」：再点一次，在弹出的窗口里输入这台 Mac 的登录密码，"
                "点「允许」")
    if "kwallet" in lower or "secretstorage" in lower or "keyring" in lower:
        return f"读不了系统钥匙环里 {name} 的密钥：用「导入 cookies.txt」"
    if "operation not permitted" in lower or "permission denied" in lower or "permissionerror" in lower:
        return f"没有权限读 {name} 的 cookies 文件：退出 {name} 再试，或用「导入 cookies.txt」"
    if "unsupported platform" in lower:
        return "这个系统上读不了 Safari"
    return f"从 {name} 读取失败：用「导入 cookies.txt」试试，或换一个浏览器"


def error_kind(stderr: str) -> str:
    """给日志的一个词（不带原文）。"""
    lower = (stderr or "").lower()
    for word, kind in (("could not find", "not-found"), ("operation not permitted", "no-permission"),
                       ("permission", "no-permission"), ("dpapi", "app-bound"),
                       ("could not copy", "locked"), ("locked", "locked"), ("find-generic-password", "keychain"),
                       ("keyring", "keyring"), ("kwallet", "keyring"), ("sqlite3", "no-sqlite")):
        if word in lower:
            return kind
    found = re.search(r"\b([A-Z][A-Za-z]*Error)\b", stderr or "")
    return found.group(1) if found else "unknown"


def _run(args: list[str], timeout: float) -> tuple[int, str]:
    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = 0x08000000
    else:
        kwargs["umask"] = 0o077
    done = subprocess.run(args, capture_output=True, text=True, errors="replace", timeout=timeout,
                          stdin=subprocess.DEVNULL, **kwargs)
    return done.returncode, done.stderr or ""


def clean_stale(parent: Path) -> None:
    """上次没删掉的临时目录（进程被杀了之类）：里面可能有别的网站的 cookies，先删。"""
    try:
        entries = list(Path(parent).iterdir())
    except OSError:
        return
    for entry in entries:
        if entry.name.startswith(TEMP_PREFIX):
            shutil.rmtree(entry, ignore_errors=True)


def read(ytdlp: str, spec: str, browser: str, dest: Path, run=_run, timeout: float = READ_TIMEOUT_S,
         system: str | None = None, now: float | None = None) -> dict:
    """用 yt-dlp 读一个浏览器的 cookies，只把 YouTube 的存进 dest（0600）。
    成功 → {"count", "login"}；失败 → BrowserError（中文，不带 cookies）。dest 原来的文件只在读到了
    登录过的 YouTube cookies 时才被换掉。"""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    clean_stale(dest.parent)
    folder = Path(tempfile.mkdtemp(prefix=TEMP_PREFIX, dir=str(dest.parent)))
    try:
        try:
            os.chmod(folder, 0o700)
        except OSError:
            pass
        out = folder / "all.txt"
        try:
            code, stderr = run(extract_args(ytdlp, spec, out), timeout)
        except subprocess.TimeoutExpired:
            raise BrowserError(f"等了 {int(timeout)} 秒没有结果：弹出钥匙串（或别的权限）窗口时要点「允许」，再试一次",
                               "timeout") from None
        except OSError:
            raise BrowserError("运行不了 yt-dlp：在上面重新「安装」YouTube 组件", "no-ytdlp") from None
        try:
            text = out.read_text(encoding="utf-8", errors="replace") if out.is_file() else ""
        except OSError:
            text = ""
        if not text.strip():
            raise BrowserError(explain(stderr, browser, system), error_kind(stderr))
        content, count, login = filter_cookies(text, now)
        text = ""
        if not count:
            raise BrowserError(f"{NAMES.get(browser, browser)} 里没有 YouTube 的 cookies："
                               "先用这个浏览器（配置）登录 YouTube，再来读取"
                               + ("（钥匙串没允许时也会这样）" if "find-generic-password" in (stderr or "") else ""),
                               "no-youtube")
        if not login:
            raise BrowserError(f"{NAMES.get(browser, browser)} 里 YouTube 没有登录（只读到 {count} 条游客 cookies，"
                               "没有保存）：先用这个浏览器（配置）登录 YouTube 小号"
                               + ("。macOS 的钥匙串窗口要点「允许」" if "find-generic-password" in (stderr or "") else ""),
                               "not-signed-in")
        try:
            saved = ytlocal.save_cookies(dest, content, now)
        except ytlocal.SettingsError as error:
            raise BrowserError(str(error)) from None
        return {"count": saved, "login": True, "code": code}
    finally:
        shutil.rmtree(folder, ignore_errors=True)
