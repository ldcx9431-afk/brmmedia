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
  echo "[ERROR] .venv not found. Run ./install_ubuntu.sh first."
  exit 1
fi

mkdir -p outputs logs
source .venv/bin/activate

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
  *" --highvram "*|*" --gpu-only "*)
    echo "[ERROR] --highvram/--gpu-only are incompatible with the MiniMax H3 dynamic-offload profile."
    exit 1
    ;;
esac

echo "[INFO] Starting Bao Rong Wan Xiang backend..."
echo "[INFO] COMFYUI_ROOT=${COMFYUI_ROOT:-$SCRIPT_DIR/ComfyUI}"
echo "[INFO] media GPU=A5000 (CUDA_VISIBLE_DEVICES=$CUDA_VISIBLE_DEVICES)"
echo "[INFO] BRM_PERF_PROFILE=${BRM_PERF_PROFILE:-balanced}; OMP_NUM_THREADS=$OMP_NUM_THREADS; COMFYUI_ARGS=${COMFYUI_ARGS:-}"
python -u entry_yzy.py 2>&1 | tee -a logs/backend.log
