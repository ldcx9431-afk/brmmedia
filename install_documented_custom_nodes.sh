#!/usr/bin/env bash
set -euo pipefail

COMFY_ROOT="${COMFY_ROOT:-/srv/brmmedia/ComfyUI}"
PYTHON_BIN="${PYTHON_BIN:-/srv/brmmedia/app/ubuntu-backend-deploy/.venv/bin/python}"
NODE_ROOT="$COMFY_ROOT/custom_nodes"

install_node() {
  local name="$1"
  local repo="$2"
  if [ -d "$NODE_ROOT/$name/.git" ]; then
    echo "[INFO] Reusing existing $name (skipping network update)"
  elif [ -e "$NODE_ROOT/$name" ]; then
    echo "[WARN] $NODE_ROOT/$name exists but is not a Git checkout; preserving it."
  else
    echo "[INFO] Cloning $name"
    git clone --depth 1 "$repo" "$NODE_ROOT/$name"
  fi
}

mkdir -p "$NODE_ROOT"
install_node ComfyUI-Index-TTS https://github.com/chenpipi0807/ComfyUI-Index-TTS.git
install_node ComfyUI-PromptRelay https://github.com/kijai/ComfyUI-PromptRelay.git
install_node rgthree-comfy https://github.com/rgthree/rgthree-comfy.git
install_node WhatDreamsCost-ComfyUI https://github.com/WhatDreamsCost/WhatDreamsCost-ComfyUI.git
install_node comfyui-easy-use https://github.com/yolain/ComfyUI-Easy-Use.git
install_node ComfyUI-GGUF https://github.com/city96/ComfyUI-GGUF.git
install_node comfyui-kjnodes https://github.com/kijai/ComfyUI-KJNodes.git
install_node ComfyUI-MelBandRoFormer https://github.com/kijai/ComfyUI-MelBandRoFormer.git
install_node comfyui-videohelpersuite https://github.com/Kosinkadink/ComfyUI-VideoHelperSuite.git
install_node comfyui_essentials https://github.com/cubiq/ComfyUI_essentials.git
install_node comfyui_layerstyle https://github.com/chflame163/ComfyUI_LayerStyle.git

for requirements in "$NODE_ROOT"/{ComfyUI-Index-TTS,ComfyUI-PromptRelay,rgthree-comfy,WhatDreamsCost-ComfyUI,comfyui-easy-use,ComfyUI-GGUF,comfyui-kjnodes,ComfyUI-MelBandRoFormer,comfyui-videohelpersuite,comfyui_essentials,comfyui_layerstyle}/requirements.txt; do
  if [ -f "$requirements" ]; then
    marker="$(dirname "$requirements")/.brm-requirements-installed"
    if [ -f "$marker" ]; then
      echo "[INFO] Requirements already installed for $(dirname "$requirements")"
    else
      echo "[INFO] Installing $(dirname "$requirements") requirements"
      # Some optional test assets in node dependencies are stored in Git LFS.
      # They are not needed at runtime and can make unattended installs wait
      # indefinitely for a large LFS smudge operation.
      GIT_LFS_SKIP_SMUDGE=1 "$PYTHON_BIN" -m pip install -r "$requirements"
      touch "$marker"
    fi
  fi
done

echo "[OK] Documented custom nodes installed."
