#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_ROOT="${INSTALL_ROOT:-/opt/baorongwanxiang}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
CUDA_INDEX_URL="${CUDA_INDEX_URL:-https://download.pytorch.org/whl/cu121}"
CPU_THREADS="$(nproc 2>/dev/null || echo 8)"

mkdir -p "$INSTALL_ROOT"

if [ "$SCRIPT_DIR" != "$INSTALL_ROOT/ubuntu-backend-deploy" ]; then
  echo "[INFO] Recommended deploy path: $INSTALL_ROOT/ubuntu-backend-deploy"
fi

cd "$SCRIPT_DIR"

echo "[1/5] Creating backend virtual environment..."
"$PYTHON_BIN" -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip wheel setuptools

echo "[2/5] Installing PyTorch CUDA wheels..."
python -m pip install torch torchvision torchaudio --index-url "$CUDA_INDEX_URL"

echo "[3/5] Installing backend requirements..."
python -m pip install -r requirements-backend.txt

echo "[4/5] Preparing ComfyUI checkout..."
COMFYUI_ROOT="${COMFYUI_ROOT:-$INSTALL_ROOT/ComfyUI}"
if [ ! -d "$COMFYUI_ROOT/.git" ]; then
  git clone https://github.com/comfyanonymous/ComfyUI.git "$COMFYUI_ROOT"
else
  echo "[INFO] Existing ComfyUI found: $COMFYUI_ROOT"
fi

echo "[5/5] Installing ComfyUI requirements..."
python -m pip install -r "$COMFYUI_ROOT/requirements.txt"

if [ -d "$SCRIPT_DIR/models" ]; then
  echo "[INFO] Bundled models found, syncing to $COMFYUI_ROOT/models ..."
  mkdir -p "$COMFYUI_ROOT/models"
  if command -v rsync >/dev/null 2>&1; then
    rsync -a --info=progress2 "$SCRIPT_DIR/models/" "$COMFYUI_ROOT/models/"
  else
    cp -a "$SCRIPT_DIR/models/." "$COMFYUI_ROOT/models/"
  fi
fi

mkdir -p "$SCRIPT_DIR/.cache/huggingface" "$SCRIPT_DIR/.cache/torch" "$SCRIPT_DIR/logs" "$SCRIPT_DIR/outputs"

if [ ! -f .env ]; then
  cp .env.example .env
  sed -i "s#^COMFYUI_ROOT=.*#COMFYUI_ROOT=$COMFYUI_ROOT#" .env
  sed -i "s#^COMFYUI_PYTHON=.*#COMFYUI_PYTHON=$SCRIPT_DIR/.venv/bin/python#" .env
  sed -i "s#^HF_HOME=.*#HF_HOME=$SCRIPT_DIR/.cache/huggingface#" .env
  sed -i "s#^TORCH_HOME=.*#TORCH_HOME=$SCRIPT_DIR/.cache/torch#" .env
fi

if ! grep -q '^OMP_NUM_THREADS=' .env; then
  {
    echo "OMP_NUM_THREADS=$CPU_THREADS"
    echo "MKL_NUM_THREADS=$CPU_THREADS"
  } >> .env
fi

mkdir -p outputs

echo
echo "[OK] Base backend installed."
echo "Next steps:"
echo "  1. Install/copy custom nodes listed in README_UBUNTU_DEPLOY.md"
echo "  2. If this is the offline package, bundled models have already been synced"
echo "  3. Edit .env if needed"
echo "  4. Run ./start_backend.sh"
