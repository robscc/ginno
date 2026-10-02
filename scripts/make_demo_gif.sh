#!/usr/bin/env bash
# Turn a screen recording into a README-quality GIF.
#
# Usage:
#   scripts/make_demo_gif.sh input.mov [output.gif] [start_seconds] [duration_seconds]
#
# Example:
#   scripts/make_demo_gif.sh ~/Desktop/ginno-demo.mov docs/demo.gif 2 12
#
# How to record the input (either works):
#   A) QuickTime Player → File → New Screen Recording → save as .mov
#   B) ffmpeg -f avfoundation -i "1:0" -r 15 demo.mov   (then Ctrl-C to stop)
#
# Needs ffmpeg. On macOS:  brew install ffmpeg
# No ffmpeg and no Homebrew? Upload the .mov to https://ezgif.com/video-to-gif
set -euo pipefail

IN="${1:-}"
OUT="${2:-docs/demo.gif}"
START="${3:-0}"
DUR="${4:-12}"

if [[ -z "$IN" ]]; then
  echo "usage: $0 <input.mov> [output.gif] [start_s] [duration_s]" >&2
  exit 2
fi
if [[ ! -f "$IN" ]]; then
  echo "error: input not found: $IN" >&2
  exit 2
fi
if ! command -v ffmpeg >/dev/null 2>&1; then
  cat >&2 <<'EOF'
error: ffmpeg not found.

Install it:            brew install ffmpeg
Or use the web tool:   https://ezgif.com/video-to-gif  (drag the .mov in, trim, download GIF)
EOF
  exit 1
fi

# Two-pass palette for a clean, small GIF. Scale to 1200px wide, 12 fps.
PALETTE="$(mktemp -t ginno_pal).png"
trap 'rm -f "$PALETTE"' EXIT

echo "→ building palette…"
ffmpeg -v error -ss "$START" -t "$DUR" -i "$IN" \
  -vf "fps=12,scale=1200:-1:flags=lanczos,palettegen=stats_mode=diff" -y "$PALETTE"

echo "→ rendering GIF…"
ffmpeg -v error -ss "$START" -t "$DUR" -i "$IN" -i "$PALETTE" \
  -lavfi "fps=12,scale=1200:-1:flags=lanczos[x];[x][1:v]paletteuse=dither=bayer:bayer_scale=3" \
  -loop 0 -y "$OUT"

echo "✓ wrote $OUT ($(du -h "$OUT" | cut -f1))"
echo "  Add to README:  ![Demo]($OUT)"