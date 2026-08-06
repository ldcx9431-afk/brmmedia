#!/usr/bin/env python3
"""Exercise the H3 restart-recovery acceptance script against a real HTTP API.

The fixture deliberately uses the production shell script, real curl and real
ffprobe.  Only systemctl is replaced, so the test proves that a completed task
is re-read by its original id after a backend restart and that its downloaded
MP4 still contains both video and stereo audio.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote, urlparse


REPO_ROOT = Path(__file__).resolve().parents[1]
ACCEPTANCE = REPO_ROOT / "accept_minimax_h3_video.sh"


class _FixtureApi(BaseHTTPRequestHandler):
    """Tiny in-process LAN API sufficient for a quick T2V/I2V acceptance."""

    media = b""
    requests: Counter[str] = Counter()
    raw_requests: Counter[str] = Counter()
    task_ids: list[str] = []

    def log_message(self, _format: str, *_args: object) -> None:
        pass

    def _json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    @classmethod
    def task_payload(cls, task_id: str) -> dict:
        # A user-visible generated filename can contain whitespace.  The API
        # path must remain percent encoded; this fixture catches shell parsers
        # that split the name and mistake part of it for download_url.
        artifact_name = f"任务 H3 视频 {task_id}.mp4"
        return {
            "task_id": task_id,
            "state": "completed",
            "effective_settings": {
                "profile": "preview",
                "requested_seconds": 4,
                "width": 480,
                "height": 288,
                # H3's supported frame grid: >=124 and frame % 17 == 5.
                "frames": 124,
                "effective_seconds": round(124 / 24, 3),
            },
            "artifacts": [
                {
                    "name": artifact_name,
                    "download_url": f"/api/v1/tasks/{task_id}/artifacts/{quote(artifact_name)}",
                }
            ],
        }

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        path = unquote(urlparse(self.path).path)
        type(self).requests[path] += 1
        type(self).raw_requests[self.path] += 1
        if path == "/api/v1/health":
            self._json({"status": "ok"})
            return
        if path == "/gradio_api/info":
            self._json({"named_endpoints": {}})
            return
        if path.startswith("/api/v1/tasks/") and "/artifacts/" in path:
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(len(type(self).media)))
            self.end_headers()
            self.wfile.write(type(self).media)
            return
        if path.startswith("/api/v1/tasks/"):
            self._json(self.task_payload(path.rsplit("/", 1)[-1]))
            return
        self._json({"detail": "not found"}, 404)

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        path = urlparse(self.path).path
        type(self).requests[path] += 1
        # Consume multipart/JSON input before responding so curl observes a
        # normal successful upload/submit lifecycle.
        length = int(self.headers.get("Content-Length", "0"))
        if length:
            self.rfile.read(length)
        if path == "/api/v1/files":
            self._json({"asset_id": "fixture-image"})
            return
        if path == "/api/v1/tasks":
            task_id = f"h3-task-{len(type(self).task_ids) + 1}"
            type(self).task_ids.append(task_id)
            self._json({"task_id": task_id})
            return
        self._json({"detail": "not found"}, 404)


class H3AcceptanceRecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        required = ("bash", "curl", "ffmpeg", "ffprobe")
        missing = [command for command in required if shutil.which(command) is None]
        if missing:
            raise unittest.SkipTest(f"requires {', '.join(missing)}")

        cls._temp = tempfile.TemporaryDirectory()
        temp = Path(cls._temp.name)
        media = temp / "fixture.mp4"
        # Generate a valid, tiny MP4 with a stereo AAC track.  The assertion
        # below is therefore a real container/stream check, not a mocked
        # ffprobe success path.
        subprocess.run(
            [
                "ffmpeg", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i", "color=c=black:s=32x32:r=12",
                "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
                "-t", "0.3", "-shortest", "-c:v", "mpeg4", "-c:a", "aac",
                "-ac", "2", str(media),
            ],
            check=True,
        )
        _FixtureApi.media = media.read_bytes()
        _FixtureApi.requests = Counter()
        _FixtureApi.raw_requests = Counter()
        _FixtureApi.task_ids = []
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _FixtureApi)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.thread.join(timeout=5)
        cls.server.server_close()
        cls._temp.cleanup()

    def test_completed_task_is_recovered_by_id_and_downloaded_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            fake_bin = temp / "bin"
            fake_bin.mkdir()
            systemctl_log = temp / "systemctl.log"
            (fake_bin / "id").write_text(
                "#!/usr/bin/env bash\n[ \"${1:-}\" = \"-u\" ] && { echo 0; exit 0; }\nexec /usr/bin/id \"$@\"\n",
                encoding="utf-8",
            )
            (fake_bin / "systemctl").write_text(
                "#!/usr/bin/env bash\nprintf '%s\\n' \"$*\" >> \"$BRMMEDIA_TEST_SYSTEMCTL_LOG\"\n"
                "case \"${1:-}\" in is-active) exit 0 ;; restart) exit 0 ;; esac\nexit 0\n",
                encoding="utf-8",
            )
            for command in fake_bin.iterdir():
                command.chmod(0o755)

            port = self.server.server_address[1]
            env = os.environ | {
                "PATH": f"{fake_bin}:{os.environ['PATH']}",
                "BRMMEDIA_TEST_SYSTEMCTL_LOG": str(systemctl_log),
                "BRMMEDIA_LAN_API_BASE": f"http://127.0.0.1:{port}/api/v1",
                "BRMMEDIA_RESTART_GRADIO_HEALTH_URL": f"http://127.0.0.1:{port}/gradio_api/info",
                "BRMMEDIA_BACKEND_UNIT": "baorong-backend-h3-test",
                "BRMMEDIA_H3_POLL_SECONDS": "2",
                "BRMMEDIA_RESTART_RECOVERY_TIMEOUT_SECONDS": "10",
            }
            result = subprocess.run(
                ["bash", str(ACCEPTANCE), "--restart-recovery"],
                env=env,
                text=True,
                capture_output=True,
                timeout=45,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr + "\n" + result.stdout)
            self.assertIn("persisted across baorong-backend-h3-test restart", result.stdout)
            self.assertEqual(_FixtureApi.task_ids, ["h3-task-1", "h3-task-2"])
            self.assertIn("restart baorong-backend-h3-test", systemctl_log.read_text(encoding="utf-8"))

            # h3-task-1 was queried while complete, then queried again after
            # the restart.  Its artifact was fetched before and after restart;
            # ffprobe in the real shell script had to accept it both times.
            first_task = "/api/v1/tasks/h3-task-1"
            first_artifact_name = "任务 H3 视频 h3-task-1.mp4"
            first_artifact = f"/api/v1/tasks/h3-task-1/artifacts/{first_artifact_name}"
            first_artifact_encoded = f"/api/v1/tasks/h3-task-1/artifacts/{quote(first_artifact_name)}"
            self.assertGreaterEqual(_FixtureApi.requests[first_task], 2)
            self.assertGreaterEqual(_FixtureApi.requests[first_artifact], 2)
            self.assertGreaterEqual(_FixtureApi.raw_requests[first_artifact_encoded], 2)
            self.assertGreaterEqual(_FixtureApi.requests["/api/v1/tasks/h3-task-2"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
