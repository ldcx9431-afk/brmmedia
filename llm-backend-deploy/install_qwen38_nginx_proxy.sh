#!/usr/bin/env bash
# Update only the Qwen location in the WSL Nginx site after the selected engine
# is healthy. The Windows Qwen3.8 endpoint is reachable only through the WSL
# NAT gateway and a firewall rule limited to the WSL address range.
set -euo pipefail
target="${1:-}"
case "$target" in qwen38|qwen35) ;; *) echo "Usage: $0 qwen38|qwen35" >&2; exit 2;; esac
site=/etc/nginx/sites-enabled/brmmedia
snippet=/etc/nginx/snippets/brmmedia-qwen-upstream.conf
mkdir -p /etc/nginx/snippets /etc/brmmedia
if [ "$target" = qwen38 ]; then
  host_gateway="$(awk '/^nameserver / {print $2; exit}' /etc/resolv.conf)"
  [ -n "$host_gateway" ] || { echo "Windows WSL gateway is unavailable" >&2; exit 1; }
  upstream="http://$host_gateway:8001/"
  model=qwen38-27b-ud-q4-xl
  backend_api="http://$host_gateway:8001/v1"
else
  upstream=http://127.0.0.1:8000/
  model=qwen35-4b-awq
  backend_api=http://127.0.0.1:8000/v1
fi

tmp="${snippet}.tmp"
cat > "$tmp" <<EOF
location /qwen/ {
    proxy_pass $upstream;
    proxy_http_version 1.1;
    proxy_set_header Host \$host;
    proxy_set_header X-Real-IP \$remote_addr;
    proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto \$scheme;
    proxy_read_timeout 3700s;
    proxy_send_timeout 3700s;
    proxy_buffering off;
    client_max_body_size 32M;
}
EOF

if ! grep -Fq 'brmmedia-qwen-upstream.conf' "$site"; then
  backup="${site}.qwen38-backup.$(date +%Y%m%d%H%M%S)"
  cp -a "$site" "$backup"
  python3 - "$site" <<'PY'
from pathlib import Path
import re, sys
p = Path(sys.argv[1])
s = p.read_text()
old = re.compile(r'\n    location /qwen/ \{.*?\n    \}\n', re.S)
s, count = old.subn('\n    include /etc/nginx/snippets/brmmedia-qwen-upstream.conf;\n', s, count=1)
if count != 1:
    raise SystemExit('Unable to locate the existing /qwen/ Nginx location.')
p.write_text(s)
PY
fi
mv -f "$tmp" "$snippet"
if ! nginx -t; then
  echo "Nginx validation failed; leaving the previous active service untouched." >&2
  exit 1
fi
systemctl reload nginx
cat > /etc/brmmedia/qwen-active.env <<EOF
QWEN_ACTIVE_MODEL=$model
QWEN_ACTIVE_PORT=$([ "$target" = qwen38 ] && echo 8001 || echo 8000)
QWEN_ACTIVE_UPSTREAM=$upstream
EOF
backend_env=/srv/brmmedia/app/ubuntu-backend-deploy/.env
if [ -f "$backend_env" ]; then
  python3 - "$backend_env" "$backend_api" "$model" <<'PY'
from pathlib import Path
import re, sys
p, api, model = map(str, sys.argv[1:])
s = Path(p).read_text()
def setenv(text, name, value):
    line = f"{name}={value}"
    pattern = rf"(?m)^{re.escape(name)}=.*$"
    return re.sub(pattern, line, text) if re.search(pattern, text) else text.rstrip() + "\n" + line + "\n"
s = setenv(s, "BRM_QWEN_API_BASE", api)
s = setenv(s, "BRM_QWEN_MODEL", model)
Path(p).write_text(s)
PY
  # Avoid silently interrupting a media job while merely switching the Qwen UI.
  queue="$(curl --silent --max-time 5 http://127.0.0.1:8188/queue 2>/dev/null || true)"
  if printf '%s' "$queue" | grep -Eq '"queue_(running|pending)"[[:space:]]*:[[:space:]]*\[[[:space:]]*[^][:space:]]'; then
    echo "[WARN] Media queue is active; backend restart is deferred. Restart baorong-backend after it drains." >&2
  else
    systemctl restart baorong-backend
  fi
fi
echo "[OK] Nginx /qwen/v1 now proxies $model"
