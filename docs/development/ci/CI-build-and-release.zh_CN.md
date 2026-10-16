<p align="right">
  <strong>简体中文</strong> · <a href="CI-build-and-release.md">English</a>
</p>

# 自动构建与发布（CI / Build & Release）

本仓库提供一套基于 GitHub Actions 的自动构建与发布流水线，用于在打 tag 时自动编译固件并发布 Release。

本文件与 `.github/workflows/build-firmware.yml` 一同维护，工作流行为变化时必须同步更新。

## 触发条件

- **push tag**：当向仓库推送 tag（如 `v1.0.0`、`v0.1.0-feature/xxx`）时触发自动构建，并在构建成功后自动创建 Release（带固件产物）。
- **workflow_dispatch**：可在 GitHub Actions 页面手动触发（用于调试/预发布验证）。

> 平时 push 到分支（非 tag）**不会**触发构建；只有打 tag 才会。

## 流水线做了什么

工作流有三个 job：

- **build**，每个设备一个（矩阵：`ai-passport` 用 `./tools/validate.sh --prototype` 编 ESP32-C3，`xingzhi-cube-154` 用 `./tools/validate.sh --xingzhi` 编 ESP32-S3），都在 ESP-IDF 5.5.3 容器里，各自一份 ccache。两个都是**公开版**（`AV_PUBLIC_BUILD=ON`）：`validate.sh` 核对合并镜像的布局（`tools/verify_firmware.py`：引导程序在 `0x0`、分区表在 `0x8000`、应用在 `0x10000`、8 MB Flash、受保护的 `cardid`），并核对它确实是公开版（`tools/firmware_manifest.py check-public`：`AV_PUBLIC_BUILD` 传到了编译器，所以没有私有头文件、令牌和帮助页地址；sdkconfig 里没有默认服务器和网站）。版本号是完整检出后的 `git describe --tags`，作为 `AV_FW_VERSION`（→ `PROJECT_VER`）传进去，设备在 HELLO 的 `fw` 里报它。然后 `tools/firmware_manifest.py make` 再读一遍镜像（芯片号、`0x10000` 处是本项目的应用、版本号对得上、Passport 在 `0x356000` 之前结束），按发布用的文件名复制一份，写出 `manifest-<设备>.json`。
- **package** 把两份合成 `manifest.json`，写 `SHA256SUMS`（每个镜像按清单重新算哈希；版本号不一致的两次编译会被拒绝），按刷机页的规矩再查一遍，并打包 `xiaozhi-tv-flasher.zip`：网页刷机（`packaging/flash/`）加旁边的 `firmware/`，放到 `/flash/` 下就能用。手动触发时也会跑，打 tag 之前就能看到整套文件。
- **release**（只在 tag 上，`contents: write`，和服务端工作流共用 release 并发组）确认每个文件都在，然后发布。

所有 Action 都固定到完整 commit SHA。build 和 package job 只有 `contents: read`。

## 产物

| 文件 | 是什么 | 怎么刷 |
| --- | --- | --- |
| `FoloToy-AI-Passport-tv.bin` | AI Passport（ESP32-C3）合并镜像 | 从 `0x0` 写，**绝不擦除**（出厂身份信息在 `0x356000`） |
| `Xingzhi-Cube-154-tv-public.bin` | 星智 Cube 1.54（ESP32-S3）合并镜像 | 替换小智 AI 固件时先整片擦除一次，再从 `0x0` 写 |
| `manifest.json` | 两个设备的设备名、芯片、版本、偏移、大小、SHA-256、擦除规矩、写入上限、保护区 | 网页刷机读它 |
| `manifest-<设备>.json` | 同上，单个设备 | |
| `SHA256SUMS` | 镜像和清单的校验值 | 刷机页也会核对 |
| `xiaozhi-tv-flasher.zip` | `flash/`：页面、esptool-js、`firmware/` | 解压到任意 HTTPS 网站下 |

## 在线烧录

网页刷机是 `packaging/flash/`（静态 HTML 和 JavaScript，esptool-js 0.6.1 连同许可证放在仓库里）。它从自己所在的网站取 `firmware/manifest.json`、`firmware/SHA256SUMS` 和镜像：GitHub Release 的下载不带 CORS 头，网页没法直接从 Release 取（见 `docs/flash/index.html`）。所以要把固件放在页面旁边：

- **静态网站**（`<静态网站>/flash/`，必须 HTTPS，网页串口要求）：把 `xiaozhi-tv-flasher.zip` 解压到网站根目录，或运行 `python3 tools/firmware_manifest.py bundle <放发布文件的目录> --out <网站>/flash`。
- **家庭服务器**：把同一个目录放在 `/flash/`；在插着设备的那台电脑上用 `http://localhost:8080/flash/` 打开，网页串口就能用。自托管包带上 `packaging/flash/` 的页面和这次发布的固件文件（放在它的 `firmware/` 里），并给 relay 设 `TV_FIRMWARE_MANIFEST`（那份 `manifest.json`）和 `TV_FLASHER_URL=/flash/`，总后台的设备页就能标出哪些设备的固件旧了。
- **手动**：`esptool.py --chip esp32c3 write_flash 0x0 FoloToy-AI-Passport-tv.bin`（Passport 绝不能 `erase_flash`）；星智见[它的板子文档](../../boards/xingzhi-cube-154.zh_CN.md#刷机)。

原来那个只支持 Passport 的 `docs/flash/index.html`（GitHub Pages，固件提交在 `docs/firmware/`）不变。

## Release 标题

当本仓库从同一棵源码树发布多个不同应用时，只有版本号看不出这个 Release 是哪个应用。给每个 tag 起一个
同时带版本与应用名的名字，并确保 Release 标题两者都显示。

- **Tag 命名约定**：按 `v<版本>-<应用名>` 小写连字符命名，例如 `v0.1.0-voice-keychain`、
  `v1.0.0-pocket-pomodoro`。`<应用名>` 是该 Release 构建的应用（见
  `plays/<username>/<app-name>/` 档案命名）。多应用共享同一棵树时，只写版本号的 tag 会有歧义。
- **发布成功后，核对 Release 标题**：workflow 会把标题设为 tag 名，因此命名正确的 tag 本身就显示成
  `v0.1.0-voice-keychain`。若 tag 没带应用名，或标题一眼看不出是哪个应用，就编辑该 Release
  （GitHub：`Edit release`；GitLab：编辑 tag），让标题为 `<版本> <应用名>`，例如 `v0.1.0 Voice
  Keychain`。快速扫一遍 Release 列表，就能区分每个 Release 是哪个应用。
- **标题与 tag 保持一致**：用 `<版本>-<应用名>`，让应用名在 tag 列表和 Release 列表里都可见。不要只靠
  人类可读的正文承载应用名。

## Release 说明

tag 触发的 Release 只有在合并固件与它的 Release 说明一起发布时才完整。发布 Release 后，要写一份
说明，向可能没读过仓库的用户解释这次构建。覆盖三块：

- **功能（What's new / 功能）**：本次 Release 相对上一版新增或变更的功能、行为或修复。面向用户，
  不是 commit 日志。
- **方法（How to build / 方法）**：如何生成并校验合并固件（`./tools/validate.sh --firmware` 或
  `idf.py build`），以及要烧录的产物文件（从 `0x0` 烧录的 `FoloToy-AI-Passport-full.bin`）。
- **使用（How to use / 使用）**：如何烧录（上方在线刷机工具），以及本次 Release 的关键交互或硬件
  要求。

用英文写 Release 说明（项目双语时再配一份简体中文），并在 GitHub/GitLab Release 上链接它们。对
用户可见的行为，保持与 `docs/CHANGELOG.md` 一致。

## 服务器产物的构建（`build-server.yml`）

服务端是另一个可独立分发的程序，由 `.github/workflows/build-server.yml` 构建，与固件互不影响。本文件与该工作流也需同步维护。

- **触发条件**：push tag、改动 `server/`、`tools/`、`packaging/`、`tests/` 或 `channels.txt` 的 pull request，以及手动触发。平时的分支 push 不触发。
- **三平台矩阵**：Linux x86_64、macOS arm64、Windows x86_64 各在自己的 runner 上构建。PyInstaller 不能跨平台构建，所以是矩阵而不是一个 job 加参数。矩阵设 `fail-fast: false`——某个平台失败是那个平台的事实，取消其余平台会掩盖同一改动是否也破坏了它们。
- **构建后立即冒烟测试**：每个平台的产物会当场启动一次，检查 `--help` 正常返回、空目录启动后两个服务都起来、媒体服务器端口接受连接、程序旁边生成了频道表。
  这一步不可省。本功能此前查出的每个缺陷都是"构建成功但一跑就死"——PyInstaller 按静态分析决定打包什么，只在运行时按名字导入的模块会被静默漏掉，只报告"构建成功"的流水线会把这些全部发出去。
- **产物**：`build-server/tv-server-<system>-<arch>[.exe]`，随 tag 发布到 Release。与固件一样不进仓库。
- **与固件工作流共用一个 release 并发组**：Release 的更新是"读-改-写"，两个 workflow 同时写同一个 tag 的 Release 会丢文件。两者用同一个 `concurrency` 组串行执行，且 `cancel-in-progress` 保持 `false`——发布任务被中途取消，正是已发布的 Release 缺文件的方式。


## 相关文件

- `.github/workflows/build-firmware.yml`：本流水线定义。
- 详见 `docs/hardware-design/AI_HARDWARE_DEVELOPMENT_GUIDE.md`（硬件/烧录细节）。
