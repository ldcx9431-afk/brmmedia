#!/usr/bin/env python3
"""Static and dependency-light contracts for isolated IndexTTS-2.5 routing."""

from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "ubuntu-backend-deploy" / "indextts25_server.py"
START = ROOT / "ubuntu-backend-deploy" / "start_indextts25.sh"
PREPARE = ROOT / "ubuntu-backend-deploy" / "prepare_indextts25_candidate.sh"
LOCK = ROOT / "runtime-locks" / "indextts25-v2.5.env"
WEBUI = ROOT / "ubuntu-backend-deploy" / "webui.py"
LAN_API = ROOT / "ubuntu-backend-deploy" / "lan_api.py"


class IndexTTS25ContractTests(unittest.TestCase):
    def test_runtime_is_pinned_and_isolated(self) -> None:
        lock = LOCK.read_text(encoding="utf-8")
        prepare = PREPARE.read_text(encoding="utf-8")
        start = START.read_text(encoding="utf-8")
        self.assertIn("INDEXTTS25_PYTHON=3.11", lock)
        self.assertIn("INDEXTTS25_SOURCE_COMMIT=39207d91c30899cad1e7c1b9eb678c241f678e55", lock)
        self.assertIn("INDEXTTS25_MODEL_REVISION=c39ce5ba981572cb187443877ff559dfb246ce63", lock)
        self.assertIn("INDEXTTS25_USE_DEEPSPEED=0", lock)
        self.assertIn("INDEXTTS25_DEFAULT_INDEX=https://mirrors.aliyun.com/pypi/simple", lock)
        self.assertIn("UV_NO_MANAGED_PYTHON=1 UV_PROJECT_ENVIRONMENT=\"$VENV_ROOT\"", prepare)
        self.assertIn('"$UV_BIN" sync --locked --python "$VENV_PYTHON"', prepare)
        self.assertIn("--default-index", prepare)
        self.assertIn("/mnt/d/model/IndexTTS-2.5", prepare)
        self.assertIn("rsync -a --checksum", prepare)
        self.assertIn("--host \"${INDEXTTS25_HOST:-127.0.0.1}\"", start)
        self.assertNotIn("0.0.0.0", start)

    def test_service_has_only_supported_controls(self) -> None:
        text = SERVER.read_text(encoding="utf-8")
        self.assertIn('SUPPORTED_LANGUAGES = {"zh", "en", "ja", "es", "ar"}', text)
        self.assertIn("duration_factor=1.0 / speed", text)
        self.assertIn("use_bf16=USE_BF16", text)
        self.assertIn("use_cuda_kernel=USE_CUDA_KERNEL", text)
        self.assertIn("use_deepspeed=USE_DEEPSPEED", text)
        self.assertIn("_release_engine()", text)
        self.assertNotIn("use_qwen_emo=True", text)

    def test_workspace_and_rest_preserve_legacy_temperature_without_using_it(self) -> None:
        webui = WEBUI.read_text(encoding="utf-8")
        lan = LAN_API.read_text(encoding="utf-8")
        self.assertIn('"IndexTTS-2.5-语音克隆"', webui)
        self.assertIn("def api_submit_workflow_7(", webui)
        self.assertIn("temperature_deprecated", webui)
        self.assertIn('"voice-clone"', lan)
        self.assertIn('"IndexTTS-2.5 语音克隆"', lan)
        self.assertIn('"deprecated": True', lan)
        self.assertIn("VOICE_CLONE_LANGUAGE_VALUES", lan)


if __name__ == "__main__":
    unittest.main()
