#!/usr/bin/env bash
set -euo pipefail

APP_ROOT=/srv/brmmedia/app
VENV_PYTHON="$APP_ROOT/ubuntu-backend-deploy/.venv/bin/python"
REQUIREMENTS="$APP_ROOT/ubuntu-backend-deploy/requirements-backend.txt"
LOG_FILE=/srv/brmmedia/logs/backend-requirements.log

if pgrep -f 'requirements-backend.txt' >/dev/null 2>&1; then
  echo "[INFO] Backend requirements installation is already running."
  exit 0
fi

nohup "$VENV_PYTHON" -m pip install -r "$REQUIREMENTS" \
  >"$LOG_FILE" 2>&1 < /dev/null &
echo "[OK] Backend requirements installer started (pid=$!). Log: $LOG_FILE"
