"""视频中转入口：python3 -m server.relay --state … --channels-out … --key-file … --ffmpeg …"""

from __future__ import annotations

import argparse
import sys
import threading
from pathlib import Path

from .bilibili import Bilibili
from .state import State
from .web import Relay, make_servers


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m server.relay")
    parser.add_argument("--state", required=True, type=Path)
    parser.add_argument("--channels-out", required=True, type=Path)
    parser.add_argument("--key-file", required=True, type=Path)
    parser.add_argument("--events-out", type=Path,
                        help="投屏事件文件，电视服务据此让设备切台")
    parser.add_argument("--ffmpeg", required=True)
    parser.add_argument("--stream-port", type=int, default=8099)
    parser.add_argument("--manage-port", type=int, default=8100)
    args = parser.parse_args(argv)

    key = args.key_file.read_text(encoding="utf-8").strip()
    # 密钥出现在网址里，太短就能被猜中。
    if len(key) < 16:
        print(f"{args.key_file}：密钥太短（至少 16 个字符）", file=sys.stderr)
        return 2
    state = State(args.state)
    relay = Relay(state, Bilibili(), args.channels_out, args.ffmpeg, key,
                  events_out=args.events_out,
                  log=lambda message: print(message, flush=True))
    relay.write_table()
    # 只监听回环：播放流给本机电视服务，管理网页由 nginx 转进来。
    streams, manage = make_servers(relay, args.stream_port, args.manage_port)
    threading.Thread(target=streams.serve_forever, daemon=True).start()
    print(f"视频中转已启动：播放流 127.0.0.1:{args.stream_port}，"
          f"管理网页 127.0.0.1:{args.manage_port}，{len(state.creators)} 个 UP 主", flush=True)
    try:
        manage.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
