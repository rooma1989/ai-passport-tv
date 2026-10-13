"""视频中转入口。

单用户（和以前一样）：python3 -m server.relay --state … --channels-out … --key-file … --ffmpeg …
多用户：python3 -m server.relay --db /var/lib/ai-passport-tv/tv.db --key-file … --ffmpeg …
  设备、用户、订阅都在数据库里（先用 python3 -m server.tenancy.migrate 从单用户的文件迁移）；
  每台设备的文件写在 --devices-dir（默认数据库旁边的 devices/）。--state、--channels-out、
  --events-out、--groups-out、--now-playing、--remote-url-out、--device-name、--extra-device 不再用。
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from pathlib import Path

from . import voicecmds
from .bilibili import Bilibili
from .state import State
from .web import Relay, make_servers
from .youtube import YouTube


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m server.relay")
    parser.add_argument("--state", type=Path, help="单用户的状态文件（不给 --db 时必需）")
    parser.add_argument("--channels-out", type=Path, help="单用户的频道表（不给 --db 时必需）")
    parser.add_argument("--db", type=Path, help="多用户：账号和设备数据库（server/tenancy）")
    parser.add_argument("--devices-dir", type=Path, help="多用户：每台设备的文件目录，默认数据库旁边的 devices/")
    parser.add_argument("--wifi-key", type=Path,
                        help="多用户：给设备加密 Wi-Fi 修改的私钥文件（32 字节，0600），默认数据库旁边的 wifi-key，"
                             "没有就生成；和数据库一起备份")
    parser.add_argument("--key-file", required=True, type=Path)
    parser.add_argument("--events-out", type=Path,
                        help="投屏事件文件，电视服务据此让设备切台")
    parser.add_argument("--ffmpeg", required=True)
    parser.add_argument("--main-channels", type=Path, help="直播频道表（只读），手机遥控按它列台")
    parser.add_argument("--groups-out", type=Path, help="手机上调整的频道分组（电视服务读取）")
    parser.add_argument("--now-playing", type=Path, help="电视服务写的「正在播放」")
    parser.add_argument("--remote-url-out", type=Path, help="写给电视服务的遥控二维码地址")
    parser.add_argument("--public-url", default="", help="手机访问的网站地址，例如 https://example.com")
    parser.add_argument("--cstone", type=Path,
                        help="YouTube 节点配置 JSON：{url, key, cert}；不给则只支持 B 站")
    parser.add_argument("--device-name", default="小智",
                        help="--now-playing / --events-out 那台设备在手机上显示的名字")
    parser.add_argument("--extra-device", action="append", default=[], metavar="名字:正在播放:事件",
                        help="再遥控一台设备（它的电视服务写的「正在播放」和读的事件文件），可重复")
    parser.add_argument("--stream-port", type=int, default=8099)
    parser.add_argument("--manage-port", type=int, default=8100)
    args = parser.parse_args(argv)

    key = args.key_file.read_text(encoding="utf-8").strip()
    # 密钥出现在网址里，太短就能被猜中。
    if len(key) < 16:
        print(f"{args.key_file}：密钥太短（至少 16 个字符）", file=sys.stderr)
        return 2
    if args.db is None and (args.state is None or args.channels_out is None):
        print("要么给 --db（多用户），要么给 --state 和 --channels-out（单用户）", file=sys.stderr)
        return 2
    extra = []
    for spec in args.extra_device:
        parts = spec.split(":")
        if len(parts) != 3 or not all(parts):
            print(f"--extra-device {spec!r}：要写成 名字:正在播放文件:事件文件", file=sys.stderr)
            return 2
        extra.append((parts[0], Path(parts[1]), Path(parts[2])))
    youtube = None
    if args.cstone and args.cstone.is_file():
        config = json.loads(args.cstone.read_text(encoding="utf-8"))
        youtube = YouTube(config={k: config[k] for k in ("url", "key", "cert")})
    if args.db is not None:
        return _serve_tenants(args, key, youtube)
    state = State(args.state)
    relay = Relay(state, Bilibili(), args.channels_out, args.ffmpeg, key,
                  events_out=args.events_out, youtube=youtube,
                  main_channels=args.main_channels, groups_out=args.groups_out,
                  now_playing_in=args.now_playing, remote_url_out=args.remote_url_out,
                  public_url=args.public_url,
                  device_name=args.device_name, extra_devices=extra,
                  log=lambda message: print(message, flush=True))
    relay.write_table()
    state.ensure_code()
    state.save()
    relay.write_remote_url()
    # 语音：换台历史、到点的定时（voicecmds.py）。
    voicecmds.start(relay)
    # 只监听回环：播放流给本机电视服务，管理网页由 nginx 转进来。
    streams, manage = make_servers(relay, args.stream_port, args.manage_port)
    threading.Thread(target=streams.serve_forever, daemon=True).start()
    print(f"视频中转已启动：播放流 127.0.0.1:{args.stream_port}，"
          f"管理网页 127.0.0.1:{args.manage_port}，"
          f"{sum(c.kind == 'up' for c in state.creators)} 个 UP 主，"
          f"{sum(c.kind == 'series' for c in state.creators)} 部剧，"
          f"{len(relay.devices)} 台设备，"
          f"YouTube {'已配置' if youtube else '未配置'}", flush=True)
    try:
        manage.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


# 多用户：管理命令直接改数据库（绑定、接收旧设备……），中转每隔这么久跟上一次设备文件。
SYNC_S = 10


def _serve_tenants(args, key: str, youtube) -> int:
    from ..tenancy.accounts import Accounts
    accounts = Accounts(args.db)
    if accounts.admin_id() is None:
        print(f"{args.db}：还没有管理员。先运行 python3 -m server.tenancy.migrate（见运维手册）。",
              file=sys.stderr)
        return 2
    if args.public_url:
        # 电视服务给未绑定设备的绑定二维码要用网站地址。
        accounts.set_meta("public_url", args.public_url.rstrip("/"))
    devices_dir = args.devices_dir or args.db.parent / "devices"
    relay = Relay(None, Bilibili(), None, args.ffmpeg, key, youtube=youtube,
                  main_channels=args.main_channels, public_url=args.public_url,
                  accounts=accounts, devices_dir=devices_dir,
                  log=lambda message: print(message, flush=True))
    relay.sync_devices(force=True)
    # 手机上管理设备的 Wi-Fi：中转的私钥（第一次启动时生成），公钥进数据库给电视服务放进 CONFIG。
    from . import wifi as wifi_api
    wifi_api.setup_key(relay, args.wifi_key)

    def keep_in_sync() -> None:
        while True:
            time.sleep(SYNC_S)
            try:
                relay.sync_devices()
            except Exception as error:  # 后台线程不能死：记下来，下一轮再试。
                print(f"同步设备文件失败：{type(error).__name__}: {error}", flush=True)

    threading.Thread(target=keep_in_sync, daemon=True).start()
    voicecmds.start(relay)
    streams, manage = make_servers(relay, args.stream_port, args.manage_port)
    threading.Thread(target=streams.serve_forever, daemon=True).start()
    print(f"视频中转已启动（多用户）：播放流 127.0.0.1:{args.stream_port}，"
          f"管理网页 127.0.0.1:{args.manage_port}，"
          f"{len(accounts.users())} 个用户，{len(relay.devices)} 台设备，"
          f"YouTube {'已配置' if youtube else '未配置'}", flush=True)
    try:
        manage.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
