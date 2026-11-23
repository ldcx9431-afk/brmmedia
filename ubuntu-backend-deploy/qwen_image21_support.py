"""Unified Qwen Image 2.1 on main ComfyUI (:8188), called by the shared media TaskQueue."""
import os
import json
import logging
import sys
from pathlib import Path, PurePosixPath
import time
import uuid
import requests

BASE = os.environ.get('BRM_QWEN_IMAGE21_URL', 'http://127.0.0.1:8188').rstrip('/')
WORKFLOW = 'QwenImage21-文生图'
EDIT = 'QwenImage21-图生图'
WORKFLOWS = (WORKFLOW, EDIT)
MODEL = 'qwen_image_2.1_int8_convrot.safetensors'
CLIP = 'qwen3vl_8b_int8_convrot.safetensors'
VAE = 'qwen_image_2.1_vae_bf16.safetensors'
TURBO_LORA = 'qwen_image_2.1_turbo_lora_avg_rank_178_bf16.safetensors'
DEFAULT_TURBO_STEPS = 8
DEFAULT_BASE_STEPS = 40
TURBO_AUTO_MAX_STEPS = 12
MAX_PIXELS = 1024 * 1024
FULL_HD_SIZES = ((1920, 1080), (1080, 1920))


def _resolve_turbo_and_steps(steps, turbo):
    """Resolve effective Turbo LoRA flag and default step count."""
    if turbo is not None:
        if not isinstance(turbo, bool):
            raise ValueError('turbo 参数必须为布尔值')
        use_turbo = turbo
    elif steps is not None:
        if isinstance(steps, bool) or int(steps) != steps:
            raise ValueError('尺寸、步数及种子必须为整数')
        use_turbo = int(steps) <= TURBO_AUTO_MAX_STEPS
    else:
        use_turbo = True
    if steps is None:
        steps = DEFAULT_TURBO_STEPS if use_turbo else DEFAULT_BASE_STEPS
    return use_turbo, steps


def validate(prompt, width=1024, height=1024, steps=None, seed=0, turbo: bool | None = None):
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 8000:
        raise ValueError('请输入提示词（最多8000字符）')
    use_turbo, steps = _resolve_turbo_and_steps(steps, turbo)
    values = (width, height, steps, seed)
    if any(isinstance(v, bool) or int(v) != v for v in values):
        raise ValueError('尺寸、步数及种子必须为整数')
    width, height, steps, seed = map(int, values)
    if (width, height) not in FULL_HD_SIZES and (min(width, height) < 256 or max(width, height) > 1536 or width % 32 or height % 32 or width * height > MAX_PIXELS):
        raise ValueError('请选择1920×1080、1080×1920，或32倍数、256–1536px且不超过1MP的尺寸')
    if not 1 <= steps <= 40 or not 0 <= seed <= 2**63 - 1:
        raise ValueError('步数须为1–40，种子须为0–2^63-1')
    return dict(prompt=prompt.strip(), width=width, height=height, steps=steps, seed=seed, turbo=use_turbo)


def validate_edit(prompt, width=1024, height=1024, steps=None, seed=0, input_filename='', turbo: bool | None = None):
    args = validate(prompt, width, height, steps, seed, turbo=turbo)
    path = PurePosixPath(input_filename) if isinstance(input_filename, str) else None
    if not input_filename or path is None or path.is_absolute() or '..' in path.parts or '\\' in input_filename or ':' in input_filename:
        raise ValueError('请先上传参考图片')
    args['input_filename'] = input_filename
    return args


def build_workflow(args, turbo: bool | None = None):
    raw_turbo = turbo if turbo is not None else args.get('turbo')
    a = validate(
        args['prompt'],
        args.get('width', 1024),
        args.get('height', 1024),
        args.get('steps'),
        args.get('seed', 0),
        turbo=raw_turbo,
    )
    width, height = ((a['width'] + 31) // 32 * 32, (a['height'] + 31) // 32 * 32)
    graph = {
        '1': {'class_type': 'UNETLoader', 'inputs': {'unet_name': MODEL, 'weight_dtype': 'default'}},
        '2': {'class_type': 'CLIPLoader', 'inputs': {'clip_name': CLIP, 'type': 'qwen_image', 'device': 'default'}},
        '3': {'class_type': 'VAELoader', 'inputs': {'vae_name': VAE}},
        '4': {'class_type': 'TextEncodeQwenImage21', 'inputs': {'clip': ['2', 0], 'prompt': a['prompt'], 'negative_prompt': '', 'resolution': 1024}},
        '5': {'class_type': 'EmptyLatentImage', 'inputs': {'width': width, 'height': height, 'batch_size': 1}},
        '6': {'class_type': 'KSampler', 'inputs': {'model': ['1', 0], 'seed': a['seed'], 'steps': a['steps'], 'cfg': 1.0, 'sampler_name': 'euler', 'scheduler': 'simple', 'positive': ['4', 0], 'negative': ['4', 1], 'latent_image': ['5', 0], 'denoise': 1.0}},
        '7': {'class_type': 'VAEDecode', 'inputs': {'samples': ['6', 0], 'vae': ['3', 0]}},
        '8': {'class_type': 'SaveImage', 'inputs': {'images': ['7', 0], 'filename_prefix': 'QwenImage21/research'}},
    }
    if a['turbo']:
        graph['12'] = {
            'class_type': 'LoraLoaderModelOnly',
            'inputs': {
                'lora_name': TURBO_LORA,
                'strength_model': 1.0,
                'model': ['1', 0],
            },
        }
        graph['6']['inputs']['model'] = ['12', 0]
    if args.get('input_filename'):
        validate_edit(a['prompt'], a['width'], a['height'], a['steps'], a['seed'], args['input_filename'], turbo=a['turbo'])
        graph['9'] = {'class_type': 'LoadImage', 'inputs': {'image': args['input_filename']}}
        graph['10'] = {'class_type': 'ImageScale', 'inputs': {'image': ['9', 0], 'upscale_method': 'lanczos', 'width': width, 'height': height, 'crop': 'center'}}
        graph['4']['inputs'].update({'vae': ['3', 0], 'images.image_1': ['10', 0], 'resolution': 0})
        graph['6']['inputs']['latent_image'] = ['4', 2]
        del graph['5']
    if (a['width'], a['height']) in FULL_HD_SIZES:
        graph['11'] = {'class_type': 'ImageCrop', 'inputs': {'image': ['7', 0], 'width': a['width'], 'height': a['height'], 'x': (width-a['width'])//2, 'y': (height-a['height'])//2}}
        graph['8']['inputs']['images'] = ['11', 0]
    return graph

def _session():
    s = requests.Session()
    s.trust_env = False
    return s

def release_idle(base):
    with _session() as s:
        r = s.get(base + '/queue', timeout=10); r.raise_for_status()
        q = r.json()
        if q.get('queue_running') or q.get('queue_pending'):
            raise RuntimeError('模型运行时仍有任务；拒绝争抢媒体GPU，请稍后重试')
        r = s.post(base + '/free', json={'unload_models': True, 'free_memory': True}, timeout=20)
        r.raise_for_status()

def cancel_prompt(session, pid):
    """Remove only our pending job and target only our running prompt.

    Pinned ComfyUI supports prompt_id on /interrupt; never interrupt globally.
    Deletion first also covers a prompt moving from pending to running.
    """
    r = session.post(BASE + '/queue', json={'delete': [pid]}, timeout=10)
    r.raise_for_status()
    r = session.post(BASE + '/interrupt', json={'prompt_id': pid}, timeout=10)
    r.raise_for_status()

def _mark_model_group(base: str, model_group: str) -> None:
    try:
        import media_runtime_guard
        media_runtime_guard.set_last_model_group(base, model_group)
    except Exception:
        pass


def read_progress(ws, pid, progress):
    """Drain pending events; persist only the newest matching progress.

    Returns True when ``execution_success`` for ``pid`` is observed.
    """
    latest = None
    succeeded = False
    for _ in range(256):
        try:
            message = ws.recv()
        except Exception:
            break
        if not isinstance(message, str):
            continue
        try:
            event = json.loads(message)
            data = event.get('data', {})
            if data.get('prompt_id', pid) == pid:
                ev_type = event.get('type')
                if ev_type == 'progress':
                    maximum = int(data.get('max', 0))
                    value = int(data.get('value', 0))
                    latest = {'stage': 'qwen_image21_sampling', 'current_step': value, 'total_steps': maximum, 'progress': value / maximum if maximum else 0}
                elif ev_type == 'execution_success':
                    succeeded = True
        except (ValueError, TypeError, AttributeError):
            continue
    if latest is not None:
        progress(latest)
    return succeeded


def _download_view_to_path(session, base: str, img_params: dict, dest: Path) -> None:
    r = session.get(base + '/view', params=img_params, timeout=60, stream=True)
    r.raise_for_status()
    iter_content = getattr(r, 'iter_content', None)
    iterator = None
    if callable(iter_content):
        try:
            iterator = iter(iter_content(chunk_size=1024 * 1024))
        except TypeError:
            iterator = None
    with dest.open('wb') as fh:
        if iterator is not None:
            for chunk in iterator:
                if chunk:
                    fh.write(chunk)
        else:
            fh.write(r.content)


def run_task(task, output_dir, main_base, progress, *, workflow_builder=build_workflow):
    if task.cancel_event.is_set():
        raise RuntimeError('Qwen Image 2.1 任务已取消')
    single_instance = (BASE == main_base.rstrip('/'))
    if not single_instance:
        release_idle(main_base)
    pid = getattr(task, 'prompt_id', '')
    if not pid:
        if not single_instance:
            release_idle(BASE)
        args = dict(task.args)
        if task.workflow_name == EDIT:
            args = validate_edit(**args)
            if not single_instance:
                from h3_image_support import transfer_input
                with _session() as upload_session:
                    args['input_filename'] = transfer_input(upload_session, main_base, args['input_filename'], target_base=BASE)
        wf = workflow_builder(args)
    _mark_model_group(BASE, 'qwen21')
    with _session() as s:
        client = uuid.uuid4().hex
        ws = None
        try:
            import websocket
            ws = websocket.create_connection(BASE.replace('http://', 'ws://').replace('https://', 'wss://') + '/ws?clientId=' + client, timeout=0.2)
            ws.settimeout(0.02)
        except Exception:
            pass  # HTTP history remains authoritative when websocket is unavailable.
        try:
            if pid:
                r = s.get(BASE + '/history/' + pid, timeout=15); r.raise_for_status()
                if pid not in r.json():
                    r = s.get(BASE + '/queue', timeout=10); r.raise_for_status()
                    queued = r.json()
                    if not any(item[1] == pid for key in ['queue_running', 'queue_pending'] for item in queued.get(key, [])):
                        raise RuntimeError('原研究任务已不在隔离运行时中，拒绝自动重复提交')
            else:
                r = s.post(BASE + '/prompt', json={'prompt': wf, 'client_id': client}, timeout=30)
                r.raise_for_status()
                pid = r.json().get('prompt_id')
        except Exception:
            if ws is not None:
                ws.close()
            raise
        if not pid:
            if ws is not None:
                ws.close()
            raise RuntimeError('Qwen Image 2.1 未接受任务')
        deadline = time.monotonic() + 3600
        stopping = False
        try:
            progress({'prompt_id': pid, 'stage': 'qwen_image21_sampling'})
            while True:
                if ws is not None:
                    read_progress(ws, pid, progress)
                if (task.cancel_event.is_set() or time.monotonic() > deadline) and not stopping:
                    cancel_prompt(s, pid)
                    stopping = True
                r = s.get(BASE + '/history/' + pid, timeout=15); r.raise_for_status()
                entry = r.json().get(pid)
                if entry:
                    if stopping:
                        raise RuntimeError('Qwen Image 2.1 已取消或超过1小时预算')
                    if entry.get('status', {}).get('status_str') != 'success':
                        raise RuntimeError('Qwen Image 2.1 推理失败：' + str(entry.get('status', {}).get('messages', []))[-1200:])
                    paths = []
                    for out in entry.get('outputs', {}).values():
                        for img in out.get('images', []):
                            path = Path(output_dir) / (task.name + '-' + uuid.uuid4().hex[:8] + '.png')
                            _download_view_to_path(s, BASE, img, path)
                            paths.append(str(path))
                    if not paths:
                        raise RuntimeError('Qwen Image 2.1 没有输出图片')
                    progress({'stage': 'completed', 'progress': 1.0})
                    return paths
                if stopping:
                    q = s.get(BASE + '/queue', timeout=10); q.raise_for_status()
                    if not q.json().get('queue_running') and not q.json().get('queue_pending'):
                        raise RuntimeError('Qwen Image 2.1 已取消或超时')
                time.sleep(1)
        finally:
            failed = sys.exc_info()[0] is not None
            if ws is not None:
                try:
                    ws.close()
                except Exception:
                    logging.warning('Qwen Image 2.1 websocket close failed')
            if failed:
                try:
                    cancel_prompt(s, pid)
                except Exception:
                    logging.warning('Qwen Image 2.1 targeted cleanup failed; idle GPU guard remains enabled')
            if not single_instance:
                try:
                    release_idle(BASE)
                except Exception:
                    if not failed:
                        raise  # A successful task cannot conceal failed GPU handover.
                    logging.warning('Qwen Image 2.1 GPU release failed; preserving original task error')
