English | [简体中文](README.zh_CN.md)

# RooTV

*RooTV (its Chinese name means "little mouse TV") was called Xiaozhi TV until
0.2.5-beta. The repository keeps its name.*

RooTV turns a small ESP32 gadget into a tiny television for the family.
A computer at home does the heavy work, the gadget shows the picture, and a
phone is the remote control.

It works on two devices:

- **Xingzhi Cube 1.54** (ESP32-S3), the cube sold as a "Xiaozhi AI" toy
- **FoloToy AI Passport** (ESP32-C3)

> **This is a beta (v0.2.0-beta).** It works at home, but some steps have not
> been tried on every kind of computer yet. See [About this beta](#about-this-beta).

## What it is made of

| Part | What it does |
| --- | --- |
| **Firmware** on the device | Shows the picture, plays the sound, has buttons for channel and volume |
| **PC client** on a computer that stays on | Fetches what you want to watch, converts it and sends it to the devices over your home Wi-Fi. Windows, macOS (Apple silicon and Intel) and Linux. It lives as a small TV icon in the tray or menu bar |
| **Phone remote** in the phone's browser | Channels, creators and series, picking an episode, casting a video, listening on the phone, the device's Wi-Fi. Add it to the home screen and it works like an app |

Video stays on your home network. It does not pass through a cloud server.

**The project ships no channels, live-stream addresses, playlists, accounts or
cookies.** The channel list starts empty. You add what you want to watch.

## What you need

| Thing | Notes |
| --- | --- |
| **A device** | A Xingzhi Cube 1.54 or a FoloToy AI Passport |
| **A computer that stays on** | Windows 10/11 (64-bit), or macOS 12 or later. Linux works too (less tested). It must be on while you watch |
| **Home Wi-Fi** | The computer, the device and the phones on the same home network |
| **A USB data cable** | Only for flashing the device. Some cables only charge; use one that carries data |
| **Chrome or Edge** | On the computer, for flashing. Safari and Firefox cannot flash |
| **A phone** | Any smartphone with a camera and a browser |

## Downloads

Everything is on the **[Releases page](https://github.com/rooma1989/ai-passport-tv/releases)**.
The newest version is at
**[releases/latest](https://github.com/rooma1989/ai-passport-tv/releases/latest)**;
for now that is a beta.

| Your computer | File |
| --- | --- |
| Windows 10/11 | `RooTV-<version>-windows-x64-setup.exe` (installer, recommended), or `RooTV-<version>-windows-x64.zip` (no install) |
| Mac with Apple silicon (M1, M2, M3, M4...) | `RooTV-<version>-macos-arm64.dmg` |
| Mac with an Intel processor | `RooTV-<version>-macos-x86_64.dmg` |
| Linux x86_64 (less tested) | `rootv-<version>-linux-x86_64.tar.gz` (read `README-linux.txt` inside) |

Releases up to 0.2.5-beta named these files `XiaozhiTV-...` and
`xiaozhi-tv-...`. Installing RooTV over Xiaozhi TV replaces it: the old
program quits and is removed (Windows installer) or can be deleted (Mac), and
the first start moves the data folder, so devices, accounts and channels stay.

Not sure which Mac you have? Apple menu > About This Mac. "Chip Apple M..." is
Apple silicon, "Processor Intel..." is Intel.

`SHA256SUMS-desktop` lists checksums for these files, if you want to check
the download. You do not need the other files on the page to get started:
the PC client already carries the device firmware.

## Getting ready: install the PC client

The step-by-step tutorial with every screen is in
[`docs/desktop/README.md`](docs/desktop/README.md). The short version:

1. **Install it.**
   - **Windows**: double-click the `setup.exe`. Answer "Yes" when Windows asks
     to allow changes.
   - **Mac**: open the `.dmg` and **drag "RooTV" into Applications**. Then
     open it from Applications.
2. **Get past the warning.** This beta is not signed yet, so the system warns
   you the first time. The file is not damaged.
   - **Windows** ("Windows protected your PC"): click **More info**, then
     **Run anyway**.
   - **Mac** ("RooTV" Not Opened): click **Done**.
     **Do not click Move to Trash**, that deletes the program.
     Then open **System Settings > Privacy & Security**, scroll down,
     find the line about "RooTV", click **Open Anyway** and enter your
     password. (On macOS 12 to 14 you can also Control-click the app in
     Applications and pick Open.)
   - When the Mac asks whether RooTV may find devices on your local
     network, click **Allow**.
3. **First run.** A small TV icon appears in the notification area (Windows)
   or the menu bar (Mac). The first time, it downloads a video component
   (ffmpeg, a few tens of MB). Then the **setup page opens in the browser**.
   Type a name for your home and your own name, and become the administrator.
4. **Connect your phone.** Click the TV icon > **Connect a phone**. A QR code
   appears. With the phone on the home Wi-Fi, scan it with the phone's camera
   and sign in. Then use "Add to Home Screen" so the remote is one tap away.
   Each family member can scan their own code.

The menus are in Chinese; the tutorial gives the English meaning of each item.

## Step 1: Flash the firmware

The device needs the RooTV firmware once.

1. Plug the device into **the computer running the PC client** with the USB
   data cable.
2. Click the TV icon > **Set up a TV**. The flasher opens in the browser. Use
   **Chrome or Edge**.
3. Click connect and pick the device's port. The page recognises the device by
   itself.
4. Follow the page and wait until it says the write is done.

**Read this before you flash:**

- **Xingzhi Cube: the original Xiaozhi AI firmware is erased.** The first time,
  the whole chip is wiped. After that the cube is a TV and no longer the
  Xiaozhi AI chat toy. This flasher cannot bring the old firmware back. The
  page asks you to tick a box before it erases. Later updates do not erase it
  again.
- **AI Passport: never erased.** It holds factory identity data that cannot be
  recovered once lost. The flasher only writes the firmware area and checks
  the range itself.
- **AI Passport with the Muse dual setup:** not supported by this public build.
  The flasher refuses such a device. See
  [`docs/boards/passport-muse-dual.md`](docs/boards/passport-muse-dual.md).

**After flashing, start the device by hand**:

- **AI Passport**: hold the power button for about 2 seconds to switch off,
  then hold it for about half a second to switch on again. It has a battery,
  so pulling the cable does not restart it. Without this step the screen stays
  dark.
- **Xingzhi Cube**: tap the bottom key (the reset key).

Other ways to flash, for people who know what they are doing:

- **Web flasher on your own site**: `xiaozhi-tv-flasher.zip` from the release
  is the flasher page plus firmware. Unpack it as `/flash/` on any https site.
- **By hand with esptool**: the release has `FoloToy-AI-Passport-tv.bin` and
  `Xingzhi-Cube-154-tv-public.bin`, full images written at address `0`.
  Details are in [`docs/boards/xingzhi-cube-154.md`](docs/boards/xingzhi-cube-154.md)
  and [`docs/desktop/README.md`](docs/desktop/README.md). **Never run
  `erase-flash` on an AI Passport.**

## Step 2: Wi-Fi and binding

After flashing, the device shows a setup screen with a QR code.

1. **Wi-Fi by sound** (easiest): scan the QR code on the device's screen with
   the phone. A page opens. Type your home Wi-Fi name and password. The server
   field is already filled with the computer's address. Turn the phone's volume
   up, hold it close to the device (within half a metre) and tap **Send**. The
   device listens, joins the Wi-Fi and restarts.
2. **Hotspot** (if sound does not work): page through the setup screens with
   ▲ / ▼ until one shows the device's hotspot name and a setup address. Join
   that hotspot with the phone, open the address (usually `192.168.4.1`) and
   fill in your Wi-Fi and the server address.
3. **Bind it to your account**: the device now shows "BIND THIS TV" and a code.
   Double-click a key to open the status page (see [Buttons](#buttons)) and scan
   its QR code with your signed-in phone. Or, in the remote, open **Me > Bind a
   new device** and type the code.

To set up Wi-Fi again later, open the status page and hold the top key (up).
Owners can also manage a device's saved Wi-Fi networks from the phone
(**Me > My devices > Wi-Fi**).

## Step 3: Add channels and creators

- **Channels**: click the TV icon > **Open the console**. In the channel table,
  import **your own** M3U playlist (an address or a file), check which entries
  play, and save the ones you want. A device holds up to 128 channels.
- **Creators and series**: on the phone, open **Subscriptions** and paste a
  Bilibili creator, series or collection link. Each one becomes a channel that
  plays the newest or next episode and remembers where you stopped.

A device with nothing to play shows "NO CHANNELS YET". It picks up new channels
by itself.

## Watching

### The phone remote

The bottom bar has four tabs: **Remote**, **Cast**, **Subscriptions**, **Me**.

- **Remote**: change channel, volume and brightness; pause, seek and skip on
  videos; previous and next episode on a series.
- **Listen on the phone**: the device's sound plays on the phone or on
  Bluetooth headphones paired with it, even with the phone locked. The device
  keeps showing the picture with its speaker muted. Good for watching late at
  night.
- **Cast**: paste a Bilibili video link and the device plays it, then goes
  back to its channel.
- **Subscriptions**: your creators and series, with an episode picker.
- **Me**: your devices (Wi-Fi, guest code, rename, unbind), binding a new
  device, signing in another phone or the computer, inviting family.

**Voice control**: the firmware can take spoken commands (hold OK and speak),
but it needs a speech recogniser on the server. The PC client in this beta does
not include one yet.

### Buttons

| What you want | AI Passport | Xingzhi Cube |
| --- | --- | --- |
| Volume | Click ▲ or ▼ | Click the top or second key |
| Change channel | Hold ▲ or ▼ | Hold the top or second key |
| Channel list | Click OK | Click the third key (BOOT) |
| Status page | Double-click OK | Double-click the top or second key |
| Brightness | Double-click ▲ or ▼ | Hold the third key |
| Wi-Fi setup again | On the status page, hold ▲ | On the status page, hold the top key |
| Sound only (the pet instead of the picture) | On the status page, click OK | On the status page, click OK |
| Power | Hold the power button | Hold the bottom key |

Board details: [`docs/boards/xingzhi-cube-154.md`](docs/boards/xingzhi-cube-154.md).

## About this beta

- **One device per model at a time.** A Xingzhi Cube and an AI Passport can
  play together; two of the same model cannot yet.
- **Remote control at home only.** The phone must be on the home Wi-Fi.
  Control from outside the home (a cloud channel) is planned for v0.3.0-beta.
- **Not signed yet.** Windows and macOS warn the first time (see above).
- **The computer must stay on.** The client stops idle sleep while someone
  watches, but closing a laptop lid still puts it to sleep.
- **Keep the computer's address the same.** If the router gives it a new
  address, the devices lose it. A fixed address (DHCP reservation) in the
  router helps.
- **Not everything has been tried by hand yet**, for example a real Windows
  PC and an Intel Mac from start to finish. The tutorial lists what is not
  verified. Please report problems on GitHub; the tray's "Export diagnostics"
  makes a zip without passwords or keys.

Common problems and fixes:
[`docs/desktop/README.md`](docs/desktop/README.md#common-problems).

## Advanced: NAS, Docker, no screen

No computer that stays on, but a NAS or a mini PC? The same server runs as the
**household server**, in Docker, in NAS containers or with plain Python, without
the tray: [`docs/selfhost/README.md`](docs/selfhost/README.md).

## Legacy single-user server

Earlier releases had a single-user server (`tv-server-*` downloads, or
`./run.sh` / `run.bat` without options). It serves one device, has no accounts
and no phone sign-in, and the device is pointed at it by typing the computer's
address with port 8096 on the hotspot page. It is kept for one more release for
people already using it. **New installs should use the PC client.** How it
works: [`server/README.md`](server/README.md).

## For developers

**Repository map**

| Path | What is there |
| --- | --- |
| `main/`, `components/bsp/` | Device firmware (ESP-IDF) and board support for both devices |
| `server/` | The media server, the relay (accounts, phone remote, console) and the household server (`server/selfhost/`) |
| `desktop/`, `packaging/desktop/` | The PC client's tray and its installers |
| `packaging/flash/` | The web flasher |
| `tests/`, `tools/` | Host tests and build, check and lab scripts |
| `docs/` | Documentation, design specs and the changelog |

**Build the firmware** (ESP-IDF 5.5.3):

```sh
./tools/validate.sh --prototype   # AI Passport TV image
./tools/validate.sh --xingzhi     # Xingzhi Cube TV image
```

The images land in `build/` (`FoloToy-AI-Passport-prototype-public.bin`,
`Xingzhi-Cube-154-tv-public.bin`). Wi-Fi and the server address are set on the
device, so nothing private is compiled in.

**Checks and tests**:

```sh
./tools/validate.sh --static      # repository checks and host tests, no ESP-IDF needed
./tools/validate.sh               # the complete gate
python3 tools/check_repo.py       # links, language pairs, secrets
```

**CI** (`.github/workflows/`): static checks and host tests, firmware builds,
the PC client core and installers, the Docker image and the legacy server. A
version tag builds and publishes the release.

**Where to read next**

- [`AGENTS.md`](AGENTS.md): conventions, validation and safety rules (start here)
- [`docs/README.md`](docs/README.md): hardware contract and the documentation index
- [`docs/assets/architecture-index.md`](docs/assets/architecture-index.md): which file does what
- [`docs/desktop/developer.md`](docs/desktop/developer.md): PC client code map, building and the control API
- [PC client design](docs/superpowers/specs/2026-10-19-pc-client-architecture-design.md)
  and [self-hosting design](docs/superpowers/specs/2026-10-15-self-hosting-design.md)
- [`docs/CHANGELOG.md`](docs/CHANGELOG.md): what changed
- [`docs/fork-guide.md`](docs/fork-guide.md): how this fork relates to upstream

## Credits and licence

This project is a fork of
[zpdeng1020-coder/ai-passport-tv](https://github.com/zpdeng1020-coder/ai-passport-tv),
which is based on the
[official FoloToy AI Passport firmware](https://github.com/folotoy/ai-passport).
Thanks to both. The AI Passport hardware design belongs to FoloToy. Xingzhi
Cube and Xiaozhi AI are other makers' products; this project is not affiliated
with them.

The code is under the MIT licence: see [`LICENSE`](LICENSE). Third-party parts
keep their own licences; the PC client lists them in
[`packaging/desktop/NOTICE.txt`](packaging/desktop/NOTICE.txt) and the Docker
image in [`packaging/docker/NOTICE`](packaging/docker/NOTICE).
[FFmpeg](https://ffmpeg.org/) is not part of the PC client download: the client
fetches it on first run from its publisher (GPL builds).

## Disclaimer

RooTV is open-source software for private, non-commercial use in your own
home. It does not include, host, sell or recommend any channel, stream, video,
account or proxy. Everything it plays is fetched by your own computer from
sources you add yourself. You are responsible for having the right to watch
them and for following the terms of the services you use and the laws where
you live. Do not use it to share content with people outside your household.
The software is provided "as is", without warranty of any kind (MIT licence).
More in [`docs/selfhost/README.md`](docs/selfhost/README.md#disclaimer).
