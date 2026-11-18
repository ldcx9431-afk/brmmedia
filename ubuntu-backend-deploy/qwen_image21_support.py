"""Isolated Qwen Image 2.1, called only by the shared media TaskQueue."""
import os
import json
from pathlib import Path
import time
import uuid
import requests

BASE = os.environ.get('BRM_QWEN_IMAGE21_URL', 'http://127.0.0.1:8190').rstrip('/')
WORKFLOW = 'QwenImage21-文生图'
MODEL = 'qwen_image_2.1_int8_convrot.safetensors'
CLIP = 'qwen3vl_8b_int8_convrot.safetensors'
VAE = 'qwen_image_2.1_vae_bf16.safetensors'
MAX_PIXELS = 1024 * 1024

def validate(prompt, width, height, steps, seed):
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 8000:
        raise ValueError('请输入提示词（最多8000字符）')
    values = (width, height, steps, seed)
    if any(isinstance(v, bool) or int(v) != v for v in values):
        raise ValueError('尺寸、步数及种子必须为整数')
    width, height, steps, seed = map(int, values)
    if min(width, height) < 256 or max(width, height) > 1536 or width % 32 or height % 32 or width * height > MAX_PIXELS:
        raise ValueError('研究版尺寸必须为32的倍数，256–1536px且不超过1MP')
    if not 1 <= steps <= 40 or not 0 <= seed <= 2**63 - 1:
        raise ValueError('步数须为1–40，种子须为0–2^63-1')
    return dict(prompt=prompt.strip(), width=width, height=height, steps=steps, seed=seed)

def build_workflow(args):
    a = validate(args['prompt'], args['width'], args['height'], args['steps'], args['seed'])
    return {
        '1': {'class_type': 'UNETLoader', 'inputs': {'unet_name': MODEL, 'weight_dtype': 'default'}},
        '2': {'class_type': 'CLIPLoader', 'inputs': {'clip_name': CLIP, 'type': 'qwen_image', 'device': 'default'}},
        '3': {'class_type': 'VAELoader', 'inputs': {'vae_name': VAE}},
        '4': {'class_type': 'TextEncodeQwenImage21', 'inputs': {'clip': ['2', 0], 'prompt': a['prompt'], 'negative_prompt': '', 'resolution': 1024}},
        '5': {'class_type': 'EmptyLatentImage', 'inputs': {'width': a['width'], 'height': a['height'], 'batch_size': 1}},
        '6': {'class_type': 'KSampler', 'inputs': {'model': ['1', 0], 'seed': a['seed'], 'steps': a['steps'], 'cfg': 1.0, 'sampler_name': 'euler', 'scheduler': 'simple', 'positive': ['4', 0], 'negative': ['4', 1], 'latent_image': ['5', 0], 'denoise': 1.0}},
        '7': {'class_type': 'VAEDecode', 'inputs': {'samples': ['6', 0], 'vae': ['3', 0]}},
        '8': {'class_type': 'SaveImage', 'inputs': {'images': ['7', 0], 'filename_prefix': 'QwenImage21/research'}},
    }

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

def read_progress(ws, pid, progress):
    """Drain pending events, instead of displaying a stale step backlog."""
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
            if data.get('prompt_id', pid) == pid and event.get('type') == 'progress':
                maximum = int(data.get('max', 0))
                value = int(data.get('value', 0))
                progress({'stage': 'qwen_image21_sampling', 'current_step': value, 'total_steps': maximum, 'progress': value / maximum if maximum else 0})
        except (ValueError, TypeError, AttributeError):
            continue

def run_task(task, output_dir, main_base, progress):
    if task.cancel_event.is_set():
        raise RuntimeError('Qwen Image 2.1 任务已取消')
    release_idle(main_base)
    pid = getattr(task, 'prompt_id', '')
    if not pid:
        release_idle(BASE)
        wf = build_workflow(task.args)
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
        progress({'prompt_id': pid, 'stage': 'qwen_image21_sampling'})
        deadline = time.monotonic() + 3600
        stopping = False
        try:
            while True:
                if ws is not None:
                    read_progress(ws, pid, progress)
                if (task.cancel_event.is_set() or time.monotonic() > deadline) and not stopping:
                    r = s.post(BASE + '/interrupt', json={}, timeout=10); r.raise_for_status()
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
                            r = s.get(BASE + '/view', params=img, timeout=60); r.raise_for_status()
                            path = Path(output_dir) / (task.name + '-' + uuid.uuid4().hex[:8] + '.png')
                            path.write_bytes(r.content)
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
            if ws is not None:
                ws.close()
            release_idle(BASE)
