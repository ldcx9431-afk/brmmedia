#!/usr/bin/env python3
"""Comprehensive positive & negative unit tests for deep Python perf, /internal/v1,
SIGTERM shutdown, workflow slimming, NVENC fallback, and /api/v1/metrics.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import importlib.util
import io
import json
import os
import signal
import sqlite3
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[1]
DEPLOY_DIR = REPO_ROOT / "ubuntu-backend-deploy"
TESTS_DIR = REPO_ROOT / "tests"
for p in (str(DEPLOY_DIR), str(TESTS_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

import adaptive_concurrency as ac
import llm_runtime
from test_comfyui_task_lifecycle import _load_module as _load_comfyui_server
from test_task_queue import _load_webui

VALIDATOR_SPEC = importlib.util.spec_from_file_location(
    "validate_comfy_workflows", REPO_ROOT / "validate_comfy_workflows.py"
)
assert VALIDATOR_SPEC and VALIDATOR_SPEC.loader
validator = importlib.util.module_from_spec(VALIDATOR_SPEC)
VALIDATOR_SPEC.loader.exec_module(validator)


def _load_real_lan_api(module_name: str = "lan_api_deep_test"):
    prev_comfy = sys.modules.get("comfyui_server")
    comfy = types.ModuleType("comfyui_server")
    comfy.BASE = "http://127.0.0.1:8188"
    comfy.COMFY_ROOT = DEPLOY_DIR / "ComfyUI"
    comfy.audio_duration = lambda *a, **kw: 1.0
    comfy.upload_image = lambda *a, **kw: "mock-input.png"
    sys.modules["comfyui_server"] = comfy
    try:
        spec = importlib.util.spec_from_file_location(module_name, DEPLOY_DIR / "lan_api.py")
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = mod
        spec.loader.exec_module(mod)
        return mod
    finally:
        if prev_comfy is None:
            sys.modules.pop("comfyui_server", None)
        else:
            sys.modules["comfyui_server"] = prev_comfy


def _load_qwen_gateway(module_name: str = "qwen_gw_deep_test"):
    spec = importlib.util.spec_from_file_location(module_name, DEPLOY_DIR / "qwen_api_gateway.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    return mod


def _load_indextts25(module_name: str = "indextts25_deep_test"):
    spec = importlib.util.spec_from_file_location(module_name, DEPLOY_DIR / "indextts25_server.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    return mod


class LanApiAsyncAuthAndPersistenceTests(unittest.TestCase):
    def test_bearer_wal_ttl_cache_and_usage_buffer_and_thread_offload(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            db_path = tmp_path / "keys.sqlite3"
            tasks_path = tmp_path / "local-tasks.json"
            with patch.dict(
                os.environ,
                {
                    "BRM_QWEN_API_KEY_DB": str(db_path),
                    "BRM_QWEN_KEY_CACHE_TTL": "8.0",
                    "BRM_LAN_API_LOCAL_TASKS_PATH": str(tasks_path),
                },
            ):
                gw = _load_qwen_gateway("gw_for_lan_auth_test")
                rec, api_key = gw._create_key("lan-key", 10)
                api = _load_real_lan_api("lan_api_auth_test")
                api._KEY_CACHE.clear()
                api._USAGE_BUFFER.clear()

                # First validation populates cache, enables WAL, increments use_count=1
                api._validate_bearer_api_key(api_key)
                conn = sqlite3.connect(db_path)
                try:
                    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
                    self.assertEqual(str(mode).lower(), "wal")
                    count1 = conn.execute("SELECT use_count FROM api_keys WHERE id = ?", (rec["id"],)).fetchone()[0]
                    self.assertEqual(count1, 1)
                finally:
                    conn.close()

                # Second & third validations hit memory cache without opening SQLite
                with patch.object(api.sqlite3, "connect", side_effect=AssertionError("should hit TTL cache")):
                    api._validate_bearer_api_key(api_key)
                    api._validate_bearer_api_key(api_key)

                # Flushing buffered usage updates SQLite batch-wise
                api._flush_usage_buffer(db_path)
                conn = sqlite3.connect(db_path)
                try:
                    count2 = conn.execute("SELECT use_count FROM api_keys WHERE id = ?", (rec["id"],)).fetchone()[0]
                    self.assertEqual(count2, 3)
                finally:
                    conn.close()

                # Negative: invalid bearer token fails with 401
                with self.assertRaises(api.HTTPException) as err:
                    api._validate_bearer_api_key("brm_invalid_key")
                self.assertEqual(err.exception.status_code, 401)

                # Verify auth_middleware runs blocking auth on worker thread, not event loop thread
                seen_threads = []
                loop_threads = []
                orig_validate = api._validate_bearer_api_key

                def spy_validate(tok):
                    seen_threads.append(threading.get_ident())
                    return orig_validate(tok)

                async def check_thread():
                    loop_threads.append(threading.get_ident())
                    req = SimpleNamespace(
                        method="GET",
                        headers={"authorization": f"Bearer {api_key}"},
                        url=SimpleNamespace(path="/api/v1/capabilities"),
                    )

                    async def call_next(r):
                        return SimpleNamespace(status_code=200)

                    with patch.object(api, "_validate_bearer_api_key", side_effect=spy_validate):
                        resp = await api.auth_middleware(req, call_next)
                        self.assertEqual(resp.status_code, 200)

                asyncio.run(check_thread())
                self.assertEqual(len(seen_threads), 1)
                self.assertNotEqual(seen_threads[0], loop_threads[0])

    def test_htpasswd_mtime_ns_cache_and_invalidation(self):
        with tempfile.TemporaryDirectory() as tmp:
            ht = Path(tmp) / ".htpasswd"
            pw_hash = "{SHA}" + base64.b64encode(hashlib.sha1(b"pass1").digest()).decode("ascii")
            ht.write_text(f"alice:{pw_hash}\n", encoding="utf-8")
            api = _load_real_lan_api("lan_api_htpasswd_test")
            api._HTPASSWD_CACHE.clear()
            encoded1 = base64.b64encode(b"alice:pass1").decode("ascii")
            encoded2 = base64.b64encode(b"alice:pass2").decode("ascii")

            with patch.dict(os.environ, {"BRM_LAN_API_HTPASSWD": str(ht), "BRM_LAN_API_BASIC_AUTH": ""}):
                api._validate_basic_auth(encoded1)
                # Second call with unchanged mtime_ns/size must not re-read file
                with patch.object(Path, "read_text", side_effect=AssertionError("should use st_mtime_ns cache")):
                    api._validate_basic_auth(encoded1)

                # Modify htpasswd -> new content/size/mtime_ns invalidates cache
                pw_hash2 = "{SHA}" + base64.b64encode(hashlib.sha1(b"pass2").digest()).decode("ascii")
                time.sleep(0.02)
                ht.write_text(f"alice:{pw_hash2}\n", encoding="utf-8")
                with self.assertRaises(api.HTTPException):
                    api._validate_basic_auth(encoded1)
                api._validate_basic_auth(encoded2)

    def test_local_tasks_atomic_persistence_and_restart_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            tasks_file = Path(tmp) / "lan-api-local-tasks.json"
            with patch.dict(os.environ, {"BRM_LAN_API_LOCAL_TASKS_PATH": str(tasks_file)}):
                api = _load_real_lan_api("lan_api_persist_test")
                with api._LOCAL_TASKS_LOCK:
                    api._LOCAL_TASKS["1" * 32] = {
                        "task_id": "1" * 32,
                        "workflow": "zonos3-voice-clone",
                        "state": "completed",
                        "status": "已完成",
                        "submitted_at": 100.0,
                        "finished_at": 102.0,
                        "output_files": ["zonos3-ok.wav"],
                        "execution": {"stage": "completed", "progress": 1.0},
                    }
                    api._LOCAL_TASKS["2" * 32] = {
                        "task_id": "2" * 32,
                        "workflow": "zonos3-voice-clone",
                        "state": "running",
                        "status": "运行中",
                        "submitted_at": 103.0,
                        "output_files": [],
                        "execution": {"stage": "synthesizing_zonos3", "progress": 0.2},
                    }
                    api._save_local_tasks_locked()
                self.assertTrue(tasks_file.is_file())

                # Simulate process restart by resetting in-memory cache
                with api._LOCAL_TASKS_LOCK:
                    api._LOCAL_TASKS.clear()
                    api._LOCAL_TASKS_LOADED_PATH = None

                done_rec = api._local_task_status("1" * 32)
                self.assertIsNotNone(done_rec)
                self.assertEqual(done_rec["state"], "completed")
                self.assertEqual(done_rec["output_files"], ["zonos3-ok.wav"])

                interrupted = api._local_task_status("2" * 32)
                self.assertIsNotNone(interrupted)
                self.assertEqual(interrupted["state"], "failed")
                self.assertIn("重启", interrupted["error"])


class AsyncOffloadGatewayAndIndexTTSTests(unittest.IsolatedAsyncioTestCase):
    async def test_qwen_proxy_offloads_sync_work_and_cleans_closed_loops(self):
        gw = _load_qwen_gateway("gw_offload_test")
        loop_thread = threading.get_ident()
        call_threads: dict[str, int] = {}

        class SpyGuard:
            def __enter__(self):
                call_threads["guard_enter"] = threading.get_ident()
                return self

            def __exit__(self, *args):
                call_threads["guard_exit"] = threading.get_ident()

        def spy_validate(tok):
            call_threads["validate"] = threading.get_ident()

        def spy_prepare(body):
            call_threads["prepare"] = threading.get_ident()
            return body

        async def receive():
            return {"type": "http.request", "body": b'{"messages":[{"role":"user","content":"hi"}]}', "more_body": False}

        from starlette.requests import Request

        scope = {
            "type": "http",
            "method": "POST",
            "path": "/v1/chat/completions",
            "query_string": b"",
            "headers": [(b"authorization", b"Bearer brm_test")],
            "scheme": "http",
            "server": ("127.0.0.1", 9300),
        }
        with patch.object(gw, "_validate_and_record_usage", side_effect=spy_validate), \
             patch.object(gw.llm_runtime, "inference_guard", return_value=SpyGuard()), \
             patch.object(gw.llm_runtime, "upstream_headers", return_value={}), \
             patch.object(gw, "_prepare_chat_payload", side_effect=spy_prepare):
            resp = await gw.qwen_proxy("chat/completions", Request(scope, receive))
            self.assertIsInstance(resp, gw._UpstreamResponse)

        for label in ("validate", "guard_enter", "prepare"):
            self.assertIn(label, call_threads)
            self.assertNotEqual(call_threads[label], loop_thread, label)

        # Verify closed event loop entries are pruned from _ASYNC_CLIENT_MAP
        closed_loop = asyncio.new_event_loop()
        closed_id = id(closed_loop)
        closed_loop.close()
        with gw._ASYNC_CLIENT_LOCK:
            import weakref
            gw._ASYNC_CLIENT_MAP[closed_id] = Mock()
            gw._ASYNC_CLIENT_LOOPS[closed_id] = weakref.ref(closed_loop)
        client, owns = gw._acquire_async_client()
        self.assertFalse(owns)
        self.assertIsNotNone(client)
        self.assertNotIn(closed_id, gw._ASYNC_CLIENT_MAP)
        await gw._close_async_clients()

    async def test_indextts25_voice_clone_offloads_disk_infer_release_and_rmtree(self):
        srv = _load_indextts25("indextts25_offload_test")
        loop_thread = threading.get_ident()
        threads: dict[str, int] = {}

        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"

            def fake_synth(ref, text, lang, speed, out):
                threads["synth"] = threading.get_ident()
                self.assertTrue(ref.is_file())
                out.write_bytes(b"RIFFfakewav")
                return b"RIFFfakewav"

            def fake_release():
                threads["release"] = threading.get_ident()

            upload = SimpleNamespace(
                filename="ref.wav",
                read=lambda n: asyncio.sleep(0, result=b"RIFFref"),
            )
            orig_rmtree = srv.shutil.rmtree

            def spy_rmtree(path, **kw):
                threads["rmtree"] = threading.get_ident()
                return orig_rmtree(path, **kw)

            with patch.object(srv, "RUN_DIR", run_dir), \
                 patch.object(srv, "_synthesize", side_effect=fake_synth), \
                 patch.object(srv, "_release_engine", side_effect=fake_release), \
                 patch.object(srv.shutil, "rmtree", side_effect=spy_rmtree):
                resp = await srv.voice_clone(
                    text="你好", language="zh", speed=1.0, reference_audio=upload
                )
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.body, b"RIFFfakewav")
            for k in ("synth", "release", "rmtree"):
                self.assertIn(k, threads)
                self.assertNotEqual(threads[k], loop_thread, k)


class InternalV1AndMetricsAndShutdownTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.webui = _load_webui(Path(cls.temp_dir.name))
        cls.comfy = _load_comfyui_server()

    @classmethod
    def tearDownClass(cls):
        cls.temp_dir.cleanup()
        os.environ.pop("BRM_OUTPUT_DIR", None)

    def test_internal_v1_router_auth_and_direct_operations(self):
        out_dir = Path(self.temp_dir.name)
        artifact = out_dir / "internal-test.png"
        artifact.write_bytes(b"png-bytes")
        tid = "a" * 32
        task = self.webui.Task(
            id=tid, name="internal-test", workflow_name="image_z_image_turbo", args={}
        )
        task.status = self.webui.TaskStatus.DONE
        task.result = [str(artifact)]
        with self.webui.task_queue._lock:
            self.webui.task_queue._done.insert(0, task)

        pending_tid = "b" * 32
        pending = self.webui.Task(
            id=pending_tid, name="internal-pending", workflow_name="image_z_image_turbo", args={}
        )
        self.webui.task_queue.enqueue(pending)

        with patch.dict(os.environ, {"BRM_INTERNAL_API_TOKEN": "top-secret-token"}):
            router = self.webui.build_internal_api_router()
            app = FastAPI()
            app.include_router(router)
            client = TestClient(app)

            # Negative: unauthorized without token or wrong token
            self.assertEqual(client.get(f"/internal/v1/tasks/{tid}").status_code, 401)
            self.assertEqual(
                client.get(
                    f"/internal/v1/tasks/{tid}",
                    headers={"X-BRM-Internal-Token": "wrong"},
                ).status_code,
                401,
            )

            hdr = {"X-BRM-Internal-Token": "top-secret-token"}
            # Positive: direct task_status
            st = client.get(f"/internal/v1/tasks/{tid}", headers=hdr)
            self.assertEqual(st.status_code, 200)
            self.assertEqual(st.json()["state"], "completed")
            self.assertEqual(st.json()["output_files"], ["internal-test.png"])

            # Positive: direct cancel
            cn = client.post(f"/internal/v1/tasks/{pending_tid}/cancel", headers=hdr)
            self.assertEqual(cn.status_code, 200)
            self.assertEqual(cn.json()["state"], "cancelled")

            # Positive: direct artifact deletion
            dl = client.delete(
                f"/internal/v1/tasks/{tid}/artifacts/internal-test.png?confirmed=true",
                headers=hdr,
            )
            self.assertEqual(dl.status_code, 200)
            self.assertTrue(dl.json()["deleted"])
            self.assertFalse(artifact.exists())

    def test_lan_api_prefers_internal_v1_and_falls_back_on_404_or_unconfigured(self):
        api = _load_real_lan_api("lan_api_internal_test")
        tid = "c" * 32

        with patch.dict(os.environ, {"BRM_INTERNAL_API_TOKEN": "tok-123"}):
            session = Mock()
            ok_resp = Mock(status_code=200)
            ok_resp.json.return_value = {"task_id": tid, "state": "completed", "output_files": ["x.png"]}
            session.get.return_value = ok_resp
            with patch.object(api, "_http_session", return_value=session), \
                 patch.object(api, "_gradio_call", side_effect=AssertionError("should bypass SSE")):
                status = api._task_status(tid)
                self.assertEqual(status["state"], "completed")
                self.assertIn("X-BRM-Internal-Token", session.get.call_args.kwargs["headers"])

            # Negative 1: 404 from /internal/v1 falls back seamlessly to _gradio_call
            not_found_resp = Mock(status_code=404)
            session.get.return_value = not_found_resp
            with patch.object(api, "_http_session", return_value=session), \
                 patch.object(api, "_gradio_call", return_value={"task_id": tid, "state": "queued"}) as g_spy:
                status = api._task_status(tid)
                self.assertEqual(status["state"], "queued")
                g_spy.assert_called_once_with("task_status", [tid])

        # Negative 2: unconfigured/disabled internal API falls back to _gradio_call
        with patch.dict(os.environ, {"BRM_INTERNAL_API_DISABLED": "1"}):
            with patch.object(api, "_gradio_call", return_value={"task_id": tid, "state": "cancelled"}) as g_cancel:
                res = api.cancel_task(tid)
                self.assertEqual(res["state"], "cancelled")
                g_cancel.assert_called_once_with("cancel_task", [tid])

    def test_sigterm_handlers_flush_history_before_stop_comfyui(self):
        order = []
        with patch.object(self.webui.task_queue, "_save_history", side_effect=lambda **kw: order.append("save_history")), \
             patch.object(self.webui, "stop_comfyui", side_effect=lambda: order.append("stop_comfyui")):
            with self.assertRaises(SystemExit):
                self.webui._graceful_shutdown(signal.SIGTERM, None)
        self.assertEqual(order, ["save_history", "stop_comfyui"])

        with patch.object(self.webui.signal, "signal") as sig_spy:
            self.webui.install_shutdown_handlers()
            if hasattr(signal, "SIGTERM"):
                sig_spy.assert_called_once_with(signal.SIGTERM, self.webui._graceful_shutdown)

        with patch.object(self.comfy.signal, "signal") as c_sig_spy:
            self.comfy.install_signal_handlers()
            if hasattr(signal, "SIGTERM"):
                c_sig_spy.assert_called_once_with(signal.SIGTERM, self.comfy._handle_sigterm)

    def test_extract_result_cleans_up_comfy_output_copy_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            comfy_root = Path(tmp) / "comfy"
            out_src_dir = comfy_root / "output"
            in_src_dir = comfy_root / "input"
            out_src_dir.mkdir(parents=True)
            in_src_dir.mkdir(parents=True)
            comfy_out_file = out_src_dir / "gen.mp4"
            comfy_out_file.write_bytes(b"video-payload")
            comfy_in_file = in_src_dir / "ref.png"
            comfy_in_file.write_bytes(b"input-payload")
            dest_dir = Path(tmp) / "dest"

            task = self.webui.Task("ex-clean", "clean_test", "MiniMaxH3-文生视频", {})
            outputs = {
                "1": {"videos": [{"filename": "gen.mp4", "subfolder": "", "type": "output"}]},
                "2": {"images": [{"filename": "ref.png", "subfolder": "", "type": "input"}]},
            }
            with patch.object(self.webui, "COMFY_ROOT", comfy_root):
                saved = self.webui.extract_result(outputs, task, destination_dir=dest_dir)
            self.assertEqual(len(saved), 2)
            self.assertFalse(comfy_out_file.exists())
            self.assertTrue(comfy_in_file.exists())

    def test_metrics_tracker_and_lan_api_metrics_endpoint(self):
        tracker = ac.WorkflowMetricsTracker(window_size=50)
        for d in (1.0, 2.0, 3.0, 4.0, 10.0):
            tracker.record("image_z_image_turbo", success=True, duration_s=d, peak_vram_mib=7000 + int(d * 100))
        tracker.record("image_z_image_turbo", success=False, duration_s=1.5, error="CUDA out of memory", peak_vram_mib=24000)
        tracker.record("image_z_image_turbo", success=False, duration_s=0.5, cancelled=True)

        snap = tracker.snapshot()
        wf = snap["workflows"]["image_z_image_turbo"]
        self.assertEqual(wf["total"], 7)
        self.assertEqual(wf["success"], 5)
        self.assertEqual(wf["failed"], 1)
        self.assertEqual(wf["cancelled"], 1)
        self.assertEqual(wf["oom"], 1)
        self.assertEqual(wf["peak_vram_mib"], 24000)
        self.assertIsNotNone(wf["p50_duration_s"])
        self.assertGreaterEqual(wf["p95_duration_s"], wf["p50_duration_s"])

        api = _load_real_lan_api("lan_api_metrics_test")
        api._CONCURRENCY_CONTROLLER._metrics = ac.WorkflowMetricsTracker()
        api._CONCURRENCY_CONTROLLER.record_task_outcome("zonos3-voice-clone", success=True, duration_s=2.5)
        with patch.object(api, "_internal_api_call", return_value=snap):
            resp = api.get_metrics()
        self.assertEqual(resp["status"], "ok")
        self.assertTrue(resp["sources"]["webui"])
        self.assertIn("image_z_image_turbo", resp["workflows"])
        self.assertIn("zonos3-voice-clone", resp["workflows"])
        self.assertEqual(resp["summary"]["oom"], 1)

        # Negative: webui offline -> lan_api still returns local metrics cleanly
        with patch.object(api, "_internal_api_call", return_value=None):
            offline_resp = api.get_metrics()
        self.assertFalse(offline_resp["sources"]["webui"])
        self.assertIn("zonos3-voice-clone", offline_resp["workflows"])


class WorkflowSlimmingTemplateCacheAndNvencTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.webui = _load_webui(Path(cls.temp_dir.name))

    @classmethod
    def tearDownClass(cls):
        cls.temp_dir.cleanup()
        os.environ.pop("BRM_OUTPUT_DIR", None)

    def test_ltx23_and_minimax_h3_workflow_graphs(self):
        wf_dir = DEPLOY_DIR / "workflows"
        for name in (
            "LTX23-文生视频.json",
            "LTX23-图生视频.json",
            "LTX23-首尾帧视频.json",
            "LTX23-单图数字人-语音驱动.json",
        ):
            path = wf_dir / name
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertNotIn("VisualizeSigmasKJ", validator.required_nodes(path))
            self.assertEqual(data["26"]["class_type"], "BasicScheduler")
            self.assertEqual(data["29"]["class_type"], "SamplerCustom")
            self.assertEqual(data["29"]["inputs"]["sigmas"], ["26", 0])
            self.assertEqual(validator.enum_input_issues(path, {}), [])

        for name in ("MiniMaxH3-文生视频.json", "MiniMaxH3-图生视频.json"):
            path = wf_dir / name
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertIn("VAEDecodeTiled", validator.required_nodes(path))
            self.assertNotIn("VAEDecode", validator.required_nodes(path))
            node10 = data["10"]
            self.assertEqual(node10["class_type"], "VAEDecodeTiled")
            self.assertEqual(
                node10["inputs"],
                {
                    "samples": ["14", 0],
                    "vae": ["11", 0],
                    "tile_size": 512,
                    "overlap": 64,
                    "temporal_size": 64,
                    "temporal_overlap": 8,
                },
            )

    def test_workflow_template_lru_cache_and_deepcopy_isolation(self):
        self.webui._load_workflow_template_cached.cache_clear()
        args = {"prompt": "test prompt", "size": "1024 × 1024", "batch": 1}
        wf1 = self.webui.build_workflow_1("image_z_image_turbo", args)
        wf1["57:27"]["inputs"]["text"] = "MUTATED_BY_CALLER"
        with patch.object(Path, "read_text", side_effect=AssertionError("should hit @lru_cache")):
            wf2 = self.webui.build_workflow_1("image_z_image_turbo", args)
        self.assertEqual(wf2["57:27"]["inputs"]["text"], "test prompt")

    def test_seedvr2_segments_cfr_copy_nvenc_and_libx264_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            temp_dir = Path(tmp)
            source = temp_dir / "source.mp4"
            source.write_bytes(b"src")
            task = self.webui.Task("seg-1", "seg-1", "SeedVR2-图像/视频增强修复", {})
            info = {
                "duration": 120.0,
                "fps": 24.0,
                "nominal_fps": 24.0,
                "fps_text": "24",
                "codec_name": "h264",
            }
            seen_cmds = []

            def fake_run_copy(cmd, t, deadline):
                seen_cmds.append(cmd)
                for idx in range(2):
                    (temp_dir / f"input_{idx:04d}.mp4").write_bytes(b"chunk")

            with patch.object(self.webui.shutil, "which", return_value="/usr/bin/ffmpeg"), \
                 patch.object(self.webui, "_run_seedvr2_command", side_effect=fake_run_copy):
                segs = self.webui._seedvr2_video_segments(source, info, temp_dir, task, time.monotonic() + 60)
            self.assertEqual(len(segs), 2)
            self.assertEqual(seen_cmds[0][seen_cmds[0].index("-c:v") + 1], "copy")

            # Case 2: copy fails, GPU0 is idle -> tries h264_nvenc; NVENC fails -> falls back to libx264
            seen_cmds.clear()
            for f in temp_dir.glob("input_*.mp4"):
                f.unlink()

            def fake_run_fallback(cmd, t, deadline):
                seen_cmds.append(cmd)
                codec = cmd[cmd.index("-c:v") + 1]
                if codec in {"copy", "h264_nvenc"}:
                    raise RuntimeError(f"{codec} failed")
                (temp_dir / "input_0000.mp4").write_bytes(b"x264-chunk")

            with patch.object(self.webui.shutil, "which", return_value="/usr/bin/ffmpeg"), \
                 patch.object(self.webui, "_is_gpu0_idle_for_nvenc", return_value=True), \
                 patch.object(self.webui, "_run_seedvr2_command", side_effect=fake_run_fallback):
                segs = self.webui._seedvr2_video_segments(source, info, temp_dir, task, time.monotonic() + 60)
            codecs = [cmd[cmd.index("-c:v") + 1] for cmd in seen_cmds]
            self.assertEqual(codecs, ["copy", "h264_nvenc", "libx264"])
            nvenc_cmd = seen_cmds[1]
            self.assertEqual(nvenc_cmd[nvenc_cmd.index("-preset") + 1], "p4")
            self.assertEqual(nvenc_cmd[nvenc_cmd.index("-cq") + 1], "18")
            self.assertEqual(len(segs), 1)

    def test_llm_runtime_permission_error_graceful_degrade(self):
        fake_fcntl = types.SimpleNamespace(LOCK_SH=1, LOCK_NB=2, LOCK_UN=8, flock=lambda *a: None)
        with tempfile.TemporaryDirectory() as tmp:
            lock_file = Path(tmp) / "llm-traffic.lock"
            lock_file.write_text("", encoding="utf-8")
            with patch.dict(sys.modules, {"fcntl": fake_fcntl}), \
                 patch.object(llm_runtime.os, "name", "posix"), \
                 patch.dict(os.environ, {"BRM_LLM_TRAFFIC_LOCK": str(lock_file)}), \
                 patch.object(Path, "open", side_effect=PermissionError("denied")):
                with llm_runtime.inference_guard():
                    entered = True
                self.assertTrue(entered)


if __name__ == "__main__":
    unittest.main(verbosity=2)
