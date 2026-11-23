#!/usr/bin/env python3
"""BRMMedia LAN automation API.

This service is deliberately separate from the Gradio UI process.  It exposes
an ordinary REST contract for LAN applications while the existing Gradio API
remains a backwards-compatible UI integration surface.  The process binds to
loopback only; Nginx provides the authenticated ``/api/`` LAN entry point.
"""

from __future__ import annotations

import asyncio
import atexit
import base64
import binascii
import copy
import hashlib
import hmac
import json
import mimetypes
import os
import re
import secrets
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

try:
    import anyio
except ImportError:  # pragma: no cover - minimal contract test stub
    anyio = None  # type: ignore[assignment]
import requests
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
try:
    from fastapi.responses import Response as _FastAPIResponse
except ImportError:  # pragma: no cover - minimal contract test stub
    _FastAPIResponse = None
from pydantic import BaseModel, Field

import adaptive_concurrency
import llm_runtime
import system_telemetry
import zonos3_support
import breeze_tts2_support
from comfyui_server import BASE as COMFY_BASE, COMFY_ROOT, audio_duration, upload_image
from flux2_fast_support import SIZES as FLUX2_FAST_SIZES
from flux3_image_support import SIZES as FLUX3_IMAGE_SIZES
from qwen3_tts_support import LANGUAGES as QWEN3_TTS_LANGUAGES, MAX_REFERENCE_BYTES, MAX_REFERENCE_SECONDS
from seedvr2_support import (
    MAX_OUTPUT_EDGE,
    MAX_OUTPUT_PIXELS,
    PLANNED_VIDEO_RESOLUTION_PRESETS,
    SEEDVR2_FAST_MODEL,
    SEEDVR2_FAST_MODEL_SHA256,
    SEEDVR2_MODEL,
    SEEDVR2_MODEL_DEFAULT,
    SEEDVR2_MODEL_SHA256,
    SEEDVR2_MODELS,
    SEEDVR2_REPO,
    SEEDVR2_REVISION,
    SEEDVR2_VAE,
    STRENGTH_DENOISE,
    TASK_MAX_SECONDS,
    TASK_TIMEOUT_SECONDS,
    VIDEO_EXTENSIONS,
    VIDEO_UPLOAD_MAX_BYTES,
    available_disk_bytes,
    estimated_video_workspace_bytes,
    is_supported_video_filename,
    model_installed,
    models_installed,
    output_dimensions,
    probe_video,
    resolve_comfy_input,
    validate_video_info,
    video_output_dimensions,
)


API_PREFIX = "/api/v1"
BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = Path(os.environ.get("BRM_OUTPUT_DIR", BASE_DIR / "outputs")).expanduser().resolve()
ASSET_INDEX_PATH = OUTPUT_DIR / "lan-api-assets.json"
UPLOAD_DIR = OUTPUT_DIR / ".lan-api-uploads"
GRADIO_BASE = os.environ.get("BRM_GRADIO_API_BASE", "http://127.0.0.1:9000").rstrip("/")
ZONOS3_BASE = os.environ.get("BRM_ZONOS3_URL", "http://127.0.0.1:1919").rstrip("/")
ZONOS3_TIMEOUT_SECONDS = max(30, int(os.environ.get("BRM_ZONOS3_TIMEOUT_SECONDS", "210")))
BREEZE_TTS2_BASE = os.environ.get("BRM_BREEZE_TTS2_URL", "http://127.0.0.1:9206").rstrip("/")
BREEZE_TTS2_TIMEOUT_SECONDS = max(30, int(os.environ.get("BRM_BREEZE_TTS2_TIMEOUT_SECONDS", "300")))
MAX_UPLOAD_BYTES = max(1, int(os.environ.get("BRM_LAN_API_MAX_UPLOAD_BYTES", str(256 * 1024 * 1024))))
GRADIO_TIMEOUT_SECONDS = max(10, int(os.environ.get("BRM_LAN_API_GRADIO_TIMEOUT", "60")))
ASSET_TTL_SECONDS = max(3600, int(os.environ.get("BRM_LAN_API_ASSET_TTL", str(7 * 24 * 3600))))
_API_KEY_DB_LOCK = threading.Lock()
KEY_CACHE_TTL_SECONDS = max(0.0, float(os.environ.get("BRM_QWEN_KEY_CACHE_TTL", "8.0")))
_KEY_CACHE_LOCK = threading.Lock()
_KEY_CACHE: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}
_USAGE_BUFFER: dict[tuple[str, str], tuple[int, int]] = {}
_USAGE_FLUSH_AT: dict[str, float] = {}
_HTPASSWD_CACHE_LOCK = threading.Lock()
_HTPASSWD_CACHE: dict[str, tuple[tuple[int, int], list[tuple[str, str]]]] = {}
_LOCAL_TASKS_LOCK = threading.Lock()
_LOCAL_TASKS: dict[str, dict[str, Any]] = {}
_LOCAL_TASKS_LOADED_PATH: str | None = None
_CONCURRENCY_CONTROLLER = adaptive_concurrency.AdaptiveConcurrencyController()
_ZONOS3_GATE = adaptive_concurrency.CrossProcessSlotGate(
    lambda: int(_CONCURRENCY_CONTROLLER.evaluate()["limits"][adaptive_concurrency.TIER_VOICE_CPU])
)
_MAX_LOCAL_TASKS = 500
_HTTP_LOCAL = threading.local()
_GRADIO_INFO_LOCK = threading.Lock()
_GRADIO_PARAM_CACHE: dict[str, tuple[float, dict[str, list[str]]]] = {}
_MUSIC_MODELS_LOCK = threading.Lock()
_MUSIC_MODELS_CACHE: tuple[str, float, list[str]] = ("", 0.0, [])
_ASSETS_CACHE_LOCK = threading.Lock()
_ASSETS_CACHE: tuple[str, tuple[int, int] | None, dict[str, "AssetRecord"]] = ("", None, {})
_DOTENV_CACHE_LOCK = threading.Lock()
_DOTENV_CACHE: tuple[str, tuple[int, int] | None, dict[str, str]] = ("", None, {})


def _http_session():
    session_cls = getattr(requests, "Session", None)
    if not callable(session_cls):
        return requests
    session = getattr(_HTTP_LOCAL, "session", None)
    if session is None or getattr(_HTTP_LOCAL, "session_cls", None) is not session_cls:
        session = session_cls()
        session.trust_env = False
        _HTTP_LOCAL.session = session
        _HTTP_LOCAL.session_cls = session_cls
    return session

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}
AUDIO_EXTENSIONS = {".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg"}
MEDIA_EXTENSIONS = IMAGE_EXTENSIONS | AUDIO_EXTENSIONS | VIDEO_EXTENSIONS
SIZE_VALUES = [
    "512 × 512", "768 × 768", "1024 × 1024", "2048 × 2048",
    "1024 × 768", "768 × 1024", "1344 × 768", "768 × 1344",
    "1920 × 1080", "1080 × 1920", "2048 × 1536", "1536 × 2048",
    "2560 × 1440", "1440 × 2560", "3840 × 2160", "2160 × 3840",
]
LANGUAGE_VALUES = ["zh", "en", "ja", "ko", "fr", "de", "es", "ru", "unknown"]
VOICE_CLONE_LANGUAGE_VALUES = ["zh", "en", "ja", "es", "ar"]
# ACE-Step SFT is retired; prefer Base (50-step strong vocal/lyric alignment) with Turbo (8-step fast) and YuE2-3B.
MUSIC_MODEL_CANDIDATES = ["base", "turbo"]
H3_PROFILE_VALUES = ["draft", "preview", "quality"]
H3_ACCELERATION_VALUES = ["standard", "turbo_balanced", "turbo_fast"]
H3_ACCELERATION_DETAILS = {
    "standard": {
        "engine": "MiniMax H3 Base", "steps": 20,
        "sampler": "res_multistep", "shift_video": 12, "shift_audio": 3,
        "purpose": "Official quality path and safe fallback; no Turbo LoRA.",
    },
    "turbo_balanced": {
        "engine": "MiniMax H3 + LightX2V Turbo v1.0 8-step", "steps": 8,
        "sampler": "euler", "shift_video": 12, "shift_audio": 3,
        "lora": "minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors",
        "purpose": "Balanced acceleration for mixed aspect ratios; 7–15 second requests use the stable PyTorch attention path on A5000.",
        "constraints": {"maximum_seconds": 15, "long_sequence_attention": "pytorch-stable"},
    },
    "turbo_fast": {
        "engine": "MiniMax H3 + LightX2V Turbo v1.0 4-step 768P", "steps": 4,
        "sampler": "euler", "shift_video": 6, "shift_audio": 3,
        "lora": "minimax_h3_fl2v_turbo_4step_v1.0_768p_comfyui_bf16.safetensors",
        "purpose": "Fast 768P landscape path for up to 6 seconds. A 7–15 second request is transparently executed by the compatible 8-step Turbo path with adaptive canvas on A5000.",
        "constraints": {"profile": "quality", "aspect_ratio": "16:9 landscape", "effective_size": "1344 × 768 up to 6 seconds; adaptive 8-step fallback for 7–15 seconds", "maximum_seconds": 15, "long_sequence_attention": "pytorch-stable"},
    },
}
H3_PROFILE_DETAILS = {
    "draft": {"target_megapixels": 0.4, "frames": 73, "default_seconds": 3, "minimum_seconds": 3, "maximum_seconds": 3,
              "purpose": "Fast official H3 prompt/composition/motion draft."},
    "preview": {"short_edge": 480, "default_seconds": 5, "minimum_seconds": 4, "maximum_seconds": 15},
    "quality": {"short_edge": 768, "default_seconds": 6, "minimum_seconds": 4, "maximum_seconds": 15},
}
H3_COMMON_PARAMS = {
    "prompt": {
        "type": "string", "required": True, "maximum_length": 12000,
        "description": "Video prompt. Include scene, movement, and desired audio where applicable.",
    },
    "size": {
        "type": "string", "required": False, "default": "768 × 1024",
        "enum_source": "size_values",
        "description": "Aspect ratio only; the service selects the H3 canvas for the requested profile.",
    },
    "profile": {
        "type": "string", "required": False, "default": "preview",
        "enum": H3_PROFILE_VALUES,
        "description": "draft uses the official ~0.4MP / 73-frame template; preview uses a 480px short edge; quality uses a 768px short edge.",
    },
    "acceleration": {
        "type": "string", "required": False, "default": "standard",
        "enum": H3_ACCELERATION_VALUES,
        "description": "All modes accept up to 15 seconds. Long quality requests use an adaptive canvas; Turbo requests longer than 6 seconds use stable PyTorch attention, and turbo_fast uses the compatible 8-step Turbo path.",
    },
    "seconds": {
        "type": "integer", "required": False, "minimum": 3, "maximum": 15,
        "default": 5,
        "default_by_profile": {profile: detail["default_seconds"] for profile, detail in H3_PROFILE_DETAILS.items()},
        "description": "H3 adjusts the request to its 24fps / 17-frame grid; read effective_settings from task status.",
    },
}

# H3 remains the legacy ``text-to-video`` / ``image-to-video`` default while
# it is active.  Expose the retained LTX2.3 paths under explicit slugs so LAN
# callers can choose an engine instead of having to change the global H3
# switch (which would also hide the H3 UI).
LTX_TEXT_TO_VIDEO_OPTIONS = {
    "engine": "LTX2.3",
    "availability": "active",
    "seconds": {"minimum": 2, "maximum": 360},
    "params": {
        "prompt": {"type": "string", "required": True, "maximum_length": 12000},
        "size": {
            "type": "string", "required": False, "default": "768 × 1024",
            "enum_source": "size_values",
            "description": "Literal LTX2.3 output canvas, unlike H3's aspect-ratio selector.",
        },
        "seconds": {"type": "integer", "required": False, "default": 5, "minimum": 2, "maximum": 360},
    },
}
LTX_IMAGE_TO_VIDEO_OPTIONS = {
    "engine": "LTX2.3",
    "availability": "active",
    "seconds": {"minimum": 2, "maximum": 360},
    "size_rule": "LTX2.3 image-to-video retains the uploaded source image canvas.",
    "params": {
        "prompt": {"type": "string", "required": True, "maximum_length": 12000},
        "image_asset_id": {
            "type": "asset_id", "required": True, "asset_kind": "image",
            "description": "An image asset_id returned by POST /files?kind=image.",
        },
        "seconds": {"type": "integer", "required": False, "default": 5, "minimum": 2, "maximum": 360},
    },
}


@dataclass(frozen=True)
class WorkflowSpec:
    endpoint: str
    description: str
    required_assets: dict[str, str]
    normalizer: Callable[[dict[str, Any], dict[str, "AssetRecord"]], list[Any]]
    options: dict[str, Any] | None = None


@dataclass(frozen=True)
class AssetRecord:
    asset_id: str
    kind: str
    filename: str
    source_filename: str
    size_bytes: int
    sha256: str
    created_at: float


class TaskSubmission(BaseModel):
    workflow: str = Field(description="Stable workflow slug returned by /capabilities")
    params: dict[str, Any] = Field(default_factory=dict)


app = FastAPI(
    title="BRMMedia LAN API",
    version="2.0.0",
    description="Authenticated LAN automation API for BRMMedia generation workflows.",
    openapi_url=f"{API_PREFIX}/openapi.json",
    docs_url=f"{API_PREFIX}/docs",
    redoc_url=None,
)


def _key_db_path() -> Path:
    return Path(
        os.environ.get("BRM_QWEN_API_KEY_DB", "/var/lib/brmmedia/qwen-api-keys.sqlite3")
    ).expanduser()


def _key_digest(api_key: str) -> str:
    """Match qwen_api_gateway.py SHA-256 key digest logic."""
    try:
        import qwen_api_gateway  # noqa: WPS433
        return qwen_api_gateway._key_digest(api_key)
    except Exception:
        return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


def _invalidate_key_cache(db_path: Path | None = None) -> None:
    db_key = str(db_path or _key_db_path())
    with _KEY_CACHE_LOCK:
        for cache_key in [k for k in _KEY_CACHE if k[0] == db_key]:
            _KEY_CACHE.pop(cache_key, None)


def _flush_usage_buffer(db_path: Path | None = None) -> None:
    target_path = db_path or _key_db_path()
    db_key = str(target_path)
    with _KEY_CACHE_LOCK:
        pending = [
            (key_id, count, last_used)
            for (k_db, key_id), (count, last_used) in list(_USAGE_BUFFER.items())
            if k_db == db_key and _USAGE_BUFFER.pop((k_db, key_id), None)
        ]
        _USAGE_FLUSH_AT[db_key] = time.monotonic()
    if not pending or not target_path.is_file():
        return
    with _API_KEY_DB_LOCK:
        try:
            connection = sqlite3.connect(target_path)
        except sqlite3.Error:
            return
        try:
            try:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA synchronous=NORMAL")
            except sqlite3.OperationalError:
                pass
            for key_id, count, last_used in pending:
                connection.execute(
                    "UPDATE api_keys SET last_used_at = MAX(COALESCE(last_used_at, 0), ?), use_count = use_count + ? WHERE id = ?",
                    (last_used, count, key_id),
                )
            connection.commit()
        except sqlite3.OperationalError:
            # Allow read-only DB mounts while keeping token validation strict.
            pass
        except sqlite3.Error:
            pass
        finally:
            connection.close()


atexit.register(_flush_usage_buffer)


def _validate_bearer_api_key(api_key: str) -> None:
    """Validate Bearer API key against the shared qwen_api_gateway SQLite store with 8s TTL cache."""
    token = api_key.strip()
    if not token:
        raise HTTPException(status_code=401, detail="Bearer API key is required")
    db_path = _key_db_path()
    if not db_path.is_file():
        raise HTTPException(status_code=401, detail="API key is invalid, disabled, or revoked")
    now = int(time.time())
    now_mono = time.monotonic()
    db_key = str(db_path)
    digest = _key_digest(token)
    cache_key = (db_key, digest)

    if KEY_CACHE_TTL_SECONDS > 0:
        should_flush = False
        cached_entry = None
        with _KEY_CACHE_LOCK:
            cached = _KEY_CACHE.get(cache_key)
            if cached is not None:
                expires_mono, meta = cached
                if now_mono < expires_mono:
                    if meta["expires_at"] is not None and meta["expires_at"] <= now:
                        _KEY_CACHE.pop(cache_key, None)
                    else:
                        prev_count, _ = _USAGE_BUFFER.get((db_key, meta["id"]), (0, 0))
                        _USAGE_BUFFER[(db_key, meta["id"])] = (prev_count + 1, now)
                        last_flush = _USAGE_FLUSH_AT.get(db_key, 0.0)
                        should_flush = (now_mono - last_flush) >= 5.0
                        cached_entry = meta
                else:
                    _KEY_CACHE.pop(cache_key, None)
        if cached_entry is not None:
            if should_flush:
                _flush_usage_buffer(db_path)
            return

    with _API_KEY_DB_LOCK:
        try:
            connection = sqlite3.connect(db_path)
            connection.row_factory = sqlite3.Row
        except sqlite3.Error:
            raise HTTPException(status_code=401, detail="API key store is unavailable") from None
        try:
            try:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA synchronous=NORMAL")
            except sqlite3.OperationalError:
                pass
            row = connection.execute(
                "SELECT id, status, expires_at FROM api_keys WHERE key_hash = ?",
                (digest,),
            ).fetchone()
            if row is None or row["status"] != "active":
                raise HTTPException(status_code=401, detail="API key is invalid, disabled, or revoked")
            if row["expires_at"] is not None and row["expires_at"] <= now:
                raise HTTPException(status_code=401, detail="API key has expired")
            with _KEY_CACHE_LOCK:
                buffered_count, _ = _USAGE_BUFFER.pop((db_key, row["id"]), (0, 0))
                _USAGE_FLUSH_AT[db_key] = now_mono
            try:
                connection.execute(
                    "UPDATE api_keys SET last_used_at = MAX(COALESCE(last_used_at, 0), ?), use_count = use_count + ? WHERE id = ?",
                    (now, 1 + buffered_count, row["id"]),
                )
                connection.commit()
            except sqlite3.OperationalError:
                # Allow read-only DB mounts while keeping token validation strict.
                pass
            if KEY_CACHE_TTL_SECONDS > 0:
                with _KEY_CACHE_LOCK:
                    _KEY_CACHE[cache_key] = (
                        now_mono + KEY_CACHE_TTL_SECONDS,
                        {"id": row["id"], "expires_at": row["expires_at"]},
                    )
        except HTTPException:
            raise
        except sqlite3.Error:
            raise HTTPException(status_code=401, detail="API key is invalid, disabled, or revoked") from None
        finally:
            connection.close()


def _verify_htpasswd_hash(password: str, stored_hash: str) -> bool:
    if stored_hash.startswith("{SHA}"):
        digest = base64.b64encode(hashlib.sha1(password.encode("utf-8")).digest()).decode("ascii")
        return hmac.compare_digest(stored_hash, "{SHA}" + digest)
    if stored_hash.startswith(("$2a$", "$2b$", "$2y$")):
        try:
            import bcrypt  # type: ignore
            return bcrypt.checkpw(password.encode("utf-8"), stored_hash.encode("ascii"))
        except ImportError:
            pass
    try:
        import crypt  # type: ignore
        return hmac.compare_digest(crypt.crypt(password, stored_hash), stored_hash)
    except (ImportError, OSError):
        return False


def _load_htpasswd_entries(htpasswd_path: Path) -> list[tuple[str, str]]:
    """Load and parse htpasswd entries with st_mtime_ns + st_size memory cache."""
    try:
        st = htpasswd_path.stat()
    except OSError as exc:
        raise HTTPException(status_code=401, detail="Basic authentication store is unavailable") from exc
    cache_key = str(htpasswd_path)
    sig = (st.st_mtime_ns, st.st_size)
    with _HTPASSWD_CACHE_LOCK:
        cached = _HTPASSWD_CACHE.get(cache_key)
        if cached is not None and cached[0] == sig:
            return cached[1]
    try:
        lines = htpasswd_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise HTTPException(status_code=401, detail="Basic authentication store is unavailable") from exc
    entries: list[tuple[str, str]] = []
    for line in lines:
        if not line or line.startswith("#"):
            continue
        entry_user, entry_sep, entry_hash = line.partition(":")
        if entry_sep and entry_user:
            entries.append((entry_user, entry_hash.strip()))
    with _HTPASSWD_CACHE_LOCK:
        _HTPASSWD_CACHE[cache_key] = (sig, entries)
    return entries


def _validate_basic_auth(encoded: str) -> None:
    if not encoded:
        raise HTTPException(status_code=401, detail="Basic authentication credentials are required")
    try:
        decoded = base64.b64decode(encoded.encode("ascii"), validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        raise HTTPException(status_code=401, detail="Invalid Basic authentication header") from None
    username, sep, password = decoded.partition(":")
    if not sep or not username or not password:
        raise HTTPException(status_code=401, detail="Invalid Basic authentication credentials")

    configured_pairs = os.environ.get("BRM_LAN_API_BASIC_AUTH", "").strip()
    if configured_pairs:
        for item in configured_pairs.split(","):
            cfg_user, cfg_sep, cfg_pass = item.strip().partition(":")
            if cfg_sep and hmac.compare_digest(username, cfg_user) and hmac.compare_digest(password, cfg_pass):
                return
        raise HTTPException(status_code=401, detail="Invalid Basic authentication credentials")

    htpasswd_env = os.environ.get("BRM_LAN_API_HTPASSWD", "").strip()
    if htpasswd_env:
        htpasswd_path = Path(htpasswd_env).expanduser()
        entries = _load_htpasswd_entries(htpasswd_path)
        for entry_user, entry_hash in entries:
            if hmac.compare_digest(username, entry_user) and _verify_htpasswd_hash(password, entry_hash):
                return
        raise HTTPException(status_code=401, detail="Invalid Basic authentication credentials")


def _authenticate_request_sync(authorization: str, path: str) -> None:
    if authorization:
        scheme, _, value = authorization.partition(" ")
        lowered = scheme.lower()
        if lowered == "bearer":
            _validate_bearer_api_key(value)
        elif lowered == "basic":
            _validate_basic_auth(value.strip())
        else:
            raise HTTPException(status_code=401, detail="Unsupported authorization scheme")
        return
    unauthenticated_paths = {
        f"{API_PREFIX}/health",
        f"{API_PREFIX}/openapi.json",
        f"{API_PREFIX}/docs",
    }
    require_auth = os.environ.get("BRM_LAN_API_REQUIRE_AUTH", "1").strip().lower() not in {
        "0", "false", "no", "off",
    }
    if require_auth and path not in unauthenticated_paths:
        raise HTTPException(
            status_code=401,
            detail="Authentication required (Bearer API key or Basic auth)",
        )


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    if request.method == "OPTIONS":
        return await call_next(request)
    authorization = request.headers.get("authorization", "")
    try:
        if authorization:
            if anyio is not None:
                await anyio.to_thread.run_sync(_authenticate_request_sync, authorization, request.url.path)
            else:
                await asyncio.to_thread(_authenticate_request_sync, authorization, request.url.path)
        else:
            _authenticate_request_sync("", request.url.path)
    except HTTPException as exc:
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.detail},
            headers={"WWW-Authenticate": 'Bearer, Basic realm="BRMMedia"'},
        )
    return await call_next(request)


def _fail(status_code: int, detail: str) -> None:
    raise HTTPException(status_code=status_code, detail=detail)


def _trim_text(value: Any, field: str, *, maximum: int = 12000, required: bool = True) -> str:
    if not isinstance(value, str):
        _fail(422, f"{field} must be a string")
    value = value.strip()
    if required and not value:
        _fail(422, f"{field} must not be empty")
    if len(value) > maximum:
        _fail(422, f"{field} must be at most {maximum} characters")
    return value


def _number(value: Any, field: str, *, minimum: float, maximum: float, integer: bool = False) -> float | int:
    if isinstance(value, bool):
        _fail(422, f"{field} must be a number")
    try:
        numeric = float(value)
        if integer and not numeric.is_integer():
            _fail(422, f"{field} must be an integer")
        parsed = int(numeric) if integer else numeric
    except (TypeError, ValueError):
        _fail(422, f"{field} must be a number")
    if parsed < minimum or parsed > maximum:
        _fail(422, f"{field} must be between {minimum:g} and {maximum:g}")
    return parsed


def _choice(value: Any, field: str, choices: list[str], default: str | None = None) -> str:
    if value is None and default is not None:
        value = default
    if value not in choices:
        _fail(422, f"{field} must be one of: {', '.join(choices)}")
    return str(value)


def _available_music_models() -> list[str]:
    """Discover installed ACE-Step DiT & YuE2-3B variants from ComfyUI's live schema (60s TTL cache)."""
    global _MUSIC_MODELS_CACHE
    base_key = COMFY_BASE.rstrip("/")
    now = time.monotonic()
    with _MUSIC_MODELS_LOCK:
        c_base, c_ts, c_vals = _MUSIC_MODELS_CACHE
        if c_base == base_key and c_vals and (now - c_ts) < 60.0:
            return list(c_vals)
    available: list[str] = []
    try:
        payload = _http_session().get(f"{base_key}/object_info/UNETLoader", timeout=5).json()
        values = payload["UNETLoader"]["input"]["required"]["unet_name"][0]
        if isinstance(values, list):
            for model in MUSIC_MODEL_CANDIDATES:
                candidates = (
                    f"acestep/acestep_v1.5_xl_{model}_bf16.safetensors",
                    f"acestep_v1.5_xl_{model}_bf16.safetensors",
                )
                if any(c in values for c in candidates):
                    available.append(model)
    except (KeyError, TypeError, ValueError, requests.RequestException):
        pass
    try:
        ckpt_payload = _http_session().get(f"{base_key}/object_info/CheckpointLoaderSimple", timeout=5).json()
        ckpts = ckpt_payload["CheckpointLoaderSimple"]["input"]["required"]["ckpt_name"][0]
        ckpt_dir = Path(COMFY_ROOT) / "models" / "checkpoints"
        if isinstance(ckpts, list) and any(
            "yue2" in str(c).lower() and not (ckpt_dir / f"{c}.aria2").exists()
            for c in ckpts
        ):
            available.append("yue2")
    except (KeyError, TypeError, ValueError, requests.RequestException):
        pass
    if available:
        with _MUSIC_MODELS_LOCK:
            _MUSIC_MODELS_CACHE = (base_key, time.monotonic(), list(available))
    return available


def _default_music_model(available: list[str], *, has_lyrics: bool = True) -> str:
    if not available:
        _fail(503, "no ACE-Step or YuE2 music model is installed")
    order = ("base", "yue2", "turbo") if has_lyrics else ("turbo", "base", "yue2")
    for cand in order:
        if cand in available:
            return cand
    return available[0]


def _load_assets() -> dict[str, AssetRecord]:
    global _ASSETS_CACHE
    index_key = str(ASSET_INDEX_PATH)
    try:
        st = ASSET_INDEX_PATH.stat()
        sig = (st.st_mtime_ns, st.st_size)
    except FileNotFoundError:
        with _ASSETS_CACHE_LOCK:
            _ASSETS_CACHE = ("", None, {})
        return {}
    except OSError:
        return {}

    now = time.time()
    with _ASSETS_CACHE_LOCK:
        c_path, c_sig, c_records = _ASSETS_CACHE
        if c_path == index_key and c_sig == sig:
            if not any(
                not isinstance(rec.created_at, (int, float)) or (now - rec.created_at > ASSET_TTL_SECONDS)
                for rec in c_records.values()
            ):
                return dict(c_records)

    try:
        payload = json.loads(ASSET_INDEX_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        with _ASSETS_CACHE_LOCK:
            _ASSETS_CACHE = ("", None, {})
        return {}
    except (OSError, json.JSONDecodeError):
        return {}
    records: dict[str, AssetRecord] = {}
    changed = False
    for raw in payload.get("assets", []):
        try:
            record = AssetRecord(**raw)
        except (TypeError, ValueError):
            changed = True
            continue
        if not isinstance(record.created_at, (int, float)) or now - record.created_at > ASSET_TTL_SECONDS:
            changed = True
            continue
        records[record.asset_id] = record
    if changed:
        _save_assets(records)
    else:
        try:
            st_after = ASSET_INDEX_PATH.stat()
            with _ASSETS_CACHE_LOCK:
                _ASSETS_CACHE = (index_key, (st_after.st_mtime_ns, st_after.st_size), dict(records))
        except OSError:
            pass
    return records


def _save_assets(records: dict[str, AssetRecord]) -> None:
    global _ASSETS_CACHE
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    payload = {"version": 1, "assets": [record.__dict__ for record in records.values()]}
    temporary = ASSET_INDEX_PATH.with_name(f".{ASSET_INDEX_PATH.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(ASSET_INDEX_PATH)
    try:
        st = ASSET_INDEX_PATH.stat()
        with _ASSETS_CACHE_LOCK:
            _ASSETS_CACHE = (str(ASSET_INDEX_PATH), (st.st_mtime_ns, st.st_size), dict(records))
    except OSError:
        pass


def _asset(params: dict[str, Any], assets: dict[str, AssetRecord], field: str, kind: str) -> AssetRecord:
    asset_id = _trim_text(params.get(field), field, maximum=64)
    record = assets.get(asset_id)
    if record is None:
        _fail(422, f"{field} is unknown or expired; upload the file again")
    if record.kind != kind:
        _fail(422, f"{field} must reference an uploaded {kind} asset")
    return record


def _normal_text_to_image(params: dict[str, Any], assets: dict[str, AssetRecord]) -> list[Any]:
    return [
        _trim_text(params.get("prompt"), "prompt"),
        _choice(params.get("size"), "size", SIZE_VALUES, "1024 × 1024"),
        _number(params.get("batch", 1), "batch", minimum=1, maximum=4, integer=True),
    ]


def _normal_qwen_image21(params: dict[str, Any], assets: dict[str, AssetRecord]) -> list[Any]:
    from qwen_image21_support import validate
    try:
        a = validate(
            params.get("prompt"),
            params.get("width", 1024),
            params.get("height", 1024),
            params.get("steps"),
            params.get("seed", 0),
            turbo=params.get("turbo"),
        )
    except (TypeError, ValueError, OverflowError) as exc:
        _fail(422, str(exc))
    return [a[k] for k in ("prompt", "width", "height", "steps", "seed", "turbo")]


def _normal_qwen_image21_edit(params: dict[str, Any], assets: dict[str, AssetRecord]) -> list[Any]:
    image = _asset(params, assets, "image_asset_id", "image")
    from qwen_image21_support import validate_edit
    try:
        a = validate_edit(
            params.get("prompt"),
            params.get("width", 1024),
            params.get("height", 1024),
            params.get("steps"),
            params.get("seed", 0),
            image.filename,
            turbo=params.get("turbo"),
        )
    except (TypeError, ValueError, OverflowError) as exc:
        _fail(422, str(exc))
    return [a[k] for k in ("prompt", "width", "height", "steps", "seed", "input_filename", "turbo")]


def _normal_h3_image(params: dict[str, Any], assets: dict[str, AssetRecord]) -> list[Any]:
    from h3_image_support import validate
    if set(params) - {"prompt", "width", "height", "seed"}:
        _fail(422, "H3 Image accepts only prompt, width, height and seed")
    try:
        a = validate(params.get("prompt"), params.get("width", 2048), params.get("height", 2048), params.get("seed", 0))
    except (TypeError, ValueError, OverflowError) as exc:
        _fail(422, str(exc))
    return [a[k] for k in ("prompt", "width", "height", "seed")]


def _normal_h3_image_edit(params: dict[str, Any], assets: dict[str, AssetRecord]) -> list[Any]:
    image = _asset(params, assets, "image_asset_id", "image")
    return _normal_h3_image({k: v for k, v in params.items() if k != "image_asset_id"}, assets) + [image.filename]


def _normal_image_edit(params: dict[str, Any], assets: dict[str, AssetRecord]) -> list[Any]:
    image = _asset(params, assets, "image_asset_id", "image")
    return [_trim_text(params.get("prompt"), "prompt"), image.filename]


def _normal_text_to_video(params: dict[str, Any], assets: dict[str, AssetRecord]) -> list[Any]:
    profile = _choice(params.get("profile"), "profile", H3_PROFILE_VALUES, "preview")
    acceleration = _choice(
        params.get("acceleration"), "acceleration", H3_ACCELERATION_VALUES, "standard"
    )
    size = _choice(params.get("size"), "size", SIZE_VALUES, "768 × 1024")
    seconds = _number(params.get("seconds", H3_PROFILE_DETAILS[profile]["default_seconds"]), "seconds",
                      minimum=H3_PROFILE_DETAILS[profile]["minimum_seconds"],
                      maximum=H3_PROFILE_DETAILS[profile]["maximum_seconds"], integer=True)
    _validate_h3_acceleration(profile, size, acceleration)
    return [
        _trim_text(params.get("prompt"), "prompt"),
        size,
        seconds,
        profile, acceleration,
    ]


def _normal_image_to_video(params: dict[str, Any], assets: dict[str, AssetRecord]) -> list[Any]:
    image = _asset(params, assets, "image_asset_id", "image")
    profile = _choice(params.get("profile"), "profile", H3_PROFILE_VALUES, "preview")
    acceleration = _choice(
        params.get("acceleration"), "acceleration", H3_ACCELERATION_VALUES, "standard"
    )
    size = _choice(params.get("size"), "size", SIZE_VALUES, "768 × 1024")
    seconds = _number(params.get("seconds", H3_PROFILE_DETAILS[profile]["default_seconds"]), "seconds",
                      minimum=H3_PROFILE_DETAILS[profile]["minimum_seconds"],
                      maximum=H3_PROFILE_DETAILS[profile]["maximum_seconds"], integer=True)
    _validate_h3_acceleration(profile, size, acceleration)
    return [
        _trim_text(params.get("prompt"), "prompt"), image.filename,
        size,
        seconds,
        profile, acceleration,
    ]


def _validate_h3_acceleration(profile: str, size: str, acceleration: str) -> None:
    match = re.search(r"(\d+)\s*[×xX*]\s*(\d+)", size)
    is_landscape_16_9 = bool(
        match
        and int(match.group(1)) > int(match.group(2))
        # H3/LightX2V's trained 768P production cell is 1344x768.  Its
        # storage-friendly canvas ratio is 1.75 rather than an exact 16:9
        # 1.777..., so keep the guard narrow but include that official cell.
        and abs(int(match.group(1)) / int(match.group(2)) - 16 / 9) <= 0.03
    )
    if acceleration == "turbo_fast" and (profile != "quality" or not is_landscape_16_9):
        _fail(422, "turbo_fast initially requires profile=quality and a 16:9 landscape size")


def _normal_ltx_text_to_video(params: dict[str, Any], assets: dict[str, AssetRecord]) -> list[Any]:
    return [
        _trim_text(params.get("prompt"), "prompt"),
        _choice(params.get("size"), "size", SIZE_VALUES, "768 × 1024"),
        _number(params.get("seconds", 5), "seconds", minimum=2, maximum=360, integer=True),
    ]


def _normal_ltx_image_to_video(params: dict[str, Any], assets: dict[str, AssetRecord]) -> list[Any]:
    image = _asset(params, assets, "image_asset_id", "image")
    return [
        _trim_text(params.get("prompt"), "prompt"), image.filename,
        _number(params.get("seconds", 5), "seconds", minimum=2, maximum=360, integer=True),
    ]


def _normal_first_last_frame(params: dict[str, Any], assets: dict[str, AssetRecord]) -> list[Any]:
    first = _asset(params, assets, "first_image_asset_id", "image")
    last = _asset(params, assets, "last_image_asset_id", "image")
    return [
        _trim_text(params.get("prompt"), "prompt"), first.filename, last.filename,
        _number(params.get("seconds", 5), "seconds", minimum=2, maximum=360, integer=True),
    ]


def _normal_talking_head(params: dict[str, Any], assets: dict[str, AssetRecord]) -> list[Any]:
    image = _asset(params, assets, "image_asset_id", "image")
    audio = _asset(params, assets, "audio_asset_id", "audio")
    duration = _number(params.get("duration"), "duration", minimum=0.01, maximum=3600)
    return [
        _trim_text(params.get("prompt"), "prompt"), image.filename, audio.filename, duration,
        _choice(params.get("size"), "size", SIZE_VALUES, "768 × 1024"),
    ]


def _normal_flux2_fast(params: dict[str, Any], assets: dict[str, AssetRecord]) -> list[Any]:
    from flux2_fast_support import validate
    image=_asset(params,assets,"image_asset_id","image")
    try:
        a=validate(params.get("prompt"),image.filename,params.get("width",1024),params.get("height",1024),params.get("seed",42))
    except (TypeError,ValueError,OverflowError) as exc:
        _fail(422,str(exc))
    return [a[k] for k in ("prompt","input_filename","width","height","seed")]


def _normal_flux3(params: dict[str, Any], assets: dict[str, AssetRecord], edit=False) -> list[Any]:
    from flux3_image_support import validate
    image=_asset(params,assets,"image_asset_id","image") if edit else None
    try:
        a=validate(params.get("prompt"),params.get("width",1024),params.get("height",1024),params.get("seed",42),image.filename if image else None)
    except (TypeError,ValueError,OverflowError) as exc:
        _fail(422,str(exc))
    return [a[k] for k in (("prompt","width","height","seed","input_filename") if edit else ("prompt","width","height","seed"))]


def _normal_qwen3_tts(params: dict[str, Any], assets: dict[str, AssetRecord]) -> list[Any]:
    from qwen3_tts_support import validate, check_reference
    audio=_asset(params,assets,"ref_audio_asset_id","audio")
    try:
        a=validate(params.get("text"),audio.filename,params.get("language","zh"),params.get("reference_text",""))
        check_reference(audio.filename,COMFY_ROOT / "input")
    except (TypeError,ValueError) as exc:
        _fail(422,str(exc))
    return [a[k] for k in ("text","input_filename","language","reference_text")]


ZONOS3_LANGUAGE_MAP = {
    "zh": "cmn", "cmn": "cmn", "en": "en_us", "en_us": "en_us", "en_gb": "en_gb",
    "ja": "ja", "ko": "ko", "fr": "fr_fr", "fr_fr": "fr_fr", "de": "de",
    "es": "es", "pt": "pt_br", "pt_br": "pt_br", "it": "it", "ru": "ru",
}


def _normal_zonos3(params: dict[str, Any], assets: dict[str, AssetRecord]) -> list[Any]:
    text_value = params.get("text") if params.get("text") is not None else params.get("prompt")
    text = _trim_text(text_value, "text", maximum=2000)
    seed = _number(params.get("seed", 42), "seed", minimum=0, maximum=2**32 - 1, integer=True)
    raw_lang = str(params.get("language") or "zh").strip().lower()
    if raw_lang not in ZONOS3_LANGUAGE_MAP:
        _fail(422, f"language must be one of: {', '.join(sorted(ZONOS3_LANGUAGE_MAP))}")
    payload: dict[str, Any] = {
        "text": text,
        "format": "wav",
        "stream": False,
        "language": ZONOS3_LANGUAGE_MAP[raw_lang],
        "seed": seed,
        "speaker_mode": "embedding",
    }
    if params.get("ref_audio_asset_id"):
        audio = _asset(params, assets, "ref_audio_asset_id", "audio")
        try:
            audio_path = resolve_comfy_input(COMFY_ROOT, audio.filename)
            audio_bytes = audio_path.read_bytes()
        except (OSError, ValueError) as exc:
            _fail(422, f"unable to read reference audio: {exc}")
        if len(audio_bytes) > 16 * 1024**2:
            _fail(422, "reference audio must be at most 16 MiB")
        payload["speaker_audio_base64"] = base64.b64encode(audio_bytes).decode("ascii")
    elif params.get("speaker_audio_base64") is not None:
        raw_b64 = _trim_text(params.get("speaker_audio_base64"), "speaker_audio_base64", maximum=24 * 1024 * 1024)
        try:
            decoded = base64.b64decode(raw_b64.encode("ascii"), validate=True)
        except (binascii.Error, ValueError):
            _fail(422, "speaker_audio_base64 must be valid base64")
        if len(decoded) > 16 * 1024**2:
            _fail(422, "reference audio must be at most 16 MiB")
        payload["speaker_audio_base64"] = raw_b64
    return [payload]


def _normal_breeze_tts2(params: dict[str, Any], assets: dict[str, AssetRecord]) -> list[Any]:
    text_value = params.get("text") if params.get("text") is not None else params.get("prompt")
    text = _trim_text(text_value, "text", maximum=breeze_tts2_support.DEFAULT_MAX_TEXT_CHARS)
    raw_lang = str(params.get("language") or "zh").strip().lower()
    try:
        lang = breeze_tts2_support.normalise_language(raw_lang)
    except ValueError as exc:
        _fail(422, str(exc))
    speed = float(_number(params.get("speed", 1.0), "speed", minimum=0.5, maximum=2.0))
    seed = int(_number(params.get("seed", 42), "seed", minimum=0, maximum=2**32 - 1, integer=True))
    cfg_scale = float(_number(params.get("cfg_scale", 1.0), "cfg_scale", minimum=1.0, maximum=5.0))
    ref_text = _trim_text(params.get("reference_text") if params.get("reference_text") is not None else params.get("ref_text", ""), "reference_text", maximum=2000, required=False)
    instruction = _trim_text(params.get("instruction", ""), "instruction", maximum=1000, required=False)
    ref_filename = ""
    if params.get("ref_audio_asset_id"):
        audio = _asset(params, assets, "ref_audio_asset_id", "audio")
        try:
            resolve_comfy_input(COMFY_ROOT, audio.filename)
        except (OSError, ValueError) as exc:
            _fail(422, f"unable to read reference audio: {exc}")
        ref_filename = audio.filename
    return [text, ref_filename, lang, speed, ref_text, instruction, seed, cfg_scale]


def _normal_voice_clone(params: dict[str, Any], assets: dict[str, AssetRecord]) -> list[Any]:
    audio = _asset(params, assets, "ref_audio_asset_id", "audio")
    prompt_value = params.get("prompt") if params.get("prompt") is not None else params.get("text")
    return [
        _trim_text(prompt_value, "prompt"), audio.filename,
        # Retain this positional value for existing Gradio/API callers.  The
        # IndexTTS-2.5 submitter records it as deprecated and deliberately
        # ignores it in inference.
        _number(params.get("temperature", 0.8), "temperature", minimum=0, maximum=1.5),
        _choice(params.get("language"), "language", VOICE_CLONE_LANGUAGE_VALUES, "zh"),
        _number(params.get("speed", 1.0), "speed", minimum=0.5, maximum=2.0),
    ]


def _detect_music_language(lyrics: str, requested_language: Any) -> str:
    raw_lyrics = str(lyrics or "").strip()
    lang = str(requested_language or "").strip().lower()
    if raw_lyrics and raw_lyrics.lower() not in {"[inst]", "[instrumental]", "inst", "instrumental", "纯音乐", "无歌词"}:
        cjk_count = len(re.findall(r"[\u4e00-\u9fff]", raw_lyrics))
        if lang in {"", "auto", "unknown"} or (lang == "en" and cjk_count >= 2):
            if len(re.findall(r"[\u3040-\u30ff]", raw_lyrics)) >= 2:
                return "ja"
            if len(re.findall(r"[\uac00-\ud7af]", raw_lyrics)) >= 2:
                return "ko"
            if cjk_count >= 1:
                return "zh"
    return _choice(requested_language, "language", LANGUAGE_VALUES, "zh")


def _normal_music(params: dict[str, Any], assets: dict[str, AssetRecord]) -> list[Any]:
    lyrics = params.get("lyrics", "")
    if lyrics is None:
        lyrics = ""
    lyrics_str = _trim_text(lyrics, "lyrics", maximum=12000, required=False)
    has_lyrics = bool(lyrics_str) and lyrics_str.lower() not in {
        "[inst]", "[instrumental]", "inst", "instrumental", "纯音乐", "无歌词",
    }
    available_models = _available_music_models()
    raw_model = params.get("model")
    if isinstance(raw_model, str):
        normalized_model = raw_model.strip().lower()
        alias_map = {
            "sft": "base" if "base" in available_models else "turbo",
            "ace-step-sft": "turbo",
            "ace-step-turbo": "turbo",
            "ace-step-base": "base",
            "yue2": "yue2",
            "yue2-3b": "yue2",
            "yue2-3b-music": "yue2",
            "auto": _default_music_model(available_models, has_lyrics=has_lyrics),
            "": _default_music_model(available_models, has_lyrics=has_lyrics),
        }
        if normalized_model in alias_map:
            mapped = alias_map[normalized_model]
            raw_model = mapped if mapped in available_models else _default_music_model(available_models, has_lyrics=has_lyrics)
    tags_raw = params.get("tags") if params.get("tags") is not None else params.get("style")
    return [
        _trim_text(tags_raw, "tags", maximum=2000),
        lyrics_str,
        _number(params.get("duration", 30), "duration", minimum=1, maximum=600),
        _number(params.get("bpm", 120), "bpm", minimum=30, maximum=300, integer=True),
        _detect_music_language(lyrics_str, params.get("language")),
        _choice(raw_model, "model", available_models, _default_music_model(available_models, has_lyrics=has_lyrics)),
    ]


def _normal_yue2_music(params: dict[str, Any], assets: dict[str, AssetRecord]) -> list[Any]:
    if "yue2" not in _available_music_models():
        _fail(503, "YuE2-3B checkpoint is not installed yet")
    tags_raw = params.get("tags") if params.get("tags") is not None else params.get("style")
    lyrics = params.get("lyrics", "")
    if lyrics is None:
        lyrics = ""
    lyrics_str = _trim_text(lyrics, "lyrics", maximum=12000, required=False)
    duration_raw = params.get("duration") if params.get("duration") is not None else params.get("max_duration", 120)
    mode = _choice(params.get("mode"), "mode", ["full", "melody"], "full")
    abc_planning = bool(params.get("abc_planning", True))
    steps = _number(params.get("steps", 32), "steps", minimum=16, maximum=64, integer=True)
    return [
        _trim_text(tags_raw, "tags", maximum=2000),
        lyrics_str,
        _number(duration_raw, "duration", minimum=10, maximum=360),
        mode,
        abc_planning,
        steps,
    ]


def _normal_seedvr2_enhance(params: dict[str, Any], assets: dict[str, AssetRecord]) -> list[Any]:
    mode = _choice(params.get("mode"), "mode", ["image", "video"], "image")
    model_variant = _choice(
        params.get("model_variant"), "model_variant", list(SEEDVR2_MODELS), SEEDVR2_MODEL_DEFAULT,
    )
    scale = _number(params.get("scale", 2), "scale", minimum=1, maximum=2, integer=True)
    strength = _choice(params.get("strength"), "strength", list(STRENGTH_DENOISE), "standard")
    resolution_preset = "native"
    if mode == "video":
        requested_preset = str(params.get("resolution_preset", "native") or "native").strip().lower()
        if requested_preset == "4k":
            _fail(
                422,
                "4K UHD 预设暂未开放：3840×2160 超出当前 A5000 安全输出上限。"
                "需先完成 4K 显存、稳定性与画质实测后再启用。",
            )
        resolution_preset = _choice(
            requested_preset, "resolution_preset", ["native", "720p", "1080p"], "native",
        )
    image_filename = ""
    video_filename = ""
    if mode == "image":
        image = _asset(params, assets, "image_asset_id", "image")
        try:
            image_path = resolve_comfy_input(COMFY_ROOT, image.filename)
            from PIL import Image

            with Image.open(image_path) as source:
                output_dimensions(source.width, source.height, int(scale))
        except ImportError:
            _fail(503, "图像尺寸检查依赖 Pillow 不可用")
        except (OSError, ValueError) as exc:
            _fail(422, f"无法验证图像素材：{exc}")
        image_filename = image.filename
    else:
        video = _asset(params, assets, "video_asset_id", "video")
        try:
            video_path = resolve_comfy_input(COMFY_ROOT, video.filename)
            info = probe_video(video_path)
            validate_video_info(info)
            _, _, scale_by = video_output_dimensions(
                info["width"], info["height"], resolution_preset, int(scale),
            )
        except (OSError, RuntimeError, ValueError) as exc:
            _fail(422, str(exc))
        try:
            required = estimated_video_workspace_bytes(video.size_bytes, 2 if scale_by > 1 else 1)
            free = available_disk_bytes({OUTPUT_DIR, video_path.parent})
        except OSError as exc:
            _fail(503, f"无法检查视频处理空间：{exc}")
        if free < required:
            _fail(
                507,
                f"视频处理至少需要 {required / 1024 ** 3:.1f} GiB 可用空间，"
                f"当前仅 {free / 1024 ** 3:.1f} GiB；请先清理空间。",
            )
        video_filename = video.filename
    return [mode, image_filename, video_filename, int(scale), strength, resolution_preset, model_variant]


H3_IMAGE_OPTIONS = {"engine": "MiniMax H3 Image INT8", "usage": "internal research/evaluation", "max_pixels": 4194304,
    "max_edge": 4096, "size_multiple": 32, "additional_sizes": [[1920,1080],[1080,1920]], "default_width": 2048, "default_height": 2048,
    "batch": 1, "steps": 6, "cfg": 1, "sampler": "euler", "scheduler": "simple", "prompt_enhancement": False}

QWEN_IMAGE21_OPTIONS = {
    "license": "qwen-research; non-commercial research/evaluation only",
    "turbo": True,
    "turbo_lora": "qwen_image_2.1_turbo_lora_avg_rank_178_bf16.safetensors",
    "max_pixels": 2073600,
    "standard_max_pixels": 1048576,
    "additional_sizes": [[1920, 1080], [1080, 1920]],
    "batch": 1,
    "default_steps": 8,
    "steps": [1, 40],
    "size_multiple": 32,
}

ZONOS3_OPTIONS = {
    "engine": "ZONOS3-Mini",
    "direct_service": ZONOS3_BASE,
    "output": "wav",
    "max_text_chars": 2000,
    "speaker_mode": "embedding",
    "languages": ["zh", "cmn", "en", "en_us", "en_gb", "ja", "ko", "fr", "de", "es", "pt", "it", "ru"],
    "params": {
        "text": {"type": "string", "required": True, "maximum_length": 2000, "description": "要合成的文本（兼容 prompt 字段）"},
        "ref_audio_asset_id": {"type": "asset_id", "required": False, "asset_kind": "audio", "description": "可选参考音色音频 asset_id（兼容 speaker_audio_base64）"},
        "language": {"type": "string", "required": False, "default": "zh", "description": "合成语言（zh 自动映射 cmn，en 自动映射 en_us）"},
        "seed": {"type": "integer", "required": False, "default": 42},
    },
}

BREEZE_TTS2_OPTIONS = {
    "engine": "Breeze-TTS2",
    "direct_service": BREEZE_TTS2_BASE,
    "output": "wav",
    "sample_rate_hz": 24000,
    "max_text_chars": breeze_tts2_support.DEFAULT_MAX_TEXT_CHARS,
    "languages": ["zh", "en", "auto"],
    "params": {
        "text": {"type": "string", "required": True, "maximum_length": breeze_tts2_support.DEFAULT_MAX_TEXT_CHARS, "description": "要合成的文本（兼容 prompt 字段；支持 [叹气]/[笑] 与 (sigh)/(laugh) 情感标签）"},
        "ref_audio_asset_id": {"type": "asset_id", "required": False, "asset_kind": "audio", "description": "可选参考音色音频 asset_id"},
        "reference_text": {"type": "string", "required": False, "default": "", "description": "可选参考音频逐字稿（兼容 ref_text）"},
        "instruction": {"type": "string", "required": False, "default": "", "description": "可选音色描述或语气指令"},
        "language": {"type": "string", "required": False, "default": "zh", "enum": ["zh", "en", "auto"]},
        "speed": {"type": "number", "required": False, "default": 1.0, "minimum": 0.5, "maximum": 2.0},
        "seed": {"type": "integer", "required": False, "default": 42},
        "cfg_scale": {"type": "number", "required": False, "default": 1.0, "minimum": 1.0, "maximum": 5.0},
    },
}

WORKFLOWS: dict[str, WorkflowSpec] = {
    "h3-image-text-to-image": WorkflowSpec("submit_h3_image", "MiniMax H3 Image 文生图", {}, _normal_h3_image, H3_IMAGE_OPTIONS),
    "h3-image-edit": WorkflowSpec("submit_h3_image_edit", "MiniMax H3 Image 单图编辑", {"image_asset_id": "image"}, _normal_h3_image_edit, H3_IMAGE_OPTIONS),
    "qwen-image21": WorkflowSpec("submit_qwen_image21", "Qwen Image 2.1 文生图（默认 Turbo 8 步）", {}, _normal_qwen_image21, QWEN_IMAGE21_OPTIONS),
    "qwen-image21-turbo": WorkflowSpec("submit_qwen_image21", "Qwen Image 2.1 Turbo LoRA 8 步极速文生图", {}, _normal_qwen_image21, QWEN_IMAGE21_OPTIONS),
    "qwen-image21-edit": WorkflowSpec("submit_qwen_image21_edit", "Qwen Image 2.1 图生图（默认 Turbo 8 步）", {"image_asset_id": "image"}, _normal_qwen_image21_edit, QWEN_IMAGE21_OPTIONS),
    "text-to-image": WorkflowSpec("submit_workflow_1", "Z-Image 文生图", {}, _normal_text_to_image),
    "image-edit": WorkflowSpec("submit_workflow_2", "FLUX.2-klein 图片编辑", {"image_asset_id": "image"}, _normal_image_edit),
    "flux3-text-to-image": WorkflowSpec("submit_flux3_text","FLUX 3文生图（实验）",{},_normal_flux3,{"experimental":True,"steps":16,"cfg":1,"batch":1,"output":"png","default_width":1024,"default_height":1024,"max_pixels":1048576,"sizes":[list(x) for x in FLUX3_IMAGE_SIZES],"prompt_enhancement":"official","restart_behavior":"fail_without_replay"}),
    "flux3-image-edit": WorkflowSpec("submit_flux3_edit","FLUX 3单图编辑（实验）",{"image_asset_id":"image"},lambda params,assets:_normal_flux3(params,assets,True),{"experimental":True,"steps":16,"cfg":1,"batch":1,"output":"png","default_width":1024,"default_height":1024,"max_pixels":1048576,"sizes":[list(x) for x in FLUX3_IMAGE_SIZES],"prompt_enhancement":"official","restart_behavior":"fail_without_replay"}),
    "flux2-fast-edit": WorkflowSpec("submit_flux2_fast","FLUX.2 Klein 四步快速编辑",{"image_asset_id":"image"},_normal_flux2_fast,{"steps":4,"cfg":1,"batch":1,"output":"png","default_width":1024,"default_height":1024,"max_pixels":2073600,"sizes":[list(x) for x in FLUX2_FAST_SIZES],"additional_sizes":[[1920,1080],[1080,1920]]}),
    "zonos3-voice-clone": WorkflowSpec("direct_zonos3_voice_clone", "ZONOS3-Mini 语音克隆（直连推理）", {}, _normal_zonos3, ZONOS3_OPTIONS),
    "breeze-tts2-voice-clone": WorkflowSpec("submit_breeze_tts2", "Breeze-TTS2 最新优化版（华语/中英混读语音克隆 · 零显存）", {}, _normal_breeze_tts2, BREEZE_TTS2_OPTIONS),
    "qwen3-voice-clone": WorkflowSpec("submit_qwen3_tts","Qwen3-TTS 语音克隆（实验）",{"ref_audio_asset_id":"audio"},_normal_qwen3_tts,{"experimental":True,"max_text_chars":600,"max_reference_text_chars":2000,"reference_seconds":[1,MAX_REFERENCE_SECONDS],"max_reference_bytes":MAX_REFERENCE_BYTES,"output":"wav","default_language":"zh","languages":list(QWEN3_TTS_LANGUAGES),"tested_languages":["zh","en","ja"]}),
    "text-to-video": WorkflowSpec(
        "submit_workflow_3_h3", "MiniMax H3 本地文生视频（含同步立体声音频）", {},
        _normal_text_to_video,
        {"engine": "MiniMax H3 Base", "default_profile": "preview", "profiles": H3_PROFILE_DETAILS,
         "default_acceleration": "standard", "accelerations": H3_ACCELERATION_DETAILS,
         "seconds": {"minimum": 3, "maximum": 15, "frame_grid": "24fps; effective duration is adjusted to H3's 17k+5 frame grid"},
         "size_rule": "size selects aspect ratio; draft is ~0.4MP/73 frames, preview uses a 480px short edge and quality uses 768px short edge (max long edge 1344)",
         "params": H3_COMMON_PARAMS},
    ),
    "image-to-video": WorkflowSpec(
        "submit_workflow_4_h3", "MiniMax H3 本地图生视频（含同步立体声音频）", {"image_asset_id": "image"},
        _normal_image_to_video,
        {"engine": "MiniMax H3 Base", "default_profile": "preview", "profiles": H3_PROFILE_DETAILS,
         "default_acceleration": "standard", "accelerations": H3_ACCELERATION_DETAILS,
         "seconds": {"minimum": 3, "maximum": 15, "frame_grid": "24fps; effective duration is adjusted to H3's 17k+5 frame grid"},
         "size_rule": "size selects output aspect ratio; the source image is fitted to the H3 canvas",
         "params": {
             "image_asset_id": {
                 "type": "asset_id", "required": True, "asset_kind": "image",
                 "description": "An image asset_id returned by POST /files?kind=image.",
             },
             **H3_COMMON_PARAMS,
         }},
    ),
    "ltx-text-to-video": WorkflowSpec(
        "submit_workflow_3_ltx", "LTX2.3 文生视频", {},
        _normal_ltx_text_to_video, LTX_TEXT_TO_VIDEO_OPTIONS,
    ),
    "ltx-image-to-video": WorkflowSpec(
        "submit_workflow_4_ltx", "LTX2.3 图生视频", {"image_asset_id": "image"},
        _normal_ltx_image_to_video, LTX_IMAGE_TO_VIDEO_OPTIONS,
    ),
    "first-last-frame-video": WorkflowSpec("submit_workflow_5", "LTX2.3 首尾帧视频", {"first_image_asset_id": "image", "last_image_asset_id": "image"}, _normal_first_last_frame),
    "talking-head": WorkflowSpec("submit_workflow_6", "LTX2.3 单图数字人-语音驱动", {"image_asset_id": "image", "audio_asset_id": "audio"}, _normal_talking_head),
    "voice-clone": WorkflowSpec(
        "submit_workflow_7", "IndexTTS-2.5 / ZONOS3 / Breeze-TTS2 语音克隆", {"ref_audio_asset_id": "audio"}, _normal_voice_clone,
        {"engine": "IndexTTS-2.5", "supported_tts_engines": ["indextts25", "zonos3", "breeze_tts2"],
         "service_boundary": "127.0.0.1 only; routed through the global A5000 media queue (or direct :1919 for tts_engine=zonos3, :9206 for tts_engine=breeze_tts2)",
         "output": {"service_format": "WAV", "sample_rate_hz": 22050, "workspace_format": "MP3"},
         "languages": VOICE_CLONE_LANGUAGE_VALUES,
         "params": {
             "prompt": {"type": "string", "required": True, "maximum_length": 12000},
             "ref_audio_asset_id": {"type": "asset_id", "required": True, "asset_kind": "audio"},
             "tts_engine": {"type": "string", "required": False, "default": "indextts25", "enum": ["indextts25", "zonos3", "breeze_tts2"],
                            "description": "Set tts_engine='zonos3' for ZONOS3-Mini (:1919) or tts_engine='breeze_tts2' for Breeze-TTS2 (:9206)."},
             "language": {"type": "string", "required": False, "default": "zh", "enum": VOICE_CLONE_LANGUAGE_VALUES,
                          "description": "Synthesis language for IndexTTS-2.5."},
             "speed": {"type": "number", "required": False, "default": 1.0, "minimum": 0.5, "maximum": 2.0,
                       "description": "Native speech speed; 1.0 is normal."},
             "temperature": {"type": "number", "required": False, "default": 0.8, "minimum": 0, "maximum": 1.5,
                             "deprecated": True, "description": "Accepted for IndexTTS-2 compatibility; ignored by IndexTTS-2.5."},
         }},
    ),
    "music-generate": WorkflowSpec(
        "submit_workflow_8",
        "ACE-Step 1.5 XL / YuE2-3B 音乐生成（带歌词默认 Base 50步强遵循）",
        {},
        _normal_music,
        {"default_model": "base", "supported_models": ["base", "turbo", "yue2"], "retired_aliases": {"sft": "turbo"}},
    ),
    "yue2-3b-music": WorkflowSpec(
        "submit_yue2_music",
        "YuE2-3B 符号ABC+音频融合音乐生成",
        {},
        _normal_yue2_music,
        {"engine": "YuE2-3B", "modes": ["full", "melody"], "default_steps": 32},
    ),
    "seedvr2-enhance": WorkflowSpec(
        "submit_seedvr2_enhance", "SeedVR2 图像/视频增强修复", {},
        _normal_seedvr2_enhance,
        {
            "engine": "SeedVR2 INT8",
            "availability": "ready" if models_installed(COMFY_ROOT) else "models_missing",
            "default_model_variant": SEEDVR2_MODEL_DEFAULT,
            "models": {
                "7b": {
                    "filename": SEEDVR2_MODEL,
                    "sha256": SEEDVR2_MODEL_SHA256,
                    "available": model_installed(COMFY_ROOT, "7b"),
                    "role": "高质量，保留原有默认模型",
                },
                "3b": {
                    "filename": SEEDVR2_FAST_MODEL,
                    "sha256": SEEDVR2_FAST_MODEL_SHA256,
                    "available": model_installed(COMFY_ROOT, "3b"),
                    "role": "快速模式",
                },
            },
            "model": {
                "repository": SEEDVR2_REPO,
                "revision": SEEDVR2_REVISION,
                "diffusion_model": SEEDVR2_MODEL,
                "vae": SEEDVR2_VAE,
            },
            "max_task_seconds": TASK_MAX_SECONDS,
            "task_timeout_seconds": TASK_TIMEOUT_SECONDS,
            "max_output": {"long_edge": MAX_OUTPUT_EDGE, "pixels": MAX_OUTPUT_PIXELS},
            "params": {
                "mode": {"type": "string", "required": False, "default": "image", "enum": ["image", "video"]},
                "model_variant": {
                    "type": "string", "required": False, "default": SEEDVR2_MODEL_DEFAULT,
                    "enum": list(SEEDVR2_MODELS),
                    "description": "7b 保留原高质量模式；3b 为额外快速模式，不替换 7b。",
                },
                "image_asset_id": {"type": "asset_id", "required_for_mode": "image", "asset_kind": "image"},
                "video_asset_id": {"type": "asset_id", "required_for_mode": "video", "asset_kind": "video"},
                "scale": {"type": "integer", "required": False, "default": 2, "enum": [1, 2]},
                "resolution_preset": {
                    "type": "string", "required": False, "default": "native",
                    "enum": ["native", "720p", "1080p"],
                    "planned": sorted(PLANNED_VIDEO_RESOLUTION_PRESETS),
                    "description": "视频专用；按短边指定分辨率并保持源画幅。4K 暂未通过 A5000 安全验证。",
                },
                "strength": {
                    "type": "string", "required": False, "default": "standard",
                    "enum": list(STRENGTH_DENOISE), "denoise": STRENGTH_DENOISE,
                },
            },
        },
    ),
}

INDEXTTS2_WORKFLOW = WorkflowSpec(
    "submit_workflow_7", "IndexTTS-2 语音克隆（回退）", {"ref_audio_asset_id": "audio"}, _normal_voice_clone,
    {"engine": "IndexTTS-2", "availability": "active", "rollback": True,
     "params": {"temperature": {"type": "number", "required": False, "default": 0.8,
                                 "minimum": 0, "maximum": 1.5}}},
)


def _active_env_value(name: str, default: str) -> str:
    """Read a backend engine toggle with mtime-validated dotenv caching.

    The LAN API is started before H3 activation and remains running while the
    activation script updates the candidate `.env`; checking ``st_mtime_ns``
    preserves immediate visibility on `.env` updates while avoiding repeated disk reads.
    """
    global _DOTENV_CACHE
    override = os.environ.get(name)
    if override:
        return override.strip().lower()
    env_path = BASE_DIR / ".env"
    env_key = str(env_path)
    try:
        st = env_path.stat()
        sig = (st.st_mtime_ns, st.st_size)
    except OSError:
        with _DOTENV_CACHE_LOCK:
            if _DOTENV_CACHE[0] == env_key:
                _DOTENV_CACHE = ("", None, {})
        return default
    with _DOTENV_CACHE_LOCK:
        c_path, c_sig, c_map = _DOTENV_CACHE
        if c_path == env_key and c_sig == sig:
            return c_map.get(name, default)
    parsed: dict[str, str] = {}
    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            k, _, v = stripped.partition("=")
            parsed[k.strip()] = v.strip().strip('"').lower()
    except OSError:
        return default
    with _DOTENV_CACHE_LOCK:
        _DOTENV_CACHE = (env_key, sig, parsed)
    return parsed.get(name, default)


def _active_video_engine() -> str:
    return _active_env_value("BRMMEDIA_VIDEO_ENGINE", "ltx23")


def _active_voice_engine() -> str:
    return _active_env_value("BRMMEDIA_VOICE_ENGINE", "indextts2")


def _active_workflows() -> dict[str, WorkflowSpec]:
    """Keep stable REST slugs while advertising the engine that can run now."""
    workflows = dict(WORKFLOWS)
    if os.environ.get("BRM_FLUX3_IMAGE_ENABLED") != "1":
        workflows.pop("flux3-text-to-image", None)
        workflows.pop("flux3-image-edit", None)
    if os.environ.get("BRM_FLUX2_FAST_ENABLED") != "1":
        workflows.pop("flux2-fast-edit", None)
    if os.environ.get("BRM_QWEN3_TTS_ENABLED") != "1":
        workflows.pop("qwen3-voice-clone", None)
    if os.environ.get("BRM_H3_IMAGE_ENABLED") != "1":
        workflows.pop("h3-image-text-to-image", None)
        workflows.pop("h3-image-edit", None)
    if os.environ.get("BRM_QWEN_IMAGE21_ENABLED", "1") == "0":
        workflows.pop("qwen-image21", None)
        workflows.pop("qwen-image21-turbo", None)
        workflows.pop("qwen-image21-edit", None)
    if os.environ.get("BRM_ZONOS3_ENABLED", "1") == "0":
        workflows.pop("zonos3-voice-clone", None)
    if os.environ.get("BRM_BREEZE_TTS2_ENABLED", "1") == "0":
        workflows.pop("breeze-tts2-voice-clone", None)
    if _active_video_engine() != "h3":
        workflows["text-to-video"] = WorkflowSpec(
            "submit_workflow_3", "LTX2.3 文生视频（H3 尚未启用）", {},
            _normal_ltx_text_to_video,
            LTX_TEXT_TO_VIDEO_OPTIONS,
        )
        workflows["image-to-video"] = WorkflowSpec(
            "submit_workflow_4", "LTX2.3 图生视频（H3 尚未启用）", {"image_asset_id": "image"},
            _normal_ltx_image_to_video,
            LTX_IMAGE_TO_VIDEO_OPTIONS,
        )
    if _active_voice_engine() != "indextts25":
        workflows["voice-clone"] = INDEXTTS2_WORKFLOW
    return workflows


def _fit_qwen_image21_dimensions(params: dict[str, Any]) -> None:
    """Scale legacy oversized dimensions (e.g., H3 2048x2048) to Qwen2.1's 1MP grid."""
    raw_w = params.get("width")
    raw_h = params.get("height")
    if raw_w is None or raw_h is None or isinstance(raw_w, bool) or isinstance(raw_h, bool):
        return
    try:
        w, h = int(raw_w), int(raw_h)
    except (TypeError, ValueError):
        return
    if w <= 0 or h <= 0 or (w, h) in ((1920, 1080), (1080, 1920)):
        return
    max_pixels = 1024 * 1024
    if w * h <= max_pixels and 256 <= min(w, h) and max(w, h) <= 1536 and w % 32 == 0 and h % 32 == 0:
        return
    scale = (max_pixels / (w * h)) ** 0.5 if w * h > max_pixels else 1.0
    nw = max(256, min(1536, (int(w * scale) // 32) * 32))
    nh = max(256, min(1536, (int(h * scale) // 32) * 32))
    while nw * nh > max_pixels and max(nw, nh) > 256:
        if nw >= nh and nw > 256:
            nw -= 32
        elif nh > 256:
            nh -= 32
        else:
            break
    params["width"] = nw
    params["height"] = nh


def _resolve_workflow_and_params(
    requested_workflow: str,
    raw_params: dict[str, Any],
) -> tuple[str, dict[str, Any], str | None]:
    """Smoothly redirect retired/aliased workflows so downstream callers get zero 404s."""
    slug = (requested_workflow or "").strip()
    params = dict(raw_params or {})
    lowered = slug.lower()

    if lowered in {"qwen-image21-turbo", "qwen-image21-turbo-edit"}:
        target = "qwen-image21-edit" if "edit" in lowered else "qwen-image21"
        params["turbo"] = True
        if params.get("steps") is None:
            params["steps"] = 8
        return target, params, slug

    if lowered.startswith("h3-image-") or lowered.startswith("flux3-"):
        target = "qwen-image21-edit" if "edit" in lowered else "qwen-image21"
        params["turbo"] = True
        raw_steps = params.get("steps")
        if raw_steps is None or isinstance(raw_steps, bool) or int(raw_steps) > 12:
            params["steps"] = 8
        _fit_qwen_image21_dimensions(params)
        return target, params, slug

    if lowered in {"breeze-tts2", "breezyvoice", "breeze-tts-2", "breeze-tts2-voice-clone"}:
        return "breeze-tts2-voice-clone", params, (slug if lowered != "breeze-tts2-voice-clone" else None)

    if lowered == "qwen3-voice-clone" or lowered.startswith("qwen3-tts"):
        tts_engine = str(params.get("tts_engine", "") or "").strip().lower()
        if tts_engine == "indextts25":
            if params.get("prompt") is None and params.get("text") is not None:
                params["prompt"] = params["text"]
            return "voice-clone", params, slug
        if tts_engine in {"breeze_tts2", "breeze-tts2", "breezyvoice"}:
            return "breeze-tts2-voice-clone", params, slug
        return "zonos3-voice-clone", params, slug

    if lowered == "voice-clone":
        tts_engine = str(params.get("tts_engine", "") or "").strip().lower()
        if tts_engine == "zonos3":
            return "zonos3-voice-clone", params, slug
        if tts_engine in {"breeze_tts2", "breeze-tts2", "breezyvoice"}:
            return "breeze-tts2-voice-clone", params, slug
        return "voice-clone", params, None

    if lowered in {"ace-step-sft", "ace-step-turbo"}:
        params["model"] = "turbo"
        return "music-generate", params, slug

    return slug, params, None


def _local_tasks_path() -> Path:
    return Path(
        os.environ.get("BRM_LAN_API_LOCAL_TASKS_PATH", "/var/lib/brmmedia/lan-api-local-tasks.json")
    ).expanduser()


def _save_local_tasks_locked() -> None:
    path = _local_tasks_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "tasks": list(_LOCAL_TASKS.values())}
        tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(path)
    except OSError:
        # Never break Zonos3 task execution if the persistence directory is read-only in tests.
        pass


def _ensure_local_tasks_loaded_locked() -> None:
    global _LOCAL_TASKS_LOADED_PATH
    path = _local_tasks_path()
    path_key = str(path)
    if _LOCAL_TASKS_LOADED_PATH == path_key:
        return
    _LOCAL_TASKS_LOADED_PATH = path_key
    _LOCAL_TASKS.clear()
    if not path.is_file():
        return
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    items = raw.get("tasks") if isinstance(raw, dict) else (raw if isinstance(raw, list) else [])
    if not isinstance(items, list):
        return
    now = time.time()
    changed = False
    for rec in items:
        if not isinstance(rec, dict) or not isinstance(rec.get("task_id"), str):
            continue
        item = copy.deepcopy(rec)
        if item.get("state") in {"queued", "running"}:
            item["state"] = "failed"
            item["status"] = "执行失败"
            item["queue_position"] = None
            item["finished_at"] = now
            item["error"] = "服务重启导致本地语音任务中断，请重新提交"
            execution = item.get("execution")
            if isinstance(execution, dict):
                execution["stage"] = "failed"
                execution["last_progress_at"] = now
            changed = True
        _LOCAL_TASKS[item["task_id"]] = item
    _prune_local_tasks_locked()
    if changed:
        _save_local_tasks_locked()


def _prune_local_tasks_locked() -> None:
    if len(_LOCAL_TASKS) <= _MAX_LOCAL_TASKS:
        return
    removable = [
        tid for tid, rec in _LOCAL_TASKS.items()
        if rec.get("state") in {"completed", "failed", "cancelled"}
    ]
    removable.sort(key=lambda tid: _LOCAL_TASKS[tid].get("submitted_at") or 0)
    for tid in removable[: max(0, len(_LOCAL_TASKS) - _MAX_LOCAL_TASKS)]:
        _LOCAL_TASKS.pop(tid, None)


def _is_local_task_cancelled(task_id: str) -> bool:
    with _LOCAL_TASKS_LOCK:
        _ensure_local_tasks_loaded_locked()
        rec = _LOCAL_TASKS.get(task_id)
        return rec is None or bool(rec.get("_cancelled"))


def _run_zonos3_worker(task_id: str, payload: dict[str, Any]) -> None:
    workflow_name = "zonos3-voice-clone"
    started_mono: float | None = None
    final_state = "failed"
    final_error = ""
    if not _ZONOS3_GATE.acquire(cancel_check=lambda: _is_local_task_cancelled(task_id)):
        with _LOCAL_TASKS_LOCK:
            rec = _LOCAL_TASKS.get(task_id)
            if rec is not None:
                workflow_name = str(rec.get("workflow") or workflow_name)
                if rec.get("state") in {"queued", "running"}:
                    now = time.time()
                    rec["state"] = "cancelled"
                    rec["status"] = "已取消"
                    rec["queue_position"] = None
                    rec["finished_at"] = now
                    rec["execution"]["stage"] = "interrupted"
                    _save_local_tasks_locked()
        _CONCURRENCY_CONTROLLER.record_task_outcome(
            workflow_name, success=False, duration_s=None, error="cancelled", cancelled=True
        )
        return
    try:
        with _LOCAL_TASKS_LOCK:
            rec = _LOCAL_TASKS.get(task_id)
            if rec is None or rec.get("_cancelled"):
                if rec is not None:
                    workflow_name = str(rec.get("workflow") or workflow_name)
                    now = time.time()
                    rec["state"] = "cancelled"
                    rec["status"] = "已取消"
                    rec["queue_position"] = None
                    rec["finished_at"] = now
                    rec["execution"]["stage"] = "interrupted"
                    _save_local_tasks_locked()
                final_state = "cancelled"
                return
            workflow_name = str(rec.get("workflow") or workflow_name)
            now = time.time()
            started_mono = time.monotonic()
            rec["state"] = "running"
            rec["status"] = "运行中"
            rec["queue_position"] = None
            rec["started_at"] = now
            rec["execution"]["stage"] = "synthesizing_zonos3"
            rec["execution"]["last_progress_at"] = now
            _save_local_tasks_locked()

        try:
            OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

            def _post_zonos3(chunk_payload: dict[str, Any]) -> bytes:
                session_cls = getattr(requests, "Session", None)
                if callable(session_cls):
                    with session_cls() as session:
                        session.trust_env = False
                        response = session.post(
                            f"{ZONOS3_BASE}/v2/generate",
                            json=chunk_payload,
                            timeout=(5, ZONOS3_TIMEOUT_SECONDS),
                        )
                        response.raise_for_status()
                        return response.content
                response = requests.post(
                    f"{ZONOS3_BASE}/v2/generate",
                    json=chunk_payload,
                    timeout=(5, ZONOS3_TIMEOUT_SECONDS),
                )
                response.raise_for_status()
                return response.content

            content, meta = zonos3_support.synthesize_zonos3_wav(
                payload,
                service_url=ZONOS3_BASE,
                timeout_seconds=ZONOS3_TIMEOUT_SECONDS,
                gate=_ZONOS3_GATE,
                cancel_check=lambda: _is_local_task_cancelled(task_id),
                post_fn=_post_zonos3,
            )
            if not content.startswith(b"RIFF") or content[8:12] != b"WAVE":
                raise ValueError("ZONOS3 returned an invalid WAV")

            with _LOCAL_TASKS_LOCK:
                rec = _LOCAL_TASKS.get(task_id)
                if rec is not None and rec.get("_cancelled"):
                    done_at = time.time()
                    rec["state"] = "cancelled"
                    rec["status"] = "已取消"
                    rec["finished_at"] = done_at
                    rec["execution"]["stage"] = "interrupted"
                    _save_local_tasks_locked()
                    final_state = "cancelled"
                    return
                if rec is not None and isinstance(rec.get("effective_settings"), dict):
                    rec["effective_settings"]["chunked"] = bool(meta.get("chunked", False))
                    rec["effective_settings"]["chunks"] = int(meta.get("chunks", 1))
                    rec["effective_settings"]["chunk_workers"] = int(meta.get("workers", 1))

            filename = f"zonos3-{task_id}.wav"
            target = OUTPUT_DIR / filename
            tmp = OUTPUT_DIR / f".{filename}.{os.getpid()}.tmp"
            tmp.write_bytes(content)
            tmp.replace(target)

            done_at = time.time()
            with _LOCAL_TASKS_LOCK:
                rec = _LOCAL_TASKS.get(task_id)
                if rec is not None:
                    rec["state"] = "completed"
                    rec["status"] = "已完成"
                    rec["finished_at"] = done_at
                    rec["output_files"] = [filename]
                    rec["execution"]["stage"] = "completed"
                    rec["execution"]["progress"] = 1.0
                    rec["execution"]["last_progress_at"] = done_at
                    _save_local_tasks_locked()
            final_state = "completed"
        except Exception as exc:
            done_at = time.time()
            final_error = str(exc)
            final_state = "failed"
            with _LOCAL_TASKS_LOCK:
                rec = _LOCAL_TASKS.get(task_id)
                if rec is not None:
                    rec["state"] = "failed"
                    rec["status"] = "执行失败"
                    rec["finished_at"] = done_at
                    rec["error"] = final_error
                    rec["execution"]["stage"] = "failed"
                    rec["execution"]["last_progress_at"] = done_at
                    _save_local_tasks_locked()
    finally:
        _ZONOS3_GATE.release()
        duration_s = (time.monotonic() - started_mono) if started_mono is not None else None
        _CONCURRENCY_CONTROLLER.record_task_outcome(
            workflow_name,
            success=(final_state == "completed"),
            duration_s=duration_s,
            error=final_error,
            cancelled=(final_state == "cancelled"),
        )


def _submit_zonos3_task(
    workflow_name: str,
    payload: dict[str, Any],
    *,
    routed_from: str | None = None,
) -> dict[str, Any]:
    task_id = uuid.uuid4().hex
    now = time.time()
    record: dict[str, Any] = {
        "task_id": task_id,
        "task_name": f"zonos3-{task_id[:8]}",
        "workflow": workflow_name,
        "state": "queued",
        "status": "排队中",
        "queue_position": 1,
        "submitted_at": now,
        "started_at": None,
        "finished_at": None,
        "output_files": [],
        "error": None,
        "prompt_id": None,
        "execution": {
            "stage": "queued",
            "node_id": None,
            "current_step": None,
            "total_steps": None,
            "progress": 0.0,
            "last_progress_at": None,
            "recovered_after_restart": False,
            "stage_seconds": {},
        },
        "effective_settings": {
            "engine": "zonos3-mini",
            "format": payload["format"],
            "speaker_mode": payload["speaker_mode"],
            "seed": payload["seed"],
            "has_reference_audio": "speaker_audio_base64" in payload,
        },
        "_cancelled": False,
    }
    if routed_from:
        record["routed_from"] = routed_from
    with _LOCAL_TASKS_LOCK:
        _ensure_local_tasks_loaded_locked()
        _prune_local_tasks_locked()
        queue_position = sum(1 for r in _LOCAL_TASKS.values() if r.get("state") == "queued") + 1
        record["queue_position"] = queue_position
        _LOCAL_TASKS[task_id] = record
        _save_local_tasks_locked()
    thread = threading.Thread(target=_run_zonos3_worker, args=(task_id, payload), daemon=True)
    thread.start()
    result: dict[str, Any] = {
        "task_id": task_id,
        "workflow": workflow_name,
        "state": "accepted",
        "queue_position": queue_position,
        "links": {"status": f"{API_PREFIX}/tasks/{task_id}"},
    }
    if routed_from:
        result["routed_from"] = routed_from
    return result


def _local_task_status(task_id: str) -> dict[str, Any] | None:
    with _LOCAL_TASKS_LOCK:
        _ensure_local_tasks_loaded_locked()
        rec = _LOCAL_TASKS.get(task_id)
        if rec is None:
            return None
        snapshot = copy.deepcopy(rec)
    snapshot.pop("_cancelled", None)
    return snapshot


def _local_cancel_task(task_id: str) -> dict[str, Any] | None:
    with _LOCAL_TASKS_LOCK:
        _ensure_local_tasks_loaded_locked()
        rec = _LOCAL_TASKS.get(task_id)
        if rec is None:
            return None
        state = rec.get("state")
        if state in {"completed", "failed", "cancelled"}:
            return {"task_id": task_id, "state": state}
        rec["_cancelled"] = True
        if state == "queued":
            rec["state"] = "cancelled"
            rec["status"] = "已取消"
            rec["queue_position"] = None
            rec["finished_at"] = time.time()
            rec["execution"]["stage"] = "interrupted"
            _save_local_tasks_locked()
            return {"task_id": task_id, "state": "cancelled"}
        _save_local_tasks_locked()
        return {"task_id": task_id, "state": "cancel_requested"}


def _local_delete_artifact(task_id: str, filename: str) -> dict[str, Any] | None:
    with _LOCAL_TASKS_LOCK:
        _ensure_local_tasks_loaded_locked()
        rec = _LOCAL_TASKS.get(task_id)
        if rec is None:
            return None
        if rec.get("state") in {"queued", "running"}:
            return {"error": "asset_busy"}
        if filename not in rec.get("output_files", []):
            return {"error": "not_found"}
        try:
            candidate = (OUTPUT_DIR / filename).resolve(strict=True)
            if OUTPUT_DIR not in candidate.parents:
                return {"error": "not_found"}
            candidate.unlink(missing_ok=True)
        except OSError:
            return {"error": "not_found"}
        rec["output_files"] = [name for name in rec["output_files"] if name != filename]
        _save_local_tasks_locked()
        return {"deleted": filename, "task_id": task_id}


with _LOCAL_TASKS_LOCK:
    _ensure_local_tasks_loaded_locked()


def _gradio_endpoint_params(endpoint: str, *, force_refresh: bool = False) -> list[str]:
    base_key = GRADIO_BASE.rstrip("/")
    with _GRADIO_INFO_LOCK:
        cached = _GRADIO_PARAM_CACHE.get(base_key)
        if not force_refresh and cached is not None and endpoint in cached[1]:
            return list(cached[1][endpoint])
    client = _http_session()
    try:
        info = client.get(f"{base_key}/gradio_api/info", timeout=10).json()
        named = info["named_endpoints"]
        mapping: dict[str, list[str]] = {}
        for ep_key, ep_info in named.items():
            ep_name = ep_key.lstrip("/")
            params = ep_info.get("parameters") or []
            mapping[ep_name] = [parameter["parameter_name"] for parameter in params]
        if endpoint not in mapping:
            raise KeyError(endpoint)
    except (KeyError, TypeError, ValueError, requests.RequestException) as exc:
        raise RuntimeError(f"Gradio API metadata is unavailable: {exc}") from exc
    with _GRADIO_INFO_LOCK:
        _GRADIO_PARAM_CACHE[base_key] = (time.monotonic(), mapping)
    return list(mapping[endpoint])


def _gradio_call(endpoint: str, arguments: list[Any]) -> Any:
    """Call the loopback-only Gradio API using cached metadata/Session and SSE."""
    names = _gradio_endpoint_params(endpoint)
    if len(names) != len(arguments):
        names = _gradio_endpoint_params(endpoint, force_refresh=True)
    if len(names) != len(arguments):
        raise RuntimeError(f"Gradio endpoint {endpoint} changed its parameter contract")

    client = _http_session()
    base_key = GRADIO_BASE.rstrip("/")
    try:
        accepted = client.post(
            f"{base_key}/gradio_api/call/v2/{endpoint}",
            json=dict(zip(names, arguments)),
            timeout=15,
        )
        if getattr(accepted, "status_code", 200) in {400, 404, 422}:
            # Schema may have changed after a backend restart; refresh once and retry.
            names = _gradio_endpoint_params(endpoint, force_refresh=True)
            if len(names) != len(arguments):
                raise RuntimeError(f"Gradio endpoint {endpoint} changed its parameter contract")
            accepted = client.post(
                f"{base_key}/gradio_api/call/v2/{endpoint}",
                json=dict(zip(names, arguments)),
                timeout=15,
            )
        accepted.raise_for_status()
        event_id = accepted.json().get("event_id")
        if not event_id:
            raise RuntimeError("Gradio did not return an event id")
        with client.get(
            f"{base_key}/gradio_api/call/v2/{endpoint}/{event_id}",
            headers={"Accept": "text/event-stream"},
            stream=True,
            timeout=GRADIO_TIMEOUT_SECONDS,
        ) as stream:
            stream.raise_for_status()
            event = ""
            for raw_line in stream.iter_lines(decode_unicode=True):
                line = raw_line or ""
                if line.startswith("event: "):
                    event = line[7:]
                elif line.startswith("data: "):
                    data = line[6:]
                    if event == "complete":
                        payload = json.loads(data)
                        # Gradio 6 serializes API callback outputs as its
                        # component-output list, including the common
                        # single-output case used by BRMMedia submit/status
                        # endpoints.  Preserve multi-output payloads while
                        # restoring the endpoint's documented object result.
                        if isinstance(payload, list) and len(payload) == 1:
                            return payload[0]
                        return payload
                    if event == "error":
                        raise RuntimeError(f"Gradio handler error: {data}")
    except (ValueError, requests.RequestException) as exc:
        with _GRADIO_INFO_LOCK:
            _GRADIO_PARAM_CACHE.pop(base_key, None)
        raise RuntimeError(f"Gradio API request failed: {exc}") from exc
    raise RuntimeError("Gradio SSE stream ended before completion")


def _internal_api_token() -> str:
    if os.environ.get("BRM_INTERNAL_API_DISABLED", "").strip().lower() in {"1", "true", "yes", "on"}:
        return ""
    explicit = os.environ.get("BRM_INTERNAL_API_TOKEN", "").strip()
    if explicit:
        return explicit
    api_key = os.environ.get("BRM_API_KEY", "").strip()
    if api_key:
        return hmac.new(api_key.encode("utf-8"), b"brmmedia-internal-v1", hashlib.sha256).hexdigest()
    token_path = OUTPUT_DIR / ".brm-internal-api.token"
    try:
        if token_path.is_file():
            stored = token_path.read_text(encoding="utf-8").strip()
            if stored:
                return stored
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        generated = secrets.token_hex(32)
        try:
            fd = os.open(token_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            return token_path.read_text(encoding="utf-8").strip()
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(generated + "\n")
        return generated
    except OSError:
        return ""


def _internal_api_call(method: str, path: str, *, timeout: float = 5.0) -> dict[str, Any] | None:
    """Call webui.py /internal/v1 routes directly over loopback, bypassing Gradio queue/SSE."""
    token = _internal_api_token()
    if not token:
        return None
    client = _http_session()
    fn = getattr(client, method.lower(), None)
    if not callable(fn):
        return None
    url = f"{GRADIO_BASE.rstrip('/')}{path}"
    try:
        resp = fn(
            url,
            headers={"X-BRM-Internal-Token": token},
            timeout=timeout,
        )
        status_code = getattr(resp, "status_code", 200)
        if status_code in {401, 403, 404, 405, 501}:
            return None
        if hasattr(resp, "raise_for_status"):
            resp.raise_for_status()
        payload = resp.json()
        return payload if isinstance(payload, dict) else None
    except (requests.RequestException, ValueError, OSError):
        return None


def _task_status(task_id: str) -> dict[str, Any]:
    local = _local_task_status(task_id)
    if local is not None:
        return local
    payload = _internal_api_call("GET", f"/internal/v1/tasks/{quote(task_id, safe='')}")
    if payload is None:
        payload = _gradio_call("task_status", [task_id])
    if not isinstance(payload, dict):
        raise RuntimeError("Gradio task_status returned an invalid payload")
    return payload


def _artifact_url(task_id: str, filename: str) -> str:
    return f"{API_PREFIX}/tasks/{task_id}/artifacts/{quote(filename)}"


def _safe_artifact(task_id: str, filename: str) -> Path:
    # Generated task names intentionally remain human-readable.  In
    # particular, MiniMax H3 output names include spaces (for example
    # ``任务_MiniMax H3 文生视频_…mp4``).  A literal space is safe in a
    # filename, while path separators and all other special characters remain
    # forbidden by this allow-list and the resolved-path containment check.
    if not re.fullmatch(r"[a-zA-Z0-9 _\-().\u4e00-\u9fff\uff08\uff09]+\.[a-zA-Z0-9]+", filename or ""):
        _fail(404, "artifact not found")
    status = _task_status(task_id)
    if status.get("state") != "completed":
        _fail(409, "task is not completed")
    if filename not in status.get("output_files", []):
        _fail(404, "artifact not found")
    try:
        candidate = (OUTPUT_DIR / filename).resolve(strict=True)
    except OSError:
        _fail(404, "artifact not found")
    if OUTPUT_DIR not in candidate.parents or candidate.suffix.lower() not in MEDIA_EXTENSIONS or not candidate.is_file():
        _fail(404, "artifact not found")
    return candidate


@app.get(f"{API_PREFIX}/health", tags=["system"])
def health() -> JSONResponse:
    dependencies: dict[str, bool] = {}
    for name, url in {
        "gradio": f"{GRADIO_BASE}/gradio_api/info",
        "comfyui": f"{COMFY_BASE}/system_stats",
    }.items():
        try:
            dependencies[name] = requests.get(url, timeout=5).status_code == 200
        except requests.RequestException:
            dependencies[name] = False
    ready = all(dependencies.values())
    telemetry = system_telemetry.collect_host_telemetry(
        OUTPUT_DIR,
        cache_ttl=3.0,
        self_service="lan_api",
    )
    with _LOCAL_TASKS_LOCK:
        zonos_running = sum(1 for r in _LOCAL_TASKS.values() if r.get("state") == "running")
        zonos_queued = sum(1 for r in _LOCAL_TASKS.values() if r.get("state") == "queued")
    concurrency = dict(telemetry.get("concurrency") or {})
    if concurrency:
        active = dict(concurrency.get("active") or {})
        active.update({
            "zonos3_running": zonos_running,
            "zonos3_queued": zonos_queued,
            "zonos3_gate_active": _ZONOS3_GATE.active_count(),
            "zonos3_effective_limit": _ZONOS3_GATE.current_limit(),
        })
        concurrency["active"] = active
        telemetry = dict(telemetry)
        telemetry["concurrency"] = concurrency
    return JSONResponse(
        status_code=200 if ready else 503,
        content={
            "status": "ok" if ready else "degraded",
            "dependencies": dependencies,
            "output_dir": str(OUTPUT_DIR),
            "concurrency": concurrency,
            "telemetry": telemetry,
        },
    )


@app.get(f"{API_PREFIX}/metrics", tags=["system"])
def get_metrics() -> dict[str, Any]:
    webui_metrics = _internal_api_call("GET", "/internal/v1/metrics", timeout=2.0)
    local_metrics = _CONCURRENCY_CONTROLLER.metrics_snapshot()
    merged = adaptive_concurrency.merge_workflow_metrics(webui_metrics or {}, local_metrics)
    return {
        "status": "ok",
        "generated_at": time.time(),
        "sources": {
            "webui": webui_metrics is not None,
            "lan_api": True,
        },
        "summary": merged["summary"],
        "workflows": merged["workflows"],
    }


@app.get(f"{API_PREFIX}/capabilities", tags=["system"])
def capabilities() -> dict[str, Any]:
    active_engine = _active_video_engine()
    workflows = _active_workflows()
    return {
        "api_version": "v1",
        "auth_modes": ["bearer", "basic"],
        "video_engine": {
            "active": active_engine,
            "h3_available": active_engine == "h3",
        },
        "max_upload_bytes": MAX_UPLOAD_BYTES,
        "max_upload_bytes_by_kind": {
            "image": MAX_UPLOAD_BYTES,
            "audio": MAX_UPLOAD_BYTES,
            "video": VIDEO_UPLOAD_MAX_BYTES,
        },
        "asset_ttl_seconds": ASSET_TTL_SECONDS,
        "size_values": SIZE_VALUES,
        "music_languages": LANGUAGE_VALUES,
        "music_models": _available_music_models(),
        "recommended_models": {
            "image": [
                {"id": "qwen-image21-turbo", "workflow": "qwen-image21", "status": "active", "steps": 8, "recommended": True},
                {"id": "z-image-turbo", "workflow": "text-to-image", "status": "active"},
                {"id": "flux2-fast-edit", "workflow": "flux2-fast-edit", "status": "active"},
            ],
            "tts": [
                {"id": "zonos3-voice-clone", "workflow": "zonos3-voice-clone", "status": "active", "recommended": True},
                {"id": "breeze-tts2-voice-clone", "workflow": "breeze-tts2-voice-clone", "status": "active"},
                {"id": "cosyvoice3-tts", "workflow": "cosyvoice3-tts", "status": "planned"},
                {"id": "indextts25", "workflow": "voice-clone", "status": "active"},
            ],
            "music": [
                {
                    "id": "ace-step-base",
                    "workflow": "music-generate",
                    "model": "base",
                    "status": "active" if "base" in _available_music_models() else "planned",
                    "recommended": True,
                },
                {
                    "id": "ace-step-turbo",
                    "workflow": "music-generate",
                    "model": "turbo",
                    "status": "active" if "turbo" in _available_music_models() else "planned",
                },
                {
                    "id": "yue2-3b-music",
                    "workflow": "yue2-3b-music",
                    "model": "yue2",
                    "status": "active" if "yue2" in _available_music_models() else "planned",
                },
            ],
            "video": [
                {"id": "minimax-h3" if active_engine == "h3" else "ltx23", "workflow": "text-to-video", "status": "active", "recommended": True},
            ],
            "enhance": [
                {"id": "seedvr2-int8", "workflow": "seedvr2-enhance", "status": "active", "recommended": True},
            ],
        },
        "recommended_model_ids": [
            "qwen-image21-turbo",
            "zonos3-voice-clone",
            "breeze-tts2-voice-clone",
            "yue2-3b-music",
            "cosyvoice3-tts",
            "ace-step-turbo",
        ],
        "deprecated_redirects": {
            "h3-image-*": "qwen-image21 (turbo 8-step)",
            "flux3-image-*": "qwen-image21 (turbo 8-step)",
            "qwen3-tts-*": "zonos3-voice-clone (or voice-clone when tts_engine=indextts25)",
            "ace-step-sft": "music-generate (model=turbo / ace-step-turbo)",
        },
        "workflows": {
            slug: {
                "description": spec.description,
                "required_asset_fields": spec.required_assets,
                "options": spec.options or {},
            }
            for slug, spec in workflows.items()
        },
    }


@app.post(f"{API_PREFIX}/files", status_code=201, tags=["assets"])
def upload_file(kind: str, file: UploadFile = File(...)) -> dict[str, Any]:
    if kind not in {"image", "audio", "video"}:
        _fail(422, "kind must be image, audio or video")
    source_name = Path(file.filename or "upload").name
    extension = Path(source_name).suffix.lower()
    allowed = IMAGE_EXTENSIONS if kind == "image" else AUDIO_EXTENSIONS if kind == "audio" else VIDEO_EXTENSIONS
    supported = is_supported_video_filename(source_name) if kind == "video" else extension in allowed
    if not supported:
        _fail(422, f"unsupported {kind} extension")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    temporary = UPLOAD_DIR / f"{uuid.uuid4().hex}{extension}"
    size = 0
    digest = hashlib.sha256()
    duration = None
    video_info = None
    upload_limit = VIDEO_UPLOAD_MAX_BYTES if kind == "video" else MAX_UPLOAD_BYTES
    try:
        with temporary.open("wb") as target:
            while chunk := file.file.read(1024 * 1024):
                size += len(chunk)
                if size > upload_limit:
                    _fail(413, f"file exceeds {upload_limit} byte {kind} upload limit")
                digest.update(chunk)
                target.write(chunk)
        if kind == "video":
            try:
                video_info = probe_video(temporary)
                validate_video_info(video_info)
                output_dimensions(video_info["width"], video_info["height"], 1)
            except (OSError, RuntimeError, ValueError) as exc:
                _fail(422, str(exc))
            try:
                required = estimated_video_workspace_bytes(size, 2)
                free = available_disk_bytes({OUTPUT_DIR, COMFY_ROOT / "input"})
            except OSError as exc:
                _fail(503, f"unable to check video workspace disk space: {exc}")
            if free < required:
                _fail(
                    507,
                    f"视频导入至少需要 {required / 1024 ** 3:.1f} GiB 可用空间，"
                    f"当前仅 {free / 1024 ** 3:.1f} GiB。",
                )
        comfy_filename = upload_image(temporary, timeout=600 if kind == "video" else 60)
        duration = audio_duration(temporary) if kind == "audio" else None
        if kind == "audio" and (duration is None or duration <= 0):
            _fail(422, "unable to read a valid audio duration")
    except HTTPException:
        raise
    except (OSError, requests.RequestException) as exc:
        _fail(502, f"unable to ingest file into ComfyUI: {exc}")
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
    record = AssetRecord(
        asset_id=uuid.uuid4().hex,
        kind=kind,
        filename=str(comfy_filename),
        source_filename=source_name,
        size_bytes=size,
        sha256=digest.hexdigest(),
        created_at=time.time(),
    )
    assets = _load_assets()
    assets[record.asset_id] = record
    try:
        _save_assets(assets)
    except OSError as exc:
        _fail(500, f"uploaded file could not be indexed: {exc}")
    response = {
        "asset_id": record.asset_id,
        "kind": record.kind,
        "source_filename": record.source_filename,
        "size_bytes": record.size_bytes,
        "sha256": record.sha256,
        "expires_at": record.created_at + ASSET_TTL_SECONDS,
    }
    if duration is not None:
        response["duration"] = round(duration, 3)
    if video_info is not None:
        response.update({
            "duration": round(video_info["duration"], 3),
            "width": video_info["width"],
            "height": video_info["height"],
            "fps": round(video_info["fps"], 6),
            "has_audio": video_info["has_audio"],
            "max_task_seconds": TASK_MAX_SECONDS,
        })
    return response


@app.post(f"{API_PREFIX}/tasks", status_code=202, tags=["tasks"])
def submit_task(submission: TaskSubmission) -> dict[str, Any]:
    resolved_workflow, resolved_params, routed_from = _resolve_workflow_and_params(
        submission.workflow, submission.params
    )
    auto_enhance = bool(resolved_params.pop("auto_enhance", False))
    spec = _active_workflows().get(resolved_workflow)
    if spec is None:
        _fail(422, "unknown workflow; use GET /api/v1/capabilities")
    if auto_enhance and resolved_workflow not in {"zonos3-voice-clone", "breeze-tts2-voice-clone", "voice-clone", "qwen3-voice-clone"}:
        if isinstance(resolved_params.get("prompt"), str) and resolved_params["prompt"].strip():
            resolved_params["prompt"] = llm_runtime.enhance_prompt(
                resolved_params["prompt"], workflow=resolved_workflow
            )
        elif resolved_workflow == "music-generate" and isinstance(resolved_params.get("tags"), str) and resolved_params["tags"].strip():
            resolved_params["tags"] = llm_runtime.enhance_prompt(
                resolved_params["tags"], workflow=resolved_workflow, max_length=2000
            )
    if resolved_workflow in {"h3-image-text-to-image", "h3-image-edit"}:
        from h3_image_support import check_available
        try:
            check_available()
        except requests.RequestException:
            _fail(503, "MiniMax H3 Image runtime is unavailable; retry after recovery")
    if resolved_workflow in {"qwen-image21", "qwen-image21-turbo", "qwen-image21-edit"}:
        from qwen_image21_support import BASE as qwen_image21_base
        try:
            response = _http_session().get(qwen_image21_base + "/system_stats", timeout=5)
            response.raise_for_status()
        except requests.RequestException:
            _fail(503, "Qwen Image 2.1 research runtime is unavailable; retry after recovery")
    if resolved_workflow == "seedvr2-enhance" and not models_installed(COMFY_ROOT):
        _fail(503, "SeedVR2 model files are not installed yet")
    assets = _load_assets()
    arguments = spec.normalizer(resolved_params, assets)
    if resolved_workflow == "seedvr2-enhance":
        selected_model = arguments[-1]
        if not model_installed(COMFY_ROOT, selected_model):
            _fail(503, f"SeedVR2 {selected_model.upper()} 权重或共享 VAE 尚未安装")
    if spec.endpoint == "direct_zonos3_voice_clone":
        return _submit_zonos3_task(resolved_workflow, arguments[0], routed_from=routed_from)
    try:
        accepted = _gradio_call(spec.endpoint, arguments)
    except RuntimeError as exc:
        _fail(503, str(exc))
    if not isinstance(accepted, dict) or not isinstance(accepted.get("task_id"), str):
        _fail(502, "workspace did not return a task id")
    task_id = accepted["task_id"]
    response: dict[str, Any] = {
        "task_id": task_id,
        "workflow": resolved_workflow,
        "state": "accepted",
        "queue_position": accepted.get("queue_position"),
        "links": {"status": f"{API_PREFIX}/tasks/{task_id}"},
    }
    if routed_from:
        response["routed_from"] = routed_from
    return response


@app.post(f"{API_PREFIX}/tasks/{{task_id}}/cancel", tags=["tasks"])
def cancel_task(task_id: str) -> dict[str, Any]:
    if not re.fullmatch(r"[a-f0-9]{32}", task_id or ""):
        _fail(404, "task not found")
    local_result = _local_cancel_task(task_id)
    if local_result is not None:
        return local_result
    result = _internal_api_call("POST", f"/internal/v1/tasks/{quote(task_id, safe='')}/cancel")
    if result is None:
        try:
            result = _gradio_call("cancel_task", [task_id])
        except RuntimeError as exc:
            _fail(503, str(exc))
    if not isinstance(result, dict):
        _fail(503, "invalid task cancellation response")
    if result.get("error") == "not_found":
        _fail(404, "task not found")
    return result


@app.get(f"{API_PREFIX}/tasks/{{task_id}}", tags=["tasks"])
def get_task(task_id: str) -> dict[str, Any]:
    if not re.fullmatch(r"[a-f0-9]{32}", task_id or ""):
        _fail(404, "task not found")
    try:
        status = _task_status(task_id)
    except RuntimeError as exc:
        _fail(503, str(exc))
    if status.get("state") == "not_found":
        _fail(404, "task not found")
    output_files = status.pop("output_files", [])
    status["artifacts"] = [
        {"name": name, "download_url": _artifact_url(task_id, name)}
        for name in output_files
        if isinstance(name, str) and Path(name).name == name
    ]
    return status


def _should_use_x_accel(request: Request | None) -> tuple[bool, str]:
    prefix = os.environ.get("BRM_LAN_API_X_ACCEL_PREFIX", "/internal-outputs").strip()
    if not prefix:
        return False, ""
    toggle = os.environ.get("BRM_LAN_API_X_ACCEL_ENABLED", "").strip().lower()
    if toggle in {"0", "false", "no", "off"}:
        return False, ""
    if toggle in {"1", "true", "yes", "on"}:
        return True, prefix
    if request is not None:
        headers = {k.lower(): v for k, v in getattr(request, "headers", {}).items()}
        if headers.get("x-forwarded-for") or headers.get("x-real-ip") or headers.get("x-accel-internal"):
            return True, prefix
    return False, ""


@app.get(f"{API_PREFIX}/tasks/{{task_id}}/artifacts/{{filename}}", tags=["artifacts"], response_model=None)
def download_artifact(task_id: str, filename: str, request: Request = None):
    path = _safe_artifact(task_id, filename)
    media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    use_accel, prefix = _should_use_x_accel(request)
    if use_accel and _FastAPIResponse is not None:
        try:
            rel = path.relative_to(OUTPUT_DIR.resolve())
            accel_uri = f"{prefix.rstrip('/')}/{quote(rel.as_posix())}"
            return _FastAPIResponse(
                status_code=200,
                headers={
                    "X-Accel-Redirect": accel_uri,
                    "Content-Type": media_type,
                    "Content-Disposition": f"attachment; filename*=UTF-8''{quote(path.name)}",
                },
            )
        except ValueError:
            pass
    return FileResponse(path, media_type=media_type, filename=path.name)


@app.delete(f"{API_PREFIX}/tasks/{{task_id}}/artifacts/{{filename}}", tags=["artifacts"])
def delete_artifact(task_id: str, filename: str, confirmation: dict[str, Any]) -> dict[str, Any]:
    if not re.fullmatch(r"[a-f0-9]{32}", task_id or "") or not filename or filename in (".", "..") or any(c in filename for c in ("/", "\\", "\x00")):
        _fail(404, "artifact not found")
    if confirmation.get("confirmed") is not True:
        _fail(400, "explicit deletion confirmation required")
    local_result = _local_delete_artifact(task_id, filename)
    if local_result is not None:
        error = local_result.get("error")
        if error:
            _fail({"not_found": 404, "asset_busy": 409, "confirmation_required": 400}.get(error, 500), str(error))
        return local_result
    result = _internal_api_call(
        "DELETE",
        f"/internal/v1/tasks/{quote(task_id, safe='')}/artifacts/{quote(filename, safe='')}?confirmed=true",
    )
    if result is None:
        try:
            result = _gradio_call("delete_asset", [task_id, filename, True])
        except RuntimeError:
            _fail(503, "asset deletion service unavailable")
    if not isinstance(result, dict):
        _fail(503, "invalid asset deletion response")
    error = result.get("error")
    if error:
        _fail({"not_found":404, "asset_busy":409, "confirmation_required":400}.get(error,500), str(error))
    return result
