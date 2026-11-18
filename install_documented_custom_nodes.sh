#!/usr/bin/env bash
set -euo pipefail

COMFY_ROOT="${COMFY_ROOT:-/srv/brmmedia/ComfyUI}"
PYTHON_BIN="${PYTHON_BIN:-/srv/brmmedia/app/ubuntu-backend-deploy/.venv/bin/python}"
NODE_ROOT="$COMFY_ROOT/custom_nodes"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NODE_LOCK_FILE="${BRMMEDIA_NODE_LOCK_FILE:-$SCRIPT_DIR/runtime-locks/comfyui-custom-nodes.lock.tsv}"

install_node() {
  local name="$1"
  local repo="$2"
  local ref="$3"
  if [ -d "$NODE_ROOT/$name/.git" ]; then
    local current
    current="$(git -C "$NODE_ROOT/$name" rev-parse HEAD)"
    if [ "$current" != "$ref" ]; then
      echo "[ERROR] $name is at $current, but the production lock requires $ref."
      echo "        Preserve it for investigation or explicitly restore the locked revision."
      return 1
    fi
    echo "[INFO] Reusing locked $name ($ref)"
  elif [ -e "$NODE_ROOT/$name" ]; then
    echo "[WARN] $NODE_ROOT/$name exists but is not a Git checkout; preserving it."
    return 1
  else
    echo "[INFO] Cloning locked $name ($ref)"
    git clone --filter=blob:none "$repo" "$NODE_ROOT/$name"
    git -C "$NODE_ROOT/$name" checkout --detach "$ref"
  fi
}

mkdir -p "$NODE_ROOT"
if [ ! -f "$NODE_LOCK_FILE" ]; then
  echo "[ERROR] Custom-node lock file not found: $NODE_LOCK_FILE"
  exit 1
fi

node_names=()
declare -A node_refs=()
while IFS=$'\t' read -r name repo ref; do
  [ -z "$name" ] && continue
  case "$name" in \#*) continue ;; esac
  install_node "$name" "$repo" "$ref"
  node_names+=("$name")
  node_refs["$name"]="$ref"
done < "$NODE_LOCK_FILE"

for name in "${node_names[@]}"; do
  requirements="$NODE_ROOT/$name/requirements.txt"
  if [ -f "$requirements" ]; then
    marker="$(dirname "$requirements")/.brm-requirements-installed-${node_refs[$name]}"
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
