"""设备上「正在播 / 正在加载」画的那一行：哪一集、什么标题。

信息来自视频中转（server/relay/web.py 的 stream_info）：播放流地址 http://127.0.0.1:端口/ch/<台>
把 /ch/ 换成 /info/，回答 {"channel", "title", "episode", "total"}。只发给 HELLO 里带 "info": 1 的
固件：会话开始时放进 CONFIG 的 "playing"，会话中片源要换（下一个、上一集、选集、播完自动接下一集）时
用 SWITCH {"loading": {...}}，新片源的画面来了用 SWITCH {"loading": 0}。见 docs/local-tv-prototype.md。
"""

from __future__ import annotations

import json
import re
import threading
import urllib.error
import urllib.request

# 只在本机回环上问，答得慢就当没有：会话开始时这一问挡在 CONFIG 前面（设备只等约 2 秒）。
TIMEOUT_S = 0.5
# 标题在设备上最多 63 字节（UTF-8），按字截短，不切坏半个汉字。
TITLE_MAX_BYTES = 60
EPISODE_MAX = 100000

_RELAY_STREAM = re.compile(
    r"^(http://(?:127\.0\.0\.1|localhost)(?::\d{1,5})?)/ch/"
    r"((?:[0-9a-f]{12}/|legacy-[a-z0-9]{1,20}/)?(?:up\d\d|sr\d\d|cast))$")


def info_url(stream_url: str | None) -> str | None:
    """中转播放流的地址换成问信息的地址；不是中转的流（电视台直播）为 None。"""
    match = _RELAY_STREAM.match(stream_url or "")
    return f"{match.group(1)}/info/{match.group(2)}" if match else None


def clip_utf8(text: str, max_bytes: int = TITLE_MAX_BYTES) -> str:
    out, used = [], 0
    for char in text:
        size = len(char.encode("utf-8"))
        if used + size > max_bytes:
            break
        out.append(char)
        used += size
    return "".join(out)


def _count(value) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= EPISODE_MAX:
        return 0
    return value


def payload(title, episode=0, total=0) -> dict | None:
    """发给设备的 {"title", "episode", "total"}；没有集数时只有标题，什么都没有时为 None。"""
    title = clip_utf8(" ".join(str(title or "").split()))
    episode, total = _count(episode), _count(total)
    if total and episode > total:
        total = 0
    if not title and not episode:
        return None
    out: dict = {"title": title}
    if episode:
        out["episode"] = episode
        if total:
            out["total"] = total
    return out


def from_relay(answer) -> dict | None:
    if not isinstance(answer, dict):
        return None
    return payload(answer.get("title"), answer.get("episode", 0), answer.get("total", 0))


def fetch(url: str | None, timeout: float = TIMEOUT_S, opener=urllib.request.urlopen) -> dict | None:
    """问中转；出错、超时、404 都是 None（设备照旧只画频道）。"""
    if not url:
        return None
    try:
        with opener(url, timeout=timeout) as answer:
            return from_relay(json.loads(answer.read(4096).decode("utf-8")))
    except (OSError, ValueError, urllib.error.URLError):
        return None


class Fetch:
    """在后台线程里问，发送循环不等它（片源重开时用）。"""

    def __init__(self, url: str, opener=urllib.request.urlopen):
        self.result: dict | None = None
        self._thread = threading.Thread(target=self._run, args=(url, opener), daemon=True)
        self._thread.start()

    def _run(self, url: str, opener) -> None:
        self.result = fetch(url, opener=opener)

    @property
    def done(self) -> bool:
        return not self._thread.is_alive()
