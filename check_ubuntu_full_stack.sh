#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "== Checking ComfyUI/Gradio backend =="
(
  cd "$ROOT_DIR/ubuntu-backend-deploy"
  chmod +x check_ubuntu_ready.sh
  ./check_ubuntu_ready.sh
)

echo
echo "== Checking Qwen vLLM backend =="
if curl -s http://127.0.0.1:8000/v1/models >/dev/null 2>&1; then
  (
    cd "$ROOT_DIR/llm-backend-deploy"
    chmod +x check_qwen_vllm.sh
    ./check_qwen_vllm.sh
  )
else
  echo "[WARN] Qwen vLLM is not running on 127.0.0.1:8000 yet."
  echo "      Start it with: cd llm-backend-deploy && ./start_qwen_vllm.sh"
fi

echo
echo "[DONE] Full-stack check completed."
