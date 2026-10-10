#!/usr/bin/env python3
"""Execute the project's Z-Image workflow directly and save returned images."""

from __future__ import annotations

import argparse
import json
import random
from pathlib import Path

from comfyui_server import WORKFLOW_DIR, get_view_file, run_workflow


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--prompt", default="a small red apple on a wooden table, studio photo")
    parser.add_argument("--width", type=int, default=512)
    parser.add_argument("--height", type=int, default=512)
    parser.add_argument("--output-dir", type=Path, default=Path("/srv/brmmedia/outputs"))
    args = parser.parse_args()

    workflow_path = WORKFLOW_DIR / "image_z_image_turbo.json"
    workflow = json.loads(workflow_path.read_text(encoding="utf-8"))
    workflow["57:27"]["inputs"]["text"] = args.prompt
    workflow["57:13"]["inputs"]["width"] = args.width
    workflow["57:13"]["inputs"]["height"] = args.height
    workflow["57:13"]["inputs"]["batch_size"] = 1
    workflow["57:3"]["inputs"]["seed"] = random.randint(1, 2**31 - 1)

    outputs = run_workflow(workflow)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    saved: list[str] = []
    for node_output in outputs.values():
        for image in node_output.get("images", []):
            target = args.output_dir / f"smoke_z_{image['filename']}"
            target.write_bytes(
                get_view_file(image["filename"], image.get("subfolder", ""), image.get("type", "output"))
            )
            saved.append(str(target))

    if not saved:
        raise RuntimeError(f"ComfyUI returned no image output: {outputs}")
    print("SMOKE_OUTPUTS=" + json.dumps(saved, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
