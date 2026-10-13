<p align="right">
  <strong>简体中文</strong> · <a href="2026-10-12-multi-tenant-design.md">English</a>
</p>

# 云服务多租户 —— 设计

状态：2026-10-12 草案，待决定。只做调研和设计，这里写的都还没有实现。承接
[`2026-10-09-creator-channels-and-cast-design.zh_CN.md`](2026-10-09-creator-channels-and-cast-design.zh_CN.md)、
[`2026-10-10-grouped-channels-and-remote-design.zh_CN.md`](2026-10-10-grouped-channels-and-remote-design.zh_CN.md)
和
[`2026-10-10-playback-and-device-controls-design.zh_CN.md`](2026-10-10-playback-and-device-controls-design.zh_CN.md)。

需求原话：「现在 PC 版是单用户。我想要多租户：PC 扫码登录、看到遥控、设备管理，因此需要一个整体的管理后台。」

部署情况只做一般性描述：**国内云主机**（1 个 vCPU、不支持 VNNI，约 1.8 GB 内存，公网带宽
3 Mbit/s，前面是 nginx；同一台机器还跑着另一个产品的 `muse-asr`）和**海外 YouTube 节点**
（yt-dlp + deno，1 核，内存上限 600 MB）。`<site>` 代表网站地址。

## 1. 现在哪些地方是单用户的

下面每一项都假定只有一个主人，也就是设计里要改的地方。

| 假定 | 位置 |
| --- | --- |
| 所有设备共用一个配对 token；公开版固件不带 token，用不了云主机 | `main/av_config.h` 的 `AV_PAIRING_TOKEN`，由 `main/av_player.c` 的 `hello()` 发出；`server/tv_server.py` 的 `authenticate()` 校验 |
| HELLO 里没有设备身份，服务端只能凭设备拨的端口认设备 | `main/av_player.c` 的 `hello()`；`server/tv_server.py` 的 `AVServer._session()` |
| 每种设备型号一个媒体服务进程，同一时间只服务一个连接，多余连接直接关闭 | `server/tv_server.py` 的 `AVServer.serve()` / `_reject_waiting()`；单元 `tv-xingzhi`（端口 8098）和 `tv-passport`（端口 8097） |
| 画面尺寸、帧率、字节率是整个进程的，导入时从环境变量读 | `server/frames.py` 的 `TV_GEOMETRY`，`server/rate.py` / `server/live.py` 的 `TV_FPS`、`TV_RATE_START` |
| 每个进程一张频道表，是模块级全局变量 | `server/live.py` 的 `CHANNELS`、`load_channels()`、`reload_channels_if_changed()` |
| 中转只有一份状态：UP 主、剧集、看过的标记、进度、一个投屏位、投屏历史、手机会话、一个扫码码 | `server/relay/state.py` 的 `State`，一个 `relay-state.json` |
| 中转只有一张频道表，流地址里没有归属（`/ch/up01`） | `server/relay/table.py`（`STREAM_BASE`、`render()`），`server/relay/web.py` 的 `_CHANNEL` |
| 所有人共用一个分组调整文件 | `channel-groups.json`、`server/channel_groups.py`、`TV_CHANNEL_GROUPS_FILE` |
| 中转认识哪些设备，写死在它的命令行里 | `server/relay/__main__.py` 的 `--extra-device 名字:正在播放:事件`；`server/relay/web.py` 的 `Relay.devices` |
| 一个主密钥；拿到密钥链接、设备上的码或已登录手机的人，能控制所有设备、改所有 UP 主 | `Relay.key_ok()`、`Relay.code_ok()`、`State.sessions` |
| 中转的推流状态只按频道 id 存 | `server/relay/web.py` 的 `Relay._active`、`_paused`、`_bridge`、`_titles` |

好消息是：媒体服务和中转之间已经是通过文件通信的，文件路径都来自环境变量
（`TV_NOW_PLAYING_FILE`、`TV_RELAY_EVENTS_FILE`、`TV_RELAY_CHANNELS_FILE`、
`TV_CHANNEL_GROUPS_FILE`、`TV_REMOTE_URL_FILE`）。给每台设备一套自己的文件，媒体服务内部不用改，
只要每个进程的环境不同即可。本设计就借这一点。

## 2. 目标与非目标

目标：

1. **账号。** 多个用户，各有自己的设备、UP 主、剧集、观看进度、投屏历史和频道分组。一个用户看不到、
   也控制不了别人的设备。
2. **设备身份。** 每台设备有稳定的 id 和自己的密钥；扫设备屏幕上的二维码把设备绑定到账号。
3. **PC 控制台。** 用已登录的手机扫码登录电脑；遥控、设备、订阅、剧集、设置。
4. **管理后台。** 用户；设备在线离线、正在放什么；主机负载和带宽；封禁、解绑；全局直播频道表；审计日志。
5. **多台设备同时播放**，在主机承受范围内，每种型号用对的画面尺寸、帧率和字节率。
6. 迁移**不丢数据**，不强制重刷：现在已经刷好的两台设备全程照常能用。

非目标：

- 对所有人开放的公共服务（见 Q1：法律和成本原因）。
- 付费、按用户计费的配额、不用邀请的自助注册。
- 设备 TCP 链路上的 TLS 或加密（不变；见「安全」）。
- 改媒体包的线上格式、码率控制或时序代码（`server/timeline.py`、`server/rate.py`）。
- 前四个阶段内的高可用或多台媒体主机。
- 迁走 `muse-asr`（它留在原处；容量估算里把它算作邻居）。

## 决定（2026-10-12）

第 3 节的建议全部采纳：

- Q1 租户：只邀请家人朋友，大约十台设备。
- Q2 登录：邀请链接，加上用已登录的手机扫码并输入码；密钥链接留给管理员应急用。
- Q3 角色：管理员、用户，以及只能管一台设备的访客。
- Q4 进度：按用户记；投屏按设备记。
- Q5 直播频道：管理员维护一张全局表，用户可以隐藏、排序、改分组，新的源走申请。
- Q6 同时播放：按四台规划。主机升级和第 4 阶段推迟到第 1–3 阶段做完以后。
- Q7 服务端代码：以这个 fork 的 `server/` 为准。
- Q8 未绑定设备：不转码，只显示「扫码绑定」的静止画面；15 分钟演示额度可选。

**补充（2026-10-13）：起步和恢复，用户已决定。** Q1、Q2 留了两个洞：新用户要等管理员的邀请；手机上的
登录全丢了的用户，除了找管理员没有别的路。现在两个都以设备为信任的根补上了（只有在设备跟前的人才看得到
它的屏幕、按得到它的键）：(a) 没登录的手机打开未绑定设备的绑定链接，可以直接建一个拥有这台设备的普通账号，
不用管理员批准（按网段限流、记审计）——这是借硬件自己注册，不是公开注册，没有设备的人仍然只能靠邀请，Q1 的
「只邀请」不变；(b) 已绑定设备的状态页上做一个手势，设备在自己已认证的会话上向服务器要一个主人的一次性手机
登录链接，以二维码显示在设备上（每台设备每小时 3 个，主人别的登录会收到提示）；(c) 管理员的
`login-link <用户 id>` 命令是最后的办法。实现和操作见
[`tenancy-phase1-runbook.zh_CN.md`](../../development/tenancy-phase1-runbook.zh_CN.md#没有登录时怎么进来--恢复登录)。
还没定：管理页（阶段 3）要不要列出自己注册的用户以便审查；主人被封的设备要不要也允许恢复（现在拒绝）。

## 3. 需要你决定的问题

每个问题都列了选项和建议。文档其余部分按建议写；选别的会影响注明的章节。

### Q1. 租户是谁？

| 选项 | 后果 |
| --- | --- |
| **A. 亲友，凭邀请，约 10 台设备** | 不开放注册，没有验证码和短信成本，一个管理员。现在的主机够同时 1–2 台，再多稍作升级即可。 |
| B. 公开注册 | 需要注册、防滥用、服务条款页、网站 ICP 备案，主机要按未知的需求来配。直播频道来自社区播放列表，版权状况未经核实（`server/README.md` 里写明了）；在中国大陆向公众转播电视台和视频网站内容需要《信息网络传播视听节目许可证》。 |

**建议：A。** B 是另一个产品，有法律风险；本设计不妨碍以后放开，但那应当另行决定。

### Q2. 用户怎么登录？

| 选项 | 需要什么 | 说明 |
| --- | --- | --- |
| **A. 邀请链接，PC 用「已登录的手机扫码」** | 不依赖外部服务 | 手机通过邀请（管理员发的一次性链接或二维码）登录一次；电脑用这台手机扫电脑上的二维码登录，或者在手机上输入 6 位码。基于现有的会话 cookie 和扫码登录（`State.new_session()`、`/TV/R/<码>`）。 |
| B. 微信开放平台「网站应用」登录 | 微信开放平台上**经过认证的企业开发者账号**（个人不能创建网站应用）、ICP 备案的域名、每年的认证费 | 用户熟悉；但依赖微信审核和一个公司主体。手机在微信里打开时，要在微信内授权还需要一个服务号，同样要企业认证。 |
| C. 短信验证码 | 短信服务商账号，签名和模板要审核（个人受限），防短信轰炸的图形验证，按条付费 | 有可找回的身份（手机号），但要存个人信息，也招来滥用。 |

**建议：A**，现有的密钥链接（`?k=<中转密钥>`）保留，作为管理员的应急登录。以后如果有了公司账号，
B 可以作为给已有用户挂会话的第二种方式加上（一个 `wechat_unionid` 字段）。

### Q3. 管理角色

| 选项 | 角色 |
| --- | --- |
| **A. 两种账号角色，加设备访客** | `admin`（你：全部权限）、`user`（自己的设备和数据）；另有**设备访客**会话：扫一台已绑定设备的码得到，只能控制那一台设备，别的都改不了。 |
| B. 三种账号角色 | 多一个 `operator`（能看、能封禁，不能改频道表和角色）。只有第二个人一起运维时才有用。 |

**建议：A。** 访客会话保留了现在家里的便利（屋里谁都能扫设备、用遥控），又不会把主人的订阅交给访客。

### Q4. 观看进度按用户还是按设备？

现在两台设备共用一份状态，所以一个 UP 主的进度是共享的。**建议：按用户**（一家人共用一个队列，和现在一样），
**投屏位按设备**（同一用户的两台设备可以投不同的视频）。按设备分的「档案」以后再说。

### Q5. 直播频道表：只有全局的，还是用户可以自己加？

| 选项 | 后果 |
| --- | --- |
| **A. 一张全局表，管理员编辑；每个用户可以隐藏、排序、改分组** | 用户给的网址不会进到服务器上的 ffmpeg。 |
| B. 用户可以加自己的直播网址 | 服务器上的 ffmpeg 会打开用户给的网址：服务端请求伪造（内网地址、中转的回环端口、`file:`），每个网址都要有协议白名单、排除内网地址，还要审核队列。 |

**建议：A**，用户可以*建议*一个台，由管理员添加（有 B 的审核队列，没有 B 的风险）。

### Q6. 同时播放的预算

现在的主机能撑**一路**满画质的星智，或**两台**都降画质（见 7.6 节）。要更多设备同时播放，先要更多公网带宽，
其次是 CPU 和内存。请决定目标峰值并发（建议：按约 10 台设备里**同时 4 台**来规划），以及是升级现有主机还是另加一台
媒体主机。阶段 4 只有和这次升级一起做才值得。

### Q7. 服务端代码放在哪里？

`server/README.md` 说媒体服务已经搬到单独的仓库、这里的 `server/` 是冻结的快照，可是最近的服务端和中转工作
（比如多设备、剧集的提交）都进了本仓库，云主机上跑的也是它。**建议：** 阶段 1 之前定下哪个仓库是正本，并改正 README；
本设计引用的是本仓库的代码。

### Q8. 未绑定的设备能做什么？

**建议：** 既然只凭邀请，未绑定设备不转码：服务端发 CONFIG（带绑定二维码）和一张写着怎么绑定的静止画面，配静音，
几乎不花资源。管理后台可以设成「未绑定设备每次开机可以放 15 分钟全局频道」，方便演示。

## 4. 身份与数据模型

### 4.1 实体

```
user ──< device_member >── device
 │                           │
 ├── session (手机, PC)      ├── device_state (投屏位, 频道偏好)
 ├── user_state (UP 主,      └── 每台设备的文件 (正在播放, 事件,
 │    剧集, 进度, 历史)                         中转频道表, 分组, 遥控地址)
 └── invite (管理员创建)

source_cache (共享)：UP 主投稿列表和剧集分集表，按网站 + 来源存
audit_log, login_ticket
```

### 4.2 设备身份（要改固件）

新固件在 HELLO 里加四个平铺的键（和现有的 `volume`、`pet`、`audio_only` 一样平铺）：

```json
{"version":1, "channel":"cctv1", "switch":1, ...,
 "device_id":"a1b2c3d4e5f6", "model":"xingzhi-cube-154",
 "device_secret":"<43 个字符，32 个随机字节的 base64url>", "fw":"<git describe>"}
```

- **`device_id`**：芯片出厂时写在 eFuse 里的基础 MAC（`esp_efuse_mac_get_default()`），12 位小写十六进制。
  重刷、擦 NVS 都不变；在 AI Passport 上，Muse 固件和播放器读到的是同一个。它是标识，不是秘密：局域网里看得到。
- **`device_secret`**：Wi-Fi 起来之后用 `esp_fill_random()` 取 32 字节（此时射频给硬件随机数发生器提供熵），
  第一次开机生成，存在 NVS 命名空间 `avsettings` 的 `dsecret` 键（和 `main/av_store.c` 里的 `pet`、`audio` 放在一起）。
  Muse 双固件版本从不擦 NVS（`docs/boards/passport-muse-dual.zh_CN.md`），切换固件后还在。`erase_flash`
  （星智第一次刷写时要做一次）会生成新密钥，设备需要重新配对（见 8.1 节）。
- **`model`**：`xingzhi-cube-154` 或 `ai-passport`，由编译决定（`main/av_protocol.h` 的 `AV_VIDEO_WIDTH` 和板子配置）。
  服务端据此选画面尺寸、帧率和字节率（见 7.4 节）。
- **`fw`**：这次编译的版本字符串，给管理页看，也用来判断什么时候可以关掉旧 token。

固件编进了 token 时，HELLO 里照样带 `token`，所以新固件也能连现在的服务端：`authenticate()` 只检查 `version` 和
`token`，不认识的键一律忽略。有了每台设备的密钥，**公开版固件**也能用云主机了，因为不再需要编进去的 token；
这正合 `AGENTS.md` 要求测试公开版的规则。

固件这边改动小：`main/av_player.c` 的 `hello()`，`main/av_store.c` 里一对读写函数，再加一个检查 HELLO JSON 形状的主机测试。
AI Passport 的 HELLO 仍远小于 `AV_CONTROL_MAX`（7168 字节）。

### 4.3 绑定设备

固件已经会在状态页上把 CONFIG 里的 `remote_url` 画成二维码（`AV_REMOTE_URL_MAX` 64 个字符，大写字母数字，
第 2 版二维码；见 `server/tv_server.py` 的 `_REMOTE_URL`）。绑定就复用它，所以**二维码这部分不用改固件**：

1. 新设备连上来。服务端看到一个不认识的 `device_id`，把它记为 `unclaimed`（未认领），存下密钥的 SHA-256
   （首次使用即信任），并生成 20 位认领码（字母表与 `State.ensure_code()` 相同）。
2. CONFIG 里带 `remote_url = HTTPS://<SITE>/TV/B/<认领码>`，状态页上显示出来。新固件还可以收到 CONFIG 里的
   `"bound":0`，不打开状态页也提示「扫码绑定」；旧固件忽略这个键。
3. 已登录的手机扫码。页面显示型号和 id 的后四位，问「绑定到你的账号？」。确认后设备变为 `active`，
   该用户成为主人，认领码清除，写一条审计记录。
4. 设备的下一次会话（服务端可以结束当前会话来促成）拿到主人的频道表，`remote_url` 换成这台设备自己的遥控码
   `HTTPS://<SITE>/TV/R/<码>`，此后扫它打开这台设备的遥控（扫码者不是成员时得到访客会话）。

没登录的手机扫认领码时，页面会让用户先从主屏幕图标打开遥控，或在遥控里输入认领码（5.2 节解释原因：cookie 不共享）。2026-10-13 起也可以直接建一个拥有这台设备的账号（见「决定」下的补充）。

解绑（主人或管理员）把设备变回 `unclaimed` 并生成新认领码，密钥哈希保留，删除这台设备的设备状态（投屏位、频道偏好）；
主人的用户状态不动。

### 4.4 现有数据去哪儿

| 现在（文件、字段） | 改为 | 范围 |
| --- | --- | --- |
| `relay-state.json` 的 `creators[]`（kind 为 `up` 和 `series`）、`videos`、`watched`、`positions` | `user_state.creators`（JSON 结构不变） | 用户 |
| `creators[].videos`、`fetched_at` | 同时写进 `source_cache`，关注同一来源的所有用户都读它 | 共享 |
| `relay-state.json` 的 `cast`、`cast_position`、`cast_done` | `device_state.cast` | 设备 |
| `relay-state.json` 的 `history` | `user_state.history` | 用户 |
| `relay-state.json` 的 `sessions`（明文 token） | `session` 表，**存哈希** | 用户 |
| `relay-state.json` 的 `code` | 星智设备的遥控码（它屏幕上的二维码照常能用） | 设备 |
| `channel-groups.json` | `user_state.group_overrides` | 用户 |
| `channels.txt` | 仍是文件：全局直播频道表，在管理页编辑 | 全局 |
| `relay-channels.txt` | 每台设备生成一份：`devices/<id>/relay-channels.txt` | 设备 |
| `now-playing.json`、`relay-events.json`、`remote-url.txt` | `devices/<id>/…` | 设备 |
| `--extra-device` 列表 | `device` 表的行 | — |

UP 主和剧集的 JSON 结构不变，`State`（`server/relay/state.py`）就几乎可以原样保留：它变成「一个用户的状态」，
从一行数据读、存回一行，而不是一个文件。`Video`、`Creator`、`next_video()`、`episode()`、`mark_watched()` 都不改。
会话和扫码码从 `State` 移到账号模块。

频道 id 不变（`up01`…、`sr01`…、`cast`）；它们在一台设备的频道表里唯一，固件只需要这个（`AV_CHANNEL_ID_MAX` 16）。
每台设备的 CONFIG 上限（`server/channel_groups.py` 的 `CONFIG_MAX` 7168 字节、`CHANNEL_MAX` 128）按设备算，
所以中转现有的「再加一个台 CONFIG 就超长则拒绝」检查改为按设备做：用户自己的 UP 主和剧集，加上全局表减去用户隐藏的台。

### 4.5 存储：SQLite

**SQLite（标准库的 `sqlite3`），WAL 模式，一个文件 `/var/lib/ai-passport-tv/tv.db`。**

- 服务端代码只用标准库（`server/README.md`）；SQLite 保持这一点。
- 几十个用户和设备，一天几百行。写得最多的是存进度：每路正在播的流每 10 秒更新一行（`Relay.play()` 里的 `tick()`），
  同时 4 路也远低于每秒 1 次写。
- 有两个进程会用它：中转（账号、状态、网页）和媒体网关（设备认证、`last_seen`）。WAL 允许多个读者同时读、一个写者写；
  几秒的 `busy_timeout` 足以应付偶尔的冲突。
- 不多一个守护进程：在已经跑着 `muse-asr` 的 1.8 GB 主机上，一个数据库服务器要占 100 MB 以上。
- 备份：每晚 `sqlite3 tv.db ".backup …"`，留 14 天；每次迁移前也备份一次。

由别的进程按自己的节奏读取的东西仍用文件（媒体工作进程的频道表、事件、正在播放）：这套进程间通信已经能用，也有测试
（`tests/test_relay_events.py`、`tests/test_cast_switch.py`）。

表结构草图（类型从略）：

```sql
user(id INTEGER PK, name TEXT, role TEXT CHECK(role IN ('admin','user')),
     status TEXT CHECK(status IN ('active','banned')), created_at REAL)
session(token_sha256 BLOB PK, user_id INT, kind TEXT /* phone|pc|guest */,
        device_id TEXT NULL /* 仅访客 */, created_at REAL, last_seen REAL,
        expires_at REAL, user_agent TEXT, ip_prefix TEXT)
invite(code_sha256 BLOB PK, created_by INT, role TEXT, expires_at REAL, used_by INT NULL)
login_ticket(ticket_sha256 BLOB PK, short_code TEXT, created_at REAL, expires_at REAL,
             state TEXT /* pending|confirmed|used */, user_id INT NULL,
             pc_ip_prefix TEXT, pc_user_agent TEXT)
device(id TEXT PK, model TEXT, secret_sha256 BLOB, status TEXT /* unclaimed|active|banned|repair */,
       name TEXT, claim_code TEXT NULL, remote_code TEXT, fw TEXT,
       legacy_port INT NULL, created_at REAL, last_seen REAL, last_ip_prefix TEXT)
device_member(device_id TEXT, user_id INT, role TEXT /* owner|member */, PRIMARY KEY(device_id,user_id))
user_state(user_id INT PK, data TEXT /* JSON：creators、history、group_overrides、hidden、order */,
           updated_at REAL)
device_state(device_id TEXT PK, data TEXT /* JSON：投屏位、上次的台 */, updated_at REAL)
source_cache(site TEXT, source TEXT, kind TEXT, name TEXT, videos TEXT, fetched_at REAL,
             PRIMARY KEY(site,source,kind))
audit_log(id INTEGER PK, at REAL, actor_user INT NULL, actor_ip_prefix TEXT, action TEXT,
          target_kind TEXT, target_id TEXT, detail TEXT)
```

密钥和 token 只存 SHA-256 哈希（它们本身有 128 位以上的随机性，慢哈希没有额外好处）。IP 地址只存前缀：
IPv4 存 /24，IPv6 存 /48。

## 5. PC 控制台

### 5.1 一套接口，两种布局

手机页（`server/relay/assets/remote.html`）和控制台调用同一套接口。现在每个接口都作用于 `Relay.target()`
（选定的设备，或最近换过台的那台）。多租户保留这个思路，只是限定在调用者自己的设备里：

- `GET /api/state` 返回调用者的设备（按成员关系过滤的 `device_rows()`）和选中那台的状态。
- 每个设备操作（`/api/switch`、`/api/seek`、`/api/next`、`/api/pause`、`/api/resume`、`/api/device`、`/api/pet`、
  `/api/audio`、`/api/cast`、`/api/history/play`）多一个可选的 `device`；不给时按现在的 `target()` 规则，
  但只在调用者的设备里选。每次调用服务端都检查成员关系。
- 账号级接口（`/api/creators`、`/api/series`、`/api/history`、`/api/groups`）作用于调用者的用户状态；访客得到 404。
- 新增：`/api/devices`（列出、改名、解绑、重新配对、成员）、`/api/sessions`（列出、吊销自己的会话）、
  `/api/login/*`（5.2 节）、`/api/admin/*`（第 6 节，仅管理员）。

手机页只改一处：只列调用者的设备。控制台是同一进程提供的第二个静态页（`assets/console.html`），宽屏布局：
左边设备列表，中间遥控，右边标签页放订阅、剧集和设置。

### 5.2 扫码登录

```
PC 浏览器                         服务端                          已登录的手机
   │ GET /tv/console（无会话）     │                                   │
   │──────────────────────────────▶│ 建 login_ticket：128 位 id、      │
   │◀── 页面：二维码 + 短码 ───────│ 6 位码，2 分钟过期                │
   │   二维码 = https://<site>/TV/L/<ticket>                           │
   │                               │◀──── 手机打开 /TV/L/<ticket> ─────│（相机扫码）
   │                               │      或在「登录电脑」里输入 6 位码 │
   │                               │── 确认页：「在电脑上登录，        │
   │                               │   <浏览器>，<城市/IP 前缀>？」 ──▶│
   │                               │◀──────────── 确认 ────────────────│
   │ 长轮询 /api/login/poll        │ ticket → confirmed，user_id       │
   │──────────────────────────────▶│                                   │
   │◀── Set-Cookie：PC 会话 ───────│ ticket → used（一次性）           │
```

- **为什么还要输入码。** 手机相机扫码会在系统浏览器里打开链接，微信扫一扫会在微信自己的浏览器里打开。
  两者都不和加到主屏幕的遥控共用 cookie（现有文档已经写过：主屏幕应用有自己的 cookie，所以它的地址里带着会话）。
  打开链接的浏览器没有会话时，页面提示「从主屏幕打开电视遥控，输入码 `K7P-4QX`」，为此遥控里加一个「登录电脑」入口。
  在遥控页里直接调用相机扫码（getUserMedia 加 JavaScript 二维码解码）留作以后的选项。
- 确认页显示 PC 的浏览器和大致网络，防止用户被骗去批准别人的登录请求（二维码钓鱼）。
- 登录请求一次性、2 分钟过期，按 IP 限制创建频率（8.4 节）。PC 会话 30 天（手机会话保持现在的 180 天，`SESSION_S`），
  cookie 属性和现在的 `tvs` 一样是 `HttpOnly; Secure; SameSite=Lax`（`_cookie_header()`），并出现在用户的会话列表里，可以吊销。
- 轮询：PC 每 2 秒长轮询一次，或保持一个最长 25 秒的请求；nginx 对 `/tv/` 已经设了 `proxy_buffering off` 和
  `proxy_read_timeout 60s`（`packaging/nginx/tv-relay.conf`）。

### 5.3 控制台页面

| 页面 | 内容 | 复用 |
| --- | --- | --- |
| 遥控 | 正在播放、分组和频道、进度条、快进快退、暂停、下一个、音量、亮度、宠物、只听声音 | `summary()`、`now()`、`groups()`、`video_progress()` |
| 设备 | 名字、型号、固件、在线、正在放、最后在线；改名、重新配对、解绑；成员和访客；「绑定设备」说明 | 新的 `/api/devices` |
| 订阅 | UP 主（B 站、YouTube），添加删除，每个 UP 主下一个要放的视频 | `add_creator()`、`remove_creator()` |
| 剧集与分集 | 剧集列表、下一集是第几集、选集、清零进度 | `add_series()`、`State.episode()`；新增「设定下一集」 |
| 投屏 | 粘贴链接，历史可重播、删除 | `cast()`、`replay()`、`forget()` |
| 频道 | 隐藏、排序、改分组直播台；建议新台 | `set_group()`；新增隐藏、排序 |
| 设置 | 自己的会话（手机、PC、访客），可吊销；YouTube 节点状态 | 新的 `/api/sessions`；`youtube_state()` |

## 6. 管理后台

同一个进程，`/tv/admin`，只限管理员，每个操作写审计日志。

| 页面 | 内容 |
| --- | --- |
| 总览 | 当前在线设备，每台：主人、型号、频道、会话时长、画面和声音字节率、帧率、当前字节目标；主机 CPU、内存、公网出方向带宽；YouTube 节点健康状况和占用的推流名额 |
| 用户 | 列表、角色、状态、设备、最后在线；生成邀请；封禁（结束其会话，拒绝其设备）、解封；删除（先导出数据） |
| 设备 | 所有设备及状态（`unclaimed`、`active`、`banned`、`repair`）、主人、固件、是否旧设备；解绑、封禁、接受新密钥、强制重连 |
| 直播频道 | 表格编辑全局 `channels.txt`：添加、改名、排序、试开网址、校验后保存并留一份带日期的备份；用户的建议 |
| 审计日志 | 谁、何时、从哪里（IP 前缀）、做了什么、对象；按用户或设备筛选 |
| 系统 | 注册方式、未绑定设备策略（Q8）、旧 token 开关、并发上限、备份 |

数据来源，都不需要额外的守护进程：

- **每台设备：** 媒体工作进程已经每 10 秒写一次 `now-playing.json`（`now_playing.write()`、`REFRESH_S`），
  每 10 秒记一次字节数和忙碌时间（`tv_server.py` 里 `busy=` 那行日志附近）。在正在播放的记录里加 `bytes_10s`、`fps`、
  `target_rate`；管理页读每台设备的文件。
- **主机：** 中转每 5 秒采样 `/proc/stat`、`/proc/meminfo`、`/proc/net/dev`，内存里留 24 小时。
- **每个进程：** 每个工作进程的 `/proc/<pid>/stat`（网关知道它们的 pid）。
- **编辑全局频道表**时用 `live.parse_channels()`（媒体服务自己用的同一个解析器）和 `channel_groups.config_bytes()`
  的 CONFIG 大小检查来校验；`tools/channel_config.py` 在本地页面上已经做了这些检查，直接复用。

## 7. 媒体通路

### 7.1 现状

每种设备型号一个单元、一个端口（`tv-xingzhi` 在 8098，240x180、15 fps、`TV_RATE_START=200000`；`tv-passport` 在 8097，
320x180、12 fps、110000；`packaging/systemd/` 里只有 `tv-xingzhi.service`，Passport 的单元是在主机上复制出来的）。
`AVServer.serve()` 接受一个连接，把 `_session()` 跑完，再关掉期间排队的连接。换台就是新建一个 TCP 连接（频道写在 HELLO 里），
所以「一台设备」其实是一串短会话，两次会话之间的设备记忆存在 `AVServer` 的属性里：`pending_switch`、`switch_watcher`
（上次看到的事件 `seq`、投屏播完要回去的台）、`channel_name`、`_device_uptime`。每个直播会话都启动自己的 ffmpeg，
解码源并缩放到该型号的尺寸（`server/live.py` 的 `LiveChannel`、`source_command()`），Python 进程再按连接切条、映射调色板、
压缩、按节奏发送（`frames.py`、`rate.py`、`_pace_live()`）。

### 7.2 方案

**(a) 网关加每台在线设备一个工作进程。** 新增一个小服务 `tv-gate`，占用公网端口。它读第一个 HELLO，在数据库里核对设备，
查型号配置，然后把已打开的套接字和 HELLO 字节通过 Unix 套接字交给这台设备的工作进程（`socket.send_fds()`，Python 3.9+）。
这台设备还没有工作进程时，网关按设备的环境启动一个（`TV_GEOMETRY`、`TV_FPS`、`TV_RATE_START` 以及这台设备的文件路径）。
工作进程就是现在的 `AVServer`，多一个「从网关接连接」的模式，代替 `listener.accept()`。设备在线期间及之后 10 分钟内它一直活着，
所以 `pending_switch` 和投屏的返回台能跨换台、跨短暂断线保留；之后退出。

**(b) 一个进程服务多个会话。** 需要把模块级状态改成按会话的状态：`frames.WIDTH/HEIGHT/STRIPE_ROWS`（只从 `TV_GEOMETRY`
读一次）、`live.py` 里的 `CHANNELS` 全局变量、`rate.py` 里的码率常量，以及 `AVServer` 里「只有一个连接」的种种假定。
每帧的工作在 Python 里做（调色板映射、差分、压缩、可选的 numpy 感知拟合），一个进程会让所有设备的编码挤在一个 GIL、一个核上，
而 `timeline.py` 和 `rate.py` 依赖每帧很紧的期限。

**(c) 每台设备一个固定单元。** 每台设备复制一份 `tv-xingzhi.service`，各有端口、token 文件和数据目录，就像当初给 Passport 做的那样。
不用改代码，但每台设备都要在设置页填自己的端口、在防火墙里开端口，闲着的单元也占内存，中转的设备列表仍写在命令行里。

| | (a) 网关 + 工作进程 | (b) 单进程 | (c) 固定单元 |
| --- | --- | --- | --- |
| 代码改动 | 新网关（约 300 行）、`AVServer` 的工作模式、进程管理 | 大改时序敏感的代码 | 无 |
| 按型号的画面尺寸 | 自然（每个工作进程一套环境） | 要把 `frames.py`、`rate.py` 去全局化 | 自然 |
| 能用多核 | 能（多进程） | 不能（GIL） | 能 |
| 设备设置 | 所有设备同一个地址 | 同一个地址 | 每台一个端口 |
| 空闲成本 | 宽限期过后为零 | 低 | 每台设备常驻一个 Python 进程 |
| 隔离 | 崩溃只影响一台设备 | 一次崩溃影响所有设备 | 按设备 |

**建议：(a)。** 它几乎不动已经测过的会话代码，顺带解决了按型号的画面尺寸，还能用多核。只有在阶段 4 之前就必须服务第三台设备时，
才拿 (c) 临时顶一下。

### 7.3 网关细节

- **端口。** 网关监听 8098 和 8097（已刷好的设备拨的端口），别的不听；新设备哪个都可以用。HELLO 里有了 `model`，
  端口就不再决定画面尺寸。工作进程不监听任何端口。nginx 不在设备通路上：设备协议不是 HTTP，用 nginx 的 `stream`
  代理只会把设备的地址藏起来。
- **握手时限。** 设备发出 HELLO 后等 CONFIG 包头 2 秒（`main/av_player.c` 的 `receive_task()` 里的 `header_deadline_ms`）；
  服务端在 250 ms 内读 HELLO（`IO_TIMEOUT`）。启动一个工作进程要付一次带 numpy 的 Python 启动开销（在这台主机上估计
  0.3–0.8 秒，待实测），够用但余量不大；工作进程和现在一样先发 CONFIG，再采样调色板。如果实测太紧，就由网关自己发 CONFIG
  之后再交接会话，或者每个型号常备一个热的备用工作进程。
- **重连顺序。** 设备可能在上一个会话还没关完时就重连。现在靠监听队列（`listen(8)`）留住它。工作进程维护一个只有一格的
  待接连接队列：新的胜出，旧的关掉。
- **准入控制。** 网关统计正在进行的视频会话数。超过全局上限（按 7.6 节的容量定）或每用户上限（默认 2）时，回 ERROR
  `{"reason":"server busy"}`，设备显示「服务器没有响应」并重试；管理员的设备优先。只听声音的会话按一个零头计。
- **被封禁或密钥不对。** 网关在 HELLO 之后直接关闭，不发 CONFIG，写审计记录，按设备 id 和 IP 限频。
- **未绑定设备**（Q8）由网关自己应答：CONFIG 带绑定地址，加一张由 `server/overlay.py` 画的静止画面（它的 5x7 字库要补上
  「SCAN TO BIND」用到的字母），配静音；不启动 ffmpeg。

### 7.4 型号配置

`/etc/ai-passport-tv/models.json`：

| model | 画面尺寸 | fps | `TV_RATE_START` | 依据 |
| --- | --- | --- | --- | --- |
| `xingzhi-cube-154` | 240x180 | 15 | 200000 | `tv-xingzhi.service` |
| `ai-passport` | 320x180 | 12 | 110000 | C3 解码不了比 110 kB/s 多多少的画面（`docs/boards/passport-muse-dual.zh_CN.md`） |

不认识的型号直接拒绝，不猜：尺寸不对会让设备上的每个会话都结束（`CONFIG field width: server=320 device=240`）。

### 7.5 中转按设备

- **设备登记表**代替 `--device-name` 和 `--extra-device`：`Relay.devices` 从数据库生成，每项带自己的目录
  `/var/lib/ai-passport-tv/devices/<id>/`，里面是 `now-playing.json`、`relay-events.json`、`relay-channels.txt`、
  `channel-groups.json`、`remote-url.txt`。工作进程的环境指向同样的文件，所以媒体服务那一侧的进程间通信不变。
- **流地址带上设备：** `STREAM_BASE` 改为 `http://127.0.0.1:8099/ch/<device_id>/`，`_CHANNEL` 接受
  `^/ch/([0-9a-f]{12})/(up\d\d|sr\d\d|cast)$`。中转由设备找到主人，从而知道用谁的 UP 主和进度、用哪个投屏位。
- **每路流的状态**（`_active`、`_paused`、`_bridge`、`_titles`）的键从 `频道` 改为 `(device_id, 频道)`。
- **生成频道表：** `write_table()` 为每台设备生成一张表：主人的 UP 主和剧集。`channel_rows()` 和 `groups()` 用主人的分组调整
  和隐藏设置。
- **共享来源缓存：** `refresh_if_stale()` 读写 `source_cache`，两个用户关注同一个 UP 主，每 30 分钟（`LIST_MAX_AGE_S`）
  只向 B 站要一次列表，而不是两次。B 站对云服务器地址的部分接口已经返回 412；用户多了不能让请求也跟着多。
- **YouTube 节点上限：** `server/relay/ytnode.py` 的 `MAX_STREAMS = 2` 是所有租户共用的。中转应当提示「YouTube 忙，
  稍后再试」，而不是让人久等；管理后台总览显示占用的名额。

### 7.6 容量

已实测或文档里有的数字：

- 星智一路流：画面最多 200 kB/s，声音约 8 kB/s，约 **1.7 Mbit/s**。AI Passport：120–150 kB/s，约 **1.0–1.2 Mbit/s**。
  只听声音：约 8 kB/s，**0.07 Mbit/s**（`docs/boards/xingzhi-cube-154.zh_CN.md`、`docs/boards/passport-muse-dual.zh_CN.md`）。
- 一路星智的 ffmpeg 在 2 核主机上约占一个核的 12 %（`docs/boards/xingzhi-cube-154.zh_CN.md` 的「云服务器」一节）。
- 现在的主机公网出方向 3 Mbit/s = 375 kB/s。

没有实测、标为**估计**的：

- 每路流 Python 那部分（调色板、差分、压缩、调度）：估计占一个核的 15–35 %。每路视频流合计，算上 ffmpeg 解码不同分辨率的源：
  **估计 0.3–0.5 个 vCPU**。
- 每个在播的工作进程的内存（排队的 16 秒画面、numpy、ffmpeg）：**估计 150–300 MB**（`tv-xingzhi.service` 的上限是 600 MB）。
- 只听声音的会话现在仍然解码画面（画面在发送时才丢掉，见 `_pace_live()` 里的 `device_audio_only`），所以省的是带宽，不是 CPU。
  只听声音时让 ffmpeg 不解码画面就能改掉这一点。

**现在的主机能服务多少：**

| 负载 | 带宽 | 结论 |
| --- | --- | --- |
| 1 台星智 | 3 Mbit/s 中用 1.7 | 没问题 |
| 1 台星智 + 1 台 Passport | 3 Mbit/s 中用 2.7–2.9 | 两台都降画质（已经看到过） |
| 2 台星智 | 需要 3.4，只有 3 | 放不下 |
| 只听声音 | 每台 0.07 | 带宽够几十台；CPU（在改掉之前估计每台 0.3 个 vCPU 以上）只够约 2 台 |

所以**无论软件怎么改，现在的主机只能满画质服务一台，或者两台都降画质。** `muse-asr`、nginx 和中转共用这一个 vCPU，
CPU 是第二道墙，大约也在两路（估计）。

**N 路视频同时播放时**（按星智计，留 20 % 余量；除每路带宽外都是估计）：

| 同时 N 路 | 公网出方向 | vCPU（媒体）+ 其余 1 | 内存 |
| --- | --- | --- | --- |
| 2 | 4 Mbit/s | 1 + 1 = 2 | 2 GB |
| 4 | 8–10 Mbit/s | 2 + 1 = 3–4 | 4 GB |
| 6 | 12–15 Mbit/s | 3 + 1 = 4 | 4 GB |
| 10 | 20–25 Mbit/s | 5 + 1 = 6–8 | 8 GB |

如果按流量计费：星智看一小时约 0.75 GB。4 台设备每天各看 3 小时，约每天 9 GB、每月约 270 GB（估计；请拿云厂商当前的每 GB
价格和固定带宽套餐比较）。

对 Q6 的建议（目标同时 4 台）：要么把现有主机升到约 4 vCPU / 4 GB，带宽 10 Mbit/s 或按流量计费并把峰值设在 20–30 Mbit/s；
要么另开一台同样规格的国内主机放网关和工作进程，现有主机留给网站、中转和 `muse-asr`。中转的拉流端口现在只听回环；
媒体单独放一台主机时，它要改听内网地址，并保持「不需要密钥、只对内网」的规则。

海外 YouTube 节点（1 核，`MAX_STREAMS = 2`）把所有用户同时看 YouTube 的路数限制在两路，它的内存上限也是在两路流 OOM
之后定的；看 YouTube 的人多了要换更大的节点。

**在定任何规格之前，** 先在现有主机上实测：用 `pidstat -u -r 5` 分别测一路星智直播、一路 UP 主视频、一路只听声音，各 10 分钟。

## 8. 安全

### 8.1 设备认证

- 每台设备的密钥（4.2 节）代替共用的配对 token。一个密钥泄露只影响一台设备，主人可以吊销。
- 首次使用即信任：某个 id 的第一个 HELLO 固定下它的密钥哈希。之后密钥不同的 HELLO 会被拒绝并标记。**重新配对**（主人或管理员）
  把设备设为 `repair` 状态 10 分钟，这个 id 的下一个 HELLO 固定新的密钥。`erase_flash` 的情况由此处理。
- id 不是秘密（它是 MAC）。知道某个 MAC 的人可以抢在真设备之前用自己的密钥连上来；他会占住一条 `unclaimed` 记录，
  但没看到设备屏幕就绑不了，管理员也可以删掉它。授予访问权的是绑定，不是第一次连接。
- 链路没有 TLS，和现在一样：密钥像现在的 token 一样明文传输。为防路径上的被动窃听，以后的固件可以改成挑战应答：
  服务端在 CONFIG 里放一个随机数，设备在下一个 HELLO 里发 `HMAC(密钥, 随机数)`（每次换台都是新的 HELLO）。前几个阶段不做。
- 现在 `TV_ALLOW_PUBLIC=1` 要求有 token；以后改为要求有 token 或设备数据库之一。

### 8.2 租户隔离

- 每个接口调用先把会话解析成一个用户（或访客的那一台设备），动设备前检查成员关系，只改该用户的状态。每个接口都写测试：
  用别的用户的设备 id 调用，预期 404（不是 403，和现在页面对待陌生请求的方式一致）。
- 每台设备的文件放在 `devices/<id>/`，只由中转和工作进程写，权限 0640，组 `tvserver`。
- 拉流端口仍只听回环；nginx 只转发网页端口。
- 用户给的网址不会进到服务器上的 ffmpeg（Q5）。UP 主、剧集、投屏链接和现在一样，先解析成网站的 id
  （`parse_creator_link()`、`parse_video_link()`、`youtube.py` 里的正则）再发任何请求。
- 管理接口在服务端检查角色，不能只靠页面。

### 8.3 会话

- 会话 token 256 位（和现在一样用 `secrets.token_urlsafe(32)`），存哈希。
- 手机 180 天，PC 30 天，访客 30 天，都可以吊销；封禁用户会吊销他的所有会话。
- 主屏幕地址里现有的 `?s=<会话>`（因为主屏幕应用有自己的 cookie）保留；`/tv/` 的访问日志保持关闭
  （`packaging/nginx/tv-relay.conf`）。
- 「换一个新的二维码」（`/api/code`、`State.new_code()`）现在会结束所有人的所有会话。改为按设备：只换这台设备的遥控码，
  只结束这台设备的访客会话。

### 8.4 限频

- nginx 对 `/tv/` 按 IP 做 `limit_req`：接口每秒 10 次、突发 20；登录请求的创建、`/TV/B/` 和 `/TV/R/` 每分钟 5 次
  （码有 100 位，限频是为了负载，不是防猜）。
- 每个用户：添加 UP 主和剧集每小时 20 次，投屏每小时 30 次（每次都要解析一次 B 站或 YouTube；YouTube 在节点上解析一次约 18 秒）。
- 网关：设备认证失败每个 IP、每个设备 id 每分钟 10 次，之后拒绝 1 分钟。
- 现有的上限保留：每个用户 12 个 UP 主、40 部剧（`MAX_CREATORS`、`MAX_SERIES`），50 条历史，每台设备的 CONFIG 大小。

### 8.5 秘密的保管

- `/etc/ai-passport-tv/` 里放中转密钥（管理员应急登录）、旧的配对 token（关掉之前）和 YouTube 节点配置（中转读的 JSON：
  地址、密钥、固定的证书）；权限 0640，组 `tvserver`，绝不进仓库（`AGENTS.md`）。
- 数据库里只存密钥、会话、邀请和登录请求的哈希。
- 日志里从不出现 token、密钥、会话 id 或完整 IP（`serve()` 日志现有的规则）；可以记设备 id。
- YouTube 的 cookies 文件只放在海外节点上。

## 9. 迁移与兼容

### 9.1 步骤（不丢数据）

1. **备份**：先不停任何服务；把 `/var/lib/ai-passport-tv`、`/var/lib/ai-passport-tv-passport` 和 `/etc/ai-passport-tv`
   复制成一个带日期的归档；列出文件大小和 SHA-256。
2. **对副本试跑**迁移脚本：它建 `tv.db`、管理员用户、两条 `legacy_port` 为 8098 和 8097 的 `device` 记录（设备报上真实 id 之前，
   id 先叫 `legacy-xingzhi`、`legacy-passport`），按 4.4 节导入 `relay-state.json` 和 `channel-groups.json`，把现有手机会话
   存成哈希（手机不用重新登录），并把现在的扫码码作为星智的遥控码（它屏幕上的二维码照常能用）。脚本打印两边的数目
   （UP 主、剧集、看过的标记、进度、历史、会话），必须一致。
3. **切换**：停 `tv-relay`、`tv-xingzhi`、`tv-passport`；对正式文件运行脚本；启动新的中转（从阶段 4 起还有网关）。
   旧文件不改也不删，它们就是回退的依据。
4. **回退**：停掉新服务，用原来的命令行启动旧单元。切换之后前进的进度在回退时会丢，除非导回去（脚本有 `--export` 模式，
   从数据库写出一个 `relay-state.json`）。
5. 脚本可重复执行（数据库非空时拒绝运行，除非明确要求替换），并有主机测试，用和现在 `relay-state.json` 同结构的样例文件
   （`tests/test_relay_state.py` 里有例子）。

### 9.2 已刷好的固件

- 旧固件发共用 token、不发 `device_id`。服务端（阶段 1 是固定单元，阶段 4 起是网关）只在 8098 和 8097 端口接受旧 token，
  并映射到该端口的 `legacy-*` 设备，归管理员所有。一切和现在一样。
- 某台设备刷了新固件、报上真实 `device_id` 后，管理员「收编」它：`legacy-*` 记录的状态（投屏位、遥控码）转到真实 id，旧记录退役。
- 连续 30 天没有设备用旧 token（管理页按 `fw` 和最后使用时间可以看到），管理员就把它关掉。
- 新固件连旧服务端也能用（HELLO 里不认识的键被忽略），所以固件可以先于服务端发布。

## 10. 分阶段计划

每个阶段都交付能用的东西。工作量是粗估，单位是「智能体·天」（一个智能体带主机测试干活），不含你的审阅和上机验证。

### 阶段 0 —— 决定与实测（0.5 天）

回答 Q1–Q8。实测每路流的 CPU 和内存（7.6 节）。定下服务端的正本仓库（Q7）。

### 阶段 1 —— 账号、设备绑定、按用户的中转状态，在现有主机上（6–8 天）

交付：邀请；多个用户各有自己的 UP 主、剧集、进度、历史和分组；扫码绑定设备；手机遥控只显示自己的设备；扫设备码的访客。

- 1a. 表结构、账号模块（`server/accounts/`：用户、会话、邀请、设备、审计）、带测试的迁移脚本。（2 天）
- 1b. 中转按用户：`State` 按用户从 SQLite 读；`Relay.devices` 来自数据库；每台设备的目录和频道表；流地址带设备 id；
  `_active` 等按设备分键；共享 `source_cache`。（2–3 天）
- 1c. 接口限定范围、检查成员关系；手机页只列自己的设备；绑定页 `/TV/B/<码>`；访客会话。（1–2 天）
- 1d. 固件：HELLO 里加 `device_id`、`model`、`device_secret`、`fw`；密钥存 NVS；`bound` 提示；主机测试。
  媒体服务：除旧 token 外也按设备数据库认证；`remote_url` 读这台设备的文件。（1–2 天）

媒体通路不变：两个固定单元，每种型号同一时间一台设备。

并行：1a 的表结构和接口约定写定后，1b、1c、1d 可以由三个智能体同时做；1d（固件）不碰其他人改的文件。

### 阶段 2 —— PC 控制台与扫码登录（3–4 天）

交付：`https://<site>/tv/console`，扫码登录，遥控、设备、订阅、剧集与分集、投屏、频道、带会话列表的设置。

- 2a. 登录请求、短码、确认页、长轮询、PC 会话。（1 天）
- 2b. 基于阶段 1 接口的控制台页面。（2–3 天）
- 2c. 手机页的「登录电脑」入口和会话列表。（0.5 天）

并行：2a 和 2b（2b 先用假的登录）；接口约定定下后，2b 在阶段 1 期间就可以开始。

### 阶段 3 —— 管理后台（3–4 天）

交付：`/tv/admin`，总览、用户、设备、直播频道、审计日志、系统设置。

- 3a. 指标：媒体服务扩展正在播放记录，`/proc` 采样。（1 天）
- 3b. 管理接口和页面。（2 天）
- 3c. 全局频道表编辑器，复用 `live.parse_channels()` 和 `tools/channel_config.py` 的检查。（1 天）

并行：3a、3b、3c 三个智能体；可以和阶段 2 重叠。

### 阶段 4 —— 媒体网关与每设备工作进程，连同主机升级（5–8 天）

交付：所有设备一个地址，多台设备同时播放且各用自己的配置，准入控制，未绑定设备不转码。

- 4a. `tv-gate`：HELLO、设备核对、型号配置、工作进程管理、传递文件描述符、准入控制、未绑定设备的静止画面。（2–3 天）
- 4b. `AVServer` 的工作模式（从网关接连接、空闲退出），现有 `tests/test_tv_server.py` 全部通过。（1–2 天）
- 4c. 只听声音的会话不解码画面。（1 天）
- 4d. 在新主机上用模拟设备做负载测试；systemd 单元；文档（`docs/boards/*.md`、`server/README.md`）。（1–2 天）

并行：4a 和 4b 按约定好的交接接口同时做；4c 独立。4a 只需要设备表，阶段 2 期间就能开始。

**合计约 18–25 智能体·天**，多个智能体并行加上审阅，大约 3–4 周。

## 11. 风险

| 风险 | 影响 | 对策 |
| --- | --- | --- |
| 带宽：3 Mbit/s 只够一路满画质 | 不升级就多租户，用户之间互相拖低画质 | 准入控制；阶段 4 前按 Q6 升级 |
| CPU 估计没有实测 | 规格可能差两倍 | 阶段 0 实测 |
| `muse-asr` 共用同一个 vCPU | 双方互相干扰 | 和现在一样用 `CPUQuota`/`Nice`；需要时媒体单独一台主机 |
| B 站限流和 412 随用户增多 | 所有人的 UP 主列表都取不到 | 共享 `source_cache`；每用户限频 |
| YouTube 节点：2 个推流名额，一个 cookies 账号 | 流量多了 YouTube「忙」或账号被要求验证 | 提示「忙」；换更大的节点；保留 6 小时一次的 cookies 检查 |
| 内容版权与许可 | 超出亲友范围就有法律风险 | Q1 选只凭邀请 |
| 旧固件和旧 token | 过渡期内共用的密钥仍然有效 | 旧 token 只在两个端口、映射到两条记录；30 天没人用就关掉 |
| 擦除 flash 会生成新的设备密钥 | 重新配对之前设备被拒 | 「重新配对」按钮；管理员可以接受 |
| cookie 不共享（相机、微信、主屏幕应用） | 扫码登录悄无声息地失败 | 可以输入的 6 位码 |
| SQLite 用法不当（长事务、两个写者） | 「database is locked」错误 | WAL、短事务、`busy_timeout`、每晚 `.backup` |
| 设备等 CONFIG 的 2 秒里工作进程启动太慢 | 空闲后第一次连接在设备端超时 | 实测；工作进程保留 10 分钟；每个型号一个热备进程，或由网关自己发 CONFIG |
| 重构中转（1000 行的 `web.py`） | 投屏、跳转、暂停出回归 | 现有中转测试保持全绿；按设备分键这一步先机械地改 |
| 服务端有两个仓库（Q7） | 改动进错地方 | 阶段 0 定下 |
| 设备链路明文 | 密钥可能在路径上被窃听 | 每台设备一个密钥限制损失；以后做挑战应答 |

## 12. 未验证

- 7.6 节里所有标为估计的 CPU 和内存数字。
- 现有主机上工作进程的启动时间。
- 同一台 AI Passport 上，Muse 固件和播放器用 `esp_efuse_mac_get_default()` 得到的 id 是否相同（预期相同：它读的是 eFuse，不是 NVS）。
- 微信开放平台和短信服务商目前的要求和价格；选 Q2 的 B 或 C 之前要核实。
- 每 GB 流量的价格；请看云厂商控制台。
