"""扫未绑定设备的绑定码，自己注册（不用管理员邀请）。

为什么可以：未绑定的设备在屏幕上（「扫码绑定」静止画面、状态页二维码）给出绑定码（云端 20 位，
自建 6 位数字）HTTPS://<网站>/TV/B/<码>。看得到它的人就站在设备跟前——设备本身就是信任的根。还没登录的手机
扫了它，落地页给「创建账号并绑定这台设备」：只问一个名字（默认「我」），一点就建一个普通用户、
这台手机得到普通的成员手机会话（180 天）、设备归这个新用户。已经登录的手机还是原来的「绑定到我的账号」。

绑定码和以前一样只能用一次（绑定后清掉）。限流：每个网段每小时最多建 3 个账号，全中转每小时 30 个；
只有真建成了才计数（码不对不算：20 位的码猜不中；自建的 6 位数字由中转的错码限流挡，
server/tenancy/claimguard.py）。建账号和绑定都记审计（user.signup、device.bind）。
"""

from __future__ import annotations

from . import db
from .tickets import Limited, RateLimit

# 每个网段、全中转：一小时里最多建这么多个账号。只在内存里，中转重启清零。
NET_LIMIT = (3, 3600)
ALL_LIMIT = (30, 3600)


class SignUp:
    def __init__(self, accounts):
        self.accounts = accounts
        self.net_limit = RateLimit(*NET_LIMIT, clock=accounts.clock)
        self.all_limit = RateLimit(*ALL_LIMIT, clock=accounts.clock)

    def register(self, claim: str, name: str, ip: str = "", user_agent: str = "") -> tuple[int, str, dict]:
        """建账号并绑定：(用户 id, 会话, 设备)。太频繁时 Limited，码不对时 ValueError（消息给人看）。"""
        net = db.ip_prefix(ip) or "?"
        if self.net_limit.blocked(net) or self.all_limit.blocked("*"):
            raise Limited("这个网络里新建的账号太多了，请一小时后再试，或者请家里已经有账号的人来绑定")
        made = self.accounts.register_with_claim(claim, name, user_agent, ip)
        self.net_limit.hit(net)
        self.all_limit.hit("*")
        return made
