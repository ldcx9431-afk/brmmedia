#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
# vLLM 0.26.0 is the production-verified build for the Qwen3.5 4B profile.
# Do not silently resolve the newest vLLM on a recovery server.
VLLM_PIP_SPEC="${VLLM_PIP_SPEC:-vllm==0.26.0}"
VLLM_EXTRA_INDEX_URL="${VLLM_EXTRA_INDEX_URL:-}"

cd "$SCRIPT_DIR"

echo "[1/4] Creating vLLM virtual environment..."
"$PYTHON_BIN" -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip wheel setuptools

echo "[2/4] Installing vLLM..."
VLLM_INSTALL_ARGS=("$VLLM_PIP_SPEC" "huggingface_hub[cli]" openai)
if [ "${VLLM_USE_UV:-false}" = "true" ]; then
  python -m pip install uv
  UV_ARGS=(pip install --python "$SCRIPT_DIR/.venv/bin/python" --torch-backend="${VLLM_TORCH_BACKEND:-auto}")
  if [ -n "$VLLM_EXTRA_INDEX_URL" ]; then
    # Nightly may temporarily omit x86_64 wheels.  Allow uv to fall back to
    # PyPI's compatible build while still considering the nightly index.
    UV_ARGS+=(--extra-index-url "$VLLM_EXTRA_INDEX_URL" --index-strategy unsafe-best-match)
  fi
  uv "${UV_ARGS[@]}" "${VLLM_INSTALL_ARGS[@]}"
elif [ -n "$VLLM_EXTRA_INDEX_URL" ]; then
  python -m pip install --extra-index-url "$VLLM_EXTRA_INDEX_URL" "${VLLM_INSTALL_ARGS[@]}"
else
  python -m pip install "${VLLM_INSTALL_ARGS[@]}"
fi

echo "[3/4] Preparing .env..."
if [ ! -f .env ]; then
  cp .env.example .env
  sed -i "s#^HF_HOME=.*#HF_HOME=$SCRIPT_DIR/.cache/huggingface#" .env
fi

mkdir -p .cache/huggingface logs

echo "[4/4] Optional model prefetch..."
set -a
# shellcheck disable=SC1091
source .env
set +a

if [ "${PREFETCH_MODEL:-1}" = "1" ]; then
  LOCAL_MODEL_DIR="${QWEN_LOCAL_MODEL_DIR:-./models/Qwen3.6-35B-A3B-AWQ-4bit}"
  if [ "${QWEN_PREFER_LOCAL_MODEL:-true}" = "true" ] && [ -f "$LOCAL_MODEL_DIR/config.json" ]; then
    echo "[INFO] Local Qwen model found at $LOCAL_MODEL_DIR; skipping prefetch."
  else
    huggingface-cli download "$QWEN_MODEL" || true
  fi
fi

echo
echo "[OK] vLLM environment is ready."
echo "Next:"
echo "  ./start_qwen_vllm.sh"
