#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "== Bao Rong Wan Xiang Ubuntu full-stack install =="
echo "Repo: $ROOT_DIR"
echo

echo "[1/2] Installing ComfyUI/Gradio backend..."
(
  cd "$ROOT_DIR/ubuntu-backend-deploy"
  chmod +x install_ubuntu.sh start_backend.sh check_ubuntu_ready.sh tune_nvidia_performance.sh
  ./install_ubuntu.sh
)

echo
echo "[2/2] Installing Qwen vLLM backend..."
(
  cd "$ROOT_DIR/llm-backend-deploy"
  chmod +x install_qwen_vllm.sh start_qwen_vllm.sh check_qwen_vllm.sh
  ./install_qwen_vllm.sh
)

echo
echo "[OK] Full stack is installed."
echo "Next:"
echo "  ./check_ubuntu_full_stack.sh"
echo "  ./install_ubuntu_systemd_services.sh"
