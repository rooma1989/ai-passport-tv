// Overlay menu and banner state. Pure logic: no ESP-IDF, no drawing, no keys.
// The caller feeds it gestures and reads back what should be on screen, which
// keeps the behaviour testable on the development machine.
#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

// The menu holds as many channels as CONFIG may carry. A smaller number here was
// left behind when the device limit rose, and ui_menu_load then rejected the
// whole list, which silently disabled the menu.
#include "av_protocol.h"
#define UI_MENU_MAX AV_CHANNEL_MAX
// 16 glyphs of 16 pixels already overflow a 320-pixel row, so this is well past
// what can be shown; a longer name is drawn truncated, never refused.
#define UI_MENU_NAME_MAX 48u
#define UI_MENU_ID_MAX 16u
// Channel groups sent by the server (live TV split by kind, creators, the cast).
// The server's list has nine; the room left is for a server that grows it.
#define UI_MENU_GROUP_MAX 16u
#define UI_MENU_GROUP_NAME_MAX 24u

typedef enum {
    UI_VIEW_VIDEO = 0,   // playing; a banner may still be up
    UI_VIEW_MENU,        // the channel list is open
    UI_VIEW_STATUS,      // the status page is up
    UI_VIEW_BRIGHTNESS,  // the backlight level is being set
    UI_VIEW_PET,         // choosing the pet, from the status page
} ui_view_t;

// What the overlay wants the caller to do next.
typedef enum {
    UI_ACTION_NONE = 0,
    UI_ACTION_SWITCH,    // switch to ui_menu_selected()
    UI_ACTION_CLOSE,     // leave the overlay and resume the session
    UI_ACTION_PET,       // save menu->pet_current as the pet
} ui_action_t;

typedef struct {
    char id[UI_MENU_ID_MAX];
    char name[UI_MENU_NAME_MAX];
    // Index into the group names; one past the last means the first group.
    uint8_t group;
} ui_menu_entry_t;

// What the level indicator at the top of the picture is showing.
typedef enum {
    UI_LEVEL_VOLUME = 0,
    UI_LEVEL_BRIGHTNESS,   // set from the phone; the keys use the brightness page
} ui_level_t;

// The box in the middle of the picture after a seek or "next" from the phone.
typedef enum {
    UI_HINT_NONE = 0,
    UI_HINT_FORWARD,
    UI_HINT_BACK,
    UI_HINT_NEXT,
} ui_hint_t;

typedef struct {
    ui_view_t view;
    ui_menu_entry_t entries[UI_MENU_MAX];
    unsigned count;
    // The list has two levels when the channels fall into more than one group:
    // the groups, then one group's channels. `selected` and `scroll` count rows
    // of whichever level is showing.
    char groups[UI_MENU_GROUP_MAX][UI_MENU_GROUP_NAME_MAX];
    unsigned group_count;
    bool at_groups;
    unsigned group;       // the group whose channels are (or were last) shown
    char current[UI_MENU_ID_MAX]; // the channel playing, as last loaded
    unsigned selected;
    unsigned scroll;      // index of the first visible row
    unsigned visible_rows; // rows the caller can draw; 0 means it has not said
    // Milliseconds left before the overlay closes itself; 0 means "no timeout".
    int64_t timeout_ms;
    // Set when the user picked an entry, cleared by ui_menu_take_action().
    ui_action_t action;
    // A banner shown over the video after a switch, and its remaining time.
    char banner[UI_MENU_NAME_MAX];
    int64_t banner_ms;
    // Where that channel sits, for the banner: its number in the whole list
    // (0 when unknown) and "group · n/total" ("" when unknown).
    unsigned banner_number;
    char banner_detail[UI_MENU_GROUP_NAME_MAX + 24];
    // The volume indicator shown while the volume is being changed, and its
    // remaining time. It carries the level rather than reading it back, so the
    // display shows what was asked for at the moment it was asked for.
    unsigned volume;
    int64_t volume_ms;
    // Which level the indicator shows; the brightness comes from `brightness`.
    ui_level_t level;
    // The seek box: what was asked, the seconds for a seek, how long it may
    // stay, and a count that moves on every new one so a still screen knows to
    // repaint.
    ui_hint_t hint;
    int hint_seconds;
    int64_t hint_ms;
    unsigned hint_seq;
    // The pet picker: the pet in use, the one being looked at, how many there
    // are, and whether OK has just chosen (the page then stays a second).
    unsigned pet_current;
    unsigned pet_browse;
    unsigned pet_count;
    bool pet_chosen;
    // Which backlight step is in use, as an index into AV_BRIGHTNESS_LEVELS.
    // The menu carries it so the brightness page can be drawn from the same
    // state the key handler updates, with no second source of truth.
    unsigned brightness;
} ui_menu_t;

// Rows the list can show at once, given the screen height and row height.
unsigned ui_menu_visible_rows(int screen_height, int row_height);

void ui_menu_init(ui_menu_t *menu);
// Replace the channel list. `current` marks which id is playing now; the
// selection starts there, so opening the menu does not move the highlight.
// Returns false and leaves the menu untouched if any entry is unusable.
bool ui_menu_load(ui_menu_t *menu, const ui_menu_entry_t *entries, unsigned count,
                  const char *current);
// Set the group names the entries' `group` fields index. Call before
// ui_menu_load. Zero names (or one) gives a one-level list, as before groups.
// Returns false, changing nothing, for more than UI_MENU_GROUP_MAX names.
bool ui_menu_set_groups(ui_menu_t *menu, const char (*names)[UI_MENU_GROUP_NAME_MAX],
                        unsigned count);
// Open the list: on the groups, highlighting the playing channel's group, when
// there is more than one non-empty group; otherwise on the channels. No effect
// when the list is empty.
void ui_menu_open(ui_menu_t *menu);
// Long press on OK: from a group's channels back to the groups; from the
// groups, or a one-level list, close.
void ui_menu_back(ui_menu_t *menu);
// What the list is showing, for drawing: whether it is the group level, how
// many rows the level has, each row's text, and the header title.
bool ui_menu_at_groups(const ui_menu_t *menu);
// True while one group's channels are showing, so OK long press goes back.
bool ui_menu_in_group(const ui_menu_t *menu);
// What a row of the showing level is: a group index at the groups level, an
// entry index otherwise; -1 for a row past the end or of the other level.
int ui_menu_row_group(const ui_menu_t *menu, unsigned row);
int ui_menu_row_entry(const ui_menu_t *menu, unsigned row);
// Channels in group `g`.
unsigned ui_menu_group_size(const ui_menu_t *menu, unsigned g);
// The playing channel's entry and group, or -1 when it is not in the list.
int ui_menu_playing_entry(const ui_menu_t *menu);
int ui_menu_playing_group(const ui_menu_t *menu);
unsigned ui_menu_row_count(const ui_menu_t *menu);
const char *ui_menu_row_label(const ui_menu_t *menu, unsigned row);
const char *ui_menu_title(const ui_menu_t *menu);
// Open the status page.
void ui_menu_open_status(ui_menu_t *menu);
// Open the backlight page. Unlike the list this needs no channel data, so it
// opens on a device that has not received a channel list yet.
void ui_menu_open_brightness(ui_menu_t *menu);
// Restart the current page's timeout. Used by a page whose viewer is still
// adjusting something, so the page does not close under their hand: the caller
// keeps the timer in the module that owns it rather than writing the field.
void ui_menu_keep_open(ui_menu_t *menu);
void ui_menu_close(ui_menu_t *menu);

// One gesture. `delta` is +1 for down and -1 for up; ignored outside list views.
void ui_menu_step(ui_menu_t *menu, int delta);
// The same, but told how many rows are on screen, so the highlight can be kept
// in view. Scrolling cannot be done without that number.
void ui_menu_scroll_by(ui_menu_t *menu, int delta, unsigned rows);
// Tell the menu how many rows it may show, so it can keep the highlight visible.
void ui_menu_set_rows(ui_menu_t *menu, unsigned rows);
// Confirm the highlighted row: on a group, open its channels; on a channel, ask
// for a switch.
void ui_menu_confirm(ui_menu_t *menu);

// Advance timers. Returns true when something changed and the screen must be
// redrawn; the caller passes the real elapsed milliseconds.
bool ui_menu_tick(ui_menu_t *menu, int64_t elapsed_ms);

// The id the caller should switch to, read once. Clears the pending action.
ui_action_t ui_menu_take_action(ui_menu_t *menu, char *out, size_t out_size);

// Show the banner naming the channel that is now playing, if it differs from
// `previous`. Called after a switch completes.
void ui_menu_banner(ui_menu_t *menu, const char *name, const char *previous);

// Show the volume indicator at `level` percent and restart its timeout. Every
// press calls this, so holding the button keeps the display up instead of
// letting it expire mid-press.
void ui_menu_show_volume(ui_menu_t *menu, unsigned level);

// Whether the volume indicator should be drawn now.
bool ui_menu_volume_visible(const ui_menu_t *menu);

// Open the pet picker on `current`, one of `count` pets.
void ui_menu_open_pets(ui_menu_t *menu, unsigned current, unsigned count);
// Look at the previous (delta < 0) or next pet, wrapping.
void ui_menu_pet_step(ui_menu_t *menu, int delta);
// Choose the pet being looked at: UI_ACTION_PET is raised, and the page shows
// it in use for a second before it closes.
void ui_menu_pet_confirm(ui_menu_t *menu);

// Show the backlight in the same indicator, as the phone set it. Whichever of
// the two came last is the one shown.
void ui_menu_show_brightness(ui_menu_t *menu);

// Fill banner_number and banner_detail for channel `id`, from the loaded list.
// `id` not in the list leaves both empty.
void ui_menu_banner_place(ui_menu_t *menu, const char *id);
// The same for any caller: `number` 0 and `detail` "" when `id` is not listed.
void ui_menu_channel_place(const ui_menu_t *menu, const char *id, unsigned *number,
                           char *detail, size_t detail_size);

// Show the seek box. `seconds` is the jump, negative for back; ignored for
// "next". The box stays until the picture comes back (ui_menu_hint_resumed) or
// UI_MENU_HINT_MAX_MS passes, whichever is first.
void ui_menu_show_hint(ui_menu_t *menu, ui_hint_t hint, int seconds);
// The picture came back after stopping: the box stays half a second more.
void ui_menu_hint_resumed(ui_menu_t *menu);
// The box's first line: 快进30秒, 快退2分, 下一个. Empty when no box is up.
void ui_menu_hint_text(const ui_menu_t *menu, char *out, size_t out_size);


// The channel id currently highlighted, or NULL when the list is empty or the
// group level is showing.
const char *ui_menu_selected(const ui_menu_t *menu);
