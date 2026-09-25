#!/usr/bin/env bash
# Production media regression after the H3 canary and production activation.
# It covers the retained non-H3 ComfyUI flows that should remain available on
# A5000: Z-Image, FLUX image edit, ACE-Step, IndexTTS2, first/last-frame LTX
# and audio-driven talking head.  All REST calls remain loopback-only.
set -euo pipefail

RUNTIME_ENV_FILE="${BRMMEDIA_RUNTIME_ENV_FILE:-/etc/brmmedia/runtime.env}"
runtime_env_value() {
  local key="$1"
  [ -r "$RUNTIME_ENV_FILE" ] || return 0
  sed -n "s/^${key}=//p" "$RUNTIME_ENV_FILE" | tail -n1
}

APP_ROOT="${BRMMEDIA_APP_ROOT:-$(runtime_env_value BRMMEDIA_APP_ROOT)}"
APP_ROOT="${APP_ROOT:-/srv/brmmedia/app}"
BACKEND_DIR="$APP_ROOT/ubuntu-backend-deploy"
API_BASE="${BRMMEDIA_LAN_API_BASE:-http://127.0.0.1:9100/api/v1}"
OUTPUT_DIR="${BRMMEDIA_REGRESSION_OUTPUT_DIR:-/srv/brmmedia/outputs}"
POLL_SECONDS="${BRMMEDIA_MEDIA_REGRESSION_POLL_SECONDS:-10}"
TIMEOUT_SECONDS="${BRMMEDIA_MEDIA_REGRESSION_TIMEOUT_SECONDS:-5400}"
REPORT_DIR="${BRMMEDIA_ACCEPTANCE_REPORT_DIR:-$BACKEND_DIR/outputs/acceptance}"

for command in curl python3 ffprobe date find; do
  command -v "$command" >/dev/null 2>&1 || {
    echo "[ERROR] Required command is unavailable: $command" >&2
    exit 1
  }
done
if [ ! -x "$APP_ROOT/run_smoke_z_image.sh" ] || [ ! -x "$APP_ROOT/run_smoke_music.sh" ] || [ ! -x "$APP_ROOT/run_smoke_tts.sh" ]; then
  echo "[ERROR] Candidate runtime is missing a media smoke script: $APP_ROOT" >&2
  exit 1
fi
if ! [[ "$POLL_SECONDS" =~ ^[0-9]+$ ]] || [ "$POLL_SECONDS" -lt 2 ] || ! [[ "$TIMEOUT_SECONDS" =~ ^[1-9][0-9]*$ ]]; then
  echo "[ERROR] Invalid media regression polling/timeout settings." >&2
  exit 2
fi

work_dir="$(mktemp -d /tmp/brmmedia-media-regression.XXXXXX)"
trap 'rm -rf "$work_dir"' EXIT
mkdir -p "$REPORT_DIR"

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

api_get() {
  curl --fail --silent --show-error "$1"
}

upload_asset() {
  local kind="$1" file="$2" response
  response="$(curl --fail --silent --show-error -X POST -F "file=@$file" "$API_BASE/files?kind=$kind")"
  printf '%s' "$response" | json_get asset_id
}

submit_task() {
  local workflow="$1" payload="$2" response
  response="$(curl --fail --silent --show-error -H 'Content-Type: application/json' \
    -d "{\"workflow\":\"$workflow\",\"params\":$payload}" "$API_BASE/tasks")"
  printf '%s' "$response" | json_get task_id
}

wait_and_verify_task() {
  local task_id="$1" label="$2" expected_kind="$3" started now response state artifact_url artifact streams
  started="$(date +%s)"
  while :; do
    response="$(api_get "$API_BASE/tasks/$task_id")"
    state="$(printf '%s' "$response" | json_get state)"
    case "$state" in
      completed) break ;;
      failed|cancelled|interrupted)
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
  printf '%s' "$response" > "$work_dir/$task_id.json"
  artifact_url="$(printf '%s' "$response" | python3 -c '
import json, sys
payload = json.load(sys.stdin)
items = payload.get("artifacts") or []
if not items or not isinstance(items[0], dict) or not isinstance(items[0].get("download_url"), str):
    raise SystemExit("no downloadable artifact")
print(items[0]["download_url"])
')"
  case "$artifact_url" in
    http://*|https://*) ;;
    /*) artifact_url="${API_BASE%/api/v1}$artifact_url" ;;
    *) echo "[ERROR] $label returned an unsupported artifact URL: $artifact_url" >&2; return 1 ;;
  esac
  artifact="$work_dir/$task_id.artifact"
  curl --fail --silent --show-error "$artifact_url" -o "$artifact"
  if [ ! -s "$artifact" ]; then
    echo "[ERROR] $label artifact is empty." >&2
    return 1
  fi
  if [ "$expected_kind" = "video" ]; then
    streams="$(ffprobe -v error -show_entries stream=codec_type -of csv=p=0 "$artifact" | sort -u)"
    if ! printf '%s\n' "$streams" | grep -qx video; then
      echo "[ERROR] $label artifact has no video stream: $streams" >&2
      return 1
    fi
  fi
  echo "[OK] $label completed with downloadable artifact: $task_id"
}

health="$(api_get "$API_BASE/health")"
if [ "$(printf '%s' "$health" | json_get status)" != "ok" ]; then
  echo "[ERROR] LAN API is not healthy: $health" >&2
  exit 1
fi

echo "[1/6] Z-Image regression..."
"$APP_ROOT/run_smoke_z_image.sh"
image="$(find "$OUTPUT_DIR" -maxdepth 1 -type f \( -name 'smoke_z_*.png' -o -name 'smoke_z_*.jpg' -o -name 'smoke_z_*.webp' \) -printf '%T@ %p\n' 2>/dev/null | sort -nr | head -n1 | cut -d' ' -f2-)"
if [ -z "$image" ] || [ ! -f "$image" ]; then
  echo "[ERROR] Z-Image smoke did not produce a reusable image in $OUTPUT_DIR." >&2
  exit 1
fi

echo "[2/6] ACE-Step music regression..."
"$APP_ROOT/run_smoke_music.sh"
audio="$(find "$OUTPUT_DIR" -maxdepth 1 -type f \( -name 'smoke_music_*.mp3' -o -name 'smoke_music_*.wav' \) -printf '%T@ %p\n' 2>/dev/null | sort -nr | head -n1 | cut -d' ' -f2-)"
if [ -z "$audio" ] || [ ! -f "$audio" ]; then
  echo "[ERROR] ACE-Step smoke did not produce reusable audio in $OUTPUT_DIR." >&2
  exit 1
fi

echo "[3/6] IndexTTS2 voice-clone regression..."
"$APP_ROOT/run_smoke_tts.sh"
tts_audio="$(find "$OUTPUT_DIR" -maxdepth 1 -type f \( -name 'smoke_tts_*.wav' -o -name 'smoke_tts_*.mp3' \) -printf '%T@ %p\n' 2>/dev/null | sort -nr | head -n1 | cut -d' ' -f2-)"
if [ -z "$tts_audio" ] || [ ! -f "$tts_audio" ]; then
  echo "[ERROR] IndexTTS2 smoke did not produce reusable audio in $OUTPUT_DIR." >&2
  exit 1
fi

image_asset="$(upload_asset image "$image")"
audio_asset="$(upload_asset audio "$tts_audio")"
audio_duration="$(ffprobe -v error -show_entries format=duration -of default=nokey=1:noprint_wrappers=1 "$tts_audio")"
if ! python3 - "$audio_duration" <<'PY'
import sys
raise SystemExit(0 if float(sys.argv[1]) > 0 else 1)
PY
then
  echo "[ERROR] TTS smoke audio has no usable duration." >&2
  exit 1
fi

echo "[4/6] FLUX.2-klein image-edit REST regression..."
task="$(submit_task image-edit "{\"image_asset_id\":\"$image_asset\",\"prompt\":\"Preserve the composition and add a subtle warm cinematic color grade.\"}")"
wait_and_verify_task "$task" "FLUX image edit" image

echo "[5/6] LTX2.3 first/last-frame REST regression..."
task="$(submit_task first-last-frame-video "{\"first_image_asset_id\":\"$image_asset\",\"last_image_asset_id\":\"$image_asset\",\"prompt\":\"A gentle, natural camera move with stable composition.\",\"seconds\":2}")"
wait_and_verify_task "$task" "LTX first/last-frame video" video

echo "[6/6] LTX2.3 talking-head REST regression..."
task="$(submit_task talking-head "{\"image_asset_id\":\"$image_asset\",\"audio_asset_id\":\"$audio_asset\",\"duration\":$audio_duration,\"prompt\":\"Natural speaking movement, stable face and soft lighting.\",\"size\":\"512 × 512\"}")"
wait_and_verify_task "$task" "LTX talking head" video

report="$REPORT_DIR/media-regression-$(date -u +%Y%m%dT%H%M%SZ).json"
python3 - "$report" "$API_BASE" <<'PY'
import json, sys
from datetime import datetime, timezone
path, api_base = sys.argv[1:]
with open(path, "w", encoding="utf-8") as handle:
    json.dump({
        "suite": "a5000-media-regression",
        "passed": True,
        "api_base": api_base,
        "covered": ["text-to-image", "image-edit", "music-generate", "voice-clone", "first-last-frame-video", "talking-head"],
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }, handle, ensure_ascii=False, indent=2)
    handle.write("\n")
PY
echo "[PASS] A5000 media regression passed; report=$report"
