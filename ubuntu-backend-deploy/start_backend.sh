#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

ENV_FILE="${BRMMEDIA_ENV_FILE:-$SCRIPT_DIR/.env}"
if [ -f "$ENV_FILE" ]; then
  set -a
  # shellcheck disable=SC1091
  source "$ENV_FILE"
  set +a
fi

BACKEND_VENV="${BRMMEDIA_BACKEND_VENV:-$SCRIPT_DIR/.venv}"
BACKEND_PYTHON="$BACKEND_VENV/bin/python"
if [ ! -x "$BACKEND_PYTHON" ]; then
  echo "[ERROR] Backend Python is unavailable: $BACKEND_PYTHON" >&2
  echo "        Run ./install_ubuntu.sh first, or set BRMMEDIA_BACKEND_VENV to a valid isolated venv." >&2
  exit 1
fi

mkdir -p outputs logs
# `activate` keeps VIRTUAL_ENV correct for custom nodes and Python packages
# that inspect it.  Production retains the checkout-local .venv default;
# the H3 canary injects its own physical venv through its private env file.
# shellcheck disable=SC1090
source "$BACKEND_VENV/bin/activate"

CPU_THREADS="$(nproc 2>/dev/null || echo 8)"
export PYTHONUNBUFFERED="${PYTHONUNBUFFERED:-1}"
export PYTORCH_CUDA_ALLOC_CONF="${PYTORCH_CUDA_ALLOC_CONF:-backend:cudaMallocAsync,expandable_segments:True}"
export CUDA_MODULE_LOADING="${CUDA_MODULE_LOADING:-LAZY}"
export NVIDIA_TF32_OVERRIDE="${NVIDIA_TF32_OVERRIDE:-1}"
export TORCH_ALLOW_TF32_CUBLAS_OVERRIDE="${TORCH_ALLOW_TF32_CUBLAS_OVERRIDE:-1}"
export OMP_NUM_THREADS="${OMP_NUM_THREADS:-$CPU_THREADS}"
export MKL_NUM_THREADS="${MKL_NUM_THREADS:-$CPU_THREADS}"
export HF_HOME="${HF_HOME:-$SCRIPT_DIR/.cache/huggingface}"
export TORCH_HOME="${TORCH_HOME:-$SCRIPT_DIR/.cache/torch}"
export CUDA_VISIBLE_DEVICES="${COMFYUI_CUDA_VISIBLE_DEVICES:-0}"
mkdir -p "$HF_HOME" "$TORCH_HOME"

case " ${COMFYUI_ARGS:-} " in
  *" --highvram "*|*" --gpu-only "*|*" --disable-smart-memory "*|*" --cache-none "*)
    echo "[ERROR] --highvram/--gpu-only/--disable-smart-memory/--cache-none are incompatible with the MiniMax H3 dynamic-offload profile."
    exit 1
    ;;
esac

echo "[INFO] Starting Bao Rong Wan Xiang backend..."
echo "[INFO] COMFYUI_ROOT=${COMFYUI_ROOT:-$SCRIPT_DIR/ComfyUI}"
echo "[INFO] BACKEND_VENV=$BACKEND_VENV"
echo "[INFO] media GPU=A5000 (CUDA_VISIBLE_DEVICES=$CUDA_VISIBLE_DEVICES)"
echo "[INFO] BRM_PERF_PROFILE=${BRM_PERF_PROFILE:-balanced}; OMP_NUM_THREADS=$OMP_NUM_THREADS; COMFYUI_ARGS=${COMFYUI_ARGS:-}"
"$BACKEND_PYTHON" -u entry_yzy.py 2>&1 | tee -a logs/backend.log
