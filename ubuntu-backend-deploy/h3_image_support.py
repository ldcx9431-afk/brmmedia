"""MiniMax H3 Image: isolated runtime, owned prompts, shared media worker."""
import io
import math
import os
from pathlib import Path, PurePosixPath
import time
import uuid
import logging

import requests

BASE = os.environ.get('BRM_H3_IMAGE_URL', 'http://127.0.0.1:8191').rstrip('/')
TEXT = 'MiniMaxH3Image-文生图'
EDIT = 'MiniMaxH3Image-单图编辑'
WORKFLOWS = (TEXT, EDIT)
MODEL = 'minimax_h3_image_pruned_int8_convrot.safetensors'
CLIP = 'qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors'
VAE = 'minimax_h3_image_vae_fp16.safetensors'
MAX_PIXELS = 4194304
FULL_HD_SIZES = ((1920, 1080), (1080, 1920))
TIMEOUT = 3600


def enabled():
    return os.environ.get('BRM_H3_IMAGE_ENABLED') == '1'


def runtime_ready():
    return enabled() or os.environ.get('BRM_H3_IMAGE_RUNTIME_READY') == '1'


def validate(prompt, width=2048, height=2048, seed=0, input_filename=None):
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 12000:
        raise ValueError('请输入提示词（最多12000字符），推荐详细英文描述')
    values = (width, height, seed)
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or int(v) != v for v in values):
        raise ValueError('宽度、高度及种子必须为整数')
    width, height, seed = map(int, values)
    if (width, height) not in FULL_HD_SIZES and (min(width, height) < 256 or max(width, height) > 4096 or width % 32 or height % 32 or width * height > MAX_PIXELS):
        raise ValueError('尺寸须为32的倍数、256–4096px且不超过4,194,304像素')
    if not 0 <= seed <= 2**63 - 1:
        raise ValueError('种子须为0–2^63-1')
    result = dict(prompt=prompt, width=width, height=height, seed=seed)
    if input_filename is not None:
        path = PurePosixPath(input_filename) if isinstance(input_filename, str) else None
        if not input_filename or path is None or path.is_absolute() or '..' in path.parts or '\\' in input_filename or ':' in input_filename:
            raise ValueError('请先上传单张参考图片')
        result['input_filename'] = input_filename
    return result


def build_workflow(args):
    a = validate(**args)
    width, height = ((a['width'] + 31) // 32 * 32, (a['height'] + 31) // 32 * 32)
    conditioning = dict(clip=['2', 0], vae=['3', 0], prompt=a['prompt'], width=width, height=height)
    graph = {
        '1': {'class_type': 'UNETLoader', 'inputs': {'unet_name': MODEL, 'weight_dtype': 'default'}},
        '2': {'class_type': 'CLIPLoader', 'inputs': {'clip_name': CLIP, 'type': 'minimax', 'device': 'default'}},
        '3': {'class_type': 'VAELoader', 'inputs': {'vae_name': VAE}},
        '4': {'class_type': 'MiniMaxH3ImageConditioning', 'inputs': conditioning},
        '5': {'class_type': 'ConditioningZeroOut', 'inputs': {'conditioning': ['4', 0]}},
        '6': {'class_type': 'KSampler', 'inputs': {'model': ['1', 0], 'seed': a['seed'], 'steps': 6, 'cfg': 1.0,
              'sampler_name': 'euler', 'scheduler': 'simple', 'positive': ['4', 0], 'negative': ['5', 0], 'latent_image': ['4', 1], 'denoise': 1.0}},
        '7': {'class_type': 'VAEDecode', 'inputs': {'samples': ['6', 0], 'vae': ['3', 0]}},
        '8': {'class_type': 'SaveImage', 'inputs': {'images': ['7', 0], 'filename_prefix': 'H3Image/research'}},
    }
    if a.get('input_filename'):
        graph['9'] = {'class_type': 'LoadImage', 'inputs': {'image': a['input_filename']}}
        conditioning['images.image_1'] = ['9', 0]
    if (a['width'], a['height']) in FULL_HD_SIZES:
        graph['11'] = {'class_type': 'ImageCrop', 'inputs': {'image': ['7', 0], 'width': a['width'], 'height': a['height'], 'x': (width-a['width'])//2, 'y': (height-a['height'])//2}}
        graph['8']['inputs']['images'] = ['11', 0]
    return graph


def session():
    s = requests.Session()
    s.trust_env = False
    return s


def check_available():
    with session() as s:
        s.get(BASE + '/system_stats', timeout=5).raise_for_status()


def cancel(s, pid):
    s.post(BASE + '/queue', json={'delete': [pid]}, timeout=10).raise_for_status()
    s.post(BASE + '/interrupt', json={'prompt_id': pid}, timeout=10).raise_for_status()


def queued(q, pid):
    return any(row[1] == pid for key in ('queue_running', 'queue_pending') for row in q.get(key, []))


def _json_get(s, url):
    r = s.get(url, timeout=15)
    r.raise_for_status()
    return r.json()


def transfer_input(s, source_base, filename, target_base=None):
    """Assets are already uploaded to the main runtime; never accept OS paths."""
    path = PurePosixPath(filename)
    r = s.get(source_base + '/view', params={'filename': path.name, 'subfolder': str(path.parent) if path.parent != PurePosixPath('.') else '', 'type': 'input'}, timeout=60)
    r.raise_for_status()
    data = r.content
    from PIL import Image
    with Image.open(io.BytesIO(data)) as image:
        image.verify()
    r = s.post((target_base or BASE) + '/upload/image', files={'image': (uuid.uuid4().hex + '.png', data)}, data={'type': 'input', 'overwrite': 'false'}, timeout=60)
    r.raise_for_status()
    uploaded = r.json()
    return str(PurePosixPath(uploaded.get('subfolder') or '') / uploaded['name'])


def run_task(task, output_dir, main_base, progress):
    from media_runtime_guard import lease
    with lease():
        return _run_task(task, output_dir, main_base, progress)


def _run_task(task, output_dir, main_base, progress):
    from media_runtime_guard import handover, release_all
    if task.cancel_event.is_set():
        raise RuntimeError('H3 Image 任务已取消')
    pid = task.prompt_id or ''
    handover(main_base, target=BASE, owned_prompt=pid)
    stopping = False
    timed_out = False
    wall_deadline = task.args.get('h3_deadline_ts', time.time() + TIMEOUT)
    deadline = time.monotonic() + max(0, wall_deadline - time.time())
    ws = None
    try:
        with session() as s:
            if pid:
                history = _json_get(s, BASE + '/history/' + pid)
                if pid not in history and not queued(_json_get(s, BASE + '/queue'), pid):
                    raise RuntimeError('原H3 Image任务已丢失，拒绝自动重复生成')
            else:
                args = validate(**task.args)
                if task.workflow_name == EDIT:
                    if not args.get('input_filename'):
                        raise ValueError('单图编辑需要参考图片')
                    args['input_filename'] = transfer_input(s, main_base, args['input_filename'])
                graph = build_workflow(args)
                pid = str(uuid.uuid4())
                task.args['h3_deadline_ts'] = wall_deadline
                # Persist the client-owned ID before POST: ambiguous timeouts cannot cause resubmission.
                progress({'prompt_id': pid, 'stage': 'submitting'})
                client = uuid.uuid4().hex
                try:
                    import websocket
                    ws = websocket.create_connection(BASE.replace('http://', 'ws://') + '/ws?clientId=' + client, timeout=5)
                    ws.settimeout(0.02)
                except Exception:
                    ws = None
                r = s.post(BASE + '/prompt', json={'prompt': graph, 'prompt_id': pid, 'client_id': client}, timeout=30)
                r.raise_for_status()
                if r.json().get('prompt_id') != pid:
                    raise RuntimeError('H3 Image运行时返回了不匹配的任务ID')
            progress({'prompt_id': pid, 'stage': 'h3_image_sampling'})
            while True:
                if ws is not None:
                    import json
                    latest = None
                    for _ in range(256):
                        try:
                            event = json.loads(ws.recv())
                            data = event.get('data') or {}
                            if data.get('prompt_id') == pid:
                                if event.get('type') == 'progress':
                                    maximum = int(data.get('max', 6))
                                    value = int(data.get('value', 0))
                                    latest = {'stage': 'h3_image_sampling', 'current_step': value, 'total_steps': maximum, 'progress': value / max(maximum, 1)}
                                elif event.get('type') == 'executing':
                                    stage = {'2': 'loading_text_encoder', '4': 'encoding_prompt', '6': 'h3_image_sampling', '7': 'decoding_image', '8': 'saving_artifacts'}.get(data.get('node'))
                                    if stage:
                                        latest = {'stage': stage}
                        except Exception:
                            break
                    if latest:
                        progress(latest)
                if task.cancel_event.is_set() or time.monotonic() >= deadline:
                    if not stopping:
                        timed_out = not task.cancel_event.is_set()
                        cancel(s, pid)
                        stopping = True
                entry = _json_get(s, BASE + '/history/' + pid).get(pid)
                if entry:
                    if stopping:
                        if timed_out:
                            raise TimeoutError('H3 Image超过1小时预算，已停止任务')
                        raise RuntimeError('H3 Image任务已取消')
                    if entry.get('status', {}).get('status_str') != 'success':
                        raise RuntimeError('H3 Image推理失败：' + str(entry.get('status', {}).get('messages', []))[-1800:])
                    paths = []
                    from PIL import Image
                    for out in entry.get('outputs', {}).values():
                        for img in out.get('images', []):
                            r = s.get(BASE + '/view', params=img, timeout=60)
                            r.raise_for_status()
                            with Image.open(io.BytesIO(r.content)) as image:
                                if image.size != (task.args['width'], task.args['height']):
                                    raise RuntimeError('H3 Image输出尺寸不符合请求')
                                image.verify()
                            path = Path(output_dir) / (task.name + '-' + uuid.uuid4().hex[:8] + '.png')
                            path.write_bytes(r.content)
                            paths.append(str(path))
                    if len(paths) != 1:
                        raise RuntimeError('H3 Image未输出恰好一张图片')
                    return paths
                if stopping and not queued(_json_get(s, BASE + '/queue'), pid):
                    if timed_out:
                        raise TimeoutError('H3 Image超过1小时预算，已停止任务')
                    raise RuntimeError('H3 Image任务已取消')
                time.sleep(1)
    finally:
        if ws:
            try:
                ws.close()
            except Exception:
                logging.warning('H3 Image websocket close failed; continuing owned-prompt cleanup')
        # Fail closed: a cleanup error must prevent the worker from claiming safe handover.
        if pid:
            with session() as s:
                cancel(s, pid)
                stop_deadline = time.monotonic() + 120
                while queued(_json_get(s, BASE + '/queue'), pid):
                    if time.monotonic() >= stop_deadline:
                        raise RuntimeError('H3 Image未能停止，禁止GPU交接')
                    time.sleep(0.5)
        release_all(main_base)
