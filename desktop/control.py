"""Client for the core's local control API (design 5.13, "The contract as implemented (P1c)").

The core writes `<data>/run/control.json` = {"api": 1, "port", "token", "pid",
"version", "data_dir", "logs_dir", ...} (mode 0600) and serves
`http://127.0.0.1:<port>/local/v1/...`; every request carries
`Authorization: Bearer <token>`. The file is re-read on every request: a
restarted core has a new port and token, and a file left behind by a crash names
a port nobody answers on (that is "unavailable", not an error).

What the tray reads from `GET /local/v1/status` is parsed in one place,
`Status.from_json`, so a change in the contract is a change here only. Fields a
core does not report fall back to "unknown", never to an exception.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

PREFIX = "/local/v1"
DEFAULT_TIMEOUT_S = 5.0


class ControlUnavailable(Exception):
    """No control file yet, or nothing answering on its port (core starting or gone)."""


class ControlError(Exception):
    """The core answered {"ok": false, "error": <code>, "message": <Chinese text>}."""

    def __init__(self, status: int, code: str = "", message: str = ""):
        super().__init__(message or (f"HTTP {status} {code}".strip()))
        self.status = status
        self.code = code
        self.message = message


def read_control_file(path: Path) -> dict | None:
    """The parsed control.json, or None when missing, partial or malformed."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    port, token = data.get("port"), data.get("token")
    if not isinstance(port, int) or isinstance(port, bool) or not 0 < port < 65536:
        return None
    if not isinstance(token, str) or len(token) < 16:
        return None
    return data


class ControlClient:
    def __init__(self, control_file: Path, timeout: float = DEFAULT_TIMEOUT_S):
        self.control_file = Path(control_file)
        self.timeout = timeout
        # No proxies: a system proxy setting must never see the token, and some
        # proxy configurations do not exempt 127.0.0.1.
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def info(self) -> dict | None:
        return read_control_file(self.control_file)

    def request(self, method: str, path: str, body: dict | None = None, timeout: float | None = None) -> dict:
        info = self.info()
        if info is None:
            raise ControlUnavailable("control.json not there yet")
        # Host is 127.0.0.1:<port> (the core refuses other Host values) and no
        # Origin header is ever sent (the core refuses any).
        url = f"http://127.0.0.1:{info['port']}{PREFIX}{path}"
        data = json.dumps(body or {}).encode("utf-8") if method != "GET" else None
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("Authorization", f"Bearer {info['token']}")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with self._opener.open(request, timeout=timeout or self.timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as error:
            code, message = "", ""
            try:
                answer = json.loads(error.read() or b"{}")
                code, message = str(answer.get("error", ""))[:60], str(answer.get("message", ""))[:200]
            except (ValueError, AttributeError, OSError):
                pass
            raise ControlError(error.code, code, message) from None
        except (urllib.error.URLError, OSError, TimeoutError) as error:
            raise ControlUnavailable(type(error).__name__) from None
        if not raw:
            return {}
        try:
            answer = json.loads(raw)
        except ValueError:
            raise ControlError(200, "not_json") from None
        if not isinstance(answer, dict):
            return {"value": answer}
        if answer.get("ok") is False:
            raise ControlError(200, str(answer.get("error", "")), str(answer.get("message", "")))
        return answer

    # The endpoints the tray uses, one line each so the contract is visible.
    def status(self) -> "Status":
        return Status.from_json(self.request("GET", "/status"))

    def console_link(self) -> str:
        """Setup page (no admin yet) or one-time admin sign-in, both for the local browser."""
        return _url(self.request("POST", "/console-link"))

    def phone_qr(self) -> dict:
        return self.request("POST", "/phone-qr")

    def set_power(self, keep_awake: str) -> dict:
        return self.request("POST", "/power", {"keep_awake": keep_awake})

    def set_autostart(self, enabled: bool) -> dict:
        return self.request("POST", "/autostart", {"enabled": bool(enabled)})

    def fetch_ffmpeg(self) -> dict:
        return self.request("POST", "/ffmpeg/fetch")

    def trust_network(self, network_id: str, trusted: bool) -> dict:
        # Not in the P1c contract yet: proposed for 5.11 (see docs/desktop/).
        return self.request("POST", "/network/trust", {"id": network_id, "trusted": bool(trusted)})

    def diagnostics(self) -> dict:
        return self.request("POST", "/diagnostics", timeout=120)

    def restart(self) -> dict:
        return self.request("POST", "/restart")

    def stop(self) -> dict:
        return self.request("POST", "/stop")


def _url(answer: dict) -> str:
    url = answer.get("url") or answer.get("link") or ""
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        raise ControlError(200, "no_url")
    return url


def origin(url: str) -> str:
    """http://localhost:8080/tv/ -> http://localhost:8080"""
    parts = urllib.parse.urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}" if parts.scheme and parts.netloc else ""


# ---------------------------------------------------------------- status model

@dataclass(frozen=True)
class Device:
    name: str
    model: str = ""
    online: bool = False
    watching: bool = False
    channel: str = ""
    listening: bool = False         # a phone is listening to it ("用手机听")


@dataclass(frozen=True)
class Ffmpeg:
    state: str = ""                 # "ok" | "downloading" | "missing" | "failed" | "" (not reported)
    progress: float | None = None   # 0..1 while downloading, when the size is known
    error: str = ""


@dataclass(frozen=True)
class Network:
    """Trusted networks (5.11): `status.network`, and `POST /network/trust` to answer."""
    id: str = ""                    # router identity (hash), "" = unknown
    trusted: bool | None = None     # None = not decided yet ("ask")
    profile: str = ""               # Windows: "private" | "public" | "domain" | ""
    lan_open: bool | None = None    # whether the LAN ports are open right now


@dataclass(frozen=True)
class Problem:
    code: str
    message: str


# The core's names -> the tray's (P1c uses ready/fetching; the tray's menu says ok/downloading).
_FFMPEG_STATES = {"ready": "ok", "ok": "ok", "fetching": "downloading", "downloading": "downloading",
                  "missing": "missing", "failed": "failed"}


@dataclass(frozen=True)
class Status:
    version: str = ""
    state: str = "running"          # "starting" | "fetching_ffmpeg" | "running" | "stopping" | "error"
    lan_url: str = ""
    local_url: str = ""
    lan_port: int = 0
    setup_pending: bool = False     # no admin yet: console-link answers with the setup page
    devices: tuple[Device, ...] = ()
    watching_count: int | None = None   # the core's own count, when it gives one
    listening: int = 0              # devices a phone is listening to (keeps the computer awake too)
    ffmpeg: Ffmpeg = field(default_factory=Ffmpeg)
    problems: tuple[Problem, ...] = ()
    autostart: bool | None = None   # settings.autostart, None = core does not say
    keep_awake: str = ""            # settings.keep_awake, "" = core does not say
    logs_dir: str = ""
    power_held_by_core: bool = False
    network: Network | None = None  # None = core does not do trusted networks (yet)

    @property
    def watching(self) -> int:
        if self.watching_count is not None:
            return self.watching_count
        return sum(1 for d in self.devices if d.watching)

    @property
    def console_base(self) -> str:
        """Origin of the local pages, e.g. http://localhost:8080."""
        return origin(self.local_url) or (f"http://localhost:{self.lan_port}" if self.lan_port else "")

    @classmethod
    def from_json(cls, raw: dict) -> "Status":
        raw = raw if isinstance(raw, dict) else {}
        devices = tuple(_device(d) for d in _list(raw.get("devices")) if isinstance(d, dict))
        setup = raw.get("setup_pending")
        ff = raw.get("ffmpeg")
        if isinstance(ff, dict):
            progress = ff.get("progress")
            if not _number(progress):
                done, total = ff.get("done"), ff.get("total")
                progress = done / total if _number(done) and _number(total) and total > 0 else None
            ffmpeg = Ffmpeg(_FFMPEG_STATES.get(_str(ff.get("state")), ""),
                            max(0.0, min(1.0, float(progress))) if progress is not None else None,
                            _str(ff.get("error")))
        else:
            ffmpeg = Ffmpeg()
        problems = []
        for p in _list(raw.get("problems")):
            if isinstance(p, dict) and (p.get("message") or p.get("code")):
                problems.append(Problem(_str(p.get("code")), _str(p.get("message")) or _str(p.get("code"))))
            elif isinstance(p, str) and p:
                problems.append(Problem("", p))
        settings = raw.get("settings") if isinstance(raw.get("settings"), dict) else {}
        autostart = settings.get("autostart")
        power = raw.get("power") if isinstance(raw.get("power"), dict) else {}
        net = raw.get("network")
        network = None
        if isinstance(net, dict):
            trusted, lan_open = net.get("trusted"), net.get("lan_open")
            network = Network(_str(net.get("id")), trusted if isinstance(trusted, bool) else None,
                              _str(net.get("profile")).lower(), lan_open if isinstance(lan_open, bool) else None)
        ports = raw.get("ports") if isinstance(raw.get("ports"), dict) else {}
        lan_port = ports.get("lan")
        watching = raw.get("watching")
        listening = raw.get("listening")
        return cls(
            version=_str(raw.get("version")),
            state=_str(raw.get("state")) or "running",
            lan_url=_str(raw.get("lan_url")),
            local_url=_str(raw.get("local_url")),
            lan_port=lan_port if _int(lan_port) and 0 < lan_port < 65536 else 0,
            setup_pending=setup is True,
            devices=devices,
            watching_count=watching if _int(watching) and watching >= 0 else None,
            listening=listening if _int(listening) and listening >= 0 else 0,
            ffmpeg=ffmpeg,
            problems=tuple(problems),
            autostart=autostart if isinstance(autostart, bool) else None,
            keep_awake=_str(settings.get("keep_awake")),
            logs_dir=_str(raw.get("logs_dir")),
            power_held_by_core=power.get("managed_by") == "core",
            network=network,
        )


def _str(value) -> str:
    return value if isinstance(value, str) else ""


def _int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _list(value) -> list:
    return value if isinstance(value, list) else []


def _device(raw: dict) -> Device:
    name = _str(raw.get("name")) or _str(raw.get("id")) or "小电视"
    online = raw.get("online") is True
    channel = _str(raw.get("channel"))
    watching = raw.get("watching")
    if not isinstance(watching, bool):
        # P1c: a device is listed while connected; online with a channel = watching.
        watching = online and bool(channel)
    return Device(name=name, model=_str(raw.get("model")), online=online, watching=watching, channel=channel,
                  listening=online and raw.get("listening") is True)
