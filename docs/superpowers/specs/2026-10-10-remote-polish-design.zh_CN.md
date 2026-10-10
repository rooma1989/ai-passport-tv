<p align="right">
  <strong>简体中文</strong> · <a href="2026-10-10-remote-polish-design.md">English</a>
</p>

# YouTube 自动保养与换台反馈 —— 设计

状态：2026-10-10 已确认。承接
[`2026-10-10-grouped-channels-and-remote-design.zh_CN.md`](2026-10-10-grouped-channels-and-remote-design.zh_CN.md)。
手机调音量、亮度和播放控制是下一步，另写设计。

## 目标

1. YouTube 不用人盯着也能一直用：yt-dlp 自动更新，cookies 需要更换时手机页会提示。
2. 手机上点了某个台之后，页面显示小智正在切换，而不是继续把旧台显示成「播放中」。

## YouTube 自动保养

### 每周更新 yt-dlp（cstone）

- `packaging/yt-relay/update-ytdlp.sh`，由 `yt-dlp-update.timer` 每周一 04:00 运行：把最新版下载到现有程序旁边，以 `ytrelay`
  身份带上 deno 和 cookies 解析一个固定的测试视频，能用才替换。不能用就保留旧版，并在日志里写明原因。

### 健康状态

- 节点记录每次 YouTube 解析的结果：`ok`、`login`（YouTube 要求登录，也就是 cookies 过期了）或 `error`。后台每 6 小时不走缓存解析一次
  测试视频，所以即使没人在看，cookies 过期也能发现。
- `GET /yt/health`（需要密钥）回答 `{"state", "at", "version"}`，第一次解析之前 `state` 是 `unknown`。
- 中转最多每分钟问一次，配置了 YouTube 时在 `/api/state` 里加上 `"youtube": {"state"}`；节点连不上时是 `unreachable`。
- 网页在 `login` 时在所有标签页上方显示一条朱红色提示「YouTube 需要更新 cookies」，点开是导出和上传的步骤；`unreachable` 时提示
  「YouTube 暂时不可用」。

## 换台反馈

- `now-playing.json` 增加 `since`：当前会话开始的时间。中转以 `now.since` 返回；换台、投屏、重播的回答都带 `at`，即中转当时的时间。
- 点击成功后，网页记下一个「待切换」（目标台、`at`、截止时间）。在小智报告正在播目标台、且 `since >= at - 1` 之前，目标那一行显示
  琥珀色的「切换中…」，台号屏显示「正在切到 …」，网页改为每秒刷新一次（平时每 5 秒）。
- 截止时间：直播台 20 秒，UP 主和投屏 45 秒（YouTube 起播慢）。超时后提示「切换好像没成功，再点一次试试」。
- 投屏和重播都算切到 `cast`；用 `since` 和 `at` 比较，才能把新的投屏和正在播的旧投屏区分开。

## 测试

- 主机测试：节点健康状态的变化（ok、login、error、unknown）和接口的密钥检查；中转对健康状态的缓存和 `unreachable`；`since` 的写入与读取；
  回答里的 `at`；网页脚本用到的字段与接口一致。
- 服务器：手动跑一次更新脚本；确认定时任务已排上。
- 手机：点一个台，看到「切换中…」直到小智切过去；投屏一次；临时把节点的 cookies 路径指向不存在的文件，看 YouTube 提示条。
