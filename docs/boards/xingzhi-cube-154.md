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
4. Open `https://<site>/TV/cast/?k=<key>` on a phone and paste a creator's
   space link or any of their video links. The channel appears after the
   device's next channel change.

The same page casts a single video: paste a Bilibili video link and the device
switches to "My cast" (the first channel, always listed) within a second, plays
it, and returns to the channel it was on when it ends. The relay records this in
`relay-events.json`; the media server reads it once a second
(`TV_RELAY_EVENTS_FILE`) and sends a `SWITCH` packet, but only to firmware whose
handshake says it understands one. Older firmware still plays "My cast" when
picked by hand.

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
new YouTube video takes 20-40 s. The key is the only
protection: anyone with the link can add and remove creators.

## Status bar and setup screen

- While playing, the top band shows signal bars, a battery icon and the charge;
  the fill turns red below 20 % and a bolt appears while charging (GPIO38).
- Setup (status page, long-press the top key) is a set of pages turned with the
  top key (previous) and the second key (next); long-press OK leaves:
  1. a Wi-Fi QR code that a phone's **system camera** uses to join the hotspot
     (WeChat and Alipay cannot join Wi-Fi from a QR code);
  2. a QR code of the setup page `http://192.168.4.1`;
  3. a QR code of the help page, when `AV_HELP_URL` is set;
  4. the hotspot, setup address, server and connection state as text.
- The status page carries a small help-page QR code when `AV_HELP_URL` is set.
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
| Top (printed VOL−) | volume +10% | next channel |
| Second (printed VOL+) | volume −10% | previous channel |
| Top 3 (BOOT) | channel list | brightness |
| Any, double-click | status page (long-press the top key there for Wi-Fi setup) | |
| Bottom | hardware reset; long press powers on/off | |

Click and long press are swapped relative to the Passport because these are the
case's volume keys (`BSP_ARROW_CLICK_IS_VOLUME`). Up and down follow the keys'
physical order rather than the printed VOL−/VOL+, which read backwards in use.

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
