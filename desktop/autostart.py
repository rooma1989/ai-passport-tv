"""Starting the client at login (design Q8: on by default, a tray toggle turns it off).

| OS      | Entry                                                                    |
| ------- | ------------------------------------------------------------------------ |
| macOS   | ~/Library/LaunchAgents/<bundle id>.plist (RunAtLoad), per user           |
| Windows | HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run, value "RooTV"     |
| Linux   | ~/.config/autostart/rootv.desktop (XDG autostart)                        |

All three are per user and need no administrator, so the tray can switch them.
The program is started with `--autostarted`, which keeps it from opening a
browser tab at every login.

Writing an entry never starts anything now: the LaunchAgent is not loaded with
launchctl (RunAtLoad would start a second copy immediately), it is picked up at
the next login. Removing it never stops the running copy either.

Only an installed client registers itself: from a source checkout or from the
mounted DMG the entry would point at a path that will not be there.

Before the rename (0.2.5-beta and older) the entries were named after Xiaozhi TV:
LaunchAgent io.github.ai-passport-tv.XiaozhiTV, Run value "XiaozhiTV",
xiaozhi-tv.desktop. `legacy_for_this_os` finds them so the tray can replace the
old entry with its own instead of leaving two (tray.App._initialise_login_item).
"""

from __future__ import annotations

import os
import plistlib
import sys
from pathlib import Path

from . import APP_ID, APP_NAME, BUNDLE_ID, LEGACY_APP_ID, LEGACY_BUNDLE_ID

FLAG = "--autostarted"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
# Task Manager's "Startup apps" switch: a value here whose first byte is odd
# (0x03, 0x01...) after 0x02 means "disabled by the user" -- even with our Run
# value present, Windows will not start it.
APPROVED_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run"


def _quote_windows(path: str) -> str:
    return f'"{path}"'


class MacLaunchAgent:
    def __init__(self, program: Path, home: Path | None = None, label: str = BUNDLE_ID):
        self.program = Path(program)
        self.label = label
        self.path = (home or Path.home()) / "Library" / "LaunchAgents" / f"{label}.plist"

    def plist(self) -> bytes:
        return plistlib.dumps({
            "Label": self.label,
            "ProgramArguments": [str(self.program), FLAG],
            "RunAtLoad": True,
            # A GUI program in the user's session, not a background daemon:
            # Interactive keeps it out of App Nap-style throttling by launchd.
            "ProcessType": "Interactive",
            "LimitLoadToSessionType": "Aqua",
        })

    def is_enabled(self) -> bool:
        try:
            data = plistlib.loads(self.path.read_bytes())
        except (OSError, ValueError, plistlib.InvalidFileException):
            return False
        args = data.get("ProgramArguments") or []
        return bool(args) and Path(args[0]) == self.program

    def exists(self) -> bool:
        return self.path.is_file()

    def disabled_by_user(self) -> bool:
        return False

    def enable(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".plist.tmp")
        tmp.write_bytes(self.plist())
        os.replace(tmp, self.path)

    def disable(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


class WindowsRunKey:
    def __init__(self, program: Path, winreg=None, name: str = APP_ID):
        if winreg is None:
            import winreg       # type: ignore[import-not-found]
        self.winreg = winreg
        self.program = Path(program)
        self.name = name

    def command(self) -> str:
        return f"{_quote_windows(str(self.program))} {FLAG}"

    def _open(self, key: str, write: bool):
        access = self.winreg.KEY_SET_VALUE if write else self.winreg.KEY_READ
        if write:
            return self.winreg.CreateKeyEx(self.winreg.HKEY_CURRENT_USER, key, 0, access)
        return self.winreg.OpenKey(self.winreg.HKEY_CURRENT_USER, key, 0, access)

    def is_enabled(self) -> bool:
        try:
            with self._open(RUN_KEY, False) as key:
                value, _ = self.winreg.QueryValueEx(key, self.name)
        except OSError:
            return False
        if str(self.program).lower() not in str(value).lower():
            return False
        return not self._disabled_in_task_manager()

    def exists(self) -> bool:
        try:
            with self._open(RUN_KEY, False) as key:
                self.winreg.QueryValueEx(key, self.name)
        except OSError:
            return False
        return True

    def disabled_by_user(self) -> bool:
        return self._disabled_in_task_manager()

    def _disabled_in_task_manager(self) -> bool:
        try:
            with self._open(APPROVED_KEY, False) as key:
                value, _ = self.winreg.QueryValueEx(key, self.name)
        except OSError:
            return False
        return isinstance(value, (bytes, bytearray)) and len(value) > 0 and value[0] & 1 == 1

    def enable(self) -> None:
        with self._open(RUN_KEY, True) as key:
            self.winreg.SetValueEx(key, self.name, 0, self.winreg.REG_SZ, self.command())
        # Clear a Task Manager "disabled" mark, otherwise the toggle would show
        # on and nothing would start. 12 bytes: 0x02 = enabled, then a zero time.
        try:
            with self._open(APPROVED_KEY, True) as key:
                self.winreg.SetValueEx(key, self.name, 0, self.winreg.REG_BINARY, bytes([2] + [0] * 11))
        except OSError:
            pass

    def disable(self) -> None:
        for key_name in (RUN_KEY, APPROVED_KEY):
            try:
                with self._open(key_name, True) as key:
                    self.winreg.DeleteValue(key, self.name)
            except OSError:
                pass


class XdgAutostart:
    def __init__(self, program: Path, config_home: Path | None = None, file_name: str = "rootv.desktop"):
        self.program = Path(program)
        base = config_home or Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
        self.path = Path(base) / "autostart" / file_name

    def entry(self) -> str:
        program = str(self.program).replace("\\", "\\\\").replace('"', '\\"')
        return ("[Desktop Entry]\n"
                "Type=Application\n"
                f"Name={APP_NAME}\n"
                "Name[en]=RooTV\n"
                f"Comment={APP_NAME}电脑客户端（原名小智电视）\n"
                f'Exec="{program}" {FLAG}\n'
                "Terminal=false\n"
                "X-GNOME-Autostart-enabled=true\n")

    def is_enabled(self) -> bool:
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError:
            return False
        return str(self.program) in text and "Hidden=true" not in text

    def exists(self) -> bool:
        return self.path.is_file()

    def disabled_by_user(self) -> bool:
        try:
            return "Hidden=true" in self.path.read_text(encoding="utf-8")
        except OSError:
            return False

    def enable(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(self.entry(), encoding="utf-8")

    def disable(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


def for_this_os(program: Path, platform_name: str | None = None):
    platform_name = platform_name or sys.platform
    if platform_name == "darwin":
        return MacLaunchAgent(program)
    if platform_name == "win32":
        return WindowsRunKey(program)
    return XdgAutostart(program)


def legacy_for_this_os(program: Path, platform_name: str | None = None, home: Path | None = None,
                       winreg=None, config_home: Path | None = None):
    """The entry the pre-rename client registered (only `exists`, `disabled_by_user`
    and `disable` are meaningful: it points at the old program, whatever `program` is)."""
    platform_name = platform_name or sys.platform
    if platform_name == "darwin":
        return MacLaunchAgent(program, home=home, label=LEGACY_BUNDLE_ID)
    if platform_name == "win32":
        return WindowsRunKey(program, winreg=winreg, name=LEGACY_APP_ID)
    return XdgAutostart(program, config_home=config_home, file_name="xiaozhi-tv.desktop")


def supported(frozen: bool, translocated: bool) -> bool:
    """Only an installed copy registers itself (see the module docstring)."""
    return frozen and not translocated
