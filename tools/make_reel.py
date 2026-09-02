#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PY = sys.executable                       # the sofit venv python running this
SOFIT = Path(PY).with_name("sofit")
# sofit defaults to --titler api (needs ANTHROPIC_API_KEY). We drive it via the
# Claude Code CLI, so force that backend on every direct sofit call (the shell
# `sofit` function that injects this is bypassed when we exec the binary).
TITLER = ["--titler", "claude-cli"]


def run(cmd: list, **kw):
    print("  $", " ".join(str(c) for c in cmd[:3]), "…")
    subprocess.run([str(c) for c in cmd], check=True, **kw)


def tool(name: str, *args):
    run([PY, HERE / name, *args])


def spec_clip_ids(spec: Path) -> list[str]:
    return [c["id"] for c in json.loads(spec.read_text(encoding="utf-8"))["clips"]]


def repoint(spec: Path, video: Path, dest: Path):
    d = json.loads(spec.read_text(encoding="utf-8"))
    d["source"]["video"] = str(video)
    dest.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")


def _rotation(video: Path) -> int:
    """Degrees of display-matrix / tag rotation on the video's first stream (0 if none).
    Phone footage (Pixel/iPhone) is stored landscape with a 90/270 flag; tools that
    ignore it compute crops on the unrotated frame and mis-frame the speaker."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream_side_data=rotation:stream_tags=rotate",
         "-of", "json", str(video)], capture_output=True, text=True).stdout
    try:
        d = json.loads(out or "{}")
    except json.JSONDecodeError:
        return 0
    rot = 0
    for st in d.get("streams", []):
        for sd in st.get("side_data_list", []):
            if sd.get("rotation") is not None:
                rot = int(float(sd["rotation"]))
        if st.get("tags", {}).get("rotate") is not None:
            rot = int(float(st["tags"]["rotate"]))
    return rot % 360


def _avg_fps(video: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=avg_frame_rate", "-of", "default=nk=1:nw=1",
         str(video)], capture_output=True, text=True).stdout.strip()
    try:
        num, den = out.split("/")
        return float(num) / float(den) if float(den) else 0.0
    except ValueError:
        return 0.0


REEL_FPS = 25   # reel_dynamics + sofit render run at 25fps and do NOT resample the source
NORM_MAX_LONG = 2560  # cap the master's long side; the reel is 1080p, so 2560 keeps
                      # ample punch-in headroom while cutting 4K encode cost ~4x.


def _dims(video: Path) -> tuple[int, int]:
    """Stored (w, h) of the first video stream. max(w,h) is orientation-invariant,
    so it's a safe 'long side' even for rotated phone footage."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x", str(video)],
        capture_output=True, text=True).stdout.strip()
    try:
        w, h = (int(x) for x in out.split("x")[:2])
        return w, h
    except (ValueError, IndexError):
        return 0, 0


_VTB = None
def _has_videotoolbox() -> bool:
    """True if ffmpeg has the Apple hardware H.264 encoder (cached)."""
    global _VTB
    if _VTB is None:
        try:
            enc = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"],
                                 capture_output=True, text=True).stdout
            _VTB = "h264_videotoolbox" in enc
        except Exception:
            _VTB = False
    return _VTB


def normalize_source(video: Path, work: Path) -> Path:
    """Bake rotation upright, resample to 25fps, and downscale oversized (4K) sources
    to a fast intermediate master (else the original unchanged). Traps avoided:
    (1) phone footage stored landscape with a 90/270 flag mis-frames the speaker if
    tools crop the unrotated frame; (2) a 60fps source drifts through the 25fps pipeline
    ("slowed video"). ffmpeg auto-applies the display matrix on decode, so re-encoding
    bakes orientation; fps=REEL_FPS fixes timing. SPEED: re-encoding the full 4K/60
    source with libx264 was ~10min — downscaling the long side to <=NORM_MAX_LONG plus
    the hardware h264_videotoolbox encoder cuts it to well under a minute. This is an
    intermediate master (re-encoded again downstream), so hardware quality is fine."""
    rot = _rotation(video)
    fps = _avg_fps(video)
    w, h = _dims(video)
    off_fps = abs(fps - REEL_FPS) > 0.1
    oversized = max(w, h) > NORM_MAX_LONG
    if rot == 0 and not off_fps and not oversized:
        return video
    dest = work / (video.stem + ".norm.mp4")
    if not dest.exists():
        why = ([f"{rot}deg"] if rot else []) + ([f"{fps:.2f}->{REEL_FPS}fps"] if off_fps else []) \
              + ([f"{max(w, h)}->{NORM_MAX_LONG}px"] if oversized else [])
        # scale caps the long side, keeps AR, forces even dims; runs on the rotated frame.
        vf = f"fps={REEL_FPS}"
        if oversized:
            vf += (f",scale={NORM_MAX_LONG}:{NORM_MAX_LONG}"
                   ":force_original_aspect_ratio=decrease:force_divisible_by=2")
        if _has_videotoolbox():
            enc = ["-c:v", "h264_videotoolbox", "-b:v", "16M"]
        else:
            enc = ["-c:v", "libx264", "-crf", "18", "-preset", "fast"]
        eng = "videotoolbox" if _has_videotoolbox() else "libx264"
        print(f"normalizing source ({', '.join(why)}, {eng}) -> {dest.name}")
        subprocess.run(["ffmpeg", "-v", "error", "-i", str(video),
                        "-vf", vf, "-vsync", "cfr", *enc,
                        "-c:a", "aac", "-metadata:s:v:0", "rotate=0",
                        "-movflags", "+faststart", "-y", str(dest)], check=True)
    return dest


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("input", help="a video file or an existing clips.json")
    ap.add_argument("--out", default="~/Desktop/reels")
    ap.add_argument("--clip")
    ap.add_argument("--focus", type=float)
    ap.add_argument("--no-cut", action="store_true")
    ap.add_argument("--no-zoom", action="store_true")
    ap.add_argument("--broll", action="store_true")
    ap.add_argument("--broll-plan")
    ap.add_argument("--no-judge", action="store_true")
    ap.add_argument("--broll-raw", action="store_true")
    ap.add_argument("--broll-manifest", metavar="MANIFEST",
                    help="compose b-roll from a b-roll-library pipeline manifest "
                         "(~/b-roll-library/runs/<slug>/manifest.json); beats map to "
                         "{start,dur,path} over the dyn master")
    ap.add_argument("--illustrate", action="store_true",
                    help="drawn-only b-roll: plan -> seed Gemini stills -> browser review -> Veo")
    ap.add_argument("--illus-n", type=int, default=4, help="seed stills per moment (batch to pick from)")
    ap.add_argument("--illus-model", default="gemini-3-pro-image")
    a = ap.parse_args()

    out = Path(a.out).expanduser()
    work = out / ".work"
    work.mkdir(parents=True, exist_ok=True)
    inp = Path(a.input).expanduser()

    # 1. get a clips.json (generate from a video, or use the given spec)
    if inp.suffix == ".json":
        spec = work / "reel.clips.json"
        shutil.copy2(inp, spec)
        sd = json.loads(spec.read_text(encoding="utf-8"))          # bake rotation on the
        src = Path(sd["source"]["video"]).expanduser()             # spec's source if flagged
        upright = normalize_source(src, work)
        if upright != src:
            sd["source"]["video"] = str(upright)
            spec.write_text(json.dumps(sd, ensure_ascii=False, indent=2), encoding="utf-8")
    else:
        inp = normalize_source(inp, work)                     # phone footage -> upright
        spec = work / "reel.clips.json"
        print("generating clip specs (sofit + claude)...")
        run([SOFIT, str(inp), "--clips-json", str(spec), *TITLER])

    clip_ids = [a.clip] if a.clip else spec_clip_ids(spec)
    finals = []
    for cid in clip_ids:
        print(f"\n=== clip {cid} ===")
        dyn_args = [str(spec), "--clip", cid]
        if a.focus is not None:
            dyn_args += ["--focus", str(a.focus)]
        if a.no_cut:
            dyn_args.append("--no-cut")
        if a.no_zoom:
            dyn_args.append("--no-zoom")
        tool("reel_dynamics.py", *dyn_args)                       # step 2
        stem = spec.stem.replace(".clips", "")
        dyn_master = work / f"{stem}.{cid}.dyn.mp4"
        dyn_spec = work / f"{stem}.{cid}.dyn.clips.json"

        render_master, render_spec = dyn_master, dyn_spec
        if a.broll_manifest:                                      # step 3-lib: b-roll-library manifest
            mani = json.loads(Path(a.broll_manifest).expanduser().read_text(encoding="utf-8"))
            mdir = Path(a.broll_manifest).expanduser().parent
            items = []
            for b in mani.get("beats", []):
                clip_path = mdir / "broll" / b["file"]
                if not clip_path.exists():
                    print(f"  manifest beat {b.get('n')}: missing {clip_path} — skipped")
                    continue
                dur = (b.get("trim") or {}).get("use_first_s") or round(b["end"] - b["start"], 2)
                items.append({"start": b["start"], "dur": dur, "path": str(clip_path),
                              "illustration": (b.get("clip", {}).get("source") != "pexels"),
                              "speed": 1.0})
            if items:
                lib_plan = work / f"{stem}.{cid}.lib_compose.json"
                lib_plan.write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")
                broll_master = work / f"{stem}.{cid}.broll.mp4"
                tool("broll.py", str(lib_plan), "--master", str(dyn_master), "--out", str(broll_master))
                render_master = broll_master
                render_spec = work / f"{stem}.{cid}.render.clips.json"
                repoint(dyn_spec, broll_master, render_spec)
            else:
                print("  manifest has no usable beats — rendering without b-roll")
        elif a.illustrate:                                        # step 3-alt: DRAWN b-roll only
            tool("broll_illus_plan.py", str(dyn_spec))
            plan = dyn_spec.with_name(dyn_spec.name.replace(".clips.json", "")
                                      + ".illus_plan.json")
            # interactive: seeds N stills/moment, opens the browser review, blocks
            # until you pick one per moment and hit Continue -> writes .selected.json
            tool("broll_review.py", str(plan), "-n", str(a.illus_n), "--model", a.illus_model)
            selected = plan.with_name(plan.stem + ".selected.json")
            sel_items = json.loads(selected.read_text(encoding="utf-8"))
            veo_dir = work / f"{stem}.{cid}.veo"
            veo_dir.mkdir(parents=True, exist_ok=True)
            compose_plan = []
            for j, it in enumerate(sel_items):                    # animate each approved still
                if not it.get("image"):
                    continue
                veo_out = veo_dir / f"veo_{j}.mp4"
                motion = it.get("motion") or "gentle parallax, the sketch settles"
                vprompt = (f"{motion}. Keep the exact hand-drawn indigo marker-sketch style, "
                           "white background, and composition of the source image; add no text or letters.")
                tool("broll_veo.py", "--image", it["image"], "--out", str(veo_out),
                     "--prompt", vprompt, "--duration", "4")
                compose_plan.append({"start": it["start"], "dur": it["dur"],
                                     "path": str(veo_out), "illustration": True, "speed": 1.0})
            if compose_plan:
                cplan = work / f"{stem}.{cid}.illus_compose.json"
                cplan.write_text(json.dumps(compose_plan, ensure_ascii=False, indent=2), encoding="utf-8")
                broll_master = work / f"{stem}.{cid}.broll.mp4"
                tool("broll.py", str(cplan), "--master", str(dyn_master), "--out", str(broll_master))
                render_master = broll_master
                render_spec = work / f"{stem}.{cid}.render.clips.json"
                repoint(dyn_spec, broll_master, render_spec)
            else:
                print("  no illustrated clips selected — rendering without b-roll")
        elif a.broll:                                             # step 3 (optional)
            if a.broll_plan:
                plan = Path(a.broll_plan).expanduser()
            else:
                tool("broll_plan.py", str(dyn_spec))
                plan = dyn_spec.with_name(dyn_spec.name.replace(".clips.json", "")
                                          + ".broll_plan.json")
            plan_items = json.loads(plan.read_text(encoding="utf-8"))
            has_ids = plan_items and all("id" in it for it in plan_items)
            if not has_ids and not a.no_judge:
                tool("broll_select.py", str(plan))
                plan = plan.with_name(plan.stem + ".selected.json")
            broll_master = work / f"{stem}.{cid}.broll.mp4"
            broll_args = [str(plan), "--master", str(dyn_master), "--out", str(broll_master)]
            if a.broll_raw:
                broll_args.append("--raw")
            tool("broll.py", *broll_args)
            render_master = broll_master
            render_spec = work / f"{stem}.{cid}.render.clips.json"
            repoint(dyn_spec, broll_master, render_spec)

        run([SOFIT, "--render-from", str(render_spec),           # step 4
             "--render-clips", str(out), "--safe-area", "reels",
             "--no-hook-card", *TITLER])                          # hook gate dropped — no burnt hook
        finals.append(out / f"{cid}.mp4")

    print("\ndone. reels:")
    for f in finals:
        print("  ", f)


if __name__ == "__main__":
    main()
