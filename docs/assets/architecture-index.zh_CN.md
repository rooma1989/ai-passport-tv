<p align="right">
  <strong>简体中文</strong> · <a href="architecture-index.md">English</a>
</p>

# 架构索引（仅限本 fork 的导航辅助文档）

**用途。** 本文件是一张地图，不是规则集：目的是让本 fork 收到一个新需求时能一次定位到该改哪个文件，而不必重新通读整棵代码树。它不新增也不改变任何规则——[`AGENTS.md`](../../AGENTS.md) 仍是约定、构建命令与安全基线的唯一事实来源。先读本文件找"在哪";再读 `AGENTS.md` 自己的路由表和相邻代码找"怎么做"。

按 [`docs/fork-guide.md`](../fork-guide.md)（`docs/assets/` 用于存放"架构笔记"的那条约定），本文件属于 fork 私有内容，不向上游提交。

**本文件自身的维护规则。** 每当 `main/` 或 `server/` 新增模块，或出现下表未覆盖的任务类型时，就更新本文件。每条条目只写一行 + 一个链接；把理由写在它指向的代码注释或文档里，绝不写在这里——重复的解释就是第二个事实来源，一定会 drift。如果本文件与代码冲突，以代码为准，回来修本文件。

## 1. 一个入口背后的两种固件形态

[`main/main.c`](../../main/main.c) 在 `app_main()`（约第 108 行）里，根据
`CONFIG_AV_RAW_PROTOTYPE`（声明于 [`main/Kconfig.projbuild`](../../main/Kconfig.projbuild)）
在两套完全不同的程序之间二选一：

| 形态 | 开启方式 | 入口 | 是什么 |
| --- | --- | --- | --- |
| BSP 能力演示菜单（上游基线） | 默认（`CONFIG_AV_RAW_PROTOTYPE=n`） | `app_main()` 直接搭建 LVGL 菜单 | 板级能力演示：Display/Button/Audio/Battery/Wi-Fi/BLE/Low-Power 各页。本 fork 基本不动它；沿用 `AGENTS.md` 自己的路由表即可。 |
| 电视播放原型（本 fork 的产品） | `sdkconfig.av-prototype`（`CONFIG_AV_RAW_PROTOTYPE=y`） | [`main/av_player.c`](../../main/av_player.c) 中的 `av_player_main()` | 本 fork 实际发布的网络电视固件。几乎所有 fork 需求都落在这里或 `server/`。 |

本 fork 自己的开发几乎全在第二行。本索引其余部分都围绕它组织。

## 2. 设备固件：`main/av_player.c`（约 3600 行——用 grep 定位，不要通读）

一个文件拥有整个会话：socket、三个 FreeRTOS worker、配网流程和叠加菜单。直接跳到对应函数，不要从头翻：

| 关注点 | 函数 / 符号 | 备注 |
| --- | --- | --- |
| 线协议常量、包头结构体、索引图解码 | [`main/av_protocol.h`](../../main/av_protocol.h) / `.c` | 无 ESP-IDF 依赖，可跑 host test。每个魔法数字都有带实测数据的"为什么"注释——改数字前先读注释。 |
| Wi-Fi 拉起 | `wifi_init()` | 初始化 NVS + `av_provision_*`。 |
| 启动流程、模式判定、主循环 | `av_player_main()` | 负责配网 vs 播放的判定（`av_boot_mode()`）、音量/亮度恢复、每会话重置、任务创建顺序。 |
| 配网 / captive portal 流程 | `run_setup_mode()` | 绘制配网屏；与 `av_provision_*` 交互。 |
| 握手（HELLO/CONFIG） | `hello()`、`config_valid()` | 按 `av_protocol.h` 里的常量校验服务端的 CONFIG JSON。 |
| socket 接收循环 | `receive_task()` | 任务优先级最高（7），是刻意的——为什么这个顺序相对 audio/video 是硬约束，见文件末尾 `xTaskCreate` 调用上方的注释。 |
| 音频播放 | `audio_task()` | 拥有 I2S；音频是本会话的时钟基准（等价于 `estimated_pts()`）。 |
| 视频解码与绘制 | `video_task()`、`push_stripe()`、`enlarge_stripe()`、`overlay_stripe()` | 拥有裸屏；解压 → 索引转 RGB565 → 可选放大 → 叠加层 → DMA 提交。 |
| 换台请求 | `request_switch()`、`process_key()` | 按键手势 → 待切换频道 id；在主循环里随会话重启时统一应用。 |
| 会话生命周期 | `allocate_session()`、`free_session()`、`session_drain()` | 缓冲区归属与排空/超时规则，详见 `docs/local-tv-prototype.md`。 |
| 设备端截图（调试用） | `shot_take()`/`shot_print()`（受 `CONFIG_AV_SCREEN_CAPTURE` 保护） | 默认关闭；见 Kconfig 帮助文字。 |

配套模块，均已可在 host 端测试，且已被 `tools/validate.sh --static` 覆盖：

| 文件 | 负责什么 |
| --- | --- |
| [`main/av_channel_policy.c`/`.h`](../../main/av_channel_policy.h) | "这个频道是否出过画面"的记录，以及跳到下一频道的决策。 |
| [`main/av_adpcm.c`/`.h`](../../main/av_adpcm.h) | 音频包的 IMA ADPCM 块解码（324 字节 → 640 个采样），以及麦克风上行用的编码器；块格式写在头文件和 [`docs/local-tv-prototype.md`](../local-tv-prototype.zh_CN.md) 里。由 `audio_task()` 调用。 |
| [`main/av_provision.cpp`/`.h`](../../main/av_provision.h) | Wi-Fi station/AP 生命周期（C++，封装 `esp-wifi-connect`）。 |
| [`main/av_provision_policy.c`/`.h`](../../main/av_provision_policy.h) | 纯启动模式判定（`AV_BOOT_PLAY` vs `AV_BOOT_SETUP`）、自动配网和退出、配网页列表、声波页地址 `/TV/W`、声波状态那一行；无 ESP-IDF 依赖。 |
| [`main/av_settings.c`/`.h`](../../main/av_settings.h) | 亮度/音量的分档表（固定档位而非连续值）。 |
| [`main/av_store.c`/`.h`](../../main/av_store.h) | 音量/亮度/服务器地址的 NVS 持久化，以及设备密钥。 |
| [`main/av_identity.c`/`.h`](../../main/av_identity.h) | HELLO 里给多用户服务器的设备身份（`device_id`、`model`、`device_secret`、`fw`）；格式化部分有主机测试。 |
| [`main/av_wifi_ops.c`/`.h`](../../main/av_wifi_ops.h)、[`main/av_wifi_crypto.c`/`.h`](../../main/av_wifi_crypto.h)、[`main/av_wifimgr.c`/`.h`](../../main/av_wifimgr.h) | 手机管理保存的 Wi-Fi：修改检查和回答 JSON（主机测试）、X25519/HKDF/AES-GCM 信封（对 IDF 自带 mbedTLS 做主机测试）、以及衔接（HELLO `wifimgr`/`wkey`、CONFIG `wifi_key`、SWITCH `wifi`、STATUS 回答）。设计：[`2026-10-13-wifi-setup-design.zh_CN.md`](../superpowers/specs/2026-10-13-wifi-setup-design.zh_CN.md)。 |
| [`main/av_soundsetup.c`/`.h`](../../main/av_soundsetup.h) | 配网模式里的声波配网：麦克风任务喂解码器 `av_soundprov`、给屏幕的进度、像网页表单那样试连并保存；构建定义了 `AV_SOUNDPROV_PRESENT`，缺了解码器会编译出错，而不是悄悄变成「不可用」。 |
| [`main/av_wifi_hint.c`/`.h`](../../main/av_wifi_hint.h) | 配网热点扫到的 Wi-Fi，放进声波配网页的二维码 `<SITE>/TV/W#1<base32hex>`：留哪些扫描结果、片段编码、让码不超过 5 版（主机测试；页面 `wifi.html` 负责解析）。设计：[`2026-10-13-sound-provisioning-design.zh_CN.md`](../superpowers/specs/2026-10-13-sound-provisioning-design.zh_CN.md)。 |
| [`main/av_soundprov.c`/`.h`](../../main/av_soundprov.h) | 声波配网解码器：输入 16 kHz 麦克风 PCM，输出 Wi-Fi 名称、密码、服务器地址；纯 C、只用整数，在模拟信道上有主机测试。手机端是 `server/relay/assets/wifi.html`（`/tv/wifi`、`/TV/W`）。 |
| [`main/av_server_addr.c`/`.h`](../../main/av_server_addr.h) | 解析配网页填的 "host[:port]" 文本。 |
| [`main/ui_menu.c`/`.h`](../../main/ui_menu.h) | 频道列表/状态页/亮度叠加层的状态机，以及频道列表面板里的语音子状态——纯逻辑，无 LVGL。 |
| [`main/av_voice_policy.c`/`.h`](../../main/av_voice_policy.h) | 按住说话：每种语音状态下每个按键做什么、按住与短按的区分、各种超时——纯逻辑，有主机测试。 |
| [`main/av_recover.c`/`.h`](../../main/av_recover.h) | 设备上的「恢复登录」：状态页上的手势（快速连按 N 下 OK、同时按住 ▲ 和 OK）以及它和状态页怎么分用 OK、恢复画面的各个阶段、网址检查——纯逻辑，有主机测试（`tests/test_av_recover.c`）。 |
| [`main/av_voice.c`/`.h`](../../main/av_voice.h) | 设备端的按住说话：录音任务、高通、ADPCM、由 `receive_task()` 发出的上行环形缓冲、`AV_VOICE_RESULT` 解析。与播放器的衔接在 `av_player.c` 的 `voice_*`。 |
| [`main/ui_text.c`/`.h`](../../main/ui_text.h) | 直接画到屏幕条带上的位图文字（非 LVGL）；CJK 字形来自 `ui_font_cjk.bin`，由 `tools/make_font.py` 重新生成。 |
| [`main/ui_draw.c`/`.h`](../../main/ui_draw.h) | 设备端 UI 设计稿的图标、宠物和像素图形（点线、切角矩形、10 格进度条、不透明面板）；素材在 `main/xiaozhi_tv_ui_assets.h`，由设计稿导出。 |
| [`main/ui_status.c`/`.h`](../../main/ui_status.h) | 顶条：左侧标签（播放时是台号和台名）和右侧的信号、电量。 |
| [`main/ui_overlay.c`/`.h`](../../main/ui_overlay.h) | 播放画面上的浮层：音量/亮度条、换台横幅、快进框、暂停横条。`tools/render_ui/render_ui.sh` 在电脑上用真字库把它们画成 PNG，用来核对版式。 |
| [`main/ui_pixel.c`/`.h`](../../main/ui_pixel.h) | 共用的视觉主题（天空/草地/吉祥物/面板），BSP 演示菜单也在用。按 `docs/development/ai-guide.md` 要求保持完整。 |

## 3. 服务端：`server/`（纯 Python 标准库）

| 文件 | 负责什么 |
| --- | --- |
| [`server/tv_server.py`](../../server/tv_server.py) | 入口；`live`/`run`/`prepare`/`import-video` 子命令；单连接会话循环。 |
| [`server/live.py`](../../server/live.py) | 直播频道的 ffmpeg 管线、`CHANNELS` 表（从 `channels.txt` 加载）、调色板处理。快速起播：`open_urls`（解析好的列表地址、`TV_MEMORY`，打不开时 `LiveChannel.start` 退回频道表里的地址）、短探测、`LIVE_BURST_S` / `burst_supported`（`-readrate_initial_burst`）、帧率探测缓存（`FPS_PROBE_ARGS`、`FPS_RETRY_S`）。 |
| [`server/hlsopen.py`](../../server/hlsopen.py) | 小电视（`live.py`）和电脑上看（`relay/watch.py`）共用的直播起播代码：`LIVE_PROBE`、`LiveMemory`（`RESOLVED_S` 内记住的列表地址、电脑上的转法）、`resolved_playlist`（从 ffmpeg 输出里认）、`resolve_playlist`（先走一遍跳转和主列表）、`variants` / `pick_variant`（多码率主列表挑一路：小电视挑不用放大的最小一路，电脑挑不超过 `PC_MAX_BPS` / `PC_MAX_HEIGHT` 的最高一路）（+ `tests/test_live_fast_start.py`）。 |
| [`server/routes.py`](../../server/routes.py) | 直播台的备用线路（存在 `channels.txt.meta.json` 里）、试线路的顺序（`preferred`：上次好用的先试，除非另一条快 40% 以上，再按量到的用时；`RouteCycle`）、上次好用的线路、检测和起播的用时、出错的大白话；轻量的线路检测在 [`server/relay/routecheck.py`](../../server/relay/routecheck.py)；后台按时间表检测每条线路、顺便取的直播封面（`/api/art/live/<id>`，只在自建 / 电脑客户端）在 [`server/relay/linecheck.py`](../../server/relay/linecheck.py)；订阅的直播源（定时重新读，新线路并进表里已有的台，`<频道表>.subs.json`，只在自建 / 电脑客户端）在 [`server/relay/subscriptions.py`](../../server/relay/subscriptions.py)；每条线路的真实速度和画面真不真（画面不动、和同台其他线路对不上；存成 `quality`，排线路顺序用）在 [`server/relay/linequality.py`](../../server/relay/linequality.py)。 |
| [`server/protocol.py`](../../server/protocol.py) | `main/av_protocol.h` 线协议的 Python 镜像。两边要保持一致；有测试交叉核对共用常量。 |
| [`server/frames.py`](../../server/frames.py) | 索引图的条带切割/压缩——是 `av_expand_indexed`/`push_stripe` 的服务端对应物。 |
| [`server/timeline.py`](../../server/timeline.py) | 内容时间 vs 到达时间、`SessionClock`。**任何音画同步或时序问题先读这里**；本区域当前"已修/未修/已测"的状态见 `docs/development/state-20260916.md`。 |
| [`server/pts.py`](../../server/pts.py) | 从同一次解码中推导两路流各自的时间戳，而不是靠计帧数。 |
| [`server/rate.py`](../../server/rate.py) | 会话运行期间的自适应视频码率/帧率选择。 |
| [`server/media.py`](../../server/media.py) | `prepare`/`import-video`：ffmpeg 调用、几何/黑边规则。 |
| [`server/netident.py`](../../server/netident.py) | 推算本机自身可达地址，打印给设备用。 |
| [`server/usb_link.py`](../../server/usb_link.py) | 在 USB 串口传输上包一层 socket 形状的适配器（仅用于测量build）。 |
| [`server/live_sender.py`](../../server/live_sender.py) | opt-in 的 v2 发送器（`TV_LIVE_ENGINE=v2`）；动它之前先看 [`docs/development/live-sender-v2.md`](../development/live-sender-v2.zh_CN.md) 的范围与边界。 |
| [`server/relay/`](../../server/relay/web.py) | UP 主和剧集频道的视频中转：B 站接口、看过/进度状态、生成的频道表、MPEG-TS 流和带密钥的网页。独立服务（`python3 -m server.relay`）。 |
| [`server/tenancy/`](../../server/tenancy/accounts.py) | 多用户阶段 1：SQLite 里的账号、会话、邀请、设备和每个用户的状态（`accounts.py`、`db.py`）；电视服务认设备（`gate.py`、`unbound.py`）；迁移（`migrate.py`）和管理命令（`admin.py`）。中转一侧：`server/relay/tenants.py`。运维手册：[`docs/development/tenancy-phase1-runbook.zh_CN.md`](../development/tenancy-phase1-runbook.zh_CN.md)。 |
| [`server/relay/console.py`](../../server/relay/console.py) | 多用户阶段 2：电脑版（媒体中心界面：`assets/console.html`、`console.css`、`console.js`；管理员的「管理」`#/admin` 在 `console-admin.js`、`console-admin.css`；旧地址 `/tv/console-classic`、`/tv/console/classic`、`/tv/admin` 转到新电脑版，`LEGACY_REDIRECTS`）、扫码登录的接口和手机确认页（`assets/login.html`）、手机上点了才加载的相机扫码（`assets/scan.js`：登录模式，和遥控「扫一扫」认所有码的 `classify()`；随附的 jsQR `assets/jsqr.js` 由 `tools/vendor_jsqr.py` 生成）、登录列表、跨站 POST 检查（`same_origin()`）；反方向「让手机登录」（已登录的电脑或手机给新手机，`assets/phone.html`，链接在 [`server/tenancy/phonelinks.py`](../../server/tenancy/phonelinks.py)）。登录票据和限流：[`server/tenancy/tickets.py`](../../server/tenancy/tickets.py)。二维码画成 SVG：[`server/relay/qr.py`](../../server/relay/qr.py)。 |
| [`server/relay/static.py`](../../server/relay/static.py) | 中转的静态页面、样式和脚本：启动时压一次 gzip（`web.PACKED`、`console.PACKED_PAGES`），请求收 gzip 就给压过的（`Content-Encoding`、`Vary: Accept-Encoding`，两种各一个 ETag，`If-None-Match` 对上回 304；带 Range 的、图片不压）。管理脚本 `console-admin.js` 不在电脑版页面里：管理员打开「管理」时 `console.js` 才加载（`loadAdmin`）。 |
| [`server/relay/admin.py`](../../server/relay/admin.py) | 多用户阶段 3：总后台的接口 `/api/admin/*`（页面是电脑版的「管理」，`assets/console-admin.js`），只给管理员：概览和设备流量（「正在播放」的 `bytes_10s`/`fps`/`target_rate`，[`server/now_playing.py`](../../server/now_playing.py)）、主机负载（[`server/relay/hoststats.py`](../../server/relay/hoststats.py)，`/proc` 采样）、用户、设备、操作记录、系统；全局频道表编辑（[`server/relay/channeltable.py`](../../server/relay/channeltable.py)：id 不变、子进程里跑 `live.parse_channels()`、带日期备份）。数据库一侧：[`server/tenancy/manage.py`](../../server/tenancy/manage.py)。 |
| [`server/channel_groups.py`](../../server/channel_groups.py) | 频道分组：按台名归类、手机上的调整、CONFIG 里的分组表；设备的上限，长表截成一台设备收得下的那部分（`device_list`、`TABLE_MAX`）。 |
| [`server/now_playing.py`](../../server/now_playing.py) / [`server/cast_switch.py`](../../server/cast_switch.py) | 正在播放（给手机看），以及把中转的事件变成 SWITCH 包或 HELLO 时的新台。 |
| [`server/listen.py`](../../server/listen.py) | 用手机听（原型，尚未接入）：设备晚 H 秒照着放的时间表、HLS 音频段窗口、从队列取声音、收听令牌。设计见 [`2026-10-12-phone-audio-design.zh_CN.md`](../superpowers/specs/2026-10-12-phone-audio-design.zh_CN.md)；本机试验在 `tools/listen_proto/`。 |
| [`server/relay/vod.py`](../../server/relay/vod.py) | 在电脑上点播（只有自建 / 电脑客户端）：浏览器能直接放的 B 站视频（html5 playurl 的单段 H.264 MP4，`bilibili.play_info`、`vod.playable`）在 `/tv/vod/<编号>/<令牌>/media` 按 Range 转发，地址过期了重新取，名额和 `watch.py` 一起算；`web.py` 的 `_vod_source` 按每一项选点播还是 HLS（`vod: true`、`quality`）；播放器是 `assets/console.js` 里的 `VodPlayer`（浏览器自带的 `<video>`）。YouTube 多了 DASH 的两条轨（`…/v`、`…/a`、`…/manifest`），由 `ytvod.py` 准备。 |
| [`server/relay/ytvod.py`](../../server/relay/ytvod.py) | 在电脑上点播 YouTube（只在本机模式）：节点 `/yt/formats` 的结果按视频缓存到地址过期，后台预取（`POST /api/yt/prefetch`，绝不耽误起播），选 H.264 + AAC 的格式（不行时 360p 合体文件），解析 `sidx` 成分段表，经 YouTube 代理取上游；播放器是 `assets/console.js` 里的 `DashPlayer`（MediaSource 的两个 SourceBuffer）。 |
| [`server/relay/ytupdate.py`](../../server/relay/ytupdate.py) | YouTube 本机模式的 yt-dlp 自动更新：每天查一次、解析失败像是 yt-dlp 跟不上时马上查（`ytnode.looks_broken`、`/yt/health` 的 `broken` / `busy`），经 `ytcomponents.Components` 安装并留着上一个版本（`previous`、`commit`、`rollback`、换之前的 gate），用 `/yt/check` 自检，`[maintenance] update_check`；状态在 `youtube/bin/autoupdate.json`（+ `tests/test_ytdlp_autoupdate.py`）。 |
| [`server/relay/bilidash.py`](../../server/relay/bilidash.py) | 电脑上点播很长的 B 站视频：html5 的 MP4 的索引（moov）大得等不起时（`vod.probe_moov`、`vod.MOOV_CHECK_S` / `MOOV_MAX`），同一 P 改放 B 站的 DASH（`bilibili.Bilibili.vod_dash`、`dash_info`），清单和播放器用 YouTube 那一套（`ytvod.DashIndexes`、页面的 `DashPlayer`）；到上游的连接复用，和 urllib 一样跟随环境变量里的代理（+ `tests/test_relay_bilidash.py`）。 |
| [`server/relay/watch.py`](../../server/relay/watch.py) | 在电脑上看（只有自建 / 电脑客户端，中转 `--selfhost`）：电脑版的「在电脑上看」放选中的小电视正在播的台的片源——ffmpeg（H.264 只换封装，别的画面转码，声音转 AAC）把分片 MP4 写到管道，这里切成内存里的 HLS，同源给 `/tv/watch/<编号>/<令牌>/`；`POST /api/watch`（主人和成员）在 `web.py`；播放器是 `assets/console.js` 里的 `WatchPlayer`（MediaSource）。 |
| [`server/relay/library.py`](../../server/relay/library.py) | 我的片库（M7 第一步，只在自建 / 电脑客户端；`relay.library`，`__main__` 里 `attach`）：来源（WebDAV，例如 OpenList 挂出来的百度网盘、夸克；本机文件夹）存在数据库旁边的 `library.json`（600，密码从不回给页面、不进日志），索引在 `library-index.json`（重新扫描只改变了的，条目数和层数有上限），元数据只用本地的（`.nfo`、视频旁边的海报、按文件名认电影和剧集），每 12 小时后台扫描一次；`GET /api/library`、`/api/library/file/<id>`（按 Range，只认条目 id；网盘的跳转照跟、不带密码）、`/api/library/art/<id>`（网盘的海报要时才取，存在 `library-art/`）；只给家里人，访客 404。管理员接口 `/api/admin/library*` 在 `admin.py`；在电脑上放是 `web.py` 的 `Relay._pc_library`（`POST /api/watch {library}`）；页面是 `assets/console.js` 的 `renderLib`。第二步：每个条目探测一次（索引里的 `media`；`probe`、`parse_probe_json`、`parse_ffmpeg_dump`），`plan` 定直接放还是封装（页面的 `caps` 允许时 HEVC 也只换封装，AAC 直接用，否则把默认的声音转码），`watch_source` 做 `watch.Source`（明确的 -map、`hvc1`、照原样的时间轴、有上限的预读、网盘内嵌字幕的旁路输出），ffmpeg 读的是 `/api/library/raw/<id>?t=`（`raw`，只给本机、HMAC 令牌），`/api/library/subs/<id>/<n>.vtt`（`serve_subs`，只给家里人）把内嵌和外挂字幕给成 WebVTT（缓存在 `library-subs/`）。 |
| [`server/relay/librarycache.py`](../../server/relay/librarycache.py) | 片库网盘文件的索引区缓存（`Library.cache`，只给 ffmpeg 读的本机地址 `/api/library/raw` 用；library.py 的 `_serve_cached`）：每次起播、跳转都重起 ffmpeg，它每次都要再读一遍同样的「目录」字节，所以把这些地方存在 `library-cache/`（`<id>-<大小>-<修改时间>.json` 和 `.<起点>.bin`，文件 600、文件夹 700）：开头到媒体数据再多 `SLACK`（MP4 / MOV 到 mdat 或 moof，`mp4_layout`；MKV / WebM 到第一个 Cluster，`mkv_layout`；别的格式 `HEAD_OTHER`）、MP4 的 mdat 后面（文件尾的 moov）、别的容器最后 `TAIL` 字节里第一次被读的地方起到文件尾（MKV 的 Cues）。只在 ffmpeg 本来就读这些字节时边放边存（从不另外去网盘取）；起点落在已存的一段里的请求从缓存给，不够的在同一个回答里接着从网盘读。大小或修改时间变了、网盘回答的总长对不上时作废；每个文件至多 `FILE_MAX`，总共至多 `CACHE_MAX`，按最后用过的时间删最旧的；本机文件夹不缓存（及 `tests/test_relay_library_cache.py`）。 |
| [`server/relay/home.py`](../../server/relay/home.py) | 媒体中心电脑版的汇总接口：`GET /api/home`（首页几排，按调用者，带缓存）、`GET /api/targets` 和 `POST /api/play`（播放到：一个入口分到换台、订阅播放、选集、投屏、重播和在电脑上看）、`POST /api/sleep`（遥控面板的定时关，用 `voicecmds.set_timer`），以及 `PcProgress`（电脑上看到的位置，按用户，`pc-progress.json`）。每个人在自己的小电视和电脑上看各个直播台看了多久（直播页的「常看」，`/api/state` 的 `frequent`，`live-plays.json`）：[`server/relay/livetally.py`](../../server/relay/livetally.py)。按播放列表 `tvg-logo` 缓存的台标：[`server/relay/logos.py`](../../server/relay/logos.py)（`GET /api/art/logo/<台>`）。设计：`docs/superpowers/specs/2026-10-24-media-center-ui-design.zh_CN.md`。 |
| [`server/episode_info.py`](../../server/episode_info.py) | 给 `"info": 1` 的固件的「正在播 / 正在加载哪一集」：中转的 `/info/<台>`、CONFIG 的 `playing`、SWITCH 的 `loading`。 |
| [`server/progress.py`](../../server/progress.py) | 给 `"progress": 1` 的固件的进度条锚点：中转 `/info/<台>` 的 `duration`/`offset`，每个片源开始时的 SWITCH `progress`，断流或暂停时的 `stop`。设备侧：`main/ui_menu.c` 的 `ui_progress_*`，由 `ui_overlay_progress_*` 画。 |
| [`server/voice/`](../../server/voice/session.py) | 设备的语音命令：VOICE_CTL/VOICE 包和结果队列（`session.py`）、到 muse-asr 的 Unix 套接字桥（`asr.py`）、规则优先的意图引擎（`intent.py`、`match.py`、`numbers.py`、由 `tools/make_pinyin_table.py` 生成的 `pinyin_table.py`）、带明确设备的中转调用（`relay_client.py`）。运维说明见 [`server/README.zh_CN.md`](../../server/README.zh_CN.md#语音控制)。 |
| [`server/relay/voice.py`](../../server/relay/voice.py) | 给媒体服务的 `GET /api/voice/catalog`。`device` 字段本身在 `web.py` 的 `Relay.find_device()`/`_pick()`（单用户时也认名字；有 `--db` 时带密钥点名设备的请求按那台设备的访客处理，`_DEVICE_SCOPED`）。 |
| [`server/relay/voicecmds.py`](../../server/relay/voicecmds.py) | 中转这边的第二批语音命令：每台设备的换台历史、定时（后台定时检查：背光调到最暗，再发 value 为 1 的 `pause`，媒体服务不会 30 分钟后自动继续）、投屏历史、订阅正在放的投屏的作者、删台；目录里加的字段。存在 `relay-state.json` 的 `voice` 或设备的 `device_state` 里。 |
| [`server/relay/wifi.py`](../../server/relay/wifi.py)、[`server/tenancy/wifi.py`](../../server/tenancy/wifi.py)、[`server/wifi_crypto.py`](../../server/wifi_crypto.py) | 手机/电脑管理设备保存的 Wi-Fi（只有主人）：`GET`/`POST /api/wifi`、为设备加密、表 `device_wifi`（记住的公钥、名字、等回答的修改、回答）。 |
| [`server/fault.py`](../../server/fault.py) | 面向受控设备实验（B02-R）的故障注入工具；不在正式发布的服务端路径里。 |

## 4. 做需求时真正会用到的工具

`tools/` 下文件很多，这些是需求最常用到的：

| 需求 | 工具 |
| --- | --- |
| 在浏览器里增删/排序频道列表 | [`tools/channel_config.py`](../../tools/channel_config.py)（写 `channels.txt`） |
| 一起启动服务端和频道配置页 | [`tools/launch.py`](../../tools/launch.py)（`run.sh`/`run.bat` 是它的包装） |
| 把服务端打包成单个可执行文件 | [`tools/build_server.py`](../../tools/build_server.py)、[`packaging/tv-server.spec`](../../packaging/tv-server.spec)、[`.github/workflows/build-server.yml`](../../.github/workflows/build-server.yml) |
| 构建/校验固件镜像 | [`tools/validate.sh`](../../tools/validate.sh) `--prototype`、[`tools/verify_firmware.py`](../../tools/verify_firmware.py)、[`sdkconfig.av-prototype`](../../sdkconfig.av-prototype) |
| 给测试机的 NVS 写入 Wi-Fi/服务器地址 | [`tools/set_wifi_cred.py`](../../tools/set_wifi_cred.py)、[`tools/set_server_addr.py`](../../tools/set_server_addr.py) |

## 5. 线协议——一份契约，写在四个地方

改这里任何东西都要按下面顺序更新全部四处（先改设备端结构体，因为它是已刷设备无法再协商的部分）：

1. [`main/av_protocol.h`](../../main/av_protocol.h)——设备端权威结构体/常量。
2. [`server/protocol.py`](../../server/protocol.py) + [`server/frames.py`](../../server/frames.py)——服务端镜像。
3. [`docs/local-tv-prototype.md`](../local-tv-prototype.zh_CN.md)——设备侧叙述文档（归属、生命周期、时序预算）。
4. [`server/README.md`](../../server/README.zh_CN.md)——服务端叙述文档（调度、边界、关闭流程）。

## 6. 已经整理好上下文的文档——直接打开，不要重新推导

| 文档 | 什么时候读 |
| --- | --- |
| [`docs/development/handoff-20260928.md`](../development/handoff-20260928.zh_CN.md) | **做性能优化或换架构之前必读。**硬件推导的理论上限与真机基准计划。 |
| [`docs/development/tcp-delta-progress-20260930.md`](../development/tcp-delta-progress-20260930.zh_CN.md) | **继续做产品 TCP 路径优化之前读。**产品的帧率上限是限速器的预算而不是链路；流式条带接收、64 KB 窗口、delta 编码、当前问题。UDP 改造已搁置。 |
| [`docs/development/server-optimisation-handoff-20260930.md`](../development/server-optimisation-handoff-20260930.zh_CN.md) | **做服务端或画质相关工作前。**按频道固定帧率、逐帧拟合字节目标、测到了什么和没测到什么、按顺序的下一步。 |
| [`docs/development/state-20260916.md`](../development/state-20260916.zh_CN.md) | **动时序/会话/画质之前必读。** 当前已修/未修/已测的状态；动手前先跟代码核对。 |
| [`docs/development/metrics-dictionary.md`](../development/metrics-dictionary.zh_CN.md) | 使用或打印任何计数器之前——它到底在数什么，不能被读成什么。 |
| [`docs/development/live-sender-v2.md`](../development/live-sender-v2.zh_CN.md) | 动 `server/live_sender.py` 之前。 |
| [`docs/local-tv-prototype.md`](../local-tv-prototype.zh_CN.md) / [`server/README.md`](../../server/README.zh_CN.md) | 设备端与服务端各自的协议、归属、内存与生命周期叙述。 |
| [`docs/CHANGELOG.md`](../CHANGELOG.zh_CN.md) | 项目自己记录的、已经发布过什么。 |

## 7. 快速任务路由（在 `AGENTS.md` 的表基础上，补齐本 fork 的电视功能面）

| 新需求提到... | 从这里开始 |
| --- | --- |
| 频道列表，增删/排序频道 | `channels.txt`、`tools/channel_config.py`、`server/live.py`（`CHANNELS`、`reload_channels_if_changed`）、`main/av_channel_policy.c` |
| 直播台的备用线路、自动换线路、最快的线路、电脑播放器里换线路、「线路被拒绝 / 源站超时」这类提示 | `server/routes.py`、`server/relay/watch.py`（`WatchStream._run`）、`server/tv_server.py`（`route_*`）、`server/reconnect.py`、`server/relay/routecheck.py`、`server/relay/linecheck.py`、`server/relay/subscriptions.py`、`server/relay/linequality.py`（及 `tests/test_live_routes.py`、`tests/test_relay_linecheck.py`、`tests/test_relay_subscriptions.py`、`tests/test_relay_linequality.py`） |
| UP 主频道、视频网站、手机网页 | `server/relay/`、`packaging/systemd/tv-relay.service`、`packaging/nginx/tv-relay.conf` |
| 产品网站（静态页、屏幕图） | `packaging/site/`、`tools/render_ui/site_shots.py`、`packaging/nginx/tv-site.conf`、[`docs/development/host-setup.zh_CN.md`](../development/host-setup.zh_CN.md#产品网站) |
| 生产主机：systemd unit 和 drop-in、安装顺序、备份 | [`docs/development/host-setup.zh_CN.md`](../development/host-setup.zh_CN.md)、`packaging/systemd/` |
| 自建服务包：在家里跑服务器（Docker 镜像、NAS、首次运行建管理员、多台同时看、网页刷机）——只有设计，尚未实现 | [`2026-10-15-self-hosting-design.zh_CN.md`](../superpowers/specs/2026-10-15-self-hosting-design.zh_CN.md) |
| 电脑客户端（Windows/macOS 托盘程序，包着自建核心）、给在外面的手机用的云端遥控通道（中继）、带有效期和限额的可选服务器账号——只有设计，取代自建服务包的方向 | [`2026-10-19-pc-client-architecture-design.zh_CN.md`](../superpowers/specs/2026-10-19-pc-client-architecture-design.zh_CN.md) |
| 家庭媒体中心界面：信息架构（首页、直播、节目单、剧集、UP 主、片库、搜索、设备、设置）、「播放到」目标选择器、遥控面板、手机和桌面布局、首页汇总和统一播放接口、本地片库计划——只有设计和可点击原型，没实现 | [`2026-10-24-media-center-ui-design.zh_CN.md`](../superpowers/specs/2026-10-24-media-center-ui-design.zh_CN.md)、[`docs/design/media-center/index.html`](../design/media-center/index.html) |
| 电脑客户端的核心（阶段 P1、P3a）：监督进程的 `--desktop` 模式、打包版的子命令分派、本机控制接口（约定在设计第 5.13 节）、电脑版登录链接、轮转日志、诊断信息、带镜像的 ffmpeg 下载 | `server/selfhost/desktop_main.py`、`desktop.py`、`control.py`、`diagnostics.py`、`supervisor.py`（`child_command`）、`server/relay/setup.py`（`/local-login`）、`tools/core_entry.py`、`tools/packaged_entry.py`（`run_core`）、`tools/subcommands.py`、`tools/ffmpeg_fetch.py`、`packaging/desktop/rootv-core.spec`、`tools/build_core.py`、`tools/smoke_test_core.py`、`.github/workflows/build-desktop-core.yml`（及 `tests/test_selfhost_desktop.py`） |
| 电脑客户端的托盘和安装包：托盘菜单、第一次运行、只开一个、阻止睡眠、登录时启动、DMG / Inno Setup / Linux 压缩包（阶段 P2） | `desktop/`（+ `tests/test_desktop_tray.py`）、`packaging/desktop/`、`.github/workflows/build-desktop.yml`、[`docs/desktop/developer.zh_CN.md`](../desktop/developer.zh_CN.md) |
| 改名为 RooTV（小鼠电视，媒体中心设计第 15 节的 R 阶段）：名字、XiaozhiTV 数据和日志文件夹的一次性搬家、替换旧的登录启动项和退出旧托盘、从品牌 SVG 生成的应用/托盘/网页图标 | `tools/desktop_folders.py`、`desktop/legacy.py`、`desktop/autostart.py`（`legacy_for_this_os`）、`desktop/icons.py` + `desktop/assets/`、`tools/make_icons.py`、`packaging/desktop/icons/`、`packaging/desktop/rootv.iss`（`PrepareToInstall`）、`tests/test_desktop_rename.py` |
| 手机遥控、登录、分组、设备频道列表的两级 | `server/relay/web.py` 与 `assets/remote.html`、`server/channel_groups.py`、`main/ui_menu.c` |
| 用户、邀请、设备绑定、访客、谁能遥控哪台设备 | `server/tenancy/`、`server/relay/tenants.py`、`assets/join.html`、`docs/superpowers/specs/2026-10-12-multi-tenant-design.zh_CN.md` |
| 电脑版、扫码登录、让手机登录、登录列表 | `server/relay/console.py`、`server/relay/home.py`、`server/relay/logos.py`、`assets/console.html`、`assets/console.css`、`assets/console.js`、`assets/login.html`、`assets/scan.js`、`assets/phone.html`、`server/tenancy/phonelinks.py`、`server/tenancy/tickets.py`（及 `tests/test_relay_console.py`、`tests/test_relay_home.py`） |
| 电脑版首页的货架（两头箭头、鼠标滚轮、「全部 ›」弹窗、按 UP 主筛选）、大图按尺寸铺满还是放海报、搜索里看一个 UP 主的投稿、重启后 B 站封面 | `assets/console.js`（`shelf`、`heroLook`、`openAll`、`openSpace`，纯函数那一段由 `tests/console_shelves_test.mjs` 测）、`server/relay/home.py`（`CoverWarm`）、`server/relay/bilisearch.py`（`/api/bili/space`、缩略图尺寸 `h`）、`server/relay/bilibili.py`（`uploads_page`）（+ `tests/test_relay_home.py`） |
| 网盘组件（OpenList，AGPL-3.0）：固定版本的下载和 SHA-256 核对、按发布的 digest 更新、只听 127.0.0.1 的受监督子进程和它的包装、管理员密码、片库里受管的「网盘」来源、片库页的网盘面板 | `server/selfhost/openlist.py`（安装、`main` 包装）、`server/selfhost/__main__.py`（`openlist_port`、`openlist_child_args`）、`tools/subcommands.py`（`__openlist`）、`server/relay/clouddrive.py`（`/api/admin/clouddrive*`、`sync`）、`server/relay/library.py`（`ensure_managed`、`drop_managed`）、`assets/console.js`（`driveHTML`、`loadDrive`）（及 `tests/test_selfhost_openlist.py`） |
| 在小鼠电视里加网盘：OpenList 的管理接口（登录令牌，存储的列出 / 新建 / 更新 / 删除，驱动的默认值）、夸克 / UC 的 TV 版扫码登录（update 重跑驱动的 Init）、百度网盘经 OpenList 在线接口的刷新令牌、大白话的状态、令牌不给页面不进日志 | `server/relay/clouddrive.py`（`Api`、`storages`、`quark_start`、`quark_check`、`baidu_add`、`delete`、`rescan`、`drives`）、`server/relay/admin.py`（`/api/admin/clouddrive/storages`、`ROUTES`）、`assets/console.js`（`drivesHTML`、`driveQr`、`driveBaidu`）、`assets/console-admin.js`（操作记录的名字）（及 `tests/test_relay_clouddrive.py`） |
| 我的片库：OpenList 的网盘（WebDAV）、本机文件夹、扫描、`.nfo` 和海报、在电脑上放片库里的文件 | `server/relay/library.py`、`server/relay/web.py`（`_pc_library`）、`server/relay/admin.py`（`/api/admin/library*`）、`assets/console.js`（`renderLib`、`playLibrary`）（及 `tests/test_relay_library.py`） |
| 片库从豆瓣取片名、海报、评分和简介（不登录）、乱七八糟的文件名怎么认、手动匹配、在线资料的开关 | `server/relay/libmeta.py`（`Douban`、`parse_suggest`、`parse_mobile`、`parse_subject`、`pick`、`MetaStore`）、`server/relay/library.py`（`parse_name`、`unmask`、`junk_only`、`meta_step`、`match`、`serve_meta_art`）、`server/relay/admin.py`（`/api/admin/library/douban`、`/match`、`/meta/refresh`、`/settings`）、`assets/console.js`（`libInfoHTML`、`libMatchHTML`）（及 `tests/test_relay_libmeta.py`） |
| 片库怎么放（直接放还是封装、HEVC 只换封装、挑哪路声音）、探测、给 ffmpeg 的本机地址、字幕（菜单、WebVTT、外挂文件）、封装时照原样的时间轴和有上限的预读、网盘文件的索引区缓存（moov、MKV 的头和 Cues） | `server/relay/library.py`（`plan`、`media_of`、`watch_source`、`raw`、`serve_subs`、`_serve_cached`）、`server/relay/librarycache.py`、`server/relay/watch.py`（`Source.copy_video` / `maps` / `exact` / `bounded`、`hevc_codec`、`WatchStream._hold`）、`assets/console.js`（`LIB_CAPS`、`subsLoad`、`VOD_BUFFER`）（及 `tests/test_relay_library_play.py`、`tests/test_relay_library_cache.py`） |
| 总后台：用户、设备、流量、主机负载、操作记录、全局频道表 | `server/relay/admin.py`、`assets/console-admin.js` 和 `console-admin.css`、`server/relay/channeltable.py`、`server/relay/hoststats.py`、`server/tenancy/manage.py`（+ `tests/test_relay_admin.py`） |
| 导入时读播放列表的分组名（`group-title`、TXT 的 `#genre#`）、列表推荐的节目单（`x-tvg-url`，在节目单设置里推荐）、频道表里改台标和节目单编号、并入重复的台时补上空着的台标和 tvg 字段 | `server/m3u.py`（`epg_urls`）、`server/channel_groups.py`（`from_title`）、`server/relay/playlist.py`、`server/relay/channeltable.py`（`merge_meta`、`fill_meta`、`meta_problems`）、`server/relay/subscriptions.py`（`merge_lines`）、`server/relay/epg.py`（`suggest`、`admin_dismiss`）、`server/relay/logos.py`（`preview`）、`server/relay/admin.py`（`logo_preview`）、`assets/console-admin.js`（`achLogoEditor`、`achRenderEpgSuggest`）（+ `tests/test_relay_channel_meta.py`） |
| 没有登录时怎么进来：扫未绑定设备自己注册、设备恢复登录、管理员登录链接 | `server/tenancy/signup.py`、`server/tenancy/recover.py`、`server/tenancy/phonelinks.py`、`server/tenancy/admin.py`、`assets/join.html`、`assets/phone.html`、`main/av_recover.c`（及 `tests/test_account_recovery.py`、`tests/test_av_recover.c`） |
| 线协议/包格式改动 | `main/av_protocol.h`、`server/protocol.py`、`server/frames.py`，见上文第 5 节 |
| 语音命令、语音识别、一句话是什么意思 | `server/voice/intent.py`（及 `tests/test_voice_intent.py`）、`server/voice/session.py`、`docs/superpowers/specs/2026-10-12-voice-control-design.zh_CN.md` |
| 画质、帧率、条带/几何参数 | `main/av_protocol.h`（`AV_VIDEO_*`、`AV_FPS`）、`server/frames.py`、`server/rate.py`、`main/av_player.c` 的 `video_task`/`push_stripe` |
| 音画同步、drift、卡顿、会话重置 | `server/timeline.py`、`server/pts.py`、`main/av_player.c` 的 `audio_task`/`video_task`、`docs/development/state-20260916.md` |
| 设备按键、屏幕菜单、横幅、音量/亮度 HUD | `main/ui_menu.c`/`.h`、`main/av_player.c` 的 `process_key()`、`main/av_settings.c` |
| 语音控制、麦克风 | `main/av_voice_policy.c`、`main/av_voice.c`、`components/bsp/src/*/bsp_mic.c`、`components/bsp/src/bsp_button_events.c`、`docs/superpowers/specs/2026-10-12-voice-control-design.md` |
| Wi-Fi/服务器地址配网流程、captive portal | `main/av_provision.cpp`、`main/av_provision_policy.c`、`main/av_player.c` 的 `run_setup_mode()` |
| 服务端打包/分发/发布 | `tools/build_server.py`、`packaging/tv-server.spec`、`.github/workflows/build-server.yml` |
| 固件构建、CI、发布产物 | `tools/validate.sh`、`sdkconfig.av-prototype`、`.github/workflows/build-firmware.yml` |
| BSP 层硬件（引脚、总线、显示、音频、电池） | 与 `AGENTS.md` 一致——先看 `components/bsp/include/bsp_pins.h` |
| 不在本表里的任何需求 | 退回 `AGENTS.md` 自己的路由表，再看 `docs/development/ai-guide.md` |

## 8. 继承自 `AGENTS.md` 的约束——不在此重复

Flash 布局（`cardid@0x356000`、3 MB 应用上限）、只用公开版做验证、
`bsp_lvgl_lock()`、按键回调不阻塞、文档双语配对规则等，全部原样适用。
以 [`AGENTS.md`](../../AGENTS.md) 当前文字为准——本文件只是在它之上加一张地图，绝不是它的第二份拷贝。
