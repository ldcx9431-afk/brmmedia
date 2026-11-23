#!/usr/bin/env python3
"""Resource and input-contract tests for the SeedVR2 workflow."""

from __future__ import annotations

import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SUPPORT = ROOT / "ubuntu-backend-deploy"
if str(SUPPORT) not in sys.path:
    sys.path.insert(0, str(SUPPORT))

import seedvr2_support as seedvr2


class SeedVR2SupportTests(unittest.TestCase):
    def test_three_b_model_can_be_enabled_without_removing_seven_b(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fast_model, vae = seedvr2.model_paths(temporary, "3b")
            fast_model.parent.mkdir(parents=True)
            vae.parent.mkdir(parents=True)
            fast_model.touch()
            vae.touch()
            self.assertTrue(seedvr2.models_installed(temporary))
            self.assertTrue(seedvr2.model_installed(temporary, "3b"))
            self.assertFalse(seedvr2.model_installed(temporary, "7b"))
            self.assertEqual(seedvr2.installed_model_variants(temporary), ["3b"])

    def test_pinned_model_layout_and_installed_check(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            model, vae = seedvr2.model_paths(temporary)
            self.assertEqual(model.name, "seedvr2_7b_int8_convrot.safetensors")
            fast_model, fast_vae = seedvr2.model_paths(temporary, "3b")
            self.assertEqual(fast_model.name, "seedvr2_3b_int8_convrot.safetensors")
            self.assertEqual(fast_vae, vae)
            self.assertEqual(vae.name, "ema_vae_fp16.safetensors")
            self.assertFalse(seedvr2.models_installed(temporary))
            model.parent.mkdir(parents=True)
            vae.parent.mkdir(parents=True)
            model.touch()
            vae.touch()
            self.assertTrue(seedvr2.models_installed(temporary))
            self.assertTrue(seedvr2.model_installed(temporary, "7b"))
            self.assertFalse(seedvr2.model_installed(temporary, "3b"))
            fast_model.touch()
            self.assertTrue(seedvr2.model_installed(temporary, "3b"))
            self.assertEqual(seedvr2.installed_model_variants(temporary), ["7b", "3b"])
            self.assertEqual(
                seedvr2.seedvr2_model_filename("3b"), "seedvr2_3b_int8_convrot.safetensors",
            )
            with self.assertRaisesRegex(ValueError, "仅支持 7b 或 3b"):
                seedvr2.model_paths(temporary, "1b")

    def test_scale_and_output_resource_guard(self) -> None:
        self.assertEqual(seedvr2.output_dimensions(1280, 720, 2), (2560, 1440))
        with self.assertRaisesRegex(ValueError, "安全限制"):
            seedvr2.output_dimensions(1920, 1080, 2)
        with self.assertRaisesRegex(ValueError, "仅支持"):
            seedvr2.output_dimensions(640, 480, 4)

    def test_video_resolution_presets_preserve_aspect_and_keep_four_k_locked(self) -> None:
        self.assertEqual(seedvr2.video_output_dimensions(1280, 720, "1080p", 2), (1920, 1080, 1.5))
        self.assertEqual(seedvr2.video_output_dimensions(720, 1280, "1080p", 1)[:2], (1080, 1920))
        self.assertEqual(seedvr2.video_output_dimensions(1920, 1080, "720p", 2)[:2], (1280, 720))
        with self.assertRaisesRegex(ValueError, "4K UHD 预设暂未开放"):
            seedvr2.video_output_dimensions(1920, 1080, "4k", 2)
        with self.assertRaisesRegex(ValueError, "安全限制"):
            seedvr2.video_output_dimensions(3000, 1000, "1080p", 2)

    def test_supported_video_extensions_are_case_insensitive(self) -> None:
        for name in ("clip.mp4", "clip.MP4", "clip.MoV", "clip.webm", "clip.AVI"):
            with self.subTest(name=name):
                self.assertTrue(seedvr2.is_supported_video_filename(name))
        self.assertFalse(seedvr2.is_supported_video_filename("clip.m4v"))

    def test_video_probe_and_variable_frame_rate_rejection(self) -> None:
        output = {
            "format": {"duration": "12.0"},
            "streams": [{
                "codec_type": "video", "width": 1280, "height": 720,
                "avg_frame_rate": "24/1", "r_frame_rate": "24/1", "nb_frames": "288",
            }, {"codec_type": "audio"}],
        }
        completed = types.SimpleNamespace(returncode=0, stdout=json.dumps(output), stderr="")
        with patch.object(seedvr2.shutil, "which", return_value="ffprobe"), patch.object(
            seedvr2.subprocess, "run", return_value=completed,
        ):
            info = seedvr2.probe_video("fixture.mp4")
        seedvr2.validate_video_info(info)
        self.assertTrue(info["has_audio"])
        self.assertEqual(info["frame_count"], 288)
        variable = {**info, "fps": 23.5, "nominal_fps": 24.0}
        with self.assertRaisesRegex(ValueError, "可变帧率"):
            seedvr2.validate_video_info(variable)

    def test_duration_and_disk_budget_limits(self) -> None:
        with self.assertRaisesRegex(ValueError, "不超过 12 小时"):
            seedvr2.validate_video_info({
                "duration": seedvr2.TASK_MAX_SECONDS + 1,
                "fps": 24, "nominal_fps": 24,
            })
        self.assertGreaterEqual(seedvr2.estimated_video_workspace_bytes(1, 1), 6 * 1024**3)
        self.assertGreater(
            seedvr2.estimated_video_workspace_bytes(1024**3, 2),
            seedvr2.estimated_video_workspace_bytes(1024**3, 1),
        )
        self.assertEqual(seedvr2.segment_duration_seconds(24), 241 / 24)
        self.assertEqual(seedvr2.segment_duration_seconds(120), 241 / 120)


if __name__ == "__main__":
    unittest.main(verbosity=2)
