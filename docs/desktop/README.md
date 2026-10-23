<p align="right">
  <a href="README.zh_CN.md">简体中文</a> · <strong>English</strong>
</p>

# Xiaozhi TV PC client (0.2.1 beta)

Turns a Windows PC or a Mac at home into the "TV station" for Xiaozhi TV: the
computer fetches and transcodes the sources in the background and streams them
over the home Wi-Fi to the devices; phones at home control it in the browser.
Once installed, the only thing on screen is a small TV icon in the notification
area (Windows) or the menu bar (Mac).

Video stays on your home network and never crosses a cloud server. Design and
trade-offs:
[`2026-10-19-pc-client-architecture-design.md`](../superpowers/specs/2026-10-19-pc-client-architecture-design.md).

**This software ships no channels, streams, playlists, accounts or cookies.**
The channel table is empty on the first start; you add what to play (the
channel table can read a few public third-party playlists by their links).

> Screenshots: places marked "screenshot TODO" will get real screenshots; the
> written steps can be followed already. The Chinese page is the primary one.

## Beta limits

- **One device per model at a time.** A Xingzhi Cube and an AI Passport can
  play at once; two Xingzhi need 1.0.
- **Remote control at home only** (phone and computer on the same Wi-Fi).
  Away-from-home control is the next beta (0.3).
- **The installers are not signed**: Windows and macOS warn on first open; the
  steps are below. The public release will be signed.
- Flashing uses the web flasher in the browser (Chrome or Edge); built-in
  flashing, automatic updates and voice recognition come later.
- Steps marked "unverified" follow official documentation and have not yet been
  walked through on real machines.

## What you need

- A computer that **stays on**: Windows 10/11 (64-bit) or macOS 12 or later
  (Apple silicon or Intel). The client prevents idle sleep while a device
  watches, but closing a laptop lid still sleeps it.
- The computer, the devices and the phones on the **same home network** (a wired
  computer is fine if it is behind the same router).
- A Xiaozhi TV device: Xingzhi Cube 1.54 (Xiaozhi AI case) or FoloToy AI Passport.
- For flashing: a USB data cable and Chrome or Edge on the computer.
- Internet on the first run: the client downloads the ffmpeg video component
  (a few tens of MB; progress shows in the tray menu).

## Step 1: download

From the release page, download the file for your computer (the project
website's download page picks it for you):

| Your computer | Download |
| --- | --- |
| Windows 10/11 | `XiaozhiTV-<version>-windows-x64-setup.exe` (installer, recommended); `XiaozhiTV-<version>-windows-x64.zip` (portable) |
| Mac with Apple silicon (M1, M2, M3, M4...) | `XiaozhiTV-<version>-macos-arm64.dmg` |
| Mac with an Intel processor | `XiaozhiTV-<version>-macos-x86_64.dmg` |
| Linux (bonus) | `xiaozhi-tv-<version>-linux-x86_64.tar.gz`, see `README-linux.txt` inside |

**Which Mac?** Apple menu > About This Mac: "Chip Apple M..." is Apple silicon,
"Processor Intel..." is Intel.

**Checking the download (optional)**: the release has `SHA256SUMS-desktop`. On Windows,
`Get-FileHash .\XiaozhiTV-...-setup.exe` in PowerShell; on a Mac,
`shasum -a 256 ~/Downloads/XiaozhiTV-....dmg` in Terminal. The result must equal
the line in `SHA256SUMS-desktop`.

## Step 2: install

### Windows

1. Double-click `XiaozhiTV-...-setup.exe`.
2. If the blue "**Windows protected your PC**" (SmartScreen) appears: click
   "**More info**", then "**Run anyway**". The beta has no code-signing
   certificate; the file is not damaged. (screenshot TODO: SmartScreen)
3. User Account Control asks to allow changes: "**Yes**". Administrator rights
   are needed once, to install into Program Files and add the firewall rule.
4. Click through the wizard and finish with "Launch Xiaozhi TV" ticked.

The installer adds a firewall rule for Xiaozhi TV that applies **only to
Private networks**, so devices and phones can connect. If Windows has your home
Wi-Fi marked Public, the tray menu says so; to change it: Settings > Network &
Internet > your Wi-Fi (or Ethernet) > Network profile type > Private.

Windows Defender occasionally flags programs packaged with PyInstaller. If the
program is removed or quarantined after installing, check the SHA-256 as above,
restore it under Windows Security > Virus & threat protection > Protection
history, and please report it on GitHub.

### Mac

1. Double-click the `.dmg` and **drag "Xiaozhi TV" onto "Applications"** in the
   window that opens. Drag it first: run straight from the DMG window, macOS
   puts it in a temporary read-only place and its data and login item break (the
   tray menu warns about this too).
2. Open the Applications folder and double-click "Xiaozhi TV".
3. macOS says "**"Xiaozhi TV" Not Opened**" or that the developer cannot be
   verified: click "**Done**" (**not "Move to Trash"**).
4. Open **System Settings > Privacy & Security**, scroll to Security, find
   ""Xiaozhi TV" was blocked to protect your Mac", click "**Open Anyway**",
   confirm "Open Anyway" and enter your password. (screenshot TODO: Open Anyway)
   - On macOS 12-14 this also works: in Applications, **Control-click "Xiaozhi
     TV" > Open > Open**. From macOS 15 only the steps above work.
5. Later opens do not ask again (each update to a new version asks once more;
   do the same).
6. If macOS asks to allow "Xiaozhi TV" to find devices on your local network,
   click "**Allow**", or it cannot reach the sources and devices at home. If the
   Mac's firewall is on, it also asks to accept incoming connections: "Allow".

On a Mac the client is only a small TV icon in the menu bar at the top of the
screen, with no Dock icon. That is intended.

### Linux (bonus, unverified)

Unpack into your home folder as `~/xiaozhi-tv` and run `~/xiaozhi-tv/xiaozhi-tv`;
on a machine without a tray add `--no-tray` and the setup link is printed. See
`README-linux.txt` in the archive.

## Step 3: first run

1. A **small TV icon** appears in the notification area (Windows; it may be
   behind the "^", you can drag it out) or in the menu bar (Mac).
2. The first start prepares the data folder and keys, and downloads ffmpeg when
   the computer has none; the tray menu shows the download progress.
3. Then the **browser opens the setup page** with the one-time setup code
   already filled in (the code never leaves this computer). Enter a household
   name and your name, then click the button that makes you the admin.
   (screenshot TODO: setup page)
4. Afterwards, tray icon > "**Open the console**" brings you back, signed in as
   the admin.

If the browser does not open by itself: tray icon > "Continue the first setup".

## Step 4: connect your phone

1. Tray icon > "**Connect a phone**". A QR page opens in the browser.
   (screenshot TODO: QR page)
2. With the phone on the home Wi-Fi, scan it with the phone's **own camera**,
   then tap "sign in on this phone".
3. Follow the page to "Add to Home Screen" and open the remote from that icon.

The QR code is valid for a few minutes and once; when it expires, click
"Connect a phone" again. Each family member can scan their own; or in the
remote, Me > "let another phone sign in".

## Step 5: flash, set up and bind a device

1. **Flash**: connect the device to **this computer** with the USB cable, tray
   icon > "**Set up a TV**". It opens
   `http://localhost:8080/flash/` (in **Chrome or Edge**; Safari and Firefox have
   no Web Serial). The page detects the chip:
   - a Xingzhi Cube is **fully erased** the first time (removes the Xiaozhi AI
     firmware; you tick to agree), later only updated;
   - an AI Passport is **never erased** (it holds its factory identity);
   - a device already running Xiaozhi TV: choose "**Update**". Only the
     bootloader, the partition table and the firmware itself are written; the
     settings area holding Wi-Fi, server address and the device's secret is
     not, so settings and binding stay. If a new version changes the
     partition table the page says so first and offers only a clean
     reinstall (followed by "re-pair" in the admin backend).
2. **Wi-Fi by sound**: when the device shows its setup screen, open the setup
   page on the phone: scan the code on the device's screen, or open
   `http://<the computer's LAN address>:8080/tv/wifi` (the finished setup page
   links to it). Enter the Wi-Fi name and password; the "server" field is
   already this computer's address. Tap Send with the phone loud and close to
   the device (within half a metre).
3. **Bind**: once connected the device shows "BIND THIS TV" and a 6-digit code.
   Scan the QR on the device's status page with the signed-in phone, or enter
   the code in the remote under Me > "bind a new device" (the field opens the
   number pad). 5 wrong codes from one phone address, or 30 in total, within
   10 minutes pause binding for up to 10 minutes; after 30 every unbound device
   also gets a new code. A device still showing a 20-character code from an
   earlier version shows a 6-digit one after its next reconnect.
4. **Add channels**: in the console (tray > "Open the console") > admin
   backend > Channels, import a playlist from an address or a file, or pick
   one of the public playlists listed there and press "Read"; then the CCTV
   or satellite quick filter, "Select all", **Add to the channel table**
   and **Save** gives you CCTV and the provincial satellite channels in one go
   (details in [`docs/selfhost/` step 3](../selfhost/README.md#step-3-add-channels-your-own)).
   Those lists are third-party lists found on the internet; their content is
   the responsibility of their maintainers, this project does not provide or
   vouch for any stream, and you should add only what you are entitled to
   watch. On the phone, Subscriptions adds Bilibili creators and series.

**The computer's address must stay the same**: a laptop on another network, or
a router that hands the computer a new IP, and the devices lose it. A DHCP
reservation for the computer in the router is the safest fix (optional).

## The tray menu

The menu is in Chinese in the product; in order:

| Item | What it does |
| --- | --- |
| Status line ("streaming to N devices") | Current state; a problem adds a line below (ffmpeg missing, port in use...) |
| Open the console | Opens the console in the browser, signed in as the admin (also a left click on the icon on Windows) |
| Connect a phone | Phone sign-in by QR code |
| Set up a TV | Opens the web flasher (the `localhost` flasher page) |
| Devices | What each device plays, online or offline |
| Keep this computer awake | Ticked: no idle sleep while Xiaozhi TV runs (a device can be switched on any time). Unticked (default): no idle sleep while a device watches or a phone listens, until 2 minutes after the last one stops. Neither keeps the screen on |
| Start at login | Start after logging in (on by default) |
| This is my home network | Shown until you answer it once. On a laptop the client then opens its ports (web page, devices) only on that network; elsewhere, in a café for example, only this computer can reach them and the menu says "not on the home network". A new router counts as a new network: click it again at home |
| Open the logs folder / Export diagnostics | For problems: diagnostics are a zip on the Desktop without passwords or keys, for you to send to whoever helps you; nothing is uploaded |
| Restart the TV service | Try this first when a device cannot connect or the picture freezes |
| Quit Xiaozhi TV | Stops everything; devices show "server did not answer" |

Opening "Xiaozhi TV" again does not start a second copy; it opens the console.

## Data, updates and uninstalling

| OS | Data (database, keys, channel table) | Logs |
| --- | --- | --- |
| Windows | `%LOCALAPPDATA%\XiaozhiTV\` | `logs\` in the same folder |
| Mac | `~/Library/Application Support/XiaozhiTV/` | `~/Library/Logs/XiaozhiTV/` |
| Linux | `~/.local/share/xiaozhi-tv/` | `~/.local/state/xiaozhi-tv/logs/` |

- `secrets/` in the data folder holds keys; keep backups like passwords.
- **Updating**: download the new version; on Windows run the new installer (it
  closes the running client first), on a Mac drag the new app into
  Applications to replace it. Data is kept. The beta does not update itself.
- **Uninstalling**: Windows, Settings > Apps (the firewall rule and the login
  item go too); Mac, quit from the menu, drag "Xiaozhi TV" from Applications to
  the Trash and delete
  `~/Library/LaunchAgents/io.github.ai-passport-tv.XiaozhiTV.plist`. Both **keep
  the data folder**; delete it by hand when you are sure.

## Common problems

- **Bilibili videos, creators or series never start (stuck on loading)**: open
  bilibili.com in a browser on the same computer. If that fails too, this computer cannot reach Bilibili:
  the router or parental controls block it (allow it there), or a proxy app
  (Clash, Surge and the like, especially in TUN mode) is sending Bilibili through
  a node that does not work. Add direct rules for Bilibili in the proxy app:
  `DOMAIN-SUFFIX,bilibili.com,DIRECT`, `DOMAIN-SUFFIX,bilivideo.com,DIRECT`,
  `DOMAIN-SUFFIX,hdslb.com,DIRECT`, or turn TUN mode off for a while.
- **The device says "server did not answer"**: is the computer on and the
  client running? Same router? On Windows, is the home network Private? Is
  another device of the same model watching (one at a time in the beta)?
- **The phone cannot open the remote**: the phone must be on the home Wi-Fi
  (not mobile data), on the same network as the computer. A VPN or proxy app
  on the phone can also keep it from reaching the computer: turn it off.
- **The address shown is wrong (198.18.x.x, 100.x.x.x, a VPN's address)**: the
  client picks the address of the computer's Wi-Fi or Ethernet card that has the
  home router, and ignores VPN, proxy (TUN mode), Tailscale, Docker and virtual
  machine adapters; a wrong address stored by an older version is detected
  again. If it is still wrong, write the right one in the data folder's
  `config.toml` (`[server] device_address = "192.168.1.20"`, no port) and click
  "Restart the TV service": the restart re-reads `config.toml` and the
  computer's address (ports and `bind` still need the client to be quit and
  opened again). A new address from the router (DHCP) is noticed while the
  client runs, within about half a minute (up to about five minutes while a
  VPN or proxy is on).
- **The tray says a port is in use**: 8097 and 8098 are fixed by the device
  firmware; the menu names the program holding it; close it and click
  "Restart the TV service".
  When 8080 is taken the client moves to another port; phones scan a QR anyway.
- **The flasher finds no device**: another USB cable that carries data; Chrome
  or Edge; on Windows a USB-serial driver (CH340/CP210x) may be needed.
- **After an update the device keeps "opening", or its screen says
  `RE-PAIR THIS TV IN ADMIN` (it is refused)**: its settings area was
  cleared -- the flasher of v0.2.0-beta and earlier wrote it blank even
  under "Update" (fixed since), and a clean reinstall does so too -- so it
  made a new device secret while the household server still has the old one
  and refuses it. Set up its Wi-Fi by sound again, then open Devices in the
  admin backend: the refused device carries a red "key mismatch" badge.
  Press "**re-pair**" on it; within 10 minutes it connects and is
  remembered, with its channels and binding.
- **The device stops when the laptop lid closes**: closing the lid sleeps the
  computer; change the lid action in the power settings, or use a desktop or
  mini PC that stays on.

## Advanced: NAS, Docker and the command line

Without installing the client, or on a NAS / mini PC that is always on, the
same core runs as a Docker image, in Synology/UGREEN/QNAP containers, or as
plain Python: [`docs/selfhost/`](../selfhost/README.md). Those have no tray; the
first-run setup code is in the log.

Developer notes (code map, building, CI, the interface with the core):
[`developer.md`](developer.md).

## Not verified yet

- Installing, first run, tray icon and menu on a real Windows PC and an Intel
  Mac (the Windows installer is silently installed, started and uninstalled in
  CI; the tray icon has only been seen on a Mac).
- The exact SmartScreen, Gatekeeper and macOS Local Network screens and
  wording; whether Windows Defender flags the build.
- The whole flow with the real core and a real device (CI runs the frozen
  real core with a simulated device).
- "Keep this computer awake" on Windows; on macOS the idle-sleep assertion was
  confirmed to appear in the system.
- The Linux tray (GNOME has no tray area by default).

## Disclaimer

The same as in [`docs/selfhost/`](../selfhost/README.md#disclaimer): for
private, non-commercial use in your own home; ships, hosts and recommends no
channels, streams, videos, accounts or proxies; provided "as is" without
warranty (MIT licence). The public playlists listed on the Channels page are
third-party lists whose content is the responsibility of their maintainers;
the project does not provide or vouch for any live stream.
