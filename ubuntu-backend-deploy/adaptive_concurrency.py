# -*- coding: utf-8 -*-
"""Dynamic hardware- and workload-aware concurrency controller for BRMMedia.

Classifies workflows by GPU0/RAM/CPU footprint and computes safe real-time
concurrency limits from live host telemetry (GPU0 VRAM free & utilization,
host MemAvailable, CPU loadavg & utilization) with anti-starvation support.
"""

from __future__ import annotations

import json
import os
import threading
import time
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
    # Pure CPU voice workflow (Zonos3-mini :1919): zero GPU0 VRAM, decoupled 1..4
    "Zonos3-语音克隆": TaskProfile(TIER_VOICE_CPU, False, False, False, 10),
    "zonos3-voice-clone": TaskProfile(TIER_VOICE_CPU, False, False, False, 10),
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
    if "zonos3" in lower:
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
    )


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
    return {
        "adaptive_enabled": True,
        "effective_mode": "adaptive",
        "degraded": degraded,
        "degrade_reasons": list(dict.fromkeys(reasons)),
        "hardware": hardware,
        "limits": limits,
        "configured_max": configured_max,
        "active": dict(active_counts or {}),
    }


class AdaptiveConcurrencyController:
    """Thread-safe controller caching live hardware snapshots for task schedulers."""

    def __init__(
        self,
        *,
        probe_fn: Callable[[], dict[str, Any]] | None = None,
        config_provider: Callable[[], AdaptiveConfig] | None = None,
        cache_ttl: float = 1.5,
    ) -> None:
        self._probe_fn = probe_fn
        self._config_provider = config_provider or load_adaptive_config
        self._cache_ttl = max(0.0, float(cache_ttl))
        self._lock = threading.Lock()
        self._cached_at = 0.0
        self._cached_hw: dict[str, Any] | None = None

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
        return evaluate_concurrency(
            cpu=hw.get("cpu"),
            ram=hw.get("ram"),
            gpus=hw.get("gpus"),
            config=cfg,
            active_counts=active_counts,
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
