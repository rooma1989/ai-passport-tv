"""tools/soundprov_dump.py: the console dump of the setup screen's microphone back into WAV files."""

import base64
import struct
import subprocess
import sys
import tempfile
import unittest
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def line(samples):
    raw = struct.pack(f"<{len(samples)}h", *samples)
    return "I (1234) av_soundsetup: SNDDUMP " + base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


class DumpTests(unittest.TestCase):
    def test_blocks_become_wavs(self):
        log = "\n".join([
            "noise before",
            "SNDDUMP begin rate=16000",
            line([0, 1, -1, 32767]),
            line([-32768, 5]),
            "SNDDUMP end",
            "SNDDUMP begin rate=16000",
            line([7] * 320),
            # cut off before "end"
        ])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "console.log"
            path.write_text(log, encoding="utf-8")
            out = Path(tmp) / "cap"
            result = subprocess.run([sys.executable, str(ROOT / "tools/soundprov_dump.py"), str(path), "-o", str(out)],
                                    capture_output=True, text=True, encoding="utf-8", check=True)
            self.assertIn("cap-1.wav", result.stdout)
            with wave.open(str(out) + "-1.wav") as wav:
                self.assertEqual((wav.getnchannels(), wav.getsampwidth(), wav.getframerate()), (1, 2, 16000))
                self.assertEqual(struct.unpack("<6h", wav.readframes(6)), (0, 1, -1, 32767, -32768, 5))
            with wave.open(str(out) + "-2.wav") as wav:
                self.assertEqual(wav.getnframes(), 320)

    def test_no_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "console.log"
            path.write_text("nothing here\n", encoding="utf-8")
            result = subprocess.run([sys.executable, str(ROOT / "tools/soundprov_dump.py"), str(path)],
                                    capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(result.returncode, 1)


if __name__ == "__main__":
    unittest.main()
