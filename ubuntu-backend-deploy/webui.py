# -*- coding: utf-8 -*-
"""
webui.py
========
某个具体页面的 Gradio 前端(本例:文生图)。本文件作为模板使用:
  前半部分是基本不动的通用骨架(任务对象、任务队列、存活提示);
  后半部分是每个页面都要改的内容(工作流构建、结果解析、界面与入口)。

新增不同模态的页面时,通常只改后半部分即可:
  1) 写一个 build_workflow_<名字>;
  2) 写一个 submit_<名字>;
  3) 在 WORKFLOW_BUILDERS 里登记一行;
  4) 在 build_ui 里复制一个 gr.Tab。

产物(音频 / 视频 / 图片)统一保存到 BASE_DIR/outputs,文件名以 task.name 为前缀。

依赖:
  pip install gradio requests
"""

from __future__ import annotations

import re
import enum
import os
import uuid
import time
import random
import shutil
import threading
import subprocess
from collections import deque
from dataclasses import dataclass, field
from html import escape
from pathlib import Path
from urllib.parse import quote
import gradio as gr
import requests

# 所有与 ComfyUI 的通信都来自通用层。
from comfyui_server import (
    start_comfyui, stop_comfyui, is_alive, run_workflow,
    get_view_file, interrupt, BASE, COMFY_ROOT, WORKFLOW_DIR, upload_image, audio_duration, TASK_TIMEOUT, H3_TASK_TIMEOUT,
)


# ============================================================================
# 目录与常量
# ============================================================================
# 先拿到本文件所在目录,后续若需要写文件都基于它推导。
BASE_DIR = Path(__file__).resolve().parent

# Tests and recovery tools can override this with an isolated directory. The
# production default remains next to webui.py, preserving existing assets.
OUTPUT_DIR = Path(os.environ.get("BRM_OUTPUT_DIR", BASE_DIR / "outputs")).expanduser().resolve()
# Keep the deployment reversible while H3 is staged and burn-in tested.  The
# service unit sets this to "h3" only after the native nodes and weights have
# passed validation; recovery can switch it back to ltx23 without a Git reset.
VIDEO_ENGINE = os.environ.get("BRMMEDIA_VIDEO_ENGINE", "ltx23").strip().lower()
if VIDEO_ENGINE not in {"ltx23", "h3"}:
    raise RuntimeError("BRMMEDIA_VIDEO_ENGINE must be ltx23 or h3")
# The Web UI must describe the engine that is actually wired into submitters.
# During staging or rollback the same two tabs continue to work through LTX,
# but must not present H3 profiles/audio promises that are unavailable.
H3_ENABLED = VIDEO_ENGINE == "h3"
VOICE_ENGINE = os.environ.get("BRMMEDIA_VOICE_ENGINE", "indextts2").strip().lower()
if VOICE_ENGINE not in {"indextts2", "indextts25"}:
    raise RuntimeError("BRMMEDIA_VOICE_ENGINE must be indextts2 or indextts25")
PRIMARY_T2V_TAB_LABEL = "MiniMax H3 文生视频" if H3_ENABLED else "文生视频 LTX2.3（回退）"
PRIMARY_I2V_TAB_LABEL = "MiniMax H3 图生视频" if H3_ENABLED else "图生视频 LTX2.3（回退）"
PRIMARY_VIDEO_SECONDS_MIN = 3 if H3_ENABLED else 2
# H3 runs on its own single A5000 queue and has a four-hour task deadline, so
# production permits the model's 15-second upper bound after CUDA/Sage
# acceptance.  The UI/API still default to the quicker profiles.
PRIMARY_VIDEO_SECONDS_MAX = 15 if H3_ENABLED else 360
PRIMARY_VIDEO_SECONDS_DEFAULT = 5
TASK_HISTORY_PATH = OUTPUT_DIR / "task-history.json"
# 自定义全屏查看器仅允许读取任务产物目录；不会因此暴露宿主机其它路径。
gr.set_static_paths(paths=[OUTPUT_DIR])

# 历史恢复时只扫描实际可展示或可下载的媒体，避免把日志等运行文件放进任务列表。
PERSISTABLE_MEDIA_EXTS = {
    ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif",
    ".mp4", ".webm", ".mov", ".mkv", ".avi",
    ".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg",
}


def persisted_output_path(value) -> Path | None:
    """Return a regular media file inside ``OUTPUT_DIR``, else ``None``.

    Task history is durable state and must never become a way to surface an
    arbitrary filesystem path after a manual edit, a stale migration record,
    or a symlink in the output directory.  Resolve both sides before the
    containment check so a link pointing outside the output root is rejected.
    """
    try:
        path = Path(value).resolve(strict=True)
        output_root = OUTPUT_DIR.resolve(strict=False)
    except (OSError, TypeError, ValueError):
        return None
    if path.suffix.lower() not in PERSISTABLE_MEDIA_EXTS:
        return None
    if path != output_root and output_root not in path.parents:
        return None
    return path if path.is_file() else None

DONE_GALLERY_MAX = 30       # 已完成画廊最多展示多少张
DONE_TASKS_MAX   = 200      # 已完成任务最多保留多少条(防止长时间运行后无限增长)


################################ YZY启动器配置专用 开始 ##########################################
import socket, json, sys
_no_proxy_hosts = "localhost,127.0.0.1,0.0.0.0"
for _proxy_key in ("NO_PROXY", "no_proxy"):
    _proxy_value = os.environ.get(_proxy_key, "")
    if _proxy_value:
        if "localhost" not in _proxy_value:
            os.environ[_proxy_key] = _proxy_value + "," + _no_proxy_hosts
    else:
        os.environ[_proxy_key] = _no_proxy_hosts
LOCAL_YZY_CONFIG_PATH = BASE_DIR / "yzy_config.json"
PARENT_YZY_CONFIG_PATH = BASE_DIR.parent / "yzy_config.json"
YZY_CONFIG_PATH = str(LOCAL_YZY_CONFIG_PATH if LOCAL_YZY_CONFIG_PATH.exists() else PARENT_YZY_CONFIG_PATH)
GRADIO_HOST = os.environ.get("BRM_GRADIO_HOST", "127.0.0.1")
GRADIO_ROOT_PATH = os.environ.get("BRM_GRADIO_ROOT_PATH", "").strip() or None
QWEN_API_BASE = os.environ.get("BRM_QWEN_API_BASE", "http://127.0.0.1:8000/v1").rstrip("/")
QWEN_MODEL = os.environ.get("BRM_QWEN_MODEL", "qwen35-4b-awq")
LAN_PASSWORD_HELPER = os.environ.get(
    "BRM_LAN_PASSWORD_HELPER", "/usr/local/sbin/brmmedia-set-lan-password"
)


def load_config():
    """ 加载启动器的配置文件 """
    if os.path.exists(YZY_CONFIG_PATH):
        try:
            with open(YZY_CONFIG_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            print(f"读取全局配置失败，将使用默认配置: {exc}")
    return {}


def save_config(updates: dict) -> None:
    """原子写入全局配置，避免服务意外中断时留下半个 JSON 文件。"""
    config_path = Path(YZY_CONFIG_PATH)
    config = load_config()
    config.update(updates)
    temp_path = config_path.with_name(f".{config_path.name}.{os.getpid()}.tmp")
    temp_path.write_text(
        json.dumps(config, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temp_path.replace(config_path)
    yzy_config.clear()
    yzy_config.update(config)


def configured_port(default=9000):
    """读取固定监听端口；端口冲突时显式失败，避免反向代理失去目标。"""
    try:
        return int(os.environ.get("BRM_GRADIO_PORT", str(default)))
    except ValueError:
        print("BRM_GRADIO_PORT 必须是有效的整数端口。")
        sys.exit(1)


server_port = configured_port()
# 必须是第一个打印的：把端口号打印到 stdout（Electron 会捕获）
print(json.dumps({"server_port": server_port}))
sys.stdout.flush()
time.sleep(1)
yzy_config = load_config()
fp16 = True if yzy_config.get("fp16") is None else yzy_config.get("fp16")
def config_int(name, default=1, min_value=1, max_value=4):
    try:
        value = int(yzy_config.get(name, default))
    except Exception:
        value = default
    return max(min_value, min(max_value, value))
# MiniMax H3 dynamically swaps several large components on the A5000.  It
# must never overlap another ComfyUI workflow on that GPU.  The regular LTX
# mode keeps the existing user-configurable range, while H3 makes the queue a
# deliberate single-worker queue rather than merely a UI recommendation.
MAX_MEDIA_QUEUE_CONCURRENCY = 1 if VIDEO_ENGINE == "h3" else 4
QUEUE_CONCURRENCY = config_int(
    "queue_concurrency", default=1, min_value=1, max_value=MAX_MEDIA_QUEUE_CONCURRENCY
)
DONE_TASKS_MAX = config_int("done_tasks_max", default=DONE_TASKS_MAX, min_value=20, max_value=500)
DONE_GALLERY_MAX = config_int("done_gallery_max", default=DONE_GALLERY_MAX, min_value=24, max_value=100)


def configured_free_disk_percent(default=10.0) -> float:
    """返回允许接收新任务前必须保留的最小磁盘空间百分比。"""
    try:
        value = float(os.environ.get("BRM_MIN_FREE_DISK_PERCENT", default))
    except (TypeError, ValueError):
        value = default
    return max(1.0, min(value, 50.0))


MIN_FREE_DISK_PERCENT = configured_free_disk_percent()
################################ YZY启动器配置专用 结束 ##########################################

# ============================================================================
# 通用骨架:任务对象、任务队列、存活提示(一般不用改)
# ============================================================================
class TaskStatus(str, enum.Enum):
    PENDING = "排队中"
    RUNNING = "处理中"
    DONE    = "已完成"
    CANCELLED = "已中断"
    TIMEOUT = "已超时"
    ERROR   = "失败"


@dataclass
class Task:
    id: str
    name: str
    workflow_name: str
    args: dict
    status: TaskStatus = TaskStatus.PENDING
    submit_ts: float = field(default_factory=time.time)
    start_ts: float = 0.0       # 开始执行的时间戳(被 worker 取走时记录)
    done_ts: float = 0.0        # 执行结束的时间戳(无论成功或失败)
    result: object = None       # 最终结果,这里是已保存产物的文件路径列表
    error: str = ""
    prompt_id: str = ""        # ComfyUI prompt id；提交成功后立即持久化
    stage: str = "queued"
    node_id: str | None = None
    current_step: int | float | None = None
    total_steps: int | float | None = None
    progress: float | None = None
    last_progress_ts: float = 0.0
    recovered: bool = False
    # 每个任务独立的取消信号。不能复用队列生命周期的 stop 事件，否则一次
    # 用户中断会让 worker 退出并影响之后的新任务。
    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)


def make_task_name(wfname: str) -> str:
    """任务名:工作流名 + 时间戳。也作为产物文件名前缀。"""
    return "任务_" + wfname.strip() + time.strftime("_%Y%m%d-%H%M%S_") + str(random.randint(1000, 9999))


class TaskQueue:
    """
    Gradio 程序自己的任务队列(不是 ComfyUI 的队列)。
    支持多个后台 worker、FIFO 执行。worker 数由启动器配置 queue_concurrency 控制;
    队列空时新任务一进来就会被立即取走执行。
    通过传入的 processor(task) 实际执行任务,所以队列本身与模态无关。
    """

    def __init__(self, processor, max_done: int = DONE_TASKS_MAX, max_workers: int = 1):
        self._processor = processor
        self._max_done = max_done
        self._max_workers = max(1, int(max_workers or 1))
        restored = self._load_history()
        self._pending: deque[Task] = deque(sorted(
            (task for task in restored if task.status == TaskStatus.PENDING),
            key=lambda task: task.submit_ts,
        ))
        self._running: dict[str, Task] = {}
        self._done: list[Task] = [
            task for task in restored if task.status != TaskStatus.PENDING
        ]
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()      # 有新任务时唤醒 worker,实现立即执行
        self._workers: dict[int, threading.Thread] = {}
        self._retire_worker_ids: set[int] = set()
        self._next_worker_id = 1

    @staticmethod
    def _record_from_task(task: Task) -> dict:
        """只保存恢复任务面板所需的字段，避免持久化不必要的提交参数。"""
        return {
            "id": task.id,
            "name": task.name,
            "workflow_name": task.workflow_name,
            "status": task.status.value,
            "submit_ts": task.submit_ts,
            "start_ts": task.start_ts,
            "done_ts": task.done_ts,
            "result": task.result if isinstance(task.result, list) else [],
            "error": task.error,
            "prompt_id": task.prompt_id,
            "stage": task.stage,
            "node_id": task.node_id,
            "current_step": task.current_step,
            "total_steps": task.total_steps,
            "progress": task.progress,
            "last_progress_ts": task.last_progress_ts,
            "effective_settings": {
                key: task.args[key]
                for key in TASK_EFFECTIVE_SETTING_KEYS
                if key in task.args
            },
        }

    @staticmethod
    def _task_from_record(record: dict) -> "Task | None":
        """从落盘历史恢复已经结束的任务；丢失的产物不会显示为可下载素材。"""
        try:
            status = TaskStatus(record["status"])
            prompt_id = str(record.get("prompt_id", "")).strip()
            recovered = False
            if status == TaskStatus.RUNNING:
                if prompt_id:
                    # The worker will reattach to the existing ComfyUI prompt
                    # instead of submitting a duplicate workflow.
                    status, recovered = TaskStatus.PENDING, True
                else:
                    status = TaskStatus.ERROR
            elif status == TaskStatus.PENDING:
                # Submission args intentionally are not persisted (prompts may
                # contain sensitive business data), so a pre-submit task cannot
                # be replayed after process loss.
                status = TaskStatus.ERROR
            result = [
                str(path)
                for value in record.get("result", [])
                if isinstance(value, str) and (path := persisted_output_path(value))
            ]
            if status == TaskStatus.DONE and not result:
                return None
            saved_settings = record.get("effective_settings", {})
            if not isinstance(saved_settings, dict):
                saved_settings = {}
            return Task(
                id=str(record["id"]),
                name=str(record.get("name", "历史任务")),
                workflow_name=str(record.get("workflow_name", "历史恢复")),
                args={
                    key: value
                    for key, value in saved_settings.items()
                    if key in TASK_EFFECTIVE_SETTING_KEYS
                },
                status=status,
                submit_ts=float(record.get("submit_ts", 0.0)),
                start_ts=float(record.get("start_ts", 0.0)),
                done_ts=float(record.get("done_ts", 0.0)),
                result=result,
                error=(
                    "后端重启时任务尚无 ComfyUI prompt_id，无法安全重放。"
                    if status == TaskStatus.ERROR and record.get("status") in {
                        TaskStatus.PENDING.value, TaskStatus.RUNNING.value,
                    } and not prompt_id
                    else str(record.get("error", ""))
                ),
                prompt_id=prompt_id,
                stage="recovering" if recovered else str(record.get("stage", "completed")),
                node_id=record.get("node_id"),
                current_step=record.get("current_step"),
                total_steps=record.get("total_steps"),
                progress=record.get("progress"),
                last_progress_ts=float(record.get("last_progress_ts", 0.0)),
                recovered=recovered,
            )
        except (KeyError, TypeError, ValueError):
            return None

    def _legacy_output_history(self) -> list[Task]:
        """为旧版本已经生成、但尚无任务索引的媒体创建一次可恢复历史。"""
        try:
            media_files = [
                resolved for path in OUTPUT_DIR.iterdir()
                if (resolved := persisted_output_path(path))
            ]
        except OSError:
            return []

        recovered = []
        for path in sorted(media_files, key=lambda item: item.stat().st_mtime, reverse=True):
            try:
                timestamp = path.stat().st_mtime
            except OSError:
                continue
            recovered.append(Task(
                id=f"legacy-{uuid.uuid5(uuid.NAMESPACE_URL, str(path))}",
                name=f"历史素材：{path.name}",
                workflow_name="历史恢复",
                args={},
                status=TaskStatus.DONE,
                submit_ts=timestamp,
                start_ts=timestamp,
                done_ts=timestamp,
                result=[str(path)],
            ))
        return recovered[:self._max_done]

    def _load_history(self) -> list[Task]:
        """加载完成/失败记录；首次升级时从已有 outputs 自动建立历史。"""
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        restored = []
        try:
            if TASK_HISTORY_PATH.exists():
                payload = json.loads(TASK_HISTORY_PATH.read_text(encoding="utf-8"))
                restored = [
                    task for record in payload.get("tasks", [])
                    if isinstance(record, dict) and (task := self._task_from_record(record))
                ]
        except (OSError, json.JSONDecodeError) as exc:
            print(f"[Queue] 读取任务历史失败，将从输出目录恢复: {exc}")

        if not restored:
            restored = self._legacy_output_history()
            if restored:
                self._save_history(restored)
        restored.sort(key=lambda task: task.done_ts or task.submit_ts, reverse=True)
        active = [task for task in restored if task.status == TaskStatus.PENDING]
        terminal = [task for task in restored if task.status != TaskStatus.PENDING]
        # Never evict a recoverable ComfyUI prompt merely because the completed
        # history already reached its display limit.
        return active + terminal[:self._max_done]

    def _save_history(self, tasks: "list[Task] | None" = None) -> None:
        """原子保存已结束任务；写入失败不会影响正在运行的生成任务。"""
        if tasks is None:
            active = list(getattr(self, "_pending", [])) + list(getattr(self, "_running", {}).values())
            tasks = active + self._done
        records = [self._record_from_task(task) for task in tasks]
        payload = {"version": 2, "tasks": records}
        try:
            OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
            temp_path = TASK_HISTORY_PATH.with_name(f".{TASK_HISTORY_PATH.name}.{os.getpid()}.tmp")
            temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            temp_path.replace(TASK_HISTORY_PATH)
        except OSError as exc:
            print(f"[Queue] 保存任务历史失败: {exc}")

    def enqueue(self, task: Task) -> int:
        """把任务追加到队尾并返回提交时的排队位置。"""
        with self._lock:
            task.status = TaskStatus.PENDING
            task.stage = "queued"
            self._pending.append(task)
            position = len(self._pending)
            self._save_history()
        self._wake.set()
        return position

    def clear_pending(self) -> int:
        """取消排队中的任务，并保留可查询的终态历史。

        A caller receives a ``task_id`` immediately after submission.  Dropping
        queued items outright would later turn that ID into ``not_found`` and
        make an intentional operator action indistinguishable from data loss.
        """
        with self._lock:
            cancelled = list(self._pending)
            self._pending.clear()
            now = time.time()
            for task in cancelled:
                task.status = TaskStatus.CANCELLED
                task.done_ts = now
                task.error = "队列已清空"
                self._done.insert(0, task)
            del self._done[self._max_done:]
            if cancelled:
                self._save_history()
            return len(cancelled)

    def cancel_running(self, task_ids: set[str] | None = None) -> list[Task]:
        """请求取消指定的运行任务；未指定时取消当前全部运行任务。"""
        with self._lock:
            running = [
                task for task_id, task in self._running.items()
                if task_ids is None or task_id in task_ids
            ]
            for task in running:
                task.cancel_event.set()
            return running

    def task_status(self, task_id: str) -> dict:
        """Return a safe, JSON-ready status record without leaking output paths."""
        task_id = str(task_id or "").strip()
        state_by_status = {
            TaskStatus.PENDING: "queued",
            TaskStatus.RUNNING: "running",
            TaskStatus.DONE: "completed",
            TaskStatus.CANCELLED: "cancelled",
            TaskStatus.TIMEOUT: "timed_out",
            TaskStatus.ERROR: "failed",
        }
        with self._lock:
            task = self._running.get(task_id)
            queue_position = None
            if task is None:
                for index, pending in enumerate(self._pending, start=1):
                    if pending.id == task_id:
                        task = pending
                        queue_position = index
                        break
            if task is None:
                task = next((done for done in self._done if done.id == task_id), None)
            if task is None:
                return {
                    "task_id": task_id,
                    "state": "not_found",
                    "status": "未找到",
                    "queue_position": None,
                    "output_files": [],
                }
            outputs = [
                Path(str(path)).name
                for path in (task.result if isinstance(task.result, list) else [])
                if isinstance(path, str)
            ]
            return {
                "task_id": task.id,
                "task_name": task.name,
                "workflow": task.workflow_name,
                "state": state_by_status[task.status],
                "status": task.status.value,
                "queue_position": queue_position,
                "submitted_at": task.submit_ts,
                "started_at": task.start_ts or None,
                "finished_at": task.done_ts or None,
                "output_files": outputs,
                "error": task.error or None,
                "prompt_id": task.prompt_id or None,
                "execution": {
                    "stage": task.stage,
                    "node_id": task.node_id,
                    "current_step": task.current_step,
                    "total_steps": task.total_steps,
                    "progress": task.progress,
                    "last_progress_at": task.last_progress_ts or None,
                    "recovered_after_restart": task.recovered,
                },
                "effective_settings": {
                    key: task.args[key]
                    for key in TASK_EFFECTIVE_SETTING_KEYS
                    if key in task.args
                },
            }

    def snapshot(self):
        """取一份当前状态快照,供界面渲染。"""
        with self._lock:
            return list(self._pending), list(self._running.values()), list(self._done)

    def update_execution(self, task: Task, event: dict) -> None:
        """Apply and durably store a ComfyUI lifecycle/progress event."""
        now = time.time()
        with self._lock:
            if event.get("prompt_id"):
                task.prompt_id = str(event["prompt_id"])
            if event.get("stage"):
                task.stage = str(event["stage"])
            if "node_id" in event:
                task.node_id = event.get("node_id")
            if "current_step" in event:
                task.current_step = event.get("current_step")
            if "total_steps" in event:
                task.total_steps = event.get("total_steps")
            if "progress" in event and event.get("progress") is not None:
                task.progress = float(event["progress"])
            task.last_progress_ts = now
            self._save_history()

    @property
    def stop_event(self) -> threading.Event:
        return self._stop

    def start_worker(self) -> None:
        self.start_workers()

    def start_workers(self) -> None:
        with self._lock:
            self._stop.clear()
            self._sync_workers_locked()
            active_count = len(self._workers)
        print(f"[Queue] workers started: {active_count} (target: {self._max_workers})")

    def _sync_workers_locked(self) -> None:
        """把后台 worker 数量调整到目标值；缩容只会等待当前任务自然结束。"""
        self._workers = {
            worker_id: worker
            for worker_id, worker in self._workers.items()
            if worker.is_alive()
        }
        worker_ids = sorted(self._workers)
        keep_ids = set(worker_ids[:self._max_workers])
        self._retire_worker_ids = set(worker_ids) - keep_ids

        while len(self._workers) < self._max_workers:
            worker_id = self._next_worker_id
            self._next_worker_id += 1
            worker = threading.Thread(
                target=self._loop,
                args=(worker_id,),
                daemon=True,
                name=f"task-worker-{worker_id}",
            )
            self._workers[worker_id] = worker
            worker.start()
        self._wake.set()

    def set_max_workers(self, max_workers: int) -> tuple[int, int]:
        """动态调整并发。降低并发不会中断正在处理的任务。"""
        with self._lock:
            self._max_workers = max(1, int(max_workers or 1))
            self._sync_workers_locked()
            return self._max_workers, len(self._workers)

    def set_max_done(self, max_done: int) -> int:
        """动态调整已完成任务的保留上限，并立即裁剪旧记录。"""
        with self._lock:
            self._max_done = max(1, int(max_done or 1))
            del self._done[self._max_done:]
            self._save_history()
            return self._max_done

    def _loop(self, worker_index: int = 1) -> None:
        while not self._stop.is_set():
            task = None
            with self._lock:
                if worker_index in self._retire_worker_ids:
                    self._retire_worker_ids.discard(worker_index)
                    self._workers.pop(worker_index, None)
                    return
                if self._pending:
                    task = self._pending.popleft()
                    task.status = TaskStatus.RUNNING
                    task.stage = "recovering" if task.recovered else "building_workflow"
                    task.start_ts = time.time()
                    self._running[task.id] = task
                    self._save_history()
            if task is None:
                # 队列空,等待新任务唤醒(最多等 0.5 秒再检查一次)。
                self._wake.wait(timeout=0.5)
                self._wake.clear()
                continue
            try:
                self._processor(task)
                task.status = TaskStatus.DONE
                task.stage = "completed"
                task.progress = 1.0
            except TimeoutError as e:
                task.status, task.error = TaskStatus.TIMEOUT, str(e)
                task.stage = "timed_out"
                print(f"[Queue] 任务「{task.name}」已超时，已请求中断 ComfyUI")
            except Exception as e:
                if task.cancel_event.is_set():
                    task.status, task.error = TaskStatus.CANCELLED, "用户请求中断"
                    task.stage = "interrupted"
                    print(f"[Queue] 任务「{task.name}」已中断")
                else:
                    task.status, task.error = TaskStatus.ERROR, str(e)
                    task.stage = "failed"
                    print(f"[Queue] 任务「{task.name}」失败: {e}")
            finally:
                task.done_ts = time.time()
                with self._lock:
                    self._running.pop(task.id, None)
                    self._done.insert(0, task)
                    # 只保留最近的若干条,避免列表与界面表格无限膨胀。
                    del self._done[self._max_done:]
                    self._save_history()


# 标记是否已经为 ComfyUI 掉线弹过一次提示,避免每轮检测都重复弹窗。
_health_warned = False


def check_health() -> None:
    """定时检测 ComfyUI 存活;掉线时弹出一次错误提示,恢复后重新武装。"""
    global _health_warned
    if is_alive():
        _health_warned = False
        return
    if not _health_warned:
        _health_warned = True
        raise gr.Error(
            "后台检测到 ComfyUI 进程已退出或 API 不再响应。"
            "请查看终端报错或 comfyui_runtime.log,修复后重启本程序。"
            , duration=0
        )


CUSTOM_CSS = """
:root {
    --brm-ink: #14253a;
    --brm-muted: #65758b;
    --brm-line: #dbe4eb;
    --brm-soft: #f4f7f9;
    --brm-panel: #ffffff;
    --brm-teal: #0b8793;
    --brm-teal-dark: #086775;
    --brm-teal-soft: #e8f6f6;
    --brm-shadow: 0 10px 30px rgba(22, 45, 64, 0.075);
}
footer {
    display: none !important;
}
.gradio-container {
    font-family: Inter, ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif !important;
    color: var(--brm-ink) !important;
    background: #f7f9fb !important;
}
.fillable {
    max-width: 1660px !important;
    margin: 0 auto !important;
    padding: 16px 24px 38px !important;
}
#global-toolbar {
    position: relative;
    z-index: 10;
    align-items: center;
    min-height: 70px;
    margin: 0 0 10px;
    padding: 8px 12px 8px 18px;
    border: 1px solid var(--brm-line);
    border-radius: 14px;
    background: rgba(255, 255, 255, 0.96);
    box-shadow: var(--brm-shadow);
}
#brand-lockup { margin: 0; }
#brand-lockup .brm-brand-title {
    color: var(--brm-ink);
    font-size: 1.42rem;
    font-weight: 800;
    letter-spacing: -0.02em;
}
#primary-nav-wrap { min-width: 560px !important; }
#brand-lockup .brm-brand-subtitle {
    margin-top: 2px;
    color: var(--brm-muted);
    font-size: 0.78rem;
}
#global-toolbar button {
    min-height: 40px !important;
    border-color: #cfd9e2 !important;
    border-radius: 9px !important;
    background: #fff !important;
    color: #31465b !important;
    font-weight: 700 !important;
    box-shadow: none !important;
}
/* 设计稿中的一级功能区。真实工作流仍由下面的原生 Gradio Tabs 承载。 */
#primary-nav {
    gap: 6px;
    align-items: center;
    margin: 0;
    padding: 0;
    border: 0;
    border-radius: 0;
    background: transparent;
    box-shadow: none;
}
#primary-nav > div { min-width: 0 !important; }
#primary-nav button {
    min-height: 46px !important;
    border: 1px solid transparent !important;
    border-radius: 9px !important;
    background: transparent !important;
    color: #536477 !important;
    font-size: 0.96rem !important;
    font-weight: 720 !important;
    box-shadow: none !important;
}
#primary-nav button:focus {
    outline: none !important;
    box-shadow: none !important;
}
#primary-nav button:hover {
    border-color: #cde2e5 !important;
    background: #f2f9fa !important;
    color: var(--brm-teal-dark) !important;
}
#primary-nav[data-active="image"] #nav-image,
#primary-nav[data-active="video"] #nav-video,
#primary-nav[data-active="audio"] #nav-audio,
#primary-nav[data-active="tools"] #nav-tools {
    border-color: #b9dfe2 !important;
    background: var(--brm-teal-soft) !important;
    color: var(--brm-teal-dark) !important;
    box-shadow: inset 0 -3px 0 var(--brm-teal) !important;
}
/* Gradio 会把超出宽度的 Tab 放进省略号菜单。这里保留原生按钮和
   原生切换逻辑，只将菜单铺开为设计稿中的二级工作流横向导航。 */
#workflow-tabs {
    position: relative;
    min-height: 0;
    margin: 0;
    padding: 0 !important;
    border: 0;
    background: transparent;
}
#workflow-tabs > .tab-wrapper {
    display: flex !important;
    align-items: center !important;
    gap: 7px;
    margin: 0 0 10px;
    padding: 6px 9px !important;
    overflow: hidden;
    border: 1px solid var(--brm-line);
    border-radius: 12px;
    background: #fff;
    box-shadow: 0 5px 18px rgba(22, 45, 64, 0.04);
}
#workflow-tabs .tab-container.visually-hidden { display: none !important; }
#workflow-tabs [role="tablist"] {
    flex: 0 0 auto;
    border: 0 !important;
}
#workflow-tabs .overflow-menu {
    display: flex !important;
    flex: 1 1 auto;
    min-width: 0;
}
#workflow-tabs .overflow-menu > button { display: none !important; }
#workflow-tabs .overflow-dropdown,
#workflow-tabs .overflow-dropdown.hide {
    position: static !important;
    display: flex !important;
    flex: 1 1 auto;
    flex-wrap: nowrap !important;
    gap: 5px;
    min-width: 0;
    max-width: none !important;
    padding: 0 !important;
    overflow-x: auto !important;
    border: 0 !important;
    border-radius: 0 !important;
    background: transparent !important;
    box-shadow: none !important;
    opacity: 1 !important;
    visibility: visible !important;
    transform: none !important;
    scrollbar-width: thin;
}
#workflow-tabs [role="tab"],
#workflow-tabs .overflow-dropdown button {
    position: relative !important;
    flex: 0 0 auto !important;
    min-height: 38px !important;
    width: auto !important;
    min-width: 0 !important;
    margin: 0 !important;
    padding: 8px 12px !important;
    border: 1px solid transparent !important;
    border-radius: 8px !important;
    background: transparent !important;
    color: #526477 !important;
    font-size: 0.86rem !important;
    font-weight: 680 !important;
    white-space: nowrap;
    box-shadow: none !important;
}
#workflow-tabs [role="tab"]:hover,
#workflow-tabs .overflow-dropdown button:hover {
    border-color: #c8e0e3 !important;
    background: #f0f8f9 !important;
    color: var(--brm-teal-dark) !important;
}
#workflow-tabs [role="tab"][aria-selected="true"] {
    border-color: #b7dce0 !important;
    background: var(--brm-teal-soft) !important;
    color: var(--brm-teal-dark) !important;
    box-shadow: inset 0 -3px 0 var(--brm-teal) !important;
}
#workflow-tabs [role="tab"]:focus-visible {
    outline: 3px solid rgba(11, 135, 147, 0.24) !important;
    outline-offset: 1px;
}
#workflow-tabs [role="tabpanel"] {
    min-width: 0;
    padding: 18px !important;
    border: 1px solid var(--brm-line);
    border-radius: 14px;
    background: var(--brm-panel);
    box-shadow: var(--brm-shadow);
}
#workflow-tabs [role="tabpanel"] .row { gap: 14px; }
#workflow-tabs [role="tabpanel"] .block {
    border-color: #dce5ec !important;
    border-radius: 10px !important;
    box-shadow: none !important;
}
#workflow-tabs .workflow-heading {
    margin: 0 0 13px !important;
    padding: 0 0 11px !important;
    border-bottom: 1px solid #e5ebf0;
}
#workflow-tabs .workflow-heading h2 {
    margin: 0 !important;
    color: var(--brm-ink);
    font-size: 1.22rem !important;
    font-weight: 780 !important;
    letter-spacing: -0.015em;
}
#workflow-tabs span[data-testid="block-info"] {
    margin: 0 0 8px !important;
    padding: 0 !important;
    border-radius: 0 !important;
    background: transparent !important;
    color: #26384a !important;
    font-size: 0.88rem !important;
    font-weight: 730 !important;
}
#workflow-tabs [role="tabpanel"] textarea,
#workflow-tabs [role="tabpanel"] input {
    color: var(--brm-ink) !important;
}
#workflow-tabs [role="tabpanel"] .audio-container {
    min-height: 170px !important;
    height: 170px !important;
}
#qwen-workspace { gap: 14px; align-items: stretch; }
#qwen-chat-panel,
#qwen-config-panel {
    padding: 14px;
    border: 1px solid var(--brm-line);
    border-radius: 12px;
    background: #fbfdfe;
}
#qwen-config-panel { align-self: stretch; }
#q-table-md table,
#q-table-md th,
#q-table-md td { border-color: #dbe4eb !important; }
#q-table-md table { font-size: 0.86rem; }
button.primary {
    border-color: var(--brm-teal) !important;
    background: var(--brm-teal) !important;
    color: #fff !important;
    box-shadow: 0 7px 16px rgba(11, 135, 147, 0.18) !important;
}
button.primary:hover {
    border-color: var(--brm-teal-dark) !important;
    background: var(--brm-teal-dark) !important;
}
/* 任务实时进度：仅在有运行任务时显示，避免空 HTML 宿主占位。 */
#q-live-progress {
    margin: 8px 0 10px;
}
#q-live-progress .brm-live-progress-list {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
    gap: 8px;
}
#q-live-progress .brm-live-progress-card {
    padding: 10px 13px;
    border: 1px solid #b9d7df;
    border-left: 4px solid #0f7186;
    border-radius: 9px;
    background: #f4fbfc;
    box-shadow: none;
}
#q-live-progress .brm-live-progress-heading {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 12px;
    color: #173b46;
    font-weight: 700;
}
#q-live-progress .brm-live-progress-name {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}
#q-live-progress .brm-live-progress-percent {
    flex: 0 0 auto;
    color: #0f7186;
    font-variant-numeric: tabular-nums;
}
#q-live-progress .brm-live-progress-detail {
    margin-top: 5px;
    color: #526675;
    font-size: 0.86rem;
}
#q-live-progress .brm-live-progress-track {
    height: 8px;
    margin-top: 7px;
    overflow: hidden;
    border-radius: 999px;
    background: #cce9ed;
}
#q-live-progress .brm-live-progress-fill {
    height: 100%;
    border-radius: inherit;
    background: linear-gradient(90deg, #0f7186, #32a0ae);
    transition: width 0.35s ease;
}
#q-live-progress .brm-live-progress-fill.is-indeterminate {
    width: 42% !important;
    animation: brm-progress-sweep 1.35s ease-in-out infinite;
}
@keyframes brm-progress-sweep {
    from { transform: translateX(-125%); }
    to { transform: translateX(255%); }
}
/* 任务中心：摘要、运行态和历史列表采用同一张紧凑卡片。 */
#task-center {
    gap: 8px;
    margin: 12px 0 0;
    padding: 16px;
    border: 1px solid var(--brm-line);
    border-radius: 14px;
    background: var(--brm-panel);
    box-shadow: var(--brm-shadow);
}
#task-center-title { margin: 0 !important; }
#task-center-title h3 { margin: 0 0 2px !important; color: var(--brm-ink); }
#q-summary { margin: 0; }
#q-summary .brm-queue-summary {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 8px 12px;
    min-height: 34px;
}
#q-summary .brm-queue-health {
    display: inline-flex;
    align-items: center;
    gap: 7px;
    padding: 6px 10px;
    border-radius: 999px;
    background: #f1f5f9;
    color: #334155;
    font-size: 0.86rem;
    font-weight: 750;
}
#q-summary .brm-queue-health i {
    width: 8px;
    height: 8px;
    border-radius: 999px;
    background: #64748b;
}
#q-summary .brm-queue-health.is-online { background: #eaf8f0; color: #166534; }
#q-summary .brm-queue-health.is-online i { background: #16a34a; }
#q-summary .brm-queue-health.is-offline { background: #fff1f2; color: #b91c1c; }
#q-summary .brm-queue-health.is-offline i { background: #dc2626; }
#q-summary .brm-queue-metrics { display: flex; flex-wrap: wrap; gap: 6px; }
#q-summary .brm-queue-metric {
    display: inline-flex;
    align-items: baseline;
    gap: 5px;
    padding: 5px 8px;
    border-radius: 7px;
    background: #f8fafc;
    color: #64748b;
    font-size: 0.82rem;
}
#q-summary .brm-queue-metric strong { color: #23364a; font-variant-numeric: tabular-nums; }
#q-summary .brm-queue-metric.is-active { background: #e7f5f7; color: #0f5d70; }
#q-summary .brm-queue-metric.is-success { background: #edf9f1; color: #207044; }
#q-summary .brm-queue-metric.is-warning { background: #fff8e7; color: #9a6700; }
#q-summary .brm-queue-metric.is-danger { background: #fff1f2; color: #b42318; }
/* 任务表格:给历史任务更多高度，右侧操作保持紧凑。 */
#q-table-md {
    height: 190px;
    width: 100%;
    overflow: auto;
    box-sizing: border-box;
    padding: 0;
    border: 1px solid var(--border-color-primary, #e5e7eb);
    border-radius: 10px;
}
#q-table-md table {
    width: 100%;
    min-width: 1040px;
    table-layout: fixed;
    border-collapse: separate;
    border-spacing: 0;
}
#q-table-md th {
    position: sticky;
    top: 0;
    z-index: 1;
    background: #f8fafc;
    white-space: nowrap;
}
#q-table-md th,
#q-table-md td {
    padding: 8px 11px;
    vertical-align: top;
    line-height: 1.45;
}
#queue-actions { gap: 9px; }
#queue-actions button {
    min-height: 72px !important;
    padding: 8px 12px !important;
    border-radius: 9px !important;
    font-size: 0.9rem !important;
}
#task-center-body { gap: 12px; align-items: stretch; }
#task-operation-status { min-height: 0 !important; margin: 0 !important; }
/* 完成音频是一张可直接点选的素材清单；选中后右侧播放器立刻试听并提供下载。 */
#completed-audio-list {
    border: 1px solid #d8e2ee;
    border-radius: 10px;
    overflow: hidden;
    background: #ffffff;
}
#completed-audio-list label {
    margin: 0 !important;
    padding: 7px 11px !important;
    border-bottom: 1px solid #edf2f7;
    color: #334155;
    cursor: pointer;
}
#completed-audio-list label:hover {
    background: #e8f5f6 !important;
    color: #0f5d70;
}
#completed-audio-list label:last-child { border-bottom: 0; }
#asset-center {
    gap: 10px;
    margin: 12px 0 0;
    padding: 16px;
    border: 1px solid var(--brm-line);
    border-radius: 14px;
    background: var(--brm-panel);
    box-shadow: var(--brm-shadow);
}
#audio-asset-workspace { gap: 12px; align-items: stretch; }
#audio-preview-clear button { min-height: 38px !important; }
html:not([data-brm-section="audio"]) #audio-asset-workspace,
html:not([data-brm-section="audio"]) #audio-preview-clear {
    display: none !important;
}
html[data-brm-section="audio"] #q-gallery,
html[data-brm-section="audio"] #completed-media-hint {
    display: none !important;
}
html[data-brm-section="tools"] #asset-center {
    display: none !important;
}
#completed-media { margin: 0 !important; }
#completed-media h3 { margin: 0 !important; color: var(--brm-ink); }
#completed-media-hint { margin: 8px 2px 0; color: #64748b; font-size: 0.86rem; }
/* 素材库优先展示更多真实产物，完整素材仍在点击后通过原查看器展示。 */
#q-gallery {
    border: 1px solid #d8e2ee;
    border-radius: 12px;
    overflow: hidden;
    background: #f7fafb;
}
#q-gallery .grid-wrap {
    padding: 7px !important;
    gap: 7px !important;
}
#q-gallery button {
    transform: scale(0.78);
    transform-origin: top right;
}
#q-gallery img,
#q-gallery video { border-radius: 7px !important; }
#q-table-md th:nth-child(1),
#q-table-md td:nth-child(1) { width: 28%; }
#q-table-md th:nth-child(2),
#q-table-md td:nth-child(2) { width: 112px; white-space: nowrap; }
#q-table-md th:nth-child(3),
#q-table-md td:nth-child(3) { width: 18%; }
#q-table-md th:nth-child(4),
#q-table-md td:nth-child(4) { width: 112px; white-space: nowrap; }
#q-table-md th:nth-child(5),
#q-table-md td:nth-child(5) { width: 96px; white-space: nowrap; }
#q-table-md th:nth-child(6),
#q-table-md td:nth-child(6) {
    white-space: normal;
    overflow-wrap: anywhere;
}
/* 独立的浏览器主体素材查看器，HTML 直接在固定层中渲染。 */
#media-viewer {
    position: fixed !important;
    inset: 0 !important;
    z-index: 2000 !important;
    box-sizing: border-box;
    padding: 74px 5vw max(122px, env(safe-area-inset-bottom));
    overflow: hidden;
    background: rgba(13, 25, 48, 0.84);
    backdrop-filter: blur(10px);
}
#media-viewer .brm-media-viewer-content {
    width: 100%;
    height: 100%;
    min-height: 0;
    display: flex;
    flex-direction: column;
    gap: 12px;
}
#media-viewer .brm-media-viewer-toolbar {
    width: min(1200px, 100%);
    flex: 0 0 auto;
    display: flex;
    align-items: center;
    justify-content: space-between;
    color: #f8fafc;
    gap: 12px;
}
#media-viewer .brm-media-viewer-name {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}
#media-viewer .brm-media-download {
    flex: 0 0 auto;
    padding: 8px 14px;
    border-radius: 9px;
    background: #fff;
    color: #172554;
    font-weight: 700;
    text-decoration: none;
    box-shadow: 0 4px 16px rgba(0, 0, 0, 0.18);
}
#media-viewer .brm-media-viewer-stage {
    width: min(1200px, calc(100vw - 10vw));
    /* 不能使用 flex + height:0：HTML 组件的宿主没有确定高度，会导致
       图片和视频的 max-height 解析为 0。改为由视口直接确定内容区。 */
    height: calc(100vh - 242px) !important;
    min-height: 240px !important;
    flex: 0 0 auto;
    overflow: auto;
    display: flex;
    align-items: center;
    justify-content: center;
}
#media-viewer img,
#media-viewer video {
    display: block;
    width: auto !important;
    height: auto !important;
    max-width: min(1200px, calc(100vw - 10vw)) !important;
    max-height: 100% !important;
    object-fit: contain;
    border-radius: 10px;
    box-shadow: 0 16px 64px rgba(0, 0, 0, 0.48);
}
#media-viewer-close {
    position: fixed !important;
    z-index: 2001 !important;
    top: 18px;
    right: 4vw;
    width: auto !important;
    min-width: 0 !important;
}
#media-viewer-close button {
    width: auto !important;
    min-width: 116px !important;
    padding: 9px 16px !important;
    background: #fff !important;
    color: #1e293b !important;
    box-shadow: 0 6px 24px rgba(0, 0, 0, 0.24);
}
#global-settings-panel {
    width: min(1120px, 100%) !important;
    height: auto !important;
    max-height: none !important;
    overflow: visible;
    box-sizing: border-box;
    margin: 10px auto 24px;
    padding: 24px;
    border: 1px solid #dbe3ee;
    border-top: 4px solid #155e75;
    border-radius: 16px;
    background: #fdfdfb;
    box-shadow: 0 18px 44px rgba(20, 36, 58, 0.14);
}
#global-settings-panel {
    font-size: 1rem;
    color: #1f2937;
}
#global-settings-panel .block,
#global-settings-panel .form,
#global-settings-panel .wrap {
    background: transparent !important;
}
#global-settings-panel label span {
    color: #155e75 !important;
    background: #e8f4f2 !important;
    border-radius: 999px;
    padding: 3px 9px;
    font-size: 0.88rem;
    font-weight: 700;
}
#global-settings-panel input {
    background: #f8fafc !important;
    border-color: #cbd5e1 !important;
}
#global-settings-panel button {
    border-radius: 9px !important;
}
#global-settings-panel button.primary {
    background: #155e75 !important;
    border-color: #155e75 !important;
}
#global-settings-close {
    min-width: 116px !important;
}
#global-settings-close button {
    min-width: 116px !important;
}
@media (max-width: 720px) {
    .fillable { padding: 12px 12px 26px !important; }
    #global-toolbar {
        min-height: 58px;
        padding: 8px 10px 8px 14px;
        flex-wrap: wrap !important;
    }
    #brand-lockup .brm-brand-subtitle { display: none; }
    #primary-nav-wrap {
        order: 3;
        flex-basis: 100% !important;
        width: 100% !important;
        min-width: 100% !important;
    }
    #primary-nav {
        flex-wrap: nowrap !important;
        overflow-x: auto;
    }
    #primary-nav > div { min-width: 126px !important; }
    #workflow-tabs {
        min-height: 0;
        padding: 0 !important;
        border: 0;
        border-radius: 0;
        background: transparent;
    }
    #workflow-tabs > .tab-wrapper { padding: 5px 7px !important; }
    #workflow-tabs [role="tab"],
    #workflow-tabs .overflow-dropdown button {
        font-size: 0.82rem !important;
        padding: 7px 9px !important;
    }
    #workflow-tabs [role="tabpanel"] { padding: 12px !important; }
    #q-summary .brm-queue-summary { gap: 7px; }
    #q-table-md { height: 198px; }
    #task-center { padding: 10px; }
    #asset-center { padding: 10px; }
    #q-gallery { height: 420px !important; }
    #q-gallery .grid-wrap { grid-template-columns: repeat(3, minmax(0, 1fr)) !important; }
    #global-settings-panel {
        width: 100% !important;
        padding: 18px;
    }
    #media-viewer {
        padding: 62px 3vw max(108px, env(safe-area-inset-bottom));
    }
    #media-viewer .brm-media-viewer-stage {
        width: 94vw;
        height: calc(100vh - 202px) !important;
        min-height: 200px !important;
    }
    #media-viewer img,
    #media-viewer video {
        max-width: 94vw !important;
        max-height: 100% !important;
    }
}
@media (min-width: 721px) and (max-width: 1200px) {
    #global-toolbar { flex-wrap: wrap !important; }
    #primary-nav-wrap {
        order: 3;
        flex-basis: 100% !important;
        width: 100% !important;
        min-width: 100% !important;
    }
    #primary-nav button { font-size: 0.9rem !important; }
    #q-gallery .grid-wrap { grid-template-columns: repeat(5, minmax(0, 1fr)) !important; }
}
"""


# ============================================================================
# 页面定制:工作流构建、结果解析、界面与入口(每个页面按需修改)
# ============================================================================

# ComfyUI 的 outputs 里可能出现的媒体字段(文本不在此列)。
#   images -> 图片;audio -> 音频;gifs/videos/video -> 视频
MEDIA_KEYS = ("images", "gifs", "videos", "video", "audio")

# 画廊只能显示图片,这里用扩展名过滤(音频/视频不进画廊,但都会存到 outputs)。
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}
VIDEO_EXTS   = {".mp4", ".webm", ".mov", ".mkv", ".avi"}   # .gif 已在 IMAGE_EXTS 里
AUDIO_EXTS   = {".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg"}
GALLERY_EXTS = IMAGE_EXTS | VIDEO_EXTS

# ACE-Step 的下拉选项必须反映实际存在的 DiT 权重。此前固定展示
# base/sft，即使服务器只有 turbo，也会让用户提交后才得到 ComfyUI 400。
ACE_STEP_MODEL_FILENAMES = {
    "turbo": "acestep/acestep_v1.5_xl_turbo_bf16.safetensors",
    "base": "acestep/acestep_v1.5_xl_base_bf16.safetensors",
    "sft": "acestep/acestep_v1.5_xl_sft_bf16.safetensors",
}


def installed_acestep_models() -> list[str]:
    """Return only ACE-Step model variants installed in ComfyUI's model root."""
    comfy_root = Path(os.environ.get("COMFYUI_ROOT", BASE_DIR / "ComfyUI")).expanduser()
    model_root = comfy_root / "models" / "diffusion_models"
    return [
        model for model, relative_path in ACE_STEP_MODEL_FILENAMES.items()
        if (model_root / relative_path).is_file()
    ]


def require_installed_acestep_model(model: str) -> str:
    """Reject unavailable model choices before a task reaches ComfyUI."""
    installed = installed_acestep_models()
    if model not in installed:
        readable = "、".join(installed) if installed else "无"
        raise gr.Error(
            f"ACE-Step 模型“{model}”未安装，当前可用：{readable}。"
            "请在部署对应权重后重启后端，或选择可用模型。"
        )
    return model

def _parse_size(size: str) -> tuple[int, int]:
    """把下拉框里 '宽 × 高' 形式的整体值解析成 (宽, 高)。"""
    m = re.search(r"(\d+)\s*[×xX*]\s*(\d+)", size or "")
    if not m:
        raise gr.Error(f"无法识别的尺寸:{size!r}")
    return int(m.group(1)), int(m.group(2))


# MiniMax H3 Base is deliberately kept to the locally supported 768 short
# edge.  The model generates on a 17-frame grid with a five-frame offset;
# showing the adjusted values in the task record prevents the UI/API from
# claiming an impossible duration.
H3_PROFILES = {
    # The official H3 templates use approximately 0.4MP / 73 frames as a
    # lightweight prompt-and-motion check.  Keep it a fixed native-grid job.
    "draft": {"target_pixels": 400_000, "default_seconds": 3, "min_seconds": 3, "max_seconds": 3,
              "label": "极速草稿（约 0.4MP，73 帧，约 3 秒）"},
    "preview": {"short_edge": 480, "default_seconds": 5, "min_seconds": 4, "max_seconds": 15,
                "label": "稳定预览（480 短边，约 5 秒）"},
    "quality": {"short_edge": 768, "default_seconds": 6, "min_seconds": 4, "max_seconds": 15,
                "label": "质量（768 短边，约 6 秒）"},
}
H3_MAX_LONG_EDGE = 1344
H3_FPS = 24
H3_TURBO_SAGE_MAX_SECONDS = 6
# H3 15-second jobs contain 362 frames.  The A5000 reliably completes up to
# the 768x1024 (0.786MP) cell but 1344x768 (1.032MP) caused a CUDA illegal
# address during model offload.  Cap only long requests, and publish the
# actual canvas in effective_settings instead of rejecting the requested 15s.
H3_LONG_VIDEO_MAX_PIXELS = 786_432
H3_ACCELERATION_MODES = {
    "standard": {
        "label": "官方质量（20 步）",
        "steps": 20,
        "shift_video": 12.0,
        "shift_audio": 3.0,
        "sampler": "res_multistep",
        "lora_name": None,
        "engine": "MiniMax H3 Base",
    },
    "turbo_balanced": {
        "label": "LightX2V Turbo v1.0 平衡（8 步）",
        "steps": 8,
        "shift_video": 12.0,
        "shift_audio": 3.0,
        "sampler": "euler",
        "lora_name": "minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors",
        "engine": "MiniMax H3 + LightX2V Turbo v1.0 8-step",
    },
    "turbo_fast": {
        "label": "LightX2V Turbo v1.0 极速（4 步，768P 横版）",
        "steps": 4,
        "shift_video": 6.0,
        "shift_audio": 3.0,
        "sampler": "euler",
        "lora_name": "minimax_h3_fl2v_turbo_4step_v1.0_768p_comfyui_bf16.safetensors",
        "engine": "MiniMax H3 + LightX2V Turbo v1.0 4-step 768P",
    },
}
H3_EFFECTIVE_SETTING_KEYS = (
    "profile", "requested_acceleration", "acceleration", "execution_policy",
    "engine", "base_model", "steps",
    "shift_video", "shift_audio", "sampler", "lora_name", "attention_backend",
    "requested_size", "requested_seconds", "width", "height", "frames",
    "effective_seconds",
)
VOICE_CLONE_EFFECTIVE_SETTING_KEYS = (
    "engine", "language", "speed", "output_format", "temperature_deprecated",
)
TASK_EFFECTIVE_SETTING_KEYS = H3_EFFECTIVE_SETTING_KEYS + VOICE_CLONE_EFFECTIVE_SETTING_KEYS

INDEXTTS25_SERVICE_URL = os.environ.get(
    "BRMMEDIA_INDEXTTS25_URL", "http://127.0.0.1:9205"
).rstrip("/")
INDEXTTS25_TIMEOUT_SECONDS = max(
    30, int(os.environ.get("BRMMEDIA_INDEXTTS25_TIMEOUT_SECONDS", "1800"))
)
INDEXTTS25_MAX_OUTPUT_BYTES = max(
    1_000_000, int(os.environ.get("BRMMEDIA_INDEXTTS25_MAX_OUTPUT_BYTES", str(256 * 1024 * 1024)))
)
INDEXTTS25_LANGUAGES = ("zh", "en", "ja", "es", "ar")


def normalise_h3_request(
    size: str, seconds, profile: str = "preview", acceleration: str = "standard"
) -> dict:
    """Validate a H3 request and return the actual canvas/frame-grid values."""
    profile = str(profile or "preview")
    if profile not in H3_PROFILES:
        raise gr.Error("H3 档位仅支持 draft、preview 或 quality")
    requested_acceleration = str(acceleration or "standard")
    if requested_acceleration not in H3_ACCELERATION_MODES:
        raise gr.Error("H3 加速模式仅支持 standard、turbo_balanced 或 turbo_fast")
    try:
        numeric_seconds = float(seconds)
        if not numeric_seconds.is_integer():
            raise ValueError
        requested_seconds = int(numeric_seconds)
    except (TypeError, ValueError):
        raise gr.Error("视频时长必须是当前档位支持的整数秒") from None
    limits = H3_PROFILES[profile]
    if not limits["min_seconds"] <= requested_seconds <= limits["max_seconds"]:
        raise gr.Error(
            f"MiniMax H3 {profile} 档位仅支持 {limits['min_seconds']}–{limits['max_seconds']} 秒"
        )
    requested_width, requested_height = _parse_size(size)
    if requested_acceleration == "turbo_fast" and (
        profile != "quality"
        or requested_width <= requested_height
        # The trained LightX2V cell is 1344x768 (ratio 1.75), close to but
        # intentionally not an exact mathematical 16:9 canvas.
        or abs(requested_width / requested_height - 16 / 9) > 0.03
    ):
        raise gr.Error("LightX2V 4 步极速模式首版仅支持 quality 档和 16:9 横版")
    if "target_pixels" in limits:
        scale = (limits["target_pixels"] / (requested_width * requested_height)) ** 0.5
        scale = min(scale, H3_MAX_LONG_EDGE / max(requested_width, requested_height))
    else:
        short_edge = limits["short_edge"]
        scale = min(
            short_edge / min(requested_width, requested_height),
            H3_MAX_LONG_EDGE / max(requested_width, requested_height),
        )
    width = max(32, int(round(requested_width * scale / 32)) * 32)
    height = max(32, int(round(requested_height * scale / 32)) * 32)
    # The 4-step v1.0 LoRA is trained specifically at 1344x768. Accept common
    # 16:9 selectors (for example 1920x1080) but execute on its native canvas.
    acceleration = requested_acceleration
    execution_policy = "native"
    if requested_acceleration == "turbo_fast" and requested_seconds > H3_TURBO_SAGE_MAX_SECONDS:
        # The 4-step LoRA is trained for a 1344x768 short-video cell.  Long
        # 362-frame runs must lower the canvas on a 24GB A5000, so use the
        # compatible 8-step Turbo LoRA rather than pretend a downscaled 4-step
        # model remains in-distribution.
        acceleration = "turbo_balanced"
        execution_policy = "turbo_fast_long_fallback_to_balanced"
    if acceleration == "turbo_fast":
        width, height = 1344, 768
    if requested_seconds > H3_TURBO_SAGE_MAX_SECONDS and width * height > H3_LONG_VIDEO_MAX_PIXELS:
        downscale = (H3_LONG_VIDEO_MAX_PIXELS / (width * height)) ** 0.5
        width = max(32, int(width * downscale // 32) * 32)
        height = max(32, int(height * downscale // 32) * 32)
        execution_policy = (
            "long_duration_adaptive_canvas"
            if execution_policy == "native"
            else f"{execution_policy}+adaptive_canvas"
        )
    # H3 accepts the native 17k+5 frame grid.  The official 0.4MP draft
    # template is 73 frames, so do not artificially force all profiles to 124.
    frames = round(requested_seconds * H3_FPS)
    frames += (5 - frames % 17) % 17
    acceleration_config = H3_ACCELERATION_MODES[acceleration]
    attention_backend = (
        "pytorch-stable"
        if acceleration != "standard" and requested_seconds > H3_TURBO_SAGE_MAX_SECONDS
        else "sage-auto"
    )
    return {
        "profile": profile,
        "requested_acceleration": requested_acceleration,
        "acceleration": acceleration,
        "execution_policy": execution_policy,
        "engine": acceleration_config["engine"],
        "base_model": "minimax_h3_fl2va_pruned_int8_convrot.safetensors",
        "steps": acceleration_config["steps"],
        "shift_video": acceleration_config["shift_video"],
        "shift_audio": acceleration_config["shift_audio"],
        "sampler": acceleration_config["sampler"],
        "lora_name": acceleration_config["lora_name"],
        "attention_backend": attention_backend,
        "requested_size": size,
        "requested_seconds": requested_seconds,
        "width": width,
        "height": height,
        "frames": frames,
        "effective_seconds": round(frames / H3_FPS, 3),
    }


def h3_profile_duration_update(profile: str, acceleration: str = "standard"):
    """Reset and constrain duration after an H3 profile switch.

    Browsers can retain a value from an older page bundle. Updating the
    component bounds as well as its value keeps each profile's valid range
    visible and prevents stale values reaching Gradio's schema validator.
    """
    limits = H3_PROFILES.get(str(profile), H3_PROFILES["preview"])
    acceleration = str(acceleration or "standard")
    maximum = limits["max_seconds"]
    value = limits["default_seconds"]
    mode_note = (
        "；Turbo 7–15 秒会自动切换到更稳健的 PyTorch attention 执行链"
        if acceleration != "standard" and profile != "draft"
        else ""
    )
    update = {
        "value": value,
        "minimum": limits["min_seconds"],
        "maximum": maximum,
        "label": f"视频时长（秒，{limits['min_seconds']}–{maximum}）",
        "info": f"{limits['label']}；切换档位或加速模式会自动重置时长{mode_note}。15 秒高画幅会按 A5000 容量自动下调实际画布。",
    }
    # Gradio's update helper is available in the production runtime.  Keeping
    # the plain mapping fallback also supports compatible versions (and makes
    # the H3 profile switch testable without a full Gradio installation).
    updater = getattr(gr, "update", None)
    return updater(**update) if callable(updater) else update


def h3_profile_default_seconds(profile: str) -> int:
    """Compatibility helper retained for existing API tests/callers."""
    return H3_PROFILES.get(str(profile), H3_PROFILES["preview"])["default_seconds"]


def extract_result(outputs: dict, task: Task) -> list:
    """
    把 ComfyUI 执行完的 outputs 里的产物(音频 / 视频 / 图片)下载并保存到
    BASE_DIR/outputs。文件名以 task.name 为前缀;若有多个产物,则追加
    _1、_2……(从 1 开始);只有一个产物时不加编号。
    返回保存后的文件路径(字符串)列表。
    """
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # 先把所有媒体产物的引用收集起来,便于判断是否“多个输出”。
    items = []
    for node_out in outputs.values():
        for key in MEDIA_KEYS:
            items.extend(node_out.get(key, []))

    multiple = len(items) > 1
    saved = []
    for item in items:
        try:
            raw = get_view_file(item["filename"],
                                item.get("subfolder", ""),
                                item.get("type", "output"))
        except Exception as e:
            print(f"[App] 取产物失败: {e}")
            continue
        ext = Path(item["filename"]).suffix             # 沿用原文件后缀,区分图/音/视频
        suffix = f"_{len(saved) + 1}" if multiple else ""
        out_path = OUTPUT_DIR / f"{task.name}{suffix}{ext}"
        out_path.write_bytes(raw)
        saved.append(str(out_path))
    return saved


def _indextts25_reference_path(filename: str) -> Path:
    """Resolve a ComfyUI-uploaded reference audio without accepting paths."""
    input_root = (COMFY_ROOT / "input").resolve()
    candidate = (input_root / Path(str(filename)).name).resolve()
    try:
        candidate.relative_to(input_root)
    except ValueError as exc:
        raise RuntimeError("IndexTTS-2.5 参考音频路径无效") from exc
    if not candidate.is_file():
        raise RuntimeError("IndexTTS-2.5 找不到已上传的参考音频，请重新上传")
    return candidate


def run_indextts25(task: Task) -> list[str]:
    """Invoke the isolated loopback IndexTTS-2.5 service and produce MP3.

    The service is deliberately outside ComfyUI's Python 3.12 process.  This
    prevents IndexTTS-2.5's Python 3.11/Torch runtime from changing media
    workflows, while TaskQueue keeps the existing single-A5000 serialization.
    """
    prompt = str(task.args.get("prompt", "")).strip()
    language = str(task.args.get("language", "zh")).strip().lower()
    try:
        speed = float(task.args.get("speed", 1.0))
    except (TypeError, ValueError) as exc:
        raise RuntimeError("IndexTTS-2.5 语速必须是数字") from exc
    if language not in INDEXTTS25_LANGUAGES:
        raise RuntimeError("IndexTTS-2.5 仅支持 zh、en、ja、es、ar")
    if not 0.5 <= speed <= 2.0:
        raise RuntimeError("IndexTTS-2.5 语速必须在 0.5–2.0 之间")
    if not prompt:
        raise RuntimeError("IndexTTS-2.5 合成文本不能为空")
    reference = _indextts25_reference_path(task.args.get("ref_audio", ""))
    task_queue.update_execution(task, {"stage": "checking_indextts25"})
    try:
        health = requests.get(f"{INDEXTTS25_SERVICE_URL}/health", timeout=(5, 10))
    except requests.RequestException as exc:
        raise RuntimeError("IndexTTS-2.5 候选服务不可用，未回退到旧版。请联系管理员检查服务状态。") from exc
    if health.status_code != 200:
        raise RuntimeError(f"IndexTTS-2.5 健康检查失败（HTTP {health.status_code}）")
    try:
        health_payload = health.json()
    except ValueError as exc:
        raise RuntimeError("IndexTTS-2.5 健康检查返回了无效内容") from exc
    if not health_payload.get("ok"):
        detail = health_payload.get("detail") or health_payload.get("error") or "候选服务未就绪"
        raise RuntimeError(f"IndexTTS-2.5 候选服务未就绪：{detail}")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    wav_path = OUTPUT_DIR / f".{task.id}.indextts25.wav"
    mp3_path = OUTPUT_DIR / f"{task.name}.mp3"
    task_queue.update_execution(task, {"stage": "synthesizing_indextts25"})
    try:
        with reference.open("rb") as audio_file:
            response = requests.post(
                f"{INDEXTTS25_SERVICE_URL}/v1/voice-clone",
                data={"text": prompt, "language": language, "speed": f"{speed:.3f}"},
                files={"reference_audio": (reference.name, audio_file, "application/octet-stream")},
                timeout=(10, INDEXTTS25_TIMEOUT_SECONDS),
            )
        if response.status_code != 200:
            detail = response.text[:500].replace("\n", " ")
            raise RuntimeError(f"IndexTTS-2.5 合成失败（HTTP {response.status_code}）：{detail}")
        if len(response.content) > INDEXTTS25_MAX_OUTPUT_BYTES:
            raise RuntimeError("IndexTTS-2.5 返回音频过大，已拒绝保存")
        wav_path.write_bytes(response.content)
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise RuntimeError("未安装 ffmpeg，无法将 IndexTTS-2.5 WAV 转换为 MP3")
        task_queue.update_execution(task, {"stage": "encoding_mp3"})
        conversion = subprocess.run(
            [ffmpeg, "-nostdin", "-loglevel", "error", "-y", "-i", str(wav_path),
             "-codec:a", "libmp3lame", "-b:a", "192k", str(mp3_path)],
            text=True, capture_output=True, timeout=180, check=False,
        )
        if conversion.returncode != 0 or not mp3_path.is_file():
            raise RuntimeError(f"IndexTTS-2.5 MP3 转换失败：{conversion.stderr[-500:]}")
    finally:
        wav_path.unlink(missing_ok=True)
    task.args.update({
        "engine": "IndexTTS-2.5",
        "language": language,
        "speed": speed,
        "output_format": "MP3（源服务输出 22.05kHz WAV）",
        "temperature_deprecated": "已接收但在 IndexTTS-2.5 中不参与推理",
    })
    return [str(mp3_path)]


# ---------------------------------------------------------------------------
# 提交工作流
# ---------------------------------------------------------------------------
def submit_workflow_1(prompt, size, batch):
    # 工作流 JSON 的文件名(不含 .json)
    if not (prompt or "").strip():
        raise gr.Error("请输入图片提示词")
    return submit("image_z_image_turbo", {"prompt": prompt, "size": size, "batch": batch})


def submit_workflow_2(prompt, input_filename):
    # 工作流 JSON 的文件名(不含 .json)
    if not prompt or not input_filename:
        raise gr.Error("请输入提示词并上传要编辑的图片")
    return submit("image_flux2_klein_image_edit_4b_base", {"prompt": prompt, "input_filename": input_filename})

def submit_workflow_3(prompt, size, seconds, profile="preview", acceleration="standard"):
    if not (prompt or "").strip():
        raise gr.Error("请输入视频提示词")
    if VIDEO_ENGINE == "ltx23":
        return submit("LTX23-文生视频", {"prompt": prompt, "seconds": seconds, "size": size})
    """Queue a local MiniMax H3 text-to-video job."""
    args = {
        "prompt": prompt,
        **normalise_h3_request(size, seconds, profile, acceleration),
    }
    return submit("MiniMaxH3-文生视频", args)

def submit_workflow_4(
    prompt, input_filename, seconds, profile="preview", size="768 × 1024",
    acceleration="standard",
):
    if not (prompt or "").strip() or not input_filename:
        raise gr.Error("请输入提示词并上传源图片")
    if VIDEO_ENGINE == "ltx23":
        return submit("LTX23-图生视频", {"prompt": prompt, "seconds": seconds, "input_filename": input_filename})
    """Queue a local MiniMax H3 image-to-video job."""
    args = {
        "prompt": prompt,
        "input_filename": input_filename,
        **normalise_h3_request(size, seconds, profile, acceleration),
    }
    return submit("MiniMaxH3-图生视频", args)


def submit_workflow_3_ltx(prompt, size, seconds):
    """Queue the retained LTX2.3 text-to-video workflow explicitly.

    H3 is the production default, but LTX must remain a first-class choice
    rather than only becoming visible during an H3 rollback.
    """
    if not (prompt or "").strip():
        raise gr.Error("请输入视频提示词")
    return submit("LTX23-文生视频", {"prompt": prompt, "seconds": seconds, "size": size})


def submit_workflow_4_ltx(prompt, input_filename, seconds):
    """Queue the retained LTX2.3 image-to-video workflow explicitly."""
    if not (prompt or "").strip() or not input_filename:
        raise gr.Error("请输入提示词并上传源图片")
    return submit(
        "LTX23-图生视频",
        {"prompt": prompt, "seconds": seconds, "input_filename": input_filename},
    )


def submit_workflow_5(prompt, input_filename1, input_filename2, seconds):
    # 工作流 JSON 的文件名(不含 .json)
    if not (prompt or "").strip() or not input_filename1 or not input_filename2:
        raise gr.Error("请输入提示词并上传首帧、尾帧图片")
    return submit(
        "LTX23-首尾帧视频",
        {
            "prompt": prompt, "seconds": seconds,
            "input_filename1": input_filename1,
            "input_filename2": input_filename2,
        }
    )


def submit_workflow_6(prompt, image, audio, uploaded_dur, size):
    # 工作流 JSON 的文件名(不含 .json)
    if not (prompt or "").strip() or not image or not audio:
        raise gr.Error("请输入提示词并上传驱动图片、音频")
    if not uploaded_dur or float(uploaded_dur) <= 0:
        raise gr.Error("无法读取音频时长，请重新上传有效音频")
    return submit(
        "LTX23-单图数字人-语音驱动",
        {
            "prompt": prompt, "size": size,
            "image": image,
            "audio": audio,
            "duration": uploaded_dur,
        }
    )


def submit_workflow_7(prompt, ref_audio, temperature=0.8, language="zh", speed=1.0):
    """Queue the isolated IndexTTS-2.5 voice-clone service.

    ``temperature`` remains in the public signature for existing LAN callers,
    but IndexTTS-2.5 uses native language and duration/pace control instead.
    """
    if not prompt or not prompt.strip():
        raise gr.Error("请输入要合成的文本")
    if not ref_audio:
        raise gr.Error("请上传参考音频(克隆音色来源)")
    if VOICE_ENGINE == "indextts2":
        # Candidate code can be released while the historical ComfyUI workflow
        # remains the active production route until human A/B acceptance.
        return submit("TTS-语音克隆", {
            "prompt": prompt, "ref_audio": ref_audio, "temperature": temperature,
            "engine": "IndexTTS-2（回退）",
        })
    language = str(language or "zh").lower()
    if language not in INDEXTTS25_LANGUAGES:
        raise gr.Error("语言仅支持中文、英语、日语、西语或阿语")
    try:
        speed = float(speed)
    except (TypeError, ValueError):
        raise gr.Error("语速必须是 0.5–2.0 的数字") from None
    if not 0.5 <= speed <= 2.0:
        raise gr.Error("语速仅支持 0.5–2.0")
    return submit("IndexTTS-2.5-语音克隆", {
        "prompt": prompt,
        "ref_audio": ref_audio,
        "temperature": temperature,
        "language": language,
        "speed": speed,
    })


def submit_workflow_8(tags, lyrics, duration=30.0, bpm=120, language="zh", model="turbo"):
    # 音乐生成(ACE-Step 1.5)。
    if not tags or not tags.strip():
        raise gr.Error("请输入音乐风格标签(tags)")
    model = require_installed_acestep_model(str(model or "turbo"))
    return submit("音乐生成", {
        "tags": tags,
        "lyrics": lyrics,
        "duration": duration,
        "bpm": bpm,
        "language": language,
        "model": model,
    })


# Public API wrappers deliberately use ordinary typed arguments instead of
# gr.State.  Gradio omits State components from generated public APIs, which
# would otherwise make the file-name arguments for edit/video/TTS workflows
# impossible to pass from a LAN client.
def api_submit_workflow_1(prompt: str, size: str, batch: int) -> dict:
    return submit_workflow_1(prompt, size, batch)


def api_submit_workflow_2(prompt: str, input_filename: str) -> dict:
    return submit_workflow_2(prompt, input_filename)


def api_submit_workflow_3(prompt: str, size: str, seconds: int) -> dict:
    return submit_workflow_3(prompt, size, seconds)


def api_submit_workflow_4(prompt: str, input_filename: str, seconds: int) -> dict:
    return submit_workflow_4(prompt, input_filename, seconds)


# Versioned H3-oriented wrappers are used by the REST bridge.  Keep the two
# legacy Gradio endpoints above intact for clients that already call them.
def api_submit_workflow_3_h3(
    prompt: str, size: str, seconds: int, profile: str,
    acceleration: str = "standard",
) -> dict:
    if VIDEO_ENGINE != "h3":
        raise gr.Error("MiniMax H3 尚未启用；当前视频引擎为 LTX2.3")
    return submit_workflow_3(prompt, size, seconds, profile, acceleration)


def api_submit_workflow_4_h3(
    prompt: str, input_filename: str, size: str, seconds: int, profile: str,
    acceleration: str = "standard",
) -> dict:
    if VIDEO_ENGINE != "h3":
        raise gr.Error("MiniMax H3 尚未启用；当前视频引擎为 LTX2.3")
    return submit_workflow_4(prompt, input_filename, seconds, profile, size, acceleration)


def api_submit_workflow_3_ltx(prompt: str, size: str, seconds: int) -> dict:
    """Stable public endpoint for the explicitly selected LTX2.3 T2V path."""
    return submit_workflow_3_ltx(prompt, size, seconds)


def api_submit_workflow_4_ltx(prompt: str, input_filename: str, seconds: int) -> dict:
    """Stable public endpoint for the explicitly selected LTX2.3 I2V path."""
    return submit_workflow_4_ltx(prompt, input_filename, seconds)


def api_submit_workflow_5(
    prompt: str, input_filename1: str, input_filename2: str, seconds: int
) -> dict:
    return submit_workflow_5(prompt, input_filename1, input_filename2, seconds)


def api_submit_workflow_6(
    prompt: str, image: str, audio: str, duration: float, size: str
) -> dict:
    return submit_workflow_6(prompt, image, audio, duration, size)


def api_submit_workflow_7(
    prompt: str, ref_audio: str, temperature: float = 0.8,
    language: str = "zh", speed: float = 1.0,
) -> dict:
    return submit_workflow_7(prompt, ref_audio, temperature, language, speed)


def ui_submit_workflow_7(prompt: str, ref_audio: str, language: str, speed: float) -> dict:
    """UI adapter: do not expose legacy temperature on the refreshed tab."""
    return submit_workflow_7(prompt, ref_audio, 0.8, language, speed)


def api_submit_workflow_8(
    tags: str, lyrics: str, duration: float, bpm: int, language: str, model: str
) -> dict:
    return submit_workflow_8(tags, lyrics, duration, bpm, language, model)


# ---------------------------------------------------------------------------
# 构建 工作流
# ---------------------------------------------------------------------------
def build_workflow_1(workflow_name: str, args: dict) -> dict:
    """
    workflow_name 是工作流文件名(不是完整路径,没有 .json 后缀),会到 WORKFLOW_DIR 下查找。
    """
    import json
    path = WORKFLOW_DIR / (workflow_name + ".json")
    if not path.exists():
        raise gr.Error(f"找不到工作流文件:{path}")
    wf = json.loads(path.read_text(encoding="utf-8"))
    width, height = _parse_size(args["size"])
    wf["57:27"]["inputs"]["text"] = args["prompt"]
    wf["57:13"]["inputs"]["width"] = width
    wf["57:13"]["inputs"]["height"] = height
    wf["57:3"]["inputs"]["seed"] = random.randint(1, 10000)
    wf["57:13"]["inputs"]["batch_size"] = int(args["batch"])   # batch_size 必须是整数
    return wf


def build_workflow_2(workflow_name: str, args: dict) -> dict:
    """
    工作流的构建。读取 JSON 后编辑。
    """
    import json
    path = WORKFLOW_DIR / (workflow_name + ".json")
    if not path.exists():
        raise gr.Error(f"找不到工作流文件:{path}")
    wf = json.loads(path.read_text(encoding="utf-8"))
    wf["76"]["inputs"]["image"] = args["input_filename"]
    wf["75:74"]["inputs"]["text"] = args["prompt"]
    wf["75:73"]["inputs"]["noise_seed"] = random.randint(1, 10000)
    return wf


def build_workflow_3(workflow_name: str, args: dict) -> dict:
    """
    工作流的构建。读取 JSON 后编辑。
    """
    import json
    path = WORKFLOW_DIR / (workflow_name + ".json")
    if not path.exists():
        raise gr.Error(f"找不到工作流文件:{path}")
    wf = json.loads(path.read_text(encoding="utf-8"))
    wf["104"]["inputs"].update({
        "prompt": args["prompt"], "width": args["width"],
        "height": args["height"], "length": args["frames"],
    })
    _configure_h3_acceleration(wf, args)
    wf["15"]["inputs"]["noise_seed"] = random.randint(1, 1_000_000)
    wf["110"]["inputs"]["filename_prefix"] = "MiniMaxH3-文生视频"
    return wf


def build_workflow_4(workflow_name: str, args: dict) -> dict:
    """
    工作流的构建。读取 JSON 后编辑。
    """
    import json
    path = WORKFLOW_DIR / (workflow_name + ".json")
    if not path.exists():
        raise gr.Error(f"找不到工作流文件:{path}")
    wf = json.loads(path.read_text(encoding="utf-8"))
    wf["1"]["inputs"]["image"] = args["input_filename"]
    wf["104"]["inputs"].update({
        "prompt": args["prompt"], "width": args["width"],
        "height": args["height"], "length": args["frames"],
    })
    _configure_h3_acceleration(wf, args)
    wf["15"]["inputs"]["noise_seed"] = random.randint(1, 1_000_000)
    wf["110"]["inputs"]["filename_prefix"] = "MiniMaxH3-图生视频"
    return wf


def _configure_h3_acceleration(wf: dict, args: dict) -> None:
    """Patch an H3 API graph for the selected acceleration mode.

    Standard deliberately preserves the checked-in 20-step Sage graph. Turbo
    graphs add only ComfyUI's core model-only LoRA loader between UNETLoader
    and MiniMaxH3SigmaShift; the existing Sage patch remains downstream.
    """
    acceleration = str(args.get("acceleration") or "standard")
    config = H3_ACCELERATION_MODES.get(acceleration)
    if config is None:
        raise gr.Error("H3 加速模式仅支持 standard、turbo_balanced 或 turbo_fast")
    if acceleration == "standard":
        return

    loader_id = "111"
    while loader_id in wf:
        loader_id = str(int(loader_id) + 1)
    wf[loader_id] = {
        "class_type": "LoraLoaderModelOnly",
        "inputs": {
            "model": ["6", 0],
            "lora_name": config["lora_name"],
            "strength_model": 1.0,
        },
    }
    wf["25"]["inputs"].update({
        "model": [loader_id, 0],
        "shift_video": config["shift_video"],
        "shift_audio": config["shift_audio"],
    })
    wf["17"]["inputs"]["sampler_name"] = config["sampler"]
    wf["9"]["inputs"]["steps"] = config["steps"]
    if args.get("attention_backend") == "pytorch-stable":
        # SageAttention is retained for standard and short Turbo requests.
        # On the A5000, long Turbo latents plus dynamic LoRA patching have
        # produced a reproducible illegal-address failure in the fused Sage
        # path.  Keep the LoRA acceleration but route the two model consumers
        # directly to the sigma-shifted model for the 7–15 second cell.
        wf["9"]["inputs"]["model"] = ["25", 0]
        wf["16"]["inputs"]["model"] = ["25", 0]


def build_workflow_ltx3(workflow_name: str, args: dict) -> dict:
    import json
    path = WORKFLOW_DIR / (workflow_name + ".json")
    if not path.exists():
        raise gr.Error(f"找不到工作流文件:{path}")
    width, height = _parse_size(args["size"])
    wf = json.loads(path.read_text(encoding="utf-8"))
    wf["14"]["inputs"]["text"] = args["prompt"]
    wf["38"]["inputs"]["value"] = args["seconds"]
    wf["23"]["inputs"]["width"] = width
    wf["23"]["inputs"]["height"] = height
    wf["29"]["inputs"]["noise_seed"] = random.randint(1, 10000)
    return wf


def build_workflow_ltx4(workflow_name: str, args: dict) -> dict:
    import json
    path = WORKFLOW_DIR / (workflow_name + ".json")
    if not path.exists():
        raise gr.Error(f"找不到工作流文件:{path}")
    wf = json.loads(path.read_text(encoding="utf-8"))
    wf["10"]["inputs"]["image"] = args["input_filename"]
    wf["14"]["inputs"]["text"] = args["prompt"]
    wf["38"]["inputs"]["value"] = args["seconds"]
    wf["29"]["inputs"]["noise_seed"] = random.randint(1, 10000)
    return wf


def build_workflow_5(workflow_name: str, args: dict) -> dict:
    """
    工作流的构建。读取 JSON 后编辑。
    """
    import json
    path = WORKFLOW_DIR / (workflow_name + ".json")
    if not path.exists():
        raise gr.Error(f"找不到工作流文件:{path}")
    wf = json.loads(path.read_text(encoding="utf-8"))
    wf["10"]["inputs"]["image"] = args["input_filename1"]
    wf["220"]["inputs"]["image"] = args["input_filename2"]
    wf["14"]["inputs"]["text"] = args["prompt"]
    wf["38"]["inputs"]["value"] = args["seconds"]
    wf["29"]["inputs"]["noise_seed"] = random.randint(1, 10000)
    return wf


def build_workflow_6(workflow_name: str, args: dict) -> dict:
    """
    工作流的构建。读取 JSON 后编辑。
    """
    import json
    path = WORKFLOW_DIR / (workflow_name + ".json")
    if not path.exists():
        raise gr.Error(f"找不到工作流文件:{path}")
    width, height = _parse_size(args["size"])
    wf = json.loads(path.read_text(encoding="utf-8"))
    wf["423"]["inputs"]["audio"] = args["audio"]
    wf["423"]["inputs"]["start_time"] = 0.0
    wf["423"]["inputs"]["end_time"] = args["duration"]
    wf["423"]["inputs"]["duration"] = args["duration"]

    wf["301"]["inputs"]["image_paths"] = args["image"]
    wf["296"]["inputs"]["value"] = width
    wf["297"]["inputs"]["value"] = height

    frames = int(args["duration"] * 24 + 1)
    wf["294"]["inputs"]["local_prompts"] = args["prompt"]
    # wf["294"]["inputs"]["max_frames"][0] = str(frames)
    wf["294"]["inputs"]["segment_lengths"] = frames
    timeline_data = {"segments":[{"prompt":args["prompt"],"length":frames,"color":"#d9534f"}]}
    wf["294"]["inputs"]["timeline_data"] = json.dumps(timeline_data)

    wf["29"]["inputs"]["noise_seed"] = random.randint(1, 10000)
    return wf


def build_workflow_7(workflow_name: str, args: dict) -> dict:
    """
    TTS / 语音克隆工作流构建(IndexTTS2)。
    工作流结构:LoadAudio(参考音频) -> IndexTTS2BaseNode(合成) -> SaveAudio(保存)。
    args:
      prompt:           要合成的文本
      ref_audio:        克隆参考音频在 ComfyUI input 目录里的文件名(由 upload_image 上传得到)
      temperature:      采样温度,影响多样性(默认 0.8)
    """
    import json
    path = WORKFLOW_DIR / (workflow_name + ".json")
    if not path.exists():
        raise gr.Error(f"找不到工作流文件:{path}")
    wf = json.loads(path.read_text(encoding="utf-8"))

    # 节点1: LoadAudio —— 指定参考音频文件名
    wf["1"]["inputs"]["audio"] = args["ref_audio"]
    # 节点2: IndexTTS2BaseNode —— 填写合成文本与采样参数
    wf["2"]["inputs"]["text"] = args["prompt"]
    wf["2"]["inputs"]["seed"] = random.randint(1, 1000000)
    if "temperature" in args:
        wf["2"]["inputs"]["temperature"] = args["temperature"]
    return wf


def build_workflow_8(workflow_name: str, args: dict) -> dict:
    """
    音乐生成工作流构建(ACE-Step 1.5)。
    工作流结构:
      UNETLoader(turbo) -> ModelSamplingAuraFlow -> KSampler(8步,超快)
      DualCLIPLoader(qwen0.6b+4b) -> TextEncodeAceStepAudio1.5(tags+lyrics)
      -> ConditioningZeroOut(negative)
      EmptyAceStep1.5LatentAudio -> KSampler -> VAEDecodeAudio -> SaveAudioMP3
    args:
      tags:     音乐风格标签(如 "pop, upbeat, electronic")
      lyrics:   歌词(可选,纯音乐留空)
      duration: 时长秒(默认30)
      bpm:      节拍(默认120)
      language: 语言(默认zh)
      model:    模型版本 turbo/base/sft(默认turbo)
    """
    import json
    path = WORKFLOW_DIR / (workflow_name + ".json")
    if not path.exists():
        raise gr.Error(f"找不到工作流文件:{path}")
    wf = json.loads(path.read_text(encoding="utf-8"))

    # 选择模型版本(turbo/base/sft),对应不同的采样参数
    model = require_installed_acestep_model(str(args.get("model", "turbo")))
    wf["104"]["inputs"]["unet_name"] = ACE_STEP_MODEL_FILENAMES[model]
    if model == "turbo":
        wf["3"]["inputs"]["steps"] = 8        # turbo: 8步极速
        wf["3"]["inputs"]["cfg"] = 1.0
    else:
        wf["3"]["inputs"]["steps"] = 50       # base/sft: 50步高质量
        wf["3"]["inputs"]["cfg"] = 4.0

    # 文本编码: tags(风格) + lyrics(歌词)
    wf["94"]["inputs"]["tags"] = args["tags"]
    wf["94"]["inputs"]["lyrics"] = args.get("lyrics", "")
    wf["94"]["inputs"]["bpm"] = int(args.get("bpm", 120))
    wf["94"]["inputs"]["language"] = args.get("language", "zh")
    duration = float(args.get("duration", 30.0))
    wf["94"]["inputs"]["duration"] = duration
    wf["94"]["inputs"]["seed"] = random.randint(1, 1000000)

    # latent 时长 = 生成时长
    wf["98"]["inputs"]["seconds"] = duration

    # KSampler seed(独立于文本编码的seed)
    wf["3"]["inputs"]["seed"] = random.randint(1, 1000000)
    return wf


# ---------------------------------------------------------------------------
# 工作流登记表:工作流名 -> 构建函数。新增工作流时,在这里登记一行即可。
# ---------------------------------------------------------------------------
WORKFLOW_BUILDERS = {
    "image_z_image_turbo": (build_workflow_1, "文生图"),
    "image_flux2_klein_image_edit_4b_base": (build_workflow_2, "图片编辑"),
    "MiniMaxH3-文生视频": (build_workflow_3, "MiniMax H3 文生视频"),
    "MiniMaxH3-图生视频": (build_workflow_4, "MiniMax H3 图生视频"),
    "LTX23-文生视频": (build_workflow_ltx3, "LTX2.3 文生视频"),
    "LTX23-图生视频": (build_workflow_ltx4, "LTX2.3 图生视频"),
    "LTX23-首尾帧视频": (build_workflow_5, "首尾帧视频"),
    "LTX23-单图数字人-语音驱动": (build_workflow_6, "单图数字人-语音驱动"),
    # Keep the existing ComfyUI workflow available for a deliberate rollback.
    "TTS-语音克隆": (build_workflow_7, "语音克隆"),
    "IndexTTS-2.5-语音克隆": (build_workflow_7, "语音克隆 IndexTTS-2.5"),
    "音乐生成": (build_workflow_8, "音乐生成"),
}

H3_STAGE_BY_NODE = {
    "6": "loading_diffusion_model",
    "13": "loading_text_encoder",
    "104": "encoding_prompt",
    "14": "sampling",
    "10": "decoding_video",
    "23": "decoding_audio",
    "91": "muxing",
    "110": "saving_artifacts",
}


def process_task(task: Task) -> None:
    """一次完整执行:构建工作流 -> 提交并等待 -> 解析并保存结果。供任务队列调用。"""
    if task.workflow_name == "IndexTTS-2.5-语音克隆":
        # Keep IndexTTS-2.5 out of ComfyUI's Python 3.12 runtime.  It still
        # runs inside the same TaskQueue worker, so it cannot contend with H3
        # or the other A5000 media workflows.
        task.result = run_indextts25(task)
        task_queue.update_execution(task, {"stage": "saving_artifacts", "progress": 1.0})
        return
    workflow = None
    if not task.prompt_id:
        builder = WORKFLOW_BUILDERS.get(task.workflow_name)
        if builder is None:
            raise ValueError(f"未登记的工作流:{task.workflow_name}")
        workflow = builder[0](task.workflow_name, task.args)
    timeout = H3_TASK_TIMEOUT if task.workflow_name.startswith("MiniMaxH3-") else None

    def submitted(prompt_id: str) -> None:
        task_queue.update_execution(task, {"prompt_id": prompt_id, "stage": "submitted"})

    def progress(event: dict) -> None:
        event = dict(event)
        if task.workflow_name.startswith("MiniMaxH3-") and event.get("node_id") in H3_STAGE_BY_NODE:
            event["stage"] = H3_STAGE_BY_NODE[event["node_id"]]
        task_queue.update_execution(task, event)

    outputs = run_workflow(
        workflow,
        timeout=timeout if timeout is not None else TASK_TIMEOUT,
        submit_timeout=timeout if timeout is not None else None,
        stop_event=task.cancel_event,
        prompt_id=task.prompt_id or None,
        on_submitted=submitted,
        on_progress=progress,
    )
    task_queue.update_execution(task, {"stage": "saving_artifacts", "progress": 1.0})
    task.result = extract_result(outputs, task)


# 任务队列实例,processor 指向本页面的 process_task。
task_queue = TaskQueue(processor=process_task, max_workers=QUEUE_CONCURRENCY)


def _setting_int(value, default: int, min_value: int, max_value: int) -> int:
    try:
        value = int(value)
    except (TypeError, ValueError):
        value = default
    return max(min_value, min(max_value, value))


def save_global_settings(queue_concurrency=None, done_tasks_max=None, done_gallery_max=None):
    """保存并立即应用工作台层面的全局设置。"""
    global QUEUE_CONCURRENCY, DONE_TASKS_MAX, DONE_GALLERY_MAX

    # 某些旧浏览器标签页在 Gradio 重连时可能发送一个没有表单值的陈旧事件。
    # 忽略它，避免无意义的错误日志或意外覆盖已保存的设置。
    if queue_concurrency is None and done_tasks_max is None and done_gallery_max is None:
        return "当前全局设置未变更。"

    concurrency = _setting_int(
        queue_concurrency, QUEUE_CONCURRENCY, 1, MAX_MEDIA_QUEUE_CONCURRENCY
    )
    task_limit = _setting_int(done_tasks_max, DONE_TASKS_MAX, 20, 500)
    gallery_limit = _setting_int(done_gallery_max, DONE_GALLERY_MAX, 24, 100)
    try:
        save_config({
            "queue_concurrency": concurrency,
            "done_tasks_max": task_limit,
            "done_gallery_max": gallery_limit,
        })
    except OSError as exc:
        return f"❌ 保存失败，设置未应用：{exc}"

    QUEUE_CONCURRENCY = concurrency
    DONE_TASKS_MAX = task_queue.set_max_done(task_limit)
    DONE_GALLERY_MAX = gallery_limit
    target_workers, active_workers = task_queue.set_max_workers(concurrency)
    mode_note = "（MiniMax H3 模式固定单队列）" if VIDEO_ENGINE == "h3" else ""
    return (
        f"✅ 全局设置已保存并生效：并发 {target_workers}{mode_note}（当前 worker {active_workers}），"
        f"保留任务 {DONE_TASKS_MAX} 条，画廊显示 {DONE_GALLERY_MAX} 个产物。"
    )


def show_global_settings():
    return gr.update(visible=True)


def hide_global_settings():
    return gr.update(visible=False)


def change_lan_access_password(current_password, new_password, confirm_password):
    """经受限 root helper 修改 Nginx Basic Auth 密码，绝不将密码放进命令行参数。"""
    current_password = current_password or ""
    new_password = new_password or ""
    confirm_password = confirm_password or ""

    if not current_password:
        return "", "", "", "❌ 请输入当前局域网访问密码。"
    if len(new_password) < 8:
        return "", "", "", "❌ 新密码至少需要 8 个字符。"
    if new_password != confirm_password:
        return "", "", "", "❌ 两次输入的新密码不一致。"
    if any("\n" in value or "\r" in value for value in (current_password, new_password)):
        return "", "", "", "❌ 密码不能包含换行符。"

    try:
        result = subprocess.run(
            ["sudo", "-n", LAN_PASSWORD_HELPER],
            input=f"{current_password}\n{new_password}\n",
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        )
    except FileNotFoundError:
        return "", "", "", "❌ 密码修改组件尚未安装，请联系管理员部署。"
    except subprocess.TimeoutExpired:
        return "", "", "", "❌ 密码修改超时，未确认是否生效；请勿重复提交并联系管理员检查。"
    except OSError as exc:
        return "", "", "", f"❌ 无法执行密码修改：{exc}"

    if result.returncode != 0:
        # 不显示 helper 的详细输出，避免将认证细节暴露在工作台页面。
        return "", "", "", "❌ 当前密码不正确，或密码修改服务暂不可用。"

    return (
        "", "", "",
        "✅ 局域网访问密码已更新。请在当前浏览器刷新后，使用新密码重新登录。",
    )


def submit(wfname, args):
    """入队并返回供 LAN API/自动化跟踪的安全任务标识。"""
    try:
        usage = shutil.disk_usage(OUTPUT_DIR)
        free_percent = usage.free * 100 / usage.total
    except OSError as exc:
        raise gr.Error(f"无法检查产物磁盘空间：{exc}")
    if free_percent < MIN_FREE_DISK_PERCENT:
        raise gr.Error(
            f"磁盘可用空间仅 {free_percent:.1f}%，低于保护阈值 "
            f"{MIN_FREE_DISK_PERCENT:.1f}%。请先归档或清理历史素材后再提交。"
        )
    _name = WORKFLOW_BUILDERS[wfname][1]
    task = Task(id=uuid.uuid4().hex, name=make_task_name(_name), workflow_name=wfname, args=args)
    position = task_queue.enqueue(task)
    gr.Info(f"已提交：{task.name}（提交时排队位置 {position}）", duration=3)
    return {
        "task_id": task.id,
        "task_name": task.name,
        "workflow": task.workflow_name,
        "state": "accepted",
        "queue_position": position,
    }


def clear_pending():
    return f"已清空排队任务 {task_queue.clear_pending()} 个。"


def api_task_status(task_id: str) -> dict:
    """LAN API: obtain one workspace task's state without exposing file paths."""
    return task_queue.task_status(task_id)


def interrupt_running_tasks():
    """确认 ComfyUI 已接收中断后，再标记本工作台对应任务。"""
    _, running, _ = task_queue.snapshot()
    if not running:
        return "当前没有工作台运行任务；未向 ComfyUI 发送中断信号。"

    accepted, signal_result = interrupt()
    if not accepted:
        return (
            f"❌ {signal_result} 当前工作台任务仍保持原状态；"
            "请检查 ComfyUI 服务后再尝试中断。"
        )

    # 只标记用户点击按钮时已经在运行的任务，避免在请求发送后刚被 worker
    # 取走的新任务被错误写成已中断。
    running = task_queue.cancel_running({task.id for task in running})
    names = "、".join(task.name for task in running[:3])
    if len(running) > 3:
        names += f" 等 {len(running)} 个任务"
    return (
        f"已请求中断 {len(running)} 个运行任务（{names}）。{signal_result} "
        "任务会在下一次 ComfyUI 状态轮询时记录为已中断；已进入 ComfyUI 队列的其他请求请在 ComfyUI 侧确认。"
    )


def _fmt_ts(ts: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(ts))


def _fmt_duration(seconds: float) -> str:
    """把秒数格式化成易读的耗时字符串,如 45秒 / 1分23秒 / 1时02分03秒。"""
    total = int(round(seconds))
    if total < 0:
        return "-"
    if total < 60:
        return f"{total}秒"
    minutes, secs = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}分{secs}秒"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}时{minutes:02d}分{secs:02d}秒"


# 不同状态在表格里用不同的圆点图标标识。
STATUS_ICONS = {
    TaskStatus.PENDING: "⚪",
    TaskStatus.RUNNING: "🔵",
    TaskStatus.DONE:    "🟢",
    TaskStatus.CANCELLED: "⚫",
    TaskStatus.TIMEOUT: "🟠",
    TaskStatus.ERROR:   "🔴",
}


# 任务阶段来自 ComfyUI WebSocket、IndexTTS 服务与队列本身。中文化展示在
# 前端完成，持久化仍使用稳定的机器可读 stage 值，避免影响 REST 查询和恢复。
STAGE_LABELS = {
    "queued": "等待队列",
    "recovering": "正在重新附着任务",
    "building_workflow": "正在构建工作流",
    "submitted": "已提交至 ComfyUI",
    "execution_start": "开始执行",
    "executing": "正在执行节点",
    "finalizing": "正在完成节点",
    "loading_diffusion_model": "正在加载视频模型",
    "loading_text_encoder": "正在加载文本编码器",
    "encoding_prompt": "正在编码提示词",
    "sampling": "正在采样生成",
    "decoding_video": "正在解码视频",
    "decoding_audio": "正在解码音频",
    "muxing": "正在合成音视频",
    "checking_indextts25": "正在检查语音服务",
    "synthesizing_indextts25": "正在合成语音",
    "encoding_mp3": "正在编码 MP3",
    "saving_artifacts": "正在保存素材",
    "completed": "已完成",
    "timed_out": "等待超时",
    "interrupted": "已中断",
    "failed": "执行失败",
}


def _stage_label(stage: str | None) -> str:
    """Return a user-facing stage label without exposing raw backend values."""
    value = str(stage or "").strip()
    if not value:
        return "正在准备"
    return STAGE_LABELS.get(value, value.replace("_", " "))


def _progress_ratio(task: Task) -> float | None:
    """Return a clamped task ratio when ComfyUI has reported one."""
    if task.progress is None:
        return None
    try:
        return max(0.0, min(1.0, float(task.progress)))
    except (TypeError, ValueError):
        return None


def _task_progress_text(task: Task, queue_position: int | None = None) -> str:
    """Compact progress text for the queue table and API-independent UI."""
    if task.status == TaskStatus.PENDING:
        return f"排队第 {queue_position} 位" if queue_position else "等待队列"
    if task.status != TaskStatus.RUNNING:
        return _stage_label(task.stage)

    stage = _stage_label(task.stage)
    ratio = _progress_ratio(task)
    steps = ""
    if task.current_step is not None and task.total_steps:
        steps = f"{task.current_step}/{task.total_steps} 步"
    if ratio is not None:
        percent = f"{ratio * 100:.0f}%"
        return " · ".join(part for part in (stage, steps, percent) if part)
    return " · ".join(part for part in (stage, steps or "等待 ComfyUI 上报进度") if part)


def _render_live_progress(running: list[Task]) -> str:
    """Render live cards for running tasks; values are refreshed by gr.Timer."""
    if not running:
        return ""

    cards = []
    now = time.time()
    for task in sorted(running, key=lambda item: item.start_ts or item.submit_ts):
        ratio = _progress_ratio(task)
        percent = f"{ratio * 100:.0f}%" if ratio is not None else "进行中"
        if task.current_step is not None and task.total_steps:
            step_detail = f"采样 {escape(str(task.current_step))}/{escape(str(task.total_steps))} 步"
        elif task.node_id:
            step_detail = f"节点 {escape(str(task.node_id))}"
        else:
            step_detail = "等待 ComfyUI 上报节点进度"
        elapsed = _fmt_duration(now - (task.start_ts or task.submit_ts))
        updated = ""
        if task.last_progress_ts:
            updated = f" · 最近更新 {_fmt_duration(max(0, now - task.last_progress_ts))}前"
        if ratio is None:
            fill = '<div class="brm-live-progress-fill is-indeterminate"></div>'
            aria_value = ""
        else:
            fill = f'<div class="brm-live-progress-fill" style="width:{ratio * 100:.2f}%"></div>'
            aria_value = f' aria-valuenow="{ratio * 100:.2f}"'
        cards.append(
            '<article class="brm-live-progress-card">'
            '<div class="brm-live-progress-heading">'
            f'<span class="brm-live-progress-name" title="{escape(task.name)}">{escape(task.name)}</span>'
            f'<span class="brm-live-progress-percent">{percent}</span>'
            '</div>'
            f'<div class="brm-live-progress-detail">{escape(_stage_label(task.stage))} · {step_detail} · 已运行 {elapsed}{updated}</div>'
            f'<div class="brm-live-progress-track" role="progressbar" aria-label="{escape(task.name)} 生成进度" aria-valuemin="0" aria-valuemax="100"{aria_value}>'
            f'{fill}</div></article>'
        )
    return '<section class="brm-live-progress-list" aria-live="polite">' + "".join(cards) + '</section>'


def _render_queue_summary(*, backend: str, pending: int, running: int, completed: int,
                          cancelled: int, timed_out: int, failed: int) -> str:
    """Render the compact, scan-friendly queue status strip."""
    state_class = "is-online" if backend == "在线" else "is-offline"
    metrics = (
        ("并发", QUEUE_CONCURRENCY, ""),
        ("排队", pending, "is-active" if pending else ""),
        ("处理中", running, "is-active" if running else ""),
        ("完成", completed, "is-success" if completed else ""),
        ("中断", cancelled, ""),
        ("超时", timed_out, "is-warning" if timed_out else ""),
        ("失败", failed, "is-danger" if failed else ""),
    )
    pills = "".join(
        f'<span class="brm-queue-metric {css_class}"><b>{label}</b><strong>{value}</strong></span>'
        for label, value, css_class in metrics
    )
    return (
        '<section class="brm-queue-summary" aria-label="任务队列状态">'
        f'<span class="brm-queue-health {state_class}"><i></i>ComfyUI {backend}</span>'
        f'<div class="brm-queue-metrics">{pills}</div>'
        '</section>'
    )


def _md_cell(text: str) -> str:
    """转义 Markdown 表格单元格里的特殊字符,并把换行压成空格。"""
    return (text or "").replace("|", "\\|").replace("\n", " ").replace("\r", " ").strip()


def _completed_output_path(value) -> Path | None:
    """只允许任务历史中位于输出目录的文件进入预览或播放器。"""
    return persisted_output_path(value)


def open_completed_media_viewer(gallery_paths, evt: gr.SelectData):
    """由缩略图选择事件打开覆盖浏览器主体的图片/视频查看器。"""
    try:
        index = evt.index[0] if isinstance(evt.index, (tuple, list)) else int(evt.index)
        path = _completed_output_path(gallery_paths[index])
    except (IndexError, TypeError, ValueError, AttributeError):
        path = None

    if path is None:
        return gr.update(value="", visible=False), gr.update(visible=False)

    url = f"/gradio_api/file={quote(str(path), safe='/')}"
    filename = escape(path.name)
    if path.suffix.lower() in {".mp4", ".webm", ".mov", ".mkv", ".avi"}:
        media = f'<video controls autoplay playsinline src="{url}"></video>'
    else:
        media = f'<img src="{url}" alt="{filename}">'
    viewer_html = (
        '<div class="brm-media-viewer-content">'
        '<div class="brm-media-viewer-toolbar">'
        f'<span class="brm-media-viewer-name">{filename}</span>'
        f'<a class="brm-media-download" href="{url}" download>下载素材</a>'
        '</div>'
        f'<div class="brm-media-viewer-stage">{media}</div>'
        '</div>'
    )
    return gr.update(value=viewer_html, visible=True), gr.update(visible=True)


def close_completed_media_viewer():
    return gr.update(value="", visible=False), gr.update(visible=False)


def play_completed_audio(path):
    """点击完成音频清单中的文件名后，直接把它送入内置播放器。"""
    path = _completed_output_path(path)
    return str(path) if path and path.suffix.lower() in AUDIO_EXTS else None


def clear_completed_audio_preview():
    """停止并移除当前试听音频，避免旧音频持续占据播放器。"""
    return gr.update(value=None), None


def render_queue():
    """Render the summary, live execution cards, queue table, and artifacts."""
    pending, running, done = task_queue.snapshot()

    def note(t: Task) -> str:
        if t.status == TaskStatus.CANCELLED:
            return t.error or "用户请求中断"
        if t.status == TaskStatus.TIMEOUT:
            return t.error or "等待超时，已请求中断 ComfyUI"
        if t.status == TaskStatus.ERROR:
            return t.error
        if t.status == TaskStatus.DONE and isinstance(t.result, list):
            return f"产出 {len(t.result)} 个文件"
        return ""

    def cost(t: Task) -> str:
        # 只有已完成的任务显示耗时,其余一律用短横代替。
        if t.status in (TaskStatus.DONE, TaskStatus.CANCELLED, TaskStatus.TIMEOUT, TaskStatus.ERROR) and t.start_ts and t.done_ts:
            return _fmt_duration(t.done_ts - t.start_ts)
        return "-"

    # 汇总处理中 / 排队中 / 已完成的所有任务,按提交时间倒序(最新的在最前面)。
    all_tasks = list(pending) + list(running) + list(done)
    all_tasks.sort(key=lambda t: t.submit_ts, reverse=True)

    pending_positions = {task.id: index for index, task in enumerate(pending, start=1)}
    rows = [
        "| 任务名称 | 状态 | 实时进度 | 提交时间 | 耗时 | 备注 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for t in all_tasks:
        icon = STATUS_ICONS.get(t.status, "")
        rows.append(
            f"| {_md_cell(t.name)} "
            f"| {icon} {t.status.value} "
            f"| {_md_cell(_task_progress_text(t, pending_positions.get(t.id)))} "
            f"| {_fmt_ts(t.submit_ts)} "
            f"| {cost(t)} "
            f"| {_md_cell(note(t))} |"
        )
    if len(rows) == 2:      # 只有表头,说明暂无任务
        rows.append("| 暂无任务 |  |  |  |  |  |")
    table_md = "\n".join(rows)

    ok = sum(1 for t in done if t.status == TaskStatus.DONE)
    cancelled = sum(1 for t in done if t.status == TaskStatus.CANCELLED)
    timed_out = sum(1 for t in done if t.status == TaskStatus.TIMEOUT)
    err = sum(1 for t in done if t.status == TaskStatus.ERROR)
    backend = "在线" if is_alive() else "离线"
    summary = _render_queue_summary(
        backend=backend,
        pending=len(pending),
        running=len(running),
        completed=ok,
        cancelled=cancelled,
        timed_out=timed_out,
        failed=err,
    )

    # 图片与视频保持紧凑缩略图，点击后交给独立大预览；音频则以可点击列表直接送入播放器。
    imgs = []
    audios = []
    for t in done:
        if isinstance(t.result, list):
            for p in t.result:
                path = _completed_output_path(p)
                if path is None:
                    continue
                suffix = path.suffix.lower()
                if suffix in GALLERY_EXTS:
                    imgs.append(str(path))
                elif suffix in AUDIO_EXTS:
                    audios.append(str(path))
    gallery_paths = imgs[:DONE_GALLERY_MAX]
    audio_paths = audios[:DONE_TASKS_MAX]
    audio_choices = [(Path(path).name, path) for path in audio_paths]
    return (
        summary,
        gr.update(value=_render_live_progress(running), visible=bool(running)),
        table_md,
        gallery_paths,
        gr.update(choices=audio_choices),
        gallery_paths,
    )


output_size = [
    "512 × 512", "768 × 768", "1024 × 1024", "2048 × 2048",
    "1024 × 768", "768 × 1024",
    "1344 × 768", "768 × 1344",
    "1920 × 1080", "1080 × 1920",
    "2048 × 1536", "1536 × 2048",
    "2560 × 1440", "1440 × 2560",
    "3840 × 2160", "2160 × 3840",
]

# H3 does not use the selected pixel dimensions as a literal output size: the
# selected profile, requested duration and 32-pixel model grid determine the
# actual canvas.  Keep only one representative per supported aspect ratio so
# the UI cannot suggest nonexistent 2K/4K output options.
H3_VIDEO_ASPECT_CHOICES = [
    "1024 × 1024",  # 1:1
    "1024 × 768",   # 4:3
    "768 × 1024",   # 3:4
    "1344 × 768",   # 16:9 (also the short turbo_fast native cell)
    "768 × 1344",   # 9:16
]

def on_ref_upload(filepath):
    if not filepath:
        return ""
    name = upload_image(filepath)
    gr.Info(f"已上传{name}", duration=2)
    return name

def on_audio_upload(filepath):
    if not filepath:
        return "", 0.0
    name = upload_image(filepath)            # 复用上传:LoadAudio 也走 /upload/image
    dur  = audio_duration(filepath)
    return name, dur


def _qwen_display_text(reasoning: str, answer: str) -> str:
    """将 Qwen 的可选推理片段与最终回答组合成适合流式文本框显示的内容。"""
    parts = []
    if reasoning:
        parts.append("【模型推理】\n" + reasoning)
    if answer:
        parts.append("【回答】\n" + answer)
    return "\n\n".join(parts) or "正在等待模型输出……"


def stream_qwen_answer(question, system_prompt, temperature, max_tokens, enable_thinking):
    """通过 WSL 内部 vLLM OpenAI 兼容接口流式返回 Qwen 回答。"""
    question = (question or "").strip()
    if not question:
        yield "请输入问题后再发送。"
        return

    messages = []
    if (system_prompt or "").strip():
        messages.append({"role": "system", "content": system_prompt.strip()})
    messages.append({"role": "user", "content": question})
    payload = {
        "model": QWEN_MODEL,
        "messages": messages,
        "temperature": float(temperature),
        "max_tokens": int(max_tokens),
        "stream": True,
        # Qwen 默认会先输出较长的 reasoning。普通问答关闭它，把输出额度
        # 留给正文；需要观察推理流时，用户可在页面中显式开启。
        "chat_template_kwargs": {"enable_thinking": bool(enable_thinking)},
    }

    reasoning_parts = []
    answer_parts = []
    last_emit = 0.0
    finish_reason = None
    try:
        with requests.post(
            f"{QWEN_API_BASE}/chat/completions",
            json=payload,
            stream=True,
            timeout=(8, 30),
        ) as response:
            response.raise_for_status()
            for raw_line in response.iter_lines(decode_unicode=True):
                if not raw_line or not raw_line.startswith("data:"):
                    continue
                data = raw_line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                    choice = chunk.get("choices", [{}])[0]
                    delta = choice.get("delta", {})
                    if choice.get("finish_reason"):
                        finish_reason = choice["finish_reason"]
                except (json.JSONDecodeError, IndexError, TypeError):
                    continue

                # 当前 vLLM 的 Qwen3.5 reasoning parser 使用 `reasoning`；
                # 也兼容其他 OpenAI 兼容服务常见的 `reasoning_content` 字段。
                reasoning = delta.get("reasoning") or delta.get("reasoning_content") or ""
                content = delta.get("content") or ""
                if reasoning:
                    reasoning_parts.append(reasoning)
                if content:
                    answer_parts.append(content)

                now = time.monotonic()
                if (content or (enable_thinking and reasoning)) and now - last_emit >= 0.08:
                    last_emit = now
                    yield _qwen_display_text(
                        "".join(reasoning_parts) if enable_thinking else "",
                        "".join(answer_parts),
                    )
    except requests.ReadTimeout:
        yield "⚠️ Qwen 已连续 30 秒没有返回内容，连接已停止。请重试或缩短问题。"
        return
    except requests.RequestException as exc:
        yield (
            f"❌ 无法连接 Qwen 服务：{exc}\n\n"
            "请确认当前为常规模式，且 `qwen-vllm` 服务处于运行状态。"
        )
        return

    final_text = _qwen_display_text(
        "".join(reasoning_parts) if enable_thinking else "",
        "".join(answer_parts),
    )
    if not reasoning_parts and not answer_parts:
        final_text = "⚠️ Qwen 服务已响应，但未返回可显示的文本。"
    elif not answer_parts and reasoning_parts and not enable_thinking:
        final_text = "⚠️ 模型未返回最终回答。请重试，或降低问题长度后再试。"
    elif finish_reason == "length":
        final_text += (
            "\n\n⚠️ 已达到最大输出 Token，回答可能未完成。"
            "请提高“最大输出 Token”，或将长文拆分为多次生成。"
        )
    yield final_text


def clear_qwen_chat():
    return "", ""


# Gradio 6 会动态把次级 Tab 放入 overflow 菜单。这个初始化脚本只负责
# 将原生按钮按四个产品分区整理并保持一级导航选中态，不改变任何 API、
# 事件函数或任务语义。
BRM_NAV_JS = r"""
(() => {
  const groups = {
    "文生图Z-Image": "image",
    "图片编辑FLUX.2-klein": "image",
    "MiniMax H3 文生视频": "video",
    "MiniMax H3 图生视频": "video",
    "文生视频 LTX2.3": "video",
    "图生视频 LTX2.3": "video",
    "首尾帧视频LTX2.3": "video",
    "数字人-语音驱动LTX2.3": "video",
    "语音克隆 IndexTTS-2.5": "audio",
    "语音克隆 IndexTTS-2（回退）": "audio",
    "音乐生成ACE-Step 1.5": "audio",
    "Qwen 大模型": "tools"
  };
  const firstTabs = {
    image: "文生图Z-Image",
    video: "MiniMax H3 文生视频",
    audio: "语音克隆 IndexTTS-2.5",
    tools: "Qwen 大模型"
  };
  const boot = () => {
    const root = document.querySelector("#workflow-tabs");
    const nav = document.querySelector("#primary-nav");
    if (!root || !nav) {
      window.setTimeout(boot, 80);
      return;
    }
    if (window.__brmNavigationReady) return;

    const labelOf = (button) => (button?.textContent || "").trim();
    const workflowButtons = () => Array.from(root.querySelectorAll(
      '.tab-container[role="tablist"] button, .overflow-dropdown button'
    ));
    const showGroup = (group) => {
      nav.dataset.active = group;
      document.documentElement.dataset.brmSection = group;
      workflowButtons().forEach((button) => {
        const buttonGroup = groups[labelOf(button)];
        button.style.display = buttonGroup === group ? "inline-flex" : "none";
      });
    };
    const syncFromSelected = () => {
      const selected = root.querySelector('[role="tab"][aria-selected="true"]');
      showGroup(groups[labelOf(selected)] || nav.dataset.active || "image");
    };

    window.__brmSelectCategory = (group) => {
      showGroup(group);
      const targetLabel = firstTabs[group];
      const target = workflowButtons().find((button) => labelOf(button) === targetLabel);
      if (target && target.getAttribute("aria-selected") !== "true") target.click();
      window.setTimeout(syncFromSelected, 60);
    };
    root.addEventListener("click", () => window.setTimeout(syncFromSelected, 60));
    new MutationObserver(syncFromSelected).observe(root, {
      childList: true,
      subtree: true,
      attributes: true,
      attributeFilter: ["aria-selected", "class"]
    });
    window.__brmNavigationReady = true;
    syncFromSelected();
  };
  boot();
})();
"""

def build_ui():
    with gr.Blocks(
        title="BRM AI 工作台",
        css=CUSTOM_CSS,
        theme=gr.themes.Soft(primary_hue="teal", secondary_hue="slate", neutral_hue="slate"),
    ) as demo:
        with gr.Row(elem_id="global-toolbar", equal_height=True):
            with gr.Column(scale=2, min_width=260):
                gr.HTML(
                    '<div class="brm-brand-title">BRM AI 工作台</div>'
                    '<div class="brm-brand-subtitle">本地 AI 媒体生成、任务编排与素材管理</div>',
                    elem_id="brand-lockup",
                )
            # 一级分区与品牌同处顶栏；按钮只触发原生 Tab，不改变 API。
            with gr.Column(scale=5, min_width=560, elem_id="primary-nav-wrap"):
                with gr.Row(elem_id="primary-nav", equal_height=True):
                    with gr.Column(scale=1, min_width=120):
                        nav_image_btn = gr.Button("图像创作", elem_id="nav-image")
                    with gr.Column(scale=1, min_width=120):
                        nav_video_btn = gr.Button("视频创作", elem_id="nav-video")
                    with gr.Column(scale=1, min_width=120):
                        nav_audio_btn = gr.Button("音频创作", elem_id="nav-audio")
                    with gr.Column(scale=1, min_width=120):
                        nav_tools_btn = gr.Button("智能工具", elem_id="nav-tools")
            with gr.Column(scale=1, min_width=140):
                settings_btn = gr.Button("全局设置", variant="secondary")

        with gr.Column(visible=False, elem_id="global-settings-panel") as settings_panel:
            with gr.Row(equal_height=True):
                with gr.Column(scale=10):
                    gr.Markdown(
                        "### 全局设置\n"
                        "并发会立即调整；下调时，已在处理的任务会自然完成后再收缩。"
                        + (
                            "MiniMax H3 当前启用：为避免 A5000 显存争用，媒体队列固定为 **1**。"
                            if VIDEO_ENGINE == "h3"
                            else "视频、数字人等高显存任务通常建议保持并发 **1**。"
                        )
                    )
                with gr.Column(scale=1, min_width=116):
                    settings_close_top_btn = gr.Button(
                        "关闭", variant="secondary", elem_id="global-settings-close",
                    )
            with gr.Row():
                # Gradio 6 rejects a degenerate Slider range.  In H3 mode the
                # policy deliberately caps media concurrency at one, so keep
                # the control visibly disabled while giving the component a
                # valid (unused) upper bound.
                concurrency_slider_max = max(2, MAX_MEDIA_QUEUE_CONCURRENCY)
                setting_concurrency = gr.Slider(
                    1, concurrency_slider_max, value=QUEUE_CONCURRENCY,
                    step=1, precision=0, interactive=MAX_MEDIA_QUEUE_CONCURRENCY > 1,
                    label="任务并发数",
                )
                setting_done_tasks = gr.Slider(
                    20, 500, value=DONE_TASKS_MAX, step=10, precision=0,
                    label="已完成任务保留数",
                )
                setting_done_gallery = gr.Slider(
                    24, 100, value=DONE_GALLERY_MAX, step=5, precision=0,
                    label="画廊最多显示产物数",
                )
            with gr.Row():
                save_settings_btn = gr.Button("保存并应用", variant="primary")
                close_settings_btn = gr.Button("关闭", variant="secondary")
            settings_status = gr.Markdown("")
            gr.Markdown("---\n#### 局域网访问密码")
            gr.Markdown(
                "修改的是进入 AI 工作台与 `/qwen/v1` API 的 Basic Auth 密码。"
                "修改后当前浏览器需要用新密码重新登录。"
            )
            lan_current_password = gr.Textbox(
                label="当前访问密码", type="password", max_length=128,
            )
            with gr.Row():
                lan_new_password = gr.Textbox(
                    label="新访问密码", type="password", max_length=128,
                )
                lan_confirm_password = gr.Textbox(
                    label="确认新访问密码", type="password", max_length=128,
                )
            change_lan_password_btn = gr.Button("修改局域网访问密码", variant="secondary")
            lan_password_status = gr.Markdown("")

        # ---- 每个工作流仍是原生 Gradio Tab；CSS/初始化脚本只负责分区呈现。 ----
        with gr.Tabs(elem_id="workflow-tabs"):
            # ========== Tab 1 ==========
            with gr.Tab("文生图Z-Image"):
                gr.Markdown("## 文生图 Z-Image", elem_classes=["workflow-heading"])
                with gr.Row():
                    with gr.Column(scale=1):
                        prompt1 = gr.Textbox(label="提示词", autofocus=True, value="一个漂亮的女生在校园散步", lines=3)
                    with gr.Column(scale=1):
                        with gr.Row():
                            size1 = gr.Dropdown(label="图片尺寸", choices=output_size, value="1024 × 1024")
                            batch1 = gr.Number(value=1, label="图片数量", minimum=1, maximum=4, precision=0)
                        submit_btn1 = gr.Button("提交", variant="primary")
                submit_btn1.click(
                    fn=submit_workflow_1,
                    inputs=[prompt1, size1, batch1],
                    api_name="ui_submit_workflow_1",
                    api_visibility="private",
                )

            # ========== Tab 2 ==========
            with gr.Tab("图片编辑FLUX.2-klein"):
                gr.Markdown("## 图片编辑 FLUX.2-klein", elem_classes=["workflow-heading"])
                with gr.Row(equal_height=True):
                    with gr.Column(scale=1):
                        prompt2 = gr.Textbox(label="提示词", autofocus=True, placeholder="输入图片提示词", lines=13, max_lines=13)
                    with gr.Column(scale=1):
                        reference_image2 = gr.Image(type="filepath", height=400)   # 关键:拿到磁盘路径才能上传
                        uploaded_name2   = gr.State("")                # 存上传后 ComfyUI 给的文件名
                        submit_btn2 = gr.Button("提交", variant="primary")
                reference_image2.upload(fn=on_ref_upload, inputs=reference_image2, outputs=uploaded_name2,
                                         api_visibility="private")
                reference_image2.clear(fn=lambda: "", outputs=uploaded_name2,
                                       api_visibility="private")   # 清空时一并清掉
                submit_btn2.click(
                    fn=submit_workflow_2,
                    inputs=[prompt2, uploaded_name2],
                    api_name="ui_submit_workflow_2",
                    api_visibility="private",
                )

            # ========== Tab 3 ==========
            with gr.Tab(PRIMARY_T2V_TAB_LABEL):
                gr.Markdown(f"## {PRIMARY_T2V_TAB_LABEL}", elem_classes=["workflow-heading"])
                with gr.Row():
                    with gr.Column(scale=1):
                        prompt3 = gr.Textbox(label="提示词", autofocus=True, value="一个漂亮的亚洲女孩在在花丛中散步", lines=3)
                    with gr.Column(scale=1):
                        with gr.Row():
                            size3 = gr.Dropdown(
                                label="视频画幅",
                                choices=H3_VIDEO_ASPECT_CHOICES,
                                value="768 × 1024",
                                info="仅选择画幅比例；实际分辨率由档位、时长和 H3 网格决定。",
                            )
                            seconds3 = gr.Number(
                                value=PRIMARY_VIDEO_SECONDS_DEFAULT,
                                label="视频时长（秒，4–15）" if H3_ENABLED else "视频时长（秒）",
                                minimum=PRIMARY_VIDEO_SECONDS_MIN,
                                maximum=PRIMARY_VIDEO_SECONDS_MAX,
                                precision=0,
                                info="所有模式支持最长 15 秒；15 秒高画幅会自动下调实际画布，Turbo 7–15 秒采用稳健 attention；draft 固定 3 秒。" if H3_ENABLED else None,
                            )
                            profile3 = gr.Dropdown(
                                label="生成档位", choices=list(H3_PROFILES), value="preview",
                                info="draft：0.4MP / 73 帧草稿；preview：480 短边；quality：768 短边。实际时长会按 H3 帧网格调整。",
                                visible=H3_ENABLED,
                            )
                            acceleration3 = gr.Dropdown(
                                label="加速模式",
                                choices=[
                                    (config["label"], key)
                                    for key, config in H3_ACCELERATION_MODES.items()
                                ],
                                value="standard",
                                info="所有模式支持最长 15 秒；Turbo 7–15 秒自动绕过长序列下不稳定的 Sage 融合核。4 步极速在长时会切换到兼容的 8 步 Turbo 路径。",
                                visible=H3_ENABLED,
                            )
                        submit_btn3 = gr.Button("提交", variant="primary")
                submit_btn3.click(
                    fn=submit_workflow_3,
                    inputs=[prompt3, size3, seconds3, profile3, acceleration3],
                    api_name="ui_submit_workflow_3",
                    api_visibility="private",
                )
                profile3.change(
                    fn=h3_profile_duration_update,
                    inputs=[profile3, acceleration3],
                    outputs=seconds3,
                    api_visibility="private",
                )
                acceleration3.change(
                    fn=h3_profile_duration_update,
                    inputs=[profile3, acceleration3],
                    outputs=seconds3,
                    api_visibility="private",
                )

            # ========== Tab 4 ==========
            with gr.Tab(PRIMARY_I2V_TAB_LABEL):
                gr.Markdown(f"## {PRIMARY_I2V_TAB_LABEL}", elem_classes=["workflow-heading"])
                with gr.Row(equal_height=True):
                    with gr.Column(scale=1):
                        prompt4 = gr.Textbox(label="提示词", autofocus=True, placeholder="输入提示词", lines=10, max_lines=10)
                        with gr.Row():
                            size4 = gr.Dropdown(label="输出画幅", choices=H3_VIDEO_ASPECT_CHOICES, value="768 × 1024",
                                                info="仅选择画幅比例；实际分辨率由档位、时长和 H3 网格决定。")
                            seconds4 = gr.Number(
                                value=PRIMARY_VIDEO_SECONDS_DEFAULT,
                                label="视频时长（秒，4–15）" if H3_ENABLED else "视频时长（秒）",
                                minimum=PRIMARY_VIDEO_SECONDS_MIN,
                                maximum=PRIMARY_VIDEO_SECONDS_MAX,
                                precision=0,
                                info="所有模式支持最长 15 秒；15 秒高画幅会自动下调实际画布，Turbo 7–15 秒采用稳健 attention；draft 固定 3 秒。" if H3_ENABLED else None,
                            )
                        profile4 = gr.Dropdown(
                            label="生成档位", choices=list(H3_PROFILES), value="preview",
                            info="draft：0.4MP / 73 帧草稿；preview：480 短边；quality：768 短边。H3 会将源图适配至所选画幅，并生成同步立体声音频。",
                            visible=H3_ENABLED,
                        )
                        acceleration4 = gr.Dropdown(
                            label="加速模式",
                            choices=[
                                (config["label"], key)
                                for key, config in H3_ACCELERATION_MODES.items()
                            ],
                            value="standard",
                            info="所有模式支持最长 15 秒；Turbo 7–15 秒自动使用稳健 attention，4 步极速会转兼容的 8 步 Turbo。短时 4 步仍要求 quality + 16:9 横版并生成 1344 × 768。",
                            visible=H3_ENABLED,
                        )
                    with gr.Column(scale=1):
                        reference_image4 = gr.Image(type="filepath", height=400)   # 关键:拿到磁盘路径才能上传
                        uploaded_name4   = gr.State("")                # 存上传后 ComfyUI 给的文件名
                        submit_btn4 = gr.Button("提交", variant="primary")
                reference_image4.upload(fn=on_ref_upload, inputs=reference_image4, outputs=uploaded_name4,
                                         api_visibility="private")
                reference_image4.clear(fn=lambda: "", outputs=uploaded_name4,
                                       api_visibility="private")   # 清空时一并清掉
                submit_btn4.click(
                    fn=submit_workflow_4,
                    inputs=[prompt4, uploaded_name4, seconds4, profile4, size4, acceleration4],
                    api_name="ui_submit_workflow_4",
                    api_visibility="private",
                )
                profile4.change(
                    fn=h3_profile_duration_update,
                    inputs=[profile4, acceleration4],
                    outputs=seconds4,
                    api_visibility="private",
                )
                acceleration4.change(
                    fn=h3_profile_duration_update,
                    inputs=[profile4, acceleration4],
                    outputs=seconds4,
                    api_visibility="private",
                )

            # ========== Tab 5 ==========
            # Keep the original LTX routes visible even while H3 is the
            # default engine.  They share the global media queue, so this does
            # not add A5000 concurrency or change H3's resource policy.
            with gr.Tab("文生视频 LTX2.3"):
                gr.Markdown("## 文生视频 LTX2.3", elem_classes=["workflow-heading"])
                with gr.Row():
                    with gr.Column(scale=1):
                        ltx_prompt3 = gr.Textbox(
                            label="提示词", autofocus=True,
                            value="雨后街道反射霓虹灯，电影感镜头缓慢推进", lines=3,
                        )
                    with gr.Column(scale=1):
                        with gr.Row():
                            ltx_size3 = gr.Dropdown(
                                label="视频尺寸", choices=output_size, value="768 × 1024",
                                info="LTX2.3 按此尺寸生成；与 H3 档位和加速模式互不混用。",
                            )
                            ltx_seconds3 = gr.Number(
                                value=5, label="视频时长（秒）", minimum=2, maximum=360, precision=0,
                            )
                        ltx_submit_btn3 = gr.Button("提交 LTX2.3 文生视频", variant="primary")
                ltx_submit_btn3.click(
                    fn=submit_workflow_3_ltx,
                    inputs=[ltx_prompt3, ltx_size3, ltx_seconds3],
                    api_name="ui_submit_workflow_3_ltx",
                    api_visibility="private",
                )

            # ========== Tab 6 ==========
            with gr.Tab("图生视频 LTX2.3"):
                gr.Markdown("## 图生视频 LTX2.3", elem_classes=["workflow-heading"])
                with gr.Row(equal_height=True):
                    with gr.Column(scale=1):
                        ltx_prompt4 = gr.Textbox(
                            label="提示词", autofocus=True, placeholder="输入运动与镜头提示词",
                            lines=10, max_lines=10,
                        )
                        ltx_seconds4 = gr.Number(
                            value=5, label="视频时长（秒）", minimum=2, maximum=360, precision=0,
                            info="LTX2.3 图生视频沿用源图画幅；如需控制尺寸，请预先裁剪源图。",
                        )
                    with gr.Column(scale=1):
                        ltx_reference_image4 = gr.Image(type="filepath", height=400)
                        ltx_uploaded_name4 = gr.State("")
                        ltx_submit_btn4 = gr.Button("提交 LTX2.3 图生视频", variant="primary")
                ltx_reference_image4.upload(
                    fn=on_ref_upload, inputs=ltx_reference_image4, outputs=ltx_uploaded_name4,
                    api_visibility="private",
                )
                ltx_reference_image4.clear(
                    fn=lambda: "", outputs=ltx_uploaded_name4, api_visibility="private",
                )
                ltx_submit_btn4.click(
                    fn=submit_workflow_4_ltx,
                    inputs=[ltx_prompt4, ltx_uploaded_name4, ltx_seconds4],
                    api_name="ui_submit_workflow_4_ltx",
                    api_visibility="private",
                )

            # ========== Tab 7 ==========
            with gr.Tab("首尾帧视频LTX2.3"):
                gr.Markdown("## 首尾帧视频 LTX2.3", elem_classes=["workflow-heading"])
                with gr.Row(equal_height=True):
                    with gr.Column(scale=1):
                        prompt5 = gr.Textbox(label="提示词", autofocus=True, placeholder="输入提示词", lines=3, max_lines=3)
                    with gr.Column(scale=1):
                        seconds5 = gr.Number(value=5, label="视频时长", minimum=2, maximum=360, precision=0)
                with gr.Row(equal_height=True):
                    with gr.Column(scale=1):
                        reference_image5_1 = gr.Image(type="filepath", height=400)   # 关键:拿到磁盘路径才能上传
                        uploaded_name5_1   = gr.State("")                # 存上传后 ComfyUI 给的文件名
                    with gr.Column(scale=1):
                        reference_image5_2 = gr.Image(type="filepath", height=400)   # 关键:拿到磁盘路径才能上传
                        uploaded_name5_2   = gr.State("")                # 存上传后 ComfyUI 给的文件名
                submit_btn5 = gr.Button("提交", variant="primary")

                reference_image5_1.upload(fn=on_ref_upload, inputs=reference_image5_1, outputs=uploaded_name5_1,
                                           api_visibility="private")
                reference_image5_1.clear(fn=lambda: "", outputs=uploaded_name5_1,
                                         api_visibility="private")   # 清空时一并清掉

                reference_image5_2.upload(fn=on_ref_upload, inputs=reference_image5_2, outputs=uploaded_name5_2,
                                           api_visibility="private")
                reference_image5_2.clear(fn=lambda: "", outputs=uploaded_name5_2,
                                         api_visibility="private")   # 清空时一并清掉

                submit_btn5.click(
                    fn=submit_workflow_5,
                    inputs=[prompt5, uploaded_name5_1, uploaded_name5_2, seconds5],
                    api_name="ui_submit_workflow_5",
                    api_visibility="private",
                )

            # ========== Tab 8 ==========
            with gr.Tab("数字人-语音驱动LTX2.3"):
                gr.Markdown("## 数字人 · 语音驱动 LTX2.3", elem_classes=["workflow-heading"])
                with gr.Row(equal_height=True):
                    with gr.Column(scale=1):
                        prompt6 = gr.Textbox(label="提示词", autofocus=True, placeholder="输入提示词", lines=3, max_lines=3)
                    with gr.Column(scale=1):
                        size6 = gr.Dropdown(label="视频尺寸", choices=output_size, value="768 × 1024")
                with gr.Row(equal_height=True):
                    with gr.Column(scale=1):
                        reference_audio6_1 = gr.Image(type="filepath", height=400)   # 关键:拿到磁盘路径才能上传
                        uploaded_name6_1   = gr.State("")                # 存上传后 ComfyUI 给的文件名
                    with gr.Column(scale=1):
                        reference_audio6_2 = gr.Audio(type="filepath")   # 关键:拿到磁盘路径才能上传
                        uploaded_name6_2   = gr.State("")                # 存上传后 ComfyUI 给的文件名
                        uploaded_dur   = gr.State(0.0)                # 存上传后 ComfyUI 给的文件名

                submit_btn6 = gr.Button("提交", variant="primary")

                reference_audio6_1.upload(fn=on_ref_upload, inputs=reference_audio6_1, outputs=uploaded_name6_1,
                                           api_visibility="private")
                reference_audio6_1.clear(fn=lambda: "", outputs=uploaded_name6_1,
                                         api_visibility="private")   # 清空时一并清掉

                reference_audio6_2.upload(fn=on_audio_upload, inputs=reference_audio6_2,
                                           outputs=[uploaded_name6_2, uploaded_dur], api_visibility="private")
                reference_audio6_2.clear(fn=lambda: ("", ""), outputs=[uploaded_name6_2, uploaded_dur],
                                         api_visibility="private")   # 清空时一并清掉

                submit_btn6.click(
                    fn=submit_workflow_6,
                    inputs=[prompt6, uploaded_name6_1, uploaded_name6_2, uploaded_dur, size6],
                    api_name="ui_submit_workflow_6",
                    api_visibility="private",
                )

            # ========== Tab 7 ==========
            with gr.Tab("语音克隆 IndexTTS-2.5" if VOICE_ENGINE == "indextts25" else "语音克隆 IndexTTS-2（回退）"):
                gr.Markdown(
                    "## 语音克隆 IndexTTS-2.5"
                    if VOICE_ENGINE == "indextts25"
                    else "## 语音克隆 IndexTTS-2（回退）",
                    elem_classes=["workflow-heading"],
                )
                with gr.Row(equal_height=True):
                    with gr.Column(scale=1):
                        prompt7 = gr.Textbox(label="合成文本", autofocus=True, placeholder="输入要合成的文本",
                                             value="你好，这是一段语音克隆测试。", lines=8, max_lines=12)
                    with gr.Column(scale=1):
                        reference_audio7 = gr.Audio(label="参考音频（克隆音色来源，建议 10–30 秒清晰人声）",
                                                    type="filepath")
                        uploaded_name7 = gr.State("")
                        uploaded_dur7 = gr.State(0.0)
                        if VOICE_ENGINE == "indextts25":
                            language7 = gr.Dropdown(
                                choices=[("中文", "zh"), ("English", "en"), ("日本語", "ja"),
                                         ("Español", "es"), ("العربية", "ar")],
                                value="zh", label="语言",
                            )
                            speed7 = gr.Slider(0.5, 2.0, value=1.0, step=0.05,
                                               label="原生语速（1.0 为正常）")
                            gr.Markdown("IndexTTS‑2.5 使用语言与原生语速控制；旧 API 的 `temperature` 参数仍可传入，但不会影响本次推理。")
                        else:
                            temperature7 = gr.Slider(0.1, 1.5, value=0.8, step=0.05,
                                                     label="采样温度（IndexTTS‑2 回退模式）")
                        submit_btn7 = gr.Button("提交", variant="primary")
                reference_audio7.upload(fn=on_audio_upload, inputs=reference_audio7,
                                        outputs=[uploaded_name7, uploaded_dur7], api_visibility="private")
                reference_audio7.clear(fn=lambda: ("", 0.0), outputs=[uploaded_name7, uploaded_dur7],
                                       api_visibility="private")
                if VOICE_ENGINE == "indextts25":
                    submit_btn7.click(
                        fn=ui_submit_workflow_7,
                        inputs=[prompt7, uploaded_name7, language7, speed7],
                        api_name="ui_submit_workflow_7", api_visibility="private",
                    )
                else:
                    submit_btn7.click(
                        fn=submit_workflow_7,
                        inputs=[prompt7, uploaded_name7, temperature7],
                        api_name="ui_submit_workflow_7", api_visibility="private",
                    )

            # ========== Tab 8 ==========
            with gr.Tab("音乐生成ACE-Step 1.5"):
                gr.Markdown("## 音乐生成 ACE-Step 1.5", elem_classes=["workflow-heading"])
                ace_step_models = installed_acestep_models()
                if not ace_step_models:
                    gr.Markdown(
                        "⚠️ 未检测到 ACE-Step DiT 权重，音乐生成暂不可用。"
                        "请先完成模型部署后重启后端。"
                    )
                with gr.Row(equal_height=True):
                    with gr.Column(scale=1):
                        tags8 = gr.Textbox(
                            label="音乐风格标签",
                            autofocus=True,
                            value="pop, upbeat, electronic",
                            placeholder="例如: pop, upbeat, electronic",
                            lines=3,
                            max_lines=5,
                        )
                        lyrics8 = gr.Textbox(
                            label="歌词",
                            placeholder="可留空,纯音乐直接不填",
                            lines=8,
                            max_lines=12,
                        )
                    with gr.Column(scale=1):
                        with gr.Row():
                            duration8 = gr.Number(value=30, label="时长(秒)", minimum=1, maximum=2000, precision=1)
                            bpm8 = gr.Number(value=120, label="BPM", minimum=10, maximum=300, precision=0)
                        with gr.Row():
                            language8 = gr.Dropdown(
                                label="语言",
                                choices=["zh", "en", "ja", "ko", "fr", "de", "es", "ru", "unknown"],
                                value="zh",
                            )
                            model8 = gr.Dropdown(
                                label="模型",
                                choices=ace_step_models,
                                value=ace_step_models[0] if ace_step_models else None,
                                info="仅显示服务器已安装的 ACE-Step 权重；安装新模型并重启后端后才会出现。",
                            )
                        submit_btn8 = gr.Button("提交", variant="primary")
                submit_btn8.click(
                    fn=submit_workflow_8,
                    inputs=[tags8, lyrics8, duration8, bpm8, language8, model8],
                    api_name="ui_submit_workflow_8",
                    api_visibility="private",
                )

            # ========== Tab 9 ==========
            with gr.Tab("Qwen 大模型"):
                gr.Markdown("## 智能工具 / Qwen 大模型", elem_classes=["workflow-heading"])
                with gr.Row(elem_id="qwen-workspace"):
                    with gr.Column(scale=3, elem_id="qwen-chat-panel"):
                        gr.Markdown(
                            "### Qwen 本地大模型\n"
                            "流式验证问答能力；Qwen 固定在 A4000，媒体工作流固定在 A5000。"
                        )
                        qwen_system = gr.Textbox(
                            label="系统提示词（可选）",
                            value="你是一个专业、简洁的中文助手。",
                            lines=2,
                        )
                        qwen_question = gr.Textbox(
                            label="问题",
                            placeholder="例如：用三句话解释什么是向量数据库？",
                            lines=5,
                            autofocus=True,
                        )
                        qwen_answer = gr.Textbox(
                            label="Qwen 流式回答",
                            lines=18,
                            max_lines=30,
                            interactive=False,
                            autoscroll=True,
                            buttons=["copy"],
                            elem_id="qwen-answer",
                        )
                    with gr.Column(scale=1, min_width=300, elem_id="qwen-config-panel"):
                        gr.Markdown("### 模型运行配置\nA4000 · Qwen3.5-4B-AWQ · 在线")
                        qwen_temperature = gr.Slider(
                            0, 1.5, value=0.7, step=0.1,
                            label="温度",
                        )
                        qwen_max_tokens = gr.Slider(
                            128, 3584, value=2048, step=128, precision=0,
                            label="最大输出 Token（长文建议 3072+）",
                        )
                        qwen_enable_thinking = gr.Checkbox(
                            label="启用模型推理（会占用输出字数）",
                            value=False,
                            info="普通问答默认关闭；仅在需要观察推理流时开启。",
                        )
                        qwen_send_btn = gr.Button("发送并流式回答", variant="primary")
                        qwen_stop_btn = gr.Button("停止", variant="stop")
                        qwen_clear_btn = gr.Button("清空", variant="secondary")

        # ---- 共享的任务队列面板(所有 Tab 共用一个队列与后台 worker) ----
        with gr.Column(elem_id="task-center"):
            gr.Markdown("### 任务中心", elem_id="task-center-title")
            q_summary = gr.HTML(value="", elem_id="q-summary")
            q_live_progress = gr.HTML(value="", visible=False, elem_id="q-live-progress")
            with gr.Row(equal_height=True, elem_id="task-center-body"):
                with gr.Column(scale=10):
                    q_table = gr.Markdown(elem_id="q-table-md")
                with gr.Column(scale=1, min_width=168, elem_id="queue-actions"):
                    clear_btn = gr.Button("清空排队任务")
                    interrupt_btn = gr.Button("中断当前运行任务", variant="stop")
            op_status = gr.Markdown("", elem_id="task-operation-status")

        with gr.Column(elem_id="asset-center"):
            gr.Markdown("### 素材库", elem_id="completed-media")
            with gr.Row(elem_id="audio-asset-workspace"):
                completed_audio_list = gr.Radio(
                    label="已完成音频 · 点击名称即可试听",
                    choices=[],
                    value=None,
                    interactive=True,
                    scale=1,
                    elem_id="completed-audio-list",
                )
                completed_audio_player = gr.Audio(
                    label="音频试听（播放器右上角可下载）",
                    type="filepath",
                    interactive=False,
                    buttons=["download"],
                    scale=2,
                )
            clear_audio_preview_btn = gr.Button(
                "停止并清除当前试听", variant="secondary", elem_id="audio-preview-clear",
            )
            q_gallery = gr.Gallery(
                label="已完成图片/视频（累计，最多 30 项）",
                columns=7,
                rows=3,
                height=420,
                object_fit="cover",
                allow_preview=False,
                preview=False,
                buttons=["download", "download_all"],
                elem_id="q-gallery",
            )
            completed_gallery_paths = gr.State([])
            media_viewer = gr.HTML(value="", visible=False, elem_id="media-viewer")
            media_viewer_close_btn = gr.Button(
                "关闭预览", visible=False, variant="secondary", elem_id="media-viewer-close",
            )
            gr.Markdown(
                "点击缩略图打开浏览器主体大预览；可在预览内下载，画廊工具栏可下载全部。",
                elem_id="completed-media-hint",
            )

        # 事件绑定。
        clear_btn.click(fn=clear_pending, outputs=op_status, api_visibility="private")
        interrupt_btn.click(fn=interrupt_running_tasks, outputs=op_status, api_visibility="private")
        completed_audio_list.change(
            fn=play_completed_audio,
            inputs=completed_audio_list,
            outputs=completed_audio_player,
            api_visibility="private",
        )
        clear_audio_preview_btn.click(
            fn=clear_completed_audio_preview,
            outputs=[completed_audio_list, completed_audio_player],
            api_visibility="private",
        )
        q_gallery.select(
            fn=open_completed_media_viewer,
            inputs=completed_gallery_paths,
            outputs=[media_viewer, media_viewer_close_btn],
            api_visibility="private",
        )
        media_viewer_close_btn.click(
            fn=close_completed_media_viewer,
            outputs=[media_viewer, media_viewer_close_btn],
            api_visibility="private",
        )

        # 定时刷新任务面板与存活检测。
        gr.Timer(1.5).tick(
            fn=render_queue,
            outputs=[
                q_summary,
                q_live_progress,
                q_table,
                q_gallery,
                completed_audio_list,
                completed_gallery_paths,
            ],
            api_visibility="private",
        )
        gr.Timer(3.0).tick(fn=check_health, api_visibility="private")

        # Stable LAN API surface. These wrappers accept explicit values for
        # arguments that are State-only in the browser UI (uploaded filenames
        # and audio duration), so every documented workflow can be submitted
        # from a non-browser client.
        gr.api(api_submit_workflow_1, api_name="submit_workflow_1")
        gr.api(api_submit_workflow_2, api_name="submit_workflow_2")
        gr.api(api_submit_workflow_3, api_name="submit_workflow_3")
        gr.api(api_submit_workflow_4, api_name="submit_workflow_4")
        gr.api(api_submit_workflow_3_h3, api_name="submit_workflow_3_h3")
        gr.api(api_submit_workflow_4_h3, api_name="submit_workflow_4_h3")
        gr.api(api_submit_workflow_3_ltx, api_name="submit_workflow_3_ltx")
        gr.api(api_submit_workflow_4_ltx, api_name="submit_workflow_4_ltx")
        gr.api(api_submit_workflow_5, api_name="submit_workflow_5")
        gr.api(api_submit_workflow_6, api_name="submit_workflow_6")
        gr.api(api_submit_workflow_7, api_name="submit_workflow_7")
        gr.api(api_submit_workflow_8, api_name="submit_workflow_8")
        gr.api(api_task_status, api_name="task_status")

        # 放在既有队列/API 事件之后，保持旧浏览器标签页中已有事件的编号稳定。
        nav_image_btn.click(
            fn=None,
            js='() => { window.__brmSelectCategory?.("image"); return []; }',
            api_visibility="private",
        )
        nav_video_btn.click(
            fn=None,
            js='() => { window.__brmSelectCategory?.("video"); return []; }',
            api_visibility="private",
        )
        nav_audio_btn.click(
            fn=None,
            js='() => { window.__brmSelectCategory?.("audio"); return []; }',
            api_visibility="private",
        )
        nav_tools_btn.click(
            fn=None,
            js='() => { window.__brmSelectCategory?.("tools"); return []; }',
            api_visibility="private",
        )
        settings_btn.click(fn=show_global_settings, outputs=settings_panel, api_visibility="private")
        settings_close_top_btn.click(fn=hide_global_settings, outputs=settings_panel, api_visibility="private")
        close_settings_btn.click(fn=hide_global_settings, outputs=settings_panel, api_visibility="private")
        save_settings_btn.click(
            fn=save_global_settings,
            inputs=[setting_concurrency, setting_done_tasks, setting_done_gallery],
            outputs=settings_status,
            api_visibility="private",
        )
        change_lan_password_btn.click(
            fn=change_lan_access_password,
            inputs=[lan_current_password, lan_new_password, lan_confirm_password],
            outputs=[lan_current_password, lan_new_password, lan_confirm_password, lan_password_status],
            api_visibility="private",
            concurrency_limit=1,
            concurrency_id="lan-password-change",
            show_progress="minimal",
        )
        qwen_submit_event = qwen_send_btn.click(
            fn=stream_qwen_answer,
            inputs=[qwen_question, qwen_system, qwen_temperature, qwen_max_tokens, qwen_enable_thinking],
            outputs=qwen_answer,
            api_name="qwen_chat",
            api_visibility="private",
            concurrency_limit=1,
            concurrency_id="qwen-stream",
            show_progress="minimal",
            stream_every=0.08,
        )
        qwen_stop_btn.click(
            fn=None,
            cancels=[qwen_submit_event],
            api_visibility="private",
        )
        qwen_clear_btn.click(
            fn=clear_qwen_chat,
            outputs=[qwen_question, qwen_answer],
            api_visibility="private",
        )

    return demo


def main():
    start_comfyui(wait=True)
    task_queue.start_workers()      # 启动队列后台 worker

    demo = build_ui()
    demo.queue()
    demo.launch(
        server_name=GRADIO_HOST,
        server_port=server_port,
        inbrowser=False,
        root_path=GRADIO_ROOT_PATH,
        js=BRM_NAV_JS,
    )


if __name__ == "__main__":
    main()
