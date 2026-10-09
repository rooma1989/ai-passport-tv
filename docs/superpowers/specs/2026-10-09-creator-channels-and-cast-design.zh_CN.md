<p align="right">
  <strong>简体中文</strong> · <a href="2026-10-09-creator-channels-and-cast-design.md">English</a>
</p>

# UP 主频道与手机投屏 —— 设计

状态：2026-10-09 已确认（方案 A）。承接
[`2026-10-09-paged-setup-and-help-design.zh_CN.md`](2026-10-09-paged-setup-and-help-design.zh_CN.md)。

## 目标

除了直播电视，星智设备还能：

1. **UP 主频道**：每个关注的 B 站或 YouTube 博主一个台，播放他最新的、还没看过的视频，播完自动接下一个。
2. **投屏**：在手机网页上粘贴 B 站或 YouTube 视频链接，设备自动切到「我的投屏」台播放，播完回到原来的台。

## 已定事项

| 问题 | 决定 |
| --- | --- |
| UP 主台播什么 | 最近 20 个投稿里最新的未看视频；20 个都看过后从最新的重新开始 |
| 频道怎么放 | 每个 UP 主一个台，台名即 UP 主名；「我的投屏」和 UP 主排在列表最前 |
| 链接怎么发 | 手机网页；设备自动切台 |
| 网页权限 | 带随机密钥的私密链接（`?k=…`），收藏到手机 |
| 平台 | B 站和 YouTube |
| YouTube 在哪取 | 用户的海外服务器 cstone，绝不在阿里云上取 |

阿里云服务器上不跑任何代理客户端：阿里云国内机器的使用条款不允许，而且这台机器还托管着用户的网站。

## 架构

```
手机 ──HTTPS──▶ nginx（iewww.com/TV/cast/）──▶ 中转（阿里云，127.0.0.1:8099）
                                                  │  B 站：yt-dlp + ffmpeg，直连
                                                  │  YouTube：HTTPS ──▶ 中转（cstone，只做 YouTube）
                                                  ▼
电视服务（阿里云，:8098）── ffmpeg ── http://127.0.0.1:8099/ch/<id>
      │
      └── TCP ──▶ 星智设备
```

- **中转**是新的 Python 服务（`server/relay/`），把「UP 主 X 的下一个视频」或「投屏视频」变成一条
  HTTP 上的 MPEG-TS 流。电视服务把中转地址当普通直播源处理。
- **同一份中转代码**在 cstone 上以「只做 YouTube」模式运行，只提供 `/yt/<视频 id>`：
  把该视频压成小码流输出。阿里云的中转把 YouTube 的活交给它。
- **电视服务改动**：频道表变化时重新读取；新增 SWITCH 包推给设备。
- **固件改动**：收到 SWITCH 就切台，效果和用户在列表里选台一样。

## 中转（阿里云）

### 播放流

`GET /ch/<id>` 是电视服务唯一用到的接口，只监听本机回环地址，不需要密钥。

- `cast`：播投屏队列当前视频；队列为空返回 404。
- `upNN`：选出该 UP 主的下一个视频：
  1. 投稿列表超过 30 分钟就刷新一次（B 站对列表请求限流），保留最新 20 个；
  2. 有保存进度的视频，从「进度 − 10 秒」处续播；
  3. 否则取最新的未看视频；
  4. 20 个都看过，清掉标记，取最新的一个。
- 每个视频先用 `yt-dlp -J` 拿到各格式地址及所需 HTTP 头（B 站需要 `Referer`）。B 站：取 ≤360p 的
  最低 AVC 视频和最低音频，`ffmpeg -re -c copy -f mpegts` 直接封装，2 核服务器上不转码。
  YouTube：数据来自 cstone，已经是小码流。
- 播放中每 10 秒保存一次进度（起点 + 已过的真实时间，`-re` 按实时速度输出）。
- 视频自然播完就标记已看、清除进度，然后结束响应。电视服务看到解码停止，现有重连流程
  （`reconnect.hold`）重新打开同一地址，中转就给出下一个视频。两个视频之间设备会显示几秒重连提示，可以接受。
- 投屏视频播完，中转把它移出队列；队列空了就写一个「return」事件（见下）。

### 频道表

中转生成 `relay-channels.txt`（格式同 `channels.txt`）：
`cast | 我的投屏 | http://127.0.0.1:8099/ch/cast`，每个 UP 主一行 `upNN | <名字> | …/ch/upNN`。
名字截到 10 个字，保证 CONFIG 里的列表不超过 `TV_CONTROL_MAX`（7168 字节）；UP 主最多 12 个。

### 给电视服务的事件

中转原子地写 `relay-events.json`（先写临时文件再改名）：`{"seq": n, "action": "cast" | "return"}`，
`seq` 只增不减。

### 管理网页与接口

nginx 把 `https://iewww.com/TV/cast/` 转到中转。这里的每个请求都必须带密钥（网页用 `?k=`，接口调用在
`X-Key` 头里带同一个值），密钥不对一律 404。密钥文件 `/etc/ai-passport-tv/relay-key`，权限 0640 root:tvserver。

| 调用 | 作用 |
| --- | --- |
| `GET /` | 网页（中文、手机尺寸、不引用外部资源） |
| `GET /api/state` | UP 主列表（名字、平台、剩余未看数）和投屏队列 |
| `POST /api/creators {url}` | 用主页链接添加 UP 主 |
| `DELETE /api/creators/<id>` | 删除 UP 主 |
| `POST /api/cast {url}` | 用这个视频替换投屏队列，写「cast」事件 |

只接受 `bilibili.com`、`b23.tv`（短链先解析）、`youtube.com`、`youtu.be` 的链接。yt-dlp 和 ffmpeg 都用参数列表调用，
不经过 shell，URL 放在 `--` 之后。

### 状态

`/var/lib/ai-passport-tv/relay-state.json`：UP 主（id、平台、来源地址、名字、带抓取时间的缓存列表、已看 id、
保存的进度）和投屏队列。原子写入，所有修改在同一把锁内完成。

## 中转（cstone，只做 YouTube）

- `GET /yt/<视频 id>?k=<密钥>` → yt-dlp 选 ≤360p 的格式，ffmpeg 实时转成 320×240 量级的 H.264（约 300 kbit/s）
  + AAC 48 kbit/s 的 MPEG-TS。跨境流量约每小时 150 MB。
- `GET /yt-list/<频道>?k=` → 最新 20 个投稿（id、标题）的 JSON，YouTube 博主列表也从 cstone 读取。
- 高端口上的 HTTPS，自签证书；阿里云中转只信任这张证书（固定证书文件），所以不需要域名。地址和密钥放在阿里云的
  `/etc/ai-passport-tv/cstone.conf`。
- YouTube 要求登录时，可选给 yt-dlp 一个 `cookies.txt`。
- systemd 单元，沙箱设置与 `tv-xingzhi.service` 相同。

## 电视服务改动

- **两个频道文件**：先 `TV_RELAY_CHANNELS_FILE`，再 `TV_CHANNELS_FILE`。每个新会话开始时，任一文件 mtime 变了就重读；
  文件有错则保留旧表并打警告。
- **切台**：直播会话中每秒查看一次 `relay-events.json`。遇到新的 `seq`：
  - `cast`：把当前频道记为 `previous`（当前是 `cast` 时不记），发 `SWITCH {"channel": "cast"}`；
  - `return`：设备仍在 `cast` 时，发 `SWITCH {"channel": previous}`。
  启动后第一次看到的 `seq` 只记录不执行，避免重放旧事件。HELLO 里没有 `"switch": 1` 的设备一律不发。
- `server/protocol.py` 新增包类型 `SWITCH = 8`，载荷为 JSON。

## 固件改动

- `main/av_protocol.h` 新增 `AV_SWITCH`（= 8）。
- 接收任务把频道 id（不超过 `AV_CHANNEL_ID_MAX`）拷到待切换槽位；主循环调用现有的 `request_switch()`，并显示常规的换台横幅。
  未知 id 打日志后忽略。
- `av_header_decode()` 与 `av_stream_accept()` 接受 `AV_SWITCH`（无标志位，长度 1..`AV_CONTROL_MAX`，和 `AV_PALETTE`
  一样只占一个序号）；两处都有主机测试。
- 改动之前的固件遇到未知包类型会拒绝并断开会话，所以设备在 HELLO 中声明 `"switch": 1`，服务端只给声明过的设备发 SWITCH。
  不支持的设备仍可手动选「我的投屏」观看。

## 出错处理

| 情况 | 处理 |
| --- | --- |
| 视频不可用（已删除、地区限制、充电专属） | 标记已看、记日志、试下一个（每次请求最多 3 个，之后返回 503） |
| 抓取 UP 主列表失败 | 用缓存列表；没有缓存就 503 → 设备显示重连提示，失效频道策略会换台 |
| cstone 连不上 | YouTube 台和 YouTube 投屏返回 503；B 站不受影响 |
| 网页上链接无效或不支持 | 网页显示错误，什么都不改 |
| 投屏时设备未连接 | 队列保留；之后选「我的投屏」即可播放 |

## 测试

- **先做验证**（其余部分动工前）：手工把一个 B 站视频封装成 HTTP MPEG-TS 地址，经电视服务播放，确认时钟校准能接受、
  流结束后会重新打开；在 cstone 上用 yt-dlp 取一个 YouTube 视频。
- 主机单元测试（`tests/test_relay_*.py`）：下一个视频的选择（已看、续播、20 个后回绕）、链接校验、频道表输出与名字截断、
  事件文件 `seq`、密钥检查、状态文件读写。yt-dlp 与 ffmpeg 用模拟替代。
- 电视服务测试：mtime 变化时重读、坏文件保留旧表、`cast` 再 `return` 发出正确的 SWITCH、不重放旧事件、
  不给没有 HELLO 标志的设备发 SWITCH。
- 固件主机测试：`AV_SWITCH` 的包头和序号接受。
- 设备：看直播时从手机投屏 → 自动切台、播放、播完返回。

## 分步交付

1. 验证。
2. 中转核心 + B 站 UP 主频道；电视服务重读频道表。
3. 投屏：网页、服务端与固件的 SWITCH。
4. 经 cstone 的 YouTube（需要像阿里云那样配好 cstone 的 SSH 密钥登录）。

每一步都能单独使用，部署并在设备上试过之后再做下一步。
