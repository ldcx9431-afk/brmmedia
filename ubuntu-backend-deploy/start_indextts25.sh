#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${BRMMEDIA_INDEXTTS25_ENV_FILE:-$SCRIPT_DIR/.env.indextts25}"
if [[ ! -f "$ENV_FILE" ]]; then
  echo "[ERROR] Missing IndexTTS-2.5 environment file: $ENV_FILE" >&2
  exit 1
fi
set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

: "${INDEXTTS25_VENV:?INDEXTTS25_VENV is required}"
: "${INDEXTTS25_SOURCE_ROOT:?INDEXTTS25_SOURCE_ROOT is required}"
: "${INDEXTTS25_MODEL_DIR:?INDEXTTS25_MODEL_DIR is required}"
PYTHON="$INDEXTTS25_VENV/bin/python"
[[ -x "$PYTHON" ]] || { echo "[ERROR] Missing candidate Python: $PYTHON" >&2; exit 1; }
[[ -f "$INDEXTTS25_SOURCE_ROOT/indextts/infer_v2_5.py" ]] || { echo "[ERROR] Missing pinned IndexTTS-2.5 source" >&2; exit 1; }
[[ -f "$INDEXTTS25_MODEL_DIR/config.yaml" ]] || { echo "[ERROR] Missing IndexTTS-2.5 model/config" >&2; exit 1; }

export PYTHONPATH="$SCRIPT_DIR:$INDEXTTS25_SOURCE_ROOT${PYTHONPATH:+:$PYTHONPATH}"
# PyTorch's JIT extension loader invokes the ``ninja`` executable rather than
# importing its Python module.  The candidate service is launched by systemd,
# whose PATH does not include the isolated venv by default.
export PATH="$(dirname "$PYTHON"):$PATH"
export PYTHONUNBUFFERED=1
export CUDA_VISIBLE_DEVICES="${INDEXTTS25_CUDA_VISIBLE_DEVICES:-0}"
export INDEXTTS25_RUN_DIR="${INDEXTTS25_RUN_DIR:-$SCRIPT_DIR/runtime-locks/indextts25-v2.5/run}"
mkdir -p "$INDEXTTS25_RUN_DIR"

echo "[INFO] IndexTTS-2.5 loopback service on ${INDEXTTS25_HOST:-127.0.0.1}:${INDEXTTS25_PORT:-9205}; media GPU=$CUDA_VISIBLE_DEVICES"
exec "$PYTHON" -m uvicorn indextts25_server:app \
  --host "${INDEXTTS25_HOST:-127.0.0.1}" --port "${INDEXTTS25_PORT:-9205}" --proxy-headers
