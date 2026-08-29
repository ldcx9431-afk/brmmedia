#!/usr/bin/env bash
# Start the isolated llama.cpp Qwen3.8 service.  It is intentionally local-only;
# Nginx remains the sole authenticated LAN entry.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
if [ -f .env.qwen38-27b ]; then
  set -a
  # shellcheck disable=SC1091
  source .env.qwen38-27b
  set +a
fi

BIN="${QWEN38_BIN:-/srv/brmmedia/qwen38-llama/bin/llama-server}"
MODEL="${QWEN38_MODEL:-/srv/brmmedia/models/Qwen3.8-27B-GGUF/Qwen3.8-27B-UD-Q4_K_XL.gguf}"
HOST="${QWEN38_HOST:-127.0.0.1}"

[ -x "$BIN" ] || { echo "[ERROR] llama-server is missing or not executable: $BIN" >&2; exit 2; }
[ -r "$MODEL" ] || { echo "[ERROR] Qwen3.8 GGUF is missing: $MODEL" >&2; exit 2; }
[ "$HOST" = "127.0.0.1" ] || { echo "[ERROR] Qwen3.8 must remain loopback-only." >&2; exit 2; }

export CUDA_VISIBLE_DEVICES="${QWEN38_CUDA_VISIBLE_DEVICES:-1,2}"
export CUDA_DEVICE_ORDER="${CUDA_DEVICE_ORDER:-PCI_BUS_ID}"
# Keep llama.cpp's default CUDA memory policy.  The explicit "0" setting
# causes the dual-GPU Qwen3.8 service to stall during weight placement on the
# current WSL/NVIDIA driver stack, while the validated default loads normally.
unset GGML_CUDA_ENABLE_UNIFIED_MEMORY

exec "$BIN" \
  --model "$MODEL" \
  --alias "${QWEN38_SERVED_MODEL_NAME:-qwen38-27b-ud-q4-xl}" \
  --host "$HOST" \
  --port "${QWEN38_PORT:-8001}" \
  --ctx-size "${QWEN38_CONTEXT_SIZE:-4096}" \
  --parallel "${QWEN38_PARALLEL:-1}" \
  --gpu-layers "${QWEN38_GPU_LAYERS:-all}" \
  --split-mode "${QWEN38_SPLIT_MODE:-layer}" \
  --tensor-split "${QWEN38_TENSOR_SPLIT:-1,1}" \
  --flash-attn "${QWEN38_FLASH_ATTN:-auto}" \
  --log-colors off \
  --metrics
