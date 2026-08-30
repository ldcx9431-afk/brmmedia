#!/usr/bin/env bash
# Download the two reviewed LightX2V MiniMax-H3 Turbo v1.0 ComfyUI LoRAs to
# the Windows D: model source.  The immutable revision, byte sizes and LFS
# SHA-256 values prevent a moving Hub repository from changing production.
set -euo pipefail

MODEL_SOURCE_ROOT="${BRMMEDIA_MODEL_SOURCE_ROOT:-/mnt/d/model}"
MODEL_DIR="$MODEL_SOURCE_ROOT/MiniMax-H3-Turbo"
REPO="${BRMMEDIA_H3_TURBO_REPO:-lightx2v/Minimax-h3-Turbo}"
REVISION="${BRMMEDIA_H3_TURBO_REVISION:-5d1d4829fe614c1b93fcfd9cc7718e9ba71f73e1}"
BASE_URL="${BRMMEDIA_H3_TURBO_BASE_URL:-https://huggingface.co}"
TRANSPORT="${BRMMEDIA_H3_TURBO_TRANSPORT:-curl}"

declare -A EXPECTED_SIZE=(
  [minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors]=1956193000
  [minimax_h3_fl2v_turbo_4step_v1.0_768p_comfyui_bf16.safetensors]=1956192992
)
declare -A EXPECTED_SHA256=(
  [minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors]=2339acdf19bfe123f46b971ea35d367a84adb85de43627e1eceafa5a5b2b111e
  [minimax_h3_fl2v_turbo_4step_v1.0_768p_comfyui_bf16.safetensors]=c396a9a06f58399e9df9754b18299818d84a2ddd371724ba48fe4a41221437dc
)
FILES=(
  minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors
  minimax_h3_fl2v_turbo_4step_v1.0_768p_comfyui_bf16.safetensors
)

mkdir -p "$MODEL_DIR"

verify_file() {
  local name="$1" file="$MODEL_DIR/$1" size actual
  size="$(stat -c%s "$file" 2>/dev/null || true)"
  [ "$size" = "${EXPECTED_SIZE[$name]}" ] || {
    echo "[ERROR] $name size mismatch: expected ${EXPECTED_SIZE[$name]}, got ${size:-missing}" >&2
    return 1
  }
  actual="$(sha256sum "$file" | awk '{print $1}')"
  [ "$actual" = "${EXPECTED_SHA256[$name]}" ] || {
    echo "[ERROR] $name SHA-256 mismatch" >&2
    return 1
  }
  echo "[OK] $name ($size bytes, SHA-256 verified)"
}

if [ "${1:-}" = "--verify" ]; then
  for name in "${FILES[@]}"; do verify_file "$name"; done
  exit 0
fi

case "$TRANSPORT" in
  curl)
    for name in "${FILES[@]}"; do
      target="$MODEL_DIR/$name"
      if [ -f "$target" ] && verify_file "$name" 2>/dev/null; then
        continue
      fi
      echo "[INFO] Downloading $REPO@$REVISION/$name"
      curl --fail --location --continue-at - --retry 20 --retry-delay 5 \
        --retry-all-errors --connect-timeout 30 --speed-time 120 --speed-limit 1024 \
        --output "$target" "$BASE_URL/$REPO/resolve/$REVISION/$name"
      verify_file "$name"
    done
    ;;
  hf)
    command -v hf >/dev/null 2>&1 || { echo "[ERROR] hf CLI is unavailable" >&2; exit 1; }
    HF_HUB_DISABLE_XET=1 hf download "$REPO" --revision "$REVISION" \
      --local-dir "$MODEL_DIR" "${FILES[@]}"
    for name in "${FILES[@]}"; do verify_file "$name"; done
    ;;
  *) echo "[ERROR] BRMMEDIA_H3_TURBO_TRANSPORT must be curl or hf" >&2; exit 2 ;;
esac

echo "[OK] LightX2V MiniMax-H3 Turbo v1.0 source weights are ready in $MODEL_DIR"
