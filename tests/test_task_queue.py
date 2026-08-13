#!/usr/bin/env python3
"""Hermetic behavioral checks for the workspace task queue.

The production UI imports Gradio and ComfyUI bridge modules.  This test stubs
only their import-time surface and exercises the real Task/TaskQueue classes
against a temporary output directory, so it remains runnable in GitHub Actions
without GPU, models, or the production Python environment.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
WEBUI_PATH = REPO_ROOT / "ubuntu-backend-deploy" / "webui.py"


def _install_import_stubs() -> None:
    requests = types.ModuleType("requests")
    requests.RequestException = Exception
    sys.modules["requests"] = requests

    gradio = types.ModuleType("gradio")

    class Error(Exception):
        pass

    class SelectData:
        pass

    gradio.Error = Error
    gradio.SelectData = SelectData
    gradio.Info = lambda *args, **kwargs: None
    gradio.set_static_paths = lambda **kwargs: None
    sys.modules["gradio"] = gradio

    comfy = types.ModuleType("comfyui_server")
    comfy.start_comfyui = lambda *args, **kwargs: None
    comfy.stop_comfyui = lambda *args, **kwargs: None
    comfy.is_alive = lambda: True
    comfy.run_workflow = lambda *args, **kwargs: {}
    comfy.get_view_file = lambda *args, **kwargs: b""
    comfy.interrupt = lambda: (True, "mock interrupt")
    comfy.TASK_TIMEOUT = 3600
    comfy.H3_TASK_TIMEOUT = 14400
    comfy.BASE = "http://127.0.0.1:8188"
    comfy.WORKFLOW_DIR = REPO_ROOT / "ubuntu-backend-deploy" / "workflows"
    comfy.upload_image = lambda *args, **kwargs: "mock-input"
    comfy.audio_duration = lambda *args, **kwargs: 0.0
    sys.modules["comfyui_server"] = comfy


def _load_webui(output_dir: Path):
    _install_import_stubs()
    os.environ["BRM_OUTPUT_DIR"] = str(output_dir)
    spec = importlib.util.spec_from_file_location("brmmedia_webui_under_test", WEBUI_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class TaskQueueTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.output_dir = Path(cls.temp_dir.name)
        cls.webui = _load_webui(cls.output_dir)

    @classmethod
    def tearDownClass(cls):
        cls.temp_dir.cleanup()
        os.environ.pop("BRM_OUTPUT_DIR", None)

    def test_pending_task_has_queue_position(self):
        queue = self.webui.TaskQueue(lambda task: None, max_done=5)
        task = self.webui.Task("pending-1", "pending", "unit", {})

        self.assertEqual(queue.enqueue(task), 1)
        status = queue.task_status(task.id)

        self.assertEqual(status["state"], "queued")
        self.assertEqual(status["queue_position"], 1)
        self.assertEqual(status["output_files"], [])

    def test_prompt_and_progress_are_persisted_and_exposed(self):
        queue = self.webui.TaskQueue(lambda task: None, max_done=5)
        task = self.webui.Task("progress-1", "progress", "MiniMaxH3-unit", {})
        queue.enqueue(task)
        with queue._lock:
            queue._pending.clear()
            task.status = self.webui.TaskStatus.RUNNING
            queue._running[task.id] = task

        queue.update_execution(task, {
            "prompt_id": "comfy-prompt-123",
            "stage": "sampling",
            "node_id": "15",
            "current_step": 3,
            "total_steps": 8,
            "progress": 0.375,
        })
        status = queue.task_status(task.id)
        self.assertEqual(status["prompt_id"], "comfy-prompt-123")
        self.assertEqual(status["execution"]["stage"], "sampling")
        self.assertEqual(status["execution"]["current_step"], 3)
        self.assertEqual(status["execution"]["total_steps"], 8)
        self.assertEqual(status["execution"]["progress"], 0.375)

        saved = json.loads((self.output_dir / "task-history.json").read_text(encoding="utf-8"))
        record = next(item for item in saved["tasks"] if item["id"] == task.id)
        self.assertEqual(saved["version"], 2)
        self.assertEqual(record["prompt_id"], "comfy-prompt-123")
        self.assertEqual(record["stage"], "sampling")

    def test_restart_reattaches_running_prompt_without_resubmitting(self):
        history = {
            "version": 2,
            "tasks": [{
                "id": "recover-1",
                "name": "recover",
                "workflow_name": "MiniMaxH3-unit",
                "status": self.webui.TaskStatus.RUNNING.value,
                "submit_ts": 1,
                "start_ts": 2,
                "done_ts": 0,
                "result": [],
                "error": "",
                "prompt_id": "comfy-existing-prompt",
                "stage": "sampling",
                "current_step": 2,
                "total_steps": 8,
                "progress": 0.25,
                "last_progress_ts": 3,
                "effective_settings": {},
            }],
        }
        (self.output_dir / "task-history.json").write_text(
            json.dumps(history), encoding="utf-8"
        )
        queue = self.webui.TaskQueue(lambda task: None, max_done=5)

        pending, running, done = queue.snapshot()
        self.assertEqual(len(pending), 1)
        self.assertEqual(running, [])
        self.assertEqual(done, [])
        self.assertEqual(pending[0].prompt_id, "comfy-existing-prompt")
        self.assertTrue(pending[0].recovered)
        self.assertEqual(queue.task_status("recover-1")["state"], "queued")

    def test_recoverable_prompt_is_not_evicted_by_completed_history_limit(self):
        artifact = self.output_dir / "old.png"
        artifact.write_bytes(b"fixture")
        tasks = [{
            "id": "recover-priority", "name": "recover", "workflow_name": "MiniMaxH3-unit",
            "status": self.webui.TaskStatus.RUNNING.value,
            "submit_ts": 1, "start_ts": 2, "done_ts": 0, "result": [], "error": "",
            "prompt_id": "live-prompt", "effective_settings": {},
        }]
        tasks.extend({
            "id": f"done-{index}", "name": "done", "workflow_name": "unit",
            "status": self.webui.TaskStatus.DONE.value,
            "submit_ts": 10 + index, "start_ts": 10 + index, "done_ts": 10 + index,
            "result": [str(artifact)], "error": "", "prompt_id": "",
            "effective_settings": {},
        } for index in range(4))
        (self.output_dir / "task-history.json").write_text(
            json.dumps({"version": 2, "tasks": tasks}), encoding="utf-8"
        )
        queue = self.webui.TaskQueue(lambda task: None, max_done=2)

        pending, _, done = queue.snapshot()
        self.assertEqual([task.id for task in pending], ["recover-priority"])
        self.assertEqual(len(done), 2)

    def test_restart_does_not_replay_running_task_without_prompt_id(self):
        history = {
            "version": 2,
            "tasks": [{
                "id": "unsafe-replay-1", "name": "unsafe", "workflow_name": "unit",
                "status": self.webui.TaskStatus.RUNNING.value,
                "submit_ts": 1, "start_ts": 2, "done_ts": 0,
                "result": [], "error": "", "prompt_id": "", "effective_settings": {},
            }],
        }
        (self.output_dir / "task-history.json").write_text(json.dumps(history), encoding="utf-8")
        queue = self.webui.TaskQueue(lambda task: None, max_done=5)

        status = queue.task_status("unsafe-replay-1")
        self.assertEqual(status["state"], "failed")
        self.assertIn("无法安全重放", status["error"])

    def test_cancelled_task_is_not_reported_as_failed(self):
        queue = self.webui.TaskQueue(lambda task: None, max_done=5)
        task = self.webui.Task("cancel-1", "cancel", "unit", {})
        queue.enqueue(task)
        with queue._lock:
            queue._pending.clear()
            task.status = self.webui.TaskStatus.RUNNING
            queue._running[task.id] = task

        self.assertEqual(queue.cancel_running(), [task])
        self.assertTrue(task.cancel_event.is_set())
        self.assertFalse(queue.stop_event.is_set())

        task.status = self.webui.TaskStatus.CANCELLED
        task.error = "用户请求中断"
        with queue._lock:
            queue._running.clear()
            queue._done.append(task)

        status = queue.task_status(task.id)
        self.assertEqual(status["state"], "cancelled")
        self.assertEqual(status["status"], "已中断")
        self.assertEqual(status["error"], "用户请求中断")

    def test_cleared_pending_task_remains_queryable_as_cancelled(self):
        queue = self.webui.TaskQueue(lambda task: None, max_done=5)
        task = self.webui.Task("cleared-1", "cleared", "unit", {})
        queue.enqueue(task)

        self.assertEqual(queue.clear_pending(), 1)
        status = queue.task_status(task.id)

        self.assertEqual(status["state"], "cancelled")
        self.assertEqual(status["status"], "已中断")
        self.assertEqual(status["error"], "队列已清空")
        self.assertIsNone(status["queue_position"])

    def test_interrupt_marks_only_snapshot_tasks_after_comfy_accepts(self):
        queue = self.webui.TaskQueue(lambda task: None, max_done=5)
        task = self.webui.Task("running-1", "running", "unit", {})
        with queue._lock:
            task.status = self.webui.TaskStatus.RUNNING
            queue._running[task.id] = task

        original_queue = self.webui.task_queue
        original_interrupt = self.webui.interrupt
        self.addCleanup(setattr, self.webui, "task_queue", original_queue)
        self.addCleanup(setattr, self.webui, "interrupt", original_interrupt)
        self.webui.task_queue = queue
        self.webui.interrupt = lambda: (True, "已发送 ComfyUI 中断信号。")

        result = self.webui.interrupt_running_tasks()

        self.assertIn("已请求中断 1 个运行任务", result)
        self.assertTrue(task.cancel_event.is_set())

    def test_interrupt_failure_keeps_workspace_task_running(self):
        queue = self.webui.TaskQueue(lambda task: None, max_done=5)
        task = self.webui.Task("running-2", "running", "unit", {})
        with queue._lock:
            task.status = self.webui.TaskStatus.RUNNING
            queue._running[task.id] = task

        original_queue = self.webui.task_queue
        original_interrupt = self.webui.interrupt
        self.addCleanup(setattr, self.webui, "task_queue", original_queue)
        self.addCleanup(setattr, self.webui, "interrupt", original_interrupt)
        self.webui.task_queue = queue
        self.webui.interrupt = lambda: (False, "中断失败: connection refused")

        result = self.webui.interrupt_running_tasks()

        self.assertIn("当前工作台任务仍保持原状态", result)
        self.assertFalse(task.cancel_event.is_set())

    def test_history_restores_completed_task_without_leaking_paths(self):
        artifact = self.output_dir / "generated.png"
        artifact.write_bytes(b"not an image, only a queue fixture")
        queue = self.webui.TaskQueue(lambda task: None, max_done=5)
        completed = self.webui.Task(
            "done-1",
            "done",
            "unit",
            {},
            status=self.webui.TaskStatus.DONE,
            result=[str(artifact)],
        )
        queue._done = [completed]
        queue._save_history()

        restored = self.webui.TaskQueue(lambda task: None, max_done=5)
        status = restored.task_status(completed.id)

        self.assertEqual(status["state"], "completed")
        self.assertEqual(status["output_files"], ["generated.png"])
        self.assertNotIn(str(self.output_dir), json.dumps(status, ensure_ascii=False))

    def test_history_rejects_path_outside_outputs(self):
        external_dir = tempfile.TemporaryDirectory()
        self.addCleanup(external_dir.cleanup)
        external_file = Path(external_dir.name) / "unrelated.png"
        external_file.write_bytes(b"outside the workspace output directory")
        history = {
            "version": 1,
            "tasks": [{
                "id": "outside-1",
                "name": "outside",
                "workflow_name": "unit",
                "status": self.webui.TaskStatus.DONE.value,
                "submit_ts": 1,
                "start_ts": 1,
                "done_ts": 1,
                "result": [str(external_file)],
                "error": "",
            }],
        }
        (self.output_dir / "task-history.json").write_text(
            json.dumps(history), encoding="utf-8"
        )

        restored = self.webui.TaskQueue(lambda task: None, max_done=5)

        self.assertEqual(restored.task_status("outside-1")["state"], "not_found")

    def test_unknown_task_is_explicit(self):
        queue = self.webui.TaskQueue(lambda task: None, max_done=5)
        self.assertEqual(queue.task_status("not-here")["state"], "not_found")

    def test_h3_profile_normalisation_uses_supported_canvas_and_frame_grid(self):
        draft = self.webui.normalise_h3_request("768 × 1024", 3, "draft")
        self.assertEqual(draft["frames"], 73)
        self.assertEqual(draft["effective_seconds"], 3.042)

        minimum = self.webui.normalise_h3_request("768 × 1024", 4, "preview")
        self.assertEqual(minimum["frames"], 107)
        self.assertEqual(minimum["effective_seconds"], 4.458)

        preview = self.webui.normalise_h3_request("1920 × 1080", 5, "preview")
        self.assertEqual((preview["width"], preview["height"]), (864, 480))
        self.assertEqual(preview["frames"], 124)
        self.assertEqual(preview["effective_seconds"], 5.167)

        quality = self.webui.normalise_h3_request("1080 × 1920", 6, "quality")
        self.assertEqual((quality["width"], quality["height"]), (768, 1344))
        self.assertEqual(quality["frames"], 158)

    def test_h3_profile_switch_uses_the_documented_default_duration(self):
        self.assertEqual(self.webui.h3_profile_default_seconds("draft"), 3)
        self.assertEqual(self.webui.h3_profile_default_seconds("preview"), 5)
        self.assertEqual(self.webui.h3_profile_default_seconds("quality"), 6)
        self.assertEqual(self.webui.h3_profile_default_seconds("unknown"), 5)

    def test_h3_profile_switch_resets_and_constrains_duration_component(self):
        quality = self.webui.h3_profile_duration_update("quality")
        self.assertEqual(quality["value"], 6)
        self.assertEqual(quality["minimum"], 4)
        self.assertEqual(quality["maximum"], 15)
        draft = self.webui.h3_profile_duration_update("draft")
        self.assertEqual(draft["value"], 3)
        self.assertEqual(draft["minimum"], 3)
        self.assertEqual(draft["maximum"], 3)

    def test_h3_quality_allows_15_second_production_request(self):
        quality = self.webui.normalise_h3_request("768 × 1024", 15, "quality")
        self.assertEqual(quality["requested_seconds"], 15)
        self.assertEqual(quality["frames"] % 17, 5)

    def test_h3_acceleration_defaults_to_unchanged_standard_path(self):
        request = self.webui.normalise_h3_request("1920 × 1080", 5, "preview")
        self.assertEqual(request["acceleration"], "standard")
        self.assertEqual(request["steps"], 20)
        self.assertEqual(request["sampler"], "res_multistep")
        self.assertIsNone(request["lora_name"])

    def test_h3_turbo_modes_expose_lightx2v_v1_settings(self):
        balanced = self.webui.normalise_h3_request(
            "768 × 1024", 5, "preview", "turbo_balanced"
        )
        self.assertEqual(balanced["steps"], 8)
        self.assertEqual(balanced["sampler"], "euler")
        self.assertEqual((balanced["shift_video"], balanced["shift_audio"]), (12.0, 3.0))
        self.assertIn("8step_v1.0_comfyui", balanced["lora_name"])

        fast = self.webui.normalise_h3_request(
            "1920 × 1080", 6, "quality", "turbo_fast"
        )
        self.assertEqual((fast["width"], fast["height"]), (1344, 768))
        self.assertEqual(fast["steps"], 4)
        self.assertEqual((fast["shift_video"], fast["shift_audio"]), (6.0, 3.0))
        with self.assertRaises(self.webui.gr.Error):
            self.webui.normalise_h3_request("768 × 1024", 6, "quality", "turbo_fast")

    def test_h3_mode_forces_single_media_queue(self):
        previous_engine = os.environ.get("BRMMEDIA_VIDEO_ENGINE")
        try:
            os.environ["BRMMEDIA_VIDEO_ENGINE"] = "h3"
            with tempfile.TemporaryDirectory() as temp_dir:
                h3_webui = _load_webui(Path(temp_dir))
            self.assertEqual(h3_webui.MAX_MEDIA_QUEUE_CONCURRENCY, 1)
            self.assertEqual(h3_webui.QUEUE_CONCURRENCY, 1)
        finally:
            if previous_engine is None:
                os.environ.pop("BRMMEDIA_VIDEO_ENGINE", None)
            else:
                os.environ["BRMMEDIA_VIDEO_ENGINE"] = previous_engine

    def test_h3_workflow_builders_forward_actual_canvas_and_source_image(self):
        args = {
            "prompt": "rain falls on a city street", "width": 864, "height": 480,
            "frames": 124, "input_filename": "source.png",
        }
        t2v = self.webui.build_workflow_3("MiniMaxH3-文生视频", args)
        self.assertEqual(t2v["104"]["inputs"]["prompt"], args["prompt"])
        self.assertEqual(t2v["104"]["inputs"]["length"], 124)
        self.assertEqual(t2v["91"]["inputs"]["audio"], ["23", 0])
        self.assertEqual(t2v["25"]["class_type"], "MiniMaxH3SigmaShift")
        self.assertEqual(t2v["26"]["class_type"], "PathchSageAttentionKJ")
        self.assertEqual(t2v["26"]["inputs"]["model"], ["25", 0])
        self.assertEqual(t2v["9"]["inputs"]["model"], ["26", 0])
        self.assertEqual(t2v["16"]["inputs"]["model"], ["26", 0])

        standard_nodes = [node for node in t2v.values() if node["class_type"] == "LoraLoaderModelOnly"]
        self.assertEqual(standard_nodes, [])

        turbo_args = dict(args, acceleration="turbo_balanced")
        turbo = self.webui.build_workflow_3("MiniMaxH3-文生视频", turbo_args)
        lora_id = next(key for key, node in turbo.items() if node["class_type"] == "LoraLoaderModelOnly")
        self.assertEqual(turbo[lora_id]["inputs"]["model"], ["6", 0])
        self.assertEqual(turbo["25"]["inputs"]["model"], [lora_id, 0])
        self.assertEqual(turbo["25"]["inputs"]["shift_video"], 12.0)
        self.assertEqual(turbo["17"]["inputs"]["sampler_name"], "euler")
        self.assertEqual(turbo["9"]["inputs"]["steps"], 8)
        self.assertEqual(turbo["26"]["inputs"]["model"], ["25", 0])

        i2v = self.webui.build_workflow_4("MiniMaxH3-图生视频", args)
        self.assertEqual(i2v["1"]["inputs"]["image"], "source.png")
        self.assertEqual(i2v["104"]["inputs"]["first_frame"], ["1", 0])
        self.assertEqual(i2v["25"]["class_type"], "MiniMaxH3SigmaShift")
        self.assertEqual(i2v["26"]["class_type"], "PathchSageAttentionKJ")

        fast_i2v = self.webui.build_workflow_4(
            "MiniMaxH3-图生视频", dict(args, acceleration="turbo_fast")
        )
        fast_lora_id = next(
            key for key, node in fast_i2v.items()
            if node["class_type"] == "LoraLoaderModelOnly"
        )
        self.assertIn("4step_v1.0_768p_comfyui", fast_i2v[fast_lora_id]["inputs"]["lora_name"])
        self.assertEqual(fast_i2v["25"]["inputs"]["shift_video"], 6.0)
        self.assertEqual(fast_i2v["17"]["inputs"]["sampler_name"], "euler")
        self.assertEqual(fast_i2v["9"]["inputs"]["steps"], 4)

    def test_acestep_model_choices_follow_installed_weights(self):
        model_root = self.output_dir / "fake-comfy"
        turbo = model_root / "models" / "diffusion_models" / "acestep" / "acestep_v1.5_xl_turbo_bf16.safetensors"
        turbo.parent.mkdir(parents=True)
        turbo.write_bytes(b"fixture")
        old_root = os.environ.get("COMFYUI_ROOT")
        self.addCleanup(
            lambda: os.environ.__setitem__("COMFYUI_ROOT", old_root)
            if old_root is not None else os.environ.pop("COMFYUI_ROOT", None)
        )
        os.environ["COMFYUI_ROOT"] = str(model_root)

        self.assertEqual(self.webui.installed_acestep_models(), ["turbo"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
