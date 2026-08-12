#!/usr/bin/env bash
# Capture a reproducible H3 T2V/I2V benchmark through the loopback REST API.
# It does not change model/runtime flags; each A/B candidate gets a separate
# report.  The caller must first start either a production or isolated canary.
set -euo pipefail

API_BASE="${BRMMEDIA_LAN_API_BASE:-http://127.0.0.1:9100/api/v1}"
REPORT_DIR="${BRMMEDIA_H3_BENCHMARK_DIR:-./ubuntu-backend-deploy/outputs/benchmarks}"
PROFILE="${BRMMEDIA_H3_BENCHMARK_PROFILE:-draft}"
RUNS="${BRMMEDIA_H3_BENCHMARK_RUNS:-3}"
POLL_SECONDS="${BRMMEDIA_H3_POLL_SECONDS:-10}"
TIMEOUT_SECONDS="${BRMMEDIA_H3_TASK_TIMEOUT_SECONDS:-14400}"
COMFY_LOG="${BRMMEDIA_COMFY_LOG_FILE:-./ubuntu-backend-deploy/comfyui_runtime.log}"

case "$PROFILE" in draft|preview|quality) ;; *) echo "[ERROR] profile must be draft, preview or quality" >&2; exit 2;; esac
case "$RUNS" in ''|*[!0-9]*) echo "[ERROR] runs must be a positive integer" >&2; exit 2;; esac
[ "$RUNS" -ge 1 ] || { echo "[ERROR] runs must be >= 1" >&2; exit 2; }
seconds=3; [ "$PROFILE" = draft ] || seconds=4
mkdir -p "$REPORT_DIR"
work="$(mktemp -d /tmp/brmmedia-h3-benchmark.XXXXXX)"
trap 'rm -rf "$work"' EXIT

json_value() {
  python3 -c 'import json,sys; value=json.load(sys.stdin); exec("for key in sys.argv[1].split(\".\"):\n    value = value[int(key)] if key.isdigit() else value[key]"); print(value)' "$1"
}
submit() { curl --fail --silent --show-error -H 'Content-Type: application/json' -d "$2" "$API_BASE/tasks" | json_value task_id; }
wait_task() {
  local id="$1" start now response state
  start="$(date +%s)"
  while :; do
    response="$(curl --fail --silent --show-error "$API_BASE/tasks/$id")"
    state="$(printf '%s' "$response" | json_value state)"
    case "$state" in completed) printf '%s' "$response"; return 0;; failed|cancelled|timed_out) echo "[ERROR] task $id ended as $state: $response" >&2; return 1;; esac
    now="$(date +%s)"; [ $((now-start)) -lt "$TIMEOUT_SECONDS" ] || { echo "[ERROR] task $id benchmark timeout" >&2; return 1; }
    sleep "$POLL_SECONDS"
  done
}

snapshot_gpu() {
  local smi="/usr/lib/wsl/lib/nvidia-smi"
  [ -x "$smi" ] || smi="$(command -v nvidia-smi || true)"
  [ -n "$smi" ] || { echo "nvidia-smi unavailable"; return 0; }
  "$smi" --query-gpu=index,name,utilization.gpu,memory.used,memory.total --format=csv,noheader 2>/dev/null || echo "nvidia-smi query failed"
}
before_log_lines=0; [ -f "$COMFY_LOG" ] && before_log_lines="$(wc -l < "$COMFY_LOG")"
results="$work/results.jsonl"
for kind in text-to-video image-to-video; do
  for n in $(seq 1 "$RUNS"); do
    payload="{\"workflow\":\"$kind\",\"params\":{\"prompt\":\"A small paper boat moves gently across a quiet blue pond with soft synchronized water ambience.\",\"size\":\"1344 × 768\",\"seconds\":$seconds,\"profile\":\"$PROFILE\"}}"
    start="$(date +%s)"; gpu_before="$(snapshot_gpu)"; id="$(submit "$kind" "$payload")"
    response="$(wait_task "$id")"; end="$(date +%s)"; gpu_after="$(snapshot_gpu)"
    python3 - "$results" "$kind" "$n" "$id" "$start" "$end" "$gpu_before" "$gpu_after" <<'PY'
import json, sys
path, kind, run, task_id, start, end, before, after = sys.argv[1:]
with open(path, "a", encoding="utf-8") as f:
    json.dump({"workflow":kind,"run":int(run),"task_id":task_id,"started":int(start),"ended":int(end),"elapsed_seconds":int(end)-int(start),"gpu_before":before,"gpu_after":after}, f, ensure_ascii=False)
    f.write("\n")
PY
  done
done
report="$REPORT_DIR/h3-${PROFILE}-$(date -u +%Y%m%dT%H%M%SZ).json"
python3 - "$results" "$report" "$PROFILE" "$seconds" "$COMFY_LOG" "$before_log_lines" <<'PY'
import json, os, sys
from datetime import datetime, timezone
source, target, profile, seconds, log, start_line = sys.argv[1:]
items=[json.loads(line) for line in open(source, encoding="utf-8")]
tail=[]
if os.path.exists(log):
    with open(log, encoding="utf-8", errors="replace") as f:
        tail=f.readlines()[int(start_line):]
keywords=("cuda backend", "sage", "prompt executed", "loaded partially")
evidence=[line.strip() for line in tail if any(k in line.lower() for k in keywords)][-200:]
payload={"suite":"h3-profile-benchmark","profile":profile,"requested_seconds":int(seconds),"runs":items,"log_evidence":evidence,"created_at":datetime.now(timezone.utc).isoformat()}
with open(target,"w",encoding="utf-8") as f: json.dump(payload,f,ensure_ascii=False,indent=2); f.write("\n")
print(target)
PY
echo "[OK] Benchmark report: $report"
