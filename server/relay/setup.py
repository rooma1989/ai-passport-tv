"""自建模式（server/selfhost）在中转里的部分：第一个管理员、「等待设置」、设备该连的地址。

设计见 docs/superpowers/specs/2026-10-15-self-hosting-design.md 第 4.5、6.1、7.1 节。只在中转带
--selfhost 启动时有（Relay.selfhost 不是 None）；云端的中转（nginx 后面、已有管理员）不走这里。

- 还没有管理员时中转照常启动（不像云端那样拒绝启动），这时除了 /tv/setup 和静态文件，每个页面都指向
  /tv/setup，接口回答 503 {"setup": true}。
- /tv/setup：输入名字和一次性设置码（监督进程启动时写在 secrets/setup-code、打在日志里；二维码把它放在
  网址的 # 后面，不经过服务器日志），对了就建管理员、给这台手机一个 180 天的会话、删掉设置码文件，
  并记下浏览器用的地址（Host 是局域网 IPv4 或 .local 名字时）当作设备该连的地址和网站地址。
- 同一个网段 10 分钟里最多错 5 次；码有 10 位（约 50 位熵），限流只是防刷。
- wifi.html（声波配网）的服务器栏：自建时用记下的设备地址，而不是页面自己的主机名——经隧道打开时
  那是隧道的名字，设备连不到。
- /tv/api/setup/info 和 /tv/api/setup/wifi-qr.svg：设备地址、声波配网页的网址和它的二维码，给刷机页
  （/flash/）的「刷完之后」用。只读，只透露局域网地址；前端的公开端口上不转。
"""

from __future__ import annotations

import hmac
import html
import ipaddress
import re
import threading
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path

from .. import netident
from ..tenancy import db
from ..tenancy.tickets import RateLimit
from . import console, qr

_ASSET_DIR = Path(__file__).resolve().parent / "assets"
SETUP_PAGE = (_ASSET_DIR / "setup.html").read_bytes()
# 设置码：和遥控码同一套字母（大写 base32），10 位。监督进程（server/selfhost/bootstrap.py）生成。
CODE_LENGTH = 10
# 错码限流：(次数, 秒)，按网段。
WRONG_LIMIT = (5, 600)
NAME_MAX = 20
# 设备地址：IPv4 或主机名（.local），不带端口——设备按自己的型号补端口（8098 / 8097）。
_ADDRESS = re.compile(r"^(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?$")
DEVICE_ADDRESS_META = "device_address"
# 声波配网页地址的二维码（SVG），刷机页用。
WIFI_QR = "/api/setup/wifi-qr.svg"
# 电脑客户端托盘的「打开电脑版」：控制接口（server/selfhost/control.py）给本机浏览器的一次性登录链接。
LOCAL_LOGIN = "/local-login"
_LOOPBACK = ("127.0.0.1", "::1", "::ffff:127.0.0.1")


@dataclass
class SelfHost:
    """中转的自建设置：设置码文件、配置里写定的设备地址（空的话用设置时记下的）。"""
    setup_code_file: Path | None = None
    device_address: str = ""
    wrong: RateLimit = field(default_factory=lambda: RateLimit(*WRONG_LIMIT))
    lock: threading.Lock = field(default_factory=threading.Lock)
    # 有了管理员就不会再回到「等待设置」（总后台不让删最后一个管理员），记住免得每个请求都查库。
    done: bool = False


def normalize_code(text) -> str:
    """去掉空格和横线，转大写（日志里显示成 K7P4Q-X2M9A）。"""
    return re.sub(r"[\s-]", "", str(text or "")).upper()


def read_code(path: Path | None) -> str:
    if path is None:
        return ""
    try:
        return normalize_code(Path(path).read_text(encoding="ascii"))
    except (OSError, UnicodeError):
        return ""


def pending(relay) -> bool:
    """还在等第一个管理员。"""
    selfhost = relay.selfhost
    if selfhost is None or relay.accounts is None or selfhost.done:
        return False
    if relay.accounts.admin_id() is not None:
        selfhost.done = True
        return False
    return True


def lan_host(host_header: str | None) -> str:
    """浏览器用的主机名，是局域网 IPv4 或 .local 名字时返回它（不带端口），否则空串。"""
    host = urllib.parse.urlsplit("//" + (host_header or "")).hostname or ""
    if host.endswith(".local") and _ADDRESS.match(host):
        return host
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return ""
    # 198.18.0.0/15 在 Python 看来也是私有地址，但那是代理软件 TUN 网卡的（netident.EXCLUDED_NETWORKS）：
    # 这台电脑的浏览器经 TUN 地址打开时不能记下它，设备和手机都连不到。
    if address.version == 4 and address.is_private and not address.is_loopback and not address.is_link_local \
            and not netident.excluded(host):
        return host
    return ""


def device_address(relay) -> str:
    """设备该连的地址：配置里写的，否则设置时记下的；都没有时空串。"""
    selfhost = relay.selfhost
    if selfhost is None:
        return ""
    given = selfhost.device_address
    if not given and relay.accounts:
        # 数据库里记下的（第一次设置时浏览器用的地址）：以前的版本可能把代理 TUN 的 198.18.0.1 记了进去。
        given = relay.accounts.meta(DEVICE_ADDRESS_META, "")
        given = "" if netident.excluded(given) else given
    return given if _ADDRESS.match(given or "") else ""


def wifi_url(handler, relay) -> str:
    """手机打开声波配网页的地址：http://<设备地址>:<端口>/tv/wifi。端口取这次请求的 Host（浏览器连局域网
    端口用的那个；Docker 把 8080 映射成别的端口时也对），没写端口就是 80。不知道设备地址时空串。"""
    address = device_address(relay)
    if not address:
        return ""
    netloc = urllib.parse.urlsplit("//" + (handler.headers.get("Host") or "").strip())
    try:
        port = netloc.port or 80
    except ValueError:
        port = 80
    return f"http://{address}{'' if port == 80 else f':{port}'}/tv/wifi"


def wifi_page(relay, page: bytes) -> bytes:
    """声波配网页：把设备地址写进 <meta name="tv-device-address">，页面的 siteHost() 先看它。"""
    address = device_address(relay)
    if not address:
        return page
    tag = f'<meta name="tv-device-address" content="{html.escape(address, quote=True)}">\n'
    return page.replace(b"</head>", tag.encode("ascii") + b"</head>", 1)


def get(handler, relay, path: str) -> bool:
    """在 web.py 的 _get 里、静态文件之后调用；处理过了返回 True。"""
    if relay.selfhost is None:
        return False
    if path in ("/setup", "/setup/"):
        if not pending(relay):
            # 已经有管理员了：设置页不再存在，去电脑版登录。
            handler._send(302, b"", "text/plain", {"Location": "/tv/console"})
        else:
            handler._send(200, SETUP_PAGE, "text/html; charset=utf-8", console.PAGE_HEADERS)
        return True
    if path == "/api/setup/info":
        handler._json({"ok": True, "pending": pending(relay), "device_address": device_address(relay),
                       "wifi_url": wifi_url(handler, relay)})
        return True
    if path == WIFI_QR:
        # 刷机页「刷完之后」的二维码（packaging/flash/）：只含局域网地址，设置页本来就显示它；只读，
        # 前端的公开端口上 /api/setup/ 整个不转（server/front.py）。
        link = wifi_url(handler, relay)
        if not link:
            handler._not_found()
        else:
            handler._send(200, qr.svg(link).encode("ascii"), "image/svg+xml",
                          {"Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'"})
        return True
    if path == LOCAL_LOGIN:
        _local_login(handler, relay)
        return True
    if not pending(relay):
        return False
    _point_to_setup(handler, path)
    return True


def post(handler, relay, path: str) -> bool:
    """在 web.py 的 _post 最前面调用；处理过了返回 True。"""
    if relay.selfhost is None:
        return False
    if path == "/api/setup":
        _setup(handler, relay)
        return True
    if pending(relay):
        _point_to_setup(handler, path)
        return True
    return False


def _point_to_setup(handler, path: str) -> None:
    if path.startswith("/api/"):
        handler._json({"ok": False, "setup": True, "error": "这台服务器还没设置管理员：请先打开 /tv/setup"}, 503)
    else:
        handler._send(302, b"", "text/plain", {"Location": "/tv/setup"})


def _setup(handler, relay) -> None:
    body = handler._body()
    if body is None or not isinstance(body.get("code"), str) or not isinstance(body.get("name", ""), str):
        handler._json({"ok": False, "error": "请求格式不对"}, 400)
        return
    selfhost = relay.selfhost
    if not pending(relay):
        handler._json({"ok": False, "error": "这台服务器已经有管理员了，请到电脑版（/tv/console）登录"})
        return
    ip = handler._ip()
    network = db.ip_prefix(ip) or "?"
    if selfhost.wrong.blocked(network):
        handler._json({"ok": False, "error": "设置码错了太多次，请 10 分钟后再试"})
        return
    expected = read_code(selfhost.setup_code_file)
    given = normalize_code(body["code"])
    if len(expected) != CODE_LENGTH or not hmac.compare_digest(given.encode(), expected.encode()):
        selfhost.wrong.hit(network)
        relay.accounts.audit("user.setup_refused", None, ip)
        handler._json({"ok": False, "error": "设置码不对。请看服务器日志（或启动它的终端）里「设置码」那一行；"
                                             "每次重启服务器都会换一个新的。"})
        return
    name = " ".join(body.get("name", "").split())[:NAME_MAX] or "管理员"
    with selfhost.lock:
        if relay.accounts.admin_id() is not None:
            handler._json({"ok": False, "error": "这台服务器已经有管理员了，请到电脑版（/tv/console）登录"})
            return
        user_id = relay.accounts.create_user(name, "admin")
        selfhost.done = True
    token = relay.accounts.new_session(user_id, "phone", user_agent=handler._agent(), ip=ip)
    relay.accounts.audit("user.setup", user_id, ip, "user", str(user_id))
    try:
        selfhost.setup_code_file.unlink()
    except (OSError, AttributeError):
        pass
    _remember_address(handler, relay)
    relay.log(f"自建：第一个管理员（用户 {user_id}）已创建，设置码作废")
    handler._json({"ok": True, "redirect": f"/tv/?s={token}&home=1", "device_address": device_address(relay)},
                  headers={"Set-Cookie": handler._cookie_header(token)})


def _local_login(handler, relay) -> None:
    """/tv/local-login?t=<令牌>：只认这台电脑自己来的请求（中转的对端和前端转来的 X-Real-IP 都是回环；
    前端的公开端口上这个路径根本不转，server/front.py），令牌是 PhoneLinks.create_local 给的（来路 local，
    60 秒，用一次）。对了就给这个浏览器一个电脑会话，跳到电脑版。不对一律当作没有这个页面。"""
    peer = handler.client_address[0]
    token = handler._query().get("t")
    links = relay.phone_links
    if peer not in _LOOPBACK or handler._ip() not in _LOOPBACK or not isinstance(token, str) or links is None:
        handler._not_found()
        return
    if links.origin_of(token=token)["origin"] != "local":
        handler._not_found()
        return
    user = links.consume(token=token, ip=handler._ip(), user_agent=handler._agent())
    if user is None:
        handler._send(302, b"", "text/plain", {"Location": "/tv/console"})
        return
    session = relay.accounts.new_session(user, "pc", user_agent=handler._agent(), ip=handler._ip(), via="local")
    relay.accounts.audit("login.pc.local", user, handler._ip(), "user", str(user))
    relay.log(f"用户 {user} 用这台电脑上的小智电视程序登录了电脑版")
    handler._send(302, b"", "text/plain",
                  {"Location": "/tv/console", "Set-Cookie": console.cookie_header(session, "pc", handler._https())})


def _remember_address(handler, relay) -> None:
    """记下浏览器用的地址：设备该连的地址（没在配置里写定时）、网站地址（没配置时，二维码用）。"""
    host_header = (handler.headers.get("Host") or "").strip()
    address = lan_host(host_header)
    if not address:
        return
    if not relay.selfhost.device_address:
        relay.accounts.set_meta(DEVICE_ADDRESS_META, address)
    if not relay.public_url:
        relay.public_url = f"{'https' if handler._https() else 'http'}://{host_header}".rstrip("/")
        # 电视服务给未绑定设备的绑定二维码用数据库里的这一项（server/tenancy/gate.py 的 claim_url）。
        relay.accounts.set_meta("public_url", relay.public_url)
    relay.sync_devices(force=True)
