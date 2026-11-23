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
def light_lease(main_base, cancel_event=None, max_vram_mib: int = 14336):
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
                        handover(main_base)
                    else:
                        with requests.Session() as s:
                            s.trust_env = False
                            r = s.get(main_base.rstrip('/') + '/queue', timeout=10)
                            r.raise_for_status()
                            q = r.json()
                            rows = q.get('queue_running', []) + q.get('queue_pending', [])
                            if rows:
                                raise RuntimeError('媒体运行时仍有其他任务，拒绝争抢A5000')
                            if gpu_used_mib() > max_vram_mib:
                                s.post(
                                    main_base.rstrip('/') + '/free',
                                    json={'unload_models': True, 'free_memory': True},
                                    timeout=20,
                                ).raise_for_status()
                except BaseException:
                    file_lease.__exit__(None, None, None)
                    raise
                _light_file_lease = file_lease
                break
            if not _light_releasing and _light_count >= 1:
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
                release_all(main_base)
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


def gpu_used_mib():
    executable = shutil.which('nvidia-smi') or '/usr/lib/wsl/lib/nvidia-smi'
    out = subprocess.check_output([executable, '--id=0', '--query-gpu=memory.used', '--format=csv,noheader,nounits'], text=True, timeout=10)
    return int(out.strip())


def handover(main_base, target=None, owned_prompt=''):
    with requests.Session() as s:
        s.trust_env = False
        idle = []
        for base in runtime_bases(main_base):
            r = s.get(base + '/queue', timeout=10)
            r.raise_for_status()
            q = r.json()
            rows = q.get('queue_running', []) + q.get('queue_pending', [])
            if rows:
                if base == target and owned_prompt and all(row[1] == owned_prompt for row in rows):
                    continue
                raise RuntimeError('媒体运行时仍有其他任务，拒绝争抢A5000')
            idle.append(base)
        for base in idle:
            if owned_prompt and base == target:
                continue
            s.post(base + '/free', json={'unload_models': True, 'free_memory': True}, timeout=20).raise_for_status()
        if not owned_prompt:
            deadline = time.monotonic() + 60
            while gpu_used_mib() > 2560:
                if time.monotonic() >= deadline:
                    raise RuntimeError('A5000缓存未能安全释放，已阻止后续任务')
                time.sleep(0.5)


def release_all(main_base):
    handover(main_base)
