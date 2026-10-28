"""B 站的视频在电脑上点播时起播快一点：取地址的预取和缓存、每个视频走哪条路的记忆（vod.py、bilidash.py 的前一步）。

为什么：在电脑上点一个 B 站视频，中转要先问 B 站要地址（视频信息拿 cid、再 playurl，两个来回），很长的视频还要
再看一眼 MP4 的文件头（vod.probe_moov）决定放 MP4 还是 DASH，然后浏览器才开始取视频——三个先后的请求，每个
都要等 B 站一个来回（经代理时更久）。这里省掉前两步：

- 预取（POST /api/bili/prefetch）：电脑版上鼠标在一个 B 站视频的卡片上停住（≥ 300 毫秒）、或焦点到了它，
  页面把 BV 号发过来；这里排队、一次一个、每分钟至多 PREFETCH_PER_MINUTE 个（ytvod.Prefetcher，和 YouTube
  的预取同一套），按这个人的 B 站登录把视频信息和 playurl 先问好（Plays.prefetch）。只取地址，从不取视频的
  字节（连看 moov 的那 64 个字节也不取）。有起播正在取地址时预取先停着。
- 地址缓存（Plays）：预取到的结果按 (哪种, 视频, 谁的登录) 留到地址里的 deadline 前 EXPIRE_MARGIN_S（没写时
  NO_DEADLINE_S，最多 MAX_AGE_S），LRU 至多 PLAYS_MAX 个。起播（web.py 的 _vod_source）先看这里；同一个视频
  正在预取就等它（至多 FLIGHT_WAIT_S）。起播自己取到的不放进来（下一次照旧问 B 站，地址总是新的）；地址过期了
  重新取时（VodSource.resolve）不看缓存、并把它忘掉。
- 走哪条路（Plays.moov）：看过一次 moov 的视频记下它有多大（PATHS_MAX 个、PATH_KEEP_S），第二次打开时不再看：
  moov 太大的直接取 DASH（连 html5 的 MP4 地址都不用取），不大的直接放 MP4。

地址里有签名：从不写进日志。只用标准库。
"""

from __future__ import annotations

import collections
import re
import threading
import time
import urllib.parse
from typing import Callable

from . import vod

# 地址的结果留几个、留到地址过期前多久、最多留多久；地址里没写过期时间时留多久。
PLAYS_MAX = 64
EXPIRE_MARGIN_S = 30 * 60
MAX_AGE_S = vod.URL_REFRESH_S
NO_DEADLINE_S = 10 * 60
# 起播碰上正在预取的同一个视频，最多等它多久（B 站的 playurl 平常一两百毫秒）。
FLIGHT_WAIT_S = 10.0
# 记住多少个视频的 moov 大小、记多久（同一个视频的文件不会变，只是别无限长）。
PATHS_MAX = 512
PATH_KEEP_S = 24 * 3600
# 预取：一次请求至多几个、每分钟至多几个、排队至多几个。
PREFETCH_IDS_MAX = 6
PREFETCH_PER_MINUTE = 12
PREFETCH_QUEUE = 8
# 认的视频：BV 号，或者「BV 号/cid」「ep<集 id>/cid」（web.py 的 Video.bvid 就是这几种）。
ID = re.compile(r"^(?:BV[0-9A-Za-z]{10}(?:/\d{1,12})?|ep\d{1,12}/\d{1,12})$")
_DEADLINE = re.compile(r"[?&]deadline=(\d{9,11})(?:\D|$)")


def who(login) -> tuple | None:
    """缓存按谁的登录分开（bililogin.Login 的用户、是不是大会员）；不登录是 None。"""
    return None if login is None else (getattr(login, "user", None), bool(getattr(login, "vip", False)))


def deadline_of(info: dict) -> int:
    """vod_media / vod_dash 的结果里地址的过期时间（B 站地址里的 deadline，Unix 秒）；没有时 0。"""
    url = str(info.get("url") or (info.get("video") or {}).get("url") or "") if isinstance(info, dict) else ""
    query = urllib.parse.urlsplit(url).query
    match = _DEADLINE.search("?" + query) if query else None
    return int(match.group(1)) if match else 0


def keep_until(info: dict, now: float) -> float:
    deadline = deadline_of(info)
    if not deadline:
        return now + NO_DEADLINE_S
    return min(now + MAX_AGE_S, deadline - EXPIRE_MARGIN_S)


class _Flight:
    def __init__(self):
        self.done = threading.Event()
        self.result: dict | None = None


class Plays:
    """地址缓存和走哪条路的记忆（见模块说明）。clock 是墙上时间（和地址里的 deadline 比）。"""

    def __init__(self, clock=time.time, log=print, size: int = PLAYS_MAX):
        self.clock = clock
        self.log = log
        self.size = size
        self._cache: collections.OrderedDict[tuple, tuple[float, float, dict]] = collections.OrderedDict()
        self._flights: dict[tuple, _Flight] = {}
        self._paths: collections.OrderedDict[str, tuple[float, int]] = collections.OrderedDict()
        self._plays = 0
        self._lock = threading.Lock()
        # 起播用上了几次预取的结果（给日志、测试和量速度看）。
        self.hits = 0

    def busy(self) -> bool:
        """有起播正在取地址：预取先停着。"""
        with self._lock:
            return self._plays > 0

    def cached(self, key: tuple) -> tuple[dict, float] | None:
        """(结果, 取到多少秒了)；没有或快过期了 None。key：(哪种 "mp4" / "dash", 视频, who())。"""
        now = self.clock()
        with self._lock:
            hit = self._cache.get(key)
            if hit is None:
                return None
            if now >= hit[0]:
                del self._cache[key]
                return None
            self._cache.move_to_end(key)
            return hit[2], max(0.0, now - hit[1])

    def forget(self, key: tuple) -> None:
        with self._lock:
            self._cache.pop(key, None)

    def get(self, key: tuple, fetch: Callable[[], dict]) -> tuple[dict, float]:
        """起播：预取过的直接用（同一个正在预取就等它），否则 fetch()（出错照样抛）。返回 (结果, 取到多少秒了)。"""
        hit = self.cached(key)
        if hit is not None:
            with self._lock:
                self.hits += 1
            return hit
        with self._lock:
            self._plays += 1
            flight = self._flights.get(key)
        try:
            if flight is not None:
                flight.done.wait(FLIGHT_WAIT_S)
                if flight.result is not None:
                    with self._lock:
                        self.hits += 1
                    return flight.result, 0.0
            return fetch(), 0.0
        finally:
            with self._lock:
                self._plays -= 1

    def prefetch(self, key: tuple, fetch: Callable[[], dict]) -> bool:
        """预取一个：已经有了、正在取时不做；取到了留着（返回 True）。fetch 出错照样抛（Prefetcher 记一行日志）。"""
        if self.cached(key) is not None:
            return False
        with self._lock:
            if key in self._flights:
                return False
            flight = self._flights[key] = _Flight()
        try:
            info = fetch()
            if not info:
                return False
            now = self.clock()
            with self._lock:
                self._cache[key] = (keep_until(info, now), now, info)
                self._cache.move_to_end(key)
                while len(self._cache) > self.size:
                    self._cache.popitem(last=False)
            flight.result = info
            return True
        finally:
            with self._lock:
                if self._flights.get(key) is flight:
                    del self._flights[key]
            flight.done.set()

    # ---- 走哪条路 ----

    def moov(self, vid: str) -> int | None:
        """记下的这个视频 html5 MP4 的 moov 有多大（字节）；没看过（或记得太久了）是 None。"""
        now = self.clock()
        with self._lock:
            hit = self._paths.get(vid)
            if hit is None:
                return None
            if now - hit[0] > PATH_KEEP_S:
                del self._paths[vid]
                return None
            self._paths.move_to_end(vid)
            return hit[1]

    def remember_moov(self, vid: str, size: int) -> None:
        if size <= 0:
            return                      # 没看出来：下次再看
        with self._lock:
            self._paths[vid] = (self.clock(), int(size))
            self._paths.move_to_end(vid)
            while len(self._paths) > PATHS_MAX:
                self._paths.popitem(last=False)
