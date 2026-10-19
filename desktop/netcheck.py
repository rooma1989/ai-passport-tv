"""Which network the computer is on (design 5.11).

Two questions:

* **Is the home Wi-Fi marked Public on Windows?** Then the installer's firewall
  rule (Private profile only) does not apply and devices cannot connect. The
  tray says how to switch it to Private.
* **Is this the home network?** A laptop in a cafe should not expose ports
  8080/8097/8098 there. The home network is remembered by the router's identity
  (the default gateway's MAC address, hashed) once the user confirms it; on other
  networks the core binds its LAN ports to 127.0.0.1. The core enforces it; the
  tray asks the question and passes the answer through the control API.

Everything here is stdlib and side-effect free apart from running `route`/`arp`
(or PowerShell on Windows), so the core can use the same functions. Parsing is
separate from running, for the tests.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

_MAC = re.compile(r"([0-9a-fA-F]{1,2}(?:[:-][0-9a-fA-F]{1,2}){5})")
_IPV4 = re.compile(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b")
CREATE_NO_WINDOW = 0x08000000
TIMEOUT_S = 8

# NetworkCategory of Get-NetConnectionProfile (the enum serialises as a number
# in Windows PowerShell 5.1 and as a name in PowerShell 7).
CATEGORIES = {0: "public", 1: "private", 2: "domain", "public": "public", "private": "private",
              "domainauthenticated": "domain"}


def _run(argv: list[str]) -> str:
    kwargs = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = CREATE_NO_WINDOW   # no console flashing up from the tray
    try:
        result = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                timeout=TIMEOUT_S, **kwargs)
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout if result.returncode == 0 else ""


def normalise_mac(text: str) -> str:
    """0:11:2:33:44:5 or 00-11-02-33-44-05 -> 00:11:02:33:44:05; "" if not a usable MAC."""
    parts = re.split(r"[:-]", text.strip())
    if len(parts) != 6:
        return ""
    try:
        values = [int(p, 16) for p in parts]
    except ValueError:
        return ""
    if all(v == 0 for v in values) or all(v == 0xFF for v in values) or values[0] & 1:
        return ""   # incomplete ARP entry, broadcast, multicast
    return ":".join(f"{v:02x}" for v in values)


def identity_of(mac: str) -> str:
    """A short, stable id for a router, without writing its MAC into configs and logs."""
    mac = normalise_mac(mac)
    if not mac:
        return ""
    return hashlib.sha256(f"xiaozhi-tv-gateway:{mac}".encode("ascii")).hexdigest()[:16]


# --- parsers (one per OS and command) -------------------------------------------------

def parse_macos_route(text: str) -> str:
    match = re.search(r"^\s*gateway:\s*(\S+)", text, re.MULTILINE)
    return match.group(1) if match and _IPV4.fullmatch(match.group(1)) else ""


def parse_arp(text: str, ip: str) -> str:
    """macOS/Linux `arp -n <ip>` and Windows `arp -a <ip>`: the MAC on the line naming ip."""
    for line in text.splitlines():
        if re.search(rf"(?<![\d.]){re.escape(ip)}(?![\d.])", line):
            match = _MAC.search(line)
            if match:
                mac = normalise_mac(match.group(1))
                if mac:
                    return mac
    return ""


def parse_linux_route(text: str) -> str:
    """/proc/net/route: the default route's gateway, little-endian hex."""
    best = None
    for line in text.splitlines()[1:]:
        fields = line.split()
        if len(fields) < 8 or fields[1] != "00000000" or fields[7] != "00000000":
            continue
        try:
            raw, metric = int(fields[2], 16), int(fields[6])
        except ValueError:
            continue
        if raw and (best is None or metric < best[0]):
            best = (metric, raw)
    if best is None:
        return ""
    raw = best[1]
    return ".".join(str((raw >> shift) & 0xFF) for shift in (0, 8, 16, 24))


def parse_linux_arp(text: str, ip: str) -> str:
    for line in text.splitlines()[1:]:
        fields = line.split()
        if len(fields) >= 4 and fields[0] == ip:
            return normalise_mac(fields[3])
    return ""


def parse_windows_route(text: str) -> str:
    """`route print -4 0.0.0.0`: the default route with the lowest metric."""
    best = None
    for line in text.splitlines():
        fields = line.split()
        if len(fields) >= 5 and fields[0] == "0.0.0.0" and fields[1] == "0.0.0.0" and _IPV4.fullmatch(fields[2]):
            try:
                metric = int(fields[-1])
            except ValueError:
                continue
            if best is None or metric < best[0]:
                best = (metric, fields[2])
    return best[1] if best else ""


def parse_windows_profiles(text: str) -> list[dict]:
    """`Get-NetConnectionProfile | ConvertTo-Json`: [{"name", "category"}]."""
    try:
        data = json.loads(text) if text.strip() else []
    except ValueError:
        return []
    if isinstance(data, dict):
        data = [data]
    profiles = []
    for item in data if isinstance(data, list) else []:
        if not isinstance(item, dict):
            continue
        raw = item.get("NetworkCategory")
        key = raw.lower() if isinstance(raw, str) else raw
        profiles.append({"name": str(item.get("Name") or item.get("InterfaceAlias") or ""),
                         "category": CATEGORIES.get(key, "")})
    return profiles


# --- the questions ---------------------------------------------------------------------

def default_gateway(platform_name: str | None = None, run=_run) -> str:
    platform_name = platform_name or sys.platform
    if platform_name == "darwin":
        return parse_macos_route(run(["route", "-n", "get", "default"]))
    if platform_name == "win32":
        return parse_windows_route(run(["route", "print", "-4", "0.0.0.0"]))
    try:
        return parse_linux_route(Path("/proc/net/route").read_text(encoding="ascii"))
    except OSError:
        return ""


def gateway_mac(ip: str, platform_name: str | None = None, run=_run) -> str:
    if not ip:
        return ""
    platform_name = platform_name or sys.platform
    if platform_name == "darwin":
        return parse_arp(run(["arp", "-n", ip]), ip)
    if platform_name == "win32":
        return parse_arp(run(["arp", "-a", ip]), ip)
    try:
        return parse_linux_arp(Path("/proc/net/arp").read_text(encoding="ascii"), ip)
    except OSError:
        return ""


def network_identity(platform_name: str | None = None, run=_run) -> str:
    """The current home-router id, or "" when offline or unknown (e.g. a VPN without ARP)."""
    return identity_of(gateway_mac(default_gateway(platform_name, run), platform_name, run))


def windows_profiles(run=_run) -> list[dict]:
    return parse_windows_profiles(run([
        "powershell", "-NoProfile", "-NonInteractive", "-Command",
        "Get-NetConnectionProfile | Select-Object Name,InterfaceAlias,NetworkCategory | ConvertTo-Json -Compress"]))


def public_profile_name(profiles: list[dict]) -> str:
    """The name of a connected network marked Public, or "" (Windows only)."""
    for profile in profiles:
        if profile.get("category") == "public":
            return profile.get("name") or "当前网络"
    return ""


def lan_decision(trusted: list[str], current: str) -> str:
    """What the core should do with the LAN ports on this network.

    "open": a trusted network, or nothing decided yet (the first run is almost
    always at home, and a closed first run would look broken);
    "ask": open, but the tray asks "is this your home network?";
    "closed": the user has a home network and this is not it -> 127.0.0.1 only.
    """
    if current and current in trusted:
        return "open"
    if not trusted:
        return "ask"
    return "closed"
