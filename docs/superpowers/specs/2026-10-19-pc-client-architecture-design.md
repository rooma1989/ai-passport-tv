<p align="right">
  <a href="2026-10-19-pc-client-architecture-design.zh_CN.md">简体中文</a> · <strong>English</strong>
</p>

# PC client, phone remote and cloud remote channel — architecture design

Status: draft for decision, 2026-10-19. Design only; nothing here is
implemented. This document **supersedes the direction** of
[`2026-10-15-self-hosting-design.md`](2026-10-15-self-hosting-design.md): the
household server stops being "Docker first" and becomes a desktop program for
Windows and macOS (Linux as a bonus) built from the same core. Phases S1 and S3
of that document are implemented and are reused unchanged; its findings (plain
HTTP cookies on a LAN, the two front ports, the content-free rule, the
secure-context table, the gate-lite) still hold and are referred to, not
repeated. Also follows
[`2026-10-12-multi-tenant-design.md`](2026-10-12-multi-tenant-design.md)
(accounts, binding, sessions, admin; phase 4 media gate not built),
[`2026-10-12-phone-audio-design.md`](2026-10-12-phone-audio-design.md),
[`2026-10-12-voice-control-design.md`](2026-10-12-voice-control-design.md),
[`2026-10-13-wifi-setup-design.md`](2026-10-13-wifi-setup-design.md) and
[`2026-10-13-sound-provisioning-design.md`](2026-10-13-sound-provisioning-design.md).

The request: the owner does not want to carry everyone's video on his own
server, for cost and for risk. The product becomes three parts that run in the
user's home, plus an optional server the owner controls:

1. **Firmware** on the devices (Xingzhi Cube 1.54, ESP32-S3; AI Passport,
   ESP32-C3). Done: public builds, release workflow, web flasher.
2. **The PC client**: a program on a Windows or Mac computer at home that pulls
   the content, transcodes it and streams it to the devices on the home LAN.
3. **The phone remote**: the existing web remote, served by the PC client on
   the LAN, and reachable from outside the home through a small **cloud remote
   channel** on the owner's server that carries only remote-control messages.
4. **Optional server**: the owner's existing cloud host with accounts, for the
   few people he chooses who have no PC, with a validity period and limits.

Names used below: **the PC client** (or "the client") is the desktop program;
**the core** is the Python program it runs (today's `server/selfhost`
supervisor and its children); **the hub** is the cloud remote channel's
service on the owner's host; `<public-domain>` is the owner's public HTTPS
domain; `<owner-server>` is the owner's cloud host; `<static-site>` is the
project's static website. Figures marked **estimated** are not measured.

## Already decided with the owner (not reopened here)

- PC client **option A first**: the existing Python core packaged with
  PyInstaller, a system-tray icon, the existing web pages opened in the default
  browser at `localhost`. **Option B** (a Tauri shell around the same web UI
  with the core as a sidecar) is the later upgrade; A must be designed so that
  B needs no core changes (section 5.13).
- Windows and macOS both matter; Linux is a bonus.
- Video never crosses the owner's server for a household that has a PC. A
  device away from home uses an optional server account instead.
- Homes without a public IP must get phone control from outside without port
  forwarding, through **our own cloud remote channel**: the PC keeps one
  outbound connection to the owner's server; the phone opens the public site;
  the server forwards commands to that household's PC.
- Voice recognition (SenseVoice, about 550 MB of RAM) runs locally on the PC as
  an optional download.
- The client can flash firmware itself (bundled esptool), without the browser's
  Web Serial limits.
- Sleep is prevented while a device is watching.
- ffmpeg is bundled or downloaded on first run, with attention to LGPL versus
  GPL builds.
- Unsigned for the beta, with a tutorial for macOS Gatekeeper and Windows
  SmartScreen; an Apple Developer ID and a Windows code-signing certificate
  before a public release.
- YouTube needs the user's own proxy or node; no instructions for obtaining one.

## 1. What moves where

### 1.1 Today

The owner's cloud host carries everything for everyone it serves: the relay
with accounts, the media servers (one per board model), all video at about
1.7 Mbit/s per Xingzhi out of 3 Mbit/s, the phone remote, the admin backend,
voice recognition, the YouTube node link. Self-hosting households (the
v0.1.0-beta) run the same core at home through Docker or `./run.sh --selfhost`,
which needs a technical person.

### 1.2 After this design

| Piece | Today | After |
| --- | --- | --- |
| Transcoding and streaming to devices | Owner's host (or a household's Docker) | **PC client at home**; owner's host only for optional accounts |
| Fetching sources (Bilibili, live, YouTube via the user's node) | Owner's host | PC client, from a home address (no Bilibili 412 for cloud addresses) |
| Phone remote on the LAN | Owner's host, or the household server | PC client (`http://<pc>:8080/tv/`) |
| Phone remote from outside | Owner's host, or a tunnel the household sets up | **Hub** on the owner's host: forwards small encrypted API calls to the PC; pages are static |
| Listening on the phone (HLS audio) | Owner's host | PC client, **LAN only** (Q3) |
| Accounts, devices, progress, subscriptions | Owner's host database | PC client's own database (one household); owner's host only for optional accounts |
| Voice recognition | Owner's host (`muse-asr`) | PC client, optional local download |
| Firmware flashing | Web flasher on `<static-site>` or the household server's `localhost` copy | Same, plus built-in flashing in the client |
| Admin backend | Owner's host (all users) | Each PC client has its own (one household); the owner's `/tv/admin` manages optional accounts and hub households |
| Static website, tutorial, downloads, `latest.json` | `<static-site>` | Same; installers also on a mainland mirror |

What the owner's host carries afterwards: the hub (idle connections plus a
few kB per remote command), static remote pages, and the optional accounts'
streams, capped by its 3 Mbit/s.

## 2. Goals and non-goals

Who uses it: non-technical families, with one person who installs a program
once (download, double-click, allow it past the OS warning) and flashes the
devices. No terminal, no Docker, no router settings.

Goals:

1. **Install like an app** on Windows 10/11 and macOS 12+ (Apple silicon and
   Intel): an installer or a `.dmg`, an icon in the tray or menu bar, autostart
   at login.
2. **First run in the browser**: the client opens `http://localhost:<port>/tv/setup`
   with the setup code already filled in; the admin is created without
   reading a log.
3. **Same core as the Docker image and the cloud host**: the client is a way of
   running `server/`, not a fork. Docker and plain Python stay for NAS and
   headless users.
4. **Phone control from anywhere** without port forwarding, without the owner's
   server seeing what is watched (end-to-end encryption, section 7.6).
5. **Several devices at once** on one PC (gate-lite, S2 of the self-hosting
   design), cross-platform.
6. **Self-contained**: ffmpeg, flashing and (optionally) voice come with the
   client or are fetched by it with pinned hashes.
7. **No content shipped**, no telemetry, no shared secrets.
8. **Option B ready**: everything the tray does goes through a local control
   API, so a Tauri shell can replace the tray without core changes.

Non-goals:

- Video or listening audio through the owner's server for PC households.
- An app-store app (Mac App Store, Microsoft Store) or a mobile app.
- Running as a Windows service or a macOS launch daemon before login (the
  client runs in the user's session; NAS and headless users keep Docker).
- 32-bit Windows, Windows on ARM as a supported target (it runs the x64 build
  under emulation, untested), macOS older than 12.
- Changing the device wire protocol, the rate controller or timing code.
- Peer-to-peer (WebRTC) between phone and PC in the first release (section 7.11).

## Decisions (2026-10-19)

The owner accepted every recommendation (Q1–Q15 as recommended). `v0.2.0-beta` comes first: P0's small trials, P1, P2, P3a and part of P10 (the PC client, one device per model, LAN remote).

## 3. Questions for you to decide

Each has options and a recommendation; the rest of the document assumes the
recommendations.

### Q1. End-to-end encryption between phone and PC in the first remote release?

| Option | Consequence |
| --- | --- |
| **A. Yes, from the start** | 2.5–3.5 agent-days more (section 7.6). The hub sees routing ids, sizes and timing only; never session tokens, titles, subscriptions, or the Wi-Fi passwords the remote can send to a device. You can truthfully say you only route ciphertext. Limitation: the page's JavaScript comes from your server, so a compromised server could serve a page that leaks the key; E2E protects against logs, database leaks and casual access, not against a malicious operator. |
| B. TLS to the hub only | Faster to build. The hub decrypts everything: every household's session tokens and Wi-Fi passwords pass through your server in clear, and any log or bug exposes them. Turning E2E on later means re-pairing every phone. |

**Recommendation: A.** Without it the Wi-Fi management feature would have to be
disabled remotely, and your server would hold credentials for every household.

### Q2. Who may connect a PC to the hub?

| Option | Consequence |
| --- | --- |
| **A. Beta: an invite code from `/tv/admin`; public release: open, with quotas** | You see who uses the hub while it is small; strangers cannot fill it. At public release, open registration with per-household quotas and a per-network registration limit (section 7.9). |
| B. Open from the start | Anyone with the client can register. The hub carries no content and no HTML from households, so abuse is limited to burning quota, but you would not know who is on it. |
| C. Always invite only | Every household needs to ask you; does not scale past friends. |

**Recommendation: A.**

### Q3. Listening on the phone away from home?

| Option | Consequence |
| --- | --- |
| **A. LAN only** | Nothing changes on the hub. Outside the home the remote works, and the listen button explains "only at home". |
| B. Allowed with limits through the hub | About 4–5 kB/s per listening phone (32 kbit/s AAC) through your 3 Mbit/s, so about 60 phones would fill it; it is content (audio) passing through your server, which breaks "the server never carries content"; and HLS cannot be decrypted by the `<audio>` element, so it would be outside E2E. |

**Recommendation: A.**

### Q4. One home-screen icon or two?

The public remote page (`https://<public-domain>/r/`) cannot call the PC's
plain-HTTP LAN address (mixed content and the browser's private-network rules),
so a phone using it always goes through the hub, even at home.

| Option | Consequence |
| --- | --- |
| **A. One icon, the public page, always through the hub** | Simplest for a family. Each foreground remote costs your host about 0.1–0.8 kB/s (section 7.10). The LAN page stays available (QR on the device, PC console). |
| B. Two icons, "at home" (LAN) and "away" (hub) | Less hub traffic; families must pick the right one. |

**Recommendation: A**, with the hub's bandwidth guard (section 7.9). Revisit if
measured hub traffic grows past the budget.

### Q5. ffmpeg: download on first run, or bundle our own LGPL build?

| Option | Consequence |
| --- | --- |
| A. Keep today's first-run download (`tools/ffmpeg_fetch.py`: `imageio-ffmpeg` wheels from PyPI, version and SHA-256 pinned) | Works today on Windows and macOS. The builds are GPL, which is why they are fetched rather than redistributed. Needs PyPI at first run (reachable but slow in parts of mainland China; pinned hashes allow any mirror). The Linux build is fully static and failed to resolve host names on the cloud host. |
| **B. Our own minimal LGPL build, bundled** | The core needs only decoders, demuxers, `scale`/`pad`/`fps`/`aresample`, the native AAC encoder, `rawvideo`/`s16le` output and HTTP(S) (section 5.7). Built in CI per platform (about 5–10 MB each), shipped with its source and configure line. Works offline on first run. 1–2 agent-days. |

**Recommendation: A for the beta, B for 1.0**, with A's fetch kept as a fallback
and given mainland mirrors.

### Q6. macOS builds: two DMGs or one universal?

**Recommendation:** two DMGs (Apple silicon, Intel) built on two runners for the
beta; try `universal2` once numpy is out of the client (section 5.8), since
mixed-architecture wheels are what usually blocks it.

### Q7. Windows installer

| Option | Consequence |
| --- | --- |
| **A. Inno Setup, per-machine (one UAC prompt)** | Installs to Program Files, adds a firewall rule for the core on the **Private** profile only, adds the Start-menu entry and the autostart. Works unsigned (SmartScreen warning). |
| B. Inno Setup, per-user, no admin | No UAC prompt, but the first listen triggers the firewall prompt, which needs admin credentials on a standard account; many families click it away and devices cannot connect. |
| C. MSIX | Cannot be installed unsigned outside developer mode. For after signing, if wanted. |

**Recommendation: A.**

### Q8. Autostart and sleep

**Recommendation:** autostart at login on by default (a tray toggle turns it
off). Prevent idle system sleep while any device is connected or a phone is
listening; never keep the display awake; a tray toggle "keep this computer
awake while Xiaozhi TV runs" for households that want the TV available at any
time (a sleeping PC cannot answer a device that is switched on).

### Q9. Built-in flashing

| Option | Consequence |
| --- | --- |
| A. Only the web flasher, opened at `http://localhost:<port>/flash/` (works today: `localhost` is a secure context) | No new code; needs Chrome or Edge (Safari and Firefox have no Web Serial). |
| **B. Built-in flashing using Espressif's own esptool executable, run as a separate process** | Works with any browser, and can also write the PC's address into a freshly erased Xingzhi (section 6.1). esptool is GPLv2: run as a separate bundled program (not imported into our process), with its licence and source link. 2–3 agent-days. |

**Recommendation: A for the beta, B for 1.0.**

### Q10. Optional server accounts: what happens at the end date?

**Recommendation:** a hard stop at `valid_until` (no grace period): sessions
end, the user's devices get a still frame "access ended on <date>, ask <admin
name>" instead of transcoding; a notice on the remote 7 days before; defaults
for a new grant: 1 device, 1 concurrent stream, 3 months; you can extend from
the Users page in one click.

### Q11. Where does the hub run?

**Recommendation:** on the current host, behind its nginx, for the beta and up
to about 1,000 connected households (section 7.10). If the hub's measured
traffic or connections approach the budget, move it alone to a small
pay-by-traffic instance (the hub's traffic is small in volume; it is the
3 Mbit/s peak that is tight), keeping the same domain.

### Q12. Updates

**Recommendation:** the client checks a signed `latest.json` on `<static-site>`
once a day (no identifiers; a setting turns it off), shows "a new version is
available", downloads the installer on request from GitHub Releases or a
mainland mirror (object storage), verifies its SHA-256 and the manifest's
Ed25519 signature, then opens the installer for the user. No silent updates in
the beta. The private signing key stays offline with you.

### Q13. Code-signing timing and budget

**Recommendation:** unsigned beta with the tutorial (section 5.12). Before a
public release: Apple Developer Program (USD 99 a year, notarisation included)
and a Windows OV code-signing certificate (from about CNY 1,500–3,000 a year;
since 2024 an EV certificate no longer gives instant SmartScreen reputation, so
OV is enough; Azure Trusted Signing is cheaper but, as far as known, not
available to individuals in mainland China; to check).

### Q14. Version numbering

The repository carries upstream's tags `v1.0.0`–`v1.3.0` (the single-user
server) next to this fork's `v0.1.0-beta`. **Recommendation:** keep `v0.x`
betas for the PC client (`v0.2.0-beta`: client; `v0.3.0-beta`: hub) and make
the first public release `v2.0.0`, so it sorts above upstream's tags in any
version comparison (update checks, release lists).

### Q15. Your own devices

**Recommendation:** move your home devices to your own PC client (it frees the
cloud host's 3 Mbit/s for the people you grant access), keep one test device
on the cloud host, and keep your admin account there for the optional users.

## 4. Overview

```
                              home LAN
   ┌───────────────────────────────────────────────────────────────┐
   │  PC client (Windows / macOS / Linux)                          │
   │   tray ─ local control API ─ core supervisor                  │
   │            relay · front :8080 · media :8097/:8098 (gate)     │
   │            ffmpeg · optional ASR · hub agent                  │
   │      ▲ raw TCP (video, sound)        ▲ HTTP (LAN remote)      │
   │   devices (Xingzhi, Passport)     phones at home              │
   └──────────────────────────────────────┬────────────────────────┘
                                          │ one outbound WSS, ciphertext
                                          ▼
   ┌───────────────────────────────────────────────────────────────┐
   │ <owner-server>  (2 vCPU, ~1.8 GB usable, 3 Mbit/s out)        │
   │  nginx ── /r/ static remote pages (versioned)                 │
   │       ── /hub/ hub: routes phone calls to the household's PC  │
   │       ── /tv/ optional accounts' relay + admin (as today)     │
   │  media servers :8097/:8098 for optional accounts only         │
   └───────────────────────────────────────────────────────────────┘
          ▲ HTTPS (ciphertext API calls)            ▲ raw TCP
     phones away from home                  optional users' devices
```

## 5. The PC client (option A)

### 5.1 Process model

```
XiaozhiTV (tray process, GUI main thread)
 │  reads <data>/run/control.json (port + token), talks only to the control API
 └─ starts and watches ──► xiaozhi-tv-core selfhost --data <data> --desktop
                            supervisor (server/selfhost), owns the control API 127.0.0.1:<random>
                            ├── relay   127.0.0.1:8099 / :8100
                            ├── front   0.0.0.0:8080 (LAN), 127.0.0.1:<tunnel port> (hub agent only)
                            ├── media   :8098 Xingzhi, :8097 Passport  (S1)  → gate + workers (S2)
                            ├── hub agent   outbound WSS to <public-domain>/hub/  (when enabled)
                            ├── asr     127.0.0.1:<port>  (optional)
                            └── power   sleep assertions (in the supervisor)
```

- **Two processes at the top, not one.** The tray toolkit wants the main thread
  (macOS requires it for AppKit); the supervisor installs signal handlers,
  which Python allows only on the main thread. A crash in one does not take the
  other down, and option B replaces only the top one.
- **The core is today's supervisor** (`server/selfhost/__main__.py`,
  `supervisor.py`), with a `--desktop` flag for the differences below. Docker
  and `./run.sh --selfhost` keep running it without the flag.
- **Children of a frozen program.** The supervisor starts its children with
  `sys.executable -u -m server.relay …` (`python_command()` in
  `server/selfhost/supervisor.py`). In a PyInstaller build `sys.executable` is
  the bundle and `-m` means nothing, which is why `tools/launch.py` refuses
  `--selfhost` when frozen today. The client needs the dispatch that
  `tools/packaged_entry.py` already does for the single-user server
  (`__media`, `__config`), extended to `__selfhost`, `__relay`, `__front`,
  `__gate`, `__worker`, `__hub-agent`, `__asr`; `python_command()` returns
  `[sys.executable, "<sub-command>"]` when frozen.
- **Children die with their parent.** On POSIX the supervisor's children are in
  its process group and are terminated on stop. On Windows `terminate()` is
  `TerminateProcess`, which leaves the media server's ffmpeg grandchildren
  running and holding ports. `tools/parentwatch.py` already solves this for the
  single-user launcher (a job object with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`
  on Windows, a never-written pipe on POSIX); the supervisor and the tray adopt
  it. The tray puts the core in its own job, so quitting or killing the tray
  stops everything.
- **Single instance.** The tray takes a lock in `<data>/run/` and, if another
  instance holds it, asks that one (through the control API) to open the
  console, then exits.

### 5.2 Packaging per OS

PyInstaller **onedir**, not onefile, on every platform: a onefile executable
unpacks itself into a temporary folder at every start, and the core starts five
or more processes from the same executable (and, with the gate, one per playing
device, within a 2-second budget, section 6.3). Onedir also triggers fewer
antivirus false positives and makes LGPL components replaceable.

| OS | Artifact | Contents | Built on |
| --- | --- | --- | --- |
| macOS, Apple silicon | `XiaozhiTV-<ver>-macos-arm64.dmg` | `Xiaozhi TV.app` (`LSUIElement`: menu-bar only, no Dock icon) with the onedir core inside `Contents/MacOS/`, ffmpeg, esptool; a link to Applications | `macos-14`/`macos-15` runner |
| macOS, Intel | `XiaozhiTV-<ver>-macos-x86_64.dmg` | Same, x86_64 | an Intel runner (`macos-15-intel` or what GitHub offers then; check before building) |
| Windows 10/11 x64 | `XiaozhiTV-<ver>-windows-x64-setup.exe` (Inno Setup) and a portable `.zip` | Program Files install, Start menu, autostart entry, firewall rule (Q7) | `windows-latest` |
| Linux x86_64 (bonus) | `xiaozhi-tv-<ver>-linux-x86_64.tar.gz` | onedir, a `.desktop` file, an optional `systemd --user` unit; `--no-tray` when no tray is available | `ubuntu-22.04` (older glibc for wider reach) |

- **DMG** made with `hdiutil` on the runner; no third-party tool.
- **Info.plist** keys: `LSUIElement`, `NSLocalNetworkUsageDescription` (macOS
  15 asks before an app talks to LAN hosts; the core connects to LAN sources and,
  later, to devices for discovery), `NSAppSleepDisabled` (no App Nap: the core's
  pacing must not be throttled; to verify on macOS 14/15).
- **The release workflow** grows from `.github/workflows/build-server.yml`
  (today: one onefile single-user `tv-server` per platform, macOS arm64 only)
  into a new `build-desktop.yml` with the matrix above, a smoke test that starts
  the frozen core in `--desktop` mode on a temporary data folder and checks
  `/healthz`, a simulated device (`tools/device_sim.py`) getting the unbound
  frame, and the frozen sub-command dispatch for every child. The single-user
  build stays until the client replaces it in the release notes (section 10).
- **Release text.** The release body in `build-server.yml` is upstream's
  single-user text (port 8096, the `FoloToy-XXXX` hotspot, upstream's links);
  it is rewritten for the client.

### 5.3 Tray menu

pystray (LGPLv3; Win32 through ctypes on Windows, AppKit through PyObjC on
macOS, AppIndicator or Xlib on Linux) is the first choice; a spike in phase
P0 confirms it inside a PyInstaller `.app`. The menu, in Chinese in the product:

```
Xiaozhi TV — running · 2 devices watching
  Open the console                 (opens the browser, signed in as the local admin)
  Connect a phone…                 (QR page: LAN sign-in, and away-from-home pairing)
  Set up a TV…                     (setup page: flash, sound provisioning)
  ──────────
  Devices ▸  Living room — CCTV-1 (watching) · Bedroom — offline
  Away-from-home control: on ✓ (connected)
  Keep this computer awake ✓
  Start at login ✓
  ──────────
  Check for updates…
  Open logs folder · Export diagnostics…
  Restart the TV service
  Quit Xiaozhi TV
```

Status in the tooltip and the icon: running, a device watching (filled icon),
problem (badge: ffmpeg missing, port in use, hub rejected).

### 5.4 First run

```
install, start ─► tray appears ─► core first start (bootstrap as today):
   data folder, config.toml, secrets, tv.db (meta.selfhost=1), setup code
 ─► ffmpeg present? (bundled; or fetched with progress in the tray, Q5)
 ─► tray opens http://localhost:8080/tv/setup#<setup code>   (code never leaves the machine)
 ─► page: household name, admin name ─► admin created, browser signed in
 ─► "Connect your phone": QR = http://<lan ip>:8080/TV/P/<phone link>  (existing phone-link sign-in)
 ─► "Set up a TV": flash (built-in or /flash/) ─► sound provisioning from the phone
       (server field = this PC's address, already injected by the relay)
 ─► "Add channels": playlist import, Bilibili creators and series (as in S1)
 ─► optional: "Control from outside the home" (section 7.4), "Voice" (download, 5.9)
```

- The setup code mechanism of S1 stays (proves the admin is at the machine);
  the client passes it to the local browser in the URL fragment, so nobody
  reads a log. LAN visitors without the code still cannot take the server.
- **Local admin sign-in.** "Open the console" asks the control API for a
  one-time sign-in link (the same mechanism as the break-glass
  `python3 -m server.tenancy.admin login-link`), valid 60 s, loopback only.
  Anyone who can use the control API already has the user's files, so this
  gives nothing new.
- **Port in use.** If 8080 is taken (common on developer machines), the core
  tries 8090, 18080, 28080 and records the one it got; phones get QR codes
  anyway. 8097 and 8098 cannot move (the firmware's default ports); if one is
  taken the tray says which program holds it.

### 5.5 Data, configuration and logs per OS

One folder per user with S1's layout (`config.toml`, `secrets/`, `tv.db`,
`devices/`, `channels.txt`, `backups/`, `run/`, `firmware/`, `models/`), plus
`logs/` and `bin/` (fetched ffmpeg, esptool, voice runtime).

| OS | Data | Logs | Program |
| --- | --- | --- | --- |
| Windows | `%LOCALAPPDATA%\XiaozhiTV\` | `…\XiaozhiTV\logs\` | `C:\Program Files\Xiaozhi TV\` |
| macOS | `~/Library/Application Support/XiaozhiTV/` | `~/Library/Logs/XiaozhiTV/` | `/Applications/Xiaozhi TV.app` |
| Linux | `$XDG_DATA_HOME/xiaozhi-tv/` (`~/.local/share/…`) | `$XDG_STATE_HOME/xiaozhi-tv/logs/` | wherever unpacked |

- **Local, not roaming, on Windows.** `tools/datadir.py` uses `%APPDATA%`
  (roaming) today; a SQLite database in a roaming profile can be synced
  half-written on domain machines. The client uses `%LOCALAPPDATA%`.
- **Migration from the beta.** If `<per-user data dir>/selfhost` (the plain-
  Python beta's folder) exists and the client's folder does not, the first run
  offers to reuse it; the layout is the same.
- **Non-ASCII paths.** Chinese user names are common on Windows; the smoke test
  runs with a data folder whose path contains Chinese characters (ffmpeg
  arguments, SQLite path, logs).
- **Secrets.** `os.chmod(0o600)` does nothing useful on Windows; the per-user
  `%LOCALAPPDATA%` ACL is the protection, and the backups warning stays.

### 5.6 Configuration additions

```toml
[desktop]
autostart = true
keep_awake = "watching"        # "watching" | "always" | "never" (Q8)
trusted_networks = []          # router identities where the LAN ports open (5.11); empty = ask

[remote]                       # the cloud remote channel (section 7)
enabled = false
hub_url = "https://<public-domain>/hub/"

[voice]
mode = "off"                   # "off" | "local" (downloaded runtime + model, 5.9)
```

### 5.7 ffmpeg

What the core asks of ffmpeg (from `server/live.py`, `server/media.py`,
`server/frames.py`, `server/listen.py`): read HTTP(S)/HLS/FLV/MP4 sources,
decode common video (H.264, HEVC, MPEG-2, VP9, AV1 optional) and audio (AAC,
MP3, AC-3, Opus), the filters `scale`, `pad`, `setsar`, `fps`, `format`,
`showinfo`, `setpts`, `aresample`, `aformat`, `asetnsamples`, `ashowinfo`,
`anullsrc`, the native `aac` encoder (phone listening) and `rawvideo`/`s16le`
output over TCP. **Nothing GPL-only** (no libx264, no GPL filters), so an LGPL
build suffices.

| Stage | How | Licence handling |
| --- | --- | --- |
| Beta (Q5 A) | `tools/ffmpeg_fetch.py` as today: pinned `imageio-ffmpeg` 0.6.0 wheels, SHA-256 checked, cached in `<data>/bin/`; add mirror URLs (the hash pins make any mirror safe) and a progress line in the tray | GPL builds, fetched by the user's machine, not redistributed (the reason the module exists) |
| 1.0 (Q5 B) | CI builds a minimal LGPL ffmpeg per platform (`--disable-gpl --disable-nonfree --disable-everything` plus the list above; TLS through SecureTransport on macOS, Schannel on Windows, OpenSSL 3 or GnuTLS on Linux), bundled in the app | LGPL 2.1+: ship the exact source tarball and build script as a release asset, the licence in the app's `NOTICE`, and keep ffmpeg a separate replaceable executable |

On Linux the existing note stands: the fetched static build failed to resolve
host names on the cloud host; the Linux client prefers the system's ffmpeg if
present, then the bundled LGPL build.

### 5.8 numpy and the picture degrade

The default degrade, `steady`, needs no numpy (`server/degrade.py`); the numpy
modes (`perceptual`, `mosaic`) are lab modes. The client **leaves numpy out**:
about 20–30 MB less, faster worker start-up, and a universal2 macOS build
becomes possible. `TV_DEGRADE` other than `steady`/`ladder` is refused in the
client with a clear message, as `server/live.py` already does without numpy.

### 5.9 Optional voice

- **Off by default**; the console's Settings offers "Voice (about 300 MB to
  download, about 550 MB of memory while on)". Download: the sherpa-onnx
  runtime for the platform and the SenseVoice-Small model, from a mirror, with
  pinned hashes, after showing the model's licence (still to check, as in the
  self-hosting document).
- Runs as a supervisor child (`__asr`), not a container. **The socket changes:**
  `server/voice/asr.py` speaks HTTP over a Unix socket, which CPython does not
  provide on Windows; the client uses loopback TCP with the relay key in a
  header. The cloud host keeps its Unix socket.
- Below 8 GB of RAM the page warns before enabling.

### 5.10 Sleep prevention

| OS | Mechanism | Scope |
| --- | --- | --- |
| Windows | `SetThreadExecutionState(ES_CONTINUOUS \| ES_SYSTEM_REQUIRED)` through ctypes, from a supervisor thread that stays alive | Idle sleep only; never `ES_DISPLAY_REQUIRED`. A user-chosen sleep or a closed laptop lid still sleeps |
| macOS | An `IOPMAssertion` of type `PreventUserIdleSystemSleep` through ctypes (IOKit), or a `caffeinate -i -w <core pid>` child as the simplest fallback | Idle sleep only. A closed MacBook lid sleeps unless on power with an external display |
| Linux | `systemd-inhibit --what=idle:sleep` child, if available | Same |

Taken when the first device connects or a phone starts listening (the
supervisor reads the media servers' `now-playing` files, which it already
watches for `/healthz`), released 2 minutes after the last one leaves; with
`keep_awake = "always"` taken while the core runs. The tray says when the OS
power plan will still sleep (laptop on battery, lid). Later idea: the device
sends a Wake-on-LAN packet to the PC's MAC (given in CONFIG) when the server
does not answer; useful only for wired desktops with WoL enabled.

### 5.11 Firewall and network exposure

- **Windows:** the installer adds inbound rules for the core executable on the
  **Private** profile only. The most common failure is a home Wi-Fi marked
  **Public**: the core detects it (`Get-NetConnectionProfile` equivalent through
  the `netsh`/WMI query once at start) and the tray says how to switch it to
  Private.
- **macOS:** the application firewall is off by default; when on, it prompts
  for the core. An ad-hoc-signed (unsigned) app gets a new prompt after each
  update; signing fixes it (Q13).
- **Trusted networks:** a laptop taken to a café would otherwise expose ports
  8080/8097/8098 there. The client remembers the home network by the router's
  identity (default gateway's MAC address) the first time the user confirms
  "this is my home network"; on other networks the core binds the LAN ports to
  `127.0.0.1` only (the hub agent keeps working). About 1 agent-day; worth it
  because most beta users will run the client on a laptop.

### 5.12 Unsigned beta: what users see and the tutorial

- **macOS 15+:** right-click > Open no longer bypasses Gatekeeper. The tutorial
  shows: open the app, click Done (not Move to Trash), System Settings > Privacy
  & Security > "Open Anyway", confirm. Also: drag the app to Applications first
  (an app run from the mounted DMG is translocated and loses its data path).
  The builds are ad-hoc signed, which Apple silicon requires.
- **Windows:** SmartScreen "Windows protected your PC" > More info > Run
  anyway. Windows Defender sometimes flags PyInstaller bootloaders: onedir,
  building the bootloader from source in CI, and submitting false positives to
  Microsoft reduce it; the tutorial says how to check the published SHA-256.
- Every release publishes `SHA256SUMS` and its Ed25519 signature (Q12), so a
  careful user can verify the download.

### 5.13 Local control API and option B

Everything the tray does goes through one HTTP API that the supervisor serves
on `127.0.0.1:<random port>`, written with a random token into
`<data>/run/control.json` (mode 0600, per-user folder). Requests carry the
token in a header; anything without it is refused.

| Endpoint | Purpose |
| --- | --- |
| `GET /local/v1/status` | version, ports, LAN URL, devices and what they play, ffmpeg, voice, hub state, update available |
| `POST /local/v1/console-link` | one-time admin sign-in URL for the browser (or a webview) |
| `POST /local/v1/phone-qr` | LAN phone-link URL, and the pairing URL when the hub is on (section 7.4) |
| `POST /local/v1/remote/enable`, `/disable`, `/forget-phones` | hub registration and pairings |
| `POST /local/v1/power`, `/autostart` | settings the tray toggles |
| `POST /local/v1/flash/*` | built-in flashing (section 6.1) |
| `POST /local/v1/update/check`, `/update/download` | updates (5.14) |
| `POST /local/v1/diagnostics` | builds the diagnostics archive (5.15) |
| `POST /local/v1/restart`, `/stop` | lifecycle |

**Option B (Tauri)** replaces only the tray process: a Tauri v2 shell starts
the same onedir core (bundled as a resource and started through the shell
plugin, since Tauri's `externalBin` sidecar expects a single file), reads
`control.json`, shows the console in a WebView via `console-link`, and draws its
tray from `/local/v1/status`. No core change. What B adds: a real window
instead of a browser tab, notifications, and a native feel; what it costs:
4–6 agent-days, a Rust toolchain in CI, WebView2 on Windows (present on current
Windows 10/11), and the same signing.

#### The contract as implemented (P1c)

This is the interface between the core (P1, branch `desktop-core`) and the tray
(P2) or a later Tauri shell. Anything not written here is not part of it.

**Starting the core.** The onedir build is a folder `xiaozhi-tv-core/` with the
executable `xiaozhi-tv-core` (`.exe` on Windows). The tray runs
`xiaozhi-tv-core selfhost --desktop` (no arguments means the same), optionally
with `--data <folder>`; without it the data folder is the per-OS one of 5.5.
Other options: `--ffmpeg <path>` (or `--ffmpeg fetch`: always use the pinned
download), `--bind 127.0.0.1` (loopback only; tests). From a checkout the same
core is `python3 -m server.selfhost --desktop`.

- **Lifetime.** Start it with standard input a pipe that the tray never writes
  and the variable `TV_WATCH_PARENT=1`: when that pipe closes (the tray quits or
  is killed) the core stops cleanly. On Windows also put it in a job object with
  `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` and start it with `CREATE_NO_WINDOW`; the
  core puts each of its own children in a job of its own, so ffmpeg
  grandchildren end with them.
- **Exit codes.** 0 stopped normally; 1 could not start (the reason is the last
  lines of `core.log` and of standard error); 3 another core already runs on
  this data folder (`run/core.lock` is held): read its `control.json` instead.
- **Locks.** The core holds `<data>/run/core.lock`; the tray uses its own
  `<data>/run/tray.lock` for its single instance.

**`<data>/run/control.json`** (written atomically after the API listens, mode
0600, removed on a clean stop; a file left by a crash names a port that no
longer answers):

```json
{"api": 1, "port": 51234, "token": "<43 chars>", "pid": 4242,
 "version": "0.2.0-beta", "started": 1760870000.0,
 "data_dir": "…", "logs_dir": "…"}
```

**Requests.** `http://127.0.0.1:<port>/local/v1/…` with the header
`Authorization: Bearer <token>`. Refused: no or wrong token (401), a `Host`
other than `127.0.0.1:<port>` or `localhost:<port>` (403), any `Origin` header
(403; a web page cannot use the API). Bodies are JSON objects of at most 64 kB.
Answers are JSON: `{"ok": true, …}` or `{"ok": false, "error": "<code>",
"message": "<Chinese text for the user>"}`.

| Endpoint | Body | Answer |
| --- | --- | --- |
| `GET /local/v1/status` | – | the status below |
| `POST /local/v1/console-link` | – | `{"kind": "setup", "url": "http://localhost:<lan>/tv/setup#<code>"}` while there is no admin (open it: the setup page has the code), otherwise `{"kind": "login", "url": "http://localhost:<lan>/tv/local-login?t=<token>", "expires_in": 60}`: opening it in the local browser signs it in as the admin (a 30-day computer session) and lands on `/tv/console`. One use, loopback only. `503 not_ready` before the web front is up |
| `POST /local/v1/phone-qr` | – | `{"url": "http://<lan ip>:<lan>/TV/P/<token>", "svg": "<svg…>", "expires_in": 300}`: the existing one-time phone sign-in for the admin. `409 setup_pending`, `503 no_lan_address` (bound to loopback or no LAN address). `pairing_url` arrives with P5 |
| `POST /local/v1/power` | `{"keep_awake": "watching" \| "always" \| "never"}` | `{"settings": {…}}`. Stored only; who takes the sleep assertion is P2b's choice |
| `POST /local/v1/autostart` | `{"enabled": true \| false}` | `{"settings": {…}}`. Stored only; the tray registers the login item |
| `POST /local/v1/ffmpeg/fetch` | `{"mirror": "<base URL>"}` (optional) | `202` and the download starts (when ffmpeg is missing or the last try failed); `409 busy` or `409 ready` otherwise. Progress is in the status |
| `POST /local/v1/diagnostics` | `{"dest_dir": "<folder>"}` (optional; default the Desktop, else `<data>/exports`) | `{"path": "<zip>", "size": n}` (5.15; no secrets, addresses shortened) |
| `POST /local/v1/restart` | – | `202`; every child is stopped and started again |
| `POST /local/v1/stop` | – | `202`; the core stops and exits 0 |
| `/local/v1/remote/*`, `/flash/*`, `/update/*` | – | `501 not_implemented` until P5, P7, P8 |

The status (`GET /local/v1/status`), every field always present:

```json
{"ok": true, "api": 1, "version": "0.2.0-beta", "pid": 4242, "started": 1760870000.0,
 "state": "starting | fetching_ffmpeg | running | stopping | error",
 "problems": [{"code": "…", "message": "…"}],
 "data_dir": "…", "logs_dir": "…", "bind": "0.0.0.0",
 "ports": {"lan": 8080, "lan_wanted": 8080, "xingzhi": 8098, "passport": 8097, "control": 51234},
 "lan_url": "http://192.168.1.5:8080/tv/", "local_url": "http://localhost:8080/tv/",
 "setup_pending": true,
 "children": {"relay": {"running": true, "restarts": 0}, "xingzhi": {…}, "passport": {…}, "front": {…}},
 "devices": [{"id": "…", "name": "…", "model": "xingzhi-cube-154", "online": true, "channel": "…", "since": 1760870100.0}],
 "watching": 1,
 "ffmpeg": {"state": "ready | fetching | missing | failed", "path": "…", "source": "given | system | fetched",
            "done": 0, "total": 0, "error": ""},
 "voice": {"mode": "off"}, "hub": {"state": "off"}, "update": {"available": false},
 "settings": {"keep_awake": "watching", "autostart": true}}
```

`problems[].code` is one of `ffmpeg_missing`, `ffmpeg_failed`,
`port_in_use` (with `port` and, when the OS says, `holder`: the program's
name), `lan_port_moved` (with `from` and `to`), `child_down` (with `name`),
`config` (config.toml refused; `state` is then `error`). The tray shows the
first problem's `message`. `lan_url` is empty when the core listens on
loopback only or the LAN address is unknown.

Settings the tray toggles live in `<data>/desktop.json` (written by the core
through the API), not in `config.toml`, which stays the user's hand-edited
file and is never rewritten.

### 5.14 Updates

Q12's flow. `latest.json` on `<static-site>`:
`{version, released, notes_url, files: [{platform, url, mirror_url, sha256, size}]}`
with a detached Ed25519 signature (`latest.json.sig`). Verification uses a
small pure-Python Ed25519 check (one verification per day; speed does not
matter) or the `cryptography` package if bundled (section 7.6). The client
never runs a downloaded file itself: it verifies, then opens the installer (or
the DMG) for the user. Firmware updates stay as in S3 (re-flash without erase;
the admin backend shows which devices are behind).

### 5.15 Logging and diagnostics

- The supervisor writes each child's prefixed output to `logs/core.log`, rotated
  at 2 MB, 5 files; the tray process to `logs/tray.log`. The rules stay: no
  tokens, secrets, session ids, pairing keys or full IP addresses.
- "Export diagnostics" makes a zip on the Desktop: logs, `config.toml` (no
  secrets folder), versions, OS, `status.json`, ffmpeg version, port check,
  network profile. The user sends it if they want; nothing is uploaded.
- No telemetry, no crash reporting service.

## 6. Devices and phones on the LAN

### 6.1 How a device finds the PC

| Way | Status | Notes |
| --- | --- | --- |
| **Sound provisioning** from the phone, server field pre-filled with the PC's address | Works (S1 injects `device_address` into `wifi.html`) | The default path; the PC knows its LAN address (no container), so the banner problem of bridged Docker does not arise |
| **Built-in flashing writes the address** | Q9 B | Xingzhi: after the erase, write a small NVS partition holding `wifi/ota_url` (the entry `tools/set_server_addr.py` edits). Passport: never erase; rewrite only an existing `ota_url` the way `set_server_addr.py` does, or leave it to sound setup |
| Hotspot page `192.168.4.1` | Works | Fallback, as today |
| The PC's `.local` name instead of its IP | Unverified | Survives DHCP changes; lwIP's mDNS query is on by default in ESP-IDF but untested on both boards; Windows 10+ and macOS answer for their own name. If verified, the client offers the `.local` name as `device_address` |
| UDP discovery fallback (firmware) | Later | When the stored server does not answer N times, the device broadcasts a "where is the TV server" datagram; the client answers with its address; the device accepts it only from a server whose relay key matches the `wifi_key` it pinned on first use. About 1.5 agent-days of firmware plus 0.5 server |

The address can change: a laptop's DHCP lease usually keeps its IP, but not
always. The tutorial suggests a DHCP reservation as optional; the `.local` name
or the discovery fallback removes the need.

### 6.2 How phones find the PC

- **QR from the PC**: tray > "Connect a phone" opens a page showing
  `http://<lan ip>:<port>/TV/P/<one-time phone link>` (the existing "let another
  phone sign in" mechanism, `server/tenancy/phonelinks.py`). Scanning signs the
  phone in as the household's account; Add to Home Screen keeps `?s=` as today.
- **QR on the device** (`/TV/R/<code>`) keeps working for guests and family.
- When the hub is on, the same page shows the away-from-home pairing QR as
  well (section 7.4); with Q4 A the family adds only that one.

### 6.3 Several devices at once (gate-lite, S2)

Needed as soon as a home has two devices of the same model. The design of
self-hosting section 5.3 stands (listen on 8097/8098, read HELLO, profile from
`model`, hand the socket to a per-device worker, worker stays 10 minutes, cap
`max_streams`, unbound frame from the gate). What the PC adds:

- **Windows has no `socket.send_fds`.** Windows has its own hand-off:
  `socket.share(worker_pid)` in the gate and `socket.fromshare(data)` in the
  worker (WSADuplicateSocket underneath). The gate sends the shared blob and the
  HELLO bytes over a loopback control connection to the worker. POSIX keeps
  `send_fds` over a Unix socket.
- **Not a byte-copying proxy.** A gate that relays bytes would be simpler and
  portable, but it would add two socket buffers between the worker and the
  device and hide the backpressure that the sender's pacing and the rate
  controller measure. The hand-off keeps the worker on the device's own socket.
- **Start-up budget.** The device waits 2 s for CONFIG. A frozen onedir worker
  start on Windows includes Defender's scan of a new process (estimated
  0.5–1.5 s, to measure); the gate keeps **one warm spare worker per model** on
  Windows from the start.
- Effort: S2's 3–4.5 days plus about 0.5–1 day for the Windows path and its
  tests.

### 6.4 One household: simplifications

Same as self-hosting 7.3 (no legacy token, no migration, friendlier device
names), plus: the PC's own browser is signed in as admin through the control
API; the Users page stays (family members, guests) but the console opens on
the remote, not on administration; signup through an unbound device stays LAN
only. No multi-user code is removed: it is what guests, family accounts and
the optional server use.

## 7. Cloud remote channel (the hub)

### 7.1 Why our own, compared with the alternatives

| | **Hub (this design)** | frp through the owner's server | Tailscale / ZeroTier | Cloudflare Tunnel | Port forward + DDNS |
| --- | --- | --- | --- | --- | --- |
| What the family installs | Nothing beyond the client | Nothing (frpc in the client) | The VPN app on **every phone**, an account (Google, GitHub, Microsoft) | Nothing on phones; a domain on Cloudflare per household | Nothing; router access |
| Works without a public IP | Yes (outbound 443) | Yes | Yes, often via relays | Yes | No (CGNAT is now common on mainland home broadband) |
| What crosses the owner's server | Small encrypted API calls | Everything proxied: pages, listening audio, anything the household opens | Nothing (their relays) | Nothing (Cloudflare's) | Nothing |
| Owner sees content | No (E2E) | Yes, unless each household adds its own TLS | No | No (Cloudflare does) | No |
| Mainland China | Mainland host, fast | Fast | Relays overseas, slow or disturbed on some networks | No mainland PoPs on the free plan; slow | Ports 80/443 usually blocked |
| HTTPS for the phone (camera scanner, clipboard, lock-screen controls) | Yes, the public domain | Only with per-household certificates | Yes (`ts.net`) | Yes | Own certificate work |
| Effort for us | 7–9.5 agent-days | 2–3 days, then a generic tunnel to police | Tutorial only | Tutorial only | Tutorial only |
| Abuse surface | Fixed API shape, quotas, no HTML from households | A generic TCP/HTTP tunnel on your domain: phishing pages, proxying | None for us | None for us | Household's own exposure |

The hub is the only option that needs nothing from a non-technical family,
works behind CGNAT, is fast in mainland China, and keeps the owner's server
from seeing or carrying content. The others stay as **advanced recipes** for
Docker and NAS users (the self-hosting tutorial pages).

### 7.2 Protocol between the PC and the hub

- **Transport:** one WebSocket over TLS from the PC's hub agent to
  `wss://<public-domain>/hub/v1/pc`. nginx terminates TLS and proxies the
  upgrade to the hub on loopback. A minimal RFC 6455 implementation with the
  standard library (text and binary frames, ping/pong, close; no extensions),
  shared by the hub and the agent, about 300 lines with tests, keeps `server/`
  dependency-free.
- **Authentication:** after the upgrade the agent sends
  `{"t":"hello","hh":"<household id>","token":"<household token>","core":"<version>","pages":"<page version>"}`.
  The hub stores only SHA-256 of the token (as the relay does for sessions).
  Wrong token: close with a code, count per IP.
- **Heartbeats:** the agent sends a ping every 45 s (well inside home-router
  and CGNAT idle timeouts; to verify on a few mainland networks); the hub drops
  a connection silent for 120 s. nginx's `proxy_read_timeout` is set above
  that.
- **Reconnect:** exponential backoff 1 s to 60 s with full jitter; after a hub
  restart the first retry is spread over 0–30 s, so a thousand PCs do not
  reconnect in the same second. A newer connection for the same household
  replaces the older one.
- **Frames:** `{"t":"call","id":<n>,"phone":"<phone id>","env":"<ciphertext>"}`
  from the hub, `{"t":"reply","id":<n>,"env":"<ciphertext>"}` back, plus
  `{"t":"block","phone":…}` from the agent (stop forwarding a phone that fails
  authentication) and `{"t":"quota",…}` from the hub (tell the PC it is being
  limited).

### 7.3 Phone to hub

- `GET /r/` (static, from nginx): a tiny bootstrap page. It reads the pairing
  from the URL fragment or its own storage, asks `GET /hub/v1/h/<hh>/info`
  (online, since when, page version), then loads `/r/v/<page version>/remote.html`.
- `POST /hub/v1/h/<hh>/call` with `{"phone":"<phone id>","env":"<ciphertext>"}`:
  the hub forwards it to the household's socket and waits for the reply (20 s;
  30 s for the console's sign-in long-poll), then answers with the ciphertext.
  PC offline: `503 {"pc":"offline","since":…}`; quota: `429`.
- One HTTPS request per remote API call; nginx keeps HTTP/2 connections to the
  phone, so the cost per call is a few hundred bytes of headers.
- **Pages are static and versioned.** The release workflow publishes the
  remote and console pages as `remote-pages-<version>.zip`; the owner's deploy
  unpacks it under `/r/v/<version>/` and keeps the last ten versions. The PC
  announces its page version in `hello`, so a phone always gets pages that
  match its PC's API. An unknown version (a development build) gets "update the
  PC client" rather than a mismatched page.
- **The page change is small.** Every API call in `remote.html` (about 40) and
  `console.html` (about 34) goes through one `call()` helper that does
  `fetch(path)`. In hub mode `call()` encrypts, posts to the hub and decrypts;
  the rest of the page does not change. The listen button shows "only at home"
  in hub mode (Q3).

### 7.4 Pairing

```
Enable (once per PC)                         Pair a phone
tray/console "Control from outside"          tray "Connect a phone", or the LAN remote's
  PC ─POST /hub/v1/register {invite}─► hub     "Use this phone away from home"
  hub ─► {hh: 26-char id, token}               core: phone_id (random 64 bits), K (256 bits),
  stored in secrets/hub.json (0600)              a new relay phone session for the chosen user,
  agent connects                                 stored as pairs[phone_id] = {K, session, user}
                                               QR / link: https://<public-domain>/r/#p1.<hh>.<phone_id>.<K>
                                               phone opens it ─► bootstrap stores (hh, phone_id, K)
                                                 in its own storage, keeps the fragment in the
                                                 address for Add to Home Screen
```

- **The key travels in the URL fragment**, which browsers never send to a
  server; the hub never sees it. The QR is shown on the PC's screen (tray) or
  produced on the phone itself from the LAN remote, where it is a navigation
  from the LAN page to the public page (the LAN page passes it in the
  fragment).
- **One secret on the phone.** The phone holds `K` and its `phone_id`; the PC
  keeps the matching relay session and adds it to each forwarded request. So
  the relay's existing account checks apply unchanged, and revoking a pairing
  revokes that session.
- **Home-screen apps** on iPhone keep their own storage, separate from Safari;
  the existing remote handles this with `?s=` in the saved address. The
  bootstrap keeps the pairing fragment in the address before Add to Home
  Screen for the same reason (that iOS keeps the fragment in a home-screen
  bookmark is to verify on a device).
- **Revocation:** the console's Settings lists "phones away from home" (name,
  paired when, last used) with revoke; tray "Forget all phones" also rotates
  the household token. Pairings unused for 180 days expire, like phone
  sessions.

### 7.5 Routing on the hub and what the PC does with a call

```
phone ─HTTPS─► nginx ─► hub: look up hh → socket; check quotas; forward {id, phone, env}
                               │
PC hub agent ◄─WSS─────────────┘
  decrypt with pairs[phone].K (fail → count, block after 10)
  check allowlist; add Cookie: tvs=<pairs[phone].session>
  HTTP to the front's tunnel port 127.0.0.1:<tunnel port>  (X-Forwarded-For: phone network prefix from the hub)
  response → gzip → encrypt → reply
```

- The front gets a third listener, the **tunnel port**, bound to loopback and
  used only by the agent. It applies the existing public-port rules (no admin,
  no setup, no `?k=`, no self-registration) and, in addition, an **allowlist**
  of remote and console API paths; everything else (flasher, listen streams,
  playlist import, help pages) is refused.
- The hub keeps an in-memory map `hh → connection` and `id → waiting phone
  request`; nothing about calls is stored. A small SQLite table holds
  households (`id`, token hash, created, last seen, status, invite used, quota
  counters).

### 7.6 End-to-end encryption

Symmetric, keyed by the pairing, so it needs nothing that browsers or Python
lack:

- `K` (256 bits) shared by pairing. Per direction:
  `k_up = HKDF-SHA256(K, salt = "tvhub1" ‖ hh ‖ phone_id, info = "phone→pc")`,
  `k_down` likewise with `"pc→phone"`.
- Each message: AES-256-GCM, 96-bit random nonce, AAD = `hh ‖ phone_id ‖
  direction`. Plaintext: `{"ts": <ms>, "rid": <random>, "m": "GET", "p":
  "/api/state", "b": …}`; responses `{"rid": …, "st": 200, "ct":
  "application/json", "b": <gzip+base64 or raw>}`.
- **Replay:** the PC rejects `ts` outside ±120 s of its clock (the phone learns
  the PC's clock offset from each reply) and remembers `rid`s for 240 s.
- **Browser:** WebCrypto (`crypto.subtle`: HKDF, AES-GCM), available in every
  current browser in a secure context, including WeChat's built-in browser;
  the public page is HTTPS, so it is a secure context.
- **Python:** the client bundles the `cryptography` package (fast, OpenSSL);
  without it (plain-Python runs) the core falls back to the AES-GCM and HKDF
  already written and tested for Wi-Fi sealing (`server/wifi_crypto.py`), which
  is slow but adequate for a few kB every few seconds. Shared test vectors for
  both sides, like `tests/test_wifi_crypto.py`.
- **Compression before encryption** keeps the hub's traffic down (state JSON
  compresses several times). The usual compression-oracle concern needs an
  attacker who injects chosen text next to a secret in the same message and
  watches sizes; responses here mix no secrets with attacker-controlled text
  (session tokens are added by the PC and never returned). Accepted, noted.
- **No forward secrecy:** whoever obtains `K` later can read recorded traffic
  of that phone. A later version can add an X25519 exchange once WebCrypto
  X25519 is on every phone the families use (iOS 17+, recent Chrome).
- **Limitation, stated plainly in the privacy page:** the pages' JavaScript is
  served by the owner's server. E2E stops the server's logs, database and
  operators from seeing content; it does not protect against a server that
  deliberately serves a malicious page. The pages are open source and their
  hashes are listed per release.

Effort: 2.5–3.5 agent-days (both sides, vectors, the replay window). Benefit:
the hub never handles session tokens, Wi-Fi passwords sent to devices, titles
or subscriptions; a hub breach exposes routing ids only.

### 7.7 What goes through, and what never does

| Through the hub | Never through the hub |
| --- | --- |
| Remote and console API calls (state, switch, seek, pause, volume, cast a link, creators, series, history, device settings, Wi-Fi management of a device) | Video to devices (raw TCP stays on the LAN) |
| The console's PC sign-in long-poll (if the console is used away) | Listening audio (Q3: LAN only) |
| | The admin backend, first-run setup, the break-glass key link |
| | Firmware, flasher, playlist imports, ffmpeg, model downloads |
| | Device connections of any kind |

The remote pages themselves come from the hub's static folder, not from the
PC.

### 7.8 Failure modes

| Failure | Effect | Handling |
| --- | --- | --- |
| Hub or owner's server down | Remote from outside unavailable | LAN unaffected: devices, LAN remote, console keep working. The public page says "the relay service is unreachable" |
| PC asleep, off, or offline | Hub answers `pc offline since …` | The page says so; Q8's keep-awake option for households that want it |
| Hub restart | All PCs reconnect | Jittered backoff; phones retry once after 2 s |
| Version mismatch | Unknown page version | "Update the PC client" |
| PC reinstalled without its data folder | Old household token gone | Enable again: new household, re-pair phones; the old household expires after 30 days unseen |
| Pairing key leaked | Someone can control that household | Revoke the phone; "forget all phones" rotates everything |
| Hub quota hit | `429` | The page backs off; the PC's tray shows it |

### 7.9 Rate limits, quotas, abuse

- Per household: 8 paired phones, 120 calls a minute, 30 MB a day, 256 kB per
  message after compression; the hub replies `429` above them.
- Per phone network: 60 calls a minute across households (stops one phone
  hammering many household ids).
- Registration: an invite code in the beta (Q2); afterwards 3 households per
  network per day, plus a global cap and a kill switch per household in the
  owner's admin backend (a new "Households" list: id prefix, created, last
  seen, online, calls today, bytes today, status; never content).
- **Bandwidth guard:** the hub counts its outbound bytes per second; above a
  configured share of the 3 Mbit/s (default 1 Mbit/s) it tells pages to slow
  their polling (5 s to 15 s) before refusing anything, so the owner's own and
  optional users' streams keep priority.
- The hub serves no HTML from households and forwards only the fixed envelope
  shape to one PC, so it cannot host phishing pages or act as a general proxy;
  quotas make it useless as a covert channel.
- The PC drops calls that fail to decrypt and asks the hub to block that phone
  id after 10 failures.

### 7.10 Cost per household and capacity of the current host

**Estimated**, to measure in phase P5 with simulated households:

| Item | Per household | Notes |
| --- | --- | --- |
| Idle connection, memory | about 100 kB (hub coroutine and buffers 10–20 kB, nginx TLS connection and upstream 50–80 kB) | asyncio, one thread for all connections |
| Idle traffic | ping every 45 s: about 4 B/s, about 10 MB a month | mostly inbound |
| Foreground remote phone | state poll every 5 s: compressed state 1–4 kB, or about 0.3 kB when unchanged (the PC answers "same as before" by hash): **0.1–0.8 kB/s** out | the page stops polling when hidden or locked |
| A command (switch, pause) | 1–3 kB round trip | |
| Static pages | 30–60 kB compressed per first load, then cached | `remote.html` is about 100 kB uncompressed |

Capacity of the current host (2 vCPU, about 1.8 GB usable of which `muse-asr`,
the relay and the media servers already use most; 3 Mbit/s out):

| Limit | Budget for the hub | Households |
| --- | --- | --- |
| Memory | 150–250 MB | about 1,500–2,500 connected |
| File descriptors | nginx `worker_connections` and `nofile` raised (two descriptors per household at nginx) | config, not a limit |
| Outbound bandwidth | 1 Mbit/s share (125 kB/s) by the guard | about 150–1,000 **foreground** remote phones at once, depending on how often state changes |
| CPU | message forwarding is trivial; TLS handshakes after a restart about 1 ms each | thousands, with jitter |

So the claim "hundreds to thousands of households" holds for **connected**
households; the real limit is how many phones are in the foreground at the same
moment, which is low because people use the remote in short bursts and mostly
at home. If it grows, move the hub to its own pay-by-traffic instance (Q11);
the traffic volume is small (an estimated 10–30 GB a month at 1,000
households), the 3 Mbit/s peak is what is tight.

### 7.11 Not chosen now: peer-to-peer

WebRTC data channels between the phone and the PC, with the hub as signalling,
would take even the small API traffic off the owner's server when NAT allows
it (estimated 70–85 % of cases without TURN). It needs a WebRTC stack in the PC
client (a large native dependency), and the fallback still needs the hub.
Revisit if hub traffic becomes the bottleneck.

## 8. Optional server accounts

For people without a PC, the owner's host keeps its multi-user relay and media
servers. New: each user can have a validity period and limits.

### 8.1 Data and enforcement

`user` gets columns (added the existing way, `ALTER TABLE … ADD COLUMN` in
`server/tenancy/db.py`): `valid_from REAL NULL`, `valid_until REAL NULL`,
`max_devices INT NULL`, `max_streams INT NULL`, `note TEXT`. NULL means no
limit; the admin and existing users stay unlimited. Invites carry defaults for
the new user (Q10: 1 device, 1 stream, 3 months).

| Where | Check |
| --- | --- |
| Session resolution (`server/relay/web.py`, accounts) | Outside `[valid_from, valid_until)`: the user is treated as disabled; pages show "access ended on <date>" or "starts on <date>" |
| Device HELLO (`server/tenancy/gate.py`, `apply()`) | Owner outside validity: CONFIG plus a still frame (like the unbound frame), no transcoding |
| Concurrent streams | Count the user's devices that are online now (fresh `devices/<id>/now-playing.json`); above `max_streams`, `ERROR {"reason":"server busy"}`; with the phase 4 gate this moves into admission control |
| Binding | Refuse a new bind above `max_devices` with a clear message |
| Daily sweep in the relay | At expiry: end the user's sessions, kick their devices (`_kick`), audit `user.expired`; 7 days before: a notice on the remote |

### 8.2 Admin backend

The Users page (`server/relay/admin.py`, `assets/admin.html`) gets columns
"valid until", "devices x/limit", "streams limit", a status badge (active,
not yet started, expired, banned), quick actions "extend 1 month / 3 months /
1 year", an edit dialog for the four fields, and a filter "expiring in 7 days".
Every change is audited. The Overview shows the hub's households count and
bandwidth beside the host curves.

### 8.3 Their devices, and honest capacity

Such a user's devices dial `<public-domain>` (ports 8098/8097), as today, and
their phones use `https://<public-domain>/tv/`. The host's 3 Mbit/s carries
**one Xingzhi at full quality, or two devices degraded, in total**, shared by
the owner and every granted user (multi-tenant design 7.6). Without the phase 4
gate there is also one connection per board model at a time: a second Xingzhi
anywhere gets "server did not answer" while another plays. Audio-only uses
about 0.07 Mbit/s. So: grant access to a handful of people whose viewing rarely
overlaps, with `max_streams = 1`; the phase 4 gate (with admin priority) is
worth building only together with a bandwidth upgrade.

Content: the optional server plays the owner's channel table to the people he
grants, which is the owner serving content to others; the multi-tenant
design's "family and friends, invite only" stays the rule.

## 9. Content and copyright

Unchanged from self-hosting section 8: the client ships no channel list, no
stream address, no cookies, no proxy; the first run starts empty with playlist
import; YouTube is a generic node or proxy setting with no instructions for
obtaining one. The hub carries no content and, with E2E, cannot read titles.
The disclaimer gains one sentence for the remote channel: "Away-from-home
control passes encrypted commands through the project's relay service, which
cannot read them and carries no video or audio."

## 10. Security

| Topic | Design |
| --- | --- |
| LAN exposure | The client listens on the LAN: 8080 (pages), 8097, 8098 (devices). Windows firewall rule on Private only; trusted networks bind them to loopback elsewhere (5.11). Everything else (relay, listen, tunnel port, control API, ASR) is loopback only |
| Local control API | Loopback, random port, random token in a per-user file; the one-time console link expires in 60 s |
| First admin | S1's setup code, passed to the local browser in the fragment |
| Remote path | Only the agent reaches the tunnel port; public-port rules plus an allowlist; no admin, setup, key link, flasher or listen |
| Pairing secrets | 256-bit `K` per phone, fragment only, stored on the PC in `secrets/` (0600 or per-user ACL); revocable; expire after 180 days unused |
| Household token | 256-bit, hashed on the hub; rotated by "forget all phones" |
| Owner's server as relay | Sees household id, phone id, IP addresses, timing and sizes; not content (E2E). Keeps no call logs, only counters; access logs off for `/hub/` and `/r/` paths that carry ids |
| Device link | Unchanged: plain TCP on the LAN with per-device secrets |
| Updates | HTTPS download, SHA-256 from a manifest signed with an offline Ed25519 key, user-confirmed install; never a silent run |
| Downloaded components | ffmpeg, esptool, voice runtime and model: pinned versions and SHA-256, refused on mismatch (the `ffmpeg_fetch` rule) |
| Unsigned binaries | Users are taught to bypass OS warnings during the beta; mitigated by published, signed checksums and by signing before the public release |
| Optional server | As the multi-tenant design, plus validity and limits (section 8) |

## 11. Migration

- **Docker and plain Python stay** for NAS and headless users: same core,
  without `--desktop`. The tutorial puts the PC client first and Docker under
  "NAS and servers".
- **v0.1.0-beta users** (plain Python or Docker): the client offers to reuse
  the beta's data folder (5.5); Docker users keep their volume; nothing in the
  database changes.
- **The single-user `tv-server` executables** from the upstream-style release
  (port 8096, channel page) stop being the recommended download once the client
  ships; the workflow keeps building them for one release with a note, then
  drops them.
- **The owner's devices**: Q15. Optional users stay on the host as today.
- **Carry-over from the self-hosting phases:**

| Phase | Status | In this design |
| --- | --- | --- |
| S1 (supervisor, front, setup code, empty start, playlist import) | Done | Reused unchanged; the client adds `--desktop` |
| S2 (gate-lite) | Not built | Still needed (6.3), plus the Windows hand-off |
| S3 (public firmware, web flasher) | Done | Reused; `localhost` flasher works in the client; built-in flashing in 1.0 |
| S4 (static site and tutorial) | Not built | Rewritten around the client download (P10); Docker pages kept as "advanced" |
| S5 (voice container, YouTube local mode, remote-access recipes) | Not built | Voice becomes the client's optional download (5.9); YouTube local mode unchanged; remote-access recipes replaced by the hub for client users |
| S6 (hardening on real machines) | Not built | Becomes P11 with a Windows/macOS matrix |

## 12. Code changes

| Area | Files | Change |
| --- | --- | --- |
| Frozen entry | `tools/packaged_entry.py` (or a new `desktop/entry.py`), `server/selfhost/supervisor.py` | Sub-command dispatch for every child; `python_command()` frozen-aware; job object / parent pipe from `tools/parentwatch.py` |
| Supervisor | `server/selfhost/__main__.py`, `config.py`, new `control.py`, `power.py`, `netguard.py` | `--desktop`; `[desktop]`, `[remote]`, `[voice]`; control API; sleep assertions; port fallback; trusted networks; rotating log files; hub agent and ASR children |
| Front | `server/front.py` | Tunnel listener on loopback with the remote allowlist |
| Hub | new `server/hub/` (`ws.py`, `service.py`, `agent.py`, `envelope.py`, `store.py`) | WebSocket, asyncio hub with quotas and the bandwidth guard, PC agent, E2E envelope, households table |
| Relay | `server/relay/web.py`, `console.py`, `admin.py` | Pairings (create, list, revoke; a session per paired phone); "same as before" answer for unchanged state; hub status; Households admin page on the owner's host; Users page validity and limits |
| Accounts | `server/tenancy/db.py`, `accounts.py`, `gate.py`, `invite` handling | Validity and limits columns and checks; expired still frame; daily sweep |
| Pages | `server/relay/assets/remote.html`, `console.html`, new `hub.js`, new `/r/` bootstrap | Transport in `call()`; pairing entry; listen "only at home" in hub mode |
| Voice | `server/voice/asr.py`, new `server/voice/local.py` | Loopback TCP; sherpa-onnx runner for the client |
| Media | new `server/gate.py` (S2), `server/tv_server.py` worker mode | Gate-lite with `send_fds` and Windows `socket.share` |
| Desktop | new `desktop/` (`tray.py`, `autostart.py`, `flash.py`, `update.py`, `firstrun.py`) | Tray, autostart per OS, built-in flashing (esptool process, NVS pre-fill), update check and verification |
| Packaging | new `packaging/desktop/` (PyInstaller spec, Info.plist, Inno Setup script, icons, DMG script, NOTICE), `tools/ffmpeg_fetch.py` (mirrors) | Installers per OS; licence notices for ffmpeg, esptool, pystray, Python |
| CI | new `.github/workflows/build-desktop.yml`, `build-ffmpeg.yml`; `build-server.yml` release text | Builds, smoke tests, LGPL ffmpeg builds with source, signed `latest.json`, `remote-pages-<version>.zip` |
| Owner's host | new `packaging/nginx/tv-hub.conf`, `packaging/systemd/tv-hub.service`; `docs/development/host-setup.md` | Hub service and nginx locations, page deploy |
| Firmware | none required; later: UDP discovery fallback, WoL | — |
| Tests | `tests/test_hub_*.py`, `tests/test_envelope.py` (+ shared JS vectors), `tests/test_selfhost_desktop.py`, `tests/test_gate.py`, `tests/test_account_limits.py` | Protocol, quotas, E2E, allowlist, frozen dispatch, limits and expiry |
| Docs | `docs/selfhost/`, new `docs/desktop/`, `server/README.md`, architecture index | User and operator docs |

## 13. Phased plan

Efforts in agent-days (one agent with host tests), excluding your review and
on-device checks. Light work only on your machines; no load tests beyond the
small simulated one in P5e.

### P0 — decisions and spikes (1–1.5)

Answer Q1–Q15. Spikes: pystray inside a PyInstaller onedir `.app` and on
Windows; the frozen sub-command dispatch with the supervisor; `socket.share`
hand-off on Windows; size of `/api/state` for a typical household; whether iOS
keeps a URL fragment in a home-screen bookmark.

### P1 — the core, frozen, on three OSes (3–4)

- P1a. Frozen dispatch, spec (hidden imports for `server.relay`,
  `server.tenancy`, `server.selfhost`, `server.front`, `server.listen`; the
  pages, flasher and help as data), smoke test of every child. (1–1.5)
- P1b. Windows path of the supervisor: job object, stop without signals, ASR
  over TCP, non-ASCII paths, port fallback, `%LOCALAPPDATA%`. (1–1.5)
- P1c. Control API, one-time console link, rotating logs, diagnostics. (1)

### P2 — tray and installers (4–5.5)

- P2a. Tray, menu, first-run flow, single instance. (1.5–2)
- P2b. Sleep prevention, autostart, network profile check, trusted networks. (1–1.5)
- P2c. `.app` + DMG (arm64, x86_64), Inno Setup, Linux archive, CI workflow
  with smoke tests, NOTICE. (1.5–2)

**Status (2026-10-19): implemented on branch `desktop-tray`**, against the P1c
contract of 5.13, with `desktop-core` merged in (CI builds the installers around
the real core; `desktop/devcore.py` is a stand-in core for the tray's tests). Code map, decisions and what is still open:
[`docs/desktop/developer.md`](../../desktop/developer.md). The tray holds the
sleep assertion and registers the login item; trusted networks (5.11) wait for
the core (the tray side and a proposed `network` status block are ready); the
core and the tray are two onedirs (macOS: the core in `Contents/Resources/core/`,
not `Contents/MacOS/`, for code signing).

### P3 — ffmpeg (1–2.5)

- P3a. Beta: mirrors for the pinned fetch, tray progress. (0.5)
- P3b. 1.0: minimal LGPL builds per platform in CI with source assets. (0.5–2;
  the macOS build is the uncertain part)

### P4 — several devices at once (3.5–5)

S2 as designed, plus the Windows `socket.share` path and a warm spare worker.

### P5 — cloud remote channel (7–9.5)

- P5a. WebSocket, hub service, households store, quotas, bandwidth guard,
  nginx and systemd files. (2.5–3)
- P5b. Hub agent, tunnel listener and allowlist in the front. (1–1.5)
- P5c. E2E envelope in Python and WebCrypto with shared vectors. (1.5–2)
- P5d. Pairing (tray, console, LAN remote), revocation list, `call()`
  transport, versioned page deploy. (1.5–2)
- P5e. Simulated-household test: a few hundred idle connections and a few dozen
  pollers against a local hub; measure memory per connection. (0.5–1)

### P6 — optional server: validity and limits (2–3)

Columns, checks, expired frame, sweep, Users page, invites with defaults,
tests. Independent of everything else; can go first if you want to grant access
soon.

### P7 — built-in flashing (2–3)

esptool executable per platform (pinned), chip detection, the board rules of
S3 (Xingzhi erase and NVS address pre-fill; Passport never erase, protected
range), a console page.

### P8 — updates (1.5–2)

Signed `latest.json`, check, download with mirror, verification, opening the
installer; release workflow signs with a key you hold (or signs locally).

### P9 — optional voice (2–3)

sherpa-onnx runtime and model download with hashes, local ASR child over TCP,
settings page, memory warning.

### P10 — website and tutorial (2–3)

Download page per OS, Gatekeeper and SmartScreen steps with screenshots,
first run, phones, away-from-home control and its privacy statement, Docker
pages moved to "advanced".

**Status (2026-10-19), the `v0.2.0-beta` part:** the tutorial
[`docs/desktop/`](../../desktop/README.md) (Chinese first: download, Gatekeeper
and SmartScreen, first run, phones, flashing and binding, tray menu, data and
uninstalling; screenshots still TODO) and the download page
`packaging/site/download/index.html`; `docs/selfhost/` is marked "advanced".
Away-from-home control and its privacy statement come with P5.

### P11 — beta hardening on real machines (2–3)

Windows 10 and 11, an Apple silicon and an Intel Mac, a laptop changing
networks, sleep and wake, two devices of one model, update from the previous
version, the hub from a mainland mobile network.

**Total: about 31–45 agent-days.**

| Milestone | Phases | Cumulative |
| --- | --- | --- |
| `v0.2.0-beta`: PC client, one device per model, LAN remote | P0, P1, P2, P3a, part of P10 | about 10–13 |
| `v0.3.0-beta`: away-from-home control | + P5 | about 17–23 |
| Optional server limits (any time) | P6 | +2–3 |
| `v2.0.0` public (Q14) | + P3b, P4, P7, P8, rest of P10, P11, signing | about 29–42 |
| Voice | P9 | +2–3 |

Parallel: P6 is independent; P5a can start during P2; P4 after P1; P7 and P8
after P1c.

## 14. Risks

| Risk | Effect | Mitigation |
| --- | --- | --- |
| Antivirus flags the PyInstaller build | Install blocked or the core deleted | Onedir, bootloader built from source, false-positive submissions, signing before public release |
| Users cannot get past Gatekeeper or SmartScreen | Beta stalls at install | Screenshot tutorial; signing for the public release (Q13) |
| Home Wi-Fi marked Public on Windows | Devices cannot connect | Detect and explain in the tray (5.11) |
| PC sleeps or is switched off | Device shows "server did not answer"; remote says PC offline | Keep-awake option; clear messages; WoL later |
| PC's IP changes | Devices lose the server | `.local` name once verified; discovery fallback later; optional DHCP reservation |
| Hub becomes popular | Owner's 3 Mbit/s or memory fills | Bandwidth guard, quotas, invite-only beta, move hub to its own instance (Q11) |
| E2E key handling on phones (storage cleared, home-screen storage separate) | Phone must be paired again | Pairing is one scan; the LAN remote can re-pair itself |
| JavaScript served by the owner's server | E2E does not cover a malicious server | Stated in the privacy page; page hashes per release |
| Mainland reachability of GitHub and PyPI | Downloads, ffmpeg fetch, updates fail | Mainland mirror for installers; bundled LGPL ffmpeg in 1.0; hash-pinned mirrors |
| ffmpeg / esptool / pystray licences | Compliance | LGPL build with source; esptool as a separate program with source link; NOTICE |
| macOS App Nap or power management throttles the core | Stutter in the background | `NSAppSleepDisabled`, power assertion while watching; verify on macOS 14/15 |
| Windows worker start-up slower than 2 s | First connection after idle fails | Warm spare worker per model (6.3) |
| Drift between client, Docker and cloud | Three ways to run one core | `--desktop` is configuration; CI runs the same tests for all |
| Optional server content | The owner serves content to others | Keep it to a few invited people, as decided in the multi-tenant design |

## 15. Not verified

- Every figure marked **estimated**: hub memory per connection, state size,
  worker start-up on Windows.
- pystray inside a PyInstaller `.app` on macOS 14/15, and its Linux tray
  support on GNOME.
- `socket.share`/`fromshare` hand-off between frozen processes on Windows.
- Idle timeouts of mainland home routers and CGNAT for a 45 s heartbeat.
- That iOS keeps the URL fragment in a home-screen bookmark; WebCrypto HKDF and
  AES-GCM in WeChat's browser on current Android and iOS.
- Device resolution of `.local` names (both boards); Windows answering mDNS for
  its own host name with default firewall rules.
- macOS 15 Local Network prompt behaviour for an unsigned menu-bar app;
  `NSAppSleepDisabled` honoured for the core's child processes.
- Availability of an Intel macOS runner on GitHub at build time; Azure Trusted
  Signing eligibility from mainland China; current Apple and certificate
  prices.
- A minimal LGPL ffmpeg build on macOS with SecureTransport TLS covering the
  sources households use.
- SenseVoice-Small's licence for download by the client.
