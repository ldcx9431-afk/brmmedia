#!/usr/bin/env bash
# Copy checksum-gated ACE-Step 1.5 XL base/SFT weights from D: into the
# production ComfyUI model root.  It never alters the existing Turbo weight.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_ROOT="${BRMMEDIA_MODEL_SOURCE_ROOT:-/mnt/d/model}/ACE-Step-1.5/split_files/diffusion_models"
TARGET_ROOT="${BRMMEDIA_COMFYUI_MODEL_ROOT:-/srv/brmmedia/ComfyUI/models}/diffusion_models/acestep"

"$SCRIPT_DIR/download_acestep_quality_models.sh" --verify
install -d "$TARGET_ROOT"

for name in acestep_v1.5_xl_base_bf16.safetensors acestep_v1.5_xl_sft_bf16.safetensors; do
  source="$SOURCE_ROOT/$name"
  target="$TARGET_ROOT/$name"
  rsync -a --partial --append-verify --info=progress2 "$source" "$target"
  source_hash="$(sha256sum "$source" | awk '{print $1}')"
  target_hash="$(sha256sum "$target" | awk '{print $1}')"
  [ "$source_hash" = "$target_hash" ] || {
    echo "[ERROR] Imported ACE-Step checksum mismatch: $name" >&2
    exit 1
  }
done

echo "[OK] ACE-Step XL base and sft imported into $TARGET_ROOT"
