#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BREEZE_DIR="${BRM_BREEZE_DIR:-/srv/brmmedia/breeze-tts2}"
CORE_BIN="${BRM_BREEZE_CORE_BIN:-$BREEZE_DIR/build-cpu-openblas/breeze-server}"
MODEL_PATH="${BRM_BREEZE_MODEL_PATH:-/srv/brmmedia/models/Breeze-TTS2/breeze-tts-2-q8_0.gguf}"
VOICES_DIR="${BRM_BREEZE_VOICES_DIR:-/srv/brmmedia/models/Breeze-TTS2/voices}"
CORE_HOST="${BRM_BREEZE_CORE_HOST:-127.0.0.1}"
CORE_PORT="${BRM_BREEZE_CORE_PORT:-9207}"
HOST="${BRM_BREEZE_HOST:-127.0.0.1}"
PORT="${BRM_BREEZE_PORT:-9206}"
PYTHON="${BRM_BREEZE_PYTHON:-/srv/brmmedia/releases/h3-23d7084/runtime-locks/venvs/comfyui-v039/bin/python}"

[[ -x "$CORE_BIN" ]] || { echo "[ERROR] Missing breeze-server binary: $CORE_BIN" >&2; exit 1; }
[[ -f "$MODEL_PATH" ]] || { echo "[ERROR] Missing Breeze-TTS2 model: $MODEL_PATH" >&2; exit 1; }
[[ -x "$PYTHON" ]] || { echo "[ERROR] Missing Python runtime: $PYTHON" >&2; exit 1; }
mkdir -p "$VOICES_DIR"

export BREEZE_THREADS="${BREEZE_THREADS:-24}"
export OMP_NUM_THREADS="${OMP_NUM_THREADS:-1}"
export OPENBLAS_NUM_THREADS="${OPENBLAS_NUM_THREADS:-1}"
export BRM_BREEZE_CORE_URL="http://${CORE_HOST}:${CORE_PORT}"
export BRM_BREEZE_MODEL_PATH="$MODEL_PATH"
export BRM_BREEZE_VOICES_DIR="$VOICES_DIR"
export PYTHONPATH="$SCRIPT_DIR${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONUNBUFFERED=1

CORE_PID=""
cleanup() {
  if [[ -n "$CORE_PID" ]] && kill -0 "$CORE_PID" 2>/dev/null; then
    kill "$CORE_PID" 2>/dev/null || true
    wait "$CORE_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

echo "[INFO] Starting breeze-server core on ${CORE_HOST}:${CORE_PORT} (BREEZE_THREADS=$BREEZE_THREADS)..."
"$CORE_BIN" "$MODEL_PATH" \
  --host "$CORE_HOST" \
  --port "$CORE_PORT" \
  --ws-port -1 \
  --cpu \
  --voices-dir "$VOICES_DIR" \
  --parallel 1 \
  --no-warmup \
  --verbose &
CORE_PID=$!

for _ in $(seq 1 60); do
  if ! kill -0 "$CORE_PID" 2>/dev/null; then
    echo "[ERROR] breeze-server core exited prematurely" >&2
    exit 1
  fi
  if curl -fsS "http://${CORE_HOST}:${CORE_PORT}/health" >/dev/null 2>&1; then
    break
  fi
  sleep 0.5
done

if ! curl -fsS "http://${CORE_HOST}:${CORE_PORT}/health" >/dev/null 2>&1; then
  echo "[ERROR] breeze-server core did not become healthy in time" >&2
  exit 1
fi

echo "[INFO] Breeze-TTS2 FastAPI wrapper listening on ${HOST}:${PORT}"
"$PYTHON" -m uvicorn breeze_tts2_server:app --host "$HOST" --port "$PORT" --proxy-headers &
UVICORN_PID=$!
wait -n "$CORE_PID" "$UVICORN_PID"
