#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_USER="${1:-$USER}"
APP_ROOT="${2:-$ROOT_DIR}"

if [ "$(id -u)" -ne 0 ]; then
  echo "[ERROR] Run with sudo:"
  echo "  sudo $0 <service-user> [runtime-app-root]"
  exit 1
fi

# The Windows SSH login is not necessarily a Linux account inside WSL.  Reuse
# the user of an already-running backend when an operator passed such a login;
# otherwise fail before installing units that systemd can never start.
if ! id "$SERVICE_USER" >/dev/null 2>&1; then
  existing_pid="$(systemctl show --property=MainPID --value baorong-backend 2>/dev/null || true)"
  existing_user=""
  if [ "${existing_pid:-0}" -gt 0 ] 2>/dev/null; then
    existing_user="$(ps -o user= -p "$existing_pid" 2>/dev/null | xargs || true)"
  fi
  if [ -n "$existing_user" ] && id "$existing_user" >/dev/null 2>&1; then
    echo "[WARN] Linux user '$SERVICE_USER' does not exist; reusing active backend user '$existing_user'."
    SERVICE_USER="$existing_user"
  else
    echo "[ERROR] Linux service user '$SERVICE_USER' does not exist."
    echo "[ERROR] Pass a valid WSL user, for example: sudo $0 brm [runtime-app-root]"
    exit 1
  fi
fi

echo "[INFO] Installing services for user: $SERVICE_USER"
echo "[INFO] Source root: $ROOT_DIR"
echo "[INFO] Runtime app root: $APP_ROOT"

if [ ! -x "$APP_ROOT/ubuntu-backend-deploy/start_backend.sh" ] || [ ! -x "$APP_ROOT/llm-backend-deploy/start_qwen_vllm.sh" ]; then
  echo "[ERROR] Runtime app root is incomplete: $APP_ROOT"
  exit 1
fi

# /usr/local/sbin/brmmedia-verify-runtime is shared across releases.  Record
# the release it must inspect so post-H3 acceptance never silently validates
# the preserved recovery checkout instead of the active candidate.
install -d -m 0755 /etc/brmmedia
printf 'BRMMEDIA_APP_ROOT=%s\nBRMMEDIA_SOURCE_ROOT=%s\n' "$APP_ROOT" "$ROOT_DIR" \
  > /etc/brmmedia/runtime.env
chmod 0644 /etc/brmmedia/runtime.env

install_service() {
  local src="$1"
  local dst="$2"
  local workdir="$3"
  local execstart="$4"

  # Unit 文件必须是普通配置文件；若目标曾被误设为可执行，cp 会保留
  # 目标权限并导致 systemd 发出警告。
  install -m 0644 "$src" "$dst"
  sed -i "s#YOUR_USER#$SERVICE_USER#g; s#^User=.*#User=$SERVICE_USER#" "$dst"
  sed -i "s#WorkingDirectory=.*#WorkingDirectory=$workdir#g" "$dst"
  sed -i "s#ExecStart=.*#ExecStart=$execstart#g" "$dst"
}

install_service \
  "$ROOT_DIR/ubuntu-backend-deploy/baorong-backend.service.example" \
  /etc/systemd/system/baorong-backend.service \
  "$APP_ROOT/ubuntu-backend-deploy" \
  "$APP_ROOT/ubuntu-backend-deploy/start_backend.sh"

install_service \
  "$ROOT_DIR/ubuntu-backend-deploy/brmmedia-lan-api.service.example" \
  /etc/systemd/system/brmmedia-lan-api.service \
  "$APP_ROOT/ubuntu-backend-deploy" \
  "$APP_ROOT/ubuntu-backend-deploy/.venv/bin/python -m uvicorn lan_api:app --host 127.0.0.1 --port 9100 --proxy-headers"

install -m 0755 "$ROOT_DIR/brmmedia-healthcheck.sh" /usr/local/sbin/brmmedia-healthcheck
install -m 0755 "$ROOT_DIR/brmmedia-verify-runtime.sh" /usr/local/sbin/brmmedia-verify-runtime
install -m 0755 "$ROOT_DIR/import_comfy_models.sh" /usr/local/sbin/brmmedia-import-comfy-models
install -m 0755 "$ROOT_DIR/verify_comfy_models.sh" /usr/local/sbin/brmmedia-verify-comfy-models
install -m 0644 "$ROOT_DIR/ubuntu-backend-deploy/brmmedia-healthcheck.service.example" \
  /etc/systemd/system/brmmedia-healthcheck.service
install -m 0644 "$ROOT_DIR/ubuntu-backend-deploy/brmmedia-healthcheck.timer.example" \
  /etc/systemd/system/brmmedia-healthcheck.timer
install -m 0644 "$ROOT_DIR/ubuntu-backend-deploy/brmmedia-logrotate.conf.example" \
  /etc/logrotate.d/brmmedia
if [ -f "$ROOT_DIR/ubuntu-backend-deploy/brmmedia-h3-stage.service.example" ]; then
  install -m 0644 "$ROOT_DIR/ubuntu-backend-deploy/brmmedia-h3-stage.service.example" \
    /etc/systemd/system/brmmedia-h3-stage.service
  sed -i "s#APP_ROOT#$APP_ROOT#g" /etc/systemd/system/brmmedia-h3-stage.service
fi

install_service \
  "$ROOT_DIR/llm-backend-deploy/qwen-vllm.service.example" \
  /etc/systemd/system/qwen-vllm.service \
  "$APP_ROOT/llm-backend-deploy" \
  "$APP_ROOT/llm-backend-deploy/start_qwen_vllm.sh"

# Legacy high-VRAM mode stole GPU0 and stopped Qwen.  Keep its source example
# for rollback only; prevent an old installed unit from competing with the
# production A5000-media/A4000-Qwen split profile.
systemctl disable --now baorong-backend-highvram.service 2>/dev/null || true

if [ -f "$ROOT_DIR/ubuntu-backend-deploy/baorong-model-import.service.example" ]; then
  install_service \
    "$ROOT_DIR/ubuntu-backend-deploy/baorong-model-import.service.example" \
    /etc/systemd/system/baorong-model-import.service \
    "$APP_ROOT" \
    "/usr/local/sbin/brmmedia-import-comfy-models"
fi

if [ -f "$ROOT_DIR/ubuntu-backend-deploy/baorong-custom-nodes-install.service.example" ]; then
  cp "$ROOT_DIR/ubuntu-backend-deploy/baorong-custom-nodes-install.service.example" \
    /etc/systemd/system/baorong-custom-nodes-install.service
fi

if [ -f "$ROOT_DIR/ubuntu-backend-deploy/baorong-model-verify.service.example" ]; then
  install_service \
    "$ROOT_DIR/ubuntu-backend-deploy/baorong-model-verify.service.example" \
    /etc/systemd/system/baorong-model-verify.service \
    "$APP_ROOT" \
    "/usr/local/sbin/brmmedia-verify-comfy-models"
fi

systemctl daemon-reload
systemctl enable baorong-backend
systemctl enable --now brmmedia-lan-api
systemctl enable --now brmmedia-healthcheck.timer
if [ -f /etc/systemd/system/brmmedia-h3-stage.service ]; then
  systemctl enable brmmedia-h3-stage.service
fi
systemd-analyze verify \
  /etc/systemd/system/baorong-backend.service \
  /etc/systemd/system/brmmedia-lan-api.service \
  /etc/systemd/system/qwen-vllm.service \
  /etc/systemd/system/brmmedia-healthcheck.service \
  /etc/systemd/system/brmmedia-healthcheck.timer
logrotate --debug /etc/logrotate.d/brmmedia >/dev/null

cat <<EOF

[OK] Services installed and enabled.

Start the normal image/music backend:
  sudo systemctl start baorong-backend

LAN automation API:
  sudo systemctl status brmmedia-lan-api

Start Qwen only after its model has been imported:
  sudo systemctl enable --now qwen-vllm

Import local models / documented custom nodes when their source is ready:
  sudo systemctl reset-failed baorong-model-import baorong-model-verify
  sudo systemctl restart baorong-model-import
  sudo systemctl start baorong-custom-nodes-install

Production GPU layout:
  baorong-backend / all ComfyUI media flows: GPU0 RTX A5000
  qwen-vllm: GPU1 RTX A4000
  Both services are intentionally enabled and online together.

Logs:
  sudo journalctl -u baorong-backend -f
  sudo journalctl -u qwen-vllm -f
  sudo systemctl list-timers brmmedia-healthcheck.timer
  sudo systemctl start brmmedia-h3-stage   # optional resumable H3 staging
  sudo brmmedia-verify-runtime
EOF
