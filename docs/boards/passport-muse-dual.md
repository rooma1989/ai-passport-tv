<p align="right">
  <a href="passport-muse-dual.zh_CN.md">简体中文</a> · <strong>English</strong>
</p>

# AI Passport: the player beside the Muse gadget firmware

The AI Passport can carry two firmwares: the Muse gadget firmware
(github.com/facebookincubator/muse-gadget-sdk, profile `ai-passport`) in
`ota_0`, and this player in `ota_1`. Each boots the other by setting the boot
slot and restarting:

- Muse to the player: Settings > **TV** (Muse built with `CONFIG_MUSE_TV_SLOT=y`).
- The player to Muse: open the status page (double-click an arrow) and
  **double-click OK**. The page's key hint says so.

The two share Muse's partition table (`partitions-muse-dual.csv`, a copy of
Muse's `partitions_muse_8mb.csv`, at offset 0x10000) and its NVS partition, each
with its own namespaces. Muse's bootloader stays: only the player's app image is
written.

## What the player does differently (`CONFIG_AV_MUSE_DUAL`)

- **Never erases NVS.** The Wi-Fi component's erase-on-init-failure would wipe
  Muse's pairing; it is skipped.
- **Marks itself valid** (`esp_ota_mark_app_valid_cancel_rollback`) right after
  the display and Wi-Fi stack are up, before any of the player's own restarts:
  Muse's bootloader has rollback on and would otherwise boot Muse again.
- **Takes Muse's network** (namespace `homehub`, keys `ssid`/`password`) on a
  first boot with none of its own, and **Muse's volume** (namespace `muse`, key
  `volume`) until a volume is set in the player.
- The status page's key hint mentions the way back.

## Build, sign, flash

Build with ESP-IDF 5.5.3 and a private build (the server address and pairing
token come from `main/av_private_config.h`; `AV_SERVER_PORT` is the Passport
instance's port):

```sh
SDKCONFIG_DEFAULTS="sdkconfig.defaults;sdkconfig.av-prototype;sdkconfig.muse-dual" \
  idf.py -B build-muse-dual -D IDF_TARGET=esp32c3 -D AV_PUBLIC_BUILD=OFF \
  -D SDKCONFIG=build-muse-dual/sdkconfig build
```

Muse checks an image's signature when it switches to it
(`CONFIG_SECURE_SIGNED_ON_UPDATE`), so sign the app with Muse's key (espsecure
from ESP-IDF 6, Secure Boot v2 RSA scheme), then write it to `ota_1` only:

```sh
espsecure sign-data --version 2 --keyfile <muse-gadget-sdk>/esp32/dev_signing_key.pem \
  --output build-muse-dual/tv-ota1-signed.bin build-muse-dual/FoloToy-AI-Passport.bin
python -m esptool --chip esp32c3 -p PORT write-flash 0x400000 build-muse-dual/tv-ota1-signed.bin
```

Don't flash this build's bootloader, partition table or `ota_data_initial.bin`.
`tools/verify_firmware.py` assumes the player's own layout (factory at
0x10000) and does not apply to this one.

## Server

The geometry is fixed per server process and one device is served at a time,
so the Passport has its own instance next to the Xingzhi one: `tv-passport`, a
copy of `tv-xingzhi` with `TV_GEOMETRY=320x180`, `TV_FPS=12`, port 8097, its own
`TV_DATA_DIR`, now-playing file and relay events file
(`TV_RELAY_EVENTS_FILE=/var/lib/ai-passport-tv-passport/relay-events.json`).
The relay is told about it with
`--extra-device Passport:/var/lib/ai-passport-tv-passport/now-playing.json:/var/lib/ai-passport-tv-passport/relay-events.json`;
the phone remote then follows whichever device changed channel last, or the
one picked at the top of the page. Open the port in the cloud firewall as well.

It keeps the Xingzhi's byte rate (200000). In busy scenes at 140-165 kB/s of
picture the ESP32-C3's decode and panel work takes longer than the time it
covers and frames fall to 9.5-11 a second. Capping the Passport at 110 kB/s
kept it ahead but cost the colour: every frame is fitted into its byte budget
by stepping down a ladder of coarser colours (`frames.encode_within`), and at
about 9 kB a frame detailed channels such as CCTV1 sat on the coarsest rungs,
eight flat colours. The cap was removed once the firmware took keys above the
video task.

On this single-core chip a busy
scene keeps the video task (priority 5) ready all the time, and the task that
takes the keys -- the main task running `av_player_main()`, priority 1 -- did
not run until the scene calmed down. The Passport's board header sets
`BSP_KEYS_ABOVE_VIDEO 1`, which lifts that task to priority 6, just above the
video task; it sleeps on the key queue in 50 ms waits, so the picture loses
little, and sound (7) stays above it. Each key logs
`key <n> ev <n> waited <ms> ms`, the time from the button task to the handler.
The dual-core Xingzhi sets it to 0 and keeps its priorities.

A Passport stream is about 120-150 kB/s (picture plus sound). On a host with 3 Mbit/s of public
bandwidth, two devices watching at once share it and both lose quality.

## Voice (push-to-talk)

With `CONFIG_AV_VOICE` (the default) and a server whose CONFIG offers voice,
holding OK for 0.45 s while playing or in the channel list talks; letting go
finishes, ▲ cancels. An OK click still opens the list; inside the list and the
voice results a double-click of OK goes back a level (the long press now talks).
The backlight stays on the arrows' double-click, so nothing else moves. Without
a server offering voice, the keys are exactly as before.

The microphone is the ES8311's, read from the I2S0 receive channel that the
player already opens full duplex at 16 kHz/16-bit (left slot, 30 dB input gain,
`no_dac_ref`): no codec reopen and no glitch in the TV's sound, which is muted in
software from OK press-down until the recording ends. Voice takes about 10 KB of
buffers plus a 3 KB task stack while OK is held and gives them back afterwards;
the log prints `capture started: buffers=… heap=… largest=…` and
`voice buffers freed; heap=… min_heap=…`. While the list or a voice screen
covers the picture the C3 does not decode it. Not yet verified on the device:
the capture level in a room with the TV just muted, and the frame rate and key
latency during and after voice in a busy scene.

