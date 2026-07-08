#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
VLLM_PIP_SPEC="${VLLM_PIP_SPEC:-vllm}"

cd "$SCRIPT_DIR"

echo "[1/4] Creating vLLM virtual environment..."
"$PYTHON_BIN" -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip wheel setuptools

echo "[2/4] Installing vLLM..."
python -m pip install "$VLLM_PIP_SPEC" "huggingface_hub[cli]" openai

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
  huggingface-cli download "$QWEN_MODEL" || true
fi

echo
echo "[OK] vLLM environment is ready."
echo "Next:"
echo "  ./start_qwen_vllm.sh"
