#!/usr/bin/env bash
# Atomically switch the stable /qwen/v1 Nginx path between the Qwen3.8 default
# and the retained Qwen3.5 cold standby. Run as root.
set -euo pipefail

target="${1:-}"
[ "$(id -u)" -eq 0 ] || { echo "Run as root." >&2; exit 2; }
case "$target" in qwen38|qwen35) ;; *) echo "Usage: $0 qwen38|qwen35" >&2; exit 2;; esac

lock=/run/lock/brmmedia-qwen-switch.lock
mkdir -p "$(dirname "$lock")" /etc/brmmedia /etc/nginx/snippets
exec 9>"$lock"
flock -n 9 || { echo "Another Qwen switch is in progress." >&2; exit 1; }

if [ "$target" = qwen38 ]; then
  target_service=qwen38-llama; target_port=8001; target_model=qwen38-27b-ud-q4-xl
  profile=/srv/brmmedia/app/llm-backend-deploy/.env.qwen38-27b
  if [ -f "$profile" ]; then
    set -a
    # shellcheck disable=SC1090
    source "$profile"
    set +a
    target_port="${QWEN38_PORT:-$target_port}"
    target_model="${QWEN38_SERVED_MODEL_NAME:-$target_model}"
  fi
  previous_service=qwen-vllm
else
  target_service=qwen-vllm; target_port=8000; target_model=qwen35-4b-awq
  previous_service=qwen38-llama
fi

rollback() {
  echo "[WARN] $target did not become healthy; restoring $previous_service." >&2
  systemctl stop "$target_service" || true
  systemctl start "$previous_service" || true
}

systemctl stop "$previous_service" || true
systemctl start "$target_service"
for _ in $(seq 1 120); do
  if curl --silent --fail --max-time 5 "http://127.0.0.1:$target_port/v1/models" | grep -Fq "$target_model"; then
    break
  fi
  sleep 2
done
if ! curl --silent --fail --max-time 5 "http://127.0.0.1:$target_port/v1/models" | grep -Fq "$target_model"; then
  rollback
  exit 1
fi

snippet=/etc/nginx/snippets/brmmedia-qwen-upstream.conf
tmp="${snippet}.tmp"
site=/etc/nginx/sites-enabled/brmmedia
site_backup=""
snippet_backup=""
if ! grep -Fq 'brmmedia-qwen-upstream.conf' "$site"; then
  site_backup="${site}.qwen38-backup.$(date +%Y%m%d%H%M%S)"
  cp -a "$site" "$site_backup"
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
cat > "$tmp" <<EOF
location /qwen/ {
    proxy_pass http://127.0.0.1:$target_port/;
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
[ -f "$snippet" ] && snippet_backup="${snippet}.qwen38-backup.$(date +%Y%m%d%H%M%S)" && cp -a "$snippet" "$snippet_backup"
mv -f "$tmp" "$snippet"
if ! nginx -t; then
  [ -n "$site_backup" ] && cp -a "$site_backup" "$site"
  if [ -n "$snippet_backup" ]; then cp -a "$snippet_backup" "$snippet"; else rm -f "$snippet"; fi
  nginx -t || true
  rollback
  exit 1
fi
systemctl reload nginx
cat > /etc/brmmedia/qwen-active.env <<EOF
QWEN_ACTIVE_MODEL=$target_model
QWEN_ACTIVE_SERVICE=$target_service
QWEN_ACTIVE_PORT=$target_port
EOF

# The Gradio Qwen tab calls the local OpenAI-compatible endpoint directly;
# keep it aligned with the authenticated Nginx route without exposing another
# LAN listener.  Restart only if ComfyUI has no active media work.
backend_env=/srv/brmmedia/app/ubuntu-backend-deploy/.env
if [ -f "$backend_env" ]; then
  python3 - "$backend_env" "http://127.0.0.1:$target_port/v1" "$target_model" <<'PY'
from pathlib import Path
import re, sys
p = Path(sys.argv[1])
api, model = map(str, sys.argv[2:])
s = Path(p).read_text()
def setenv(text, name, value):
    line = f"{name}={value}"
    pattern = rf"(?m)^{re.escape(name)}=.*$"
    return re.sub(pattern, line, text) if re.search(pattern, text) else text.rstrip() + "\n" + line + "\n"
s = setenv(s, "BRM_QWEN_API_BASE", api)
s = setenv(s, "BRM_QWEN_MODEL", model)
p.write_text(s)
PY
  queue="$(curl --silent --max-time 5 http://127.0.0.1:8188/queue 2>/dev/null || true)"
  if printf '%s' "$queue" | grep -Eq '"queue_(running|pending)"[[:space:]]*:[[:space:]]*\[[[:space:]]*[^][:space:]]'; then
    echo "[WARN] Media queue is active; baorong-backend restart is deferred." >&2
  else
    systemctl restart baorong-backend
  fi
fi
systemctl disable --now "$previous_service" || true
systemctl enable "$target_service"
echo "[OK] /qwen/v1 now serves $target_model on $target_service."
