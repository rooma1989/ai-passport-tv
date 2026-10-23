"""受信任的网络（电脑客户端，--desktop）：不在家里的网络上时，网页和设备端口只听本机。

设计见 docs/superpowers/specs/2026-10-19-pc-client-architecture-design.md 第 5.11 节。笔记本带到咖啡馆，
不该在那里开着 8080/8097/8098。家里的网络按路由器认（默认网关 MAC 的哈希，tools/netcheck.py，托盘也用它）：

- 用户在托盘里点「这是我家的网络」（POST /local/v1/network/trust {"id", "trusted": true}），记进 desktop.json；
- 说「不是」（trusted: false）的也记下，那里一直只听本机；
- 判断用 netcheck.lan_decision：还没记过任何家里的网络时照常开着（第一次运行几乎总在家里），托盘问一句；
  记过以后，只有在记下的网络上才开，别处（认不出路由器时也算）只听 127.0.0.1。

每 CHECK_S 秒在后台线程里看一次网关；换了网络、开关变了，就叫监督进程按新的地址重启子进程
（on_change → Supervisor.rebuild_requested）。读数要连续两次一样才算数：ARP 表一时没有网关的条目、
Wi-Fi 重连的几秒钟，不该让服务重启两次。

同一个线程也看这台电脑在家里网络中的地址（AddressWatch）：DHCP 换了地址、换了 Wi-Fi 时，按新的地址重建子进程，
中转给声波配网页填的地址、网站地址跟着变，不用退出程序。

Windows 上同一个线程每 PROFILE_S 秒看一次网络类型：家里的 Wi-Fi 被标成「公用」时，安装程序加的防火墙规则
（只对「专用」）不起作用，设备连不进来；这时状态里多一个 network_public 问题（只在端口开着时说，
咖啡馆的公用网络本来就该挡住）。只取类型，不取网络的名字。

config.toml 的 bind 是 127.0.0.1 时，这里什么都不打开；不带 --desktop 的启动（Docker、云端）不用这个模块。
"""

from __future__ import annotations

import re
import sys
import threading
import time

LOOPBACK = "127.0.0.1"
CHECK_S = 15.0
PROFILE_S = 60.0
MAX_REMEMBERED = 32
NETWORK_ID = re.compile(r"^[0-9a-f]{16}$")

PUBLIC_MESSAGE = ("Windows 把现在连着的网络标成了「公用」，小电视和手机连不进来：在 设置 > 网络和 Internet 里"
                  "把它改成「专用」")
AWAY_MESSAGE = "不在家里的网络上：网页和小电视的端口只对这台电脑开放"


def valid_id(value) -> bool:
    return isinstance(value, str) and bool(NETWORK_ID.match(value))


class TrustedNetworks:
    """configured_bind：config.toml 的 bind（或 --bind）。settings：desktop.Settings（存 trusted / untrusted）。
    identify、profiles：换掉它们来测试（默认是 tools/netcheck 里真的去问系统的函数）。"""

    def __init__(self, configured_bind: str, settings, log=print, identify=None, profiles=None,
                 platform_name: str | None = None, clock=time.monotonic):
        from tools import netcheck
        self.netcheck = netcheck
        self.configured_bind = configured_bind
        self.settings = settings
        self.log = log
        self.identify = identify or netcheck.network_identity
        platform_name = platform_name or sys.platform
        if profiles is None and platform_name == "win32":
            profiles = netcheck.windows_profiles
        self.profiles = profiles          # None：不是 Windows，不查网络类型
        self.clock = clock
        self.on_change = None             # 端口开关变了时调用（监督进程重建子进程）
        self.watchers: list = []          # 每次看网关之后也调它们的 check()（AddressWatch）
        self._lock = threading.Lock()
        self._current = ""
        self._candidate: str | None = None
        self._profile = ""
        self._next_profile = 0.0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ---- 判断 ----

    def _lists(self) -> tuple[list[str], list[str]]:
        networks = self.settings.networks()
        return networks["trusted"], networks["untrusted"]

    def decision(self) -> str:
        trusted, untrusted = self._lists()
        with self._lock:
            current = self._current
        return self.netcheck.lan_decision(trusted, current, untrusted)

    @property
    def lan_open(self) -> bool:
        return self.configured_bind != LOOPBACK and self.decision() != "closed"

    @property
    def bind(self) -> str:
        """子进程该听的地址：家里（或还没决定）时是设置里的，别处是 127.0.0.1。"""
        return self.configured_bind if self.lan_open else LOOPBACK

    def snapshot(self) -> dict:
        """状态里的 network 一项：{"id", "trusted", "profile", "lan_open"}。trusted 为 None：还没决定。"""
        trusted, untrusted = self._lists()
        with self._lock:
            current, profile = self._current, self._profile
        mark = True if current and current in trusted else False if current and current in untrusted else None
        return {"id": current, "trusted": mark, "profile": profile, "lan_open": self.lan_open}

    def problems(self) -> list[dict]:
        with self._lock:
            profile = self._profile
        if self.configured_bind == LOOPBACK:
            return []
        if not self.lan_open:
            return [{"code": "network_away", "message": AWAY_MESSAGE}]
        if profile == "public":
            return [{"code": "network_public", "message": PUBLIC_MESSAGE}]
        return []

    # ---- 托盘的回答 ----

    def trust(self, network_id: str, trusted: bool) -> dict:
        """记下（或忘掉）一个网络；端口开关因此变了就通知。返回新的 snapshot。"""
        before = self.lan_open
        trusted_ids, untrusted_ids = self._lists()
        trusted_ids = [i for i in trusted_ids if i != network_id]
        untrusted_ids = [i for i in untrusted_ids if i != network_id]
        (trusted_ids if trusted else untrusted_ids).append(network_id)
        self.settings.set_networks(trusted_ids[-MAX_REMEMBERED:], untrusted_ids[-MAX_REMEMBERED:])
        self.log("记下了家里的网络" if trusted else "记下了：这不是家里的网络")
        self._changed(before)
        return self.snapshot()

    # ---- 看网关 ----

    def check(self, initial: bool = False) -> bool:
        """读一次网关。initial：启动时，读到就算数（认不出时隔一秒再试一次）。返回端口开关有没有变。"""
        before = self.lan_open
        try:
            seen = self.identify()
        except Exception:       # noqa: BLE001 —— 认不出路由器就当作不知道
            seen = ""
        if initial and not seen:
            time.sleep(1.0)
            try:
                seen = self.identify()
            except Exception:   # noqa: BLE001
                seen = ""
        with self._lock:
            if initial or seen == self._current:
                self._current, self._candidate = seen, None
            elif seen == self._candidate:
                self._current, self._candidate = seen, None
            else:
                self._candidate = seen
                return False
        if not initial:
            return self._changed(before)
        return False

    @property
    def current_id(self) -> str:
        """现在认出的路由器（空串：认不出）。"""
        with self._lock:
            return self._current

    def check_profile(self) -> None:
        if self.profiles is None or self.clock() < self._next_profile:
            return
        self._next_profile = self.clock() + PROFILE_S
        try:
            category = self.netcheck.profile_category(self.profiles())
        except Exception:       # noqa: BLE001 —— 只是提示
            category = ""
        with self._lock:
            changed, self._profile = category != self._profile, category
        if changed and category == "public":
            self.log("Windows 把现在的网络标成了公用网络：设备连不进来")

    def _changed(self, before: bool) -> bool:
        after = self.lan_open
        if after == before:
            return False
        self.log("在家里的网络上：网页和小电视的端口对家里开放" if after else AWAY_MESSAGE)
        callback = self.on_change
        if callback is not None:
            callback()
        return True

    def start(self) -> None:
        def loop() -> None:
            while True:
                try:
                    self.check_profile()
                except Exception as error:      # noqa: BLE001 —— 这个线程不能死
                    self.log(f"查网络类型出错：{type(error).__name__}")
                if self._stop.wait(CHECK_S):
                    break
                try:
                    self.check()
                except Exception as error:      # noqa: BLE001
                    self.log(f"查网络出错：{type(error).__name__}")
                for watcher in list(self.watchers):
                    try:
                        watcher.check()
                    except Exception as error:  # noqa: BLE001
                        self.log(f"查本机地址出错：{type(error).__name__}")
        # 网络类型第一次也在线程里查（Windows 的 PowerShell 要一两秒），不拖慢启动。
        self._thread = threading.Thread(target=loop, daemon=True, name="trusted-networks")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()


class AddressWatch:
    """这台电脑在家里网络中的地址变了没有（电脑客户端，--desktop）：DHCP 换了地址、换了一个 Wi-Fi。

    每 CHECK_S 秒（和看网关同一个线程，TrustedNetworks 的 watchers）先做一次便宜的检查：UDP 探测的结果
    （不起子进程）和受信任网络认出的路由器，变了才去列网卡（netident.lan_info，macOS/Linux 起一两个命令，
    Windows 起一次 PowerShell）；另外每 FULL_S 秒无论如何列一次（代理开着 TUN 时探测结果一直是 TUN 的，
    看不出 DHCP 换了地址）。识别的地址要连续两次一样才算数，然后调 on_change(info)。
    """

    FULL_S = 300.0

    def __init__(self, current: str, detect=None, probe=None, signal=None, clock=time.monotonic, log=print):
        from .. import netident
        self.detect = detect or netident.lan_info
        self.probe = probe or netident.probe_address
        self.signal = signal or (lambda: "")
        self.clock, self.log = clock, log
        self.current = current
        self.on_change = None           # on_change(netident.LanInfo)：地址变了
        self._candidate: str | None = None
        self._cheap = None
        self._next_full = clock() + self.FULL_S

    def check(self) -> bool:
        """返回这次有没有认定地址变了（并且调了 on_change）。"""
        try:
            cheap = (self.probe(), self.signal())
        except Exception:       # noqa: BLE001 —— 便宜的检查出错就当作没变，等定时的完整检查
            cheap = self._cheap
        now = self.clock()
        if self._candidate is None and cheap == self._cheap and now < self._next_full:
            return False
        first = self._cheap is None
        self._cheap = cheap
        if first and now < self._next_full:
            return False        # 第一次只记下便宜检查的结果：启动时刚识别过
        self._next_full = now + self.FULL_S
        info = self.detect()
        found = info.address
        if found == self.current:
            self._candidate = None
            return False
        if found != self._candidate:
            self._candidate = found     # 再看一次（Wi-Fi 重连的几秒钟里地址可能还没拿到）
            return False
        self._candidate = None
        before, self.current = self.current, found
        self.log(f"这台电脑在家里网络中的地址变了：{before or '（没有）'} → {found or '（没有）'}")
        callback = self.on_change
        if callback is not None:
            callback(info)
        return True
