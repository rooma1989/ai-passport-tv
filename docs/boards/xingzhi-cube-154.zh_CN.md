<p align="right">
  <strong>简体中文</strong> · <a href="xingzhi-cube-154.md">English</a>
</p>

# 星智 Cube 1.54 板

同一份电视固件也能编给"星智 Cube 1.54 TFT ML307"：一块以小智 AI 玩具出售的 ESP32-S3
板子（1.54 寸屏、4G 模组、电池、右侧四个按键）。设计说明见
[`docs/superpowers/specs/2026-10-08-xingzhi-cube-port-design.zh_CN.md`](../superpowers/specs/2026-10-08-xingzhi-cube-port-design.zh_CN.md)。

## 与 AI Passport 的区别

| | AI Passport | 星智 Cube 1.54 |
| --- | --- | --- |
| 芯片 | ESP32-C3 | ESP32-S3，8 MB Flash |
| 屏幕 | 240×320，画面 320×180（横屏） | 240×240，画面 240×180（16:9 实际显示 240×135） |
| 音频 | ES8311 codec | I²S 直连功放，软件音量 |
| 按键 | 3 键共用一路 ADC | 3 个 GPIO 键 + 1 个硬件复位/电源键 |
| 电量 | CW2017 电量计 | ADC 读电压，百分比较粗略 |

引脚与板级事实在
[`components/bsp/include/boards/xingzhi_cube_154.h`](../../components/bsp/include/boards/xingzhi_cube_154.h)；
编译配置在 [`sdkconfig.defaults.xingzhi-cube-154`](../../sdkconfig.defaults.xingzhi-cube-154)。

## 编译

激活 ESP-IDF 5.5.3 后：

```bash
./tools/validate.sh --xingzhi
```

镜像输出到 `build/Xingzhi-Cube-154-tv-public.bin`。

## 刷机

板子不响应时先进下载模式：按住 **BOOT**（从上往下第三个键），再短按最下面的键。
第一次刷之前先整片擦除，因为小智固件的 NVS 落在本固件的 NVS 范围里：

```bash
python -m esptool --chip esp32s3 -p /dev/cu.usbmodem1101 erase_flash
```

```bash
python -m esptool --chip esp32s3 -p /dev/cu.usbmodem1101 -b 921600 write_flash 0x0 build/Xingzhi-Cube-154-tv-public.bin
```

## 服务端

Passport 的服务发 320×180，星智固件只接受 240×180，所以用同一个 `tv-server` 程序再开一个媒体服务：

```bash
tools/start_xingzhi_media.sh
```

它监听 **8098** 端口，并打印设备设置页要填的地址（`<主机名>.local:8098` 或 `<局域网 IP>:8098`）。
Passport 那份照常占用 8096 和 8097。设备填错端口时能连上但会被拒绝，日志里是
`CONFIG field width: server=320 device=240`。

## 云服务器

媒体服务也可以跑在一台常开的 Linux 主机上，这样电脑不用开着。实测于 2 核、出网带宽 3 Mbps 的
Alibaba Cloud Linux 3 主机：15 帧、声音满速率，ffmpeg 约占一个核的 12%。

1. **Python 3.9 以上。** 代码只用标准库。在 Alibaba Cloud Linux 3 / RHEL 8 上
   `dnf install python3.11` 会与系统自带的 Python 并存。发布的 `tv-server-linux-x86_64`
   需要 glibc 2.39，在这类主机上跑不起来。
2. **ffmpeg。** 用动态链接的版本，比如 BtbN 的 `ffmpeg-n8.1-latest-linux64-gpl`（glibc 2.28+）。
   `tools/ffmpeg_fetch.py` 下载的全静态版在该主机上解析域名时会崩溃。
3. 把 `server/`、`tools/` 和你的 `channels.txt` 复制到主机，建一个 `tvserver` 系统用户，
   以及一个只有它能读的配对令牌文件。
4. 安装 [`packaging/systemd/tv-xingzhi.service`](../../packaging/systemd/tv-xingzhi.service)。
   `TV_ALLOW_PUBLIC=1` 允许公网地址的设备连接；没有配置令牌时服务会拒绝启动。
   `TV_FPS` 和 `TV_RATE_START` 让码流控制在较小的上行带宽内。
5. 在云厂商的**安全组**和主机**防火墙**两处都放行端口（`firewall-cmd --permanent --add-port=8098/tcp`）。
6. 用同一个令牌编译设备固件：新建被 git 忽略的 `main/av_private_config.h`（`AV_PAIRING_TOKEN`，
   可选默认的 `AV_SERVER_IPV4`/`AV_SERVER_PORT`），不加 `AV_PUBLIC_BUILD` 编译。然后在设置页填
   `<服务器IP>:8098`。

令牌是明文传输的：能挡住扫描端口的人，但不是加密。改频道就改主机上的 `channels.txt`，下次换台时服务端会重新读取
（`TV_RELAY_CHANNELS_FILE` 也一样）。

## UP 主频道

同一台主机上的视频中转把每个关注的 B 站 UP 主变成一个台：播放他最新的、没看过的视频，播完接下一个，中断后从停下的地方继续。
设计和实测数据见
[`docs/superpowers/specs/2026-10-09-creator-channels-and-cast-design.zh_CN.md`](../superpowers/specs/2026-10-09-creator-channels-and-cast-design.zh_CN.md)。

1. 安装 [`packaging/systemd/tv-relay.service`](../../packaging/systemd/tv-relay.service)，在
   `/etc/ai-passport-tv/relay-key` 里放一个至少 16 个字符的随机密钥（权限 0640，组 `tvserver`）。它只监听本机回环：
   8099 给媒体服务拉流，8100 是管理网页。
2. 媒体服务的单元里加 `TV_RELAY_CHANNELS_FILE`（模板里已有）；UP 主频道排在 `channels.txt` 前面。
3. 由网站转发管理网页：把 [`packaging/nginx/tv-relay.conf`](../../packaging/nginx/tv-relay.conf) 放进站点的 nginx 配置
   （宝塔面板主机放在站点的 `extension/<站点>/` 目录），检查后重新加载 nginx。
4. 手机登录一次（见下面的「手机遥控」），在「UP 主」标签粘贴 UP 主主页链接或他的任意一个视频链接。设备下次换台后就能看到这个台。

同一个网页还能投屏单个视频：粘贴 B 站视频链接，设备在一秒内自动切到「我的投屏」（总在频道列表第一个），播完回到原来的台。
中转把这件事写进 `relay-events.json`；媒体服务每秒读一次（`TV_RELAY_EVENTS_FILE`），发 `SWITCH` 包给设备，
但只发给握手时声明支持的固件。旧固件手动选「我的投屏」也能看。

B 站对云服务器地址的网页和带签名的接口返回 412，所以中转直接调用不带签名的网页 API，不用 yt-dlp。

同一个网页也能粘贴 **YouTube** 链接，加频道、投屏都行。国内主机从不连 YouTube：海外主机上的节点
（[`server/relay/ytnode.py`](../../server/relay/ytnode.py)、[`packaging/systemd/yt-relay.service`](../../packaging/systemd/yt-relay.service)）
运行 yt-dlp 并转成约 400 kbit/s 的小码流，中转经 HTTPS 读取，只信任节点的自签证书，并带密钥（`--cstone` 指向含 `url`、`key`、`cert`
的 JSON）。节点端口只对中转所在主机开放。节点需要 yt-dlp、deno（yt-dlp 的 JavaScript 运行环境）和 ffmpeg，实际上还需要一个
Google 小号在无痕窗口里导出的 cookies 文件：试过的所有客户端身份，YouTube 都要求海外主机登录。单核上解析一个视频约 18 秒，
所以节点缓存 30 分钟；新 YouTube 视频第一次出画面要 20–40 秒。

## 手机遥控

中转的网页是设备的遥控器，地址 `https://<站点>/tv/`。设计见
[`docs/superpowers/specs/2026-10-10-grouped-channels-and-remote-design.zh_CN.md`](../superpowers/specs/2026-10-10-grouped-channels-and-remote-design.zh_CN.md)。

- **登录**：打开一次 `https://<站点>/tv/?k=<密钥>`，或扫设备状态页上的二维码（`https://<站点>/TV/R/<扫码码>`）。两者都会设置一个
  180 天的会话 cookie，之后直接打开 `https://<站点>/tv/` 即可。在登录后的页面上加到桌面（iPhone：Safari →「分享」→「添加到主屏幕」）；
  它的网址带着会话，因为桌面网页应用不和 Safari 共用 cookie。「UP 主」标签里的「换一个扫码码」会让所有会话失效。
- **遥控**：正在播放、分组、该组的台；点一下设备约 1 秒后换台。「调整分组」可以把直播台移到别的组（`channel-groups.json`）。
- **投屏**：粘贴 B 站或 YouTube 链接；最近 50 次投屏存在服务器上，点一下重播。
- **UP 主**：增删 UP 主；使用说明的入口。
- **分组**按台名自动归类（`server/channel_groups.py`）：我的投屏、UP 主、央视、卫视、港澳台、国际、音乐、动画影视、其他。
  媒体服务在 CONFIG 里只发一次分组名，每个台带一个编号。
- **服务端设置**：中转的单元加了 `--main-channels`、`--groups-out`、`--now-playing`、`--remote-url-out`、`--public-url`；
  媒体服务的单元加了 `TV_CHANNEL_GROUPS_FILE`、`TV_NOW_PLAYING_FILE`、`TV_REMOTE_URL_FILE`（两个模板里都有）。
  nginx 需要 `packaging/nginx/tv-relay.conf` 里的 `/tv/` 和 `/TV/R/` 两段。

- **播放与设备控制**：UP 主台和投屏台上，「遥控」标签有进度条、退 / 进 30 秒、暂停 / 继续和下一个：中转掐断它在推的流，媒体服务的解码器
  自己从新位置重新打开；暂停时设备停在 PAUSED 画面并持续静音，最长 30 分钟。音量、亮度滑条显示设备的当前值：固件在 HELLO 里、以及每次变化时用
  STATUS 包（类型 9）上报，手机的调节由带 `volume` 或 `brightness` 的 AV_SWITCH 发给设备。设计见
  [`docs/superpowers/specs/2026-10-10-playback-and-device-controls-design.zh_CN.md`](../superpowers/specs/2026-10-10-playback-and-device-controls-design.zh_CN.md)。
- **宠物**：「遥控」标签里的「宠物」一行打开「我的宠物」：从 11 只里选一只点「换上」，设备保存下来并让它开心 2 秒。
  `POST /api/pet` 写 `pet` 事件；媒体服务只给 HELLO 里带 `pet` 的固件发 `AV_SWITCH {"pet": i}`，固件在 HELLO 和 STATUS 里报当前宠物。
  快进和下一个也会写 `seek`/`next` 事件，以 `{"seek": 秒}`/`{"next": 1}` 发给 HELLO 带 `"hint": 1` 的固件，设备画一个提示框，画面恢复后消失。
- **自动保养**：YouTube 节点每 6 小时检查一次测试视频，cookies 需要更换时网页会提示；`packaging/yt-relay/yt-dlp-update.timer` 每周更新
  yt-dlp，新版能解析测试视频才替换。

拿到密钥链接、设备上的二维码或已登录手机的人，都能控制设备、增删 UP 主。

## 界面

设备界面按 Claude Design 交来的设计稿重做（暖墨色、像素图标、11 只宠物），做了什么见
[`docs/superpowers/specs/2026-10-10-device-ui-redesign-design.zh_CN.md`](../superpowers/specs/2026-10-10-device-ui-redesign-design.zh_CN.md)。
`tools/render_ui/render_ui.sh out.png` 在电脑上用固件自己的绘图代码和字库画出所有界面，不刷机就能核对版式。

- 顶条（第 0–29 行）一直显示：左边是标签（播放时是台号和台名，其他时候是页面名），右边是信号、电池和电量。
  低于 20% 填充和数字变朱红，低于 10% 数字闪烁，充电时（GPIO38）显示闪电。
- 菜单和页面铺满整个画面区（第 30–209 行），底是不透明的；底条（第 210–239 行）显示列表的帧率、网速或页面的按键提示。
- 画面上的提示：音量条（手机调亮度时是亮度条）、换台横幅、快进框、暂停横条。
- 没有画面时：正在连接 Wi-Fi、正在连接服务器（开机后第一次）、换台中、连不上服务器，四种画面上宠物会跳动。
- 选的宠物存在 NVS（`avsettings/pet`）；在状态页长按第二个键打开选择宠物。
- **只听声音模式**只放声音，屏幕上只有宠物（[设计](../superpowers/specs/2026-10-10-audio-only-mode-design.zh_CN.md)）。双击 OK、在状态页短按 OK
  或在手机上开关，屏幕上是 6 倍大的宠物站在声波条上、跟着声音动，有音符、声波条和小星星，屏幕变暗后宠物睡着；它会存下来（`avsettings/audio`），设备重连一次，服务器就不再发画面（约 8 KB/s，原来约 200 KB/s）。
  这时按声音判断台是否正常；10 秒不操作背光降到最低一档。
- 配网（状态页里长按最上面的键）分成几页，最上面的键翻上一页、第二个键翻下一页，长按 OK 退出：
  1. Wi-Fi 二维码，用手机**系统相机**扫码加入热点（微信、支付宝不能扫码连 Wi-Fi）；
  2. 配网页 `http://192.168.4.1` 的二维码；
  3. 使用说明页的二维码（设置了 `AV_HELP_URL` 时才有）；
  4. 热点、配网地址、服务器、连接状态的文字，以及 15 分钟配网时间还剩多少。
- 配网页上点以前连过的 Wi-Fi 会填好名字；密码留空就用记住的密码。记住的密码只存在设备上，不会发给网页。
- 状态页的信号显示 dBm。HELLO 报网络名、信号、信道和发射功率，STATUS 每 30 秒报一次信号，媒体服务记进日志（`device wifi ...`、
  `device rssi=...`）。发射功率默认 11 dBm（`sdkconfig.defaults.xingzhi-cube-154`；20 dBm 在路由器附近吞吐反而差）；
  离路由器远时可以在配网页「高级选项」调高，存在 NVS 里，Wi-Fi 启动后生效。
- HELLO 还带开机时长和重启原因；开机时长比「上次 HELLO 的开机时长加上之后过去的时间」小时，媒体服务记一行 `device boot reason=...`，
  所以即使中间放了很久，也能分清是设备重启还是网络断了一下。
- 服务器下发了遥控地址时（CONFIG 里的 `remote_url`），状态页显示手机遥控的二维码（第 2 版，每个码点 3 像素）；
  否则在设置了 `AV_HELP_URL` 时显示说明页的小二维码。
- `AV_HELP_URL` 来自被 git 忽略的 `main/av_private_config.h`，公开版为空。网址要短并用大写
  （例如 `HTTPS://EXAMPLE.COM/TV/`）：这样能放进版本 1 的二维码，状态页只放得下这么大。
  说明页本身是 [`packaging/help/index.html`](../../packaging/help/index.html)，复制到网站对应目录即可。
- 最多记住 10 个 Wi-Fi，开机自动连附近能找到的那个。扫描 1–13 信道。找不到已保存的网络时，屏幕会显示正在连接的网络名；
  双击再长按最上面的键进入配网。

## 播放时的按键

| 按键 | 短按 | 长按 |
| --- | --- | --- |
| 最上面的键（外壳印"音量−"） | 音量 +10% | 同组的下一个台 |
| 第二个键（外壳印"音量+"） | 音量 −10% | 同组的上一个台 |
| 上3（BOOT） | 频道列表 | 亮度 |
| 最上面或第二个键双击 | 状态页（在状态页长按最上面的键进入 Wi-Fi 设置，长按第二个键选择宠物，短按 OK 只听声音） | |
| 上3（BOOT）双击 | 只听声音 / 回到看画面 | |
| 最下面 | 硬件复位；长按开关机 | |

服务器下发了分组时，频道列表分两级：打开时停在正在播的台所在的组；OK 进入一个组，长按 OK 返回上一级，再长按关闭。
失效的台会自动跳到同组的下一个台。

短按调音量、长按换台，因为这两个键在外壳上就是音量键（`BSP_ARROW_CLICK_IS_VOLUME`）；Passport 现在也一样。
上下按物理位置排列，不按外壳印的音量−/+，因为按印的字用起来是反的。

## 针对这块板的修正（均在一台设备上实测）

- **Wi-Fi 发射功率上限 11 dBm。** 20 dBm 时板子电源被拉抖，纯 TCP 只有 15–50 KB/s；
  13 dBm 约 230 KB/s，11 dBm 约 400 KB/s，8 dBm 又掉回 15 KB/s。测于 2026-10-08，
  RSSI 约 −50 dBm、拥挤的 2.4 GHz 信道 11；换个环境可能需要别的值。
- **开机让 4G 模组进入飞行模式**（`AT+CFUN=0`）。SIM 被拒时它不停尝试附着，是欠压复位的原因之一。
- **Wi-Fi 事件任务栈 4096 字节。** 共用的 2304 在 S3 上配网后立刻溢出。
- 开机打印上次复位原因（`bsp_power: 上次复位原因: N`；1 = 复位键或上电，4 = 崩溃，
  9 = 欠压，11 = USB），因为 USB 重新枚举期间 ROM 自己打印的复位原因会丢。

## 未验证

- 电池分压比（按 1:2 推测），因此 `bsp_battery_mv()` 未经核实；充电时百分比偏高。
- 超过约十分钟的长时间稳定性，以及在其他 Wi-Fi 网络下的表现。
- 4G 模组在飞行模式下的耗电，以及是否应改为让它保持复位。
- `tools/start_xingzhi_media.sh` 在 Windows 上未测试（仅 macOS）。
