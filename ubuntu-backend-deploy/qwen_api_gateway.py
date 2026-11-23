#!/usr/bin/env python3
"""Bearer API-key gateway for the local OpenAI-compatible Qwen service.

The existing ``/qwen/v1`` Nginx entry remains protected by LAN Basic Auth.
This process adds a separately scoped ``/qwen-api/v1`` entry for application
integrations. Authentication uses SHA-256 digests. New keys additionally have
an encrypted administrator-retrievable copy; legacy hashes are not reversible.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import secrets
import sqlite3
import subprocess
import threading
import time
import uuid
import weakref
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import unquote

import anyio
import httpx
import llm_runtime
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
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
KEY_CACHE_TTL_SECONDS = max(0.0, float(os.environ.get("BRM_QWEN_KEY_CACHE_TTL", "8.0")))
_DB_LOCK = threading.Lock()
_VAULT_LOCK = threading.Lock()
_SWITCH_LOCK = threading.Lock()
_SWITCH_ERROR = ""
_INITIALIZED_DBS: set[str] = set()
_KEY_CACHE_LOCK = threading.Lock()
_KEY_CACHE: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}
_USAGE_BUFFER: dict[tuple[str, str], tuple[int, int]] = {}
_USAGE_FLUSH_AT: dict[str, float] = {}
_ORIGINAL_ASYNC_CLIENT = httpx.AsyncClient
_ASYNC_CLIENT_LOCK = threading.Lock()
_ASYNC_CLIENT_MAP: dict[int, httpx.AsyncClient] = {}
_ASYNC_CLIENT_LOOPS: dict[int, weakref.ref[asyncio.AbstractEventLoop]] = {}


class SwitchModelRequest(BaseModel):
    target: str


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
    can_reveal: bool = False


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
        "can_reveal": bool(row["key_ciphertext"]),
    }


def _ensure_database() -> None:
    KEY_DB_PATH.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    connection = sqlite3.connect(KEY_DB_PATH)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
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
        columns = {row[1] for row in connection.execute("PRAGMA table_info(api_keys)")}
        if "key_ciphertext" not in columns:
            connection.execute("ALTER TABLE api_keys ADD COLUMN key_ciphertext TEXT")
        connection.commit()
    finally:
        connection.close()
    try:
        KEY_DB_PATH.chmod(0o600)
    except OSError:
        pass
    _INITIALIZED_DBS.add(str(KEY_DB_PATH))


@contextmanager
def _database() -> Iterator[sqlite3.Connection]:
    with _DB_LOCK:
        db_key = str(KEY_DB_PATH)
        if db_key not in _INITIALIZED_DBS or not KEY_DB_PATH.exists():
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


def _create_key(name: str, expires_in_days: int | None, *, revoke_id: str | None = None) -> tuple[dict[str, Any], str]:
    name = name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="API key name must contain non-whitespace characters")
    api_key = _new_api_key()
    now = _timestamp()
    expires_at = now + expires_in_days * 86400 if expires_in_days else None
    record_id = str(uuid.uuid4())
    ciphertext = _key_cipher().encrypt(api_key.encode("utf-8")).decode("ascii")
    with _database() as connection:
        if revoke_id:
            previous = connection.execute("SELECT status FROM api_keys WHERE id = ?", (revoke_id,)).fetchone()
            if previous is None or previous["status"] == "revoked":
                raise HTTPException(status_code=409, detail="API key no longer available for rotation")
            connection.execute("UPDATE api_keys SET status = 'revoked', revoked_at = ? WHERE id = ?", (now, revoke_id))
        connection.execute(
            """INSERT INTO api_keys
               (id, name, key_hash, prefix, status, created_at, expires_at, use_count, key_ciphertext)
               VALUES (?, ?, ?, ?, 'active', ?, ?, 0, ?)""",
            (record_id, name, _key_digest(api_key), api_key[:12], now, expires_at, ciphertext),
        )
        row = connection.execute("SELECT * FROM api_keys WHERE id = ?", (record_id,)).fetchone()
    return _record_from_row(row), api_key


def _key_cipher():
    with _VAULT_LOCK:
        return _load_key_cipher()


def _load_key_cipher():
    """Keep the encryption key outside SQLite; never replace a missing vault key."""
    from cryptography.fernet import Fernet
    vault_path = Path(os.environ.get("BRM_QWEN_KEY_VAULT", str(KEY_DB_PATH) + ".fernet"))
    vault_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not vault_path.exists():
        with _database() as connection:
            if connection.execute("SELECT 1 FROM api_keys WHERE key_ciphertext IS NOT NULL LIMIT 1").fetchone():
                raise HTTPException(status_code=503, detail="密钥加密文件缺失，请恢复备份；现有调用不受影响")
        try:
            fd = os.open(vault_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, "wb") as handle:
                handle.write(Fernet.generate_key())
                handle.flush()
                os.fsync(handle.fileno())
    try:
        return Fernet(vault_path.read_bytes())
    except (OSError, ValueError):
        raise HTTPException(status_code=503, detail="密钥加密文件不可用，请检查权限或恢复备份") from None


def _invalidate_key_cache(db_path: Path | None = None) -> None:
    db_key = str(db_path or KEY_DB_PATH)
    with _KEY_CACHE_LOCK:
        for cache_key in [k for k in _KEY_CACHE if k[0] == db_key]:
            _KEY_CACHE.pop(cache_key, None)


def _flush_usage_buffer(db_path: Path | None = None) -> None:
    db_key = str(db_path or KEY_DB_PATH)
    with _KEY_CACHE_LOCK:
        pending = [
            (key_id, count, last_used)
            for (k_db, key_id), (count, last_used) in list(_USAGE_BUFFER.items())
            if k_db == db_key and _USAGE_BUFFER.pop((k_db, key_id), None)
        ]
        _USAGE_FLUSH_AT[db_key] = time.monotonic()
    if not pending:
        return
    with _database() as connection:
        for key_id, count, last_used in pending:
            connection.execute(
                "UPDATE api_keys SET last_used_at = MAX(COALESCE(last_used_at, 0), ?), use_count = use_count + ? WHERE id = ?",
                (last_used, count, key_id),
            )


def _require_key(key_id: str) -> sqlite3.Row:
    _flush_usage_buffer()
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
    now_mono = time.monotonic()
    db_key = str(KEY_DB_PATH)
    digest = _key_digest(api_key)
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
                try:
                    _flush_usage_buffer()
                except sqlite3.Error:
                    pass
            return

    with _database() as connection:
        row = connection.execute(
            "SELECT * FROM api_keys WHERE key_hash = ?", (digest,)
        ).fetchone()
        if row is None or row["status"] != "active":
            raise HTTPException(status_code=401, detail="API key is invalid, disabled, or revoked")
        if row["expires_at"] is not None and row["expires_at"] <= now:
            raise HTTPException(status_code=401, detail="API key has expired")
        with _KEY_CACHE_LOCK:
            buffered_count, _ = _USAGE_BUFFER.pop((db_key, row["id"]), (0, 0))
            _USAGE_FLUSH_AT[db_key] = now_mono
        connection.execute(
            "UPDATE api_keys SET last_used_at = ?, use_count = use_count + ? WHERE id = ?",
            (now, 1 + buffered_count, row["id"]),
        )
        if KEY_CACHE_TTL_SECONDS > 0:
            with _KEY_CACHE_LOCK:
                _KEY_CACHE[cache_key] = (
                    now_mono + KEY_CACHE_TTL_SECONDS,
                    {"id": row["id"], "expires_at": row["expires_at"]},
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
    # Once the switching controller is installed, its atomic selection wins
    # over the original static process environment.
    if llm_runtime.TRAFFIC_LOCK.exists():
        return llm_runtime.selection()[0]
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


def _forward_headers(response: httpx.Response) -> dict[str, str]:
    allowed = {"content-type", "cache-control", "content-disposition", "x-request-id"}
    return {key: value for key, value in response.headers.items() if key.lower() in allowed}


def _acquire_async_client() -> tuple[httpx.AsyncClient, bool]:
    """Return (client, owns_client). Reuses a pooled AsyncClient in production."""
    timeout = httpx.Timeout(READ_TIMEOUT_SECONDS, connect=CONNECT_TIMEOUT_SECONDS)
    if httpx.AsyncClient is not _ORIGINAL_ASYNC_CLIENT:
        return httpx.AsyncClient(timeout=timeout, trust_env=False), True
    loop = asyncio.get_running_loop()
    loop_key = id(loop)
    with _ASYNC_CLIENT_LOCK:
        stale_keys = [
            key for key, loop_ref in list(_ASYNC_CLIENT_LOOPS.items())
            if (ref_loop := loop_ref()) is None or ref_loop.is_closed()
        ]
        for key in stale_keys:
            _ASYNC_CLIENT_LOOPS.pop(key, None)
            _ASYNC_CLIENT_MAP.pop(key, None)
        existing_ref = _ASYNC_CLIENT_LOOPS.get(loop_key)
        if existing_ref is not None and existing_ref() is not loop:
            _ASYNC_CLIENT_LOOPS.pop(loop_key, None)
            _ASYNC_CLIENT_MAP.pop(loop_key, None)
        client = _ASYNC_CLIENT_MAP.get(loop_key)
        if client is None or getattr(client, "is_closed", False):
            limits = httpx.Limits(max_connections=64, max_keepalive_connections=16, keepalive_expiry=30.0)
            client = httpx.AsyncClient(timeout=timeout, limits=limits, trust_env=False)
            _ASYNC_CLIENT_MAP[loop_key] = client
            _ASYNC_CLIENT_LOOPS[loop_key] = weakref.ref(loop)
        return client, False


async def _close_async_clients() -> None:
    with _ASYNC_CLIENT_LOCK:
        clients = list(_ASYNC_CLIENT_MAP.values())
        _ASYNC_CLIENT_MAP.clear()
        _ASYNC_CLIENT_LOOPS.clear()
    for client in clients:
        try:
            await client.aclose()
        except Exception:
            pass


class _UpstreamResponse(Response):
    """Own the entire ASGI lifetime, including disconnect before headers.

    A synchronous iterator's finally is not guaranteed to run when Starlette
    abandons it. Race async upstream I/O against ASGI disconnect instead, and
    close resources under a shield before releasing the inference interlock.
    There is only one consumer of receive; no version-specific StreamingResponse
    disconnect behavior is needed.
    """

    def __init__(self, method, target, body, headers, guard):
        super().__init__(content=b"")
        self.method, self.target, self.body = method, target, body
        self.upstream_headers, self.guard = headers, guard

    async def __call__(self, scope, receive, send):
        client = upstream = None
        owns_client = False
        started = False
        try:
            async with anyio.create_task_group() as tasks:
                async def disconnected():
                    while True:
                        message = await receive()
                        if message["type"] == "http.disconnect":
                            tasks.cancel_scope.cancel()
                            return

                tasks.start_soon(disconnected)
                try:
                    client, owns_client = _acquire_async_client()
                    request = client.build_request(self.method, self.target, content=self.body,
                                                   headers=self.upstream_headers)
                    upstream = await client.send(request, stream=True)
                    headers = _forward_headers(upstream)
                    if "text/event-stream" in upstream.headers.get("content-type", ""):
                        headers["X-Accel-Buffering"] = "no"
                    raw_headers = [(k.lower().encode("latin-1"), v.encode("latin-1"))
                                   for k, v in headers.items()]
                    await send({"type": "http.response.start", "status": upstream.status_code,
                                "headers": raw_headers})
                    started = True
                    # No 8192-byte accumulator: forward each available chunk.
                    # aiter_bytes decodes content just like the previous proxy;
                    # content-encoding/length are deliberately not forwarded.
                    async for chunk in upstream.aiter_bytes():
                        await send({"type": "http.response.body", "body": chunk, "more_body": True})
                    await send({"type": "http.response.body", "body": b"", "more_body": False})
                except httpx.HTTPError as error:
                    if started:
                        raise  # terminate a broken stream, never manufacture DONE
                    response = _error_response(503, f"Qwen upstream is unavailable: {error.__class__.__name__}")
                    await response(scope, receive, send)
                except OSError:
                    if not started:
                        raise
                    # ASGI 2.4 permits send() to raise on a closed downstream.
                finally:
                    tasks.cancel_scope.cancel()
        finally:
            # Cancellation must not cancel aclose(). Unlock even if close fails.
            try:
                with anyio.CancelScope(shield=True):
                    try:
                        if upstream is not None:
                            await upstream.aclose()
                    finally:
                        if client is not None and owns_client:
                            await client.aclose()
            finally:
                with anyio.CancelScope(shield=True):
                    await anyio.to_thread.run_sync(self.guard.__exit__, None, None, None)


def _valid_upstream_path(path: str) -> bool:
    decoded = path
    for _ in range(8):
        expanded = unquote(decoded)
        if expanded == decoded:
            break
        decoded = expanded
    # Never let URL normalization escape /v1 into runtime settings or health.
    return not any(segment in {".", ".."} for segment in decoded.replace("\\", "/").split("/")) and not any(char in decoded for char in "\\?#")


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
    payload = llm_runtime.prepare_payload(payload, llm_runtime.selection()[1])
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


@app.get("/admin/llm-runtime")
def llm_runtime_status():
    result = llm_runtime.status()
    result["model"] = llm_runtime.selection()[1]
    result["switching"] = _SWITCH_LOCK.locked() or result.get("state") == "switching"
    result["strata_installed"] = Path("/srv/brmmedia/strata/Strata/DEPLOYMENT_READY").exists()
    if _SWITCH_ERROR:
        result["message"] = _SWITCH_ERROR
    return result


@app.post("/admin/llm-runtime/switch", status_code=202)
def switch_llm(request: SwitchModelRequest):
    global _SWITCH_ERROR
    if request.target not in {"qwen38", "strata"}:
        raise HTTPException(400, "Unknown runtime")
    if request.target == "strata" and not Path("/srv/brmmedia/strata/Strata/DEPLOYMENT_READY").exists():
        raise HTTPException(409, "Strata 安装、校验尚未完成")
    if not _SWITCH_LOCK.acquire(blocking=False):
        raise HTTPException(409, "已有模型切换正在进行")
    # Fail before accepting when a live inference holds the shared lock.
    try:
        import fcntl
        with llm_runtime.TRAFFIC_LOCK.open("rb") as traffic:
            fcntl.flock(traffic, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, ImportError):
        _SWITCH_LOCK.release()
        raise HTTPException(409, "模型仍在处理请求或切换功能未部署")

    def worker():
        global _SWITCH_ERROR
        try:
            subprocess.run(["sudo", "-n", "/usr/local/sbin/brmmedia-request-llm-switch", request.target],
                           check=True, timeout=20, capture_output=True)
        except (OSError, subprocess.SubprocessError):
            # Controller owns durable status and rollback; never expose stderr
            # or private configuration to API callers.
            _SWITCH_ERROR = "切换未成功，请刷新状态并检查当前模型；需要管理员检查切换日志。"
        finally:
            _SWITCH_LOCK.release()
    _SWITCH_ERROR = ""
    threading.Thread(target=worker, daemon=True).start()
    return {"accepted": True, "target": request.target}


@app.exception_handler(HTTPException)
async def _http_error(_: Request, exception: HTTPException) -> JSONResponse:
    return _error_response(exception.status_code, str(exception.detail))


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "upstream": _active_qwen_base()}


@app.get("/admin/api-keys", response_model=list[KeyRecord])
def list_api_keys() -> list[dict[str, Any]]:
    _flush_usage_buffer()
    with _database() as connection:
        rows = connection.execute("SELECT * FROM api_keys ORDER BY created_at DESC, id DESC").fetchall()
    return [_record_from_row(row) for row in rows]


@app.post("/admin/api-keys", response_model=CreatedKey, status_code=201)
def create_api_key(request: CreateKeyRequest) -> dict[str, Any]:
    record, api_key = _create_key(request.name, request.expires_in_days)
    return {**record, "api_key": api_key}


@app.post("/admin/api-keys/{key_id}/reveal")
def reveal_api_key(key_id: str) -> JSONResponse:
    from cryptography.fernet import InvalidToken
    row = _require_key(key_id)
    if not row["key_ciphertext"]:
        raise HTTPException(status_code=409, detail="旧密钥仅保存哈希，无法恢复明文；请使用原保存副本，或轮换后查看新密钥")
    try:
        api_key = _key_cipher().decrypt(row["key_ciphertext"].encode("ascii")).decode("utf-8")
    except InvalidToken:
        raise HTTPException(status_code=503, detail="密钥无法解密，请恢复匹配的加密文件备份") from None
    return JSONResponse({"id": row["id"], "api_key": api_key}, headers={"Cache-Control": "no-store", "Pragma": "no-cache"})


@app.post("/admin/api-keys/{key_id}/disable", response_model=KeyRecord)
def disable_api_key(key_id: str) -> dict[str, Any]:
    _require_key(key_id)
    with _database() as connection:
        connection.execute(
            "UPDATE api_keys SET status = 'disabled', disabled_at = ? WHERE id = ? AND status = 'active'",
            (_timestamp(), key_id),
        )
        row = connection.execute("SELECT * FROM api_keys WHERE id = ?", (key_id,)).fetchone()
    _invalidate_key_cache()
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
    _invalidate_key_cache()
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
    _invalidate_key_cache()
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
    _invalidate_key_cache()
    return _record_from_row(row)


@app.post("/admin/api-keys/{key_id}/rotate", response_model=CreatedKey, status_code=201)
def rotate_api_key(key_id: str) -> dict[str, Any]:
    previous = _require_key(key_id)
    if previous["status"] == "revoked":
        raise HTTPException(status_code=409, detail="Revoked API keys cannot be rotated")
    remaining_days = None
    if previous["expires_at"] is not None:
        remaining_days = max(1, (previous["expires_at"] - _timestamp() + 86399) // 86400)
    record, api_key = _create_key(previous["name"], remaining_days, revoke_id=key_id)
    _invalidate_key_cache()
    return {**record, "api_key": api_key}


def _resolve_upstream_target_and_headers(
    upstream_path: str, query: str, base_headers: dict[str, str]
) -> tuple[str, dict[str, str]]:
    target = _active_qwen_base().rstrip("/")
    if upstream_path:
        target += "/" + upstream_path
    if query:
        target += "?" + query
    headers = dict(base_headers)
    headers.update(llm_runtime.upstream_headers(llm_runtime.selection()[1]))
    return target, headers


@app.api_route("/v1/{upstream_path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
async def qwen_proxy(upstream_path: str, request: Request):
    token = _authorization_key(request)
    await anyio.to_thread.run_sync(_validate_and_record_usage, token)
    if not _valid_upstream_path(upstream_path):
        return _error_response(400, "Invalid API path")
    guard = llm_runtime.inference_guard()
    try:
        await anyio.to_thread.run_sync(guard.__enter__)
    except RuntimeError as error:
        return _error_response(503, str(error))
    try:
        base_headers = {
            key: value for key, value in request.headers.items()
            if key.lower() in {"accept", "content-type", "user-agent"}
        }
        target, headers = await anyio.to_thread.run_sync(
            _resolve_upstream_target_and_headers, upstream_path, request.url.query, base_headers
        )
        body = await request.body()
        if upstream_path.rstrip("/") == "chat/completions":
            body = await anyio.to_thread.run_sync(_prepare_chat_payload, body)
        return _UpstreamResponse(request.method, target, body, headers, guard)
    except BaseException:
        # Failures/disconnect while reading the request body precede response
        # ownership, so they must also release the lock here.
        with anyio.CancelScope(shield=True):
            await anyio.to_thread.run_sync(guard.__exit__, None, None, None)
        raise
