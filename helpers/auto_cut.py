"""Draft an EDL that removes silences, vocal fillers and retakes.

Reads the cached word-level transcripts in <edit>/transcripts/ (Scribe or
transcribe_local.py — same shape) and writes:

  <edit>/edl.json             ranges to keep, ready for render.py
  <edit>/auto_cut_report.md   what was removed and why, plus suspects to review

This is a first draft for the editor (you), not a final decision: read the
report, drill into doubtful spots with timeline_view.py, edit edl.json by hand
where the heuristics were wrong, then render.

What it cuts:
  - silences: any gap between kept words longer than --max-gap becomes a cut;
    the speech on both sides keeps --pad-before / --pad-after of air
    (Hard Rule 7), never more than half the gap, so a pad cannot reach into a
    neighbouring word.
  - fillers: standalone vocal fillers (ahn, éh, hum, uh, um, ...). Real words
    like "tipo" or "então" are never auto-cut; they are listed as suspects.
  - retakes: a phrase whose words are repeated as the opening of one of the
    next few phrases (false start, restarted sentence). The LAST take is kept.
  - stutters: an immediately repeated run of 2+ words inside a phrase
    ("a gente teve a gente teve um ..."), first copy removed. A repeated
    single word ("muito muito") is only reported, since it is often emphasis.

Usage:
    python helpers/auto_cut.py --edit-dir <edit> <video> [<video> ...]
    python helpers/auto_cut.py --edit-dir <edit> <video> --max-gap 0.35 --pad-before 0.06 --pad-after 0.10
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path

FILLERS = {
    # Pure vocalizations: never a word in pt / en / es, safe to cut anywhere.
    "ahn", "ãh", "ããã", "éh", "ééé", "éé", "hum", "humm", "hmm", "hm", "mm", "mmm",
    "uh", "uhh", "uhm", "umm", "erm",
}
# Fillers in English that are real words elsewhere ("um" is the Portuguese
# article, "eh" Spanish): only cut when the transcript is English.
FILLERS_EN = {"um", "er", "eh"}
EN_HINTS = {"the", "and", "is", "you", "that", "this", "it", "of", "to", "we"}
PT_ES_HINTS = {"de", "que", "não", "é", "você", "gente", "uma", "para", "como", "el", "la", "los", "por"}
# Not cut automatically: these are words too, so only report them.
SOFT_FILLERS = {"tipo", "né", "então", "assim", "sabe", "like", "basically", "actually"}
SENTENCE_END = (".", "?", "!", "…")


def norm(text: str) -> str:
    t = unicodedata.normalize("NFC", text.lower())
    return re.sub(r"[^\w%]+", "", t)


def load_words(transcript: Path) -> list[dict]:
    data = json.loads(transcript.read_text())
    words = []
    for w in data.get("words", []):
        if w.get("type", "word") != "word" or w.get("start") is None:
            continue
        words.append({"text": w["text"].strip(), "start": float(w["start"]),
                      "end": float(w.get("end", w["start"])), "n": norm(w["text"])})
    for i, w in enumerate(words):
        w["i"] = i
    return words


def split_phrases(words: list[dict], phrase_gap: float) -> list[list[dict]]:
    phrases: list[list[dict]] = []
    cur: list[dict] = []
    for i, w in enumerate(words):
        if cur and (w["start"] - cur[-1]["end"] >= phrase_gap or cur[-1]["text"].endswith(SENTENCE_END)):
            phrases.append(cur)
            cur = []
        cur.append(w)
    if cur:
        phrases.append(cur)
    return phrases


def text_of(ws: list[dict]) -> str:
    return " ".join(w["text"] for w in ws)


def find_retakes(phrases: list[list[dict]], lookahead: int, window_s: float, min_ratio: float):
    """Phrase i is a retake when its words reappear at the start of a later phrase."""
    dropped: list[tuple[list[dict], list[dict], float]] = []
    for i, p in enumerate(phrases):
        a = [w["n"] for w in p if w["n"] and w["n"] not in FILLERS]
        if len(a) < 2:
            continue
        for q in phrases[i + 1: i + 1 + lookahead]:
            if q[0]["start"] - p[-1]["end"] > window_s:
                break
            b = [w["n"] for w in q if w["n"] and w["n"] not in FILLERS][: len(a) + 2]
            ratio = SequenceMatcher(None, a, b[: len(a)]).ratio()
            ratio = max(ratio, SequenceMatcher(None, a, b).ratio())
            if ratio >= min_ratio:
                dropped.append((p, q, ratio))
                break
    return dropped


def find_stutters(phrase: list[dict]) -> tuple[list[dict], list[list[dict]]]:
    """Adjacent repeated n-grams. Returns (words to drop for n>=2, single-word repeats to report)."""
    drop: list[dict] = []
    singles: list[list[dict]] = []
    toks = [w["n"] for w in phrase]
    i = 0
    while i < len(phrase):
        hit = 0
        for n in range(min(6, (len(phrase) - i) // 2), 0, -1):
            if toks[i:i + n] == toks[i + n:i + 2 * n] and all(toks[i:i + n]):
                hit = n
                break
        if hit >= 2:
            drop += phrase[i:i + hit]
            i += hit
        elif hit == 1:
            singles.append(phrase[i:i + 2])
            i += 1
        else:
            i += 1
    return drop, singles


def build_ranges(words: list[dict], keep: set[int], max_gap: float, pad_before: float,
                 pad_after: float, total: float | None) -> list[dict]:
    """Group kept words into contiguous ranges and pad their edges."""
    runs: list[list[dict]] = []
    for w in words:
        if w["i"] not in keep:
            continue
        if runs:
            last = runs[-1][-1]
            contiguous = all(j in keep for j in range(last["i"] + 1, w["i"]))
            if contiguous and w["start"] - last["end"] <= max_gap:
                runs[-1].append(w)
                continue
        runs.append([w])

    ranges = []
    for run in runs:
        first, last = run[0], run[-1]
        prev = words[first["i"] - 1] if first["i"] > 0 else None
        nxt = words[last["i"] + 1] if last["i"] + 1 < len(words) else None
        start = first["start"] - pad_before
        if prev:
            start = max(start, (prev["end"] + first["start"]) / 2)
        end = last["end"] + pad_after
        if nxt:
            end = min(end, (last["end"] + nxt["start"]) / 2)
        start = max(0.0, start)
        if total:
            end = min(end, total)
        ranges.append({"start": round(start, 3), "end": round(end, 3), "words": run})

    merged: list[dict] = []
    for r in ranges:
        if merged and r["start"] - merged[-1]["end"] < 0.02:
            merged[-1]["end"] = r["end"]
            merged[-1]["words"] += r["words"]
        else:
            merged.append(r)
    return merged


def probe_duration(video: Path) -> float | None:
    import subprocess
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                          str(video)], capture_output=True, text=True)
    try:
        return float(out.stdout.strip())
    except ValueError:
        return None


def fmt(t: float) -> str:
    return f"{int(t // 60):02d}:{t % 60:05.2f}"


def main() -> None:
    ap = argparse.ArgumentParser(description="Draft an EDL that removes silences, fillers and retakes")
    ap.add_argument("videos", nargs="+", type=Path, help="Source videos, in output order")
    ap.add_argument("--edit-dir", type=Path, required=True)
    ap.add_argument("--max-gap", type=float, default=0.45,
                    help="Longest pause kept inside a range (s). Longer pauses are cut. Default 0.45")
    ap.add_argument("--pad-before", type=float, default=0.08, help="Air kept before the first word (s)")
    ap.add_argument("--pad-after", type=float, default=0.12, help="Air kept after the last word (s)")
    ap.add_argument("--phrase-gap", type=float, default=0.5, help="Pause that ends a phrase, for retakes (s)")
    ap.add_argument("--retake-ratio", type=float, default=0.75, help="Word similarity to call a retake (0-1)")
    ap.add_argument("--retake-lookahead", type=int, default=3, help="How many later phrases to compare")
    ap.add_argument("--retake-window", type=float, default=20.0, help="Max seconds between take and retake")
    ap.add_argument("--keep-retakes", action="store_true", help="Report retakes but do not cut them")
    ap.add_argument("--keep-stutters", action="store_true", help="Report stutters but do not cut them")
    ap.add_argument("--grade", default="none", help="EDL grade field (preset name, 'auto' or raw filter)")
    ap.add_argument("-o", "--output", type=Path, default=None, help="Default: <edit>/edl.json")
    args = ap.parse_args()

    edit_dir = args.edit_dir.resolve()
    edl = {"version": 1, "sources": {}, "ranges": [], "grade": args.grade, "overlays": []}
    report = ["# auto_cut report", ""]
    total_in = total_out = 0.0

    for video in args.videos:
        video = video.resolve()
        tr = edit_dir / "transcripts" / f"{video.stem}.json"
        if not tr.exists():
            sys.exit(f"no transcript for {video.name} at {tr} — run transcribe first")
        words = load_words(tr)
        duration = probe_duration(video) or (words[-1]["end"] if words else 0.0)
        edl["sources"][video.stem] = str(video)
        keep = {w["i"] for w in words}
        tokens = [w["n"] for w in words]
        english = sum(t in EN_HINTS for t in tokens) > sum(t in PT_ES_HINTS for t in tokens)
        fillers = FILLERS | (FILLERS_EN if english else set())
        removed: list[str] = []
        suspects: list[str] = []

        for w in words:
            if w["n"] in fillers:
                keep.discard(w["i"])
                removed.append(f"- filler `{w['text']}` @ {fmt(w['start'])}")

        phrases = split_phrases(words, args.phrase_gap)
        for p, q, ratio in find_retakes(phrases, args.retake_lookahead, args.retake_window, args.retake_ratio):
            line = (f"- retake {fmt(p[0]['start'])}–{fmt(p[-1]['end'])} \"{text_of(p)}\" → kept later take "
                    f"@ {fmt(q[0]['start'])} \"{text_of(q)}\" (similarity {ratio:.2f})")
            if args.keep_retakes:
                suspects.append(line)
            else:
                keep -= {w["i"] for w in p}
                removed.append(line)

        for p in split_phrases([w for w in words if w["i"] in keep], args.phrase_gap):
            drop, singles = find_stutters(p)
            if drop:
                line = f"- stutter @ {fmt(drop[0]['start'])}: removed \"{text_of(drop)}\" (repeated right after)"
                if args.keep_stutters:
                    suspects.append(line)
                else:
                    keep -= {w["i"] for w in drop}
                    removed.append(line)
            for pair in singles:
                suspects.append(f"- repeated word @ {fmt(pair[0]['start'])}: \"{text_of(pair)}\" (emphasis or slip?)")

        for w in words:
            if w["i"] in keep and w["n"] in SOFT_FILLERS and w["text"].endswith(","):
                suspects.append(f"- possible verbal crutch @ {fmt(w['start'])}: \"{w['text']}\"")

        ranges = build_ranges(words, keep, args.max_gap, args.pad_before, args.pad_after, duration)
        kept_s = sum(r["end"] - r["start"] for r in ranges)
        silences = max(0, len(ranges) - 1)
        for r in ranges:
            edl["ranges"].append({
                "source": video.stem, "start": r["start"], "end": r["end"],
                "quote": text_of(r["words"]), "reason": "auto_cut: speech run",
            })
        total_in += duration
        total_out += kept_s

        report += [
            f"## {video.name}",
            f"- source {fmt(duration)} → kept {fmt(kept_s)} ({100 * kept_s / max(duration, 1e-6):.0f}%)",
            f"- {len(ranges)} range(s), {silences} internal cut(s); pauses > {args.max_gap:.2f}s removed",
            "",
            "### Removed",
            *(removed or ["- (nothing beyond silences)"]),
            "",
            "### Review these (not cut)",
            *(suspects or ["- (none)"]),
            "",
            "### Kept ranges",
            *[f"- [{fmt(r['start'])}–{fmt(r['end'])}] {text_of(r['words'])}" for r in ranges],
            "",
        ]

    edl["total_duration_s"] = round(total_out, 3)
    out = (args.output or edit_dir / "edl.json").resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(edl, indent=2, ensure_ascii=False))
    report.insert(2, f"**Total:** {fmt(total_in)} → {fmt(total_out)} "
                     f"(-{fmt(total_in - total_out)}, {100 * (1 - total_out / max(total_in, 1e-6)):.0f}% shorter)\n")
    (edit_dir / "auto_cut_report.md").write_text("\n".join(report))
    print(f"edl → {out}  ({len(edl['ranges'])} ranges, {fmt(total_in)} → {fmt(total_out)})")
    print(f"report → {edit_dir / 'auto_cut_report.md'}")


if __name__ == "__main__":
    main()
