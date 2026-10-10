<p align="right">
  <a href="2026-10-10-audio-only-mode-design.zh_CN.md">简体中文</a> · <strong>English</strong>
</p>

# Audio-only mode — design

Status: approved 2026-10-10. Follows
[`2026-10-10-device-ui-redesign-design.md`](2026-10-10-device-ui-redesign-design.md).

## Goal

A mode in which the device plays sound only and the screen shows just the pet.
It saves the link and the battery: the server sends no picture, and the
backlight dims.

## The mode lives on the device

- The device stores `audio_only` in NVS (`avsettings`), like the volume and the
  pet. It holds across channel switches and reboots until it is turned off.
- HELLO carries `"audio_only": 0|1`. Its presence also says the firmware accepts
  `AV_SWITCH {"audio_only": 0|1}`.
- Turning the mode on or off stores it and ends the session; the device dials
  again at once with the new HELLO, the way a channel switch does. Sound stops
  for about 1-2 s.

## Server

- For a session whose HELLO says `"audio_only": 1`, the media server sends no
  video packets. CONFIG, the palette and the decoder are unchanged; audio is
  paced exactly as now. The link drops from about 200 KB/s to about 8 KB/s.
- `now-playing.json` carries `audio_only` with the device's other values.
- The phone: `POST /api/audio {"on": true|false}` writes a relay event `audio`
  (value 0/1). The media server sends `AV_SWITCH {"audio_only": v}` only to a
  device whose HELLO carried the field. Sliders' rule applies: offline or older
  firmware disables the control.

## Device

- **Switching on the device:** short-press OK on the status page toggles the
  mode (it used to close the page). The status page's key line reads "OK:
  sound only" or "OK: picture" (in Chinese on the device); other keys still
  close it.
- **Switching on the phone:** a "sound only" switch under the channel display
  on the Remote tab, showing the device's state.
- **Screen while playing audio-only:** the top bar as usual (channel number and
  name, signal, battery); rows 30-209 in `UI_INK` with the pet at 3× in the
  middle, mood 1, bobbing like the waiting screens; the bottom bar empty. Menus,
  pages, the level box, the banner, the seek box and the pause band work as
  they do over a picture.
- **Backlight:** 10 s after entering the mode, or after the last key press or
  phone action, the backlight goes to the lowest step. Any key or phone action
  restores the viewer's brightness for another 10 s. The stored brightness is
  not changed; the brightness page, while open, keeps the viewer's level.
- **No picture is not a fault:** in audio-only mode the "sound but no picture"
  rules are off -- the waiting screen after 1.5 s without frames, the 20 s
  session limit and the dead-channel skip. Their place is taken by sound: the
  waiting screen appears when no audio has arrived for 1.5 s, and a channel that
  never delivers audio is skipped as a channel without a picture is now.

## Testing

- Host: no video packets to an audio-only session and audio unchanged; the
  switch packet only to firmware that reported the field; the relay endpoint,
  event and page field; the firmware's stall and skip policy with audio as the
  signal; the pet screen drawn by `tools/render_ui`.
- Device: on and off from the keys and from the phone, both shown in step; the
  mode survives a reboot and a channel switch; the backlight dims after 10 s and
  wakes on a key; five minutes on one channel without a skip; pause and seek.
