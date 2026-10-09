<p align="right">
  <strong>简体中文</strong> · <a href="2026-10-08-xingzhi-cube-port-design.md">English</a>
</p>

# 移植到星智 Cube 1.54 板 —— 设计

状态：2026-10-08 已确认。分支：`board/xingzhi-cube-154`。

## 目标

让这个电视玩法跑在第二种板子上，即以"小智 AI"玩具出售的"星智 Cube 1.54 TFT ML307"，
同时不破坏 FoloToy AI Passport 的构建。功能保持一致：Wi-Fi 看直播、上下换台、音量、
亮度、频道菜单、状态页、SoftAP 配网。

验收标准：星智板子上，用**未经修改**的 `tv-server` 程序能看到带声音的直播；三个按键的
行为与 Passport 一致；电池供电下设备不掉电；Passport 的构建照常编译通过。

## 硬件事实

在用户设备上实测（esptool 读芯片和 Flash ID，逐个按键做 GPIO 探测），并与
[78/xiaozhi-esp32](https://github.com/78/xiaozhi-esp32) 的板型定义
`main/boards/nologo/xingzhi-cube-1.54tft-ml307` 交叉核对。

| 项目 | AI Passport（现有） | 星智 Cube 1.54（新增） |
| --- | --- | --- |
| 芯片 | ESP32-C3，无 PSRAM | ESP32-S3 QFN56 rev 0.2，8 MB octal PSRAM |
| Flash | 8 MB | 8 MB（Winbond `ef4017`） |
| 屏幕 | ST7789P3 240×320，SPI2 | ST7789 240×240，SPI3：MOSI 10、SCLK 9、DC 8、CS 14、RST 18，SPI mode 3，80 MHz，反色开启，无偏移 |
| 背光 | GPIO21 LEDC | GPIO13 LEDC，高电平点亮 |
| 音频输出 | ES8311 codec（I²C + I²S） | I²S 直连功放：BCLK 15、LRCK 16、DOUT 7；无 codec、无 I²C |
| 麦克风 | ES8311 ADC | I²S MEMS（WS 4、SCK 5、DIN 6）—— 本玩法不使用 |
| 按键 | 3 键共用一路 ADC 分压（GPIO0） | 3 个 GPIO，低电平有效：上 = GPIO40（音量+）、下 = GPIO39（音量−）、确定 = GPIO0（BOOT）；第 4 个键是硬件复位/电源键 |
| 电量 | CW2017 电量计（I²C） | ADC2 通道 6 电压，12 dB 衰减；充电检测 GPIO38（高 = 充电中） |
| 电源 | 常通 | **GPIO21 电源保持（RTC GPIO）：开机即拉高**；关机时拉低、hold，再进 deep sleep |
| 其他 | NFC | ML307R 4G 模组走 UART（RX 11、TX 12）—— 不使用，不去碰它 |

## 方案

保持一份源码，编译时选择板型（三个方案中的 A；B"在 fork 里整体替换 BSP"无法再合并上游，
C"在小智框架上重写"会丢掉已调好的播放器）。

1. **板型选择。** 在 `components/bsp/Kconfig` 中新增 Kconfig 选项 `AV_BOARD`，可选
   `AV_BOARD_PASSPORT`（默认）和 `AV_BOARD_XINGZHI_CUBE_154`。板子默认配置放在
   `sdkconfig.defaults.xingzhi-cube-154`，通过
   `SDKCONFIG_DEFAULTS="sdkconfig.defaults;sdkconfig.defaults.xingzhi-cube-154"`
   和 `idf.py set-target esp32s3` 生效。该文件设定板型、目标芯片、8 MB Flash 和 octal
   PSRAM。电视产品固件仍和 Passport 一样，在列表里再加上 `sdkconfig.av-prototype`
   （`CONFIG_AV_RAW_PROTOTYPE`）；不加时编出的是 BSP 演示菜单，本次移植把它当作硬件自检。
2. **BSP 拆分。** `components/bsp/include` 中的公开头文件仍是唯一接口。`bsp_pins.h`
   改为分发器，按板型包含 `boards/passport/bsp_pins_board.h` 或
   `boards/xingzhi_cube_154/bsp_pins_board.h`。源文件移到 `src/passport/*.c` 与
   `src/xingzhi_cube_154/*.c`，`CMakeLists.txt` 只编译选中的那组。共用代码
   （`bsp_display_lvgl.c`）保持公共。星智板子没有 I²C 外设：它的 `bsp_i2c_init()` 返回
   `ESP_ERR_NOT_SUPPORTED`，`bsp_i2c_scan()` 只打日志，因此 `main.c` 无需改动。
3. **"画面模式"屏幕方向作为引脚事实。** 每块板子定义 `BSP_RAW_W`/`BSP_RAW_H`（画面
   模式下帧缓冲的尺寸）以及 raw claim 的 swap/mirror。Passport：通过 swap + mirror X
   得到 320×240；星智：240×240，不做 swap。`av_protocol.h` 由它们推出
   `AV_WIDTH`/`AV_HEIGHT`，`av_player.c` 现有的静态断言改为
   `AV_WIDTH == BSP_RAW_W && AV_HEIGHT == BSP_RAW_H`。
4. **按板子设定视频几何。** `AV_VIDEO_WIDTH/HEIGHT` 由板子决定：Passport 保持
   320×180；星智使用服务端已有的 240×180。服务端会把 16:9 片源无裁切地缩放进这个尺寸，
   所以实际可见画面是 240×135，居中显示在 240×240 屏上；位置由现有的
   `AV_VIDEO_Y_OFFSET`（30 行）确定。状态、音量浮层本来就画在由这些常量算出的黑边区域里。
5. **音频。** 新板子的 `bsp_audio.c` 打开一个 I²S TX 单工通道（16 位单声道、标准 Philips
   格式，采样率由 `bsp_audio_set_format` 设定）。由于没有 codec，音量改为在
   `bsp_audio_write*` 里做软件增益；`bsp_audio_write_timeout` 保持原有约定。
   `bsp_audio_read` 返回 `ESP_ERR_NOT_SUPPORTED`。
6. **按键。** 用 `iot_button` 的 GPIO 按键（低电平有效、内部上拉），映射到
   `BSP_BTN_UP/DOWN/OK`，产生同样的 PRESS/CLICK/DOUBLE/LONG 事件。
   `bsp_button_read_mv()` 返回 −1（没有分压电路）。
7. **电量。** ADC2 CH6 单次采样后取平均，按小智的对照表换算（原始值 1970 → 0 %、
   2062 → 20 %、2154 → 40 %、2246 → 60 %、2338 → 80 %、2430 → 100 %）。
   `bsp_battery_mv()` 返回校准后引脚电压 ×2（按 1:2 分压推测，实测前标注为未验证）。
8. **电源。** 新增 `bsp_power_init()`（仅星智有效，Passport 上为空操作），在 `app_main`
   最开始把 GPIO21 拉高。
9. **服务端。** 不改代码。提供一个辅助脚本，用用户现有的 `tv-server` 再启动一个媒体服务：
   `TV_GEOMETRY=240x180`、端口 8098、不带自己的频道页，这样 Passport 那份（320×180，
   媒体端口 8096、频道页 8097）照常运行。设备配网时服务器地址填
   `<电脑主机名>.local:8098`。
10. **刷机。** 用 `esptool` 在 `0x0` 写入合并后的 8 MB 镜像。新板子同样保留 Passport 的
    受保护分区布局（3 MB 应用、`cardid` 位于 `0x356000`），两块板共用一张分区表。

## 测试

- 主机测试：现有测试保持全部通过；为电量换算和软件音量缩放（纯函数）新增主机测试。
- 构建：两块板（`esp32c3` 的 Passport、`esp32s3` 的星智）都以 `AV_PUBLIC_BUILD=ON`
  编译。
- 设备测试，按顺序：（1）用 BSP 演示菜单固件做硬件自检，包括 Display、Button、Audio、
  Battery 四页以及串口日志；（2）SoftAP 配网；（3）连接 240×180 的服务实例看直播，
  持续 10 分钟，记录帧率、音频欠载和会话重置次数。

## 不在本次范围

4G 模组、麦克风、BLE、除关机路径以外的深度睡眠省电，以及向上游推送任何东西（需用户另行同意）。

## 风险

- 电池分压比例和功放增益是推断的，尚未实测。
- ESP32-S3 在 64 KB TCP 窗口下的 Wi-Fi/lwIP 吞吐未经测试；S3 内存更大，预计比 C3 更宽裕。
- 4G 模组默认仍上电；如果耗电明显或造成射频干扰，后续可能需要让它保持复位状态。
