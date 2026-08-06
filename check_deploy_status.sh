#!/usr/bin/env bash
set -euo pipefail

echo "SYSTEMD=$(systemctl is-system-running 2>/dev/null || true)"
echo "IMPORT_PROCS"
pgrep -af import_comfy_models || true
echo "IMPORT_LOG"
tail -n 12 /srv/brmmedia/logs/model-import.log 2>/dev/null || true
echo "DISK"
df -h /srv/brmmedia
echo "MODELS"
find /srv/brmmedia/ComfyUI/models -type f 2>/dev/null | wc -l
du -sh /srv/brmmedia/ComfyUI/models 2>/dev/null || true
echo "SERVICES"
systemctl is-enabled baorong-backend qwen-vllm brmmedia-lan-api brmmedia-healthcheck.timer 2>&1 || true
echo "LEGACY_HIGHVRAM"
systemctl is-active baorong-backend-highvram 2>&1 || true
