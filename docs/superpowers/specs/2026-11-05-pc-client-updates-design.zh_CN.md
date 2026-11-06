<p align="right">
  <strong>简体中文</strong> · <a href="2026-11-05-pc-client-updates-design.md">English</a>
</p>

# 电脑客户端的更新 —— 设计

状态：2026-11-05 主人逐段确认。

## 1. 目标和决定

主人每发一个新版本，装好的电脑客户端（小鼠电视 / RooTV）都能知道，在后台下好，有人点「更新并重启」时装上。

- 范围：只做电脑客户端，不管电视固件。
- 下载源：先七牛云 Kodo 镜像，再 fork 的 GitHub Releases。两个都只当搬运：只认主人 ed25519 私钥签过名的清单。
- 行为：每天查一次，后台下载、校验，提示「新版本 x.y.z 已就绪」；点一下装好并重启。重启前有人在看会先问。
- 做法：核心查和下（`server/selfhost/updater.py`），托盘装（它本来就管生命周期），用一个小的替换脚本。

## 2. 版本清单和签名

`rootv-update.json`，每次发版一份：

```json
{"version": "0.3.9-beta", "date": "2026-11-10", "notes": "……这一版的中文更新记录……",
 "files": {"macos-arm64": {"name": "RooTV-0.3.9-beta-macos-arm64.dmg", "size": 123456789, "sha256": "…"},
           "macos-x86_64": {…}, "windows-x64": {…安装包…}, "windows-x64-portable": {…zip…},
           "linux-x86_64": {…}}}
```

- 旁边的 `rootv-update.json.sig`：ed25519 签名（项目的 `server/ed25519.py`），用专门的更新签名密钥 `~/.config/xiaozhi-tv/update-signing.key`（0600），和频道包那把分开；公钥内置在客户端里。
- 只认：签名对、版本比正在跑的新（`0.4.0` 新于 `0.4.0-beta`）、日期不早于上次认过的清单（不能拿旧清单骗着降级）。
- 精简版只认 `<平台>-lite` 的条目。
- 地址：七牛 `https://<七牛域名>/rootv/latest/rootv-update.json`（安装包在 `/rootv/<版本>/`），再 GitHub `https://github.com/rooma1989/ai-passport-tv/releases/latest/download/rootv-update.json`。七牛域名是打包时填的常量，空着就只用 GitHub。

## 3. 查和下（核心，`server/selfhost/updater.py`）

- 启动 10 分钟后查一次，之后每 24 小时；设置里有「立即检查」。
- GitHub 走「设置 → YouTube」里的代理（有的话）；七牛直连。
- 只下本平台的文件，放进 `<数据目录>/updates/<版本>/`，边下边算 SHA-256，能断点续传；SHA-256 不对就删掉重下（3 次）；磁盘不够不下。
- 状态 `latest`、`checking`、`downloading`（带进度）、`ready`、`failed`（带原因），给托盘（控制接口 `/local/v1/update`）和管理员（`GET /api/update`、`POST /api/update/check`、`POST /api/update/install`）。网页上只给管理员一条提示：「新版本 x.y.z 已就绪 · 看看改了什么 · 更新并重启」。
- 从源代码跑（`tools/dev_desktop.sh`）和 Docker 自建都不查。

## 4. 安装和回滚（托盘）

1. 看有没有人在看：控制接口的 `watching` 和小电视的状态；有人在看先问（从网页点的就在网页上问）。
2. 托盘对着签过名的清单再算一遍下好的文件的 SHA-256。
3. 把替换脚本拷进 `updates/`，后台启动它，停掉核心、自己退出。
   - **macOS**：等旧进程退出 → 挂载 DMG → `ditto` 把新的 RooTV.app 拷到旧的旁边 → 旧的改名 `.old` → 新的换上去 → 卸载 → 打开。新 app 的控制接口能连上后删 `.old`；60 秒内连不上就换回来、打开旧的，托盘说更新没成功。app 所在的文件夹写不了就改为打开 DMG。
   - **Windows 安装版**：运行安装包 `/VERYSILENT /SUPPRESSMSGBOXES /NORESTART`（装在 Program Files，UAC 问一次）；失败时 Inno Setup 自己回滚，装好后启动新版本。
   - **Windows 免安装版**（程序旁边没有卸载程序）和 **Linux**：解压到临时目录，改名替换，启动；失败换回来。写不了就告诉人下载在哪。
4. 新版本第一次启动清掉旧的下载，日志记「从 x 更新到 y」。
5. 数据、设置、数据库都在程序外面，不动（启动时本来就备份数据库）。

## 5. 发版（主人自己的电脑）

- 第一次：`python3 tools/publish_update.py keygen` 生成更新签名密钥、显示公钥，公钥写进代码。要备份。
- 每次发版，CI 打完包后：`python3 tools/publish_update.py v0.3.9-beta`：
  从 Release 下 `SHA256SUMS-desktop` 和安装包并核对；从 `docs/CHANGELOG.zh_CN.md` 取更新说明；生成清单并签名；清单和签名传到 GitHub Release（`gh`）；安装包、再是签名、最后是清单传到七牛（`rootv/<版本>/`、`rootv/latest/`），密钥从 `~/.config/xiaozhi-tv/qiniu.json` 读（标准库调上传 API，不装 SDK）。
- `--dry-run` 只生成、检查，不上传；`--skip-qiniu` 只发 GitHub。不做灰度。

## 6. 测试

- 单元：版本先后（beta、精简版）、签名（改一个字节、换一把钥匙）、不能降级、两个源依次试、续传、SHA-256 重下、状态变化、源代码里不查、发版命令对着假的 Release 和假的七牛、替换脚本的参数。
- macOS 实测：临时目录里放一个假的「旧」app，本机做一个「新」DMG，本机测试服务器当下载源——下载、更新、替换、重启、退回各一遍。不碰主人装好的 app。
- 不跑的：Windows、Linux 的替换脚本（照文档写，只做静态检查，文档里写明）；真的七牛上传等主人填好七牛配置、发下一版时验证。
