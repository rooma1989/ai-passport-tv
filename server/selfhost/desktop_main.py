"""python3 -m server.selfhost --desktop（打包版：xiaozhi-tv-core selfhost --desktop）：电脑客户端的核心。

和不带 --desktop 的启动（__main__.main）做同样的准备（数据目录、设置、密钥、数据库、设置码），然后多做：
数据目录默认在每个系统自己的位置（tools/datadir.desktop_data_dir），日志写文件，只开一个（run/core.lock），
网页端口被占时换一个，ffmpeg 没有就下载，开本机控制接口（control.py），托盘的管道一关就停。
退出码：0 正常停止；1 起不来；3 这个数据目录上已经有一个核心在跑（desktop.LOCK_HELD_EXIT）。
约定见 docs/superpowers/specs/2026-10-19-pc-client-architecture-design.md 第 5.13 节。
"""

from __future__ import annotations

import dataclasses
import ipaddress
import os
import sys
from pathlib import Path


def run(args) -> int:
    from tools import datadir

    from . import bootstrap, desktop

    if sys.version_info < (3, 11):
        print("电脑客户端的核心需要 Python 3.11 或更新的版本", file=sys.stderr)
        return 1
    explicit = args.data is not None
    data = (args.data if explicit else datadir.desktop_data_dir()).expanduser().resolve()
    logs_dir = datadir.desktop_logs_dir(data if explicit else None)

    def open_log():
        try:
            return desktop.CoreLog(logs_dir / "core.log")
        except OSError as error:
            print(f"写不了日志文件夹 {logs_dir}：{type(error).__name__}", file=sys.stderr)
            return desktop.CoreLog(None)

    layout = bootstrap.Layout(data)
    try:
        bootstrap.prepare(layout)
    except bootstrap.DataDirError as error:
        failed = open_log()
        failed(str(error))
        failed.close()
        print(str(error), file=sys.stderr)
        return 1
    # 先拿锁再开日志：两个核心不能同时轮转同一个 core.log。
    lock = desktop.acquire_lock(layout.run / "core.lock")
    if lock is None:
        print(f"这个数据目录上已经有一个小智电视核心在跑：{data}", file=sys.stderr)
        return desktop.LOCK_HELD_EXIT
    log = open_log()
    try:
        return _run_locked(args, layout, logs_dir, log)
    finally:
        log.close()
        lock.close()


def _run_locked(args, layout, logs_dir, log) -> int:
    from tools import datadir

    from . import bootstrap, config as config_module, desktop
    from .__main__ import _addresses, build_children
    from .control import ControlServer
    from .supervisor import Supervisor, watch_stdin
    from .version import VERSION
    log(f"小智电视核心 {VERSION} 正在启动：数据目录 {layout.root}，日志 {logs_dir}")
    settings = desktop.Settings(layout.root / "desktop.json")
    ffmpeg = desktop.Ffmpeg(layout.root / "bin", log)
    config_error = ""
    try:
        if config_module.ensure(layout.config):
            log(f"写好了默认设置：{layout.config}")
        config = config_module.load(layout.config)
        if args.bind:
            try:
                ipaddress.IPv4Address(args.bind)
            except ValueError:
                raise config_module.ConfigError("--bind 要写 IPv4 地址，例如 127.0.0.1") from None
            config = dataclasses.replace(config, bind=args.bind)
    except config_module.ConfigError as error:
        config, config_error = config_module.Config(), str(error)
        log(f"设置文件有错，电视服务没有启动：{error}")

    lan_port = desktop.pick_lan_port(config.bind, config.lan_port) or config.lan_port
    model_ports = {"xingzhi": config.xingzhi_port, "passport": config.passport_port}
    state = desktop.Desktop(layout, logs_dir, log, config.bind, lan_port, config.lan_port, model_ports, "",
                            ffmpeg, settings)
    state.config_error = config_error
    if lan_port != config.lan_port:
        holder = desktop.port_holder(config.lan_port)
        state.add_problem("lan_port_moved", f"端口 {config.lan_port} 被{holder or '别的程序'}占着，网页改用 {lan_port}",
                          **{"from": config.lan_port, "to": lan_port})
        log(f"端口 {config.lan_port} 被占了，网页改用 {lan_port}")
    if not config_error:
        fixed = {config.xingzhi_port: "星智", config.passport_port: "AI Passport"}
        fixed.update({port: "内部" for port in config_module.FIXED_PORTS.values()})
        for port, what in fixed.items():
            if not desktop.port_free(config.bind, port):
                holder = desktop.port_holder(port)
                state.add_problem("port_in_use", f"端口 {port}（{what}）被{holder or '别的程序'}占着",
                                  port=port, holder=holder)
                log(f"端口 {port}（{what}）被{holder or '别的程序'}占着")

    control = ControlServer(state, layout.run, log)
    control.start()
    log(f"本机控制接口：127.0.0.1:{control.port}（令牌在 run/control.json）")
    try:
        if config_error:
            # 只开着控制接口，让托盘能说出哪一项错了；等托盘叫停。
            watch_stdin(state.stop_event)
            while not state.stop_event.wait(0.5):
                pass
            return 1

        if bootstrap.relay_key(layout):
            log("生成了中转密钥（secrets/relay-key）。")
        accounts = bootstrap.database(layout)
        code = bootstrap.setup_code(layout, accounts)
        config = dataclasses.replace(config, lan_port=lan_port)
        detected, device_address, public_url = _addresses(config, accounts, False)
        accounts.close()
        state.lan_address = detected
        image_copy = os.environ.get(bootstrap.IMAGE_FIRMWARE_ENV) or None
        if image_copy is None and datadir.is_frozen():
            image_copy = str(datadir.code_root() / "firmware")
        firmware = bootstrap.firmware_dir(layout, Path(image_copy) if image_copy else None)
        if code:
            log(f"还没有管理员：在这台电脑上打开 {state.local_url}setup（托盘的「打开电脑版」会带上设置码）")
        if detected and config.bind != "127.0.0.1":
            log(f"家里的手机打开：{state.lan_url}；小电视上的服务器地址填：{device_address or detected}")
        if firmware is None:
            log("网页刷机还没有固件：把发布里的 xiaozhi-tv-flasher.zip 解压进数据目录的 firmware/")

        if not ffmpeg.resolve(args.ffmpeg):
            if ffmpeg.state == "failed":
                log(f"ffmpeg：{ffmpeg.error}")
            else:
                ffmpeg.fetch()
        else:
            log(f"ffmpeg（{ffmpeg.source}）：{ffmpeg.path}")

        def pending():
            if ffmpeg.state != "ready":
                return None
            return build_children(layout, config, ffmpeg.path, device_address, public_url, log, False, firmware,
                                  desktop=True)

        supervisor = Supervisor(layout, [], config.backup_keep_days, log, pending=pending)
        state.supervisor = supervisor
        if watch_stdin(supervisor.stopping):
            log("托盘关掉标准输入时停止。")
        return supervisor.run()
    finally:
        control.close()
