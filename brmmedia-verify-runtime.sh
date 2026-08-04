#!/usr/bin/env bash
# Read-only post-deploy acceptance for the production WSL runtime.
set -u -o pipefail

APP_ROOT="${BRMMEDIA_APP_ROOT:-/srv/brmmedia/app}"
COMFY_ROOT="${COMFYUI_ROOT:-/srv/brmmedia/ComfyUI}"
EXPECTED_COMFY_REF="${BRMMEDIA_COMFYUI_REF:-42d2aa55432b57371ddc9d4078ae250b54227641}"
BACKEND_DIR="$APP_ROOT/ubuntu-backend-deploy"
QWEN_DIR="$APP_ROOT/llm-backend-deploy"
WORKFLOW_VALIDATOR="$APP_ROOT/validate_comfy_workflows.py"
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

check_timer() {
  local name="$1"
  if ! systemctl is-active --quiet "$name"; then
    bad "timer $name is not active ($(systemctl is-active "$name" 2>/dev/null || true))"
    return
  fi
  if systemctl is-enabled --quiet "$name"; then
    ok "timer $name is active and enabled"
  else
    bad "timer $name is active but not enabled for boot"
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

check_gradio_ui_contract() {
  local result
  if [ ! -x "$BACKEND_DIR/.venv/bin/python" ]; then
    bad "cannot inspect Gradio UI contract: backend Python is missing"
    return
  fi
  # /config is the server-side source of truth for rendered Gradio components.
  # Check the controls that protect the user journeys repeatedly adjusted in
  # this project, without opening a browser or submitting a generation task.
  if result="$("$BACKEND_DIR/.venv/bin/python" - <<'PY'
import json
import sys
import urllib.request

expected_ids = {
    "global-settings-panel",
    "global-settings-close",
    "qwen-answer",
    "q-gallery",
    "media-viewer",
    "media-viewer-close",
}

expected_labels = {
    "当前访问密码",
    "选择要试听的已完成音频",
    "音频试听",
}
expected_endpoints = {
    "/submit_workflow_1", "/submit_workflow_2", "/submit_workflow_3",
    "/submit_workflow_4", "/submit_workflow_5", "/submit_workflow_6",
    "/submit_workflow_7", "/submit_workflow_8", "/task_status",
}

try:
    with urllib.request.urlopen("http://127.0.0.1:9000/config", timeout=15) as response:
        config = json.load(response)
except Exception as exc:
    print(f"unable to read /config: {exc}", file=sys.stderr)
    raise SystemExit(1)

components = config.get("components")
if not isinstance(components, list):
    print("/config has no components list", file=sys.stderr)
    raise SystemExit(1)

try:
    with urllib.request.urlopen("http://127.0.0.1:9000/gradio_api/info", timeout=15) as response:
        api_info = json.load(response)
except Exception as exc:
    print(f"unable to read /gradio_api/info: {exc}", file=sys.stderr)
    raise SystemExit(1)
named_endpoints = api_info.get("named_endpoints")
if not isinstance(named_endpoints, dict):
    print("/gradio_api/info has no named_endpoints map", file=sys.stderr)
    raise SystemExit(1)
missing_endpoints = sorted(expected_endpoints - set(named_endpoints))
if missing_endpoints:
    print("missing Gradio API endpoints: " + ", ".join(missing_endpoints), file=sys.stderr)
    raise SystemExit(1)

ids = set()
labels = set()
for component in components:
    props = component.get("props") or {}
    if props.get("elem_id"):
        ids.add(props["elem_id"])
    if props.get("label"):
        labels.add(props["label"])

missing = sorted((expected_ids - ids) | (expected_labels - labels))
if missing:
    print("missing UI components: " + ", ".join(missing), file=sys.stderr)
    raise SystemExit(1)
print(f"{len(components)} components, {len(expected_endpoints)} required API endpoints")
PY
  )"; then
    ok "Gradio UI contract present (${result})"
  else
    bad "Gradio UI contract is incomplete"
  fi
}

check_comfy_workflows() {
  local result summary
  if [ ! -x "$BACKEND_DIR/.venv/bin/python" ] || [ ! -f "$WORKFLOW_VALIDATOR" ]; then
    bad "cannot validate ComfyUI workflows: validator or backend Python is missing"
    return
  fi
  if result="$("$BACKEND_DIR/.venv/bin/python" "$WORKFLOW_VALIDATOR" \
    --url http://127.0.0.1:8188 \
    --workflows "$BACKEND_DIR/workflows" \
    --timeout 10)"; then
    summary="${result##*$'\n'}"
    ok "ComfyUI workflow preflight passed (${summary})"
  else
    printf '%s\n' "$result" >&2
    bad "one or more workflow nodes or static assets are unavailable"
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
check_timer brmmedia-healthcheck.timer

highvram=false
if systemctl is-active --quiet baorong-backend-highvram; then
  highvram=true
  ok "high-VRAM mode is active; Qwen is intentionally excluded"
else
  check_service qwen-vllm
fi

check_http gradio http://127.0.0.1:9000/gradio_api/info
check_http comfyui http://127.0.0.1:8188/system_stats
check_gradio_ui_contract
check_comfy_workflows

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
