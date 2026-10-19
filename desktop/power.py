"""Keeping the computer from idle-sleeping while a device is watching (design 5.10, Q8).

Only *idle system* sleep is prevented, never the display: the screen still turns
off, and a sleep the user chooses (menu, lid, power button) still happens.

| OS      | Mechanism                                                                  |
| ------- | -------------------------------------------------------------------------- |
| Windows | SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED) from one thread |
| macOS   | IOPMAssertion "PreventUserIdleSystemSleep" (IOKit via ctypes); `caffeinate -i -w <pid>` as fallback |
| Linux   | a `systemd-inhibit --what=idle:sleep` child, if systemd-inhibit exists     |

`KeepAwake` is the policy: held while any device watches or a phone listens,
released two minutes after the last one leaves; `keep_awake = "always"` holds it
while the client runs, `"never"` never does. The design puts the assertion in
the supervisor; until the core reports that it manages power itself
(`status.power.managed_by == "core"`), the tray holds it, driven by the same
status. The backends here are plain stdlib so the core can import them.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import queue
import shutil
import subprocess
import sys
import threading
import time

RELEASE_DELAY_S = 120.0
# ASCII: `pmset -g assertions` shows a non-ASCII name as "".
REASON = "Xiaozhi TV: a device is watching"

# Windows
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002      # deliberately never used

# macOS
K_CF_STRING_ENCODING_UTF8 = 0x08000100
K_IOPM_ASSERTION_LEVEL_ON = 255
PREVENT_IDLE_SYSTEM_SLEEP = "PreventUserIdleSystemSleep"


class NullBackend:
    name = "none"

    def acquire(self) -> bool:
        return False

    def release(self) -> None:
        pass

    def close(self) -> None:
        pass


class WindowsBackend:
    """The execution state belongs to the thread that set it, and is cleared
    when that thread ends -- so one long-lived thread does every call."""

    name = "SetThreadExecutionState"

    def __init__(self, kernel32=None):
        if kernel32 is None:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)   # type: ignore[attr-defined]
            kernel32.SetThreadExecutionState.argtypes = [ctypes.c_uint32]
            kernel32.SetThreadExecutionState.restype = ctypes.c_uint32
        self._kernel32 = kernel32
        self._requests: queue.Queue = queue.Queue()
        self._thread = threading.Thread(target=self._serve, daemon=True, name="keep-awake")
        self._thread.start()

    def _serve(self) -> None:
        while True:
            flags, reply = self._requests.get()
            if flags is None:
                self._kernel32.SetThreadExecutionState(ES_CONTINUOUS)
                reply.put(True)
                return
            reply.put(bool(self._kernel32.SetThreadExecutionState(flags)))

    def _call(self, flags) -> bool:
        reply: queue.Queue = queue.Queue()
        self._requests.put((flags, reply))
        try:
            return reply.get(timeout=5)
        except queue.Empty:
            return False

    def acquire(self) -> bool:
        return self._call(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)

    def release(self) -> None:
        self._call(ES_CONTINUOUS)

    def close(self) -> None:
        if self._thread.is_alive():
            self._call(None)


class MacBackend:
    """IOPMAssertionCreateWithName / IOPMAssertionRelease through ctypes."""

    name = "IOPMAssertion"

    def __init__(self, iokit=None, corefoundation=None):
        if iokit is None:
            iokit = ctypes.CDLL(ctypes.util.find_library("IOKit"))
            corefoundation = ctypes.CDLL(ctypes.util.find_library("CoreFoundation"))
            corefoundation.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
            corefoundation.CFStringCreateWithCString.restype = ctypes.c_void_p
            corefoundation.CFRelease.argtypes = [ctypes.c_void_p]
            corefoundation.CFRelease.restype = None
            iokit.IOPMAssertionCreateWithName.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p,
                                                          ctypes.POINTER(ctypes.c_uint32)]
            iokit.IOPMAssertionCreateWithName.restype = ctypes.c_int
            iokit.IOPMAssertionRelease.argtypes = [ctypes.c_uint32]
            iokit.IOPMAssertionRelease.restype = ctypes.c_int
        self._iokit = iokit
        self._cf = corefoundation
        self._id: int | None = None

    def _cfstring(self, text: str):
        return self._cf.CFStringCreateWithCString(None, text.encode("utf-8"), K_CF_STRING_ENCODING_UTF8)

    def acquire(self) -> bool:
        if self._id is not None:
            return True
        kind, reason = self._cfstring(PREVENT_IDLE_SYSTEM_SLEEP), self._cfstring(REASON)
        try:
            assertion = ctypes.c_uint32(0)
            result = self._iokit.IOPMAssertionCreateWithName(kind, K_IOPM_ASSERTION_LEVEL_ON, reason,
                                                             ctypes.byref(assertion))
        finally:
            for ref in (kind, reason):
                if ref:
                    self._cf.CFRelease(ref)
        if result != 0:
            return False
        self._id = assertion.value
        return True

    def release(self) -> None:
        if self._id is not None:
            self._iokit.IOPMAssertionRelease(self._id)
            self._id = None

    def close(self) -> None:
        self.release()


class ChildBackend:
    """A helper program that holds the inhibition while it runs:
    `caffeinate -i -w <pid>` on macOS, `systemd-inhibit ... sleep infinity` on Linux."""

    def __init__(self, argv: list[str], name: str, popen=subprocess.Popen):
        self.argv, self.name, self._popen = argv, name, popen
        self._process = None

    def acquire(self) -> bool:
        if self._process is not None and self._process.poll() is None:
            return True
        try:
            self._process = self._popen(self.argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL)
        except OSError:
            self._process = None
            return False
        return True

    def release(self) -> None:
        if self._process is not None:
            if self._process.poll() is None:
                self._process.terminate()
                try:
                    self._process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self._process.kill()
            self._process = None

    def close(self) -> None:
        self.release()


def default_backend(platform_name: str | None = None, which=shutil.which, pid: int | None = None):
    platform_name = platform_name or sys.platform
    pid = os.getpid() if pid is None else pid
    try:
        if platform_name == "win32":
            return WindowsBackend()
        if platform_name == "darwin":
            try:
                return MacBackend()
            except (OSError, AttributeError, TypeError):
                if which("caffeinate"):
                    # -i: idle sleep only (no -d: the display may sleep); -w: end with us.
                    return ChildBackend(["caffeinate", "-i", "-w", str(pid)], "caffeinate")
        elif which("systemd-inhibit"):
            return ChildBackend(["systemd-inhibit", "--what=idle:sleep", "--who=Xiaozhi TV",
                                 f"--why={REASON}", "--mode=block", "sleep", "infinity"], "systemd-inhibit")
    except (OSError, AttributeError):
        pass
    return NullBackend()


class KeepAwake:
    """When to hold the assertion. `update()` is called with every status poll."""

    MODES = ("watching", "always", "never")

    def __init__(self, backend=None, clock=time.monotonic, release_delay: float = RELEASE_DELAY_S):
        self.backend = backend if backend is not None else NullBackend()
        self._clock = clock
        self.release_delay = release_delay
        self.held = False
        self._last_active: float | None = None

    def update(self, mode: str, active: bool) -> bool:
        """mode: "watching" | "always" | "never"; active: a device watching or a phone listening."""
        now = self._clock()
        if mode not in self.MODES:
            mode = "watching"
        if active:
            self._last_active = now
        if mode == "always":
            want = True
        elif mode == "never":
            want = False
        else:
            want = active or (self._last_active is not None and now - self._last_active < self.release_delay)
        if want and not self.held:
            self.held = bool(self.backend.acquire())
        elif not want and self.held:
            self.backend.release()
            self.held = False
        return self.held

    def close(self) -> None:
        if self.held:
            self.backend.release()
            self.held = False
        self.backend.close()
