"""视频中转入口：python3 -m server.relay --state … --channels-out … --key-file … --ffmpeg …"""

from __future__ import annotations

import argparse
import json
import sys
import threading
from pathlib import Path

from .bilibili import Bilibili
from .state import State
from .web import Relay, make_servers
from .youtube import YouTube


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m server.relay")
    parser.add_argument("--state", required=True, type=Path)
    parser.add_argument("--channels-out", required=True, type=Path)
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
    extra = []
    for spec in args.extra_device:
        parts = spec.split(":")
        if len(parts) != 3 or not all(parts):
            print(f"--extra-device {spec!r}：要写成 名字:正在播放文件:事件文件", file=sys.stderr)
            return 2
        extra.append((parts[0], Path(parts[1]), Path(parts[2])))
    state = State(args.state)
    youtube = None
    if args.cstone and args.cstone.is_file():
        config = json.loads(args.cstone.read_text(encoding="utf-8"))
        youtube = YouTube(config={k: config[k] for k in ("url", "key", "cert")})
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


if __name__ == "__main__":
    sys.exit(main())
