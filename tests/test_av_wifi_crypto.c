// The device half of the Wi-Fi message encryption (main/av_wifi_crypto.c),
// built on the host against the mbedTLS sources ESP-IDF ships, so it runs the
// same library code the firmware links. The envelope below is the one
// tests/test_wifi_crypto.py checks on the server side: if either side changes
// the algorithm, the labels or the order of the salt, one of the two fails.
#include "av_wifi_crypto.h"

#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

int av_wifi_crypto_rng(void *ctx, unsigned char *out, size_t length)
{
    (void)ctx;
    // Blinding only: any bytes give the same result, which the vectors check.
    static uint32_t state = 0x12345678u;
    for (size_t i = 0; i < length; i++) {
        state = state * 1664525u + 1013904223u;
        out[i] = (unsigned char)(state >> 24);
    }
    return 0;
}

static void hex(const char *text, uint8_t *out, size_t n)
{
    assert(strlen(text) == n * 2);
    for (size_t i = 0; i < n; i++) {
        unsigned v;
        sscanf(text + 2 * i, "%2x", &v);
        out[i] = (uint8_t)v;
    }
}

static const char DEVICE_PRIVATE[] = "77076d0a7318a57d3c16c17251b26645df4c2f87ebc0992ab177fba51db92c2a";
static const char SERVER_PRIVATE[] = "5dab087e624a8a4b79e17f8b83800ee66f3bb1292618b6fd1c2f8b27ff88e0eb";

static void test_x25519(void)
{
    uint8_t a[32], b[32], pa[32], pb[32], want[32];
    hex(DEVICE_PRIVATE, a, 32);
    hex(SERVER_PRIVATE, b, 32);
    assert(av_wifi_crypto_public(a, pa));
    assert(av_wifi_crypto_public(b, pb));
    hex("8520f0098930a754748b7ddcb43ef75a0dbf3a0d26381af4eba4a98eaa9b4e6a", want, 32);
    assert(memcmp(pa, want, 32) == 0);
    hex("de9edb7d7b7dc1b4d35b61c2ece435373f8343c85b78674dadfc7e146f882b4f", want, 32);
    assert(memcmp(pb, want, 32) == 0);
}

static void test_key(void)
{
    uint8_t a[32], b[32], pa[32], pb[32], k1[32], k2[32], want[32];
    hex(DEVICE_PRIVATE, a, 32);
    hex(SERVER_PRIVATE, b, 32);
    av_wifi_crypto_public(a, pa);
    av_wifi_crypto_public(b, pb);
    assert(av_wifi_crypto_key(a, pb, pa, pb, k1));
    assert(av_wifi_crypto_key(b, pa, pa, pb, k2));
    hex("e199edc6ebecdf6b5e4220633c4f4177c1fd7e4ff951ded7f59903670ddcdcfb", want, 32);
    assert(memcmp(k1, want, 32) == 0 && memcmp(k2, want, 32) == 0);
    // A low-order (all-zero) peer key gives no usable secret.
    const uint8_t zero[32] = { 0 };
    assert(!av_wifi_crypto_key(a, zero, pa, zero, k1));
}

static const char NONCE[] = "AAECAwQFBgcICQoL";
static const char SEALED[] =
    "Xa5ImP6K9whZIov1TEK7yBuVwus5hf5hb2BTiXrXgdXmik4N3P8Uh3o_divwEQ6NaNwx8OquEeSUEtaYIX8ODRp4nPUudYmDHXk"
    "OA1SLlTNiuoKyqcO40Hmvz4DlelTguMriPqw";
static const char PLAIN[] =
    "{\"seq\":1760000000000,\"op\":\"add\",\"ssid\":\"\xE5\xAE\xB6\xE9\x87\x8C\xE7\x9A\x84WiFi\","
    "\"password\":\"p@ss word \xE4\xB8\xAD\xE6\x96\x87\"}";

static void test_open(void)
{
    uint8_t dev[32], srv[32], srv_pub[32];
    uint8_t out[AV_WIFI_PLAIN_MAX + 1];
    hex(DEVICE_PRIVATE, dev, 32);
    hex(SERVER_PRIVATE, srv, 32);
    av_wifi_crypto_public(srv, srv_pub);
    int n = av_wifi_crypto_open(dev, srv_pub, NONCE, SEALED, out, sizeof(out));
    assert(n == (int)strlen(PLAIN));
    assert(strcmp((char *)out, PLAIN) == 0);

    // One bit off anywhere: refused, and nothing left in the buffer.
    char bad[sizeof(SEALED)];
    memcpy(bad, SEALED, sizeof(SEALED));
    bad[5] = bad[5] == 'A' ? 'B' : 'A';
    assert(av_wifi_crypto_open(dev, srv_pub, NONCE, bad, out, sizeof(out)) == -1);
    memcpy(bad, SEALED, sizeof(SEALED));
    bad[sizeof(SEALED) - 3] = bad[sizeof(SEALED) - 3] == 'A' ? 'B' : 'A';
    assert(av_wifi_crypto_open(dev, srv_pub, NONCE, bad, out, sizeof(out)) == -1);
    assert(av_wifi_crypto_open(dev, srv_pub, "AAECAwQFBgcICQoM", SEALED, out, sizeof(out)) == -1);
    // The wrong server key.
    uint8_t other[32];
    av_wifi_crypto_public(dev, other);
    assert(av_wifi_crypto_open(dev, other, NONCE, SEALED, out, sizeof(out)) == -1);
    // Malformed fields and a short buffer.
    assert(av_wifi_crypto_open(dev, srv_pub, "AAEC", SEALED, out, sizeof(out)) == -1);
    assert(av_wifi_crypto_open(dev, srv_pub, NONCE, "AA", out, sizeof(out)) == -1);
    assert(av_wifi_crypto_open(dev, srv_pub, NONCE, SEALED, out, 64) == -1);
    assert(av_wifi_crypto_open(dev, srv_pub, NULL, SEALED, out, sizeof(out)) == -1);
}

static void test_base64(void)
{
    char text[64];
    uint8_t raw[48];
    const uint8_t data[] = { 0xfb, 0xff, 0x00, 0x10, 0x83 };
    for (size_t n = 0; n <= sizeof(data); n++) {
        size_t m = av_b64url_encode(data, n, text, sizeof(text));
        assert(m == (n * 4 + 2) / 3);
        assert(!strchr(text, '=') && !strchr(text, '+') && !strchr(text, '/'));
        if (n) {
            assert(av_b64url_decode(text, raw, sizeof(raw)) == (int)n);
            assert(memcmp(raw, data, n) == 0);
        }
    }
    assert(strcmp((av_b64url_encode(data, 3, text, sizeof(text)), text), "-_8A") == 0);
    assert(av_b64url_encode(data, 5, text, 7) == 0);          // needs 7 + NUL
    assert(av_b64url_decode("", raw, sizeof(raw)) == -1);
    assert(av_b64url_decode("A", raw, sizeof(raw)) == -1);
    assert(av_b64url_decode("AB=C", raw, sizeof(raw)) == -1);
    assert(av_b64url_decode("AB+C", raw, sizeof(raw)) == -1);
    assert(av_b64url_decode("AQ", raw, sizeof(raw)) == 1 && raw[0] == 1);
    assert(av_b64url_decode("AB", raw, sizeof(raw)) == -1);   // non-zero leftover bits
    assert(av_b64url_decode("AAAA", raw, 2) == -1);           // does not fit
}

int main(void)
{
    test_x25519();
    test_key();
    test_open();
    test_base64();
    puts("test_av_wifi_crypto: PASS");
    return 0;
}
