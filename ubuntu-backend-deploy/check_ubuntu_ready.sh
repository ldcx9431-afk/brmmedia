#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

COMFYUI_ROOT="${COMFYUI_ROOT:-/opt/baorongwanxiang/ComfyUI}"

echo "== System =="
uname -a
echo

echo "== CPU / RAM =="
nproc || true
free -h || true
echo

echo "== Disk =="
df -h "$SCRIPT_DIR" "$COMFYUI_ROOT" 2>/dev/null || df -h .
echo

echo "== NVIDIA =="
if command -v nvidia-smi >/dev/null 2>&1; then
  nvidia-smi
else
  echo "[WARN] nvidia-smi not found"
fi
echo

echo "== Python =="
if [ -x .venv/bin/python ]; then
  .venv/bin/python --version
  .venv/bin/python - <<'PY'
try:
    import torch
    print("torch:", torch.__version__)
    print("cuda available:", torch.cuda.is_available())
    if torch.cuda.is_available():
        print("gpu:", torch.cuda.get_device_name(0))
        print("tf32 matmul:", torch.backends.cuda.matmul.allow_tf32)
except Exception as e:
    print("torch check failed:", e)
PY
else
  echo "[WARN] .venv not found"
fi
echo

echo "== Required files =="
test -f entry_yzy.py && echo "OK entry_yzy.py"
test -f webui.py && echo "OK webui.py"
test -f comfyui_server.py && echo "OK comfyui_server.py"
test -d workflows && echo "OK workflows/"
test -d models && echo "OK bundled models/" || echo "[WARN] bundled models/ not found"
test -f "$COMFYUI_ROOT/main.py" && echo "OK ComfyUI main.py" || echo "[WARN] ComfyUI main.py not found at $COMFYUI_ROOT"
echo

echo "== Key model spot check =="
for p in \
  "models/unet/z_image_turbo_bf16.safetensors" \
  "models/diffusion_models/LTX2.3/LTX-2.3-22B-distilled-1.1-Q4_K_M.gguf" \
  "models/IndexTTS-2/gpt.pth" \
  "models/text_encoders/qwen_3_4b.safetensors"; do
  if [ -f "$p" ]; then
    echo "OK $p"
  else
    echo "[WARN] missing bundled $p"
  fi
done
echo

echo "[DONE] readiness check completed."
