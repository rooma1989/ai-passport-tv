<p align="right">
  <a href="2026-10-12-multi-tenant-design.zh_CN.md">简体中文</a> · <strong>English</strong>
</p>

# Multi-tenant cloud service — design

Status: draft for decision, 2026-10-12. Research and design only; nothing here
is implemented. Follows
[`2026-10-09-creator-channels-and-cast-design.md`](2026-10-09-creator-channels-and-cast-design.md),
[`2026-10-10-grouped-channels-and-remote-design.md`](2026-10-10-grouped-channels-and-remote-design.md)
and
[`2026-10-10-playback-and-device-controls-design.md`](2026-10-10-playback-and-device-controls-design.md).

The request: today the cloud service is single-user. Make it multi-tenant:
sign in on a PC by scanning a code, see the remote there, manage devices, and
therefore an overall admin backend.

Deployment is described generically: **the mainland cloud host** (1 vCPU without
VNNI, about 1.8 GB RAM, 3 Mbit/s public bandwidth, nginx in front; it also runs
`muse-asr` for another product) and **the overseas YouTube node** (yt-dlp +
deno, 1 core, 600 MB memory limit). `<site>` stands for the website's address.

## 1. What is single-user today

Every piece below assumes one owner. Each is a place the design has to change.

| Assumption | Where |
| --- | --- |
| One shared pairing token for every device; a public build sends none and cannot use the cloud host | `AV_PAIRING_TOKEN` in `main/av_config.h`, sent by `hello()` in `main/av_player.c`; checked by `authenticate()` in `server/tv_server.py` |
| HELLO carries no device identity; the server knows a device only by the port it dialled | `hello()` in `main/av_player.c`; `AVServer._session()` in `server/tv_server.py` |
| One media-server process per device model, serving one connection at a time; extra connections are closed | `AVServer.serve()` / `_reject_waiting()` in `server/tv_server.py`; units `tv-xingzhi` (port 8098) and `tv-passport` (port 8097) |
| Picture geometry, frame rate and byte rate are process-wide, read from the environment at import | `TV_GEOMETRY` in `server/frames.py`, `TV_FPS`, `TV_RATE_START` in `server/rate.py` / `server/live.py` |
| One channel table per process, module globals | `CHANNELS`, `load_channels()`, `reload_channels_if_changed()` in `server/live.py` |
| One relay state: creators, series, watched marks, positions, one cast slot, cast history, phone sessions, one sign-in code | `State` in `server/relay/state.py`, one `relay-state.json` |
| One relay channel table, stream URLs without an owner (`/ch/up01`) | `server/relay/table.py` (`STREAM_BASE`, `render()`), `_CHANNEL` in `server/relay/web.py` |
| One group-override file for everyone | `channel-groups.json`, `server/channel_groups.py`, `TV_CHANNEL_GROUPS_FILE` |
| Devices known to the relay are fixed on its command line | `--extra-device name:now-playing:events` in `server/relay/__main__.py`; `Relay.devices` in `server/relay/web.py` |
| One master key; anyone holding the key, the device's code or a signed-in phone controls every device and edits every creator | `Relay.key_ok()`, `Relay.code_ok()`, `State.sessions` |
| Relay stream state keyed by channel id only | `Relay._active`, `_paused`, `_bridge`, `_titles` in `server/relay/web.py` |

The good news: the media server and the relay already talk through files whose
paths come from the environment (`TV_NOW_PLAYING_FILE`, `TV_RELAY_EVENTS_FILE`,
`TV_RELAY_CHANNELS_FILE`, `TV_CHANNEL_GROUPS_FILE`, `TV_REMOTE_URL_FILE`). Giving
each device its own set of files needs no change inside the media server, only
a different environment per process. The design leans on that.

## 2. Goals and non-goals

Goals:

1. **Accounts.** Several users, each with their own devices, creators, series,
   watch progress, cast history and channel groups. One user cannot see or
   control another user's devices.
2. **Device identity.** Each device has a stable id and its own secret; a device
   is bound to an account by scanning a QR code on its screen.
3. **PC console.** Sign in on a computer by scanning a code with an
   already-signed-in phone; remote, devices, subscriptions, series, settings.
4. **Admin backend.** Users, devices online and offline with what they play,
   host load and bandwidth, ban and unbind, the global live channel table, an
   audit log.
5. **Several devices at once,** within what the host can carry, with the right
   geometry, frame rate and byte rate per device model.
6. **No data loss** in migration and no forced reflash: the two devices flashed
   today keep working throughout.

Non-goals:

- A public service open to anyone (see Q1: legal and cost reasons).
- Payments, quotas billed per user, or self-service sign-up without an invite.
- TLS or encryption on the device's TCP link (unchanged; see Security).
- Changing the wire format of media packets, the rate controller or the timing
  code (`server/timeline.py`, `server/rate.py`).
- High availability or more than one media host in the first four phases.
- Moving `muse-asr` (it stays; it is counted as a neighbour in capacity).

## 3. Questions for you to decide

Each has options and a recommendation. The rest of the document assumes the
recommendations; a different choice changes the sections named.

### Q1. Who are the tenants?

| Option | Consequence |
| --- | --- |
| **A. Family and friends, invite only, about 10 devices** | No public registration, no captcha or SMS cost, one admin. Fits the current host for 1–2 devices at a time and a modest upgrade beyond that. |
| B. Public sign-up | Needs registration, abuse control, a terms page, ICP filing for the site, and a host sized for unknown demand. The live channels come from a community playlist whose rights status is not verified (`server/README.md` says so), and rebroadcasting TV and video-site content to the public in mainland China needs an audiovisual programme licence. |

**Recommendation: A.** B is a different product with legal exposure; nothing in
this design prevents opening up later, but it should be a separate decision.

### Q2. How does a user sign in?

| Option | What it needs | Notes |
| --- | --- | --- |
| **A. Invite link, then "scan with a signed-in phone" for PCs** | Nothing external | The phone signs in once through an invite (a one-time link or QR from the admin); a PC is signed in by scanning its QR code with that phone, or typing a 6-character code into the phone. Builds on today's session cookies and QR sign-in (`State.new_session()`, `/TV/R/<code>`). |
| B. WeChat Open Platform "website application" login | A **verified company (enterprise) developer account** on the WeChat Open Platform (individuals cannot create a website application), an ICP-filed domain, a yearly verification fee | Familiar to users; adds a dependency on WeChat's review and on a company entity. The phone side inside WeChat would additionally need an Official Account (service account) for in-WeChat OAuth, also company-verified. |
| C. SMS code | An SMS provider account with an approved signature and template (restricted for individuals), a captcha against SMS-pumping, cost per message | Gives a recoverable identity (the phone number) but stores personal data and invites abuse. |

**Recommendation: A**, with the existing key link (`?k=<relay key>`) kept as the
admin's break-glass sign-in. B can be added later as a second way to attach a
session to an existing user (a `wechat_unionid` column), if a company account
becomes available.

### Q3. Admin roles

| Option | Roles |
| --- | --- |
| **A. Two account roles plus a device guest** | `admin` (you: everything), `user` (own devices and data); plus a **device guest** session created by scanning a bound device's code, which controls that one device and edits nothing else. |
| B. Three account roles | Adds `operator` (sees and bans, cannot change the channel table or roles). Useful only with a second person running the service. |

**Recommendation: A.** The guest session keeps today's household convenience
(anyone in the room scans the device and uses the remote) without giving the
guest the owner's subscriptions.

### Q4. Watch progress: per user or per device?

Today one state serves both devices, so a creator's progress is shared.
**Recommendation: per user** (the household shares one queue, as today), with the
cast slot **per device** (two devices of one user can cast different videos).
Per-device profiles can come later.

### Q5. Live channel table: global only, or per-user additions?

| Option | Consequence |
| --- | --- |
| **A. One global table, edited by the admin; each user hides, orders and regroups it** | No user-supplied URLs reach ffmpeg on the server. |
| B. Users may add their own live URLs | ffmpeg would open user-supplied URLs on the server: server-side request forgery (internal addresses, the relay's loopback ports, `file:`), so every URL needs an allowlist of schemes and a check against private addresses, and a review queue. |

**Recommendation: A**, with users able to *propose* a channel for the admin to
add (B's review queue without the risk).

### Q6. Budget for concurrency

The current host carries **one** Xingzhi stream at full quality, or **two**
devices with both degraded (section 7.6). More concurrent devices need more
public bandwidth first, then CPU and memory. Decide the target peak concurrency
(recommendation: plan for **4 concurrent** out of about 10 devices) and whether
to upgrade the host or add a separate media host. Phase 4 is only worth doing
together with that upgrade.

### Q7. Where does server code live?

`server/README.md` says the media server has moved to its own repository and
`server/` here is a frozen snapshot, yet recent server and relay work (for
example the multi-device and series commits) landed in this repository and the
cloud host runs it. **Recommendation:** decide which repository is canonical
before phase 1 and correct the README; this design cites the code in this
repository.

### Q8. What may an unbound device do?

**Recommendation:** with invite-only tenancy, an unbound device gets no
transcoding: the server sends CONFIG with the bind QR code and a still frame
saying how to bind, with silence, and costs almost nothing. An admin setting can
let unbound devices play the global table for 15 minutes per boot for
demonstrations.

## 4. Identity and data model

### 4.1 Entities

```
user ──< device_member >── device
 │                           │
 ├── session (phone, pc)     ├── device_state (cast slot, channel prefs)
 ├── user_state (creators,   └── per-device files (now-playing, events,
 │    series, progress, history)                     relay table, groups, remote URL)
 └── invite (created by admin)

source_cache (shared): creator upload lists and series episode lists, keyed by site + source
audit_log, login_ticket
```

### 4.2 Device identity (firmware change)

New firmware adds four flat keys to HELLO (flat, like the existing `volume`,
`pet`, `audio_only`):

```json
{"version":1, "channel":"cctv1", "switch":1, ...,
 "device_id":"a1b2c3d4e5f6", "model":"xingzhi-cube-154",
 "device_secret":"<43 characters, base64url of 32 random bytes>", "fw":"<git describe>"}
```

- **`device_id`**: the chip's factory base MAC from eFuse
  (`esp_efuse_mac_get_default()`), 12 lower-case hex digits. It is stable across
  reflashes and NVS erases, and the same under the Muse firmware and the player
  on the AI Passport. It is an identifier, not a secret: it is visible on the
  LAN.
- **`device_secret`**: 32 bytes from `esp_fill_random()` after Wi-Fi has started
  (the RF subsystem then feeds the hardware RNG), generated on first boot and
  stored in NVS, namespace `avsettings`, key `dsecret` (next to `pet` and
  `audio` in `main/av_store.c`). The Muse dual build never erases NVS
  (`docs/boards/passport-muse-dual.md`), so it survives switching firmwares. An
  `erase_flash` (needed once on the Xingzhi's first flash) creates a new secret,
  and the device must be re-paired (section 8.1).
- **`model`**: `xingzhi-cube-154` or `ai-passport`, from the build
  (`AV_VIDEO_WIDTH` and the board config in `main/av_protocol.h`). The server
  picks geometry, frame rate and byte rate from it (section 7.4).
- **`fw`**: the build's version string, for the admin page and for deciding when
  the legacy token can be switched off.

The existing `token` stays in HELLO while a build has one, so new firmware still
works against today's server: `authenticate()` checks only `version` and
`token` and ignores unknown keys. With per-device secrets a **public build** can
use the cloud host, because it no longer needs a compiled-in token; that matches
`AGENTS.md`'s rule to test the public build.

Effort on the firmware side is small: `hello()` in `main/av_player.c`, one store
function pair in `main/av_store.c`, and a host test for the HELLO JSON shape. The
AI Passport's HELLO stays well under `AV_CONTROL_MAX` (7168 bytes).

### 4.3 Binding a device

The firmware already draws a QR code of `remote_url` from CONFIG on the status
page (`AV_REMOTE_URL_MAX` 64 characters, upper-case alphanumeric for a version-2
code; `_REMOTE_URL` in `server/tv_server.py`). Binding reuses it, so **the QR part
needs no firmware change**:

1. A new device connects. The server sees an unknown `device_id`, stores it as
   `unclaimed` with the SHA-256 of its secret (trust on first use), and creates a
   20-character claim code (same alphabet as `State.ensure_code()`).
2. CONFIG carries `remote_url = HTTPS://<SITE>/TV/B/<CLAIM>`. The status page
   shows it. New firmware may also get `"bound":0` in CONFIG and show "scan to
   bind" without opening the status page; older firmware ignores the key.
3. A signed-in phone scans it. The page shows the model and the last four digits
   of the id and asks "Bind to your account?". On confirm the device becomes
   `active`, the user becomes its owner, the claim code is cleared and an audit
   entry is written.
4. On its next session (the server can force one by ending the current session)
   the device gets its owner's channel list, and `remote_url` changes to the
   device's own remote code `HTTPS://<SITE>/TV/R/<CODE>`, which from then on
   opens the remote for that device (guest session if the scanner is not a
   member).

A phone that is not signed in and scans the claim code is told to open the
remote from its home-screen icon first, or to type the claim code there
(section 5.2 explains why: cookie jars).

Unbinding (owner or admin) returns the device to `unclaimed` with a fresh claim
code, keeps its secret hash, and deletes its device state (cast slot, channel
prefs); the owner's user state is untouched.

### 4.4 Where today's data goes

| Today (file, field) | Becomes | Scope |
| --- | --- | --- |
| `relay-state.json` `creators[]` (kind `up` and `series`), `videos`, `watched`, `positions` | `user_state.creators` (same JSON shape) | user |
| `creators[].videos`, `fetched_at` | also written to `source_cache`, read by every user following the same source | shared |
| `relay-state.json` `cast`, `cast_position`, `cast_done` | `device_state.cast` | device |
| `relay-state.json` `history` | `user_state.history` | user |
| `relay-state.json` `sessions` (raw tokens) | `session` rows, **hashed** | user |
| `relay-state.json` `code` | the Xingzhi device's remote code (so its printed QR keeps working) | device |
| `channel-groups.json` | `user_state.group_overrides` | user |
| `channels.txt` | stays a file: the global live table, edited from the admin page | global |
| `relay-channels.txt` | rendered per device: `devices/<id>/relay-channels.txt` | device |
| `now-playing.json`, `relay-events.json`, `remote-url.txt` | `devices/<id>/…` | device |
| `--extra-device` list | `device` rows | — |

Keeping the creator and series JSON shape lets `State`
(`server/relay/state.py`) stay almost as it is: it becomes "one user's state",
loaded from and saved to a row instead of a file. `Video`, `Creator`,
`next_video()`, `episode()`, `mark_watched()` do not change. The sessions and
code move out of `State` into the account module.

Channel ids stay as today (`up01`…, `sr01`…, `cast`); they are unique within one
device's table, which is all the firmware needs (`AV_CHANNEL_ID_MAX` 16). The
per-device CONFIG limit (`CONFIG_MAX` 7168 bytes, `CHANNEL_MAX` 128 in
`server/channel_groups.py`) applies per device, so the relay's existing
"refuse a channel that would overflow CONFIG" check runs per device, against the
user's own creators plus the global table minus the channels the user hid.

### 4.5 Storage: SQLite

**SQLite (the standard library's `sqlite3`) in WAL mode, one file
`/var/lib/ai-passport-tv/tv.db`.**

- The server code uses only the standard library (`server/README.md`); SQLite
  keeps that.
- Tens of users and devices, hundreds of rows a day. The busiest writer is
  position saving: one row update per playing stream every 10 s (`tick()` in
  `Relay.play()`), well under 1 write/s at 4 concurrent streams.
- Two processes touch it: the relay (accounts, state, web) and the media gate
  (device authentication, `last_seen`). WAL allows concurrent readers with one
  writer; a `busy_timeout` of a few seconds covers the rare overlap.
- No extra daemon: a database server would cost 100+ MB on a 1.8 GB host that
  already runs `muse-asr`.
- Backups: `sqlite3 tv.db ".backup …"` nightly, kept 14 days, plus before every
  migration.

Files stay files where another process reads them on its own schedule (the
media workers' channel table, events and now-playing): that IPC already works and
is tested (`tests/test_relay_events.py`, `tests/test_cast_switch.py`).

Schema sketch (column types abbreviated):

```sql
user(id INTEGER PK, name TEXT, role TEXT CHECK(role IN ('admin','user')),
     status TEXT CHECK(status IN ('active','banned')), created_at REAL)
session(token_sha256 BLOB PK, user_id INT, kind TEXT /* phone|pc|guest */,
        device_id TEXT NULL /* guest only */, created_at REAL, last_seen REAL,
        expires_at REAL, user_agent TEXT, ip_prefix TEXT)
invite(code_sha256 BLOB PK, created_by INT, role TEXT, expires_at REAL, used_by INT NULL)
login_ticket(ticket_sha256 BLOB PK, short_code TEXT, created_at REAL, expires_at REAL,
             state TEXT /* pending|confirmed|used */, user_id INT NULL,
             pc_ip_prefix TEXT, pc_user_agent TEXT)
device(id TEXT PK, model TEXT, secret_sha256 BLOB, status TEXT /* unclaimed|active|banned|repair */,
       name TEXT, claim_code TEXT NULL, remote_code TEXT, fw TEXT,
       legacy_port INT NULL, created_at REAL, last_seen REAL, last_ip_prefix TEXT)
device_member(device_id TEXT, user_id INT, role TEXT /* owner|member */, PRIMARY KEY(device_id,user_id))
user_state(user_id INT PK, data TEXT /* JSON: creators, history, group_overrides, hidden, order */,
           updated_at REAL)
device_state(device_id TEXT PK, data TEXT /* JSON: cast slot, last channel */, updated_at REAL)
source_cache(site TEXT, source TEXT, kind TEXT, name TEXT, videos TEXT, fetched_at REAL,
             PRIMARY KEY(site,source,kind))
audit_log(id INTEGER PK, at REAL, actor_user INT NULL, actor_ip_prefix TEXT, action TEXT,
          target_kind TEXT, target_id TEXT, detail TEXT)
```

Secrets and tokens are stored only as SHA-256 hashes (they are 128+ random
bits, so a slow hash adds nothing). IP addresses are kept as a /24 (IPv4) or /48
(IPv6) prefix.

## 5. PC web console

### 5.1 One API, two layouts

The phone page (`server/relay/assets/remote.html`) and the console call the same
API. Today every endpoint acts on `Relay.target()` (the chosen device, or the
one that changed channel last). Multi-tenant keeps that idea, restricted to the
caller's devices:

- `GET /api/state` returns the caller's devices (`device_rows()` filtered by
  membership) and the state of the selected one.
- Every device action (`/api/switch`, `/api/seek`, `/api/next`, `/api/pause`,
  `/api/resume`, `/api/device`, `/api/pet`, `/api/audio`, `/api/cast`,
  `/api/history/play`) takes an optional `device`; without it the current
  `target()` rule applies over the caller's devices only. The server checks
  membership on every call.
- Account-level endpoints (`/api/creators`, `/api/series`, `/api/history`,
  `/api/groups`) act on the caller's user state; guests get 404.
- New: `/api/devices` (list, rename, unbind, re-pair, members), `/api/sessions`
  (list and revoke own sessions), `/api/login/*` (section 5.2), `/api/admin/*`
  (section 6, admin only).

The phone page keeps working with one change: it lists only the caller's
devices. The console is a second static page (`assets/console.html`) served by
the same process, with a wide layout: device list on the left, remote in the
middle, a tab area for subscriptions, series and settings.

### 5.2 Scan to sign in

```
PC browser                       server                          signed-in phone
   │ GET /tv/console (no session)  │                                   │
   │──────────────────────────────▶│ create login_ticket: 128-bit id,  │
   │◀── page with QR + short code ─│ 6-char code, expires in 2 min     │
   │   QR = https://<site>/TV/L/<ticket>                               │
   │                               │◀──── phone opens /TV/L/<ticket> ──│ (camera scan)
   │                               │      or types the 6-char code in  │
   │                               │      "Sign in a computer"         │
   │                               │── confirm page: "Sign in on a PC, │
   │                               │   <browser>, <city/IP prefix>?" ─▶│
   │                               │◀──────────── confirm ─────────────│
   │ long-poll /api/login/poll     │ ticket → confirmed, user_id       │
   │──────────────────────────────▶│                                   │
   │◀── Set-Cookie: pc session ────│ ticket → used (one-time)          │
```

- **Why also a typed code.** A phone camera opens the QR link in the system
  browser, and WeChat's scanner opens it in WeChat's own browser. Neither shares
  cookies with a remote added to the home screen (the existing docs already note
  that a home-screen app keeps its own cookies, which is why its address carries
  the session). If the browser that opens the link has no session, the page says
  "open the TV remote from your home screen and enter code `K7P-4QX`". The remote
  gets a "Sign in a computer" entry for that. A camera scanner inside the remote
  (getUserMedia plus a JavaScript QR decoder) is a later option.
- The confirm page shows the PC's browser and approximate network so a user is
  not tricked into approving someone else's ticket (QR phishing).
- Tickets are one-time, expire in 2 minutes, and ticket creation is rate-limited
  per IP (section 8.4). The PC session lasts 30 days (phone sessions keep
  today's 180 days, `SESSION_S`), is `HttpOnly; Secure; SameSite=Lax` like
  today's `tvs` cookie (`_cookie_header()`), and appears in the user's session
  list for revocation.
- Polling: the PC long-polls every 2 s or keeps one request open up to 25 s;
  nginx already sets `proxy_buffering off` and `proxy_read_timeout 60s` for
  `/tv/` (`packaging/nginx/tv-relay.conf`).

### 5.3 Console pages

| Page | Contents | Reuses |
| --- | --- | --- |
| Remote | now playing, groups and channels, progress bar, seek, pause, next, volume, brightness, pet, audio-only | `summary()`, `now()`, `groups()`, `video_progress()` |
| Devices | name, model, firmware, online, playing, last seen; rename, re-pair, unbind; members and guests; "bind a device" help | new `/api/devices` |
| Subscriptions | creators (Bilibili, YouTube), add/remove, next video per creator | `add_creator()`, `remove_creator()` |
| Series & episodes | series list, the episode that plays next, pick an episode, reset progress | `add_series()`, `State.episode()`; new "set next episode" |
| Cast | paste a link, history with replay and delete | `cast()`, `replay()`, `forget()` |
| Channels | hide, order and regroup live channels; propose a new channel | `set_group()`; new hide/order |
| Settings | own sessions (phone, PC, guests) with revoke; YouTube node status | new `/api/sessions`; `youtube_state()` |

## 6. Admin backend

Same process, `/tv/admin`, admin role only, every action written to the audit
log.

| Page | Contents |
| --- | --- |
| Overview | devices online now, per device: owner, model, channel, session length, picture and audio byte rate, frame rate, current byte target; host CPU, memory, public outbound bandwidth; YouTube node health and stream slots in use |
| Users | list, role, status, devices, last seen; create invite; ban (ends sessions, refuses their devices), unban; delete (with data export first) |
| Devices | all devices with status (`unclaimed`, `active`, `banned`, `repair`), owner, firmware, legacy flag; unbind, ban, accept a new secret, force reconnect |
| Live channels | the global `channels.txt` edited in a table: add, rename, reorder, test-open a URL, save with validation and a dated backup copy; proposals from users |
| Audit log | who, when, from where (IP prefix), what, target; filter by user or device |
| System | registration mode, unbound-device policy (Q8), legacy token on/off, concurrency caps, backups |

Data sources, all without extra daemons:

- **Per device:** the media worker already writes `now-playing.json` every 10 s
  (`now_playing.write()`, `REFRESH_S`) and logs bytes and busy time per 10 s
  (`tv_server.py` around the `busy=` log line). Extend the now-playing record
  with `bytes_10s`, `fps` and `target_rate`; the admin page reads the per-device
  files.
- **Host:** `/proc/stat`, `/proc/meminfo`, `/proc/net/dev` sampled every 5 s by
  the relay, kept 24 h in memory.
- **Per process:** `/proc/<pid>/stat` for each worker (the gate knows the pids).
- **Global table editing** validates with `live.parse_channels()` (the same parser
  the media server uses) and the CONFIG size check in
  `channel_groups.config_bytes()`; `tools/channel_config.py` already does this
  for the local page and its checks are reused.

## 7. Media path

### 7.1 Today

Each device model has its own unit and port (`tv-xingzhi` on 8098 at 240x180,
15 fps, `TV_RATE_START=200000`; `tv-passport` on 8097 at 320x180, 12 fps,
110000; only `tv-xingzhi.service` is in `packaging/systemd/`, the Passport unit
is a copy made on the host). `AVServer.serve()` accepts one connection, runs `_session()` to its end,
then closes whatever queued meanwhile. A channel change is a new TCP connection
(the channel travels in HELLO), so "the device" is really a sequence of short
sessions, and the per-device memory between them lives in `AVServer`
attributes: `pending_switch`, `switch_watcher` (last seen event `seq`, the
channel to return to after a cast), `channel_name`, `_device_uptime`. Every live
session starts its own ffmpeg that decodes the source and scales it to the
model's geometry (`LiveChannel`, `source_command()` in `server/live.py`), and the
Python process cuts, palettes, compresses and paces frames per connection
(`frames.py`, `rate.py`, `_pace_live()`).

### 7.2 Options

**(a) A gate plus one worker process per connected device.** A new small
`tv-gate` service owns the public port(s). It reads the first HELLO, checks the
device against the database, looks up the model profile, and hands the open
socket and the HELLO bytes to that device's worker over a Unix socket
(`socket.send_fds()`, Python 3.9+). If no worker runs for the device, the gate
starts one with the device's environment (`TV_GEOMETRY`, `TV_FPS`,
`TV_RATE_START`, and the per-device file paths). The worker is today's
`AVServer` with a new "take connections from the gate" mode instead of
`listener.accept()`. It stays alive while the device is online and for 10
minutes after, so `pending_switch` and the cast return channel survive channel
changes and short drops; then it exits.

**(b) One process serving many sessions.** Requires turning module-level state
into per-session state: `frames.WIDTH/HEIGHT/STRIPE_ROWS` (read once from
`TV_GEOMETRY`), the `CHANNELS` globals in `live.py`, the rate constants in
`rate.py`, and the single-connection assumptions in `AVServer`. The per-frame
work is Python (palette mapping, delta, compression, optional numpy perceptual
fitting), so one process would put every device's encoding behind one GIL on one
core, while `timeline.py` and `rate.py` depend on tight per-frame deadlines.

**(c) Static units per device.** A copy of `tv-xingzhi.service` per device, each
with its own port, token file and data directory, as was done for the Passport.
No code change, but each device needs its own port typed on its setup page and
opened in the firewall, idle units hold memory, and the relay's device list is
still a command line.

| | (a) gate + workers | (b) one process | (c) static units |
| --- | --- | --- | --- |
| Code change | new gate (~300 lines), worker mode in `AVServer`, supervisor | large refactor of timing-critical code | none |
| Per-model geometry | natural (environment per worker) | needs de-globalising `frames.py`, `rate.py` | natural |
| Uses more than one core | yes (processes) | no (GIL) | yes |
| Device setup | one address for every device | one address | one port per device |
| Idle cost | none after the grace period | low | one Python process per device, always |
| Isolation | a crash affects one device | one crash affects all | per device |

**Recommendation: (a).** It keeps the tested session code nearly untouched,
handles per-model geometry for free, and scales across cores. Use (c) only as a
stopgap if a third device must be served before phase 4.

### 7.3 Gate details

- **Ports.** The gate listens on 8098 and 8097 (the ports flashed devices dial)
  and nothing else; new devices can use either. With `model` in HELLO the port no
  longer decides the geometry. Workers listen on nothing. nginx is not in the
  device path: the device protocol is not HTTP, and an nginx `stream` proxy would
  only hide the device's address.
- **Handshake budget.** After HELLO the device waits 2 s for the CONFIG header
  (`header_deadline_ms` in `receive_task()`, `main/av_player.c`); the server
  reads HELLO within 250 ms (`IO_TIMEOUT`). Starting a worker costs a Python
  start-up with numpy (an estimate of 0.3–0.8 s on this host, to be measured),
  which fits but not by much; the worker sends CONFIG first, then samples the
  palette, as today. If the measurement is too close, the gate sends CONFIG
  itself and hands over the session after it, or keeps one warm spare worker per
  model.
- **Reconnect ordering.** A device can reconnect while its previous session is
  still closing. Today the listen backlog holds it (`listen(8)`). The worker
  keeps a queue of one incoming socket: the newest wins, older ones are closed.
- **Admission control.** The gate counts active video sessions. Above the global
  cap (set from capacity, section 7.6) or the per-user cap (default 2) it
  refuses with ERROR `{"reason":"server busy"}` and the device shows "server did
  not answer" and retries; an admin's device is admitted first. Audio-only
  sessions count as a fraction.
- **Banned or unknown secret.** The gate closes after HELLO with no CONFIG and
  writes an audit entry, rate-limited per device id and per IP.
- **Unbound devices** (Q8) are answered by the gate itself: CONFIG with the bind
  URL and a still frame drawn by `server/overlay.py` (its 5x7 font needs the
  extra letters for "SCAN TO BIND"), with silence; no ffmpeg.

### 7.4 Model profiles

`/etc/ai-passport-tv/models.json`:

| model | geometry | fps | `TV_RATE_START` | why |
| --- | --- | --- | --- | --- |
| `xingzhi-cube-154` | 240x180 | 15 | 200000 | `tv-xingzhi.service` |
| `ai-passport` | 320x180 | 12 | 110000 | the C3 cannot decode much more than 110 kB/s (`docs/boards/passport-muse-dual.md`) |

An unknown model is refused rather than guessed: a geometry mismatch ends every
session on the device (`CONFIG field width: server=320 device=240`).

### 7.5 The relay, per device

- **Device registry** replaces `--device-name` and `--extra-device`:
  `Relay.devices` is built from the database, each entry with its directory
  `/var/lib/ai-passport-tv/devices/<id>/` holding `now-playing.json`,
  `relay-events.json`, `relay-channels.txt`, `channel-groups.json`,
  `remote-url.txt`. The worker's environment points at the same files, so the
  media-server side of the IPC does not change.
- **Stream URLs carry the device:** `STREAM_BASE` becomes
  `http://127.0.0.1:8099/ch/<device_id>/` and `_CHANNEL` accepts
  `^/ch/([0-9a-f]{12})/(up\d\d|sr\d\d|cast)$`. From the device the relay finds
  the owner, hence whose creators and positions to use, and which cast slot.
- **Per-stream state** (`_active`, `_paused`, `_bridge`, `_titles`) is keyed by
  `(device_id, channel)` instead of `channel`.
- **Rendering:** `write_table()` renders one table per device: the owner's
  creators and series. `channel_rows()` and `groups()` use the owner's group
  overrides and hidden channels.
- **Shared source cache:** `refresh_if_stale()` reads and writes `source_cache`, so
  two users following the same creator cause one Bilibili list request per 30
  minutes (`LIST_MAX_AGE_S`), not two. Bilibili answers 412 to cloud addresses
  for some APIs already; more users must not mean more requests.
- **YouTube node limit:** `MAX_STREAMS = 2` in `server/relay/ytnode.py` is global
  to all tenants. The relay should show "YouTube busy, try again" instead of a
  long wait, and the admin overview shows the slots in use.

### 7.6 Capacity

Measured or documented figures:

- Xingzhi stream: up to 200 kB/s of picture plus about 8 kB/s of sound, about
  **1.7 Mbit/s**. AI Passport: 120–150 kB/s, about **1.0–1.2 Mbit/s**. Audio-only:
  about 8 kB/s, **0.07 Mbit/s** (`docs/boards/xingzhi-cube-154.md`,
  `docs/boards/passport-muse-dual.md`).
- ffmpeg for one Xingzhi stream used about 12 % of one core on a 2-core host
  (`docs/boards/xingzhi-cube-154.md`, "Cloud server").
- The current host has 3 Mbit/s of public outbound bandwidth = 375 kB/s.

Not measured, marked as **estimates**:

- The Python side (palette, delta, compression, pacing) per stream: estimate
  15–35 % of one core. Total per video stream, including ffmpeg decoding sources
  of varying resolution: **estimate 0.3–0.5 vCPU**.
- Memory per active worker (16 s of queued frames, numpy, ffmpeg): **estimate
  150–300 MB** (`tv-xingzhi.service` caps it at 600 MB).
- Audio-only sessions still decode the video today (the frames are dropped at
  send, `device_audio_only` in `_pace_live()`), so they save bandwidth, not CPU.
  Starting ffmpeg without video for audio-only sessions would fix that.

**What the current host can serve:**

| Load | Bandwidth | Verdict |
| --- | --- | --- |
| 1 Xingzhi | 1.7 of 3 Mbit/s | fine |
| 1 Xingzhi + 1 Passport | 2.7–2.9 of 3 Mbit/s | both degrade (already observed) |
| 2 Xingzhi | 3.4 of 3 Mbit/s | does not fit |
| audio-only | 0.07 each | bandwidth allows dozens; CPU (estimate 0.3+ vCPU each, until fixed) allows about 2 |

So **the current host serves one device at full quality, or two degraded**,
whatever the software does. With `muse-asr`, nginx and the relay on the same
single vCPU, CPU is the second wall at about two streams (estimate).

**For N concurrent video streams** (Xingzhi-class, 20 % headroom; all estimates
except bandwidth per stream):

| N concurrent | Public outbound | vCPU (media) + 1 for the rest | RAM |
| --- | --- | --- | --- |
| 2 | 4 Mbit/s | 1 + 1 = 2 | 2 GB |
| 4 | 8–10 Mbit/s | 2 + 1 = 3–4 | 4 GB |
| 6 | 12–15 Mbit/s | 3 + 1 = 4 | 4 GB |
| 10 | 20–25 Mbit/s | 5 + 1 = 6–8 | 8 GB |

Traffic, if billed by volume: one Xingzhi hour is about 0.75 GB. Four devices
three hours a day is about 9 GB a day, roughly 270 GB a month (estimate; check
the provider's current price per GB against a fixed-bandwidth plan).

Recommendation for Q6 (target 4 concurrent): either raise the current host to
about 4 vCPU / 4 GB with 10 Mbit/s or pay-by-traffic with a 20–30 Mbit/s cap, or
put the gate and workers on a second mainland host of that size and leave the
current host for the website, relay and `muse-asr`. The relay's stream port is
loopback-only today; a separate media host would need it on a private network
address with the same "no key, private only" rule.

The overseas YouTube node (1 core, `MAX_STREAMS = 2`) caps concurrent YouTube
viewing at two across all users, and its memory limit was set after an OOM with
two streams; more YouTube viewers need a bigger node.

**Before sizing anything,** measure on the current host: `pidstat -u -r 5` for
one Xingzhi live stream, one creator stream and one audio-only stream, 10 minutes
each.

## 8. Security

### 8.1 Device authentication

- Per-device secret (section 4.2) replaces the shared pairing token. A leaked
  secret affects one device and is revoked by the owner.
- Trust on first use: the first HELLO for an id pins its secret hash. A later
  HELLO with a different secret is refused and flagged. **Re-pair** (owner or
  admin) sets the device to `repair` for 10 minutes; the next HELLO from that id
  pins the new secret. This covers an `erase_flash`.
- The id is not secret (it is the MAC). Someone who knows a MAC could connect
  first with their own secret before the real device ever does; they would hold
  an `unclaimed` record but could not bind it without seeing the device's screen,
  and the admin can delete it. Binding, not first contact, is what grants
  access.
- The link has no TLS, as today: the secret travels in clear text like the
  current token. Against a passive observer on the path a later firmware can
  replace it with a challenge: the server puts a nonce in CONFIG, and the device
  sends `HMAC(secret, nonce)` in its next HELLO (every channel change is a new
  HELLO). Not in the first phases.
- `TV_ALLOW_PUBLIC=1` today requires a token; it will require either a token or
  the device database.

### 8.2 Tenant isolation

- Every API call resolves the session to a user (or a guest's one device) and
  checks membership before touching a device, and acts only on that user's
  state. Tests for each endpoint with another user's device id (expect 404, not
  403, as today's page does for unknown callers).
- Per-device files live in `devices/<id>/`, written by the relay and the worker
  only, mode 0640, group `tvserver`.
- The stream port stays loopback-only; nginx proxies only the page port.
- No user-supplied URL reaches ffmpeg on the server (Q5). Creator, series and
  cast links are parsed into site ids (`parse_creator_link()`,
  `parse_video_link()`, `youtube.py`'s patterns) before any request, as today.
- Admin endpoints check the role on the server, never only in the page.

### 8.3 Sessions

- Session tokens 256 bits (`secrets.token_urlsafe(32)` as today), stored hashed.
- Phone 180 days, PC 30 days, guest 30 days, all revocable; a user ban revokes
  all of that user's sessions.
- The existing `?s=<session>` in the home-screen address (needed because a
  home-screen app has its own cookies) stays; access logs stay off for `/tv/`
  (`packaging/nginx/tv-relay.conf`).
- "Issue a new QR code" (`/api/code`, `State.new_code()`) today ends every
  session for everyone. It becomes per device: it rotates that device's remote
  code and ends that device's guest sessions only.

### 8.4 Rate limits

- nginx `limit_req` on `/tv/` by IP: 10 requests/s burst 20 for the API; 5/min
  for login-ticket creation, `/TV/B/` and `/TV/R/` (codes have 100 bits, so this
  is about load, not guessing).
- Per user: creator and series additions 20/hour, casts 30/hour (each costs a
  Bilibili or YouTube resolve; a YouTube resolve takes about 18 s on the node).
- Gate: failed device authentications 10/min per IP and per device id, then
  1-minute refusal.
- Existing limits stay: 12 creators and 40 series per user (`MAX_CREATORS`,
  `MAX_SERIES`), 50 history entries, the per-device CONFIG size.

### 8.5 Secrets handling

- `/etc/ai-passport-tv/` keeps the relay key (admin break-glass), the legacy
  pairing token (until switched off) and the YouTube node config (the relay's
  JSON: url, key, pinned certificate); mode 0640, group `tvserver`, never in the
  repository (`AGENTS.md`).
- The database holds only hashes of secrets, sessions, invites and tickets.
- Logs never contain tokens, secrets, session ids or full IPs (today's rule in
  `serve()`'s logging); device ids may be logged.
- The YouTube cookies file stays on the overseas node only.

## 9. Migration and compatibility

### 9.1 Steps (zero data loss)

1. **Back up**: stop nothing yet; copy `/var/lib/ai-passport-tv`,
   `/var/lib/ai-passport-tv-passport` and `/etc/ai-passport-tv` to a dated
   archive; list file sizes and SHA-256 sums.
2. **Dry run** the migration script against the copy: it creates `tv.db`, the
   admin user, two `device` rows with `legacy_port` 8098 and 8097 (ids
   `legacy-xingzhi`, `legacy-passport` until the devices report real ids), imports
   `relay-state.json` and `channel-groups.json` as in section 4.4, hashes the
   existing phone sessions so phones stay signed in, and keeps the current
   sign-in code as the Xingzhi's remote code so its printed QR keeps working. It
   prints counts (creators, series, watched marks, positions, history, sessions)
   from both sides; they must match.
3. **Cut over**: stop `tv-relay`, `tv-xingzhi`, `tv-passport`; run the script on
   the live files; start the new relay (and, from phase 4, the gate). The old
   files are not modified or deleted; they are the rollback.
4. **Rollback**: stop the new services, start the old units with their old
   command lines. Positions advanced after the cut-over are lost on rollback
   unless exported back (the script has an `--export` mode that writes a
   `relay-state.json` from the database).
5. The script is idempotent (it refuses to run on a non-empty database unless
   told to replace it) and has host tests using fixture files in the shape of
   today's `relay-state.json` (`tests/test_relay_state.py` has examples).

### 9.2 Already-flashed firmware

- Old firmware sends the shared token and no `device_id`. The server (the
  static units in phase 1, the gate from phase 4) accepts the legacy token only
  on ports 8098 and 8097 and maps it to the `legacy-*` device of that port,
  owned by the admin. Everything works as today.
- When a device runs new firmware and reports its real `device_id`, the admin
  "adopts" it: the `legacy-*` record's state (cast slot, remote code) moves to
  the real id, and the legacy record is retired.
- When no device has used the legacy token for 30 days (visible on the admin
  page by `fw` and last use), the admin switches it off.
- New firmware against the old server works (unknown HELLO keys are ignored), so
  firmware can ship before the server.

## 10. Phased plan

Each phase ships something usable. Efforts are rough, in agent-days (one agent
working with host tests), and exclude your review and the on-device checks.

### Phase 0 — decisions and measurements (0.5 day)

Answer Q1–Q8. Measure CPU and memory per stream (section 7.6). Decide the
canonical server repository (Q7).

### Phase 1 — accounts, device binding, per-user relay state, on the current host (6–8 days)

Ships: invites, several users each with their own creators, series, progress,
history and groups; device binding by QR; the phone remote shows only your
devices; guests by device QR.

- 1a. Schema, account module (`server/accounts/`: users, sessions, invites,
  devices, audit), migration script with tests. (2 days)
- 1b. Relay per user: `State` loaded per user from SQLite; `Relay.devices` from
  the database; per-device directories and tables; stream URLs with device id;
  per-device keys for `_active` and friends; shared `source_cache`. (2–3 days)
- 1c. API scoping and membership checks; phone page lists only own devices;
  binding pages `/TV/B/<code>`; guest sessions. (1–2 days)
- 1d. Firmware: `device_id`, `model`, `device_secret`, `fw` in HELLO; secret in
  NVS; `bound` hint; host test. Media server: authenticate by device database
  as well as the legacy token; `remote_url` from the device's file. (1–2 days)

Media path unchanged: two static units, one device per model at a time.

Parallel: after 1a's schema and API contract are written down, 1b, 1c and 1d
run as three agents; 1d (firmware) touches nothing the others do.

### Phase 2 — PC console and scan login (3–4 days)

Ships: `https://<site>/tv/console` with scan-to-sign-in, remote, devices,
subscriptions, series and episodes, cast, channels, settings with session list.

- 2a. Login tickets, short codes, confirm page, long-poll, PC sessions. (1 day)
- 2b. Console page against the phase-1 API. (2–3 days)
- 2c. "Sign in a computer" entry and session list in the phone page. (0.5 day)

Parallel: 2a and 2b (2b against a stubbed login); 2b can start during phase 1
once the API contract is fixed.

### Phase 3 — admin backend (3–4 days)

Ships: `/tv/admin` with overview, users, devices, live channels, audit log,
system settings.

- 3a. Metrics: now-playing extension in the media server, `/proc` sampler. (1 day)
- 3b. Admin API and pages. (2 days)
- 3c. Global channel table editor reusing `live.parse_channels()` and
  `tools/channel_config.py`'s checks. (1 day)

Parallel: 3a, 3b, 3c as three agents; can overlap with phase 2.

### Phase 4 — media gate and per-device workers, with the host upgrade (5–8 days)

Ships: one address for every device, several devices at once with the right
profile each, admission control, unbound devices handled without transcoding.

- 4a. `tv-gate`: HELLO, device check, profiles, worker supervisor, fd passing,
  admission control, unbound-device still frame. (2–3 days)
- 4b. Worker mode in `AVServer` (connections from the gate, idle exit) with the
  existing `tests/test_tv_server.py` suite passing. (1–2 days)
- 4c. Audio-only sessions without video decoding. (1 day)
- 4d. Load test with simulated devices on the new host; systemd units; docs
  (`docs/boards/*.md`, `server/README.md`). (1–2 days)

Parallel: 4a and 4b against an agreed hand-off interface; 4c independent. 4a can
start during phase 2, since it needs only the device table.

**Total: about 18–25 agent-days**, roughly 3–4 calendar weeks with parallel
agents and review.

## 11. Risks

| Risk | Effect | Mitigation |
| --- | --- | --- |
| Bandwidth: 3 Mbit/s carries one full-quality stream | multi-tenant without an upgrade means users degrade each other | admission control; Q6 upgrade before phase 4 |
| CPU estimates are unmeasured | sizing off by 2x either way | phase 0 measurement |
| `muse-asr` on the same vCPU | noisy neighbour in both directions | `CPUQuota`/`Nice` as today; separate media host if needed |
| Bilibili rate limits and 412s grow with users | creator lists fail for everyone | shared `source_cache`; per-user limits |
| YouTube node: 2 stream slots, one cookies account | YouTube "busy" or the account challenged with more traffic | show "busy"; bigger node; keep the 6-hour cookie check |
| Content rights and licensing | legal exposure if opened beyond family and friends | Q1 = invite only |
| Old firmware and the legacy token | a shared secret stays valid during transition | legacy only on two ports, mapped to two records; switch off after 30 days unused |
| Erasing flash creates a new device secret | device refused until re-paired | re-pair button; admin can accept |
| Cookie jars (camera, WeChat, home-screen app) | scan-to-sign-in fails silently | typed 6-character code |
| SQLite misuse (long transactions, two writers) | "database is locked" errors | WAL, short transactions, `busy_timeout`, nightly `.backup` |
| Worker start latency inside the device's 2 s CONFIG wait | first connection after idle times out on the device | measure; keep workers 10 minutes; a warm spare worker per model, or the gate sends CONFIG itself |
| Refactoring the relay (1000-line `web.py`) | regressions in casting, seek, pause | keep the existing relay tests green; key-by-device change done mechanically first |
| Two repositories for the server (Q7) | changes land in the wrong place | decide in phase 0 |
| Plain-text device link | a secret can be sniffed on the path | per-device scope limits damage; challenge-response later |

## 12. Not verified

- All CPU and memory figures marked as estimates in section 7.6.
- Worker start-up time on the current host.
- That `esp_efuse_mac_get_default()` gives the same id under the Muse firmware
  and the player on the same AI Passport (expected: it reads eFuse, not NVS).
- WeChat Open Platform and SMS provider requirements and prices as of today; check
  before choosing Q2 B or C.
- Traffic prices per GB; check the provider's console.
