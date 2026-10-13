// The smallest mbedTLS configuration main/av_wifi_crypto.c needs, for building
// the library from ESP-IDF's sources on the host (tests/test_av_wifi_crypto.c).
// The firmware uses ESP-IDF's own configuration, which enables all of these.
#pragma once
#define MBEDTLS_AES_C
#define MBEDTLS_GCM_C
#define MBEDTLS_CIPHER_C
#define MBEDTLS_BIGNUM_C
#define MBEDTLS_ECP_C
#define MBEDTLS_ECP_DP_CURVE25519_ENABLED
#define MBEDTLS_MD_C
#define MBEDTLS_SHA256_C
