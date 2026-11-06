"""Installing an update the core has downloaded (design
docs/superpowers/specs/2026-11-05-pc-client-updates-design.md, section 4).

The core checks, downloads and verifies (server/selfhost/updater.py) and reports
the file through the control API (`GET /local/v1/update/ready`). The tray hashes
it again, writes a small swap script for this platform into `<data>/updates/`,
starts it detached, stops the core and quits. The script waits for the tray to
exit, swaps the program, starts the new one, and swaps back if it does not come
up; a failure is left in `updates/result.json` for the next start to report.

macOS: the DMG's RooTV.app replaces the running bundle (ditto, rename-swap).
Windows installed: the Inno Setup installer, silent (UAC asks once).
Windows portable and Linux: the archive's top folder replaces the program folder.
The Windows and Linux scripts follow the documentation and have not been run on
those systems.

Nothing here imports `server`; the data folder and the control API are the
only contact with the core.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


class UpdateFailed(Exception):
    """Cannot install; the message is for people."""


@dataclass(frozen=True)
class Plan:
    script: Path            # written into <data>/updates/
    command: list[str]      # what starts it
    text: str               # the script's content


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def check_file(ready: dict) -> Path:
    """The downloaded file, hashed again against what the signed manifest said."""
    path = Path(str(ready.get("path") or ""))
    if not path.is_file():
        raise UpdateFailed("找不到下好的新版本，等它重新下载")
    if path.stat().st_size != ready.get("size") or sha256(path) != ready.get("sha256"):
        raise UpdateFailed("下好的新版本校验不对，已经作废，会重新下载")
    return path


def app_bundle(executable: Path) -> Path | None:
    """macOS: the .app the running program is in (…/RooTV.app/Contents/MacOS/RooTV)."""
    for parent in executable.resolve().parents:
        if parent.suffix == ".app":
            return parent
    return None


def installed_windows(executable: Path) -> bool:
    """Windows: installed by Inno Setup (an unins*.exe beside the program) rather than unzipped."""
    folder = executable.resolve().parent
    return any(folder.glob("unins*.exe")) or any(folder.parent.glob("unins*.exe"))


def writable(folder: Path) -> bool:
    return os.access(folder, os.W_OK)


MAC_SCRIPT = r"""#!/bin/sh
# RooTV update (desktop/update.py): swap the app bundle, start it, roll back if it does not come up.
DMG="$1"; APP="$2"; DATA="$3"; FROM="$4"; TO="$5"; WAITPID="$6"
LOG="$DATA/updates/install.log"
exec >>"$LOG" 2>&1
echo "update $FROM -> $TO at $(date)"
fail() {
  printf '{"ok": false, "from": "%s", "to": "%s", "error": "%s"}\n' "$FROM" "$TO" "$1" > "$DATA/updates/result.json"
  echo "failed: $1"
}
i=0
while kill -0 "$WAITPID" 2>/dev/null && [ $i -lt 120 ]; do sleep 0.5; i=$((i+1)); done
i=0
while [ -f "$DATA/run/control.json" ] && [ $i -lt 60 ]; do sleep 0.5; i=$((i+1)); done
MNT=$(mktemp -d "${TMPDIR:-/tmp}/rootv-update.XXXXXX") || { fail "mktemp"; open "$APP"; exit 1; }
if ! hdiutil attach -nobrowse -readonly -noverify -mountpoint "$MNT" "$DMG"; then
  fail "挂载不了安装包"; open "$APP"; exit 1
fi
NEW=$(ls -d "$MNT"/*.app 2>/dev/null | head -n 1)
STAGE="${APP%.app}.new.app"; OLD="${APP%.app}.old.app"
rm -rf "$STAGE" "$OLD"
if [ -z "$NEW" ] || ! ditto "$NEW" "$STAGE"; then
  hdiutil detach "$MNT" -quiet; rm -rf "$STAGE"; fail "安装包里没有程序"; open "$APP"; exit 1
fi
hdiutil detach "$MNT" -quiet
rmdir "$MNT" 2>/dev/null
if ! mv "$APP" "$OLD"; then rm -rf "$STAGE"; fail "换不了程序"; open "$APP"; exit 1; fi
if ! mv "$STAGE" "$APP"; then mv "$OLD" "$APP"; fail "换不了程序"; open "$APP"; exit 1; fi
xattr -dr com.apple.quarantine "$APP" 2>/dev/null
STARTED=$(date +%s)
open "$APP"
i=0
while [ $i -lt "${ROOTV_UPDATE_WAIT:-120}" ]; do
  if [ -f "$DATA/run/control.json" ] && [ "$(stat -f %m "$DATA/run/control.json")" -ge "$STARTED" ]; then
    rm -rf "$OLD"; echo "done"; exit 0
  fi
  sleep 0.5; i=$((i+1))
done
pkill -f "$APP/Contents/" 2>/dev/null
sleep 3
rm -rf "$APP"; mv "$OLD" "$APP"
fail "新版本 60 秒内没有起来，已经退回"
open "$APP"
exit 1
"""

POSIX_ARCHIVE_SCRIPT = r"""#!/bin/sh
# RooTV update (desktop/update.py): swap the program folder from the tarball, roll back if it does not come up.
ARCHIVE="$1"; DIR="$2"; DATA="$3"; FROM="$4"; TO="$5"; WAITPID="$6"; EXE="$7"
LOG="$DATA/updates/install.log"
exec >>"$LOG" 2>&1
echo "update $FROM -> $TO at $(date)"
fail() {
  printf '{"ok": false, "from": "%s", "to": "%s", "error": "%s"}\n' "$FROM" "$TO" "$1" > "$DATA/updates/result.json"
  echo "failed: $1"
}
i=0
while kill -0 "$WAITPID" 2>/dev/null && [ $i -lt 120 ]; do sleep 0.5; i=$((i+1)); done
TMP=$(mktemp -d "$(dirname "$DIR")/.rootv-update.XXXXXX") || { fail "mktemp"; "$EXE" & exit 1; }
if ! tar -xzf "$ARCHIVE" -C "$TMP"; then rm -rf "$TMP"; fail "解不开安装包"; "$EXE" & exit 1; fi
NEW=$(find "$TMP" -mindepth 1 -maxdepth 1 -type d | head -n 1)
OLD="$DIR.old"
rm -rf "$OLD"
if [ -z "$NEW" ] || ! mv "$DIR" "$OLD"; then rm -rf "$TMP"; fail "换不了程序"; "$EXE" & exit 1; fi
if ! mv "$NEW" "$DIR"; then mv "$OLD" "$DIR"; rm -rf "$TMP"; fail "换不了程序"; "$EXE" & exit 1; fi
rm -rf "$TMP"
STARTED=$(date +%s)
"$EXE" &
i=0
while [ $i -lt "${ROOTV_UPDATE_WAIT:-120}" ]; do
  if [ -f "$DATA/run/control.json" ] && [ "$(stat -c %Y "$DATA/run/control.json")" -ge "$STARTED" ]; then
    rm -rf "$OLD"; echo "done"; exit 0
  fi
  sleep 0.5; i=$((i+1))
done
pkill -f "$DIR/" 2>/dev/null
sleep 3
rm -rf "$DIR"; mv "$OLD" "$DIR"
fail "new version did not start, rolled back"
"$EXE" &
exit 1
"""

# Windows scripts keep to ASCII (cmd and PowerShell 5 read files in the system code page).
WINDOWS_INSTALLER_SCRIPT = r"""@echo off
rem RooTV update (desktop/update.py): run the installer silently, then start the program.
set "INSTALLER=%~1"
set "EXE=%~2"
set "DATA=%~3"
set "FROM=%~4"
set "TO=%~5"
set "WAITPID=%~6"
set /a n=0
:wait
tasklist /FI "PID eq %WAITPID%" 2>nul | find "%WAITPID%" >nul
if not errorlevel 1 if %n% lss 60 (set /a n+=1 & timeout /t 1 /nobreak >nul & goto wait)
"%INSTALLER%" /VERYSILENT /SUPPRESSMSGBOXES /NORESTART /CLOSEAPPLICATIONS
if errorlevel 1 (
  > "%DATA%\updates\result.json" echo {"ok": false, "from": "%FROM%", "to": "%TO%", "error": "installer failed"}
)
start "" "%EXE%"
"""

WINDOWS_PORTABLE_SCRIPT = r"""param([string]$Archive, [string]$Dir, [string]$Data, [string]$From, [string]$To, [int]$WaitPid, [string]$Exe)
# RooTV update (desktop/update.py): swap the program folder from the zip, roll back if it does not come up.
$log = Join-Path $Data "updates\install.log"
function Fail($why) {
  $result = '{"ok": false, "from": "' + $From + '", "to": "' + $To + '", "error": "' + $why + '"}'
  Set-Content -Path (Join-Path $Data "updates\result.json") -Value $result -Encoding ASCII
  Add-Content -Path $log -Value "failed: $why"
}
Add-Content -Path $log -Value "update $From -> $To at $(Get-Date)"
for ($i = 0; $i -lt 60 -and (Get-Process -Id $WaitPid -ErrorAction SilentlyContinue); $i++) { Start-Sleep -Seconds 1 }
$tmp = Join-Path (Split-Path $Dir -Parent) (".rootv-update-" + [guid]::NewGuid().ToString("N"))
try { Expand-Archive -Path $Archive -DestinationPath $tmp -Force } catch { Fail "cannot unpack"; Start-Process $Exe; exit 1 }
$new = Get-ChildItem -Path $tmp -Directory | Select-Object -First 1
$old = "$Dir.old"
if (Test-Path $old) { Remove-Item $old -Recurse -Force }
try { Rename-Item $Dir $old -ErrorAction Stop } catch { Remove-Item $tmp -Recurse -Force; Fail "cannot swap"; Start-Process $Exe; exit 1 }
try { Move-Item $new.FullName $Dir -ErrorAction Stop } catch { Rename-Item $old $Dir; Fail "cannot swap"; Start-Process $Exe; exit 1 }
Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
$started = Get-Date
Start-Process $Exe
$control = Join-Path $Data "run\control.json"
for ($i = 0; $i -lt 120; $i++) {
  if ((Test-Path $control) -and ((Get-Item $control).LastWriteTime -ge $started)) { Remove-Item $old -Recurse -Force; exit 0 }
  Start-Sleep -Milliseconds 500
}
Get-Process | Where-Object { $_.Path -and $_.Path.StartsWith($Dir) } | Stop-Process -Force
Start-Sleep -Seconds 3
Remove-Item $Dir -Recurse -Force
Rename-Item $old $Dir
Fail "new version did not start, rolled back"
Start-Process $Exe
exit 1
"""


def plan(ready: dict, data: Path, executable: Path, system: str, current: str, pid: int) -> Plan:
    """What to run for this platform. Raises UpdateFailed when it cannot be installed here."""
    data = Path(data)
    folder = data / "updates"
    file = Path(str(ready.get("path") or ""))
    to = str(ready.get("version") or "")
    args = [str(data), current, to, str(pid)]
    if system == "Darwin":
        app = app_bundle(executable)
        if app is None:
            raise UpdateFailed("不是从 RooTV.app 运行的，没法自动更新")
        if not writable(app.parent):
            raise UpdateFailed(f"没有权限改 {app.parent}，请手动安装下好的新版本")
        script = folder / "install-macos.sh"
        return Plan(script, ["/bin/sh", str(script), str(file), str(app)] + args, MAC_SCRIPT)
    if system == "Windows":
        if installed_windows(executable):
            script = folder / "install-windows.cmd"
            return Plan(script, ["cmd", "/c", str(script), str(file), str(executable)] + args,
                        WINDOWS_INSTALLER_SCRIPT)
        program = executable.resolve().parent
        if not writable(program.parent):
            raise UpdateFailed(f"没有权限改 {program.parent}，请手动解压下好的新版本")
        script = folder / "install-windows.ps1"
        return Plan(script, ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script),
                             "-Archive", str(file), "-Dir", str(program), "-Data", str(data), "-From", current,
                             "-To", to, "-WaitPid", str(pid), "-Exe", str(executable)], WINDOWS_PORTABLE_SCRIPT)
    program = executable.resolve().parent
    if not writable(program.parent):
        raise UpdateFailed(f"没有权限改 {program.parent}，新版本在 {file}")
    script = folder / "install-linux.sh"
    return Plan(script, ["/bin/sh", str(script), str(file), str(program)] + args + [str(executable)],
                POSIX_ARCHIVE_SCRIPT)


def launch(chosen: Plan, popen=subprocess.Popen) -> None:
    """Write the script and start it detached: it must outlive the tray that starts it."""
    chosen.script.parent.mkdir(parents=True, exist_ok=True)
    newline = "\r\n" if chosen.script.suffix in (".cmd", ".ps1") else "\n"
    with open(chosen.script, "w", encoding="utf-8", newline=newline) as out:
        out.write(chosen.text)
    if chosen.script.suffix == ".sh":
        chosen.script.chmod(0o700)
    options = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
               "close_fds": True}
    if sys.platform == "win32":
        options["creationflags"] = 0x00000008 | 0x00000200    # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    else:
        options["start_new_session"] = True
    popen(chosen.command, **options)
