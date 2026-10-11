#!/usr/bin/env bash
# Research/evaluation only. Does not modify the existing ComfyUI or its venv.
set -euo pipefail
task_source=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
task_root=${BRM_QWEN_IMAGE21_ROOT:-/srv/brmmedia/qwen-image21}
task_base_venv=${BRMMEDIA_BACKEND_VENV:-/srv/brmmedia/releases/h3-23d7084/runtime-locks/venvs/h3-v032-canary}
task_commit=7f7fd918ed0815c5d5ef2ccfe44bfc8e72cfd217
test -x "$task_base_venv/bin/python"
mkdir -p "$task_root/models" "$task_root/deploy"
if [ ! -e "$task_root/ComfyUI" ]; then
 git clone https://github.com/Comfy-Org/ComfyUI.git "$task_root/ComfyUI"
fi
if [ -n "$(git -C "$task_root/ComfyUI" status --porcelain --untracked-files=no)" ]; then
 echo 'Refusing to replace a dirty research runtime' >&2; exit 1
fi
git -C "$task_root/ComfyUI" checkout --detach "$task_commit"
"$task_base_venv/bin/python" -m venv --system-site-packages "$task_root/venv"
task_base_site=$("$task_base_venv/bin/python" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')
task_new_site=$("$task_root/venv/bin/python" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')
printf '%s\n' "$task_base_site" > "$task_new_site/production-readonly.pth"
"$task_root/venv/bin/python" -m pip install -r "$task_root/ComfyUI/requirements.txt"
printf 'qwen21:\n  base_path: %s/models\n  diffusion_models: diffusion_models\n  text_encoders: text_encoders\n  vae: vae\n' "$task_root" > "$task_root/extra_models.yaml"
"$task_root/venv/bin/python" -m pip freeze > "$task_root/requirements.lock"
install -m 644 "$task_source/download_qwen_image21.py" "$task_root/deploy/"
install -m 644 "$task_source/qwen_image21_support.py" "$task_root/deploy/"
"$task_root/venv/bin/python" -u "$task_root/deploy/download_qwen_image21.py" --models "$task_root/models" --parallel
echo 'Research runtime and weights staged. Run generation acceptance BEFORE enabling the service/workbench.'
