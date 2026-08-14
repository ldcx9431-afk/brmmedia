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
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

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
if any(
    arg
    in {
        "--highvram",
        "--gpu-only",
        "--lowvram",
        "--novram",
        "--disable-smart-memory",
        "--cache-none",
        "--disable-dynamic-vram",
        "--disable-async-offload",
    }
    for arg in EXTRA_ARGS
):
    raise RuntimeError(
        "MiniMax H3 requires ComfyUI dynamic asynchronous offload and smart caching; remove the "
        "forced VRAM/offload/cache override from COMFYUI_ARGS."
    )
if "--use-ck-attention" in EXTRA_ARGS and "--use-sage-attention" in EXTRA_ARGS:
    raise RuntimeError("Kitchen and Sage Attention must be benchmarked in separate candidate cells.")
START_ARGS = DEFAULT_ARGS + EXTRA_ARGS

_process: subprocess.Popen | None = None
_log_thread: threading.Thread | None = None
_process_watch_thread: threading.Thread | None = None
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


def get_queue() -> dict:
    """Return ComfyUI's live queue, or an empty, well-formed snapshot."""
    try:
        response = requests.get(f"{BASE}/queue", timeout=10)
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, dict):
            payload["_available"] = True
            return payload
        return {"_available": False}
    except (requests.RequestException, ValueError):
        return {"_available": False}


def _queue_prompt_ids(queue: dict, key: str) -> set[str]:
    """Extract prompt IDs from ComfyUI's positional queue records."""
    ids: set[str] = set()
    for record in queue.get(key, []) if isinstance(queue, dict) else []:
        if isinstance(record, (list, tuple)) and len(record) > 1:
            ids.add(str(record[1]))
        elif isinstance(record, dict):
            prompt_id = record.get("prompt_id") or record.get("id")
            if prompt_id:
                ids.add(str(prompt_id))
    return ids


def delete_queued_prompt(prompt_id: str) -> tuple[bool, str]:
    """Delete one pending prompt without disturbing unrelated queue entries."""
    try:
        response = requests.post(f"{BASE}/queue", json={"delete": [prompt_id]}, timeout=5)
        response.raise_for_status()
        return True, "已从 ComfyUI 队列删除目标 prompt。"
    except requests.RequestException as exc:
        return False, f"删除 ComfyUI 队列项失败: {exc}"


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


@dataclass(frozen=True)
class CancellationResult:
    prompt_id: str
    was_running: bool
    was_pending: bool
    interrupt_accepted: bool
    delete_accepted: bool
    queue_cleared: bool
    history_confirmed: bool

    @property
    def confirmed(self) -> bool:
        return self.queue_cleared and (self.history_confirmed or self.delete_accepted)

    def summary(self) -> str:
        return (
            f"prompt_id={self.prompt_id}; running={self.was_running}; pending={self.was_pending}; "
            f"interrupt={self.interrupt_accepted}; delete={self.delete_accepted}; "
            f"queue_cleared={self.queue_cleared}; history_confirmed={self.history_confirmed}"
        )


def cancel_prompt(
    prompt_id: str,
    *,
    verify_timeout: float = 30.0,
    poll_interval: float = 0.5,
) -> CancellationResult:
    """Stop exactly one prompt and verify that it no longer occupies the queue.

    Local ComfyUI exposes a global ``/interrupt`` endpoint, so it is only sent
    when the requested prompt is confirmed as the running item. Pending work is
    removed through ``POST /queue`` with a prompt-id delete list. History is
    then checked so a timeout never becomes a UI-only state transition while
    the A5000 silently keeps working.
    """
    before = get_queue()
    running = _queue_prompt_ids(before, "queue_running")
    pending = _queue_prompt_ids(before, "queue_pending")
    was_running = prompt_id in running
    was_pending = prompt_id in pending
    interrupt_accepted = False
    if was_running:
        interrupt_accepted, _ = interrupt()
    delete_accepted, _ = delete_queued_prompt(prompt_id)

    deadline = time.monotonic() + max(0.0, verify_timeout)
    queue_cleared = False
    history_confirmed = False
    while True:
        current = get_queue()
        queue_cleared = current.get("_available", True) and prompt_id not in (
            _queue_prompt_ids(current, "queue_running")
            | _queue_prompt_ids(current, "queue_pending")
        )
        entry = get_history(prompt_id).get(prompt_id)
        if isinstance(entry, dict):
            status = entry.get("status") or {}
            history_confirmed = bool(entry.get("outputs")) or status.get("status_str") in {
                "success", "error",
            }
        if queue_cleared and (history_confirmed or delete_accepted):
            break
        if time.monotonic() >= deadline:
            break
        time.sleep(max(0.05, poll_interval))

    return CancellationResult(
        prompt_id=prompt_id,
        was_running=was_running,
        was_pending=was_pending,
        interrupt_accepted=interrupt_accepted,
        delete_accepted=delete_accepted,
        queue_cleared=queue_cleared,
        history_confirmed=history_confirmed,
    )


ProgressCallback = Callable[[dict], None]


def _notify(callback: ProgressCallback | None, **event) -> None:
    if callback is not None:
        callback(event)


def _websocket_url() -> str:
    host = COMFY_HOST
    if host in {"0.0.0.0", "::"}:
        host = "127.0.0.1"
    return f"ws://{host}:{COMFY_PORT}/ws?clientId={urllib.parse.quote(CLIENT_ID)}"


def _open_progress_websocket():
    """Open websocket-client lazily; callers transparently fall back to HTTP."""
    try:
        import websocket

        connection = websocket.create_connection(_websocket_url(), timeout=1.0)
        connection.settimeout(0.25)
        return connection
    except Exception:
        return None


def _handle_ws_event(prompt_id: str, payload: dict, callback: ProgressCallback | None) -> str | None:
    """Translate a ComfyUI websocket message into stable lifecycle metadata."""
    if not isinstance(payload, dict):
        return None
    kind = payload.get("type")
    data = payload.get("data") or {}
    if data.get("prompt_id") not in {None, prompt_id}:
        return None
    if kind == "execution_start":
        _notify(callback, stage="execution_start", prompt_id=prompt_id)
    elif kind == "executing":
        node = data.get("node")
        _notify(callback, stage="executing" if node else "finalizing", node_id=node, prompt_id=prompt_id)
    elif kind == "progress":
        current = data.get("value")
        total = data.get("max")
        progress = None
        if isinstance(current, (int, float)) and isinstance(total, (int, float)) and total:
            progress = max(0.0, min(1.0, float(current) / float(total)))
        _notify(
            callback, stage="sampling", current_step=current, total_steps=total,
            progress=progress, node_id=data.get("node"), prompt_id=prompt_id,
        )
    elif kind == "execution_success":
        _notify(callback, stage="completed", progress=1.0, prompt_id=prompt_id)
        return "success"
    elif kind in {"execution_error", "execution_interrupted"}:
        message = data.get("exception_message") or data.get("exception_type") or kind
        _notify(callback, stage="interrupted" if kind.endswith("interrupted") else "failed", prompt_id=prompt_id)
        raise RuntimeError(f"ComfyUI execution failed: {message}")
    return None


def wait_for_outputs(
    prompt_id: str,
    timeout: int = TASK_TIMEOUT,
    stop_event: threading.Event | None = None,
    on_progress: ProgressCallback | None = None,
    reattach: bool = False,
) -> dict:
    if reattach:
        entry = get_history(prompt_id).get(prompt_id)
        if isinstance(entry, dict) and entry.get("outputs"):
            _notify(on_progress, stage="completed", progress=1.0, prompt_id=prompt_id)
            return entry["outputs"]
        queue = get_queue()
        known = (
            _queue_prompt_ids(queue, "queue_running")
            | _queue_prompt_ids(queue, "queue_pending")
        )
        if queue.get("_available", True) and prompt_id not in known and not entry:
            raise RuntimeError(
                f"ComfyUI prompt {prompt_id} is absent from queue and history after backend restart"
            )
    deadline = time.monotonic() + timeout
    websocket_connection = _open_progress_websocket()
    next_history_poll = 0.0
    _notify(on_progress, stage="submitted", prompt_id=prompt_id, progress=0.0)
    while time.monotonic() < deadline:
        if stop_event is not None and stop_event.is_set():
            result = cancel_prompt(prompt_id)
            raise RuntimeError(f"任务已请求停止；{result.summary()}")
        if _process is not None and _process.poll() is not None:
            raise RuntimeError(f"ComfyUI process exited with code {_process.returncode}")

        if websocket_connection is not None:
            try:
                import json

                message = websocket_connection.recv()
                if isinstance(message, str):
                    _handle_ws_event(prompt_id, json.loads(message), on_progress)
            except Exception as exc:
                # Execution errors raised by the event translator must reach
                # the task queue; transport/JSON errors simply enable polling.
                if isinstance(exc, RuntimeError):
                    raise
                if "timeout" in exc.__class__.__name__.lower():
                    # A quiet websocket is normal for long nodes. Keep it open
                    # but still reach the HTTP history poll below this cycle.
                    pass
                else:
                    try:
                        websocket_connection.close()
                    except Exception:
                        pass
                    websocket_connection = None

        now = time.monotonic()
        if now >= next_history_poll:
            entry = get_history(prompt_id).get(prompt_id)
            if entry:
                status = entry.get("status", {})
                if status.get("status_str") == "error":
                    messages = status.get("messages") or []
                    raise RuntimeError(f"ComfyUI execution failed: {messages}")
                if entry.get("outputs"):
                    _notify(on_progress, stage="completed", progress=1.0, prompt_id=prompt_id)
                    return entry["outputs"]
            next_history_poll = now + 1.5
        time.sleep(0.1 if websocket_connection is not None else 0.5)
    # A timeout used to leave the prompt running in ComfyUI.  That made the
    # UI show a failed task while the A5000 stayed occupied indefinitely.
    # Request interruption before returning a distinct timeout to the queue.
    result = cancel_prompt(prompt_id)
    _notify(on_progress, stage="timed_out", prompt_id=prompt_id)
    raise TimeoutError(f"Timed out waiting for ComfyUI task ({timeout}s); {result.summary()}")


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
    workflow: dict | None,
    timeout: int = TASK_TIMEOUT,
    stop_event: threading.Event | None = None,
    on_submitted: Callable[[str], None] | None = None,
    on_progress: ProgressCallback | None = None,
    prompt_id: str | None = None,
) -> dict:
    reattach = prompt_id is not None
    if prompt_id is None:
        if workflow is None:
            raise ValueError("workflow is required when prompt_id is not supplied")
        res = queue_prompt(workflow)
        prompt_id = str(res["prompt_id"])
        if on_submitted is not None:
            on_submitted(prompt_id)
    return wait_for_outputs(
        prompt_id, timeout=timeout, stop_event=stop_event, on_progress=on_progress,
        reattach=reattach,
    )


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


def _watch_process_exit(proc: subprocess.Popen) -> None:
    """Fail the parent backend when its managed ComfyUI child crashes.

    Leaving Gradio alive after a CUDA process failure makes every queued task
    fail against a dead port.  Exiting non-zero lets systemd restart the whole
    media backend and restore one coherent ComfyUI/worker pair.
    """
    exit_code = proc.wait()
    if _stop_logging.is_set() or proc is not _process:
        return
    print(
        f"[Launcher] managed ComfyUI exited unexpectedly with code {exit_code}; "
        "terminating backend for systemd recovery.",
        file=sys.stderr,
        flush=True,
    )
    os._exit(70)


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
    global _process, _log_thread, _process_watch_thread

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
    _process_watch_thread = threading.Thread(
        target=_watch_process_exit, args=(_process,), daemon=True
    )
    _process_watch_thread.start()
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
