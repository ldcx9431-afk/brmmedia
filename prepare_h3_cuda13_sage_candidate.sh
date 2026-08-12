#!/usr/bin/env bash
# Build and validate an isolated CUDA 13 + SageAttention H3 runtime.  It
# never mutates the production venv or starts a LAN-facing service.
set -euo pipefail

APP_ROOT="${BRMMEDIA_APP_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
BACKEND_DIR="$APP_ROOT/ubuntu-backend-deploy"
CANARY_ENV="$APP_ROOT/runtime-locks/h3-canary.env"
SERVICE_USER="${BRMMEDIA_SERVICE_USER:-brm}"
CANARY_VENV="${BRMMEDIA_H3_CANARY_VENV:-$APP_ROOT/runtime-locks/venvs/h3-canary}"
TORCH_VERSION="${BRMMEDIA_H3_TORCH_VERSION:-2.11.0}"
TORCHVISION_VERSION="${BRMMEDIA_H3_TORCHVISION_VERSION:-0.26.0}"
TORCHAUDIO_VERSION="${BRMMEDIA_H3_TORCHAUDIO_VERSION:-2.11.0}"
TORCH_INDEX="${BRMMEDIA_H3_TORCH_INDEX:-https://download.pytorch.org/whl/cu130}"
SAGE_WHEEL_URL="${BRMMEDIA_H3_SAGE_WHEEL_URL:-}"
MANIFEST="$APP_ROOT/runtime-locks/h3-cuda13-sage-candidate.json"

die() { echo "[ERROR] $*" >&2; exit 1; }
note() { echo "[INFO] $*"; }

[ "$(id -u)" -eq 0 ] || die "Run with sudo: sudo $0"
[ -f "$CANARY_ENV" ] || die "Canary environment is missing: $CANARY_ENV"
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

if [ -z "$SAGE_WHEEL_URL" ]; then
  cat >&2 <<'EOF'
[ERROR] Set BRMMEDIA_H3_SAGE_WHEEL_URL to the reviewed Linux x86_64 wheel.
        It must match Python/PyTorch/CUDA 13 (or a vendor-documented stable-ABI
        build that explicitly supports this runtime).  This guard intentionally
        refuses `pip install sageattention`: PyPI's package is a source wrapper,
        not the prebuilt CUDA kernel wheel required by this server.
EOF
  exit 2
fi
case "$SAGE_WHEEL_URL" in https://*) ;; *) die "Sage wheel must use an explicit HTTPS URL";; esac

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

note "Installing reviewed SageAttention wheel in the candidate..."
wheel_tmp="$(mktemp /tmp/brmmedia-sageattention.XXXXXX.whl)"
trap 'rm -f "$wheel_tmp"' EXIT
curl --fail --location --proto '=https' --tlsv1.2 --retry 3 --output "$wheel_tmp" "$SAGE_WHEEL_URL"
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

python3 - "$MANIFEST" "$before" "$TORCH_VERSION" "$TORCHVISION_VERSION" "$TORCHAUDIO_VERSION" "$SAGE_WHEEL_URL" <<'PY'
import json, sys
from datetime import datetime, timezone
path, before, torch_version, torchvision_version, torchaudio_version, wheel = sys.argv[1:]
with open(path, "w", encoding="utf-8") as handle:
    json.dump({
        "candidate": "cuda13-sageattention",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "torch": f"{torch_version}+cu130",
        "torchvision": f"{torchvision_version}+cu130",
        "torchaudio": f"{torchaudio_version}+cu130",
        "sage_wheel": wheel,
        "rollback_pip_freeze": before,
        "next": "Start loopback canary, verify native CUDA logs and Sage patch, then benchmark draft T2V/I2V.",
    }, handle, ensure_ascii=False, indent=2)
    handle.write("\n")
PY
chown "$SERVICE_USER:$SERVICE_USER" "$MANIFEST"
echo "[OK] CUDA 13 + SageAttention candidate prepared: $MANIFEST"
echo "     Do not expose it. Start only with $APP_ROOT/start_minimax_h3_canary.sh, then run acceptance."
