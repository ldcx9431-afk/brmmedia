#!/usr/bin/env python3
"""Static safeguards for the A5000-media/A4000-Qwen recovery profile."""

from __future__ import annotations

import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


class DeploymentProfileTests(unittest.TestCase):
    def test_bootstrap_uses_the_supported_qwen_profile(self):
        text = (REPO_ROOT / "bootstrap_wsl.sh").read_text(encoding="utf-8")
        self.assertIn('cp "$QWEN_DIR/.env.qwen35-4b.example" "$QWEN_DIR/.env"', text)
        self.assertIn("Qwen3.5-4B-AWQ-4bit", text)
        self.assertNotIn('cp "$QWEN_DIR/.env.example" "$QWEN_DIR/.env"', text)

    def test_generic_qwen_example_preserves_gpu_split(self):
        text = (REPO_ROOT / "llm-backend-deploy" / ".env.example").read_text(encoding="utf-8")
        self.assertIn("QWEN_CUDA_VISIBLE_DEVICES=1", text)
        self.assertIn("QWEN_MODEL=cyankiwi/Qwen3.5-4B-AWQ-4bit", text)
        self.assertIn("QWEN_GPU_MEMORY_UTILIZATION=0.70", text)
        self.assertNotIn("QWEN_CUDA_VISIBLE_DEVICES=0", text)

    def test_runtime_verifier_uses_installed_release_record(self):
        verifier = (REPO_ROOT / "brmmedia-verify-runtime.sh").read_text(encoding="utf-8")
        installer = (REPO_ROOT / "install_ubuntu_systemd_services.sh").read_text(encoding="utf-8")
        self.assertIn("/etc/brmmedia/runtime.env", verifier)
        self.assertIn("runtime_env_value BRMMEDIA_APP_ROOT", verifier)
        self.assertIn("BRMMEDIA_SOURCE_ROOT", installer)
        self.assertIn("/etc/brmmedia/runtime.env", installer)

    def test_h3_activation_uses_candidate_env_and_restores_on_start_failure(self):
        activation = (REPO_ROOT / "activate_minimax_h3.sh").read_text(encoding="utf-8")
        preparation = (REPO_ROOT / "prepare_minimax_h3_comfyui.sh").read_text(encoding="utf-8")
        self.assertIn("dotenv_value COMFYUI_ROOT", activation)
        self.assertIn("restore_after_failed_activation", activation)
        self.assertIn("trap restore_after_failed_activation ERR", activation)
        self.assertIn("Stopping partially started H3 backend before rollback", activation)
        self.assertIn("systemctl stop baorong-backend", activation)
        self.assertIn("rollback_minimax_h3_comfyui.sh", activation)
        self.assertIn("BRMMEDIA_H3_STARTUP_HEALTH_TIMEOUT_SECONDS", activation)
        self.assertIn("/gradio_api/info", activation)
        self.assertIn("/system_stats", activation)
        self.assertIn("systemctl restart qwen-vllm", activation)
        self.assertIn("/v1/models", activation)
        self.assertIn("Validating all ComfyUI workflow nodes", activation)
        self.assertIn("validate_comfy_workflows.py", activation)
        self.assertIn("563b98eefbe643a4cd510ee7f0b43e79880d5a3f", preparation)
        self.assertNotIn("15989f87ca89bfe2e7c47763252c559e96d97551", preparation)

    def test_qwen_acceptance_is_loopback_and_records_non_secret_evidence(self):
        acceptance = (REPO_ROOT / "accept_qwen_vllm.sh").read_text(encoding="utf-8")
        self.assertIn("http://127.0.0.1:8000/v1", acceptance)
        self.assertIn("/chat/completions", acceptance)
        self.assertIn('"enable_thinking": False', acceptance)
        self.assertIn('QWEN_RESPONSE="$response"', acceptance)
        self.assertIn('"runs": int(runs)', acceptance)
        self.assertNotIn("BRM_PASSWORD", acceptance)

    def test_h3_acceptance_can_verify_task_recovery_across_restart(self):
        acceptance = (REPO_ROOT / "accept_minimax_h3_video.sh").read_text(encoding="utf-8")
        self.assertIn("--restart-recovery", acceptance)
        self.assertIn("verify_completed_task_after_restart", acceptance)
        self.assertIn("systemctl restart", acceptance)
        self.assertIn('"$API_BASE/tasks/$task_id"', acceptance)
        self.assertIn("persisted H3 task recovery", acceptance)

    def test_h3_candidate_refresh_rejects_active_service_paths(self):
        refresh = (REPO_ROOT / "refresh_h3_runtime_release.sh").read_text(encoding="utf-8")
        self.assertIn('/srv/brmmedia/releases/h3-*', refresh)
        self.assertIn('do not refresh an active service path', refresh)
        self.assertIn("--exclude 'ubuntu-backend-deploy/.env'", refresh)
        self.assertIn("--exclude 'ubuntu-backend-deploy/outputs'", refresh)

    def test_h3_worker_inherits_the_stage_chunk_size(self):
        downloader = (REPO_ROOT / "start_minimax_h3_download.sh").read_text(encoding="utf-8")
        self.assertIn('--setenv="BRMMEDIA_H3_CHUNK_BYTES=$CHUNK_BYTES"', downloader)
        self.assertIn("BRMMEDIA_H3_MIN_CHUNK_BYTES", downloader)
        self.assertIn("will retry with", downloader)

    def test_h3_canary_stays_off_the_production_lan_ports(self):
        prepare = (REPO_ROOT / "prepare_minimax_h3_canary.sh").read_text(encoding="utf-8")
        starter = (REPO_ROOT / "start_minimax_h3_canary.sh").read_text(encoding="utf-8")
        guide = (REPO_ROOT / "WSL_TEST_DEPLOY.md").read_text(encoding="utf-8")
        self.assertIn("ComfyUI-h3-canary", prepare)
        self.assertIn("BRM_GRADIO_PORT 9001", prepare)
        self.assertIn("BRMMEDIA_VIDEO_ENGINE h3", prepare)
        self.assertIn("baorong-backend-h3-canary", starter)
        self.assertIn("--port 9101", starter)
        self.assertIn("Nginx is never repointed to the canary", guide)

    def test_current_ubuntu_guide_does_not_recommend_highvram(self):
        guide = (REPO_ROOT / "ubuntu-backend-deploy" / "README_UBUNTU_DEPLOY.md").read_text(encoding="utf-8")
        self.assertIn("禁止 `--highvram`、`--gpu-only`", guide)
        self.assertNotIn("echo 'COMFYUI_ARGS=--highvram'", guide)

    def test_h3_deployment_guide_matches_range_downloader(self):
        guide = (REPO_ROOT / "WSL_TEST_DEPLOY.md").read_text(encoding="utf-8")
        self.assertIn("`curl`", guide)
        self.assertIn("HTTP Range", guide)
        self.assertNotIn("下载器默认使用 HF CLI", guide)
