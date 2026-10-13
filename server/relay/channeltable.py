"""总后台的全局直播频道表（channels.txt）：读成表格、按表格改、检查、带备份地保存。

文件格式不变（id | 显示名 | 地址 | User-Agent（可选）），电视服务照旧读它：每个新会话开始时
live.reload_channels_if_changed() 看文件的修改时间和大小，变了就重读，所以保存后不用重启，
每台设备下次换台或重连时拿到新表（正在播的会话不受影响）。中转自己每次列台都读这个文件。

台的 id 不变：改名、挪位置都只改那一行的名字和顺序。用户在手机上调的分组、设备记住的
「上次看的台」都按 id 记，重新编号会让它们指到别的台上。新加的台取下一个没用过的 chNNN。

检查分两层：这里先用中文查表格本身（名字、地址、竖线、长度），再让电视服务自己的解析器
（live.parse_channels，和电视服务一模一样的检查）在一个子进程里过一遍。用子进程是因为 server.live
要带上 numpy 等一堆东西，中转常驻内存只有 200 MB，不值得为偶尔一次保存一直背着。

保存前把原文件复制成 channels.txt.bak-<日期-时间>（同一目录），最多留 BACKUP_KEEP 份。
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from .. import channel_groups

ID_MAX = 15                  # live.TV_CHANNEL_ID_MAX - 1
NAME_MAX = 40
URL_MAX = 400
AGENT_MAX = 120
LINE_MAX = 512               # live.MAX_CHANNEL_LINE_BYTES
BACKUP_KEEP = 30
CHECK_TIMEOUT_S = 30
HEADER = ("# 由总后台生成（/tv/admin 的「频道表」）。每行： id | 显示名 | 地址 | User-Agent（可选）\n"
          "# 顺序就是设备上换台的顺序。id 不要改：用户的分组和设备记住的台都按 id 记。\n"
          "# 手动编辑同样有效；电视服务在每个新会话开始时发现文件变了就重读，不用重启。\n\n")
_NEW_ID = re.compile(r"^ch(\d{3,6})$")
_ROOT = Path(__file__).resolve().parents[2]


def version_of(text: str) -> str:
    """文件内容的指纹：页面带着它保存，期间文件被别人改过就拒绝，免得覆盖掉。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def read(path: Path) -> dict:
    """{version, rows: [{id, name, url, agent}], bad: [看不懂的行号]}；文件不存在时 rows 为空。"""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        text = ""
    rows, bad = [], []
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [part.strip() for part in line.split("|")]
        if len(parts) not in (3, 4) or not parts[0]:
            bad.append(number)
            continue
        rows.append({"id": parts[0], "name": parts[1], "url": parts[2], "agent": parts[3] if len(parts) == 4 else ""})
    return {"version": version_of(text), "rows": rows, "bad": bad}


def next_id(used: set[str]) -> str:
    numbers = [int(m.group(1)) for m in map(_NEW_ID.match, used) if m]
    n = max(numbers, default=-1) + 1
    while f"ch{n:03d}" in used:
        n += 1
    return f"ch{n:03d}"


def _clean(value, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    return " ".join(value.split())[:limit + 1]


def apply_edits(current: list[dict], edits) -> list[dict]:
    """页面发来的新表 → 完整的行。edits 每一项：
    - "ch012"：这个台原样保留（只是位置可能变了）；
    - {"id": "ch012", "name": …, "url"?: …, "agent"?: …}：改这个台，没给的字段照旧；
    - {"name": …, "url": …, "agent"?: …}（没有 id）：新台，取一个新 id。
    原表里有、edits 里没有的台就是删掉了。格式不对时 ValueError（中文，给人看）。"""
    if not isinstance(edits, list) or not edits:
        raise ValueError("频道表不能是空的")
    by_id = {row["id"]: row for row in current}
    used = set(by_id)
    rows, seen = [], set()
    for n, item in enumerate(edits, start=1):
        if isinstance(item, str):
            item = {"id": item}
        if not isinstance(item, dict):
            raise ValueError(f"第 {n} 行格式不对")
        key = item.get("id")
        if key is not None:
            if key not in by_id:
                raise ValueError(f"第 {n} 行：没有 {key} 这个台（可能别人刚改过频道表，请刷新）")
            if key in seen:
                raise ValueError(f"第 {n} 行：{key} 出现了两次")
            row = dict(by_id[key])
        else:
            row = {"id": next_id(used), "name": "", "url": "", "agent": ""}
            used.add(row["id"])
        for field, limit in (("name", NAME_MAX), ("url", URL_MAX), ("agent", AGENT_MAX)):
            if field in item:
                value = _clean(item[field], limit)
                if value is None:
                    raise ValueError(f"第 {n} 行格式不对")
                row[field] = value
        seen.add(row["id"])
        rows.append(row)
    return rows


def problems(rows: list[dict]) -> list[str]:
    """表格本身的问题（中文，带行号）；空列表是没问题。数量和 CONFIG 大小另由调用方按每个用户算。"""
    found = []
    for n, row in enumerate(rows, start=1):
        name, url, agent = row["name"], row["url"], row["agent"]
        label = f"第 {n} 行（{name or row['id']}）"
        if not name:
            found.append(f"{label}：名字不能是空的")
        elif len(name) > NAME_MAX:
            found.append(f"{label}：名字太长（最多 {NAME_MAX} 个字）")
        if not url.startswith(("http://", "https://")):
            found.append(f"{label}：地址要以 http:// 或 https:// 开头")
        elif len(url) > URL_MAX or " " in url:
            found.append(f"{label}：地址太长或含有空格")
        if len(agent) > AGENT_MAX:
            found.append(f"{label}：User-Agent 太长")
        if any("|" in v for v in (name, url, agent)):
            found.append(f"{label}：不能含有竖线 |")
        if any(ord(c) < 32 for v in (name, url, agent) for c in v):
            found.append(f"{label}：含有看不见的控制字符")
        if len(line_of(row).encode("utf-8")) > LINE_MAX:
            found.append(f"{label}：这一行太长（最多 {LINE_MAX} 字节）")
    if len(rows) > channel_groups.CHANNEL_MAX:
        found.append(f"一共 {len(rows)} 个台，设备最多收 {channel_groups.CHANNEL_MAX} 个")
    return found


def line_of(row: dict) -> str:
    return f"{row['id']} | {row['name']} | {row['url']}" + (f" | {row['agent']}" if row["agent"] else "")


def render(rows: list[dict]) -> str:
    return HEADER + "".join(line_of(row) + "\n" for row in rows)


def media_check(text: str, timeout_s: float = CHECK_TIMEOUT_S) -> dict:
    """用电视服务的解析器（live.parse_channels）在子进程里检查：{ok, error, warnings}。"""
    try:
        done = subprocess.run([sys.executable, "-m", "server.relay.channeltable", "--check"], input=text,
                              capture_output=True, text=True, encoding="utf-8", timeout=timeout_s,
                              cwd=str(_ROOT), env=dict(os.environ, PYTHONIOENCODING="utf-8"))
        answer = json.loads(done.stdout.strip().splitlines()[-1])
    except (OSError, subprocess.SubprocessError, ValueError, IndexError) as error:
        return {"ok": False, "error": f"检查程序没能运行（{type(error).__name__}），没有保存", "warnings": []}
    if not isinstance(answer, dict):
        return {"ok": False, "error": "检查程序的回答看不懂", "warnings": []}
    if answer.get("error"):
        # 解析器说的是文件的行号（英文）：换成表格里的第几行。
        answer["error"] = "电视服务的检查没通过：" + re.sub(
            r"^line (\d+): ", lambda m: f"第 {int(m.group(1)) - HEADER.count(chr(10))} 行：", answer["error"])
    return answer


def save(path: Path, text: str, clock=time.time) -> str:
    """先备份（有原文件时）再整份替换。返回备份文件名（没有原文件时为空）。"""
    path = Path(path)
    backup = ""
    if path.is_file():
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(clock()))
        target = path.with_name(f"{path.name}.bak-{stamp}")
        n = 1
        while target.exists():
            n += 1
            target = path.with_name(f"{path.name}.bak-{stamp}-{n}")
        shutil.copy2(path, target)
        backup = target.name
        old = sorted(path.parent.glob(path.name + ".bak-*"), key=lambda p: p.stat().st_mtime)
        for extra in old[:-BACKUP_KEEP]:
            try:
                extra.unlink()
            except OSError:
                pass
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)
    return backup


def _check_main() -> int:
    """子进程：从标准输入读一份频道表，用 live 的解析器查，打印一行 JSON。

    server.live 导入时会按 TV_CHANNELS_FILE 加载一次频道表：先让它指向一个只有一个台的临时文件，
    导入不受服务器上现有文件的影响；再单独解析要查的表。live 的告警是 print 出来的，收集起来交给页面。"""
    text = sys.stdin.read()
    answer = {"ok": True, "error": "", "warnings": []}
    with tempfile.TemporaryDirectory() as tmp:
        placeholder = Path(tmp) / "channels.txt"
        placeholder.write_text("check | check | http://127.0.0.1/\n", encoding="utf-8")
        os.environ["TV_CHANNELS_FILE"] = str(placeholder)
        os.environ.pop("TV_RELAY_CHANNELS_FILE", None)
        heard = io.StringIO()
        try:
            with contextlib.redirect_stdout(heard):
                from .. import live
                heard.seek(0)
                heard.truncate()
                live.parse_channels(text)
        except (ValueError, OSError, UnicodeError) as error:
            answer = {"ok": False, "error": str(error), "warnings": []}
        lines = [line.strip() for line in heard.getvalue().splitlines() if line.strip()]
        answer["warnings"] = list(dict.fromkeys(lines))[:12]
    print(json.dumps(answer, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    if sys.argv[1:] == ["--check"]:
        sys.exit(_check_main())
    print("用法：python3 -m server.relay.channeltable --check < channels.txt", file=sys.stderr)
    sys.exit(2)
