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
MODEL="${QWEN38_MODEL:-/srv/brmmedia/models/Qwen3.8-27B-GGUF/Qwen3.8-27B-UD-Q4_K_M.gguf}"
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

vision_args=()
if [ -n "${QWEN38_MMPROJ:-}" ]; then
  [ -r "$QWEN38_MMPROJ" ] || { echo "[ERROR] Qwen visual projector is missing or unreadable." >&2; exit 2; }
  if [ -n "${QWEN38_MMPROJ_SHA256:-}" ]; then
    printf '%s  %s\n' "$QWEN38_MMPROJ_SHA256" "$QWEN38_MMPROJ" | sha256sum --check --status || {
      echo "[ERROR] Qwen visual projector checksum mismatch." >&2; exit 2;
    }
  fi
  image_tokens="${QWEN38_IMAGE_MAX_TOKENS:-1024}"
  case "$image_tokens" in ''|*[!0-9]*) echo "[ERROR] Image token budget must be a positive integer." >&2; exit 2;; esac
  [ "$image_tokens" -gt 0 ] || { echo "[ERROR] Image token budget must be positive." >&2; exit 2; }
  vision_args=(--mmproj "$QWEN38_MMPROJ" --image-max-tokens "$image_tokens")
  case "${QWEN38_MMPROJ_OFFLOAD:-0}" in
    0) vision_args+=(--no-mmproj-offload);;
    1) vision_args+=(--mmproj-offload);;
    *) echo "[ERROR] QWEN38_MMPROJ_OFFLOAD must be 0 or 1." >&2; exit 2;;
  esac
fi

exec "$BIN" \
  --model "$MODEL" \
  --alias "${QWEN38_SERVED_MODEL_NAME:-qwen38-27b-ud-q4-k-m}" \
  --host "$HOST" \
  --port "${QWEN38_PORT:-8001}" \
  --ctx-size "${QWEN38_CONTEXT_SIZE:-140288}" \
  --parallel "${QWEN38_PARALLEL:-1}" \
  --gpu-layers "${QWEN38_GPU_LAYERS:-all}" \
  --split-mode "${QWEN38_SPLIT_MODE:-layer}" \
  --tensor-split "${QWEN38_TENSOR_SPLIT:-1,1}" \
  --flash-attn "${QWEN38_FLASH_ATTN:-auto}" \
  --cache-type-k "${QWEN38_CACHE_TYPE_K:-q8_0}" \
  --cache-type-v "${QWEN38_CACHE_TYPE_V:-q8_0}" \
  --log-colors off \
  --metrics \
  "${vision_args[@]}"
