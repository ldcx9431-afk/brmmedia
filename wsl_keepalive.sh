#!/usr/bin/env bash
# Keep one WSL client process attached. WSL does not treat systemd services as
# an active client, so without this small process it may stop the VM while the
# backend or a long model import is still running. While alive, publish the
# distro's current NAT address for the Windows SYSTEM port-forwarder task.

REPORT_FILE="/mnt/d/brmmedia/artifacts/wsl-ip.txt"
REPORT_DIR="$(dirname "$REPORT_FILE")"
HEALTH_FILE="$REPORT_DIR/wsl-health.txt"
mkdir -p "$REPORT_DIR"

while true; do
  wsl_ip="$(hostname -I | awk '{print $1}')"
  if [[ "$wsl_ip" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    report_tmp="${REPORT_FILE}.tmp"
    printf '%s\n' "$wsl_ip" > "$report_tmp"
    mv -f "$report_tmp" "$REPORT_FILE"
  fi

  # Persist a small atomic snapshot outside the WSL virtual disk.  When WSL
  # is killed unexpectedly, the last snapshot gives the Windows-side
  # investigation a useful memory/GPU baseline without a continuously growing
  # log or any dependency on the network.
  health_tmp="${HEALTH_FILE}.tmp"
  {
    printf 'timestamp=%s\n' "$(date --iso-8601=seconds)"
    printf 'uptime=%s\n' "$(cut -d. -f1 /proc/uptime)"
    awk '/MemAvailable:|SwapFree:/{printf "%s=%s%s\n", $1, $2, $3}' /proc/meminfo
    if command -v nvidia-smi >/dev/null 2>&1; then
      printf 'gpu_memory_used_mib='
      nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits | paste -sd, -
    fi
  } > "$health_tmp"
  mv -f "$health_tmp" "$HEALTH_FILE"
  sleep 15
done
