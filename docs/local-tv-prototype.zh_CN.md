[English](local-tv-prototype.md) | 简体中文

# 独立本地音画原型

本可选启动入口通过一条鉴权局域网 TCP 连接，播放直播或预生成的 160x120 baseline YUV420 JPEG 与 16 kHz 单声道 IMA ADPCM 音频（设备端解码为 s16le PCM）。服务器的 `live` 子命令实时转码一个频道，`run` 回放已准备好的素材。频道列表与当前频道始终由服务器决定，白名单地址是未经核实的第三方中继。原菜单、演示和像素主题全部保留。`CONFIG_AV_RAW_PROTOTYPE` 默认关闭；RAW 模式不启动 LVGL 或 BLE。本项目不是公网 URL 代理，也不是生产播放器。TCP/token 配对不提供加密，只应在可信局域网使用。

## 私有配置与构建

仅在本机创建权限 `0600`、已忽略的 `main/av_private_config.h`。定义 `AV_WIFI_SSID`、`AV_WIFI_PASSWORD`、`AV_SERVER_IPV4`（IPv4 字面量）、`AV_SERVER_PORT`（整数，通常 8096）和 `AV_PAIRING_TOKEN`。不要把值粘贴到跟踪文件、日志或命令。缺少私有头仍能编译，RAW 启动会报缺失配置且不联网。应用不擦除 NVS，Wi-Fi 配置存储使用 RAM。

激活 ESP-IDF 5.5.3 后在仓库执行：

```bash
./tools/validate.sh --static
./tools/validate.sh --firmware   # 原demo，公开构建不读取私有头
./tools/validate.sh --prototype  # RAW overlay，公开构建不读取私有头

# 真实本地设备镜像含凭据，不得发布build-av文件。
idf.py -B build-av -D SDKCONFIG=build-av/sdkconfig \
  -D 'SDKCONFIG_DEFAULTS=sdkconfig.defaults;sdkconfig.av-prototype' \
  -D AV_PUBLIC_BUILD=OFF build
idf.py -B build-av merge-bin -o build-av/FoloToy-AI-Passport-full.bin
python3 tools/verify_firmware.py build-av
```

公开门禁显式设置 `AV_PUBLIC_BUILD=ON`，因此不会读开发者私有头。公开 RAW 镜像用于编译/布局检查，启动只报告缺失配置。`--prototype` 输出 `build/FoloToy-AI-Passport-prototype-public.bin`，不覆盖 demo 镜像。私有 `build-av` 保留分段 bootloader、分区表和 app，供授权操作人员使用；上述命令均不刷机。保持 8 MB Flash、3 MB 应用和 `cardid@0x356000` 不变；写入已配置设备前先完成两次一致的全片备份。禁止整片擦除或发布私有合并镜像。用独立默认构建恢复 demo，不要删除原 UI。

## 线协议

固定 24 字节网络序头对应 Python `!4sBBHIIII`：`FAV1`、version 1、type、零 flags、session、全局 sequence、毫秒 PTS、payload length。HELLO（1）头 session/seq/PTS 均零，JSON 为 `{token,version:1}`，并可带 `channel` 指定要播放的频道（省略或填未知 id 时由服务器回退默认）。CONFIG（2）使用非零 session，seq/PTS 零，JSON session 必须匹配。必需参数：width 160、height 120、fps 12、sample_rate 16000、channels 1、sample_bits 16、audio_codec "ima_adpcm"、audio_chunk_ms 40、video_max_bytes 24576。`sample_bits` 指解码后的声音，`audio_codec` 说明线上传的是什么；缺少该字段的 CONFIG（仍发送 PCM 的服务器）会被拒绝，日志里会点名这个字段。可选 `start_delay_ms` 必须为 200；容许服务器附加 timing 字段。

`channels` 表示音频声道数，必须保持为数字。可选频道列表放在独立的 `channel_list` 数组里，每项为 `{id,name}`，设备按上下键短按在列表中前后切换。服务器不发送该键时，设备停留在当前频道。两个键必须分开：把列表当作 `channels` 发送会让设备判定 CONFIG 非法，每个会话都会立即断开。启动目标是收到 CONFIG 后 200ms，但音频复位/预缓冲可能使其延后（见估计时钟）。

AUDIO（3）严格为每 40ms 324 字节：一个 640 采样（16 kHz 单声道）的 IMA ADPCM 块，同样的声音用 PCM 是 1280 字节。块由四字节头加 320 字节采样组成：

```text
[s16 predictor，大端][u8 step index 0..88][u8 零][320 字节：640 个 4 位编码]
```

头里放的是解码器在**第一个采样之前**的状态，而不是第一个采样本身，所以 640 个采样全部被编码，每个块无需前一个块就能解码。连续编码器因此把“该块开始时自己持有的 predictor 和 index”写进每个块的头。两个编码共用一个字节，**较早的采样在低半字节**。每个编码就是标准 IMA：第 3 位为符号，第 0-2 位为幅度；步长表 89 项，从 7 到 32767；index 按幅度变化 -1、-1、-1、-1、2、4、6、8；`diff = step>>3`，幅度第 2、1、0 位分别再加 `step`、`step>>1`、`step>>2`；predictor 与 index 都要截断到范围内。step index 超过 88 或第四个头字节非零都会终止会话。由于半字节顺序和头的内容都与 WAV 的 IMA 块不同（WAV 把第 0 个采样放在头里，324 字节装 641 个采样），现成的 `adpcm_ima_wav` 流不是这个格式；`audioop.lin2adpcm` 先放高半字节，拿它当参考时要把每个字节的两个半字节对调。

VIDEO（4）为完整 baseline YUV420 JPEG，1..24576 字节，实际尺寸/采样由 ROM 检查。END（5）为空载荷。ERROR（6）及其他控制 JSON 上限 1024 字节。错误 magic、版本、类型、flags、大小、会话、序号、配置、JPEG 或截断 TCP 都终止会话；不搜索 magic 恢复。递归 JSON 解析之前，先通过识别字符串/转义的线性扫描将嵌套深度限制为四。CONFIG 前收到 ERROR 视为终止握手失败，不当作媒体。远端 JSON 不写日志。

序号全局递增，但各媒体 PTS 独立检查：音频从零开始每次 +40，视频严格递增且可跳帧。视频 PTS 可以低于紧前音频 PTS。素材循环必须保持时间戳/序号连续或新建会话；`duration_ms` 表示服务器素材长度，不是固件固定会话超时。

### 语音（按住说话），类型 10-12

带 `CONFIG_AV_VOICE` 编译的固件在 HELLO 里加 `"voice":1`。语音只有在 CONFIG 带 `"voice":{"max_s":N}`（N >= 1；设备把一句话限制在 min(N, 10) 秒）时才打开；不带它的 CONFIG 会把语音关掉，按键就和原来完全一样。数值以 `main/av_protocol.h` 为准（`AV_VOICE_CTL`、`AV_VOICE`、`AV_VOICE_RESULT`、`AV_VOICE_RESULT_MAX`、`AV_VOICE_BLOCKS_MAX`）；`server/protocol.py` 与之对应，有主机测试比对两边。

| 类型 | 方向 | 包头 | 载荷 |
| --- | --- | --- | --- |
| VOICE_CTL（10） | 设备到服务器 | session 为本会话，seq 0，PTS 0（同 STATUS） | JSON：`{"op":"begin","id":n,"rate":16000,"codec":"adpcm"}`、`{"op":"end","id":n}`、`{"op":"cancel","id":n}`、`{"op":"pick","id":n,"ref":"..."}` |
| VOICE（11） | 设备到服务器 | seq 0，PTS = 该块从这句话第一块数起的序号 x 40 | 恰好 324 字节：一个与上文 AUDIO 布局相同的 IMA ADPCM 块，640 个 16 kHz 麦克风采样；每句最多 250 块 |
| VOICE_RESULT（12） | 服务器到设备 | 本会话的下一个 seq，PTS 0，flags 0（同 SWITCH） | JSON，1..2048 字节 |

- `id` 每次开机从 1 开始数。按住确定键满 `CONFIG_AV_VOICE_HOLD_MS`（450 ms）才发 "begin"，但录音从按下那一刻就开始，所以 "begin" 之后最先发出的几块是它之前录下的预录（第 0 块就是按下的时刻）。编码器在整句话上连续运行，每块自带状态，所以上行环形缓冲满时丢掉的块（日志 `dropped=`，PTS 随之跳过）不影响下一块。
- "end" 跟在最后一块后面，大约在松开确定键 120 ms 之后。"cancel"（听写或识别时按 ▲，或新的一次按下放弃了没说完的一句）丢掉还没发的内容；还没离开设备的 "begin" 直接作废。"pick" 回应带 `ref` 的候选；服务器用同一个 `id` 的结果回答。
- 只有接收任务写套接字。它在自己的两次读之间发送这些包：每个包开头、等音频队列腾位时，以及有一句话在途时，在读包头之前按 20 ms 分片等待，所以安静的流（暂停、只听声音）也不会卡住上行。写失败或只写了一半都结束会话，和其他包一样。
- 结果读进控制缓冲区，解析成固定结构（不记录识别文字），交给会话主任务。`id` 不是正在等的那句话（已取消、或有更新的一句）的结果直接丢弃。
- 用到的字段：`state`（`final`、`error`；`partial` 会在识别中更新听到的字，第一阶段服务器不发）、`heard`、`reply`、`intent`（`none` 表示没听懂，播低音提示）、`label` 与 `lead`（单个匹配时显示的那一行）、`do`（取 `channel`、`volume`、`brightness`（亮度档位下标）、`audio_only`、`pet`、`page`（`status`、`close`；设备也接受 `list`、`brightness`、`pets`）或 `setup` 之一），以及 `choices`（最多 6 个 `{label, lead, do | ref}`）。明确的单个匹配立即执行，走与 AV_SWITCH 相同的路径；既无 `do` 也无 `choices` 时显示 `reply`（中转服务已经做了），除非 `intent` 为 `none`。只有候选是频道时，才在后面加一行“都不是，打开频道表”。

### 剧集信息（"info": 1）

会画「正在播 / 正在加载哪一集」的固件在 HELLO 里加 `"info":1`。下面这些只发给这样的设备；旧固件收到的和以前完全一样。

- CONFIG 对中转的台（剧集、UP 主、投屏）可以带 `"playing":{"title":"...","episode":n,"total":N}`：这次会话打开的视频。`episode`、`total` 只有剧集才有（从 1 数；知道总集数时才有 `total`）。标题是 UTF-8，服务器按字截到 60 字节以内；设备最多存 63 字节，自己也按字截。打开频道的画面把标题画在原来的分组位置，底下是「正在打开 第 n/N 集」；台标横幅把「分组 · n/总数」换成「第 n/N 集 · 标题」。没有这个字段（直播台、旧服务器）就是不知道。
- `AV_SWITCH {"loading":{"title","episode","total"}}`：会话里片源马上要换（下一个、上一集、手机或语音选集、一集播完）。设备在画面上 y=84..155 画一条横条——「下一个」图标、「正在加载 第 n/N 集」、标题；画面停了 1.5 秒后换成频道等待画面，字样是「正在加载」而不是「正在打开」。横条在时不画「下一个」提示框（`{"next":1}`）。
- `AV_SWITCH {"loading":0}`：新片源好了，接下来就是它的画面。正在加载的变成正在播的，台标横幅报出它，横条不再画在画面上（新片源从整帧开始），等待画面保留到下一帧画出来。
- 不会卡住：最后一个 `loading` 之后 30 秒没等到 `0`、`0` 之后 3 秒没有画面（只听声音时本来就没有）、以及每次会话开始，都会收掉。两种消息都是 SWITCH 包：按深度 2 解析，没有时间轴，占本会话的下一个 seq。

剧集是中转知道的。它的播放流端口回答 `GET /info/<台>`（多用户时 `/info/<设备>/<台>`）：`{"channel","title","episode","total"}`，即 `/ch/<台>` 这一路流正在放的、或者现在打开会选的视频（只看不改，也不访问网站）；别的一律 404。电视服务从频道的播放流地址推出这个地址，会话开始时问一次（至多 0.5 秒，在 CONFIG 之前），之后每次重开片源都在后台再问。为了马上有反应，中转在「下一个」「上一集」或选集掐断流之前先写一条 `loading` 事件（台、`value` 为集数或 0、`total`、`text` 为标题）；电视服务一秒内转给设备，顺手丢掉同一批里的 `next` 提示，重开的片源预缓冲好时发 `{"loading":0}`。

## 素材转换与兼容性

`prepare` 和 `import-video` 均生成实际160x120 baseline YUV420 JPEG，manifest 与 CONFIG 尺寸一致。拒绝旧320x240素材/配置，应在新目录重新生成，不覆盖旧素材。导入按源显示比例（含像素宽高比）适配16:12（4:3），保比例居中黑边，不裁切、不拉伸，YUV420偶数取整可能造成最多两像素黑边不对称。PCM、12fps时间戳、大小上限及私有网络配置不变。参见[服务器说明](../server/README.zh_CN.md)。

## 所有权、内存和生命周期

- 接收任务独占套接字，非阻塞连接/读写配合有界轮询和截止时间；完整 ADPCM 块/JPEG 才入队，消费者不读 TCP。
- 音频任务仅复用 BSP I2S/codec，单次写上限 100ms，部分提交视为失败。它在写入前把每个排队的 ADPCM 块解码成 1280 字节 PCM；队列空时送入全零 PCM。队列共 24 块（960ms），每块 324 字节，合计 7776 字节，同样的音频按 PCM 存是 30720 字节（队列深度以 `main/av_player.c` 的 `PCM_QUEUE` 为准）。启动至少预缓冲五块。队列满、欠载或提交间隔过长都会重建连接及同步起点。
- 视频任务独占 RAW 屏幕，不注册 LVGL 回调。两块 24 KiB JPEG、两块 320x16x2 内部 DMA 条带（20480 字节）、4096 字节解码工作区。JPEG 池满时完整丢弃来帧，不让解码阻塞音频。仍使用 C3 ROM tjpgd 输出 RGB888（不是 LEO 的 LVGL BGR 解码器），scale=0 解码 160x120，再 x2 最近邻转换为 320x240 大端 RGB565。每个16源行 MCU 行填两块16目标行条带；最后源 y=112..119 仅填目标 y=224..239 的一块条带。packer 按源矩形行跨度裁出各条带。两块填完再依次提交 DMA 并等待完成后复用，条带 RAM 仍为20480字节，不声称解码/DMA重叠或播放更快。这是 LEO 风格的小尺寸输入，不是20fps复刻。
- 栈预算：接收 5120 字节，音频 5120 字节，视频 4096 字节；另计队列、驱动 DMA、Wi-Fi 与 BSP。overlay 使用 160 MHz 和有界 Wi-Fi RX/TX 池。内存依据运行时 free/minimum/largest heap，不把构建体积当堆测量。分配失败后停止测试。
- 按钮投递八项队列（按键加手势），不阻塞。OK 切换停止/重启；UP/DOWN 短按按服务器下发的频道列表前后换台，实现上是以新 id 重连；UP/DOWN 长按每次调整音量 5%，范围 10..100%，初始 55%。启动显示 RAW 红绿蓝色条；停止保留最后画面。
- 停止/错误设置取消，接收生产者关闭套接字，消费者完成有界调用并发最终资源访问结束信号，随后才释放会话队列/缓冲。不强删任务。LCD DMA 超时刻意保留缓冲等待完成，不制造 use-after-free；连续十次 200ms 等待失败会明确记录故障并重启，不先释放 DMA 缓冲。控制、按钮、Wi-Fi 事件循环和 BSP 归整个启动生命周期，会话内存每次释放；不支持运行时返回 LVGL。
- 语音（`main/av_voice.c`、`main/av_voice_policy.c`）：确定键按下之前什么都不占。按下时分配约 9.9 KB 的一块（24 块、每块 328 字节的上行环形缓冲，1280 字节的块累加区，640 字节的读取块和一个备用块）和一个任务（C3 上栈 3072 字节，S3 上 4096，优先级 6）；录音停止且缓冲发完后两者都释放，单击时立即释放。结果显示期间约占 1.1 KB，另有在接收任务里解析时短暂的 cJSON 树（几 KB）。从按下到录音结束，音频任务把电视声音乘零；频道表或语音页盖住画面时，视频任务不解码、直接释放每个条带，面板由它单独重画。
- 接收无进展五秒重建会话，不设总会话固定超时。正常 END 消费完队列后清理。重连间隔一秒，清零状态重新握手。服务器连续 PTS 的素材循环本身不触发重启。

## 时钟与度量：明确是估计

日志使用 `CLOCK_ESTIMATED`。提交样本数仅代表 I2S 驱动接受的字节，**不是 DMA 已完成样本，更不是实际声学输出**。单调时钟起点设为首次写入后假定 90ms（六个 240-frame 描述符 /16kHz），播放位置上限为已提交时长。这个预算不是观测到的延迟。启动/复位先静音写零，再重建起点。codec/扬声器真实延迟、DMA 实际填充、采样时钟漂移、网络延迟均未测量。当前视频等估计 PTS 再解码，显示可能再落后解码/传输耗时；已过期 100ms 的帧丢弃。这是有界延迟 MVP，不是同步验收。调整时钟偏移或解码提前量前，应实测闪光/短音标记。

每十秒记录实际区间完成帧数和时间、丢帧、队列高水位、最长解码、heap/largest block。会话结束补充实得渲染 FPS（含启动/排空）、提交样本、最大提交间隔、最低 heap，并明确 DMA/声学未测量。目标 12fps 不当作已达帧率。应用层不输出 SSID/password/token/远端 JSON，但底层 Wi-Fi 日志可能带网络标识，分享前仍须脱敏。

## 验证与待测

`tests/test_av_protocol.c` 为纯 C，覆盖头大小端/长度/类型边界、畸形头、重组切分边界、序号/会话拒绝、独立媒体 PTS、重连清零、RGB 条带尺寸和字节序。全帧 MCU 测试覆盖 x2 像素复制、源行跨度、拼接边界、保护字节和最后8源行；静态门禁还运行服务器测试和 ffmpeg 可用时的真实生成/导入测试；不模拟 ROM JPEG、LCD DMA、I2S、Wi-Fi、并发或声学时序。

分别报告 Build、Host tests 和 Device tests。硬件验收前需检查颜色/方向、JPEG 实际速度、PCM 单声道采样率和音量、闪光/短音偏移漂移、超过 30 分钟持续播放和服务器素材循环、队列/堆边界、服务器不可达/慢速、坏/截断 JPEG、Wi-Fi 断连、音频欠载、重复 OK 停止重启及停后 DMA 所有权。构建/合并布局验证通过不能代替任何实机结果。
