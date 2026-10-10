#!/usr/bin/env bash
set -euo pipefail

MODEL_ROOT=/mnt/d/models
MODEL_DIR="$MODEL_ROOT/Qwen3.5-4B-AWQ-4bit"
MODEL_REPO=https://huggingface.co/cyankiwi/Qwen3.5-4B-AWQ-4bit
RUNTIME_DIR=/srv/brmmedia/models/Qwen3.5-4B-AWQ-4bit
LOG_FILE=/srv/brmmedia/logs/qwen35-download.log

mkdir -p "$MODEL_ROOT" "$(dirname "$LOG_FILE")"
exec >>"$LOG_FILE" 2>&1

echo "===== qwen35 download start $(date -Is) ====="
if [ -d "$MODEL_DIR/.git" ] && git -C "$MODEL_DIR" rev-parse --verify HEAD >/dev/null 2>&1; then
  cd "$MODEL_DIR"
  git lfs pull
else
  if [ -e "$MODEL_DIR" ]; then
    PARTIAL_DIR="${MODEL_DIR}.partial-$(date +%Y%m%dT%H%M%S)"
    mv "$MODEL_DIR" "$PARTIAL_DIR"
    echo "preserved incomplete clone at $PARTIAL_DIR"
  fi
  git lfs install
  git clone "$MODEL_REPO" "$MODEL_DIR"
fi

test -f "$MODEL_DIR/config.json"
mkdir -p "$RUNTIME_DIR"
rsync -a --partial --append-verify "$MODEL_DIR/" "$RUNTIME_DIR/"
echo "===== qwen35 download complete $(date -Is) ====="
