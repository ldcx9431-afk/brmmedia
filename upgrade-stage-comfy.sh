#!/usr/bin/env bash
set -euo pipefail
root=/srv/brmmedia/upgrade-20261118
candidate=$root/main-comfy-045
source=/srv/brmmedia/h3-image
production=/srv/brmmedia/ComfyUI-h3-v032-canary
qwen_source=/srv/brmmedia/qwen-image21
commit=989d93d0d968e704c589ce0146bb18c77f6b50cc
layerstyle_commit=02acdc50affb84cd24f341d1fc2d3a9134b2ad3d
available=$(df -B1 --output=avail /mnt/e | tail -1)
((available >= 30 * 1024**3))
test -d "$source/ComfyUI/.git"
test -d "$source/venv"
test -d "$qwen_source/venv"
test -d "$production/custom_nodes/ComfyUI-Index-TTS"
install -d -m 0755 "$candidate"
mkdir "$candidate/ComfyUI"
git -c safe.directory="$source/ComfyUI" -C "$source/ComfyUI" archive "$commit" | tar -x -C "$candidate/ComfyUI"
printf '%s\n' "$commit" > "$candidate/commit.txt"
cp -a "$qwen_source/venv" "$candidate/venv"
for plugin in ComfyUI-Index-TTS ComfyUI_LayerStyle ComfyUI-GGUF comfyui-kjnodes comfyui-videohelpersuite comfyui_essentials rgthree-comfy ComfyUI-MelBandRoFormer; do
  test ! -d "$production/custom_nodes/$plugin" || cp -a "$production/custom_nodes/$plugin" "$candidate/ComfyUI/custom_nodes/"
done
if [[ ! -d "$candidate/ComfyUI/custom_nodes/ComfyUI_LayerStyle" ]]; then
  git clone --no-checkout https://github.com/chflame163/ComfyUI_LayerStyle.git "$candidate/ComfyUI/custom_nodes/ComfyUI_LayerStyle"
  git -C "$candidate/ComfyUI/custom_nodes/ComfyUI_LayerStyle" checkout --detach "$layerstyle_commit"
fi
test -d "$candidate/ComfyUI/custom_nodes/ComfyUI-Index-TTS"
test -d "$candidate/ComfyUI/custom_nodes/ComfyUI_LayerStyle"
rg -l 'IndexTTS2BaseNode' "$candidate/ComfyUI/custom_nodes/ComfyUI-Index-TTS" >/dev/null
rg -l 'ImageScaleByAspectRatio V2' "$candidate/ComfyUI/custom_nodes/ComfyUI_LayerStyle" >/dev/null
git -C "$candidate/ComfyUI/custom_nodes/ComfyUI_LayerStyle" rev-parse HEAD > "$candidate/layerstyle-commit.txt"
printf 'production_models:\n  base_path: %s\n' "$production/models" > "$candidate/models.yaml"
for folder in "$production"/models/*/; do
  test -d "$folder" || continue
  name=$(basename "$folder")
  [[ "$name" =~ ^[a-zA-Z0-9_-]+$ ]] || continue
  printf '  %s: %s\n' "$name" "$name" >> "$candidate/models.yaml"
done
"$candidate/venv/bin/python" -m pip freeze --all > "$candidate/requirements.lock"
"$candidate/venv/bin/python" -m pip check > "$candidate/pip-check.txt"
"$candidate/venv/bin/python" -m pytest -q "$candidate/ComfyUI/tests" > "$candidate/upstream-tests.txt"
"$candidate/venv/bin/python" "$candidate/ComfyUI/main.py" --help > "$candidate/cli.txt"
touch "$candidate/prepared"
