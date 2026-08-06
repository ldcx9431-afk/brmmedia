#!/usr/bin/env bash
set -euo pipefail

SOURCE_ROOT="${BRMMEDIA_MODEL_SOURCE_ROOT:-/mnt/d/model}"
TARGET_ROOT="${BRMMEDIA_COMFYUI_MODEL_ROOT:-/srv/brmmedia/ComfyUI/models}"

copy_file() {
  local source_rel="$1"
  local target_rel="$2"
  local source="$SOURCE_ROOT/$source_rel"
  local target="$TARGET_ROOT/$target_rel"
  if [ ! -f "$source" ]; then
    echo "[ERROR] Required model is missing: $source"
    exit 1
  fi
  install -d "$(dirname "$target")"
  rsync -a --partial --append-verify --info=progress2 "$source" "$target"
}

copy_tree() {
  local source_rel="$1"
  local target_rel="$2"
  local source="$SOURCE_ROOT/$source_rel/"
  local target="$TARGET_ROOT/$target_rel/"
  if [ ! -d "$source" ]; then
    echo "[ERROR] Required model directory is missing: $source"
    exit 1
  fi
  install -d "$target"
  rsync -a --partial --append-verify --info=progress2 "$source" "$target"
}

copy_file z_image_turbo_bf16.safetensors diffusion_models/z_image_turbo_bf16.safetensors
copy_file flux-2-klein-base-4b-fp8.safetensors diffusion_models/flux-2-klein-base-4b-fp8.safetensors
copy_file acestep_v1.5_xl_turbo_bf16.safetensors diffusion_models/acestep/acestep_v1.5_xl_turbo_bf16.safetensors
copy_file LTX-2.3-22B-distilled-1.1-Q4_K_M.gguf diffusion_models/LTX2.3/LTX-2.3-22B-distilled-1.1-Q4_K_M.gguf
copy_file MelBandRoformer_fp32.safetensors diffusion_models/MelBandRoformer_fp32.safetensors

copy_file gemma-3-12b-it-heretic-Q4_K_M.gguf text_encoders/gemma-3-12b-it-heretic-Q4_K_M.gguf
copy_file ltx-2.3_text_projection_bf16.safetensors text_encoders/ltx-2.3_text_projection_bf16.safetensors
copy_file qwen_0.6b_ace15.safetensors text_encoders/qwen_0.6b_ace15.safetensors
copy_file qwen_3_4b.safetensors text_encoders/qwen_3_4b.safetensors
copy_file qwen_4b_ace15.safetensors text_encoders/qwen_4b_ace15.safetensors

copy_file ace_1.5_vae.safetensors vae/ace_1.5_vae.safetensors
copy_file ae.safetensors vae/ae.safetensors
copy_file full_encoder_small_decoder.safetensors vae/full_encoder_small_decoder.safetensors
copy_file LTX23_audio_vae_bf16.safetensors vae/LTX23_audio_vae_bf16.safetensors
copy_file LTX23_video_vae_bf16.safetensors vae/LTX23_video_vae_bf16.safetensors

copy_tree LTX-2.3 loras/LTX-2.3
copy_tree IndexTTS-2 IndexTTS-2

# H3 is staged separately because the four files are about 42 GB.  Existing
# recovery imports remain usable before this optional model source arrives;
# once any H3 source component is present, require all four atomically.
H3_SOURCE="$SOURCE_ROOT/MiniMax-H3"
h3_source_ready() {
  local relative expected size
  while IFS=':' read -r relative expected; do
    [ -n "$relative" ] || continue
    size="$(stat -c%s "$H3_SOURCE/$relative" 2>/dev/null || true)"
    [ "$size" = "$expected" ] || return 1
  done <<'EOF'
diffusion_models/minimax_h3_fl2va_pruned_int8_convrot.safetensors:20970379616
text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors:15687142551
vae/minimax_h3_video_vae_fp16.safetensors:5207808496
vae/minimax_h3_audio_vae_fp32.safetensors:605254808
EOF
}

if h3_source_ready; then
  copy_file MiniMax-H3/diffusion_models/minimax_h3_fl2va_pruned_int8_convrot.safetensors diffusion_models/minimax_h3_fl2va_pruned_int8_convrot.safetensors
  copy_file MiniMax-H3/text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors
  copy_file MiniMax-H3/vae/minimax_h3_video_vae_fp16.safetensors vae/minimax_h3_video_vae_fp16.safetensors
  copy_file MiniMax-H3/vae/minimax_h3_audio_vae_fp32.safetensors vae/minimax_h3_audio_vae_fp32.safetensors
else
  if [ "${BRMMEDIA_REQUIRE_H3_MODELS:-0}" = "1" ]; then
    echo "[ERROR] MiniMax H3 source is missing or incomplete; all four exact-size files are required." >&2
    exit 1
  fi
  echo "[INFO] MiniMax H3 source is missing or incomplete; skipping optional H3 import."
fi

echo "[OK] ComfyUI model import complete."
