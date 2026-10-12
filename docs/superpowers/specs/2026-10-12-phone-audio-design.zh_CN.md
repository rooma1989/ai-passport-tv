<p align="right">
  <strong>简体中文</strong> · <a href="2026-10-12-phone-audio-design.md">English</a>
</p>

# 用手机听 —— 设计

状态：设计文档，另有一个服务器端原型（`server/listen.py`、`tools/listen_proto/`），
只能在电脑上配合合成节目运行。尚未接入媒体服务、中转和固件，也还没在 iPhone 上试过。
日期：2026-10-12。

## 目标

手机遥控页播放所选设备正在放的声音，这样就能用和 iPhone 配对的蓝牙耳机或音箱来听。
设备照常显示画面，自己的扬声器静音。ESP32-S3（星智）和 ESP32-C3（护照）只有 BLE，
没有经典蓝牙，也就没有 A2DP，所以只能让手机来当这座桥。

**用户的要求（2026-10-12）：** iPhone 锁屏、或网页退到后台时，声音也要接着放。
声音晚几秒没关系，设备的画面可以跟着推迟来对上。

## 结论一览

1. **传输：用 `<audio>` 元素播放 HLS 音频流**（AAC，16 kHz 单声道，每段 1.024 秒的
   packed audio）。在 iOS 上，只有声音的媒体元素锁屏时不会被挂起；Web Audio 会被挂起，
   除非满足额外条件（见 §1）。
2. **手机流是主时间线，设备晚 H 秒照同一份时间表播放**（H 默认 5 秒）。这段延迟放在
   服务器上，用的是媒体服务本来就有的频道队列。设备只存得下不到一秒的声音、两三帧画面，
   放不下几秒的延迟（见 §2）。
3. **两端各自对准服务器时钟。** 每段都带 `EXT-X-PROGRAM-DATE-TIME`，含义是“设备播这段的
   墙钟时刻”。手机读出自己正放到哪儿（`getStartDate() + currentTime`），和校准过的时钟、
   蓝牙延迟比一比，再跳转对齐。服务器则靠补上或跳过 40 ms 的小块，让设备守住同一份时间表。
   设备扬声器是静音的，所以这些调整听不见。
4. **预计对齐精度：** 网页在前台、固件上报播放位置、蓝牙延迟已知时，约 ±60–80 ms；
   没有位置上报约 ±250 ms。锁屏期间另有缓慢漂移，每小时最多约 0.1 秒，下次打开网页时纠正
   （见 §3）。
5. **代价：** 每个收听者约 5 kB/s（3 Mbit/s 主机的 1.4% 左右）；编码和取声音约占 1–3% CPU；
   每台设备的队列内存多出几 MB（见 §4）。
6. **固件改动：** 一个只在本次会话有效的 `mute`（保存的音量不变），以及在 `STATUS` 里报播放
   位置。两样都很小（见 §5）。

## 1. iOS 上的传输方式与播放器

调研结论如下（来源见文末）。“已核实”指在 WebKit 源码、提交记录、MDN 兼容数据或
缺陷单里查到的；“推断”指还需要在 iPhone 上验证的。

| 方案 | 锁屏 / 后台 | 延迟 | 对齐手段 | 结论 |
| --- | --- | --- | --- | --- |
| **`<audio>` + HLS**（原生） | **继续放。** 已核实：iOS 上后台受限的只有视频和 Web Audio，只有声音的元素不受限。 | 段长 + 末端保留量：用我们的 1 秒段实测 3–5 秒 | `getStartDate()` 加 PROGRAM-DATE-TIME 给出正在放的位置对应的墙钟时刻；设 `currentTime` 跳转是精确的 | **采用** |
| Web Audio，经 WebSocket/fetch 喂数据 | 默认会被打断（状态变成 `"interrupted"`）。已核实：设了 `navigator.audioSession.type = "playback"` 后允许后台播放（WebKit 提交 275558@main，2024-03；大约从 iOS 17.5 起可靠）。加到主屏幕的网页应用里还有未修的缺陷（291892、276016）。后台时 WebSocket 和 JS 是否还在跑，没有确证。 | 0.2–1 秒 | 采样级精确调度；`outputLatency`（iOS 18.4+）包含蓝牙路由 | 只作第 3 阶段的试验 |
| HTTP 渐进式 MP3/AAC | 继续放 | 3–6 秒，由 Safari 决定 | 无（没有 PDT） | 不用 |
| Managed Media Source（iOS 17.1+） | 继续放（audio 元素） | 约 0.3–1 秒，由我们控制 | 完全可控 | 以后可以考虑；网页代码多很多 |
| WebRTC | 不确定，有锁屏被挂起的报告；`jitterBufferTarget` 要到 iOS 27 才有 | 0.1–0.5 秒 | 弱 | 不用 |

另外几条影响网页设计的结论：

- **Media Session**（iOS 15+）：支持元数据、播放/暂停和跳转回调，`<audio>` 元素可以显示在
  锁屏的“正在播放”里。已核实。
- **媒体元素拿不到蓝牙延迟。** AVFoundation 只会让同一个元素自己的画面和声音对齐，不知道
  另一台设备上的画面。网页可以临时开一个 `AudioContext` 去读：`outputLatency`（iOS 18.4+）
  等于音频单元延迟加上 AVAudioSession 的 `outputLatency`，因此包含蓝牙路由（WebKit 源码
  已核实）。没在发声时它读出 0，开启防指纹保护时会被固定成一个值。实测 AirPods 延迟从
  126 ms（Pro 2）到 274 ms（一代）不等，而且刚连上的头几分钟，系统报的值可能偏差 50 ms
  以上。所以网页同时提供自动读取、预设和滑条。
- `navigator.audioSession.type = "playback"`（Safari 16.4+）还能让声音不受静音拨片影响。
  网页会设置它，对 `<audio>` 没有坏处。
- 开始播放必须点一下屏幕。来电、Siri 等打断会让元素暂停；从锁屏恢复播放属于用户操作。

## 2. 架构：手机流在前，设备晚 H 秒跟着放

```
  源 ─ffmpeg─▶ LiveChannel 队列（按内容顺序；deque，最多 16 秒）
                      │                       │
       往前看，不出队  │                       │  按设备的节奏出队（现在的发送端）
     （ChannelTap）   ▼                       ▼
                Ledger：每 40 ms 一条          _pace_live ── 问 Ledger.for_device(play_at)
                 = 第 X 块内容，或静音            • 正好    → 发第 X 块
                      │                          • 设备早了 → 发填充静音（只发给设备）
                      ▼                          • 设备晚了 → 跳过条目
               ffmpeg AAC ─▶ 每段 1.024 秒              │
               PDT = 设备播它的时刻                      ▼
                      │                           设备（扬声器静音）在 published(k) + H
                      ▼                           播第 k 条
               nginx /tv/listen/dN/ ─▶ iPhone <audio>，跳转到
                                       “正在放的 PDT = 服务器现在 + 蓝牙延迟”
```

- **时间表**（`server/listen.Ledger`）：每过 40 ms 实际时间，就在手机流末尾追加一条：
  频道队列里的下一块内容；源暂时没数据时追加静音。第 *k* 条在
  `published(k) = start + k·40 ms` 成为流的一部分，设备必须在 `published(k) + H` 播它。
- **设备照时间表走。** 发送端要发一块声音、设备会在 `play_at` 播它时，调用
  `for_device(play_at)`：
  - 下一条的目标时刻比 `play_at` 晚 25 ms 以上，说明设备早了：发一块填充静音。它让设备
    退后 40 ms，并且不会进手机流。
  - 早 25 ms 以上，说明设备晚了：跳过条目。
  - 否则发这一条，频道队列一直出队到这一块。

  容差略大于半块，所以设备会稳定在 ±20 ms 以内，不会来回震荡。主机测试在四种相位下验证过
  （`test_a_device_at_any_phase_settles_without_oscillating`）。
- **画面不用新逻辑。** `LiveChannel.pop_video` 本来就把每帧和队首的那块声音配对；时间表让
  队首保持内容顺序，画面自然跟上。设备被补静音期间不发画面，设备上显示提示
  （“声音交给手机…”），做法和现在的 `reconnect.hold` 一样。
- **为什么延迟放在服务器。** 设备的缓冲（见 `main/av_player.c`）：
  - `PCM_QUEUE` = 24 块 = 960 ms，20 块（800 ms）处开始流控；
  - 画面环形缓冲 `AV_VIDEO_RING_BYTES` = 16 KB、32 项，也就是两三帧；
  - 画面比声音时钟晚 100 ms 以上就丢掉。

  设备存不下几秒。媒体服务的 `LiveChannel` 队列有 `PCM_QUEUE_CHUNKS` = 400 块（16 秒），
  画面 `TV_VIDEO_QUEUE_S` = 16 秒。
- **用固定的 H，而不是逐台手机测出一个 D。** H 是每台设备的一个设置（默认 5 秒），不和
  每台手机协商。手机自己的延迟和滑条都由手机跳转来消化。好处：
  - 有人拖滑条时设备不动；
  - 几台手机可以同时听一台设备；
  - 设备的时间表不依赖一台可能已经锁屏、不出声的手机。

  H 必须大于手机“段出现 → 听到声音”的延迟。在 macOS 的 AVPlayer 上实测（§7），回环下
  H = 3 秒也不卡；取 5 秒，给手机网络和蓝牙留约 2 秒余量。网页会显示离直播末端多远，
  可以根据实际使用再调 H。
- **余量（cushion）。** `ChannelTap` 等队列里在它前面攒够一段余量才开始取：
  - 直播 HLS 源一阵一阵来，一次好几秒，要约 3 秒余量；
  - 中转源（B 站、YouTube、投屏）由 `-re` 按实时速度来，1 秒就够。

  取空了就发静音，设备之后也放同一段静音，两边仍然对齐。空了 0.5 秒以上就重新攒余量。
- **有人在听，流就不停。** 设备两次会话之间（换台、重连），时间表照样按实时追加静音，
  手机的播放器不会卡住。锁屏的手机不需要网页醒来也能保持对齐。

### 各种情况会怎样

| 情况 | 手机 | 设备 |
| --- | --- | --- |
| 节目中途开始听 | 从最初几段开始放（先 3 秒预填静音，然后是节目） | 静音。用填充把它往后挪的时候，画面停约 H + 余量 − 当前队列深度（一般 3–5 秒），之后比手机晚 H 继续 |
| 换台（手机或设备按键） | 已发布的旧台放完，然后静音，等预缓冲 + 余量后是新台（约 5–7 秒） | 立刻离开；“连接中”比现在多停 H 秒（总共约 9–10 秒） |
| 暂停（中转台） | 取声音立刻停，改发静音 | 先把手机已经听过的 H 秒放完，走到时间表里的暂停标记时再显示暂停条（标记条目，第 2 阶段） |
| 继续 | 中转重开源、攒够余量后节目接着放 | 晚 H 秒，内容相同 |
| 快进/快退、下一个、下一集（中转重开源） | 静音，然后是新内容 | `reconnect.hold` 会关掉旧的 `LiveChannel`，所以切断前最多 H 秒的画面不显示（反正用户要跳过）；之后比手机晚 H 放新内容 |
| 源自己断了（少见） | 听到断点，然后静音 | 同上：最多丢 H 秒画面。可以留着已关的频道直到时间表用完，暂时不值得做 |
| 手机暂停或关掉 / 锁屏后暂停 | Safari 不再取播放列表 | 20 秒没取播放列表就算不听了：设备取消静音，时间表丢弃。设备多出的 H 秒延迟保持到下次会话 |
| 网页上点“停止” | `POST …/bye` | 同上，立即生效 |

中转台和直播台的区别有三点：
- 中转台来得平稳，余量可以小；
- 中转台能暂停、跳转、换下一个（见上表）；
- 中转估算进度时假设中转输出到设备屏幕固定延迟 `PLAYBACK_LAG_S = 4.0` 秒，收听期间要
  再加 H（见 §6）。

## 3. 对齐方法与预计数字

- **手机与服务器对时。** 走同一个 HTTPS 源，类似 NTP：连发 8 次 `GET /tv/listen/time`，
  取往返最短的一次，偏移 = 服务器时间 − 收发中点。误差最多是这次往返不对称的一半，手机网络
  下一般 ±10–25 ms。每分钟重做一次，每次网页回到前台（`visibilitychange`）时也重做。
- **手机正放到哪儿。** `audio.getStartDate().getTime() + currentTime·1000` 就是正在渲染的
  采样的 PDT。备用办法：每段 ID3 里的 `TXXX pdt` 会作为 metadata cue 出现。手机实际听到的
  位置还要再减去蓝牙延迟。
- **误差和纠正。** `误差 = PDT − 蓝牙 − 服务器现在 + 用户偏移`。正数表示手机早了，往回跳。
  网页在 |误差| > 60 ms 时跳转，最多每 4 秒一次。跳转后要先缓冲新位置，这段时间不出声；
  AVPlayer 上稳定损失 0.35 秒，所以网页从残差里学到这个代价，下次跳转时补上（§7：第二次跳转后残差 −3 到 −28 ms）。
- **设备。** 服务器只能把设备的播放时刻估到设备队列深度那么准：
  - 发送端最多比到期时刻早 `AUDIO_LEAD_MS` 320 + `AUDIO_MAX_LOOKAHEAD_MS` 280 ms 放出声音；
  - 设备按自己的 I2S 速率连续播放，每次欠载都往后滑一点。

  所以估计值 δ 在大约 −230 ms 到 +200 ms 之间游走。领先量改成 320 ms 后欠载已经很少，
  用常数估计大多数时候够用，但不可靠。收听期间每 2 秒在 `STATUS` 包里带一个 `"pos"`
  （设备的 `estimated_pts()`），服务器就能把 δ 测到约 ±20–30 ms（Wi-Fi 上行的延迟），
  再由时间表的填充/跳过把设备稳在那里。
- **锁屏时。** 网页的纠正不运行。手机音频时钟相对服务器时钟按晶振精度漂移，±20–50 ppm，
  即每小时 0.07–0.18 秒。设备跟着服务器时钟走。下次打开网页时纠正。

网页在前台时的误差预算：

| 项 | 大小 |
| --- | --- |
| 手机与服务器对时 | ±10–25 ms |
| 读 PDT | 约 1 ms（AVPlayer：稳定到毫秒） |
| 学习后的跳转落点 | 实测 −3 到 −28 ms（macOS AVPlayer） |
| 蓝牙延迟 | ±30–60 ms（outputLatency 或预设）；稳定的那部分用滑条消掉 |
| 设备时间表的颗粒 | ±20 ms（每块 40 ms） |
| 设备播放时刻 δ | 有 `pos` 上报 ±20–30 ms；没有 ±200 ms |
| **合计** | **一般约 ±60–80 ms；没有 `pos` 约 ±250 ms** |

作为参照，ITU-R BT.1359 给出的唇音不同步可察觉阈值是：声音早 45 ms、声音晚 125 ms
（可接受阈值 90/185 ms）。声音晚一点更不容易被察觉，所以默认偏移可以让手机晚 20–40 ms。

**达不到延迟时**（手机离直播末端不到一段左右，或者反复卡顿）：网页最多跳到可跳范围末端前
1.5 秒，并记一次“末端不够”。办法是把这台设备的 H 调大，而不是让画面变慢。退而求其次就是
接受固定的差值。

## 4. 从哪里取声音；鉴权；代价

- **取声音。** 在媒体服务里，从设备发送端出队的同一个 `LiveChannel` 队列往前看，不出队
  （`ChannelTap`）。队列里存的是线上格式（IMA ADPCM，每 40 ms 324 字节）。原型用 Python
  解码：实测每块 0.17 ms，占一个核的 0.4%（Apple 芯片），而且手机听到的和设备本该放的
  完全一样。更好的做法：有人在听时，在 `LiveChannel._process_audio` 里把编码前的 PCM 存进
  一个并行队列（每块 1280 字节，400 块约 0.5 MB），省掉解码，也没有 ADPCM 的底噪。
- **编码。** 每台被收听的设备开一个常驻的 `ffmpeg … -c:a aac -b:a 32k -f adts`。ffmpeg
  自带的 AAC 编码器所有版本都有，包括部署用的 btbn 8.1。实测：
  - 从送进去到出帧多 118–141 ms；
  - Apple 芯片上每 31 秒用 0.15–0.18 CPU 秒（约 0.5%），云主机的 vCPU 上估计 1–3%（未实测）；
  - 时间表、取声音、HTTP 这些 Python 部分不到 1%。
- **每个收听者的带宽。** 音频段 4.1–4.25 kB/s，加上 12 段的播放列表（约 0.84 kB）大约每秒
  取一次，合计约 5 kB/s ≈ 41 kbit/s ≈ 3 Mbit/s 的 1.4%。作为对比，一台设备的画面流是
  110–200 kB/s。nginx 对 `.m3u8` 开 gzip 可以再减掉播放列表那部分。
- **内存。** 多出来的是 H + 余量那么长的节目，存在现有的 `LiveChannel` 队列里：
  - 声音 8.1 kB/s；
  - 画面按原始索引帧存（每像素 1 字节）。240×180 每帧 43.2 kB，星智 15 fps × 8 秒 ≈ 5.2 MB；
    护照 160×120 更小。

  一阵一阵来的直播源可能把队列撑过 16 秒，所以收听期间把 `TV_PCM_QUEUE_CHUNKS` /
  `TV_VIDEO_QUEUE_S` 调到约 24 秒（星智最坏约 15 MB，`MemoryMax=600M`）。段窗口是
  12 段 × 约 4 kB。
- **谁能听。** 中转已经认识登录过的手机（cookie `tvs`，加到主屏幕的应用用 `?s=`）。由它
  签发令牌 `listen.issue_token(secret, "d0")`（`<到期时间>.<截短的 HMAC-SHA256>`，12 小时），
  媒体服务用同一个密钥校验。令牌放在网址里，因为不能保证 AVFoundation 取播放列表和段时会带
  cookie，加到主屏幕、不和 Safari 共用 cookie 的网页应用更没保证。播放列表里每个段的地址都
  带上令牌。nginx 对这些路径 `access_log off`，和 `/tv/` 其余部分一样。密钥是一个新文件，
  `tvserver` 组可读，例如 `/etc/ai-passport-tv/listen-key`。

## 5. 设备端

- **静音但不丢音量。**
  - 新增 `AV_SWITCH {"mute": 1|0}`，HELLO 里加 `"mute": 1` 表示支持；
  - 收听期间 CONFIG 里也带 `"mute": 1`，新会话一开始就是静音，不会冒一声；
  - 设备只记在内存里（不写 `av_store`），`audio_task` 保持编解码器静音
    （`bsp_audio_set_mute(true)`），不在第一块声音时取消静音；
  - `s.volume` 和 NVS 都不动，手机上的音量滑条照样显示设备的值；
  - 重启、或下一次会话不带这个标志，就取消静音，所以服务器崩溃也不会让设备一直哑着；
  - 状态页可以显示一个耳机标记。
- **播放位置。** 在 `STATUS` 里加 `"pos": estimated_pts()`（线上时间轴上的毫秒数），为收听
  而静音期间每 2 秒发一次。只多一个字段，不加新包。
- **旧固件。** 只有 HELLO 带 `mute` 的设备才提供收听。考虑过“把音量调成 0、以后再恢复”的
  退路，没有采用：设备会把音量存进 NVS、报给手机，恢复一旦丢了，设备就一直是 0。
- **补静音期间设备看到什么。** 时间戳连续的静音块。扬声器是静音的，所以什么也听不到。画面停在
  最后一帧，服务器每秒发一次提示画面，这样就不会触发 1.5 秒的“画面停了”判定、3 秒的
  `AUDIO_SILENCE_MAX_MS` 和 3 秒的 `AV_READ_DEADLINE_MS`，做法和 `reconnect.hold` 一样。

## 6. 怎样接进现有代码（本次没有做）

`server/relay/web.py` 和 `server/relay/assets/remote.html` 等选集功能那边的改动合进来之后再改。

- **`server/tv_server.py`（`AVServer`）。**
  - 新环境变量：`TV_LISTEN_PORT`（星智 8198，护照 8199）、`TV_LISTEN_DEVICE`（`d0`/`d1`，即
    中转里的设备编号）、`TV_LISTEN_KEY_FILE`。
  - 设了就起一个 `ThreadingHTTPServer(("127.0.0.1", port), listen.make_handler(...))`。
  - `streams(device, start)` 在第一次取播放列表时建 `self.listen = ListenStream(self.ffmpeg, lead_s)`
    并返回它。
  - 一个每 20 ms 跑一次的线程调用 `self.listen.pump(tap)`，其中 `tap` 是 `ChannelTap`，
    `self.live_channel` 一换就重新绑定；`idle()` 时关掉流。
  - `_pace_live` 的两处发声音的循环里，收听期间把 `channel.pop_audio()` 换成时间表的决定：
    - `play_at = origin + audio_pts/1000 + δ`；
    - 填充：发 `bytes(AUDIO_BYTES)`，`stamp_base += AUDIO_CHUNK_MS`，不出队、不发画面；
    - 条目：出队到该条目的块为止。
  - 开始和结束收听时发 `{"mute": 1}` / `{"mute": 0}`，CONFIG 里带 `"mute": 1`。
  - 用 `STATUS.pos` 更新 δ（取最近 5 次的中位数）。
  - 在 `now-playing.json` 里写 `"listening": true`。
  - 时间表的键改成 `(频道代数, content_ms)`，因为换掉的频道内容时间会从 0 重新开始。
- **`server/reconnect.py`。** 收听期间，暂停分支要等设备走到时间表里的暂停标记才进入
  （第 2 阶段）。补静音时的提示画面复用 `_send_notice`。
- **`server/relay/web.py`。**
  - 新增 `POST /api/listen` → `{"ok": true, "url": "/tv/listen/<id>/live.m3u8?t=<令牌>"}`，
    针对 `self.target()`；设备不在线、或 HELLO 里没有 `mute` 时拒绝。
  - 新增 `--listen-key-file`。
  - `now_playing` 显示 `listening` 时，估算进度用的 `PLAYBACK_LAG_S` 加上 H。
- **`remote.html`。**
  - 设备面板里加“用手机听”开关，配偏移滑条和蓝牙预设/自动测量按钮，设置按设备存进
    `localStorage`。
  - 点开时：设 `audioSession.type = "playback"`，POST `/api/listen`，设 `audio.src`，
    `play()`，再设 MediaSession 元数据（频道名）。
  - 对齐循环、对时、跳转学习、`visibilitychange` 处理直接搬
    `tools/listen_proto/index.html`。只把手机当耳机用时，网页不能替设备换台。
- **nginx（`packaging/nginx/tv-relay.conf`）。** 前缀更长的会优先于 `/tv/`：

  ```nginx
  location ^~ /tv/listen/d0/ { proxy_pass http://127.0.0.1:8198/listen/d0/; proxy_buffering off; access_log off; }
  location ^~ /tv/listen/d1/ { proxy_pass http://127.0.0.1:8199/listen/d1/; proxy_buffering off; access_log off; }
  location = /tv/listen/time { proxy_pass http://127.0.0.1:8198/listen/time; access_log off; }
  ```

- **systemd。** 在 `tv-xingzhi.service` 和护照的服务文件里加上述环境变量，在
  `tv-relay.service` 里加 `--listen-key-file`。
- **固件。** 见 §5。

## 7. 原型

分支里有：

- `server/listen.py`：
  - `Ledger`；
  - ADTS 切帧；
  - ID3 标签（PRIV 传输流时间戳 + TXXX pdt）；
  - `HlsWindow`（段，以及带 PDT 和 `EXT-X-START` 的播放列表）；
  - `AacEncoder`（ffmpeg）、`ChannelTap`、`ListenStream`；
  - 令牌和 HTTP 处理（`/listen/time`、`/listen/<设备>/live.m3u8`、`/listen/<设备>/<n>.aac`、
    `POST /listen/<设备>/bye`）。
- `tests/test_listen.py`：18 个主机测试。编码器那个测试在 `PATH` 里有 `ffmpeg`、或
  `TV_LISTEN_FFMPEG` 指向一个 ffmpeg 时才跑，否则跳过。
- `tools/listen_proto/serve.py`：合成节目加模拟设备，见下文“在 iPhone 上试”。
  `--bursty` 模拟直播 HLS 源；`--measure N` 无界面运行，打印下面这些数字。
- `tools/listen_proto/index.html`：收听页（中文界面）。有开始/停止、对时、误差显示、自动对齐、
  偏移滑条、蓝牙预设与自动测量、Media Session、回到前台重新对齐，还有一个在设备“嘀”的
  时刻闪一下的方块。
- `tools/listen_proto/avplayer_probe.swift`：用 macOS 的 AVPlayer（Safari 的 HLS 引擎）静音
  播放这个流，打印 PDT 误差和离末端的距离，并像网页一样跳转对齐。

模拟设备是一个按时间表发送的发送端，会记下自己“播”每一声嘀的时刻。合成节目每秒嘀一声，
每 5 秒一声高音。

在开发用的 Mac 上实测（回环，ffmpeg 4.4 自带 AAC，macOS AVPlayer）：

| 测量项 | 结果 |
| --- | --- |
| 音频段数据量 | 4.10–4.25 kB/s；播放列表 841 字节 |
| 编码器额外延迟（PCM 进 → 帧出） | 中位 118–121 ms，最大 141 ms |
| ffmpeg CPU | 每 31 秒 0.15–0.18 秒（约 0.5%） |
| Python CPU（时间表、取声音、HTTP，含模拟本身） | 0.7–0.8% |
| 取声音时 ADPCM → PCM 解码 | 每 40 ms 块 0.17 ms |
| 模拟设备相对时间表 | 稳定在 25 ms 容差内（一次运行：恒为 8.2 ms） |
| 一阵一阵来的源、3 秒余量 | 攒够余量后没再插入静音 |
| AVPlayer 能否播 16 kHz packed audio | 能；`play()` 后 0.5 秒内出声；`currentDate()` 跟着 PDT 走 |
| AVPlayer 自然起播位置（有 `EXT-X-START`） | 手机偏差 −0.66 到 +0.51 秒（四次运行） |
| 第一次跳转 | 晚 0.35 秒落地（缓冲耗掉的时间） |
| 第二次跳转（补上学到的代价） | 残差 −3 ms（H = 6 秒）、−5 ms（H = 5 和 4）、−28 ms（H = 3）；之后 13–20 秒稳定 |
| H = 3、4、5、6 秒时卡顿 | 回环下播放中没有（有一次在出声前等了一下） |

在 Chromium（桌面应用的浏览器面板）里流能放，但没有 `getStartDate`，也没有 metadata cue，
网页无法对齐；对齐需要 Safari。

**没有测的：** iPhone（Safari 和主屏幕应用）、锁屏、蓝牙、手机网络、云主机上的 CPU、
真实的媒体服务和设备。

**在 iPhone 上试：** 在同一 Wi-Fi 下的 Mac 上运行
`python3 tools/listen_proto/serve.py --ffmpeg <ffmpeg> --lan`，用 Safari 打开打印出的地址，
点“开始在手机上听”。对着闪烁的方块听嘀声；锁屏、再解锁，看“手机声音比电视画面”是否回到
接近零。

## 决定（2026-10-12）

用户采纳了建议：

1. 先按 H = 5 秒做；第 0 阶段试完，数字允许就缩短。
2. 两块板都升级固件，加 `mute` 和 `pos`。
3. 锁屏上按暂停：中转台像遥控页的暂停键一样暂停节目；直播台只是停止收听。
4. 主屏幕应用和 Safari 都支持；iOS 18.4 及以上自动读蓝牙延迟，更早的用预设和滑条。
5. 允许几台手机同时听一台设备。
6. 以这个 fork 的 `server/` 为准。

## 8. 建议、计划、工作量、风险

**建议：** 按上面的方案做 HLS 路径，设备在服务器上固定晚 H。Web Audio 加 `audioSession`
"playback" 留作以后的低延迟模式（H 约 1 秒）试验，因为 WebKit 源码显示它现在能在锁屏后继续。
但它有两点没有确证：后台时 JS 和网络连接是否还在跑，以及在主屏幕应用里的表现（有未修的
缺陷）。它可以复用同一份时间表和设备端。

| 阶段 | 内容 | 工作量 |
| --- | --- | --- |
| 0 | 用用户的 iPhone 试原型（Safari 和主屏幕应用、AirPods、锁屏），记下数字 | 半天 |
| 1 | 媒体服务接入（§6）、中转 `/api/listen` 和令牌、网页开关和对齐循环、nginx/systemd、两块板的固件 `mute` + `pos`、主机测试、上设备测试 | 4–6 天 |
| 2 | 时间表里的暂停标记、中转进度加 H、用 PCM 取声音代替 ADPCM 解码、设备上的耳机标记、按实际数据调 H、播放列表 gzip | 2–3 天 |
| 3（可选） | 低延迟模式：Web Audio + `audioSession` "playback" 走 WebSocket，H 约 1 秒，前提是锁屏后站得住 | 2–3 天 |

风险：

- **还没在 iPhone 上跑过。** 待确认的有：iOS 上的 `getStartDate`、手机网络下跳转的代价、
  主屏幕应用的表现。WebKit 缺陷 261858 报告独立模式下 `<audio>` 的锁屏控制没反应；我们
  只有一个直播条目，用不到“下一首”。第 0 阶段就是为此安排的。
- **电视画面更晚。** 收听期间画面比直播再晚约 H（5 秒），设备上换台也多等约 5 秒。
- **源被切断。** 快进、下一个或源故障时，设备上最多丢 H 秒画面。
- **iOS 18.4 以前的蓝牙。** 没有 `outputLatency`，只能靠预设或滑条。
- **锁屏漂移。** 每小时约 0.1 秒，下次打开网页前不纠正；锁屏时卡顿一次，差值会一直留到那时。
- **内存。** 加深的队列每台设备最坏多约 15 MB。

**需要用户回答的问题：**

1. 收听期间电视画面比直播再晚约 5 秒、换台多等约 5 秒，能接受吗？还是第 0 阶段之后争取更小
   的 H？
2. 两台设备都可以升级固件（`mute` 和 `pos`）吗？没有 `pos`，对齐约 ±250 ms，而不是
   ±60–80 ms。
3. 在锁屏上按暂停：只是不听了（约 20 秒后电视恢复自己的声音），还是在中转台上像遥控页的
   暂停按钮一样暂停节目？
4. 主要从主屏幕应用进，还是从 Safari 进？iPhone 是哪个 iOS 版本？18.4 以上可以自动测蓝牙
   延迟。
5. 要不要支持几台手机同时听一台设备？设计上不额外花代价。
6. `server/README.md` 说媒体服务已搬到单独的 `ai-passport-tv-server` 仓库，但部署和最近的
   提交用的都是本仓库的 `server/`。这项工作应该做在哪个仓库？

## 来源

- WebKit 缺陷 261554 与提交 b848143（设了 `audioSession` "playback" 后允许 Web Audio 后台播放）：https://bugs.webkit.org/show_bug.cgi?id=261554 ，https://github.com/WebKit/WebKit/commit/b848143a2ad57c80405dc5b30e61c750f5896d72
- WebKit 源码（main）：`Source/WebCore/Modules/webaudio/AudioContext.cpp`、`platform/audio/ios/MediaSessionManagerIOS.mm`、`html/HTMLMediaElement.cpp`、`platform/audio/cocoa/AudioDestinationCocoa.cpp`、`platform/audio/ios/AudioSessionIOS.mm`、`Modules/audiosession/DOMAudioSession.cpp`、`Modules/mediastream/RTCRtpReceiver.idl` —— https://github.com/WebKit/WebKit
- 主屏幕应用与打断相关缺陷：https://bugs.webkit.org/show_bug.cgi?id=291892 ，https://bugs.webkit.org/show_bug.cgi?id=276016 ，https://bugs.webkit.org/show_bug.cgi?id=261858 ，https://bugs.webkit.org/show_bug.cgi?id=283419 ，https://bugs.webkit.org/show_bug.cgi?id=239602 ，https://bugs.webkit.org/show_bug.cgi?id=236363
- MDN / 兼容数据：https://developer.mozilla.org/en-US/docs/Web/API/AudioSession ，https://developer.mozilla.org/docs/Web/API/AudioContext/outputLatency ，https://github.com/mdn/browser-compat-data
- 输出延迟：https://web.dev/articles/audio-output-latency
- Safari 版本说明：https://webkit.org/blog/13966/webkit-features-in-safari-16-4/ ，https://webkit.org/blog/14735/
- Apple 开发者论坛（后台音频、LL-HLS）：https://developer.apple.com/forums/thread/679274 ，https://developer.apple.com/forums/thread/774239 ，https://developers.apple.com/forums/thread/745254
- AirPods 延迟实测：https://stephencoyle.net/airpods-pro-2 ，https://9to5mac.com/2019/12/22/airpods-latency-test/
- HLS：RFC 8216（EXT-X-PROGRAM-DATE-TIME、EXT-X-START）；Apple《HTTP Live Streaming》packed audio 一节（ID3 PRIV `com.apple.streaming.transportStreamTimestamp`）
- 唇音同步阈值：ITU-R BT.1359-1
