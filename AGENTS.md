---
name: make-reel-podcast
description: Turn a podcast video (or YouTube URL, or two synced camera+mic feeds) into publish-ready 9:16 reels — reaction framing (split two-shot ↔ speaker focus), Productivy logo, music bed, static Heebo captions. Use when asked to make podcast reels, cut an episode into reels, or /make-reel-podcast.
---

# make-reel-podcast (standalone kit)

Local macOS pipeline. Run every tool through `./mrp.sh <tool.py> …` (it uses the
sofit venv python). Run `./setup.sh` once first. Captions are static white
Heebo-800 phrase blocks with a soft shadow — never sofit's karaoke render.

Assets: `~/.config/sofit/podcast/logo.png`, `~/.config/sofit/podcast/theme.m4a`.
If either is missing, tell the user to run `./setup.sh` and stop.

## Pipeline

1. **Intake (confirm first):** source — a single video / YouTube URL, OR two
   synced per-person camera+mic feeds; type — longform (propose cuts, user
   picks) or a pre-made cut; framing — `reaction` (default: dynamic split/single)
   / `split` / `switch` / `as-is`; how many reels; slug.
2. **YouTube?** `yt-dlp -f "bv*[height<=2160][ext=mp4]+ba[ext=m4a]/b[ext=mp4]/b" --merge-output-format mp4 -o source.mp4 <url>` (on 403/"reload": `brew upgrade yt-dlp`).
3. **Longform → propose cuts:** `SOFIT_CLI_TIMEOUT=1200 ./mrp.sh podcast_clips.py <video> --out cut.clips.json` (18–60s candidates). Show the user id/hook/duration; they pick.
4. **Build the framing master** per picked clip:
   - single video: `./mrp.sh reaction_cut.py --video <video> --spec cut.clips.json --clip-id <id> --out master.mp4 --words-out words.json --layout-out layout.json`
   - two synced feeds: same with `--tracks A.mp4 B.mp4` (active speaker from each person's mic). `--sync-offset`, `--hook-split-sec`, `--min-span`, `--plan-out` tune it.
5. **Compose the reel:** `./mrp.sh podcast_reel.py master.mp4 --spec words.json --clip-id clip-1 --layout full --layout-map layout.json --grain --logo ~/.config/sofit/podcast/logo.png --music ~/.config/sofit/podcast/theme.m4a --out out/<id>.mp4`.
   - Pre-made cut (no reaction_cut): `./mrp.sh podcast_reel.py <cut.mp4> --layout full --logo … --music … --out out/clip-1.mp4` (it transcribes the cut). Fix transcript slips with `--word-fixes` — never invent words.
6. **QA per reel:** ffprobe 1080×1920; LUFS in −17..−11 (`ffmpeg -af loudnorm=print_format=json`); frame-grab a few times to check caption position, logo, and no doubled badge. Fix → re-render.

Reaction framing = single close-ups of the active speaker + split-screen at the
hook and speaker handoffs. Quality is capped by input: two synced per-person
feeds = editor-grade simultaneous close-ups; a single published edit can only
split its own wide two-shots.
