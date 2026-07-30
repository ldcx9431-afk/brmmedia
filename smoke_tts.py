#!/usr/bin/env python3
"""Exercise the IndexTTS2 workflow using an existing local audio file."""

from __future__ import annotations

import json
import random
from pathlib import Path

from comfyui_server import WORKFLOW_DIR, get_view_file, run_workflow, upload_image


def main() -> int:
    reference = Path("/srv/brmmedia/outputs/smoke_music_ACE-Step_turbo_00001.mp3")
    if not reference.is_file():
        raise FileNotFoundError(f"reference audio is missing: {reference}")
    uploaded_name = upload_image(reference, overwrite=True)

    workflow = json.loads((WORKFLOW_DIR / "TTS-语音克隆.json").read_text(encoding="utf-8"))
    workflow["1"]["inputs"]["audio"] = uploaded_name
    workflow["2"]["inputs"].update(
        {"text": "这是部署验证语音。", "seed": random.randint(1, 2**31 - 1), "temperature": 0.8}
    )
    outputs = run_workflow(workflow)
    output_dir = Path("/srv/brmmedia/outputs")
    saved: list[str] = []
    for node_output in outputs.values():
        for audio in node_output.get("audio", []):
            target = output_dir / f"smoke_tts_{audio['filename']}"
            target.write_bytes(
                get_view_file(audio["filename"], audio.get("subfolder", ""), audio.get("type", "output"))
            )
            saved.append(str(target))
    if not saved:
        raise RuntimeError(f"ComfyUI returned no TTS audio: {outputs}")
    print("SMOKE_OUTPUTS=" + json.dumps(saved, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
