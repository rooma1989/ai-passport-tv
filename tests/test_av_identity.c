// The identity fields of HELLO, in the shapes server/tenancy/gate.py accepts:
// device_id ^[0-9a-f]{12}$, device_secret ^[A-Za-z0-9_-]{43}$.
#include "av_identity.h"

#include <assert.h>
#include <stdio.h>
#include <string.h>

static void test_id(void)
{
    const uint8_t mac[6] = {0x98, 0xc3, 0x77, 0x0a, 0xbc, 0xff};
    char id[AV_DEVICE_ID_LEN + 1];
    memset(id, 'x', sizeof(id));
    av_identity_format_id(mac, id);
    assert(strcmp(id, "98c3770abcff") == 0);
    assert(strlen(id) == 12);
}

static void test_secret_encoding(void)
{
    uint8_t raw[AV_DEVICE_SECRET_BYTES];
    char text[AV_DEVICE_SECRET_LEN + 1];

    memset(raw, 0, sizeof(raw));
    av_identity_encode_secret(raw, text);
    assert(strlen(text) == 43);
    for (size_t i = 0; i < 43; i++) assert(text[i] == 'A');
    assert(av_identity_secret_valid(text));

    // 0xff everywhere: every 6-bit group is 63 ('_'), and the last character
    // carries the final 4 bits followed by two zero bits (60, '8').
    memset(raw, 0xff, sizeof(raw));
    av_identity_encode_secret(raw, text);
    for (size_t i = 0; i < 42; i++) assert(text[i] == '_');
    assert(text[42] == '8');

    // A known vector: bytes 0..31, checked against Python's
    // base64.urlsafe_b64encode(bytes(range(32))).rstrip(b"=").
    for (unsigned i = 0; i < 32; i++) raw[i] = (uint8_t)i;
    av_identity_encode_secret(raw, text);
    assert(strcmp(text, "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8") == 0);
    assert(av_identity_secret_valid(text));
}

static void test_secret_validation(void)
{
    assert(!av_identity_secret_valid(NULL));
    assert(!av_identity_secret_valid(""));
    assert(!av_identity_secret_valid("AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh"));    // 42
    assert(!av_identity_secret_valid("AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8A"));  // 44
    assert(!av_identity_secret_valid("AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh="));   // padding
    assert(!av_identity_secret_valid("AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdH+8"));   // '+' is not url-safe
}

static void test_fw_and_model(void)
{
    char out[8];
    av_identity_clean_fw("v1.2\n\x01-dirty-long", out, 7);
    assert(strcmp(out, "v1.2-di") == 0);
    av_identity_clean_fw(NULL, out, 7);
    assert(out[0] == '\0');
    // Host builds have no sdkconfig: the Passport model, like av_protocol.h.
    assert(strcmp(AV_DEVICE_MODEL, "ai-passport") == 0);
}

int main(void)
{
    test_id();
    test_secret_encoding();
    test_secret_validation();
    test_fw_and_model();
    puts("test_av_identity: ok");
    return 0;
}
