#!/usr/bin/env python3
"""Re-applyable brand patch for sofit's burned social captions.

sofit is installed as a third-party package (pipx), so `pipx upgrade sofit-cli`
overwrites its render.py and wipes these edits. This script re-applies them:

  1. Word-highlight colour -> RE.FOCUS cyan (#00f6ff) instead of the stock yellow.
  2. Caption fonts -> Heebo (Black) for Hebrew, Poppins (ExtraBold) for English,
     chosen per word (sofit already lays captions out word-by-word). Hook cards
     and the fallback caption font default to Heebo.

Fonts themselves are installed once via Homebrew and live in ~/Library/Fonts, so
they survive pipx upgrades — only this code patch needs re-running:

    brew install --cask font-heebo font-poppins   # one time
    python3 Tools/sofit/brand_captions_patch.py    # after every pipx upgrade

The script is idempotent: run it as many times as you like. It errors loudly if
sofit's source has changed shape (so a silent no-op never hides a broken patch).
"""
from __future__ import annotations
import glob
import os
import sys

HEEBO_PATH = os.path.expanduser("~/Library/Fonts/Heebo[wght].ttf")
POPPINS_PATH = os.path.expanduser("~/Library/Fonts/Poppins-ExtraBold.otf")

# Injected once after the caption colour constants. Keys font choice off the
# text's script so per-word measurement and drawing stay consistent.
BRAND_FONT_BLOCK = '''

# --- RE.FOCUS brand caption fonts (injected by Tools/sofit/brand_captions_patch.py) ---
_HEEBO_PATH = os.path.expanduser("~/Library/Fonts/Heebo[wght].ttf")
_POPPINS_PATH = os.path.expanduser("~/Library/Fonts/Poppins-ExtraBold.otf")
_BRAND_FONT_CACHE: dict = {}


def _brand_caption_font(kind: str, size: int):
    """kind: "lat" -> Poppins ExtraBold, else Heebo Black. Cached per (kind,size)."""
    key = (kind, size)
    cached = _BRAND_FONT_CACHE.get(key)
    if cached is not None:
        return cached
    from PIL import ImageFont
    path = _POPPINS_PATH if kind == "lat" else _HEEBO_PATH
    try:
        f = ImageFont.truetype(path, size)
        if kind == "he":
            try:
                f.set_variation_by_name("Black")
            except Exception:
                pass
    except Exception:
        f = _load_caption_font(size)  # graceful fallback if a font is missing
    _BRAND_FONT_CACHE[key] = f
    return f


def _font_for_text(txt: str, size: int):
    """Poppins for pure-Latin tokens (English brands/acronyms), Heebo otherwise."""
    has_hebrew = any("֐" <= c <= "׿" for c in txt)
    has_latin = any(c.isascii() and c.isalnum() for c in txt)
    return _brand_caption_font("lat" if (has_latin and not has_hebrew) else "he", size)
# --- end RE.FOCUS brand caption fonts ---
'''

# (old, new, label). Each applied only if `old` is present; if the post-edit
# marker (`new`) is already there, that edit is treated as done.
EDITS = [
    (
        "_ACCENT = (255, 214, 10, 255)",
        "_ACCENT = (0, 246, 255, 255)",
        "highlight colour -> cyan",
    ),
    (
        "# active word — punchy yellow",
        "# active word — RE.FOCUS cyan (#00f6ff)",
        "highlight comment",
    ),
    (
        "        candidates.append(font)\n    if _BUNDLED_FONT.exists():",
        "        candidates.append(font)\n    if os.path.exists(_HEEBO_PATH):\n"
        "        candidates.append(_HEEBO_PATH)\n    if _BUNDLED_FONT.exists():",
        "default caption font -> Heebo",
    ),
    (
        "    def word_w(txt: str) -> float:\n        return measure.textlength(txt, font=pil_font)",
        "    def word_w(txt: str) -> float:\n        return measure.textlength(txt, font=_font_for_text(txt, font_size))",
        "caption measure -> per-word font",
    ),
    (
        '                    d.text((x, y), w["text"], font=pil_font, fill=color,',
        '                    d.text((x, y), w["text"], font=_font_for_text(w["text"], font_size), fill=color,',
        "caption draw -> per-word font",
    ),
    (
        "    def cta_w(txt: str) -> float:\n        return measure.textlength(txt, font=cta_font)",
        "    def cta_w(txt: str) -> float:\n        return measure.textlength(txt, font=_font_for_text(txt, cta_size))",
        "CTA measure -> per-word font",
    ),
    (
        '            twd = measure.textlength(tok["text"], font=cta_font)',
        '            twd = measure.textlength(tok["text"], font=_font_for_text(tok["text"], cta_size))',
        "CTA wrap -> per-word font",
    ),
    (
        '                    d.text((x, y), w["text"], font=cta_font, fill=_WHITE,',
        '                    d.text((x, y), w["text"], font=_font_for_text(w["text"], cta_size), fill=_WHITE,',
        "CTA draw -> per-word font",
    ),
    (
        '    logo = logo or os.environ.get("SOFIT_LOGO") or None',
        '    logo = logo or os.environ.get("SOFIT_LOGO") or None\n'
        '    logo_pos = os.environ.get("SOFIT_LOGO_POS") or logo_pos  # brand default (e.g. bottom-left)',
        "logo position env override",
    ),
    (
        "    accent = _accent_from_art(cover if audiogram_assets else logo)",
        "    accent = None  # RE.FOCUS brand: always the cyan _ACCENT, never logo-derived",
        "accent -> always brand cyan",
    ),
    (
        "    max_words = 6      # short chunks read better in short-form",
        '    max_words = int(os.environ.get("SOFIT_CAPTION_MAX_WORDS") or 6)      # short chunks read better in short-form',
        "caption max-words env override",
    ),
    (
        "    max_span = 2.6",
        '    max_span = float(os.environ.get("SOFIT_CAPTION_MAX_SPAN") or 2.6)',
        "caption max-span env override",
    ),
    (
        "    bottom_margin = int(height * b_frac)  # sit in the lower third, clear of the edge",
        "    if os.environ.get(\"SOFIT_CAPTION_BOTTOM_FRAC\"):  # split-screen: put captions at the seam\n"
        "        b_frac = float(os.environ[\"SOFIT_CAPTION_BOTTOM_FRAC\"])\n"
        "    bottom_margin = int(height * b_frac)  # sit in the lower third, clear of the edge",
        "caption position env override",
    ),
]

INSERT_ANCHOR = "_OUTLINE = (0, 0, 0, 255)\n"
INSERT_MARKER = "def _font_for_text("


def find_render() -> str:
    pat = os.path.expanduser(
        "~/Library/Application Support/pipx/venvs/sofit-cli/lib/python*/site-packages/sofit/render.py"
    )
    hits = glob.glob(pat)
    if not hits:
        sys.exit(f"render.py not found under: {pat}\nIs sofit-cli installed via pipx?")
    return hits[0]


def main() -> None:
    for label, path in (("Heebo", HEEBO_PATH), ("Poppins", POPPINS_PATH)):
        if not os.path.exists(path):
            print(f"  ! {label} font missing at {path}")
            print("    run: brew install --cask font-heebo font-poppins")
    render = find_render()
    src = open(render, encoding="utf-8").read()

    bak = render + ".orig"
    if not os.path.exists(bak):
        open(bak, "w", encoding="utf-8").write(src)
        print(f"  backed up pristine render.py -> {os.path.basename(bak)}")

    changed = 0

    # 1. inject the brand-font block once
    if INSERT_MARKER not in src:
        if INSERT_ANCHOR not in src:
            sys.exit("anchor for font block not found — sofit source changed shape.")
        src = src.replace(INSERT_ANCHOR, INSERT_ANCHOR + BRAND_FONT_BLOCK, 1)
        changed += 1
        print("  + injected brand caption font helpers")
    else:
        print("  = brand caption font helpers already present")

    # 2. targeted edits. Idempotency keys on the INSERTED text (new minus old),
    # not on `new in src` — several edits keep `old` inside `new` (prepend/append
    # a line), so `new in src` alone would re-insert on every run.
    for old, new, label in EDITS:
        marker = new.replace(old, "", 1) if old in new else new
        if marker.strip() and marker in src:
            print(f"  = {label} (already applied)")
            continue
        if old not in src:
            sys.exit(f"could not find target for edit: {label}\n  sofit source changed shape.")
        src = src.replace(old, new, 1)
        changed += 1
        print(f"  + {label}")

    if changed:
        open(render, "w", encoding="utf-8").write(src)
        print(f"\nPatched {render}\n({changed} change(s) written)")
    else:
        print("\nNothing to do — already fully patched.")


if __name__ == "__main__":
    main()
