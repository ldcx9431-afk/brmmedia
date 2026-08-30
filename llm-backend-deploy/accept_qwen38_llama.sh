#!/usr/bin/env bash
set -euo pipefail

if [ -n "${BRMMEDIA_QWEN38_API_BASE:-}" ]; then
  BASE="$BRMMEDIA_QWEN38_API_BASE"
else
  # The validated service is local to WSL; Nginx remains the only LAN entry.
  BASE="http://127.0.0.1:8001/v1"
fi
RUNS="${BRMMEDIA_QWEN38_ACCEPTANCE_RUNS:-10}"
MODEL="${BRMMEDIA_QWEN38_MODEL:-qwen38-27b-q4-k-m}"
case "$RUNS" in ''|*[!0-9]*) echo "RUNS must be a positive integer" >&2; exit 2;; esac
[ "$RUNS" -gt 0 ] || { echo "RUNS must be greater than zero" >&2; exit 2; }

curl --fail --silent --show-error --max-time 20 "$BASE/models" | grep -Fq "$MODEL"
for i in $(seq 1 "$RUNS"); do
  response="$(curl --fail --silent --show-error --max-time 180 \
    -H 'Content-Type: application/json' \
    -d "{\"model\":\"$MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"请仅回复：Qwen3.8 双 A4000 验收通过 #$i\"}],\"temperature\":0.2,\"max_tokens\":64}" \
    "$BASE/chat/completions")"
  printf '%s' "$response" | grep -q '"choices"' || { echo "[ERROR] completion $i is invalid" >&2; exit 1; }
  echo "[OK] completion $i/$RUNS"
done
echo "[PASS] Qwen3.8 llama.cpp acceptance passed"
