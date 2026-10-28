"""YouTube 本机模式的 yt-dlp 自动更新（家庭服务器和电脑客户端；云端、「自己的节点」不开）。

YouTube 改得勤，旧的 yt-dlp 几周就不灵了；以前只有在「设置 → YouTube」点「更新 yt-dlp」才会换。现在中转在后台：

- 每天最多问一次 GitHub 有没有新版（CHECK_EVERY_S，记在 youtube/bin/autoupdate.json 里，重启不重问）；
- 节点说最近一次解析失败、而且像是 yt-dlp 跟不上了（ytnode.looks_broken：Unable to extract、Sign in to confirm、
  HTTP Error 403……，/yt/health 的 broken）时马上问一次，这种「失败后立刻问」至多每 FAILURE_GAP_S 一次；
- 有新版就按手动更新一样的办法装（ytcomponents：只认那个版本 SHA2-256SUMS 里的哈希、--version 跑得起来），
  旧版本先留着；换之前等节点空下来（/yt/health 的 busy：正在起播、解析时不换，至多等 QUIET_WAIT_S）；
- 换好后自检：解析一次测试视频（和设置页的「测试」一样，/yt/check）。能解析（ok，或 YouTube 要求登录——那是
  cookies 的事，yt-dlp 本身好的）就删掉旧版本；解析出错就换回旧版本，这个版本记成坏的不再自动装（等更新的）；
  连不上 YouTube 没法判断时下一轮再测，SELFTEST_TRIES 次都测不了就先用新版。

照设置来：config.toml 的 [maintenance] update_check = false 时不自动更新；只在本机模式、组件装好了时做；
经 [youtube] proxy 下载；镜像用上次在设置页手动安装、更新、检查时填的那个（记在 autoupdate.json）。
日志只写版本号和原因，不写代理地址。
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

from .bilibili import SourceError

CHECK_EVERY_S = 24 * 3600
FAILURE_GAP_S = 6 * 3600
# 有新版却没装上（下载失败、一直在起播）：过这么久再试，不等到明天。
RETRY_S = 3600
# 后台多久看一次；启动后先等一会儿（别和启动时的下载、节点的第一次检查挤在一起）。
TICK_S = 300
FIRST_TICK_S = 180
HEALTH_TIMEOUT_S = 5
# 换 yt-dlp 前等节点空下来：至多等这么久，每隔这么久问一次。
QUIET_WAIT_S = 180
QUIET_POLL_S = 3
SELFTEST_TIMEOUT_S = 55
SELFTEST_TRIES = 3
STATE_FILE = "autoupdate.json"
BAD_KEEP = 10


class Updater:
    """relay._yt_updater：后台线程每 TICK_S 秒 tick() 一次。parts：ytsettings.components(relay)。"""

    def __init__(self, relay, parts, path: Path, clock=time.time, sleep=time.sleep, config=None):
        self.relay = relay
        self.parts = parts
        self.path = Path(path)
        self.clock = clock
        self.sleep = sleep
        # config()：(update_check, proxy)。默认读 config.toml（ytsettings._config），测试换掉。
        self.config = config or self._config
        self.lock = threading.Lock()
        self.state = self._load()

    # ---- 记下来的 ----

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        bad = data.get("bad")
        return {"checked_at": float(data.get("checked_at") or 0), "failure_at": float(data.get("failure_at") or 0),
                "failure_seen": float(data.get("failure_seen") or 0),
                "bad": [str(v) for v in bad][-BAD_KEEP:] if isinstance(bad, list) else [],
                "mirror": str(data.get("mirror") or ""), "tries": int(data.get("tries") or 0),
                "last": str(data.get("last") or ""), "last_at": float(data.get("last_at") or 0)}

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps(self.state, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            pass

    def _note(self, message: str) -> None:
        """记下最近一次自动更新的结果（设置页显示），也写日志。"""
        with self.lock:
            self.state["last"], self.state["last_at"] = message, self.clock()
            self._save()
        self.relay.log(f"yt-dlp 自动更新：{message}")

    def remember_mirror(self, mirror: str) -> None:
        with self.lock:
            if self.state["mirror"] != mirror:
                self.state["mirror"] = mirror
                self._save()

    def _config(self) -> tuple[bool, str]:
        from .ytsettings import _config
        config, _ = _config(self.relay)
        return bool(config.update_check), config.youtube_proxy

    # ---- 什么时候做 ----

    def enabled(self) -> tuple[bool, str]:
        """(开着没有, 没开的原因)。"""
        if getattr(self.relay, "youtube_kind", "") != "local":
            return False, "只在本机模式自动更新"
        if not self.parts.assets:
            return False, "这台电脑用不了本机模式"
        if not self.config()[0]:
            return False, "已关闭（config.toml 的 [maintenance] update_check = false）"
        return True, ""

    def _client(self):
        return self.relay.clients.get("youtube")

    def _health(self) -> dict | None:
        client = self._client()
        if client is None:
            return None
        try:
            health = client.health(timeout=HEALTH_TIMEOUT_S)
        except SourceError:
            return None
        return health if isinstance(health, dict) else None

    def quiet(self) -> bool:
        """节点空下来了（没有 yt-dlp 在跑、在排队）：可以换 yt-dlp。至多等 QUIET_WAIT_S 秒。"""
        waited = 0.0
        while True:
            health = self._health()
            if health is not None and not health.get("busy"):
                return True
            if waited >= QUIET_WAIT_S:
                return False
            self.sleep(QUIET_POLL_S)
            waited += QUIET_POLL_S

    def tick(self) -> None:
        if not self.enabled()[0]:
            return
        if self.parts.snapshot().get("state") == "working" or not self.parts.ready() or self.parts.legacy():
            return      # 正在装 / 还没装好：交给安装（ytsettings.autostart、设置页）
        if self.parts.previous() is not None:
            self.selftest()
            return
        health = self._health()
        now = self.clock()
        with self.lock:
            daily = now - self.state["checked_at"] >= CHECK_EVERY_S
            seen = float((health or {}).get("at") or 0)
            failing = bool(health) and bool(health.get("broken")) and health.get("state") in ("error", "login") \
                and seen > self.state["failure_seen"] and now - self.state["failure_at"] >= FAILURE_GAP_S
            if not (daily or failing):
                return
            if failing:
                self.state["failure_seen"], self.state["failure_at"] = seen, now
            self.state["checked_at"] = now
            self._save()
            mirror, bad = self.state["mirror"], list(self.state["bad"])
        if failing:
            self.relay.log("YouTube 解析失败，像是 yt-dlp 跟不上了：马上查有没有新版 yt-dlp")
        self._update(mirror, bad)

    def _update(self, mirror: str, bad: list[str]) -> None:
        from ..selfhost.ytcomponents import ComponentError
        proxy = self.config()[1]
        try:
            found = self.parts.check_update(proxy, mirror)
        except ComponentError as error:
            self._note(f"查新版失败：{error}")
            return
        if not found.get("update"):
            with self.lock:
                self.state["last"], self.state["last_at"] = f"{found.get('current')} 已是最新", self.clock()
                self._save()
            return
        latest = found.get("latest") or ""
        if latest in bad:
            return      # 这个版本自检没通过过：等更新的
        self.relay.log(f"yt-dlp 有新版 {latest}（现在 {found.get('current')}），自动更新")
        if not self.parts.start("update", proxy, mirror, wait=True, keep_previous=True, gate=self.quiet):
            return
        snapshot = self.parts.snapshot()
        if snapshot.get("state") != "done":
            with self.lock:
                self.state["checked_at"] = min(self.state["checked_at"], self.clock() - CHECK_EVERY_S + RETRY_S)
            self._note(f"没装上 {latest}：{snapshot.get('error') or '原因不明'}")
            return
        if self.parts.previous() is None:
            return
        with self.lock:
            self.state["tries"] = 0
            self._save()
        self.selftest()

    def selftest(self) -> None:
        """解析一次测试视频：新版能用就删掉旧的，出错就换回旧的。"""
        version = self.parts.installed().get("yt-dlp", {}).get("version", "")
        client = self._client()
        try:
            result = client.check(timeout=SELFTEST_TIMEOUT_S) if client is not None else {"state": "slow"}
        except SourceError:
            result = {"state": "slow"}
        state = result.get("state") if isinstance(result, dict) else "error"
        self.relay._youtube_health = None
        if state in ("ok", "login"):
            self.parts.commit()
            self._note(f"已更新到 {version}，自检通过")
            return
        if state in ("network", "slow"):
            with self.lock:
                self.state["tries"] += 1
                tries = self.state["tries"]
                self._save()
            if tries >= SELFTEST_TRIES:
                self.parts.commit()
                self._note(f"已更新到 {version}；连不上 YouTube，没法自检，先用新版")
            return
        back = self.parts.rollback()
        if not back:
            return
        with self.lock:
            if version and version not in self.state["bad"]:
                self.state["bad"] = (self.state["bad"] + [version])[-BAD_KEEP:]
            self._save()
        self._note(f"新版 {version} 自检没通过（{state}），已换回 {back}")

    def snapshot(self) -> dict:
        """设置页显示的：{"enabled", "reason", "checked_at", "last", "last_at"}。"""
        on, reason = self.enabled()
        with self.lock:
            return {"enabled": on, "reason": reason, "checked_at": self.state["checked_at"],
                    "last": self.state["last"], "last_at": self.state["last_at"]}

    def run_forever(self) -> None:
        self.sleep(FIRST_TICK_S)
        while True:
            try:
                self.tick()
            except Exception as error:      # noqa: BLE001 —— 后台线程不能死：记下来，下一轮再试
                self.relay.log(f"yt-dlp 自动更新出错：{type(error).__name__}")
            self.sleep(TICK_S)


def updater(relay) -> Updater | None:
    """这个中转进程里唯一的自动更新（自建 / 电脑客户端才有）。"""
    found = getattr(relay, "_yt_updater", None)
    if found is None and getattr(relay, "selfhost", None) is not None \
            and getattr(relay, "selfhost_data", None) is not None:
        from . import ytsettings
        layout = ytsettings._paths(relay)
        found = Updater(relay, ytsettings.components(relay), layout.youtube_bin / STATE_FILE)
        relay._yt_updater = found
    return found


def attach(relay, start: bool = True) -> Updater | None:
    """自建 / 电脑客户端：建自动更新，本机模式时在后台跑（模式改了中转会重启，重启后再看）。"""
    found = updater(relay)
    if found is None or getattr(relay, "youtube_kind", "") != "local":
        return found
    if start:
        threading.Thread(target=found.run_forever, name="yt-autoupdate", daemon=True).start()
    return found
