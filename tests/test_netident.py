"""Which address devices and phones should use (server/netident.py), on every OS.

The commands are never run here: each test feeds the parsers the output a real
machine prints (macOS with a proxy in TUN mode and a WireGuard VPN, Linux with
Tailscale and Docker, Windows with Hyper-V and a wintun TUN, two network cards),
and checks the physical LAN address wins. Also the rule for an address stored at
first-run setup (server/selfhost/__main__.py `_addresses`) and the watcher that
notices a new DHCP address while the PC client runs (server/selfhost/network.py).
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server import netident
from server.selfhost import config as config_module
from server.selfhost.__main__ import _addresses
from server.selfhost.network import AddressWatch

# ---- macOS: Surge/Clash/Stash in TUN mode (split routes 1/8, 2/7 ... via utun1024) and a WireGuard VPN ----

MAC_IFCONFIG = """\
lo0: flags=8049<UP,LOOPBACK,RUNNING,MULTICAST> mtu 16384
\toptions=1203<RXCSUM,TXCSUM,TXSTATUS,SW_TIMESTAMP>
\tinet 127.0.0.1 netmask 0xff000000
\tinet6 ::1 prefixlen 128
gif0: flags=8010<POINTOPOINT,MULTICAST> mtu 1280
anpi0: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
en1: flags=8963<UP,BROADCAST,SMART,RUNNING,PROMISC,SIMPLEX,MULTICAST> mtu 1500
bridge0: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
\tmember: en1 flags=3<LEARNING,DISCOVER>
\tstatus: inactive
en0: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
\tether 3c:22:fb:00:00:01
\tinet6 fe80::1c%en0 prefixlen 64 secured scopeid 0xe
\tinet 192.168.1.118 netmask 0xffffff00 broadcast 192.168.1.255
\tstatus: active
bridge100: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
\tinet 192.168.2.1 netmask 0xffffff00 broadcast 192.168.2.255
awdl0: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
utun0: flags=8051<UP,POINTOPOINT,RUNNING,MULTICAST> mtu 1380
utun1024: flags=8051<UP,POINTOPOINT,RUNNING,MULTICAST> mtu 1500
\tinet 198.18.0.1 --> 198.18.0.1 netmask 0xfffffffc
utun8: flags=8051<UP,POINTOPOINT,RUNNING,MULTICAST> mtu 1420
\tinet 10.66.0.101 --> 10.66.0.101 netmask 0xffffffff
"""

MAC_NETSTAT = """\
Routing tables

Internet:
Destination        Gateway            Flags               Netif Expire
default            192.168.1.1        UGScg                 en0
default            link#27            UCSIg               utun8
1                  198.18.0.1         UGSc             utun1024
2/7                198.18.0.1         UGSc             utun1024
128.0/1            198.18.0.1         UGSc             utun1024
10.66/24           link#27            UCS                 utun8
127                127.0.0.1          UCS                   lo0
192.168.1          link#14            UCS                   en0      !
192.168.1.1        58:41:20:00:00:01  UHLWIir               en0   1187
"""

# A proxy that replaced the default route itself: the TUN's default comes first,
# the Wi-Fi's (scoped, flag I) is still listed.
MAC_NETSTAT_TUN_FIRST = """\
Internet:
Destination        Gateway            Flags               Netif Expire
default            198.18.0.1         UGSc             utun1024
default            192.168.1.1        UGScIg                en0
"""

# ---- Linux: Tailscale, Docker (docker0 and a compose bridge), libvirt, and the Wi-Fi ----

LINUX_IP_ADDR = """\
1: lo    inet 127.0.0.1/8 scope host lo\\       valid_lft forever preferred_lft forever
2: enp3s0    inet 192.168.1.20/24 brd 192.168.1.255 scope global dynamic noprefixroute enp3s0\\       valid_lft 85000sec preferred_lft 85000sec
3: docker0    inet 172.17.0.1/16 brd 172.17.255.255 scope global docker0\\       valid_lft forever preferred_lft forever
4: br-1a2b3c4d5e6f    inet 172.18.0.1/16 brd 172.18.255.255 scope global br-1a2b3c4d5e6f\\       valid_lft forever preferred_lft forever
5: virbr0    inet 192.168.122.1/24 brd 192.168.122.255 scope global virbr0\\       valid_lft forever preferred_lft forever
6: tailscale0    inet 100.101.102.103/32 scope global tailscale0\\       valid_lft forever preferred_lft forever
7: veth12ab@if6    inet 169.254.10.2/16 brd 169.254.255.255 scope global veth12ab\\       valid_lft forever preferred_lft forever
8: wg0    inet 10.13.13.2/24 scope global wg0\\       valid_lft forever preferred_lft forever
"""
LINUX_IP_ROUTE = "default via 192.168.1.1 dev enp3s0 proto dhcp src 192.168.1.20 metric 100\n"

LINUX_TWO_NICS_ADDR = """\
2: eth0    inet 192.168.1.5/24 brd 192.168.1.255 scope global eth0\\       valid_lft forever preferred_lft forever
3: wlan0    inet 192.168.1.23/24 brd 192.168.1.255 scope global dynamic wlan0\\       valid_lft 3000sec preferred_lft 3000sec
4: eth1    inet 10.0.0.2/24 brd 10.0.0.255 scope global eth1\\       valid_lft forever preferred_lft forever
"""
LINUX_TWO_NICS_ROUTE = """\
default via 192.168.1.1 dev wlan0 proto dhcp metric 600
default via 192.168.1.1 dev eth0 proto dhcp metric 100
"""

# No `ip` (a minimal system): /proc/net/route and `hostname -I`.
LINUX_PROC_ROUTE = """\
Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT
eth0\t00000000\t0101A8C0\t0003\t0\t0\t100\t00000000\t0\t0\t0
eth0\t0001A8C0\t00000000\t0001\t0\t0\t100\t00FFFFFF\t0\t0\t0
docker0\t000011AC\t00000000\t0001\t0\t0\t0\t0000FFFF\t0\t0\t0
"""
LINUX_HOSTNAME_I = "192.168.1.5 172.17.0.1 100.101.102.103 fd7a:115c:a1e0::1\n"

# ---- Windows: Get-NetAdapter / Get-NetIPAddress / Get-NetRoute (WINDOWS_SCRIPT) ----

WINDOWS_JSON = json.dumps({
    "physical": [12, 7],
    "addresses": [
        {"ifIndex": 1, "InterfaceAlias": "Loopback Pseudo-Interface 1", "IPAddress": "127.0.0.1", "PrefixLength": 8},
        {"ifIndex": 12, "InterfaceAlias": "WLAN", "IPAddress": "192.168.1.23", "PrefixLength": 24},
        {"ifIndex": 7, "InterfaceAlias": "以太网", "IPAddress": "169.254.33.4", "PrefixLength": 16},
        {"ifIndex": 30, "InterfaceAlias": "vEthernet (Default Switch)", "IPAddress": "172.29.80.1", "PrefixLength": 20},
        {"ifIndex": 41, "InterfaceAlias": "Clash", "IPAddress": "198.18.0.1", "PrefixLength": 30},
        {"ifIndex": 42, "InterfaceAlias": "singbox_tun", "IPAddress": "172.19.0.1", "PrefixLength": 30},
        {"ifIndex": 50, "InterfaceAlias": "Tailscale", "IPAddress": "100.88.1.2", "PrefixLength": 32},
    ],
    "routes": [
        {"ifIndex": 42, "NextHop": "172.19.0.2", "RouteMetric": 1},
        {"ifIndex": 41, "NextHop": "0.0.0.0", "RouteMetric": 0},
        {"ifIndex": 12, "NextHop": "192.168.1.1", "RouteMetric": 35},
    ],
}, ensure_ascii=False)

# Hyper-V's external switch: the LAN address moved to the virtual adapter, the
# physical card has none. It carries the gateway, so it is used.
WINDOWS_EXTERNAL_SWITCH = json.dumps({
    "physical": [12],
    "addresses": [{"ifIndex": 33, "InterfaceAlias": "vEthernet (External)", "IPAddress": "192.168.1.40",
                   "PrefixLength": 24},
                  {"ifIndex": 30, "InterfaceAlias": "vEthernet (Default Switch)", "IPAddress": "172.29.80.1",
                   "PrefixLength": 20}],
    "routes": {"ifIndex": 33, "NextHop": "192.168.1.1", "RouteMetric": 25},     # one route: not a list
})

WINDOWS_IPCONFIG_EN = """\

Windows IP Configuration


Ethernet adapter vEthernet (Default Switch):

   Connection-specific DNS Suffix  . :
   Link-local IPv6 Address . . . . . : fe80::1%30
   IPv4 Address. . . . . . . . . . . : 172.29.80.1
   Subnet Mask . . . . . . . . . . . : 255.255.240.0
   Default Gateway . . . . . . . . . :

Wireless LAN adapter Wi-Fi:

   Connection-specific DNS Suffix  . : lan
   Link-local IPv6 Address . . . . . : fe80::2%12
   IPv4 Address. . . . . . . . . . . : 192.168.1.23
   Subnet Mask . . . . . . . . . . . : 255.255.255.0
   Default Gateway . . . . . . . . . : fe80::1%12
                                       192.168.1.1

Unknown adapter Clash:

   IPv4 Address. . . . . . . . . . . : 198.18.0.1
   Subnet Mask . . . . . . . . . . . : 255.255.255.252
   Default Gateway . . . . . . . . . : 0.0.0.0
"""

WINDOWS_IPCONFIG_ZH = """\

Windows IP 配置


以太网适配器 以太网:

   媒体状态  . . . . . . . . . . . . : 媒体已断开连接
   连接特定的 DNS 后缀 . . . . . . . :

无线局域网适配器 WLAN:

   连接特定的 DNS 后缀 . . . . . . . :
   本地链接 IPv6 地址. . . . . . . . : fe80::2%12
   IPv4 地址 . . . . . . . . . . . . : 192.168.31.8
   子网掩码  . . . . . . . . . . . . : 255.255.255.0
   默认网关. . . . . . . . . . . . . : 192.168.31.1

以太网适配器 VMware Network Adapter VMnet8:

   IPv4 地址 . . . . . . . . . . . . : 192.168.80.1
   子网掩码  . . . . . . . . . . . . : 255.255.255.0
   默认网关. . . . . . . . . . . . . :
"""


def runner(outputs: dict):
    """A fake `run`: the canned output for a command (matched by its first words), "" otherwise."""
    def run(argv, *_args):
        for key, text in outputs.items():
            if tuple(argv[:len(key)]) == key:
                return text
        return ""
    return run


def no_probe():
    raise AssertionError("the UDP probe must not be needed")


def detect(platform_name: str, outputs: dict, read=lambda _path: "", probe=no_probe) -> netident.LanInfo:
    return netident.lan_info(platform_name, runner(outputs), read, probe)


class MacTests(unittest.TestCase):
    def test_tun_proxy_and_wireguard_do_not_win(self):
        info = detect("darwin", {("ifconfig",): MAC_IFCONFIG, ("netstat",): MAC_NETSTAT})
        self.assertEqual(info.address, "192.168.1.118")
        self.assertEqual(info.local, frozenset({"192.168.1.118"}),
                         "the TUN, the VPN and Internet Sharing's bridge are not the home network")

    def test_the_wifi_route_counts_even_when_the_tun_route_is_first(self):
        info = detect("darwin", {("ifconfig",): MAC_IFCONFIG, ("netstat",): MAC_NETSTAT_TUN_FIRST})
        self.assertEqual(info.address, "192.168.1.118")

    def test_parsing(self):
        found = {i.name: i for i in netident.parse_ifconfig(MAC_IFCONFIG)}
        self.assertEqual((found["en0"].address, found["en0"].prefix, found["en0"].pointopoint),
                         ("192.168.1.118", 24, False))
        self.assertTrue(found["utun1024"].pointopoint)
        self.assertNotIn("gif0", found)
        routes = netident.parse_netstat_routes(MAC_NETSTAT)
        self.assertEqual([(r.name, r.gateway) for r in routes], [("en0", "192.168.1.1"), ("utun8", "")])


class LinuxTests(unittest.TestCase):
    def test_tailscale_docker_libvirt_and_wireguard_do_not_win(self):
        info = detect("linux", {("ip", "-4", "-o", "addr"): LINUX_IP_ADDR, ("ip", "-4", "route"): LINUX_IP_ROUTE})
        self.assertEqual(info.address, "192.168.1.20")
        self.assertEqual(info.local, frozenset({"192.168.1.20"}))

    def test_two_cards_on_the_same_router_take_the_lower_metric(self):
        info = detect("linux", {("ip", "-4", "-o", "addr"): LINUX_TWO_NICS_ADDR,
                                ("ip", "-4", "route"): LINUX_TWO_NICS_ROUTE})
        self.assertEqual(info.address, "192.168.1.5")
        self.assertEqual(info.local, frozenset({"192.168.1.5", "192.168.1.23", "10.0.0.2"}),
                         "every card on a real network may carry a stored address")

    def test_a_card_without_the_router_loses_to_the_one_with_it(self):
        addr = ("2: eth1    inet 10.0.0.2/24 scope global eth1\\ x\n"
                "3: eth0    inet 192.168.1.5/24 scope global eth0\\ x\n")
        info = detect("linux", {("ip", "-4", "-o", "addr"): addr,
                                ("ip", "-4", "route"): "default via 192.168.1.1 dev eth0\n"})
        self.assertEqual(info.address, "192.168.1.5")

    def test_a_bridge_carrying_the_lan_is_fine(self):
        addr = ("3: br0    inet 192.168.1.5/24 scope global br0\\ x\n"
                "4: docker0    inet 172.17.0.1/16 scope global docker0\\ x\n")
        info = detect("linux", {("ip", "-4", "-o", "addr"): addr,
                                ("ip", "-4", "route"): "default via 192.168.1.1 dev br0 metric 425\n"})
        self.assertEqual(info.address, "192.168.1.5")

    def test_without_ip_proc_route_and_hostname_i(self):
        info = detect("linux", {("hostname", "-I"): LINUX_HOSTNAME_I},
                      read=lambda path: LINUX_PROC_ROUTE if path == "/proc/net/route" else "")
        self.assertEqual(info.address, "192.168.1.5")
        routes, links = netident.parse_proc_route(LINUX_PROC_ROUTE)
        self.assertEqual([(r.name, r.gateway) for r in routes], [("eth0", "192.168.1.1")])
        names = {i.address: i.name for i in netident.parse_hostname_i(LINUX_HOSTNAME_I, links)}
        self.assertEqual(names, {"192.168.1.5": "eth0", "172.17.0.1": "docker0", "100.101.102.103": ""})


class WindowsTests(unittest.TestCase):
    def test_wifi_wins_over_hyperv_clash_singbox_and_tailscale(self):
        info = detect("win32", {("powershell",): WINDOWS_JSON})
        self.assertEqual(info.address, "192.168.1.23")
        self.assertEqual(info.local, frozenset({"192.168.1.23"}))

    def test_hyperv_external_switch_carrying_the_lan(self):
        self.assertEqual(detect("win32", {("powershell",): WINDOWS_EXTERNAL_SWITCH}).address, "192.168.1.40")

    def test_ipconfig_when_powershell_fails_english_and_chinese(self):
        self.assertEqual(detect("win32", {("ipconfig",): WINDOWS_IPCONFIG_EN}).address, "192.168.1.23")
        self.assertEqual(detect("win32", {("ipconfig",): WINDOWS_IPCONFIG_ZH}).address, "192.168.31.8")
        found, routes = netident.parse_ipconfig(WINDOWS_IPCONFIG_EN)
        self.assertEqual([(i.name, i.address, i.prefix) for i in found],
                         [("Ethernet adapter vEthernet (Default Switch)", "172.29.80.1", 20),
                          ("Wireless LAN adapter Wi-Fi", "192.168.1.23", 24),
                          ("Unknown adapter Clash", "198.18.0.1", 30)])
        self.assertEqual([(r.name, r.gateway) for r in routes], [("Wireless LAN adapter Wi-Fi", "192.168.1.1")])

    def test_a_bom_and_garbage_are_survived(self):
        self.assertEqual(netident.parse_windows_json("﻿" + WINDOWS_JSON)[0][1].address, "192.168.1.23")
        for text in ("", "not json", "[1, 2]", '{"addresses": 5}', '{"addresses": [{"IPAddress": "x"}]}'):
            self.assertEqual(netident.parse_windows_json(text)[0], [], text)

    def test_the_script_asks_for_utf8_and_physical_adapters(self):
        self.assertIn("UTF8Encoding($false)", netident.WINDOWS_SCRIPT)
        self.assertIn("Get-NetAdapter -Physical", netident.WINDOWS_SCRIPT)


class FallbackTests(unittest.TestCase):
    def test_the_probe_is_the_last_resort_and_never_a_tun_address(self):
        self.assertEqual(detect("linux", {}, probe=lambda: "192.168.1.9").address, "192.168.1.9")
        self.assertIsNone(detect("linux", {}, probe=lambda: "192.168.1.9").local, "nothing listed: unknown")

        class FakeSocket:
            def __init__(self, *_a):
                pass

            def settimeout(self, _t):
                pass

            def connect(self, _address):
                pass

            def getsockname(self):
                return ("198.18.0.1", 5000)

            def close(self):
                pass

        with mock.patch.object(netident.socket, "socket", FakeSocket):
            self.assertEqual(netident.probe_address(), "")

    def test_only_tunnels_listed_means_no_address(self):
        ifconfig = ("utun3: flags=8051<UP,POINTOPOINT,RUNNING,MULTICAST> mtu 1380\n"
                    "\tinet 10.8.0.6 --> 10.8.0.5 netmask 0xffffffff\n")
        info = detect("darwin", {("ifconfig",): ifconfig}, probe=lambda: "10.8.0.6")
        self.assertEqual((info.address, info.local), ("", frozenset()))

    def test_a_parser_crash_falls_back_to_the_probe(self):
        with mock.patch.object(netident, "parse_ifconfig", side_effect=ValueError):
            self.assertEqual(detect("darwin", {}, probe=lambda: "192.168.1.9").address, "192.168.1.9")

    def test_excluded_ranges(self):
        for address in ("198.18.0.1", "198.19.255.1", "100.64.0.1", "100.127.1.1", "169.254.1.1", "127.0.0.1",
                        "0.0.0.0", "224.0.0.251"):
            self.assertTrue(netident.excluded(address), address)
        for address in ("192.168.1.118", "10.0.0.2", "172.16.0.1", "198.20.0.1", "100.128.0.1", "nas.local", ""):
            self.assertFalse(netident.excluded(address), address)


class StoredAddressTests(unittest.TestCase):
    """The rule for the DB meta `device_address` / `public_url` set at first-run setup."""

    LOCAL = frozenset({"192.168.1.118"})

    def test_stored_address_ok(self):
        ok = netident.stored_address_ok
        self.assertTrue(ok("192.168.1.118", self.LOCAL))
        self.assertTrue(ok("my-mac.local", self.LOCAL), "a name survives DHCP")
        self.assertFalse(ok("198.18.0.1", self.LOCAL), "the proxy's TUN address recorded by an older version")
        self.assertFalse(ok("198.18.0.1", None))
        self.assertFalse(ok("192.168.1.50", self.LOCAL), "no longer this computer's address")
        self.assertTrue(ok("192.168.1.50", None), "cannot tell (Docker): keep it")
        self.assertFalse(ok("", self.LOCAL))

    class Accounts:
        def __init__(self, **meta):
            self.values = meta

        def meta(self, key, default=""):
            return self.values.get(key, default)

    INFO = netident.LanInfo("192.168.1.118", frozenset({"192.168.1.118"}))

    def addresses(self, config_text="", in_container=False, info=None, **meta):
        config = config_module.parse(config_text)
        return _addresses(config, self.Accounts(**meta), in_container, info or self.INFO)

    def test_a_stored_tun_address_is_detected_again(self):
        self.assertEqual(self.addresses(device_address="198.18.0.1", public_url="http://198.18.0.1:8080"),
                         ("192.168.1.118", "192.168.1.118", "http://192.168.1.118:8080"))

    def test_a_stale_stored_address_is_detected_again(self):
        self.assertEqual(self.addresses(device_address="192.168.1.50", public_url="http://192.168.1.50:8080")[1:],
                         ("192.168.1.118", "http://192.168.1.118:8080"))

    def test_a_good_stored_address_and_a_name_are_kept(self):
        self.assertEqual(self.addresses(device_address="192.168.1.118")[1], "192.168.1.118")
        self.assertEqual(self.addresses(device_address="my-mac.local", public_url="http://my-mac.local:8080")[1:],
                         ("my-mac.local", "http://my-mac.local:8080"))

    def test_config_always_wins(self):
        # The owner's machine: device_address written in config.toml, a TUN address in the DB.
        text = '[server]\ndevice_address = "192.168.1.118"\n'
        other = netident.LanInfo("10.0.0.7", frozenset({"10.0.0.7"}))
        self.assertEqual(self.addresses(text, info=other, device_address="198.18.0.1")[1], "192.168.1.118")
        text = '[server]\ndevice_address = "192.168.9.9"\npublic_url = "https://tv.example.com"\n'
        self.assertEqual(self.addresses(text, device_address="198.18.0.1")[1:],
                         ("192.168.9.9", "https://tv.example.com"))

    def test_in_a_container_only_the_excluded_ranges_are_refused(self):
        self.assertEqual(self.addresses(in_container=True, info=netident.LanInfo(), device_address="192.168.1.50"),
                         ("", "192.168.1.50", ""))
        self.assertEqual(self.addresses(in_container=True, info=netident.LanInfo(), device_address="198.18.0.1"),
                         ("", "", ""))

    def test_detection_is_skipped_on_loopback(self):
        with mock.patch.object(netident, "lan_info", side_effect=AssertionError("not asked")):
            config = config_module.parse('[server]\nbind = "127.0.0.1"\n')
            self.assertEqual(_addresses(config, self.Accounts(), False), ("", "", ""))


class ThisMachineTests(unittest.TestCase):
    """The real commands on the machine running the tests. build-desktop-core.yml runs this file on macOS,
    Windows and Linux runners, where the WINDOWS_SCRIPT and the `ip`/`ifconfig` parsing meet real output; on CI
    (which always has a network card) the platform's own listing must find something."""

    def test_the_listing_works_here(self):
        if sys.platform == "win32":
            found, routes = netident.parse_windows_json(netident._output(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", netident.WINDOWS_SCRIPT]))
        else:
            found, routes = netident.interfaces_and_routes()
        info = netident.lan_info()
        self.assertFalse(netident.excluded(info.address), info)
        if os.environ.get("CI"):
            self.assertTrue(found, "no IPv4 address listed on this runner")
            self.assertTrue(routes, "no default route listed on this runner")
            self.assertTrue(info.address, "no LAN address chosen on this runner")
            print(f"\n{sys.platform}: {len(found)} addresses, {len(routes)} default routes, chose a "
                  f"{'private' if netident.is_rfc1918(info.address) else 'public'} address", file=sys.stderr)


class AddressWatchTests(unittest.TestCase):
    def make(self, readings):
        self.now = 0.0
        self.probe = "192.168.1.118"
        self.detected = list(readings)
        self.calls = []
        self.changes = []

        def detect():
            self.calls.append(self.now)
            return netident.LanInfo(self.detected.pop(0) if len(self.detected) > 1 else self.detected[0])

        watch = AddressWatch("192.168.1.118", detect=detect, probe=lambda: self.probe, signal=lambda: "home",
                             clock=lambda: self.now, log=lambda _m: None)
        watch.on_change = self.changes.append
        return watch

    def test_nothing_is_listed_while_the_cheap_check_is_unchanged(self):
        watch = self.make(["192.168.1.118"])
        for _ in range(10):
            self.now += 15
            self.assertFalse(watch.check())
        self.assertEqual(self.calls, [])

    def test_a_new_dhcp_address_counts_after_two_readings(self):
        watch = self.make(["192.168.1.130"])
        self.assertFalse(watch.check())         # remembers the cheap reading
        self.probe = "192.168.1.130"
        self.now += 15
        self.assertFalse(watch.check())         # first reading of the new address
        self.now += 15
        self.assertTrue(watch.check())
        self.assertEqual([info.address for info in self.changes], ["192.168.1.130"])
        self.assertEqual(watch.current, "192.168.1.130")
        self.now += 15
        self.assertFalse(watch.check())
        self.assertEqual(len(self.calls), 2)

    def test_a_flicker_does_not_count(self):
        watch = self.make(["", "192.168.1.118"])
        watch.check()
        self.probe = ""
        self.now += 15
        self.assertFalse(watch.check())         # Wi-Fi reconnecting: no address for a moment
        self.now += 15
        self.assertFalse(watch.check())         # back to the old one
        self.assertEqual(self.changes, [])

    def test_behind_a_tun_the_full_check_still_runs_every_few_minutes(self):
        watch = self.make(["192.168.1.130"])
        self.probe = ""                         # a TUN: the probe tells nothing
        watch.check()
        self.now += AddressWatch.FULL_S
        self.assertFalse(watch.check())
        self.now += 15
        self.assertTrue(watch.check())


if __name__ == "__main__":
    unittest.main()
