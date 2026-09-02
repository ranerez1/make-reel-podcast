#!/usr/bin/env python3
"""Reaction-cut master builder — dynamic split/single framing for podcast reels.

Turns continuous per-participant feeds into a 9:16 master that alternates
SINGLE (active-speaker close-up) with SPLIT (both people stacked) — the way a
human editor cuts reaction shots. Pre-bakes the framing into a master (sofit's
spec has no per-segment framing), which `podcast_reel.py` then captions/brands.

Phase 1 — multitrack mode (premium; matches an editor's reaction cutting):
  reaction_cut.py --tracks A.mp4 B.mp4 --window 60 95 --out master.mp4
Each track is a synced single-person Riverside feed with its OWN mic, so the
active speaker is read from per-track audio RMS (robust — no lip-motion guess).

Run with the sofit venv python (needs cv2 + the bundled face model):
  "$HOME/Library/Application Support/pipx/venvs/sofit-cli/bin/python" \
      Tools/sofit/reaction_cut.py --tracks ben.mp4 ran.mp4 --window 60 95 --out m.mp4
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import cv2
import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from autoframe_speakers import DETECT_W, detect, make_detector  # noqa: E402

VENV_SOFIT = HERE  # sofit.render importable when run via the venv python
from sofit.render import _crop_position_for_face  # noqa: E402
import split_screen as ss  # noqa: E402  (learn_seats/pane_crop for single-edit wide splits)

OUT_W, OUT_H = 1080, 1920
PANE_W, PANE_H = 1080, 960          # each split pane; stacked -> 1080x1920
ZOOM = 3.5                          # crop height = ZOOM x face height
FACE_Y_IN_PANE = 0.42               # face sits this far down its pane
SINGLE_FACE_Y = 0.40                # face this far down a full single frame
# Matched encode profile — every span MUST share these so the concat demuxer
# can stream-copy without a re-encode (identical codec/pixfmt/rate/timebase).
V_ENC = ["-c:v", "libx264", "-preset", "medium", "-crf", "18",
         "-pix_fmt", "yuv420p", "-r", "25", "-video_track_timescale", "12800",
         "-x264-params", "keyint=50:min-keyint=50:scenecut=0"]
A_ENC = ["-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2"]

RMS_SR = 8000                       # audio decode rate for energy analysis
RMS_WIN = 0.10                      # seconds per energy window
SWITCH_RATIO = 1.3                  # one mic must be this x louder to grab focus
SILENCE_RMS = 0.006                 # below this on both mics = keep previous


def ff(cmd: list) -> None:
    subprocess.run([str(c) for c in cmd], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def probe_dims(video: Path) -> tuple[int, int]:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=width,height", "-of", "csv=p=0:s=x", str(video)],
        capture_output=True, text=True).stdout.strip()
    w, h = (int(x) for x in out.split("x")[:2])
    return w, h


# --- per-track face center --------------------------------------------------
def face_stats(video: Path, t0: float, t1: float, samples: int = 12) -> dict:
    """Median dominant-face {cx, cy, fh} (fractions) across the window, for one
    single-person track. Falls back to a centered guess when no face is found."""
    dur = max(0.5, t1 - t0)
    cx, cy, fh = [], [], []
    with tempfile.TemporaryDirectory(prefix="rc_face_") as tmp:
        pat = str(Path(tmp) / "f_%03d.png")
        ff(["ffmpeg", "-v", "error", "-ss", t0, "-t", dur, "-i", video,
            "-vf", f"fps={samples}/{dur},scale={DETECT_W}:-1",
            "-frames:v", samples, "-y", pat])
        detector = None
        for fp in sorted(Path(tmp).glob("f_*.png")):
            img = cv2.imread(str(fp))
            if img is None:
                continue
            if detector is None:
                detector = make_detector(img.shape[1], img.shape[0])
            h, w = img.shape[:2]
            faces = detect(detector, img)
            if not faces:
                continue
            d = max(faces, key=lambda d: float(d[2]) * float(d[3]))  # biggest face
            cx.append((float(d[0]) + float(d[2]) / 2) / w)
            cy.append((float(d[1]) + float(d[3]) / 2) / h)
            fh.append(float(d[3]) / h)
    if not cx:
        return {"cx": 0.5, "cy": 0.42, "fh": 0.22}
    return {"cx": float(np.median(cx)), "cy": float(np.median(cy)),
            "fh": float(np.median(fh))}


# --- per-track audio energy -------------------------------------------------
def track_rms(video: Path, t0: float, t1: float, offset: float = 0.0) -> np.ndarray:
    """Windowed RMS envelope over [t0,t1] for one track's mic (mono)."""
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", str(t0 + offset), "-t", str(t1 - t0),
         "-i", str(video), "-ac", "1", "-ar", str(RMS_SR), "-f", "f32le", "-"],
        capture_output=True).stdout
    a = np.frombuffer(raw, dtype=np.float32)
    n = int(RMS_SR * RMS_WIN)
    if a.size < n:
        return np.zeros(1, dtype=np.float32)
    w = a[: a.size // n * n].reshape(-1, n)
    return np.sqrt((w * w).mean(axis=1))


def active_timeline(tracks: list[Path], t0: float, t1: float,
                    offsets: list[float]) -> np.ndarray:
    """Per-window active-speaker index (0/1), with hysteresis so back-channel
    blips don't flip focus. Silence keeps the previous speaker."""
    rms = [track_rms(t, t0, t1, off) for t, off in zip(tracks, offsets)]
    n = min(len(r) for r in rms)
    a, b = rms[0][:n], rms[1][:n]
    active = np.zeros(n, dtype=int)
    cur = 0
    for i in range(n):
        if max(a[i], b[i]) < SILENCE_RMS:
            active[i] = cur
            continue
        if a[i] > SWITCH_RATIO * b[i]:
            cur = 0
        elif b[i] > SWITCH_RATIO * a[i]:
            cur = 1
        active[i] = cur
    return active


# --- framing timeline -------------------------------------------------------
def framing_plan(active: np.ndarray, dur: float, hook_split: float,
                 min_span: float, transition_split: float = 1.0) -> list[dict]:
    """Spans of {start,end,mode,speaker}. SPLIT for the opening hook, around
    every speaker handoff (so the cut between people shows both faces, the way
    an editor does — never a bare hard cut), and for rapid exchanges; SINGLE
    (active speaker) for stable talking stretches."""
    win = dur / len(active)
    # mark a split zone within +/-transition_split of every speaker change
    kt = max(1, int(transition_split / win))
    kd = max(1, int(2.0 / win))
    split_zone = np.zeros(len(active), dtype=bool)
    density = np.zeros(len(active), dtype=int)
    for i in range(1, len(active)):
        if active[i] != active[i - 1]:
            split_zone[max(0, i - kt): i + kt] = True         # handoff -> show both
            density[max(0, i - kd): i + kd] += 1
    raw = []  # per-window (mode, speaker)
    for i, sp in enumerate(active):
        t = i * win
        if t < hook_split or split_zone[i] or density[i] >= 2:
            raw.append(("split", int(sp)))
        else:
            raw.append(("single", int(sp)))
    # coalesce equal neighbors into spans
    spans = []
    for i, (mode, sp) in enumerate(raw):
        t = i * win
        key = (mode, sp if mode == "single" else -1)
        if spans and spans[-1]["_key"] == key:
            spans[-1]["end"] = t + win
        else:
            spans.append({"start": t, "end": t + win, "mode": mode,
                          "speaker": sp, "_key": key})
    # merge spans shorter than min_span into the previous
    merged = [spans[0]]
    for s in spans[1:]:
        if s["end"] - s["start"] < min_span:
            merged[-1]["end"] = s["end"]
        else:
            merged.append(s)
    if merged[0]["end"] - merged[0]["start"] < min_span and len(merged) > 1:
        merged[1]["start"] = merged[0]["start"]
        merged.pop(0)
    for s in merged:
        s.pop("_key", None)
    return merged


# --- span rendering ---------------------------------------------------------
def crop_expr_single(fs: dict, src_w: int, src_h: int) -> str:
    """9:16 crop of a full frame centered on the face."""
    crop_w_frac = (OUT_W / OUT_H) * (src_h / src_w)
    cp = _crop_position_for_face(fs["cx"], crop_w_frac)
    cw = int(src_h * OUT_W / OUT_H) // 2 * 2
    ch = src_h
    x = int(min(max((src_w - cw) * cp, 0), src_w - cw))
    return f"crop={cw}:{ch}:{x}:0,scale={OUT_W}:{OUT_H}"


def pane_crop(fs: dict, src_w: int, src_h: int) -> str:
    """Crop+scale one track to a 1080x960 pane centered on the face."""
    ch = fs["fh"] * src_h * ZOOM
    ch = max(0.40 * src_h, min(ch, 0.92 * src_h))
    cw = ch * PANE_W / PANE_H
    if cw > src_w:
        cw, ch = src_w, src_w * PANE_H / PANE_W
    cw, ch = int(cw // 2 * 2), int(ch // 2 * 2)
    x = int(min(max(fs["cx"] * src_w - cw / 2, 0), src_w - cw))
    y = int(min(max(fs["cy"] * src_h - FACE_Y_IN_PANE * ch, 0), src_h - ch))
    return f"crop={cw}:{ch}:{x}:{y},scale={PANE_W}:{PANE_H}"


# --- single published-edit path (best-effort) -------------------------------
def facecount_timeline(video: Path, t0: float, t1: float, fps: int = 4) -> np.ndarray:
    """Faces per sampled frame across [t0,t1] — labels wide(>=2) vs solo shots
    in a published multicam edit (split only possible on wide two-shots)."""
    dur = max(0.5, t1 - t0)
    counts = []
    with tempfile.TemporaryDirectory(prefix="rc_fc_") as tmp:
        pat = str(Path(tmp) / "c_%04d.png")
        ff(["ffmpeg", "-v", "error", "-ss", t0, "-t", dur, "-i", video,
            "-vf", f"fps={fps},scale={DETECT_W}:-1", "-y", pat])
        detector = None
        for fp in sorted(Path(tmp).glob("c_*.png")):
            img = cv2.imread(str(fp))
            if img is None:
                continue
            if detector is None:
                detector = make_detector(img.shape[1], img.shape[0])
            counts.append(len(detect(detector, img)))
    return np.array(counts or [1], dtype=int)


def plan_singleedit(video: Path, bs: float, be: float, hook_split: float,
                    min_span: float) -> list[dict]:
    """Framing spans for a single video: SPLIT only where the source is a wide
    two-shot (>=2 faces), else SINGLE. Splits during the hook + wide stretches."""
    fc = facecount_timeline(video, bs, be)
    win = (be - bs) / len(fc)
    raw = []
    for i, c in enumerate(fc):
        t = i * win
        raw.append("split" if (c >= 2 and (t < hook_split or True)) else "single")
    spans = []
    for i, mode in enumerate(raw):
        t = bs + i * win
        if spans and spans[-1]["mode"] == mode:
            spans[-1]["end"] = t + win
        else:
            spans.append({"start": t, "end": t + win, "mode": mode, "speaker": 0})
    merged = [spans[0]]
    for s in spans[1:]:
        if s["end"] - s["start"] < min_span:
            merged[-1]["end"] = s["end"]
        else:
            merged.append(s)
    return merged


def two_seats(video: Path, t0: float, t1: float, samples: int = 12):
    """Two seat centers {cx,cy,fh} learned FROM THE SPAN [t0,t1] (seeked), or
    None if the span isn't a stable two-shot. Own impl because split_screen's
    learn_seats samples from the video start, not the span."""
    dur = max(0.5, t1 - t0)
    dets = []
    with tempfile.TemporaryDirectory(prefix="rc_seat_") as tmp:
        pat = str(Path(tmp) / "s_%03d.png")
        ff(["ffmpeg", "-v", "error", "-ss", t0, "-t", dur, "-i", video,
            "-vf", f"fps={samples}/{dur},scale={DETECT_W}:-1", "-frames:v", samples,
            "-y", pat])
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
                             (float(d[1]) + float(d[3]) / 2) / h, float(d[3]) / h))
    if len(dets) < 2:
        return None
    dets.sort()
    clusters = [[dets[0]]]
    for d in dets[1:]:
        (clusters[-1].append(d) if d[0] - clusters[-1][-1][0] <= 0.12
         else clusters.append([d]))
    clusters.sort(key=len, reverse=True)
    if len(clusters) < 2:
        return None
    seats = [{"cx": float(np.median([x[0] for x in c])),
              "cy": float(np.median([x[1] for x in c])),
              "fh": float(np.median([x[2] for x in c]))}
             for c in sorted(clusters[:2], key=lambda c: np.median([x[0] for x in c]))]
    return seats


def render_span_singleedit(span: dict, video: Path, dim: tuple[int, int],
                           out: Path) -> None:
    t0 = span["source_start"]
    t1 = t0 + span["source_dur"]
    src_w, src_h = dim
    ins = ["-ss", f"{t0:.3f}", "-t", f"{span['source_dur']:.3f}", "-i", str(video)]
    seats = two_seats(video, t0, t1) if span["mode"] == "split" else None
    if seats is None:   # single, or a split whose two-shot couldn't be learned
        fs = face_stats(video, t0, t1)
        fc = f"[0:v]{crop_expr_single(fs, src_w, src_h)}[v]"
    else:  # wide two-shot -> two panes from L/R of the same frame
        lt = ss.pane_crop(seats[1], src_w, src_h)   # right seat on top
        lb = ss.pane_crop(seats[0], src_w, src_h)
        fc = (f"[0:v]split=2[a][b];"
              f"[a]crop={lt[0]}:{lt[1]}:{lt[2]}:{lt[3]},scale={PANE_W}:{PANE_H}[pt];"
              f"[b]crop={lb[0]}:{lb[1]}:{lb[2]}:{lb[3]},scale={PANE_W}:{PANE_H}[pb];"
              f"[pt][pb]vstack=inputs=2[v]")
    nf = round(span["source_dur"] * 25)
    ff(["ffmpeg", "-v", "error", *ins, "-filter_complex", fc, "-map", "[v]", "-an",
        *V_ENC, "-frames:v", str(nf), "-movflags", "+faststart", "-y", str(out)])


def render_span(span: dict, tracks: list[Path], offsets: list[float],
                dims: list[tuple[int, int]], out: Path) -> None:
    """VIDEO-ONLY span render (audio is built once, muxed later — so per-segment
    AAC priming never accumulates into A/V drift across the concat)."""
    t0, t1 = span["source_start"], span["source_start"] + span["source_dur"]
    ins = []
    for tr, off in zip(tracks, offsets):
        ins += ["-ss", f"{t0 + off:.3f}", "-t", f"{span['source_dur']:.3f}", "-i", str(tr)]
    if span["mode"] == "single":
        k = span["speaker"]
        fs = face_stats(tracks[k], t0, t1)
        fc = f"[{k}:v]{crop_expr_single(fs, *dims[k])}[v]"
    else:  # split — active speaker pane on top
        top, bot = (span["speaker"], 1 - span["speaker"])
        ft = face_stats(tracks[top], t0, t1)
        fb = face_stats(tracks[bot], t0, t1)
        fc = (f"[{top}:v]{pane_crop(ft, *dims[top])}[pt];"
              f"[{bot}:v]{pane_crop(fb, *dims[bot])}[pb];"
              f"[pt][pb]vstack=inputs=2[v]")
    nf = round(span["source_dur"] * 25)   # exact output frames == audio dur * 25
    ff(["ffmpeg", "-v", "error", *ins, "-filter_complex", fc, "-map", "[v]", "-an",
        *V_ENC, "-frames:v", str(nf), "-movflags", "+faststart", "-y", str(out)])


def build_audio(spans: list[dict], inputs: list[str], offsets: list[float],
                out: Path) -> None:
    """Build the whole master audio in ONE encode: per span, trim the source
    mic(s) at the span's exact source window, mix, then concat — one AAC encode,
    so there is no per-segment priming drift. `inputs` are the -i args (2 mic
    tracks for multitrack, 1 video for single-edit)."""
    two = inputs.count("-i") >= 2   # single-edit has one input -> no amix
    fc, labels = [], []
    for i, sp in enumerate(spans):
        s = sp["source_start"]
        e = s + sp["source_dur"]
        if two:
            fc.append(f"[0:a]atrim={s:.3f}:{e:.3f},asetpts=PTS-STARTPTS[x{i}]")
            fc.append(f"[1:a]atrim={s + offsets[1]:.3f}:{e + offsets[1]:.3f},"
                      f"asetpts=PTS-STARTPTS[y{i}]")
            fc.append(f"[x{i}][y{i}]amix=inputs=2:normalize=0[m{i}]")
        else:
            fc.append(f"[0:a]atrim={s:.3f}:{e:.3f},asetpts=PTS-STARTPTS[m{i}]")
        labels.append(f"[m{i}]")
    fc.append("".join(labels) + f"concat=n={len(spans)}:v=0:a=1[a]")
    ff(["ffmpeg", "-v", "error", *inputs, "-filter_complex", ";".join(fc),
        "-map", "[a]", *A_ENC, "-y", str(out)])


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--tracks", nargs="+", type=Path,
                     help="premium: 2 synced per-participant videos (own mic each)")
    src.add_argument("--video", type=Path,
                     help="best-effort: one published multicam edit (wide-only splits)")
    ap.add_argument("--window", nargs=2, type=float, metavar=("START", "END"),
                    help="absolute seconds to cut (testing; else --spec)")
    ap.add_argument("--spec", type=Path, help="clips.json for the chosen cut")
    ap.add_argument("--clip-id")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--sync-offset", type=float, default=0.0,
                    help="seconds to shift track 2 relative to track 1")
    ap.add_argument("--hook-split-sec", type=float, default=3.0)
    ap.add_argument("--transition-split-sec", type=float, default=1.0,
                    help="split-screen window around each speaker handoff (0 = hard cut)")
    ap.add_argument("--min-span", type=float, default=1.5)
    ap.add_argument("--plan-out", type=Path, help="write the framing plan JSON")
    ap.add_argument("--words-out", type=Path,
                    help="write a clips.json of master-timeline words (for podcast_reel --spec)")
    ap.add_argument("--layout-out", type=Path,
                    help="write the per-span caption layout map (for podcast_reel --layout-map)")
    a = ap.parse_args()

    multitrack = a.tracks is not None
    if multitrack and len(a.tracks) != 2:
        sys.exit("only 2 tracks are supported")
    offsets = [0.0, a.sync_offset]
    if multitrack:
        dims = [probe_dims(t) for t in a.tracks]
    else:
        dim = probe_dims(a.video)

    # Beats to frame: a clip's segments (source time) — or a raw --window for
    # phase-1 testing. Each beat is framed independently; the master concatenates
    # them, so master time = cumulative beat offset (no within-beat trimming yet).
    beats: list[dict] = []      # {s, e, words(clip-relative), hook}
    if a.spec:
        if not a.clip_id:
            sys.exit("--spec requires --clip-id")
        spec = json.loads(a.spec.read_text(encoding="utf-8"))
        clip = next((c for c in spec["clips"] if c["id"] == a.clip_id), None)
        if clip is None:
            sys.exit(f"clip {a.clip_id!r} not in {a.spec}")
        if clip.get("segments"):
            beats = [{"s": float(s["start"]), "e": float(s["end"]),
                      "words": s.get("words") or []} for s in clip["segments"]]
        else:
            beats = [{"s": float(clip["start"]), "e": float(clip["end"]),
                      "words": clip.get("words") or []}]
    elif a.window:
        beats = [{"s": a.window[0], "e": a.window[1], "words": []}]
    else:
        sys.exit("pass either --spec/--clip-id or --window START END")

    fps = 25

    def snap(d: float) -> float:  # to the 25fps frame grid, so video ⟷ audio align
        return round(d * fps) / fps

    render_spans: list[dict] = []   # {source_start, source_dur, mode, speaker, master_start}
    layout_map: list[dict] = []     # master-time {start,end,layout}
    words_master: list[dict] = []   # master-time words {t,d,w}
    cum = 0.0                       # running master time
    for bi, beat in enumerate(beats):
        bs, be = beat["s"], beat["e"]
        hook = a.hook_split_sec if bi == 0 else 0.0
        print(f"beat {bi+1}/{len(beats)}: {bs:.1f}-{be:.1f}s — framing...")
        if multitrack:
            active = active_timeline(a.tracks, bs, be, offsets)
            plan = framing_plan(active, be - bs, hook, a.min_span,
                                a.transition_split_sec)  # beat-relative
        else:
            abs_plan = plan_singleedit(a.video, bs, be, hook, a.min_span)  # source time
            plan = [{**s, "start": s["start"] - bs, "end": s["end"] - bs} for s in abs_plan]
        beat_master_start = cum
        for span in plan:  # beat-relative (0..be-bs)
            dur = snap(span["end"] - span["start"])
            if dur <= 0:
                continue
            render_spans.append({"source_start": bs + span["start"], "source_dur": dur,
                                 "mode": span["mode"], "speaker": span["speaker"],
                                 "master_start": round(cum, 3)})
            layout_map.append({"start": round(cum, 3), "end": round(cum + dur, 3),
                               "layout": "split" if span["mode"] == "split" else "full"})
            cum += dur
        # words: linear-map into the (snapped) master span for this beat
        beat_master_dur = cum - beat_master_start
        scale = beat_master_dur / (be - bs) if be > bs else 1.0
        for w in beat["words"]:
            words_master.append({"t": round(beat_master_start + w["t"] * scale, 3),
                                 "d": w["d"], "w": w["w"]})

    print(f"{len(render_spans)} framing spans across {len(beats)} beat(s):")
    for s in render_spans:
        tag = "SPLIT" if s["mode"] == "split" else f"single·spk{s['speaker']}"
        print(f"  {s['master_start']:7.2f}+{s['source_dur']:4.2f}  {tag}")
    if a.plan_out:
        a.plan_out.write_text(json.dumps(render_spans, indent=2), encoding="utf-8")
    if a.layout_out:
        a.layout_out.write_text(json.dumps(layout_map, indent=2), encoding="utf-8")
    if a.words_out:
        doc = {"schema_version": 1, "source": {"video": str(a.out)},
               "clips": [{"id": "clip-1", "start": 0.0, "end": round(cum, 3),
                          "hook": "", "hook_variants": [], "focus": 0.5,
                          "words": words_master}]}
        a.words_out.write_text(json.dumps(doc, ensure_ascii=False, indent=2),
                               encoding="utf-8")

    # Render VIDEO-ONLY spans, concat losslessly, build the whole audio in one
    # encode, then mux — no per-segment AAC priming drift.
    tmp = Path(tempfile.mkdtemp(prefix="rc_spans_"))
    parts = []
    for i, span in enumerate(render_spans):
        p = tmp / f"span_{i:03d}.mp4"
        if multitrack:
            render_span(span, a.tracks, offsets, dims, p)
        else:
            render_span_singleedit(span, a.video, dim, p)
        parts.append(p)

    from sofit.render import _concat_parts
    a.out.parent.mkdir(parents=True, exist_ok=True)
    video_master = tmp / "video.mp4"
    _concat_parts(parts, video_master)          # video-only, stream copy
    audio_master = tmp / "audio.m4a"
    audio_inputs = (["-i", str(a.tracks[0]), "-i", str(a.tracks[1])] if multitrack
                    else ["-i", str(a.video)])
    build_audio(render_spans, audio_inputs, offsets, audio_master)
    ff(["ffmpeg", "-v", "error", "-i", str(video_master), "-i", str(audio_master),
        "-map", "0:v", "-map", "1:a", "-c", "copy", "-shortest",
        "-movflags", "+faststart", "-y", str(a.out)])
    print("master ->", a.out)


if __name__ == "__main__":
    main()
