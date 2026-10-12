// What is drawn over the picture while it plays (HANDOFF.md 4.4-4.6): the
// level box, the channel banner, the seek box and the pause band. Each draws
// only into the rows of `surface` it covers and only when the menu state says
// it is up, so the player calls them for every stripe.
//
// No ESP-IDF dependencies: tools/render_ui.c draws them on the host to check
// the layout against the design.
#pragma once

#include "ui_menu.h"
#include "ui_text.h"

// The volume (or, from the phone, the backlight at `brightness_percent`) across
// y=30..74.
void ui_overlay_level(ui_surface_t surface, const ui_menu_t *menu, unsigned brightness_percent);

// The channel banner across y=160..209: number at 2x, name, "group · n/total".
void ui_overlay_banner(ui_surface_t surface, const ui_menu_t *menu);

// The seek box at (60,92), 120x56.
void ui_overlay_hint(ui_surface_t surface, const ui_menu_t *menu);

// The loading band across y=84..155 while the source changes (next, previous,
// an episode picked): the "next" mark, 正在加载 and 第 n/N 集, the title.
void ui_overlay_loading(ui_surface_t surface, const ui_menu_t *menu);

// The pause band over rows [top, bottom): `pet` dozing, the pause mark, 已暂停
// and the video's title.
void ui_overlay_pause(ui_surface_t surface, int top, int bottom, unsigned pet, const char *title);

// The bottom bar while paused, rows [top, bottom): how to carry on.
void ui_overlay_pause_hint(ui_surface_t surface, int top, int bottom);

// ---- Menus and pages (HANDOFF.md 4.7-4.10), drawn on the opaque panel ----

// Rows of the list on screen at once.
#define UI_OVERLAY_LIST_ROWS 7u

// A hint line centred across the panel with its glyphs from row `y`: the key
// names (▲ ▼ OK) in UI_AMBER, the rest in UI_MUTED.
void ui_overlay_key_hint(ui_surface_t surface, int y, const char *text);
// The same from `x`, on `background`; returns the x past it.
int ui_overlay_key_line(ui_surface_t surface, int x, int y, const char *text, uint16_t background);

// The channel list, either level: rows from y=36, the scroll bar at x=232.
void ui_overlay_list(ui_surface_t surface, const ui_menu_t *menu);

// The backlight page: the sun, the percentage at 3x, ten cells, the keys.
void ui_overlay_brightness(ui_surface_t surface, unsigned percent);

// What the status page shows; the player fills it once a second.
typedef struct {
    char number[4];       // "07", or "" when the channel is not in the list
    char name[64];
    unsigned bars;        // 0..4; 0 reads "--"
    int rssi;             // dBm, shown beside the signal; 0 when unknown
    int soc;              // < 0 reads "--"
    unsigned pet;
    const uint8_t *qr;    // the remote's (or the help page's) code, or NULL
    const char *qr_caption;
    bool audio_only;      // which way OK switches, for the bottom line
} ui_status_page_t;

void ui_overlay_status(ui_surface_t surface, const ui_status_page_t *page);

// The bottom bar's contents, rows [top, top + 30): the list's frame and
// receive rates, or one centred line.
void ui_overlay_bottom_rates(ui_surface_t surface, int top, unsigned fps_x10, unsigned kbytes_per_s);
void ui_overlay_bottom_text(ui_surface_t surface, int top, const char *text, uint16_t color);
// The status page's bottom line: what OK does there, and that the rest close.
void ui_overlay_status_bottom(ui_surface_t surface, int top, bool audio_only);

// ---- Voice screens, in the list's panel (voice control design, mock-ups 1-12) ----
//
// The header takes rows 36-87 (the microphone, then the loudness bars or the
// words heard and a status line), a dotted rule sits at y=90, and up to
// UI_VOICE_ROWS_SHOWN rows of 24 px start at y=96.

// The voice screen `menu->voice.screen`, on the opaque panel. Nothing for
// UI_VOICE_NONE (the list is drawn by ui_overlay_list).
void ui_overlay_voice(ui_surface_t surface, const ui_menu_t *menu);

// The top bar's label for a voice screen: up to three coloured runs into
// `texts`/`colors`, using `scratch` for a count. Returns how many.
unsigned ui_overlay_voice_label(const ui_menu_t *menu, const char *texts[3], uint16_t colors[3],
                                char *scratch, size_t scratch_size);

// The bottom bar's line while the list or a voice screen is up and voice is
// on: how to talk, finish, cancel or go back. ▲ ▼ OK in amber.
const char *ui_overlay_voice_hint(const ui_menu_t *menu);
void ui_overlay_bottom_keys(ui_surface_t surface, int top, const char *text);

// ---- Waiting screens (HANDOFF.md 4.2, 4.3) and the pet picker (4.15) ----

typedef enum {
    UI_WAIT_WIFI = 0,   // joining the Wi-Fi network
    UI_WAIT_SERVER,     // first dial to the server since boot
    UI_WAIT_CHANNEL,    // opening a channel, or the picture stopped
    UI_WAIT_FAILED,     // the server did not answer
    UI_WAIT_LISTEN,     // audio-only: sound is playing, the pet keeps it company
} ui_wait_kind_t;

typedef struct {
    ui_wait_kind_t kind;
    unsigned pet;
    char line[64];      // the network's name, or the server's address
    unsigned number;    // the channel's number in the list; 0 when unknown
    char name[64];      // the channel's name; "" when unknown
    char detail[48];    // "group · n/total"
    // A relay channel's video (UI_WAIT_CHANNEL): its title and "第 n/N 集", in
    // place of `detail` and after the verb; `loading` says 正在加载 (the source
    // is changing in a session) rather than 正在打开. Empty: as before.
    char title[64];
    char episode[24];
    bool loading;
    // The listening screen's live inputs, refreshed before each frame:
    // loudness now and over the last UI_LISTEN_BARS chunks (oldest first),
    // 0..255; whether the screen has dimmed (the pet sleeps); and tenths of
    // a second since it woke (it hops when that is small).
    uint8_t level;
    uint8_t history[12];
    bool asleep;
    unsigned since_wake;
} ui_wait_page_t;

#define UI_LISTEN_BARS 12u

// The top bar's label for a waiting screen: 小智电视, or 换台中.
const char *ui_overlay_wait_label(ui_wait_kind_t kind);

// The picture area of a waiting screen, rows 30..209, with its animation at
// `tick` (tenths of a second): the pet bobs a pixel every half second, the
// Wi-Fi dots take turns, the block runs between pet and globe, the bar sweeps.
void ui_overlay_wait(ui_surface_t surface, const ui_wait_page_t *page, unsigned tick);

// The rows the animation changes, and a number that changes exactly when
// what is drawn in them does: repaint [*y0, *y1) when it moves.
unsigned ui_overlay_wait_frame(ui_wait_kind_t kind, unsigned tick, int *y0, int *y1);

// The pet picker on the opaque panel; its bottom bar line is
// ui_overlay_pet_bottom.
void ui_overlay_pets(ui_surface_t surface, const ui_menu_t *menu);
void ui_overlay_pet_bottom(ui_surface_t surface, int top);

// ---- Setup pages (HANDOFF.md 4.11-4.14), whole screen ----

typedef struct {
    unsigned index, count;  // this page, 0-based, of `count`
    bool status;            // the 4/4 status page rather than a code page
    const char *title;
    const uint8_t *qr;      // a code page's code, or NULL
    const char *line1;
    const char *line2;
    uint16_t line2_color;
    unsigned pet, mood;
    // The status page's rows.
    const char *ssid, *page_url, *server;
    bool client_ready;
    unsigned remaining_s;   // of the setup window
} ui_setup_page_t;

// The whole page, top bar to bottom bar. `blink_on` is the status page's
// waiting square in its lit second.
void ui_overlay_setup(ui_surface_t surface, const ui_setup_page_t *page, bool blink_on);

// The rows the status page changes each second: the waiting square and the
// countdown.
#define UI_SETUP_TICK_Y0 112
#define UI_SETUP_TICK_Y1 208
