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
        self.assertIn("dotenv_value COMFYUI_ROOT", activation)
        self.assertIn("restore_after_failed_activation", activation)
        self.assertIn("trap restore_after_failed_activation ERR", activation)
        self.assertIn("rollback_minimax_h3_comfyui.sh", activation)

    def test_qwen_acceptance_is_loopback_and_records_non_secret_evidence(self):
        acceptance = (REPO_ROOT / "accept_qwen_vllm.sh").read_text(encoding="utf-8")
        self.assertIn("http://127.0.0.1:8000/v1", acceptance)
        self.assertIn("/chat/completions", acceptance)
        self.assertIn('"runs": int(runs)', acceptance)
        self.assertNotIn("BRM_PASSWORD", acceptance)
