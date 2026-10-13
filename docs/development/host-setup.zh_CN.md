<p align="right">
  <strong>简体中文</strong> · <a href="host-setup.md">English</a>
</p>

# 生产主机：unit、drop-in、安装顺序、备份

生产部署跑的每一个 systemd unit 和 drop-in：模板在 [`packaging/systemd/`](../../packaging/systemd/) 的哪里、
做什么用、按什么顺序装，以及要备份什么。`<site>` 代表网站的域名。模板已经和两台主机 2026-10-12 的
`systemctl cat` 输出对过一遍；当时的差别见[主机和模板的差别](#主机和模板的差别)。

两台主机：

- **国内主机**（Alibaba Cloud Linux 3，2 核，出网 3 Mbps，内存小）：视频中转、每种设备一个媒体服务、
  muse-asr（语音识别）、网站的 nginx。
- **海外节点**：只跑 YouTube 节点。国内主机从不直接访问 YouTube。

## Unit

| Unit | 主机 | 模板 | 作用 |
| --- | --- | --- | --- |
| `tv-relay.service` | 国内 | [`tv-relay.service`](../../packaging/systemd/tv-relay.service) | 视频中转：UP 主和剧集频道、投屏、手机遥控和 PC 控制台（在 nginx 后面）、多用户模式下的账号。只听本机：8099 推流，8100 网页和接口。 |
| `tv-xingzhi.service` | 国内 | [`tv-xingzhi.service`](../../packaging/systemd/tv-xingzhi.service) | 星智方块的媒体服务（240x180，15 帧，端口 8098）。 |
| `tv-passport.service` | 国内 | [`tv-passport.service`](../../packaging/systemd/tv-passport.service) | Muse 旁那台 AI Passport 的媒体服务（320x180，12 帧，端口 8097，数据在 `/var/lib/ai-passport-tv-passport`）。 |
| `muse-asr.service` | 国内 | muse-gadget-sdk 仓库（`esp32/tools/muse_asr`），不在本仓库 | Muse 小玩意儿用的 SenseVoice 语音识别和语音合成；电视的媒体服务用它做语音控制。 |
| `muse-asr-warm.service` + `.timer` | 国内 | [`muse-asr-warm.service`](../../packaging/systemd/muse-asr-warm.service)、[`muse-asr-warm.timer`](../../packaging/systemd/muse-asr-warm.timer) | 每 2 分钟识别一段短音频，让模型留在内存里。媒体服务最多等识别 6 秒；在内存紧的主机上，模型闲置一阵后第一句话的识别比这还慢。 |
| `yt-relay.service` | 海外 | [`yt-relay.service`](../../packaging/systemd/yt-relay.service) | YouTube 节点（`server/relay/ytnode.py`）：yt-dlp 和 ffmpeg，HTTPS、固定的自签名证书加密钥。 |

## Drop-in

每个都装成 `/etc/systemd/system/<unit>.service.d/<名字>.conf`。`packaging/systemd/` 里的模板目录和主机上的同名。

| 主机上的 drop-in | 模板 | 作用 | 什么时候要 |
| --- | --- | --- | --- |
| `tv-relay.service.d/passport-events.conf` | [`tv-relay.service.d/passport-events.conf`](../../packaging/systemd/tv-relay.service.d/passport-events.conf) | 给 Passport 的数据目录开 `ReadWritePaths=`：单用户中转把这个 `--extra-device` 的正在播放和事件文件写在那里。 | 有 Passport、单用户中转 |
| `tv-relay.service.d/tenancy.conf` | [`tenancy/tv-relay.conf`](../../packaging/systemd/tenancy/tv-relay.conf) | 多用户中转：把 `ExecStart` 换成带 `--db` 的命令行。删掉它就是回滚。 | 多用户切换之后（[运维手册](tenancy-phase1-runbook.zh_CN.md)） |
| `tv-xingzhi.service.d/voice.conf` | [`tv-xingzhi.service.d/voice.conf`](../../packaging/systemd/tv-xingzhi.service.d/voice.conf) | 语音控制：`TV_ASR_SOCKET`、`TV_RELAY_KEY_FILE`、`TV_RELAY_DEVICE=d0`、`After=muse-asr.service`。里面的 `TV_VOICE_LOG_TEXT=1` 是注释掉的：只在调阈值时打开。 | 要语音时 |
| `tv-xingzhi.service.d/tenancy.conf` | [`tenancy/tv-media.conf`](../../packaging/systemd/tenancy/tv-media.conf) | 多用户媒体服务：`TV_DB`、`TV_DEVICES_DIR`。 | 切换之后 |
| `tv-passport.service.d/relay-events.conf` | [`tv-passport.service.d/relay-events.conf`](../../packaging/systemd/tv-passport.service.d/relay-events.conf) | Passport 数据目录里的 `TV_RELAY_EVENTS_FILE`：投屏和遥控切台才到得了 Passport。 | 单用户中转 |
| `tv-passport.service.d/voice.conf` | [`tv-passport.service.d/voice.conf`](../../packaging/systemd/tv-passport.service.d/voice.conf) | 和星智的一样，`TV_RELAY_DEVICE=Passport`。 | 要语音时 |
| `tv-passport.service.d/tenancy.conf` | [`tenancy/tv-media.conf`](../../packaging/systemd/tenancy/tv-media.conf) | 和星智的一样；另外让这个 unit 能写 `/var/lib/ai-passport-tv`（数据库的 WAL 文件）。 | 切换之后 |
| `muse-asr.service.d/tv-voice.conf` | [`muse-asr.service.d/tv-voice.conf`](../../packaging/systemd/muse-asr.service.d/tv-voice.conf) | 媒体服务送音频用的本机套接字 `/run/muse-asr/asr.sock`（0660，组 `tvserver`）。这个组就是唯一的凭据。 | 要语音时 |
| `yt-relay.service.d/memory.conf`（海外） | [`yt-relay.service.d/memory.conf`](../../packaging/systemd/yt-relay.service.d/memory.conf) | `MemoryMax=600M`：yt-dlp 加 deno 峰值约 330 MB，再加两路 ffmpeg 就超过了 400M。 | 只在基础 unit 比模板旧时要；模板已经是 600M |

删掉 `voice.conf`（再 `systemctl daemon-reload`）就关掉这个 unit 的语音；删掉几个 `tenancy.conf` 就是多用户的回滚。
单用户中转服务 Passport 时，Passport 的两个事件 drop-in 不能省：基础的 `tv-relay.service` 在 `--extra-device` 里写着它。

## 依赖

国内主机：

- `python3.11`（`dnf install python3.11`，和系统自带的 Python 并存）。
- **numpy**（可选）。默认的画面降质方式 `steady`（`server/degrade.py`）不需要它；只有用来对比的旧模式
  `perceptual`、`perceptual_2d`、`mosaic` 需要。生产上不要用 `TV_DEGRADE=ladder`：颜色会逐帧跳动（设备上见过蓝黄闪烁），
  设了它媒体服务启动时会记一行以 `WARNING: TV_DEGRADE=ladder` 开头的警告。仍想装 numpy：阿里云 Linux 3 的 `python3.11` 不带 pip：

  ```sh
  dnf install python3.11-pip
  python3.11 -m pip install "numpy<3"      # 用 root 装，tvserver 才能导入
  sudo -u tvserver python3.11 -c "import numpy; print(numpy.__version__)"
  ```

- 动态链接的 ffmpeg，放在 `/opt/ai-passport-tv/ffmpeg-btbn-8.1`（BtbN 的
  `ffmpeg-n8.1-latest-linux64-gpl`；全静态版在那台主机上解析域名时崩溃，见
  [`xingzhi-cube-154.zh_CN.md`](../boards/xingzhi-cube-154.zh_CN.md)「云服务器」）。
- `sqlite3`（备份）、`curl`（预热 unit）、nginx。
- muse-asr 自己的虚拟环境和模型，在 `/opt/muse-asr` 下（按它那个仓库的说明）。

海外节点：`python3`、yt-dlp、deno 和 ffmpeg（[`xingzhi-cube-154.zh_CN.md`](../boards/xingzhi-cube-154.zh_CN.md)「UP 主频道」）。

## 安装顺序

先建用户和文件：系统用户 `tvserver`（中转和媒体服务）、`museasr`（muse-asr；drop-in 把它加进 `tvserver` 组）、
海外节点上的 `ytrelay`。代码在 `/opt/ai-passport-tv`，数据在 `/var/lib/ai-passport-tv` 和
`/var/lib/ai-passport-tv-passport`（属主 `tvserver`），密钥在 `/etc/ai-passport-tv`（配对 `token` 只有
`tvserver` 能读；`relay-key` 权限 0640、组 `tvserver`；`cstone.json` 和它指向的节点证书）。

1. **numpy**，如上。
2. **muse-asr** 加 `muse-asr.service.d/tv-voice.conf`；用
   `sudo -u tvserver curl --unix-socket /run/muse-asr/asr.sock http://x/health` 检查。然后是预热：
   把一段裸音频（16 kHz 单声道 PCM16 小端，约 1 秒）放到 `/usr/local/share/muse-asr-warm.pcm`，
   `tvserver` 能读，装上两个 `muse-asr-warm` 文件，`systemctl enable --now muse-asr-warm.timer`。
3. **tv-relay** 加 `passport-events.conf`。把 `--public-url` 里的 `<site>` 换掉。
4. **tv-xingzhi** 和 **tv-passport** 加各自的 drop-in（`voice.conf`，Passport 还有 `relay-events.conf`）。
   在云安全组和主机防火墙放行 8097 和 8098。
5. **nginx**：[`packaging/nginx/tv-relay.conf`](../../packaging/nginx/tv-relay.conf)。
6. **多用户**：`tenancy.conf` 这几个 drop-in，只作为[运维手册](tenancy-phase1-runbook.zh_CN.md)切换的一步（迁移之后）装。
7. **海外节点**：`yt-relay.service`（`memory.conf` 只在基础 unit 较旧时要）。
8. **产品网站**（可选）：见[下文](#产品网站)。

在主机上的一份本仓库副本里（用 root）：

```sh
S=packaging/systemd D=/etc/systemd/system
install -m 0644 $S/tv-relay.service $S/tv-xingzhi.service $S/tv-passport.service \
    $S/muse-asr-warm.service $S/muse-asr-warm.timer $D/
sed -i 's|https://<site>|https://YOUR-SITE|' $D/tv-relay.service
for u in tv-relay tv-xingzhi tv-passport muse-asr; do
    install -d $D/$u.service.d && install -m 0644 $S/$u.service.d/*.conf $D/$u.service.d/
done
systemctl daemon-reload
systemctl enable --now muse-asr-warm.timer
systemctl restart muse-asr tv-relay tv-xingzhi tv-passport   # 按这个顺序
```

检查：`systemctl cat <unit>` 列出基础 unit 和每个 drop-in；每个媒体服务记一行
`voice: on, relay on, transcripts not logged`（多用户时是 `relay per device`），并且没有
`WARNING: TV_DEGRADE=ladder`；`systemctl list-timers muse-asr-warm.timer` 显示下一次运行。

## 产品网站

产品网站是静态的：[`packaging/site/`](../../packaging/site/)（`index.html` 和 `assets/`，
不用构建，不请求第三方）。放在它自己那个域名的根目录，本节记作 `<site>`。

- 把 `packaging/site/index.html` 和 `packaging/site/assets/` 拷进这个域名的网站根目录；
  使用说明 [`packaging/help/index.html`](../../packaging/help/index.html) 拷成同一根目录下的
  `TV/index.html`，这样 `/TV/` 在这里也能打开，和主站一样。
- 页面用以 `/` 开头的相对路径链接中转的页面，所以这个域名也要像主站一样提供：`/tv/`
  （手机遥控、`/tv/console`、`/tv/wifi`）、`/TV/R/`、`/TV/B/`、`/TV/L/`、`/TV/W`，以及 `/TV/`
  （使用说明）。在这个 server 块里 include
  [`packaging/nginx/tv-relay.conf`](../../packaging/nginx/tv-relay.conf)。
  [`packaging/nginx/tv-site.conf`](../../packaging/nginx/tv-site.conf) 是整个 server 块的模板，
  用 `<site>`、`<root>` 占位。
- 登录 cookie 按域名分开（`Path=/tv/`，不带 `Domain`）。二维码、邀请链接和电脑版的登录码用的是
  中转的 `--public-url`。它还是主站时，在主站登录过的手机在网站域名下仍是未登录；要让网站域名成为唯一入口，
  就把 `--public-url` 改成网站域名（主站的 location 保留，已经打印或保存的链接照样能用）。
- `assets/screen-*.png` 里的屏幕图是从固件自己的绘制代码截出来的：界面改了以后运行
  `python3 tools/render_ui/site_shots.py`（要装 Pillow）。

## 主机和模板的差别

2026-10-12 对比的结果，只看设置（不看注释）：

| Unit | 主机 | 模板 | 原因 |
| --- | --- | --- | --- |
| `tv-relay.service` | `--public-url` 里是真实域名 | `<site>` | 域名不进仓库。 |
| 两个 `voice.conf` | `TV_VOICE_LOG_TEXT=1` 开着 | 注释掉 | 调阈值时打开的；它会把人说的话写进日志，所以默认关。调完在主机上关掉。 |
| `yt-relay.service` | `MemoryMax=400M`，由 `memory.conf` 提高 | 基础 unit 里就是 `MemoryMax=600M` | 主机装好之后模板才改正；实际上限一样。 |
| `tv-xingzhi.service` | 语音变量在 `voice.conf` 里 | 现在一样 | 在这一页之前，仓库把它们写在基础 unit 里；挪到 drop-in 后，删一个文件就关语音，和主机一致。 |
| `tv-passport.service` | 开头写着「装成 tv-xingzhi.service」；`TV_FPS=12` 上面的注释写「15 fps」；有一行「Cast events」注释下面没有变量 | 注释已改正 | 从 tv-xingzhi 手抄的。设置完全一样。 |
| `tenancy.conf`（媒体） | 注释说语音变量留在基础 unit 里 | 写的是 `voice.conf` | 只是注释。 |

`muse-asr.service` 本身不在这里做模板（归它的仓库管）；主机上的设了 `MUSE_ASR_LISTEN=127.0.0.1:8102`、
`MemoryMax=900M` 和 `Nice=5`。

## 备份

备份到主机以外，只有运维能读。绝不要复制进本仓库、issue 或日志。

| 文件 | 为什么重要 |
| --- | --- |
| `/var/lib/ai-passport-tv/tv.db` | 账号、设备、每个用户的状态（多用户）。只用 `sqlite3 tv.db ".backup <文件>"` 复制；直接复制 WAL 数据库可能漏掉最近的写入。每晚一次，每次迁移或升级之前也要（[运维手册「备份」](tenancy-phase1-runbook.zh_CN.md)）。 |
| `/var/lib/ai-passport-tv/wifi-key` | 中转加密 Wi-Fi 修改用的 X25519 私钥，在数据库旁边。丢了它，设备会拒绝 Wi-Fi 修改，直到每台都在设备上完成一次配网。和数据库一起备份。 |
| `/etc/ai-passport-tv/relay-key` | 中转的密钥：手机的密钥链接、媒体服务调中转（`TV_RELAY_KEY_FILE`）。 |
| `/etc/ai-passport-tv/token` | 配对令牌；私有固件编译时带着同一个值（被 git 忽略的 `main/av_private_config.h` 里的 `AV_PAIRING_TOKEN`）。换了令牌，这些设备就要重新编译、重新烧录。 |
| `/etc/ai-passport-tv/cstone.json` 和它指向的证书（`.pem`） | YouTube 节点的地址、密钥和固定证书。 |
| `/etc/muse-asr/key` | muse-asr 给 Muse 小玩意儿加密接口用的密钥。 |
| 海外：`/etc/yt-relay/{key,cert.pem,tls-key.pem}`、`/var/lib/yt-relay/cookies.txt` | 节点的密钥和 TLS 身份（中转固定了这张证书），以及 YouTube 登录 cookie。 |
| `/var/lib/ai-passport-tv/channels.txt` 和两个数据目录里的其他文件 | 频道表，单用户时还有 `relay-state.json`；都很小。 |

主机上 `systemctl cat` 和模板一致之后，unit 和 drop-in 本身不用另外备份，本仓库就是备份。
