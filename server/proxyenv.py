"""本机地址不走代理：给 ffmpeg 等子进程的环境变量补上小写的 no_proxy。

开着代理软件的电脑常带 http_proxy / https_proxy。ffmpeg 只读小写的 no_proxy，不认大写的 NO_PROXY，
于是连 127.0.0.1 上的中转播放流也发给代理，代理回 502，片源起不来（设备一直「加载中」）。
这里把大小写两份合并，并保证回环地址在里面；没有任何代理变量时原样返回。
"""

from __future__ import annotations

from collections.abc import Mapping

PROXY_KEYS = ("http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")
LOOPBACK = ("localhost", "127.0.0.1", "::1")


def loopback_bypass(env: Mapping[str, str]) -> dict[str, str]:
    """返回一份新的环境变量：有代理时 no_proxy 和 NO_PROXY 都含回环地址，其余不变。"""
    result = dict(env)
    if not any(result.get(key) for key in PROXY_KEYS):
        return result
    hosts: list[str] = []
    for key in ("no_proxy", "NO_PROXY"):
        for host in (result.get(key) or "").split(","):
            host = host.strip()
            if host and host not in hosts:
                hosts.append(host)
    for host in LOOPBACK:
        if host not in hosts:
            hosts.append(host)
    result["no_proxy"] = result["NO_PROXY"] = ",".join(hosts)
    return result
