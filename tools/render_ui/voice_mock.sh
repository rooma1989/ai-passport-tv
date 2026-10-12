#!/usr/bin/env bash
# PROTOTYPE: draw the proposed voice-control screens (voice_mock.c) with the
# firmware's own drawing code and font, into one PNG. Not part of the firmware.
# Usage: voice_mock.sh [--passport] [out.png]
set -euo pipefail
root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
board=(-DRENDER_W=240)
if [[ "${1:-}" == --passport ]]; then
    board=(-DRENDER_W=320 -DUI_RENDER_PASSPORT=1)
    shift
fi
out="${1:-${TMPDIR:-/tmp}/voice-mock.png}"
work="$(mktemp -d)"
cd "${root}"
"${CC:-cc}" -std=c11 -O1 "${board[@]}" -Imain -Icomponents/qrcodegen tools/render_ui/voice_mock.c tools/render_ui/font_blob.S \
    main/ui_overlay.c main/ui_status.c main/ui_draw.c main/ui_text.c main/ui_menu.c \
        main/ui_qr.c components/qrcodegen/qrcodegen.c \
    -o "${work}/voice_mock"
"${work}/voice_mock" "${work}/screens.ppm"
if command -v sips >/dev/null 2>&1; then
    sips -s format png "${work}/screens.ppm" --out "${out}" >/dev/null
else
    python3 -c "import sys; from PIL import Image; Image.open(sys.argv[1]).save(sys.argv[2])" \
        "${work}/screens.ppm" "${out}"
fi
rm -rf "${work}"
echo "${out}"
