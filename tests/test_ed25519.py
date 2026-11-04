"""Ed25519 (server/ed25519.py) against RFC 8032 section 7.1 test vectors, plus tampering."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from server import ed25519

VECTORS = [
    ("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60",
     "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a", "",
     "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"),
    ("4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb",
     "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c", "72",
     "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00"),
]


class Ed25519Tests(unittest.TestCase):
    def test_rfc8032_vectors(self):
        for secret, public, message, signature in VECTORS:
            sk, msg = bytes.fromhex(secret), bytes.fromhex(message)
            self.assertEqual(ed25519.public_key(sk).hex(), public)
            self.assertEqual(ed25519.sign(sk, msg).hex(), signature)
            self.assertTrue(ed25519.verify(bytes.fromhex(public), msg, bytes.fromhex(signature)))

    def test_tampering_fails(self):
        secret, public, _message, signature = VECTORS[1]
        pk, sig = bytes.fromhex(public), bytes.fromhex(signature)
        self.assertFalse(ed25519.verify(pk, b"\x73", sig), "other message")
        self.assertFalse(ed25519.verify(pk, b"\x72", sig[:-1] + bytes([sig[-1] ^ 1])), "other signature")
        self.assertFalse(ed25519.verify(bytes.fromhex(VECTORS[0][1]), b"\x72", sig), "other key")
        self.assertFalse(ed25519.verify(pk, b"\x72", b"short"), "wrong length")


if __name__ == "__main__":
    unittest.main()
