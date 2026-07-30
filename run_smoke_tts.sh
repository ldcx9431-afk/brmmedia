#!/usr/bin/env bash
set -euo pipefail

BACKEND_DIR=/srv/brmmedia/app/ubuntu-backend-deploy
SCRIPT_ROOT=/mnt/d/brmmedia/source
LOG_FILE=/srv/brmmedia/logs/smoke-tts.log

mkdir -p "$(dirname "$LOG_FILE")"
exec >>"$LOG_FILE" 2>&1
echo "===== smoke-tts start $(date -Is) ====="
cd "$BACKEND_DIR"
set -a
# shellcheck disable=SC1091
source .env
set +a
export PYTHONPATH="$BACKEND_DIR${PYTHONPATH:+:$PYTHONPATH}"
exec "$BACKEND_DIR/.venv/bin/python" "$SCRIPT_ROOT/smoke_tts.py"
