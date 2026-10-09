"""Run the actual launcher against a fake server, without loading any GPU model."""
import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


@unittest.skipUnless(os.name == "posix", "Launcher behavior is checked on Linux")
class QwenVisionStartupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        source = Path(os.environ.get("QWEN38_TEST_LAUNCHER", Path(__file__).resolve().parents[1] / "llm-backend-deploy/start_qwen38_llama.sh"))
        self.script = self.root / "start.sh"
        self.script.write_bytes(source.read_bytes())
        self.server = self.root / "fake-server"
        self.server.write_text('#!/usr/bin/env bash\nprintf "%s\\n" "$@"\n')
        self.server.chmod(0o755)
        self.model = self.root / "model.gguf"
        self.model.write_bytes(b"model fixture")
        self.projector = self.root / "projector with spaces.gguf"
        self.projector.write_bytes(b"projector fixture")
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("QWEN38_")}
        self.env.update(QWEN38_BIN=str(self.server), QWEN38_MODEL=str(self.model))

    def run_launcher(self, **config):
        return subprocess.run(["bash", str(self.script)], env={**self.env, **config}, capture_output=True, text=True)

    def test_text_only_profile_keeps_existing_context(self):
        result = self.run_launcher()
        self.assertEqual(result.returncode, 0, result.stderr)
        args = result.stdout.splitlines()
        self.assertNotIn("--mmproj", args)
        self.assertEqual(args[args.index("--ctx-size") + 1], "140288")

    def test_vision_defaults_to_cpu_and_keeps_projector_path_as_one_argument(self):
        result = self.run_launcher(QWEN38_MMPROJ=str(self.projector), QWEN38_MMPROJ_SHA256=hashlib.sha256(self.projector.read_bytes()).hexdigest())
        self.assertEqual(result.returncode, 0, result.stderr)
        args = result.stdout.splitlines()
        self.assertEqual(args[args.index("--mmproj") + 1], str(self.projector))
        self.assertIn("--no-mmproj-offload", args)
        self.assertEqual(args[args.index("--image-max-tokens") + 1], "1024")

    def test_missing_or_corrupt_projector_refuses_to_start(self):
        for config in ({"QWEN38_MMPROJ": str(self.root / "missing.gguf")}, {"QWEN38_MMPROJ": str(self.projector), "QWEN38_MMPROJ_SHA256": "0" * 64}):
            with self.subTest(config=config):
                result = self.run_launcher(**config)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")

    def test_invalid_resource_configuration_refuses_to_start(self):
        for config in ({"QWEN38_IMAGE_MAX_TOKENS": "0"}, {"QWEN38_IMAGE_MAX_TOKENS": "bad"}, {"QWEN38_MMPROJ_OFFLOAD": "yes"}):
            with self.subTest(config=config):
                result = self.run_launcher(QWEN38_MMPROJ=str(self.projector), **config)
                self.assertEqual(result.returncode, 2)


if __name__ == "__main__":
    unittest.main()
