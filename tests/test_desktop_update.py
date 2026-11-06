"""The tray's side of updates (desktop/update.py, the menu rows, status parsing).

The swap scripts themselves are checked for shape here (and sh -n for the POSIX
ones); the macOS one is run end to end by tests/test_desktop_update_mac.py on a
Mac. The Windows scripts have not been run on Windows.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from desktop import menu, update
from desktop.control import Status


class FileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def ready(self, data=b"new version"):
        path = self.root / "data" / "updates" / "0.4.0-beta" / "RooTV.dmg"
        path.parent.mkdir(parents=True)
        path.write_bytes(data)
        return {"version": "0.4.0-beta", "path": str(path), "size": len(data),
                "sha256": hashlib.sha256(data).hexdigest()}

    def test_check_file(self):
        ready = self.ready()
        self.assertEqual(update.check_file(ready), Path(ready["path"]))
        Path(ready["path"]).write_bytes(b"new versioN")
        with self.assertRaises(update.UpdateFailed):
            update.check_file(ready)
        with self.assertRaises(update.UpdateFailed):
            update.check_file(dict(ready, path=str(self.root / "missing")))

    def test_mac_plan(self):
        ready = self.ready()
        exe = self.root / "Apps" / "RooTV.app" / "Contents" / "MacOS" / "RooTV"
        exe.parent.mkdir(parents=True)
        exe.write_text("")
        chosen = update.plan(ready, self.root / "data", exe, "Darwin", "0.3.8-beta", 4242)
        self.assertEqual(chosen.script, self.root / "data" / "updates" / "install-macos.sh")
        self.assertEqual(chosen.command[:4], ["/bin/sh", str(chosen.script), ready["path"],
                                              str((self.root / "Apps" / "RooTV.app").resolve())])
        self.assertEqual(chosen.command[4:], [str(self.root / "data"), "0.3.8-beta", "0.4.0-beta", "4242"])
        with self.assertRaises(update.UpdateFailed):
            update.plan(ready, self.root / "data", self.root / "python3", "Darwin", "0.3.8-beta", 1)
        apps = self.root / "Apps"
        apps.chmod(stat.S_IRUSR | stat.S_IXUSR)
        try:
            if not os.access(apps, os.W_OK):       # root ignores the mode
                with self.assertRaises(update.UpdateFailed):
                    update.plan(ready, self.root / "data", exe, "Darwin", "0.3.8-beta", 1)
        finally:
            apps.chmod(stat.S_IRWXU)

    def test_windows_and_linux_plans(self):
        ready = self.ready()
        program = self.root / "Program" / "RooTV"
        program.mkdir(parents=True)
        exe = program / "RooTV.exe"
        exe.write_text("")
        portable = update.plan(ready, self.root / "data", exe, "Windows", "0.3.8-beta", 7)
        self.assertEqual(portable.script.name, "install-windows.ps1")
        self.assertIn("-ExecutionPolicy", portable.command)
        self.assertEqual(portable.command[portable.command.index("-Dir") + 1], str(program.resolve()))
        (program / "unins000.exe").write_text("")
        installed = update.plan(ready, self.root / "data", exe, "Windows", "0.3.8-beta", 7)
        self.assertEqual(installed.script.name, "install-windows.cmd")
        self.assertEqual(installed.command[:5], ["cmd", "/c", str(installed.script), ready["path"], str(exe)])
        linux = update.plan(ready, self.root / "data", exe, "Linux", "0.3.8-beta", 7)
        self.assertEqual(linux.script.name, "install-linux.sh")
        self.assertEqual(linux.command[-1], str(exe))

    def test_scripts(self):
        for text in (update.WINDOWS_INSTALLER_SCRIPT, update.WINDOWS_PORTABLE_SCRIPT):
            self.assertTrue(text.isascii(), "cmd and PowerShell 5 read the system code page")
        self.assertIn("/VERYSILENT", update.WINDOWS_INSTALLER_SCRIPT)
        if shutil.which("sh"):
            for text in (update.MAC_SCRIPT, update.POSIX_ARCHIVE_SCRIPT):
                checked = subprocess.run(["sh", "-n"], input=text, capture_output=True, text=True, encoding="utf-8")
                self.assertEqual(checked.returncode, 0, checked.stderr)

    def test_launch_writes_and_detaches(self):
        started = []
        chosen = update.Plan(self.root / "updates" / "install-linux.sh", ["/bin/sh", "x"], "echo hi\n")
        update.launch(chosen, popen=lambda command, **options: started.append((command, options)))
        self.assertEqual(chosen.script.read_text(), "echo hi\n")
        self.assertEqual(stat.S_IMODE(chosen.script.stat().st_mode), 0o700)
        command, options = started[0]
        self.assertEqual(command, ["/bin/sh", "x"])
        if sys.platform != "win32":
            self.assertTrue(options["start_new_session"])
        windows = update.Plan(self.root / "updates" / "install-windows.cmd", ["cmd"], "@echo off\nrem x\n")
        update.launch(windows, popen=lambda *a, **k: None)
        self.assertEqual(windows.script.read_bytes(), b"@echo off\r\nrem x\r\n")


class MenuTests(unittest.TestCase):
    def rows(self, **up):
        status = Status.from_json({"version": "0.3.8-beta", "update": up})
        return [(i.text, i.action, i.enabled) for i in menu._update(status)]

    def test_rows(self):
        self.assertEqual(self.rows(state="off"), [])
        self.assertEqual(self.rows(), [], "an older core without updates")
        self.assertEqual(self.rows(state="latest"), [("检查更新", menu.CHECK_UPDATE, True)])
        self.assertEqual(self.rows(state="checking"), [("正在检查更新…", menu.CHECK_UPDATE, False)])
        self.assertEqual(self.rows(state="downloading", version="0.4.0", done=25, total=100),
                         [("正在下载新版本 0.4.0（25%）", None, False)])
        self.assertEqual(self.rows(state="ready", version="0.4.0"),
                         [("更新到 0.4.0 并重启…", menu.INSTALL_UPDATE, True)])
        rows = self.rows(state="latest", result="更新到 0.4.0 没成功")
        self.assertEqual(rows[0], ("更新到 0.4.0 没成功", None, False))

    def test_status(self):
        up = Status.from_json({"update": {"state": "ready", "version": "0.4.0", "install": True}}).update
        self.assertEqual((up.state, up.version, up.install), ("ready", "0.4.0", True))
        self.assertEqual(Status.from_json({}).update.state, "off")

    def test_menu_has_the_row(self):
        status = Status.from_json({"version": "0.3.8-beta", "state": "running",
                                   "update": {"state": "ready", "version": "0.4.0"}})
        items = menu.build(menu.TrayState(core="running", status=status))
        self.assertIn(menu.INSTALL_UPDATE, [i.action for i in items])


class TrayInstallTests(unittest.TestCase):
    """App.install_update with the control API and the launcher replaced."""

    def setUp(self):
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import test_desktop_tray as tt
        from desktop import power
        from desktop.control import ControlClient
        from desktop.tray import App
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name)
        self.core = tt.FakeCore()
        self.app = App(self.data, ControlClient(self.data / "run" / "control.json", timeout=1), self.core,
                       power.KeepAwake(tt.FakeBackend(), clock=tt.Clock()), opener=lambda *_: None,
                       path_opener=lambda *_: None, revealer=lambda *_: None, logs_dir=self.data / "logs",
                       browser_system=tt.browsers.System(platform="test", run=None, read_plist=None, app_dirs=()))
        payload = b"new"
        file = self.data / "updates" / "0.4.0" / "RooTV.tar.gz"
        file.parent.mkdir(parents=True)
        file.write_bytes(payload)
        self.ready = {"ok": True, "version": "0.4.0", "path": str(file), "size": 3,
                      "sha256": hashlib.sha256(payload).hexdigest()}
        self.posts, self.launched, self.quit = [], [], []

        def request(method, path, body=None, timeout=None):
            if path == "/update/ready":
                return dict(self.ready)
            self.posts.append((method, path, body))
            return {"ok": True}
        self.app.client.request = request
        self.app.on_quit = lambda: self.quit.append(True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_launches_then_quits(self):
        exe = self.data / "program" / "rootv"
        exe.parent.mkdir()
        exe.write_text("")
        with unittest.mock.patch.object(update, "launch", lambda chosen: self.launched.append(chosen)), \
                unittest.mock.patch("desktop.paths.system", lambda: "Linux"), \
                unittest.mock.patch.object(sys, "executable", str(exe)):
            self.app.install_update(confirm=False)
        self.assertEqual(self.launched[0].script.name, "install-linux.sh")
        self.assertTrue(self.app.quitting.is_set())
        self.assertIn("stop", self.core.calls)
        self.assertEqual(self.quit, [True])

    def test_a_bad_file_does_not_quit(self):
        Path(self.ready["path"]).write_bytes(b"bad")
        with unittest.mock.patch.object(update, "launch", lambda chosen: self.launched.append(chosen)):
            self.app.install_update(confirm=False)
        self.assertEqual(self.launched, [])
        self.assertFalse(self.app.quitting.is_set())
        self.assertEqual(self.posts[-1][1], "/update/done")
        self.assertIn("校验", self.posts[-1][2]["error"])


if __name__ == "__main__":
    unittest.main()
