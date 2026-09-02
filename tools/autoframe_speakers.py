#!/usr/bin/env python3
"""Auto-frame 2-speaker podcast clips: decide per clip WHO is talking and set
that clip's `focus` (crop position) in a sofit clips.json — no manual input.

Why: sofit's face crop follows face PROMINENCE (size x confidence), not audio.
In a static two-shot both faces are always on screen, so clips can frame the
listener while the other person delivers the line. The audio here is mono-mixed
(L/R identical), so mic channels can't tell speakers apart either. What does:
LIP MOTION, gated by the transcript's word timings — measure mouth movement for
each seat only at voiced moments, and whoever's mouth moves more is the speaker.

Pipeline per spec:
  1. Learn the two "seats" (stable face x-positions) from frames sampled across
     the whole video.
  2. Per clip: extract frames over each rendered span, detect both faces
     (sofit's bundled YuNet model — mouth-corner landmarks included), measure
     frame-to-frame mouth-ROI motion per seat at voiced times only.
  3. Clear winner -> write `focus` = that seat's crop position (same formula as
     sofit's face crop). Ambiguous -> leave focus null so sofit's own
     prominence tracker + pan handles it (never worse than today).
  4. Write <stem>.autoframe.clips.json next to the input spec.

Then render, with no Claude / no re-transcription:
  sofit --render-from <stem>.autoframe.clips.json --render-clips <dir> --safe-area reels

Run with sofit's venv python (has cv2 + the bundled face model):
  "$HOME/Library/Application Support/pipx/venvs/sofit-cli/bin/python" \
      Tools/sofit/autoframe_speakers.py <clips.json>
"""
from __future__ import annotations

import json
import statistics
import subprocess
import sys
import tempfile
from pathlib import Path

import cv2
import numpy as np

from sofit.render import _FACE_MODEL, _crop_position_for_face, _target_resolution

DETECT_W = 640          # downscale width for detection frames
FPS = 6                 # sample rate inside spans (needs consecutive frames for motion)
SEAT_GAP = 0.12         # faces closer than this (x-fraction) are the same seat
VOICE_PAD = 0.15        # seconds of slack around each word interval
MOUTH_SIZE = (48, 24)   # normalized mouth ROI (w, h) for motion diffing
WIN_RATIO = 1.6         # winner needs this x the loser's motion energy
MIN_PAIRS = 8           # and at least this many voiced frame-pairs measured


def ffprobe_duration(video: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(video)], capture_output=True, text=True, check=True)
    return float(out.stdout.strip())


def extract_frames(video: Path, start: float, span: float, fps: float, tmp: str) -> list[Path]:
    pattern = str(Path(tmp) / "f_%04d.png")
    n = max(2, int(span * fps))
    subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", str(max(0.0, start)), "-t", str(span),
         "-i", str(video), "-vf", f"fps={fps},scale={DETECT_W}:-1",
         "-frames:v", str(n), "-y", pattern],
        capture_output=True, timeout=300, check=True)
    return sorted(Path(tmp).glob("f_*.png"))


def make_detector(w: int, h: int):
    return cv2.FaceDetectorYN.create(str(_FACE_MODEL), "", (w, h), score_threshold=0.7)


def detect(detector, img):
    """Return YuNet rows: bbox(4) + landmarks re,le,nose,rm,lm (10) + score."""
    detector.setInputSize((img.shape[1], img.shape[0]))
    _, dets = detector.detect(img)
    return [] if dets is None else list(dets)


def mouth_roi(img, det) -> np.ndarray | None:
    """Grayscale mouth crop from the two mouth-corner landmarks, normalized."""
    rx, ry, lx, ly = float(det[10]), float(det[11]), float(det[12]), float(det[13])
    cx, cy = (rx + lx) / 2, (ry + ly) / 2
    dist = max(np.hypot(lx - rx, ly - ry), 6.0)
    w, h = dist * 1.5, dist * 1.0
    x0, x1 = int(cx - w / 2), int(cx + w / 2)
    y0, y1 = int(cy - h * 0.45), int(cy + h * 0.55)
    x0, y0 = max(x0, 0), max(y0, 0)
    x1, y1 = min(x1, img.shape[1]), min(y1, img.shape[0])
    if x1 - x0 < 8 or y1 - y0 < 4:
        return None
    roi = cv2.cvtColor(img[y0:y1, x0:x1], cv2.COLOR_BGR2GRAY)
    roi = cv2.resize(roi, MOUTH_SIZE).astype(np.float32)
    return (roi - roi.mean()) / (roi.std() + 1e-6)   # illumination-invariant


def learn_seats(video: Path, duration: float, samples: int = 24) -> list[float]:
    """Cluster face x-positions across the video into stable seat centers."""
    xs: list[float] = []
    with tempfile.TemporaryDirectory(prefix="af_seats_") as tmp:
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
            for d in detect(detector, img):
                xs.append((float(d[0]) + float(d[2]) / 2) / img.shape[1])
    if not xs:
        sys.exit("no faces found while learning seats — is this the right video?")
    xs.sort()
    clusters: list[list[float]] = [[xs[0]]]
    for x in xs[1:]:
        if x - clusters[-1][-1] > SEAT_GAP:
            clusters.append([x])
        else:
            clusters[-1].append(x)
    clusters.sort(key=len, reverse=True)
    seats = sorted(statistics.median(c) for c in clusters[:2])
    return seats


def voiced_intervals(clip: dict) -> list[tuple[float, float]]:
    """Absolute (start, end) of every word, padded."""
    out = []
    for seg in clip.get("segments") or []:
        base = float(seg["start"])
        for w in seg.get("words") or []:
            t = base + float(w["t"])
            out.append((t - VOICE_PAD, t + float(w["d"]) + VOICE_PAD))
    return out


def is_voiced(t: float, intervals: list[tuple[float, float]]) -> bool:
    return any(a <= t <= b for a, b in intervals)


def clip_spans(clip: dict) -> list[tuple[float, float]]:
    segs = clip.get("segments") or []
    if segs:
        return [(float(s["start"]), float(s["end"])) for s in segs]
    return [(float(clip["start"]), float(clip["end"]))]


def score_clip(video: Path, clip: dict, seats: list[float]) -> tuple[list[float], list[int], dict[int, list[float]]]:
    """Accumulate voiced mouth-motion energy per seat over the clip's spans.

    Returns (energy per seat, frame-pair counts per seat, seat -> detected cx list).
    """
    voiced = voiced_intervals(clip)
    energy = [0.0] * len(seats)
    pairs = [0] * len(seats)
    seat_cx: dict[int, list[float]] = {i: [] for i in range(len(seats))}

    for span_start, span_end in clip_spans(clip):
        span = span_end - span_start
        if span <= 0.4:
            continue
        with tempfile.TemporaryDirectory(prefix="af_clip_") as tmp:
            frames = extract_frames(video, span_start, span, FPS, tmp)
            detector = None
            prev: dict[int, np.ndarray] = {}
            for i, fp in enumerate(frames):
                img = cv2.imread(str(fp))
                if img is None:
                    prev = {}
                    continue
                if detector is None:
                    detector = make_detector(img.shape[1], img.shape[0])
                t_abs = span_start + (i + 0.5) / FPS
                cur: dict[int, np.ndarray] = {}
                for d in detect(detector, img):
                    cx = (float(d[0]) + float(d[2]) / 2) / img.shape[1]
                    seat = min(range(len(seats)), key=lambda s: abs(seats[s] - cx))
                    if abs(seats[seat] - cx) > SEAT_GAP:
                        continue
                    roi = mouth_roi(img, d)
                    if roi is None:
                        continue
                    cur[seat] = roi
                    seat_cx[seat].append(cx)
                if is_voiced(t_abs, voiced):
                    for seat, roi in cur.items():
                        if seat in prev:
                            energy[seat] += float(np.mean(np.abs(roi - prev[seat])))
                            pairs[seat] += 1
                prev = cur
    return energy, pairs, seat_cx


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    spec_path = Path(sys.argv[1]).expanduser()
    doc = json.loads(spec_path.read_text(encoding="utf-8"))
    video = Path(doc["source"]["video"])
    if not video.exists():
        sys.exit(f"source video not found: {video}")

    duration = ffprobe_duration(video)
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0", str(video)],
        capture_output=True, text=True, check=True).stdout.strip().split(",")
    src_w, src_h = int(probe[0]), int(probe[1])
    tw, th = _target_resolution("9:16")
    crop_w_frac = (tw / th) * (src_h / src_w)

    seats = learn_seats(video, duration)
    if len(seats) < 2:
        sys.exit(f"only one seat found ({seats}) — single-speaker video, nothing to do.")
    print(f"seats (x-fraction): left={seats[0]:.2f}  right={seats[1]:.2f}")

    for clip in doc["clips"]:
        energy, pairs, seat_cx = score_clip(video, clip, seats)
        total_pairs = sum(pairs)
        line = f"{clip['id']}: L={energy[0]:.2f}({pairs[0]}p) R={energy[1]:.2f}({pairs[1]}p)"
        lo, hi = sorted(range(2), key=lambda s: energy[s])
        if total_pairs >= MIN_PAIRS and energy[hi] > WIN_RATIO * max(energy[lo], 1e-6):
            cx = statistics.median(seat_cx[hi]) if seat_cx[hi] else seats[hi]
            clip["focus"] = round(_crop_position_for_face(cx, crop_w_frac), 3)
            who = "left" if hi == 0 else "right"
            print(f"  {line} -> {who} speaks, focus={clip['focus']}")
        else:
            clip["focus"] = None
            print(f"  {line} -> ambiguous, focus=null (sofit tracker decides)")

    out = spec_path.with_name(spec_path.name.replace(".clips.json", ".autoframe.clips.json"))
    out.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nwrote {out}")
    print(f"render: sofit --render-from \"{out}\" --render-clips <dir> --safe-area reels")


if __name__ == "__main__":
    main()
