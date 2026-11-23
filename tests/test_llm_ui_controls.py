"""Test production UI callbacks without loading Gradio or GPU libraries."""
import ast
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

SOURCE = Path(__file__).resolve().parents[1] / "ubuntu-backend-deploy" / "webui.py"


class LlmUiControlsTests(unittest.TestCase):
    def setUp(self):
        self.get = Mock()
        self.runtime = SimpleNamespace(
            status=lambda: {"state": "ready"},
            selection=lambda *a: ("loopback", "fallback"),
        )
        self.env = {
            "requests": SimpleNamespace(get=self.get),
            "llm_runtime": self.runtime,
            "gr": SimpleNamespace(update=lambda **kw: kw),
            "QWEN_API_BASE": "loopback",
            "QWEN_MODEL": "fallback",
        }
        names = {"_llm_ui_state", "llm_status_text", "llm_control_updates"}
        tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
        funcs = ast.Module(
            body=[x for x in tree.body if isinstance(x, ast.FunctionDef) and x.name in names],
            type_ignores=[],
        )
        exec(compile(funcs, str(SOURCE), "exec"), self.env)

    def state(self, **kw):
        data = {"state": "ready", "target": "strata", "model": "strata-qwen38", "strata_installed": True, **kw}
        self.get.return_value = SimpleNamespace(ok=True, json=lambda: data)
        return self.env["llm_control_updates"]()

    def test_status_reflects_active_strata_model_and_custom_message(self):
        label = self.state()
        self.assertIn("strata-qwen38", label)
        self.assertIn("Strata", label)
        custom = self.state(target="other", model="custom-model", message="custom ready")
        self.assertIn("custom-model", custom)
        self.assertIn("custom ready", custom)

    def test_controller_unavailable_surfaces_clear_status_message(self):
        self.get.side_effect = OSError("offline")
        label = self.env["llm_control_updates"]()
        self.assertIn("暂不可用", label)
        self.assertIn("fallback", label)

    def test_toolbar_above_chat_without_dead_switch_buttons_or_polling_timer(self):
        source = SOURCE.read_text(encoding="utf-8")
        self.assertLess(
            source.index('with gr.Column(elem_id="llm-switch-toolbar")'),
            source.index('with gr.Row(elem_id="qwen-workspace")'),
        )
        self.assertNotIn('elem_id="llm-switch-qwen"', source)
        self.assertNotIn('elem_id="llm-switch-strata"', source)
        self.assertNotIn("runtime_timer", source)
        self.assertIn('elem_id="llm-switch-refresh"', source)
        for line in source.splitlines():
            if "refresh_runtime.click(" in line:
                self.assertIn("queue=False", line)
                self.assertIn('api_visibility="private"', line)


if __name__ == "__main__":
    unittest.main()
