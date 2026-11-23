#!/usr/bin/env bash
# Root-owned systemd entrypoint: restore resumable downloads after boot, then
# hand the waiting/import phase to the normal BRMMedia service account.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_USER="${BRMMEDIA_SERVICE_USER:-brm}"

if [ "$(id -u)" -ne 0 ]; then
  echo "[ERROR] Run through root-owned systemd or sudo." >&2
  exit 1
fi
if ! id "$SERVICE_USER" >/dev/null 2>&1; then
  echo "[ERROR] Service user does not exist: $SERVICE_USER" >&2
  exit 1
fi

"$SCRIPT_DIR/start_minimax_h3_download.sh"
exec runuser -u "$SERVICE_USER" -- \
  env BRMMEDIA_SERVICE_USER="$SERVICE_USER" \
  "$SCRIPT_DIR/wait_import_minimax_h3_models.sh"
