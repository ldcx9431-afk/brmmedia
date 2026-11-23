#!/usr/bin/env bash
set -euo pipefail

# BRMMedia Bare-Metal Ubuntu 24.04 LTS Bootstrap & Offline Recovery Script
# Generated: 2026-11-22
# Usage: sudo bash bootstrap_ubuntu2404.sh [--data-dev /dev/sdX2] [--srv-dev /dev/nvmeXn1p1] [--symlink-models] [--verify-sha256]

KIT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA_MOUNT="/mnt/d"
SRV_ROOT="/srv/brmmedia"
DATA_DEV=""
SRV_DEV=""
SYMLINK_MODELS=0
VERIFY_SHA256=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --data-dev)       DATA_DEV="${2:-}"; shift 2 ;;
    --srv-dev)        SRV_DEV="${2:-}"; shift 2 ;;
    --symlink-models) SYMLINK_MODELS=1; shift ;;
    --verify-sha256)  VERIFY_SHA256=1; shift ;;
    -h|--help)
      echo "Usage: sudo bash $0 [--data-dev /dev/sdX2] [--srv-dev /dev/nvmeXn1p1] [--symlink-models] [--verify-sha256]"
      exit 0
      ;;
    *) echo "[ERROR] Unknown argument: $1" >&2; exit 1 ;;
  esac
done

if [ "$(id -u)" -ne 0 ]; then
  echo "[ERROR] Please run as root (sudo bash $0)." >&2
  exit 1
fi

echo "=== [1/8] Checking OS & Installing Base Packages ==="
. /etc/os-release
echo "OS: ${PRETTY_NAME:-unknown}"
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
  nginx sqlite3 curl jq rsync ntfs-3g git python3 python3-venv python3.12-venv \
  ffmpeg libsndfile1 libgl1 libglib2.0-0 aria2 libgomp1

echo "=== [2/8] Checking NVIDIA Driver & GPU Visibility ==="
if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi >/dev/null 2>&1; then
  nvidia-smi --query-gpu=index,name,memory.total,driver_version --format=csv,noheader
else
  echo "[WARN] nvidia-smi not ready. Installing nvidia-driver-570-server..."
  DEBIAN_FRONTEND=noninteractive apt-get install -y nvidia-driver-570-server
  echo "[IMPORTANT] NVIDIA driver installed. Please reboot and re-run this script!"
  exit 2
fi

echo "=== [3/8] Ensuring Service User (brm) & Mount Points ==="
if ! id brm >/dev/null 2>&1; then
  useradd --system --create-home --shell /bin/bash brm
  echo "[OK] Created user brm"
fi

if [ -n "$DATA_DEV" ] && ! mountpoint -q "$DATA_MOUNT"; then
  mkdir -p "$DATA_MOUNT"
  UUID="$(blkid -s UUID -o value "$DATA_DEV")"
  FSTYPE="$(blkid -s TYPE -o value "$DATA_DEV")"
  if [ -n "$UUID" ] && ! grep -q "$UUID" /etc/fstab; then
    if [ "$FSTYPE" = "ntfs" ]; then
      echo "UUID=$UUID $DATA_MOUNT ntfs3 defaults,uid=brm,gid=brm,umask=002,nofail 0 0" >> /etc/fstab
    else
      echo "UUID=$UUID $DATA_MOUNT $FSTYPE defaults,nofail 0 2" >> /etc/fstab
    fi
  fi
  mount -a
fi

if [ -n "$SRV_DEV" ] && ! mountpoint -q /srv; then
  mkdir -p /srv
  UUID_SRV="$(blkid -s UUID -o value "$SRV_DEV")"
  FSTYPE_SRV="$(blkid -s TYPE -o value "$SRV_DEV")"
  if [ -n "$UUID_SRV" ] && ! grep -q "$UUID_SRV" /etc/fstab; then
    echo "UUID=$UUID_SRV /srv $FSTYPE_SRV defaults,noatime 0 2" >> /etc/fstab
  fi
  mount -a
fi

mkdir -p /var/lib/brmmedia /etc/brmmedia \
  "$SRV_ROOT/models" "$SRV_ROOT/logs" "$SRV_ROOT/outputs" \
  "$SRV_ROOT/cache/huggingface" "$SRV_ROOT/cache/torch" \
  "$SRV_ROOT/releases/h3-23d7084/runtime-locks/venvs" \
  "$SRV_ROOT/strata" \
  "$DATA_MOUNT/brmmedia/outputs"

if [ "$VERIFY_SHA256" -eq 1 ] && [ -f "$KIT_DIR/CHECKSUMS_WEIGHTS.sha256" ]; then
  echo "=== [Optional] Verifying CHECKSUMS_WEIGHTS.sha256 ==="
  sha256sum -c "$KIT_DIR/CHECKSUMS_WEIGHTS.sha256"
fi

echo "=== [4/8] Restoring Databases, Secrets, Workbench State & Nginx ==="
cp -a "$KIT_DIR/var_lib_brmmedia/." /var/lib/brmmedia/
chown -R brm:brm /var/lib/brmmedia
chmod 750 /var/lib/brmmedia
[ -f /var/lib/brmmedia/qwen-api-keys.sqlite3 ] && chmod 600 /var/lib/brmmedia/qwen-api-keys.sqlite3
[ -f /var/lib/brmmedia/qwen-api-keys.sqlite3.fernet ] && chmod 600 /var/lib/brmmedia/qwen-api-keys.sqlite3.fernet

cp -a "$KIT_DIR/etc_brmmedia/." /etc/brmmedia/
chown root:brm /etc/brmmedia
chmod 750 /etc/brmmedia
chown root:brm /etc/brmmedia/* 2>/dev/null || true
chmod 640 /etc/brmmedia/* 2>/dev/null || true

mkdir -p /etc/nginx/snippets /etc/nginx/sites-available /etc/nginx/sites-enabled
[ -d "$KIT_DIR/etc_nginx_snippets" ] && cp -a "$KIT_DIR/etc_nginx_snippets/." /etc/nginx/snippets/
[ -f "$KIT_DIR/etc_nginx_extra/nginx.conf" ] && cp -a "$KIT_DIR/etc_nginx_extra/nginx.conf" /etc/nginx/nginx.conf
cp -a "$KIT_DIR/etc_nginx_sites-available/." /etc/nginx/sites-available/
cp -a "$KIT_DIR/etc_nginx_sites-enabled/." /etc/nginx/sites-enabled/
chown root:root /etc/nginx/nginx.conf /etc/nginx/sites-available /etc/nginx/sites-enabled /etc/nginx/snippets
chmod 0755 /etc/nginx/sites-available /etc/nginx/sites-enabled /etc/nginx/snippets
chmod 0644 /etc/nginx/nginx.conf
find /etc/nginx/sites-available /etc/nginx/sites-enabled /etc/nginx/snippets -type f -exec chown root:root {} + -exec chmod 0644 {} +
for f in "$KIT_DIR/etc_nginx_extra/"*; do
  [ -e "$f" ] || continue
  base="$(basename "$f")"
  if [[ "$base" == .htpasswd* ]]; then
    cp -a "$f" "/etc/nginx/$base"
  fi
done
nginx -t
systemctl enable --now nginx
systemctl reload nginx

echo "=== [5/8] Restoring Runtime Code, Venvs, Engines & Custom Nodes ==="
BKP_DIR="$KIT_DIR/srv_brmmedia_backup"
extract_tar() {
  local archive="$1"
  local dest="$2"
  if [ -f "$archive" ]; then
    echo "[EXTRACT] $(basename "$archive") -> $dest"
    mkdir -p "$dest"
    if [[ "$archive" == *.tar.gz ]]; then
      tar -xzf "$archive" -C "$dest"
    else
      tar -xf "$archive" -C "$dest"
    fi
  else
    echo "[WARN] Archive not found: $archive"
  fi
}

extract_tar "$BKP_DIR/ComfyUI-code-and-custom_nodes.tar.gz" "$SRV_ROOT"
extract_tar "$BKP_DIR/ComfyUI-h3-v032-canary.tar.gz" "$SRV_ROOT"
extract_tar "$BKP_DIR/h3-23d7084.tar.gz" "$SRV_ROOT/releases"
extract_tar "$BKP_DIR/venv-h3-v032-canary.tar" "$SRV_ROOT/releases/h3-23d7084/runtime-locks/venvs"
extract_tar "$BKP_DIR/indextts25-runtime.tar" "$SRV_ROOT/releases/h3-23d7084/runtime-locks"
extract_tar "$BKP_DIR/qwen-image21-runtime.tar" "$SRV_ROOT"
extract_tar "$BKP_DIR/strata-runtime-and-venv.tar" "$SRV_ROOT/strata"
extract_tar "$BKP_DIR/zonos3-mini.tar.gz" "$SRV_ROOT"
extract_tar "$BKP_DIR/qwen3-tts-and-modellab.tar.gz" "$SRV_ROOT"

# Keep outputs & venv symlinks normalized for case-sensitive Linux ntfs3
ln -sfn "$DATA_MOUNT/brmmedia/outputs" "$SRV_ROOT/releases/h3-23d7084/ubuntu-backend-deploy/outputs"
ln -sfn "$SRV_ROOT/releases/h3-23d7084/runtime-locks/venvs/h3-v032-canary" "$SRV_ROOT/releases/h3-23d7084/ubuntu-backend-deploy/.venv"
ln -sfn "$SRV_ROOT/ComfyUI/models" "$SRV_ROOT/ComfyUI-h3-v032-canary/models"

echo "=== [6/8] Restoring Model Weights Mirror & Symlinks ==="
MIRROR_SRV="$KIT_DIR/full_models_and_runtime_mirror/srv_brmmedia"
AVAIL_GB="$(df -BG --output=avail /srv | tail -n 1 | tr -dc '0-9')"
AVAIL_GB="${AVAIL_GB:-0}"
echo "Available space on /srv: ${AVAIL_GB}G (symlink_models=${SYMLINK_MODELS})"

restore_model_tree() {
  local rel="$1"
  local src="$MIRROR_SRV/$rel"
  local dst="$SRV_ROOT/$rel"
  [ -d "$src" ] || return 0
  if [ "$SYMLINK_MODELS" -eq 0 ] && [ "$AVAIL_GB" -ge 260 ]; then
    echo "[RSYNC] $src -> $dst"
    mkdir -p "$dst"
    rsync -rtD --info=progress2 "$src/" "$dst/"
  else
    echo "[SYMLINK] $dst -> $src"
    mkdir -p "$(dirname "$dst")"
    if [ -d "$dst" ] && [ ! -L "$dst" ]; then
      rmdir "$dst" 2>/dev/null || rm -rf "$dst"
    fi
    ln -sfn "$src" "$dst"
  fi
}

if [ -d "$MIRROR_SRV" ]; then
  restore_model_tree "ComfyUI/models"
  restore_model_tree "strata/data"
  restore_model_tree "qwen-image21/models"
  restore_model_tree "qwen-image21/ComfyUI/models"
  restore_model_tree "releases/h3-23d7084/runtime-locks/indextts25-v2.5/models"
  restore_model_tree "model-hot-cache"
  [ -d "$MIRROR_SRV/offline-wheels" ] && rsync -rtD "$MIRROR_SRV/offline-wheels/" "$SRV_ROOT/offline-wheels/"
fi

# Restore /srv/brmmedia/models/* symlinks to /mnt/d/brmmedia/models/*
for model_dir in YuE2-3B-GGUF Fun-CosyVoice3-0.5B-2512 IndexTTS-2 LTX-2.3 acestep; do
  if [ -d "$DATA_MOUNT/brmmedia/models/$model_dir" ] && [ ! -e "$SRV_ROOT/models/$model_dir" ]; then
    ln -sfn "$DATA_MOUNT/brmmedia/models/$model_dir" "$SRV_ROOT/models/$model_dir"
  fi
done

# Restore recorded symlinks from SYMLINKS_RESTORE_MAP.tsv
if [ -f "$KIT_DIR/SYMLINKS_RESTORE_MAP.tsv" ]; then
  while IFS=$'\t' read -r link_path target; do
    [ -n "$link_path" ] && [ -n "$target" ] || continue
    # Normalize Windows-case /mnt/d/BRMMedia -> /mnt/d/brmmedia
    target="${target/\/mnt\/d\/BRMMedia\//\/mnt\/d\/brmmedia\/}"
    # Skip creating inside symlinked directory itself
    parent_dir="$(dirname "$link_path")"
    if [ -L "$parent_dir" ]; then
      continue
    fi
    real_parent="$(readlink -f "$parent_dir" 2>/dev/null || echo "$parent_dir")"
    if [[ "$real_parent" == "$KIT_DIR"* ]]; then
      continue
    fi
    mkdir -p "$parent_dir"
    if [ ! -e "$link_path" ]; then
      ln -sfn "$target" "$link_path"
    fi
  done < "$KIT_DIR/SYMLINKS_RESTORE_MAP.tsv"
fi

chown -R brm:brm "$SRV_ROOT"
BROKEN="$(find "$SRV_ROOT" -xtype l 2>/dev/null | wc -l || true)"
echo "Broken symlinks in $SRV_ROOT: $BROKEN"

echo "=== [7/8] Restoring Systemd Units, Sysctl & Sudoers Helpers ==="
cp -a "$KIT_DIR/etc_systemd_system/." /etc/systemd/system/
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
install -d -m 0750 -o brm -g brm /var/tmp/brmmedia-strata
printf 'd /var/tmp/brmmedia-strata 0750 brm brm -\n' > /etc/tmpfiles.d/brmmedia-strata.conf
chmod 0644 /etc/tmpfiles.d/brmmedia-strata.conf
cat <<'EOF' > /etc/sysctl.d/99-brmmedia-ai-perf.conf
vm.swappiness = 1
vm.dirty_ratio = 20
vm.dirty_background_ratio = 5
fs.file-max = 2097152
fs.inotify.max_user_watches = 524288
net.core.somaxconn = 65535
net.ipv4.tcp_tw_reuse = 1
EOF
chmod 0644 /etc/sysctl.d/99-brmmedia-ai-perf.conf
sysctl --system >/dev/null || true
for f in "$KIT_DIR/sudoers_and_sbin/"*; do
  [ -e "$f" ] || continue
  base="$(basename "$f")"
  if [[ "$base" == brmmedia-* ]]; then
    cp -a "$f" "/usr/local/sbin/$base"
    chmod 755 "/usr/local/sbin/$base"
  else
    cp -a "$f" "/etc/sudoers.d/$base"
    chmod 440 "/etc/sudoers.d/$base"
  fi
done
systemctl daemon-reload

echo "=== [8/8] Enabling & Starting Core Production Services & Health Check ==="
CORE_SERVICES=(
  baorong-backend.service
  brmmedia-lan-api.service
  brmmedia-qwen-api-gateway.service
  brmmedia-qwen-image21.service
  brmmedia-strata.service
  brmmedia-zonos3-mini.service
  indextts25.service
  brmmedia-healthcheck.timer
)
for svc in "${CORE_SERVICES[@]}"; do
  if [ -f "/etc/systemd/system/$svc" ]; then
    systemctl enable --now "$svc"
    echo "[SERVICE] $svc -> $(systemctl is-active "$svc" || true)"
  else
    echo "[WARN] Missing unit: $svc"
  fi
done

echo "=== Health Check (Waiting 10s for loopback ports) ==="
sleep 10
for spec in "80:/" "8188:/" "9000:/" "9100:/api/v1/health" "9205:/health" "9300:/health" "8190:/" "1919:/health" "8002:/v1/models"; do
  port="${spec%%:*}"
  path="${spec#*:}"
  code="$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 "http://127.0.0.1:${port}${path}" || echo "ERR")"
  echo "http://127.0.0.1:${port}${path} -> HTTP $code"
done

if find "$DATA_MOUNT/brmmedia/models/YuE2-3B-GGUF" "$DATA_MOUNT/brmmedia/models/Fun-CosyVoice3-0.5B-2512" -name "*.part" 2>/dev/null | grep -q .; then
  echo "[NOTICE] Partial HF downloads detected; run: bash $KIT_DIR/resume_hf_downloads.sh"
fi

echo "[DONE] Bootstrap complete."
