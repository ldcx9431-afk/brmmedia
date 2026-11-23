"""Shared runtime selection and inference/switch interlock (no credentials)."""
import json
import os
from contextlib import contextmanager
from pathlib import Path

ACTIVE = Path(os.getenv("BRM_QWEN_ACTIVE_ENV", "/etc/brmmedia/qwen-active.env"))
STATE = Path("/var/lib/brmmedia/llm-switch.json")
TRAFFIC_LOCK = Path("/var/lib/brmmedia/llm-traffic.lock")
STRATA_KEY = Path("/etc/brmmedia/strata-api.key")


def upstream_headers(model):
    if model.startswith("strata-"):
        return {"Authorization": "Bearer " + STRATA_KEY.read_text().strip()}
    return {}


def selection(default_base="http://127.0.0.1:8001/v1", default_model="qwen38-27b-ud-q4-k-m"):
    values = {}
    try:
        for line in ACTIVE.read_text().splitlines():
            key, sep, value = line.partition("=")
            if sep:
                values[key] = value.strip().strip('"')
    except OSError:
        pass
    base = values.get("QWEN_ACTIVE_UPSTREAM", default_base).rstrip("/")
    return (base if base.endswith("/v1") else base + "/v1", values.get("QWEN_ACTIVE_MODEL", default_model))


def status():
    try:
        return json.loads(STATE.read_text())
    except (OSError, ValueError):
        return {"state": "idle", "model": selection()[1], "message": "尚未切换"}


@contextmanager
def inference_guard():
    # Only activate the Linux interlock after deployment has provisioned it.
    if os.name != "posix" or not TRAFFIC_LOCK.exists():
        yield
        return
    import fcntl
    with TRAFFIC_LOCK.open("rb") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("模型正在切换，请等待加载完成后重试")
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def prepare_payload(payload, model):
    if model.startswith("strata-"):
        payload = dict(payload)
        payload["model"] = model
        thinking = payload.get("chat_template_kwargs", {}).get("enable_thinking", False)
        payload.setdefault("reasoning_effort", "medium" if thinking else "none")
    return payload
