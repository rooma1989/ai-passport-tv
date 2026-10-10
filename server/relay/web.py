"""中转的两个 HTTP 服务：给电视服务的播放流，和给手机的管理网页。

两者都只监听 127.0.0.1，且分开端口：播放流（/ch/<id>）只给本机的电视服务用，不需要密钥；
管理网页经 nginx 的 /TV/cast/ 转进来，每个请求都要带密钥，密钥不对一律 404，
外面看不出这里有服务。nginx 只转发到管理端口，所以从外网到不了播放流。
"""

from __future__ import annotations

import hmac
import json
import re
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import events, stream, table, youtube as youtube_links
from .bilibili import BilibiliError, SourceError
from .state import LIST_MAX_AGE_S, RESUME_BACK_S, State

# 一次请求里最多跳过几个放不了的视频，之后返回 503，让电视服务稍后再来。
MAX_SKIPS = 3
# 同一视频连续几次刚起播就失败（例如地址过期、被拒），就当作放不了。
MAX_QUICK_FAILURES = 3
QUICK_FAILURE_S = 5.0
MAX_BODY_BYTES = 4096
_CHANNEL = re.compile(r"^/ch/(up\d\d|cast)$")
_CREATOR = re.compile(r"^/api/creators/(up\d\d)$")


class Relay:
    """播放与管理共用的逻辑；HTTP 处理类只做解析和回写。"""

    def __init__(self, state: State, client, channels_out: Path, ffmpeg: str, key: str,
                 events_out: Path | None = None, youtube=None,
                 log=print, clock=time.time, monotonic=time.monotonic,
                 starter=stream.start, pumper=stream.pump):
        self.state = state
        # 每个网站一个客户端；YouTube 只经 cstone 节点，没配置时为 None。
        self.clients = {"bilibili": client, "youtube": youtube}
        self.events_out = Path(events_out) if events_out is not None else None
        self.channels_out = Path(channels_out)
        self.ffmpeg = ffmpeg
        self.key = key
        self.log = log
        self.clock = clock
        self.monotonic = monotonic
        self.starter = starter
        self.pumper = pumper
        self._quick_failures: dict[tuple[str, str], int] = {}
        # 刷新列表要访问 B 站，同一时间只让一个请求去做。
        self._refresh_lock = threading.Lock()

    # ---- 频道表 ----

    def write_table(self) -> None:
        with self.state.lock:
            text = table.render(self.state.creators)
        if table.write_if_changed(self.channels_out, text):
            self.log(f"频道表已更新：{len(self.state.creators)} 个 UP 主")

    # ---- 播放 ----

    def refresh_if_stale(self, cid: str) -> None:
        creator = self.state.get(cid)
        if creator is None or self.clock() - creator.fetched_at < LIST_MAX_AGE_S:
            return
        with self._refresh_lock:
            creator = self.state.get(cid)
            if creator is None or self.clock() - creator.fetched_at < LIST_MAX_AGE_S:
                return
            try:
                videos = self._client(creator.site).uploads(creator.source)
            except SourceError as error:
                # 拿不到就继续用缓存的列表。
                self.log(f"{cid} 刷新投稿列表失败，用缓存：{error}")
                return
            if videos:
                self.state.set_videos(cid, videos, self.clock())
                self.state.save()

    def open_channel(self, cid: str):
        """选出下一个视频并启动 ffmpeg；返回 (进程, 视频, 起点) 或说明原因的 HTTP 码。"""
        if self.state.get(cid) is None:
            return 404
        self.refresh_if_stale(cid)
        for _ in range(MAX_SKIPS):
            pick = self.state.next_video(cid)
            if pick is None:
                return 503
            video, offset = pick
            try:
                return self._start(video, offset), video, offset
            except SourceError as error:
                if not error.permanent:
                    self.log(f"{cid} {video.bvid} 暂时取不到：{error}")
                    return 503
                self.log(f"{cid} {video.bvid} 放不了，跳过：{error}")
                self.state.mark_watched(cid, video.bvid)
                self.state.save()
        return 503

    def _client(self, site: str):
        client = self.clients.get(site)
        if client is None:
            raise SourceError(f"{site} 还没配置")
        return client

    def _start(self, video, offset: float):
        """起播：B 站在本机用 ffmpeg 换封装；YouTube 由 cstone 节点转好码，这里只转发。"""
        if video.site == "youtube":
            return self._client("youtube").open(video.bvid, offset)
        url = self._client("bilibili").media_url(video.bvid)
        return self.starter(self.ffmpeg, url, offset)

    def finish(self, cid: str, video, offset: float, outcome: str, elapsed: float) -> None:
        key = (cid, video.bvid)
        if outcome == "ended":
            self._quick_failures.pop(key, None)
            self.state.mark_watched(cid, video.bvid)
        elif outcome == "failed" and elapsed < QUICK_FAILURE_S:
            count = self._quick_failures.get(key, 0) + 1
            self._quick_failures[key] = count
            if count >= MAX_QUICK_FAILURES:
                self.log(f"{cid} {video.bvid} 连续 {count} 次起播失败，跳过")
                self._quick_failures.pop(key, None)
                self.state.mark_watched(cid, video.bvid)
        else:
            self._quick_failures.pop(key, None)
            self.state.save_position(cid, video.bvid, offset + elapsed)
        self.state.save()

    def _event(self, action: str) -> None:
        if self.events_out is None:
            return
        try:
            seq = events.write_event(self.events_out, action)
            self.log(f"事件 {seq} {action}")
        except OSError as error:
            self.log(f"写事件失败：{error}")

    def open_cast(self):
        """投屏视频：从保存的进度往回退一点续播；已播完的从头重播。"""
        with self.state.lock:
            video, done, position = self.state.cast, self.state.cast_done, self.state.cast_position
        if video is None:
            return 404
        offset = 0.0 if done else max(0.0, position - RESUME_BACK_S)
        try:
            return self._start(video, offset), video, offset
        except SourceError as error:
            self.log(f"cast {video.bvid} 取不到：{error}")
            return 503

    def finish_cast(self, video, offset: float, outcome: str, elapsed: float) -> None:
        if outcome == "ended":
            self.state.cast_ended(video.bvid)
            self.state.save()
            # 让电视服务把设备切回投屏前的台。
            self._event("return")
            return
        self.state.save_cast_position(video.bvid, offset + elapsed)
        self.state.save()

    def play(self, cid: str, handler) -> None:
        cast = cid == table.CAST_ID
        opened = self.open_cast() if cast else self.open_channel(cid)
        if isinstance(opened, int):
            handler.send_error(opened)
            return
        proc, video, offset = opened
        self.log(f"{cid} 开始 {video.bvid} 从 {int(offset)} 秒 《{video.title}》")
        began = self.monotonic()
        try:
            handler.send_response(200)
            handler.send_header("Content-Type", "video/mp2t")
            handler.send_header("Cache-Control", "no-store")
            handler.end_headers()
        except OSError:
            proc.kill()
            proc.wait()
            return

        ticked = [0.0]

        def tick(elapsed: float) -> None:
            ticked[0] = elapsed
            if cast:
                self.state.save_cast_position(video.bvid, offset + elapsed)
            else:
                self.state.save_position(cid, video.bvid, offset + elapsed)
            self.state.save()

        outcome = self.pumper(proc, handler.wfile.write, tick)
        elapsed = max(self.monotonic() - began, ticked[0])
        if cast:
            self.finish_cast(video, offset, outcome, elapsed)
        else:
            self.finish(cid, video, offset, outcome, elapsed)
        self.log(f"{cid} 结束 {video.bvid} {outcome} 播了 {int(elapsed)} 秒")

    # ---- 管理 ----

    def key_ok(self, given: str | None) -> bool:
        return bool(given) and hmac.compare_digest(given.encode(), self.key.encode())

    def summary(self) -> dict:
        with self.state.lock:
            creators = [{"id": c.id, "name": c.name, "site": c.site,
                         "left": sum(v.bvid not in c.watched for v in c.videos),
                         "total": len(c.videos)} for c in self.state.creators]
            cast = ({"title": self.state.cast.title, "done": self.state.cast_done}
                    if self.state.cast is not None else None)
        return {"creators": creators, "cast": cast}

    def add_creator(self, text: str) -> dict:
        text = (text or "").strip()
        if not text:
            return {"ok": False, "error": "请先粘贴链接"}
        site = _site_of(text)
        if self.clients.get(site) is None:
            return {"ok": False, "error": "YouTube 还没配置好，暂时只能添加 B 站 UP 主"}
        try:
            mid = self.clients[site].creator_id(text)
            if mid is None:
                return {"ok": False, "error": "看不懂这个链接。请粘贴 UP 主（频道）主页链接，或他的任意一个视频链接"}
            name, videos = self.clients[site].creator(mid)
        except SourceError as error:
            self.log(f"添加 UP 主失败：{error}")
            return {"ok": False, "error": f"{_SITE_NAMES[site]}暂时连不上或拒绝了请求，请稍后再试"}
        try:
            creator = self.state.add(site, mid, name, videos, self.clock())
        except ValueError as error:
            return {"ok": False, "error": str(error)}
        self.state.save()
        self.write_table()
        self.log(f"添加 UP 主 {creator.id} {name}（mid {mid}，{len(videos)} 个投稿）")
        return {"ok": True, "creator": {"id": creator.id, "name": name}}

    def cast(self, text: str) -> dict:
        text = (text or "").strip()
        if not text:
            return {"ok": False, "error": "请先粘贴视频链接"}
        site = _site_of(text)
        if self.clients.get(site) is None:
            return {"ok": False, "error": "YouTube 还没配置好，暂时只能投 B 站视频"}
        try:
            bvid = self.clients[site].video_id(text)
            if bvid is None:
                return {"ok": False, "error": "看不懂这个链接。请粘贴视频链接（App 里“分享 → 复制链接”）"}
            video = self.clients[site].video(bvid)
        except SourceError as error:
            self.log(f"投屏失败：{error}")
            if error.permanent:
                return {"ok": False, "error": "这个视频放不了（可能已删除、私享或需要会员）"}
            return {"ok": False, "error": f"{_SITE_NAMES[site]}暂时取不到，请稍后再试"}
        self.state.set_cast(video)
        self.state.save()
        self.write_table()
        self.log(f"投屏 {bvid} 《{video.title}》")
        self._event("cast")
        return {"ok": True, "title": video.title}

    def remove_creator(self, cid: str) -> dict:
        if not self.state.remove(cid):
            return {"ok": False, "error": "没有这个 UP 主"}
        self.state.save()
        self.write_table()
        self.log(f"删除 UP 主 {cid}")
        return {"ok": True}


_SITE_NAMES = {"bilibili": "B 站", "youtube": "YouTube "}


def _site_of(text: str) -> str:
    return "youtube" if youtube_links.is_youtube(text) else "bilibili"


def _stream_handler(relay: Relay):
    class StreamHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def do_GET(self):
            match = _CHANNEL.match(urllib.parse.urlsplit(self.path).path)
            if not match:
                self.send_error(404)
                return
            relay.play(match.group(1), self)

        def log_message(self, *_args):
            pass

    return StreamHandler


def _manage_handler(relay: Relay):
    class ManageHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _authorized(self) -> bool:
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            given = self.headers.get("X-Key") or (query.get("k") or [None])[0]
            if relay.key_ok(given):
                return True
            self._send(404, b"Not Found", "text/plain; charset=utf-8")
            return False

        def _send(self, code: int, body: bytes, kind: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            # 网址里带着密钥，不能经 Referer 带到别处。
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, data: dict, code: int = 200) -> None:
            self._send(code, json.dumps(data, ensure_ascii=False).encode(),
                       "application/json; charset=utf-8")

        def _path(self) -> str:
            return urllib.parse.urlsplit(self.path).path

        def do_GET(self):
            if not self._authorized():
                return
            path = self._path()
            if path == "/":
                self._send(200, PAGE.encode(), "text/html; charset=utf-8")
            elif path == "/api/state":
                self._json(relay.summary())
            else:
                self._send(404, b"Not Found", "text/plain; charset=utf-8")

        def do_POST(self):
            if not self._authorized():
                return
            path = self._path()
            if path not in ("/api/creators", "/api/cast"):
                self._send(404, b"Not Found", "text/plain; charset=utf-8")
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if not 0 < length <= MAX_BODY_BYTES:
                self._json({"ok": False, "error": "请求太大或为空"}, 400)
                return
            try:
                body = json.loads(self.rfile.read(length))
                url = body["url"] if isinstance(body, dict) else None
            except (ValueError, KeyError):
                url = None
            if not isinstance(url, str):
                self._json({"ok": False, "error": "请求格式不对"}, 400)
                return
            self._json(relay.add_creator(url) if path == "/api/creators" else relay.cast(url))

        def do_DELETE(self):
            if not self._authorized():
                return
            match = _CREATOR.match(self._path())
            if not match:
                self._send(404, b"Not Found", "text/plain; charset=utf-8")
                return
            self._json(relay.remove_creator(match.group(1)))

        def log_message(self, *_args):
            pass

    return ManageHandler


def make_servers(relay: Relay, stream_port: int, manage_port: int,
                 bind: str = "127.0.0.1") -> tuple[ThreadingHTTPServer, ThreadingHTTPServer]:
    streams = ThreadingHTTPServer((bind, stream_port), _stream_handler(relay))
    manage = ThreadingHTTPServer((bind, manage_port), _manage_handler(relay))
    streams.daemon_threads = manage.daemon_threads = True
    return streams, manage


PAGE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer">
<title>小智电视 UP 主与投屏</title>
<style>
  :root { --bg:#f6f7f9; --card:#fff; --text:#1d2129; --dim:#5c6370; --key:#0b6bcb; --rule:#e3e6eb; --bad:#c62828; }
  @media (prefers-color-scheme: dark) {
    :root { --bg:#16181d; --card:#20232a; --text:#e8eaed; --dim:#a0a6b1; --key:#6cb4ff; --rule:#30343c; --bad:#ef9a9a; }
  }
  * { box-sizing: border-box; }
  body { margin:0; background:var(--bg); color:var(--text);
         font:16px/1.6 -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif; }
  main { max-width:640px; margin:0 auto; padding:16px; }
  h1 { font-size:22px; margin:8px 0 4px; }
  .sub { color:var(--dim); margin:0 0 16px; font-size:14px; }
  section { background:var(--card); border-radius:12px; padding:14px 16px; margin:0 0 14px;
            box-shadow:0 1px 2px rgba(0,0,0,.06); }
  h2 { font-size:17px; margin:0 0 10px; }
  textarea { width:100%; min-height:72px; font:inherit; padding:8px; border:1px solid var(--rule);
             border-radius:8px; background:transparent; color:inherit; resize:vertical; }
  button { font:inherit; border:0; border-radius:8px; padding:8px 14px; cursor:pointer; }
  .primary { background:var(--key); color:#fff; margin-top:8px; width:100%; }
  .del { background:transparent; color:var(--bad); padding:4px 8px; }
  ul { list-style:none; margin:0; padding:0; }
  li { display:flex; align-items:center; gap:8px; padding:8px 0; border-top:1px solid var(--rule); }
  li:first-child { border-top:none; }
  .name { flex:1; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .meta { color:var(--dim); font-size:14px; white-space:nowrap; }
  .msg { min-height:1.6em; font-size:14px; margin-top:6px; }
  .msg.bad { color:var(--bad); }
  .empty { color:var(--dim); }
</style>
</head>
<body>
<main>
  <h1>小智电视 UP 主</h1>
  <p class="sub">每个 UP 主是一个台，从最新的没看过的视频开始播。这个网址带着密钥，不要发给别人。</p>

  <section>
    <h2>投屏到小智</h2>
    <textarea id="cast" placeholder="粘贴 B 站或 YouTube 视频链接（App 里“分享 → 复制链接”即可）"></textarea>
    <button class="primary" id="castbtn">在小智上播放</button>
    <div class="msg" id="castmsg"></div>
    <p class="sub" id="nowcast" style="margin:6px 0 0"></p>
  </section>

  <section>
    <h2>添加 UP 主</h2>
    <textarea id="link" placeholder="粘贴 B 站 UP 主或 YouTube 频道的主页链接，或他的任意一个视频链接（App 里“分享 → 复制链接”即可）"></textarea>
    <button class="primary" id="add">添加</button>
    <div class="msg" id="addmsg"></div>
  </section>

  <section>
    <h2>已添加</h2>
    <ul id="list"><li class="empty">加载中…</li></ul>
    <p class="sub" style="margin:8px 0 0">添加或删除后，小智下次换台时频道列表就会更新。最多 12 个。</p>
  </section>
</main>
<script>
"use strict";
const KEY = new URLSearchParams(location.search).get("k") || "";
const $ = (id) => document.getElementById(id);

async function call(method, path, body) {
  const options = { method, headers: { "X-Key": KEY } };
  if (body !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  const response = await fetch(path, options);
  if (response.status === 404) throw new Error("链接里的密钥不对");
  return response.json();
}

function show(id, text, bad) {
  const el = $(id);
  el.textContent = text;
  el.className = bad ? "msg bad" : "msg";
}

async function refresh() {
  let data;
  try {
    data = await call("GET", "api/state");
  } catch (error) {
    $("list").innerHTML = "";
    const li = document.createElement("li");
    li.className = "empty";
    li.textContent = "读取失败：" + error.message;
    $("list").appendChild(li);
    return;
  }
  $("nowcast").textContent = data.cast
    ? (data.cast.done ? "上次投屏：《" : "正在投屏：《") + data.cast.title + "》"
    : "";
  const list = $("list");
  list.innerHTML = "";
  if (!data.creators.length) {
    const li = document.createElement("li");
    li.className = "empty";
    li.textContent = "还没有添加 UP 主";
    list.appendChild(li);
  }
  for (const c of data.creators) {
    const li = document.createElement("li");
    const name = document.createElement("span");
    name.className = "name";
    name.textContent = c.name;
    const meta = document.createElement("span");
    meta.className = "meta";
    meta.textContent = "未看 " + c.left + "/" + c.total;
    const del = document.createElement("button");
    del.className = "del";
    del.textContent = "删除";
    del.onclick = async () => {
      if (!confirm("删除「" + c.name + "」这个台？")) return;
      const answer = await call("DELETE", "api/creators/" + c.id);
      if (!answer.ok) alert(answer.error);
      refresh();
    };
    li.append(name, meta, del);
    list.appendChild(li);
  }
}

$("castbtn").onclick = async () => {
  const button = $("castbtn");
  button.disabled = true;
  show("castmsg", "正在发送…", false);
  try {
    const answer = await call("POST", "api/cast", { url: $("cast").value });
    if (answer.ok) {
      show("castmsg", "已发送，小智会自动切到「我的投屏」播放《" + answer.title + "》", false);
      $("cast").value = "";
      refresh();
    } else {
      show("castmsg", answer.error, true);
    }
  } catch (error) {
    show("castmsg", error.message, true);
  } finally {
    button.disabled = false;
  }
};

$("add").onclick = async () => {
  const button = $("add");
  button.disabled = true;
  show("addmsg", "正在添加…", false);
  try {
    const answer = await call("POST", "api/creators", { url: $("link").value });
    if (answer.ok) {
      show("addmsg", "已添加「" + answer.creator.name + "」", false);
      $("link").value = "";
      refresh();
    } else {
      show("addmsg", answer.error, true);
    }
  } catch (error) {
    show("addmsg", error.message, true);
  } finally {
    button.disabled = false;
  }
};

refresh();
</script>
</body>
</html>
"""
