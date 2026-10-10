#!/usr/bin/env bash
# Capacity gate before a 42 GB H3 download or service cutover.
set -euo pipefail

# The operational requirement is 64 GB (decimal).  Linux reports KiB, so do
# not reject a normal 64-GB Windows host merely because it appears as ~62 GiB.
required_kib=$((64 * 1000 * 1000))
required_model_bytes=$((50 * 1024 * 1024 * 1024))
runtime_root="${BRMMEDIA_APP_ROOT:-/srv/brmmedia/app}"
model_source="${BRMMEDIA_MODEL_SOURCE_ROOT:-/mnt/d/model}"
failed=0
allow_pagefile_override="${BRMMEDIA_H3_ALLOW_PAGEFILE_OVERRIDE:-0}"

# systemd units in WSL intentionally start with a narrow PATH.  NVIDIA's WSL
# shim is installed outside that PATH, even though it is available in an
# interactive shell.  Resolve it explicitly so the production activation
# gate behaves the same in both environments.
nvidia_smi="${BRMMEDIA_NVIDIA_SMI:-}"
if [ -z "$nvidia_smi" ]; then
  nvidia_smi="$(command -v nvidia-smi || true)"
fi
if [ -z "$nvidia_smi" ] && [ -x /usr/lib/wsl/lib/nvidia-smi ]; then
  nvidia_smi=/usr/lib/wsl/lib/nvidia-smi
fi

ok() { printf 'OK   %s\n' "$*"; }
bad() { printf 'FAIL %s\n' "$*" >&2; failed=1; }

if [ -z "$nvidia_smi" ]; then
  bad "nvidia-smi is unavailable"
else
  gpu_data="$("$nvidia_smi" --query-gpu=index,name,memory.total,uuid --format=csv,noheader 2>/dev/null || true)"
  printf '%s\n' "$gpu_data"
  printf '%s\n' "$gpu_data" | grep -E '^0, RTX A5000|^0, NVIDIA RTX A5000' >/dev/null && ok "GPU0 is A5000" || bad "GPU0 must be RTX A5000"
  printf '%s\n' "$gpu_data" | grep -E '^1, RTX A4000|^1, NVIDIA RTX A4000' >/dev/null && ok "GPU1 is A4000" || bad "GPU1 must be RTX A4000"
  gpu0_mem="$(printf '%s\n' "$gpu_data" | awk -F, '$1 ~ /^0$/ {gsub(/[^0-9]/,"",$3); print $3}')"
  gpu1_mem="$(printf '%s\n' "$gpu_data" | awk -F, '$1 ~ /^1$/ {gsub(/[^0-9]/,"",$3); print $3}')"
  [ "${gpu0_mem:-0}" -ge 24000 ] && ok "GPU0 has at least 24 GB VRAM" || bad "GPU0 must expose at least 24 GB VRAM"
  [ "${gpu1_mem:-0}" -ge 16000 ] && ok "GPU1 has at least 16 GB VRAM" || bad "GPU1 must expose at least 16 GB VRAM"
  "$nvidia_smi" --query-gpu=driver_version --format=csv,noheader | head -n1 | grep -Eq '^[0-9]+\.' && ok "NVIDIA driver version is available" || bad "NVIDIA driver version unavailable"
fi

mem_kib="$(awk '/MemTotal:/ {print $2}' /proc/meminfo)"
if [ "${mem_kib:-0}" -ge "$required_kib" ]; then
  ok "WSL visible memory is at least 64 GB"
else
  bad "WSL visible memory is below 64 GiB (${mem_kib:-0} KiB)"
fi

pagefile=""
if command -v powershell.exe >/dev/null 2>&1; then
  pagefile="$(powershell.exe -NoProfile -Command 'Get-CimInstance Win32_PageFileUsage | Select-Object Name,AllocatedBaseSize | ConvertTo-Json -Compress' 2>/dev/null | tr -d '\r' || true)"
fi
if printf '%s' "$pagefile" | grep -E '"Name":"E:.*"AllocatedBaseSize":([6-9][4-9][0-9][0-9][0-9]|[1-9][0-9]{5,})' >/dev/null; then
  ok "Windows E: page file is at least 64 GB"
elif [ "$allow_pagefile_override" = "1" ]; then
  ok "Windows page-file gate explicitly overridden by operator"
else
  bad "Windows page file must be on E: and at least 64 GB (detected: ${pagefile:-unavailable})"
fi

for path in "$runtime_root" "$model_source"; do
  if [ ! -e "$path" ]; then
    bad "missing required path: $path"
    continue
  fi
  available="$(df -PB1 "$path" | awk 'NR==2 {print $4}')"
  if [ "${available:-0}" -ge "$required_model_bytes" ]; then
    ok "at least 50 GB free at $path"
  else
    bad "need at least 50 GB free at $path (found ${available:-0} bytes)"
  fi
done

if [ "$failed" -ne 0 ]; then
  echo "[ERROR] H3 preflight did not pass; do not download or switch services." >&2
  exit 1
fi
echo "[OK] H3 preflight passed."
