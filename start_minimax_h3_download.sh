#!/usr/bin/env bash
# Start H3 component downloads as transient systemd units.  Unlike an SSH
# background job, these continue through an SSH disconnect and are resumable.
set -euo pipefail

MODEL_SOURCE_ROOT="${BRMMEDIA_MODEL_SOURCE_ROOT:-/mnt/d/model}"
MODEL_DIR="$MODEL_SOURCE_ROOT/MiniMax-H3"
REPO="${BRMMEDIA_H3_REPO:-Comfy-Org/MiniMax-H3}"
REVISION="${BRMMEDIA_H3_REVISION:-0bd506d2e895983a9663037febda27aa3948cf48}"
SERVICE_USER="${BRMMEDIA_SERVICE_USER:-brm}"
# The corporate proxy has been observed to close long Xet transfers early.
# Keep each request small enough to finish, then append only a header-checked
# byte range.  One MiB is deliberately conservative for the observed proxy:
# 8MiB Xet responses were frequently terminated early, while 1MiB ranges
# complete reliably and never require discarding a partial response.
CHUNK_BYTES="${BRMMEDIA_H3_CHUNK_BYTES:-1048576}"

declare -A components=(
  [diffusion]='diffusion_models/minimax_h3_fl2va_pruned_int8_convrot.safetensors'
  [text]='text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors'
  [video]='vae/minimax_h3_video_vae_fp16.safetensors'
  [audio]='vae/minimax_h3_audio_vae_fp32.safetensors'
)

expected_size() {
  case "$1" in
    diffusion) echo 20970379616 ;;
    text) echo 15687142551 ;;
    video) echo 5207808496 ;;
    audio) echo 605254808 ;;
    *) return 1 ;;
  esac
}

download_component() {
  local name="$1" relative file expected actual attempt end chunk tmp headers received content_range
  relative="${components[$name]:-}"
  [ -n "$relative" ] || { echo "[ERROR] Unknown component: $name" >&2; return 2; }
  file="$MODEL_DIR/$relative"
  expected="$(expected_size "$name")"
  install -d "$(dirname "$file")"

  # Xet-backed Hugging Face redirects can occasionally close a long ranged
  # response early (curl 18).  Download immutable, bounded ranges and append
  # only after both length and Content-Range prove it is the requested segment.
  for attempt in $(seq 1 10000); do
    actual="$(stat -c%s "$file" 2>/dev/null || echo 0)"
    if [ "$actual" -eq "$expected" ]; then
      echo "[OK] $name complete: $actual bytes"
      return 0
    fi
    if [ "$actual" -gt "$expected" ]; then
      echo "[ERROR] $name exceeds expected byte count ($actual > $expected)" >&2
      return 1
    fi
    chunk="$CHUNK_BYTES"
    if [ $((expected - actual)) -lt "$chunk" ]; then
      chunk=$((expected - actual))
    fi
    end=$((actual + chunk - 1))
    tmp="${file}.chunk.$$"
    headers="${tmp}.headers"
    rm -f "$tmp" "$headers"
    echo "[INFO] $name attempt=$attempt range=$actual-$end/$expected"
    curl --silent --show-error --fail --location --range "$actual-$end" \
      --dump-header "$headers" \
      --retry 6 --retry-delay 5 --retry-all-errors --connect-timeout 30 \
      --speed-time 90 --speed-limit 1024 --output "$tmp" \
      "https://huggingface.co/$REPO/resolve/$REVISION/$relative" || true
    received="$(stat -c%s "$tmp" 2>/dev/null || echo 0)"
    content_range="$(awk 'BEGIN{IGNORECASE=1} /^content-range:/ {line=$0} END {gsub(/\r/, "", line); print line}' "$headers" 2>/dev/null || true)"
    if [ "$received" -eq "$chunk" ] && printf '%s\n' "$content_range" | grep -qi "^content-range: bytes $actual-$end/$expected$"; then
      dd if="$tmp" of="$file" oflag=append conv=notrunc status=none
      rm -f "$tmp" "$headers"
      continue
    fi
    echo "[WARN] $name rejected incomplete/unexpected segment: received=$received expected=$chunk range=${content_range:-missing}" >&2
    rm -f "$tmp" "$headers"
    sleep 5
  done
  echo "[ERROR] $name did not complete after repeated resumable attempts" >&2
  return 1
}

show_status() {
  local name relative file expected actual
  for name in diffusion text video audio; do
    relative="${components[$name]}"
    file="$MODEL_DIR/$relative"
    expected="$(expected_size "$name")"
    actual="$(stat -c%s "$file" 2>/dev/null || echo 0)"
    printf '%-10s %-12s %s/%s bytes  %s\n' "$name" \
      "$(systemctl is-active "brmmedia-h3-download-$name" 2>/dev/null || true)" "$actual" "$expected" "$relative"
  done
}

if [ "${1:-}" = "--worker" ]; then
  download_component "${2:?usage: $0 --worker <component>}"
  exit $?
fi
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
    /usr/bin/env bash "$(readlink -f "$0")" --worker "$name"
done
show_status
