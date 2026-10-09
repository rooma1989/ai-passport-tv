#pragma once
// Never put real credentials here. The ignored private header is local only.
#if __has_include("av_private_config.h") && !defined(AV_PUBLIC_BUILD)
#include "av_private_config.h"
#define AV_CONFIG_PRESENT 1
#else
#define AV_CONFIG_PRESENT 0
#define AV_WIFI_SSID ""
#define AV_WIFI_PASSWORD ""
#define AV_SERVER_IPV4 "0.0.0.0"
#define AV_SERVER_PORT 8096
#define AV_PAIRING_TOKEN ""
#endif
// Where the "how to use" QR code points, on setup page 3 and the status page.
// Empty in a public build: the page and the code are then left out. Upper case
// keeps a short URL inside a version-1 code (see ui_qr.h).
#ifndef AV_HELP_URL
#define AV_HELP_URL ""
#endif
