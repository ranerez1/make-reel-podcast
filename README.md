# make-reel-podcast

Turn a podcast recording into publish-ready vertical (9:16) reels — with the
Productivy logo, a music bed, and clean static captions — from **one video**,
a **YouTube link**, or **two per-person camera + mic feeds** (dynamic
"reaction cutting": stacked two-shot ↔ single speaker focus).

Runs locally on macOS. No cloud, no API keys.

---

## What it does

Three small tools under `tools/`, driven by the `./mrp.sh` wrapper:

| Tool | Job |
|------|-----|
| `podcast_clips.py` | Transcribe a long episode and propose ~5–8 short clip candidates (18–60s) with hooks. |
| `reaction_cut.py` | Build a dynamically-framed 9:16 master — single speaker close-ups + split-screen two-shots — from one video or two synced camera feeds. |
| `podcast_reel.py` | Burn the captions + logo + music bed onto a finished 9:16 clip. |

Captions are **static white phrase blocks** (Heebo bold, soft shadow, no
karaoke) — the "Startup for Startup" style.

---

## One-time setup

Needs macOS with [Homebrew](https://brew.sh). Then:

```bash
./setup.sh
```

This installs ffmpeg, `pipx`, the **sofit** engine (`sofit-cli`, which brings
Whisper transcription + render helpers), OpenCV, `libraqm` (Hebrew text
shaping), `yt-dlp`, and the **Heebo/Poppins** fonts; applies the brand caption
patch; and copies the logo + music into `~/.config/sofit/podcast/`.

Re-run `./setup.sh` any time — it's idempotent. (Re-run it after any
`pipx upgrade sofit-cli`, which wipes the caption patch.)

Everything is then run through the wrapper, which uses the sofit environment:

```bash
./mrp.sh podcast_reel.py --help
```

---

## Workflows

### A. Pre-made cut (you already have the 9:16 or 16:9 clip)

```bash
./mrp.sh podcast_reel.py my_cut.mp4 --layout full \
    --logo ~/.config/sofit/podcast/logo.png \
    --music ~/.config/sofit/podcast/theme.m4a \
    --grain --out out/clip.mp4
```

It transcribes the cut itself for captions. `--layout full` = lower-third
captions; `--layout split` = captions at the two-shot seam. Fix any
transcription slips with `--word-fixes fixes.json`
(`[{"match":"...","replace":"...","near_t":44.0}]`) — never invent words.

### B. Longform episode → pick a cut → reel

```bash
# 1) propose clips (transcribes; up to 60s each)
SOFIT_CLI_TIMEOUT=1200 ./mrp.sh podcast_clips.py episode.mp4 --out cut.clips.json

# 2) look at the printed candidates, then build the reaction master for one clip
./mrp.sh reaction_cut.py --video episode.mp4 --spec cut.clips.json --clip-id clip-1 \
    --out master.mp4 --words-out words.json --layout-out layout.json

# 3) caption + brand it
./mrp.sh podcast_reel.py master.mp4 --spec words.json --clip-id clip-1 --layout full \
    --layout-map layout.json --grain \
    --logo ~/.config/sofit/podcast/logo.png --music ~/.config/sofit/podcast/theme.m4a \
    --out out/clip-1.mp4
```

### C. YouTube source

Download the video first, then run workflow B on it:

```bash
yt-dlp -f "bv*[height<=2160][ext=mp4]+ba[ext=m4a]/b[ext=mp4]/b" \
    --merge-output-format mp4 -o source.mp4 "<youtube-url>"
```

If you hit `HTTP 403` / "page needs to be reloaded", yt-dlp is stale —
`brew upgrade yt-dlp` and retry.

### D. Two camera feeds + two per-person mics (editor-grade reaction cutting)

When each speaker has their **own continuous camera and mic**, you get true
simultaneous close-ups (open on the stacked two-shot, then focus on whoever
talks). The feeds must be **synced to a shared timeline** (same start, or a
known offset):

```bash
./mrp.sh reaction_cut.py --tracks personA.mp4 personB.mp4 \
    --spec cut.clips.json --clip-id clip-1 \
    --out master.mp4 --words-out words.json --layout-out layout.json
# then podcast_reel.py as in step 3 above
```

`reaction_cut.py` reads the active speaker from each person's **own mic**
(most robust). `--sync-offset <sec>` shifts track 2 if the pair drifts;
`--hook-split-sec` / `--min-span` tune the cadence; `--plan-out` writes the
framing plan for inspection.

> Note: the tracks currently need to arrive already synced (each track a video
> file whose embedded audio is that person's mic, on one shared clock). If your
> cameras recorded silent/unsynced, they must be aligned first — that
> auto-sync step is not part of this kit yet.

---

## Assets & branding

`~/.config/sofit/podcast/` holds `logo.png` (the badge overlaid top-right) and
`theme.m4a` (the music bed). `setup.sh` puts the bundled ones there; swap them
to rebrand. The `theme.m4a` is a **licensed track** — keep this repo private.

Captions use **Heebo** (installed by setup) at `~/Library/Fonts/Heebo[wght].ttf`.

---

## Output & QA

Reels are 1080×1920, ~−14 LUFS. Sanity-check each:

```bash
ffprobe -v error -select_streams v:0 -show_entries stream=width,height -of csv=p=0 out/clip.mp4
ffmpeg -i out/clip.mp4 -af loudnorm=print_format=json -f null - 2>&1 | grep input_i   # want -17..-11
```

---

## Troubleshooting

- **Hebrew captions render as boxes / wrong order** → Pillow is missing raqm.
  `brew install libraqm` then re-run `./setup.sh`.
- **English words inside a Hebrew caption look mis-ordered** → known limitation
  (right-to-left shaping of embedded Latin runs); fine for all-Hebrew reels.
- **`sofit venv python not found`** → run `./setup.sh`; ensure `pipx install
  sofit-cli` completed.
- **Captions/logo disappear partway** → you're on an old `podcast_reel.py`;
  this kit's version loops the image inputs to prevent it.

`AGENTS.md` / `SKILL.md` in this repo is the same workflow written for an AI
coding agent (e.g. Claude Code / Cursor), if the producer uses one.
