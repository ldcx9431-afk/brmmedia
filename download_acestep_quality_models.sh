#!/usr/bin/env bash
# Stage the two ACE-Step 1.5 XL quality DiT weights on the controlled Windows
# model source.  The runtime import script copies them into ComfyUI separately.
set -euo pipefail

MODEL_SOURCE_ROOT="${BRMMEDIA_MODEL_SOURCE_ROOT:-/mnt/d/model}"
MODEL_DIR="$MODEL_SOURCE_ROOT/ACE-Step-1.5"
APP_ROOT="${BRMMEDIA_APP_ROOT:-/srv/brmmedia/app}"
REPO="${BRMMEDIA_ACE_STEP_REPO:-Comfy-Org/ace_step_1.5_ComfyUI_files}"
REVISION="${BRMMEDIA_ACE_STEP_REVISION:-694a9723ff772285c73f0700caacf944d3f02f8d}"
TRANSPORT="${BRMMEDIA_ACE_STEP_TRANSPORT:-auto}"
VERIFY_ONLY=0
ACE_STEP_FILES=(
  'split_files/diffusion_models/acestep_v1.5_xl_base_bf16.safetensors'
  'split_files/diffusion_models/acestep_v1.5_xl_sft_bf16.safetensors'
)

HF_BIN="${BRMMEDIA_HF_BIN:-}"
if [ -z "$HF_BIN" ] && command -v hf >/dev/null 2>&1; then
  HF_BIN="$(command -v hf)"
fi
if [ -z "$HF_BIN" ] && [ -x "$APP_ROOT/ubuntu-backend-deploy/.venv/bin/hf" ]; then
  HF_BIN="$APP_ROOT/ubuntu-backend-deploy/.venv/bin/hf"
fi

download_with_hf() {
  [ -n "$HF_BIN" ] && [ -x "$HF_BIN" ] || {
    echo "[ERROR] Hugging Face CLI 'hf' is required for hf transport." >&2
    return 1
  }
  echo "[INFO] Staging $REPO@$REVISION with Hugging Face CLI"
  HF_HUB_DISABLE_XET=1 "$HF_BIN" download "$REPO" --revision "$REVISION" \
    --local-dir "$MODEL_DIR" --include "${ACE_STEP_FILES[@]}"
}

download_with_curl() {
  command -v curl >/dev/null 2>&1 || {
    echo "[ERROR] curl is required for curl transport." >&2
    return 1
  }
  local base="https://huggingface.co/$REPO/resolve/$REVISION" relative target
  echo "[INFO] Staging $REPO@$REVISION with resumable curl downloads"
  for relative in "${ACE_STEP_FILES[@]}"; do
    target="$MODEL_DIR/$relative"
    mkdir -p "$(dirname "$target")"
    curl --fail --location --continue-at - --retry 12 --retry-delay 5 --retry-all-errors \
      --connect-timeout 30 --speed-time 90 --speed-limit 1024 \
      --output "$target" "$base/$relative"
  done
}

if [ "${1:-}" = "--verify" ]; then
  VERIFY_ONLY=1
elif [ -n "${1:-}" ]; then
  echo "Usage: $0 [--verify]" >&2
  exit 2
fi

if [ "$VERIFY_ONLY" = "0" ]; then
  mkdir -p "$MODEL_DIR"
  case "$TRANSPORT" in
    hf) download_with_hf ;;
    curl) download_with_curl ;;
    auto) download_with_hf || download_with_curl ;;
    *) echo "[ERROR] BRMMEDIA_ACE_STEP_TRANSPORT must be auto, hf, or curl." >&2; exit 1 ;;
  esac
fi

verify_components() {
  local relative expected_size expected_hash actual_size actual_hash
  while IFS=':' read -r relative expected_size expected_hash; do
    actual_size="$(stat -c%s "$MODEL_DIR/$relative" 2>/dev/null || true)"
    if [ "$actual_size" != "$expected_size" ]; then
      echo "[ERROR] ACE-Step component size mismatch: $relative (expected $expected_size, got ${actual_size:-missing})" >&2
      return 1
    fi
    actual_hash="$(sha256sum "$MODEL_DIR/$relative" | awk '{print $1}')"
    if [ "$actual_hash" != "$expected_hash" ]; then
      echo "[ERROR] ACE-Step component SHA-256 mismatch: $relative" >&2
      return 1
    fi
  done <<'EOF'
split_files/diffusion_models/acestep_v1.5_xl_base_bf16.safetensors:9974719930:56bf816fc9a69a5f45635e867b2ad742e1e648eb51fadb7d124cb8332d2e0940
split_files/diffusion_models/acestep_v1.5_xl_sft_bf16.safetensors:9974719930:3c05ae268353b3540fb1fd7db4fd77ffbda9802ec641b624e15648e030ecf3ce
EOF
}

verify_components
echo "[OK] ACE-Step 1.5 XL base and sft source weights verified in $MODEL_DIR."
