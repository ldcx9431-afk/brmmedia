#!/usr/bin/env bash
# Small, dependency-light watchdog for the WSL runtime. Run it from the
# accompanying systemd timer; it only restarts a component after several
# consecutive failed checks, preventing a temporary model warm-up from
# becoming a restart storm.
set -euo pipefail

STATE_DIR="${BRMMEDIA_HEALTHCHECK_STATE_DIR:-/var/lib/brmmedia}"
REPORT_FILE="${BRMMEDIA_HEALTHCHECK_REPORT_FILE:-/srv/brmmedia/artifacts/service-health.txt}"
FAIL_FILE="$STATE_DIR/healthcheck-failures"
MEDIA_FAIL_FILE="$STATE_DIR/healthcheck-media-failures"
NGINX_FAIL_FILE="$STATE_DIR/healthcheck-nginx-failures"
QWEN_FAIL_FILE="$STATE_DIR/healthcheck-qwen-failures"
RESTART_AFTER="${BRMMEDIA_HEALTHCHECK_RESTART_AFTER:-3}"
HTTP_TIMEOUT="${BRMMEDIA_HEALTHCHECK_HTTP_TIMEOUT:-10}"

mkdir -p "$STATE_DIR" "$(dirname "$REPORT_FILE")"

service_state() {
  systemctl is-active "$1" 2>/dev/null || true
}

http_code() {
  curl --silent --output /dev/null --write-out '%{http_code}' --max-time "$HTTP_TIMEOUT" "$1" || true
}

media_queue_active() {
  # Never restart ComfyUI/Gradio merely because H3 is loading or generating.
  # /queue is local-only and its lists are non-empty while work is active.
  local queue
  queue="$(curl --silent --max-time 5 http://127.0.0.1:8188/queue 2>/dev/null || true)"
  printf '%s' "$queue" | grep -Eq '"queue_(running|pending)"[[:space:]]*:[[:space:]]*\[[[:space:]]*[^][:space:]]'
}

is_ok() {
  [ "$1" = "active" ]
}

read_failures() {
  local fail_file="$1"
  local failures=0
  if [ -r "$fail_file" ]; then
    read -r failures < "$fail_file" || failures=0
  fi
  case "$failures" in
    ''|*[!0-9]*) failures=0 ;;
  esac
  printf '%s' "$failures"
}

write_failures() {
  printf '%s\n' "$2" > "$1"
}

backend_service="baorong-backend"
active_env=/etc/brmmedia/qwen-active.env
if [ -r "$active_env" ]; then
  # shellcheck disable=SC1090
  source "$active_env"
fi
qwen_model="${QWEN_ACTIVE_MODEL:-qwen35-4b-awq}"
qwen_port="${QWEN_ACTIVE_PORT:-8000}"
qwen_upstream="${QWEN_ACTIVE_UPSTREAM:-http://127.0.0.1:8000/}"
if [ "$qwen_model" = "qwen38-27b-ud-q4-xl" ]; then
  qwen_service="windows-qwen38-task"
  mode="a5000-media-dual-a4000-qwen38"
else
  qwen_service="qwen-vllm"
  mode="a5000-media-a4000-qwen35"
fi
qwen_expected=true
nginx_state="$(service_state nginx)"

backend_state="$(service_state "$backend_service")"
qwen_state="$(service_state "$qwen_service")"
gradio_code="$(http_code http://127.0.0.1:9000/gradio_api/info)"
comfy_code="$(http_code http://127.0.0.1:8188/system_stats)"
qwen_code="000"
if [ "$qwen_expected" = true ]; then
  qwen_code="$(http_code "${qwen_upstream%/}/v1/models")"
fi

media_problem=false
nginx_problem=false
qwen_problem=false
problems=()
if ! is_ok "$backend_state" || [ "$gradio_code" != "200" ] || [ "$comfy_code" != "200" ]; then
  media_problem=true
  is_ok "$backend_state" || problems+=("$backend_service=$backend_state")
  [ "$gradio_code" = "200" ] || problems+=("gradio_http=$gradio_code")
  [ "$comfy_code" = "200" ] || problems+=("comfy_http=$comfy_code")
fi
if ! is_ok "$nginx_state"; then
  nginx_problem=true
  problems+=("nginx=$nginx_state")
fi
if [ "$qwen_expected" = true ] && { { [ "$qwen_service" != "windows-qwen38-task" ] && ! is_ok "$qwen_state"; } || [ "$qwen_code" != "200" ]; }; then
  qwen_problem=true
  if [ "$qwen_service" != "windows-qwen38-task" ]; then
    is_ok "$qwen_state" || problems+=("$qwen_service=$qwen_state")
  fi
  [ "$qwen_code" = "200" ] || problems+=("qwen_http=$qwen_code")
fi

media_failures="$(read_failures "$MEDIA_FAIL_FILE")"
nginx_failures="$(read_failures "$NGINX_FAIL_FILE")"
qwen_failures="$(read_failures "$QWEN_FAIL_FILE")"
restarted="none"

if [ "$media_problem" = true ]; then
  media_failures=$((media_failures + 1))
  if [ "$media_failures" -ge "$RESTART_AFTER" ]; then
    if media_queue_active; then
      restarted="deferred-active-media"
    else
      systemctl restart "$backend_service"
      restarted="$backend_service"
      media_failures=0
    fi
  fi
else
  media_failures=0
fi

if [ "$nginx_problem" = true ]; then
  nginx_failures=$((nginx_failures + 1))
  if [ "$nginx_failures" -ge "$RESTART_AFTER" ]; then
    systemctl restart nginx
    restarted="$restarted,nginx"
    nginx_failures=0
  fi
else
  nginx_failures=0
fi

if [ "$qwen_problem" = true ]; then
  qwen_failures=$((qwen_failures + 1))
  if [ "$qwen_failures" -ge "$RESTART_AFTER" ]; then
    if [ "$qwen_service" = "windows-qwen38-task" ]; then
      restarted="$restarted,windows-qwen38-manual-check-required"
    else
      systemctl restart "$qwen_service"
      restarted="$restarted,$qwen_service"
    fi
    qwen_failures=0
  fi
else
  qwen_failures=0
fi

failures="$media_failures"
if [ "$nginx_failures" -gt "$failures" ]; then failures="$nginx_failures"; fi
if [ "$qwen_failures" -gt "$failures" ]; then failures="$qwen_failures"; fi
write_failures "$MEDIA_FAIL_FILE" "$media_failures"
write_failures "$NGINX_FAIL_FILE" "$nginx_failures"
write_failures "$QWEN_FAIL_FILE" "$qwen_failures"
# Retain the aggregate file for existing operational tooling.
write_failures "$FAIL_FILE" "$failures"

report_tmp="${REPORT_FILE}.tmp"
{
  printf 'timestamp=%s\n' "$(date --iso-8601=seconds)"
  printf 'mode=%s\n' "$mode"
  printf 'backend_service=%s\n' "$backend_service"
  printf 'backend_state=%s\n' "$backend_state"
  printf 'nginx_state=%s\n' "$nginx_state"
  printf 'qwen_state=%s\n' "$qwen_state"
  printf 'qwen_model=%s\n' "$qwen_model"
  printf 'qwen_upstream=%s\n' "$qwen_upstream"
  printf 'gradio_http=%s\n' "$gradio_code"
  printf 'comfy_http=%s\n' "$comfy_code"
  printf 'qwen_http=%s\n' "$qwen_code"
  printf 'consecutive_failures=%s\n' "$failures"
  printf 'media_consecutive_failures=%s\n' "$media_failures"
  printf 'nginx_consecutive_failures=%s\n' "$nginx_failures"
  printf 'qwen_consecutive_failures=%s\n' "$qwen_failures"
  printf 'restart_action=%s\n' "$restarted"
  printf 'media_queue_active=%s\n' "$(media_queue_active && echo true || echo false)"
  printf 'problems=%s\n' "${problems[*]:-none}"
} > "$report_tmp"
mv -f "$report_tmp" "$REPORT_FILE"

if [ "${#problems[@]}" -ne 0 ]; then
  printf '[WARN] BRMMedia healthcheck: %s (failure %s/%s; restart=%s)\n' \
    "${problems[*]}" "$failures" "$RESTART_AFTER" "$restarted" >&2
  exit 1
fi

printf '[OK] BRMMedia healthcheck: mode=%s\n' "$mode"
