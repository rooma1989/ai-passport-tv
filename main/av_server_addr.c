#include "av_server_addr.h"

#include <string.h>

// Whitespace at either end is dropped before anything else: the value comes from
// a text field, and a trailing space after a pasted address is a mistake the
// reader should not have to notice.
static const char *skip_space(const char *p)
{
    while (*p == ' ' || *p == '\t' || *p == '\r' || *p == '\n') {
        p++;
    }
    return p;
}

bool av_server_addr_parse(const char *text, av_server_addr_t *out)
{
    if (!text || !out) {
        return false;
    }

    const char *begin = skip_space(text);
    const char *end = begin + strlen(begin);
    while (end > begin && (end[-1] == ' ' || end[-1] == '\t' ||
                           end[-1] == '\r' || end[-1] == '\n')) {
        end--;
    }
    if (end == begin) {
        return false;   // blank field
    }

    // The scheme is stripped BEFORE anything is cut at a slash.
    //
    // The other order looks equivalent and is not: "http://host:8096/live" would
    // first be cut at the first '/', leaving "http:", and the address would be
    // lost before the scheme was ever recognised. That is the shape a person
    // pastes most often, so the order here is the part that matters.
    const char *host_begin = begin;
    for (const char *p = begin; p + 2 < end; p++) {
        if (p[0] == ':' && p[1] == '/' && p[2] == '/') {
            host_begin = p + 3;
            break;
        }
    }

    // From here on, a '/' or '?' ends the address: what follows is a path that
    // has nothing to do with where to connect.
    const char *stop = host_begin;
    while (stop < end && *stop != '/' && *stop != '?') {
        stop++;
    }
    size_t length = (size_t)(stop - host_begin);
    if (length == 0) {
        return false;
    }

    // A port, when present, is separated by the last ':' of what is left. Only
    // the final one introduces the port; an earlier colon would be part of a
    // scheme, which has already been removed.
    const char *colon = NULL;
    for (const char *p = host_begin; p < host_begin + length; p++) {
        if (*p == ':') {
            colon = p;
        }
    }

    uint16_t port = AV_SERVER_PORT_DEFAULT;
    bool port_given = false;
    size_t host_length = length;
    if (colon) {
        host_length = (size_t)(colon - host_begin);
        const char *digits = colon + 1;
        size_t count = (size_t)((host_begin + length) - digits);
        // A colon with nothing usable after it is not a port. Treated as an
        // error rather than ignored, because "host:" is far more likely to be a
        // typo than an address whose port was meant to be omitted.
        if (count == 0 || count > 5) {
            return false;
        }
        unsigned value = 0;
        for (size_t i = 0; i < count; i++) {
            char c = digits[i];
            if (c < '0' || c > '9') {
                return false;
            }
            value = value * 10u + (unsigned)(c - '0');
        }
        // Zero is not a port anything listens on; above 65535 does not fit.
        if (value == 0 || value > 65535u) {
            return false;
        }
        port = (uint16_t)value;
        port_given = true;
    }

    if (host_length == 0 || host_length >= AV_SERVER_ADDR_MAX) {
        return false;   // nothing left, or too long to store
    }

    av_server_addr_t parsed;
    memcpy(parsed.host, host_begin, host_length);
    parsed.host[host_length] = '\0';
    parsed.port = port;
    parsed.port_given = port_given;

    // Written only now, so a rejected value leaves the caller's copy alone.
    *out = parsed;
    return true;
}

bool av_server_addr_usable(const av_server_addr_t *addr)
{
    if (!addr || addr->host[0] == '\0') {
        return false;
    }
    // The build-time placeholder means "not configured". Anything else is taken
    // at face value: whether it resolves is the network stack's business, and
    // refusing a name here would defeat the point of accepting one.
    return strcmp(addr->host, "0.0.0.0") != 0;
}

// A name a public DNS server answers for: has a dot, is not an IPv4 literal
// (digits and dots only) and is not a multicast-DNS ".local" name.
static bool public_name(const char *host)
{
    bool dot = false, letter = false;
    for (const char *p = host; *p; p++) {
        if (*p == '.') dot = true;
        else if (*p < '0' || *p > '9') letter = true;
    }
    if (!dot || !letter) return false;
    const size_t n = strlen(host);
    static const char local[] = ".local";
    const size_t k = sizeof(local) - 1u;
    size_t end = n;
    if (end && host[end - 1] == '.') end--;   // "printer.local." is the same name
    if (end >= k) {
        bool same = true;
        for (size_t i = 0; i < k; i++) {
            char c = host[end - k + i];
            if (c >= 'A' && c <= 'Z') c = (char)(c - 'A' + 'a');
            if (c != local[i]) { same = false; break; }
        }
        if (same) return false;
    }
    return true;
}

bool av_server_addr_use_build_port(av_server_addr_t *addr, uint16_t build_port)
{
    if (!addr || build_port == 0 || addr->port == build_port) {
        return false;
    }
    const bool legacy = addr->port_given && addr->port == AV_SERVER_PORT_DEFAULT &&
                        public_name(addr->host);
    if (addr->port_given && !legacy) {
        return false;
    }
    addr->port = build_port;
    return true;
}
