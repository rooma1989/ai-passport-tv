<p align="right">
  <a href="README.zh_CN.md">简体中文</a> · <strong>English</strong>
</p>

# Running the Xiaozhi TV server at home (0.x beta)

> **Advanced.** For a Windows PC or a Mac, use the **PC client** instead: an
> installer, a tray icon, the setup page opens by itself
> ([`docs/desktop/`](../desktop/README.md), from 0.2.0-beta). This page is for a
> NAS, a mini PC without a desktop, Docker, or running from source.

The Xiaozhi TV server can run on a computer, NAS or mini PC at home that stays
on; the TVs on the same home network connect to it. No cloud server, no
bandwidth bill, and the best picture. This page is the quick start; the design
and its trade-offs are in
[`2026-10-15-self-hosting-design.md`](../superpowers/specs/2026-10-15-self-hosting-design.md).

**This software ships no channels, streams, playlists, accounts or cookies.**
The channel table is empty on first start; you add what to play yourself (the
Channels page can read a few public third-party playlists by their links, see
step 3).
Please read the [disclaimer](#disclaimer) at the end first.

## Limits of the beta

- **One TV per board model at a time.** With two Xingzhi Cubes at home, the
  second shows "server did not answer" (in Chinese) and retries until the first
  is switched off or disconnects. One Xingzhi and one AI Passport can watch at
  the same time. Several at once comes with version 1.0.
- No voice container yet (a later version).
- Steps marked "unverified" are written from the official documentation and
  have not been tried by the author; so far only the plain-Python path has run,
  on one Apple-silicon Mac.

## What you need

- A Xiaozhi TV: a Xingzhi Cube 1.54 (Xiaozhi AI case) or a FoloToy AI Passport,
  with the Xiaozhi TV public firmware.
- A machine that stays on (table below), on the home network, ideally with a
  fixed IP from the router ("DHCP reservation").
- A USB cable that carries data, and a computer with Chrome or Edge (once, for
  flashing).
- 2.4 GHz Wi-Fi at home.

### How strong a machine

The beta plays one stream per board model at a time, so almost any 64-bit
machine that runs Docker is enough: at least 2 GB of RAM. The figures below are
for later, when several TVs watch at once. Everything not marked "measured" is
**estimated** (scaled by single-core performance, not measured on that machine).

| Quantity | Value |
| --- | --- |
| CPU per video stream | Measured (M1 Pro): about 0.15 of a core (720p source; 0.07–0.09 at 360p, about 0.18 at 1080p) |
| RAM per video stream | Measured: 50–70 MB |
| Server at idle | Relay measured about 22 MB; the other processes expected under 100 MB together |
| LAN bandwidth per stream | Measured: about 1.7 Mbit/s Xingzhi, 1.0–1.2 Mbit/s AI Passport |
| Internet download per stream | The source's own bitrate, typically 1–6 Mbit/s |

| Machine | Streams at once (estimated) | Notes |
| --- | --- | --- |
| Intel N100 / N97 mini PC, 8 GB | about 10 | Recommended: 6–10 W idle |
| Old laptop, Core i5 4th gen or newer, 4 GB+ | 3–4 (dual core) to 6+ (quad core) | Must not sleep with the lid closed |
| Mac, Apple silicon | M1 Pro about 30 (saturation not measured yet) | Must not sleep |
| Synology / QNAP x86 NAS (J4125, R1600) | about 4–6 | |
| UGREEN DXP (N100) | about 10 | |
| Raspberry Pi 5, 4–8 GB, 64-bit OS | about 6 | Experimental, not measured |
| Raspberry Pi 4, 4 GB+, 64-bit OS | about 2 | Experimental, not measured |
| ARM NAS (Realtek RTD1296/1619B) | 0–1 | Not recommended |
| Raspberry Pi 3, Zero 2, 32-bit OS, routers | — | Not supported |

## Step 1: install the server

The server uses these ports: **8080** (the pages phones and computers open),
**8098** (Xingzhi Cube), **8097** (AI Passport).

### A. Mac or Linux without Docker

Verified on the author's Mac (Apple silicon, Python 3.12, Homebrew's ffmpeg).
Needs Python 3.11 or newer and ffmpeg:

```bash
brew install python@3.12 ffmpeg          # Mac; Debian/Ubuntu: sudo apt install python3 ffmpeg git
git clone https://github.com/rooma1989/ai-passport-tv.git
cd ai-passport-tv
./run.sh --selfhost
```

The data (database, keys, your channel table) goes to
`~/Library/Application Support/ai-passport-tv/selfhost` on a Mac and
`~/.local/share/ai-passport-tv/selfhost` on Linux; add `--data <folder>` to put
it elsewhere. Closing the terminal window (or Ctrl+C) stops the server.

On a Mac, the first start asks whether Python may accept incoming network
connections: choose **Allow**, or the TVs and phones cannot connect. The Mac
must not sleep while anyone watches: System Settings → Battery (or Energy) →
prevent automatic sleeping on power adapter.

### B. Docker (Linux mini PC, old laptop) — unverified

Needs Docker and Docker Compose (`docker compose version` prints a version). The
image is not published yet, so the first start builds it on your machine, which
takes a few minutes:

```bash
git clone https://github.com/rooma1989/ai-passport-tv.git
cd ai-passport-tv/packaging/docker
mkdir data                    # create it first so the container can write to it
docker compose up -d
docker compose logs tv        # the setup code is in here
```

All data is in `packaging/docker/data/`. The container runs as user id 1000;
when it cannot read or write `data` the log says so. Put `PUID=…` and `PGID=…`
in a `.env` file next to `compose.yaml` (the `id` command shows yours) and run
`docker compose up -d` again.

Inside the container the server cannot see your LAN address, so the log says
"this computer's address" (in Chinese): use the IP of the machine running Docker
(for example `192.168.1.20`).

### C. NAS (Synology, UGREEN, QNAP) — unverified

1. In the NAS file manager, create a folder such as `docker/xiaozhi-tv/data`.
2. Synology DSM 7.2+: Container Manager → Project → Create, path
   `docker/xiaozhi-tv`, contents from `packaging/docker/compose.yaml` (until
   the image is published a NAS cannot build it: wait for the image, or build
   on a computer and import it with `docker save`). UGREEN UGOS Pro's Docker app
   and QNAP Container Station take the same compose file.
3. Set PUID/PGID to the owner of that folder; if another NAS app uses 8080,
   change `"8080:8080"` in the compose file to `"18080:8080"` and open the pages
   on 18080.
4. Find the setup code in the NAS's container log.

### Windows — unverified

Use Docker Desktop (needs WSL 2, one restart after installing), then follow B
above. The first published port brings up a Windows firewall prompt: choose
"Private networks". The computer must not sleep either.

## Step 2: first run (the setup code)

On start the server prints this to its log (or terminal), in Chinese:

```
Xiaozhi TV server is running.
  Open on a phone or computer on your home network:  http://192.168.1.20:8080/tv/setup
  Setup code: K7P4Q-X2M9A   (one use; makes the administrator)
  Or scan this with a phone (the code is filled in): …
  On the TVs, enter this server address: 192.168.1.20 (no port needed)
```

1. Open that address on a phone on the home Wi-Fi (or scan the terminal's QR
   code, which fills the code in).
2. Enter the setup code and your name, and tap the button. The first person to
   do this is the administrator.
3. Follow the page to add the phone remote to the home screen.

The setup code works once; until the administrator exists, every restart makes
a new one. The setup page opens only on the home network (not on the tunnel
port). Five wrong codes from one network in 10 minutes means a wait.

## Step 3: add channels (your own)

The server ships no channels. The administrator opens
`http://<address>:8080/tv/admin` on a computer → **Channels**:

- **CCTV and provincial satellite channels in one click**: under "Import a
  playlist", pick one of the public playlists (iptv-org China or Chinese,
  vbskycn/iptv, myIPTV) and press "Read" (or its upstream's mirror button, for
  example gh-proxy, when GitHub is slow from where you are). In the list press
  the CCTV or satellite quick filter, then "Select all" and **Add to the
  channel table**, then **Save** at the bottom. These are third-party lists
  found on the internet; their content is the responsibility of their
  maintainers, this project does not provide or vouch for any stream, and you
  should add only what you are entitled to watch. Many streams in them stop
  working after a while; "Try" (up to 20 ticked channels at a time) shows which
  play now.
- **Import from an address or a file**: paste the address of an M3U playlist
  and press "Read", or upload a `.m3u` file; then the same list. The address is
  fetched once, when you press "Read", and not stored.
- Channels whose address or name is already in the table, and repeats within
  the list, are marked "duplicate", left unticked and skipped by "Select all";
  the same address is never added twice. Adding stops at what every user's TV
  can hold, and saving checks it again and backs up the old table first.
- **Add one channel**: a name and a live stream address (`http(s)://…`).
- A TV takes at most 128 channels; pick the ones you watch.

Bilibili creators and series can be added in the phone remote's Subscriptions
tab (per person). YouTube is optional and off by default; see
[YouTube (optional)](#youtube-optional) below. This project does not provide a
way to reach YouTube.

With no channel at all, a bound TV shows "NO CHANNELS YET / ADD SOME / ON YOUR
PHONE" and reconnects by itself once something is added.

## Step 4: flash, connect and bind a TV

1. **Flash**: plug the TV into the **server machine** with the USB cable and,
   on that machine, open **`http://localhost:8080/flash/`** in Chrome or Edge
   (your port if you changed it). Browsers allow Web Serial only on `https://`
   or `localhost` pages, so **the LAN address
   (`http://192.168.x.x:8080/flash/`) cannot flash**, and the page says so. The
   page detects the chip: a Xingzhi Cube is erased on its first install, which
   removes the original Xiaozhi AI firmware and needs your tick, and later
   flashes update without erasing; the AI Passport is **never** erased.
   - The server's flasher needs the firmware files: without Docker, run
     `./run.sh --fetch-firmware` (downloads the latest release's
     `xiaozhi-tv-flasher.zip`, checks it and puts it into the data folder's
     `firmware/`), or download that zip yourself and unpack it into
     `firmware/`, then restart the server. The Docker image is built with the
     latest released firmware; a set in the data folder's `firmware/` wins.
   - Server on a NAS, or the TV plugged into another computer: unpack
     `xiaozhi-tv-flasher.zip` under `/flash/` on any **https** site (the
     project website's copy comes with phase S4; its address goes here once
     published) and open it on the computer the TV is plugged into.
   - The admin backend's Devices page marks TVs whose firmware can be updated
     and links to `/flash/`.
2. **Wi-Fi by sound**: when the TV shows its setup page, open
   `http://<address>:8080/tv/wifi` on the phone (the setup page links to it),
   enter the Wi-Fi name and password; the server address is filled in with the
   address from the banner (no port). Press "Send" with the phone near the TV.
   **Hotspot** (fallback): join the TV's hotspot with the phone, open
   `192.168.4.1`, and enter the Wi-Fi and the server address.
3. **Bind**: once connected, the TV shows "BIND THIS TV" and a 6-digit bind
   code (the cloud service uses 20 characters; a self-hosted server uses 6
   digits). With the signed-in phone, scan the QR code on the TV's status page
   with the phone's camera, or use Me → Bind a new device in the remote to scan
   or type the code. The TV then plays your channels. Wrong codes are limited:
   5 per phone address and 30 in total per 10 minutes, then binding pauses for
   up to 10 minutes; after 30 wrong codes every unbound TV also gets a new code,
   shown within a few seconds. A TV still showing a 20-character code from an
   older version gets a 6-digit one the next time it connects.

The server's address must not change: a fixed IP from the router is the most
reliable. A name like `computer.local` may work too, but has not been verified
on the TVs.

## YouTube (optional)

YouTube is off by default; Bilibili and live channels work without it. Turning
it on is for households that already have their own way to reach YouTube from
this computer: **this project provides no proxy and gives no instructions for
getting one.**

1. In the console (signed in as the admin) open **Settings → YouTube** and
   choose **This computer**, or set `[youtube] mode = "local"` and
   `proxy = "…"` in `config.toml` and restart the server.
2. Proxy: type the address your proxy program gives for this computer, for
   example `http://127.0.0.1:<port>` or `socks5://127.0.0.1:<port>`, or press
   **Detect**, which reads the system proxy (macOS network
   settings, the Windows proxy setting, the `https_proxy` / `all_proxy`
   environment variables on Linux) and fills in what it found. Leave it empty
   when the proxy program routes everything by itself (TUN / enhanced mode) or
   YouTube is reachable directly.
3. Press **Save**. The first time, the server downloads its two YouTube
   components, yt-dlp and deno (about 80 MB, through that proxy; you can also
   enter a download mirror prefix: what replaces `https://github.com` in the
   download address, so a GitHub accelerator that takes a whole address is
   written `https://<accelerator>/https://github.com`), and checks each file
   against the SHA-256 written in the program. Progress shows in the same section.
4. Press **Test**: it resolves a public test video and answers in plain words
   ("success in 12 s", "can't reach YouTube: check the proxy", "needs sign-in:
   import cookies").
5. **Cookies**: YouTube often asks proxy and data-centre addresses to "sign in
   to confirm you're not a bot". Then import a `cookies.txt` (Netscape format):
   use a **secondary account**, sign in to it in a private browser window, open
   YouTube, export only youtube.com with a cookies.txt export extension, then
   close the private window without signing out (signing out invalidates the
   cookies). The server keeps only the youtube.com and google.com lines, stores
   them in `<data>/youtube/cookies.txt` readable only by you, and shows only
   how many there are. The file is that account's login: do not share it.
6. YouTube changes often. When videos stop playing, press **Check for updates**
   and **Update yt-dlp** (the new version is verified against the release's
   own `SHA2-256SUMS`).

Changes apply without restarting the server: saving restarts only the parts that changed
(a new proxy restarts the YouTube helper alone). YouTube videos, creators and
playlists (series) then work as on the cloud service. "Own node" is for a
YouTube node you run yourself (`secrets/youtube-node.json`).

In Docker, `127.0.0.1` is the container itself: give the proxy as the host's
LAN address (and let the proxy program accept LAN connections), and Detect
only sees the container's environment variables. The components go into
`/data/youtube/bin/` (linux amd64 or arm64; 32-bit ARM is not supported).

## Family and guests

As on the cloud host: Me → let another phone sign in (same account, the
simplest), invites (separate accounts), and guests scanning the TV's QR code.
One shared account per household and the guest code for visitors is the
recommendation.

## Backups and updates

- The data folder is everything you have (except `run/`, which needs no
  backup). The server backs up the database every night after 3 a.m. into
  `backups/` and keeps 14 days (`backup_keep_days` in `config.toml`).
  **`secrets/` holds keys** (and `youtube/cookies.txt` a YouTube login): keep backups like a
  password and do not share them. `youtube/bin/` need not be backed up (it is downloaded again).
- Restore: stop the server, replace the data folder with the backup, start.
- Update: without Docker, `git pull` and restart; with Docker, `git pull` and
  `docker compose up -d --build` (once the image is published:
  `docker compose pull && docker compose up -d`).

## The settings file

The first start writes a commented `config.toml` into the data folder; changes
take effect on restart. The common keys:

| Key | Default | Effect |
| --- | --- | --- |
| `[server] lan_port` | 8080 | Port of the pages |
| `[server] device_address` | empty | What the TVs dial; empty = the address the browser used at first setup |
| `[server] public_port` | 0 | A port for a tunnel (Cloudflare Tunnel, Tailscale serve), e.g. 8081; it has no admin backend, setup page or key sign-in |
| `[devices] allow_internet` | false | Accept TVs from public addresses (only for port forwarding) |
| `[accounts] signup_via_device` | "lan" | Self-registration by scanning an unbound TV: home network only / tunnel too / off |
| `[youtube] mode`, `proxy` | "off", empty | YouTube: off / `local` (this machine, through `proxy`) / `node` (your own node); the console's Settings → YouTube rewrites these two lines and applies them without a restart |

## Common problems

- **The pages do not open**: is the phone on the same Wi-Fi as the server? Was
  the Mac's firewall prompt allowed? If the port is taken, change `lan_port`.
- **The TV says "server did not answer"**: is the server address right (LAN IP,
  no port)? Is another TV of the same model watching (one at a time in the
  beta)?
- **Sign-in does not stick**: open `http://<LAN IP>:8080`; `localhost`, the IP,
  a `.local` name and a tunnel address each have their own sign-in.
- **The in-page scanner cannot open the camera**: browsers allow the camera
  only over HTTPS; scan with the phone's own camera app, or type the code.
- **A copy button does nothing**: the same plain-HTTP limit; select and copy by
  hand.

## Not verified yet

- Every step on Docker, Docker Desktop (Mac, Windows), Synology Container
  Manager, UGREEN and QNAP, and `PUID`/`PGID`.
- The image itself (built in CI; the author's computer has no Docker), and
  Debian's ffmpeg on arm64.
- The web flasher on a real TV (it has not been run against a device yet),
  and `./run.sh --fetch-firmware` against a real release (none is published
  yet).
- The TVs resolving `.local` names; lock-screen media controls over plain HTTP
  on an iPhone.
- Every figure marked "estimated" in section 13 of the design.
- The public playlists on the Channels page were read and parsed on
  2026-10-22 from a Mac outside the mainland network path (through a proxy),
  not from a home connection in mainland China; whether their streams play on
  a TV was not checked.
- YouTube local mode in Docker, on Linux and on Windows (run only on a Mac: the component
  download and the proxy path were tried there; with no cookies YouTube asked for sign-in every
  time through the proxy, so a whole video was not played through it).

## Disclaimer

This wording is a suggestion to be reviewed by someone qualified; it is not
legal advice.

> This project is open-source software for private, non-commercial use in
> your own home. It does not include, host, sell or recommend any channel,
> stream, video, account or proxy. Everything it plays is fetched by your own
> server from sources you add yourself, and you are responsible for having the
> right to access them and for following the terms of the services you use and
> the laws where you live. Do not use it to redistribute content to others. The
> software is provided "as is", without warranty of any kind (MIT licence).
>
> The public playlists listed on the Channels page are third-party lists found on the
> internet; their content is the responsibility of their maintainers. This project
> does not provide or vouch for any live stream; add only what you are entitled to watch.

- Add only sources you are entitled to watch, for your own household. Do not
  share your server or its streams with people outside it, and do not make it
  public.
- Video sites' terms apply to you when the server fetches their content on your
  behalf; cookies are your own account's and stay on your machine.
