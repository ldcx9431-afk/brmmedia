#!/usr/bin/env python3
"""Unit and behavioral tests for dynamic workload + hardware adaptive concurrency."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

REPO_ROOT = Path(__file__).resolve().parents[1]
DEPLOY_DIR = REPO_ROOT / "ubuntu-backend-deploy"
if str(DEPLOY_DIR) not in sys.path:
    sys.path.insert(0, str(DEPLOY_DIR))

import adaptive_concurrency as ac
import media_runtime_guard as guard
from test_lan_api_contract import _load_lan_api
from test_task_queue import _load_webui


HEALTHY_HW = {
    "cpu": {"cpu_count": 64, "load_1m": 0.4, "usage_pct": 2.0},
    "ram": {"used_gib": 47.5, "avail_gib": 78.1, "total_gib": 125.6, "percent": 37.8},
    "gpus": [{"index": 0, "name": "NVIDIA RTX A5000", "util_pct": 5.0, "vram_used_mib": 350, "vram_total_mib": 24564}],
}


class AdaptiveConcurrencyPolicyTests(unittest.TestCase):
    def test_workflow_tier_classification(self):
        self.assertEqual(ac.classify_workflow("MiniMaxH3-文生视频").tier, ac.TIER_VIDEO_HEAVY)
        self.assertTrue(ac.classify_workflow("MiniMaxH3-图生视频").exclusive)
        self.assertTrue(ac.classify_workflow("LTX23-文生视频").exclusive)
        self.assertTrue(ac.classify_workflow("SeedVR2-图像/视频增强修复").exclusive)
        self.assertEqual(ac.classify_workflow("QwenImage21-文生图").tier, ac.TIER_IMAGE_HEAVY)
        self.assertTrue(ac.classify_workflow("QwenImage21-图生图").exclusive)

        light = ac.classify_workflow("image_z_image_turbo")
        self.assertEqual(light.tier, ac.TIER_IMAGE_LIGHT)
        self.assertFalse(light.exclusive)
        self.assertTrue(light.shares_comfy_light_lease)

        music = ac.classify_workflow("音乐生成")
        self.assertEqual(music.tier, ac.TIER_AUDIO_GPU)
        self.assertFalse(music.exclusive)
        self.assertTrue(music.shares_comfy_light_lease)

        zonos = ac.classify_workflow("Zonos3-语音克隆")
        self.assertEqual(zonos.tier, ac.TIER_VOICE_CPU)
        self.assertFalse(zonos.exclusive)
        self.assertFalse(zonos.uses_gpu)

        unknown = ac.classify_workflow("unknown-experimental-model")
        self.assertTrue(unknown.exclusive)

    def test_healthy_hardware_expands_light_and_voice_while_locking_heavy_to_1(self):
        snap = ac.evaluate_concurrency(
            cpu=HEALTHY_HW["cpu"],
            ram=HEALTHY_HW["ram"],
            gpus=HEALTHY_HW["gpus"],
            config=ac.AdaptiveConfig(),
        )
        self.assertTrue(snap["adaptive_enabled"])
        self.assertEqual(snap["effective_mode"], "adaptive")
        self.assertFalse(snap["degraded"])
        self.assertEqual(snap["degrade_reasons"], [])
        self.assertEqual(snap["limits"][ac.TIER_VIDEO_HEAVY], 1)
        self.assertEqual(snap["limits"][ac.TIER_IMAGE_HEAVY], 1)
        self.assertEqual(snap["limits"][ac.TIER_IMAGE_LIGHT], 3)
        self.assertEqual(snap["limits"][ac.TIER_AUDIO_GPU], 2)
        self.assertEqual(snap["limits"][ac.TIER_VOICE_CPU], 4)

    def test_fault_injection_high_ram_pressure_degrades_all_to_1(self):
        snap = ac.evaluate_concurrency(
            cpu=HEALTHY_HW["cpu"],
            ram={"used_gib": 117.6, "avail_gib": 8.0, "total_gib": 125.6, "percent": 93.6},
            gpus=HEALTHY_HW["gpus"],
            config=ac.AdaptiveConfig(),
        )
        self.assertTrue(snap["degraded"])
        self.assertIn("high_ram_pressure", snap["degrade_reasons"])
        self.assertEqual(snap["limits"][ac.TIER_IMAGE_LIGHT], 1)
        self.assertEqual(snap["limits"][ac.TIER_AUDIO_GPU], 1)
        self.assertEqual(snap["limits"][ac.TIER_VOICE_CPU], 1)

    def test_fault_injection_low_vram_degrades_gpu_to_1_but_keeps_voice_cpu_decoupled(self):
        snap = ac.evaluate_concurrency(
            cpu=HEALTHY_HW["cpu"],
            ram=HEALTHY_HW["ram"],
            gpus=[{"index": 0, "name": "NVIDIA RTX A5000", "util_pct": 25.0, "vram_used_mib": 21500, "vram_total_mib": 24564}],
            config=ac.AdaptiveConfig(),
        )
        self.assertTrue(snap["degraded"])
        self.assertIn("low_vram", snap["degrade_reasons"])
        self.assertEqual(snap["limits"][ac.TIER_IMAGE_LIGHT], 1)
        self.assertEqual(snap["limits"][ac.TIER_AUDIO_GPU], 1)
        self.assertEqual(snap["limits"][ac.TIER_VOICE_CPU], 4)

    def test_fault_injection_high_cpu_load_degrades_to_1(self):
        snap = ac.evaluate_concurrency(
            cpu={"cpu_count": 64, "load_1m": 58.0, "usage_pct": 94.0},
            ram=HEALTHY_HW["ram"],
            gpus=HEALTHY_HW["gpus"],
            config=ac.AdaptiveConfig(),
        )
        self.assertTrue(snap["degraded"])
        self.assertIn("high_cpu_load", snap["degrade_reasons"])
        self.assertEqual(snap["limits"][ac.TIER_IMAGE_LIGHT], 1)
        self.assertEqual(snap["limits"][ac.TIER_AUDIO_GPU], 1)
        self.assertEqual(snap["limits"][ac.TIER_VOICE_CPU], 1)

    def test_missing_gpu_telemetry_fails_closed_for_gpu_only(self):
        snap = ac.evaluate_concurrency(
            cpu=HEALTHY_HW["cpu"],
            ram=HEALTHY_HW["ram"],
            gpus=[],
            config=ac.AdaptiveConfig(),
        )
        self.assertIn("gpu_telemetry_unavailable", snap["degrade_reasons"])
        self.assertEqual(snap["limits"][ac.TIER_IMAGE_LIGHT], 1)
        self.assertEqual(snap["limits"][ac.TIER_VOICE_CPU], 4)

    def test_config_file_and_env_override_disable(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "yzy_config.json"
            cfg_path.write_text(json.dumps({"adaptive_concurrency": False, "adaptive_voice_cpu_max": 3}), encoding="utf-8")
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("BRM_ADAPTIVE_CONCURRENCY", None)
                cfg = ac.load_adaptive_config(config_path=cfg_path)
            self.assertFalse(cfg.enabled)
            snap = ac.evaluate_concurrency(cpu=HEALTHY_HW["cpu"], ram=HEALTHY_HW["ram"], gpus=HEALTHY_HW["gpus"], config=cfg)
            self.assertEqual(snap["effective_mode"], "static")
            self.assertEqual(snap["limits"][ac.TIER_IMAGE_LIGHT], 1)
            self.assertEqual(snap["limits"][ac.TIER_VOICE_CPU], 1)

            with patch.dict(os.environ, {"BRM_ADAPTIVE_CONCURRENCY": "1", "BRM_ADAPTIVE_VOICE_CPU_MAX": "3"}):
                cfg2 = ac.load_adaptive_config(config_path=cfg_path)
            self.assertTrue(cfg2.enabled)
            self.assertEqual(cfg2.voice_cpu_max, 3)


class AdaptiveTaskQueueBehaviorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.webui = _load_webui(Path(cls.temp_dir.name))
        cls.api = _load_lan_api()

    @classmethod
    def tearDownClass(cls):
        cls.temp_dir.cleanup()
        os.environ.pop("BRM_OUTPUT_DIR", None)

    def test_light_image_parallel_and_cpu_voice_decoupled_from_video_heavy(self):
        hw_state = dict(HEALTHY_HW)
        controller = ac.AdaptiveConcurrencyController(
            probe_fn=lambda: hw_state,
            config_provider=lambda: ac.AdaptiveConfig(),
            cache_ttl=0.0,
        )

        lock = threading.Lock()
        active_gpu = 0
        active_voice = 0
        max_active_light = 0
        max_active_voice = 0
        overlap_video_with_other_gpu = False
        overlap_video_with_voice = False
        video_started = threading.Event()
        release_video = threading.Event()
        all_done = threading.Event()
        completed = []

        def processor(task):
            nonlocal active_gpu, active_voice, max_active_light, max_active_voice
            nonlocal overlap_video_with_other_gpu, overlap_video_with_voice
            prof = ac.classify_workflow(task.workflow_name)
            with lock:
                if prof.tier == ac.TIER_VOICE_CPU:
                    active_voice += 1
                    max_active_voice = max(max_active_voice, active_voice)
                    if video_started.is_set() and not release_video.is_set():
                        overlap_video_with_voice = True
                else:
                    active_gpu += 1
                    if not prof.exclusive:
                        max_active_light = max(max_active_light, active_gpu)
                    if active_gpu > 1 and prof.exclusive:
                        overlap_video_with_other_gpu = True
            try:
                if prof.exclusive:
                    video_started.set()
                    release_video.wait(timeout=3.0)
                else:
                    time.sleep(0.08)
            finally:
                with lock:
                    if prof.tier == ac.TIER_VOICE_CPU:
                        active_voice -= 1
                    else:
                        active_gpu -= 1
                    completed.append(task.id)
                    if len(completed) == 7:
                        all_done.set()

        q = self.webui.TaskQueue(processor, max_done=20, concurrency_controller=controller)
        q.start_workers()
        try:
            # 1) Submit 3 light image tasks -> should expand concurrently up to 3
            for i in range(3):
                q.enqueue(self.webui.Task(f"light-{i}", f"light-{i}", "image_z_image_turbo", {}))
            time.sleep(0.35)
            self.assertGreaterEqual(max_active_light, 2)

            # 2) Submit 1 exclusive heavy video + 3 Zonos3 CPU voice tasks
            q.enqueue(self.webui.Task("video-1", "video-1", "MiniMaxH3-文生视频", {}))
            self.assertTrue(video_started.wait(timeout=2.0))
            for i in range(3):
                q.enqueue(self.webui.Task(f"voice-{i}", f"voice-{i}", self.webui.ZONOS3_WORKFLOW, {}))
            time.sleep(0.25)
            # Voice tasks must execute concurrently WHILE heavy video holds GPU0
            self.assertGreaterEqual(max_active_voice, 2)
            self.assertTrue(overlap_video_with_voice)
            release_video.set()
            self.assertTrue(all_done.wait(timeout=3.0))
            self.assertFalse(overlap_video_with_other_gpu)
        finally:
            q.stop_event.set()

    def test_heavy_video_strictly_mutexes_with_light_image_and_no_starvation(self):
        controller = ac.AdaptiveConcurrencyController(
            probe_fn=lambda: HEALTHY_HW,
            config_provider=lambda: ac.AdaptiveConfig(),
            cache_ttl=0.0,
        )
        q = self.webui.TaskQueue(lambda t: None, max_done=10, concurrency_controller=controller)
        now = time.time()
        light_running = self.webui.Task("l1", "l1", "image_z_image_turbo", {}, submit_ts=now)
        heavy_waiting = self.webui.Task("v1", "v1", "MiniMaxH3-文生视频", {}, submit_ts=now + 1)
        light_behind = self.webui.Task("l2", "l2", "image_z_image_turbo", {}, submit_ts=now + 2)
        with q._lock:
            q._running[light_running.id] = light_running
            q._pending.extend([heavy_waiting, light_behind])
            # Anti-starvation: light_behind must NOT jump ahead of waiting heavy_waiting
            self.assertIsNone(q._pop_next_task_locked())
            # Once light_running completes, heavy_waiting starts exclusively
            q._running.clear()
            popped = q._pop_next_task_locked()
            self.assertIs(popped, heavy_waiting)
            q._running[heavy_waiting.id] = heavy_waiting
            # While heavy_waiting runs, light_behind cannot start
            self.assertIsNone(q._pop_next_task_locked())

    def test_fault_injection_in_queue_throttles_light_gpu_to_1(self):
        degraded_hw = {
            "cpu": {"cpu_count": 64, "load_1m": 0.5, "usage_pct": 5.0},
            "ram": HEALTHY_HW["ram"],
            "gpus": [{"index": 0, "name": "NVIDIA RTX A5000", "util_pct": 20.0, "vram_used_mib": 21500, "vram_total_mib": 24564}],
        }
        controller = ac.AdaptiveConcurrencyController(
            probe_fn=lambda: degraded_hw,
            config_provider=lambda: ac.AdaptiveConfig(),
            cache_ttl=0.0,
        )
        q = self.webui.TaskQueue(lambda t: None, max_done=10, concurrency_controller=controller)
        l1 = self.webui.Task("l1", "l1", "image_z_image_turbo", {})
        l2 = self.webui.Task("l2", "l2", "Flux2Klein-快速图生图", {})
        with q._lock:
            q._pending.extend([l1, l2])
            first = q._pop_next_task_locked()
            self.assertIs(first, l1)
            q._running[l1.id] = l1
            # Low VRAM degrades image_light limit to 1 -> l2 must wait
            self.assertIsNone(q._pop_next_task_locked())

    def test_adaptive_slot_gate_expands_and_degrades_for_lan_api_zonos3(self):
        limit_box = {"val": 4}
        gate = ac.AdaptiveSlotGate(lambda: limit_box["val"], poll_interval=0.02)
        max_active = 0
        cur_active = 0
        lock = threading.Lock()

        def worker():
            nonlocal max_active, cur_active
            self.assertTrue(gate.acquire())
            try:
                with lock:
                    cur_active += 1
                    max_active = max(max_active, cur_active)
                time.sleep(0.08)
            finally:
                with lock:
                    cur_active -= 1
                gate.release()

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=2.0)
        self.assertGreaterEqual(max_active, 2)

        # Degrade to 1 and verify strict serial gating
        limit_box["val"] = 1
        max_active = 0
        threads = [threading.Thread(target=worker) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=2.0)
        self.assertEqual(max_active, 1)

    def test_lan_api_health_exposes_adaptive_concurrency_telemetry(self):
        mock_ok = Mock(status_code=200)
        with patch.object(self.api.requests, "get", return_value=mock_ok), \
             patch.object(self.api.system_telemetry, "collect_cpu_stats", return_value=HEALTHY_HW["cpu"]), \
             patch.object(self.api.system_telemetry, "collect_ram_stats", return_value=HEALTHY_HW["ram"]), \
             patch.object(self.api.system_telemetry, "collect_gpu_stats", return_value=HEALTHY_HW["gpus"]), \
             patch.object(self.api.system_telemetry, "collect_services_status", return_value={"online": 5, "total": 5, "items": []}), \
             patch.object(self.api.system_telemetry, "collect_storage_tiers", return_value=[]), \
             patch.object(self.api.system_telemetry, "detect_active_engine", return_value={"busy": False, "label": "空闲"}):
            self.api.system_telemetry._telemetry_cache.clear()
            resp = self.api.health()
        self.assertEqual(resp.status_code, 200)
        body = resp.content
        self.assertIn("concurrency", body)
        self.assertIn("concurrency", body["telemetry"])
        self.assertEqual(body["concurrency"]["limits"]["image_light"], 3)
        self.assertEqual(body["concurrency"]["limits"]["voice_cpu"], 4)
        self.assertEqual(body["concurrency"]["limits"]["video_heavy"], 1)

    def test_light_lease_shares_lock_across_batch_and_releases_once(self):
        session = Mock()
        session.__enter__ = Mock(return_value=session)
        session.__exit__ = Mock(return_value=False)
        session.get.return_value.json.return_value = {"queue_running": [], "queue_pending": []}
        barrier = threading.Barrier(2)

        def run_light():
            with guard.light_lease("http://127.0.0.1:8188"):
                barrier.wait(timeout=2.0)

        with tempfile.TemporaryDirectory() as tmp, \
             patch.dict(os.environ, {"BRM_MEDIA_GPU_LOCK_PATH": str(Path(tmp) / "media-gpu.lock")}), \
             patch.object(guard.requests, "Session", return_value=session), \
             patch.object(guard, "gpu_used_mib", return_value=512), \
             patch.object(guard, "release_all") as release_all:
            t1 = threading.Thread(target=run_light)
            t2 = threading.Thread(target=run_light)
            t1.start(); t2.start()
            t1.join(timeout=3.0); t2.join(timeout=3.0)
        # /queue probed only by first entrant, release_all called only once when batch ends
        self.assertEqual(session.get.call_count, 1)
        release_all.assert_called_once_with("http://127.0.0.1:8188")


if __name__ == "__main__":
    unittest.main(verbosity=2)
