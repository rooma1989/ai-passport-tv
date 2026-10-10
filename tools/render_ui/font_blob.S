// The real glyph table, linked into the host renderer the way the firmware's
// EMBED_FILES does it. Built from the repository root (see render_ui.sh).
#ifdef __APPLE__
    .section __DATA,__const
#else
    .section .rodata
#endif
    .globl _binary_ui_font_cjk_bin_start
_binary_ui_font_cjk_bin_start:
    .incbin "main/ui_font_cjk.bin"
