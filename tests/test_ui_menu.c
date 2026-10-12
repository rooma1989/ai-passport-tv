// Host tests for overlay menu state: selection, scrolling, timeouts and the
// banner rules. Pure logic, so no device is needed.
#include "ui_menu.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static int failures;

static void check(int condition, const char *what)
{
    if (!condition) {
        printf("FAIL: %s\n", what);
        failures++;
    }
}

static ui_menu_entry_t ENTRIES[] = {
    { "cctv1", "CCTV1", 0 },
    { "cctv5", "CCTV5", 0 },
    { "cctv9", "CCTV9", 0 },
    { "cgtn", "CGTN", 0 },
    { "dongfang", "Dongfang", 0 },
};
#define ENTRY_COUNT (sizeof(ENTRIES) / sizeof(ENTRIES[0]))

static ui_menu_t loaded(const char *current)
{
    ui_menu_t menu;
    ui_menu_init(&menu);
    check(ui_menu_load(&menu, ENTRIES, (unsigned)ENTRY_COUNT, current),
          "the sample list loads");
    return menu;
}

static void test_open_selects_the_playing_channel(void)
{
    ui_menu_t menu = loaded("cctv9");
    check(menu.selected == 2, "the highlight starts on the channel playing now");

    // Opening must not move the highlight; the list opens where the viewer is.
    ui_menu_open(&menu);
    check(menu.view == UI_VIEW_MENU, "opening switches to the list view");
    check(strcmp(ui_menu_selected(&menu), "cctv9") == 0, "the playing channel stays selected");
}

static void test_stepping_wraps_and_keeps_the_selection_sane(void)
{
    ui_menu_t menu = loaded("cctv1");
    ui_menu_open(&menu);

    ui_menu_step(&menu, 1);
    check(strcmp(ui_menu_selected(&menu), "cctv5") == 0, "down moves to the next channel");
    ui_menu_step(&menu, -1);
    check(strcmp(ui_menu_selected(&menu), "cctv1") == 0, "up moves back");

    // Past the start it wraps to the end rather than sticking.
    ui_menu_step(&menu, -1);
    check(strcmp(ui_menu_selected(&menu), "dongfang") == 0, "up from the first wraps to the last");

    // And past the end back to the start.
    for (unsigned i = 0; i < ENTRY_COUNT; i++) {
        ui_menu_step(&menu, 1);
    }
    check(strcmp(ui_menu_selected(&menu), "dongfang") == 0,
          "a full cycle returns to the same entry");
}

static void test_confirm_reports_the_highlighted_channel(void)
{
    ui_menu_t menu = loaded("cctv1");
    ui_menu_open(&menu);
    ui_menu_step(&menu, 1);
    ui_menu_step(&menu, 1);
    ui_menu_confirm(&menu);

    char id[UI_MENU_ID_MAX] = "";
    ui_action_t action = ui_menu_take_action(&menu, id, sizeof(id));
    check(action == UI_ACTION_SWITCH, "confirming asks for a switch");
    check(strcmp(id, "cctv9") == 0, "the switch names the highlighted channel");
    check(menu.view == UI_VIEW_VIDEO, "the list closes after the choice");

    // The action is consumed exactly once, so a switch cannot repeat.
    check(ui_menu_take_action(&menu, id, sizeof(id)) == UI_ACTION_NONE,
          "the action is cleared after it is read");
}

static void test_menu_times_out_but_not_the_video(void)
{
    ui_menu_t menu = loaded("cctv1");
    ui_menu_open(&menu);
    check(!ui_menu_tick(&menu, 100), "a short delay changes nothing");
    check(ui_menu_tick(&menu, 12000), "the menu closes itself after its timeout");
    check(menu.view == UI_VIEW_VIDEO, "the timeout returns to the video");

    // In the video view there is nothing to time out, so ticks report no change.
    check(!ui_menu_tick(&menu, 60000), "the video view never times out");
}

static void test_banner_only_when_the_channel_actually_changed(void)
{
    ui_menu_t menu = loaded("cctv1");
    ui_menu_banner(&menu, "CCTV5", "CCTV1");
    check(menu.banner_ms > 0, "a real change raises the banner");
    check(strcmp(menu.banner, "CCTV5") == 0, "the banner names the new channel");

    ui_menu_t same = loaded("cctv1");
    ui_menu_banner(&same, "CCTV1", "CCTV1");
    check(same.banner_ms == 0, "reconnecting to the same channel raises no banner");

    // The banner clears itself and reports the change.
    check(ui_menu_tick(&menu, 5000), "the banner expires");
    check(menu.banner_ms == 0, "the banner is gone after its time");
    check(menu.view == UI_VIEW_VIDEO, "the banner does not disturb the video view");
}

static void test_unusable_entries_are_skipped_not_fatal(void)
{
    ui_menu_t menu = loaded("cctv1");

    // A malformed entry must not cost the viewer the whole menu: the rest of the
    // list is still usable, and refusing everything was how one odd channel name
    // could disable the list entirely.
    ui_menu_entry_t mixed[3] = { { "ok1", "Fine", 0 }, { "has space", "Broken", 0 },
                                 { "ok2", "Also fine", 0 } };
    check(ui_menu_load(&menu, mixed, 3, "ok1"), "a list with one bad entry still loads");
    check(menu.count == 2, "only the usable entries are kept");
    check(strcmp(ui_menu_selected(&menu), "ok1") == 0, "the highlight follows the current id");

    // A 16-byte id is one the device table cannot hold, so it is dropped.
    ui_menu_entry_t too_long[2] = { { "0123456789abcdef", "Sixteen", 0 }, { "ok", "Fine", 0 } };
    check(ui_menu_load(&menu, too_long, 2, NULL), "a list with only that id plus a good one loads");
    check(menu.count == 1 && strcmp(ui_menu_selected(&menu), "ok") == 0,
          "the unusable id is dropped and the good one kept");

    // An empty name disqualifies an entry; an overlong one is only truncated
    // where it is drawn, so it stays.
    ui_menu_entry_t empty_name[2] = { { "ok", "", 0 }, { "also", "Kept", 0 } };
    check(ui_menu_load(&menu, empty_name, 2, NULL), "an empty name drops just that entry");
    check(menu.count == 1 && strcmp(ui_menu_selected(&menu), "also") == 0,
          "the entry without a name is the one that goes");

    // Nothing usable at all: keep what was already there rather than empty it.
    unsigned before = menu.count;
    ui_menu_entry_t all_bad[1] = { { "has space", "Broken", 0 } };
    check(!ui_menu_load(&menu, all_bad, 1, NULL), "a wholly unusable list is refused");
    check(menu.count == before, "a refused list leaves the previous one intact");

    check(!ui_menu_load(&menu, ENTRIES, 0, NULL), "an empty list is refused");
}

static void test_list_capacity_is_enforced(void)
{
    ui_menu_t menu;
    ui_menu_init(&menu);
    // Heap, not a stack array: at UI_MENU_MAX entries this is already far larger
    // than a stack should carry, which is the mistake this whole round is fixing.
    ui_menu_entry_t *big = malloc(sizeof(ui_menu_entry_t) * (UI_MENU_MAX + 1));
    check(big != NULL, "the oversized list could be built");
    if (!big) {
        return;
    }
    for (unsigned i = 0; i <= UI_MENU_MAX; i++) {
        snprintf(big[i].id, UI_MENU_ID_MAX, "c%u", i);
        snprintf(big[i].name, UI_MENU_NAME_MAX, "Channel %u", i);
    }
    check(!ui_menu_load(&menu, big, UI_MENU_MAX + 1, NULL),
          "more channels than the device accepts is refused");
    free(big);
}

static void test_a_full_channel_list_loads(void)
{
    // The case that actually matters: as many channels as the device accepts
    // must fit, because that is what the server sends.
    ui_menu_entry_t *many = malloc(sizeof(ui_menu_entry_t) * UI_MENU_MAX);
    check(many != NULL, "the full-size list could be built");
    if (!many) {
        return;
    }
    for (unsigned i = 0; i < UI_MENU_MAX; i++) {
        snprintf(many[i].id, UI_MENU_ID_MAX, "c%u", i);
        // A long Chinese name, as the real channel table has.
        snprintf(many[i].name, UI_MENU_NAME_MAX, "\u6d4b\u8bd5\u9891\u9053%u", i);
    }
    ui_menu_t menu;
    ui_menu_init(&menu);
    check(ui_menu_load(&menu, many, UI_MENU_MAX, "c0"),
          "a list of exactly the maximum size loads");
    check(menu.count == UI_MENU_MAX, "every entry is kept");
    free(many);
}

static void test_a_channel_missing_from_the_list_is_handled(void)
{
    // The playing channel is not in the new list, e.g. after a source change.
    ui_menu_t menu = loaded("removed");
    check(menu.selected == 0, "an unknown current channel falls back to the top");
    check(strcmp(ui_menu_selected(&menu), "cctv1") == 0, "and the top entry is selected");

    // A later load keeps the highlight on the same channel when it survives.
    ui_menu_t kept = loaded("cctv9");
    ui_menu_load(&kept, ENTRIES, (unsigned)ENTRY_COUNT, NULL);
    check(strcmp(ui_menu_selected(&kept), "cctv9") == 0,
          "a reload without a current channel keeps the highlight");
}

static void test_open_with_no_list_does_nothing(void)
{
    ui_menu_t menu;
    ui_menu_init(&menu);
    ui_menu_open(&menu);
    check(menu.view == UI_VIEW_VIDEO, "an empty list cannot be opened");
    ui_menu_step(&menu, 1);
    ui_menu_confirm(&menu);
    char id[UI_MENU_ID_MAX];
    check(ui_menu_take_action(&menu, id, sizeof(id)) == UI_ACTION_NONE,
          "an empty list produces no action");
}

static void test_scrolling_keeps_the_highlight_on_screen(void)
{
    ui_menu_t menu = loaded("cctv1");
    ui_menu_set_rows(&menu, 3);      // a three-row window over five entries
    ui_menu_open(&menu);

    ui_menu_step(&menu, 1);
    check(menu.scroll == 0 && menu.selected == 1, "moving inside the window does not scroll");

    ui_menu_step(&menu, 1);
    ui_menu_step(&menu, 1);
    check(menu.selected == 3, "the highlight reaches the fourth entry");
    // Without this the selection would sit off the bottom of a three-row window.
    check(menu.scroll == 1, "the window follows the highlight down");
    check(menu.selected >= menu.scroll && menu.selected < menu.scroll + 3,
          "the highlight stays inside the visible window");

    // And the same going back up.
    for (int i = 0; i < 4; i++) {
        ui_menu_step(&menu, -1);
    }
    check(menu.selected < menu.scroll + 3 && menu.selected >= menu.scroll,
          "the window follows the highlight up as well");

    // A window that was never declared must not scroll into nonsense.
    ui_menu_t blank = loaded("cctv1");
    ui_menu_open(&blank);
    ui_menu_step(&blank, 1);
    check(blank.scroll == 0, "an undeclared window leaves the scroll at the top");
}

static void test_confirm_then_take_yields_the_id_once(void)
{
    // The exact order the firmware uses: a click sets the action, the key pump
    // collects it in the same pass. Pressing OK did nothing on the device, so
    // this covers the hand-off rather than the state change alone.
    ui_menu_t menu = loaded("cctv1");
    ui_menu_set_rows(&menu, 8);
    ui_menu_open(&menu);
    ui_menu_scroll_by(&menu, 1, 8);
    ui_menu_scroll_by(&menu, 1, 8);
    ui_menu_confirm(&menu);

    char id[UI_MENU_ID_MAX] = "";
    ui_action_t first = ui_menu_take_action(&menu, id, sizeof(id));
    check(first == UI_ACTION_SWITCH, "the click is collected as a switch");
    check(strcmp(id, "cctv9") == 0, "the switch carries the highlighted id");
    check(menu.view == UI_VIEW_VIDEO, "choosing closes the list");

    // And it must not fire twice: a duplicate switch would reconnect to the
    // same channel and look like the button had done nothing.
    char again[UI_MENU_ID_MAX] = "";
    check(ui_menu_take_action(&menu, again, sizeof(again)) == UI_ACTION_NONE,
          "a second read yields nothing");
    check(again[0] == '\0', "the second read leaves the buffer empty");
}

static void test_confirm_survives_a_long_list_and_scrolling(void)
{
    // The device case: many channels, the highlight deep in the list, confirmed.
    const unsigned count = UI_MENU_MAX;
    ui_menu_entry_t *many = malloc(sizeof(ui_menu_entry_t) * count);
    check(many != NULL, "the long list could be built");
    if (!many) {
        return;
    }
    for (unsigned i = 0; i < count; i++) {
        snprintf(many[i].id, UI_MENU_ID_MAX, "c%u", i);
        snprintf(many[i].name, UI_MENU_NAME_MAX, "\u9891\u9053%u", i);
    }
    ui_menu_t menu;
    ui_menu_init(&menu);
    check(ui_menu_load(&menu, many, count, "c0"), "the long list loads");
    ui_menu_set_rows(&menu, 8);
    ui_menu_open(&menu);

    // Walk to entry 40 and confirm; the id must survive the scroll arithmetic.
    for (int i = 0; i < 40; i++) {
        ui_menu_scroll_by(&menu, 1, 8);
    }
    check(menu.selected == 40, "the highlight reached the fortieth entry");
    check(menu.scroll == 33, "the window scrolled to keep it visible");
    ui_menu_confirm(&menu);

    char id[UI_MENU_ID_MAX] = "";
    check(ui_menu_take_action(&menu, id, sizeof(id)) == UI_ACTION_SWITCH,
          "a deep entry can still be confirmed");
    check(strcmp(id, "c40") == 0, "the id is the fortieth entry");
    free(many);
}

static void test_visible_rows_never_exceeds_the_list(void)
{
    check(ui_menu_visible_rows(240, 20) == 12, "rows fit the screen height");
    check(ui_menu_visible_rows(240, 0) == 0, "a zero row height is rejected");
    // A window taller than the list must still not claim more rows than there
    // are entries, or the drawing loop would walk past the end.
    check(ui_menu_visible_rows((int)UI_MENU_MAX * 20 + 100, 20) == UI_MENU_MAX,
          "the row count never exceeds the list capacity");
}

// ---- two levels: groups, then the group's channels ----

static const char GROUP_NAMES[][UI_MENU_GROUP_NAME_MAX] = { "投屏", "UP 主", "央视", "空组" };

static ui_menu_entry_t GROUPED[] = {
    { "cast", "我的投屏", 0 },
    { "up01", "小Lin说", 1 },
    { "up02", "老高", 1 },
    { "ch000", "CCTV1", 2 },
    { "ch001", "CCTV2", 2 },
    { "ch002", "CCTV3", 2 },
};
#define GROUPED_COUNT (sizeof(GROUPED) / sizeof(GROUPED[0]))

static ui_menu_t grouped(const char *current)
{
    ui_menu_t menu;
    ui_menu_init(&menu);
    check(ui_menu_set_groups(&menu, GROUP_NAMES, 4), "group names load");
    check(ui_menu_load(&menu, GROUPED, (unsigned)GROUPED_COUNT, current), "the grouped list loads");
    ui_menu_set_rows(&menu, 8);
    return menu;
}

static void test_groups_open_on_the_playing_group(void)
{
    ui_menu_t menu = grouped("ch001");
    ui_menu_open(&menu);
    check(ui_menu_at_groups(&menu), "a grouped list opens on the group level");
    check(ui_menu_row_count(&menu) == 3, "only groups with channels are listed");
    check(strcmp(ui_menu_row_label(&menu, 2), "央视") == 0, "rows are the group names");
    check(menu.selected == 2, "the highlight starts on the playing channel's group");
    check(strcmp(ui_menu_title(&menu), "频道分组") == 0, "the group level is titled");
    check(ui_menu_selected(&menu) == NULL, "no channel is chosen on the group level");
}

static void test_confirm_enters_the_group_and_back_returns(void)
{
    ui_menu_t menu = grouped("ch001");
    ui_menu_open(&menu);
    ui_menu_confirm(&menu);
    check(!ui_menu_at_groups(&menu), "OK on a group opens its channels");
    check(menu.action == UI_ACTION_NONE, "entering a group switches nothing");
    check(ui_menu_row_count(&menu) == 3, "the level lists that group only");
    check(strcmp(ui_menu_title(&menu), "央视") == 0, "the channel level is titled with the group");
    check(strcmp(ui_menu_selected(&menu), "ch001") == 0, "the playing channel is highlighted");
    ui_menu_step(&menu, 1);
    ui_menu_step(&menu, 1);
    check(strcmp(ui_menu_selected(&menu), "ch000") == 0, "stepping wraps inside the group");

    ui_menu_back(&menu);
    check(menu.view == UI_VIEW_MENU && ui_menu_at_groups(&menu), "back returns to the groups");
    check(menu.selected == 2, "on the group just left");
    ui_menu_step(&menu, -1);
    ui_menu_confirm(&menu);
    check(strcmp(ui_menu_selected(&menu), "up01") == 0,
          "another group opens on its first channel when nothing in it is playing");
    ui_menu_confirm(&menu);
    char id[UI_MENU_ID_MAX];
    check(ui_menu_take_action(&menu, id, sizeof(id)) == UI_ACTION_SWITCH && strcmp(id, "up01") == 0,
          "OK on a channel asks for the switch");

    ui_menu_back(&menu);
    ui_menu_back(&menu);
    check(menu.view == UI_VIEW_VIDEO, "back from the group level closes the list");
}

static void test_without_groups_the_list_is_one_level(void)
{
    ui_menu_t menu = loaded("cctv9");
    ui_menu_open(&menu);
    check(!ui_menu_at_groups(&menu), "no groups: straight to the channels");
    check(ui_menu_row_count(&menu) == ENTRY_COUNT, "every channel is a row");
    check(strcmp(ui_menu_title(&menu), "频道") == 0, "titled as a plain list");
    ui_menu_back(&menu);
    check(menu.view == UI_VIEW_VIDEO, "back closes a one-level list");

    ui_menu_t one;
    ui_menu_init(&one);
    check(ui_menu_set_groups(&one, GROUP_NAMES, 1), "a single group loads");
    check(ui_menu_load(&one, ENTRIES, (unsigned)ENTRY_COUNT, "cctv1"), "list loads");
    ui_menu_open(&one);
    check(!ui_menu_at_groups(&one), "a single group skips the group level too");
}

static void test_group_names_are_bounded(void)
{
    ui_menu_t menu;
    ui_menu_init(&menu);
    char many[UI_MENU_GROUP_MAX + 1][UI_MENU_GROUP_NAME_MAX];
    memset(many, 0, sizeof(many));
    for (unsigned i = 0; i < UI_MENU_GROUP_MAX + 1; i++) snprintf(many[i], sizeof(many[i]), "g%u", i);
    check(!ui_menu_set_groups(&menu, (const char (*)[UI_MENU_GROUP_NAME_MAX])many, UI_MENU_GROUP_MAX + 1),
          "too many groups are refused");
    check(ui_menu_set_groups(&menu, NULL, 0), "clearing the groups is allowed");
    // An entry naming a group that does not exist is shown under the first.
    static ui_menu_entry_t stray[] = { { "a", "A", 0 }, { "b", "B", 9 } };
    check(ui_menu_set_groups(&menu, GROUP_NAMES, 2), "two groups");
    check(ui_menu_load(&menu, stray, 2, "a"), "loads");
    ui_menu_open(&menu);
    check(!ui_menu_at_groups(&menu) && ui_menu_row_count(&menu) == 2,
          "the stray entry joins the first group, and one non-empty group needs no group level");
}

static void test_row_queries(void)
{
    ui_menu_t menu = grouped("up02");
    ui_menu_open(&menu);
    check(ui_menu_at_groups(&menu), "opens on the groups");
    check(ui_menu_row_group(&menu, 0) == 0 && ui_menu_row_group(&menu, 2) == 2 &&
          ui_menu_row_group(&menu, 3) == -1, "rows name the used groups only");
    check(ui_menu_row_entry(&menu, 0) == -1, "no entries at the groups level");
    check(ui_menu_group_size(&menu, 1) == 2 && ui_menu_group_size(&menu, 2) == 3 &&
          ui_menu_group_size(&menu, 3) == 0, "group sizes");
    check(ui_menu_playing_entry(&menu) == 2 && ui_menu_playing_group(&menu) == 1, "what is playing");
    ui_menu_confirm(&menu);   // into UP 主, on the playing channel
    check(ui_menu_row_entry(&menu, 0) == 1 && ui_menu_row_entry(&menu, 1) == 2 &&
          ui_menu_row_entry(&menu, 2) == -1, "rows name the group's entries");
}

// ---- the banner's place, the level indicator, the seek box ----

static void test_banner_place(void)
{
    ui_menu_t menu = grouped("ch001");
    ui_menu_banner_place(&menu, "ch001");
    check(menu.banner_number == 5, "the number counts the whole list");
    check(strcmp(menu.banner_detail, "央视 \xC2\xB7 2/3") == 0, "group, then place in the group");
    ui_menu_banner_place(&menu, "nope");
    check(menu.banner_number == 0 && menu.banner_detail[0] == '\0', "unknown id: nothing");
    ui_menu_t flat = loaded("cctv9");
    ui_menu_banner_place(&flat, "cctv9");
    check(flat.banner_number == 3 && strcmp(flat.banner_detail, "频道 \xC2\xB7 3/5") == 0,
          "one level: the list is the group");
}

static void test_level_indicator(void)
{
    ui_menu_t menu = loaded("cctv1");
    ui_menu_show_volume(&menu, 70);
    check(ui_menu_volume_visible(&menu) && menu.level == UI_LEVEL_VOLUME, "volume shows");
    ui_menu_show_brightness(&menu);
    check(ui_menu_volume_visible(&menu) && menu.level == UI_LEVEL_BRIGHTNESS, "brightness takes over");
    ui_menu_show_brightness_keys(&menu);
    check(ui_menu_volume_visible(&menu) && menu.level == UI_LEVEL_BRIGHTNESS_KEYS,
          "brightness stepped on the device shows in the same indicator");
    check(ui_menu_tick(&menu, 1500) && !ui_menu_volume_visible(&menu), "and goes after 1.5 s");
}

static void test_seek_hint(void)
{
    ui_menu_t menu = loaded("cctv1");
    char text[32];
    ui_menu_hint_text(&menu, text, sizeof(text));
    check(text[0] == '\0', "no box, no text");
    const unsigned seq = menu.hint_seq;
    ui_menu_show_hint(&menu, UI_HINT_FORWARD, 30);
    ui_menu_hint_text(&menu, text, sizeof(text));
    check(strcmp(text, "快进30秒") == 0 && menu.hint_seq != seq, "forward 30 s");
    ui_menu_show_hint(&menu, UI_HINT_BACK, -150);
    ui_menu_hint_text(&menu, text, sizeof(text));
    check(strcmp(text, "快退3分") == 0, "long jumps in minutes");
    ui_menu_show_hint(&menu, UI_HINT_NEXT, 0);
    ui_menu_hint_text(&menu, text, sizeof(text));
    check(strcmp(text, "下一个") == 0, "next");
    check(!ui_menu_tick(&menu, 3000) && menu.hint == UI_HINT_NEXT, "stays while the picture is away");
    ui_menu_hint_resumed(&menu);
    check(!ui_menu_tick(&menu, 400), "half a second more after it is back");
    check(ui_menu_tick(&menu, 200) && menu.hint == UI_HINT_NONE, "then it goes");
    ui_menu_show_hint(&menu, UI_HINT_FORWARD, 30);
    check(ui_menu_tick(&menu, 8000) && menu.hint == UI_HINT_NONE, "and never longer than 8 s");
}

static void test_pet_picker(void)
{
    ui_menu_t menu = loaded("cctv1");
    ui_menu_open_pets(&menu, 3, 11);
    check(menu.view == UI_VIEW_PET && menu.pet_browse == 3, "opens on the pet in use");
    ui_menu_pet_step(&menu, -1);
    ui_menu_pet_step(&menu, -1);
    ui_menu_pet_step(&menu, -1);
    ui_menu_pet_step(&menu, -1);
    check(menu.pet_browse == 10, "wraps backwards");
    ui_menu_pet_step(&menu, 1);
    check(menu.pet_browse == 0, "and forwards");
    check(!ui_menu_tick(&menu, 19000) && menu.view == UI_VIEW_PET, "stays while looked at");
    ui_menu_pet_step(&menu, 1);
    check(!ui_menu_tick(&menu, 19000), "a step restarts the clock");
    ui_menu_pet_confirm(&menu);
    char out[8];
    check(ui_menu_take_action(&menu, out, sizeof(out)) == UI_ACTION_PET && menu.pet_current == 1,
          "OK chooses the pet looked at");
    ui_menu_pet_step(&menu, 1);
    check(menu.pet_browse == 1, "no browsing once chosen");
    check(!ui_menu_tick(&menu, 900) && ui_menu_tick(&menu, 200) && menu.view == UI_VIEW_VIDEO,
          "shown a second, then closed");
    ui_menu_show_pet(&menu, 7, 11);
    check(menu.view == UI_VIEW_PET && menu.pet_current == 7 && menu.pet_chosen &&
          ui_menu_take_action(&menu, out, sizeof(out)) == UI_ACTION_NONE, "the phone's pet, no action");
    check(!ui_menu_tick(&menu, 1900) && ui_menu_tick(&menu, 200), "shown two seconds");
    ui_menu_open_pets(&menu, 1, 11);
    ui_menu_pet_step(&menu, 1);
    check(ui_menu_tick(&menu, 20000) && menu.view == UI_VIEW_VIDEO && menu.pet_current == 1,
          "left alone, it closes without changing the pet");
}

static void test_voice_screens_share_the_panel(void)
{
    // From the picture: the panel opens on the voice screen.
    ui_menu_t menu = grouped("ch001");
    ui_menu_voice_show(&menu, UI_VOICE_LISTENING);
    check(menu.view == UI_VIEW_MENU && ui_menu_voice_up(&menu), "voice opens the panel");
    check(!menu.voice.over_list, "no list underneath");
    check(menu.timeout_ms == 0, "the voice policy owns the timing");
    check(!ui_menu_tick(&menu, 60000) && menu.view == UI_VIEW_MENU, "the panel does not time out under it");
    ui_menu_scroll_by(&menu, 1, 7);
    ui_menu_confirm(&menu);
    check(menu.action == UI_ACTION_NONE, "list keys do nothing to a voice screen");
    // "None of these": the list opens afresh at the groups.
    ui_menu_voice_to_list(&menu);
    check(menu.view == UI_VIEW_MENU && !ui_menu_voice_up(&menu), "the list replaces the voice screen");
    check(ui_menu_at_groups(&menu), "opened at the groups");
    check(menu.timeout_ms > 0, "the list's own timeout is back");

    // From inside a group: the list is kept as it was underneath.
    ui_menu_confirm(&menu);
    check(!ui_menu_at_groups(&menu), "inside a group");
    const unsigned selected = menu.selected;
    ui_menu_voice_show(&menu, UI_VOICE_LISTENING);
    check(menu.voice.over_list, "the list is underneath");
    ui_menu_voice_show(&menu, UI_VOICE_RECOGNISING);
    check(menu.voice.over_list, "still underneath after the next screen");
    ui_menu_voice_to_list(&menu);
    check(!ui_menu_at_groups(&menu) && menu.selected == selected, "back to the same place in the list");

    // Back (double-click) from a voice screen over the list returns to it, and
    // from one over the picture closes the panel.
    ui_menu_voice_show(&menu, UI_VOICE_NOT_UNDERSTOOD);
    ui_menu_back(&menu);
    check(menu.view == UI_VIEW_MENU && !ui_menu_voice_up(&menu) && !ui_menu_at_groups(&menu),
          "back from voice: the list as it was");
    ui_menu_close(&menu);
    ui_menu_voice_show(&menu, UI_VOICE_ERROR);
    ui_menu_back(&menu);
    check(menu.view == UI_VIEW_VIDEO && menu.voice.screen == UI_VOICE_NONE, "back from voice over the picture closes");

    // Rows, highlight and scrolling.
    ui_menu_voice_show(&menu, UI_VOICE_CHOICES);
    menu.voice.row_count = 7;
    ui_menu_voice_select(&menu, 0);
    check(menu.voice.highlight && menu.voice.selected == 0 && menu.voice.scroll == 0, "first row");
    ui_menu_voice_select(&menu, 5);
    check(menu.voice.scroll == 2, "the highlight is kept on screen");
    ui_menu_voice_select(&menu, 99);
    check(menu.voice.selected == 6 && menu.voice.scroll == 3, "clamped to the last row");
    ui_menu_voice_select(&menu, 1);
    check(menu.voice.scroll == 1, "scrolls back up");
    ui_menu_voice_show(&menu, UI_VOICE_CHOICES);
    check(menu.voice.row_count == 7, "the same screen keeps its rows");
    ui_menu_voice_show(&menu, UI_VOICE_DONE);
    check(menu.voice.row_count == 0 && !menu.voice.highlight, "another screen starts empty");

    // Any other page replaces it.
    ui_menu_open_status(&menu);
    check(menu.view == UI_VIEW_STATUS && menu.voice.screen == UI_VOICE_NONE, "a page drops the voice screen");
    ui_menu_voice_show(&menu, UI_VOICE_ERROR);
    check(!menu.voice.over_list, "a page is not the list");

    // Without a list, going "back to the list" closes.
    ui_menu_t empty;
    ui_menu_init(&empty);
    ui_menu_voice_show(&empty, UI_VOICE_UNAVAILABLE);
    check(empty.view == UI_VIEW_MENU, "voice opens without a list");
    ui_menu_voice_to_list(&empty);
    check(empty.view == UI_VIEW_VIDEO, "no list to go back to: closed");
}

static void test_episode_loading(void)
{
    ui_menu_t menu;
    ui_menu_init(&menu);
    char text[32];
    ui_episode_t ep = { "", 3, 26 };
    ui_episode_text(&ep, text, sizeof(text));
    check(strcmp(text, "第 3/26 集") == 0, "episode with total");
    ep.total = 0;
    ui_episode_text(&ep, text, sizeof(text));
    check(strcmp(text, "第 3 集") == 0, "episode without total");
    ep.total = 2;
    ui_episode_text(&ep, text, sizeof(text));
    check(strcmp(text, "第 3 集") == 0, "a total below the episode is not shown");
    ep.episode = 0;
    ui_episode_text(&ep, text, sizeof(text));
    check(text[0] == '\0' && !ui_episode_known(&ep), "not a series, no title: nothing");

    // A title cut on a whole character: 21 CJK characters are 63 bytes, 22 do not fit.
    char long_title[80] = "";
    for (int i = 0; i < 22; i++) strcat(long_title, "犬");
    ui_episode_set_title(&ep, long_title);
    check(strlen(ep.title) == 63, "21 characters fit");
    strcpy(long_title, "a");
    for (int i = 0; i < 22; i++) strcat(long_title, "犬");
    ui_episode_set_title(&ep, long_title);
    check(strlen(ep.title) == 61, "a cut character is dropped, not halved");
    ui_episode_set_title(&ep, NULL);
    check(ep.title[0] == '\0', "no title");

    // Loading: the band goes up, takes a "next" box with it, refuses a new one.
    ui_menu_show_hint(&menu, UI_HINT_NEXT, 0);
    ui_episode_t next = { "第2集 战国", 2, 26 };
    const unsigned seq = menu.info_seq;
    ui_menu_show_loading(&menu, &next);
    check(ui_menu_loading_band(&menu) && ui_menu_loading_up(&menu) && menu.info_seq != seq, "band up");
    check(menu.hint == UI_HINT_NONE, "the next box goes");
    ui_menu_show_hint(&menu, UI_HINT_NEXT, 0);
    check(menu.hint == UI_HINT_NONE, "and does not come back while loading");
    ui_menu_show_hint(&menu, UI_HINT_FORWARD, 30);
    check(menu.hint == UI_HINT_FORWARD, "a seek box still shows");
    ui_menu_show_loading(&menu, &(ui_episode_t){ "", 0, 0 });
    check(menu.loading.episode == 2, "an empty loading changes nothing");

    // A picture before the server's word does not end it.
    ui_menu_loading_shown(&menu);
    check(ui_menu_loading_up(&menu), "still loading");
    (void)ui_menu_tick(&menu, 20000);   // the seek box goes; the band stays
    check(ui_menu_loading_band(&menu), "stays while the source opens");

    // The server has it: no band over pictures, the waiting screen keeps it.
    check(ui_menu_loading_done(&menu), "done");
    check(!ui_menu_loading_done(&menu), "once");
    check(!ui_menu_loading_band(&menu) && ui_menu_loading_up(&menu), "band off, still loading");
    check(menu.playing.episode == 2 && strcmp(menu.playing.title, "第2集 战国") == 0, "it plays now");
    ui_menu_loading_shown(&menu);
    check(!ui_menu_loading_up(&menu), "the picture ends it");

    // Never stuck: 30 s without word, 3 s after the word without a picture.
    ui_menu_show_loading(&menu, &next);
    check(!ui_menu_tick(&menu, 29000) && ui_menu_loading_up(&menu), "29 s");
    check(ui_menu_tick(&menu, 1000) && !ui_menu_loading_up(&menu), "30 s");
    ui_menu_show_loading(&menu, &next);
    ui_menu_loading_done(&menu);
    check(ui_menu_tick(&menu, 3000) && !ui_menu_loading_up(&menu), "3 s after done");
    check(!ui_menu_loading_done(&menu), "done with nothing loading is nothing");

    ui_menu_set_playing(&menu, NULL);
    check(!ui_episode_known(&menu.playing), "forgotten");
}


static void test_progress(void)
{
    ui_progress_t p;
    ui_progress_forget(&p);
    uint32_t pos = 1;
    check(!ui_progress_position(&p, 5000, &pos) && pos == 1, "nothing known");
    ui_progress_seek(&p, 30, 1000);
    check(!p.known && !p.seeking, "a seek with nothing known is nothing");
    check(!ui_progress_anchor(&p, 0, 0, 0, false, 0) && !p.known, "no length, no bar");

    // 23:40 long, at 12 s when the sound clock read 0: it moves with the clock.
    check(ui_progress_anchor(&p, 1420000, 12000, 0, false, 0), "anchor");
    check(ui_progress_position(&p, 0, &pos) && pos == 12000, "at the anchor");
    check(ui_progress_position(&p, 60000, &pos) && pos == 72000, "a minute on");
    check(ui_progress_position(&p, 5000000, &pos) && pos == 1420000, "never past the end");
    // An anchor for a later clock reading (a new source after a silence).
    ui_progress_anchor(&p, 1420000, 300000, 64000, false, 0);
    check(ui_progress_position(&p, 10000, &pos) && pos == 300000, "before its first sound: at its start");
    check(ui_progress_position(&p, 74000, &pos) && pos == 310000, "10 s after");

    // Stopped (a stall, a pause): the bar stays where the sound ran out.
    ui_progress_anchor(&p, 1420000, 300000, 64000, true, 70000);
    check(ui_progress_position(&p, 68000, &pos) && pos == 304000, "the queued sound still plays");
    check(ui_progress_position(&p, 99000, &pos) && pos == 306000, "then it stops");
    ui_progress_anchor(&p, 1420000, 300000, 64000, true, 1000);
    check(ui_progress_position(&p, 99000, &pos) && pos == 300000, "a stop before the anchor is the anchor");

    // A seek: there at once, still, through the old source's stop, until the new anchor.
    ui_progress_anchor(&p, 1420000, 0, 0, false, 0);
    ui_progress_seek(&p, 30, 60000);
    check(p.seeking && ui_progress_position(&p, 61000, &pos) && pos == 90000, "jumped 30 s on, at once");
    check(ui_progress_position(&p, 65000, &pos) && pos == 90000, "and still");
    ui_progress_seek(&p, 30, 65000);
    check(ui_progress_position(&p, 66000, &pos) && pos == 120000, "a second seek adds to the first");
    ui_progress_anchor(&p, 1420000, 0, 0, true, 66000);
    check(p.seeking && ui_progress_position(&p, 67000, &pos) && pos == 120000, "the cut does not undo it");
    ui_progress_anchor(&p, 1420000, 119000, 70000, false, 0);
    check(!p.seeking && ui_progress_position(&p, 71000, &pos) && pos == 120000, "the new source takes over");
    ui_progress_seek(&p, -600, 71000);
    check(ui_progress_position(&p, 71000, &pos) && pos == 0, "not before the start");
    ui_progress_seek(&p, 86400, 71000);
    check(ui_progress_position(&p, 71000, &pos) && pos == 1420000, "nor past the end");
    check(ui_progress_position(&p, 71000 + 20000, &pos) && pos == 140000,
          "no new source within 20 s: back to the anchor");

    // Pixels and times.
    check(ui_progress_fill(0, 1420000, 320) == 0, "none played");
    check(ui_progress_fill(710000, 1420000, 320) == 160, "half");
    check(ui_progress_fill(1420000, 1420000, 320) == 320 && ui_progress_fill(9, 0, 320) == 0, "ends");
    char played[16], total[16];
    ui_progress_times(754000, 1420000, played, total, sizeof(played));
    check(!strcmp(played, "12:34") && !strcmp(total, "23:40"), "12:34 / 23:40");
    ui_progress_times(5000, 185000, played, total, sizeof(played));
    check(!strcmp(played, "0:05") && !strcmp(total, "3:05"), "0:05 / 3:05");
    ui_progress_times(754000, 5020000, played, total, sizeof(played));
    check(!strcmp(played, "0:12:34") && !strcmp(total, "1:23:40"), "an hour or more: H:MM:SS for both");
    ui_progress_times(9000000, 3600000, played, total, sizeof(played));
    check(!strcmp(played, "1:00:00") && !strcmp(total, "1:00:00"), "played never past the total");

    // The times under the picture: with the banner, and 4 s after a key.
    ui_menu_t menu;
    ui_menu_init(&menu);
    check(!ui_menu_progress_label(&menu), "down at first");
    ui_menu_show_progress(&menu);
    check(ui_menu_progress_label(&menu), "up after a key");
    check(!ui_menu_tick(&menu, 3900) && ui_menu_progress_label(&menu), "3.9 s: still up, no repaint asked");
    (void)ui_menu_tick(&menu, 100);
    check(!ui_menu_progress_label(&menu), "4 s: down");
    ui_menu_banner(&menu, "犬夜叉", NULL);
    check(ui_menu_progress_label(&menu), "up with the banner");
    ui_progress_forget(&p);
    check(!p.known, "forgotten");
}

int main(void)
{
    test_voice_screens_share_the_panel();
    test_open_selects_the_playing_channel();
    test_stepping_wraps_and_keeps_the_selection_sane();
    test_confirm_reports_the_highlighted_channel();
    test_menu_times_out_but_not_the_video();
    test_banner_only_when_the_channel_actually_changed();
    test_unusable_entries_are_skipped_not_fatal();
    test_list_capacity_is_enforced();
    test_a_full_channel_list_loads();
    test_a_channel_missing_from_the_list_is_handled();
    test_open_with_no_list_does_nothing();
    test_scrolling_keeps_the_highlight_on_screen();
    test_confirm_then_take_yields_the_id_once();
    test_confirm_survives_a_long_list_and_scrolling();
    test_visible_rows_never_exceeds_the_list();
    test_groups_open_on_the_playing_group();
    test_confirm_enters_the_group_and_back_returns();
    test_without_groups_the_list_is_one_level();
    test_group_names_are_bounded();
    test_row_queries();
    test_banner_place();
    test_level_indicator();
    test_seek_hint();
    test_pet_picker();
    test_episode_loading();
    test_progress();

    if (failures) {
        printf("%d check(s) failed\n", failures);
        return 1;
    }
    printf("overlay menu tests: PASS\n");
    return 0;
}
