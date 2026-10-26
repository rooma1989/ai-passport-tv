"""启动横幅：在哪儿打开、设置码、给手机扫的终端二维码、小电视上填什么地址。

设计见 docs/superpowers/specs/2026-10-15-self-hosting-design.md 第 4.5 节。设置码是日志里唯一会出现的
秘密：只能用一次，管理员建好就作废；二维码把它放在网址 # 后面，浏览器不会把它发给服务器。
"""

from __future__ import annotations

from ..relay import qr
from .bootstrap import show_code

THIS_COMPUTER = "这台电脑的地址"


def terminal_qr(text: str) -> list[str]:
    """用半格方块画的二维码：一个字符两行模块，浅色是方块（黑底终端上扫得出来），四周留两格白边。"""
    code = qr.QrCode(text)
    border = 2
    size = code.size + 2 * border

    def light(x: int, y: int) -> bool:
        inside = border <= x < size - border and border <= y < size - border
        return not (inside and code.modules[y - border][x - border])

    lines = []
    for y in range(0, size, 2):
        row = []
        for x in range(size):
            top, bottom = light(x, y), y + 1 < size and light(x, y + 1)
            row.append("█" if top and bottom else "▀" if top else "▄" if bottom else " ")
        lines.append("".join(row))
    return lines


def lines(address: str, lan_port: int, code: str, device_address: str, in_container: bool) -> list[str]:
    """横幅的每一行。address：本机的局域网地址（容器里不知道时为空）。"""
    host = address or (THIS_COMPUTER if in_container else "127.0.0.1")
    port = "" if lan_port == 80 else f":{lan_port}"
    base = f"http://{host}{port}"
    out = ["", "小鼠电视服务器已启动。"]
    if code:
        out += [f"  用家里网络上的手机或电脑打开：{base}/tv/setup",
                f"  设置码：{show_code(code)}   （只能用一次；用来建管理员）"]
        if address:
            out += ["  或者用手机扫这个码（设置码已经填好）："] + \
                   ["    " + line for line in terminal_qr(f"{base}/tv/setup#{code}")]
        elif in_container:
            out += [f"  （容器里看不到局域网地址：把「{THIS_COMPUTER}」换成运行 Docker 的电脑或 NAS 在家里网络中的 IP）"]
    else:
        out += [f"  手机遥控：{base}/tv/    电脑版：{base}/tv/console    总后台：{base}/tv/admin"]
    # 本机回环地址（只在本机试用时）设备连不到，不给它看。
    shown = device_address or (address if address and not address.startswith("127.") else THIS_COMPUTER)
    out += [f"  小电视上的服务器地址填：{shown}（不用写端口）", ""]
    return out


def flash_lines(lan_port: int, firmware, in_container: bool) -> list[str]:
    """网页刷机：网页串口只在安全的页面（https 或 localhost）上能用，所以要在服务器那台电脑上用 localhost 打开。"""
    port = "" if lan_port == 80 else f":{lan_port}"
    out = [f"  刷固件：在这台电脑上用 Chrome 或 Edge 打开 http://localhost{port}/flash/",
           "          （网页串口只在 localhost 或 https 网址上能用；用局域网 IP 打开不行，小电视的 USB 线也要插在这台电脑上）"]
    if firmware is None:
        where = "数据文件夹的 firmware/" if in_container else "数据文件夹的 firmware/，或运行 ./run.sh --fetch-firmware"
        out.append(f"          还没有固件文件：把发布里的 xiaozhi-tv-flasher.zip 解压进{where}")
    return out + [""]
