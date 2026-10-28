"""中转的两个 HTTP 服务：给电视服务的播放流，和给手机的管理网页。

两者都只监听 127.0.0.1，且分开端口：播放流（/ch/<id>）只给本机的电视服务用，不需要密钥；
管理网页经 nginx 的 /TV/cast/ 转进来，每个请求都要带密钥，密钥不对一律 404，
外面看不出这里有服务。nginx 只转发到管理端口，所以从外网到不了播放流。

多用户（给了 accounts，见 server/tenancy 和 tenants.py）时：每个请求先认出调用者（管理员、
用户、某台设备的访客），只能看到、遥控自己的设备；订阅和进度按用户存，投屏位按设备存；
播放流地址带设备 id（/ch/<设备>/<台>），每台设备一套文件（devices/<设备>/）。
"""

from __future__ import annotations

import hmac
import http.cookies
import json
import re
import secrets
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .. import channel_groups, listen as listen_audio, now_playing
from ..tenancy import db as tenancy_db, files as tenancy_files
from ..tenancy.claimguard import ClaimGuard
from ..tenancy.phonelinks import PhoneLinks
from ..tenancy.signup import SignUp
from ..tenancy.tickets import Limited, LoginTickets
from . import admin, bilisearch, console, epg, events, home, setup, stream, table, voice, voicecmds, \
    vod, watch as watch_pc, wifi as wifi_api, youtube as youtube_links
from . import accountfeeds, bilidash, bililogin, follows, static, ytsearch, ytsettings, ytvod
from . import library as library_api
from . import localsearch
from .bilibili import EPISODE_MEMBER_HINT, PC_QUALITY, PC_QUALITY_VIP, REFERER as BILIBILI_REFERER, \
    UA as BILIBILI_UA, BilibiliError, MemberOnly, SourceError, parse_series_link, pgc_refusal
from .state import KINDS, RESUME_BACK_S, SESSION_S, CastSlot, State, Video
from .tenants import LEGACY, Caller, DeviceStore, NotFound, NullStore, UserStore

# 一次请求里最多跳过几个放不了的视频，之后返回 503，让电视服务稍后再来。
MAX_SKIPS = 3
# 同一视频连续几次刚起播就失败（例如地址过期、被拒），就当作放不了。
MAX_QUICK_FAILURES = 3
QUICK_FAILURE_S = 5.0
MAX_BODY_BYTES = 4096
_CHANNEL = re.compile(r"^/ch/(up\d\d|sr\d\d|cast)$")
# 多用户：/ch/<设备 id>/<台>
_DEVICE_CHANNEL = re.compile(r"^/ch/([0-9a-f]{12}|legacy-[a-z0-9]{1,20})/(up\d\d|sr\d\d|cast)$")
# 电视服务问「这一路流放的是什么」（标题、第几集）：播放流地址把 /ch/ 换成 /info/。
_INFO = re.compile(r"^/info/(up\d\d|sr\d\d|cast)$")
_DEVICE_INFO = re.compile(r"^/info/([0-9a-f]{12}|legacy-[a-z0-9]{1,20})/(up\d\d|sr\d\d|cast)$")
_CREATOR = re.compile(r"^/api/(?:creators|series)/(up\d\d|sr\d\d)$")
_EPISODES = re.compile(r"^/api/series/(sr\d\d)/episodes$")
_EPISODE_PLAY = re.compile(r"^/api/series/(sr\d\d)/play$")
_HISTORY = re.compile(r"^/api/history/(bilibili|youtube)/([A-Za-z0-9_-]{1,32})$")
_CODE = re.compile(r"^/r/([A-Za-z2-7]{20})$")
# 多用户：扫设备遥控码、绑定码、邀请链接的落地页（同一个页面，按路径分辨）。
_JOIN = re.compile(r"^/(?:r|b|i)/([A-Za-z2-7]{20})$")
_INVITE_INFO = re.compile(r"^/api/invite/([A-Za-z2-7]{20})$")
_BIND_INFO = re.compile(r"^/api/bind/([A-Za-z2-7]{20})$")
# 自建（--selfhost）的绑定码是 6 位数字（server/tenancy/db.py 的 new_short_claim）：只在自建时认这两条。
_JOIN_SHORT = re.compile(r"^/b/([0-9]{6})$")
_BIND_INFO_SHORT = re.compile(r"^/api/bind/([0-9]{6})$")
_DEVICE_API = re.compile(r"^/api/devices/([0-9a-f]{12}|legacy-[a-z0-9]{1,20})(?:/(unbind|repair))?$")
# 电脑版（媒体中心界面）的台标：只报台 id，地址只来自频道表的 meta（logos.py）。
_LOGO = re.compile(r"^/api/art/logo/([A-Za-z0-9_-]{1,15})$")
# 直播台的真画面封面（linecheck.py 在后台检测线路时取的一帧）：只报台 id，只读数据目录的 covers/live/。
_LIVE_COVER = re.compile(r"^/api/art/live/([A-Za-z0-9_-]{1,15})$")
# 只作用在一台设备上的遥控接口。多用户时，带密钥（X-Key）并点名设备的这些请求——电视服务替
# 说话的那台设备发的语音命令——按那台设备的访客处理：只能动那一台，按设备主人的剧和进度来，
# 碰不到账号数据（例外：语音搜到的、或正在投屏的 UP 主可以订阅到主人名下，用户决定的；设备上确认过的
# 语音删台，见 voice.py、voicecmds.py）。
_DEVICE_SCOPED = frozenset({"/api/switch", "/api/seek", "/api/next", "/api/previous", "/api/device",
                            "/api/audio", "/api/pet", "/api/pause", "/api/resume", "/api/voice/catalog",
                            "/api/voice/search", "/api/voice/cast", "/api/voice/creator", "/api/sleep"}
                           | set(voicecmds.PATHS))
COOKIE = "tvs"
# 中转推出的内容要经电视服务缓冲约这么久才在小智屏幕上出现（实测音频领先约 2.7 秒，加上解码起步）。
PLAYBACK_LAG_S = 4.0
# 跳转后多久内，新流还没起来也按跳转目标继续累加。
SEEK_BRIDGE_S = 15.0
# 暂停最长多久（与电视服务的 PAUSE_MAX_S 一致），之后自动继续。
PAUSE_MAX_S = 30 * 60
# 最多每分钟问一次 YouTube 节点的健康状态。
YOUTUBE_HEALTH_S = 60
# CONFIG 包里除频道列表外还有会话号、遥控地址等，加台时给它们留的字节数（和电视服务截断设备列表时一样）。
CONFIG_SPARE_BYTES = channel_groups.CONFIG_SPARE_BYTES
# 剧集台的名字最多这么多个字符；手机上可以自己起名。
SERIES_NAME_MAX = 20
# 在电脑上看（直接看一个台）时的页面编号：页面自己随机生成，同一个页面换台只占一路。
_VIEWER = re.compile(r"^[0-9a-f]{8,16}$")
# 电脑上看到的位置大约比片源转出来的晚这么多秒（播放器从倒数第三段起放，见 console.html 的 WatchPlayer）。
PC_BEHIND_S = 6.0
# 投到电脑的视频的信息（标题、时长、各集）留多久、最多留几个：换集、跳转时不再访问网站。
PC_ITEM_KEEP_S = 1800
PC_ITEMS_MAX = 64
# 播放器给的秒数（跳转的目标、报的位置）最大多少：再大就是请求不对。
PC_SECONDS_MAX = 7 * 24 * 3600
# 电脑上看 B 站视频的清晰度：720p 是点播（vod.py，原文件，秒开、拖动即跳、能倍速）；1080p 要登录 B 站，
# 走 watch.py（ffmpeg → HLS，跳到缓冲外要重连）。页面记住选的哪个，请求里带 quality。
QUALITY_CHOICES = ("720p", "1080p")
# 没有设备时顶替的「设备」：不在线，什么事件也不写。
NO_DEVICE = {"id": "", "name": "小电视", "now": None, "events": None, "channels": None, "groups": None,
             "remote": None, "owner": None, "model": "", "slot": "", "legacy": False}


def _resume_at(offset: float, outcome: str, elapsed: float) -> float:
    """要存的进度。重新打开时从「存的进度 − RESUME_BACK_S」开始。

    换台走开（client_gone）再回来，往回退一点好接上前情；片源中途断了（failed）时，电视服务
    还在同一个会话里等着接上，画面不该倒回去重放，所以把回退量加回来，正好从断的地方接。
    """
    position = offset + elapsed
    return position + RESUME_BACK_S if outcome == "failed" else position


class Prefer(str):
    """页面会放点播时它要什么：本身是 B 站的清晰度（"720p" / "1080p"，和以前的字符串一样比较）；
    dash：页面能用 MediaSource 放 H.264 + AAC 的分片 MP4（YouTube 的 DASH，ytvod.py）；yt：YouTube 要的清晰度。"""
    dash: bool = False
    yt: str = ytvod.DEFAULT_QUALITY


def _prefer(body: dict) -> str | None:
    """请求里页面会不会放点播（vod: true，新的电脑版才带；旧版只会放 HLS）、要哪个清晰度：None，或者 Prefer
    （"720p" 或 "1080p"，再带着 dash、yt：新的电脑版带 dash: true 和 ytq）。"""
    if not isinstance(body, dict) or body.get("vod") is not True:
        return None
    prefer = Prefer("1080p" if body.get("quality") == "1080p" else "720p")
    prefer.dash = body.get("dash") is True
    prefer.yt = body.get("ytq") if body.get("ytq") in ytvod.QUALITIES else ytvod.DEFAULT_QUALITY
    return prefer


def _seconds(value):
    """播放器给的秒数：None（没给）、非负的 float，或者 False（格式不对）。"""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value \
            or not 0 <= value <= PC_SECONDS_MAX:
        return False
    return float(value)


def _clamp(position: float, duration: float) -> float:
    """跳转的目标：不早于 0，知道时长时不晚于结尾前 3 秒（再往后就是放完了）。"""
    position = max(0.0, float(position))
    return min(position, max(0.0, float(duration) - 3)) if duration else position


class _LibraryPlay:
    """片库的视频在电脑上放（library.py）：没有转发的那一路，浏览器自己按 Range 取文件；只有一个编号，
    让播放器报的进度对得上这一次（_pc_page 比 sid）。"""

    def __init__(self, sid: str):
        self.sid = sid


def _relay_channel(cid: str) -> bool:
    """中转推流的台：投屏、UP 主、剧集。"""
    return cid == table.CAST_ID or (cid[:2] in ("up", "sr") and cid[2:].isdigit())


class Relay:
    """播放与管理共用的逻辑；HTTP 处理类只做解析和回写。

    遥控类的方法都带 caller（谁在调用）和可选的 device（要遥控哪一台）；不给就是单用户模式的
    LEGACY 和「自动」选的那台，和以前一样。stream 一侧的方法带 slot：单用户时是空串（所有设备
    共用一份状态，和以前一样），多用户时是设备 id。
    """

    def __init__(self, state: State | None, client, channels_out: Path | None, ffmpeg: str, key: str,
                 events_out: Path | None = None, youtube=None,
                 main_channels: Path | None = None, groups_out: Path | None = None,
                 now_playing_in: Path | None = None, remote_url_out: Path | None = None,
                 public_url: str = "",
                 device_name: str = "小智", extra_devices=(),
                 log=print, clock=time.time, monotonic=time.monotonic,
                 starter=stream.start, pumper=stream.pump,
                 accounts=None, devices_dir: Path | None = None, listen_secret: bytes | None = None,
                 selfhost: "setup.SelfHost | None" = None):
        self.state = state
        # 自建模式（server/selfhost，中转带 --selfhost）：第一个管理员、明文 HTTP 的 cookie 等，见 setup.py。
        self.selfhost = selfhost
        # 多用户：账号库（server/tenancy/accounts.py）和每台设备的文件目录。
        self.accounts = accounts
        self.devices_dir = Path(devices_dir) if devices_dir is not None else None
        # 电脑扫码登录的票据（阶段 2，console.py）。
        self.login_tickets = LoginTickets(accounts) if accounts is not None else None
        # 反方向：电脑版给手机的一次性登录链接（server/tenancy/phonelinks.py）。
        self.phone_links = PhoneLinks(accounts) if accounts is not None else None
        # 没登录的手机扫未绑定设备的绑定码：建账号并绑定（server/tenancy/signup.py）。
        self.signup = SignUp(accounts) if accounts is not None else None
        # 自建的绑定码只有 6 位数字：收绑定码的接口先过错码限流（server/tenancy/claimguard.py）。云端没有。
        self.claim_guard = (ClaimGuard(accounts, log=lambda message: self.log(message))
                            if accounts is not None and selfhost is not None else None)
        # 每个网站一个客户端；YouTube 只经 cstone 节点，没配置时为 None。
        self.clients = {"bilibili": client, "youtube": youtube}
        self.events_out = Path(events_out) if events_out is not None else None
        self.channels_out = Path(channels_out) if channels_out is not None else None
        self.groups_out = Path(groups_out) if groups_out else None
        self.remote_url_out = Path(remote_url_out) if remote_url_out else None
        # 单用户：遥控的设备，每台一个电视服务，各写各的「正在播放」、各读各的事件文件。
        # 第一台来自 now_playing_in / events_out，其余是 extra_devices 的 (名字, 正在播放, 事件)。
        shared = {"channels": self.channels_out, "groups": self.groups_out, "remote": self.remote_url_out,
                  "owner": None, "model": "", "slot": "", "legacy": True}
        self._legacy_devices = [dict(shared, id="d0", name=device_name,
                                     now=Path(now_playing_in) if now_playing_in else None,
                                     events=self.events_out)]
        self._legacy_devices += [dict(shared, id=f"d{n}", name=name, now=Path(now) if now else None,
                                      events=Path(out) if out else None)
                                 for n, (name, now, out) in enumerate(extra_devices, start=1)]
        # 手机选定遥控哪一台，按调用者分别记；没有记录（None）是自动（跟着最近换过台的在线设备）。
        self._targets: dict[tuple, str | None] = {}
        self.ffmpeg = ffmpeg
        self.key = key
        # 「用手机听」的令牌密钥（server/listen.py）：默认由中转密钥派生，电视服务从同一个文件派生出同一个。
        self.listen_secret = listen_secret if listen_secret is not None else listen_audio.derive_secret(key)
        self.log = log
        self.clock = clock
        self.monotonic = monotonic
        # 在电脑上看（watch.py）：只在自建 / 电脑客户端开；云端的小主机上行带宽不够，没有这个功能。
        self.watch = (watch_pc.WatchService(ffmpeg, watch_pc.derive_secret(key), log=lambda m: self.log(m))
                      if selfhost is not None and accounts is not None else None)
        if self.watch is not None:
            # 直播台的备用线路和上次好用的线路记在全局频道表旁边的 meta 里（server/routes.py）。
            self.watch.routes_path = lambda: self.main_channels
        # 点播（vod.py）：B 站的视频在电脑上直接放原文件（浏览器的 <video>，中转按 Range 转发），名额和 watch 一起算。
        self.vod = (vod.VodService(vod.derive_secret(key), watch=self.watch, log=lambda m: self.log(m))
                    if self.watch is not None else None)
        # YouTube 的点播（ytvod.py）：取地址的缓存、预取、经 YouTube 代理的上游；只在本机模式用（_yt_vod_on）。
        self.ytvod = (ytvod.YtVod(lambda: self.clients.get("youtube"), proxy=lambda: self.youtube_proxy(),
                                  log=lambda m: self.log(m))
                      if self.vod is not None else None)
        # 很长的 B 站视频在电脑上按 DASH 放（bilidash.py）：索引缓存和到 B 站视频主机的连接（复用）。
        self.bilidash = bilidash.BiliDash(log=lambda m: self.log(m)) if self.vod is not None else None
        # 电脑上直接看 UP 主、剧集、投屏视频时各自的进度（和小电视的分开）：键是 (用户, 台) 或
        # (用户, "v:<网站>:<id>")，值是 {"bvid", "offset"（这一路从第几秒起）, "stream"（那一路，估计看到了哪里）,
        # 和存进度用的 title、name、duration、index、total、site}。存进数据目录的 pc-progress.json（home.PcProgress），
        # 「继续观看」显示它，重启后电脑上接着看也从那里起；从不改小电视的进度。
        self._pc_cursor: dict[tuple, dict] = {}
        # 每个页面（用户, 页面编号）正在直接看的那一项的键：播放器报位置（watch_progress）时找游标用。
        self._pc_pages: dict[tuple, str] = {}
        # 投到电脑的视频的信息和各集：(网站, id) → (取到的时刻, _pc_cast 的结果)，见 PC_ITEM_KEEP_S。
        self._pc_items: dict[tuple, tuple] = {}
        self.pc_progress = (home.PcProgress(Path(accounts.path).parent / "pc-progress.json", clock=clock)
                            if self.watch is not None else None)
        # 首页（home.py）按调用者缓存几秒；「播放到」以后马上作废。
        self._home_cache: dict[tuple, tuple] = {}
        self._home_lock = threading.Lock()
        # 直播台的台标缓存（logos.py）：__main__ 建，存在数据目录的 logos/；没建时台标接口一律 404。
        self.logos = None
        # 直播台线路的后台自检和真画面封面（linecheck.py）：自建 / 电脑客户端由 __main__ 建，云端为 None。
        self.linecheck = None
        # 直播源订阅（subscriptions.py）：自建 / 电脑客户端由 __main__ 建，云端为 None。
        self.subscriptions = None
        # 我的片库（library.py，网盘经 WebDAV 和本机文件夹）：自建 / 电脑客户端由 __main__ 建，云端为 None。
        self.library = None
        # 每个人常看的直播台（livetally.py，直播页的「常看」分组）：由 __main__ 建。
        self.tally = None
        self.starter = starter
        self.pumper = pumper
        self._quick_failures: dict[tuple, int] = {}
        # 刷新列表要访问 B 站，同一时间只让一个请求去做。
        self._refresh_lock = threading.Lock()
        # 手机遥控：直播频道表（只读）；分组调整单用户时只由这里写 groups_out。
        self.main_channels = Path(main_channels) if main_channels else None
        self.public_url = public_url.rstrip("/")
        # 每个台最后起播的视频标题，给手机上的「正在播放」用；键是 (slot, 台)。
        self._titles: dict[tuple, str] = {}
        # YouTube 节点的健康状态，最多每分钟问一次。
        self._youtube_health: tuple[float, str] | None = None
        # 每个台正在推的那一路流：进程、视频、起点、开始时刻；跳转、下一个、暂停靠它掐断。键是 (slot, 台)。
        self._active: dict[tuple, dict] = {}
        self._active_lock = threading.Lock()
        # 手机上暂停的台，每个 slot 至多一个：台、视频、精确位置、暂停时刻。暂停期间这个台不给流。
        self._paused: dict[str, dict] = {}
        # 跳转后、新流起来之前（约 2–5 秒）：记下目标，连着点「进 30 秒」就在它上面累加。键是 (slot, 台)。
        self._bridge: dict[tuple, dict] = {}
        # 多用户：每个用户的状态、每台设备的投屏位（读一次，之后在内存里改、存回数据库）。
        self._users: dict[int, State] = {}
        self._casts: dict[str, CastSlot] = {}
        self._cache_lock = threading.RLock()
        # 电脑版「订阅」页的 B 站搜索和封面缩略图（bilisearch.py）：缓存和次数限制。
        self.bili_search = bilisearch.BiliSearch(clock=monotonic, log=lambda message: self.log(message))
        self.bili_search.youtube_proxy = self.youtube_proxy
        # 电脑版「搜索」页的 YouTube 搜索（ytsearch.py）：缓存、次数限制，同时只让一个搜索去节点。
        self.yt_search = ytsearch.YtSearch(clock=monotonic, log=lambda message: self.log(message))
        # 每个用户自己的 B 站登录（bililogin.py）：只在自建 / 电脑客户端有，存在数据目录的 secrets/bilibili/。
        self.bili_accounts = (bililogin.BiliAccounts(Path(accounts.path).parent / "secrets" / "bilibili",
                                                     log=lambda message: self.log(message))
                              if selfhost is not None and accounts is not None else None)
        # 设备文件上次写的样子（主人、遥控码），变了就重写。
        self._synced: dict[str, tuple] = {}
        # 多用户：给设备加密 Wi-Fi 修改用的 X25519 私钥（wifi.setup_key 在启动时读进来）。
        self.wifi_private: bytes | None = None
        # 节目单（epg.py）：多用户时 __main__ 用 epg.attach 建；没打开时它不上网、不占内存。
        self.guide: "epg.Guide | None" = None
        # 首页上自己账号的几排、「我的关注」的同步、B 站历史的回写（accountfeeds.py、follows.py）：
        # 只在自建 / 电脑客户端有（和 B 站登录一样），云端为 None。
        self.account_feeds = accountfeeds.attach(self)

    @property
    def tenant(self) -> bool:
        return self.accounts is not None

    # ---- 设备与调用者 ----

    def _device_entry(self, row: dict) -> dict:
        files = tenancy_files.device_files(self.devices_dir, row["id"])
        return {"id": row["id"], "name": row["name"] or row["id"], "now": files.now, "events": files.events,
                "channels": files.channels, "groups": files.groups, "remote": files.remote,
                "owner": row.get("owner"), "model": row.get("model") or "", "slot": row["id"],
                "legacy": row.get("legacy_port") is not None, "fw": row.get("fw") or "",
                "remote_code": row.get("remote_code") or ""}

    @property
    def devices(self) -> list[dict]:
        if not self.tenant:
            return self._legacy_devices
        return [self._device_entry(row) for row in self.accounts.active_devices()]

    def devices_for(self, caller: Caller = LEGACY) -> list[dict]:
        """调用者能遥控的设备：单用户时是全部；访客只有那一台；用户是自己（主人或成员）的。"""
        if not self.tenant:
            return self._legacy_devices
        if caller.guest:
            return [d for d in self.devices if d["id"] == caller.device]
        mine = {row["id"] for row in self.accounts.devices_of(caller.user)} if caller.user is not None else set()
        return [d for d in self.devices if d["id"] in mine]

    def _pick(self, caller: Caller = LEGACY, device: str | None = None) -> dict:
        """要遥控的那台：点名的（不是自己的就 NotFound），否则是 target()。"""
        if device:
            chosen = self.find_device(device, caller)
            if chosen is None:
                raise NotFound(device)
            return chosen
        return self.target(caller)

    def find_device(self, key, caller: Caller = LEGACY) -> dict | None:
        """调用者点名的设备：按 id；单用户时也认名字（--device-name、--extra-device 的名字，
        电视服务的 TV_RELAY_DEVICE 用的就是它）。多用户时名字是用户起的、不唯一，只认 id。"""
        if not isinstance(key, str) or not key:
            return None
        mine = self.devices_for(caller)
        found = next((d for d in mine if d["id"] == key), None)
        if found is None and not self.tenant:
            found = next((d for d in mine if d["name"] == key), None)
        return found

    def _state_of(self, dev: dict) -> State:
        """设备主人的订阅和进度（单用户时就是那一份状态）。"""
        if not self.tenant:
            return self.state
        if dev.get("owner") is None:
            return State(store=NullStore())
        return self.user_state(dev["owner"])

    def user_state(self, user_id: int) -> State:
        with self._cache_lock:
            state = self._users.get(user_id)
            if state is None:
                state = self._users[user_id] = State(store=UserStore(self.accounts, user_id))
            return state

    def _cast_of(self, dev: dict):
        """设备的投屏位：单用户时所有设备共用 State 里那一个，和以前一样。"""
        if not self.tenant:
            return self.state
        if not dev.get("id"):
            return CastSlot(NullStore())
        with self._cache_lock:
            slot = self._casts.get(dev["id"])
            if slot is None:
                slot = self._casts[dev["id"]] = CastSlot(DeviceStore(self.accounts, dev["id"]))
            return slot

    def _account(self, caller: Caller) -> State:
        """账号级的状态（订阅、剧集、投屏历史、分组）：访客没有，NotFound。"""
        if not self.tenant:
            return self.state
        if caller.guest or caller.user is None:
            raise NotFound("account")
        return self.user_state(caller.user)

    def _stream_device(self, slot: str) -> dict | None:
        """播放流请求对应的设备。单用户时是此刻遥控的那台（只用来写事件），和以前一样。"""
        if not self.tenant:
            return self.target(LEGACY)
        return next((d for d in self.devices if d["id"] == slot), None)

    # ---- 频道表 ----

    def write_table(self) -> None:
        if self.tenant:
            self.sync_devices(force=True)
            return
        with self.state.lock:
            text = table.render(self.state.creators)
        if table.write_if_changed(self.channels_out, text):
            self.log(f"频道表已更新：{len(self.state.creators)} 个台")

    def write_device_files(self, dev: dict) -> None:
        """多用户：一台设备的频道表、分组、遥控地址，写在 devices/<id>/ 下给电视服务读。"""
        tenancy_files.ensure_dir(self.devices_dir, dev["id"])
        state = self._state_of(dev)
        with state.lock:
            text = table.render(state.creators, base=f"{table.STREAM_BASE}{dev['id']}/")
            groups = json.dumps(self._overrides(state), ensure_ascii=False, indent=1)
            count = len(state.creators)
        if table.write_if_changed(dev["channels"], text):
            self.log(f"{dev['id']} 频道表已更新：{count} 个台")
        table.write_if_changed(dev["groups"], groups)
        if self.public_url and dev.get("remote_code"):
            table.write_if_changed(dev["remote"], self.remote_url(dev) + "\n")

    def sync_devices(self, force: bool = False, owner: int | None = None) -> None:
        """多用户：新出现的设备、换了主人或遥控码的设备，重写它的文件；force 时全部重写。

        管理命令（server/tenancy/admin.py）直接改数据库，中转靠这个（每 10 秒、以及每次需要时）跟上。
        换了主人的设备，内存里的投屏位作废，免得上一个主人投的视频留给下一个。
        """
        if not self.tenant:
            return
        for dev in self.devices:
            signature = (dev["owner"], dev["remote_code"], dev["name"])
            previous = self._synced.get(dev["id"])
            if previous is not None and previous[0] != dev["owner"]:
                with self._cache_lock:
                    self._casts.pop(dev["id"], None)
            if force or previous != signature or (owner is not None and dev["owner"] == owner):
                try:
                    self.write_device_files(dev)
                    self._synced[dev["id"]] = signature
                except OSError as error:
                    self.log(f"{dev['id']} 写设备文件失败：{error}")

    def _tables_changed(self, state: State) -> None:
        """订阅或分组变了：重写用到这份状态的设备的频道表。"""
        if not self.tenant:
            self.write_table()
            return
        owner = next((uid for uid, s in self._users.items() if s is state), None)
        if owner is not None:
            self.sync_devices(owner=owner)

    # ---- 播放 ----

    def _stale(self, creator) -> bool:
        return creator is not None and \
            self.clock() - creator.fetched_at >= KINDS[creator.kind][3]

    def refresh_if_stale(self, cid: str, state: State | None = None, owner: int | None = None) -> None:
        """owner：这份状态的主人（多用户）。他用大会员登录了 B 站时，番剧的会员集也留着（bililogin.py）。"""
        state = state if state is not None else self.state
        if not self._stale(state.get(cid)):
            return
        with self._refresh_lock:
            creator = state.get(cid)
            if not self._stale(creator):
                return
            login = self._vip_login(owner) if creator.kind == "series" and creator.site == "bilibili" else None
            # 多用户：同一个 UP 主（剧）被好几个人订阅时，列表只向网站要一次（共享缓存）。
            # 大会员的剧集列表里有会员集，别人放不了：不用也不写共享缓存。
            if self.tenant and login is None:
                cached = self.accounts.cached_source(creator.site, creator.source, creator.kind)
                if cached is not None and self.clock() - cached["fetched_at"] < KINDS[creator.kind][3]:
                    state.set_videos(cid, [Video(**v) for v in cached["videos"]], cached["fetched_at"])
                    state.save()
                    return
            try:
                client = self._client(creator.site)
                if creator.kind == "series":
                    videos = (client.series(creator.source, login=login) if login is not None
                              else client.series(creator.source))[1]
                else:
                    videos = client.uploads(creator.source)
            except SourceError as error:
                # 拿不到就继续用缓存的列表。
                self.log(f"{cid} 刷新视频列表失败，用缓存：{error}")
                return
            if videos:
                now = self.clock()
                state.set_videos(cid, videos, now)
                if creator.kind == "up":
                    state.set_total(cid, self._upload_total(creator.site, creator.source))
                state.save()
                if login is None:
                    self._cache_videos(creator, videos, now)

    def _upload_total(self, site: str, source: str) -> int:
        """UP 主一共有多少个投稿：客户端最近一次取列表时网站说的（B 站有，YouTube 节点没有），不知道为 0。"""
        getter = getattr(self.clients.get(site), "upload_total", None)
        try:
            total = getter(source) if getter is not None else 0
        except (TypeError, ValueError):
            return 0
        return total if isinstance(total, int) and not isinstance(total, bool) and total > 0 else 0

    def _cache_videos(self, creator, videos, now: float) -> None:
        if self.tenant:
            self.accounts.cache_source(creator.site, creator.source, creator.kind, creator.name,
                                       [v.__dict__ for v in videos], now)

    def open_channel(self, cid: str, slot: str = ""):
        """选出下一个视频并启动 ffmpeg；返回 (进程, 视频, 起点) 或说明原因的 HTTP 码。"""
        dev = self._stream_device(slot)
        if dev is None:
            return 404
        state = self._state_of(dev)
        if state.get(cid) is None:
            return 404
        if self._paused_on(dev, cid):
            return 503
        self.refresh_if_stale(cid, state, dev.get("owner"))
        for _ in range(MAX_SKIPS):
            pick = state.next_video(cid)
            if pick is None:
                return 503
            video, offset = pick
            try:
                return self._start(video, offset, dev.get("owner")), video, offset
            except SourceError as error:
                if not error.permanent:
                    self.log(f"{cid} {video.bvid} 暂时取不到：{error}")
                    return 503
                self.log(f"{cid} {video.bvid} 放不了，跳过：{error}")
                state.mark_watched(cid, video.bvid)
                state.save()
        return 503

    def _client(self, site: str):
        client = self.clients.get(site)
        if client is None:
            raise SourceError(f"{site} 还没配置")
        return client

    def _start(self, video, offset: float, owner: int | None = None):
        """起播：B 站在本机用 ffmpeg 换封装；YouTube 由 cstone 节点转好码，这里只转发。
        owner：设备主人；他登录了 B 站（bililogin.py）就带着他的 cookies 取地址（清晰度还是小电视的）。
        片库的视频（投屏位里 site 是 "library"）：ffmpeg 读只给本机的 raw 地址，缩成电视服务要的画面（library.tv_start）。"""
        if video.site == "library":
            return library_api.tv_start(self, video.bvid, offset)
        if video.site == "youtube":
            return self._client("youtube").open(video.bvid, offset)
        url = self._bili_url(video.bvid, self.bili_login(owner), owner)
        return self.starter(self.ffmpeg, url, offset)

    # ---- B 站登录（自建 / 电脑客户端，bililogin.py） ----

    def bili_login(self, user: int | None):
        """这个用户的 B 站登录（bililogin.Login）；没开这个功能、没登录、登录失效了是 None。"""
        if self.bili_accounts is None or user is None:
            return None
        return self.bili_accounts.login_for(user)

    def _vip_login(self, user: int | None):
        """大会员的登录（剧集列表要不要留会员集），否则 None。"""
        login = self.bili_login(user)
        return login if login is not None and login.vip else None

    def _bili_url(self, bvid: str, login, user: int | None, quality: int | None = None) -> str:
        """B 站的播放地址；不登录时和以前一模一样地调用客户端。会员集只给试看、而这个人存过登录时，
        核对一次登录：已经失效了就当作暂时放不了（503，重新扫码后就好），别把这一集当成看过跳过去。"""
        client = self._client("bilibili")
        if login is None:
            if user is None or self.bili_accounts is None or self.bili_accounts.record(user) is None:
                return client.media_url(bvid)
            try:
                return client.media_url(bvid)
            except MemberOnly as error:
                if self.bili_accounts.recheck(user):
                    raise SourceError(f"{error}（B 站登录已失效，请重新扫码）") from None
                raise
        try:
            return client.media_url(bvid, login=login, quality=quality) if quality else \
                client.media_url(bvid, login=login)
        except MemberOnly as error:
            if self.bili_accounts is not None and self.bili_accounts.recheck(user):
                raise SourceError(f"{error}（B 站登录已失效，请重新扫码）") from None
            raise

    def finish(self, cid: str, video, offset: float, outcome: str, elapsed: float, slot: str = "") -> None:
        dev = self._stream_device(slot) or NO_DEVICE
        state = self._state_of(dev)
        self._report_history(dev, video, offset, outcome, elapsed)
        key = (slot, cid, video.bvid)
        if outcome == "ended":
            self._quick_failures.pop(key, None)
            state.mark_watched(cid, video.bvid)
        elif outcome == "failed" and elapsed < QUICK_FAILURE_S:
            count = self._quick_failures.get(key, 0) + 1
            self._quick_failures[key] = count
            if count >= MAX_QUICK_FAILURES:
                self.log(f"{cid} {video.bvid} 连续 {count} 次起播失败，跳过")
                self._quick_failures.pop(key, None)
                state.mark_watched(cid, video.bvid)
        else:
            self._quick_failures.pop(key, None)
            state.save_position(cid, video.bvid, _resume_at(offset, outcome, elapsed))
        state.save()

    def _report_history(self, dev: dict, video, offset: float, outcome: str, elapsed: float) -> None:
        """小电视上一路停了（看完、换台、断了）：设备主人打开了「同步到 B 站历史」时报给他的 B 站账号（accountfeeds.py）。"""
        if self.account_feeds is None or video is None or not dev.get("owner"):
            return
        position = (video.duration or offset + elapsed) if outcome == "ended" else offset + elapsed
        try:
            self.account_feeds.report(dev["owner"], video.site, video.bvid, position, final=True)
        except Exception as error:  # noqa: BLE001 —— 回写失败不能影响播放
            self.log(f"回写 B 站历史出错：{type(error).__name__}")

    def _pc_history(self, caller: Caller, body: dict, final: bool) -> None:
        """电脑上看到的位置：看的人打开了「同步到 B 站历史」时报给他的 B 站账号（accountfeeds.py）。"""
        if self.account_feeds is None or caller.user is None:
            return
        _, cursor = self._pc_page(caller, body)
        if cursor is None or not cursor.get("bvid") or cursor.get("site") == "library":
            return
        try:
            self.account_feeds.report(caller.user, cursor.get("site") or "bilibili", cursor["bvid"],
                                      self._pc_position(cursor), final=final)
        except Exception as error:  # noqa: BLE001
            self.log(f"回写 B 站历史出错：{type(error).__name__}")

    def _event(self, dev: dict | None, action: str, channel: str = "", value: int | None = None,
               text: str = "", total: int = 0) -> None:
        if dev is None or dev["events"] is None:
            return
        try:
            seq = events.write_event(dev["events"], action, channel=channel, value=value, text=text,
                                     total=total)
            self.log(f"事件 {seq} {dev['name']} {action} "
                     f"{channel or ('' if value is None else value)}".rstrip())
        except OSError as error:
            self.log(f"写事件失败：{error}")

    def _describe(self, state: State, cid: str, video) -> dict | None:
        """设备上「正在加载 / 正在播」要画的：台、标题；剧集再加第几集、共几集。"""
        if video is None:
            return None
        info = {"channel": cid, "title": video.title}
        creator = state.get(cid) if cid != table.CAST_ID else None
        if cid == table.CAST_ID and getattr(video, "site", "") == "library":
            # 片库的剧集：第几集、共几集（library.tv_episode）。
            info.update(library_api.tv_episode(self, video))
        if creator is not None and creator.kind == "series":
            n = self._episode_of(state, cid, video)
            if n:
                with state.lock:
                    info.update(episode=n, total=len(creator.videos))
        return info

    def stream_info(self, cid: str, slot: str = "") -> dict | None:
        """这一路流放的是什么：正在推的那个视频；还没推（或刚被跳转、下一个掐断）时，是现在打开
        会选的那个（只看不改、不访问网站）。电视服务在会话开始、片源重开时问，转给设备。

        进度条用的两项：duration（视频总长，秒；不知道时没有）；正在推流时再加 offset（这一路流从
        第几秒推起，秒），电视服务据此告诉设备它的进度（server/progress.py）。
        """
        dev = self._stream_device(slot)
        if dev is None:
            return None
        state = self._state_of(dev)
        with self._active_lock:
            entry = self._active.get((slot, cid))
        if entry is not None and entry["abort"]:
            entry = None
        if cid == table.CAST_ID:
            cast = self._cast_of(dev)
            with cast.lock:
                video = cast.cast
        elif state.get(cid) is None:
            return None
        else:
            video = entry["video"] if entry is not None else state.peek_video(cid)
        info = self._describe(state, cid, video)
        if info is None:
            return None
        duration = int(getattr(video, "duration", 0) or 0)
        if duration > 0:
            info["duration"] = duration
        if entry is not None and entry["video"].bvid == video.bvid:
            info["offset"] = round(float(entry["offset"]), 1)
        return info

    def _loading(self, dev: dict, cid: str, state: State, video) -> None:
        """设备正在播的台马上换片源（下一个、上一集、选集）：先告诉它要加载什么。"""
        info = self._describe(state, cid, video)
        if info is not None:
            self._event(dev, "loading", channel=cid, value=info.get("episode", 0),
                        text=info["title"], total=info.get("total", 0))

    def open_cast(self, slot: str = ""):
        """投屏视频：从保存的进度往回退一点续播；已播完的从头重播。"""
        dev = self._stream_device(slot)
        if dev is None:
            return 404
        cast = self._cast_of(dev)
        with cast.lock:
            video, done, position = cast.cast, cast.cast_done, cast.cast_position
        if video is None:
            return 404
        if self._paused_on(dev, table.CAST_ID):
            return 503
        offset = 0.0 if done else max(0.0, position - RESUME_BACK_S)
        try:
            return self._start(video, offset, dev.get("owner")), video, offset
        except SourceError as error:
            self.log(f"cast {video.bvid} 取不到：{error}")
            return 503

    def finish_cast(self, video, offset: float, outcome: str, elapsed: float, slot: str = "") -> None:
        dev = self._stream_device(slot)
        cast = self._cast_of(dev or NO_DEVICE)
        self._report_history(dev or NO_DEVICE, video, offset, outcome, elapsed)
        if video.site == "library":
            # 片库：记进度；剧集放完一集接着放下一集（"next"），不切回原来的台（library.tv_finished）。
            outcome = library_api.tv_finished(self, dev or NO_DEVICE, video, offset, outcome, elapsed)
            if outcome == "next":
                return
        if outcome == "ended":
            cast.cast_ended(video.bvid)
            cast.save()
            # 让电视服务把设备切回投屏前的台。
            self._event(dev, "return")
            return
        cast.save_cast_position(video.bvid, _resume_at(offset, outcome, elapsed))
        cast.save()

    def play(self, cid: str, handler, slot: str = "") -> None:
        is_cast = cid == table.CAST_ID
        opened = self.open_cast(slot) if is_cast else self.open_channel(cid, slot)
        if isinstance(opened, int):
            handler.send_error(opened)
            return
        proc, video, offset = opened
        dev = self._stream_device(slot) or NO_DEVICE
        state, cast = self._state_of(dev), self._cast_of(dev)
        key = (slot, cid)
        self._titles[key] = video.title
        self.log(f"{slot + '/' if slot else ''}{cid} 开始 {video.bvid} 从 {int(offset)} 秒 《{video.title}》")
        began = self.monotonic()
        entry = {"proc": proc, "video": video, "offset": offset, "began": began,
                 "at": self.clock(), "abort": ""}
        with self._active_lock:
            self._active[key] = entry
            self._bridge.pop(key, None)
        try:
            handler.send_response(200)
            handler.send_header("Content-Type", "video/mp2t")
            handler.send_header("Cache-Control", "no-store")
            handler.end_headers()
        except OSError:
            proc.kill()
            proc.wait()
            return

        ticked = [0.0]

        def tick(elapsed: float) -> None:
            ticked[0] = elapsed
            if entry["abort"]:
                return          # 跳转等已经存好了新位置，别再覆盖
            if is_cast:
                cast.save_cast_position(video.bvid, offset + elapsed)
                cast.save()
                if video.site == "library":
                    # 片库的进度另记一份给「继续观看」（按屏幕上的位置：推出去的要晚 PLAYBACK_LAG_S 秒才到）。
                    library_api.tv_progress(self, dev, video, offset + max(0.0, elapsed - PLAYBACK_LAG_S))
            else:
                state.save_position(cid, video.bvid, offset + elapsed)
                state.save()

        outcome = self.pumper(proc, handler.wfile.write, tick)
        elapsed = max(self.monotonic() - began, ticked[0])
        with self._active_lock:
            if self._active.get(key) is entry:
                del self._active[key]
        if entry["abort"]:
            # 手机上跳转、下一个或暂停掐断的：状态已经由那边改好了，这里不再记进度或标看过。
            self.log(f"{cid} 结束 {video.bvid} {entry['abort']} 播了 {int(elapsed)} 秒")
            return
        if is_cast:
            self.finish_cast(video, offset, outcome, elapsed, slot)
        else:
            self.finish(cid, video, offset, outcome, elapsed, slot)
        self.log(f"{cid} 结束 {video.bvid} {outcome} 播了 {int(elapsed)} 秒")

    # ---- 播放控制 ----

    def _playing_entry(self, dev: dict):
        """(台, 正在推的流)；只对中转推流的台（投屏、UP 主、剧集）有效。"""
        playing = self._playing(dev)
        cid = playing["channel"]
        if not playing["online"] or not _relay_channel(cid):
            return cid, None
        with self._active_lock:
            return cid, self._active.get((dev["slot"], cid))

    def _lag(self, dev: dict) -> float:
        """推出去的内容多久后到小智屏幕上：PLAYBACK_LAG_S，有人用手机听时设备再晚 H 秒（电视服务写的 lag）。"""
        listen = self._playing(dev).get("listen")
        return PLAYBACK_LAG_S + (listen["lag"] if listen else 0.0)

    def _position(self, entry: dict, lag: float = PLAYBACK_LAG_S) -> float:
        """小智此刻大约播到的位置。中转推出去的内容要先经电视服务缓冲，约 lag 秒后才在
        屏幕上出现，所以从推流时长里扣掉它；跳转、暂停也按这个位置算，和画面对得上。"""
        return entry["offset"] + max(0.0, self.monotonic() - entry["began"] - lag)

    def _current(self, dev: dict):
        """(台, 视频, 当前位置, 正在推的流)。刚跳转、新流还没起来时，用跳转目标顶上。"""
        cid, entry = self._playing_entry(dev)
        if entry is not None:
            return cid, entry["video"], self._position(entry, self._lag(dev)), entry
        bridge = self._bridge.get((dev["slot"], cid))
        if bridge is not None and self.clock() - bridge["at"] < SEEK_BRIDGE_S:
            return cid, bridge["video"], bridge["target"], None
        return cid, None, 0.0, None

    def _cut(self, cid: str, entry: dict, reason: str) -> None:
        entry["abort"] = reason
        try:
            entry["proc"].kill()
        except OSError:
            pass
        self.log(f"{cid} {reason}：掐断当前的流，电视服务会重新打开")

    def _paused_on(self, dev: dict, cid: str) -> dict | None:
        """cid 正在暂停时返回暂停信息。暂停超过 PAUSE_MAX_S，或设备已换到别的台，就当作结束了。
        语音定时的暂停（"sleep"，voicecmds.py）最长 SLEEP_PAUSE_MAX_S，设备重连（新会话）也算结束。"""
        paused = self._paused.get(dev["slot"])
        if paused is None or paused["cid"] != cid:
            return None
        playing = self._playing(dev)
        sleep = paused.get("sleep")
        limit = voicecmds.SLEEP_PAUSE_MAX_S if sleep else PAUSE_MAX_S
        if self.clock() - paused["at"] > limit or (playing["online"] and playing["channel"] != cid) or \
                (sleep and playing["online"] and playing["since"] > paused["at"] + 1):
            self._paused.pop(dev["slot"], None)
            return None
        return paused

    def _store_position(self, dev: dict, cid: str, video, target: float) -> None:
        # 重新打开时会从「存的位置 − RESUME_BACK_S」开始，所以存目标加回退量，正好从目标起播。
        stored = target + RESUME_BACK_S if target > 0 else 0.0
        if cid == table.CAST_ID:
            cast = self._cast_of(dev)
            cast.save_cast_position(video.bvid, stored)
            cast.save()
            if video.site == "library":
                library_api.tv_progress(self, dev, video, target)
        else:
            state = self._state_of(dev)
            state.save_position(cid, video.bvid, stored)
            state.save()

    def pause(self, caller: Caller = LEGACY, device: str | None = None) -> dict:
        return self.pause_device(self._pick(caller, device))

    def pause_device(self, dev: dict, sleep: bool = False) -> dict:
        """sleep：语音定时到了（voicecmds.sleep）：pause 事件带 value=1，电视服务不自动继续。"""
        cid, video, position, entry = self._current(dev)
        if video is None:
            return {"ok": False, "error": "现在没有在播视频"}
        self._store_position(dev, cid, video, position)
        self._paused[dev["slot"]] = {"cid": cid, "video": video, "position": position, "at": self.clock(),
                                     "sleep": sleep}
        self._bridge.pop((dev["slot"], cid), None)
        if entry is not None:
            self._cut(cid, entry, "定时暂停" if sleep else "暂停")
        self._event(dev, "pause", value=1 if sleep else None, text=video.title)
        return {"ok": True, "at": self.clock(), "position": position}

    def resume(self, caller: Caller = LEGACY, device: str | None = None) -> dict:
        dev = self._pick(caller, device)
        playing = self._playing(dev)
        # 语音定时暂停的（直播台也算）：醒来，背光恢复（voicecmds.wake）。
        asleep = voicecmds.wake(self, dev)
        if self._paused_on(dev, playing["channel"]) is None and not asleep:
            return {"ok": False, "error": "现在没有暂停"}
        self._paused.pop(dev["slot"], None)
        self._event(dev, "resume")
        return {"ok": True, "at": self.clock()}

    def video_progress(self, dev: dict, cid: str) -> dict | None:
        paused = self._paused_on(dev, cid)
        if paused is not None:
            return {"position": round(paused["position"], 1), "duration": paused["video"].duration,
                    "paused": True, "started": paused["at"], "moving": False}
        key = (dev["slot"], cid)
        with self._active_lock:
            entry = self._active.get(key)
        if entry is None:
            bridge = self._bridge.get(key)
            if bridge is None or self.clock() - bridge["at"] >= SEEK_BRIDGE_S:
                return None
            # 新流还没起来：先报跳转目标；started 留在 0，手机上的「跳转中」等真正的新流。
            return {"position": round(bridge["target"], 1), "duration": bridge["video"].duration,
                    "paused": False, "started": 0.0, "moving": False}
        # moving：内容已经到了小智屏幕上、进度在走；新流刚起来的缓冲期里它是假，网页别自己往前推。
        lag = self._lag(dev)
        moving = self.monotonic() - entry["began"] >= lag
        return {"position": round(self._position(entry, lag), 1), "duration": entry["video"].duration,
                "paused": False, "started": entry["at"], "moving": moving}

    def seek(self, delta: float | None = None, position: float | None = None,
             caller: Caller = LEGACY, device: str | None = None, fraction: float | None = None,
             from_end: float | None = None, within: bool = False) -> dict:
        """跳转：相对（delta）、到某一秒（position）、按比例（fraction，「跳到一半」）、到结尾前几秒
        （from_end）。后两种和 within（语音说的时间超过结尾时不跳，说视频有多长）见 voice.seek_target。"""
        dev = self._pick(caller, device)
        cid, video, current, entry = self._current(dev)
        if video is None:
            return {"ok": False, "error": "现在没有在播视频"}
        if fraction is not None or from_end is not None or within:
            target, refusal = voice.seek_target(video.duration, position, fraction, from_end)
            if refusal:
                return {"ok": False, "error": refusal}
        else:
            target = position if position is not None else current + (delta or 0)
        target = max(0.0, float(target))
        if video.duration and target >= video.duration - 3:
            # 单用户时也点名这台：语音命令的设备不一定是 target()（手机遥控的那台）。
            return self.next(caller, dev["id"] or None)
        self._store_position(dev, cid, video, target)
        self._bridge[(dev["slot"], cid)] = {"video": video, "target": target, "at": self.clock()}
        # 设备上画「快进 30 秒」之类的提示；秒数是相对现在的位置。
        self._event(dev, "seek", value=int(round(target - current)))
        if entry is not None:
            self._cut(cid, entry, f"跳到 {int(target)} 秒")
        else:
            self.log(f"{cid} 跳到 {int(target)} 秒（新流起来时生效）")
        return {"ok": True, "at": self.clock(), "position": target}

    def next(self, caller: Caller = LEGACY, device: str | None = None) -> dict:
        dev = self._pick(caller, device)
        cid, video, _, entry = self._current(dev)
        if video is None:
            return {"ok": False, "error": "现在没有在播视频"}
        if cid == table.CAST_ID and video.site == "library":
            # 片库的剧集：换成下一集（最后一集时照投屏的办法收场）。
            stepped = library_api.tv_step(self, dev, video, 1, entry)
            if stepped is not None:
                return stepped
        self._bridge.pop((dev["slot"], cid), None)
        if cid == table.CAST_ID:
            self._event(dev, "next")
            cast = self._cast_of(dev)
            cast.cast_ended(video.bvid)
            cast.save()
            self._event(dev, "return")
        else:
            state = self._state_of(dev)
            state.mark_watched(cid, video.bvid)
            state.save()
            # 「正在加载」在「下一个」之前：会画它的固件就不再画「下一个」的提示框。
            self._loading(dev, cid, state, state.peek_video(cid))
            self._event(dev, "next")
        if entry is not None:
            self._cut(cid, entry, "下一个")
        return {"ok": True, "at": self.clock()}

    # ---- 选集 ----

    def _on_air(self, dev: dict, cid: str):
        """设备此刻在 cid 台上播的视频：正在推的、暂停着的、或刚跳转还没起来的；不在这个台上为 None。"""
        playing = self._playing(dev)
        if not playing["online"] or playing["channel"] != cid:
            return None
        paused = self._paused_on(dev, cid)
        if paused is not None:
            return paused["video"]
        return self._current(dev)[1]

    def _episode_of(self, state: State, cid: str, video) -> int:
        """video 是剧集台 cid 的第几集（从 1 数）；不是这部剧的、或 video 为 None 时为 0。"""
        creator = state.get(cid)
        if video is None or creator is None or creator.kind != "series":
            return 0
        with state.lock:
            return next((n for n, v in enumerate(creator.videos, 1) if v.bvid == video.bvid), 0)

    def _series_view(self, caller: Caller, device: str | None):
        """看集数表、选集用的 (设备, 状态)：选集是对设备的遥控，按设备主人的剧来。"""
        dev = self._pick(caller, device)
        if self.tenant and not caller.guest and dev.get("owner") != caller.user:
            # 自己的剧（不是这台设备主人的）：只能看，选了也播不到这台设备上。
            return dev, self._account(caller)
        return dev, self._state_of(dev)

    def episodes(self, cid: str, caller: Caller = LEGACY, device: str | None = None) -> dict:
        """剧集的集数表：每集的状态（看过 / 下一次播的 / 没看）和进度，下一次播哪集，设备正在播哪集。"""
        dev, state = self._series_view(caller, device)
        with state.lock:
            creator = state.get(cid)
            if creator is None or creator.kind != "series":
                return {"ok": False, "error": "没有这部剧"}
            upcoming = state.episode(cid)
            watched = set(creator.watched)
            rows = []
            for n, video in enumerate(creator.videos, 1):
                row = {"n": n, "title": video.title, "duration": video.duration,
                       "state": "current" if n == upcoming else "watched" if video.bvid in watched else "left"}
                if video.bvid in creator.positions:
                    row["position"] = int(creator.positions[video.bvid])
                rows.append(row)
            name = creator.name
        playing = self._episode_of(state, cid, self._on_air(dev, cid)) if state is self._state_of(dev) else 0
        return {"ok": True, "id": cid, "name": name, "total": len(rows), "next": upcoming,
                "playing": playing, "episodes": rows}

    def play_episode(self, cid: str, n: int, caller: Caller = LEGACY, device: str | None = None) -> dict:
        """从第 n 集接着播（见 State.play_from），并让设备播它。

        设备已在这个台上：掐断当前的流，电视服务重新打开时就是第 n 集（暂停着的先结束暂停）；
        在别的台上：发 switch 事件换过来。设备不在线或固件不能遥控时只记下选择，下次换到这个台生效，
        回答里的 note 说明这一点。cut 为真表示在同一个台上换集（手机按跳转等新的一路流）。
        """
        dev, state = self._series_view(caller, device)
        creator = state.get(cid)
        if creator is None or creator.kind != "series":
            return {"ok": False, "error": "没有这部剧"}
        if not 1 <= n <= len(creator.videos):
            return {"ok": False, "error": f"没有第 {n} 集"}
        chosen = creator.videos[n - 1]
        own_device = state is self._state_of(dev)
        playing = self._playing(dev)
        here = own_device and playing["online"] and playing["channel"] == cid
        entry = None
        if here:
            _, video, _, entry = self._current(dev)
            if video is not None and video.bvid == chosen.bvid:
                return {"ok": True, "episode": n, "title": chosen.title, "note": "正在播这一集"}
            if entry is not None:
                # 先标上，免得改状态的同时旧流的 tick 又把旧那集的进度存回去。
                entry["abort"] = f"换到第 {n} 集"
        state.play_from(cid, n)
        state.save()
        self._bridge.pop((dev["slot"], cid), None)
        self.log(f"{cid} 选集：第 {n} 集《{chosen.title}》")
        answer = {"ok": True, "at": self.clock(), "episode": n, "title": chosen.title}
        name = dev["name"]
        if not own_device:
            answer["note"] = f"已记下：下次从第 {n} 集播（{name}上没有这部剧）"
        elif here:
            # 先说要加载哪一集，再继续或掐断：电视服务据此立刻让设备画「正在加载 第 n 集」。
            self._loading(dev, cid, state, chosen)
            if self._paused_on(dev, cid) is not None:
                self._paused.pop(dev["slot"], None)
                self._event(dev, "resume")
            elif entry is not None:
                self._cut(cid, entry, f"换到第 {n} 集")
            answer["cut"] = True
        elif not playing["online"]:
            answer["note"] = f"{name}没在线，下次换到这个台从第 {n} 集播"
        elif not playing["switch"]:
            answer["note"] = f"{name}的固件不支持遥控，在{name}上换到这个台就从第 {n} 集播"
        else:
            self._event(dev, "switch", channel=cid)
        return answer

    def previous(self, caller: Caller = LEGACY, device: str | None = None) -> dict:
        """剧集台回到上一集，从头播（上一集已经看过，没有进度）。"""
        dev = self._pick(caller, device)
        playing = self._playing(dev)
        cid = playing["channel"]
        video = self._on_air(dev, cid)
        if video is None:
            return {"ok": False, "error": "现在没有在播视频"}
        if cid == table.CAST_ID and video.site == "library":
            # 片库的剧集：回到上一集，从头播。
            stepped = library_api.tv_step(self, dev, video, -1, self._current(dev)[3])
            return stepped or {"ok": False, "error": "已经是第 1 集"}
        state = self._state_of(dev)
        creator = state.get(cid)
        if creator is None or creator.kind != "series":
            return {"ok": False, "error": "只有剧集台能回到上一集"}
        n = self._episode_of(state, cid, video)
        if n <= 1:
            return {"ok": False, "error": "已经是第 1 集"}
        return self.play_episode(cid, n - 1, caller, dev["id"] or None)

    # ---- 管理 ----

    def key_ok(self, given: str | None) -> bool:
        return bool(given) and hmac.compare_digest(given.encode(), self.key.encode())

    def code_ok(self, given: str | None) -> bool:
        code = self.state.ensure_code()
        return bool(given) and hmac.compare_digest(given.upper().encode(), code.encode())

    def remote_url(self, dev: dict | None = None) -> str:
        """二维码地址：全大写，二维码可用字母数字模式（第 2 版就装得下）。"""
        code = dev["remote_code"] if dev is not None and self.tenant else self.state.ensure_code()
        return f"{self.public_url.upper()}/TV/R/{code}"

    def write_remote_url(self) -> None:
        if self.tenant:
            self.sync_devices(force=True)
            return
        if self.remote_url_out is None or not self.public_url:
            return
        if table.write_if_changed(self.remote_url_out, self.remote_url() + "\n"):
            self.log("遥控二维码地址已更新")

    def new_code(self, caller: Caller = LEGACY, device: str | None = None) -> dict:
        if self.tenant:
            # 多用户：只换这一台的遥控码，只有扫旧码进来的访客要重新扫。
            if caller.guest:
                raise NotFound("code")
            dev = self._pick(caller, device)
            if not dev["id"]:
                return {"ok": False, "error": "还没有绑定小电视"}
            self.accounts.new_remote_code(dev["id"], caller.user)
            self.sync_devices()
            self.log(f"{dev['id']} 换了新的遥控码")
            return {"ok": True}
        self.state.new_code()
        self.state.save()
        self.write_remote_url()
        self.log("换了新的扫码码，已登录的手机需要重新扫码")
        return {"ok": True}

    # ---- 设备 ----

    def target(self, caller: Caller = LEGACY) -> dict:
        """手机现在遥控的那台：选定的；自动时是在线设备里最近开始会话（换台）的那台。"""
        devices = self.devices_for(caller)
        if not devices:
            return NO_DEVICE
        chosen_id = self._targets.get(caller.key)
        if chosen_id is not None:
            chosen = next((d for d in devices if d["id"] == chosen_id), None)
            if chosen is not None:
                return chosen
        now = self.clock()
        online = [(now_playing.read(d["now"], now)["since"], n) for n, d in enumerate(devices)
                  if now_playing.read(d["now"], now)["online"]]
        return devices[max(online)[1]] if online else devices[0]

    def _playing(self, dev: dict) -> dict:
        return now_playing.read(dev["now"], self.clock())

    def playing(self, caller: Caller = LEGACY) -> dict:
        return self._playing(self.target(caller))

    def set_target(self, device: str | None, caller: Caller = LEGACY) -> dict:
        if device is not None and device not in {d["id"] for d in self.devices_for(caller)}:
            return {"ok": False, "error": "没有这台设备"}
        if device is None:
            self._targets.pop(caller.key, None)
        else:
            self._targets[caller.key] = device
        return {"ok": True}

    def device_rows(self, caller: Caller = LEGACY) -> list[dict]:
        target, now = self.target(caller)["id"], self.clock()
        rows = []
        for d in self.devices_for(caller):
            row = {"id": d["id"], "name": d["name"], "online": now_playing.read(d["now"], now)["online"],
                   "target": d["id"] == target}
            if self.tenant:
                row.update(model=d["model"], legacy=d["legacy"],
                           mine=not caller.guest and d["owner"] == caller.user)
            rows.append(row)
        return rows

    # ---- 频道与遥控 ----

    def _overrides(self, state: State | None = None) -> dict:
        if not self.tenant:
            return channel_groups.load_overrides(self.groups_out)
        # 总后台给直播台设的默认分组垫在下面，用户自己调的盖在上面（admin.global_groups）。
        own = state.group_overrides if state is not None else {}
        return {k: v for k, v in {**admin.global_groups(self), **own}.items() if v in channel_groups.GROUPS}

    def _own_rows(self, state: State, marked: bool = True) -> list[tuple[str, str]]:
        """自己的台：投屏台、UP 主、剧集。设备列表里它们排在全局表前面，一定收得下（room_of 管着）。"""
        label = table.label if marked else table.plain_label
        with state.lock:
            return [(table.CAST_ID, table.CAST_LABEL)] + [(c.id, label(c)) for c in state.creators]

    def _rows_of(self, state: State, marked: bool = True) -> list[tuple[str, str]]:
        """marked：名字和设备列表里一样带网站标记（算 CONFIG 字节用）；手机网页另有网站标签，不带。

        这是整张表（电脑上看、手机换台都用它）；设备只收得下前一部分，见 channel_groups.device_list。"""
        rows = self._own_rows(state, marked)
        if self.main_channels is not None:
            rows += channel_groups.read_table(self.main_channels)
        return rows

    def channel_rows(self, dev: dict | None = None) -> list[tuple[str, str]]:
        """(id, 显示名)：投屏台、UP 主、剧集，再接直播频道表。"""
        return self._rows_of(self._state_of(dev or self.target(LEGACY)))

    def groups(self, dev: dict | None = None, state: State | None = None) -> list[dict]:
        """频道分组。state：按这份状态排（没有小电视的用户：自己的订阅），不给就按设备主人的。"""
        dev = dev or self.target(LEGACY)
        state = state if state is not None else self._state_of(dev)
        overrides = self._overrides(state)
        grouped: dict[str, list[dict]] = {g: [] for g in channel_groups.GROUPS}
        for cid, name in self._rows_of(state, marked=False):
            group = channel_groups.group_of(cid, name, overrides)
            grouped[group].append({"id": cid, "name": name, "n": len(grouped[group]) + 1})
        return [{"name": g, "channels": grouped[g]} for g in channel_groups.GROUPS if grouped[g]]

    def now(self, groups: list[dict], dev: dict | None = None) -> dict:
        dev = dev or self.target(LEGACY)
        playing = self._playing(dev)
        cid = playing["channel"]
        found = next(((g["name"], c) for g in groups for c in g["channels"] if c["id"] == cid), None)
        title = ""
        cast = self._cast_of(dev)
        state = self._state_of(dev)
        if cid == table.CAST_ID and cast.cast is not None:
            title = cast.cast.title
        elif _relay_channel(cid):
            title = self._titles.get((dev["slot"], cid), "")
        # 剧集台：正在播第几集、共几集；不是剧集台或还不知道播的是哪集时为 0。
        episode = total = 0
        creator = state.get(cid) if cid[:2] == "sr" else None
        if creator is not None and creator.kind == "series":
            video = self._on_air(dev, cid)
            episode, total = self._episode_of(state, cid, video), len(creator.videos)
            if episode:
                title = video.title
        return {"channel": cid, "online": playing["online"], "switch": playing["switch"],
                "group": found[0] if found else "", "name": found[1]["name"] if found else "",
                "n": found[1]["n"] if found else 0, "title": title, "since": playing["since"],
                "episode": episode, "total": total,
                "video": self.video_progress(dev, cid) if playing["online"] else None,
                "device": _phone_device(playing["device"]) if playing["online"] else None,
                "listen": _phone_listen(playing.get("listen"))}

    def _online_status(self, dev: dict):
        """(在线时设备报告的状态, 错误)：不在线或固件太旧时给出错误回答。"""
        playing = self._playing(dev)
        if not playing["online"]:
            return None, {"ok": False, "error": f"{dev['name']}没在线"}
        return playing["device"], None

    def device(self, volume=None, brightness=None, caller: Caller = LEGACY, device: str | None = None) -> dict:
        """手机调音量（0–100，按 10 取整）或亮度档位。"""
        dev = self._pick(caller, device)
        status, error = self._online_status(dev)
        if error:
            return error
        if status is None:
            return {"ok": False, "error": "小电视的固件太旧，需要更新才能用手机调音量和亮度"}
        if volume is not None:
            if not 0 <= volume <= 100:
                return {"ok": False, "error": "音量要在 0 到 100 之间"}
            self._event(dev, "volume", value=int(round(volume / 10) * 10))
        elif brightness is not None:
            if not 0 <= brightness < status["steps"]:
                return {"ok": False, "error": "没有这一档亮度"}
            # 手机上越大越亮；固件的第 0 档是最亮的，所以倒过来。
            self._event(dev, "brightness", value=status["steps"] - 1 - int(brightness))
        else:
            return {"ok": False, "error": "请求格式不对"}
        return {"ok": True}

    def set_pet(self, pet, caller: Caller = LEGACY, device: str | None = None) -> dict:
        """手机换宠物：0..PET_COUNT-1。"""
        dev = self._pick(caller, device)
        status, error = self._online_status(dev)
        if error:
            return error
        if status is None or "pet" not in status:
            return {"ok": False, "error": "小电视的固件太旧，需要更新才能换宠物"}
        if not isinstance(pet, int) or isinstance(pet, bool) or not 0 <= pet < PET_COUNT:
            return {"ok": False, "error": "没有这只宠物"}
        self._event(dev, "pet", value=pet)
        return {"ok": True}

    def set_audio(self, on, caller: Caller = LEGACY, device: str | None = None) -> dict:
        """手机开关只听声音。"""
        dev = self._pick(caller, device)
        status, error = self._online_status(dev)
        if error:
            return error
        if status is None or "audio_only" not in status:
            return {"ok": False, "error": "小电视的固件太旧，需要更新才能只听声音"}
        if not isinstance(on, bool):
            return {"ok": False, "error": "请求格式不对"}
        self._event(dev, "audio", value=1 if on else 0)
        return {"ok": True}

    def listen(self, caller: Caller = LEGACY, device: str | None = None) -> dict:
        """手机开始「用手机听」：给这台设备的手机流地址（带令牌）。权限和别的遥控一样：
        单用户时登录的手机；多用户时这台设备的主人、成员和它的访客（_pick，别人的设备 404）。"""
        dev = self._pick(caller, device)
        playing = self._playing(dev)
        if not dev["id"] or not playing["online"]:
            return {"ok": False, "error": f"{dev['name']}没在线"}
        info = playing.get("listen")
        if info is None:
            return {"ok": False, "error": "服务器还没开「用手机听」"}
        if not info["ok"]:
            return {"ok": False, "error": "小电视的固件太旧，需要更新才能用手机听"}
        token = listen_audio.issue_token(self.listen_secret, dev["id"], now=self.clock())
        path = f"/tv/listen/{info['route']}/{dev['id']}/"
        self.log(f"{dev['id']} 用手机听：发了一个地址")
        return {"ok": True, "device": dev["id"], "name": dev["name"], "url": f"{path}live.m3u8?t={token}",
                "bye": f"{path}bye?t={token}", "lead": info["lead"]}

    def watch_start(self, caller: Caller = LEGACY, device: str | None = None, viewer=None, start=None,
                    prefer: str | None = None) -> dict:
        """电脑版「在电脑上看」：这台设备正在播的台的片源，转成 HLS 的地址（带短期令牌，watch.py）。
        只给设备的主人和成员：访客（扫访客码的）不行，别人的设备 404（_pick）。没开这个功能（云端）也 404。

        start：在电脑的播放器上跳转（拖进度条、±10 秒）。只动电脑：给这个页面另起一路（占这个页面的位置，
        和直接看一样），从 start 秒起；小电视照常播它的。不给时是全家共用的那一路（按设备）。
        prefer：页面会放点播（_prefer）时，B 站的视频给这个页面一路点播（vod.py，占这个页面的位置），
        从小电视此刻的位置起，跳转在页面里做，不再来要。"""
        if self.watch is None:
            raise NotFound("watch")
        if caller.guest or caller.user is None:
            return {"ok": False, "error": "访客不能在电脑上看，请用家里人的账号登录"}
        start = _seconds(start)
        if start is False:
            return {"ok": False, "error": "请求格式不对"}
        dev = self._pick(caller, device)
        playing = self._playing(dev)
        if not dev["id"] or not playing["online"] or not playing["channel"]:
            return {"ok": False, "error": f"{dev['name']}没在线"}
        cid = playing["channel"]
        own = isinstance(viewer, str) and bool(_VIEWER.match(viewer))
        if start is not None and (not _relay_channel(cid) or not own):
            return {"ok": False, "error": "直播不能跳转" if own else "请求格式不对"}
        video = self._tv_video(dev, cid)[0] if _relay_channel(cid) else None
        source = self._watch_source(dev, cid, caller.user, start=start, prefer=prefer if own else None)
        if isinstance(source, str):
            return {"ok": False, "error": source}
        if isinstance(source, vod.VodSource):
            try:
                session = self.vod.open(self._pc_slot(caller, viewer), source, user=caller.user)
            except watch_pc.Busy as busy:
                return {"ok": False, "error": self._watch_busy(busy)}
            self._pc_pages.pop((caller.user, viewer), None)
            return {"device": dev["id"], "channel": cid, "own": True,
                    **self._watch_answer(session, getattr(video, "duration", 0) or 0, start=source.start)}
        try:
            if own and self.vod is not None:
                self.vod.close(self._pc_slot(caller, viewer))
            if start is not None:
                stream = self.watch.open(self._pc_slot(caller, viewer), source, user=caller.user, restart=True)
            else:
                if own:
                    # 这个页面刚才在直接看一个台（或者跳转过）：改成跟随，那一路马上让出名额。
                    self.watch.close(self._pc_slot(caller, viewer))
                stream = self.watch.open(dev["id"], source, user=caller.user)
        except watch_pc.Busy as busy:
            return {"ok": False, "error": self._watch_busy(busy)}
        if own:
            self._pc_pages.pop((caller.user, viewer), None)
        duration = getattr(video, "duration", 0) or 0
        return {"device": dev["id"], "channel": cid, "own": start is not None,
                **self._watch_answer(stream, duration)}

    def _watch_answer(self, stream, duration: float = 0, start: float | None = None, **extra) -> dict:
        """回答里和播放器有关的几项：mode（"hls"：watch.py 转的 HLS；"vod"：vod.py 转发的原文件，浏览器直接放）、
        地址（带令牌）、从视频的第几秒起（start）、视频多长（duration，不知道时 0）、能不能跳转（seekable：视频能，
        直播不能），登录了 B 站的人还有清晰度（quality：{current, choices}）。
        HLS 的 start 取这一路自己的：接着用已经在转的那一路时，它起的地方比这次算出来的早；点播的 start 是这次算的
        （同一个视频接着用同一路，播放器自己跳过去）。"""
        source = stream.source
        video = source.key[0] == "video"
        quality = {}
        if isinstance(stream, vod.VodSession):
            if source.hd:
                quality = {"quality": {"current": source.quality, "choices": list(QUALITY_CHOICES)}}
            elif source.site == "youtube" and len(source.choices) > 1:
                # YouTube 的 DASH：菜单里是这个视频有的 360p…1080p；换一个就是另一路（vod.VodSource.variant）。
                quality = {"quality": {"current": source.quality, "choices": list(source.choices), "site": "youtube"}}
            begin = source.start if start is None else start
            base = f"vod/{stream.sid}/{self.vod.token(stream)}/"
            return {"ok": True, "sid": stream.sid, "title": source.label, "kind": "video",
                    "mode": "dash" if source.dash else "vod",
                    "url": base if source.dash else base + "media", "start": round(float(begin), 1),
                    "duration": int(duration or source.duration or 0), "seekable": True, **quality, **extra}
        if getattr(source, "quality", ""):
            quality = {"quality": {"current": source.quality, "choices": list(QUALITY_CHOICES)}}
        # 片库经 watch.py 放的文件（library.py）：和视频一样能跳转、有时长。
        video = video or source.key[0] == "library"
        if source.key[0] == "live":
            extra.setdefault("routes", max(1, len(getattr(source, "routes", ()) or ())))
            if extra["routes"] > 1 and isinstance(stream, watch_pc.WatchStream):
                # 几条线路：现在放（或先试）第几条（从 0 数；之后自动换了线路看列表的 X-RTV-Route），和每条的
                # 用时 / 好坏（给播放器头上的线路菜单，不带地址）。
                extra.setdefault("route", stream.route)
                extra.setdefault("lines", self.watch.routes.lines(source.key[1], list(source.routes)))
        return {"ok": True, "sid": stream.sid, "title": source.label, "kind": source.key[0], "mode": "hls",
                "url": f"watch/{stream.sid}/{self.watch.token(stream)}/",
                "start": round(float(source.start), 1) if video else 0.0,
                "duration": int(duration or 0) if video else 0, "seekable": video, **quality, **extra}

    def _watch_busy(self, busy: "watch_pc.Busy") -> str:
        """满了说哪一种满了：这个用户自己的、只换封装（含点播）的、转码的（watch.busy_message）。"""
        caps = {"user": self.watch.max_per_user, "copy": self.watch.max_copy, "encode": self.watch.max_encode}
        return watch_pc.busy_message(busy.limit, busy.cap or caps.get(busy.limit, 0))

    def _tv_video(self, dev: dict, cid: str):
        """(设备在 cid 台上正在播的视频, 此刻的位置)：暂停着的按暂停的地方；还没开始放时视频为 None。"""
        paused = self._paused_on(dev, cid)
        if paused is not None:
            return paused["video"], paused["position"]
        _, video, position, _ = self._current(dev)
        return video, position

    def _watch_source(self, dev: dict, cid: str, viewer: int | None = None, start: float | None = None,
                      prefer: str | None = None):
        """设备正在播的 cid 的片源（watch.Source），或者给人看的原因（字符串）。
        视频（UP 主、剧集、投屏）从设备此刻的位置起（start 给了就从 start 起）；直播台用频道表里的地址和
        User-Agent。viewer：在电脑上看的人。B 站按他的登录（没有时按设备主人的）取更高的清晰度（bililogin.py）。"""
        if _relay_channel(cid):
            video, position = self._tv_video(dev, cid)
            if video is None:
                return "这个台还没开始放，过几秒再试"
            if start is not None:
                position = _clamp(start, video.duration)
            return self._video_source(("video", cid, video.site, video.bvid), video, position,
                                      viewer=viewer, owner=dev.get("owner"), prefer=prefer)
        return self._live_source(cid)

    def _video_source(self, key: tuple, video, position: float, viewer: int | None = None,
                      owner: int | None = None, prefer: str | None = None):
        """一个视频（B 站、YouTube）的片源，从 position 秒起；YouTube 没配置时是原因（字符串）。
        B 站按看的人（viewer）的登录取更高的清晰度，他没登录时按 owner（设备主人）的（bililogin.py）。
        prefer：页面会放点播时的清晰度（_prefer："720p" / "1080p"）。B 站的视频能直接放原文件时给 vod.VodSource
        （_vod_source），否则（和 YouTube、prefer 为 None 时）照旧给 watch.Source（ffmpeg → HLS）。"""
        if video.site == "library":
            # 小电视在放片库的视频：在电脑上到「片库」里放（那里按页面的能力定怎么放，访客看不到）。
            return "小电视在放片库里的视频：在电脑上看请到「片库」里点它"
        if video.site == "youtube":
            client = self.clients.get("youtube")
            if client is None:
                return self._youtube_off("YouTube 还没配置")
            if prefer is not None:
                found = self._yt_vod_source(key, video, position, prefer)
                if found is not None:
                    return found
            return watch_pc.Source(key, opener=lambda: client.open(video.bvid, position), paced=False,
                                   label=video.title, site="youtube", start=position)
        client = self.clients.get("bilibili")
        if client is None:
            return "B 站还没配置"
        whose = viewer if self.bili_login(viewer) is not None else owner
        login = self.bili_login(whose)
        if prefer is not None and not (login is not None and prefer == "1080p"):
            found = self._vod_source(key, video, position, login, dash=getattr(prefer, "dash", False))
            if found is not None:
                return found
        if login is None:
            url = lambda: self._bili_url(video.bvid, None, whose)  # noqa: E731
        else:
            quality = PC_QUALITY_VIP if login.vip else PC_QUALITY
            url = lambda: self._bili_url(video.bvid, login, whose, quality)  # noqa: E731
        return watch_pc.Source(key, url=url, user_agent=BILIBILI_UA, referer=BILIBILI_REFERER,
                               offset=position, label=video.title, site="bilibili", start=position,
                               quality="1080p" if login is not None and prefer == "1080p" else "")

    def _vod_source(self, key: tuple, video, position: float, login, dash: bool = False):
        """B 站的这个视频能不能在电脑上直接放原文件（vod.py）：能就是 vod.VodSource（地址已经取好，过期了再取），
        不能（分段的、FLV、HEVC、取地址出错——会员集登录失效之类的原因交给 HLS 那条路照旧说）是 None。
        login：取地址带的 B 站登录（会员剧集）；清晰度最高 720p，登录了的人另有 1080P（hd，走 HLS）。
        dash：页面能用 MediaSource 放分片 MP4。很长的视频 MP4 的索引（moov）太大时改放 DASH（_bili_dash_source）。"""
        getter = getattr(self.clients.get("bilibili"), "vod_media", None)
        if getter is None or self.vod is None:
            return None
        fetch = (lambda: getter(video.bvid, login=login)) if login is not None else (lambda: getter(video.bvid))
        try:
            info = fetch()
        except SourceError as error:
            self.log(f"在电脑上点播 {video.bvid} 取不到原文件（{watch_pc.explain('bilibili', str(error))}），转 HLS")
            return None
        reason = vod.playable(info)
        if reason:
            self.log(f"在电脑上点播 {video.bvid}：{reason}，转 HLS")
            return None
        headers = (("Referer", BILIBILI_REFERER), ("User-Agent", BILIBILI_UA))
        length = float(video.duration or info.get("duration") or 0)
        if dash and self.bilidash is not None and length >= vod.MOOV_CHECK_S:
            moov = vod.probe_moov(vod.media_of(info), headers, self.vod.opener)
            if moov > vod.MOOV_MAX:
                found = self._bili_dash_source(key, video, position, login, headers, moov)
                if found is not None:
                    return found

        def resolve() -> vod.Media:
            again = fetch()
            why = vod.playable(again)
            if why:
                raise vod.NotPlayable(why)
            return vod.media_of(again)

        return vod.VodSource(key, resolve=resolve, headers=(("Referer", BILIBILI_REFERER), ("User-Agent", BILIBILI_UA)),
                             label=video.title, site="bilibili", start=position,
                             duration=float(video.duration or info.get("duration") or 0), first=vod.media_of(info),
                             quality="720p", hd=login is not None)

    def _bili_dash_source(self, key: tuple, video, position: float, login, headers: tuple, moov: int):
        """很长的 B 站视频按 DASH 放（bilidash.py）：vod.VodSource，取不到 DASH、没有 H.264 时 None（照旧放 MP4）。"""
        getter = getattr(self.clients.get("bilibili"), "vod_dash", None)
        if getter is None:
            return None
        fetch = (lambda: getter(video.bvid, login=login)) if login is not None else (lambda: getter(video.bvid))
        try:
            info = fetch()
        except SourceError as error:
            self.log(f"在电脑上点播 {video.bvid}：MP4 的索引有 {moov >> 20} MB，DASH 取不到"
                     f"（{watch_pc.explain('bilibili', str(error))}），照旧放 MP4")
            return None
        if not info:
            self.log(f"在电脑上点播 {video.bvid}：MP4 的索引有 {moov >> 20} MB，没有能放的 DASH，照旧放 MP4")
            return None
        self.log(f"在电脑上点播 {video.bvid}：MP4 的索引有 {moov >> 20} MB，改放 DASH（{bilidash.label(info)}）")
        return self.bilidash.source(key, video, position, info, fetch, headers, hd=login is not None)

    def _yt_vod_on(self) -> bool:
        """YouTube 能在电脑上点播：本机模式（地址绑的是这台电脑经代理的出口；用 cstone 节点时地址绑的是 cstone，
        这里取不了），开着在电脑上看，节点认 /yt/formats（旧节点 404，见 ytvod.py）。"""
        client = self.clients.get("youtube")
        return self.ytvod is not None and getattr(self, "youtube_kind", "") == "local" \
            and getattr(client, "formats", None) is not None

    def _yt_vod_source(self, key: tuple, video, position: float, prefer):
        """YouTube 的这个视频在电脑上点播（ytvod.py）：DASH（页面能用 MediaSource 时）或 360p 的合体文件，
        vod.VodSource；放不了、节点太旧、取地址出了别的错时 None（照旧走节点的流 → HLS，由那条路说原因）；
        视频本身看不了（删了、私享）时是原因（字符串），不用再让 HLS 那条路白跑一次 yt-dlp。"""
        if not self._yt_vod_on():
            return None
        try:
            return self.ytvod.source(key, video, position, getattr(prefer, "yt", ytvod.DEFAULT_QUALITY),
                                     dash=getattr(prefer, "dash", False))
        except SourceError as error:
            if getattr(error, "reason", "") == "unsupported":
                self.log("在电脑上点播 YouTube：节点太旧（没有 /yt/formats），转 HLS")
                return None
            if error.permanent:
                return "这个 YouTube 视频看不了（删了、私享或者有地区限制）"
            self.log(f"在电脑上点播 YouTube {video.bvid} 取不到地址（{watch_pc.explain('youtube', str(error))}），转 HLS")
            return None

    def _yt_video(self, vid: str):
        """投到电脑的 YouTube 视频的标题、时长：能在电脑上点播时从 ytvod 取地址的结果里拿（Video），不另问节点的
        /yt/info——预取过的马上就有（节点自己的缓存只留半小时，过了 /yt/info 就要再跑一次 yt-dlp，这里留到地址
        快过期）；没取过时这一次取地址起播也要用，信息、地址只跑一次 yt-dlp。不能点播、节点太旧时 None（照旧问
        /yt/info）；取不到抛 SourceError，和 /yt/info 一样。"""
        if not self._yt_vod_on():
            return None
        try:
            info = self.ytvod.resolver.resolve(vid)
        except SourceError as error:
            if getattr(error, "reason", "") == "unsupported":
                return None
            raise
        try:
            return Video(bvid=vid, title=str(info.get("title") or ""), duration=int(info.get("duration") or 0),
                         pubdate=0, site="youtube")
        except (TypeError, ValueError):
            return None

    def yt_prefetch(self, caller: Caller, body: dict) -> dict:
        """POST /api/yt/prefetch {ids: [视频 id, …], q}：电脑版先替看得见的、鼠标停住的 YouTube 视频取好地址
        （ytvod.Prefetcher；不耽误真正的播放）。没开在电脑上看（云端）404；不是本机模式时 off 为真，页面就不再发。"""
        if self.watch is None:
            raise NotFound("watch")
        ids = body.get("ids")
        if caller.guest or caller.user is None or not isinstance(ids, list) \
                or not 0 < len(ids) <= ytvod.PREFETCH_IDS_MAX \
                or not all(isinstance(v, str) and youtube_links._ID.match(v) for v in ids):
            return {"ok": False, "error": "请求格式不对"}
        if not self._yt_vod_on():
            return {"ok": True, "off": True, "queued": 0}
        quality = body.get("q") if body.get("q") in ytvod.QUALITIES else ytvod.DEFAULT_QUALITY
        return {"ok": True, "queued": self.ytvod.prefetch(ids, quality)}

    def _live_source(self, cid: str):
        """全局频道表（main_channels）里的直播台：地址和 User-Agent；不在表里的是原因（字符串）。"""
        lines = watch_pc.live_lines(self.main_channels, cid)
        if not lines:
            return "找不到这个台的地址"
        url, agent = lines[0]
        name = next((n for c, n in channel_groups.read_table(self.main_channels) if c == cid), cid)
        # 直播源自己就按实时的速度给，不加 -re：加了以后源每停一下（咪咕这类几秒一段的 HLS 常有）ffmpeg 只按
        # 1.05 倍追，产出越来越落后，电脑上几秒卡一下。
        # 有备用线路（server/routes.py）时一路里按顺序换线路（watch.WatchStream），上次好用的先试。
        return watch_pc.Source(watch_pc.live_key(cid, lines), url=url, user_agent=agent, label=name, paced=False,
                               routes=tuple(lines))

    # ---- 在电脑上看：直接看一个台（不经小电视） ----

    @staticmethod
    def _pc_slot(caller: Caller, viewer: str) -> str:
        """直接看时这个页面占的位置：同一个页面换台只占一路；令牌绑的就是它（watch.WatchService）。"""
        return f"pc{caller.user}-{viewer}"

    def _pc_position(self, cursor: dict) -> float:
        """电脑上看到了哪里：播放器报过的位置（watch_progress、跳转的目标）；没报过时估计：
        这一路的起点 + 转出来的秒数 − 播放器落后的那几秒。"""
        if cursor.get("position") is not None:
            return max(0.0, float(cursor["position"]))
        stream = cursor.get("stream")
        if isinstance(stream, (vod.VodSession, _LibraryPlay)) or cursor.get("site") == "library":
            # 点播、片库：播放器还没报过，就是起播的地方（片库经 watch.py 放的那一路跑在播放器前面，不能按出了多少段算）。
            return max(0.0, float(cursor["offset"]))
        produced = stream.window.total_s if stream is not None else 0.0
        return max(0.0, cursor["offset"] + produced - PC_BEHIND_S)

    def youtube_proxy(self) -> str:
        """本机模式的 YouTube 代理（「设置 → YouTube」，config.toml）：YouTube 缩略图经它取；别的情况为空串。"""
        if getattr(self, "youtube_kind", "") != "local" or getattr(self, "selfhost_data", None) is None:
            return ""
        try:
            config, _ = ytsettings._config(self)
        except Exception:  # noqa: BLE001 —— 读不了设置就直连，缩略图取不到也只是用占位图
            return ""
        return config.youtube_proxy or ""

    def forget_home(self, caller: Caller) -> None:
        with self._home_lock:
            self._home_cache.pop(caller.key, None)

    def pc_sync(self, user: int | None) -> None:
        """把这个用户电脑上每一路看到的位置记进 pc-progress.json（变化不大时不写）。"""
        if self.pc_progress is None or user is None:
            return
        for (who, key), cursor in list(self._pc_cursor.items()):
            if who != user:
                continue
            record = {k: v for k, v in cursor.items() if k not in ("stream", "offset", "position", "bvid")}
            record.update(channel=key if key[:2] in ("up", "sr") else "cast", bvid=cursor["bvid"],
                          position=round(self._pc_position(cursor), 1))
            self.pc_progress.put(user, key, record)

    def _pc_saved(self, user: int | None, key: str, bvid: str | None = None) -> dict | None:
        """pc-progress.json 里这个台（或视频）上次在电脑上看到的地方；bvid 给了就要对得上。"""
        if self.pc_progress is None or user is None:
            return None
        found = self.pc_progress.get(user, key)
        if found is None or (bvid is not None and found.get("bvid") != bvid):
            return None
        return found

    def _pc_page(self, caller: Caller, body: dict):
        """(这个页面正在直接看的那一项的键, 它的进度游标)；没有时 (None, None)。sid 给了就要是那一路：
        刚换了一集、跳转过的页面，晚到的旧报告不能记到新的那一集上。"""
        viewer = body.get("viewer")
        if not isinstance(viewer, str) or not _VIEWER.match(viewer):
            return None, None
        key = self._pc_pages.get((caller.user, viewer))
        cursor = self._pc_cursor.get((caller.user, key)) if key else None
        if cursor is None:
            return None, None
        sid = body.get("sid")
        stream = cursor.get("stream")
        if sid is not None and (stream is None or stream.sid != sid):
            return None, None
        return key, cursor

    def _pc_report(self, caller: Caller, body: dict) -> bool:
        """播放器报的位置（position，视频里的秒数）和放完了没有（ended）：记进游标，放完或快放完的那一集记进
        「电脑上看完了」（done）。从不碰小电视的进度和「看过」。"""
        key, cursor = self._pc_page(caller, body)
        if cursor is None:
            return False
        position = _seconds(body.get("position"))
        if not cursor.get("duration") and cursor.get("site") == "library":
            # 片库的视频服务器不知道多长：播放器读到文件的时长后跟着进度一起报。
            length = _seconds(body.get("duration"))
            if isinstance(length, float) and length > 0:
                cursor["duration"] = round(length, 1)
        duration = float(cursor.get("duration") or 0)
        if isinstance(position, float):
            cursor["position"] = min(position, duration) if duration else position
        ended = body.get("ended") is True
        if ended and duration:
            cursor["position"] = duration
        near = duration and float(cursor.get("position") or 0) >= duration - min(home.PROGRESS_END_S, duration * 0.1)
        if (ended or near) and self.pc_progress is not None:
            self.pc_progress.mark_done(caller.user, key, cursor["bvid"])
        return True

    def watch_progress(self, caller: Caller, body: dict) -> dict:
        """POST /api/watch/progress {viewer, sid, position, ended?}：电脑上的播放器每 10 秒左右报一次看到哪里。"""
        if self.watch is None:
            raise NotFound("watch")
        if caller.guest or caller.user is None:
            return {"ok": False, "error": "请求格式不对"}
        if not self._pc_report(caller, body):
            return {"ok": False, "error": "这个页面现在没有在电脑上看视频"}
        self._pc_history(caller, body, final=body.get("ended") is True)
        session = self.vod.get(self._pc_slot(caller, body["viewer"])) if self.vod is not None else None
        if session is not None:
            session.touch()        # 点播暂停着时浏览器不取数据：页面报进度就是还在看
        self.pc_sync(caller.user)
        if body.get("ended") is True:
            self.forget_home(caller)
        return {"ok": True}

    def watch_stop(self, caller: Caller, viewer, body: dict | None = None) -> dict:
        """页面上点了「关闭」：马上停掉这个页面直接看的那一路（跟随小电视的那一路是全家共用的，等它自己空闲停）。
        body 里带着 sid 和 position 时先记下看到的地方（关页面时也发，keepalive）。"""
        if self.watch is None:
            raise NotFound("watch")
        if caller.guest or caller.user is None or not isinstance(viewer, str) or not _VIEWER.match(viewer):
            return {"ok": False, "error": "请求格式不对"}
        if body and body.get("position") is not None:
            if self._pc_report(caller, body):
                self._pc_history(caller, body, final=True)
        self.pc_sync(caller.user)
        self._pc_pages.pop((caller.user, viewer), None)
        self.forget_home(caller)
        stopped = self.vod.close(self._pc_slot(caller, viewer)) if self.vod is not None else False
        return {"ok": True, "stopped": self.watch.close(self._pc_slot(caller, viewer)) or stopped}

    def watch_warm(self, caller: Caller, body: dict) -> dict:
        """POST /api/watch/warm {channels: [台 id, …]}：电脑上看直播时，先把接下来可能换到的直播台转起来
        （上一台、下一台，鼠标停在哪个台上），换过去时马上有画面（watch.WatchService.warm）。至多两个台，
        只认全局频道表里的直播台；没开预热时 off 为真，页面就不再问。"""
        if self.watch is None:
            raise NotFound("watch")
        if caller.guest or caller.user is None:
            return {"ok": False, "error": "请求格式不对"}
        channels = body.get("channels")
        if not isinstance(channels, list) or len(channels) > 2 or \
                not all(isinstance(c, str) and c for c in channels):
            return {"ok": False, "error": "请求格式不对"}
        if not self.watch.warm_max:
            return {"ok": True, "off": True, "warm": {}}
        table_ids = {c for c, _ in channel_groups.read_table(self.main_channels)} \
            if self.main_channels is not None else set()
        done = {}
        for cid in dict.fromkeys(channels):
            if cid not in table_ids or _relay_channel(cid) or cid == table.CAST_ID:
                done[cid] = "live"
                continue
            source = self._live_source(cid)
            done[cid] = source if isinstance(source, str) else self.watch.warm(caller.user, source)
        return {"ok": True, "warm": done}

    def watch_prepare(self, caller: Caller, body: dict) -> dict:
        """POST /api/watch/prepare {channels: [台 id, …]}：直播页上看得见的那一组台，先在后台把跳转和主列表走一遍
        （watch.WatchService.prepare，不起 ffmpeg），点下去时起播少等几秒。只认全局频道表里的直播台，至多
        watch.PREPARE_MAX 个。"""
        if self.watch is None:
            raise NotFound("watch")
        if caller.guest or caller.user is None:
            return {"ok": False, "error": "请求格式不对"}
        channels = body.get("channels")
        if not isinstance(channels, list) or len(channels) > watch_pc.PREPARE_MAX or \
                not all(isinstance(c, str) and c for c in channels):
            return {"ok": False, "error": "请求格式不对"}
        table_ids = {c for c, _ in channel_groups.read_table(self.main_channels)} \
            if self.main_channels is not None else set()
        sources = []
        for cid in dict.fromkeys(channels):
            if cid in table_ids and not _relay_channel(cid) and cid != table.CAST_ID:
                source = self._live_source(cid)
                if not isinstance(source, str):
                    sources.append(source)
        return {"ok": True, **self.watch.prepare(sources)}

    def watch_direct(self, caller: Caller, device: str | None, body: dict) -> dict:
        """电脑版「在电脑上看」一个台，不经小电视（没有小电视、小电视没在线、或者只想在电脑上看）。

        body：viewer（页面编号）和下面之一——
          channel：台 id。直播台要在全局频道表里；UP 主、剧集只能是自己订阅的（点名了别人的设备时
                   不行：那台的频道表是主人的订阅）；cast 是点名设备的投屏位，没有设备时是自己最近投到电脑的。
                   UP 主、剧集再可以带 episode（第几集 / 第几个，从 1 数）或 step（1 下一个，−1 上一个）。
          url：视频链接，投到电脑（记进自己的投屏历史，不动任何设备的投屏位）。多 P 视频、合集、番剧、
               YouTube 播放列表可以带 part（第几集，从 1 数）；不带时是链接里的 ?p= / 那一集，或者上次看到的那一集。
          video：{"site", "id"}，投屏历史里的一条，在电脑上重播（也可以带 part）。
        再可以带 start（从第几秒起：跳转、「从头播放」是 0）：同一个片源也重起一路；直播台可以带 route（第几条线路，
        从 0 数）：先试这条，起不来照常换别的，放起来了就记成好用的那条。回答里几条线路的直播台有 routes（几条）、
        route（现在放第几条）和 lines（每条的 ms / bad，频道表的顺序）。
        进度：UP 主、剧集从电脑上次看到的地方起，没有时从小电视下次会播的地方起（只读）；之后电脑上的进度另记
        （_pc_cursor，播放器报的位置，pc-progress.json），不标看过、不存进小电视——小电视的剧集不会因为在电脑上看了
        而往后推。回答里的 parts 是选集用的各集（电脑上看完了的打 ✓），resume 是接着上次看的地方起的秒数。
        """
        if self.watch is None:
            raise NotFound("watch")
        if caller.guest or caller.user is None:
            return {"ok": False, "error": "访客不能在电脑上看，请用家里人的账号登录"}
        viewer = body.get("viewer")
        if not isinstance(viewer, str) or not _VIEWER.match(viewer):
            viewer = "0"
        start = _seconds(body.get("start"))
        part, route = body.get("part"), body.get("route")
        if start is False or any(v is not None and (not isinstance(v, int) or isinstance(v, bool))
                                 for v in (part, route)) or (route is not None and route < 0):
            return {"ok": False, "error": "请求格式不对"}
        dev = self.find_device(device, caller) if device else None
        account = self._account(caller)
        self.pc_sync(caller.user)
        prefer = _prefer(body)
        if body.get("library") is not None:
            # caps：页面能不能放 HEVC（MediaSource、<video>）；remux：直接放出了错，要求换一种放法。
            return self._pc_library(caller, viewer, body.get("library"), start, body.get("caps"),
                                    body.get("remux") is True)
        cid, name, extra, meta, key, duration = "cast", table.CAST_LABEL, {}, {}, "", 0
        if body.get("url") is not None or body.get("video") is not None:
            planned = self._pc_cast_item(caller, account, dev, body.get("url"), body.get("video"), part, start,
                                         prefer)
        else:
            cid = body.get("channel")
            if not isinstance(cid, str) or not cid:
                return {"ok": False, "error": "请求格式不对"}
            if cid == table.CAST_ID and dev is None:
                # 没有设备：自己最近投到电脑的那个，和从投屏历史里重播一样（选集、接着上次的地方）。
                with account.lock:
                    latest = dict(account.history[0]) if account.history else None
                if latest is None:
                    return {"ok": False, "error": "还没有投过视频：到「投屏」页粘贴链接，点「投到电脑」"}
                planned = self._pc_cast_item(caller, account, None, None,
                                             {"site": latest.get("site"), "id": latest.get("id")}, part, start,
                                             prefer)
            elif cid == table.CAST_ID:
                planned = self._pc_cast_channel(dev, caller.user, start, prefer)
            elif _relay_channel(cid):
                if dev is not None and dev.get("owner") != caller.user:
                    return {"ok": False, "error": f"这是{dev['name']}主人订阅的台，在电脑上只能看自己订阅的"}
                planned = self._pc_subscription(caller, account, cid, body, start, prefer)
            elif self.main_channels is not None and \
                    cid in {c for c, _ in channel_groups.read_table(self.main_channels)}:
                source = self._live_source(cid)
                planned = (source, "", {}, {}, 0)
                name = source.label if not isinstance(source, str) else cid
            else:
                return {"ok": False, "error": "没有这个台"}
        if isinstance(planned, str):
            return {"ok": False, "error": planned}
        source, key, extra, meta, duration = planned
        name = extra.pop("_name", name)
        if isinstance(source, str):
            return {"ok": False, "error": source}
        video = source.key[0] == "video"
        native = isinstance(source, vod.VodSource)
        try:
            if native:
                # 点播：这个页面的 HLS（如果有）由 vod.open 关掉；同一个视频接着用同一路，跳转在页面里做。
                stream = self.vod.open(self._pc_slot(caller, viewer), source, user=caller.user)
            else:
                if self.vod is not None:
                    self.vod.close(self._pc_slot(caller, viewer))
                # keep_old：这个页面刚才在看的直播台再转一会儿，换回来马上有画面（开了预热时，watch.py）。
                # route：直播台在播放器头上选了第几条线路（从 0 数），这一次先试它（watch.WatchService.open）。
                stream = self.watch.open(self._pc_slot(caller, viewer), source, user=caller.user,
                                         restart=video and start is not None, keep_old=True, route=route)
        except watch_pc.Busy as busy:
            return {"ok": False, "error": self._watch_busy(busy)}
        begin = source.start if native else stream.source.start
        if key and video:
            cursor = self._pc_cursor.get((caller.user, key))
            if cursor is None or cursor.get("stream") is not stream:
                self._pc_cursor[(caller.user, key)] = {"bvid": source.key[3], "offset": begin,
                                                       "stream": stream, **meta}
                if start is not None:
                    self._pc_cursor[(caller.user, key)]["position"] = begin
                self.pc_sync(caller.user)
            self._pc_pages[(caller.user, viewer)] = key
        else:
            self._pc_pages.pop((caller.user, viewer), None)
        if video:
            extra.setdefault("video", {"site": source.key[2], "id": source.key[3]})
        return {"channel": cid, "name": name,
                **self._watch_answer(stream, duration, start=source.start if native else None), **extra}

    def _pc_library(self, caller: Caller, viewer: str, iid, start: float | None, caps=None,
                    remux: bool = False) -> dict:
        """片库里的一个视频在电脑上放（library.py）。先看探测的结果（第一次放时探测，缓存在索引里）和页面报的能力
        （caps）定怎么放（library.plan）：
        - 直接放：浏览器按 Range 取 api/library/file/<id>（同源，带登录的 cookie），不经 vod.py 的那一路；
        - 封装 / 转码：watch.py 的一路（ffmpeg 读只给本机的 /api/library/raw），回答和别的 HLS 一样（mode "hls"），
          跳转时从那里重起（start），时间轴照原样，进度和字幕都按文件里的时间。
        进度和别的视频一样记在 pc-progress.json（键 lib:<id>），继续观看、「接着上次看的地方」照旧；剧集给选集
        （parts，每集带 id）；字幕菜单是 subs（library.Library.subs_view）。"""
        library = library_api.wired(self)
        found = library.item(iid) if library is not None and isinstance(iid, str) else None
        if found is None:
            return {"ok": False, "error": "片库里没有这个视频（可能刚重新扫描过，刷新一下）"}
        media = library.media_of(found["id"])
        remote = library.remote(found)
        decision = library_api.plan(media, found.get("ext") or "", caps, remote=remote, remux=remux)
        if decision["how"] == "none":
            return {"ok": False, "error": decision["note"]}
        slot = self._pc_slot(caller, viewer)
        if self.vod is not None:
            self.vod.close(slot)
        key = "lib:" + found["id"]
        cursor = self._pc_cursor.get((caller.user, key))
        saved = self._pc_saved(caller.user, key)
        duration = float((cursor or {}).get("duration") or (saved or {}).get("duration") or 0) \
            or round(float((media or {}).get("dur") or 0), 1)
        offset = self._pc_position(cursor) if cursor is not None else float((saved or {}).get("position") or 0)
        if start is not None:
            offset = _clamp(start, duration) if duration else max(0.0, start)
        elif duration and offset >= duration - 5:
            offset = 0.0
        episodes = library.episodes_of(found)
        # 剧名 / 片名：对上豆瓣的用豆瓣的（library.shown）。
        show = name = library.shown(found)["name"] or found["title"]
        label = found["title"] if found.get("kind") == "episode" else name
        if decision["how"] == "direct":
            self.watch.close(slot)
            play = _LibraryPlay(secrets.token_hex(8))
            answer = {"ok": True, "sid": play.sid, "title": label, "kind": "video", "mode": "vod",
                      "url": f"api/library/file/{found['id']}", "start": round(offset, 1), "duration": int(duration),
                      "seekable": True}
        else:
            source = library.watch_source(found, media, decision, offset, label)
            try:
                # 每次都重起一路（跳转、从头放、换一集）：从 offset 那一秒起，时间轴照原样。
                play = self.watch.open(slot, source, user=caller.user, restart=True)
            except watch_pc.Busy as busy:
                return {"ok": False, "error": self._watch_busy(busy)}
            answer = self._watch_answer(play, duration)
        self.log(f"在电脑上放片库的「{found['title']}」：{decision['note']}")
        self._pc_cursor[(caller.user, key)] = {"bvid": found["id"], "offset": offset, "stream": play,
                                               "title": label, "name": name, "duration": duration,
                                               "site": "library"}
        if start is not None:
            self._pc_cursor[(caller.user, key)]["position"] = offset
        self._pc_pages[(caller.user, viewer)] = key
        self.pc_sync(caller.user)
        live = decision["how"] == "remux" and remote
        answer.update(channel="lib", name=name, library=found["id"], kind="video",
                      subs=library.subs_view(found, media, live), subs_url=f"api/library/subs/{found['id']}/")
        if start is None and offset > 0:
            answer["resume"] = round(offset, 1)
        if len(episodes) > 1:
            done = set()
            if self.pc_progress is not None:
                for ep in episodes:
                    done |= self.pc_progress.done(caller.user, "lib:" + ep["id"])
            current = next(n for n, ep in enumerate(episodes, 1) if ep["id"] == found["id"])
            answer["parts"] = {"kind": "series", "name": show, "current": current,
                               "items": [{"n": n, "id": ep["id"], "title": ep["title"], "duration": 0,
                                          "watched": ep["id"] in done, "playable": library.playable(ep)}
                                         for n, ep in enumerate(episodes, 1)]}
            answer["part"] = current
        return answer

    # ---- 投到电脑的视频：多 P、合集、番剧、YouTube 播放列表的选集 ----

    def _pc_cast_item(self, caller: Caller, account: State, dev: dict | None, text, video, part, start,
                      prefer: str | None = None):
        """投到电脑的一项（链接或投屏历史里的一条）：选好放哪一集、从第几秒起。
        返回 (片源, 进度的键, 回答里多给的, 存进度用的, 时长) 或原因。"""
        found = self._pc_cast(account, text, video, caller.user)
        if isinstance(found, str):
            return found
        if found.get("missing") and part is None:
            return EPISODE_MEMBER_HINT if self._vip_login(caller.user) is None else \
                "B 站上这一集现在放不了（可能下架了或要单独付费），可以在选集里挑别的集"
        item, parts = found["item"], found["parts"]
        key = f"v:{item.site}:{item.bvid}"
        saved = self._pc_saved(caller.user, key)
        done = self.pc_progress.done(caller.user, key) if self.pc_progress is not None else set()
        n = 0
        if parts:
            ids = [p.bvid for p in parts]
            asked = part if part is not None else found.get("asked")
            if asked is not None:
                n = asked
            elif saved is not None and saved.get("bvid") in ids:
                # 上次在电脑上看到的那一集；看完了就接着下一集。
                n = ids.index(saved["bvid"]) + 1
                if saved["bvid"] in done and n < len(parts):
                    n += 1
            else:
                n = found["default"]
            if not 1 <= n <= len(parts):
                return f"没有第 {n} 集"
            chosen = parts[n - 1]
        elif part not in (None, 1):
            return f"没有第 {part} 集"
        else:
            chosen = item
        duration = chosen.duration or (saved.get("duration") if saved and saved.get("bvid") == chosen.bvid else 0) or 0
        resumed = start is None and saved is not None and saved.get("bvid") == chosen.bvid \
            and chosen.bvid not in done
        offset = start if start is not None else float(saved.get("position") or 0) if resumed else 0.0
        offset = _clamp(offset, duration)
        if start is None and duration and offset >= duration - 5:
            offset = 0.0
        meta = {"title": chosen.title if parts else item.title, "name": item.title if parts else table.CAST_LABEL,
                "duration": duration, "site": item.site, "item": item.bvid}
        extra = {"video": {"site": item.site, "id": item.bvid}}
        if parts:
            meta.update(part=n, total=len(parts), kind=found["kind"], list=found.get("list") or "")
            extra.update(part=n, item_title=item.title,
                         parts=self._parts_answer(found["kind"], found["name"] or item.title, parts, n, done))
        elif found.get("list"):
            meta["list"] = found["list"]
        if start is None and offset > 0:
            extra["resume"] = round(offset, 1)
        whose = dev.get("owner") if dev else None
        source = self._video_source(("video", "pc", chosen.site, chosen.bvid), chosen, offset,
                                    viewer=caller.user, owner=whose, prefer=prefer)
        return source, key, extra, meta, duration

    @staticmethod
    def _parts_answer(kind: str, name: str, videos: list, current: int, done: set) -> dict:
        """选集的列表：{kind, name, current, items: [{n, title, duration, watched}]}；watched 是电脑上看完了的。"""
        return {"kind": kind, "name": name, "current": current,
                "items": [{"n": n, "title": v.title, "duration": int(v.duration or 0), "watched": v.bvid in done}
                          for n, v in enumerate(videos, 1)]}

    def _pc_cast(self, account: State, text, video, user: int | None = None):
        """投到电脑的那一项：{item（记在投屏历史里的那个 Video）, parts（各集，单个视频为空）, kind, name,
        default（默认第几集）, asked（链接里点名的那一集）, list（YouTube 播放列表）} 或原因。
        链接要取视频信息（记进投屏历史）；历史里的一条按 PC_ITEM_KEEP_S 缓存，换集、跳转不再访问网站。"""
        if video is not None:
            if not isinstance(video, dict) or not isinstance(video.get("site"), str) \
                    or not isinstance(video.get("id"), str):
                return "请求格式不对"
            site, vid = video["site"], video["id"]
            entry = account.history_entry(site, vid)
            if entry is None:
                return "历史里没有这个视频"
            if self.clients.get(site) is None:
                return self._youtube_off("YouTube 还没配置好")
            cached = self._pc_items.get((site, vid))
            if cached is not None and self.monotonic() - cached[0] < PC_ITEM_KEEP_S:
                return dict(cached[1])
            saved = self._pc_saved(user, f"v:{site}:{vid}") or {}
            found = self._pc_resolve(site, vid, entry.get("title", ""), playlist=saved.get("list") or "",
                                     user=user, fallback=True)
            self._keep_item(found)
            return dict(found)
        text = text.strip() if isinstance(text, str) else ""
        if not text:
            return "请先粘贴视频链接"
        site = _site_of(text)
        client = self.clients.get(site)
        if client is None:
            return self._youtube_off("YouTube 还没配置好，暂时只能看 B 站视频")
        try:
            vid = client.video_id(text)
            playlist = ""
            if site == "youtube":
                getter = getattr(client, "series_id", None)
                playlist = (getter(text) or "") if getter is not None else ""
            if vid is None and site == "bilibili":
                # 番剧的链接（/bangumi/play/ss…、ep…）：一季的各集。
                getter = getattr(client, "series_id", None)
                season = getter(text) if getter is not None else None
                if isinstance(season, str) and season.startswith("ss"):
                    vid = season
            if vid is None and playlist:
                vid = ""
            if vid is None:
                return "看不懂这个链接。请粘贴视频链接（App 里“分享 → 复制链接”）"
            found = self._pc_resolve(site, vid, playlist=playlist, user=user, text=text)
        except SourceError as error:
            self.log(f"投到电脑失败：{error}")
            if error.permanent:
                # 番剧、影视剧的链接：要大会员、地区限制分开说（整季都要会员时 series() 说「都要会员」）。
                pgc = site == "bilibili" and (parse_series_link(text) or "")[:2] in ("ss", "ep", "md")
                return (pgc_refusal(error) if pgc else "") or "这个视频放不了（可能已删除、私享或需要会员）"
            return f"{_SITE_NAMES[site].strip()}连不上：检查路由器或代理" if site == "bilibili" \
                else f"{_SITE_NAMES[site].strip()}暂时取不到，请稍后再试"
        item = found["item"]
        account.remember(item, at=self.clock())
        account.save()
        self._keep_item(dict(found, asked=None))
        self.log(f"投到电脑 {item.bvid} 《{item.title}》" + (f"（{len(found['parts'])} 集）" if found["parts"] else ""))
        return found

    def _keep_item(self, found: dict) -> None:
        item = found["item"]
        self._pc_items.pop((item.site, item.bvid), None)
        self._pc_items[(item.site, item.bvid)] = (self.monotonic(), dict(found, asked=None, missing=False))
        while len(self._pc_items) > PC_ITEMS_MAX:
            self._pc_items.pop(next(iter(self._pc_items)))

    def _pc_resolve(self, site: str, vid: str, title: str = "", playlist: str = "", user: int | None = None,
                    fallback: bool = False, text: str = "") -> dict:
        """取一个投到电脑的视频的信息和它的各集。fallback：从投屏历史重播时，取不到信息也照放（没有选集、
        不知道时长），和以前一样；分集信息取不到时只是没有选集。"""
        client = self.clients[site]
        found = {"item": None, "parts": [], "kind": "", "name": "", "default": 1, "asked": None, "list": playlist}
        if site == "bilibili" and vid.startswith("ss"):
            login = self._vip_login(user)
            name, episodes = client.series(vid, login=login) if login is not None else client.series(vid)
            found.update(item=Video(bvid=vid, title=name or title, duration=0, pubdate=0, site="bilibili"),
                         parts=list(episodes), kind="pgc", name=name)
            asked = re.search(r"/bangumi/play/ep(\d{1,12})", text)
            if asked:
                at = next((n for n, e in enumerate(episodes, 1) if e.bvid.startswith(f"ep{asked.group(1)}/")), None)
                found["asked"] = at
                # 点名的那一集不在列表里（会员集而这个人不是大会员，或者下架、付费）：别换成别的集
                # （首页观看记录带着的进度是那一集的），_pc_cast_item 说清楚。
                found["missing"] = at is None
            return found
        if not vid and site == "youtube" and playlist:
            name, videos = client.series(playlist)
            videos = list(videos)
            found.update(item=videos[0], parts=videos, kind="playlist", name=name)
            return found
        try:
            found["item"] = self._yt_video(vid) if site == "youtube" else None
            if found["item"] is None:
                found["item"] = client.video(vid)
        except SourceError:
            if not fallback:
                raise
            found["item"] = Video(bvid=vid, title=title, duration=0, pubdate=0, site=site)
        try:
            if site == "bilibili":
                getter = getattr(client, "parts", None)
                parts = getter(vid) if getter is not None else None
                if parts:
                    kind, name, videos = parts
                    found.update(parts=list(videos), kind=kind, name=name)
                    if kind == "season":
                        found["default"] = next((n for n, v in enumerate(videos, 1)
                                                 if v.bvid.split("/", 1)[0] == vid), 1)
                    else:
                        asked = re.search(r"[?&]p=(\d{1,4})\b", text)
                        if asked and 1 <= int(asked.group(1)) <= len(videos):
                            found["asked"] = int(asked.group(1))
            elif playlist:
                # 播放列表里的一个视频：列表就是选集（这个视频不在列表里时只放它）。
                name, videos = client.series(playlist)
                videos = list(videos)
                ids = [v.bvid for v in videos]
                if vid in ids:
                    found.update(parts=videos, kind="playlist", name=name, default=ids.index(vid) + 1)
                    if text:
                        found["asked"] = ids.index(vid) + 1
        except SourceError as error:
            self.log(f"投到电脑：{vid} 的分集取不到（{error}），只放这一个")
        return found

    def _pc_cast_channel(self, dev: dict, viewer: int | None = None, start: float | None = None,
                         prefer: str | None = None):
        """点名设备时「我的投屏」台：那台的投屏位，从它存的进度起（start 给了就从 start 起）。"""
        slot = self._cast_of(dev)
        with slot.lock:
            video, position, done = slot.cast, slot.cast_position, slot.cast_done
        if video is None:
            return "还没有投过视频：到「投屏」页粘贴链接"
        offset = _clamp(start, video.duration) if start is not None else \
            0.0 if done else max(0.0, position - RESUME_BACK_S)
        source = self._video_source(("video", "cast", video.site, video.bvid), video, offset,
                                    viewer=viewer, owner=dev.get("owner"), prefer=prefer)
        meta = {"title": video.title, "name": table.CAST_LABEL, "site": video.site, "duration": video.duration}
        return source, f"v:{video.site}:{video.bvid}", {}, meta, video.duration

    def _pc_subscription(self, caller: Caller, account: State, cid: str, body: dict, start: float | None = None,
                         prefer: str | None = None):
        """自己订阅的 UP 主、剧集：(片源或原因, 进度的键, 回答里多给的 {episode, total, index, parts, resume},
        存进度用的, 时长)。"""
        creator = account.get(cid)
        if creator is None:
            return "没有这个台"
        self.refresh_if_stale(cid, account)
        with account.lock:
            videos = list(creator.videos)
            positions = dict(creator.positions)
        if not videos:
            return "这个台还没有视频（列表取不到，过一会儿再试）"
        ids = [v.bvid for v in videos]

        def saved(video) -> float:
            # 小电视存的进度（只读）：和 State.next_video 一样往回退一点接上前情。
            return max(0.0, positions[video.bvid] - RESUME_BACK_S) if video.bvid in positions else 0.0

        cursor = self._pc_cursor.get((caller.user, cid))
        here = cursor["bvid"] if cursor is not None and cursor["bvid"] in ids else None
        # 这次开着以前（中转重启过）在电脑上看到的地方：pc-progress.json。
        stored = self._pc_saved(caller.user, cid) if here is None else None
        if stored is not None and stored.get("bvid") in ids:
            here = stored["bvid"]
        done = self.pc_progress.done(caller.user, cid) if self.pc_progress is not None else set()

        def pc_here() -> float:
            return self._pc_position(cursor) if cursor is not None and cursor["bvid"] == here \
                else float(stored.get("position") or 0) if stored else 0.0

        episode, step = body.get("episode"), body.get("step")
        if isinstance(episode, int) and not isinstance(episode, bool):
            if not 1 <= episode <= len(videos):
                return f"没有第 {episode} 集"
            video = videos[episode - 1]
            offset = pc_here() if here == video.bvid else saved(video)
        elif isinstance(step, int) and not isinstance(step, bool) and step in (-1, 1):
            base = here or (account.peek_video(cid) or videos[0]).bvid
            video = videos[(ids.index(base) + step) % len(videos)]
            offset = 0.0
        elif here is not None:
            video = videos[ids.index(here)]
            offset = pc_here()
            if here in done and offset >= (video.duration or 0) - 5 and ids.index(here) + 1 < len(videos):
                video, offset = videos[ids.index(here) + 1], 0.0      # 电脑上看完了这一集：接着下一集
        else:
            video = account.peek_video(cid) or videos[0]
            offset = saved(video)
        if start is not None:
            offset = _clamp(start, video.duration)
        elif video.duration and offset >= video.duration - 5:
            offset = 0.0
        n = ids.index(video.bvid) + 1
        series = creator.kind == "series"
        extra = {"_name": creator.name, "index": n, "total": len(videos), "series": series,
                 "parts": self._parts_answer("series" if series else "up", creator.name, videos, n, done)}
        if series:
            extra["episode"] = n
        if start is None and offset > 0:
            extra["resume"] = round(offset, 1)
        meta = {"title": video.title, "name": creator.name, "duration": video.duration, "index": n,
                "total": len(videos), "series": series, "site": video.site}
        source = self._video_source(("video", cid, video.site, video.bvid), video, offset,
                                    viewer=caller.user, owner=caller.user, prefer=prefer)
        return source, cid, extra, meta, video.duration

    def youtube_state(self) -> str:
        """ok / login（cookies 过期）/ error / unknown / unreachable（节点连不上）。"""
        now = self.clock()
        cached = self._youtube_health
        if cached is not None and now - cached[0] < YOUTUBE_HEALTH_S:
            return cached[1]
        try:
            state = str(self.clients["youtube"].health().get("state") or "unknown")
        except SourceError:
            state = "unreachable"
        self._youtube_health = (now, state)
        return state

    def switch(self, channel: str, caller: Caller = LEGACY, device: str | None = None) -> dict:
        dev = self._pick(caller, device)
        if channel not in {cid for cid, _ in self.channel_rows(dev)}:
            return {"ok": False, "error": "没有这个台"}
        playing = self._playing(dev)
        if not playing["online"]:
            return {"ok": False, "error": f"{dev['name']}没在线"}
        if not playing["switch"]:
            return {"ok": False, "error": "小电视的固件不支持遥控，请在小电视上手动换台"}
        self._event(dev, "switch", channel=channel)
        # at：中转此刻的时间。手机等到小智报告的会话开始时间晚于它，才算切换完成。
        return {"ok": True, "at": self.clock()}

    def play_subscription(self, channel: str, caller: Caller = LEGACY, device: str | None = None) -> dict:
        """电脑版「订阅」页的「在小电视上播放」：让选的那台换到这个 UP 主或剧集的台（和遥控换台同一个 switch）。

        台 id（up01、sr03）是账号里的编号，设备的频道表按设备主人的订阅写。多用户时只有主人的设备上
        这个编号才是这个 UP 主；遥控别人的设备（成员）时不换，免得换到主人那边同号的另一个台。"""
        state = self._account(caller)
        creator = state.get(channel)
        if creator is None:
            return {"ok": False, "error": "没有这个台"}
        dev = self._pick(caller, device)
        if self.tenant and not dev["id"]:
            return {"ok": False, "error": "还没有绑定小电视"}
        if self.tenant and dev["owner"] != caller.user:
            return {"ok": False, "error": f"{dev['name']}放的是主人订阅的台，只能在主人的账号里换到这里"}
        answer = self.switch(channel, caller, dev["id"] or None)
        if answer.get("ok"):
            answer.update(channel=channel, name=creator.name)
        return answer

    def set_group(self, channel: str, group, caller: Caller = LEGACY) -> dict:
        state = self._account(caller)
        names = dict(self._rows_of(state))
        if channel not in names or channel_groups.classify(channel, names[channel]) in (
                channel_groups.CAST, channel_groups.CREATORS):
            return {"ok": False, "error": "这个台不能调整分组"}
        if group is not None and group not in channel_groups.GROUPS[2:]:
            return {"ok": False, "error": "没有这个分组"}
        if not self.tenant and self.groups_out is None:
            return {"ok": False, "error": "没有配置分组文件"}
        # 多用户时只改用户自己的那一层；「自动」和选回默认分组（总后台设的，或按名字归的）都是去掉这一条。
        overrides = self._overrides(state) if not self.tenant else dict(state.group_overrides)
        default = (admin.global_groups(self).get(channel) if self.tenant else None) or \
            channel_groups.classify(channel, names[channel])
        if group is None or group == default:
            overrides.pop(channel, None)
        else:
            overrides[channel] = group
        if self.tenant:
            with state.lock:
                state.group_overrides = overrides
            state.save()
            self._tables_changed(state)
        else:
            channel_groups.save_overrides(self.groups_out, overrides)
        self.log(f"分组 {channel} {names[channel]} → {group or '自动'}")
        return {"ok": True}

    # ---- 管理 ----

    def summary(self, caller: Caller = LEGACY, device: str | None = None) -> dict:
        # device：电脑版点名看哪一台（不改手机选的那台）；别人的设备 NotFound。
        dev = self._pick(caller, device) if device else self.target(caller)
        try:
            account = self._account(caller)
        except NotFound:
            account = None
        # 没有小电视的用户：频道表按自己的订阅排（电脑上看用），而不是空的。
        groups = self.groups(dev, account if self.tenant and not dev["id"] and account is not None else None)
        creators, series, history = [], [], []
        if account is not None:
            with account.lock:
                # left、total 只数中转留着的最新 LIST_SIZE 个投稿；count 是 UP 主一共有多少个（不知道为 0）。
                creators = [{"id": c.id, "name": c.name, "site": c.site, "source": c.source,
                             "left": sum(v.bvid not in c.watched for v in c.videos),
                             "total": len(c.videos),
                             "count": max(account.totals.get(c.id) or self._upload_total(c.site, c.source),
                                          len(c.videos))}
                            for c in account.creators if c.kind == "up"]
                series = [{"id": c.id, "name": c.name, "site": c.site, "source": c.source,
                           "episode": account.episode(c.id),
                           "total": len(c.videos)} for c in account.creators if c.kind == "series"]
                history = [dict(h) for h in account.history]
        cast_slot = self._cast_of(dev)
        with cast_slot.lock:
            cast = ({"title": cast_slot.cast.title, "done": cast_slot.cast_done}
                    if cast_slot.cast is not None else None)
        summary = {"now": self.now(groups, dev), "groups": groups, "creators": creators,
                   "series": series, "room": self.room(caller=caller) if account is not None else None,
                   "cast": cast, "history": history,
                   "devices": self.device_rows(caller), "auto": self._targets.get(caller.key) is None,
                   "group_names": list(channel_groups.GROUPS[2:])}
        if self.tenant:
            summary["me"] = {"name": caller.name, "role": caller.role}
            if self.watch is not None and not caller.guest:
                # 在电脑上看（watch.py）：只有自建 / 电脑客户端有，电脑版据此显示按钮。
                summary["watch"] = True
                if self.watch.warm_max:
                    # 换台快一点：电脑版看直播时让中转预热相邻的台（watch_warm）。
                    summary["watch_warm"] = True
            if self.bili_accounts is not None and not caller.guest and caller.user is not None:
                # 自建：每个人自己的 B 站登录（bililogin.py），电脑版据此显示「设置 → B 站账号」和导入关注。
                summary["bili"] = self.bili_accounts.status(caller.user)
            if self.account_feeds is not None and not caller.guest and caller.user is not None:
                # 首页「B 站推荐」等几排和「UP 主 → 我的关注」（accountfeeds.py）：电脑版据此显示，云端没有。
                summary["feeds"] = True
            if self.selfhost is not None:
                # 自建：绑定码是 6 位数字，遥控页和电脑版的输入框据此换成数字键盘（云端没有这一项，还是 20 位）。
                summary["claim_digits"] = tenancy_db.CLAIM_DIGITS
        if self.guide is not None and self.guide.enabled:
            # 节目单打开了：遥控页和电脑版据此去问 /api/epg，在台名下面写「正在播：…」。
            summary["epg"] = True
        if self.tally is not None and not caller.guest:
            # 直播页最前面的「常看」分组：这个人看得最多的直播台（台 id，从多到少，livetally.py）。
            summary["frequent"] = self.tally.top(caller.user if self.tenant else None)
        if self.linecheck is not None:
            # 直播页：有真画面封面的台（台 id → 封面的时刻，页面拼 api/art/live/<id>?v=…）和全部线路都不通的台
            # （卡片上标「暂时打不开」）。
            status = self.linecheck.status()
            covers = self.linecheck.covers
            summary["live_status"] = {"covers": covers.stamps() if covers is not None else {},
                                      "dead": status["dead"]}
        if dev["id"]:
            # 遥控面板的「定时关」（POST /api/sleep，和语音的定时是同一个）：还剩几秒，或「看完这集」。
            summary["sleep"] = voicecmds.timer_of(self, dev)
        if self.clients.get("youtube") is not None:
            summary["youtube"] = {"state": self.youtube_state()}
            if getattr(self, "youtube_kind", "") == "local":
                summary["youtube"]["kind"] = "local"   # 自建的本机模式：遥控页的提示不说「海外节点」
        elif self.selfhost is not None:
            # 家庭服务器、电脑客户端：投屏页提示去「设置 → YouTube」打开它。
            summary["youtube_off"] = True
        return summary

    def _youtube_off(self, cloud: str) -> str:
        """YouTube 没配置时的回答：自建的指向「设置 → YouTube」，云端照旧。"""
        return YOUTUBE_OFF_SELFHOST if self.selfhost is not None else cloud

    def add_creator(self, text: str, caller: Caller = LEGACY) -> dict:
        state = self._account(caller)
        text = (text or "").strip()
        if not text:
            return {"ok": False, "error": "请先粘贴链接"}
        site = _site_of(text)
        if self.clients.get(site) is None:
            return {"ok": False, "error": self._youtube_off("YouTube 还没配置好，暂时只能添加 B 站 UP 主")}
        try:
            mid = self.clients[site].creator_id(text)
            if mid is None:
                return {"ok": False, "error": "看不懂这个链接。请粘贴 UP 主（频道）主页链接，或他的任意一个视频链接"}
            name, videos = self.clients[site].creator(mid)
        except SourceError as error:
            self.log(f"添加 UP 主失败：{error}")
            return {"ok": False, "error": f"{_SITE_NAMES[site]}暂时连不上或拒绝了请求，请稍后再试"}
        try:
            creator = state.add(site, mid, name, videos, self.clock())
        except ValueError as error:
            return {"ok": False, "error": str(error)}
        state.set_total(creator.id, self._upload_total(site, mid))
        state.save()
        self._cache_videos(creator, creator.videos, creator.fetched_at)
        self._tables_changed(state)
        self.log(f"添加 UP 主 {creator.id} {name}（mid {mid}，{len(videos)} 个投稿）")
        return {"ok": True, "creator": {"id": creator.id, "name": name}}

    def room(self, extra: str = "", caller: Caller = LEGACY) -> dict:
        """设备频道列表还剩多少：字节和台数。extra 是准备加的台名。"""
        return self.room_of(self._account(caller), extra)

    def room_of(self, state: State, extra: str = "") -> dict:
        """同 room()，按一份状态算（语音订阅按设备主人的状态，见 voice.py）。

        只算自己的台：它们在设备列表的最前面，必须整份收得下；全局表再长也只是在后面少显示几个台
        （电视服务截断），不挡加 UP 主、剧集。"""
        rows = self._own_rows(state) + ([("sr00", extra)] if extra else [])
        used = channel_groups.config_bytes(rows) + CONFIG_SPARE_BYTES
        return {"bytes": max(0, channel_groups.CONFIG_MAX - used),
                "channels": max(0, channel_groups.CHANNEL_MAX - len(rows)),
                "fits": used <= channel_groups.CONFIG_MAX and len(rows) <= channel_groups.CHANNEL_MAX}

    def add_series(self, text: str, name: str = "", caller: Caller = LEGACY) -> dict:
        """番剧、多集视频或合集成为一个台，按集数顺序播，记住看到哪一集。"""
        state = self._account(caller)
        text, name = (text or "").strip(), " ".join((name or "").split())[:SERIES_NAME_MAX]
        if not text:
            return {"ok": False, "error": "请先粘贴链接"}
        site = _site_of(text)
        client = self.clients.get(site)
        if client is None:
            return {"ok": False, "error": self._youtube_off("YouTube 还没配置好，剧集暂时只能用 B 站")}
        login = self._vip_login(caller.user) if site == "bilibili" else None
        try:
            source = client.series_id(text)
            if source is None:
                return {"ok": False, "error": "看不懂这个链接。请粘贴 B 站番剧页、多集视频（合集）的链接，"
                                              "或 YouTube 播放列表的链接（带 list=）"}
            title, episodes = client.series(source, login=login) if login is not None else client.series(source)
        except SourceError as error:
            self.log(f"添加剧集失败：{error}")
            if not error.permanent:
                return {"ok": False, "error": f"{_SITE_NAMES[site]}暂时连不上或拒绝了请求，请稍后再试"}
            # 「只有一集」「都要会员」之类原样告诉用户；接口的错误码换成人话。
            reason = str(error).split(": ", 1)[-1]
            if reason.startswith(("code=", "404")):
                reason = f"{_SITE_NAMES[site].strip()}说这个放不了（可能已删除、私享或需要会员）"
            if "会员" in reason and self.bili_accounts is not None and login is None:
                reason += "。需要大会员：在「设置 → B 站账号」扫码登录大会员账号后再加"
            return {"ok": False, "error": reason}
        name = name or title or source
        if not self.room(table.label_for(name, site), caller)["fits"]:
            return {"ok": False, "error": "小电视的频道列表满了，先删掉几个台再加"}
        try:
            creator = state.add(site, source, name, episodes, self.clock(), kind="series")
        except ValueError as error:
            return {"ok": False, "error": str(error)}
        state.save()
        if login is None:
            self._cache_videos(creator, creator.videos, creator.fetched_at)
        self._tables_changed(state)
        self.log(f"添加剧集 {creator.id} {name}（{source}，{len(creator.videos)} 集）")
        return {"ok": True, "series": {"id": creator.id, "name": name, "total": len(creator.videos)}}

    def cast(self, text: str, caller: Caller = LEGACY, device: str | None = None) -> dict:
        dev = self._pick(caller, device)
        text = (text or "").strip()
        if not text:
            return {"ok": False, "error": "请先粘贴视频链接"}
        site = _site_of(text)
        if self.clients.get(site) is None:
            return {"ok": False, "error": self._youtube_off("YouTube 还没配置好，暂时只能投 B 站视频")}
        try:
            bvid = self.clients[site].video_id(text)
            if bvid is None and site == "bilibili" and (parse_series_link(text) or "")[:2] in ("ss", "ep", "md"):
                return {"ok": False, "error": "番剧、影视剧在小电视上要先「加为剧集」（搜索结果里有这个按钮），"
                                             "或者在这台电脑上看"}
            if bvid is None:
                return {"ok": False, "error": "看不懂这个链接。请粘贴视频链接（App 里“分享 → 复制链接”）"}
            video = self.clients[site].video(bvid)
        except SourceError as error:
            self.log(f"投屏失败：{error}")
            if error.permanent:
                return {"ok": False, "error": "这个视频放不了（可能已删除、私享或需要会员）"}
            return {"ok": False, "error": f"{_SITE_NAMES[site]}暂时取不到，请稍后再试"}
        return self._cast_video(video, caller, dev)

    def _cast_video(self, video, caller: Caller, dev: dict, position: float = 0.0) -> dict:
        """position：从这里接着放（语音「继续看刚才投屏的」，voicecmds.recast）。"""
        if self.tenant and not dev["id"]:
            return {"ok": False, "error": "还没有绑定小电视"}
        cast = self._cast_of(dev)
        with cast.lock:
            previous, at, done = cast.cast, cast.cast_position, cast.cast_done
        cast.set_cast(video)
        if position > 0:
            cast.save_cast_position(video.bvid, position)
        # 这台设备的投屏历史（语音「重播刚才那个视频」「上一个投屏」）。
        voicecmds.remember_cast(self, dev, video, previous, at, done)
        try:
            # 投屏历史记在投屏的人名下；访客没有历史。
            account = self._account(caller)
        except NotFound:
            account = None
        if account is not None:
            account.remember(video, at=self.clock())
        if account is not cast:
            cast.save()
        if account is not None:
            account.save()
        if not self.tenant:
            self.write_table()
        self.log(f"投屏 {video.bvid} 《{video.title}》")
        self._event(dev, "cast")
        return {"ok": True, "title": video.title, "at": self.clock()}

    def replay(self, site: str, video_id: str, caller: Caller = LEGACY, device: str | None = None) -> dict:
        entry = self._account(caller).history_entry(site, video_id)
        if entry is None:
            return {"ok": False, "error": "历史里没有这个视频"}
        if self.clients.get(site) is None:
            return {"ok": False, "error": "YouTube 还没配置好"}
        if site == "bilibili" and video_id.startswith("ss"):
            # 投到电脑的番剧（一整季，watch_direct）：小电视上要加成剧集才能看。
            return {"ok": False, "error": "这是一整季番剧：在电脑上看，或者到「剧集」里加上它，在小电视上看"}
        return self._cast_video(Video(bvid=video_id, title=entry.get("title", ""), duration=0,
                                      pubdate=0, site=site), caller, self._pick(caller, device))

    def forget(self, site: str, video_id: str, caller: Caller = LEGACY) -> dict:
        state = self._account(caller)
        if not state.forget(site, video_id):
            return {"ok": False, "error": "历史里没有这个视频"}
        state.save()
        return {"ok": True}

    def remove_creator(self, cid: str, caller: Caller = LEGACY) -> dict:
        state = self._account(caller)
        if not state.remove(cid):
            return {"ok": False, "error": "没有这个台"}
        state.save()
        self._tables_changed(state)
        self.log(f"删除 {cid}")
        return {"ok": True}

    # ---- 多用户：设备绑定、管理、邀请 ----

    def my_devices(self, caller: Caller) -> list[dict]:
        if caller.guest or caller.user is None:
            raise NotFound("devices")
        now = self.clock()
        rows = []
        for row in self.accounts.devices_of(caller.user):
            dev = self._device_entry(row)
            rows.append({"id": dev["id"], "name": dev["name"], "model": dev["model"], "fw": dev["fw"],
                         "legacy": dev["legacy"], "mine": row["member_role"] == "owner",
                         "online": now_playing.read(dev["now"], now)["online"]})
        return rows

    def claim_route(self, path: str, short, long):
        """路径里的绑定码：20 位的总认；6 位数字只在自建时认。"""
        match = long.match(path)
        if match is None and self.selfhost is not None:
            match = short.match(path)
        return match

    def _find_claim(self, claim: str, ip: str) -> dict | None:
        """按绑定码找未绑定的设备；自建时先过错码限流（太频繁时 Limited），找不到算一次错码。"""
        guard = self.claim_guard
        if guard is not None:
            guard.check(ip)
        row = self.accounts.device_by_claim(claim)
        if row is None and guard is not None:
            guard.failed(ip)
        return row

    def bind_info(self, claim: str, caller: Caller, ip: str = "") -> dict:
        if caller.guest or caller.user is None:
            raise NotFound("bind")
        try:
            row = self._find_claim(claim, ip)
        except Limited as error:
            return {"ok": False, "error": str(error), "limited": True}
        if row is None:
            return {"ok": False, "error": "这个绑定码不对，或者设备已经绑定过了"}
        return {"ok": True, "model": row["model"], "tail": row["id"][-4:].upper()}

    def bind_preview(self, claim: str, ip: str = "") -> dict:
        """没登录（或只是访客）的手机打开绑定码：设备是什么型号、尾号几位，可以「创建账号并绑定」。"""
        try:
            row = self._find_claim(claim, ip)
        except Limited as error:
            return {"ok": False, "error": str(error), "limited": True}
        if row is None:
            return {"ok": False, "error": "这个绑定码不对，或者设备已经绑定过了"}
        return {"ok": True, "model": row["model"], "tail": row["id"][-4:].upper(), "register": True}

    def register_bind(self, claim: str, name: str, ip: str = "", agent: str = "") -> tuple[dict, str | None]:
        """创建账号并绑定这台设备：(给页面的回答, 新会话或 None)。"""
        guard = self.claim_guard
        try:
            if guard is not None:
                guard.check(ip)
            user_id, token, row = self.signup.register(claim, name, ip, agent)
        except Limited as error:
            return {"ok": False, "error": str(error), "limited": True}, None
        except ValueError as error:
            if guard is not None:
                guard.failed(ip)
            return {"ok": False, "error": str(error)}, None
        with self._cache_lock:
            self._casts.pop(row["id"], None)
        self.sync_devices()
        self.log(f"新用户 {user_id} 扫绑定码注册，{row['id']} 绑定给他")
        return {"ok": True, "device": {"id": row["id"], "name": row["name"]},
                "redirect": f"/tv/?s={token}&home=1"}, token

    def bind(self, claim: str, caller: Caller, ip: str = "") -> dict:
        if caller.guest or caller.user is None:
            raise NotFound("bind")
        guard = self.claim_guard
        try:
            if guard is not None:
                guard.check(ip)
            row = self.accounts.bind(claim, caller.user, ip)
        except Limited as error:
            return {"ok": False, "error": str(error), "limited": True}
        except ValueError as error:
            if guard is not None:
                guard.failed(ip)
            return {"ok": False, "error": str(error)}
        with self._cache_lock:
            self._casts.pop(row["id"], None)
        self.sync_devices()
        self.log(f"{row['id']} 绑定到用户 {caller.user}")
        return {"ok": True, "device": {"id": row["id"], "name": row["name"]}}

    def _owned(self, device_id: str, caller: Caller) -> dict:
        """主人（或管理员）才能改名、解绑、重新配对。"""
        if caller.guest or caller.user is None:
            raise NotFound(device_id)
        role = self.accounts.member_role(device_id, caller.user)
        if role != "owner" and not (caller.role == "admin" and role is not None):
            raise NotFound(device_id)
        return self.accounts.device(device_id)

    def rename_device(self, device_id: str, name: str, caller: Caller) -> dict:
        self._owned(device_id, caller)
        try:
            name = self.accounts.rename(device_id, name, caller.user)
        except ValueError as error:
            return {"ok": False, "error": str(error)}
        self.sync_devices()
        return {"ok": True, "name": name}

    def unbind_device(self, device_id: str, caller: Caller, ip: str = "") -> dict:
        self._owned(device_id, caller)
        try:
            self.accounts.unbind(device_id, caller.user, ip)
        except ValueError as error:
            return {"ok": False, "error": str(error)}
        with self._cache_lock:
            self._casts.pop(device_id, None)
        self._targets = {k: v for k, v in self._targets.items() if v != device_id}
        self._synced.pop(device_id, None)
        self.log(f"{device_id} 解绑")
        return {"ok": True}

    def repair_device(self, device_id: str, caller: Caller) -> dict:
        row = self._owned(device_id, caller)
        if row["legacy_port"] is not None:
            return {"ok": False, "error": "旧固件的设备没有自己的密钥，不用重新配对"}
        self.accounts.start_repair(device_id, caller.user)
        return {"ok": True, "note": "10 分钟内让小电视重新连上网络，它的新密钥会被记住"}

    def create_invite(self, name: str, caller: Caller) -> dict:
        if caller.role != "admin":
            raise NotFound("invites")
        code = self.accounts.create_invite(caller.user, "user", name)
        return {"ok": True, "link": f"{self.public_url}/tv/i/{code}", "code": code}


_SITE_NAMES = {"bilibili": "B 站", "youtube": "YouTube "}
YOUTUBE_OFF_SELFHOST = "YouTube 还没开启：去「设置 → YouTube」填代理即可"


def _phone_device(device: dict | None) -> dict | None:
    """设备报告的亮度是固件的档位号（0 最亮）；给手机时换成越大越亮。"""
    if device is None:
        return None
    phone = {"volume": device["volume"], "brightness": device["steps"] - 1 - device["brightness"],
             "steps": device["steps"]}
    for key in ("pet", "audio_only"):
        if key in device:
            phone[key] = device[key]
    return phone


def _phone_listen(listen: dict | None) -> dict | None:
    """「用手机听」给手机的部分：能不能听（ok）、有没有人在听（on）、H。电视服务没开就是 None。"""
    if listen is None:
        return None
    return {"ok": listen["ok"], "on": listen["on"], "lead": listen["lead"]}


def _site_of(text: str) -> str:
    return "youtube" if youtube_links.is_youtube(text) else "bilibili"


def _stream_handler(relay: Relay):
    class StreamHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def do_GET(self):
            path = urllib.parse.urlsplit(self.path).path
            if path.startswith("/info/"):
                self._info(path)
                return
            if relay.tenant:
                match = _DEVICE_CHANNEL.match(path)
                if not match:
                    self.send_error(404)
                    return
                relay.play(match.group(2), self, slot=match.group(1))
                return
            match = _CHANNEL.match(path)
            if not match:
                self.send_error(404)
                return
            relay.play(match.group(1), self)

        def _info(self, path: str) -> None:
            if relay.tenant:
                match = _DEVICE_INFO.match(path)
                info = relay.stream_info(match.group(2), slot=match.group(1)) if match else None
            else:
                match = _INFO.match(path)
                info = relay.stream_info(match.group(1)) if match else None
            if info is None:
                self.send_error(404)
                return
            body = json.dumps(info, ensure_ascii=False).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    return StreamHandler


def _manage_handler(relay: Relay):
    class ManageHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _path(self) -> str:
            path = urllib.parse.urlsplit(self.path).path
            # nginx 把 /tv/ 去掉前缀转进来；直接访问本端口时也接受带前缀的路径。
            if path == "/tv" or path.startswith("/tv/"):
                path = path[3:] or "/"
            return path

        def _query(self) -> dict:
            return {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).items()}

        def _cookie(self) -> str:
            jar = http.cookies.SimpleCookie()
            try:
                jar.load(self.headers.get("Cookie") or "")
            except http.cookies.CookieError:
                return ""
            morsel = jar.get(COOKIE)
            return morsel.value if morsel else ""

        def _ip(self) -> str:
            """手机的地址：经 nginx 转进来时看 X-Real-IP（只信本机转来的）。只用来记网段。"""
            peer = self.client_address[0]
            forwarded = self.headers.get("X-Real-IP") or ""
            return forwarded.strip() if peer in ("127.0.0.1", "::1") and forwarded else peer

        def _agent(self) -> str:
            return (self.headers.get("User-Agent") or "")[:160]

        def _session_caller(self, token: str | None) -> Caller | None:
            info = relay.accounts.session(token)
            if info is None:
                return None
            return Caller(user=info["user_id"], role=info["role"], device=info["device_id"], name=info["name"])

        def _caller(self) -> Caller | None:
            """这个请求是谁：单用户时持有密钥或登录过就是 LEGACY；多用户时按会话认人，密钥是管理员。"""
            if not relay.tenant:
                ok = (relay.key_ok(self.headers.get("X-Key"))
                      or relay.state.session_ok(self._cookie(), relay.clock()))
                return LEGACY if ok else None
            if relay.key_ok(self.headers.get("X-Key")):
                admin = relay.accounts.admin_id()
                user = relay.accounts.user(admin) if admin is not None else None
                return Caller(user=admin, role="admin", name=user["name"]) if user else None
            return self._session_caller(self._cookie())

        def _device_scoped(self, caller: Caller, path: str, device: str | None) -> Caller:
            """多用户：带密钥、点名设备的单设备遥控（电视服务的语音命令）当作那台设备的访客。

            密钥在多用户时是管理员，只能遥控管理员自己的设备；而电视服务要替连着它的任何一台
            已绑定设备做事（谁的都可能），又只该动那一台。访客正好是这个范围。手机（cookie）不受影响。"""
            if (relay.tenant and device and relay.key_ok(self.headers.get("X-Key"))
                    and (path in _DEVICE_SCOPED or _EPISODES.match(path) or _EPISODE_PLAY.match(path))):
                return Caller(role="guest", device=device, name="电视服务")
            return caller

        def _session_ok(self, token: str | None) -> bool:
            if not relay.tenant:
                return relay.state.session_ok(token, relay.clock())
            return self._session_caller(token) is not None

        def _https(self) -> bool:
            """这个请求是不是经 HTTPS 来的。云端（nginx 后面）一律当作是；自建模式看前端（server/front.py）
            给的 X-Forwarded-Proto，而且只信本机转来的，和 X-Real-IP 一样。"""
            if relay.selfhost is None:
                return True
            proto = (self.headers.get("X-Forwarded-Proto") or "").strip().lower()
            return self.client_address[0] in ("127.0.0.1", "::1") and proto == "https"

        def _cookie_header(self, token: str) -> str:
            # 明文 HTTP 的局域网地址上，浏览器不保存带 Secure 的 cookie：自建时只有 HTTPS 才带。
            secure = "Secure; " if self._https() else ""
            return (f"{COOKIE}={token}; HttpOnly; {secure}SameSite=Lax; Path=/tv/; "
                    f"Max-Age={SESSION_S}")

        def _send(self, code: int, body: bytes, kind: str, headers: dict | None = None) -> None:
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            # 默认不缓存；静态文件（ASSETS）自己给 Cache-Control，不能再叠一个 no-store（叠了就一直不缓存）。
            if "Cache-Control" not in (headers or {}):
                self.send_header("Cache-Control", "no-store")
            # 网址里可能带着密钥或扫码码，不能经 Referer 带到别处。
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Content-Type-Options", "nosniff")
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            if self.command in ("POST", "DELETE") and not getattr(self, "_read_body", True):
                # 没读请求体就回答了（登录失效的 404、跨站的请求……）：体还留在连接里，浏览器复用这条连接时
                # 下一个请求会被当成它的后半截（501）。关掉这条连接。
                self.send_header("Connection", "close")
                self.close_connection = True
            self.end_headers()
            self.wfile.write(body)

        def _json(self, data: dict, code: int = 200, headers: dict | None = None) -> None:
            self._send(code, json.dumps(data, ensure_ascii=False).encode(),
                       "application/json; charset=utf-8", headers)

        def _not_found(self) -> None:
            self._send(404, NOT_FOUND.encode(), "text/html; charset=utf-8")

        def _login(self) -> None:
            """验证过密钥或扫码码：发一个 180 天的会话 cookie，跳到遥控页。多用户时登录的是管理员。"""
            if relay.tenant:
                admin = relay.accounts.admin_id()
                if admin is None:
                    self._not_found()
                    return
                token = relay.accounts.new_session(admin, "phone", user_agent=self._agent(), ip=self._ip())
                relay.accounts.audit("login.key", admin, self._ip())
            else:
                token = relay.state.new_session(relay.clock())
                relay.state.save()
            # 会话也放进跳转后的网址：iPhone 加到桌面的网页应用不和 Safari 共用 cookie，
            # 从这个页面「添加到主屏幕」，图标会记住带会话的网址。它只是可作废的会话，不是密钥。
            self._send(302, b"", "text/plain",
                       {"Location": f"/tv/?s={token}", "Set-Cookie": self._cookie_header(token)})

        def _body(self):
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if not 0 < length <= MAX_BODY_BYTES:
                return None
            self._read_body = True
            try:
                body = json.loads(self.rfile.read(length))
            except ValueError:
                return None
            return body if isinstance(body, dict) else None

        def do_GET(self):
            try:
                self._get()
            except NotFound:
                self._not_found()

        def do_POST(self):
            self._read_body = not (self.headers.get("Content-Length") or "0").strip("0 ")
            if not console.same_origin(self.headers):
                self._not_found()       # 跨站来的（表单、别的网站的脚本）
                return
            try:
                self._post()
            except NotFound:
                self._not_found()

        def do_DELETE(self):
            self._read_body = not (self.headers.get("Content-Length") or "0").strip("0 ")
            if not console.same_origin(self.headers):
                self._not_found()
                return
            try:
                self._delete()
            except NotFound:
                self._not_found()

        def _get(self):
            path = self._path()
            if path in ASSETS:
                kind, data = ASSETS[path]
                if relay.selfhost is not None and data is ASSETS["/wifi"][1]:
                    # 自建：服务器栏填设备该连的地址（setup.wifi_page），不缓存，地址可能刚记下。
                    self._send(200, setup.wifi_page(relay, data), kind)
                    return
                # 启动时压好的那份（PACKED，static.py）：浏览器收 gzip 就给它，ETag 两种各一个。
                item = PACKED.get(path)
                if item is None or item.data is not data:
                    item = static.pack(kind, data)
                static.send(self, item, {"Cache-Control": "public, max-age=86400"})
                return
            # 自建模式：第一个管理员（/tv/setup）；还没设置时别的页面都指向它（setup.py）。
            if setup.get(self, relay, path):
                return
            # 电脑版和扫码登录（阶段 2）：页面、票据、登录列表……见 console.py；总后台（阶段 3）见 admin.py。
            if relay.tenant and (console.get(self, relay, path) or admin.get(self, relay, path)
                                 or ytsettings.get(self, relay, path) or bililogin.get(self, relay, path)
                                 or accountfeeds.get(self, relay, path) or follows.get(self, relay, path)):
                return
            # 在电脑上看的列表和段（watch.py）：只认地址里的令牌，不看登录；没开这个功能时 404。
            # 片库给中转自己的 ffmpeg 的文件（library.raw）：只认本机直接连进来的、带令牌的。
            if watch_pc.get(self, relay, path) or vod.get(self, relay, path) or library_api.raw(self, relay, path):
                return
            if relay.tenant:
                # 扫设备二维码、绑定码、邀请链接：落地页不用登录，页面里的脚本再带着 cookie 问接口。
                if relay.claim_route(path, _JOIN_SHORT, _JOIN):
                    static.send(self, PACKED_PAGES["join"])
                    return
                match = relay.claim_route(path, _BIND_INFO_SHORT, _BIND_INFO)
                if match:
                    # 没登录、或只是访客：可以「创建账号并绑定」；登录了的手机走下面原来的绑定确认。
                    member = self._caller()
                    if member is None or member.guest or member.user is None:
                        self._json(relay.bind_preview(match.group(1), self._ip()))
                        return
                match = _INVITE_INFO.match(path)
                if match:
                    invite = relay.accounts.invite(match.group(1))
                    self._json({"ok": True, "name": invite["name"]} if invite else
                               {"ok": False, "error": "这个邀请已经用过或过期了，请向管理员要一个新的"})
                    return
            else:
                match = _CODE.match(path)
                if match:
                    if relay.code_ok(match.group(1)):
                        self._login()
                    else:
                        self._not_found()
                    return
            if path == "/" and relay.key_ok(self._query().get("k")):
                self._login()
                return
            session = self._query().get("s")
            if path == "/" and self._session_ok(session):
                # 从桌面图标打开：网址里的会话有效，补上 cookie，后续接口调用就带着它。
                static.send(self, PACKED_PAGES["remote"], {"Set-Cookie": self._cookie_header(session)})
                return
            caller = self._caller()
            if caller is None:
                self._not_found()
                return
            device = self._query().get("device") or None
            caller = self._device_scoped(caller, path, device)
            if library_api.get(self, relay, caller, path):
                # 我的片库（library.py）：页面、文件、海报；只给登录的家里人，访客和云端 404。
                return
            if path == "/":
                static.send(self, PACKED_PAGES["remote"])
            elif path == "/api/state":
                summary = relay.summary(caller, device)
                if relay.tenant and not caller.guest and caller.user is not None:
                    # 别的手机通过设备「恢复登录」登进了这个账号：遥控页提示一次（console.recovery_notice）。
                    notice = console.recovery_notice(relay, caller.user, self._cookie() or self._query().get("s"))
                    if notice:
                        summary["notice"] = notice
                self._json(summary)
            elif path == "/api/listen/time":
                # 用手机听的对时（NTP 式，网页取往返最短的一次）：和电视服务同一台机器、同一个时钟。
                self._json({"t": relay.clock() * 1000})
            elif _EPISODES.match(path):
                self._json(relay.episodes(_EPISODES.match(path).group(1), caller, device))
            elif path == "/api/voice/catalog":
                self._json(voicecmds.catalog(relay, device, caller))
            elif path == "/api/voice/search":
                # 语音搜索视频或 UP 主（server/relay/voice.py）。
                query = self._query()
                self._json(voice.search(relay, device, caller, query.get("site"), query.get("q"),
                                        query.get("wait"), query.get("kind") or "video"))
            elif path in bilisearch.PATHS:
                # 电脑版「订阅」页的 B 站搜索和封面缩略图；和订阅接口一样要账号（访客 404）。
                bilisearch.get(self, relay, caller, path)
            elif path in ytsearch.PATHS:
                # 电脑版「搜索」页的 YouTube 搜索；和 B 站搜索一样要账号（访客 404）。
                ytsearch.get(self, relay, caller, path)
            elif path == "/api/epg":
                # 节目单（epg.py）：直播台现在和下一个节目；和频道列表一样，登录了的（访客也算）都能看。
                self._json(epg.api(relay, self._query()))
            elif path == "/api/search/local":
                # 搜索页最上面的几段（localsearch.py）：自己的直播台、节目单、片库、订阅；访客只有直播台和节目单。
                self._json(localsearch.search(relay, caller, self._query().get("q")))
            elif path == "/api/home":
                # 电脑版首页的几排（home.py）：按调用者，访客只有直播。
                self._json(home.home(relay, caller))
            elif path == "/api/targets":
                # 「播放到」：能遥控的小电视，和（家庭服务器、电脑客户端上）这台电脑。
                self._json(home.targets(relay, caller))
            elif _LOGO.match(path):
                # 直播台的台标（logos.py）：登录了的（访客也算）都能看，和节目单一样。
                found = relay.logos.get(_LOGO.match(path).group(1)) if relay.logos is not None else None
                if found is None:
                    # 没有台标也缓存一会儿：直播页每个台都会来问一次。
                    self._send(404, b"", "text/plain; charset=utf-8", {"Cache-Control": "private, max-age=3600"})
                else:
                    self._send(200, found[1], found[0], {"Cache-Control": "private, max-age=86400"})
            elif _LIVE_COVER.match(path):
                # 直播台的真画面封面（linecheck.py）：和台标一样，登录了的（访客也算）都能看；只在本机，不上传。
                covers = relay.linecheck.covers if relay.linecheck is not None else None
                found = covers.get(_LIVE_COVER.match(path).group(1)) if covers is not None else None
                if found is None:
                    self._send(404, b"", "text/plain; charset=utf-8", {"Cache-Control": "private, max-age=300"})
                else:
                    # 页面的地址带 ?v=<封面的时刻>：换了封面地址就变，所以可以缓存久一点。
                    self._send(200, found, "image/jpeg", {"Cache-Control": "private, max-age=86400"})
            elif relay.tenant and path == "/api/devices":
                self._json({"ok": True, "devices": relay.my_devices(caller)})
            elif relay.tenant and path == "/api/wifi":
                # 设备保存的 Wi-Fi（只有名字）和最近一次修改的结果：主人才看得到（server/relay/wifi.py）。
                self._json(wifi_api.get(relay, caller, device))
            elif relay.tenant and (claimed := relay.claim_route(path, _BIND_INFO_SHORT, _BIND_INFO)):
                self._json(relay.bind_info(claimed.group(1), caller, self._ip()))
            else:
                self._not_found()

        def _scan(self, body: dict) -> None:
            """扫了设备的遥控码：自己的设备就直接打开遥控（选中它）；否则给一个只管这台的访客会话。"""
            code = body.get("code") if isinstance(body.get("code"), str) else ""
            row = relay.accounts.device_by_remote_code(code)
            if row is None:
                self._json({"ok": False, "error": "这个二维码已经失效了，请重新扫小电视状态页上的码"})
                return
            token = self._cookie()
            caller = self._session_caller(token)
            if caller is not None and not caller.guest:
                if relay.accounts.member_role(row["id"], caller.user) is None:
                    # not_mine：遥控页「扫一扫」据此说明这是别人的访客码（而不是码失效了）。
                    self._json({"ok": False, "not_mine": True,
                                "error": "这台小电视不是你的，不能用你的账号遥控。想当访客遥控它，"
                                         "请用浏览器的无痕窗口扫码。"})
                    return
                relay.set_target(row["id"], caller)
                # name：遥控页「扫一扫」切到这台后提示用。
                self._json({"ok": True, "redirect": f"/tv/?s={token}", "name": row["name"]})
                return
            if caller is not None and caller.device == row["id"]:
                self._json({"ok": True, "redirect": f"/tv/?s={token}"})
                return
            token = relay.accounts.new_session(None, "guest", device_id=row["id"], user_agent=self._agent(),
                                               ip=self._ip())
            relay.accounts.audit("login.guest", None, self._ip(), "device", row["id"])
            self._json({"ok": True, "redirect": f"/tv/?s={token}"},
                       headers={"Set-Cookie": self._cookie_header(token)})

        def _register(self, body: dict) -> None:
            """没登录的手机扫了未绑定设备的绑定码，点了「创建账号并绑定这台设备」。"""
            claim, name = body.get("claim"), body.get("name") or ""
            if not isinstance(claim, str) or not isinstance(name, str):
                self._json({"ok": False, "error": "请求格式不对"}, 400)
                return
            caller = self._session_caller(self._cookie())
            if caller is not None and not caller.guest:
                self._json({"ok": False, "error": "这台手机已经登录了，请刷新页面，直接绑定到你的账号"})
                return
            answer, token = relay.register_bind(claim.strip().replace(" ", "").replace("-", ""), name,
                                                self._ip(), self._agent())
            self._json(answer, headers={"Set-Cookie": self._cookie_header(token)} if token else None)

        def _accept_invite(self, body: dict) -> None:
            code, name = body.get("code"), body.get("name") or ""
            if not isinstance(code, str) or not isinstance(name, str):
                self._json({"ok": False, "error": "请求格式不对"}, 400)
                return
            try:
                user_id, token = relay.accounts.accept_invite(code, name, self._agent(), self._ip())
            except ValueError as error:
                self._json({"ok": False, "error": str(error)})
                return
            relay.log(f"新用户 {user_id} 接受了邀请")
            self._json({"ok": True, "redirect": f"/tv/?s={token}"},
                       headers={"Set-Cookie": self._cookie_header(token)})

        def _post(self):
            path = self._path()
            if setup.post(self, relay, path):
                return
            if relay.tenant and (console.post(self, relay, path) or admin.post(self, relay, path)
                                 or ytsettings.post(self, relay, path) or bililogin.post(self, relay, path)
                                 or accountfeeds.post(self, relay, path) or follows.post(self, relay, path)):
                return
            if relay.tenant and path in ("/api/scan", "/api/invite/accept", "/api/bind/register"):
                body = self._body()
                if body is None:
                    self._json({"ok": False, "error": "请求格式不对"}, 400)
                elif path == "/api/scan":
                    self._scan(body)
                elif path == "/api/bind/register":
                    self._register(body)
                else:
                    self._accept_invite(body)
                return
            caller = self._caller()
            if caller is None:
                self._not_found()
                return
            body = self._body()
            if body is None:
                self._json({"ok": False, "error": "请求格式不对"}, 400)
                return
            device = body.get("device")
            if device is not None and device != "" and not isinstance(device, str):
                self._json({"ok": False, "error": "没有这台设备"}, 404)
                return
            device = device or None
            if device is not None and path != "/api/target":
                # 点名了设备（电视服务的语音命令总是点名）：动作作用在那台上，而不是手机遥控的那台。
                # /api/target 的 device 是另一个意思（手机选遥控哪台），由 set_target 自己查。
                caller = self._device_scoped(caller, path, device)
                found = relay.find_device(device, caller)
                if found is None:
                    if relay.tenant:
                        raise NotFound(device)      # 多用户：别人的设备和不存在一样，一律 404 页面
                    self._json({"ok": False, "error": "没有这台设备"}, 404)
                    return
                device = found["id"]
            if path == "/api/code":
                self._json(relay.new_code(caller, device))
                return
            if path == "/api/target":
                self._json(relay.set_target(device, caller))
                return
            if path == "/api/switch":
                self._json(relay.switch(str(body.get("channel") or ""), caller, device))
                return
            if path == "/api/subs/play":
                # 电脑版「订阅」页的「在小电视上播放」：换到这个 UP 主或剧集的台（Relay.play_subscription）。
                self._json(relay.play_subscription(str(body.get("channel") or ""), caller, device))
                return
            if path == "/api/groups":
                group = body.get("group")
                self._json(relay.set_group(str(body.get("channel") or ""),
                                           group if isinstance(group, str) else None, caller))
                return
            if path == "/api/seek":
                # 语音还可能给 fraction、from_end 和 within（server/relay/voice.py 的 seek_target）。
                given = {k: body.get(k) for k in ("delta", "position", "fraction", "from_end")}
                given = {k: v for k, v in given.items() if isinstance(v, (int, float)) and not isinstance(v, bool)}
                if not given:
                    self._json({"ok": False, "error": "请求格式不对"}, 400)
                    return
                self._json(relay.seek(**given, within=body.get("within") is True, caller=caller, device=device))
                return
            if path == "/api/next":
                self._json(relay.next(caller, device))
                return
            if path == "/api/previous":
                self._json(relay.previous(caller, device))
                return
            match = _EPISODE_PLAY.match(path)
            if match:
                episode = body.get("episode")
                if not isinstance(episode, int) or isinstance(episode, bool):
                    self._json({"ok": False, "error": "请求格式不对"}, 400)
                    return
                self._json(relay.play_episode(match.group(1), episode, caller, device))
                return
            if path == "/api/device":
                volume, level = body.get("volume"), body.get("brightness")
                self._json(relay.device(volume=volume if isinstance(volume, (int, float)) else None,
                                        brightness=level if isinstance(level, int) else None,
                                        caller=caller, device=device))
                return
            if path == "/api/audio":
                self._json(relay.set_audio(body.get("on"), caller, device))
                return
            if path == "/api/listen":
                self._json(relay.listen(caller, device))
                return
            if path == "/api/watch":
                if any(body.get(k) is not None for k in ("channel", "url", "video", "library")):
                    # 直接看一个台（不经小电视）、片库里的一个视频，见 Relay.watch_direct。
                    self._json(relay.watch_direct(caller, device, body))
                else:
                    self._json(relay.watch_start(caller, device, body.get("viewer"), body.get("start"),
                                                 prefer=_prefer(body)))
                return
            if path == "/api/watch/stop":
                self._json(relay.watch_stop(caller, body.get("viewer"), body))
                return
            if path == "/api/watch/progress":
                self._json(relay.watch_progress(caller, body))
                return
            if path == "/api/watch/warm":
                self._json(relay.watch_warm(caller, body))
                return
            if path == "/api/watch/prepare":
                self._json(relay.watch_prepare(caller, body))
                return
            if path == "/api/yt/prefetch":
                self._json(relay.yt_prefetch(caller, body))
                return
            if path == "/api/pet":
                self._json(relay.set_pet(body.get("pet"), caller, device))
                return
            if path == "/api/pause":
                self._json(relay.pause(caller, device))
                return
            if path == "/api/play":
                # 电脑版「播放到」：一个入口，按目标和节目分到换台、订阅、选集、投屏、在电脑上看（home.py）。
                self._json(home.play(relay, caller, body))
                return
            if path == "/api/sleep":
                # 遥控面板的「定时关」（home.sleep → voicecmds.set_timer）。
                self._json(home.sleep(relay, caller, device, body.get("minutes")))
                return
            if path == "/api/resume":
                self._json(relay.resume(caller, device))
                return
            if path in ("/api/voice/cast", "/api/voice/creator"):
                # 语音搜到的视频投屏、UP 主订阅或播最新（server/relay/voice.py）；device 上面已经认过。
                act = voice.cast if path == "/api/voice/cast" else voice.creator
                self._json(act(relay, device, caller, body))
                return
            if path in voicecmds.PATHS:
                # 语音定时、订阅正在放的、删台、投屏历史（server/relay/voicecmds.py）。
                self._json(voicecmds.post(relay, path, device, caller, body,
                                          keyed=relay.key_ok(self.headers.get("X-Key"))))
                return
            if path == "/api/history/play":
                self._json(relay.replay(str(body.get("site") or ""), str(body.get("id") or ""), caller, device))
                return
            if path == "/api/series":
                url, name = body.get("url"), body.get("name") or ""
                if not isinstance(url, str) or not isinstance(name, str):
                    self._json({"ok": False, "error": "请求格式不对"}, 400)
                    return
                self._json(relay.add_series(url, name, caller))
                return
            if path in ("/api/creators", "/api/cast"):
                url = body.get("url")
                if not isinstance(url, str):
                    self._json({"ok": False, "error": "请求格式不对"}, 400)
                    return
                self._json(relay.add_creator(url, caller) if path == "/api/creators"
                           else relay.cast(url, caller, device))
                return
            if relay.tenant:
                self._post_tenant(path, body, caller)
                return
            self._not_found()

        def _post_tenant(self, path: str, body: dict, caller: Caller) -> None:
            if path == "/api/wifi":
                # 改设备保存的 Wi-Fi：加密后经电视服务发给设备（server/relay/wifi.py）。
                self._json(wifi_api.post(relay, caller, body, self._ip()))
                return
            if path == "/api/bind":
                claim = body.get("claim")
                if not isinstance(claim, str):
                    self._json({"ok": False, "error": "请求格式不对"}, 400)
                    return
                self._json(relay.bind(claim.strip().replace(" ", "").replace("-", ""), caller, self._ip()))
                return
            if path == "/api/invites":
                name = body.get("name") or ""
                self._json(relay.create_invite(name if isinstance(name, str) else "", caller))
                return
            match = _DEVICE_API.match(path)
            if match:
                device_id, action = match.group(1), match.group(2)
                if action == "unbind":
                    self._json(relay.unbind_device(device_id, caller, self._ip()))
                elif action == "repair":
                    self._json(relay.repair_device(device_id, caller))
                else:
                    name = body.get("name")
                    if not isinstance(name, str):
                        self._json({"ok": False, "error": "请求格式不对"}, 400)
                        return
                    self._json(relay.rename_device(device_id, name, caller))
                return
            self._not_found()

        def _delete(self):
            if setup.pending(relay):
                self._not_found()
                return
            caller = self._caller()
            if caller is None:
                self._not_found()
                return
            path = self._path()
            match = _CREATOR.match(path)
            if match:
                self._json(relay.remove_creator(match.group(1), caller))
                return
            match = _HISTORY.match(path)
            if match:
                self._json(relay.forget(match.group(1), match.group(2), caller))
                return
            self._not_found()

        def log_message(self, *_args):
            pass

    return ManageHandler


def make_servers(relay: Relay, stream_port: int, manage_port: int,
                 bind: str = "127.0.0.1") -> tuple[ThreadingHTTPServer, ThreadingHTTPServer]:
    streams = ThreadingHTTPServer((bind, stream_port), _stream_handler(relay))
    manage = ThreadingHTTPServer((bind, manage_port), _manage_handler(relay))
    streams.daemon_threads = manage.daemon_threads = True
    # 片库（library.py）：中转自己的 ffmpeg 从 127.0.0.1 的这个端口取文件（/api/library/raw）。
    relay.manage_port = manage.server_address[1]
    return streams, manage


# 设备上宠物的个数（固件 xiaozhi_tv_ui_assets.h 的 PET_COUNT）。
PET_COUNT = 11
_ASSET_DIR = Path(__file__).resolve().parent / "assets"
PAGE = (_ASSET_DIR / "remote.html").read_text(encoding="utf-8")
# 多用户：扫设备遥控码（/TV/R/）、绑定码（/TV/B/）、邀请链接（/tv/i/）的落地页。
JOIN_PAGE = (_ASSET_DIR / "join.html").read_text(encoding="utf-8")
# 清单和图标不带登录也要能取：浏览器取清单时不带 cookie。
ASSETS = {
    "/manifest.webmanifest": ("application/manifest+json",
                              (_ASSET_DIR / "manifest.webmanifest").read_bytes()),
    "/icon-180.png": ("image/png", (_ASSET_DIR / "icon-180.png").read_bytes()),
    "/icon-512.png": ("image/png", (_ASSET_DIR / "icon-512.png").read_bytes()),
    # RooTV 的图标（tools/make_icons.py 从 docs/design/media-center/brand/ 生成）。
    **{f"/{name}": (kind, (_ASSET_DIR / name).read_bytes()) for name, kind in (
        ("favicon.ico", "image/x-icon"), ("favicon.svg", "image/svg+xml"), ("apple-touch-icon.png", "image/png"),
        ("icon-192.png", "image/png"), ("icon-maskable-192.png", "image/png"),
        ("icon-maskable-512.png", "image/png"))},
    # 声波配网：手机把 Wi-Fi 名称和密码播成声音给配网中的电视听。不用登录，页面不连网。
    "/wifi": ("text/html; charset=utf-8", (_ASSET_DIR / "wifi.html").read_bytes()),
}
ASSETS["/TV/W"] = ASSETS["/wifi"]  # 二维码用的短地址；nginx 也把它转到 /wifi
# 扫码登录电脑：手机点「扫码登录电脑」时才加载（页面里写的是 /tv/scan.js?v=…，改了脚本就改版本号）。
# jsqr.js 是 jsQR 1.4.0（Apache-2.0，见 LICENSE.jsqr.txt），只在浏览器没有 BarcodeDetector 时加载。
ASSETS.update({f"/{name}": ("text/javascript; charset=utf-8", (_ASSET_DIR / name).read_bytes())
               for name in ("scan.js", "jsqr.js")})
# 电脑版（媒体中心界面）的样式和脚本（console.py：页面里的地址带内容散列）。
ASSETS.update(console.CONSOLE_ASSETS)
# 宠物图（设计稿导出的 3 倍图），手机遥控页的「我的宠物」用。
ASSETS.update({f"/pets/{p.name}": ("image/png", p.read_bytes())
               for p in sorted((_ASSET_DIR / "pets").glob("*.png"))})
# 启动时各压一次 gzip、算好 ETag（static.py）：回答时只挑一份，不再花 CPU。图片不压（本来就压过）。
PACKED = {path: static.pack(kind, data) for path, (kind, data) in ASSETS.items()}
# 遥控页和落地页也一样（电脑版的几个页面在 console.PACKED_PAGES）。
PACKED_PAGES = {"remote": static.pack("text/html; charset=utf-8", PAGE.encode()),
                "join": static.pack("text/html; charset=utf-8", JOIN_PAGE.encode())}
NOT_FOUND = ("<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
             "<body style='background:#1c1915;color:#e6dcc8;font:16px/1.6 serif;padding:24px'>"
             "<p>打不开。请用小电视状态页上的二维码重新扫一下。</p>")
