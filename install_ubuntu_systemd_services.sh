#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_USER="${1:-$USER}"

if [ "$(id -u)" -ne 0 ]; then
  echo "[ERROR] Run with sudo:"
  echo "  sudo $0 $SERVICE_USER"
  exit 1
fi

echo "[INFO] Installing services for user: $SERVICE_USER"
echo "[INFO] Repo root: $ROOT_DIR"

install_service() {
  local src="$1"
  local dst="$2"
  local workdir="$3"
  local execstart="$4"

  cp "$src" "$dst"
  sed -i "s#YOUR_USER#$SERVICE_USER#g" "$dst"
  sed -i "s#WorkingDirectory=.*#WorkingDirectory=$workdir#g" "$dst"
  sed -i "s#ExecStart=.*#ExecStart=$execstart#g" "$dst"
}

install_service \
  "$ROOT_DIR/ubuntu-backend-deploy/baorong-backend.service.example" \
  /etc/systemd/system/baorong-backend.service \
  "$ROOT_DIR/ubuntu-backend-deploy" \
  "$ROOT_DIR/ubuntu-backend-deploy/start_backend.sh"

install_service \
  "$ROOT_DIR/llm-backend-deploy/qwen-vllm.service.example" \
  /etc/systemd/system/qwen-vllm.service \
  "$ROOT_DIR/llm-backend-deploy" \
  "$ROOT_DIR/llm-backend-deploy/start_qwen_vllm.sh"

install_service \
  "$ROOT_DIR/ubuntu-backend-deploy/baorong-backend-highvram.service.example" \
  /etc/systemd/system/baorong-backend-highvram.service \
  "$ROOT_DIR/ubuntu-backend-deploy" \
  "$ROOT_DIR/ubuntu-backend-deploy/start_backend.sh"

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
EOF
