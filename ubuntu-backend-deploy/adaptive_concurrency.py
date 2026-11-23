# -*- coding: utf-8 -*-
"""Dynamic hardware- and workload-aware concurrency controller for BRMMedia.

Classifies workflows by GPU0/RAM/CPU footprint and computes safe real-time
concurrency limits from live host telemetry (GPU0 VRAM free & utilization,
host MemAvailable, CPU loadavg & utilization) with anti-starvation support.
"""

from __future__ import annotations

import json
import math
import os
import re
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


TIER_VIDEO_HEAVY = "video_heavy"
TIER_IMAGE_HEAVY = "image_heavy"
TIER_IMAGE_LIGHT = "image_light"
TIER_AUDIO_GPU = "audio_gpu"
TIER_VOICE_CPU = "voice_cpu"

ALL_TIERS = (
    TIER_VIDEO_HEAVY,
    TIER_IMAGE_HEAVY,
    TIER_IMAGE_LIGHT,
    TIER_AUDIO_GPU,
    TIER_VOICE_CPU,
)


@dataclass(frozen=True)
class TaskProfile:
    """Resource footprint profile for a media/audio workflow."""

    tier: str
    exclusive: bool
    uses_gpu: bool
    shares_comfy_light_lease: bool
    weight: int


_WORKFLOW_PROFILES: dict[str, TaskProfile] = {
    # Heavy video workflows (~18-24.1 GiB peak on A5000 24G): strictly exclusive = 1
    "MiniMaxH3-文生视频": TaskProfile(TIER_VIDEO_HEAVY, True, True, False, 100),
    "MiniMaxH3-图生视频": TaskProfile(TIER_VIDEO_HEAVY, True, True, False, 100),
    "LTX23-文生视频": TaskProfile(TIER_VIDEO_HEAVY, True, True, False, 100),
    "LTX23-图生视频": TaskProfile(TIER_VIDEO_HEAVY, True, True, False, 100),
    "LTX23-首尾帧视频": TaskProfile(TIER_VIDEO_HEAVY, True, True, False, 100),
    "LTX23-单图数字人-语音驱动": TaskProfile(TIER_VIDEO_HEAVY, True, True, False, 100),
    "SeedVR2-图像/视频增强修复": TaskProfile(TIER_VIDEO_HEAVY, True, True, False, 100),
    "text-to-video": TaskProfile(TIER_VIDEO_HEAVY, True, True, False, 100),
    "image-to-video": TaskProfile(TIER_VIDEO_HEAVY, True, True, False, 100),
    "ltx-text-to-video": TaskProfile(TIER_VIDEO_HEAVY, True, True, False, 100),
    "ltx-image-to-video": TaskProfile(TIER_VIDEO_HEAVY, True, True, False, 100),
    "first-last-frame-video": TaskProfile(TIER_VIDEO_HEAVY, True, True, False, 100),
    "talking-avatar-video": TaskProfile(TIER_VIDEO_HEAVY, True, True, False, 100),
    "seedvr2-enhance": TaskProfile(TIER_VIDEO_HEAVY, True, True, False, 100),
    # Heavy image workflows (~20-24.1 GiB peak on A5000 24G): strictly exclusive = 1
    "QwenImage21-文生图": TaskProfile(TIER_IMAGE_HEAVY, True, True, False, 90),
    "QwenImage21-图生图": TaskProfile(TIER_IMAGE_HEAVY, True, True, False, 90),
    "MiniMaxH3Image-文生图": TaskProfile(TIER_IMAGE_HEAVY, True, True, False, 90),
    "MiniMaxH3Image-图生图": TaskProfile(TIER_IMAGE_HEAVY, True, True, False, 90),
    "Flux3-文生图": TaskProfile(TIER_IMAGE_HEAVY, True, True, False, 90),
    "Flux3-图生图": TaskProfile(TIER_IMAGE_HEAVY, True, True, False, 90),
    "qwen-image21": TaskProfile(TIER_IMAGE_HEAVY, True, True, False, 90),
    "qwen-image21-turbo": TaskProfile(TIER_IMAGE_HEAVY, True, True, False, 90),
    "qwen-image21-edit": TaskProfile(TIER_IMAGE_HEAVY, True, True, False, 90),
    "h3-image-text-to-image": TaskProfile(TIER_IMAGE_HEAVY, True, True, False, 90),
    "h3-image-edit": TaskProfile(TIER_IMAGE_HEAVY, True, True, False, 90),
    "flux3-text-to-image": TaskProfile(TIER_IMAGE_HEAVY, True, True, False, 90),
    "flux3-image-edit": TaskProfile(TIER_IMAGE_HEAVY, True, True, False, 90),
    # Light/medium GPU image workflows (~6-12 GiB): pipelined concurrency 1..3
    "image_z_image_turbo": TaskProfile(TIER_IMAGE_LIGHT, False, True, True, 40),
    "Flux2Klein-快速图生图": TaskProfile(TIER_IMAGE_LIGHT, False, True, True, 40),
    "image_flux2_klein_image_edit_4b_base": TaskProfile(TIER_IMAGE_LIGHT, False, True, True, 40),
    "text-to-image": TaskProfile(TIER_IMAGE_LIGHT, False, True, True, 40),
    "image-edit": TaskProfile(TIER_IMAGE_LIGHT, False, True, True, 40),
    "flux2-fast-edit": TaskProfile(TIER_IMAGE_LIGHT, False, True, True, 40),
    # Light/medium GPU audio workflows (~4-10 GiB): pipelined concurrency 1..2
    "音乐生成": TaskProfile(TIER_AUDIO_GPU, False, True, True, 35),
    "music-generate": TaskProfile(TIER_AUDIO_GPU, False, True, True, 35),
    "TTS-语音克隆": TaskProfile(TIER_AUDIO_GPU, False, True, True, 35),
    "IndexTTS-2.5-语音克隆": TaskProfile(TIER_AUDIO_GPU, False, True, False, 35),
    "Qwen3-TTS-语音克隆": TaskProfile(TIER_AUDIO_GPU, False, True, False, 35),
    "voice-clone": TaskProfile(TIER_AUDIO_GPU, False, True, False, 35),
    "qwen3-voice-clone": TaskProfile(TIER_AUDIO_GPU, False, True, False, 35),
    # Pure CPU voice workflows (Zonos3-mini :1919, Breeze-TTS2 :9206): zero GPU0 VRAM, decoupled 1..4
    "Zonos3-语音克隆": TaskProfile(TIER_VOICE_CPU, False, False, False, 10),
    "zonos3-voice-clone": TaskProfile(TIER_VOICE_CPU, False, False, False, 10),
    "Breeze-TTS2-语音克隆": TaskProfile(TIER_VOICE_CPU, False, False, False, 10),
    "breeze-tts2-voice-clone": TaskProfile(TIER_VOICE_CPU, False, False, False, 10),
    "breeze-tts2": TaskProfile(TIER_VOICE_CPU, False, False, False, 10),
    "breezyvoice": TaskProfile(TIER_VOICE_CPU, False, False, False, 10),
}

_DEFAULT_CONSERVATIVE_PROFILE = TaskProfile(TIER_IMAGE_HEAVY, True, True, False, 90)


def classify_workflow(workflow_name: str) -> TaskProfile:
    """Return the resource profile for a Gradio or LAN REST workflow name."""
    name = str(workflow_name or "").strip()
    if not name:
        return _DEFAULT_CONSERVATIVE_PROFILE
    direct = _WORKFLOW_PROFILES.get(name)
    if direct is not None:
        return direct
    lower = name.lower()
    if "zonos3" in lower or "breeze-tts2" in lower or "breeze_tts2" in lower or "breezyvoice" in lower:
        return TaskProfile(TIER_VOICE_CPU, False, False, False, 10)
    if "minimaxh3image" in lower or "h3-image" in lower or "qwenimage21" in lower or "qwen-image21" in lower or "flux3" in lower:
        return TaskProfile(TIER_IMAGE_HEAVY, True, True, False, 90)
    if "minimaxh3" in lower or "ltx" in lower or "seedvr2" in lower or "video" in lower or "视频" in lower:
        return TaskProfile(TIER_VIDEO_HEAVY, True, True, False, 100)
    if "z_image" in lower or "z-image" in lower or "flux2" in lower:
        return TaskProfile(TIER_IMAGE_LIGHT, False, True, True, 40)
    if "music" in lower or "音乐" in lower or "acestep" in lower:
        return TaskProfile(TIER_AUDIO_GPU, False, True, True, 35)
    if "indextts" in lower or "qwen3_tts" in lower or "qwen3-tts" in lower or "tts" in lower:
        return TaskProfile(TIER_AUDIO_GPU, False, True, False, 35)
    return _DEFAULT_CONSERVATIVE_PROFILE


def _parse_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def _parse_int(value: Any, default: int, min_value: int, max_value: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(min_value, min(max_value, parsed))


def _parse_float(value: Any, default: float, min_value: float, max_value: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return max(min_value, min(max_value, parsed))


def _read_config_file(config_path: Path | None = None) -> dict[str, Any]:
    path = config_path or Path(
        os.environ.get("BRMMEDIA_CONFIG_PATH") or (Path(__file__).resolve().parent / "yzy_config.json")
    )
    try:
        if path.is_file():
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                return raw
    except (OSError, ValueError):
        pass
    return {}


@dataclass(frozen=True)
class AdaptiveConfig:
    enabled: bool = True
    light_gpu_max: int = 3
    audio_gpu_max: int = 2
    voice_cpu_max: int = 4
    gpu_vram_min_free_mib: int = 6144
    gpu_vram_warn_free_mib: int = 10240
    gpu_util_warn_pct: float = 85.0
    gpu_util_critical_pct: float = 98.0
    ram_min_avail_gib: float = 16.0
    ram_warn_avail_gib: float = 32.0
    ram_critical_used_pct: float = 88.0
    ram_warn_used_pct: float = 78.0
    cpu_load_ratio_moderate: float = 0.40
    cpu_load_ratio_warn: float = 0.60
    cpu_load_ratio_critical: float = 0.85
    cpu_usage_moderate_pct: float = 55.0
    cpu_usage_warn_pct: float = 75.0
    cpu_usage_critical_pct: float = 90.0
    starvation_age_seconds: float = 60.0
    sticky_streak_max: int = 4
    failure_cooldown_seconds: float = 120.0
    vram_safety_margin_mib: int = 2048


def load_adaptive_config(
    config_dict: dict[str, Any] | None = None,
    *,
    config_path: Path | None = None,
) -> AdaptiveConfig:
    """Load adaptive concurrency parameters from yzy_config.json with env overrides."""
    cfg = dict(config_dict) if config_dict is not None else _read_config_file(config_path)
    nested = cfg.get("adaptive_concurrency_settings")
    if not isinstance(nested, dict):
        nested = {}

    def pick(cfg_key: str, env_key: str, default: Any) -> Any:
        if env_key in os.environ and os.environ[env_key] != "":
            return os.environ[env_key]
        if cfg_key in nested:
            return nested[cfg_key]
        if cfg_key in cfg:
            return cfg[cfg_key]
        return default

    enabled = _parse_bool(pick("adaptive_concurrency", "BRM_ADAPTIVE_CONCURRENCY", True), True)
    return AdaptiveConfig(
        enabled=enabled,
        light_gpu_max=_parse_int(pick("adaptive_light_gpu_max", "BRM_ADAPTIVE_LIGHT_GPU_MAX", 3), 3, 1, 4),
        audio_gpu_max=_parse_int(pick("adaptive_audio_gpu_max", "BRM_ADAPTIVE_AUDIO_GPU_MAX", 2), 2, 1, 3),
        voice_cpu_max=_parse_int(pick("adaptive_voice_cpu_max", "BRM_ADAPTIVE_VOICE_CPU_MAX", 4), 4, 1, 8),
        gpu_vram_min_free_mib=_parse_int(
            pick("adaptive_gpu_vram_min_free_mib", "BRM_ADAPTIVE_GPU_VRAM_MIN_FREE_MIB", 6144), 6144, 512, 22528
        ),
        gpu_vram_warn_free_mib=_parse_int(
            pick("adaptive_gpu_vram_warn_free_mib", "BRM_ADAPTIVE_GPU_VRAM_WARN_FREE_MIB", 10240), 10240, 1024, 23552
        ),
        gpu_util_warn_pct=_parse_float(
            pick("adaptive_gpu_util_warn_pct", "BRM_ADAPTIVE_GPU_UTIL_WARN_PCT", 85.0), 85.0, 10.0, 100.0
        ),
        gpu_util_critical_pct=_parse_float(
            pick("adaptive_gpu_util_critical_pct", "BRM_ADAPTIVE_GPU_UTIL_CRITICAL_PCT", 98.0), 98.0, 20.0, 100.0
        ),
        ram_min_avail_gib=_parse_float(
            pick("adaptive_ram_min_avail_gib", "BRM_ADAPTIVE_RAM_MIN_AVAIL_GIB", 16.0), 16.0, 1.0, 256.0
        ),
        ram_warn_avail_gib=_parse_float(
            pick("adaptive_ram_warn_avail_gib", "BRM_ADAPTIVE_RAM_WARN_AVAIL_GIB", 32.0), 32.0, 2.0, 512.0
        ),
        ram_critical_used_pct=_parse_float(
            pick("adaptive_ram_critical_used_pct", "BRM_ADAPTIVE_RAM_CRITICAL_USED_PCT", 88.0), 88.0, 50.0, 99.0
        ),
        ram_warn_used_pct=_parse_float(
            pick("adaptive_ram_warn_used_pct", "BRM_ADAPTIVE_RAM_WARN_USED_PCT", 78.0), 78.0, 40.0, 98.0
        ),
        cpu_load_ratio_moderate=_parse_float(
            pick("adaptive_cpu_load_moderate", "BRM_ADAPTIVE_CPU_LOAD_MODERATE", 0.40), 0.40, 0.1, 4.0
        ),
        cpu_load_ratio_warn=_parse_float(
            pick("adaptive_cpu_load_warn", "BRM_ADAPTIVE_CPU_LOAD_WARN", 0.60), 0.60, 0.2, 4.0
        ),
        cpu_load_ratio_critical=_parse_float(
            pick("adaptive_cpu_load_critical", "BRM_ADAPTIVE_CPU_LOAD_CRITICAL", 0.85), 0.85, 0.3, 8.0
        ),
        cpu_usage_moderate_pct=_parse_float(
            pick("adaptive_cpu_usage_moderate_pct", "BRM_ADAPTIVE_CPU_USAGE_MODERATE_PCT", 55.0), 55.0, 10.0, 99.0
        ),
        cpu_usage_warn_pct=_parse_float(
            pick("adaptive_cpu_usage_warn_pct", "BRM_ADAPTIVE_CPU_USAGE_WARN_PCT", 75.0), 75.0, 20.0, 99.0
        ),
        cpu_usage_critical_pct=_parse_float(
            pick("adaptive_cpu_usage_critical_pct", "BRM_ADAPTIVE_CPU_USAGE_CRITICAL_PCT", 90.0), 90.0, 30.0, 100.0
        ),
        starvation_age_seconds=_parse_float(
            pick("adaptive_starvation_seconds", "BRM_ADAPTIVE_STARVATION_SECONDS", 60.0), 60.0, 1.0, 3600.0
        ),
        sticky_streak_max=_parse_int(
            pick("adaptive_sticky_streak_max", "BRM_ADAPTIVE_STICKY_STREAK_MAX", 4), 4, 1, 16
        ),
        failure_cooldown_seconds=_parse_float(
            pick("adaptive_failure_cooldown_seconds", "BRM_ADAPTIVE_FAILURE_COOLDOWN_SECONDS", 120.0),
            120.0,
            5.0,
            3600.0,
        ),
        vram_safety_margin_mib=_parse_int(
            pick("adaptive_vram_safety_margin_mib", "BRM_ADAPTIVE_VRAM_SAFETY_MARGIN_MIB", 2048),
            2048,
            512,
            8192,
        ),
    )


_SIZE_RE = re.compile(r"(\d+)\s*[×xX*]\s*(\d+)")
_OOM_CUDA_KEYWORDS = (
    "out of memory",
    "cuda out of memory",
    "cuda error",
    "cublas",
    "cudnn",
    "illegal address",
    "device-side assert",
    "alloc failed",
)


def _is_oom_or_cuda_error(error: str) -> bool:
    text = str(error or "").strip().lower()
    if not text:
        return False
    if "oom" in re.findall(r"[a-z]+", text):
        return True
    return any(keyword in text for keyword in _OOM_CUDA_KEYWORDS)


def _extract_dimensions(params: dict[str, Any]) -> tuple[int, int]:
    for w_key, h_key in (("width", "height"), ("output_width", "output_height")):
        raw_w, raw_h = params.get(w_key), params.get(h_key)
        if raw_w is not None and raw_h is not None and not isinstance(raw_w, bool) and not isinstance(raw_h, bool):
            try:
                w, h = int(raw_w), int(raw_h)
                if w > 0 and h > 0:
                    return w, h
            except (TypeError, ValueError):
                pass
    size_val = params.get("size")
    if isinstance(size_val, str):
        match = _SIZE_RE.search(size_val)
        if match:
            return max(64, int(match.group(1))), max(64, int(match.group(2)))
    return 1024, 1024


def estimate_task_cost(workflow_name: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """Estimate VRAM (MiB) and runtime budget from workflow and task parameters."""
    profile = classify_workflow(workflow_name)
    args = params if isinstance(params, dict) else {}
    lower = str(workflow_name or "").strip().lower()
    width, height = _extract_dimensions(args)
    pixels = max(1, width * height)
    megapixels = pixels / float(1024 * 1024)

    try:
        batch = max(1, int(args.get("batch", 1) or 1))
    except (TypeError, ValueError):
        batch = 1

    try:
        seconds = float(args.get("seconds") or args.get("duration") or 0.0)
    except (TypeError, ValueError):
        seconds = 0.0

    steps_raw = args.get("steps")
    try:
        steps = max(1, int(steps_raw)) if steps_raw is not None and not isinstance(steps_raw, bool) else None
    except (TypeError, ValueError):
        steps = None

    if profile.tier == TIER_VOICE_CPU:
        text_len = len(str(args.get("text") or args.get("prompt") or ""))
        return {
            "tier": profile.tier,
            "est_vram_mib": 0,
            "est_seconds": round(max(2.0, text_len * 0.03), 1),
            "batch": 1,
            "pixels": 0,
        }

    if profile.tier == TIER_IMAGE_LIGHT:
        if "flux2" in lower or lower == "image-edit":
            est_vram = int(7000 + 1500 * megapixels * batch)
            est_sec = max(5.0, 10.0 * megapixels * batch)
        else:
            est_vram = int(5800 + 1400 * megapixels * batch)
            est_sec = max(3.0, 6.0 * megapixels * batch)
    elif profile.tier == TIER_AUDIO_GPU:
        if "音乐" in lower or "music" in lower or "acestep" in lower:
            model = str(args.get("model") or "turbo").strip().lower()
            dur = max(10.0, seconds or 30.0)
            is_base = "base" in model
            base_vram = 8200 if is_base else 6400
            est_vram = int(base_vram + (dur / 60.0) * 600)
            est_sec = (25.0 if is_base else 10.0) + dur * (0.6 if is_base else 0.25)
        else:
            est_vram = 5500
            est_sec = 12.0
    elif profile.tier == TIER_IMAGE_HEAVY:
        if "qwen" in lower:
            turbo = args.get("turbo")
            eff_steps = steps if steps is not None else (40 if turbo is False else 8)
            est_vram = int(17500 + 2200 * megapixels + max(0, eff_steps - 8) * 35)
            est_sec = max(8.0, (12.0 if eff_steps <= 12 else 45.0) * max(0.6, megapixels))
        else:
            est_vram = int(18500 + 1500 * megapixels)
            est_sec = max(12.0, 25.0 * megapixels)
    else:  # TIER_VIDEO_HEAVY
        if "seedvr2" in lower:
            variant = str(args.get("model_variant") or "7b").strip().lower()
            base_vram = 12000 if variant == "3b" else 18500
            dur = max(1.0, seconds or 5.0)
            est_vram = int(base_vram + 2000 * min(4.0, megapixels))
            est_sec = (15.0 if variant == "3b" else 35.0) * max(1.0, dur / 5.0)
        else:
            dur = max(3.0, seconds or 5.0)
            eff_steps = steps or 20
            est_vram = int(min(24000, 19000 + dur * 280 + 800 * megapixels))
            est_sec = max(20.0, dur * (18.0 if eff_steps >= 15 else 10.0))

    return {
        "tier": profile.tier,
        "est_vram_mib": max(512, est_vram),
        "est_seconds": round(float(est_sec), 1),
        "batch": batch,
        "pixels": pixels,
    }


def can_admit_gpu_task(
    workflow_name: str,
    params: dict[str, Any] | None,
    running_gpu_specs: list[tuple[str, dict[str, Any] | None]],
    snapshot: dict[str, Any],
    *,
    safety_margin_mib: int | None = None,
) -> bool:
    """Decide if a GPU task fits within the live GPU0 VRAM budget alongside running tasks."""
    if not running_gpu_specs:
        return True
    profile = classify_workflow(workflow_name)
    if profile.exclusive or not profile.shares_comfy_light_lease:
        return False
    hw = snapshot.get("hardware") if isinstance(snapshot, dict) else {}
    hw = hw if isinstance(hw, dict) else {}
    margin = int(
        safety_margin_mib
        if safety_margin_mib is not None
        else snapshot.get("vram_safety_margin_mib", 2048)
    )
    gpu_total = hw.get("gpu0_vram_total_mib")
    gpu_free = hw.get("gpu0_vram_free_mib")
    if gpu_free is not None and int(gpu_free) < margin + 1024:
        return False
    budget = max(8192, int(gpu_total or 24564) - margin)
    active_cost = sum(
        estimate_task_cost(wf, args)["est_vram_mib"] for wf, args in running_gpu_specs
    )
    next_cost = estimate_task_cost(workflow_name, params)["est_vram_mib"]
    return (active_cost + next_cost) <= budget


def _select_gpu0(gpus: list[dict[str, Any]] | None) -> dict[str, Any] | None:
    if not gpus:
        return None
    for gpu in gpus:
        if gpu.get("index") == 0 or "A5000" in str(gpu.get("name", "")).upper():
            return gpu
    return gpus[0]


def evaluate_concurrency(
    *,
    cpu: dict[str, Any] | None = None,
    ram: dict[str, Any] | None = None,
    gpus: list[dict[str, Any]] | None = None,
    config: AdaptiveConfig | None = None,
    active_counts: dict[str, int] | None = None,
    external_queue_depth: int = 0,
    gpu_cooldown_active: bool = False,
    cooldown_reason: str = "",
    telemetry_feedback: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compute real-time per-tier concurrency limits and degrade reasons."""
    cfg = config or load_adaptive_config()
    configured_max = {
        TIER_VIDEO_HEAVY: 1,
        TIER_IMAGE_HEAVY: 1,
        TIER_IMAGE_LIGHT: cfg.light_gpu_max,
        TIER_AUDIO_GPU: min(cfg.audio_gpu_max, cfg.light_gpu_max),
        TIER_VOICE_CPU: cfg.voice_cpu_max,
    }

    gpu0 = _select_gpu0(gpus)
    gpu_used = gpu0.get("vram_used_mib") if isinstance(gpu0, dict) else None
    gpu_total = gpu0.get("vram_total_mib") if isinstance(gpu0, dict) else None
    gpu_free = (
        max(0, int(gpu_total) - int(gpu_used))
        if gpu_used is not None and gpu_total is not None and int(gpu_total) > 0
        else None
    )
    gpu_util = gpu0.get("util_pct") if isinstance(gpu0, dict) else None

    ram_total = ram.get("total_gib") if isinstance(ram, dict) else None
    ram_used = ram.get("used_gib") if isinstance(ram, dict) else None
    ram_avail = ram.get("avail_gib") if isinstance(ram, dict) else None
    if ram_avail is None and ram_total is not None and ram_used is not None:
        ram_avail = round(max(0.0, float(ram_total) - float(ram_used)), 1)
    ram_pct = ram.get("percent") if isinstance(ram, dict) else None

    cpu_count = int((cpu or {}).get("cpu_count") or os.cpu_count() or 1)
    cpu_load_1m = (cpu or {}).get("load_1m")
    cpu_usage_pct = (cpu or {}).get("usage_pct")
    cpu_load_ratio = (
        round(float(cpu_load_1m) / max(1, cpu_count), 3)
        if cpu_load_1m is not None
        else None
    )

    hardware = {
        "gpu0_vram_used_mib": gpu_used,
        "gpu0_vram_total_mib": gpu_total,
        "gpu0_vram_free_mib": gpu_free,
        "gpu0_util_pct": gpu_util,
        "ram_avail_gib": ram_avail,
        "ram_total_gib": ram_total,
        "ram_used_pct": ram_pct,
        "cpu_load_1m": cpu_load_1m,
        "cpu_count": cpu_count,
        "cpu_load_ratio": cpu_load_ratio,
        "cpu_usage_pct": cpu_usage_pct,
    }

    if not cfg.enabled:
        return {
            "adaptive_enabled": False,
            "effective_mode": "static",
            "degraded": False,
            "degrade_reasons": ["adaptive_disabled"],
            "hardware": hardware,
            "limits": {tier: 1 for tier in ALL_TIERS},
            "configured_max": configured_max,
            "active": dict(active_counts or {}),
            "starvation_age_seconds": cfg.starvation_age_seconds,
            "sticky_streak_max": cfg.sticky_streak_max,
            "vram_safety_margin_mib": cfg.vram_safety_margin_mib,
            "external_queue_depth": int(external_queue_depth or 0),
            "gpu_cooldown_active": bool(gpu_cooldown_active),
        }

    light_gpu_limit = configured_max[TIER_IMAGE_LIGHT]
    audio_gpu_limit = configured_max[TIER_AUDIO_GPU]
    voice_cpu_limit = configured_max[TIER_VOICE_CPU]
    reasons: list[str] = []

    # 1. GPU0 VRAM & utilization checks (only throttle GPU tiers; voice_cpu stays decoupled)
    if gpu_free is None:
        light_gpu_limit = 1
        audio_gpu_limit = 1
        reasons.append("gpu_telemetry_unavailable")
    else:
        if gpu_free < cfg.gpu_vram_min_free_mib:
            light_gpu_limit = 1
            audio_gpu_limit = 1
            reasons.append("low_vram")
        elif gpu_free < cfg.gpu_vram_warn_free_mib:
            light_gpu_limit = min(light_gpu_limit, 2)
            audio_gpu_limit = min(audio_gpu_limit, 2)
            reasons.append("moderate_vram")

    if gpu_util is not None:
        if float(gpu_util) >= cfg.gpu_util_critical_pct:
            light_gpu_limit = 1
            audio_gpu_limit = 1
            reasons.append("high_gpu_util")
        elif float(gpu_util) >= cfg.gpu_util_warn_pct:
            light_gpu_limit = min(light_gpu_limit, 2)
            audio_gpu_limit = min(audio_gpu_limit, 2)
            reasons.append("elevated_gpu_util")

    # 1b. External ComfyUI /queue depth & recent GPU failure/OOM cooldown
    ext_depth = max(0, int(external_queue_depth or 0))
    if ext_depth > 0:
        light_gpu_limit = 1
        audio_gpu_limit = 1
        reasons.append("external_comfy_queue_busy")
    if gpu_cooldown_active:
        light_gpu_limit = 1
        audio_gpu_limit = 1
        reasons.append(cooldown_reason or "recent_gpu_failure_cooldown")

    # 2. Host RAM MemAvailable checks (affect both GPU staging and CPU voice workers)
    effective_min_avail = cfg.ram_min_avail_gib
    effective_warn_avail = cfg.ram_warn_avail_gib
    if ram_total is not None and float(ram_total) > 0:
        effective_min_avail = min(cfg.ram_min_avail_gib, float(ram_total) * 0.25)
        effective_warn_avail = min(cfg.ram_warn_avail_gib, float(ram_total) * 0.40)

    if ram_avail is None and ram_pct is None:
        light_gpu_limit = 1
        audio_gpu_limit = 1
        voice_cpu_limit = 1
        reasons.append("ram_telemetry_unavailable")
    else:
        ram_critical = (
            (ram_avail is not None and float(ram_avail) < effective_min_avail)
            or (ram_pct is not None and float(ram_pct) >= cfg.ram_critical_used_pct)
        )
        ram_warn = (
            (ram_avail is not None and float(ram_avail) < effective_warn_avail)
            or (ram_pct is not None and float(ram_pct) >= cfg.ram_warn_used_pct)
        )
        if ram_critical:
            light_gpu_limit = 1
            audio_gpu_limit = 1
            voice_cpu_limit = 1
            reasons.append("high_ram_pressure")
        elif ram_warn:
            light_gpu_limit = min(light_gpu_limit, 2)
            audio_gpu_limit = min(audio_gpu_limit, 2)
            voice_cpu_limit = min(voice_cpu_limit, 2)
            reasons.append("elevated_ram_pressure")

    # 3. Host CPU loadavg / utilization checks
    cpu_critical = (
        (cpu_load_ratio is not None and cpu_load_ratio >= cfg.cpu_load_ratio_critical)
        or (cpu_usage_pct is not None and float(cpu_usage_pct) >= cfg.cpu_usage_critical_pct)
    )
    cpu_warn = (
        (cpu_load_ratio is not None and cpu_load_ratio >= cfg.cpu_load_ratio_warn)
        or (cpu_usage_pct is not None and float(cpu_usage_pct) >= cfg.cpu_usage_warn_pct)
    )
    cpu_moderate = (
        (cpu_load_ratio is not None and cpu_load_ratio >= cfg.cpu_load_ratio_moderate)
        or (cpu_usage_pct is not None and float(cpu_usage_pct) >= cfg.cpu_usage_moderate_pct)
    )
    if cpu_critical:
        light_gpu_limit = 1
        audio_gpu_limit = 1
        voice_cpu_limit = 1
        reasons.append("high_cpu_load")
    elif cpu_warn:
        light_gpu_limit = min(light_gpu_limit, 2)
        audio_gpu_limit = min(audio_gpu_limit, 2)
        voice_cpu_limit = min(voice_cpu_limit, 2)
        reasons.append("elevated_cpu_load")
    elif cpu_moderate:
        voice_cpu_limit = min(voice_cpu_limit, 3)
        reasons.append("moderate_cpu_load")

    limits = {
        TIER_VIDEO_HEAVY: 1,
        TIER_IMAGE_HEAVY: 1,
        TIER_IMAGE_LIGHT: max(1, light_gpu_limit),
        TIER_AUDIO_GPU: max(1, min(audio_gpu_limit, light_gpu_limit)),
        TIER_VOICE_CPU: max(1, voice_cpu_limit),
    }
    degraded = (
        limits[TIER_IMAGE_LIGHT] < configured_max[TIER_IMAGE_LIGHT]
        or limits[TIER_AUDIO_GPU] < configured_max[TIER_AUDIO_GPU]
        or limits[TIER_VOICE_CPU] < configured_max[TIER_VOICE_CPU]
    )
    result = {
        "adaptive_enabled": True,
        "effective_mode": "adaptive",
        "degraded": degraded,
        "degrade_reasons": list(dict.fromkeys(reasons)),
        "hardware": hardware,
        "limits": limits,
        "configured_max": configured_max,
        "active": dict(active_counts or {}),
        "starvation_age_seconds": cfg.starvation_age_seconds,
        "sticky_streak_max": cfg.sticky_streak_max,
        "vram_safety_margin_mib": cfg.vram_safety_margin_mib,
        "external_queue_depth": ext_depth,
        "gpu_cooldown_active": bool(gpu_cooldown_active),
    }
    if telemetry_feedback:
        result["telemetry_feedback"] = telemetry_feedback
    return result


def _percentile(sorted_values: list[float], pct: float) -> float | None:
    if not sorted_values:
        return None
    if len(sorted_values) == 1:
        return round(float(sorted_values[0]), 3)
    idx = math.ceil((float(pct) / 100.0) * len(sorted_values)) - 1
    idx = max(0, min(len(sorted_values) - 1, idx))
    return round(float(sorted_values[idx]), 3)


class WorkflowMetricsTracker:
    """Thread-safe sliding-window latency, peak VRAM, and outcome tracker per workflow."""

    def __init__(self, *, window_size: int = 100) -> None:
        self._window_size = max(1, int(window_size))
        self._lock = threading.Lock()
        self._entries: dict[str, dict[str, Any]] = {}

    def record(
        self,
        workflow_name: str,
        *,
        success: bool,
        duration_s: float | None = None,
        error: str = "",
        cancelled: bool = False,
        peak_vram_mib: int | None = None,
    ) -> None:
        name = str(workflow_name or "unknown").strip() or "unknown"
        now = time.time()
        with self._lock:
            entry = self._entries.get(name)
            if entry is None:
                entry = {
                    "durations": deque(maxlen=self._window_size),
                    "vram_peaks": deque(maxlen=self._window_size),
                    "total": 0,
                    "success": 0,
                    "failed": 0,
                    "cancelled": 0,
                    "oom": 0,
                    "last_duration_s": None,
                    "last_peak_vram_mib": None,
                    "last_updated_at": None,
                }
                self._entries[name] = entry

            entry["total"] += 1
            entry["last_updated_at"] = now
            if cancelled:
                entry["cancelled"] += 1
            elif success:
                entry["success"] += 1
            else:
                entry["failed"] += 1
                if _is_oom_or_cuda_error(error):
                    entry["oom"] += 1

            if not cancelled and duration_s is not None and math.isfinite(float(duration_s)) and float(duration_s) >= 0:
                val = round(float(duration_s), 3)
                entry["durations"].append(val)
                entry["last_duration_s"] = val

            if peak_vram_mib is not None and int(peak_vram_mib) > 0:
                vram_val = int(peak_vram_mib)
                entry["vram_peaks"].append(vram_val)
                entry["last_peak_vram_mib"] = vram_val

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            workflows: dict[str, Any] = {}
            summary = {"total": 0, "success": 0, "failed": 0, "cancelled": 0, "oom": 0}
            for wf, entry in sorted(self._entries.items()):
                durs = sorted(entry["durations"])
                vrams = list(entry["vram_peaks"])
                terminal_non_cancel = entry["success"] + entry["failed"]
                workflows[wf] = {
                    "tier": classify_workflow(wf).tier,
                    "total": entry["total"],
                    "success": entry["success"],
                    "failed": entry["failed"],
                    "cancelled": entry["cancelled"],
                    "oom": entry["oom"],
                    "success_rate": round(entry["success"] / terminal_non_cancel, 4)
                    if terminal_non_cancel > 0
                    else None,
                    "sample_count": len(durs),
                    "p50_duration_s": _percentile(durs, 50.0),
                    "p95_duration_s": _percentile(durs, 95.0),
                    "avg_duration_s": round(sum(durs) / len(durs), 3) if durs else None,
                    "last_duration_s": entry["last_duration_s"],
                    "peak_vram_mib": max(vrams) if vrams else None,
                    "last_peak_vram_mib": entry["last_peak_vram_mib"],
                    "last_updated_at": entry["last_updated_at"],
                    "_durations": list(entry["durations"]),
                    "_vram_peaks": vrams,
                }
                for key in summary:
                    summary[key] += entry[key]
        return {
            "window_size": self._window_size,
            "workflows": workflows,
            "summary": summary,
        }


def merge_workflow_metrics(*snapshots: dict[str, Any]) -> dict[str, Any]:
    """Merge one or more WorkflowMetricsTracker snapshots into one clean public view."""
    merged: dict[str, dict[str, Any]] = {}
    summary = {"total": 0, "success": 0, "failed": 0, "cancelled": 0, "oom": 0}
    for snap in snapshots:
        if not isinstance(snap, dict):
            continue
        workflows = snap.get("workflows")
        if not isinstance(workflows, dict):
            continue
        for wf, item in workflows.items():
            if not isinstance(item, dict):
                continue
            target = merged.setdefault(
                wf,
                {
                    "tier": item.get("tier") or classify_workflow(wf).tier,
                    "total": 0,
                    "success": 0,
                    "failed": 0,
                    "cancelled": 0,
                    "oom": 0,
                    "_durations": [],
                    "_vram_peaks": [],
                    "last_duration_s": None,
                    "last_peak_vram_mib": None,
                    "last_updated_at": None,
                },
            )
            for key in ("total", "success", "failed", "cancelled", "oom"):
                val = int(item.get(key) or 0)
                target[key] += val
                summary[key] += val
            if isinstance(item.get("_durations"), list):
                target["_durations"].extend(float(x) for x in item["_durations"])
            elif item.get("last_duration_s") is not None:
                target["_durations"].append(float(item["last_duration_s"]))
            if isinstance(item.get("_vram_peaks"), list):
                target["_vram_peaks"].extend(int(x) for x in item["_vram_peaks"])
            elif item.get("peak_vram_mib") is not None:
                target["_vram_peaks"].append(int(item["peak_vram_mib"]))
            item_ts = item.get("last_updated_at")
            if item_ts is not None and (target["last_updated_at"] is None or item_ts >= target["last_updated_at"]):
                target["last_updated_at"] = item_ts
                target["last_duration_s"] = item.get("last_duration_s")
                target["last_peak_vram_mib"] = item.get("last_peak_vram_mib")

    public_workflows: dict[str, Any] = {}
    for wf, item in sorted(merged.items()):
        durs = sorted(item.pop("_durations"))
        vrams = item.pop("_vram_peaks")
        terminal_non_cancel = item["success"] + item["failed"]
        item["success_rate"] = round(item["success"] / terminal_non_cancel, 4) if terminal_non_cancel > 0 else None
        item["sample_count"] = len(durs)
        item["p50_duration_s"] = _percentile(durs, 50.0)
        item["p95_duration_s"] = _percentile(durs, 95.0)
        item["avg_duration_s"] = round(sum(durs) / len(durs), 3) if durs else None
        item["peak_vram_mib"] = max(vrams) if vrams else None
        public_workflows[wf] = item
    return {"workflows": public_workflows, "summary": summary}


class AdaptiveConcurrencyController:
    """Thread-safe controller caching live hardware snapshots for task schedulers."""

    def __init__(
        self,
        *,
        probe_fn: Callable[[], dict[str, Any]] | None = None,
        config_provider: Callable[[], AdaptiveConfig] | None = None,
        cache_ttl: float = 1.5,
        comfy_queue_fn: Callable[[], dict[str, Any]] | None = None,
        known_prompt_ids_fn: Callable[[], set[str]] | None = None,
    ) -> None:
        self._probe_fn = probe_fn
        self._config_provider = config_provider or load_adaptive_config
        self._cache_ttl = max(0.0, float(cache_ttl))
        self._comfy_queue_fn = comfy_queue_fn
        self._known_prompt_ids_fn = known_prompt_ids_fn
        self._lock = threading.Lock()
        self._cached_at = 0.0
        self._cached_hw: dict[str, Any] | None = None
        self._cached_ext_at = 0.0
        self._cached_ext_depth = 0
        self._gpu_outcomes: deque[tuple[float, bool, bool]] = deque(maxlen=10)
        self._gpu_cooldown_until = 0.0
        self._cooldown_reason = ""
        self._ewma_duration: dict[str, float] = {}
        self._metrics = WorkflowMetricsTracker()

    def _default_probe(self) -> dict[str, Any]:
        import system_telemetry

        return {
            "cpu": system_telemetry.collect_cpu_stats(),
            "ram": system_telemetry.collect_ram_stats(),
            "gpus": system_telemetry.collect_gpu_stats(),
        }

    def _read_hardware(self, *, force_refresh: bool = False) -> dict[str, Any]:
        now = time.monotonic()
        with self._lock:
            if (
                not force_refresh
                and self._cached_hw is not None
                and self._cache_ttl > 0
                and (now - self._cached_at) < self._cache_ttl
            ):
                return self._cached_hw
        probe = self._probe_fn or self._default_probe
        hw = probe()
        with self._lock:
            self._cached_at = time.monotonic()
            self._cached_hw = hw
        return hw

    def _read_external_queue_depth(self, *, force_refresh: bool = False) -> int:
        if self._comfy_queue_fn is None:
            return 0
        now = time.monotonic()
        with self._lock:
            if not force_refresh and self._cache_ttl > 0 and (now - self._cached_ext_at) < self._cache_ttl:
                return self._cached_ext_depth
        depth = 0
        try:
            queue = self._comfy_queue_fn()
            if isinstance(queue, dict) and queue.get("_available", True):
                comfy_ids: set[str] = set()
                for key in ("queue_running", "queue_pending"):
                    for row in queue.get(key) or []:
                        if isinstance(row, (list, tuple)) and len(row) > 1 and row[1]:
                            comfy_ids.add(str(row[1]))
                        elif isinstance(row, dict):
                            pid = row.get("prompt_id") or row.get("id")
                            if pid:
                                comfy_ids.add(str(pid))
                known = set(self._known_prompt_ids_fn()) if self._known_prompt_ids_fn is not None else set()
                depth = len(comfy_ids - known)
        except Exception:
            depth = 0
        with self._lock:
            self._cached_ext_at = time.monotonic()
            self._cached_ext_depth = depth
        return depth

    def record_task_outcome(
        self,
        workflow_name: str,
        *,
        success: bool,
        duration_s: float | None = None,
        error: str = "",
        cancelled: bool = False,
        peak_vram_mib: int | None = None,
    ) -> None:
        self._metrics.record(
            workflow_name,
            success=success,
            duration_s=duration_s,
            error=error,
            cancelled=cancelled,
            peak_vram_mib=peak_vram_mib,
        )
        profile = classify_workflow(workflow_name)
        cfg = self.config()
        now = time.monotonic()
        with self._lock:
            if duration_s is not None and duration_s >= 0:
                prev = self._ewma_duration.get(profile.tier)
                self._ewma_duration[profile.tier] = (
                    float(duration_s) if prev is None else round(0.7 * prev + 0.3 * float(duration_s), 2)
                )
            if not profile.uses_gpu or cancelled:
                return
            is_oom = (not success) and _is_oom_or_cuda_error(error)
            self._gpu_outcomes.append((now, bool(success), is_oom))
            if not success:
                recent = list(self._gpu_outcomes)
                consec_fail = 0
                for _, ok, _ in reversed(recent):
                    if ok:
                        break
                    consec_fail += 1
                fail_count = sum(1 for _, ok, _ in recent if not ok)
                if is_oom or consec_fail >= 2 or (len(recent) >= 3 and fail_count / len(recent) >= 0.5):
                    self._gpu_cooldown_until = max(
                        self._gpu_cooldown_until, now + cfg.failure_cooldown_seconds
                    )
                    self._cooldown_reason = (
                        "recent_gpu_oom_cooldown" if is_oom else "recent_gpu_failure_cooldown"
                    )
            elif now >= self._gpu_cooldown_until:
                self._cooldown_reason = ""

    def clear_cooldown(self) -> None:
        with self._lock:
            self._gpu_outcomes.clear()
            self._gpu_cooldown_until = 0.0
            self._cooldown_reason = ""

    def metrics_snapshot(self) -> dict[str, Any]:
        return self._metrics.snapshot()

    def sample_gpu0_vram_mib(self) -> int | None:
        try:
            gpus = None
            if self._probe_fn is not None:
                hw = self._probe_fn()
                if isinstance(hw, dict):
                    gpus = hw.get("gpus")
            else:
                import system_telemetry

                gpus = system_telemetry._collect_gpu_stats_pynvml()
                if gpus is None:
                    with self._lock:
                        cached = self._cached_hw
                    if isinstance(cached, dict):
                        gpus = cached.get("gpus")
            gpu0 = _select_gpu0(gpus)
            if isinstance(gpu0, dict) and gpu0.get("vram_used_mib") is not None:
                return int(gpu0["vram_used_mib"])
        except Exception:
            pass
        return None

    def config(self) -> AdaptiveConfig:
        return self._config_provider()

    def worker_pool_size(self) -> int:
        cfg = self.config()
        if not cfg.enabled:
            return 1
        return max(2, cfg.light_gpu_max + cfg.voice_cpu_max)

    def evaluate(
        self,
        *,
        active_counts: dict[str, int] | None = None,
        force_refresh: bool = False,
    ) -> dict[str, Any]:
        cfg = self.config()
        hw = self._read_hardware(force_refresh=force_refresh)
        ext_depth = self._read_external_queue_depth(force_refresh=force_refresh)
        now = time.monotonic()
        with self._lock:
            cooldown_active = now < self._gpu_cooldown_until
            cooldown_reason = self._cooldown_reason if cooldown_active else ""
            feedback = {
                "ewma_seconds": dict(self._ewma_duration),
                "cooldown_remaining_s": round(max(0.0, self._gpu_cooldown_until - now), 1) if cooldown_active else 0.0,
            }
        return evaluate_concurrency(
            cpu=hw.get("cpu"),
            ram=hw.get("ram"),
            gpus=hw.get("gpus"),
            config=cfg,
            active_counts=active_counts,
            external_queue_depth=ext_depth,
            gpu_cooldown_active=cooldown_active,
            cooldown_reason=cooldown_reason,
            telemetry_feedback=feedback,
        )


class AdaptiveSlotGate:
    """Dynamic concurrency gate replacing fixed threading.Semaphore(1) for CPU voice."""

    def __init__(
        self,
        limit_fn: Callable[[], int],
        *,
        poll_interval: float = 0.25,
    ) -> None:
        self._limit_fn = limit_fn
        self._poll_interval = max(0.05, float(poll_interval))
        self._condition = threading.Condition()
        self._active = 0

    def active_count(self) -> int:
        with self._condition:
            return self._active

    def current_limit(self) -> int:
        try:
            return max(1, int(self._limit_fn()))
        except Exception:
            return 1

    def available_slots(self) -> int:
        with self._condition:
            return max(0, self.current_limit() - self._active)

    def try_acquire(self) -> bool:
        with self._condition:
            if self._active < self.current_limit():
                self._active += 1
                return True
            return False

    def acquire(self, cancel_check: Callable[[], bool] | None = None) -> bool:
        with self._condition:
            while True:
                if cancel_check is not None and cancel_check():
                    return False
                if self._active < self.current_limit():
                    self._active += 1
                    return True
                self._condition.wait(timeout=self._poll_interval)

    def release(self) -> None:
        with self._condition:
            if self._active > 0:
                self._active -= 1
            self._condition.notify_all()


_WIN_SLOT_GUARD = threading.Lock()
_WIN_SLOT_LOCKS: dict[tuple[str, int], threading.Lock] = {}


class CrossProcessSlotGate:
    """Cross-process slot gate backed by flock slot files (with Windows fallback).

    Allows ``webui.py`` and ``lan_api.py`` (separate OS processes) to share one
    unified ``voice_cpu`` concurrency budget without stale lock leaks on crash.
    """

    def __init__(
        self,
        limit_fn: Callable[[], int],
        *,
        slot_dir: Path | str | None = None,
        max_slots: int = 8,
        poll_interval: float = 0.15,
    ) -> None:
        self._limit_fn = limit_fn
        self._slot_dir_override = Path(slot_dir) if slot_dir is not None else None
        self._max_slots = max(1, int(max_slots))
        self._poll_interval = max(0.03, float(poll_interval))
        self._condition = threading.Condition()
        self._held: list[tuple[int, int, Any]] = []  # (thread_id, slot_idx, handle_or_lock)

    def _slot_dir(self) -> Path:
        if self._slot_dir_override is not None:
            return self._slot_dir_override
        env_dir = os.environ.get("BRM_ZONOS3_SLOTS_DIR", "").strip()
        if env_dir:
            return Path(env_dir)
        if os.name == "nt":
            return Path(os.environ.get("TEMP", ".")) / "brmmedia-zonos3-slots"
        return Path("/var/lib/brmmedia/zonos3-slots")

    def current_limit(self) -> int:
        try:
            return max(1, min(self._max_slots, int(self._limit_fn())))
        except Exception:
            return 1

    def _try_lock_slot(self, slot_dir: Path, idx: int) -> Any | None:
        slot_path = slot_dir / f"slot-{idx}.lock"
        try:
            import fcntl

            slot_dir.mkdir(parents=True, exist_ok=True)
            fh = open(slot_path, "a+", encoding="utf-8")
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return fh
            except (BlockingIOError, OSError):
                fh.close()
                return None
        except ImportError:
            try:
                resolved = str(slot_dir.resolve())
            except OSError:
                resolved = str(slot_dir)
            with _WIN_SLOT_GUARD:
                lock = _WIN_SLOT_LOCKS.setdefault((resolved, idx), threading.Lock())
            if lock.acquire(blocking=False):
                return lock
            return None
        except OSError:
            return None

    def _unlock_handle(self, handle: Any) -> None:
        try:
            import fcntl

            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()
        except ImportError:
            try:
                handle.release()
            except RuntimeError:
                pass
        except OSError:
            pass

    def _try_acquire_locked(self) -> bool:
        limit = self.current_limit()
        slot_dir = self._slot_dir()
        local_busy = {idx for _, idx, _ in self._held}
        for idx in range(limit):
            if idx in local_busy:
                continue
            handle = self._try_lock_slot(slot_dir, idx)
            if handle is not None:
                self._held.append((threading.get_ident(), idx, handle))
                return True
        return False

    def try_acquire(self) -> bool:
        with self._condition:
            return self._try_acquire_locked()

    def acquire(self, cancel_check: Callable[[], bool] | None = None, timeout: float | None = None) -> bool:
        deadline = (time.monotonic() + float(timeout)) if timeout is not None else None
        with self._condition:
            while True:
                if cancel_check is not None and cancel_check():
                    return False
                if self._try_acquire_locked():
                    return True
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return False
                    wait_time = min(self._poll_interval, remaining)
                else:
                    wait_time = self._poll_interval
                self._condition.wait(timeout=wait_time)

    def release(self) -> None:
        tid = threading.get_ident()
        handle = None
        with self._condition:
            for pos in range(len(self._held) - 1, -1, -1):
                if self._held[pos][0] == tid:
                    _, _, handle = self._held.pop(pos)
                    break
            if handle is None and self._held:
                _, _, handle = self._held.pop()
            if handle is not None:
                self._unlock_handle(handle)
            self._condition.notify_all()

    def local_active_count(self) -> int:
        with self._condition:
            return len(self._held)

    def active_count(self) -> int:
        with self._condition:
            local_busy = {idx for _, idx, _ in self._held}
            count = len(local_busy)
            slot_dir = self._slot_dir()
            limit = self.current_limit()
            for idx in range(limit):
                if idx in local_busy:
                    continue
                handle = self._try_lock_slot(slot_dir, idx)
                if handle is None:
                    count += 1
                else:
                    self._unlock_handle(handle)
            return count

    def available_slots(self) -> int:
        return max(0, self.current_limit() - self.active_count())
