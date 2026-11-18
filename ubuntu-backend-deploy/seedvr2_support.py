"""Shared SeedVR2 model, input, and resource checks for BRMMedia.

This module intentionally contains no ComfyUI or web framework imports so its
limits and media probes can be tested without the production GPU runtime.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from fractions import Fraction
from pathlib import Path
from typing import Any


SEEDVR2_REPO = "Comfy-Org/SeedVR2"
SEEDVR2_REVISION = "10f035adc869a5b3ffc466360b869641511c0610"
SEEDVR2_MODEL = "seedvr2_7b_int8_convrot.safetensors"
SEEDVR2_MODEL_SHA256 = "5aa0d25fc9d35e449b659d0c9a5dcb22e2a4fa04032101b95a39da42b32c1be6"
SEEDVR2_FAST_MODEL = "seedvr2_3b_int8_convrot.safetensors"
SEEDVR2_FAST_MODEL_SHA256 = "c3dec8bcc5916843a8a858572970597462e1f2dc598d6dfd818f6cd40f53a157"
SEEDVR2_MODELS = {
    "7b": SEEDVR2_MODEL,
    "3b": SEEDVR2_FAST_MODEL,
}
SEEDVR2_MODEL_DEFAULT = "7b"
SEEDVR2_VAE = "ema_vae_fp16.safetensors"
SEEDVR2_VAE_SHA256 = "20678548f420d98d26f11442d3528f8b8c94e57ee046ef93dbb7633da8612ca1"

VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".avi"}
VIDEO_UPLOAD_MAX_BYTES = 2 * 1024 * 1024 * 1024
TASK_MAX_SECONDS = 12 * 60 * 60
SEGMENT_SECONDS = 300
MAX_SEGMENT_FRAMES = 241  # 4n+1, matching SeedVR2's temporal frame grid.
STRENGTH_DENOISE = {"light": 0.6, "standard": 0.8, "strong": 1.0}
VIDEO_RESOLUTION_PRESETS = {"720p": 720, "1080p": 1080}
PLANNED_VIDEO_RESOLUTION_PRESETS = {"4k": 2160}


def _bounded_int_env(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, default))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(value, maximum))


# The initial production guard is intentionally conservative for one 24 GB
# A5000. It can only be raised after a real workload passes the burn-in test.
MAX_OUTPUT_EDGE = _bounded_int_env("BRM_SEEDVR2_MAX_OUTPUT_EDGE", 2560, 512, 8192)
MAX_OUTPUT_PIXELS = _bounded_int_env(
    "BRM_SEEDVR2_MAX_OUTPUT_PIXELS", 4_000_000, 262_144, 33_554_432
)
TASK_TIMEOUT_SECONDS = _bounded_int_env(
    "BRM_SEEDVR2_TASK_TIMEOUT", TASK_MAX_SECONDS, 3600, TASK_MAX_SECONDS
)
SEGMENT_SECONDS = _bounded_int_env("BRM_SEEDVR2_SEGMENT_SECONDS", SEGMENT_SECONDS, 30, 600)


def seedvr2_model_filename(model_variant: str = SEEDVR2_MODEL_DEFAULT) -> str:
    variant = str(model_variant or SEEDVR2_MODEL_DEFAULT).strip().lower()
    try:
        return SEEDVR2_MODELS[variant]
    except KeyError as exc:
        raise ValueError("SeedVR2 模型仅支持 7b 或 3b") from exc


def model_paths(
    comfy_root: Path | str,
    model_variant: str = SEEDVR2_MODEL_DEFAULT,
) -> tuple[Path, Path]:
    root = Path(comfy_root).expanduser()
    return (
        root / "models" / "diffusion_models" / seedvr2_model_filename(model_variant),
        root / "models" / "vae" / SEEDVR2_VAE,
    )


def model_installed(comfy_root: Path | str, model_variant: str = SEEDVR2_MODEL_DEFAULT) -> bool:
    model, vae = model_paths(comfy_root, model_variant)
    return model.is_file() and vae.is_file()


def installed_model_variants(comfy_root: Path | str) -> list[str]:
    return [variant for variant in SEEDVR2_MODELS if model_installed(comfy_root, variant)]


def models_installed(comfy_root: Path | str) -> bool:
    """Return true when the shared VAE and at least one supported model exist."""
    return bool(installed_model_variants(comfy_root))


def resolve_comfy_input(comfy_root: Path | str, filename: str) -> Path:
    """Resolve an uploaded ComfyUI input name while rejecting path traversal."""
    if not isinstance(filename, str) or not filename.strip():
        raise ValueError("缺少已上传的素材文件名，请重新上传")
    input_root = (Path(comfy_root).expanduser() / "input").resolve()
    candidate = (input_root / filename.replace("\\", "/")).resolve()
    try:
        candidate.relative_to(input_root)
    except ValueError as exc:
        raise ValueError("上传素材路径无效，请重新上传") from exc
    if not candidate.is_file():
        raise ValueError("找不到已上传的素材，请重新上传")
    return candidate


def is_supported_video_filename(filename: str | Path) -> bool:
    """Accept supported video extensions without regard to letter casing."""
    return Path(str(filename or "")).suffix.casefold() in VIDEO_EXTENSIONS


def output_dimensions(width: int, height: int, scale: int) -> tuple[int, int]:
    if isinstance(width, bool) or isinstance(height, bool) or width <= 0 or height <= 0:
        raise ValueError("无法读取有效的素材尺寸")
    if scale not in (1, 2):
        raise ValueError("输出倍率仅支持 1× 或 2×")
    out_width = int(round(width * scale))
    out_height = int(round(height * scale))
    if max(out_width, out_height) > MAX_OUTPUT_EDGE or out_width * out_height > MAX_OUTPUT_PIXELS:
        raise ValueError(
            f"输出尺寸 {out_width}×{out_height} 超出当前 A5000 安全限制："
            f"最长边 {MAX_OUTPUT_EDGE}px、总像素 {MAX_OUTPUT_PIXELS:,}；请缩小源素材或改用 1×。"
        )
    return out_width, out_height


def video_output_dimensions(
    width: int,
    height: int,
    preset: str,
    scale: int = 2,
) -> tuple[int, int, float]:
    """Return aspect-preserving target dimensions and ImageScaleBy multiplier.

    Resolution presets use the shorter edge (1080 for Full HD, 720 for HD),
    so landscape, portrait and non-16:9 footage keep their original aspect.
    4K is intentionally listed as planned but rejected until it passes the
    production A5000 memory/quality burn-in against the active safety limits.
    """
    if isinstance(width, bool) or isinstance(height, bool) or width <= 0 or height <= 0:
        raise ValueError("无法读取有效的视频尺寸")
    preset = str(preset or "native").strip().lower()
    if preset == "native":
        out_width, out_height = output_dimensions(width, height, scale)
        return out_width, out_height, float(scale)
    if preset in PLANNED_VIDEO_RESOLUTION_PRESETS:
        raise ValueError(
            "4K UHD 预设暂未开放：3840×2160 超出当前 A5000 安全输出上限。"
            "需先完成 4K 显存、稳定性与画质实测后再启用。"
        )
    short_edge = VIDEO_RESOLUTION_PRESETS.get(preset)
    if short_edge is None:
        raise ValueError("视频输出规格仅支持按倍率、720p 或 1080p；4K 尚待验证")

    scale_by = short_edge / min(width, height)
    # Video encoders and SeedVR2 postprocessing require even output dimensions.
    out_width = max(2, int(round(width * scale_by)) // 2 * 2)
    out_height = max(2, int(round(height * scale_by)) // 2 * 2)
    if max(out_width, out_height) > MAX_OUTPUT_EDGE or out_width * out_height > MAX_OUTPUT_PIXELS:
        raise ValueError(
            f"预设输出尺寸 {out_width}×{out_height} 超出当前 A5000 安全限制："
            f"最长边 {MAX_OUTPUT_EDGE}px、总像素 {MAX_OUTPUT_PIXELS:,}；"
            "请改用较低分辨率或按倍率输出。"
        )
    return out_width, out_height, scale_by


def probe_video(path: Path | str, *, timeout: int = 30) -> dict[str, Any]:
    """Read dimensions, duration, frame rate, frame count and audio presence."""
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise RuntimeError("服务器未安装 ffprobe，无法安全读取视频信息")
    command = [
        ffprobe, "-v", "error", "-show_entries",
        "format=duration:stream=codec_type,width,height,avg_frame_rate,r_frame_rate,nb_frames,duration",
        "-of", "json", str(path),
    ]
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"读取视频信息失败：{exc}") from exc
    if completed.returncode != 0:
        raise ValueError(f"无法解码视频容器：{completed.stderr[-400:].strip()}")
    try:
        payload = json.loads(completed.stdout)
        streams = payload.get("streams", [])
        video = next(stream for stream in streams if stream.get("codec_type") == "video")
        duration = float(payload.get("format", {}).get("duration") or video.get("duration") or 0)
        rate_text = video.get("avg_frame_rate") or video.get("r_frame_rate") or "0/0"
        fps = float(Fraction(rate_text))
        nominal_text = video.get("r_frame_rate") or rate_text
        nominal_fps = float(Fraction(nominal_text))
        frame_count_value = video.get("nb_frames")
        frame_count = int(frame_count_value) if frame_count_value not in (None, "N/A") else int(round(duration * fps))
        width, height = int(video["width"]), int(video["height"])
    except (KeyError, StopIteration, TypeError, ValueError, ZeroDivisionError, json.JSONDecodeError) as exc:
        raise ValueError("视频缺少有效的视频流、尺寸、帧率或时长信息") from exc
    if duration <= 0 or fps <= 0 or frame_count <= 0 or width <= 0 or height <= 0:
        raise ValueError("视频时长、帧率或尺寸无效")
    return {
        "duration": duration,
        "fps": fps,
        "fps_text": rate_text,
        "nominal_fps": nominal_fps,
        "frame_count": frame_count,
        "width": width,
        "height": height,
        "has_audio": any(stream.get("codec_type") == "audio" for stream in streams),
    }


def validate_video_info(info: dict[str, Any], *, max_seconds: int = TASK_MAX_SECONDS) -> None:
    duration = float(info.get("duration", 0))
    fps = float(info.get("fps", 0))
    nominal_fps = float(info.get("nominal_fps", fps))
    if duration <= 0 or duration > max_seconds:
        raise ValueError(f"视频时长必须大于 0 且不超过 {max_seconds // 3600} 小时")
    if fps < 1 or fps > 120:
        raise ValueError("视频帧率仅支持 1–120 fps")
    # CreateVideo writes a constant frame rate. Reject VFR sources whose
    # average differs materially from nominal rather than silently drifting.
    if nominal_fps > 0 and abs(fps - nominal_fps) / nominal_fps > 0.02:
        raise ValueError("暂不支持可变帧率视频；请先转为恒定帧率再上传，以保持音画同步")


def segment_duration_seconds(fps: float) -> float:
    """Bound each outer video segment by admin policy and a 4n+1 frame cap."""
    if fps <= 0:
        raise ValueError("视频帧率必须大于 0")
    return min(float(SEGMENT_SECONDS), MAX_SEGMENT_FRAMES / fps)


def estimated_video_workspace_bytes(input_bytes: int, scale: int) -> int:
    """Conservative minimum for source, intermediates, result and remux space."""
    if input_bytes < 0 or scale not in (1, 2):
        raise ValueError("视频空间预算参数无效")
    gib = 1024**3
    scale_reserve = 6 if scale == 2 else 4
    return max(6 * gib, input_bytes * scale_reserve + 2 * gib)


def available_disk_bytes(paths: list[Path | str] | tuple[Path | str, ...] | set[Path | str]) -> int:
    """Return the lowest free-space value across all filesystems used by a job."""
    free_values = []
    seen_devices = set()
    for value in paths:
        path = Path(value).expanduser()
        while not path.exists() and path != path.parent:
            path = path.parent
        usage = shutil.disk_usage(path)
        try:
            device = path.stat().st_dev
        except OSError:
            device = str(path)
        if device not in seen_devices:
            seen_devices.add(device)
            free_values.append(usage.free)
    return min(free_values) if free_values else 0
