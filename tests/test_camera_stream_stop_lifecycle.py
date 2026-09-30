import asyncio
import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Signal

from desktop_app.controllers.camera_stream_controller import CameraStreamController
from desktop_app.services.jetson_connection_service import JetsonConnectionService
from desktop_app.state.jetson_state import JetsonState
from desktop_app.ui.camera_page import CameraPage
from devices.camera.jetson_zed_stream import JETSON_ZED_STREAM_MANAGER
from devices.camera.remote_zed_adapter import RemoteZedAdapter


class FakeJetsonService(QObject):
    operation_succeeded = Signal(str, object)
    operation_failed = Signal(str, str)

    def __init__(self):
        super().__init__()
        self.is_connected = True
        self.submitted = []

    def submit_operation(self, name, _operation):
        request_id = f"request-{len(self.submitted) + 1}"
        self.submitted.append((request_id, name))
        return request_id


class FakeSSH:
    local_address = "127.0.0.1"

    def __init__(self):
        self.command = None
        self.timeout = None

    async def run(self, command, timeout):
        self.command, self.timeout = command, timeout
        return type("Result", (), {
            "stdout": 'CAMERA_STREAM_JSON={"ok":true,"status":{"running":false,"state":"stopped"}}'
        })()


class CameraStreamStopLifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.service = FakeJetsonService()
        self.controller = CameraStreamController(self.service, object())
        self.payload = {"profile_id": "zed_x_one_4k", "device_id": "12345"}

    def tearDown(self):
        self.controller.timer.stop()

    def _start_to_streaming(self):
        self.controller.start(self.payload)
        request_id, name = self.service.submitted[-1]
        self.assertEqual(name, "camera_start_stream")
        self.service.operation_succeeded.emit(
            request_id, {"pid": 321, "status": {"state": "streaming", "running": True}}
        )

    def test_start_stream_stop_and_start_again(self):
        self._start_to_streaming()
        self.assertTrue(self.controller.timer.isActive())

        self.controller.stop()
        stop_id, stop_name = self.service.submitted[-1]
        self.assertEqual(stop_name, "camera_stop_stream")
        self.service.operation_succeeded.emit(
            stop_id, {"status": {"state": "stopped", "running": False}}
        )
        self.assertIsNone(self.controller.payload)

        self._start_to_streaming()
        self.assertEqual(self.service.submitted[-1][1], "camera_start_stream")

    def test_stop_waits_for_status_poll_then_submits_one_stop(self):
        self._start_to_streaming()
        self.controller._poll()
        poll_id, poll_name = self.service.submitted[-1]
        self.assertEqual(poll_name, "camera_stream_status")

        self.controller.stop()
        self.controller.stop()
        self.assertEqual(len(self.service.submitted), 2)
        self.assertTrue(self.controller.stop_requested)

        self.service.operation_succeeded.emit(
            poll_id, {"status": {"state": "streaming", "process_alive": True}}
        )
        self.assertEqual(self.service.submitted[-1][1], "camera_stop_stream")
        stop_id = self.service.submitted[-1][0]
        self.assertEqual(len(self.service.submitted), 3)
        self.service.operation_succeeded.emit(
            stop_id, {"status": {"state": "stopped", "running": False}}
        )
        self.assertFalse(self.controller.stop_requested)

    def test_successful_jpeg_reconnect_clears_previous_preview_error(self):
        self.assertIn(
            'base.update(preview_state="connected", preview_client_connected=True, preview_error=None)',
            JETSON_ZED_STREAM_MANAGER,
        )
        self.assertIn(
            'base.update(preview_state="live", preview_client_connected=True, preview_error=None)',
            JETSON_ZED_STREAM_MANAGER,
        )

    def test_adapter_sends_remote_stop_request_with_bounded_timeout(self):
        ssh = FakeSSH()
        result = asyncio.run(RemoteZedAdapter().execute_with_ssh(
            ssh, "stop_stream", {"profile_id": "zed_x_one_4k", "device_id": "12345"}
        ))
        self.assertFalse(result["status"]["running"])
        self.assertEqual(ssh.timeout, 15.0)
        self.assertIn('"action":"stop"', ssh.command)
        self.assertIn('"stop_timeout":5', ssh.command)
        self.assertIn("stop_path.touch()", JETSON_ZED_STREAM_MANAGER)

    def test_stop_request_is_sent_and_worker_exit_is_bounded(self):
        with tempfile.TemporaryDirectory() as temp_root:
            root = os.path.join(temp_root, "camera")
            runtime = os.path.join(root, "12345")
            os.makedirs(runtime)
            worker = (
                "import json, os, sys, time\n"
                "from pathlib import Path\n"
                "runtime=Path(sys.argv[1]); pid=runtime/'stream.pid'; status=runtime/'stream_status.json'\n"
                "pid.write_text(str(os.getpid()))\n"
                "status.write_text(json.dumps({'running': True, 'state': 'streaming'}))\n"
                "deadline=time.monotonic()+1.5\n"
                "while not (runtime/'stop.request').exists() and time.monotonic()<deadline: time.sleep(.01)\n"
                "(runtime/'saw_stop.request').write_text('yes')\n"
                "status.write_text(json.dumps({'running': False, 'state': 'stopped'}))\n"
                "pid.unlink(missing_ok=True)\n"
            )
            subprocess.run(
                ["bash", "-c", f"{shlex.quote(sys.executable)} -c {shlex.quote(worker)} {shlex.quote(runtime)} &"],
                check=True,
            )
            pid_path = os.path.join(runtime, "stream.pid")
            deadline = time.monotonic() + 2
            while not os.path.exists(pid_path) and time.monotonic() < deadline:
                time.sleep(0.01)

            manager = JETSON_ZED_STREAM_MANAGER.replace(
                'ROOT = Path("/tmp/cam_lidar/camera")', f"ROOT = Path({root!r})"
            )
            request = {"action": "stop", "serial_number": "12345", "stop_timeout": 1}
            result = subprocess.run(
                [sys.executable, "-c", manager, json.dumps(request)],
                capture_output=True, text=True, timeout=5, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(os.path.exists(os.path.join(runtime, "saw_stop.request")))
            self.assertFalse(os.path.exists(os.path.join(runtime, "stop.request")))
            response = json.loads(next(
                line.split("CAMERA_STREAM_JSON=", 1)[1]
                for line in result.stdout.splitlines()
                if line.startswith("CAMERA_STREAM_JSON=")
            ))
            self.assertTrue(response["ok"])
            self.assertFalse(response["status"]["running"])
            self.assertEqual(response["status"]["state"], "stopped")

    def test_gui_stop_bypasses_operation_guard_for_live_and_lost_preview(self):
        state = JetsonState()
        service = JetsonConnectionService(state)
        page = CameraPage(state, service)
        try:
            page.execution_host_combo.setCurrentText("Jetson")
            page.connection_state = page.connection_state.__class__.STREAMING
            page._monitor_access_mode = page._monitor_access_mode.__class__.DIRECT_SDK
            page.stream_controller.payload = dict(self.payload)
            page.stream_controller.pending_request_id = "status-poll"
            page.stream_controller.pending_action = "stream_status"
            page._set_state(page.connection_state.__class__.CONNECTED)
            self.assertTrue(page.start_stream_button.isEnabled())
            self.assertFalse(page.stop_stream_button.isEnabled())
            page._set_state(page.connection_state.__class__.STREAMING)
            self.assertFalse(page.start_stream_button.isEnabled())
            self.assertTrue(page.stop_stream_button.isEnabled())
            for preview_state in ("LIVE", "LOST"):
                page.preview_state = preview_state
                page._request_action("stop_stream")
                self.assertEqual(page.operation_state, "stopping")
                self.assertFalse(page.start_stream_button.isEnabled())
                self.assertFalse(page.stop_stream_button.isEnabled())
                self.assertTrue(page.stream_controller.stop_requested)
                self.assertIn("Stop Stream requested.", page.live_log.toPlainText())
                self.assertNotIn(
                    "A camera operation is already running.", page.live_log.toPlainText()
                )
                page.operation_state = "idle"
                page.stream_controller.stop_requested = False
        finally:
            service.shutdown()
            page.deleteLater()


if __name__ == "__main__":
    unittest.main()
