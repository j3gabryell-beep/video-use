"""One command for stage 1 of the cut → animate pipeline (see PIPELINE.md).

  source (path or URL) → transcribe → takes_packed.md → auto_cut (edl.json +
  report) → render the cut → prepare the HyperFrames work dir

Stops before anything creative: read <edit>/auto_cut_report.md, fix edl.json
if a heuristic was wrong (then re-run with --skip-cut), and author the
HyperFrames overlays per <edit>/hyperframes/BRIEF.md.

Usage:
    python helpers/auto_edit.py <video_or_url> [<video_or_url> ...] --workdir ~/videos/projeto
    python helpers/auto_edit.py clip.mp4 --workdir ~/videos/projeto --max-gap 0.35 --grade neutral_punch
    python helpers/auto_edit.py clip.mp4 --workdir ~/videos/projeto --skip-cut   # keep a hand-edited edl.json
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent


def step(title: str, cmd: list[str]) -> None:
    print(f"\n━━ {title}", flush=True)
    if subprocess.run([sys.executable, *cmd]).returncode:
        sys.exit(f"step failed: {title}")


def fetch(src: str, workdir: Path) -> Path:
    """Local paths are copied in (sources stay untouched), URLs downloaded."""
    if src.startswith(("http://", "https://")):
        name = Path(urllib.parse.urlparse(src).path).name or "source.mp4"
        dest = workdir / name
        if not dest.exists():
            print(f"downloading {src}", flush=True)
            urllib.request.urlretrieve(src, dest)
        return dest
    path = Path(src).expanduser().resolve()
    if not path.exists():
        sys.exit(f"not found: {path}")
    if path.parent == workdir:
        return path
    dest = workdir / path.name
    if not dest.exists():
        shutil.copy2(path, dest)
    return dest


def main() -> None:
    ap = argparse.ArgumentParser(description="Transcribe, auto-cut and stage a video for HyperFrames")
    ap.add_argument("sources", nargs="+", help="Video paths or http(s) URLs, in output order")
    ap.add_argument("--workdir", type=Path, required=True, help="Project folder (sources + edit/)")
    ap.add_argument("--engine", choices=["auto", "scribe", "local"], default="auto")
    ap.add_argument("--max-gap", default="0.45")
    ap.add_argument("--pad-before", default="0.08")
    ap.add_argument("--pad-after", default="0.12")
    ap.add_argument("--keep-retakes", action="store_true")
    ap.add_argument("--grade", default="none")
    ap.add_argument("--skip-cut", action="store_true", help="Reuse the existing edl.json")
    ap.add_argument("--preview", action="store_true", help="Faster render of the cut (QC quality)")
    ap.add_argument("--no-stage", action="store_true", help="Stop after the cut; skip HyperFrames prep")
    args = ap.parse_args()

    workdir = args.workdir.expanduser().resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    edit = workdir / "edit"
    videos = [fetch(s, workdir) for s in args.sources]

    for v in videos:
        step(f"transcribe {v.name}", [str(HERE / "transcribe.py"), str(v), "--edit-dir", str(edit),
                                      "--engine", args.engine])
    step("pack transcripts", [str(HERE / "pack_transcripts.py"), "--edit-dir", str(edit)])

    if not args.skip_cut:
        cut_cmd = [str(HERE / "auto_cut.py"), "--edit-dir", str(edit), *map(str, videos),
                   "--max-gap", args.max_gap, "--pad-before", args.pad_before,
                   "--pad-after", args.pad_after, "--grade", args.grade]
        if args.keep_retakes:
            cut_cmd.append("--keep-retakes")
        step("auto cut (silences, fillers, retakes)", cut_cmd)

    cut = edit / "cut.mp4"
    render_cmd = [str(HERE / "render.py"), str(edit / "edl.json"), "-o", str(cut), "--no-subtitles"]
    if args.preview:
        render_cmd.append("--preview")
    step("render cut", render_cmd)

    if not args.no_stage:
        step("prepare HyperFrames stage", [str(HERE / "hyperframes_stage.py"), "prepare",
                                            "--edit-dir", str(edit), "--cut", str(cut)])

    print(f"\ncut   → {cut}")
    print(f"report→ {edit / 'auto_cut_report.md'}")
    if not args.no_stage:
        print(f"next  → author overlays in {edit / 'hyperframes'} (BRIEF.md), then "
              "hyperframes_stage.py render + finish")


if __name__ == "__main__":
    main()
