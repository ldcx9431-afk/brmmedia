#!/usr/bin/env bash
set -euo pipefail

APP_ROOT=/srv/brmmedia/app
LOG_FILE=/srv/brmmedia/logs/install_backend.log

if pgrep -f 'install_ubuntu.sh' >/dev/null 2>&1; then
  echo "[INFO] Backend installer is already running."
  exit 0
fi

mkdir -p "$(dirname "$LOG_FILE")"
nohup env INSTALL_ROOT=/srv/brmmedia \
  bash "$APP_ROOT/ubuntu-backend-deploy/install_ubuntu.sh" \
  >"$LOG_FILE" 2>&1 < /dev/null &
echo "[OK] Backend installer started (pid=$!). Log: $LOG_FILE"
