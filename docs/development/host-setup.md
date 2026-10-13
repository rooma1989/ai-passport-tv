<p align="right">
  <a href="host-setup.zh_CN.md">简体中文</a> · <strong>English</strong>
</p>

# Production host: units, drop-ins, install order, backups

Every systemd unit and drop-in the production deployment runs, where its
template is in [`packaging/systemd/`](../../packaging/systemd/), what it is for,
the order to install them in, and what to back up. `<site>` stands for the
website's host name. The templates were reconciled with the hosts' `systemctl cat`
output of 2026-10-12; [Where the host differs](#where-the-host-differs-from-the-templates)
lists what was different then.

Two hosts:

- **Mainland host** (Alibaba Cloud Linux 3, 2 cores, 3 Mbps out, little memory):
  the video relay, one media server per device type, muse-asr (speech
  recognition), nginx for the website.
- **Overseas node**: the YouTube node only. The mainland host never contacts
  YouTube.

## Units

| Unit | Host | Template | What it does |
| --- | --- | --- | --- |
| `tv-relay.service` | mainland | [`tv-relay.service`](../../packaging/systemd/tv-relay.service) | Video relay: creator and series channels, casting, the phone remote and PC console (behind nginx), accounts in multi-user mode. Loopback only: streams on 8099, pages and API on 8100. |
| `tv-xingzhi.service` | mainland | [`tv-xingzhi.service`](../../packaging/systemd/tv-xingzhi.service) | Media server for the Xingzhi Cube (240x180, 15 fps, port 8098). |
| `tv-passport.service` | mainland | [`tv-passport.service`](../../packaging/systemd/tv-passport.service) | Media server for the AI Passport beside Muse (320x180, 12 fps, port 8097, data in `/var/lib/ai-passport-tv-passport`). |
| `muse-asr.service` | mainland | muse-gadget-sdk repository (`esp32/tools/muse_asr`), not this one | SenseVoice speech recognition and text-to-speech for Muse gadgets; the TV media servers use it for voice control. |
| `muse-asr-warm.service` + `.timer` | mainland | [`muse-asr-warm.service`](../../packaging/systemd/muse-asr-warm.service), [`muse-asr-warm.timer`](../../packaging/systemd/muse-asr-warm.timer) | Recognises a short clip every 2 minutes so the model stays in memory. The media server waits at most 6 s for recognition; on the memory-tight host a model left idle answered the first utterance slower than that. |
| `yt-relay.service` | overseas | [`yt-relay.service`](../../packaging/systemd/yt-relay.service) | YouTube node (`server/relay/ytnode.py`): yt-dlp and ffmpeg, HTTPS with a pinned self-signed certificate and a key. |

## Drop-ins

Install each as `/etc/systemd/system/<unit>.service.d/<name>.conf`. The template
directories in `packaging/systemd/` are named like the host's.

| Drop-in on the host | Template | What it does | Needed |
| --- | --- | --- | --- |
| `tv-relay.service.d/passport-events.conf` | [`tv-relay.service.d/passport-events.conf`](../../packaging/systemd/tv-relay.service.d/passport-events.conf) | `ReadWritePaths=` for the Passport's data directory, where the single-user relay writes that `--extra-device`'s now-playing and events files. | With the Passport, single-user relay |
| `tv-relay.service.d/tenancy.conf` | [`tenancy/tv-relay.conf`](../../packaging/systemd/tenancy/tv-relay.conf) | Multi-user relay: replaces `ExecStart` with the `--db` command line. Deleting it is the rollback. | After the multi-user cut-over ([runbook](tenancy-phase1-runbook.md)) |
| `tv-xingzhi.service.d/voice.conf` | [`tv-xingzhi.service.d/voice.conf`](../../packaging/systemd/tv-xingzhi.service.d/voice.conf) | Voice control: `TV_ASR_SOCKET`, `TV_RELAY_KEY_FILE`, `TV_RELAY_DEVICE=d0`, `After=muse-asr.service`. `TV_VOICE_LOG_TEXT=1` is in it commented out: on only while tuning. | For voice |
| `tv-xingzhi.service.d/tenancy.conf` | [`tenancy/tv-media.conf`](../../packaging/systemd/tenancy/tv-media.conf) | Multi-user media server: `TV_DB`, `TV_DEVICES_DIR`. | After the cut-over |
| `tv-passport.service.d/relay-events.conf` | [`tv-passport.service.d/relay-events.conf`](../../packaging/systemd/tv-passport.service.d/relay-events.conf) | `TV_RELAY_EVENTS_FILE` in the Passport's data directory: casts and remote switches reach the Passport. | With the single-user relay |
| `tv-passport.service.d/voice.conf` | [`tv-passport.service.d/voice.conf`](../../packaging/systemd/tv-passport.service.d/voice.conf) | As the Xingzhi's, with `TV_RELAY_DEVICE=Passport`. | For voice |
| `tv-passport.service.d/tenancy.conf` | [`tenancy/tv-media.conf`](../../packaging/systemd/tenancy/tv-media.conf) | As the Xingzhi's; also gives this unit write access to `/var/lib/ai-passport-tv` (the database's WAL files). | After the cut-over |
| `muse-asr.service.d/tv-voice.conf` | [`muse-asr.service.d/tv-voice.conf`](../../packaging/systemd/muse-asr.service.d/tv-voice.conf) | The local socket `/run/muse-asr/asr.sock` (0660, group `tvserver`) the media servers send audio to. The group is the only credential. | For voice |
| `yt-relay.service.d/memory.conf` (overseas) | [`yt-relay.service.d/memory.conf`](../../packaging/systemd/yt-relay.service.d/memory.conf) | `MemoryMax=600M`: yt-dlp with deno peaks near 330 MB, and two ffmpeg streams on top overran 400M. | Only with a base unit older than the template, which already has 600M |

Deleting `voice.conf` (and `systemctl daemon-reload`) turns voice off for that
unit; deleting the `tenancy.conf` files is the multi-user rollback. The two
Passport event drop-ins are not optional while the single-user relay serves the
Passport: the base `tv-relay.service` names it in `--extra-device`.

## Dependencies

Mainland host:

- `python3.11` (`dnf install python3.11`; it installs beside the system Python).
- **numpy** (optional). The default picture degrade, `steady` (`server/degrade.py`),
  does not need it; only the older comparison modes `perceptual`, `perceptual_2d`
  and `mosaic` do. Do not run production on `TV_DEGRADE=ladder`: it shifts colours
  from frame to frame (blue/yellow flicker was seen on a device), and the media
  server logs one line starting `WARNING: TV_DEGRADE=ladder` at start-up when it is
  set. To install numpy anyway: Alibaba Cloud Linux 3's `python3.11` has no pip:

  ```sh
  dnf install python3.11-pip
  python3.11 -m pip install "numpy<3"      # as root, so tvserver can import it
  sudo -u tvserver python3.11 -c "import numpy; print(numpy.__version__)"
  ```

- A dynamically linked ffmpeg at `/opt/ai-passport-tv/ffmpeg-btbn-8.1` (BtbN's
  `ffmpeg-n8.1-latest-linux64-gpl`; the static build crashed resolving host names
  there; see [`xingzhi-cube-154.md`](../boards/xingzhi-cube-154.md#cloud-server)).
- `sqlite3` (backups), `curl` (the warm-up unit), nginx.
- muse-asr's own virtualenv and model under `/opt/muse-asr` (its repository's instructions).

Overseas node: `python3`, yt-dlp, deno and ffmpeg
([`xingzhi-cube-154.md`](../boards/xingzhi-cube-154.md#creator-channels)).

## Install order

Users and files first: system users `tvserver` (relay and media servers),
`museasr` (muse-asr; the drop-in adds it to group `tvserver`), `ytrelay` on the
overseas node. The code in `/opt/ai-passport-tv`, data in
`/var/lib/ai-passport-tv` and `/var/lib/ai-passport-tv-passport` (owner
`tvserver`), secrets in `/etc/ai-passport-tv` (pairing `token` readable only by
`tvserver`; `relay-key` mode 0640 group `tvserver`; `cstone.json` and the node's
certificate it names).

1. **numpy**, as above.
2. **muse-asr** with `muse-asr.service.d/tv-voice.conf`; check with
   `sudo -u tvserver curl --unix-socket /run/muse-asr/asr.sock http://x/health`.
   Then the warm-up: put a raw clip (16 kHz mono PCM16 little-endian, about 1 s)
   at `/usr/local/share/muse-asr-warm.pcm`, readable by `tvserver`, install both
   `muse-asr-warm` files and `systemctl enable --now muse-asr-warm.timer`.
3. **tv-relay** with `passport-events.conf`. Replace `<site>` in its
   `--public-url`.
4. **tv-xingzhi** and **tv-passport** with their drop-ins (`voice.conf`, and the
   Passport's `relay-events.conf`). Open 8097 and 8098 in the cloud security
   group and the host firewall.
5. **nginx**: [`packaging/nginx/tv-relay.conf`](../../packaging/nginx/tv-relay.conf).
6. **Multi-user**: the `tenancy.conf` drop-ins, only as part of the
   [runbook](tenancy-phase1-runbook.md)'s cut-over (after the migration).
7. **Overseas node**: `yt-relay.service` (and `memory.conf` only for an older base unit).
8. **Product website** (optional): [below](#product-website).

From a copy of this repository on the host (run as root):

```sh
S=packaging/systemd D=/etc/systemd/system
install -m 0644 $S/tv-relay.service $S/tv-xingzhi.service $S/tv-passport.service \
    $S/muse-asr-warm.service $S/muse-asr-warm.timer $D/
sed -i 's|https://<site>|https://YOUR-SITE|' $D/tv-relay.service
for u in tv-relay tv-xingzhi tv-passport muse-asr; do
    install -d $D/$u.service.d && install -m 0644 $S/$u.service.d/*.conf $D/$u.service.d/
done
systemctl daemon-reload
systemctl enable --now muse-asr-warm.timer
systemctl restart muse-asr tv-relay tv-xingzhi tv-passport   # this order
```

Check: `systemctl cat <unit>` shows the base unit and each drop-in; each media
server logs `voice: on, relay on, transcripts not logged` (multi-user:
`relay per device`) and no `WARNING: TV_DEGRADE=ladder`; `systemctl list-timers muse-asr-warm.timer`
shows the next run.

## Product website

The product website is static: [`packaging/site/`](../../packaging/site/)
(`index.html` and `assets/`, no build step, no third-party requests). It is
served at the root of its own host name, `<site>` in this section.

- Copy `packaging/site/index.html` and `packaging/site/assets/` into that host
  name's web root, and the help page
  [`packaging/help/index.html`](../../packaging/help/index.html) to
  `TV/index.html` under the same root, so `/TV/` works there as on the main site.
- The page links to the relay's pages with root-relative paths, so the website's
  host name has to serve them as the main site does: `/tv/` (phone remote,
  `/tv/console`, `/tv/wifi`), `/TV/R/`, `/TV/B/`, `/TV/L/`, `/TV/W`, and `/TV/`
  (the help page). Include
  [`packaging/nginx/tv-relay.conf`](../../packaging/nginx/tv-relay.conf) in that
  server block. [`packaging/nginx/tv-site.conf`](../../packaging/nginx/tv-site.conf)
  is a template of the whole server block with `<site>` and `<root>` placeholders.
- Sign-in cookies are per host name (`Path=/tv/`, no `Domain`). The QR codes,
  invite links and the console's sign-in code use the relay's `--public-url`.
  While that is still the main site, a phone signed in there is not signed in on
  the website's host name; change `--public-url` to the website's host name to
  make it the one address (keep the main site's locations so printed or saved
  links keep working).
- The screen pictures in `assets/screen-*.png` are cut from the firmware's own
  drawing: run `python3 tools/render_ui/site_shots.py` (needs Pillow) after a UI
  change.

## Where the host differs from the templates

As compared on 2026-10-12, settings only (comments aside):

| Unit | Host | Templates | Why |
| --- | --- | --- | --- |
| `tv-relay.service` | the real site name in `--public-url` | `<site>` | The domain stays out of the repository. |
| both `voice.conf` | `TV_VOICE_LOG_TEXT=1` on | commented out | It was on for threshold tuning; it logs what people say, so it is off by default. Turn it off on the host when tuning is done. |
| `yt-relay.service` | `MemoryMax=400M`, raised by `memory.conf` | `MemoryMax=600M` in the base unit | The template was corrected after the host's unit was installed; the effective limit is the same. |
| `tv-xingzhi.service` | voice variables in `voice.conf` | same now | The repository had them in the base unit until this page; they moved to the drop-in so deleting one file turns voice off, as on the host. |
| `tv-passport.service` | header says "install as tv-xingzhi.service"; "15 fps" comment above `TV_FPS=12`; a "Cast events" comment with no variable under it | comments corrected | Copied from tv-xingzhi by hand. Settings are identical. |
| `tenancy.conf` (media) | comment says the voice variables stay in the base unit | says `voice.conf` | Comment only. |

`muse-asr.service` itself is not templated here (its repository owns it); the
host's sets `MUSE_ASR_LISTEN=127.0.0.1:8102`, `MemoryMax=900M` and `Nice=5`.

## Backups

Back these up off the host, readable only by the operator. Never copy them into
this repository, an issue or a log.

| File | Why it matters |
| --- | --- |
| `/var/lib/ai-passport-tv/tv.db` | Accounts, devices, per-user state (multi-user). Copy it only with `sqlite3 tv.db ".backup <file>"`; a plain copy of a WAL database can miss recent writes. Nightly and before every migration or upgrade ([runbook, Backups](tenancy-phase1-runbook.md#4-backups)). |
| `/var/lib/ai-passport-tv/wifi-key` | The relay's X25519 key for sealing Wi-Fi changes, next to the database. Losing it makes devices refuse Wi-Fi changes until setup is completed on each device once. Back it up with the database. |
| `/etc/ai-passport-tv/relay-key` | The relay's key: phone key links, the media servers' relay calls (`TV_RELAY_KEY_FILE`). |
| `/etc/ai-passport-tv/token` | The pairing token; private firmware builds carry the same value (`AV_PAIRING_TOKEN` in the git-ignored `main/av_private_config.h`). A new token means rebuilding and reflashing those devices. |
| `/etc/ai-passport-tv/cstone.json` and the certificate (`.pem`) it names | The YouTube node's address, key and pinned certificate. |
| `/etc/muse-asr/key` | muse-asr's key for the Muse gadgets' encrypted interface. |
| Overseas: `/etc/yt-relay/{key,cert.pem,tls-key.pem}`, `/var/lib/yt-relay/cookies.txt` | The node's key and TLS identity (the relay pins this certificate) and the YouTube sign-in cookies. |
| `/var/lib/ai-passport-tv/channels.txt` and the rest of both data directories | The channel table and, single-user, `relay-state.json`; small. |

The units and drop-ins themselves need no backup beyond this repository, once
`systemctl cat` on the host matches the templates.
