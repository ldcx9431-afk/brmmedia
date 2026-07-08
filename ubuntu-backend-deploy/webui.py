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
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

import gradio as gr

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
def load_config():
    """ 加载启动器的配置文件 """
    if os.path.exists(YZY_CONFIG_PATH):
        with open(YZY_CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}
def find_available_port(start_port=9000):
    """ 找一个可用端口 """
    port = start_port
    cnt = 0
    while True:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(("0.0.0.0", port))
                return port
        except Exception:
            cnt += 1
            if cnt >= 20:
                print("请检查网络是否正常.")
                sys.exit(1)
            print(f"端口 {port} 已被占用，尝试下一个...")
            sys.stdout.flush()
            time.sleep(0.1)  # 必须马上打印，不影响后面的打印
            port += 1
# 必须是第一个打印的：把端口号打印到 stdout（electron 会捕获）
server_port = find_available_port()
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
        self._done: list[Task] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()      # 有新任务时唤醒 worker,实现立即执行
        self._workers: list[threading.Thread] = []

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
        self._stop.clear()
        self._workers = [worker for worker in self._workers if worker.is_alive()]
        while len(self._workers) < self._max_workers:
            index = len(self._workers) + 1
            worker = threading.Thread(target=self._loop, args=(index,), daemon=True, name=f"task-worker-{index}")
            worker.start()
            self._workers.append(worker)
        print(f"[Queue] workers started: {len(self._workers)}")

    def _loop(self, worker_index: int = 1) -> None:
        while not self._stop.is_set():
            task = None
            with self._lock:
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
    submit("image_z_image_turbo", {"prompt": prompt, "size": size, "batch": batch})


def submit_workflow_2(prompt, input_filename):
    # 工作流 JSON 的文件名(不含 .json)
    if not prompt or not input_filename:
        gr.Error("请输入提示词 并且 上传要编辑的图片")
        return
    submit("image_flux2_klein_image_edit_4b_base", {"prompt": prompt, "input_filename": input_filename})

def submit_workflow_3(prompt, size, seconds):
    # 工作流 JSON 的文件名(不含 .json)
    submit("LTX23-文生视频", {"prompt": prompt, "seconds": seconds, "size": size})

def submit_workflow_4(prompt, input_filename, seconds):
    # 工作流 JSON 的文件名(不含 .json)
    submit("LTX23-图生视频", {"prompt": prompt, "seconds": seconds, "input_filename": input_filename})


def submit_workflow_5(prompt, input_filename1, input_filename2, seconds):
    # 工作流 JSON 的文件名(不含 .json)
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

    # 只把图片类产物送进画廊(音频/视频无法在画廊显示,但都已存到 outputs 目录)。
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
    return summary, table_md, imgs[:DONE_GALLERY_MAX], audios[:DONE_TASKS_MAX]


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

def build_ui():
    with gr.Blocks(title="ComfyUI × Gradio", css=CUSTOM_CSS, theme=gr.themes.Soft()) as demo:
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
                submit_btn1.click(fn=submit_workflow_1, inputs=[prompt1, size1, batch1])

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
                submit_btn2.click(fn=submit_workflow_2, inputs=[prompt2, uploaded_name2])

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
                submit_btn3.click(fn=submit_workflow_3, inputs=[prompt3, size3, seconds3])

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
                submit_btn4.click(fn=submit_workflow_4, inputs=[prompt4, uploaded_name4, seconds4])

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

                submit_btn5.click(fn=submit_workflow_5, inputs=[prompt5, uploaded_name5_1, uploaded_name5_2, seconds5])

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

                submit_btn6.click(fn=submit_workflow_6, inputs=[prompt6, uploaded_name6_1, uploaded_name6_2, uploaded_dur, size6])

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
                submit_btn7.click(fn=submit_workflow_7, inputs=[prompt7, uploaded_name7, temperature7])

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
                interrupt_btn = gr.Button("中断当前任务")
        op_status = gr.Markdown("")
        q_audio = gr.File(label="已完成音频(累计)", file_count="multiple")
        q_gallery = gr.Gallery(label="已完成图片/视频(累计)", columns=3, height=320)
        gr.Markdown("", height=20)

        # 事件绑定。
        clear_btn.click(fn=clear_pending, outputs=op_status)
        interrupt_btn.click(fn=interrupt, outputs=op_status)

        # 定时刷新任务面板与存活检测。
        gr.Timer(1.5).tick(fn=render_queue, outputs=[q_summary, q_table, q_gallery, q_audio])
        gr.Timer(3.0).tick(fn=check_health)

    return demo


def main():
    start_comfyui(wait=True)
    task_queue.start_workers()      # 启动队列后台 worker

    demo = build_ui()
    demo.queue()
    demo.launch(server_name="0.0.0.0", server_port=server_port, inbrowser=False)


if __name__ == "__main__":
    main()
