"""Isolated Qwen3-TTS child process owned by the existing media worker."""
import json,math,os,signal,subprocess,tempfile,time,uuid
from pathlib import Path,PurePosixPath

WORKFLOW='Qwen3TTS-语音克隆'
PYTHON='/srv/brmmedia/qwen3-tts/venv/bin/python'
MODEL='/mnt/d/brmmedia/models/Qwen3-TTS-1.7B-Base'
LANGUAGES={'zh':'Chinese','en':'English','ja':'Japanese','ko':'Korean','de':'German','fr':'French','ru':'Russian','pt':'Portuguese','es':'Spanish','it':'Italian'}
MAX_REFERENCE_BYTES=50*1024*1024
MAX_REFERENCE_SECONDS=60

def enabled():return os.environ.get('BRM_QWEN3_TTS_ENABLED')=='1'

def validate(text,input_filename,language='zh',reference_text=''):
    if not isinstance(text,str) or not text.strip() or len(text)>600:raise ValueError('合成文字须为1–600字符')
    if language not in LANGUAGES:raise ValueError('不支持的语音语言')
    if not isinstance(reference_text,str) or len(reference_text)>2000:raise ValueError('参考文字最多2000字符')
    p=PurePosixPath(input_filename) if isinstance(input_filename,str) else None
    if not input_filename or p is None or p.is_absolute() or '..' in p.parts or '\\' in input_filename or ':' in input_filename:raise ValueError('请先上传参考音频')
    return dict(text=text,input_filename=input_filename,language=language,reference_text=reference_text)

def stop_owned(proc):
    if proc.poll() is not None:return
    try:os.killpg(proc.pid,signal.SIGTERM)
    except ProcessLookupError:return
    try:proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        try:os.killpg(proc.pid,signal.SIGKILL)
        except ProcessLookupError:pass
        proc.wait(timeout=10)

def check_reference(input_filename,input_dir):
    root=Path(input_dir).resolve();reference=(root/input_filename).resolve()
    if not reference.is_relative_to(root) or not reference.is_file():raise ValueError('参考音频不存在，请重新上传')
    if reference.stat().st_size>MAX_REFERENCE_BYTES:raise ValueError('参考音频不能超过50MiB')
    try:
        result=subprocess.run(['ffprobe','-v','error','-show_entries','format=duration','-of','default=noprint_wrappers=1:nokey=1',str(reference)],check=True,capture_output=True,text=True,timeout=10)
        duration=float(result.stdout.strip())
    except (OSError,subprocess.SubprocessError,ValueError) as exc:raise ValueError('无法读取参考音频，请上传有效的人声音频') from exc
    if not math.isfinite(duration) or not 1<=duration<=MAX_REFERENCE_SECONDS:raise ValueError('参考音频须为1–60秒，建议使用10–30秒清晰人声')
    return reference

def run_task(task,output_dir,input_dir,progress):
    a=validate(**task.args)
    if task.cancel_event.is_set():raise RuntimeError('语音任务已取消')
    reference=check_reference(a['input_filename'],input_dir)
    name=task.name+'-'+uuid.uuid4().hex[:8]+'.wav';target=Path(output_dir)/name
    with tempfile.TemporaryDirectory(prefix='brm-qwen3-tts-') as tmp:
        request=Path(tmp)/'request.json';log=Path(tmp)/'runner.log'
        request.write_text(json.dumps({**a,'reference':str(reference),'output':str(target),'parent_pid':os.getpid()},ensure_ascii=False),encoding='utf-8');request.chmod(0o600)
        threads=str(max(1,min(16,int(os.environ.get('BRM_QWEN3_TTS_CPU_THREADS','8')))))
        env={**os.environ,'CUDA_VISIBLE_DEVICES':'0','HF_HUB_OFFLINE':'1','TRANSFORMERS_OFFLINE':'1','PYTHONUNBUFFERED':'1','OMP_NUM_THREADS':threads,'MKL_NUM_THREADS':threads,'OPENBLAS_NUM_THREADS':threads}
        started=time.monotonic();progress({'stage':'qwen3_tts_loading'})
        with log.open('wb') as stream:
            proc=subprocess.Popen([PYTHON,str(Path(__file__).resolve()),str(request)],stdout=stream,stderr=stream,env=env,start_new_session=True)
            try:
                while proc.poll() is None:
                    if task.cancel_event.is_set():raise RuntimeError('语音任务已取消')
                    if time.monotonic()-started>3600:raise TimeoutError('语音任务超过1小时预算')
                    time.sleep(.25)
                if proc.returncode:raise RuntimeError('Qwen3-TTS生成失败：'+log.read_text(errors='replace')[-1000:])
                if not target.is_file() or target.stat().st_size<1024:raise RuntimeError('Qwen3-TTS未输出有效音频')
            except BaseException:
                stop_owned(proc);target.unlink(missing_ok=True);target.with_name(target.stem+'.partial.wav').unlink(missing_ok=True);raise
    progress({'stage':'completed','progress':1.0})
    return [str(target)]

def runner(request_path):
    # Child exits with its worker even when the workbench restarts. Never replay
    # an ambiguous subprocess generation as a fresh task after a restart.
    import ctypes
    ctypes.CDLL(None).prctl(1,signal.SIGTERM)
    a=json.loads(Path(request_path).read_text(encoding='utf-8'))
    if os.getppid()!=a['parent_pid']:raise RuntimeError('Worker ended before runner startup')
    import torch,soundfile as sf,numpy as np
    from qwen_tts import Qwen3TTSModel
    model=Qwen3TTSModel.from_pretrained(MODEL,device_map='cuda:0',dtype=torch.bfloat16,attn_implementation='sdpa',local_files_only=True)
    kwargs={'ref_audio':a['reference'],'x_vector_only_mode':not bool(a['reference_text'].strip())}
    if a['reference_text'].strip():kwargs['ref_text']=a['reference_text']
    wavs,sr=model.generate_voice_clone(text=a['text'],language=LANGUAGES[a['language']],max_new_tokens=2048,do_sample=False,**kwargs)
    audio=np.asarray(wavs[0]);assert np.isfinite(audio).all() and len(audio)>sr*.15 and np.max(np.abs(audio))>.005,'Empty or invalid speech'
    target=Path(a['output']);part=target.with_name(target.stem+'.partial.wav');sf.write(str(part),audio,sr);os.replace(part,target)

if __name__=='__main__':
    import sys
    runner(sys.argv[1])
