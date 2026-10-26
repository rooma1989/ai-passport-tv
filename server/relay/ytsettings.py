"""电脑版「设置 → YouTube」的接口（家庭服务器和电脑客户端；云端没有这些路径）。

只有管理员（自建时就是这台服务器的主人）能用，别人一律 404，和总后台一样。改东西的请求由 web.py 先查同源。

    GET  /api/youtube/settings          模式、代理、组件、cookies、现在用的是哪种 YouTube
    POST /api/youtube/settings          {"mode": off|local|node, "proxy": ""}：改 config.toml，请监督进程更新子进程
    POST /api/youtube/detect            自动检测系统代理（只回答找到的，不保存）
    POST /api/youtube/test              用现在运行的设置解析一个公开的测试视频，回答用了几秒或出了什么错
    POST /api/youtube/components        {"action": install|update|check, "mirror": ""}：安装 / 更新 yt-dlp / 只查版本
    POST /api/youtube/cookies           {"text": cookies.txt 的内容}：校验、只留 YouTube 的条目、存成 0600
    POST /api/youtube/cookies/delete

从浏览器读取登录、导入订阅的频道（/api/youtube/browsers…、/api/youtube/subscriptions…）在 ytaccount.py。

cookies 的内容不进日志、不进回答；回答里只有条数。代理地址只回给管理员自己。
设计：docs/superpowers/specs/2026-10-15-self-hosting-design.md 第 8.4 节。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from .bilibili import SourceError

PREFIX = "/api/youtube/"
BODY_MAX = 64 * 1024
# 「测试」用的视频（YouTube 上第一个视频，19 秒，公开）；和节点后台检查的是同一个。
TEST_VIDEO = "jNQXAC9IVRw"
# 前端（server/front.py）等中转 65 秒；yt-dlp 解析一次在慢的代理上要十几到几十秒。
TEST_TIMEOUT_S = 55
STATUS_FILE = "youtube-components.json"


def _allowed(handler, relay) -> bool:
    if relay.selfhost is None or getattr(relay, "selfhost_data", None) is None:
        return False
    caller = handler._caller()
    return caller is not None and caller.role == "admin" and not caller.guest


def _paths(relay):
    from ..selfhost.bootstrap import Layout
    return Layout(Path(relay.selfhost_data))


def _config(relay):
    """(config, 错误)：config.toml 读不了时用默认值，错误给页面看。"""
    from ..selfhost import config as config_module
    try:
        return config_module.load(_paths(relay).config), ""
    except config_module.ConfigError as error:
        return config_module.Config(), str(error)


def components(relay):
    """这个中转进程里唯一的组件管理（下载在它的后台线程里）。"""
    found = getattr(relay, "_yt_components", None)
    if found is None:
        from ..selfhost.ytcomponents import Components
        layout = _paths(relay)
        found = Components(layout.youtube_bin, log=relay.log, status_file=layout.run / STATUS_FILE)
        relay._yt_components = found
    return found


def autostart(relay) -> None:
    """本机模式的中转启动时：组件还没装好就开始装（第一次用），经设置里的代理。"""
    if getattr(relay, "youtube_kind", "") != "local" or getattr(relay, "selfhost_data", None) is None:
        return
    parts = components(relay)
    if not parts.assets:
        return
    if parts.ready() and not parts.legacy():
        return
    config, _ = _config(relay)
    if parts.ready():
        # 以前装的单文件 yt-dlp：换成纯 Python 版（ytcomponents 的说明）。换好之前旧的照样能用。
        relay.log("YouTube 本机模式：把单文件版 yt-dlp 换成纯 Python 版（启动快得多）")
    else:
        relay.log("YouTube 本机模式：组件还没装好，开始下载 yt-dlp 和 deno")
    parts.start("install", config.youtube_proxy)


def settings(relay) -> dict:
    from ..selfhost import ytlocal
    layout = _paths(relay)
    config, error = _config(relay)
    kind = getattr(relay, "youtube_kind", "")
    answer = {"ok": True, "mode": config.youtube_mode, "proxy": config.youtube_proxy, "config_error": error,
              "running": kind or "off", "node_file": layout.youtube_node.is_file(),
              "components": components(relay).snapshot(),
              "cookies": ytlocal.cookies_summary(ytlocal.cookies_path(layout.root, config.youtube_cookies)),
              "health": None}
    if kind and relay.clients.get("youtube") is not None:
        answer["health"] = relay.youtube_state()
    return answer


def _save(relay, body: dict) -> dict:
    from ..selfhost import ytlocal
    mode, proxy = body.get("mode"), body.get("proxy", "")
    if mode not in ytlocal.MODES or not isinstance(proxy, str):
        return {"ok": False, "error": "请求格式不对"}
    try:
        proxy = ytlocal.check_proxy(proxy)
    except ytlocal.SettingsError as error:
        return {"ok": False, "error": str(error)}
    layout = _paths(relay)
    config, error = _config(relay)
    if error:
        return {"ok": False, "error": f"config.toml 有错，先改好它：{error}"}
    if mode == "node" and not layout.youtube_node.is_file():
        return {"ok": False, "error": "「自己的节点」要先把节点的配置放进 secrets/youtube-node.json（url、key、cert），"
                                      "见使用说明"}
    parts = components(relay)
    if mode == "local" and not parts.assets:
        return {"ok": False, "error": "这台电脑的系统没有官方的 deno，本机模式用不了；可以用「自己的节点」"}
    if (mode, proxy) == (config.youtube_mode, config.youtube_proxy):
        if mode == "local" and getattr(relay, "youtube_kind", "") == "local" and parts.legacy():
            parts.start("install", proxy)
        return {"ok": True, "changed": False, "restart": False}
    try:
        ytlocal.set_youtube(layout.config, mode, proxy)
    except ytlocal.SettingsError as error:
        return {"ok": False, "error": str(error)}
    kind = getattr(relay, "youtube_kind", "")
    running = kind or "off"
    # 中转自己的参数只跟模式有关（代理只给 ytnode）：模式变了中转会被重启，组件交给重启后的中转去装。
    restart = running != mode
    relay.log(f"YouTube 设置改了：模式 {mode}，{'有' if proxy else '没有'}代理")
    if mode == "local" and not restart and (not parts.ready() or parts.legacy()):
        parts.start("install", proxy)
    ytlocal.request_refresh(layout.run)
    return {"ok": True, "changed": True, "restart": restart}


def _detect(relay) -> dict:
    from ..selfhost import ytlocal
    import os
    found = ytlocal.detect_proxy(in_container=os.environ.get("XIAOZHI_TV_CONTAINER") == "1"
                                 or Path("/.dockerenv").exists())
    if not found["proxy"]:
        return {"ok": True, "proxy": "", "message": "没有找到系统代理。代理软件开着 TUN（增强）模式时可以不填，直接保存再测试；"
                                                    "否则填代理软件里写的本机端口，例如 http://127.0.0.1:端口"}
    return {"ok": True, "proxy": found["proxy"], "source": found["source"],
            "message": f"在{found['source']}里找到：{found['proxy']}。点「保存」才会用它"}


def describe_check(result: dict, cookies_present: bool) -> dict:
    """节点 /yt/check 的回答 → {"ok", "message"}（给人看的中文）。"""
    state, seconds = result.get("state"), result.get("seconds") or 0
    if state == "ok":
        version = result.get("version") or ""
        return {"ok": True, "message": f"成功：{seconds} 秒解析了测试视频" + (f"（yt-dlp {version}）" if version else "")}
    if state == "missing":
        return {"ok": False, "message": "YouTube 组件还没装好：点下面的「安装」"}
    if state == "login":
        return {"ok": False, "message": "需要登录：YouTube 要求证明不是机器人，cookies 可能过期了，用小号重新导出"
                if cookies_present else "需要登录：YouTube 要求证明不是机器人，导入 cookies（建议用小号）"}
    startup = result.get("startup")
    if state in ("network", "slow") and isinstance(startup, (int, float)) and not isinstance(startup, bool):
        from .ytsearch import SLOW_START_S
        if startup >= SLOW_START_S:
            return {"ok": False, "message": f"是本机的 yt-dlp 自己慢：光启动就要 {startup:g} 秒，不是代理的问题。"
                                            "电脑很忙或安全软件在检查它时会这样；点下面的「检查更新」可以重新装 yt-dlp"}
    if state in ("network", "slow"):
        return {"ok": False, "message": "连不上 YouTube：检查代理（地址、端口对不对，代理软件开着没有）"}
    error = str(result.get("error") or "")[:160]
    return {"ok": False, "message": "出错了：" + (error or "原因不明，看日志")}


def _test(relay) -> dict:
    from ..selfhost import ytlocal
    client = relay.clients.get("youtube")
    kind = getattr(relay, "youtube_kind", "")
    config, _ = _config(relay)
    if (kind or "off") != config.youtube_mode:
        return {"ok": False, "message": "刚保存的设置还在生效：等几秒再点「测试」"}
    if client is None or not kind:
        return {"ok": False, "message": "YouTube 还没开启：选「本机」、填好代理后先点「保存」"}
    started = time.monotonic()
    if kind == "node":
        try:
            client.video(TEST_VIDEO)
        except SourceError as error:
            return {"ok": False, "message": "节点解析失败：" + str(error)[:160]}
        return {"ok": True, "message": f"成功：节点 {time.monotonic() - started:.1f} 秒回答了"}
    try:
        result = client.check(timeout=TEST_TIMEOUT_S)
    except SourceError as error:
        if error.reason == "slow" or time.monotonic() - started >= TEST_TIMEOUT_S - 1:
            from .ytsearch import SLOW_START_S, startup_seconds
            startup = startup_seconds(client)
            if startup is not None and startup >= SLOW_START_S:
                return {"ok": False, "message": f"超过 {TEST_TIMEOUT_S} 秒没有结果：是本机的 yt-dlp 自己慢（光启动就要 "
                                                f"{startup:g} 秒），不是代理的问题。点下面的「检查更新」可以重新装 yt-dlp"}
            return {"ok": False, "message": f"超过 {TEST_TIMEOUT_S} 秒没有结果：代理太慢或连不上 YouTube，检查代理"}
        return {"ok": False, "message": "本机的 YouTube 节点没有响应（刚保存过的话等几秒再试）"}
    present = ytlocal.cookies_summary(ytlocal.cookies_path(_paths(relay).root, config.youtube_cookies))["present"]
    answer = describe_check(result, present)
    relay._youtube_health = None    # 遥控页的「YouTube 状态」跟上这次的结果
    relay.log(f"YouTube 测试：{result.get('state')}，{result.get('seconds')} 秒")
    return answer


def _components(relay, body: dict) -> dict:
    from ..selfhost import ytcomponents
    action, mirror = body.get("action"), body.get("mirror") or ""
    if action not in ("install", "update", "check") or not isinstance(mirror, str):
        return {"ok": False, "error": "请求格式不对"}
    mirror = mirror.strip()
    if mirror and not ytcomponents.valid_mirror(mirror):
        return {"ok": False, "error": "镜像要写成 https:// 开头的网址前缀"}
    config, _ = _config(relay)
    parts = components(relay)
    if not parts.assets:
        return {"ok": False, "error": "这台电脑的系统没有官方的 deno，本机模式用不了"}
    if action == "check":
        try:
            found = parts.check_update(config.youtube_proxy, mirror)
        except ytcomponents.ComponentError as error:
            return {"ok": False, "error": str(error)}
        return {"ok": True, **found, "components": parts.snapshot()}
    if not parts.start(action, config.youtube_proxy, mirror):
        return {"ok": False, "error": "正在下载，等它做完"}
    return {"ok": True, "components": parts.snapshot()}


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


def _cookies(relay, body: dict) -> dict:
    from ..selfhost import ytlocal
    text = body.get("text")
    if not isinstance(text, str):
        return {"ok": False, "error": "请求格式不对"}
    config, _ = _config(relay)
    path = ytlocal.cookies_path(_paths(relay).root, config.youtube_cookies)
    try:
        count = ytlocal.save_cookies(path, text)
    except ytlocal.SettingsError as error:
        return {"ok": False, "error": str(error)}
    except OSError as error:
        return {"ok": False, "error": f"存不了 cookies：{type(error).__name__}"}
    relay.log(f"导入了 YouTube cookies（{count} 条 youtube.com）")
    relay._youtube_health = None
    _forget_subscriptions(relay)
    return {"ok": True, "cookies": ytlocal.cookies_summary(path)}


def _forget_subscriptions(relay) -> None:
    """换了（删了）cookies：缓存的订阅列表是原来那个账号的，不要了（ytaccount.py）。"""
    subs = getattr(relay, "_yt_subs", None)
    if subs is not None:
        subs.forget()


def _delete_cookies(relay) -> dict:
    from ..selfhost import ytlocal
    config, _ = _config(relay)
    path = ytlocal.cookies_path(_paths(relay).root, config.youtube_cookies)
    if ytlocal.delete_cookies(path):
        relay.log("删除了 YouTube cookies")
    _forget_subscriptions(relay)
    return {"ok": True, "cookies": ytlocal.cookies_summary(path)}


def get(handler, relay, path: str) -> bool:
    from . import ytaccount
    if ytaccount.get(handler, relay, path):
        return True
    if path != PREFIX + "settings":
        return False
    if not _allowed(handler, relay):
        handler._not_found()
        return True
    handler._json(settings(relay))
    return True


POSTS = ("settings", "detect", "test", "components", "cookies", "cookies/delete")


def post(handler, relay, path: str) -> bool:
    from . import ytaccount
    if ytaccount.post(handler, relay, path):
        return True
    if not path.startswith(PREFIX) or path[len(PREFIX):] not in POSTS:
        return False
    name = path[len(PREFIX):]
    body = _read_body(handler)
    if not _allowed(handler, relay):
        handler._not_found()
        return True
    if body is None and name in ("settings", "components", "cookies"):
        handler._json({"ok": False, "error": "请求格式不对（cookies 文件太大时也会这样）"}, 400)
        return True
    if name == "settings":
        handler._json(_save(relay, body))
    elif name == "detect":
        handler._json(_detect(relay))
    elif name == "test":
        handler._json(_test(relay))
    elif name == "components":
        handler._json(_components(relay, body))
    elif name == "cookies":
        handler._json(_cookies(relay, body))
    else:
        handler._json(_delete_cookies(relay))
    return True
