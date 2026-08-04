#!/usr/bin/env bash
set -euo pipefail

APP_ROOT=/srv/brmmedia/app
COMFY_ROOT=/srv/brmmedia/ComfyUI
RUNTIME_ROOT=/srv/brmmedia
SERVICE_USER=brm
STAGED_SOURCE=/mnt/d/brmmedia/source

if [ "$(id -u)" -ne 0 ]; then
  echo "[ERROR] Run this bootstrap as root inside WSL."
  exit 1
fi

if [ ! -d "$STAGED_SOURCE/ubuntu-backend-deploy" ]; then
  echo "[ERROR] Staged source not found: $STAGED_SOURCE"
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y \
  systemd systemd-sysv sudo git git-lfs curl ca-certificates \
  ffmpeg build-essential python3 python3-venv python3-pip \
  rsync nginx apache2-utils libsndfile1 libgl1 libglib2.0-0

if ! id "$SERVICE_USER" >/dev/null 2>&1; then
  useradd --create-home --shell /bin/bash "$SERVICE_USER"
fi

install -d -o "$SERVICE_USER" -g "$SERVICE_USER" \
  "$APP_ROOT" "$COMFY_ROOT" \
  "$RUNTIME_ROOT/models" "$RUNTIME_ROOT/cache/huggingface" \
  "$RUNTIME_ROOT/cache/torch" "$RUNTIME_ROOT/logs" "$RUNTIME_ROOT/outputs"

# 仅同步可版本化的代码。运行期配置、模型、缓存、日志和产物属于服务器状态，
# 绝不能在普通更新时被源目录的默认值或 --delete 覆盖。
rsync -a --delete \
  --exclude=.git \
  --exclude=.env \
  --exclude=.venv \
  --exclude=.cache \
  --exclude=models \
  --exclude=outputs \
  --exclude=logs \
  --exclude=yzy_config.json \
  "$STAGED_SOURCE/" "$APP_ROOT/"
chown -R "$SERVICE_USER:$SERVICE_USER" "$RUNTIME_ROOT"

BACKEND_DIR="$APP_ROOT/ubuntu-backend-deploy"
QWEN_DIR="$APP_ROOT/llm-backend-deploy"

if [ ! -f "$BACKEND_DIR/.env" ]; then
  cp "$BACKEND_DIR/.env.example" "$BACKEND_DIR/.env"
  sed -i \
    -e "s#^COMFYUI_ROOT=.*#COMFYUI_ROOT=$COMFY_ROOT#" \
    -e "s#^COMFYUI_PYTHON=.*#COMFYUI_PYTHON=$BACKEND_DIR/.venv/bin/python#" \
    -e "s#^HF_HOME=.*#HF_HOME=$RUNTIME_ROOT/cache/huggingface#" \
    -e "s#^TORCH_HOME=.*#TORCH_HOME=$RUNTIME_ROOT/cache/torch#" \
    "$BACKEND_DIR/.env"
fi
if [ ! -f "$QWEN_DIR/.env" ]; then
  cp "$QWEN_DIR/.env.example" "$QWEN_DIR/.env"
  sed -i \
    -e "s#^QWEN_LOCAL_MODEL_DIR=.*#QWEN_LOCAL_MODEL_DIR=$RUNTIME_ROOT/models/Qwen3.6-35B-A3B-AWQ-4bit#" \
    -e "s#^HF_HOME=.*#HF_HOME=$RUNTIME_ROOT/cache/huggingface#" \
    "$QWEN_DIR/.env"
fi

cat >/etc/wsl.conf <<EOF
[boot]
systemd=true

[user]
default=$SERVICE_USER
EOF

echo "[OK] WSL bootstrap complete. Run 'wsl --shutdown' on Windows, then continue deployment."
