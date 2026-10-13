// Setup-mode decisions, tested without a device.
//
// These two functions decide whether a device offers its setup page and when it
// gives up on it. Both are cheap to get wrong in ways a build cannot catch: a
// wrong mode means a device that never joins a network, and a wrong expiry means
// an open access point left running.
#include "av_provision_policy.h"

#include <assert.h>
#include <stdio.h>
#include <string.h>

static void test_device_with_a_network_plays(void)
{
    // The ordinary case: a network is known and there is somewhere to stream
    // from, so play.
    assert(av_boot_mode(true, true) == AV_BOOT_PLAY);
}

static void test_device_with_a_network_but_no_server_plays(void)
{
    // Setup is forced only by a missing network, because it is the only screen
    // reachable without one. With a network the device runs, and the status page
    // -- drawn over playback -- is the way to setup if the address is needed.
    // Forcing it here would also mean a viewer who left was returned at once,
    // leaving the open access point up almost continuously.
    assert(av_boot_mode(true, false) == AV_BOOT_PLAY);
}

static void test_device_with_no_network_offers_setup(void)
{
    // The case this feature exists for: a published image has no credentials
    // compiled in, so it has no network and must ask for one.
    assert(av_boot_mode(false, true) == AV_BOOT_SETUP);
    assert(av_boot_mode(false, false) == AV_BOOT_SETUP);
}

static void test_setup_runs_out_after_its_window(void)
{
    uint32_t start = 1000u;
    assert(!av_setup_expired(start, start, AV_SETUP_WINDOW_MS));
    assert(!av_setup_expired(start, start + AV_SETUP_WINDOW_MS - 1u, AV_SETUP_WINDOW_MS));
    // The boundary counts as expired, so the window is never longer than stated.
    assert(av_setup_expired(start, start + AV_SETUP_WINDOW_MS, AV_SETUP_WINDOW_MS));
    assert(av_setup_expired(start, start + AV_SETUP_WINDOW_MS + 1u, AV_SETUP_WINDOW_MS));
}

static void test_zero_limit_means_no_expiry(void)
{
    assert(!av_setup_expired(0u, 0xFFFFFFFFu, 0u));
}

static void test_setup_expiry_survives_the_counter_wrapping(void)
{
    // The millisecond counter the device passes in wraps about every 49 days.
    // Starting just before the wrap and asking just after it must still measure
    // the elapsed time, not read the wrap as the window having passed.
    uint32_t start = 0xFFFFFFFFu - 1000u;   // 1 s before the wrap
    assert(!av_setup_expired(start, start + 500u, AV_SETUP_WINDOW_MS));
    assert(!av_setup_expired(start, 500u, AV_SETUP_WINDOW_MS));  // wrapped, 1500 ms on
    assert(av_setup_expired(start, AV_SETUP_WINDOW_MS, AV_SETUP_WINDOW_MS));
}

static void test_auto_setup_only_at_boot(void)
{
    // Saved networks, none reachable for the whole wait: open setup.
    assert(!av_boot_auto_setup(true, false, false, AV_BOOT_WIFI_WAIT_MS - 1u));
    assert(av_boot_auto_setup(true, false, false, AV_BOOT_WIFI_WAIT_MS));
    // Online once since boot (a router restart later): keep waiting instead.
    assert(!av_boot_auto_setup(true, true, false, AV_BOOT_WIFI_WAIT_MS * 10u));
    // Once per boot.
    assert(!av_boot_auto_setup(true, false, true, AV_BOOT_WIFI_WAIT_MS * 10u));
    // No network at all is av_boot_mode's case, not this one.
    assert(!av_boot_auto_setup(false, false, false, AV_BOOT_WIFI_WAIT_MS * 10u));
}

static void test_auto_setup_leaves_when_a_network_returns(void)
{
    assert(av_auto_setup_leave(true, true, false, false));
    // Not while someone is using it, and never a setup the viewer asked for.
    assert(!av_auto_setup_leave(true, true, true, false));
    assert(!av_auto_setup_leave(true, true, false, true));
    assert(!av_auto_setup_leave(false, true, false, false));
    assert(!av_auto_setup_leave(true, false, false, false));
}

static void test_setup_pages(void)
{
    av_setup_page_kind_t pages[AV_SETUP_PAGES_MAX];
    // Without sound and help: the three pages as before.
    assert(av_setup_pages(false, false, pages) == 3);
    assert(pages[0] == AV_SETUP_PAGE_JOIN && pages[1] == AV_SETUP_PAGE_OPEN && pages[2] == AV_SETUP_PAGE_STATUS);
    assert(av_setup_pages(false, true, pages) == 4);
    assert(pages[2] == AV_SETUP_PAGE_HELP && pages[3] == AV_SETUP_PAGE_STATUS);
    // Sound is the first page, the hotspot second.
    assert(av_setup_pages(true, true, pages) == 5);
    assert(pages[0] == AV_SETUP_PAGE_SOUND && pages[1] == AV_SETUP_PAGE_JOIN && pages[4] == AV_SETUP_PAGE_STATUS);
    assert(av_setup_pages(true, false, pages) == 4);
}

static void test_site_and_wifi_page(void)
{
    char site[64], page[80];
    assert(av_setup_site_from_url("HTTPS://EXAMPLE.COM/TV/R/ABCDEFGHIJKLMNOPQRST", site, sizeof(site)));
    assert(!strcmp(site, "https://example.com"));
    assert(av_setup_wifi_page(site, page, sizeof(page)) && !strcmp(page, "HTTPS://EXAMPLE.COM/TV/W"));
    assert(av_setup_wifi_page_text(site, page, sizeof(page)) && !strcmp(page, "example.com/TV/W"));
    assert(!av_setup_wifi_page_text("https://", page, sizeof(page)));
    assert(!av_setup_wifi_page_text(site, page, 16));
    assert(av_setup_wifi_page_text(site, page, 17));
    assert(av_setup_site_from_url("http://10.0.0.9:8100", site, sizeof(site)) && !strcmp(site, "http://10.0.0.9:8100"));
    assert(av_setup_site_from_url("HTTPS://EXAMPLE.COM/TV/", site, sizeof(site)) && !strcmp(site, "https://example.com"));
    assert(av_setup_site_from_url("https://a.b?x", site, sizeof(site)) && !strcmp(site, "https://a.b"));
    // Not a site: no scheme, no host, odd characters, does not fit.
    assert(!av_setup_site_from_url("example.com/tv", site, sizeof(site)));
    assert(!av_setup_site_from_url("https:///tv", site, sizeof(site)));
    assert(!av_setup_site_from_url("https://ex ample.com", site, sizeof(site)));
    assert(!av_setup_site_from_url("ftp://example.com", site, sizeof(site)));
    assert(!av_setup_site_from_url("https://example.com", site, 19));
    assert(av_setup_site_from_url("https://example.com", site, 20));
    assert(!av_setup_site_from_url(NULL, site, sizeof(site)));
    assert(!av_setup_wifi_page("", page, sizeof(page)));
    assert(!av_setup_wifi_page("https://example.com", page, 24));
    assert(av_setup_wifi_page("https://example.com", page, 25) && !strcmp(page, "HTTPS://EXAMPLE.COM/TV/W"));
}

static void test_sound_lines(void)
{
    char line[64];
    av_setup_sound_line(AV_SOUND_RECEIVING, 60, line, sizeof(line));
    assert(!strcmp(line, "正在接收… 60%"));
    av_setup_sound_line(AV_SOUND_RECEIVING, 250, line, sizeof(line));
    assert(!strcmp(line, "正在接收… 100%"));
    av_setup_sound_line(AV_SOUND_LISTENING, 0, line, sizeof(line));
    assert(!strcmp(line, "靠近电视点「发送」"));
    av_setup_sound_line(AV_SOUND_OFF, 0, line, sizeof(line));
    assert(!strcmp(line, ""));
    av_setup_sound_line(AV_SOUND_FAILED, 0, line, sizeof(line));
    assert(line[0]);
}

int main(void)
{
    test_device_with_a_network_plays();
    test_device_with_a_network_but_no_server_plays();
    test_device_with_no_network_offers_setup();
    test_setup_runs_out_after_its_window();
    test_zero_limit_means_no_expiry();
    test_setup_expiry_survives_the_counter_wrapping();
    test_auto_setup_only_at_boot();
    test_auto_setup_leaves_when_a_network_returns();
    test_setup_pages();
    test_site_and_wifi_page();
    test_sound_lines();
    printf("test_av_provision_policy: all cases passed\n");
    return 0;
}
