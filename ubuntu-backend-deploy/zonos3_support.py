# -*- coding: utf-8 -*-
"""Zonos3-mini smart text chunking, parallel synthesis, and WAV crossfade stitching."""

from __future__ import annotations

import io
import logging
import os
import re
import wave
from array import array
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

import requests

DEFAULT_CHUNK_THRESHOLD = 300
DEFAULT_MAX_CHUNK_CHARS = 220
DEFAULT_CROSSFADE_MS = 18.0

_SENTENCE_BREAK_RE = re.compile(r"(?<=[。！？；\n\r])|(?<!\d)(?<=[.!?;])(?=\s|$)")
_CLAUSE_BREAK_RE = re.compile(r"(?<=[，,、：:\s])")


def configured_chunk_threshold() -> int:
    try:
        return max(50, int(os.environ.get("BRM_ZONOS3_CHUNK_THRESHOLD", str(DEFAULT_CHUNK_THRESHOLD))))
    except (TypeError, ValueError):
        return DEFAULT_CHUNK_THRESHOLD


def configured_max_chunk_chars() -> int:
    try:
        return max(60, int(os.environ.get("BRM_ZONOS3_MAX_CHUNK_CHARS", str(DEFAULT_MAX_CHUNK_CHARS))))
    except (TypeError, ValueError):
        return DEFAULT_MAX_CHUNK_CHARS


def configured_crossfade_ms() -> float:
    try:
        val = float(os.environ.get("BRM_ZONOS3_CROSSFADE_MS", str(DEFAULT_CROSSFADE_MS)))
        return max(0.0, min(100.0, val))
    except (TypeError, ValueError):
        return DEFAULT_CROSSFADE_MS


def _split_long_segment(segment: str, limit: int) -> list[str]:
    pieces = [part for part in _CLAUSE_BREAK_RE.split(segment) if part]
    if not pieces:
        pieces = [segment]
    out: list[str] = []
    buf = ""
    for part in pieces:
        if len(part) > limit:
            if buf.strip():
                out.append(buf.strip())
                buf = ""
            for idx in range(0, len(part), limit):
                chunk = part[idx : idx + limit].strip()
                if chunk:
                    out.append(chunk)
            continue
        if buf and len(buf) + len(part) > limit:
            if buf.strip():
                out.append(buf.strip())
            buf = part
        else:
            buf += part
    if buf.strip():
        out.append(buf.strip())
    return out


def split_zonos3_text(
    text: str,
    *,
    threshold: int | None = None,
    max_chunk_chars: int | None = None,
) -> list[str]:
    """Split long Zonos3 text along natural sentence/newline boundaries."""
    cleaned = str(text or "").strip()
    if not cleaned:
        return []
    eff_threshold = configured_chunk_threshold() if threshold is None else max(50, int(threshold))
    if len(cleaned) <= eff_threshold:
        return [cleaned]

    eff_max = configured_max_chunk_chars() if max_chunk_chars is None else max(60, int(max_chunk_chars))
    raw_sentences = [part.strip() for part in _SENTENCE_BREAK_RE.split(cleaned) if part and part.strip()]
    sentences: list[str] = []
    for sent in raw_sentences:
        if len(sent) > eff_max:
            sentences.extend(_split_long_segment(sent, eff_max))
        else:
            sentences.append(sent)
    if not sentences:
        return [cleaned]

    chunks: list[str] = []
    current = ""
    for sent in sentences:
        sep = "\n" if current and not current.endswith(("\n", "。", "！", "？", ".", "!", "?", "；", ";")) else (" " if current and current[-1].isascii() and sent[0].isascii() else "")
        candidate = f"{current}{sep}{sent}" if current else sent
        if current and len(candidate) > eff_max:
            chunks.append(current)
            current = sent
        else:
            current = candidate
    if current:
        chunks.append(current)

    if len(chunks) > 1 and len(chunks[-1]) < 20 and len(chunks[-2]) + len(chunks[-1]) + 1 <= eff_max + 80:
        tail = chunks.pop()
        sep = " " if chunks[-1][-1].isascii() and tail[0].isascii() else ""
        chunks[-1] = f"{chunks[-1]}{sep}{tail}"
    return chunks if chunks else [cleaned]


def _validate_wav_bytes(data: bytes) -> None:
    if not isinstance(data, (bytes, bytearray)) or len(data) < 44:
        raise ValueError("ZONOS3 returned an empty or truncated WAV")
    if not data.startswith(b"RIFF") or data[8:12] != b"WAVE":
        raise ValueError("ZONOS3 returned an invalid WAV")


def stitch_wav_chunks(wav_chunks: list[bytes], *, crossfade_ms: float | None = None) -> bytes:
    """Concatenate 16-bit PCM WAV chunks seamlessly with short linear crossfades."""
    if not wav_chunks:
        raise ValueError("No WAV chunks to stitch")
    if len(wav_chunks) == 1:
        _validate_wav_bytes(wav_chunks[0])
        return wav_chunks[0]

    fade_ms = configured_crossfade_ms() if crossfade_ms is None else max(0.0, float(crossfade_ms))
    params = None
    pcm_chunks: list[array] = []
    for raw in wav_chunks:
        _validate_wav_bytes(raw)
        with wave.open(io.BytesIO(raw), "rb") as wf:
            p = (wf.getnchannels(), wf.getsampwidth(), wf.getframerate(), wf.getcomptype())
            if p[1] != 2 or p[3] != "NONE":
                raise ValueError(f"Unsupported WAV format for stitching: {p}")
            if params is None:
                params = p
            elif p != params:
                raise ValueError(f"WAV chunk parameter mismatch: {p} != {params}")
            samples = array("h")
            samples.frombytes(wf.readframes(wf.getnframes()))
            pcm_chunks.append(samples)

    assert params is not None
    nchannels, sampwidth, framerate, comptype = params
    fade_frames = int(round(framerate * fade_ms / 1000.0))

    combined = array("h", pcm_chunks[0])
    for nxt in pcm_chunks[1:]:
        prev_frames = len(combined) // nchannels
        nxt_frames = len(nxt) // nchannels
        overlap_frames = min(fade_frames, prev_frames // 4, nxt_frames // 4)
        if overlap_frames > 1:
            overlap_samples = overlap_frames * nchannels
            tail_start = len(combined) - overlap_samples
            for frame_idx in range(overlap_frames):
                t = (frame_idx + 1) / float(overlap_frames + 1)
                w_prev = 1.0 - t
                w_nxt = t
                for ch in range(nchannels):
                    s_idx = frame_idx * nchannels + ch
                    blended = int(round(combined[tail_start + s_idx] * w_prev + nxt[s_idx] * w_nxt))
                    combined[tail_start + s_idx] = max(-32768, min(32767, blended))
            combined.extend(nxt[overlap_samples:])
        else:
            combined.extend(nxt)

    out_buf = io.BytesIO()
    with wave.open(out_buf, "wb") as out_wf:
        out_wf.setnchannels(nchannels)
        out_wf.setsampwidth(sampwidth)
        out_wf.setframerate(framerate)
        out_wf.writeframes(combined.tobytes())
    return out_buf.getvalue()


def _default_post_chunk(service_url: str, timeout_seconds: int, chunk_payload: dict[str, Any]) -> bytes:
    with requests.Session() as session:
        session.trust_env = False
        response = session.post(
            f"{service_url.rstrip('/')}/v2/generate",
            json=chunk_payload,
            timeout=(10, timeout_seconds),
        )
        if response.status_code != 200:
            detail = response.text[:500].replace("\n", " ")
            raise RuntimeError(f"ZONOS3-Mini 合成失败（HTTP {response.status_code}）：{detail}")
        content = response.content
        _validate_wav_bytes(content)
        return content


class Zonos3CancelledError(RuntimeError):
    """Raised when user cancels mid-synthesis so fallback does not retry."""


def synthesize_zonos3_wav(
    payload: dict[str, Any],
    *,
    service_url: str,
    timeout_seconds: int,
    gate: Any = None,
    cancel_check: Callable[[], bool] | None = None,
    post_fn: Callable[[dict[str, Any]], bytes] | None = None,
    threshold: int | None = None,
    max_chunk_chars: int | None = None,
    crossfade_ms: float | None = None,
) -> tuple[bytes, dict[str, Any]]:
    """Synthesize Zonos3 WAV, splitting long text into parallel chunks when spare slots exist.

    The caller is expected to already hold 1 primary slot on ``gate`` if ``gate``
    is provided. Additional spare slots are acquired non-blockingly via
    ``gate.try_acquire()`` to prevent deadlocks across concurrent requests.
    """
    sender = post_fn or (lambda p: _default_post_chunk(service_url, timeout_seconds, p))
    text = str(payload.get("text") or "").strip()
    chunks = split_zonos3_text(text, threshold=threshold, max_chunk_chars=max_chunk_chars)
    if len(chunks) <= 1:
        if cancel_check is not None and cancel_check():
            raise Zonos3CancelledError("ZONOS3-Mini 语音任务已取消")
        return sender(dict(payload)), {"chunked": False, "chunks": 1, "workers": 1}

    extra_slots = 0
    if gate is not None and callable(getattr(gate, "try_acquire", None)):
        for _ in range(len(chunks) - 1):
            if gate.try_acquire():
                extra_slots += 1
            else:
                break

    workers = 1 + extra_slots
    try:
        def _run_chunk(chunk_text: str) -> bytes:
            if cancel_check is not None and cancel_check():
                raise Zonos3CancelledError("ZONOS3-Mini 语音任务已取消")
            chunk_payload = dict(payload)
            chunk_payload["text"] = chunk_text
            wav_bytes = sender(chunk_payload)
            _validate_wav_bytes(wav_bytes)
            return wav_bytes

        if workers == 1:
            wav_parts = [_run_chunk(chunk) for chunk in chunks]
        else:
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="zonos3-chunk") as pool:
                wav_parts = list(pool.map(_run_chunk, chunks))

        if cancel_check is not None and cancel_check():
            raise Zonos3CancelledError("ZONOS3-Mini 语音任务已取消")
        stitched = stitch_wav_chunks(wav_parts, crossfade_ms=crossfade_ms)
        return stitched, {"chunked": True, "chunks": len(chunks), "workers": workers}
    except Zonos3CancelledError:
        raise
    except Exception as exc:
        logging.warning("Zonos3 chunked parallel synthesis failed (%s); falling back to single segment", exc)
        if cancel_check is not None and cancel_check():
            raise Zonos3CancelledError("ZONOS3-Mini 语音任务已取消")
        return sender(dict(payload)), {"chunked": False, "chunks": 1, "workers": 1, "fallback": True}
    finally:
        if gate is not None and extra_slots > 0:
            for _ in range(extra_slots):
                gate.release()
