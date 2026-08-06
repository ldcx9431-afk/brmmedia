#!/usr/bin/env bash
# Restore the exact ComfyUI checkout recorded before a MiniMax H3 upgrade.
# Run this only when no media job is active.
set -euo pipefail

COMFY_ROOT="${COMFYUI_ROOT:-/srv/brmmedia/ComfyUI}"
APP_ROOT="${BRMMEDIA_APP_ROOT:-/srv/brmmedia/app}"
PYTHON_BIN="${COMFYUI_PYTHON:-$APP_ROOT/ubuntu-backend-deploy/.venv/bin/python}"
BACKUP_DIR="$APP_ROOT/runtime-locks/comfyui-h3-backups"
backup_file="${1:-$(ls -1t "$BACKUP_DIR"/before-h3-*.commit 2>/dev/null | head -n1)}"

if [ -z "$backup_file" ] || [ ! -f "$backup_file" ]; then
  echo "[ERROR] No MiniMax H3 pre-upgrade commit record found in $BACKUP_DIR" >&2
  exit 1
fi
if systemctl is-active --quiet baorong-backend; then
  echo "[ERROR] Stop baorong-backend before rolling back ComfyUI." >&2
  exit 1
fi

previous_ref="$(tr -d '[:space:]' < "$backup_file")"
git -C "$COMFY_ROOT" checkout --detach "$previous_ref"
"$PYTHON_BIN" -m pip install -r "$COMFY_ROOT/requirements.txt"
echo "[OK] ComfyUI restored to $previous_ref. Start baorong-backend, then run brmmedia-verify-runtime."
