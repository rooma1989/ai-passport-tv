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
    UI_LEVEL_BRIGHTNESS,   // set from the phone
    UI_LEVEL_BRIGHTNESS_KEYS,  // stepped with the keys (the Passport's double-clicked arrows)
} ui_level_t;

// The box in the middle of the picture after a seek or "next" from the phone.
typedef enum {
    UI_HINT_NONE = 0,
    UI_HINT_FORWARD,
    UI_HINT_BACK,
    UI_HINT_NEXT,
} ui_hint_t;

// What a relay channel (a series, a creator, the cast) is playing or about to
// play, as a server that knows says (HELLO "info": 1; CONFIG "playing", SWITCH
// "loading"). An empty title and episode 0 is "nothing known": a live channel.
// The title is UTF-8, cut on a character, at most UI_EPISODE_TITLE_MAX - 1 bytes.
#define UI_EPISODE_TITLE_MAX 64u
typedef struct {
    char title[UI_EPISODE_TITLE_MAX];
    unsigned episode;   // 1-based; 0 when not a series
    unsigned total;     // episodes in the series; 0 when unknown
} ui_episode_t;

// How far a relay channel's video has played, from a server told "progress": 1
// (SWITCH "progress"). The server says once where the video is at a moment of
// this session's sound clock -- the audio PTS the device plays, in ms -- and the
// device moves the bar on with that clock, so nothing is sent per second. All
// times are milliseconds. Kept by av_player.c under its list lock: the receive
// task writes it, the video task reads it.
typedef struct {
    bool known;            // a length and an anchor are known
    uint32_t dur_ms;       // the video's length, > 0
    uint32_t pos_ms;       // where the video is ...
    uint32_t at_ms;        // ... when the sound clock reads this
    bool stopped;          // the source stopped (a stall, a pause) when the
    uint32_t stop_ms;      // clock read this: the bar stays there
    bool seeking;          // a seek was asked: the bar shows seek_ms, still,
    uint32_t seek_ms;      // until the new source's anchor (or
    uint32_t seek_at_ms;   // UI_PROGRESS_SEEK_MS of clock after the ask)
} ui_progress_t;

// ---- Voice: the push-to-talk screens share the list's panel ("2-in-1") ----
//
// A sub-state of UI_VIEW_MENU: while `voice.screen` is not UI_VOICE_NONE the
// panel shows the voice header and its rows instead of the channel list, which
// stays where it was underneath. The voice policy (av_voice_policy.c) decides
// which screen; this only holds what is drawn.
#define UI_VOICE_ROWS_MAX 7u     // six candidates and "none of these, open the list"
#define UI_VOICE_ROWS_SHOWN 4u   // rows below the voice header
#define UI_VOICE_HEARD_MAX 96u
#define UI_VOICE_STATUS_MAX 64u
#define UI_VOICE_BARS 12u

typedef enum {
    UI_VOICE_NONE = 0,        // the channel list (or nothing)
    UI_VOICE_LISTENING,       // red dot, microphone, live loudness, examples, ten cells
    UI_VOICE_RECOGNISING,     // how long was recorded, "recognising"
    UI_VOICE_DONE,            // the words heard, what was done, the matched row
    UI_VOICE_CHOICES,         // the words heard, candidates to pick from
    UI_VOICE_NOT_UNDERSTOOD,  // the words heard, "didn't catch that", examples
    UI_VOICE_UNAVAILABLE,     // no session to talk over, and how to reach setup
    UI_VOICE_SETUP,           // asks before entering setup
    UI_VOICE_ERROR,           // nothing came back
} ui_voice_screen_t;

typedef struct {
    char label[UI_MENU_NAME_MAX];
    char lead[4];                       // "05" in the number column, or "" for the icon
    char right[UI_MENU_GROUP_NAME_MAX]; // muted text at the right edge, or ""
    uint8_t icon;                       // a ui_icon_t, drawn when `lead` is empty
    bool muted;                         // an example or a way out, not a result
} ui_voice_row_t;

typedef struct {
    ui_voice_screen_t screen;
    // Whether the channel list was open under the voice screens, so going back
    // returns to it as it was rather than reopening it at the groups.
    bool over_list;
    char heard[UI_VOICE_HEARD_MAX];    // what the server heard, without brackets
    char status[UI_VOICE_STATUS_MAX];  // the line under it: a reply, or the state
    uint8_t levels[UI_VOICE_BARS];     // listening: loudness, oldest first, 0..255
    unsigned elapsed_ds;               // listening: tenths of a second so far (ten cells = 10 s)
    unsigned recorded_ds;              // recognising: tenths of a second recorded
    ui_voice_row_t rows[UI_VOICE_ROWS_MAX];
    unsigned row_count;
    bool highlight;                    // a row is highlighted (candidates, setup, the match)
    unsigned selected;
    unsigned scroll;                   // first row on screen
} ui_voice_t;

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
    // The voice screens, drawn in the list's panel (see ui_voice_t).
    ui_voice_t voice;
    // What this session plays (for the banner and the opening screen), and
    // what is loading while the source changes: the band over the picture,
    // then the waiting screen. loading_ms counts down while it is up;
    // loading_done says the server has the new source and its picture is next,
    // so the band is no longer drawn over pictures (the first new one is a
    // whole frame and must not carry it). info_seq moves on every change so a
    // still screen knows to repaint.
    ui_episode_t playing;
    ui_episode_t loading;
    int64_t loading_ms;
    bool loading_done;
    unsigned info_seq;
    // How long the played and total times stay under the picture after a key,
    // a seek, a pause, a new source (they are also up with the banner).
    int64_t progress_ms;
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
// Show `pet` as just chosen for two seconds -- the phone's choice, already
// stored -- without raising an action.
void ui_menu_show_pet(ui_menu_t *menu, unsigned pet, unsigned count);
// Look at the previous (delta < 0) or next pet, wrapping.
void ui_menu_pet_step(ui_menu_t *menu, int delta);
// Choose the pet being looked at: UI_ACTION_PET is raised, and the page shows
// it in use for a second before it closes.
void ui_menu_pet_confirm(ui_menu_t *menu);

// Show the backlight in the same indicator, as the phone set it. Whichever of
// the two came last is the one shown.
void ui_menu_show_brightness(ui_menu_t *menu);
// The same, stepped on the device itself.
void ui_menu_show_brightness_keys(ui_menu_t *menu);

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


// ---- Episode information ----

// Whether `episode` says anything: a title or an episode number.
bool ui_episode_known(const ui_episode_t *episode);
// Copy `title` into `episode->title`, cut on a whole UTF-8 character.
void ui_episode_set_title(ui_episode_t *episode, const char *title);
// "第 3/26 集", "第 3 集" without a total, "" when not a series.
void ui_episode_text(const ui_episode_t *episode, char *out, size_t out_size);
// What the session plays; NULL forgets it (a live channel, a new session).
void ui_menu_set_playing(ui_menu_t *menu, const ui_episode_t *episode);
// The source is changing to `episode`: the loading band goes up for at most
// UI_MENU_LOADING_MAX_MS, and a "next" box it makes redundant goes.
void ui_menu_show_loading(ui_menu_t *menu, const ui_episode_t *episode);
// The server has the new source and its picture comes next: what loads becomes
// what plays. The band stops being drawn over pictures; the waiting screen keeps
// it until ui_menu_loading_shown, or UI_MENU_LOADING_AFTER_MS at most. False
// (nothing changed) when nothing was loading.
bool ui_menu_loading_done(ui_menu_t *menu);
// A picture was drawn after ui_menu_loading_done: the loading is over.
void ui_menu_loading_shown(ui_menu_t *menu);
// Whether the band is drawn over the picture, and whether anything is loading.
bool ui_menu_loading_band(const ui_menu_t *menu);
bool ui_menu_loading_up(const ui_menu_t *menu);

// ---- Playback progress ----

// Nothing known: a live channel, a new session, the source changing.
void ui_progress_forget(ui_progress_t *progress);
// The server's anchor: the video, `dur_ms` long, is at `pos_ms` when the sound
// clock reads `at_ms`; with `stopped`, its source stopped when the clock read
// `stop_ms`. A running anchor ends a seek's preview; a stopped one (the old
// source cut by the seek) leaves it. False, changing nothing, for a length of 0.
bool ui_progress_anchor(ui_progress_t *progress, uint32_t dur_ms, uint32_t pos_ms, uint32_t at_ms,
                        bool stopped, uint32_t stop_ms);
// The phone or a voice command moved the video by `seconds` (SWITCH "seek"),
// asked when the clock read `clock_ms`: the bar goes there at once and stays
// until the new source's anchor. Nothing when nothing is known.
void ui_progress_seek(ui_progress_t *progress, int seconds, uint32_t clock_ms);
// Where the video is when the sound clock reads `clock_ms`; false when nothing
// is known.
bool ui_progress_position(const ui_progress_t *progress, uint32_t clock_ms, uint32_t *pos_ms);
// How many of `width` pixels are played.
unsigned ui_progress_fill(uint32_t pos_ms, uint32_t dur_ms, unsigned width);
// The played and total times, "12:34" and "23:40"; both "H:MM:SS" when the
// video is an hour or longer ("0:12:34", "1:02:34").
void ui_progress_times(uint32_t pos_ms, uint32_t dur_ms, char *played, char *total, size_t size);
// Keep the times up for UI_MENU_PROGRESS_MS (a key, a seek, a pause, a new source).
void ui_menu_show_progress(ui_menu_t *menu);
// Whether the times are up over a picture: with the banner, or after the above.
bool ui_menu_progress_label(const ui_menu_t *menu);

// ---- Voice screens ----

// Show `screen` in the panel. From the picture the panel opens; over the list
// the list is kept underneath. The voice policy owns the timing, so the panel
// has no timeout of its own while a voice screen is up. Rows are cleared when
// the screen kind changes; the caller fills `voice` after this.
void ui_menu_voice_show(ui_menu_t *menu, ui_voice_screen_t screen);
// Whether a voice screen (not the list) is in the panel.
bool ui_menu_voice_up(const ui_menu_t *menu);
// Back from the voice screens to the channel list: as it was when the list was
// underneath, opened afresh (at the groups) when the voice panel came from the
// picture. No list loaded: the panel closes.
void ui_menu_voice_to_list(ui_menu_t *menu);
// Drop the voice screen without touching the view.
void ui_menu_voice_clear(ui_menu_t *menu);
// Highlight row `row` of the voice rows, scrolling to keep it on screen.
void ui_menu_voice_select(ui_menu_t *menu, unsigned row);

// The channel id currently highlighted, or NULL when the list is empty or the
// group level is showing.
const char *ui_menu_selected(const ui_menu_t *menu);
