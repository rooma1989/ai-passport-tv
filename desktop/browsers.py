"""Which browser "打开电脑版" uses (macOS; elsewhere always the system default).

The choice is a bundle id kept in tray.json under `console_browser`; "" (or no
entry) follows the system default browser, which is what the tray always did.
The OS default browser is never changed: a chosen browser is started with
`open -b <bundle id> <url>` for this one link.

Only browsers from a known list are offered. Asking LaunchServices for every
app that handles http would also list terminals, note apps and media players
(on the developer's Mac: iTerm, Evernote, VLC, ChatGPT...), so detection looks
for the known bundle ids only: the .app bundles in /Applications and
~/Applications (Info.plist read with plistlib) plus one `mdfind` query for
copies elsewhere. Nothing here needs a dependency.

Everything that touches the machine goes through `System`, so the tests run
the whole flow with fakes on any OS.
"""

from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

PREF_KEY = "console_browser"
SYSTEM_DEFAULT = ""

# (bundle id, name shown in the menu), in menu order. The ids marked "unverified"
# come from third-party listings, not from an installed copy; a wrong one only
# never matches.
KNOWN: tuple[tuple[str, str], ...] = (
    ("com.apple.Safari", "Safari"),
    ("com.apple.SafariTechnologyPreview", "Safari Technology Preview"),
    ("com.google.Chrome", "Google Chrome"),
    ("com.google.Chrome.beta", "Google Chrome Beta"),
    ("com.google.Chrome.dev", "Google Chrome Dev"),
    ("com.google.Chrome.canary", "Google Chrome Canary"),
    ("org.chromium.Chromium", "Chromium"),
    ("com.microsoft.edgemac", "Microsoft Edge"),
    ("com.microsoft.edgemac.Beta", "Microsoft Edge Beta"),
    ("com.microsoft.edgemac.Dev", "Microsoft Edge Dev"),
    ("com.microsoft.edgemac.Canary", "Microsoft Edge Canary"),
    ("org.mozilla.firefox", "Firefox"),
    ("org.mozilla.firefoxdeveloperedition", "Firefox Developer Edition"),
    ("org.mozilla.nightly", "Firefox Nightly"),
    ("com.brave.Browser", "Brave"),
    ("com.brave.Browser.beta", "Brave Beta"),
    ("com.brave.Browser.nightly", "Brave Nightly"),
    ("company.thebrowser.Browser", "Arc"),
    ("company.thebrowser.dia", "Dia"),
    ("com.operasoftware.Opera", "Opera"),
    ("com.operasoftware.OperaGX", "Opera GX"),
    ("com.vivaldi.Vivaldi", "Vivaldi"),
    ("com.kagi.kagimacOS", "Orion"),
    ("com.duckduckgo.macos.browser", "DuckDuckGo"),
    ("app.zen-browser.zen", "Zen"),
    ("io.gitlab.librewolf-community", "LibreWolf"),
    ("net.waterfox.waterfox", "Waterfox"),
    ("org.torproject.torbrowser", "Tor Browser"),
    ("ru.yandex.desktop.yandex-browser", "Yandex"),
    ("com.quark.desktop", "夸克"),
    ("com.tencent.QQBrowserLite", "QQ浏览器 Lite"),     # unverified
)
NAMES = dict(KNOWN)

APP_DIRS = ("/Applications", "~/Applications")
MDFIND_TIMEOUT_S = 10.0
OPEN_TIMEOUT_S = 15.0
DIALOG_TIMEOUT_S = 330.0        # the dialog gives up after 300 s by itself
DIALOG_TITLE = "小鼠电视"


@dataclass(frozen=True)
class Browser:
    bundle_id: str
    name: str


def read_plist(path: Path) -> dict:
    with open(path, "rb") as handle:
        data = plistlib.load(handle)
    return data if isinstance(data, dict) else {}


@dataclass
class System:
    """What detection and opening touch on the machine (fakes in the tests)."""
    platform: str = field(default_factory=lambda: sys.platform)
    run: Callable = subprocess.run              # mdfind, open -b, osascript
    read_plist: Callable = read_plist
    app_dirs: tuple = APP_DIRS


def supported(system: System) -> bool:
    return system.platform == "darwin"


def name_of(bundle_id: str) -> str:
    return NAMES.get(bundle_id, bundle_id)


def _scan_dirs(system: System) -> set[str]:
    found = set()
    for folder in system.app_dirs:
        folder = Path(os.path.expanduser(folder))
        try:
            apps = sorted(folder.glob("*.app"))
        except OSError:
            continue
        for app in apps:
            try:
                bundle_id = system.read_plist(app / "Contents" / "Info.plist").get("CFBundleIdentifier")
            except Exception:       # noqa: BLE001 - an unreadable app is just not a browser here
                continue
            if bundle_id in NAMES:
                found.add(bundle_id)
    return found


def _mdfind(system: System) -> set[str]:
    """Known browsers installed elsewhere (another folder, a second disk). One query."""
    query = " || ".join(f"kMDItemCFBundleIdentifier == '{bundle_id}'" for bundle_id, _ in KNOWN)
    try:
        result = system.run(["mdfind", query], capture_output=True, text=True, encoding="utf-8",
                            errors="replace", timeout=MDFIND_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        return set()
    if result.returncode != 0:
        return set()
    found = set()
    for line in result.stdout.splitlines():
        path = line.strip()
        # The Trash and mounted disk images are not installed copies.
        if not path.endswith(".app") or "/.Trash/" in path or path.startswith("/Volumes/"):
            continue
        try:
            bundle_id = system.read_plist(Path(path) / "Contents" / "Info.plist").get("CFBundleIdentifier")
        except Exception:       # noqa: BLE001
            continue
        if bundle_id in NAMES:
            found.add(bundle_id)
    return found


def detect(system: System | None = None) -> tuple[Browser, ...]:
    """The known browsers installed on this Mac, in menu order; () elsewhere."""
    system = system or System()
    if not supported(system):
        return ()
    found = _scan_dirs(system) | _mdfind(system)
    return tuple(Browser(bundle_id, name) for bundle_id, name in KNOWN if bundle_id in found)


def chosen(prefs, system: System) -> str:
    """The saved choice, "" for the system default (and always "" off macOS)."""
    if not supported(system):
        return SYSTEM_DEFAULT
    value = prefs.get(PREF_KEY, SYSTEM_DEFAULT)
    return value if isinstance(value, str) else SYSTEM_DEFAULT


def open_with(system: System, bundle_id: str, url: str) -> bool:
    """`open -b <id> <url>`; False when the app is not there or open fails."""
    try:
        result = system.run(["open", "-b", bundle_id, url], capture_output=True, timeout=OPEN_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return True         # still launching: opening it a second time would be worse
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


# --------------------------------------------------------------- dialogs (osascript)
# The texts go in as arguments (`on run argv`), so nothing has to be quoted for
# AppleScript. Each answers None when the dialog itself could not be shown.

FALLBACK = "这次用系统默认浏览器打开"
RECHOOSE = "重新选择浏览器"
CANCEL = "取消"

_ASK_SCRIPT = """on run argv
activate
try
set answer to display dialog (item 1 of argv) with title (item 5 of argv) buttons {item 4 of argv, item 3 of argv, item 2 of argv} default button (item 2 of argv) cancel button (item 4 of argv) with icon caution giving up after 300
if gave up of answer then return ""
return button returned of answer
on error number -128
return ""
end try
end run"""

_CHOOSE_SCRIPT = """on run argv
activate
set choices to items 4 thru -1 of argv
set answer to choose from list choices with title (item 2 of argv) with prompt (item 1 of argv) default items {item 3 of argv} OK button name "用它打开" cancel button name "取消"
if answer is false then return ""
return item 1 of answer
end run"""


def _osascript(system: System, script: str, args: list[str]) -> str | None:
    try:
        result = system.run(["osascript", "-e", script, *args], capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=DIALOG_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def ask_missing(system: System, bundle_id: str) -> str | None:
    """The chosen browser did not open: "fallback", "rechoose", "cancel", or None (no dialog)."""
    message = (f"打不开「{name_of(bundle_id)}」，它可能已经被删除或移动了。\n\n"
               "可以这次先用系统默认浏览器打开电脑版（不改你选的浏览器），或者重新选一个。")
    answer = _osascript(system, _ASK_SCRIPT, [message, FALLBACK, RECHOOSE, CANCEL, DIALOG_TITLE])
    if answer is None:
        return None
    return {FALLBACK: "fallback", RECHOOSE: "rechoose"}.get(answer, "cancel")


def ask_choice(system: System, browsers: tuple[Browser, ...], current: str) -> str | None:
    """Pick a browser from a list: a bundle id, SYSTEM_DEFAULT, or None (cancelled / no dialog)."""
    options = [("跟随系统默认浏览器", SYSTEM_DEFAULT)] + [(b.name, b.bundle_id) for b in browsers]
    labels = [label for label, _ in options]
    default = next((label for label, value in options if value == current), labels[0])
    answer = _osascript(system, _CHOOSE_SCRIPT, ["电脑版用哪个浏览器打开？", DIALOG_TITLE, default, *labels])
    if not answer:
        return None
    return dict(options).get(answer)


# --------------------------------------------------------------- opening the console

def open_console(url: str, prefs, open_default: Callable[[str], None], system: System | None = None,
                 available: Callable[[], tuple[Browser, ...]] | None = None,
                 notify: Callable[[str], None] = lambda text: None) -> str:
    """Open the console link with the chosen browser.

    Returns what happened: "default", "chosen", "fallback" (the chosen one did
    not open; the system default did, this time only), "rechosen" (a new
    choice was made, saved and used) or "cancelled". A fallback never touches
    the saved choice; only picking another browser in the dialog does.
    """
    system = system or System()
    bundle_id = chosen(prefs, system)
    if not bundle_id:
        open_default(url)
        return "default"
    if open_with(system, bundle_id, url):
        return "chosen"
    name = name_of(bundle_id)
    answer = ask_missing(system, bundle_id)
    if answer == "rechoose":
        browsers = (available or (lambda: detect(system)))()
        browsers = tuple(b for b in browsers if b.bundle_id != bundle_id)
        new = ask_choice(system, browsers, SYSTEM_DEFAULT)
        if new is None:
            return "cancelled"
        prefs.set(PREF_KEY, new)
        notify(f"电脑版以后用「{name_of(new) if new else '系统默认浏览器'}」打开")
        if new and open_with(system, new, url):
            return "rechosen"
        open_default(url)
        return "rechosen"
    if answer == "cancel":
        return "cancelled"
    # "fallback", or no dialog could be shown: open it anyway, once.
    open_default(url)
    notify(f"打不开「{name}」，这次用系统默认浏览器打开了电脑版；"
           "要换浏览器，在菜单「电脑版用的浏览器」里选")
    return "fallback"
