#!/usr/bin/env bash
# Gate an H3-capable ComfyUI before it may serve a candidate or production
# backend.  A successful process/HTTP health check is not enough after a
# ComfyUI upgrade: retained custom nodes and their static model selectors
# must still be present in the live object-info schema.
set -euo pipefail

APP_ROOT="${BRMMEDIA_APP_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
BACKEND_DIR="$APP_ROOT/ubuntu-backend-deploy"
PYTHON_BIN="${BRMMEDIA_H3_GATE_PYTHON:-$BACKEND_DIR/.venv/bin/python}"
COMFY_URL="${BRMMEDIA_H3_GATE_URL:-http://127.0.0.1:8188}"
WORKFLOWS_DIR="${BRMMEDIA_H3_GATE_WORKFLOWS:-$BACKEND_DIR/workflows}"
TIMEOUT="${BRMMEDIA_H3_GATE_TIMEOUT:-15}"

usage() {
  echo "usage: $0 [--url URL] [--workflows DIR] [--timeout SECONDS]" >&2
  exit 2
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --url) COMFY_URL="${2:-}"; shift 2 ;;
    --workflows) WORKFLOWS_DIR="${2:-}"; shift 2 ;;
    --timeout) TIMEOUT="${2:-}"; shift 2 ;;
    -h|--help) usage ;;
    *) usage ;;
  esac
done

if [ ! -x "$PYTHON_BIN" ]; then
  echo "[ERROR] H3 workflow gate Python is unavailable: $PYTHON_BIN" >&2
  exit 2
fi
if [ ! -f "$APP_ROOT/validate_comfy_workflows.py" ]; then
  echo "[ERROR] H3 workflow validator is missing: $APP_ROOT/validate_comfy_workflows.py" >&2
  exit 2
fi
if [ ! -d "$WORKFLOWS_DIR" ]; then
  echo "[ERROR] H3 workflow directory is unavailable: $WORKFLOWS_DIR" >&2
  exit 2
fi
if ! [[ "$TIMEOUT" =~ ^[1-9][0-9]*$ ]]; then
  echo "[ERROR] H3 workflow gate timeout must be a positive integer." >&2
  exit 2
fi

# Both candidate H3 templates must be present in the exact checked-in
# workflow directory.  The full validator below then checks these and every
# retained workflow against the live ComfyUI node registry.
for workflow in MiniMaxH3-文生视频.json MiniMaxH3-图生视频.json; do
  if [ ! -f "$WORKFLOWS_DIR/$workflow" ]; then
    echo "[ERROR] Required H3 workflow is missing: $WORKFLOWS_DIR/$workflow" >&2
    exit 1
  fi
done

echo "[gate] Checking H3 and retained ComfyUI workflows at $COMFY_URL..."
"$PYTHON_BIN" "$APP_ROOT/validate_comfy_workflows.py" \
  --url "$COMFY_URL" --workflows "$WORKFLOWS_DIR" --timeout "$TIMEOUT"
echo "[OK] H3 workflow compatibility gate passed."
