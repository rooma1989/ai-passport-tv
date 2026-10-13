// End-to-end encryption of the Wi-Fi management messages; see av_wifi_crypto.h.
#include "av_wifi_crypto.h"

#include <stdlib.h>
#include <string.h>

#include "mbedtls/ecp.h"
#include "mbedtls/gcm.h"
#include "mbedtls/md.h"

static const char INFO[] = "ai-passport-tv wifi v1";
static const char AAD[] = "avwifi1";

// Wipe without the compiler deciding the store is dead.
static void wipe(void *p, size_t n)
{
    volatile uint8_t *v = (volatile uint8_t *)p;
    while (n--) *v++ = 0;
}

// RFC 7748 X25519 through mbedTLS's Curve25519 group. The scalar is clamped
// here rather than left to the library, so any 32 bytes are a valid key, as
// they are in the RFC and in server/wifi_crypto.py.
static bool x25519(const uint8_t scalar[32], const uint8_t u[32], uint8_t out[32])
{
    mbedtls_ecp_group grp;
    mbedtls_ecp_point q, r;
    mbedtls_mpi d;
    uint8_t k[32];
    mbedtls_ecp_group_init(&grp);
    mbedtls_ecp_point_init(&q);
    mbedtls_ecp_point_init(&r);
    mbedtls_mpi_init(&d);
    memcpy(k, scalar, sizeof(k));
    k[0] &= 248;
    k[31] &= 127;
    k[31] |= 64;
    // Reading the point masks the top bit, as the RFC asks, and the library
    // refuses the handful of low-order points outright.
    // A Montgomery point is written as its x coordinate, little-endian.
    size_t written = 0;
    bool ok = mbedtls_ecp_group_load(&grp, MBEDTLS_ECP_DP_CURVE25519) == 0 &&
              mbedtls_mpi_read_binary_le(&d, k, sizeof(k)) == 0 &&
              mbedtls_ecp_point_read_binary(&grp, &q, u, 32) == 0 &&
              mbedtls_ecp_mul(&grp, &r, &d, &q, av_wifi_crypto_rng, NULL) == 0 &&
              !mbedtls_ecp_is_zero(&r) &&
              mbedtls_ecp_point_write_binary(&grp, &r, MBEDTLS_ECP_PF_UNCOMPRESSED, &written, out, 32) == 0 &&
              written == 32;
    wipe(k, sizeof(k));
    mbedtls_mpi_free(&d);
    mbedtls_ecp_point_free(&r);
    mbedtls_ecp_point_free(&q);
    mbedtls_ecp_group_free(&grp);
    return ok;
}

bool av_wifi_crypto_public(const uint8_t private_key[AV_WIFI_KEY_BYTES],
                           uint8_t public_key[AV_WIFI_KEY_BYTES])
{
    static const uint8_t base[32] = { 9 };
    return x25519(private_key, base, public_key);
}

bool av_wifi_crypto_key(const uint8_t own_private[AV_WIFI_KEY_BYTES],
                        const uint8_t peer_public[AV_WIFI_KEY_BYTES],
                        const uint8_t device_public[AV_WIFI_KEY_BYTES],
                        const uint8_t server_public[AV_WIFI_KEY_BYTES],
                        uint8_t key[AV_WIFI_KEY_BYTES])
{
    const mbedtls_md_info_t *sha = mbedtls_md_info_from_type(MBEDTLS_MD_SHA256);
    uint8_t shared[32], salt[64], prk[32], block[sizeof(INFO) - 1 + 1];
    if (!sha || !x25519(own_private, peer_public, shared)) return false;
    uint8_t any = 0;
    for (unsigned i = 0; i < sizeof(shared); i++) any |= shared[i];
    bool ok = any != 0;
    // HKDF-SHA256 (RFC 5869) by hand: one HMAC to extract, one block to expand,
    // because 32 bytes is a single SHA-256 block and the firmware's mbedTLS is
    // built without MBEDTLS_HKDF_C.
    memcpy(salt, device_public, 32);
    memcpy(salt + 32, server_public, 32);
    memcpy(block, INFO, sizeof(INFO) - 1);
    block[sizeof(INFO) - 1] = 1;
    ok = ok && mbedtls_md_hmac(sha, salt, sizeof(salt), shared, sizeof(shared), prk) == 0 &&
         mbedtls_md_hmac(sha, prk, sizeof(prk), block, sizeof(block), key) == 0;
    wipe(shared, sizeof(shared));
    wipe(prk, sizeof(prk));
    if (!ok) wipe(key, AV_WIFI_KEY_BYTES);
    return ok;
}

int av_wifi_crypto_open(const uint8_t device_private[AV_WIFI_KEY_BYTES],
                        const uint8_t server_public[AV_WIFI_KEY_BYTES],
                        const char *nonce_b64, const char *sealed_b64,
                        uint8_t *out, size_t out_size)
{
    if (!nonce_b64 || !sealed_b64 || !out || out_size < AV_WIFI_PLAIN_MAX + 1u ||
        strlen(sealed_b64) > AV_WIFI_SEALED_B64_MAX) {
        return -1;
    }
    uint8_t nonce[AV_WIFI_NONCE_BYTES], device_public[32], key[32];
    if (av_b64url_decode(nonce_b64, nonce, sizeof(nonce)) != (int)sizeof(nonce)) return -1;
    // One short-lived block for the ciphertext and the GCM context: about a
    // kilobyte, freed before returning.
    const size_t sealed_max = AV_WIFI_PLAIN_MAX + AV_WIFI_TAG_BYTES;
    uint8_t *sealed = malloc(sealed_max);
    mbedtls_gcm_context *gcm = malloc(sizeof(*gcm));
    int result = -1;
    if (!sealed || !gcm) goto done;
    const int n = av_b64url_decode(sealed_b64, sealed, sealed_max);
    if (n < (int)AV_WIFI_TAG_BYTES) goto done;
    const size_t length = (size_t)n - AV_WIFI_TAG_BYTES;
    if (!av_wifi_crypto_public(device_private, device_public) ||
        !av_wifi_crypto_key(device_private, server_public, device_public, server_public, key)) {
        goto done;
    }
    mbedtls_gcm_init(gcm);
    if (mbedtls_gcm_setkey(gcm, MBEDTLS_CIPHER_ID_AES, key, 256) == 0 &&
        mbedtls_gcm_auth_decrypt(gcm, length, nonce, sizeof(nonce),
                                 (const unsigned char *)AAD, sizeof(AAD) - 1,
                                 sealed + length, AV_WIFI_TAG_BYTES, sealed, out) == 0) {
        out[length] = '\0';
        result = (int)length;
    } else {
        wipe(out, length);
    }
    mbedtls_gcm_free(gcm);
done:
    wipe(key, sizeof(key));
    if (sealed) { wipe(sealed, sealed_max); free(sealed); }
    free(gcm);
    return result;
}

static const char B64[] = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";

size_t av_b64url_encode(const uint8_t *in, size_t length, char *out, size_t out_size)
{
    const size_t need = (length * 4u + 2u) / 3u;
    if (!out || out_size < need + 1u) return 0;
    size_t o = 0;
    for (size_t i = 0; i < length; i += 3) {
        uint32_t v = (uint32_t)in[i] << 16;
        if (i + 1 < length) v |= (uint32_t)in[i + 1] << 8;
        if (i + 2 < length) v |= in[i + 2];
        out[o++] = B64[(v >> 18) & 63];
        out[o++] = B64[(v >> 12) & 63];
        if (i + 1 < length) out[o++] = B64[(v >> 6) & 63];
        if (i + 2 < length) out[o++] = B64[v & 63];
    }
    out[o] = '\0';
    return o;
}

static int b64_value(char c)
{
    const char *p = c ? strchr(B64, c) : NULL;
    return p ? (int)(p - B64) : -1;
}

int av_b64url_decode(const char *in, uint8_t *out, size_t out_size)
{
    if (!in || !out) return -1;
    const size_t length = strlen(in);
    if (length == 0 || length % 4u == 1u) return -1;
    size_t o = 0;
    uint32_t acc = 0;
    unsigned bits = 0;
    for (size_t i = 0; i < length; i++) {
        const int v = b64_value(in[i]);
        if (v < 0) return -1;
        acc = (acc << 6) | (uint32_t)v;
        bits += 6;
        if (bits >= 8) {
            bits -= 8;
            if (o >= out_size) return -1;
            out[o++] = (uint8_t)(acc >> bits);
        }
    }
    // Leftover bits must be zero, so each byte string has one spelling.
    if (acc & ((1u << bits) - 1u)) return -1;
    return (int)o;
}
