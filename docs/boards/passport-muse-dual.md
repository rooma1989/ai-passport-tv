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

A Passport stream is about 150-200 kB/s. On a host with 3 Mbit/s of public
bandwidth, two devices watching at once share it and both lose quality.
