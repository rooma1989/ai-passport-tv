// Where the streaming server is, as configured rather than compiled in.
//
// The address used to be a build-time constant. That works for the one device
// that was built with it and breaks the moment anything moves: a home router
// hands out a different lease, or a second person wants to point their device at
// their own server, and the only cure is a rebuild and a reflash.
//
// So the address is stored instead, entered on the device's setup page. This
// module turns what was stored into something connectable, and is kept apart
// from the network code so the parsing can be tested without a device.
#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

// Longest stored address accepted, including the terminating byte. Long enough
// for an IPv4 literal with a port, and for a hostname.
#define AV_SERVER_ADDR_MAX 64u

// The port the parser fills in when the text names none: the local media
// server's own default (server/tv_server.py). The firmware does not dial it
// as it is -- av_server_addr_use_build_port() replaces it with the port of the
// server this build is made for (8097 for the AI Passport's relay, 8098 for
// the Xingzhi board's), because a typed domain means that server.
#define AV_SERVER_PORT_DEFAULT 8096u

typedef struct {
    // The host exactly as it was stored: an IPv4 literal or a name to resolve.
    char host[AV_SERVER_ADDR_MAX];
    uint16_t port;
    // Whether the text named the port ("host:port"). False when `port` is
    // AV_SERVER_PORT_DEFAULT filled in by the parser.
    bool port_given;
} av_server_addr_t;

// Parse "host", "host:port" or "host/anything".
//
// Accepts both an IPv4 literal and a hostname; deciding which it is happens when
// the address is used, not here, so this stays free of the network stack.
// Surrounding whitespace is ignored, because the value arrives from a text field
// someone typed into. A path or query is accepted and dropped: people paste a
// whole URL, and the host and port are what matter.
//
// Returns false when there is nothing usable, and leaves *out untouched, so a
// caller can keep a previous good value rather than losing it to a bad edit.
bool av_server_addr_parse(const char *text, av_server_addr_t *out);

// Whether the stored value names something that can be dialled. An empty string,
// whitespace, or a placeholder such as "0.0.0.0" is not usable, and the caller
// shows the setup page instead of trying to connect.
bool av_server_addr_usable(const av_server_addr_t *addr);

// The port to dial, given the port of the server this build is made for
// (AV_SERVER_PORT, see av_config.h). Returns true when it changed `addr`.
//
// - No port in the text: the build's port. "example.com" on an AI Passport is
//   example.com:8097, on the Xingzhi board example.com:8098.
// - An explicit port is kept as typed, with one exception for addresses
//   stored by earlier firmware, which dialled 8096 for a portless value and
//   whose hotspot page suggested ":8096": a public DNS name (a dot, not an
//   IPv4 literal, not ".local") with port 8096 gets the build's port when that
//   differs. 8096 is the local media server's port (server/tv_server.py), which
//   runs on a LAN address or a .local name -- those keep 8096 -- while the
//   public relays listen on 8097 and 8098, never 8096.
bool av_server_addr_use_build_port(av_server_addr_t *addr, uint16_t build_port);
