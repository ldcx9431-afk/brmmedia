#!/usr/bin/env python3
"""Static contract checks for the separate, stable LAN REST service.

The production dependency set lives in the Linux backend venv.  These checks
stay dependency-free so GitHub Actions can guard the public contract without
loading GPU/Gradio/FastAPI runtime modules.
"""

from __future__ import annotations

import ast
import importlib.util
import os
import sys
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LAN_API = ROOT / "ubuntu-backend-deploy" / "lan_api.py"
NGINX = ROOT / "ubuntu-backend-deploy" / "nginx-brmmedia.conf.example"
SERVICE = ROOT / "ubuntu-backend-deploy" / "brmmedia-lan-api.service.example"
H3_ACCEPTANCE = ROOT / "accept_minimax_h3_video.sh"


def _load_lan_api():
    """Load the normalizers without FastAPI/ComfyUI production dependencies."""
    requests = types.ModuleType("requests")

    class RequestException(Exception):
        pass

    def unavailable(*args, **kwargs):
        raise RequestException("network disabled in contract tests")

    requests.RequestException = RequestException
    requests.get = unavailable
    sys.modules["requests"] = requests

    fastapi = types.ModuleType("fastapi")

    class FastAPI:
        def __init__(self, *args, **kwargs):
            pass

        @staticmethod
        def _route(*args, **kwargs):
            return lambda function: function

        get = _route
        post = _route

    class HTTPException(Exception):
        def __init__(self, status_code, detail):
            self.status_code = status_code
            self.detail = detail
            super().__init__(detail)

    class UploadFile:
        pass

    fastapi.FastAPI = FastAPI
    fastapi.File = lambda *args, **kwargs: None
    fastapi.HTTPException = HTTPException
    fastapi.UploadFile = UploadFile
    sys.modules["fastapi"] = fastapi

    responses = types.ModuleType("fastapi.responses")

    class FileResponse:
        pass

    class JSONResponse:
        def __init__(self, *args, **kwargs):
            self.status_code = kwargs.get("status_code")
            self.content = kwargs.get("content")

    responses.FileResponse = FileResponse
    responses.JSONResponse = JSONResponse
    sys.modules["fastapi.responses"] = responses

    pydantic = types.ModuleType("pydantic")
    pydantic.BaseModel = object
    pydantic.Field = lambda default=None, **kwargs: default
    sys.modules["pydantic"] = pydantic

    comfy = types.ModuleType("comfyui_server")
    comfy.BASE = "http://127.0.0.1:8188"
    comfy.audio_duration = lambda *args, **kwargs: 0.0
    comfy.upload_image = lambda *args, **kwargs: "mock-input"
    sys.modules["comfyui_server"] = comfy

    spec = importlib.util.spec_from_file_location("brmmedia_lan_api_under_test", LAN_API)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class LanApiContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.api = _load_lan_api()

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
        self.assertIn("_available_music_models", text)
        self.assertIn('"MiniMax H3 Base"', text)
        self.assertIn('"preview"', text)
        self.assertIn('"quality"', text)
        self.assertNotIn("0.0.0.0", text)

    def test_proxy_and_service_are_loopback_only(self) -> None:
        nginx = NGINX.read_text(encoding="utf-8")
        service = SERVICE.read_text(encoding="utf-8")
        self.assertIn("location /api/", nginx)
        self.assertIn("proxy_pass http://127.0.0.1:9100", nginx)
        self.assertIn("--host 127.0.0.1 --port 9100", service)

    def test_h3_profile_defaults_and_gradio_argument_order(self) -> None:
        text_args = self.api._normal_text_to_video(
            {"prompt": "morning city", "profile": "quality"}, {}
        )
        self.assertEqual(text_args, ["morning city", "768 × 1024", 6, "quality"])

        asset = self.api.AssetRecord(
            asset_id="a" * 32, kind="image", filename="source.png",
            source_filename="source.png", size_bytes=1, sha256="b" * 64, created_at=0,
        )
        image_args = self.api._normal_image_to_video(
            {"prompt": "animate", "image_asset_id": asset.asset_id, "profile": "quality"},
            {asset.asset_id: asset},
        )
        self.assertEqual(image_args, ["animate", "source.png", "768 × 1024", 6, "quality"])

        explicit_preview = self.api._normal_text_to_video(
            {"prompt": "morning city", "profile": "preview", "seconds": 4}, {}
        )
        self.assertEqual(explicit_preview[-2:], [4, "preview"])

    def test_h3_capabilities_publish_structured_form_schema(self) -> None:
        previous = os.environ.get("BRMMEDIA_VIDEO_ENGINE")
        self.addCleanup(
            os.environ.__setitem__, "BRMMEDIA_VIDEO_ENGINE", previous
        ) if previous is not None else self.addCleanup(os.environ.pop, "BRMMEDIA_VIDEO_ENGINE", None)
        os.environ["BRMMEDIA_VIDEO_ENGINE"] = "h3"
        capabilities = self.api.capabilities()
        text_schema = capabilities["workflows"]["text-to-video"]["options"]["params"]
        image_schema = capabilities["workflows"]["image-to-video"]["options"]["params"]

        self.assertTrue(text_schema["prompt"]["required"])
        self.assertEqual(text_schema["profile"]["enum"], ["preview", "quality"])
        self.assertEqual(text_schema["seconds"]["default_by_profile"]["quality"], 6)
        self.assertEqual(text_schema["size"]["enum_source"], "size_values")
        self.assertEqual(image_schema["image_asset_id"]["asset_kind"], "image")
        self.assertEqual(capabilities["video_engine"], {"active": "h3", "h3_available": True})

    def test_ltx_capabilities_do_not_claim_h3_before_activation(self) -> None:
        previous = os.environ.get("BRMMEDIA_VIDEO_ENGINE")
        self.addCleanup(
            os.environ.__setitem__, "BRMMEDIA_VIDEO_ENGINE", previous
        ) if previous is not None else self.addCleanup(os.environ.pop, "BRMMEDIA_VIDEO_ENGINE", None)
        os.environ["BRMMEDIA_VIDEO_ENGINE"] = "ltx23"
        capabilities = self.api.capabilities()
        text_video = capabilities["workflows"]["text-to-video"]
        self.assertEqual(capabilities["video_engine"], {"active": "ltx23", "h3_available": False})
        self.assertEqual(text_video["options"]["engine"], "LTX2.3")
        self.assertNotIn("params", text_video["options"])

    def test_h3_acceptance_follows_task_artifact_contract(self) -> None:
        text = H3_ACCEPTANCE.read_text(encoding="utf-8")
        self.assertIn('payload.get("artifacts")', text)
        self.assertIn('first["download_url"]', text)
        self.assertIn('format=format_name', text)
        self.assertIn('did not return an MP4 filename', text)
        self.assertNotIn('json.load(sys.stdin)["output_files"][0]', text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
