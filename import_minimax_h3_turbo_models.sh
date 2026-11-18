#!/usr/bin/env bash
# Copy checksum-gated LightX2V LoRAs from D: to the selected ComfyUI model
# directory on the E: WSL ext4 volume.  This never changes an active workflow.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_ROOT="${BRMMEDIA_MODEL_SOURCE_ROOT:-/mnt/d/model}/MiniMax-H3-Turbo"
TARGET_ROOT="${BRMMEDIA_COMFYUI_MODEL_ROOT:-/srv/brmmedia/ComfyUI-h3-v032-canary/models}"
VERIFY_SCRIPT="$SCRIPT_DIR/download_minimax_h3_turbo_models.sh"

"$VERIFY_SCRIPT" --verify
install -d "$TARGET_ROOT/loras"
for name in \
  minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors \
  minimax_h3_fl2v_turbo_4step_v1.0_768p_comfyui_bf16.safetensors
do
  rsync -a --partial --append-verify --info=progress2 \
    "$SOURCE_ROOT/$name" "$TARGET_ROOT/loras/$name"
  source_hash="$(sha256sum "$SOURCE_ROOT/$name" | awk '{print $1}')"
  target_hash="$(sha256sum "$TARGET_ROOT/loras/$name" | awk '{print $1}')"
  [ "$source_hash" = "$target_hash" ] || {
    echo "[ERROR] Imported LoRA checksum mismatch: $name" >&2
    exit 1
  }
done

echo "[OK] LightX2V Turbo LoRAs imported into $TARGET_ROOT/loras"
