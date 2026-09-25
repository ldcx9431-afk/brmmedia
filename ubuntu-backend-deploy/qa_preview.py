"""Launch the current web UI module on an isolated QA-only port.

This helper intentionally does not start ComfyUI or task workers.  It is used
only for browser layout verification against the checked-in design images.
"""

from __future__ import annotations

import os

import gradio as gr

import webui


def main() -> None:
    demo = webui.build_ui()
    demo.queue()
    demo.launch(
        server_name=os.environ.get("BRM_QA_HOST", "127.0.0.1"),
        server_port=int(os.environ.get("BRM_QA_PORT", "9011")),
        css=webui.CUSTOM_CSS,
        theme=gr.themes.Soft(
            primary_hue="red", secondary_hue="orange", neutral_hue="stone"
        ),
        inbrowser=False,
        root_path="",
        js=webui.BRM_NAV_JS,
    )


if __name__ == "__main__":
    main()
