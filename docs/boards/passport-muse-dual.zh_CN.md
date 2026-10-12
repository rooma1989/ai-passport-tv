<p align="right">
  <strong>简体中文</strong> · <a href="passport-muse-dual.md">English</a>
</p>

# AI Passport：和 Muse 固件并存的播放器

AI Passport 可以同时装两套固件：`ota_0` 里是 Muse 固件
（github.com/facebookincubator/muse-gadget-sdk，配置 `ai-passport`），`ota_1`
里是这个播放器。两边都是把启动分区设成对方再重启来切换：

- 从 Muse 到播放器：Settings > **TV**（Muse 编译时打开 `CONFIG_MUSE_TV_SLOT=y`）。
- 从播放器回 Muse：打开状态页（双击方向键），然后**双击 OK**。页面底部的按键提示会写明。

两套固件共用 Muse 的分区表（`partitions-muse-dual.csv`，照抄 Muse 的
`partitions_muse_8mb.csv`，位于 0x10000）和它的 NVS 分区，各用各的命名空间。
Muse 的引导程序保持不动，只写播放器的应用镜像。

## 播放器有哪些不同（`CONFIG_AV_MUSE_DUAL`）

- **从不擦除 NVS。** Wi-Fi 组件在初始化失败时会擦除 NVS，那会抹掉 Muse 的配对，这里跳过。
- **把自己标记为可用**（`esp_ota_mark_app_valid_cancel_rollback`）：在显示和 Wi-Fi
  协议栈起来后马上做，早于播放器自己的任何重启。Muse 的引导程序开着回滚，不标记的话会再启动回 Muse。
- 第一次启动、自己还没有网络时，**用 Muse 的 Wi-Fi**（命名空间 `homehub`，键 `ssid`/`password`）；
  在播放器里设过音量之前，**用 Muse 的音量**（命名空间 `muse`，键 `volume`）。
- 状态页的按键提示写着回 Muse 的方法。

## 编译、签名、刷写

用 ESP-IDF 5.5.3 做私有编译（服务器地址和配对 token 来自 `main/av_private_config.h`；
`AV_SERVER_PORT` 填 Passport 实例的端口）：

```sh
SDKCONFIG_DEFAULTS="sdkconfig.defaults;sdkconfig.av-prototype;sdkconfig.muse-dual" \
  idf.py -B build-muse-dual -D IDF_TARGET=esp32c3 -D AV_PUBLIC_BUILD=OFF \
  -D SDKCONFIG=build-muse-dual/sdkconfig build
```

Muse 切换到某个镜像时会校验签名（`CONFIG_SECURE_SIGNED_ON_UPDATE`），所以要用 Muse 的密钥
给应用签名（用 ESP-IDF 6 的 espsecure，Secure Boot v2 RSA 方案），然后只写入 `ota_1`：

```sh
espsecure sign-data --version 2 --keyfile <muse-gadget-sdk>/esp32/dev_signing_key.pem \
  --output build-muse-dual/tv-ota1-signed.bin build-muse-dual/FoloToy-AI-Passport.bin
python -m esptool --chip esp32c3 -p PORT write-flash 0x400000 build-muse-dual/tv-ota1-signed.bin
```

不要刷这次编译出来的引导程序、分区表和 `ota_data_initial.bin`。
`tools/verify_firmware.py` 按播放器自己的布局检查（factory 在 0x10000），不适用于这种布局。

## 服务器

画面尺寸是每个服务器进程固定的，而且一次只服务一台设备，所以 Passport 在星智那台旁边有自己的实例：
`tv-passport`，照抄 `tv-xingzhi`，改成 `TV_GEOMETRY=320x180`、`TV_FPS=12`、`TV_RATE_START=110000`、端口 8097，
用自己的 `TV_DATA_DIR`、正在播放文件和中转事件文件（`TV_RELAY_EVENTS_FILE=/var/lib/ai-passport-tv-passport/relay-events.json`）。
中转用 `--extra-device Passport:/var/lib/ai-passport-tv-passport/now-playing.json:/var/lib/ai-passport-tv-passport/relay-events.json`
认识它；手机遥控自动跟着最近换过台的那台设备，也可以在页面顶上点名遥控哪一台。云服务器的防火墙也要放行这个端口。

码率比星智的 200000 低，因为 ESP32-C3 解不动更多：复杂画面每秒 140–165 KB 时，解码和刷屏的耗时超过了实际时长，帧率掉到每秒 9.5–11 帧，按键也要等画面平静下来才响应。每秒 110 KB 以下它有余量。

固件这边不只靠这个上限。C3 是单核，复杂画面时视频任务（优先级 5）一直处于就绪状态，而处理按键的任务——运行
`av_player_main()` 的 main 任务，优先级 1——要等画面平静下来才轮得到。Passport 的板级头文件设了
`BSP_KEYS_ABOVE_VIDEO 1`，把这个任务提到优先级 6，刚好高过视频任务；它平时在按键队列上以 50 ms 为单位等待，
所以画面几乎不受影响，声音（7）仍在它之上。每次按键都会打印 `key <n> ev <n> waited <ms> ms`，即从按键任务到处理按键的等待时间。
双核的星智设为 0，保持原来的优先级。

一路 Passport 约 120–150 kB/s（画面加声音）。公网带宽 3 Mbit/s 的主机上，两台同时看会分摊带宽，两边画质都会下降。
