#include "av_identity.h"

#include <string.h>

static const char HEX[] = "0123456789abcdef";
static const char B64URL[] =
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";

void av_identity_format_id(const uint8_t mac[6], char out[AV_DEVICE_ID_LEN + 1])
{
    for (unsigned i = 0; i < 6; i++) {
        out[2 * i] = HEX[mac[i] >> 4];
        out[2 * i + 1] = HEX[mac[i] & 0x0f];
    }
    out[AV_DEVICE_ID_LEN] = '\0';
}

void av_identity_encode_secret(const uint8_t raw[AV_DEVICE_SECRET_BYTES],
                               char out[AV_DEVICE_SECRET_LEN + 1])
{
    size_t o = 0;
    size_t i = 0;
    // Whole groups of three bytes become four characters.
    for (; i + 3 <= AV_DEVICE_SECRET_BYTES; i += 3) {
        uint32_t v = ((uint32_t)raw[i] << 16) | ((uint32_t)raw[i + 1] << 8) | raw[i + 2];
        out[o++] = B64URL[(v >> 18) & 63];
        out[o++] = B64URL[(v >> 12) & 63];
        out[o++] = B64URL[(v >> 6) & 63];
        out[o++] = B64URL[v & 63];
    }
    // 32 = 10 * 3 + 2: the last two bytes make three characters, no padding.
    uint32_t v = ((uint32_t)raw[i] << 16) | ((uint32_t)raw[i + 1] << 8);
    out[o++] = B64URL[(v >> 18) & 63];
    out[o++] = B64URL[(v >> 12) & 63];
    out[o++] = B64URL[(v >> 6) & 63];
    out[o] = '\0';
}

bool av_identity_secret_valid(const char *text)
{
    if (!text || strlen(text) != AV_DEVICE_SECRET_LEN) {
        return false;
    }
    for (size_t i = 0; i < AV_DEVICE_SECRET_LEN; i++) {
        if (!strchr(B64URL, text[i]) || text[i] == '\0') {
            return false;
        }
    }
    return true;
}

void av_identity_clean_fw(const char *in, char *out, size_t max_len)
{
    size_t o = 0;
    for (size_t i = 0; in && in[i] && o < max_len; i++) {
        char c = in[i];
        if (c >= ' ' && c <= '~') {
            out[o++] = c;
        }
    }
    out[o] = '\0';
}
