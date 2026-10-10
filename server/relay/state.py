"""中转的持久状态：关注的 UP 主、看过的视频、播放进度。

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
# 投稿列表的缓存时间；B 站对列表接口限流。
LIST_MAX_AGE_S = 30 * 60
# 续播时往回退几秒，接上中断前的内容。
RESUME_BACK_S = 10
# 频道名会进设备的 CONFIG 包，UP 主数量受包长限制。
MAX_CREATORS = 12
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


def _creator_from(raw: dict) -> Creator:
    raw = dict(raw)
    raw["videos"] = [Video(**v) for v in raw.get("videos", [])]
    return Creator(**raw)


class State:
    """所有修改都在 `lock` 内进行；调用者负责在修改后 save()。"""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.lock = threading.RLock()
        self.creators: list[Creator] = []
        # 投屏只留一个视频：新的投屏替换旧的；播完后保留，手动选「我的投屏」可以重看。
        self.cast: Video | None = None
        self.cast_position = 0.0
        self.cast_done = False
        self.history: list[dict] = []
        self.sessions: dict[str, float] = {}
        self.code = ""
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                self.creators = [_creator_from(c) for c in data["creators"]]
                cast = data.get("cast")
                if cast:
                    self.cast = Video(**cast["video"])
                    self.cast_position = float(cast.get("position", 0.0))
                    self.cast_done = bool(cast.get("done", False))
                self.history = [dict(h) for h in data.get("history") or []][:HISTORY_SIZE]
                self.sessions = {str(k): float(v) for k, v in (data.get("sessions") or {}).items()}
                self.code = str(data.get("code") or "")
            except (ValueError, KeyError, TypeError) as error:
                # 坏文件不自动清空：宁可服务起不来，也不丢掉用户的关注列表。
                raise ValueError(f"{self.path}: 状态文件损坏：{error}") from None

    def save(self) -> None:
        with self.lock:
            data = {"creators": [dataclasses.asdict(c) for c in self.creators]}
            if self.cast is not None:
                data["cast"] = {"video": dataclasses.asdict(self.cast),
                                "position": self.cast_position, "done": self.cast_done}
            data["history"] = self.history
            data["sessions"] = self.sessions
            data["code"] = self.code
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
            now: float) -> Creator:
        with self.lock:
            if any(c.site == site and c.source == source for c in self.creators):
                raise ValueError("这个 UP 主已经添加过了")
            used = {c.id for c in self.creators}
            free = [f"up{n:02d}" for n in range(1, MAX_CREATORS + 1) if f"up{n:02d}" not in used]
            if not free:
                raise ValueError(f"最多只能添加 {MAX_CREATORS} 个 UP 主")
            creator = Creator(id=free[0], site=site, source=source, name=name,
                              videos=list(videos[:LIST_SIZE]), fetched_at=now)
            self.creators.append(creator)
            self.creators.sort(key=lambda c: c.id)
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
            creator.videos = list(videos[:LIST_SIZE])
            creator.fetched_at = now
            listed = {v.bvid for v in creator.videos}
            creator.watched = [b for b in creator.watched if b in listed]
            creator.positions = {b: p for b, p in creator.positions.items() if b in listed}

    def next_video(self, cid: str) -> tuple[Video, float] | None:
        """有进度的视频先续播；否则最新的未看视频；都看过就清掉标记从最新的开始。"""
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
