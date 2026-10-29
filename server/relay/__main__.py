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

from .. import listen
from . import voicecmds
from .bilibili import QUALITIES, SELFHOST_QUALITY, Bilibili, DEFAULT_QUALITY
from .state import State
from .web import Relay, make_servers
from .youtube import YouTube


def _quality(args) -> int:
    """B 站清晰度：命令行给了就用它；没给时自建（家宽）720p，云端 360p。"""
    if args.bilibili_quality is not None:
        return args.bilibili_quality
    return SELFHOST_QUALITY if args.selfhost else DEFAULT_QUALITY


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
    # 家庭服务器、电脑客户端的本机 YouTube 节点（server/selfhost 的监督进程给，[youtube] mode = "local"）。
    parser.add_argument("--ytlocal", default="", help="本机 YouTube 节点的地址 http://127.0.0.1:端口")
    parser.add_argument("--ytlocal-key-file", type=Path, help="本机 YouTube 节点的密钥文件")
    parser.add_argument("--selfhost-data", type=Path,
                        help="自建：数据目录（电脑版「设置 → YouTube」改 config.toml、导入 cookies、下载组件用）")
    parser.add_argument("--device-name", default="小智",
                        help="--now-playing / --events-out 那台设备在手机上显示的名字")
    parser.add_argument("--extra-device", action="append", default=[], metavar="名字:正在播放:事件",
                        help="再遥控一台设备（它的电视服务写的「正在播放」和读的事件文件），可重复")
    parser.add_argument("--listen-key-file", type=Path,
                        help="「用手机听」令牌的密钥文件；不给就由 --key-file 派生（电视服务读同一个文件，见 server/listen.py）")
    parser.add_argument("--stream-port", type=int, default=8099)
    parser.add_argument("--manage-port", type=int, default=8100)
    # 自建（server/selfhost 的监督进程给）：没有管理员也启动、等 /tv/setup；明文 HTTP 的 cookie；见 setup.py。
    parser.add_argument("--selfhost", action="store_true", help="自建模式（只和 --db 一起用）")
    parser.add_argument("--bilibili-quality", type=int, choices=QUALITIES, default=None,
                        help="B 站清晰度：16=360p，32=480p，64=720p；默认云端 16、自建 64")
    parser.add_argument("--setup-code-file", type=Path, help="自建：一次性设置码文件（还没有管理员时）")
    parser.add_argument("--device-address", default="", help="自建：设备该连的地址（不带端口）；空的话用设置时记下的")
    parser.add_argument("--pc-warm", type=int, default=0, metavar="路数",
                        help="自建：电脑上看直播时预热相邻的台、刚换走的台再转 30 秒，至多几路（0 = 不开，见 watch.py）")
    parser.add_argument("--pc-copy", type=int, default=None, metavar="路数",
                        help="自建：电脑上同时看几路不用转码的（含点播、预热），默认 watch.MAX_COPY")
    parser.add_argument("--pc-transcode", type=int, default=None, metavar="路数",
                        help="自建：电脑上同时至多几路要转码的，默认 watch.MAX_ENCODE（0 = 不转码）")
    args = parser.parse_args(argv)

    key = args.key_file.read_text(encoding="utf-8").strip()
    # 密钥出现在网址里，太短就能被猜中。
    if len(key) < 16:
        print(f"{args.key_file}：密钥太短（至少 16 个字符）", file=sys.stderr)
        return 2
    listen_secret = None
    if args.listen_key_file is not None:
        listen_secret = listen.derive_secret(args.listen_key_file.read_text(encoding="utf-8"))
    args.listen_secret = listen_secret
    if args.db is None and (args.state is None or args.channels_out is None):
        print("要么给 --db（多用户），要么给 --state 和 --channels-out（单用户）", file=sys.stderr)
        return 2
    if args.selfhost and args.db is None:
        print("--selfhost 要和 --db 一起用", file=sys.stderr)
        return 2
    extra = []
    for spec in args.extra_device:
        parts = spec.split(":")
        if len(parts) != 3 or not all(parts):
            print(f"--extra-device {spec!r}：要写成 名字:正在播放文件:事件文件", file=sys.stderr)
            return 2
        extra.append((parts[0], Path(parts[1]), Path(parts[2])))
    youtube, args.youtube_kind = None, ""
    if args.ytlocal:
        if args.ytlocal_key_file is None:
            print("--ytlocal 要和 --ytlocal-key-file 一起用", file=sys.stderr)
            return 2
        node_key = args.ytlocal_key_file.read_text(encoding="utf-8").strip()
        youtube, args.youtube_kind = YouTube(config={"url": args.ytlocal, "key": node_key}), "local"
    elif args.cstone and args.cstone.is_file():
        config = json.loads(args.cstone.read_text(encoding="utf-8"))
        youtube = YouTube(config={k: config[k] for k in ("url", "key", "cert")})
        args.youtube_kind = "node"
    if args.db is not None:
        return _serve_tenants(args, key, youtube)
    state = State(args.state)
    relay = Relay(state, Bilibili(quality=_quality(args)), args.channels_out, args.ffmpeg, key,
                  events_out=args.events_out, youtube=youtube,
                  main_channels=args.main_channels, groups_out=args.groups_out,
                  now_playing_in=args.now_playing, remote_url_out=args.remote_url_out,
                  public_url=args.public_url,
                  device_name=args.device_name, extra_devices=extra, listen_secret=listen_secret,
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
    selfhost = None
    if args.selfhost:
        from ..tenancy.accounts import SELFHOST_META
        from .setup import SelfHost
        # 自建的库：绑定码是 6 位数字（电视服务读同一项，accounts.short_claims）。监督进程也写，这里再确认一次。
        accounts.set_meta(SELFHOST_META, "1")
        selfhost = SelfHost(setup_code_file=args.setup_code_file, device_address=args.device_address)
        if accounts.admin_id() is None:
            # 自建：不拒绝启动，等第一个管理员在 /tv/setup 用设置码建出来。
            print("还没有管理员：等待在 /tv/setup 设置", flush=True)
    elif accounts.admin_id() is None:
        print(f"{args.db}：还没有管理员。先运行 python3 -m server.tenancy.migrate（见运维手册）。",
              file=sys.stderr)
        return 2
    if args.public_url:
        # 电视服务给未绑定设备的绑定二维码要用网站地址。
        accounts.set_meta("public_url", args.public_url.rstrip("/"))
    devices_dir = args.devices_dir or args.db.parent / "devices"
    relay = Relay(None, Bilibili(quality=_quality(args)), None, args.ffmpeg, key, youtube=youtube,
                  main_channels=args.main_channels, public_url=args.public_url,
                  accounts=accounts, devices_dir=devices_dir, listen_secret=args.listen_secret,
                  log=lambda message: print(message, flush=True), selfhost=selfhost)
    # 「设置 → YouTube」（server/relay/ytsettings.py）：现在用的是哪种 YouTube、数据目录在哪。
    relay.youtube_kind = args.youtube_kind
    relay.selfhost_data = args.selfhost_data if selfhost is not None else None
    if relay.watch is not None:
        # 换台快一点：预热相邻的直播台（server/selfhost 按 config.toml 的 [pc] warm_channels 和内存给）。
        relay.watch.warm_max = max(0, args.pc_warm)
        # 同时看几路（config.toml 的 [pc] copy_streams、transcode_streams；没给时用 watch.py 的默认）。
        if args.pc_copy is not None:
            relay.watch.max_copy = max(1, args.pc_copy)
        if args.pc_transcode is not None:
            relay.watch.max_encode = max(0, args.pc_transcode)
    # 本机模式第一次用：组件还没装好就在后台下载（server/relay/ytsettings.py）。
    from . import ytsettings
    ytsettings.autostart(relay)
    # 本机模式的 yt-dlp 自动更新：每天查一次、解析失败像是 yt-dlp 跟不上时马上查（ytupdate.py）。
    from . import ytupdate
    ytupdate.attach(relay)
    relay.sync_devices(force=True)
    # 节目单（epg.py）：默认关着；总后台打开后在数据库旁边的 epg/ 里存下好的文件。
    from . import epg
    epg.attach(relay, args.db.parent / "epg")
    # 电脑版首页的台标（logos.py）：用到时取一次，存在数据库旁边的 logos/ 里；频道表里没写台标的台用节目单的，
    # 节目单也没有的按台名用公开台标库的（logoset.py，总后台频道表里能关）。
    from . import logoset
    from .logos import LogoCache
    relay.logos = LogoCache(args.db.parent / "logos", relay.main_channels, log=lambda message: print(message, flush=True),
                            extra=lambda: relay.guide.icons() if relay.guide is not None and relay.guide.enabled else {},
                            public=lambda: logoset.enabled(relay))
    # 直播台线路的后台自检和真画面封面（linecheck.py）：只在自建 / 电脑客户端（有「在电脑上看」时）开，
    # 封面存在数据库旁边的 covers/live/。
    from . import linecheck
    linecheck.attach(relay, args.db.parent / "covers" / "live")
    # 直播源订阅（subscriptions.py）：自建 / 电脑客户端定时重新取订阅的播放列表，新线路并进频道表。
    from . import subscriptions
    subscriptions.attach(relay)
    # 我的片库（library.py）：自建 / 电脑客户端的网盘（OpenList 的 WebDAV）和本机文件夹，定时重新扫描；
    # 来源和索引存在数据库旁边的 library.json、library-index.json，网盘的海报缓存在 library-art/。
    from . import library
    library.attach(relay)
    # 网盘组件（clouddrive.py、server/selfhost/openlist.py）：组件在跑时片库里有一个受管的「网盘」来源。
    from . import clouddrive
    clouddrive.attach(relay)
    # 每个人常看的直播台（livetally.py）：直播页的「常看」分组，存在数据库旁边的 live-plays.json。
    from . import livetally
    livetally.attach(relay, args.db.parent)
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
            if relay.account_feeds is not None:
                try:
                    # 「我的关注」：每个人到点（6 小时）了就在后台同步一次（follows.py；tick 自己限次数）。
                    relay.account_feeds.tick()
                except Exception as error:  # noqa: BLE001
                    print(f"检查关注同步失败：{type(error).__name__}", flush=True)
            if relay.bili_accounts is not None:
                try:
                    # B 站登录自动续期：每天问一次要不要刷新 cookies（bililogin.py；tick 自己限次数，在后台线程里做）。
                    relay.bili_accounts.tick()
                except Exception as error:  # noqa: BLE001
                    print(f"检查 B 站登录续期失败：{type(error).__name__}", flush=True)

    threading.Thread(target=keep_in_sync, daemon=True).start()
    # 总后台的主机负载曲线：每 5 秒读一次 /proc，留 24 小时（没有 /proc 时不采样）。
    from .hoststats import HostStats
    relay.host_stats = HostStats()
    relay.host_stats.start()
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
    finally:
        # 片库的整集缓存（libraryfull.py）：下载线程停下（说明里只记写好了的，被直接杀掉也不坏）。
        if getattr(relay, "library", None) is not None:
            relay.library.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
