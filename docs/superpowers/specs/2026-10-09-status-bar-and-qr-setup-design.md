<p align="right">
  <a href="2026-10-09-status-bar-and-qr-setup-design.zh_CN.md">简体中文</a> · <strong>English</strong>
</p>

# Status bar and QR-code setup — design

Status: approved 2026-10-09.

## Goal

1. While playing, always show Wi-Fi signal and battery in the top-right corner.
2. Make Wi-Fi setup a matter of scanning, not of finding the device's hotspot in
   the phone's Wi-Fi list and typing an address.

Remembering networks needs no work: `components/esp-wifi-connect` already keeps
the ten most recent networks (newest first) and joins whichever one is in range.

## 1. Status bar

- Drawn in the top letterbox band (`AV_VIDEO_Y_OFFSET` rows, 30 on both boards),
  right-aligned: four signal bars, a battery outline filled to the charge, the
  percentage as text. Red fill below 20 %. A lightning bolt over the battery
  while charging.
- Signal bars come from the existing `wifi_bars()` (RSSI ≥ −55/−65/−75 dBm →
  4/3/2, else 1; 0 when not associated, drawn dim). Battery from the existing
  30-second `bsp_battery_soc()` poll, charging from a new
  `bsp_battery_charging()` read at the same time.
- `refresh_info_bars()` now always owns the top band once media has started and
  repaints it every 2 s; the bottom band (frame rate, receive rate) stays
  menu-only. The menu's old "WiFi n/4 / Battery n%" text in the top band is
  replaced by the same icons.
- New pure module `main/ui_status.{h,c}` draws the group into a `ui_surface_t`
  with `ui_text_fill`/`ui_text_draw` only, so it is host-tested.

### BSP addition

`int bsp_battery_charging(void)` in `bsp_battery.h`: 1 charging, 0 not, −1
unknown. Passport returns −1 (CW2017 does not report it). Xingzhi reads GPIO38
as an input, high = charging (as the Xiaozhi firmware does); the pin goes back
into the board header as `BSP_BATT_CHARGE_GPIO`.

## 2. QR-code setup screen

- `paint_setup_screen()` shows two QR codes side by side instead of the
  "connect to … / open …" text:
  - left: `WIFI:T:nopass;S:<hotspot SSID>;;` — a phone camera joins the hotspot;
  - right: the setup URL (`http://192.168.4.1`) — opens the page even when the
    phone's captive-portal sheet does not appear (it did not on the user's phone).
  A numbered Chinese caption under each ("scan to join the hotspot", "scan to
  open setup"); then the server line, the state line and the exit hint as today.
- QR encoding: nayuki's `qrcodegen` (MIT), vendored verbatim as the component
  `components/qrcodegen`, ECC LOW, versions 1–6, mask auto.
- New pure module `main/ui_qr.{h,c}`:
  - `ui_qr_wifi_text()` builds the Wi-Fi string, escaping `\ ; , : "` in the SSID;
  - `ui_qr_layout()` picks one module size for both codes so that both, each
    with a 2-module quiet zone, fit side by side in the panel width with a gap,
    capped at 5 px per module;
  - `ui_qr_draw()` paints one code (white quiet zone, black modules) into the
    part of a stripe it overlaps.
  The QR codes are encoded once per screen paint into stack buffers
  (`qrcodegen_BUFFER_LEN_FOR_VERSION(6)` = 220 bytes each, two buffers per code).
- The setup screen keeps its colour-bar background; the QR codes sit on their own
  white quiet zones, which is what a scanner needs.
- Entering setup is unchanged (status page → long-press UP). Both boards get the
  new screen; the layout is computed from `AV_WIDTH`.

## Testing

- Host: `tests/test_ui_qr.c` (escaping, layout fits 240 and 320 wide, a drawn
  code has dark finder corners and a light quiet zone), `tests/test_ui_status.c`
  (battery fill width at 0/50/100 %, red below 20 %, bars lit per level, unknown
  battery shows "--"). Both wired into `tools/validate.sh`.
- Build: Passport public build, Xingzhi public build, Xingzhi cloud (token) build.
- Device: the user scans both codes with a phone and completes setup; watches the
  status bar during playback, unplugs USB to see the bolt disappear.

## Out of scope

Bluetooth or mini-program provisioning, changing how setup mode is entered,
showing the bottom band during playback.
