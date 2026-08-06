#!/usr/bin/env bash
# Stop only the isolated H3 canary processes.  It deliberately preserves the
# canary checkout, configuration and outputs for investigation/retry.
set -euo pipefail

BACKEND_UNIT="${BRMMEDIA_H3_CANARY_BACKEND_UNIT:-baorong-backend-h3-canary}"
API_UNIT="${BRMMEDIA_H3_CANARY_API_UNIT:-brmmedia-lan-api-h3-canary}"

if [ "$(id -u)" -ne 0 ]; then
  echo "[ERROR] Run with sudo: sudo $0" >&2
  exit 1
fi
systemctl stop "$API_UNIT" "$BACKEND_UNIT" 2>/dev/null || true
if systemctl is-active --quiet "$API_UNIT" || systemctl is-active --quiet "$BACKEND_UNIT"; then
  echo "[ERROR] One or more H3 canary units are still active." >&2
  exit 1
fi
echo "[OK] H3 canary services stopped; production service configuration was not changed."
