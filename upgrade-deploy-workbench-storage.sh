#!/usr/bin/env bash
set -euo pipefail
umask 077
app=$(systemctl show baorong-backend -p WorkingDirectory --value)
[[ "$app" == /srv/brmmedia/releases/*/ubuntu-backend-deploy ]]
[[ "$app" == "$(systemctl show brmmedia-lan-api -p WorkingDirectory --value)" ]]
for unit in nginx brmmedia-healthcheck.timer baorong-backend brmmedia-lan-api; do systemctl is-active --quiet "$unit"; done
state=/var/lib/brmmedia/workbench-nvme
old_history="$app/outputs/task-history.json"
drop=/etc/systemd/system/baorong-backend.service.d/70-workbench-storage.conf
[[ ! -e "$drop" && ! -e "$state" ]]
backup=$(mktemp -d /srv/brmmedia/backups/workbench-storage.XXXXXXXX)
cp -a "$app/webui.py" "$backup/webui.py"
python3 - "$backup" <<'PY'
import sys
from pathlib import Path
folder = Path(sys.argv[1])
source = (folder / 'webui.py').read_text(encoding='utf-8')
changes = {
    'TASK_HISTORY_PATH = OUTPUT_DIR / "task-history.json"': 'TASK_HISTORY_PATH = Path(os.environ.get("BRM_TASK_HISTORY_PATH", OUTPUT_DIR / "task-history.json")).expanduser().resolve()',
    'MEDIA_CACHE_DIR = OUTPUT_DIR / ".brm-cache"': 'MEDIA_CACHE_DIR = Path(os.environ.get("BRM_MEDIA_CACHE_DIR", OUTPUT_DIR / ".brm-cache")).expanduser().resolve()',
    'OUTPUT_DIR.mkdir(parents=True, exist_ok=True)\n            temp_path = TASK_HISTORY_PATH': 'TASK_HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)\n            temp_path = TASK_HISTORY_PATH',
    'and MEDIA_CACHE_DIR not in p.parents\n': 'and MEDIA_CACHE_DIR not in p.parents and OUTPUT_DIR / ".brm-cache" not in p.parents\n',
    '        root_path=GRADIO_ROOT_PATH,\n': '        root_path=GRADIO_ROOT_PATH,\n        allowed_paths=[str(THUMB_CACHE_DIR), str(DOWNLOAD_CACHE_DIR)],\n',
}
for old, new in changes.items():
    if source.count(old) != 1:
        raise SystemExit('Unexpected production source for ' + old)
    source = source.replace(old, new)
compile(source, 'webui.py', 'exec')
(folder / 'webui-new.py').write_text(source, encoding='utf-8')
PY
changed=0
restore() {
  local rc=$?
  if (( rc != 0 && changed )); then
    systemctl stop brmmedia-lan-api baorong-backend
    cp -a "$backup/webui.py" "$app/.webui.storage-rollback.py"
    mv -T "$app/.webui.storage-rollback.py" "$app/webui.py"
    rm -f -- "$drop"
    systemctl daemon-reload
  fi
  if (( rc != 0 )); then
    systemctl start baorong-backend brmmedia-lan-api nginx brmmedia-healthcheck.timer
  fi
  exit "$rc"
}
trap restore EXIT
systemctl stop brmmedia-healthcheck.timer nginx
python3 - "$old_history" <<'PY'
import json, sys, urllib.request
from pathlib import Path
get = urllib.request.build_opener(urllib.request.ProxyHandler({})).open
for port in (8188, 8190, 8191):
    with get(f'http://127.0.0.1:{port}/queue', timeout=4) as response:
        queue = json.load(response)
    if queue['queue_running'] or queue['queue_pending']:
        raise SystemExit(f'Queue busy on {port}')
records = json.loads(Path(sys.argv[1]).read_bytes())['tasks']
if any(task['status'] in ('\u6392\u961f\u4e2d', '\u5904\u7406\u4e2d') for task in records):
    raise SystemExit('Task history contains active work')
print(f'Drained. Existing task records: {len(records)}')
PY
systemctl stop brmmedia-lan-api baorong-backend
cp -p "$old_history" "$backup/task-history.json"
install -d -m 0750 -o brm -g brm "$state" "$state/cache" "$state/cache/thumbnails" "$state/cache/downloads"
install -m 0640 -o brm -g brm "$backup/task-history.json" "$state/task-history.json"
changed=1
install -m 0644 -o brm -g brm "$backup/webui-new.py" "$app/.webui.storage-release.py"
mv -T "$app/.webui.storage-release.py" "$app/webui.py"
printf '[Service]\nEnvironment=BRM_TASK_HISTORY_PATH=%s/task-history.json\nEnvironment=BRM_MEDIA_CACHE_DIR=%s/cache\n' "$state" "$state" > "$drop"
systemctl daemon-reload
systemctl start baorong-backend brmmedia-lan-api
ready=0
for i in $(seq 1 45); do
  if curl --noproxy '*' -fsS --max-time 2 http://127.0.0.1:9100/api/v1/health | python3 -c 'import json,sys; assert json.load(sys.stdin)["status"] == "ok"' 2>/dev/null; then ready=1; break; fi
  sleep 2
done
((ready))
systemctl start nginx brmmedia-healthcheck.timer
printf 'DEPLOYED backup=%s\n' "$backup"
