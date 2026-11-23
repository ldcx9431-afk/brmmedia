"""FLUX 3: official prompt enhancement and an owned, temporary GPU0 runtime."""
import io
import json
import math
import os
from pathlib import Path, PurePosixPath
import signal
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

import requests

TEXT = 'Flux3Klein-文生图'
EDIT = 'Flux3Klein-单图编辑'
WORKFLOWS = (TEXT, EDIT)
SIZES = ((1024,1024),(1152,864),(864,1152),(1248,832),(832,1248),(1024,576),(576,1024))
TIMEOUT = 3600
BASE = 'http://127.0.0.1:8192'
PYTHON = '/srv/brmmedia/h3-image/venv/bin/python'
COMFY = '/srv/brmmedia/h3-image/ComfyUI'
EXTRA_MODELS = '/srv/brmmedia/model-iteration-20261114/flux3-candidate/extra_models.yaml'
RUNTIME = Path('/mnt/d/brmmedia/flux3-runtime')


def enabled():
    return os.environ.get('BRM_FLUX3_IMAGE_ENABLED') == '1'


def validate(prompt, width=1024, height=1024, seed=42, input_filename=None):
    if not isinstance(prompt,str) or not prompt.strip() or len(prompt)>8000:
        raise ValueError('请输入提示词（最多8000字符）')
    for value in (width,height,seed):
        if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or int(value)!=value:
            raise ValueError('尺寸和种子必须为整数')
    width,height,seed=map(int,(width,height,seed))
    if (width,height) not in SIZES or not 0<=seed<=2**63-1:
        raise ValueError('请选择约1MP以内的预设尺寸和有效种子')
    args=dict(prompt=prompt,width=width,height=height,seed=seed)
    if input_filename is not None:
        path=PurePosixPath(input_filename) if isinstance(input_filename,str) else None
        if not input_filename or path is None or path.is_absolute() or '..' in path.parts or '\\' in input_filename or ':' in input_filename:
            raise ValueError('请先上传单张参考图片')
        args['input_filename']=input_filename
    return args


def build_workflow(args, phase, caption='', image_name=None):
    mode='edit' if args.get('input_filename') else 'text'
    graph=json.loads((Path(__file__).parent/'workflows'/f'flux3_{mode}_{phase}.api.json').read_text(encoding='utf-8'))
    if phase=='enhance':
        graph['2']['inputs'].update(prompt=args['prompt'])
        graph['2']['inputs']['sampling_mode.seed']=args['seed']
    else:
        graph['4']['inputs']['text']=caption
        graph['6']['inputs'].update(width=args['width'],height=args['height'])
        graph['7']['inputs']['seed']=args['seed']
        graph['9']['inputs']['filename_prefix']='Flux3/'+uuid.uuid4().hex
    if image_name:
        graph['10']['inputs']['image']=image_name
    return graph


def _check(task, deadline, proc=None):
    if task.cancel_event.is_set():
        raise RuntimeError('FLUX 3任务已取消')
    if time.monotonic()>=deadline:
        raise TimeoutError('FLUX 3任务超过1小时预算，已停止')
    if proc is not None and proc.poll() is not None:
        raise RuntimeError('FLUX 3运行时已退出，任务不会自动重复提交')


def _request(session,method,path,**kwargs):
    r=session.request(method,BASE+path,timeout=30 if method=='POST' and path=='/prompt' else 15,**kwargs)
    r.raise_for_status()
    if path=='/free' and not r.content:
        return {}
    return r.json()


def _execute(session, task, graph, phase, deadline, proc, progress):
    pid=str(uuid.uuid4());client=uuid.uuid4().hex;ws=None
    # Persist ownership before POST; a lost acknowledgement never causes replay.
    progress({'prompt_id':pid,'stage':'flux3_'+phase,'current_step':None,'total_steps':None})
    try:
        try:
            import websocket
            ws=websocket.create_connection(BASE.replace('http://','ws://')+'/ws?clientId='+client,timeout=5)
            ws.settimeout(.02)
        except Exception:
            ws=None
        submitted=_request(session,'POST','/prompt',json={'prompt':graph,'prompt_id':pid,'client_id':client})
        if submitted.get('prompt_id')!=pid:
            raise RuntimeError('FLUX 3运行时返回的任务ID不匹配')
        while True:
            _check(task,deadline,proc)
            if ws:
                for _ in range(128):
                    try:
                        event=json.loads(ws.recv());data=event.get('data') or {}
                    except Exception:
                        break
                    if data.get('prompt_id')==pid and event.get('type')=='progress' and phase=='generate':
                        maximum=int(data.get('max',16));value=int(data.get('value',0))
                        progress({'stage':'flux3_sampling','current_step':value,'total_steps':maximum,'progress':.25+.7*value/max(1,maximum)})
            entry=_request(session,'GET','/history/'+pid).get(pid)
            if entry:
                if entry.get('status',{}).get('status_str')!='success':
                    raise RuntimeError('FLUX 3执行失败，请查看本地运行记录')
                return entry,pid
            time.sleep(.5)
    finally:
        if ws:
            try:ws.close()
            except Exception:pass


def run_task(task, output_dir, main_base, progress):
    from media_runtime_guard import lease, handover, release_all
    from qwen3_tts_support import stop_owned
    from h3_image_support import transfer_input
    from PIL import Image, PngImagePlugin
    # A worker restart kills this task's child runtime. Never restart its graph.
    if task.prompt_id:
        raise RuntimeError('工作台重启后原FLUX 3运行时已结束；未重复生成，请核对后重新提交')
    args=validate(**task.args)
    if task.workflow_name==EDIT and not args.get('input_filename'):
        raise ValueError('单图编辑需要参考图片')
    deadline=time.monotonic()+TIMEOUT
    proc=None
    RUNTIME.mkdir(parents=True,exist_ok=True)
    with lease():
        handover(main_base)
        try:
            _check(task,deadline)
            with tempfile.TemporaryDirectory(prefix='task-',dir=RUNTIME) as temporary:
                root=Path(temporary)
                for name in ('input','output','temp'):(root/name).mkdir()
                with requests.Session() as session:
                    session.trust_env=False
                    try:
                        session.get(BASE+'/system_stats',timeout=2)
                    except requests.ConnectionError:
                        pass
                    else:
                        raise RuntimeError('FLUX 3端口已有运行时，拒绝接管其他进程')
                    progress({'stage':'flux3_starting','progress':0})
                    config=root/'runtime.json';config.write_text(json.dumps({'parent_pid':os.getpid(),'root':str(root)}));config.chmod(0o600)
                    env={**os.environ,'CUDA_VISIBLE_DEVICES':'0','OMP_NUM_THREADS':'8','MKL_NUM_THREADS':'8','OPENBLAS_NUM_THREADS':'8','PYTHONUNBUFFERED':'1','HF_HUB_OFFLINE':'1','TRANSFORMERS_OFFLINE':'1','XDG_CACHE_HOME':'/mnt/d/brmmedia/cache/flux3-xdg','HF_HOME':'/mnt/d/brmmedia/cache/flux3-hf'}
                    with (root/'runtime.log').open('wb') as log:
                        proc=subprocess.Popen([PYTHON,str(Path(__file__).resolve()),'--runtime',str(config)],cwd=COMFY,env=env,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
                        try:
                            startup=min(deadline,time.monotonic()+180)
                            while True:
                                _check(task,deadline,proc)
                                try:
                                    _request(session,'GET','/system_stats');break
                                except requests.RequestException:
                                    if time.monotonic()>=startup:raise TimeoutError('FLUX 3运行时启动超时')
                                    time.sleep(.5)
                            image_name=transfer_input(session,main_base,args['input_filename'],target_base=BASE) if args.get('input_filename') else None
                            _check(task,deadline,proc)
                            enhanced,enhance_id=_execute(session,task,build_workflow(args,'enhance',image_name=image_name),'enhance',deadline,proc,progress)
                            texts=enhanced['outputs']['3']['text'];caption=texts[0] if isinstance(texts,list) else texts
                            if not isinstance(caption,str) or len(caption.strip())<40:raise RuntimeError('官方提示词增强器未输出有效描述')
                            _request(session,'POST','/free',json={'unload_models':True,'free_memory':True})
                            result,generate_id=_execute(session,task,build_workflow(args,'generate',caption,image_name),'generate',deadline,proc,progress)
                            _check(task,deadline,proc)
                            images=result.get('outputs',{}).get('9',{}).get('images',[])
                            if len(images)!=1:raise RuntimeError('FLUX 3未输出恰好一张图片')
                            response=session.get(BASE+'/view',params=images[0],timeout=60);response.raise_for_status()
                            metadata=PngImagePlugin.PngInfo()
                            provenance={'task_id':getattr(task,'id',None),'original_prompt':args['prompt'],'enhanced_prompt':caption,'enhance_prompt_id':enhance_id,'generate_prompt_id':generate_id,'seed':args['seed'],'steps':16,'width':args['width'],'height':args['height'],'revision':'9d36114cf7a2e03a45c28f5cc4987d20ebfdb6c1'}
                            metadata.add_text('flux3_provenance',json.dumps(provenance,ensure_ascii=False))
                            destination=Path(output_dir)/(task.name+'-'+uuid.uuid4().hex[:8]+'.png')
                            with Image.open(io.BytesIO(response.content)) as image:
                                if image.format!='PNG' or image.size!=(args['width'],args['height']):raise RuntimeError('FLUX 3输出尺寸不符合请求')
                                image.load()
                                partial=destination.with_name(destination.name+'.partial')
                                image.save(partial,format='PNG',pnginfo=metadata)
                                partial.replace(destination)
                            return [str(destination)]
                        except BaseException:
                            stop_owned(proc)
                            try:
                                errors=Path('/srv/brmmedia/flux3-image/errors')
                                errors.mkdir(parents=True,exist_ok=True,mode=0o700)
                                identifier=str(getattr(task,'id',''))
                                if len(identifier)!=32 or any(c not in '0123456789abcdef' for c in identifier):identifier=uuid.uuid4().hex
                                error_log=errors/(identifier+'.log')
                                shutil.copyfile(root/'runtime.log',error_log)
                                error_log.chmod(0o600)
                            except OSError:
                                pass
                            raise
                        finally:
                            stop_owned(proc)
        finally:
            # Stop only this job's process group, then verify GPU0 is free.
            if proc is not None:stop_owned(proc)
            release_all(main_base)


def _runtime(path):
    import ctypes
    config=json.loads(Path(path).read_text())
    if ctypes.CDLL(None).prctl(1,signal.SIGTERM)!=0:
        raise RuntimeError('Cannot establish runtime parent ownership')
    if os.getppid()!=config['parent_pid']:
        raise RuntimeError('Owning worker exited before runtime startup')
    root=Path(config['root'])
    os.execv(PYTHON,[PYTHON,str(Path(COMFY)/'main.py'),'--listen','127.0.0.1','--port','8192','--extra-model-paths-config',EXTRA_MODELS,'--input-directory',str(root/'input'),'--output-directory',str(root/'output'),'--temp-directory',str(root/'temp'),'--disable-all-custom-nodes','--offline','--cache-none','--reserve-vram','5','--disable-pinned-memory'])


if __name__=='__main__':
    if len(sys.argv)!=3 or sys.argv[1]!='--runtime':raise SystemExit('Internal runtime launcher only')
    _runtime(sys.argv[2])
