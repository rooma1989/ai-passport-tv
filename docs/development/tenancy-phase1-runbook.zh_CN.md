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
- 用**没登录过**的手机扫小智状态页的二维码：打开的是只管这一台的访客遥控（只有「遥控」和「投屏」）。

## 3. 日常使用

- **邀请家人。** 管理员手机上：我的 → 邀请家人；或者
  `python3.11 -m server.tenancy.admin invite --name 妈妈`。把链接
  （`https://<site>/tv/i/<码>`，7 天内有效、只能用一次）发过去；对方打开、点「接受邀请」，
  这台手机就以新用户身份登录，名下还没有设备。
- **绑定新设备。** 刷新固件（公开版本即可，不需要令牌）。连上后它存为未绑定，屏幕上是
  「BIND THIS TV」和 20 位绑定码的静止画面，状态页的二维码变成 `HTTPS://<SITE>/TV/B/<码>`。
  主人用已登录的手机（遥控右上角的扫一扫）扫码确认，或在「我的 → 绑定新设备」里输入绑定码。设备两秒左右重连，
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
   `RuntimeDirectory=muse-asr`、`RuntimeDirectoryMode=0755`、`SupplementaryGroups=tvserver`（drop-in
   [`packaging/systemd/muse-asr.service.d/tv-voice.conf`](../../packaging/systemd/muse-asr.service.d/tv-voice.conf)），
   内存紧的主机再加预热定时器 [`muse-asr-warm.timer`](../../packaging/systemd/muse-asr-warm.timer)。检查：
   `sudo -u tvserver curl --unix-socket /run/muse-asr/asr.sock http://x/health`。
2. tv-relay：只换新代码，不加参数。`--device-name` 那台是 `d0`，每个 `--extra-device` 依次是 `d1`、`d2`…
   （也认它的名字）。
3. 媒体单元：各加一个 `voice.conf` drop-in，和 `tenancy.conf` 并列（不写进它）：

   | 单元 | drop-in |
   | --- | --- |
   | `tv-xingzhi` | [`tv-xingzhi.service.d/voice.conf`](../../packaging/systemd/tv-xingzhi.service.d/voice.conf)：`TV_ASR_SOCKET=/run/muse-asr/asr.sock`、`TV_RELAY_KEY_FILE=/etc/ai-passport-tv/relay-key`、`TV_RELAY_DEVICE=d0`；`After=muse-asr.service tv-relay.service` |
   | `tv-passport` | [`tv-passport.service.d/voice.conf`](../../packaging/systemd/tv-passport.service.d/voice.conf)：同样的套接字和密钥文件，`TV_RELAY_DEVICE=Passport`（tv-relay 的 `--extra-device` 里的名字，或 `d1`）；它的用户要在 `tvserver` 组里、能读密钥文件 |

   `#Environment=TV_VOICE_LOG_TEXT=1` 平时保持注释，只在调阈值时打开。
4. `systemctl daemon-reload`，依次重启 muse-asr、tv-relay、两个媒体单元。每个媒体单元记一行
   `voice: on, relay on, transcripts not logged`。在剧集台按住 OK 说「暂停」：这台暂停，另一台不动。

**B. 切换之后（多用户）。** `packaging/systemd/tenancy/` 的 drop-in 只加 `TV_DB`，不碰语音的变量：什么都不用改。
设了 `TV_DB` 时媒体服务不看 `TV_RELAY_DEVICE`（记一次 `voice: TV_RELAY_DEVICE ignored with a device
database …`，然后 `voice: on, relay per device`），每次调中转都点名这次会话认出的设备：旧固件或还没接收的新固件
是 `legacy-xingzhi` / `legacy-passport`，接收（`admin adopt`）或绑定后是 12 位 MAC。中转把带密钥、在单设备接口上
点名设备的请求当作那台设备的访客，所以能动任何用户已绑定的那台，别的都动不了；未绑定的设备没有语音。
`TV_RELAY_DEVICE` 留在 `voice.conf` 里：回滚（删 `tenancy.conf` 这几个 drop-in）时单用户模式连同这个对应关系原样回来。

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

## 阶段 2：电脑版

电脑版是多用户中转的一部分，没有新的单元、端口或参数。单用户中转（没有 `--db`）对电脑版的所有
路径都回答 404：先切到 `--db`。上线步骤：

1. 按第 0 步复制新的 `server/`。中转第一次启动时给 `tv.db` 加一列（`login_ticket.poll_sha256`，
   `ALTER TABLE ... ADD COLUMN`），库里别的都不动，旧代码照样能用升级后的文件。保险起见先按第 4 节备份。
2. nginx：加上 [`packaging/nginx/tv-relay.conf`](../../packaging/nginx/tv-relay.conf) 里的 `/TV/L/`，
   `nginx -t` 后重新加载。`/tv/console` 已经在 `/tv/` 里了。
3. `systemctl restart tv-relay`。电视服务不用动。

检查：在电脑上打开 `https://<site>/tv/console`，屏幕上有二维码和 6 位登录码。用已登录的手机扫码
（打开的浏览器没登录时，页面会让你改用桌面图标），或者打开遥控「订阅 → 登录电脑版」输入登录码；
看一眼显示的浏览器和网段，点确认。电脑一两秒内就登录了。它的「设置」页列出这次登录，「退出登录」结束它。

工作方式和限制：

| | |
| --- | --- |
| 路径 | 页面 `/tv/console`；扫码落地和手机确认页 `/TV/L/<票据>`（中转里是 `/l/<票据>`）和 `/tv/login`（输入登录码、已登录的电脑）；接口 `POST /api/login/ticket`、`POST /api/login/poll`、`GET /api/login/info`、`POST /api/login/confirm`、`GET /api/login/status`、`GET /api/sessions`、`POST /api/sessions/revoke`、`POST /api/logout`、`GET /api/console/devices`、`GET /api/invites`（管理员）、`POST /api/qr`；`GET /api/state?device=<id>` 只看某一台，不改手机选的那台 |
| 票据 | 26 位 id（130 位）只在二维码里，6 位登录码（去掉 I、L、O、0、1）在屏幕上，另一个 256 位的轮询密钥只在页面里；除登录码外都只存 SHA-256。2 分钟过期，只能用一次；电脑每次长轮询最多等 25 秒 |
| 谁能确认 | 用户已登录的手机或电脑会话（cookie）；访客和密钥都不行。确认页显示电脑的浏览器和 /24 网段，和手机不在同一网段时提醒 |
| 限流 | 每个网段（/24 或 /48）10 分钟最多 12 张票据；每个用户、每个网段 10 分钟最多输错 10 次登录码；同时最多 100 张有效票据、32 个挂着的长轮询（多出来的立刻回答，页面再来） |
| 电脑会话 | 30 天，cookie `tvs`，`HttpOnly; Secure; SameSite=Lax; Path=/tv/`；「设置」里和手机、你设备上的访客列在一起；「退出其他所有登录」只留下当前这个 |
| 跨站请求 | 所有 POST 和 DELETE（两种模式都是）在浏览器表明来自别的网站时（`Sec-Fetch-Site: cross-site`/`same-site`，或 `Origin` 的主机和 `Host` 不同）一律 404；没有这两个头的请求（电视服务、curl）不受影响 |
| 页面 | `Content-Security-Policy` 不允许任何外部来源，`frame-ancestors 'none'`，`X-Frame-Options: DENY`；二维码由中转自己画（`server/relay/qr.py`，移植自固件用的同一个库） |

管理员的密钥链接仍是应急登录：在电脑上打开 `https://<site>/tv/?k=<密钥>`，再打开 `/tv/console`。
电脑版的「邀请」页（只有管理员看得到）发的邀请链接和手机上的一样；完整的管理后台是 `/tv/admin`（阶段 3，见下）。

电脑版里还没有：每个用户隐藏、排序直播台和提议新台；主人以外的设备成员（电脑版显示设备的访客码，
就是现有的访客链接）。

### 遥控里扫码登录电脑，和让手机登录

后来加的两项，和阶段 2 其余部分一样部署（复制 `server/` 和 `packaging/help/`，重启 `tv-relay`）：

- **扫码登录电脑**（遥控里）：「我的」里有个按钮（「遥控」页右上角的扫一扫也认这个码），打开手机相机（`/tv/scan.js`；浏览器没有
  `BarcodeDetector` 时再加上随附的 jsQR `/tv/jsqr.js`，都只在点按钮后加载），认出后在遥控所在的网站上
  打开 `/tv/l/<票据>`。票据全中转通用，主站上的遥控能确认显示 `tv.` 子域名的二维码，反过来也一样。
  nginx 不用改。
- **让手机登录**（已登录的电脑上，「设置」里或「遥控」页的提示；或已登录的手机上，遥控「我的 → 让另一部手机登录」，整屏显示）：把 `https://<site>/TV/P/<令牌>`
  显示成二维码，外加 8 位码；手机相机打开链接，页面显示是谁的账号、哪台电脑或手机发起的，点一下这台手机就得到
  同一个用户普通的手机会话（成员，180 天；然后在遥控页「添加到主屏幕」，网址里带着会话）。只打开页面
  不会用掉链接（链接预览只会 GET）。二维码用 `--public-url`（`tv.` 子域名），但页面和接口在两个网站上
  都能用。**nginx：**加上 [`packaging/nginx/tv-relay.conf`](../../packaging/nginx/tv-relay.conf) 里的
  `/TV/P/`，`nginx -t` 后重新加载。输入 8 位码的 `/tv/p` 已经在 `/tv/` 里。中转第一次启动时建
  `phone_link` 表。

| | |
| --- | --- |
| 路径 | 落地页 `/TV/P/<令牌>`（中转里是 `/p/<令牌>`）和 `/tv/p`；接口 `POST /api/phone-link`（电脑或手机）、`GET /api/phone-link/status?poll=`（同一个会话）、`GET /api/phone-link/info?t=` 或 `?code=`、`POST /api/phone-link/accept` `{token}` 或 `{code}` |
| 链接 | 26 位令牌（130 位，大写 base32）只在二维码里，8 位码（去掉 I、L、O、0、1，约 40 位）在屏幕上，另一个 256 位的轮询密钥只在页面里；三样都只存 SHA-256。5 分钟过期，只能用一次；同一个用户再要一个，旧的作废；用户被封后链接失效 |
| 谁能要 | 成员已登录的电脑或手机会话（cookie）；访客、密钥都不行 |
| 限流 | 每个用户 10 分钟 6 个、每个网段 12 个；输错码每个网段 10 分钟 10 次、全中转 100 次 |
| 审计 | `phone_link.create`（detail 以 `pc` 或 `phone` 开头，是发起的会话）和 `login.phone.link` |

## 没有登录时怎么进来 / 恢复登录

手机上的登录会丢（换了手机、清了 Safari 数据、删了桌面图标），邀请又要管理员。下面三条路都以一个
普通的成员手机会话（180 天）结束；前两条以设备本身为信任的根：只有站在它跟前的人才看得到它的屏幕、
按得到它的键。部署：复制 `server/` 和 `packaging/help/`，重启 `tv-relay` 和两个电视服务
（`tv-xingzhi`、`tv-passport`）；nginx 不用改（`/TV/B/`、`/TV/P/` 已经有了）。中转第一次启动时给
`phone_link` 加 `origin`、`device_id`，给 `session` 加 `via`（旧行当作 `pc` / 普通登录）。设备一侧要新固件；
旧固件只是没有这个手势。

**1. 用未绑定的设备自己注册（不用邀请）。** 没登录（或只是访客）的手机打开设备的绑定链接 `/TV/B/<码>`
（「BIND THIS TV」静止画面上的码，或状态页的二维码）。页面给「创建账号并绑定这台设备」，只问一个名字
（默认「我」）；点一下就建一个普通用户、设备归他、这台手机得到成员手机会话，跳到遥控页并提示「添加到主屏幕」。
已登录的手机还是原来的「绑定到我的账号」。绑定码照旧只能用一次（绑定后清掉）。

**2. 设备上的「恢复登录」。** 在设备的状态页上：

| 板子 | 手势 |
| --- | --- |
| 小智（星智） | 同时按住 ▲ 和 OK 1.5 秒，或快速连按 5 下 OK |
| AI Passport | 快速连按 5 下 OK（三个键共用一个 ADC 分压、▲ 是 0 Ω，两个键一起按只读得出 ▲） |

「快速」是按键驱动的规矩：第一下以后每一下按住、每个间隔都不到 180 毫秒；两组之间停顿到 0.4 秒左右
仍算一次（2 + 3 = 5）。因为状态页上的 OK 单击要等次数数清楚才处理，那里的 OK（只听声音）现在松开后约
0.6 秒才生效（以前约 0.2 秒），Passport 的双击 OK 回 Muse 也一样；没按完的尝试（4 下、按慢了一下）什么也不做。
长按 ▲（配网）、长按 ▼（宠物）、长按 OK（关闭）不变；一起按住时，后一个键自己的长按或单击会被吞掉。
手势在 `components/bsp/include/boards/*.h` 里选（`BSP_RECOVER_CLICKS`、`BSP_RECOVER_CHORD`）。

设备在已认证的会话上发 `STATUS {"recover": 1}`；电视服务给主人要一个一次性链接（`phone_link`，
`origin=device`），回 `SWITCH {"recover": {"url": "HTTPS://<网站>/TV/P/<令牌>", "ttl": 300}}`。设备全屏显示
二维码（一般的网站是第 3 版、每模块 4 像素；服务端保证网址不超过 96 个字符，第 4 版以内）、「用手机相机扫码 /
以主人身份登录」和倒计时；任意键关闭。任何手机相机扫了都打开「让手机登录」同一个落地页，上面写着
「小电视「…」上的恢复登录」，点按钮就以主人身份登录。新会话记作 `via=recover`：主人别的登录会提示一次
「有新手机通过设备恢复登录」（电脑版打开时、遥控下次刷新时），电脑版登录列表里那一行标着「设备恢复登录」，
可以退出它。未绑定的设备显示它的绑定码（第 1 条路）。服务端拒绝时屏幕上说原因：一小时 3 次用完了、
没有主人、服务器太旧或没配 `--public-url`、没联网。

**3. 管理员的最后办法。** 在主机上：

```
sudo -u tvserver python3 -m server.tenancy.admin --db /var/lib/ai-passport-tv/tv.db users
sudo -u tvserver python3 -m server.tenancy.admin --db /var/lib/ai-passport-tv/tv.db login-link <用户 id>
```

打印 `HTTPS://<网站>/TV/P/<令牌>`（5 分钟、一次）；发给本人，在手机上打开、点按钮。总后台的「用户」页上
有同样的按钮（阶段 3，见下）。

| | |
| --- | --- |
| 路径 | `GET /api/bind/<码>` 对没登录的手机回答 `register: true`；`POST /api/bind/register` `{claim, name}`；落地页和 `/api/phone-link/*` 同上，`info` 多了 `origin`、`device_name`；`/api/login/status`、`/api/state` 多了 `notice`；`/api/sessions` 每行带 `via` |
| 协议 | HELLO `"recover": 1`；CONFIG `"recover": 1`（多用户、已绑定、知道网站地址）；STATUS `{"recover": 1}`；SWITCH `{"recover": {"url", "ttl"}}` 或 `{"recover": {"err": "limited" \| "unbound" \| "off" \| "fail"}}` |
| 谁能 | 注册：拿着未绑定设备绑定码的人，已登录的成员不行；恢复：电视服务替它按密钥认过的设备要，只在已绑定、主人没被封时；login-link：能动数据库的 shell |
| 限流 | 注册每个网段每小时 3 个账号、全中转 30 个（建成才算，在中转内存里）；恢复每台设备每小时 3 个链接（按库里的记录算，重启不清零）；同一台设备新要的让没用掉的作废；链接 5 分钟、一次；设备和管理员的链接没有 8 位码 |
| 审计 | `user.signup`、`device.bind`；`phone_link.device`、`login.phone.recover`；`phone_link.admin`、`login.phone.admin` |
| 日志 | 从不写令牌：电视服务只记 `recover link issued for user N` 或拒绝的原因 |

## 阶段 3：总后台

`https://<网站>/tv/admin`，只给管理员：电脑版（名字下面）和手机遥控「我的」（「邀请家人」下面）各有一个入口。
别的会话、访客、没登录的打开页面会被送回 `/tv/console`，每个 `/api/admin/*` 接口对他们都回 404；
密钥（`X-Key` 或 `?k=` 登录）算管理员。POST 和电脑版一样过同源检查，每个改动都写进操作记录，
记下管理员和他的 /24 网段。

| 页面 | 做什么 |
| --- | --- |
| 概览 | 设备在线/离线、主人、在播的台和标题、这次会话多久；最近约 10 秒发出的字节、每秒画面数、字节率目标（来自电视服务的「正在播放」文件）；主机 CPU、内存、出站流量的现值和 24 小时曲线；电视服务、中转、ffmpeg、muse-asr、nginx 几个进程；YouTube 节点状态和在放几路（共 2 路） |
| 用户 | 怎么来的（邀请、自己注册、最早的账号）；自己注册的标「待查看」，点「知道了」消失；改名；封禁/解封；删除（输入名字确认，先导出）；一次性登录链接和二维码（5 分钟、一次，和命令行 `login-link` 一样）；他的登录（含他设备上的访客），可以退出；邀请 |
| 设备 | 所有设备：状态、主人、固件、最后连接和网段；重连、改名、重新配对、解绑、封禁/解封 |
| 频道表 | 全局 `channels.txt` 的表格：改名、调分组、调顺序、删除、新加；「检查」和「保存」（见下） |
| 操作记录 | 从新到旧，按用户、设备、操作筛，每次 50 条 |
| 系统 | 注册方式和未绑定设备的规矩、限额、登录有效期（只读，写在代码里）、共享配对令牌开关、数据库和频道表文件的情况 |

各个操作做了什么：

- **封禁用户**：他的登录马上删掉；他是主人的设备收到 `reconnect` 事件（电视服务结束会话），只要主人还在封禁中，
  下次 HELLO 就被拒绝（`gate.py`）。数据和绑定都保留；解封后恢复，他要重新登录（恢复登录、登录链接、邀请）。
  不能封禁、删除自己和最后一个管理员。
- **删除用户**：要输入他的名字。导出（用户、`user_state`、设备、登录（不含令牌）、邀请、和他有关的操作记录）
  写到数据库旁边的 `exports/user-<id>-<时间>.json`（0640），页面也下载一份；然后他的设备解绑（新的绑定码，
  访客退出），他的登录、链接、状态、成员关系删掉。他发出的邀请保留；他接受过的邀请改成过期。有旧固件设备的用户
  不能删（先 adopt）。
- **设备重连、封禁、解绑**：中转往设备的 `relay-events.json` 写一个 `reconnect` 事件；声明了 `switch` 的固件
  一两秒内结束会话、重新连上（更旧的固件不认，忽略）。
- **频道表**：id 永远不变（用户的分组、设备记住的上一个台都按 id 记）；新台取下一个没用过的 `chNNN`。保存时
  查名字和地址、每个设备主人的频道列表放不放得下（他的 UP 主和剧集加上这张表，和加 UP 主时一样留 512 字节余量），
  最后在子进程里跑电视服务自己的 `live.parse_channels()`（`python3 -m server.relay.channeltable --check`；用子进程
  是为了中转不必一直背着 `server.live` 和 numpy）。页面打开之后文件被别处改过就拒绝。写之前把原文件复制成同一目录下的
  `channels.txt.bak-<日期>-<时间>`（留最新 30 份）。电视服务每个新会话开始时重读这个文件，所以**不用重启**：
  每台设备下次换台或重连时拿到新表。这里设的分组是这个台给所有人的默认分组（存在数据库 `meta.channel_groups`）；
  用户在手机上自己调的盖在它上面。

部署：复制 `server/`（中转和电视服务都改了）和 `packaging/help/`（使用说明加了一节），重启 `tv-relay`、`tv-xingzhi`、`tv-passport`。nginx 不用改
（`/tv/admin` 在 `/tv/` 下面；页面把没改的台只发 id，每个请求都在 nginx 的 8 KB 请求体限制以内）。中转第一次
启动时加上 `user.reviewed_at` 列。中转的用户要能写 `--main-channels` 所在目录（备份和整份替换）和数据库所在目录
（`exports/`）；模板里两个都是 `/var/lib/ai-passport-tv`。不重启电视服务的话，流量指标是空的、`reconnect` 被忽略，
其余照常。

| | |
| --- | --- |
| 路径 | 页面 `/tv/admin`；`GET /api/admin/overview[?history=1]`、`users`、`users/<id>/sessions`、`users/<id>/export`、`devices`、`audit?user=&device=&action=&before=`、`system`、`channels`；`POST /api/admin/users/<id>/rename\|ban\|unban\|reviewed\|login-link\|delete\|sessions/revoke`、`invites`、`devices/<id>/unbind\|ban\|unban\|reconnect\|repair\|rename`、`channels` `{version, rows, dry}`、`system/legacy-token` |
| 指标 | 「正在播放」多了 `bytes_10s`、`fps`（每秒发出的画面数；只有默认的发送器数，`TV_LIVE_ENGINE=v2` 时是 0）、`target_rate`；主机采样每 5 秒读 `/proc/stat`、`/proc/meminfo`、`/proc/net/dev`，内存里留 15 分钟的 5 秒点和 24 小时的分钟平均（重启就没了）；没有 `/proc`（macOS）时不运行 |
| 审计 | `user.rename`、`user.reviewed`、`user.ban`/`unban`、`user.export`、`user.delete`、`admin.session.revoke`/`revoke_all`、`phone_link.admin`、`invite.create`、`device.reconnect`、`device.banned`/`active`/`unclaimed`、`device.unbind`、`device.repair`、`device.rename`、`channels.save`（台数变化和备份文件名）、`legacy_token.on`/`off` |

## 留给以后的阶段

每个用户隐藏、排序直播台和提议新台（`user_state` 给 `hidden`/`order` 留了位置）；主人以外的设备成员
（表已建好，没有界面）；媒体网关、每台设备一个工作进程、型号配置和准入控制（阶段 4），那时总后台才有
同时服务的上限和每个工作进程的数字。

## 未验证

- 这些步骤没在云主机上跑过；仓库里没有 Passport 单元的实际路径。
- 固件的身份部分（HELLO 的新键、NVS 里的密钥）两块板子都编译通过，但没上设备测：
  `esp_efuse_mac_get_default()` 在 Muse 固件下给出同一个 id、AI Passport 切换固件后密钥还在，
  都是预期而不是实测。
- 绑定画面和 `bound: 0` 只对着服务器测过；两种固件在屏幕上怎么画这张画面没在设备上看过。
- 手机上的流程是在桌面浏览器里按手机宽度、对着本机中转和合成状态检查的，没在 iPhone 主屏幕
  应用或微信里试过。
- 自己注册、设备恢复登录和 `login-link` 由 `tests/test_account_recovery.py` 覆盖，手势由
  `tests/test_av_recover.c`（对着按键驱动的模型）覆盖；两种手势都没在设备上按过，二维码没用手机相机扫过，
  时间（两组之间 0.4 秒、驱动的 180 毫秒）没有用手实测。
- 电脑版是在桌面应用的 Chromium 窗格里按 1440 和 900 像素宽、对着本机中转、合成状态、假的视频网站
  和假设备检查的；二维码由这个浏览器的 `BarcodeDetector` 识别，没用手机相机或微信扫过。没在 Safari、
  Firefox、Edge 里试过，也没经过 nginx。
- 总后台（阶段 3）在同一个窗格里按 1440 像素和手机宽度（375 像素）、对着本机中转、临时数据库、假设备和
  合成的 `/proc` 检查过（macOS 上的主机曲线是编出来的）；没在云主机上跑过，所以真实的 `/proc` 数字、进程表、
  带 numpy 时检查子进程要多久（本机不带 numpy 约 0.1 秒）、备份和 `exports/` 的文件权限都没验证。
  `reconnect` 事件只有主机测试，没在设备上试过。
