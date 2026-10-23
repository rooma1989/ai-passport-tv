"""总后台「导入播放列表」里可以一点就读的公开播放列表（自建模式和电脑客户端）。

这里只有播放列表的地址，没有任何直播流地址：列表的内容由各自的维护者在他们的网站上更新，
本项目不带、不改、也不保证其中任何一个台。页面在列表上方写明这一点（见 admin.html 的
导入块）。决定见 docs/superpowers/specs/2026-10-15-self-hosting-design.md 第 8.2 节（2026-10-22 补充）。

挑选的标准：公开、社区维护、2026 年还在更新、有央视和卫视、能被 server/m3u.py 读出来。
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
)


def public() -> list[dict]:
    """给页面的：每条带上全部地址，[{id, name, maintainer, homepage, description, urls: [{label, url}]}]。"""
    return [{"id": p["id"], "name": p["name"], "maintainer": p["maintainer"], "homepage": p["homepage"],
             "description": p["description"],
             "urls": [{"label": "主地址", "url": p["url"]}] + [{"label": label, "url": url}
                                                              for label, url in p["mirrors"]]}
            for p in PRESETS]


def url_of(preset_id, mirror=0) -> str | None:
    """预设的第几个地址（0 是主地址，1… 是 mirrors）；没有这个预设或这个地址时 None。"""
    if not isinstance(preset_id, str) or isinstance(mirror, bool) or not isinstance(mirror, int):
        return None
    for preset in PRESETS:
        if preset["id"] == preset_id:
            urls = [preset["url"]] + [url for _label, url in preset["mirrors"]]
            return urls[mirror] if 0 <= mirror < len(urls) else None
    return None
