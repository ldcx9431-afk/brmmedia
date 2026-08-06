#!/usr/bin/env bash
# Small, dependency-light watchdog for the WSL runtime. Run it from the
# accompanying systemd timer; it only restarts a component after several
# consecutive failed checks, preventing a temporary model warm-up from
# becoming a restart storm.
set -euo pipefail

STATE_DIR="${BRMMEDIA_HEALTHCHECK_STATE_DIR:-/var/lib/brmmedia}"
REPORT_FILE="${BRMMEDIA_HEALTHCHECK_REPORT_FILE:-/srv/brmmedia/artifacts/service-health.txt}"
FAIL_FILE="$STATE_DIR/healthcheck-failures"
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

backend_service="baorong-backend"
mode="a5000-media-a4000-qwen"
qwen_expected=true
nginx_state="$(service_state nginx)"

backend_state="$(service_state "$backend_service")"
qwen_state="$(service_state qwen-vllm)"
gradio_code="$(http_code http://127.0.0.1:9000/gradio_api/info)"
comfy_code="$(http_code http://127.0.0.1:8188/system_stats)"
qwen_code="000"
if [ "$qwen_expected" = true ]; then
  qwen_code="$(http_code http://127.0.0.1:8000/v1/models)"
fi

problems=()
is_ok "$backend_state" || problems+=("$backend_service=$backend_state")
is_ok "$nginx_state" || problems+=("nginx=$nginx_state")
[ "$gradio_code" = "200" ] || problems+=("gradio_http=$gradio_code")
[ "$comfy_code" = "200" ] || problems+=("comfy_http=$comfy_code")
if [ "$qwen_expected" = true ]; then
  is_ok "$qwen_state" || problems+=("qwen-vllm=$qwen_state")
  [ "$qwen_code" = "200" ] || problems+=("qwen_http=$qwen_code")
fi

failures=0
if [ -r "$FAIL_FILE" ]; then
  read -r failures < "$FAIL_FILE" || failures=0
fi
case "$failures" in
  ''|*[!0-9]*) failures=0 ;;
esac

restarted="none"
if [ "${#problems[@]}" -eq 0 ]; then
  failures=0
else
  failures=$((failures + 1))
  if [ "$failures" -ge "$RESTART_AFTER" ]; then
    # Restart only the service that owns the failed dependency.  Both media
    # and Qwen are expected to be online in the split-GPU production profile.
    if { ! is_ok "$backend_state" || [ "$gradio_code" != "200" ] || [ "$comfy_code" != "200" ]; } && media_queue_active; then
      restarted="deferred-active-media"
    elif ! is_ok "$backend_state" || [ "$gradio_code" != "200" ] || [ "$comfy_code" != "200" ]; then
      systemctl restart "$backend_service"
      restarted="$backend_service"
    fi
    if ! is_ok "$nginx_state"; then
      systemctl restart nginx
      restarted="$restarted,nginx"
    fi
    if [ "$qwen_expected" = true ] && { ! is_ok "$qwen_state" || [ "$qwen_code" != "200" ]; }; then
      systemctl restart qwen-vllm
      restarted="$restarted,qwen-vllm"
    fi
    failures=0
  fi
fi

printf '%s\n' "$failures" > "$FAIL_FILE"

report_tmp="${REPORT_FILE}.tmp"
{
  printf 'timestamp=%s\n' "$(date --iso-8601=seconds)"
  printf 'mode=%s\n' "$mode"
  printf 'backend_service=%s\n' "$backend_service"
  printf 'backend_state=%s\n' "$backend_state"
  printf 'nginx_state=%s\n' "$nginx_state"
  printf 'qwen_state=%s\n' "$qwen_state"
  printf 'gradio_http=%s\n' "$gradio_code"
  printf 'comfy_http=%s\n' "$comfy_code"
  printf 'qwen_http=%s\n' "$qwen_code"
  printf 'consecutive_failures=%s\n' "$failures"
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
