# -*- coding: utf-8 -*-
"""
Linux/Ubuntu ComfyUI bridge for the Bao Rong Wan Xiang backend.

This module keeps the same public API used by webui.py, but removes the
Windows portable Python assumptions. It starts a standard ComfyUI checkout
with the current Python interpreter by default.
"""

from __future__ import annotations

import atexit
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid
from pathlib import Path

import requests


BASE_DIR = Path(__file__).resolve().parent

# Ubuntu deployment layout:
#   ubuntu-backend-deploy/
#     comfyui_server.py
#     webui.py
#     workflows/
#   ComfyUI is external and selected with COMFYUI_ROOT.
COMFY_ROOT = Path(os.environ.get("COMFYUI_ROOT", BASE_DIR / "ComfyUI")).expanduser().resolve()
# Do not resolve this path: a virtualenv interpreter is normally a symlink to
# the system Python.  Resolving it would silently replace the venv executable
# with /usr/bin/python and make ComfyUI miss its installed dependencies.
PYTHON_EXE = Path(os.environ.get("COMFYUI_PYTHON", sys.executable)).expanduser()
MAIN_PY = COMFY_ROOT / "main.py"
# The H3 canary uses a private workflow snapshot so an experimental custom
# node never changes the LAN production workflows before acceptance.  Normal
# deployments intentionally retain the checkout-local workflows directory.
WORKFLOW_DIR = Path(os.environ.get("BRMMEDIA_WORKFLOW_DIR", BASE_DIR / "workflows")).expanduser().resolve()

COMFY_HOST = os.environ.get("COMFYUI_HOST", "127.0.0.1")
COMFY_PORT = int(os.environ.get("COMFYUI_PORT", "8188"))
BASE = f"http://{COMFY_HOST}:{COMFY_PORT}"

CLIENT_ID = uuid.uuid4().hex
STARTUP_TIMEOUT = int(os.environ.get("COMFYUI_STARTUP_TIMEOUT", "300"))
TASK_TIMEOUT = int(os.environ.get("COMFYUI_TASK_TIMEOUT", "3600"))
# H3 deliberately keeps a longer, workflow-specific wait budget.  It avoids
# falsely marking an active long video as failed while still allowing the
# caller to interrupt the ComfyUI prompt once the budget is genuinely used.
H3_TASK_TIMEOUT = int(os.environ.get("BRM_H3_TASK_TIMEOUT", "14400"))
LOG_FILE = Path(os.environ.get("COMFYUI_LOG_FILE", BASE_DIR / "comfyui_runtime.log"))

DEFAULT_ARGS = ["--enable-manager", "--disable-auto-launch"]
EXTRA_ARGS = os.environ.get("COMFYUI_ARGS", "").split()
PERF_PROFILE = os.environ.get("BRM_PERF_PROFILE", "balanced").strip().lower()
if any(arg in {"--highvram", "--gpu-only", "--disable-smart-memory", "--cache-none"} for arg in EXTRA_ARGS):
    raise RuntimeError(
        "MiniMax H3 requires ComfyUI dynamic offload and smart caching; remove --highvram, --gpu-only, "
        "--disable-smart-memory and --cache-none from COMFYUI_ARGS."
    )
START_ARGS = DEFAULT_ARGS + EXTRA_ARGS

_process: subprocess.Popen | None = None
_log_thread: threading.Thread | None = None
_stop_logging = threading.Event()


def is_port_open(host: str = COMFY_HOST, port: int = COMFY_PORT, timeout: float = 1.0) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        return s.connect_ex((host, port)) == 0


def is_comfy_ready(host: str = COMFY_HOST, port: int = COMFY_PORT) -> bool:
    if not is_port_open(host, port):
        return False
    try:
        r = requests.get(f"http://{host}:{port}/system_stats", timeout=2)
        return r.status_code == 200
    except requests.RequestException:
        return False


def is_alive() -> bool:
    global _process
    if _process is not None and _process.poll() is not None:
        return False
    return is_comfy_ready()


def queue_prompt(workflow: dict) -> dict:
    payload = {"prompt": workflow, "client_id": CLIENT_ID}
    r = requests.post(f"{BASE}/prompt", json=payload, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"ComfyUI submit failed ({r.status_code}): {r.text[:500]}")
    data = r.json()
    if data.get("node_errors"):
        raise RuntimeError(f"Workflow validation failed: {data['node_errors']}")
    return data


def get_history(prompt_id: str) -> dict:
    r = requests.get(f"{BASE}/history/{prompt_id}", timeout=10)
    return r.json() if r.status_code == 200 else {}


def get_view_file(filename: str, subfolder: str = "", folder_type: str = "output") -> bytes:
    params = {"filename": filename, "subfolder": subfolder, "type": folder_type}
    url = f"{BASE}/view?" + urllib.parse.urlencode(params)
    r = requests.get(url, timeout=60)
    r.raise_for_status()
    return r.content


def interrupt() -> tuple[bool, str]:
    """Ask ComfyUI to interrupt and report whether it accepted the request."""
    try:
        response = requests.post(f"{BASE}/interrupt", timeout=5)
        response.raise_for_status()
        return True, "已发送 ComfyUI 中断信号。"
    except requests.RequestException as e:
        return False, f"中断失败: {e}"


def wait_for_outputs(
    prompt_id: str,
    timeout: int = TASK_TIMEOUT,
    stop_event: threading.Event | None = None,
) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if stop_event is not None and stop_event.is_set():
            raise RuntimeError("任务已请求停止")
        if _process is not None and _process.poll() is not None:
            raise RuntimeError(f"ComfyUI process exited with code {_process.returncode}")

        entry = get_history(prompt_id).get(prompt_id)
        if entry:
            status = entry.get("status", {})
            if status.get("status_str") == "error":
                messages = status.get("messages") or []
                raise RuntimeError(f"ComfyUI execution failed: {messages}")
            if entry.get("outputs"):
                return entry["outputs"]
        time.sleep(1.5)
    # A timeout used to leave the prompt running in ComfyUI.  That made the
    # UI show a failed task while the A5000 stayed occupied indefinitely.
    # Request interruption before returning a distinct timeout to the queue.
    interrupted, message = interrupt()
    result = "中断请求已发送" if interrupted else message
    raise TimeoutError(f"Timed out waiting for ComfyUI task ({timeout}s); {result}")


def upload_image(filepath, subfolder: str = "", overwrite: bool = False) -> str:
    # ComfyUI uses /upload/image for image-like and many audio input nodes.
    with open(filepath, "rb") as f:
        files = {"image": (Path(filepath).name, f, "application/octet-stream")}
        data = {"type": "input", "overwrite": str(overwrite).lower()}
        if subfolder:
            data["subfolder"] = subfolder
        r = requests.post(f"{BASE}/upload/image", files=files, data=data, timeout=60)
    r.raise_for_status()
    info = r.json()
    return f"{info['subfolder']}/{info['name']}" if info.get("subfolder") else info["name"]


def audio_duration(filepath) -> float:
    try:
        from mutagen import File as MutagenFile

        info = MutagenFile(filepath)
        if info is not None and info.info is not None:
            return float(info.info.length)
    except Exception:
        pass

    try:
        import contextlib
        import wave

        with contextlib.closing(wave.open(str(filepath), "rb")) as wav:
            return wav.getnframes() / float(wav.getframerate())
    except Exception:
        return 0.0


def run_workflow(
    workflow: dict,
    timeout: int = TASK_TIMEOUT,
    stop_event: threading.Event | None = None,
) -> dict:
    res = queue_prompt(workflow)
    return wait_for_outputs(res["prompt_id"], timeout=timeout, stop_event=stop_event)


def _stream_output(proc: subprocess.Popen, log_path: Path) -> None:
    if proc.stdout is None:
        return
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"\n\n========== ComfyUI start {time.strftime('%Y-%m-%d %H:%M:%S')} ==========\n")
            for line in iter(proc.stdout.readline, ""):
                if _stop_logging.is_set():
                    break
                sys.stdout.write(line)
                sys.stdout.flush()
                f.write(line)
                f.flush()
    except Exception as e:
        print(f"[Launcher] log thread error: {e}")


def _wait_until_ready(timeout: int = STARTUP_TIMEOUT) -> None:
    print(f"[Launcher] waiting for ComfyUI ({timeout}s max): {BASE}")
    start = time.time()
    while time.time() - start < timeout:
        if _process is not None and _process.poll() is not None:
            raise RuntimeError(f"ComfyUI exited with code {_process.returncode}. See {LOG_FILE}")
        if is_comfy_ready():
            print(f"[Launcher] ComfyUI ready in {time.time() - start:.1f}s -> {BASE}")
            return
        time.sleep(1.5)
    raise TimeoutError(f"Timed out waiting for ComfyUI startup ({timeout}s)")


def start_comfyui(wait: bool = True):
    global _process, _log_thread

    if is_comfy_ready():
        print(f"[Launcher] existing ComfyUI detected at {BASE}; reusing it.")
        return None

    if not PYTHON_EXE.exists():
        raise FileNotFoundError(f"Python executable not found: {PYTHON_EXE}")
    if not MAIN_PY.exists():
        raise FileNotFoundError(
            f"ComfyUI main.py not found: {MAIN_PY}\n"
            "Set COMFYUI_ROOT to your Ubuntu ComfyUI checkout."
        )

    cmd = [str(PYTHON_EXE), "-u", str(MAIN_PY), "--listen", COMFY_HOST, "--port", str(COMFY_PORT), *START_ARGS]
    print("[Launcher] start command:", " ".join(cmd))

    _stop_logging.clear()
    child_env = {
        **os.environ,
        "PYTHONUNBUFFERED": os.environ.get("PYTHONUNBUFFERED", "1"),
        "PYTORCH_CUDA_ALLOC_CONF": os.environ.get(
            "PYTORCH_CUDA_ALLOC_CONF",
            "backend:cudaMallocAsync,expandable_segments:True",
        ),
        "CUDA_MODULE_LOADING": os.environ.get("CUDA_MODULE_LOADING", "LAZY"),
        "NVIDIA_TF32_OVERRIDE": os.environ.get("NVIDIA_TF32_OVERRIDE", "1"),
        "TORCH_ALLOW_TF32_CUBLAS_OVERRIDE": os.environ.get("TORCH_ALLOW_TF32_CUBLAS_OVERRIDE", "1"),
    }

    _process = subprocess.Popen(
        cmd,
        cwd=str(COMFY_ROOT),
        env=child_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        start_new_session=True,
    )
    _log_thread = threading.Thread(target=_stream_output, args=(_process, LOG_FILE), daemon=True)
    _log_thread.start()
    atexit.register(stop_comfyui)

    if wait:
        _wait_until_ready()
    return _process


def stop_comfyui() -> None:
    global _process
    _stop_logging.set()
    if _process is None or _process.poll() is not None:
        _process = None
        return

    print("[Launcher] stopping ComfyUI...")
    try:
        os.killpg(os.getpgid(_process.pid), signal.SIGTERM)
        _process.wait(timeout=15)
    except Exception:
        try:
            os.killpg(os.getpgid(_process.pid), signal.SIGKILL)
        except Exception:
            try:
                _process.kill()
            except Exception:
                pass
    _process = None


if __name__ == "__main__":
    start_comfyui(wait=True)
    print("[Launcher] ComfyUI is running. Press Ctrl+C to stop.")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        stop_comfyui()
