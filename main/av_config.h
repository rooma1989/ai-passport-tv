#pragma once
#if __has_include("sdkconfig.h")
#include "sdkconfig.h"
#endif
// Never put real credentials here. The ignored private header is local only.
#if __has_include("av_private_config.h") && !defined(AV_PUBLIC_BUILD)
#include "av_private_config.h"
#define AV_CONFIG_PRESENT 1
#else
#define AV_CONFIG_PRESENT 0
#define AV_WIFI_SSID ""
#define AV_WIFI_PASSWORD ""
#define AV_SERVER_IPV4 "0.0.0.0"
// The port of the server this build is made for, used when an address names
// none (av_server_addr_use_build_port): Kconfig AV_DEFAULT_SERVER_PORT, 8097
// on the AI Passport and 8098 on the Xingzhi board unless set otherwise.
#if defined(CONFIG_AV_DEFAULT_SERVER_PORT)
#define AV_SERVER_PORT CONFIG_AV_DEFAULT_SERVER_PORT
#else
#define AV_SERVER_PORT 8096
#endif
#define AV_PAIRING_TOKEN ""
#endif
// Where the "how to use" QR code points, on setup page 3 and the status page.
// Empty in a public build: the page and the code are then left out. Upper case
// keeps a short URL inside a version-1 code (see ui_qr.h).
#ifndef AV_HELP_URL
#define AV_HELP_URL ""
#endif
