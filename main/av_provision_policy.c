#include "av_provision_policy.h"

#include <stdio.h>
#include <string.h>

av_boot_mode_t av_boot_mode(bool has_network, bool server_configured)
{
    // Only a missing network forces setup at start-up. The reason is that setup
    // is the only screen reachable without a network: the way back to it is the
    // status page, which is drawn over playback, and playback cannot start
    // without a network. A device that came up here and left setup without a
    // network would have no way to return to it.
    //
    // A missing server address is different. The device can join its network and
    // run; the session fails to connect, and the waiting screen it leaves behind
    // still owns the panel that the status page is drawn over. So the viewer can
    // open the status page and reach setup from there, whenever they choose.
    // Forcing setup in that case would also mean a viewer who left it was pushed
    // straight back, with an open access point up almost continuously.
    (void)server_configured;
    return has_network ? AV_BOOT_PLAY : AV_BOOT_SETUP;
}

bool av_setup_expired(uint32_t started_ms, uint32_t now_ms, uint32_t limit_ms)
{
    if (limit_ms == 0) {
        return false;
    }
    // Unsigned subtraction, so this stays correct across the millisecond counter
    // wrapping, which happens about every 49 days. Comparing the timestamps
    // directly would make a wrapped counter look like it had run out.
    return (uint32_t)(now_ms - started_ms) >= limit_ms;
}

bool av_boot_auto_setup(bool has_network, bool ever_connected, bool already_opened,
                        uint32_t waited_ms)
{
    // Without a network setup opens straight away (av_boot_mode), so this is
    // only about saved networks that are all out of reach.
    return has_network && !ever_connected && !already_opened && waited_ms >= AV_BOOT_WIFI_WAIT_MS;
}

bool av_auto_setup_leave(bool automatic, bool saved_network_visible, bool phone_on_hotspot,
                         bool sound_busy)
{
    // A setup the viewer asked for stays until they leave it or it times out.
    return automatic && saved_network_visible && !phone_on_hotspot && !sound_busy;
}

unsigned av_setup_pages(bool sound, bool help, av_setup_page_kind_t out[AV_SETUP_PAGES_MAX])
{
    unsigned n = 0;
    if (sound) out[n++] = AV_SETUP_PAGE_SOUND;
    out[n++] = AV_SETUP_PAGE_JOIN;
    out[n++] = AV_SETUP_PAGE_OPEN;
    if (help) out[n++] = AV_SETUP_PAGE_HELP;
    out[n++] = AV_SETUP_PAGE_STATUS;
    return n;
}

static char lower(char c)
{
    return (c >= 'A' && c <= 'Z') ? (char)(c - 'A' + 'a') : c;
}

bool av_setup_site_from_url(const char *url, char *site, size_t size)
{
    if (!url || !site || size == 0) return false;
    site[0] = '\0';
    size_t scheme;
    char head[9] = { 0 };
    for (size_t i = 0; i < 8 && url[i]; i++) head[i] = lower(url[i]);
    if (!strncmp(head, "https://", 8)) scheme = 8;
    else if (!strncmp(head, "http://", 7)) scheme = 7;
    else return false;
    size_t end = scheme;
    while (url[end] && url[end] != '/' && url[end] != '?' && url[end] != '#') {
        const char c = url[end];
        // Host names, an IPv4 literal, a port: nothing that needs escaping.
        if (!((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9') ||
              c == '.' || c == '-' || c == ':')) {
            return false;
        }
        end++;
    }
    if (end == scheme || end + 1 > size) return false;
    for (size_t i = 0; i < end; i++) site[i] = lower(url[i]);
    site[end] = '\0';
    return true;
}

bool av_setup_wifi_page(const char *site, char *out, size_t size)
{
    if (!site || !site[0] || !out) return false;
    const int n = snprintf(out, size, "%s/TV/W", site);
    if (n <= 0 || (size_t)n >= size) return false;
    for (char *c = out; *c; c++) {
        if (*c >= 'a' && *c <= 'z') *c = (char)(*c - 'a' + 'A');
    }
    return true;
}

bool av_setup_wifi_page_text(const char *site, char *out, size_t size)
{
    if (!site || !out) return false;
    const char *host = strstr(site, "://");
    host = host ? host + 3 : site;
    if (!host[0]) return false;
    const int n = snprintf(out, size, "%s/TV/W", host);
    return n > 0 && (size_t)n < size;
}

void av_setup_sound_line(av_sound_state_t state, unsigned progress, char *out, size_t size)
{
    if (!out || size == 0) return;
    if (progress > 100u) progress = 100u;
    switch (state) {
    case AV_SOUND_RECEIVING:
        snprintf(out, size, "正在接收… %u%%", progress);
        break;
    case AV_SOUND_CONNECTING:
        snprintf(out, size, "收到了，正在连接…");
        break;
    case AV_SOUND_FAILED:
        snprintf(out, size, "没连上，检查密码再发");
        break;
    case AV_SOUND_DONE:
        snprintf(out, size, "已连上，马上重启");
        break;
    case AV_SOUND_LISTENING:
        snprintf(out, size, "靠近电视点「发送」");
        break;
    default:
        out[0] = '\0';
        break;
    }
}
