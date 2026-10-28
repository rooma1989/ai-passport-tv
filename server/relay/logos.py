"""直播台的台标（M3U 的 tvg-logo）：取一次、存在数据目录的 logos/ 里，之后从本站给页面。

电脑版的 CSP 只许本站的图片（img-src 'self'），台标却在各种网站上，所以由中转去取。它不是一个
「给我一个地址我就去取」的代理：页面只能报台的 id（GET /api/art/logo/<台 id>），地址只来自
频道表旁边的 channels.txt.meta.json（总后台导入播放列表时存的，见 channeltable.meta_of）。

- 只取 http(s)，不带登录信息、不带端口以外的花样；主机解析出来是本机、局域网、链路本地等地址的不取
  （免得频道表里一个地址就能让中转去碰家里的路由器；代理 TUN 的假 IP 除外，见 FAKE_IP_NETWORKS）；跳转最多 MAX_REDIRECTS 次，每一跳重新检查。
- 最多 MAX_BYTES，只收 PNG、JPEG、GIF、WebP（按文件头认，不信对方说的类型）；SVG 不收（能带脚本）。
- 取到的按地址的散列存成 <散列>.<扩展名>：地址改了就是新文件，旧文件留着不碍事。取不到的记下来，
  RETRY_S 内不再去（重启后再试一次）；同时最多 PARALLEL 个在取，目录里最多 MAX_FILES 个文件。
- 频道表里没写台标的台，用节目单里对上的那个台的 <icon src>（extra，epg.Guide.icons）：取的规矩一样。
- 没有台标、取不到：404，页面画一个用台名生成的徽标。
- 唯一的例外是总后台频道表里改台标时的「看一眼」（preview，只有管理员能用，admin.logo_preview）：地址是管理员
  填的，规矩和上面完全一样，取到的存进同一个缓存。
"""

from __future__ import annotations

import hashlib
import ipaddress
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import channeltable

MAX_BYTES = 256 * 1024
TIMEOUT_S = 6.0
RETRY_S = 6 * 3600
PARALLEL = 2
MAX_FILES = 2000
MAX_REDIRECTS = 3
USER_AGENT = "rootv-logo/1"
_CID = re.compile(r"^[A-Za-z0-9_-]{1,15}$")
# 代理软件 TUN 模式的「假 IP」（Clash、Surge、Stash 的 fake-ip：198.18.0.0/15，IPv6 是 2001:2::/48）：开着它时系统
# 解析出来的每个网站都是这种地址，由代理按域名去连真的网站；家里的设备不会用这两段（见 server/netident.py）。
# 不放过它们的话，开着代理的电脑上一个台标都取不到。
FAKE_IP_NETWORKS = (ipaddress.ip_network("198.18.0.0/15"), ipaddress.ip_network("2001:2::/48"))
_TYPES = {"png": "image/png", "jpg": "image/jpeg", "gif": "image/gif", "webp": "image/webp"}


def sniff(body: bytes) -> str | None:
    """文件头 → 扩展名；不认识的（含 SVG、HTML）为 None。"""
    if body.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if body.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if body.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if len(body) >= 12 and body[:4] == b"RIFF" and body[8:12] == b"WEBP":
        return "webp"
    return None


def allowed_url(url: str, resolve=socket.getaddrinfo) -> bool:
    """http(s)、没有用户名密码、主机解析出来全是公网地址。"""
    try:
        parts = urllib.parse.urlsplit(url)
        port = parts.port
    except ValueError:
        return False
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
        return False
    if port is not None and port not in (80, 443, 8080, 8443):
        return False
    try:
        infos = resolve(parts.hostname, port or (443 if parts.scheme == "https" else 80), proto=socket.IPPROTO_TCP)
    except (OSError, UnicodeError):
        return False
    if not infos:
        return False
    for info in infos:
        try:
            address = ipaddress.ip_address(info[4][0].split("%", 1)[0])
        except ValueError:
            return False
        if not address.is_global and not any(address in network for network in FAKE_IP_NETWORKS):
            return False
    return True


class _Checked(urllib.request.HTTPRedirectHandler):
    max_redirections = MAX_REDIRECTS

    def __init__(self, check):
        super().__init__()
        self.check = check

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not self.check(newurl):
            return None
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch(url: str, check=allowed_url, timeout: float = TIMEOUT_S) -> bytes:
    """取一个台标的原始字节（多读一个字节，好知道是否超过上限）；不合规就 ValueError。"""
    if not check(url):
        raise ValueError("地址不允许")
    opener = urllib.request.build_opener(_Checked(check))
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "image/*"})
    with opener.open(request, timeout=timeout) as answer:
        return answer.read(MAX_BYTES + 1)


class LogoCache:
    def __init__(self, folder: Path, table: Path | None, fetcher=fetch, clock=time.monotonic, log=lambda _m: None,
                 extra=None):
        self.folder = Path(folder)
        # 兜底的台标 {台 id: 地址}（节目单的 <icon src>）：频道表里写了台标的台不用它。
        self.extra = extra
        self.table = Path(table) if table is not None else None
        self.fetcher = fetcher
        self.clock = clock
        self.log = log
        self.lock = threading.Lock()
        self._gate = threading.BoundedSemaphore(PARALLEL)
        self._failed: dict[str, float] = {}
        self._meta: tuple[float, dict] | None = None

    def _logos(self) -> dict[str, str]:
        """{台 id: 台标地址}：频道表 meta 里写的，没写的台用 extra 给的；meta 文件改了才重读。"""
        own = self._own()
        extra = self.extra() if self.extra is not None else None
        if not extra:
            return own
        return {**extra, **own}

    def _own(self) -> dict[str, str]:
        if self.table is None:
            return {}
        path = channeltable.meta_path(self.table)
        try:
            stamp = path.stat().st_mtime
        except OSError:
            return {}
        with self.lock:
            if self._meta is None or self._meta[0] != stamp:
                meta = channeltable.meta_of(self.table)
                self._meta = (stamp, {cid: m["logo"] for cid, m in meta.items() if m.get("logo")})
            return self._meta[1]

    def has(self, cid: str) -> bool:
        return cid in self._logos()

    def known(self) -> set[str]:
        return set(self._logos())

    @staticmethod
    def _name(url: str) -> str:
        return hashlib.sha256(url.encode("utf-8")).hexdigest()[:32]

    def _cached(self, name: str) -> tuple[str, bytes] | None:
        for ext, kind in _TYPES.items():
            path = self.folder / f"{name}.{ext}"
            try:
                return kind, path.read_bytes()
            except OSError:
                continue
        return None

    def get(self, cid) -> tuple[str, bytes] | None:
        """(类型, 内容)，或 None（没有台标、取不到、地址不允许）。"""
        if not isinstance(cid, str) or not _CID.match(cid):
            return None
        url = self._logos().get(cid)
        if not url:
            return None
        name = self._name(url)
        found = self._cached(name)
        if found is not None:
            return found
        with self.lock:
            failed = self._failed.get(name)
            if failed is not None and self.clock() - failed < RETRY_S:
                return None
        if not self._gate.acquire(timeout=TIMEOUT_S):
            return None
        try:
            found = self._cached(name)          # 别的请求刚取到
            if found is not None:
                return found
            try:
                body = self.fetcher(url)
            except (OSError, ValueError, urllib.error.URLError) as error:
                self._fail(name, f"{cid} 台标取不到（{type(error).__name__}）")
                return None
            ext = sniff(body)
            if ext is None or len(body) > MAX_BYTES:
                self._fail(name, f"{cid} 台标不是认得的图片，或者太大")
                return None
            try:
                self.folder.mkdir(parents=True, exist_ok=True)
                if sum(1 for _ in self.folder.iterdir()) >= MAX_FILES:
                    self._fail(name, "台标目录满了")
                    return _TYPES[ext], body
                tmp = self.folder / f".{name}.tmp"
                tmp.write_bytes(body)
                tmp.replace(self.folder / f"{name}.{ext}")
            except OSError as error:
                self.log(f"台标存不下：{error}")
            return _TYPES[ext], body
        finally:
            self._gate.release()

    def preview(self, url: str) -> tuple[tuple[str, bytes] | None, str]:
        """总后台频道表里改台标时看一眼（admin.logo_preview）：((类型, 内容), "") 或 (None, 为什么看不了)。
        规矩和 get 一样（同一个 fetcher：只取公网、跳转每一跳都查、只收认得的图片），取到的按地址存进缓存，
        保存以后 get 不用再取；取不到的不记失败（管理员可能马上改好地址再试）。"""
        if not channeltable.clean_meta({"logo": url}):
            return None, "台标地址要以 http:// 或 https:// 开头，不能有空格和引号"
        name = self._name(url)
        found = self._cached(name)
        if found is not None:
            return found, ""
        if not self._gate.acquire(timeout=TIMEOUT_S):
            return None, "正在取别的台标，稍等再试"
        try:
            try:
                body = self.fetcher(url)
            except ValueError:
                return None, "这个地址不取：只取公网上的 http(s) 图片（本机、局域网的地址不取）"
            except (OSError, urllib.error.URLError) as error:
                return None, f"取不到这个地址（{type(error).__name__}）"
            ext = sniff(body)
            if ext is None:
                return None, "这个地址不是认得的图片（只认 PNG、JPEG、GIF、WebP）"
            if len(body) > MAX_BYTES:
                return None, f"图片太大（最多 {MAX_BYTES // 1024} KB）"
            try:
                self.folder.mkdir(parents=True, exist_ok=True)
                if sum(1 for _ in self.folder.iterdir()) < MAX_FILES:
                    tmp = self.folder / f".{name}.tmp"
                    tmp.write_bytes(body)
                    tmp.replace(self.folder / f"{name}.{ext}")
            except OSError as error:
                self.log(f"台标存不下：{error}")
            with self.lock:
                self._failed.pop(name, None)
            return (_TYPES[ext], body), ""
        finally:
            self._gate.release()

    def _fail(self, name: str, message: str) -> None:
        with self.lock:
            self._failed[name] = self.clock()
        self.log(message)
