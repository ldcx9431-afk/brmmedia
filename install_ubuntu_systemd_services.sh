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

echo "[INFO] Installing services for user: $SERVICE_USER"
echo "[INFO] Source root: $ROOT_DIR"
echo "[INFO] Runtime app root: $APP_ROOT"

if [ ! -x "$APP_ROOT/ubuntu-backend-deploy/start_backend.sh" ] || [ ! -x "$APP_ROOT/llm-backend-deploy/start_qwen_vllm.sh" ]; then
  echo "[ERROR] Runtime app root is incomplete: $APP_ROOT"
  exit 1
fi

install_service() {
  local src="$1"
  local dst="$2"
  local workdir="$3"
  local execstart="$4"

  # Unit 文件必须是普通配置文件；若目标曾被误设为可执行，cp 会保留
  # 目标权限并导致 systemd 发出警告。
  install -m 0644 "$src" "$dst"
  sed -i "s#YOUR_USER#$SERVICE_USER#g" "$dst"
  sed -i "s#WorkingDirectory=.*#WorkingDirectory=$workdir#g" "$dst"
  sed -i "s#ExecStart=.*#ExecStart=$execstart#g" "$dst"
}

install_service \
  "$ROOT_DIR/ubuntu-backend-deploy/baorong-backend.service.example" \
  /etc/systemd/system/baorong-backend.service \
  "$APP_ROOT/ubuntu-backend-deploy" \
  "$APP_ROOT/ubuntu-backend-deploy/start_backend.sh"

install -m 0755 "$ROOT_DIR/brmmedia-healthcheck.sh" /usr/local/sbin/brmmedia-healthcheck
install -m 0755 "$ROOT_DIR/brmmedia-verify-runtime.sh" /usr/local/sbin/brmmedia-verify-runtime
install -m 0644 "$ROOT_DIR/ubuntu-backend-deploy/brmmedia-healthcheck.service.example" \
  /etc/systemd/system/brmmedia-healthcheck.service
install -m 0644 "$ROOT_DIR/ubuntu-backend-deploy/brmmedia-healthcheck.timer.example" \
  /etc/systemd/system/brmmedia-healthcheck.timer
install -m 0644 "$ROOT_DIR/ubuntu-backend-deploy/brmmedia-logrotate.conf.example" \
  /etc/logrotate.d/brmmedia

install_service \
  "$ROOT_DIR/llm-backend-deploy/qwen-vllm.service.example" \
  /etc/systemd/system/qwen-vllm.service \
  "$APP_ROOT/llm-backend-deploy" \
  "$APP_ROOT/llm-backend-deploy/start_qwen_vllm.sh"

install_service \
  "$ROOT_DIR/ubuntu-backend-deploy/baorong-backend-highvram.service.example" \
  /etc/systemd/system/baorong-backend-highvram.service \
  "$APP_ROOT/ubuntu-backend-deploy" \
  "$APP_ROOT/ubuntu-backend-deploy/start_backend.sh"

if [ -f "$ROOT_DIR/ubuntu-backend-deploy/baorong-model-import.service.example" ]; then
  cp "$ROOT_DIR/ubuntu-backend-deploy/baorong-model-import.service.example" \
    /etc/systemd/system/baorong-model-import.service
fi

if [ -f "$ROOT_DIR/ubuntu-backend-deploy/baorong-custom-nodes-install.service.example" ]; then
  cp "$ROOT_DIR/ubuntu-backend-deploy/baorong-custom-nodes-install.service.example" \
    /etc/systemd/system/baorong-custom-nodes-install.service
fi

if [ -f "$ROOT_DIR/ubuntu-backend-deploy/baorong-model-verify.service.example" ]; then
  cp "$ROOT_DIR/ubuntu-backend-deploy/baorong-model-verify.service.example" \
    /etc/systemd/system/baorong-model-verify.service
fi

systemctl daemon-reload
systemctl enable baorong-backend
systemctl enable --now brmmedia-healthcheck.timer
systemd-analyze verify \
  /etc/systemd/system/baorong-backend.service \
  /etc/systemd/system/baorong-backend-highvram.service \
  /etc/systemd/system/qwen-vllm.service \
  /etc/systemd/system/brmmedia-healthcheck.service \
  /etc/systemd/system/brmmedia-healthcheck.timer
logrotate --debug /etc/logrotate.d/brmmedia >/dev/null

cat <<EOF

[OK] Services installed and enabled.

Start the normal image/music backend:
  sudo systemctl start baorong-backend

Start Qwen only after its model has been imported:
  sudo systemctl enable --now qwen-vllm

Import local models / documented custom nodes when their source is ready:
  sudo systemctl enable --now baorong-model-import
  sudo systemctl start baorong-custom-nodes-install

High-VRAM video mode (stops the normal backend and Qwen):
  sudo systemctl start baorong-backend-highvram

Return to normal mode:
  sudo systemctl stop baorong-backend-highvram
  sudo systemctl start baorong-backend qwen-vllm

Logs:
  sudo journalctl -u baorong-backend -f
  sudo journalctl -u qwen-vllm -f
  sudo systemctl list-timers brmmedia-healthcheck.timer
  sudo brmmedia-verify-runtime
EOF
