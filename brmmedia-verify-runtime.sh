#!/usr/bin/env bash
# Read-only post-deploy acceptance for the production WSL runtime.
set -u -o pipefail

RUNTIME_ENV_FILE="${BRMMEDIA_RUNTIME_ENV_FILE:-/etc/brmmedia/runtime.env}"
runtime_env_value() {
  local key="$1"
  [ -r "$RUNTIME_ENV_FILE" ] || return 0
  sed -n "s/^${key}=//p" "$RUNTIME_ENV_FILE" | tail -n1
}

# The verifier is installed globally, while the active runtime can be a staged
# H3 release.  Prefer an explicit caller value, then the systemd installer
# record, and only then the legacy recovery root.
APP_ROOT="${BRMMEDIA_APP_ROOT:-$(runtime_env_value BRMMEDIA_APP_ROOT)}"
SOURCE_ROOT="${BRMMEDIA_SOURCE_ROOT:-$(runtime_env_value BRMMEDIA_SOURCE_ROOT)}"
APP_ROOT="${APP_ROOT:-/srv/brmmedia/app}"
SOURCE_ROOT="${SOURCE_ROOT:-/mnt/d/brmmedia/source}"
COMFY_ROOT="${COMFYUI_ROOT:-/srv/brmmedia/ComfyUI}"
EXPECTED_COMFY_REF="${BRMMEDIA_COMFYUI_REF:-563b98eefbe643a4cd510ee7f0b43e79880d5a3f}"
BACKEND_DIR="$APP_ROOT/ubuntu-backend-deploy"
QWEN_DIR="$APP_ROOT/llm-backend-deploy"
WORKFLOW_VALIDATOR="$APP_ROOT/validate_comfy_workflows.py"
HTTP_READY_WAIT_SECONDS="${BRMMEDIA_VERIFY_HTTP_WAIT_SECONDS:-45}"
HTTP_READY_POLL_SECONDS="${BRMMEDIA_VERIFY_HTTP_POLL_SECONDS:-2}"
# systemd and globally installed diagnostic commands in WSL can have a narrow
# PATH.  NVIDIA's WSL shim lives outside it, so resolve the same fallback used
# by the H3 preflight rather than falsely reporting GPU0 as unavailable.
nvidia_smi="${BRMMEDIA_NVIDIA_SMI:-}"
if [ -z "$nvidia_smi" ]; then
  nvidia_smi="$(command -v nvidia-smi || true)"
fi
if [ -z "$nvidia_smi" ] && [ -x /usr/lib/wsl/lib/nvidia-smi ]; then
  nvidia_smi=/usr/lib/wsl/lib/nvidia-smi
fi
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

check_source_runtime_sync() {
  local relative source_file runtime_file mismatches=0
  local -a files=(
    "ubuntu-backend-deploy/webui.py"
    "ubuntu-backend-deploy/lan_api.py"
    "ubuntu-backend-deploy/comfyui_server.py"
    "ubuntu-backend-deploy/requirements-backend.txt"
    "ubuntu-backend-deploy/workflows/MiniMaxH3-文生视频.json"
    "ubuntu-backend-deploy/workflows/MiniMaxH3-图生视频.json"
    "validate_comfy_workflows.py"
    "check_h3_preflight.sh"
    "download_minimax_h3_models.sh"
    "import_comfy_models.sh"
    "verify_comfy_models.sh"
    "prepare_minimax_h3_comfyui.sh"
    "rollback_minimax_h3_comfyui.sh"
    "activate_minimax_h3.sh"
    "accept_minimax_h3_video.sh"
    "prepare_h3_cuda13_sage_candidate.sh"
    "runtime-locks/h3-comfyui-v0.32.0.env"
    "configure_h3_canary_ab.sh"
    "verify_h3_canary_runtime.sh"
    "download_minimax_h3_turbo_models.sh"
    "import_minimax_h3_turbo_models.sh"
    "benchmark_h3_profiles.sh"
    "accept_qwen_vllm.sh"
    "start_minimax_h3_download.sh"
    "wait_import_minimax_h3_models.sh"
    "brmmedia-verify-runtime.sh"
  )
  if [ ! -d "$SOURCE_ROOT" ]; then
    bad "deployment source root is missing: $SOURCE_ROOT"
    return
  fi
  for relative in "${files[@]}"; do
    source_file="$SOURCE_ROOT/$relative"
    runtime_file="$APP_ROOT/$relative"
    if [ ! -f "$source_file" ] || [ ! -f "$runtime_file" ]; then
      bad "source/runtime file is missing: $relative"
      mismatches=$((mismatches + 1))
    elif ! cmp -s "$source_file" "$runtime_file"; then
      bad "deployment source differs from runtime: $relative"
      mismatches=$((mismatches + 1))
    fi
  done
  if [ "$mismatches" -eq 0 ]; then
    ok "deployment source matches runtime for ${#files[@]} critical files"
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
    "/submit_workflow_4", "/submit_workflow_3_h3", "/submit_workflow_4_h3",
    "/submit_workflow_5", "/submit_workflow_6",
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
unexpected_endpoints = sorted(set(named_endpoints) - expected_endpoints)
if unexpected_endpoints:
    print("unexpected public Gradio endpoints: " + ", ".join(unexpected_endpoints), file=sys.stderr)
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
check_service brmmedia-lan-api
check_service nginx
check_timer brmmedia-healthcheck.timer
check_source_runtime_sync

if systemctl is-active --quiet baorong-backend-highvram; then
  bad "legacy baorong-backend-highvram is active; it conflicts with the split-GPU production profile"
else
  ok "legacy high-VRAM service is inactive"
fi
check_service qwen-vllm

check_http gradio http://127.0.0.1:9000/gradio_api/info
check_http lan_api http://127.0.0.1:9100/api/v1/health
check_http comfyui http://127.0.0.1:8188/system_stats
check_gradio_ui_contract
check_comfy_workflows

check_http qwen http://127.0.0.1:8000/v1/models

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
  qwen_gpu="$(grep '^QWEN_CUDA_VISIBLE_DEVICES=' "$QWEN_DIR/.env" | head -n1 | cut -d= -f2-)"
  qwen_memory="$(grep '^QWEN_GPU_MEMORY_UTILIZATION=' "$QWEN_DIR/.env" | head -n1 | cut -d= -f2-)"
  qwen_context="$(grep '^QWEN_MAX_MODEL_LEN=' "$QWEN_DIR/.env" | head -n1 | cut -d= -f2-)"
  qwen_seqs="$(grep '^QWEN_MAX_NUM_SEQS=' "$QWEN_DIR/.env" | head -n1 | cut -d= -f2-)"
  qwen_batch_tokens="$(grep '^QWEN_MAX_NUM_BATCHED_TOKENS=' "$QWEN_DIR/.env" | head -n1 | cut -d= -f2-)"
  [ -n "$qwen_model" ] && ok "Qwen served model configured: $qwen_model" || bad "QWEN_SERVED_MODEL_NAME is missing"
  if [ "$qwen_host" = "127.0.0.1" ]; then
    ok "Qwen is bound to loopback"
  else
    bad "Qwen host is not loopback: ${qwen_host:-unset}"
  fi
  [ "$qwen_gpu" = "1" ] && ok "Qwen is pinned to A4000/GPU1" || bad "Qwen GPU must be 1, got ${qwen_gpu:-unset}"
  [ "$qwen_memory" = "0.70" ] && ok "Qwen GPU memory cap is 70%" || bad "Qwen GPU memory cap must be 0.70, got ${qwen_memory:-unset}"
  [ "$qwen_context" = "4096" ] && ok "Qwen context is 4096" || bad "Qwen context must be 4096, got ${qwen_context:-unset}"
  [ "$qwen_seqs" = "1" ] && ok "Qwen concurrency is 1" || bad "Qwen concurrency must be 1, got ${qwen_seqs:-unset}"
  [ "$qwen_batch_tokens" = "2048" ] && ok "Qwen batch tokens are 2048" || bad "Qwen batch tokens must be 2048, got ${qwen_batch_tokens:-unset}"
else
  bad "Qwen runtime .env is missing"
fi

if [ -f "$BACKEND_DIR/.env" ]; then
  comfy_gpu="$(grep '^COMFYUI_CUDA_VISIBLE_DEVICES=' "$BACKEND_DIR/.env" | head -n1 | cut -d= -f2-)"
  comfy_args="$(grep '^COMFYUI_ARGS=' "$BACKEND_DIR/.env" | head -n1 | cut -d= -f2-)"
  video_engine="$(grep '^BRMMEDIA_VIDEO_ENGINE=' "$BACKEND_DIR/.env" | head -n1 | cut -d= -f2-)"
  [ "$comfy_gpu" = "0" ] && ok "ComfyUI is pinned to A5000/GPU0" || bad "ComfyUI GPU must be 0, got ${comfy_gpu:-unset}"
  case " $comfy_args " in
    *" --highvram "*|*" --gpu-only "*) bad "ComfyUI must use dynamic model offload, not ${comfy_args}" ;;
    *) ok "ComfyUI dynamic-offload arguments are safe" ;;
  esac
  [ "$video_engine" = "h3" ] && ok "MiniMax H3 is the active video engine" || bad "BRMMEDIA_VIDEO_ENGINE must be h3, got ${video_engine:-unset}"
else
  bad "Backend runtime .env is missing"
fi

gpu_inventory=""
if [ -n "$nvidia_smi" ]; then
  gpu_inventory="$("$nvidia_smi" --query-gpu=index,name,memory.total,uuid --format=csv,noheader 2>/dev/null || true)"
fi
if ! printf '%s\n' "$gpu_inventory" | grep -Eq '^0, (NVIDIA )?RTX A5000, (2[4-9][0-9]{3}|[3-9][0-9]{4}) MiB,'; then
  bad "GPU0 A5000/24GB is not visible to WSL"
else
  ok "GPU0 A5000/24GB is visible to WSL"
fi
if ! printf '%s\n' "$gpu_inventory" | grep -Eq '^1, (NVIDIA )?RTX A4000, (1[6-9][0-9]{3}|[2-9][0-9]{4}) MiB,'; then
  bad "GPU1 A4000/16GB is not visible to WSL"
else
  ok "GPU1 A4000/16GB is visible to WSL"
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
