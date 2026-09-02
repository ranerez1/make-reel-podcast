#!/usr/bin/env python3
"""Podcast-reel composer: burn static podcast-style captions + logo + music bed
onto a finished 9:16 clip in ONE ffmpeg pass.

Caption style (matched to the Startup for Startup reference reel, 2026-09-01):
white Heebo wght-700 phrase blocks, soft blurred drop shadow, no outline, no
karaoke highlight, <=2 centered lines, punctuation/pause-aware chunk breaks,
each chunk held on screen until the next starts. This is deliberately NOT
sofit's karaoke caption render.

Run with the sofit venv python (has PIL+raqm and sofit.transcribe):
  "$HOME/Library/Application Support/pipx/venvs/sofit-cli/bin/python" \
      Tools/sofit/podcast_reel.py <clip.mp4> --out final.mp4 --layout split \
      --logo ~/.config/sofit/podcast/logo.png --music ~/.config/sofit/podcast/theme.m4a

Words come from --spec/--clip-id (a sofit clips.json; supports narrative-cut
`segments` via cumulative-offset remap) or, without a spec, from transcribing
the clip itself (content-addressed cache, so re-runs are free).

Music: sofit's --music is audiogram-only, so the bed is mixed here with the
same recipe render.py uses (loop + loudnorm to -28 LUFS + sidechain duck under
speech + amix).
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

W, H = 1080, 1920
LAYOUT_BOTTOM_FRAC = {"split": 0.56, "full": 0.74}  # caption block bottom / H
FONT_PATH = Path.home() / "Library/Fonts/Heebo[wght].ttf"
FONT_WEIGHT = 800     # Heebo ExtraBold — matches the heavier weight a human editor uses
LINE_SPACING = 1.30
# Punchy defaults, tuned 2026-09-01 against a human editor's cut of the same clip:
# short 1-2 line phrases (~4-6 words), changing every ~1.5-2.5s. Narrower lines +
# a shorter max duration + a hard word cap keep chunks from packing too much.
MAX_W_FRAC = 0.72
CHUNK_MAX_DUR = 2.8
CHUNK_MAX_WORDS = 6
SHADOW_BLUR = 9
SHADOW_DY = 5
SHADOW_ALPHA = 190
HOLD_MAX_GAP = 1.2      # hold a caption over a speech gap up to this long
LOGO_MARGIN_X = 40
LOGO_MARGIN_Y = 160     # clears the ~140px top UI zone (reel-rubric gate 4)


def probe(path: Path, entries: str, stream: str | None = "v:0") -> str:
    cmd = ["ffprobe", "-v", "error"]
    if stream:
        cmd += ["-select_streams", stream]
    cmd += ["-show_entries", entries, "-of", "csv=p=0:s=x", str(path)]
    return subprocess.run(cmd, capture_output=True, text=True).stdout.strip()


def load_words_from_spec(spec_path: Path, clip_id: str) -> list[dict]:
    """Words on the OUTPUT timeline of a rendered clip. Plain clips carry
    clip-relative words; narrative-cut clips carry per-segment words relative
    to each segment's start, concatenated in the render — remap cumulatively."""
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    clip = next((c for c in spec["clips"] if c["id"] == clip_id), None)
    if clip is None:
        sys.exit(f"clip {clip_id!r} not in {spec_path}")
    out = []
    if clip.get("segments"):
        cum = 0.0
        for seg in clip["segments"]:
            for w in seg.get("words") or []:
                out.append({"start": cum + w["t"], "end": cum + w["t"] + w["d"],
                            "text": w["w"].strip()})
            cum += float(seg["end"]) - float(seg["start"])
    else:
        for w in clip.get("words") or []:
            out.append({"start": w["t"], "end": w["t"] + w["d"], "text": w["w"].strip()})
    return [w for w in out if w["text"]]


def load_words_from_transcription(video: Path) -> list[dict]:
    from sofit.transcribe import transcribe
    words = []
    for seg in transcribe(str(video)):
        for w in seg.words:
            t = w.text.strip()
            if t:
                words.append({"start": w.start, "end": w.end, "text": t})
    return words


HEB_PREFIX = set("בכלמו")


def tidy_words(words: list[dict]) -> list[dict]:
    """Merge whisper's split number tokens so captions read naturally:
    '%' joins the previous token; a bare numeric token after a single Hebrew
    prefix letter (e.g. 'ל' + '-90') joins it."""
    out: list[dict] = []
    for w in words:
        prev = out[-1] if out else None
        if prev and w["text"] == "%":
            prev["text"] += "%"
            prev["end"] = w["end"]
        elif (prev and re.fullmatch(r"-?\d+(\.\d+)?", w["text"])
              and len(prev["text"]) == 1 and prev["text"] in HEB_PREFIX):
            joiner = "" if w["text"].startswith("-") else "-"
            prev["text"] += joiner + w["text"]
            prev["end"] = w["end"]
        else:
            out.append(dict(w))
    return out


def apply_word_fixes(words: list[dict], fixes: list[dict]) -> None:
    """fixes: [{"match": "אז", "replace": "חס", "near_t": 44.0}] — corrects a
    verified whisper artifact at a known moment; never invents new words."""
    for fix in fixes:
        for w in words:
            if w["text"] == fix["match"] and abs(w["start"] - fix["near_t"]) < 2.0:
                w["text"] = fix["replace"]
                break


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("video", type=Path)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--spec", type=Path, help="sofit clips.json holding word timings")
    ap.add_argument("--clip-id", help="clip id inside --spec")
    ap.add_argument("--layout", choices=sorted(LAYOUT_BOTTOM_FRAC), required=True,
                    help="caption position: split = at the two-shot seam, full = lower third")
    ap.add_argument("--caption-bottom-frac", type=float,
                    help="override the layout's caption-block-bottom / height fraction")
    ap.add_argument("--logo", type=Path, help="transparent PNG badge to overlay")
    ap.add_argument("--logo-pos", default="top-right",
                    choices=["top-left", "top-right", "bottom-left", "bottom-right"])
    ap.add_argument("--logo-width-frac", type=float, default=0.14)
    ap.add_argument("--music", type=Path, help="music bed, looped + ducked under speech")
    ap.add_argument("--font-size", type=int, default=64)
    ap.add_argument("--max-line-frac", type=float, default=MAX_W_FRAC,
                    help="caption line width / frame width (lower = shorter lines)")
    ap.add_argument("--chunk-max-dur", type=float, default=CHUNK_MAX_DUR,
                    help="max seconds a caption chunk stays before forcing a break")
    ap.add_argument("--chunk-max-words", type=int, default=CHUNK_MAX_WORDS,
                    help="max words per caption chunk (punchy = fewer)")
    ap.add_argument("--layout-map", type=Path,
                    help="JSON [{start,end,layout}] to position captions per span "
                         "(split=seam, full=lower third); from reaction_cut.py")
    ap.add_argument("--grain", type=int, nargs="?", const=8, default=0,
                    metavar="AMOUNT", help="subtle film grain + vignette (default 8)")
    ap.add_argument("--target-lufs", type=int, default=-14,
                    help="integrated loudness for the speech program (rubric ~-14)")
    ap.add_argument("--word-fixes", type=Path,
                    help='JSON [{"match","replace","near_t"}] whisper-artifact fixes')
    a = ap.parse_args()

    from PIL import Image, ImageDraw, ImageFont, ImageFilter, features
    if not features.check("raqm"):
        sys.exit("PIL lacks raqm — Hebrew shaping would be wrong. Use the sofit venv python.")
    if not FONT_PATH.exists():
        sys.exit(f"missing {FONT_PATH} — brew install --cask font-heebo")
    if a.spec and not a.clip_id:
        sys.exit("--spec requires --clip-id")

    dims = probe(a.video, "stream=width,height")
    if not dims:
        sys.exit(f"cannot probe {a.video}")
    src_w, src_h = (int(x) for x in dims.split("x")[:2])

    # --- words ---------------------------------------------------------------
    if a.spec:
        words = load_words_from_spec(a.spec, a.clip_id)
    else:
        print("transcribing (cached)...")
        words = load_words_from_transcription(a.video)
    words = tidy_words(words)
    if a.word_fixes:
        apply_word_fixes(words, json.loads(a.word_fixes.read_text(encoding="utf-8")))
    if not words:
        sys.exit("no words — nothing to caption")

    # --- typography ----------------------------------------------------------
    font = ImageFont.truetype(str(FONT_PATH), a.font_size)
    font.set_variation_by_axes([FONT_WEIGHT])
    line_h = int(a.font_size * LINE_SPACING)
    max_w = int(W * a.max_line_frac)
    chunk_max_dur = a.chunk_max_dur
    chunk_max_words = a.chunk_max_words
    default_frac = a.caption_bottom_frac or LAYOUT_BOTTOM_FRAC[a.layout]

    # Per-span caption position (reaction-cut): split spans -> seam (0.56H),
    # single spans -> lower third (0.74H). Falls back to the flat --layout.
    layout_spans = (json.loads(a.layout_map.read_text(encoding="utf-8"))
                    if a.layout_map else [])

    def bottom_frac_at(t: float) -> float:
        for s in layout_spans:
            if s["start"] <= t < s["end"]:
                return LAYOUT_BOTTOM_FRAC.get(s["layout"], default_frac)
        return default_frac
    measure = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    space_w = measure.textlength(" ", font=font)

    def tw(text: str) -> float:
        return measure.textlength(text, font=font)

    def wrap(ws: list[dict]) -> list[list[dict]]:
        lines, cur, cur_w = [], [], 0.0
        for w in ws:
            ww = tw(w["text"])
            add = ww + (space_w if cur else 0)
            if cur and cur_w + add > max_w:
                lines.append(cur)
                cur, cur_w = [w], ww
            else:
                cur.append(w)
                cur_w += add
        if cur:
            lines.append(cur)
        return lines

    # --- chunking: phrase blocks that fit two lines --------------------------
    def best_break(ws: list[dict]) -> int:
        """Split index (start of the carried tail), preferring a phrase
        boundary — punctuation or the biggest audible pause — near the end."""
        lo = max(1, len(ws) - 8)
        for i in range(len(ws) - 1, lo - 1, -1):
            if ws[i - 1]["text"][-1] in ".?!,":
                return i
        gaps = [(ws[i]["start"] - ws[i - 1]["end"], i) for i in range(lo, len(ws))]
        g, i = max(gaps)
        return i if g >= 0.25 else len(ws)

    chunks: list[list[dict]] = []
    cur: list[dict] = []
    for w in words:
        if cur:
            gap = w["start"] - cur[-1]["end"]
            dur = cur[-1]["end"] - cur[0]["start"]
            sentence_end = cur[-1]["text"][-1] in ".?!"
            if len(wrap(cur + [w])) > 2 or len(cur) >= chunk_max_words:
                cut = best_break(cur)
                chunks.append(cur[:cut])
                cur = cur[cut:]
            elif (gap > 0.5 and dur > 1.0) or (sentence_end and dur > 1.2) or dur > chunk_max_dur:
                chunks.append(cur)
                cur = []
        cur.append(w)
    if cur:
        chunks.append(cur)

    # --- overlay PNGs --------------------------------------------------------
    tmp = Path(tempfile.mkdtemp(prefix="podreel_"))
    overlays = []
    for ci, ch in enumerate(chunks):
        lines = wrap(ch)
        block_h = len(lines) * line_h
        pad = SHADOW_BLUR * 3
        img = Image.new("RGBA", (W, block_h + 2 * pad), (0, 0, 0, 0))
        shadow = Image.new("RGBA", img.size, (0, 0, 0, 0))
        sd, d = ImageDraw.Draw(shadow), ImageDraw.Draw(img)
        for li, line in enumerate(lines):
            text = " ".join(w["text"] for w in line)
            x = (W - tw(text)) / 2
            y = pad + li * line_h
            sd.text((x, y + SHADOW_DY), text, font=font,
                    fill=(0, 0, 0, SHADOW_ALPHA), direction="rtl")
            d.text((x, y), text, font=font, fill=(255, 255, 255, 255), direction="rtl")
        shadow = shadow.filter(ImageFilter.GaussianBlur(SHADOW_BLUR))
        img = Image.alpha_composite(shadow, img)
        path = tmp / f"chunk_{ci:02d}.png"
        img.save(path)
        bottom_y = int(H * bottom_frac_at(ch[0]["start"]))
        overlays.append({"path": path, "start": max(0.0, ch[0]["start"] - 0.05),
                         "end": ch[-1]["end"], "y": bottom_y - block_h - pad})
    for prev, nxt in zip(overlays, overlays[1:]):   # hold until the next caption
        prev["end"] = nxt["start"] if nxt["start"] - prev["end"] <= HOLD_MAX_GAP \
            else prev["end"] + 0.3
    overlays[-1]["end"] += 0.3

    # --- one ffmpeg pass -----------------------------------------------------
    inputs: list[str] = ["-i", str(a.video)]
    filters: list[str] = []
    vin = "0:v"
    if (src_w, src_h) != (W, H):
        filters.append(f"[0:v]scale={W}:{H}:force_original_aspect_ratio=decrease,"
                       f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2[vbase]")
        vin = "vbase"
        print(f"note: source is {src_w}x{src_h}, scaling/padding to {W}x{H}")
    # Every PNG input is `-loop 1` so it is a continuous stream, not a single
    # frame. Without this, `overlay ... enable='between(t,a,b)'` in a long chain
    # silently stops drawing later images (captions AND logo vanish partway
    # through — the more overlays, the earlier they drop).
    n_in = 1
    for i, ov in enumerate(overlays):
        inputs += ["-loop", "1", "-i", str(ov["path"])]
        out = f"v{i}"
        filters.append(f"[{vin}][{n_in}:v]overlay=0:{ov['y']}:"
                       f"enable='between(t,{ov['start']:.3f},{ov['end']:.3f})'[{out}]")
        vin, n_in = out, n_in + 1
    if a.logo:
        logo_w = int(W * a.logo_width_frac)
        x = f"{LOGO_MARGIN_X}" if "left" in a.logo_pos else f"W-w-{LOGO_MARGIN_X}"
        y = f"{LOGO_MARGIN_Y}" if "top" in a.logo_pos else f"H-h-{LOGO_MARGIN_Y}"
        inputs += ["-loop", "1", "-i", str(a.logo)]
        filters.append(f"[{n_in}:v]scale={logo_w}:-1[logo]")
        filters.append(f"[{vin}][logo]overlay={x}:{y}[vfin]")
        vin, n_in = "vfin", n_in + 1

    if a.grain:
        # subtle film texture + a light vignette for a premium feel
        filters.append(f"[{vin}]noise=alls={a.grain}:allf=t+u,"
                       "vignette=PI/5[vgr]")
        vin = "vgr"

    maps = ["-map", f"[{vin}]"]
    # Speech is normalized to ~-14 LUFS FIRST (Riverside exports run quiet, ~-23,
    # and sofit only loudnorms the bed) — this also restores the intended ~14dB
    # speech-to-bed gap before the duck.
    filters.append(f"[0:a]loudnorm=I={a.target_lufs}:TP=-1.5:LRA=11[speech]")
    if a.music:
        inputs += ["-stream_loop", "-1", "-i", str(a.music)]
        # same recipe as sofit's audiogram bed: quiet loudnorm + sidechain duck
        filters.append("[speech]asplit=2[a_key][a_mix]")
        filters.append(f"[{n_in}:a]loudnorm=I=-28:TP=-3:LRA=7[bed0]")
        filters.append("[bed0][a_key]sidechaincompress="
                       "threshold=0.03:ratio=4:attack=20:release=500[bed]")
        filters.append("[a_mix][bed]amix=inputs=2:duration=first:normalize=0[aout]")
        maps += ["-map", "[aout]"]
        audio_args = ["-c:a", "aac", "-b:a", "192k", "-shortest"]
    else:
        maps += ["-map", "[speech]"]
        # -shortest bounds the render to the finite base video, since the looped
        # PNG inputs are otherwise infinite streams.
        audio_args = ["-c:a", "aac", "-b:a", "192k", "-shortest"]

    a.out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-v", "error", *inputs,
                    "-filter_complex", ";".join(filters), *maps,
                    "-c:v", "libx264", "-crf", "18", "-preset", "medium",
                    *audio_args, "-movflags", "+faststart", "-y", str(a.out)],
                   check=True)

    print(f"chunks: {len(chunks)}")
    for ov, ch in zip(overlays, chunks):
        print(f"  {ov['start']:6.2f}-{ov['end']:6.2f}  {' '.join(w['text'] for w in ch)}")
    print("final ->", a.out)


if __name__ == "__main__":
    main()
