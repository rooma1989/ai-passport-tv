<p align="right">
  <strong>简体中文</strong> · <a href="2026-10-15-self-hosting-design.md">English</a>
</p>

# 自建服务包（「小智电视」在家里跑）—— 设计

状态：2026-10-15 草案，2026-10-16 已决定（见下面「决定」）；2026-10-22 加了公开播放列表预设（第 8.2 节）；2026-10-23 实现了 YouTube 本机模式（S5b，第 8.4 节）。阶段 S1 和 S3 已实现，就是 0.x 测试版的代码（第 15 节，写明了哪些验证过、哪些没有）；S2、S4–S6 仍是设计。承接
[`2026-10-12-multi-tenant-design.zh_CN.md`](2026-10-12-multi-tenant-design.zh_CN.md)
（账号、绑定、PC 控制台、总后台：阶段 1–3 已完成；阶段 4 媒体网关没做）、
[`2026-10-12-phone-audio-design.zh_CN.md`](2026-10-12-phone-audio-design.zh_CN.md)、
[`2026-10-12-voice-control-design.zh_CN.md`](2026-10-12-voice-control-design.zh_CN.md)、
[`2026-10-13-wifi-setup-design.zh_CN.md`](2026-10-13-wifi-setup-design.zh_CN.md) 和
[`2026-10-13-sound-provisioning-design.zh_CN.md`](2026-10-13-sound-provisioning-design.zh_CN.md)。

需求：你那台国内小云主机（实际约一个核、1.8 GB 内存、出网 3 Mbit/s）继续给你自己和几个朋友用。
其他人用**自建服务包**：一个 Docker 镜像、一套教程和一个网页刷机工具，每个家庭在家里的旧笔记本、
NAS、迷你主机或 Mac 上跑服务器，设备和它在同一个局域网。这样不花云带宽、画质最好，家里的 IP 也不像
云主机 IP 那样被 B 站拒绝。镜像**不带任何内容**：没有频道表、没有直播地址、没有 cookies。第一个注册的人
成为管理员。

已经和你定下、这里不再讨论的：Netlify 和 Cloudflare 跑不了媒体服务（没有常驻进程和 ffmpeg，设备说的是
原始 TCP，Cloudflare 免费版在大陆没有节点、CDN 还限制视频）；Cloudflare Tunnel 可以把家里服务器的网页
暴露出去；Netlify 或 Cloudflare Pages 可以托管静态网站、教程和网页刷机；Oracle Cloud 永久免费是唯一值得
一提的免费虚拟机，而且在海外。

下文用语：**家庭服务器**指家里跑这个包的机器；`<your-host>` 是它的局域网地址或名字；`<static-site>` 是
项目的静态网站；`<owner>` 是项目在 GitHub 或镜像仓库的命名空间。第 13 节的数字来自 2026-10-16 在 M1 Pro 上的压测
（单路成本已测完，上限测试中途停止）；标了**推算**或**预期**的都还没实测。

## 1. 起点：现在哪些地方默认是云主机

代码离自建已经很近：媒体服务和中转是标准库 Python 加 ffmpeg，多用户模式用 SQLite、不需要守护进程，
公开固件已经带每台设备自己的身份、没有共享令牌。挡路的是这些：

| 前提 | 在哪 | 为什么要紧 |
| --- | --- | --- |
| 没有管理员时中转拒绝启动，而管理员只能由「从单用户文件迁移」建出来 | `server/relay/__main__.py` 的 `_serve_tenants()`；`server/tenancy/migrate.py` | 新家庭没有东西可迁移 |
| 中转的网页只监听回环，默认前面有 nginx（`/tv/`、`/TV/R/`、`/TV/B/`、`/TV/L/`、`/TV/P/`、`/TV/W`、`/tv/listen/<route>/`） | `server/relay/web.py` 的 `make_servers()`；`packaging/nginx/tv-relay.conf`、`tv-listen.conf` | 需要一个在 Docker、Mac、NAS 上表现都一样的前端 |
| 登录 cookie 总是带 `Secure` | `server/relay/web.py` 的 `_cookie_header()` | 浏览器不保存经局域网 `http://` 设置的 `Secure` cookie，登录会留不住（主屏幕图标那种地址里带 `?s=` 的方式仍然有效） |
| 只有对端是回环时才看 `X-Real-IP` 取调用方地址 | `server/relay/web.py` 请求处理里的 `_ip()` | 经隧道进来的请求会看起来都是同一个地址，按网段的限流会把所有人算在一起 |
| 每个板型一个媒体服务进程，一次只服务一个连接，其余的关掉 | `server/tv_server.py` 的 `AVServer.serve()` / `_reject_waiting()`；单元 `tv-xingzhi`（8098）、`tv-passport`（8097） | 家里有两台星智，一次只能一台看 |
| 内置兜底频道、代码旁边的仓库 `channels.txt`、频道页默认的社区播放列表地址 | `server/live.py` 的 `BUILTIN_CHANNELS` 和 `load_channels()`；`channels.txt`；`tools/channel_config.py` 的 `DEFAULT_SOURCE` | 镜像不能带内容 |
| 频道表为空算错误 | `server/live.py` 的 `parse_channels()`（「channel table contains no channels」） | 新家庭一开始没有直播频道 |
| 声波配网页把服务器一栏填成页面自己的主机名 | `server/relay/assets/wifi.html` 的 `siteHost()` | 经隧道打开时会填隧道的名字，设备连不上 |
| YouTube 节点要求 TLS 和固定证书，没有代理设置 | `server/relay/ytnode.py`（`--cert`、`--tls-key`）、`server/relay/youtube.py` | 大陆的家里只能通过用户自己已有的代理访问 YouTube |
| 语音要 `muse-asr`，它在另一个仓库 | `server/voice/asr.py`（Unix 套接字 `POST /asr`） | 需要一个自己的小识别容器 |
| 打包好的局域网服务器（`tools/launch.py`、PyInstaller 构建）是单用户的，用 8096（媒体）和 8097（频道页） | `tools/launch.py`、`packaging/tv-server.spec` | 多用户方案里 8097 是护照的媒体端口 |
| 固件：公开构建没有默认服务器、不知道网站；只用 IPv4；只有一个 factory 应用分区，没有 OTA；星智的 4G 模块被置为飞行模式 | `main/av_config.h`、`main/Kconfig.projbuild`（`AV_DEFAULT_SERVER`、`AV_PUBLIC_SITE`）；`main/av_player.c` 里的 `hints.ai_family=AF_INET`；`partitions.csv`；`docs/boards/xingzhi-cube-154.zh_CN.md` | 决定了配网流程、「出门在外」和固件更新的做法 |

已经合适的：声波配网页有服务器一栏，并按打开它的地址填好；设备的声波配网页在不知道任何网站时也能用
（「手机打开遥控页的 /tv/wifi」）；不带端口的服务器地址会补上板子自己的端口（星智 8098、护照 8097，
`av_server_addr_use_build_port()`）；未绑定的设备只收到「绑定这台电视」的静止画面、不转码
（`server/tenancy/unbound.py`）；借未绑定设备自己注册、设备恢复登录和总后台都已经有了；CONFIG 的
`remote_url` 既接受 `HTTPS://` 也接受 `HTTP://`（`server/tenancy/gate.py` 的 `_REMOTE_URL`，最多 64 个字符）。

## 2. 目标和非目标

谁在用：**不懂技术的家庭**，**每家有一个懂点技术的人**做一次性设置（装 Docker 或 NAS 应用、给设备刷机、
加频道）。其他人只用手机遥控和设备。

目标：

1. **一个下载、一条命令**（或 NAS 上填一张表）就拉起一个家庭需要的全部：媒体服务、带账号的中转、网页、
   可选的语音、可选的 YouTube。
2. **首次运行除了读一行字不需要终端**：打印出地址和设置码，手机扫一下就建好第一个管理员。
3. **一台家庭服务器上多台设备同时看**，两种板型都行，每台设备填同一个地址。
4. **不带内容。** 用户自己加直播频道、UP 主、剧集；要 YouTube 的话用自己的节点或代理和 cookies。
5. **和你云主机上的代码相同**：这个包是运行 `server/` 的一种方式，不是它的分叉。多用户的东西全部复用。
6. **x86-64 和 arm64 镜像**，给不装 Docker 的 Mac 一条纯 Python 的路，Windows 和 NAS 有写好的说明。
7. **在浏览器里刷固件**：给公开构建的网页刷机工具。
8. **默认安全**：只在局域网、总后台不对外、没有共享令牌、密钥首次启动时生成且从不打印（一次性设置码除外）。

非目标：

- 给陌生人用的托管服务（你的主机仍只给亲友）。
- 把 Windows 原生服务、安卓、路由器固件、32 位 ARM（树莓派 3、Zero 2）作为支持的平台。
- 附带任何频道表、直播地址、cookie 或代理。（播放列表链接：2026-10-22 项目负责人决定按链接列出几个公开的第三方
  播放列表，见第 8.2 节；下面那个社区播放列表仍然不带。）
- 不需要用户折腾网络就让设备在外面用 4G 连回家（见 6.6 节）。
- 改线上协议、码率控制或计时代码。
- 第一版就做硬件视频解码（VAAPI、V4L2）；列为以后的优化。

## 决定（2026-10-16）

你接受了全部建议（Q1–Q12 都按每题的建议）。先做 0.x 测试版：S1 + S3 + 简短的 S4，每个型号一次一台（Q1 的 A）；1.0 版再做 S2 轻网关（Q1 的 C）。暂不做压测这类占用大量性能的工作，第 13 节的推算数字以后在真实硬件上补测。

## 3. 需要你决定的问题

每个问题都列了选项和建议；文档其余部分按建议写。

### Q1. 第一版就支持多台同时看吗？

| 选项 | 后果 |
| --- | --- |
| A. 用现在的媒体路径：每个板型一次一台 | 最快（不动媒体）。家里两台星智要轮流：第二台显示「服务器没有响应」并重试。 |
| B. 每台设备一个媒体进程、一个端口 | 不用写新的媒体代码，但每多一台设备就要在它的配网页上输自己的端口，声波配网不知道该发哪个端口（5.2 节）。 |
| **C. 现在就做一个「轻网关」：每个型号端口一个监听，把每台设备交给它自己的工作进程** | 3–4.5 个 agent-日。所有设备一个地址。它就是多租户设计阶段 4 的核心，去掉云主机才需要的部分，以后云主机直接复用。 |

**建议：1.0 版用 C**；C 做出来之前，给少数朋友的 0.x 测试版用 A 也可以。

### Q2. 怎么证明第一个管理员？

| 选项 | 风险 |
| --- | --- |
| **A. 一次性设置码**，打印在服务器日志和启动横幅里（还有一个终端二维码，打开就是填好设置码的设置页） | 需要看一眼日志，而做设置的人本来就在看（`docker compose up`、NAS 的日志页）。设置前就开了隧道，也没法借它抢走服务器。 |
| B. 局域网里谁先注册就是谁 | 最简单；但连着家里 Wi-Fi 的任何客人，或者经过一个开得太早的隧道的任何人，都可能成为管理员。 |
| C. 谁先绑定设备就是谁 | 需要碰到设备；但设备得先刷好、先联网，教程顺序要倒过来。 |

**建议：A。** 「第一个注册的人成为管理员」这个要求保留；设置码只是证明这第一个人就是装服务器的那个人。

### Q3. 包里的网页前端

| 选项 | 后果 |
| --- | --- |
| **A. 一个只用标准库的小 Python 前端**（`server/front.py`），包里用它代替 nginx | 在 Docker、Mac、NAS 上一样；能读网关写下的每台设备的收听端口；能做 6.3 节的双端口划分。约 300 行加测试。 |
| B. 镜像里放 nginx（或 Caddy） | 久经考验，配置现成；但 Mac 上的纯 Python 路也得装 nginx，按设备的收听路由要生成配置并重载。 |

**建议：A。** 一个家庭只有几个客户端；nginx 留在云主机上。

### Q4. 镜像发布到哪？

大陆访问 Docker Hub 不稳定，GHCR 也慢。**建议：** 同时发布到 GHCR（`ghcr.io/<owner>/xiaozhi-tv`）和
阿里云容器镜像服务的免费个人版命名空间（大陆快），并在每个发布里附上各架构的离线 `docker save` 包，
给访问不了镜像仓库的 NAS 用户。

### Q5. 静态网站放在哪？

| 选项 | 说明 |
| --- | --- |
| **A. Cloudflare Pages，用自定义子域名** | 免费、有 HTTPS（Web Serial 需要）、不占云主机的 3 Mbit/s。默认的 `*.pages.dev` 在大陆访问出过问题；自定义域名能避开大部分。 |
| B. 你现有的网站 | 国内快；固件下载（每个 2–3 MB）会和你的视频流抢 3 Mbit/s。 |
| C. GitHub Pages（上游的刷机页在那里） | 大陆部分地区慢或打不开。 |

**建议：A**，固件同时附在 GitHub Releases 上；如果大陆用户反映下载慢，以后再给固件加一个国内对象存储镜像。

### Q6. 中文教程里 YouTube 讲到什么程度？

大陆不通过代理访问不了 YouTube，而这个包不能附带、也不能推荐代理。**建议：** 功能保留，表现为一个通用的
「YouTube 节点地址」或「HTTP 代理地址」设置；中文教程写明有这个设置，说明它是给大陆以外的家庭或已经有
自己节点的人用的，不写任何获取代理的方法；英文教程相同，cookies 部分写得更细。

### Q7. 包里要不要语音？

**建议：** 做成可选的第二个容器（`xiaozhi-tv-asr`，SenseVoice-Small 经 sherpa-onnx，和 `muse-asr` 一样），
默认关闭，因为它要多占约 550 MB 内存（云主机上 `muse-asr` 的实测）。模型在第一次启用时下载，不打进镜像，
这样模型许可由用户自己接受（决定前先查模型的许可，见 17 节）。

### Q8. 外网访问和出门在外的设备

**建议：** 默认什么都不开。教程给**网页**两个可选做法：Tailscale（私有，适合用手机听）和用家庭自己域名的
Cloudflare Tunnel（可分享的链接、远程遥控）。**出门在外的设备**只给一个「进阶」做法的说明（路由器端口转发
加 DDNS，或者经用户自己的 VPS 用 frp）；项目不运营任何中继（6.6 节）。

### Q9. 哪些板子是一等公民？

**建议：** 星智 Cube 1.54 是一等公民（公开构建，网页刷机先擦除）。AI 护照的独立公开构建通过同一个刷机页支持
（绝不擦除）。和 Muse 并存的护照构建仍然是「自己编译」，除非你确认 Muse 的签名密钥可以在 CI 里用（9.4 节）。

### Q10. 固件更新

**建议：** 目前用网页刷机重刷（不擦除时设置和身份都保留）。从家庭服务器 OTA 需要新的分区表（每台星智都要
擦一次）和签名镜像；等第一版有了用户再决定（9.5 节）。

### Q11. 公开构建可以回落到你自己的服务器吗？

**建议：不可以。** 公开构建没有默认服务器（`CONFIG_AV_DEFAULT_SERVER` 为空）。用你主机的朋友照旧用烧了你
地址的单独构建。否则每个自建用户的设备在地址填错时都会去连你那台 3 Mbit/s 的主机。

### Q12. 检查更新和遥测

**建议：** 没有遥测。总后台每天从 `<static-site>` 取一次很小的 `latest.json`（不带任何标识、不带 cookie），
用来提示「有新版本」；有一个设置可以关掉。

## 4. 这个包

### 4.1 结构

一个核心容器运行所有通过回环或共享数据目录通信的进程；可选的容器加上语音和隧道。

```
container "tv" (image xiaozhi-tv)
 supervisor  python3 -m server.selfhost            PID 1 (via tini), restarts children, backups
 ├── front       :8080 LAN pages (all)   :8081 public pages (tunnel only, optional)
 │                 -> 127.0.0.1:8100 relay pages, 127.0.0.1:<listen port> per device
 ├── relay --db  127.0.0.1:8099 streams (no key, loopback only), 127.0.0.1:8100 pages
 ├── gate        :8097 :8098 (phase S2; phase S1 runs one media server per model there)
 │    └── worker per playing device  (today's AVServer in worker mode, ffmpeg child)
 └── ytnode      127.0.0.1:47443, only with YouTube in "local" mode (section 8.4)

container "asr"     optional, profile "voice": POST /asr on /data/run/asr.sock
container "tunnel"  optional, profile "tunnel": cloudflared -> tv:8081
```

为什么一个核心容器而不是每个进程一个：中转的播放流端口和收听端口按设计只在回环上（它们没有密钥，见
`packaging/systemd/tv-relay.service`）；拆成多个容器就得把它们放到网络上。这些进程本来就通过同一个数据目录里
的文件通信（`TV_NOW_PLAYING_FILE`、`TV_RELAY_EVENTS_FILE`、`devices/<id>/` 下每台设备的文件），一个数据卷
就自然满足。监护进程沿用 `tools/launch.py` 的做法（启动、看着、重启、用通俗的中文报告）。

### 4.2 镜像

- **基础：** `python:3.12-slim`（Debian）。只用标准库；不装 numpy（默认的画面降级 `steady` 不需要它）。
- **ffmpeg：** Debian 的动态链接包。全静态构建在云主机上解析主机名时崩溃过
  （[`xingzhi-cube-154.zh_CN.md`](../../boards/xingzhi-cube-154.zh_CN.md)）；每个发布固定版本，冒烟测试覆盖。
- **架构：** `linux/amd64` 和 `linux/arm64`，CI 里用 `docker buildx` 从同一个 Dockerfile 构建。不支持 32 位 ARM。
- **YouTube 工具**（yt-dlp、deno）不进核心镜像：会多出约 100 MB，而且每周要更新。本地 YouTube 模式在第一次
  启用时把它们下载到 `/data/youtube/bin/`、校验哈希，按 `packaging/yt-relay/update-ytdlp.sh` 的做法更新
  （测试视频解析成功才换）。
- **用户：** 以非特权用户运行。`PUID`/`PGID` 环境变量设定它的 id，这是 NAS 用户熟悉的约定，这样他们在 NAS
  界面里建的数据文件夹可写。
- **大小：** 预期解压后 250–350 MB；阶段 S1 实测。
- **不进构建上下文**（`.dockerignore`）：`channels.txt`、`server/.local/`、`main/av_private_config.h`、任何
  `*.pem`、`token`、`relay-key`、cookies。CI 检查镜像，只要里面有 `channels.txt` 或 `BUILTIN_CHANNELS` /
  `DEFAULT_SOURCE` 的字符串就让构建失败。
- **许可：** 一个 `NOTICE` 列出 ffmpeg（Debian 的 GPL 构建，附 Debian 源码包的指引）、Python、jsQR（已随许可
  一起放在仓库里）和本仓库的 MIT 许可。

### 4.3 数据布局

一个家庭的全部东西都在一个文件夹里，挂载到 `/data`：

```
/data/
  config.toml             settings (section 4.4); created with defaults on first start
  secrets/                mode 0700
    relay-key             generated: relay key, break-glass link, listen token secret
    wifi-key              generated by the relay (X25519, for sealed Wi-Fi changes)
    setup-code            only while no admin exists; deleted when the admin is made
    youtube-node.json     optional, user-supplied: {url, key, cert} of a remote node
  youtube/                optional: cookies.txt (the user's own), bin/ (yt-dlp, deno)
  tv.db  tv.db-wal  tv.db-shm
  devices/<id>/           per-device files (now-playing, events, channel table, groups, remote URL)
  channels.txt            the live table; empty at first
  backups/                nightly tv-YYYYMMDD.db, kept 14 days
  exports/                user exports from the admin backend
  run/                    sockets and the gate's worker registry (not backed up)
  models/                 optional, the voice model
```

除了 `run/` 整个文件夹都要备份。`secrets/` 和 `youtube/cookies.txt` 是敏感的；教程的备份一节会写明。

### 4.4 配置文件

TOML（用标准库的 `tomllib` 读，Python 3.11+）。首次启动时带注释写出；总后台的「系统」页在阶段 S1 只读
展示它，以后再让它改那些安全的键。

```toml
[server]
name = "Home"                 # shown in page titles and on the setup page
lan_port = 8080               # phone remote, PC console, admin, setup: LAN only
public_port = 0               # 8081 to offer pages to a tunnel (section 6.3); 0 = off
device_address = ""           # what devices dial; empty = learnt at first setup (section 6.1)
public_url = ""               # e.g. https://tv.example.com when a tunnel exists; empty = LAN URL

[devices]
allow_internet = false        # TV_ALLOW_PUBLIC: accept device connections from public addresses
max_streams = 0               # 0 = no cap; otherwise refuse "server busy" above it

[accounts]
signup_via_device = "lan"     # "lan" | "anywhere" | "off": self-registration with an unbound device
admin_over_tunnel = false     # admin backend and break-glass key link on the public port

[youtube]
mode = "off"                  # "off" | "node" (remote node) | "local" (yt-dlp in this container)
proxy = ""                    # local mode only: http://host:port, used by yt-dlp and ffmpeg
cookies = "youtube/cookies.txt"

[voice]
asr_socket = ""               # "/data/run/asr.sock" when the asr container runs

[maintenance]
backup_keep_days = 14
update_check = true           # daily latest.json from the static site, no identifiers
```

（配置文件里的注释实际用中文写；这里与英文版保持一致。）

### 4.5 首次启动

监护进程在启动任何东西之前：

1. 文件夹和 `config.toml` 不存在就建；检查能否写 `/data`，不能就说该怎么办（NAS 上最常见的问题）。
2. 没有 `secrets/relay-key` 就生成一个（`secrets.token_urlsafe(32)`，0600）。`wifi-key` 由中转自己建（`--wifi-key`）。
3. 用现有的建表代码（`server/tenancy/db.py`）打开或新建 `tv.db`，设 `meta.legacy_token = 0`（自建没有共享
   令牌；也没有令牌文件），记 `meta.selfhost = 1`。
4. 还没有管理员时：把新的设置码（用现有的码字母表，10 个字符，约 50 位）写到 `secrets/setup-code`，打印下面的
   横幅。有管理员之前每次启动都换一个新码。
5. 启动中转（`--db /data/tv.db`；自建模式下中转「还没有管理员」的拒绝启动改为「等待设置」：除 `/tv/setup`
   以外的页面都回答一个指向它的提示）、媒体那一侧、前端，以及可选的 YouTube 节点。

横幅（实际是中文）：

```
Xiaozhi TV is running.
  Open on a phone or computer on this network:  http://<lan address>:8080/tv/setup
  Setup code: K7P4-QX2M9   (one use; needed to create the administrator)
  [terminal QR code of http://<lan address>:8080/tv/setup#K7P4QX2M9]
  Devices: enter <lan address> as the server address (no port needed).
```

只有主机网络或纯 Python 方式下才知道 `<lan address>`；在桥接网络的容器里，横幅写「这台电脑的地址」，
由设置页按浏览器用的地址填上（6.1 节）。

### 4.6 Compose 文件（大致样子）

```yaml
services:
  tv:
    image: ghcr.io/<owner>/xiaozhi-tv:1
    restart: unless-stopped
    ports: ["8080:8080", "8097:8097", "8098:8098"]
    volumes: ["./data:/data"]
    environment: [PUID=1000, PGID=1000, TZ=Asia/Shanghai]
    logging: {driver: json-file, options: {max-size: 10m, max-file: "3"}}
  asr:
    image: ghcr.io/<owner>/xiaozhi-tv-asr:1
    profiles: [voice]
    restart: unless-stopped
    volumes: ["./data/run:/run/asr", "./data/models:/models"]
  tunnel:
    image: cloudflare/cloudflared:<pinned version>
    profiles: [tunnel]
    restart: unless-stopped
    command: tunnel --no-autoupdate run
    environment: [TUNNEL_TOKEN=${TUNNEL_TOKEN}]
```

`docker compose up -d` 启动核心；加 `--profile voice` 或 `--profile tunnel` 再启动其他的。所有平台默认都用桥接
网络：在 Linux 上 Docker 对发布的端口保留设备的源地址，媒体服务的私网地址检查和审计要用它。Linux 上另给一个
注释掉的主机网络写法，主要是为了让横幅能打出局域网地址。

### 4.7 不用 Docker（Mac、Linux）

`./run.sh --selfhost [--data <文件夹>]` 从源码或发布包运行同一个监护进程。需要 Python 3.11+ 和 ffmpeg
（Mac 上用 Homebrew：`brew install python@3.12 ffmpeg`）；`tools/launch.py` 现有的检查会报出缺什么以及安装
命令。数据文件夹默认用 `tools/datadir.py` 已经会选的那个用户目录。语音容器在阶段 S1 没有纯 Python 的对应
（只有 Docker）。

教程会讲的 Mac 细节：有人在看时 Mac 不能睡眠（系统设置 → 能耗：接电源时防止自动睡眠）；第一次启动会弹
macOS 的「接受传入连接」提示，要允许 Python。

### 4.8 Windows

Docker Desktop 加 WSL 2，用同一个 compose 文件。教程会讲：开启 WSL 2（要重启一次）、第一次发布端口时 Windows
防火墙对 Docker 后台的提示（勾「专用网络」）、睡眠设置。以后也可以做一个自建模式的原生
`tv-server-windows-amd64.exe`（单用户局域网服务器已经有 PyInstaller 构建），但网关要用的
`socket.send_fds` 在 Windows 上没有，所以它只能每个型号一次一台；不在计划里。

### 4.9 NAS

- **群晖**（DSM 7.2+，Container Manager），x86 型号（赛扬 J4125、AMD 锐龙 R1600/V1500B）：用 compose 文件建
  一个「项目」，数据文件夹先在 File Station 里建好。ARM 的 j / value 系列（瑞昱 RTD1296/1619B）是 arm64 但很弱：
  推算最多一路。
- **绿联**（UGOS Pro，DXP 系列，Intel N100/i5）：Docker 应用，支持 compose；同群晖。
- **威联通**（Container Station）x86：同一个 compose。
- 教程列出的常见坑：文件夹权限（`PUID`/`PGID`）、端口被别的 NAS 应用占了（改 `lan_port` 和映射）、从镜像加速
  拉取或导入离线包（Q4）。

## 5. 一台主机上的多台设备

### 5.1 现在

云主机每个型号一个媒体服务（`tv-xingzhi` 8098，240x180，15 fps；`tv-passport` 8097，320x180，12 fps）。每个
一次服务一个连接，其余的关掉。两次会话之间每台设备的状态（待切的台、投屏结束后回到的台、开机时长）存在
`AVServer` 的属性里。多用户模式已经按 id 和密钥认出每台设备，并让这次会话用那台设备自己的文件
（`server/tenancy/gate.py` 的 `apply()`），所以**是哪台**设备已经解决了；**同时几台**没有。

### 5.2 选项

| | A. 每个型号一台（现在） | B. 每台设备一个进程一个端口 | C. 轻网关 + 每台设备一个工作进程 |
| --- | --- | --- | --- |
| 新代码 | 无 | 监护配置、每个进程的前端路由 | 监听、交接文件描述符、工作模式、工作进程登记表 |
| 设备上填的地址 | `<your-host>` | `<your-host>:<它的端口>`，每台手输 | `<your-host>` |
| 声波配网 | 可用 | 不知道该发哪个端口 | 可用 |
| 同时 | 1 台星智 + 1 台护照 | 配几个进程就几台 | 到 `max_streams` / 机器的上限 |
| 空闲开销 | 2 个 Python 进程 | 每台配置的设备一个，一直在 | 只有监听；设备离开 10 分钟后工作进程退出 |
| 云主机阶段 4 能否复用 | — | 不能 | 能 |
| 工作量 | 0 | 1–1.5 天 | 3–4.5 天 |

**建议：C**（Q1）。B 的每台设备一个端口打破了声波配网白送的「一个地址」，而且它的工作以后都要扔掉。

### 5.3 轻网关

就是多租户设计 7.3 节，去掉只有共享云主机才需要的部分：

- **监听** 8097 和 8098（两个公开构建在地址不带端口时拨的端口）。在现有的 250 ms（`IO_TIMEOUT`）内读完
  HELLO，照现在的办法经 `tenancy.gate` 认证。
- **按 HELLO 里的 `model` 选配置**，而不是按端口：`xingzhi-cube-154` 240x180/15 fps/200000，`ai-passport`
  320x180/12 fps/110000（多租户设计 7.4 节的表，做成一张小的内置表）。没有 `model` 的旧固件按端口的配置。
- **交接：** 网关经 Unix 套接字（`socket.send_fds`）把套接字和 HELLO 的字节交给这台设备的工作进程，没有就先
  启动一个：`python3 -m server.tv_server worker`，带这一型的 `TV_GEOMETRY`、`TV_FPS`、`TV_RATE_START` 和这台
  设备的文件路径。工作进程就是现在的 `AVServer`，只是连接来自网关而不是 `accept()`；设备离开后再留 10 分钟，
  这样待切的台和投屏的返回台能熬过换台。同一台设备以最新的连接为准。
- **启动时间：** 设备在 HELLO 之后等 CONFIG 2 秒。不带 numpy 的工作进程启动预期远低于此；阶段 S2 在树莓派 4
  （支持的机器里最慢的）上实测。如果那里太慢，网关为每个型号预留一个热备工作进程。
- **未绑定的设备**由网关自己用现有的静止画面回答（`server/tenancy/unbound.py`），不起工作进程、不起 ffmpeg。
- **上限：** `max_streams`（0 = 不限）；超过时设备收到 `ERROR {"reason":"server busy"}` 并重试。不分用户上限，
  不给管理员优先（只有一个家庭）。
- **用手机听：** 每个工作进程从一个池子（8200–8299）里拿一个自己的回环收听端口，把 `{device_id: port}` 记到
  `/data/run/workers.json`；中转的 `POST /api/listen` 回答 `/tv/listen/<设备 id>/live.m3u8?t=…`（路由从型号名
  改成设备 id），前端按记录把这个路径转给对应端口。
- **语音：** 每个工作进程拿同一个 `TV_ASR_SOCKET`；识别器一次处理一个请求，对一个家庭够用。
- **不移植的：** 按带宽的准入、按用户的上限、管理员优先、单独的媒体主机。

### 5.4 地址和端口

| 端口 | 给谁 | 暴露在 |
| --- | --- | --- |
| 8098 | 星智设备（它们构建里的默认端口） | 局域网 |
| 8097 | AI 护照设备（它们构建里的默认端口） | 局域网 |
| 8080 | 手机遥控、PC 控制台、总后台、设置页、声波配网页 | 局域网 |
| 8081 | 同样的网页，但没有总后台、设置页和密钥链接；只给隧道 | 只在容器网络里 |
| 8099、8100 | 中转的播放流和网页 | 容器内回环 |
| 8200–8299 | 每台设备的收听流 | 容器内回环 |
| 47443 | 本地 YouTube 节点 | 容器内回环 |

设备的服务器地址就是 `<your-host>`：不带端口，因为每个构建会补自己的端口。用旧构建刷过、存的是 `name:8096`
的设备，在 `.local` 名字或 IP 上会保留 8096；用配网页或声波配网重新设置一次即可。

## 6. 网络

### 6.1 家庭服务器的地址

设备需要一个不会变的地址。按稳妥程度：

1. **在路由器里给家庭服务器做 DHCP 地址保留**（教程给两三种最常见的路由器界面截图），然后用 IP。
2. **机器的 `.local` 名字**（`nas.local`、`my-mac.local`）。macOS、Windows 10+、装了 avahi 的 Linux 和大多数 NAS
   都会应答。在设备上，lwIP 的一次性 mDNS 查询（`CONFIG_LWIP_DNS_SUPPORT_MDNS_QUERIES`，ESP-IDF 默认开启）
   应当能经 `getaddrinfo()` 解析它；上游 README 让用户输入 `name.local:8096`，但本分支没有在任何一块板子上
   验证过（17 节）。安卓浏览器不能可靠解析 `.local`，所以给手机的二维码一律用 IP。

服务器怎么知道它：主机网络或纯 Python 方式下，监护进程列出本机的私网 IPv4 地址和 `.local` 名字。桥接网络的
容器看不到局域网地址，所以**首次运行页把浏览器用的主机记下来**（`/tv/setup` 请求的 `Host` 头，是私网 IPv4 或
`.local` 名字时）作为 `device_address`，管理员可以在「系统」里改。`public_url` 默认是
`http://<device_address>:<lan_port>`。

设备上显示的二维码（绑定、遥控、恢复登录）用大写的 `public_url` 拼出来，必须在 64 个字符以内（恢复登录 96）：
`HTTP://192.168.100.200:8080/TV/R/<20 个字符>` 是 53。超过约 35 个字符的隧道主机名放不下；总后台会提示。

### 6.2 给设备配网

```
 flash (web flasher, section 9)          device boots into setup (no network stored)
            │                                       │
            ▼                                       ▼
 phone opens http://<your-host>:8080/tv/setup ─► "Set up a TV" ─► /tv/wifi
   (from the banner's QR, or typed)               page: Wi-Fi name, password,
                                                  server = device_address (pre-filled)
                                                  "Send" plays it as sound
            │                                       │
            ▼                                       ▼
 device hears it, joins Wi-Fi, dials <your-host>:8098 (or 8097)
            │
            ▼
 unbound: "bind this TV" still frame with a code
 phone (signed in) scans or types it ─► device bound, plays the owner's channels
```

- **先用声波。** 设备的声波页不需要知道网站：它显示「手机打开遥控页的 /tv/wifi」（`av_player.c`，配网页
  `SOUND`）。这一页从家庭服务器打开，所以服务器一栏会填好。需要改的：页面从服务器拿 `device_address`
  （注入到 HTML 里），而不是用 `location.hostname`，这样经隧道打开时发出去的仍是局域网地址。
- **热点作为后备**（现有配网的第 2–4 页）：连上设备的热点，打开 `192.168.4.1`，填 Wi-Fi 和 `<your-host>`。
- **刷机页预填**（以后，只限星智，9.3 节）。
- 绑定用现有流程（`/TV/B/<绑定码>`）；第一台设备可以由刚在设置页登录的管理员来绑。

### 6.3 网页前端和它的两个端口

前端（Q3）提供 nginx 在云主机上提供的同样路径（`packaging/nginx/tv-relay.conf`、`tv-listen.conf`），另加
`/tv/setup` 和一个跳到遥控页的静态 `/`。它给中转设好 `X-Real-IP` 和 `X-Forwarded-Proto`。

它监听**两个端口**，让「互联网能碰到什么」由端口决定，而不是靠检查请求头：

- **局域网端口（8080）：** 全部。
- **公网端口（8081，默认关）：** 只给隧道。不在主机上发布；隧道容器经 compose 网络访问它（Tailscale `serve`
  同样指向它）。它拒绝 `/tv/admin`、`/api/admin/*`、`/tv/setup`、密钥登录 `?k=`，以及
  `signup_via_device = "lan"` 时的自己注册。它从 `CF-Connecting-IP`（Cloudflare）或 `X-Forwarded-For`
  （Tailscale serve）取调用方地址，作为 `X-Real-IP` 传下去，这样中转按网段的限流照常有效。

中转为纯 HTTP 要改的：只有请求是经 HTTPS 来的（前端给 `X-Forwarded-Proto: https`）时登录 cookie 才带
`Secure`；中转只信任来自回环的这些头，和现在对 `X-Real-IP` 一样。

### 6.4 哪些需要 HTTPS

在局域网里网页是纯 `http://`。浏览器只在**安全上下文**（HTTPS，或 `http://localhost`）里开放某些功能：

| 功能 | 用在哪 | 在局域网纯 HTTP 上 |
| --- | --- | --- |
| 登录 cookie（`Secure`） | 所有页面 | 做了 6.3 的 cookie 修改后可用 |
| 手机遥控、投屏、订阅、控制台、总后台 | 所有页面 | 可用 |
| 添加到主屏幕（iPhone） | 遥控 | 可用；地址里带会话（`?s=`），和现在一样 |
| 可安装的网页应用（安卓 Chrome、service worker） | 遥控的 manifest | 不能安装；主屏幕快捷方式仍可用 |
| 页内扫码（相机，`getUserMedia`） | `scan.js`（给电脑登录、绑定） | 不可用；`scan.js` 已经会提示。用手机自带相机扫二维码可以；输入码可以 |
| 复制按钮（`navigator.clipboard`） | 总后台、控制台、遥控 | 不可用；需要一个「选中后复制」的后备（小改动） |
| 声波配网（`AudioContext`） | `/tv/wifi` | 可用 |
| 用手机听（`<audio>` + HLS） | 遥控 | 预期可用（普通媒体元素） |
| 锁屏控制（Media Session） | 遥控、用手机听 | 可能没有；有的浏览器只在安全上下文里提供。要在 iPhone 上验证 |
| Web Serial（刷机） | 网页刷机 | 只能在 HTTPS 的静态网站上，或插着 USB 线的那台机器上的 `http://localhost` |

所以**在家日常使用不需要 HTTPS**。HTTPS 随外网访问（6.5）而来，而扫码和锁屏控制正是在那种场景里最有用。

### 6.5 从外面访问网页

| | Tailscale | Cloudflare Tunnel | 端口转发 |
| --- | --- | --- | --- |
| 需要 | 服务器和每部要用的手机都装 Tailscale | Cloudflare 上的一个域名（免费版）、隧道容器 | 家里有公网 IPv4（常常没有：运营商级 NAT）、能改路由器 |
| HTTPS | `tailscale serve` 在 `<机器>.<tailnet>.ts.net` 上给有效证书 | 有，Cloudflare 的 | 要自己弄证书 |
| 谁能打开 | 只有 tailnet 里的设备 | 有链接的人都能（中转的登录保护它） | 任何人 |
| 在大陆 | 大多能用，常走中继（较慢）；有的网络会干扰 | 能用；从国内访问延迟高；免费版没有大陆节点 | 家宽通常封了 80/443 |
| 用手机听 | 没问题 | 经免费 CDN 传音频属于 Cloudflare 条款限制的「不成比例的」媒体；偶尔用 | 没问题 |
| 建议 | **给家里人自己的手机**，尤其是用手机听 | **给要分享的链接**（来访亲戚用的遥控、上班时用 PC 控制台） | 网页不建议 |

国内常见的同类工具（ZeroTier、花生壳、frp）原理相同；教程点名 Tailscale 和 Cloudflare Tunnel，其余的只讲
原理。不管用哪个，把 `public_url` 设成它，二维码和邀请链接就用它；在局域网地址上登录的手机是另一份会话
（cookie 按主机），和云主机两个主机名的情况一样。

### 6.6 出门在外的设备

设备说原始 TCP，既跑不了 Tailscale 也跑不了隧道客户端。星智的 4G 模块现在被固件置为飞行模式
（[`xingzhi-cube-154.zh_CN.md`](../../boards/xingzhi-cube-154.zh_CN.md)），所以「在外面」指设备连手机热点或别的 Wi-Fi。

| 选项 | 需要什么 | 代价和风险 |
| --- | --- | --- |
| A. 路由器把 8097/8098 端口转发出去 + DDNS | 家里有公网 IPv4；`allow_internet = true` | 媒体端口在互联网上：靠每台设备的密钥（明文传输）和网关的失败限流守着；谁的设备都能连到未绑定画面 |
| B. 经用户自己的 VPS 用 frp（或类似） | 一台小 VPS；服务器旁边跑 `frpc` | 视频流经过 VPS：每台星智约 1.7 Mbit/s，和云主机同样的带宽问题 |
| C. 直接 IPv6 | 固件改动：拨号用 `AF_UNSPEC` 而不是 `AF_INET`；家里路由器放行入站 IPv6 | 国内移动网络大多给 IPv6；家用路由器默认常拦入站 IPv6。有希望，未验证 |
| D. 用你的云主机给陌生人中继 | — | 不可行：3 Mbit/s |
| E. 跑 Tailscale 的旅行路由器 | 设备连旅行路由器的 Wi-Fi | 不用改代码；适合爱折腾的人 |

**建议：** 默认都不开（Q8）。A 和 B 写成进阶做法；`allow_internet = true` 时，来自公网地址的未绑定设备拿不到
绑定码、被网关拒绝（只有已绑定的设备能从外面连）。C 作为以后的固件项考虑。在外面建议用「只听声音」：约
0.07 Mbit/s 而不是 1.7（星智看一小时画面约 0.75 GB 流量）。

## 7. 首次运行和账号

### 7.1 第一个管理员

```
banner: setup code + QR ──► phone opens /tv/setup#<code>
  page: "Name" (default: "Admin"), the code pre-filled from the fragment
  POST /api/setup {code, name}
     relay: code matches secrets/setup-code (constant-time), no admin yet
        → create_user(role="admin"), phone session (180 days), audit "user.setup"
        → delete secrets/setup-code; record device_address from Host
  page: "Add to Home Screen", then "Set up a TV" (6.2) and "Add channels" (8.2)
```

- 设置码在网址的 **#片段** 里，所以不会进任何服务器日志或代理；页面在 POST 正文里发送它。
- 每个网段 10 分钟内错 5 次就要等；码有 10 个字符，这是防负载，不是防猜。
- `/tv/setup` 只在还没有管理员时存在，而且只在局域网端口上。
- 设置之后：PC 控制台照云主机上的办法扫码登录；`python3 -m server.tenancy.admin login-link <id>`
  （在容器里用 `docker compose exec tv …`）仍是最后的办法。

### 7.2 家里其他人

现有的方式全部照旧：「让另一部手机登录」（同一个账号，对什么都共用的家庭最简单）、邀请（各自的账号）、
扫未绑定设备自己注册（`signup_via_device`）、扫已绑定设备的二维码当访客。教程建议一家共用一个账号，客人用
访客二维码。

### 7.3 单个家庭的简化

没有新代码路径，只是默认值和措辞：

- 没有旧设备、没有共享令牌、不用迁移：`meta.legacy_token = 0`，`meta.selfhost = 1` 时「系统」页隐藏这个开关。
- 总后台的概览在 YouTube 关闭时隐藏 YouTube 节点面板，没有 `/proc`（不用 Docker 的 Mac）时隐藏主机曲线，
  这一点现在已经如此。
- 设备列表给新设备起名「星智电视 1」「星智电视 2」，而不是型号加 id 末几位（`default_device_name()`）。
- 自己注册的网段限制（每个网段每小时 3 个账号）保留；局域网里所有手机算一个网段，对一个家庭没问题。

## 8. 内容和版权

### 8.1 包里带什么

不带任何能播的东西。具体是：

- 镜像里没有 `channels.txt`；直播表一开始是空的。
- 自建模式下不用 `BUILTIN_CHANNELS`（监护进程设 `TV_NO_BUILTIN_CHANNELS=1`），`load_channels()` 不再到代码
  旁边找 `channels.txt`。
- 不提供 `DEFAULT_SOURCE`（一个社区播放列表）；导入频道时要用户给自己的播放列表地址或文件。
- 没有 B 站或 YouTube 的 cookies，没有 YouTube 节点地址或密钥，没有代理。
- 媒体服务接受空表：一个台都没有时，已绑定的设备收到一张由 `server/overlay.py` 画的静止画面「还没有频道，
  在手机上加几个吧」（和绑定画面一样），静音，不起 ffmpeg。

### 8.2 直播频道

总后台的「频道」页（阶段 3）已经能加、改名、改分组、排序、检查并带校验地保存。为自建新增
**「导入播放列表」**：贴一个 M3U 地址或上传一个 M3U 文件；页面列出其中的条目（按 `tools/channel_config.py`
的办法解析），带勾选框和「检查哪些能播」按钮，然后加入勾选的。地址只在请求时取一次，不保存。能加地址的是
这个家庭的管理员，所以多租户设计里关于服务端请求伪造的顾虑（那边的 Q5）是这个家庭自己的事；协议仍只允许
`http`/`https`。

**公开播放列表预设**（2026-10-22 项目负责人决定，方案 B）：发布版不带任何直播地址，但一个家庭应该能一键加上央视和
各省卫视。所以导入块只按链接列出几个知名的、公开的、社区维护的播放列表（`server/relay/presets.py`：名字、链接、
上游自己提供的镜像链接、维护者、主页；由 `tests/test_playlist_presets.py` 检查），上面写明「以下是网上公开的第三方
播放列表，内容由其维护者负责，本项目不提供也不保证任何直播源；请只添加你有权观看的内容。」选一个和粘贴它的地址
一样（给导入接口发 `{preset, mirror}`，只取一次，不保存）。列表显示每个台的自动分组（`channel_groups.classify`），
有「央视 / 卫视」快速筛选、搜索框、全选 / 全不选，「试一下」只在点的时候试勾选的、一次最多 20 个（从不试整张
列表）；地址或名字已经在表里、或在列表里重复的台标出来，默认不加。加的时候只加每个用户的设备都放得下的
（按 `config_bytes` 估算），保存时照旧检查并备份。只有自建和电脑客户端有：云端中转（没有 `--selfhost`）既没有
导入也没有预设。`DEFAULT_SOURCE` 不在其中，镜像的内容检查照旧禁止它。

### 8.3 B 站

UP 主和剧集和云主机上一样，而且是从不会被 B 站回 412 的家庭地址访问。中转的请求是匿名的（它自己取一个
`buvid3` cookie）；无需配置。登录后的 cookie（更高画质或会员内容）不支持，也不打算做。

### 8.4 YouTube

三种模式（`[youtube] mode`）：

- **off**（默认）：YouTube 链接被拒绝，提示「YouTube 未设置」。
- **node：** 用户自己运行的远程节点（在自己的海外 VPS 上跑 `server/relay/ytnode.py`，即现有做法），用
  `secrets/youtube-node.json`（`url`、`key`、`cert`）配置。
- **local：** `ytnode` 在容器里的回环上运行（回环上用纯 HTTP，不要证书，新开关），yt-dlp 和 deno 第一次启用时
  下载，用用户自己的 `cookies.txt`，可选一个 HTTP 代理（yt-dlp 的 `--proxy`、ffmpeg 的 `-http_proxy`；两者都是
  新加的）。在大陆以外，家庭地址常常根本不需要 cookies。

cookies 是用户自己账号的；教程写明用小号、在无痕窗口导出、绝不分享这个文件。每个节点 `MAX_STREAMS` 仍是 2。

**实际实现（S5b，2026-10-23，分支 `desktop-ytlocal`）。** 只有 `mode = "local"` 时监督进程才起第五个子进程 `ytnode`
（打包版的 `__ytnode`；`server/relay/ytnode.py --plain --bind 127.0.0.1 --port 47443`（被占时换一个空端口）
`--tools-dir <数据目录>/youtube/bin --remux --no-check`）；中转带 `--ytlocal http://127.0.0.1:<端口>` 和
`secrets/ytnode-key` 里的密钥，经一个完全不用代理的 opener 连它（环境变量和 macOS 的系统代理都不看）。代理在子进程的
环境变量 `TV_YT_PROXY` 里（不在命令行上）：yt-dlp 用 `--proxy`；ffmpeg 用 `http_proxy`（`no_proxy` 里有回环地址），
`socks5://`、`socks5h://` 时节点里开一个只听回环的 HTTP CONNECT 转接口，经 socks 代理转出去。H.264 + AAC 的格式只换封装
（`-c copy`），别的照云端节点转码。yt-dlp 和 deno 每次调用都按 `components.json` 现找，安装、更新都不用重启；cookies 也是。
下载由 `server/selfhost/ytcomponents.py` 做（固定 yt-dlp 2026.10.21 和 deno 2.9.8，SHA-256 取自两个发布页公布的校验值；
更新 yt-dlp 时按那个版本自己的 `SHA2-256SUMS` 核对；经用户的代理或镜像前缀）。电脑版「设置 → YouTube」
（`server/relay/ytsettings.py`，只给管理员）改 `config.toml` 的 `[youtube] mode` 和 `proxy`，再放一个
`run/refresh-request`；监督进程重读设置，只重启参数变了的子进程。代理自动检测：macOS 用 `scutil --proxy`，Windows 读
`Internet Settings` 注册表，Linux 和容器里看环境变量。在 Mac 上经本机代理试过：组件下载（HTTP CONNECT）、yt-dlp 加 deno 经
`socks5h` 到达 YouTube（不带 cookies 时 YouTube 每次都要求登录）、ffmpeg 经 socks 转接口取 HTTPS 媒体。没有在 Windows、
Linux、Docker 上跑过。

### 8.5 教程怎么说

- 这个软件只播放你自己加进去的东西。它不带任何频道、直播流或账号。
- 只添加你有权观看的来源，只给你自己家里用。不要把你的服务器或它的视频流分享给家以外的人，也不要公开它。
- 服务器替你抓取视频网站内容时，网站的条款对你适用；cookies 是你自己账号的，留在你自己的机器上。
- 在访问不了 YouTube 的地方，YouTube 的设置是给已经有自己访问办法的家庭的；本项目不提供任何办法。

### 8.6 免责声明措辞（建议，不是法律意见）

这只是一个建议，应当请有资质的人审阅；本文的任何内容都不是法律意见。

> 本项目是开源软件，仅供在您自己家中私人、非商业使用。它不包含、不托管、不出售、也不推荐任何频道、直播流、
> 视频、账号或代理。它播放的一切内容都由您自己的服务器从您自己添加的来源获取；您应自行确保有权访问这些来源，
> 并遵守您所使用服务的条款和您所在地的法律。请勿用它向他人传播内容。本软件按「现状」提供，不附带任何形式的
> 担保（MIT 许可）。

## 9. 固件分发

### 9.1 发布产物

CI（在现在构建护照公开镜像的 `.github/workflows/build-firmware.yml` 上扩展）为每个发布构建并附上：

| 文件 | 构建 | 刷写 |
| --- | --- | --- |
| `Xingzhi-Cube-154-tv-public.bin` | `./tools/validate.sh --xingzhi` | 擦一次，然后从 `0x0` 写 |
| `FoloToy-AI-Passport-tv.bin` | `./tools/validate.sh --prototype` | 从 `0x0` 写，**绝不擦除**（出厂身份在 `0x356000`） |
| `manifest-*.json` | 生成 | 给网页刷机用 |
| `SHA256SUMS` | 生成 | 刷机页校验 |

公开构建：没有默认服务器、没有网站、没有帮助网址、没有令牌、没有 Wi-Fi。

### 9.2 网页刷机

`<static-site>/flash/` 上的一个页面，基于 esptool-js（上游的刷机页已经用 esptool-js 0.6.1 并检查写入范围；
`docs/flash/index.html`）。只支持桌面版 Chrome 或 Edge（Web Serial）；在其他浏览器和手机上页面会这样说明。

- 识别芯片：ESP32-S3 给星智镜像，ESP32-C3 给护照镜像；其他的拒绝。
- **星智：** 说明这会替换掉小智 AI 固件（并链接到小智自己的刷机页以便刷回去），要求明确勾选，擦除芯片、写入、
  复位。芯片没响应时提示用 BOOT 键进下载模式。
- **护照：** 绝不擦除；保留上游的检查，写入范围必须在 `0x356000` 之前结束，并说明怎么重启设备（有电池：拔线
  不会复位）。
- 写入前按 `SHA256SUMS` 校验文件。
- 刷完：「现在打开你家庭服务器的设置页，选『设置一台电视』」。
- 家庭服务器也可以在 `http://localhost:8080/flash/` 给插着设备的那台机器提供一份刷机页（`localhost` 算安全
  上下文）；这时固件文件来自镜像本身。静态网站慢的时候有用。

### 9.3 服务器地址：烧进去还是事后设置

| 方式 | 给谁 |
| --- | --- |
| **刷完后**用声波或热点页**设置**（6.2） | 所有人；默认 |
| 刷机页预填：在 `0x9000` 写一个小的 NVS 分区，里面放服务器地址（键 `wifi/ota_url`，就是 `tools/set_server_addr.py` 改的那一条） | 只限星智，因为它本来就要擦；以后做（阶段 S6） |
| 把 `CONFIG_AV_DEFAULT_SERVER` 烧进构建 | 只给你朋友用的构建（Q11）；公开构建不用 |

NVS 预填绝不能在护照上做：它的 NVS 里有播放器的设置，和 Muse 并存时还有 Muse 的配对。

### 9.4 和 Muse 并存的 AI 护照

`ota_1` 镜像必须用 Muse 的密钥签名（`CONFIG_SECURE_SIGNED_ON_UPDATE`），只写到 `0x400000`，绝不带这个构建的
引导程序或分区表（[`passport-muse-dual.zh_CN.md`](../../boards/passport-muse-dual.zh_CN.md)）。只有能发布签好名的
镜像时网页刷机才能做这件事，而这取决于 Muse 的签名密钥能不能在 CI 里用（Q9）。在那之前教程链接到板子文档，
标为进阶。

### 9.5 固件更新

现在：用网页刷机重刷、不擦除；设备 id、密钥、Wi-Fi 列表和 NVS 里的设置都保留，所以设备仍是绑定状态。总后台
的「设备」页把每台设备 HELLO 里报的 `fw` 和这个发布附带的版本对照显示，带一个「怎么更新」的链接。

以后（Q10）从家庭服务器 OTA：星智换一张有两个应用槽的新分区表（8 MB flash 放得下；每个人擦一次），设备经
HTTP 从家庭服务器取镜像，并用编进固件的项目公钥在应用层校验签名（链路是纯 HTTP，公开构建也没有安全启动）。
在有会去更新的用户之前不值得做。

## 10. 更新、备份和日志

- **标签：** `1.2.3`、`1.2`、`1`、`latest`，以及从 `main` 构建的 `edge`。教程用 `:1`。只有更新需要手动步骤时
  才升主版本。
- **更新：** `docker compose pull && docker compose up -d`；NAS 上「重新拉取镜像」再重启。默认不自动更新
  （提一下 Watchtower，但不配置）。
- **数据库：** 结构变更已经由中转在启动时自动完成（`ALTER TABLE … ADD COLUMN`，旧代码照样能用升级后的文件）。
  监护进程另外在启动一个结构版本号和数据库不同的版本前先备份一次（`tv-pre-<版本>.db`）。
- **固件兼容：** 服务器照现在的办法按 HELLO 的标志继续服务旧固件；发布说明写明支持的最旧固件。
- **备份：** 每晚 `sqlite3 .backup`（用 Python 的 `sqlite3.Connection.backup`，不需要命令行工具）到 `backups/`，
  保留 `backup_keep_days` 天。总后台 → 系统加「下载备份」（数据库副本 + `secrets/` + `channels.txt` +
  `config.toml` 打成一个包，并提示里面有密钥）。恢复：停掉，用包里的内容替换 `/data`，启动。
- **日志：** 每个进程输出到 stdout，带短前缀（`relay:`、`gate:`、`worker <id>:`）；Docker 自己的轮转让它保持小。
  规则不变：不记令牌、密钥、会话 id 或完整 IP；设备 id 可以；`TV_VOICE_LOG_TEXT` 关闭。唯一的例外是横幅里的
  设置码，一次性，有了管理员就消失。以后：在总后台「系统」页显示最后 200 行。
- **健康检查：** 前端的 `GET /healthz`（中转在应答、媒体端口在监听），给 Docker 的 `HEALTHCHECK` 和 NAS 的
  状态灯用。

## 11. 静态网站

按 Q5 托管；中文优先，英文并列。没有构建步骤，没有第三方请求（和现在的 `packaging/site/` 一样）。

| 页面 | 内容 |
| --- | --- |
| 首页 | 是什么、需要什么（一台支持的设备、一台一直开着的电脑或 NAS、一根 USB 线、用一次 Chrome 或 Edge）、`tools/render_ui/site_shots.py` 生成的截图 |
| 1. 安装服务器 | Linux / 迷你主机上的 Docker；NAS（群晖、绿联、威联通）；Mac（Docker 或纯 Python）；Windows（Docker Desktop） |
| 2. 首次运行 | 横幅、设置码、建管理员、添加到主屏幕 |
| 3. 给设备刷机 | 网页刷机；手动 esptool 命令；只有星智要擦除 |
| 4. 给设备配网 | 声波配网、热点后备、绑定、固定 IP / `.local` |
| 5. 添加要看的 | 直播频道（导入你的播放列表）、B 站 UP 主和剧集、YouTube（可选，Q6） |
| 6. 日常使用 | 按键、手机遥控、语音（可选）、用手机听 |
| 7. 在家以外（可选） | Tailscale、Cloudflare Tunnel；出门在外的设备（进阶） |
| 8. 更新和备份 | 更新镜像、重刷固件、备份和恢复 |
| 常见问题 / 排障 | 防火墙、端口被占、NAS 权限、从镜像加速拉取、「服务器没有响应」、「服务器忙」 |
| 免责声明 | 8.6 节 |
| `/flash/` | 网页刷机 |
| `latest.json` | 给检查更新用的版本信息（Q12） |
| 下载 | GitHub Releases 的链接（固件、离线镜像）；镜像仓库地址 |

现有的帮助页（`packaging/help/index.html`，设备上帮助二维码指向的页面）由家庭服务器自己在 `/TV/` 提供，所以
设备的帮助二维码在局域网里也能用。

## 12. 安全

| 项目 | 设计 |
| --- | --- |
| 暴露面 | 默认只在局域网：8080、8097、8098。公网端口 8081 不在主机上发布；只有隧道容器能访问 |
| 总后台 | 在公网端口上关闭，除非 `admin_over_tunnel = true`；角色检查照旧在中转里做 |
| 第一个管理员 | 一次性设置码（7.1），只在局域网端口，有限流，记审计 |
| 应急密钥链接（`?k=`） | 默认只在局域网端口；密钥自动生成，从不打印 |
| 共享令牌 | 没有：没有令牌文件，`meta.legacy_token = 0`，公开构建也不带 |
| 设备 | 每台设备一个密钥、首次信任、重新配对窗口（不变）。`allow_internet = false` 保留媒体服务的私网地址规则；为 true 时只有已绑定的设备能从公网地址连 |
| 自己注册 | 默认只能从局域网端口（`signup_via_device = "lan"`） |
| 限流 | 中转现有的按网段限流（登录票据、手机链接、注册、输错码），两个端口上都由前端喂真实的调用方地址 |
| Cookie | HTTPS 时带 `Secure`，始终 `HttpOnly; SameSite=Lax; Path=/tv/`；每个 POST 都做同源检查（现有） |
| 网页 | 现有的 CSP，不允许外部来源，`frame-ancestors 'none'` |
| 容器 | 非特权用户，根文件系统只读（`/data` 和 `/tmp` 除外），去掉所有 capability，不挂 Docker 套接字 |
| 设备链路是明文 | 局域网里不变；做了端口转发时设备密钥会以明文经过互联网（挑战-应答是以后的固件项，多租户设计 8.1） |
| 用户给的网址 | 只有管理员能加直播地址和播放列表；只允许 `http`/`https` |
| 静态存储的密钥 | `secrets/` 0700，备份会提示里面有密钥；数据库只存哈希（现有） |

## 13. 资源和支持的硬件

### 13.1 每路流和每台机器

云主机上的实测（多租户设计 7.6）：一路星智最多 200 kB/s 画面加约 8 kB/s 声音，**约 1.7 Mbit/s**；一路护照
**1.0–1.2 Mbit/s**；只听声音**约 0.07 Mbit/s**；一路星智的 ffmpeg 在 2 核主机上约占一个核的 12 %。

| 数量 | 值 |
| --- | --- |
| 每路视频的 CPU | **实测（M1 Pro）约 0.15 核**（720p 片源；360p 或低动态画面 0.07–0.09，1080p 约 0.18）。约三分之二是 ffmpeg 解码片源，Python 发送端固定 0.04–0.07。x86 现代核推算相近，2 核云主机推算 0.2–0.3 vCPU |
| 每路视频的 CPU，arm64（树莓派 4/5） | **推算**：树莓派 5 约 0.4 核，树莓派 4 约 0.8–1 核（按单核性能比例，未实测） |
| 用手机听的额外开销 | **实测**：每路再加约 0.02 核和 20 MB（一个小的 AAC ffmpeg）。只听声音的会话现在仍会解码视频（多租户设计 7.6），成本和看视频相同 |
| 每个活动工作进程的内存 | **实测 50–70 MB**（Python 24–26 MB + ffmpeg 15–45 MB，片源分辨率越高越多）；没设备连着的媒体服务约 23 MB |
| 空闲基础内存（监护、前端、中转、网关） | 中转**实测**约 22 MB、几乎不占 CPU；监护、前端、网关**预期**合计 100 MB 以内 |
| 语音容器内存 | 约 550 MB（`muse-asr` 实测） |
| 本地 YouTube 节点内存 | yt-dlp 加 deno 峰值约 330 MB（节点实测），每路 YouTube 另加一个 ffmpeg |
| 每路的局域网带宽 | 星智 1.7 Mbit/s，护照 1.0–1.2（实测）；真正的上限是设备的 2.4 GHz Wi-Fi |
| 每路的外网下载 | 等于片源自身的码率，常见 1–6 Mbit/s（HLS 360p–1080p）。两台看同一个台也各拉一份（网关以后可以合并） |
| 工作进程启动时间（轻网关） | **待测**，阶段 S2 实测；必须远低于 2 秒 |
| 磁盘 | 镜像：预期 250–350 MB。数据：50 MB 以内，另加备份 |

### 13.2 每台机器带几台设备

这台 M1 Pro 上测到单路成本；加到几路开始掉帧的上限测试中途停了，所以下表除 Mac 外都是按单核性能比例**推算**的，Mac 那行也待上限测试确认。树莓派和 N100 在 S0 阶段各补一次实测。

| 机器 | 同时视频路数（推算） | 说明 |
| --- | --- | --- |
| Intel N100 / N97 迷你主机，8 GB | 约 10 | **推荐**：待机 6–10 W，x86-64 |
| 旧笔记本，四代酷睿 i5 或更新，4 GB 以上 | 3–4（双核）到 6+（四核） | 合盖不睡眠；Linux 或 Windows + Docker Desktop |
| Mac，Apple 芯片 | M1 Pro 约 30（待上限实测） | Docker Desktop 或纯 Python；不能睡眠 |
| 群晖 / 威联通 x86 NAS（J4125、R1600） | 约 4–6 | Container Manager / Container Station |
| 绿联 DXP（N100） | 约 10 | Docker 应用 |
| 树莓派 5，4–8 GB，64 位系统 | 约 6 | 实测前算实验性 |
| 树莓派 4，4 GB 以上，64 位系统 | 约 2 | 实测前算实验性 |
| ARM NAS（瑞昱 RTD1296/1619B） | 0–1 | 不推荐 |
| 2 核云主机（参考） | 2–3 | 带宽通常先成为瓶颈（每路 1.7 Mbit/s 出站） |
| 树莓派 3、Zero 2、32 位系统、路由器 | — | 不支持 |

语音让最低内存多 1 GB；不带语音的最低是 2 GB。

## 14. 代码改动

| 范围 | 文件 | 改什么 |
| --- | --- | --- |
| 监护和配置 | 新的 `server/selfhost/`（`__main__.py`、`config.py`、`bootstrap.py`、`banner.py`） | 配置文件、数据布局、密钥、建库、设置码、启动并重启子进程、每晚备份、升级前备份、带终端二维码的横幅（复用 `server/relay/qr.py`） |
| 网页前端 | 新的 `server/front.py` | 两个端口、`tv-relay.conf`/`tv-listen.conf` 的路由、按 `run/workers.json` 的每台设备收听路由、`X-Real-IP` / `X-Forwarded-Proto`、公网端口上的隧道头、`/healthz`、静态帮助页和刷机页副本 |
| 中转 | `server/relay/__main__.py`、`server/relay/web.py` | 用「等待设置」代替退出；`/tv/setup` 页和 `POST /api/setup`；只有 HTTPS 时 cookie 才带 `Secure`；信任来自回环的 `X-Forwarded-Proto`；按设备的收听路由；没有 `navigator.clipboard` 时页面里「选中后复制」的后备 |
| 账号 | `server/tenancy/accounts.py`、`server/tenancy/signup.py`、`server/tenancy/gate.py` | 用设置码建第一个管理员；`signup_via_device` 策略；`allow_internet` 时拒绝公网地址来的未绑定设备；`meta.selfhost`；设备命名 |
| 页面 | `server/relay/assets/wifi.html`、新的 `setup.html`、`admin.html` | 服务器一栏来自 `device_address`；首次运行页；「系统」显示配置和备份；「频道」上导入播放列表；固件版本一列 |
| 媒体服务 | `server/live.py`、`server/tv_server.py`、`server/overlay.py` | 允许空表、「还没有频道」静止画面；`TV_NO_BUILTIN_CHANNELS`；自建模式下不在代码旁找 `channels.txt`；工作模式（S2） |
| 轻网关（S2） | 新的 `server/gate.py`（进程网关，不同于 `server/tenancy/gate.py`） | 监听、HELLO、配置表、交接文件描述符、工作进程启动和空闲退出、上限、未绑定画面、工作进程登记表 |
| YouTube | `server/relay/ytnode.py`、`server/relay/youtube.py` | 回环上纯 HTTP；yt-dlp 和 ffmpeg 的 `--proxy`；工具下载到 `/data/youtube/bin` |
| 语音（可选） | 新的 `packaging/asr/`（Dockerfile、约 150 行的服务） | Unix 套接字上的 `POST /asr`，sherpa-onnx SenseVoice，首次启动时下载模型 |
| 打包 | 新的 `packaging/docker/`（Dockerfile、`compose.yaml`、`.dockerignore`、`NOTICE`），`run.sh`（`--selfhost`） | 镜像、compose、纯 Python 入口 |
| CI | `.github/workflows/` 新的 `build-image.yml`；`build-firmware.yml` | 多架构镜像推到 GHCR 和国内仓库、离线包、内容检查；星智公开构建作为发布产物，清单和校验和 |
| 静态网站 | `packaging/site/`（教程页）、`docs/flash/`（刷机页）或新的 `packaging/flash/` | 教程、两种芯片的刷机页、`latest.json` |
| 固件 | S1–S5 不需要；以后可选：`main/av_player.c`（IPv6 用 `AF_UNSPEC`）、分区和 OTA（Q10） | — |
| 测试 | `tests/test_selfhost_*.py`、`tests/test_front.py`、`tests/test_gate.py`、扩展中转测试 | 启动建库、设置码、双端口规则、cookie 标志、收听路由、空表、用模拟设备（`tools/device_sim.py`）测网关交接 |
| 文档 | `server/README.md`、`docs/development/host-setup.md`（加指引）、新的 `docs/development/selfhost.md`（运维说明） | — |

## 15. 分阶段计划

工作量按 agent-日计（一个 agent 带主机测试），不含你的审阅和设备上的检查。

### 阶段 S0 —— 决定和数字（0.5 天）

回答 Q1–Q12。补完 M1 Pro 的上限测试；在树莓派和 N100 上各实测一次，替换第 13 节的**推算**数字。

### 阶段 S1 —— 能用的家庭服务器，每个型号一台（5–7 天）

交付：镜像和 compose、纯 Python 方式、首次运行设置、空表起步、导入播放列表、局域网 HTTP 网页。可以做 0.x 测试版。

- S1a. `server/selfhost/`：配置、数据布局、密钥、建库、设置码、横幅、监护、备份。（1.5–2 天）
- S1b. 带两个端口、请求头规则、`/healthz` 的 `server/front.py`；中转的 cookie 和等待设置改动；`/tv/setup`。（1.5–2 天）
- S1c. 内容：空表、静止画面、不用内置频道、总后台「频道」页导入播放列表、`wifi.html` 的服务器一栏。（1–1.5 天）
- S1d. Dockerfile、compose、`.dockerignore`、CI 多架构构建和内容检查、`run.sh --selfhost`。（1–1.5 天）

并行：S1a 和 S1c 互不相关；S1b 只需要 S1a 的配置形状。

**S1 现状（2026-10-16）：已实现**，每个型号一台（Q1 选项 A）；快速上手见
[`docs/selfhost/README.zh_CN.md`](../../selfhost/README.zh_CN.md)。已在一台 Mac 上用纯 Python 方式
（`./run.sh --selfhost`，只听 127.0.0.1）验证：横幅和设置码、经 `/tv/setup` 建第一个管理员、明文 HTTP 下不带
`Secure` 的 cookie、导入播放列表和试播、保存频道表、`/healthz`、用模拟设备看未绑定画面和空表画面、正常停止。只写了、
读过、没跑过：Dockerfile、compose 文件和 CI 工作流（开发机上没有 Docker），NAS 和 Windows 的步骤。所有自建的行为都在
中转的 `--selfhost` 开关或 `TV_NO_BUILTIN_CHANNELS=1` 后面，只有监督进程会设；云端的单元照旧。和上文不同的地方：

- Docker 的文件在 `packaging/docker/`；`.dockerignore` 在仓库根目录，是白名单。内置频道挪到了
  `server/builtin_channels.py`，镜像不带它；镜像里没有 `tools/`（所以也没有 `DEFAULT_SOURCE`）。
- `PUID`/`PGID` 变成 compose 的 `user:`，而不是用 root 入口再降权，所以容器可以 `cap_drop: ALL`。
- 纯 Python 方式的数据文件夹是「每用户数据目录」下的 `selfhost`（绝不是代码目录；`tools/datadir.py` 在代码目录可写时会选它）。
- 中转的端口（8099、8100）和「用手机听」的端口（8198、8199）是固定的；设置文件多了 `[server] bind` 和
  `[devices] xingzhi_port` / `passport_port`。`max_streams`、`update_check`
  能写、但还不起作用（`[youtube] proxy` 和本机 YouTube 模式：S5b，第 8.4 节）。
- 镜像只有手动运行工作流并勾选「publish」时才推送；Debian 的 ffmpeg 还没有固定版本。
- 和 S3 接上（2026-10-16）：前端在 `/flash/` 提供 `packaging/flash/`，在 `/flash/firmware/` 提供选中的那套固件，两个端口都给
  （静态文件，不改任何东西）。固件先用数据文件夹的 `firmware/`（`./run.sh --fetch-firmware` 用
  `tools/firmware_manifest.py install` 装好最新发布的 `xiaozhi-tv-flasher.zip`，或自己把这个 zip 解压进去），没有再用镜像工作流
  拷进镜像的那一套。中转拿到 `TV_FIRMWARE_MANIFEST` 和 `TV_FLASHER_URL=/flash/`。网页串口要安全的页面，所以横幅和教程都让人在
  服务器那台电脑上打开 `http://localhost:<lan_port>/flash/`；用局域网地址刷不了。已在 Mac 上看过：刷机页从纯 Python 的服务器能打开，
  总后台「设备」页用一份示例清单显示了固件一栏。
- 留到以后：「系统」页显示设置和下载备份、选中复制的后备、「小智电视 1」这样的设备名、更新检查、`allow_internet`
  时拒绝公网地址的未绑定设备。

### 阶段 S2 —— 多台同时（轻网关）（3–4.5 天）

交付：一个地址，所有设备同时看，直到机器的上限。加上 S1 就是 1.0 版。

- S2a. 网关：监听、HELLO、配置表、交接、上限、未绑定画面。（1.5–2 天）
- S2b. `AVServer` 的工作模式、空闲退出；现有的 `tests/test_tv_server.py` 保持全绿。（1–1.5 天）
- S2c. 每台设备的收听端口、登记表、中转路由、前端路由。（0.5–1 天）

并行：S2a 和 S2b 按约定好的交接接口同时做。

### 阶段 S3 —— 固件分发和网页刷机（2–3 天）

- S3a. CI：星智公开构建作为发布产物、清单、校验和。（0.5 天）
- S3b. 两种芯片的刷机页：识别、星智先擦除并征得同意、护照绝不擦除的范围检查、校验和、说明文字；家庭服务器
  提供的 `localhost` 副本。（1.5–2 天）
- S3c. 总后台的固件版本一列和「怎么更新」。（0.5 天）

可与 S1、S2 并行。

**进展（2026-10-16）：已做完，还没在设备上试过，也还没在 CI 上跑过。**

- S3a：`.github/workflows/build-firmware.yml` 用矩阵编两个公开版镜像（Passport 用 `--prototype`，
  星智用 `--xingzhi`），版本号取 `git describe --tags`（`AV_FW_VERSION` → `PROJECT_VER`），连同
  `manifest.json`、`manifest-<设备>.json`、`SHA256SUMS` 和 `xiaozhi-tv-flasher.zip` 一起发布。
  `tools/firmware_manifest.py` 写清单、查清单；`validate.sh` 会跑它的 `check-public`，没带
  `AV_PUBLIC_BUILD` 或带了默认服务器、网站的编译都会被拒绝（Q11）。
- S3b：刷机页放在 `packaging/flash/`（没用 `docs/flash/`，那个仍是原来只支持 Passport 的 GitHub Pages
  页面）。它从自己所在的网站取 `firmware/manifest.json` 和 `firmware/SHA256SUMS`，因为 Release 的下载
  不带 CORS 头。设备的规矩（擦不擦、写到哪为止、保护区）在页面自己的表里再写一遍，和清单冲突时以页面
  为准。识别时读 `0x8000` 和 `0x10000`：已经是本固件的星智默认只更新、不擦除（9.5），别的固件打勾
  同意后先擦除；Passport 从不擦除，装着 Muse 双系统的直接拒绝。写之前按清单和 `SHA256SUMS` 两头核对
  文件，写完用 MD5 读回核对。esptool-js 0.6.1 连同许可证放在仓库里。
  **S1 的前端要做的**（已做，见 S1 的现状）：把 `packaging/flash/` 放在 `/flash/`，这次发布的 `manifest.json`、`SHA256SUMS`
  和镜像放在 `/flash/firmware/`（就是 `tools/firmware_manifest.py bundle` 产出的布局），并给 relay 设
  `TV_FIRMWARE_MANIFEST=<那份 manifest.json>` 和 `TV_FLASHER_URL=/flash/`。静态网站（S4）把
  `xiaozhi-tv-flasher.zip` 解压到网站根目录即可。
- S3c：总后台设备页里，`fw` 和这次发布里同型号版本不一样的设备标「可更新到 …」，下面写怎么更新，设了
  `TV_FLASHER_URL` 就链过去。
- Q9，Muse 双系统进 CI：签名用的是 Muse 公开的共享开发密钥（facebookincubator/muse-gadget-sdk 里的
  `esp32/dev_signing_key.pem`，那边写明发货前要换掉），所以 CI 不需要保管机密也能签；没有做，原因见
  [`passport-muse-dual.zh_CN.md`](../../boards/passport-muse-dual.zh_CN.md#网页刷机和发布的镜像)。

### 阶段 S4 —— 静态网站和教程（2.5–3.5 天）

中文优先，英文并列：第 11 节的页面、免责声明、截图、NAS 和 Windows 的步骤（按官方文档写，实际试过之前标为未验证）。
S1 的命令定下来就可以开始。

### 阶段 S5 —— 可选部分（3–4.5 天）

- S5a. 语音容器（sherpa-onnx SenseVoice，`POST /asr`）。（1.5–2 天）
- S5b. YouTube 本地模式：回环 HTTP、代理、工具下载和更新。（1–1.5 天）——2026-10-23 已实现（第 8.4 节）。
- S5c. 外网访问的做法（Tailscale serve、Cloudflare Tunnel 的 compose profile）和出门在外的做法；在一个网络上试过。（0.5–1 天）

三者互不相关。

### 阶段 S6 —— 测试版加固（2–3 天）

在真机上端到端（一台 N100 或 x86 笔记本、一台 Mac、有的话一台群晖或绿联 NAS、一台树莓派 4/5），设备那一侧由你
来；备份和恢复演练；从上一个标签升级；如果要的话做星智的刷机页 NVS 预填（Q10/9.3，+1 天）。

**合计：约 18–25 个 agent-日**；测试版（S1 + S3 + 简短的 S4）约 10–12 天后，1.0 版（再加 S2、S4、S6）约 16–20
天后，可选部分另算。

## 16. 风险

| 风险 | 影响 | 应对 |
| --- | --- | --- |
| 小机器上的压测数字比预期低 | 树莓派和 ARM NAS 用户会卡 | 按实测发布每种机器的建议；`max_streams`；只听声音在不再解码视频后更省 |
| 大陆访问不了 Docker Hub / GHCR | 第一条命令就装不上 | 国内镜像仓库；离线包（Q4） |
| 大陆访问不了 `*.pages.dev` / GitHub | 教程或刷机页打不开 | 自定义域名；家庭服务器的 `localhost` 刷机页也能提供固件 |
| 设备解析不了 `.local` | 输入名字连不上 | 教程先讲 DHCP 地址保留和 IP；在两块板子上验证 lwIP 的 mDNS 查询 |
| 纯 HTTP 的限制（页内扫码、剪贴板、锁屏控制） | 在家少了些便利 | 输入码和手机相机；选中后复制的后备；想要的人经 Tailscale 用 HTTPS |
| 用户把媒体端口暴露出去 | 陌生人的设备能连到未绑定画面；密钥明文 | 默认关闭；公网地址只允许已绑定设备；做法标为进阶 |
| 内容：用户公开分享自己的服务器 | 用户的法律风险；项目的名声 | 不带内容；免责声明；不做服务器的公开目录；包里没有「分享给所有人」的开关 |
| YouTube 的说明被看作翻墙指导 | 项目在大陆惹麻烦 | 通用的代理设置，不写方法（Q6） |
| 语音模型的许可 | 可能不允许打包 | 首次启用时展示许可后再下载；S5a 前先查 |
| 分发的镜像里有 GPL 的 ffmpeg | 许可合规 | 用 Debian 的包并给源码指引；`NOTICE` |
| 慢的 ARM 上轻网关工作进程启动 | 空闲后第一次连接在设备上超时 | S2 实测；每个型号一个热备工作进程 |
| 手动更新固件 | 用户一直用旧固件 | 服务器继续支持旧的 HELLO 标志；总后台的版本一列；以后做 OTA（Q10） |
| 同一份代码多了一种运行方式 | 云主机和包之间走样 | 同一个 `server/`；自建模式是配置（`meta.selfhost`、监护进程），CI 里有测试覆盖 |

## 17. 未验证

- 所有标了**推算**或**预期**的数字。
- 设备能否解析 `.local` 名字（lwIP 一次性 mDNS 查询，ESP-IDF 5.5 默认开启），两块板子都没试过。
- 纯 HTTP 下的浏览器行为：Safari 是否也丢掉经 `http://` 设置的 `Secure` cookie（Chrome 和 Firefox 会丢），以及
  Media Session 在 iPhone 的纯 HTTP 页面上能否用。
- 树莓派上工作进程的启动时间；Debian 的 ffmpeg 在 arm64 上配合本项目参数的表现。
- Docker Desktop（Mac、Windows）默认设置下发布端口在局域网里能否访问；群晖 Container Manager 和绿联 UGOS 的表现；
  这些系统上的 `PUID`/`PGID`。
- 目前从大陆访问 `*.pages.dev`、GHCR、Docker Hub、Tailscale 中继的情况，以及阿里云个人版镜像仓库免费额度的条款。
- Cloudflare 现行条款对经免费隧道传音频的规定；Tailscale 免费版的限制。
- SenseVoice-Small 模型再分发的许可。
- Muse 的签名密钥能否用来给公开发布的护照镜像签名。
