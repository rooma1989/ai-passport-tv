"""M3U 播放列表：读出 (名字, 地址, User-Agent)，还有节目单和台标用的 tvg-id / tvg-name / tvg-logo。

两处用：本地的频道配置页（tools/channel_config.py）和总后台频道表的「导入播放列表」
（server/relay/admin.py，自建模式）。只认 http/https 的地址。
"""

from __future__ import annotations

import re

# #EXTINF 行上的 `http-user-agent="..."`，或单独一行 `#EXTVLCOPT:http-user-agent=...`。
USER_AGENT_RE = re.compile(r"""http-user-agent\s*=\s*["']([^"']*)["']""", re.IGNORECASE)
EXTVLCOPT_UA_RE = re.compile(r"""#EXTVLCOPT:http-user-agent\s*=\s*([^\r\n]*)""", re.IGNORECASE)
# 节目单对台（server/relay/epg.py）和台标用的属性：tvg-id="CCTV1"，也有不带引号的 tvg-name=湖南卫视。
TVG_RE = re.compile(r"""(?<![\w-])tvg-(id|name|logo)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s,"']+))""", re.IGNORECASE)
TVG_KEYS = {"id": "tvg_id", "name": "tvg_name", "logo": "logo"}


def _tvg(extinf: str) -> dict:
    """#EXTINF 行上的 tvg-id、tvg-name、tvg-logo（没有的为空串），只看台名前面的属性部分。"""
    found = {"tvg_id": "", "tvg_name": "", "logo": ""}
    for match in TVG_RE.finditer(extinf):
        key = TVG_KEYS[match.group(1).lower()]
        if not found[key]:
            found[key] = next(v for v in match.group(2, 3, 4) if v is not None).strip()
    return found


def _title(extinf: str) -> str:
    """#EXTINF 行的台名：第一个不在双引号里的逗号之后。"""
    quoted = False
    for index, char in enumerate(extinf):
        if char == '"':
            quoted = not quoted
        elif char == "," and not quoted:
            return extinf[index + 1:].strip()
    # 引号没配对（写坏了的行）：退回第一个逗号。
    return extinf.partition(",")[2].strip()


def parse_playlist(text: str) -> list[dict]:
    """[{name, url, agent, tvg_id, tvg_name, logo, routes}]（tvg 三项没有时是空串；routes 是 TXT 一行里
    用 # 隔开的其余地址，M3U 的为空——M3U 里同一个台的几行由调用方合并，见 server/relay/playlist.py）。

    也读 TXT 直播源（「台名,地址」一行一个、「分组,#genre#」分组行），见下面的说明。

    名字取 #EXTINF 上第一个不在引号里的逗号之后的部分（属性值里可以有逗号，例如
    User-Agent 的 "(KHTML, like Gecko)"）；地址是其后第一行像网址的行。同名的台都保留
    （播放列表里一个台列两次是常见的），由调用方分配 id。
    """
    channels: list[dict] = []
    pending: str | None = None
    pending_agent: str = ""
    pending_tvg: dict = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXTINF"):
            name = _title(line) or "unnamed"
            pending = name
            match = USER_AGENT_RE.search(line)
            pending_agent = match.group(1) if match else ""
            # 属性只在台名之前：台名里正好有「tvg-id=」这种字样的不算。
            pending_tvg = _tvg(line[:len(line) - len(name)] if line.endswith(name) else line)
            continue
        if line.startswith("#EXTVLCOPT:http-user-agent"):
            match = EXTVLCOPT_UA_RE.search(line)
            if match:
                pending_agent = match.group(1).strip()
            continue
        if line.startswith("#"):
            continue
        if not line.startswith(("http://", "https://")) and pending is None:
            # TXT 直播源（国内常见，DIYP 格式）：「台名,地址」一行一个，「分组,#genre#」是分组行；
            # 一个台写几条线路时用 # 隔开，地址后面的 $说明 是线路名。第一条是地址，其余的（http/https 的）
            # 放进 routes（备用线路，见 server/routes.py）。
            label, comma, rest = line.partition(",")
            found = [part.split("$", 1)[0].strip() for part in rest.split("#")]
            found = [u for u in found if u.startswith(("http://", "https://"))]
            first = rest.split("#", 1)[0].split("$", 1)[0].strip()
            if comma and label.strip() and first.startswith(("http://", "https://")):
                channels.append({"name": label.strip(), "url": first, "agent": "",
                                 "tvg_id": "", "tvg_name": "", "logo": "",
                                 "routes": list(dict.fromkeys(u for u in found[1:] if u != first))})
            continue
        if line.startswith(("http://", "https://")):
            name = pending if pending else line.rsplit("/", 1)[-1]
            channels.append({"name": name, "url": line, "agent": pending_agent,
                             **(pending_tvg if pending else {"tvg_id": "", "tvg_name": "", "logo": ""}),
                             "routes": []})
            pending = None
            pending_agent = ""
            pending_tvg = {}
    return channels
