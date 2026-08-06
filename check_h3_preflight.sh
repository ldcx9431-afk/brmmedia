#!/usr/bin/env bash
# Read-only capacity gate before a 42 GB H3 download or service cutover.
set -euo pipefail

# The operational requirement is 64 GB (decimal).  Linux reports KiB, so do
# not reject a normal 64-GB Windows host merely because it appears as ~62 GiB.
required_kib=$((64 * 1000 * 1000))
required_model_bytes=$((50 * 1024 * 1024 * 1024))
runtime_root="${BRMMEDIA_APP_ROOT:-/srv/brmmedia/app}"
model_source="${BRMMEDIA_MODEL_SOURCE_ROOT:-/mnt/d/model}"
failed=0

ok() { printf 'OK   %s\n' "$*"; }
bad() { printf 'FAIL %s\n' "$*" >&2; failed=1; }

if ! command -v nvidia-smi >/dev/null 2>&1; then
  bad "nvidia-smi is unavailable"
else
  gpu_data="$(nvidia-smi --query-gpu=index,name,memory.total,uuid --format=csv,noheader 2>/dev/null || true)"
  printf '%s\n' "$gpu_data"
  printf '%s\n' "$gpu_data" | grep -E '^0, RTX A5000|^0, NVIDIA RTX A5000' >/dev/null && ok "GPU0 is A5000" || bad "GPU0 must be RTX A5000"
  printf '%s\n' "$gpu_data" | grep -E '^1, RTX A4000|^1, NVIDIA RTX A4000' >/dev/null && ok "GPU1 is A4000" || bad "GPU1 must be RTX A4000"
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
