<p align="right">
  <a href="2026-10-08-xingzhi-cube-port-design.zh_CN.md">简体中文</a> · <strong>English</strong>
</p>

# Port to the Xingzhi Cube 1.54 board — design

Status: approved 2026-10-08. Branch: `board/xingzhi-cube-154`.

## Goal

Run this TV play on a second board, the "Xingzhi Cube 1.54 TFT ML307" sold as a
Xiaozhi AI toy, without breaking the FoloToy AI Passport build. Same features:
live TV over Wi-Fi, channel up/down, volume, brightness, channel menu, status
view, SoftAP provisioning.

Success: on the Xingzhi board the picture plays with sound from an unmodified
`tv-server` binary, the three buttons behave as on the Passport, the device
stays powered on battery, and the Passport build still compiles unchanged.

## Hardware facts

Measured on the user's device (esptool chip/flash ID, GPIO probe of every
button) and cross-checked against the board definition
`main/boards/nologo/xingzhi-cube-1.54tft-ml307` in
[78/xiaozhi-esp32](https://github.com/78/xiaozhi-esp32).

| Item | AI Passport (existing) | Xingzhi Cube 1.54 (new) |
| --- | --- | --- |
| SoC | ESP32-C3, no PSRAM | ESP32-S3 QFN56 rev 0.2, 8 MB octal PSRAM |
| Flash | 8 MB | 8 MB (Winbond `ef4017`) |
| Display | ST7789P3 240×320, SPI2 | ST7789 240×240, SPI3: MOSI 10, SCLK 9, DC 8, CS 14, RST 18, SPI mode 3, 80 MHz, colour invert on, no gap |
| Backlight | GPIO21 LEDC | GPIO13 LEDC, active high |
| Audio out | ES8311 codec over I²C + I²S | I²S straight to amplifier: BCLK 15, LRCK 16, DOUT 7; no codec, no I²C |
| Microphone | ES8311 ADC | I²S MEMS (WS 4, SCK 5, DIN 6) — unused by this play |
| Buttons | 3 on one ADC ladder (GPIO0) | 3 GPIOs, active low: UP = GPIO40 (VOL+), DOWN = GPIO39 (VOL−), OK = GPIO0 (BOOT); a 4th key is hardware reset / power |
| Battery | CW2017 fuel gauge (I²C) | ADC2 channel 6 voltage, 12 dB atten; charging detect GPIO38 (high = charging) |
| Power | always on | **GPIO21 power hold (RTC GPIO): drive high at boot**, low + hold + deep sleep to power off |
| Other | NFC | ML307R 4G modem on UART (RX 11, TX 12) — not used, left untouched |

## Approach

Keep one source tree and choose the board at build time (approach A of three
considered; B "replace the BSP in a fork" loses upstream merges, C "rewrite on
the Xiaozhi framework" discards the tuned player).

1. **Board selection.** A Kconfig choice `AV_BOARD` in
   `components/bsp/Kconfig` with `AV_BOARD_PASSPORT` (default) and
   `AV_BOARD_XINGZHI_CUBE_154`. Board defaults live in
   `sdkconfig.defaults.xingzhi-cube-154`, applied with
   `SDKCONFIG_DEFAULTS="sdkconfig.defaults;sdkconfig.defaults.xingzhi-cube-154"`
   and `idf.py set-target esp32s3`. That file sets the board choice and target.
   PSRAM stays off: the player already fits the C3's internal RAM, the S3 has
   more, and with PSRAM on large `malloc`s could land where SPI DMA has to
   bounce-copy them. The TV product is still built by adding
   `sdkconfig.av-prototype` (`CONFIG_AV_RAW_PROTOTYPE`) to that list, exactly
   as on the Passport; without it the image is the BSP demo menu, which this
   port uses as its hardware bring-up test.
2. **BSP split.** Public headers in `components/bsp/include` stay the single
   interface. `bsp_pins.h` becomes a dispatcher that includes
   `boards/passport/bsp_pins_board.h` or
   `boards/xingzhi_cube_154/bsp_pins_board.h`. Audio, button, battery,
   I²C and power sources move to `src/passport/*.c` and
   `src/xingzhi_cube_154/*.c`; `CMakeLists.txt` compiles only the selected set.
   The display driver (`bsp_display.c`, `bsp_display_lvgl.c`) stays one shared
   file, parameterised by `BSP_LCD_VENDOR_INIT` (Passport-only gamma table) and
   the `BSP_RAW_*` orientation macros, so the DMA stripe path is not
   duplicated. The Xingzhi board has no I²C peripherals: its `bsp_i2c_init()`
   returns `ESP_ERR_NOT_SUPPORTED` and `bsp_i2c_scan()` logs that, so
   `main.c` keeps calling them unchanged.
3. **Raw display orientation as a pin fact.** Each board defines
   `BSP_RAW_W`/`BSP_RAW_H` (the picture-mode framebuffer size) and the raw
   claim's swap/mirror. Passport: 320×240 via swap + mirror X. Xingzhi:
   240×240, no swap. `av_protocol.h` derives `AV_WIDTH`/`AV_HEIGHT` from them,
   and the existing static assertion in `av_player.c` becomes
   `AV_WIDTH == BSP_RAW_W && AV_HEIGHT == BSP_RAW_H`.
4. **Video geometry per board.** `AV_VIDEO_WIDTH/HEIGHT` come from the board:
   Passport keeps 320×180; Xingzhi uses 240×180, a geometry the server already
   offers. The server fits 16:9 sources into it without cropping, so the
   visible picture is 240×135 centred on the 240×240 panel; the existing
   `AV_VIDEO_Y_OFFSET` (30 rows) places it. The status/volume overlays already
   draw into the letterbox rows computed from these constants.
5. **Audio.** New `bsp_audio.c` for the board opens an I²S TX simplex channel
   (16-bit mono, standard Philips, sample rate from `bsp_audio_set_format`).
   With no codec, volume is a software gain applied in `bsp_audio_write*`;
   `bsp_audio_write_timeout` keeps its contract. `bsp_audio_read` returns
   `ESP_ERR_NOT_SUPPORTED`.
6. **Buttons.** `iot_button` GPIO buttons (active low, internal pull-up) mapped
   to `BSP_BTN_UP/DOWN/OK`, emitting the same PRESS/CLICK/DOUBLE/LONG events.
   `bsp_button_read_mv()` returns −1 (no ladder).
7. **Battery.** ADC2 CH6 oneshot, averaged, mapped through the Xiaozhi table
   (raw 1970 → 0 %, 2062 → 20 %, 2154 → 40 %, 2246 → 60 %, 2338 → 80 %,
   2430 → 100 %). `bsp_battery_mv()` returns the calibrated pin voltage ×2
   (divider assumed 1:2; marked unverified until measured).
8. **Power.** `bsp_power_init()` (new, Xingzhi only, no-op on Passport) drives
   GPIO21 high first thing in `app_main`. Nothing in the play powers the board
   down, so there is no power-off API; the charging-detect pin is recorded in
   the table above but not used.
9. **Server.** No code change. A helper script starts a second media server
   from the user's existing `tv-server` with `TV_GEOMETRY=240x180` on port 8098
   and without its own channel page, so the Passport instance (320×180, media
   8096, channel page 8097) keeps running. The device is provisioned with
   `<mac-hostname>.local:8098`.
10. **Flashing.** Merged 8 MB image at `0x0` via `esptool`. The first flash
    erases the chip: the Xiaozhi NVS and otadata sit inside this table's NVS
    range and would otherwise confuse `nvs_flash_init`. The protected
    Passport layout (3 MB app, `cardid` at `0x356000`) is kept unchanged on the
    new board too, so one partition table serves both.

## Testing

- Host tests: existing suite must stay green; add a host test for the battery
  mapping and the software-volume scaling (pure functions).
- Build: both boards (`esp32c3` Passport, `esp32s3` Xingzhi) with
  `AV_PUBLIC_BUILD=ON`.
- Device, in order: (1) BSP bring-up with the BSP demo-menu build — its
  Display, Button, Audio and Battery pages, plus the serial log; (2) provisioning via SoftAP; (3) live
  playback from the 240×180 server instance, logging frame rate, audio
  underruns and session resets for 10 minutes.

## Out of scope

4G modem, microphone, BLE, deep-sleep power saving beyond the power-off path,
and pushing anything upstream (needs the user's separate agreement).

## Risks

- The battery divider ratio and the amplifier's gain are inferred, not measured.
- ESP32-S3 Wi-Fi/lwIP throughput with the 64 KB TCP window is untested; the S3
  has more RAM, so this is expected to be easier than on the C3.
- The 4G modem stays powered by default; if it draws noticeable current or
  causes RF interference, a later change may need to hold it in reset.
