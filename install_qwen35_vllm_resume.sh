#!/usr/bin/env bash
set -euo pipefail

# Durable installer for Qwen3.5's vLLM runtime.  It is intentionally kept on
# the Windows D: source volume so a WSL VM restart can resume from the D: pip
# cache without relying on transient files in /srv.
SOURCE_DIR=/mnt/d/brmmedia/source/llm-backend-deploy
APP_DIR=/srv/brmmedia/app/llm-backend-deploy
PROXY_URL="${BRMMEDIA_PROXY_URL:-http://192.168.1.254:8888}"

install -m 700 "$SOURCE_DIR/install_qwen_vllm.sh" "$APP_DIR/install_qwen_vllm.sh"
install -m 600 "$SOURCE_DIR/.env.qwen35-4b.example" "$APP_DIR/.env"

export HTTP_PROXY="$PROXY_URL"
export HTTPS_PROXY="$PROXY_URL"
export PIP_CACHE_DIR=/mnt/d/brmmedia/artifacts/pip-cache-qwen35
export PREFETCH_MODEL=0
export VLLM_EXTRA_INDEX_URL=https://wheels.vllm.ai/nightly
export VLLM_USE_UV=true
export VLLM_TORCH_BACKEND=auto

cd "$APP_DIR"
exec ./install_qwen_vllm.sh
