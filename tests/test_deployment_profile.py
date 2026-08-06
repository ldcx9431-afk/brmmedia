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

