#!/usr/bin/env bash
# Make MiniMax H3 the active T2V/I2V engine only after all local prerequisites
# are verifiable.  This is intentionally root-only because it changes the
# running backend service and the ComfyUI checkout.
set -euo pipefail

APP_ROOT="${BRMMEDIA_APP_ROOT:-/srv/brmmedia/app}"
COMFY_ROOT="${COMFYUI_ROOT:-/srv/brmmedia/ComfyUI}"
BACKEND_DIR="$APP_ROOT/ubuntu-backend-deploy"
BACKEND_ENV="$BACKEND_DIR/.env"
SERVICE_USER="${BRMMEDIA_SERVICE_USER:-brm}"

if [ "$(id -u)" -ne 0 ]; then
  echo "[ERROR] Run with sudo: sudo $0" >&2
  exit 1
fi
if [ ! -f "$BACKEND_ENV" ] || [ ! -f "$BACKEND_DIR/webui.py" ]; then
  echo "[ERROR] Runtime application is incomplete: $APP_ROOT" >&2
  exit 1
fi
if ! grep -q 'MiniMaxH3-文生视频' "$BACKEND_DIR/webui.py" || \
   [ ! -f "$BACKEND_DIR/workflows/MiniMaxH3-文生视频.json" ] || \
   [ ! -f "$BACKEND_DIR/workflows/MiniMaxH3-图生视频.json" ]; then
  echo "[ERROR] Runtime application does not contain the H3 release. Sync the reviewed source first." >&2
  exit 1
fi
if systemctl is-active --quiet baorong-backend; then
  echo "[ERROR] Stop or drain media tasks before activating H3; this script will not interrupt a running job." >&2
  exit 1
fi

# The H3 environment is deliberately isolated in a candidate release.  Do not
# allow a successful-looking activation to start the old production checkout:
# systemd must first have been regenerated for this exact APP_ROOT.
expected_exec="$BACKEND_DIR/start_backend.sh"
if ! systemctl cat baorong-backend 2>/dev/null | grep -Fq "ExecStart=$expected_exec"; then
  echo "[ERROR] baorong-backend is not installed for this candidate release." >&2
  echo "[ERROR] First run: sudo $APP_ROOT/install_ubuntu_systemd_services.sh $SERVICE_USER $APP_ROOT" >&2
  exit 1
fi

echo "[1/4] Verifying all H3 components were imported into the E: WSL model store..."
runuser -u "$SERVICE_USER" -- env BRMMEDIA_REQUIRE_H3_MODELS=1 \
  /usr/local/sbin/brmmedia-verify-comfy-models

echo "[2/4] Upgrading ComfyUI to the pinned native-H3 revision..."
runuser -u "$SERVICE_USER" -- env COMFYUI_ROOT="$COMFY_ROOT" BRMMEDIA_APP_ROOT="$APP_ROOT" \
  "$APP_ROOT/prepare_minimax_h3_comfyui.sh"

echo "[3/4] Enabling H3 in the backend environment..."
if grep -q '^BRMMEDIA_VIDEO_ENGINE=' "$BACKEND_ENV"; then
  sed -i 's/^BRMMEDIA_VIDEO_ENGINE=.*/BRMMEDIA_VIDEO_ENGINE=h3/' "$BACKEND_ENV"
else
  printf '\nBRMMEDIA_VIDEO_ENGINE=h3\n' >> "$BACKEND_ENV"
fi

echo "[4/4] Starting backend; run H3 preview T2V and I2V smoke tests next..."
systemctl start baorong-backend
systemctl is-active --quiet baorong-backend
echo "[OK] MiniMax H3 is active. Do not call this production-ready until the preview/quality and regression acceptance suite passes."
