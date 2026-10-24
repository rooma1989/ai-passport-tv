"""总后台频道表的「导入播放列表」和「试一下能不能放」（自建模式，admin.py 转过来）。

设计见 docs/superpowers/specs/2026-10-15-self-hosting-design.md 第 8.2 节：自建的镜像不带任何频道，
管理员粘贴自己的 M3U 地址或上传 M3U 文件，页面列出里面的台、勾选、试播，再加进频道表（照常经
admin.save_channels 检查和保存）。地址只在请求时取一次，不存。只认 http/https。

页面还列出几个网上公开的第三方播放列表（presets.py，只有列表地址，没有直播流地址），点一下由
服务器读那个地址，和粘贴地址一样。读出来的台带上自动分组（kind，channel_groups.classify：
页面的「央视 / 卫视」快速筛选用）和重复标记（dup：地址或名字已经在频道表里、或者列表里前面
已经有同名的台），重复的默认不勾。

能加地址的只有这一家的管理员，服务器替他去取一个地址（包括局域网里的）是他自己的事
（多租户设计的 Q5 那个顾虑在自建时不成立）；云端的中转没有这两个接口。
"""

from __future__ import annotations

import re
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from .. import channel_groups, m3u
from . import channeltable, presets

# 上传的 M3U 内容、或取回来的播放列表，最多这么大。
IMPORT_MAX = 2 * 1024 * 1024
# 请求体（JSON 里的文件内容，换行等会转义）最多这么大。
IMPORT_BODY_MAX = 3 * 1024 * 1024
# 连接和每次读的超时；整份最多读这么久（服务器一点一点地吐也不会拖住一个线程太久）。
FETCH_TIMEOUT_S = 20
FETCH_TOTAL_S = 45
_CHUNK = 64 * 1024
URL_FIELD_MAX = 2000
# 一次最多列出这么多个台（设备本身最多 128 个台，列太多页面也勾不过来）。
ENTRIES_MAX = 2000
# 试播：取一帧画面。SLOW 秒内出画面算「能放」，DEAD 秒还没有算「放不了」，中间算「慢」。
PROBE_SLOW_S = 15
PROBE_DEAD_S = 30
DEFAULT_USER_AGENT = "AptvPlayer-UA"
# 同时最多试这么多个（每个都是一个 ffmpeg）。
_probes = threading.BoundedSemaphore(2)


def _fetch(url: str, opener=urllib.request.urlopen, monotonic=time.monotonic) -> tuple[str | None, str]:
    """(播放列表文字, 错误)。"""
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or len(url) > URL_FIELD_MAX:
        return None, "地址要以 http:// 或 https:// 开头"
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "xiaozhi-tv-import/1"})
        deadline = monotonic() + FETCH_TOTAL_S
        raw = b""
        with opener(request, timeout=FETCH_TIMEOUT_S) as response:
            while len(raw) <= IMPORT_MAX:
                chunk = response.read(min(_CHUNK, IMPORT_MAX + 1 - len(raw)))
                if not chunk:
                    break
                raw += chunk
                if monotonic() > deadline:
                    return None, f"这个地址太慢（{FETCH_TOTAL_S} 秒没读完）"
    except urllib.error.HTTPError as error:
        return None, f"取不到这个地址（HTTP {error.code}）"
    except urllib.error.URLError as error:
        return None, f"取不到这个地址：{error.reason}"
    except (OSError, ValueError) as error:
        return None, f"取不到这个地址（{type(error).__name__}）"
    if len(raw) > IMPORT_MAX:
        return None, "播放列表太大（超过 2 MB）"
    return raw.decode("utf-8", errors="replace"), ""


def _clean(entry: dict) -> dict | None:
    """按频道表的规矩整理一条（channeltable.problems 的那些）；地址不合格的丢掉。"""
    name = " ".join(entry["name"].replace("|", " ").split())
    name = "".join(c for c in name if ord(c) >= 32)[:channeltable.NAME_MAX].strip()
    url = entry["url"].strip()
    agent = entry["agent"].strip()
    if not url.startswith(("http://", "https://")) or len(url) > channeltable.URL_MAX or \
            any(c in url for c in " |") or any(ord(c) < 32 for c in url):
        return None
    if len(agent) > channeltable.AGENT_MAX or "|" in agent or any(ord(c) < 32 for c in agent):
        agent = ""
    row = {"name": name or url.rsplit("/", 1)[-1][:channeltable.NAME_MAX] or "未命名", "url": url, "agent": agent}
    if len(channeltable.line_of(dict(row, id="ch000000")).encode("utf-8")) > channeltable.LINE_MAX:
        return None
    # 节目单对台、台标用的属性（不进 channels.txt，存在旁边的 channels.txt.meta.json，见 channeltable.meta_of）。
    row.update(channeltable.clean_meta(entry))
    return row


def read(body: dict, opener=urllib.request.urlopen) -> dict:
    """body：{url}、{text} 或 {preset, mirror?}（presets.py 里的一个列表，mirror 是第几个地址）。
    回答 {ok, entries: [{name, url, agent}], skipped, total} 或 {ok: False, error}。"""
    url, text = body.get("url"), body.get("text")
    if "preset" in body:
        if url is not None or text is not None:
            return {"ok": False, "error": "请求格式不对"}
        url = presets.url_of(body.get("preset"), body.get("mirror", 0))
        if url is None:
            return {"ok": False, "error": "没有这个播放列表（页面可能是旧的，请刷新）"}
    if isinstance(url, str) and url.strip() and text is None:
        text, error = _fetch(url.strip(), opener)
        if text is None:
            return {"ok": False, "error": error}
    elif isinstance(text, str) and url is None:
        if len(text.encode("utf-8")) > IMPORT_MAX:
            return {"ok": False, "error": "播放列表太大（超过 2 MB）"}
    else:
        return {"ok": False, "error": "请粘贴播放列表的地址，或选一个 M3U 文件"}
    found = m3u.parse_playlist(text)
    entries = [row for row in map(_clean, found) if row is not None]
    if not entries:
        return {"ok": False, "error": "这里面没有找到频道：请确认是 M3U 播放列表（#EXTINF 加地址的格式）"}
    return {"ok": True, "entries": entries[:ENTRIES_MAX], "skipped": len(found) - len(entries),
            "total": len(entries)}


def name_key(name: str) -> str:
    """比较台名用：不分大小写，不管空格、横线和下划线（「CCTV-1」和「cctv 1」算同一个）。"""
    return re.sub(r"[\s\-_]+", "", name).casefold()


def mark(entries: list[dict], rows: list[dict]) -> list[dict]:
    """给读出来的台加上 kind（自动分组）和 dup：
    "url"  地址已经在频道表里；"name" 频道表里有同名的台；"list" 列表里前面已经有同名的台；"" 不重复。
    rows 是现在的频道表（channeltable.read 的 rows）。原地改，也返回 entries。"""
    urls = {row["url"] for row in rows}
    names = {name_key(row["name"]) for row in rows}
    seen: set[str] = set()
    for entry in entries:
        key = name_key(entry["name"])
        entry["kind"] = channel_groups.classify("", entry["name"])
        entry["dup"] = "url" if entry["url"] in urls else "name" if key in names else "list" if key in seen else ""
        seen.add(key)
    return entries


def probe(body: dict, ffmpeg: str, run=subprocess.run, monotonic=time.monotonic) -> dict:
    """试一个地址：{ok, state: ok / slow / dead, seconds}。"""
    url, agent = body.get("url"), body.get("agent") or ""
    if not isinstance(url, str) or not url.startswith(("http://", "https://")) or len(url) > channeltable.URL_MAX \
            or not isinstance(agent, str) or len(agent) > channeltable.AGENT_MAX:
        return {"ok": False, "error": "地址要以 http:// 或 https:// 开头"}
    # -rw_timeout 的单位是微秒。
    command = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "quiet", "-rw_timeout", str(PROBE_DEAD_S * 1000000),
               "-user_agent", agent or DEFAULT_USER_AGENT, "-i", url, "-map", "0:v:0", "-frames:v", "1",
               "-f", "image2pipe", "-"]
    with _probes:
        started = monotonic()
        try:
            done = run(command, capture_output=True, timeout=PROBE_DEAD_S + 4)
            played = len(done.stdout or b"") > 200
        except subprocess.TimeoutExpired:
            played = False
        except OSError:
            return {"ok": False, "error": "找不到 ffmpeg，没法试"}
        elapsed = monotonic() - started
    state = ("ok" if elapsed < PROBE_SLOW_S else "slow") if played else "dead"
    return {"ok": True, "state": state, "seconds": round(elapsed, 1)}
