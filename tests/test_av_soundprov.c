// Sound-wave Wi-Fi provisioning: the decoder (main/av_soundprov.c) against the
// page's encoder (mirrored in tests/soundprov_ref.c) through a simulated phone
// speaker, room, clock difference and microphone.
//
//   test_av_soundprov            unit tests + a short channel run (the gate)
//   test_av_soundprov --sweep    success rates against SNR and clock drift
//   test_av_soundprov --bench    host CPU time per second of audio
//   test_av_soundprov --golden   print the symbol hashes pinned below
//   test_av_soundprov --false    a minute each of noise, random chords on the
//                                modem's tones and a crude voice: must not decode
//   test_av_soundprov --wav F [--snr dB] [--drift d] [--seed n]
//                                decode a WAV file (any rate, 16-bit or float
//                                mono) through the channel; exit 0 if decoded
//
// The decoder's .c is included so its Reed-Solomon, CRC and parser can be
// tested directly; build this file without main/av_soundprov.c.
#include "av_soundprov.c"
#include "soundprov_ref.h"

#include <assert.h>
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#define PI 3.14159265358979323846

// ---- small helpers ----

static uint64_t rng_state = 88172645463325252ull;

static uint64_t rng(void)
{
    rng_state ^= rng_state << 13;
    rng_state ^= rng_state >> 7;
    rng_state ^= rng_state << 17;
    return rng_state;
}

static double urand(void)
{
    return (double)(rng() >> 11) * (1.0 / 9007199254740992.0);
}

static double grand(void)
{
    double u = urand(), v = urand();
    if (u < 1e-300) u = 1e-300;
    return sqrt(-2.0 * log(u)) * cos(2 * PI * v);
}

static uint32_t fnv1a(const uint8_t *p, int n)
{
    uint32_t h = 0x811C9DC5u;
    for (int i = 0; i < n; i++) h = (h ^ p[i]) * 0x01000193u;
    return h;
}

// ---- golden vectors: also pinned in tests/test_soundprov_page.py ----

typedef struct {
    const char *ssid, *pass, *server;
    int nsym;
    uint32_t hash;  // FNV-1a of the transmitted bytes after the preamble
} golden_t;

static const golden_t GOLDEN[] = {
    {"MyWiFi", "12345678", "", 45, 0xB3400EC1u},
    {"\xe5\xb0\x8f\xe7\xb1\xb3\xe8\xb7\xaf\xe7\x94\xb1\xe5\x99\xa8_5G",
     "p\xc3\xa4sswd \xe5\xaf\x86\xe7\xa0\x81!", "", 74, 0x47655DC8u},
    {"ABCDEFGHIJKLMNOPQRSTUVWXYZ012345",
     "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef", "example.org:8097", 191,
     0x1CD967C4u},
    {"Guest", "", "", 32, 0xD9B5C29Au},
};
#define NGOLDEN (int)(sizeof(GOLDEN) / sizeof(GOLDEN[0]))

static void golden_of(int i, int *nsym, uint32_t *hash)
{
    uint8_t ta[SP_REF_MAX_SYMBOLS], tb[SP_REF_MAX_SYMBOLS], bytes[SP_REF_MAX_SYMBOLS];
    *nsym = sp_ref_symbols(GOLDEN[i].ssid, GOLDEN[i].pass, GOLDEN[i].server, ta, tb, bytes);
    assert(*nsym > 0);
    *hash = fnv1a(bytes, *nsym - PRE);
}

// ---- unit tests ----

static void test_crc32(void)
{
    assert(crc32((const uint8_t *)"123456789", 9) == 0xCBF43926u);
}

// Systematic encoder written against the decoder's field tables.
static void enc(uint8_t *cw, int k, int npar)
{
    uint8_t g[MAX_PAR + 1] = {1};
    int len = 1;
    for (int i = 0; i < npar; i++) {
        uint8_t a = gf_pow2(i);
        g[len] = gf_mul(a, g[len - 1]);
        for (int j = len - 1; j >= 1; j--) g[j] ^= gf_mul(a, g[j - 1]);
        len++;
    }
    uint8_t *par = cw + k;
    memset(par, 0, (size_t)npar);
    for (int i = 0; i < k; i++) {
        uint8_t fb = cw[i] ^ par[0];
        memmove(par, par + 1, (size_t)npar - 1);
        par[npar - 1] = 0;
        for (int j = 0; j < npar; j++) par[j] ^= gf_mul(fb, g[j + 1]);
    }
}

static void test_reed_solomon(void)
{
    const int sizes[][2] = {{1, 4}, {9, 8}, {32, 16}, {40, 20}, {167, 84}, {20, 8}};
    for (unsigned s = 0; s < sizeof(sizes) / sizeof(sizes[0]); s++) {
        int k = sizes[s][0], npar = sizes[s][1], n = k + npar;
        for (int trial = 0; trial < 300; trial++) {
            uint8_t cw[MAX_CW], rx[MAX_CW], eras[MAX_CW];
            for (int i = 0; i < k; i++) cw[i] = (uint8_t)rng();
            enc(cw, k, npar);
            memcpy(rx, cw, (size_t)n);
            assert(rs_decode(rx, n, npar, eras, 0) == 0);
            // f erasures and e errors with 2e + f <= npar, distinct positions.
            int f = (int)(rng() % (unsigned)(npar + 1));
            int e = (int)(rng() % (unsigned)((npar - f) / 2 + 1));
            uint8_t used[MAX_CW] = {0};
            for (int i = 0; i < f + e; i++) {
                int pos;
                do pos = (int)(rng() % (unsigned)n); while (used[pos]);
                used[pos] = 1;
                if (i < f) {
                    eras[i] = (uint8_t)pos;
                    rx[pos] = (uint8_t)rng();          // an erasure may still be right
                } else {
                    rx[pos] ^= (uint8_t)(1 + rng() % 255);
                }
            }
            int r = rs_decode(rx, n, npar, eras, f);
            assert(r >= 0);
            assert(memcmp(rx, cw, (size_t)n) == 0);
        }
    }
}

static int build_payload(const char *ssid, const char *pass, const char *server, uint8_t *p)
{
    int L = 0;
    const char *f[3] = {ssid, pass, server};
    p[L++] = 1;
    for (int i = 0; i < 3; i++) {
        size_t n = strlen(f[i]);
        p[L++] = (uint8_t)n;
        memcpy(p + L, f[i], n);
        L += (int)n;
    }
    uint32_t c = crc32(p, L);
    for (int i = 0; i < 4; i++) p[L++] = (uint8_t)(c >> (8 * i));
    return L;
}

static void test_parse(void)
{
    uint8_t p[256];
    av_soundprov_result_t r;
    int L = build_payload("home", "secret12", "", p);
    memset(&r, 0x55, sizeof(r));
    assert(parse_payload(p, L, &r));
    assert(strcmp(r.ssid, "home") == 0 && strcmp(r.password, "secret12") == 0);
    assert(r.server[0] == '\0');
    p[3] ^= 1;  // CRC catches it
    assert(!parse_payload(p, L, &r));
    // An empty SSID, a NUL inside a field, a wrong version, lengths that do not
    // add up: each is refused even with a correct CRC.
    L = build_payload("", "x", "", p);
    assert(!parse_payload(p, L, &r));
    L = build_payload("a?b", "x", "", p);
    p[3] = 0;
    uint32_t c = crc32(p, L - 4);
    for (int i = 0; i < 4; i++) p[L - 4 + i] = (uint8_t)(c >> (8 * i));
    assert(!parse_payload(p, L, &r));
    L = build_payload("ab", "x", "", p);
    p[0] = 2;
    c = crc32(p, L - 4);
    for (int i = 0; i < 4; i++) p[L - 4 + i] = (uint8_t)(c >> (8 * i));
    assert(!parse_payload(p, L, &r));
    L = build_payload("ab", "x", "", p);
    p[1] = 3;
    c = crc32(p, L - 4);
    for (int i = 0; i < 4; i++) p[L - 4 + i] = (uint8_t)(c >> (8 * i));
    assert(!parse_payload(p, L, &r));
    assert(parity_for(9) == 8 && parity_for(32) == 16 && parity_for(33) == 18);
    assert(MAX_L + parity_for(MAX_L) <= MAX_CW);
}

static void test_golden(void)
{
    for (int i = 0; i < NGOLDEN; i++) {
        int nsym;
        uint32_t h;
        golden_of(i, &nsym, &h);
        assert(nsym == GOLDEN[i].nsym);
        assert(h == GOLDEN[i].hash);
    }
    uint8_t ta[SP_REF_MAX_SYMBOLS], tb[SP_REF_MAX_SYMBOLS];
    assert(sp_ref_symbols("", "x", "", ta, tb, NULL) < 0);
    assert(sp_ref_symbols("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456", "", "", ta, tb, NULL) < 0);
}

// ---- the channel ----

typedef struct {
    double snr_db;     // full band (0-8 kHz) at the microphone; >= 99: no noise
    double drift;      // device clock against the phone's: +0.005 = 0.5 % fast
    int garble;        // per repeat, one symbol sent with other tones
    int drop;          // per repeat, one symbol not sent at all
    int reverb;        // a small room: one reflection and a 0.4 s tail
    int pink;          // pink noise instead of white
    int repeats;
    int glitch_ms;     // per repeat, this many milliseconds of capture lost
} chan_t;

typedef struct {
    double b0, b1, b2, a1, a2, z1, z2;
} biquad_t;

static biquad_t biquad(int highpass, double f, double q, double fs)
{
    double w = 2 * PI * f / fs, c = cos(w), s = sin(w), al = s / (2 * q), a0 = 1 + al;
    biquad_t b = {0};
    if (highpass) {
        b.b0 = (1 + c) / 2 / a0;
        b.b1 = -(1 + c) / a0;
    } else {
        b.b0 = (1 - c) / 2 / a0;
        b.b1 = (1 - c) / a0;
    }
    b.b2 = b.b0;
    b.a1 = -2 * c / a0;
    b.a2 = (1 - al) / a0;
    return b;
}

// RBJ peaking equaliser: an uneven phone speaker.
static biquad_t peak(double f, double q, double gain_db, double fs)
{
    double A = pow(10.0, gain_db / 40), w = 2 * PI * f / fs, c = cos(w), al = sin(w) / (2 * q);
    double a0 = 1 + al / A;
    biquad_t b = {0};
    b.b0 = (1 + al * A) / a0;
    b.b1 = -2 * c / a0;
    b.b2 = (1 - al * A) / a0;
    b.a1 = -2 * c / a0;
    b.a2 = (1 - al / A) / a0;
    return b;
}

static double bq(biquad_t *b, double x)
{
    double y = b->b0 * x + b->z1;
    b->z1 = b->b1 * x - b->a1 * y + b->z2;
    b->z2 = b->b2 * x - b->a2 * y;
    return y;
}

// Windowed-sinc resampler from rate fs_in to 16 kHz on a clock (1 + drift)
// faster than the sender's: output m is input position m * fs_in / 16000 /
// (1 + drift). Cut off at 7 kHz, the codec's own anti-alias filter.
#define RS_HALF 32
#define RS_PHASES 512

static size_t resample(const float *in, size_t nin, double fs_in, double drift, double *out,
                       size_t max_out)
{
    static double table[RS_PHASES + 1][2 * RS_HALF];
    static double table_fs = 0;
    double fc = 7000.0 / fs_in;
    if (table_fs != fs_in) {
        for (int ph = 0; ph <= RS_PHASES; ph++) {
            double frac = (double)ph / RS_PHASES;
            for (int t = 0; t < 2 * RS_HALF; t++) {
                double x = (double)(t - RS_HALF + 1) - frac;
                double sinc = fabs(x) < 1e-12 ? 2 * fc : sin(2 * PI * fc * x) / (PI * x);
                double win = 0.42 + 0.5 * cos(PI * x / RS_HALF) + 0.08 * cos(2 * PI * x / RS_HALF);
                table[ph][t] = fabs(x) >= RS_HALF ? 0 : sinc * win;
            }
        }
        table_fs = fs_in;
    }
    double step = fs_in / 16000.0 / (1.0 + drift);
    size_t m = 0;
    for (; m < max_out; m++) {
        double u = m * step;
        size_t i = (size_t)u;
        if (i + RS_HALF >= nin) break;
        int ph = (int)lround((u - (double)i) * RS_PHASES);
        double acc = 0;
        for (int t = 0; t < 2 * RS_HALF; t++) {
            long k = (long)i + t - RS_HALF + 1;
            if (k >= 0) acc += in[k] * table[ph][t];
        }
        out[m] = acc;
    }
    return m;
}

// A Schroeder reverberator: four combs, two all-passes, at 16 kHz.
static void room(double *x, size_t n)
{
    static const int comb_len[4] = {557, 593, 641, 677};
    static const int ap_len[2] = {225, 76};
    double *buf[6];
    int pos[6] = {0};
    double g[4];
    for (int i = 0; i < 4; i++) {
        buf[i] = calloc((size_t)comb_len[i], sizeof(double));
        g[i] = pow(10.0, -3.0 * comb_len[i] / (0.4 * 16000.0));
    }
    for (int i = 0; i < 2; i++) buf[4 + i] = calloc((size_t)ap_len[i], sizeof(double));
    double refl[48] = {0};
    for (size_t t = 0; t < n; t++) {
        double in = x[t], wet = 0;
        for (int i = 0; i < 4; i++) {
            double y = buf[i][pos[i]];
            buf[i][pos[i]] = in + g[i] * y;
            pos[i] = (pos[i] + 1) % comb_len[i];
            wet += y;
        }
        for (int i = 0; i < 2; i++) {
            double *b = buf[4 + i];
            double y = b[pos[4 + i]];
            double v = wet + 0.7 * y;
            b[pos[4 + i]] = v;
            pos[4 + i] = (pos[4 + i] + 1) % ap_len[i];
            wet = y - 0.7 * v;
        }
        // One reflection 3 ms later (a table top), then the tail ~5 dB down.
        double early = refl[t % 48];
        refl[t % 48] = in;
        x[t] = in + 0.5 * early + 0.09 * wet;
    }
    for (int i = 0; i < 6; i++) free(buf[i]);
}

typedef struct {
    int16_t *pcm;
    size_t n;
    size_t repeat_end[8];  // 16 kHz sample where each repeat (and its gap) ends
} capture_t;

// Phone samples (at fs) -> speaker -> air/room -> microphone at 16 kHz.
static void channel(const float *phone, size_t nphone, double fs, const chan_t *c,
                    capture_t *cap)
{
    float *spk = malloc(nphone * sizeof(float));
    biquad_t h1 = biquad(1, 700, 0.707, fs), h2 = biquad(1, 700, 0.707, fs);
    biquad_t l1 = biquad(0, 6500, 0.707, fs);
    biquad_t p1 = peak(2800, 2, 6, fs), p2 = peak(4400, 3, -10, fs);
    for (size_t i = 0; i < nphone; i++) {
        double v = bq(&l1, bq(&h2, bq(&h1, phone[i])));
        if (c->reverb) v = bq(&p2, bq(&p1, v));  // the room case also gets a rough speaker
        spk[i] = (float)v;
    }
    size_t max_out = (size_t)(nphone * 16000.0 / fs * (1 + fabs(c->drift)) + 64);
    double *mic = malloc(max_out * sizeof(double));
    size_t n = resample(spk, nphone, fs, c->drift, mic, max_out);
    free(spk);
    if (c->reverb) room(mic, n);
    double p = 0;
    size_t active = 0;
    for (size_t i = 0; i < n; i++) {
        if (fabs(mic[i]) > 1e-4) {
            p += mic[i] * mic[i];
            active++;
        }
    }
    p = active ? p / active : 1e-9;
    double scale = 3000.0 / sqrt(p);  // signal RMS about -21 dBFS at the codec
    double sigma = c->snr_db >= 99 ? 0 : 3000.0 / pow(10.0, c->snr_db / 20.0);
    // Noise scaled to exactly sigma over the whole capture.
    double *noise = malloc(n * sizeof(double));
    double b0 = 0, b1 = 0, b2 = 0, b3 = 0, b4 = 0, b5 = 0, b6 = 0, pn = 0;
    for (size_t i = 0; i < n; i++) {
        double w = grand(), v = w;
        if (c->pink) {  // Paul Kellet's pink filter
            b0 = 0.99886 * b0 + w * 0.0555179;
            b1 = 0.99332 * b1 + w * 0.0750759;
            b2 = 0.96900 * b2 + w * 0.1538520;
            b3 = 0.86650 * b3 + w * 0.3104856;
            b4 = 0.55000 * b4 + w * 0.5329522;
            b5 = -0.7616 * b5 - w * 0.0168980;
            v = b0 + b1 + b2 + b3 + b4 + b5 + b6 + w * 0.5362;
            b6 = w * 0.115926;
        }
        noise[i] = v;
        pn += v * v;
    }
    double ns = n && pn > 0 ? sigma / sqrt(pn / n) : 0;
    cap->pcm = malloc(n * sizeof(int16_t));
    cap->n = n;
    for (size_t i = 0; i < n; i++) {
        double v = mic[i] * scale + ns * noise[i];
        if (v > 32767) v = 32767;
        if (v < -32768) v = -32768;
        cap->pcm[i] = (int16_t)lround(v);
    }
    free(noise);
    free(mic);
}

typedef struct {
    int repeats_needed;  // 1.. when decoded correctly, 0 when not decoded
    int wrong;           // decoded something other than what was sent
} outcome_t;

static outcome_t run_trial(const char *ssid, const char *pass, const char *server,
                           const chan_t *c)
{
    uint8_t ta[SP_REF_MAX_SYMBOLS], tb[SP_REF_MAX_SYMBOLS];
    int nsym = sp_ref_symbols(ssid, pass, server, ta, tb, NULL);
    assert(nsym > 0);
    const double fs = 48000;
    size_t lead = (size_t)(fs * (0.2 + 1.5 * urand()));  // random start offset
    size_t rep = sp_ref_repeat_len(nsym, fs);
    size_t total = lead + rep * (size_t)c->repeats;
    float *phone = calloc(total, sizeof(float));
    for (int r = 0; r < c->repeats; r++) {
        uint8_t a[SP_REF_MAX_SYMBOLS], b[SP_REF_MAX_SYMBOLS];
        memcpy(a, ta, (size_t)nsym);
        memcpy(b, tb, (size_t)nsym);
        int g = PRE + (int)(rng() % (unsigned)(nsym - PRE));
        if (c->garble) {
            a[g] = (uint8_t)((a[g] + 1 + rng() % 15) & 15);
            b[g] = (uint8_t)((b[g] + 1 + rng() % 15) & 15);
        }
        float *out = phone + lead + rep * (size_t)r;
        sp_ref_render(a, b, nsym, fs, out);
        if (c->drop) {
            int k = PRE + (int)(rng() % (unsigned)(nsym - PRE));
            size_t n0 = (size_t)llround(k * SP_REF_SYMBOL_S * fs);
            size_t n1 = (size_t)llround((k + 1) * SP_REF_SYMBOL_S * fs);
            memset(out + n0, 0, (n1 - n0) * sizeof(float));
        }
    }
    capture_t cap;
    channel(phone, total, fs, c, &cap);
    free(phone);
    for (int r = 0; r < c->repeats; r++)
        cap.repeat_end[r] =
            (size_t)((double)(lead + rep * (size_t)(r + 1)) * 16000.0 / fs * (1 + c->drift));
    if (c->glitch_ms) {
        // Lose a stretch of the capture somewhere in the middle of each
        // repeat, as a microphone task that missed a DMA buffer would.
        size_t gone = (size_t)c->glitch_ms * 16, removed = 0;
        for (int r = 0; r < c->repeats; r++) {
            size_t start = r ? cap.repeat_end[r - 1] : (size_t)(lead * 16000.0 / fs);
            size_t at = start + (size_t)((0.3 + 0.4 * urand()) * (double)(cap.repeat_end[r] - start)) - removed;
            memmove(cap.pcm + at, cap.pcm + at + gone, (cap.n - at - gone) * sizeof(int16_t));
            cap.n -= gone;
            removed += gone;
            cap.repeat_end[r] -= removed;
        }
    }

    av_soundprov_t *d = av_soundprov_create();
    assert(d);
    outcome_t o = {0, 0};
    size_t i = 0;
    int last_progress = 0;
    while (i < cap.n) {
        size_t chunk = 1 + rng() % 400;
        if (chunk > cap.n - i) chunk = cap.n - i;
        int done = av_soundprov_feed(d, cap.pcm + i, chunk);
        i += chunk;
        int p = av_soundprov_progress(d);
        assert(p >= 0 && p <= 100);
        last_progress = p;
        if (done) break;
    }
    av_soundprov_result_t res;
    if (av_soundprov_result(d, &res)) {
        assert(last_progress == 100);
        if (strcmp(res.ssid, ssid) || strcmp(res.password, pass) || strcmp(res.server, server)) {
            o.wrong = 1;
        } else {
            int r = 0;
            while (r < c->repeats - 1 && i > cap.repeat_end[r]) r++;
            o.repeats_needed = r + 1;
        }
    }
    av_soundprov_destroy(d);
    free(cap.pcm);
    return o;
}

// ---- gate tests ----

static void feed_all(av_soundprov_t *d, const int16_t *pcm, size_t n)
{
    size_t i = 0;
    while (i < n) {
        size_t chunk = 1 + rng() % 700;
        if (chunk > n - i) chunk = n - i;
        av_soundprov_feed(d, pcm + i, chunk);
        i += chunk;
    }
}

static void test_clean_loopback(void)
{
    av_soundprov_t *d = av_soundprov_create();
    assert(d);
    printf("decoder state: %zu bytes\n", sizeof(struct av_soundprov));
    for (int g = 0; g < NGOLDEN; g++) {
        uint8_t ta[SP_REF_MAX_SYMBOLS], tb[SP_REF_MAX_SYMBOLS];
        int nsym = sp_ref_symbols(GOLDEN[g].ssid, GOLDEN[g].pass, GOLDEN[g].server, ta, tb, NULL);
        size_t len = sp_ref_repeat_len(nsym, 16000);
        size_t lead = 3000 + rng() % 5000;
        float *f = malloc(len * sizeof(float));
        int16_t *pcm = calloc(lead + len, sizeof(int16_t));
        sp_ref_render(ta, tb, nsym, 16000, f);
        for (size_t i = 0; i < len; i++) pcm[lead + i] = (int16_t)lrintf(f[i] * 12000.0f);
        av_soundprov_reset(d);
        av_soundprov_result_t r;
        assert(!av_soundprov_result(d, &r) && r.ssid[0] == '\0');
        assert(av_soundprov_progress(d) == 0);
        // Half way through, the progress hint is somewhere in the middle.
        feed_all(d, pcm, lead + len / 2);
        int p = av_soundprov_progress(d);
        assert(p > 20 && p < 80);
        feed_all(d, pcm + lead + len / 2, len - len / 2);
        assert(av_soundprov_progress(d) == 100);
        assert(av_soundprov_feed(d, pcm, 10) == 1);  // sticky until reset
        assert(av_soundprov_result(d, &r) == 1);
        assert(strcmp(r.ssid, GOLDEN[g].ssid) == 0);
        assert(strcmp(r.password, GOLDEN[g].pass) == 0);
        assert(strcmp(r.server, GOLDEN[g].server) == 0);
        // Very quiet works as well as loud: the decoder looks at shares.
        for (size_t i = 0; i < len; i++) pcm[lead + i] = (int16_t)lrintf(f[i] * 60.0f);
        av_soundprov_reset(d);
        feed_all(d, pcm, lead + len);
        assert(av_soundprov_result(d, &r) == 1 && strcmp(r.ssid, GOLDEN[g].ssid) == 0);
        free(f);
        free(pcm);
    }
    av_soundprov_destroy(d);
}

// Noise, a stray tone, random two-tone chords: never a message.
static void test_no_false_message(void)
{
    av_soundprov_t *d = av_soundprov_create();
    const size_t n = 16000 * 30;
    int16_t *pcm = malloc(n * sizeof(int16_t));
    int chord_a = 0, chord_b = 0;
    for (size_t i = 0; i < n; i++) {
        if (i % 768 == 0) {
            chord_a = (int)(rng() % 16);
            chord_b = (int)(rng() % 16);
        }
        double t = (double)i / 16000;
        double v = 2000 * grand();
        if ((i / 160000) % 2)
            v += 6000 * (sin(2 * PI * (1250 + 125 * chord_a) * t) +
                         sin(2 * PI * (3250 + 125 * chord_b) * t));
        else
            v += 5000 * sin(2 * PI * 2000 * t);
        pcm[i] = (int16_t)lround(v);
    }
    assert(av_soundprov_feed(d, pcm, n) == 0);
    av_soundprov_destroy(d);
    free(pcm);
}

static void test_channel_short(void)
{
    // One case per impairment at a modest SNR; each must decode within the
    // three repeats the page plays.
    const chan_t cases[] = {
        {0, 0.005, 1, 1, 0, 0, 3, 0},
        {0, -0.005, 1, 1, 0, 0, 3, 0},
        {3, 0.002, 1, 0, 1, 1, 3, 0},
        {-3, 0, 0, 1, 0, 0, 3, 0},
        {99, -0.004, 1, 1, 1, 0, 3, 0},
        {6, 0.001, 0, 0, 0, 0, 3, 10},
    };
    for (unsigned i = 0; i < sizeof(cases) / sizeof(cases[0]); i++) {
        for (int t = 0; t < 3; t++) {
            outcome_t o = run_trial("TP-LINK_5A3F", "jiayou2024!!", "", &cases[i]);
            assert(!o.wrong);
            if (!o.repeats_needed) {
                fprintf(stderr, "channel case %u trial %d failed\n", i, t);
                assert(0);
            }
        }
    }
}

// ---- modes ----

static void sweep(void)
{
    const double snrs[] = {-15, -12, -10, -8, -6, -3, 0, 6, 20};
    const double drifts[] = {-0.005, 0, 0.005};
    const int N = 40;
    printf("SSID \"TP-LINK_5A3F\" + password \"jiayou2024!!\": %d trials per cell; one symbol "
           "garbled and one dropped in every repeat.\n",
           N);
    printf("Success after 1 / 2 / 3 repeats (%%), white noise, SNR over 0-8 kHz:\n");
    printf("%8s", "SNR dB");
    for (unsigned d = 0; d < 3; d++) printf("   drift %+.1f%%     ", drifts[d] * 100);
    printf("\n");
    int wrong = 0;
    for (unsigned s = 0; s < sizeof(snrs) / sizeof(snrs[0]); s++) {
        printf("%8.0f", snrs[s]);
        for (unsigned d = 0; d < 3; d++) {
            chan_t c = {snrs[s], drifts[d], 1, 1, 0, 0, 3, 0};
            int by[4] = {0};
            for (int t = 0; t < N; t++) {
                outcome_t o = run_trial("TP-LINK_5A3F", "jiayou2024!!", "", &c);
                wrong += o.wrong;
                for (int r = o.repeats_needed ? o.repeats_needed : 4; r <= 3; r++) by[r]++;
            }
            printf("   %3d / %3d / %3d  ", by[1] * 100 / N, by[2] * 100 / N, by[3] * 100 / N);
        }
        printf("\n");
        fflush(stdout);
    }
    printf("\nUneven speaker (+6 dB at 2.8 kHz, -10 dB at 4.4 kHz), room (reflection + 0.4 s "
           "tail), pink noise, drift +0.3%%, garbled + dropped symbol:\n");
    for (unsigned s = 0; s < sizeof(snrs) / sizeof(snrs[0]); s++) {
        chan_t c = {snrs[s], 0.003, 1, 1, 1, 1, 3, 0};
        int by[4] = {0};
        for (int t = 0; t < N; t++) {
            outcome_t o = run_trial("TP-LINK_5A3F", "jiayou2024!!", "", &c);
            wrong += o.wrong;
            for (int r = o.repeats_needed ? o.repeats_needed : 4; r <= 3; r++) by[r]++;
        }
        printf("%8.0f   %3d / %3d / %3d\n", snrs[s], by[1] * 100 / N, by[2] * 100 / N,
               by[3] * 100 / N);
        fflush(stdout);
    }
    printf("\nLongest message (32-byte SSID, 64-byte password, server), drift +0.5%%:\n");
    for (unsigned s = 0; s < sizeof(snrs) / sizeof(snrs[0]); s++) {
        chan_t c = {snrs[s], 0.005, 1, 1, 0, 0, 3, 0};
        int by[4] = {0};
        for (int t = 0; t < N / 2; t++) {
            outcome_t o = run_trial(GOLDEN[2].ssid, GOLDEN[2].pass, GOLDEN[2].server, &c);
            wrong += o.wrong;
            for (int r = o.repeats_needed ? o.repeats_needed : 4; r <= 3; r++) by[r]++;
        }
        printf("%8.0f   %3d / %3d / %3d\n", snrs[s], by[1] * 200 / N, by[2] * 200 / N,
               by[3] * 200 / N);
        fflush(stdout);
    }
    const int glitches[] = {5, 10, 15, 20, 30};
    printf("\nCapture loses N ms mid-repeat (white noise, SNR 6 dB, drift +0.2%%):\n");
    for (unsigned g = 0; g < sizeof(glitches) / sizeof(glitches[0]); g++) {
        chan_t c = {6, 0.002, 0, 0, 0, 0, 3, glitches[g]};
        int by[4] = {0};
        for (int t = 0; t < N; t++) {
            outcome_t o = run_trial("TP-LINK_5A3F", "jiayou2024!!", "", &c);
            wrong += o.wrong;
            for (int r = o.repeats_needed ? o.repeats_needed : 4; r <= 3; r++) by[r]++;
        }
        printf("%5d ms   %3d / %3d / %3d\n", glitches[g], by[1] * 100 / N, by[2] * 100 / N,
               by[3] * 100 / N);
        fflush(stdout);
    }
    printf("\nwrong messages accepted: %d\n", wrong);
}

static void bench(void)
{
    uint8_t ta[SP_REF_MAX_SYMBOLS], tb[SP_REF_MAX_SYMBOLS];
    int nsym = sp_ref_symbols("TP-LINK_5A3F", "jiayou2024!!", "", ta, tb, NULL);
    size_t len = sp_ref_repeat_len(nsym, 16000);
    const size_t seconds = 60, n = 16000 * seconds;
    float *f = malloc(len * sizeof(float));
    int16_t *pcm = malloc(n * sizeof(int16_t));
    sp_ref_render(ta, tb, nsym, 16000, f);
    // Signal (the message, over and over) with noise, so the receiver works.
    for (size_t i = 0; i < n; i++) pcm[i] = (int16_t)lrint(f[i % len] * 3000 + 800 * grand());
    av_soundprov_t *d = av_soundprov_create();
    int decoded = 0;
    clock_t t0 = clock();
    for (int rep = 0; rep < 5; rep++) {
        for (size_t i = 0; i < n; i += 320) {
            if (av_soundprov_feed(d, pcm + i, 320)) {
                decoded++;
                av_soundprov_reset(d);
            }
        }
    }
    double sec = (double)(clock() - t0) / CLOCKS_PER_SEC;
    printf("host: %.1f us of CPU per second of audio (%zu s fed 5 times, %d messages decoded)\n",
           sec * 1e6 / (5.0 * seconds), seconds, decoded);
    printf("state: %zu bytes\n", sizeof(struct av_soundprov));
    av_soundprov_destroy(d);
    free(f);
    free(pcm);
}

static int read_wav(const char *path, float **out, size_t *n, double *fs)
{
    FILE *fp = fopen(path, "rb");
    if (!fp) return 0;
    unsigned char h[12];
    if (fread(h, 1, 12, fp) != 12 || memcmp(h, "RIFF", 4) || memcmp(h + 8, "WAVE", 4)) {
        fclose(fp);
        return 0;
    }
    int fmt = 0, ch = 0, bits = 0;
    unsigned char c[8];
    while (fread(c, 1, 8, fp) == 8) {
        uint32_t len = c[4] | (c[5] << 8) | (c[6] << 16) | ((uint32_t)c[7] << 24);
        if (!memcmp(c, "fmt ", 4)) {
            unsigned char f[40] = {0};
            if (len > sizeof(f) || fread(f, 1, len, fp) != len) break;
            fmt = f[0] | (f[1] << 8);
            ch = f[2] | (f[3] << 8);
            *fs = (double)(f[4] | (f[5] << 8) | (f[6] << 16) | ((uint32_t)f[7] << 24));
            bits = f[14] | (f[15] << 8);
        } else if (!memcmp(c, "data", 4)) {
            if (ch != 1 || !((fmt == 1 && bits == 16) || (fmt == 3 && bits == 32))) break;
            size_t count = len / (size_t)(bits / 8);
            *out = malloc(count * sizeof(float));
            for (size_t i = 0; i < count; i++) {
                if (bits == 16) {
                    unsigned char s[2];
                    if (fread(s, 1, 2, fp) != 2) break;
                    (*out)[i] = (float)(int16_t)(s[0] | (s[1] << 8)) / 32768.0f;
                } else {
                    float v;
                    if (fread(&v, 4, 1, fp) != 1) break;
                    (*out)[i] = v;
                }
            }
            *n = count;
            fclose(fp);
            return 1;
        } else {
            fseek(fp, (long)(len + (len & 1)), SEEK_CUR);
        }
    }
    fclose(fp);
    return 0;
}

static int decode_wav(const char *path, double snr, double drift)
{
    float *in = NULL;
    size_t n = 0;
    double fs = 0;
    if (!read_wav(path, &in, &n, &fs)) {
        fprintf(stderr, "cannot read %s (mono 16-bit or float WAV)\n", path);
        return 2;
    }
    chan_t c = {snr, drift, 0, 0, 0, 0, 1, 0};
    capture_t cap;
    channel(in, n, fs, &c, &cap);
    free(in);
    av_soundprov_t *d = av_soundprov_create();
    size_t i = 0;
    for (; i < cap.n; i += 256) {
        size_t k = cap.n - i < 256 ? cap.n - i : 256;
        if (av_soundprov_feed(d, cap.pcm + i, k)) break;
    }
    av_soundprov_result_t r;
    int ok = av_soundprov_result(d, &r);
    if (ok) {
        const char *names[3] = {"ssid", "password", "server"}, *values[3] = {r.ssid, r.password, r.server};
        printf("{");
        for (int f = 0; f < 3; f++) {
            printf("\"%s\": \"", names[f]);
            for (const unsigned char *c = (const unsigned char *)values[f]; *c; c++) {
                if (*c == '"' || *c == '\\') printf("\\%c", *c);
                else if (*c < 0x20) printf("\\u%04x", *c);
                else putchar(*c);
            }
            printf("\", ");
        }
        printf("\"at_s\": %.2f}\n", (double)i / 16000);
    } else
        printf("not decoded (%zu samples at 16 kHz)\n", cap.n);
    av_soundprov_destroy(d);
    free(cap.pcm);
    return ok ? 0 : 1;
}

int main(int argc, char **argv)
{
    if (argc > 1 && !strcmp(argv[1], "--golden")) {
        for (int i = 0; i < NGOLDEN; i++) {
            int nsym;
            uint32_t h;
            golden_of(i, &nsym, &h);
            printf("%d symbols, hash 0x%08X, %.2f s\n", nsym, h, nsym * SP_REF_SYMBOL_S);
        }
        return 0;
    }
    if (argc > 1 && !strcmp(argv[1], "--sweep")) {
        sweep();
        return 0;
    }
    if (argc > 1 && !strcmp(argv[1], "--false")) {
        // 60 s each of white noise, symbol-synchronous random chords on the
        // modem's own tones, and a crude voice (harmonics of a wandering pitch).
        const size_t n = 16000 * 60;
        int16_t *pcm = malloc(n * sizeof(int16_t));
        for (int kind = 0; kind < 3; kind++) {
            int ca = 0, cb = 0;
            double ph = 0, f0 = 150;
            for (size_t i = 0; i < n; i++) {
                double v = 1500 * grand();
                if (kind == 1) {
                    if (i % 768 == 0) {
                        ca = (int)(rng() % 16);
                        cb = (int)(rng() % 16);
                    }
                    double t = (double)i / 16000;
                    v += 5000 * (sin(2 * PI * (1250 + 125 * ca) * t) + sin(2 * PI * (3250 + 125 * cb) * t));
                } else if (kind == 2) {
                    if (i % 800 == 0) f0 = 90 + 160 * urand();
                    ph += 2 * PI * f0 / 16000;
                    double env = 0.5 + 0.5 * sin(2 * PI * 3.0 * (double)i / 16000);
                    for (int h = 1; h * f0 < 7000; h++) v += env * 3000.0 / h * sin(h * ph);
                }
                if (v > 32767) v = 32767;
                if (v < -32768) v = -32768;
                pcm[i] = (int16_t)lround(v);
            }
            av_soundprov_t *d = av_soundprov_create();
            printf("kind %d: decoded %d\n", kind, av_soundprov_feed(d, pcm, n));
            av_soundprov_destroy(d);
        }
        free(pcm);
        return 0;
    }
    if (argc > 1 && !strcmp(argv[1], "--bench")) {
        bench();
        return 0;
    }
    if (argc > 2 && !strcmp(argv[1], "--wav")) {
        double snr = 99, drift = 0;
        for (int i = 3; i + 1 < argc; i += 2) {
            if (!strcmp(argv[i], "--snr")) snr = atof(argv[i + 1]);
            else if (!strcmp(argv[i], "--drift")) drift = atof(argv[i + 1]);
            else if (!strcmp(argv[i], "--seed")) rng_state = (uint64_t)atoll(argv[i + 1]) | 1;
        }
        return decode_wav(argv[2], snr, drift);
    }
    test_crc32();
    test_reed_solomon();
    test_parse();
    test_golden();
    test_clean_loopback();
    test_no_false_message();
    test_channel_short();
    printf("av_soundprov: PASS\n");
    return 0;
}
