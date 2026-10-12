"""The relay's QR encoder (server/relay/qr.py) against the firmware's C library.

Both are Project Nayuki's algorithm; the C copy in components/qrcodegen is the
one the device draws its codes with. The test compiles a tiny driver around it
with the host compiler and compares every module of every symbol, so a wrong
table entry, mask or penalty in the Python port shows up as a mismatch. Without
a C compiler the comparison is skipped and only the structural checks run.
"""

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from server.relay import qr

SAMPLES = [
    "HTTPS://EXAMPLE.COM/TV/L/ABCDEFGHIJKLMNOPQRSTUVWXYZ",          # alphanumeric: the console's sign-in code
    "HTTPS://EXAMPLE.COM/TV/R/ABCDEFGHIJKLMNOPQRST",                 # a device's guest link
    "https://example.com/tv/i/ABCDEFGHIJKLMNOPQRST",                 # bytes: an invite link
    "0123456789012345",                                              # numeric
    "小智电视",                                                        # UTF-8 bytes
    "x" * 200,                                                       # version 7+: version information blocks
    "A",
]

DRIVER = r"""
#include <stdio.h>
#include "qrcodegen.h"
int main(int argc, char **argv) {
    static uint8_t qr[qrcodegen_BUFFER_LEN_MAX], tmp[qrcodegen_BUFFER_LEN_MAX];
    for (int a = 1; a < argc; a++) {
        if (!qrcodegen_encodeText(argv[a], tmp, qr, qrcodegen_Ecc_MEDIUM, qrcodegen_VERSION_MIN,
                                  qrcodegen_VERSION_MAX, qrcodegen_Mask_AUTO, true)) return 1;
        int size = qrcodegen_getSize(qr);
        for (int y = 0; y < size; y++) {
            for (int x = 0; x < size; x++) putchar(qrcodegen_getModule(qr, x, y) ? '#' : '.');
            putchar('\n');
        }
        putchar('\n');
    }
    return 0;
}
"""


def rows(code):
    return ["".join("#" if cell else "." for cell in row) for row in code.modules]


class QrTests(unittest.TestCase):
    def test_structure(self):
        code = qr.QrCode(SAMPLES[0])
        self.assertEqual(code.size, code.version * 4 + 17)
        # Three finder patterns: dark 7x7 ring corners, light separator.
        for x0, y0 in ((0, 0), (code.size - 7, 0), (0, code.size - 7)):
            self.assertTrue(all(code.modules[y0][x0 + i] for i in range(7)))
            self.assertTrue(code.modules[y0 + 3][x0 + 3])
            self.assertFalse(code.modules[y0 + 1][x0 + 1])
        self.assertEqual(qr.QrCode("x" * 200).version >= 7, True)

    def test_too_long(self):
        with self.assertRaises(qr.DataTooLong):
            qr.QrCode("x" * 3000)

    def test_svg_is_self_contained(self):
        image = qr.svg("HTTPS://EXAMPLE.COM/TV/L/ABC")
        self.assertTrue(image.startswith("<svg xmlns=\"http://www.w3.org/2000/svg\""))
        self.assertNotIn("href", image)
        side = qr.QrCode("HTTPS://EXAMPLE.COM/TV/L/ABC").size + 8
        self.assertIn(f'viewBox="0 0 {side} {side}"', image)

    @unittest.skipUnless(shutil.which("cc"), "no C compiler")
    def test_same_modules_as_the_firmware_library(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "driver.c"
            source.write_text(DRIVER, encoding="utf-8")
            binary = Path(tmp) / "driver"
            subprocess.run(["cc", "-std=c99", "-O1", f"-I{ROOT / 'components/qrcodegen'}", str(source),
                            str(ROOT / "components/qrcodegen/qrcodegen.c"), "-o", str(binary)],
                           check=True, capture_output=True)
            output = subprocess.run([str(binary), *SAMPLES], check=True, capture_output=True, encoding="utf-8",
                                    text=True).stdout
        symbols = [block.split("\n") for block in output.strip("\n").split("\n\n")]
        self.assertEqual(len(symbols), len(SAMPLES))
        for text, expected in zip(SAMPLES, symbols):
            with self.subTest(text=text[:30]):
                self.assertEqual(rows(qr.QrCode(text)), expected)


if __name__ == "__main__":
    unittest.main()
