"""Focused regression tests for local Qwen API-key lifecycle logic."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "ubuntu-backend-deploy" / "qwen_api_gateway.py"


class QwenApiGatewayTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.previous_db = os.environ.get("BRM_QWEN_API_KEY_DB")
        os.environ["BRM_QWEN_API_KEY_DB"] = str(Path(self.tempdir.name) / "keys.sqlite3")
        requests = types.ModuleType("requests")

        class RequestException(Exception):
            pass

        requests.RequestException = RequestException
        requests.Response = object
        requests.request = lambda *args, **kwargs: (_ for _ in ()).throw(RequestException())
        self.previous_requests = sys.modules.get("requests")
        sys.modules["requests"] = requests
        spec = importlib.util.spec_from_file_location("qwen_api_gateway_test", MODULE_PATH)
        self.gateway = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(self.gateway)

    def tearDown(self):
        if self.previous_requests is None:
            sys.modules.pop("requests", None)
        else:
            sys.modules["requests"] = self.previous_requests
        if self.previous_db is None:
            os.environ.pop("BRM_QWEN_API_KEY_DB", None)
        else:
            os.environ["BRM_QWEN_API_KEY_DB"] = self.previous_db
        self.tempdir.cleanup()

    def test_plaintext_key_is_not_stored_and_usage_is_recorded(self):
        record, api_key = self.gateway._create_key("integration", 30)
        self.assertTrue(api_key.startswith("brm_"))
        self.gateway._validate_and_record_usage(api_key)
        with self.gateway._database() as connection:
            row = connection.execute("SELECT * FROM api_keys WHERE id = ?", (record["id"],)).fetchone()
        self.assertNotIn(api_key, tuple(row))
        self.assertEqual(row["use_count"], 1)
        self.assertIsNotNone(row["last_used_at"])

    def test_disabled_and_expired_keys_are_rejected(self):
        record, api_key = self.gateway._create_key("integration", 1)
        with self.gateway._database() as connection:
            connection.execute("UPDATE api_keys SET status = 'disabled' WHERE id = ?", (record["id"],))
        with self.assertRaises(self.gateway.HTTPException) as disabled:
            self.gateway._validate_and_record_usage(api_key)
        self.assertEqual(disabled.exception.status_code, 401)

        record, api_key = self.gateway._create_key("expired", 1)
        with self.gateway._database() as connection:
            connection.execute("UPDATE api_keys SET expires_at = 1 WHERE id = ?", (record["id"],))
        with self.assertRaises(self.gateway.HTTPException) as expired:
            self.gateway._validate_and_record_usage(api_key)
        self.assertEqual(expired.exception.status_code, 401)

    def test_key_lifecycle_actions_preserve_audit_records(self):
        record, api_key = self.gateway._create_key("lifecycle", 30)
        disabled = self.gateway.disable_api_key(record["id"])
        self.assertEqual(disabled["status"], "disabled")
        enabled = self.gateway.enable_api_key(record["id"])
        self.assertEqual(enabled["status"], "active")

        rotated = self.gateway.rotate_api_key(record["id"])
        self.assertNotEqual(rotated["id"], record["id"])
        self.assertTrue(rotated["api_key"].startswith("brm_"))
        with self.assertRaises(self.gateway.HTTPException) as old_key:
            self.gateway._validate_and_record_usage(api_key)
        self.assertEqual(old_key.exception.status_code, 401)

        revoked = self.gateway.revoke_api_key(rotated["id"])
        self.assertEqual(revoked["status"], "revoked")
        with self.gateway._database() as connection:
            rows = connection.execute("SELECT id, status FROM api_keys ORDER BY created_at").fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual({row["status"] for row in rows}, {"revoked"})

    def test_upstream_is_derived_from_active_nginx_proxy(self):
        snippet = Path(self.tempdir.name) / "upstream.conf"
        snippet.write_text("location /qwen/ {\n proxy_pass http://172.20.0.1:8001/;\n}\n", encoding="utf-8")
        self.gateway.QWEN_NGINX_SNIPPET = snippet
        self.gateway.ACTIVE_QWEN_ENV = Path(self.tempdir.name) / "missing.env"
        self.assertEqual(self.gateway._active_qwen_base(), "http://172.20.0.1:8001/v1")

    def test_chat_payload_defaults_to_answer_only_for_compatibility(self):
        body = json.dumps({"model": "qwen", "messages": []}).encode()
        prepared = json.loads(self.gateway._prepare_chat_payload(body))
        self.assertEqual(prepared["chat_template_kwargs"], {"enable_thinking": False})

        explicit = json.dumps({
            "model": "qwen",
            "enable_thinking": True,
            "chat_template_kwargs": {"enable_thinking": True},
        }).encode()
        self.assertTrue(json.loads(self.gateway._prepare_chat_payload(explicit))["chat_template_kwargs"]["enable_thinking"])


if __name__ == "__main__":
    unittest.main()
