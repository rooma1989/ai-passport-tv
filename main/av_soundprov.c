// Sound-wave Wi-Fi provisioning decoder. See av_soundprov.h for the format and
// docs/superpowers/specs/2026-10-13-sound-provisioning-design.md for why it is
// built this way. The phone's encoder is in server/relay/assets/wifi.html; a C
// mirror of it for the host test is tests/soundprov_ref.c. A change to anything
// on the air changes all three together.
//
// How it listens:
//   1. Every 64 samples (a "hop", 4 ms) it correlates the hop with the 32 tone
//      frequencies, keeping the phase of each tone continuous across hops, so
//      four consecutive hops add up to an exact 256-point DFT bin (a 16 ms
//      window, 62.5 Hz resolution; tones are 125 Hz apart).
//   2. For each window it keeps each tone's share of its band's energy (0..255)
//      in a ring of 128 windows. Shares rather than energies make it deaf to
//      volume, and noise outside the 32 tone frequencies counts for nothing.
//   3. The preamble is found by matching the six known tone pairs against the
//      ring at every hop. A 48 ms symbol is read from two windows in its middle
//      (2 hops of guard each side), so the match is flat for +-2 hops; the
//      start is the middle of that plateau, refined by comparing how much of a
//      tone is heard in windows straddling the symbol's two edges.
//   4. Symbols are then read one by one at the predicted time. After each one
//      the same edge comparison nudges the timing, which follows a phone and a
//      device whose clocks differ by well over 0.5 %.
//   5. Header (length, Reed-Solomon protected), then the payload codeword. On
//      the last symbol it tries Reed-Solomon decoding with no erasures and then
//      with growing numbers of the least certain symbols erased, on this
//      repeat alone and on the running average of all repeats of the same
//      length, and accepts only a CRC-32-correct, well-formed payload.
// Integers only; the arithmetic bounds are noted where they matter.
#include "av_soundprov.h"

#include <stdlib.h>
#include <string.h>

// ---- what is on the air (must match wifi.html and tests/soundprov_ref.c) ----

#define HOP 64          // samples per hop: 4 ms
#define WIN 4           // hops per analysis window: 16 ms, 62.5 Hz bins
#define SYM 12          // hops per symbol: 48 ms
#define OFF1 2          // the two windows read inside a symbol start here...
#define OFF2 6          // ...and here, leaving 2 hops of guard each side
#define NT 32           // tones; tone k is bin 20 + 2k of 62.5 Hz = 1250 + 125k Hz
#define NB 16           // tones per band (band A: k 0..15, high nibble; B: 16..31)
#define PRE 6           // preamble symbols
#define HDR 5           // header symbols: length byte + 4 Reed-Solomon parity
#define HDR_PARITY 4
#define MAX_CW 255      // longest payload codeword
#define MIN_L 9         // version, ssid_len, 1 ssid byte, pass_len, server_len, CRC
#define MAX_L (4 + 32 + 64 + 63 + 4)

static const uint8_t PRE_A[PRE] = {3, 12, 0, 9, 15, 6};
static const uint8_t PRE_B[PRE] = {10, 1, 13, 4, 7, 14};

// Parity bytes for a payload of L bytes: 2*ceil(L/4), at least 8. One wrong
// symbol in four can be corrected, one in two if the decoder knows which.
static int parity_for(int L)
{
    int e = 2 * ((L + 3) / 4);
    return e < 8 ? 8 : e;
}

// The byte sent as symbol j after the preamble is codeword byte j XOR this.
static uint8_t whiten(uint32_t j)
{
    return (uint8_t)(((j + 1u) * 0x9E3779B1u) >> 24);
}

// ---- receiver tuning ----

#define RING 128                    // windows kept (512 ms); power of two
#define PK_LEN 10                   // hops of preamble scores looked at before locking
#define PRE_MAX (PRE * 4 * 255)     // a perfect preamble score
#define THR_IDLE (PRE_MAX * 28 / 100)  // preamble score that starts a reception
#define THR_BUSY (PRE_MAX * 65 / 100)  // ...and one that cuts short a reception under way
#define MATCH_MIN 7                 // of the preamble's 12 tones, at least this many loudest
#define WEAK 140                    // a band's best tone below this (of 510) is "nothing"
#define WEAK_RUN 6                  // this many weak symbols in a row end a reception
#define TRACK_CONF 20               // symbols less certain than this (of 255) do not steer timing
#define TRACK_DIV 4                 // timing loop gain: a quarter of the error per symbol
#define SESSION_HOPS 15000          // repeats more than 60 s apart are not combined

// cos(2 pi i / 256) in Q14.
static const int16_t COS[256] = {
    16384, 16379, 16364, 16340, 16305, 16261, 16207, 16143, 16069, 15986, 15893, 15791,
    15679, 15557, 15426, 15286, 15137, 14978, 14811, 14635, 14449, 14256, 14053, 13842,
    13623, 13395, 13160, 12916, 12665, 12406, 12140, 11866, 11585, 11297, 11003, 10702,
    10394, 10080, 9760, 9434, 9102, 8765, 8423, 8076, 7723, 7366, 7005, 6639,
    6270, 5897, 5520, 5139, 4756, 4370, 3981, 3590, 3196, 2801, 2404, 2006,
    1606, 1205, 804, 402, 0, -402, -804, -1205, -1606, -2006, -2404, -2801,
    -3196, -3590, -3981, -4370, -4756, -5139, -5520, -5897, -6270, -6639, -7005, -7366,
    -7723, -8076, -8423, -8765, -9102, -9434, -9760, -10080, -10394, -10702, -11003, -11297,
    -11585, -11866, -12140, -12406, -12665, -12916, -13160, -13395, -13623, -13842, -14053, -14256,
    -14449, -14635, -14811, -14978, -15137, -15286, -15426, -15557, -15679, -15791, -15893, -15986,
    -16069, -16143, -16207, -16261, -16305, -16340, -16364, -16379, -16384, -16379, -16364, -16340,
    -16305, -16261, -16207, -16143, -16069, -15986, -15893, -15791, -15679, -15557, -15426, -15286,
    -15137, -14978, -14811, -14635, -14449, -14256, -14053, -13842, -13623, -13395, -13160, -12916,
    -12665, -12406, -12140, -11866, -11585, -11297, -11003, -10702, -10394, -10080, -9760, -9434,
    -9102, -8765, -8423, -8076, -7723, -7366, -7005, -6639, -6270, -5897, -5520, -5139,
    -4756, -4370, -3981, -3590, -3196, -2801, -2404, -2006, -1606, -1205, -804, -402,
    0, 402, 804, 1205, 1606, 2006, 2404, 2801, 3196, 3590, 3981, 4370,
    4756, 5139, 5520, 5897, 6270, 6639, 7005, 7366, 7723, 8076, 8423, 8765,
    9102, 9434, 9760, 10080, 10394, 10702, 11003, 11297, 11585, 11866, 12140, 12406,
    12665, 12916, 13160, 13395, 13623, 13842, 14053, 14256, 14449, 14635, 14811, 14978,
    15137, 15286, 15426, 15557, 15679, 15791, 15893, 15986, 16069, 16143, 16207, 16261,
    16305, 16340, 16364, 16379,
};

// ---- GF(256), primitive polynomial 0x11d, generator 2 ----

static const uint8_t GF_EXP[255] = {
    1, 2, 4, 8, 16, 32, 64, 128, 29, 58, 116, 232, 205, 135, 19, 38,
    76, 152, 45, 90, 180, 117, 234, 201, 143, 3, 6, 12, 24, 48, 96, 192,
    157, 39, 78, 156, 37, 74, 148, 53, 106, 212, 181, 119, 238, 193, 159, 35,
    70, 140, 5, 10, 20, 40, 80, 160, 93, 186, 105, 210, 185, 111, 222, 161,
    95, 190, 97, 194, 153, 47, 94, 188, 101, 202, 137, 15, 30, 60, 120, 240,
    253, 231, 211, 187, 107, 214, 177, 127, 254, 225, 223, 163, 91, 182, 113, 226,
    217, 175, 67, 134, 17, 34, 68, 136, 13, 26, 52, 104, 208, 189, 103, 206,
    129, 31, 62, 124, 248, 237, 199, 147, 59, 118, 236, 197, 151, 51, 102, 204,
    133, 23, 46, 92, 184, 109, 218, 169, 79, 158, 33, 66, 132, 21, 42, 84,
    168, 77, 154, 41, 82, 164, 85, 170, 73, 146, 57, 114, 228, 213, 183, 115,
    230, 209, 191, 99, 198, 145, 63, 126, 252, 229, 215, 179, 123, 246, 241, 255,
    227, 219, 171, 75, 150, 49, 98, 196, 149, 55, 110, 220, 165, 87, 174, 65,
    130, 25, 50, 100, 200, 141, 7, 14, 28, 56, 112, 224, 221, 167, 83, 166,
    81, 162, 89, 178, 121, 242, 249, 239, 195, 155, 43, 86, 172, 69, 138, 9,
    18, 36, 72, 144, 61, 122, 244, 245, 247, 243, 251, 235, 203, 139, 11, 22,
    44, 88, 176, 125, 250, 233, 207, 131, 27, 54, 108, 216, 173, 71, 142,
};
static const uint8_t GF_LOG[256] = {
    0, 0, 1, 25, 2, 50, 26, 198, 3, 223, 51, 238, 27, 104, 199, 75,
    4, 100, 224, 14, 52, 141, 239, 129, 28, 193, 105, 248, 200, 8, 76, 113,
    5, 138, 101, 47, 225, 36, 15, 33, 53, 147, 142, 218, 240, 18, 130, 69,
    29, 181, 194, 125, 106, 39, 249, 185, 201, 154, 9, 120, 77, 228, 114, 166,
    6, 191, 139, 98, 102, 221, 48, 253, 226, 152, 37, 179, 16, 145, 34, 136,
    54, 208, 148, 206, 143, 150, 219, 189, 241, 210, 19, 92, 131, 56, 70, 64,
    30, 66, 182, 163, 195, 72, 126, 110, 107, 58, 40, 84, 250, 133, 186, 61,
    202, 94, 155, 159, 10, 21, 121, 43, 78, 212, 229, 172, 115, 243, 167, 87,
    7, 112, 192, 247, 140, 128, 99, 13, 103, 74, 222, 237, 49, 197, 254, 24,
    227, 165, 153, 119, 38, 184, 180, 124, 17, 68, 146, 217, 35, 32, 137, 46,
    55, 63, 209, 91, 149, 188, 207, 205, 144, 135, 151, 178, 220, 252, 190, 97,
    242, 86, 211, 171, 20, 42, 93, 158, 132, 60, 57, 83, 71, 109, 65, 162,
    31, 45, 67, 216, 183, 123, 164, 118, 196, 23, 73, 236, 127, 12, 111, 246,
    108, 161, 59, 82, 41, 157, 85, 170, 251, 96, 134, 177, 187, 204, 62, 90,
    203, 89, 95, 176, 156, 169, 160, 81, 11, 245, 22, 235, 122, 117, 44, 215,
    79, 174, 213, 233, 230, 231, 173, 232, 116, 214, 244, 234, 168, 80, 88, 175,
};

static uint8_t gf_mul(uint8_t a, uint8_t b)
{
    if (a == 0 || b == 0) return 0;
    unsigned s = (unsigned)GF_LOG[a] + GF_LOG[b];
    return GF_EXP[s >= 255 ? s - 255 : s];
}

static uint8_t gf_pow2(int e)  // alpha^e for any integer e
{
    e %= 255;
    if (e < 0) e += 255;
    return GF_EXP[e];
}

static uint8_t gf_inv(uint8_t a)  // a != 0
{
    return GF_EXP[(255 - GF_LOG[a]) % 255];
}

#define MAX_PAR 86  // parity_for(MAX_L) is 84; the header uses 4

// Reed-Solomon errors-and-erasures decoding in place. cw[0] is the highest
// coefficient; the code's roots are alpha^0 .. alpha^(npar-1). eras lists
// erased positions (0..n-1). Returns the number of bytes changed, or -1.
static int rs_decode(uint8_t *cw, int n, int npar, const uint8_t *eras, int nf)
{
    uint8_t S[MAX_PAR], lam[MAX_PAR + 1], B[MAX_PAR + 1], T[MAX_PAR + 1], om[MAX_PAR];
    if (npar > MAX_PAR || n > MAX_CW || n <= npar || nf > npar) return -1;

    int nonzero = 0;
    for (int i = 0; i < npar; i++) {
        uint8_t x = gf_pow2(i), s = 0;
        for (int j = 0; j < n; j++) s = gf_mul(s, x) ^ cw[j];
        S[i] = s;
        nonzero |= s;
    }
    if (!nonzero) return 0;

    // Erasure locator: product of (1 + X x) with X = alpha^(n-1-pos).
    memset(lam, 0, sizeof(lam));
    lam[0] = 1;
    for (int k = 0; k < nf; k++) {
        uint8_t X = gf_pow2(n - 1 - eras[k]);
        for (int d = k + 1; d >= 1; d--) lam[d] ^= gf_mul(lam[d - 1], X);
    }
    memcpy(B, lam, sizeof(B));

    // Berlekamp-Massey, started from the erasure locator.
    int L = nf;
    for (int r = nf; r < npar; r++) {
        uint8_t delta = 0;
        for (int i = 0; i <= L && i <= r; i++) delta ^= gf_mul(lam[i], S[r - i]);
        memmove(B + 1, B, MAX_PAR);
        B[0] = 0;
        if (delta) {
            for (int i = 0; i <= MAX_PAR; i++) T[i] = lam[i] ^ gf_mul(delta, B[i]);
            if (2 * L <= r + nf) {
                uint8_t inv = gf_inv(delta);
                for (int i = 0; i <= MAX_PAR; i++) B[i] = gf_mul(lam[i], inv);
                L = r + 1 + nf - L;
            }
            memcpy(lam, T, sizeof(lam));
        }
    }
    int deg = MAX_PAR;
    while (deg > 0 && lam[deg] == 0) deg--;
    if (deg != L || 2 * (L - nf) + nf > npar) return -1;

    // Omega = S * lambda mod x^npar.
    for (int i = 0; i < npar; i++) {
        uint8_t v = 0;
        for (int k = 0; k <= i && k <= L; k++) v ^= gf_mul(lam[k], S[i - k]);
        om[i] = v;
    }

    // Chien search over the (shortened) codeword, then Forney.
    int found = 0;
    for (int j = 0; j < n; j++) {
        int e = n - 1 - j;                 // X = alpha^e
        uint8_t xinv = gf_pow2(-e), v = 0, p = 1;
        for (int i = 0; i <= L; i++) {
            v ^= gf_mul(lam[i], p);
            p = gf_mul(p, xinv);
        }
        if (v) continue;
        uint8_t num = 0, den = 0;
        p = 1;
        for (int i = 0; i < npar; i++) {
            num ^= gf_mul(om[i], p);
            p = gf_mul(p, xinv);
        }
        // lambda'(x): the odd coefficients, one power down.
        p = 1;
        uint8_t x2 = gf_mul(xinv, xinv);
        for (int i = 1; i <= L; i += 2) {
            den ^= gf_mul(lam[i], p);
            p = gf_mul(p, x2);
        }
        if (den == 0) return -1;
        cw[j] ^= gf_mul(gf_pow2(e), gf_mul(num, gf_inv(den)));
        found++;
    }
    return found == L ? found : -1;
}

static uint32_t crc32(const uint8_t *p, int n)
{
    uint32_t c = 0xFFFFFFFFu;
    for (int i = 0; i < n; i++) {
        c ^= p[i];
        for (int b = 0; b < 8; b++) c = (c >> 1) ^ (0xEDB88320u & (0u - (c & 1u)));
    }
    return ~c;
}

// ---- state ----

struct av_soundprov {
    // Front end.
    int16_t hopbuf[HOP];
    uint32_t fill;
    uint32_t hop;                    // hops completed
    int32_t sre[WIN][NT], sim[WIN][NT];
    uint8_t frac[RING][NT];          // tone shares, indexed by window start hop

    // Preamble search.
    uint8_t pk_active, pk_n;
    uint32_t pk_first;
    uint16_t pk_sc[PK_LEN];
    uint32_t search_from;

    // A reception under way.
    uint8_t rx;
    int64_t t_q8;                    // start of the next symbol, hops in Q8
    uint16_t j, total, L, E;
    uint8_t weak;
    uint8_t tone_a[2], tone_b[2];    // symbols j-2 and j-1
    uint8_t conf_prev;               // certainty of symbol j-1
    int64_t s_prev;                  // start hop of symbol j-1
    uint8_t hdr[HDR], hdr_conf[HDR];
    uint8_t sym[MAX_CW], conf[MAX_CW];

    // Repeats of the same message, averaged.
    uint8_t acc[MAX_CW][NT];
    uint8_t acc_n;
    uint16_t acc_L;
    uint32_t acc_hop;

    // Decoding scratch.
    uint8_t cw[MAX_CW], work[MAX_CW], order[MAX_CW];

    uint8_t done;
    av_soundprov_result_t res;
};

_Static_assert(sizeof(struct av_soundprov) <= AV_SOUNDPROV_STATE_BYTES, "decoder state too big");

av_soundprov_t *av_soundprov_create(void)
{
    av_soundprov_t *d = malloc(sizeof(*d));
    if (d) av_soundprov_reset(d);
    return d;
}

void av_soundprov_destroy(av_soundprov_t *d)
{
    free(d);
}

void av_soundprov_reset(av_soundprov_t *d)
{
    if (d) memset(d, 0, sizeof(*d));
}

// ---- payload ----

static int parse_payload(const uint8_t *p, int L, av_soundprov_result_t *out)
{
    if (L < MIN_L || L > MAX_L) return 0;
    uint32_t want = (uint32_t)p[L - 4] | ((uint32_t)p[L - 3] << 8) |
                    ((uint32_t)p[L - 2] << 16) | ((uint32_t)p[L - 1] << 24);
    if (crc32(p, L - 4) != want || p[0] != 1) return 0;
    const int limit[3] = {32, 64, 63};
    char *dest[3] = {out->ssid, out->password, out->server};
    int i = 1;
    for (int f = 0; f < 3; f++) {
        if (i >= L - 4) return 0;
        int n = p[i++];
        if (n > limit[f] || i + n > L - 4) return 0;
        if (memchr(p + i, 0, (size_t)n)) return 0;
        memcpy(dest[f], p + i, (size_t)n);
        dest[f][n] = '\0';
        i += n;
    }
    return i == L - 4 && out->ssid[0] != '\0';
}

// Positions sorted from least to most certain (counting sort on 0..255).
static void sort_by_conf(const uint8_t *conf, int n, uint8_t *order)
{
    uint16_t count[256];
    memset(count, 0, sizeof(count));
    for (int i = 0; i < n; i++) count[conf[i]]++;
    uint16_t start = 0;
    for (int c = 0; c < 256; c++) {
        uint16_t k = count[c];
        count[c] = start;
        start += k;
    }
    for (int i = 0; i < n; i++) order[count[conf[i]]++] = (uint8_t)i;
}

// Reed-Solomon with no erasures, then with a quarter, half, three quarters
// and all but two of the parity's worth of the least certain bytes erased.
// d->cw holds the received codeword; conf its certainties.
static int try_decode(av_soundprov_t *d, const uint8_t *conf, int n, int L, int E)
{
    sort_by_conf(conf, n, d->order);
    int tried = -1;
    for (int step = 0; step <= 4; step++) {
        int f = step == 4 ? E - 2 : E * step / 4;
        if (f == tried) continue;
        tried = f;
        memcpy(d->work, d->cw, (size_t)n);
        if (rs_decode(d->work, n, E, d->order, f) < 0) continue;
        av_soundprov_result_t r;
        memset(&r, 0, sizeof(r));
        if (parse_payload(d->work, L, &r)) {
            d->res = r;
            return 1;
        }
    }
    return 0;
}

static int header_length(av_soundprov_t *d)
{
    uint8_t order[HDR];
    sort_by_conf(d->hdr_conf, HDR, order);
    for (int f = 0; f <= 2; f++) {
        uint8_t w[HDR];
        memcpy(w, d->hdr, HDR);
        if (rs_decode(w, HDR, HDR_PARITY, order, f) < 0) continue;
        if (w[0] >= MIN_L && w[0] <= MAX_L) return w[0];
    }
    return 0;
}

// ---- receiver ----

// The best tone of a band in a symbol starting at window s, and how sure.
static void read_band(const av_soundprov_t *d, int64_t s, int band, uint16_t v[NB],
                      uint8_t *tone, uint16_t *best, uint16_t *margin)
{
    const uint8_t *w1 = d->frac[(s + OFF1) & (RING - 1)] + band * NB;
    const uint8_t *w2 = d->frac[(s + OFF2) & (RING - 1)] + band * NB;
    uint16_t b = 0, second = 0;
    uint8_t t = 0;
    for (int k = 0; k < NB; k++) {
        v[k] = (uint16_t)(w1[k] + w2[k]);
        if (v[k] > b) {
            second = b;
            b = v[k];
            t = (uint8_t)k;
        } else if (v[k] > second) {
            second = v[k];
        }
    }
    *tone = t;
    *best = b;
    *margin = (uint16_t)(b - second);
}

// Timing error of a symbol that started at window s, from the share of its tone
// in the windows straddling its two edges, in Q8 hops (positive: it really
// started later). Only bands whose neighbours used a different tone count.
static int edge_error_q8(const av_soundprov_t *d, int64_t s, const uint8_t cur[2],
                         const uint8_t prev[2], const uint8_t next[2])
{
    int sum = 0, n = 0;
    for (int b = 0; b < 2; b++) {
        if (cur[b] == prev[b] || cur[b] == next[b]) continue;
        int k = b * NB + cur[b];
        int early = d->frac[(s - 2) & (RING - 1)][k];
        int late = d->frac[(s + SYM - 2) & (RING - 1)][k];
        if (early + late == 0) continue;
        sum += (late - early) * 256 / (late + early);
        n++;
    }
    return n ? sum / n : 0;
}

static void end_reception(av_soundprov_t *d)
{
    // A repeat that got into its payload has been folded into the average.
    if (d->rx && d->j > HDR && d->acc_n < 255) d->acc_n++;
    d->rx = 0;
}

static void start_reception(av_soundprov_t *d, int64_t pre_q8)
{
    end_reception(d);
    // Refine the start with the edges of the preamble's inner symbols, whose
    // neighbours are all known and all different.
    int64_t s0 = (pre_q8 + 128) >> 8;
    int err = 0;
    for (int i = 1; i < PRE - 1; i++) {
        const uint8_t cur[2] = {PRE_A[i], PRE_B[i]};
        const uint8_t prev[2] = {PRE_A[i - 1], PRE_B[i - 1]};
        const uint8_t next[2] = {PRE_A[i + 1], PRE_B[i + 1]};
        err += edge_error_q8(d, s0 + (int64_t)SYM * i, cur, prev, next);
    }
    err /= PRE - 2;
    if (err > 512) err = 512;
    if (err < -512) err = -512;
    pre_q8 += err;

    d->rx = 1;
    d->j = 0;
    d->total = 0;
    d->weak = 0;
    d->tone_a[0] = PRE_A[PRE - 2];
    d->tone_b[0] = PRE_B[PRE - 2];
    d->tone_a[1] = PRE_A[PRE - 1];
    d->tone_b[1] = PRE_B[PRE - 1];
    d->conf_prev = 255;
    d->s_prev = (pre_q8 + 128) / 256 + (int64_t)SYM * (PRE - 1);
    d->t_q8 = pre_q8 + (int64_t)SYM * PRE * 256;
}

static void finish_message(av_soundprov_t *d)
{
    int n = d->L + d->E;
    // This repeat on its own.
    for (int i = 0; i < n; i++) d->cw[i] = d->sym[i];
    if (try_decode(d, d->conf, n, d->L, d->E)) {
        d->done = 1;
        return;
    }
    // All repeats of this length so far (this one is already in the average).
    if (d->acc_n >= 1) {
        uint8_t conf[MAX_CW];
        for (int i = 0; i < n; i++) {
            uint8_t byte = 0;
            conf[i] = 255;
            for (int b = 0; b < 2; b++) {
                const uint8_t *a = d->acc[i] + b * NB;
                uint8_t best = 0, second = 0, t = 0;
                for (int k = 0; k < NB; k++) {
                    if (a[k] > best) {
                        second = best;
                        best = a[k];
                        t = (uint8_t)k;
                    } else if (a[k] > second) {
                        second = a[k];
                    }
                }
                byte = (uint8_t)(byte | (b == 0 ? t << 4 : t));
                if (best - second < conf[i]) conf[i] = (uint8_t)(best - second);
            }
            d->cw[i] = byte ^ whiten((uint32_t)(HDR + i));
        }
        if (try_decode(d, conf, n, d->L, d->E)) d->done = 1;
    }
}

// One symbol, read at its predicted time.
static void read_symbol(av_soundprov_t *d)
{
    int64_t s = (d->t_q8 + 128) >> 8;
    uint16_t va[NB], vb[NB], best_a, best_b, m_a, m_b;
    uint8_t ta, tb;
    read_band(d, s, 0, va, &ta, &best_a, &m_a);
    read_band(d, s, 1, vb, &tb, &best_b, &m_b);
    uint16_t m = m_a < m_b ? m_a : m_b;
    uint8_t conf = (uint8_t)(m / 2);
    uint8_t byte = (uint8_t)(((ta << 4) | tb) ^ whiten(d->j));

    if (d->j < HDR) {
        d->hdr[d->j] = byte;
        d->hdr_conf[d->j] = conf;
    } else {
        int i = d->j - HDR;
        d->sym[i] = byte;
        d->conf[i] = conf;
        // Fold into the running average of this message's repeats; after
        // four repeats it becomes a moving average.
        unsigned n = d->acc_n < 3 ? d->acc_n : 3;
        for (int k = 0; k < NB; k++) {
            uint8_t *a = &d->acc[i][k], *b = &d->acc[i][NB + k];
            *a = (uint8_t)((*a * n + va[k] / 2 + n / 2) / (n + 1));
            *b = (uint8_t)((*b * n + vb[k] / 2 + n / 2) / (n + 1));
        }
    }

    // Steer the timing with the previous symbol, now that both its neighbours
    // are known.
    if (d->conf_prev >= TRACK_CONF) {
        const uint8_t cur[2] = {d->tone_a[1], d->tone_b[1]};
        const uint8_t prev[2] = {d->tone_a[0], d->tone_b[0]};
        const uint8_t next[2] = {ta, tb};
        d->t_q8 += edge_error_q8(d, d->s_prev, cur, prev, next) / TRACK_DIV;
    }
    d->tone_a[0] = d->tone_a[1];
    d->tone_b[0] = d->tone_b[1];
    d->tone_a[1] = ta;
    d->tone_b[1] = tb;
    d->conf_prev = conf;
    d->s_prev = s;
    d->t_q8 += SYM * 256;
    d->j++;

    d->weak = (best_a < WEAK && best_b < WEAK) ? (uint8_t)(d->weak + 1) : 0;
    if (d->weak >= WEAK_RUN) {
        end_reception(d);
        return;
    }

    if (d->j == HDR) {
        int L = header_length(d);
        if (L == 0 && d->acc_n > 0 && d->hop - d->acc_hop < SESSION_HOPS)
            L = d->acc_L;  // header lost: assume the message being repeated
        if (L == 0) {
            d->rx = 0;
            return;
        }
        if (L != d->acc_L || d->hop - d->acc_hop >= SESSION_HOPS) {
            d->acc_n = 0;   // a different message: start a new average
            d->acc_L = (uint16_t)L;
        }
        d->acc_hop = d->hop;
        d->L = (uint16_t)L;
        d->E = (uint16_t)parity_for(L);
        d->total = (uint16_t)(HDR + d->L + d->E);
    } else if (d->total && d->j == d->total) {
        d->acc_hop = d->hop;
        finish_message(d);
        end_reception(d);
    }
}

static void preamble_search(av_soundprov_t *d, uint32_t w)
{
    if (w < (uint32_t)(SYM * (PRE - 1) + OFF2)) return;
    uint32_t p = w - (SYM * (PRE - 1) + OFF2);
    if (p < d->search_from) return;
    unsigned sc = 0;
    for (int i = 0; i < PRE; i++) {
        const uint8_t *w1 = d->frac[(p + SYM * i + OFF1) & (RING - 1)];
        const uint8_t *w2 = d->frac[(p + SYM * i + OFF2) & (RING - 1)];
        sc += w1[PRE_A[i]] + w2[PRE_A[i]] + w1[NB + PRE_B[i]] + w2[NB + PRE_B[i]];
    }
    // Once a header has been read, only a clearly better preamble may cut the
    // reception short; before that, a reception may have started on noise.
    unsigned thr = (d->rx && d->total) ? THR_BUSY : THR_IDLE;
    if (!d->pk_active) {
        if (sc < thr) return;
        d->pk_active = 1;
        d->pk_first = p;
        d->pk_n = 0;
    }
    d->pk_sc[d->pk_n++] = (uint16_t)sc;
    if (d->pk_n < PK_LEN) return;

    // The middle of the plateau around the best score.
    d->pk_active = 0;
    int best = 0;
    for (int i = 1; i < PK_LEN; i++)
        if (d->pk_sc[i] > d->pk_sc[best]) best = i;
    unsigned floor = d->pk_sc[best] - d->pk_sc[best] / 12;
    int lo = best, hi = best;
    while (lo > 0 && d->pk_sc[lo - 1] >= floor) lo--;
    while (hi < PK_LEN - 1 && d->pk_sc[hi + 1] >= floor) hi++;
    if (d->pk_sc[best] < thr) return;
    int64_t center_q8 = (int64_t)d->pk_first * 256 + (lo + hi) * 128;
    // The sum alone can be reached by speech or by a stretch of payload that
    // happens to share a few tones; the preamble's tone must also be the
    // loudest of its band in most of the twelve places.
    uint32_t c = d->pk_first + (uint32_t)((lo + hi) / 2);
    int match = 0;
    for (int i = 0; i < PRE; i++) {
        uint16_t v[NB], best_v, margin;
        uint8_t t;
        read_band(d, (int64_t)c + SYM * i, 0, v, &t, &best_v, &margin);
        match += t == PRE_A[i];
        read_band(d, (int64_t)c + SYM * i, 1, v, &t, &best_v, &margin);
        match += t == PRE_B[i];
    }
    if (match < MATCH_MIN) return;
    d->search_from = d->pk_first + (uint32_t)hi + SYM / 2;
    start_reception(d, center_q8);
}

static void on_hop(av_soundprov_t *d)
{
    uint32_t h = d->hop;
    int slot = (int)(h & (WIN - 1));
    // Correlate with each tone. The phase at the hop's first sample is
    // m * 64 * h mod 256 = 64 * (m * h mod 4). Each product is shifted down 8
    // bits: |x * cos| >> 8 < 2^21, times 64 samples < 2^27, times 4 hops < 2^29.
    for (int k = 0; k < NT; k++) {
        unsigned m = 20u + 2u * (unsigned)k;
        unsigned idx = ((m * h) & 3u) << 6;
        int32_t re = 0, im = 0;
        for (int i = 0; i < HOP; i++) {
            int32_t x = d->hopbuf[i];
            re += (x * COS[idx]) >> 8;
            im += (x * COS[(idx + 192u) & 255u]) >> 8;
            idx = (idx + m) & 255u;
        }
        d->sre[slot][k] = re;
        d->sim[slot][k] = im;
    }
    d->hop = h + 1;
    if (h < WIN - 1) return;

    // The window that just completed starts at hop w.
    uint32_t w = h - (WIN - 1);
    uint8_t *out = d->frac[w & (RING - 1)];
    for (int b = 0; b < 2; b++) {
        uint64_t e[NB], tot = 0;
        for (int k = 0; k < NB; k++) {
            int kk = b * NB + k;
            int64_t re = (int64_t)d->sre[0][kk] + d->sre[1][kk] + d->sre[2][kk] + d->sre[3][kk];
            int64_t im = (int64_t)d->sim[0][kk] + d->sim[1][kk] + d->sim[2][kk] + d->sim[3][kk];
            e[k] = (uint64_t)(re * re) + (uint64_t)(im * im);  // < 2^59
            tot += e[k];                                        // < 2^63
        }
        int sh = 0;
        while ((tot >> sh) > 0xFFFFFFu) sh++;
        uint32_t t = (uint32_t)(tot >> sh);
        for (int k = 0; k < NB; k++)
            out[b * NB + k] = t ? (uint8_t)(((uint32_t)(e[k] >> sh) * 255u + t / 2) / t) : 0;
    }

    preamble_search(d, w);
    // Read every symbol whose late edge window (start s + 10) is complete.
    while (d->rx && !d->done) {
        int64_t s = (d->t_q8 + 128) >> 8;
        if ((int64_t)w < s + SYM - 2) break;
        if ((int64_t)w - (s - 2) >= RING) {  // fell behind the ring: give up
            end_reception(d);
            break;
        }
        read_symbol(d);
    }
}

int av_soundprov_feed(av_soundprov_t *d, const int16_t *pcm, size_t n)
{
    if (!d) return 0;
    for (size_t i = 0; i < n && !d->done; i++) {
        d->hopbuf[d->fill++] = pcm[i];
        if (d->fill == HOP) {
            d->fill = 0;
            on_hop(d);
        }
    }
    return d->done;
}

int av_soundprov_progress(const av_soundprov_t *d)
{
    if (!d) return 0;
    if (d->done) return 100;
    if (!d->rx) return 0;
    int p = d->total ? d->j * 100 / d->total : d->j;
    if (p < 1) p = 1;
    return p > 99 ? 99 : p;
}

int av_soundprov_result(const av_soundprov_t *d, av_soundprov_result_t *out)
{
    if (!out) return 0;
    memset(out, 0, sizeof(*out));
    if (!d || !d->done) return 0;
    *out = d->res;
    return 1;
}
