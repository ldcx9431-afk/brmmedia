"""Fail-closed ownership transfer between loopback ComfyUI runtimes on GPU0."""
import logging
import os
import subprocess
import sys
import time
from contextlib import contextmanager
import threading
import shutil

import requests

_lease_state = threading.local()
_voice_condition = threading.Condition()
_voice_file_lease = None
_voice_count = 0
_voice_releasing = False

_light_condition = threading.Condition()
_light_file_lease = None
_light_count = 0
_light_releasing = False

_model_group_lock = threading.Lock()
_last_model_groups: dict[str, str] = {}

SAFE_KEEP_VRAM_MIB = max(
    4096,
    int(os.environ.get("BRM_KEEP_MODEL_MAX_VRAM_MIB", str(22 * 1024))),
)


def workflow_model_group(workflow_name: str, args: dict | None = None) -> str:
    """Map a workflow name and optional parameters to a coarse VRAM model group."""
    name = str(workflow_name or "").strip()
    lower = name.lower()
    params = args if isinstance(args, dict) else {}
    if not lower:
        return "unknown"
    if "zonos3" in lower:
        return "zonos3"
    if "indextts" in lower or lower == "voice-clone":
        return "indextts25"
    if lower == "tts-语音克隆":
        return "indextts2"
    if "qwen3_tts" in lower or "qwen3-tts" in lower or "qwen3-voice" in lower:
        return "qwen3_tts"
    if "qwenimage21" in lower or "qwen-image21" in lower or "qwen_image21" in lower:
        return "qwen21"
    if "minimaxh3image" in lower or "h3-image" in lower or "h3_image" in lower:
        return "h3_image"
    if "flux3" in lower:
        return "flux3"
    if "flux2" in lower or lower == "image-edit":
        return "flux2"
    if "z_image" in lower or "z-image" in lower or lower == "text-to-image":
        return "z_image"
    if "seedvr2" in lower:
        variant = str(params.get("model_variant") or "7b").strip().lower()
        return "seedvr2_3b" if variant == "3b" else "seedvr2_7b"
    if "ltx" in lower or "first-last-frame" in lower or "talking-head" in lower or "talking-avatar" in lower:
        return "ltx23"
    if "minimaxh3" in lower or lower in {"text-to-video", "image-to-video"}:
        return "h3"
    if "音乐生成" in lower or "music" in lower or "acestep" in lower:
        model = str(params.get("model") or "turbo").strip().lower()
        return "acestep_base" if "base" in model else "acestep_turbo"
    return lower


def get_last_model_group(base: str) -> str | None:
    key = str(base or "").rstrip("/")
    with _model_group_lock:
        return _last_model_groups.get(key)


def set_last_model_group(base: str, model_group: str | None) -> None:
    key = str(base or "").rstrip("/")
    if not key:
        return
    with _model_group_lock:
        if model_group:
            _last_model_groups[key] = str(model_group)
        else:
            _last_model_groups.pop(key, None)


def clear_last_model_group(base: str | None = None) -> None:
    with _model_group_lock:
        if base is None:
            _last_model_groups.clear()
        else:
            _last_model_groups.pop(str(base).rstrip("/"), None)


@contextmanager
def _file_lease():
    try:
        import fcntl
    except ImportError:  # pragma: no cover - Windows unit-test fallback
        yield
        return
    from pathlib import Path
    lock_path = Path(
        os.environ.get('BRM_MEDIA_GPU_LOCK_PATH')
        or os.environ.get('BRM_MEDIA_GPU_LOCK')
        or '/var/lib/brmmedia/media-gpu.lock'
    )
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


@contextmanager
def lease():
    """Process-wide GPU ownership, also honored by isolated acceptance tools."""
    if getattr(_lease_state, 'held', False):
        yield
        return
    with _file_lease():
        _lease_state.held = True
        try:
            yield
        finally:
            _lease_state.held = False


@contextmanager
def voice_lease(main_base, cancel_event):
    global _voice_file_lease, _voice_count, _voice_releasing
    with _voice_condition:
        while True:
            if cancel_event.is_set():
                raise RuntimeError('语音任务已取消')
            if not _voice_releasing and _voice_count == 0:
                file_lease = _file_lease()
                file_lease.__enter__()
                try:
                    handover(main_base)
                except BaseException:
                    file_lease.__exit__(None, None, None)
                    raise
                _voice_file_lease = file_lease
                break
            if not _voice_releasing and _voice_count == 1 and gpu_used_mib() <= 12288:
                break
            _voice_condition.wait(timeout=0.5)
        _voice_count += 1
    try:
        if cancel_event.is_set():
            raise RuntimeError('语音任务已取消')
        yield
    finally:
        with _voice_condition:
            _voice_count -= 1
            last = _voice_count == 0
            if last:
                _voice_releasing = True
        if last:
            try:
                release_all(main_base)
            finally:
                with _voice_condition:
                    _voice_file_lease.__exit__(None, None, None)
                    _voice_file_lease = None
                    _voice_releasing = False
                    _voice_condition.notify_all()


@contextmanager
def light_lease(main_base, cancel_event=None, max_vram_mib: int = 14336, model_group: str | None = None):
    """Shared process-wide GPU0 lease for pipelined light/medium ComfyUI tasks.

    Holds the exclusive inter-process ``media-gpu.lock`` across the active batch
    so heavy tasks and external scripts cannot collide, while avoiding ``/free``
    thrashing between concurrent light ComfyUI submissions.
    """
    global _light_file_lease, _light_count, _light_releasing
    with _light_condition:
        while True:
            if cancel_event is not None and cancel_event.is_set():
                raise RuntimeError('轻量媒体任务已取消')
            if not _light_releasing and _light_count == 0:
                file_lease = _file_lease()
                file_lease.__enter__()
                try:
                    bases = runtime_bases(main_base)
                    if len(bases) > 1:
                        handover(main_base, model_group=model_group)
                    else:
                        with requests.Session() as s:
                            s.trust_env = False
                            r = s.get(main_base.rstrip('/') + '/queue', timeout=10)
                            r.raise_for_status()
                            q = r.json()
                            rows = q.get('queue_running', []) + q.get('queue_pending', [])
                            if rows:
                                raise RuntimeError('媒体运行时仍有其他任务，拒绝争抢A5000')
                            last_group = get_last_model_group(bases[0])
                            same_group = bool(model_group and last_group == model_group)
                            group_switched = bool(model_group and last_group and last_group != model_group)
                            effective_max = max(max_vram_mib, SAFE_KEEP_VRAM_MIB) if same_group else max_vram_mib
                            if group_switched or gpu_used_mib() > effective_max:
                                s.post(
                                    main_base.rstrip('/') + '/free',
                                    json={'unload_models': True, 'free_memory': True},
                                    timeout=20,
                                ).raise_for_status()
                                clear_last_model_group(bases[0])
                        if model_group:
                            set_last_model_group(bases[0], model_group)
                except BaseException:
                    file_lease.__exit__(None, None, None)
                    raise
                _light_file_lease = file_lease
                break
            if not _light_releasing and _light_count >= 1:
                if model_group:
                    bases = runtime_bases(main_base)
                    if len(bases) == 1:
                        set_last_model_group(bases[0], model_group)
                break
            _light_condition.wait(timeout=0.5)
        _light_count += 1
    try:
        if cancel_event is not None and cancel_event.is_set():
            raise RuntimeError('轻量媒体任务已取消')
        yield
    finally:
        with _light_condition:
            _light_count -= 1
            last = _light_count == 0
            if last:
                _light_releasing = True
        if last:
            task_failed = sys.exc_info()[0] is not None
            try:
                if model_group is None:
                    release_all(main_base)
                else:
                    release_all(main_base, model_group=model_group, force_free=task_failed)
            except Exception:
                if not task_failed:
                    raise
                logging.warning("light_lease GPU release failed; preserving original task error")
            finally:
                with _light_condition:
                    _light_file_lease.__exit__(None, None, None)
                    _light_file_lease = None
                    _light_releasing = False
                    _light_condition.notify_all()


def runtime_bases(main_base):
    bases = [main_base]
    if os.environ.get('BRM_QWEN_IMAGE21_ENABLED') == '1':
        bases.append(os.environ.get('BRM_QWEN_IMAGE21_URL', 'http://127.0.0.1:8188'))
    if os.environ.get('BRM_H3_IMAGE_ENABLED') == '1' or os.environ.get('BRM_H3_IMAGE_RUNTIME_READY') == '1':
        bases.append(os.environ.get('BRM_H3_IMAGE_URL', 'http://127.0.0.1:8191'))
    return list(dict.fromkeys(base.rstrip('/') for base in bases))


def _gpu_used_mib_pynvml() -> int | None:
    try:
        import pynvml  # type: ignore

        pynvml.nvmlInit()
        handle = pynvml.nvmlDeviceGetHandleByIndex(0)
        info = pynvml.nvmlDeviceGetMemoryInfo(handle)
        return int(info.used // (1024 * 1024))
    except Exception:
        return None


def gpu_used_mib():
    nvml_used = _gpu_used_mib_pynvml()
    if nvml_used is not None:
        return nvml_used
    executable = shutil.which('nvidia-smi') or '/usr/lib/wsl/lib/nvidia-smi'
    out = subprocess.check_output([executable, '--id=0', '--query-gpu=memory.used', '--format=csv,noheader,nounits'], text=True, timeout=10)
    return int(out.strip())


def handover(main_base, target=None, owned_prompt='', model_group: str | None = None, max_vram_mib: int | None = None):
    bases = runtime_bases(main_base)
    effective_max_vram = SAFE_KEEP_VRAM_MIB if max_vram_mib is None else int(max_vram_mib)
    with requests.Session() as s:
        s.trust_env = False
        idle = []
        for base in bases:
            r = s.get(base + '/queue', timeout=10)
            r.raise_for_status()
            q = r.json()
            rows = q.get('queue_running', []) + q.get('queue_pending', [])
            if rows:
                if base == target and owned_prompt and all(row[1] == owned_prompt for row in rows):
                    continue
                raise RuntimeError('媒体运行时仍有其他任务，拒绝争抢A5000')
            idle.append(base)
        if (
            len(bases) == 1
            and model_group
            and not owned_prompt
            and get_last_model_group(bases[0]) == model_group
            and gpu_used_mib() <= effective_max_vram
        ):
            set_last_model_group(bases[0], model_group)
            return
        for base in idle:
            if owned_prompt and base == target:
                continue
            s.post(base + '/free', json={'unload_models': True, 'free_memory': True}, timeout=20).raise_for_status()
            clear_last_model_group(base)
        if not owned_prompt:
            deadline = time.monotonic() + 60
            while gpu_used_mib() > 2560:
                if time.monotonic() >= deadline:
                    raise RuntimeError('A5000缓存未能安全释放，已阻止后续任务')
                time.sleep(0.5)
        if model_group and len(bases) == 1:
            set_last_model_group(bases[0], model_group)


def release_all(main_base, model_group: str | None = None, max_vram_mib: int | None = None, force_free: bool = False):
    bases = runtime_bases(main_base)
    effective_max_vram = SAFE_KEEP_VRAM_MIB if max_vram_mib is None else int(max_vram_mib)
    if (
        not force_free
        and model_group
        and len(bases) == 1
        and get_last_model_group(bases[0]) == model_group
    ):
        with requests.Session() as s:
            s.trust_env = False
            r = s.get(bases[0] + '/queue', timeout=10)
            r.raise_for_status()
            q = r.json()
            rows = q.get('queue_running', []) + q.get('queue_pending', [])
            if rows:
                raise RuntimeError('媒体运行时仍有其他任务，拒绝争抢A5000')
        if gpu_used_mib() <= effective_max_vram:
            return
    handover(main_base)
