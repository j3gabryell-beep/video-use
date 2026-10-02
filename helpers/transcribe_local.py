"""Transcribe a video offline with NVIDIA Parakeet TDT v3 (via sherpa-onnx).

Drop-in fallback for `transcribe.py` when no ElevenLabs key is available or
the network blocks api.elevenlabs.io. Writes the same Scribe-shaped JSON
(`words` with type word/spacing, start, end, speaker_id) to
<edit_dir>/transcripts/<video_stem>.json, so pack_transcripts.py, render.py
and auto_cut.py work unchanged.

Parakeet v3 is multilingual (25 European languages incl. Portuguese, English,
Spanish) and runs at ~7x realtime on 4 CPU cores. Caveats vs Scribe:
  - no diarization (every word is speaker_0)
  - no audio events
  - vocal fillers ("ahn", "é...", "um") are often dropped from the text. Their
    audio still sits in the gap between the neighbouring words, so the gap
    reads as "non-speech" and auto_cut.py removes it like a silence.

The model (~650 MB) is fetched once from the sherpa-onnx GitHub release into
~/.cache/video-use/models/ (override with VIDEO_USE_MODELS_DIR).

Usage:
    python helpers/transcribe_local.py <video_path>
    python helpers/transcribe_local.py <video_path> --edit-dir /custom/edit
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
import wave
from pathlib import Path

import numpy as np

from transcribe import count_audio_tracks, extract_audio, peak_dbfs, transcript_path

MODEL_NAME = "sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8"
MODEL_URL = (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/"
    f"{MODEL_NAME}.tar.bz2"
)
SAMPLE_RATE = 16000
# Parakeet is trained on utterances up to ~20-30s. Longer windows decode, but
# timestamps degrade, so split long audio at the quietest point near this size.
WINDOW_TARGET_S = 20.0
WINDOW_MIN_S = 12.0
WINDOW_MAX_S = 28.0
FRAME_S = 0.02
# A word "ends" at its last 20ms frame louder than this, relative to the
# loudest frame of the file. Trims the trailing silence Parakeet folds into
# the duration of the last token before a pause.
SPEECH_FLOOR_DB = -38.0


def models_dir() -> Path:
    return Path(os.environ.get("VIDEO_USE_MODELS_DIR", Path.home() / ".cache" / "video-use" / "models"))


def ensure_model(verbose: bool = True) -> Path:
    dest = models_dir() / MODEL_NAME
    if (dest / "encoder.int8.onnx").exists():
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    archive = dest.parent / f"{MODEL_NAME}.tar.bz2"
    if verbose:
        print(f"  downloading {MODEL_NAME} (~490 MB, once)", flush=True)
    urllib.request.urlretrieve(MODEL_URL, archive)
    with tarfile.open(archive, "r:bz2") as tar:
        tar.extractall(dest.parent)
    archive.unlink()
    return dest


def load_recognizer(model: Path):
    import sherpa_onnx

    return sherpa_onnx.OfflineRecognizer.from_transducer(
        encoder=str(model / "encoder.int8.onnx"),
        decoder=str(model / "decoder.int8.onnx"),
        joiner=str(model / "joiner.int8.onnx"),
        tokens=str(model / "tokens.txt"),
        model_type="nemo_transducer",
        num_threads=max(1, os.cpu_count() or 1),
    )


def read_wav(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as w:
        data = w.readframes(w.getnframes())
    return np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0


def frame_db(audio: np.ndarray) -> np.ndarray:
    """RMS level per 20ms frame, in dB relative to the loudest frame."""
    n = int(FRAME_S * SAMPLE_RATE)
    frames = len(audio) // n
    if frames == 0:
        return np.array([0.0])
    rms = np.sqrt(np.mean(audio[: frames * n].reshape(frames, n) ** 2, axis=1) + 1e-12)
    return 20 * np.log10(rms / rms.max())


def split_points(db: np.ndarray, total_s: float) -> list[float]:
    """Window boundaries (seconds), each at the quietest frame in its search span."""
    points = [0.0]
    while total_s - points[-1] > WINDOW_MAX_S:
        lo = int((points[-1] + WINDOW_MIN_S) / FRAME_S)
        hi = int((points[-1] + WINDOW_MAX_S) / FRAME_S)
        target = (points[-1] + WINDOW_TARGET_S) / FRAME_S
        span = db[lo:hi]
        # Quietest frame wins; ties broken by distance to the target size.
        order = np.lexsort((np.abs(np.arange(lo, hi) - target), np.round(span, 0)))
        points.append((lo + int(order[0])) * FRAME_S)
    points.append(total_s)
    return points


def tokens_to_words(tokens: list[str], starts: list[float], durs: list[float], offset: float) -> list[dict]:
    """Merge SentencePiece tokens into words. A leading space opens a new word;
    bare punctuation tokens attach to the previous word."""
    words: list[dict] = []
    for tok, st, du in zip(tokens, starts, durs):
        st, en = offset + float(st), offset + float(st) + float(du)
        piece = tok.replace("▁", " ")
        # Inverse text normalization can emit "40%" with no word marker after "de".
        glued_number = bool(words) and piece[:1].isdigit() and words[-1]["text"][-1:].isalpha()
        if words and not piece.startswith(" ") and not glued_number:
            words[-1]["text"] += piece
            if piece.strip() and any(c.isalnum() for c in piece):
                words[-1]["end"] = en
            continue
        if not piece.strip():
            continue
        words.append({"text": piece.strip(), "start": st, "end": en})
    return words


def tighten_ends(words: list[dict], db: np.ndarray) -> None:
    """Clamp each word end to the next word start, then trim trailing
    sub-floor frames so silences are measured from real speech."""
    for i, w in enumerate(words):
        if i + 1 < len(words):
            w["end"] = min(w["end"], words[i + 1]["start"])
        lo = int(w["start"] / FRAME_S)
        hi = min(int(w["end"] / FRAME_S) + 1, len(db))
        loud = np.nonzero(db[lo:hi] > SPEECH_FLOOR_DB)[0]
        if len(loud):
            w["end"] = max(w["start"] + 0.04, min(w["end"], (lo + loud[-1] + 1) * FRAME_S))
        w["start"], w["end"] = round(w["start"], 3), round(w["end"], 3)


def to_scribe(words: list[dict], language: str | None) -> dict:
    out: list[dict] = []
    for i, w in enumerate(words):
        if i and w["start"] > out[-1]["end"]:
            out.append({"text": " ", "start": out[-1]["end"], "end": w["start"], "type": "spacing",
                        "speaker_id": "speaker_0"})
        out.append({**w, "type": "word", "speaker_id": "speaker_0"})
    return {
        "language_code": language or "auto",
        "text": " ".join(w["text"] for w in words),
        "words": out,
        "engine": f"local:{MODEL_NAME}",
    }


def transcribe_local_one(
    video: Path,
    edit_dir: Path,
    language: str | None = None,
    verbose: bool = True,
    audio_track: int = 0,
) -> Path:
    """Transcribe one video offline. Cached like transcribe_one()."""
    out_path = transcript_path(edit_dir, video, audio_track)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.exists():
        if verbose:
            print(f"cached: {out_path.name}")
        return out_path

    model = ensure_model(verbose)
    recognizer = load_recognizer(model)
    n_tracks = count_audio_tracks(video)
    if n_tracks > 1 and verbose:
        print(f"  note: {video.name} has {n_tracks} audio tracks, using track {audio_track + 1}", flush=True)

    t0 = time.time()
    with tempfile.TemporaryDirectory() as tmp:
        wav = Path(tmp) / f"{video.stem}.wav"
        extract_audio(video, wav, audio_track)
        peak = peak_dbfs(wav)
        if peak < -60.0:
            raise RuntimeError(f"track {audio_track + 1} of {video.name} is silent (peak {peak:.1f} dBFS)")
        audio = read_wav(wav)

    total_s = len(audio) / SAMPLE_RATE
    db = frame_db(audio)
    bounds = split_points(db, total_s)
    words: list[dict] = []
    for a, b in zip(bounds, bounds[1:]):
        chunk = audio[int(a * SAMPLE_RATE): int(b * SAMPLE_RATE)]
        stream = recognizer.create_stream()
        stream.accept_waveform(SAMPLE_RATE, chunk)
        recognizer.decode_stream(stream)
        r = stream.result
        words += tokens_to_words(list(r.tokens), list(r.timestamps), list(r.durations), a)
        if verbose:
            print(f"  [{a:7.2f}-{b:7.2f}] {r.text[:90]}", flush=True)
    tighten_ends(words, db)

    out_path.write_text(json.dumps(to_scribe(words, language), indent=2, ensure_ascii=False))
    if verbose:
        print(f"  saved: {out_path.name} ({len(words)} words, {total_s:.1f}s audio) in {time.time() - t0:.1f}s")
    return out_path


def main() -> None:
    ap = argparse.ArgumentParser(description="Transcribe a video offline (Parakeet v3)")
    ap.add_argument("video", type=Path)
    ap.add_argument("--edit-dir", type=Path, default=None, help="Default: <video_parent>/edit")
    ap.add_argument("--language", default=None, help="Recorded in the JSON only; the model auto-detects")
    ap.add_argument("--audio-track", type=int, default=0)
    args = ap.parse_args()

    video = args.video.resolve()
    if not video.exists():
        sys.exit(f"video not found: {video}")
    edit_dir = (args.edit_dir or (video.parent / "edit")).resolve()
    transcribe_local_one(video, edit_dir, args.language, audio_track=args.audio_track)


if __name__ == "__main__":
    main()
