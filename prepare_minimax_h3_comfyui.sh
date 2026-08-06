#!/usr/bin/env bash
# Controlled ComfyUI upgrade for native MiniMax H3 nodes.  It writes a
# recoverable commit snapshot before changing the checkout.
set -euo pipefail

COMFY_ROOT="${COMFYUI_ROOT:-/srv/brmmedia/ComfyUI}"
APP_ROOT="${BRMMEDIA_APP_ROOT:-/srv/brmmedia/app}"
PYTHON_BIN="${COMFYUI_PYTHON:-$APP_ROOT/ubuntu-backend-deploy/.venv/bin/python}"
# Verified against the official ComfyUI master on 2026-08-05.  This commit
# includes comfy_extras/nodes_minimax_h3.py and its native FL2VA nodes.
H3_COMFY_REF="${BRMMEDIA_H3_COMFYUI_REF:-563b98eefbe643a4cd510ee7f0b43e79880d5a3f}"
BACKUP_DIR="$APP_ROOT/runtime-locks/comfyui-h3-backups"

if [ ! -d "$COMFY_ROOT/.git" ]; then
  echo "[ERROR] ComfyUI checkout missing: $COMFY_ROOT" >&2
  exit 1
fi
if [ ! -x "$PYTHON_BIN" ]; then
  echo "[ERROR] Backend Python missing: $PYTHON_BIN" >&2
  exit 1
fi

mkdir -p "$BACKUP_DIR"
stamp="$(date +%Y%m%d-%H%M%S)"
git -C "$COMFY_ROOT" rev-parse HEAD > "$BACKUP_DIR/before-h3-$stamp.commit"
# The isolated canary intentionally replaces its checkout-local models/
# placeholders with a symlink to the verified shared E: model store.  Ignore
# only that expected runtime indirection; all source and custom-node changes
# remain a hard pre-upgrade failure.
git -C "$COMFY_ROOT" status --short -- . ':(exclude)models/**' > "$BACKUP_DIR/before-h3-$stamp.status"
"$PYTHON_BIN" -m pip freeze > "$BACKUP_DIR/before-h3-$stamp.python-requirements.txt"
if [ -s "$BACKUP_DIR/before-h3-$stamp.status" ]; then
  echo "[ERROR] ComfyUI checkout has local changes; resolve or snapshot them before upgrading." >&2
  exit 1
fi

previous_ref="$(cat "$BACKUP_DIR/before-h3-$stamp.commit")"
previous_requirements="$BACKUP_DIR/before-h3-$stamp.python-requirements.txt"
upgraded=0
rollback_checkout() {
  if [ "$upgraded" -eq 1 ]; then
    echo "[WARN] H3 preparation failed; restoring ComfyUI checkout $previous_ref" >&2
    git -C "$COMFY_ROOT" checkout --detach "$previous_ref" || true
    if [ -s "$previous_requirements" ]; then
      echo "[WARN] Restoring the pre-upgrade Python package versions" >&2
      "$PYTHON_BIN" -m pip install -r "$previous_requirements" || true
    fi
  fi
}
trap rollback_checkout ERR

git -C "$COMFY_ROOT" fetch --tags origin
git -C "$COMFY_ROOT" checkout --detach "$H3_COMFY_REF"
upgraded=1
"$PYTHON_BIN" -m pip install -r "$COMFY_ROOT/requirements.txt"

if ! COMFYUI_ROOT="$COMFY_ROOT" "$PYTHON_BIN" - <<'PY'
import sys
sys.path.insert(0, "")
from pathlib import Path
root = Path(__import__("os").environ.get("COMFYUI_ROOT", "/srv/brmmedia/ComfyUI"))
assert (root / "comfy_extras/nodes_minimax_h3.py").is_file(), "native MiniMax H3 nodes are missing"
PY
then
  echo "[ERROR] upgraded ComfyUI lacks native MiniMax H3 nodes" >&2
  exit 1
fi
trap - ERR

echo "[OK] ComfyUI pinned to $H3_COMFY_REF; rollback reference: $(cat "$BACKUP_DIR/before-h3-$stamp.commit")"
