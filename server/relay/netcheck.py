"""「网络检查」（总后台 → 系统，自建 / 电脑客户端）：网盘、直播、网站慢的时候，是不是代理软件绕了远路。

起因：电脑上开着 Clash Verge（TUN 模式，fake-ip 198.18.0.0/15，规则最后是 MATCH,PROXY），夸克的下载地址
不在任何 DIRECT 规则里，于是网盘组件取片子走了国外的代理节点，片库放得一卡一卡（11.5 Mbit/s）；给夸克加上
DIRECT 规则以后 65 Mbit/s。这里把这件事查出来、用大白话说清楚，并给出要加的规则——只给建议，从不改代理软件的配置。

    GET  /api/admin/netcheck    上次的结果（内存里，没测过时 null）、在不在测、测到哪一步
    POST /api/admin/netcheck    开始测（在后台线程里，同时只有一次；页面每 1.5 秒问一次 GET）

测这几样：

1. 网盘（网盘组件 OpenList 里加的每个网盘，clouddrive.py）：挑一个片库里有的、这个网盘上的文件（片库还没扫到
   就向 OpenList 列一两层目录找一个），向 OpenList 要它的下载地址（/api/fs/link，管理员才能调；取不到再看
   /api/fs/get 的 raw_url），只留下主机名——地址里有签名，不进结果、不进日志。再看这个主机名的 DNS 回答是不是
   198.18.0.0/15 里的假 IP（是的话，走哪条线由代理软件的规则决定），然后照片库的路（OpenList 的 /dav，
   library.WebDav，本地代理）用 Range 读 DRIVE_S 秒、至多 DRIVE_CAP 字节，量第一个字节多久到、每秒多少。
   慢（低于 SLOW_MBPS）又是假 IP 的，给出要加的 DIRECT 规则（DOMAIN-SUFFIX,quark.cn,DIRECT 这样的）。
2. 直播源：频道表里央视、卫视的头 LIVE_COUNT 个台（上次放得起来的那条线路，没有就是主线路），和「检测线路」
   一样只取列表和一个片段的头几 KB（routecheck.probe），量用了多久，看主机名是不是假 IP。
3. 网站：YouTube（经「设置 → YouTube」里填的代理，没填就和别的一样）、B 站、豆瓣各一个公开地址，短超时，
   回答了就算通（403 也算：连上了），看是不是经系统 / 环境变量里的代理、DNS 是不是假 IP。
4. 可选：读 Clash Verge 的配置（CLASH_PATH，只读），只取 rules: 那一段，看这些域名有没有 DIRECT 规则、
   会落到哪一条；文件里别的（订阅地址、secret、节点）一概不留。没装、读不了就跳过。

结果只有主机名、毫秒、Mbit/s 和大白话，从不带地址、令牌、代理的用户名密码；日志只写一行做完了、几项要注意。
上次的结果存在内存里（中转重启就没了，再点一次就是）。云端的中转没有这个（relay.selfhost 为 None）。
"""

from __future__ import annotations

import http.client
import ipaddress
import os
import re
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .. import channel_groups, routes

NOT_HERE = "只有家庭服务器和电脑客户端有网络检查"
FAKE_NET = ipaddress.IPv4Network("198.18.0.0/15")
# 网盘测速：读这么久（从第一个字节算起）、至多这么多；低于 SLOW_MBPS 算慢（1080p 的片子要十几 Mbit/s，
# 4K 和高码率的要二三十）。读到的少于 SAMPLE_MIN 时说「文件太小，测不准」。
DRIVE_S = 5.0
DRIVE_CAP = 30 * 1024 * 1024
DRIVE_TIMEOUT_S = 10.0
SLOW_MBPS = 25.0
SAMPLE_MIN = 1024 * 1024
CHUNK = 64 * 1024
# 片库还没扫到某个网盘时，向 OpenList 列目录找一个文件：至多这么多层、这么多次。
LIST_DEPTH = 3
LIST_CALLS = 12
LIST_PAGE = 100
# 直播：测几个台、多久算慢（取列表加一个片段的头 16 KB）。
LIVE_COUNT = 3
LIVE_SLOW_MS = 3000
LIVE_GROUPS = ("央视", "卫视")
# 网站：超时、多久算慢、读回答的头多少字节。
WEB_TIMEOUT_S = 6.0
WEB_SLOW_MS = 2000
WEB_READ = 4096
UA = "Mozilla/5.0 RooTV-netcheck"
# (键, 页面上的名字, 地址, 这家的域名——建议 DIRECT 规则时用)。YouTube 本来就要经代理，不建议 DIRECT。
WEB_TARGETS = (
    ("youtube", "YouTube", "https://www.youtube.com/generate_204", ()),
    ("bilibili", "B 站", "https://api.bilibili.com/x/web-interface/nav", ("bilibili.com", "bilivideo.com", "hdslb.com")),
    ("douban", "豆瓣", "https://movie.douban.com/favicon.ico", ("douban.com", "doubanio.com")),
)
# 每种网盘的下载地址常用的域名（OpenList 的驱动名 → 后缀），和实际取到的主机名一起给成 DIRECT 规则。
DRIVE_SUFFIXES = {"QuarkTV": ("quark.cn",), "Quark": ("quark.cn",), "QuarkOpen": ("quark.cn",),
                  "UCTV": ("uc.cn",), "UC": ("uc.cn",), "BaiduNetdisk": ("baidupcs.com",),
                  "BaiduPhoto": ("baidupcs.com",)}
# Clash Verge Rev 在 macOS 上的运行配置（它把订阅、合并、脚本处理完以后真正交给内核的那份）。
CLASH_PATH = Path.home() / "Library" / "Application Support" / "io.github.clash-verge-rev.clash-verge-rev" / "clash-verge.yaml"
CLASH_MAX = 8 * 1024 * 1024
RULES_MAX = 100000
# 规则里这里能对上的种类；别的（GEOSITE、RULE-SET、GEOIP、AND……）这里判断不了，排在它们后面的结论就「不一定」。
_DOMAIN_KINDS = ("DOMAIN", "DOMAIN-SUFFIX", "DOMAIN-KEYWORD")
_SECOND_LEVEL = {"com", "net", "org", "gov", "edu", "co", "ac"}
ADVICE = ("在 Clash Verge 里给正在用的订阅加「前置规则」（要排在最后的 MATCH 前面），加完不用重启；"
          "小鼠电视不会去改代理软件的配置。")


def _ip(host: str):
    try:
        return ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return None


def system_resolve(host: str) -> list[str]:
    """系统的 DNS 回答（和网盘组件、ffmpeg 看到的一样）：IPv4 和 IPv6 地址，去重。"""
    found = []
    for item in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM):
        address = item[4][0]
        if address not in found:
            found.append(address)
    return found


def dns_of(host: str, resolve=system_resolve) -> dict:
    """{fake: True / False / None（解析不了）, ip: 这个主机名直接是 IP}。"""
    literal = _ip(host)
    if literal is not None:
        return {"fake": literal.version == 4 and literal in FAKE_NET, "ip": True}
    try:
        addresses = resolve(host)
    except (OSError, UnicodeError, ValueError):
        return {"fake": None, "ip": False}
    if not addresses:
        return {"fake": None, "ip": False}
    fake = False
    for address in addresses:
        parsed = _ip(address)
        if parsed is not None and parsed.version == 4 and parsed in FAKE_NET:
            fake = True
    return {"fake": fake, "ip": False}


def suffix_of(host: str) -> str:
    """主机名的「主域名」：video-play.quark.cn → quark.cn，a.b.example.com.cn → example.com.cn。"""
    labels = [p for p in host.lower().strip(".").split(".") if p]
    if len(labels) >= 3 and labels[-2] in _SECOND_LEVEL and len(labels[-1]) == 2:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def direct_rules(hosts, suffixes=()) -> list[str]:
    """要加的 DIRECT 规则：这家的域名、实际主机名的主域名；直接是 IP 的用 IP-CIDR。"""
    rules = []
    for suffix in suffixes:
        rule = f"DOMAIN-SUFFIX,{suffix},DIRECT"
        if rule not in rules:
            rules.append(rule)
    for host in hosts:
        if not host:
            continue
        literal = _ip(host)
        if literal is not None:
            rule = f"IP-CIDR,{literal}/32,DIRECT,no-resolve" if literal.version == 4 else f"IP-CIDR6,{literal}/128,DIRECT,no-resolve"
        else:
            rule = f"DOMAIN-SUFFIX,{suffix_of(host)},DIRECT"
        if rule not in rules:
            rules.append(rule)
    return rules


# ---------------------------------------------------------------- Clash 的规则（只读，只取 rules:）

def parse_rules(text: str) -> list[str]:
    """YAML 里顶层 rules: 下面的列表（一行一条的写法，Clash Verge 生成的就是这样）。别的键一概不看。"""
    rules, inside = [], False
    for raw in text.splitlines():
        if not inside:
            if re.match(r"^rules:\s*(#.*)?$", raw):
                inside = True
            continue
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        if raw[0] not in " \t-":
            break                               # 下一个顶层的键
        match = re.match(r"^\s*-\s*(.+?)\s*$", raw)
        if match is None:
            continue
        item = match.group(1)
        if item[:1] in "'\"":
            item = item[1:item.find(item[0], 1)] if item.find(item[0], 1) > 0 else item[1:]
        else:
            item = re.sub(r"\s+#.*$", "", item)
        item = item.strip()
        if item:
            rules.append(item)
        if len(rules) >= RULES_MAX:
            break
    return rules


def load_rules(path: Path | None) -> list[str] | None:
    """Clash Verge 的规则；没有这个文件、读不了、太大时 None。"""
    if path is None:
        return None
    try:
        if not Path(path).is_file() or Path(path).stat().st_size > CLASH_MAX:
            return None
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    return parse_rules(text)


def route_of(rules: list[str], host: str) -> dict:
    """这个主机名按规则从上往下落到哪一条：{policy, rule, unsure（前面有判断不了的规则）}；没落到 policy 为 ""。"""
    host = host.lower().strip(".")
    literal = _ip(host)
    unsure = False
    for rule in rules:
        parts = [p.strip() for p in rule.split(",")]
        kind = parts[0].upper()
        if kind == "MATCH" and len(parts) >= 2:
            return {"policy": parts[1], "rule": f"MATCH,{parts[1]}", "unsure": unsure}
        if len(parts) < 3:
            continue
        value, policy = parts[1].lower().strip("."), parts[2]
        hit = None
        if kind in _DOMAIN_KINDS:
            if literal is not None:
                continue
            hit = (host == value) if kind == "DOMAIN" else \
                (host == value or host.endswith("." + value)) if kind == "DOMAIN-SUFFIX" else value in host
        elif kind in ("IP-CIDR", "IP-CIDR6") and literal is not None:
            try:
                hit = literal in ipaddress.ip_network(value, strict=False)
            except ValueError:
                continue
        elif literal is None and "no-resolve" in (p.lower() for p in parts[3:]):
            continue                            # IP 类的规则带 no-resolve：域名不会拿去解析了对它
        else:
            unsure = True
            continue
        if hit:
            return {"policy": policy, "rule": f"{kind},{value},{policy}", "unsure": unsure}
    return {"policy": "", "rule": "", "unsure": unsure}


def _direct(policy: str) -> bool:
    return policy.upper() == "DIRECT"


# ---------------------------------------------------------------- 代理

def _hostport(proxy: str) -> str:
    """代理地址只留 主机:端口（不带用户名密码）。"""
    parts = urllib.parse.urlsplit(proxy if "://" in proxy else "http://" + proxy)
    try:
        port = parts.port
    except ValueError:
        port = None
    host = parts.hostname or ""
    return f"{host}:{port}" if host and port else host


def system_proxy(url: str) -> str:
    """urllib 去这个地址时会经的代理（环境变量、macOS 的系统代理）：主机:端口，没有 ""。B 站、豆瓣、直播的检测都这么走。"""
    parts = urllib.parse.urlsplit(url)
    try:
        proxies = urllib.request.getproxies()
        found = proxies.get(parts.scheme) or proxies.get("all")
        if not found or urllib.request.proxy_bypass(parts.hostname or ""):
            return ""
    except (OSError, ValueError):
        return ""
    return _hostport(found)


def env_proxy(url: str, environ=None) -> str:
    """只看环境变量（网盘组件是 Go 写的，只认环境变量里的代理，不认 macOS 的系统代理）。"""
    environ = os.environ if environ is None else environ
    parts = urllib.parse.urlsplit(url)
    found = ""
    for key in (f"{parts.scheme}_proxy", "all_proxy"):
        found = environ.get(key) or environ.get(key.upper()) or ""
        if found:
            break
    if not found:
        return ""
    bypass = (environ.get("no_proxy") or environ.get("NO_PROXY") or "").split(",")
    host = (parts.hostname or "").lower()
    for item in (b.strip().lower().lstrip("*") for b in bypass):
        if item == "*" or (item and (host == item.lstrip(".") or host.endswith("." + item.lstrip(".")))):
            return ""
    return _hostport(found)


class _Tunneled(http.client.HTTPSConnection):
    """经「设置 → YouTube」里填的代理（http:// 或 socks5://，ytnode.open_tunnel）连过去的 HTTPS。"""

    def __init__(self, host: str, port: int, proxy, timeout: float, tls: bool):
        super().__init__(host, port, timeout=timeout)
        self._via, self._tls = proxy, tls

    def connect(self):
        from .ytnode import open_tunnel
        sock = open_tunnel(self._via, self.host, self.port, timeout=self.timeout)
        sock.settimeout(self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host) if self._tls else sock


def _web_error(error: BaseException) -> str:
    """连网站出错 → 大白话（不带地址）。"""
    reason = error.reason if isinstance(error, urllib.error.URLError) else error
    text = str(reason)
    if isinstance(reason, TimeoutError) or "timed out" in text:
        return "超时（%d 秒内没回答）" % int(WEB_TIMEOUT_S)
    if isinstance(reason, ConnectionRefusedError) or "refused" in text:
        return "连接被拒绝"
    if isinstance(reason, ConnectionResetError) or "reset" in text:
        return "连接被重置（常见于被墙或代理断开）"
    if isinstance(reason, socket.gaierror) or "nodename" in text or "Name or service" in text:
        return "域名解析不了"
    if isinstance(reason, ssl.SSLError) or "SSL" in text or "certificate" in text:
        return "TLS 握手失败"
    return f"连不上（{type(reason).__name__}）"


def fetch_status(url: str, proxy_text: str = "", opener=None, timeout: float = WEB_TIMEOUT_S) -> int:
    """GET 这个地址，读回答的头几 KB：HTTP 状态码（4xx 也照样返回——连上了）。连不上抛 OSError。
    proxy_text：经这个代理（YouTube 的设置）；空的时候用 opener（默认跟着环境变量和系统代理走，和中转平时一样）。"""
    if proxy_text:
        from .ytnode import parse_proxy
        proxy = parse_proxy(proxy_text)
        parts = urllib.parse.urlsplit(url)
        tls = parts.scheme == "https"
        connection = _Tunneled(parts.hostname or "", parts.port or (443 if tls else 80), proxy, timeout, tls)
        try:
            path = parts.path or "/"
            connection.request("GET", path + ("?" + parts.query if parts.query else ""),
                               headers={"User-Agent": UA, "Accept-Encoding": "identity"})
            response = connection.getresponse()
            response.read(WEB_READ)
            return response.status
        except http.client.HTTPException as error:
            raise OSError(type(error).__name__) from None
        finally:
            connection.close()
    request = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Encoding": "identity"})
    try:
        with (opener or urllib.request.urlopen)(request, timeout=timeout) as response:
            response.read(WEB_READ)
            return getattr(response, "status", None) or response.getcode()
    except urllib.error.HTTPError as error:
        error.close()
        return error.code
    except http.client.HTTPException as error:
        raise OSError(type(error).__name__) from None


# ---------------------------------------------------------------- 一行结果

def _row(state: str, name: str, text: str, notes=None, rules=None) -> dict:
    """state：ok ✓、warn ⚠、skip 跳过（只是说一声）。"""
    return {"state": state, "name": name, "text": text, "notes": [n for n in (notes or []) if n], "rules": list(rules or [])}


def _route_note(clash: list[str] | None, host: str) -> tuple[str, dict | None]:
    """(Clash 的规则怎么说, route_of 的结果)；没读到规则时 ("", None)。"""
    if clash is None or not host:
        return "", None
    found = route_of(clash, host)
    if not found["policy"]:
        return ("Clash 的规则里没有对上 " + host + " 的" + ("（前面有规则集，可能被它接住）" if found["unsure"] else "")), found
    maybe = "（前面有规则集、GEOIP 这类，可能先被它们接住）" if found["unsure"] else ""
    if _direct(found["policy"]):
        return f"Clash 的规则里 {host} 落在「{found['rule']}」：直连{maybe}", found
    return f"Clash 的规则里 {host} 落在「{found['rule']}」：走代理「{found['policy']}」{maybe}", found


def _fake_note(dns: dict, host: str) -> str:
    if dns["fake"]:
        return f"{host} 的 DNS 回答是 198.18 开头的假 IP：电脑上的代理软件开着 TUN（fake-ip），走哪条线由它的规则决定"
    if dns["fake"] is None:
        return f"{host} 的域名现在解析不了"
    if dns["ip"]:
        return ""
    return f"{host} 的 DNS 回答是真实地址：没经过代理软件的 fake-ip"


def _mbps(count: int, seconds: float) -> float:
    return round(count * 8 / seconds / 1e6, 1) if seconds > 0 else 0.0


# ---------------------------------------------------------------- 网盘

class _Stop(Exception):
    pass


def _pick(relay, mount: str) -> dict | None:
    """片库里这个网盘上的一个文件 {path, size}：优先至少 DRIVE_CAP 大的里最小的，不然最大的。"""
    library = getattr(relay, "library", None)
    managed = library.managed() if library is not None else None
    if managed is None:
        return None
    prefix = mount.strip("/") + "/"
    found = [i for i in library.items() if i.get("source") == managed["id"] and str(i.get("path") or "").startswith(prefix)
             and isinstance(i.get("size"), int) and i["size"] > 0]
    if not found:
        return None
    big = [i for i in found if i["size"] >= DRIVE_CAP]
    chosen = min(big, key=lambda i: i["size"]) if big else max(found, key=lambda i: i["size"])
    return {"path": chosen["path"], "size": chosen["size"]}


def _listed(client, mount: str) -> dict | None:
    """片库还没扫到：向 OpenList 一层一层列（至多 LIST_DEPTH 层、LIST_CALLS 次），找一个最大的文件。"""
    from .library import VIDEO_TYPES
    queue, calls, best = ["/" + mount.strip("/")], 0, None
    while queue and calls < LIST_CALLS:
        folder = queue.pop(0)
        calls += 1
        answer = client.call("POST", "/api/fs/list", {"path": folder, "page": 1, "per_page": LIST_PAGE, "refresh": False})
        data = answer.get("data") if isinstance(answer.get("data"), dict) else {}
        content = data.get("content") if answer.get("code") == 200 else None
        for entry in content if isinstance(content, list) else []:
            if not isinstance(entry, dict) or not isinstance(entry.get("name"), str) or "/" in entry["name"]:
                continue
            path = folder.rstrip("/") + "/" + entry["name"]
            if entry.get("is_dir"):
                if path.count("/") - mount.strip("/").count("/") - 1 < LIST_DEPTH:
                    queue.append(path)
                continue
            size = entry.get("size") if isinstance(entry.get("size"), int) else 0
            video = os.path.splitext(entry["name"])[1].lower() in VIDEO_TYPES
            score = (video, size)
            if size > 0 and (best is None or score > best[0]):
                best = (score, {"path": path.lstrip("/"), "size": size})
        if best is not None and best[0][0] and best[1]["size"] >= DRIVE_CAP:
            break
    return best[1] if best else None


def _loopback(host: str) -> bool:
    if host in ("localhost", ""):
        return True
    literal = _ip(host)
    return literal is not None and (literal.is_loopback or literal.is_unspecified)


def link_host(client, path: str) -> str:
    """OpenList 给这个文件的下载地址的主机名（只留主机名，地址本身马上扔掉）；取不到 ""。"""
    for api, key in (("/api/fs/link", "url"), ("/api/fs/get", "raw_url")):
        try:
            answer = client.call("POST", api, {"path": "/" + path.lstrip("/")})
        except Exception:                       # noqa: BLE001 —— 取不到主机名不影响测速
            continue
        data = answer.get("data") if isinstance(answer.get("data"), dict) else {}
        url = data.get(key) if answer.get("code") == 200 else None
        if isinstance(url, str) and url:
            host = (urllib.parse.urlsplit(url).hostname or "").lower()
            if not _loopback(host):
                return host
    return ""


def measure(dav, path: str, size: int, seconds: float = DRIVE_S, cap: int = DRIVE_CAP,
            monotonic=time.monotonic) -> dict:
    """照片库的路读这个文件：{bytes, first_ms, mbps, host（跟着跳转到了别的主机时）, error}。"""
    from .library import LibraryError, _dav_reason
    start = size // 4 if size > 2 * cap else 0
    end = min(size, start + cap) - 1
    began = monotonic()
    try:
        answer = dav.open(path, f"bytes={start}-{end}", timeout=DRIVE_TIMEOUT_S)
    except urllib.error.HTTPError as error:
        error.close()
        return {"error": _dav_reason(error)}
    except LibraryError as error:
        return {"error": str(error)}
    except (OSError, ValueError) as error:
        return {"error": _dav_reason(error)}
    final = (urllib.parse.urlsplit(answer.geturl()).hostname or "").lower() if hasattr(answer, "geturl") else ""
    count, first = 0, None
    try:
        with answer:
            read = getattr(answer, "read1", None) or answer.read
            while count < cap:
                chunk = read(min(CHUNK, cap - count))
                if not chunk:
                    break
                if first is None:
                    first = monotonic()
                count += len(chunk)
                if monotonic() - first >= seconds:
                    break
    except (OSError, http.client.HTTPException) as error:
        if first is None:
            return {"error": _dav_reason(error) if isinstance(error, OSError) else "网盘中途断开了"}
    if first is None:
        return {"error": "网盘没给数据"}
    took = monotonic() - first
    return {"bytes": count, "first_ms": max(1, int((first - began) * 1000)), "mbps": _mbps(count, took),
            "host": "" if _loopback(final) else final}


# ---------------------------------------------------------------- 检查

class NetCheck:
    """一次检查的全部依赖（测试换成假的）：resolve 解析域名，opener 是 urllib 的打开函数（直播和网站用），
    proxies(url) / env(url) 说经不经代理，clash_path 是 Clash Verge 的配置，targets 是要测的网站。"""

    def __init__(self, resolve=system_resolve, opener=None, proxies=system_proxy, env=env_proxy,
                 clash_path: Path | None = CLASH_PATH, targets=WEB_TARGETS, monotonic=time.monotonic, clock=time.time,
                 drive_s: float = DRIVE_S, drive_cap: int = DRIVE_CAP, slow_mbps: float = SLOW_MBPS):
        self.resolve, self.opener, self.proxies, self.env = resolve, opener, proxies, env
        self.clash_path, self.targets, self.monotonic, self.clock = clash_path, targets, monotonic, clock
        self.drive_s, self.drive_cap, self.slow_mbps = drive_s, drive_cap, slow_mbps
        self.lock = threading.Lock()
        self.running = False
        self.step = ""
        self.last: dict | None = None

    # ---- 网盘 ----

    def drives(self, relay, clash) -> list[dict]:
        from ..selfhost import openlist
        from . import clouddrive
        from .library import WebDav
        try:
            client = clouddrive.api(relay)
            storages = client.storages()
        except clouddrive.DriveError as error:
            return [_row("skip", "网盘组件", str(error) + "，跳过网盘的测速")]
        storages.sort(key=lambda s: (s.get("order") or 0, s.get("id") or 0))
        if not storages:
            return [_row("skip", "网盘组件", "还没有加网盘，跳过")]
        dav = WebDav(f"http://127.0.0.1:{client.port}/dav", openlist.USERNAME, client.password)
        rows = []
        for storage in storages:
            mount = storage["mount_path"]
            name = mount.strip("/") or "/"
            driver = str(storage.get("driver") or "")
            kind = clouddrive.KINDS.get(driver, driver)
            label = f"{name}（{kind}）" if kind and kind != name else name
            state, _reason = clouddrive._state(storage)
            if state == "off":
                rows.append(_row("skip", label, "在网盘管理里停用了，跳过"))
                continue
            if state != "ok":
                rows.append(_row("warn", label, "这个网盘现在" + clouddrive.LABELS[state] + "：先在「片库 → 网盘」里重新登录再测"))
                continue
            self.step = f"正在测网盘「{name}」的速度…"
            target = _pick(relay, mount)
            if target is None:
                try:
                    target = _listed(client, mount)
                except clouddrive.DriveError:
                    target = None
            if target is None:
                rows.append(_row("skip", label, "这个网盘里没找到文件，没法测速"))
                continue
            host = link_host(client, target["path"])
            speed = measure(dav, target["path"], target["size"], self.drive_s, self.drive_cap, self.monotonic)
            host = host or speed.get("host") or ""
            rows.append(self._drive_row(label, driver, host, speed, clash))
        return rows

    def _drive_row(self, label: str, driver: str, host: str, speed: dict, clash) -> dict:
        notes = []
        if host:
            notes.append(f"下载地址的主机：{host}")
        else:
            notes.append("网盘组件没给出下载地址的主机名（这个网盘可能不给直链），只量了速度")
        # 没取到主机名时，拿这家网盘的域名看 DNS 和规则（夸克就是 quark.cn）。
        probe = host or (DRIVE_SUFFIXES.get(driver) or ("",))[0]
        dns = dns_of(probe, self.resolve) if probe else {"fake": None, "ip": False}
        if probe:
            notes.append(_fake_note(dns, probe))
        rule_text, found = _route_note(clash, probe)
        notes.append(rule_text)
        via = self.env("https://" + probe) if probe else ""
        if via:
            notes.append(f"环境变量里有代理 {via}：网盘组件取片子时也会经它")
        if speed.get("error"):
            return _row("warn", label, "测不了速度：" + speed["error"], notes)
        mbps, first = speed["mbps"], speed["first_ms"]
        text = f"{mbps} Mbit/s，第一个字节 {first} 毫秒"
        if speed["bytes"] < SAMPLE_MIN:
            return _row("skip", label, text + "（文件太小，测不准）", notes)
        if mbps >= self.slow_mbps:
            if dns["fake"] and found is not None and found["policy"] and not _direct(found["policy"]):
                notes.append("现在够快；要是哪天变慢，先看代理节点")
            return _row("ok", label, text, notes)
        rules = []
        if dns["fake"] or via:
            if found is not None and _direct(found["policy"]) and not found["unsure"]:
                say = "偏慢；Clash 的规则已经让它直连，慢的原因不在代理软件（可能是网盘限速，或家里的网络慢）。"
            else:
                rules = direct_rules([host] if host else [], DRIVE_SUFFIXES.get(driver, ()))
                say = "偏慢：很可能经代理软件绕到了国外的节点。在代理软件里给它加直连规则（见下面），再测一次。"
        elif dns["fake"] is None:
            say = "偏慢；判断不了是不是经了代理软件。"
        else:
            say = "偏慢：没经过代理软件，可能是网盘限速（不是会员）或家里的网络本来就慢。"
        return _row("warn", label, text + "。" + say, notes, rules)

    # ---- 直播 ----

    def _live_pick(self, path: Path) -> list[dict]:
        from . import channeltable
        rows = channeltable.read(path)["rows"]
        first = [r for r in rows if channel_groups.classify(r["id"], r["name"]) in LIVE_GROUPS]
        rest = [r for r in rows if r not in first]
        picked, hosts = [], set()
        for row in first + rest:                 # 先挑主机不同的
            host = (urllib.parse.urlsplit(row["url"]).hostname or "").lower()
            if host and host not in hosts and len(picked) < LIVE_COUNT:
                picked.append(row)
                hosts.add(host)
        for row in first + rest:
            if len(picked) >= LIVE_COUNT:
                break
            if row not in picked and urllib.parse.urlsplit(row["url"]).hostname:
                picked.append(row)
        return picked

    def live(self, relay, clash) -> list[dict]:
        from . import routecheck
        path = getattr(relay, "main_channels", None)
        if path is None:
            return [_row("skip", "直播", "没有频道表，跳过")]
        chosen = self._live_pick(path)
        if not chosen:
            return [_row("skip", "直播", "频道表里还没有台，跳过")]
        self.step = "正在测直播源…"
        results: list[dict | None] = [None] * len(chosen)

        def one(n: int, row: dict) -> None:
            lines = routes.table_routes(path, row["id"], row["url"], row["agent"], routecheck.DEFAULT_USER_AGENT)
            good = routes.good_of(path, row["id"])
            url, agent = next(((u, a) for u, a in lines if good and routes.fingerprint(u) == good), lines[0])
            found = routecheck.probe(url, agent, self.opener or urllib.request.urlopen, self.monotonic)
            results[n] = self._live_row(row["name"], url, found, clash)

        threads = [threading.Thread(target=one, args=(n, row), daemon=True) for n, row in enumerate(chosen)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(routecheck.TOTAL_S + 5)
        return [r or _row("warn", chosen[n]["name"], "检测没在时间内做完") for n, r in enumerate(results)]

    def _live_row(self, name: str, url: str, found: dict, clash) -> dict:
        host = (urllib.parse.urlsplit(url).hostname or "").lower()
        dns = dns_of(host, self.resolve)
        via = self.proxies(url)
        notes = [f"源站：{host}"]
        if via:
            notes.append(f"经代理 {via}（系统或环境变量里设的）")
        elif dns["ip"]:
            notes.append("地址直接是 IP：开着 TUN 的代理软件按 IP 规则（比如 GEOIP,CN）决定走哪条线")
        else:
            notes.append(_fake_note(dns, host))
        rule_text, found_rule = _route_note(clash, host)
        notes.append(rule_text)
        if not found["ok"]:
            return _row("warn", name, "连不上：" + found["why"], notes)
        text = f"取列表和一个片段用了 {found['ms']} 毫秒"
        if found["ms"] <= LIVE_SLOW_MS:
            return _row("ok", name, text, notes)
        rules = []
        if (dns["fake"] or via) and not (found_rule is not None and _direct(found_rule["policy"])):
            rules = direct_rules([host])
        return _row("warn", name, text + "，偏慢" + ("：可能经代理绕了远路，试试给它加直连规则" if rules else ""), notes, rules)

    # ---- 网站 ----

    def web(self, relay, clash) -> list[dict]:
        self.step = "正在测 YouTube、B 站、豆瓣…"
        youtube_proxy = ""
        if getattr(relay, "selfhost_data", None) is not None:
            try:
                from . import ytsettings
                youtube_proxy = ytsettings._config(relay)[0].youtube_proxy
            except Exception:                   # noqa: BLE001 —— 读不到设置就当没填
                youtube_proxy = ""
        results: list[dict | None] = [None] * len(self.targets)

        def one(n: int, target) -> None:
            key, label, url, suffixes = target
            results[n] = self._web_row(key, label, url, suffixes, youtube_proxy if key == "youtube" else "", clash)

        threads = [threading.Thread(target=one, args=(n, t), daemon=True) for n, t in enumerate(self.targets)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(WEB_TIMEOUT_S * 2 + 5)
        return [r or _row("warn", self.targets[n][1], "检测没在时间内做完") for n, r in enumerate(results)]

    def _web_row(self, key: str, label: str, url: str, suffixes, proxy_text: str, clash) -> dict:
        host = (urllib.parse.urlsplit(url).hostname or "").lower()
        dns = dns_of(host, self.resolve)
        notes = []
        if proxy_text:
            notes.append(f"经「设置 → YouTube」里的代理 {_hostport(proxy_text)}")
            via = _hostport(proxy_text)
        else:
            via = self.proxies(url)
            notes.append(f"经代理 {via}（系统或环境变量里设的）" if via else _fake_note(dns, host))
        rule_text, found = _route_note(clash, host)
        notes.append(rule_text)
        began = self.monotonic()
        try:
            status = fetch_status(url, proxy_text, self.opener, WEB_TIMEOUT_S)
        except (OSError, ValueError) as error:
            reason = "代理地址写得不对" if isinstance(error, ValueError) else _web_error(error)
            if key == "youtube" and not proxy_text and not via:
                notes.append("在国内连 YouTube 要经代理：在「设置 → YouTube」里填代理地址")
            return _row("warn", label, "连不上：" + reason, notes)
        ms = max(1, int((self.monotonic() - began) * 1000))
        text = f"能连上（{ms} 毫秒，回答 {status}）"
        if ms <= WEB_SLOW_MS:
            return _row("ok", label, text, notes)
        rules = []
        if suffixes and (dns["fake"] or via) and not (found is not None and _direct(found["policy"])):
            rules = direct_rules([], suffixes)
        return _row("warn", label, text + "，偏慢" + ("：国内的网站经代理绕了远路，试试加直连规则" if rules else ""),
                    notes, rules)

    # ---- Clash ----

    def clash(self) -> tuple[list[str] | None, dict]:
        rules = load_rules(self.clash_path)
        if rules is None:
            return None, _row("skip", "Clash Verge", "这台电脑上没找到 Clash Verge 的配置，跳过规则的检查（不影响上面的测速）")
        return rules, _row("ok", "Clash Verge", f"读到 {len(rules)} 条规则（只读了 rules: 这一段，配置一个字没改）",
                           ["下面每项的说明里写了它落在哪一条规则上"])

    # ---- 跑一次 ----

    def run(self, relay) -> dict:
        from . import clouddrive
        began = self.monotonic()
        clash, clash_row = self.clash()
        sections = []
        if clouddrive.enabled_here(relay):
            sections.append({"key": "drives", "title": "网盘", "rows": self.drives(relay, clash)})
        sections.append({"key": "live", "title": "直播源", "rows": self.live(relay, clash)})
        sections.append({"key": "web", "title": "网站", "rows": self.web(relay, clash)})
        sections.append({"key": "clash", "title": "代理软件的规则", "rows": [clash_row]})
        rules = []
        for section in sections:
            for row in section["rows"]:
                rules += [r for r in row["rules"] if r not in rules]
        warn = sum(1 for s in sections for r in s["rows"] if r["state"] == "warn")
        return {"at": int(self.clock()), "took_s": round(self.monotonic() - began, 1), "sections": sections,
                "rules": rules, "advice": ADVICE if rules else "", "warn": warn}

    def start(self, relay) -> bool:
        with self.lock:
            if self.running:
                return False
            self.running, self.step = True, "开始…"
        threading.Thread(target=self._work, args=(relay,), name="netcheck", daemon=True).start()
        return True

    def _work(self, relay) -> None:
        try:
            result = self.run(relay)
            relay.log(f"网络检查：做完了，用了 {result['took_s']} 秒，{result['warn']} 项要注意")
        except Exception as error:              # noqa: BLE001 —— 后台线程不能悄悄死，页面要能再点
            relay.log(f"网络检查出错：{type(error).__name__}")
            result = {"at": int(self.clock()), "took_s": 0, "sections": [], "rules": [], "advice": "", "warn": 0,
                      "error": "检查中途出错了，再试一次"}
        with self.lock:
            self.last, self.running, self.step = result, False, ""

    def view(self) -> dict:
        with self.lock:
            return {"ok": True, "running": self.running, "step": self.step, "result": self.last}


def checker(relay) -> NetCheck:
    found = getattr(relay, "_netcheck", None)
    if found is None:
        found = relay._netcheck = NetCheck()
    return found


def view(relay) -> dict:
    """GET /api/admin/netcheck。"""
    if relay.selfhost is None:
        return {"ok": False, "error": NOT_HERE}
    return checker(relay).view()


def start(relay) -> dict:
    """POST /api/admin/netcheck：开始测（已经在测就接着等那一次）。"""
    if relay.selfhost is None:
        return {"ok": False, "error": NOT_HERE}
    found = checker(relay)
    found.start(relay)
    return found.view()
