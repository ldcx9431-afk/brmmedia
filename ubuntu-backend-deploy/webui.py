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

import re
import enum
import uuid
import time
import random
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
    get_view_file, interrupt, BASE, WORKFLOW_DIR, upload_image, audio_duration,
)


# ============================================================================
# 目录与常量
# ============================================================================
# 先拿到本文件所在目录,后续若需要写文件都基于它推导。
BASE_DIR = Path(__file__).resolve().parent

OUTPUT_DIR = BASE_DIR / "outputs"   # 所有产物统一保存到这里
TASK_HISTORY_PATH = OUTPUT_DIR / "task-history.json"
# 自定义全屏查看器仅允许读取任务产物目录；不会因此暴露宿主机其它路径。
gr.set_static_paths(paths=[OUTPUT_DIR])

# 历史恢复时只扫描实际可展示或可下载的媒体，避免把日志等运行文件放进任务列表。
PERSISTABLE_MEDIA_EXTS = {
    ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif",
    ".mp4", ".webm", ".mov", ".mkv", ".avi",
    ".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg",
}

DONE_GALLERY_MAX = 30       # 已完成画廊最多展示多少张
DONE_TASKS_MAX   = 200      # 已完成任务最多保留多少条(防止长时间运行后无限增长)


################################ YZY启动器配置专用 开始 ##########################################
import socket, json, os, sys
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
QUEUE_CONCURRENCY = config_int("queue_concurrency", default=1, min_value=1, max_value=4)
DONE_TASKS_MAX = config_int("done_tasks_max", default=DONE_TASKS_MAX, min_value=20, max_value=500)
DONE_GALLERY_MAX = config_int("done_gallery_max", default=DONE_GALLERY_MAX, min_value=10, max_value=100)
################################ YZY启动器配置专用 结束 ##########################################

# ============================================================================
# 通用骨架:任务对象、任务队列、存活提示(一般不用改)
# ============================================================================
class TaskStatus(str, enum.Enum):
    PENDING = "排队中"
    RUNNING = "处理中"
    DONE    = "已完成"
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
        self._pending: deque[Task] = deque()
        self._running: dict[str, Task] = {}
        self._done: list[Task] = self._load_history()
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
        }

    @staticmethod
    def _task_from_record(record: dict) -> "Task | None":
        """从落盘历史恢复已经结束的任务；丢失的产物不会显示为可下载素材。"""
        try:
            status = TaskStatus(record["status"])
            if status not in (TaskStatus.DONE, TaskStatus.ERROR):
                return None
            result = [
                str(Path(path))
                for path in record.get("result", [])
                if isinstance(path, str) and Path(path).is_file()
            ]
            if status == TaskStatus.DONE and not result:
                return None
            return Task(
                id=str(record["id"]),
                name=str(record.get("name", "历史任务")),
                workflow_name=str(record.get("workflow_name", "历史恢复")),
                args={},
                status=status,
                submit_ts=float(record.get("submit_ts", 0.0)),
                start_ts=float(record.get("start_ts", 0.0)),
                done_ts=float(record.get("done_ts", 0.0)),
                result=result,
                error=str(record.get("error", "")),
            )
        except (KeyError, TypeError, ValueError):
            return None

    def _legacy_output_history(self) -> list[Task]:
        """为旧版本已经生成、但尚无任务索引的媒体创建一次可恢复历史。"""
        try:
            media_files = [
                path for path in OUTPUT_DIR.iterdir()
                if path.is_file() and path.suffix.lower() in PERSISTABLE_MEDIA_EXTS
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
        return restored[:self._max_done]

    def _save_history(self, tasks: "list[Task] | None" = None) -> None:
        """原子保存已结束任务；写入失败不会影响正在运行的生成任务。"""
        records = [self._record_from_task(task) for task in (tasks if tasks is not None else self._done)]
        payload = {"version": 1, "tasks": records}
        try:
            OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
            temp_path = TASK_HISTORY_PATH.with_name(f".{TASK_HISTORY_PATH.name}.{os.getpid()}.tmp")
            temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            temp_path.replace(TASK_HISTORY_PATH)
        except OSError as exc:
            print(f"[Queue] 保存任务历史失败: {exc}")

    def enqueue(self, task: Task) -> None:
        """把任务追加到队尾,并唤醒 worker。"""
        with self._lock:
            task.status = TaskStatus.PENDING
            self._pending.append(task)
        self._wake.set()

    def clear_pending(self) -> int:
        """清空排队中的任务(不影响正在执行和已完成的)。"""
        with self._lock:
            n = len(self._pending)
            self._pending.clear()
            return n

    def snapshot(self):
        """取一份当前状态快照,供界面渲染。"""
        with self._lock:
            return list(self._pending), list(self._running.values()), list(self._done)

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
                    task.start_ts = time.time()
                    self._running[task.id] = task
            if task is None:
                # 队列空,等待新任务唤醒(最多等 0.5 秒再检查一次)。
                self._wake.wait(timeout=0.5)
                self._wake.clear()
                continue
            try:
                self._processor(task)
                task.status = TaskStatus.DONE
            except Exception as e:
                task.status, task.error = TaskStatus.ERROR, str(e)
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
footer {
    display: none !important;
}
.fillable {
    max-width: 1400px !important;
    margin: 0 auto !important;
    padding: 10px !important;
}
/* 任务表格:铺满宽度,高度与右侧两个按钮一致,内容超出时出现滚动条。 */
#q-table-md {
    height: 160px;                 /* 约等于右侧两个按钮的总高度,可按需微调 */
    width: 100%;
    overflow-y: auto;
    box-sizing: border-box;
    padding: 0 12px;
    border: 1px solid var(--border-color-primary, #e5e7eb);
    border-radius: var(--radius-lg, 8px);
}
#q-table-md table {
    width: 100%;                  /* 表格占满整个容器宽度 */
}
/* 独立的浏览器主体素材查看器，不再依赖 Gallery 内部的小预览区域。 */
#media-viewer {
    position: fixed !important;
    inset: 0 !important;
    z-index: 2000 !important;
    box-sizing: border-box;
    /* 预留顶部工具区和底部系统 Dock，避免竖图/竖视频被切掉。 */
    padding: 72px 5vw max(180px, env(safe-area-inset-bottom));
    overflow: hidden;
    background: rgba(15, 23, 42, 0.88);
}
#media-viewer .brm-media-viewer-content {
    width: 100%;
    height: 100%;
    min-height: 0;
    display: grid;
    grid-template-rows: auto minmax(0, 1fr);
    align-items: center;
    gap: 12px;
}
#media-viewer .brm-media-viewer-toolbar {
    width: min(1200px, 100%);
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
    border-radius: 8px;
    background: #fff;
    color: #312e81;
    font-weight: 600;
    text-decoration: none;
}
#media-viewer .brm-media-viewer-stage {
    width: min(1200px, calc(100vw - 10vw));
    height: auto !important;
    min-height: 0;
    max-height: 100%;
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
    top: 16px;
    right: 24px;
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
    position: relative !important;
    z-index: auto !important;
    width: 100% !important;
    height: auto !important;
    max-height: none !important;
    overflow: visible;
    box-sizing: border-box;
    margin: 0 0 20px;
    padding: 28px;
    border: 1px solid var(--border-color-primary, #e5e7eb);
    border-radius: var(--radius-lg, 14px);
    background: var(--block-background-fill, #fff);
    box-shadow: 0 20px 64px rgba(15, 23, 42, 0.32);
}
#global-toolbar {
    align-items: center;
    margin-bottom: 6px;
}
#global-settings-panel {
    font-size: 1.05rem;
}
#global-settings-close {
    min-width: 116px !important;
}
#global-settings-close button {
    min-width: 116px !important;
}
@media (max-width: 720px) {
    #global-settings-panel {
        width: 100% !important;
        padding: 18px;
    }
    #media-viewer {
        padding: 64px 3vw max(132px, env(safe-area-inset-bottom));
    }
    #media-viewer .brm-media-viewer-stage {
        width: 94vw;
        height: auto !important;
        max-height: 100%;
    }
    #media-viewer img,
    #media-viewer video {
        max-width: 94vw !important;
        max-height: 100% !important;
    }
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

def _parse_size(size: str) -> tuple[int, int]:
    """把下拉框里 '宽 × 高' 形式的整体值解析成 (宽, 高)。"""
    m = re.search(r"(\d+)\s*[×xX*]\s*(\d+)", size or "")
    if not m:
        raise gr.Error(f"无法识别的尺寸:{size!r}")
    return int(m.group(1)), int(m.group(2))


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


# ---------------------------------------------------------------------------
# 提交工作流
# ---------------------------------------------------------------------------
def submit_workflow_1(prompt, size, batch):
    # 工作流 JSON 的文件名(不含 .json)
    if not (prompt or "").strip():
        raise gr.Error("请输入图片提示词")
    submit("image_z_image_turbo", {"prompt": prompt, "size": size, "batch": batch})


def submit_workflow_2(prompt, input_filename):
    # 工作流 JSON 的文件名(不含 .json)
    if not prompt or not input_filename:
        raise gr.Error("请输入提示词并上传要编辑的图片")
    submit("image_flux2_klein_image_edit_4b_base", {"prompt": prompt, "input_filename": input_filename})

def submit_workflow_3(prompt, size, seconds):
    # 工作流 JSON 的文件名(不含 .json)
    if not (prompt or "").strip():
        raise gr.Error("请输入视频提示词")
    submit("LTX23-文生视频", {"prompt": prompt, "seconds": seconds, "size": size})

def submit_workflow_4(prompt, input_filename, seconds):
    # 工作流 JSON 的文件名(不含 .json)
    if not (prompt or "").strip() or not input_filename:
        raise gr.Error("请输入提示词并上传源图片")
    submit("LTX23-图生视频", {"prompt": prompt, "seconds": seconds, "input_filename": input_filename})


def submit_workflow_5(prompt, input_filename1, input_filename2, seconds):
    # 工作流 JSON 的文件名(不含 .json)
    if not (prompt or "").strip() or not input_filename1 or not input_filename2:
        raise gr.Error("请输入提示词并上传首帧、尾帧图片")
    submit(
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
    submit(
        "LTX23-单图数字人-语音驱动",
        {
            "prompt": prompt, "size": size,
            "image": image,
            "audio": audio,
            "duration": uploaded_dur,
        }
    )


def submit_workflow_7(prompt, ref_audio, temperature=0.8):
    # 语音克隆 / TTS(IndexTTS2)。
    if not prompt or not prompt.strip():
        raise gr.Error("请输入要合成的文本")
    if not ref_audio:
        raise gr.Error("请上传参考音频(克隆音色来源)")
    submit("TTS-语音克隆", {
        "prompt": prompt,
        "ref_audio": ref_audio,
        "temperature": temperature,
    })


def submit_workflow_8(tags, lyrics, duration=30.0, bpm=120, language="zh", model="turbo"):
    # 音乐生成(ACE-Step 1.5)。
    if not tags or not tags.strip():
        raise gr.Error("请输入音乐风格标签(tags)")
    submit("音乐生成", {
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
def api_submit_workflow_1(prompt: str, size: str, batch: int) -> None:
    submit_workflow_1(prompt, size, batch)


def api_submit_workflow_2(prompt: str, input_filename: str) -> None:
    submit_workflow_2(prompt, input_filename)


def api_submit_workflow_3(prompt: str, size: str, seconds: int) -> None:
    submit_workflow_3(prompt, size, seconds)


def api_submit_workflow_4(prompt: str, input_filename: str, seconds: int) -> None:
    submit_workflow_4(prompt, input_filename, seconds)


def api_submit_workflow_5(
    prompt: str, input_filename1: str, input_filename2: str, seconds: int
) -> None:
    submit_workflow_5(prompt, input_filename1, input_filename2, seconds)


def api_submit_workflow_6(
    prompt: str, image: str, audio: str, duration: float, size: str
) -> None:
    submit_workflow_6(prompt, image, audio, duration, size)


def api_submit_workflow_7(prompt: str, ref_audio: str, temperature: float) -> None:
    submit_workflow_7(prompt, ref_audio, temperature)


def api_submit_workflow_8(
    tags: str, lyrics: str, duration: float, bpm: int, language: str, model: str
) -> None:
    submit_workflow_8(tags, lyrics, duration, bpm, language, model)


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
    width, height = _parse_size(args["size"])
    wf = json.loads(path.read_text(encoding="utf-8"))
    wf["14"]["inputs"]["text"] = args["prompt"]
    wf["38"]["inputs"]["value"] = args["seconds"]
    wf["23"]["inputs"]["width"] = width
    wf["23"]["inputs"]["height"] = height
    wf["29"]["inputs"]["noise_seed"] = random.randint(1, 10000)
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
    model = args.get("model", "turbo")
    wf["104"]["inputs"]["unet_name"] = f"acestep/acestep_v1.5_xl_{model}_bf16.safetensors"
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
    "LTX23-文生视频": (build_workflow_3, "文生视频"),
    "LTX23-图生视频": (build_workflow_4, "图生视频"),
    "LTX23-首尾帧视频": (build_workflow_5, "首尾帧视频"),
    "LTX23-单图数字人-语音驱动": (build_workflow_6, "单图数字人-语音驱动"),
    "TTS-语音克隆": (build_workflow_7, "语音克隆"),
    "音乐生成": (build_workflow_8, "音乐生成"),
}


def process_task(task: Task) -> None:
    """一次完整执行:构建工作流 -> 提交并等待 -> 解析并保存结果。供任务队列调用。"""
    builder = WORKFLOW_BUILDERS.get(task.workflow_name)
    if builder is None:
        raise ValueError(f"未登记的工作流:{task.workflow_name}")
    workflow = builder[0](task.workflow_name, task.args)
    outputs = run_workflow(workflow, stop_event=task_queue.stop_event)
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

    concurrency = _setting_int(queue_concurrency, QUEUE_CONCURRENCY, 1, 4)
    task_limit = _setting_int(done_tasks_max, DONE_TASKS_MAX, 20, 500)
    gallery_limit = _setting_int(done_gallery_max, DONE_GALLERY_MAX, 10, 100)
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
    return (
        f"✅ 全局设置已保存并生效：并发 {target_workers}（当前 worker {active_workers}），"
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
    """点击提交:把任务放进队列,立即返回。队列空时会被 worker 立即取走执行。"""
    _name = WORKFLOW_BUILDERS[wfname][1]
    task = Task(id=uuid.uuid4().hex, name=make_task_name(_name), workflow_name=wfname, args=args)
    task_queue.enqueue(task)
    gr.Info(f"已提交", duration=2)


def clear_pending():
    return f"已清空排队任务 {task_queue.clear_pending()} 个。"


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
    TaskStatus.ERROR:   "🔴",
}


def _md_cell(text: str) -> str:
    """转义 Markdown 表格单元格里的特殊字符,并把换行压成空格。"""
    return (text or "").replace("|", "\\|").replace("\n", " ").replace("\r", " ").strip()


def _completed_output_path(value) -> Path | None:
    """只允许任务历史中位于输出目录的文件进入预览或播放器。"""
    try:
        path = Path(value).resolve()
        output_root = OUTPUT_DIR.resolve()
    except (OSError, TypeError, ValueError):
        return None
    if path.is_file() and (path == output_root or output_root in path.parents):
        return path
    return None


def open_completed_media_viewer(gallery_paths, evt: gr.SelectData):
    """由缩略图选择事件打开覆盖浏览器主体的图片/视频查看器。"""
    try:
        index = evt.index[0] if isinstance(evt.index, (tuple, list)) else int(evt.index)
        path = _completed_output_path(gallery_paths[index])
    except (IndexError, TypeError, ValueError, AttributeError):
        path = None

    if path is None:
        return gr.update(visible=False), gr.update(visible=False)

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
        f'<a class="brm-media-download" href="{url}" download>下载</a>'
        '</div>'
        f'<div class="brm-media-viewer-stage">{media}</div>'
        '</div>'
    )
    return gr.update(value=viewer_html, visible=True), gr.update(visible=True)


def close_completed_media_viewer():
    return gr.update(value="", visible=False), gr.update(visible=False)


def play_completed_audio(path):
    """将用户从完成音频列表中选择的文件送入内置播放器。"""
    path = _completed_output_path(path)
    return str(path) if path and path.suffix.lower() in AUDIO_EXTS else None


def clear_completed_audio_preview():
    """停止并移除当前试听音频，避免旧音频持续占据播放器。"""
    return gr.update(value=None), None


def render_queue():
    """把队列快照渲染成 概览文本 / 任务表格(Markdown) / 已完成画廊。"""
    pending, running, done = task_queue.snapshot()

    def note(t: Task) -> str:
        if t.status == TaskStatus.ERROR:
            return t.error
        if t.status == TaskStatus.DONE and isinstance(t.result, list):
            return f"产出 {len(t.result)} 个文件"
        return ""

    def cost(t: Task) -> str:
        # 只有已完成的任务显示耗时,其余一律用短横代替。
        if t.status == TaskStatus.DONE and t.start_ts and t.done_ts:
            return _fmt_duration(t.done_ts - t.start_ts)
        return "-"

    # 汇总处理中 / 排队中 / 已完成的所有任务,按提交时间倒序(最新的在最前面)。
    all_tasks = list(pending) + list(running) + list(done)
    all_tasks.sort(key=lambda t: t.submit_ts, reverse=True)

    rows = [
        "| 任务名称 | 状态 | 提交时间 | 耗时 | 备注 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for t in all_tasks:
        icon = STATUS_ICONS.get(t.status, "")
        rows.append(
            f"| {_md_cell(t.name)} "
            f"| {icon} {t.status.value} "
            f"| {_fmt_ts(t.submit_ts)} "
            f"| {cost(t)} "
            f"| {_md_cell(note(t))} |"
        )
    if len(rows) == 2:      # 只有表头,说明暂无任务
        rows.append("| 暂无任务 |  |  |  |  |")
    table_md = "\n".join(rows)

    ok = sum(1 for t in done if t.status == TaskStatus.DONE)
    err = sum(1 for t in done if t.status == TaskStatus.ERROR)
    backend = "在线" if is_alive() else "离线"
    summary = (f"ComfyUI:{backend}　｜　并发 {QUEUE_CONCURRENCY}　｜　排队 {len(pending)}　｜　"
               f"处理中 {len(running)}　｜　完成 {ok}　｜　失败 {err}")

    # 图片与视频保持紧凑缩略图，点击后交给独立全屏查看器；音频提供下载列表和播放器选择器。
    imgs = []
    audios = []
    for t in done:
        if isinstance(t.result, list):
            for p in t.result:
                suffix = Path(p).suffix.lower()
                if suffix in GALLERY_EXTS:
                    imgs.append(p)
                elif suffix in AUDIO_EXTS:
                    audios.append(p)
    gallery_paths = imgs[:DONE_GALLERY_MAX]
    audio_paths = audios[:DONE_TASKS_MAX]
    audio_choices = [(Path(path).name, path) for path in audio_paths]
    return (
        summary,
        table_md,
        gallery_paths,
        audio_paths,
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

def build_ui():
    with gr.Blocks(title="ComfyUI × Gradio", css=CUSTOM_CSS, theme=gr.themes.Soft()) as demo:
        with gr.Row(elem_id="global-toolbar", equal_height=True):
            with gr.Column(scale=8):
                gr.Markdown("## BRM AI 工作台")
            with gr.Column(scale=1, min_width=140):
                settings_btn = gr.Button("⚙ 全局设置", variant="secondary")

        with gr.Group(visible=False, elem_id="global-settings-panel") as settings_panel:
            with gr.Row(equal_height=True):
                with gr.Column(scale=10):
                    gr.Markdown(
                        "### 全局设置\n"
                        "并发会立即调整；下调时，已在处理的任务会自然完成后再收缩。"
                        "视频、数字人等高显存任务通常建议保持并发 **1**。"
                    )
                with gr.Column(scale=1, min_width=116):
                    settings_close_top_btn = gr.Button(
                        "✕ 关闭", variant="secondary", elem_id="global-settings-close",
                    )
            with gr.Row():
                setting_concurrency = gr.Slider(
                    1, 4, value=QUEUE_CONCURRENCY, step=1, precision=0,
                    label="任务并发数",
                )
                setting_done_tasks = gr.Slider(
                    20, 500, value=DONE_TASKS_MAX, step=10, precision=0,
                    label="已完成任务保留数",
                )
                setting_done_gallery = gr.Slider(
                    10, 100, value=DONE_GALLERY_MAX, step=5, precision=0,
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

        # ---- 每个工作流一个 Tab。新增工作流时,复制一个 gr.Tab 块即可。 ----
        with gr.Tabs():
            # ========== Tab 1 ==========
            with gr.Tab("文生图Z-Image"):
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
                with gr.Row(equal_height=True):
                    with gr.Column(scale=1):
                        prompt2 = gr.Textbox(label="提示词", autofocus=True, placeholder="输入图片提示词", lines=13, max_lines=13)
                    with gr.Column(scale=1):
                        reference_image2 = gr.Image(type="filepath", height=400)   # 关键:拿到磁盘路径才能上传
                        uploaded_name2   = gr.State("")                # 存上传后 ComfyUI 给的文件名
                        submit_btn2 = gr.Button("提交", variant="primary")
                reference_image2.upload(fn=on_ref_upload, inputs=reference_image2, outputs=uploaded_name2)
                reference_image2.clear(fn=lambda: "", outputs=uploaded_name2)   # 清空时一并清掉
                submit_btn2.click(
                    fn=submit_workflow_2,
                    inputs=[prompt2, uploaded_name2],
                    api_name="ui_submit_workflow_2",
                    api_visibility="private",
                )

            # ========== Tab 3 ==========
            with gr.Tab("文生视频LTX2.3"):
                with gr.Row():
                    with gr.Column(scale=1):
                        prompt3 = gr.Textbox(label="提示词", autofocus=True, value="一个漂亮的亚洲女孩在在花丛中散步", lines=3)
                    with gr.Column(scale=1):
                        with gr.Row():
                            size3 = gr.Dropdown(label="视频尺寸", choices=output_size, value="768 × 1024")
                            seconds3 = gr.Number(value=5, label="视频时长", minimum=2, maximum=360, precision=0)
                        submit_btn3 = gr.Button("提交", variant="primary")
                submit_btn3.click(
                    fn=submit_workflow_3,
                    inputs=[prompt3, size3, seconds3],
                    api_name="ui_submit_workflow_3",
                    api_visibility="private",
                )

            # ========== Tab 4 ==========
            with gr.Tab("图生视频LTX2.3"):
                with gr.Row(equal_height=True):
                    with gr.Column(scale=1):
                        prompt4 = gr.Textbox(label="提示词", autofocus=True, placeholder="输入提示词", lines=10, max_lines=10)
                        seconds4 = gr.Number(value=5, label="视频时长", minimum=2, maximum=360, precision=0)
                    with gr.Column(scale=1):
                        reference_image4 = gr.Image(type="filepath", height=400)   # 关键:拿到磁盘路径才能上传
                        uploaded_name4   = gr.State("")                # 存上传后 ComfyUI 给的文件名
                        submit_btn4 = gr.Button("提交", variant="primary")
                reference_image4.upload(fn=on_ref_upload, inputs=reference_image4, outputs=uploaded_name4)
                reference_image4.clear(fn=lambda: "", outputs=uploaded_name4)   # 清空时一并清掉
                submit_btn4.click(
                    fn=submit_workflow_4,
                    inputs=[prompt4, uploaded_name4, seconds4],
                    api_name="ui_submit_workflow_4",
                    api_visibility="private",
                )

            # ========== Tab 5 ==========
            with gr.Tab("首尾帧视频LTX2.3"):
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

                reference_image5_1.upload(fn=on_ref_upload, inputs=reference_image5_1, outputs=uploaded_name5_1)
                reference_image5_1.clear(fn=lambda: "", outputs=uploaded_name5_1)   # 清空时一并清掉

                reference_image5_2.upload(fn=on_ref_upload, inputs=reference_image5_2, outputs=uploaded_name5_2)
                reference_image5_2.clear(fn=lambda: "", outputs=uploaded_name5_2)   # 清空时一并清掉

                submit_btn5.click(
                    fn=submit_workflow_5,
                    inputs=[prompt5, uploaded_name5_1, uploaded_name5_2, seconds5],
                    api_name="ui_submit_workflow_5",
                    api_visibility="private",
                )

            # ========== Tab 6 ==========
            with gr.Tab("数字人-语音驱动LTX2.3"):
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

                reference_audio6_1.upload(fn=on_ref_upload, inputs=reference_audio6_1, outputs=uploaded_name6_1)
                reference_audio6_1.clear(fn=lambda: "", outputs=uploaded_name6_1)   # 清空时一并清掉

                reference_audio6_2.upload(fn=on_audio_upload, inputs=reference_audio6_2, outputs=[uploaded_name6_2, uploaded_dur])
                reference_audio6_2.clear(fn=lambda: ("", ""), outputs=[uploaded_name6_2, uploaded_dur])   # 清空时一并清掉

                submit_btn6.click(
                    fn=submit_workflow_6,
                    inputs=[prompt6, uploaded_name6_1, uploaded_name6_2, uploaded_dur, size6],
                    api_name="ui_submit_workflow_6",
                    api_visibility="private",
                )

            # ========== Tab 7 ==========
            with gr.Tab("语音克隆IndexTTS2"):
                with gr.Row(equal_height=True):
                    with gr.Column(scale=1):
                        prompt7 = gr.Textbox(label="合成文本", autofocus=True, placeholder="输入要合成的文本",
                                             value="你好，这是一段语音克隆测试。", lines=8, max_lines=12)
                    with gr.Column(scale=1):
                        # 参考音频:用 on_audio_upload 复用上传(它走 /upload/image 到 input 目录)
                        # 同时返回 ComfyUI 文件名 + 时长,这里只需文件名
                        reference_audio7 = gr.Audio(label="参考音频(克隆音色来源,建议10-30秒清晰人声)",
                                                    type="filepath")
                        uploaded_name7   = gr.State("")   # ComfyUI 文件名
                        uploaded_dur7    = gr.State(0.0)  # 时长(本工作流用不到,但 on_audio_upload 返回两个值)
                        temperature7     = gr.Slider(0.1, 1.5, value=0.8, step=0.05,
                                                     label="采样温度(越高越多样,越低越稳定)")
                        submit_btn7 = gr.Button("提交", variant="primary")
                reference_audio7.upload(fn=on_audio_upload, inputs=reference_audio7,
                                        outputs=[uploaded_name7, uploaded_dur7])
                reference_audio7.clear(fn=lambda: ("", 0.0), outputs=[uploaded_name7, uploaded_dur7])
                submit_btn7.click(
                    fn=submit_workflow_7,
                    inputs=[prompt7, uploaded_name7, temperature7],
                    api_name="ui_submit_workflow_7",
                    api_visibility="private",
                )

            # ========== Tab 8 ==========
            with gr.Tab("音乐生成ACE-Step 1.5"):
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
                                choices=["turbo", "base", "sft"],
                                value="turbo",
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
                gr.Markdown(
                    "使用本机 Qwen3.5-4B 的流式问答能力做快速验证。"
                    "高显存视频模式会暂时停止 Qwen 服务。"
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
                with gr.Row():
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
                with gr.Row():
                    qwen_send_btn = gr.Button("发送并流式回答", variant="primary")
                    qwen_stop_btn = gr.Button("停止", variant="stop")
                    qwen_clear_btn = gr.Button("清空", variant="secondary")
                qwen_answer = gr.Textbox(
                    label="Qwen 流式回答",
                    lines=18,
                    max_lines=30,
                    interactive=False,
                    autoscroll=True,
                    buttons=["copy"],
                    elem_id="qwen-answer",
                )

        gr.Markdown("---")

        # ---- 共享的任务队列面板(所有 Tab 共用一个队列与后台 worker) ----
        gr.Markdown("### 任务队列")
        q_summary = gr.Markdown("队列状态加载中……")
        with gr.Row(equal_height=True):
            with gr.Column(scale=10):
                q_table = gr.Markdown(elem_id="q-table-md")
            with gr.Column(scale=1, min_width=160):
                clear_btn = gr.Button("清空排队任务")
                interrupt_btn = gr.Button("中断 ComfyUI 执行（影响全部任务）", variant="stop")
        op_status = gr.Markdown("")
        q_audio = gr.File(label="已完成音频（累计，可下载）", file_count="multiple", height=110)
        with gr.Row():
            completed_audio_selector = gr.Dropdown(
                label="选择要试听的已完成音频",
                choices=[],
                value=None,
                scale=1,
            )
            completed_audio_player = gr.Audio(
                label="音频试听",
                type="filepath",
                interactive=False,
                buttons=["download"],
                scale=2,
            )
        clear_audio_preview_btn = gr.Button("清除当前试听", variant="secondary")
        q_gallery = gr.Gallery(
            label="已完成图片/视频（累计）",
            columns=5,
            rows=1,
            height=190,
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
        gr.Markdown("点击图片或视频缩略图会打开浏览器主体大预览；可在预览内下载。画廊工具栏可下载全部。")
        gr.Markdown("", height=20)

        # 事件绑定。
        clear_btn.click(fn=clear_pending, outputs=op_status)
        interrupt_btn.click(fn=interrupt, outputs=op_status)
        completed_audio_selector.change(
            fn=play_completed_audio,
            inputs=completed_audio_selector,
            outputs=completed_audio_player,
            api_visibility="private",
        )
        clear_audio_preview_btn.click(
            fn=clear_completed_audio_preview,
            outputs=[completed_audio_selector, completed_audio_player],
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
                q_table,
                q_gallery,
                q_audio,
                completed_audio_selector,
                completed_gallery_paths,
            ],
        )
        gr.Timer(3.0).tick(fn=check_health)

        # Stable LAN API surface. These wrappers accept explicit values for
        # arguments that are State-only in the browser UI (uploaded filenames
        # and audio duration), so every documented workflow can be submitted
        # from a non-browser client.
        gr.api(api_submit_workflow_1, api_name="submit_workflow_1")
        gr.api(api_submit_workflow_2, api_name="submit_workflow_2")
        gr.api(api_submit_workflow_3, api_name="submit_workflow_3")
        gr.api(api_submit_workflow_4, api_name="submit_workflow_4")
        gr.api(api_submit_workflow_5, api_name="submit_workflow_5")
        gr.api(api_submit_workflow_6, api_name="submit_workflow_6")
        gr.api(api_submit_workflow_7, api_name="submit_workflow_7")
        gr.api(api_submit_workflow_8, api_name="submit_workflow_8")

        # 放在既有队列/API 事件之后，保持旧浏览器标签页中已有事件的编号稳定。
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
    )


if __name__ == "__main__":
    main()
