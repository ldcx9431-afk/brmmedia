#!/usr/bin/env bash
# Build and validate an isolated CUDA 13 + SageAttention H3 runtime.  It
# never mutates the production venv or starts a LAN-facing service.
set -euo pipefail

APP_ROOT="${BRMMEDIA_APP_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
BACKEND_DIR="$APP_ROOT/ubuntu-backend-deploy"
CANARY_ENV="${BRMMEDIA_H3_CANARY_ENV:-$APP_ROOT/runtime-locks/h3-v032-canary.env}"
RUNTIME_LOCK="${BRMMEDIA_H3_RUNTIME_LOCK:-$APP_ROOT/runtime-locks/h3-comfyui-v0.32.0.env}"
SERVICE_USER="${BRMMEDIA_SERVICE_USER:-brm}"
CANARY_VENV="${BRMMEDIA_H3_CANARY_VENV:-$APP_ROOT/runtime-locks/venvs/h3-v032-canary}"
TORCH_VERSION="${BRMMEDIA_H3_TORCH_VERSION:-2.11.0}"
TORCHVISION_VERSION="${BRMMEDIA_H3_TORCHVISION_VERSION:-0.26.0}"
TORCHAUDIO_VERSION="${BRMMEDIA_H3_TORCHAUDIO_VERSION:-2.11.0}"
TORCH_INDEX="${BRMMEDIA_H3_TORCH_INDEX:-https://download.pytorch.org/whl/cu130}"
SAGE_WHEEL_URL="${BRMMEDIA_H3_SAGE_WHEEL_URL:-}"
SAGE_SOURCE_BUILD="${BRMMEDIA_H3_SAGE_SOURCE_BUILD:-0}"
SAGE_SOURCE_REPO="${BRMMEDIA_H3_SAGE_SOURCE_REPO:-https://github.com/thu-ml/SageAttention.git}"
# Pinned official v2.2.0 tag.  Keep the full commit in the manifest so a
# candidate can be recreated without trusting an unpinned branch head.
SAGE_SOURCE_COMMIT="${BRMMEDIA_H3_SAGE_SOURCE_COMMIT:-eb615cf6cf4d221338033340ee2de1c37fbdba4a}"
CUDA_HOME="${CUDA_HOME:-/usr/local/cuda-13.0}"
MANIFEST="$APP_ROOT/runtime-locks/h3-cuda13-sage-candidate.json"

die() { echo "[ERROR] $*" >&2; exit 1; }
note() { echo "[INFO] $*"; }

[ "$(id -u)" -eq 0 ] || die "Run with sudo: sudo $0"
[ -f "$CANARY_ENV" ] || die "Canary environment is missing: $CANARY_ENV"
[ -f "$RUNTIME_LOCK" ] || die "Reviewed ComfyUI v0.32 runtime lock is missing: $RUNTIME_LOCK"
# shellcheck disable=SC1090
source "$RUNTIME_LOCK"
[ -x "$CANARY_VENV/bin/python" ] || die "Physical canary venv is missing: $CANARY_VENV"
[ ! -L "$CANARY_VENV" ] || die "Refusing a symlinked canary venv: $CANARY_VENV"
id "$SERVICE_USER" >/dev/null 2>&1 || die "Missing service user: $SERVICE_USER"
if systemctl is-active --quiet baorong-backend || systemctl is-active --quiet baorong-backend-h3-canary; then
  die "Stop the media backend/canary before changing the isolated candidate venv."
fi

NVIDIA_SMI="${BRMMEDIA_NVIDIA_SMI:-/usr/lib/wsl/lib/nvidia-smi}"
[ -x "$NVIDIA_SMI" ] || NVIDIA_SMI="$(command -v nvidia-smi || true)"
[ -n "$NVIDIA_SMI" ] || die "nvidia-smi is unavailable"
gpu="$($NVIDIA_SMI --query-gpu=index,name,driver_version,compute_cap,memory.total --format=csv,noheader 2>/dev/null || true)"
printf '%s\n' "$gpu"
printf '%s\n' "$gpu" | grep -E '^0, (NVIDIA )?RTX A5000, .*8\.6, (24[0-9]{3}|2[5-9][0-9]{3}) MiB$' >/dev/null || \
  die "GPU0 must be an A5000 (sm86) with 24 GB VRAM"
mem_kib="$(awk '/MemTotal:/ {print $2}' /proc/meminfo)"
[ "${mem_kib:-0}" -ge 64000000 ] || die "WSL must expose >=64GB before a H3 candidate (currently ${mem_kib:-0} KiB)"

if [ -z "$SAGE_WHEEL_URL" ] && [ "$SAGE_SOURCE_BUILD" != "1" ]; then
  cat >&2 <<'EOF'
[ERROR] Set BRMMEDIA_H3_SAGE_WHEEL_URL to the reviewed Linux x86_64 wheel.
        Or set BRMMEDIA_H3_SAGE_SOURCE_BUILD=1 to build the pinned official
        SageAttention source in this isolated candidate venv. This guard
        intentionally refuses unpinned `pip install sageattention` installs.
EOF
  exit 2
fi
if [ -n "$SAGE_WHEEL_URL" ]; then
  case "$SAGE_WHEEL_URL" in https://*) ;; *) die "Sage wheel must use an explicit HTTPS URL";; esac
fi

note "Snapshotting candidate package set before CUDA 13 upgrade..."
install -d -o "$SERVICE_USER" -g "$SERVICE_USER" "$(dirname "$MANIFEST")"
before="$CANARY_VENV/.brm-before-cuda13-sage-$(date -u +%Y%m%dT%H%M%SZ).freeze"
runuser -u "$SERVICE_USER" -- "$CANARY_VENV/bin/python" -m pip freeze > "$before"

note "Installing PyTorch CUDA 13.0 into isolated candidate..."
runuser -u "$SERVICE_USER" -- "$CANARY_VENV/bin/python" -m pip install --upgrade --force-reinstall \
  --index-url "$TORCH_INDEX" \
  "torch==${TORCH_VERSION}+cu130" "torchvision==${TORCHVISION_VERSION}+cu130" "torchaudio==${TORCHAUDIO_VERSION}+cu130"

note "Reinstalling the pinned ComfyUI requirements in the candidate..."
comfy_root="$(sed -n 's/^COMFYUI_ROOT=//p' "$CANARY_ENV" | tail -n1)"
[ -f "$comfy_root/requirements.txt" ] || die "Canary ComfyUI requirements are missing: $comfy_root"
runuser -u "$SERVICE_USER" -- "$CANARY_VENV/bin/python" -m pip install -r "$comfy_root/requirements.txt"

if [ "$SAGE_SOURCE_BUILD" = "1" ]; then
  [ -x "$CUDA_HOME/bin/nvcc" ] || die "Official CUDA toolkit compiler is required: $CUDA_HOME/bin/nvcc"
  source_dir="$APP_ROOT/runtime-locks/sageattention-${SAGE_SOURCE_COMMIT}"
  wheel_dir="$APP_ROOT/runtime-locks/sage-wheels"
  install -d -o "$SERVICE_USER" -g "$SERVICE_USER" "$wheel_dir"
  if [ ! -d "$source_dir/.git" ]; then
    runuser -u "$SERVICE_USER" -- git clone "$SAGE_SOURCE_REPO" "$source_dir"
  fi
  runuser -u "$SERVICE_USER" -- git -C "$source_dir" fetch --tags --force
  runuser -u "$SERVICE_USER" -- git -C "$source_dir" checkout --detach "$SAGE_SOURCE_COMMIT"
  actual_commit="$(git -C "$source_dir" rev-parse HEAD)"
  [ "$actual_commit" = "$SAGE_SOURCE_COMMIT" ] || die "SageAttention source commit mismatch: $actual_commit"
  note "Building pinned official SageAttention source for sm86 in the candidate..."
  rm -f "$wheel_dir"/sageattention-*.whl
  runuser -u "$SERVICE_USER" -- env \
    PATH="$CUDA_HOME/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin" \
    CUDA_HOME="$CUDA_HOME" TORCH_CUDA_ARCH_LIST=8.6 MAX_JOBS=2 \
    "$CANARY_VENV/bin/python" -m pip wheel --no-build-isolation --no-deps --wheel-dir "$wheel_dir" "$source_dir"
  wheel_tmp="$(find "$wheel_dir" -maxdepth 1 -name 'sageattention-*.whl' -print -quit)"
  [ -n "$wheel_tmp" ] || die "Pinned SageAttention source build produced no wheel"
  sage_origin="${SAGE_SOURCE_REPO}@${SAGE_SOURCE_COMMIT}"
else
  note "Installing reviewed SageAttention wheel in the candidate..."
  wheel_tmp="$(mktemp /tmp/brmmedia-sageattention.XXXXXX.whl)"
  trap 'rm -f "$wheel_tmp"' EXIT
  curl --fail --location --proto '=https' --tlsv1.2 --retry 3 --output "$wheel_tmp" "$SAGE_WHEEL_URL"
  sage_origin="$SAGE_WHEEL_URL"
fi
wheel_sha256="$(sha256sum "$wheel_tmp" | awk '{print $1}')"
runuser -u "$SERVICE_USER" -- "$CANARY_VENV/bin/python" -m pip install --no-deps --force-reinstall "$wheel_tmp"

note "Running CUDA/Sage imports and H3 node availability checks..."
COMFYUI_ROOT="$comfy_root" "$CANARY_VENV/bin/python" - <<'PY'
import importlib.util
import os
import sys
import torch

assert torch.cuda.is_available(), "CUDA is unavailable to candidate PyTorch"
assert torch.version.cuda and torch.version.cuda.startswith("13."), torch.version.cuda
assert torch.cuda.get_device_capability(0) == (8, 6), torch.cuda.get_device_capability(0)
assert importlib.util.find_spec("sageattention"), "sageattention import is unavailable"
root = os.environ["COMFYUI_ROOT"]
assert os.path.isfile(os.path.join(root, "comfy_extras", "nodes_minimax_h3.py")), "native H3 nodes missing"
print("python", sys.version.split()[0])
print("torch", torch.__version__, "cuda", torch.version.cuda)
print("gpu", torch.cuda.get_device_name(0), torch.cuda.get_device_capability(0))
print("sageattention", importlib.util.find_spec("sageattention").origin)
PY

after="$APP_ROOT/runtime-locks/h3-v032-cuda13-sage.requirements.lock"
runuser -u "$SERVICE_USER" -- "$CANARY_VENV/bin/python" -m pip freeze > "$after"
requirements_sha256="$(sha256sum "$after" | awk '{print $1}')"
comfy_ref="$(git -c safe.directory="$comfy_root" -C "$comfy_root" rev-parse HEAD)"
[ "$comfy_ref" = "$BRMMEDIA_H3_COMFYUI_REF" ] || \
  die "ComfyUI v0.32 lock drifted before CUDA/Sage manifest: $comfy_ref"

python3 - "$MANIFEST" "$before" "$after" "$requirements_sha256" \
  "$BRMMEDIA_H3_COMFYUI_VERSION" "$comfy_ref" "$TORCH_VERSION" \
  "$TORCHVISION_VERSION" "$TORCHAUDIO_VERSION" "$sage_origin" "$wheel_sha256" \
  "$BRMMEDIA_H3_COMFY_KITCHEN_VERSION" <<'PY'
import json, sys
from datetime import datetime, timezone
(
    path, before, after, requirements_sha256, comfy_version, comfy_ref,
    torch_version, torchvision_version, torchaudio_version, source,
    wheel_sha256, kitchen_version,
) = sys.argv[1:]
with open(path, "w", encoding="utf-8") as handle:
    json.dump({
        "candidate": "comfyui-v032-cuda13-sageattention",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "comfyui_version": comfy_version,
        "comfyui_commit": comfy_ref,
        "torch": f"{torch_version}+cu130",
        "torchvision": f"{torchvision_version}+cu130",
        "torchaudio": f"{torchaudio_version}+cu130",
        "comfy_kitchen": kitchen_version,
        "sage_source_or_wheel": source,
        "sage_wheel_sha256": wheel_sha256,
        "rollback_pip_freeze": before,
        "candidate_pip_freeze": after,
        "candidate_pip_freeze_sha256": requirements_sha256,
        "next": "Start loopback canary, verify native CUDA logs and Sage patch, then benchmark draft T2V/I2V.",
    }, handle, ensure_ascii=False, indent=2)
    handle.write("\n")
PY
chown "$SERVICE_USER:$SERVICE_USER" "$MANIFEST"
echo "[OK] CUDA 13 + SageAttention candidate prepared: $MANIFEST"
echo "     Do not expose it. Start only with $APP_ROOT/start_minimax_h3_canary.sh, then run acceptance."
