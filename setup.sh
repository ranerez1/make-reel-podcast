#!/usr/bin/env bash
# One-time setup for make-reel-podcast on a fresh macOS machine.
# Installs the sofit engine + system deps, applies the brand caption patch,
# and places the logo/music assets. Safe to re-run.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"

echo "==> make-reel-podcast setup"
[ "$(uname)" = "Darwin" ] || { echo "This kit targets macOS (Homebrew + ~/Library/Fonts)."; exit 1; }

# 1. Homebrew
command -v brew >/dev/null || { echo "Install Homebrew first: https://brew.sh"; exit 1; }

# 2. System deps: ffmpeg (render), pipx (python app installer), libraqm (Hebrew
#    text shaping for Pillow), yt-dlp (YouTube sources), the brand fonts.
echo "==> brew installing ffmpeg, pipx, libraqm, yt-dlp, fonts ..."
brew install ffmpeg pipx libraqm yt-dlp >/dev/null || true
brew install --cask font-heebo font-poppins >/dev/null || true
pipx ensurepath >/dev/null 2>&1 || true

# 3. The sofit engine (transcription, clip selection, render helpers) + opencv
echo "==> installing sofit-cli (this pulls faster-whisper; can take a few minutes) ..."
pipx install "sofit-cli[mcp,render,youtube]" 2>/dev/null || pipx upgrade sofit-cli || true
pipx inject sofit-cli opencv-python-headless 2>/dev/null || true

PIPX_HOME="$(pipx environment --value PIPX_HOME 2>/dev/null || echo "$HOME/.local/pipx")"
PY="$PIPX_HOME/venvs/sofit-cli/bin/python"
[ -x "$PY" ] || PY="$HOME/Library/Application Support/pipx/venvs/sofit-cli/bin/python"
[ -x "$PY" ] || { echo "Could not find the sofit venv python. Is 'pipx install sofit-cli' done?"; exit 1; }
echo "    sofit python: $PY"

# 4. Brand caption fonts/colour patch to sofit's render.py (idempotent; re-apply
#    after any 'pipx upgrade sofit-cli').
echo "==> applying brand caption patch ..."
"$PY" "$HERE/tools/brand_captions_patch.py" || echo "   (patch step reported an issue — the reaction/podcast_reel captions don't depend on it, only the legacy split/switch path does)"

# 5. Pillow Hebrew shaping check
if ! "$PY" -c "from PIL import features; import sys; sys.exit(0 if features.check('raqm') else 1)"; then
  echo "!! Pillow lacks 'raqm' (needed for correct Hebrew captions)."
  echo "   Fixing: reinstalling Pillow against libraqm ..."
  "$PY" -m pip install --force-reinstall --no-binary :all: pillow >/dev/null 2>&1 || \
    echo "   Could not rebuild Pillow. Ensure 'brew install libraqm' succeeded, then: $PY -m pip install --force-reinstall --no-binary :all: pillow"
fi

# 6. Podcast brand assets
echo "==> installing brand assets to ~/.config/sofit/podcast/ ..."
mkdir -p "$HOME/.config/sofit/podcast"
cp "$HERE/assets/logo.png"  "$HOME/.config/sofit/podcast/logo.png"
cp "$HERE/assets/theme.m4a" "$HOME/.config/sofit/podcast/theme.m4a"

echo ""
echo "==> Done. Test it:"
echo "    ./mrp.sh podcast_reel.py --help"
echo "    (see README.md for the full workflow)"
