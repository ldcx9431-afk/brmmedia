#!/usr/bin/env bash
# Start a resumable Unsloth Qwen3.8 UD-Q4_K_XL download from WSL.  Keeping the
# backgrounding inside this script avoids Windows OpenSSH terminating curl when
# the SSH session exits.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT="brmmedia-qwen38-download"

if systemctl is-active --quiet "$UNIT"; then
  echo "Download already running as ${UNIT}.service."
  exit 0
fi

# `systemd-run` owns the process independently of the SSH/WSL caller. Nohup is
# insufficient here because Windows may terminate descendant processes when an
# OpenSSH command session ends.
systemd-run --unit="$UNIT" --collect --property=Nice=10 \
  "$SCRIPT_DIR/download_qwen38_udq4_foreground.sh"
echo "Qwen3.8 download started as ${UNIT}.service."
