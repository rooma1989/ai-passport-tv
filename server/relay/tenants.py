"""中转里「谁在调用」和「状态存哪儿」：单用户和多用户两种模式共用 Relay 的代码。

单用户（没有 --db）：只有一个 LEGACY 调用者，能遥控所有设备，状态就是 relay-state.json。
多用户（--db）：每个请求解析成一个 Caller —— 管理员、用户或某一台设备的访客；
每个用户的订阅和进度是数据库里的一行（UserStore），每台设备的投屏位也是一行（DeviceStore）。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Caller:
    user: int | None = None      # 用户 id；单用户模式和访客为 None
    role: str = "admin"          # admin / user / guest
    device: str | None = None    # 访客能遥控的那一台
    name: str = ""

    @property
    def key(self) -> tuple:
        """手机选定遥控哪一台：按调用者分别记。"""
        return (self.user, self.device)

    @property
    def guest(self) -> bool:
        return self.role == "guest"


# 单用户模式唯一的调用者：持有密钥或已登录的手机，能做一切。
LEGACY = Caller()


class NotFound(Exception):
    """调用者看不到的东西（别人的设备、访客碰账号接口）：一律 404，和不存在一样。"""


class UserStore:
    def __init__(self, accounts, user_id: int):
        self.accounts, self.user_id = accounts, user_id

    def load(self):
        return self.accounts.load_user_state(self.user_id) or {"creators": []}

    def save(self, data: dict) -> None:
        self.accounts.save_user_state(self.user_id, data)


class DeviceStore:
    def __init__(self, accounts, device_id: str):
        self.accounts, self.device_id = accounts, device_id

    def load(self):
        return self.accounts.load_device_state(self.device_id)

    def save(self, data: dict) -> None:
        self.accounts.save_device_state(self.device_id, data)


class NullStore:
    """没有主人的设备（不该出现）用的空状态：读出来是空的，存了也不留。"""

    def load(self):
        return {"creators": []}

    def save(self, data: dict) -> None:
        pass
