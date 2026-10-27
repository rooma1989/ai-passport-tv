"""在电脑上点播很长的 B 站视频：放 DASH（两个分片 MP4），不放 html5 的那一个 MP4。

为什么：vod.py 平常转发 html5 平台的 MP4（H.264 + AAC，最高 720p），浏览器 <video> 直接放。可那个 MP4 的索引（moov）
在文件头，和视频一样长：一个九个多小时的分 P（「网盘分享」那种合集，每 P 9:45:05）光 moov 就有 43.8 MB，浏览器下完它
才出画面——直连约 5 秒，经代理（1 MB/s）四十多秒，看着就是「正在打开…」一直不动；换下一 P 又是一样。
B 站网页自己的播放器放的是 DASH：画面、声音各一个分片 MP4，文件头只有不到 1 KB 的初始化段和几十 KB 的 sidx（每一段
几秒、多少字节），按时间算出字节范围用 Range 取。YouTube 的点播（ytvod.py）已经这样放了，这里用同一套：

- web.py 的 _vod_source 先照旧取 html5 的 MP4；视频不短于 vod.MOOV_CHECK_S、文件头里的 moov 超过 vod.MOOV_MAX
  （vod.probe_moov，一个 64 字节的请求）、页面能用 MediaSource 时，改取 DASH（bilibili.Bilibili.vod_dash：
  H.264 ≤ 720P、AAC；不登录时 B 站最高给 480P），给页面的 mode 是 "dash"，页面用 DashPlayer 放（和 YouTube 一样）。
  取不到 DASH、没有 H.264 时照旧放 MP4（慢，但放得了）。
- 清单（…/manifest）、索引缓存是 ytvod.DashIndexes（和 YouTube 共用）；字节照旧经 vod.py 按 Range 转发
  （…/v、…/a），带 B 站要的 Referer 和 User-Agent。
- 上游（ytvod.Fetcher）连接留着复用：DASH 一段一个请求，不用每次都握手。B 站的地址不经 YouTube 的代理；系统
  （环境变量）配了代理时和 urllib 一样经它，NO_PROXY 里写了的直连。
- 地址约两小时过期：过期（403 / 404 / 410）时 vod.py 重新取一次（resolve），换的是同一对格式。

地址里有签名：从不写进日志。只用标准库。
"""

from __future__ import annotations

import threading
import urllib.request
from typing import Callable

from . import vod, ytvod
from .ytnode import parse_proxy

# 认「是不是同一个上游」用的 B 站视频主机（问系统代理设置时用它）。
CDN_HOST = "upos-sz-mirrorcos.bilivideo.com"


def system_proxy(host: str = CDN_HOST, getproxies=urllib.request.getproxies, bypass=urllib.request.proxy_bypass) -> str:
    """和 urllib 一样看环境变量里的代理（https_proxy；NO_PROXY 里有的直连）。看不懂的（不是 http / socks5）当没有。"""
    try:
        proxy = (getproxies() or {}).get("https") or ""
        if not proxy or bypass(host):
            return ""
        parse_proxy(proxy)
        return proxy
    except (ValueError, OSError):
        return ""


def track_of(t: dict, kind: str) -> vod.Track:
    """bilibili.dash_info 的一条 → vod.Track（itag 是「清晰度编号:编码」，索引按它和分 P 缓存）。"""
    mime = f'{"video" if kind == "v" else "audio"}/mp4; codecs="{t["codecs"]}"'
    return vod.Track(url=t["url"], mime=mime, itag=f"{t['id']}:{t['codecs']}", init=t["init"], index=t["index"],
                     height=int(t.get("height") or 0), bitrate=int(t.get("bandwidth") or 0))


def media_of(info: dict) -> vod.Media:
    return vod.Media(url="", duration=float(info.get("duration") or 0),
                     tracks={"v": track_of(info["video"], "v"), "a": track_of(info["audio"], "a")})


def label(info: dict) -> str:
    """给人看的清晰度：画面的行数（"480p"、"720p"）。"""
    height = int(info["video"].get("height") or 0)
    return f"{height}p" if height else "720p"


class BiliDash(ytvod.DashIndexes):
    """web.py 用的这一套：索引缓存和到 B 站视频主机的连接。fetcher 可换（测试用）。"""

    def __init__(self, log=print, fetcher=ytvod.Fetcher, proxy: Callable[[], str] = system_proxy):
        self.log = log
        self.indexes = ytvod._Lru(ytvod.INDEX_CACHE_MAX)
        self._make_fetcher = fetcher
        self._proxy = proxy
        self._fetchers: dict[str, ytvod.Fetcher] = {}
        self._lock = threading.Lock()

    def fetcher(self) -> ytvod.Fetcher:
        proxy = self._proxy() or ""
        with self._lock:
            found = self._fetchers.get(proxy)
            if found is None:
                for old in self._fetchers.values():
                    old.close()
                self._fetchers = {proxy: self._make_fetcher(proxy)}
                found = self._fetchers[proxy]
            return found

    def source(self, key: tuple, video, position: float, info: dict, fetch: Callable[[], dict],
               headers: tuple, hd: bool = False) -> vod.VodSource:
        """这个视频按 DASH 放的片源（vod.VodSource，dash 为真）。info：刚取到的 dash_info；fetch()：重新取一次
        （地址过期时）；hd：这个人还能选 1080P（登录了 B 站，走 HLS）。"""
        first = media_of(info)

        def resolve() -> vod.Media:
            again = fetch()
            if not again:
                raise vod.NotPlayable("没有 DASH 了")
            return media_of(again)

        return vod.VodSource(key, resolve=resolve, headers=headers, label=video.title, site="bilibili",
                             start=position, duration=float(video.duration or info.get("duration") or 0),
                             first=first, quality=label(info), hd=hd, opener=self.fetcher(), dash=True,
                             manifest=self.manifest, variant="dash")
