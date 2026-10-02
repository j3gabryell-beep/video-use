# video-use × HyperFrames — agent notes

This checkout runs the **cut → animate** pipeline in `PIPELINE.md`: video-use cuts silences, fillers and retakes; HyperFrames (`../hyperframes`, skill `talking-head-recut`) lays animated overlays about what is being said over the cut. Read `SKILL.md` (editing rules) and `PIPELINE.md` (the flow) before editing anything.

## When the user sends a video

1. **Setup.** If `python3 -c "import sherpa_onnx"` fails, or `~/.cache/video-use/models/` is empty, run `bash scripts/setup_cloud.sh`. It is idempotent.
2. **Locate the source.** Check, in this order:
   - a chat attachment (find its path on disk)
   - a URL the user pasted (GitHub release asset, direct link)
   - a file committed to a repo `inbox/`

   Work in `~/videos/<short-name>/`, never inside a repo (Hard Rule 12).
3. **Stage 1:** `python helpers/auto_edit.py <src> --workdir ~/videos/<name>`. Then read `edit/auto_cut_report.md` and `edit/takes_packed.md`. Check every "Review these" item and any cut that looks wrong with `timeline_view.py`. Fix `edl.json` by hand and re-run with `--skip-cut` if needed.
4. **Confirm once (Hard Rule 11).** Send one message with: what was cut (before → after length, retakes and fillers removed), the overlay plan (aspect, layout, style, 4–8 cards with timestamps and content), and subtitles yes/no. Wait for OK.
   - Skip the wait only if the user said to go ahead without asking ("pode fazer direto", "decide você").
5. **Stage 2:** in `edit/hyperframes/`, follow `../hyperframes/skills/talking-head-recut/SKILL.md` Steps 5→10.
   - Use `transcript.json` as given: it is already on the cut timeline. Fix ASR typos only.
   - Write `public/index.html` with the cut as `input-video.mp4`.
   - Animate `#video-wrap` with `x`/`y`/`scale` (`transform-origin: 0 0`), never `left`/`top`/`width`/`height`. The lint rejects layout-property tweens (`gsap_non_transform_motion`).
   - Give every `.card-host` an `id`.
6. **Render:**
   - `python helpers/hyperframes_stage.py render --edit-dir <edit>` (add `--draft` for a fast timing check)
   - then `python helpers/hyperframes_stage.py finish --edit-dir <edit> [--subtitles]`
7. **Self-eval before showing anything.**
   - Grab frames at each card's entry and exit, plus the first and last 2s.
   - Measure loudness: `ffmpeg -i final.mp4 -af ebur128=peak=true -f null -`.
   - Confirm the final duration equals the cut duration.
8. **Deliver.** Copy `final.mp4` into the session's primary working directory or scratchpad and send it with `SendUserFile`. Append the session to `edit/project.md`.

## Environment facts (Claude Code cloud container)

- **ElevenLabs and HuggingFace may be blocked by the network policy.** Then `transcribe.py --engine auto` falls back to offline Parakeet v3 (from GitHub releases), which handles Portuguese.
- **HyperFrames render** uses Playwright's headless shell at `/opt/pw-browsers/chromium_headless_shell-*/`. `hyperframes_stage.py` sets `PRODUCER_HEADLESS_SHELL_PATH` automatically.
- **Speed:** HyperFrames renders at roughly 5x the video duration on 4 cores (1080p30). For long videos, do a `--draft` render first.
- **Defaults when the user gave no direction:** keep the source aspect, `overlay` layout, a clean dark theme (`slate`), and Inter. Pick the card count with the talking-head-recut density rule. Subtitles stay off unless asked.
