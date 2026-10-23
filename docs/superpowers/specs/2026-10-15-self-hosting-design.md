<p align="right">
  <a href="2026-10-15-self-hosting-design.zh_CN.md">简体中文</a> · <strong>English</strong>
</p>

# Self-hosting package ("Xiaozhi TV at home") — design

Status: draft 2026-10-15, decided 2026-10-16 ("Decisions" below); public
playlist presets added 2026-10-22 (section 8.2); YouTube local mode (S5b)
implemented 2026-10-23 (section 8.4). Phases S1
and S3 are implemented, the 0.x beta's code (section 15, with what is verified
and what is not); S2 and S4–S6 are design. Follows
[`2026-10-12-multi-tenant-design.md`](2026-10-12-multi-tenant-design.md)
(accounts, binding, console, admin backend: phases 1–3 done; phase 4, the media
gate, not built),
[`2026-10-12-phone-audio-design.md`](2026-10-12-phone-audio-design.md),
[`2026-10-12-voice-control-design.md`](2026-10-12-voice-control-design.md),
[`2026-10-13-wifi-setup-design.md`](2026-10-13-wifi-setup-design.md) and
[`2026-10-13-sound-provisioning-design.md`](2026-10-13-sound-provisioning-design.md).

The request: the owner's small mainland cloud host (about one effective core,
1.8 GB RAM, 3 Mbit/s out) keeps serving the owner and a few friends. Everyone
else gets a **self-hosting package**: a Docker image, a tutorial and a web
flasher, so each household runs the server at home on an old laptop, a NAS, a
mini PC or a Mac, with the devices on the same LAN. That costs no cloud
bandwidth, gives the best picture, and a home address is not refused by
Bilibili the way cloud addresses are. The image **ships no content**: no channel
list, no stream addresses, no cookies. The first person to register becomes the
admin.

Already settled with the owner and not reopened here: Netlify and Cloudflare
cannot run the media server (no long-lived processes or ffmpeg, the device
speaks raw TCP, Cloudflare's free plan has no mainland PoPs and restricts video
on its CDN); Cloudflare Tunnel can expose a home server's web pages; Netlify or
Cloudflare Pages can host the static website, tutorial and web flasher; Oracle
Cloud Always Free is the only notable free VM, and it is overseas.

Names used below: **the household server** is the machine at home running the
package; `<your-host>` is its LAN address or name; `<static-site>` is the
project's static website; `<owner>` is the project's GitHub or registry
namespace. The figures in section 13 come from the 2026-10-16 load test on an M1 Pro
(per-stream cost measured; the saturation sweep was stopped part-way); figures
marked **estimated** or **expectation** are not measured yet.

## 1. Starting point: what assumes the cloud host today

The code is close to self-hostable: the media server and relay are standard
library Python plus ffmpeg, multi-user mode works on SQLite with no daemon, and
public firmware builds already carry a per-device identity and no shared
token. What stands in the way:

| Assumption | Where | Matters because |
| --- | --- | --- |
| The relay refuses to start until an admin exists, and only the migration from single-user files creates one | `_serve_tenants()` in `server/relay/__main__.py`; `server/tenancy/migrate.py` | A fresh household has nothing to migrate |
| The relay's pages listen on loopback only and expect nginx in front (`/tv/`, `/TV/R/`, `/TV/B/`, `/TV/L/`, `/TV/P/`, `/TV/W`, `/tv/listen/<route>/`) | `make_servers()` in `server/relay/web.py`; `packaging/nginx/tv-relay.conf`, `tv-listen.conf` | Needs a front that runs the same on Docker, a Mac and a NAS |
| The session cookie is always `Secure` | `_cookie_header()` in `server/relay/web.py` | Browsers do not keep a `Secure` cookie set over plain `http://` on a LAN address, so sign-in would not stick (the `?s=` address of the home-screen app still works) |
| The caller's address comes from `X-Real-IP` only when the peer is loopback | `_ip()` in the request handler of `server/relay/web.py` | Behind a tunnel every request would look like one address, and the per-network rate limits would lump all callers together |
| One media-server process per board model, one connection at a time; others are closed | `AVServer.serve()` / `_reject_waiting()` in `server/tv_server.py`; units `tv-xingzhi` (8098), `tv-passport` (8097) | A household with two Xingzhi boxes can watch on one at a time |
| Built-in fallback channels, the repository's `channels.txt` found next to the code, and a community playlist URL as the channel page's default source | `BUILTIN_CHANNELS` and `load_channels()` in `server/live.py`; `channels.txt`; `DEFAULT_SOURCE` in `tools/channel_config.py` | The image must ship no content |
| An empty channel table is an error | `parse_channels()` in `server/live.py` ("channel table contains no channels") | A fresh household starts with no live channels |
| The sound-setup page fills the server field with the page's own host name | `siteHost()` in `server/relay/assets/wifi.html` | Opened through a tunnel it would send the tunnel's name, which a device cannot dial |
| The YouTube node needs TLS with a pinned certificate and has no proxy setting | `server/relay/ytnode.py` (`--cert`, `--tls-key`), `server/relay/youtube.py` | A home in mainland China reaches YouTube only through a proxy the user already has |
| Voice needs `muse-asr`, which lives in another repository | `server/voice/asr.py` (Unix socket `POST /asr`) | Needs a small recogniser container of our own |
| The packaged LAN server (`tools/launch.py`, PyInstaller builds) is single-user and uses 8096 (media) and 8097 (channel page) | `tools/launch.py`, `packaging/tv-server.spec` | 8097 is the Passport's media port in the multi-user plan |
| Firmware: public builds have no default server and no known site; IPv4 only; one factory app partition, no OTA; the Xingzhi's 4G modem is put in airplane mode | `main/av_config.h`, `main/Kconfig.projbuild` (`AV_DEFAULT_SERVER`, `AV_PUBLIC_SITE`); `hints.ai_family=AF_INET` in `main/av_player.c`; `partitions.csv`; `docs/boards/xingzhi-cube-154.md` | Shapes the setup flow, "away from home" and firmware updates |

What already fits: the sound-setup page carries a server field and fills it
with the address it was opened from; the device's sound page works before it
knows any site ("open the remote's /tv/wifi on the phone"); a portless server
address gets the board's own port (8098 Xingzhi, 8097 Passport,
`av_server_addr_use_build_port()`); unbound devices are answered with a "bind
this TV" still frame and no transcoding (`server/tenancy/unbound.py`);
self-registration through an unbound device, device recovery and the admin
backend all exist; CONFIG's `remote_url` accepts `HTTP://` as well as `HTTPS://`
(`_REMOTE_URL` in `server/tenancy/gate.py`, at most 64 characters).

## 2. Goals and non-goals

Who uses it: **non-technical families**, with **one technical person per
household** who does the setup once (installs Docker or a NAS app, flashes the
devices, adds channels). Everyone else only uses the phone remote and the
devices.

Goals:

1. **One download, one command** (or one NAS form) brings up everything a
   household needs: media server(s), relay with accounts, web pages, optional
   voice, optional YouTube.
2. **First run without a terminal session beyond reading one line**: the
   address and a setup code are printed, a phone scan makes the first admin.
3. **Several devices at once** on one household server, of both board models,
   with one address to type on every device.
4. **No content shipped.** Users add their own live channels, creators, series
   and, if they want YouTube, their own node or proxy and cookies.
5. **Same code as the owner's cloud host**: the package is a way of running
   `server/`, not a fork of it. Everything multi-user is reused.
6. **Images for x86-64 and arm64**, a plain-Python path for Macs without
   Docker, and written stories for Windows and NAS.
7. **Firmware from the browser**: a web flasher for the public builds.
8. **Safe defaults**: LAN only, admin closed to the internet, no shared tokens,
   secrets generated on first start and never printed except the one-time
   setup code.

Non-goals:

- A hosted service for strangers (the owner's host stays family and friends).
- Native Windows service, Android, router firmware, 32-bit ARM (Raspberry Pi 3,
  Zero 2) as supported platforms.
- Bundling any channel list, stream address, cookie or proxy. (Playlist links:
  the owner decided on 2026-10-22 to list a few public third-party playlists by
  their links, section 8.2; the community playlist below stays out.)
- Making devices reach home over 4G without the user doing network work
  (section 6.6).
- Changing the wire protocol, the rate controller or timing code.
- Hardware video decoding (VAAPI, V4L2) in the first release; it is listed as
  a later optimisation.

## Decisions (2026-10-16)

The owner accepted every recommendation (Q1–Q12 as recommended). The 0.x beta comes first: S1 + S3 + a short S4, one device per model at a time (Q1 option A); the 1.0 release adds S2, the gate-lite (Q1 option C). No performance-heavy work such as load tests for now; the estimated figures in section 13 get measured later on real hardware.

## 3. Questions for you to decide

Each has options and a recommendation; the rest of the document assumes the
recommendations.

### Q1. Several devices at once in the first release?

| Option | Consequence |
| --- | --- |
| A. Ship with today's media path: one device per board model at a time | Fastest (no media work). Two Xingzhi boxes in one home take turns: the second one shows "server did not answer" and retries. |
| B. One media process and one port per device | No new media code, but each extra device needs its own port typed on its setup page, which the sound setup cannot know (section 5.2). |
| **C. A "gate-lite" now: one listener per model port hands each device to its own worker process** | 3–4.5 agent-days. One address for every device. It is the core of the multi-tenant design's phase 4, minus the cloud parts, so the cloud host reuses it later. |

**Recommendation: C for version 1.0**, with A acceptable for a 0.x beta to a
few friends while C is built.

### Q2. How is the first admin proven?

| Option | Risk |
| --- | --- |
| **A. One-time setup code** printed in the server's log and console banner (and as a terminal QR code that opens the setup page with the code filled in) | Needs one look at the log, which the setup person sees anyway (`docker compose up`, the NAS's log tab). A tunnel opened before setup cannot be used to take the server. |
| B. Whoever registers first on the LAN | Simplest; any guest on the home Wi-Fi, or anyone through a tunnel set up early, could become admin. |
| C. Whoever binds the first device | Needs physical access to a device; but the device must be flashed and online first, which reorders the tutorial. |

**Recommendation: A.** The owner's wish "the first person to register becomes
admin" is kept; the code only proves that this first person is the one who
installed the server.

### Q3. The web front inside the package

| Option | Consequence |
| --- | --- |
| **A. A small standard-library Python front** (`server/front.py`) replacing nginx in the package | Same on Docker, a Mac and a NAS; can read the per-device listen ports the gate writes; handles the two-port split of section 6.3. About 300 lines plus tests. |
| B. nginx (or Caddy) inside the image | Battle-tested, configs exist; but the plain-Python path on a Mac would need nginx too, and per-device listen routing needs generated config and reloads. |

**Recommendation: A.** Households have a handful of clients; nginx stays on the
cloud host.

### Q4. Where are images published?

Docker Hub is not reliably reachable from mainland China, and GHCR is slow
there. **Recommendation:** publish to GHCR (`ghcr.io/<owner>/xiaozhi-tv`) and
to a free Alibaba Cloud Container Registry personal namespace (fast in
mainland China), and attach an offline `docker save` archive per architecture
to each release for NAS users whose registry access fails.

### Q5. Where is the static website hosted?

| Option | Notes |
| --- | --- |
| **A. Cloudflare Pages on a custom subdomain** | Free, HTTPS (needed for Web Serial), unaffected by the cloud host's 3 Mbit/s. The default `*.pages.dev` names have had reachability problems from mainland China; a custom domain avoids most of it. |
| B. The owner's existing site | Fast in China; firmware downloads (2–3 MB each) compete with the owner's streams for 3 Mbit/s. |
| C. GitHub Pages (upstream's flasher is there) | Slow or unreachable in parts of mainland China. |

**Recommendation: A**, with firmware also attached to GitHub Releases, and an
object-storage mirror for firmware in mainland China later if users report
slow downloads.

### Q6. How much should the Chinese tutorial say about YouTube?

YouTube is not reachable from mainland China without a proxy, and the package
must not ship or recommend one. **Recommendation:** the feature stays, as a
generic "YouTube node address" or "HTTP proxy address" setting; the Chinese
tutorial names the setting and says it is for households outside mainland China
or that already run their own node, with no instructions for obtaining a
proxy; the English tutorial is the same with more detail on cookies.

### Q7. Voice in the package?

**Recommendation:** an optional second container (`xiaozhi-tv-asr`,
SenseVoice-Small through sherpa-onnx, like `muse-asr`), off by default because
it adds about 550 MB of RAM (measured for `muse-asr` on the cloud host). The
model is downloaded on first enable rather than baked into the image, so its
licence is accepted by the user (check the model's licence before deciding;
section 17).

### Q8. Remote access and devices away from home

**Recommendation:** nothing on by default. The tutorial has two optional
recipes for the **web pages**: Tailscale (private, works for listening on the
phone) and Cloudflare Tunnel on the household's own domain (shareable links,
remote control). For **devices away from home**, a documented "advanced"
recipe only (router port forward plus DDNS, or frp through the user's own VPS);
no project-run relay (section 6.6).

### Q9. Which boards are first-class?

**Recommendation:** the Xingzhi Cube 1.54 is first-class (public build, web
flasher with erase-first). The AI Passport's standalone public build is
supported through the same flasher (never erase). The Passport-beside-Muse
build stays "build it yourself" unless you confirm that Muse's signing key may
be used in CI (section 9.4).

### Q10. Firmware updates

**Recommendation:** manual re-flash from the web flasher for now (settings and
identity survive a re-flash without erase). OTA from the household server needs
a new partition table (one erase on every Xingzhi) and signed images; decide
later, after the first release has users (section 9.5).

### Q11. May public builds fall back to your own server?

**Recommendation: no.** Public builds have no default server
(`CONFIG_AV_DEFAULT_SERVER` empty). Friends on your host get a separate build
with your address baked in, as today. Otherwise every self-hoster's device
would contact your 3 Mbit/s host on a mistyped address.

### Q12. Update check and telemetry

**Recommendation:** no telemetry. The admin backend fetches a small
`latest.json` from `<static-site>` once a day (no identifiers, no cookies) to
show "a new version is available"; a setting turns it off.

## 4. The package

### 4.1 Shape

One core container runs every process that talks over loopback or the shared
data directory; optional containers add voice and a tunnel.

```
container "tv" (image xiaozhi-tv)
 supervisor  python3 -m server.selfhost            PID 1 (via tini), restarts children, backups
 ├── front       :8080 LAN pages (all)   :8081 public pages (tunnel only, optional)
 │                 -> 127.0.0.1:8100 relay pages, 127.0.0.1:<listen port> per device
 ├── relay --db  127.0.0.1:8099 streams (no key, loopback only), 127.0.0.1:8100 pages
 ├── gate        :8097 :8098 (phase S2; phase S1 runs one media server per model there)
 │    └── worker per playing device  (today's AVServer in worker mode, ffmpeg child)
 └── ytnode      127.0.0.1:47443, only with YouTube in "local" mode (section 8.4)

container "asr"     optional, profile "voice": POST /asr on /data/run/asr.sock
container "tunnel"  optional, profile "tunnel": cloudflared -> tv:8081
```

Why one core container rather than one per process: the relay's stream port
and the listen ports are loopback-only by design (no key on them,
`packaging/systemd/tv-relay.service`); splitting them into containers would put
them on a network. The processes already talk through files in one data
directory (`TV_NOW_PLAYING_FILE`, `TV_RELAY_EVENTS_FILE`, the per-device files
under `devices/<id>/`), which one volume gives for free. The supervisor reuses
the approach of `tools/launch.py` (start, watch, restart, report in plain
Chinese).

### 4.2 Image

- **Base:** `python:3.12-slim` (Debian). Standard library only; numpy is not
  installed (the default degrade, `steady`, does not need it).
- **ffmpeg:** Debian's dynamically linked package. The fully static build
  crashed resolving host names on the cloud host
  ([`xingzhi-cube-154.md`](../../boards/xingzhi-cube-154.md#cloud-server));
  the version is pinned per release and covered by the smoke test.
- **Architectures:** `linux/amd64` and `linux/arm64`, built with `docker buildx`
  in CI from one Dockerfile. No 32-bit ARM.
- **YouTube tools** (yt-dlp, deno) are not in the core image: they would add
  roughly 100 MB and need weekly updates. Local YouTube mode downloads them into
  `/data/youtube/bin/` on first enable, verified by checksum, and updates them
  the way `packaging/yt-relay/update-ytdlp.sh` does (only after the test video
  resolves).
- **User:** runs as an unprivileged user. `PUID`/`PGID` environment variables
  set its ids, the convention NAS users know, so the data folder they created
  in the NAS UI is writable.
- **Size:** expectation 250–350 MB unpacked; measured in phase S1.
- **Excluded from the build context** (`.dockerignore`): `channels.txt`,
  `server/.local/`, `main/av_private_config.h`, any `*.pem`, `token`,
  `relay-key`, cookies. A CI check fails the build if the image contains a
  `channels.txt` or the strings of `BUILTIN_CHANNELS` / `DEFAULT_SOURCE`.
- **Licences:** a `NOTICE` lists ffmpeg (GPL build from Debian, with a pointer
  to Debian's source packages), Python, jsQR (already vendored with its licence)
  and this repository's MIT licence.

### 4.3 Data layout

Everything a household has lives in one folder, mounted at `/data`:

```
/data/
  config.toml             settings (section 4.4); created with defaults on first start
  secrets/                mode 0700
    relay-key             generated: relay key, break-glass link, listen token secret
    wifi-key              generated by the relay (X25519, for sealed Wi-Fi changes)
    setup-code            only while no admin exists; deleted when the admin is made
    youtube-node.json     optional, user-supplied: {url, key, cert} of a remote node
  youtube/                optional: cookies.txt (the user's own), bin/ (yt-dlp, deno)
  tv.db  tv.db-wal  tv.db-shm
  devices/<id>/           per-device files (now-playing, events, channel table, groups, remote URL)
  channels.txt            the live table; empty at first
  backups/                nightly tv-YYYYMMDD.db, kept 14 days
  exports/                user exports from the admin backend
  run/                    sockets and the gate's worker registry (not backed up)
  models/                 optional, the voice model
```

Back up the whole folder except `run/`. `secrets/` and `youtube/cookies.txt`
are sensitive; the tutorial says so in the backup section.

### 4.4 Config file

TOML (read with the standard library's `tomllib`, Python 3.11+). Written with
comments on first start; the admin backend's System page shows it read-only in
phase S1 and edits the safe keys later.

```toml
[server]
name = "Home"                 # shown in page titles and on the setup page
lan_port = 8080               # phone remote, PC console, admin, setup: LAN only
public_port = 0               # 8081 to offer pages to a tunnel (section 6.3); 0 = off
device_address = ""           # what devices dial; empty = learnt at first setup (section 6.1)
public_url = ""               # e.g. https://tv.example.com when a tunnel exists; empty = LAN URL

[devices]
allow_internet = false        # TV_ALLOW_PUBLIC: accept device connections from public addresses
max_streams = 0               # 0 = no cap; otherwise refuse "server busy" above it

[accounts]
signup_via_device = "lan"     # "lan" | "anywhere" | "off": self-registration with an unbound device
admin_over_tunnel = false     # admin backend and break-glass key link on the public port

[youtube]
mode = "off"                  # "off" | "node" (remote node) | "local" (yt-dlp in this container)
proxy = ""                    # local mode only: http://host:port, used by yt-dlp and ffmpeg
cookies = "youtube/cookies.txt"

[voice]
asr_socket = ""               # "/data/run/asr.sock" when the asr container runs

[maintenance]
backup_keep_days = 14
update_check = true           # daily latest.json from the static site, no identifiers
```

### 4.5 First start

The supervisor, before starting anything:

1. Creates the folders and `config.toml` if missing; checks it can write
   `/data` and says what to do if not (the most common NAS problem).
2. Generates `secrets/relay-key` (`secrets.token_urlsafe(32)`, 0600) if
   missing. The relay creates `wifi-key` itself (`--wifi-key`).
3. Opens or creates `tv.db` with the existing schema code (`server/tenancy/db.py`),
   sets `meta.legacy_token = 0` (no shared token in self-hosting; there is also
   no token file), and records `meta.selfhost = 1`.
4. If no admin exists: writes a fresh setup code (10 characters from the
   existing code alphabet, about 50 bits) to `secrets/setup-code` and prints
   the banner below. A new code is made at every start until an admin exists.
5. Starts the relay (`--db /data/tv.db`; the relay's "no admin yet" refusal is
   replaced in self-host mode by "setup pending": every page except `/tv/setup`
   answers with a pointer to it), the media side, the front, and the optional
   YouTube node.

The banner (the real one is in Chinese):

```
Xiaozhi TV is running.
  Open on a phone or computer on this network:  http://<lan address>:8080/tv/setup
  Setup code: K7P4-QX2M9   (one use; needed to create the administrator)
  [terminal QR code of http://<lan address>:8080/tv/setup#K7P4QX2M9]
  Devices: enter <lan address> as the server address (no port needed).
```

`<lan address>` is known only with host networking or the plain-Python path;
inside a bridged container the banner says "this computer's address" and the
setup page fills it in from the address the browser used (section 6.1).

### 4.6 Compose file (shape)

```yaml
services:
  tv:
    image: ghcr.io/<owner>/xiaozhi-tv:1
    restart: unless-stopped
    ports: ["8080:8080", "8097:8097", "8098:8098"]
    volumes: ["./data:/data"]
    environment: [PUID=1000, PGID=1000, TZ=Asia/Shanghai]
    logging: {driver: json-file, options: {max-size: 10m, max-file: "3"}}
  asr:
    image: ghcr.io/<owner>/xiaozhi-tv-asr:1
    profiles: [voice]
    restart: unless-stopped
    volumes: ["./data/run:/run/asr", "./data/models:/models"]
  tunnel:
    image: cloudflare/cloudflared:<pinned version>
    profiles: [tunnel]
    restart: unless-stopped
    command: tunnel --no-autoupdate run
    environment: [TUNNEL_TOKEN=${TUNNEL_TOKEN}]
```

`docker compose up -d` starts the core; `--profile voice` or `--profile tunnel`
adds the others. Bridge networking is the default everywhere: on Linux Docker
keeps the device's source address for published ports, which the media
server's private-address check and audit use. Host networking is offered as a
commented alternative for Linux, mainly so the banner can print the LAN
address.

### 4.7 Without Docker (Mac, Linux)

`./run.sh --selfhost [--data <folder>]` runs the same supervisor from a
checkout or a release archive. Needs Python 3.11+ and ffmpeg (Homebrew on a
Mac: `brew install python@3.12 ffmpeg`); the existing checks in
`tools/launch.py` report what is missing with the install command. The data
folder defaults to the per-user location `tools/datadir.py` already chooses.
The voice container has no plain-Python equivalent in phase S1 (Docker only).

Mac specifics the tutorial covers: the Mac must not sleep while anyone watches
(System Settings, Energy: prevent automatic sleeping on power adapter); the
first start shows the macOS "accept incoming connections" prompt for Python,
which must be allowed.

### 4.8 Windows

Docker Desktop with WSL 2, the same compose file. The tutorial covers: enabling
WSL 2 (one restart), the Windows firewall prompt for Docker's backend on the
first published port ("private networks"), and sleep settings. A native
`tv-server-windows-amd64.exe` in self-host mode (the PyInstaller build exists
for the single-user LAN server) is possible later, but `socket.send_fds`, which
the gate uses, does not exist on Windows, so it would serve one device per
model; not planned.

### 4.9 NAS

- **Synology** (DSM 7.2+, Container Manager), x86 models (Celeron J4125, AMD
  Ryzen R1600/V1500B): a "Project" from the compose file, with the data folder
  created in File Station first. ARM "j"/"value" models (Realtek RTD1296/1619B)
  are arm64 but weak: estimated one stream at most.
- **UGREEN** (UGOS Pro, DXP series, Intel N100/i5): Docker app, compose
  supported; same as Synology.
- **QNAP** (Container Station) x86: same compose.
- Common pitfalls the tutorial lists: folder permissions (`PUID`/`PGID`), a
  port already used by another NAS app (change `lan_port` and the mapping),
  pulling from a mirror or importing the offline archive (Q4).

## 5. Several devices on one host

### 5.1 Today

The cloud host runs one media server per model (`tv-xingzhi` 8098 at 240x180,
15 fps; `tv-passport` 8097 at 320x180, 12 fps). Each serves one connection at a
time and closes the rest. The per-device state between sessions (pending
switch, return channel after a cast, uptime) lives in `AVServer` attributes.
Multi-user mode already authenticates each device by id and secret and points
the session at that device's own files (`server/tenancy/gate.py`, `apply()`),
so **which** device connects is solved; **how many at once** is not.

### 5.2 Options

| | A. One device per model (today) | B. One process and port per device | C. Gate-lite + worker per device |
| --- | --- | --- | --- |
| New code | none | supervisor config, front routes per process | listener, fd hand-off, worker mode, worker registry |
| Address on the device | `<your-host>` | `<your-host>:<its port>`, typed per device | `<your-host>` |
| Sound setup | works | cannot know which port to send | works |
| Concurrency | 1 Xingzhi + 1 Passport | as many processes as configured | up to `max_streams` / the machine |
| Idle cost | 2 Python processes | 1 per configured device, always | listener only; workers exit 10 min after the device leaves |
| Reused by the cloud host's phase 4 | — | no | yes |
| Effort | 0 | 1–1.5 days | 3–4.5 days |

**Recommendation: C** (Q1). B's per-device ports break the one-address setup
that sound provisioning gives for free, and its work is thrown away later.

### 5.3 Gate-lite

The multi-tenant design's section 7.3, minus what only a shared cloud host
needs:

- **Listen** on 8097 and 8098 (the ports the two public builds dial when the
  address has no port). Read HELLO within the existing 250 ms
  (`IO_TIMEOUT`), authenticate through `tenancy.gate` as today.
- **Profile from HELLO's `model`**, not from the port: `xingzhi-cube-154`
  240x180/15 fps/200000, `ai-passport` 320x180/12 fps/110000 (the multi-tenant
  design's section 7.4 table, as a small built-in table). Old firmware without
  `model` gets the port's profile.
- **Hand-off:** the gate passes the socket and the HELLO bytes to the device's
  worker over a Unix socket (`socket.send_fds`), starting the worker if none
  runs: `python3 -m server.tv_server worker` with the profile's `TV_GEOMETRY`,
  `TV_FPS`, `TV_RATE_START` and the device's file paths. The worker is today's
  `AVServer` taking connections from the gate instead of `accept()`; it stays
  for 10 minutes after the device leaves, so the pending switch and cast return
  channel survive channel changes. Newest connection wins per device.
- **Start-up budget:** the device waits 2 s for CONFIG after HELLO. A worker
  start without numpy is expected well under that; measured in phase S2 on a
  Raspberry Pi 4 as the slowest supported machine. If too slow there, the gate
  keeps one warm spare worker per model.
- **Unbound devices** are answered by the gate itself with the existing still
  frame (`server/tenancy/unbound.py`), no worker, no ffmpeg.
- **Cap:** `max_streams` (0 = none); above it the device gets
  `ERROR {"reason":"server busy"}` and retries. No per-user caps, no admin
  priority (one household).
- **Listening on the phone:** each worker gets its own loopback listen port
  from a pool (8200–8299) and records `{device_id: port}` in
  `/data/run/workers.json`; the relay's `POST /api/listen` answers
  `/tv/listen/<device id>/live.m3u8?t=…` (route = device id instead of the
  model name), and the front proxies that path to the recorded port.
- **Voice:** every worker gets the same `TV_ASR_SOCKET`; the recogniser serves
  one request at a time, which is enough for a household.
- **Not ported:** admission by bandwidth, per-user caps, priority for the
  admin, the separate media host.

### 5.4 Addresses and ports

| Port | Who | Exposed |
| --- | --- | --- |
| 8098 | Xingzhi devices (their build's default) | LAN |
| 8097 | AI Passport devices (their build's default) | LAN |
| 8080 | phone remote, PC console, admin, setup, sound-setup page | LAN |
| 8081 | the same pages minus admin, setup and the key link; for a tunnel only | container network only |
| 8099, 8100 | relay streams and pages | loopback in the container |
| 8200–8299 | per-device listen streams | loopback in the container |
| 47443 | local YouTube node | loopback in the container |

A device's server address is just `<your-host>`: no port, because each build
fills in its own. A device flashed with an older build that stored
`name:8096` keeps 8096 on a `.local` name or IP; the setup page or sound setup
replaces it.

## 6. Networking

### 6.1 The household's address

Devices need an address that does not change. In order of robustness:

1. **A DHCP reservation** for the household server in the router (the tutorial
   has screenshots for the two or three most common router UIs), then the IP.
2. **The machine's `.local` name** (`nas.local`, `my-mac.local`). macOS,
   Windows 10+, Linux with avahi and most NAS systems answer it. On the device,
   lwIP's one-shot mDNS query (`CONFIG_LWIP_DNS_SUPPORT_MDNS_QUERIES`, on by
   default in ESP-IDF) should resolve it through `getaddrinfo()`; upstream's
   README tells users to type `name.local:8096`, but this fork has not verified
   it on either board (section 17). Android browsers do not reliably resolve
   `.local`, so QR codes for phones always use the IP.

How the server learns it: with host networking or the plain-Python path the
supervisor lists the machine's private IPv4 addresses and its `.local` name. In
a bridged container it cannot see the LAN address, so the **first-run page
records the host the browser used** (`Host` header of `/tv/setup`, when it is a
private IPv4 address or a `.local` name) as `device_address`, and the admin can
change it under System. `public_url` defaults to `http://<device_address>:<lan_port>`.

QR codes the device shows (bind, remote, recovery) are built from `public_url`
in upper case and must stay within 64 characters (96 for recovery):
`HTTP://192.168.100.200:8080/TV/R/<20 chars>` is 53. A tunnel host name longer
than about 35 characters does not fit; the admin page warns.

### 6.2 Setting up a device

```
 flash (web flasher, section 9)          device boots into setup (no network stored)
            │                                       │
            ▼                                       ▼
 phone opens http://<your-host>:8080/tv/setup ─► "Set up a TV" ─► /tv/wifi
   (from the banner's QR, or typed)               page: Wi-Fi name, password,
                                                  server = device_address (pre-filled)
                                                  "Send" plays it as sound
            │                                       │
            ▼                                       ▼
 device hears it, joins Wi-Fi, dials <your-host>:8098 (or 8097)
            │
            ▼
 unbound: "bind this TV" still frame with a code
 phone (signed in) scans or types it ─► device bound, plays the owner's channels
```

- **Sound first.** The device's sound page needs no known site: it says "open
  the remote's /tv/wifi on the phone" (`av_player.c`, setup page `SOUND`). The
  page is opened from the household server, so it fills the server field.
  Change needed: the page takes `device_address` from the server (injected into
  the HTML) instead of `location.hostname`, so opening it through a tunnel
  still sends the LAN address.
- **Hotspot as fallback** (the existing pages 2–4 of setup): join the device's
  hotspot, open `192.168.4.1`, type Wi-Fi and `<your-host>`.
- **Web flasher pre-fill** (later, Xingzhi only, section 9.3).
- Binding uses the existing flow (`/TV/B/<claim>`); the first device can be
  bound by the admin who just signed in on the setup page.

### 6.3 The web front and its two ports

The front (Q3) serves the same paths nginx serves on the cloud host
(`packaging/nginx/tv-relay.conf`, `tv-listen.conf`) plus `/tv/setup` and a
static `/` that redirects to the remote. It sets `X-Real-IP` and
`X-Forwarded-Proto` for the relay.

It listens on **two ports** so that "what the internet can reach" is a port,
not a header check:

- **LAN port (8080):** everything.
- **Public port (8081, off by default):** for a tunnel only. Not published on
  the host; the tunnel container reaches it on the compose network (Tailscale
  `serve` points at it the same way). It refuses `/tv/admin`, `/api/admin/*`,
  `/tv/setup`, the key sign-in `?k=`, and self-registration when
  `signup_via_device = "lan"`. It takes the caller's address from
  `CF-Connecting-IP` (Cloudflare) or `X-Forwarded-For` (Tailscale serve) and
  passes it on as `X-Real-IP`, so the relay's per-network limits keep working.

Relay changes for plain HTTP: the session cookie carries `Secure` only when
the request arrived over HTTPS (`X-Forwarded-Proto: https` from the front), and
the relay trusts those headers only from loopback, as it does `X-Real-IP` now.

### 6.4 What needs HTTPS

On a LAN the pages are plain `http://`. Browsers allow some features only in a
**secure context** (HTTPS, or `http://localhost`):

| Feature | Where used | On plain LAN HTTP |
| --- | --- | --- |
| Sign-in cookie (`Secure`) | every page | Works after the cookie change in 6.3 |
| Phone remote, cast, subscriptions, console, admin | all pages | Works |
| Add to Home Screen (iPhone) | remote | Works; the address carries the session (`?s=`), as today |
| Installable web app (Android Chrome, service worker) | remote manifest | Not installable; a home-screen shortcut still works |
| In-page QR scanner (camera, `getUserMedia`) | `scan.js` (sign in a PC, bind) | Does not work; `scan.js` already says so. The phone's own camera app scanning the QR code works; typed codes work |
| Copy buttons (`navigator.clipboard`) | admin, console, remote | Does not work; needs a select-and-copy fallback (small change) |
| Sound setup (`AudioContext`) | `/tv/wifi` | Works |
| Listening on the phone (`<audio>` + HLS) | remote | Expected to work (plain media element) |
| Lock-screen controls (Media Session) | remote, listening | May be missing; some browsers expose it only in a secure context. To verify on an iPhone |
| Web Serial (flashing) | web flasher | Only on the HTTPS static site, or `http://localhost` on the machine with the USB cable |

So HTTPS is **not needed for daily use at home**. It comes with remote access
(6.5), which is where the camera scanner and lock-screen controls matter most.

### 6.5 Reaching the pages from outside

| | Tailscale | Cloudflare Tunnel | Port forward |
| --- | --- | --- | --- |
| Needs | Tailscale on the server and on each phone that uses it | A domain on Cloudflare (free plan), the tunnel container | A public IPv4 at home (often missing: carrier-grade NAT), router access |
| HTTPS | `tailscale serve` gives a valid certificate on `<machine>.<tailnet>.ts.net` | Yes, Cloudflare's | Only with own certificate work |
| Who can open it | Only devices in the tailnet | Anyone with the link (the relay's sign-in protects it) | Anyone |
| From mainland China | Mostly works, often through relays (slower); some networks interfere | Works; latency from China is high; free plan has no mainland PoPs | Ports 80/443 are usually blocked on home lines |
| Listening on the phone | Fine | Audio streaming through the free CDN is within what Cloudflare's terms restrict for "disproportionate" media; keep it occasional | Fine |
| Recommendation | **For the family's own phones**, especially listening | **For shareable links** (remote for a visiting relative, PC console at work) | Not recommended for pages |

Equivalent tools popular in China (ZeroTier, Oray, frp) work on the same
principle; the tutorial names Tailscale and Cloudflare Tunnel and points to
the general idea for the rest. Whichever is used, set `public_url` to it so QR
codes and invite links use it; phones signed in on the LAN address are separate
sessions (cookies are per host), as with the cloud host's two host names.

### 6.6 Devices away from home

The device speaks raw TCP and runs neither Tailscale nor a tunnel client. The
Xingzhi's 4G modem is held in airplane mode by the firmware today
([`xingzhi-cube-154.md`](../../boards/xingzhi-cube-154.md)), so "away" means the
device on a phone hotspot or another Wi-Fi.

| Option | What it takes | Cost and risk |
| --- | --- | --- |
| A. Router port forward of 8097/8098 + DDNS | Public IPv4 at home; `allow_internet = true` | Media port on the internet: the per-device secret (sent in clear) and the gate's failure limits guard it; anyone's device can reach the unbound screen |
| B. frp (or similar) through the user's own VPS | A small VPS; `frpc` beside the server | The VPS carries the stream: about 1.7 Mbit/s per Xingzhi, the same bandwidth problem as the cloud host |
| C. IPv6 direct | Firmware change: dial `AF_UNSPEC` instead of `AF_INET`; an inbound IPv6 rule on the home router | Mobile networks in China mostly give IPv6; home routers often block inbound IPv6 by default. Promising, unverified |
| D. Your cloud host as a relay for strangers | — | Not viable: 3 Mbit/s |
| E. A travel router running Tailscale | The device joins the travel router's Wi-Fi | Works without code; for enthusiasts |

**Recommendation:** none on by default (Q8). Document A and B as advanced
recipes; with `allow_internet = true`, unbound devices from public addresses
get no claim code and the gate refuses them (only bound devices may connect
from outside). Consider C as a later firmware item. Away from home, recommend
audio-only mode: about 0.07 Mbit/s instead of 1.7 (a Xingzhi hour of picture is
about 0.75 GB of mobile data).

## 7. First run and accounts

### 7.1 The first admin

```
banner: setup code + QR ──► phone opens /tv/setup#<code>
  page: "Name" (default: "Admin"), the code pre-filled from the fragment
  POST /api/setup {code, name}
     relay: code matches secrets/setup-code (constant-time), no admin yet
        → create_user(role="admin"), phone session (180 days), audit "user.setup"
        → delete secrets/setup-code; record device_address from Host
  page: "Add to Home Screen", then "Set up a TV" (6.2) and "Add channels" (8.2)
```

- The code is in the URL **fragment**, so it never reaches a server log or a
  proxy; the page sends it in the POST body.
- Five wrong codes per 10 minutes per network, then a wait; the code is 10
  characters, so this is about load, not guessing.
- `/tv/setup` exists only while no admin exists, and only on the LAN port.
- After setup: the PC console signs in by scanning, as on the cloud host;
  `python3 -m server.tenancy.admin login-link <id>` (inside the container with
  `docker compose exec tv …`) stays the break-glass.

### 7.2 Family members

All existing ways, unchanged: "Let another phone sign in" (same account, the
simplest for a family that shares everything), invites (separate accounts),
self-registration by scanning an unbound device (`signup_via_device`), guests
by scanning a bound device's QR code. The tutorial recommends one shared
account per household and the guest QR code for visitors.

### 7.3 Single-household simplifications

No new code paths; defaults and wording only:

- No legacy devices, no shared token, no migration: `meta.legacy_token = 0`,
  and the System page hides the switch when `meta.selfhost = 1`.
- The admin backend's Overview hides the YouTube node panel when YouTube is
  off, and the host curves when there is no `/proc` (a Mac without Docker), as
  it already does.
- The device list names a new device "Xingzhi TV 1", "Xingzhi TV 2" instead of
  the model plus id digits (`default_device_name()`).
- Self-registration's network limit (3 accounts per network per hour) stays;
  on a LAN every phone is one network, which is fine for a family.

## 8. Content and copyright

### 8.1 What ships

Nothing playable. Concretely:

- No `channels.txt` in the image; the live table starts empty.
- `BUILTIN_CHANNELS` is not used in self-host mode (`TV_NO_BUILTIN_CHANNELS=1`,
  set by the supervisor), and `load_channels()` stops looking for a
  `channels.txt` next to the code.
- `DEFAULT_SOURCE` (a community playlist) is not offered; the channel import
  asks for the user's own playlist address or file.
- No Bilibili or YouTube cookies, no YouTube node address or key, no proxy.
- The media server accepts an empty table: with no channel at all, a bound
  device gets a still frame "No channels yet. Add some on your phone." drawn by
  `server/overlay.py` like the bind frame, with silence and no ffmpeg.

### 8.2 Live channels

The admin backend's Channels page (phase 3) already adds, renames, regroups,
reorders, checks and saves rows with validation. Added for self-hosting:
**"Import a playlist"**: paste an M3U address or upload an M3U file; the page
lists its entries (parsed as `tools/channel_config.py` does) with check boxes
and a "check which play" button, then adds the chosen ones. The address is
fetched once, on request, and not stored. The users who may add URLs are the
household's admins, so the multi-tenant design's server-side request concern
(Q5 there) is the household's own business; schemes stay limited to
`http`/`https`.

**Public playlist presets** (decided by the owner 2026-10-22, option B): the
release ships no stream address, but a household should get CCTV and the
provincial satellite channels in one click. The import block therefore lists a
few well-known public, community-maintained playlists by their links only
(`server/relay/presets.py`: name, link, the upstream's own mirror links,
maintainer, homepage; checked by `tests/test_playlist_presets.py`), under the
notice "These are third-party playlists found on the internet; their content is
the responsibility of their maintainers. This project does not provide or vouch
for any live stream; add only what you are entitled to watch." Picking one is
the same as pasting its address (`{preset, mirror}` to the import endpoint,
fetched once, not stored). The list shows each entry's automatic group
(`channel_groups.classify`) with quick filters for CCTV and satellite channels, a search box, select
all / none, "Try" for at most 20 ticked entries on request (never the whole
list), and marks entries whose address or name is already in the table, or
that repeat within the list; those are skipped by default. Adding stops at what
every user's device can hold (an estimate of `config_bytes`); saving checks and
backs up as before. Self-host and PC client only: the cloud relay (no
`--selfhost`) has neither the import nor the presets. `DEFAULT_SOURCE` is not
among them and stays forbidden by the image content check.

### 8.3 Bilibili

Creators and series work as on the cloud host, from a home address that
Bilibili does not refuse with 412. The relay's requests are anonymous (a
`buvid3` cookie it fetches itself); nothing to configure. A logged-in cookie
(for higher quality or member content) is not supported and not planned.

### 8.4 YouTube

Three modes (`[youtube] mode`):

- **off** (default): YouTube links are refused with "YouTube is not set up".
- **node:** a remote node the user runs (`server/relay/ytnode.py` on their own
  overseas VPS, the existing setup), configured with
  `secrets/youtube-node.json` (`url`, `key`, `cert`).
- **local:** `ytnode` runs inside the container on loopback (plain HTTP on
  loopback, no certificate, a new flag), with yt-dlp and deno downloaded on
  first enable, the user's own `cookies.txt`, and an optional HTTP proxy
  (`--proxy` for yt-dlp, `-http_proxy` for ffmpeg; both new). Outside mainland
  China a home address often needs no cookies at all.

The cookies are the user's own account's; the tutorial says to use a secondary
account, export in a private window, and never share the file. `MAX_STREAMS`
stays 2 per node.

**As implemented (S5b, 2026-10-23, branch `desktop-ytlocal`).** The
supervisor starts a fifth child, `ytnode` (`__ytnode` in the frozen core;
`server/relay/ytnode.py --plain --bind 127.0.0.1 --port 47443` or a free port,
`--tools-dir <data>/youtube/bin --remux --no-check`), only when
`mode = "local"`; the relay gets `--ytlocal http://127.0.0.1:<port>` and the
key in `secrets/ytnode-key`, and talks to it through an opener with no proxy
at all (environment and macOS system settings both ignored). The proxy is the
child's `TV_YT_PROXY` (not on the command line): yt-dlp `--proxy`; ffmpeg
`http_proxy` (with loopback in `no_proxy`), or, for `socks5://` /
`socks5h://`, a loopback HTTP CONNECT shim inside the node that tunnels through
the socks proxy. H.264 + AAC formats are remuxed (`-c copy`), others
transcoded as on the cloud node. yt-dlp and deno are looked up in
`components.json` at every call, so installing and updating need no restart;
cookies likewise. `server/selfhost/ytcomponents.py` downloads them (pinned
yt-dlp 2026.10.21 and deno 2.9.8 with SHA-256 from the releases' published
sums; updates of yt-dlp verified against that release's `SHA2-256SUMS`; the
user's proxy or a mirror prefix). The console's Settings → YouTube
(`server/relay/ytsettings.py`, admin only) rewrites `[youtube] mode` and
`proxy` in `config.toml` and drops `run/refresh-request`; the supervisor
re-reads the config and restarts only the children whose arguments changed.
Proxy detection: `scutil --proxy` on macOS, the `Internet Settings` registry
values on Windows, environment variables on Linux and in containers. Checked
on a Mac through a local proxy: the component download (HTTP CONNECT), yt-dlp
with deno reaching YouTube through `socks5h` (without cookies YouTube asked
for sign-in every time), and ffmpeg fetching HTTPS media through the socks
shim. Not run on Windows, Linux or in Docker.

### 8.5 What the tutorial says

- This software plays only what you add. It ships no channels, streams or
  accounts.
- Add only sources you are entitled to watch, for your own household. Do not
  share your server or its streams with people outside it, and do not make it
  public.
- Video sites' terms apply to you when the server fetches their content on
  your behalf; cookies are your own account's and stay on your machine.
- Where YouTube is not reachable, the YouTube settings are for households that
  already have their own way to reach it; this project does not provide one.

### 8.6 Disclaimer wording (suggestion, not legal advice)

This is a suggestion to be reviewed by someone qualified; nothing in this
document is legal advice. The Chinese wording is in the Chinese version of this
document.

> This project is open-source software for private, non-commercial use in
> your own home. It does not include, host, sell or recommend any channel,
> stream, video, account or proxy. Everything it plays is fetched by your own
> server from sources you add yourself, and you are responsible for having the
> right to access them and for following the terms of the services you use and
> the laws where you live. Do not use it to redistribute content to others. The
> software is provided "as is", without warranty of any kind (MIT licence).

## 9. Firmware distribution

### 9.1 Release artifacts

CI (extending `.github/workflows/build-firmware.yml`, which builds the
Passport's public image today) builds and attaches to every release:

| File | Build | Flash |
| --- | --- | --- |
| `Xingzhi-Cube-154-tv-public.bin` | `./tools/validate.sh --xingzhi` | erase once, then write at `0x0` |
| `FoloToy-AI-Passport-tv.bin` | `./tools/validate.sh --prototype` | write at `0x0`, **never erase** (factory identity at `0x356000`) |
| `manifest-*.json` | generated | for the web flasher |
| `SHA256SUMS` | generated | checked by the flasher |

Public builds: no default server, no site, no help URL, no token, no Wi-Fi.

### 9.2 Web flasher

One page on `<static-site>/flash/`, built on esptool-js (upstream's flasher
already uses esptool-js 0.6.1 with a write-range check;
`docs/flash/index.html`). Chrome or Edge on a desktop only (Web Serial); the
page says so on other browsers and on phones.

- Detects the chip: ESP32-S3 offers the Xingzhi image, ESP32-C3 the Passport
  image; anything else is refused.
- **Xingzhi:** explains that this replaces the Xiaozhi AI firmware (and links
  to Xiaozhi's own flasher to go back), asks for an explicit tick, erases the
  chip, writes, resets. The BOOT-key download-mode hint is shown when the chip
  does not answer.
- **Passport:** never erases; keeps upstream's check that the write range ends
  before `0x356000`, and says how to restart the device (battery: unplugging
  does not reset it).
- Verifies the file against `SHA256SUMS` before writing.
- After flashing: "Now open your household server's setup page and choose
  'Set up a TV'".
- The household server can also serve a copy of the flasher at
  `http://localhost:8080/flash/` for the machine the device is plugged into
  (`localhost` counts as a secure context); the firmware files then come from
  the image itself. Useful when the static site is slow.

### 9.3 Server address: baked in or set afterwards

| Way | For |
| --- | --- |
| **Set after flashing** by sound or the hotspot page (6.2) | Everyone; the default |
| Pre-filled by the flasher: it writes a small NVS partition at `0x9000` holding the server address (key `wifi/ota_url`, the same entry `tools/set_server_addr.py` edits) | Xingzhi only, because it is erased anyway; later (phase S6) |
| `CONFIG_AV_DEFAULT_SERVER` baked into a build | The owner's friends' builds only (Q11); not for public builds |

The NVS pre-fill must never run on a Passport: its NVS holds the player's
settings and, beside Muse, Muse's pairing.

### 9.4 AI Passport beside Muse

The `ota_1` image must be signed with Muse's key (`CONFIG_SECURE_SIGNED_ON_UPDATE`)
and written to `0x400000` only, never with this build's bootloader or partition
table ([`passport-muse-dual.md`](../../boards/passport-muse-dual.md)). The web
flasher can do that only if a signed image can be published, which depends on
whether Muse's signing key may be used in CI (Q9). Until then the tutorial
links to the board document and marks it advanced.

### 9.5 Firmware updates

Now: re-flash from the web flasher without erasing; device id, secret, Wi-Fi
list and settings in NVS survive, so the device stays bound. The admin
backend's Devices page shows each device's `fw` from HELLO against the version
the release ships, with a "how to update" link.

Later (Q10), OTA from the household server: a new Xingzhi partition table with
two app slots (8 MB flash has room; one erase for everyone), the device fetching
the image from the household server over HTTP, and an application-level
signature check against a project public key compiled into the firmware (the
link is plain HTTP and public builds have no secure boot). Not worth it before
there are users who would update.

## 10. Updates, backups and logs

- **Tags:** `1.2.3`, `1.2`, `1`, `latest`, and `edge` from `main`. The
  tutorial uses `:1`. A major version changes only when an update needs manual
  steps.
- **Updating:** `docker compose pull && docker compose up -d`; on a NAS,
  "re-pull image" and restart. No automatic updates by default (Watchtower is
  mentioned, not configured).
- **Database:** schema changes are already applied by the relay on start
  (`ALTER TABLE … ADD COLUMN`, older code keeps working with the upgraded file).
  The supervisor adds a backup before starting a version whose schema number
  differs from the database's (`tv-pre-<version>.db`).
- **Firmware compatibility:** the server keeps answering older firmware by its
  HELLO flags, as today; release notes state the oldest firmware supported.
- **Backups:** nightly `sqlite3 .backup` (Python's `sqlite3.Connection.backup`,
  no CLI needed) into `backups/`, kept `backup_keep_days`. Admin → System gets
  "Download a backup" (database copy + `secrets/` + `channels.txt` +
  `config.toml` as one archive, with a warning that it contains secrets).
  Restore: stop, replace `/data` with the archive's contents, start.
- **Logs:** every process logs to stdout with a short prefix (`relay:`,
  `gate:`, `worker <id>:`); Docker's own rotation keeps them small. The rules
  stay: no tokens, secrets, session ids or full IP addresses; device ids are
  fine; `TV_VOICE_LOG_TEXT` off. The one exception is the setup code in the
  banner, single use and gone once the admin exists. Later: the last 200 lines
  on the admin System page.
- **Health:** `GET /healthz` on the front (relay answering, media ports
  listening) for Docker's `HEALTHCHECK` and NAS status lights.

## 11. The static website

Hosted per Q5; Chinese first, English beside it. No build step, no
third-party requests (like `packaging/site/` today).

| Page | Contents |
| --- | --- |
| Home | What it is, what you need (a supported device, a computer or NAS that stays on, a USB cable, Chrome or Edge once), screenshots from `tools/render_ui/site_shots.py` |
| 1. Install the server | Docker on Linux / mini PC; NAS (Synology, UGREEN, QNAP); Mac (Docker or plain Python); Windows (Docker Desktop) |
| 2. First run | The banner, the setup code, making the admin, Add to Home Screen |
| 3. Flash a device | The web flasher; manual esptool commands; erase only for Xingzhi |
| 4. Set up a device | Sound setup, hotspot fallback, binding, fixed IP / `.local` |
| 5. Add things to watch | Live channels (import your playlist), Bilibili creators and series, YouTube (optional, Q6) |
| 6. Everyday use | Buttons, phone remote, voice (optional), listening on the phone |
| 7. Outside the home (optional) | Tailscale, Cloudflare Tunnel; devices away from home (advanced) |
| 8. Updates and backups | Image updates, firmware re-flash, backup and restore |
| FAQ / troubleshooting | Firewall, ports in use, NAS permissions, mirror pulls, "server did not answer", "server busy" |
| Disclaimer | Section 8.6 |
| `/flash/` | The web flasher |
| `latest.json` | Version info for the update check (Q12) |
| Downloads | Links to GitHub Releases (firmware, offline images); registry addresses |

The existing help page (`packaging/help/index.html`, the device's QR target) is
served by the household server itself at `/TV/`, so the device's help QR works
on the LAN too.

## 12. Security

| Topic | Design |
| --- | --- |
| Exposure | LAN only by default: 8080, 8097, 8098. The public port 8081 is not published on the host; only a tunnel container reaches it |
| Admin | Closed on the public port unless `admin_over_tunnel = true`; role checks in the relay as today |
| First admin | One-time setup code (7.1), LAN port only, rate-limited, audited |
| Break-glass key link (`?k=`) | LAN port only by default; the key is generated, never printed |
| Shared tokens | None: no token file, `meta.legacy_token = 0`, public builds carry none |
| Devices | Per-device secret, trust on first use, re-pair window (unchanged). `allow_internet = false` keeps the media server's private-address rule; when true, only bound devices may connect from public addresses |
| Self-registration | Only from the LAN port by default (`signup_via_device = "lan"`) |
| Rate limits | The relay's existing per-network limits (tickets, phone links, signups, wrong codes), fed with the real caller address by the front on both ports |
| Cookies | `Secure` when HTTPS, `HttpOnly; SameSite=Lax; Path=/tv/` always; same-origin check on every POST (existing) |
| Pages | Existing CSP without external sources, `frame-ancestors 'none'` |
| Container | Unprivileged user, read-only root filesystem except `/data` and `/tmp`, all capabilities dropped, no Docker socket |
| Plain-text device link | Unchanged on the LAN; with port forwarding the device secret crosses the internet in clear (challenge-response is a later firmware item, multi-tenant design 8.1) |
| User-supplied URLs | Only admins add live URLs and playlists; `http`/`https` only |
| Secrets at rest | `secrets/` 0700, backups warn they contain secrets; the database holds hashes only (existing) |

## 13. Resources and supported hardware

### 13.1 Per stream and per machine

Measured on the cloud host (multi-tenant design 7.6): a Xingzhi stream is up to
200 kB/s of picture plus about 8 kB/s of sound, **about 1.7 Mbit/s**; a
Passport stream **1.0–1.2 Mbit/s**; audio-only **about 0.07 Mbit/s**; ffmpeg for
one Xingzhi stream about 12 % of one core on the 2-core host.

| Quantity | Value |
| --- | --- |
| CPU per video stream | **Measured (M1 Pro): about 0.15 of a core** on a 720p source (0.07–0.09 on 360p or low motion, about 0.18 on 1080p). Two thirds is ffmpeg decoding the source; the Python sender is 0.04–0.07 regardless. A modern x86 core is estimated similar; a 2-vCPU cloud VM 0.2–0.3 vCPU |
| CPU per video stream, arm64 (Raspberry Pi 4/5) | **Estimated**: Pi 5 about 0.4 of a core, Pi 4 about 0.8–1 (scaled by single-core performance, not measured) |
| Extra cost of a listening phone | **Measured**: about 0.02 core and 20 MB more per stream (a small AAC ffmpeg). An audio-only session still decodes video today (multi-tenant design 7.6), so it costs the same as video |
| RAM per active worker | **Measured 50–70 MB** (Python 24–26 MB + ffmpeg 15–45 MB, more for higher source resolution); a media server with no device about 23 MB |
| RAM, idle base (supervisor, front, relay, gate) | relay **measured** about 22 MB and near-zero CPU; supervisor, front and gate **expected** under 100 MB together |
| RAM, voice container | about 550 MB (measured for `muse-asr`) |
| RAM, local YouTube node | about 330 MB peak for yt-dlp with deno (measured on the node) plus ffmpeg per YouTube stream |
| LAN bandwidth per stream | 1.7 Mbit/s Xingzhi, 1.0–1.2 Passport (measured); the device's 2.4 GHz Wi-Fi is the real limit |
| Internet download per stream | The source's own bitrate, typically 1–6 Mbit/s (HLS 360p–1080p). Two devices on the same channel each pull a copy (a gate could share it later) |
| Worker start-up (gate-lite) | **TBD**, measured in phase S2; must stay well under 2 s |
| Disk | Image: expectation 250–350 MB. Data: under 50 MB plus backups |

### 13.2 Devices per machine

Per-stream cost was measured on an M1 Pro; the saturation sweep was stopped part-way, so every row except the Mac is **estimated** by single-core performance ratio, and the Mac row awaits the saturation run. Phase S0 adds one real run each on a Raspberry Pi and an N100.

| Machine | Concurrent video streams (estimated) | Notes |
| --- | --- | --- |
| Intel N100 / N97 mini PC, 8 GB | about 10 | **Recommended**: 6–10 W idle, x86-64 |
| Old laptop, Core i5 4th gen or newer, 4 GB+ | 3–4 (dual core) to 6+ (quad core) | Lid closed without sleeping; Linux or Windows + Docker Desktop |
| Mac, Apple silicon | M1 Pro about 30 (awaiting saturation run) | Docker Desktop or plain Python; must not sleep |
| Synology / QNAP x86 NAS (J4125, R1600) | about 4–6 | Container Manager / Container Station |
| UGREEN DXP (N100) | about 10 | Docker app |
| Raspberry Pi 5, 4–8 GB, 64-bit OS | about 6 | Experimental until measured |
| Raspberry Pi 4, 4 GB+, 64-bit OS | about 2 | Experimental until measured |
| ARM NAS (Realtek RTD1296/1619B) | 0–1 | Not recommended |
| 2-vCPU cloud VM (reference) | 2–3 | Bandwidth usually runs out first (1.7 Mbit/s outbound per stream) |
| Raspberry Pi 3, Zero 2, 32-bit OS, routers | — | Not supported |

Voice adds 1 GB of RAM to the minimum; the minimum without voice is 2 GB.

## 14. Code changes

| Area | Files | Change |
| --- | --- | --- |
| Supervisor and config | new `server/selfhost/` (`__main__.py`, `config.py`, `bootstrap.py`, `banner.py`) | Config file, data layout, secrets, DB init, setup code, starting and restarting children, nightly backup, pre-upgrade backup, banner with terminal QR (reuses `server/relay/qr.py`) |
| Web front | new `server/front.py` | Two ports, routes of `tv-relay.conf`/`tv-listen.conf`, per-device listen routing from `run/workers.json`, `X-Real-IP` / `X-Forwarded-Proto`, tunnel headers on the public port, `/healthz`, static help and flasher copy |
| Relay | `server/relay/__main__.py`, `server/relay/web.py` | "Setup pending" mode instead of exiting; `/tv/setup` page and `POST /api/setup`; cookie `Secure` only over HTTPS; trust `X-Forwarded-Proto` from loopback; listen route per device; select-and-copy fallback in pages without `navigator.clipboard` |
| Accounts | `server/tenancy/accounts.py`, `server/tenancy/signup.py`, `server/tenancy/gate.py` | First admin from the setup code; `signup_via_device` policy; refuse unbound devices from public addresses when `allow_internet`; `meta.selfhost`; device naming |
| Pages | `server/relay/assets/wifi.html`, new `setup.html`, `admin.html` | Server field from `device_address`; first-run page; System shows config and backups; playlist import on Channels; firmware version column |
| Media server | `server/live.py`, `server/tv_server.py`, `server/overlay.py` | Empty table allowed, "no channels yet" still frame; `TV_NO_BUILTIN_CHANNELS`; no `channels.txt` lookup next to the code in self-host mode; worker mode (S2) |
| Gate-lite (S2) | new `server/gate.py` (process gate, distinct from `server/tenancy/gate.py`) | Listeners, HELLO, profiles, fd hand-off, worker start and idle exit, cap, unbound frame, worker registry |
| YouTube | `server/relay/ytnode.py`, `server/relay/youtube.py` | Plain HTTP on loopback; `--proxy` for yt-dlp and ffmpeg; tools downloaded into `/data/youtube/bin` |
| Voice (optional) | new `packaging/asr/` (Dockerfile, a ~150-line server) | `POST /asr` on a Unix socket, sherpa-onnx SenseVoice, model download on first start |
| Packaging | new `packaging/docker/` (Dockerfile, `compose.yaml`, `.dockerignore`, `NOTICE`), `run.sh` (`--selfhost`) | Image, compose, plain-Python entry |
| CI | `.github/workflows/` new `build-image.yml`; `build-firmware.yml` | Multi-arch image to GHCR and the mainland registry, offline archives, content check; Xingzhi public build as a release artifact, manifests and checksums |
| Static site | `packaging/site/` (tutorial pages), `docs/flash/` (flasher) or a new `packaging/flash/` | Tutorial, flasher for both chips, `latest.json` |
| Firmware | none required for S1–S5; optional later: `main/av_player.c` (`AF_UNSPEC` for IPv6), partitions and OTA (Q10) | — |
| Tests | `tests/test_selfhost_*.py`, `tests/test_front.py`, `tests/test_gate.py`, extensions to relay tests | Bootstrap, setup code, two-port rules, cookie flags, listen routing, empty table, gate hand-off with a simulated device (`tools/device_sim.py`) |
| Docs | `server/README.md`, `docs/development/host-setup.md` (pointer), new `docs/development/selfhost.md` (operator notes) | — |

## 15. Phased plan

Efforts in agent-days (one agent with host tests), excluding your review and
on-device checks.

### Phase S0 — decisions and numbers (0.5 day)

Answer Q1–Q12. Finish the M1 Pro saturation sweep and run once each on a
Raspberry Pi and an N100 to replace the **estimated** figures in section 13.

### Phase S1 — a household server that works, one device per model (5–7 days)

Ships: image and compose, plain-Python path, first-run setup, empty start,
playlist import, LAN pages over HTTP. Suitable for a 0.x beta.

- S1a. `server/selfhost/`: config, data layout, secrets, DB init, setup code,
  banner, supervisor, backups. (1.5–2 days)
- S1b. `server/front.py` with both ports, header rules, `/healthz`; relay
  cookie and setup-pending changes; `/tv/setup`. (1.5–2 days)
- S1c. Content: empty table, still frame, no built-in channels, playlist import
  in the admin Channels page, `wifi.html` server field. (1–1.5 days)
- S1d. Dockerfile, compose, `.dockerignore`, CI multi-arch build and content
  check, `run.sh --selfhost`. (1–1.5 days)

Parallel: S1a and S1c are independent; S1b needs S1a's config shape only.

**S1 status (2026-10-16): implemented**, one device per model (Q1 option A); the
quick start is [`docs/selfhost/README.md`](../../selfhost/README.md). Verified on
a Mac through the plain-Python path (`./run.sh --selfhost`, bound to
127.0.0.1): banner and setup code, first admin through `/tv/setup`, the cookie
without `Secure` over plain HTTP, playlist import and probe, saving the table,
`/healthz`, the unbound frame and the empty-table frame with a simulated
device, clean stop. Written and checked by reading only: the Dockerfile,
compose file and CI workflow (no Docker on the development machine), the NAS
and Windows steps. Every self-host behaviour is behind the relay's
`--selfhost` flag or `TV_NO_BUILTIN_CHANNELS=1`, which only the supervisor
sets; the cloud units run as before. Where it differs from the text above:

- Docker files are in `packaging/docker/`; `.dockerignore` is at the repository
  root and is an allow-list. The built-in channels moved to
  `server/builtin_channels.py`, which the image leaves out; the image has no
  `tools/` (so no `DEFAULT_SOURCE` either).
- `PUID`/`PGID` become compose's `user:` rather than a root entry point that
  drops privileges, so the container keeps `cap_drop: ALL`.
- The plain-Python data folder is `<per-user data dir>/selfhost` (never the
  checkout, where `tools/datadir.py` would put it when writable).
- The relay ports (8099, 8100) and listen ports (8198, 8199) are fixed; the
  config file adds `[server] bind` and `[devices] xingzhi_port` /
  `passport_port`. `max_streams` and `update_check` are accepted but do
  nothing yet (`[youtube] proxy` and local YouTube mode: S5b, section 8.4).
- The image is pushed only by a manual run of the workflow with "publish"
  ticked; Debian's ffmpeg is not pinned to a version yet.
- Integrated with S3 (2026-10-16): the front serves `packaging/flash/` at
  `/flash/` and the chosen firmware at `/flash/firmware/`, on both ports (static
  files that change nothing). The firmware is the data folder's `firmware/`
  (filled by `./run.sh --fetch-firmware`, which installs the latest release's
  `xiaozhi-tv-flasher.zip` through `tools/firmware_manifest.py install`, or by
  unpacking that zip there), else the copy the image workflow puts into the
  image. The relay gets `TV_FIRMWARE_MANIFEST` and `TV_FLASHER_URL=/flash/`.
  Web Serial needs a secure context, so the banner and the tutorial send people
  to `http://localhost:<lan_port>/flash/` on the server machine itself; the LAN
  address cannot flash. Checked on a Mac: the page loads from the plain-Python
  server, and the admin Devices page shows the firmware column with a sample
  manifest.
- Left for later: the System page's config view and backup download, the
  select-and-copy fallback, "Xingzhi TV 1" device names, the update check,
  refusing unbound devices from public addresses when `allow_internet`.

### Phase S2 — several devices at once (gate-lite) (3–4.5 days)

Ships: one address, every device at once up to the machine. With S1: version
1.0.

- S2a. Gate: listeners, HELLO, profiles, hand-off, cap, unbound frame. (1.5–2 days)
- S2b. Worker mode in `AVServer`, idle exit; the existing `tests/test_tv_server.py`
  stays green. (1–1.5 days)
- S2c. Per-device listen ports, registry, relay route, front routing. (0.5–1 day)

Parallel: S2a and S2b against an agreed hand-off interface.

### Phase S3 — firmware distribution and web flasher (2–3 days)

- S3a. CI: Xingzhi public build as a release artifact, manifests, checksums.
  (0.5 day)
- S3b. Flasher page for both chips: detection, erase-first with consent for
  Xingzhi, never-erase range check for the Passport, checksums, help text;
  `localhost` copy served by the household server. (1.5–2 days)
- S3c. Firmware version column and "how to update" in the admin backend. (0.5 day)

Can run in parallel with S1 and S2.

**Status (2026-10-16): built, not yet tried on a device or run in CI.**

- S3a: `.github/workflows/build-firmware.yml` builds both public images in a
  matrix (`--prototype` for the Passport, `--xingzhi`), versioned from
  `git describe --tags` (`AV_FW_VERSION` → `PROJECT_VER`), and publishes them
  with `manifest.json`, `manifest-<board>.json`, `SHA256SUMS` and
  `xiaozhi-tv-flasher.zip`. `tools/firmware_manifest.py` writes and checks the
  manifests; `check-public`, run by `validate.sh`, refuses a build without
  `AV_PUBLIC_BUILD` or with a default server or site (Q11).
- S3b: the flasher is `packaging/flash/` (chosen over `docs/flash/`, which stays
  the old Passport-only GitHub Pages page). It reads `firmware/manifest.json` and
  `firmware/SHA256SUMS` from its own origin, because release downloads have no
  CORS header. The board rules (erase, write limit, protected range) are
  repeated in the page's own table and win over the manifest. Detection reads
  `0x8000` and `0x10000`: a Xingzhi already running this firmware is offered an
  update without erase (9.5), anything else is erased after a tick; a Passport
  is never erased, and one carrying the Muse dual setup is refused. Files are
  checked against both the manifest and `SHA256SUMS`, the write is verified by
  MD5 read-back. esptool-js 0.6.1 is vendored with its licence.
  **For S1's front** (done, see S1's status): serve `packaging/flash/` at `/flash/` with the release's
  `manifest.json`, `SHA256SUMS` and images in `/flash/firmware/` (the layout
  `tools/firmware_manifest.py bundle` produces), and set
  `TV_FIRMWARE_MANIFEST=<that manifest.json>` and `TV_FLASHER_URL=/flash/` for
  the relay. The static site (S4) unzips `xiaozhi-tv-flasher.zip` at its root.
- S3c: the admin Devices page tags devices whose `fw` differs from the
  release's version for their model and explains how to update, linking
  `TV_FLASHER_URL` when set.
- Q9, Muse dual in CI: the signing key is Muse's public, shared development
  key (`esp32/dev_signing_key.pem` in facebookincubator/muse-gadget-sdk, which
  says to replace it before shipping), so CI could sign without a secret; not
  done, see [`passport-muse-dual.md`](../../boards/passport-muse-dual.md#web-flasher-and-published-images).

### Phase S4 — static website and tutorial (2.5–3.5 days)

Chinese first, English beside it: the pages of section 11, disclaimer,
screenshots, NAS and Windows walkthroughs (written from documentation, marked
unverified until tried). Can start once S1's commands are fixed.

### Phase S5 — optional pieces (3–4.5 days)

- S5a. Voice container (sherpa-onnx SenseVoice, `POST /asr`). (1.5–2 days)
- S5b. YouTube local mode: loopback HTTP, proxy, tool download and update. (1–1.5 days) — implemented
  2026-10-23 (section 8.4).
- S5c. Remote-access recipes (Tailscale serve, Cloudflare Tunnel compose
  profile) and the away-from-home recipes; tested on one network. (0.5–1 day)

Independent of each other.

### Phase S6 — beta hardening (2–3 days)

End-to-end on real machines (an N100 or x86 laptop, a Mac, a Synology or UGREEN
NAS if available, a Raspberry Pi 4/5), with real devices on your side; backup
and restore drill; update from the previous tag; the flasher's NVS pre-fill for
Xingzhi if wanted (Q10/9.3, +1 day).

**Total: about 18–25 agent-days**; a beta (S1 + S3 + a short S4) after about
10–12, version 1.0 (adding S2, S4, S6) after about 16–20, with the optional
pieces on top.

## 16. Risks

| Risk | Effect | Mitigation |
| --- | --- | --- |
| Load-test numbers lower than expected on small machines | Pi and ARM NAS users get stutter | Publish per-machine guidance from measurements; `max_streams`; audio-only uses less once it stops decoding video |
| Docker Hub / GHCR unreachable from mainland China | Install fails at the first command | Mainland registry mirror; offline archives (Q4) |
| `*.pages.dev` / GitHub unreachable from mainland China | Tutorial or flasher does not load | Custom domain; firmware also served by the household server's `localhost` flasher |
| `.local` not resolved by the device | Typed name does not connect | Tutorial leads with a DHCP reservation and the IP; verify lwIP's mDNS query on both boards |
| Plain HTTP limits (camera scanner, clipboard, lock-screen controls) | Some conveniences missing at home | Typed codes and the phone camera; select-and-copy fallback; HTTPS through Tailscale for those who want it |
| Users expose the media ports | Strangers' devices reach the unbound screen; secret in clear | Off by default; bound-only from public addresses; recipes marked advanced |
| Content: users share their servers publicly | Legal exposure for the users; reputational for the project | No content shipped; disclaimer; no public directory of servers; the package has no "share with everyone" switch |
| YouTube advice read as circumvention | Problems for the project in mainland China | Generic proxy setting, no instructions (Q6) |
| Voice model licence | Bundling may not be allowed | Download on first enable after showing the licence; check before S5a |
| ffmpeg GPL in a distributed image | Licence compliance | Debian package with source pointers; `NOTICE` |
| Gate-lite worker start-up on slow ARM | First connection after idle times out | Measure in S2; warm spare worker per model |
| Firmware updates by hand | Users stay on old firmware | Server keeps old HELLO flags working; version column in the admin backend; OTA later (Q10) |
| One more way of running the same code | Drift between the cloud host and the package | Same `server/`; self-host mode is configuration (`meta.selfhost`, supervisor), covered by tests in CI |

## 17. Not verified

- Every figure marked **estimated** or **expectation**.
- That the device resolves `.local` names (lwIP one-shot mDNS query, default
  on in ESP-IDF 5.5) on either board.
- Browser behaviour on plain HTTP: that Safari also drops a `Secure` cookie
  set over `http://` (Chrome and Firefox do), and whether Media Session works
  on an iPhone over plain HTTP.
- Worker start-up time on a Raspberry Pi; Debian's ffmpeg with the project's
  arguments on arm64.
- Docker Desktop (Mac, Windows) LAN reachability of published ports with the
  default settings; Synology Container Manager and UGREEN UGOS behaviour;
  `PUID`/`PGID` on those systems.
- Reachability from mainland China today of `*.pages.dev`, GHCR, Docker Hub,
  Tailscale's relays and the Alibaba Cloud personal registry's free tier terms.
- Cloudflare's current terms for audio through a free tunnel; Tailscale's
  free-plan limits.
- SenseVoice-Small's model licence for redistribution.
- Whether Muse's signing key may be used to sign a published Passport image.
