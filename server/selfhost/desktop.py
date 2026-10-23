"""电脑客户端模式（--desktop）：监督进程在 Docker、./run.sh --selfhost 之外多做的事。

设计见 docs/superpowers/specs/2026-10-19-pc-client-architecture-design.md 第 5.1、5.4、5.5、5.7、5.13、5.15 节
（阶段 P1b、P1c、P3a）。这里有：

- 日志：写到日志文件夹的 core.log（2 MB 一份、留 5 份），写之前去掉令牌、会话、设置码，IP 只留前三段（redact）。
- 端口：8080 被占时依次试 8090、18080、28080（pick_lan_port）；设备的 8097、8098 和内部端口不能换，被占时
  在状态里说是哪个程序占着（port_holder）。
- ffmpeg：给定的、系统里的，都没有就在后台下载固定哈希的那份（tools/ffmpeg_fetch.py，带镜像），进度给控制接口。
- 只开一个：数据目录里的 run/core.lock（acquire_lock）。
- 托盘的开关存在 desktop.json（Settings）；状态（Desktop.status）就是控制接口 /local/v1/status 的回答。

不在这里：控制接口本身（control.py）、诊断压缩包（diagnostics.py）。
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import platform
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import bootstrap
from .version import VERSION

API_VERSION = 1
LAN_PORT_FALLBACKS = (8090, 18080, 28080)
LOG_BYTES = 2 * 1024 * 1024
LOG_FILES = 5
LOCK_HELD_EXIT = 3
KEEP_AWAKE = ("watching", "always", "never")
DEFAULT_SETTINGS = {"keep_awake": "watching", "autostart": True}

# ---- 日志 ----

_IPV4 = re.compile(r"(?<![\d.])((?:25[0-5]|2[0-4]\d|1?\d?\d)\.(?:25[0-5]|2[0-4]\d|1?\d?\d)\."
                   r"(?:25[0-5]|2[0-4]\d|1?\d?\d))\.(?:25[0-5]|2[0-4]\d|1?\d?\d)(?![\d.])")
_IPV6ISH = re.compile(r"(?<![0-9A-Fa-f:])([0-9A-Fa-f]{1,4}):(?:[0-9A-Fa-f]{0,4}:){3,6}[0-9A-Fa-f]{0,4}(?![0-9A-Fa-f:])")
_QUERY_SECRET = re.compile(r"([?&#](?:s|k|t|token|code|poll|key|session|ticket)=)[^&\s\"'<>]+", re.IGNORECASE)
_PATH_SECRET = re.compile(r"(/(?:TV/[PRBLW]|tv/[ip]|[prbli])/)[A-Za-z0-9_-]{6,}")
_FRAGMENT_SECRET = re.compile(r"((?:setup|/r/)#)[^\s\"'<>]+")
_SETUP_CODE = re.compile(r"(设置码[：:]\s*)\S+")
_BEARER = re.compile(r"(Bearer\s+)\S+", re.IGNORECASE)
_COOKIE = re.compile(r"(tvs=)[^;\s]+")


def redact(text: str, home: str | None = None) -> str:
    """去掉日志里不该有的东西：网址里的会话、密钥、令牌、设置码，cookie，IPv4 的最后一段，长的 IPv6；
    home：再把用户目录换成 ~（诊断包里的路径带着用户名）。回环和 0.0.0.0 原样留着，排查时要看。"""
    text = _QUERY_SECRET.sub(r"\1***", text)
    text = _PATH_SECRET.sub(r"\1***", text)
    text = _FRAGMENT_SECRET.sub(r"\1***", text)
    text = _SETUP_CODE.sub(r"\1***", text)
    text = _BEARER.sub(r"\1***", text)
    text = _COOKIE.sub(r"\1***", text)
    text = _IPV4.sub(lambda m: m.group(0) if m.group(1) in ("127.0.0", "0.0.0") else m.group(1) + ".x", text)
    text = _IPV6ISH.sub(lambda m: m.group(1) + ":…", text)
    if home:
        text = text.replace(home, "~")
    return text


class CoreLog:
    """监督进程和子进程的每一行：去掉秘密，写进 core.log（轮转），也照旧打到标准输出（有的话）。"""

    def __init__(self, path: Path | None, echo: bool = True):
        self.path = path
        self.echo = echo
        self._lock = threading.Lock()
        self._handler = None
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._handler = logging.handlers.RotatingFileHandler(
                path, maxBytes=LOG_BYTES, backupCount=LOG_FILES, encoding="utf-8")
            self._handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))

    def __call__(self, message: str) -> None:
        line = redact(str(message))
        if self._handler is not None:
            record = logging.LogRecord("core", logging.INFO, "", 0, line, None, None)
            with self._lock:
                try:
                    self._handler.emit(record)
                except Exception:       # noqa: BLE001 —— 写不了日志不能停掉服务器
                    pass
        if self.echo and sys.stdout is not None:
            try:
                print(line, flush=True)
            except (OSError, ValueError):
                pass

    def close(self) -> None:
        if self._handler is not None:
            self._handler.close()


# ---- 端口 ----

def port_free(bind: str, port: int) -> bool:
    """这个端口现在能不能听。0.0.0.0 时也试 127.0.0.1：别的程序只听本机的同一个端口时，浏览器打开 localhost
    会到它那儿去。Windows 上不设 SO_REUSEADDR（那里它的意思是「抢别人正在听的端口」）。"""
    addresses = [bind] + (["127.0.0.1"] if bind == "0.0.0.0" else [])
    for address in addresses:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            if os.name != "nt":
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((address, port))
            sock.listen(1)
        except OSError:
            return False
        finally:
            sock.close()
    return True


def pick_lan_port(bind: str, wanted: int, free=port_free) -> int | None:
    """网页端口：想要的那个，被占了依次试 LAN_PORT_FALLBACKS；都被占时 None。"""
    for port in (wanted,) + tuple(p for p in LAN_PORT_FALLBACKS if p != wanted):
        if free(bind, port):
            return port
    return None


def _run(command: list[str], timeout: float = 4.0) -> str:
    try:
        kwargs = {"creationflags": 0x08000000} if os.name == "nt" else {}
        done = subprocess.run(command, capture_output=True, text=True, timeout=timeout, errors="replace", **kwargs)
        return done.stdout or ""
    except (OSError, subprocess.SubprocessError):
        return ""


def port_holder(port: int) -> str:
    """占着这个 TCP 端口在听的程序名（系统肯说时），否则空串。只给托盘说「被 XX 占了」用。"""
    if os.name == "nt":
        for line in _run(["netstat", "-ano", "-p", "TCP"]).splitlines():
            parts = line.split()
            if len(parts) >= 5 and parts[3].upper() == "LISTENING" and parts[1].rsplit(":", 1)[-1] == str(port):
                pid = parts[4]
                row = _run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"]).strip()
                if row.startswith('"'):
                    return row.split('","')[0].strip('"')
                return f"PID {pid}"
        return ""
    if shutil.which("lsof") is None:
        return ""
    for line in _run(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-Fc"]).splitlines():
        if line.startswith("c"):
            return line[1:]
    return ""


# ---- 只开一个 ----

def acquire_lock(path: Path):
    """拿到 run/core.lock 的独占锁就返回打开的文件（进程活着就一直拿着），别人拿着时 None。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+", encoding="utf-8")
    try:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


# ---- 托盘的开关 ----

class Settings:
    """<数据目录>/desktop.json：托盘经控制接口改的开关。读不懂的值用默认的。
    还存受信任的网络（network.py）：trusted、untrusted 两个路由器身份的列表；它们不在 snapshot() 里
    （状态的 settings 只有托盘的开关）。"""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self.values = dict(DEFAULT_SETTINGS)
        self._networks = {"trusted": [], "untrusted": []}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        if isinstance(data, dict):
            if data.get("keep_awake") in KEEP_AWAKE:
                self.values["keep_awake"] = data["keep_awake"]
            if isinstance(data.get("autostart"), bool):
                self.values["autostart"] = data["autostart"]
            from .network import valid_id
            for key in self._networks:
                found = data.get(f"{key}_networks")
                if isinstance(found, list):
                    self._networks[key] = [i for i in found if valid_id(i)]

    def _write(self) -> None:
        data = dict(self.values, trusted_networks=self._networks["trusted"],
                    untrusted_networks=self._networks["untrusted"])
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)

    def update(self, **changes) -> dict:
        with self._lock:
            self.values.update(changes)
            self._write()
            return dict(self.values)

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self.values)

    def networks(self) -> dict:
        with self._lock:
            return {key: list(ids) for key, ids in self._networks.items()}

    def set_networks(self, trusted: list[str], untrusted: list[str]) -> None:
        with self._lock:
            self._networks = {"trusted": list(trusted), "untrusted": list(untrusted)}
            self._write()


# ---- ffmpeg ----

FFMPEG_FETCH = "fetch"


def _executable(name: str) -> str:
    return name + (".exe" if os.name == "nt" else "")


class Ffmpeg:
    """ffmpeg 在哪、从哪来；没有时在后台下载，进度给状态。state：ready / fetching / missing / failed。"""

    def __init__(self, bin_dir: Path, log=print):
        self.bin_dir = bin_dir
        self.log = log
        self._lock = threading.Lock()
        self.state, self.path, self.source = "missing", "", ""
        self.done = self.total = 0
        self.error = ""
        self.version = ""
        self.mirror_used = ""
        # 还没找完（或找不到、下载还没开始）：这时「没有」不是问题，状态说 starting 而不是报错。
        # 找到了、指定的不能用、下载开始了，才算定下来。
        self.resolved = False

    def resolve(self, given: str | None, environ=os.environ) -> bool:
        """找现成的。given：--ffmpeg 的值（"fetch" 表示一定用下载的那份）。找到了返回 True。
        都没有时 resolved 还是 False：接着就该 fetch()（它把 resolved 设上）。以前一开始就设 resolved，
        检查下载过的那份（Windows、Intel Mac 上 ffmpeg -version 要几秒）的这段时间里状态是 error。"""
        try:
            return self._resolve(given, environ)
        finally:
            if self.state != "missing":
                self.resolved = True

    def _resolve(self, given: str | None, environ) -> bool:
        from tools import datadir, ffmpeg_fetch
        cached = ffmpeg_fetch.cached_path(self.bin_dir)
        if given == FFMPEG_FETCH:
            return self._found(cached, "fetched") if ffmpeg_fetch.is_usable(cached) else False
        for candidate, source in ((given, "given"), (environ.get("TV_FFMPEG"), "given")):
            if candidate:
                found = shutil.which(candidate) or (candidate if Path(candidate).is_file() else None)
                if found:
                    return self._found(Path(found), source)
                with self._lock:
                    self.state, self.error = "failed", f"找不到指定的 ffmpeg：{candidate}"
                return False
        bundled = datadir.code_root() / "bin" / _executable("ffmpeg")
        if bundled.is_file():
            return self._found(bundled, "bundled")
        system = shutil.which("ffmpeg")
        if system is None and platform.system() == "Darwin":
            # 从访达、登录项启动的程序的 PATH 里没有 Homebrew。
            system = next((p for p in ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg") if Path(p).is_file()), None)
        if system:
            return self._found(Path(system), "system")
        if ffmpeg_fetch.is_usable(cached):
            return self._found(cached, "fetched")
        return False

    def _found(self, path: Path, source: str) -> bool:
        version = ""
        out = _run([str(path), "-hide_banner", "-version"], timeout=10)
        if out:
            version = out.splitlines()[0][:120]
        with self._lock:
            if not version:
                self.state, self.error = "failed", f"ffmpeg 运行不了：{path}"
                return False
            self.state, self.path, self.source, self.version, self.error = "ready", str(path), source, version, ""
        return True

    def fetch(self, mirror: str | None = None, wait: bool = False) -> bool:
        """开始下载（已经在下载或已经有了时 False）。wait：在当前线程里做完（测试用）。"""
        with self._lock:
            if self.state in ("fetching", "ready"):
                return False
            self.state, self.error, self.done, self.total = "fetching", "", 0, 0
            self.resolved = True
        thread = threading.Thread(target=self._fetch, args=(mirror,), daemon=True, name="ffmpeg-fetch")
        thread.start()
        if wait:
            thread.join()
        return True

    def _fetch(self, mirror: str | None) -> None:
        from tools import ffmpeg_fetch

        def progress(done: int, total: int) -> None:
            with self._lock:
                self.done, self.total = done, total

        def source(url: str) -> None:
            host = url.split("/")[2] if url.count("/") >= 2 else url
            with self._lock:
                self.mirror_used = host
            self.log(f"正在下载 ffmpeg：{host}")

        def announce(size: int) -> None:
            with self._lock:
                self.total = size
            self.log(f"正在获取视频转换组件 ffmpeg（约 {size / 1e6:.0f} MB，仅此一次）…")

        try:
            path = ffmpeg_fetch.ensure(self.bin_dir, on_announce=announce, on_progress=progress, mirror=mirror,
                                       on_source=source)
        except ffmpeg_fetch.FetchError as error:
            with self._lock:
                self.state, self.error = "failed", str(error).splitlines()[0]
            self.log(f"下载 ffmpeg 失败：{error}")
            return
        except Exception as error:      # noqa: BLE001 —— 后台线程里的意外也要变成状态
            with self._lock:
                self.state, self.error = "failed", f"{type(error).__name__}: {error}"
            self.log(f"下载 ffmpeg 失败：{type(error).__name__}: {error}")
            return
        if self._found(path, "fetched"):
            self.log(f"ffmpeg 已就绪：{self.version}")
        else:
            self.log(f"下载的 ffmpeg 运行不了：{self.error}")

    def snapshot(self) -> dict:
        with self._lock:
            return {"state": self.state, "path": self.path, "source": self.source, "version": self.version,
                    "done": self.done, "total": self.total, "error": self.error, "mirror": self.mirror_used}


# ---- 设备 ----

def devices(layout: bootstrap.Layout, clock=time.time) -> list[dict]:
    """数据库里的设备，加上电视服务写的「正在播放」。只读打开数据库；读不到就是空的。"""
    from .. import now_playing
    rows: list[dict] = []
    if not layout.db.is_file():
        return rows
    try:
        conn = sqlite3.connect(layout.db.as_uri() + "?mode=ro", uri=True, timeout=1)
        try:
            found = conn.execute("SELECT id, name, model FROM device WHERE status IN ('active', 'unclaimed', 'repair') "
                                 "ORDER BY id").fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return rows
    for device_id, name, model in found:
        playing = now_playing.read(layout.devices / str(device_id) / "now-playing.json", clock())
        listen = playing.get("listen") if playing["online"] else None
        rows.append({"id": device_id, "name": name or "", "model": model or "", "online": bool(playing["online"]),
                     "channel": playing["channel"] if playing["online"] else "",
                     "since": playing.get("since") or 0,
                     # 「用手机听」：有手机在听这台设备（电视服务关掉没人听的流，见 server/listen.py）。
                     "listening": bool(listen and listen.get("on"))})
    return rows


# ---- 状态 ----

class Desktop:
    """电脑客户端模式下监督进程的全部状态；控制接口（control.py）只和它打交道。"""

    def __init__(self, layout: bootstrap.Layout, logs_dir: Path, log, bind: str, lan_port: int, lan_wanted: int,
                 model_ports: dict, lan_address: str, ffmpeg: Ffmpeg, settings: Settings, clock=time.time,
                 network=None):
        self.layout, self.logs_dir, self.log = layout, logs_dir, log
        self.configured_bind, self.lan_port, self.lan_wanted = bind, lan_port, lan_wanted
        # 受信任的网络（network.TrustedNetworks）：不在家时子进程只听本机；None 时就按设置的 bind。
        self.network = network
        self.model_ports, self.lan_address = model_ports, lan_address
        self.ffmpeg, self.settings, self.clock = ffmpeg, settings, clock
        self.started = clock()
        self.supervisor = None
        # 控制接口的 POST restart 先调它（desktop_main：重读 config.toml、重新识别地址），再重建子进程；None 时
        # 只是用原来的参数重启子进程。
        self.reload = None
        # 没有监督进程时（设置文件有错，只开着控制接口报告）/stop 设这个。
        self.stop_event = threading.Event()
        self.control_port = 0
        self.config_error = ""
        self._problems: list[dict] = []
        self._lock = threading.Lock()

    # 启动时发现的问题（端口被占、端口换了），一直留着到重启核心。
    def add_problem(self, code: str, message: str, **extra) -> None:
        with self._lock:
            self._problems.append(dict(extra, code=code, message=message))

    @property
    def bind(self) -> str:
        """子进程此刻听的地址（受信任的网络决定；没有时是设置里的）。"""
        return self.network.bind if self.network is not None else self.configured_bind

    @property
    def lan_url(self) -> str:
        if self.bind == "127.0.0.1" or not self.lan_address:
            return ""
        return f"http://{self.lan_address}:{self.lan_port}/tv/"

    @property
    def local_url(self) -> str:
        return f"http://localhost:{self.lan_port}/tv/"

    def state(self) -> str:
        supervisor = self.supervisor
        if supervisor is not None and supervisor.stopping.is_set():
            return "stopping"
        if self.config_error:
            return "error"
        ffmpeg = self.ffmpeg.state
        if not self.ffmpeg.resolved:
            return "starting"
        if ffmpeg == "fetching":
            return "fetching_ffmpeg"
        if ffmpeg in ("missing", "failed"):
            return "error"
        if supervisor is not None and supervisor.started:
            return "running"
        return "starting"

    def problems(self) -> list[dict]:
        out = []
        if self.config_error:
            out.append({"code": "config", "message": self.config_error})
        ffmpeg = self.ffmpeg.snapshot()
        if ffmpeg["state"] == "failed":
            out.append({"code": "ffmpeg_failed", "message": "没能准备好视频转换组件 ffmpeg：" + ffmpeg["error"]})
        elif ffmpeg["state"] == "missing" and self.ffmpeg.resolved:
            out.append({"code": "ffmpeg_missing", "message": "没有视频转换组件 ffmpeg"})
        with self._lock:
            out.extend(dict(p) for p in self._problems)
        if self.network is not None:
            out.extend(self.network.problems())
        supervisor = self.supervisor
        if supervisor is not None and supervisor.started and not supervisor.stopping.is_set():
            for child in supervisor.children:
                if not child.running:
                    out.append({"code": "child_down", "name": child.name,
                                "message": f"{child.name} 停了，正在重启（看日志 core.log）"})
        return out

    def setup_code(self) -> str:
        try:
            return self.layout.setup_code.read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def front_up(self) -> bool:
        try:
            socket.create_connection(("127.0.0.1", self.lan_port), timeout=1).close()
            return True
        except OSError:
            return False

    def status(self) -> dict:
        supervisor = self.supervisor
        children = {}
        if supervisor is not None:
            children = {c.name: {"running": c.running, "restarts": c.restarts} for c in supervisor.children}
        found = devices(self.layout, self.clock)
        ffmpeg = self.ffmpeg.snapshot()
        return {
            "ok": True, "api": API_VERSION, "version": VERSION, "pid": os.getpid(), "started": self.started,
            "state": self.state(), "problems": self.problems(),
            "data_dir": str(self.layout.root), "logs_dir": str(self.logs_dir), "bind": self.bind,
            "ports": {"lan": self.lan_port, "lan_wanted": self.lan_wanted, "xingzhi": self.model_ports["xingzhi"],
                      "passport": self.model_ports["passport"], "control": self.control_port},
            "lan_url": self.lan_url, "local_url": self.local_url,
            "setup_pending": self.layout.setup_code.exists(),
            "children": children, "devices": found, "watching": sum(1 for d in found if d["online"]),
            "listening": sum(1 for d in found if d["listening"]),
            "network": self.network.snapshot() if self.network is not None else
            {"id": "", "trusted": None, "profile": "", "lan_open": self.bind != "127.0.0.1"},
            "ffmpeg": {k: ffmpeg[k] for k in ("state", "path", "source", "done", "total", "error")},
            "voice": {"mode": "off"}, "hub": {"state": "off"}, "update": {"available": False},
            "settings": self.settings.snapshot(),
        }
