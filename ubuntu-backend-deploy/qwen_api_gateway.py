#!/usr/bin/env python3
"""Bearer API-key gateway for the local OpenAI-compatible Qwen service.

The existing ``/qwen/v1`` Nginx entry remains protected by LAN Basic Auth.
This process adds a separately scoped ``/qwen-api/v1`` entry for application
integrations.  Plaintext keys are returned only when created or rotated; the
SQLite store contains SHA-256 digests and lifecycle metadata only.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import requests
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field


BASE_DIR = Path(__file__).resolve().parent
KEY_DB_PATH = Path(
    os.environ.get("BRM_QWEN_API_KEY_DB", "/var/lib/brmmedia/qwen-api-keys.sqlite3")
).expanduser()
ACTIVE_QWEN_ENV = Path(os.environ.get("BRM_QWEN_ACTIVE_ENV", "/etc/brmmedia/qwen-active.env"))
QWEN_NGINX_SNIPPET = Path(
    os.environ.get("BRM_QWEN_NGINX_SNIPPET", "/etc/nginx/snippets/brmmedia-qwen-upstream.conf")
)
CONNECT_TIMEOUT_SECONDS = max(1, int(os.environ.get("BRM_QWEN_CONNECT_TIMEOUT", "10")))
READ_TIMEOUT_SECONDS = max(10, int(os.environ.get("BRM_QWEN_READ_TIMEOUT", "3700")))
_DB_LOCK = threading.Lock()

app = FastAPI(
    title="BRMMedia Qwen API Key Gateway",
    version="1.0.0",
    docs_url=None,
    redoc_url=None,
)


class CreateKeyRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    expires_in_days: int | None = Field(default=None, ge=1, le=3650)


class KeyRecord(BaseModel):
    id: str
    name: str
    prefix: str
    status: str
    created_at: int
    expires_at: int | None
    last_used_at: int | None
    use_count: int
    disabled_at: int | None
    revoked_at: int | None


class CreatedKey(KeyRecord):
    api_key: str


def _timestamp() -> int:
    return int(time.time())


def _key_digest(api_key: str) -> str:
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


def _new_api_key() -> str:
    # 32 random bytes gives 256 bits of entropy. The brm_ prefix aids key
    # identification without being used as a secret.
    return "brm_" + secrets.token_urlsafe(32)


def _record_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "prefix": row["prefix"],
        "status": row["status"],
        "created_at": row["created_at"],
        "expires_at": row["expires_at"],
        "last_used_at": row["last_used_at"],
        "use_count": row["use_count"],
        "disabled_at": row["disabled_at"],
        "revoked_at": row["revoked_at"],
    }


def _ensure_database() -> None:
    KEY_DB_PATH.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    connection = sqlite3.connect(KEY_DB_PATH)
    try:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS api_keys (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                key_hash TEXT NOT NULL UNIQUE,
                prefix TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('active', 'disabled', 'revoked')),
                created_at INTEGER NOT NULL,
                expires_at INTEGER,
                last_used_at INTEGER,
                use_count INTEGER NOT NULL DEFAULT 0,
                disabled_at INTEGER,
                revoked_at INTEGER
            )
            """
        )
        connection.execute("CREATE INDEX IF NOT EXISTS idx_api_keys_hash ON api_keys(key_hash)")
        connection.commit()
    finally:
        connection.close()
    try:
        KEY_DB_PATH.chmod(0o600)
    except OSError:
        pass


@contextmanager
def _database() -> Iterator[sqlite3.Connection]:
    with _DB_LOCK:
        _ensure_database()
        connection = sqlite3.connect(KEY_DB_PATH)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()


def _create_key(name: str, expires_in_days: int | None) -> tuple[dict[str, Any], str]:
    name = name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="API key name must contain non-whitespace characters")
    api_key = _new_api_key()
    now = _timestamp()
    expires_at = now + expires_in_days * 86400 if expires_in_days else None
    record_id = str(uuid.uuid4())
    with _database() as connection:
        connection.execute(
            """INSERT INTO api_keys
               (id, name, key_hash, prefix, status, created_at, expires_at, use_count)
               VALUES (?, ?, ?, ?, 'active', ?, ?, 0)""",
            (record_id, name, _key_digest(api_key), api_key[:12], now, expires_at),
        )
        row = connection.execute("SELECT * FROM api_keys WHERE id = ?", (record_id,)).fetchone()
    return _record_from_row(row), api_key


def _require_key(key_id: str) -> sqlite3.Row:
    with _database() as connection:
        row = connection.execute("SELECT * FROM api_keys WHERE id = ?", (key_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="API key not found")
    return row


def _authorization_key(request: Request) -> str:
    authorization = request.headers.get("authorization", "")
    scheme, _, value = authorization.partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        raise HTTPException(status_code=401, detail="Bearer API key is required")
    return value.strip()


def _validate_and_record_usage(api_key: str) -> None:
    now = _timestamp()
    with _database() as connection:
        row = connection.execute(
            "SELECT * FROM api_keys WHERE key_hash = ?", (_key_digest(api_key),)
        ).fetchone()
        if row is None or row["status"] != "active":
            raise HTTPException(status_code=401, detail="API key is invalid, disabled, or revoked")
        if row["expires_at"] is not None and row["expires_at"] <= now:
            raise HTTPException(status_code=401, detail="API key has expired")
        connection.execute(
            "UPDATE api_keys SET last_used_at = ?, use_count = use_count + 1 WHERE id = ?",
            (now, row["id"]),
        )


def _read_env_value(path: Path, key: str) -> str | None:
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith(key + "="):
                return line.partition("=")[2].strip().strip('"')
    except OSError:
        return None
    return None


def _active_qwen_base() -> str:
    explicit = os.environ.get("BRM_QWEN_API_BASE", "").strip()
    if explicit:
        return explicit.rstrip("/")

    active_upstream = _read_env_value(ACTIVE_QWEN_ENV, "QWEN_ACTIVE_UPSTREAM")
    if not active_upstream:
        try:
            match = re.search(
                r"(?m)^\s*proxy_pass\s+(https?://[^;]+);", QWEN_NGINX_SNIPPET.read_text(encoding="utf-8")
            )
            active_upstream = match.group(1) if match else None
        except OSError:
            active_upstream = None
    base = (active_upstream or "http://127.0.0.1:8000").rstrip("/")
    return base if base.endswith("/v1") else base + "/v1"


def _error_response(status_code: int, message: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"error": {"message": message, "type": "gateway_error"}})


def _forward_headers(response: requests.Response) -> dict[str, str]:
    allowed = {"content-type", "cache-control", "content-disposition", "x-request-id"}
    return {key: value for key, value in response.headers.items() if key.lower() in allowed}


def _prepare_chat_payload(body: bytes) -> bytes:
    """Make OpenAI-compatible chat checks return answer text by default.

    Qwen reasoning models can spend a small client's entire ``max_tokens``
    budget on ``reasoning_content`` and leave ``content`` empty.  Most
    third-party "test connection" forms do not expose the provider-specific
    switch, so default those requests to answer-only mode while honoring an
    explicit ``chat_template_kwargs.enable_thinking`` (or the common
    top-level spelling).
    """
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return body
    if not isinstance(payload, dict):
        return body
    template_kwargs = payload.get("chat_template_kwargs")
    if not isinstance(template_kwargs, dict):
        template_kwargs = {}
    if "enable_thinking" not in template_kwargs:
        if isinstance(payload.get("enable_thinking"), bool):
            template_kwargs["enable_thinking"] = payload["enable_thinking"]
        else:
            template_kwargs["enable_thinking"] = False
    payload["chat_template_kwargs"] = template_kwargs
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


@app.exception_handler(HTTPException)
async def _http_error(_: Request, exception: HTTPException) -> JSONResponse:
    return _error_response(exception.status_code, str(exception.detail))


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "upstream": _active_qwen_base()}


@app.get("/admin/api-keys", response_model=list[KeyRecord])
def list_api_keys() -> list[dict[str, Any]]:
    with _database() as connection:
        rows = connection.execute("SELECT * FROM api_keys ORDER BY created_at DESC, id DESC").fetchall()
    return [_record_from_row(row) for row in rows]


@app.post("/admin/api-keys", response_model=CreatedKey, status_code=201)
def create_api_key(request: CreateKeyRequest) -> dict[str, Any]:
    record, api_key = _create_key(request.name, request.expires_in_days)
    return {**record, "api_key": api_key}


@app.post("/admin/api-keys/{key_id}/disable", response_model=KeyRecord)
def disable_api_key(key_id: str) -> dict[str, Any]:
    _require_key(key_id)
    with _database() as connection:
        connection.execute(
            "UPDATE api_keys SET status = 'disabled', disabled_at = ? WHERE id = ? AND status = 'active'",
            (_timestamp(), key_id),
        )
        row = connection.execute("SELECT * FROM api_keys WHERE id = ?", (key_id,)).fetchone()
    return _record_from_row(row)


@app.post("/admin/api-keys/{key_id}/enable", response_model=KeyRecord)
def enable_api_key(key_id: str) -> dict[str, Any]:
    row = _require_key(key_id)
    if row["status"] == "revoked":
        raise HTTPException(status_code=409, detail="Revoked API keys cannot be enabled; rotate or create a new key")
    with _database() as connection:
        connection.execute(
            "UPDATE api_keys SET status = 'active', disabled_at = NULL WHERE id = ?", (key_id,)
        )
        row = connection.execute("SELECT * FROM api_keys WHERE id = ?", (key_id,)).fetchone()
    return _record_from_row(row)


@app.post("/admin/api-keys/{key_id}/revoke", response_model=KeyRecord)
def revoke_api_key(key_id: str) -> dict[str, Any]:
    _require_key(key_id)
    with _database() as connection:
        connection.execute(
            "UPDATE api_keys SET status = 'revoked', revoked_at = ? WHERE id = ?",
            (_timestamp(), key_id),
        )
        row = connection.execute("SELECT * FROM api_keys WHERE id = ?", (key_id,)).fetchone()
    return _record_from_row(row)


@app.delete("/admin/api-keys/{key_id}", response_model=KeyRecord)
def delete_api_key(key_id: str) -> dict[str, Any]:
    """Permanently remove a key that is already unable to authenticate."""
    with _database() as connection:
        row = connection.execute("SELECT * FROM api_keys WHERE id = ?", (key_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="API key not found")
        if row["status"] not in {"disabled", "revoked"}:
            raise HTTPException(
                status_code=409,
                detail="Disable or revoke an API key before permanently deleting it",
            )
        connection.execute("DELETE FROM api_keys WHERE id = ?", (key_id,))
    return _record_from_row(row)


@app.post("/admin/api-keys/{key_id}/rotate", response_model=CreatedKey, status_code=201)
def rotate_api_key(key_id: str) -> dict[str, Any]:
    previous = _require_key(key_id)
    if previous["status"] == "revoked":
        raise HTTPException(status_code=409, detail="Revoked API keys cannot be rotated")
    with _database() as connection:
        connection.execute(
            "UPDATE api_keys SET status = 'revoked', revoked_at = ? WHERE id = ?",
            (_timestamp(), key_id),
        )
    remaining_days = None
    if previous["expires_at"] is not None:
        remaining_days = max(1, (previous["expires_at"] - _timestamp() + 86399) // 86400)
    record, api_key = _create_key(previous["name"], remaining_days)
    return {**record, "api_key": api_key}


@app.api_route("/v1/{upstream_path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
async def qwen_proxy(upstream_path: str, request: Request):
    _validate_and_record_usage(_authorization_key(request))
    target = _active_qwen_base().rstrip("/")
    if upstream_path:
        target += "/" + upstream_path
    if request.url.query:
        target += "?" + request.url.query

    headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower() in {"accept", "content-type", "user-agent"}
    }
    body = await request.body()
    if upstream_path.rstrip("/") == "chat/completions":
        body = _prepare_chat_payload(body)
    try:
        upstream = requests.request(
            request.method,
            target,
            data=body,
            headers=headers,
            stream=True,
            timeout=(CONNECT_TIMEOUT_SECONDS, READ_TIMEOUT_SECONDS),
        )
    except requests.RequestException as error:
        return _error_response(503, f"Qwen upstream is unavailable: {error.__class__.__name__}")

    def stream_response() -> Iterator[bytes]:
        try:
            yield from upstream.iter_content(chunk_size=8192)
        finally:
            upstream.close()

    return StreamingResponse(
        stream_response(),
        status_code=upstream.status_code,
        headers=_forward_headers(upstream),
        media_type=upstream.headers.get("content-type"),
    )
