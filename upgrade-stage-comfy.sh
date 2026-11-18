#!/usr/bin/env bash
set -euo pipefail
root=/srv/brmmedia/upgrade-20261118
candidate=$root/main-comfy-045
source=/srv/brmmedia/h3-image
production=/srv/brmmedia/ComfyUI-h3-v032-canary
commit=989d93d0d968e704c589ce0146bb18c77f6b50cc
layerstyle_commit=02acdc50affb84cd24f341d1fc2d3a9134b2ad3d
indextts_commit=f6274d13443be238ee8940dadb9bb0036cfd4694
available=$(df -B1 --output=avail /mnt/e | tail -1)
((available >= 30 * 1024**3))
test -d "$source/ComfyUI/.git"
test -d "$source/venv"
for entry in "comfyui_layerstyle:$layerstyle_commit" "ComfyUI-Index-TTS:$indextts_commit"; do
  plugin_path="$production/custom_nodes/${entry%%:*}"
  test "$(git -c safe.directory="$plugin_path" -C "$plugin_path" rev-parse HEAD)" = "${entry#*:}"
done
install -d -m 0700 "$root"
mkdir -m 0755 "$candidate"
mkdir "$candidate/ComfyUI"
git -c safe.directory="$source/ComfyUI" -C "$source/ComfyUI" archive "$commit" | tar -x -C "$candidate/ComfyUI"
printf '%s\n' "$commit" > "$candidate/commit.txt"
cp -a "$source/venv" "$candidate/venv"
for plugin in "$production"/custom_nodes/*/; do
  [[ -d "$plugin" && "${plugin%/}" != */__pycache__ ]] || continue
  cp -a -- "${plugin%/}" "$candidate/ComfyUI/custom_nodes/"
done
grep -rlF --include='*.py' 'IndexTTS2BaseNode' "$candidate/ComfyUI/custom_nodes/ComfyUI-Index-TTS" >/dev/null
grep -rlF --include='*.py' 'ImageScaleByAspectRatio V2' "$candidate/ComfyUI/custom_nodes/comfyui_layerstyle" >/dev/null
printf '%s\n' "$layerstyle_commit" > "$candidate/layerstyle-commit.txt"
printf 'production_models:\n  base_path: %s\n' "$production/models" > "$candidate/models.yaml"
for folder in "$production"/models/*/; do
  test -d "$folder" || continue
  name=$(basename "$folder")
  [[ "$name" =~ ^[a-zA-Z0-9_-]+$ ]] || continue
  printf '  %s: %s\n' "$name" "$name" >> "$candidate/models.yaml"
done
"$candidate/venv/bin/python" -m pip freeze --all > "$candidate/requirements.lock"
"$candidate/venv/bin/python" -m pip check > "$candidate/pip-check.txt"
"$candidate/venv/bin/python" "$candidate/ComfyUI/main.py" --help > "$candidate/cli.txt"
touch "$candidate/staged-source-only"
