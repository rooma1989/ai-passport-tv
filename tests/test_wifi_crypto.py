"""Wi-Fi 管理消息的加密（server/wifi_crypto.py）：公开测试向量，以及和固件共用的一条整消息向量。

ENVELOPE 也写在 tests/test_av_wifi_crypto.c 里：设备一侧（mbedTLS）解开它得到同一段明文，
两边任何一处改了算法、标签或拼接顺序，至少一边的测试会失败。
"""

import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server import wifi_crypto as w  # noqa: E402

h = bytes.fromhex

# RFC 7748 第 6.1 节的两把私钥，这里当作设备和中转的。
DEVICE_PRIVATE = h("77076d0a7318a57d3c16c17251b26645df4c2f87ebc0992ab177fba51db92c2a")
SERVER_PRIVATE = h("5dab087e624a8a4b79e17f8b83800ee66f3bb1292618b6fd1c2f8b27ff88e0eb")
DEVICE_PUBLIC_B64 = "hSDwCYkwp1R0i33ctD73Wg2_Og0mOBr066SpjqqbTmo"
SERVER_PUBLIC_B64 = "3p7bfXt9wbTTW2HC7OQ1Nz-DQ8hbeGdNrfx-FG-IK08"
MESSAGE_KEY = h("e199edc6ebecdf6b5e4220633c4f4177c1fd7e4ff951ded7f59903670ddcdcfb")
PLAINTEXT = ('{"seq":1760000000000,"op":"add","ssid":"家里的WiFi","password":"p@ss word 中文"}').encode()
ENVELOPE = {"v": 1, "n": "AAECAwQFBgcICQoL",
            "c": "Xa5ImP6K9whZIov1TEK7yBuVwus5hf5hb2BTiXrXgdXmik4N3P8Uh3o_divwEQ6NaNwx8OquEeSUEtaYIX8ODRp4nPUudYmDHXk"
                 "OA1SLlTNiuoKyqcO40Hmvz4DlelTguMriPqw"}


class Vectors(unittest.TestCase):
    def test_x25519_rfc7748(self):
        self.assertEqual(w.x25519(h("a546e36bf0527c9d3b16154b82465edd62144c0ac1fc5a18506a2244ba449ac4"),
                                  h("e6db6867583030db3594c1a424b15f7c726624ec26b3353b10a903a6d0ab1c4c")).hex(),
                         "c3da55379de9c6908e94ea4df28d084f32eccf03491c71f754b4075577a28552")
        alice, bob = w.public_key(DEVICE_PRIVATE), w.public_key(SERVER_PRIVATE)
        self.assertEqual(alice.hex(), "8520f0098930a754748b7ddcb43ef75a0dbf3a0d26381af4eba4a98eaa9b4e6a")
        self.assertEqual(bob.hex(), "de9edb7d7b7dc1b4d35b61c2ece435373f8343c85b78674dadfc7e146f882b4f")
        shared = "4a5d9d5ba4ce2de1728e3bf480350f25e07e21c947d19e3376f09b3c1e161742"
        self.assertEqual(w.x25519(DEVICE_PRIVATE, bob).hex(), shared)
        self.assertEqual(w.x25519(SERVER_PRIVATE, alice).hex(), shared)

    def test_hkdf_rfc5869_case1(self):
        self.assertEqual(w.hkdf_sha256(bytes(range(13)), b"\x0b" * 22, bytes(range(0xF0, 0xFA)), 42).hex(),
                         "3cb25f25faacd57a90434f64d0362f2a2d2d0a90cf1a5a4c5db02d56ecc4c5bf34007208d5b887185865")

    def test_gcm_spec_cases(self):
        # McGrew & Viega, "The Galois/Counter Mode of Operation", test cases 14 and 16 (AES-256).
        self.assertEqual(w.gcm_encrypt(bytes(32), bytes(12), bytes(16), b"").hex(),
                         "cea7403d4d606b6e074ec5d3baf39d18d0d1c8a799996bf0265b98b5d48ab919")
        key = h("feffe9928665731c6d6a8f9467308308feffe9928665731c6d6a8f9467308308")
        plain = h("d9313225f88406e5a55909c5aff5269a86a7a9531534f7da2e4c303d8a318a72"
                  "1c3c0c95956809532fcf0e2449a6b525b16aedf5aa0de657ba637b39")
        aad = h("feedfacedeadbeeffeedfacedeadbeefabaddad2")
        iv = h("cafebabefacedbaddecaf888")
        sealed = w.gcm_encrypt(key, iv, plain, aad)
        self.assertEqual(sealed[:-16].hex(),
                         "522dc1f099567d07f47f37a32a84427d643a8cdcbfe5c0c97598a2bd2555d1aa"
                         "8cb08e48590dbb3da7b08b1056828838c5f61e6393ba7a0abcc9f662")
        self.assertEqual(sealed[-16:].hex(), "76fc6ece0f4e1768cddf8853bb2d551b")
        self.assertEqual(w.gcm_decrypt(key, iv, sealed, aad), plain)

    def test_gcm_rejects_tampering(self):
        key, iv = os.urandom(32), os.urandom(12)
        sealed = bytearray(w.gcm_encrypt(key, iv, b"secret", b"aad"))
        for index in (0, len(sealed) - 1):
            bad = bytearray(sealed)
            bad[index] ^= 1
            with self.assertRaises(w.CryptoError):
                w.gcm_decrypt(key, iv, bytes(bad), b"aad")
        with self.assertRaises(w.CryptoError):
            w.gcm_decrypt(key, iv, bytes(sealed), b"other")


class Envelope(unittest.TestCase):
    def test_shared_vector(self):
        """The same envelope the firmware test opens."""
        self.assertEqual(w.b64e(w.public_key(DEVICE_PRIVATE)), DEVICE_PUBLIC_B64)
        self.assertEqual(w.b64e(w.public_key(SERVER_PRIVATE)), SERVER_PUBLIC_B64)
        device_public, server_public = w.public_key(DEVICE_PRIVATE), w.public_key(SERVER_PRIVATE)
        self.assertEqual(w.message_key(SERVER_PRIVATE, device_public, device_public, server_public), MESSAGE_KEY)
        self.assertEqual(w.message_key(DEVICE_PRIVATE, server_public, device_public, server_public), MESSAGE_KEY)
        built = w.plaintext(1760000000000, "add", ssid="家里的WiFi", password="p@ss word 中文")
        self.assertEqual(built, PLAINTEXT)
        self.assertEqual(w.seal(SERVER_PRIVATE, device_public, PLAINTEXT, nonce=bytes(range(12))), ENVELOPE)
        self.assertEqual(w.open_sealed(DEVICE_PRIVATE, server_public, ENVELOPE), PLAINTEXT)

    def test_round_trip_random(self):
        device, server = w.new_private_key(), w.new_private_key()
        envelope = w.seal(server, w.public_key(device), b'{"seq":2,"op":"remove","i":0,"ssid":"x"}')
        self.assertEqual(w.open_sealed(device, w.public_key(server), envelope),
                         b'{"seq":2,"op":"remove","i":0,"ssid":"x"}')
        # Another device's key cannot open it.
        with self.assertRaises(w.CryptoError):
            w.open_sealed(w.new_private_key(), w.public_key(server), envelope)
        # Nor can a different server key.
        with self.assertRaises(w.CryptoError):
            w.open_sealed(device, w.public_key(w.new_private_key()), envelope)

    def test_password_never_in_envelope(self):
        envelope = w.seal(SERVER_PRIVATE, w.public_key(DEVICE_PRIVATE),
                          w.plaintext(5, "add", ssid="n", password="hunter2hunter2"))
        self.assertNotIn("hunter2", repr(envelope))

    def test_weak_point_refused(self):
        with self.assertRaises(w.CryptoError):
            w.seal(SERVER_PRIVATE, bytes(32), b"{}")

    def test_limits(self):
        with self.assertRaises(w.CryptoError):
            w.plaintext(1, "add", ssid="x" * 32, password="y" * (w.PLAIN_MAX))
        for bad in ("", "a", "a=b", "!!!!", None):
            with self.assertRaises(w.CryptoError):
                w.b64d(bad)
        with self.assertRaises(w.CryptoError):
            w.open_sealed(DEVICE_PRIVATE, w.public_key(SERVER_PRIVATE), {"v": 2, "n": "AA", "c": "AA"})


class KeyFile(unittest.TestCase):
    def test_created_once_private(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "wifi-key"
            first = w.load_or_create_key(path)
            self.assertEqual(len(first), 32)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(w.load_or_create_key(path), first)
            path.write_bytes(b"short")
            with self.assertRaises(w.CryptoError):
                w.load_or_create_key(path)


if __name__ == "__main__":
    unittest.main()
