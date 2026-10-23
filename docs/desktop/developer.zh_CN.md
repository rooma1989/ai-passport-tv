<p align="right">
  <strong>简体中文</strong> · <a href="developer.md">English</a>
</p>

# 电脑客户端：托盘和安装包（开发者说明）

设计：[`2026-10-19-pc-client-architecture-design.zh_CN.md`](../superpowers/specs/2026-10-19-pc-client-architecture-design.zh_CN.md)
第 5.1–5.4、5.10–5.13 节，阶段 P2（P2a 托盘、P2b 睡眠/自启动/网络、P2c 安装包）。用户教程见 [`README.zh_CN.md`](README.zh_CN.md)。

## 代码在哪

| 文件 | 内容 |
| --- | --- |
| `desktop/tray.py` | 入口（`XiaozhiTV`、`python3 -m desktop`）；`App`：启动并看着核心、每 2 秒读状态、第一次运行、各个菜单动作；`PystrayUI`（真托盘）、`HeadlessUI`（`--no-tray`）；`--smoke-test`、`--tray-check` |
| `desktop/menu.py` | 菜单的纯数据模型：标题行、问题、图标状态、菜单项。测试不需要图形界面 |
| `desktop/control.py` | 本机控制接口的客户端（P1c 约定）和 `Status.from_json`：约定变了只改这里 |
| `desktop/core.py` | 启动/重启/停止核心进程：不写的标准输入管道 + `TV_WATCH_PARENT=1`；Windows 上作业对象（`tools/parentwatch.attach`）和 `CREATE_NO_WINDOW`；退出码 3（另一个核心占着数据目录）不当作崩溃 |
| `desktop/instance.py` | 只开一个：`run/tray.lock` 上的系统文件锁；第二次启动经控制接口打开电脑版后退出 |
| `desktop/power.py` | 阻止闲置睡眠：Windows `SetThreadExecutionState`（一个专用线程）、macOS `IOPMAssertion`（ctypes，后备 `caffeinate -i -w`）、Linux `systemd-inhibit`；`KeepAwake` 策略（有设备在看时拿住，最后一台离开 2 分钟后放开） |
| `desktop/autostart.py` | 登录时启动：LaunchAgent、HKCU Run（尊重任务管理器里的关闭）、XDG autostart |
| `tools/netcheck.py` | Windows 网络被标成公用；路由器身份（网关 MAC 的哈希）和受信任网络的判断；和核心共用（由 `server/selfhost/network.py` 执行） |
| `desktop/icons.py`、`logs.py`、`diagnostics.py`、`paths.py` | 托盘图标（Pillow 现画）、`tray.log`、核心不在时托盘自己导出的诊断包、各系统的路径 |
| `desktop/devcore.py` | 替身核心：只有控制接口和假状态，按 P1c 约定说话。给测试和 `--stub-core` 构建用，**不能发给用户** |
| `packaging/desktop/` | `tray.spec`、`stub-core.spec`、`build.py`、`smoke.py`、`release.py`（版本检查、发布文件集）、`fetch_firmware.py`、`xiaozhi-tv.iss`（Inno Setup）和附带的 `ChineseSimplified.isl`、`NOTICE.txt`、Linux 的 `.desktop`/systemd 单元/说明 |
| `packaging/release/release-body.txt` | 发布页的说明，固件、服务器、电脑客户端三个流程都发它 |
| `.github/workflows/build-desktop.yml` | 四个平台的构建和冒烟测试，汇总发布文件集；推标签时发布 |
| `tests/test_desktop_release.py` | 发布文件集、标签和版本的检查、等同一次发布的固件、发布说明、中文安装向导 |
| `tests/test_desktop_tray.py` | 菜单状态、状态解析、控制接口客户端（对着替身核心）、只开一个、自启动文件、睡眠断言（模拟）、网络解析、核心进程、`App` 的动作 |

托盘从不导入 `server`；核心是另一个程序，两者之间只有控制接口。这样方案 B（Tauri 外壳）只换托盘。

## 本机运行

```bash
python3 -m venv build-venv && build-venv/bin/pip install pystray pillow pyinstaller
build-venv/bin/python -m desktop --dev-core                 # 替身核心 + 真托盘
build-venv/bin/python -m desktop --dev-core --no-tray       # 不要托盘
build-venv/bin/python -m desktop --dev-core --tray-check 3  # 显示 3 秒后自己退出
python3 -m desktop --dev-core --smoke-test --data /tmp/x    # CI 用的检查（不用 pystray）
python3 -m unittest tests.test_desktop_tray
```

不带 `--dev-core` 时从源代码运行的是真核心 `python3 -m server.selfhost --desktop --data <数据目录>`（分支 `desktop-core`）。
从源代码运行时不会注册登录启动项（只有安装好的副本才注册），`--smoke-test`、`--tray-check` 也不拿睡眠断言。

## 构建

```bash
python3 packaging/desktop/build.py --stub-core               # CI：替身核心
python3 packaging/desktop/build.py --core-dir <xiaozhi-tv-core 的 onedir>   # 真核心
python3 packaging/desktop/smoke.py [--tray-check]
```

产物在 `build-desktop/`：macOS 的 `Xiaozhi TV.app` 和 `XiaozhiTV-<版本>-macos-<arm64|x86_64>.dmg`，Windows 的免安装
zip 和给 Inno Setup 的 `stage/Xiaozhi TV/`，Linux 的 `xiaozhi-tv-<版本>-linux-x86_64.tar.gz`，以及 `SHA256SUMS`。
`--release` 拒绝替身核心。

包的布局：托盘和核心是两个 PyInstaller onedir。macOS 上核心在 `Contents/Resources/core/`（`Contents/MacOS` 里只能放
代码，onedir 里还有数据文件），先逐个给核心里的 Mach-O 临时签名，再签整个应用；Windows 和 Linux 上核心在托盘旁边的
`core/`。代价是两份 Python 运行库（约多 15 MB）；以后可以用 PyInstaller 的多程序打包合成一份。

## 和核心之间的约定（P1c）

托盘用到的：`control.json`（`port`、`token`）、`Authorization: Bearer`、`GET /status`（`state`、`problems`、`lan_url`、
`local_url`、`ports.lan`、`setup_pending`、`devices`、`watching`、`listening`、`network`、`ffmpeg`、`settings`、`logs_dir`）、`console-link`
（第一次运行时它返回设置页）、`phone-qr`（`url` + `svg`，托盘写一个本地页面显示二维码）、`power`、`autostart`、
`network/trust`、`ffmpeg/fetch`、`diagnostics`、`restart`、`stop`；退出码 3。

托盘这边定下的：

- **睡眠断言由托盘拿**（约定里说由 P2b 决定）。如果以后核心自己拿，状态里加 `"power": {"managed_by": "core"}`，托盘就
  放开自己的。有设备在看、或有手机在听（`status.listening`，核心从电视服务的「正在播放」文件里读）时都拿住。
- **登录启动项由托盘注册**；`POST /autostart` 只是让核心在 `desktop.json` 里记一下。第一次运行时注册一次，之后以系统里的
  登录项为准（用户在任务栏管理器、登录项里关掉的不会被重新打开）。
- **受信任的网络（5.11）**：核心报 `status.network`（`{"id", "trusted", "profile", "lan_open"}`）并执行（不在家里的网络上
  局域网端口只在 127.0.0.1，问题 `network_away`）；`trusted` 是 null 时托盘问「这是我家的网络」，用
  `POST /local/v1/network/trust {"id", "trusted"}` 回答。
- **Windows 网络被标成公用**：核心报（问题 `network_public`、`network.profile`）；只有核心不报时托盘才自己查
  （PowerShell `Get-NetConnectionProfile`，每分钟一次）。

## CI

`build-desktop.yml`：macos-14（arm64）、macos-15-intel、windows-latest、ubuntu-22.04。先由一个任务取核心刷机页用的固件：
推标签时等 build-firmware.yml 把同一次发布的 `xiaozhi-tv-flasher.zip` 发出来，版本必须就是那个标签；别的时候取最新发布的，
尽力而为。每个平台：托盘的单元测试、版本（推标签时必须是 `v` + `desktop.__version__`，并写进核心）、构建核心（`tools/build_core.py`，从 `desktop-core`
合进来的）、构建客户端、冒烟测试（中文加空格的数据目录）、托盘图标检查（允许失败，Linux 跳过）；Windows 再编译安装包、静默安装、
检查防火墙规则（只有专用网络、指向核心程序）、对装好的副本做冒烟测试、卸载并确认规则和文件都没了。失败原因都打成
`::error::` 注解（看日志要登录，注解不用）。产物是未签名的测试构建，名字是 `desktop-<平台>`，保留 7 天。

然后 `release-set`（每次都跑）按每个平台构建时写的 `SHA256SUMS` 核对文件，把五个下载文件和 `SHA256SUMS-desktop` 存成
产物 `desktop-release`（`packaging/desktop/release.py collect`）。`release` 只在推标签时（或在标签上手动运行并勾上
`publish`）运行，用 softprops/action-gh-release 把这一套发到那个标签的发布里，并发组 `release-<标签>` 和固件、服务器流程
共用。所以推分支和手动运行都是发布的试运行。

发版：确认 `desktop/__init__.py` 里是这个版本，合进默认分支，推标签 `v<版本>`；固件、服务器、电脑客户端三个流程各自把文件加进
同一个发布。

在本机做一份试用（macOS）：`python3 tools/build_core.py`，再 `python3 packaging/desktop/build.py --core-dir
build-core/dist/xiaozhi-tv-core`；想留着就把 DMG 复制到 `dist-desktop/`（不进 git）。

## 还要做的

- `SHA256SUMS-desktop` 的 Ed25519 签名（P8）。
- 在 CI 里从源码编 PyInstaller 引导程序（减少杀毒误报，5.12）；Windows 程序的版本信息。
- GitHub 的运行机换了 Inno Setup 版本时，换上对应的 `ChineseSimplified.isl`（CI 会警告；见
  `packaging/desktop/ChineseSimplified-SOURCE.txt`）。
- 真机：Windows 10/11 上的托盘和菜单、SmartScreen、Defender；Intel Mac；macOS 15 的「本地网络」询问。
