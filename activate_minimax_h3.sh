#!/usr/bin/env bash
# Make MiniMax H3 the active T2V/I2V engine only after all local prerequisites
# are verifiable.  This is intentionally root-only because it changes the
# running backend service and the ComfyUI checkout.
set -euo pipefail

# Activation is executed from the staged release during cutover.  Defaulting
# to the legacy recovery checkout here would validate one release and start
# another, so derive the runtime root from this script unless an explicit
# override is supplied for an unusual layout.
SCRIPT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_ROOT="${BRMMEDIA_APP_ROOT:-$SCRIPT_ROOT}"
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

# The candidate dotenv is what start_backend.sh will source.  Resolve the
# same values here rather than accepting a caller's COMFYUI_ROOT override,
# which could otherwise prepare one checkout and launch another.
dotenv_value() {
  sed -n "s/^$1=//p" "$BACKEND_ENV" | tail -n1 | sed -e 's/^"//' -e 's/"$//'
}
COMFY_ROOT="$(dotenv_value COMFYUI_ROOT)"
COMFY_ROOT="${COMFY_ROOT:-/srv/brmmedia/ComfyUI}"
COMFY_PYTHON="$(dotenv_value COMFYUI_PYTHON)"
COMFY_PYTHON="${COMFY_PYTHON:-$BACKEND_DIR/.venv/bin/python}"
COMFY_PORT="$(dotenv_value COMFYUI_PORT)"
COMFY_PORT="${COMFY_PORT:-8188}"
GRADIO_PORT="$(dotenv_value BRM_GRADIO_PORT)"
GRADIO_PORT="${GRADIO_PORT:-9000}"
HEALTH_TIMEOUT_SECONDS="${BRMMEDIA_H3_STARTUP_HEALTH_TIMEOUT_SECONDS:-420}"
if [ ! -d "$COMFY_ROOT" ] || [ ! -x "$COMFY_PYTHON" ]; then
  echo "[ERROR] Candidate ComfyUI runtime is invalid (root=$COMFY_ROOT, python=$COMFY_PYTHON)." >&2
  exit 1
fi
if ! [[ "$COMFY_PORT" =~ ^[1-9][0-9]*$ ]] || ! [[ "$GRADIO_PORT" =~ ^[1-9][0-9]*$ ]] || \
   ! [[ "$HEALTH_TIMEOUT_SECONDS" =~ ^[1-9][0-9]*$ ]]; then
  echo "[ERROR] Candidate health-check ports/timeout are invalid." >&2
  exit 1
fi
if ! grep -q 'MiniMaxH3-文生视频' "$BACKEND_DIR/webui.py" || \
   [ ! -f "$BACKEND_DIR/workflows/MiniMaxH3-文生视频.json" ] || \
   [ ! -f "$BACKEND_DIR/workflows/MiniMaxH3-图生视频.json" ]; then
  echo "[ERROR] Runtime application does not contain the H3 release. Sync the reviewed source first." >&2
  exit 1
fi
if [ ! -x "$APP_ROOT/check_h3_preflight.sh" ]; then
  echo "[ERROR] H3 preflight script is missing: $APP_ROOT/check_h3_preflight.sh" >&2
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

previous_engine="$(dotenv_value BRMMEDIA_VIDEO_ENGINE)"
previous_engine="${previous_engine:-ltx23}"
engine_updated=0
restore_after_failed_activation() {
  local rollback_status=0
  if [ "$engine_updated" -eq 1 ]; then
    echo "[WARN] H3 activation failed; restoring candidate video engine to $previous_engine." >&2
    if grep -q '^BRMMEDIA_VIDEO_ENGINE=' "$BACKEND_ENV"; then
      sed -i "s/^BRMMEDIA_VIDEO_ENGINE=.*/BRMMEDIA_VIDEO_ENGINE=$previous_engine/" "$BACKEND_ENV"
    else
      printf '\nBRMMEDIA_VIDEO_ENGINE=%s\n' "$previous_engine" >> "$BACKEND_ENV"
    fi
    # prepare_minimax_h3_comfyui.sh has already completed at this point, so
    # invoke its durable snapshot rollback while the failed backend is down.
    if ! runuser -u "$SERVICE_USER" -- env COMFYUI_ROOT="$COMFY_ROOT" \
      COMFYUI_PYTHON="$COMFY_PYTHON" BRMMEDIA_APP_ROOT="$APP_ROOT" \
      "$APP_ROOT/rollback_minimax_h3_comfyui.sh"; then
      rollback_status=1
      echo "[WARN] Automatic ComfyUI rollback failed; inspect $APP_ROOT/runtime-locks/comfyui-h3-backups." >&2
    fi
  fi
  return "$rollback_status"
}
trap restore_after_failed_activation ERR

echo "[preflight] Verifying GPU, memory, page-file, and disk prerequisites..."
BRMMEDIA_APP_ROOT="$APP_ROOT" "$APP_ROOT/check_h3_preflight.sh"

echo "[1/4] Verifying all H3 components were imported into the E: WSL model store..."
runuser -u "$SERVICE_USER" -- env BRMMEDIA_REQUIRE_H3_MODELS=1 \
  /usr/local/sbin/brmmedia-verify-comfy-models

echo "[2/4] Upgrading ComfyUI to the pinned native-H3 revision..."
runuser -u "$SERVICE_USER" -- env COMFYUI_ROOT="$COMFY_ROOT" COMFYUI_PYTHON="$COMFY_PYTHON" BRMMEDIA_APP_ROOT="$APP_ROOT" \
  "$APP_ROOT/prepare_minimax_h3_comfyui.sh"

echo "[3/4] Enabling H3 in the backend environment..."
if grep -q '^BRMMEDIA_VIDEO_ENGINE=' "$BACKEND_ENV"; then
  sed -i 's/^BRMMEDIA_VIDEO_ENGINE=.*/BRMMEDIA_VIDEO_ENGINE=h3/' "$BACKEND_ENV"
else
  printf '\nBRMMEDIA_VIDEO_ENGINE=h3\n' >> "$BACKEND_ENV"
fi
engine_updated=1

echo "[4/4] Starting backend; run H3 preview T2V and I2V smoke tests next..."
systemctl start baorong-backend
systemctl is-active --quiet baorong-backend
deadline=$((SECONDS + HEALTH_TIMEOUT_SECONDS))
while :; do
  gradio_code="$(curl --silent --output /dev/null --write-out '%{http_code}' --max-time 15 \
    "http://127.0.0.1:$GRADIO_PORT/gradio_api/info" || true)"
  comfy_code="$(curl --silent --output /dev/null --write-out '%{http_code}' --max-time 15 \
    "http://127.0.0.1:$COMFY_PORT/system_stats" || true)"
  if [ "$gradio_code" = "200" ] && [ "$comfy_code" = "200" ]; then
    break
  fi
  if [ "$SECONDS" -ge "$deadline" ] || ! systemctl is-active --quiet baorong-backend; then
    echo "[ERROR] Candidate backend did not become healthy (gradio=$gradio_code, comfy=$comfy_code)." >&2
    exit 1
  fi
  sleep 3
done
trap - ERR
echo "[OK] MiniMax H3 is active and both Gradio/ComfyUI health endpoints returned HTTP 200."
echo "[WARN] Do not call this production-ready until the preview/quality and regression acceptance suite passes."
