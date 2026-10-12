"""The relay's ffmpeg command and its copy loop."""

import io
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server.relay import stream


class FakePipe:
    """Hands out four bytes per read, like a pipe fed slowly."""

    def __init__(self, data):
        self.data = io.BytesIO(data)

    def read1(self, _size):
        return self.data.read(4)


class FakeProc:
    def __init__(self, chunks, returncode):
        self.stdout = FakePipe(b"".join(chunks))
        self.final = returncode
        self.returncode = None
        self.killed = False

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        self.returncode = self.final
        return self.final


class FakeClock:
    def __init__(self, step):
        self.now, self.step = 0.0, step

    def __call__(self):
        self.now += self.step
        return self.now


class StreamTests(unittest.TestCase):
    def test_args(self):
        args = stream.ffmpeg_args("/opt/ff", "https://upos.example/v.mp4?a=1&b=2", 115.7)
        self.assertEqual(args[0], "/opt/ff")
        self.assertEqual(args[args.index("-i") + 1], "https://upos.example/v.mp4?a=1&b=2")
        self.assertEqual(args[args.index("-ss") + 1], "115")
        self.assertLess(args.index("-ss"), args.index("-i"))
        self.assertIn("Referer: https://www.bilibili.com/\r\n", args)
        self.assertEqual(args[-3:], ["-f", "mpegts", "pipe:1"])
        self.assertEqual(args[args.index("-c") + 1], "copy")
        # Reconnect options are input options: they must come before -i.
        self.assertLess(args.index("-reconnect"), args.index("-i"))
        self.assertEqual(args[args.index("-reconnect_on_network_error") + 1], "1")

    def run_pump(self, proc, write=None, step=3.0):
        out, ticks = bytearray(), []
        outcome = stream.pump(proc, write or out.extend, ticks.append, tick_s=10, clock=FakeClock(step))
        return outcome, bytes(out), ticks

    def test_natural_end(self):
        proc = FakeProc([b"abcdefgh", b"ijkl"], 0)
        outcome, out, ticks = self.run_pump(proc)
        self.assertEqual((outcome, out), ("ended", b"abcdefghijkl"))
        self.assertTrue(proc.killed)

    def test_ffmpeg_failure(self):
        self.assertEqual(self.run_pump(FakeProc([b"abcd"], 1))[0], "failed")

    def test_client_gone(self):
        def write(_data):
            raise BrokenPipeError()
        proc = FakeProc([b"abcdefgh"], 0)
        self.assertEqual(self.run_pump(proc, write)[0], "client_gone")
        self.assertTrue(proc.killed)

    def test_ticks_report_elapsed(self):
        proc = FakeProc([b"x" * 40], 0)
        _, _, ticks = self.run_pump(proc, step=4.0)
        self.assertTrue(all(b > a for a, b in zip(ticks, ticks[1:])))
        self.assertGreaterEqual(ticks[0], 10.0)


if __name__ == "__main__":
    unittest.main()
