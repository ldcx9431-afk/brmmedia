#!/usr/bin/env bash
# Refresh a pre-staged E: H3 candidate from its clean D: Git release without
# touching the active recovery app, persistent outputs, virtualenvs or dotenvs.
set -euo pipefail

SOURCE_RELEASE="${1:?usage: sudo $0 <clean-source-release> <candidate-runtime-root>}"
TARGET_ROOT="${2:?usage: sudo $0 <clean-source-release> <candidate-runtime-root>}"

if [ "$(id -u)" -ne 0 ]; then
  echo "[ERROR] Run with sudo." >&2
  exit 1
fi
SOURCE_RELEASE="$(readlink -f "$SOURCE_RELEASE")"
TARGET_ROOT="$(readlink -f "$TARGET_ROOT")"
case "$TARGET_ROOT" in
  /srv/brmmedia/releases/h3-*) ;;
  *) echo "[ERROR] Candidate must be an explicit /srv/brmmedia/releases/h3-* directory: $TARGET_ROOT" >&2; exit 1 ;;
esac
if [ ! -d "$SOURCE_RELEASE/.git" ] || [ ! -f "$SOURCE_RELEASE/ubuntu-backend-deploy/webui.py" ]; then
  echo "[ERROR] Source is not a clean BRMMedia Git release: $SOURCE_RELEASE" >&2
  exit 1
fi
if [ ! -d "$TARGET_ROOT" ] || [ ! -f "$TARGET_ROOT/ubuntu-backend-deploy/.env" ]; then
  echo "[ERROR] Candidate was not staged or has no independent backend .env: $TARGET_ROOT" >&2
  exit 1
fi

# Once a service is already pointed at the candidate, a refresh could make a
# running code/config pair inconsistent.  Refresh before installing candidate
# systemd units; activation separately insists the queue is drained.
for service in baorong-backend brmmedia-lan-api qwen-vllm; do
  if systemctl cat "$service" 2>/dev/null | grep -Fq "$TARGET_ROOT/"; then
    echo "[ERROR] $service is already installed for this candidate; do not refresh an active service path." >&2
    exit 1
  fi
done

rsync -a --delete \
  --exclude '.git' \
  --exclude 'ubuntu-backend-deploy/.env' \
  --exclude 'llm-backend-deploy/.env' \
  --exclude 'ubuntu-backend-deploy/.venv' \
  --exclude 'llm-backend-deploy/.venv' \
  --exclude 'ubuntu-backend-deploy/outputs' \
  --exclude 'ubuntu-backend-deploy/yzy_config.json' \
  --exclude 'runtime-locks' \
  "$SOURCE_RELEASE/" "$TARGET_ROOT/"

# `runtime-locks` also contains the candidate's large venvs, build artifacts
# and rollback state, so it must never be deleted as part of a code refresh.
# Copy the reviewed immutable lock manifests explicitly, however; otherwise a
# refreshed release could run new preparation code with a stale runtime pin.
mkdir -p "$TARGET_ROOT/runtime-locks"
for lock_manifest in \
  backend.requirements.lock \
  comfyui-custom-nodes.lock.tsv \
  h3-comfyui-v0.32.0.env \
  minimax-h3-target-profile-2026-08-05.md \
  production-profile-2026-08-03.md \
  qwen35-4b.requirements.lock; do
  install -m 0644 "$SOURCE_RELEASE/runtime-locks/$lock_manifest" \
    "$TARGET_ROOT/runtime-locks/$lock_manifest"
done

critical=(
  'ubuntu-backend-deploy/webui.py'
  'ubuntu-backend-deploy/lan_api.py'
  'ubuntu-backend-deploy/comfyui_server.py'
  'ubuntu-backend-deploy/requirements-backend.txt'
  'ubuntu-backend-deploy/workflows/MiniMaxH3-文生视频.json'
  'ubuntu-backend-deploy/workflows/MiniMaxH3-图生视频.json'
  'runtime-locks/h3-comfyui-v0.32.0.env'
  'configure_h3_canary_ab.sh'
  'verify_h3_canary_runtime.sh'
  'download_minimax_h3_turbo_models.sh'
  'import_minimax_h3_turbo_models.sh'
  'accept_minimax_h3_video.sh'
  'accept_qwen_vllm.sh'
  'activate_minimax_h3.sh'
  'brmmedia-verify-runtime.sh'
)
for relative in "${critical[@]}"; do
  if ! cmp -s "$SOURCE_RELEASE/$relative" "$TARGET_ROOT/$relative"; then
    echo "[ERROR] Candidate synchronization mismatch: $relative" >&2
    exit 1
  fi
done
printf '[OK] Refreshed inactive H3 candidate: %s\n' "$TARGET_ROOT"
printf '     source_commit=%s\n' "$(git -c safe.directory="$SOURCE_RELEASE" -C "$SOURCE_RELEASE" rev-parse --short HEAD)"
