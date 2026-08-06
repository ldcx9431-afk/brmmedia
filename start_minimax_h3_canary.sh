#!/usr/bin/env bash
# Start the isolated H3 canary after prepare_minimax_h3_canary.sh.  Both
# services are loopback-only and use ports distinct from production, so the
# authenticated LAN entry cannot accidentally expose unaccepted H3 output.
set -euo pipefail

APP_ROOT="${BRMMEDIA_APP_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
BACKEND_DIR="$APP_ROOT/ubuntu-backend-deploy"
CANARY_ENV="$APP_ROOT/runtime-locks/h3-canary.env"
SERVICE_USER="${BRMMEDIA_SERVICE_USER:-brm}"
BACKEND_UNIT="${BRMMEDIA_H3_CANARY_BACKEND_UNIT:-baorong-backend-h3-canary}"
API_UNIT="${BRMMEDIA_H3_CANARY_API_UNIT:-brmmedia-lan-api-h3-canary}"
TIMEOUT_SECONDS="${BRMMEDIA_H3_CANARY_STARTUP_TIMEOUT_SECONDS:-420}"

if [ "$(id -u)" -ne 0 ]; then
  echo "[ERROR] Run with sudo: sudo $0" >&2
  exit 1
fi
if [ ! -f "$CANARY_ENV" ] || [ ! -x "$BACKEND_DIR/start_backend.sh" ]; then
  echo "[ERROR] Canary is not prepared. Run $APP_ROOT/prepare_minimax_h3_canary.sh first." >&2
  exit 1
fi
if ! id "$SERVICE_USER" >/dev/null 2>&1; then
  echo "[ERROR] Linux service user does not exist: $SERVICE_USER" >&2
  exit 1
fi
if systemctl is-active --quiet baorong-backend; then
  echo "[ERROR] Stop the production backend before starting the shared-A5000 canary." >&2
  exit 1
fi
if ! [[ "$TIMEOUT_SECONDS" =~ ^[1-9][0-9]*$ ]]; then
  echo "[ERROR] BRMMEDIA_H3_CANARY_STARTUP_TIMEOUT_SECONDS must be a positive integer." >&2
  exit 2
fi

dotenv_value() {
  sed -n "s/^$1=//p" "$CANARY_ENV" | tail -n1 | sed -e 's/^"//' -e 's/"$//'
}
COMFY_ROOT="$(dotenv_value COMFYUI_ROOT)"
COMFY_PORT="$(dotenv_value COMFYUI_PORT)"
GRADIO_PORT="$(dotenv_value BRM_GRADIO_PORT)"
OUTPUT_DIR="$(dotenv_value BRM_OUTPUT_DIR)"
COMFY_ROOT="${COMFY_ROOT:-/srv/brmmedia/ComfyUI-h3-canary}"
COMFY_PORT="${COMFY_PORT:-8189}"
GRADIO_PORT="${GRADIO_PORT:-9001}"
OUTPUT_DIR="${OUTPUT_DIR:-$BACKEND_DIR/outputs-h3-canary}"
if ! [[ "$COMFY_PORT" =~ ^[1-9][0-9]*$ ]] || ! [[ "$GRADIO_PORT" =~ ^[1-9][0-9]*$ ]]; then
  echo "[ERROR] Canary port configuration is invalid." >&2
  exit 2
fi

systemctl stop "$BACKEND_UNIT" "$API_UNIT" 2>/dev/null || true
systemd-run --unit="$BACKEND_UNIT" --collect \
  --property="User=$SERVICE_USER" --property="WorkingDirectory=$BACKEND_DIR" \
  --property=Nice=5 --setenv="BRMMEDIA_ENV_FILE=$CANARY_ENV" \
  /usr/bin/env bash "$BACKEND_DIR/start_backend.sh"
systemd-run --unit="$API_UNIT" --collect \
  --property="User=$SERVICE_USER" --property="WorkingDirectory=$BACKEND_DIR" \
  --property=Nice=5 --setenv="COMFYUI_ROOT=$COMFY_ROOT" \
  --setenv="COMFYUI_PORT=$COMFY_PORT" --setenv="BRMMEDIA_VIDEO_ENGINE=h3" \
  --setenv="BRM_GRADIO_API_BASE=http://127.0.0.1:$GRADIO_PORT" \
  --setenv="BRM_OUTPUT_DIR=$OUTPUT_DIR" \
  "$BACKEND_DIR/.venv/bin/python" -m uvicorn lan_api:app --host 127.0.0.1 --port 9101 --proxy-headers

deadline=$((SECONDS + TIMEOUT_SECONDS))
while :; do
  gradio="$(curl --silent --output /dev/null --write-out '%{http_code}' --max-time 15 "http://127.0.0.1:$GRADIO_PORT/gradio_api/info" || true)"
  comfy="$(curl --silent --output /dev/null --write-out '%{http_code}' --max-time 15 "http://127.0.0.1:$COMFY_PORT/system_stats" || true)"
  api="$(curl --silent --output /dev/null --write-out '%{http_code}' --max-time 15 http://127.0.0.1:9101/api/v1/health || true)"
  if [ "$gradio" = 200 ] && [ "$comfy" = 200 ] && [ "$api" = 200 ]; then
    break
  fi
  if [ "$SECONDS" -ge "$deadline" ] || ! systemctl is-active --quiet "$BACKEND_UNIT" || ! systemctl is-active --quiet "$API_UNIT"; then
    echo "[ERROR] H3 canary did not become healthy (gradio=$gradio comfy=$comfy api=$api)." >&2
    echo "[ERROR] Inspect: journalctl -u $BACKEND_UNIT -u $API_UNIT --no-pager -n 200" >&2
    exit 1
  fi
  sleep 3
done

echo "[OK] H3 canary is ready on loopback Gradio :$GRADIO_PORT and REST :9101."
echo "     Run acceptance only against http://127.0.0.1:9101/api/v1; do not expose this port through Nginx."
