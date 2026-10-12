// Device identity in HELLO: which device this is, and the secret that proves it.
//
// The multi-tenant server (docs/superpowers/specs/2026-10-12-multi-tenant-design.md,
// section 4.2) binds a device to an account by its id and authenticates it by a
// per-device secret, so a public build can use the cloud host without a
// compiled-in pairing token. Four flat keys go into HELLO next to the existing
// ones: "device_id", "model", "device_secret" and "fw". A server that does not
// know them ignores them, so this firmware still works against an older server.
//
// The id is the chip's factory base MAC from eFuse: stable across reflashes and
// NVS erases, and the same under the Muse firmware and the player on the AI
// Passport. It is an identifier, not a secret. The secret is 32 random bytes
// generated once, after Wi-Fi has started, and kept in NVS (av_store.h).
//
// Only text formatting lives here, with no ESP-IDF dependency, so the shapes the
// server checks are covered by a host test (tests/test_av_identity.c).
#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

// Board selection reaches this header through sdkconfig on the device; host
// tests have no sdkconfig and get the Passport, like av_protocol.h.
#if __has_include("sdkconfig.h")
#include "sdkconfig.h"
#endif

// 12 lower-case hex digits of the factory MAC, no separators.
#define AV_DEVICE_ID_LEN 12u
// 32 bytes of randomness, base64url without padding: 43 characters.
#define AV_DEVICE_SECRET_BYTES 32u
#define AV_DEVICE_SECRET_LEN 43u

// The server picks geometry, frame rate and byte rate by model, so these two
// strings are a contract with server/tenancy (and the model profiles of phase 4).
#if defined(CONFIG_AV_BOARD_XINGZHI_CUBE_154)
#define AV_DEVICE_MODEL "xingzhi-cube-154"
#else
#define AV_DEVICE_MODEL "ai-passport"
#endif

// mac[6] -> "a1b2c3d4e5f6". out must hold AV_DEVICE_ID_LEN + 1 bytes.
void av_identity_format_id(const uint8_t mac[6], char out[AV_DEVICE_ID_LEN + 1]);

// raw[AV_DEVICE_SECRET_BYTES] -> base64url without padding, NUL-terminated.
// out must hold AV_DEVICE_SECRET_LEN + 1 bytes.
void av_identity_encode_secret(const uint8_t raw[AV_DEVICE_SECRET_BYTES],
                               char out[AV_DEVICE_SECRET_LEN + 1]);

// True when text is exactly AV_DEVICE_SECRET_LEN base64url characters: what a
// stored secret must look like before it is sent. A record written by anything
// else is regenerated rather than sent, because the server would refuse it.
bool av_identity_secret_valid(const char *text);

// Printable ASCII only, cut to max_len: the build's version string goes to the
// server's admin page and must not carry anything a log line cannot hold.
void av_identity_clean_fw(const char *in, char *out, size_t max_len);
