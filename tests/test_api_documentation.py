#!/usr/bin/env python3
"""Keep the checked-in LAN API contract documentation free of retired routes."""

from __future__ import annotations

import unittest
from pathlib import Path


DOC_PATH = Path(__file__).resolve().parents[1] / "API接口文档.md"


class ApiDocumentationTests(unittest.TestCase):
    def test_documents_current_gateway_and_task_contract(self) -> None:
        text = DOC_PATH.read_text(encoding="utf-8")

        self.assertIn("http://<Windows-LAN-IP>/", text)
        self.assertIn("Nginx HTTP Basic Auth", text)
        self.assertIn("/task_status", text)
        self.assertIn("/submit_workflow_8", text)
        self.assertIn("call_gradio_api.py", text)

    def test_does_not_reintroduce_retired_direct_lan_routes(self) -> None:
        text = DOC_PATH.read_text(encoding="utf-8")

        self.assertNotIn("192.168.1.118", text)
        self.assertNotIn("192.168.1.106", text)
        self.assertNotIn("/render_queue", text)
        self.assertNotIn("/clear_pending", text)

    def test_legacy_deployment_documents_are_explicitly_labeled(self) -> None:
        root = DOC_PATH.parent
        expected_labels = {
            "对接经验.md": "历史参考",
            "TTS工作流集成方案.md": "历史选型与 Windows 集成记录",
            "ubuntu-backend-deploy/README_UBUNTU_DEPLOY.md": "不可直接用于当前服务器",
        }
        for relative_path, marker in expected_labels.items():
            with self.subTest(path=relative_path):
                self.assertIn(marker, (root / relative_path).read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
