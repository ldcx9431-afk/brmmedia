#!/usr/bin/env bash
# Runtime acceptance for the dedicated A4000 Qwen vLLM service.  This script
# only makes loopback API calls; it never changes a service or logs credentials.
set -euo pipefail

API_BASE="${BRMMEDIA_QWEN_API_BASE:-http://127.0.0.1:8000/v1}"
RUNS="${BRMMEDIA_QWEN_ACCEPTANCE_RUNS:-10}"
REPORT_DIR="${BRMMEDIA_ACCEPTANCE_REPORT_DIR:-/srv/brmmedia/artifacts/acceptance}"

if ! [[ "$RUNS" =~ ^[1-9][0-9]*$ ]]; then
  echo "[ERROR] BRMMEDIA_QWEN_ACCEPTANCE_RUNS must be a positive integer." >&2
  exit 2
fi
for command in curl python3 date; do
  command -v "$command" >/dev/null 2>&1 || {
    echo "[ERROR] Required command is unavailable: $command" >&2
    exit 1
  }
done

mkdir -p "$REPORT_DIR"
work_dir="$(mktemp -d /tmp/brmmedia-qwen-accept.XXXXXX)"
trap 'rm -rf "$work_dir"' EXIT

models="$(curl --fail --silent --show-error --max-time 30 "$API_BASE/models")"
model="$(printf '%s' "$models" | python3 -c '
import json, sys
payload = json.load(sys.stdin)
models = payload.get("data")
if not isinstance(models, list) or not models or not isinstance(models[0].get("id"), str):
    raise SystemExit("/models returned no usable model")
print(models[0]["id"])
')"

for index in $(seq 1 "$RUNS"); do
  payload="$(python3 - "$model" "$index" <<'PY'
import json, sys
model, index = sys.argv[1:]
print(json.dumps({
    "model": model,
    "messages": [{"role": "user", "content": f"请仅回复：A4000 Qwen 验收通过 #{index}"}],
    "max_tokens": 32,
    "temperature": 0,
}, ensure_ascii=False))
PY
)"
  response="$(curl --fail --silent --show-error --max-time 120 \
    -H 'Content-Type: application/json' \
    -d "$payload" "$API_BASE/chat/completions")"
  printf '%s' "$response" | python3 - "$index" <<'PY'
import json, sys
index = sys.argv[1]
payload = json.load(sys.stdin)
try:
    content = payload["choices"][0]["message"]["content"]
except (KeyError, IndexError, TypeError) as exc:
    raise SystemExit(f"run {index}: malformed completion: {exc}")
if not isinstance(content, str) or not content.strip():
    raise SystemExit(f"run {index}: empty completion")
PY
  echo "[OK] Qwen completion $index/$RUNS"
done

report="$REPORT_DIR/qwen-vllm-$(date -u +%Y%m%dT%H%M%SZ).json"
python3 - "$report" "$model" "$RUNS" "$API_BASE" <<'PY'
import json, sys
from datetime import datetime, timezone
path, model, runs, api_base = sys.argv[1:]
with open(path, "w", encoding="utf-8") as handle:
    json.dump({
        "suite": "qwen-vllm-loopback-acceptance",
        "passed": True,
        "model": model,
        "runs": int(runs),
        "api_base": api_base,
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }, handle, ensure_ascii=False, indent=2)
    handle.write("\n")
PY
echo "[PASS] Qwen vLLM accepted: $RUNS/$RUNS non-empty completions; report=$report"
