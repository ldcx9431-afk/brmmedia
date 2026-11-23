"""Pinned Comfy-Org Qwen Image 2.1 weights; research/evaluation use only."""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
import urllib.request

REVISION = 'df94239739eef7205973a145ffa6f441f7b64e84'
FILES = {
    'diffusion_models/qwen_image_2.1_int8_convrot.safetensors': (7256783064, 'cb74113cb03faecd79611b01fd7fd642f0aa60d6f0b95086abee214d75eaa57d'),
    'text_encoders/qwen3vl_8b_int8_convrot.safetensors': (9350798360, '8bfd0f6e12abf2d2d697ecc888e5e90b0d6741d6708f05799f53afa560452e8f'),
    'vae/qwen_image_2.1_vae_bf16.safetensors': (675509688, 'bb21f7473051e1ac368515dd3f2e15cd44d7a11748ee8823e1ddca3e4876b7c9'),
}

def checksum(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(8 * 1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()

def main():
    p = argparse.ArgumentParser()
    p.add_argument('--models', type=Path, required=True)
    p.add_argument('--only', choices=['diffusion_models', 'text_encoders', 'vae'])
    p.add_argument('--parallel', action='store_true', help='Download the three components in separate processes')
    a = p.parse_args()
    if a.parallel:
        processes = [subprocess.Popen([sys.executable, '-u', __file__, '--models', str(a.models), '--only', kind])
                     for kind in ('diffusion_models', 'text_encoders', 'vae')]
        results = [process.wait() for process in processes]
        if any(results):
            raise RuntimeError(f'Component download failed: exit statuses {results}')
        print('All three components verified', flush=True)
        return
    for name, (size, digest) in FILES.items():
        if a.only and not name.startswith(a.only + '/'):
            continue
        target = a.models / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if target.stat().st_size == size and checksum(target) == digest:
                print('verified', name, flush=True)
                continue
            raise RuntimeError(f'Refusing to overwrite unverified existing file: {target}')
        part = target.with_suffix(target.suffix + '.partial')
        offset = part.stat().st_size if part.exists() else 0
        url = f'https://huggingface.co/Comfy-Org/Qwen-Image-2.1/resolve/{REVISION}/{name}'
        if shutil.which('aria2c'):
            subprocess.run([
                'aria2c', '--continue=true', '--split=8', '--max-connection-per-server=8',
                '--min-split-size=64M', '--file-allocation=none', '--auto-file-renaming=false',
                '--summary-interval=60', '--console-log-level=warn', '--download-result=hide',
                '--max-tries=5', '--retry-wait=10', '--timeout=90', '--connect-timeout=30',
                '--checksum=sha-256=' + digest, '--dir=' + str(part.parent), '--out=' + part.name, url,
            ], check=True)
            if part.stat().st_size != size or checksum(part) != digest:
                raise RuntimeError('aria2 download did not pass independent SHA256 validation')
            part.replace(target)
            print('verified', name, flush=True)
            continue
        req = urllib.request.Request(url, headers={'Range': f'bytes={offset}-'} if offset else {})
        with urllib.request.urlopen(req, timeout=90) as response:
            if offset and (response.status != 206 or not response.headers.get('Content-Range', '').startswith(f'bytes {offset}-')):
                raise RuntimeError('Server did not honor resume range; partial retained')
            print('downloading', name, 'offset', offset, flush=True)
            with part.open('ab' if offset else 'wb') as f:
                last = offset
                for chunk in iter(lambda: response.read(8 * 1024 * 1024), b''):
                    f.write(chunk)
                    offset += len(chunk)
                    if offset - last >= 512 * 1024 * 1024:
                        print(name, offset, '/', size, flush=True)
                        last = offset
                f.flush()
                os.fsync(f.fileno())
        if part.stat().st_size != size or checksum(part) != digest:
            raise RuntimeError(f'Size/SHA256 mismatch: {name}; partial retained')
        part.replace(target)
        print('verified', name, flush=True)
    print(json.dumps({'revision': REVISION, 'verified': [name for name in FILES if not a.only or name.startswith(a.only + '/')]}), flush=True)

if __name__ == '__main__':
    main()
