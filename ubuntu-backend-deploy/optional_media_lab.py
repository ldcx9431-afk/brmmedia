"""Serve optional BRMMedia models on a separate, authenticated workbench."""

from __future__ import annotations

import base64
import logging
import os
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
from uuid import uuid4

import gradio as gr
from PIL import Image
import requests

PRODUCTION_APP = Path(os.environ.get("BRM_PRODUCTION_APP", "/srv/brmmedia/releases/h3-23d7084/ubuntu-backend-deploy"))
sys.path.append(str(PRODUCTION_APP))
import media_runtime_guard as gpu_guard
import qwen_image21_support as qwen

MAIN_COMFY = "http://127.0.0.1:8188"
ZONOS = "http://127.0.0.1:1919"
OUTPUTS = Path(os.environ.get("BRM_MODEL_LAB_OUTPUTS", "/var/lib/brmmedia-model-lab/outputs"))
LORA = "qwen_image_2.1_turbo_lora_avg_rank_178_bf16.safetensors"


def turbo_workflow(args):
    graph = qwen.build_workflow(args)
    graph["12"] = {
        "class_type": "LoraLoaderModelOnly",
        "inputs": {"model": ["1", 0], "lora_name": LORA, "strength_model": 1.0},
    }
    graph["6"]["inputs"]["model"] = ["12", 0]
    graph["8"]["inputs"]["filename_prefix"] = "ModelLab/QwenTurbo"
    return graph


def local_session():
    session = requests.Session()
    session.trust_env = False
    return session


def generate_image(prompt, size, seed, reference=None):
    try:
        width, height = map(int, size.split("×"))
        args = qwen.validate(prompt, width, height, 8, seed)
        if reference:
            path = Path(reference)
            if path.stat().st_size > 16 * 1024**2:
                raise ValueError("参考图片最大 16 MB")
            with Image.open(path) as source:
                source.verify()
            with local_session() as session, path.open("rb") as stream:
                response = session.post(
                    MAIN_COMFY + "/upload/image",
                    files={"image": (f"model-lab-{uuid4().hex}{path.suffix}", stream)},
                    timeout=25,
                )
                response.raise_for_status()
                args["input_filename"] = response.json()["name"]
        task = SimpleNamespace(
            args=args, cancel_event=threading.Event(), prompt_id="",
            workflow_name=qwen.EDIT if reference else qwen.WORKFLOW,
            name=f"qwen-image21-turbo-{uuid4().hex}",
        )
        with gpu_guard.lease():
            gpu_guard.handover(MAIN_COMFY, target=qwen.BASE)
            try:
                files = qwen.run_task(task, OUTPUTS, MAIN_COMFY, lambda event: None, workflow_builder=turbo_workflow)
            finally:
                gpu_guard.release_all(MAIN_COMFY)
        return files[0]
    except (OSError, ValueError, RuntimeError, requests.RequestException) as exc:
        logging.exception("Qwen Turbo task failed")
        raise gr.Error("图像生成失败，请检查输入或稍后重试。") from exc


def generate_voice(text, reference):
    text = (text or "").strip()
    if not 1 <= len(text) <= 600:
        raise gr.Error("请输入 1–600 字的配音文本。")
    payload = {"text": text, "format": "wav", "stream": False, "seed": 42, "speaker_mode": "embedding"}
    try:
        if reference:
            path = Path(reference)
            if path.stat().st_size > 16 * 1024**2:
                raise ValueError("参考音频最大 16 MB")
            payload["speaker_audio_base64"] = base64.b64encode(path.read_bytes()).decode("ascii")
        with local_session() as session:
            response = session.post(ZONOS + "/v2/generate", json=payload, timeout=(5, 210))
            response.raise_for_status()
        if not response.content.startswith(b"RIFF") or response.content[8:12] != b"WAVE":
            raise ValueError("ZONOS returned an invalid WAV")
        path = OUTPUTS / f"zonos3-{uuid4().hex}.wav"
        path.write_bytes(response.content)
        return str(path)
    except (OSError, ValueError, requests.RequestException) as exc:
        logging.exception("ZONOS3 task failed")
        raise gr.Error("语音生成失败，请检查参考音频或稍后重试。") from exc


def build():
    with gr.Blocks(title="BRMMedia 模型试用", delete_cache=(3600, 86400)) as app:
        gr.Markdown("# BRMMedia 模型试用")
        with gr.Tab("Qwen Image 2.1 Turbo LoRA"):
            prompt = gr.Textbox(label="提示词", lines=4)
            with gr.Row():
                size = gr.Dropdown(["1024×1024", "1024×576", "576×1024"], value="1024×1024", label="尺寸")
                seed = gr.Number(value=42, minimum=0, maximum=4294967295, precision=0, label="种子")
            reference = gr.Image(type="filepath", label="参考图（选填）")
            image = gr.Image(type="filepath", label="结果", interactive=False)
            gr.Button("生成图片", variant="primary").click(
                generate_image, [prompt, size, seed, reference], image, api_name="qwen-turbo", concurrency_limit=1,
            )
        with gr.Tab("ZONOS3-Mini"):
            words = gr.Textbox(label="配音文本", lines=4, max_length=600)
            clip = gr.Audio(type="filepath", label="参考音频（选填）")
            audio = gr.Audio(type="filepath", label="结果", interactive=False)
            gr.Button("生成语音", variant="primary").click(generate_voice, [words, clip], audio, api_name="zonos3", concurrency_limit=1)
    return app


if __name__ == "__main__":
    OUTPUTS.mkdir(parents=True, exist_ok=True)
    app = build()
    app.queue(max_size=6)
    app.launch(
        server_name="127.0.0.1", server_port=9013, root_path="/model-lab",
        allowed_paths=[str(OUTPUTS)], max_file_size="16mb", inbrowser=False,
    )
