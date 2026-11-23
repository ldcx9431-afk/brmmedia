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
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
LAN_API = ROOT / "ubuntu-backend-deploy" / "lan_api.py"
NGINX = ROOT / "ubuntu-backend-deploy" / "nginx-brmmedia.conf.example"
SERVICE = ROOT / "ubuntu-backend-deploy" / "brmmedia-lan-api.service.example"
H3_ACCEPTANCE = ROOT / "accept_minimax_h3_video.sh"


def _load_lan_api():
    """Load the normalizers without FastAPI/ComfyUI production dependencies."""
    module_names = ("requests", "fastapi", "fastapi.responses", "pydantic", "comfyui_server")
    previous_modules = {name: sys.modules.get(name) for name in module_names}
    if str(LAN_API.parent) not in sys.path:
        sys.path.insert(0, str(LAN_API.parent))
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
        delete = _route
        middleware = _route

    class HTTPException(Exception):
        def __init__(self, status_code, detail):
            self.status_code = status_code
            self.detail = detail
            super().__init__(detail)

    class UploadFile:
        pass

    class Request:
        pass

    fastapi.FastAPI = FastAPI
    fastapi.File = lambda *args, **kwargs: None
    fastapi.HTTPException = HTTPException
    fastapi.Request = Request
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
    comfy.COMFY_ROOT = ROOT / "ubuntu-backend-deploy" / "ComfyUI"
    comfy.audio_duration = lambda *args, **kwargs: 0.0
    comfy.upload_image = lambda *args, **kwargs: "mock-input"
    sys.modules["comfyui_server"] = comfy

    spec = importlib.util.spec_from_file_location("brmmedia_lan_api_under_test", LAN_API)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        for name, previous in previous_modules.items():
            if previous is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous
    return module


class LanApiContractTests(unittest.TestCase):
    def test_single_artifact_delete_requires_confirmation_and_maps_failures(self):
        tid='a'*32
        with patch.object(self.api,'_gradio_call',return_value={'deleted':True}) as bridge:
            with self.assertRaises(self.api.HTTPException) as error:
                self.api.delete_artifact(tid,'one.png',{'confirmed':False})
            self.assertEqual(error.exception.status_code,400)
            bridge.assert_not_called()
            self.assertTrue(self.api.delete_artifact(tid,'one.png',{'confirmed':True})['deleted'])
            bridge.assert_called_once_with('delete_asset',[tid,'one.png',True])
        for code,status in [('not_found',404),('asset_busy',409),('delete_failed',500)]:
            with patch.object(self.api,'_gradio_call',return_value={'error':code}), self.assertRaises(self.api.HTTPException) as error:
                self.api.delete_artifact(tid,'one.png',{'confirmed':True})
            self.assertEqual(error.exception.status_code,status)

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

    def test_iteration_workflows_are_gated_and_validate_asset_kind(self):
        with patch.dict(os.environ, {"BRM_FLUX2_FAST_ENABLED":"0", "BRM_QWEN3_TTS_ENABLED":"0"}):
            self.assertNotIn("flux2-fast-edit", self.api._active_workflows())
            self.assertNotIn("qwen3-voice-clone", self.api._active_workflows())
        with patch.dict(os.environ, {"BRM_FLUX2_FAST_ENABLED":"1", "BRM_QWEN3_TTS_ENABLED":"1"}):
            self.assertIn("flux2-fast-edit", self.api._active_workflows())
            self.assertIn("qwen3-voice-clone", self.api._active_workflows())
        image = self.api.AssetRecord(asset_id="a"*32,kind="image",filename="source.png",source_filename="source.png",size_bytes=1,sha256="b"*64,created_at=0)
        audio = self.api.AssetRecord(asset_id="c"*32,kind="audio",filename="ref.wav",source_filename="ref.wav",size_bytes=1,sha256="d"*64,created_at=0)
        assets={image.asset_id:image,audio.asset_id:audio}
        self.assertEqual(self.api._normal_flux2_fast({"prompt":"蓝色茶壶","image_asset_id":image.asset_id,"width":1920,"height":1080},assets),["蓝色茶壶","source.png",1920,1080,42])
        with patch('qwen3_tts_support.check_reference'):
            self.assertEqual(self.api._normal_qwen3_tts({"text":"你好","ref_audio_asset_id":audio.asset_id},assets),["你好","ref.wav","zh",""])
        for params, normalizer in [({"prompt":"蓝色","image_asset_id":audio.asset_id},self.api._normal_flux2_fast),({"text":"你好","ref_audio_asset_id":image.asset_id},self.api._normal_qwen3_tts),({"prompt":"蓝色","image_asset_id":image.asset_id,"width":4096},self.api._normal_flux2_fast),({"text":"长"*601,"ref_audio_asset_id":audio.asset_id},self.api._normal_qwen3_tts)]:
            with self.assertRaises(self.api.HTTPException):normalizer(params,assets)

    def test_flux3_flag_modes_and_reference_contract(self):
        with patch.dict(os.environ,{"BRM_FLUX3_IMAGE_ENABLED":"0"}):
            self.assertNotIn("flux3-text-to-image",self.api._active_workflows())
            self.assertNotIn("flux3-image-edit",self.api._active_workflows())
        with patch.dict(os.environ,{"BRM_FLUX3_IMAGE_ENABLED":"1"}):
            active=self.api._active_workflows()
            self.assertIn("flux3-text-to-image",active)
            self.assertEqual(active["flux3-image-edit"].options["prompt_enhancement"],"official")
        image=self.api.AssetRecord(asset_id="a"*32,kind="image",filename="source.png",source_filename="source.png",size_bytes=1,sha256="b"*64,created_at=0)
        audio=self.api.AssetRecord(asset_id="c"*32,kind="audio",filename="ref.wav",source_filename="ref.wav",size_bytes=1,sha256="d"*64,created_at=0)
        assets={image.asset_id:image,audio.asset_id:audio}
        self.assertEqual(self.api._normal_flux3({"prompt":"中文"},assets),["中文",1024,1024,42])
        self.assertEqual(self.api._normal_flux3({"prompt":"蓝色","width":1152,"height":864,"seed":7,"image_asset_id":image.asset_id},assets,True),["蓝色",1152,864,7,"source.png"])
        for params in ({"prompt":"x"},{"prompt":"x","image_asset_id":audio.asset_id}):
            with self.assertRaises(self.api.HTTPException):self.api._normal_flux3(params,assets,True)

    def test_api_keeps_paths_loopback_only_and_uses_opaque_assets(self) -> None:
        text = LAN_API.read_text(encoding="utf-8")
        self.assertIn('"http://127.0.0.1:9000"', text)
        self.assertIn('"asset_id"', text)
        self.assertIn('"download_url"', text)
        self.assertIn("_available_music_models", text)
        self.assertIn('"MiniMax H3 Base"', text)
        self.assertIn('"preview"', text)
        self.assertIn('"quality"', text)
        self.assertIn('"draft"', text)
        self.assertNotIn("0.0.0.0", text)

    def test_proxy_and_service_are_loopback_only(self) -> None:
        nginx = NGINX.read_text(encoding="utf-8")
        service = SERVICE.read_text(encoding="utf-8")
        self.assertIn("location /api/", nginx)
        self.assertTrue(
            "proxy_pass http://127.0.0.1:9100" in nginx
            or ("server 127.0.0.1:9100;" in nginx and "proxy_pass http://brmmedia_lan_api" in nginx)
        )
        self.assertIn("client_max_body_size 2100M", nginx)
        self.assertIn("location /comfyui/", nginx)
        self.assertTrue(
            "proxy_pass http://127.0.0.1:8188/" in nginx
            or ("server 127.0.0.1:8188;" in nginx and "proxy_pass http://brmmedia_comfyui/" in nginx)
        )
        self.assertIn('proxy_set_header Upgrade $http_upgrade', nginx)
        self.assertIn("--host 127.0.0.1 --port 9100", service)

    def test_h3_profile_defaults_and_gradio_argument_order(self) -> None:
        draft_args = self.api._normal_text_to_video(
            {"prompt": "fast composition", "profile": "draft"}, {}
        )
        self.assertEqual(draft_args, ["fast composition", "768 × 1024", 3, "draft", "standard"])

    def test_qwen_image21_research_gate_and_arguments(self) -> None:
        with patch.dict(os.environ, {"BRM_QWEN_IMAGE21_ENABLED": "0"}):
            self.assertNotIn("qwen-image21", self.api._active_workflows())
        with patch.dict(os.environ, {"BRM_QWEN_IMAGE21_ENABLED": "1"}):
            self.assertIn("qwen-image21", self.api._active_workflows())
        self.assertEqual(self.api._normal_qwen_image21({"prompt": "research"}, {}), ["research", 1024, 1024, 8, 0, True])
        self.assertEqual(self.api._normal_qwen_image21({"prompt": "research", "steps": 40}, {}), ["research", 1024, 1024, 40, 0, False])
        self.assertEqual(self.api._normal_qwen_image21({"prompt": "research", "turbo": False}, {}), ["research", 1024, 1024, 40, 0, False])
        asset = self.api.AssetRecord(
            asset_id="a" * 32, kind="image", filename="source.png",
            source_filename="source.png", size_bytes=1, sha256="b" * 64, created_at=0,
        )
        self.assertEqual(
            self.api._normal_qwen_image21_edit({"prompt": "edit", "image_asset_id": asset.asset_id}, {asset.asset_id: asset}),
            ["edit", 1024, 1024, 8, 0, "source.png", True],
        )
        self.assertEqual(
            self.api._normal_qwen_image21_edit({"prompt": "edit", "image_asset_id": asset.asset_id, "turbo": False}, {asset.asset_id: asset}),
            ["edit", 1024, 1024, 40, 0, "source.png", False],
        )
        for bad in (
            {"prompt": "research", "width": 2048, "height": 2048},
            {"prompt": "research", "turbo": "yes"},
        ):
            with self.assertRaises(self.api.HTTPException) as caught:
                self.api._normal_qwen_image21(bad, {})
            self.assertEqual(caught.exception.status_code, 422)

    def test_h3_explicit_profiles_and_assets(self) -> None:

        text_args = self.api._normal_text_to_video(
            {"prompt": "morning city", "profile": "quality"}, {}
        )
        self.assertEqual(text_args, ["morning city", "768 × 1024", 6, "quality", "standard"])

        asset = self.api.AssetRecord(
            asset_id="a" * 32, kind="image", filename="source.png",
            source_filename="source.png", size_bytes=1, sha256="b" * 64, created_at=0,
        )
        image_args = self.api._normal_image_to_video(
            {"prompt": "animate", "image_asset_id": asset.asset_id, "profile": "quality"},
            {asset.asset_id: asset},
        )
        self.assertEqual(image_args, ["animate", "source.png", "768 × 1024", 6, "quality", "standard"])

        explicit_preview = self.api._normal_text_to_video(
            {"prompt": "morning city", "profile": "preview", "seconds": 4}, {}
        )
        self.assertEqual(explicit_preview[-3:], [4, "preview", "standard"])

        fast_args = self.api._normal_text_to_video({
            "prompt": "fast", "profile": "quality", "size": "1920 × 1080",
            "acceleration": "turbo_fast",
        }, {})
        self.assertEqual(fast_args[-1], "turbo_fast")
        trained_cell_args = self.api._normal_text_to_video({
            "prompt": "trained cell", "profile": "quality", "size": "1344 × 768",
            "acceleration": "turbo_fast",
        }, {})
        self.assertEqual(trained_cell_args[-1], "turbo_fast")
        long_balanced_args = self.api._normal_text_to_video({
            "prompt": "long balanced", "profile": "quality", "size": "1920 × 1080",
            "seconds": 15, "acceleration": "turbo_balanced",
        }, {})
        self.assertEqual(long_balanced_args[-3:], [15, "quality", "turbo_balanced"])
        long_fast_args = self.api._normal_text_to_video({
            "prompt": "long fast", "profile": "quality", "size": "1920 × 1080",
            "seconds": 15, "acceleration": "turbo_fast",
        }, {})
        self.assertEqual(long_fast_args[-3:], [15, "quality", "turbo_fast"])
        with self.assertRaises(self.api.HTTPException):
            self.api._normal_text_to_video({
                "prompt": "bad fast", "profile": "quality", "size": "768 × 1024",
                "acceleration": "turbo_fast",
            }, {})

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
        self.assertEqual(text_schema["profile"]["enum"], ["draft", "preview", "quality"])
        self.assertEqual(
            text_schema["acceleration"]["enum"],
            ["standard", "turbo_balanced", "turbo_fast"],
        )
        self.assertEqual(text_schema["acceleration"]["default"], "standard")
        self.assertEqual(text_schema["seconds"]["default_by_profile"]["quality"], 6)
        self.assertEqual(
            capabilities["workflows"]["text-to-video"]["options"]["accelerations"]
            ["turbo_balanced"]["constraints"]["maximum_seconds"],
            15,
        )
        self.assertEqual(text_schema["size"]["enum_source"], "size_values")
        self.assertEqual(image_schema["image_asset_id"]["asset_kind"], "image")
        self.assertEqual(
            capabilities["workflows"]["ltx-text-to-video"]["options"]["engine"],
            "LTX2.3",
        )
        self.assertEqual(
            capabilities["workflows"]["ltx-image-to-video"]["options"]["params"]
            ["image_asset_id"]["asset_kind"],
            "image",
        )
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
        self.assertIn("params", text_video["options"])
        self.assertIn("ltx-text-to-video", capabilities["workflows"])
        self.assertIn("ltx-image-to-video", capabilities["workflows"])

    def test_seedvr2_capabilities_publish_modes_upload_limit_and_safe_defaults(self) -> None:
        capabilities = self.api.capabilities()
        workflow = capabilities["workflows"]["seedvr2-enhance"]
        options = workflow["options"]
        params = options["params"]
        self.assertEqual(options["engine"], "SeedVR2 INT8")
        self.assertEqual(options["default_model_variant"], "7b")
        self.assertEqual(options["models"]["7b"]["filename"], "seedvr2_7b_int8_convrot.safetensors")
        self.assertEqual(options["models"]["3b"]["filename"], "seedvr2_3b_int8_convrot.safetensors")
        self.assertEqual(options["models"]["3b"]["role"], "快速模式")
        self.assertEqual(params["mode"]["enum"], ["image", "video"])
        self.assertEqual(params["model_variant"]["default"], "7b")
        self.assertEqual(params["model_variant"]["enum"], ["7b", "3b"])
        self.assertEqual(params["scale"]["enum"], [1, 2])
        self.assertEqual(params["resolution_preset"]["enum"], ["native", "720p", "1080p"])
        self.assertEqual(params["resolution_preset"]["planned"], ["4k"])
        self.assertEqual(params["strength"]["denoise"], {
            "light": 0.6, "standard": 0.8, "strong": 1.0,
        })
        self.assertEqual(capabilities["max_upload_bytes_by_kind"]["video"], 2 * 1024**3)
        self.assertEqual(options["max_task_seconds"], 12 * 60 * 60)

    def test_seedvr2_rejects_missing_or_wrong_kind_asset(self) -> None:
        with self.assertRaises(self.api.HTTPException) as missing:
            self.api._normal_seedvr2_enhance({"mode": "image"}, {})
        self.assertEqual(missing.exception.status_code, 422)

        audio = self.api.AssetRecord(
            asset_id="a" * 32, kind="audio", filename="source.wav",
            source_filename="source.wav", size_bytes=1, sha256="b" * 64, created_at=0,
        )
        with self.assertRaises(self.api.HTTPException) as mismatch:
            self.api._normal_seedvr2_enhance(
                {"mode": "video", "video_asset_id": audio.asset_id}, {audio.asset_id: audio},
            )
        self.assertEqual(mismatch.exception.status_code, 422)

        with self.assertRaises(self.api.HTTPException) as unsupported_model:
            self.api._normal_seedvr2_enhance({"model_variant": "1b"}, {})
        self.assertEqual(unsupported_model.exception.status_code, 422)
        self.assertIn("model_variant", unsupported_model.exception.detail)

        with self.assertRaises(self.api.HTTPException) as four_k:
            self.api._normal_seedvr2_enhance({"mode": "video", "resolution_preset": "4k"}, {})
        self.assertEqual(four_k.exception.status_code, 422)
        self.assertIn("暂未开放", four_k.exception.detail)

    def test_seedvr2_video_preset_reaches_workbench_arguments(self) -> None:
        video = self.api.AssetRecord(
            asset_id="c" * 32, kind="video", filename="sample.MP4",
            source_filename="sample.MP4", size_bytes=1024, sha256="d" * 64, created_at=0,
        )
        fake_path = Path("/tmp/sample.MP4")
        info = {
            "width": 1280, "height": 720, "duration": 10,
            "fps": 24, "frame_count": 240,
        }
        with patch.object(self.api, "resolve_comfy_input", return_value=fake_path), patch.object(
            self.api, "probe_video", return_value=info,
        ), patch.object(self.api, "validate_video_info"), patch.object(
            self.api, "video_output_dimensions", return_value=(1920, 1080, 1.5),
        ), patch.object(self.api, "estimated_video_workspace_bytes", return_value=1), patch.object(
            self.api, "available_disk_bytes", return_value=1024**4,
        ):
            arguments = self.api._normal_seedvr2_enhance(
                {
                    "mode": "video", "video_asset_id": video.asset_id,
                    "resolution_preset": "1080p",
                    "model_variant": "3b",
                },
                {video.asset_id: video},
            )
        self.assertEqual(arguments, ["video", "", "sample.MP4", 2, "standard", "1080p", "3b"])

    def test_h3_acceptance_follows_task_artifact_contract(self) -> None:
        text = H3_ACCEPTANCE.read_text(encoding="utf-8")
        self.assertIn('payload.get("artifacts")', text)
        self.assertIn('first["download_url"]', text)
        self.assertIn('format=format_name', text)
        self.assertIn('did not return an MP4 filename', text)
        self.assertNotIn('json.load(sys.stdin)["output_files"][0]', text)

    def test_artifact_download_allows_safe_h3_filename_spaces(self) -> None:
        """H3's user-visible output names contain ordinary spaces."""
        filename = "任务_MiniMax H3 文生视频_demo.mp4"
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir).resolve()
            artifact = output_dir / filename
            artifact.write_bytes(b"fixture")
            previous_output = self.api.OUTPUT_DIR
            previous_status = self.api._task_status
            self.addCleanup(setattr, self.api, "OUTPUT_DIR", previous_output)
            self.addCleanup(setattr, self.api, "_task_status", previous_status)
            self.api.OUTPUT_DIR = output_dir
            self.api._task_status = lambda _task_id: {
                "state": "completed", "output_files": [filename]
            }
            self.assertEqual(self.api._safe_artifact("a" * 32, filename), artifact)

    def test_experimental_speech_artifact_allows_chinese_parentheses(self):
        filename="任务_Qwen3-TTS 语音克隆（实验）_demo.wav"
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();artifact=root/filename;artifact.write_bytes(b"fixture")
            with patch.object(self.api,"OUTPUT_DIR",root),patch.object(self.api,"_task_status",return_value={"state":"completed","output_files":[filename]}):
                self.assertEqual(self.api._safe_artifact("a"*32,filename),artifact)
                for bad in ("../"+filename,"sub/"+filename,"sub\\"+filename,"not-owned.wav"):
                    with self.assertRaises(self.api.HTTPException):self.api._safe_artifact("a"*32,bad)


if __name__ == "__main__":
    unittest.main(verbosity=2)
