#!/usr/bin/env bash
# Runtime acceptance for the local MiniMax H3 T2V/I2V API.  The service must
# already be activated; this script never changes service configuration.
set -euo pipefail

API_BASE="${BRMMEDIA_LAN_API_BASE:-http://127.0.0.1:9100/api/v1}"
POLL_SECONDS="${BRMMEDIA_H3_POLL_SECONDS:-10}"
TIMEOUT_SECONDS="${BRMMEDIA_H3_TASK_TIMEOUT_SECONDS:-14400}"
FULL=0
RESTART_RECOVERY=0
QUALITY_I2V_ONLY=0

for argument in "$@"; do
  case "$argument" in
    --full) FULL=1 ;;
    --restart-recovery) RESTART_RECOVERY=1 ;;
    --quality-i2v-only) QUALITY_I2V_ONLY=1 ;;
    *) echo "usage: $0 [--full] [--restart-recovery] [--quality-i2v-only]" >&2; exit 2 ;;
  esac
done
if [ "$QUALITY_I2V_ONLY" -eq 1 ] && { [ "$FULL" -eq 1 ] || [ "$RESTART_RECOVERY" -eq 1 ]; }; then
  echo "[ERROR] --quality-i2v-only cannot be combined with --full or --restart-recovery." >&2
  exit 2
fi

for command in curl python3 ffprobe base64; do
  command -v "$command" >/dev/null 2>&1 || {
    echo "[ERROR] Required command is unavailable: $command" >&2
    exit 1
  }
done
if ! [[ "$POLL_SECONDS" =~ ^[0-9]+$ ]] || [ "$POLL_SECONDS" -lt 2 ]; then
  echo "[ERROR] BRMMEDIA_H3_POLL_SECONDS must be an integer >= 2." >&2
  exit 1
fi
if [ "$RESTART_RECOVERY" -eq 1 ] && [ "$(id -u)" -ne 0 ]; then
  echo "[ERROR] --restart-recovery restarts a systemd backend; run this option with sudo." >&2
  exit 2
fi

work_dir="$(mktemp -d /tmp/brmmedia-h3-accept.XXXXXX)"
trap 'rm -rf "$work_dir"' EXIT

api_get() {
  curl --fail --silent --show-error "$1"
}

json_get() {
  local path="$1"
  python3 -c '
import json, sys
value = json.load(sys.stdin)
for key in sys.argv[1].split("."):
    value = value[int(key)] if key.isdigit() else value[key]
print(value)
' "$path"
}

submit_task() {
  local workflow="$1" payload="$2" response
  response="$(curl --fail --silent --show-error \
    -H 'Content-Type: application/json' \
    -d "{\"workflow\":\"$workflow\",\"params\":$payload}" \
    "$API_BASE/tasks")"
  printf '%s' "$response" | json_get task_id
}

wait_for_task() {
  local task_id="$1" label="$2" started now response state
  started="$(date +%s)"
  while :; do
    response="$(api_get "$API_BASE/tasks/$task_id")"
    state="$(printf '%s' "$response" | json_get state)"
    case "$state" in
      completed)
        printf '%s' "$response" > "$work_dir/$task_id.json"
        echo "[OK] $label completed: $task_id"
        return 0
        ;;
      failed|cancelled|interrupted|timed_out)
        echo "[ERROR] $label ended as $state: $response" >&2
        return 1
        ;;
    esac
    now="$(date +%s)"
    if [ $((now - started)) -gt "$TIMEOUT_SECONDS" ]; then
      echo "[ERROR] $label exceeded ${TIMEOUT_SECONDS}s: $task_id" >&2
      return 1
    fi
    sleep "$POLL_SECONDS"
  done
}

verify_effective_h3_settings() {
  local task_id="$1" label="$2" profile="$3" seconds="$4" result
  result="$(<"$work_dir/$task_id.json")"
  H3_TASK_JSON="$result" python3 - "$label" "$profile" "$seconds" <<'PY'
import json, math, os, sys
label, expected_profile, expected_seconds = sys.argv[1], sys.argv[2], int(sys.argv[3])
payload = json.loads(os.environ["H3_TASK_JSON"])
settings = payload.get("effective_settings")
if not isinstance(settings, dict):
    raise SystemExit(f"[ERROR] {label} omitted effective_settings: {payload}")
if settings.get("profile") != expected_profile:
    raise SystemExit(f"[ERROR] {label} profile mismatch: {settings}")
if settings.get("requested_seconds") != expected_seconds:
    raise SystemExit(f"[ERROR] {label} requested_seconds mismatch: {settings}")
width, height, frames = settings.get("width"), settings.get("height"), settings.get("frames")
if not all(isinstance(value, int) for value in (width, height, frames)):
    raise SystemExit(f"[ERROR] {label} has invalid effective canvas/frame values: {settings}")
if width < 32 or height < 32 or width % 32 or height % 32 or frames < 5 or frames % 17 != 5:
    raise SystemExit(f"[ERROR] {label} violates the H3 canvas/frame grid: {settings}")
expected_effective = round(frames / 24, 3)
if not math.isclose(float(settings.get("effective_seconds")), expected_effective, abs_tol=0.001):
    raise SystemExit(f"[ERROR] {label} has inconsistent effective duration: {settings}")
if expected_profile == "draft" and frames != 73:
    raise SystemExit(f"[ERROR] {label} draft must use the official 73-frame profile: {settings}")
print(f"[OK] {label} effective H3 settings: {width}x{height}, {frames} frames, {expected_effective}s")
PY
}

verify_native_audio_mp4() {
  local task_id="$1" label="$2" profile="$3" seconds="$4" result artifact_fields filename_b64 artifact_url_b64 extra_field filename artifact_url artifact streams audio_channels container_formats
  result="$(<"$work_dir/$task_id.json")"
  verify_effective_h3_settings "$task_id" "$label" "$profile" "$seconds"
  # Do not split the artifact record on shell whitespace.  Completed task
  # names are user-visible and may contain spaces (including alongside
  # Chinese characters), while download_url is normally a relative,
  # percent-encoded API path.  Emit both fields as base64 tokens so a filename
  # can never shift the URL into an unsupported position.
  artifact_fields="$(printf '%s' "$result" | python3 -c '
import base64
import json, sys
payload = json.load(sys.stdin)
artifacts = payload.get("artifacts")
if not isinstance(artifacts, list) or not artifacts:
    raise SystemExit("task response has no downloadable artifacts")
first = artifacts[0]
if not isinstance(first, dict) or not isinstance(first.get("name"), str) or not isinstance(first.get("download_url"), str):
    raise SystemExit("task response has an invalid artifact record")
for value in (first["name"], first["download_url"]):
    print(base64.b64encode(value.encode("utf-8")).decode("ascii"), end=" ")
')" || {
    echo "[ERROR] $label returned an invalid artifact record." >&2
    return 1
  }
  read -r filename_b64 artifact_url_b64 extra_field <<< "$artifact_fields"
  if [ -z "${filename_b64:-}" ] || [ -z "${artifact_url_b64:-}" ] || [ -n "${extra_field:-}" ]; then
    echo "[ERROR] $label returned malformed artifact fields." >&2
    return 1
  fi
  filename="$(printf '%s' "$filename_b64" | base64 -d)" || {
    echo "[ERROR] $label returned an undecodable artifact filename." >&2
    return 1
  }
  artifact_url="$(printf '%s' "$artifact_url_b64" | base64 -d)" || {
    echo "[ERROR] $label returned an undecodable artifact URL." >&2
    return 1
  }
  case "$artifact_url" in
    http://*|https://*) ;;
    /*) artifact_url="${API_BASE%/api/v1}$artifact_url" ;;
    *) echo "[ERROR] $label returned an unsupported artifact URL: $artifact_url" >&2; return 1 ;;
  esac
  case "$filename" in
    *.mp4|*.MP4) ;;
    *) echo "[ERROR] $label did not return an MP4 filename: $filename" >&2; return 1 ;;
  esac
  artifact="$work_dir/$task_id-$filename"
  curl --fail --silent --show-error "$artifact_url" -o "$artifact"
  container_formats="$(ffprobe -v error -show_entries format=format_name -of default=nokey=1:noprint_wrappers=1 "$artifact")"
  if ! printf '%s\n' "$container_formats" | grep -Eq '(^|,)mp4(,|$)'; then
    echo "[ERROR] $label is not in an MP4 container: $container_formats" >&2
    return 1
  fi
  streams="$(ffprobe -v error -show_entries stream=codec_type -of csv=p=0 "$artifact" | sort -u)"
  if ! printf '%s\n' "$streams" | grep -qx video || ! printf '%s\n' "$streams" | grep -qx audio; then
    echo "[ERROR] $label is not a native-audio MP4: $streams" >&2
    return 1
  fi
  audio_channels="$(ffprobe -v error -select_streams a:0 -show_entries stream=channels -of default=nokey=1:noprint_wrappers=1 "$artifact")"
  if [ "$audio_channels" != "2" ]; then
    echo "[ERROR] $label audio is not stereo (channels=$audio_channels): $filename" >&2
    return 1
  fi
  echo "[OK] $label MP4 has video + stereo audio streams: $filename"
}

make_fixture_image() {
  # A standards-compliant 64x64 RGB PNG.  The 1x1 grayscale fixture previously
  # used here is legal PNG, but recent ComfyUI/PyAV builds may reject it while
  # opening image inputs through their video abstraction.  H3 expands this
  # fixture to the selected canvas; it remains local and is removed by the
  # trap, so I2V acceptance has no user asset effect.
  base64 -d > "$work_dir/h3-fixture.png" <<'PNG'
iVBORw0KGgoAAAANSUhEUgAAAEAAAABACAIAAAAlC+aJAAAAeklEQVR4nO3PUQkAIBTAwFfIkkY0kSH8OITBAtxm7fN1wwUNaEEDWtCAFjSgBQ1oQQNa0IAWNKAFDWhBA1rQgBY0oAUNaEEDWtCAFjSgBQ1oQQNa0IAWNKAFDWhBA1rQgBY0oAUNaEEDWtCAFjSgBQ1oQQNa0IAWPHYB5TcRaViDW+IAAAAASUVORK5CYII=
PNG
}

verify_runtime_acceleration_evidence() {
  local log_file="${BRMMEDIA_COMFY_LOG_FILE:-./ubuntu-backend-deploy/comfyui_runtime.log}"
  [ -f "$log_file" ] || {
    echo "[ERROR] ComfyUI runtime log is unavailable: $log_file" >&2
    return 1
  }
  if ! grep -Eqi 'cuda backend.*enabled|native ops:.*(convrot|nvfp4)' "$log_file"; then
    echo "[ERROR] CUDA quantization backend evidence is missing from $log_file" >&2
    return 1
  fi
  if ! grep -Eqi 'sage.?attention' "$log_file"; then
    echo "[ERROR] Sage Attention evidence is missing from $log_file" >&2
    return 1
  fi
  echo "[OK] Runtime log records CUDA quantization and Sage Attention evidence."
}

upload_fixture_image() {
  local response
  make_fixture_image
  response="$(curl --fail --silent --show-error -X POST \
    -F "file=@$work_dir/h3-fixture.png;type=image/png" \
    "$API_BASE/files?kind=image")"
  printf '%s' "$response" | json_get asset_id
}

verify_completed_task_after_restart() {
  local task_id="$1" label="$2" profile="$3" seconds="$4"
  local backend_unit="${BRMMEDIA_BACKEND_UNIT:-baorong-backend}"
  local gradio_health_url="${BRMMEDIA_RESTART_GRADIO_HEALTH_URL:-http://127.0.0.1:9000/gradio_api/info}"
  local timeout_seconds="${BRMMEDIA_RESTART_RECOVERY_TIMEOUT_SECONDS:-420}"
  local deadline response state health_code

  if ! [[ "$timeout_seconds" =~ ^[1-9][0-9]*$ ]]; then
    echo "[ERROR] BRMMEDIA_RESTART_RECOVERY_TIMEOUT_SECONDS must be a positive integer." >&2
    return 2
  fi
  echo "[INFO] Restarting $backend_unit to verify persisted H3 task recovery..."
  systemctl restart "$backend_unit"
  deadline=$((SECONDS + timeout_seconds))
  while :; do
    health_code="$(curl --silent --output /dev/null --write-out '%{http_code}' --max-time 15 \
      "$gradio_health_url" || true)"
    if [ "$health_code" = "200" ]; then
      break
    fi
    if [ "$SECONDS" -ge "$deadline" ] || ! systemctl is-active --quiet "$backend_unit"; then
      echo "[ERROR] $backend_unit did not recover its Gradio endpoint ($health_code)." >&2
      return 1
    fi
    sleep 3
  done
  # The task endpoint is supplied by the still-running loopback LAN bridge.
  # Re-fetch the original task after webui.py reloads its durable history,
  # then repeat artifact download/container/audio validation from that record.
  response="$(api_get "$API_BASE/tasks/$task_id")"
  state="$(printf '%s' "$response" | json_get state)"
  if [ "$state" != "completed" ]; then
    echo "[ERROR] $label was not restored as completed after restart: $response" >&2
    return 1
  fi
  printf '%s' "$response" > "$work_dir/$task_id.json"
  verify_native_audio_mp4 "$task_id" "$label after restart" "$profile" "$seconds"
  echo "[OK] $label persisted across $backend_unit restart."
}

health="$(api_get "$API_BASE/health")"
if [ "$(printf '%s' "$health" | json_get status)" != "ok" ]; then
  echo "[ERROR] LAN API is not healthy: $health" >&2
  exit 1
fi
if [ "${BRMMEDIA_REQUIRE_RUNTIME_ACCELERATION_EVIDENCE:-0}" = "1" ]; then
  verify_runtime_acceleration_evidence
else
  echo "[WARN] CUDA/Sage runtime-log evidence check is disabled; enable BRMMEDIA_REQUIRE_RUNTIME_ACCELERATION_EVIDENCE=1 for candidate/production acceptance."
fi

image_asset_id="$(upload_fixture_image)"
if [ "$QUALITY_I2V_ONLY" -eq 1 ]; then
  # This focused gate is intentionally available for recovery from an
  # interrupted full burn-in.  It still submits through the public REST
  # contract and verifies the returned effective settings, downloadable MP4,
  # video stream and native stereo audio.  It never substitutes for a clean
  # full run unless the earlier preview/T2V-quality evidence is retained.
  quality_i2v="$(submit_task image-to-video "{\"image_asset_id\":\"$image_asset_id\",\"prompt\":\"A gentle cinematic movement with synchronized natural ambience.\",\"size\":\"1344 × 768\",\"seconds\":6,\"profile\":\"quality\"}")"
  wait_for_task "$quality_i2v" "image-to-video quality recovery"
  verify_native_audio_mp4 "$quality_i2v" "image-to-video quality recovery" quality 6
  echo "[PASS] MiniMax H3 quality I2V recovery acceptance passed."
  exit 0
fi
preview_runs=1
if [ "$FULL" -eq 1 ]; then preview_runs=3; fi

for index in $(seq 1 "$preview_runs"); do
  t2v="$(submit_task text-to-video '{"prompt":"A small paper boat gently moves across a quiet blue pond. Natural ripples and synchronized soft water ambience.","size":"1344 × 768","seconds":4,"profile":"preview"}')"
  wait_for_task "$t2v" "T2V preview #$index"
  verify_native_audio_mp4 "$t2v" "T2V preview #$index" preview 4
  if [ "$RESTART_RECOVERY" -eq 1 ] && [ "$index" -eq 1 ]; then
    verify_completed_task_after_restart "$t2v" "T2V preview #$index" preview 4
  fi

  i2v="$(submit_task image-to-video "{\"image_asset_id\":\"$image_asset_id\",\"prompt\":\"The image comes alive with a subtle camera push-in and synchronized gentle ambient sound.\",\"size\":\"1344 × 768\",\"seconds\":4,\"profile\":\"preview\"}")"
  wait_for_task "$i2v" "I2V preview #$index"
  verify_native_audio_mp4 "$i2v" "I2V preview #$index" preview 4
done

if [ "$FULL" -eq 1 ]; then
  for workflow in text-to-video image-to-video; do
    if [ "$workflow" = image-to-video ]; then
      payload="{\"image_asset_id\":\"$image_asset_id\",\"prompt\":\"A gentle cinematic movement with synchronized natural ambience.\",\"size\":\"1344 × 768\",\"seconds\":6,\"profile\":\"quality\"}"
    else
      payload='{"prompt":"A calm cinematic landscape with synchronized natural ambience.","size":"1344 × 768","seconds":6,"profile":"quality"}'
    fi
    task_id="$(submit_task "$workflow" "$payload")"
    wait_for_task "$task_id" "$workflow quality"
    verify_native_audio_mp4 "$task_id" "$workflow quality" quality 6
  done
fi

echo "[PASS] MiniMax H3 $([ "$FULL" -eq 1 ] && echo full || echo quick) video acceptance passed."
