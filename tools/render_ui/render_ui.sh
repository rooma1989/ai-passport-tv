#!/usr/bin/env bash
# Draw the device's screens on the host with the firmware's own drawing code
# and the real font, into one PNG, to check a layout against the design
# (docs/xiaozhi-tv-ui-handoff) without flashing.
# Usage: render_ui.sh [--passport] [out.png]  (--passport: the AI Passport's
# 320x240 panel; default the Xingzhi board's 240x240)
set -euo pipefail
root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
board=(-DRENDER_W=240)
if [[ "${1:-}" == --passport ]]; then
    board=(-DRENDER_W=320 -DUI_RENDER_PASSPORT=1)
    shift
fi
out="${1:-${TMPDIR:-/tmp}/xiaozhi-ui.png}"
work="$(mktemp -d)"
cd "${root}"
"${CC:-cc}" -std=c11 -O1 "${board[@]}" -Imain -Icomponents/qrcodegen tools/render_ui/render_ui.c tools/render_ui/font_blob.S \
    main/ui_overlay.c main/ui_status.c main/ui_draw.c main/ui_text.c main/ui_menu.c \
        main/ui_qr.c components/qrcodegen/qrcodegen.c \
    -o "${work}/render_ui"
"${work}/render_ui" "${work}/screens.ppm"
if command -v sips >/dev/null 2>&1; then
    sips -s format png "${work}/screens.ppm" --out "${out}" >/dev/null
else
    python3 -c "import sys; from PIL import Image; Image.open(sys.argv[1]).save(sys.argv[2])" \
        "${work}/screens.ppm" "${out}"
fi
rm -rf "${work}"
echo "${out}"
