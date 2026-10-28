"""直播源起播的共用部分：电脑上看（server/relay/watch.py）和小电视（server/live.py）都用这一份。

慢的是连片源：跳转、主列表、子列表、第一段，每一步约 1 秒。所以：
- 探测只读一点（LIVE_PROBE）：直播源的头里就有编码和帧率，默认的 5 秒 / 5 MB 只是白等。
- 记住跳转以后真正的列表地址（LiveMemory，RESOLVED_S 秒内有效）：下次 ffmpeg 直接从它起。地址可以从 ffmpeg
  的输出里认出来（resolved_playlist），也可以先自己走一遍跳转和主列表（resolve_playlist，几个小请求，不起 ffmpeg）。
- 多路清晰度的主列表只开一路（pick_variant）：ffmpeg 打开主列表时每一路的子列表和第一段都要取一遍、探测完才出
  画面，用到的只有一路。小电视挑不比屏幕小的最小一路（AT_LEAST 给屏幕尺寸）；电脑挑码率不超过 PC_MAX_BPS 的
  最高一路。挑的结果就是记住的那个地址（按线路、按小电视 / 电脑分开记）；打不开时调用方忘掉它、从频道表里的
  地址（主列表）重来。不写进频道表旁边的 meta：子列表的地址带会话参数，过一阵就失效，挑哪一路每次按主列表现挑，
  不多花一次请求。

只用标准库。地址里带会话参数：不写日志。
"""

from __future__ import annotations

import collections
import re
import threading
import time
import urllib.parse
import urllib.request

# 直播源（MPEG-TS、HLS）读多少就开始转：默认 5 秒 / 5 MB，直播源的头里就有编码，1 秒、1 MB 够了。
# 量过（CCTV1，咪咕 6 秒一段）：慢的是连片源，探测只占零点几秒。
LIVE_PROBE = ("-probesize", "1000000", "-analyzeduration", "1000000")
# 跳转以后真正的列表地址记多久：咪咕这类源先经两三次跳转、再取主列表，才到真正的列表，每一步约 1 秒；
# 记住了下次直接从它起。地址里带会话参数，量过 9 分钟以后还能用；记的时间短一点，失效了（打不开、一段也没出）
# 就忘掉、从频道表里的地址重来。只在内存里，从不写日志。
RESOLVED_S = 600.0
# 记住多少个台（转法、地址各这么多）。
MEMORY_MAX = 256
# 自己走跳转和主列表时：至多等这么久、至多读这么多。
RESOLVE_TIMEOUT_S = 8.0
PLAYLIST_BYTES = 256 * 1024
# 电脑上挑清晰度：码率不超过这么多的最高一路（只换封装，码率就是要传给浏览器的；转码的再压到 720p）。
PC_MAX_BPS = 8_000_000
# 电脑上挑清晰度：高不超过这么多的（4K 的一路只换封装也是十几 MB 一段，浏览器、内存都吃力）。
PC_MAX_HEIGHT = 1080
_OPENING = re.compile(r"Opening '(https?://[^']+)' for reading")
_ATTR = re.compile(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)')
# 只有声音的编码（CODECS 里只有这些的一路没有画面）。
_AUDIO_CODECS = ("mp4a", "ac-3", "ec-3", "opus", "mp3", "flac", "alac")


class LiveMemory:
    """每个直播台（键由调用方定：电脑上看是 Source.key，小电视是 ("tv", 地址, UA)）记住的两样，让下次起得快：
    - 转法（电脑上看的 Mode，这里不管是什么）：要转码的台直接按转码起，不用先按只换封装起、看到编码再重起。
    - 跳转以后真正的列表地址（resolved_s 秒内有效）：ffmpeg 不用再走一遍跳转和主列表、不再开每一路清晰度。"""

    def __init__(self, clock=time.monotonic, resolved_s: float = RESOLVED_S):
        self.clock = clock
        self.resolved_s = resolved_s
        self._modes: collections.OrderedDict[tuple, object] = collections.OrderedDict()
        self._urls: dict[tuple, tuple[str, float]] = {}
        self._lock = threading.Lock()

    def mode(self, key: tuple):
        with self._lock:
            return self._modes.get(key)

    def remember_mode(self, key: tuple, mode) -> None:
        with self._lock:
            self._modes[key] = mode
            self._modes.move_to_end(key)
            while len(self._modes) > MEMORY_MAX:
                self._modes.popitem(last=False)

    def url(self, key: tuple) -> str | None:
        with self._lock:
            found = self._urls.get(key)
            if found is None:
                return None
            if self.clock() - found[1] > self.resolved_s:
                del self._urls[key]
                return None
            return found[0]

    def remember_url(self, key: tuple, url: str) -> None:
        with self._lock:
            self._urls[key] = (url, self.clock())
            while len(self._urls) > MEMORY_MAX:
                del self._urls[min(self._urls, key=lambda k: self._urls[k][1])]

    def forget_url(self, key: tuple) -> None:
        with self._lock:
            self._urls.pop(key, None)

    def age(self, key: tuple) -> float | None:
        """记住的列表地址记了多少秒；没有（或已过期）时 None。"""
        with self._lock:
            found = self._urls.get(key)
        if found is None:
            return None
        age = self.clock() - found[1]
        return age if age <= self.resolved_s else None


def resolved_playlist(line: str, given: str) -> str | None:
    """ffmpeg 的一行输出里打开的列表地址（跳转和主列表之后真正的那个 .m3u8）；不是、或者就是频道表里的地址时 None。"""
    match = _OPENING.search(line)
    if match is None:
        return None
    url = match.group(1)
    path = url.split("?", 1)[0].split("#", 1)[0]
    return url if path.endswith(".m3u8") and url != given else None


def variants(text: str) -> list[dict]:
    """主列表里的每一路：{uri, bandwidth, width, height, video（False：CODECS 说只有声音）, audio（声音组名）}，
    列表里的顺序；没写的数字为 0。"""
    lines = text.splitlines()
    found = []
    for i, line in enumerate(lines[:-1]):
        if not line.startswith("#EXT-X-STREAM-INF"):
            continue
        uri = lines[i + 1].strip()
        if not uri or uri.startswith("#"):
            continue
        attrs = {k: v.strip('"') for k, v in _ATTR.findall(line.partition(":")[2])}
        width = height = 0
        size = re.fullmatch(r"(\d+)x(\d+)", attrs.get("RESOLUTION", ""))
        if size:
            width, height = int(size.group(1)), int(size.group(2))
        try:
            bandwidth = int(attrs.get("BANDWIDTH", "0"))
        except ValueError:
            bandwidth = 0
        codecs = [c.strip().lower() for c in attrs.get("CODECS", "").split(",") if c.strip()]
        video = not codecs or any(not c.startswith(_AUDIO_CODECS) for c in codecs)
        found.append({"uri": uri, "bandwidth": bandwidth, "width": width, "height": height,
                      "video": video, "audio": attrs.get("AUDIO", "")})
    return found


def pick_variant(choices: list[dict], at_least: tuple[int, int] | None = None,
                 max_bps: int = PC_MAX_BPS, max_height: int = PC_MAX_HEIGHT) -> dict | None:
    """挑一路。at_least（小电视的屏幕宽、高）：缩放到屏幕上不用放大的最小一路（宽或高至少和屏幕一样），都比屏幕小
    时挑最大的；没写分辨率的主列表不挑（None，照旧开主列表）。没有 at_least（电脑）：码率不超过 max_bps、高不超过
    max_height 的最高一路，都超了挑码率最低的。只有声音的一路不挑；只有一路时就是它。"""
    usable = [v for v in choices if v["video"]]
    if len(usable) <= 1:
        # 只有一路（电台的主列表常常只有一路声音）时就是它。
        return usable[0] if usable else (choices[0] if len(choices) == 1 else None)
    if at_least is not None:
        sized = [v for v in usable if v["width"] and v["height"]]
        if not sized:
            return None
        width, height = at_least
        area = lambda v: (v["width"] * v["height"], v["bandwidth"])  # noqa: E731
        covers = [v for v in sized if v["width"] >= width or v["height"] >= height]
        return min(covers, key=area) if covers else max(sized, key=area)
    fitting = [v for v in usable if v["bandwidth"] <= max_bps and v["height"] <= max_height]
    if fitting:
        return max(fitting, key=lambda v: (v["bandwidth"], v["height"]))
    return min(usable, key=lambda v: (v["bandwidth"], v["height"]))


def resolve_playlist(url: str, user_agent: str = "", timeout: float = RESOLVE_TIMEOUT_S,
                     urlopen=urllib.request.urlopen, at_least: tuple[int, int] | None = None) -> str | None:
    """像 ffmpeg 那样打开一个直播地址：跟着跳转；是主列表时按 pick_variant 挑一路（at_least 见那里）。返回要给
    ffmpeg 的那个列表地址（和 resolved_playlist 一样：.m3u8 结尾、和给的不同）；挑不了（声音在单独的列表里、
    没写分辨率而要按屏幕挑）、不是 HLS、打不开时 None。地址里带会话参数：不写日志。"""
    if not url.startswith(("http://", "https://")):
        return None
    # 频道表里偶尔有中文路径：ffmpeg 原样发，urllib 要先转义。
    asked = urllib.parse.quote(url, safe=":/?#[]@!$&'()*+,;=%~")
    # 没写 User-Agent 的台：和 ffmpeg 一样报 Lavf（ffmpeg 只在台写了时才加 -user_agent）。
    request = urllib.request.Request(asked, headers={"User-Agent": user_agent or "Lavf/61"})
    try:
        with urlopen(request, timeout=timeout) as response:
            final = response.geturl()
            text = response.read(PLAYLIST_BYTES).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001 —— 解析不了就算了，ffmpeg 照常从频道表里的地址起
        return None
    if "#EXTM3U" not in text:
        return None
    if "#EXT-X-STREAM-INF" in text:
        chosen = pick_variant(variants(text), at_least)
        if chosen is None or _separate_audio(text, chosen["audio"]):
            return None        # 单独的声音列表：只开子列表就没有声音，要 ffmpeg 开主列表
        found = urllib.parse.urljoin(final, chosen["uri"])
    elif "#EXTINF" in text:
        found = final
    else:
        return None
    path = found.split("?", 1)[0].split("#", 1)[0]
    return found if path.endswith(".m3u8") and found not in (url, asked) else None


def _separate_audio(text: str, group: str) -> bool:
    """这一路的声音在单独的列表里（#EXT-X-MEDIA TYPE=AUDIO 带 URI；这一路没写 AUDIO 组时任何一个都算）。"""
    for line in text.splitlines():
        if not line.startswith("#EXT-X-MEDIA:"):
            continue
        attrs = {k: v.strip('"') for k, v in _ATTR.findall(line.partition(":")[2])}
        if attrs.get("TYPE") == "AUDIO" and attrs.get("URI") and (not group or attrs.get("GROUP-ID") == group):
            return True
    return False
