"""总后台「导入播放列表」里可以一点就读的公开播放列表（自建模式和电脑客户端）。

这里只有播放列表的地址，没有任何直播流地址：列表的内容由各自的维护者在他们的网站上更新，
本项目不带、不改、也不保证其中任何一个台。页面在列表上方写明这一点（见电脑版「管理 → 频道表」
的导入块，assets/console-admin.js）。决定见 docs/superpowers/specs/2026-10-15-self-hosting-design.md 第 8.2 节（2026-10-22 补充）。

挑选的标准：公开、社区维护、2026 年还在更新、有央视和卫视、能被 server/m3u.py 读出来，
而且是普通的 http(s) 直播地址（不收组播 rtp/udp、只有 IPv6 的列表，也不收会过期的带签名地址的列表）。
镜像只放上游自己写在说明里的（例如 gh-proxy 加速链接），不自己编。每条在 tests/test_playlist_presets.py
里检查格式；地址失效时改这张表就行，页面和接口不用动。
"""

from __future__ import annotations

# 每一条：id（接口里用）、name、url（上游的主地址）、mirrors（上游提供的其他地址，[(说明, 地址)]）、
# maintainer、homepage（维护者的项目主页）、description（给人看的一句话）。
PRESETS: tuple[dict, ...] = (
    {
        "id": "iptv-org-cn",
        "name": "iptv-org：中国",
        "url": "https://iptv-org.github.io/iptv/countries/cn.m3u",
        "mirrors": (),
        "maintainer": "iptv-org 社区",
        "homepage": "https://github.com/iptv-org/iptv",
        "description": "全球最大的公开电视频道收录项目，按国家整理的中国频道。央视较全；台名是英文"
                       "（例如 Hunan Satellite TV），部分源有地区限制。",
    },
    {
        "id": "iptv-org-zho",
        "name": "iptv-org：中文",
        "url": "https://iptv-org.github.io/iptv/languages/zho.m3u",
        "mirrors": (),
        "maintainer": "iptv-org 社区",
        "homepage": "https://github.com/iptv-org/iptv",
        "description": "同一项目按语言整理的中文频道：内地、港澳台和海外的华语台。",
    },
    {
        "id": "vbskycn-ipv4",
        "name": "vbskycn/iptv：IPv4",
        "url": "https://live.zbds.top/tv/iptv4.m3u",
        "mirrors": (
            ("gh-proxy 加速（上游提供）",
             "https://gh-proxy.com/raw.githubusercontent.com/vbskycn/iptv/refs/heads/master/tv/iptv4.m3u"),
            ("GitHub 原文件", "https://raw.githubusercontent.com/vbskycn/iptv/master/tv/iptv4.m3u"),
        ),
        "maintainer": "vbskycn",
        "homepage": "https://github.com/vbskycn/iptv",
        "description": "自动整理、每几个小时更新一次的中文列表：央视、卫视、地方台、电影台。",
    },
    {
        "id": "suxuang-ipv4",
        "name": "myIPTV（suxuang）：IPv4",
        "url": "https://raw.githubusercontent.com/suxuang/myIPTV/main/ipv4.m3u",
        "mirrors": (
            ("gh-proxy 加速（上游提供）", "https://gh-proxy.org/raw.githubusercontent.com/suxuang/myIPTV/main/ipv4.m3u"),
        ),
        "maintainer": "suxuang",
        "homepage": "https://github.com/suxuang/myIPTV",
        "description": "手工维护的大列表：央视、卫视、港澳台都有，同一个台常列好几个源（重复的会标出来）。",
    },
    {
        "id": "guovin-ipv4",
        "name": "iptv-api（Guovin）：IPv4",
        "url": "https://raw.githubusercontent.com/Guovin/iptv-api/gd/output/ipv4/result.m3u",
        "mirrors": (
            ("gh-proxy 加速（上游在列表里用的）",
             "https://gh-proxy.com/https://raw.githubusercontent.com/Guovin/iptv-api/gd/output/ipv4/result.m3u"),
        ),
        "maintainer": "Guovin",
        "homepage": "https://github.com/Guovin/iptv-api",
        "description": "程序自动测速后整理的结果：央视、卫视齐全，同一个台常有好几个源（重复的会标出来），"
                       "带 tvg-id 和台标，节目单对得上。",
    },
    {
        "id": "aptv-iptv",
        "name": "APTV（Kimentanm）",
        "url": "https://raw.githubusercontent.com/Kimentanm/aptv/master/m3u/iptv.m3u",
        "mirrors": (),
        "maintainer": "Kimentanm",
        "homepage": "https://github.com/Kimentanm/aptv",
        "description": "APTV 播放器作者维护的小列表：央视、卫视基本一台一个源，部分源要求的 User-Agent 已写在列表里。",
    },
)

# 节目单（XMLTV）：总后台「频道表」页的「节目单」一块，管理员自己打开（默认一个都不开，server/relay/epg.py）。
# 和上面一样只有地址：节目单由各自的维护者生成和更新。字段同上；url 是 XMLTV 文件（可以是 gzip 压缩的）。
EPG_PRESETS: tuple[dict, ...] = (
    {
        "id": "epg-112114",
        "name": "112114 节目单",
        "url": "https://epg.112114.xyz/pp.xml",
        "mirrors": (),
        "maintainer": "112114",
        "homepage": "https://epg.112114.xyz/",
        "description": "当天的节目单：央视、卫视和很多地方台，台名就是中文名。下载约 0.2 MB（gzip，解开约 3 MB）。",
    },
    {
        "id": "epg-fanmingming",
        "name": "fanmingming 节目单",
        "url": "https://live.fanmingming.cn/e.xml",
        "mirrors": (),
        "maintainer": "fanmingming",
        "homepage": "https://github.com/fanmingming/live",
        "description": "央视、卫视和付费数字台，前后约一周（只留用得着的一天）。下载约 0.5 MB（gzip，解开约 7 MB）。",
    },
)


def public(table=None) -> list[dict]:
    """给页面的：每条带上全部地址，[{id, name, maintainer, homepage, description, urls: [{label, url}]}]。
    table 默认播放列表（PRESETS），节目单用 epg_public()。"""
    return [{"id": p["id"], "name": p["name"], "maintainer": p["maintainer"], "homepage": p["homepage"],
             "description": p["description"],
             "urls": [{"label": "主地址", "url": p["url"]}] + [{"label": label, "url": url}
                                                              for label, url in p["mirrors"]]}
            for p in (PRESETS if table is None else table)]


def epg_public() -> list[dict]:
    """节目单预设，格式同 public()。"""
    return public(EPG_PRESETS)


def url_of(preset_id, mirror=0, table=None) -> str | None:
    """预设的第几个地址（0 是主地址，1… 是 mirrors）；没有这个预设或这个地址时 None。table 默认播放列表。"""
    if not isinstance(preset_id, str) or isinstance(mirror, bool) or not isinstance(mirror, int):
        return None
    for preset in PRESETS if table is None else table:
        if preset["id"] == preset_id:
            urls = [preset["url"]] + [url for _label, url in preset["mirrors"]]
            return urls[mirror] if 0 <= mirror < len(urls) else None
    return None
