"""Tests for the workbench's same-access Basic Auth user creation callback."""

from __future__ import annotations

import importlib.util
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "ubuntu-backend-deploy"
    / "lan_user_management.py"
)
SPEC = importlib.util.spec_from_file_location("lan_user_management_test", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
lan_users = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(lan_users)


class LanUserManagementTests(unittest.TestCase):
    def test_valid_user_is_sent_to_helper_on_stdin_not_argv(self):
        with patch.object(lan_users.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run:
            result = lan_users.add_lan_user("zhangsan", "safe-pass-123", "safe-pass-123")

        self.assertEqual(result[:3], ("", "", ""))
        self.assertIn("与 brmadmin 相同", result[3])
        args, kwargs = run.call_args
        self.assertEqual(args[0], ["sudo", "-n", lan_users.LAN_USER_HELPER])
        self.assertNotIn("safe-pass-123", " ".join(args[0]))
        self.assertEqual(kwargs["input"], "zhangsan\nsafe-pass-123\n")
        self.assertTrue(kwargs["capture_output"])

    def test_invalid_inputs_never_invoke_privileged_helper(self):
        invalid = [
            ("brmadmin", "safe-pass-123", "safe-pass-123"),
            ("has space", "safe-pass-123", "safe-pass-123"),
            ("ab", "safe-pass-123", "safe-pass-123"),
            ("zhangsan", "short", "short"),
            ("zhangsan", "safe-pass-123", "different-pass"),
            ("zhangsan", "汉" * 25, "汉" * 25),
        ]
        with patch.object(lan_users.subprocess, "run") as run:
            for values in invalid:
                with self.subTest(username=values[0]):
                    result = lan_users.add_lan_user(*values)
                    self.assertTrue(result[3].startswith("❌"))
        run.assert_not_called()

    def test_duplicate_username_is_reported_without_helper_output(self):
        with patch.object(
            lan_users.subprocess,
            "run",
            return_value=subprocess.CompletedProcess([], 3, stdout="secret", stderr="internal detail"),
        ):
            result = lan_users.add_lan_user("zhangsan", "safe-pass-123", "safe-pass-123")
        self.assertIn("已存在", result[3])
        self.assertNotIn("secret", result[3])
        self.assertNotIn("internal detail", result[3])


if __name__ == "__main__":
    unittest.main()
