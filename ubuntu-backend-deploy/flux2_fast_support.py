"""Four-step FLUX.2 Klein editing, compatible with the existing media queue."""
import json
import os
from pathlib import Path, PurePosixPath

WORKFLOW = 'Flux2Klein-快速图生图'
MODEL = 'flux-2-klein-4b-fp8.safetensors'
BASE_MODEL = 'flux-2-klein-base-4b-fp8.safetensors'
SIZES = ((1024,1024),(1152,864),(864,1152),(1248,832),(832,1248),(1024,576),(576,1024),(1920,1080),(1080,1920))


def _resolve_model_and_sampler():
    comfy_root = Path(os.environ.get('COMFYUI_ROOT', Path(__file__).resolve().parent / 'ComfyUI')).expanduser()
    diffusion_dir = comfy_root / 'models' / 'diffusion_models'
    if (diffusion_dir / MODEL).is_file():
        return MODEL, 4, 1
    if (diffusion_dir / BASE_MODEL).is_file():
        return BASE_MODEL, 20, 5
    return MODEL, 4, 1


def validate(prompt, input_filename, width=1024, height=1024, seed=42):
    if not isinstance(prompt,str) or not prompt.strip() or len(prompt)>8000:
        raise ValueError('请输入提示词（最多8000字符）')
    p=PurePosixPath(input_filename) if isinstance(input_filename,str) else None
    if not input_filename or p is None or p.is_absolute() or '..' in p.parts or '\\' in input_filename or ':' in input_filename:
        raise ValueError('请先上传参考图片')
    if any(isinstance(v,bool) or not isinstance(v,(int,float)) or int(v)!=v for v in (width,height,seed)):
        raise ValueError('尺寸和种子必须为整数')
    width,height,seed=map(int,(width,height,seed))
    if (width,height) not in SIZES or not 0<=seed<=2**63-1:
        raise ValueError('请选择预设尺寸和有效种子')
    return dict(prompt=prompt,input_filename=input_filename,width=width,height=height,seed=seed)


def build_workflow(workflow_name,args):
    a=validate(**args)
    graph=json.loads((Path(__file__).parent/'workflows/image_flux2_klein_image_edit_4b_base.json').read_text(encoding='utf-8'))
    model_name, steps, cfg = _resolve_model_and_sampler()
    graph['76']['inputs']['image']=a['input_filename']
    graph['75:70']['inputs']['unet_name']=model_name
    graph['75:74']['inputs']['text']=a['prompt']
    graph['75:73']['inputs']['noise_seed']=a['seed']
    graph['75:62']['inputs']['steps']=steps
    graph['75:63']['inputs']['cfg']=cfg
    w,h=((a['width']+31)//32*32,(a['height']+31)//32*32)
    graph['75:80']={'class_type':'ImageScale','inputs':{'image':['76',0],'upscale_method':'lanczos','width':w,'height':h,'crop':'center'}}
    if (w,h)!=(a['width'],a['height']):
        graph['fullhd-crop']={'class_type':'ImageCrop','inputs':{'image':['75:65',0],'width':a['width'],'height':a['height'],'x':(w-a['width'])//2,'y':(h-a['height'])//2}}
        graph['9']['inputs']['images']=['fullhd-crop',0]
    graph['9']['inputs']['filename_prefix']='Flux2-Klein-4b-fast'
    return graph
