#!/usr/bin/env bash
# Discover ACE-Step XL Base/SFT in the Windows model package, stage an audited
# copy on D:, then import it into the active WSL ComfyUI runtime.  Use --check
# for a read-only production investigation before performing the copy.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODEL_SOURCE_ROOT="${BRMMEDIA_MODEL_SOURCE_ROOT:-/mnt/d/model}"
STAGED_DIR="$MODEL_SOURCE_ROOT/ACE-Step-1.5/split_files/diffusion_models"
# Current H3 production runs its own pinned ComfyUI checkout.  Preserve the
# legacy root as a fallback for recovery installs, while allowing operators to
# override the target explicitly for a staged release.
if [ -n "${BRMMEDIA_COMFYUI_MODEL_ROOT:-}" ]; then
  COMFYUI_MODEL_ROOT="$BRMMEDIA_COMFYUI_MODEL_ROOT"
elif [ -d /srv/brmmedia/ComfyUI-h3-v032-canary/models ]; then
  COMFYUI_MODEL_ROOT=/srv/brmmedia/ComfyUI-h3-v032-canary/models
else
  COMFYUI_MODEL_ROOT=/srv/brmmedia/ComfyUI/models
fi
CHECK_ONLY=0

if [ "${1:-}" = "--check" ]; then
  CHECK_ONLY=1
elif [ -n "${1:-}" ]; then
  echo "Usage: $0 [--check]" >&2
  exit 2
fi

declare -a candidates=()
if [ -n "${BRMMEDIA_ACE_STEP_WINDOWS_MODEL_DIR:-}" ]; then
  candidates+=("$BRMMEDIA_ACE_STEP_WINDOWS_MODEL_DIR")
fi
candidates+=(
  "/mnt/h/yzylauncher-win-ltx23/win-unpacked/python/ComfyUI_windows_portable/ComfyUI/models/diffusion_models/acestep"
  "$STAGED_DIR"
  "$MODEL_SOURCE_ROOT"
)

source_dir=""
for candidate in "${candidates[@]}"; do
  if [ -f "$candidate/acestep_v1.5_xl_base_bf16.safetensors" ] && \
     [ -f "$candidate/acestep_v1.5_xl_sft_bf16.safetensors" ]; then
    source_dir="$candidate"
    break
  fi
done

if [ -z "$source_dir" ]; then
  echo "[ERROR] ACE-Step XL Base/SFT were not found in any configured Windows source." >&2
  printf '        checked: %s\n' "${candidates[@]}" >&2
  exit 1
fi

echo "[OK] Found ACE-Step high-quality source: $source_dir"
verify_source() {
  local name expected_size expected_hash actual_size actual_hash
  while IFS=':' read -r name expected_size expected_hash; do
    actual_size="$(stat -c%s "$source_dir/$name")"
    actual_hash="$(sha256sum "$source_dir/$name" | awk '{print $1}')"
    if [ "$actual_size" != "$expected_size" ] || [ "$actual_hash" != "$expected_hash" ]; then
      echo "[ERROR] Windows ACE-Step source failed checksum verification: $name" >&2
      return 1
    fi
    echo "[OK] $name ($actual_size bytes; SHA-256 verified)"
  done <<'EOF'
acestep_v1.5_xl_base_bf16.safetensors:9974719930:56bf816fc9a69a5f45635e867b2ad742e1e648eb51fadb7d124cb8332d2e0940
acestep_v1.5_xl_sft_bf16.safetensors:9974719930:3c05ae268353b3540fb1fd7db4fd77ffbda9802ec641b624e15648e030ecf3ce
EOF
}

verify_source

if [ "$CHECK_ONLY" = "1" ]; then
  echo "[OK] Read-only check completed; no model files or services were changed."
  exit 0
fi

if [ "$source_dir" != "$STAGED_DIR" ]; then
  install -d "$STAGED_DIR"
  for name in acestep_v1.5_xl_base_bf16.safetensors acestep_v1.5_xl_sft_bf16.safetensors; do
    rsync -a --partial --append-verify --info=progress2 "$source_dir/$name" "$STAGED_DIR/$name"
  done
fi

"$SCRIPT_DIR/download_acestep_quality_models.sh" --verify
BRMMEDIA_COMFYUI_MODEL_ROOT="$COMFYUI_MODEL_ROOT" \
  "$SCRIPT_DIR/import_acestep_quality_models.sh"
echo "[OK] ACE-Step XL Base/SFT are imported. Restart baorong-backend to expose them to the UI/API."
