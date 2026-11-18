#!/usr/bin/env bash
# Matching visual projector for the pinned production UD-Q4_K_M weight.
set -euo pipefail
root="${QWEN38_MODEL_DIR:-/srv/brmmedia/models/Qwen3.8-27B-GGUF}"
model="$root/mmproj-F16.gguf"
sha=cbb841a9ee0636b2ec172f5bb8df2ea8dfeb01e90fe7c6126581d662a0b4e43e
mkdir -p "$root"
check() { printf '%s  %s\n' "$sha" "$1" | sha256sum --check --status; }
if [ -f "$model" ]; then
  check "$model"
  echo '[OK] Existing projector verified.'
  exit 0
fi
curl --fail --location --retry 8 --retry-delay 5 --connect-timeout 30 --max-time 3600 --continue-at - \
  --output "$model.part" \
  'https://huggingface.co/unsloth/Qwen3.8-27B-GGUF/resolve/4ca720788d1e01f1bff70c033e0d0028fd02e502/mmproj-F16.gguf?download=true'
[ "$(stat -c %s "$model.part")" = 927607488 ]
check "$model.part"
chmod 0644 "$model.part"
mv "$model.part" "$model"
echo '[OK] Projector size and SHA-256 verified; download complete.'
