#!/usr/bin/env bash
# Run any make-reel-podcast tool with the sofit venv python (which has sofit,
# opencv, Pillow+raqm). Usage:  ./mrp.sh <tool.py> [args...]
#   ./mrp.sh podcast_clips.py episode.mp4 --out cut.clips.json
#   ./mrp.sh reaction_cut.py --video source.mp4 --spec cut.clips.json --clip-id clip-1 --out master.mp4 ...
#   ./mrp.sh podcast_reel.py master.mp4 --spec words.json --clip-id clip-1 --layout full ...
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
PIPX_HOME="$(pipx environment --value PIPX_HOME 2>/dev/null || echo "$HOME/.local/pipx")"
PY="$PIPX_HOME/venvs/sofit-cli/bin/python"
[ -x "$PY" ] || PY="$HOME/Library/Application Support/pipx/venvs/sofit-cli/bin/python"
[ -x "$PY" ] || { echo "sofit venv python not found — run ./setup.sh first."; exit 1; }
[ $# -ge 1 ] || { echo "usage: ./mrp.sh <tool.py> [args...]"; exit 1; }
exec "$PY" "$HERE/tools/$1" "${@:2}"
