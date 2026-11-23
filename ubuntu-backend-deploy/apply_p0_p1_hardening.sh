#!/usr/bin/env bash
# Apply or roll back BRMMedia bare-metal P0 resource bounds and P1 security/storage hardening.
set -euo pipefail

[ "$(id -u)" -eq 0 ] || { echo "[ERROR] Run as root." >&2; exit 1; }

BACKUP_DIR="${BRMMEDIA_OPT_BACKUP_DIR:-/var/backups/brmmedia-opt-20261123}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ "${1:-}" = "--rollback" ]; then
  [ -d "$BACKUP_DIR" ] || { echo "[ERROR] Backup directory missing: $BACKUP_DIR" >&2; exit 1; }
  echo "[INFO] Rolling back configurations from $BACKUP_DIR..."
  cp -a "$BACKUP_DIR/systemd/." /etc/systemd/system/
  rm -f /etc/systemd/system/baorong-backend.service.d/50-resource-bounds.conf \
        /etc/systemd/system/brmmedia-qwen-image21.service.d/50-resource-bounds.conf \
        /etc/systemd/system/brmmedia-zonos3-mini.service.d/50-resource-bounds.conf \
        /etc/systemd/system/brmmedia-lan-api.service.d/50-resource-bounds.conf \
        /etc/systemd/system/brmmedia-qwen-api-gateway.service.d/50-resource-bounds.conf \
        /etc/systemd/system/brmmedia-strata.service.d/50-resource-bounds.conf \
        /etc/systemd/system/nginx.service.d/50-resource-bounds.conf \
        /etc/tmpfiles.d/brmmedia-strata.conf
  cp -a "$BACKUP_DIR/sysctl/99-brmmedia-ai-perf.conf" /etc/sysctl.d/99-brmmedia-ai-perf.conf
  cp -a "$BACKUP_DIR/nginx/nginx.conf" /etc/nginx/nginx.conf
  cp -a "$BACKUP_DIR/nginx/sites-available/." /etc/nginx/sites-available/
  cp -a "$BACKUP_DIR/nginx/sites-enabled/." /etc/nginx/sites-enabled/
  cp -a "$BACKUP_DIR/nginx/snippets/." /etc/nginx/snippets/
  cp -a "$BACKUP_DIR/etc_brmmedia/." /etc/brmmedia/
  APP_DIR="$(systemctl show baorong-backend -p WorkingDirectory --value)"
  if [ -d "$APP_DIR" ]; then
    cp -a "$BACKUP_DIR/backend_deploy/yzy_config.json" "$APP_DIR/yzy_config.json"
    cp -a "$BACKUP_DIR/backend_deploy/.env" "$APP_DIR/.env"
  fi
  sysctl --system >/dev/null
  nginx -t
  systemctl daemon-reload
  systemctl restart brmmedia-strata baorong-backend brmmedia-zonos3-mini brmmedia-lan-api brmmedia-qwen-api-gateway nginx
  systemctl is-enabled --quiet brmmedia-qwen-image21 2>/dev/null && systemctl restart brmmedia-qwen-image21 || true
  echo "[OK] Rollback completed."
  exit 0
fi

install -d -m 0750 -o brm -g brm /var/tmp/brmmedia-strata
printf 'd /var/tmp/brmmedia-strata 0750 brm brm -\n' > /etc/tmpfiles.d/brmmedia-strata.conf
chmod 0644 /etc/tmpfiles.d/brmmedia-strata.conf
systemd-tmpfiles --create /etc/tmpfiles.d/brmmedia-strata.conf

sed -i 's#Environment=PATH=/usr/lib/wsl/lib:#Environment=PATH=#' /etc/systemd/system/brmmedia-strata.service
mkdir -p /etc/systemd/system/brmmedia-strata.service.d
printf '[Service]\nEnvironment=TMPDIR=/var/tmp/brmmedia-strata\n' > /etc/systemd/system/brmmedia-strata.service.d/30-managed-temp.conf
printf '[Service]\nMemoryHigh=76G\nMemoryMax=88G\nMemorySwapMax=0\nOOMScoreAdjust=-200\nEnvironment=HF_HOME=/var/lib/brmmedia-nvme1/cache/huggingface\nEnvironment=TORCH_HOME=/var/lib/brmmedia-nvme1/cache/torch\n' > /etc/systemd/system/brmmedia-strata.service.d/50-resource-bounds.conf

mkdir -p /etc/systemd/system/baorong-backend.service.d
printf '[Service]\nMemoryHigh=44G\nMemoryMax=56G\nOOMScoreAdjust=200\nEnvironment=OMP_NUM_THREADS=24\nEnvironment=MKL_NUM_THREADS=24\n' > /etc/systemd/system/baorong-backend.service.d/50-resource-bounds.conf

if systemctl is-enabled --quiet brmmedia-qwen-image21 2>/dev/null; then
  mkdir -p /etc/systemd/system/brmmedia-qwen-image21.service.d
  printf '[Service]\nMemoryHigh=18G\nMemoryMax=24G\nOOMScoreAdjust=300\nEnvironment=HF_HOME=/var/lib/brmmedia-nvme1/cache/huggingface\nEnvironment=TORCH_HOME=/var/lib/brmmedia-nvme1/cache/torch\n' > /etc/systemd/system/brmmedia-qwen-image21.service.d/50-resource-bounds.conf
fi

mkdir -p /etc/systemd/system/brmmedia-zonos3-mini.service.d
printf '[Service]\nMemoryMax=10G\nMemorySwapMax=0\nOOMScoreAdjust=100\n' > /etc/systemd/system/brmmedia-zonos3-mini.service.d/50-resource-bounds.conf

mkdir -p /etc/systemd/system/nginx.service.d
printf '[Service]\nOOMScoreAdjust=-500\n' > /etc/systemd/system/nginx.service.d/50-resource-bounds.conf

mkdir -p /etc/systemd/system/brmmedia-lan-api.service.d
printf '[Service]\nMemorySwapMax=0\nOOMScoreAdjust=-300\n' > /etc/systemd/system/brmmedia-lan-api.service.d/50-resource-bounds.conf

mkdir -p /etc/systemd/system/brmmedia-qwen-api-gateway.service.d
printf '[Service]\nMemorySwapMax=0\nOOMScoreAdjust=-300\n' > /etc/systemd/system/brmmedia-qwen-api-gateway.service.d/50-resource-bounds.conf

sed -i 's/qwen-vllm\.service/brmmedia-strata.service/' /etc/systemd/system/brmmedia-healthcheck.service
install -m 0644 "$SCRIPT_DIR/99-brmmedia-ai-perf.conf" /etc/sysctl.d/99-brmmedia-ai-perf.conf
sysctl --system >/dev/null

chown root:root /etc/systemd/system
chmod 0755 /etc/systemd/system
find /etc/systemd/system -maxdepth 1 \( -name 'brmmedia*' -o -name 'baorong*' -o -name 'qwen*' -o -name 'indextts*' -o -name 'nginx.service.d' \) | while read -r entry; do
  if [ -d "$entry" ]; then
    chown -R root:root "$entry"
    chmod 0755 "$entry"
    find "$entry" -type f -exec chmod 0644 {} +
  elif [ -f "$entry" ]; then
    chown root:root "$entry"
    chmod 0644 "$entry"
  fi
done

chown root:root /etc/nginx/nginx.conf /etc/nginx/sites-available /etc/nginx/sites-enabled /etc/nginx/snippets
chmod 0755 /etc/nginx/sites-available /etc/nginx/sites-enabled /etc/nginx/snippets
chmod 0644 /etc/nginx/nginx.conf
find /etc/nginx/sites-available /etc/nginx/sites-enabled /etc/nginx/snippets -type f -exec chown root:root {} + -exec chmod 0644 {} +

chown root:brm /etc/brmmedia
chmod 0750 /etc/brmmedia
chown root:brm /etc/brmmedia/*
chmod 0640 /etc/brmmedia/*

nginx -t
systemctl daemon-reload
systemctl enable --now brmmedia-healthcheck.timer
echo "[OK] P0/P1 hardening applied."
