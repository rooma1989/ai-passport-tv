"""其他网站的视频（A1）：粘贴 yt-dlp 认得的任意视频网址，在电脑上放、投到小电视。

设计：docs/superpowers/specs/2026-11-05-web-sources-design.md。只在家庭服务器、电脑客户端的 YouTube 本机模式下有
（解析靠本机的 yt-dlp 节点 ytnode.py 的 /web/*）；精简版、云端没有。

边界：只认 yt-dlp 有专门解析器的普通视频网页（节点拒绝 generic 解析器）；本机、内网地址一律不收；有 DRM 的照实说、
不重试；不认「苹果CMS 采集接口」那类资源站。

    GET  /api/websites/info?url=        家人：解析一个网址（视频 → 标题、时长；列表 → 里面的视频），访客 404
    GET  /api/websites/thumb?u=         家人：封面图（电脑版的 CSP 只许本站的图片）。只取刚才解析时节点给过的封面地址
                                        （THUMBS_MAX 个，不收别的地址：不能拿它去访问任意网址），只要图片、至多 THUMB_MAX 字节
    GET  /api/websites/settings         管理员：每个站的 cookies（有没有、几条、什么时候导入）和代理
    POST /api/websites/cookies          管理员：{"site" 或 "url", "text": cookies.txt}：只留这个站的条目，存成 0600
    POST /api/websites/cookies/delete   管理员：{"site"}
    POST /api/websites/proxy            管理员：{"site", "proxy": "direct" | "youtube"}

站点的设置在数据目录的 websites/ 里：cookies/<站>.txt、sites.json（{"<站>": {"proxy": "youtube"}}，没写的直连）。
节点每次调用时现读，改了不用重启。cookies 的内容从不进日志、不进回答；日志里只写站名，不写完整网址和媒体地址。
"""

from __future__ import annotations

import collections
import ipaddress
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import vod, ytvod
from .bilibili import SourceError
from .state import Video
from .youtube import NodeError, StreamProc, YouTube

PREFIX = "/api/websites/"
URL_MAX = 2000
BODY_MAX = 512 * 1024
COOKIES_MAX = 400 * 1024
INFO_TIMEOUT_S = 90
FORMATS_TIMEOUT_S = 90
STREAM_TIMEOUT_S = 60
PROXIES = ("direct", "youtube")
# 封面：记住最近解析出来的这么多个封面地址，只给这些做代理；一张至多这么大、等这么久。
THUMBS_MAX = 2000
THUMB_MAX = 2 * 1024 * 1024
THUMB_TIMEOUT_S = 10
THUMB_TYPES = ("image/jpeg", "image/png", "image/webp", "image/gif", "image/avif")
_SITE = re.compile(r"^[a-z0-9-]+(?:\.[a-z0-9-]+)+$")
# 分享文字里的网址：一定要带 http(s)://（不像 B 站那样认裸的域名，免得把随便一个词当成网站）。
_URL = re.compile(r"https?://[^\s<>\"'，。、！？；：“”‘’（）【】《》]+", re.IGNORECASE)
# 这些后缀下面一层才是站名（xxx.com.cn 的站是 xxx.com.cn）。
_SECOND_LEVEL = ("com", "net", "org", "gov", "edu", "co", "ac")
_LOCAL_SUFFIXES = (".local", ".lan", ".localhost", ".internal", ".home", ".arpa")
_BILIBILI = ("bilibili.com", "b23.tv")
_YOUTUBE = ("youtube.com", "youtu.be")
COOKIE_HEADER = "# Netscape HTTP Cookie File\n# 小鼠电视：只留了这个网站的条目。不要发给别人。\n"

# 节点归好类的错误（ytnode.web_error）→ 给人看的话。
MESSAGES = {
    "drm": "这个视频有版权加密（DRM），放不了",
    "login": "这个网站要登录：请管理员在「设置 → 网站」导入它的 cookies（可能过期了）",
    "unsupported": "这个网址不支持（只认 yt-dlp 能解析的视频网站）",
    "gone": "这个视频看不了（可能删了、私享或者要会员）",
    "geo": "这个视频有地区限制，这里放不了",
    "busy": "解析器正忙，过几秒再试",
    "slow": "网站回答太慢，稍后再试",
    "missing": "解析组件还没装好：请管理员到「设置 → YouTube」安装",
    "list": "这是一个列表（作者页或播放列表）：到「网站」页粘贴它，挑里面的视频放",
    "error": "这个网站暂时取不到，稍后再试",
}
PERMANENT = ("drm", "unsupported", "gone", "geo")


# ---- 网址 ----

def first_url(text: str) -> str:
    """分享文字里的第一个 http(s) 网址（去掉末尾的标点）；没有时空串。"""
    found = _URL.search(text or "")
    return found.group(0).rstrip(".,;:!?)]}>") if found else ""


def _host_in(host: str, domains) -> bool:
    return any(host == d or host.endswith("." + d) for d in domains)


def is_web_link(text: str) -> bool:
    """有网址，而且不是 B 站、YouTube（它们有自己的客户端）。"""
    url = first_url(text)
    if not url:
        return False
    try:
        host = (urllib.parse.urlsplit(url).hostname or "").lower()
    except ValueError:
        return False
    return bool(host) and not _host_in(host, _BILIBILI) and not _host_in(host, _YOUTUBE)


def _local_host(host: str) -> bool:
    try:
        address = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return host == "localhost" or "." not in host or host.endswith(_LOCAL_SUFFIXES)
    return not address.is_global


def safe_url(text: str) -> str | None:
    """能交给节点的网址：http/https、有主机、不是本机或内网（字面的 IP、localhost、.local 之类），不太长；
    规范化（主机小写、去掉 #…）后返回，不行就 None。不查 DNS：代理软件的 fake-IP 会把所有域名都解析成内网段。"""
    url = first_url(text) if not (text or "").lower().startswith(("http://", "https://")) else (text or "").strip()
    if not url or len(url) > URL_MAX or any(ord(ch) < 33 for ch in url):
        return None
    try:
        parts = urllib.parse.urlsplit(url)
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or not host or parts.username or parts.password \
            or _local_host(host):
        return None
    netloc = host if port is None else f"{host}:{port}"
    if ":" in host:
        netloc = f"[{host}]" if port is None else f"[{host}]:{port}"
    return urllib.parse.urlunsplit((parts.scheme.lower(), netloc, parts.path or "/", parts.query, ""))


def site_key(url: str) -> str:
    """站点的键：主机的最后两段（xxx.com.cn、xxx.co.uk 这种取三段）；看不懂时空串。"""
    try:
        host = (urllib.parse.urlsplit(url).hostname or "").lower().strip(".")
    except ValueError:
        return ""
    labels = [label for label in host.split(".") if label]
    if len(labels) < 2 or not _SITE.match(host) or labels[-1].isdigit():
        return ""
    take = 3 if len(labels) >= 3 and len(labels[-1]) == 2 and labels[-2] in _SECOND_LEVEL else 2
    return ".".join(labels[-take:])


def clean_site(text: str) -> str:
    """页面给的站名（"youku.com"）或网址 → 站点的键；不像站名时空串。"""
    text = (text or "").strip().lower()
    if "://" in text:
        return site_key(text)
    return site_key("https://" + text) if _SITE.match(text) else ""


# ---- cookies ----

class CookieError(ValueError):
    """cookies.txt 不对；消息不带文件内容。"""


def parse_cookies(text: str, site: str, now: float | None = None) -> tuple[str, int]:
    """Netscape 格式的 cookies.txt → (只留这个站的条目的新内容, 没过期的条数)。"""
    if not isinstance(text, str) or not text.strip():
        raise CookieError("文件是空的")
    if len(text.encode("utf-8", "replace")) > COOKIES_MAX:
        raise CookieError("文件太大了：只导出这个网站的 cookies 再试")
    if "\x00" in text:
        raise CookieError("这不是文本文件")
    now = time.time() if now is None else now
    kept, live, valid = [], 0, 0
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
        domain = (fields[0][len("#HttpOnly_"):] if http_only else fields[0]).lower()
        flag, path, secure, expiry, name = fields[1], fields[2], fields[3], fields[4], fields[5]
        if flag.upper() not in ("TRUE", "FALSE") or secure.upper() not in ("TRUE", "FALSE") \
                or not path.startswith("/") or not name or not re.match(r"^-?\d+$", expiry or "0"):
            continue
        valid += 1
        if not _host_in(domain.lstrip("."), (site,)):
            continue
        kept.append(line)
        expires = int(expiry or 0)
        if expires == 0 or expires > now:
            live += 1
    if not valid:
        raise CookieError("不是 Netscape 格式的 cookies.txt（用浏览器扩展「Get cookies.txt LOCALLY」之类导出）")
    if not kept:
        raise CookieError(f"里面没有 {site} 的 cookies：在浏览器里登录这个网站后再导出")
    return COOKIE_HEADER + "\n".join(kept) + "\n", live


class Settings:
    """数据目录里 websites/ 的读写：cookies/<站>.txt（0600）和 sites.json（每个站的代理）。"""

    def __init__(self, root: Path):
        self.root = Path(root)
        self._lock = threading.Lock()

    def _dir(self) -> Path:
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        return self.root

    def cookies_path(self, site: str) -> Path:
        return self.root / "cookies" / f"{site}.txt"

    def _sites(self) -> dict:
        try:
            data = json.loads((self.root / "sites.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, dict)} \
            if isinstance(data, dict) else {}

    def _write_sites(self, sites: dict) -> None:
        path = self._dir() / "sites.json"
        tmp = path.with_name("sites.json.tmp")
        tmp.write_text(json.dumps(sites, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)

    def proxy(self, site: str) -> str:
        return "youtube" if self._sites().get(site, {}).get("proxy") == "youtube" else "direct"

    def set_proxy(self, site: str, proxy: str) -> None:
        with self._lock:
            sites = self._sites()
            entry = dict(sites.get(site) or {})
            if proxy == "youtube":
                entry["proxy"] = "youtube"
            else:
                entry.pop("proxy", None)
            if entry:
                sites[site] = entry
            else:
                sites.pop(site, None)
            self._write_sites(sites)

    def save_cookies(self, site: str, text: str) -> int:
        content, count = parse_cookies(text, site)
        folder = self._dir() / "cookies"
        folder.mkdir(mode=0o700, exist_ok=True)
        path = self.cookies_path(site)
        tmp = path.with_name(path.name + ".tmp")
        try:
            tmp.unlink()
        except OSError:
            pass
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
        os.replace(tmp, path)
        return count

    def delete_cookies(self, site: str) -> bool:
        try:
            self.cookies_path(site).unlink()
            return True
        except FileNotFoundError:
            return False

    def summary(self) -> list[dict]:
        """[{site, cookies: {present, count, updated}, proxy}]：导入过 cookies 或改过代理的站，按站名排。"""
        sites = set(self._sites())
        folder = self.root / "cookies"
        try:
            sites |= {p.name[:-4] for p in folder.iterdir() if p.name.endswith(".txt")}
        except OSError:
            pass
        rows = []
        for site in sorted(s for s in sites if _SITE.match(s)):
            path = self.cookies_path(site)
            try:
                text, updated = path.read_text(encoding="utf-8", errors="replace"), path.stat().st_mtime
                try:
                    count = parse_cookies(text, site)[1]
                except CookieError:
                    count = 0
                cookies = {"present": True, "count": count, "updated": updated}
            except OSError:
                cookies = {"present": False, "count": 0, "updated": 0}
            rows.append({"site": site, "cookies": cookies, "proxy": self.proxy(site)})
        return rows


# ---- 客户端（和 youtube.YouTube 同一个本机节点）----

def _kind_of(error: NodeError) -> str:
    """节点的错误回答（{"error", "kind"}）里的归类；旧样子的回答按 HTTP 码猜。"""
    text = str(error)
    found = re.search(r'"kind":\s*"([a-z]+)"', text)
    if found and found.group(1) in MESSAGES:
        return found.group(1)
    if error.status == 503 and "tools missing" in text:
        return "missing"
    return {503: "busy", 504: "slow", 404: "gone", 400: "unsupported"}.get(error.status, "error")


def message_of(error: Exception) -> str:
    """给人看的话（SourceError 的 reason 是归类时）。"""
    return MESSAGES.get(getattr(error, "reason", "") or "", MESSAGES["error"])


class Web(YouTube):
    """其他网站的客户端，接口和 youtube.YouTube 一样（web.py 的 clients["web"]）：视频的 id 是规范化的网址。"""

    def _call(self, path: str, _timeout: float | None = None, **params) -> dict:
        if self._fetch is None:
            raise SourceError("其他网站还没配置（要 YouTube 本机模式）", reason="missing")
        try:
            answer = self._fetch(path, params, timeout=_timeout or INFO_TIMEOUT_S)
        except NodeError as error:
            kind = _kind_of(error)
            failure = SourceError(f"web {path}: {kind}", permanent=kind in PERMANENT, reason=kind)
            failure.status = error.status
            raise failure from None
        except (OSError, ValueError) as error:
            raise SourceError(f"web {path}: {type(error).__name__}", reason="error") from None
        if not isinstance(answer, dict):
            raise SourceError(f"web {path}: 回答格式不对")
        return answer

    def video_id(self, text: str) -> str | None:
        return safe_url(text)

    def info(self, url: str) -> dict:
        """/web/info：{"kind": "video", "id", "title", "duration", "thumb", "uploader", "site"} 或
        {"kind": "list", "title", "entries": [{"url", "title", "duration", "thumb"}]}。"""
        return self._call("/web/info", _timeout=INFO_TIMEOUT_S, url=url)

    def video(self, url: str) -> Video:
        answer = self.info(url)
        if answer.get("kind") != "video":
            raise SourceError(f"web {site_key(url)}: 这是列表", permanent=True, reason="list")
        try:
            return Video(bvid=url, title=str(answer.get("title") or url), duration=int(answer.get("duration") or 0),
                         pubdate=0, site="web")
        except (TypeError, ValueError):
            raise SourceError(f"web {site_key(url)}: 视频信息格式不对") from None

    def formats(self, url: str, prefetch: bool = False, fresh: bool = False,
                timeout: float = FORMATS_TIMEOUT_S) -> dict:
        params = {"url": url}
        if fresh:
            params["fresh"] = 1
        answer = self._call("/web/formats", _timeout=timeout, **params)
        if not isinstance(answer.get("formats"), list):
            raise SourceError(f"web /web/formats {site_key(url)}: 回答格式不对")
        return answer

    def open(self, url: str, offset: float) -> StreamProc:
        if self.config is None:
            raise SourceError("其他网站还没配置", reason="missing")
        request = self._request("/web/stream", {"url": url, "ss": int(offset)})
        try:
            response = self._urlopen(request, STREAM_TIMEOUT_S)
        except Exception as error:  # noqa: BLE001 —— HTTPError / OSError 都当作这次放不了
            code = getattr(error, "code", 0)
            body = ""
            try:
                body = error.read(2000).decode("utf-8", "replace") if code else ""
            except Exception:  # noqa: BLE001
                body = ""
            kind = _kind_of(NodeError(code, body)) if code else "error"
            raise SourceError(f"web stream {site_key(url)}: {kind}", permanent=kind in PERMANENT,
                              reason=kind) from None
        return StreamProc(response)

    # 下面这些 YouTube 有、网站（A1）还没有：web.py 按 None 处理。
    def creator_id(self, text: str) -> str | None:
        return None

    def series_id(self, text: str) -> str | None:
        return None


# ---- 电脑上点播（MP4、DASH；复用 ytvod 那一套，网站各走各的代理）----

class WebVod(ytvod.YtVod):
    """ytvod.YtVod 换成网站的客户端：site="web"，取字节按这个站的代理设置（直连或 YouTube 的代理）。"""

    def __init__(self, client, proxy_of, log=print, clock=time.time, fetcher=ytvod.Fetcher):
        super().__init__(client, proxy=lambda: "", log=log, clock=clock, fetcher=fetcher)
        self.proxy_of = proxy_of

    def fetcher_for(self, url: str):
        try:
            proxy = self.proxy_of(url) or ""
        except Exception:  # noqa: BLE001 —— 读不了设置就直连
            proxy = ""
        with self._lock:
            found = self._fetchers.get(proxy)
            if found is None:
                found = self._fetchers[proxy] = self._make_fetcher(proxy)
            return found

    def info(self, url: str) -> dict:
        return self.resolver.resolve(url)

    def source(self, key: tuple, video, position: float, quality: str, dash: bool):
        """MP4（H.264 + AAC 合体）或 DASH（只有画面的 H.264 + 只有声音的 AAC，带索引）时 vod.VodSource；
        别的（HLS、FLV、HEVC……）None，交给 web.py 走 HLS。取不到地址抛 SourceError。"""
        url = video.bvid
        info = self.resolver.resolve(url)
        choice, why = ytvod.choose(info.get("formats") or [], quality, dash=dash)
        if choice is None:
            return None
        resolver = self.resolver

        def resolve() -> vod.Media:
            again = resolver.resolve(url, fresh=True)
            same = ytvod.same_choice(again.get("formats") or [], choice)
            if same is None:
                raise vod.NotPlayable("格式变了")
            return ytvod.media_of(same, again)

        dashed = choice.kind == "dash"
        return vod.VodSource(key, resolve=resolve, headers=ytvod.upstream_headers(choice), label=video.title,
                             site="web", start=position,
                             duration=float(video.duration or info.get("duration") or 0),
                             first=ytvod.media_of(choice, info), quality=choice.current,
                             choices=choice.choices if dashed else (), opener=self.fetcher_for(url), dash=dashed,
                             manifest=self.manifest if dashed else None, variant=choice.variant,
                             refresh_s=max(600.0, ytvod.keep_until(info, self.clock()) - self.clock()))


def hls_input(info: dict) -> tuple[str, str, str] | None:
    """浏览器不能直接放时给中转自己的 ffmpeg 读的那一个地址：(地址, User-Agent, Referer)。
    yt-dlp 选中的是一个合体的格式（m3u8 或文件）才行；画面、声音分开的两个地址 None（交给节点的流）。"""
    formats = info.get("formats") or []
    chosen = str(info.get("format_id") or "")
    found = next((f for f in formats if str(f.get("format_id") or "") == chosen), None) if chosen else None
    if found is None:
        muxed = [f for f in formats if str(f.get("vcodec") or "none") != "none"
                 and str(f.get("acodec") or "none") != "none"]
        found = max(muxed, key=lambda f: (int(f.get("height") or 0) <= 1080, int(f.get("height") or 0)),
                    default=None)
    if found is None or str(found.get("acodec") or "") == "none" or str(found.get("vcodec") or "") == "none":
        return None
    url = str(found.get("url") or "")
    if not url.startswith(("https://", "http://")):
        return None
    headers = found.get("headers") or {}
    lowered = {str(k).lower(): str(v) for k, v in headers.items()}
    return url, lowered.get("user-agent", ""), lowered.get("referer", "")


# ---- 接到中转上 ----

def settings_of(relay) -> Settings | None:
    data = getattr(relay, "selfhost_data", None)
    if data is None:
        return None
    found = getattr(relay, "_web_settings", None)
    if found is None or found.root != Path(data) / "websites":
        found = relay._web_settings = Settings(Path(data) / "websites")
    return found


def attach(relay) -> None:
    """本机 YouTube 模式时给中转加上 clients["web"] 和 webvod（__main__ 在设好 youtube_kind 之后调用）。"""
    youtube = relay.clients.get("youtube")
    if getattr(relay, "youtube_kind", "") != "local" or youtube is None or not getattr(youtube, "config", None):
        return
    relay.clients["web"] = Web(config=dict(youtube.config))
    if getattr(relay, "vod", None) is not None:
        relay.webvod = WebVod(lambda: relay.clients.get("web"), proxy_of=lambda url: proxy_url(relay, url),
                              log=lambda m: relay.log(m))


def proxy_url(relay, url: str) -> str:
    """这个网址的站取字节走哪个代理：设置成 YouTube 代理的站给它的地址，别的空串（直连）。"""
    settings = settings_of(relay)
    if settings is None or settings.proxy(site_key(url)) != "youtube":
        return ""
    return relay.youtube_proxy()


def _admin(handler, relay) -> bool:
    if relay.selfhost is None or getattr(relay, "selfhost_data", None) is None:
        return False
    caller = handler._caller()
    return caller is not None and caller.role == "admin" and not caller.guest


def _family(handler, relay) -> bool:
    caller = handler._caller()
    return caller is not None and not caller.guest and caller.user is not None


def _read_body(handler) -> dict | None:
    try:
        length = int(handler.headers.get("Content-Length") or 0)
    except ValueError:
        return None
    if not 0 < length <= BODY_MAX:
        if length > BODY_MAX:
            handler.close_connection = True
        return None
    try:
        handler._read_body = True
        body = json.loads(handler.rfile.read(length))
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


def _info(relay, query: dict) -> dict:
    client = relay.clients.get("web")
    if client is None:
        return {"ok": False, "error": "其他网站要在「设置 → YouTube」开本机模式才能用"}
    url = safe_url(query.get("url", ""))
    if url is None:
        return {"ok": False, "error": "请粘贴 http:// 或 https:// 开头的视频网址（本机、局域网的地址不收）"}
    try:
        answer = client.info(url)
    except SourceError as error:
        relay.log(f"网站 {site_key(url)} 解析失败（{getattr(error, 'reason', '') or 'error'}）")
        return {"ok": False, "error": message_of(error), "kind": getattr(error, "reason", "") or "error"}
    thumbs = thumbs_of(relay)
    answer = dict(answer, ok=True, url=url, site_key=site_key(url))
    answer["thumb"] = thumbs.add(answer.get("thumb") or "", url)
    if answer.get("kind") == "list":
        entries = []
        for e in answer.get("entries") or []:
            link = safe_url(str(e.get("url") or ""))
            if link:
                entries.append(dict(e, url=link, thumb=thumbs.add(e.get("thumb") or "", link)))
        answer["entries"] = entries
    return answer


class Thumbs:
    """解析时见过的封面地址（LRU）；/api/websites/thumb 只取这些。"""

    def __init__(self, size: int = THUMBS_MAX):
        self.size = size
        self._seen: collections.OrderedDict[str, str] = collections.OrderedDict()
        self._lock = threading.Lock()

    def add(self, url: str, page: str) -> str:
        """记下一个封面（page：它所属的网页，按那个站的代理取），返回页面用的地址；不能用的给空串。"""
        url = safe_url(url) if isinstance(url, str) and url.lower().startswith(("http://", "https://")) else None
        if url is None:
            return ""
        with self._lock:
            self._seen[url] = page
            self._seen.move_to_end(url)
            while len(self._seen) > self.size:
                self._seen.popitem(last=False)
        return "api/websites/thumb?" + urllib.parse.urlencode({"u": url})

    def page_of(self, url: str) -> str | None:
        with self._lock:
            return self._seen.get(url)


def thumbs_of(relay) -> Thumbs:
    found = getattr(relay, "_web_thumbs", None)
    if found is None:
        found = relay._web_thumbs = Thumbs()
    return found


def fetch_thumb(url: str, proxy: str = "", opener=None) -> tuple[bytes, str] | None:
    """取一张封面：(字节, 类型)；不是图片、太大、取不到时 None。直连时不走环境变量和系统的代理。"""
    if opener is None:
        handlers = urllib.request.ProxyHandler({"http": proxy, "https": proxy} if proxy else {})
        opener = urllib.request.build_opener(handlers).open
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "image/*"})
    try:
        with opener(request, timeout=THUMB_TIMEOUT_S) as answer:
            kind = str(answer.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
            if kind not in THUMB_TYPES:
                return None
            data = answer.read(THUMB_MAX + 1)
    except (OSError, ValueError, urllib.error.URLError):
        return None
    return (data, kind) if 0 < len(data) <= THUMB_MAX else None


def _thumb(handler, relay, query: dict) -> None:
    url = query.get("u", "")
    page = thumbs_of(relay).page_of(url)
    if page is None:
        handler._not_found()
        return
    found = fetch_thumb(url, proxy_url(relay, page))
    if found is None:
        handler._not_found()
        return
    data, kind = found
    handler.send_response(200)
    handler.send_header("Content-Type", kind)
    handler.send_header("Content-Length", str(len(data)))
    handler.send_header("Cache-Control", "private, max-age=86400")
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.end_headers()
    handler.wfile.write(data)


def get(handler, relay, path: str) -> bool:
    if not path.startswith(PREFIX):
        return False
    name = path[len(PREFIX):]
    if name in ("info", "thumb"):
        if not _family(handler, relay) or relay.clients.get("web") is None:
            handler._not_found()
            return True
        query = {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlsplit(handler.path).query).items()}
        if name == "thumb":
            _thumb(handler, relay, query)
        else:
            handler._json(_info(relay, query))
        return True
    if name == "settings":
        settings = settings_of(relay)
        if not _admin(handler, relay) or settings is None:
            handler._not_found()
            return True
        handler._json({"ok": True, "on": relay.clients.get("web") is not None, "sites": settings.summary()})
        return True
    return False


def post(handler, relay, path: str) -> bool:
    if not path.startswith(PREFIX) or path[len(PREFIX):] not in ("cookies", "cookies/delete", "proxy"):
        return False
    name = path[len(PREFIX):]
    body = _read_body(handler)
    settings = settings_of(relay)
    if not _admin(handler, relay) or settings is None:
        handler._not_found()
        return True
    if body is None:
        handler._json({"ok": False, "error": "请求格式不对（cookies 文件太大时也会这样）"}, 400)
        return True
    site = clean_site(str(body.get("site") or body.get("url") or ""))
    if not site:
        handler._json({"ok": False, "error": "请填网站的网址或域名（比如 youku.com）"})
        return True
    if name == "cookies":
        try:
            count = settings.save_cookies(site, body.get("text"))
        except CookieError as error:
            handler._json({"ok": False, "error": str(error)})
            return True
        except OSError as error:
            handler._json({"ok": False, "error": f"存不了 cookies：{type(error).__name__}"})
            return True
        relay.log(f"导入了 {site} 的 cookies（{count} 条）")
    elif name == "cookies/delete":
        if settings.delete_cookies(site):
            relay.log(f"删除了 {site} 的 cookies")
    else:
        proxy = body.get("proxy")
        if proxy not in PROXIES:
            handler._json({"ok": False, "error": "请求格式不对"})
            return True
        settings.set_proxy(site, proxy)
        relay.log(f"{site} 改成{'走 YouTube 的代理' if proxy == 'youtube' else '直连'}")
    handler._json({"ok": True, "sites": settings.summary()})
    return True
