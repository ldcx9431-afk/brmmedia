#!/usr/bin/env bash
# Stage a clean Git release on the E: WSL filesystem without touching the
# existing working runtime.  The old app remains an immediate service rollback
# target, while outputs/venvs are shared deliberately. Runtime .env files are
# copied so an H3 activation cannot mutate the recovery configuration.
set -euo pipefail

SOURCE_RELEASE="${1:?usage: sudo $0 <clean-source-release> [runtime-release-root]}"
ACTIVE_ROOT="${BRMMEDIA_ACTIVE_APP_ROOT:-/srv/brmmedia/app}"
TARGET_ROOT="${2:-/srv/brmmedia/releases/$(basename "$SOURCE_RELEASE")}" 
BACKEND_REL="ubuntu-backend-deploy"

if [ "$(id -u)" -ne 0 ]; then
  echo "[ERROR] Run with sudo." >&2
  exit 1
fi
if [ ! -d "$SOURCE_RELEASE/.git" ] || [ ! -f "$SOURCE_RELEASE/$BACKEND_REL/webui.py" ]; then
  echo "[ERROR] Source must be a clean BRMMedia Git release: $SOURCE_RELEASE" >&2
  exit 1
fi
if [ ! -d "$ACTIVE_ROOT/$BACKEND_REL" ]; then
  echo "[ERROR] Active runtime is missing: $ACTIVE_ROOT" >&2
  exit 1
fi
if [ -e "$TARGET_ROOT" ]; then
  echo "[ERROR] Target release already exists: $TARGET_ROOT" >&2
  exit 1
fi

install -d -m 0755 "$TARGET_ROOT"
rsync -a --delete --exclude '.git' "$SOURCE_RELEASE/" "$TARGET_ROOT/"

# The release must use the verified virtual environments and persistent user
# state, not make duplicate caches or make prior output history disappear.
for relative in "$BACKEND_REL/.venv" "llm-backend-deploy/.venv"; do
  source_path="$ACTIVE_ROOT/$relative"
  target_path="$TARGET_ROOT/$relative"
  if [ -d "$source_path" ]; then
    ln -s "$source_path" "$target_path"
  fi
done
for relative in "$BACKEND_REL/outputs" "$BACKEND_REL/yzy_config.json"; do
  source_path="$ACTIVE_ROOT/$relative"
  target_path="$TARGET_ROOT/$relative"
  if [ -e "$source_path" ]; then
    rm -rf "$target_path"
    ln -s "$source_path" "$target_path"
  fi
done
for relative in "$BACKEND_REL/.env" "llm-backend-deploy/.env"; do
  source_path="$ACTIVE_ROOT/$relative"
  target_path="$TARGET_ROOT/$relative"
  if [ -f "$source_path" ]; then
    # Copy, do not link: release-specific engine/GPU settings must remain
    # independently reversible even though output history is shared.
    install -m 0600 "$source_path" "$target_path"
  fi
done

chown -R brm:brm "$TARGET_ROOT"
printf '[OK] Staged H3 runtime release: %s\n' "$TARGET_ROOT"
printf '     source_commit=%s\n' "$(git -c safe.directory="$SOURCE_RELEASE" -C "$SOURCE_RELEASE" rev-parse --short HEAD)"
printf '     old_runtime_preserved=%s\n' "$ACTIVE_ROOT"
printf "Next: after model import, stop media services and run this release's install_ubuntu_systemd_services.sh with %s.\n" "$TARGET_ROOT"
