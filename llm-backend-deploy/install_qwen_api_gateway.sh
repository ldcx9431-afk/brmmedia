#!/usr/bin/env bash
# Install the local Qwen Bearer API-key gateway. Run as root on the WSL host.
set -euo pipefail

[ "$(id -u)" -eq 0 ] || { echo "Run as root." >&2; exit 2; }

APP_DIR="${BRM_APP_DIR:-/srv/brmmedia/app}"
RUN_AS="${BRM_RUN_AS:-brm}"
SOURCE="$APP_DIR/ubuntu-backend-deploy/qwen_api_gateway.py"
UNIT_SOURCE="$APP_DIR/ubuntu-backend-deploy/brmmedia-qwen-api-gateway.service.example"
VENV_PYTHON="$APP_DIR/ubuntu-backend-deploy/.venv/bin/python"
SERVICE=/etc/systemd/system/brmmedia-qwen-api-gateway.service
ENV_FILE=/etc/brmmedia/qwen-api-gateway.env

for file in "$SOURCE" "$UNIT_SOURCE" "$VENV_PYTHON"; do
  [ -e "$file" ] || { echo "Missing required file: $file" >&2; exit 1; }
done
id "$RUN_AS" >/dev/null

install -d -m 0750 -o "$RUN_AS" -g "$RUN_AS" /var/lib/brmmedia /etc/brmmedia
if [ ! -f "$ENV_FILE" ]; then
  install -m 0600 -o root -g "$RUN_AS" /dev/null "$ENV_FILE"
fi

sed \
  -e "s|User=YOUR_USER|User=$RUN_AS|" \
  -e "s|/srv/brmmedia/app|$APP_DIR|g" \
  "$UNIT_SOURCE" > "$SERVICE"
chmod 0644 "$SERVICE"

systemctl daemon-reload
systemctl enable --now brmmedia-qwen-api-gateway
systemctl --no-pager --full status brmmedia-qwen-api-gateway
