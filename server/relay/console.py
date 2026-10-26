"""电脑版（多用户阶段 2）：/tv/console 页面（媒体中心界面，旧版在 /tv/console-classic）、扫码登录、登录列表、设备详情、邀请列表、二维码。

只在多用户（--db）时有；单用户的中转对这些路径一律 404（电脑上想用，先上 --db）。
遥控、订阅、投屏用的是手机同一套接口（web.py）：「一套接口，两种排版」。这里只放电脑版
多出来的那几个，由 web.py 的处理类在最前面转过来（get()/post() 返回 True 表示处理过了）。

谁能做什么：
- 页面、要票据、等结果、扫码落地页：不用登录（票据本身就是凭据）。
- 让手机登录（反方向，phonelinks.py）：已登录的电脑或手机（成员的会话，手机遥控「我的 → 让另一部手机登录」）
  要链接、看结果；访客和密钥不行。新手机的落地页和「登录」不用登录（一次性链接本身就是凭据，GET 不会用掉它）。
- 确认电脑登录、看登录列表、退出别处：已登录的手机或电脑（cookie），访客和密钥都不行。
- 设备详情、二维码：账号（访客不行，密钥算管理员）。邀请列表：管理员。
"""

from __future__ import annotations

import hashlib
import re
import urllib.parse
from pathlib import Path

from ..tenancy import db
from ..tenancy.accounts import MODEL_NAMES, SESSION_S
from ..tenancy.tickets import Limited, describe_agent
from . import qr

# 和 web.COOKIE 相同（web 引用本模块，这里不反过来引用它）；tests/test_relay_console.py 核对。
COOKIE = "tvs"
_LOGIN_LINK = re.compile(r"^/(?:l|TV/L)/([A-Za-z2-7]{26})$")
# 让手机登录的落地页：/TV/P/<令牌>（nginx 转成 /p/<令牌>），没有令牌的 /tv/p 是输入 8 位码的地方。
_PHONE_LINK = re.compile(r"^/(?:p|TV/P)(?:/|/[A-Za-z2-7]{26})?$")
QR_TEXT_MAX = 300
_ASSET_DIR = Path(__file__).resolve().parent / "assets"
# 电脑版（媒体中心界面）：页面 + 样式 + 脚本三个文件。样式和脚本当静态文件给（web.ASSETS，浏览器缓存一天），
# 页面里写的地址带着内容的散列（?v=…），改了文件浏览器就会取新的。
CONSOLE_CSS = (_ASSET_DIR / "console.css").read_bytes()
CONSOLE_JS = (_ASSET_DIR / "console.js").read_bytes()
CONSOLE_PAGE = (_ASSET_DIR / "console.html").read_bytes() \
    .replace(b"__CONSOLE_CSS__", hashlib.sha256(CONSOLE_CSS).hexdigest()[:12].encode()) \
    .replace(b"__CONSOLE_JS__", hashlib.sha256(CONSOLE_JS).hexdigest()[:12].encode())
CONSOLE_ASSETS = {"/console.css": ("text/css; charset=utf-8", CONSOLE_CSS),
                  "/console.js": ("text/javascript; charset=utf-8", CONSOLE_JS)}
# 旧版电脑版：测试版里留在 /tv/console-classic（和 /tv/console 同一层，页面里的相对地址不用改）。
CLASSIC_PAGE = (_ASSET_DIR / "console-classic.html").read_bytes()
LOGIN_PAGE = (_ASSET_DIR / "login.html").read_bytes()
PHONE_PAGE = (_ASSET_DIR / "phone.html").read_bytes()
# 登录确认页和电脑版都不许被别的网站嵌进框里（防止骗点「确认」）。
# 脚本只许本站的（'self'：扫码登录时才加载的 /tv/scan.js、/tv/jsqr.js）；相机画面是 srcObject，不受 media-src 管。
# media-src：电脑版「在电脑上看」的播放器（watch.py）——本站的 HLS，或者 MediaSource 的 blob: 地址。
PAGE_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; script-src 'self' 'unsafe-inline'; style-src 'unsafe-inline'; "
                               "img-src 'self' data:; media-src 'self' blob:; connect-src 'self'; manifest-src 'self'; "
                               "base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
    "X-Frame-Options": "DENY",
}
# 新电脑版多了本站的样式表（console.css）；别的和上面一样。
CONSOLE_HEADERS = dict(PAGE_HEADERS, **{
    "Content-Security-Policy": PAGE_HEADERS["Content-Security-Policy"].replace(
        "style-src 'unsafe-inline'", "style-src 'self' 'unsafe-inline'")})


def same_origin(headers) -> bool:
    """改东西的请求（POST、DELETE）只收本站页面发的：跨站的表单或脚本一律当作不存在。

    cookie 本身是 SameSite=Lax，跨站的 POST 不带它；这里再看一眼浏览器给的来源，双保险。
    没有这两个头的（电视服务的语音命令、curl）照常处理：它们不是浏览器，带不了别人的 cookie。
    """
    site = (headers.get("Sec-Fetch-Site") or "").lower()
    if site and site not in ("same-origin", "none"):
        return False
    origin = headers.get("Origin")
    if origin is None:
        return True
    # 只比主机名：nginx 转进来的 Host 是 $host，不带端口。
    host = urllib.parse.urlsplit("//" + (headers.get("Host") or "")).hostname
    return origin != "null" and host is not None and urllib.parse.urlsplit(origin).hostname == host


def _https(handler) -> bool:
    """请求是不是经 HTTPS 来的（web.py 的 _https；没有这个方法的处理类当作是）。"""
    check = getattr(handler, "_https", None)
    return check() if check is not None else True


def cookie_header(token: str, kind: str, secure: bool = True) -> str:
    """secure：自建模式的明文 HTTP 局域网上不带 Secure，浏览器才会保存（web.py 的 _https）。"""
    flag = "Secure; " if secure else ""
    return (f"{COOKIE}={token}; HttpOnly; {flag}SameSite=Lax; Path=/tv/; "
            f"Max-Age={SESSION_S[kind]}")


def _clear_cookie(secure: bool = True) -> str:
    flag = "Secure; " if secure else ""
    return f"{COOKIE}=; HttpOnly; {flag}SameSite=Lax; Path=/tv/; Max-Age=0"


def _base(relay, handler) -> str:
    """网站地址（二维码用）：--public-url；没配时按请求的 Host 猜（自建的明文 HTTP 局域网上是 http://）。"""
    if relay.public_url:
        return relay.public_url
    host = handler.headers.get("Host") or "localhost"
    local = host.startswith(("127.", "localhost", "[::1]"))
    return ("http://" if local or not _https(handler) else "https://") + host


def _session(relay, handler) -> dict | None:
    """cookie 里的会话（不认密钥）：{user_id, kind, device_id, role, name}。"""
    return relay.accounts.session(handler._cookie())


def _member_session(relay, handler) -> dict | None:
    info = _session(relay, handler)
    return info if info is not None and info["role"] != "guest" else None


# ---- GET ----

def get(handler, relay, path: str) -> bool:
    if path == "/console":
        handler._send(200, CONSOLE_PAGE, "text/html; charset=utf-8", CONSOLE_HEADERS)
        return True
    if path == "/console-classic":
        handler._send(200, CLASSIC_PAGE, "text/html; charset=utf-8", PAGE_HEADERS)
        return True
    if path == "/console/":
        handler._send(302, b"", "text/plain", {"Location": "/tv/console"})
        return True
    if path == "/login" or _LOGIN_LINK.match(path):
        handler._send(200, LOGIN_PAGE, "text/html; charset=utf-8", PAGE_HEADERS)
        return True
    if path == "/api/login/status":
        info = _session(relay, handler)
        me = {"name": info["name"], "role": info["role"], "kind": info["kind"]} if info else None
        answer = {"ok": True, "signed_in": info is not None, "me": me}
        notice = recovery_notice(relay, info["user_id"], handler._cookie()) if info and info["user_id"] else None
        if notice:
            answer["notice"] = notice
        handler._json(answer)
        return True
    if path == "/api/login/info":
        _login_info(handler, relay)
        return True
    if _PHONE_LINK.match(path):
        handler._send(200, PHONE_PAGE, "text/html; charset=utf-8", PAGE_HEADERS)
        return True
    if path == "/api/phone-link/info":
        _phone_link_info(handler, relay)
        return True
    if path == "/api/phone-link/status":
        info = _member_session(relay, handler)
        if info is None:
            handler._not_found()
            return True
        handler._json({"ok": True, **relay.phone_links.status(info["user_id"], handler._query().get("poll"))})
        return True
    if path == "/api/sessions":
        info = _member_session(relay, handler)
        if info is None:
            handler._not_found()
            return True
        rows = relay.accounts.session_list(info["user_id"], handler._cookie())
        for row in rows:
            row["browser"] = describe_agent(row.pop("user_agent"))
        handler._json({"ok": True, "sessions": rows})
        return True
    if path == "/api/console/devices":
        caller = handler._caller()
        if caller is None or caller.guest or caller.user is None:
            handler._not_found()
            return True
        handler._json({"ok": True, "devices": devices(relay, caller, _base(relay, handler))})
        return True
    if path == "/api/invites":
        caller = handler._caller()
        if caller is None or caller.role != "admin":
            handler._not_found()
            return True
        handler._json({"ok": True, "invites": invites(relay)})
        return True
    return False


RECOVERY_NOTICE = "有新手机通过设备恢复登录"


def recovery_notice(relay, user_id: int, token: str | None) -> dict | None:
    """这个登录之后有别的手机经设备「恢复登录」登进了同一个账号：{id, text, at, browser, network}。
    遥控页和电脑版各提示一次（按 id 记在浏览器里）；登录列表里那一行标着「设备恢复」，可以退出它。"""
    found = relay.accounts.recovery_notice(user_id, token)
    if found is None:
        return None
    return {"id": found["id"], "text": RECOVERY_NOTICE, "at": found["at"],
            "browser": describe_agent(found["user_agent"]), "network": found["network"]}


def _login_info(handler, relay) -> None:
    """确认页要看的：用二维码里的票据不用登录（只回答登录码，好让人去桌面图标里输入）；
    用 6 位码查要已登录，并且按用户和网段限流（输错才计数）。"""
    query = handler._query()
    tickets = relay.login_tickets
    member = _member_session(relay, handler)
    ticket, code = query.get("t"), query.get("code")
    if code is not None:
        if member is None:
            handler._not_found()
            return
        keys = (f"u{member['user_id']}", db.ip_prefix(handler._ip()) or "?")
        if tickets.guess_blocked(keys):
            handler._json({"ok": False, "error": "输错太多次了，请 10 分钟后再试"})
            return
        found = tickets.info(code=code)
        if found is None:
            tickets.guess_missed(keys)
    else:
        found = tickets.info(ticket=ticket)
    if found is None:
        handler._json({"ok": False, "error": "这个登录码不对或已经过期了，请看电脑屏幕上现在显示的码"})
        return
    found.update(ok=True, signed_in=member is not None,
                 guest=member is None and _session(relay, handler) is not None,
                 same_network=bool(found["network"]) and found["network"] == db.ip_prefix(handler._ip()),
                 name=member["name"] if member else "")
    handler._json(found)


def _phone_link_info(handler, relay) -> None:
    """手机落地页：谁的账号、哪台电脑或手机发起的。用令牌或 8 位码问；输错按网段和全局限流。不会把链接用掉。"""
    query = handler._query()
    links = relay.phone_links
    ip = handler._ip()
    by_code = query.get("t") is None
    if by_code and links.blocked(ip):
        handler._json({"ok": False, "error": "输错太多次了，请 10 分钟后再试"})
        return
    found = links.info(token=query.get("t"), code=query.get("code") if by_code else None)
    if found is not None and links.origin_of(token=query.get("t"),
                                             code=query.get("code") if by_code else None)["origin"] == "local":
        found = None     # 电脑客户端给本机浏览器的链接（setup.py 的 /local-login），不是给手机的
    if found is None:
        if by_code:
            links.missed(ip)
        handler._json({"ok": False, "error": "这个登录链接已经用过或过期了，请在已登录的电脑或手机上重新生成一个"})
        return
    # 设备上的「恢复登录」或管理员给的：落地页说是哪台设备、谁发起的。
    origin = links.origin_of(token=query.get("t"), code=query.get("code") if by_code else None)
    handler._json({"ok": True, **found, "origin": origin["origin"], "device_name": origin["device_name"]})


def _new_phone_link(handler, relay) -> None:
    """电脑版「让手机登录」和手机遥控「让另一部手机登录」：成员的会话（电脑或手机）才能要；访客、密钥 404。"""
    member = _member_session(relay, handler)
    if member is None:
        handler._not_found()
        return
    try:
        made = relay.phone_links.create(member["user_id"], handler._ip(), handler._agent(), source=member["kind"])
    except Limited as error:
        handler._json({"ok": False, "error": str(error)}, 429)
        return
    base = _base(relay, handler)
    link = f"{base.upper()}/TV/P/{made.pop('token')}"
    handler._json({"ok": True, "svg": qr.svg(link), "link": link, "typed": f"{base}/tv/p", **made})


def _accept_phone_link(handler, relay, body: dict) -> None:
    """新手机点了「在这台手机上登录」：一次性链接换成这个用户普通的手机会话（180 天）。"""
    token, code = body.get("token"), body.get("code")
    if (token is not None and not isinstance(token, str)) or (code is not None and not isinstance(code, str)) \
            or (token is None) == (code is None):
        handler._json({"ok": False, "error": "请求格式不对"}, 400)
        return
    links = relay.phone_links
    ip = handler._ip()
    if code is not None and links.blocked(ip):
        handler._json({"ok": False, "error": "输错太多次了，请 10 分钟后再试"})
        return
    local = links.origin_of(token=token, code=code)["origin"] == "local"
    user = None if local else links.consume(token=token, code=code, ip=ip, user_agent=handler._agent())
    if user is None:
        if code is not None:
            links.missed(ip)
        handler._json({"ok": False, "error": "这个登录链接已经用过或过期了，请在已登录的电脑或手机上重新生成一个"})
        return
    # 设备上的「恢复登录」（via=recover：主人别的登录上会提示）、管理员给的链接，各记各的来路。
    origin = links.origin_of(token=token, code=code)
    via = {"device": "recover", "admin": "admin"}.get(origin["origin"], "")
    session = relay.accounts.new_session(user, "phone", user_agent=handler._agent(), ip=ip, via=via)
    action = {"recover": "login.phone.recover", "admin": "login.phone.admin"}.get(via, "login.phone.link")
    relay.accounts.audit(action, user, ip, "session", origin["device_id"] or "", describe_agent(handler._agent()))
    source = {"recover": "设备恢复登录", "admin": "管理员"}.get(via, "另一台已登录的设备")
    relay.log(f"用户 {user} 用{source}给的链接在手机上登录")
    # 网址里带着会话：从这一页「添加到主屏幕」，图标记住它（见 web.py 的 _login）；home=1 让遥控提示添加。
    handler._json({"ok": True, "redirect": f"/tv/?s={session}&home=1"},
                  headers={"Set-Cookie": cookie_header(session, "phone", _https(handler))})


def devices(relay, caller, base: str) -> list[dict]:
    """电脑版「设备」页：名字、型号、固件、在线、正在播什么、访客；主人还能拿到访客链接。"""
    rows = []
    guests = {}
    for session in relay.accounts.session_list(caller.user):
        if session["kind"] == "guest":
            guests[session["device_id"]] = guests.get(session["device_id"], 0) + 1
    for row in relay.accounts.devices_of(caller.user):
        dev = relay._device_entry(row)
        owner = row["member_role"] == "owner"
        playing = relay.now(relay.groups(dev), dev)
        item = {"id": dev["id"], "name": dev["name"], "model": dev["model"],
                "model_name": MODEL_NAMES.get(dev["model"], dev["model"] or "小电视"), "fw": dev["fw"],
                "legacy": dev["legacy"], "mine": owner, "online": playing["online"],
                "channel": playing["channel"] if playing["online"] else "",
                "channel_name": playing["name"] if playing["online"] else "",
                "title": playing["title"] if playing["online"] else "",
                "last_seen": row.get("last_seen") or 0, "since": playing["since"] if playing["online"] else 0,
                "guests": guests.get(dev["id"], 0) if owner else 0}
        if owner and dev["remote_code"]:
            item["guest_link"] = f"{base.upper()}/TV/R/{dev['remote_code']}"
        rows.append(item)
    return rows


def invites(relay) -> list[dict]:
    now = relay.clock()
    rows = relay.accounts.invite_list()
    for row in rows:
        row["state"] = "used" if row["used_at"] else "expired" if row["expires_at"] <= now else "open"
    return rows


# ---- POST ----

def post(handler, relay, path: str) -> bool:
    if path in ("/api/login/ticket", "/api/phone-link"):
        # 不看请求体，但要读掉：同一个 keep-alive 连接上剩下的字节会粘到下一个请求前面（「{}GET」→ 501）。
        handler._body()
        if path == "/api/login/ticket":
            _new_ticket(handler, relay)
        else:
            _new_phone_link(handler, relay)
        return True
    if path not in ("/api/login/poll", "/api/login/confirm", "/api/logout", "/api/sessions/revoke", "/api/qr",
                    "/api/phone-link/accept"):
        return False
    body = handler._body()
    if body is None and path != "/api/logout":
        handler._json({"ok": False, "error": "请求格式不对"}, 400)
        return True
    body = body or {}
    if path == "/api/login/poll":
        _poll(handler, relay, body)
    elif path == "/api/login/confirm":
        _confirm(handler, relay, body)
    elif path == "/api/logout":
        token = handler._cookie()
        if token and relay.accounts.revoke_session(token):
            relay.log("退出登录")
        handler._json({"ok": True}, headers={"Set-Cookie": _clear_cookie(_https(handler))})
    elif path == "/api/sessions/revoke":
        _revoke(handler, relay, body)
    elif path == "/api/phone-link/accept":
        _accept_phone_link(handler, relay, body)
    else:
        caller = handler._caller()
        text = body.get("text")
        if caller is None or caller.guest or caller.user is None:
            handler._not_found()
        elif not isinstance(text, str) or not 0 < len(text) <= QR_TEXT_MAX:
            handler._json({"ok": False, "error": "请求格式不对"}, 400)
        else:
            handler._json({"ok": True, "svg": qr.svg(text)})
    return True


def _new_ticket(handler, relay) -> None:
    try:
        ticket = relay.login_tickets.create(handler._ip(), handler._agent())
    except Limited as error:
        handler._json({"ok": False, "error": str(error)}, 429)
        return
    link = f"{_base(relay, handler).upper()}/TV/L/{ticket.pop('ticket')}"
    handler._json({"ok": True, "svg": qr.svg(link), **ticket})


def _poll(handler, relay, body: dict) -> None:
    status, user = relay.login_tickets.poll(body.get("poll"))
    if status != "ok":
        handler._json({"ok": True, "state": status})
        return
    token = relay.accounts.new_session(user, "pc", user_agent=handler._agent(), ip=handler._ip())
    relay.accounts.audit("login.pc", user, handler._ip(), "session", "", describe_agent(handler._agent()))
    relay.log(f"用户 {user} 在电脑上登录")
    handler._json({"ok": True, "state": "ok"}, headers={"Set-Cookie": cookie_header(token, "pc", _https(handler))})


def _confirm(handler, relay, body: dict) -> None:
    member = _member_session(relay, handler)
    if member is None:
        handler._not_found()
        return
    ticket, code, accept = body.get("ticket"), body.get("code"), body.get("accept", True)
    if not isinstance(accept, bool) or (ticket is not None and not isinstance(ticket, str)) or \
            (code is not None and not isinstance(code, str)) or (ticket is None) == (code is None):
        handler._json({"ok": False, "error": "请求格式不对"}, 400)
        return
    tickets = relay.login_tickets
    keys = (f"u{member['user_id']}", db.ip_prefix(handler._ip()) or "?")
    if code is not None and tickets.guess_blocked(keys):
        handler._json({"ok": False, "error": "输错太多次了，请 10 分钟后再试"})
        return
    if not tickets.answer(member["user_id"], accept, ticket=ticket, code=code, ip=handler._ip()):
        if code is not None:
            tickets.guess_missed(keys)
        handler._json({"ok": False, "error": "这个登录码已经过期或用过了，请在电脑上刷新二维码"})
        return
    handler._json({"ok": True})


def _revoke(handler, relay, body: dict) -> None:
    member = _member_session(relay, handler)
    if member is None:
        handler._not_found()
        return
    if body.get("others") is True:
        count = relay.accounts.revoke_other_sessions(member["user_id"], handler._cookie())
        handler._json({"ok": True, "count": count})
        return
    session_id = body.get("id")
    if not isinstance(session_id, str) or not relay.accounts.revoke_session_id(member["user_id"], session_id):
        handler._json({"ok": False, "error": "没有这个登录"})
        return
    handler._json({"ok": True})
