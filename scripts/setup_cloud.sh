#!/usr/bin/env bash
# One-time setup for the cut → animate pipeline (PIPELINE.md) on a fresh
# machine or cloud container. Idempotent: safe to run at every session start.
#
#   bash scripts/setup_cloud.sh
#
# Installs the Python deps + offline ASR runtime, downloads the Parakeet v3
# model (~490 MB, from GitHub releases), makes sure the HyperFrames checkout
# sits next to this repo, and warms the HyperFrames CLI.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
HF_DIR="${HYPERFRAMES_DIR:-$(dirname "$REPO")/hyperframes}"

command -v ffmpeg >/dev/null || { echo "ffmpeg missing — install it first (apt-get install -y ffmpeg)"; exit 1; }
command -v node >/dev/null || { echo "Node.js 22+ missing — needed by HyperFrames"; exit 1; }

echo "━━ python deps"
python3 -m pip install -q -e "$REPO[local]" 2>&1 | grep -v "WARNING: Running pip as the 'root'" || true

echo "━━ offline ASR model"
(cd "$REPO/helpers" && python3 -c "from transcribe_local import ensure_model; print(ensure_model())")

echo "━━ hyperframes checkout"
if [ ! -f "$HF_DIR/skills/talking-head-recut/SKILL.md" ]; then
  git clone --depth 1 https://github.com/heygen-com/hyperframes "$HF_DIR"
fi
echo "$HF_DIR"

echo "━━ hyperframes CLI"
npx --yes hyperframes --version

echo "ready — see $REPO/PIPELINE.md"
