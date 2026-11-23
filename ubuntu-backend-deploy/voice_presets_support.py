"""Shared, verified reference voices for both local voice-cloning interfaces."""
import hashlib
import json
import math
import os
import re
import shutil
import tempfile
import wave
from functools import lru_cache
from pathlib import Path

ROOT=Path(os.environ.get('BRM_VOICE_PRESETS_DIR',Path(__file__).parent/'voice_presets'))

CSS="""
.voice-clone-panel {gap:16px!important; padding:4px 2px!important;}
.voice-clone-heading h2 {margin:0 0 6px!important; font-size:22px!important;}
.voice-clone-heading p,.voice-clone-hint p {color:#806963; font-size:14px; line-height:1.6;}
.voice-clone-row {align-items:flex-start!important; gap:24px!important;}
.voice-text-column,.voice-reference-column {height:auto!important; min-height:0!important; gap:12px!important;}
.voice-clone-text textarea {min-height:232px!important; max-height:390px; resize:vertical;}
.voice-clone-hint {margin:0!important; padding:0 2px!important;}
.voice-preset-description {padding:10px 12px!important; border:1px solid #f3ded6; border-radius:10px; background:#fff7f3; min-height:42px;}
.voice-preset-description p {margin:0!important; color:#72524b; font-size:13px; line-height:1.6;}
.voice-reference-audio {min-height:150px!important;}
.voice-clone-controls {align-items:flex-end!important; gap:24px!important;}
.voice-control-hint {padding:10px 4px!important; color:#806963; font-size:14px;}
.voice-clone-submit {width:100%!important; min-height:46px!important; margin-top:0!important;}
@media(max-width:760px){.voice-clone-row,.voice-clone-controls{gap:14px!important;}.voice-clone-text textarea{min-height:190px!important;}}
"""


@lru_cache(maxsize=2)
def load_catalog(root):
    root=Path(root).resolve()
    if not (root/'catalog.json').is_file():return ()
    catalog=json.loads((root/'catalog.json').read_text(encoding='utf-8'))
    voices=catalog['voices'];seen=set();result=[]
    for voice in voices:
        ident=voice['id']
        if not re.fullmatch(r'[a-z][a-z0-9-]{2,63}',ident) or ident in seen:raise ValueError('音色目录标识无效')
        seen.add(ident)
        path=(root/voice['file']).resolve()
        if not path.is_relative_to(root) or path.suffix!='.wav' or not path.is_file():raise ValueError('内置音色文件缺失或路径无效')
        if hashlib.sha256(path.read_bytes()).hexdigest()!=voice['sha256']:raise ValueError('内置音色校验失败')
        with wave.open(str(path),'rb') as audio:
            duration=audio.getnframes()/audio.getframerate()
            if audio.getnchannels()!=1 or audio.getsampwidth()!=2 or audio.getframerate()!=24000:raise ValueError('内置音色格式无效')
        if not math.isfinite(duration) or not 1<=duration<=60:raise ValueError('内置音色时长无效')
        result.append({**voice,'duration':duration,'path':path})
    return tuple(result)


def choices():
    return [('自定义上传参考音频','')]+[(v['label']+' · '+v['description'],v['id']) for v in load_catalog(ROOT)]


def get_voice(ident):
    for voice in load_catalog(ROOT):
        if voice['id']==ident:return voice
    raise ValueError('请选择有效的内置音色，或上传参考音频')


def stage_voice(ident,input_dir):
    """Use an immutable content-addressed input; never rewrite an active job's audio."""
    voice=get_voice(ident)
    root=Path(input_dir).resolve()
    # IndexTTS's existing reference resolver deliberately accepts basenames only.
    relative=Path('brm-voice-'+ident+'-'+voice['sha256'][:16]+'.wav')
    target=root/relative
    if not target.resolve().is_relative_to(root):raise ValueError('内置音色输入路径无效')
    target.parent.mkdir(parents=True,exist_ok=True)
    if not target.is_file() or hashlib.sha256(target.read_bytes()).hexdigest()!=voice['sha256']:
        with tempfile.NamedTemporaryFile(dir=target.parent,prefix='.voice-',suffix='.partial',delete=False) as stream:
            temporary=Path(stream.name)
        try:
            shutil.copyfile(voice['path'],temporary)
            if hashlib.sha256(temporary.read_bytes()).hexdigest()!=voice['sha256']:raise ValueError('内置音色复制校验失败')
            temporary.chmod(0o644);os.replace(temporary,target)
        finally:temporary.unlink(missing_ok=True)
    return relative.as_posix(),voice
