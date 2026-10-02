"""Hand a video-use cut to HyperFrames for animated overlays, then finish it.

Stage 2 of the cut → animate pipeline (see PIPELINE.md). video-use owns the
cut (silences, retakes, grade, loudness); HyperFrames owns the visuals laid
over it, authored with its `talking-head-recut` skill: the cut plays untouched
underneath, cards (titles, data callouts, lists, lower-thirds) are timed to
the words on the CUT timeline.

Subcommands:

  prepare  edl.json + rendered cut → <edit>/hyperframes/ work dir in the exact
           layout talking-head-recut expects (metadata.json, transcript.json on
           the cut timeline, public/input-video.mp4 with a dense GOP, fonts,
           GSAP). No re-transcription: word times are remapped from the cached
           source transcripts through the EDL.
  render   lint + render public/index.html → <edit>/hyperframes/output.mp4
  finish   burn master.srt onto output.mp4 (subtitles LAST, Hard Rule 1)
           → <edit>/final.mp4. Without subtitles it just copies.

Usage:
    python helpers/hyperframes_stage.py prepare --edit-dir <edit> --cut <edit>/cut.mp4
    python helpers/hyperframes_stage.py render  --edit-dir <edit>
    python helpers/hyperframes_stage.py finish  --edit-dir <edit> [--subtitles]
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from fractions import Fraction
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import render as vu_render  # noqa: E402  (helpers are run as scripts, not a package)

SKILL = "talking-head-recut"
HEADLESS_SHELL_GLOB = "/opt/pw-browsers/chromium_headless_shell-*/chrome-linux/headless_shell"


def find_skill_dir(explicit: Path | None) -> Path:
    candidates = [explicit] if explicit else []
    if os.environ.get("HYPERFRAMES_DIR"):
        candidates.append(Path(os.environ["HYPERFRAMES_DIR"]) / "skills" / SKILL)
    candidates += [
        HERE.parent.parent / "hyperframes" / "skills" / SKILL,  # sibling checkout
        Path.home() / ".claude" / "skills" / SKILL,
        Path.home() / ".agents" / "skills" / SKILL,
    ]
    for c in candidates:
        if c and (c / "SKILL.md").exists():
            return c.resolve()
    sys.exit(f"{SKILL} skill not found. Clone heygen-com/hyperframes next to video-use, "
             "set HYPERFRAMES_DIR, or pass --skill-dir.")


def hyperframes_cmd() -> list[str]:
    custom = os.environ.get("HYPERFRAMES_CLI")
    return custom.split() if custom else ["npx", "--yes", "hyperframes"]


def render_env() -> dict[str, str]:
    env = dict(os.environ)
    if "PRODUCER_HEADLESS_SHELL_PATH" not in env:
        import glob
        shells = sorted(glob.glob(HEADLESS_SHELL_GLOB))
        if shells:
            env["PRODUCER_HEADLESS_SHELL_PATH"] = shells[-1]
    return env


def probe(video: Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height,r_frame_rate:format=duration", "-of", "json", str(video)],
        capture_output=True, text=True, check=True,
    )
    data = json.loads(out.stdout)
    s = data["streams"][0]
    return {
        "width": int(s["width"]),
        "height": int(s["height"]),
        "fps": round(float(Fraction(s["r_frame_rate"])), 3),
        "duration": float(data["format"]["duration"]),
    }


def segment_durations(edl: dict, edit_dir: Path) -> list[float]:
    """Real durations of the rendered segments when they exist (frame-quantized),
    else the EDL's nominal ones. Keeps word times aligned with the actual cut."""
    nominal = [float(r["end"]) - float(r["start"]) for r in edl["ranges"]]
    for sub in ("clips_graded", "clips_preview", "clips_draft"):
        clips = sorted((edit_dir / sub).glob("seg_*.mp4"))
        if len(clips) == len(nominal):
            return [probe(c)["duration"] for c in clips]
    return nominal


def cut_transcript(edl: dict, edit_dir: Path) -> list[dict]:
    """Flat [{text, start, end}] on the output timeline — the shape
    talking-head-recut reads as transcript.json."""
    words: list[dict] = []
    offset = 0.0
    cache: dict[str, dict] = {}
    for r, dur in zip(edl["ranges"], segment_durations(edl, edit_dir)):
        src = r["source"]
        if src not in cache:
            p = edit_dir / "transcripts" / f"{src}.json"
            cache[src] = json.loads(p.read_text()) if p.exists() else {"words": []}
        start, end = float(r["start"]), float(r["end"])
        for w in vu_render._words_in_range(cache[src], start, end):
            a = max(w["start"], start) - start + offset
            b = min(w["end"], end) - start + offset
            words.append({"text": w["text"].strip(), "start": round(a, 3), "end": round(max(b, a + 0.02), 3)})
        offset += dur
    return words


def cmd_prepare(args: argparse.Namespace) -> None:
    edit_dir = args.edit_dir.resolve()
    edl = json.loads((edit_dir / "edl.json").read_text())
    cut = args.cut.resolve()
    if not cut.exists():
        sys.exit(f"cut not found: {cut} — run render.py first")
    skill_dir = find_skill_dir(args.skill_dir)
    work = edit_dir / "hyperframes"
    public = work / "public"
    for d in ("fonts", "vendor", "cards"):
        (public / d).mkdir(parents=True, exist_ok=True)

    meta = probe(cut)
    fps = args.fps or round(meta["fps"])
    meta["fps"] = fps
    (work / "metadata.json").write_text(json.dumps(meta, indent=2))

    words = cut_transcript(edl, edit_dir)
    for w in words:  # never past the media end (black tail)
        w["end"] = min(w["end"], meta["duration"])
    (work / "transcript.json").write_text(json.dumps(words, indent=2, ensure_ascii=False))

    for f in (skill_dir / "assets" / "fonts").iterdir():
        shutil.copy2(f, public / "fonts" / f.name)
    shutil.copy2(skill_dir / "assets" / "vendor" / "gsap.min.js", public / "vendor" / "gsap.min.js")

    # Dense GOP so every frame is seekable in the renderer (sparse GOP freezes).
    staged = public / "input-video.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(cut), "-c:v", "libx264", "-crf", "16",
         "-g", str(fps), "-keyint_min", str(fps), "-pix_fmt", "yuv420p", "-r", str(fps),
         "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(staged)],
        check=True,
    )

    brief = [
        "# BRIEF — HyperFrames overlay stage",
        "",
        f"- input: `{cut}` (already cut by video-use: silences / retakes removed, loudness normalized)",
        f"- canvas source: {meta['width']}×{meta['height']} @ {fps} fps, {meta['duration']:.2f}s",
        f"- transcript: `transcript.json` — {len(words)} words, ALREADY on the cut timeline (do not re-transcribe)",
        f"- staged video: `public/input-video.mp4` (dense GOP)",
        f"- skill: `{skill_dir}/SKILL.md` — follow Steps 5→10 (correct transcript, storyboard, cards, index.html)",
        "- subtitles: NOT inside the composition; `hyperframes_stage.py finish --subtitles` burns them last",
        "",
        "User notes:",
        *(f"- {n}" for n in (args.note or [])),
    ]
    (work / "BRIEF.md").write_text("\n".join(brief) + "\n")
    print(f"prepared → {work}")
    print(f"  {len(words)} words on the cut timeline, {meta['duration']:.2f}s, {meta['width']}x{meta['height']}@{fps}")
    print(f"  next: author storyboard.json + public/cards/*.html + public/index.html per {skill_dir}/SKILL.md")


def cmd_render(args: argparse.Namespace) -> None:
    work = args.edit_dir.resolve() / "hyperframes"
    public = work / "public"
    if not (public / "index.html").exists():
        sys.exit(f"no composition at {public / 'index.html'}")
    meta = json.loads((work / "metadata.json").read_text())
    env = render_env()
    hf = hyperframes_cmd()
    if not args.skip_lint and subprocess.run([*hf, "lint", str(public)], env=env).returncode:
        sys.exit("hyperframes lint failed — fix the errors above (warnings are fine), then re-run")
    out = work / "output.mp4"
    cmd = [*hf, "render", str(public), "-o", str(out), "--fps", str(meta["fps"])]
    if args.draft:
        cmd += ["--quality", "draft"]
    if subprocess.run(cmd, env=env).returncode:
        sys.exit("hyperframes render failed — see the log above")
    got = probe(out)
    drift = abs(got["duration"] - meta["duration"])
    print(f"rendered → {out} ({got['duration']:.2f}s, cut was {meta['duration']:.2f}s, drift {drift:.2f}s)")
    if drift > 0.5:
        print("  WARNING: duration differs from the cut — check data-duration on #stage and #bg-video")


def cmd_finish(args: argparse.Namespace) -> None:
    edit_dir = args.edit_dir.resolve()
    src = edit_dir / "hyperframes" / "output.mp4"
    if not src.exists():
        sys.exit(f"no HyperFrames render at {src}")
    out = (args.output or edit_dir / "final.mp4").resolve()
    subs = None
    if args.subtitles:
        edl = json.loads((edit_dir / "edl.json").read_text())
        subs = edit_dir / "master.srt"
        vu_render.build_master_srt(edl, edit_dir, subs)
    vu_render.build_final_composite(src, [], subs, out, edit_dir)
    print(f"final → {out}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("prepare")
    p.add_argument("--edit-dir", type=Path, required=True)
    p.add_argument("--cut", type=Path, required=True, help="Video rendered by render.py from edl.json")
    p.add_argument("--fps", type=int, default=None, help="Composition fps (default: the cut's, rounded)")
    p.add_argument("--skill-dir", type=Path, default=None, help="Path to hyperframes/skills/talking-head-recut")
    p.add_argument("--note", action="append", help="User direction to record in BRIEF.md (repeatable)")
    p.set_defaults(fn=cmd_prepare)

    r = sub.add_parser("render")
    r.add_argument("--edit-dir", type=Path, required=True)
    r.add_argument("--draft", action="store_true", help="Fast low-quality render for checking timing")
    r.add_argument("--skip-lint", action="store_true")
    r.set_defaults(fn=cmd_render)

    f = sub.add_parser("finish")
    f.add_argument("--edit-dir", type=Path, required=True)
    f.add_argument("--subtitles", action="store_true", help="Burn master.srt (built from the EDL) last")
    f.add_argument("-o", "--output", type=Path, default=None, help="Default: <edit>/final.mp4")
    f.set_defaults(fn=cmd_finish)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
