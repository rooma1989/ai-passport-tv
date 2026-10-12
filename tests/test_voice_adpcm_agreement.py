"""The microphone's ADPCM blocks (AV_VOICE) decode the same on the server.

tests/test_av_adpcm.c encodes a fixed integer signal with the firmware's
av_adpcm_encode() and pins the bytes it produced. This encodes the same signal
with server/adpcm.py's Encoder and checks the same figures, so the two encoders
are bit for bit the same and a block the device sends is a block the server's
decoder was written for. Change the signal in both files together.
"""

import unittest

from server.adpcm import Encoder, decode


def signal(n: int) -> list[int]:
    out, seed = [], 12345
    for i in range(n):
        seed = (seed * 1103515245 + 12345) & 0x7FFFFFFF
        noise = ((seed >> 16) & 0x7FF) - 1024
        tri = (i * 97) % 4000
        tri = tri if tri < 2000 else 4000 - tri
        burst = 20000 if (i // 160) % 4 == 1 else 0
        value = (tri - 1000) * 12 + noise + (burst if i % 2 else -burst)
        out.append(max(-32768, min(32767, value)))
    return out


def fnv1a(data: bytes) -> int:
    h = 0x811C9DC5
    for byte in data:
        h = ((h ^ byte) * 0x01000193) & 0xFFFFFFFF
    return h


class VoiceAdpcmAgreementTests(unittest.TestCase):
    def test_same_bytes_as_the_firmware_encoder(self):
        pcm = signal(1280)
        encoder = Encoder()
        first, second = encoder.encode(pcm[:640]), encoder.encode(pcm[640:])
        # The figures tests/test_av_adpcm.c asserts for av_adpcm_encode().
        self.assertEqual(len(first), 324)
        self.assertEqual(first[:8], bytes.fromhex("00000000ffffff9f"))
        self.assertEqual(second[:8], bytes.fromhex("319e3d00bab9e1a8"))
        self.assertEqual(second[-4:], bytes.fromhex("bba0bc1b"))
        self.assertEqual(fnv1a(first), 0xF085219A)
        self.assertEqual(fnv1a(second), 0x5FD8E440)
        self.assertEqual((encoder.predictor, encoder.index), (-11259, 54))
        # Each block stands alone: the header carries the state.
        self.assertEqual(len(decode(second, 640)), 640)


if __name__ == "__main__":
    unittest.main()
