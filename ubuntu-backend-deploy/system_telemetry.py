# -*- coding: utf-8 -*-
"""Host telemetry collector for CPU, RAM, multi-GPU, core services, and tiered storage."""

from __future__ import annotations

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows local test fallback
    fcntl = None
import html
import os
import re
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import requests

import adaptive_concurrency

DEFAULT_MEDIA_GPU_LOCK = "/var/lib/brmmedia/media-gpu.lock"

CORE_SERVICE_SPECS: tuple[tuple[str, str, int, str], ...] = (
    ("strata", "Strata LLM", 8002, "http://127.0.0.1:8002/health"),
    ("zonos3", "ZONOS3-Mini", 1919, "http://127.0.0.1:1919/v2/health"),
    ("comfyui", "ComfyUI", 8188, "http://127.0.0.1:8188/system_stats"),
    ("lan_api", "LAN API", 9100, "http://127.0.0.1:9100/api/v1/health"),
    ("indextts25", "IndexTTS-2.5", 9205, "http://127.0.0.1:9205/health"),
    ("breeze_tts2", "Breeze-TTS2", 9206, "http://127.0.0.1:9206/health"),
    ("qwen_gateway", "Qwen 网关", 9300, "http://127.0.0.1:9300/health"),
)

SERVICE_SHORT_NAMES: dict[str, str] = {
    "strata": "Strata",
    "zonos3": "Zonos3",
    "comfyui": "Comfy",
    "lan_api": "API",
    "indextts25": "IndexTTS",
    "breeze_tts2": "Breeze",
    "qwen_gateway": "网关",
}

_cpu_lock = threading.Lock()
_prev_cpu_sample: tuple[int, int] | None = None
_cpu_model_cache: str | None = None

_telemetry_lock = threading.Lock()
_telemetry_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def _read_proc_stat_totals() -> tuple[int, int] | None:
    try:
        with open("/proc/stat", "r", encoding="utf-8") as fh:
            first = fh.readline().strip()
    except OSError:
        return None
    if not first.startswith("cpu "):
        return None
    parts = first.split()[1:]
    try:
        values = [int(item) for item in parts]
    except ValueError:
        return None
    if len(values) < 4:
        return None
    idle = values[3] + (values[4] if len(values) > 4 else 0)
    total = sum(values)
    return total, idle


def _detect_cpu_label() -> str:
    global _cpu_model_cache
    if _cpu_model_cache is not None:
        return _cpu_model_cache
    cores = os.cpu_count() or 0
    model = ""
    try:
        with open("/proc/cpuinfo", "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if line.lower().startswith("model name"):
                    model = line.split(":", 1)[1].strip()
                    break
    except OSError:
        pass
    if "5975WX" in model:
        label = "AMD 5975WX (32C64T)"
    elif model:
        compact = re.sub(r"\s+", " ", model).replace("(R)", "").replace("(TM)", "").strip()
        label = f"{compact} ({cores}T)" if cores else compact
    else:
        label = f"CPU ({cores}T)" if cores else "CPU"
    _cpu_model_cache = label
    return label


def _read_cpu_temp_c() -> float | None:
    hwmon_root = Path("/sys/class/hwmon")
    if not hwmon_root.is_dir():
        return None
    preferred_drivers = ("k10temp", "zenpower", "coretemp")
    candidates: list[tuple[int, int, Path]] = []
    try:
        for dev in sorted(hwmon_root.iterdir()):
            name_file = dev / "name"
            if not name_file.is_file():
                continue
            driver = name_file.read_text(encoding="utf-8", errors="ignore").strip().lower()
            if driver not in preferred_drivers:
                continue
            driver_rank = preferred_drivers.index(driver)
            for temp_input in sorted(dev.glob("temp*_input")):
                label_file = dev / temp_input.name.replace("_input", "_label")
                label = label_file.read_text(encoding="utf-8", errors="ignore").strip().lower() if label_file.is_file() else ""
                label_rank = 0 if label in {"tctl", "tdie", "package id 0"} else 1
                candidates.append((driver_rank, label_rank, temp_input))
        if not candidates:
            return None
        candidates.sort(key=lambda item: (item[0], item[1], item[2].name))
        raw = candidates[0][2].read_text(encoding="utf-8", errors="ignore").strip()
        milli = float(raw)
        return round(milli / 1000.0, 1) if milli > 200 else round(milli, 1)
    except (OSError, ValueError):
        return None


def collect_cpu_stats() -> dict[str, Any]:
    global _prev_cpu_sample
    usage_pct: float | None = None
    with _cpu_lock:
        sample = _read_proc_stat_totals()
        if sample is not None:
            if _prev_cpu_sample is None:
                time.sleep(0.1)
                sample2 = _read_proc_stat_totals()
                if sample2 is not None:
                    dt = sample2[0] - sample[0]
                    di = sample2[1] - sample[1]
                    if dt > 0:
                        usage_pct = round(max(0.0, min(100.0, (dt - di) * 100.0 / dt)), 1)
                    _prev_cpu_sample = sample2
                else:
                    _prev_cpu_sample = sample
            else:
                dt = sample[0] - _prev_cpu_sample[0]
                di = sample[1] - _prev_cpu_sample[1]
                if dt > 0:
                    usage_pct = round(max(0.0, min(100.0, (dt - di) * 100.0 / dt)), 1)
                _prev_cpu_sample = sample

    load1 = load5 = load15 = None
    try:
        l1, l5, l15 = os.getloadavg()
        load1, load5, load15 = round(l1, 2), round(l5, 2), round(l15, 2)
    except (OSError, AttributeError):
        pass

    return {
        "label": _detect_cpu_label(),
        "cpu_count": os.cpu_count() or 1,
        "usage_pct": usage_pct,
        "load_1m": load1,
        "load_5m": load5,
        "load_15m": load15,
        "temp_c": _read_cpu_temp_c(),
    }


def collect_ram_stats() -> dict[str, Any]:
    info: dict[str, int] = {}
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as fh:
            for line in fh:
                if ":" not in line:
                    continue
                key, rest = line.split(":", 1)
                val_parts = rest.strip().split()
                if val_parts and val_parts[0].isdigit():
                    info[key] = int(val_parts[0]) * 1024
    except OSError:
        return {"used_gib": None, "avail_gib": None, "total_gib": None, "percent": None}

    total = info.get("MemTotal")
    if not total:
        return {"used_gib": None, "avail_gib": None, "total_gib": None, "percent": None}
    avail = info.get("MemAvailable")
    if avail is None:
        avail = info.get("MemFree", 0) + info.get("Buffers", 0) + info.get("Cached", 0)
    used = max(0, total - avail)
    return {
        "used_gib": round(used / (1024 ** 3), 1),
        "avail_gib": round(avail / (1024 ** 3), 1),
        "total_gib": round(total / (1024 ** 3), 1),
        "percent": round(used * 100.0 / total, 1),
    }


def _parse_float(value: str) -> float | None:
    cleaned = (value or "").strip()
    if not cleaned or cleaned.upper() in {"N/A", "[N/A]", "NONE", "ERR"}:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _parse_int(value: str) -> int | None:
    parsed = _parse_float(value)
    return int(round(parsed)) if parsed is not None else None


_nvml_lock = threading.Lock()
_nvml_initialized = False


def _format_gpu_entry(
    idx: int,
    raw_name: str,
    util_pct: float | None,
    vram_used: int | None,
    vram_total: int | None,
    temp_c: float | None,
    power_w: float | None,
    a4000_counter: int,
) -> tuple[dict[str, Any], int]:
    upper_name = raw_name.upper()
    if idx == 0 and "A5000" in upper_name:
        display_name = "GPU0 · RTX A5000 (媒体主力)"
        role = "media"
    elif "A4000" in upper_name:
        a4000_counter += 1
        display_name = f"GPU{idx} · RTX A4000 #{a4000_counter} (LLM并发)"
        role = "llm"
    else:
        short_name = raw_name.replace("NVIDIA ", "").strip()
        display_name = f"GPU{idx} · {short_name}"
        role = "gpu"
    vram_pct = (
        round(vram_used * 100.0 / vram_total, 1)
        if vram_used is not None and vram_total and vram_total > 0
        else None
    )
    entry = {
        "index": idx,
        "name": raw_name,
        "display_name": display_name,
        "role": role,
        "util_pct": util_pct,
        "vram_used_mib": vram_used,
        "vram_total_mib": vram_total,
        "vram_pct": vram_pct,
        "temp_c": temp_c,
        "power_w": power_w,
    }
    return entry, a4000_counter


def _collect_gpu_stats_pynvml() -> list[dict[str, Any]] | None:
    global _nvml_initialized
    try:
        import pynvml  # type: ignore
    except ImportError:
        return None
    with _nvml_lock:
        try:
            if not _nvml_initialized:
                pynvml.nvmlInit()
                _nvml_initialized = True
            count = int(pynvml.nvmlDeviceGetCount())
            gpus: list[dict[str, Any]] = []
            a4000_counter = 0
            for idx in range(count):
                handle = pynvml.nvmlDeviceGetHandleByIndex(idx)
                raw_name = pynvml.nvmlDeviceGetName(handle)
                if isinstance(raw_name, bytes):
                    raw_name = raw_name.decode("utf-8", errors="replace")
                raw_name = str(raw_name).strip()
                try:
                    mem = pynvml.nvmlDeviceGetMemoryInfo(handle)
                    vram_used = int(mem.used // (1024 * 1024))
                    vram_total = int(mem.total // (1024 * 1024))
                except Exception:
                    vram_used, vram_total = None, None
                try:
                    util = pynvml.nvmlDeviceGetUtilizationRates(handle)
                    util_pct = float(util.gpu)
                except Exception:
                    util_pct = None
                try:
                    temp_c = float(pynvml.nvmlDeviceGetTemperature(handle, pynvml.NVML_TEMPERATURE_GPU))
                except Exception:
                    temp_c = None
                try:
                    power_w = round(float(pynvml.nvmlDeviceGetPowerUsage(handle)) / 1000.0, 2)
                except Exception:
                    power_w = None
                entry, a4000_counter = _format_gpu_entry(
                    idx, raw_name, util_pct, vram_used, vram_total, temp_c, power_w, a4000_counter
                )
                gpus.append(entry)
            return gpus if gpus else None
        except Exception:
            _nvml_initialized = False
            return None


def collect_gpu_stats() -> list[dict[str, Any]]:
    nvml_gpus = _collect_gpu_stats_pynvml()
    if nvml_gpus is not None:
        return nvml_gpus

    nvidia_smi = shutil.which("nvidia-smi") or "/usr/lib/wsl/lib/nvidia-smi"
    try:
        smi = subprocess.run(
            [
                nvidia_smi,
                "--query-gpu=index,name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=2.5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if smi.returncode != 0 or not smi.stdout.strip():
        return []

    gpus: list[dict[str, Any]] = []
    a4000_counter = 0
    for line in smi.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = [item.strip() for item in line.split(",")]
        if len(parts) < 7:
            continue
        idx = _parse_int(parts[0])
        if idx is None:
            idx = len(gpus)
        raw_name = parts[1]
        entry, a4000_counter = _format_gpu_entry(
            idx,
            raw_name,
            _parse_float(parts[2]),
            _parse_int(parts[3]),
            _parse_int(parts[4]),
            _parse_float(parts[5]),
            _parse_float(parts[6]),
            a4000_counter,
        )
        gpus.append(entry)
    return gpus


def _probe_single_service(spec: tuple[str, str, int, str], self_service: str | None) -> dict[str, Any]:
    key, label, port, url = spec
    if self_service == key:
        return {"key": key, "label": label, "port": port, "online": True}
    online = False
    try:
        with requests.Session() as session:
            session.trust_env = False
            resp = session.get(url, timeout=0.6)
            online = resp.status_code == 200
    except requests.RequestException:
        online = False
    return {"key": key, "label": label, "port": port, "online": online}


def collect_services_status(*, self_service: str | None = None) -> dict[str, Any]:
    with ThreadPoolExecutor(max_workers=len(CORE_SERVICE_SPECS)) as pool:
        items = list(pool.map(lambda spec: _probe_single_service(spec, self_service), CORE_SERVICE_SPECS))
    online_count = sum(1 for item in items if item["online"])
    return {
        "online": online_count,
        "total": len(items),
        "items": items,
    }


def _comfy_queue_busy(base_url: str) -> bool:
    try:
        with requests.Session() as session:
            session.trust_env = False
            resp = session.get(f"{base_url.rstrip('/')}/queue", timeout=0.4)
            if resp.status_code != 200:
                return False
            data = resp.json()
            return bool(data.get("queue_running"))
    except (requests.RequestException, ValueError):
        return False


def is_media_gpu_lock_held() -> bool:
    if fcntl is None:
        return False
    lock_path = Path(
        os.environ.get("BRM_MEDIA_GPU_LOCK_PATH")
        or os.environ.get("BRM_MEDIA_GPU_LOCK")
        or DEFAULT_MEDIA_GPU_LOCK
    )
    try:
        if not lock_path.exists():
            return False
        with open(lock_path, "r+", encoding="utf-8") as fh:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
                return False
            except BlockingIOError:
                return True
    except OSError:
        return False


def _workflow_engine_status(workflow_name: str) -> str:
    wf = (workflow_name or "").lower()
    if "qwen_image21" in wf or "qwen-image" in wf:
        return "Qwen-Image-2.1 推理中"
    if "zonos3" in wf:
        return "ZONOS3 合成中"
    if "voice" in wf or "indextts" in wf or "qwen3_tts" in wf:
        return "语音克隆合成中"
    if "h3_image" in wf:
        return "H3-Image 推理中"
    if "flux3" in wf:
        return "FLUX 3 推理中"
    if "flux2" in wf:
        return "FLUX.2 Klein 推理中"
    if "seedvr2" in wf:
        return "SeedVR2 超分推理中"
    if "music" in wf or "ace" in wf:
        return "ACE-Step 音乐生成中"
    if "h3" in wf or "ltx" in wf or "video" in wf or "视频" in wf:
        return "ComfyUI 视频推理中"
    return "ComfyUI 推理中"


def detect_active_engine(running_workflows: list[str] | None = None) -> dict[str, Any]:
    if running_workflows:
        labels = list(dict.fromkeys(_workflow_engine_status(wf) for wf in running_workflows))
        lock_held = is_media_gpu_lock_held()
        return {
            "busy": True,
            "gpu_lock_held": lock_held,
            "label": " + ".join(labels),
        }
    qwen_url = os.environ.get("BRM_QWEN_IMAGE21_URL", "http://127.0.0.1:8188")
    comfy_url = os.environ.get("COMFYUI_BASE_URL", "http://127.0.0.1:8188")
    if qwen_url.rstrip("/") != comfy_url.rstrip("/") and _comfy_queue_busy(qwen_url):
        return {"busy": True, "gpu_lock_held": True, "label": "Qwen-Image-2.1 推理中"}
    if _comfy_queue_busy(comfy_url):
        return {"busy": True, "gpu_lock_held": True, "label": "ComfyUI 推理中"}
    if is_media_gpu_lock_held():
        return {"busy": True, "gpu_lock_held": True, "label": "GPU0 互斥锁占用中"}
    return {"busy": False, "gpu_lock_held": False, "label": "空闲"}


def format_storage_size(num_bytes: int | float) -> str:
    if num_bytes >= 1024 ** 4:
        return f"{num_bytes / (1024 ** 4):.2f} TB"
    gib = num_bytes / (1024 ** 3)
    if gib >= 10:
        return f"{gib:.0f} GB"
    return f"{gib:.1f} GB"


def _stat_storage_tier(key: str, label: str, mount_hint: str, target: Path) -> dict[str, Any]:
    try:
        resolved = target.resolve(strict=False)
        probe_path = resolved
        while not probe_path.exists() and probe_path.parent != probe_path:
            probe_path = probe_path.parent
        st = os.statvfs(probe_path)
        total = int(st.f_blocks * st.f_frsize)
        free = int(st.f_bfree * st.f_frsize)
        avail = int(st.f_bavail * st.f_frsize)
        used = max(0, total - free)
        denom = used + avail
        pct = round(used * 100.0 / denom, 1) if denom > 0 else 0.0
        pct_text = f"{pct:.1f}%" if pct < 10 else f"{pct:.0f}%"
        return {
            "key": key,
            "label": label,
            "mount": mount_hint,
            "path": str(resolved),
            "available": True,
            "used_bytes": used,
            "total_bytes": total,
            "avail_bytes": avail,
            "used_pct": pct,
            "summary": f"已用 {format_storage_size(used)} / {format_storage_size(total)}（可用 {format_storage_size(avail)} · {pct_text}）",
        }
    except (OSError, AttributeError):
        return {
            "key": key,
            "label": label,
            "mount": mount_hint,
            "path": str(target),
            "available": False,
            "used_bytes": 0,
            "total_bytes": 0,
            "avail_bytes": 0,
            "used_pct": 0.0,
            "summary": "暂不可用",
        }


def collect_storage_tiers(output_dir: Path | str) -> list[dict[str, Any]]:
    out_path = Path(output_dir)
    resolved_out = out_path.expanduser().resolve(strict=False)
    if str(resolved_out).startswith("/mnt/d"):
        output_mount = "/mnt/d"
        output_label = "8T 素材输出盘 (/mnt/d)"
    else:
        output_mount = str(resolved_out)
        output_label = f"素材输出目录 ({output_mount})"
    tiers = [
        _stat_storage_tier("outputs", output_label, output_mount, resolved_out),
    ]
    srv_path = Path("/srv")
    if srv_path.exists():
        tiers.append(_stat_storage_tier("srv", "NVMe 模型高速盘 (/srv)", "/srv", srv_path))
    root_path = Path("/")
    tiers.append(_stat_storage_tier("root", "NVMe 系统盘 (/)", "/", root_path))
    return tiers


def collect_host_telemetry(
    output_dir: Path | str,
    *,
    running_workflows: list[str] | None = None,
    cache_ttl: float = 3.0,
    self_service: str | None = None,
) -> dict[str, Any]:
    cache_key = f"{Path(output_dir).resolve(strict=False)}|{self_service or ''}"
    now = time.monotonic()
    with _telemetry_lock:
        cached = _telemetry_cache.get(cache_key)
        if cached and now - cached[0] < cache_ttl:
            data = dict(cached[1])
            data["engine"] = detect_active_engine(running_workflows)
            return data

    cpu = collect_cpu_stats()
    ram = collect_ram_stats()
    gpus = collect_gpu_stats()
    concurrency = adaptive_concurrency.evaluate_concurrency(
        cpu=cpu,
        ram=ram,
        gpus=gpus,
    )
    payload = {
        "cpu": cpu,
        "ram": ram,
        "gpus": gpus,
        "services": collect_services_status(self_service=self_service),
        "storage": collect_storage_tiers(output_dir),
        "engine": detect_active_engine(running_workflows),
        "concurrency": concurrency,
    }
    with _telemetry_lock:
        _telemetry_cache[cache_key] = (now, payload)
    return payload


def _bar_class(pct: float | None) -> str:
    if pct is None:
        return ""
    if pct >= 88:
        return "is-hot"
    if pct >= 70:
        return "is-warn"
    return "is-ok"


def _render_storage_tier_html(tier: dict[str, Any]) -> str:
    esc = html.escape
    t_label = esc(str(tier.get("label") or tier.get("mount") or "存储"))
    t_summary = esc(str(tier.get("summary") or "暂不可用"))
    t_pct = tier.get("used_pct") if tier.get("available") else None
    t_w = max(0.0, min(100.0, float(t_pct or 0.0)))
    return (
        '<div class="brm-sys-metric">'
        f'<div class="brm-sys-row"><span class="brm-sys-label">{t_label}</span>'
        f'<span class="brm-sys-sub">{t_pct:.1f}%</span></div>' if t_pct is not None else
        '<div class="brm-sys-metric">'
        f'<div class="brm-sys-row"><span class="brm-sys-label">{t_label}</span></div>'
    ) + (
        f'<div class="brm-sys-row"><span class="brm-sys-val brm-sys-val-left">{t_summary}</span></div>'
        f'<div class="brm-sys-bar {_bar_class(t_pct)}"><i style="width:{t_w:.1f}%"></i></div>'
        '</div>'
    )


def _gpu_vram_bar_class(v_pct: float | None, role: str | None) -> str:
    if v_pct is None:
        return ""
    if role == "llm":
        if v_pct >= 98:
            return "is-hot"
        return "is-ok"
    return _bar_class(v_pct)


def render_system_status_html(stats: dict[str, Any], *, default_concurrency: int = 1) -> str:
    """Render compact telemetry card with secondary hardware/port details collapsed by default."""
    esc = html.escape
    cpu = stats.get("cpu") if isinstance(stats.get("cpu"), dict) else {}
    ram = stats.get("ram") if isinstance(stats.get("ram"), dict) else {}
    gpus = stats.get("gpus") if isinstance(stats.get("gpus"), list) else []
    services = stats.get("services") if isinstance(stats.get("services"), dict) else {}
    storage_tiers = stats.get("storage_tiers") if isinstance(stats.get("storage_tiers"), list) else []
    engine = stats.get("engine") if isinstance(stats.get("engine"), dict) else {}
    q_stats = stats.get("queue_stats") if isinstance(stats.get("queue_stats"), dict) else {}
    t_stats = stats.get("task_stats") if isinstance(stats.get("task_stats"), dict) else {}

    srv_online = int(services.get("online", 0))
    srv_total = int(services.get("total", 0))
    srv_badge_cls = "brm-sys-badge" if srv_total and srv_online == srv_total else "brm-sys-badge is-warn"
    srv_badge_text = f"{srv_online}/{srv_total} 核心服务在线" if srv_total else "服务状态未知"

    cpu_pct = cpu.get("usage_pct")
    cpu_pct_text = f"{cpu_pct:.1f}%" if cpu_pct is not None else "—"
    cpu_temp = f" · {cpu['temp_c']:.1f}°C" if cpu.get("temp_c") is not None else ""
    cpu_raw_label = str(cpu.get("label") or "CPU")
    cpu_short_label = "CPU (5975WX)" if "5975WX" in cpu_raw_label else cpu_raw_label
    load1, load5, load15 = cpu.get("load_1m"), cpu.get("load_5m"), cpu.get("load_15m")
    if load1 is not None and load5 is not None and load15 is not None:
        load_text = f"{load1:.2f} / {load5:.2f} / {load15:.2f}"
    elif load1 is not None and load5 is not None:
        load_text = f"{load1:.2f} / {load5:.2f}"
    else:
        load_text = "—"
    cpu_bar_w = max(0.0, min(100.0, float(cpu_pct or 0.0)))

    ram_pct = ram.get("percent")
    if ram.get("used_gib") is not None and ram.get("total_gib") is not None and ram_pct is not None:
        ram_text = f"{ram['used_gib']:.1f}/{ram['total_gib']:.1f}G ({ram_pct:.0f}%)"
    else:
        ram_text = "暂不可用"
    ram_bar_w = max(0.0, min(100.0, float(ram_pct or 0.0)))

    gpu_blocks = []
    for gpu in gpus:
        name = esc(str(gpu.get("display_name") or gpu.get("name") or "GPU"))
        role = str(gpu.get("role") or "")
        util = gpu.get("util_pct")
        util_val = max(0.0, min(100.0, float(util or 0.0)))
        util_text = f"算力 {util:.0f}%" if util is not None else "算力 —"
        temp = f"{gpu['temp_c']:.0f}°C" if gpu.get("temp_c") is not None else "—"
        power = f"{gpu['power_w']:.0f}W" if gpu.get("power_w") is not None else "—"
        v_used = gpu.get("vram_used_mib")
        v_tot = gpu.get("vram_total_mib")
        v_pct = gpu.get("vram_pct")
        res_note = " · 常驻预载" if role == "llm" and (v_pct or 0) >= 50 and util_val < 5 else ""
        vram_text = f"{v_used:,}/{v_tot:,} MiB ({v_pct:.0f}%{res_note})" if v_used is not None and v_tot else "—"
        v_bar_w = max(0.0, min(100.0, float(v_pct or 0.0)))
        v_cls = _gpu_vram_bar_class(v_pct, role)
        gpu_blocks.append(
            '<div class="brm-sys-gpu">'
            f'<div class="brm-sys-gpu-top"><b>{name}</b><span>{util_text} · {temp} · {power}</span></div>'
            f'<div class="brm-sys-row"><span class="brm-sys-sub">显存 {esc(vram_text)}</span></div>'
            f'<div class="brm-sys-bar {v_cls}"><i style="width:{v_bar_w:.1f}%"></i></div>'
            '</div>'
        )
    if not gpu_blocks:
        gpu_blocks.append('<div class="brm-sys-row"><span class="brm-sys-label">GPU</span><span class="brm-sys-val">暂不可用</span></div>')

    svc_pills = "".join(
        f'<span class="brm-sys-pill{" is-off" if not item.get("online") else ""}" title="{esc(str(item.get("label", "")))}">'
        f'<i></i>:{item.get("port")} {esc(SERVICE_SHORT_NAMES.get(str(item.get("key", "")), ""))}</span>'
        for item in (services.get("items") or [])
    )

    eng_label = esc(str(engine.get("label") or "空闲"))
    eng_cls = "brm-sys-badge is-busy" if engine.get("busy") else "brm-sys-badge"
    q_running = int(q_stats.get("running", 0))
    q_pending = int(q_stats.get("pending", 0))
    q_conc = int(q_stats.get("concurrency", default_concurrency))
    conc_info = stats.get("concurrency") if isinstance(stats.get("concurrency"), dict) else {}
    conc_limits = conc_info.get("limits") if isinstance(conc_info.get("limits"), dict) else {}
    conc_summary = ""
    conc_pills_html = ""
    if conc_limits:
        light_lim = conc_limits.get("image_light", q_conc)
        voice_lim = conc_limits.get("voice_cpu", 1)
        audio_lim = conc_limits.get("audio_gpu", 1)
        breeze_lim = conc_limits.get("voice_breeze", 1)
        degrade_reasons = conc_info.get("degrade_reasons") or []
        degrade_note = f"降档({','.join(str(r) for r in degrade_reasons)})" if conc_info.get("degraded") and degrade_reasons else ""
        conc_items = [
            f"重型 1", f"轻图 {light_lim}", f"音频GPU {audio_lim}",
            f"Zonos3 {voice_lim}", f"Breeze {breeze_lim}",
        ]
        if degrade_note:
            conc_items.append(degrade_note)
        conc_summary = " · ".join(conc_items)
        conc_pills_html = "".join(f'<span class="brm-sys-cpill">{esc(x)}</span>' for x in conc_items)
    today_done = int(t_stats.get("today_done", 0))
    total_ok = int(t_stats.get("total_ok", 0))
    total_fail = int(t_stats.get("total_fail", 0))
    assets_text = esc(str(t_stats.get("assets_text") or stats.get("assets") or "暂不可用"))

    primary_storage = [t for t in storage_tiers if t.get("key") == "outputs"]
    if not primary_storage and storage_tiers:
        primary_storage = storage_tiers[:1]
    secondary_storage = [t for t in storage_tiers if t not in primary_storage]

    primary_storage_html = "".join(_render_storage_tier_html(tier) for tier in primary_storage)
    secondary_storage_html = "".join(_render_storage_tier_html(tier) for tier in secondary_storage)

    return (
        '<section class="brm-system-status">'
        f'<header class="brm-sys-head"><h3>系统状态</h3><span class="{srv_badge_cls}">{esc(srv_badge_text)}</span></header>'
        '<div class="brm-sys-section">'
        '<div class="brm-sys-grid-2">'
        '<div class="brm-sys-metric">'
        f'<div class="brm-sys-row"><span class="brm-sys-label">{esc(cpu_short_label)}</span><span class="brm-sys-val">{cpu_pct_text}{cpu_temp}</span></div>'
        f'<div class="brm-sys-bar {_bar_class(cpu_pct)}"><i style="width:{cpu_bar_w:.1f}%"></i></div>'
        '</div>'
        '<div class="brm-sys-metric">'
        f'<div class="brm-sys-row"><span class="brm-sys-label">物理内存</span><span class="brm-sys-val">{esc(ram_text)}</span></div>'
        f'<div class="brm-sys-bar {_bar_class(ram_pct)}"><i style="width:{ram_bar_w:.1f}%"></i></div>'
        '</div>'
        '</div>'
        f'{primary_storage_html}'
        '</div>'
        '<div class="brm-sys-section">'
        '<div class="brm-sys-title">3×GPU 算力集群</div>'
        f'{"".join(gpu_blocks)}'
        '</div>'
        '<div class="brm-sys-section">'
        '<div class="brm-sys-stat-grid">'
        f'<div class="brm-sys-stat"><span>运行中</span><b>{q_running}/{q_conc}</b></div>'
        f'<div class="brm-sys-stat"><span>排队</span><b>{q_pending}</b></div>'
        f'<div class="brm-sys-stat"><span>今日完成</span><b>{today_done}</b></div>'
        f'<div class="brm-sys-stat"><span>素材总数</span><b>{assets_text}</b></div>'
        '</div>'
        f'<div class="brm-sys-row"><span class="brm-sys-label">活跃引擎 / 累计</span>'
        f'<span class="brm-sys-row-right"><span class="{eng_cls}">{eng_label}</span>'
        f'<span class="brm-sys-sub">成功 {total_ok} / 失败 {total_fail}</span></span></div>'
        '</div>'
        '<details class="brm-telemetry-details">'
        '<summary>展开更多硬件与存储详情 (NVMe模型盘 / 系统盘 / 端口状态)</summary>'
        '<div class="brm-telemetry-details-body">'
        f'<div class="brm-sys-row"><span class="brm-sys-label">CPU 1/5/15m 负载</span><span class="brm-sys-val">{esc(load_text)}</span></div>'
        + (
            f'<div class="brm-sys-row"><span class="brm-sys-label">动态并发配额</span></div>'
            f'<div class="brm-sys-conc" title="{esc(conc_summary)}">{conc_pills_html}</div>'
            if conc_summary else ""
        )
        + f'{secondary_storage_html}'
        '<div class="brm-sys-row"><span class="brm-sys-label">核心服务端口</span></div>'
        f'<div class="brm-sys-pills">{svc_pills}</div>'
        '</div>'
        '</details>'
        '</section>'
    )
