#!/usr/bin/env bash
set -euo pipefail

if ! command -v nvidia-smi >/dev/null 2>&1; then
  echo "[ERROR] nvidia-smi not found. Install NVIDIA driver first."
  exit 1
fi

echo "[INFO] Enabling NVIDIA persistence mode..."
sudo nvidia-smi -pm 1

if [ -n "${MAX_POWER_LIMIT_WATTS:-}" ]; then
  echo "[INFO] Setting GPU power limit to ${MAX_POWER_LIMIT_WATTS}W ..."
  sudo nvidia-smi -pl "$MAX_POWER_LIMIT_WATTS"
else
  echo "[INFO] MAX_POWER_LIMIT_WATTS not set; keeping current power limit."
fi

echo "[INFO] Current GPU status:"
nvidia-smi --query-gpu=name,persistence_mode,power.limit,power.draw,utilization.gpu,memory.used,memory.total --format=csv

cat <<'EOF'

Recommended high-performance runtime profile:

  echo 'BRM_PERF_PROFILE=max' >> .env
  echo 'COMFYUI_ARGS=--highvram' >> .env

Notes:
- This script intentionally avoids application clock locking by default because
  supported clocks vary by GPU and driver.
- For data center GPUs, set MAX_POWER_LIMIT_WATTS explicitly if you know the
  safe board power target.
EOF
