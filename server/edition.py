"""这个核心是哪个产品："full"（RooTV，全部功能）或 "lite"（精简版：设备、直播、B 站、基本设置）。

设计见 docs/superpowers/specs/2026-11-04-minimal-product-design.md 第 3 节。打包时 tools/build_core.py
生成 server/_edition_build.py 把版本写死（用户改不了）；开发和测试用环境变量 TV_EDITION（只在没有这个生成文件时才读）。精简版不删模块
（YouTube、片库和 B 站的代码互相 import），只是不启动、不开接口、控制台不显示。
"""

from __future__ import annotations

import os

EDITIONS = ("full", "lite")
ENV = "TV_EDITION"
EDITION = "full"
CHANNELS_URL = ""        # 精简版拉频道包更新的地址；空 = 只用随包带的那份
BAKED = False
try:  # 打包时生成，仓库里没有（.gitignore）
    from ._edition_build import CHANNELS_URL, EDITION  # type: ignore
    BAKED = True
except ModuleNotFoundError as error:
    if error.name != f"{__package__}._edition_build":
        raise

# 精简版拦掉的接口（去掉 /tv 前缀以后的路径；等于它或以它加 "/" 开头都算）。
LITE_BLOCKED = (
    "/api/library", "/api/admin/library", "/api/admin/clouddrive", "/api/admin/storage", "/api/admin/driver",
    "/api/youtube", "/api/yt", "/api/listen", "/api/voice", "/api/pet", "/api/epg", "/api/admin/epg",
    "/api/series", "/api/detail/series", "/api/watch", "/api/subs", "/api/search/local",
    "/api/invites", "/api/admin/invites", "/api/admin/audit", "/api/phone-link",
    # 精简版的线路归频道包管，原来的频道表编辑器整个关掉（换成 /api/admin/pack）。
    "/api/admin/channels",
)
OFF_MESSAGE = "精简版没有这个功能"


def current() -> str:
    if BAKED:
        return EDITION
    value = os.environ.get(ENV, "").strip().lower()
    return value if value in EDITIONS else EDITION


def describe() -> str:
    """启动时打一行：现在是哪个版本、从哪来的。"""
    if BAKED:
        return f"版本：{EDITION}（打包时写死）"
    if current() != EDITION or os.environ.get(ENV, "").strip():
        return f"版本：{current()}（开发用环境变量 {ENV}）"
    return f"版本：{EDITION}（默认）"


def is_lite() -> bool:
    return current() == "lite"


def blocked(path: str) -> bool:
    """path must already be parsed: no query string, /tv prefix stripped (web.py `_path()`)."""
    if not is_lite():
        return False
    return any(path == prefix or path.startswith(prefix + "/") for prefix in LITE_BLOCKED)
