#!/usr/bin/env bash
# Stage the exact local MiniMax H3 Base components on the Windows D: model
# source.  The runtime import script copies them into WSL's ext4 volume on E:.
set -euo pipefail

MODEL_SOURCE_ROOT="${BRMMEDIA_MODEL_SOURCE_ROOT:-/mnt/d/model}"
MODEL_DIR="$MODEL_SOURCE_ROOT/MiniMax-H3"
APP_ROOT="${BRMMEDIA_APP_ROOT:-/srv/brmmedia/app}"
REPO="${BRMMEDIA_H3_REPO:-Comfy-Org/MiniMax-H3}"
REVISION="${BRMMEDIA_H3_REVISION:-0bd506d2e895983a9663037febda27aa3948cf48}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TRANSPORT="${BRMMEDIA_H3_TRANSPORT:-auto}"
H3_FILES=(
  'diffusion_models/minimax_h3_fl2va_pruned_int8_convrot.safetensors'
  'text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors'
  'vae/minimax_h3_video_vae_fp16.safetensors'
  'vae/minimax_h3_audio_vae_fp32.safetensors'
)

if [ "${BRMMEDIA_H3_SKIP_PREFLIGHT:-0}" != "1" ]; then
  BRMMEDIA_H3_ALLOW_PAGEFILE_OVERRIDE="${BRMMEDIA_H3_ALLOW_PAGEFILE_OVERRIDE:-0}" \
    "$SCRIPT_DIR/check_h3_preflight.sh"
fi

HF_BIN="${BRMMEDIA_HF_BIN:-}"
if [ -z "$HF_BIN" ] && command -v hf >/dev/null 2>&1; then
  HF_BIN="$(command -v hf)"
fi
if [ -z "$HF_BIN" ] && [ -x "$APP_ROOT/ubuntu-backend-deploy/.venv/bin/hf" ]; then
  HF_BIN="$APP_ROOT/ubuntu-backend-deploy/.venv/bin/hf"
fi
if [ -z "$HF_BIN" ] || [ ! -x "$HF_BIN" ]; then
  echo "[ERROR] Hugging Face CLI 'hf' is required. Install it in the WSL deployment environment first."
  echo "        $APP_ROOT/ubuntu-backend-deploy/.venv/bin/python -m pip install 'huggingface_hub<1'"
  exit 1
fi

mkdir -p "$MODEL_DIR"
download_with_hf() {
  echo "[INFO] Staging $REPO@$REVISION with Hugging Face CLI"
  # Some proxy appliances cannot carry hf-xet's HTTP stack.  Disabling Xet
  # makes a failure immediate so auto mode can fall back to curl safely.
  HF_HUB_DISABLE_XET=1 "$HF_BIN" download "$REPO" --revision "$REVISION" \
    --local-dir "$MODEL_DIR" --include "${H3_FILES[@]}"
}

download_with_curl() {
  command -v curl >/dev/null 2>&1 || { echo "[ERROR] curl is required for curl transport" >&2; return 1; }
  local base="https://huggingface.co/$REPO/resolve/$REVISION" relative target
  echo "[INFO] Staging $REPO@$REVISION with resumable curl downloads"
  for relative in "${H3_FILES[@]}"; do
    target="$MODEL_DIR/$relative"
    mkdir -p "$(dirname "$target")"
    curl --fail --location --continue-at - --retry 12 --retry-delay 5 --retry-all-errors \
      --connect-timeout 30 --speed-time 90 --speed-limit 1024 \
      --output "$target" "$base/$relative"
  done
}

case "$TRANSPORT" in
  hf) download_with_hf ;;
  curl) download_with_curl ;;
  auto) download_with_hf || download_with_curl ;;
  *) echo "[ERROR] BRMMEDIA_H3_TRANSPORT must be auto, hf, or curl" >&2; exit 1 ;;
esac

verify_component_sizes() {
  local relative expected size
  while IFS=':' read -r relative expected; do
    size="$(stat -c%s "$MODEL_DIR/$relative" 2>/dev/null || true)"
    if [ "$size" != "$expected" ]; then
      echo "[ERROR] H3 component size mismatch: $relative (expected $expected, got ${size:-missing})" >&2
      return 1
    fi
  done <<'EOF'
diffusion_models/minimax_h3_fl2va_pruned_int8_convrot.safetensors:20970379616
text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors:15687142551
vae/minimax_h3_video_vae_fp16.safetensors:5207808496
vae/minimax_h3_audio_vae_fp32.safetensors:605254808
EOF
}
verify_component_sizes

verify_component_hashes() {
  local relative expected actual
  while IFS=':' read -r relative expected; do
    actual="$(sha256sum "$MODEL_DIR/$relative" | awk '{print $1}')"
    if [ "$actual" != "$expected" ]; then
      echo "[ERROR] H3 component SHA-256 mismatch: $relative" >&2
      return 1
    fi
  done <<'EOF'
diffusion_models/minimax_h3_fl2va_pruned_int8_convrot.safetensors:e889202c41dafb67b10d67b97f0d8541508036a6090af23425a5c2615d03c47a
text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors:35a88d51044231fe332301d7a62aa81e3f2cba62febeb446e2c1e3e0ef76f2c6
vae/minimax_h3_video_vae_fp16.safetensors:7c1f131492e7eddacaac9069a61b81bdd39de5cc96561e677c5eab1cdce5e522
vae/minimax_h3_audio_vae_fp32.safetensors:8e505d95dd1561d47abd43d4238fd40d9bb1ae9e147ed0a4cba778d76ae4db48
EOF
}
verify_component_hashes

echo "[OK] H3 source weights staged. Review the MiniMax H3 model license before production use."
