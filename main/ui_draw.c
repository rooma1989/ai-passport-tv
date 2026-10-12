// main/ui_draw.c -- see ui_draw.h.
//
// The assets header comes first and ui_text.h (through ui_draw.h) after it, so
// the palette both define is compared here: a value that drifted between the
// design's export and ui_text.h is a macro redefinition, which the build turns
// into an error.
#include "xiaozhi_tv_ui_assets.h"
#include "ui_draw.h"

static const uint8_t *const icons[UI_ICON_COUNT] = {
    [UI_ICON_WIFI] = ICON_WIFI,
    [UI_ICON_SIGNAL_BAR1] = ICON_SIGNAL_BAR1,
    [UI_ICON_SIGNAL_BAR2] = ICON_SIGNAL_BAR2,
    [UI_ICON_SIGNAL_BAR3] = ICON_SIGNAL_BAR3,
    [UI_ICON_SIGNAL_BAR4] = ICON_SIGNAL_BAR4,
    [UI_ICON_BATTERY_FRAME] = ICON_BATTERY_FRAME,
    [UI_ICON_BATTERY_FILL] = ICON_BATTERY_FILL,
    [UI_ICON_BOLT] = ICON_BOLT,
    [UI_ICON_PAUSE] = ICON_PAUSE,
    [UI_ICON_PLAY] = ICON_PLAY,
    [UI_ICON_VOLUME] = ICON_VOLUME,
    [UI_ICON_SUN] = ICON_SUN,
    [UI_ICON_FF] = ICON_FF,
    [UI_ICON_NEXT] = ICON_NEXT,
    [UI_ICON_CROSS] = ICON_CROSS,
    [UI_ICON_CHECK] = ICON_CHECK,
    [UI_ICON_CAST] = ICON_CAST,
    [UI_ICON_PERSON] = ICON_PERSON,
    [UI_ICON_TV] = ICON_TV,
    [UI_ICON_GLOBE] = ICON_GLOBE,
    [UI_ICON_NOTE] = ICON_NOTE,
    [UI_ICON_FILM] = ICON_FILM,
    [UI_ICON_MORE] = ICON_MORE,
    [UI_ICON_LINK] = ICON_LINK,
    [UI_ICON_PHONE] = ICON_PHONE,
    [UI_ICON_REWIND] = ICON_REWIND,
    [UI_ICON_MIC] = ICON_MIC,
};

// True when rows [y, y + h) reach the band at all.
static bool touches(ui_surface_t surface, int y, int h)
{
    return y + h > surface.origin_y && y < surface.origin_y + surface.rows;
}

void ui_draw_icon_columns(ui_surface_t surface, ui_icon_t icon, int x, int y, int scale,
                          uint16_t color, int first, int count)
{
    if ((unsigned)icon >= UI_ICON_COUNT || scale < 1) {
        return;
    }
    if (first < 0) {
        count += first;
        first = 0;
    }
    if (first + count > UI_ICON_SIZE) {
        count = UI_ICON_SIZE - first;
    }
    if (count <= 0 || !touches(surface, y, UI_ICON_SIZE * scale)) {
        return;
    }
    const uint8_t *bits = icons[icon];
    for (int row = 0; row < UI_ICON_SIZE; row++) {
        const int row_y = y + row * scale;
        if (!touches(surface, row_y, scale)) {
            continue;
        }
        const unsigned line = ((unsigned)bits[row * 2] << 8) | bits[row * 2 + 1];
        // Runs of set dots, one fill each, as the text renderer does.
        int column = first;
        const int end = first + count;
        while (column < end) {
            if (!(line & (0x8000u >> column))) {
                column++;
                continue;
            }
            int run_end = column;
            while (run_end < end && (line & (0x8000u >> run_end))) {
                run_end++;
            }
            ui_text_fill(surface, x + column * scale, row_y, (run_end - column) * scale, scale, color);
            column = run_end;
        }
    }
}

void ui_draw_icon(ui_surface_t surface, ui_icon_t icon, int x, int y, int scale, uint16_t color)
{
    ui_draw_icon_columns(surface, icon, x, y, scale, color, 0, UI_ICON_SIZE);
}

unsigned ui_pet_count(void)
{
    return PET_COUNT;
}

const char *ui_pet_name(unsigned pet)
{
    return pet < PET_COUNT ? PETS[pet].name : NULL;
}

const char *ui_pet_kind(unsigned pet)
{
    // HANDOFF.md 4.15, in PETS[] order.
    static const char *const kinds[PET_COUNT] = {
        "小电视", "小收音机", "猫耳电视", "信号团子", "小磁带", "小灯泡",
        "小掌机", "小机器人", "小幽灵", "破壳小鸡", "小仙人掌",
    };
    return pet < PET_COUNT ? kinds[pet] : NULL;
}

unsigned ui_pet_moods(unsigned pet)
{
    return pet < PET_COUNT ? PETS[pet].mood_count : 0u;
}

void ui_draw_pet(ui_surface_t surface, unsigned pet, unsigned mood, int x, int y, int scale)
{
    if (pet >= PET_COUNT || scale < 1 || !touches(surface, y, UI_PET_SIZE * scale)) {
        return;
    }
    const pet_sprite_t *sprite = &PETS[pet];
    if (mood >= sprite->mood_count) {
        mood = sprite->mood_count - 1u;
    }
    const uint8_t *frame = sprite->frames[mood];
    for (int row = 0; row < UI_PET_SIZE; row++) {
        const int row_y = y + row * scale;
        if (!touches(surface, row_y, scale)) {
            continue;
        }
        const uint8_t *line = frame + row * (UI_PET_SIZE / 2);
        // Runs of one palette index, one fill each.
        int column = 0;
        while (column < UI_PET_SIZE) {
            const uint8_t byte = line[column / 2];
            const unsigned index = (column & 1) ? (byte & 0x0Fu) : (byte >> 4);
            int run_end = column + 1;
            while (run_end < UI_PET_SIZE) {
                const uint8_t next = line[run_end / 2];
                if (((run_end & 1) ? (next & 0x0Fu) : (next >> 4)) != index) {
                    break;
                }
                run_end++;
            }
            if (index) {
                ui_text_fill(surface, x + column * scale, row_y, (run_end - column) * scale, scale,
                             sprite->palette[index]);
            }
            column = run_end;
        }
    }
}

bool ui_pet_rows(unsigned pet, unsigned mood, int *top, int *bottom)
{
    if (pet >= PET_COUNT) {
        return false;
    }
    const pet_sprite_t *sprite = &PETS[pet];
    if (mood >= sprite->mood_count) {
        mood = sprite->mood_count - 1u;
    }
    const uint8_t *frame = sprite->frames[mood];
    int first = -1, last = -1;
    for (int row = 0; row < UI_PET_SIZE; row++) {
        for (int i = 0; i < UI_PET_SIZE / 2; i++) {
            if (frame[row * (UI_PET_SIZE / 2) + i]) {
                if (first < 0) first = row;
                last = row;
                break;
            }
        }
    }
    if (first < 0) {
        first = 0;
        last = UI_PET_SIZE - 1;
    }
    if (top) *top = first;
    if (bottom) *bottom = last;
    return true;
}

void ui_draw_dotted_hline(ui_surface_t surface, int x0, int x1, int y, uint16_t color)
{
    if (!touches(surface, y, 1)) {
        return;
    }
    for (int x = x0; x <= x1; x += 4) {
        ui_text_fill(surface, x, y, x + 1 <= x1 ? 2 : 1, 1, color);
    }
}

void ui_draw_notched_rect(ui_surface_t surface, int x, int y, int w, int h, uint16_t color)
{
    if (w < 5 || h < 4) {
        ui_text_fill(surface, x, y, w, h, color);
        return;
    }
    ui_text_fill(surface, x + 2, y, w - 4, 1, color);
    ui_text_fill(surface, x + 1, y + 1, w - 2, 1, color);
    ui_text_fill(surface, x, y + 2, w, h - 4, color);
    ui_text_fill(surface, x + 1, y + h - 2, w - 2, 1, color);
    ui_text_fill(surface, x + 2, y + h - 1, w - 4, 1, color);
}

void ui_draw_cells(ui_surface_t surface, int y, int h, unsigned lit,
                   uint16_t lit_color, uint16_t dim_color)
{
    if (!touches(surface, y, h)) {
        return;
    }
    for (unsigned k = 0; k < UI_CELLS; k++) {
        ui_text_fill(surface, UI_CELLS_X + (int)k * UI_CELLS_STEP, y, UI_CELLS_W, h,
                     k < lit ? lit_color : dim_color);
    }
}

void ui_draw_panel(ui_surface_t surface, int top, int bottom)
{
    ui_text_fill(surface, 0, top, surface.width, bottom - top, UI_INK);
}
