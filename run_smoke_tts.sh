#!/usr/bin/env bash
set -euo pipefail

RUNTIME_ENV_FILE="${BRMMEDIA_RUNTIME_ENV_FILE:-/etc/brmmedia/runtime.env}"
runtime_env_value() {
  local key="$1"
  [ -r "$RUNTIME_ENV_FILE" ] || return 0
  sed -n "s/^${key}=//p" "$RUNTIME_ENV_FILE" | tail -n1
}

APP_ROOT="${BRMMEDIA_APP_ROOT:-$(runtime_env_value BRMMEDIA_APP_ROOT)}"
SCRIPT_ROOT="${BRMMEDIA_SOURCE_ROOT:-$(runtime_env_value BRMMEDIA_SOURCE_ROOT)}"
APP_ROOT="${APP_ROOT:-/srv/brmmedia/app}"
SCRIPT_ROOT="${SCRIPT_ROOT:-/mnt/d/brmmedia/source}"
BACKEND_DIR="$APP_ROOT/ubuntu-backend-deploy"
LOG_FILE="${BRMMEDIA_SMOKE_LOG_DIR:-/srv/brmmedia/logs}/smoke-tts.log"

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
