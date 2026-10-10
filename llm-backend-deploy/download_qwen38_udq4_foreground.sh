#!/usr/bin/env bash
set -euo pipefail

MODEL_DIR="${BRMMEDIA_QWEN38_MODEL_SOURCE_DIR:-/mnt/d/model}"
MODEL_FILE="${MODEL_DIR}/Qwen3.8-27B-UD-Q4_K_XL.gguf"
ARIA_CONTROL_FILE="${MODEL_FILE}.aria2"
LOG_DIR="${BRMMEDIA_LOG_DIR:-/var/log/brmmedia}"
REVISION="f1bfb127c64f7072bdd2cad55f258b9c8b2910fe"
URL="https://huggingface.co/unsloth/Qwen3.8-27B-GGUF/resolve/${REVISION}/Qwen3.8-27B-UD-Q4_K_XL.gguf?download=true"
# Hugging Face's large-file CDN can deliberately return short partial chunks
# behind enterprise proxies. Reissue a ranged request until the immutable file
# size is present; curl's --continue-at makes every retry safe and resumable.
EXPECTED_BYTES=17923394624
EXPECTED_SHA256="bee238bbeb3dc0a34bde4d0dedbaee1f98c009e8bb4226f03070054c12fb1372"

mkdir -p "$MODEL_DIR" "$LOG_DIR"
while :; do
  actual_bytes=0
  [ -f "$MODEL_FILE" ] && actual_bytes="$(stat -c %s "$MODEL_FILE")"
  # aria2 can preallocate a sparse target file before every range is present.
  # Its .aria2 control file is authoritative until the last range completes.
  if [ "$actual_bytes" -eq "$EXPECTED_BYTES" ] && [ ! -e "$ARIA_CONTROL_FILE" ]; then
    actual_sha256="$(sha256sum "$MODEL_FILE" | awk '{print $1}')"
    [ "$actual_sha256" = "$EXPECTED_SHA256" ] || {
      printf 'SHA-256 mismatch: %s (expected %s)\n' "$actual_sha256" "$EXPECTED_SHA256" >&2
      exit 1
    }
    printf 'Download complete and verified: %s bytes, %s\n' "$actual_bytes" "$actual_sha256"
    exit 0
  fi
  if [ "$actual_bytes" -gt "$EXPECTED_BYTES" ]; then
    printf 'Refusing oversized file: %s (expected %s)\n' "$actual_bytes" "$EXPECTED_BYTES" >&2
    exit 1
  fi
  printf 'Downloading: %s / %s bytes%s\n' "$actual_bytes" "$EXPECTED_BYTES" \
    "$([ -e "$ARIA_CONTROL_FILE" ] && printf ' (aria2 ranges pending)')"
  if command -v aria2c >/dev/null 2>&1; then
    # Use concurrent HTTP ranges where the HF/Xet gateway permits them. This
    # remains resumable and falls back to curl if aria2 exits unexpectedly.
    aria2c --continue=true --max-connection-per-server=8 --split=8 \
      --min-split-size=4M --file-allocation=none --auto-file-renaming=false \
      --allow-overwrite=true --summary-interval=0 --console-log-level=warn \
      --dir "$MODEL_DIR" --out "$(basename "$MODEL_FILE")" "$URL" || true
  else
    curl -L --fail --connect-timeout 20 --max-time 60 --retry 8 --retry-delay 5 --continue-at - \
      --output "$MODEL_FILE" "$URL" || true
  fi
  sleep 1
done
