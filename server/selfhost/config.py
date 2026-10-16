"""自建服务器的设置文件 /data/config.toml：第一次启动时带注释写出默认值，之后每次启动读一遍。

设计见 docs/superpowers/specs/2026-10-15-self-hosting-design.md 第 4.4 节。用标准库的 tomllib 读
（Python 3.11 起才有）；写只在第一次（DEFAULT_TEXT 原样），之后是用户自己的文件，不去改它。
读到看不懂的值就报错并说是哪一项，不悄悄换成默认值：设置写错了却照常启动，比不启动更难查。
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from pathlib import Path

DEFAULT_TEXT = """\
# 小智电视自建服务器的设置。改完重启服务器生效（Docker：docker compose restart）。
# 说明：docs/selfhost/README.zh_CN.md

[server]
name = "我家"                   # 页面标题和设置页上显示的名字
lan_port = 8080                 # 手机遥控、电脑版、总后台、第一次设置：只给家里的网络用
public_port = 0                 # 给隧道（Cloudflare Tunnel、Tailscale serve）用的端口，例如 8081；0 = 不开
device_address = ""             # 小电视连的地址（局域网 IP 或 xxx.local，不带端口）；空 = 第一次设置时自动记下
public_url = ""                 # 有隧道时填它的网址，例如 https://tv.example.com；空 = 用局域网地址
bind = "0.0.0.0"                # 听哪个地址；只想本机试用时写 "127.0.0.1"

[devices]
allow_internet = false          # 允许公网地址的设备连进来（端口转发时才需要，默认只认家里的网络）
max_streams = 0                 # 同时最多几路（0 = 不限）；0.x 测试版每个型号一次只服务一台，这一项还不起作用
xingzhi_port = 8098             # 星智 Cube 的端口（它的固件默认连这个，一般不要改）
passport_port = 8097            # AI Passport 的端口（同上）

[accounts]
signup_via_device = "lan"       # 扫未绑定小电视的码自己注册："lan" 只在家里网络，"anywhere" 隧道也行，"off" 不允许
admin_over_tunnel = false       # 隧道端口上也开放总后台和密钥登录（不建议）

[youtube]
mode = "off"                    # "off" 不用；"node" 用你自己的 YouTube 节点（secrets/youtube-node.json）
proxy = ""                      # 以后的本机模式用，现在不起作用
cookies = "youtube/cookies.txt"

[voice]
asr_socket = ""                 # 语音识别容器的套接字，例如 "/data/run/asr.sock"；空 = 不开语音

[maintenance]
backup_keep_days = 14           # 每晚备份数据库，留几天
update_check = true             # 以后的「有新版本」提示用，现在不起作用
"""

SIGNUP = ("lan", "anywhere", "off")
YOUTUBE = ("off", "node", "local")
_HOST = re.compile(r"^(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?$")
_URL = re.compile(r"^https?://[A-Za-z0-9.:\[\]-]+(?::\d+)?/?$")


class ConfigError(ValueError):
    """设置文件有错：消息给人看（中文，带是哪一项）。"""


@dataclass
class Config:
    name: str = "我家"
    lan_port: int = 8080
    public_port: int = 0
    device_address: str = ""
    public_url: str = ""
    bind: str = "0.0.0.0"
    allow_internet: bool = False
    max_streams: int = 0
    xingzhi_port: int = 8098
    passport_port: int = 8097
    signup_via_device: str = "lan"
    admin_over_tunnel: bool = False
    youtube_mode: str = "off"
    youtube_proxy: str = ""
    youtube_cookies: str = "youtube/cookies.txt"
    asr_socket: str = ""
    backup_keep_days: int = 14
    update_check: bool = True


# (表, 键, Config 的字段, 类型)
_FIELDS = (
    ("server", "name", "name", str), ("server", "lan_port", "lan_port", int),
    ("server", "public_port", "public_port", int), ("server", "device_address", "device_address", str),
    ("server", "public_url", "public_url", str), ("server", "bind", "bind", str),
    ("devices", "allow_internet", "allow_internet", bool), ("devices", "max_streams", "max_streams", int),
    ("devices", "xingzhi_port", "xingzhi_port", int), ("devices", "passport_port", "passport_port", int),
    ("accounts", "signup_via_device", "signup_via_device", str),
    ("accounts", "admin_over_tunnel", "admin_over_tunnel", bool),
    ("youtube", "mode", "youtube_mode", str), ("youtube", "proxy", "youtube_proxy", str),
    ("youtube", "cookies", "youtube_cookies", str),
    ("voice", "asr_socket", "asr_socket", str),
    ("maintenance", "backup_keep_days", "backup_keep_days", int),
    ("maintenance", "update_check", "update_check", bool),
)
# 这几个端口在中转和电视服务里写死了（server/relay/table.py 的 STREAM_BASE、语音的中转地址、
# 「用手机听」的路线），设置文件里不让改，只用来查冲突。
FIXED_PORTS = {"relay_streams": 8099, "relay_pages": 8100, "listen_xingzhi": 8198, "listen_passport": 8199}


def ensure(path: Path) -> bool:
    """没有设置文件就写出默认的；返回是不是新写的。"""
    path = Path(path)
    if path.exists():
        return False
    path.write_text(DEFAULT_TEXT, encoding="utf-8")
    return True


def parse(text: str) -> Config:
    import tomllib
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"config.toml 格式不对：{error}") from None
    config = Config()
    for table, key, attr, kind in _FIELDS:
        section = data.get(table, {})
        if not isinstance(section, dict):
            raise ConfigError(f"config.toml：[{table}] 应该是一个表")
        if key not in section:
            continue
        value = section[key]
        # bool 是 int 的子类：端口写成 true 也要报错。
        if not isinstance(value, kind) or (kind is int and isinstance(value, bool)):
            raise ConfigError(f"config.toml：[{table}] {key} 的类型不对（应该是{_KIND_NAMES[kind]}）")
        setattr(config, attr, value.strip() if isinstance(value, str) else value)
    _check(config)
    return config


_KIND_NAMES = {str: "带引号的文字", int: "整数", bool: "true 或 false"}


def _check(config: Config) -> None:
    for attr in ("lan_port", "xingzhi_port", "passport_port"):
        if not 1 <= getattr(config, attr) <= 65535:
            raise ConfigError(f"config.toml：{attr} 要在 1 到 65535 之间")
    if not 0 <= config.public_port <= 65535:
        raise ConfigError("config.toml：public_port 要在 0 到 65535 之间（0 = 不开）")
    ports = [config.lan_port, config.xingzhi_port, config.passport_port] + \
        ([config.public_port] if config.public_port else [])
    taken = set(FIXED_PORTS.values())
    if len(set(ports)) != len(ports) or taken & set(ports):
        raise ConfigError("config.toml：端口重复了（8099、8100、8198、8199 留给服务器内部用）")
    try:
        ipaddress.IPv4Address(config.bind)
    except ValueError:
        raise ConfigError("config.toml：bind 要写 IPv4 地址，例如 \"0.0.0.0\"") from None
    if config.device_address and not _HOST.match(config.device_address):
        raise ConfigError("config.toml：device_address 只写地址（IP 或 xxx.local），不带 http:// 和端口")
    if config.public_url and not _URL.match(config.public_url):
        raise ConfigError("config.toml：public_url 要写成 http(s)://主机名[:端口]，后面不带路径")
    if config.signup_via_device not in SIGNUP:
        raise ConfigError(f"config.toml：signup_via_device 只能是 {' / '.join(SIGNUP)}")
    if config.youtube_mode not in YOUTUBE:
        raise ConfigError(f"config.toml：[youtube] mode 只能是 {' / '.join(YOUTUBE)}")
    if config.max_streams < 0 or config.backup_keep_days < 1:
        raise ConfigError("config.toml：max_streams 不能是负数，backup_keep_days 至少 1")
    if len(config.name) > 20:
        raise ConfigError("config.toml：name 最多 20 个字")


def load(path: Path) -> Config:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise ConfigError(f"读不了 {path}：{type(error).__name__}") from None
    return parse(text)
