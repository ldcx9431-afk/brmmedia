#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

if [ ! -x .venv/bin/python ]; then
  echo "[ERROR] .venv not found. Run ./install_qwen_vllm.sh first."
  exit 1
fi

source .venv/bin/activate

export CUDA_VISIBLE_DEVICES="${QWEN_CUDA_VISIBLE_DEVICES:-1}"
export HF_HOME="${HF_HOME:-$SCRIPT_DIR/.cache/huggingface}"
export VLLM_LOGGING_LEVEL="${VLLM_LOGGING_LEVEL:-INFO}"
mkdir -p "$HF_HOME" logs

MODEL_ID="${QWEN_MODEL:-cyankiwi/Qwen3.6-35B-A3B-AWQ-4bit}"
LOCAL_MODEL_DIR="${QWEN_LOCAL_MODEL_DIR:-./models/Qwen3.6-35B-A3B-AWQ-4bit}"
case "$LOCAL_MODEL_DIR" in
  /*) ;;
  *) LOCAL_MODEL_DIR="$SCRIPT_DIR/$LOCAL_MODEL_DIR" ;;
esac

if [ "${QWEN_PREFER_LOCAL_MODEL:-true}" = "true" ] && [ -f "$LOCAL_MODEL_DIR/config.json" ]; then
  MODEL_ID="$LOCAL_MODEL_DIR"
fi

ARGS=(
  serve "$MODEL_ID"
  --host "${QWEN_HOST:-0.0.0.0}"
  --port "${QWEN_PORT:-8000}"
  --served-model-name "${QWEN_SERVED_MODEL_NAME:-qwen}"
  --tensor-parallel-size "${QWEN_TENSOR_PARALLEL_SIZE:-1}"
  --gpu-memory-utilization "${QWEN_GPU_MEMORY_UTILIZATION:-0.92}"
  --max-model-len "${QWEN_MAX_MODEL_LEN:-32768}"
  --max-num-seqs "${QWEN_MAX_NUM_SEQS:-4}"
  --max-num-batched-tokens "${QWEN_MAX_NUM_BATCHED_TOKENS:-8192}"
)

if [ -n "${QWEN_REASONING_PARSER:-qwen3}" ]; then
  ARGS+=(--reasoning-parser "${QWEN_REASONING_PARSER:-qwen3}")
fi

if [ "${QWEN_ENABLE_MTP:-false}" = "true" ]; then
  ARGS+=(--speculative-config "{\"method\":\"qwen3_next_mtp\",\"num_speculative_tokens\":${QWEN_SPECULATIVE_TOKENS:-2}}")
fi

if [ -n "${QWEN_EXTRA_ARGS:-}" ]; then
  # shellcheck disable=SC2206
  EXTRA=(${QWEN_EXTRA_ARGS})
  ARGS+=("${EXTRA[@]}")
fi

echo "[INFO] Starting Qwen vLLM on CUDA_VISIBLE_DEVICES=$CUDA_VISIBLE_DEVICES"
echo "[INFO] Model=$MODEL_ID"
echo "[INFO] Port=${QWEN_PORT:-8000}"

vllm "${ARGS[@]}" 2>&1 | tee -a logs/qwen-vllm.log
