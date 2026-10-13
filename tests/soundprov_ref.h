// A C mirror of the phone page's sound-provisioning encoder
// (server/relay/assets/wifi.html, between "BEGIN SOUNDPROV ENCODER" and "END
// SOUNDPROV ENCODER"), for the host test only. The two are pinned to the same
// golden symbol hashes: tests/test_av_soundprov.c checks this one and
// tests/test_soundprov_page.py runs the page's JavaScript under node.
#pragma once
#include <stddef.h>
#include <stdint.h>

#define SP_REF_MAX_SYMBOLS (6 + 5 + 255)
#define SP_REF_SYMBOL_S 0.048     // one symbol, seconds
#define SP_REF_LEAD_S 0.15        // silence before the first repeat
#define SP_REF_GAP_S 0.3          // silence after every repeat

// Tone pairs for one repeat (preamble, header, payload codeword). tones_a in
// 0..15 (1250 + 125 a Hz), tones_b in 0..15 (3250 + 125 b Hz). bytes (may be
// NULL) receives the transmitted byte of each symbol after the preamble.
// Returns the number of symbols, or -1 when a field is too long (32, 64, 63
// bytes) or the SSID is empty.
int sp_ref_symbols(const char *ssid, const char *pass, const char *server,
                   uint8_t *tones_a, uint8_t *tones_b, uint8_t *bytes);

// Samples of one repeat at sample rate sr, including the gap after it.
size_t sp_ref_repeat_len(int nsym, double sr);
// Renders one repeat (symbols then SP_REF_GAP_S of silence) into out, which
// must hold sp_ref_repeat_len() samples. Full scale is 1.0.
void sp_ref_render(const uint8_t *tones_a, const uint8_t *tones_b, int nsym, double sr,
                   float *out);
