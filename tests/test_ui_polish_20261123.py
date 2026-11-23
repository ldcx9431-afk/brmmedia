"""UI polish / global settings regression tests (no Gradio, no GPU)."""
import ast
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1] / "ubuntu-backend-deploy"
sys.path.insert(0, str(ROOT))
SOURCE = ROOT / "webui.py"
SRC = SOURCE.read_text(encoding="utf-8")

import media_runtime_guard  # noqa: E402
import system_telemetry  # noqa: E402


def _css() -> str:
    start = SRC.index('CUSTOM_CSS = """')
    return SRC[start:SRC.index('\n"""', start)]


class CssRegressionTests(unittest.TestCase):
    def test_brand_home_not_styled_as_white_toolbar_button(self):
        css = _css()
        self.assertIn("#global-toolbar button:not(.brm-brand-home)", css)
        self.assertNotIn("#global-toolbar button {", css)
        block = css[css.rindex("#brand-lockup .brm-brand-home {"):]
        block = block[:block.index("}")]
        for rule in ("background: transparent !important", "border: 0 !important",
                     "border-radius: 0 !important", "box-shadow: none !important",
                     "height: auto !important", "padding: 4px 0 !important"):
            self.assertIn(rule, block)

    def test_labels_forced_dark_including_dark_mode(self):
        css = _css()
        self.assertGreaterEqual(css.count("color: #523b37 !important;") + css.count("color:#523b37 !important;"), 2)
        self.assertIn('.gradio-container span[data-testid="block-info"]', css)
        self.assertIn(".gradio-container .block-info", css)

    def test_audio_dock_wide_and_short_close_label(self):
        css = _css()
        self.assertIn("min(880px,calc(100vw - 32px))", css)
        self.assertIn("min-width:280px", css)
        self.assertIn("flex:1 1 300px", css)
        self.assertIn(">关闭试听</button>", SRC)
        self.assertNotIn("停止并移除当前试听</button>", SRC)

    def test_secondary_buttons_have_warm_surface(self):
        self.assertIn("button.secondary {", _css())

    def test_primary_buttons(self):
        for needle in (
            'change_lan_password_btn = gr.Button("修改 brmadmin 主账号密码", variant="primary")',
            'add_lan_user_btn = gr.Button("新增用户", variant="primary")',
            'gr.Button("查询任务", elem_id="brm-history-search", variant="primary")',
            'gr.Button("查询素材", elem_id="brm-catalog-search", variant="primary")',
        ):
            self.assertIn(needle, SRC)


class SettingsPanelTests(unittest.TestCase):
    def test_static_concurrency_slider_removed_but_hot_settings_present(self):
        self.assertNotIn("setting_concurrency", SRC)
        for name in ("setting_adaptive", "setting_light_gpu", "setting_audio_gpu",
                     "setting_voice_cpu", "setting_voice_breeze", "setting_keep_vram",
                     "ops_free_btn", "ops_telemetry_btn", "ops_thumb_btn"):
            self.assertIn(name, SRC)
        for label in ("释放 ComfyUI 显存 (/free)", "刷新系统遥测", "清理缩略图缓存"):
            self.assertIn(label, SRC)

    def test_menu_and_music_labels(self):
        self.assertIn("音频 · 语音克隆 (Zonos3 / Breeze / IndexTTS)", SRC)
        self.assertIn("YuE2-3B", SRC)
        self.assertNotIn("原 Qwen 27B 已正式退役", SRC)
        self.assertIn('if os.environ.get("BRM_FLUX2_FAST_ENABLED") == "1" else ""', SRC)

    def _load(self, names, env):
        tree = ast.parse(SRC)
        body = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
        exec(compile(ast.Module(body=body, type_ignores=[]), str(SOURCE), "exec"), env)
        return env

    def test_save_global_settings_hot_reloads_everything(self):
        saved = {}
        controller = SimpleNamespace(config=lambda: live_cfg(), worker_pool_size=lambda: 5)

        def live_cfg():
            return SimpleNamespace(
                enabled=saved.get("adaptive_concurrency", True),
                light_gpu_max=saved.get("adaptive_light_gpu_max", 3),
                audio_gpu_max=saved.get("adaptive_audio_gpu_max", 2),
                voice_cpu_max=saved.get("adaptive_voice_cpu_max", 4),
                voice_breeze_max=saved.get("adaptive_voice_breeze_max", 1),
            )

        queue = Mock()
        queue.set_max_done.side_effect = lambda n: n
        queue.set_max_workers.side_effect = lambda n: (n, n)
        guard = SimpleNamespace(
            get_safe_keep_vram_mib=lambda: 22528, KEEP_VRAM_MIN_MIB=12288, KEEP_VRAM_MAX_MIB=23552,
            set_safe_keep_vram_mib=Mock(),
        )
        env = {
            "QUEUE_CONCURRENCY": 1, "DONE_TASKS_MAX": 200, "DONE_GALLERY_MAX": 30,
            "MAX_MEDIA_QUEUE_CONCURRENCY": 1, "VOICE_PARALLEL_ENABLED": True,
            "yzy_config": {}, "save_config": lambda u: saved.update(u),
            "concurrency_controller": controller, "task_queue": queue, "media_runtime_guard": guard,
        }
        self._load({"_setting_int", "_apply_adaptive_settings", "save_global_settings"}, env)
        self.assertIn("未变更", env["save_global_settings"]())
        msg = env["save_global_settings"](None, 100, 40, True, 9, 0, 6, 3, 99999)
        self.assertIn("✅", msg)
        self.assertEqual(saved["adaptive_light_gpu_max"], 4)
        self.assertEqual(saved["adaptive_audio_gpu_max"], 1)
        self.assertEqual(saved["adaptive_voice_cpu_max"], 6)
        self.assertEqual(saved["adaptive_voice_breeze_max"], 3)
        self.assertEqual(saved["keep_model_max_vram_mib"], 23552)
        self.assertEqual(saved["queue_concurrency"], 1)
        guard.set_safe_keep_vram_mib.assert_called_once_with(23552)
        queue.set_max_workers.assert_called_with(5)

    def test_nested_adaptive_block_is_kept_in_sync(self):
        env = {"yzy_config": {"adaptive_concurrency_settings": {"adaptive_light_gpu_max": 1, "x": 7}}}
        self._load({"_apply_adaptive_settings"}, env)
        updates = {"adaptive_light_gpu_max": 3}
        env["_apply_adaptive_settings"](updates)
        self.assertEqual(updates["adaptive_concurrency_settings"], {"adaptive_light_gpu_max": 3, "x": 7})

    def test_thumbnail_cache_cleanup_stays_inside_cache_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "thumbnails"
            root.mkdir()
            (root / "a.webp").write_bytes(b"x" * 10)
            outside = Path(tmp) / "keep.txt"
            outside.write_text("keep")
            import threading
            env = {"THUMB_CACHE_DIR": root, "_thumb_lock": threading.Lock(), "_thumb_pending": {"k"}}
            self._load({"ops_clear_thumbnail_cache"}, env)
            self.assertIn("1 个文件", env["ops_clear_thumbnail_cache"]())
            self.assertFalse((root / "a.webp").exists())
            self.assertTrue(outside.exists())
            self.assertEqual(env["_thumb_pending"], set())


class GuardTests(unittest.TestCase):
    def test_keep_vram_hot_update_and_clamp(self):
        original = media_runtime_guard.SAFE_KEEP_VRAM_MIB
        try:
            self.assertEqual(media_runtime_guard.set_safe_keep_vram_mib(16384), 16384)
            self.assertEqual(media_runtime_guard.get_safe_keep_vram_mib(), 16384)
            self.assertEqual(media_runtime_guard.set_safe_keep_vram_mib(1), 12288)
            self.assertEqual(media_runtime_guard.set_safe_keep_vram_mib(10**6), 23552)
            self.assertEqual(media_runtime_guard.set_safe_keep_vram_mib("bad"), 22528)
        finally:
            media_runtime_guard.SAFE_KEEP_VRAM_MIB = original


class TelemetryRenderTests(unittest.TestCase):
    def test_conc_pills_ports_with_names_and_gpu_layout(self):
        items = [
            {"key": k, "label": k, "port": p, "online": True}
            for k, p in (("strata", 8002), ("comfyui", 8188), ("zonos3", 1919), ("lan_api", 9100),
                         ("indextts25", 9205), ("breeze_tts2", 9206), ("qwen_gateway", 9300))
        ]
        html = system_telemetry.render_system_status_html({
            "cpu": {}, "ram": {},
            "gpus": [{"display_name": "GPU0 · RTX A5000 (媒体主力)", "util_pct": 3, "vram_used_mib": 1, "vram_total_mib": 2, "vram_pct": 50}],
            "services": {"online": 7, "total": 7, "items": items},
            "concurrency": {"limits": {"image_light": 3, "audio_gpu": 2, "voice_cpu": 4, "voice_breeze": 1}},
        })
        for text in (":8002 Strata", ":8188 Comfy", ":1919 Zonos3", ":9100 API", ":9205 IndexTTS",
                     ":9206 Breeze", ":9300 网关", "GPU0 · RTX A5000 (媒体主力)"):
            self.assertIn(text, html)
        for pill in ("重型 1", "轻图 3", "音频GPU 2", "Zonos3 4", "Breeze 1"):
            self.assertIn(f'<span class="brm-sys-cpill">{pill}</span>', html)


if __name__ == "__main__":
    unittest.main()
