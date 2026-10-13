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
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .. import channel_groups, now_playing
from ..tenancy import files as tenancy_files
from ..tenancy.tickets import LoginTickets
from . import console, events, stream, table, voice, wifi as wifi_api, youtube as youtube_links
from .bilibili import BilibiliError, SourceError
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
_DEVICE_API = re.compile(r"^/api/devices/([0-9a-f]{12}|legacy-[a-z0-9]{1,20})(?:/(unbind|repair))?$")
# 只作用在一台设备上的遥控接口。多用户时，带密钥（X-Key）并点名设备的这些请求——电视服务替
# 说话的那台设备发的语音命令——按那台设备的访客处理：只能动那一台，按设备主人的剧和进度来，
# 碰不到账号数据（唯一的例外：语音搜到的 UP 主可以订阅到主人名下，用户决定的）。见 voice.py。
_DEVICE_SCOPED = frozenset({"/api/switch", "/api/seek", "/api/next", "/api/previous", "/api/device",
                            "/api/audio", "/api/pet", "/api/pause", "/api/resume", "/api/voice/catalog",
                            "/api/voice/search", "/api/voice/cast", "/api/voice/creator"})
COOKIE = "tvs"
# 中转推出的内容要经电视服务缓冲约这么久才在小智屏幕上出现（实测音频领先约 2.7 秒，加上解码起步）。
PLAYBACK_LAG_S = 4.0
# 跳转后多久内，新流还没起来也按跳转目标继续累加。
SEEK_BRIDGE_S = 15.0
# 暂停最长多久（与电视服务的 PAUSE_MAX_S 一致），之后自动继续。
PAUSE_MAX_S = 30 * 60
# 最多每分钟问一次 YouTube 节点的健康状态。
YOUTUBE_HEALTH_S = 60
# CONFIG 包里除频道列表外还有会话号、遥控地址等，加台时给它们留的字节数。
CONFIG_SPARE_BYTES = 512
# 剧集台的名字最多这么多个字符；手机上可以自己起名。
SERIES_NAME_MAX = 20
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
                 accounts=None, devices_dir: Path | None = None):
        self.state = state
        # 多用户：账号库（server/tenancy/accounts.py）和每台设备的文件目录。
        self.accounts = accounts
        self.devices_dir = Path(devices_dir) if devices_dir is not None else None
        # 电脑扫码登录的票据（阶段 2，console.py）。
        self.login_tickets = LoginTickets(accounts) if accounts is not None else None
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
        self.log = log
        self.clock = clock
        self.monotonic = monotonic
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
        # 设备文件上次写的样子（主人、遥控码），变了就重写。
        self._synced: dict[str, tuple] = {}
        # 多用户：给设备加密 Wi-Fi 修改用的 X25519 私钥（wifi.setup_key 在启动时读进来）。
        self.wifi_private: bytes | None = None

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
            groups = json.dumps(state.group_overrides, ensure_ascii=False, indent=1)
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

    def refresh_if_stale(self, cid: str, state: State | None = None) -> None:
        state = state if state is not None else self.state
        if not self._stale(state.get(cid)):
            return
        with self._refresh_lock:
            creator = state.get(cid)
            if not self._stale(creator):
                return
            # 多用户：同一个 UP 主（剧）被好几个人订阅时，列表只向网站要一次（共享缓存）。
            if self.tenant:
                cached = self.accounts.cached_source(creator.site, creator.source, creator.kind)
                if cached is not None and self.clock() - cached["fetched_at"] < KINDS[creator.kind][3]:
                    state.set_videos(cid, [Video(**v) for v in cached["videos"]], cached["fetched_at"])
                    state.save()
                    return
            try:
                client = self._client(creator.site)
                if creator.kind == "series":
                    videos = client.series(creator.source)[1]
                else:
                    videos = client.uploads(creator.source)
            except SourceError as error:
                # 拿不到就继续用缓存的列表。
                self.log(f"{cid} 刷新视频列表失败，用缓存：{error}")
                return
            if videos:
                now = self.clock()
                state.set_videos(cid, videos, now)
                state.save()
                self._cache_videos(creator, videos, now)

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
        self.refresh_if_stale(cid, state)
        for _ in range(MAX_SKIPS):
            pick = state.next_video(cid)
            if pick is None:
                return 503
            video, offset = pick
            try:
                return self._start(video, offset), video, offset
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

    def _start(self, video, offset: float):
        """起播：B 站在本机用 ffmpeg 换封装；YouTube 由 cstone 节点转好码，这里只转发。"""
        if video.site == "youtube":
            return self._client("youtube").open(video.bvid, offset)
        url = self._client("bilibili").media_url(video.bvid)
        return self.starter(self.ffmpeg, url, offset)

    def finish(self, cid: str, video, offset: float, outcome: str, elapsed: float, slot: str = "") -> None:
        dev = self._stream_device(slot) or NO_DEVICE
        state = self._state_of(dev)
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
            return self._start(video, offset), video, offset
        except SourceError as error:
            self.log(f"cast {video.bvid} 取不到：{error}")
            return 503

    def finish_cast(self, video, offset: float, outcome: str, elapsed: float, slot: str = "") -> None:
        dev = self._stream_device(slot)
        cast = self._cast_of(dev or NO_DEVICE)
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

    def _position(self, entry: dict) -> float:
        """小智此刻大约播到的位置。中转推出去的内容要先经电视服务缓冲，约 PLAYBACK_LAG_S 后才在
        屏幕上出现，所以从推流时长里扣掉它；跳转、暂停也按这个位置算，和画面对得上。"""
        return entry["offset"] + max(0.0, self.monotonic() - entry["began"] - PLAYBACK_LAG_S)

    def _current(self, dev: dict):
        """(台, 视频, 当前位置, 正在推的流)。刚跳转、新流还没起来时，用跳转目标顶上。"""
        cid, entry = self._playing_entry(dev)
        if entry is not None:
            return cid, entry["video"], self._position(entry), entry
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
        """cid 正在暂停时返回暂停信息。暂停超过 PAUSE_MAX_S，或设备已换到别的台，就当作结束了。"""
        paused = self._paused.get(dev["slot"])
        if paused is None or paused["cid"] != cid:
            return None
        playing = self._playing(dev)
        if self.clock() - paused["at"] > PAUSE_MAX_S or (playing["online"] and playing["channel"] != cid):
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
        else:
            state = self._state_of(dev)
            state.save_position(cid, video.bvid, stored)
            state.save()

    def pause(self, caller: Caller = LEGACY, device: str | None = None) -> dict:
        dev = self._pick(caller, device)
        cid, video, position, entry = self._current(dev)
        if video is None:
            return {"ok": False, "error": "现在没有在播视频"}
        self._store_position(dev, cid, video, position)
        self._paused[dev["slot"]] = {"cid": cid, "video": video, "position": position, "at": self.clock()}
        self._bridge.pop((dev["slot"], cid), None)
        if entry is not None:
            self._cut(cid, entry, "暂停")
        self._event(dev, "pause", text=video.title)
        return {"ok": True, "at": self.clock(), "position": position}

    def resume(self, caller: Caller = LEGACY, device: str | None = None) -> dict:
        dev = self._pick(caller, device)
        playing = self._playing(dev)
        if self._paused_on(dev, playing["channel"]) is None:
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
        moving = self.monotonic() - entry["began"] >= PLAYBACK_LAG_S
        return {"position": round(self._position(entry), 1), "duration": entry["video"].duration,
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
        return {k: v for k, v in (state.group_overrides if state is not None else {}).items()
                if v in channel_groups.GROUPS}

    def _rows_of(self, state: State, marked: bool = True) -> list[tuple[str, str]]:
        """marked：名字和设备列表里一样带网站标记（算 CONFIG 字节用）；手机网页另有网站标签，不带。"""
        label = table.label if marked else table.plain_label
        with state.lock:
            rows = [(table.CAST_ID, table.CAST_LABEL)]
            rows += [(c.id, label(c)) for c in state.creators]
        if self.main_channels is not None:
            rows += channel_groups.read_table(self.main_channels)
        return rows

    def channel_rows(self, dev: dict | None = None) -> list[tuple[str, str]]:
        """(id, 显示名)：投屏台、UP 主、剧集，再接直播频道表。"""
        return self._rows_of(self._state_of(dev or self.target(LEGACY)))

    def groups(self, dev: dict | None = None) -> list[dict]:
        dev = dev or self.target(LEGACY)
        state = self._state_of(dev)
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
                "device": _phone_device(playing["device"]) if playing["online"] else None}

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
        overrides = self._overrides(state)
        if group is None or group == channel_groups.classify(channel, names[channel]):
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
        groups = self.groups(dev)
        try:
            account = self._account(caller)
        except NotFound:
            account = None
        creators, series, history = [], [], []
        if account is not None:
            with account.lock:
                creators = [{"id": c.id, "name": c.name, "site": c.site,
                             "left": sum(v.bvid not in c.watched for v in c.videos),
                             "total": len(c.videos)} for c in account.creators if c.kind == "up"]
                series = [{"id": c.id, "name": c.name, "site": c.site, "episode": account.episode(c.id),
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
        if self.clients.get("youtube") is not None:
            summary["youtube"] = {"state": self.youtube_state()}
        return summary

    def add_creator(self, text: str, caller: Caller = LEGACY) -> dict:
        state = self._account(caller)
        text = (text or "").strip()
        if not text:
            return {"ok": False, "error": "请先粘贴链接"}
        site = _site_of(text)
        if self.clients.get(site) is None:
            return {"ok": False, "error": "YouTube 还没配置好，暂时只能添加 B 站 UP 主"}
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
        state.save()
        self._cache_videos(creator, creator.videos, creator.fetched_at)
        self._tables_changed(state)
        self.log(f"添加 UP 主 {creator.id} {name}（mid {mid}，{len(videos)} 个投稿）")
        return {"ok": True, "creator": {"id": creator.id, "name": name}}

    def room(self, extra: str = "", caller: Caller = LEGACY) -> dict:
        """设备频道列表还剩多少：字节和台数。extra 是准备加的台名。"""
        return self.room_of(self._account(caller), extra)

    def room_of(self, state: State, extra: str = "") -> dict:
        """同 room()，按一份状态算（语音订阅按设备主人的状态，见 voice.py）。"""
        rows = self._rows_of(state) + ([("sr00", extra)] if extra else [])
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
            return {"ok": False, "error": "YouTube 还没配置好，剧集暂时只能用 B 站"}
        try:
            source = client.series_id(text)
            if source is None:
                return {"ok": False, "error": "看不懂这个链接。请粘贴 B 站番剧页、多集视频（合集）的链接，"
                                              "或 YouTube 播放列表的链接（带 list=）"}
            title, episodes = client.series(source)
        except SourceError as error:
            self.log(f"添加剧集失败：{error}")
            if not error.permanent:
                return {"ok": False, "error": f"{_SITE_NAMES[site]}暂时连不上或拒绝了请求，请稍后再试"}
            # 「只有一集」「都要会员」之类原样告诉用户；接口的错误码换成人话。
            reason = str(error).split(": ", 1)[-1]
            if reason.startswith(("code=", "404")):
                reason = f"{_SITE_NAMES[site].strip()}说这个放不了（可能已删除、私享或需要会员）"
            return {"ok": False, "error": reason}
        name = name or title or source
        if not self.room(table.label_for(name, site), caller)["fits"]:
            return {"ok": False, "error": "小电视的频道列表满了，先删掉几个台再加"}
        try:
            creator = state.add(site, source, name, episodes, self.clock(), kind="series")
        except ValueError as error:
            return {"ok": False, "error": str(error)}
        state.save()
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
            return {"ok": False, "error": "YouTube 还没配置好，暂时只能投 B 站视频"}
        try:
            bvid = self.clients[site].video_id(text)
            if bvid is None:
                return {"ok": False, "error": "看不懂这个链接。请粘贴视频链接（App 里“分享 → 复制链接”）"}
            video = self.clients[site].video(bvid)
        except SourceError as error:
            self.log(f"投屏失败：{error}")
            if error.permanent:
                return {"ok": False, "error": "这个视频放不了（可能已删除、私享或需要会员）"}
            return {"ok": False, "error": f"{_SITE_NAMES[site]}暂时取不到，请稍后再试"}
        return self._cast_video(video, caller, dev)

    def _cast_video(self, video, caller: Caller, dev: dict) -> dict:
        if self.tenant and not dev["id"]:
            return {"ok": False, "error": "还没有绑定小电视"}
        cast = self._cast_of(dev)
        cast.set_cast(video)
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

    def bind_info(self, claim: str, caller: Caller) -> dict:
        if caller.guest or caller.user is None:
            raise NotFound("bind")
        row = self.accounts.device_by_claim(claim)
        if row is None:
            return {"ok": False, "error": "这个绑定码不对，或者设备已经绑定过了"}
        return {"ok": True, "model": row["model"], "tail": row["id"][-4:].upper()}

    def bind(self, claim: str, caller: Caller, ip: str = "") -> dict:
        if caller.guest or caller.user is None:
            raise NotFound("bind")
        try:
            row = self.accounts.bind(claim, caller.user, ip)
        except ValueError as error:
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

        def _cookie_header(self, token: str) -> str:
            return (f"{COOKIE}={token}; HttpOnly; Secure; SameSite=Lax; Path=/tv/; "
                    f"Max-Age={SESSION_S}")

        def _send(self, code: int, body: bytes, kind: str, headers: dict | None = None) -> None:
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            # 网址里可能带着密钥或扫码码，不能经 Referer 带到别处。
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Content-Type-Options", "nosniff")
            for name, value in (headers or {}).items():
                self.send_header(name, value)
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
            if not console.same_origin(self.headers):
                self._not_found()       # 跨站来的（表单、别的网站的脚本）
                return
            try:
                self._post()
            except NotFound:
                self._not_found()

        def do_DELETE(self):
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
                self._send(200, data, kind, {"Cache-Control": "public, max-age=86400"})
                return
            # 电脑版和扫码登录（阶段 2）：页面、票据、登录列表……见 console.py。
            if relay.tenant and console.get(self, relay, path):
                return
            if relay.tenant:
                # 扫设备二维码、绑定码、邀请链接：落地页不用登录，页面里的脚本再带着 cookie 问接口。
                if _JOIN.match(path):
                    self._send(200, JOIN_PAGE.encode(), "text/html; charset=utf-8")
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
                self._send(200, PAGE.encode(), "text/html; charset=utf-8",
                           {"Set-Cookie": self._cookie_header(session)})
                return
            caller = self._caller()
            if caller is None:
                self._not_found()
                return
            device = self._query().get("device") or None
            caller = self._device_scoped(caller, path, device)
            if path == "/":
                self._send(200, PAGE.encode(), "text/html; charset=utf-8")
            elif path == "/api/state":
                self._json(relay.summary(caller, device))
            elif _EPISODES.match(path):
                self._json(relay.episodes(_EPISODES.match(path).group(1), caller, device))
            elif path == "/api/voice/catalog":
                self._json(voice.catalog(relay, device, caller))
            elif path == "/api/voice/search":
                # 语音搜索视频或 UP 主（server/relay/voice.py）。
                query = self._query()
                self._json(voice.search(relay, device, caller, query.get("site"), query.get("q"),
                                        query.get("wait"), query.get("kind") or "video"))
            elif relay.tenant and path == "/api/devices":
                self._json({"ok": True, "devices": relay.my_devices(caller)})
            elif relay.tenant and path == "/api/wifi":
                # 设备保存的 Wi-Fi（只有名字）和最近一次修改的结果：主人才看得到（server/relay/wifi.py）。
                self._json(wifi_api.get(relay, caller, device))
            elif relay.tenant and _BIND_INFO.match(path):
                self._json(relay.bind_info(_BIND_INFO.match(path).group(1), caller))
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
                    self._json({"ok": False, "error": "这台小电视不是你的，不能用你的账号遥控。想当访客遥控它，"
                                                      "请用浏览器的无痕窗口扫码。"})
                    return
                relay.set_target(row["id"], caller)
                self._json({"ok": True, "redirect": f"/tv/?s={token}"})
                return
            if caller is not None and caller.device == row["id"]:
                self._json({"ok": True, "redirect": f"/tv/?s={token}"})
                return
            token = relay.accounts.new_session(None, "guest", device_id=row["id"], user_agent=self._agent(),
                                               ip=self._ip())
            relay.accounts.audit("login.guest", None, self._ip(), "device", row["id"])
            self._json({"ok": True, "redirect": f"/tv/?s={token}"},
                       headers={"Set-Cookie": self._cookie_header(token)})

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
            if relay.tenant and console.post(self, relay, path):
                return
            if relay.tenant and path in ("/api/scan", "/api/invite/accept"):
                body = self._body()
                if body is None:
                    self._json({"ok": False, "error": "请求格式不对"}, 400)
                elif path == "/api/scan":
                    self._scan(body)
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
            if path == "/api/pet":
                self._json(relay.set_pet(body.get("pet"), caller, device))
                return
            if path == "/api/pause":
                self._json(relay.pause(caller, device))
                return
            if path == "/api/resume":
                self._json(relay.resume(caller, device))
                return
            if path in ("/api/voice/cast", "/api/voice/creator"):
                # 语音搜到的视频投屏、UP 主订阅或播最新（server/relay/voice.py）；device 上面已经认过。
                act = voice.cast if path == "/api/voice/cast" else voice.creator
                self._json(act(relay, device, caller, body))
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
    # 声波配网：手机把 Wi-Fi 名称和密码播成声音给配网中的电视听。不用登录，页面不连网。
    "/wifi": ("text/html; charset=utf-8", (_ASSET_DIR / "wifi.html").read_bytes()),
}
ASSETS["/TV/W"] = ASSETS["/wifi"]  # 二维码用的短地址；nginx 也把它转到 /wifi
# 宠物图（设计稿导出的 3 倍图），手机遥控页的「我的宠物」用。
ASSETS.update({f"/pets/{p.name}": ("image/png", p.read_bytes())
               for p in sorted((_ASSET_DIR / "pets").glob("*.png"))})
NOT_FOUND = ("<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
             "<body style='background:#1c1915;color:#e6dcc8;font:16px/1.6 serif;padding:24px'>"
             "<p>打不开。请用小电视状态页上的二维码重新扫一下。</p>")
