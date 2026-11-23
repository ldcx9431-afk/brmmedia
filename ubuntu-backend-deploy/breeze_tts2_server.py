#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Loopback-only Breeze-TTS2 FastAPI service (:9206).

Wraps the high-speed Breeze-TTS-2.cpp native engine (``breeze-server``) running
on CPU+OpenBLAS (zero A5000 VRAM usage) and exposes the unified BRMMedia voice
cloning REST contract:

- ``GET /health``
- ``POST /v1/voice-clone`` (multipart form or JSON)
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import time
import uuid
import wave
from pathlib import Path
from typing import Any

import requests
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import Response

SERVICE_NAME = "Breeze-TTS2"
SUPPORTED_LANGUAGES = {"zh", "en", "auto"}
MAX_TEXT_CHARS = max(1, int(os.environ.get("BRM_BREEZE_MAX_TEXT_CHARS", "6000")))
MAX_REFERENCE_BYTES = max(
    1_000_000, int(os.environ.get("BRM_BREEZE_MAX_REFERENCE_BYTES", str(32 * 1024 * 1024)))
)
CORE_URL = os.environ.get("BRM_BREEZE_CORE_URL", "http://127.0.0.1:9207").rstrip("/")
VOICES_DIR = Path(os.environ.get("BRM_BREEZE_VOICES_DIR", "/srv/brmmedia/models/Breeze-TTS2/voices")).resolve()
CATALOG_PATH = Path(
    os.environ.get(
        "BRM_VOICE_PRESETS_CATALOG",
        "/srv/brmmedia/releases/h3-23d7084/ubuntu-backend-deploy/voice_presets/catalog.json",
    )
).resolve()
MODEL_PATH = Path(
    os.environ.get("BRM_BREEZE_MODEL_PATH", "/srv/brmmedia/models/Breeze-TTS2/breeze-tts-2-q8_0.gguf")
).resolve()
DEFAULT_INSTRUCTION = os.environ.get(
    "BRM_BREEZE_DEFAULT_INSTRUCTION",
    "A clear, natural, warm speaker with balanced pacing and expressive intonation.",
)
TIMEOUT_SECONDS = max(30, int(os.environ.get("BRM_BREEZE_TIMEOUT_SECONDS", "300")))

app = FastAPI(title="BRMMedia Breeze-TTS2 loopback service", docs_url=None, redoc_url=None)
_core_lock = asyncio.Lock()

_PRESET_HASH_MAP: dict[str, dict[str, str]] | None = None


def _load_preset_map() -> dict[str, dict[str, str]]:
    global _PRESET_HASH_MAP
    if _PRESET_HASH_MAP is not None:
        return _PRESET_HASH_MAP
    mapping: dict[str, dict[str, str]] = {}
    try:
        if CATALOG_PATH.is_file():
            data = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
            for v in data.get("voices", []):
                sha = str(v.get("sha256") or "").strip().lower()
                vid = str(v.get("id") or "").strip()
                rtext = str(v.get("reference_text") or "").strip()
                if sha and vid:
                    mapping[sha] = {"id": vid, "reference_text": rtext}
    except Exception:
        mapping = {}
    _PRESET_HASH_MAP = mapping
    return mapping


def _core_health() -> dict[str, Any]:
    with requests.Session() as s:
        s.trust_env = False
        r = s.get(f"{CORE_URL}/health", timeout=(2, 5))
        r.raise_for_status()
        return r.json()


def _pcm16_to_wav(pcm_bytes: bytes, sample_rate: int = 24000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_bytes)
    return buf.getvalue()


def _apply_speed_wav(wav_bytes: bytes, speed: float) -> bytes:
    if abs(speed - 1.0) < 0.01:
        return wav_bytes
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return wav_bytes
    # Build atempo filter chain (ffmpeg atempo accepts 0.5..2.0 directly; clamp safely)
    s = max(0.5, min(2.0, float(speed)))
    with tempfile.TemporaryDirectory(prefix="breeze-spd-") as tmp:
        inp = Path(tmp) / "in.wav"
        out = Path(tmp) / "out.wav"
        inp.write_bytes(wav_bytes)
        res = subprocess.run(
            [ffmpeg, "-nostdin", "-loglevel", "error", "-y", "-i", str(inp), "-filter:a", f"atempo={s:.4f}", str(out)],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if res.returncode == 0 and out.is_file() and out.stat().st_size > 44:
            return out.read_bytes()
    return wav_bytes


def _ensure_wav_24k_mono(audio_bytes: bytes, filename: str) -> bytes:
    """Convert arbitrary uploaded audio (mp3/flac/ogg/wav) to 24kHz mono PCM16 WAV for breeze-server."""
    if len(audio_bytes) >= 12 and audio_bytes.startswith(b"RIFF") and audio_bytes[8:12] == b"WAVE":
        try:
            with wave.open(io.BytesIO(audio_bytes), "rb") as wf:
                if wf.getnchannels() == 1 and wf.getsampwidth() == 2 and wf.getframerate() == 24000:
                    return audio_bytes
        except Exception:
            pass
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return audio_bytes
    ext = Path(filename or "ref.wav").suffix.lower() or ".audio"
    with tempfile.TemporaryDirectory(prefix="breeze-ref-") as tmp:
        inp = Path(tmp) / f"in{ext}"
        out = Path(tmp) / "ref24k.wav"
        inp.write_bytes(audio_bytes)
        res = subprocess.run(
            [ffmpeg, "-nostdin", "-loglevel", "error", "-y", "-i", str(inp), "-ac", "1", "-ar", "24000", "-sample_fmt", "s16", str(out)],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if res.returncode == 0 and out.is_file() and out.stat().st_size > 44:
            return out.read_bytes()
    return audio_bytes


def _synthesize_sync(
    text: str,
    ref_bytes: bytes | None,
    ref_filename: str,
    reference_text: str,
    instruction: str,
    seed: int,
    cfg_scale: float,
    speed: float,
) -> tuple[bytes, int, str]:
    data: dict[str, str] = {
        "text": text,
        "seed": str(seed),
        "cfg_scale": f"{cfg_scale:.3f}",
    }
    if instruction:
        data["instruction"] = instruction

    files: dict[str, tuple[str, bytes, str]] | None = None
    mode = "design"

    if ref_bytes:
        sha = hashlib.sha256(ref_bytes).hexdigest().lower()
        preset = _load_preset_map().get(sha)
        if preset and (VOICES_DIR / f"{preset['id']}.breeze").is_file():
            # Instant zero-encode voice cloning via pre-cached .breeze embedding
            data["voice_id"] = preset["id"]
            mode = f"preset:{preset['id']}"
        else:
            wav_ref = _ensure_wav_24k_mono(ref_bytes, ref_filename)
            eff_ref_text = reference_text.strip()
            if not eff_ref_text:
                # Fallback prompt if caller uploaded custom audio without transcript
                eff_ref_text = "这是一段用于语音克隆的参考音频。"
            data["ref_text"] = eff_ref_text
            files = {"ref_audio": ("reference.wav", wav_ref, "audio/wav")}
            mode = "clone"
    else:
        if not instruction:
            data["instruction"] = DEFAULT_INSTRUCTION

    with requests.Session() as s:
        s.trust_env = False
        resp = s.post(
            f"{CORE_URL}/v1/audio/speech",
            data=data,
            files=files,
            timeout=(10, TIMEOUT_SECONDS),
        )
    if resp.status_code != 200:
        detail = resp.text[:400].replace("\n", " ")
        raise RuntimeError(f"Breeze-TTS2 core HTTP {resp.status_code}: {detail}")
    sr = int(resp.headers.get("X-Sample-Rate") or "24000")
    pcm = resp.content
    if not pcm or len(pcm) < 64:
        raise RuntimeError("Breeze-TTS2 returned empty PCM audio")
    wav_bytes = _pcm16_to_wav(pcm, sample_rate=sr)
    wav_bytes = _apply_speed_wav(wav_bytes, speed)
    return wav_bytes, sr, mode


@app.get("/health")
def health() -> dict[str, Any]:
    model_exists = MODEL_PATH.is_file()
    core_ok = False
    core_info: dict[str, Any] = {}
    try:
        core_info = _core_health()
        core_ok = core_info.get("status") == "ok"
    except Exception as exc:
        core_info = {"error": str(exc)}
    return {
        "ok": bool(model_exists and core_ok),
        "engine": SERVICE_NAME,
        "model": MODEL_PATH.name,
        "device": "cpu-openblas",
        "vram_mib": 0,
        "sample_rate": int(core_info.get("sample_rate") or 24000),
        "core_url": CORE_URL,
        "core": core_info,
        "saved_voices": len(list(VOICES_DIR.glob("*.breeze"))) if VOICES_DIR.is_dir() else 0,
        "languages": sorted(SUPPORTED_LANGUAGES),
        "port_boundary": "loopback-only",
    }


@app.post("/v1/voice-clone")
async def voice_clone(
    text: str = Form(...),
    language: str = Form("zh"),
    speed: float = Form(1.0),
    reference_text: str = Form(""),
    instruction: str = Form(""),
    seed: int = Form(42),
    cfg_scale: float = Form(1.0),
    reference_audio: UploadFile | None = File(None),
) -> Response:
    text = str(text or "").strip()
    language = str(language or "zh").strip().lower()
    if not text:
        raise HTTPException(422, "text 不能为空")
    if len(text) > MAX_TEXT_CHARS:
        raise HTTPException(422, f"text 不得超过 {MAX_TEXT_CHARS} 个字符")
    if language not in SUPPORTED_LANGUAGES:
        raise HTTPException(422, "language 仅支持 zh、en、auto")
    if not 0.5 <= speed <= 2.0:
        raise HTTPException(422, "speed 必须在 0.5–2.0 之间")
    if not 1.0 <= cfg_scale <= 5.0:
        raise HTTPException(422, "cfg_scale 必须在 1.0–5.0 之间")

    ref_bytes: bytes | None = None
    ref_filename = "reference.wav"
    if reference_audio is not None:
        ref_filename = reference_audio.filename or "reference.wav"
        ref_bytes = await reference_audio.read(MAX_REFERENCE_BYTES + 1)
        if len(ref_bytes) > MAX_REFERENCE_BYTES:
            raise HTTPException(413, "参考音频超过允许大小")
        if not ref_bytes:
            ref_bytes = None

    started = time.perf_counter()
    try:
        async with _core_lock:
            wav_bytes, sr, mode = await asyncio.to_thread(
                _synthesize_sync,
                text,
                ref_bytes,
                ref_filename,
                reference_text,
                instruction,
                seed,
                cfg_scale,
                speed,
            )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, f"Breeze-TTS2 推理失败：{exc}") from exc

    elapsed = time.perf_counter() - started
    return Response(
        content=wav_bytes,
        media_type="audio/wav",
        headers={
            "X-BRMMedia-Engine": SERVICE_NAME,
            "X-BRMMedia-Mode": mode,
            "X-BRMMedia-Sample-Rate": str(sr),
            "X-BRMMedia-Elapsed-Seconds": f"{elapsed:.3f}",
            "X-BRMMedia-Request-Id": uuid.uuid4().hex,
        },
    )
