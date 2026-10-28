"""自建服务器（「在家里跑的小鼠电视」）：python3 -m server.selfhost --data <数据目录>

设计见 docs/superpowers/specs/2026-10-15-self-hosting-design.md（第 4、14、15 节，阶段 S1）。Docker 镜像里
它是 1 号进程（经 tini），数据目录是 /data；没有 Docker 的 Mac、Linux 用 ./run.sh --selfhost 起它。

启动前：建数据目录、写默认设置、生成密钥、打开数据库（关掉共享令牌）、还没有管理员就生成一次性设置码，
打印横幅。然后起这些子进程，掉了就重启：

  relay     python3 -m server.relay --db … --selfhost       127.0.0.1:8099 播放流，127.0.0.1:8100 网页
  xingzhi   python3 -m server.tv_server live --port 8098   星智 Cube 1.54（240x180，15 帧）
  passport  python3 -m server.tv_server live --port 8097   AI Passport（320x180，12 帧）
  front     python3 -m server.front --lan-port 8080 …      手机、电脑打开的网页
  ytnode    python3 -m server.relay.ytnode --plain …       只在 [youtube] mode = "local" 时：127.0.0.1 上的 YouTube 节点
  openlist  python3 -m server.selfhost.openlist …          只在网盘组件装好、没停用时：127.0.0.1 上的 OpenList（openlist.py）

0.x 测试版每个型号一次服务一台设备（设计的 Q1 选项 A）；同一型号的第二台要等第一台断开。

  python3 -m server.selfhost health   查 /healthz（Docker 的 HEALTHCHECK 用）

电脑客户端（托盘启动的核心）：python3 -m server.selfhost --desktop [--data <数据目录>]，打包版是
rootv-core selfhost --desktop。多出来的（端口后备、日志文件、ffmpeg 下载、本机控制接口、子进程随监督
进程结束）见 desktop.py、control.py、supervisor.py 和 docs/superpowers/specs/2026-10-19-pc-client-architecture-design.md
第 5 节。没有 --desktop 时一切和以前一样（Docker、./run.sh --selfhost）。
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import urllib.parse
from pathlib import Path

# 这台电脑的型号 → (端口设置项, 画面, 帧率, 起始字节率, 「用手机听」端口, 路线)。
# 和云端的 tv-xingzhi、tv-passport 两个单元一样。Passport 原先用 110000（当初为按键卡顿压的上限），
# 那会让画面发糊、出色块；按键卡顿已由固件的按键优先级修好，云端也早改回了 200000，家里带宽更不是问题。
MODELS = (
    ("xingzhi", "xingzhi_port", "240x180", 15, 200000, 8198),
    ("passport", "passport_port", "320x180", 12, 200000, 8199),
)
CONTAINER_ENV = "XIAOZHI_TV_CONTAINER"


def _in_container() -> bool:
    return os.environ.get(CONTAINER_ENV) == "1" or Path("/.dockerenv").exists()


def _find_ffmpeg(given: str | None) -> str | None:
    for candidate in (given, os.environ.get("TV_FFMPEG"), "ffmpeg"):
        if candidate:
            found = shutil.which(candidate) or (candidate if Path(candidate).is_file() else None)
            if found:
                return found
    return None


def _addresses(config, accounts, in_container: bool, info=None) -> tuple[str, str, str]:
    """(本机局域网地址, 设备该连的地址, 网站地址)。容器里（桥接网络）看不到局域网地址，留空，
    等第一次设置时由浏览器用的地址补上（server/relay/setup.py）。

    先后：config.toml 写的 > 第一次设置时记在数据库里的 > 自动识别的（netident.lan_info：物理网卡上带着
    家里路由器的地址，不是代理 TUN、Tailscale、虚拟机网卡的）。数据库里记下的 IPv4 落在这些排除的网段里
    （以前的版本会把 198.18.0.1 记进去），或者这台电脑现在已经没有这个地址了（换了 DHCP 地址、换了路由器），
    就不用它，改用识别的；xxx.local 名字照用。info：已经问过的 netident.LanInfo（测试、定时重查用）。
    """
    from .. import netident
    if info is None:
        info = netident.lan_info() if not in_container and config.bind != "127.0.0.1" else netident.LanInfo()
    detected = "" if config.bind == "127.0.0.1" else info.address
    local = info.local if not in_container else None
    stored = accounts.meta("device_address", "")
    if not netident.stored_address_ok(stored, local):
        stored = ""
    device = config.device_address or stored or detected
    stored_public = accounts.meta("public_url", "")
    host = urllib.parse.urlsplit(stored_public).hostname or "" if stored_public else ""
    if not netident.stored_address_ok(host, local):
        stored_public = ""
    public = config.public_url.rstrip("/") or stored_public or \
        (f"http://{detected}:{config.lan_port}" if detected else "")
    return detected, device, public


YTNODE_PORT = 47443
_ytnode_ports: dict = {}


def ytnode_port(layout, free=None) -> int:
    """本机 YouTube 节点的端口：47443 空着就用它，否则随便一个空的。同一个数据目录在这个进程里只选一次
    （子进程的参数不变，「设置 → YouTube」只改代理时其余子进程不用重启）。"""
    key = str(layout.root)
    if key not in _ytnode_ports:
        import socket
        if free is None:
            def free(port):
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                try:
                    sock.bind(("127.0.0.1", port))
                    return sock.getsockname()[1]
                except OSError:
                    return 0
                finally:
                    sock.close()
        _ytnode_ports[key] = free(YTNODE_PORT) or free(0)
    return _ytnode_ports[key]


_openlist_ports: dict = {}


def openlist_port(layout, free=None) -> int:
    """网盘组件（OpenList，openlist.py）的端口：47444 空着就用它，否则随便一个空的；和 ytnode_port 一样，
    同一个数据目录在这个进程里只选一次。换了端口，中转按 run/openlist.json 改片库里「网盘」来源的地址。"""
    key = str(layout.root)
    if key not in _openlist_ports:
        from .openlist import PORT
        if free is None:
            import socket

            def free(port):
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                try:
                    sock.bind(("127.0.0.1", port))
                    return sock.getsockname()[1]
                except OSError:
                    return 0
                finally:
                    sock.close()
        _openlist_ports[key] = free(PORT) or free(0)
    return _openlist_ports[key]


def openlist_child_args(layout, port=None) -> list[str] | None:
    """网盘组件的子进程参数（命令开头由调用方加）；没装、停用了、没有密码时 None（不起它）。
    port 不给时用 openlist_port（装好了才去选端口）。"""
    from .openlist import OpenList
    component = OpenList(layout.root)
    if not component.ready():
        return None
    port = openlist_port(layout) if port is None else port
    paths = component.paths
    return ["--bin", str(component.binary()), "--data", str(paths.data), "--port", str(port),
            "--password-file", str(paths.secret), "--status-file", str(paths.status)]


def ytnode_child_args(layout, config, ffmpeg: str, port: int) -> list[str]:
    """本机 YouTube 节点的参数（命令开头由调用方加）。代理不放在命令行上（可能带密码）：在环境变量 TV_YT_PROXY。"""
    from .ytlocal import cookies_path
    return ["--plain", "--bind", "127.0.0.1", "--port", str(port), "--key-file", str(layout.ytnode_key),
            "--tools-dir", str(layout.youtube_bin), "--ffmpeg", ffmpeg,
            "--cookies", str(cookies_path(layout.root, config.youtube_cookies)), "--remux", "--no-check"]


# 预热（server/relay/watch.py 的 WARM_MAX）要多一两个 ffmpeg 和十几秒的段：内存少于这些的机器（小盒子、
# 树莓派）上「auto」不开。
PC_WARM_MEMORY = 4 * 1024 ** 3


def physical_memory() -> int | None:
    """这台机器的内存（字节）；查不到（Windows）时 None。"""
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (AttributeError, ValueError, OSError):
        return None


def pc_warm_streams(config, desktop: bool, memory: int | None = None) -> int:
    """中转的 --pc-warm：电脑上看直播时预热几路（config.toml 的 [pc] warm_channels）。「auto」在内存够的机器上开；
    查不到内存时只有电脑客户端开（它本来就是一台电脑）。"""
    from ..relay.watch import WARM_MAX
    if config.pc_warm == "off":
        return 0
    if config.pc_warm == "on":
        return WARM_MAX
    if memory is None:
        memory = physical_memory()
    if memory is None:
        return WARM_MAX if desktop else 0
    return WARM_MAX if memory >= PC_WARM_MEMORY else 0


def build_children(layout, config, ffmpeg: str, device_address: str, public_url: str, log,
                   in_container: bool = False, firmware: Path | None = None, desktop: bool = False) -> list:
    """firmware：网页刷机用的固件文件夹（bootstrap.firmware_dir），没有时 None。
    desktop：电脑客户端——子进程经 tools/packaged_entry.py 的分派启动、随监督进程结束、工作目录是数据目录。"""
    from .supervisor import Child, child_command, is_frozen
    root = Path(__file__).resolve().parents[2]
    from ..proxyenv import loopback_bypass
    # 本机的中转播放流不能走用户的代理（ffmpeg 只认小写 no_proxy），见 server/proxyenv.py。
    base_env = dict(loopback_bypass(os.environ), PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
    base_env["PYTHONPATH"] = str(root) + (os.pathsep + base_env["PYTHONPATH"] if base_env.get("PYTHONPATH") else "")
    watch = desktop or is_frozen()
    cwd = layout.root if desktop else None

    def child(name: str, argv: list[str], env: dict):
        return Child(name, argv, env, log, cwd=cwd, watch=watch)

    relay = child_command("server.relay", desktop) + ["--db", str(layout.db), "--devices-dir", str(layout.devices),
                      "--key-file", str(layout.relay_key), "--wifi-key", str(layout.wifi_key),
                      "--main-channels", str(layout.channels), "--ffmpeg", ffmpeg,
                      "--selfhost", "--setup-code-file", str(layout.setup_code)]
    if public_url:
        relay += ["--public-url", public_url]
    if config.device_address or device_address:
        relay += ["--device-address", config.device_address or device_address]
    # 电脑版「设置 → YouTube」要知道数据目录（改 config.toml、存 cookies、下载组件）。
    relay += ["--selfhost-data", str(layout.root)]
    warm = pc_warm_streams(config, desktop)
    if warm:
        relay += ["--pc-warm", str(warm)]
    local_youtube = config.youtube_mode == "local"
    if config.youtube_mode == "node" and layout.youtube_node.is_file():
        relay += ["--cstone", str(layout.youtube_node)]
    elif local_youtube:
        from . import bootstrap
        if bootstrap.ytnode_key(layout):
            log("生成了本机 YouTube 节点的密钥（secrets/ytnode-key）。")
        port = ytnode_port(layout)
        relay += ["--ytlocal", f"http://127.0.0.1:{port}", "--ytlocal-key-file", str(layout.ytnode_key)]
    # 总后台「设备」页的固件一栏：这次带的固件版本、刷机页的地址（server/relay/admin.py 的 firmware_info）。
    relay_env = dict(base_env, TV_FLASHER_URL="/flash/")
    if firmware is not None:
        relay_env["TV_FIRMWARE_MANIFEST"] = str(firmware / "manifest.json")
    children = [child("relay", relay, relay_env)]
    for name, port_attr, geometry, fps, rate, listen_port in MODELS:
        env = dict(base_env, TV_GEOMETRY=geometry, TV_FPS=str(fps), TV_RATE_START=str(rate),
                   TV_DB=str(layout.db), TV_DEVICES_DIR=str(layout.devices), TV_CHANNELS_FILE=str(layout.channels),
                   TV_NO_BUILTIN_CHANNELS="1", TV_ALLOW_PUBLIC="1" if config.allow_internet else "0",
                   TV_DECODER_CONNECT_TIMEOUT_S="30",
                   TV_LISTEN_PORT=str(listen_port), TV_LISTEN_ROUTE=name, TV_LISTEN_KEY_FILE=str(layout.relay_key),
                   TV_PCM_QUEUE_CHUNKS="600", TV_VIDEO_QUEUE_S="24")
        if config.asr_socket.startswith("http://"):
            # 电脑客户端的本机识别服务走回环 TCP（Windows 没有 Unix 套接字；server/voice/asr.py）。
            env.update(TV_ASR_URL=config.asr_socket, TV_RELAY_KEY_FILE=str(layout.relay_key))
        elif config.asr_socket:
            env.update(TV_ASR_SOCKET=config.asr_socket, TV_RELAY_KEY_FILE=str(layout.relay_key))
        argv = child_command("server.tv_server", desktop) + ["live", "--port", str(getattr(config, port_attr)),
                                                             "--bind", config.bind, "--ffmpeg", ffmpeg]
        children.append(child(name, argv, env))
    front = child_command("server.front", desktop) + ["--bind", config.bind, "--lan-port", str(config.lan_port),
                      "--signup", config.signup_via_device, "--status-file", str(layout.status)]
    for name, _, _, _, _, listen_port in MODELS:
        front += ["--listen", f"{name}={listen_port}"]
    if config.public_port:
        # 给隧道的端口：容器里要让隧道容器连得到（compose 不把它映射到主机上）；不用 Docker 时隧道程序
        # （Tailscale serve、cloudflared）在同一台机器上，只听本机。
        front += ["--public-port", str(config.public_port),
                  "--public-bind", "0.0.0.0" if in_container else "127.0.0.1"]
    if config.admin_over_tunnel:
        front += ["--admin-over-tunnel"]
    flasher = root / "packaging" / "flash"
    if flasher.is_dir():
        front += ["--flasher-dir", str(flasher)]
    if firmware is not None:
        front += ["--firmware-dir", str(firmware)]
    children.append(child("front", front, base_env))
    if local_youtube:
        layout.youtube_bin.mkdir(parents=True, exist_ok=True)
        # 代理在环境变量里（ytnode 读 TV_YT_PROXY）；回环地址照样不走代理（base_env 的 no_proxy）。
        node_env = dict(base_env, TV_YT_PROXY=config.youtube_proxy)
        argv = child_command("server.relay.ytnode", desktop) + ytnode_child_args(layout, config, ffmpeg, port)
        children.append(child("ytnode", argv, node_env))
    # 网盘组件（openlist.py）：管理员在片库里装好、没停用时才有；只听 127.0.0.1。装、更新、停用后中转放
    # run/refresh-request，这里重新算一遍，只起停这一个子进程。
    drive = openlist_child_args(layout)
    if drive is not None:
        children.append(child("openlist", child_command("server.selfhost.openlist", desktop) + drive, base_env))
    return children


def _health(data: Path) -> int:
    import urllib.request
    from . import config as config_module
    from .bootstrap import Layout
    try:
        config = config_module.load(Layout(data).config)
        host = "127.0.0.1" if config.bind in ("0.0.0.0", "127.0.0.1") else config.bind
        with urllib.request.urlopen(f"http://{host}:{config.lan_port}/healthz", timeout=5) as response:
            return 0 if response.status == 200 else 1
    except Exception:       # noqa: BLE001 —— 健康检查只回答是或否
        return 1


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(prog="python3 -m server.selfhost", description="小鼠电视自建服务器")
    parser.add_argument("command", nargs="?", choices=("run", "health"), default="run")
    parser.add_argument("--data", type=Path, help="数据目录（Docker 里是 /data）")
    parser.add_argument("--ffmpeg", help="ffmpeg 的路径（默认在 PATH 里找）；--desktop 时 fetch = 用固定哈希下载的那份")
    parser.add_argument("--desktop", action="store_true", help="电脑客户端（托盘启动的核心）")
    parser.add_argument("--bind", help="和 --desktop 一起用：覆盖 config.toml 的 bind（例如 127.0.0.1，测试用）")
    args = parser.parse_args(argv)
    if args.desktop:
        from . import desktop_main
        return desktop_main.run(args)
    in_container = _in_container()
    data = args.data or (Path("/data") if in_container else None)
    if data is None:
        print("要用 --data 指定数据目录（或者用 ./run.sh --selfhost，它会选一个）", file=sys.stderr)
        return 2
    if args.command == "health":
        return _health(data)
    if sys.version_info < (3, 11):
        print(f"自建服务器需要 Python 3.11 或更新的版本（现在是 {sys.version.split()[0]}）。"
              "Mac 上：brew install python@3.12", file=sys.stderr)
        return 1

    def log(message: str) -> None:
        print(message, flush=True)

    from . import banner, bootstrap, config as config_module
    from .supervisor import Supervisor
    layout = bootstrap.Layout(data)
    try:
        bootstrap.prepare(layout)
        if config_module.ensure(layout.config):
            log(f"写好了默认设置：{layout.config}")
        config = config_module.load(layout.config)
    except (bootstrap.DataDirError, config_module.ConfigError) as error:
        print(str(error), file=sys.stderr)
        return 1
    ffmpeg = _find_ffmpeg(args.ffmpeg)
    if ffmpeg is None:
        print("找不到 ffmpeg。Mac：brew install ffmpeg；Debian/Ubuntu：sudo apt install ffmpeg；"
              "或者用 --ffmpeg 指定它的路径。", file=sys.stderr)
        return 1
    if config.youtube_mode == "local":
        log("YouTube：本机模式（组件在 youtube/bin/，设置在电脑版「设置 → YouTube」）。")
    if config.max_streams:
        log("提示：0.x 测试版每个型号一次只服务一台小电视，max_streams 还不起作用。")
    if bootstrap.relay_key(layout):
        log("生成了中转密钥（secrets/relay-key）。")
    accounts = bootstrap.database(layout)
    code = bootstrap.setup_code(layout, accounts)
    detected, device_address, public_url = _addresses(config, accounts, in_container)
    accounts.close()
    image_copy = os.environ.get(bootstrap.IMAGE_FIRMWARE_ENV) or None
    firmware = bootstrap.firmware_dir(layout, Path(image_copy) if image_copy else None)
    for line in banner.lines(detected or ("127.0.0.1" if config.bind == "127.0.0.1" else ""), config.lan_port,
                             code, device_address, in_container):
        log(line)
    for line in banner.flash_lines(config.lan_port, firmware, in_container):
        log(line)
    children = build_children(layout, config, ffmpeg, device_address, public_url, log, in_container, firmware)
    running = {"config": config}

    def rebuild():
        """「设置 → YouTube」改了 config.toml（run/refresh-request）：重读，再按新的设置造子进程。端口和 bind 要重启
        服务器才生效（容器的端口映射也是按启动时的）。"""
        import dataclasses
        try:
            fresh = config_module.load(layout.config)
        except config_module.ConfigError as error:
            log(f"重读设置文件出错，还用原来的设置：{error}")
            fresh = running["config"]
        fixed = ("bind", "lan_port", "xingzhi_port", "passport_port", "public_port")
        running["config"] = dataclasses.replace(fresh, **{name: getattr(config, name) for name in fixed})
        return build_children(layout, running["config"], ffmpeg, device_address, public_url, log, in_container,
                              firmware)

    return Supervisor(layout, children, config.backup_keep_days, log, pending=rebuild).run()


if __name__ == "__main__":
    sys.exit(main())
