#!/usr/bin/env bash
# Fail-closed structural gate for the isolated ComfyUI v0.32 H3 candidate.
# It performs no inference and does not start or stop services.
set -euo pipefail

APP_ROOT="${BRMMEDIA_APP_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
CANARY_ENV="${BRMMEDIA_H3_CANARY_ENV:-$APP_ROOT/runtime-locks/h3-v032-canary.env}"
LOCK_FILE="${BRMMEDIA_H3_RUNTIME_LOCK:-$APP_ROOT/runtime-locks/h3-comfyui-v0.32.0.env}"

die() { echo "[ERROR] $*" >&2; exit 1; }
env_value() {
  sed -n "s/^$1=//p" "$CANARY_ENV" | tail -n1 | sed -e 's/^"//' -e 's/"$//'
}

[ -f "$CANARY_ENV" ] || die "Canary env is missing: $CANARY_ENV"
[ -f "$LOCK_FILE" ] || die "Runtime lock is missing: $LOCK_FILE"
# shellcheck disable=SC1090
source "$LOCK_FILE"

comfy_root="$(env_value COMFYUI_ROOT)"
venv="$(env_value BRMMEDIA_BACKEND_VENV)"
workflow_dir="$(env_value BRMMEDIA_WORKFLOW_DIR)"
args="$(env_value COMFYUI_ARGS)"
attention="$(env_value BRMMEDIA_H3_AB_ATTENTION)"
fast_disk="$(env_value BRMMEDIA_H3_AB_FAST_DISK)"
cache="$(env_value BRMMEDIA_H3_AB_CACHE)"
python="$venv/bin/python"
cuda_visible="$(env_value COMFYUI_CUDA_VISIBLE_DEVICES)"
cuda_visible="${cuda_visible:-0}"

[ -d "$comfy_root/.git" ] || die "Candidate ComfyUI checkout is missing: $comfy_root"
[ ! -L "$venv" ] && [ -x "$python" ] || die "Candidate must use a physical venv: $venv"
[ -d "$workflow_dir" ] || die "Private workflow snapshot is missing: $workflow_dir"

for spec in \
  'minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors:2339acdf19bfe123f46b971ea35d367a84adb85de43627e1eceafa5a5b2b111e' \
  'minimax_h3_fl2v_turbo_4step_v1.0_768p_comfyui_bf16.safetensors:c396a9a06f58399e9df9754b18299818d84a2ddd371724ba48fe4a41221437dc'
do
  name="${spec%%:*}"
  expected="${spec#*:}"
  model="$comfy_root/models/loras/$name"
  [ -f "$model" ] || die "Reviewed LightX2V Turbo model is missing: $model"
  actual="$(sha256sum "$model" | awk '{print $1}')"
  [ "$actual" = "$expected" ] || die "LightX2V Turbo checksum mismatch: $name"
done

actual_ref="$(git -c safe.directory="$comfy_root" -C "$comfy_root" rev-parse HEAD)"
[ "$actual_ref" = "$BRMMEDIA_H3_COMFYUI_REF" ] || \
  die "ComfyUI commit drift: $actual_ref (expected $BRMMEDIA_H3_COMFYUI_REF)"
actual_version="$($python - "$comfy_root" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
from comfyui_version import __version__
print(__version__)
PY
)"
[ "$actual_version" = "$BRMMEDIA_H3_COMFYUI_VERSION" ] || \
  die "ComfyUI version drift: $actual_version (expected $BRMMEDIA_H3_COMFYUI_VERSION)"

case " $args " in
  *" --highvram "*|*" --gpu-only "*|*" --lowvram "*|*" --novram "*|*" --disable-smart-memory "*|*" --cache-none "*|*" --disable-dynamic-vram "*|*" --disable-async-offload "*)
    die "Forbidden H3 runtime argument in private canary: $args" ;;
esac

case "$attention" in
  workflow-sage)
    case " $args " in *" --use-ck-attention "*|*" --use-sage-attention "*) die "workflow-sage must not add a global attention backend" ;; esac
    grep -ERq --include='MiniMaxH3-*.json' 'PathchSageAttentionKJ|PatchSageAttentionKJ|SageAttentionKJ' "$workflow_dir" || \
      die "workflow-sage is selected but no Sage patch exists in the private workflows"
    ;;
  kitchen)
    case " $args " in *" --use-ck-attention "*) ;; *) die "kitchen requires --use-ck-attention" ;; esac
    if grep -ERq --include='MiniMaxH3-*.json' 'PathchSageAttentionKJ|PatchSageAttentionKJ|SageAttentionKJ' "$workflow_dir"; then
      die "Kitchen Attention cannot start with a workflow-level Sage patch"
    fi
    ;;
  *) die "Unknown or unset A/B attention profile: ${attention:-<empty>}" ;;
esac
case "$fast_disk:$args" in
  on:*"--fast-disk"*) ;;
  off:*"--fast-disk"*) die "fast-disk metadata says off but the flag is present" ;;
  off:*) ;;
  *) die "fast-disk metadata and COMFYUI_ARGS disagree" ;;
esac
case "$cache:$args" in
  default:*"--cache-lru"*) die "default cache cell contains --cache-lru" ;;
  default:*) ;;
  lru1:*"--cache-lru 1"*) ;;
  *) die "cache metadata and COMFYUI_ARGS disagree" ;;
esac

CUDA_VISIBLE_DEVICES="$cuda_visible" COMFYUI_ROOT="$comfy_root" \
  EXPECTED_KITCHEN="$BRMMEDIA_H3_COMFY_KITCHEN_VERSION" \
  EXPECT_SAGE="$attention" "$python" - <<'PY'
import importlib.metadata
import importlib.util
import os
import torch

assert torch.cuda.is_available(), "CUDA is unavailable"
assert torch.version.cuda and torch.version.cuda.startswith("13."), torch.version.cuda
assert torch.cuda.get_device_capability(0) == (8, 6), torch.cuda.get_device_capability(0)
assert "A5000" in torch.cuda.get_device_name(0), torch.cuda.get_device_name(0)
assert importlib.metadata.version("comfy-kitchen") == os.environ["EXPECTED_KITCHEN"]
if os.environ["EXPECT_SAGE"] == "workflow-sage":
    assert importlib.util.find_spec("sageattention"), "SageAttention is unavailable"
print("torch", torch.__version__, "cuda", torch.version.cuda)
print("comfy-kitchen", importlib.metadata.version("comfy-kitchen"))
PY

echo "[OK] H3 v0.32 canary runtime lock and A/B cell are consistent."
echo "     attention=$attention fast_disk=$fast_disk cache=$cache args=$args"
