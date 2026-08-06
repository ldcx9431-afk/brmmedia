#!/usr/bin/env bash
# Prepare an isolated ComfyUI + Gradio configuration for H3 burn-in.  It is
# intentionally separate from the production LTX checkout so completion of
# the expensive H3 acceptance suite is a prerequisite for production cutover.
set -euo pipefail

APP_ROOT="${BRMMEDIA_APP_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
BACKEND_DIR="$APP_ROOT/ubuntu-backend-deploy"
BACKEND_ENV="$BACKEND_DIR/.env"
SERVICE_USER="${BRMMEDIA_SERVICE_USER:-brm}"
CANARY_COMFY_ROOT="${BRMMEDIA_H3_CANARY_COMFY_ROOT:-/srv/brmmedia/ComfyUI-h3-canary}"
CANARY_ENV="$APP_ROOT/runtime-locks/h3-canary.env"
CANARY_OUTPUT_DIR="$BACKEND_DIR/outputs-h3-canary"

if [ "$(id -u)" -ne 0 ]; then
  echo "[ERROR] Run with sudo: sudo $0" >&2
  exit 1
fi
if [ ! -f "$BACKEND_ENV" ] || [ ! -x "$BACKEND_DIR/start_backend.sh" ]; then
  echo "[ERROR] Candidate runtime is incomplete: $APP_ROOT" >&2
  exit 1
fi
if ! id "$SERVICE_USER" >/dev/null 2>&1; then
  echo "[ERROR] Linux service user does not exist: $SERVICE_USER" >&2
  exit 1
fi
if systemctl is-active --quiet baorong-backend || systemctl is-active --quiet baorong-backend-h3-canary; then
  echo "[ERROR] Drain and stop the production backend before preparing the A5000 H3 canary." >&2
  exit 1
fi

dotenv_value() {
  sed -n "s/^$1=//p" "$BACKEND_ENV" | tail -n1 | sed -e 's/^"//' -e 's/"$//'
}
set_dotenv_value() {
  local key="$1" value="$2" escaped
  escaped="$(printf '%s' "$value" | sed 's/[&|]/\\&/g')"
  if grep -q "^$key=" "$CANARY_ENV"; then
    sed -i "s|^$key=.*|$key=$escaped|" "$CANARY_ENV"
  else
    printf '\n%s=%s\n' "$key" "$value" >> "$CANARY_ENV"
  fi
}

PRODUCTION_COMFY_ROOT="$(dotenv_value COMFYUI_ROOT)"
PRODUCTION_COMFY_ROOT="${PRODUCTION_COMFY_ROOT:-/srv/brmmedia/ComfyUI}"
COMFY_PYTHON="$(dotenv_value COMFYUI_PYTHON)"
COMFY_PYTHON="${COMFY_PYTHON:-$BACKEND_DIR/.venv/bin/python}"
if [ ! -d "$PRODUCTION_COMFY_ROOT/.git" ] || [ ! -x "$COMFY_PYTHON" ]; then
  echo "[ERROR] Production ComfyUI source or candidate Python is unavailable." >&2
  exit 1
fi
if [ -n "$(git -C "$PRODUCTION_COMFY_ROOT" status --porcelain)" ]; then
  echo "[ERROR] Production ComfyUI has local changes; preserve them before creating a reproducible canary." >&2
  exit 1
fi

if [ ! -e "$CANARY_COMFY_ROOT" ]; then
  install -d -o "$SERVICE_USER" -g "$SERVICE_USER" "$(dirname "$CANARY_COMFY_ROOT")"
  runuser -u "$SERVICE_USER" -- git clone --shared "$PRODUCTION_COMFY_ROOT" "$CANARY_COMFY_ROOT"
elif [ ! -d "$CANARY_COMFY_ROOT/.git" ]; then
  echo "[ERROR] Existing canary path is not a Git checkout: $CANARY_COMFY_ROOT" >&2
  exit 1
fi

# Reuse immutable weights instead of duplicating the 42+ GB model store.
# A new ComfyUI clone normally has no model directory.  Refuse to replace a
# populated non-link directory so this script cannot discard operator data.
if [ -e "$CANARY_COMFY_ROOT/models" ] && [ ! -L "$CANARY_COMFY_ROOT/models" ]; then
  if [ -n "$(find "$CANARY_COMFY_ROOT/models" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)" ]; then
    echo "[ERROR] Canary model directory is populated and not a shared-model symlink: $CANARY_COMFY_ROOT/models" >&2
    exit 1
  fi
  rmdir "$CANARY_COMFY_ROOT/models"
fi
if [ ! -e "$CANARY_COMFY_ROOT/models" ]; then
  ln -s "$PRODUCTION_COMFY_ROOT/models" "$CANARY_COMFY_ROOT/models"
fi
if [ -d "$PRODUCTION_COMFY_ROOT/custom_nodes" ]; then
  install -d -o "$SERVICE_USER" -g "$SERVICE_USER" "$CANARY_COMFY_ROOT/custom_nodes"
  rsync -a --delete "$PRODUCTION_COMFY_ROOT/custom_nodes/" "$CANARY_COMFY_ROOT/custom_nodes/"
fi
for config in extra_model_paths.yaml extra_model_paths.yaml.example; do
  if [ -f "$PRODUCTION_COMFY_ROOT/$config" ]; then
    install -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0644 "$PRODUCTION_COMFY_ROOT/$config" "$CANARY_COMFY_ROOT/$config"
  fi
done

install -d -o "$SERVICE_USER" -g "$SERVICE_USER" "$(dirname "$CANARY_ENV")" "$CANARY_OUTPUT_DIR"
install -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0600 "$BACKEND_ENV" "$CANARY_ENV"
set_dotenv_value COMFYUI_ROOT "$CANARY_COMFY_ROOT"
set_dotenv_value COMFYUI_PYTHON "$COMFY_PYTHON"
set_dotenv_value COMFYUI_PORT 8189
set_dotenv_value BRM_GRADIO_HOST 127.0.0.1
set_dotenv_value BRM_GRADIO_PORT 9001
set_dotenv_value BRMMEDIA_VIDEO_ENGINE h3
set_dotenv_value BRM_OUTPUT_DIR "$CANARY_OUTPUT_DIR"
set_dotenv_value BRMMEDIA_H3_CANARY_ROOT "$CANARY_COMFY_ROOT"

echo "[1/2] Verifying imported H3 components through the shared E: model store..."
runuser -u "$SERVICE_USER" -- env BRMMEDIA_REQUIRE_H3_MODELS=1 \
  BRMMEDIA_COMFYUI_MODEL_ROOT="$CANARY_COMFY_ROOT/models" \
  /usr/local/sbin/brmmedia-verify-comfy-models

echo "[2/2] Preparing the pinned native-H3 ComfyUI canary checkout..."
runuser -u "$SERVICE_USER" -- env COMFYUI_ROOT="$CANARY_COMFY_ROOT" \
  COMFYUI_PYTHON="$COMFY_PYTHON" BRMMEDIA_APP_ROOT="$APP_ROOT" \
  "$APP_ROOT/prepare_minimax_h3_comfyui.sh"

echo "[OK] H3 canary prepared. Start it with: sudo $APP_ROOT/start_minimax_h3_canary.sh"
echo "     env=$CANARY_ENV comfy=$CANARY_COMFY_ROOT gradio=127.0.0.1:9001 api=127.0.0.1:9101"
