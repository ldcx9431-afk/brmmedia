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

systemctl daemon-reload
systemctl enable baorong-backend qwen-vllm

cat <<EOF

[OK] Services installed and enabled.

Start:
  sudo systemctl start baorong-backend
  sudo systemctl start qwen-vllm

Logs:
  sudo journalctl -u baorong-backend -f
  sudo journalctl -u qwen-vllm -f
EOF
