#!/usr/bin/env bash
# Wait for the four immutable H3 source files, then import them into the E:
# ext4 ComfyUI model store.  This deliberately does *not* switch the video
# engine or restart any service; activation remains a separate validated step.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
MODEL_SOURCE_ROOT="${BRMMEDIA_MODEL_SOURCE_ROOT:-/mnt/d/model}"
MODEL_ROOT="${BRMMEDIA_COMFYUI_MODEL_ROOT:-/srv/brmmedia/ComfyUI/models}"
CHECK_INTERVAL_SECONDS="${BRMMEDIA_H3_IMPORT_CHECK_INTERVAL_SECONDS:-60}"

declare -A expected_sizes=(
  ["diffusion_models/minimax_h3_fl2va_pruned_int8_convrot.safetensors"]=20970379616
  ["text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors"]=15687142551
  ["vae/minimax_h3_video_vae_fp16.safetensors"]=5207808496
  ["vae/minimax_h3_audio_vae_fp32.safetensors"]=605254808
)
declare -A expected_sha256=(
  ["diffusion_models/minimax_h3_fl2va_pruned_int8_convrot.safetensors"]=e889202c41dafb67b10d67b97f0d8541508036a6090af23425a5c2615d03c47a
  ["text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors"]=35a88d51044231fe332301d7a62aa81e3f2cba62febeb446e2c1e3e0ef76f2c6
  ["vae/minimax_h3_video_vae_fp16.safetensors"]=7c1f131492e7eddacaac9069a61b81bdd39de5cc96561e677c5eab1cdce5e522
  ["vae/minimax_h3_audio_vae_fp32.safetensors"]=8e505d95dd1561d47abd43d4238fd40d9bb1ae9e147ed0a4cba778d76ae4db48
)

sources_ready() {
  local relative expected actual
  for relative in "${!expected_sizes[@]}"; do
    expected="${expected_sizes[$relative]}"
    actual="$(stat -c%s "$MODEL_SOURCE_ROOT/MiniMax-H3/$relative" 2>/dev/null || echo 0)"
    if [ "$actual" != "$expected" ]; then
      printf '[WAIT] %s: %s/%s bytes\n' "$relative" "$actual" "$expected"
      return 1
    fi
  done
  return 0
}

verify_source_hashes() {
  local relative expected actual
  for relative in "${!expected_sha256[@]}"; do
    expected="${expected_sha256[$relative]}"
    actual="$(sha256sum "$MODEL_SOURCE_ROOT/MiniMax-H3/$relative" | awk '{print $1}')"
    if [ "$actual" != "$expected" ]; then
      echo "[ERROR] H3 source SHA-256 mismatch: $relative" >&2
      return 1
    fi
    echo "[OK] H3 source SHA-256: $relative"
  done
}

if [ "$(id -u)" -eq 0 ]; then
  echo '[ERROR] Run this worker as the BRMMedia service user, not root.' >&2
  exit 1
fi
if [ ! -x "$SCRIPT_DIR/import_comfy_models.sh" ] || [ ! -x "$SCRIPT_DIR/verify_comfy_models.sh" ]; then
  echo '[ERROR] Import and verify scripts must exist beside this worker.' >&2
  exit 1
fi
if ! [[ "$CHECK_INTERVAL_SECONDS" =~ ^[0-9]+$ ]] || [ "$CHECK_INTERVAL_SECONDS" -lt 10 ]; then
  echo '[ERROR] BRMMEDIA_H3_IMPORT_CHECK_INTERVAL_SECONDS must be an integer >= 10.' >&2
  exit 1
fi

until sources_ready; do
  sleep "$CHECK_INTERVAL_SECONDS"
done

echo '[INFO] All H3 source weights reached their exact expected sizes; validating immutable LFS SHA-256 values.'
verify_source_hashes
echo '[OK] H3 source weights passed size and SHA-256 checks; importing into E: runtime store.'
BRMMEDIA_MODEL_SOURCE_ROOT="$MODEL_SOURCE_ROOT" \
BRMMEDIA_COMFYUI_MODEL_ROOT="$MODEL_ROOT" \
BRMMEDIA_REQUIRE_H3_MODELS=1 \
  "$SCRIPT_DIR/import_comfy_models.sh"
BRMMEDIA_MODEL_SOURCE_ROOT="$MODEL_SOURCE_ROOT" \
BRMMEDIA_COMFYUI_MODEL_ROOT="$MODEL_ROOT" \
BRMMEDIA_REQUIRE_H3_MODELS=1 \
  "$SCRIPT_DIR/verify_comfy_models.sh"
echo '[OK] H3 models imported and content-verified. Services remain unchanged; run controlled activation next.'
