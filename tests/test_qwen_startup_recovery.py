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

    def test_workspace_identifies_the_active_qwen38_dual_gpu_service(self):
        text = WEBUI.read_text(encoding="utf-8")
        self.assertIn("def qwen_runtime_label()", text)
        self.assertIn("Qwen3.8-27B · 双 A4000", text)
        self.assertIn("智能工具 / Qwen3.8 流式对话测试", text)
        self.assertIn("当前模型：`{QWEN_MODEL}`", text)
        self.assertIn("cancels=[qwen_submit_event]", text)

    def test_qwen_sse_is_explicitly_decoded_as_utf8(self):
        text = WEBUI.read_text(encoding="utf-8")
        self.assertIn('response.encoding = "utf-8"', text)
        self.assertIn("response.iter_lines(decode_unicode=True)", text)


if __name__ == "__main__":
    unittest.main()
