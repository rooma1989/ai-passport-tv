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

## 网页刷机和发布的镜像

网页刷机（[`packaging/flash/`](../../packaging/flash/index.html)）只写独立版 Passport 镜像，从 `0x0`
开始写。写之前先读 `0x10000`：读到 Muse 的分区表就停下并指到这里，因为整块镜像会盖掉 Muse
的分区表、NVS（配对）和程序。读不出 flash 时，要用户先确认这台 Passport 没装 Muse 才写。
这种布局请用上面的手动步骤。

发布里没有签好名的 `ota_1` 镜像（自托管设计 Q9）。调查结果（2026-10-16）：上面用的密钥是公开仓库
facebookincubator/muse-gadget-sdk（Apache-2.0）里的 `esp32/dev_signing_key.pem`。那个仓库自己的
`sdkconfig.defaults` 说它是故意提交的共享开发密钥，正式发货前要换成不进 git、由 CI 注入的私有发布
密钥；`CONFIG_HOMEHUB_DEV_BUILD` 默认打开，所以从 SDK 编出来的 Muse 接受用它签名的镜像，用发布
密钥编的 Muse 则会拒绝。所以 CI 不需要保管任何机密，从上游按固定提交取这个文件就能签名。现在不做，
因为：能切到 `ota_1` 电视的 Muse 只有自己从源码带 `CONFIG_MUSE_TV_SLOT` 编出来的（本项目的 Muse
分支），这些人本来就会用同样的方法编、签电视镜像；用作者明说「不能拿去发货」的密钥签名公开发布，需要
他们同意；给刷机页再加一种只写 `ota_1` 的模式，风险大于这点用户的需要。等电视槽位被上游接受后再看。

## 服务器

画面尺寸是每个服务器进程固定的，而且一次只服务一台设备，所以 Passport 在星智那台旁边有自己的实例：
`tv-passport`（[`packaging/systemd/tv-passport.service`](../../packaging/systemd/tv-passport.service)
及其 drop-in；[`host-setup.zh_CN.md`](../development/host-setup.zh_CN.md)），照抄 `tv-xingzhi`，改成 `TV_GEOMETRY=320x180`、`TV_FPS=12`、端口 8097，
用自己的 `TV_DATA_DIR`、正在播放文件和中转事件文件（`TV_RELAY_EVENTS_FILE=/var/lib/ai-passport-tv-passport/relay-events.json`）。
中转用 `--extra-device Passport:/var/lib/ai-passport-tv-passport/now-playing.json:/var/lib/ai-passport-tv-passport/relay-events.json`
认识它；手机遥控自动跟着最近换过台的那台设备，也可以在页面顶上点名遥控哪一台。云服务器的防火墙也要放行这个端口。

码率和星智一样是 200000。复杂画面每秒 140–165 KB 时，ESP32-C3 解码和刷屏的耗时超过实际时长，帧率掉到每秒 9.5–11 帧。
曾经把 Passport 限制在每秒 110 KB，帧率是跟上了，颜色却坏了：每帧都要塞进字节预算，塞不下就逐档减少颜色（`frames.encode_within`），
每帧只有约 9 KB 时，CCTV1 这类细节多的台一直落在最粗的几档，只剩 8 种纯色。固件把按键任务提到视频任务之上以后，这个上限就去掉了。

C3 是单核，复杂画面时视频任务（优先级 5）一直处于就绪状态，而处理按键的任务——运行
`av_player_main()` 的 main 任务，优先级 1——要等画面平静下来才轮得到。Passport 的板级头文件设了
`BSP_KEYS_ABOVE_VIDEO 1`，把这个任务提到优先级 6，刚好高过视频任务；它平时在按键队列上以 50 ms 为单位等待，
所以画面几乎不受影响，声音（7）仍在它之上。每次按键都会打印 `key <n> ev <n> waited <ms> ms`，即从按键任务到处理按键的等待时间。
双核的星智设为 0，保持原来的优先级。

一路 Passport 约 120–150 kB/s（画面加声音）。公网带宽 3 Mbit/s 的主机上，两台同时看会分摊带宽，两边画质都会下降。

## 语音（按住说话）

带 `CONFIG_AV_VOICE`（默认开）编译、服务器的 CONFIG 又提供语音时，播放中或在频道列表里按住确定键 0.45 秒就是说话；松开结束，▲ 取消。短按确定键仍打开频道列表；在频道列表和语音结果里，双击确定键返回上一级（长按现在用来说话）。亮度仍是上/下键双击，其他按键都不变。服务器不提供语音时，按键和以前完全一样。

麦克风用 ES8311 的，从播放器本来就以 16 kHz/16 位全双工打开的 I2S0 接收通道读（左声道、输入增益 30 dB、`no_dac_ref`）：不重开 codec，电视声音不会断；从按下确定键到录音结束，电视声音在软件里静音。按住期间语音占约 10 KB 缓冲加 3 KB 任务栈，结束后归还；日志会打印 `capture started: buffers=… heap=… largest=…` 和 `voice buffers freed; heap=… min_heap=…`。频道列表或语音页盖住画面时，C3 不解码画面。尚未在设备上验证：电视刚静音的房间里的录音电平，以及复杂画面下语音期间和之后的帧率与按键延迟。

## 配网

设计：[`2026-10-13-wifi-setup-design.zh_CN.md`](../superpowers/specs/2026-10-13-wifi-setup-design.zh_CN.md)。

- **声波**（配网第 1 页，固件链接了解码器 `main/av_soundprov.c` 时）：二维码指向 `<SITE>/TV/W`；手机页面把 Wi-Fi 放成声音，
  屏幕显示 `正在接收… 60%`，设备连上后重启。热点那几页排在后面，作为备用。
- **自动配网**：存了 Wi-Fi 但开机 60 秒一个也连不上时，自己进入配网；保存的 Wi-Fi 回到范围内时自己退出。
- **服务器栏**：固件知道服务器时（私有头文件，或编译目录自己 sdkconfig 里的 `CONFIG_AV_DEFAULT_SERVER`）收进「高级」。
- **手机上管理**：主人可以查看、添加、删除、调整顺序、立即切换保存的 Wi-Fi（遥控页 我的 → 我的设备 → Wi-Fi；电脑版 设备 → Wi-Fi），
  只在多用户中转下。密码端到端加密到设备（X25519 + AES-256-GCM）；设备按这个顺序连能看到的 Wi-Fi，弱的（≤ −80 dBm）排最后。
- Passport 特有：麦克风是 ES8311 的，读配网为了底噪以 16 kHz 打开的 I2S0 接收通道；底噪照常放（要在设备上确认影响）。
  加密消息用的密钥对和记住的中转公钥在 `avsettings` 里，Muse 固件不碰它。
