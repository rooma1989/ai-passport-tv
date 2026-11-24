<p align="right">
  <strong>简体中文</strong> · <a href="onboarding.md">English</a>
</p>

# 小鼠电视（RooTV）项目上手说明

> 写给用 Claude Code 一起开发的同事。整理于 2026-11-24，对应版本 v0.3.15-beta。
> 规则以仓库根目录的 `AGENTS.md` 为准；这份说明讲的是"从哪里下手"和一些踩过的坑，跟 `AGENTS.md` 冲突时听 `AGENTS.md` 的。

## 1. 项目是什么

小鼠电视（原名小智电视）把一个小 ESP32 设备变成家用迷你电视：

| 部分 | 做什么 | 代码在哪 |
| --- | --- | --- |
| 小电视固件 | 显示画面、放声音、按键换台 | `main/`（C，ESP-IDF 5.5.3） |
| 电脑客户端 | 常开电脑上跑，取内容、转码、经 Wi-Fi 推给小电视；托盘图标 + 浏览器控制台 | `server/`（Python，只用标准库）、`desktop/`（托盘） |
| 手机遥控 / 电脑控制台 | 浏览器页面：频道、B 站、YouTube、网盘片库、投屏、管理 | `server/relay/assets/`（HTML/JS/CSS） |

支持两种设备：**星智 Cube 1.54**（ESP32-S3）和 **FoloToy AI Passport**（ESP32-C3）。

视频只在家庭局域网里走。项目本身不带任何频道、直播地址、账号或 cookies。

## 2. 代码仓库

- 主仓库：`git@github.com:rooma1989/ai-passport-tv.git`（本地 remote 名叫 `fork`，`main` 分支）
- 上游：`https://github.com/zpdeng1020-coder/ai-passport-tv`（remote 名 `origin`）
- 本机目录 `~/Projects/RooTV/` 下除了 `ai-passport-tv/`，其他文件夹（`ai-passport-tv-p3`、`-psp`、`h264-bench`、`hls-proto`、`muse-*`、`RooTVBox`、`backups` 等）都是实验、备份或旁支，**日常开发只在 `ai-passport-tv/` 里**。

## 3. 让 Claude 先读什么

仓库里已经给 AI 写好了入口，开新会话时让 Claude 按这个顺序读：

1. **`CLAUDE.md` → `AGENTS.md`**：唯一的规则来源（构建命令、提交规范、安全底线）。
2. **`docs/assets/architecture-index.md`**：一张"哪个文件管哪件事"的地图。做新功能先看它，不用把整个仓库读一遍。
3. 按 `AGENTS.md` 里的路由表，再读跟任务相关的文档（固件看 `docs/development/ai-guide.md`，发布看 `docs/development/ci/`，提交看 `docs/contribution/commit-and-pr.md`）。

几个大文件别让 Claude 整个读，用 grep 定位：`main/av_player.c`（约 3600 行）、`server/relay/library.py`（片库，4000 多行）、`server/relay/assets/console.js`。

## 4. 目录速览

```
main/                 固件（av_player.c 是整个播放会话；av_protocol.h 是线上协议）
components/bsp/       板级支持（引脚、屏幕、音频）
server/
  tv_server.py        给小电视推流的媒体服务
  live.py, hlsopen.py 直播频道
  relay/              控制台后端：B 站、YouTube、片库、网盘、管理 API……
    library.py        我的片库：扫描、片名识别、播放
    clouddrive.py     网盘组件（OpenList）：夸克、百度、UC
    admin.py          /api/admin/* 管理接口
    assets/           控制台、手机页面的前端
  selfhost/           电脑客户端的核心（版本号、自动更新）
desktop/              托盘程序、自动更新的托盘那一半
packaging/desktop/    打包 Mac/Windows/Linux 安装包
tools/                构建、验证、发布脚本
tests/                主机测试（Python unittest + 一些 .mjs + C 测试）
docs/                 文档；每篇都是英文 .md + 中文 .zh_CN.md 一对
```

## 5. 常用命令

```bash
# 只跑 Python 测试（快，改 server/ 时用）
python3 -m unittest discover -s tests -p 'test_*.py'

# 跑某个文件 / 某个测试
python3 -m unittest tests.test_relay_library
python3 -m unittest tests.test_relay_library -k change_during

# 仓库检查 + 主机测试（提交前必跑，CI 上 Static checks 就是这个）
./tools/validate.sh --static

# 固件构建 + 镜像校验（要先激活 ESP-IDF 5.5.3）
./tools/validate.sh --firmware

# 完整门禁
./tools/validate.sh
```

注意：本机没装 `pytest`，用 `python3 -m unittest`；macOS 上也没有 `timeout` 命令。

### 改了 server 代码，想马上在真 App 里试

不用等打包，用源码直接跑电脑客户端的核心（读写已安装 RooTV.app 的真实数据）：

```bash
tools/dev_desktop.sh start     # 退出 RooTV.app，备份数据库，从源码启动
tools/dev_desktop.sh restart   # 改完代码后重启
tools/dev_desktop.sh log       # 看日志
tools/dev_desktop.sh stop      # 停掉，再打开 RooTV.app 回到正式版
```

## 6. 运行时的数据和日志（排查问题用）

| 东西 | 位置 |
| --- | --- |
| 数据文件夹 | `~/Library/Application Support/RooTV/` |
| 片库来源 / 索引 | 上面目录里的 `library.json`、`library-index.json` |
| 数据库 | `tv.db`（SQLite） |
| 日志 | `~/Library/Logs/RooTV/core.log`（源码运行时是 `core-dev.log`）。数据文件夹里的 `logs/` 是旧的，别看错 |
| 网盘组件 OpenList | 本机 `127.0.0.1:47444`，WebDAV 在 `/dav`；管理员密码在 `secrets/openlist-admin` |
| 控制台 | `http://localhost:8080` |

`secrets/` 里的东西**永远不要**提交、贴到聊天或发给外部服务。

## 7. 提交和发布

**提交**（详见 `docs/contribution/commit-and-pr.md`）：
- 英文、Conventional Commit：`fix(library): ...`、`feat(console): ...`，标题不超过 72 字符。
- 用户看得到的变化要写进 `docs/CHANGELOG.md` **和** `docs/CHANGELOG.zh_CN.md` 的「Unreleased / 未发布」。
- **英文 .md 里不能出现中文正文**（包括举例的中文文件名），否则 CI 的 Static checks 会挂。中文写在 `.zh_CN.md` 里。

**发版流程**（以 v0.3.15-beta 为例）：
1. 提交一个 `Release 0.3.15-beta`：改 `desktop/__init__.py` 的 `__version__`、`server/selfhost/version.py` 的 `VERSION`（写 `0.3.15-beta.dev`），两个 CHANGELOG 在「未发布」下面加 `## v0.3.15-beta` 标题。
2. 打标签 `v0.3.15-beta`，推 `main` 和标签到 `fork`。
3. GitHub Actions 的 **Build desktop client** 自动打出 Mac（arm64/x86_64）、Windows、Linux 安装包，发布到 GitHub Release（约 15 分钟）。
4. 用 `python3 tools/publish_update.py v0.3.15-beta` 签名更新清单、上传七牛 CDN，已装的客户端才会自动更新。签名密钥和七牛配置在负责人电脑的 `~/.config/xiaozhi-tv/` 下，不在仓库里。

**推送、打标签、发 Release、传 CDN 都是对外发布**，每一步都要先得到负责人同意。只能在本地提交，不要让 Claude 自己推。

## 8. 踩过的坑（2026-11 记录）

- **百度网盘加不上**：在 OpenList 取令牌页（api.oplist.org）必须打开「使用 OpenList 提供的参数」再取刷新令牌。关掉的话令牌属于别的 AppKey，RooTV 续不了，会报"令牌不对或过期了"。
- **网盘里新存的片子片库里没有**：先看 `core.log` 里"片库「网盘」扫完了：N 个视频"。v0.3.15 之前，扫描进行中加网盘，新网盘会漏掉；已经修好（`Library.scan(again=True)`）。
- **片名识别**在 `server/relay/library.py` 的 `parse_name()`。改了识别规则要把 `PARSE_VERSION` 加一，下次扫描会重新识别所有条目。改之前最好拿真实的 `library-index.json` 新旧对比一遍，看有没有误伤别的片名。
- **网络测试**：直播源、网盘速度跟所在网络关系很大，开了海外代理时测出来的不代表国内用户的情况，要在国内网络下复测。
- Mac 安装包没签名，第一次打开要在「系统设置 → 隐私与安全性」点「仍要打开」。

## 9. 给 Claude 的协作约定

- 回复用简体中文；代码、提交信息、英文文档用英文。
- 改代码前先 `git status --short --branch`，不要覆盖或清理跟任务无关的改动。
- 先写测试再修 bug；交付时分开报告 Build / Host tests / Device tests 是 PASS、FAIL 还是 NOT RUN，没在硬件上验证过的要明说。
