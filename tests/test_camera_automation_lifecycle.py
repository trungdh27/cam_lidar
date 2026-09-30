import asyncio
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt

from core.testing.definitions import load_definitions
from core.testing.evaluator import TestEvaluator
from core.testing.models import TestContext as _TestContext, TestStatus as _TestStatus
from core.testing.registry import TestRegistry as _TestRegistry
from core.testing.runner import TestRunner as _TestRunner
from desktop_app.workers.camera_test_runner_worker import CameraTestRunnerWorker
from devices.camera.service import CameraService
from desktop_app.services.jetson_connection_service import JetsonConnectionService
from desktop_app.state.jetson_state import JetsonConnectionStatus, JetsonState
from desktop_app.ui.camera_page import CameraPage
from devices.camera.models import (
    CameraConnectionState,
    CameraOwnership,
    CameraRuntimeState,
    camera_automation_identity,
    normalize_camera_serial,
)
from devices.camera.discovery import normalize_realsense_device, normalize_zed_device
from devices.camera.remote_zed_adapter import RemoteZedAdapter
from devices.camera.testing import register_camera_handlers


class CameraFakeRemote:
    connected = True

    def __init__(self, cancel_event=None, error_on_status=False, fail_dimensions=False, start_frame_count=100, already_running=False):
        self.cancel_event = cancel_event
        self.error_on_status = error_on_status
        self.fail_dimensions = fail_dimensions
        self.start_frame_count = start_frame_count
        self.already_running = already_running
        self.starts = []
        self.stops = []

    def camera(self, action, payload, timeout=15, cleanup=False):
        if action == "start_stream":
            self.starts.append(dict(payload))
            if self.already_running:
                raise RuntimeError("Camera stream is already running.")
            if self.error_on_status:
                return {"status": {"valid_frame_count": 0}}
            return {"status": {"valid_frame_count": self.start_frame_count}}
        if action == "stream_status":
            if self.cancel_event:
                self.cancel_event.set()
            if self.error_on_status:
                raise RuntimeError("simulated status failure")
            return {"status": {"valid_frame_count": 0}}
        if action == "stop_stream":
            self.stops.append(dict(payload))
            profile = payload.get("resolution", "1920 x 1080")
            width, height = (3840, 2160) if "3840" in profile else (1920, 1200) if "1200" in profile else (1920, 1080)
            if self.fail_dimensions:
                width = 1
            return {"status": {
                "state": "stopped", "running": False,
                "actual_width": width, "actual_height": height,
                "valid_frame_count": 100, "invalid_frame_count": 0,
                "corrupted_frame_count": 0, "actual_fps": payload.get("fps", 30),
                "frame_count": 100, "dropped_frames": 0,
                "duplicate_timestamp_count": 0, "timestamp_rollback_count": 0,
                "last_error": None,
            }}
        raise AssertionError(f"Unexpected camera action: {action}")

    def daemon_active(self):
        return not self.fail_dimensions


class _FakeSSH:
    local_address = "127.0.0.1"

    async def run(self, command, timeout):
        return type("Result", (), {
            "stdout": 'CAMERA_STREAM_JSON={"ok":true,"status":{"state":"streaming","running":true}}'
        })()


class CameraAutomationLifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])
        cls.registry = _TestRegistry()
        register_camera_handlers(cls.registry)
        cls.definitions = load_definitions(
            "testcases/camera/definitions/phase8_1a.json", cls.registry
        )

    def definition(self, test_id):
        return next(item for item in self.definitions if item.test_id == test_id)

    def run_case(
        self, test_id, remote, *, profile="zed_x_mini", configuration=None,
        cancel_event=None, serial="12345", device_uid=None,
    ):
        with tempfile.TemporaryDirectory() as root:
            context = _TestContext(
                services={"remote_client": remote},
                device={
                    "device_uid": device_uid or f"stereolabs:{serial}",
                    "vendor": "Stereolabs", "model": "ZED X Mini",
                    "profile_id": profile, "serial": serial,
                },
                base_configuration=configuration or {}, result_root=root,
                cancel_event=cancel_event or threading.Event(),
            )
            result = _TestRunner(self.registry, TestEvaluator()).run_one(
                self.definition(test_id), context
            )
            persisted = json.loads((Path(root) / test_id / "result.json").read_text())
            return result, persisted

    def test_connected_idle_is_editable_and_stream_locks_then_releases_configuration(self):
        state = JetsonState()
        state.status = JetsonConnectionStatus.CONNECTED
        service = JetsonConnectionService(state)
        page = CameraPage(state, service)
        try:
            self.assertTrue(page.resolution_combo.isEnabled())
            self.assertTrue(page.fps_combo.isEnabled())
            self.assertTrue(page.format_combo.isEnabled())
            mini_index = page.model_combo.findData("zed_x_mini")
            page.model_combo.setCurrentIndex(mini_index)
            page._on_action_succeeded("connect", {"device": {}})
            self.assertEqual(page.runtime_state, CameraRuntimeState.CONNECTED_IDLE)
            self.assertEqual(page.camera_owner, CameraOwnership.MONITOR)
            self.assertTrue(page.resolution_combo.isEnabled())
            self.assertTrue(page.fps_combo.isEnabled())
            self.assertTrue(page.format_combo.isEnabled())
            page._automation_running = True
            page._refresh_camera_lifecycle()
            page._update_configuration_controls()
            self.assertFalse(page.resolution_combo.isEnabled())
            self.assertFalse(page.fps_combo.isEnabled())
            self.assertFalse(page.format_combo.isEnabled())
            page._automation_running = False
            page._refresh_camera_lifecycle()
            page._update_configuration_controls()
            self.assertTrue(page.resolution_combo.isEnabled())
            self.assertTrue(page.fps_combo.isEnabled())
            self.assertTrue(page.format_combo.isEnabled())
            page.device_combo.clear()
            page.device_combo.addItem("ZED X Mini — SN 12345", "12345")
            row = next(
                row for row in range(page.test_table.rowCount())
                if page.test_table.item(row, 1).text() == "CS-007"
            )
            page.test_table.item(row, 0).setCheckState(Qt.CheckState.Checked)
            page._update_selected_test_count()
            self.assertTrue(page.run_tests_button.isEnabled())

            page.resolution_combo.setCurrentIndex(page.resolution_combo.findData("HD1080"))
            page.fps_combo.setCurrentText("30")
            requested = page._action_payload()
            self.assertEqual(requested["profile_id"], "zed_x_mini")
            self.assertEqual(requested["resolution_key"], "HD1080")
            self.assertEqual(requested["fps"], 30)

            page._set_operation_state("starting")
            self.assertFalse(page.resolution_combo.isEnabled())
            self.assertFalse(page.fps_combo.isEnabled())
            self.assertFalse(page.format_combo.isEnabled())
            page._set_operation_state("idle")
            page._set_state(CameraConnectionState.STREAMING)
            self.assertFalse(page.resolution_combo.isEnabled())
            self.assertFalse(page.fps_combo.isEnabled())
            self.assertFalse(page.format_combo.isEnabled())
            page._update_selected_test_count()
            self.assertTrue(page.run_tests_button.isEnabled())
            page._set_operation_state("stopping")
            self.assertEqual(page.runtime_state, CameraRuntimeState.STOPPING)
            page._set_operation_state("idle")
            page._set_state(CameraConnectionState.CONNECTED)
            self.assertTrue(page.resolution_combo.isEnabled())
            self.assertTrue(page.fps_combo.isEnabled())
            self.assertTrue(page.format_combo.isEnabled())
            starts = []
            page.stream_controller.start = lambda payload: starts.append(payload)
            page._request_action("start_stream")
            self.assertEqual(len(starts), 1)
            self.assertEqual(starts[0]["resolution_key"], "HD1080")
            self.assertEqual(starts[0]["fps"], 30)
            starts.clear()
            page._record_actual_configuration({
                "actual_width": 1920, "actual_height": 1080,
                "configured_fps": 30, "pixel_format": requested["pixel_format"],
            })
            page._set_operation_state("idle")
            page._set_state(CameraConnectionState.STREAMING)
            self.assertIn("1920x1080 @ 30 FPS", page.stream_configuration_summary.text())
            self.assertFalse(page.format_combo.isEnabled())
            page._on_stream_stopped({})
            self.assertEqual(page.runtime_state, CameraRuntimeState.CONNECTED_IDLE)
            self.assertTrue(page.resolution_combo.isEnabled())
            self.assertTrue(page.fps_combo.isEnabled())
            self.assertTrue(page.format_combo.isEnabled())
            self.assertIsNone(page.actual_configuration)
            # Idle selections are still the next start's request, including after stop.
            page.resolution_combo.setCurrentIndex(page.resolution_combo.findData("HD1200"))
            page.fps_combo.setCurrentText("15")
            page._request_action("start_stream")
            self.assertEqual(starts[0]["resolution_key"], "HD1200")
            self.assertEqual(starts[0]["fps"], 15)
        finally:
            service.shutdown()
            page.deleteLater()

    def test_connected_idle_camera_can_be_acquired_without_manual_disconnect(self):
        remote = CameraFakeRemote()
        result, _persisted = self.run_case(
            "CS-007", remote, configuration={"camera_connected": True},
            serial="58651554", device_uid="stereolabs:58651554",
        )
        self.assertEqual(result.status, _TestStatus.PASS)
        self.assertEqual(len(remote.starts), 1)
        self.assertEqual(len(remote.stops), 1)
        self.assertEqual(remote.starts[0]["serial_number"], "58651554")
        self.assertEqual(remote.stops[0]["serial_number"], "58651554")
        self.assertNotIn("stereolabs:", remote.starts[0]["serial_number"])
        self.assertEqual(result.device["device_uid"], "stereolabs:58651554")
        self.assertEqual(result.device["serial"], "58651554")

    def test_automation_identity_preserves_zed_uid_and_raw_serial(self):
        zed = normalize_zed_device({
            "vendor": "Stereolabs", "model": "ZED X Mini",
            "serial_number": "58651554",
        })
        identity = camera_automation_identity({
            "device_uid": zed.device_uid, "vendor": zed.vendor,
            "model": zed.model, "profile_id": "zed_x_mini", "serial": zed.serial,
        })
        self.assertEqual(identity, {
            "device_uid": "stereolabs:58651554", "vendor": "Stereolabs",
            "model": "ZED X Mini", "profile_id": "zed_x_mini", "serial": "58651554",
        })

        # Monitor inventory and test context resolve the same physical identity.
        self.assertEqual(camera_automation_identity({
            "device_uid": zed.device_uid, "vendor": zed.vendor,
            "model": zed.model, "profile_id": "zed_x_mini", "serial": zed.serial,
        }), identity)

    def test_realsense_uid_and_raw_serial_remain_distinct(self):
        realsense = normalize_realsense_device({
            "model": "Intel RealSense D435I", "serial": "123456789",
            "physical_port": "2-1.3", "backend": "pyrealsense2",
        })
        identity = camera_automation_identity({
            "device_uid": realsense.device_uid, "vendor": realsense.vendor,
            "model": realsense.model, "profile_id": "realsense_d435i",
            "serial": realsense.serial,
        })
        self.assertEqual(identity["device_uid"], "intel-realsense:123456789")
        self.assertEqual(identity["serial"], "123456789")
        self.assertEqual(normalize_camera_serial(
            "intel-realsense:123456789", profile_id="realsense_d435i",
            vendor="Intel RealSense", device_uid="intel-realsense:123456789",
        ), "intel-realsense:123456789")

    def test_remote_stream_request_receives_raw_serial_number(self):
        adapter = RemoteZedAdapter()
        captured = {}

        async def capture(_ssh, _script, request, _marker, _timeout):
            captured.update(request)
            return {"ok": True}

        adapter._execute_script = capture
        result = asyncio.run(adapter._stream_with_ssh(
            _FakeSSH(), "start_stream", {
                "profile_id": "zed_x_mini", "serial_number": "58651554",
                "device_uid": "stereolabs:58651554", "resolution_key": "HD1080",
                "resolution": "1920 x 1080", "fps": 30, "pixel_format": "BGRA",
                "automation_validation": True, "preview_mode": "OFF",
            },
        ))
        self.assertTrue(result["ok"])
        self.assertEqual(captured["serial_number"], "58651554")
        self.assertEqual(captured["device_uid"], "stereolabs:58651554")

    def test_runner_worker_canonicalizes_automation_context(self):
        state = JetsonState()
        service = JetsonConnectionService(state)
        try:
            worker = CameraTestRunnerWorker(
                [self.definition("CS-007")], self.registry, service, CameraService(),
                {
                    "device_uid": "stereolabs:58651554", "vendor": "Stereolabs",
                    "model": "ZED X Mini", "profile_id": "zed_x_mini",
                    "serial": "stereolabs:58651554",
                }, {},
            )
            self.assertEqual(worker.device["device_uid"], "stereolabs:58651554")
            self.assertEqual(worker.device["serial"], "58651554")
            self.assertEqual(worker.device["model"], "ZED X Mini")
        finally:
            service.shutdown()

    def test_legacy_canonical_zed_uid_in_serial_slot_is_normalized(self):
        identity = camera_automation_identity({
            "profile_id": "zed_x_mini", "model": "ZED X Mini",
            "serial": "stereolabs:58651554",
        })
        self.assertEqual(identity["device_uid"], "stereolabs:58651554")
        self.assertEqual(identity["serial"], "58651554")

    def test_invalid_zed_serial_is_diagnosed_before_remote_execution(self):
        remote = CameraFakeRemote()
        result, persisted = self.run_case(
            "CS-007", remote, serial="stereolabs:not-a-serial",
            device_uid="stereolabs:not-a-serial",
        )
        self.assertEqual(result.status, _TestStatus.ERROR)
        self.assertIn("Invalid automation camera identity", result.error["message"])
        self.assertIn("device_uid=stereolabs:not-a-serial", result.error["message"])
        self.assertEqual(remote.starts, [])
        self.assertEqual(remote.stops, [])
        self.assertEqual(persisted["device"]["serial"], "stereolabs:not-a-serial")

    def test_streaming_blocks_with_reason_and_prerequisite_evidence(self):
        remote = CameraFakeRemote()
        result, persisted = self.run_case(
            "CS-007", remote,
            configuration={"manual_stream_active": True, "camera_connected": True},
        )
        reason = "Camera is currently streaming in MONITOR. Stop the active stream before running exclusive Direct SDK automation."
        self.assertEqual(result.status, _TestStatus.BLOCKED)
        self.assertEqual(result.error["message"], reason)
        self.assertEqual(result.measurements["prerequisites"]["camera_detected"], "PASS")
        self.assertEqual(result.measurements["prerequisites"]["camera_selected"], "PASS")
        self.assertEqual(result.measurements["prerequisites"]["stream_active"], "YES")
        self.assertEqual(result.measurements["prerequisites"]["exclusive_sdk_access_available"], "FAIL")
        self.assertEqual(persisted["error"]["diagnostics"]["blocked_reason"], reason)
        self.assertEqual(persisted["measurements"]["prerequisites"]["stream_active"], "YES")
        self.assertEqual(remote.starts, [])

    def test_external_stream_conflict_blocks_without_stopping_other_owner(self):
        remote = CameraFakeRemote(already_running=True)
        result, _ = self.run_case("CS-007", remote)
        self.assertEqual(result.status, _TestStatus.BLOCKED)
        self.assertEqual(
            result.measurements["prerequisites"]["exclusive_sdk_access_available"], "FAIL"
        )
        self.assertIn("another active stream", result.error["message"])
        self.assertEqual(len(remote.starts), 1)
        self.assertEqual(len(remote.stops), 0)

    def test_streaming_block_reason_reaches_execution_log_and_test_details(self):
        state = JetsonState()
        state.status = JetsonConnectionStatus.CONNECTED
        service = JetsonConnectionService(state)
        try:
            worker = CameraTestRunnerWorker(
                [self.definition("CS-007")], self.registry, service, CameraService(),
                {
                    "device_uid": "stereolabs:58651554", "vendor": "Stereolabs",
                    "model": "ZED X Mini", "profile_id": "zed_x_mini",
                    "serial": "58651554",
                },
                {"manual_stream_active": True},
            )
            log_entries, results = [], []
            worker.log_event.connect(lambda _level, message: log_entries.append(message))
            worker.test_finished.connect(lambda _test_id, _status, result: results.append(result))
            with tempfile.TemporaryDirectory() as root:
                previous = os.getcwd()
                try:
                    os.chdir(root)
                    worker.run()
                finally:
                    os.chdir(previous)
            reason = "Camera is currently streaming in MONITOR. Stop the active stream before running exclusive Direct SDK automation."
            self.assertTrue(any(reason in item for item in log_entries))
            self.assertTrue(any(
                "Model: ZED X Mini; Device UID: stereolabs:58651554; Serial: 58651554"
                in item for item in log_entries
            ))
            self.assertEqual(results[0]["error"]["message"], reason)

            page = CameraPage(state, service)
            try:
                page.test_results["CS-007"] = results[0]
                page.test_statuses["CS-007"] = "BLOCKED"
                page._show_test_details("CS-007")
                detail = page.test_detail_text.toPlainText()
                self.assertIn(reason, detail)
                self.assertIn("exclusive_sdk_access_available", detail)
            finally:
                page.deleteLater()
        finally:
            service.shutdown()

    def test_x_mini_runs_supported_modes_and_blocks_only_unsupported_modes(self):
        supported, _ = self.run_case("RF-002", CameraFakeRemote())
        self.assertNotEqual(supported.status, _TestStatus.BLOCKED)
        qhd, _ = self.run_case("RF-003", CameraFakeRemote())
        self.assertEqual(qhd.status, _TestStatus.BLOCKED)
        self.assertIn("does not support QHDPLUS", qhd.error["message"])
        self.assertEqual(
            qhd.measurements["prerequisites"]["requested_configuration_supported"], "FAIL"
        )

    def test_rf_matrix_reopens_each_requested_fps_configuration(self):
        remote = CameraFakeRemote()
        result, _ = self.run_case("RF-004", remote)
        self.assertEqual(result.status, _TestStatus.PASS)
        self.assertEqual(
            [(item["resolution_key"], item["fps"]) for item in remote.starts],
            [("HD1200", 15), ("HD1200", 30), ("HD1200", 60)],
        )
        self.assertEqual(len(remote.stops), 3)

    def test_automation_releases_worker_after_pass_fail_error_and_cancel(self):
        for status, remote, cancel_event in (
            (_TestStatus.PASS, CameraFakeRemote(), threading.Event()),
            (_TestStatus.FAIL, CameraFakeRemote(fail_dimensions=True), threading.Event()),
            (_TestStatus.ERROR, CameraFakeRemote(error_on_status=True), threading.Event()),
        ):
            with self.subTest(status=status):
                result, _ = self.run_case("CS-007", remote, cancel_event=cancel_event)
                self.assertEqual(result.status, status)
                self.assertEqual(len(remote.stops), 1)

        cancel_event = threading.Event()
        remote = CameraFakeRemote(
            cancel_event=cancel_event, error_on_status=False, start_frame_count=0
        )
        result, _ = self.run_case("CS-007", remote, cancel_event=cancel_event)
        self.assertEqual(result.status, _TestStatus.CANCELLED)
        self.assertEqual(len(remote.stops), 1)

    def test_remote_adapter_allows_automation_acquisition_without_monitor_connect(self):
        payload = {
            "profile_id": "zed_x_mini", "device_id": "12345",
            "resolution_key": "HD1080", "resolution": "1920 x 1080",
            "fps": 30, "automation_validation": True,
        }
        response = asyncio.run(RemoteZedAdapter().execute_with_ssh(
            _FakeSSH(), "start_stream", payload
        ))
        self.assertEqual(response["status"]["state"], "streaming")

    def test_automation_monitor_logical_ownership_round_trip_is_repeatable(self):
        state = JetsonState()
        service = JetsonConnectionService(state)
        page = CameraPage(state, service)
        try:
            page._set_state(CameraConnectionState.CONNECTED)
            for _ in range(2):
                page._automation_running = True
                page._refresh_camera_lifecycle()
                self.assertEqual(page.runtime_state, CameraRuntimeState.AUTOMATION_RUNNING)
                self.assertEqual(page.camera_owner, CameraOwnership.AUTOMATION)
                page._automation_running = False
                page._refresh_camera_lifecycle()
                self.assertEqual(page.runtime_state, CameraRuntimeState.CONNECTED_IDLE)
                self.assertEqual(page.camera_owner, CameraOwnership.MONITOR)
            page._set_operation_state("starting")
            self.assertEqual(page.runtime_state, CameraRuntimeState.STARTING)
            page._set_operation_state("idle")
            page._set_state(CameraConnectionState.STREAMING)
            self.assertEqual(page.runtime_state, CameraRuntimeState.STREAMING)
            self.assertEqual(page.camera_owner, CameraOwnership.MONITOR)
        finally:
            service.shutdown()
            page.deleteLater()


if __name__ == "__main__":
    unittest.main()
