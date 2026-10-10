#!/usr/bin/env python3
"""Execute the project's ACE-Step music workflow and save its returned audio."""

from __future__ import annotations

import json
import random
from pathlib import Path

from comfyui_server import WORKFLOW_DIR, get_view_file, run_workflow


def main() -> int:
    workflow = json.loads((WORKFLOW_DIR / "音乐生成.json").read_text(encoding="utf-8"))
    workflow["104"]["inputs"]["unet_name"] = "acestep/acestep_v1.5_xl_turbo_bf16.safetensors"
    workflow["3"]["inputs"].update({"steps": 8, "cfg": 1.0, "seed": random.randint(1, 2**31 - 1)})
    workflow["94"]["inputs"].update(
        {"tags": "gentle piano, warm, cinematic", "lyrics": "", "bpm": 90, "language": "en", "duration": 10.0,
         "seed": random.randint(1, 2**31 - 1)}
    )
    workflow["98"]["inputs"]["seconds"] = 10.0

    outputs = run_workflow(workflow)
    output_dir = Path("/srv/brmmedia/outputs")
    output_dir.mkdir(parents=True, exist_ok=True)
    saved: list[str] = []
    for node_output in outputs.values():
        for audio in node_output.get("audio", []):
            target = output_dir / f"smoke_music_{audio['filename']}"
            target.write_bytes(
                get_view_file(audio["filename"], audio.get("subfolder", ""), audio.get("type", "output"))
            )
            saved.append(str(target))
    if not saved:
        raise RuntimeError(f"ComfyUI returned no audio output: {outputs}")
    print("SMOKE_OUTPUTS=" + json.dumps(saved, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
