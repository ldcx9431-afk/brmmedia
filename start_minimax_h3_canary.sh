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
HEALTH_TIMER="${BRMMEDIA_HEALTHCHECK_TIMER:-brmmedia-healthcheck.timer}"
HEALTH_SERVICE="${BRMMEDIA_HEALTHCHECK_SERVICE:-brmmedia-healthcheck.service}"
TIMEOUT_SECONDS="${BRMMEDIA_H3_CANARY_STARTUP_TIMEOUT_SECONDS:-420}"
CANARY_STATE_DIR="$APP_ROOT/runtime-locks"
HEALTH_TIMER_STATE="$CANARY_STATE_DIR/h3-canary-healthcheck-timer-state"

if [ "$(id -u)" -ne 0 ]; then
  echo "[ERROR] Run with sudo: sudo $0" >&2
  exit 1
fi
if [ ! -f "$CANARY_ENV" ] || [ ! -x "$BACKEND_DIR/start_backend.sh" ]; then
  echo "[ERROR] Canary is not prepared. Run $APP_ROOT/prepare_minimax_h3_canary.sh first." >&2
  exit 1
fi
if [ ! -x "$APP_ROOT/verify_h3_workflow_gate.sh" ]; then
  echo "[ERROR] H3 workflow compatibility gate is missing: $APP_ROOT/verify_h3_workflow_gate.sh" >&2
  exit 1
fi
if ! id "$SERVICE_USER" >/dev/null 2>&1; then
  echo "[ERROR] Linux service user does not exist: $SERVICE_USER" >&2
  exit 1
fi
if ! [[ "$TIMEOUT_SECONDS" =~ ^[1-9][0-9]*$ ]]; then
  echo "[ERROR] BRMMEDIA_H3_CANARY_STARTUP_TIMEOUT_SECONDS must be a positive integer." >&2
  exit 2
fi

# While a private Canary owns the shared A5000, the normal backend is
# intentionally stopped.  Pause the production health-check timer or it will
# restart that backend after three expected failures and leave two ComfyUI
# processes contending for GPU0.  The paired stop script restores only the
# prior runtime state; the production timer is never disabled permanently.
mkdir -p "$CANARY_STATE_DIR"
timer_was_active=0
if systemctl is-active --quiet "$HEALTH_TIMER"; then
  timer_was_active=1
  systemctl stop "$HEALTH_TIMER"
fi
systemctl stop "$HEALTH_SERVICE" 2>/dev/null || true
printf '%s\n' "$timer_was_active" > "$HEALTH_TIMER_STATE"

restore_health_timer_on_error() {
  if [ "$timer_was_active" -eq 1 ]; then
    systemctl start "$HEALTH_TIMER" || true
  fi
  rm -f "$HEALTH_TIMER_STATE"
}

# Quiesce the production watchdog before checking the backend state.  There is
# otherwise a small but real race between the caller stopping production and
# this canary acquiring the shared A5000: a timer tick can restart production
# after the initial check, leaving two ComfyUI processes on GPU0.  If a caller
# did not drain production, restore the exact prior timer state and fail.
if systemctl is-active --quiet baorong-backend; then
  restore_health_timer_on_error
  echo "[ERROR] Stop the production backend before starting the shared-A5000 canary." >&2
  exit 1
fi

dotenv_value() {
  sed -n "s/^$1=//p" "$CANARY_ENV" | tail -n1 | sed -e 's/^"//' -e 's/"$//'
}
COMFY_ROOT="$(dotenv_value COMFYUI_ROOT)"
COMFY_PORT="$(dotenv_value COMFYUI_PORT)"
GRADIO_PORT="$(dotenv_value BRM_GRADIO_PORT)"
OUTPUT_DIR="$(dotenv_value BRM_OUTPUT_DIR)"
CANARY_VENV="$(dotenv_value BRMMEDIA_BACKEND_VENV)"
COMFY_ROOT="${COMFY_ROOT:-/srv/brmmedia/ComfyUI-h3-canary}"
COMFY_PORT="${COMFY_PORT:-8189}"
GRADIO_PORT="${GRADIO_PORT:-9001}"
OUTPUT_DIR="${OUTPUT_DIR:-$BACKEND_DIR/outputs-h3-canary}"
CANARY_VENV="${CANARY_VENV:-$APP_ROOT/runtime-locks/venvs/h3-canary}"
CANARY_PYTHON="$CANARY_VENV/bin/python"
if ! [[ "$COMFY_PORT" =~ ^[1-9][0-9]*$ ]] || ! [[ "$GRADIO_PORT" =~ ^[1-9][0-9]*$ ]]; then
  echo "[ERROR] Canary port configuration is invalid." >&2
  exit 2
fi
if [ -L "$CANARY_VENV" ] || [ ! -x "$CANARY_PYTHON" ]; then
  echo "[ERROR] Canary requires its prepared physical Python venv: $CANARY_VENV" >&2
  exit 1
fi

systemctl stop "$BACKEND_UNIT" "$API_UNIT" 2>/dev/null || true
systemd-run --unit="$BACKEND_UNIT" --collect \
  --property="User=$SERVICE_USER" --property="WorkingDirectory=$BACKEND_DIR" \
  --property=Nice=5 --setenv="BRMMEDIA_ENV_FILE=$CANARY_ENV" \
  --setenv="BRMMEDIA_BACKEND_VENV=$CANARY_VENV" \
  /usr/bin/env bash "$BACKEND_DIR/start_backend.sh"
systemd-run --unit="$API_UNIT" --collect \
  --property="User=$SERVICE_USER" --property="WorkingDirectory=$BACKEND_DIR" \
  --property=Nice=5 --setenv="COMFYUI_ROOT=$COMFY_ROOT" \
  --setenv="COMFYUI_PORT=$COMFY_PORT" --setenv="BRMMEDIA_VIDEO_ENGINE=h3" \
  --setenv="BRM_GRADIO_API_BASE=http://127.0.0.1:$GRADIO_PORT" \
  --setenv="BRM_OUTPUT_DIR=$OUTPUT_DIR" \
  "$CANARY_PYTHON" -m uvicorn lan_api:app --host 127.0.0.1 --port 9101 --proxy-headers

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

echo "[gate] Validating live H3/custom-node workflow compatibility before canary acceptance..."
if ! runuser -u "$SERVICE_USER" -- env BRMMEDIA_APP_ROOT="$APP_ROOT" \
  BRMMEDIA_H3_GATE_PYTHON="$CANARY_PYTHON" \
  "$APP_ROOT/verify_h3_workflow_gate.sh" \
  --url "http://127.0.0.1:$COMFY_PORT" \
  --workflows "$BACKEND_DIR/workflows" \
  --timeout 15; then
  # The canary is intentionally not exposed through Nginx, but leaving an
  # incompatible process alive would still consume the shared A5000 and make
  # recovery ambiguous.  Tear down only its transient units; production was
  # already stopped by the explicit canary procedure and is never altered.
  echo "[ERROR] H3 canary compatibility gate failed; stopping isolated canary units." >&2
  systemctl stop "$API_UNIT" "$BACKEND_UNIT" || true
  restore_health_timer_on_error
  exit 1
fi

echo "[OK] H3 canary is ready on loopback Gradio :$GRADIO_PORT and REST :9101."
echo "     Run acceptance only against http://127.0.0.1:9101/api/v1; do not expose this port through Nginx."
