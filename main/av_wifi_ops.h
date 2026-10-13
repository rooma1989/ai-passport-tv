// Managing the saved Wi-Fi networks from the phone remote and the PC console:
// the decisions, kept apart from NVS and the Wi-Fi stack so they can be tested
// on a host (tests/test_av_wifi_ops.c). The firmware glue is av_wifimgr.c.
//
// A message (decrypted by av_wifi_crypto.c) asks for one of four things:
//   add     a network and its password (front of the list, like the setup page)
//   remove  the network at index i
//   up      move the network at index i one place up
//   switch  move the network at index i to the front and rejoin now
// Indexes refer to the list this device last reported, and every message
// names the network too: if the two no longer agree -- the list changed on the
// device's setup page in the meantime -- nothing is done ("changed").
#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define AV_WIFI_SAVED_MAX 10u      // SsidManager::kMaxCount
#define AV_WIFI_SSID_MAX 32u
#define AV_WIFI_PASSWORD_MAX 64u

typedef enum {
    AV_WIFI_OP_NONE = 0,
    AV_WIFI_OP_ADD,
    AV_WIFI_OP_REMOVE,
    AV_WIFI_OP_UP,
    AV_WIFI_OP_SWITCH,
} av_wifi_op_kind_t;

typedef struct {
    uint64_t seq;                               // strictly increasing per relay
    av_wifi_op_kind_t kind;
    int index;                                  // remove / up / switch
    char ssid[AV_WIFI_SSID_MAX + 1];            // add: the network; else its reported name
    char password[AV_WIFI_PASSWORD_MAX + 1];    // add only
} av_wifi_op_t;

typedef enum {
    AV_WIFI_OK = 0,
    AV_WIFI_E_BAD,        // malformed message or out-of-range value
    AV_WIFI_E_REPLAY,     // seq not newer than the last one applied
    AV_WIFI_E_FULL,       // add, and AV_WIFI_SAVED_MAX networks are saved
    AV_WIFI_E_CHANGED,    // index and name disagree with the list
    AV_WIFI_E_KEY,        // the message did not decrypt (keys differ)
    AV_WIFI_E_STORE,      // NVS refused the change
    AV_WIFI_E_BUSY,       // another message is still being applied
    AV_WIFI_E_COUNT
} av_wifi_err_t;

// The short code sent back to the server ("ok" for AV_WIFI_OK); the phone
// page turns it into a sentence.
const char *av_wifi_err_code(av_wifi_err_t err);

// The op name in a message ("add", ...) to a kind; AV_WIFI_OP_NONE if unknown.
av_wifi_op_kind_t av_wifi_op_kind(const char *name);

typedef struct {
    av_wifi_op_kind_t action;   // AV_WIFI_OP_NONE: valid, nothing to change
    int index;                  // which entry for remove / up / switch
} av_wifi_plan_t;

// Check `op` against the saved list (`names`, raw SSIDs, `count` of them) and
// the last applied seq. On AV_WIFI_OK, `plan` says what to do.
av_wifi_err_t av_wifi_plan(const av_wifi_op_t *op, uint64_t last_seq,
                           const char *const *names, unsigned count, av_wifi_plan_t *plan);

// An SSID as shown on the phone: valid UTF-8 copied, every byte that is not
// (and every control character) replaced by '?'. SSIDs are 32 arbitrary bytes,
// and one that is not text would make the whole HELLO unreadable as JSON.
void av_wifi_display_name(const char *raw, char *out, size_t size);

// `text` as a JSON string, quotes included, into `out`. Returns the length, or
// 0 when it does not fit. `text` must already be valid UTF-8 (display names are).
size_t av_wifi_json_string(const char *text, char *out, size_t size);

// The ack the device sends in AV_STATUS after applying a message:
// {"wifi":{"ack":seq,"ok":0|1,"err":"code","saved":[names],"cur":"name"}}
// `names` are raw SSIDs (turned into display names here), `current` the one in
// use or "". Returns the length, or 0 when `out` is too small.
size_t av_wifi_ack_json(char *out, size_t size, uint64_t seq, av_wifi_err_t err,
                        const char *const *names, unsigned count, const char *current);
