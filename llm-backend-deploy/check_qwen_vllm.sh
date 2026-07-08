#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

HOST="${QWEN_HOST:-127.0.0.1}"
if [ "$HOST" = "0.0.0.0" ]; then
  HOST="127.0.0.1"
fi
PORT="${QWEN_PORT:-8000}"
MODEL="${QWEN_SERVED_MODEL_NAME:-qwen}"

echo "== vLLM models =="
curl -s "http://$HOST:$PORT/v1/models"
echo
echo

echo "== Chat smoke test =="
curl -s "http://$HOST:$PORT/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -d "{
    \"model\": \"$MODEL\",
    \"messages\": [{\"role\": \"user\", \"content\": \"用一句话说明你已准备好为包容万象生成提示词。\"}],
    \"temperature\": 0.2,
    \"max_tokens\": 128
  }"
echo
