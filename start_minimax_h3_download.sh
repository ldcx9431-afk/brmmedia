#!/usr/bin/env bash
# Start H3 component downloads as transient systemd units.  Unlike an SSH
# background job, these continue through an SSH disconnect and are resumable.
set -euo pipefail

MODEL_SOURCE_ROOT="${BRMMEDIA_MODEL_SOURCE_ROOT:-/mnt/d/model}"
MODEL_DIR="$MODEL_SOURCE_ROOT/MiniMax-H3"
REPO="${BRMMEDIA_H3_REPO:-Comfy-Org/MiniMax-H3}"
REVISION="${BRMMEDIA_H3_REVISION:-0bd506d2e895983a9663037febda27aa3948cf48}"
SERVICE_USER="${BRMMEDIA_SERVICE_USER:-brm}"

declare -A components=(
  [diffusion]='diffusion_models/minimax_h3_fl2va_pruned_int8_convrot.safetensors'
  [text]='text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors'
  [video]='vae/minimax_h3_video_vae_fp16.safetensors'
  [audio]='vae/minimax_h3_audio_vae_fp32.safetensors'
)

show_status() {
  local name relative file expected actual
  for name in diffusion text video audio; do
    relative="${components[$name]}"
    file="$MODEL_DIR/$relative"
    expected="$(case "$name" in diffusion) echo 20970379616;; text) echo 15687142551;; video) echo 5207808496;; audio) echo 605254808;; esac)"
    actual="$(stat -c%s "$file" 2>/dev/null || echo 0)"
    printf '%-10s %-12s %s/%s bytes  %s\n' "$name" \
      "$(systemctl is-active "brmmedia-h3-download-$name" 2>/dev/null || true)" "$actual" "$expected" "$relative"
  done
}

if [ "${1:-}" = "--status" ]; then
  show_status
  exit 0
fi
if [ "$(id -u)" -ne 0 ]; then
  echo "[ERROR] Run with sudo: sudo $0 [--status]" >&2
  exit 1
fi
if ! id "$SERVICE_USER" >/dev/null 2>&1; then
  echo "[ERROR] Service user does not exist: $SERVICE_USER" >&2
  exit 1
fi
command -v curl >/dev/null 2>&1 || { echo "[ERROR] curl is required" >&2; exit 1; }
install -d -o "$SERVICE_USER" -g "$SERVICE_USER" "$MODEL_DIR"

for name in diffusion text video audio; do
  relative="${components[$name]}"
  file="$MODEL_DIR/$relative"
  install -d -o "$SERVICE_USER" -g "$SERVICE_USER" "$(dirname "$file")"
  if systemctl is-active --quiet "brmmedia-h3-download-$name"; then
    echo "[INFO] $name download is already active."
    continue
  fi
  systemd-run --unit="brmmedia-h3-download-$name" --collect \
    --property="User=$SERVICE_USER" --property=Nice=10 --property="WorkingDirectory=$MODEL_DIR" \
    /usr/bin/curl --silent --show-error --fail --location --continue-at - \
    --retry 12 --retry-delay 5 --retry-all-errors --connect-timeout 30 \
    --speed-time 90 --speed-limit 1024 --output "$file" \
    "https://huggingface.co/$REPO/resolve/$REVISION/$relative"
done
show_status
