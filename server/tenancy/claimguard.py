"""自建模式的绑定码错码限流。

自建 / 电脑客户端的绑定码只有 6 位数字（一百万种，db.new_short_claim），手机上好输，但能被挨个试。
所以凡是收绑定码的接口（GET /api/bind/<码>、POST /api/bind、POST /api/bind/register，见
server/relay/web.py）在自建时先问这里：

- 每个 IP 10 分钟里最多错 5 次，全中转 10 分钟里最多错 30 次；到了就先停（Limited，消息给人看），
  最早的那次错满 10 分钟后自动放开。
- 刚被停住的那一下记一条操作记录（bind.locked，detail 是 ip 或 all）；之后同一段时间里不重复记。
- 全中转错满 30 次时，所有未绑定设备的绑定码都换新的（accounts.rotate_claims）：猜过的都作废，
  设备几秒内重连、屏幕上换成新码。

在内存里，中转重启清零。云端（20 位的码，100 位熵）不用它。码对了不计数。
"""

from __future__ import annotations

import threading

from .tickets import Limited, RateLimit

# (次数, 秒)
IP_LIMIT = (5, 600)
ALL_LIMIT = (30, 600)
IP_MESSAGE = "绑定码输错太多次了，请 10 分钟后再试。请对照小电视屏幕上的 6 位数字。"
ALL_MESSAGE = "最近输错绑定码的次数太多，绑定先暂停 10 分钟。小电视屏幕上的绑定码已经换了新的，请稍后用新码再试。"


class ClaimGuard:
    def __init__(self, accounts, log=print):
        self.accounts = accounts
        self.log = log
        self.per_ip = RateLimit(*IP_LIMIT, clock=accounts.clock)
        self.overall = RateLimit(*ALL_LIMIT, clock=accounts.clock)
        # 什么时候停住的：同一段停用期里只记一次审计、只换一次码。
        self._ip_locked: dict[str, float] = {}
        self._all_locked: float | None = None
        self._lock = threading.Lock()

    def check(self, ip: str) -> None:
        """还能试吗：不能时 Limited。"""
        if self.overall.blocked("*"):
            raise Limited(ALL_MESSAGE)
        if self.per_ip.blocked(ip or "?"):
            raise Limited(IP_MESSAGE)

    def failed(self, ip: str) -> None:
        """一次错码（码不存在或设备已经绑定了）。刚好到上限时记审计；全中转到上限时换码。"""
        key = ip or "?"
        with self._lock:
            self._failed(key, ip)

    def _failed(self, key: str, ip: str) -> None:
        self.per_ip.hit(key)
        self.overall.hit("*")
        now = self.accounts.clock()
        if self.per_ip.blocked(key):
            self._ip_locked = {k: t for k, t in self._ip_locked.items() if now - t < IP_LIMIT[1]}
            if key not in self._ip_locked:
                self._ip_locked[key] = now
                self.accounts.audit("bind.locked", None, ip, detail="ip")
                self.log(f"绑定码错了太多次：{key} 暂停 10 分钟")
        if self.overall.blocked("*") and (self._all_locked is None or now - self._all_locked >= ALL_LIMIT[1]):
            self._all_locked = now
            self.accounts.audit("bind.locked", None, ip, detail="all")
            rotated = self.accounts.rotate_claims(ip)
            self.log(f"绑定码错了太多次（全中转），暂停 10 分钟；{len(rotated)} 台未绑定设备换了新码")
