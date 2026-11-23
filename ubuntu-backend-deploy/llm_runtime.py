"""Shared runtime selection and inference/switch interlock (no credentials)."""
import json
import logging
import os
from contextlib import contextmanager
from pathlib import Path

ACTIVE = Path(os.getenv("BRM_QWEN_ACTIVE_ENV", "/etc/brmmedia/qwen-active.env"))
STATE = Path("/var/lib/brmmedia/llm-switch.json")
TRAFFIC_LOCK = Path("/var/lib/brmmedia/llm-traffic.lock")
STRATA_KEY = Path("/etc/brmmedia/strata-api.key")
STRATA_DEFAULT_BASE = os.getenv("BRM_STRATA_URL", "http://127.0.0.1:8002/v1")
STRATA_DEFAULT_MODEL = os.getenv("BRM_STRATA_MODEL", "strata-qwen38")


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


def enhance_prompt(
    prompt: str,
    *,
    workflow: str = "",
    timeout: float = 8.0,
    max_length: int = 8000,
) -> str:
    """Optionally enrich a media generation prompt using dual-A4000 Strata (:8002).

    Runs with zero GPU0 overhead. Any connectivity, lock, or format failure
    transparently falls back to returning the original prompt untouched.
    """
    raw = str(prompt or "").strip()
    if not raw:
        return prompt
    try:
        import requests
    except ImportError:
        return prompt

    base, model = selection(default_base=STRATA_DEFAULT_BASE, default_model=STRATA_DEFAULT_MODEL)
    # Prefer the dedicated Strata endpoint when configured or active selection still points to retired :8001.
    explicit_strata = os.getenv("BRM_STRATA_URL", "").strip()
    if explicit_strata:
        base = explicit_strata.rstrip("/")
        if not base.endswith("/v1"):
            base += "/v1"
        if not model.startswith("strata-"):
            model = STRATA_DEFAULT_MODEL
    elif ":8001" in base and not ACTIVE.exists():
        base = STRATA_DEFAULT_BASE.rstrip("/")
        if not base.endswith("/v1"):
            base += "/v1"
        model = STRATA_DEFAULT_MODEL

    wf_lower = str(workflow or "").lower()
    if "music" in wf_lower or "音乐" in wf_lower or "acestep" in wf_lower:
        sys_instruction = (
            "You are a concise music style tag refiner. Expand and polish the input into crisp, "
            "comma-separated English music production tags (genre, mood, instruments, vocal style, tempo). "
            "Output ONLY the final tag string with no quotes or explanations."
        )
    elif "video" in wf_lower or "h3" in wf_lower or "ltx" in wf_lower or "视频" in wf_lower:
        sys_instruction = (
            "You are a concise AI video prompt enhancer. Preserve the user's exact subject and intent, "
            "enriching visual composition, lighting, camera motion, and natural ambient audio cues in one "
            "vivid paragraph. Output ONLY the enhanced prompt with no quotes or commentary."
        )
    else:
        sys_instruction = (
            "You are a concise AI image prompt enhancer. Preserve the user's exact subject and intent, "
            "enriching composition, lighting, texture, and style details clearly. "
            "Output ONLY the enhanced prompt with no quotes or commentary."
        )

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": sys_instruction},
            {"role": "user", "content": raw},
        ],
        "temperature": 0.6,
        "max_tokens": 384,
        "stream": False,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    payload = prepare_payload(payload, model)

    try:
        headers = {"Content-Type": "application/json"}
        try:
            headers.update(upstream_headers(model))
        except OSError:
            pass
        with inference_guard():
            session_cls = getattr(requests, "Session", None)
            if callable(session_cls):
                with session_cls() as s:
                    s.trust_env = False
                    resp = s.post(
                        f"{base.rstrip('/')}/chat/completions",
                        json=payload,
                        headers=headers,
                        timeout=(2.0, max(1.0, float(timeout))),
                    )
            else:
                resp = requests.post(
                    f"{base.rstrip('/')}/chat/completions",
                    json=payload,
                    headers=headers,
                    timeout=(2.0, max(1.0, float(timeout))),
                )
        if getattr(resp, "status_code", 0) != 200:
            return prompt
        data = resp.json()
        choices = data.get("choices") or []
        if not choices or not isinstance(choices[0], dict):
            return prompt
        msg = choices[0].get("message") or {}
        enhanced = str(msg.get("content") or "").strip()
        if not enhanced:
            return prompt
        if len(enhanced) > max_length:
            enhanced = enhanced[:max_length].rstrip()
        return enhanced
    except Exception as exc:
        logging.warning("Strata prompt enhance failed (%s); falling back to original prompt", exc)
        return prompt
