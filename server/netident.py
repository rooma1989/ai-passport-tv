"""自动识别设备应填写的服务端地址。

给出两种形式：

* 名称（`<主机名>.local`）：填到设备上。路由器重新分配 IP 后名称仍然有效。
* 数字地址（`192.168.1.20`）：套接字绑定需要字面地址，不能监听名称。

名称查询方式因系统而异，逐一尝试；失败就报告"名称未知"，不猜测，错误的名称比没有名称更糟。
"""

from __future__ import annotations

import ipaddress
import platform
import re
import socket
import subprocess
import sys
from dataclasses import dataclass, field

# ---- 本机在家里网络中的地址 -------------------------------------------------------------
#
# 以前只用 UDP 探测：把套接字 connect 到远端地址（不发包），取内核选的出口网卡的本端地址。代理软件开
# TUN 模式（Clash、Surge、Stash，198.18.0.0/15）、Tailscale（100.64.0.0/10）、WireGuard 时，出口网卡
# 是虚拟网卡，探测得到的是 198.18.0.1 这类地址，设备和手机都连不到。所以先按系统列出所有网卡的 IPv4
# 地址和默认路由，挑物理网卡上、带着家里路由器（默认网关在它的网段里）的那个私有地址；列不出来时才用探测，
# 且探测结果落在下面这些网段里时不用。只用标准库和系统自带的命令（ifconfig、netstat、ip、ipconfig、
# PowerShell），解析和运行分开，测试喂固定的输出。

# 不是家里网络的地址：代理软件的 TUN（RFC 2544 基准测试网段）、运营商级 NAT（Tailscale）、
# 链路本地、回环、"这个网络"、组播和保留地址。
EXCLUDED_NETWORKS = tuple(ipaddress.IPv4Network(n) for n in (
    "198.18.0.0/15", "100.64.0.0/10", "169.254.0.0/16", "127.0.0.0/8", "0.0.0.0/8", "224.0.0.0/3"))
PRIVATE_NETWORKS = tuple(ipaddress.IPv4Network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))

# 隧道：从不选（VPN、代理的 TUN、macOS 自己的 awdl/llw/anpi/ap 这类）。
_TUNNEL = re.compile(
    r"^(utun|tun|tap|wg|tailscale|zt|zerotier|ppp|ipsec|gif|stf|awdl|llw|anpi|ap\d|feth|nordlynx|wintun|"
    r"clash|mihomo|meta$|sing-?box|loopback|lo\d*$)|"
    r"(tailscale|wireguard|openvpn|tap-windows|wintun|zerotier|vpn|tunnel|clash|mihomo|loopback)", re.IGNORECASE)
# 虚拟网卡和网桥：容器、虚拟机、Hyper-V、网络共享。只有默认网关在它的网段里（网桥接着家里的网络，
# 例如 Hyper-V 的外部交换机）时才选。
_VIRTUAL = re.compile(
    r"^(docker|br-|veth|vmnet|vboxnet|virbr|bridge|vnic|cni|flannel|cali|kube|lxc|lxd|incus|podman|"
    r"vethernet)|(virtual|vmware|virtualbox|hyper-v|vethernet|docker|wsl|bluetooth|蓝牙)", re.IGNORECASE)
# 常见的物理网卡名字（同分时优先）：macOS en0…，Linux eth0/enp3s0/wlan0/wlp2s0，Windows Wi-Fi/WLAN/Ethernet/以太网。
_PHYSICAL = re.compile(r"^(en\d|eth|en[ospx]|wlan|wl|wi-?fi|ethernet|以太网|无线)", re.IGNORECASE)

# UDP 探测用的远端地址。优先用保留的文档地址，即使误发也不会到达任何主机。
_PROBE_ADDRESSES = (
    ("192.0.2.1", 9),        # RFC 5737 TEST-NET-1
    ("198.51.100.1", 9),     # RFC 5737 TEST-NET-2
    ("8.8.8.8", 53),         # 最后兜底：总有路由的地址
)
_IPV4 = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d{1,3}){3})(?![\d.])")
_CREATE_NO_WINDOW = 0x08000000
COMMAND_TIMEOUT_S = 8


@dataclass
class Interface:
    """一个 IPv4 地址和它所在的网卡。physical：系统说它是不是物理网卡（Windows 的 Get-NetAdapter），
    不知道时 None；pointopoint：macOS 的点对点标志（隧道）。"""
    name: str
    address: str
    prefix: int = 32
    physical: bool | None = None
    pointopoint: bool = False
    index: int | None = None


@dataclass
class Route:
    """一条默认路由：网卡名字（或 Windows 的 ifIndex）、下一跳（没有时空串）。"""
    name: str
    gateway: str = ""
    index: int | None = None


@dataclass
class LanInfo:
    """address：设备和手机该用的地址（找不到时空串）。local：这台电脑在家里网络上的全部地址；
    列不出网卡时 None（这时不能用它判断一个记下的地址是不是过时了）。"""
    address: str = ""
    local: frozenset | None = field(default=None)


def _ipv4(text: str) -> ipaddress.IPv4Address | None:
    try:
        return ipaddress.IPv4Address(text.strip())
    except (ipaddress.AddressValueError, ValueError):
        return None


def excluded(address: str) -> bool:
    """address 是 IPv4 字面地址且落在不是家里网络的网段里（TUN、Tailscale、链路本地、回环……）。
    不是 IPv4 字面地址（xxx.local 名字、空串）时 False。"""
    ip = _ipv4(address or "")
    return ip is not None and any(ip in net for net in EXCLUDED_NETWORKS)


def is_rfc1918(address: str) -> bool:
    ip = _ipv4(address or "")
    return ip is not None and any(ip in net for net in PRIVATE_NETWORKS)


def stored_address_ok(value: str, local: frozenset | None) -> bool:
    """第一次设置时记下的地址（数据库的 device_address、public_url 的主机）还能不能用。

    名字（xxx.local）一律能用；IPv4 落在排除的网段里不能用；能列出这台电脑的地址、而它不在其中
    （换了 DHCP 地址、换了路由器）也不能用。空串 False。
    """
    if not value:
        return False
    if _ipv4(value) is None:
        return True
    if excluded(value):
        return False
    return local is None or value in local


# ---- 解析（每个系统、每个命令一个）-----------------------------------------------------

def _mask_prefix(mask: str) -> int:
    try:
        value = int(mask, 16) if mask.lower().startswith("0x") else int(ipaddress.IPv4Address(mask))
    except ValueError:
        return 32
    return bin(value & 0xFFFFFFFF).count("1")


def parse_ifconfig(text: str) -> list[Interface]:
    """macOS（和 BSD）的 `ifconfig`：每块第一行是 `en0: flags=8863<UP,…>`，下面 `inet a.b.c.d netmask 0x…`。
    没有 UP 的网卡不要。"""
    found: list[Interface] = []
    name, up, p2p = "", False, False
    for line in text.splitlines():
        header = re.match(r"^(\S+?):\s+flags=\w+<([^>]*)>", line)
        if header:
            name = header.group(1)
            flags = header.group(2).split(",")
            up, p2p = "UP" in flags, "POINTOPOINT" in flags
            continue
        inet = re.match(r"^\s+inet\s+(\S+)(?:\s+-->\s+\S+)?\s+netmask\s+(\S+)", line)
        if inet and name and up and _ipv4(inet.group(1)):
            found.append(Interface(name, inet.group(1), _mask_prefix(inet.group(2)), pointopoint=p2p))
    return found


def parse_netstat_routes(text: str) -> list[Route]:
    """macOS 的 `netstat -rn -f inet`：每条 `default` 路由（代理开 TUN 时原来的默认路由还在，只是不再排第一）。"""
    routes = []
    for line in text.splitlines():
        fields = line.split()
        if len(fields) >= 4 and fields[0] == "default":
            gateway = fields[1] if _ipv4(fields[1]) else ""
            routes.append(Route(fields[3], gateway))
    return routes


def parse_ip_addr(text: str) -> list[Interface]:
    """Linux 的 `ip -4 -o addr show`：`2: eth0    inet 192.168.1.5/24 brd … scope global eth0\\ …`。"""
    found = []
    for line in text.splitlines():
        match = re.match(r"^\d+:\s+(\S+)\s+inet\s+(\d+\.\d+\.\d+\.\d+)(?:/(\d+))?", line)
        if match:
            name = match.group(1).split("@", 1)[0]
            found.append(Interface(name, match.group(2), int(match.group(3) or 32)))
    return found


def parse_ip_route(text: str) -> list[Route]:
    """Linux 的 `ip -4 route show default`：`default via 192.168.1.1 dev eth0 … metric 100`，按 metric 排好。"""
    def after(fields: list[str], key: str) -> str:
        return fields[fields.index(key) + 1] if key in fields[:-1] else ""

    routes = []
    for line in text.splitlines():
        fields = line.split()
        if not fields or fields[0] != "default" or not after(fields, "dev"):
            continue
        gateway, metric = after(fields, "via"), after(fields, "metric")
        routes.append((int(metric) if metric.isdigit() else 0, len(routes),
                       Route(after(fields, "dev"), gateway if _ipv4(gateway) else "")))
    return [r for _, _, r in sorted(routes, key=lambda r: (r[0], r[1]))]


def _hex_ip(text: str) -> str:
    raw = int(text, 16)
    return ".".join(str((raw >> shift) & 0xFF) for shift in (0, 8, 16, 24))


def parse_proc_route(text: str) -> tuple[list[Route], list[tuple[str, ipaddress.IPv4Network]]]:
    """Linux 的 /proc/net/route（没有 ip 命令时）：默认路由（按 metric 排好），和每个网卡直连的网段
    （用来把 `hostname -I` 的地址对上网卡）。地址是小端的十六进制。"""
    defaults, links = [], []
    for line in text.splitlines()[1:]:
        fields = line.split()
        if len(fields) < 8:
            continue
        try:
            dest, gateway, mask, metric = _hex_ip(fields[1]), _hex_ip(fields[2]), _hex_ip(fields[7]), int(fields[6])
        except ValueError:
            continue
        if dest == "0.0.0.0" and mask == "0.0.0.0":
            defaults.append((metric, len(defaults), Route(fields[0], "" if gateway == "0.0.0.0" else gateway)))
        elif gateway == "0.0.0.0":
            try:
                links.append((fields[0], ipaddress.IPv4Network(f"{dest}/{mask}")))
            except ValueError:
                continue
    return [r for _, _, r in sorted(defaults, key=lambda r: (r[0], r[1]))], links


def parse_hostname_i(text: str, links) -> list[Interface]:
    """`hostname -I` 的地址，按 /proc/net/route 的直连网段对上网卡（对不上的名字留空）。"""
    found = []
    for word in text.split():
        ip = _ipv4(word)
        if ip is None:
            continue
        name, prefix = "", 32
        for link_name, network in links:
            if ip in network and network.prefixlen >= 1:
                name, prefix = link_name, network.prefixlen
                break
        found.append(Interface(name, str(ip), prefix))
    return found


def parse_windows_json(text: str) -> tuple[list[Interface], list[Route]]:
    """WINDOWS_SCRIPT 的输出：{"physical": [ifIndex…], "addresses": [{ifIndex, InterfaceAlias, IPAddress,
    PrefixLength}…], "routes": [{ifIndex, NextHop, RouteMetric}…]}。"""
    import json
    try:
        data = json.loads(text.lstrip("\ufeff")) if text.strip() else {}
    except ValueError:
        return [], []
    if not isinstance(data, dict):
        return [], []

    def items(key):
        value = data.get(key)
        value = value if isinstance(value, list) else [value] if value not in (None, "") else []
        return value

    physical = {item for item in items("physical") if isinstance(item, int)}
    found = []
    for item in items("addresses"):
        if not isinstance(item, dict) or not _ipv4(str(item.get("IPAddress") or "")):
            continue
        index = item.get("ifIndex") if isinstance(item.get("ifIndex"), int) else None
        try:
            prefix = int(item.get("PrefixLength") or 32)
        except (TypeError, ValueError):
            prefix = 32
        found.append(Interface(str(item.get("InterfaceAlias") or ""), str(item["IPAddress"]), prefix,
                               physical=(index in physical) if physical and index is not None else None,
                               index=index))
    routes = []
    for item in items("routes"):
        if not isinstance(item, dict):
            continue
        index = item.get("ifIndex") if isinstance(item.get("ifIndex"), int) else None
        hop = str(item.get("NextHop") or "")
        try:
            metric = int(item.get("RouteMetric") or 0)
        except (TypeError, ValueError):
            metric = 0
        routes.append((metric, len(routes), Route("", hop if _ipv4(hop) and hop != "0.0.0.0" else "", index)))
    return found, [r for _, _, r in sorted(routes, key=lambda r: (r[0], r[1]))]


def parse_ipconfig(text: str) -> tuple[list[Interface], list[Route]]:
    """Windows 的 `ipconfig`（PowerShell 不能用时）。各种语言的输出都按结构读，不认字：
    不缩进、以冒号结尾的行是一块网卡的标题；块里第一个带 "IPv4" 的行是地址，255. 开头的是掩码，
    其余的 IPv4（默认网关那一行，或它的续行）是网关。"""
    found, routes = [], []
    blocks: list[tuple[str, list[str]]] = []
    for line in text.splitlines():
        if line.strip() and not line[0].isspace() and line.rstrip().endswith(":"):
            blocks.append((line.rstrip()[:-1].strip(), []))
        elif blocks:
            blocks[-1][1].append(line)
    for title, lines in blocks:
        address, prefix, gateway = "", 32, ""
        for line in lines:
            values = _IPV4.findall(line)
            if not values:
                continue
            value = values[-1]
            if not address and "IPv4" in line:
                address = value
            elif value.startswith("255.") or value == "0.0.0.0":
                prefix = _mask_prefix(value) if value.startswith("255.") else prefix
            elif address and not gateway:
                gateway = value
        if address and _ipv4(address):
            found.append(Interface(title, address, prefix))
            if gateway:
                routes.append(Route(title, gateway))
    return found, routes


# ---- 选地址 -----------------------------------------------------------------------------

def _gateway_on(iface: Interface, routes: list[Route]) -> int | None:
    """这个网卡带着家里路由器（一条默认路由的下一跳在它的网段里）时，返回那条路由的序号（越小越优先）。"""
    try:
        network = ipaddress.IPv4Interface(f"{iface.address}/{iface.prefix}").network
    except ValueError:
        return None
    for order, route in enumerate(routes):
        same = (route.index is not None and route.index == iface.index) or \
            (route.index is None and route.name and route.name == iface.name)
        hop = _ipv4(route.gateway)
        if same and hop is not None and hop in network and hop != network.network_address and \
                not excluded(route.gateway):
            return order
    return None


def _kind(iface: Interface) -> str:
    """tunnel（从不选）、virtual（带着家里路由器时才选）或 normal。"""
    if iface.pointopoint or _TUNNEL.search(iface.name):
        return "tunnel"
    if iface.physical is False or _VIRTUAL.search(iface.name):
        return "virtual"
    return "normal"


def candidates(interfaces: list[Interface], routes: list[Route]) -> list[tuple[tuple, Interface]]:
    """能给设备用的地址，按好坏排好：(分数, 网卡)。分数：带着家里路由器 > 私有地址 > 物理网卡；
    同分时默认路由靠前的（metric 小、排在前面）优先，再按网卡出现的顺序。"""
    ranked = []
    for position, iface in enumerate(interfaces):
        if _ipv4(iface.address) is None or excluded(iface.address):
            continue
        kind = _kind(iface)
        order = _gateway_on(iface, routes)
        if kind == "tunnel" or (kind == "virtual" and order is None):
            continue
        physical = iface.physical is True or (iface.physical is None and bool(_PHYSICAL.match(iface.name)))
        score = (order is not None, is_rfc1918(iface.address), physical and kind == "normal")
        ranked.append(((score, -(order if order is not None else 1 << 16), -position), iface))
    ranked.sort(key=lambda item: item[0], reverse=True)
    return ranked


def choose(interfaces: list[Interface], routes: list[Route]) -> str:
    ranked = candidates(interfaces, routes)
    return ranked[0][1].address if ranked else ""


# ---- 问系统 -----------------------------------------------------------------------------

WINDOWS_SCRIPT = (
    "$ErrorActionPreference='SilentlyContinue';"
    "[Console]::OutputEncoding=New-Object System.Text.UTF8Encoding($false);"
    "$p=@(Get-NetAdapter -Physical|ForEach-Object{[int]$_.ifIndex});"
    "$a=@(Get-NetIPAddress -AddressFamily IPv4|Select-Object @{n='ifIndex';e={[int]$_.ifIndex}},"
    "InterfaceAlias,IPAddress,@{n='PrefixLength';e={[int]$_.PrefixLength}});"
    "$r=@(Get-NetRoute -AddressFamily IPv4 -DestinationPrefix '0.0.0.0/0'|Select-Object "
    "@{n='ifIndex';e={[int]$_.ifIndex}},NextHop,@{n='RouteMetric';e={[int]$_.RouteMetric+[int]$_.InterfaceMetric}});"
    "@{physical=$p;addresses=$a;routes=$r}|ConvertTo-Json -Compress -Depth 4"
)


def _output(argv: list[str], encoding: str = "utf-8") -> str:
    """命令的全部输出；命令不存在、超时、失败时空串。Windows 上不弹控制台窗口。"""
    kwargs = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = _CREATE_NO_WINDOW
    try:
        done = subprocess.run(argv, capture_output=True, text=True, encoding=encoding, errors="replace",
                              timeout=COMMAND_TIMEOUT_S, stdin=subprocess.DEVNULL, **kwargs)
    except (OSError, subprocess.SubprocessError, LookupError):
        return ""
    return done.stdout if done.returncode == 0 else ""


def _read(path: str) -> str:
    try:
        with open(path, encoding="ascii", errors="replace") as handle:
            return handle.read()
    except OSError:
        return ""


def _console_encoding() -> str:
    """ipconfig 按控制台的 OEM 代码页输出（中文 Windows 是 936）。"oem" 编解码器只在 Windows 上有。"""
    import codecs
    try:
        codecs.lookup("oem")
        return "oem"
    except LookupError:
        return "utf-8"


def interfaces_and_routes(platform_name: str | None = None, run=_output, read=_read) \
        -> tuple[list[Interface], list[Route]]:
    """这台电脑的 IPv4 地址和默认路由。问不到就是空表。"""
    platform_name = platform_name or sys.platform
    if platform_name == "darwin" or platform_name.startswith(("freebsd", "openbsd", "netbsd")):
        return parse_ifconfig(run(["ifconfig"])), parse_netstat_routes(run(["netstat", "-rn", "-f", "inet"]))
    if platform_name == "win32":
        found, routes = parse_windows_json(run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                                                WINDOWS_SCRIPT]))
        if found:
            return found, routes
        return parse_ipconfig(run(["ipconfig"], _console_encoding()))
    found = parse_ip_addr(run(["ip", "-4", "-o", "addr", "show"]))
    if found:
        return found, parse_ip_route(run(["ip", "-4", "route", "show", "default"]))
    routes, links = parse_proc_route(read("/proc/net/route"))
    return parse_hostname_i(run(["hostname", "-I"]), links), routes


def probe_address() -> str:
    """UDP 探测：内核为远端地址选的出口网卡的本端地址（不发包）。落在排除的网段里（TUN、Tailscale）
    或回环时空串。"""
    for address, port in _PROBE_ADDRESSES:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.settimeout(0.5)
            sock.connect((address, port))
            found = sock.getsockname()[0]
        except OSError:
            continue
        finally:
            sock.close()
        if found and not excluded(found):
            return found
    return ""


def lan_info(platform_name: str | None = None, run=_output, read=_read, probe=probe_address) -> LanInfo:
    """列网卡、挑地址；列不出时用探测。local 是能用的那些地址（列不出时 None）。"""
    try:
        interfaces, routes = interfaces_and_routes(platform_name, run, read)
    except Exception:       # noqa: BLE001 —— 解析出意外就当作列不出，退回探测
        interfaces, routes = [], []
    ranked = candidates(interfaces, routes)
    if ranked:
        return LanInfo(ranked[0][1].address, frozenset(iface.address for _, iface in ranked))
    local = None
    if interfaces:
        # 列出了网卡却一个能用的都没有（只有隧道、虚拟网卡）：探测的结果也只能是它们中的一个。
        local = frozenset()
    found = probe()
    if found and local is not None and any(found == iface.address for iface in interfaces):
        return LanInfo("", local)
    return LanInfo(found, local if local is None else frozenset({found}) if found else local)


def lan_address() -> str | None:
    """同一网络中的设备访问本机所用的 IPv4 地址；找不到时返回 None。

    不用回环地址代替：127.0.0.1 只有本机能访问，填到设备上会表现为服务端故障。也不用代理 TUN、
    Tailscale、虚拟机网卡的地址（见上面的说明）。
    """
    return lan_info().address or None


def _run(command: list[str]) -> str | None:
    """返回命令输出的第一行，失败返回 None，不抛异常。

    显式指定 UTF-8：机器名可能含非 ASCII 字符，按平台默认编码读取可能在读取线程中抛异常。
    """
    try:
        done = subprocess.run(command, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    line = done.stdout.strip().splitlines()
    return line[0].strip() if line else None


def local_name() -> str | None:
    """本机的 `.local` 名称，无法确定时返回 None。

    * macOS：`scutil` 给出 Bonjour 名称。
    * Linux/BSD：`avahi-resolve` 向运行中的 mDNS 服务询问，没有 avahi 就没有名称。
    * Windows：取环境变量中的计算机名，系统以 `<名称>.local` 发布 mDNS。

    只返回带 `.local` 后缀的名称；裸主机名在家用网络中无法解析。
    """
    system = platform.system()

    if system == "Darwin":
        name = _run(["scutil", "--get", "LocalHostName"])
        return f"{name}.local" if name else None

    if system == "Windows":
        import os
        name = os.environ.get("COMPUTERNAME")
        return f"{name}.local" if name else None

    # Linux、BSD 及其他运行 mDNS 服务的系统。
    name = _run(["avahi-resolve", "--address", "-n", socket.gethostname()])
    if name:
        return name.rstrip(".") + ".local" if not name.endswith(".local") else name
    return None


def describe(address: str | None = None, port: int = 8096) -> list[str]:
    """启动时打印的地址提示行。

    `address` 为作为备用显示的局域网地址；调用方没找到时为 None（例如监听所有网卡且没有私有地址）。
    """
    name = local_name()

    # 不打印监听地址：设备填的是名称，数字地址只在名称失效时作为备用出现。
    lines: list[str] = []
    if not address:
        if name:
            lines.append("设备上要填的地址：")
            lines.append(f"    {name}:{port}")
            lines.append(f"（连不上就换成这台电脑的局域网地址:{port}）")
        else:
            lines.append(f"没有识别出这台电脑的局域网地址。设备上填它的局域网地址:{port}。")
        lines.append("")
        return lines

    # 绑定到回环地址时局域网设备无法连接，下面的填写建议不适用，改为说明原因。
    if address.startswith("127."):
        lines.append("这是一个本机地址（127 开头），只有这台电脑自己能访问，")
        lines.append("局域网里的设备连接不上。")
        return lines

    # 只给一个待填的值，下面至多一行备用说明。
    if name:
        lines.append("设备上要填的地址：")
        lines.append(f"    {name}:{port}")
        lines.append(f"（连不上就换成 {address}:{port}）")
    else:
        lines.append("设备上要填的地址：")
        lines.append(f"    {address}:{port}")
        lines.append("（读不到这台电脑的名字；路由器换 IP 后要重新填一次）")
    # 末尾空行，把后续输出与填写说明隔开。
    lines.append("")
    return lines
