<p align="right">
  <a href="2026-10-09-paged-setup-and-help-design.zh_CN.md">简体中文</a> · <strong>English</strong>
</p>

# Paged setup screen and help page — design

Status: approved 2026-10-09. Follows
[`2026-10-09-status-bar-and-qr-setup-design.md`](2026-10-09-status-bar-and-qr-setup-design.md).

## Goal

The two-code setup screen was too busy. Split it into pages turned with the
arrow keys, and add a QR code that opens a hosted help page from both the setup
screen and the status page.

## Setup pages

| Page | Content |
| --- | --- |
| 1 Join hotspot | one large Wi-Fi QR code; caption: use the phone camera, or join `<SSID>` |
| 2 Open setup | one large QR code of the setup URL; caption: or open `192.168.4.1` |
| 3 Help | one large QR code of `AV_HELP_URL`; caption: scan for the key guide, WeChat works |
| 4 Status | hotspot name, setup address, server address, connection state as text |

- Every page: `n/N` and the page title at the top, the hint "UP/DOWN pages, hold
  OK to exit" (in Chinese) at the bottom.
- In setup mode a click on UP goes to the previous page and on DOWN to the next,
  wrapping. UP is the top key on the Xingzhi board (physical order). A long press
  on OK still leaves.
- Page 3 exists only when `AV_HELP_URL` is non-empty; otherwise there are three
  pages.
- One code per page, so each can be drawn larger: `ui_qr_layout_single()` picks
  the largest module size up to 6 px that fits a given height.

## Help page

- A static, phone-sized Chinese HTML page: keys during playback, in the channel
  list, on the status page and in setup; how to set up Wi-Fi; that the first code
  needs the phone camera (WeChat cannot join Wi-Fi from a QR code); what to do
  when a channel stays silent.
- Source in `packaging/help/index.html`. Deployed by copying it to a web host;
  the user's is `https://IEWWW.COM/TV/`.
- The URL is upper case so it encodes in QR alphanumeric mode: 21 characters fit
  version 1 (21 modules) rather than version 2. That is what lets a code fit on
  the status page.

## Status page

The four existing lines stay. Below them, at the right, a QR code of
`AV_HELP_URL` at 3 px per module (2 if it would run past the picture), with a
two-line caption to its left. Encoded once into a static buffer.

## Configuration

`AV_HELP_URL` comes from `av_private_config.h` like the token; `av_config.h`
defaults it to `""`, so public builds keep three setup pages and no status-page
code.

## WeChat

WeChat's scanner does not join Wi-Fi from `WIFI:` codes; only the system camera
does. Page 1 says so. The URL codes open in WeChat.

## Testing

Host: `ui_qr_layout_single()` (scale chosen per height, centring, refusal when
too small) and that a 21-character upper-case URL encodes as version 1.
Device: page turning with both keys, each code scanned, the help page opens on
the phone, the status page code scans.
