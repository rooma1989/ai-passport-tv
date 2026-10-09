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

令牌是明文传输的：能挡住扫描端口的人，但不是加密。改频道就改主机上的 `channels.txt` 然后重启服务。

## 播放时的按键

| 按键 | 短按 | 长按 |
| --- | --- | --- |
| 上2（音量+） | 音量 +10% | 下一个台 |
| 上1（音量−） | 音量 −10% | 上一个台 |
| 上3（BOOT） | 频道列表 | 亮度 |
| 任意键双击 | 状态页（在状态页长按音量+ 进入 Wi-Fi 设置） | |
| 最下面 | 硬件复位；长按开关机 | |

上下键与 Passport 相反，因为外壳上印的就是音量+/−（`BSP_ARROW_CLICK_IS_VOLUME`）。

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
