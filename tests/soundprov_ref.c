// C mirror of the page's encoder; see soundprov_ref.h. Written independently of
// main/av_soundprov.c on purpose: a shared table would hide a mismatch.
#include "soundprov_ref.h"

#include <math.h>
#include <string.h>

#define SP_PI 3.14159265358979323846

static const uint8_t PRE_A[6] = {3, 12, 0, 9, 15, 6};
static const uint8_t PRE_B[6] = {10, 1, 13, 4, 7, 14};

static uint8_t gexp[512], glog[256];

static void gf_init(void)
{
    if (gexp[0]) return;
    unsigned x = 1;
    for (int i = 0; i < 255; i++) {
        gexp[i] = (uint8_t)x;
        glog[x] = (uint8_t)i;
        x <<= 1;
        if (x & 0x100) x ^= 0x11d;
    }
    for (int i = 255; i < 512; i++) gexp[i] = gexp[i - 255];
}

static uint8_t mul(uint8_t a, uint8_t b)
{
    return (a && b) ? gexp[glog[a] + glog[b]] : 0;
}

// parity[npar] = data * x^npar mod prod_{i<npar} (x + alpha^i)
static void rs_encode(const uint8_t *data, int k, int npar, uint8_t *parity)
{
    uint8_t g[100] = {1};
    int len = 1;
    for (int i = 0; i < npar; i++) {
        uint8_t a = gexp[i];
        g[len] = mul(a, g[len - 1]);
        for (int j = len - 1; j >= 1; j--) g[j] ^= mul(a, g[j - 1]);
        len++;
    }
    memset(parity, 0, (size_t)npar);
    for (int i = 0; i < k; i++) {
        uint8_t fb = data[i] ^ parity[0];
        memmove(parity, parity + 1, (size_t)npar - 1);
        parity[npar - 1] = 0;
        for (int j = 0; j < npar; j++) parity[j] ^= mul(fb, g[j + 1]);
    }
}

static uint32_t crc32(const uint8_t *p, int n)
{
    uint32_t c = 0xFFFFFFFFu;
    for (int i = 0; i < n; i++) {
        c ^= p[i];
        for (int b = 0; b < 8; b++) c = (c & 1u) ? (c >> 1) ^ 0xEDB88320u : c >> 1;
    }
    return ~c;
}

int sp_ref_symbols(const char *ssid, const char *pass, const char *server,
                   uint8_t *tones_a, uint8_t *tones_b, uint8_t *bytes)
{
    gf_init();
    const char *field[3] = {ssid, pass, server};
    const size_t limit[3] = {32, 64, 63};
    uint8_t cw[255];
    int L = 0;
    cw[L++] = 1;
    for (int f = 0; f < 3; f++) {
        size_t n = strlen(field[f]);
        if (n > limit[f] || (f == 0 && n == 0)) return -1;
        cw[L++] = (uint8_t)n;
        memcpy(cw + L, field[f], n);
        L += (int)n;
    }
    uint32_t crc = crc32(cw, L);
    for (int i = 0; i < 4; i++) cw[L++] = (uint8_t)(crc >> (8 * i));
    int npar = 2 * ((L + 3) / 4);
    if (npar < 8) npar = 8;
    rs_encode(cw, L, npar, cw + L);
    int n = L + npar;

    uint8_t stream[5 + 255];
    stream[0] = (uint8_t)L;
    rs_encode(stream, 1, 4, stream + 1);
    memcpy(stream + 5, cw, (size_t)n);

    int s = 0;
    for (int i = 0; i < 6; i++, s++) {
        tones_a[s] = PRE_A[i];
        tones_b[s] = PRE_B[i];
    }
    for (int j = 0; j < 5 + n; j++, s++) {
        uint8_t b = stream[j] ^ (uint8_t)(((uint32_t)(j + 1) * 0x9E3779B1u) >> 24);
        if (bytes) bytes[j] = b;
        tones_a[s] = b >> 4;
        tones_b[s] = b & 15;
    }
    return s;
}

size_t sp_ref_repeat_len(int nsym, double sr)
{
    return (size_t)llround(nsym * SP_REF_SYMBOL_S * sr) + (size_t)llround(SP_REF_GAP_S * sr);
}

void sp_ref_render(const uint8_t *tones_a, const uint8_t *tones_b, int nsym, double sr,
                   float *out)
{
    const double ramp = 0.003 * sr;
    size_t total = sp_ref_repeat_len(nsym, sr);
    memset(out, 0, total * sizeof(float));
    for (int j = 0; j < nsym; j++) {
        size_t n0 = (size_t)llround(j * SP_REF_SYMBOL_S * sr);
        size_t n1 = (size_t)llround((j + 1) * SP_REF_SYMBOL_S * sr);
        double fa = 1250.0 + 125.0 * tones_a[j];
        double fb = 3250.0 + 125.0 * tones_b[j];
        for (size_t n = n0; n < n1; n++) {
            double t = (double)(n - n0), env = 1.0;
            double left = (double)(n1 - 1 - n);
            if (t < ramp) env = 0.5 - 0.5 * cos(SP_PI * t / ramp);
            else if (left < ramp) env = 0.5 - 0.5 * cos(SP_PI * left / ramp);
            double x = t / sr;
            out[n] = (float)(env * 0.45 * (sin(2 * SP_PI * fa * x) + sin(2 * SP_PI * fb * x)));
        }
    }
}
