#!/usr/bin/env python3
"""Loopback-only IndexTTS-2.5 inference service.

This module intentionally runs in its own Python 3.11 environment.  ComfyUI
uses Python 3.12 and must stay insulated from IndexTTS-2.5's Torch/CUDA
dependency set.  Nginx never proxies this port: the BRM backend is the sole
client and converts the returned 22.05 kHz WAV to its normal MP3 artifact.
"""

from __future__ import annotations

import asyncio
import gc
import os
import shutil
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import Response


SERVICE_NAME = "IndexTTS-2.5"
SUPPORTED_LANGUAGES = {"zh", "en", "ja", "es", "ar"}
MAX_TEXT_CHARS = max(1, int(os.environ.get("INDEXTTS25_MAX_TEXT_CHARS", "12000")))
MAX_REFERENCE_BYTES = max(
    1_000_000, int(os.environ.get("INDEXTTS25_MAX_REFERENCE_BYTES", str(256 * 1024 * 1024)))
)
MODEL_DIR = Path(os.environ.get("INDEXTTS25_MODEL_DIR", "")).expanduser().resolve()
RUN_DIR = Path(os.environ.get("INDEXTTS25_RUN_DIR", "/tmp/indextts25")).expanduser().resolve()
USE_BF16 = os.environ.get("INDEXTTS25_USE_BF16", "1").strip().lower() not in {"0", "false", "no"}
USE_CUDA_KERNEL = os.environ.get("INDEXTTS25_USE_CUDA_KERNEL", "1").strip().lower() not in {"0", "false", "no"}
USE_DEEPSPEED = False  # benchmark-only; production default is intentionally off.
MODEL_REVISION = os.environ.get("INDEXTTS25_MODEL_REVISION", "unknown")
SOURCE_COMMIT = os.environ.get("INDEXTTS25_SOURCE_COMMIT", "unknown")

app = FastAPI(title="BRMMedia IndexTTS-2.5 loopback service", docs_url=None, redoc_url=None)
_engine: Any | None = None
_engine_lock = asyncio.Lock()


def _model_paths() -> tuple[Path, Path]:
    config = MODEL_DIR / "config_v2_5.yaml"
    if not config.is_file():
        raise RuntimeError(f"缺少 IndexTTS-2.5 配置文件：{config}")
    return config, MODEL_DIR


def _load_engine() -> Any:
    """Load the official 2.5 engine only while the media queue owns A5000."""
    global _engine
    if _engine is not None:
        return _engine
    config, model_dir = _model_paths()
    try:
        from indextts.infer_v2_5 import IndexTTS2
    except Exception as exc:  # pragma: no cover - depends on candidate venv
        raise RuntimeError("无法导入官方 IndexTTS-2.5 运行时") from exc
    _engine = IndexTTS2(
        cfg_path=str(config), model_dir=str(model_dir), use_bf16=USE_BF16,
        device="cuda:0", use_cuda_kernel=USE_CUDA_KERNEL,
        use_deepspeed=USE_DEEPSPEED, use_qwen_emo=False,
    )
    return _engine


def _release_engine() -> None:
    """Return A5000 memory to the next queued ComfyUI job after each request."""
    global _engine
    _engine = None
    gc.collect()
    try:
        import torch
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
            torch.cuda.synchronize()
    except Exception:
        # A failed cleanup must never conceal a completed synthesis response.
        pass


def _synthesize(reference: Path, text: str, language: str, speed: float, output: Path) -> bytes:
    engine = _load_engine()
    # IndexTTS-2.5 controls pace with duration_factor: a higher UI speed must
    # request a shorter duration, while preserving the documented 0.5–2 range.
    result = engine.infer(
        spk_audio_prompt=str(reference), text=text, output_path=str(output),
        lang=language.upper(), duration_factor=1.0 / speed, verbose=False,
    )
    if result is None or not output.is_file() or output.stat().st_size == 0:
        raise RuntimeError("IndexTTS-2.5 未生成有效 WAV")
    return output.read_bytes()


@app.get("/health")
def health() -> dict[str, Any]:
    config_exists = bool(MODEL_DIR and (MODEL_DIR / "config_v2_5.yaml").is_file())
    return {
        "ok": config_exists,
        "engine": SERVICE_NAME,
        "model_revision": MODEL_REVISION,
        "source_commit": SOURCE_COMMIT,
        "device": "cuda:0",
        "bf16": USE_BF16,
        "cuda_kernel_requested": USE_CUDA_KERNEL,
        "deepspeed": USE_DEEPSPEED,
        "languages": sorted(SUPPORTED_LANGUAGES),
        "port_boundary": "loopback-only",
    }


@app.post("/v1/voice-clone")
async def voice_clone(
    text: str = Form(...), language: str = Form("zh"), speed: float = Form(1.0),
    reference_audio: UploadFile = File(...),
) -> Response:
    text = str(text or "").strip()
    language = str(language or "").strip().lower()
    if not text:
        raise HTTPException(422, "text 不能为空")
    if len(text) > MAX_TEXT_CHARS:
        raise HTTPException(422, f"text 不得超过 {MAX_TEXT_CHARS} 个字符")
    if language not in SUPPORTED_LANGUAGES:
        raise HTTPException(422, "language 仅支持 zh、en、ja、es、ar")
    if not 0.5 <= speed <= 2.0:
        raise HTTPException(422, "speed 必须在 0.5–2.0 之间")

    RUN_DIR.mkdir(parents=True, exist_ok=True)
    request_dir = Path(tempfile.mkdtemp(prefix="request-", dir=RUN_DIR))
    reference = request_dir / f"reference{Path(reference_audio.filename or '.audio').suffix.lower() or '.audio'}"
    output = request_dir / "output.wav"
    try:
        payload = await reference_audio.read(MAX_REFERENCE_BYTES + 1)
        if len(payload) > MAX_REFERENCE_BYTES:
            raise HTTPException(413, "参考音频超过允许大小")
        if not payload:
            raise HTTPException(422, "参考音频为空")
        reference.write_bytes(payload)
        started = time.perf_counter()
        async with _engine_lock:
            try:
                audio = await asyncio.to_thread(_synthesize, reference, text, language, speed, output)
            finally:
                _release_engine()
        elapsed = time.perf_counter() - started
        return Response(
            content=audio, media_type="audio/wav",
            headers={
                "X-BRMMedia-Engine": SERVICE_NAME,
                "X-BRMMedia-Sample-Rate": "22050",
                "X-BRMMedia-Elapsed-Seconds": f"{elapsed:.3f}",
                "X-BRMMedia-Request-Id": uuid.uuid4().hex,
            },
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, f"IndexTTS-2.5 推理失败：{exc}") from exc
    finally:
        shutil.rmtree(request_dir, ignore_errors=True)

