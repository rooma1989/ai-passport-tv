<p align="right">
  <a href="README.zh_CN.md">简体中文</a> · <strong>English</strong>
</p>

# RooTV PC client (beta)

*RooTV was called Xiaozhi TV up to 0.2.5-beta; see
[Upgrading from Xiaozhi TV](#upgrading-from-xiaozhi-tv).*

Turns a Windows PC or a Mac at home into the "TV station" for your tiny TVs: the
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
- A tiny TV device: Xingzhi Cube 1.54 (Xiaozhi AI case) or FoloToy AI Passport.
- For flashing: a USB data cable and Chrome or Edge on the computer.
- Internet on the first run: the client downloads the ffmpeg video component
  (a few tens of MB; progress shows in the tray menu).

## Step 1: download

From the release page, download the file for your computer (the project
website's download page picks it for you):

| Your computer | Download |
| --- | --- |
| Windows 10/11 | `RooTV-<version>-windows-x64-setup.exe` (installer, recommended); `RooTV-<version>-windows-x64.zip` (portable) |
| Mac with Apple silicon (M1, M2, M3, M4...) | `RooTV-<version>-macos-arm64.dmg` |
| Mac with an Intel processor | `RooTV-<version>-macos-x86_64.dmg` |
| Linux (bonus) | `rootv-<version>-linux-x86_64.tar.gz`, see `README-linux.txt` inside |

**Which Mac?** Apple menu > About This Mac: "Chip Apple M..." is Apple silicon,
"Processor Intel..." is Intel.

**Checking the download (optional)**: the release has `SHA256SUMS-desktop`. On Windows,
`Get-FileHash .\RooTV-...-setup.exe` in PowerShell; on a Mac,
`shasum -a 256 ~/Downloads/RooTV-....dmg` in Terminal. The result must equal
the line in `SHA256SUMS-desktop`.

## Step 2: install

### Windows

1. Double-click `RooTV-...-setup.exe`.
2. If the blue "**Windows protected your PC**" (SmartScreen) appears: click
   "**More info**", then "**Run anyway**". The beta has no code-signing
   certificate; the file is not damaged. (screenshot TODO: SmartScreen)
3. User Account Control asks to allow changes: "**Yes**". Administrator rights
   are needed once, to install into Program Files and add the firewall rule.
4. Click through the wizard and finish with "Launch RooTV" ticked.

The installer adds a firewall rule for RooTV that applies **only to
Private networks**, so devices and phones can connect. If Windows has your home
Wi-Fi marked Public, the tray menu says so; to change it: Settings > Network &
Internet > your Wi-Fi (or Ethernet) > Network profile type > Private.

Windows Defender occasionally flags programs packaged with PyInstaller. If the
program is removed or quarantined after installing, check the SHA-256 as above,
restore it under Windows Security > Virus & threat protection > Protection
history, and please report it on GitHub.

### Mac

1. Double-click the `.dmg` and **drag "RooTV" onto "Applications"** in the
   window that opens. Drag it first: run straight from the DMG window, macOS
   puts it in a temporary read-only place and its data and login item break (the
   tray menu warns about this too).
2. Open the Applications folder and double-click "RooTV".
3. macOS says "**"RooTV" Not Opened**" or that the developer cannot be
   verified: click "**Done**" (**not "Move to Trash"**).
4. Open **System Settings > Privacy & Security**, scroll to Security, find
   ""RooTV" was blocked to protect your Mac", click "**Open Anyway**",
   confirm "Open Anyway" and enter your password. (screenshot TODO: Open Anyway)
   - On macOS 12-14 this also works: in Applications, **Control-click "RooTV" >
     Open > Open**. From macOS 15 only the steps above work.
5. Later opens do not ask again (each update to a new version asks once more;
   do the same).
6. If macOS asks to allow "RooTV" to find devices on your local network,
   click "**Allow**", or it cannot reach the sources and devices at home. If the
   Mac's firewall is on, it also asks to accept incoming connections: "Allow".

On a Mac the client is only a small TV icon in the menu bar at the top of the
screen, with no Dock icon. That is intended.

### Linux (bonus, unverified)

Unpack into your home folder as `~/rootv` and run `~/rootv/rootv`;
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
   - a device already running the TV firmware: choose "**Update**". Only the
     bootloader, the partition table and the firmware itself are written; the
     settings area holding Wi-Fi, server address and the device's secret is
     not, so settings and binding stay. If a new version changes the
     partition table the page says so first and offers only a clean
     reinstall (followed by "re-pair" in the console's Admin > Devices).
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
4. **Add channels**: in the console (tray > "Open the console") > **Admin**
   (the shield icon in the top bar) > Channels, import a playlist from an address or a file, or pick
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
| Keep this computer awake | Ticked: no idle sleep while RooTV runs (a device can be switched on any time). Unticked (default): no idle sleep while a device watches or a phone listens, until 2 minutes after the last one stops. Neither keeps the screen on |
| Start at login | Start after logging in (on by default) |
| This is my home network | Shown until you answer it once. On a laptop the client then opens its ports (web page, devices) only on that network; elsewhere, in a café for example, only this computer can reach them and the menu says "not on the home network". A new router counts as a new network: click it again at home |
| Open the logs folder / Export diagnostics | For problems: diagnostics are a zip on the Desktop without passwords or keys, for you to send to whoever helps you; nothing is uploaded |
| Restart the TV service | Try this first when a device cannot connect or the picture freezes |
| Quit RooTV | Stops everything; devices show "server did not answer" |

Opening "RooTV" again does not start a second copy; it opens the console.

## Watch on the PC, even without a TV

The console's Remote page (tray → "Open the console") plays channels right in this computer's browser, with the
source's own picture and sound:

- **No TV, or the TV is offline**: the channel grid says "click to watch on this computer", and a click plays that
  channel in the player at the top of the page. An account without a TV still gets the channel table (CCTV,
  provincial channels…), its own creator and series subscriptions, Bilibili search and casting; the cast page's
  button becomes "cast to this computer". When you get a TV later, enter the code on its screen on the Devices page.
- **The TV is online**: above the grid, "on click: switch the TV ｜ watch on this computer" picks what a click does
  (remembered in this browser). "Watch on this computer" plays on the PC without touching the TV. Ticking "follow the
  TV" in the player switches to watching whatever the TV is showing.
- The player has previous/next channel (within the tab you clicked in), next video/episode for creators and series,
  and close. When a video ends the next one starts. When Bilibili or YouTube cannot be reached, the player says so
  (for example "Bilibili cannot be reached: check the router or proxy").
- **Separate progress**: creators and series start where the TV would play next; watching on the PC never marks the
  TV's videos watched or moves its saved position. The PC's own place is kept in memory (forgotten when the client
  restarts, which then starts from the TV's place again). Picking an episode on the PC also plays only on the PC.
- Only channels from the global channel table and your own subscriptions (not the owner's channels on a shared TV);
  people signed in with a guest code cannot use it.
- Transcoding runs only while the player is open and stops at once on close; at most 3 streams on the computer and
  2 per person (two computers or two pages).

## Programme guide (optional)

Off by default. In the console > **Admin** > **Channels → Programme
guide**, tick one of the public guides (112114, fanmingming) or paste an XMLTV
address and save: the channel tiles, the phone remote's list and the PC
player's title then show what is on now and until when. These
are third-party guides found on the internet; their content is the
responsibility of their maintainers. The client fetches each guide every 8
hours into `epg/` in its data folder and keeps only your channels' next day in
memory (about 0.5 MB, a fraction of a second of CPU per read); details in
[`docs/selfhost/`](../selfhost/README.md#programme-guide-optional).

## Data, updates and uninstalling

| OS | Data (database, keys, channel table) | Logs |
| --- | --- | --- |
| Windows | `%LOCALAPPDATA%\RooTV\` | `logs\` in the same folder |
| Mac | `~/Library/Application Support/RooTV/` | `~/Library/Logs/RooTV/` |
| Linux | `~/.local/share/rootv/` | `~/.local/state/rootv/logs/` |

- `secrets/` in the data folder holds keys; keep backups like passwords.
- **Updating**: download the new version; on Windows run the new installer (it
  closes the running client first), on a Mac drag the new app into
  Applications to replace it. Data is kept. The beta does not update itself.
- **Uninstalling**: Windows, Settings > Apps (the firewall rule and the login
  item go too); Mac, quit from the menu, drag "RooTV" from Applications to
  the Trash and delete
  `~/Library/LaunchAgents/io.github.ai-passport-tv.RooTV.plist`. Both **keep
  the data folder**; delete it by hand when you are sure.

### Upgrading from Xiaozhi TV

RooTV is the same program as Xiaozhi TV under a new name and icon. Install it like an update;
nothing has to be set up again.

- **Windows**: run the RooTV installer. It runs the Xiaozhi TV uninstaller first
  (old program folder, Start-menu entries, firewall rule "Xiaozhi TV", login
  item), then installs into `Program Files\RooTV` with a firewall rule "RooTV".
- **Mac**: drag RooTV into Applications and open it. A Xiaozhi TV still running
  in the menu bar is asked to quit; afterwards you can delete "Xiaozhi TV" from
  Applications.
- **First start** (all systems): when `RooTV` does not exist yet and no old
  program still uses the old folder, `XiaozhiTV` (`xiaozhi-tv` on Linux) is
  renamed to `RooTV` (`rootv`) in one step, the logs folder likewise, and the
  old path keeps only a note, `MOVED-TO-RooTV.txt`. The old login item is
  replaced by the new one, on or off as it was. If the old folder is still in
  use, this run uses it and the move happens on a later start; if both folders
  hold data, the new one is used and the old one is left alone.
- `XIAOZHI_TV_DATA` and `XIAOZHI_TV_CORE` still work; the new names are
  `ROOTV_DATA` and `ROOTV_CORE`. A folder chosen with them or with `--data` is
  never moved.

## Bilibili login (optional)

Without logging in, Bilibili plays as a guest: up to 720p, and bangumi episodes
for premium members only give a short preview, so they are skipped. On the PC
client and a household server each family member can log in with their own
Bilibili account: open **Settings**, find the Bilibili account section, click the
scan-to-log-in button and scan the code with the Bilibili app, then confirm on the
phone. The page shows the nickname, the avatar and whether the account is a
premium member.

What a login changes:

- **Watching on the PC** uses the viewer's login (or the device owner's when the
  viewer has none) and asks for 1080p, or 1080p+ for premium members. The TV
  keeps 720p; its screen is too small to show more.
- **Premium bangumi**: with a premium login, series keep the member episodes and
  the TV plays them. Without one, adding such a series says it needs a premium
  membership and where to log in.
- **Importing followed creators**: the subscriptions page gets a button that lists
  the creators you follow, with a search box, select all and select none; ones
  you already subscribe to are marked. At most 12 creators fit, because each one
  is a channel whose name has to fit in the device's settings packet.

The login stays on this computer: `secrets/bilibili/<user id>.json` in the data
folder, readable only by your user. It is never written to logs, the
diagnostics bundle or database backups, and logging out deletes it at once.
These are Bilibili's web APIs and normal use is fine; if you worry about
Bilibili's anti-abuse checks, use a secondary account. When Bilibili rejects the
saved login (it expired, or you logged out elsewhere), the page asks you to scan
again and playback falls back to guest quality. If the router blocks Bilibili,
the page says Bilibili cannot be reached and to check the router or proxy. The
cloud relay does not have this feature.

## YouTube (optional)

YouTube is off by default; Bilibili and live channels work without it. Turning
it on is for households that already have their own way to reach YouTube from
this computer: **this project provides no proxy and gives no instructions for
getting one.**

1. In the console (signed in as the admin) open **Settings → YouTube** and
   choose **This computer**.
2. Proxy: type the address your proxy program gives for this computer, for
   example `http://127.0.0.1:<port>` or `socks5://127.0.0.1:<port>`, or press
   **Detect**, which reads the system proxy (macOS network
   settings, the Windows proxy setting, the `https_proxy` / `all_proxy`
   environment variables on Linux) and fills in what it found. Leave it empty
   when the proxy program routes everything by itself (TUN / enhanced mode) or
   YouTube is reachable directly.
3. Press **Save**. The first time, the client downloads its two YouTube
   components, yt-dlp and deno (about 80 MB, through that proxy; you can also
   enter a download mirror prefix: what replaces `https://github.com` in the
   download address, so a GitHub accelerator that takes a whole address is
   written `https://<accelerator>/https://github.com`), and checks each file
   against the SHA-256 written in the program. Progress shows in the same section.
4. Press **Test**: it resolves a public test video and answers in plain words
   ("success in 12 s", "can't reach YouTube: check the proxy", "needs sign-in:
   import cookies").
5. **Cookies**: YouTube often asks proxy and data-centre addresses to "sign in
   to confirm you're not a bot". Then import a `cookies.txt` (Netscape format):
   use a **secondary account**, sign in to it in a private browser window, open
   YouTube, export only youtube.com with a cookies.txt export extension, then
   close the private window without signing out (signing out invalidates the
   cookies). The client keeps only the youtube.com and google.com lines, stores
   them in `<data>/youtube/cookies.txt` readable only by you, and shows only
   how many there are. The file is that account's login: do not share it.

   **Or read the login from a browser on this computer** (no extension needed):
   under **Read from a browser**, pick Chrome, Edge, Firefox, Safari or Brave
   (only those found on this computer are listed) and, for all but Safari, the
   browser profile (listed by name, with the Google account signed in to it),
   then press **Read**. yt-dlp reads that profile's cookies into a temporary
   file that is deleted straight away; only the youtube.com and google.com ones
   are kept, in the same `cookies.txt` (0600), and the page shows "read (N,
   YouTube domains) · account: …". **It reads whatever account is signed in to
   that browser, which is usually your main account**: create a browser
   profile that is signed in only to the secondary account and pick that one.
   On a Mac, Chrome, Edge and Brave make the Keychain ask whether their "Safe
   Storage" key may be used (enter your Mac password and allow), and Safari
   needs RooTV to have **Full Disk Access** (System Settings → Privacy &
   Security), then a restart of RooTV. On Windows, quit the browser first (it
   locks its cookie file); recent Chrome and Edge encrypt cookies so that only
   the browser itself can read them, so use Firefox or the file import there.
   A browser that is not signed in to YouTube is refused and the old cookies
   are kept. This button only appears when the console is opened on this
   computer itself (`http://localhost:…`) by the admin: opened from another
   device, over the LAN or a tunnel, the server refuses it, because it reads the
   browser of the computer running RooTV.

   Cookies expire: YouTube logs them out after some weeks, or as soon as the
   account signs out in that browser. When **Test** says "needs sign-in" again,
   read or import them again.
6. YouTube changes often. When videos stop playing, press **Check for updates**
   and **Update yt-dlp** (the new version is verified against the release's
   own `SHA2-256SUMS`).

Changes apply without quitting: saving restarts only the parts that changed
(a new proxy restarts the YouTube helper alone). YouTube videos, creators and
playlists (series) then work as on the cloud service. "Own node" is for a
YouTube node you run yourself (`secrets/youtube-node.json`).

### Searching YouTube

Open **Search** (the magnifier tab, or press `/`) and switch the **Bilibili | YouTube**
toggle at the top to **YouTube**; the page remembers the choice in this browser. Pick
**Videos**, **Channels** or **Playlists**, type a few words and press Enter (it
also searches after you stop typing for a moment). Each search runs yt-dlp
through your proxy, so it takes a few seconds; the line under the box counts
the seconds and gives up after 25 s. Results show the thumbnail, title,
channel, length, views and roughly when it was uploaded:

- **Play** sends the video to the TV you picked in "Play to", or to this computer.
- **Subscribe to channel** makes the channel a TV channel; new videos queue up.
- **Add as series** (playlists) plays the list in order and remembers the episode.

Pasting a YouTube link into the same box plays it directly. The same search a
second time within 10 minutes answers at once; only one YouTube search runs at
a time (YouTube playback uses the same helper), so a second one is told to wait
a few seconds. When the page says YouTube is not turned on, turn on local mode
as above; when it asks for cookies, see step 5; when it says the YouTube node is
busy, the helper is starting a video and the page retries by itself.

**Import your YouTube subscriptions**: with This computer mode and cookies in
place, the admin's **Creators** page has **Import the channels I subscribe to
on YouTube**. It lists the cookies account's subscriptions (yt-dlp reads
`youtube.com/feed/channels`, falling back to the channels in
`feed/subscriptions`; the list is kept 10 minutes and fetched again at most
once a minute), with search, select all / none and the channels already
subscribed marked. Each one becomes a creator channel, within the same limits
as the Bilibili import (at most 12 creators, and what fits in the TV's channel
list); adding fetches each channel's video list, about ten seconds each, in the
background.

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
  and refuses it. Set up its Wi-Fi by sound again, then open the console's
  Admin > Devices: the refused device carries a red "key mismatch" badge.
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
- YouTube local mode on Windows and Linux (written for them, run only on a Mac), and how often YouTube
  asks for sign-in with imported cookies (on the Mac, through a proxy, it asked every time without).
- Reading the YouTube login from a real browser: developed and tested against a fake yt-dlp and canned
  `Local State` / `profiles.ini` files only. The real Keychain prompt, Safari's Full Disk Access, Windows'
  locked and app-bound-encrypted Chrome/Edge cookies, and Linux keyrings have not been tried; nor has the
  subscription import against a real account (only canned yt-dlp output shaped like its `feed/channels` answer).
- Watching a channel directly on the PC: seen in a browser on a Mac only with a local test source (a short test
  video over local HTTP); real Bilibili and YouTube sources were exercised only with canned answers in unit tests,
  and browsers on Windows have not been tried.

- Bilibili login against the real Bilibili: QR login, 1080p playback, member
  bangumi and the followings import were developed against recorded answer
  shapes only (the developer's router blocks Bilibili).

## Disclaimer

The same as in [`docs/selfhost/`](../selfhost/README.md#disclaimer): for
private, non-commercial use in your own home; ships, hosts and recommends no
channels, streams, videos, accounts or proxies; provided "as is" without
warranty (MIT licence). The public playlists listed on the Channels page are
third-party lists whose content is the responsibility of their maintainers;
the project does not provide or vouch for any live stream.
