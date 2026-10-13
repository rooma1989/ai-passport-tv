// 扫码登录电脑：手机遥控（remote.html）和电脑登录页（login.html）点「扫码登录电脑」时才加载。
// 用后置相机连续认二维码，认出电脑屏幕上的 HTTPS://<网站>/TV/L/<票据> 就关相机、把票据交给页面去确认。
// 认码：浏览器有 BarcodeDetector 就用它（安卓 Chrome）；没有（iPhone Safari）再加载 /tv/jsqr.js。
// 相机只在点按钮后打开；关闭、认出、切到后台（visibilitychange）都会立刻释放。
// tests/test_relay_console.py 在 node 里跑 ticketFrom()，并用 jsqr.js 认中转自己画的登录二维码。
(function (root) {
  "use strict";

  // 网站可以是主站也可以是 tv. 子域名（票据全中转通用），所以不看主机名，只认路径。
  // 二维码里是大写的 /TV/L/，手机浏览器地址栏里是小写的 /tv/l/；票据是 26 位 base32。
  const LINK = /^https?:\/\/[^\/\s?#]+\/(?:tv\/)?l\/([a-z2-7]{26})\/?$/i;

  function ticketFrom(text) {
    const match = LINK.exec(String(text == null ? "" : text).trim());
    return match ? match[1].toUpperCase() : "";
  }

  // 为什么开不了相机：给人看的一句话。
  function reason(error, env) {
    const wechat = /MicroMessenger/i.test(env.agent || "");
    if (wechat) return "微信里打不开相机。请从手机桌面上的「小智电视」图标打开遥控再扫，或者输入登录码。";
    if (!env.secure) return "这个网址不是 https，浏览器不让网页用相机。请输入电脑屏幕上的登录码。";
    if (!env.media) return "这个浏览器不能在网页里用相机。请输入电脑屏幕上的登录码。";
    const name = error && error.name;
    if (name === "NotAllowedError" || name === "SecurityError")
      return "没有相机权限。请在手机「设置」里允许 Safari（或这个浏览器）使用相机，再点一次；也可以输入登录码。";
    if (name === "NotFoundError" || name === "OverconstrainedError") return "这台手机上找不到可用的相机。请输入电脑屏幕上的登录码。";
    if (name === "NotReadableError" || name === "AbortError") return "相机正被别的应用占用。关掉它再试，或者输入登录码。";
    return "相机打不开。请输入电脑屏幕上的登录码。";
  }

  // 每条都以 .scan 开头并写全按钮的样式：login.html 有全局的 button / p 样式，不能漏进来。
  const CSS = `
  .scan { position: fixed; inset: 0; z-index: 100; display: flex; flex-direction: column; background: #000;
          color: var(--paper, #e6dcc8); font: 15px/1.5 -apple-system, "PingFang SC", sans-serif; }
  .scan-top { display: flex; align-items: center; justify-content: space-between; gap: 12px;
              padding: calc(12px + env(safe-area-inset-top)) 16px 12px; background: var(--ink, #1c1915);
              border-bottom: 1px solid var(--rule, #3a342b); }
  .scan-top b { font: 600 17px var(--serif, serif); letter-spacing: 3px; color: var(--paper-hi, #efe5d0); }
  .scan .scan-top button { display: block; flex: none; width: auto; min-width: 64px; margin: 0; background: none; border: 0;
                           padding: 4px 0; color: var(--dim, #8c8170); font: 15px var(--serif, serif); letter-spacing: 0;
                           text-align: left; border-radius: 0; }
  .scan .scan-top button.torch { text-align: right; }
  .scan .scan-top button.torch.on { color: var(--amber, #ffb547); }
  .scan .scan-top button[hidden] { visibility: hidden; }
  .scan-view { flex: 1; position: relative; overflow: hidden; }
  .scan-view video { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: cover; }
  .scan-frame { position: absolute; left: 50%; top: 46%; width: min(70vw, 300px); aspect-ratio: 1; transform: translate(-50%, -50%);
                box-shadow: 0 0 0 100vmax rgba(15, 13, 11, .55); }
  .scan-frame i { position: absolute; width: 26px; height: 26px; border: 0 solid var(--amber, #ffb547); }
  .scan-frame i:nth-child(1) { left: 0; top: 0; border-width: 3px 0 0 3px; }
  .scan-frame i:nth-child(2) { right: 0; top: 0; border-width: 3px 3px 0 0; }
  .scan-frame i:nth-child(3) { left: 0; bottom: 0; border-width: 0 0 3px 3px; }
  .scan-frame i:nth-child(4) { right: 0; bottom: 0; border-width: 0 3px 3px 0; }
  .scan.found .scan-frame i { border-color: var(--red, #d2553f); }
  .scan-foot { padding: 14px 20px calc(16px + env(safe-area-inset-bottom)); background: var(--ink, #1c1915);
               border-top: 1px solid var(--rule, #3a342b); text-align: center; }
  .scan .scan-hint { margin: 0; font: 16px/1.6 var(--serif, serif); color: var(--paper-hi, #efe5d0); }
  .scan .scan-note { min-height: 22px; margin: 4px 0 0; font: 14px/1.5 var(--serif, serif); color: var(--dim, #8c8170); }
  .scan .scan-note.warn { color: var(--amber, #ffb547); }
  .scan .scan-note.bad { color: var(--red, #d2553f); }
  .scan .scan-foot button { display: block; width: 100%; margin: 12px 0 0; padding: 11px; border: 1px solid var(--rule, #3a342b);
                      background: none; color: var(--dim, #8c8170); font: 16px var(--serif, serif); letter-spacing: 3px; border-radius: 0; }
  .scan .scan-foot button.go { border-color: var(--red, #d2553f); background: var(--red, #d2553f); color: #fff4e6; }
  .scan .scan-foot button[hidden] { display: none; }
  .scan-msg { position: absolute; inset: 0; display: flex; align-items: center; justify-content: center; padding: 32px;
              background: var(--ink-deep, #0f0d0b); font: 16px/1.7 var(--serif, serif); color: var(--paper, #e6dcc8); text-align: center; }
  .scan-msg[hidden] { display: none; }
  `;

  let ui = null;          // 打开着的扫码层
  let jsqrLoading = null;

  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function loadJsqr() {
    if (root.jsQR) return Promise.resolve(root.jsQR);
    if (!jsqrLoading) {
      jsqrLoading = new Promise((resolve, reject) => {
        const script = document.createElement("script");
        script.src = "/tv/jsqr.js?v=1";
        script.onload = () => (root.jsQR ? resolve(root.jsQR) : reject(new Error("jsQR")));
        script.onerror = () => { jsqrLoading = null; reject(new Error("jsQR")); };
        document.head.appendChild(script);
      });
    }
    return jsqrLoading;
  }

  // 认码器：返回 async (video) => 文本或 ""。
  async function makeDecoder() {
    const Detector = root.BarcodeDetector;
    if (Detector) {
      try {
        const formats = Detector.getSupportedFormats ? await Detector.getSupportedFormats() : ["qr_code"];
        if (formats.indexOf("qr_code") >= 0) {
          const detector = new Detector({ formats: ["qr_code"] });
          return async (video) => {
            const found = await detector.detect(video);
            for (const code of found) if (code.rawValue) return code.rawValue;
            return "";
          };
        }
      } catch (error) { /* 用 jsQR */ }
    }
    const jsQR = await loadJsqr();
    const canvas = document.createElement("canvas");
    const ctx = canvas.getContext("2d", { willReadFrequently: true });
    return async (video) => {
      const w = video.videoWidth, h = video.videoHeight;
      if (!w || !h) return "";
      // 只认画面中间的正方形（取景框那一块），缩到最多 640 像素：iPhone 上一帧几十毫秒。
      const side = Math.min(w, h);
      const size = Math.min(side, 640);
      if (canvas.width !== size) canvas.width = canvas.height = size;
      ctx.drawImage(video, (w - side) / 2, (h - side) / 2, side, side, 0, 0, size, size);
      const image = ctx.getImageData(0, 0, size, size);
      // 电脑版的二维码总是浅底深色，不用再试反色的（省一半时间）。
      const code = jsQR(image.data, size, size, { inversionAttempts: "dontInvert" });
      return code ? code.data : "";
    };
  }

  function stopCamera() {
    if (!ui) return;
    ui.run++;
    if (ui.stream) for (const track of ui.stream.getTracks()) track.stop();
    ui.stream = null;
    ui.track = null;
    ui.video.srcObject = null;
    ui.torch.hidden = true;
    ui.torch.classList.remove("on");
  }

  function note(text, cls) {
    ui.note.textContent = text || "";
    ui.note.className = "scan-note" + (cls ? " " + cls : "");
  }

  // 开不了相机：说原因，主按钮改成「输入登录码」。
  function fail(text) {
    stopCamera();
    ui.msg.textContent = text;
    ui.msg.hidden = false;
    ui.hint.textContent = "也可以输入电脑屏幕上的 6 位登录码";
    note("");
    ui.retry.hidden = true;
    ui.code.className = "go";
  }

  async function start() {
    const run = ++ui.run;
    ui.msg.hidden = true;
    ui.retry.hidden = true;
    ui.code.className = "";
    ui.box.classList.remove("found");
    ui.hint.textContent = "对准电脑屏幕上的二维码";
    note("正在打开相机…");
    const media = root.navigator && root.navigator.mediaDevices;
    const env = { agent: root.navigator ? root.navigator.userAgent : "", secure: root.isSecureContext !== false,
                  media: !!(media && media.getUserMedia) };
    if (!env.secure || !env.media) { fail(reason(null, env)); return; }
    const decoding = ui.decode ? Promise.resolve(ui.decode) : makeDecoder();   // 和相机权限的弹窗一起准备
    decoding.catch(() => {});
    let stream;
    try {
      stream = await media.getUserMedia({ audio: false,
        video: { facingMode: { ideal: "environment" }, width: { ideal: 1280 }, height: { ideal: 720 } } });
    } catch (error) {
      if (run === ui.run) fail(reason(error, env));
      return;
    }
    if (!ui || run !== ui.run) { for (const track of stream.getTracks()) track.stop(); return; }
    ui.stream = stream;
    ui.track = stream.getVideoTracks()[0] || null;
    const video = ui.video;
    video.srcObject = stream;
    try { await video.play(); } catch (error) { /* playsinline + muted 一般不会失败；失败了下面也认不到码 */ }
    const caps = ui.track && ui.track.getCapabilities ? ui.track.getCapabilities() : {};
    ui.torch.hidden = !caps.torch;
    let decode;
    try { ui.decode = decode = await decoding; } catch (error) {
      if (run === ui.run) fail("扫码组件没加载上，请检查网络后再试，或者输入登录码。");
      return;
    }
    if (run !== ui.run) return;
    note("");
    let hintUntil = 0;
    const tick = async () => {
      if (!ui || run !== ui.run) return;
      let text = "";
      try { text = await decode(video); } catch (error) { text = ""; }
      if (!ui || run !== ui.run) return;
      if (text) {
        const ticket = ticketFrom(text);
        if (ticket) {
          ui.box.classList.add("found");
          note("认出来了，正在打开确认页…", "warn");
          if (root.navigator.vibrate) root.navigator.vibrate(40);
          const done = ui.onTicket;
          close();
          done(ticket);
          return;
        }
        if (Date.now() > hintUntil) note("这不是电脑登录码", "warn");
        hintUntil = Date.now() + 2000;
      } else if (hintUntil && Date.now() > hintUntil) {
        hintUntil = 0;
        note("");
      }
      setTimeout(tick, 120);
    };
    tick();
  }

  // 切到后台就关相机；回来时等人点「继续扫码」（桌面图标打开的网页应用可能要再问一次权限）。
  function onVisibility() {
    if (!ui || document.visibilityState !== "hidden" || !ui.stream) return;
    stopCamera();
    ui.msg.textContent = "相机已经关了。";
    ui.msg.hidden = false;
    note("");
    ui.retry.hidden = false;
  }

  function close() {
    if (!ui) return;
    stopCamera();
    document.removeEventListener("visibilitychange", onVisibility);
    root.removeEventListener("pagehide", close);
    ui.box.remove();
    const done = ui.onClose;
    ui = null;
    if (done) done();
  }

  // options: onTicket(ticket) 必需；onCode() 点「输入登录码」；onClose() 关掉时（含认出后）。
  function open(options) {
    if (ui) close();
    if (!document.getElementById("scan-css")) {
      const style = el("style");
      style.id = "scan-css";
      style.textContent = CSS;
      document.head.appendChild(style);
    }
    const box = el("div", "scan");
    box.setAttribute("role", "dialog");
    box.setAttribute("aria-label", "扫码登录电脑");
    const top = el("div", "scan-top");
    const back = el("button", "", "‹ 关闭");
    const torch = el("button", "torch", "开灯");
    torch.hidden = true;
    top.append(back, el("b", "", "扫码登录电脑"), torch);
    const view = el("div", "scan-view");
    const video = el("video");
    video.muted = true;
    video.playsInline = true;
    video.autoplay = true;
    video.setAttribute("playsinline", "");
    video.setAttribute("muted", "");
    const frame = el("div", "scan-frame");
    frame.append(el("i"), el("i"), el("i"), el("i"));
    const msg = el("div", "scan-msg");
    msg.hidden = true;
    view.append(video, frame, msg);
    const foot = el("div", "scan-foot");
    const hint = el("p", "scan-hint", "对准电脑屏幕上的二维码");
    const noteLine = el("p", "scan-note");
    const retry = el("button", "go", "继续扫码");
    retry.hidden = true;
    const code = el("button", "", "输入登录码");
    foot.append(hint, noteLine, retry, code);
    box.append(top, view, foot);
    document.body.appendChild(box);
    ui = { box, video, torch, msg, hint, note: noteLine, retry, code, run: 0, stream: null, track: null, decode: null,
           onTicket: options.onTicket, onClose: options.onClose };
    back.onclick = close;
    retry.onclick = start;
    code.onclick = () => { const go = options.onCode; close(); if (go) go(); };
    torch.onclick = async () => {
      if (!ui || !ui.track) return;
      const on = !torch.classList.contains("on");
      try {
        await ui.track.applyConstraints({ advanced: [{ torch: on }] });
        torch.classList.toggle("on", on);
        torch.textContent = on ? "关灯" : "开灯";
      } catch (error) { torch.hidden = true; }
    };
    document.addEventListener("visibilitychange", onVisibility);
    root.addEventListener("pagehide", close);
    start();
  }

  root.TVScan = { ticketFrom, reason, open, close };
})(typeof window !== "undefined" ? window : this);
