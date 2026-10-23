"""YouTube 本机模式的设置：代理（校验、自动检测系统代理）、cookies.txt（校验、保存、删除）、改 config.toml 的
[youtube] 一节、请监督进程按新设置更新子进程。

给电脑版「设置 → YouTube」（server/relay/ytsettings.py）和监督进程（__main__.build_children）用。
cookies 的内容从不写进日志、错误信息或回答里：这里只数条数。
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path

from ..relay import ytnode

MODES = ("off", "local", "node")
REFRESH_REQUEST = "refresh-request"
# 前端（server/front.py）只收 64 KB 的请求体；只含 YouTube 的 cookies.txt 一般几 KB。
COOKIES_MAX = 48 * 1024
# 只留这些域名的 cookies（YouTube 用到的）；别的网站的不存。
COOKIE_DOMAINS = ("youtube.com", "google.com")
COOKIE_HEADER = "# Netscape HTTP Cookie File\n# 小智电视：只留了 youtube.com、google.com 的条目。不要发给别人。\n"


class SettingsError(ValueError):
    """消息给人看（中文），不带代理密码、不带 cookies。"""


# ---- 代理 ----

def check_proxy(url: str) -> str:
    """空 = 不用代理（直连，或代理软件开着 TUN / 增强模式）；否则规范化后的地址。不对就 SettingsError。"""
    text = (url or "").strip()
    if not text:
        return ""
    try:
        ytnode.parse_proxy(text)
    except ValueError as error:
        raise SettingsError(str(error)) from None
    return text


def _scutil(run) -> str:
    return run(["scutil", "--proxy"])


def parse_scutil(text: str) -> str:
    """macOS `scutil --proxy` 的输出 → 代理地址（HTTPS 优先，再 HTTP，再 SOCKS）；没开的时候空串。"""
    values = {}
    for line in text.splitlines():
        key, sep, value = line.partition(" : ")
        if sep:
            values[key.strip()] = value.strip()
    for prefix, scheme in (("HTTPS", "http"), ("HTTP", "http"), ("SOCKS", "socks5")):
        if values.get(f"{prefix}Enable") == "1" and values.get(f"{prefix}Proxy") and \
                values.get(f"{prefix}Port", "").isdigit():
            return f"{scheme}://{values[f'{prefix}Proxy']}:{values[f'{prefix}Port']}"
    return ""


def parse_windows(enabled, server) -> str:
    """Windows 注册表 Internet Settings 的 ProxyEnable、ProxyServer → 代理地址。ProxyServer 可以是
    「主机:端口」，也可以是「http=主机:端口;https=…;socks=…」。"""
    if not enabled or not isinstance(server, str) or not server.strip():
        return ""
    server = server.strip()
    if "=" not in server:
        return server if "://" in server else f"http://{server}"
    entries = {}
    for part in server.split(";"):
        kind, sep, address = part.partition("=")
        if sep and address.strip():
            entries[kind.strip().lower()] = address.strip()
    for kind, scheme in (("https", "http"), ("http", "http"), ("socks", "socks5")):
        if kind in entries:
            address = entries[kind]
            return address if "://" in address else f"{scheme}://{address}"
    return ""


def parse_environ(environ) -> str:
    for key in ("https_proxy", "HTTPS_PROXY", "all_proxy", "ALL_PROXY", "http_proxy", "HTTP_PROXY"):
        value = (environ.get(key) or "").strip()
        if value:
            if value.startswith("socks5h://") or value.startswith("socks5://") or value.startswith("http://"):
                return value.rstrip("/")
            if value.startswith("socks://"):
                return "socks5://" + value[len("socks://"):].rstrip("/")
            if "://" not in value:
                return "http://" + value.rstrip("/")
    return ""


def _windows_registry() -> tuple:
    import winreg  # noqa: PLC0415 —— 只有 Windows 有
    path = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as key:
        try:
            enabled = winreg.QueryValueEx(key, "ProxyEnable")[0]
        except OSError:
            enabled = 0
        try:
            server = winreg.QueryValueEx(key, "ProxyServer")[0]
        except OSError:
            server = ""
    return enabled, server


def _run(command: list[str]) -> str:
    try:
        done = subprocess.run(command, capture_output=True, text=True, errors="replace", timeout=5,
                              stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout or ""


def detect_proxy(system: str | None = None, run=_run, environ=None, registry=None,
                 in_container: bool = False) -> dict:
    """系统代理：{"proxy": 地址或空串, "source": 从哪看到的}。macOS 看 scutil --proxy，Windows 看注册表，
    Linux（和容器里）看环境变量。"""
    import platform
    system = system or platform.system()
    environ = os.environ if environ is None else environ
    found, source = "", ""
    if system == "Darwin" and not in_container:
        found, source = parse_scutil(_scutil(run)), "系统的网络代理设置"
    elif system == "Windows" and not in_container:
        try:
            found = parse_windows(*(registry or _windows_registry)())
        except OSError:
            found = ""
        source = "Windows 的代理设置"
    if not found:
        found = parse_environ(environ)
        source = "环境变量" if found else source
    if found:
        try:
            found = check_proxy(found)
        except SettingsError:
            found = ""
    return {"proxy": found, "source": source if found else ""}


# ---- cookies.txt ----

def _domain_ok(domain: str) -> bool:
    domain = domain.lower().lstrip(".")
    return any(domain == d or domain.endswith("." + d) for d in COOKIE_DOMAINS)


def parse_cookies(text: str, now: float | None = None) -> tuple[str, int]:
    """Netscape 格式的 cookies.txt → (只留 YouTube、Google 条目的新内容, youtube.com 的有效条数)。
    格式不对、没有 youtube.com 的条目就 SettingsError（消息不带内容）。"""
    if not isinstance(text, str) or not text.strip():
        raise SettingsError("文件是空的")
    if len(text.encode("utf-8", "replace")) > COOKIES_MAX:
        raise SettingsError("文件太大了：只导出 youtube.com 的 cookies 再试")
    if "\x00" in text:
        raise SettingsError("这不是文本文件")
    now = time.time() if now is None else now
    kept, youtube, valid = [], 0, 0
    for raw in text.replace("\r\n", "\n").split("\n"):
        line = raw.strip("\r")
        if not line.strip():
            continue
        http_only = line.startswith("#HttpOnly_")
        if line.startswith("#") and not http_only:
            continue
        fields = line.split("\t")
        if len(fields) != 7:
            continue
        domain = fields[0][len("#HttpOnly_"):] if http_only else fields[0]
        flag, path, secure, expiry, name = fields[1], fields[2], fields[3], fields[4], fields[5]
        if flag.upper() not in ("TRUE", "FALSE") or secure.upper() not in ("TRUE", "FALSE") \
                or not path.startswith("/") or not name or not re.match(r"^-?\d+$", expiry or "0"):
            continue
        valid += 1
        if not _domain_ok(domain):
            continue
        kept.append(line)
        expires = int(expiry or 0)
        if domain.lower().lstrip(".").endswith("youtube.com") and (expires == 0 or expires > now):
            youtube += 1
    if not valid:
        raise SettingsError("不是 Netscape 格式的 cookies.txt（用浏览器扩展「Get cookies.txt LOCALLY」之类导出）")
    if not youtube:
        raise SettingsError("里面没有 youtube.com 的有效 cookies：登录 YouTube 后再导出（可能已经过期）")
    return COOKIE_HEADER + "\n".join(kept) + "\n", youtube


def cookies_path(data_dir: Path, configured: str) -> Path:
    """config.toml 的 [youtube] cookies：相对路径按数据目录算。"""
    path = Path(configured or "youtube/cookies.txt")
    return path if path.is_absolute() else Path(data_dir) / path


def save_cookies(path: Path, text: str, now: float | None = None) -> int:
    """校验、只留 YouTube 的条目，存成 0600；返回 youtube.com 的有效条数。"""
    content, count = parse_cookies(text, now)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.unlink()
    except OSError:
        pass
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(content)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return count


def cookies_summary(path: Path, now: float | None = None) -> dict:
    """{"present", "count"（youtube.com 的有效条数）, "updated"}；不读出任何值。"""
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        updated = path.stat().st_mtime
    except OSError:
        return {"present": False, "count": 0, "updated": 0}
    try:
        _, count = parse_cookies(text, now)
    except SettingsError:
        count = 0
    return {"present": True, "count": count, "updated": updated}


def delete_cookies(path: Path) -> bool:
    try:
        Path(path).unlink()
        return True
    except FileNotFoundError:
        return False


# ---- config.toml ----

_SECTION = re.compile(r"^\s*\[\s*youtube\s*\]\s*(?:#.*)?$")
_ANY_SECTION = re.compile(r"^\s*\[")


def _set_key(lines: list[str], start: int, end: int, key: str, value: str) -> int:
    """在 lines[start:end] 里把 key 的值换成 value（TOML 字符串），保留行尾注释；没有就加一行。返回新的 end。"""
    pattern = re.compile(rf'^(\s*{key}\s*=\s*)("(?:[^"\\]|\\.)*"|\'[^\']*\')(.*)$')
    plain = re.compile(rf"^\s*{key}\s*=")
    quoted = '"' + value + '"'
    for index in range(start, end):
        match = pattern.match(lines[index])
        if match:
            rest = match.group(3)
            comment = re.match(r"^(\s+)(#.*)$", rest)
            if comment:
                # 行尾注释照旧对齐：值变长变短，空格跟着少或多（至少一个）。
                width = len(match.group(2)) + len(comment.group(1))
                rest = " " * max(1, width - len(quoted)) + comment.group(2)
            lines[index] = match.group(1) + quoted + rest
            return end
        if plain.match(lines[index]):
            lines[index] = f"{key} = {quoted}"
            return end
    lines.insert(end, f"{key} = {quoted}")
    return end + 1


def set_youtube(config_path: Path, mode: str, proxy: str) -> None:
    """改 config.toml 的 [youtube] mode 和 proxy（别的行、注释都不动）；改完能读通才写回去。"""
    from . import config as config_module
    if mode not in MODES:
        raise SettingsError("模式只能是 关闭 / 本机 / 自己的节点")
    proxy = check_proxy(proxy)
    path = Path(config_path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        raise SettingsError("读不了 config.toml") from None
    lines = text.split("\n")
    start = next((i for i, line in enumerate(lines) if _SECTION.match(line)), None)
    if start is None:
        if lines and lines[-1] != "":
            lines.append("")
        lines += ["[youtube]", f'mode = "{mode}"', f'proxy = "{proxy}"', 'cookies = "youtube/cookies.txt"', ""]
    else:
        end = next((i for i in range(start + 1, len(lines)) if _ANY_SECTION.match(lines[i])), len(lines))
        while end > start + 1 and not lines[end - 1].strip():
            end -= 1
        end = _set_key(lines, start + 1, end, "mode", mode)
        _set_key(lines, start + 1, end, "proxy", proxy)
    new_text = "\n".join(lines)
    try:
        parsed = config_module.parse(new_text)
    except config_module.ConfigError as error:
        raise SettingsError(f"config.toml 改不了：{error}") from None
    if (parsed.youtube_mode, parsed.youtube_proxy) != (mode, proxy):
        raise SettingsError("config.toml 改不了：[youtube] 一节的写法看不懂，请手动改")
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(new_text, encoding="utf-8")
    try:
        os.chmod(tmp, path.stat().st_mode & 0o777)
    except OSError:
        pass
    os.replace(tmp, path)


def request_refresh(run_dir: Path) -> None:
    """请监督进程重读 config.toml，只重启参数变了的子进程（supervisor.Supervisor 每半秒看一次）。"""
    path = Path(run_dir) / REFRESH_REQUEST
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(time.time()), encoding="utf-8")
