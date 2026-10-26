简体中文 | [English](README.md)

# 小鼠电视（RooTV）

*小鼠电视原名小智电视（Xiaozhi TV），0.2.5 测试版以后改用现在的名字；代码仓库名不变。*

小鼠电视把一个小小的 ESP32 设备变成全家用的迷你电视机：家里的电脑在后台干重活，小电视负责放画面和声音，手机就是遥控器。

支持两种设备：

- **星智 Cube 1.54**（ESP32-S3），就是当「小智 AI」玩具卖的那个小方块
- **FoloToy AI Passport**（ESP32-C3）

> **这是测试版（v0.2.0-beta）。** 在家里已经能用，但有些步骤还没在每种电脑上都走过一遍，见[这个测试版](#这个测试版)。

## 它由哪几部分组成

| 部分 | 做什么 |
| --- | --- |
| 小电视上的**固件** | 显示画面、播放声音，按键换台、调音量 |
| 常开电脑上的**电脑客户端** | 取你要看的内容、转换格式，经家里的 Wi-Fi 送到小电视。支持 Windows、macOS（Apple 芯片和 Intel）和 Linux。装好后只是任务栏或菜单栏里的一个小电视图标 |
| 手机浏览器里的**手机遥控** | 频道、UP 主和剧集订阅、选集、投屏、用手机听、管理小电视的 Wi-Fi 等。添加到主屏幕后用起来像一个 App |

视频只在你家里的网络里走，不经过任何云服务器。

**本项目不带任何频道、直播地址、播放列表、账号或 cookies。** 频道表一开始是空的，要看什么由你自己添加。

## 你需要准备

| 东西 | 说明 |
| --- | --- |
| **一台小电视** | 星智 Cube 1.54 或 FoloToy AI Passport |
| **一台常开的电脑** | Windows 10/11（64 位），或 macOS 12 及以上；Linux 也能用（验证得少）。看电视时电脑要开着 |
| **家里的 Wi-Fi** | 电脑、小电视、手机连同一个家庭网络 |
| **一根 USB 数据线** | 只在刷固件时用。有的线只能充电，要用能传数据的 |
| **Chrome 或 Edge 浏览器** | 装在电脑上，刷固件用。Safari 和 Firefox 刷不了 |
| **一部手机** | 有相机和浏览器的智能手机都行 |

## 下载

所有文件都在 **[Releases 页面](https://github.com/rooma1989/ai-passport-tv/releases)**。最新版本在 **[releases/latest](https://github.com/rooma1989/ai-passport-tv/releases/latest)**，目前它还是测试版。

| 你的电脑 | 下载哪个文件 |
| --- | --- |
| Windows 10/11 | `RooTV-<版本>-windows-x64-setup.exe`（安装包，推荐），或 `RooTV-<版本>-windows-x64.zip`（免安装版） |
| Mac，Apple 芯片（M1、M2、M3、M4…） | `RooTV-<版本>-macos-arm64.dmg` |
| Mac，Intel 芯片 | `RooTV-<版本>-macos-x86_64.dmg` |
| Linux x86_64（验证得少） | `rootv-<版本>-linux-x86_64.tar.gz`（看压缩包里的 `README-linux.txt`） |

0.2.5 测试版及以前这些文件叫 `XiaozhiTV-…` 和 `xiaozhi-tv-…`。在小智电视上直接装小鼠电视就是升级：旧程序会退出（Windows 安装程序会把它卸掉，Mac 上之后可以把「Xiaozhi TV」删掉），第一次启动时数据文件夹自动搬过来，设备、账号、频道都还在。

不知道自己是哪种 Mac？点左上角苹果菜单 →「关于本机」：写着「芯片 Apple M…」的是 Apple 芯片，写着「处理器 Intel…」的是 Intel。

想核对下载有没有出错，可以用 `SHA256SUMS-desktop` 里的校验值。页面上的其他文件刚开始用不到：电脑客户端里已经带着小电视的固件。

## 准备：安装电脑客户端

带每一步说明的完整教程在 [`docs/desktop/README.zh_CN.md`](docs/desktop/README.zh_CN.md)。简单说：

1. **安装**。
   - **Windows**：双击 `setup.exe`。系统问「是否允许更改」时点「是」。
   - **Mac**：打开 `.dmg`，把「RooTV」**拖进「应用程序」**，再从「应用程序」里打开。
2. **第一次打开会被系统拦一下**。测试版还没有签名，系统认不出发布者，不是文件坏了。
   - **Windows**（「Windows 已保护你的电脑」）：点「**更多信息**」，再点「**仍要运行**」。
   - **Mac**（「未打开"RooTV"」）：点「**完成**」。**不要点「移到废纸篓」**，那会把程序删掉。然后打开**系统设置 → 隐私与安全性**，往下滑，找到关于「RooTV」的那一行，点「**仍要打开**」，输入密码。（macOS 12 到 14 也可以在「应用程序」里按住 Control 键点这个 App，选「打开」。）
   - Mac 问能不能查找本地网络上的设备时，点「**允许**」。
3. **第一次运行**。任务栏右下角（Windows）或屏幕顶端菜单栏（Mac）出现一个小电视图标。第一次会下载视频组件 ffmpeg（几十 MB），然后**浏览器自动打开设置页**。填上家里的名字和你的名字，成为管理员。
4. **连接手机**。点小电视图标 →「**连接手机…**」，屏幕上出现二维码。手机连上家里的 Wi-Fi，用手机自带的相机扫码并登录，再按提示「添加到主屏幕」，以后一点就能打开遥控。家里每个人可以各扫一次。

## 第一步：刷固件

小电视要先刷一次小鼠电视的固件。

1. 用 USB 数据线把小电视插到**装了电脑客户端的这台电脑**上。
2. 点小电视图标 →「**设置一台电视…**」，浏览器里打开刷机页面。要用 **Chrome 或 Edge**。
3. 点连接，选中小电视对应的串口。页面自己认出是哪种设备。
4. 按页面提示操作，等它显示写入完成。

**刷之前请看清楚：**

- **星智 Cube：原来的小智 AI 固件会被清掉。** 第一次刷会把整片芯片擦除，刷完它就是一台小电视，不再是小智 AI 聊天玩具了。这个刷机页面没法把原来的固件刷回去。擦除前页面会请你打勾确认；以后再刷只更新，不再擦除。
- **AI Passport：绝不擦除。** 它里面有出厂写入的身份信息，擦掉就找不回来。刷机页面只写固件那一段，自己核对写入范围。
- **装了 Muse 双系统的 AI Passport**：公开版不支持，刷机页面会拒绝。见 [`docs/boards/passport-muse-dual.zh_CN.md`](docs/boards/passport-muse-dual.zh_CN.md)。

**刷完后要手动开机**：

- **AI Passport**：按住电源键约 2 秒关机，再按住约 0.5 秒开机。它有电池，拔掉数据线不会重启它；不做这一步，屏幕一直是黑的。
- **星智 Cube**：按一下最下面那个键（复位键）。

懂行的人还可以这样刷：

- **放到自己的网站上刷**：Release 里的 `xiaozhi-tv-flasher.zip` 是刷机页面加固件，解压到任何 https 网站的 `/flash/` 下就能用。
- **用 esptool 手动刷**：Release 里有 `FoloToy-AI-Passport-tv.bin` 和 `Xingzhi-Cube-154-tv-public.bin`，都是从地址 `0` 写入的完整镜像。细节见 [`docs/boards/xingzhi-cube-154.zh_CN.md`](docs/boards/xingzhi-cube-154.zh_CN.md) 和 [`docs/desktop/README.zh_CN.md`](docs/desktop/README.zh_CN.md)。**AI Passport 千万不要用 `erase-flash`。**

## 第二步：连 Wi-Fi、绑定设备

刷完开机后，小电视显示配网画面和一个二维码。

1. **声波配网**（最省事）：用手机扫小电视屏幕上的二维码，打开配网页。填家里的 Wi-Fi 名称和密码；「服务器」一栏已经自动填好电脑的地址。把手机音量调大、靠近小电视（半米以内），点「**发送**」。小电视听到后连上 Wi-Fi，设备会自己重启。
2. **热点配网**（声波不行时）：在配网画面上用 ▲ / ▼ 翻页，翻到显示小电视热点名称和配网地址的那一页。手机连上这个热点，打开那个地址（一般是 `192.168.4.1`），填 Wi-Fi 和服务器地址。
3. **绑定到你的账号**：小电视接着显示「BIND THIS TV」和一串绑定码。双击按键打开状态页（见[按键](#按键)），用已登录的手机扫状态页上的二维码；或者在遥控里打开「**我的 → 绑定新设备**」输入那串码。

以后要重新配网：打开状态页，长按最上面的键（▲）。设备的主人也可以在手机上管理它保存的 Wi-Fi（「**我的 → 我的设备 → Wi-Fi 网络**」）。

## 第三步：添加频道和 UP 主

- **频道**：点小电视图标 →「**打开电脑版**」，在「频道表」里导入**你自己的** M3U 播放列表（地址或文件），检查哪些能播，保存你想要的。一台小电视最多 128 个频道。
- **UP 主和剧集**：在手机遥控的「**订阅**」里贴 B 站 UP 主、剧集或合集的链接。每个都会变成一个频道，按顺序播最新的或下一集，记得上次看到哪。

还没有任何频道时，小电视显示「NO CHANNELS YET」；加上频道后它自己会接上。

## 日常使用

### 手机遥控

底部有四个标签：**遥控、投屏、订阅、我的**。

- **遥控**：换台、调音量和亮度；视频可以暂停、拖进度、快进快退；剧集有上一集、下一集。
- **用手机听**：小电视的声音从手机或和手机配对的蓝牙耳机里出来，锁屏也接着放；小电视照常放画面，喇叭静音。晚上不吵人。
- **投屏**：贴一个 B 站视频链接，小电视就播它，播完回到原来的台。
- **订阅**：你的 UP 主和剧集，可以选集。
- **我的**：你的设备（Wi-Fi、访客码、改名、解绑）、绑定新设备、让另一部手机或电脑登录、邀请家人。

**语音控制**：固件支持按住 OK 说话，但服务器那边需要语音识别。这个测试版的电脑客户端还没有带语音识别。

### 按键

| 想做什么 | AI Passport | 星智 Cube |
| --- | --- | --- |
| 调音量 | 单击 ▲ 或 ▼ | 单击第一或第二个键 |
| 换台 | 长按 ▲ 或 ▼ | 长按第一或第二个键 |
| 频道列表 | 单击 OK | 单击第三个键（BOOT） |
| 状态页 | 双击 OK | 双击第一或第二个键 |
| 亮度 | 双击 ▲ 或 ▼ | 长按第三个键 |
| 重新配网 | 在状态页长按 ▲ | 在状态页长按第一个键 |
| 只听声音（不放画面，显示小宠物） | 在状态页单击 OK | 在状态页单击 OK |
| 开关机 | 按住电源键 | 长按最下面的键 |

星智 Cube 的更多细节：[`docs/boards/xingzhi-cube-154.zh_CN.md`](docs/boards/xingzhi-cube-154.zh_CN.md)。

## 这个测试版

- **每种型号一次只服务一台小电视。** 一台星智 Cube 加一台 AI Passport 可以同时看；两台同型号的暂时不行。
- **只能在家里遥控。** 手机要连家里的 Wi-Fi。出门在外遥控（云端通道）计划在 v0.3.0-beta 提供。
- **还没有签名。** Windows 和 macOS 第一次打开会提醒（见上面）。
- **电脑要一直开着。** 有人在看时客户端会阻止电脑闲置睡眠，但合上笔记本盖子还是会睡。
- **电脑的地址最好不变。** 路由器给电脑换了地址，小电视就找不到它了。在路由器里给电脑设「固定 IP / DHCP 静态分配」最稳。
- **还没有全部手动验证过**，比如在真的 Windows 电脑和 Intel Mac 上从头走一遍。教程里列了哪些还没验证。遇到问题请到 GitHub 上报告，托盘菜单里的「导出诊断信息…」会生成一个不含密码和密钥的压缩包，可以附上。

常见问题和解决办法：[`docs/desktop/README.zh_CN.md`](docs/desktop/README.zh_CN.md#常见问题)。

## 进阶：NAS、Docker、没有屏幕的机器

没有常开的电脑，但有 NAS 或迷你主机？同一个服务器可以作为**家用服务器**跑在 Docker、NAS 的容器里，或直接用 Python 跑，没有托盘图标：[`docs/selfhost/README.zh_CN.md`](docs/selfhost/README.zh_CN.md)。

## 以前的单机版服务端

早期版本有一个单机版服务端（下载 `tv-server-*`，或不带参数运行 `./run.sh` / `run.bat`）。它只服务一台设备，没有账号和手机登录，要在小电视的热点配网页上手动填「电脑地址:8096」。它再保留一个版本，留给已经在用的人。**新装请用电脑客户端。** 用法见 [`server/README.zh_CN.md`](server/README.zh_CN.md)。

## 给开发者

**仓库里有什么**

| 路径 | 内容 |
| --- | --- |
| `main/`、`components/bsp/` | 设备固件（ESP-IDF）和两种设备的板级支持 |
| `server/` | 媒体服务、中转（账号、手机遥控、电脑版）和家用服务器（`server/selfhost/`） |
| `desktop/`、`packaging/desktop/` | 电脑客户端的托盘程序和安装包 |
| `packaging/flash/` | 网页刷机 |
| `tests/`、`tools/` | host 测试，以及构建、检查和实验脚本 |
| `docs/` | 文档、设计说明和更新日志 |

**编译固件**（ESP-IDF 5.5.3）：

```sh
./tools/validate.sh --prototype   # AI Passport 电视固件
./tools/validate.sh --xingzhi     # 星智 Cube 电视固件
```

镜像输出到 `build/`（`FoloToy-AI-Passport-prototype-public.bin`、`Xingzhi-Cube-154-tv-public.bin`）。Wi-Fi 和服务器地址都在设备上设置，固件里不编进任何私人信息。

**检查和测试**：

```sh
./tools/validate.sh --static      # 仓库检查和 host 测试，不需要 ESP-IDF
./tools/validate.sh               # 完整检查
python3 tools/check_repo.py       # 链接、中英文配对、密钥
```

**CI**（`.github/workflows/`）：静态检查和 host 测试、固件构建、电脑客户端的核心和安装包、Docker 镜像、以前的单机版服务端。推送版本标签时构建并发布 Release。

**接着读什么**

- [`AGENTS.zh_CN.md`](AGENTS.zh_CN.md)：约定、验证和安全规则（从这里开始）
- [`docs/README.zh_CN.md`](docs/README.zh_CN.md)：硬件能力契约和文档索引
- [`docs/assets/architecture-index.zh_CN.md`](docs/assets/architecture-index.zh_CN.md)：哪个文件管什么
- [`docs/desktop/developer.zh_CN.md`](docs/desktop/developer.zh_CN.md)：电脑客户端的代码地图、构建和控制接口
- [电脑客户端设计](docs/superpowers/specs/2026-10-19-pc-client-architecture-design.zh_CN.md)和[自建服务器设计](docs/superpowers/specs/2026-10-15-self-hosting-design.zh_CN.md)
- [`docs/CHANGELOG.zh_CN.md`](docs/CHANGELOG.zh_CN.md)：改了什么
- [`docs/fork-guide.zh_CN.md`](docs/fork-guide.zh_CN.md)：这个 fork 和上游的关系

## 来源与许可

本项目 fork 自 [zpdeng1020-coder/ai-passport-tv](https://github.com/zpdeng1020-coder/ai-passport-tv)，它基于 [FoloToy AI Passport 官方固件](https://github.com/folotoy/ai-passport)。感谢两者。AI Passport 的硬件设计归 FoloToy 所有。星智 Cube 和小智 AI 是其他厂商的产品，本项目和它们没有关联。

代码采用 MIT 许可，见 [`LICENSE`](LICENSE)。第三方组件保留各自的许可：电脑客户端的清单在 [`packaging/desktop/NOTICE.txt`](packaging/desktop/NOTICE.txt)，Docker 镜像的在 [`packaging/docker/NOTICE`](packaging/docker/NOTICE)。[FFmpeg](https://ffmpeg.org/) 不在电脑客户端的下载包里：客户端第一次运行时从它的发布方下载（GPL 构建）。

## 免责声明

小鼠电视是开源软件，仅供在您自己家中私人、非商业使用。它不包含、不托管、不出售、也不推荐任何频道、直播流、视频、账号或代理。它播放的一切内容都由您自己的电脑从您自己添加的来源获取；您应自行确保有权观看这些内容，并遵守您所使用服务的条款和您所在地的法律。请勿用它向家以外的人传播内容。本软件按「现状」提供，不附带任何形式的担保（MIT 许可）。详见 [`docs/selfhost/README.zh_CN.md`](docs/selfhost/README.zh_CN.md#免责声明)。
