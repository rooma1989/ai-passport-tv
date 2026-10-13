"""设备上的「恢复登录」，电视服务一侧：设备要，电视服务替主人要一个一次性手机登录链接，发回设备。

谁都没登录着的时候（手机丢了、浏览器数据清了、电脑也没登录），设备本身就是信任的根：它按自己的
密钥连上来（gate.py），能在它跟前做出手势的人就是主人身边的人。

协议（只给 HELLO 里报了 "recover": 1 的固件，多用户模式、配了网站地址时 CONFIG 才给 "recover": 1）：
- 设备 → 电视服务：STATUS {"recover": 1}（状态页上的手势）。
- 电视服务 → 设备：SWITCH {"recover": {"url": "HTTPS://<网站>/TV/P/<令牌>", "ttl": 300}}，
  或 {"recover": {"err": "limited" | "unbound" | "off" | "fail"}}。
设备全屏显示网址的二维码（全大写，字母数字模式）和倒计时；手机相机扫码打开 /TV/P/<令牌> 落地页，
点按钮才用掉（phonelinks.py，和电脑版「让手机登录」同一种链接，origin=device）。

限流、审计在 phonelinks.create_for_device：每台设备每小时 3 个；phone_link.device、login.phone.recover。
日志里不写令牌。
"""

from __future__ import annotations

import re

from .phonelinks import NotBound, PhoneLinks
from .tickets import Limited

# 网址最长多少：固件 AV_RECOVER_URL_MAX，两边一样（tools/check_config_agreement.py 核对）。
# 96 个字母数字在二维码第 4 版（低纠错 114）以内；设备最多画到第 5 版。
RECOVER_URL_MAX = 96
_URL = re.compile(r"^HTTPS?://[A-Z0-9.:/%-]+$")
ERRORS = ("limited", "unbound", "off", "fail")


def link_url(site: str, token: str) -> str:
    """HTTPS://<网站>/TV/P/<令牌>，全大写；放不下或网站地址不像样时空串。"""
    site = (site or "").rstrip("/").upper()
    url = f"{site}/TV/P/{token}" if site and token else ""
    return url if len(url) <= RECOVER_URL_MAX and _URL.match(url) else ""


def enabled(accounts) -> bool:
    """多用户模式下配了网站地址（--public-url 记在库里）才能恢复登录：二维码要指向网站。"""
    return bool(link_url(accounts.meta("public_url", ""), "A" * 26))


def answer(accounts, device_id: str, ip: str = "", logger=print) -> dict:
    """设备要恢复登录：SWITCH 的载荷 {"recover": {...}}。"""
    site = accounts.meta("public_url", "")
    if not device_id or not enabled(accounts):
        return {"recover": {"err": "off"}}
    try:
        made = PhoneLinks(accounts).create_for_device(device_id, ip)
    except NotBound:
        logger(f"device {device_id} recover refused: not bound")
        return {"recover": {"err": "unbound"}}
    except Limited:
        logger(f"device {device_id} recover refused: rate limited")
        return {"recover": {"err": "limited"}}
    except Exception as error:      # noqa: BLE001 —— 要不到不该断掉会话
        logger(f"device {device_id} recover failed: {type(error).__name__}")
        return {"recover": {"err": "fail"}}
    logger(f"device {device_id} recover link issued for user {made['user_id']}")
    return {"recover": {"url": link_url(site, made["token"]), "ttl": made["ttl"]}}
