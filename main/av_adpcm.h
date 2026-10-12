// IMA ADPCM for the FAV1 audio packet, both directions: no ESP-IDF dependencies.
#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

// An audio block is a four-byte state header followed by the samples, four bits
// each, two to a byte with the earlier sample in the LOW nibble:
//
//   [s16 predictor, big-endian][u8 step index][u8 zero][nibbles...]
//
// The header is the decoder's state *before the first sample of the block*, not
// the first sample itself, so a block holds exactly the samples it is sized for
// and every block can be decoded without the one before it. Blocks that follow
// one another from a continuous encoder carry the state that decoding the
// previous block would have reached, so the two ways of decoding agree; the
// header is what lets a block stand alone.
#define AV_ADPCM_HEADER_BYTES 4u
#define AV_ADPCM_INDEX_MAX 88u

// False when the header cannot have come from an encoder: a step index past the
// table, or a reserved byte that is not zero. The receiver ends the session on
// false, the same rule every other malformed packet gets.
bool av_adpcm_header_valid(const uint8_t block[AV_ADPCM_HEADER_BYTES]);

// Decode `samples` samples from `block` into `out`. The block must be
// AV_ADPCM_HEADER_BYTES + (samples+1)/2 bytes long and its header valid; the
// caller owns both checks. An out-of-range step index is clamped rather than
// trusted, so a header that skipped the check cannot index outside the table.
void av_adpcm_decode(const uint8_t *block, size_t samples, int16_t *out);

// The encoder's state between blocks: the predictor and step index the next
// block starts from. A stream that is encoded block after block keeps one of
// these, so consecutive blocks join up exactly as the server's encoder
// (server/adpcm.py, Encoder) makes them; zero is the state a stream starts in.
typedef struct {
    int16_t predictor;
    uint8_t index;
} av_adpcm_state_t;

// Encode `samples` samples (an even number) into one block of
// AV_ADPCM_HEADER_BYTES + samples/2 bytes, header first, and advance `state`.
// Bit for bit the same as server/adpcm.py's Encoder.encode, which is what lets
// the microphone's blocks (AV_VOICE) go through the server's decoder and the
// device's own decoder alike.
void av_adpcm_encode(av_adpcm_state_t *state, const int16_t *pcm, size_t samples, uint8_t *block);
