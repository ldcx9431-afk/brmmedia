# -*- coding: utf-8 -*-
"""Breeze-TTS2 smart text chunking, HTTP synthesis, and WAV crossfade stitching."""

from __future__ import annotations

import logging
import os
from typing import Any, Callable

import requests

import zonos3_support

DEFAULT_CHUNK_THRESHOLD = 420
DEFAULT_MAX_CHUNK_CHARS = 300
DEFAULT_CROSSFADE_MS = 18.0
DEFAULT_MAX_TEXT_CHARS = 6000

BREEZE_TTS2_LANGUAGES: dict[str, str] = {
    "zh": "zh",
    "cmn": "zh",
    "en": "en",
    "en_us": "en",
    "en_gb": "en",
    "auto": "auto",
}


def normalise_language(language: str) -> str:
    code = str(language or "zh").strip().lower()
    if code not in BREEZE_TTS2_LANGUAGES:
        raise ValueError(f"Breeze-TTS2 仅支持 zh、en、auto（收到：{language}）")
    return BREEZE_TTS2_LANGUAGES[code]


def _validate_wav_bytes(data: bytes) -> None:
    if not isinstance(data, (bytes, bytearray)) or len(data) < 44:
        raise ValueError("Breeze-TTS2 returned an empty or truncated WAV")
    if not data.startswith(b"RIFF") or data[8:12] != b"WAVE":
        raise ValueError("Breeze-TTS2 returned an invalid WAV")


def _default_post_chunk(
    service_url: str,
    timeout_seconds: int,
    chunk_text: str,
    *,
    language: str,
    speed: float,
    reference_text: str,
    instruction: str,
    seed: int,
    cfg_scale: float,
    reference_bytes: bytes | None,
    reference_filename: str,
) -> bytes:
    data = {
        "text": chunk_text,
        "language": language,
        "speed": f"{speed:.3f}",
        "reference_text": reference_text,
        "instruction": instruction,
        "seed": str(seed),
        "cfg_scale": f"{cfg_scale:.3f}",
    }
    files = (
        {"reference_audio": (reference_filename or "reference.wav", reference_bytes, "application/octet-stream")}
        if reference_bytes
        else None
    )
    with requests.Session() as session:
        session.trust_env = False
        response = session.post(
            f"{service_url.rstrip('/')}/v1/voice-clone",
            data=data,
            files=files,
            timeout=(10, timeout_seconds),
        )
    if response.status_code != 200:
        detail = response.text[:500].replace("\n", " ")
        raise RuntimeError(f"Breeze-TTS2 合成失败（HTTP {response.status_code}）：{detail}")
    content = response.content
    _validate_wav_bytes(content)
    return content


def synthesize_breeze_tts2_wav(
    *,
    text: str,
    service_url: str,
    timeout_seconds: int,
    language: str = "zh",
    speed: float = 1.0,
    reference_text: str = "",
    instruction: str = "",
    seed: int = 42,
    cfg_scale: float = 1.0,
    reference_bytes: bytes | None = None,
    reference_filename: str = "reference.wav",
    cancel_check: Callable[[], bool] | None = None,
    post_fn: Callable[[str], bytes] | None = None,
    threshold: int | None = None,
    max_chunk_chars: int | None = None,
    crossfade_ms: float | None = None,
) -> tuple[bytes, dict[str, Any]]:
    """Synthesize Breeze-TTS2 WAV, chunking very long text along sentence boundaries."""
    cleaned = str(text or "").strip()
    if not cleaned:
        raise ValueError("Breeze-TTS2 合成文本不能为空")
    eff_threshold = (
        max(80, int(os.environ.get("BRM_BREEZE_CHUNK_THRESHOLD", str(DEFAULT_CHUNK_THRESHOLD))))
        if threshold is None
        else max(80, int(threshold))
    )
    eff_max = (
        max(100, int(os.environ.get("BRM_BREEZE_MAX_CHUNK_CHARS", str(DEFAULT_MAX_CHUNK_CHARS))))
        if max_chunk_chars is None
        else max(100, int(max_chunk_chars))
    )
    sender = post_fn or (
        lambda chunk: _default_post_chunk(
            service_url,
            timeout_seconds,
            chunk,
            language=language,
            speed=speed,
            reference_text=reference_text,
            instruction=instruction,
            seed=seed,
            cfg_scale=cfg_scale,
            reference_bytes=reference_bytes,
            reference_filename=reference_filename,
        )
    )
    chunks = zonos3_support.split_zonos3_text(cleaned, threshold=eff_threshold, max_chunk_chars=eff_max)
    if len(chunks) <= 1:
        if cancel_check is not None and cancel_check():
            raise zonos3_support.Zonos3CancelledError("Breeze-TTS2 语音任务已取消")
        wav = sender(cleaned)
        _validate_wav_bytes(wav)
        return wav, {"chunked": False, "chunks": 1}

    wav_parts: list[bytes] = []
    try:
        for chunk in chunks:
            if cancel_check is not None and cancel_check():
                raise zonos3_support.Zonos3CancelledError("Breeze-TTS2 语音任务已取消")
            wav_part = sender(chunk)
            _validate_wav_bytes(wav_part)
            wav_parts.append(wav_part)
        if cancel_check is not None and cancel_check():
            raise zonos3_support.Zonos3CancelledError("Breeze-TTS2 语音任务已取消")
        fade = DEFAULT_CROSSFADE_MS if crossfade_ms is None else max(0.0, float(crossfade_ms))
        stitched = zonos3_support.stitch_wav_chunks(wav_parts, crossfade_ms=fade)
        return stitched, {"chunked": True, "chunks": len(chunks)}
    except zonos3_support.Zonos3CancelledError:
        raise
    except Exception as exc:
        logging.warning("Breeze-TTS2 chunked synthesis failed (%s); falling back to single pass", exc)
        if cancel_check is not None and cancel_check():
            raise zonos3_support.Zonos3CancelledError("Breeze-TTS2 语音任务已取消")
        wav = sender(cleaned)
        _validate_wav_bytes(wav)
        return wav, {"chunked": False, "chunks": 1, "fallback": True}
