#!/usr/bin/env python3
"""Comprehensive positive and negative unit tests for application-layer performance optimizations."""

from __future__ import annotations

import io
import json
import math
import os
import struct
import sys
import tempfile
import threading
import time
import types
import unittest
import wave
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

REPO_ROOT = Path(__file__).resolve().parents[1]
DEPLOY_DIR = REPO_ROOT / "ubuntu-backend-deploy"
TESTS_DIR = REPO_ROOT / "tests"
for p in (str(DEPLOY_DIR), str(TESTS_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

import adaptive_concurrency as ac
import llm_runtime
import media_runtime_guard as guard
import system_telemetry
import zonos3_support
from test_comfyui_task_lifecycle import _load_module as _load_comfyui_server
from test_lan_api_contract import _load_lan_api
from test_task_queue import _load_webui


HEALTHY_HW = {
    "cpu": {"cpu_count": 64, "load_1m": 0.4, "usage_pct": 2.0},
    "ram": {"used_gib": 47.5, "avail_gib": 78.1, "total_gib": 125.6, "percent": 37.8},
    "gpus": [{"index": 0, "name": "NVIDIA RTX A5000", "util_pct": 5.0, "vram_used_mib": 350, "vram_total_mib": 24564}],
}


def _make_sine_wav(duration_s: float = 0.12, framerate: int = 24000, freq: float = 440.0) -> bytes:
    num_frames = int(framerate * duration_s)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(framerate)
        samples = [
            int(12000 * math.sin(2.0 * math.pi * freq * (i / framerate)))
            for i in range(num_frames)
        ]
        wf.writeframes(struct.pack(f"<{len(samples)}h", *samples))
    return buf.getvalue()


class ModelGroupAndSingleInstanceFreeTests(unittest.TestCase):
    """Requirement 1: Single-instance :8188 redundant /free elimination by model_group."""

    def setUp(self):
        guard.clear_last_model_group()

    def tearDown(self):
        guard.clear_last_model_group()

    def test_workflow_model_group_mapping(self):
        self.assertEqual(guard.workflow_model_group("QwenImage21-文生图"), "qwen21")
        self.assertEqual(guard.workflow_model_group("qwen-image21-edit"), "qwen21")
        self.assertEqual(guard.workflow_model_group("MiniMaxH3-文生视频"), "h3")
        self.assertEqual(guard.workflow_model_group("LTX23-图生视频"), "ltx23")
        self.assertEqual(guard.workflow_model_group("SeedVR2-图像/视频增强修复", {"model_variant": "7b"}), "seedvr2_7b")
        self.assertEqual(guard.workflow_model_group("SeedVR2-图像/视频增强修复", {"model_variant": "3b"}), "seedvr2_3b")
        self.assertEqual(guard.workflow_model_group("image_z_image_turbo"), "z_image")
        self.assertEqual(guard.workflow_model_group("Flux2Klein-快速图生图"), "flux2")
        self.assertEqual(guard.workflow_model_group("音乐生成", {"model": "turbo"}), "acestep_turbo")
        self.assertEqual(guard.workflow_model_group("音乐生成", {"model": "base"}), "acestep_base")
        self.assertEqual(guard.workflow_model_group("IndexTTS-2.5-语音克隆"), "indextts25")
        self.assertEqual(guard.workflow_model_group("Zonos3-语音克隆"), "zonos3")

    def test_single_instance_same_model_group_skips_pre_and_post_free(self):
        base = "http://127.0.0.1:8188"
        session = Mock()
        session.__enter__ = Mock(return_value=session)
        session.__exit__ = Mock(return_value=False)
        session.get.return_value.json.return_value = {"queue_running": [], "queue_pending": []}

        with patch.dict(os.environ, {"BRM_QWEN_IMAGE21_ENABLED": "1", "BRM_QWEN_IMAGE21_URL": base, "BRM_H3_IMAGE_ENABLED": "0", "BRM_H3_IMAGE_RUNTIME_READY": "0"}), \
             patch.object(guard.requests, "Session", return_value=session), \
             patch.object(guard, "gpu_used_mib", return_value=1024):
            self.assertEqual(guard.runtime_bases(base), [base])
            # 1) Cold entry for qwen21 -> executes /free once and records qwen21
            guard.handover(base, model_group="qwen21")
            self.assertEqual(session.post.call_count, 1)
            self.assertEqual(guard.get_last_model_group(base), "qwen21")

            # 2) Post-task release_all with same model_group & healthy VRAM -> skips /free
            guard.release_all(base, model_group="qwen21", force_free=False)
            self.assertEqual(session.post.call_count, 1)

            # 3) Next task with same model_group ("qwen21") -> skips pre-task /free
            guard.handover(base, model_group="qwen21")
            self.assertEqual(session.post.call_count, 1)

            # 4) Negative case A: cross model_group ("h3") -> must call /free
            guard.handover(base, model_group="h3")
            self.assertEqual(session.post.call_count, 2)
            self.assertEqual(guard.get_last_model_group(base), "h3")

            # 5) Negative case B: VRAM exceeds safety threshold -> must call /free even for same group
            with patch.object(guard, "gpu_used_mib", side_effect=[23800, 1024]):
                guard.handover(base, model_group="h3", max_vram_mib=22528)
            self.assertEqual(session.post.call_count, 3)

            # 6) Negative case C: task failed (force_free=True) -> must call /free
            guard.release_all(base, model_group="h3", force_free=True)
            self.assertEqual(session.post.call_count, 4)


class TaskQueueAffinityAgingAndThrottledSaveTests(unittest.TestCase):
    """Requirement 2: TaskQueue model affinity, aging anti-starvation, HOL backfill, throttled disk save."""

    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.webui = _load_webui(Path(cls.temp_dir.name))

    @classmethod
    def tearDownClass(cls):
        cls.temp_dir.cleanup()
        os.environ.pop("BRM_OUTPUT_DIR", None)

    def test_update_execution_throttles_step_progress_and_immediately_saves_critical_changes(self):
        q = self.webui.TaskQueue(lambda t: None, max_done=10)
        task = self.webui.Task("t-throttle", "t-throttle", "QwenImage21-文生图", {})
        q.enqueue(task)
        with q._lock:
            q._pending.clear()
            task.status = self.webui.TaskStatus.RUNNING
            q._running[task.id] = task

        with patch.object(q, "_save_history", wraps=q._save_history) as save_spy:
            # 1) Critical change: first prompt_id + stage change -> immediate synchronous save
            q.update_execution(task, {"prompt_id": "pid-99", "stage": "sampling", "current_step": 1, "total_steps": 10, "progress": 0.1})
            self.assertEqual(save_spy.call_count, 1)
            self.assertEqual(save_spy.call_args.kwargs.get("async_write"), False)

            # 2) Pure step progress within 5s window -> in-memory updated, no immediate _save_history call
            for step in range(2, 8):
                q.update_execution(task, {"stage": "sampling", "current_step": step, "total_steps": 10, "progress": step / 10.0})
            self.assertEqual(save_spy.call_count, 1)
            self.assertEqual(q.task_status(task.id)["execution"]["current_step"], 7)

            # 3) Critical change: stage transition -> immediate synchronous save
            q.update_execution(task, {"stage": "saving_artifacts", "progress": 1.0})
            self.assertEqual(save_spy.call_count, 2)
            self.assertEqual(save_spy.call_args.kwargs.get("async_write"), False)

    def test_model_affinity_streak_cap_and_starvation_aging(self):
        controller = ac.AdaptiveConcurrencyController(
            probe_fn=lambda: HEALTHY_HW,
            config_provider=lambda: ac.AdaptiveConfig(starvation_age_seconds=60.0, sticky_streak_max=4),
            cache_ttl=0.0,
        )
        q = self.webui.TaskQueue(lambda t: None, max_done=20, concurrency_controller=controller)
        now = time.time()

        # Simulate GPU last ran qwen21 (streak=1)
        q._last_gpu_model_group = "qwen21"
        q._sticky_streak = 1

        h3_head = self.webui.Task("h3-1", "h3-1", "MiniMaxH3-文生视频", {}, submit_ts=now)
        qwen_2 = self.webui.Task("q2", "q2", "QwenImage21-文生图", {}, submit_ts=now + 1)
        qwen_3 = self.webui.Task("q3", "q3", "QwenImage21-图生图", {}, submit_ts=now + 2)
        qwen_4 = self.webui.Task("q4", "q4", "QwenImage21-文生图", {}, submit_ts=now + 3)
        qwen_5 = self.webui.Task("q5", "q5", "QwenImage21-文生图", {}, submit_ts=now + 4)

        with q._lock:
            q._pending.extend([h3_head, qwen_2, qwen_3, qwen_4, qwen_5])
            # Affinity pops qwen_2, qwen_3, qwen_4 while streak < 4 and h3_head age < 60s
            self.assertIs(q._pop_next_task_locked(), qwen_2)
            self.assertEqual(q._sticky_streak, 2)
            self.assertIs(q._pop_next_task_locked(), qwen_3)
            self.assertEqual(q._sticky_streak, 3)
            self.assertIs(q._pop_next_task_locked(), qwen_4)
            self.assertEqual(q._sticky_streak, 4)

            # Negative case A: streak reached cap (4) -> must pop h3_head instead of qwen_5!
            self.assertIs(q._pop_next_task_locked(), h3_head)
            self.assertEqual(q._last_gpu_model_group, "h3")
            self.assertEqual(q._sticky_streak, 1)

        # Negative case B: head_gpu aged past starvation_age_seconds (60s) -> must ignore affinity immediately
        q._last_gpu_model_group = "qwen21"
        q._sticky_streak = 1
        aged_h3 = self.webui.Task("h3-old", "h3-old", "MiniMaxH3-文生视频", {}, submit_ts=now - 70.0)
        qwen_fresh = self.webui.Task("q-fresh", "q-fresh", "QwenImage21-文生图", {}, submit_ts=now)
        with q._lock:
            q._pending.clear()
            q._pending.extend([aged_h3, qwen_fresh])
            self.assertIs(q._pop_next_task_locked(), aged_h3)


class DynamicCostExternalQueueAndCooldownTests(unittest.TestCase):
    """Requirement 3: Cost budget admission, external ComfyUI /queue awareness, and failure/OOM cooldown."""

    def test_estimate_task_cost_and_vram_budget_admission(self):
        small = ac.estimate_task_cost("image_z_image_turbo", {"size": "1024 × 1024", "batch": 1})
        huge = ac.estimate_task_cost("image_z_image_turbo", {"size": "2048 × 2048", "batch": 4})
        self.assertLess(small["est_vram_mib"], 9000)
        self.assertGreater(huge["est_vram_mib"], 20000)

        snap = ac.evaluate_concurrency(
            cpu=HEALTHY_HW["cpu"], ram=HEALTHY_HW["ram"], gpus=HEALTHY_HW["gpus"]
        )
        # Positive: two small 1024x1024 batch=1 tasks fit concurrently
        self.assertTrue(
            ac.can_admit_gpu_task(
                "image_z_image_turbo",
                {"size": "1024 × 1024", "batch": 1},
                [("image_z_image_turbo", {"size": "1024 × 1024", "batch": 1})],
                snap,
            )
        )
        # Negative: a 2048x2048 batch=4 task is rejected while another task is running
        self.assertFalse(
            ac.can_admit_gpu_task(
                "image_z_image_turbo",
                {"size": "2048 × 2048", "batch": 4},
                [("image_z_image_turbo", {"size": "1024 × 1024", "batch": 1})],
                snap,
            )
        )

    def test_external_comfy_queue_and_oom_failure_cooldown(self):
        ext_queue = {"_available": True, "queue_running": [[1, "ext-pid"]], "queue_pending": []}
        owned_ids = {"owned-pid"}
        ctrl = ac.AdaptiveConcurrencyController(
            probe_fn=lambda: HEALTHY_HW,
            config_provider=lambda: ac.AdaptiveConfig(failure_cooldown_seconds=30.0),
            cache_ttl=0.0,
            comfy_queue_fn=lambda: ext_queue,
            known_prompt_ids_fn=lambda: owned_ids,
        )
        # 1) External unowned prompt in ComfyUI /queue -> throttles light GPU to 1
        snap_ext = ctrl.evaluate(force_refresh=True)
        self.assertEqual(snap_ext["external_queue_depth"], 1)
        self.assertEqual(snap_ext["limits"][ac.TIER_IMAGE_LIGHT], 1)
        self.assertIn("external_comfy_queue_busy", snap_ext["degrade_reasons"])

        # 2) When queue only contains owned prompt -> expands back to 3
        ext_queue["queue_running"] = [[1, "owned-pid"]]
        snap_owned = ctrl.evaluate(force_refresh=True)
        self.assertEqual(snap_owned["external_queue_depth"], 0)
        self.assertEqual(snap_owned["limits"][ac.TIER_IMAGE_LIGHT], 3)

        # 3) Record CUDA OOM failure -> triggers automatic GPU cooldown to 1
        ctrl.record_task_outcome("image_z_image_turbo", success=False, duration_s=4.2, error="CUDA out of memory")
        snap_oom = ctrl.evaluate(force_refresh=True)
        self.assertTrue(snap_oom["gpu_cooldown_active"])
        self.assertEqual(snap_oom["limits"][ac.TIER_IMAGE_LIGHT], 1)
        self.assertIn("recent_gpu_oom_cooldown", snap_oom["degrade_reasons"])

        # 4) Clear cooldown -> restores normal concurrency 3
        ctrl.clear_cooldown()
        snap_recovered = ctrl.evaluate(force_refresh=True)
        self.assertFalse(snap_recovered["gpu_cooldown_active"])
        self.assertEqual(snap_recovered["limits"][ac.TIER_IMAGE_LIGHT], 3)


class GpuDecouplingStreamingAndWsWakeupTests(unittest.TestCase):
    """Requirement 4: GPU pre/post-processing decoupling, streaming copy, and WS execution_success wakeup."""

    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.webui = _load_webui(Path(cls.temp_dir.name))
        prev_req = sys.modules.get("requests")
        try:
            cls.comfy = _load_comfyui_server()
        finally:
            if prev_req is not None:
                sys.modules["requests"] = prev_req

    @classmethod
    def tearDownClass(cls):
        cls.temp_dir.cleanup()
        os.environ.pop("BRM_OUTPUT_DIR", None)

    def test_indextts25_releases_gpu_before_ffmpeg_mp3_transcode(self):
        events = []

        @contextmanager
        def track_gpu_guard():
            events.append("gpu_enter")
            try:
                yield
            finally:
                events.append("gpu_exit")

        def on_gpu_done():
            events.append("gpu_slot_released")

        def fake_ffmpeg(cmd, **kwargs):
            events.append("ffmpeg_mp3")
            Path(cmd[-1]).write_bytes(b"ID3-fake-mp3")
            return SimpleNamespace(returncode=0, stderr="")

        with tempfile.TemporaryDirectory() as tmp:
            input_dir = Path(tmp) / "input"
            input_dir.mkdir(parents=True)
            ref = input_dir / "ref.wav"
            ref.write_bytes(_make_sine_wav())
            task = self.webui.Task("tts-1", "tts_out", "IndexTTS-2.5-语音克隆", {"prompt": "你好", "ref_audio": "ref.wav", "language": "zh", "speed": 1.0})
            health_resp = Mock(status_code=200)
            health_resp.json.return_value = {"ok": True}
            synth_resp = Mock(status_code=200, content=_make_sine_wav())
            with patch.object(self.webui, "COMFY_ROOT", Path(tmp)), \
                 patch.object(self.webui.requests, "get", return_value=health_resp, create=True), \
                 patch.object(self.webui.requests, "post", return_value=synth_resp, create=True), \
                 patch.object(self.webui.shutil, "which", return_value="/usr/bin/ffmpeg"), \
                 patch.object(self.webui.subprocess, "run", side_effect=fake_ffmpeg):
                out = self.webui.run_indextts25(task, gpu_guard=track_gpu_guard, on_gpu_done=on_gpu_done)
            self.assertEqual(len(out), 1)
            self.assertEqual(events, ["gpu_enter", "gpu_exit", "gpu_slot_released", "ffmpeg_mp3"])

    def test_seedvr2_pre_slice_and_post_mux_run_outside_gpu_guard(self):
        events = []

        @contextmanager
        def track_gpu_guard():
            events.append("gpu_enter")
            try:
                yield
            finally:
                events.append("gpu_exit")

        with tempfile.TemporaryDirectory() as tmp:
            comfy_root = Path(tmp)
            in_dir = comfy_root / "input"
            in_dir.mkdir(parents=True)
            src = in_dir / "src.mp4"
            src.write_bytes(b"fake-video")
            info = {"width": 640, "height": 360, "duration": 10.0, "fps": 24.0, "fps_text": "24", "frame_count": 240, "has_audio": False}

            def fake_segments(source, inf, temp_dir, task, deadline):
                events.append("pre_slice")
                seg = temp_dir / "input_0001.mp4"
                seg.write_bytes(b"seg1")
                return [seg]

            def fake_run_workflow(*args, **kwargs):
                events.append("gpu_infer")
                return {"1": {"videos": [{"filename": "out.mp4", "subfolder": "", "type": "output"}]}}

            def fake_extract(outputs, task, *, destination_dir=None, filename_prefix=None):
                Path(destination_dir).mkdir(parents=True, exist_ok=True)
                p = Path(destination_dir) / f"{filename_prefix}.mp4"
                p.write_bytes(b"part-mp4")
                return [str(p)]

            def fake_probe(path):
                if Path(path) != src:
                    events.append("post_probe")
                    return {**info, "width": 1280, "height": 720}
                return info

            task = self.webui.Task("svr-1", "svr_out", "SeedVR2-图像/视频增强修复", {"video_filename": "src.mp4", "scale": 2, "resolution_preset": "native", "strength": "standard", "model_variant": "7b"})
            with patch.object(self.webui, "COMFY_ROOT", comfy_root), \
                 patch.object(self.webui, "probe_video", side_effect=fake_probe), \
                 patch.object(self.webui, "validate_video_info"), \
                 patch.object(self.webui, "available_disk_bytes", return_value=100 * 1024**3), \
                 patch.object(self.webui.shutil, "which", return_value="/usr/bin/ffmpeg"), \
                 patch.object(self.webui, "_seedvr2_video_segments", side_effect=fake_segments), \
                 patch.object(self.webui, "upload_image", return_value="seg1.mp4"), \
                 patch.object(self.webui, "build_seedvr2_video_workflow", return_value={"1": {}}), \
                 patch.object(self.webui, "run_workflow", side_effect=fake_run_workflow), \
                 patch.object(self.webui, "extract_result", side_effect=fake_extract):
                res = self.webui.run_seedvr2_video_task(
                    task,
                    gpu_guard_factory=track_gpu_guard,
                    on_gpu_done=lambda: events.append("gpu_slot_released"),
                )
            self.assertEqual(len(res), 1)
            self.assertEqual(
                events,
                ["pre_slice", "gpu_enter", "gpu_infer", "gpu_exit", "gpu_slot_released", "post_probe"],
            )

    def test_extract_result_prefers_local_streaming_copy_without_http_buffer(self):
        with tempfile.TemporaryDirectory() as tmp:
            comfy_root = Path(tmp) / "comfy"
            out_dir = comfy_root / "output" / "sub"
            out_dir.mkdir(parents=True)
            local_file = out_dir / "gen.png"
            local_file.write_bytes(b"local-streamed-png-data")
            dest_dir = Path(tmp) / "dest"
            task = self.webui.Task("ex-1", " stream_test ", "image_z_image_turbo", {})
            outputs = {"9": {"images": [{"filename": "gen.png", "subfolder": "sub", "type": "output"}]}}
            with patch.object(self.webui, "COMFY_ROOT", comfy_root), \
                 patch.object(self.webui, "get_view_file", side_effect=AssertionError("should not buffer via HTTP when local file exists")):
                saved = self.webui.extract_result(outputs, task, destination_dir=dest_dir)
            self.assertEqual(len(saved), 1)
            self.assertEqual(Path(saved[0]).read_bytes(), b"local-streamed-png-data")

    def test_ws_execution_success_wakes_history_poll_immediately(self):
        class SuccessSocket:
            def __init__(self):
                self.calls = 0

            def recv(self):
                self.calls += 1
                if self.calls == 1:
                    return json.dumps({"type": "executing", "data": {"prompt_id": "pid-fast", "node": "3"}})
                if self.calls == 2:
                    return json.dumps({"type": "execution_success", "data": {"prompt_id": "pid-fast"}})
                raise TimeoutError("timeout")

        histories = iter([
            {},
            {"pid-fast": {"outputs": {"8": {"images": [{"filename": "a.png"}]}}, "status": {"status_str": "success"}}},
        ])
        sleep_calls = []
        with patch.object(self.comfy, "_open_progress_websocket", return_value=SuccessSocket()), \
             patch.object(self.comfy, "get_history", side_effect=lambda _: next(histories)) as hist_spy, \
             patch.object(self.comfy.time, "sleep", side_effect=sleep_calls.append):
            out = self.comfy.wait_for_outputs("pid-fast", timeout=5)
        self.assertIn("8", out)
        # Iteration 1 polled history ({}) and set next_history_poll = now + 3.0s.
        # Iteration 2 received WS execution_success, reset next_history_poll = 0.0, and immediately polled history!
        self.assertEqual(hist_spy.call_count, 2)
        self.assertEqual(len(sleep_calls), 1)


class CrossProcessZonos3AndParallelChunkingTests(unittest.TestCase):
    """Requirement 5: CrossProcessSlotGate and Zonos3 long-text parallel chunking + wave crossfade."""

    def test_cross_process_slot_gate_unifies_two_instances_on_shared_directory(self):
        with tempfile.TemporaryDirectory() as slot_dir:
            gate_webui = ac.CrossProcessSlotGate(lambda: 2, slot_dir=slot_dir, poll_interval=0.02)
            gate_lan_api = ac.CrossProcessSlotGate(lambda: 2, slot_dir=slot_dir, poll_interval=0.02)

            self.assertTrue(gate_webui.acquire(timeout=0.5))
            self.assertTrue(gate_lan_api.acquire(timeout=0.5))
            self.assertEqual(gate_webui.active_count(), 2)
            self.assertEqual(gate_lan_api.active_count(), 2)
            # Negative: 3rd acquire across either gate must fail when limit=2
            self.assertFalse(gate_webui.try_acquire())
            self.assertFalse(gate_lan_api.acquire(timeout=0.08))

            gate_webui.release()
            self.assertEqual(gate_lan_api.active_count(), 1)
            self.assertTrue(gate_lan_api.try_acquire())
            gate_lan_api.release()
            gate_lan_api.release()
            self.assertEqual(gate_webui.active_count(), 0)

    def test_zonos3_long_text_parallel_chunking_crossfade_and_fallback(self):
        short_text = "这是一段短文本，不需要切分。"
        self.assertEqual(zonos3_support.split_zonos3_text(short_text, threshold=300), [short_text])

        long_text = "。".join([f"这是第{i}句测试长文本语音合成，用于检验智能标点分段与并行拼接效果" for i in range(1, 16)]) + "。"
        self.assertGreater(len(long_text), 300)
        chunks = zonos3_support.split_zonos3_text(long_text, threshold=300, max_chunk_chars=140)
        self.assertGreaterEqual(len(chunks), 3)

        with tempfile.TemporaryDirectory() as slot_dir:
            gate = ac.CrossProcessSlotGate(lambda: 4, slot_dir=slot_dir)
            self.assertTrue(gate.acquire())
            try:
                call_texts = []
                lock = threading.Lock()

                def fake_post(p):
                    with lock:
                        call_texts.append(p["text"])
                    return _make_sine_wav(duration_s=0.10, framerate=24000)

                wav_bytes, meta = zonos3_support.synthesize_zonos3_wav(
                    {"text": long_text, "language": "cmn", "seed": 42},
                    service_url="http://127.0.0.1:1919",
                    timeout_seconds=30,
                    gate=gate,
                    post_fn=fake_post,
                    threshold=300,
                    max_chunk_chars=140,
                )
                self.assertTrue(meta["chunked"])
                self.assertEqual(meta["chunks"], len(chunks))
                self.assertGreaterEqual(meta["workers"], 2)
                self.assertTrue(wav_bytes.startswith(b"RIFF"))
                with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
                    self.assertEqual(wf.getframerate(), 24000)
                    self.assertGreater(wf.getnframes(), int(24000 * 0.10 * (len(chunks) - 1)))

                # Negative case: chunk failure automatically falls back to single-segment full text
                attempts = []

                def flaky_post(p):
                    attempts.append(p["text"])
                    if p["text"] != long_text:
                        raise RuntimeError("chunk service glitch")
                    return _make_sine_wav(duration_s=0.15, framerate=24000)

                fallback_wav, fb_meta = zonos3_support.synthesize_zonos3_wav(
                    {"text": long_text, "language": "cmn", "seed": 42},
                    service_url="http://127.0.0.1:1919",
                    timeout_seconds=30,
                    gate=gate,
                    post_fn=flaky_post,
                    threshold=300,
                    max_chunk_chars=140,
                )
                self.assertFalse(fb_meta["chunked"])
                self.assertTrue(fb_meta.get("fallback"))
                self.assertTrue(fallback_wav.startswith(b"RIFF"))
            finally:
                gate.release()


class HotPathGatewayAndTelemetryAccelerationTests(unittest.TestCase):
    """Requirement 6 & 7: lan_api caching & X-Accel-Redirect, pynvml, asset count, Strata auto_enhance."""

    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.webui = _load_webui(Path(cls.temp_dir.name))
        cls.api = _load_lan_api()

    @classmethod
    def tearDownClass(cls):
        cls.temp_dir.cleanup()
        os.environ.pop("BRM_OUTPUT_DIR", None)

    def test_lan_api_gradio_info_cache_and_contract_refresh(self):
        self.api._GRADIO_PARAM_CACHE.clear()
        session = Mock()
        info_resp = Mock(status_code=200)
        info_resp.json.return_value = {
            "named_endpoints": {
                "/task_status": {"parameters": [{"parameter_name": "task_id"}]}
            }
        }
        post_resp = Mock(status_code=200)
        post_resp.json.return_value = {"event_id": "ev-1"}
        sse_resp = Mock(status_code=200)
        sse_resp.__enter__ = Mock(return_value=sse_resp)
        sse_resp.__exit__ = Mock(return_value=False)
        sse_resp.iter_lines.return_value = ["event: complete", 'data: [{"state": "completed"}]']

        def side_get(url, **kwargs):
            if url.endswith("/gradio_api/info"):
                return info_resp
            return sse_resp

        session.get.side_effect = side_get
        session.post.return_value = post_resp

        with patch.object(self.api, "_http_session", return_value=session):
            r1 = self.api._gradio_call("task_status", ["t1"])
            r2 = self.api._gradio_call("task_status", ["t2"])
        self.assertEqual(r1["state"], "completed")
        self.assertEqual(r2["state"], "completed")
        info_calls = [c for c in session.get.call_args_list if c.args[0].endswith("/gradio_api/info")]
        self.assertEqual(len(info_calls), 1)

    def test_lan_api_x_accel_redirect_and_file_response_fallback(self):
        tid = "b" * 32
        fname = "demo_video.mp4"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            f = root / fname
            f.write_bytes(b"video-bytes")
            fake_resp_cls = lambda **kw: SimpleNamespace(**kw)
            with patch.object(self.api, "OUTPUT_DIR", root), \
                 patch.object(self.api, "_task_status", return_value={"state": "completed", "output_files": [fname]}), \
                 patch.object(self.api, "_FastAPIResponse", side_effect=fake_resp_cls), \
                 patch.object(self.api, "FileResponse", side_effect=lambda p, **kw: SimpleNamespace(path=p, **kw)):
                # 1) Positive: Request via Nginx proxy (X-Forwarded-For) -> X-Accel-Redirect
                proxy_req = SimpleNamespace(headers={"X-Forwarded-For": "172.16.28.100"})
                accel = self.api.download_artifact(tid, fname, request=proxy_req)
                self.assertEqual(accel.status_code, 200)
                self.assertEqual(accel.headers["X-Accel-Redirect"], f"/internal-outputs/{fname}")

                # 2) Negative: Direct request without proxy headers -> graceful FileResponse fallback
                direct_req = SimpleNamespace(headers={})
                fallback = self.api.download_artifact(tid, fname, request=direct_req)
                self.assertEqual(fallback.path, f)

    def test_pynvml_preferred_and_falls_back_to_nvidia_smi(self):
        fake_pynvml = types.ModuleType("pynvml")
        fake_pynvml.NVML_TEMPERATURE_GPU = 0
        fake_pynvml.nvmlInit = Mock()
        fake_pynvml.nvmlDeviceGetCount = Mock(return_value=1)
        fake_pynvml.nvmlDeviceGetHandleByIndex = Mock(return_value="h0")
        fake_pynvml.nvmlDeviceGetName = Mock(return_value=b"NVIDIA RTX A5000")
        fake_pynvml.nvmlDeviceGetMemoryInfo = Mock(return_value=SimpleNamespace(used=4096 * 1024 * 1024, total=24564 * 1024 * 1024))
        fake_pynvml.nvmlDeviceGetUtilizationRates = Mock(return_value=SimpleNamespace(gpu=35))
        fake_pynvml.nvmlDeviceGetTemperature = Mock(return_value=52)
        fake_pynvml.nvmlDeviceGetPowerUsage = Mock(return_value=115000)

        with patch.dict(sys.modules, {"pynvml": fake_pynvml}), \
             patch.object(system_telemetry.subprocess, "run", side_effect=AssertionError("nvidia-smi should not be called when pynvml succeeds")):
            system_telemetry._nvml_initialized = False
            gpus = system_telemetry.collect_gpu_stats()
            self.assertEqual(len(gpus), 1)
            self.assertEqual(gpus[0]["vram_used_mib"], 4096)
            self.assertEqual(guard.gpu_used_mib(), 4096)

        # Negative: when pynvml raises, seamlessly fall back to nvidia-smi
        fake_pynvml.nvmlDeviceGetCount.side_effect = RuntimeError("NVML driver error")
        smi_out = SimpleNamespace(returncode=0, stdout="0, NVIDIA RTX A5000, 10, 2048, 24564, 48, 65.0\n")
        with patch.dict(sys.modules, {"pynvml": fake_pynvml}), \
             patch.object(system_telemetry.subprocess, "run", return_value=smi_out) as smi_spy:
            gpus_fb = system_telemetry.collect_gpu_stats()
            self.assertEqual(len(gpus_fb), 1)
            self.assertEqual(gpus_fb[0]["vram_used_mib"], 2048)
            smi_spy.assert_called_once()

    def test_incremental_asset_count_and_dashboard_dirty_skip(self):
        self.webui._asset_count_cache = (time.monotonic(), 10, str(self.webui.OUTPUT_DIR))
        with patch.object(Path, "rglob", side_effect=AssertionError("rglob should not run within 5min calibration window")):
            self.assertEqual(self.webui._cached_output_asset_count(), 10)
            self.webui._adjust_cached_output_asset_count(2)
            self.assertEqual(self.webui._cached_output_asset_count(), 12)
            self.webui._adjust_cached_output_asset_count(-1)
            self.assertEqual(self.webui._cached_output_asset_count(), 11)

        # Dashboard dirty flag skip when idle and state_version unchanged
        self.webui._refresh_dashboard_cache(force=True)
        with self.webui._system_status_lock:
            self.webui._system_status_cache = (time.monotonic(), {"_running_count": 0})
        with patch.object(self.webui, "_build_dashboard_snapshot", side_effect=AssertionError("should skip rebuild when version unchanged")) as spy:
            self.webui._refresh_dashboard_cache(force=False)
            spy.assert_not_called()

    def test_strata_auto_enhance_positive_and_fallback_negative(self):
        fake_resp = Mock(status_code=200)
        fake_resp.json.return_value = {
            "choices": [{"message": {"content": "Cinematic neon city in rain, 8k Masterpiece"}}]
        }
        session = Mock()
        session.__enter__ = Mock(return_value=session)
        session.__exit__ = Mock(return_value=False)
        session.post.return_value = fake_resp

        with tempfile.TemporaryDirectory() as lock_dir:
            lock_file = Path(lock_dir) / "missing-traffic.lock"
            with patch.object(llm_runtime, "TRAFFIC_LOCK", lock_file), \
                 patch.dict(os.environ, {"BRM_LLM_TRAFFIC_LOCK": str(lock_file)}), \
                 patch("requests.Session", return_value=session, create=True):
                enhanced = llm_runtime.enhance_prompt("neon city", workflow="text-to-image")
                self.assertEqual(enhanced, "Cinematic neon city in rain, 8k Masterpiece")

            # Negative: Strata error or timeout -> falls back to original prompt without raising
            session.post.side_effect = RuntimeError("Strata connection refused")
            with patch.object(llm_runtime, "TRAFFIC_LOCK", lock_file), \
                 patch.dict(os.environ, {"BRM_LLM_TRAFFIC_LOCK": str(lock_file)}), \
                 patch("requests.Session", return_value=session, create=True):
                fallback = llm_runtime.enhance_prompt("original prompt", workflow="text-to-video")
                self.assertEqual(fallback, "original prompt")

    def test_lan_api_music_models_assets_dotenv_caches_and_submit_auto_enhance(self):
        # 1) _available_music_models 60s TTL cache
        self.api._MUSIC_MODELS_CACHE = ("", 0.0, [])
        session = Mock()
        unet_resp = Mock(status_code=200)
        unet_resp.json.return_value = {
            "UNETLoader": {
                "input": {
                    "required": {
                        "unet_name": [[
                            "acestep/acestep_v1.5_xl_turbo_bf16.safetensors",
                            "acestep/acestep_v1.5_xl_base_bf16.safetensors",
                        ]]
                    }
                }
            }
        }
        session.get.return_value = unet_resp
        with patch.object(self.api, "_http_session", return_value=session):
            m1 = self.api._available_music_models()
            calls_after_first = session.get.call_count
            m2 = self.api._available_music_models()
        self.assertEqual(set(m1), {"turbo", "base"})
        self.assertEqual(m2, m1)
        self.assertGreaterEqual(calls_after_first, 1)
        # Second call within 60s TTL hits memory cache with zero new HTTP requests
        self.assertEqual(session.get.call_count, calls_after_first)

        # 2) .env mtime cache
        with tempfile.TemporaryDirectory() as tmp:
            base_dir = Path(tmp)
            env_file = base_dir / ".env"
            env_file.write_text("BRMMEDIA_VIDEO_ENGINE=ltx23\n", encoding="utf-8")
            with patch.object(self.api, "BASE_DIR", base_dir), \
                 patch.dict(os.environ, {}, clear=False):
                os.environ.pop("BRMMEDIA_VIDEO_ENGINE", None)
                self.assertEqual(self.api._active_video_engine(), "ltx23")
                # Cached read without hitting disk when mtime & size unchanged
                with patch.object(Path, "read_text", side_effect=AssertionError("should hit dotenv mtime cache")):
                    self.assertEqual(self.api._active_video_engine(), "ltx23")
                # Modify .env with different content/size -> cache invalidates automatically
                env_file.write_text("BRMMEDIA_VIDEO_ENGINE=h3\n# updated\n", encoding="utf-8")
                self.assertEqual(self.api._active_video_engine(), "h3")

        # 3) submit_task with auto_enhance=True calls llm_runtime.enhance_prompt and strips auto_enhance before normalizer
        captured_args = []
        with patch.object(self.api.llm_runtime, "enhance_prompt", return_value="enhanced prompt") as enh_spy, \
             patch.object(self.api, "_gradio_call", side_effect=lambda ep, args: (captured_args.append((ep, args)) or {"task_id": "a" * 32, "queue_position": 1})):
            sub = SimpleNamespace(workflow="text-to-image", params={"prompt": "raw", "size": "1024 × 1024", "batch": 1, "auto_enhance": True})
            res = self.api.submit_task(sub)
        self.assertEqual(res["state"], "accepted")
        enh_spy.assert_called_once_with("raw", workflow="text-to-image")
        self.assertEqual(captured_args[0][1][0], "enhanced prompt")

    def test_qwen_api_gateway_wal_and_short_ttl_key_cache_invalidation(self):
        import importlib.util

        gw_path = DEPLOY_DIR / "qwen_api_gateway.py"
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "gw-keys.sqlite3"
            with patch.dict(os.environ, {"BRM_QWEN_API_KEY_DB": str(db_path), "BRM_QWEN_KEY_CACHE_TTL": "8.0"}):
                spec = importlib.util.spec_from_file_location("qwen_gw_perf_test", gw_path)
                gw = importlib.util.module_from_spec(spec)
                assert spec and spec.loader
                spec.loader.exec_module(gw)

                rec, key = gw._create_key("perf-key", 10)
                # First validation populates cache and records use_count=1 synchronously
                gw._validate_and_record_usage(key)
                # Second validation hits memory cache without opening SQLite
                with patch.object(gw.sqlite3, "connect", side_effect=AssertionError("should hit TTL memory cache")):
                    gw._validate_and_record_usage(key)
                # Disabling key flushes buffered usage and invalidates cache immediately
                disabled = gw.disable_api_key(rec["id"])
                self.assertEqual(disabled["status"], "disabled")
                self.assertEqual(disabled["use_count"], 2)
                with self.assertRaises(gw.HTTPException) as err:
                    gw._validate_and_record_usage(key)
                self.assertEqual(err.exception.status_code, 401)


if __name__ == "__main__":
    unittest.main(verbosity=2)
