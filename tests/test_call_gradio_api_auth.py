#!/usr/bin/env python3
"""Regression tests for LAN Basic Auth support in the legacy CLI."""

from __future__ import annotations

import base64
import importlib.util
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "call_gradio_api.py"
SPEC = importlib.util.spec_from_file_location("call_gradio_api", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class CallGradioApiAuthTests(unittest.TestCase):
    def test_auth_header_uses_standard_basic_encoding(self) -> None:
        headers = MODULE.basic_auth_headers("brmadmin", "secret-value")
        expected = base64.b64encode(b"brmadmin:secret-value").decode("ascii")
        self.assertEqual(headers, {"Authorization": f"Basic {expected}"})

    def test_local_call_does_not_add_auth(self) -> None:
        self.assertEqual(MODULE.basic_auth_headers(None, None), {})

    def test_missing_password_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            MODULE.basic_auth_headers("brmadmin", None)


if __name__ == "__main__":
    unittest.main(verbosity=2)
