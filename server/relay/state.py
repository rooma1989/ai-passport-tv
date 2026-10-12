"""中转的持久状态：关注的 UP 主和剧集、看过的视频、播放进度。

剧集（番剧、多集视频、合集）和 UP 主共用一种台：UP 主的视频新的在前，剧集按集数顺序排，
「下一个没看过的」因此对 UP 主是最新的，对剧集是下一集。

下一个视频只由状态决定，不看请求次数：电视服务每次起播还会另开一个短连接采样调色板，
按请求计数会让每次换台都跳过一个视频。
"""

from __future__ import annotations

import dataclasses
import json
import os
import secrets
import threading
from dataclasses import dataclass, field
from pathlib import Path

# 每个 UP 主只看最近这么多个投稿。
LIST_SIZE = 20
# 一部剧最多留这么多集（蜡笔小新近 900 集）。
SERIES_SIZE = 1000
# 投稿列表的缓存时间；B 站对列表接口限流。
LIST_MAX_AGE_S = 30 * 60
# 剧集的集数表很少变，半天看一次有没有更新。
SERIES_MAX_AGE_S = 12 * 3600
# 续播时往回退几秒，接上中断前的内容。
RESUME_BACK_S = 10
# 频道名会进设备的 CONFIG 包，UP 主数量受包长限制。
MAX_CREATORS = 12
# 剧集台的个数上限；实际多半先碰到设备频道列表的字节上限（见 channel_groups.CONFIG_MAX）。
MAX_SERIES = 40
# 台的种类：id 前缀、个数上限、保留的视频数、列表缓存时间。
KINDS = {"up": ("up", MAX_CREATORS, LIST_SIZE, LIST_MAX_AGE_S),
         "series": ("sr", MAX_SERIES, SERIES_SIZE, SERIES_MAX_AGE_S)}
# 投屏历史条数。
HISTORY_SIZE = 50
# 手机登录一次管多久，以及最多同时记住几台手机。
SESSION_S = 180 * 24 * 3600
MAX_SESSIONS = 50
# 扫码码：20 位 base32（100 位随机），全大写，二维码可用字母数字模式。
CODE_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"
CODE_LENGTH = 20


@dataclass
class Video:
    # B 站是 BV 号，YouTube 是 11 位视频 id；字段名沿用最早只有 B 站时的叫法。
    bvid: str
    title: str
    duration: int
    pubdate: int
    site: str = "bilibili"


@dataclass
class Creator:
    id: str
    site: str
    source: str
    name: str
    videos: list[Video] = field(default_factory=list)
    fetched_at: float = 0.0
    watched: list[str] = field(default_factory=list)
    positions: dict[str, float] = field(default_factory=dict)
    # "up" 或 "series"；旧状态文件里没有这个字段，都是 UP 主。
    kind: str = "up"


def _creator_from(raw: dict) -> Creator:
    raw = dict(raw)
    raw["videos"] = [Video(**v) for v in raw.get("videos", [])]
    return Creator(**raw)


def _cast_from(cast) -> tuple[Video | None, float, bool]:
    if not cast:
        return None, 0.0, False
    return Video(**cast["video"]), float(cast.get("position", 0.0)), bool(cast.get("done", False))


def _cast_record(video: Video | None, position: float, done: bool) -> dict | None:
    if video is None:
        return None
    return {"video": dataclasses.asdict(video), "position": position, "done": done}


class State:
    """所有修改都在 `lock` 内进行；调用者负责在修改后 save()。

    单用户时是一个文件（relay-state.json）。多用户时（server/tenancy）每个用户一个 State，
    从数据库的一行读、存回那一行（store 有 load() 和 save(dict)）；那时投屏在每台设备的
    CastSlot 里，会话和扫码码在账号表里，这里只放这个用户的订阅、进度、投屏历史和分组。
    """

    def __init__(self, path: Path | None = None, store=None):
        self.path = Path(path) if path is not None else None
        self.store = store
        self.lock = threading.RLock()
        self.creators: list[Creator] = []
        # 投屏只留一个视频：新的投屏替换旧的；播完后保留，手动选「我的投屏」可以重看。
        self.cast: Video | None = None
        self.cast_position = 0.0
        self.cast_done = False
        self.history: list[dict] = []
        self.sessions: dict[str, float] = {}
        self.code = ""
        # 多用户时：这个用户调整过的直播台分组（单用户时在 channel-groups.json 里）。
        self.group_overrides: dict[str, str] = {}
        source = str(self.path) if store is None else "用户状态"
        try:
            if store is not None:
                data = store.load()
            elif self.path is not None and self.path.exists():
                data = json.loads(self.path.read_text(encoding="utf-8"))
            else:
                data = None
            if data is not None:
                self.load(data)
        except (ValueError, KeyError, TypeError) as error:
            # 坏文件不自动清空：宁可服务起不来，也不丢掉用户的关注列表。
            raise ValueError(f"{source}: 状态文件损坏：{error}") from None

    def load(self, data: dict) -> None:
        self.creators = [_creator_from(c) for c in data["creators"]]
        self.cast, self.cast_position, self.cast_done = _cast_from(data.get("cast"))
        self.history = [dict(h) for h in data.get("history") or []][:HISTORY_SIZE]
        self.sessions = {str(k): float(v) for k, v in (data.get("sessions") or {}).items()}
        self.code = str(data.get("code") or "")
        self.group_overrides = {str(k): str(v) for k, v in (data.get("group_overrides") or {}).items()}

    def snapshot(self) -> dict:
        """要存的内容。文件里的格式和以前一样；存进数据库的只有这个用户自己的部分。"""
        with self.lock:
            data = {"creators": [dataclasses.asdict(c) for c in self.creators]}
            if self.store is not None:
                data["history"] = [dict(h) for h in self.history]
                data["group_overrides"] = dict(self.group_overrides)
                return data
            cast = _cast_record(self.cast, self.cast_position, self.cast_done)
            if cast is not None:
                data["cast"] = cast
            data["history"] = self.history
            data["sessions"] = self.sessions
            data["code"] = self.code
            return data

    def save(self) -> None:
        with self.lock:
            data = self.snapshot()
            if self.store is not None:
                self.store.save(data)
                return
            text = json.dumps(data, ensure_ascii=False, indent=1)
            tmp = self.path.with_name(self.path.name + ".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o640)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
            os.chmod(tmp, 0o640)
            os.replace(tmp, self.path)

    def get(self, cid: str) -> Creator | None:
        with self.lock:
            return next((c for c in self.creators if c.id == cid), None)

    def add(self, site: str, source: str, name: str, videos: list[Video],
            now: float, kind: str = "up") -> Creator:
        prefix, most, size, _ = KINDS[kind]
        with self.lock:
            if any(c.site == site and c.source == source and c.kind == kind for c in self.creators):
                raise ValueError("这个 UP 主已经添加过了" if kind == "up" else "这部剧已经添加过了")
            used = {c.id for c in self.creators}
            free = [f"{prefix}{n:02d}" for n in range(1, most + 1)
                    if f"{prefix}{n:02d}" not in used]
            if not free:
                raise ValueError(f"最多只能添加 {most} 个 UP 主" if kind == "up"
                                 else f"最多只能添加 {most} 部剧")
            creator = Creator(id=free[0], site=site, source=source, name=name,
                              videos=list(videos[:size]), fetched_at=now, kind=kind)
            self.creators.append(creator)
            # UP 主在前，剧集在后，各自按编号排。
            self.creators.sort(key=lambda c: (c.kind != "up", c.id))
            return creator

    def remove(self, cid: str) -> bool:
        with self.lock:
            before = len(self.creators)
            self.creators = [c for c in self.creators if c.id != cid]
            return len(self.creators) != before

    def set_videos(self, cid: str, videos: list[Video], now: float) -> None:
        with self.lock:
            creator = self.get(cid)
            if creator is None:
                return
            creator.videos = list(videos[:KINDS[creator.kind][2]])
            creator.fetched_at = now
            listed = {v.bvid for v in creator.videos}
            creator.watched = [b for b in creator.watched if b in listed]
            creator.positions = {b: p for b, p in creator.positions.items() if b in listed}

    def next_video(self, cid: str) -> tuple[Video, float] | None:
        """有进度的视频先续播；否则列表里第一个没看过的；都看过就清掉标记从头开始。"""
        with self.lock:
            creator = self.get(cid)
            if creator is None or not creator.videos:
                return None
            for video in creator.videos:
                if video.bvid in creator.positions:
                    return video, max(0.0, creator.positions[video.bvid] - RESUME_BACK_S)
            for video in creator.videos:
                if video.bvid not in creator.watched:
                    return video, 0.0
            creator.watched = []
            return creator.videos[0], 0.0

    def peek_video(self, cid: str) -> Video | None:
        """next_video 下一次会选的视频，只看不改（都看过时不清标记）。给设备「正在加载」用。"""
        with self.lock:
            creator = self.get(cid)
            if creator is None or not creator.videos:
                return None
            for video in creator.videos:
                if video.bvid in creator.positions:
                    return video
            return next((v for v in creator.videos if v.bvid not in creator.watched), creator.videos[0])

    def episode(self, cid: str) -> int:
        """剧集台下一次播第几集（从 1 数），只看不改；都看完了算第 1 集。"""
        with self.lock:
            creator = self.get(cid)
            if creator is None or not creator.videos:
                return 0
            ids = [v.bvid for v in creator.videos]
            for n, bvid in enumerate(ids):
                if bvid in creator.positions:
                    return n + 1
            return next((n + 1 for n, b in enumerate(ids) if b not in creator.watched), 1)

    def play_from(self, cid: str, n: int) -> Video | None:
        """剧集台改从第 n 集（从 1 数）接着播：前面的都算看过，第 n 集和后面的都算没看过。

        第 n 集如果正是看到一半的那一集（下一次本来就播它、且有进度），接着它的进度；
        否则从头播。其他集的进度都清掉，不然 next_video 会先去续播它们。
        不是剧集台、或没有第 n 集时返回 None，什么都不改。
        """
        with self.lock:
            creator = self.get(cid)
            if creator is None or creator.kind != "series" or not 1 <= n <= len(creator.videos):
                return None
            video = creator.videos[n - 1]
            keep = creator.positions.get(video.bvid) if self.episode(cid) == n else None
            creator.watched = [v.bvid for v in creator.videos[:n - 1]]
            creator.positions = {video.bvid: keep} if keep is not None else {}
            return video

    def save_position(self, cid: str, bvid: str, seconds: float) -> None:
        with self.lock:
            creator = self.get(cid)
            if creator is not None:
                creator.positions[bvid] = float(seconds)

    def mark_watched(self, cid: str, bvid: str) -> None:
        with self.lock:
            creator = self.get(cid)
            if creator is None:
                return
            creator.positions.pop(bvid, None)
            if bvid not in creator.watched:
                creator.watched.append(bvid)

    # ---- 投屏 ----

    def set_cast(self, video: Video) -> None:
        with self.lock:
            self.cast = video
            self.cast_position = 0.0
            self.cast_done = False

    def save_cast_position(self, bvid: str, seconds: float) -> None:
        """旧的投屏流在新投屏之后才断开，它的进度不能记到新视频上。"""
        with self.lock:
            if self.cast is not None and self.cast.bvid == bvid:
                self.cast_position = float(seconds)

    def cast_ended(self, bvid: str) -> None:
        with self.lock:
            if self.cast is not None and self.cast.bvid == bvid:
                self.cast_position = 0.0
                self.cast_done = True

    # ---- 投屏历史 ----

    def remember(self, video: Video, at: float) -> None:
        """记一次投屏：新的在前，同一视频只留一条，最多 HISTORY_SIZE 条。"""
        with self.lock:
            entry = {"site": video.site, "id": video.bvid, "title": video.title, "at": at}
            self.history = [h for h in self.history
                            if (h.get("site"), h.get("id")) != (video.site, video.bvid)]
            self.history.insert(0, entry)
            del self.history[HISTORY_SIZE:]

    def forget(self, site: str, video_id: str) -> bool:
        with self.lock:
            before = len(self.history)
            self.history = [h for h in self.history if (h.get("site"), h.get("id")) != (site, video_id)]
            return len(self.history) != before

    def history_entry(self, site: str, video_id: str) -> dict | None:
        with self.lock:
            return next((dict(h) for h in self.history
                         if (h.get("site"), h.get("id")) == (site, video_id)), None)

    # ---- 手机登录 ----

    def new_session(self, now: float) -> str:
        with self.lock:
            token = secrets.token_urlsafe(32)
            self.sessions[token] = now
            # 只留最近的 MAX_SESSIONS 个，最老的先丢。
            for old in sorted(self.sessions, key=self.sessions.get)[:-MAX_SESSIONS]:
                del self.sessions[old]
            return token

    def session_ok(self, token: str | None, now: float) -> bool:
        if not token:
            return False
        with self.lock:
            created = self.sessions.get(token)
            return created is not None and now - created < SESSION_S

    def ensure_code(self) -> str:
        with self.lock:
            if not self.code:
                self.code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
            return self.code

    def new_code(self) -> str:
        """换扫码码：旧码失效，所有已登录的手机都要重新扫。"""
        with self.lock:
            self.code = ""
            self.sessions = {}
            return self.ensure_code()


class CastSlot:
    """多用户时每台设备一个投屏位（两台设备可以各投各的）；和 State 的投屏部分接口一样。

    store 有 load() 和 save(dict)：数据库里这台设备的 device_state，格式是
    {"cast": {"video": …, "position": …, "done": …}}，和 relay-state.json 里的 cast 一样。
    """

    def __init__(self, store):
        self.store = store
        self.lock = threading.RLock()
        data = store.load() or {}
        self.cast, self.cast_position, self.cast_done = _cast_from(data.get("cast"))

    def save(self) -> None:
        with self.lock:
            self.store.save({"cast": _cast_record(self.cast, self.cast_position, self.cast_done)})

    set_cast = State.set_cast
    save_cast_position = State.save_cast_position
    cast_ended = State.cast_ended
