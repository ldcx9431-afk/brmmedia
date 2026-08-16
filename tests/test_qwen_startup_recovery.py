#!/usr/bin/env python3
"""Static guardrails for Qwen warmup availability in the Gradio workspace."""

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
WEBUI = ROOT / "ubuntu-backend-deploy" / "webui.py"


class QwenStartupRecoveryTests(unittest.TestCase):
    def test_qwen_retries_only_during_local_startup(self):
        text = WEBUI.read_text(encoding="utf-8")
        self.assertIn("QWEN_STARTUP_RETRY_SECONDS", text)
        self.assertIn("except requests.ConnectionError as exc", text)
        self.assertIn("Qwen 正在启动模型，自动重试中", text)
        self.assertIn("candidate.raise_for_status()", text)
        self.assertIn("time.sleep(3)", text)


if __name__ == "__main__":
    unittest.main()
