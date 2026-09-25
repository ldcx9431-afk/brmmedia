#!/usr/bin/env bash
# Technical acceptance for the isolated IndexTTS-2.5 candidate.
# Usage: sudo -u brm ./accept_indextts25_candidate.sh <reference-audio-1> <reference-audio-2>
set -euo pipefail

if [[ $# -ne 2 ]]; then
  echo "usage: $0 <reference-audio-1> <reference-audio-2>" >&2
  exit 2
fi
for reference in "$@"; do
  [[ -f "$reference" ]] || { echo "[ERROR] Missing reference audio: $reference" >&2; exit 2; }
done
command -v curl >/dev/null || { echo "[ERROR] curl is required" >&2; exit 1; }
command -v ffprobe >/dev/null || { echo "[ERROR] ffprobe is required" >&2; exit 1; }

APP_ROOT="${BRMMEDIA_APP_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
ENV_FILE="${BRMMEDIA_INDEXTTS25_ENV_FILE:-$APP_ROOT/ubuntu-backend-deploy/.env.indextts25}"
[[ -r "$ENV_FILE" ]] || { echo "[ERROR] Candidate environment is not prepared: $ENV_FILE" >&2; exit 1; }
set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a
BASE="http://${INDEXTTS25_HOST:-127.0.0.1}:${INDEXTTS25_PORT:-9205}"
REPORT_DIR="${INDEXTTS25_ACCEPTANCE_DIR:-$APP_ROOT/runtime-locks/indextts25-v2.5/reports}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
WORK_DIR="$REPORT_DIR/acceptance-$STAMP"
mkdir -p "$WORK_DIR"

health="$(curl --fail --silent --show-error "$BASE/health")"
python3 - "$health" <<'PY'
import json, sys
payload = json.loads(sys.argv[1])
assert payload.get('ok') is True, payload
assert payload.get('engine') == 'IndexTTS-2.5', payload
assert payload.get('bf16') is True, payload
assert payload.get('cuda_kernel_requested') is True, payload
assert payload.get('deepspeed') is False, payload
assert payload.get('port_boundary') == 'loopback-only', payload
PY

declare -a LANGUAGES=(zh en ja es ar)
declare -a TEXTS=(
  '这是 IndexTTS 二点五中文验收语音，请确认音色清晰自然。'
  'This is an IndexTTS two point five English acceptance sample.'
  'これは IndexTTS 二点五の日本語音声テストです。'
  'Esta es una muestra de aceptación en español de IndexTTS dos punto cinco.'
  'هذا نموذج قبول صوتي باللغة العربية من IndexTTS اثنان فاصلة خمسة.'
)

results="$WORK_DIR/results.jsonl"
for index in "${!LANGUAGES[@]}"; do
  lang="${LANGUAGES[$index]}"
  text="${TEXTS[$index]}"
  reference="${1}"
  (( index % 2 == 1 )) && reference="${2}"
  output="$WORK_DIR/${lang}.wav"
  before="$(date +%s)"
  curl --fail --silent --show-error \
    -F "text=$text" -F "language=$lang" -F 'speed=1.0' \
    -F "reference_audio=@$reference" "$BASE/v1/voice-clone" >"$output"
  after="$(date +%s)"
  streams="$(ffprobe -v error -select_streams a:0 -show_entries stream=codec_type,sample_rate,duration -of json "$output")"
  python3 - "$lang" "$output" "$((after-before))" "$streams" >>"$results" <<'PY'
import json, sys
lang, path, elapsed, streams = sys.argv[1:]
data = json.loads(streams)
stream = data.get('streams', [{}])[0]
assert stream.get('codec_type') == 'audio', data
assert str(stream.get('sample_rate')) == '22050', data
assert float(stream.get('duration', 0)) > 0, data
print(json.dumps({'language': lang, 'file': path, 'elapsed_seconds': int(elapsed), 'audio': stream}, ensure_ascii=False))
PY
done

python3 - "$health" "$results" "$WORK_DIR/report.json" <<'PY'
import json, sys
health, lines, destination = sys.argv[1:]
payload = {'health': json.loads(health), 'runs': [json.loads(line) for line in open(lines, encoding='utf-8') if line.strip()]}
open(destination, 'w', encoding='utf-8').write(json.dumps(payload, ensure_ascii=False, indent=2) + '\n')
PY
printf '[OK] IndexTTS-2.5 multilingual technical acceptance complete: %s\n' "$WORK_DIR/report.json"
printf '     Listen to the five WAV files before setting BRMMEDIA_VOICE_ENGINE=indextts25.\n'
