<p align="right">
  <a href="2026-10-10-playback-and-device-controls-design.zh_CN.md">简体中文</a> · <strong>English</strong>
</p>

# Playback and device controls from the phone — design

Status: approved 2026-10-10. Follows
[`2026-10-10-remote-polish-design.md`](2026-10-10-remote-polish-design.md).

## Goal

On the phone remote: a progress bar with seek, back/forward 30 s, next video and
pause/resume for creator channels and casts; volume and brightness sliders that
show the device's current values.

## Events become a list

`relay-events.json` holds the last 20 events (`{"events": [{seq, action, …}]}`)
instead of only the latest, and the TV server handles every new one in order:
dragging a slider writes events quickly, and a switch must not be overwritten by
the volume change that followed it. The old single-event file still reads.
Actions: `cast`, `return`, `switch` (channel), `pause`, `resume`, `volume`
(value), `brightness` (value).

## Playback controls (server only)

- The relay tracks the stream it is serving on each channel: video, start
  offset, start time. From that it reports `now.video`: `position`, `duration`,
  `paused`, `started`.
- **Seek** (`POST /api/seek {delta | position}`), **next** (`POST /api/next`):
  the relay stores the new position (or marks the video watched; for a cast,
  ends it and switches back), then cuts the stream it is serving. The TV
  server's decoder reopens the URL by itself (`-reconnect_streamed`) and gets
  the stream from the new place; the picture pauses for 1–3 s. A seek before 0
  lands on 0; a seek past the end is "next". The relay stores the target plus
  the usual 10 s resume back-off so that the reopened stream starts exactly at
  the target.
- **Pause** (`POST /api/pause`): the relay saves the exact position, cuts the
  stream, refuses the channel (503) while paused and writes `pause`. The TV
  server closes the source and holds the device on the last frame with silence
  and a "PAUSED" notice, as it does while a source reconnects. **Resume** writes
  `resume`; the TV server restarts the source and the stream continues from the
  exact position. After 30 minutes paused it resumes on its own. A switch while
  paused ends the pause.
- The page shows the bar, `12:34 / 25:10` and the buttons only on `cast` and
  `upNN`. A seek or next shows "seeking…" until the relay reports a stream
  started after the request.

## Volume and brightness (firmware and server)

- HELLO carries `volume` (0–100), `brightness` (step index) and
  `brightness_steps`. During a session the device sends a new `STATUS` packet
  (kind 9, `{"volume", "brightness"}`) whenever either changes, from a key or
  from the phone. The TV server accepts it while playing, waiting or holding,
  and writes the values to `now-playing.json`.
- The phone's sliders (volume in steps of 10, brightness by step) write
  `volume` / `brightness` events; the TV server sends them in a `SWITCH` packet
  whose JSON has `volume` or `brightness` instead of `channel`, only to a device
  whose HELLO carried `volume`. Firmware that understands only `channel`
  ignores the packet. The device applies the value, shows its own indicator,
  saves it like a key change and reports it back.
- Sliders are disabled when the device is offline or its firmware sent no
  `volume`.

## Testing

- Host: event list order, limit and the old format; seek arithmetic, next, the
  exact resume after seek and pause, 503 while paused; the TV server's pause and
  resume and the 30-minute limit; STATUS accepted and recorded; control packets
  only to capable devices; firmware parsing of `volume`/`brightness`; the page's
  fields.
- Device: sliders both ways, seek, next, a pause of more than five minutes.
