#!/usr/bin/env python3
"""Propose podcast reel cuts with a WIDER duration window than sofit's default.

sofit's `--clips-json` hardcodes 18-45s (`make_quotes` defaults) and its Claude
prompt says "about 20-45 seconds". Podcast reels may run up to 60s, so this
driver calls the same sofit internals with the window as a parameter and aligns
the prompt text at runtime — no patching of installed files, and /make-reel's
45s behavior is untouched.

Run with the sofit venv python:
  SOFIT_CLI_TIMEOUT=900 "$HOME/Library/Application Support/pipx/venvs/sofit-cli/bin/python" \
      Tools/sofit/podcast_clips.py <video.mp4> --out <slug>.clips.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from sofit import generate
from sofit.transcribe import transcribe


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("video", type=Path)
    ap.add_argument("--out", type=Path, required=True, help="clips.json to write")
    ap.add_argument("--min-sec", type=float, default=18.0)
    ap.add_argument("--max-sec", type=float, default=60.0)
    ap.add_argument("--titler", default="claude-cli", choices=["api", "claude-cli"])
    a = ap.parse_args()

    # Align the selection prompt with the wider window, patch-style: fail loudly
    # if upstream rewords the rule so a silent 45s bias can't sneak back in.
    old = "total about 20-45"
    if old not in generate.CLIP_RULES:
        sys.exit("sofit's CLIP_RULES no longer says 'total about 20-45' — "
                 "update Tools/sofit/podcast_clips.py for the new prompt text")
    generate.CLIP_RULES = generate.CLIP_RULES.replace(
        old, f"total about 20-{int(a.max_sec)}")

    print(f"transcribing (cached) + selecting clips ({a.min_sec:.0f}-{a.max_sec:.0f}s window)...")
    segments = transcribe(str(a.video))
    if not segments:
        sys.exit("no speech found")
    quotes = generate.make_quotes(segments, titler=a.titler,
                                  min_sec=a.min_sec, max_sec=a.max_sec)
    if not quotes:
        sys.exit("no clips passed the score/length gates")
    clips = [generate.clip_spec(q, segments, f"clip-{i}")
             for i, q in enumerate(quotes, 1)]

    doc = {"schema_version": 1,
           "source": {"video": os.path.abspath(str(a.video))},
           "clips": clips}
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n{len(clips)} candidates -> {a.out}")
    for c in clips:
        dur = (sum(s["end"] - s["start"] for s in c["segments"])
               if c.get("segments") else c["end"] - c["start"])
        print(f"  {c['id']:8s} {dur:5.1f}s  {c.get('hook', '')}")


if __name__ == "__main__":
    main()
