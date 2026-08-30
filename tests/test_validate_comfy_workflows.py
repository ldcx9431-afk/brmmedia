#!/usr/bin/env python3
"""Hermetic tests for the ComfyUI static-asset preflight."""

from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "validate_comfy_workflows.py"
SPEC = importlib.util.spec_from_file_location("validate_comfy_workflows", MODULE_PATH)
assert SPEC and SPEC.loader
validator = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(validator)


class EnumInputIssuesTests(unittest.TestCase):
    def workflow_file(self, payload: dict) -> Path:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "workflow.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_accepts_valid_static_asset_and_ignores_graph_edges(self) -> None:
        path = self.workflow_file(
            {
                "1": {
                    "class_type": "CheckpointLoader",
                    "inputs": {"checkpoint": "model.safetensors", "image": ["2", 0]},
                }
            }
        )
        definitions = {
            "CheckpointLoader": {
                "input": {
                    "required": {
                        "checkpoint": [["model.safetensors"]],
                        "image": ["IMAGE"],
                    }
                }
            }
        }
        self.assertEqual(validator.enum_input_issues(path, definitions), [])

    def test_reports_unavailable_static_asset(self) -> None:
        path = self.workflow_file(
            {
                "1": {
                    "class_type": "CheckpointLoader",
                    "inputs": {"checkpoint": "missing.safetensors"},
                }
            }
        )
        definitions = {
            "CheckpointLoader": {
                "input": {"required": {"checkpoint": [["model.safetensors"]]}}
            }
        }
        self.assertEqual(
            validator.enum_input_issues(path, definitions),
            ["CheckpointLoader.checkpoint='missing.safetensors'"],
        )

    def test_does_not_treat_free_text_as_an_asset(self) -> None:
        path = self.workflow_file(
            {"1": {"class_type": "Prompt", "inputs": {"text": "a descriptive prompt"}}}
        )
        definitions = {"Prompt": {"input": {"required": {"text": ["STRING"]}}}}
        self.assertEqual(validator.enum_input_issues(path, definitions), [])

    def test_does_not_require_sample_files_for_upload_capable_input(self) -> None:
        path = self.workflow_file(
            {"1": {"class_type": "LoadImage", "inputs": {"image": "sample.webp"}}}
        )
        definitions = {
            "LoadImage": {
                "input": {
                    "required": {"image": [["currently-present.png"], {"image_upload": True}]}
                }
            }
        }
        self.assertEqual(validator.enum_input_issues(path, definitions), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
