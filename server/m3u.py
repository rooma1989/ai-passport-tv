"""M3U 播放列表：读出 (名字, 地址, User-Agent)。

两处用：本地的频道配置页（tools/channel_config.py）和总后台频道表的「导入播放列表」
（server/relay/admin.py，自建模式）。只认 http/https 的地址。
"""

from __future__ import annotations

import re

# #EXTINF 行上的 `http-user-agent="..."`，或单独一行 `#EXTVLCOPT:http-user-agent=...`。
USER_AGENT_RE = re.compile(r"""http-user-agent\s*=\s*["']([^"']*)["']""", re.IGNORECASE)
EXTVLCOPT_UA_RE = re.compile(r"""#EXTVLCOPT:http-user-agent\s*=\s*([^\r\n]*)""", re.IGNORECASE)


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
    """[{name, url, agent}]。

    名字取 #EXTINF 上第一个不在引号里的逗号之后的部分（属性值里可以有逗号，例如
    User-Agent 的 "(KHTML, like Gecko)"）；地址是其后第一行像网址的行。同名的台都保留
    （播放列表里一个台列两次是常见的），由调用方分配 id。
    """
    channels: list[dict] = []
    pending: str | None = None
    pending_agent: str = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXTINF"):
            name = _title(line) or "unnamed"
            pending = name
            match = USER_AGENT_RE.search(line)
            pending_agent = match.group(1) if match else ""
            continue
        if line.startswith("#EXTVLCOPT:http-user-agent"):
            match = EXTVLCOPT_UA_RE.search(line)
            if match:
                pending_agent = match.group(1).strip()
            continue
        if line.startswith("#"):
            continue
        if line.startswith(("http://", "https://")):
            name = pending if pending else line.rsplit("/", 1)[-1]
            channels.append({"name": name, "url": line, "agent": pending_agent})
            pending = None
            pending_agent = ""
    return channels
