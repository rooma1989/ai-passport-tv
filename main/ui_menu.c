// strnlen is POSIX, not C. macOS exposes it through <string.h> without being
// asked, glibc only exposes it when a feature-test macro asks for POSIX 2008 --
// so this file built on macOS and failed on the Linux CI runner with
// "implicit declaration of function 'strnlen'". The macro has to come before
// any header is included, which is why it is the first thing in the file.
#define _POSIX_C_SOURCE 200809L

#include "ui_menu.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

// How long the overlay stays up without a gesture. The banner is deliberately
// shorter than the menu: it carries one fact the viewer already knows.
#define UI_MENU_TIMEOUT_MS 12000
#define UI_MENU_BANNER_MS 5000
#define UI_MENU_STATUS_MS 8000
// The backlight page stays up longer than the status page: it is a control the
// viewer is working, so it should not vanish between presses.
#define UI_MENU_BRIGHTNESS_MS 12000
// How long the volume indicator stays up after the last change. Long enough to
// read while the button is held, short enough not to sit over the picture.
#define UI_MENU_VOLUME_MS 1500
// The seek box: up to this long while the picture is away, then half a second
// after it is back (HANDOFF.md 4.5).
#define UI_MENU_HINT_MAX_MS 8000
#define UI_MENU_HINT_AFTER_MS 500

// A channel id must stay printable and short enough for the device table; the
// name is only ever drawn, so it is bounded by the font and the screen.
static bool usable(const ui_menu_entry_t *entry)
{
    size_t id_length = strnlen(entry->id, UI_MENU_ID_MAX);
    if (id_length == 0 || id_length >= UI_MENU_ID_MAX) {
        return false;
    }
    for (size_t i = 0; i < id_length; i++) {
        if (entry->id[i] < '!' || entry->id[i] > '~') {
            return false;
        }
    }
    // A name is required but never a reason to drop the channel: an overlong one
    // is truncated where it is drawn, so only an empty name disqualifies an entry.
    return strnlen(entry->name, UI_MENU_NAME_MAX) > 0;
}

// Keep the highlight inside the window the caller can draw. Without this the
// selection walks off the bottom of the screen and the viewer presses a key
// with nothing visibly happening.
static void follow_selection(ui_menu_t *menu, unsigned rows)
{
    if (rows == 0) {
        return;               // the caller has not said how much fits
    }
    if (menu->selected < menu->scroll) {
        menu->scroll = menu->selected;
    } else if (menu->selected >= menu->scroll + rows) {
        menu->scroll = menu->selected - rows + 1;
    }
}

// ---- groups ----
//
// The entries stay one flat table in the server's order; a level is a filtered
// view of it, worked out on demand. A list is at most AV_CHANNEL_MAX entries and
// a screen shows about eight rows, so scanning is cheaper than keeping a second
// index in step with the first.

static unsigned entry_group(const ui_menu_t *menu, unsigned i)
{
    unsigned g = menu->entries[i].group;
    return g < menu->group_count ? g : 0;   // an unknown group joins the first
}

static bool group_used(const ui_menu_t *menu, unsigned g)
{
    for (unsigned i = 0; i < menu->count; i++) {
        if (entry_group(menu, i) == g) {
            return true;
        }
    }
    return false;
}

static unsigned used_groups(const ui_menu_t *menu)
{
    unsigned used = 0;
    for (unsigned g = 0; g < menu->group_count; g++) {
        used += group_used(menu, g) ? 1u : 0u;
    }
    return used;
}

// Two levels only pay for themselves with something to choose between: with
// one non-empty group the group page would be a single row to press through.
static bool two_levels(const ui_menu_t *menu)
{
    return menu->group_count > 1 && used_groups(menu) > 1;
}

static int group_at_row(const ui_menu_t *menu, unsigned row)
{
    for (unsigned g = 0; g < menu->group_count; g++) {
        if (group_used(menu, g) && row-- == 0) {
            return (int)g;
        }
    }
    return -1;
}

static unsigned row_of_group(const ui_menu_t *menu, unsigned group)
{
    unsigned row = 0;
    for (unsigned g = 0; g < group && g < menu->group_count; g++) {
        row += group_used(menu, g) ? 1u : 0u;
    }
    return row;
}

static int entry_at_row(const ui_menu_t *menu, unsigned row)
{
    if (!two_levels(menu)) {
        return row < menu->count ? (int)row : -1;
    }
    for (unsigned i = 0; i < menu->count; i++) {
        if (entry_group(menu, i) == menu->group && row-- == 0) {
            return (int)i;
        }
    }
    return -1;
}

static unsigned row_of_entry(const ui_menu_t *menu, unsigned index)
{
    if (!two_levels(menu)) {
        return index;
    }
    unsigned row = 0;
    for (unsigned i = 0; i < index; i++) {
        row += entry_group(menu, i) == entry_group(menu, index) ? 1u : 0u;
    }
    return row;
}

static int find_entry(const ui_menu_t *menu, const char *id)
{
    if (!id || !id[0]) {
        return -1;
    }
    for (unsigned i = 0; i < menu->count; i++) {
        if (strcmp(menu->entries[i].id, id) == 0) {
            return (int)i;
        }
    }
    return -1;
}

bool ui_menu_set_groups(ui_menu_t *menu, const char (*names)[UI_MENU_GROUP_NAME_MAX],
                        unsigned count)
{
    if (!menu || count > UI_MENU_GROUP_MAX || (count && !names)) {
        return false;
    }
    memset(menu->groups, 0, sizeof(menu->groups));
    for (unsigned g = 0; g < count; g++) {
        strncpy(menu->groups[g], names[g], UI_MENU_GROUP_NAME_MAX - 1);
    }
    menu->group_count = count;
    if (menu->group >= count) {
        menu->group = 0;
    }
    menu->at_groups = false;
    return true;
}

bool ui_menu_at_groups(const ui_menu_t *menu)
{
    return menu && menu->at_groups;
}

bool ui_menu_in_group(const ui_menu_t *menu)
{
    return menu && !menu->at_groups && two_levels(menu);
}

unsigned ui_menu_row_count(const ui_menu_t *menu)
{
    if (!menu) {
        return 0;
    }
    if (menu->at_groups) {
        return used_groups(menu);
    }
    if (!two_levels(menu)) {
        return menu->count;
    }
    unsigned rows = 0;
    for (unsigned i = 0; i < menu->count; i++) {
        rows += entry_group(menu, i) == menu->group ? 1u : 0u;
    }
    return rows;
}

const char *ui_menu_row_label(const ui_menu_t *menu, unsigned row)
{
    if (!menu) {
        return NULL;
    }
    if (menu->at_groups) {
        int g = group_at_row(menu, row);
        return g < 0 ? NULL : menu->groups[g];
    }
    int i = entry_at_row(menu, row);
    if (i < 0) {
        return NULL;
    }
    return menu->entries[i].name[0] ? menu->entries[i].name : menu->entries[i].id;
}

const char *ui_menu_title(const ui_menu_t *menu)
{
    if (menu && menu->at_groups) {
        return "频道分组";
    }
    if (menu && two_levels(menu)) {
        return menu->groups[menu->group];
    }
    return "频道";
}

unsigned ui_menu_visible_rows(int screen_height, int row_height)
{
    if (screen_height <= 0 || row_height <= 0) {
        return 0;
    }
    unsigned rows = (unsigned)(screen_height / row_height);
    if (rows > UI_MENU_MAX) {
        rows = UI_MENU_MAX;
    }
    return rows;
}

void ui_menu_init(ui_menu_t *menu)
{
    if (!menu) {
        return;
    }
    memset(menu, 0, sizeof(*menu));
    menu->view = UI_VIEW_VIDEO;
}

bool ui_menu_load(ui_menu_t *menu, const ui_menu_entry_t *entries, unsigned count,
                  const char *current)
{
    if (!menu || !entries || count == 0 || count > UI_MENU_MAX) {
        return false;
    }
    // Copy the entries that can be shown and skip the rest. Refusing the whole
    // list because one entry is malformed meant a single odd channel name could
    // disable the menu completely, with only a log line to show for it.
    unsigned kept = 0;
    for (unsigned i = 0; i < count; i++) {
        if (usable(&entries[i])) {
            kept++;
        }
    }
    if (kept == 0) {
        return false;         // nothing usable: keep whatever was there before
    }
    // Build into a local first: a rejected list must leave the previous one in
    // place, or a malformed CONFIG would empty the menu mid-session.
    // Heap, not stack: at up to UI_MENU_MAX entries this is far past the stack
    // of any caller, and a stack copy here is the same overflow that crashed the
    // device inside config_valid. Freed on every path below.
    ui_menu_entry_t *staged = malloc(sizeof(ui_menu_entry_t) * (size_t)kept);
    if (!staged) {
        return false;
    }
    unsigned next = 0;
    for (unsigned i = 0; i < count; i++) {
        if (usable(&entries[i])) {
            staged[next++] = entries[i];
        }
    }
    count = kept;

    // Keep the highlight on the playing channel so opening the list does not
    // silently move it, and keep an existing highlight when the list is
    // otherwise unchanged and still names it.
    unsigned selected = 0;
    bool found = false;
    if (current) {
        for (unsigned i = 0; i < count; i++) {
            if (strcmp(staged[i].id, current) == 0) {
                selected = i;
                found = true;
                break;
            }
        }
    }
    if (!found && menu->count) {
        const char *was = ui_menu_selected(menu);
        if (was) {
            for (unsigned i = 0; i < count; i++) {
                if (strcmp(staged[i].id, was) == 0) {
                    selected = i;
                    found = true;
                    break;
                }
            }
        }
    }

    memcpy(menu->entries, staged, count * sizeof(staged[0]));
    free(staged);
    menu->count = count;
    if (current) {
        strncpy(menu->current, current, UI_MENU_ID_MAX - 1);
        menu->current[UI_MENU_ID_MAX - 1] = '\0';
    }
    // The selection counts rows of the level showing. A list that arrives while
    // the group page is open stays there, on the playing channel's group.
    menu->group = entry_group(menu, selected);
    menu->at_groups = menu->at_groups && two_levels(menu);
    menu->selected = menu->at_groups ? row_of_group(menu, menu->group)
                                     : row_of_entry(menu, selected);
    // Start from the top of the window and pull it to the highlight. The old
    // code only clamped scroll downwards and never advanced it, so once the
    // selection passed the last visible row it drew off-screen.
    menu->scroll = 0;
    follow_selection(menu, menu->visible_rows);
    return true;
}

const char *ui_menu_selected(const ui_menu_t *menu)
{
    if (!menu || menu->count == 0 || menu->at_groups) {
        return NULL;
    }
    int i = entry_at_row(menu, menu->selected);
    return i < 0 ? NULL : menu->entries[i].id;
}

void ui_menu_open(ui_menu_t *menu)
{
    if (!menu || menu->count == 0) {
        return;
    }
    menu->view = UI_VIEW_MENU;
    menu->timeout_ms = UI_MENU_TIMEOUT_MS;
    menu->action = UI_ACTION_NONE;
    if (two_levels(menu)) {
        // Grouped: start on the groups, at the one the viewer is watching.
        int playing = find_entry(menu, menu->current);
        if (playing >= 0) {
            menu->group = entry_group(menu, (unsigned)playing);
        }
        menu->at_groups = true;
        menu->selected = row_of_group(menu, menu->group);
        menu->scroll = 0;
        follow_selection(menu, menu->visible_rows);
    }
    // One level: the highlight stays where it was, as it always has.
}

void ui_menu_back(ui_menu_t *menu)
{
    if (!menu) {
        return;
    }
    if (menu->view == UI_VIEW_MENU && !menu->at_groups && two_levels(menu)) {
        menu->at_groups = true;
        menu->selected = row_of_group(menu, menu->group);
        menu->scroll = 0;
        follow_selection(menu, menu->visible_rows);
        menu->timeout_ms = UI_MENU_TIMEOUT_MS;
        return;
    }
    ui_menu_close(menu);
}

void ui_menu_open_brightness(ui_menu_t *menu)
{
    if (!menu) {
        return;
    }
    menu->view = UI_VIEW_BRIGHTNESS;
    // Same timeout discipline as the status page: the page closes itself, so a
    // viewer who walks away cannot leave the arrows bound to brightness and be
    // unable to change channel.
    menu->timeout_ms = UI_MENU_STATUS_MS;
    menu->action = UI_ACTION_NONE;
}

void ui_menu_keep_open(ui_menu_t *menu)
{
    if (!menu || menu->view == UI_VIEW_VIDEO) {
        return;   // the video view has no page timeout to extend
    }
    // Whichever page is up gets the same treatment as a scroll: the clock
    // restarts, so a viewer mid-adjustment is never cut off.
    menu->timeout_ms = (menu->view == UI_VIEW_BRIGHTNESS) ? UI_MENU_BRIGHTNESS_MS
                                                          : UI_MENU_TIMEOUT_MS;
}

void ui_menu_open_status(ui_menu_t *menu)
{
    if (!menu) {
        return;
    }
    menu->view = UI_VIEW_STATUS;
    menu->timeout_ms = UI_MENU_STATUS_MS;
    menu->action = UI_ACTION_NONE;
}

void ui_menu_close(ui_menu_t *menu)
{
    if (!menu) {
        return;
    }
    menu->view = UI_VIEW_VIDEO;
    menu->timeout_ms = 0;
}

void ui_menu_set_rows(ui_menu_t *menu, unsigned rows)
{
    if (!menu) {
        return;
    }
    menu->visible_rows = rows;
    follow_selection(menu, rows);
}

void ui_menu_step(ui_menu_t *menu, int delta)
{
    ui_menu_scroll_by(menu, delta, menu->visible_rows);
}

void ui_menu_scroll_by(ui_menu_t *menu, int delta, unsigned rows)
{
    if (!menu || menu->view != UI_VIEW_MENU || menu->count == 0 || delta == 0) {
        return;
    }
    if (rows) {
        menu->visible_rows = rows;
    }
    // Wrap around: reaching either end and continuing is more natural than
    // stopping dead, and it is the only way back without holding a key through
    // the whole list.
    int count = (int)ui_menu_row_count(menu);
    if (count == 0) {
        return;
    }
    int next = ((int)menu->selected + delta) % count;
    if (next < 0) {
        next += count;
    }
    menu->selected = (unsigned)next;
    follow_selection(menu, menu->visible_rows);
    menu->timeout_ms = UI_MENU_TIMEOUT_MS;
}

void ui_menu_confirm(ui_menu_t *menu)
{
    if (!menu || menu->view != UI_VIEW_MENU || menu->count == 0) {
        return;
    }
    if (menu->at_groups) {
        // Into the group: on the playing channel if it is in there, else the top.
        int g = group_at_row(menu, menu->selected);
        if (g < 0) {
            return;
        }
        menu->group = (unsigned)g;
        menu->at_groups = false;
        int playing = find_entry(menu, menu->current);
        menu->selected = (playing >= 0 && entry_group(menu, (unsigned)playing) == (unsigned)g)
                             ? row_of_entry(menu, (unsigned)playing) : 0;
        menu->scroll = 0;
        follow_selection(menu, menu->visible_rows);
        menu->timeout_ms = UI_MENU_TIMEOUT_MS;
        return;
    }
    menu->action = UI_ACTION_SWITCH;
}

bool ui_menu_tick(ui_menu_t *menu, int64_t elapsed_ms)
{
    if (!menu || elapsed_ms <= 0) {
        return false;
    }
    bool changed = false;
    if (menu->banner_ms > 0) {
        menu->banner_ms -= elapsed_ms;
        if (menu->banner_ms <= 0) {
            menu->banner_ms = 0;
            changed = true;
        }
    }
    if (menu->volume_ms > 0) {
        menu->volume_ms -= elapsed_ms;
        if (menu->volume_ms <= 0) {
            menu->volume_ms = 0;
            changed = true;
        }
    }
    if (menu->hint_ms > 0) {
        menu->hint_ms -= elapsed_ms;
        if (menu->hint_ms <= 0) {
            menu->hint_ms = 0;
            menu->hint = UI_HINT_NONE;
            menu->hint_seq++;
            changed = true;
        }
    }
    if (menu->view != UI_VIEW_VIDEO && menu->timeout_ms > 0) {
        menu->timeout_ms -= elapsed_ms;
        if (menu->timeout_ms <= 0) {
            menu->timeout_ms = 0;
            ui_menu_close(menu);
            changed = true;
        }
    }
    return changed;
}

ui_action_t ui_menu_take_action(ui_menu_t *menu, char *out, size_t out_size)
{
    if (!menu) {
        return UI_ACTION_NONE;
    }
    ui_action_t action = menu->action;
    menu->action = UI_ACTION_NONE;
    if (out && out_size) {
        out[0] = '\0';
    }
    if (action == UI_ACTION_SWITCH && out && out_size) {
        const char *id = ui_menu_selected(menu);
        if (!id || strnlen(id, UI_MENU_ID_MAX) >= out_size) {
            return UI_ACTION_NONE;  // cannot name it, so do not pretend to
        }
        memcpy(out, id, strlen(id) + 1);
        ui_menu_close(menu);  // the list has done its job
    }
    return action;
}

void ui_menu_show_volume(ui_menu_t *menu, unsigned level)
{
    if (!menu) {
        return;
    }
    menu->volume = level > 100u ? 100u : level;
    menu->volume_ms = UI_MENU_VOLUME_MS;
    menu->level = UI_LEVEL_VOLUME;
}

void ui_menu_show_brightness(ui_menu_t *menu)
{
    if (!menu) {
        return;
    }
    menu->volume_ms = UI_MENU_VOLUME_MS;
    menu->level = UI_LEVEL_BRIGHTNESS;
}

void ui_menu_banner_place(ui_menu_t *menu, const char *id)
{
    if (!menu) {
        return;
    }
    menu->banner_number = 0;
    menu->banner_detail[0] = '\0';
    int found = find_entry(menu, id);
    if (found < 0) {
        return;
    }
    const unsigned g = entry_group(menu, (unsigned)found);
    unsigned position = 0, total = 0;
    for (unsigned i = 0; i < menu->count; i++) {
        if (entry_group(menu, i) != g) {
            continue;
        }
        total++;
        if (i <= (unsigned)found) {
            position++;
        }
    }
    menu->banner_number = (unsigned)found + 1u;
    // One level only: the group is the whole list, named as the list is.
    const char *group = two_levels(menu) ? menu->groups[g] : "频道";
    snprintf(menu->banner_detail, sizeof(menu->banner_detail), "%s \xC2\xB7 %u/%u",
             group, position, total);
}

void ui_menu_show_hint(ui_menu_t *menu, ui_hint_t hint, int seconds)
{
    if (!menu || hint == UI_HINT_NONE) {
        return;
    }
    menu->hint = hint;
    menu->hint_seconds = seconds;
    menu->hint_ms = UI_MENU_HINT_MAX_MS;
    menu->hint_seq++;
}

void ui_menu_hint_resumed(ui_menu_t *menu)
{
    if (menu && menu->hint != UI_HINT_NONE && menu->hint_ms > UI_MENU_HINT_AFTER_MS) {
        menu->hint_ms = UI_MENU_HINT_AFTER_MS;
    }
}

void ui_menu_hint_text(const ui_menu_t *menu, char *out, size_t out_size)
{
    if (!out || !out_size) {
        return;
    }
    out[0] = '\0';
    if (!menu || menu->hint == UI_HINT_NONE) {
        return;
    }
    if (menu->hint == UI_HINT_NEXT) {
        snprintf(out, out_size, "下一个");
        return;
    }
    const char *verb = menu->hint == UI_HINT_BACK ? "快退" : "快进";
    unsigned seconds = (unsigned)abs(menu->hint_seconds);
    if (seconds == 0) {
        snprintf(out, out_size, "%s", verb);
    } else if (seconds < 60u) {
        snprintf(out, out_size, "%s%u秒", verb, seconds);
    } else {
        snprintf(out, out_size, "%s%u分", verb, (seconds + 30u) / 60u);
    }
}

bool ui_menu_volume_visible(const ui_menu_t *menu)
{
    return menu && menu->volume_ms > 0;
}

void ui_menu_banner(ui_menu_t *menu, const char *name, const char *previous)
{
    if (!menu || !name) {
        return;
    }
    // Nothing changed, so a banner would only cover the picture to repeat what
    // the viewer already had: a single-channel list, or a reconnect that landed
    // on the same channel.
    if (previous && strcmp(name, previous) == 0) {
        return;
    }
    strncpy(menu->banner, name, sizeof(menu->banner) - 1);
    menu->banner[sizeof(menu->banner) - 1] = '\0';
    menu->banner_ms = UI_MENU_BANNER_MS;
}

