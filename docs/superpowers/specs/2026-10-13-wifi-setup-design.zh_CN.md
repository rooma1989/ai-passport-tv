<p align="right">
  <strong>简体中文</strong> · <a href="2026-10-13-wifi-setup-design.md">English</a>
</p>

# 更省事的 Wi-Fi 配网 — 设计

状态：已在分支 `wifi-setup` 上做完，2026-10-13。电脑上的测试和两块板子的编译都通过；还没有在设备上跑过（第 9 节列出要验的东西）。
声波解码器本身是另一个模块（分支 `sound-prov`，它自己的设计说明 `2026-10-13-sound-provisioning-design.md`）；本文讲配网模式怎么用它。

## 1. 目标

以前给设备配 Wi-Fi 要：手机切到设备的热点，打开网页，填 Wi-Fi 名字、密码，公开版固件还要填服务器地址，再把手机切回来。三处改动让它变短：

- **A. 声波。** 配网模式下设备同时在听。手机页面把 Wi-Fi 名字和密码放成声音，设备解出来、连上、重启。手机不用离开自己的网络。
- **B. 远程管理。** 设备在线时，主人可以在手机遥控页和电脑版上查看、添加、删除、调整顺序、立即切换它保存的 Wi-Fi——比如带去姥姥家之前先加上那边的 Wi-Fi。密码端到端加密。
- **C. 不用填服务器。** 内置了服务器地址的固件把服务器那一栏收进一个小小的「高级」里；开机连不上任何保存的 Wi-Fi 时自己进入配网。

## 2. 用户看到的配网模式

```
开机 ─┬─ 没有保存的 Wi-Fi ──────────────────────────────► 配网（和以前一样）
      └─ 有保存的 ─► 60 秒都连不上 ─────────────────────► 配网（「自动」）
                     （每次开机一次，并且开机以来从没连上过）
配网：第 1 页 手机声波配网  二维码 → <SITE>/TV/W，"example.com/TV/W"，靠近电视点「发送」
      第 2 页 连接设备热点  第 3 页 打开配网页（http://192.168.4.1）
      第 4 页 使用说明（有帮助网址时） 第 5 页 当前状态
```

- 只有链接了解码器时声波才是第 1 页；没有时各页和以前完全一样（渲染图逐像素比过）。
- 二维码用大写（`HTTPS://HOST/TV/W`），走二维码的字母数字模式（一般的网站名是第 1 版）。中转在 `/tv/wifi` 和 `/TV/W` 两个地址给出这个页面（nginx 的 `location = /TV/W`，在 sound-prov 分支）。
- 网站地址依次取：服务器上次给的 `remote_url`（CONFIG 带来新的时存进 NVS `avsettings/site`）、Kconfig `CONFIG_AV_PUBLIC_SITE`（不进 git 的 sdkconfig）、`AV_HELP_URL` 的网站（私有头文件）。都没有时只显示路径，不画二维码。
- 听的时候二维码下面一行依次是 `靠近电视点「发送」`、`正在接收… 60%`、`收到了，正在连接…`、`已连上，马上重启`，失败时朱红色的 `没连上，检查密码再发`（6 秒后继续听）。听到消息时不管在哪一页都跳回第 1 页。
- 解出来的 Wi-Fi 走的就是网页表单那条路（`WifiConfigurationAp::ConnectAndSave`：先试连、连同信道保存、带了服务器就存服务器），然后结束配网、重启。
- **自动**进入的配网，在热点的定时扫描看到某个保存的 Wi-Fi、并且没人在用配网（没有手机连在热点上、没有正在收的声音）时自己退出，回去播放——路由器重启时设备不会卡在配网里。

### 麦克风

配网本来就为了「底噪」把 codec 以 16 kHz/16 位/单声道打开，AI Passport 的 ES8311 全双工麦克风正需要这个（`bsp_mic_start` 读已经打开的 I2S0）。星智开自己的 I2S1 麦克风（这块板上的格式仍未实测）。一个任务（`av_sound`，6 KB 栈）每次读 20 毫秒、连续喂给解码器；解码器的状态是一次 14.9 KB 的分配，配网结束时连同任务一起释放。日志每 2 秒打一行 `progress=… peak=…`，分得清「什么也没听到」和「听到了但解不出」。

`CONFIG_AV_SOUNDPROV_DUMP`（默认关）在一条消息开始时，把之前半秒和之后 `CONFIG_AV_SOUNDPROV_DUMP_S`（10）秒的麦克风 PCM 打成 `SNDDUMP` 行；`tools/soundprov_dump.py` 把保存下来的串口日志转成 WAV，给 `tests/test_av_soundprov --wav` 重放。

### 服务器那一栏（C）

`main/Kconfig.projbuild` 新增 `CONFIG_AV_DEFAULT_SERVER`（host[:port]），给公开版固件用；私有版的 `AV_SERVER_IPV4`/`AV_SERVER_PORT` 也算。有其中之一，配网页从 `/advanced/config` 拿到 `"server_builtin": true`：服务器栏收进「高级」、不再必填，说明改成「已经内置」。存下来的地址仍然优先。网站名和地址不进提交的文件：它们只在编译目录自己的 sdkconfig 或不进 git 的私有头文件里。

**端口。** 没写端口的地址（两个页面上都可以只填 "example.com"）指这份固件对应的服务器：私有头文件的 `AV_SERVER_PORT`，公开版则是 `CONFIG_AV_DEFAULT_SERVER_PORT`（AI Passport 8097，星智 8098）。以前的固件给这种地址用 8096（本机 `server/tv_server.py` 的端口），只填域名的设备就去连一个没人听的端口，一直超时。写了端口的照原样用，只有一条迁移：公网域名（有点、不是 IPv4 数字地址、不是 `.local`）上的 `name:8096`——旧固件和旧页面提示会产生它——也换成本固件的端口；局域网地址保留 8096 给本机服务器。`av_server_addr_use_build_port()`（`tests/test_av_server_addr.c` 测）在每次读地址时套用，也就是每个会话，已经存下的地址不用重新配网。日志：第一次和每次变化时打印 `Server address: host:port (stored|built in[, port from this build])`；连不上打印 `RX connect to host:port failed errno=N`；域名解析失败打印 `Cannot resolve host: getaddrinfo=N`。

**页面上的默认值。** 设备热点配网页：没有存地址时，服务器栏预先填上设备现在要连的地址（`/advanced/config` 的 `"server_default"`），原样提交时发 `""`，内置地址仍是内置的。声波配网页（`wifi.html`）把「高级」里的服务器栏填成打开它的网站（`location.hostname`，本机地址除外）并随声音发送；清空就不发，空的服务器地址不会抹掉设备自己的设置。

## 3. 远程管理（B）的全程

```
手机/电脑 ──HTTPS──► 中转 ──事件文件──► 电视服务 ──TCP──► 设备
 POST /api/wifi     为这台设备加密     SWITCH {"wifi": 信封}   解密、执行
 GET  /api/wifi  ◄── 数据库 device_wifi ◄── STATUS {"wifi": 回答} ◄─ 回答 + 列表
```

1. 支持这个功能的固件在 HELLO 里带 `"wifimgr": 1`、`"wkey"`（它的 X25519 公钥，base64url）和 `"wifi_saved"`（显示名，按优先顺序）。电视服务的 gate 把它们记进 `device_wifi`（公钥首次信任，见第 4 节）。
2. CONFIG 带 `"wifi_key"`：中转的公钥（来自数据库 meta `wifi_pub`，中转启动时写入）。
3. 主人点添加/删除/上移/立即切换。`POST /api/wifi` 检查：是主人（或者是这台设备成员的管理员）——别人和其他设备接口一样一律 404；固件支持；钥匙没变；没有还在等回答的修改；设备在线（否则回答 `小电视不在线`，不排队）。然后把 `{"seq", "op", "ssid", "password"|"i"}` 为这台设备加密，在它的事件文件里加一条 `wifi` 事件（`text` 是信封，`value` 是 Unix 秒）。
4. 电视服务只把它转给发过 `wifimgr` 的固件，并且事件不超过 30 秒（暂停时放回队列的也一样），作为 `SWITCH {"wifi": {"v":1,"n":…,"c":…}}`。
5. 设备的接收任务把信封交给一个短命的工作任务（6 KB 栈）：解密、检查（`av_wifi_ops.c`）、改列表（SsidManager、NVS）、写好回答；接收任务把回答作为 `STATUS {"wifi":{"ack":seq,"ok":0|1,"err":码,"saved":[…],"cur":"…"}}` 发出。「立即切换」时工作任务等回答发出去后，把这个 Wi-Fi 挪到最前面、重启 station；连不上就按顺序连下一个能看到的，和开机时一样。
6. 电视服务记下回答；页面每秒问一次 `GET /api/wifi`，显示「已保存」、错误原因，20 秒没回答显示「小电视没有回应」。切换时一直等到设备在新 Wi-Fi 上重新上线（或者报告它回到了别的 Wi-Fi）。

设备执行的规则（`av_wifi_plan`）：`seq` 必须比上一次执行的大（NVS `avsettings/wseq`）；添加要求名字 1～32 字节、密码为空或 8～64 个字符，已存 10 个时拒绝（`full`），除非名字已经存过（那就只改密码）；删除/上移/切换用序号**加**报告过的名字指定，列表在这期间变了就拒绝（`changed`）。错误码在 `server/tenancy/wifi.py` 里换成中文。

### 优先顺序

以前 station 连信号最强的那个保存的 Wi-Fi。现在按列表顺序试能看到的，只有 −80 dBm 及更弱的排到所有能用的后面（`components/esp-wifi-connect/wifi_order.c`，有主机测试）。第一个 Wi-Fi 的信道还不知道时（刚加的或刚上移的），扫描所有信道而不是只扫存过的信道。SsidManager 最多存 10 个（`kMaxCount`），现在加了锁，防远程修改的工作任务和扫描同时读写。

## 4. 加密

### 威胁模型

| 谁 | 能看到 | 不能得到 |
| --- | --- | --- |
| 设备 ↔ 电视服务这段路上的任何人（明文 TCP：Wi-Fi 邻居、运营商、酒店网络） | 所有 HELLO、CONFIG、SWITCH、STATUS | 密码；改设备 Wi-Fi 的能力 |
| 同上但主动（能插包、改包） | 同上，还能冒充任一端 | 两边钥匙都记住之后发的消息里的密码 |
| 读得到事件文件或数据库的人 | 信封、公钥、Wi-Fi 名字 | 密码 |
| 中转进程 / 服务器管理员 | 加密那一刻内存里的密码 | ——（可信：它本来就掌握设备的节目流） |

不在范围内：手机 ↔ 中转这一段（nginx 的 HTTPS）、控制了中转或电视服务的攻击者、第一次接触（两边的钥匙都是首次信任，和 `device_secret` 一样）。

### 为什么不用 `device_secret` 推出钥匙

需求里提议 `HKDF(SHA-256(device_secret))`，两边都能算。可是设备**每次 HELLO 都明文发送 `device_secret`**（多用户设计 8.1），能看到这条链路的人——正是要防的那个人——一样算得出这把钥匙，等于没加密。

### 方案

- **设备钥匙**：设备上生成一次的 X25519 密钥对（Wi-Fi 起来后用硬件随机数），存在 NVS `avsettings/wkey`（私钥 + 公钥）。只有公钥离开设备，放在 HELLO 的 `"wkey"`。服务器第一次见到时记在 `device_wifi.pub`；以后换了公钥不认（页面显示 `key_changed`），除非设备在重新配对窗口里（主人点了「重新配对」），那时连同设备密钥一起换成新的。
- **中转钥匙**：数据库旁边一个文件里的 X25519 密钥对（`wifi-key`，0600，可用 `--wifi-key` 改位置）；公钥在数据库 meta `wifi_pub`，经 CONFIG 的 `"wifi_key"` 给设备。设备把它和自己拨的服务器地址一起记住（`avsettings/wsrv`、`wsrva`）；换了地址就改记新的；同一地址换了钥匙就拒绝，直到在设备上完成一次配网（会清掉记住的钥匙）。
- **消息钥匙**：`shared = X25519(自己的私钥, 对方的公钥)`（两边得到同一个值，从不上线）。`key = HKDF-SHA256(salt = 设备公钥 ‖ 中转公钥, ikm = shared, info = "ai-passport-tv wifi v1", 32)`。共享值全零（低阶点）时拒绝。
- **消息**：AES-256-GCM，12 字节随机 nonce，AAD `"avwifi1"`，16 字节标签。信封 `{"v":1,"n":b64url(nonce),"c":b64url(密文‖标签)}`；明文最多 320 字节 JSON，带 `seq`（中转的毫秒时钟，每台设备只增不减）。设备拒绝不比上一次大的 `seq`，截下来的消息不能重放。

效果：只看 TCP 链路的人得不到任何关于密码的信息；两边钥匙都记住以后，主动攻击者既读不了也伪造不了消息（静态-静态 Diffie-Hellman 同时认证了两端）；数据库或事件文件泄露不会泄露密码（数据库里只有公钥和 Wi-Fi 名字）。做不到的：前向保密（偷到设备或中转的私钥，就能解开录下来的、发给这台设备的消息），以及第一次接触时的保护。

实现：设备——mbedTLS（Curve25519、用 HMAC-SHA256 写的 HKDF，因为固件没开 `MBEDTLS_HKDF_C`、硬件 AES-GCM），`main/av_wifi_crypto.c`；服务器——`server/wifi_crypto.py`，只用标准库（照标准写的 AES/GCM/X25519；Python 里不是常数时间，可以接受：中转只在主人点按钮时算一次）。两边都用 RFC 7748、RFC 5869 和 GCM 规范的测试向量，外加同一个整消息信封测试（`tests/test_wifi_crypto.py`、`tests/test_av_wifi_crypto.c`；C 测试在电脑上编译 ESP-IDF 自带的 mbedTLS 源码）。

回来的 Wi-Fi 名字不加密（它们本来就在空中广播）；不是 UTF-8 的字节显示成 `?`，奇怪的 Wi-Fi 名字弄不坏 JSON。

## 5. 线上格式一览

| 位置 | 字段 | 谁发 | 含义 |
| --- | --- | --- | --- |
| HELLO | `"wifimgr": 1` | 固件 | 认得 SWITCH `"wifi"`，会发 STATUS `"wifi"` |
| HELLO | `"wkey"` | 固件 | 设备的 X25519 公钥（43 个字符） |
| HELLO | `"wifi_saved"` | 固件 | 保存的 Wi-Fi，显示名，按优先顺序 |
| CONFIG | `"wifi_key"` | 电视服务，只在多用户模式、只给 `wifimgr` 固件 | 中转的 X25519 公钥 |
| SWITCH | `{"wifi": 信封}` | 电视服务 | 一条加密的修改 |
| STATUS | `{"wifi": {"ack", "ok", "err", "saved", "cur"}}` | 固件 | 回答；`ack: 0` 表示解不开 |
| 事件 | `{"action":"wifi","text":信封,"value":unix秒}` | 中转 | 30 秒内转发 |

没有新的包类型：用 SWITCH 和 STATUS 承载，由 HELLO 里的标志打开，旧服务器、旧固件都不受影响。HELLO 原有的 `"wifi"."ssid"` 诊断现在也经过显示名过滤。

## 6. 存储

- 设备 NVS `avsettings`：`wkey`（64 字节）、`wsrv`（32 字节）+ `wsrva`（地址）、`wseq`（u64）、`site`（字符串）。Wi-Fi 列表仍在 esp-wifi-connect 的 `wifi` 命名空间里。
- 服务器数据库表 `device_wifi`（打开时建立，旧数据库自动补上）：记住的公钥、不一致的时间、上次报告的名字和正在用的、上一个 seq、等回答的修改、上一次回答。从不存密码。

## 7. 内存（固件）

- 静态内存：几百字节（公钥、900 字节的回答缓冲、标志）。实测总数见分支报告。
- 执行一条修改时：6 KB 工作任务栈，加约 1.3 KB 堆和 mbedTLS 短暂的大数，一秒左右全部释放。密钥对每台设备只生成一次，在会话分配缓冲之前、用一个临时 6 KB 任务生成。
- 带声波的配网模式：解码器 14.9 KB 加一个 6 KB 任务，配网结束时释放；打开转储选项再加 16 KB。

## 8. 文件

固件：`main/av_wifi_crypto.*`、`main/av_wifi_ops.*`、`main/av_wifimgr.*`、`main/av_soundsetup.*`、`main/av_provision.*`（保存的 Wi-Fi 接口、配网辅助）、`main/av_provision_policy.*`（自动配网、页面、网站、声波那一行）、`main/av_store.*`、`main/av_player.c`、`main/ui_overlay.c`（五个页码方块）、`components/esp-wifi-connect/`（`wifi_order.c`、SsidManager 的锁和 `MoveUp`、`ConnectAndSave`、`server_builtin`、页面上的开关）。服务器：`server/wifi_crypto.py`、`server/tenancy/wifi.py`、`server/tenancy/db.py`、`server/tenancy/gate.py`、`server/tv_server.py`、`server/cast_switch.py`、`server/relay/events.py`、`server/relay/wifi.py`、`server/relay/web.py`、`server/relay/__main__.py`、`server/relay/assets/remote.html`、`server/relay/assets/console.html`。

## 9. 还没验证的

- 还没在设备上跑过：密钥生成、工作任务的栈用量（会打出最低水位）、播放中写 NVS、切换。
- 星智麦克风的格式；声波配网时 Passport 的 codec 底噪挨着麦克风。
- 真实房间里的声波解码（解码器分支）；一次完整的声波配网。
- 路由器恢复后自动配网的退出。
