#!/usr/bin/env python3
"""Static safeguards for the A5000-media/A4000-Qwen recovery profile."""

from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


class DeploymentProfileTests(unittest.TestCase):
    def test_bootstrap_uses_the_supported_qwen_profile(self):
        text = (REPO_ROOT / "bootstrap_wsl.sh").read_text(encoding="utf-8")
        self.assertIn('cp "$QWEN_DIR/.env.qwen35-4b.example" "$QWEN_DIR/.env"', text)
        self.assertIn("Qwen3.5-4B-AWQ-4bit", text)
        self.assertNotIn('cp "$QWEN_DIR/.env.example" "$QWEN_DIR/.env"', text)

    def test_generic_qwen_example_preserves_gpu_split(self):
        text = (REPO_ROOT / "llm-backend-deploy" / ".env.example").read_text(encoding="utf-8")
        self.assertIn("QWEN_CUDA_VISIBLE_DEVICES=1", text)
        self.assertIn("QWEN_MODEL=cyankiwi/Qwen3.5-4B-AWQ-4bit", text)
        self.assertIn("QWEN_GPU_MEMORY_UTILIZATION=0.70", text)
        self.assertNotIn("QWEN_CUDA_VISIBLE_DEVICES=0", text)

    def test_runtime_verifier_uses_installed_release_record(self):
        verifier = (REPO_ROOT / "brmmedia-verify-runtime.sh").read_text(encoding="utf-8")
        installer = (REPO_ROOT / "install_ubuntu_systemd_services.sh").read_text(encoding="utf-8")
        self.assertIn("/etc/brmmedia/runtime.env", verifier)
        self.assertIn("runtime_env_value BRMMEDIA_APP_ROOT", verifier)
        self.assertIn("BRMMEDIA_SOURCE_ROOT", installer)
        self.assertIn("/etc/brmmedia/runtime.env", installer)
        self.assertIn("BRMMEDIA_NVIDIA_SMI", verifier)
        self.assertIn("/usr/lib/wsl/lib/nvidia-smi", verifier)
        self.assertIn('runtime-locks/h3-comfyui-v0.32.0.env', verifier)
        self.assertIn('backend_env_value COMFYUI_ROOT', verifier)
        self.assertIn('gpu_inventory="$("$nvidia_smi" --query-gpu=', verifier)

    def test_h3_activation_uses_candidate_env_and_restores_on_start_failure(self):
        activation = (REPO_ROOT / "activate_minimax_h3.sh").read_text(encoding="utf-8")
        preparation = (REPO_ROOT / "prepare_minimax_h3_comfyui.sh").read_text(encoding="utf-8")
        self.assertIn("dotenv_value COMFYUI_ROOT", activation)
        self.assertIn("restore_after_failed_activation", activation)
        self.assertIn("trap restore_after_failed_activation ERR", activation)
        self.assertIn("Stopping partially started H3 backend before rollback", activation)
        self.assertIn("systemctl stop baorong-backend", activation)
        self.assertIn("rollback_minimax_h3_comfyui.sh", activation)
        self.assertIn("BRMMEDIA_H3_STARTUP_HEALTH_TIMEOUT_SECONDS", activation)
        self.assertIn("/gradio_api/info", activation)
        self.assertIn("/system_stats", activation)
        self.assertIn("systemctl restart qwen-vllm", activation)
        self.assertIn("/v1/models", activation)
        self.assertIn("verify_h3_workflow_gate.sh", activation)
        self.assertIn("compatibility before activation completes", activation)
        self.assertIn('H3_COMFY_REF="${BRMMEDIA_H3_COMFYUI_REF:-563b98eefbe643a4cd510ee7f0b43e79880d5a3f}"', preparation)
        self.assertNotIn("15989f87ca89bfe2e7c47763252c559e96d97551", preparation)

    def test_h3_preflight_finds_the_wsl_nvidia_shim_for_systemd(self):
        preflight = (REPO_ROOT / "check_h3_preflight.sh").read_text(encoding="utf-8")
        self.assertIn("BRMMEDIA_NVIDIA_SMI", preflight)
        self.assertIn("/usr/lib/wsl/lib/nvidia-smi", preflight)
        self.assertIn('"$nvidia_smi" --query-gpu=index,name,memory.total,uuid', preflight)

    def test_h3_activation_uses_fast_size_gate_after_full_acceptance(self):
        activation = (REPO_ROOT / "activate_minimax_h3.sh").read_text(encoding="utf-8")
        verifier = (REPO_ROOT / "verify_comfy_models.sh").read_text(encoding="utf-8")
        self.assertIn("BRMMEDIA_VERIFY_MODEL_CONTENT=0", activation)
        self.assertIn('verify_content="${BRMMEDIA_VERIFY_MODEL_CONTENT:-1}"', verifier)
        self.assertIn("rsync -rn --size-only", verifier)

    def test_h3_workflow_gate_is_required_for_canary_and_production_cutover(self):
        gate = (REPO_ROOT / "verify_h3_workflow_gate.sh").read_text(encoding="utf-8")
        activation = (REPO_ROOT / "activate_minimax_h3.sh").read_text(encoding="utf-8")
        canary = (REPO_ROOT / "start_minimax_h3_canary.sh").read_text(encoding="utf-8")
        self.assertIn("validate_comfy_workflows.py", gate)
        self.assertIn("MiniMaxH3-文生视频.json", gate)
        self.assertIn("MiniMaxH3-图生视频.json", gate)
        self.assertIn("verify_h3_workflow_gate.sh", activation)
        self.assertIn("trap restore_after_failed_activation ERR", activation)
        self.assertIn("verify_h3_workflow_gate.sh", canary)
        self.assertIn("stopping isolated canary units", canary)
        self.assertIn('systemctl stop "$API_UNIT" "$BACKEND_UNIT"', canary)

    def test_qwen_acceptance_is_loopback_and_records_non_secret_evidence(self):
        acceptance = (REPO_ROOT / "accept_qwen_vllm.sh").read_text(encoding="utf-8")
        self.assertIn("http://127.0.0.1:8000/v1", acceptance)
        self.assertIn("/chat/completions", acceptance)
        self.assertIn('"enable_thinking": False', acceptance)
        self.assertIn('QWEN_RESPONSE="$response"', acceptance)
        self.assertIn('"runs": int(runs)', acceptance)
        self.assertNotIn("BRM_PASSWORD", acceptance)

    def test_h3_acceptance_can_verify_task_recovery_across_restart(self):
        acceptance = (REPO_ROOT / "accept_minimax_h3_video.sh").read_text(encoding="utf-8")
        self.assertIn("--restart-recovery", acceptance)
        self.assertIn("--quality-i2v-only", acceptance)
        self.assertIn("quality I2V recovery acceptance", acceptance)
        self.assertIn("verify_completed_task_after_restart", acceptance)
        self.assertIn("systemctl restart", acceptance)
        self.assertIn('"$API_BASE/tasks/$task_id"', acceptance)
        self.assertIn("persisted H3 task recovery", acceptance)

    def test_retained_media_regression_covers_smoke_and_rest_paths(self):
        acceptance = (REPO_ROOT / "accept_media_regression.sh").read_text(encoding="utf-8")
        self.assertIn('run_smoke_z_image.sh', acceptance)
        self.assertIn('run_smoke_music.sh', acceptance)
        self.assertIn('run_smoke_tts.sh', acceptance)
        self.assertIn('submit_task image-edit', acceptance)
        self.assertIn('submit_task first-last-frame-video', acceptance)
        self.assertIn('submit_task talking-head', acceptance)
        self.assertIn('ffprobe', acceptance)
        self.assertIn('http://127.0.0.1:9100/api/v1', acceptance)

    def test_tts_smoke_uses_the_latest_music_smoke_artifact(self):
        smoke_tts = (REPO_ROOT / "smoke_tts.py").read_text(encoding="utf-8")
        self.assertIn('glob("smoke_music_*")', smoke_tts)
        self.assertIn("key=lambda path: path.stat().st_mtime", smoke_tts)
        self.assertNotIn("smoke_music_ACE-Step_turbo_00001.mp3", smoke_tts)

    def test_h3_candidate_refresh_rejects_active_service_paths(self):
        refresh = (REPO_ROOT / "refresh_h3_runtime_release.sh").read_text(encoding="utf-8")
        self.assertIn('/srv/brmmedia/releases/h3-*', refresh)
        self.assertIn('do not refresh an active service path', refresh)
        self.assertIn("--exclude 'ubuntu-backend-deploy/.env'", refresh)
        self.assertIn("--exclude 'ubuntu-backend-deploy/outputs'", refresh)
        self.assertIn('install -m 0644 "$SOURCE_RELEASE/runtime-locks/$lock_manifest"', refresh)
        self.assertIn('h3-comfyui-v0.32.0.env', refresh)

    def test_systemd_installer_uses_the_release_backend_venv_for_rest(self):
        installer = (REPO_ROOT / "install_ubuntu_systemd_services.sh").read_text(encoding="utf-8")
        self.assertIn("BRMMEDIA_BACKEND_VENV", installer)
        self.assertIn('$BACKEND_VENV/bin/python -m uvicorn lan_api:app', installer)

    def test_h3_worker_inherits_the_stage_chunk_size(self):
        downloader = (REPO_ROOT / "start_minimax_h3_download.sh").read_text(encoding="utf-8")
        self.assertIn('--setenv="BRMMEDIA_H3_CHUNK_BYTES=$CHUNK_BYTES"', downloader)
        self.assertIn("BRMMEDIA_H3_MIN_CHUNK_BYTES", downloader)
        self.assertIn("BRMMEDIA_H3_CURL_RETRIES", downloader)
        self.assertIn('"$CURL_RETRIES"', downloader)
        self.assertIn("BRMMEDIA_H3_PARALLEL_RANGES", downloader)
        self.assertIn('"$PARALLEL_RANGES"', downloader)
        self.assertIn('--setenv="BRMMEDIA_H3_REPO=$REPO"', downloader)
        self.assertIn('--setenv="BRMMEDIA_H3_REVISION=$REVISION"', downloader)
        self.assertIn("strictly in offset order", downloader)
        self.assertIn("will retry with", downloader)
        self.assertIn('"$(basename \"$file\").chunk.*" -delete', downloader)
        self.assertIn("cleanup_chunks", downloader)
        self.assertIn("trap cleanup_chunks EXIT", downloader)
        self.assertIn("for proxy_env_name in HTTPS_PROXY HTTP_PROXY ALL_PROXY NO_PROXY", downloader)
        self.assertIn('"${proxy_env_args[@]}"', downloader)

    def test_windows_h3_resumer_validates_ranges_before_append(self):
        resumer = (REPO_ROOT / "windows_h3_range_download.ps1").read_text(encoding="utf-8")
        self.assertIn("RangeHeaderValue", resumer)
        self.assertIn("PartialContent", resumer)
        self.assertIn("ContentRange", resumer)
        self.assertIn("FileMode]::Append", resumer)
        self.assertIn("temporaryPath", resumer)
        self.assertIn("received -ne $length", resumer)

    def test_parallel_h3_worker_appends_verified_ranges_and_exits_cleanly(self):
        """Exercise the Bash EXIT trap with a two-range, checksum-valid batch."""
        bash_major = int(
            subprocess.check_output(
                ["bash", "-c", "printf '%s' \"${BASH_VERSINFO[0]}\""], text=True
            )
        )
        if bash_major < 4:
            self.skipTest("the deployment downloader requires Bash 4 associative arrays")
        source = (REPO_ROOT / "start_minimax_h3_download.sh").read_text(encoding="utf-8")
        payload = b"abcdefgh"
        fixture = source.replace(
            "diffusion) echo 20970379616 ;;", "diffusion) echo 8 ;;"
        ).replace(
            "diffusion) echo e889202c41dafb67b10d67b97f0d8541508036a6090af23425a5c2615d03c47a ;;",
            f"diffusion) echo {hashlib.sha256(payload).hexdigest()} ;;",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            downloader = temp / "downloader.sh"
            downloader.write_text(fixture, encoding="utf-8")
            downloader.chmod(0o755)
            fake_bin = temp / "bin"
            fake_bin.mkdir()
            (fake_bin / "curl").write_text(
                textwrap.dedent(
                    """\
                    #!/usr/bin/env python3
                    import pathlib
                    import sys

                    args = sys.argv[1:]
                    def value(flag):
                        return args[args.index(flag) + 1]
                    start, end = map(int, value("--range").split("-"))
                    payload = b"abcdefgh"[start:end + 1]
                    pathlib.Path(value("--output")).write_bytes(payload)
                    pathlib.Path(value("--dump-header")).write_text(
                        f"HTTP/1.1 206 Partial Content\\r\\nContent-Range: bytes {start}-{end}/8\\r\\n\\r\\n"
                    )
                    """
                ),
                encoding="utf-8",
            )
            (fake_bin / "stat").write_text(
                "#!/usr/bin/env bash\nif [ \"$1\" = \"-c%s\" ]; then wc -c < \"$2\" | tr -d ' '; else /usr/bin/stat \"$@\"; fi\n",
                encoding="utf-8",
            )
            (fake_bin / "sha256sum").write_text(
                "#!/usr/bin/env bash\nshasum -a 256 \"$1\"\n", encoding="utf-8"
            )
            (fake_bin / "dd").write_text(
                "#!/usr/bin/env bash\nfor arg in \"$@\"; do case \"$arg\" in if=*) input=${arg#if=} ;; of=*) output=${arg#of=} ;; esac; done\ncat \"$input\" >> \"$output\"\n",
                encoding="utf-8",
            )
            for command in fake_bin.iterdir():
                command.chmod(0o755)
            env = os.environ | {
                "PATH": f"{fake_bin}:{os.environ['PATH']}",
                "BRMMEDIA_MODEL_SOURCE_ROOT": str(temp / "models"),
                "BRMMEDIA_H3_CHUNK_BYTES": "4",
                "BRMMEDIA_H3_MIN_CHUNK_BYTES": "4",
                "BRMMEDIA_H3_PARALLEL_RANGES": "2",
            }
            result = subprocess.run(
                ["bash", str(downloader), "--worker", "diffusion"],
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            target = temp / "models" / "MiniMax-H3" / "diffusion_models" / "minimax_h3_fl2va_pruned_int8_convrot.safetensors"
            self.assertEqual(target.read_bytes(), payload)
            self.assertEqual(list(target.parent.glob("*.chunk.*")), [])
    def test_h3_canary_stays_off_the_production_lan_ports(self):
        prepare = (REPO_ROOT / "prepare_minimax_h3_canary.sh").read_text(encoding="utf-8")
        starter = (REPO_ROOT / "start_minimax_h3_canary.sh").read_text(encoding="utf-8")
        guide = (REPO_ROOT / "WSL_TEST_DEPLOY.md").read_text(encoding="utf-8")
        self.assertIn("ComfyUI-h3-v032-canary", prepare)
        self.assertIn("BRM_GRADIO_PORT 9001", prepare)
        self.assertIn("BRMMEDIA_VIDEO_ENGINE h3", prepare)
        self.assertIn("remote set-url origin", prepare)
        self.assertIn("baorong-backend-h3-canary", starter)
        self.assertIn("--port 9101", starter)
        self.assertIn("Nginx is never repointed to the canary", guide)

    def test_h3_canary_pauses_production_healthcheck_until_stop(self):
        starter = (REPO_ROOT / "start_minimax_h3_canary.sh").read_text(encoding="utf-8")
        stopper = (REPO_ROOT / "stop_minimax_h3_canary.sh").read_text(encoding="utf-8")
        self.assertIn('HEALTH_TIMER="${BRMMEDIA_HEALTHCHECK_TIMER:-brmmedia-healthcheck.timer}"', starter)
        self.assertIn('systemctl stop "$HEALTH_TIMER"', starter)
        self.assertIn('h3-canary-healthcheck-timer-state', starter)
        self.assertIn('systemctl start "$HEALTH_TIMER"', stopper)
        self.assertLess(
            starter.index('systemctl stop "$HEALTH_TIMER"'),
            starter.index('if systemctl is-active --quiet baorong-backend;'),
        )

    def test_h3_canary_uses_a_physical_python_venv(self):
        prepare = (REPO_ROOT / "prepare_minimax_h3_canary.sh").read_text(encoding="utf-8")
        starter = (REPO_ROOT / "start_minimax_h3_canary.sh").read_text(encoding="utf-8")
        backend = (REPO_ROOT / "ubuntu-backend-deploy" / "start_backend.sh").read_text(encoding="utf-8")
        self.assertIn("BRMMEDIA_H3_CANARY_VENV", prepare)
        self.assertIn("cp -a --reflink=auto", prepare)
        self.assertIn('set_dotenv_value BRMMEDIA_BACKEND_VENV "$CANARY_VENV"', prepare)
        self.assertIn('set_dotenv_value COMFYUI_PYTHON "$CANARY_PYTHON"', prepare)
        self.assertIn('--setenv="BRMMEDIA_BACKEND_VENV=$CANARY_VENV"', starter)
        self.assertIn('"$CANARY_PYTHON" -m uvicorn', starter)
        self.assertIn('BRMMEDIA_H3_GATE_PYTHON="$CANARY_PYTHON"', starter)
        self.assertIn('BACKEND_VENV="${BRMMEDIA_BACKEND_VENV:-$SCRIPT_DIR/.venv}"', backend)
        self.assertIn('source "$BACKEND_VENV/bin/activate"', backend)

    def test_h3_v032_canary_has_immutable_runtime_lock_and_ab_gate(self):
        lock = (REPO_ROOT / "runtime-locks" / "h3-comfyui-v0.32.0.env").read_text(encoding="utf-8")
        prepare = (REPO_ROOT / "prepare_minimax_h3_canary.sh").read_text(encoding="utf-8")
        starter = (REPO_ROOT / "start_minimax_h3_canary.sh").read_text(encoding="utf-8")
        verifier = (REPO_ROOT / "verify_h3_canary_runtime.sh").read_text(encoding="utf-8")
        matrix = (REPO_ROOT / "configure_h3_canary_ab.sh").read_text(encoding="utf-8")

        self.assertIn("BRMMEDIA_H3_COMFYUI_VERSION=0.32.0", lock)
        self.assertIn("BRMMEDIA_H3_COMFYUI_REF=c2bcbecd82ec5ae66594340b395c24ef0217b238", lock)
        self.assertIn("BRMMEDIA_H3_COMFY_KITCHEN_VERSION=0.2.30", lock)
        self.assertIn("h3-comfyui-v0.32.0.env", prepare)
        self.assertIn('BRMMEDIA_H3_COMFYUI_REF="$BRMMEDIA_H3_COMFYUI_REF"', prepare)
        self.assertIn("verify_h3_canary_runtime.sh", starter)
        self.assertLess(starter.index("verify_h3_canary_runtime.sh"), starter.index("systemd-run --unit=\"$BACKEND_UNIT\""))
        self.assertIn("torch.version.cuda.startswith(\"13.\")", verifier)
        self.assertIn("torch.cuda.get_device_capability(0) == (8, 6)", verifier)
        self.assertIn("--use-ck-attention", matrix)
        self.assertIn("--fast-disk", matrix)
        self.assertIn("--cache-lru 1", matrix)

    def test_h3_attention_and_offload_guards_are_fail_closed(self):
        matrix = (REPO_ROOT / "configure_h3_canary_ab.sh").read_text(encoding="utf-8")
        verifier = (REPO_ROOT / "verify_h3_canary_runtime.sh").read_text(encoding="utf-8")
        start_backend = (REPO_ROOT / "ubuntu-backend-deploy" / "start_backend.sh").read_text(encoding="utf-8")
        bridge = (REPO_ROOT / "ubuntu-backend-deploy" / "comfyui_server.py").read_text(encoding="utf-8")

        for forbidden in (
            "--highvram", "--gpu-only", "--lowvram", "--novram",
            "--disable-smart-memory", "--cache-none", "--disable-dynamic-vram",
            "--disable-async-offload",
        ):
            self.assertIn(forbidden, matrix)
            self.assertIn(forbidden, verifier)
            self.assertIn(forbidden, start_backend)
            self.assertIn(forbidden, bridge)
        self.assertIn("Kitchen Attention cannot be combined", matrix)
        self.assertIn("Kitchen Attention cannot start", verifier)
        self.assertIn("Kitchen and Sage must be benchmarked", start_backend)

    def test_h3_turbo_delivery_is_checksum_gated_and_benchmarkable(self):
        downloader = (REPO_ROOT / "download_minimax_h3_turbo_models.sh").read_text(encoding="utf-8")
        importer = (REPO_ROOT / "import_minimax_h3_turbo_models.sh").read_text(encoding="utf-8")
        prepare = (REPO_ROOT / "prepare_minimax_h3_canary.sh").read_text(encoding="utf-8")
        verifier = (REPO_ROOT / "verify_h3_canary_runtime.sh").read_text(encoding="utf-8")
        benchmark = (REPO_ROOT / "benchmark_h3_profiles.sh").read_text(encoding="utf-8")

        self.assertIn("5d1d4829fe614c1b93fcfd9cc7718e9ba71f73e1", downloader)
        for digest in (
            "2339acdf19bfe123f46b971ea35d367a84adb85de43627e1eceafa5a5b2b111e",
            "c396a9a06f58399e9df9754b18299818d84a2ddd371724ba48fe4a41221437dc",
        ):
            self.assertIn(digest, downloader)
            self.assertIn(digest, prepare)
            self.assertIn(digest, verifier)
        self.assertIn("sha256sum", importer)
        self.assertIn("BRMMEDIA_H3_BENCHMARK_ACCELERATION", benchmark)
        self.assertIn("turbo_balanced", benchmark)
        self.assertIn("turbo_fast", benchmark)

    def test_current_ubuntu_guide_does_not_recommend_highvram(self):
        guide = (REPO_ROOT / "ubuntu-backend-deploy" / "README_UBUNTU_DEPLOY.md").read_text(encoding="utf-8")
        self.assertIn("禁止 `--highvram`、`--gpu-only`", guide)
        self.assertNotIn("echo 'COMFYUI_ARGS=--highvram'", guide)

    def test_h3_deployment_guide_matches_range_downloader(self):
        guide = (REPO_ROOT / "WSL_TEST_DEPLOY.md").read_text(encoding="utf-8")
        self.assertIn("`curl`", guide)
        self.assertIn("HTTP Range", guide)
        self.assertNotIn("下载器默认使用 HF CLI", guide)
