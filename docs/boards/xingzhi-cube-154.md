<p align="right">
  <a href="xingzhi-cube-154.zh_CN.md">简体中文</a> · <strong>English</strong>
</p>

# Xingzhi Cube 1.54 board

The same TV firmware also builds for the "Xingzhi Cube 1.54 TFT ML307", an
ESP32-S3 board sold as a Xiaozhi AI toy (1.54-inch screen, 4G module, battery,
four keys on the right side). Design notes:
[`docs/superpowers/specs/2026-10-08-xingzhi-cube-port-design.md`](../superpowers/specs/2026-10-08-xingzhi-cube-port-design.md).

## How it differs from the AI Passport

| | AI Passport | Xingzhi Cube 1.54 |
| --- | --- | --- |
| Chip | ESP32-C3 | ESP32-S3, 8 MB flash |
| Screen | 240×320, picture 320×180 (landscape) | 240×240, picture 240×180 (16:9 shows as 240×135) |
| Audio | ES8311 codec | I²S straight to the amplifier, software volume |
| Keys | 3 on one ADC pin | 3 GPIO keys + a hardware reset/power key |
| Battery | CW2017 fuel gauge | ADC voltage, rough percentage |

Pins and board facts live in
[`components/bsp/include/boards/xingzhi_cube_154.h`](../../components/bsp/include/boards/xingzhi_cube_154.h);
build settings in [`sdkconfig.defaults.xingzhi-cube-154`](../../sdkconfig.defaults.xingzhi-cube-154).

## Build

With ESP-IDF 5.5.3 activated:

```bash
./tools/validate.sh --xingzhi
```

The image is written to `build/Xingzhi-Cube-154-tv-public.bin`.

## Flash

Enter download mode if the board does not answer: hold **BOOT** (third key from
the top) and tap the bottom key. On the very first flash, erase the chip first,
because the Xiaozhi firmware's NVS sits inside this firmware's NVS range:

```bash
python -m esptool --chip esp32s3 -p /dev/cu.usbmodem1101 erase_flash
```

```bash
python -m esptool --chip esp32s3 -p /dev/cu.usbmodem1101 -b 921600 write_flash 0x0 build/Xingzhi-Cube-154-tv-public.bin
```

## Server

The Passport's server sends 320×180 and the Xingzhi firmware accepts only
240×180, so run a second media server from the same `tv-server` program:

```bash
tools/start_xingzhi_media.sh
```

It listens on port **8098** and prints the address to enter on the device's
setup page (`<host>.local:8098` or `<LAN IP>:8098`). The Passport instance keeps
ports 8096 and 8097. A device pointed at the wrong port connects and is refused
with `CONFIG field width: server=320 device=240` in its log.

## Cloud server

The media server can also run on an always-on Linux host so the computer does
not need to be on. Measured on a 2-core Alibaba Cloud Linux 3 host with 3 Mbps
of outbound bandwidth: 15 fps, full-rate audio, ffmpeg at about 12% of one core.

1. **Python 3.9+.** The code uses only the standard library. On Alibaba Cloud
   Linux 3 / RHEL 8, `dnf install python3.11` installs beside the system Python.
   The released `tv-server-linux-x86_64` needs glibc 2.39 and does not run there.
2. **ffmpeg.** Use a dynamically linked build such as BtbN's
   `ffmpeg-n8.1-latest-linux64-gpl` (glibc 2.28+). The fully static build that
   `tools/ffmpeg_fetch.py` downloads crashed resolving host names on that host.
3. Copy `server/`, `tools/` and your `channels.txt` to the host, create a
   `tvserver` system user and a pairing-token file readable only by it.
4. Install [`packaging/systemd/tv-xingzhi.service`](../../packaging/systemd/tv-xingzhi.service).
   `TV_ALLOW_PUBLIC=1` lets devices connect from public addresses; the server
   refuses to start with it unless a token is configured. `TV_FPS` and
   `TV_RATE_START` keep the stream inside a small uplink.
5. Open the port in **both** the cloud provider's security group and the host
   firewall (`firewall-cmd --permanent --add-port=8098/tcp`).
6. Build the device firmware with the same token: create the git-ignored
   `main/av_private_config.h` (`AV_PAIRING_TOKEN`, optionally a default
   `AV_SERVER_IPV4`/`AV_SERVER_PORT`) and build without `AV_PUBLIC_BUILD`. Then
   enter `<server-ip>:8098` on the setup page.

The token travels in plain text: it keeps scanners out but is not encryption.
To change channels, edit `channels.txt` on the host; the server re-reads it at
the next channel change (`TV_RELAY_CHANNELS_FILE` is re-read the same way).

## Creator channels

A video relay on the same host turns each followed Bilibili creator into a
channel that plays their newest unwatched upload, then the next, resuming where
it stopped. Design and measurements:
[`docs/superpowers/specs/2026-10-09-creator-channels-and-cast-design.md`](../superpowers/specs/2026-10-09-creator-channels-and-cast-design.md).

1. Install [`packaging/systemd/tv-relay.service`](../../packaging/systemd/tv-relay.service)
   and put a random key of at least 16 characters in `/etc/ai-passport-tv/relay-key`
   (mode 0640, group `tvserver`). It listens on loopback only: streams on 8099
   for the media server, the page on 8100.
2. Add `TV_RELAY_CHANNELS_FILE` to the media server's unit (the template has it);
   creator channels are listed before `channels.txt`.
3. Proxy the page from the website: copy
   [`packaging/nginx/tv-relay.conf`](../../packaging/nginx/tv-relay.conf) into
   the site's nginx configuration (on a BT panel host, the site's
   `extension/<site>/` folder), test and reload nginx.
4. Sign a phone in once (see Phone remote below) and paste a creator's space
   link or any of their video links on the Creators tab. The channel appears
   after the device's next channel change.

The same page casts a single video: paste a Bilibili video link and the device
switches to "My cast" (the first channel, always listed) within a second, plays
it, and returns to the channel it was on when it ends. The relay records this in
`relay-events.json`; the media server reads it once a second
(`TV_RELAY_EVENTS_FILE`) and sends a `SWITCH` packet, but only to firmware whose
handshake says it understands one. Older firmware still plays "My cast" when
picked by hand.

**Series** (cartoons, shows) go below the creators on the same tab: paste a
Bilibili bangumi link, a multi-part video, a collection or a YouTube playlist
(a link with `list=`), with an optional channel name. Each becomes a channel `sr01`… in the Cartoons & films group that
plays the episodes in order and comes back to the episode it left. Bangumi keep
only their free episodes. The relay refuses a new channel when the device's
channel list would outgrow its CONFIG packet; remove a channel first.

**Picking an episode.** Tapping a series on that tab (or "Episodes" under the
player while a series plays) lists its episodes: watched ones dimmed, the one in
progress highlighted and scrolled to. Tapping an episode makes the series carry
on from there: earlier episodes count as watched, that one and the later ones as
unwatched, and other episodes' progress is dropped. The episode starts from the
beginning unless it is the one already in progress, which resumes. If the device
is on that channel, the relay cuts the stream and the media server reopens it at
the chosen episode; on another channel it gets a `switch` event; offline, the
choice waits for the next time the channel opens. While a series plays, the
player has "Previous episode" (from the beginning) and its "Next" is the next
episode; the screen shows the episode number (n of N) before the title. API:
`GET /api/series/<sr id>/episodes` returns `{ok, id, name, total, next, playing,
episodes: [{n, title, duration, state: watched|current|left, position?}]}`
(`next` is the episode the channel opens at, `playing` the one on the device or
0); `POST /api/series/<sr id>/play {"episode": n}` and `POST /api/previous` answer
`{ok, at, episode, title}` plus `cut: true` when the device was already on the
channel, or a `note` instead of switching (device offline, firmware without
remote switching, or that episode already playing). No firmware change: the
device draws no hint box for an episode change.

Bilibili answers 412 to cloud addresses for its pages and signed APIs, so the
relay uses the unsigned web API rather than yt-dlp.

**YouTube** links work on the same page, for creators and casts. The mainland
host never contacts YouTube: a node on an overseas host
([`server/relay/ytnode.py`](../../server/relay/ytnode.py),
[`packaging/systemd/yt-relay.service`](../../packaging/systemd/yt-relay.service))
runs yt-dlp and transcodes to about 400 kbit/s, and the relay reads it over HTTPS
with a pinned self-signed certificate and a key (`--cstone` JSON with `url`,
`key`, `cert`). Allow the node's port only from the relay's host. The node needs
yt-dlp, deno (yt-dlp's JavaScript runtime) and ffmpeg, and in practice a cookies
file from a secondary Google account exported in a private window: YouTube asked
the overseas host to sign in for every client tried. Resolving a video takes about
18 s on one core, so the node caches it for 30 minutes; the first picture of a
new YouTube video takes 20-40 s.

## Phone remote

The relay's page is a remote for the device, at `https://<site>/tv/`. Design:
[`docs/superpowers/specs/2026-10-10-grouped-channels-and-remote-design.md`](../superpowers/specs/2026-10-10-grouped-channels-and-remote-design.md).

- **Sign-in.** Open `https://<site>/tv/?k=<key>` once, or scan the code on the
  device's status page (`https://<site>/TV/R/<code>`). Either sets a 180-day
  session cookie; afterwards `https://<site>/tv/` opens directly. Add the page to
  the home screen from the signed-in page (iPhone: Safari, Share, Add to Home
  Screen); its address carries the session, because a home-screen app does not
  share Safari's cookies. "Issue a new QR code" on the Creators tab ends every
  session.
- **Remote tab:** what is playing, the groups, the group's channels; a tap
  switches the device within about a second. "Adjust groups" moves a live
  channel to another group (`channel-groups.json`).
- **Cast tab:** paste a Bilibili or YouTube link; the last 50 casts are kept on
  the server and replay with a tap.
- **Creators tab:** add and remove creators; the help page link.
- **Groups** are worked out from channel names (`server/channel_groups.py`):
  My cast, Creators, CCTV, Satellite, HK/Macau/Taiwan, International, Music,
  Cartoons & films, Other. The media server sends the names once and an index
  per channel in CONFIG.
- **Server settings.** The relay unit adds `--main-channels`, `--groups-out`,
  `--now-playing`, `--remote-url-out` and `--public-url`; the media server unit
  adds `TV_CHANNEL_GROUPS_FILE`, `TV_NOW_PLAYING_FILE` and `TV_REMOTE_URL_FILE`
  (both templates have them). nginx needs the `/tv/` and `/TV/R/` locations in
  `packaging/nginx/tv-relay.conf`.

- **Playback and device controls.** On creator channels and casts the Remote tab
  has a progress bar, back/forward 30 s, pause/resume and next: the relay cuts
  the stream it serves and the media server's decoder reopens it at the new
  place; a pause holds the device on a PAUSED frame with silence for up to
  30 minutes. Volume and brightness sliders show the device's values: the
  firmware reports them in HELLO and in a STATUS packet (kind 9) on every change,
  and takes the phone's values in an AV_SWITCH carrying `volume` or `brightness`.
  Design: [`docs/superpowers/specs/2026-10-10-playback-and-device-controls-design.md`](../superpowers/specs/2026-10-10-playback-and-device-controls-design.md).
- **Pets.** The Pet line on the Remote tab opens the My pets page: pick one of the
  eleven pets and apply it; the device stores it and shows it happy for two
  seconds. `POST /api/pet` writes a `pet` event; the media server sends
  `AV_SWITCH {"pet": i}` only to firmware whose HELLO carries `pet`, and the
  firmware reports its pet in HELLO and STATUS. Seek and next also write
  `seek`/`next` events, sent as `{"seek": s}`/`{"next": 1}` to firmware whose
  HELLO says `"hint": 1`, which draws a box until the picture is back.
- **Upkeep.** The YouTube node checks a test video every 6 hours and the page
  warns when its cookies need replacing; `packaging/yt-relay/yt-dlp-update.timer`
  installs a new yt-dlp weekly only after it resolves the test video.

Anyone holding the key link, the device's code or a signed-in phone can control
the device and edit its creators.

## Screens

The device UI follows a design handed over from Claude Design (a warm ink
palette, pixel icons and eleven pets); the record of what was built is
[`docs/superpowers/specs/2026-10-10-device-ui-redesign-design.md`](../superpowers/specs/2026-10-10-device-ui-redesign-design.md).
`tools/render_ui/render_ui.sh out.png` draws every screen on a computer with the
firmware's own drawing code and font, for checking a layout without flashing.

- The top bar (rows 0-29) always shows a label at the left -- the channel number
  and name while playing, otherwise the page's name -- and signal, battery and
  charge at the right. The fill and number turn vermilion below 20 %, the
  number blinks below 10 %, and a bolt appears while charging (GPIO38).
- Menus and pages cover the whole picture area (rows 30-209) on an opaque panel;
  the bottom bar (rows 210-239) carries the list's frame and receive rates or
  the page's key hint.
- Over the picture: the volume box (and the brightness box when the phone sets
  it), the channel banner, the seek box and the pause band.
- With no picture: joining Wi-Fi, connecting to the server (first time after
  boot), opening a channel ("switching"), or the server did not answer. The pet
  bobs on these screens.
- The chosen pet is stored in NVS (`avsettings/pet`); the status page's long
  press on the second key opens the picker.
- **Audio-only mode** plays sound with the pet on screen and no picture
  ([design](../superpowers/specs/2026-10-10-audio-only-mode-design.md)). A
  double-click on OK, OK on the status page, or the phone's switch turns it on
  or off; the screen shows the pet at 6x, standing on the loudness bars and moving with the sound, with notes, loudness bars
  and twinkling stars, and asleep once the screen dims; it is stored
  (`avsettings/audio`) and the device redials, so the server stops sending
  pictures (about 8 KB/s instead of 200). Sound is then what counts as a live
  channel, and the backlight dims to the lowest step after 10 s untouched.
- Setup (status page, long-press the top key) is a set of pages turned with the
  top key (previous) and the second key (next); long-press OK leaves:
  1. a Wi-Fi QR code that a phone's **system camera** uses to join the hotspot
     (WeChat and Alipay cannot join Wi-Fi from a QR code);
  2. a QR code of the setup page `http://192.168.4.1`;
  3. a QR code of the help page, when `AV_HELP_URL` is set;
  4. the hotspot, setup address, server and connection state as text, and the
     time left of the 15-minute setup window.
- On the setup page, tapping a remembered network fills its name; leaving the
  password empty keeps the stored one. Stored passwords stay on the device.
- The status page shows the signal in dBm. HELLO reports the network, signal,
  channel and transmit power, STATUS the signal every 30 s, and the media
  server logs them (`device wifi ...`, `device rssi=...`). The transmit power is
  11 dBm by default (`sdkconfig.defaults.xingzhi-cube-154`; 20 dBm gave poor
  throughput near the router); the setup page's Advanced tab can raise it for a
  device far from its router, stored in NVS and applied after Wi-Fi starts.
- HELLO also carries the uptime and the reset reason; the media server logs
  `device boot reason=...` when the uptime is below what the last HELLO plus
  the time since would give, so a reboot is told apart from a dropped link
  even after a long session.
- The status page carries the phone remote's QR code when the server sends
  one (`remote_url` in CONFIG, a version-2 code at 3 px a module); otherwise a
  small help-page QR code when `AV_HELP_URL` is set.
- `AV_HELP_URL` comes from the git-ignored `main/av_private_config.h`; public
  builds leave it empty. Keep it short and upper case (for example
  `HTTPS://EXAMPLE.COM/TV/`): that keeps it in a version-1 QR code, the only size
  that fits on the status page. The page itself is
  [`packaging/help/index.html`](../../packaging/help/index.html); copy it to the
  matching folder on a web host.
- Up to ten Wi-Fi networks are remembered; the device joins whichever is in range.
  Channels 1-13 are scanned. While no stored network is found the screen says so
  and names the network; double-click and long-press the top key opens setup.

## Keys while playing

| Key | Click | Long press |
| --- | --- | --- |
| Top (printed VOL−) | volume +10% | next channel in the same group |
| Second (printed VOL+) | volume −10% | previous channel in the same group |
| Top 3 (BOOT) | channel list | brightness |
| Top or second, double-click | status page (there, long-press the top key for Wi-Fi setup, the second key for the pet picker, click OK for audio-only) | |
| Top 3 (BOOT), double-click | audio-only on or off | |
| Bottom | hardware reset; long press powers on/off | |

The channel list has two levels when the server sends groups: it opens on the
groups at the playing channel's group; OK opens a group, a long press on OK goes
back a level and then closes. A dead channel is skipped to the next one in its
group.

A click steps the volume and a long press the channel, because these are the
case's volume keys (`BSP_ARROW_CLICK_IS_VOLUME`); the Passport now does the same.
Up and down follow the keys' physical order rather than the printed VOL−/VOL+,
which read backwards in use.

## Board-specific fixes, all measured on one device

- **Wi-Fi transmit power is capped at 11 dBm.** At 20 dBm this board's supply
  sags and raw TCP collapsed to 15–50 KB/s; 13 dBm gave about 230 KB/s, 11 dBm
  about 400 KB/s, 8 dBm fell back to 15 KB/s. Measured 2026-10-08 at about
  −50 dBm RSSI on a crowded 2.4 GHz channel 11; another location may need a
  different value.
- **The 4G module is put in airplane mode** (`AT+CFUN=0`) at boot. With a
  rejected SIM it kept retrying attach, which contributed to brownouts.
- **The Wi-Fi event task stack is 4096 bytes.** The shared 2304 overflowed on
  the S3 right after setup.
- `bsp_power` logs the previous reset reason as a number at boot (1 = reset
  key or power-on, 4 = crash, 9 = brownout, 11 = USB), because the ROM's own
  reset line is lost while USB re-enumerates.

## Not verified

- The battery divider ratio (assumed 1:2) and therefore `bsp_battery_mv()`; the
  percentage reads high while charging.
- Long-term stability beyond about ten minutes of play, and behaviour on other
  Wi-Fi networks.
- The 4G module's power draw in airplane mode, and whether it should instead be
  held in reset.
- `tools/start_xingzhi_media.sh` on Windows (macOS only).
