/* 小鼠电视 RooTV 电脑版 ·「管理」：原来的总后台（/tv/admin、admin.html）搬进新电脑版，地址 #/admin/<分区>。
   分区：概览（设备、主机负载曲线、进程、YouTube 节点）、用户（怎么来的、改名、封禁、导出后删除、邀请、一次性登录链接、
   他的登录）、设备（解绑、封禁、重连、重新配对、改名、固件版本）、频道表（导入、筛选、去重、试一下、改名分组排序、
   保存前检查和备份、节目单来源）、操作记录（筛选、往前翻）、系统（只读的规矩、共享配对令牌开关、版本）。
   接口都是 server/relay/admin.py 的 /api/admin/*：服务端只回答管理员，别人一律 404（这里的入口也只给管理员看）。
   用 console.js 的小工具（el、button、toast、modal、when …）；这里的全局名字都以 adm / ach 开头，免得和它撞上。
   在公网隧道上（server/front.py 默认拦 /tv/api/admin）回答 404 但登录还在：显示「这里打不开管理」，不当成退出登录。 */
"use strict";

const ADM_SECTIONS = [["overview", "概览", "chart"], ["users", "用户", "users"], ["devices", "设备", "tv"],
                      ["channels", "频道表", "list"], ["audit", "操作记录", "log"], ["system", "系统", "sys"]];
const ADM_STATUS = { unclaimed: "未绑定", active: "在用", banned: "已封禁", repair: "重新配对中", retired: "已退役" };
const ADM_ORIGIN = { signup: "自己注册", invite: "邀请", initial: "最早的账号" };
const ADM_KIND = { phone: "手机", pc: "电脑", guest: "访客" };
const ADM_VIA = { recover: "设备恢复", admin: "管理员链接", signup: "扫码注册", local: "电脑上的程序" };
// 操作记录里的动作（server/tenancy 各处写的 action）。没列出的原样显示。
const ADM_ACTIONS = {
  "user.signup": "扫绑定码自己注册", "user.ban": "封禁用户", "user.unban": "解封用户", "user.rename": "用户改名",
  "user.delete": "删除用户", "user.export": "导出用户数据", "user.reviewed": "看过自己注册的用户",
  "invite.create": "生成邀请", "invite.accept": "接受邀请",
  "device.register": "新设备第一次连上", "device.bind": "绑定设备", "device.unbind": "解绑设备", "device.rename": "设备改名",
  "device.repair": "开始重新配对", "device.repaired": "记住了新密钥", "device.adopt": "旧设备搬到新 id",
  "device.banned": "封禁设备", "device.active": "设备恢复使用", "device.unclaimed": "设备回到未绑定",
  "device.secret_mismatch": "设备密钥对不上", "device.new_code": "换访客码", "device.reconnect": "让设备重连",
  "device.claim_renewed": "绑定码换成 6 位数字", "device.claim_rotated": "错码太多，换了绑定码", "bind.locked": "绑定码错太多次，暂停 10 分钟",
  "login.key": "用密钥登录", "login.pc": "电脑扫码登录", "login.guest": "访客扫码", "login.phone.link": "手机用链接登录",
  "login.phone.recover": "手机经设备恢复登录", "login.phone.admin": "手机用管理员链接登录", "login.local": "电脑上的程序直接登录",
  "phone_link.create": "生成手机登录链接", "phone_link.device": "设备要恢复登录链接", "phone_link.admin": "管理员生成登录链接",
  "session.revoke": "退出一个登录", "session.revoke_others": "退出别处的登录",
  "admin.session.revoke": "管理员退出他的一个登录", "admin.session.revoke_all": "管理员退出他的全部登录",
  "legacy_token.on": "打开共享配对令牌", "legacy_token.off": "关闭共享配对令牌", "channels.save": "保存频道表",
  "channels.import": "导入播放列表", "channels.subscribe": "订阅直播源", "channels.subscription_refresh": "更新订阅的直播源", "channels.unsubscribe": "取消订阅直播源", "epg.save": "节目单设置", "epg.refresh": "更新节目单", "user.setup": "第一次设置（成为管理员）", "user.setup_refused": "设置码不对",
  "wifi.add": "给设备加 Wi-Fi", "wifi.remove": "删设备的 Wi-Fi", "wifi.up": "调 Wi-Fi 顺序", "wifi.switch": "让设备换 Wi-Fi",
  "login.pc.confirm": "手机确认电脑登录", "login.pc.deny": "手机拒绝电脑登录", "migrate": "从单用户迁移",
};
const ADM_PREFIX = { "user.": "用户…", "device.": "设备…", "login.": "登录…", "session.": "退出登录…", "invite.": "邀请…",
  "phone_link.": "登录链接…", "admin.": "管理员退出别人…", "channels.": "频道表…", "epg.": "节目单…", "legacy_token.": "共享令牌…",
  "wifi.": "设备 Wi-Fi…", "bind.": "绑定…" };

const adm = { primed: false, on: false, sec: "", run: 0, timer: 0, me: 0, users: [], devices: [], review: 0, history: [], historyAt: 0, auNext: null };

// ======================================================================= 请求

class AdminGone extends Error {}
// 和 console.js 的 call 一样，只是 404 时先问一下登录还在不在：在的话是这里打不开管理（公网隧道），不是退出了。
async function admCall(method, path, body) {
  const options = { method, credentials: "same-origin", headers: {} };
  if (body !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  const response = await fetch(path, options);
  if (response.status === 404) {
    let status = null;
    try { status = await (await fetch("api/login/status", { credentials: "same-origin" })).json(); } catch (error) { /* 网络不通 */ }
    if (status && !status.signed_in) throw new SignedOut("登录已失效");
    throw new AdminGone("这里打不开管理");
  }
  if (response.status === 413) throw new Error("一次改的太多了（请求太大）：先保存一部分");
  return response.json();
}
function admFail(error) {
  if (error instanceof SignedOut) { signedOut(); return; }
  if (error instanceof AdminGone) { admGone(); return; }
  toast(error.message || "网络不通", true);
}
async function admBusy(btn, work) {
  if (btn) btn.disabled = true;
  try { await work(); } catch (error) { admFail(error); } finally { if (btn) btn.disabled = false; }
}
// 改东西：成功时说 done（服务器给了 note 就说 note），失败说服务器的原因；返回回答或 null。
async function admAct(path, body, done) {
  try {
    const answer = await admCall("POST", path, body || {});
    if (!answer.ok) { toast(answer.error || "没成功", true); return null; }
    if (done) toast(answer.note || done);
    return answer;
  } catch (error) { admFail(error); return null; }
}
function admStill(run, sec) { return adm.on && adm.sec === sec && run === adm.run; }

// ======================================================================= 小工具

function admKb(bytes) { return bytes === null || bytes === undefined ? "—" : (bytes / 1000).toFixed(bytes < 10000 ? 1 : 0); }
function admSpan(seconds) {
  if (!seconds) return "—";
  const h = Math.floor(seconds / 3600), m = Math.floor(seconds / 60) % 60;
  return h ? h + " 小时 " + m + " 分" : m ? m + " 分钟" : seconds + " 秒";
}
function admTag(text, cls) { return el("span", "tag" + (cls ? " " + cls : ""), text); }
function admBtn(text, cls, key, onclick) {
  const b = button(text, cls || "linkbtn", onclick);
  if (key) b.dataset.k = key;
  return b;
}
// 一行：左边头像或图标，中间名字（可带小标签）和几行小字，右边 extra（数字）和按钮。
function admRow(opt) {
  const row = el("div", "li" + (opt.cls ? " " + opt.cls : ""));
  if (opt.lead) row.appendChild(opt.lead);
  const tx = el("span", "tx");
  const b = el("b", "", opt.title);
  for (const t of opt.tags || []) if (t) b.appendChild(t);
  tx.appendChild(b);
  for (const line of opt.lines || []) {
    if (!line) continue;
    tx.appendChild(typeof line === "string" ? el("small", "", line) : line);
  }
  row.appendChild(tx);
  if (opt.extra) row.appendChild(opt.extra);
  if (opt.acts && opt.acts.length) {
    const box = el("span", "acts");
    box.append(...opt.acts);
    row.appendChild(box);
  }
  return row;
}
function admAvatar(name) {
  const av = el("span", "av", [...(name || "?")][0] || "?");
  av.style.setProperty("--h", String(hsh(name || "") % 360));
  av.setAttribute("aria-hidden", "true");
  return av;
}
function admList(title, note, rows) {
  const box = el("div", "list");
  const h = el("h3", "", title);
  if (note) h.appendChild(el("small", "", note));
  box.appendChild(h);
  box.append(...rows);
  return box;
}
function admHint(text) { return el("p", "hint", text); }
function admEmpty(text) { const row = el("div", "li"); row.appendChild(el("span", "empty-t", text)); return row; }
function admTable(title, note, heads, rows, cls) {
  const box = el("div", "tbl" + (cls ? " " + cls : ""));
  if (title) {
    const h = el("h3", "", title);
    if (note) h.appendChild(el("small", "", note));
    box.appendChild(h);
  }
  const table = el("table");
  const head = el("thead"), tr = el("tr");
  for (const h of heads) {
    const th = el("th", h.startsWith("#") ? "n" : "", h.replace(/^#/, ""));
    tr.appendChild(th);
  }
  head.appendChild(tr);
  const body = el("tbody");
  body.append(...rows);
  table.append(head, body);
  box.appendChild(table);
  return box;
}
function admTd(cls, text) { return el("td", cls, text); }
function admQr(svg) {
  const box = el("div", "qrbox");
  box.innerHTML = svg;      // 服务端画的二维码 SVG（server/relay/qr.py），只有方块
  return box;
}
async function admCopy(text) {
  try { await navigator.clipboard.writeText(text); toast("已复制"); } catch (error) { toast("复制不了，请手动选中链接复制", true); }
}

// 要紧的操作先问一句：lines 里以「!」开头的是要紧的话（红边）；typed：要输入这几个字才能点（删除用户）。
function admConfirm(title, lines, okText, typed, danger) {
  return new Promise((resolve) => {
    let done = false;
    const finish = (value) => { if (done) return; done = true; closeOverlays(); resolve(value); };
    const ok = el("button", "btn sm" + (danger === false ? " pri" : " danger"), okText);
    const nodes = lines.map((line) => el("p", line.startsWith("!") ? "warn" : "", line.replace(/^!/, "")));
    let input = null;
    if (typed) {
      input = el("input");
      input.type = "text";
      input.placeholder = "输入「" + typed + "」确认";
      input.autocomplete = "off";
      ok.disabled = true;
      input.oninput = () => { ok.disabled = input.value.trim() !== typed; };
      input.onkeydown = (event) => { if (event.key === "Enter" && !ok.disabled) ok.click(); };
      nodes.push(input);
    }
    ok.onclick = () => finish(input ? input.value.trim() : true);
    const row = el("div", "row");
    row.append(button("取消", "ghost", () => finish(false)), ok);
    modal(title, ...nodes, row);
    admWatchClose(() => { if (!done) { done = true; resolve(false); } });
    (input || ok).focus();
  });
}
function admAsk(title, label, value, max) {
  return new Promise((resolve) => {
    let done = false;
    const finish = (text) => { if (done) return; done = true; closeOverlays(); resolve(text); };
    const input = el("input");
    input.type = "text";
    input.value = value || "";
    input.maxLength = max || 20;
    input.autocomplete = "off";
    const ok = el("button", "go", "好");
    ok.onclick = () => finish(input.value.trim());
    input.onkeydown = (event) => { if (event.key === "Enter") ok.click(); };
    const row = el("div", "row");
    row.append(button("取消", "ghost", () => finish("")), ok);
    modal(title, el("p", "", label), input, row);
    admWatchClose(() => { if (!done) { done = true; resolve(""); } });
    input.focus();
    input.select();
  });
}
// Esc、点遮罩关掉弹窗时也要把等着的 Promise 结束掉。
function admWatchClose(onClose) {
  const box = $("modal");
  const observer = new MutationObserver(() => { if (box.hidden) { observer.disconnect(); onClose(); } });
  observer.observe(box, { attributes: true, attributeFilter: ["hidden"] });
}

// ======================================================================= 外框：分区、打不开时

// console.js 的 showRoute 每次换页面都调：sub 是 #/admin/<分区> 的分区；离开管理时是 null。
function adminRoute(sub) {
  if (sub === null) {
    adm.on = false;
    clearInterval(adm.timer);
    adm.timer = 0;
    return;
  }
  const known = (id) => ADM_SECTIONS.some(([x]) => x === id);
  const sec = known(sub) ? sub : known(lsGet("rootv.admin", "")) ? lsGet("rootv.admin", "") : "overview";
  if (sec !== sub) { history.replaceState(null, "", "#/admin/" + sec); lastHash = location.hash; }
  if (adm.on && adm.sec === sec) return;
  adm.on = true;
  adm.sec = sec;
  adm.run++;
  lsSet("rootv.admin", sec);
  admShell();
  admLoad();
  // 第一次进「管理」：读一次「用户」上的待查看数（在「用户」「概览」分区里会顺便更新）。
  if (!adm.primed && sec !== "users" && sec !== "overview") admPrime();
  adm.primed = true;
  if (!adm.timer) adm.timer = setInterval(admTick, 10000);
}
// 「频道表」有没保存的修改：离开前问（console.js 的 hashchange 调）。
function adminCanLeave(hash) {
  if (!(adm.on && adm.sec === "channels" && achDirty())) return true;
  if (hash === "#/admin/channels") return true;
  return confirm("频道表还有没保存的修改，离开会丢掉。确定离开？");
}
addEventListener("beforeunload", (event) => { if (adm.on && adm.sec === "channels" && achDirty()) { event.preventDefault(); event.returnValue = ""; } });

function admShell() {
  const nav = ADM_SECTIONS.map(([id, t, i]) => `<a href="#/admin/${id}" data-k="adm-${id}"${id === adm.sec ? ' aria-current="page"' : ""}>${ic(i, 16)}${t}${id === "users" && adm.review ? `<em>${adm.review}</em>` : ""}</a>`).join("");
  const k = focusKey();      // 用键盘在分区标签上按回车：重画后焦点还在那个标签上
  $("admBody").innerHTML = `<header class="ph"><h1 class="lt">管理</h1></header>
    <nav class="chips asub" aria-label="管理分区">${nav}</nav><div id="admSec"><p class="aload">正在读取…</p></div>`;
  restoreFocus(k);
}
function admSetReview(n) {
  if (adm.review === n) return;
  adm.review = n;
  const a = qs('#admBody .asub a[href="#/admin/users"]');
  if (!a) return;
  let em = a.querySelector("em");
  if (!n) { if (em) em.remove(); return; }
  if (!em) { em = el("em"); a.appendChild(em); }
  em.textContent = n;
}
function admGone() {
  const box = $("admSec");
  if (!box) return;
  box.innerHTML = `<div class="empty"><div class="eic">${ic("shield", 40)}</div><b>这里打不开管理</b>
    <p>经公网隧道（手机在外面）打开电脑版时，管理默认是关着的：在家里的局域网、或者在运行${esc(BRAND_ZH)}的这台电脑上打开电脑版就能用（自建服务器的 config.toml 里 admin_over_tunnel 可以打开，不建议）。也可能你已经不是管理员了。</p>
    <a class="btn sm" href="#/home">回首页</a></div>`;
}
function admSec(...nodes) {
  const box = $("admSec");
  if (!box) return;
  const k = focusKey();
  box.replaceChildren(...nodes);
  restoreFocus(k);
}
function admLoad() {
  if (adm.sec === "overview") admOverview(true);
  else if (adm.sec === "users") admUsers();
  else if (adm.sec === "devices") admDevices();
  else if (adm.sec === "channels") achLoad();
  else if (adm.sec === "audit") admAudit(false);
  else if (adm.sec === "system") admSystem();
}
function admTick() {
  if (!adm.on || route !== "admin" || document.hidden || anyOverlay()) return;
  if (adm.sec === "overview") admOverview(false);
}
// 第一次打开（不管哪个分区）：「用户」上的待查看数。
async function admPrime() {
  try {
    const data = await admCall("GET", "api/admin/users");
    adm.users = data.users; adm.me = data.me;
    admSetReview(data.users.filter((u) => u.origin === "signup" && !u.reviewed_at).length);
  } catch (error) { /* 打开时再说 */ }
}

// ======================================================================= 概览

async function admOverview(withHistory) {
  const run = adm.run;
  const fresh = withHistory || Date.now() - adm.historyAt > 60000;
  let data;
  try { data = await admCall("GET", "api/admin/overview" + (fresh ? "?history=1" : "")); } catch (error) { admFail(error); return; }
  if (!admStill(run, "overview")) return;
  if (fresh && data.host.history) { adm.history = data.host.history; adm.historyAt = Date.now(); }
  admSetReview(data.counts.review);
  const nodes = [];
  const yt = data.youtube;
  const note = yt.configured && yt.state === "login" ? "YouTube 节点的 cookies 过期了：用小号重新导出（见运维手册），B 站不受影响"
    : yt.configured && yt.state === "unreachable" ? "连不上 YouTube 节点：YouTube 的订阅和投屏暂时放不了" : "";
  if (note) nodes.push(admBanner("warn", "YouTube", note));
  nodes.push(admTiles(data));
  nodes.push(admOvDevices(data.devices));
  nodes.push(...admHost(data.host));
  const stamp = new Date(data.now * 1000);
  nodes.push(admHint("更新于 " + pad(stamp.getHours()) + ":" + pad(stamp.getMinutes()) + ":" + pad(stamp.getSeconds()) + "，开着这一页每 10 秒刷新一次。"));
  admSec(...nodes);
}
function admBanner(kind, title, text) {
  const box = el("div", "banner " + kind);
  box.setAttribute("role", "note");
  box.innerHTML = `<span class="ico">${ic(kind === "warn" ? "warn" : "info", 22)}</span>`;
  const body = el("div");
  body.append(el("b", "", title), el("p", "", text));
  box.appendChild(body);
  return box;
}
function admStat(label, icon, value, unit, note, cls) {
  const box = el("div", "astat" + (cls ? " " + cls : ""));
  const head = el("span");
  head.innerHTML = ic(icon, 15);
  head.appendChild(document.createTextNode(label));
  const b = el("b", "", value);
  if (unit) b.appendChild(el("small", "", unit));
  box.append(head, b);
  if (note) box.appendChild(el("em", "", note));
  return box;
}
function admTiles(data) {
  const c = data.counts, h = data.host, yt = data.youtube;
  const tiles = [admStat("在线设备", "tv", c.online + " / " + c.devices, "", "共 " + c.devices + " 台（不算退役的）"),
                 admStat("用户", "users", String(c.users), "", (c.review ? c.review + " 个自己注册的待查看" : "没有待查看的") +
                         (c.banned ? " · " + c.banned + " 个已封禁" : ""), c.review > 0 ? "warn" : "")];
  if (h.available && h.cpu !== undefined) {
    tiles.push(admStat("CPU", "chart", String(Math.round(h.cpu)), "%", "整台主机，最近 5 秒", h.cpu > 85 ? "warn" : ""));
    tiles.push(admStat("内存", "sys", String(Math.round(h.mem_used)), "MB",
                       "共 " + Math.round(h.mem_total) + " MB；可以随时让出的缓存不算", h.mem_used > h.mem_total * .9 ? "warn" : ""));
    tiles.push(admStat("出站", "upc", admKb(h.tx), "kB/s", "公网出口约 375 kB/s（3 Mbit/s）", h.tx > 330000 ? "warn" : ""));
  } else {
    tiles.push(admStat("主机负载", "chart", "—", "", h.available ? "刚启动，几秒后就有" : "这台机器没有 /proc（不是 Linux），不采样"));
  }
  const ytState = !yt.configured ? "未配置" : ({ ok: "正常", login: "要换 cookies", error: "出错", unknown: "未知", unreachable: "连不上" })[yt.state] || yt.state;
  tiles.push(admStat("YouTube 节点", "live", ytState, "", yt.configured ? "在放 " + yt.streams + " / " + yt.max_streams + " 路" : "只支持 B 站",
                     yt.configured && yt.state !== "ok" && yt.state !== "unknown" ? "warn" : ""));
  const box = el("div", "astats");
  box.append(...tiles);
  return box;
}
function admOvDevices(rows) {
  rows = rows.slice().sort((a, b) => (b.online - a.online) || (b.last_seen - a.last_seen));
  const items = rows.map((d) => {
    const lead = el("span", "av dev" + (d.online ? " on" : ""));
    lead.innerHTML = ic("tv", 18);
    const state = d.online ? (d.audio_only ? "在线 · 只听" : "在线") : ADM_STATUS[d.status] === "在用" ? "离线" : ADM_STATUS[d.status] || d.status;
    const where = el("small", d.online ? "live" : d.status === "banned" ? "bad" : "",
                     state + (!d.online && d.last_seen ? " · 最后 " + when(d.last_seen) : "") +
                     (d.online ? " · 正在播 " + (d.channel_name || d.channel || "—") + (d.title ? "：" + d.title : "") : ""));
    let extra = null;
    if (d.online) {
      extra = el("span", "met");
      const rate = d.bytes_10s === null || d.bytes_10s === undefined ? null : d.bytes_10s / 10;
      extra.innerHTML = `<span>会话 <b>${esc(admSpan(d.session_s))}</b></span><span>流量 <b>${esc(admKb(rate))}</b> kB/s</span>` +
        (d.fps !== null && d.fps !== undefined ? `<span><b>${esc(String(d.fps))}</b> 画面/秒</span>` : "") +
        `<span>目标 <b>${esc(admKb(d.target_rate))}</b> kB/s</span>`;
    }
    return admRow({ lead, title: d.name, tags: [d.owner_banned ? admTag("主人已封禁", "bad") : null, d.key_mismatch ? admTag("密钥不匹配：到「设备」点「重新配对」", "bad") : null],
                    lines: [d.model_name + " · " + d.id + " · " + (d.owner_name ? "主人 " + d.owner_name : "没有主人"), where], extra });
  });
  const box = admList("设备", rows.length + " 台", items.length ? items : [admEmpty("还没有设备")]);
  const wrap = el("div");
  wrap.append(box, admHint("流量是电视服务最近约 10 秒发给设备的字节（画面加声音）；目标是字节率控制器此刻给画面的预算。主机的公网出口是 3 Mbit/s（约 375 kB/s），一台小电视满画质要 200 kB/s 左右。"));
  wrap.lastChild.style.margin = "-12px 0 26px";
  return wrap;
}
function admHost(h) {
  const head = el("div", "h2");
  head.style.margin = "0 0 14px";
  head.append(el("h2", "", "主机"), el("small", "", h.available ? "每 5 秒采样，曲线是每分钟的平均；中转重启后从头记" : ""));
  if (!h.available) {
    const empty = el("div", "empty");
    empty.style.padding = "36px 24px";
    empty.style.marginBottom = "22px";
    empty.append(el("p", "", "这台机器没有 /proc（不是 Linux），不采样主机负载。"));
    empty.lastChild.style.margin = "0 auto";
    return [head, empty];
  }
  const now = Date.now() / 1000, hist = adm.history;
  const memMax = h.mem_total || Math.max(1, ...hist.map((p) => p[2]));
  const txMax = Math.max(375000, ...hist.map((p) => p[3]));
  const charts = el("div", "acharts");
  charts.append(admChart("CPU", hist, (p) => p[1], 100, (v) => Math.round(v) + "%", now),
                admChart("内存（已用）", hist, (p) => p[2], memMax, (v) => Math.round(v) + " MB", now),
                admChart("出站流量", hist, (p) => p[3], txMax, (v) => admKb(v) + " kB/s", now));
  const out = [head, charts];
  const procs = h.processes || [];
  if (procs.length) {
    out.push(admTable("进程", "CPU 是和上次刷新相比", ["进程", "#pid", "#内存 MB", "#CPU %"], procs.map((p) => {
      const tr = el("tr");
      tr.append(admTd("", p.name), admTd("n", String(p.pid)), admTd("n", String(p.rss_mb)), admTd("n", p.cpu === null ? "…" : String(p.cpu)));
      return tr;
    })));
  }
  return out;
}
// 一条曲线（一个量一张图，不画双轴）：24 小时的分钟点；中间断了 3 分钟以上（中转重启）的地方不连线。
function admChart(title, points, pick, max, format, now) {
  const box = el("div", "achart");
  const head = el("div", "ct");
  const last = points.length ? pick(points[points.length - 1]) : null;
  head.append(el("span", "", title), el("small", "", last === null ? "—" : format(last)));
  box.appendChild(head);
  const W = 300, H = 110, ns = "http://www.w3.org/2000/svg";
  const make = (tag, attrs) => { const n = document.createElementNS(ns, tag); for (const k in attrs) n.setAttribute(k, attrs[k]); return n; };
  const svg = make("svg", { viewBox: "0 0 " + W + " " + H, preserveAspectRatio: "none", role: "img", "aria-label": title + "，最近 24 小时" });
  const start = now - 86400;
  const x = (t) => (t - start) / 86400 * W;
  const y = (v) => H - Math.max(0, Math.min(1, v / max)) * (H - 4) - 2;
  for (const f of [0.5, 1]) svg.appendChild(make("line", { x1: 0, x2: W, y1: y(max * f), y2: y(max * f), class: "ref", "vector-effect": "non-scaling-stroke" }));
  const segments = [];
  let prev = null;
  for (const p of points) {
    if (prev === null || p[0] - prev > 180) segments.push([]);
    segments[segments.length - 1].push(p);
    prev = p[0];
  }
  for (const seg of segments) {
    const line = seg.map((p, i) => (i ? "L" : "M") + x(p[0]).toFixed(1) + " " + y(pick(p)).toFixed(1)).join("");
    if (seg.length > 1) svg.appendChild(make("path", { d: line + "L" + x(seg[seg.length - 1][0]).toFixed(1) + " " + H + "L" + x(seg[0][0]).toFixed(1) + " " + H + "Z", class: "area" }));
    svg.appendChild(make("path", { d: line, class: "line", "vector-effect": "non-scaling-stroke" }));
  }
  const cross = make("line", { y1: 0, y2: H, class: "cross", "vector-effect": "non-scaling-stroke" });
  cross.style.display = "none";
  svg.appendChild(cross);
  box.appendChild(svg);
  const tip = el("div", "tip");
  tip.hidden = true;
  box.appendChild(tip);
  const axis = el("div", "axis");
  axis.append(el("span", "", "24 小时前"), el("span", "", "12 小时前"), el("span", "", "现在"));
  box.appendChild(axis);
  const hover = (clientX) => {
    if (!points.length) return;
    const rect = svg.getBoundingClientRect();
    const t = start + (clientX - rect.left) / rect.width * 86400;
    let best = points[0];
    for (const p of points) if (Math.abs(p[0] - t) < Math.abs(best[0] - t)) best = p;
    cross.setAttribute("x1", x(best[0])); cross.setAttribute("x2", x(best[0]));
    cross.style.display = "";
    tip.hidden = false;
    tip.textContent = when(best[0]) + "  " + format(pick(best));
    tip.style.left = Math.min(rect.width - 50, Math.max(50, x(best[0]) / W * rect.width)) + 16 + "px";
  };
  svg.addEventListener("pointermove", (event) => hover(event.clientX));
  svg.addEventListener("pointerleave", () => { cross.style.display = "none"; tip.hidden = true; });
  return box;
}

// ======================================================================= 用户

async function admUsers() {
  const run = adm.run;
  let data;
  try { data = await admCall("GET", "api/admin/users"); } catch (error) { admFail(error); return; }
  if (!admStill(run, "users")) return;
  adm.users = data.users;
  adm.me = data.me;
  const review = data.users.filter((u) => u.origin === "signup" && !u.reviewed_at).length;
  admSetReview(review);
  const rows = data.users.map(admUserRow);
  const list = admList("所有用户", data.users.length + " 个" + (review ? " · " + review + " 个待查看" : ""), rows);
  const hint = admHint("「自己注册」是扫了一台还没绑定的小电视的绑定码、自己建的账号（不需要邀请）；看过之后点「知道了」，标记就消失。" +
                       "「登录链接」给这个人一个 5 分钟有效、只能用一次的手机登录链接：他所有手机都退出了、又不在设备旁边时用。");
  hint.style.margin = "-12px 0 26px";
  admSec(list, hint, admInvitePanel(), admInviteList(data.invites));
}
function admUserRow(u) {
  const banned = u.status === "banned", self = u.id === adm.me, review = u.origin === "signup" && !u.reviewed_at;
  const acts = [];
  if (review) {
    acts.push(admBtn("知道了", "linkbtn on", "u" + u.id + "-ok", async () => {
      if (await admAct("api/admin/users/" + u.id + "/reviewed", {}, "已标记")) admUsers();
    }));
  }
  acts.push(admBtn("改名", "", "u" + u.id + "-rename", () => admRenameUser(u)),
            admBtn("登录链接", "", "u" + u.id + "-link", () => admLoginLink(u)),
            admBtn("登录的地方", "", "u" + u.id + "-sessions", () => admSessions(u)),
            admBtn("导出", "", "u" + u.id + "-export", () => { location.href = "api/admin/users/" + u.id + "/export"; }));
  if (!self) {
    acts.push(admBtn(banned ? "解封" : "封禁", banned ? "linkbtn" : "linkbtn red", "u" + u.id + "-ban", () => admBanUser(u)),
              admBtn("删除", "linkbtn red", "u" + u.id + "-del", () => admDeleteUser(u)));
  }
  return admRow({ lead: admAvatar(u.name), title: u.name,
                  tags: [u.role === "admin" ? admTag("管理员", "brand") : null, self ? admTag("我") : null,
                         banned ? admTag("已封禁", "bad") : null, review ? admTag("待查看", "warn") : null],
                  lines: ["#" + u.id + " · " + (ADM_ORIGIN[u.origin] || u.origin) + " · " + when(u.created_at) + " 加入",
                          u.devices + " 台设备 · " + u.sessions + " 处登录 · 最后使用 " + when(u.last_seen)], acts });
}
async function admRenameUser(u) {
  const name = await admAsk("给「" + u.name + "」改名", "新名字（最多 20 个字），家里人在遥控上看到的就是它。", u.name);
  if (!name || name === u.name) return;
  if (await admAct("api/admin/users/" + u.id + "/rename", { name }, "已改名为「" + name + "」")) admUsers();
}
async function admBanUser(u) {
  if (u.status === "banned") {
    if (!await admConfirm("解封「" + u.name + "」", ["他可以重新登录（以前的登录在封禁时已经全部退出了，要重新用邀请、恢复登录或登录链接进来），他的设备也能重新连上。"], "解封", null, false)) return;
    if (await admAct("api/admin/users/" + u.id + "/unban", {}, "已解封")) admUsers();
    return;
  }
  const ok = await admConfirm("封禁「" + u.name + "」", [
    "!他所有的登录（手机、电脑）马上退出，他是主人的 " + u.devices + " 台设备会断开，重连时被拒绝。",
    "他的订阅、进度和设备绑定都保留，解封后恢复。"], "封禁");
  if (ok && await admAct("api/admin/users/" + u.id + "/ban", {}, "已封禁")) admUsers();
}
async function admDeleteUser(u) {
  const typed = await admConfirm("删除「" + u.name + "」", [
    "!删除后不能恢复：他的账号、订阅、观看进度、登录都会删掉，他是主人的 " + u.devices + " 台设备会解绑（屏幕上出现新的绑定码）。",
    "删除前会先导出一份他的数据：服务器上存一份（数据库旁边的 exports/），这台电脑也会下载一份。",
    "想暂时不让他用，用「封禁」就够了。"], "导出并删除", u.name);
  if (!typed) return;
  const answer = await admAct("api/admin/users/" + u.id + "/delete", { confirm: typed });
  if (!answer) return;
  const blob = new Blob([JSON.stringify(answer.export, null, 1)], { type: "application/json" });
  const a = el("a");
  a.href = URL.createObjectURL(blob);
  a.download = "user-" + u.id + "-" + u.name + ".json";
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 10000);
  toast("已删除，导出在服务器 " + answer.file + "，也下载到了这台电脑", false, 6000);
  admUsers();
}
async function admLoginLink(u) {
  const ok = await admConfirm("给「" + u.name + "」一个登录链接", [
    "生成一个 5 分钟内有效、只能用一次的手机登录链接（和命令行 login-link 一样）。用它打开的手机会以「" + u.name + "」的身份登录。",
    "!只发给他本人：拿到链接的人就能进他的账号。这件事会记进操作记录。"], "生成", null, false);
  if (!ok) return;
  const answer = await admAct("api/admin/users/" + u.id + "/login-link", {});
  if (!answer) return;
  const count = el("p", "count", "");
  const end = Date.now() + answer.ttl * 1000;
  const tick = () => {
    const left = Math.max(0, Math.round((end - Date.now()) / 1000));
    count.textContent = left ? "还有 " + Math.floor(left / 60) + ":" + pad(left % 60) + " 过期" : "已经过期了";
    if (left && !$("modal").hidden && $("modal").contains(count)) setTimeout(tick, 1000);
  };
  const row = el("div", "row");
  row.append(button("复制链接", "ghost", () => admCopy(answer.link)), button("关闭", "go", closeOverlays));
  modal("「" + answer.name + "」的登录链接", el("p", "", "让他用手机相机扫这个码（或把链接发给他，在 Safari 里打开），点「在这台手机上登录」。"),
        admQr(answer.svg), count, el("div", "link", answer.link), row);
  tick();
}
async function admSessions(u) {
  let data;
  try { data = await admCall("GET", "api/admin/users/" + u.id + "/sessions"); } catch (error) { admFail(error); return; }
  const rows = data.sessions.map((s) => {
    const tr = el("tr");
    const where = admTd("", (ADM_KIND[s.kind] || s.kind) + " · " + s.browser);
    where.appendChild(el("small", "", (s.kind === "guest" ? "设备 " + (s.device_name || s.device_id) + " · " : "") + (s.ip_prefix || "")));
    const cell = admTd("n");
    cell.appendChild(button("退出", "linkbtn red", async () => {
      if (await admAct("api/admin/users/" + u.id + "/sessions/revoke", { id: s.id }, "已退出")) admSessions(u);
    }));
    tr.append(where, admTd("", ADM_VIA[s.via] || "普通登录"), admTd("m", when(s.created_at)), admTd("m", when(s.last_seen)), cell);
    return tr;
  });
  if (!rows.length) { const tr = el("tr"); const td = admTd("empty-t", "没有登录着的"); td.colSpan = 5; tr.appendChild(td); rows.push(tr); }
  const table = admTable("", "", ["哪里", "来路", "登录于", "最后使用", ""], rows);
  const row = el("div", "row");
  row.append(button("全部退出", "btn sm danger", async () => {
    if (!confirm("让「" + u.name + "」所有的手机和电脑都退出登录？他要重新登录才能用。")) return;
    const answer = await admAct("api/admin/users/" + u.id + "/sessions/revoke", { all: true });
    if (answer) { toast("退出了 " + answer.count + " 处"); admSessions(u); if (adm.sec === "users") admUsers(); }
  }), button("关闭", "ghost", closeOverlays));
  modal("「" + u.name + "」登录的地方", el("p", "", "包括他设备上的访客。退出后那台手机或电脑要重新登录。"), table, row);
  $("modal").classList.add("wide");
}
function admInvitePanel() {
  const panel = el("div", "panel");
  panel.innerHTML = `<h3>邀请家人 <small>链接 7 天内有效、只能用一次</small></h3>`;
  const line = el("div", "inline");
  const name = el("input");
  name.type = "text"; name.placeholder = "家人的名字（可不填）"; name.maxLength = 20; name.autocomplete = "off";
  const label = el("label", "chk");
  const box = el("input");
  box.type = "checkbox";
  label.append(box, document.createTextNode("也是管理员"));
  const go = admBtn("生成邀请", "go", "adm-invite", () => admBusy(go, async () => {
    const isAdmin = box.checked;
    if (isAdmin && !confirm("被邀请的人也会是管理员，能看到并管理所有人和所有设备。确定？")) return;
    const answer = await admCall("POST", "api/admin/invites", { name: name.value, admin: isAdmin });
    if (!answer.ok) { toast(answer.error, true); return; }
    name.value = "";
    box.checked = false;
    const row = el("div", "row");
    row.append(button("复制链接", "ghost", () => admCopy(answer.link)), button("关闭", "go", closeOverlays));
    modal(answer.role === "admin" ? "邀请链接（管理员）" : "邀请链接", el("p", "", "发给家人（微信就行），在手机浏览器里打开、点「接受邀请」，那台手机就登录了；之后电脑扫码登录。7 天内有效、只能用一次。"),
          admQr(answer.svg), el("div", "link", answer.link), row);
    admUsers();
  }));
  name.onkeydown = (event) => { if (event.key === "Enter") go.click(); };
  line.append(name, label, go);
  panel.appendChild(line);
  return panel;
}
function admInviteList(invites) {
  const rows = invites.map((i) => {
    const state = i.state === "used" ? "已接受 · " + (i.used_by_name || "（已删除）") : i.state === "expired" ? "已过期" : "等待接受";
    return admRow({ cls: i.state === "open" ? "air" : "", title: i.name || "（没写名字）",
                    tags: [i.role === "admin" ? admTag("管理员", "brand") : null, admTag(state, i.state === "open" ? "ok" : "")],
                    lines: ["生成 " + when(i.created_at) + " · 到期 " + when(i.expires_at)] });
  });
  return admList("最近的邀请", "", rows.length ? rows : [admEmpty("还没有邀请过")]);
}

// ======================================================================= 设备

async function admDevices() {
  const run = adm.run;
  let data;
  try { data = await admCall("GET", "api/admin/devices"); } catch (error) { admFail(error); return; }
  if (!admStill(run, "devices")) return;
  adm.devices = data.devices;
  const firmware = data.firmware || { latest: {}, flasher: "" };
  const live = data.devices.filter((d) => d.status !== "retired");
  const rows = data.devices.map((d) => admDeviceRow(d, firmware));
  const note = live.length + " 台" + (live.length !== data.devices.length ? "（另有 " + (data.devices.length - live.length) + " 条退役的旧记录）" : "");
  const hint = admHint("「重连」让设备断开再马上连上（新固件才认这个命令），封禁、解绑之后会自动做一次。" +
                       "「重新配对」：设备擦过 flash、密钥变了时，10 分钟内它用新密钥连上来就会被记住；被拒的设备标着「密钥不匹配」。" +
                       "旧固件的设备（legacy-…）不能解绑，先刷新固件再在命令行 adopt。");
  hint.style.margin = "-12px 0 22px";
  admSec(admList("所有设备", note, rows.length ? rows : [admEmpty("还没有设备连上来过")]), hint, admFirmwarePanel(firmware));
}
function admDeviceRow(d, firmware) {
  const lead = el("span", "av dev" + (d.online ? " on" : ""));
  lead.innerHTML = ic("tv", 18);
  const latest = (firmware.latest || {})[d.model];
  const status = (ADM_STATUS[d.status] || d.status) + (d.online ? " · 在线" : d.status === "active" ? " · 离线" : "") + (d.repair ? " · 等新密钥" : "");
  const where = el("small", d.status === "banned" ? "bad" : d.online ? "live" : "",
                   status + (d.online && (d.channel_name || d.channel) ? " · 正在播 " + (d.channel_name || d.channel) : "") +
                   " · 最后连接 " + when(d.last_seen) + (d.network ? " · " + d.network : "") + (d.guests ? " · " + d.guests + " 个访客" : ""));
  const acts = [], k = "d" + d.id;
  if (d.status === "active") {
    acts.push(admBtn("重连", "", k + "-re", async () => { await admAct("api/admin/devices/" + d.id + "/reconnect", {}, "已让它重连"); }),
              admBtn("改名", "", k + "-rename", () => admRenameDevice(d)));
    if (!d.legacy) acts.push(admBtn("重新配对", d.key_mismatch ? "linkbtn on" : "", k + "-repair", () => admRepairDevice(d)),
                             admBtn("解绑", "linkbtn red", k + "-unbind", () => admUnbindDevice(d)));
  }
  if (d.status === "unclaimed") acts.push(admBtn("改名", "", k + "-rename", () => admRenameDevice(d)));
  // 解绑过的设备也还记着旧密钥：没有「重新配对」它就一直被拒。
  if (d.status === "unclaimed" && d.key_mismatch) acts.push(admBtn("重新配对", "linkbtn on", k + "-repair", () => admRepairDevice(d)));
  if (d.status !== "retired") acts.push(admBtn(d.status === "banned" ? "解封" : "封禁", d.status === "banned" ? "" : "linkbtn red", k + "-ban", () => admBanDevice(d)));
  return admRow({ lead, title: d.name, cls: d.status === "retired" ? "dim" : "",
                  tags: [d.legacy ? admTag("旧固件") : null, d.owner_banned ? admTag("主人已封禁", "bad") : null,
                         d.key_mismatch ? admTag("密钥不匹配：点「重新配对」", "bad") : null,
                         latest && d.fw && d.fw !== latest ? admTag("可更新到 " + latest, "warn") : null],
                  lines: [d.id + " · " + d.model_name + " · " + (d.fw || "固件版本未知") + " · " + (d.owner_name ? "主人 " + d.owner_name : "没有主人"), where], acts });
}
// 怎么更新固件：服务器知道刷机页的地址（自托管包的 /flash/）就做成链接，否则只说去项目网站。
function admFirmwarePanel(firmware) {
  const panel = el("div", "panel");
  panel.id = "devUpdate";
  panel.appendChild(el("h3", "", "更新固件"));
  const p = el("p");
  p.append(document.createTextNode("在插着设备的那台电脑上，用 Chrome 或 Edge 打开"));
  if (firmware.flasher) {
    const a = el("a", "linkbtn", "刷机页");
    a.href = firmware.flasher; a.target = "_blank"; a.rel = "noopener";
    p.append(a, document.createTextNode("（" + firmware.flasher + "）"));
  } else {
    p.append(document.createTextNode("项目网站的「刷机」页"));
  }
  p.append(document.createTextNode("，连上设备后选「更新固件」：只写启动程序和固件本体，设置区不写，Wi-Fi、服务器地址和绑定都保留，设备不用重新配对。"));
  panel.appendChild(p);
  panel.appendChild(el("p", "", "网页串口只能在 https 页面或本机的 localhost 上用；设备插在家庭服务器那台电脑上时，在那台电脑上打开 http://localhost:8080/flash/。" +
                                (firmware.version ? "这次发布的固件是 " + firmware.version + "。" : "")));
  return panel;
}
async function admRenameDevice(d) {
  const name = await admAsk("给设备改名", d.id + " 的新名字（最多 20 个字）。", d.name);
  if (name && name !== d.name && await admAct("api/admin/devices/" + d.id + "/rename", { name }, "已改名")) admDevices();
}
async function admRepairDevice(d) {
  const ok = await admConfirm("重新配对「" + d.name + "」", [
    "设备擦过 flash（或换了固件）之后密钥变了，会被拒绝。点了之后的 10 分钟里，它用新密钥连上来就会被记住。",
    "!只在你确定是这台设备本身时用：这 10 分钟里别人冒充它的 id 也会被接受。"], "开始重新配对", null, false);
  if (ok && await admAct("api/admin/devices/" + d.id + "/repair", {}, "好的")) admDevices();
}
async function admUnbindDevice(d) {
  const ok = await admConfirm("解绑「" + d.name + "」", [
    "!它会从「" + (d.owner_name || "主人") + "」的账号里拿掉：断开、重连后屏幕上显示新的绑定码，访客全部作废，投屏记录删掉。",
    "主人的订阅和进度不受影响。之后谁扫了新的绑定码，设备就归谁。"], "解绑");
  if (ok && await admAct("api/admin/devices/" + d.id + "/unbind", {}, "已解绑")) admDevices();
}
async function admBanDevice(d) {
  if (d.status === "banned") {
    if (await admConfirm("解封「" + d.name + "」", ["它下次连上来就能正常用（有主人的回到主人那里，没有的显示绑定码）。"], "解封", null, false) &&
        await admAct("api/admin/devices/" + d.id + "/unban", {}, "已解封")) admDevices();
    return;
  }
  const ok = await admConfirm("封禁「" + d.name + "」", [
    "!它马上断开，之后每次连上来都被拒绝（屏幕上是连不上服务器）；它的访客全部退出。",
    "绑定关系保留，解封后恢复。"], "封禁");
  if (ok && await admAct("api/admin/devices/" + d.id + "/ban", {}, "已封禁")) admDevices();
}

// ======================================================================= 操作记录

const admAu = { user: "", device: "", action: "", actions: [] };
async function admAudit(more) {
  const run = adm.run;
  const params = new URLSearchParams();
  for (const key of ["user", "device", "action"]) if (admAu[key]) params.set(key, admAu[key]);
  if (more && adm.auNext) params.set("before", adm.auNext);
  let data;
  try {
    const jobs = [admCall("GET", "api/admin/audit?" + params)];
    if (!more) jobs.push(admCall("GET", "api/admin/users"), admCall("GET", "api/admin/devices"));
    const [audit, users, devices] = await Promise.all(jobs);
    data = audit;
    if (users) { adm.users = users.users; adm.me = users.me; }
    if (devices) adm.devices = devices.devices;
  } catch (error) { admFail(error); return; }
  if (!admStill(run, "audit")) return;
  adm.auNext = data.next;
  admAu.actions = data.actions;
  const names = Object.fromEntries(adm.users.map((u) => [String(u.id), u.name]));
  const devs = Object.fromEntries(adm.devices.map((d) => [d.id, d.name]));
  const rows = data.rows.map((r) => {
    const who = r.actor_user ? (r.actor_name || "（已删除的用户 #" + r.actor_user + "）") : "设备或系统";
    const target = r.target_kind === "user" ? "用户 " + (names[r.target_id] || "#" + r.target_id)
      : r.target_kind === "device" ? "设备 " + (devs[r.target_id] || r.target_id)
      : r.target_kind === "channels" ? "频道表" : r.target_kind === "epg" ? "节目单" : r.target_kind === "session" ? "登录"
      : r.target_kind === "invite" ? "邀请" : r.target_id || "";
    const code = el("em", "", r.action);
    code.style.fontFamily = "var(--mono)";
    code.style.fontSize = "12px";
    return admRow({ title: ADM_ACTIONS[r.action] || r.action,
                    lines: [when(r.at) + " · " + who + (target ? " → " + target : "") + (r.actor_ip_prefix ? " · " + r.actor_ip_prefix : ""), r.detail || ""], extra: code });
  });
  if (more) {
    const list = qs("#admSec .list .rows");
    if (list) list.append(...rows);
  } else {
    const box = el("div", "list");
    box.appendChild(el("h3", "", "最近的操作"));
    const inner = el("div", "rows");
    inner.append(...(rows.length ? rows : [admEmpty("没有符合条件的记录")]));
    box.appendChild(inner);
    const moreRow = el("div", "more-row");
    moreRow.appendChild(admBtn("更早的", "ghost", "adm-more", () => admAudit(true)));
    admSec(admAuditFilters(), box, moreRow);
  }
  const moreBtn = qs("#admSec .more-row");
  if (moreBtn) moreBtn.hidden = data.rows.length < 50;
}
function admAuditFilters() {
  const bar = el("div", "afilter");
  const select = (key, label, options) => {
    const s = el("select", "field");
    s.setAttribute("aria-label", label);
    s.append(...options);
    s.value = admAu[key];
    s.onchange = () => { admAu[key] = s.value; admAudit(false); };
    return s;
  };
  const users = [new Option("所有用户", ""), ...adm.users.map((u) => new Option(u.name + " #" + u.id, String(u.id)))];
  const devices = [new Option("所有设备", ""), ...adm.devices.map((d) => new Option(d.name + " · " + d.id.slice(-4), d.id))];
  const prefixes = [...new Set(admAu.actions.map((a) => a.split(".")[0] + "."))];
  const actions = [new Option("所有操作", ""), ...prefixes.map((p) => new Option(ADM_PREFIX[p] || p, p)),
                   ...admAu.actions.map((a) => new Option("· " + (ADM_ACTIONS[a] || a), a))];
  bar.append(select("user", "按用户筛选", users), select("device", "按设备筛选", devices), select("action", "按操作筛选", actions),
             admBtn("刷新", "ghost", "adm-au-reload", () => admAudit(false)));
  return bar;
}

// ======================================================================= 系统

async function admSystem() {
  const run = adm.run;
  let s;
  try { s = await admCall("GET", "api/admin/system"); } catch (error) { admFail(error); return; }
  if (!admStill(run, "system")) return;
  const rows = [];
  const item = (label, value, note, acts) => {
    const row = el("div", "li");
    row.appendChild(el("span", "k", label));
    const tx = el("span", "tx");
    tx.appendChild(el("b", "", value));
    if (note) tx.appendChild(el("small", "", note));
    row.appendChild(tx);
    if (acts) { const box = el("span", "acts"); box.append(...acts); row.appendChild(box); }
    rows.push(row);
  };
  item("版本", BRAND_ZH + " " + (s.version || "（未知）"), s.firmware_version ? "这次发布的固件：" + s.firmware_version : "");
  item("注册方式", "邀请，或扫一台未绑定小电视的绑定码自己注册（不用管理员批准）",
       "自己注册：每个网段每小时最多 " + s.signup_limits.net + " 个、全站每小时 " + s.signup_limits.all + " 个；注册的人会在「用户」里标成「待查看」。");
  item("未绑定的设备", "只显示「扫码绑定」画面和绑定码，不转码、不播任何台", "没有试看时间。");
  item("恢复登录", "设备状态页上的手势，给主人一个一次性手机登录链接", "每台设备每小时最多 " + s.recover_limit + " 个。");
  item("登录有效期", "手机 " + s.sessions_days.phone + " 天，电脑 " + s.sessions_days.pc + " 天，访客 " + s.sessions_days.guest + " 天");
  // 自建：没有旧设备、没有共享令牌，不给这个开关。
  if (!s.selfhost) {
    const toggle = admBtn(s.legacy_token ? "关闭" : "打开", s.legacy_token ? "linkbtn red" : "linkbtn on", "adm-legacy", async () => {
      const lines = s.legacy_token
        ? ["!关闭后，只带共享令牌、没有自己密钥的旧固件设备（legacy-…）都连不上了。", "所有设备都刷了新固件、并且 adopt 过之后再关。"]
        : ["打开后，旧固件的设备又能用共享令牌连上来（当作迁移来的旧设备）。"];
      if (await admConfirm((s.legacy_token ? "关闭" : "打开") + "共享配对令牌", lines, s.legacy_token ? "关闭" : "打开", null, !s.legacy_token ? false : undefined) &&
          await admAct("api/admin/system/legacy-token", { on: !s.legacy_token }, "已" + (s.legacy_token ? "关闭" : "打开"))) admSystem();
    });
    item("共享配对令牌", s.legacy_token ? "开着" : "关着", "旧固件只会用它；新固件每台有自己的密钥。电视服务下一次连接生效。", [toggle]);
  }
  if (s.selfhost) {
    item("在电脑上看", s.pc_watch ? "开着" : "关着", s.pc_watch ? (s.pc_warm ? "看直播时预热相邻的台：至多 " + s.pc_warm + " 路（换台快一点，多用一两个 ffmpeg）。" : "看直播时不预热相邻的台。") +
         "在 config.toml 的 [pc] warm_channels 里改（auto、on、off），改了要重启服务。" : "");
  }
  item("同时服务几台", "每个型号一个电视服务，一次服务一台", s.selfhost ? "" : "按设备排队和总数上限要等阶段 4（网关）。");
  item("网站地址", s.public_url || "（没配置 --public-url：二维码按浏览器的地址生成）");
  item("YouTube", s.youtube ? "已配置" : "没配置（只支持 B 站）", s.selfhost ? "在「设置 → YouTube」里改。" : "");
  item("数据库", s.db.name + " · " + (s.db.bytes / 1048576).toFixed(1) + " MB",
       s.db.users + " 个用户，" + s.db.devices + " 台设备，" + s.db.sessions + " 个登录，" + s.db.audit + " 条操作记录。" + (s.selfhost ? "" : "备份见运维手册（每晚 .backup）。"));
  item("频道表文件", s.channels.file || "（没配置 --main-channels）",
       s.channels.backup_count ? "旁边有 " + s.channels.backup_count + " 份保存时留下的备份（最多留 30 份）" : "");
  item("主机负载采样", s.sampler ? "在采样（每 5 秒，内存里留 24 小时）" : "没有采样（不是 Linux）");
  const list = el("div", "list kv");
  list.appendChild(el("h3", "", "设置和现状"));
  list.append(...rows);
  const hint = admHint("这里大多只是显示：注册方式、未绑定设备的规矩和限额写在代码里" + (s.selfhost ? "，服务的设置在数据目录的 config.toml 里" : "（见运维手册），改了要重新部署") + "。");
  hint.style.margin = "-12px 0 22px";
  admSec(list, hint);
}

// ======================================================================= 频道表
// 在页面里改一份副本，保存时整张发过去：没改的台只发 id，改了的发改了的字段，新台没有 id（admin.py 的 save_channels）。

// ach.rows：{id?, name, url, agent, group, routes: [{url, ua?}]（备用线路）, checks: {地址: {ok, why, at}}, good, orig?}
const ach = { data: null, rows: [], imp: [], impKind: "", impSource: "", epg: null, epgSources: [], filter: "", impFilter: "", problems: null,
              checking: false, probing: false, tab: "list" };
const ACH_DUP = { url: "已在表里", name: "表里有同名", list: "列表里重复" };
function achRoutesKey(routes) { return JSON.stringify((routes || []).map((x) => [x.url, x.ua || ""])); }
function achKey(r) { return [r.name, r.url, r.agent, r.group, achRoutesKey(r.routes)].join("\u0001"); }
function achDirty() {
  if (!ach.data || !ach.data.configured) return false;
  if (ach.rows.length !== ach.data.rows.length) return true;
  return ach.rows.some((r, i) => !r.id || r.id !== ach.data.rows[i].id || achKey(r) !== achKey(r.orig));
}
async function achLoad(keepMessage) {
  const run = adm.run;
  let data;
  try { data = await admCall("GET", "api/admin/channels"); } catch (error) { admFail(error); return; }
  if (!admStill(run, "channels")) return;
  ach.data = data;
  if (!data.configured) {
    const empty = el("div", "empty");
    empty.innerHTML = `<div class="eic">${ic("list", 40)}</div><b>没有全局频道表</b><p>中转没有配置 --main-channels：没有全局频道表可以编辑。</p>`;
    admSec(empty);
    return;
  }
  ach.rows = data.rows.map((r) => ({ id: r.id, name: r.name, url: r.url, agent: r.agent, group: r.group,
                                     routes: (r.routes || []).map((x) => ({ ...x })), checks: { ...(r.checks || {}) }, good: r.good || "", orig: r }));
  if (!keepMessage) ach.problems = null;
  if (data.bad.length) ach.problems = { ok: false, lines: ["文件里第 " + data.bad.join("、") + " 行看不懂：为了不把它们弄丢，这里不能保存。请先在服务器上改好文件。"] };
  achShell();
  if (ach.imp.length) { achImpMark(); achRenderImport(); }
  achLoadEpg();
}
// 频道表这一页：上面一条标签（频道 / 导入 / 节目单），一次只显示一个；最下面浮着的保存条只管「频道」里的手动修改，
// 导入和「加一个台」点了就直接保存。长说明都收进「说明」里。
function achShell() {
  const d = ach.data;
  const box = el("div");
  box.id = "achPage";
  box.innerHTML = `
    <div class="problems" id="achProblems" hidden></div>
    <nav class="chips achtabs" id="achTabs" aria-label="频道表分类">
      <button data-tab="list">${ic("live", 16)}频道 <em id="achCount"></em></button>
      <button data-tab="import" id="achTabImport">${ic("plus", 16)}导入</button>
      <button data-tab="epg" id="achTabEpg" hidden>${ic("guide", 16)}节目单</button>
    </nav>
    <div class="achpane" data-pane="list">
      <div class="inline achtools">
        <input type="search" id="achFilter" placeholder="找台" autocomplete="off">
        <button class="ghost" id="achAddToggle">${ic("plus", 16)}加一个台</button>
        <button class="ghost" id="achCheckAll">检测全部线路</button>
        <span class="hint" id="achCheckState"></span>
      </div>
      <div class="addrow" id="achAddRow" hidden>
        <input type="text" id="achNewName" placeholder="名字，例如 CCTV13" maxlength="40" autocomplete="off">
        <input type="text" id="achNewUrl" placeholder="地址 http(s)://…" autocomplete="off" spellcheck="false">
        <input type="text" id="achNewUa" placeholder="User-Agent（可不填）" autocomplete="off" spellcheck="false">
        <button class="go" id="achAdd">加入</button>
      </div>
      <div class="tbl chedit" id="achTable"></div>
      <p class="hint" id="achFitLine"></p>
      <details class="more-info">
        <summary>说明</summary>
        <p>名字、分组直接在表里改，改完点下面的「保存」；顺序就是设备上换台的顺序。一个台可以有几条线路（主线路 + 备用），放不起来时自动换下一条。「检测」只连一下，不解码，很轻；能连上不等于一定能放。</p>
        <p id="achBackups"></p>
        <div class="tbl" id="achFit"></div>
      </details>
    </div>
    <div class="achpane" data-pane="import" id="achImport" hidden>
      <div class="subsbox" id="achSubs" hidden>
        <b>订阅的直播源</b>
        <p class="hint">每 ${d.subs_every_h || 12} 小时自动重新读一次：表里已有的台补上新线路、换掉打不开的；表里没有的台不自动加，点「看新台」挑。</p>
        <div id="achSubList"></div>
      </div>
      <div id="achPresetBox" hidden>
        <p class="disclaimer">下面是网上公开的第三方列表，内容由维护者负责，本项目不提供任何直播源；请只添加你有权观看的内容。</p>
        <div class="presets" id="achPresets"></div>
      </div>
      <div class="improw">
        <input type="text" id="achImpUrl" placeholder="或者填播放列表地址（.m3u / .txt）" autocomplete="off" spellcheck="false">
        <button class="ghost" id="achImpFetch">读取</button>
        <button class="ghost" id="achImpSub" title="记住这个地址，定时自动更新线路">订阅</button>
        <label class="ghost" for="achImpFile" tabindex="0" id="achImpPick">选文件</label>
        <input type="file" id="achImpFile" accept=".m3u,.m3u8,.txt,audio/x-mpegurl" hidden>
      </div>
      <div id="achImpList" hidden>
        <p class="hint" id="achImpSource"></p>
        <div class="inline">
          <span class="chips" id="achImpKinds" role="group" aria-label="快速筛选" style="margin:0"><button data-kind="" aria-pressed="true">全部</button><button data-kind="央视" aria-pressed="false">央视</button><button data-kind="卫视" aria-pressed="false">卫视</button></span>
          <input type="search" id="achImpFilter" placeholder="找台" style="max-width:200px;flex:0 1 200px" autocomplete="off">
          <button class="ghost" id="achImpAll">全选</button>
          <button class="ghost" id="achImpNone">全不选</button>
          <button class="ghost" id="achImpProbe">试一下选中的</button>
          <button class="go" id="achImpAdd">加入</button>
          <span class="hint" id="achImpCount" style="margin:0"></span>
        </div>
        <div class="tbl chedit implist" id="achImpTable" style="background:var(--fill)"></div>
      </div>
      <details class="more-info">
        <summary>说明</summary>
        <p>地址只在点「读取」时取一次，不会保存。同名（或同 tvg-id）的几行、TXT 里 # 隔开的几个地址合成一个台的几条线路（每台最多 ${d.routes_max || 5} 条）。表里已经有的台，新地址并进去当备用线路，放不起来时自动换。小电视只收表里前 ${d.limits.channels} 个台，常看的往前挪。「试一下」一次开两个 ffmpeg，各取一帧，再点一下停。</p>
      </details>
    </div>
    <div class="achpane" data-pane="epg" id="achEpg" hidden>
      <p class="disclaimer">下面是网上公开的第三方节目单，内容由维护者负责；请只添加你有权使用的内容。</p>
      <div class="presets" id="achEpgPresets"></div>
      <div class="improw">
        <input type="text" id="achEpgUrl" placeholder="或者填节目单地址（XMLTV，.xml / .xml.gz）" autocomplete="off" spellcheck="false">
        <button class="ghost" id="achEpgAdd">加上</button><span></span>
      </div>
      <ul class="epgstat" id="achEpgStatus"></ul>
      <div class="inline">
        <button class="go" id="achEpgSave">保存</button>
        <button class="ghost" id="achEpgRefresh">现在更新</button>
        <span class="hint" id="achEpgState" style="margin:0"></span>
      </div>
      <details class="more-info">
        <summary>说明</summary>
        <p>直播台名下面显示「正在播：…」。服务器每 <span id="achEpgHours">8</span> 小时下载一次（存在数据目录的 epg/ 里），只留表里有的台、前后一天的节目；按台名和 tvg-id 对台，CCTV-1、CCTV1综合、湖南卫视HD 这类写法都认。最多 <span id="achEpgMax">3</span> 个来源，排前面的优先；关掉就不再下载。</p>
      </details>
    </div>
    <div class="savebar" role="region" aria-label="保存频道表">
      <span class="state" id="achState" role="status">没有改动</span>
      <button class="ghost" id="achReset">放弃修改</button>
      <button class="ghost" id="achCheck">检查</button>
      <button class="go" id="achSave">保存</button>
    </div>`;
  admSec(box);
  $("achTabImport").hidden = !d.import;
  if (d.import) achRenderPresets();
  for (const b of $("achTabs").children) b.onclick = () => achTab(b.dataset.tab);
  $("achFilter").value = ach.filter;
  $("achFilter").oninput = () => { ach.filter = $("achFilter").value; achRender(); };
  $("achAddToggle").onclick = () => { const row = $("achAddRow"); row.hidden = !row.hidden; if (!row.hidden) $("achNewName").focus(); };
  $("achAdd").onclick = () => admBusy($("achAdd"), achAddOne);
  for (const id of ["achNewName", "achNewUrl", "achNewUa"]) $(id).onkeydown = (event) => { if (event.key === "Enter") $("achAdd").click(); };
  $("achReset").onclick = () => { if (confirm("放弃所有没保存的修改？")) achLoad(); };
  $("achCheck").onclick = () => admBusy($("achCheck"), achCheck);
  $("achSave").onclick = () => admBusy($("achSave"), achSave);
  $("achCheckAll").onclick = achCheckAll;
  achAutoLine();
  achWireImport();
  achWireEpg();
  achRenderFit(d.fit);
  $("achBackups").textContent = d.backups.length ? "最近的备份（和频道表在同一个目录）：" + d.backups.slice().reverse().join("、") : "保存时会先把原文件复制一份备份。";
  if (ach.imp.length) {        // 保存后重画：刚才读的列表还在
    $("achImpList").hidden = false;
    $("achImpSource").textContent = ach.impSource;
  }
  achShowProblems();
  achRender();
  achTab(ach.tab);
}
// 换标签：没有的标签（云端没有导入、没开节目单）退回「频道」。
function achTab(tab) {
  const btn = qs('#achTabs [data-tab="' + tab + '"]');
  if (!btn || btn.hidden) tab = "list";
  ach.tab = tab;
  for (const b of $("achTabs").children) b.setAttribute("aria-pressed", String(b.dataset.tab === tab));
  for (const pane of qsa("#achPage .achpane")) pane.hidden = pane.dataset.pane !== tab || (tab === "import" && !ach.data.import) || (tab === "epg" && !(ach.epg && ach.epg.available));
}
function achShowProblems() {
  const box = $("achProblems");
  if (!box) return;
  const p = ach.problems;
  box.hidden = !p || !p.lines.length;
  if (!p) return;
  box.className = "problems" + (p.ok ? " ok" : "");
  box.replaceChildren(...p.lines.map((line) => el("div", "", line)));
}
function achProblems(lines, ok) { ach.problems = { lines, ok: !!ok }; achShowProblems(); }
function achRenderFit(fit) {
  const d = ach.data;
  const rows = (fit || []).map((f) => {
    const tr = el("tr");
    const state = admTd("");
    state.appendChild(admTag(f.fits ? "全部" : "前 " + (f.shown != null ? f.shown : d.limits.channels) + " 个", f.fits ? "ok" : ""));
    tr.append(admTd("", f.user || "（还没有绑定设备的用户）"), admTd("n", f.channels + " / " + d.limits.channels), admTd("n", f.bytes + " / " + d.limits.bytes), state);
    return tr;
  });
  const box = admTable("每个用户的小电视收到几个台", "投屏台 + 他的 UP 主和剧集 + 这张表的前面部分；电脑上都能看", ["用户", "#台数", "#约多少字节", "小电视上"], rows);
  $("achFit").replaceChildren(...box.childNodes);
  // 最少的那台：小电视上显示前几个（服务端算的 shown；全部放得下时就是全部）。
  const cut = (fit || []).filter((f) => !f.fits);
  const shown = cut.length ? Math.min(...cut.map((f) => f.table_shown != null ? f.table_shown : f.shown != null ? f.shown : d.limits.channels)) : null;
  $("achFitLine").textContent = shown != null ? "小电视上只显示前 " + shown + " 个台（常看的往前挪）；电脑上都能看" : "";
}
function achGroupSelect(r) {
  const select = el("select", "field");
  select.setAttribute("aria-label", "「" + r.name + "」的分组");
  const auto = new Option("自动（" + (r.orig ? r.orig.auto : "按名字") + "）", "");
  select.appendChild(auto);
  for (const g of ach.data.groups) select.appendChild(new Option(g, g));
  select.value = r.group || "";
  select.onchange = () => { r.group = select.value; achState(); };
  return select;
}
function achRowCls(r) { return !r.id ? "new" : achKey(r) !== achKey(r.orig) ? "changed" : ""; }
function achRender() {
  const box = $("achTable");
  if (!box) return;
  const k = focusKey();
  const filter = ach.filter.trim().toLowerCase();
  const rows = [];
  ach.rows.forEach((r, i) => {
    if (filter && !r.name.toLowerCase().includes(filter) && !(r.id || "").includes(filter)) return;
    const tr = el("tr", achRowCls(r));
    const name = el("input");
    name.type = "text"; name.value = r.name; name.maxLength = 40; name.setAttribute("aria-label", "第 " + (i + 1) + " 个台的名字");
    name.oninput = () => { r.name = name.value; achState(); tr.className = achRowCls(r); };
    const url = el("input", "url");
    url.type = "text"; url.value = r.url; url.title = r.url; url.spellcheck = false; url.setAttribute("aria-label", "地址");
    url.oninput = () => { r.url = url.value.trim(); url.title = r.url; achState(); tr.className = achRowCls(r); };
    const ua = el("input", "ua");
    ua.type = "text"; ua.value = r.agent; ua.placeholder = "默认"; ua.spellcheck = false; ua.setAttribute("aria-label", "User-Agent");
    ua.oninput = () => { ua.title = ua.value; r.agent = ua.value.trim(); achState(); tr.className = achRowCls(r); };
    const cells = [admTd("no", String(i + 1)), admTd("id", r.id || "新"), admTd(""), admTd(""), admTd(""), admTd(""), admTd("ln"), admTd("a")];
    cells[2].appendChild(name); cells[2].style.minWidth = "150px";
    cells[3].appendChild(achGroupSelect(r));
    cells[4].appendChild(url); cells[4].style.minWidth = "240px";
    cells[5].appendChild(ua); cells[5].style.width = "140px";
    const key = r.id || "new" + i;
    cells[6].appendChild(achLineSummary(r, key));
    const up = admBtn("↑", "", "ch-" + key + "-up", () => achMove(i, -1));
    up.setAttribute("aria-label", "上移"); up.disabled = i === 0;
    const down = admBtn("↓", "", "ch-" + key + "-down", () => achMove(i, 1));
    down.setAttribute("aria-label", "下移"); down.disabled = i === ach.rows.length - 1;
    cells[7].append(up, down, admBtn("删除", "linkbtn red", "ch-" + key + "-del", () => { ach.rows.splice(i, 1); achRender(); if (ach.imp.length) { achImpMark(); achRenderImport(); } }));
    tr.append(...cells);
    rows.push(tr);
  });
  if (!rows.length) { const tr = el("tr"); const td = admTd("empty-t", filter ? "没有名字里带「" + filter + "」的台" : "表是空的"); td.colSpan = 8; tr.appendChild(td); rows.push(tr); }
  const table = admTable("", "", ["#", "id", "名字", "分组", "地址（主线路）", "User-Agent", "线路", ""], rows);
  box.replaceChildren(...table.childNodes);
  restoreFocus(k);
  achState();
}
function achMove(i, step) {
  const j = i + step;
  if (j < 0 || j >= ach.rows.length) return;
  [ach.rows[i], ach.rows[j]] = [ach.rows[j], ach.rows[i]];
  achRender();
}
function achState() {
  if (!ach.data || !$("achState")) return;
  const dirty = achDirty();
  $("achCount").textContent = String(ach.rows.length);
  $("achState").textContent = dirty ? "有没保存的修改（" + ach.rows.length + " 个台）" : "没有改动";
  $("achState").className = "state" + (dirty ? " dirty" : "");
  $("achSave").disabled = $("achReset").disabled = !dirty;
  // 没有改动时收起浮着的保存条（「检查」「保存」只对改动有意义）；有问题提示时照样在上面的红框里。
  const bar = $("achState").closest(".savebar");
  if (bar) bar.classList.toggle("clean", !dirty);
  if (ach.imp.length) achImpCount();
}
async function achAddOne() {
  const name = $("achNewName").value.trim(), url = $("achNewUrl").value.trim(), agent = $("achNewUa").value.trim();
  if (!name || !/^https?:\/\//.test(url)) { toast("要有名字，地址要以 http:// 或 https:// 开头", true); return; }
  ach.rows.push({ name, url, agent, group: "", routes: [], checks: {}, good: "" });
  $("achNewName").value = $("achNewUrl").value = $("achNewUa").value = "";
  ach.filter = $("achFilter").value = "";
  achRender();
  await achSaveNow("加了「" + name + "」");
}
function achPayload(dry) {
  const rows = ach.rows.map((r) => {
    // 从播放列表导入的新台带着 tvg-id / 台标（服务器存在 channels.txt 旁边，节目单对台用）。
    if (!r.id) return { name: r.name, url: r.url, agent: r.agent, group: r.group, tvg_id: r.tvg_id, tvg_name: r.tvg_name, logo: r.logo, routes: r.routes || [] };
    const o = r.orig, item = { id: r.id };
    if (r.name !== o.name) item.name = r.name;
    if (r.url !== o.url) item.url = r.url;
    if (r.agent !== o.agent) item.agent = r.agent;
    if (r.group !== o.group) item.group = r.group;
    // 备用线路只在改过时发（云端的请求体只有 8 KB）。
    if (achRoutesKey(r.routes) !== achRoutesKey(o.routes)) item.routes = r.routes || [];
    return Object.keys(item).length === 1 ? r.id : item;
  });
  return { version: ach.data.version, rows, dry };
}
function achDescribe(s) {
  const parts = [];
  if (s.added) parts.push("新加 " + s.added + " 个");
  if (s.removed) parts.push("删掉 " + s.removed + " 个");
  if (s.changed) parts.push("改了 " + s.changed + " 个");
  if (s.moved) parts.push("调了顺序");
  if (s.groups) parts.push("改了分组");
  if (s.routes) parts.push(s.routes + " 个台的线路有改动");
  return (parts.join("，") || "没有实质改动") + "；保存后一共 " + s.total + " 个台";
}
async function achSubmit(dry) {
  // 服务端要用电视服务的解析器在子进程里查一遍，要一两秒。
  $("achState").textContent = dry ? "正在检查…" : "正在保存…";
  let answer;
  try { answer = await admCall("POST", "api/admin/channels", achPayload(dry)); } finally { achState(); }
  if (!answer.ok) {
    achProblems(answer.problems || [answer.error]);
    if (answer.fit) achRenderFit(answer.fit);
    if (answer.stale) toast("频道表被别处改过了，请刷新", true);
    return null;
  }
  return answer;
}
async function achCheck() {
  const answer = await achSubmit(true);
  if (!answer) return;
  achRenderFit(answer.fit);
  achProblems(["检查通过：" + achDescribe(answer.summary) + "。"].concat(answer.warnings || []), true);
}
async function achSave() {
  const dry = await achSubmit(true);
  if (!dry) return;
  const removed = ach.data.rows.filter((r) => !ach.rows.some((x) => x.id === r.id)).map((r) => r.name);
  const lines = [achDescribe(dry.summary) + "。",
                 "保存前会先备份原文件。每台小电视下次换台或重新连接时拿到新的频道表，正在看的不受影响，不用重启服务。"];
  if (removed.length) lines.unshift("!要删掉的台：" + removed.slice(0, 12).join("、") + (removed.length > 12 ? " 等 " + removed.length + " 个" : "") +
                                    "。正在看它们的设备下次换台时会回到别的台；用户给它们调的分组也就没用了。");
  if (!await admConfirm("保存频道表", lines.concat(dry.warnings || []), "保存", null, false)) return;
  const answer = await achSubmit(false);
  if (!answer) return;
  toast("已保存" + (answer.backup ? "，原文件备份为 " + answer.backup : ""));
  achProblems(["已保存：" + achDescribe(answer.summary) + "。" + (answer.backup ? "原文件备份为 " + answer.backup + "。" : ""),
               "每台小电视下次换台或重新连接时拿到新的频道表。"].concat(answer.warnings || []), true);
  achLoad(true);
}

// 不问就保存（加台、导入、并为备用线路）：服务端照样先检查；没过就留着没保存的修改和问题提示。
// 表里还有手动删掉的台时，走「保存」那条要确认的路。
async function achSaveNow(what) {
  if (ach.data.rows.some((r) => !ach.rows.some((x) => x.id === r.id))) { await achSave(); return; }
  const answer = await achSubmit(false);
  if (!answer) { toast(what + "，但没保存成：看上面的提示", true); return; }
  toast(what + "，已保存");
  ach.problems = null;
  await achLoad(true);
}

// ---------- 线路（主线路 + 备用线路，server/routes.py） ----------
// 主线路就是表里的「地址」和 User-Agent（channels.txt）；备用线路存在频道表旁边的 meta 里。设备和电脑上看都先试
// 上次放得起来的那条，起不来换下一条。改线路和改别的一样，要点下面的「保存」。

function achRoutesMax() { return (ach.data && ach.data.routes_max) || 5; }
function achLines(r) { return [{ url: r.url, ua: r.agent || "" }].concat((r.routes || []).map((x) => ({ url: x.url, ua: x.ua || "" }))); }
function achSetLines(r, lines) {
  r.url = lines[0].url; r.agent = lines[0].ua || "";
  r.routes = lines.slice(1).map((x) => (x.ua ? { url: x.url, ua: x.ua } : { url: x.url }));
}
function achCheckText(c) {
  if (!c) return "";
  // ms：从取列表到拿到一个片段的头几 KB 用了多久（server/relay/routecheck.py），越短越快。
  return (c.ok ? (c.ms ? "✓ " + achSecs(c.ms) : "✓ 能连上") : "✗ " + (c.why || "连不上")) + (c.at ? " · " + when(c.at) : "");
}
function achSecs(ms) { return (ms / 1000).toFixed(1) + " 秒"; }
// 检测过、能连上的线路里最快的那条该不该当主线路：是第几条（从 0 数），不用换时 -1。只在主线路不通、没检测过，
// 或者最快的比它快 40% 以上时换（和服务端 routes.FASTER 一样：差得不多的多半是网络抖动）。
const ACH_FASTER = 0.6;
function achFastest(r) {
  const lines = achLines(r), checks = r.checks || {};
  let best = -1;
  lines.forEach((x, k) => {
    const c = checks[x.url];
    if (c && c.ok && c.ms && (best < 0 || c.ms < checks[lines[best].url].ms)) best = k;
  });
  if (best <= 0) return -1;
  const main = checks[lines[0].url];
  return !main || !main.ok || !main.ms || checks[lines[best].url].ms <= main.ms * ACH_FASTER ? best : -1;
}
// 表里「线路」一格：几条线路、检测过的几条通几条不通；点开是线路编辑。
function achLineSummary(r, key) {
  const lines = achLines(r), box = el("div", "lsum");
  const checked = lines.map((x) => r.checks && r.checks[x.url]).filter(Boolean);
  const ok = checked.filter((c) => c.ok).length, bad = checked.length - ok;
  const b = admBtn(lines.length + " 条", "linkbtn", "ch-" + key + "-lines", () => achRouteEditor(r));
  b.title = "查看、检测、调整这个台的线路";
  box.appendChild(b);
  if (ok) box.append(admTag("✓" + ok, "ok"));
  if (bad) box.append(admTag("✗" + bad, "bad"));
  const times = checked.filter((c) => c.ok && c.ms).map((c) => c.ms);
  if (times.length) box.appendChild(el("small", "", "最快 " + achSecs(Math.min(...times))));
  if (checked.length) {
    const last = Math.max(...checked.map((c) => c.at || 0));
    if (last) box.appendChild(el("small", "", "检测于 " + when(last)));
  }
  if (r.id && !(r.routes || []).length && !checked.length) {
    box.appendChild(admBtn("检测", "linkbtn", "ch-" + key + "-check", async () => { if (await achCheckOne(r)) achRender(); }));
  }
  return box;
}
// 检测一个台（已保存的线路）：服务端一条一条连（列表 + 一个片段的头几 KB），结果记在 meta 里。
async function achCheckOne(r, quiet) {
  if (!r.id) { if (!quiet) toast("新加的台先保存，再检测", true); return null; }
  if (r.orig && achRoutesKey(achLines(r)) !== achRoutesKey(achLines(r.orig))) { if (!quiet) toast("这个台的线路改过了：先保存，再检测", true); return null; }
  let answer;
  try { answer = await admCall("POST", "api/admin/channels/check", { id: r.id }); }
  catch (error) { if (quiet) throw error; admFail(error); return null; }
  if (!answer.ok) { if (!quiet) toast(answer.error || "没检测成", true); return null; }
  for (const x of answer.results) {
    r.checks[x.url] = { ok: x.ok, why: x.why, at: x.at };
    if (x.ok && x.ms) r.checks[x.url].ms = x.ms;
    if (r.orig) { r.orig.checks = r.orig.checks || {}; r.orig.checks[x.url] = r.checks[x.url]; }
  }
  return answer;
}
// 后台在自动检测线路（中转的 linecheck.py，自建 / 电脑客户端）：不用点「检测全部线路」，结果已经在表里；
// 这里一句话说上次什么时候测的、几个台全部线路不通（鼠标停上去看是哪几个）。
function achAutoLine() {
  const a = ach.data && ach.data.auto, box = $("achCheckState");
  if (!a || !box || ach.checking) return;
  const names = a.dead.map((id) => (ach.rows.find((r) => r.id === id) || {}).name).filter(Boolean);
  box.textContent = "后台在自动检测：" + (a.last ? "上次 " + ago(a.last) : "马上开始")
    + (a.paused ? " · 正在放的多，先停一下" : "")
    + (names.length ? " · " + names.length + " 个台全部线路不通" : a.checked ? " · 没有打不开的台" : "");
  box.title = names.length ? names.slice(0, 20).join("、") + (names.length > 20 ? "……" : "") : "";
}
// 检测全部线路：同时至多两个台（服务端也只让两个台同时测）；再点一下停。
async function achCheckAll() {
  const btn = $("achCheckAll");
  if (ach.checking) { ach.checking = false; return; }
  const todo = ach.rows.filter((r) => r.id);
  if (!todo.length) { toast("表里还没有保存过的台", true); return; }
  ach.checking = true;
  btn.textContent = "停止检测";
  let next = 0, done = 0, bad = 0;
  const say = (text) => { const box = $("achCheckState"); if (box) box.textContent = text; };
  const state = () => say("检测中：" + done + " / " + todo.length + " 个台" + (bad ? "，" + bad + " 个台有线路不通" : ""));
  state();
  const worker = async () => {
    while (ach.checking && next < todo.length && adm.sec === "channels") {
      const r = todo[next++];
      try {
        const answer = await achCheckOne(r, true);
        if (answer && answer.results.some((x) => !x.ok)) bad++;
      } catch (error) { ach.checking = false; admFail(error); }
      done++;
      state();
      achRender();
    }
  };
  await Promise.all([worker(), worker()]);
  const stopped = !ach.checking || done < todo.length;
  ach.checking = false;
  if ($("achCheckAll") === btn) btn.textContent = "检测全部线路";
  say((stopped ? "停了：" : "检测完了：") + done + " 个台" +
      (bad ? "，" + bad + " 个台有线路不通（点「线路」看）" : "，都能连上"));
  achOfferFastest();
}
// 检测完了：有台的备用线路比主线路快很多（或主线路不通）时，给一个「把最快的设为主线路」，一下子换好，
// 和线路编辑里的「设为主线路」一样，要点「保存」才生效。
function achOfferFastest() {
  const box = $("achCheckState"), todo = ach.rows.filter((r) => achFastest(r) > 0);
  if (!box || !todo.length) return;
  const b = button("把最快的设为主线路（" + todo.length + " 个台）", "linkbtn", () => {
    let moved = 0;
    for (const r of ach.rows) {
      const k = achFastest(r);
      if (k <= 0) continue;
      const lines = achLines(r);
      lines.unshift(lines.splice(k, 1)[0]);
      achSetLines(r, lines);
      moved++;
    }
    b.remove();
    achRender();
    toast(moved + " 个台的主线路换成了最快的那条，点「保存」生效");
  });
  b.title = "只换主线路不通、或者备用线路快 40% 以上的台";
  box.append(" ", b);
}
function achRouteEditor(r) {
  const lines = achLines(r);
  const again = () => { achRender(); achRouteEditor(r); };
  const edit = (change) => { const copy = achLines(r); change(copy); achSetLines(r, copy); again(); };
  const rows = lines.map((x, k) => {
    const tr = el("tr");
    const u = admTd("u");
    u.append(el("b", "", "线路 " + (k + 1) + (k === 0 ? "（主线路）" : "") + (r.good === x.url && lines.length > 1 ? " · 上次放得起来" : "")),
             el("span", "", x.url));
    if (x.ua) u.appendChild(el("small", "", "UA：" + x.ua));
    const c = r.checks && r.checks[x.url];
    const st = admTd("s");
    if (c) st.appendChild(el("span", c.ok ? "ok" : "bad", achCheckText(c))); else st.textContent = "没检测过";
    const a = admTd("a");
    if (k > 0) a.append(button("设为主线路", "linkbtn", () => edit((l) => l.unshift(l.splice(k, 1)[0]))));
    if (k > 0) { const b = button("↑", "linkbtn", () => edit((l) => { [l[k - 1], l[k]] = [l[k], l[k - 1]]; })); b.setAttribute("aria-label", "上移"); a.append(b); }
    if (k < lines.length - 1) { const b = button("↓", "linkbtn", () => edit((l) => { [l[k + 1], l[k]] = [l[k], l[k + 1]]; })); b.setAttribute("aria-label", "下移"); a.append(b); }
    if (lines.length > 1) a.append(button("删除", "linkbtn red", () => edit((l) => l.splice(k, 1))));
    tr.append(u, st, a);
    return tr;
  });
  const table = admTable("", "", ["线路", "检测", ""], rows, "routes");
  const nodes = [el("p", "", "放不起来时按这个顺序换线路；上次放得起来的那条先试。改了要点频道表下面的「保存」。"), table];
  if (lines.length < achRoutesMax()) {
    const add = el("div", "routeadd"), url = el("input"), ua = el("input");
    url.type = ua.type = "text"; url.placeholder = "备用线路地址 http(s)://…"; ua.placeholder = "User-Agent（可不填）";
    url.autocomplete = ua.autocomplete = "off"; url.spellcheck = ua.spellcheck = false;
    const go = button("加上", "ghost", () => {
      const v = url.value.trim();
      if (!/^https?:\/\/\S+$/.test(v) || v.includes("|")) { toast("地址要以 http:// 或 https:// 开头，不能有空格和竖线", true); return; }
      if (lines.some((x) => x.url === v)) { toast("这条线路已经有了", true); return; }
      const copy = achLines(r);
      copy.push({ url: v, ua: ua.value.trim() });
      achSetLines(r, copy); again();
    });
    url.onkeydown = ua.onkeydown = (event) => { if (event.key === "Enter") go.click(); };
    add.append(url, ua, go);
    nodes.push(add);
  } else nodes.push(el("p", "", "一个台最多 " + achRoutesMax() + " 条线路。"));
  const row = el("div", "row");
  const check = button("检测这些线路", "ghost");
  check.onclick = () => admBusy(check, async () => {
    check.textContent = "正在检测…";
    if (await achCheckOne(r)) again(); else check.textContent = "检测这些线路";
  });
  row.append(check, button("好", "go", closeOverlays));
  nodes.push(row);
  modal((r.name || "新台") + " 的线路", ...nodes);
  $("modal").classList.add("xwide");
}
// 导入的一项重复了表里的哪个台：先按它的主地址，再按名字，最后按它的备用地址。
function achImpTarget(r) {
  const key = achNameKey(r.name);
  const has = (urls) => ach.rows.find((c) => achLines(c).some((x) => urls.includes(x.url)));
  return has([r.url]) || ach.rows.find((c) => achNameKey(c.name) === key) || has((r.routes || []).map((x) => x.url)) || null;
}
// 导入的一项怎么加：表里没有这个台的，加成新台（add）；有的（同名或地址一样），把它表里还没有的地址并进那个台
// 当备用线路（target + fresh，一个台可以有几条线路切换）；地址全都已经在那个台里的，没什么可加（fresh 为空）。
function achImpPlan(r) {
  const target = r.dup ? achImpTarget(r) : null;
  if (!target) return { add: true, target: null, fresh: [] };
  const have = new Set(achLines(target).map((x) => x.url));
  const fresh = [{ url: r.url, ua: r.agent || "" }].concat((r.routes || []).map((x) => ({ url: x.url, ua: x.ua || "" })))
    .filter((x) => !have.has(x.url));
  return { add: false, target, fresh };
}
function achImpUseful(r) { const plan = achImpPlan(r); return plan.add || plan.fresh.length > 0; }

// ---------- 节目单（server/relay/epg.py） ----------
// 来源是地址的列表（最多 max 个，排前面的优先）；预设只是一点就加上的地址。改了要点「保存」。

async function achLoadEpg() {
  const run = adm.run;
  let data;
  try { data = await admCall("GET", "api/admin/epg"); } catch (error) { admFail(error); return; }
  if (!admStill(run, "channels") || !$("achEpg")) return;
  ach.epg = data;
  $("achTabEpg").hidden = !data.available;
  achTab(ach.tab);
  if (!data.available) return;
  ach.epgSources = data.sources.slice();
  $("achEpgHours").textContent = data.refresh_hours;
  $("achEpgMax").textContent = data.max;
  achRenderEpg();
}
function achEpgDirty() { return !!ach.epg && ach.epgSources.join("\n") !== ach.epg.sources.join("\n"); }
function achRenderEpg() {
  const e = ach.epg;
  const box = $("achEpgPresets");
  box.replaceChildren();
  for (const p of e.presets) {
    const url = p.urls[0].url, on = ach.epgSources.includes(url);
    const item = el("div", "preset" + (on ? " on" : ""));
    const about = el("p", "", p.description + " 维护者：");
    const link = el("a", "", p.maintainer);
    link.href = p.homepage; link.target = "_blank"; link.rel = "noopener noreferrer";
    about.appendChild(link);
    const label = el("label", "chk");
    const check = el("input");
    check.type = "checkbox"; check.checked = on;
    check.onchange = () => {
      if (check.checked && !ach.epgSources.includes(url)) {
        if (ach.epgSources.length >= e.max) { toast("最多 " + e.max + " 个来源", true); check.checked = false; return; }
        ach.epgSources.push(url);
      }
      if (!check.checked) ach.epgSources = ach.epgSources.filter((u) => u !== url);
      achRenderEpg();
    };
    label.append(check, document.createTextNode("用这个节目单"));
    label.title = url;
    item.append(el("b", "", p.name), about, label);
    box.appendChild(item);
  }
  const known = new Set(e.presets.map((p) => p.urls[0].url));
  const status = Object.fromEntries((e.status || []).map((s) => [s.url, s]));
  const list = $("achEpgStatus");
  list.replaceChildren();
  ach.epgSources.forEach((url, i) => {
    const s = status[url];
    const name = (s && s.name) || (known.has(url) ? e.presets.find((p) => p.urls[0].url === url).name : url);
    const li = el("li");
    let text = (i + 1) + ". " + name + "：";
    if (!e.sources.includes(url)) text += "保存后开始下载";
    else if (!s || (!s.checked_at && !s.parsed_at)) text += "等着下载…";
    else {
      text += s.ok === false ? "这次没取到（" + s.error + "）" + (s.fetched_at ? "，用 " + when(s.fetched_at) + " 下载的" : "")
        : "上次检查 " + when(s.checked_at) + (s.fetched_at ? "，文件是 " + when(s.fetched_at) + " 的（" + admKb(s.bytes) + " KB）" : "");
      if (s.parsed_at) text += "；节目单里 " + s.channels + " 个台，对上频道表 " + s.matched + " 个" + (s.parse_error ? "（文件有错，只读了一部分）" : "");
    }
    li.appendChild(el("span", s && s.ok === false ? "bad" : "", text));
    if (!known.has(url)) li.appendChild(button("去掉", "linkbtn red", () => { ach.epgSources.splice(i, 1); achRenderEpg(); }));
    list.appendChild(li);
  });
  const c = e.coverage;
  if (e.sources.length && c && c.total) {
    list.appendChild(el("li", "", "频道表 " + c.total + " 个台里 " + c.matched + " 个有节目单" +
      (c.missing.length ? "；没对上的：" + c.missing.slice(0, 15).join("、") + (c.missing.length > 15 ? " 等" : "") : "") + "。"));
  }
  if (!ach.epgSources.length) list.appendChild(el("li", "", "现在关着：勾选一个节目单或填地址，再点「保存」。"));
  $("achEpgState").textContent = achEpgDirty() ? "有没保存的修改" : "";
  $("achEpgSave").disabled = !achEpgDirty();
  $("achEpgRefresh").hidden = !e.sources.length;
}
function achWireEpg() {
  $("achEpgAdd").onclick = () => {
    const url = $("achEpgUrl").value.trim();
    if (!ach.epg) return;
    if (!/^https?:\/\/\S+$/.test(url)) { toast("地址要以 http:// 或 https:// 开头", true); return; }
    if (ach.epgSources.includes(url)) { toast("已经有这个地址了", true); return; }
    if (ach.epgSources.length >= ach.epg.max) { toast("最多 " + ach.epg.max + " 个来源", true); return; }
    ach.epgSources.push(url);
    $("achEpgUrl").value = "";
    achRenderEpg();
  };
  $("achEpgUrl").onkeydown = (event) => { if (event.key === "Enter") $("achEpgAdd").click(); };
  $("achEpgSave").onclick = () => admBusy($("achEpgSave"), async () => {
    const answer = await admAct("api/admin/epg", { sources: ach.epgSources }, ach.epgSources.length ? "已保存：服务器在后台下载节目单，过一会儿刷新看结果" : "已关掉节目单");
    if (answer) { await achLoadEpg(); setTimeout(achLoadEpg, 8000); }
  });
  $("achEpgRefresh").onclick = () => admBusy($("achEpgRefresh"), async () => {
    if (await admAct("api/admin/epg/refresh", {}, "正在更新：过一会儿刷新看结果")) setTimeout(achLoadEpg, 8000);
  });
}

// ---------- 导入播放列表（自建模式） ----------
// 服务端读 M3U / TXT（地址、文件，或 data.presets 里的公开列表）、列出台，每个台带自动分组 kind
// （channel_groups.classify）；这里筛选、勾选、试播，再加进上面那张表，和手加的台一样要点「保存」。

// 和 server/relay/playlist.name_key 一样：不分大小写，不管空格、横线、下划线。
function achNameKey(name) { return name.replace(/[\s\-_]+/g, "").toLowerCase(); }
// 和 playlist.mark 一样，但对照的是页面上这张表（包括还没保存的修改）。
function achImpMark() {
  const urls = new Set(ach.rows.flatMap((r) => achLines(r).map((x) => x.url))), names = new Set(ach.rows.map((r) => achNameKey(r.name)));
  const seen = new Set();
  for (const r of ach.imp) {
    const key = achNameKey(r.name);
    const known = [r.url].concat((r.routes || []).map((x) => x.url)).some((u) => urls.has(u));
    r.dup = known ? "url" : names.has(key) ? "name" : seen.has(key) ? "list" : "";
    seen.add(key);
  }
}
function achRenderPresets() {
  const box = $("achPresets");
  box.replaceChildren();
  for (const p of ach.data.presets || []) {
    const item = el("div", "preset");
    const about = el("p", "", p.description + " 维护者：");
    const link = el("a", "", p.maintainer);
    link.href = p.homepage; link.target = "_blank"; link.rel = "noopener noreferrer";
    about.appendChild(link);
    const actions = el("div", "inline");
    p.urls.forEach((u, i) => {
      const b = button(i ? u.label : "读取", "ghost");
      b.title = u.url;
      b.onclick = () => admBusy(b, () => achImpRead({ preset: p.id, mirror: i }, p.name + (i ? " · " + u.label : "")));
      actions.appendChild(b);
    });
    const subscribed = ((ach.subs && ach.subs.subs) || []).some((s) => s.name === p.name);
    const sub = button(subscribed ? "已订阅" : "订阅", "ghost");
    sub.disabled = subscribed || !ach.subs;
    sub.title = "记住这个列表，定时自动更新线路";
    sub.onclick = () => admBusy(sub, () => achSubAdd({ preset: p.id, mirror: 0 }));
    actions.appendChild(sub);
    item.append(el("b", "", p.name), about, actions);
    box.appendChild(item);
  }
  $("achPresetBox").hidden = !box.children.length;
}
// ---- 订阅的直播源（server/relay/subscriptions.py）：定时重新读，新线路并进已有的台 ----
async function achSubsLoad() {
  let answer = null;
  try { answer = await admCall("GET", "api/admin/subs"); } catch (error) { answer = null; }
  ach.subs = answer && answer.ok ? answer : null;
  achRenderSubs();
  achRenderPresets();
}
function achSubSummary(r) {
  if (!r) return "还没读过";
  const parts = ["对上 " + r.channels + " 个台"];
  if (r.added) parts.push("补了 " + r.added + " 条线路");
  if (r.replaced) parts.push("换掉 " + r.replaced + " 条打不开的");
  if (!r.added && !r.replaced) parts.push("没有新线路");
  if (r.new) parts.push(r.new + " 个新台");
  return parts.join("，");
}
function achRenderSubs() {
  const box = $("achSubList");
  if (!box) return;
  const subs = (ach.subs && ach.subs.subs) || [];
  $("achSubs").hidden = !subs.length;
  box.replaceChildren();
  for (const s of subs) {
    const row = el("div", "subrow");
    const text = el("div", "subtext");
    text.append(el("b", "", s.name));
    const status = s.busy ? "正在更新…" : s.ok === false ? "上次没读到（" + (s.error || "取不到") + "）· " + ago(s.last_at)
      : s.last_at ? achSubSummary(s.result) + " · " + ago(s.last_at) : "还没读过";
    text.append(el("span", s.ok === false ? "hint bad" : "hint", status));
    const actions = el("div", "inline");
    if (s.result && s.result.new) {
      const look = button("看新台", "ghost");
      look.title = (s.result.new_names || []).slice(0, 12).join("、");
      look.onclick = () => admBusy(look, () => achImpRead({ url: s.url }, s.name, true));
      actions.append(look);
    }
    const now = button("现在更新", "ghost");
    now.onclick = () => admBusy(now, async () => {
      const answer = await admCall("POST", "api/admin/subs/refresh", { id: s.id });
      if (!answer.ok) toast(answer.error || "没更新", true);
      else { toast(s.name + "：" + achSubSummary(answer.result)); achLoad(true); }
      await achSubsLoad();
    });
    const drop = button("取消订阅", "ghost");
    drop.onclick = () => {
      if (drop.dataset.sure !== "1") { drop.dataset.sure = "1"; drop.textContent = "确定取消？"; setTimeout(() => { drop.dataset.sure = ""; drop.textContent = "取消订阅"; }, 4000); return; }
      admBusy(drop, async () => {
        const answer = await admCall("POST", "api/admin/subs/delete", { id: s.id });
        if (!answer.ok) toast(answer.error || "没取消", true); else toast("已取消订阅：已经加进来的线路照旧");
        await achSubsLoad();
      });
    };
    actions.append(now, drop);
    row.append(text, actions);
    box.appendChild(row);
  }
}
async function achSubAdd(body) {
  const answer = await admCall("POST", "api/admin/subs", body);
  if (!answer.ok) { toast(answer.error || "没订阅上", true); await achSubsLoad(); return false; }
  toast("已订阅：" + achSubSummary(answer.result), false, 6000);
  await achSubsLoad();
  achLoad(true);
  return true;
}
// onlyNew：订阅的「看新台」只列频道表里还没有的台（已有的台，新线路订阅时已经并进去了）。
async function achImpRead(body, source, onlyNew) {
  const answer = await admCall("POST", "api/admin/channels/import", body);
  if (!answer.ok) { toast(answer.error || "没读出来", true); return; }
  ach.imp = answer.entries.map((e) => ({ ...e, on: false, state: "" }));
  achImpMark();
  let total = answer.total, listed = answer.entries.length;
  if (onlyNew) {
    ach.imp = ach.imp.filter((r) => r.dup !== "url" && r.dup !== "name");
    source += "：表里没有的";
    total = listed = ach.imp.length;
  }
  ach.impKind = "";
  ach.impFilter = $("achImpFilter").value = "";
  ach.impSource = source + "：" + total + " 个台" +
    (total > listed ? "（列出前 " + listed + " 个）" : "") +
    (answer.skipped ? "，" + answer.skipped + " 个地址不合格" : "");
  $("achImpSource").textContent = ach.impSource;
  $("achImpList").hidden = false;
  achRenderImport();
  $("achImpList").scrollIntoView({ block: "start", behavior: "smooth" });
}
function achImpVisible() {
  const filter = ach.impFilter.trim().toLowerCase();
  return ach.imp.filter((r) => (!ach.impKind || r.kind === ach.impKind) && (!filter || r.name.toLowerCase().includes(filter)));
}
function achRenderImport() {
  const box = $("achImpTable");
  if (!box) return;
  for (const b of $("achImpKinds").children) b.setAttribute("aria-pressed", String(b.dataset.kind === ach.impKind));
  const visible = achImpVisible();
  const rows = visible.slice(0, 500).map((r) => {
    const tr = el("tr");
    const plan = achImpPlan(r);
    const check = el("input");
    check.type = "checkbox"; check.checked = r.on; check.setAttribute("aria-label", "选「" + r.name + "」");
    check.disabled = !r.done && !plan.add && !plan.fresh.length;
    check.onchange = () => { r.on = check.checked; achImpCount(); };
    const st = r.state;
    const dup = r.done || (plan.add ? (r.dup === "list" ? ACH_DUP.list : "")
      : plan.fresh.length ? "并入「" + plan.target.name + "」当备用" : "已在表里");
    const cells = [admTd(""), admTd("", r.name), admTd("kind", r.kind), admTd("st", r.url), admTd("kind", (1 + (r.routes || []).length) + " 条"),
                   admTd("dup", dup),
                   admTd("st " + (st === "能放" || st.startsWith("能放，") ? "ok" : st === "放不了" ? "dead" : ""), st)];
    cells[0].appendChild(check);
    cells[1].style.whiteSpace = "nowrap";
    cells[3].title = [r.url].concat((r.routes || []).map((x) => x.url)).join("\n");
    tr.append(...cells);
    return tr;
  });
  if (!rows.length) { const tr = el("tr"); const td = admTd("empty-t", ach.impKind ? "这个列表里没有「" + ach.impKind + "」的台" : "没有名字里带这几个字的台"); td.colSpan = 7; tr.appendChild(td); rows.push(tr); }
  const table = admTable("", "", ["", "名字", "分组", "地址", "线路", "怎么加", "能不能放"], rows);
  box.replaceChildren(...table.childNodes);
  achImpCount();
}
function achImpChosen() { return ach.imp.filter((r) => r.on && !r.done && r.state !== "放不了" && achImpUseful(r)); }
function achImpCount() {
  if (!$("achImpCount")) return;
  const d = ach.data;
  const chosen = achImpChosen();
  const adds = chosen.filter((r) => achImpPlan(r).add).length, merges = chosen.length - adds;
  const total = ach.rows.length + adds;
  $("achImpAdd").textContent = chosen.length ? "加入 " + chosen.length + " 个" : "加入";
  $("achImpAdd").disabled = !chosen.length;
  $("achImpCount").textContent = (chosen.length
    ? [adds ? adds + " 个新台" : "", merges ? merges + " 个并入已有的台当备用" : ""].filter(Boolean).join("，") + "；加上后 " + total + " 个台"
      + (total > d.limits.channels ? "，小电视只收前 " + d.limits.channels + " 个" : "")
    : "勾选要的台") + (achImpVisible().length > 500 ? " · 只显示前 500 个，用「找台」缩小" : "");
}
function achWireImport() {
  if (!ach.data.import) return;
  for (const b of $("achImpKinds").children) b.onclick = () => { ach.impKind = b.dataset.kind; achRenderImport(); };
  $("achImpFetch").onclick = () => admBusy($("achImpFetch"), async () => {
    const url = $("achImpUrl").value.trim();
    if (!/^https?:\/\//.test(url)) { toast("地址要以 http:// 或 https:// 开头", true); return; }
    await achImpRead({ url }, url);
  });
  $("achImpUrl").onkeydown = (event) => { if (event.key === "Enter") $("achImpFetch").click(); };
  $("achImpSub").onclick = () => admBusy($("achImpSub"), async () => {
    const url = $("achImpUrl").value.trim();
    if (!/^https?:\/\//.test(url)) { toast("地址要以 http:// 或 https:// 开头", true); return; }
    if (await achSubAdd({ url })) $("achImpUrl").value = "";
  });
  achSubsLoad();
  $("achImpPick").onkeydown = (event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); $("achImpFile").click(); } };
  $("achImpFile").onchange = async () => {
    const file = $("achImpFile").files[0];
    $("achImpFile").value = "";
    if (!file) return;
    if (file.size > 2 * 1024 * 1024) { toast("文件太大（超过 2 MB）", true); return; }
    try { await achImpRead({ text: await file.text() }, "文件 " + file.name); } catch (error) { admFail(error); }
  };
  $("achImpFilter").oninput = () => { ach.impFilter = $("achImpFilter").value; achRenderImport(); };
  // 全选：表里已经有的台也选上（新地址并进去当备用线路）；加过的、没什么可加的不选。
  $("achImpAll").onclick = () => { achImpVisible().forEach((r) => { r.on = !r.done && achImpUseful(r); }); achRenderImport(); };
  $("achImpNone").onclick = () => { achImpVisible().forEach((r) => { r.on = false; }); achRenderImport(); };
  $("achImpProbe").onclick = achProbe;
  $("achImpAdd").onclick = () => admBusy($("achImpAdd"), achImpAdd);
}
async function achProbe() {
  const btn = $("achImpProbe");
  if (ach.probing) { ach.probing = false; return; }
  const chosen = ach.imp.filter((r) => r.on);
  if (!chosen.length) { toast("先勾选要试的台", true); return; }
  // 试点的时候选中的；服务端同时最多开两个 ffmpeg，每个取一帧，慢的源要十几秒。再点一下停。
  ach.probing = true;
  btn.textContent = "停止试";
  let next = 0;
  const worker = async () => {
    while (ach.probing && next < chosen.length && adm.sec === "channels") {
      const r = chosen[next++];
      r.state = "正在试…"; achRenderImport();
      try {
        const answer = await admCall("POST", "api/admin/channels/probe", { url: r.url, agent: r.agent });
        r.state = !answer.ok ? (answer.error || "没试成") : answer.state === "ok" ? "能放" : answer.state === "slow" ? "能放，但慢（" + answer.seconds + " 秒）" : "放不了";
      } catch (error) { r.state = "没试成"; }
      achRenderImport();
    }
  };
  await Promise.all([worker(), worker()]);
  ach.probing = false;
  if ($("achImpProbe") === btn) btn.textContent = "试一下选中的";
}
async function achImpAdd() {
  achImpMark();
  const chosen = achImpChosen();
  if (!chosen.length) { toast("先勾选要的台", true); return; }
  let added = 0, lines = 0, full = 0;
  const touched = new Set();
  // 按列表的顺序一个个来：同一个台在列表里出现几次时，第一次加成新台，后面的并进它。表最多 limits.table 个台。
  const tableMax = ach.data.limits.table || 1000;
  let over = 0;
  for (const r of chosen) {
    const plan = achImpPlan(r);
    if (plan.add && ach.rows.length >= tableMax) { over++; continue; }
    r.on = false;
    if (plan.add) {
      ach.rows.push({ name: r.name, url: r.url, agent: r.agent, group: "", tvg_id: r.tvg_id || "", tvg_name: r.tvg_name || "", logo: r.logo || "",
                      routes: (r.routes || []).map((x) => ({ ...x })), checks: {}, good: "" });
      r.done = "已加入";
      added++;
      continue;
    }
    const all = achLines(plan.target);
    for (const x of plan.fresh) {
      if (all.length >= achRoutesMax()) { full++; break; }
      all.push(x);
      lines++;
      touched.add(plan.target);
    }
    achSetLines(plan.target, all);
    r.done = "已并入「" + plan.target.name + "」";
  }
  achImpMark();
  ach.filter = $("achFilter").value = "";
  achRender();
  achRenderImport();
  if (!added && !lines) { toast(over ? "表满了：最多 " + tableMax + " 个台" : "这些台的线路已经满了（每台最多 " + achRoutesMax() + " 条）", true); return; }
  await achSaveNow([added ? "加了 " + added + " 个台" : "", lines ? "给 " + touched.size + " 个台加了 " + lines + " 条备用线路" : ""]
    .filter(Boolean).join("，") + (full ? "（有的台线路满了）" : "") + (over ? "（表最多 " + tableMax + " 个台，" + over + " 个没加）" : ""));
}
