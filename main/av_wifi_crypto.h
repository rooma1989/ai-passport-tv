// End-to-end encryption of the Wi-Fi management messages, the device half
// (docs/superpowers/specs/2026-10-13-wifi-setup-design.md, section 4).
//
// The relay seals each message (add / remove / raise / switch a network) for
// one device; the media server and the plain TCP link in between only carry the
// ciphertext. Both ends derive the same key without it ever crossing the wire:
//
//   shared = X25519(own private, peer public)
//   key    = HKDF-SHA256(salt = device public || relay public,
//                        ikm  = shared, info = "ai-passport-tv wifi v1")   (32 bytes)
//   AES-256-GCM, 12-byte random nonce, AAD "avwifi1", 16-byte tag.
//
// The device's key pair is made once and kept in NVS (av_store.h); its public
// half goes out in HELLO ("wkey") and the server pins it on first sight. The
// relay's public key arrives in CONFIG ("wifi_key") and the device pins it the
// same way. Not derived from device_secret: that travels in clear in every
// HELLO, so anyone reading the link could derive such a key too.
//
// Built on mbedTLS, which the firmware links anyway (hardware AES on both
// chips). Everything here allocates only for the duration of a call: the
// contexts live on the caller's stack or in short-lived heap blocks, and
// nothing is kept between messages. The server half is server/wifi_crypto.py;
// tests/test_av_wifi_crypto.c and tests/test_wifi_crypto.py open the same
// envelope.
#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define AV_WIFI_KEY_BYTES 32u
#define AV_WIFI_KEY_B64_LEN 43u      // 32 bytes as base64url without padding
#define AV_WIFI_NONCE_BYTES 12u
#define AV_WIFI_TAG_BYTES 16u
// Longest plaintext accepted: an SSID (32 bytes, escaped in JSON up to 6x for
// control characters, in practice a few dozen), a password (64) and the
// envelope fields. Matches server/wifi_crypto.py PLAIN_MAX.
#define AV_WIFI_PLAIN_MAX 320u
// Longest "c" field: base64url of PLAIN_MAX + tag.
#define AV_WIFI_SEALED_B64_MAX (((AV_WIFI_PLAIN_MAX + AV_WIFI_TAG_BYTES) * 4u + 2u) / 3u)

// The random source X25519 blinding uses: esp_fill_random() on the device, a
// test generator on the host. Same signature as mbedTLS's f_rng.
int av_wifi_crypto_rng(void *ctx, unsigned char *out, size_t length);

// The public key for a private key (any 32 bytes; clamped per RFC 7748).
bool av_wifi_crypto_public(const uint8_t private_key[AV_WIFI_KEY_BYTES],
                           uint8_t public_key[AV_WIFI_KEY_BYTES]);

// The 32-byte message key, as both sides derive it. False on a weak peer key
// (an all-zero shared secret) or a library failure. Exposed for the tests.
bool av_wifi_crypto_key(const uint8_t own_private[AV_WIFI_KEY_BYTES],
                        const uint8_t peer_public[AV_WIFI_KEY_BYTES],
                        const uint8_t device_public[AV_WIFI_KEY_BYTES],
                        const uint8_t server_public[AV_WIFI_KEY_BYTES],
                        uint8_t key[AV_WIFI_KEY_BYTES]);

// Open one envelope ({"v":1,"n":nonce,"c":ciphertext||tag}, both base64url) on
// the device. Writes the plaintext and a terminating NUL into `out`
// (AV_WIFI_PLAIN_MAX + 1 bytes is always enough) and returns its length, or -1
// when the envelope is malformed, too long, or does not authenticate.
int av_wifi_crypto_open(const uint8_t device_private[AV_WIFI_KEY_BYTES],
                        const uint8_t server_public[AV_WIFI_KEY_BYTES],
                        const char *nonce_b64, const char *sealed_b64,
                        uint8_t *out, size_t out_size);

// base64url without padding. encode returns the length written (out gets a
// NUL), or 0 if out is too small; decode returns the byte count or -1.
size_t av_b64url_encode(const uint8_t *in, size_t length, char *out, size_t out_size);
int av_b64url_decode(const char *in, uint8_t *out, size_t out_size);
