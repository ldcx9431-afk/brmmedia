#!/usr/bin/env bash
# Stage the exact local MiniMax H3 Base components on the Windows D: model
# source.  The runtime import script copies them into WSL's ext4 volume on E:.
set -euo pipefail

MODEL_SOURCE_ROOT="${BRMMEDIA_MODEL_SOURCE_ROOT:-/mnt/d/model}"
MODEL_DIR="$MODEL_SOURCE_ROOT/MiniMax-H3"
REPO="${BRMMEDIA_H3_REPO:-Comfy-Org/MiniMax-H3}"
REVISION="${BRMMEDIA_H3_REVISION:-0bd506d2e895983a9663037febda27aa3948cf48}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ "${BRMMEDIA_H3_SKIP_PREFLIGHT:-0}" != "1" ]; then
  BRMMEDIA_H3_ALLOW_PAGEFILE_OVERRIDE="${BRMMEDIA_H3_ALLOW_PAGEFILE_OVERRIDE:-0}" \
    "$SCRIPT_DIR/check_h3_preflight.sh"
fi

if ! command -v hf >/dev/null 2>&1; then
  echo "[ERROR] Hugging Face CLI 'hf' is required. Install it in the WSL deployment environment first."
  echo "        python -m pip install -U 'huggingface_hub[cli]'"
  exit 1
fi

mkdir -p "$MODEL_DIR"
echo "[INFO] Staging $REPO@$REVISION into $MODEL_DIR"
hf download "$REPO" \
  --revision "$REVISION" \
  --local-dir "$MODEL_DIR" \
  --include 'diffusion_models/minimax_h3_fl2va_pruned_int8_convrot.safetensors' \
  --include 'text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors' \
  --include 'vae/minimax_h3_video_vae_fp16.safetensors' \
  --include 'vae/minimax_h3_audio_vae_fp32.safetensors'

echo "[OK] H3 source weights staged. Review the MiniMax H3 model license before production use."
