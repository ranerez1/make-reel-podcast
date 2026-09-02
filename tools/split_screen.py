#!/usr/bin/env python3
"""Split-screen "teaser style" renderer for 2-speaker sofit clips.

Turns a sofit clips.json into stacked-two-shot vertical clips: one speaker
close-up on top, the other on the bottom, captions at the seam — the layout of
the professionally-edited teaser. Both speakers are ALWAYS in frame, so no
active-speaker detection is needed at all (contrast: autoframe_speakers.py,
which picks one speaker per clip for the full-frame style).

How: learn the two seats (face x/y/size) from frames sampled across the video,
build ONE split-screen master via ffmpeg — only the clip spans, concatenated,
padded, with a piecewise time remap — then rewrite the spec's times against the
master and hand off to sofit's own renderer (captions, word highlight, logo).

Usage (sofit's venv python — it has cv2 + the bundled face model):
  VENV="$HOME/Library/Application Support/pipx/venvs/sofit-cli/bin/python"
  "$VENV" Tools/sofit/split_screen.py <clips.json> [--render DIR] [--top right|left]

Outputs, next to the input spec:
  <stem>.split.mp4         — the split-screen master (spans only, small)
  <stem>.split.clips.json  — spec remapped to the master, focus=0.5
With --render DIR it also runs:
  sofit --render-from <spec> --render-clips DIR --no-hook-card
(hook cards are skipped — they'd cover the top speaker's face; captions carry
the words. CTA is dropped for the same reason. Caption seam position comes from
the SOFIT_CAPTION_BOTTOM_FRAC patch knob in brand_captions_patch.py.)
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import tempfile
from pathlib import Path

import cv2

sys.path.insert(0, str(Path(__file__).resolve().parent))
from autoframe_speakers import DETECT_W, SEAT_GAP, detect, make_detector  # noqa: E402

PANE_W, PANE_H = 1080, 960     # each pane; stacked -> 1080x1920
ZOOM = 3.5                     # crop height = ZOOM x median face height
FACE_Y_IN_PANE = 0.42          # face center sits at this fraction of pane height
SPAN_PAD = 0.3                 # seconds kept around each span in the master
CAPTION_SEAM_FRAC = "0.44"     # SOFIT_CAPTION_BOTTOM_FRAC -> captions at the seam


def probe(video: Path) -> tuple[int, int, float]:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(video)], capture_output=True, text=True, check=True)
    lines = out.stdout.strip().splitlines()
    w, h = (int(x) for x in lines[0].split(",")[:2])
    return w, h, float(lines[-1])


def learn_seats(video: Path, duration: float, samples: int = 24) -> list[dict]:
    """Two seats, each {cx, cy, fh} (fractions of the source frame)."""
    dets: list[tuple[float, float, float]] = []
    with tempfile.TemporaryDirectory(prefix="ss_seats_") as tmp:
        pattern = str(Path(tmp) / "s_%03d.png")
        subprocess.run(
            ["ffmpeg", "-v", "error", "-i", str(video),
             "-vf", f"fps={samples}/{duration},scale={DETECT_W}:-1",
             "-frames:v", str(samples), "-y", pattern],
            capture_output=True, timeout=600, check=True)
        detector = None
        for fp in sorted(Path(tmp).glob("s_*.png")):
            img = cv2.imread(str(fp))
            if img is None:
                continue
            if detector is None:
                detector = make_detector(img.shape[1], img.shape[0])
            h, w = img.shape[:2]
            for d in detect(detector, img):
                dets.append(((float(d[0]) + float(d[2]) / 2) / w,
                             (float(d[1]) + float(d[3]) / 2) / h,
                             float(d[3]) / h))
    if not dets:
        sys.exit("no faces found while learning seats")
    dets.sort()
    clusters: list[list[tuple[float, float, float]]] = [[dets[0]]]
    for d in dets[1:]:
        if d[0] - clusters[-1][-1][0] > SEAT_GAP:
            clusters.append([d])
        else:
            clusters[-1].append(d)
    clusters.sort(key=len, reverse=True)
    if len(clusters) < 2:
        sys.exit("only one seat found — split-screen needs a two-shot")
    seats = []
    for c in sorted(clusters[:2], key=lambda c: statistics.median(x[0] for x in c)):
        seats.append({"cx": statistics.median(x[0] for x in c),
                      "cy": statistics.median(x[1] for x in c),
                      "fh": statistics.median(x[2] for x in c)})
    return seats  # [left, right]


def pane_crop(seat: dict, src_w: int, src_h: int) -> tuple[int, int, int, int]:
    """(w, h, x, y) crop window for one seat's pane."""
    ch = seat["fh"] * src_h * ZOOM
    ch = max(0.40 * src_h, min(ch, 0.92 * src_h))
    cw = ch * PANE_W / PANE_H
    if cw > src_w:
        cw = src_w
        ch = cw * PANE_H / PANE_W
    cw, ch = int(cw // 2 * 2), int(ch // 2 * 2)
    x = int(min(max(seat["cx"] * src_w - cw / 2, 0), src_w - cw))
    y = int(min(max(seat["cy"] * src_h - FACE_Y_IN_PANE * ch, 0), src_h - ch))
    return cw, ch, x, y


def clip_spans(clip: dict) -> list[tuple[float, float]]:
    segs = clip.get("segments")
    if segs:
        return [(float(s["start"]), float(s["end"])) for s in segs]
    return [(float(clip["start"]), float(clip["end"]))]


def merge_spans(spans: list[tuple[float, float]], duration: float) -> list[tuple[float, float]]:
    padded = sorted((max(0.0, a - SPAN_PAD), min(duration, b + SPAN_PAD)) for a, b in spans)
    merged = [list(padded[0])]
    for a, b in padded[1:]:
        if a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return [(a, b) for a, b in merged]


class TimeMap:
    def __init__(self, pieces: list[tuple[float, float]]):
        self.pieces, self.offsets = pieces, []
        o = 0.0
        for a, b in pieces:
            self.offsets.append(o)
            o += b - a

    def __call__(self, t: float) -> float:
        for (a, b), o in zip(self.pieces, self.offsets):
            if a - 1e-6 <= t <= b + 1e-6:
                return round(o + t - a, 3)
        sys.exit(f"time {t} not covered by any master piece — span merge bug")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("spec")
    ap.add_argument("--render", metavar="DIR", help="also render via sofit --render-from")
    ap.add_argument("--top", choices=["right", "left"], default="right",
                    help="which seat is the TOP pane (default right = guest seat "
                         "in the Productivy studio layout)")
    args = ap.parse_args()

    spec_path = Path(args.spec).expanduser()
    doc = json.loads(spec_path.read_text(encoding="utf-8"))
    video = Path(doc["source"]["video"])
    if not video.exists():
        sys.exit(f"source video not found: {video}")
    src_w, src_h, duration = probe(video)

    seats = learn_seats(video, duration)
    print(f"seats: left cx={seats[0]['cx']:.2f} cy={seats[0]['cy']:.2f}  "
          f"right cx={seats[1]['cx']:.2f} cy={seats[1]['cy']:.2f}")

    all_spans = [sp for c in doc["clips"] for sp in clip_spans(c)]
    pieces = merge_spans(all_spans, duration)
    tmap = TimeMap(pieces)
    total = sum(b - a for a, b in pieces)
    print(f"master: {len(pieces)} piece(s), {total:.0f}s of {duration:.0f}s source")

    master = spec_path.with_name(spec_path.name.replace(".clips.json", "") + ".split.mp4")
    ordered = [seats[1], seats[0]] if args.top == "right" else [seats[0], seats[1]]
    crops = [pane_crop(s, src_w, src_h) for s in ordered]  # [top, bottom]
    parts, vlabels, alabels = [], [], []
    for i, (a, b) in enumerate(pieces):
        parts.append(f"[0:v]trim=start={a}:end={b},setpts=PTS-STARTPTS[v{i}]")
        parts.append(f"[0:a]atrim=start={a}:end={b},asetpts=PTS-STARTPTS[a{i}]")
        vlabels.append(f"[v{i}]"); alabels.append(f"[a{i}]")
    parts.append("".join(v + a for v, a in zip(vlabels, alabels)) +
                 f"concat=n={len(pieces)}:v=1:a=1[vc][ac]")
    parts.append("[vc]split=2[st][sb]")
    for label, (cw, ch, x, y), pane in (("st", crops[0], "pt"), ("sb", crops[1], "pb")):
        parts.append(f"[{label}]crop={cw}:{ch}:{x}:{y},scale={PANE_W}:{PANE_H}[{pane}]")
    parts.append("[pt][pb]vstack=inputs=2[v]")
    print("encoding split master (one pass)...")
    subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(video), "-filter_complex", ";".join(parts),
         "-map", "[v]", "-map", "[ac]",
         "-c:v", "libx264", "-crf", "20", "-preset", "fast", "-c:a", "aac",
         "-y", str(master)], check=True)

    # remap the spec onto the master's timeline
    for clip in doc["clips"]:
        if clip.get("segments"):
            for seg in clip["segments"]:
                seg["start"], seg["end"] = tmap(float(seg["start"])), tmap(float(seg["end"]))
            clip["start"] = min(s["start"] for s in clip["segments"])
            clip["end"] = max(s["end"] for s in clip["segments"])
        else:
            clip["start"], clip["end"] = tmap(float(clip["start"])), tmap(float(clip["end"]))
        clip["focus"] = 0.5  # master is already 9:16 -> identity crop, skip face-track
    doc["source"]["video"] = str(master)
    out_spec = spec_path.with_name(spec_path.name.replace(".clips.json", ".split.clips.json"))
    out_spec.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {master.name} + {out_spec.name}")

    if args.render:
        env = dict(os.environ)
        env["SOFIT_CAPTION_BOTTOM_FRAC"] = env.get("SOFIT_CAPTION_BOTTOM_FRAC", CAPTION_SEAM_FRAC)
        env.pop("SOFIT_CTA", None)      # CTA draws over the top speaker's face
        env.pop("SOFIT_COVER", None)
        sofit_bin = Path(sys.executable).with_name("sofit")
        subprocess.run(
            [str(sofit_bin), "--render-from", str(out_spec),
             "--render-clips", args.render, "--no-hook-card"],
            env=env, check=True)
        print(f"rendered to {args.render}")
    else:
        print(f"render: sofit --render-from \"{out_spec}\" --render-clips <dir> --no-hook-card")


if __name__ == "__main__":
    main()
