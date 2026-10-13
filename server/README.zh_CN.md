<p align="right">
  <strong>简体中文</strong> · <a href="README.md">English</a>
</p>

# 本地音画原型服务器

> **这个分支的 `server/` 是正本**（2026-10-12 的决定，见
> [`2026-10-12-multi-tenant-design.zh_CN.md`](../docs/superpowers/specs/2026-10-12-multi-tenant-design.zh_CN.md) 的 Q7）：
> 云主机跑的就是这份代码，服务端的改动都提交在这里。上游的媒体服务在提交 `208357b` 时迁到了
> [ai-passport-tv-server](https://github.com/cjpjxjx/ai-passport-tv-server)，此后这个目录已经和它分开发展
> （视频中转、多设备、多用户）。多用户的运维：[`docs/development/tenancy-phase1-runbook.zh_CN.md`](../docs/development/tenancy-phase1-runbook.zh_CN.md)。


本模块是手动启用、单设备的**局域网直播频道、视频文件与合成素材播放器**，不是公网代理或生产级鉴权服务。
运行服务器只需 Python 3.11+ 标准库；ffmpeg 用于预生成素材，以及 `live` 子命令的实时转码，因此运行直播的主机需要安装它。频道地址是 `server/live.py` 中的固定白名单，来自社区聚合播放列表，可用性与版权状态未经核实，仅用于家庭局域网测试。设备在握手中按名称指定频道，服务器对无法识别的名称回退 `DEFAULT_CHANNEL`。
`numpy` 是**可选**的 pip 依赖：装了它，直播画面放不进字节目标时用感知式取舍（`server/perceptual.py`）；没装，或设置 `TV_PERCEPTUAL=0`，就像之前一样走颜色阶梯。不需要设备访问或全局配置改动。以下命令从仓库根目录运行。

## 导入视频文件

```sh
python3 -m server.tv_server import-video --input /path/to/video.mp4 \
  --media-dir server/.local/my-video --seconds 60 --start 0
```

输入为本地文件，不接收远程网址。可选择1～600秒，按原显示比例（含像素宽高比）适配16:12（4:3），居中留黑边，输出160×120、12FPS画面和16kHz单声道PCM。不裁切或拉伸；YUV420偶数取整可能导致两侧黑边最多相差两像素。没有声音的视频会补静音轨，JPEG单帧上限仍为24KiB。播放时逐包读取磁盘，不把整部视频载入内存。将生成目录交给下文相同的 `run --media-dir` 命令即可。这一步是文件转换后播放，尚不是IPTV实时转码。

旧320x240 JPEG/manifest应在新目录重新生成。设备要求160x120输入，通过x2最近邻放大到320x240；不据此声称设备帧率改善。协议时序与PCM不变。

## 播放直播频道

最省事的用法——启动全部服务，并打印设备上要填的地址：

```sh
./run.sh                 # macOS 与 Linux
run.bat                  # Windows
```

它同时启动媒体服务器和频道配置页，并在频道表保存后自动重启媒体服务器。
需要 Python 3.9 以上和 ffmpeg；缺哪个都会明确告诉你，以及怎么装。

> **Windows 未实测。** `run.bat` 与 `tools/launch.py` 里 Windows 分支的写法
> 来自命令的公开文档，从未在 Windows 机器上运行过——本项目只在 macOS 上开发。
> 请当作未经检验的代码对待。

两个服务也可以分开单独运行：

```sh
python3 -m server.tv_server live --channel ch000      # 媒体服务器，端口 8096
python3 tools/channel_config.py                       # 频道配置页，端口 8097
```

`--bind` 可以省略，而且通常就该省略：服务器会自己找出本机的网络地址，
并同时打印该地址和这台电脑的 `.local` 名字。设备上建议填**名字**，
因为路由器重新分配 IP 之后名字不用改。多网卡机器仍可手动指定 `--bind`。

```sh
python3 -m server.tv_server live --channel ch000 \
  --bind 192.168.1.20 --port 8096 --token-file <仅属主可读的文件>
```

每个设备连接都会启动自己的 ffmpeg 进程和独立时间起点，因此换台就是设备用新的频道名重新连接。转码失败只结束该次会话，服务继续监听。可用频道见 `server/live.py` 的白名单。

频道表只在启动时读取一次。`run.sh` 与 `run.bat` 会监视 `channels.txt`，
文件变化时自动重启服务器；直接运行上面的命令则要自己重启才能生效。

## 一次预生成、复制、再运行

```sh
python3 -m server.tv_server prepare --media-dir server/.local/media
# Optional: --ffmpeg /absolute/path/to/ffmpeg
```

目标目录必须不存在。输出是可移动的 `manifest.json`、`audio.s16le`（320000 字节）
和 `frame-000.jpg` 到 `frame-119.jpg`。将整个目录原样复制到运行主机即可。
`server/.local/` 下生成的素材和本地配置被 Git 忽略。预生成失败时应排查原因，
用新目标目录重试，不能使用生成到一半的素材。`run` 在监听前检查全部 JPEG。

10 秒素材包含三位帧计数（000–119）、运动方块、每个整数秒起点的一帧白色闪光，
以及对应的 50 ms、1 kHz 短音。短音幅度 0.08（-22 dBFS），设备须从低音量开始。
PCM 与视频同从 t=0 起步。RGB 图由标准库生成，无字体包依赖，然后 ffmpeg 编码为
baseline 160×120 JPEG 4:2:0 和 16 kHz 单声道 s16le。
JPEG 超限时逐步降低质量（q=5 后尝试 10、18、25、31），仍超限则预生成失败，
**绝不截断 JPEG**。简单合成图不代表最坏情况 JPEG 解码或吞吐压力。

创建配对文件时，不把值放进命令参数或 shell 日志：

```sh
python3 - <<'PY'
import os
import secrets
from pathlib import Path
folder = Path('server/.local')
folder.mkdir(mode=0o700, parents=True, exist_ok=True)
fd = os.open(folder / 'token', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, 'w') as stream:
    stream.write(secrets.token_hex(32) + '\n')
PY
python3 -m server.tv_server run --media-dir server/.local/media \
  --bind 127.0.0.1 --port 8096 --token-file server/.local/token \
  --duration-seconds 1800
```

通过另外管理的设备私有配置写入相同 token，不打印、不提交。
也可由支持秘密管理的启动器提供 `TV_PAIRING_TOKEN` 环境变量；此时不能同时指定
`--token-file`。token 为 16–128 个可打印 ASCII 字符，应使用随机值而非口令。
文件必须为进程用户拥有的普通文件，权限严格为 0400 或 0600，不能是符号链接。
用非特权账户运行。比对使用 `hmac.compare_digest`，失败时不输出 token 或输入 JSON。

连接设备时，把 `127.0.0.1` 替换为服务器实际获分配的**明确 RFC1918 局域网 IPv4 地址**。
拒绝通配/公网绑定、主机名、IPv6 和公网来源；默认端口 8096。本原型无 TLS：
仅用于可信隔离局域网，使用主机已有防火墙策略限制访问，不要做公网端口转发。
仅过滤 RFC1918 不能阻止被转发的流量。仅允许一个已认证流，推流期间新客户端立即关闭。
首个 HELLO 总接收时限为 250 ms。

## 线协议

每个 TCP 包使用 24 字节网络序 `!4sBBHIIII` 包头：

| 字段 | 值 |
| --- | --- |
| magic、version、flags | `FAV1`、1、0 |
| type | HELLO=1、CONFIG=2、PCM=3、JPEG=4、END=5、ERROR=6、PALETTE=7、SWITCH=8、STATUS=9、VOICE_CTL=10、VOICE=11、VOICE_RESULT=12 |
| session | uint32；初始 HELLO=0，服务端生成新的非零值 |
| seq | uint32；HELLO=0、CONFIG=0，其后服务端发送序号全局递增 |
| pts_ms | uint32，共享素材时间线上的呈现时间戳 |
| payload_length | uint32，读取载荷前校验 |

客户端先发送 HELLO（session=seq=pts=0）：

```json
{"version":1,"token":"<private pairing token>","channel":"cctv1"}
```

`channel` 可选，用于指定要播放的频道；省略或填服务器不认识的名称时使用默认频道。

CONFIG 包含 `width=160`、`height=120`、`fps=12`、`sample_rate=16000`、
`channels=1`、`sample_bits=16`、`audio_chunk_ms=20`、`video_max_bytes=24576`、
`session`，以及补充提示字段 `duration_ms=10000`、`start_delay_ms=200`、
`audio_lead_ms=200`、`video_lead_ms=50`；直播路径还会带上 `channel` 与 `channel_list`。
控制 JSON 不超过 1024 字节。

`channels` 是音频声道数，必须保持为数字；可选频道列表放在独立的 `channel_list`
数组里（每项 `{id,name}`），设备据此换台。两者不可混用：把列表当作 `channels`
发送会让设备判定 CONFIG 非法，每个会话都会立即断开。
每 20 ms 一个 PCM 包，严格 640 字节（320 个小端样本）；JPEG 不超过 24 KiB。
END 无载荷。客户端可以发送匹配会话的 END 或关闭连接停止；HELLO 后不允许上行媒体或其他控制命令。
非法版本、flags、类型、长度、会话或 JSON 都直接断开，不搜索下一个 magic 恢复。
ERROR 类型保留且支持拆包；服务器错误时关闭连接，不冒险在半包发送后插入 ERROR。

### 剧集信息

只对 HELLO 带 `"info": 1` 的固件、只对中转的台：直播路径在 CONFIG 里加 `playing`（`{title, episode, total}`），片源马上要换时发 `SWITCH {"loading": {...}}`，重开的片源预缓冲好时发 `SWITCH {"loading": 0}`（`server/episode_info.py`、`AVServer.info_*`、`reconnect.hold`）。剧集来自中转：它播放流端口上的 `GET /info/<台>`（把播放流地址里的 `/ch/` 换成 `/info/`），CONFIG 之前至多问 0.5 秒，每次重开片源时在后台线程里再问；以及中转在「下一个」「上一集」或选集掐断流之前写的 `loading` 事件。中转不回答就没有 `playing` 和 `loading`，播放不受影响。设备侧见 [`docs/local-tv-prototype.zh_CN.md`](../docs/local-tv-prototype.zh_CN.md) 的「剧集信息」。

只对 HELLO 带 `"progress": 1` 的固件、只对中转的台：直播路径发 `SWITCH {"progress": {"dur", "pos", "at"}}` 锚点（毫秒）——视频多长、本会话某个音频 PTS 时播到哪；设备按自己的声音时钟往前推。`server/progress.py` 管一个会话的锚点：片源开始供数据时（会话开始 `at` 为 0，或 `reconnect.hold` 换上新片源，`at` 为下一个音频 PTS）在后台线程里问中转 `/info/<台>` 的 `duration` 和 `offset`（流还没起来时隔 1 秒再问，最多 3 次）；保持（断流、暂停）开始时把锚点加上 `stop`（最后一块真声音之后的 PTS）重发；原片源自己缓过来时按停顿把锚点顺延。没有总长的视频在设备挂着进度条时发 `{"progress": 0}`。不每秒发，也不逐帧做事。v2 发送端不发。设备侧见 [`docs/local-tv-prototype.zh_CN.md`](../docs/local-tv-prototype.zh_CN.md) 的「播放进度」。

## 语音控制

在设备上按住 OK 说话，松开：设备在电视连接上把这句话传上来，媒体服务交给同一台主机上的
muse-asr（SenseVoice）识别，理解意思后回答。设计和用户的决定见
[`docs/superpowers/specs/2026-10-12-voice-control-design.zh_CN.md`](../docs/superpowers/specs/2026-10-12-voice-control-design.zh_CN.md)。
代码在 `server/voice/`（`session.py` 收包和工作线程，`asr.py` 识别桥，`intent.py`、`match.py`、
`numbers.py`、`pinyin_table.py` 规则，`relay_client.py` 调中转）和 `server/relay/voice.py`。

### 包

| 类型 | 方向 | 载荷 |
| --- | --- | --- |
| `VOICE_CTL = 10` | 设备 → 服务端 | JSON，不超过 `CONTROL_MAX`：`{"op":"begin","id":n,"rate":16000,"codec":"adpcm"}`、`{"op":"end","id":n}`、`{"op":"cancel","id":n}`、`{"op":"pick","id":n,"ref":"…"}` |
| `VOICE = 11` | 设备 → 服务端 | 正好一个 324 字节的 IMA ADPCM 块（与下行音频同一布局，40 ms）；`pts` 是从 begin 起的毫秒数；一句话最多 250 块（10 秒），多的丢弃 |
| `VOICE_RESULT = 12` | 服务端 → 设备 | JSON，不超过 2048 字节（`VOICE_RESULT_MAX`），与其他控制包一样是带 `\u` 转义的 ASCII |

- 两种上行包在 CONFIG 之后的任何阶段都收，凡是收 STATUS 的地方都收（`AVServer._take_upstream()`，
  由 `_device_left()` 和 `_wait_until()` 调用，所以起播等待、断流重连、暂停时也收）。其他上行类型仍然结束会话。
- 收到 `end` 后，工作线程解码 ADPCM、请 muse-asr 识别、做决定，需要时调中转。发送循环从不等它。
- 结果排队，由直播发送循环（`voice_flush()`）和断流重连 / 暂停中的 `reconnect.hold()` 发出，
  像 SWITCH 一样占会话的下一个 `seq`，`pts` 为 0。第一次预缓冲期间先等着。
- 结果回答它带的 `id`。`cancel`、新的 `begin` 和新会话会让旧 `id` 还没出来的结果作废。
- 只有设置了 `TV_ASR_SOCKET`，CONFIG 才带 `"voice":{"max_s":10}`。服务端不需要 HELLO 里的 `"voice":1`。

一个结果：

```json
{"id":7,"state":"final","intent":"channel","heard":"我要看湖南卫视","reply":"换台中…",
 "do":{"channel":"ch017"},"label":"湖南卫视","lead":"25"}
```

- `state`：`final`；或 `error`（识别服务没开 / 超时、音频格式不对、语音没开），只带 `reply`。
  第一期不发 `partial`。
- `do` 里正好一个键：`AV_SWITCH` 已认识的 `channel`、`volume`（0–100）、`brightness`（设备的档位，
  0 最亮）、`audio_only`（0/1），或新的 `page`（`"status"`、`"close"`）、`setup`（1）。
  清楚的单个结果带 `do`，立即执行。
- 有几个都可能时用 `choices`（最多 6 个）代替 `do`：`{"label","lead","do"}`，或者选中后要服务端去做的
  带 `{"label","lead","ref"}`（`ep:<剧>:<集>`，两部剧里选一集，经中转；`ls:<网站>.<种类>.<分组>:<从第几个>`，
  列表的下一页或分组概览里的一组；`s<操作>:<第几次搜索>:<参数>`，搜索结果、它的下一页、追问或 UP 主的动作，见下文），设备再用这个 `ref` 发 `pick`，服务端对同一个 `id` 回一个新结果。
  只认最近一次结果里的 ref。配网总是两个选项（进入配网 / 算了），因为它会停止播放。
- `intent`、`label`、`lead` 是在设计格式之外加的：决定的种类（`none` 表示没听懂，用来放低音提示），
  以及单个结果在面板上显示的那一行（名字和设备列表里的序号）。
- 既没有 `do` 也没有 `choices`：显示 `reply`（中转已经做了：暂停、跳转、下一集；或者做不了，比如「直播台不能暂停」）。

### 能听懂什么（规则，不用大模型）

频道名（去掉「请」「帮我」「我要」「看」「切到」之类）；从名字生成的别名（CCTV-n 说成「中央 n 套」
「央视 n 台」「n 套」，CCTV-5+ 说成「五加」，「央视体育」这类按内容的说法；卫视不带「卫视」也行；
音乐台按歌手名）；无声调拼音模糊匹配同音字（分数取字的最长公共子序列比例和拼音音节编辑距离比例中
较大的，数字必须一致）；「第 N 台」；分组给出候选；同组的上一个 / 下一个台；剧名加「第 N 集」或
「最新一集」，经中转的选集；下一集 / 上一集、暂停、继续、快进快退、从头播交给中转（直播台回答做不了）；
音量大 / 小 / 数值 / 静音 / 取消静音；亮度高 / 低 / 数值；只听声音开 / 关；状态页；配网（先确认）；
取消；帮助。分数 ≥ 0.85 且领先 0.15 时直接执行（一字不差的别名总算），或只有一个 ≥ 0.65 的候选时执行；
否则给出最多 6 个 ≥ 0.5 的候选；再否则「没听懂」。各种说法见 `tests/test_voice_intent.py`。

**问一类台**排在剧集和频道匹配之前，问句不会被按字模糊匹配成某个台。整句话按词表切开（每处取最长的词）：
网站（YouTube、油管、优兔、B站、哔哩哔哩、bilibili）、种类（UP 主 / 博主；剧 / 剧集 / 电视剧）、分组（央视、卫视、
港台、国际、音乐、动画、电影、其他……）、泛指的词（频道、台、节目、可以看的）以及问的词和虚词（哪些、什么、
有没有、列出、显示、告诉我、订阅了……）。每个字都切得出来、并且有问的词，就是在问一类台：「我有哪些youtube频道
告诉我」「B站的UP主有哪些」「央视有哪些台」。有切不出来的词（「有没有湖南卫视」）就还是点台；说了两个不同的
分组（「央视电影」是 CCTV-6）也是。只说类别（「卫视」「YouTube频道」「B站UP主」）也列出来，除非它正好是某个台的
名字或别名（「电视剧」还是 CCTV-8）。回答的 `intent` 是 `list`（只说类别时是 `choices`），回答如「YouTube 频道有
3 个」，各个台作为候选（`do`）；超过 6 个时每页 5 个，最后一行「下一页 · 还有 N 个」（`ls:` ref）。只说网站（「我有哪些YouTube频道」「B站有哪些频道」）
只列那个网站的 UP 主；那个网站的剧集要说「剧」「剧集」（「YouTube有哪些剧集」）。没说类别（「都有什么台」「有什么可以看的」）先列出分组，每行打开那一组。
这一类一个都没有时是 `none`，回答说清楚（「还没有 YouTube 剧集」）；问网站而中转没报网站时，回答中转没回应。
中转目录（`GET /api/voice/catalog`）的每个 UP 主和剧集都带 `site`（`bilibili` / `youtube`）。

**频道列表里的网站标记。** 中转写给设备的频道表里，UP 主（`up..`）和剧集（`sr..`）的台名后面用半角括号带上网站：
「王志安(油管)」「熊出没(B站)」（`channel_groups.mark`、`relay/table.label`；20 格放不下时截名字，不截标记；投屏台
和直播台没有）。设备只拿到名字，网站只能标在这里；半角括号在 JSON 里各占 1 字节，一个标记占 CONFIG 包 9（B站）或
14（油管）字节，加台时的空间检查也算上它。语音目录再把标记拆掉（`channel_groups.unmark`）：按不带标记的名字匹配
（「王志安」「Dulaa Music」），网站优先用中转报的，中转没回应时只看标记也知道，所以「有哪些YouTube频道」照样能答。
语音列台时除非问的就是那个网站，否则也带标记。手机网页列不带标记的名字，另有网站标签（「油管」「B站」，剧集台也有）。

说了剧名但没有这部剧（「葫芦娃第3集」），或「看」「播放」「打开」后面的名字什么都对不上（「我要看葫芦娃」），
回答「没有找到「葫芦娃」」，`intent` 仍是 `none`，设备照样放低音；后一种再加一句「，可以说「搜索葫芦娃」」。

### 跳转

在 UP 主、剧集和投屏台上（直播台照旧回答「直播台不能…」）：从头播（「从头播」「回到开头」「从第一秒开始」
「重新看这集」）；带组合单位的时间（「跳到3分20秒」「跳到一个小时零五分三十秒」「12分半」「半小时」「第90秒」、
识别结果写成「12:30」或「12点30」、「从5分钟开始播」「播到10分钟的地方」「到3分20」）；按比例（「跳到一半」「中间」
「三分之一」「四分之三」「百分之六十」「60%」，「跳到最后」= 结尾前 30 秒）；带组合单位的相对跳（「快进1分30秒」
「后退半分钟」「往回倒20秒」）。比例或时间前面没有「跳到」「到」「从」就不算（单说「一半」不是跳转，「第三集」
还是选集）。这部分用自己的规范化（`intent.seek_text`），因为共用的规范化会去掉「12:30」的冒号和百分号。比例由中转
按正在播的视频自己的时长算（`POST /api/seek` 除了 `delta`、`position` 还认 `fraction`、`from_end`），不会过时；
不知道时长时回答「不知道这个视频有多长，没法按比例跳」，跳了就说跳到哪（「跳到 12 分 22 秒」）。说的时间超过结尾时
（语音的 `position` 都带 `within: true`）不跳，回答「这个视频只有 23 分 40 秒」，而不像手机的进度条那样换到下一个。

### 搜索视频和 UP 主

只能用语音（手机页面没有搜索框）。只有句子以搜索动词开头（「搜」「搜索」「搜一下」「帮我搜」；「找」要带上
「视频」或「UP 主」），或者以「…的视频」结尾（「我想看…的视频」「放一个…的视频」）才算搜索，所以「我想看湖南卫视」
「我要看犬夜叉第三集」「看体育」、问有哪些台、播放和音量命令都不受影响。它排在状态页之后、音量和播放之前，
因为要搜的词里可能有「大声」之类。

- **网站**：说了网站就只搜它：YouTube / 油管 / 优兔是 YouTube（「油管搜lofi」「在YouTube上找lofi的视频」），
  B站 / 哔哩哔哩是 B 站。没说网站时视频搜 B 站，UP 主**两个网站一起搜**（见下）。下面的追问也记得说了哪个网站（或没说）。
- **搜什么**：视频（「搜视频X」「搜索X视频」「找X的视频」「我想看X的视频」）或 UP 主（「搜UP主X」「搜博主X」
  「找X这个UP主」「搜频道X」「搜油管频道X」「YouTube上找X频道」）。没说清时（「搜索李子柒」「搜一下老番茄」
  「帮我搜罗翔」）不猜：回答 `intent` `ask`，「要搜视频，还是搜 UP 主？」和两个选项「搜视频」「搜 UP 主」
  （ref `sq:<n>:v` / `sq:<n>:u`）。它像列表一样留在屏幕上；30 秒内的下一句话也可以回答（「视频」「UP主」
  「博主」「YouTube的视频」「第一个」）。说别的就不再等这个回答。
- **结果**是候选，每页 5 个，再加「下一页 · 还有 N 个」（`sp:<n>:<从第几个>`），`lead` 是从头数的序号
  （`01`…`20`）。视频一行是标题，放得下时接「 · 时长」「 · UP 主」；UP 主一行开头是网站，再是名字和
  「 · 2084万粉」或视频数：「B站 · 老番茄 · 2084万粉」「油管 · Lofi Girl」（YouTube 的频道搜索没有这些数）。
  网站标记总在；后面的只在一行放得下 180 像素时才接，宽度按固件字库每个字的真实宽度算（`intent.pixels`，有测试
  核对 `main/ui_font_data.h`）。视频行不标网站（一次视频搜索只搜一个网站）。设备字库画不出的字（表情）去掉，截到 47 字节。选中视频（`sv:<n>:<i>`）经中转投屏：
  设备切到「我的投屏」，播完回到原来的台，和手机投屏一样。选中 UP 主（`su:<n>:<i>`）再给两个选项「订阅为频道」
  （`sf:`）和「播放最新视频」（`sl:`）。结果还在屏幕上时，「下一页」翻页，「第二个」「播放第三个」直接选
  （行首的序号，或这一页的第几行）；「订阅」「播放最新」回答 UP 主的两个选项。屏幕上没有结果时「第二个」
  仍是第 2 台。只认最近一次搜索的 ref。
- **过滤**：B 站去掉付费（`is_pay`、`badgepay`）、充电专属（`is_charge_video`）、番剧和课程页、直播间、
  没有时长的；没有投稿的 UP 主也去掉。YouTube 去掉直播中、预告、刚结束还在处理的直播、Shorts、已删除或私享的、
  没有时长的。会员专享视频从搜索数据里看不出来，投屏时会和手机投屏一样失败。
- **延迟**：设备等一个结果 8 秒。媒体服务把识别之后 7 秒（`RESULT_BUDGET_S`）里剩下的时间给中转，中转问网站时
  再少等 0.7 秒，网站慢时能及时回答「B站搜索太慢了，稍后再试」「YouTube 搜索太慢了，稍后再试」。B 站搜索是一次
  不带签名的请求（`/x/web-interface/search/type`，`search_type=video` 或 `bili_user`，带 buvid3 cookie，不需要
  wbi 签名），约 1 秒。YouTube 经节点的 `/yt/search`（yt-dlp，一次一个）：节点最多等正在跑的 yt-dlp 3 秒，
  等不到回答 busy；中转放弃后节点仍把这次搜完，再说一遍就快了。中转缓存结果 5 分钟，节点缓存 10 分钟。
- **两个网站一起搜**（没说网站的 UP 主搜索）：媒体服务同时发两个 `GET /api/voice/search`，同一个时限，回来的
  轮流排（B 站第一个、YouTube 第一个、B 站第二个……，最多 20 个），回答「找到 11 个UP 主：B站 8 个，油管 3 个」。
  回答太慢、正忙或者到时没回答的网站，最后一行是「油管还在搜 · 再搜一次」（正忙时「油管正忙」；ref `sr:<n>:yt`），
  这一页因此少列一个结果。选它，或者结果还在屏幕上时说「再搜一次」「重试」「油管」，只再搜那个网站；搜到的并进来，
  换一套 ref 从第一页列。B 站一个都没搜到时回答「B站没搜到，油管还在搜，稍后再说一次」。根本搜不了的网站（YouTube
  没配置）悄悄略过，这一页就和只搜一个网站一样。把整句再说一遍也行：B 站用中转的缓存，YouTube 用节点的。
- **订阅**和手机「添加 UP 主」一样（同样最多 12 个，并且设备的频道列表还放得进 CONFIG），记在设备主人名下；
  这是设备访客唯一能改的账号数据。「播放最新」投屏最新一个投稿（B 站的搜索结果里已经带着）。两样都可能比语音结果
  能等的时间长（列一个 YouTube 频道要 10–20 秒）；这时中转先回答 pending，在后台做完：新台出现在设备的频道列表里，
  投屏照常让设备切过去。

中转接口（总带 `device`，多用户时按那台设备的访客处理）：
`GET /api/voice/search?device=&site=bilibili|youtube&kind=video|up&q=&wait=`、
`POST /api/voice/cast {device, site, id, title, duration}`、
`POST /api/voice/creator {device, site, id, name, action: follow|latest, wait}`。
节点接口是 `GET /yt/search?q=&n=&kind=video|channel`。

调中转时总带 `device`，作用在说话的那台设备上，而不是手机正在遥控的那台（`Relay.target()`）。是哪一台，看中转的模式：

| 中转模式 | 媒体服务发的 `device` | 从哪来 |
| --- | --- | --- |
| 单用户（没有 `--db`；媒体服务不设 `TV_DB`） | 中转里的设备 id（`d0` = `--device-name`，`d1`… = 依次每个 `--extra-device`）或名字（如 `Passport`） | `TV_RELAY_DEVICE`，每个 unit 固定 |
| 多用户（`--db`；媒体服务设了 `TV_DB`） | 媒体服务这次会话认出的设备 id：旧固件或还没接收（adopt）的新固件是 `legacy-xingzhi` / `legacy-passport`，接收或绑定后是 12 位 MAC | HELLO 握手（`_admit()` 后 `VoiceService.bind_device()`）；不看 `TV_RELAY_DEVICE`，日志里说一句 |

多用户时中转密钥就是管理员，只能遥控管理员自己的设备，而一个媒体单元可能服务任何人的设备。所以带密钥
（`X-Key`）、在单设备接口上（`switch`、`seek`、`next`、`previous`、`pause`、`resume`、`device`、`audio`、
`pet`、`series/<id>/play`、`series/<id>/episodes`、`voice/catalog`、`voice/search`、`voice/cast`、
`voice/creator`）点名设备的请求，按那台设备的**访客**处理：只能动那一台，剧集和进度按设备主人的来，
除了用语音给主人订阅 UP 主，碰不到账号数据。这里不认名字（名字是用户起的，不唯一），
不认识或未绑定的 id 回 404。手机（cookie 会话）的规则不变。未绑定的设备 CONFIG 里没有 `voice`，也没有中转
客户端。一句话用它开始时那次会话的中转客户端做完。

### 怎么打开（运维）

1. muse-asr（`muse-gadget-sdk` 仓库的 `esp32/tools/muse_asr`）设置
   `MUSE_ASR_SOCKET=/run/muse-asr/asr.sock`、`MUSE_ASR_SOCKET_GROUP=tvserver`、
   `RuntimeDirectory=muse-asr`、`RuntimeDirectoryMode=0755` 和 `SupplementaryGroups=tvserver`
   （它的服务模板里已经写好）。套接字权限 0660，属主 muse-asr，组 tvserver：这个权限就是唯一的凭据，
   不共享密钥。检查：`sudo -u tvserver curl --unix-socket /run/muse-asr/asr.sock http://x/health`。
2. 每个媒体服务的 unit：

   | 变量 | 值 |
   | --- | --- |
   | `TV_ASR_SOCKET` | `/run/muse-asr/asr.sock`；不设就不开语音，CONFIG 不提供 |
   | `TV_RELAY_KEY_FILE` | 中转的密钥文件（与 `tv-relay.service` 的 `--key-file` 相同） |
   | `TV_RELAY_DEVICE` | 只用于单用户中转：本服务在中转里对应的设备，`d0` / `--device-name` 的名字，或它那个 `--extra-device` 的名字或 id（如 `Passport` 或 `d1`）。设了 `TV_DB` 时不看；但仍留在基础 unit 里，删掉多用户 drop-in 就又用上 |
   | `TV_RELAY_MANAGE_URL` | 可选，默认 `http://127.0.0.1:8100`；只接受本机地址，密钥不会离开这台主机 |

   不设中转变量（`TV_RELAY_KEY_FILE`，单用户时还有 `TV_RELAY_DEVICE`）时，换台、音量、亮度、页面和配网
   照常；选集、暂停、跳转、上一集 / 下一集会回答「这台电视没有连到视频中转」。unit 的用户要在 `tvserver`
   组里（套接字），并能读密钥文件。
3. 中转要用这一版（POST 接口认 `device`，并提供 `GET /api/voice/catalog` 和搜索接口）；搜 YouTube 还要
   这一版的 YouTube 节点（`server/relay/ytnode.py`，提供 `/yt/search`），节点是旧版时 YouTube 搜索回答没回应，
   B 站搜索照常。重启顺序：muse-asr、
   tv-relay，再是媒体服务。多用户模式对语音不加任何设置：`packaging/systemd/tenancy/` 的 drop-in 不碰这些变量
   （[运维手册](../docs/development/tenancy-phase1-runbook.zh_CN.md#两种模式下的语音控制)）。

承诺延迟之前先在主机上量识别：muse-asr 每个请求记一行 `local ok: … asr N ms`，媒体服务每条命令记一行
`voice id=… intent=… audio_s=… asr_ms=… total_ms=…`。

### 隐私与日志

- 从不保存音频。日志只有意图种类、长度和耗时。
- `TV_VOICE_LOG_TEXT=1` 在日志里加上识别出的文字（`heard='…'`），用来调阈值。默认关闭，调完要关掉。
- `TV_VOICE_WAV_DIR=<目录>`（仅开发用，默认关闭）把每句话存成 WAV，用来攒测试语料。共用的主机上不要设。

## 管理设备的 Wi-Fi（多用户中转）

设计和威胁模型：[`2026-10-13-wifi-setup-design.zh_CN.md`](../docs/superpowers/specs/2026-10-13-wifi-setup-design.zh_CN.md)。

设备主人（或者是这台设备成员的管理员）可以在手机遥控页和电脑版上查看、添加、删除、调整顺序、立即切换设备保存的 Wi-Fi
（`GET`/`POST /api/wifi`，`server/relay/wifi.py`）。其他人一律 404。

- **钥匙。** 中转（只在 `--db` 时）启动时从 `--wifi-key` 读 X25519 私钥（默认是数据库旁边的 `wifi-key`，第一次启动时以 0600 生成），
  把公钥写进数据库 meta `wifi_pub`。这个文件要和数据库一起备份：换了钥匙，设备会拒绝修改，直到在设备上完成一次配网。
- **路径。** 中转用设备记住的公钥加密每一条修改（X25519 + HKDF-SHA256 + AES-256-GCM，`server/wifi_crypto.py`），
  在设备的事件文件里加一条 `wifi` 事件（`text` 是信封，`value` 是 Unix 秒）。电视服务对 HELLO 里有 `"wifimgr": 1` 的固件，
  在 30 秒内把它作为 `SWITCH {"wifi": …}` 发出，在 CONFIG 里给 `"wifi_key"`，并记下设备 `STATUS {"wifi": …}` 的回答。
- **存储。** 表 `device_wifi`（`server/tenancy/wifi.py`）：记住的设备公钥、上次报告的 Wi-Fi 名字和正在用的、上一个 seq、
  等回答的修改、上一次回答。密码从不写进数据库、事件文件或日志，只在中转加密时的内存里。
- **不在线不排队。** 设备不在线时直接拒绝（`小电视不在线`）；20 秒没回答，页面显示 `小电视没有回应`。
- **重新配对。** 钥匙变了的设备（刷机清空过）会被拒绝，直到主人点「重新配对」；这段时间里的下一次 HELLO 同时换上新的密钥和 Wi-Fi 钥匙。

部署：`server/wifi_crypto.py`、`server/tenancy/{db,gate,wifi}.py`、`server/tv_server.py`、`server/cast_switch.py`、
`server/relay/{events,wifi,web,__main__}.py` 和两个页面。中转要能在数据库所在目录里写文件（它本来就在那里写数据库）才能生成 `wifi-key`。
单用户（旧）模式没有设备钥匙，这个功能不开。

## 调度、边界与停止

- 服务端时间起点为 CONFIG 写完后 200 ms；音频在 PTS 前 200 ms 发送，视频提前 50 ms。
  发送调度不能保证网络到达时间和实际设备播放时间。
- **跨类型的线上 PTS 不保证全局排序。** 按发送截止时间合并事件，PCM 内与 JPEG 内 PTS
  各自严格递增，序号全局递增。如果两种数据统一按原始 PTS 排序，会让视频挡在需要更早
  预送的音频之前，形成队头阻塞。例如 audio PTS=140 会先于 video PTS=0 发送。
  接收端必须分别跟踪音频和视频 PTS。
- 每 10 秒素材索引循环，但同一会话 PTS 继续到 10000 及以后。整数时间戳避免累计误差：
  视频 PTS=`frame_index*1000//12`。默认持续 1800 秒（180 轮），可设 1–86400 秒。
  重连创建新会话和新时间起点。
- 预生成路径无应用发送队列、不做实时转码（直播路径由 `live` 子命令另行启动 ffmpeg）。预生成时内存固定持有 10 秒素材（最高约 3.3 MiB）和一个有界发送包。
  请求内核发送缓冲 32768 字节；操作系统可能取整或倍增。
- 非阻塞套接字读写使用**每完整包 250 ms 总时限**，不因每个碎片到来重置时限。
  操作之间每最多 50 ms 检查额外连接和取消。半包发送超时即关闭。
- JPEG 超过呈现时刻 83 ms 尚未发送则丢弃；音频迟于呈现时刻 100 ms 则关闭会话，
  不追赶无界过期积压。接收端负责重连和重缓冲。
- SIGINT/SIGTERM 停止本地服务器，无后台工作线程；有限播放在时间线结束时发送 END，
  监听器继续等待下一客户端。正常用户态取消延迟不超过约 300 ms；这不是对操作系统
  调度或主机挂起的硬实时保证。
- 日志包括认证后的会话编号、每 10 秒已发 PCM/JPEG 计数、丢视频总数及关闭/失败总结。
  从不包含凭据或收到的载荷。这些是**套接字提交计数**，不是 DAC/DMA、声学播放或音画同步验收数据。

## 主机验证

```sh
python3 tests/test_tv_server.py -v
python3 tests/test_video_import.py -v
python3 tests/test_live_transcode.py -v   # 加 TV_LIVE_TEST=1 才真实拉流
```

测试覆盖包头字节、拆包/粘包、EOF、不搜索 magic 恢复、未知字段、载荷及 uint32 边界、
慢接收/发送总时限、token 来源/权限/鉴权、会话错配、额外客户端拒绝、播放结束、JPEG 元数据，
以及完整 30 分钟**模拟**调度（90000 个 PCM 包、21600 个 JPEG 帧）。
PATH 有 ffmpeg 时，还真实预生成并重新加载 10 秒素材，检查全部 10 个音频标记窗口；
无 ffmpeg 则明确跳过该预生成测试。拆包测试使用的 JPEG 元数据替身不是图片解码验证。

30 分钟真实时间设备测试、DMA/音频时序、射频性能与 ROM JPEG 解码属于另外的验收工作。
主机测试通过不意味着这些项目通过。参见[仓库测试指南](../docs/development/engineering/build-and-test.md)。

## 范围说明

保留约定的不等预送量，只明确跨类型 PTS 的排序含义。非法输入直接关闭而非回复 ERROR，
避免半包歧义；新增 CONFIG 字段为补充提示。本次允许修改范围是 `server/` 和
`tests/test_tv_server.py`，故说明放在服务器旁，文档索引/changelog 由协调改动整合。
本模块不改固件或部署配置。
