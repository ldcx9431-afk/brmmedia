#!/usr/bin/env python3
"""BRMMedia LAN automation API.

This service is deliberately separate from the Gradio UI process.  It exposes
an ordinary REST contract for LAN applications while the existing Gradio API
remains a backwards-compatible UI integration surface.  The process binds to
loopback only; Nginx provides the authenticated ``/api/`` LAN entry point.
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

import requests
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from comfyui_server import BASE as COMFY_BASE, audio_duration, upload_image


API_PREFIX = "/api/v1"
BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = Path(os.environ.get("BRM_OUTPUT_DIR", BASE_DIR / "outputs")).expanduser().resolve()
ASSET_INDEX_PATH = OUTPUT_DIR / "lan-api-assets.json"
UPLOAD_DIR = OUTPUT_DIR / ".lan-api-uploads"
GRADIO_BASE = os.environ.get("BRM_GRADIO_API_BASE", "http://127.0.0.1:9000").rstrip("/")
MAX_UPLOAD_BYTES = max(1, int(os.environ.get("BRM_LAN_API_MAX_UPLOAD_BYTES", str(256 * 1024 * 1024))))
GRADIO_TIMEOUT_SECONDS = max(10, int(os.environ.get("BRM_LAN_API_GRADIO_TIMEOUT", "60")))
ASSET_TTL_SECONDS = max(3600, int(os.environ.get("BRM_LAN_API_ASSET_TTL", str(7 * 24 * 3600))))

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}
AUDIO_EXTENSIONS = {".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg"}
MEDIA_EXTENSIONS = IMAGE_EXTENSIONS | AUDIO_EXTENSIONS | {".mp4", ".webm", ".mov", ".mkv", ".avi"}
SIZE_VALUES = [
    "512 × 512", "768 × 768", "1024 × 1024", "2048 × 2048",
    "1024 × 768", "768 × 1024", "1344 × 768", "768 × 1344",
    "1920 × 1080", "1080 × 1920", "2048 × 1536", "1536 × 2048",
    "2560 × 1440", "1440 × 2560", "3840 × 2160", "2160 × 3840",
]
LANGUAGE_VALUES = ["zh", "en", "ja", "ko", "fr", "de", "es", "ru", "unknown"]
MUSIC_MODEL_VALUES = ["turbo", "base", "sft"]


@dataclass(frozen=True)
class WorkflowSpec:
    endpoint: str
    description: str
    required_assets: dict[str, str]
    normalizer: Callable[[dict[str, Any], dict[str, "AssetRecord"]], list[Any]]


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
    version="1.0.0",
    description="Authenticated LAN automation API for BRMMedia generation workflows.",
    openapi_url=f"{API_PREFIX}/openapi.json",
    docs_url=f"{API_PREFIX}/docs",
    redoc_url=None,
)


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
        parsed = int(value) if integer else float(value)
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


def _load_assets() -> dict[str, AssetRecord]:
    try:
        payload = json.loads(ASSET_INDEX_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError):
        return {}
    now = time.time()
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
    return records


def _save_assets(records: dict[str, AssetRecord]) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    payload = {"version": 1, "assets": [record.__dict__ for record in records.values()]}
    temporary = ASSET_INDEX_PATH.with_name(f".{ASSET_INDEX_PATH.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(ASSET_INDEX_PATH)


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


def _normal_image_edit(params: dict[str, Any], assets: dict[str, AssetRecord]) -> list[Any]:
    image = _asset(params, assets, "image_asset_id", "image")
    return [_trim_text(params.get("prompt"), "prompt"), image.filename]


def _normal_text_to_video(params: dict[str, Any], assets: dict[str, AssetRecord]) -> list[Any]:
    return [
        _trim_text(params.get("prompt"), "prompt"),
        _choice(params.get("size"), "size", SIZE_VALUES, "768 × 1024"),
        _number(params.get("seconds", 5), "seconds", minimum=2, maximum=360, integer=True),
    ]


def _normal_image_to_video(params: dict[str, Any], assets: dict[str, AssetRecord]) -> list[Any]:
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


def _normal_voice_clone(params: dict[str, Any], assets: dict[str, AssetRecord]) -> list[Any]:
    audio = _asset(params, assets, "ref_audio_asset_id", "audio")
    return [
        _trim_text(params.get("prompt"), "prompt"), audio.filename,
        _number(params.get("temperature", 0.8), "temperature", minimum=0, maximum=1.5),
    ]


def _normal_music(params: dict[str, Any], assets: dict[str, AssetRecord]) -> list[Any]:
    lyrics = params.get("lyrics", "")
    if lyrics is None:
        lyrics = ""
    return [
        _trim_text(params.get("tags"), "tags", maximum=2000),
        _trim_text(lyrics, "lyrics", maximum=12000, required=False),
        _number(params.get("duration", 30), "duration", minimum=1, maximum=600),
        _number(params.get("bpm", 120), "bpm", minimum=30, maximum=300, integer=True),
        _choice(params.get("language"), "language", LANGUAGE_VALUES, "zh"),
        _choice(params.get("model"), "model", MUSIC_MODEL_VALUES, "turbo"),
    ]


WORKFLOWS: dict[str, WorkflowSpec] = {
    "text-to-image": WorkflowSpec("submit_workflow_1", "Z-Image 文生图", {}, _normal_text_to_image),
    "image-edit": WorkflowSpec("submit_workflow_2", "FLUX.2-klein 图片编辑", {"image_asset_id": "image"}, _normal_image_edit),
    "text-to-video": WorkflowSpec("submit_workflow_3", "LTX2.3 文生视频", {}, _normal_text_to_video),
    "image-to-video": WorkflowSpec("submit_workflow_4", "LTX2.3 图生视频", {"image_asset_id": "image"}, _normal_image_to_video),
    "first-last-frame-video": WorkflowSpec("submit_workflow_5", "LTX2.3 首尾帧视频", {"first_image_asset_id": "image", "last_image_asset_id": "image"}, _normal_first_last_frame),
    "talking-head": WorkflowSpec("submit_workflow_6", "LTX2.3 单图数字人-语音驱动", {"image_asset_id": "image", "audio_asset_id": "audio"}, _normal_talking_head),
    "voice-clone": WorkflowSpec("submit_workflow_7", "IndexTTS2 语音克隆", {"ref_audio_asset_id": "audio"}, _normal_voice_clone),
    "music-generate": WorkflowSpec("submit_workflow_8", "ACE-Step 1.5 音乐生成", {}, _normal_music),
}


def _gradio_call(endpoint: str, arguments: list[Any]) -> Any:
    """Call the loopback-only Gradio API and wait for its SSE completion."""
    try:
        info = requests.get(f"{GRADIO_BASE}/gradio_api/info", timeout=10).json()
        endpoint_info = info["named_endpoints"][f"/{endpoint}"]
        parameters = endpoint_info["parameters"]
        names = [parameter["parameter_name"] for parameter in parameters]
    except (KeyError, TypeError, ValueError, requests.RequestException) as exc:
        raise RuntimeError(f"Gradio API metadata is unavailable: {exc}") from exc
    if len(names) != len(arguments):
        raise RuntimeError(f"Gradio endpoint {endpoint} changed its parameter contract")
    try:
        accepted = requests.post(
            f"{GRADIO_BASE}/gradio_api/call/v2/{endpoint}",
            json=dict(zip(names, arguments)), timeout=15,
        )
        accepted.raise_for_status()
        event_id = accepted.json().get("event_id")
        if not event_id:
            raise RuntimeError("Gradio did not return an event id")
        with requests.get(
            f"{GRADIO_BASE}/gradio_api/call/v2/{endpoint}/{event_id}",
            headers={"Accept": "text/event-stream"}, stream=True, timeout=GRADIO_TIMEOUT_SECONDS,
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
                        return json.loads(data)
                    if event == "error":
                        raise RuntimeError(f"Gradio handler error: {data}")
    except (ValueError, requests.RequestException) as exc:
        raise RuntimeError(f"Gradio API request failed: {exc}") from exc
    raise RuntimeError("Gradio SSE stream ended before completion")


def _task_status(task_id: str) -> dict[str, Any]:
    payload = _gradio_call("task_status", [task_id])
    if not isinstance(payload, dict):
        raise RuntimeError("Gradio task_status returned an invalid payload")
    return payload


def _artifact_url(task_id: str, filename: str) -> str:
    return f"{API_PREFIX}/tasks/{task_id}/artifacts/{quote(filename)}"


def _safe_artifact(task_id: str, filename: str) -> Path:
    if not re.fullmatch(r"[a-zA-Z0-9_\-().\u4e00-\u9fff]+\.[a-zA-Z0-9]+", filename or ""):
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
    return JSONResponse(
        status_code=200 if ready else 503,
        content={"status": "ok" if ready else "degraded", "dependencies": dependencies},
    )


@app.get(f"{API_PREFIX}/capabilities", tags=["system"])
def capabilities() -> dict[str, Any]:
    return {
        "api_version": "v1",
        "max_upload_bytes": MAX_UPLOAD_BYTES,
        "asset_ttl_seconds": ASSET_TTL_SECONDS,
        "size_values": SIZE_VALUES,
        "music_languages": LANGUAGE_VALUES,
        "music_models": MUSIC_MODEL_VALUES,
        "workflows": {
            slug: {
                "description": spec.description,
                "required_asset_fields": spec.required_assets,
            }
            for slug, spec in WORKFLOWS.items()
        },
    }


@app.post(f"{API_PREFIX}/files", status_code=201, tags=["assets"])
def upload_file(kind: str, file: UploadFile = File(...)) -> dict[str, Any]:
    if kind not in {"image", "audio"}:
        _fail(422, "kind must be image or audio")
    source_name = Path(file.filename or "upload").name
    extension = Path(source_name).suffix.lower()
    allowed = IMAGE_EXTENSIONS if kind == "image" else AUDIO_EXTENSIONS
    if extension not in allowed:
        _fail(422, f"unsupported {kind} extension")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    temporary = UPLOAD_DIR / f"{uuid.uuid4().hex}{extension}"
    size = 0
    digest = hashlib.sha256()
    try:
        with temporary.open("wb") as target:
            while chunk := file.file.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    _fail(413, f"file exceeds {MAX_UPLOAD_BYTES} byte upload limit")
                digest.update(chunk)
                target.write(chunk)
        comfy_filename = upload_image(temporary)
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
    return response


@app.post(f"{API_PREFIX}/tasks", status_code=202, tags=["tasks"])
def submit_task(submission: TaskSubmission) -> dict[str, Any]:
    spec = WORKFLOWS.get(submission.workflow)
    if spec is None:
        _fail(422, "unknown workflow; use GET /api/v1/capabilities")
    assets = _load_assets()
    arguments = spec.normalizer(submission.params, assets)
    try:
        accepted = _gradio_call(spec.endpoint, arguments)
    except RuntimeError as exc:
        _fail(503, str(exc))
    if not isinstance(accepted, dict) or not isinstance(accepted.get("task_id"), str):
        _fail(502, "workspace did not return a task id")
    task_id = accepted["task_id"]
    return {
        "task_id": task_id,
        "workflow": submission.workflow,
        "state": "accepted",
        "queue_position": accepted.get("queue_position"),
        "links": {"status": f"{API_PREFIX}/tasks/{task_id}"},
    }


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


@app.get(f"{API_PREFIX}/tasks/{{task_id}}/artifacts/{{filename}}", tags=["artifacts"])
def download_artifact(task_id: str, filename: str) -> FileResponse:
    path = _safe_artifact(task_id, filename)
    return FileResponse(path, media_type=mimetypes.guess_type(path.name)[0] or "application/octet-stream", filename=path.name)
