// Sound-wave Wi-Fi provisioning: the decoder.
//
// A phone page (server/relay/assets/wifi.html, served at /tv/wifi) plays a
// short sequence of audible two-tone chirps that carries the Wi-Fi name and
// password; the device hears it in setup mode and connects. This is the
// receiving half of that modem. The design, the numbers and the measured
// success rates are in
// docs/superpowers/specs/2026-10-13-sound-provisioning-design.md.
//
// On the air (all frequencies at the device's 16 kHz):
//   - 32 tones, 1250 Hz + 125 Hz * k (k = 0..31, so 1250..5125 Hz). Every
//     symbol is 48 ms and sounds two tones at once: one of 16 in the low band
//     (high nibble) and one of 16 in the high band (low nibble), so one symbol
//     is one byte.
//   - 6 preamble symbols with a fixed tone pattern, then a 5-symbol header
//     (payload length, Reed-Solomon protected), then the payload codeword:
//     L payload bytes + 2*ceil(L/4) (at least 8) Reed-Solomon parity bytes.
//     Every byte after the preamble is XORed with a fixed sequence so that
//     repeated characters do not repeat tones.
//   - Payload, version 1: 0x01, ssid_len, ssid, pass_len, pass, server_len,
//     server, CRC-32 (IEEE, little endian) of everything before it.
//   The phone repeats the message; the decoder adds up what it heard of each
//   repeat and tries the sum as well as each repeat on its own.
//
// Pure C, integers only (the ESP32-C3 has no FPU), no ESP-IDF dependency:
// tests/test_av_soundprov.c runs it on a host against a simulated speaker,
// room and microphone.
//
// Memory: av_soundprov_create() makes one heap allocation of
// sizeof(struct av_soundprov), about 14 KB (AV_SOUNDPROV_STATE_BYTES is the
// upper bound the host test checks), and nothing else; no large stack frames.
// CPU: about 6 million simple integer operations per second of audio, a few
// per cent of one 160 MHz core (numbers in the design note).
#pragma once
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

// The only sample rate the decoder accepts.
#define AV_SOUNDPROV_SAMPLE_RATE 16000
// Upper bound for the decoder's single allocation (checked by the host test).
#define AV_SOUNDPROV_STATE_BYTES (16 * 1024)

typedef struct av_soundprov av_soundprov_t;

// Allocates its own state (one malloc, see above). NULL when out of memory.
av_soundprov_t *av_soundprov_create(void);
void av_soundprov_destroy(av_soundprov_t *d);
// Forget everything heard so far, including a decoded message and earlier
// repeats kept for combining.
void av_soundprov_reset(av_soundprov_t *d);

// Feed 16 kHz mono signed 16-bit PCM in any chunk size. Returns 1 once a
// complete, checksummed message has been decoded (then call
// av_soundprov_result), else 0. Once it has returned 1 it keeps returning 1 and
// ignores further audio until av_soundprov_reset().
int av_soundprov_feed(av_soundprov_t *d, const int16_t *pcm, size_t n);

// 0..100: how much of a message has been received (for a progress hint on
// screen); 0 when idle. 100 once a message has been decoded.
int av_soundprov_progress(const av_soundprov_t *d);

// The decoded payload, parsed. Strings are NUL-terminated; empty when absent.
// The SSID and password are raw bytes as typed on the phone (UTF-8), never
// containing a NUL.
typedef struct {
    char ssid[33];
    char password[65];
    char server[64];
} av_soundprov_result_t;

// 1 and *out filled if a message has been decoded, else 0 and *out zeroed.
int av_soundprov_result(const av_soundprov_t *d, av_soundprov_result_t *out);

#ifdef __cplusplus
}
#endif
