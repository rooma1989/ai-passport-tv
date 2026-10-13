// Saved Wi-Fi networks managed from the phone remote and the PC console, the
// device half (docs/superpowers/specs/2026-10-13-wifi-setup-design.md).
//
//   HELLO   "wifimgr":1, "wkey":<this device's public key>, "wifi_saved":[names]
//   CONFIG  "wifi_key":<the relay's public key>            (pinned, av_store.h)
//   SWITCH  {"wifi":{"v":1,"n":nonce,"c":ciphertext}}      (av_wifi_crypto.h)
//   STATUS  {"wifi":{"ack":seq,"ok":..,"err":..,"saved":[..],"cur":..}}
//
// Who calls what:
//   - the session owner calls av_wifimgr_init() once Wi-Fi is up (it makes the
//     key pair the first time, on a short-lived task with room for the maths);
//   - the receive task, which owns the socket, adds the HELLO fields, hands over
//     CONFIG's key and SWITCH's envelope, and sends the ack between packets;
//   - a worker task, created per message and gone when it is done, decrypts,
//     checks (av_wifi_ops.h), changes the list and writes the ack.
// Nothing is held between messages but this file's statics (under 1 KB, most
// of it the ack waiting to be sent).
#pragma once
#include <stdbool.h>
#include <stddef.h>

#include "cJSON.h"

// Load the key pair, or make and store one. Idempotent; false if there is no
// key (the HELLO then leaves the feature out and the server never sends one).
bool av_wifimgr_init(void);

// Add "wifimgr", "wkey" and "wifi_saved" to a HELLO under construction.
// Returns false only when cJSON ran out of memory.
bool av_wifimgr_hello(cJSON *hello);

// CONFIG carried the relay's key. `server` is the address this session dialled
// ("host:port"), which the pin is kept with.
void av_wifimgr_config(const char *key_b64, const char *server);

// A SWITCH carried {"wifi": envelope}. Copies what it needs and returns at once.
void av_wifimgr_post(const cJSON *envelope);

// The ack waiting to be sent, if any: copies it into `out` (NUL-terminated) and
// returns its length, 0 when there is none. After a successful send, call
// av_wifimgr_ack_sent().
size_t av_wifimgr_ack(char *out, size_t size);
void av_wifimgr_ack_sent(void);
#define AV_WIFIMGR_ACK_MAX 900u

// Setup stored a network or a server: the next relay key is trusted afresh.
void av_wifimgr_forget_server_key(void);
