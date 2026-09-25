#!/usr/bin/env bash
set -euo pipefail

APP_ROOT=/srv/brmmedia/app
LOG_FILE=/srv/brmmedia/logs/model-import.log

if pgrep -f import_comfy_models.sh >/dev/null 2>&1; then
  echo "[INFO] Model import is already running."
  exit 0
fi

nohup bash /mnt/d/brmmedia/source/import_comfy_models.sh \
  >"$LOG_FILE" 2>&1 < /dev/null &
echo "[OK] Model import started (pid=$!). Log: $LOG_FILE"
