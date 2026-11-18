#!/usr/bin/env bash
set -euo pipefail

production_dir="/srv/brmmedia/releases/h3-23d7084/ubuntu-backend-deploy"
qa_dir="/mnt/c/Users/deploy/brm-ui-qa"
venv="/srv/brmmedia/releases/h3-23d7084/runtime-locks/venvs/h3-v032-canary"

set -a
source "$production_dir/.env"
set +a
export BRM_GRADIO_HOST="127.0.0.1"
export BRM_GRADIO_PORT="9011"
export BRM_QA_HOST="127.0.0.1"
export BRM_QA_PORT="9011"
export PYTHONPATH="$qa_dir:$production_dir"

cd "$qa_dir"
exec "$venv/bin/python" -u qa_preview.py
