#!/usr/bin/env python3
"""Static contract checks for the separate, stable LAN REST service.

The production dependency set lives in the Linux backend venv.  These checks
stay dependency-free so GitHub Actions can guard the public contract without
loading GPU/Gradio/FastAPI runtime modules.
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LAN_API = ROOT / "ubuntu-backend-deploy" / "lan_api.py"
NGINX = ROOT / "ubuntu-backend-deploy" / "nginx-brmmedia.conf.example"
SERVICE = ROOT / "ubuntu-backend-deploy" / "brmmedia-lan-api.service.example"


class LanApiContractTests(unittest.TestCase):
    def test_api_has_stable_rest_routes(self) -> None:
        tree = ast.parse(LAN_API.read_text(encoding="utf-8"))
        decorators = [
            decorator
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            for decorator in node.decorator_list
        ]
        routes = [
            decorator.args[0]
            for decorator in decorators
            if isinstance(decorator, ast.Call)
            and isinstance(decorator.func, ast.Attribute)
            and decorator.func.attr in {"get", "post"}
            and decorator.args
            and isinstance(decorator.args[0], ast.JoinedStr)
        ]
        expected_suffixes = {
            "/health", "/capabilities", "/files", "/tasks",
            "/tasks/{task_id}", "/tasks/{task_id}/artifacts/{filename}",
        }
        rendered = {"".join(value.value for value in route.values if isinstance(value, ast.Constant)) for route in routes}
        self.assertTrue(expected_suffixes.issubset(rendered))

    def test_api_keeps_paths_loopback_only_and_uses_opaque_assets(self) -> None:
        text = LAN_API.read_text(encoding="utf-8")
        self.assertIn('"http://127.0.0.1:9000"', text)
        self.assertIn('"asset_id"', text)
        self.assertIn('"download_url"', text)
        self.assertNotIn("0.0.0.0", text)

    def test_proxy_and_service_are_loopback_only(self) -> None:
        nginx = NGINX.read_text(encoding="utf-8")
        service = SERVICE.read_text(encoding="utf-8")
        self.assertIn("location /api/", nginx)
        self.assertIn("proxy_pass http://127.0.0.1:9100", nginx)
        self.assertIn("--host 127.0.0.1 --port 9100", service)


if __name__ == "__main__":
    unittest.main(verbosity=2)
