#!/usr/bin/env bash
set -euo pipefail

SOURCE_ROOT=/mnt/d/model
TARGET_ROOT=/srv/brmmedia/ComfyUI/models
failed=0

verify_file() {
  local source_rel="$1"
  local target_rel="$2"
  local source="$SOURCE_ROOT/$source_rel"
  local target="$TARGET_ROOT/$target_rel"
  if [ ! -f "$source" ] || [ ! -f "$target" ]; then
    echo "[FAIL] Missing: $source_rel -> $target_rel"
    failed=1
    return
  fi
  if cmp -s "$source" "$target"; then
    echo "[OK] $target_rel"
  else
    echo "[FAIL] Content mismatch: $target_rel"
    failed=1
  fi
}

verify_tree() {
  local source_rel="$1"
  local target_rel="$2"
  local result
  result="$(rsync -rcn --out-format='%n' "$SOURCE_ROOT/$source_rel/" "$TARGET_ROOT/$target_rel/" || true)"
  if [ -n "$result" ]; then
    echo "[FAIL] Tree mismatch: $target_rel"
    printf '%s\n' "$result"
    failed=1
  else
    echo "[OK] $target_rel/"
  fi
}

verify_file z_image_turbo_bf16.safetensors diffusion_models/z_image_turbo_bf16.safetensors
verify_file flux-2-klein-base-4b-fp8.safetensors diffusion_models/flux-2-klein-base-4b-fp8.safetensors
verify_file acestep_v1.5_xl_turbo_bf16.safetensors diffusion_models/acestep/acestep_v1.5_xl_turbo_bf16.safetensors
verify_file LTX-2.3-22B-distilled-1.1-Q4_K_M.gguf diffusion_models/LTX2.3/LTX-2.3-22B-distilled-1.1-Q4_K_M.gguf
verify_file MelBandRoformer_fp32.safetensors diffusion_models/MelBandRoformer_fp32.safetensors
verify_file gemma-3-12b-it-heretic-Q4_K_M.gguf text_encoders/gemma-3-12b-it-heretic-Q4_K_M.gguf
verify_file ltx-2.3_text_projection_bf16.safetensors text_encoders/ltx-2.3_text_projection_bf16.safetensors
verify_file qwen_0.6b_ace15.safetensors text_encoders/qwen_0.6b_ace15.safetensors
verify_file qwen_3_4b.safetensors text_encoders/qwen_3_4b.safetensors
verify_file qwen_4b_ace15.safetensors text_encoders/qwen_4b_ace15.safetensors
verify_file ace_1.5_vae.safetensors vae/ace_1.5_vae.safetensors
verify_file ae.safetensors vae/ae.safetensors
verify_file full_encoder_small_decoder.safetensors vae/full_encoder_small_decoder.safetensors
verify_file LTX23_audio_vae_bf16.safetensors vae/LTX23_audio_vae_bf16.safetensors
verify_file LTX23_video_vae_bf16.safetensors vae/LTX23_video_vae_bf16.safetensors
verify_tree LTX-2.3 loras/LTX-2.3
verify_tree IndexTTS-2 IndexTTS-2

if [ "$failed" -ne 0 ]; then
  exit 1
fi
echo '[OK] All imported ComfyUI models match the D: source.'
