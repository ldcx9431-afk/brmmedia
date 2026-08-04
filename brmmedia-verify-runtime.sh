#!/usr/bin/env bash
# Read-only post-deploy acceptance for the production WSL runtime.
set -u -o pipefail

APP_ROOT="${BRMMEDIA_APP_ROOT:-/srv/brmmedia/app}"
COMFY_ROOT="${COMFYUI_ROOT:-/srv/brmmedia/ComfyUI}"
EXPECTED_COMFY_REF="${BRMMEDIA_COMFYUI_REF:-42d2aa55432b57371ddc9d4078ae250b54227641}"
BACKEND_DIR="$APP_ROOT/ubuntu-backend-deploy"
QWEN_DIR="$APP_ROOT/llm-backend-deploy"
HTTP_READY_WAIT_SECONDS="${BRMMEDIA_VERIFY_HTTP_WAIT_SECONDS:-45}"
HTTP_READY_POLL_SECONDS="${BRMMEDIA_VERIFY_HTTP_POLL_SECONDS:-2}"
failures=0

ok() { printf 'OK   %s\n' "$*"; }
bad() { printf 'FAIL %s\n' "$*" >&2; failures=$((failures + 1)); }

check_service() {
  local name="$1"
  if systemctl is-active --quiet "$name"; then
    ok "service $name is active"
  else
    bad "service $name is not active ($(systemctl is-active "$name" 2>/dev/null || true))"
  fi
}

http_code() {
  curl --silent --output /dev/null --write-out '%{http_code}' --max-time 15 "$1" || true
}

check_http() {
  local name="$1" url="$2" code deadline
  # A backend restart starts ComfyUI and Gradio sequentially.  Treat an
  # immediate 000 during that warm-up as pending rather than a false failed
  # deployment. Operators can set the wait to 0 for a strictly instant probe.
  deadline=$((SECONDS + HTTP_READY_WAIT_SECONDS))
  while :; do
    code="$(http_code "$url")"
    [ "$code" = "200" ] && break
    [ "$SECONDS" -ge "$deadline" ] && break
    sleep "$HTTP_READY_POLL_SECONDS"
  done
  if [ "$code" = "200" ]; then
    ok "$name HTTP 200"
  else
    bad "$name expected HTTP 200 after ${HTTP_READY_WAIT_SECONDS}s, got ${code:-000}"
  fi
}

printf '== BRMMedia runtime verification ==\n'
printf 'app_root=%s\n' "$APP_ROOT"

if [ ! -x "$BACKEND_DIR/.venv/bin/python" ] || [ ! -x "$QWEN_DIR/.venv/bin/python" ]; then
  bad "one or more production virtual environments are missing"
else
  ok "backend and Qwen virtual environments exist"
fi

check_service baorong-backend
check_service nginx

highvram=false
if systemctl is-active --quiet baorong-backend-highvram; then
  highvram=true
  ok "high-VRAM mode is active; Qwen is intentionally excluded"
else
  check_service qwen-vllm
fi

check_http gradio http://127.0.0.1:9000/gradio_api/info
check_http comfyui http://127.0.0.1:8188/system_stats

if [ "$highvram" = false ]; then
  check_http qwen http://127.0.0.1:8000/v1/models
fi

if [ -d "$COMFY_ROOT/.git" ]; then
  comfy_ref="$(git -c safe.directory="$COMFY_ROOT" -C "$COMFY_ROOT" rev-parse HEAD 2>/dev/null || true)"
  if [ "$comfy_ref" = "$EXPECTED_COMFY_REF" ]; then
    ok "ComfyUI commit matches production profile (${comfy_ref:0:12})"
  else
    bad "ComfyUI commit ${comfy_ref:-unknown} does not match ${EXPECTED_COMFY_REF}"
  fi
else
  bad "ComfyUI git checkout is missing: $COMFY_ROOT"
fi

if [ -f "$QWEN_DIR/.env" ]; then
  # The profile only reads model identity and host, not secrets.
  qwen_model="$(grep '^QWEN_SERVED_MODEL_NAME=' "$QWEN_DIR/.env" | head -n1 | cut -d= -f2-)"
  qwen_host="$(grep '^QWEN_HOST=' "$QWEN_DIR/.env" | head -n1 | cut -d= -f2-)"
  [ -n "$qwen_model" ] && ok "Qwen served model configured: $qwen_model" || bad "QWEN_SERVED_MODEL_NAME is missing"
  if [ "$qwen_host" = "127.0.0.1" ]; then
    ok "Qwen is bound to loopback"
  else
    bad "Qwen host is not loopback: ${qwen_host:-unset}"
  fi
else
  bad "Qwen runtime .env is missing"
fi

if [ -x /usr/local/sbin/brmmedia-healthcheck ]; then
  if /usr/local/sbin/brmmedia-healthcheck >/dev/null; then
    ok "controlled health check passes"
  else
    bad "controlled health check reports a failure"
  fi
else
  bad "controlled health check is not installed"
fi

if [ "$failures" -eq 0 ]; then
  printf 'RESULT=PASS\n'
  exit 0
fi

printf 'RESULT=FAIL (%s checks)\n' "$failures" >&2
exit 1
