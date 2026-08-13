#!/usr/bin/env python3
"""Hermetic checks for the ComfyUI prompt lifecycle bridge."""

from __future__ import annotations

import importlib.util
import json
import sys
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "ubuntu-backend-deploy" / "comfyui_server.py"


class _Response:
    def __init__(self, payload=None, status_code=200):
        self._payload = payload if payload is not None else {}
        self.status_code = status_code
        self.text = json.dumps(self._payload)

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _load_module():
    requests = types.ModuleType("requests")
    requests.RequestException = RuntimeError
    requests.get = lambda *args, **kwargs: _Response()
    requests.post = lambda *args, **kwargs: _Response()
    sys.modules["requests"] = requests
    spec = importlib.util.spec_from_file_location("comfyui_lifecycle_under_test", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class ComfyUiTaskLifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.comfy = _load_module()

    def test_websocket_progress_event_is_normalised(self):
        events = []
        self.comfy._handle_ws_event("pid-1", {
            "type": "progress",
            "data": {"prompt_id": "pid-1", "node": "15", "value": 3, "max": 8},
        }, events.append)
        self.assertEqual(events, [{
            "stage": "sampling", "current_step": 3, "total_steps": 8,
            "progress": 0.375, "node_id": "15", "prompt_id": "pid-1",
        }])

    def test_websocket_ignores_another_prompt(self):
        events = []
        self.comfy._handle_ws_event("pid-1", {
            "type": "progress",
            "data": {"prompt_id": "pid-2", "value": 1, "max": 4},
        }, events.append)
        self.assertEqual(events, [])

    def test_cancel_pending_prompt_deletes_only_target_and_confirms_queue(self):
        queues = iter([
            {"queue_running": [], "queue_pending": [[1, "pid-1"], [2, "other"]]},
            {"queue_running": [], "queue_pending": [[2, "other"]]},
        ])
        with patch.object(self.comfy, "get_queue", side_effect=lambda: next(queues)), \
             patch.object(self.comfy, "delete_queued_prompt", return_value=(True, "ok")) as delete, \
             patch.object(self.comfy, "interrupt") as interrupt, \
             patch.object(self.comfy, "get_history", return_value={}):
            result = self.comfy.cancel_prompt("pid-1", verify_timeout=0)
        self.assertTrue(result.was_pending)
        self.assertFalse(result.was_running)
        self.assertTrue(result.queue_cleared)
        delete.assert_called_once_with("pid-1")
        interrupt.assert_not_called()

    def test_cancel_running_prompt_interrupts_and_confirms_history(self):
        queues = iter([
            {"queue_running": [[1, "pid-1"]], "queue_pending": []},
            {"queue_running": [], "queue_pending": []},
        ])
        history = {"pid-1": {"outputs": {}, "status": {"status_str": "error"}}}
        with patch.object(self.comfy, "get_queue", side_effect=lambda: next(queues)), \
             patch.object(self.comfy, "delete_queued_prompt", return_value=(True, "ok")), \
             patch.object(self.comfy, "interrupt", return_value=(True, "ok")) as interrupt, \
             patch.object(self.comfy, "get_history", return_value=history):
            result = self.comfy.cancel_prompt("pid-1", verify_timeout=0)
        self.assertTrue(result.was_running)
        self.assertTrue(result.interrupt_accepted)
        self.assertTrue(result.history_confirmed)
        self.assertTrue(result.confirmed)
        interrupt.assert_called_once_with()

    def test_run_workflow_exposes_prompt_before_wait(self):
        submitted = []
        with patch.object(self.comfy, "queue_prompt", return_value={"prompt_id": "pid-1"}), \
             patch.object(self.comfy, "wait_for_outputs", return_value={"42": {}}) as wait:
            outputs = self.comfy.run_workflow({"1": {}}, on_submitted=submitted.append)
        self.assertEqual(submitted, ["pid-1"])
        self.assertEqual(outputs, {"42": {}})
        wait.assert_called_once()
        self.assertEqual(wait.call_args.args[0], "pid-1")

    def test_run_workflow_reattaches_without_duplicate_submission(self):
        with patch.object(self.comfy, "queue_prompt") as submit, \
             patch.object(self.comfy, "get_history", return_value={}), \
             patch.object(self.comfy, "get_queue", return_value={"_available": True, "queue_running": [[1, "existing-pid"]]}), \
             patch.object(self.comfy, "wait_for_outputs", return_value={}) as wait:
            self.comfy.run_workflow(None, prompt_id="existing-pid")
        submit.assert_not_called()
        self.assertEqual(wait.call_args.args[0], "existing-pid")
        self.assertTrue(wait.call_args.kwargs["reattach"])

    def test_reattach_rejects_prompt_lost_from_queue_and_history(self):
        with patch.object(self.comfy, "get_history", return_value={}), \
             patch.object(self.comfy, "get_queue", return_value={"_available": True, "queue_running": [], "queue_pending": []}), \
             patch.object(self.comfy, "_open_progress_websocket", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "absent from queue and history"):
                self.comfy.wait_for_outputs("lost-pid", timeout=1, reattach=True)

    def test_quiet_websocket_still_falls_back_to_history_poll(self):
        class WebSocketTimeoutException(Exception):
            pass

        class QuietSocket:
            def recv(self):
                raise WebSocketTimeoutException()

        history = {"pid-1": {"outputs": {"42": {"images": []}}, "status": {"status_str": "success"}}}
        with patch.object(self.comfy, "_open_progress_websocket", return_value=QuietSocket()), \
             patch.object(self.comfy, "get_history", return_value=history) as get_history:
            outputs = self.comfy.wait_for_outputs("pid-1", timeout=1)
        self.assertEqual(outputs, history["pid-1"]["outputs"])
        get_history.assert_called_with("pid-1")

    def test_cancel_does_not_confirm_when_queue_endpoint_is_unavailable(self):
        with patch.object(self.comfy, "get_queue", return_value={"_available": False}), \
             patch.object(self.comfy, "delete_queued_prompt", return_value=(True, "ok")), \
             patch.object(self.comfy, "get_history", return_value={}):
            result = self.comfy.cancel_prompt("pid-1", verify_timeout=0)
        self.assertFalse(result.queue_cleared)
        self.assertFalse(result.confirmed)

    def test_stop_wait_reports_unconfirmed_cancellation(self):
        stop = threading.Event()
        stop.set()
        unconfirmed = self.comfy.CancellationResult(
            prompt_id="pid-1", was_running=False, was_pending=False,
            interrupt_accepted=False, delete_accepted=True,
            queue_cleared=False, history_confirmed=False,
        )
        with patch.object(self.comfy, "_open_progress_websocket", return_value=None), \
             patch.object(self.comfy, "cancel_prompt", return_value=unconfirmed):
            with self.assertRaisesRegex(RuntimeError, "queue_cleared=False"):
                self.comfy.wait_for_outputs("pid-1", timeout=1, stop_event=stop)


if __name__ == "__main__":
    unittest.main(verbosity=2)
