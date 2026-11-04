<p align="right">
  <a href="2026-11-04-minimal-product-design.zh_CN.md">简体中文</a> · <strong>English</strong>
</p>

# Minimal products: the lite edition and a self-hosted Muse station — design

Status: draft, 2026-11-04. The owner confirmed sections 3, 4 and 5 one by one (2026-10-31); section 6 was presented but not yet confirmed; the parts of sections 7 and 8 marked **to confirm** are proposed here for the first time. Every number in section 13 was measured; nothing there is an estimate.

## 1. Background and goal

The project grew from "an ESP32 mini TV" into a home media centre (RooTV: library, cloud drives, YouTube, voice, phone listening, several households...). The owner wants to narrow it: first ship a **minimal product** to strangers —

- Device: Xingzhi Cube 1.54 (ESP32-S3, 8 MB octal PSRAM).
- Content: **CCTV and provincial satellite live channels** and **Bilibili** only.
- Pricing: one-off purchase, no recurring service fee.
- The owner runs no video server; neither bandwidth nor retransmission sits with the owner.

This document defines one shared "lite core" and two deployments — **A: a computer in the user's home**, **B: the user's own Muse cloud computer pushing to the user's own Qiniu**. The device firmware supports both.

## 2. Settled, not reopened

| Question | Decision | Basis |
| --- | --- | --- |
| Can the device play CCTV/Bilibili directly, with no server | No | Measured (section 13.1): both serve H.264 **High** profile; Espressif `esp_h264` decodes only Constrained Baseline and produced zero frames; even re-encoded to Baseline, 360p runs at about half real time |
| A cloud server run by the owner that relays | Not done | Bandwidth costs about 0.6 CNY per device-hour (current codec) or 0.1 CNY (H.264), an ongoing loss; and the owner's server retransmitting is the highest copyright risk |
| Use the Muse cloud computer as the server and let devices connect in | Not possible | Measured (section 13.2): all egress goes through a proxy, the platform exposes no port, and a free Cloudflare tunnel cannot be opened |
| Which hardware to sell | Only S3 boards with PSRAM | HLS playback decodes H.264 on the device; the C3 (Passport) has no PSRAM |
| Form of Bilibili content | Channels: videos in order, looping; the device only changes channel | The device has three keys; no on-demand in v1 |
| Built-in CCTV channel table | Kept (owner's decision) | See the risks in section 10 |

## 3. Lite core (confirmed)

One code base with an `EDITION` setting chosen at packaging time: `full` is today's RooTV, `lite` is the lite edition (working name "Xiaozhi TV"). It is fixed in the package; users cannot change it.

With `EDITION=lite`:

- The supervisor (`build_children` in `server/selfhost/__main__.py`) starts no YouTube node, no cloud-drive component, no phone listening and no Passport media server.
- The relay (`_serve_tenants` in `server/relay/__main__.py`) skips library, cloud-drive and YouTube setup.
- Disabled endpoints return 404 in `server/relay/web.py`; the console reads the edition from `/api/state` and shows only the lite pages.
- All modules stay in the package (YouTube, library and Bilibili code import each other; removing them is fragile).

| Lite keeps | Lite turns off |
| --- | --- |
| Devices: pairing, Wi-Fi setup, web flashing | Library, cloud drives (OpenList) |
| Live: built-in channel table, backup lines, line checks, logos | YouTube |
| Bilibili: QR login, channels from followings and favourites | Voice, phone listening |
| Basic settings; one admin account, signed in automatically | Multi-user: users, audit, invites, guests |
| | EPG, series, casting, pets, watching on the computer |

Tenancy stays: pairing and Bilibili login depend on it, and a single household is already the normal desktop case.

## 4. Built-in channel table and updates (confirmed)

- **Owner's maintenance tool** (runs daily): reads a few public m3u lists and maps lines to fixed channel IDs by name; grabs one frame from each line and records format and liveness; **new lines need the owner to approve a screenshot on a web page** (lists contain mislabelled channels and advert loops); approved lines are only checked for liveness; writes `channels.json`, signs it with the owner's private key and publishes it on the project's static site.
- **On the user's side**: the installer carries a copy; the app fetches at start-up and every 12 hours, verifies with a built-in public key and ignores anything that fails; channel IDs are fixed so device channel numbers do not move; for each channel the app records per-line success locally and prefers the line that works best on that home network (existing `linecheck`/`linequality`).
- Users can hide and reorder channels, not edit lines.
- v1 scope: CCTV1–17, provincial satellite channels and a few children's channels, about 60 in all, listed first on the device.
- The full edition keeps its "no content" rule; only the lite edition ships a table.

## 5. Bilibili channels (confirmed)

- QR login on the computer (existing `bililogin`); credentials stay on that machine and are renewed before they expire (existing, **not yet tested with a real account**).
- After login two lists are offered for ticking: followed creators (sorted by latest upload, the first 12 ticked by default, i.e. `MAX_CREATORS`) and favourite folders (**new**, up to 8 by default). Each becomes a looping channel; each device remembers its place in each channel.
- On the device, OK opens the existing menu, which gains a "next video" item (small firmware change).
- 360p. Deleted, members-only and region-locked videos are skipped and logged; when the login lapses the device asks the user to scan the QR code again.
- Bilibili API calls are rate-limited.
- **To measure**: the device CONFIG packet is capped at 7168 bytes (`channel_groups.CONFIG_MAX`). About 60 live channels plus up to 20 Bilibili channels should fit, but name lengths vary; if not, shorten the names shown on the device.

## 6. Deployment A: a computer in the user's home (presented, to confirm)

- **Install**: Windows installer (existing CI, Inno Setup) and a macOS dmg under the lite name. Beta builds are unsigned and come with a guide to allow them; **before selling, buy an Apple Developer ID and a Windows code-signing certificate**.
- **First launch**: the admin is created and signed in automatically (existing), then a three-step guide: connect a device → look at the channels (already there) → Bilibili QR login (skippable). The Windows installer adds a firewall rule.
- **Device out of the box**: the owner flashes it before shipping; the buyer joins its hotspot for Wi-Fi setup, where the computer address is prefilled by the app (existing); the device shows a 6-digit pairing code that is entered on the computer (existing).
- **New: find the computer on the LAN**. The computer's IP changes with the router. After 15 s without the server the device broadcasts a UDP "where is the Xiaozhi TV server" message; the app replies with its address and port; the device stores it and reconnects. UDP broadcast rather than mDNS, because mDNS behind the Windows firewall is unreliable (marked "unverified" in the PC client design).
- **Computer off or asleep**: the device says the computer is off or asleep; the app prevents sleep while a device is watching (`desktop/power.py`, to be confirmed on Windows).
- In this deployment the device uses the **existing TCP frame-push protocol**; the firmware is unchanged.

## 7. Deployment B: Muse cloud computer plus the user's own Qiniu

### 7.1 Path

```
Source (CCTV line / Bilibili)
   │  pull
   ▼
User's Muse cloud computer: lite core (headless Linux)
   │  ffmpeg encodes small HLS and uploads it over HTTPS (Muse has only an egress proxy, which is enough)
   ▼
User's own Qiniu Kodo bucket ──► Qiniu CDN (the user's own ICP-filed domain)
   │  the device fetches segments by number (HTTP)
   ▼
Xingzhi board: HLS pull mode (section 8.2)
```

The owner provides only software and guides; the Muse account, the Qiniu account, real-name verification, the filed domain and any cost beyond the free quota belong to the user.

### 7.2 Pusher

- The lite core gains a **headless Linux mode**: no tray, no local web page, configuration in a file, plus a few commands (section 7.5). Muse runs Python 3.12 and its stock ffmpeg works.
- Output format (measured to work): H.264 Constrained Baseline 240×136, 20 fps, about 250 kbit/s; AAC-LC mono 24 kHz 32 kbit/s; 6-second TS segments, each starting on a keyframe. About 309 kbit/s in total, about 130 MB per hour.
- Upload: Qiniu form upload, **through the accelerated upload host** (`upload-z0.qiniup.com` for East China; the ordinary host manages only 26 KB/s from Muse), 3 uploads in parallel, retries, segments set to expire after 1 day.
- When the source drops the pusher reconnects and keeps numbering (implemented in the out-of-repository test script `muse-probe/qiniu_push.py` v3). **To change**: cut ffmpeg's read timeout from 15 s to about 5 s and add backup lines, switching to the next one when a line stalls — in the tests every stall came from a source that stalled and was only reopened after the full 15 s (section 13.3).
- HTTPS sources, such as Bilibili media URLs, need `-http_proxy` (Muse's egress proxy) on ffmpeg.

### 7.3 Measured Qiniu constraints

- **Overwritten files are served 10–20 s stale**, even with `.m3u8` caching set to 0 and `?t=` on the request. So the device **does not depend on the playlist**.
- **The CDN caches 404s**: asking for a segment before it is uploaded keeps returning 404 after it arrives. A request with `?t=<ms>` gets past it.
- Free quota (monthly, after real-name verification): 10 GB of HTTP download and 100,000 write requests. At 130 MB per hour **10 GB lasts about 75 hours a month** (about 2.5 hours a day); with the design in 7.4 writes run at about 600 per hour, enough for about 160 hours. Beyond that the user pays (about 0.24 CNY/GB for HTTP in mainland China).
- Pulling from Qiniu inside China is fast: a 6-second segment (about 230 KB) downloads in a median 0.22 s.

### 7.4 Channel-change control path (**to confirm**, new)

The device cannot connect to Muse, and Muse cannot push every channel around the clock (0.19 core per stream, only about 548 MB of memory available, metered traffic and writes). So **only the channel being watched is pushed**, and the device must be able to tell Muse when it changes channel. The only place both can reach is Qiniu:

- **The device writes a control file**: `<prefix>/ctl/<device-id>.json` containing `{"ch": channel id, "session": random string, "seq": n, "t": time}`. It uses a Qiniu upload token that **can write only that one file** (scope `bucket:<prefix>/ctl/<device-id>.json`, valid for a year, renewed by Muse). A leaked token can change only that file.
- **Muse reads the latest version every 2 s** through Qiniu's management API rather than the CDN (the exact call is verified during implementation).
- **Each channel change uses a new path**: Muse pushes the new channel from `<prefix>/live/<session>/seg_000000.ts`; the device asks for `seg_000000.ts` with `?t=` until it arrives. A new path has no stale playlist and no cached 404s, and no playlist needs uploading at all (half the writes).
- **Start-up buffer**: when ffmpeg opens a live HLS source it starts a few segments behind the source's newest and reads at full speed, so the first segments of a new channel come out quickly and the device can build a buffer soon after changing channel. **To measure**: time from channel change to picture; target under 10 s.
- **Stop when nobody watches**: while playing the device writes a heartbeat every 5 minutes; Muse stops pushing after 10 minutes without one, saving traffic and writes.

### 7.5 How the user sets it up (**to confirm**)

Muse itself is the interface:

1. Following the guide, the user signs up to Qiniu, completes real-name verification, creates a public bucket (East China), binds their own filed domain, sets `.m3u8` caching to 0 and creates a sub-account key limited to that bucket.
2. The user sends the lite core release to Muse with "deploy it as described" and gives Muse the five Qiniu variables (the key goes only to Muse).
3. Muse runs a `bili-login` command, posts the QR image in the chat, and the user scans it with the Bilibili app. Channel choices are also told to Muse in the chat, and Muse edits the configuration file.
4. Muse runs a `device-code` command that produces a **device configuration code** (CDN address, prefix, device ID, control-file token) and its QR code. The user pastes the code into a "cloud station" field on the device's setup page.
5. Muse installs the supervision of section 7.6; done.

### 7.6 Supervision and recovery

The Muse machine restarts without notice (seen once; the pusher was killed outright). So:

- The pusher runs under a supervisor that restarts it within 5 s, plus a periodic watchdog that restores the supervisor itself after a host restart (Muse built and verified this).
- After recovery the pusher **keeps the same prefix and session** and continues numbering, so the device sees only a short rebuffer.
- Supervision limits the damage but cannot remove the restart risk; the guide says so.

## 8. Device firmware: two modes

### 8.1 TCP frame-push mode (deployment A, existing)

Unchanged.

### 8.2 HLS pull mode (deployment B, prototype verified)

The prototype lives outside the main repository in `hls-proto/` (it reuses this repository's `components/bsp`). What moves into the firmware:

- **Fetching**: segments `seg_%06u.ts` by number; on a first 404, retry every second with `?t=<ms>`; after 40 s without the segment, write the control file with a new session so the pusher moves to a new path, and start again from segment 0 there (there is no playlist to reread, so this is the one clean resync).
- **Demux and decode**: a small TS demuxer (PAT→PMT→H.264 0x1B, AAC 0x0F), `espressif/esp_h264` software decoding, `espressif/esp_audio_codec` for AAC; timestamp jumps from source reconnects are folded into one continuous timeline.
- **Playback**: audio is the master clock; a frame is drawn when its time comes and dropped if more than 150 ms late; 240×136 centred on the 240×240 panel.
- **Buffering**: starting 4 segments behind (about 24 s) did not ride out the source's 20–35 s gaps. Make it adaptive: start with 1–2 segments, add one segment of target buffer after each stall, up to about 8 segments (48 s).
- **Memory**: PSRAM must be on. Today's TV firmware keeps PSRAM off on purpose (so large mallocs do not land in PSRAM and slow SPI DMA), so use `CONFIG_SPIRAM_USE_CAPS_ALLOC`: only allocations that ask for PSRAM go there, and TCP mode behaves as before. The prototype uses about 3.3 MB of PSRAM and leaves about 40 KB of internal RAM free.
- **Wi-Fi**: keep the TV firmware's Wi-Fi management (11 dBm transmit power and the other board fixes).
- **Control**: write the control file on channel change (7.4) and a heartbeat every 5 minutes while playing.

### 8.3 Choosing the mode

The setup page gains a "cloud station" field: with a device configuration code the device runs in HLS mode, without one in TCP mode (connect to a computer). The code is stored in NVS; the status page shows the current mode.

## 9. Failure handling

| Case | What the device shows | Handling |
| --- | --- | --- |
| Source stalls or drops | Picture holds after the buffer runs out, with a buffering notice | Pusher reconnects after a 5 s timeout and switches lines; the device resumes as soon as segments arrive |
| Muse machine restarts | Same, for longer | Supervisor restarts the pusher on the same session |
| Qiniu 404 / stale list | Nothing | Fetch by number plus `?t=` retries |
| Device configuration code expired (control token after one year) | Channel changes do nothing; asks the user to have Muse renew the code | Muse issues a new one |
| Free quota used up | Qiniu starts charging (playback continues) | Explained in the guide; Muse reports usage weekly |
| Deployment A: computer asleep / IP changed | "Computer off or asleep" | Prevent sleep; UDP broadcast discovery |

## 10. Risks and decisions

- **Copyright and broadcast regulation** (not legal advice; consult a lawyer before selling): a built-in CCTV table maintained by the owner amounts to ongoing provision of sources and carries contributory-infringement risk; a box that plays live TV is also subject to broadcast regulation of internet TV. In deployments A and B pulling and distribution happen on the user's own computer or accounts, which is far lower risk than the owner running a server. The owner understands this and decided to keep the built-in table.
- **Qiniu content review**: a user's Qiniu account distributing live channels may be suspended. The risk sits with the user's account.
- **Muse**: blocked in mainland China, so most ordinary users there cannot use it — deployment B mainly serves overseas Chinese and hobbyists. Whether Meta's terms allow long-running streaming on a personal AI assistant's cloud computer is unclear; the machine restarts.
- **Source stability**: public lines die and stall often; the maintenance tool and backup lines are what keep this product alive.

## 11. Testing and acceptance

- Section 5 CONFIG packet size: 60 live channels plus 20 Bilibili channels with real names.
- Deployment A: one Windows 10/11 and one macOS machine, from install to picture; after the computer's IP changes the device finds it again; the sleep notice.
- Deployment B: one Muse, one Qiniu bucket, two devices watching different channels for 2 hours each; record stalls and their length, channel-change time, traffic and writes. Pass: no stalls while the source is healthy; picture within 10 s of a channel change.
- Firmware: HLS mode for 8 hours without internal RAM falling; TCP mode with `SPIRAM_USE_CAPS_ALLOC` at least as fast as today.

## 12. Phases (proposed; order **to confirm**)

1. **P1 lite core**: `EDITION`, built-in table and maintenance tool, Bilibili favourites, trimmed console. Sellable on the Mac first.
2. **P2 finish deployment A**: LAN discovery (firmware and app), Windows package tests, first-run guide, signing certificates.
3. **P3 firmware HLS mode**: move the prototype into the firmware, `SPIRAM_USE_CAPS_ALLOC`, configuration code, adaptive buffering.
4. **P4 pusher**: headless core mode, Qiniu upload, control path, `bili-login`/`device-code` commands, Muse deployment guide.
5. **P5 deployment B end-to-end acceptance** (section 11).

## 13. Measurements

### 13.1 What the S3 decodes (2026-10-31, Xingzhi board, `esp_h264` 1.5.0)

| Clip | Result (including scaling to 240×135) |
| --- | --- |
| Bilibili 360p as served (High) | 0 frames, `profile_idc is error` |
| CCTV1 720p as served (High) | 0 frames, `entropy_coding_mode_flag` |
| Bilibili 640×360 re-encoded to Baseline | 15.4 fps single task / 17.2 fps dual task (51–57% of real time) |
| CCTV1 720p re-encoded to Baseline | 3.5 / 4.2 fps |
| 320×180 Baseline | 31–41 fps, faster than real time on one core |

### 13.2 Muse cloud computer (2026-11-03)

4-core EPYC, 7.9 GB of memory but about 548 MB available, located in the US; all egress through an HTTP proxy at about 47 MB/s. From there the CCTV1 line pulls at 2.9 MB/s and a provincial broadcaster's own CDN at 1.9 MB/s; the Bilibili API and media both work. One transcode costs about 0.19 core. No inbound port; a free cloudflared tunnel is blocked by the proxy while it is being requested.

### 13.3 Muse → Qiniu → device (2026-11-03/04)

- Upload: accelerated host about 233 KB/s, 1.4–1.7 s per segment on average, 10 s at worst; ordinary host 26 KB/s.
- 30-minute push: 295 segments, 65 MB, about 309 kbit/s, 0 failures; the source dropped and was reopened twice.
- Simulated device in China (Hangzhou, 3 segments behind, 18 s buffer): 27 minutes, all 269 segments fetched, 2 stalls totalling 17 s.
- **Real device** (Xingzhi board, LAN test source): 5 minutes at a steady 20 fps, no audio gaps; core 0 about 12%, core 1 about 60% (about 15 ms decode plus about 13 ms colour conversion and display per frame); about 40 KB of internal RAM free.
- **Real device pulling from the Qiniu CDN** (4 segments behind): about 27 minutes, 31,285 frames, 0 decode errors, 0 network errors, 0 resyncs; 6 stalls totalling about 51 s. Matched against the pusher's timeline, **every one was an upstream gap**: the source stalling for 22–35 s, about 15 s of timeout before each of the two reopens, and the end of the push.

## 14. Open questions

- Confirmation of section 6 as a whole (deployment A install and LAN discovery).
- The control path and "Muse as the interface" set-up in sections 7.4 and 7.5.
- Which deployment comes first (section 12).
- Whether a channel change can show a picture within 10 s; if not, use 2-second segments for the first few (more writes).
- Bilibili from Muse (a US IP): some videos may be region-locked; test with a real following list.
