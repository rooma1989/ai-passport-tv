<p align="right">
  <strong>简体中文</strong> · <a href="tenancy-phase1-runbook.md">English</a>
</p>

# 多用户阶段 1：部署、迁移、回滚

[`2026-10-12-multi-tenant-design.md`](../superpowers/specs/2026-10-12-multi-tenant-design.zh_CN.md)
阶段 1 的运维手册：账号、邀请、设备绑定、按用户存的中转状态，仍在现在这台主机上。
`<site>` 指网站地址。这里的步骤都还没在云主机上跑过，只在本机用合成的状态完整走过一遍
（见文末「未验证」）。

## 改了什么

| 以前 | 现在 |
| --- | --- |
| 一个 `relay-state.json`：UP 主、剧集、进度、投屏、手机会话、一个扫码码 | `tv.db`（SQLite，WAL）：用户、会话（只存哈希）、邀请、设备、每个用户的状态、每台设备的投屏位、共享的视频列表缓存、操作记录 |
| 有密钥、设备二维码或登录过的手机，就能遥控所有设备 | 每个用户只看得到、遥控得了自己的设备；扫设备的二维码得到只管这一台的**访客**会话；密钥链接登录的是管理员 |
| `relay-channels.txt`、`channel-groups.json`、`now-playing.json`、`relay-events.json`、`remote-url.txt` 共用或每个单元一份 | 每台设备一套，在 `/var/lib/ai-passport-tv/devices/<设备 id>/` 下 |
| 所有设备用同一个配对令牌认证 | 新固件报 `device_id`（出厂 MAC）、`device_secret`、`model`、`fw`；旧固件的令牌按它连的端口对应到那台旧设备 |

不变的：媒体路径（两个静态单元，8098 的 `tv-xingzhi` 和 8097 的 `tv-passport`，各自一次一台）、
`channels.txt`（全局直播频道表，中转只读）、线上协议、手机遥控的网址。旧文件一个字节都不写：
它们就是回滚。

全部靠配置打开，而不是用新代码替换旧代码：中转不带 `--db`、电视服务不设 `TV_DB` 时和以前
完全一样。两种模式在同一份代码里，所以回滚不需要旧版本的代码。

## 数据模型

```
user ──< device_member >── device            （owner | member；admin、user）
 ├── session  （手机 180 天、电脑 30 天、访客 30 天；只存令牌的 SHA-256）
 ├── user_state  {creators[], history[], group_overrides{}}   ← relay-state.json 的格式
 └── invite   （邀请码的 SHA-256，7 天，只能用一次）
device_state  {cast: {video, position, done}}                 ← relay-state.json 的 "cast"
source_cache  (网站, 来源, 种类) → 视频列表                      订阅同一个来源的人共用
audit_log、meta（schema_version、public_url、legacy_token）
```

设备 id：迁移来的旧固件设备是 `legacy-xingzhi` / `legacy-passport`（`legacy_port` 8098 / 8097），
新固件是 12 位十六进制的 MAC。新设备第一次连上来时存为 `unclaimed`，记下密钥的 SHA-256
（首次信任）和 20 位绑定码；绑定后变成 `active`、有主人。`retired` 是已被接收（adopt）的旧记录。

## 0. 准备（不停服）

1. 照现在的方式把新的 `server/` 复制到 `/opt/ai-passport-tv/`（最好留一份旧的，例如
   `/opt/ai-passport-tv/server.prev`；下面的回滚其实用不到它）。
2. 看一下主机上 Passport 单元的实际配置——它是手工复制的，不在仓库里：

   ```sh
   systemctl cat tv-passport | grep -E 'TV_|ReadWritePaths|ExecStart'
   ```

   记下它的数据目录（迁移的 `--passport-dir`，一般是 `/var/lib/ai-passport-tv-passport`）。
3. 备份（设计 9.1 第 1 步）：

   ```sh
   stamp=$(date +%Y%m%d-%H%M%S)
   tar -C / -czf /root/tv-backup-$stamp.tgz var/lib/ai-passport-tv var/lib/ai-passport-tv-passport etc/ai-passport-tv
   find /var/lib/ai-passport-tv /var/lib/ai-passport-tv-passport -maxdepth 1 -type f -exec sha256sum {} + > /root/tv-backup-$stamp.sha256
   ```

## 1. 演练（不停服）

用服务账号运行，之后它建的文件属主才对：

```sh
cd /opt/ai-passport-tv
sudo -u tvserver python3.11 -m server.tenancy.migrate \
    --data-dir /var/lib/ai-passport-tv --passport-dir /var/lib/ai-passport-tv-passport --dry-run
```

它用和正式迁移同一段代码，在临时目录里建一个库，打印两边的计数，然后删掉。每一行都应该相等：

```
                        旧文件  数据库
UP 主                   2       2
剧集                    1       1
视频（列表里）          16      16
看过的标记              3       3
播放进度                2       2
投屏历史                1       1
手机会话                2       2
投屏位                  1       1
分组调整                2       2
扫码码保留              1       1
直播台（channels.txt）  3       3
...
旧文件没有改动。
演练完成：什么都没写。
```

有「不一致」、「旧文件被改动了」或退出码不是 0，就停在这里。没有 Passport 单元时加 `--no-passport`。

## 2. 切换（停服几分钟）

```sh
systemctl stop tv-relay tv-xingzhi tv-passport
cd /opt/ai-passport-tv
sudo -u tvserver python3.11 -m server.tenancy.migrate \
    --data-dir /var/lib/ai-passport-tv --passport-dir /var/lib/ai-passport-tv-passport \
    --public-url https://<site>
```

库里已有数据时它拒绝运行（`--replace` 会先把旧库改名为 `tv.db.bak-<时间>`）。它写出 `tv.db`，
以及 `devices/legacy-xingzhi/`、`devices/legacy-passport/`：每台的频道表、分组、二维码地址，
再复制一份 `now-playing.json` 和 `relay-events.json`。原来的扫码码成为小智的遥控码，状态页和
打印出来的二维码照样能扫；已登录的手机保持登录（身份是管理员）。

装上 drop-in（模板在 `packaging/systemd/tenancy/`；替换 `<site>`，并对照主机上的基础单元
核对路径和 Python 可执行文件）：

```sh
mkdir -p /etc/systemd/system/tv-relay.service.d /etc/systemd/system/tv-xingzhi.service.d /etc/systemd/system/tv-passport.service.d
cp packaging/systemd/tenancy/tv-relay.conf /etc/systemd/system/tv-relay.service.d/tenancy.conf
cp packaging/systemd/tenancy/tv-media.conf /etc/systemd/system/tv-xingzhi.service.d/tenancy.conf
cp packaging/systemd/tenancy/tv-media.conf /etc/systemd/system/tv-passport.service.d/tenancy.conf
systemctl daemon-reload
```

按 [`packaging/nginx/tv-relay.conf`](../../packaging/nginx/tv-relay.conf) 给 nginx 加上新的
`/TV/B/` 和 `X-Real-IP`，`nginx -t` 后重载。

启动并检查：

```sh
systemctl start tv-relay tv-xingzhi tv-passport
journalctl -u tv-relay -n 5        # 「视频中转已启动（多用户）… 1 个用户，2 台设备」
python3.11 -m server.tenancy.admin --db /var/lib/ai-passport-tv/tv.db devices
```

- 用以前登录过的手机打开遥控：两台设备、UP 主、剧集和投屏历史都在。换个台试试。
- 在任一台设备上换台：电视服务的日志有 `device=legacy-xingzhi legacy`（或 `legacy-passport`）。
- 用**没登录过**的手机扫小智状态页的二维码：打开的是只管这一台的访客遥控（没有「订阅」页）。

## 3. 日常使用

- **邀请家人。** 管理员手机上：订阅 → 我的设备 → 邀请；或者
  `python3.11 -m server.tenancy.admin invite --name 妈妈`。把链接
  （`https://<site>/tv/i/<码>`，7 天内有效、只能用一次）发过去；对方打开、点「接受邀请」，
  这台手机就以新用户身份登录，名下还没有设备。
- **绑定新设备。** 刷新固件（公开版本即可，不需要令牌）。连上后它存为未绑定，屏幕上是
  「BIND THIS TV」和 20 位绑定码的静止画面，状态页的二维码变成 `HTTPS://<SITE>/TV/B/<码>`。
  主人用已登录的手机扫码确认，或在「订阅 → 我的设备」里输入绑定码。设备两秒左右重连，
  播主人的台。
- **旧设备刷了新固件**：只要它的固件里还有配对令牌，就继续按旧记录服务（同时登记了它的真 id）。
  把状态搬到真 id 上：

  ```sh
  python3.11 -m server.tenancy.admin devices          # 找到新的 12 位 id
  python3.11 -m server.tenancy.admin adopt legacy-xingzhi <id>
  ```

  主人、遥控码（打印的二维码）、投屏位和名字都搬过去；旧记录退役，这个端口上的令牌不再有效。
- **擦过 flash（`erase_flash`）以后**设备换了新密钥，会被拒绝。主人运行
  `python3.11 -m server.tenancy.admin repair <id>`，或调用 `POST /api/devices/<id>/repair`；
  10 分钟内的下一次连接会记住新密钥。
- **关掉共享令牌**：所有设备 30 天没用过它以后（`admin devices` 里看旧记录的最后连接时间）：
  `python3.11 -m server.tenancy.admin legacy-token off`。
- 其他命令：`users`、`unbind`、`ban-device`、`unban-device`、`ban-user`、`unban-user`、`audit`。
  中转 10 秒内会跟上数据库的改动。

## 4. 备份

```sh
sqlite3 /var/lib/ai-passport-tv/tv.db ".backup /var/backups/tv-$(date +%Y%m%d).db"
find /var/backups -name 'tv-*.db' -mtime +14 -delete
```

每晚一次（cron 或 systemd timer），迁移、升级前也做一次。只用 `.backup` 复制：直接复制 WAL
模式的数据库文件可能漏掉最近的写入。

## 5. 回滚

旧文件从没被写过，回滚就是「把配置改回去」。切换以后的进度和投屏，不先导出就会丢。

```sh
cd /opt/ai-passport-tv
sudo -u tvserver python3.11 -m server.tenancy.migrate --data-dir /var/lib/ai-passport-tv \
    --export /var/lib/ai-passport-tv/relay-state.export.json     # 可选：留住新的进度
systemctl stop tv-relay tv-xingzhi tv-passport
rm /etc/systemd/system/tv-relay.service.d/tenancy.conf \
   /etc/systemd/system/tv-xingzhi.service.d/tenancy.conf \
   /etc/systemd/system/tv-passport.service.d/tenancy.conf
systemctl daemon-reload
# 只有导出了、又想要新的进度时：
#   cp -p relay-state.json relay-state.json.pre-rollback && cp relay-state.export.json relay-state.json
systemctl start tv-relay tv-xingzhi tv-passport
```

导出的是管理员的 UP 主、剧集、进度、投屏历史，以及小智那台的投屏位；会话和扫码码取自旧文件，
手机不用重新扫码。切换后新加的用户、他们的订阅、新绑定的设备只在 `tv.db` 里——留着它；以后
再迁移时加 `--replace`。新固件连旧服务器也能用（旧服务器忽略 HELLO 里的新键），前提是固件里有
配对令牌；公开版本没有令牌，连不上旧服务器。

## 两种模式下的语音控制

按住说话（[`server/README.zh_CN.md`](../../server/README.zh_CN.md#语音控制)）两种中转都能用。先在单用户模式
下打开、跑稳；上面的切换对语音什么都不用改。

**A. 语音，单用户（切换之前）。**

1. muse-asr 开 Unix 套接字：`MUSE_ASR_SOCKET=/run/muse-asr/asr.sock`、`MUSE_ASR_SOCKET_GROUP=tvserver`、
   `RuntimeDirectory=muse-asr`、`RuntimeDirectoryMode=0755`、`SupplementaryGroups=tvserver`。检查：
   `sudo -u tvserver curl --unix-socket /run/muse-asr/asr.sock http://x/health`。
2. tv-relay：只换新代码，不加参数。`--device-name` 那台是 `d0`，每个 `--extra-device` 依次是 `d1`、`d2`…
   （也认它的名字）。
3. 媒体单元（基础 unit，不是 drop-in）：

   | 单元 | 加上 |
   | --- | --- |
   | `tv-xingzhi`（模板在 `packaging/systemd/`） | `TV_ASR_SOCKET=/run/muse-asr/asr.sock`、`TV_RELAY_KEY_FILE=/etc/ai-passport-tv/relay-key`、`TV_RELAY_DEVICE=d0`；`After=` 加上 `muse-asr.service` |
   | `tv-passport`（主机上手写的） | 同样的套接字和密钥文件，`TV_RELAY_DEVICE=Passport`（tv-relay 的 `--extra-device` 里的名字，或 `d1`）；`After=muse-asr.service tv-relay.service`；它的用户要在 `tvserver` 组里、能读密钥文件 |

   `#Environment=TV_VOICE_LOG_TEXT=1` 平时保持注释，只在调阈值时打开。
4. `systemctl daemon-reload`，依次重启 muse-asr、tv-relay、两个媒体单元。每个媒体单元记一行
   `voice: on, relay on, transcripts not logged`。在剧集台按住 OK 说「暂停」：这台暂停，另一台不动。

**B. 切换之后（多用户）。** `packaging/systemd/tenancy/` 的 drop-in 只加 `TV_DB`，不碰语音的变量：什么都不用改。
设了 `TV_DB` 时媒体服务不看 `TV_RELAY_DEVICE`（记一次 `voice: TV_RELAY_DEVICE ignored with a device
database …`，然后 `voice: on, relay per device`），每次调中转都点名这次会话认出的设备：旧固件或还没接收的新固件
是 `legacy-xingzhi` / `legacy-passport`，接收（`admin adopt`）或绑定后是 12 位 MAC。中转把带密钥、在单设备接口上
点名设备的请求当作那台设备的访客，所以能动任何用户已绑定的那台，别的都动不了；未绑定的设备没有语音。
`TV_RELAY_DEVICE` 留在基础 unit 里：回滚（删 drop-in）时单用户模式连同这个对应关系原样回来。

切换后检查：每台设备上说一个台（「CCTV 5」），在剧集台上说「暂停」。媒体日志先有 `device=legacy-…`，再有
`voice id=… intent=…`；不应出现 `relay_catalog=relay status 404`。

## 排查

| 现象 | 看哪里 |
| --- | --- |
| 中转退出，说「还没有管理员」 | 这个 `--db` 还没迁移过。 |
| 日志里有 `database is locked` | 有东西开着长事务（比如开着的 `sqlite3` 命令行），关掉它。 |
| 电视服务日志 `device refused: … secret mismatch` | 设备擦过 flash，或者是冒充的：是自己的就 `admin repair <id>`。 |
| 电视服务日志 `pairing token on port …, which has no legacy device` | 这个端口的旧设备已被接收（退役），或者没迁移。 |
| 已经绑定了，设备还显示绑定画面 | 绑定后 2 秒内会重连；没有的话看 `admin devices` 和中转日志里的「写设备文件失败」。 |
| Passport 单元写不了 `devices/` | 它的 drop-in 少了 `ReadWritePaths=/var/lib/ai-passport-tv`。 |
| 电视服务日志 `voice: relay disabled (PermissionError)` | 它的用户读不了 `TV_RELAY_KEY_FILE`。 |
| 语音说「这台电视没有连到视频中转」 | 没设 `TV_RELAY_KEY_FILE`；或者（只在单用户时）没设 `TV_RELAY_DEVICE`。 |
| 媒体日志有 `relay_catalog=relay status 404` | 单用户：`TV_RELAY_DEVICE` 不是中转里设备的 id 或名字。多用户：认出的设备不是 active（未绑定、退役、封禁）。 |

## 留给以后的阶段

电脑控制台和扫码登录（阶段 2，`login_ticket` 表已经建好）；每个用户隐藏、排序直播台和提议新台
（阶段 2；`user_state` 给 `hidden`/`order` 留了位置）；主人以外的设备成员（表已建好，没有界面）；
管理网页和指标（阶段 3）；媒体网关、每台设备一个工作进程、型号配置和准入控制（阶段 4）。

## 未验证

- 这些步骤没在云主机上跑过；仓库里没有 Passport 单元的实际路径。
- 固件的身份部分（HELLO 的新键、NVS 里的密钥）两块板子都编译通过，但没上设备测：
  `esp_efuse_mac_get_default()` 在 Muse 固件下给出同一个 id、AI Passport 切换固件后密钥还在，
  都是预期而不是实测。
- 绑定画面和 `bound: 0` 只对着服务器测过；两种固件在屏幕上怎么画这张画面没在设备上看过。
- 手机上的流程是在桌面浏览器里按手机宽度、对着本机中转和合成状态检查的，没在 iPhone 主屏幕
  应用或微信里试过。
