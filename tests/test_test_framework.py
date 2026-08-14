import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from PySide6.QtCore import QCoreApplication, QObject, QTimer, Signal

from desktop_app.state.lidar_runtime_state import (
    LidarRuntimeState,
    LidarStreamStatus,
)
from desktop_app.testing.evidence_store import EvidenceStore
from desktop_app.testing.lidar_tests import ImuRateExecutor
from desktop_app.testing.test_case import (
    AutomationLevel,
    TestCaseDefinition,
)
from desktop_app.testing.test_context import TestContext
from desktop_app.testing.test_execution_service import TestExecutionService
from desktop_app.testing.test_executor import BaseTestExecutor
from desktop_app.testing.test_registry import TestRegistry
from desktop_app.testing.test_result import (
    TestOutcome,
    TestResult,
    TestStatus,
)
from devices.livox.profile import load_default_livox_profile


class _PassExecutor(BaseTestExecutor):
    def start(self, definition, context):
        context.execution_order.append(definition.id)
        QTimer.singleShot(
            0,
            lambda: self.finish(
                TestOutcome(TestStatus.PASS, f"{definition.id} passed")
            ),
        )


class _FailExecutor(BaseTestExecutor):
    def start(self, definition, context):
        QTimer.singleShot(
            0,
            lambda: self.finish(
                TestOutcome(
                    TestStatus.FAIL,
                    "Acceptance criterion was measured and not met.",
                    {"measured": 0, "required": 1},
                )
            ),
        )


class _ErrorExecutor(BaseTestExecutor):
    def start(self, definition, context):
        QTimer.singleShot(
            0,
            lambda: self.finish(
                TestOutcome(
                    TestStatus.ERROR,
                    "Infrastructure failed.",
                    error="SSH operation failed",
                )
            ),
        )


class _HangingExecutor(BaseTestExecutor):
    def start(self, definition, context):
        return None


class _SlowExecutor(BaseTestExecutor):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(
            lambda: self.finish(TestOutcome(TestStatus.PASS, "finished"))
        )

    def start(self, definition, context):
        self.timer.start(1000)

    def cancel(self):
        super().cancel()
        self.timer.stop()


class _FakeEvidenceStore:
    def __init__(self):
        self.results = []

    def begin_session(self, device):
        return "session-1"

    def write_result(
        self,
        session_id,
        device,
        definition,
        context,
        result,
        log_lines,
    ):
        result.evidence = [f"evidence/{session_id}/{definition.id}/result.json"]
        self.results.append((definition.id, result.status, list(log_lines)))
        return result.evidence


class _FakeDiscoveryService(QObject):
    completed = Signal(dict)
    failed = Signal(str)

    def __init__(self, result):
        super().__init__()
        self.result = result
        self.busy = False
        self.start_count = 0

    def start(self, model, timeout=8):
        self.start_count += 1
        self.busy = True

        def complete():
            self.busy = False
            self.completed.emit(dict(self.result))

        QTimer.singleShot(0, complete)
        return True


class _FakeStreamService:
    def __init__(self, runtime, active=False):
        self.runtime = runtime
        self.active = active
        self.start_count = 0
        self.stop_count = 0

    def start(self, model):
        if self.active:
            return False
        self.active = True
        self.start_count += 1
        QTimer.singleShot(0, self._emit_streaming)
        return True

    def stop(self):
        if not self.active:
            return False
        self.stop_count += 1
        self.runtime.set_status(LidarStreamStatus.STOPPING)

        def stopped():
            self.active = False
            self.runtime.set_status(LidarStreamStatus.IDLE)

        QTimer.singleShot(0, stopped)
        return True

    def _emit_streaming(self):
        self.runtime.update_metrics(
            {
                "state": "STREAMING",
                "point_packet_rate_hz": 100.0,
                "point_count": 1000,
                "imu_status": "ACTIVE",
                "imu_rate_hz": 200.0,
                "packet_loss_supported": True,
                "packet_loss_percent": 0.0,
                "lidar_timestamp": 100,
            }
        )


class TestFrameworkTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QCoreApplication.instance() or QCoreApplication([])

    def setUp(self):
        profile = load_default_livox_profile()
        continuous = replace(
            profile.testing.continuous_operation,
            development_duration_sec=0.1,
        )
        testing = replace(
            profile.testing,
            sample_window_sec=0.1,
            stream_start_timeout_sec=1.0,
            continuous_operation=continuous,
        )
        self.profile = replace(profile, testing=testing)
        self.runtime = LidarRuntimeState()
        self.stream = _FakeStreamService(self.runtime)
        self.discovery_result = {
            "found": True,
            "status": "FOUND",
            "model": "MID360S",
            "serial": "SERIAL",
            "lidar_ip": str(self.profile.lidar.ip),
            "sdk_version": "1.3.1",
            "network_verification": {
                "ready": True,
                "status": "NETWORK_READY",
                "ipv4_addresses": [self.profile.jetson.cidr],
            },
            "ping": {"reachable": True},
        }
        self.discovery = _FakeDiscoveryService(self.discovery_result)
        self.context = TestContext(
            device="LiDAR",
            jetson_state=SimpleNamespace(
                connected=True,
                network_snapshot={"interfaces": []},
            ),
            jetson_service=SimpleNamespace(is_connected=True),
            device_registry=SimpleNamespace(),
            lidar_runtime_state=self.runtime,
            lidar_stream_service=self.stream,
            lidar_discovery_service=self.discovery,
            network_profile=self.profile,
            selected_model="MID360S",
            discovery_result=dict(self.discovery_result),
            network_verification=dict(
                self.discovery_result["network_verification"]
            ),
            ping_result={"reachable": True},
        )
        self.context.execution_order = []

    def _definition(self, test_id, executor, **requirements):
        return TestCaseDefinition(
            id=test_id,
            device="lidar",
            name=test_id,
            group="Unit",
            description="Unit test definition",
            automation_level=AutomationLevel.AUTO,
            priority="P0",
            timeout_sec=requirements.pop("timeout_sec", 1.0),
            executor=executor,
            **requirements,
        )

    def _service(self, definitions):
        registry = TestRegistry()
        registry.register_many(definitions)
        return TestExecutionService(
            registry,
            evidence_store=_FakeEvidenceStore(),
        )

    def _wait(self, predicate, timeout=2.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.processEvents()
            if predicate():
                return
            time.sleep(0.005)
        self.fail("Timed out waiting for test framework state")

    def test_case_result_and_registry_validation(self):
        definition = self._definition("TEST-001", _PassExecutor)
        registry = TestRegistry()
        registry.register(definition)
        self.assertEqual(registry.get("TEST-001"), definition)
        self.assertEqual(registry.get_tests("LIDAR"), [definition])
        with self.assertRaises(ValueError):
            registry.register(definition)
        with self.assertRaises(ValueError):
            replace(definition, timeout_sec=0)

        result = TestResult("TEST-001")
        result.queue()
        self.assertEqual(result.status, TestStatus.QUEUED)
        result.start()
        result.complete(
            TestOutcome(TestStatus.FAIL, "Measured value below minimum"),
            0.25,
        )
        self.assertEqual(result.status, TestStatus.FAIL)
        self.assertEqual(result.duration_sec, 0.25)

    def test_execution_order_and_actual_results(self):
        service = self._service(
            [
                self._definition("TEST-001", _PassExecutor, order=2),
                self._definition("TEST-002", _PassExecutor, order=1),
            ]
        )
        self.assertTrue(service.start(["TEST-001", "TEST-002"], self.context))
        self._wait(lambda: not service.running)
        self.assertEqual(
            self.context.execution_order,
            ["TEST-001", "TEST-002"],
        )
        self.assertEqual(service.result("TEST-001").status, TestStatus.PASS)
        self.assertIn("passed", service.result("TEST-001").actual_result)

    def test_fail_and_error_semantics_remain_distinct(self):
        service = self._service(
            [
                self._definition("FAIL-001", _FailExecutor),
                self._definition("ERROR-001", _ErrorExecutor),
            ]
        )
        service.start(["FAIL-001", "ERROR-001"], self.context)
        self._wait(lambda: not service.running)
        self.assertEqual(service.result("FAIL-001").status, TestStatus.FAIL)
        self.assertIsNone(service.result("FAIL-001").error)
        self.assertEqual(service.result("ERROR-001").status, TestStatus.ERROR)
        self.assertEqual(service.result("ERROR-001").error, "SSH operation failed")

    def test_timeout_is_execution_error(self):
        service = self._service(
            [
                self._definition(
                    "TIMEOUT-001",
                    _HangingExecutor,
                    timeout_sec=0.05,
                )
            ]
        )
        service.start(["TIMEOUT-001"], self.context)
        self._wait(lambda: not service.running)
        result = service.result("TIMEOUT-001")
        self.assertEqual(result.status, TestStatus.ERROR)
        self.assertIn("Timeout", result.error)

    def test_cancellation_marks_current_and_pending(self):
        service = self._service(
            [
                self._definition("SLOW-001", _SlowExecutor),
                self._definition("PENDING-001", _PassExecutor),
            ]
        )
        service.start(["SLOW-001", "PENDING-001"], self.context)
        self._wait(
            lambda: service.result("SLOW-001").status is TestStatus.RUNNING
        )
        self.assertTrue(service.cancel())
        self._wait(lambda: not service.running)
        self.assertEqual(service.result("SLOW-001").status, TestStatus.CANCELLED)
        self.assertEqual(
            service.result("PENDING-001").status,
            TestStatus.CANCELLED,
        )

    def test_runner_owned_stream_is_started_once_and_cleaned(self):
        definition = self._definition(
            "STREAM-001",
            _PassExecutor,
            requires_jetson=True,
            requires_network=True,
            requires_device=True,
            requires_stream=True,
        )
        service = self._service([definition])
        service.start([definition.id], self.context)
        self._wait(lambda: not service.running)
        self.assertEqual(self.stream.start_count, 1)
        self.assertEqual(self.stream.stop_count, 1)
        self.assertTrue(service.runner_started_stream)
        self.assertFalse(service.stream_was_running_before_tests)

    def test_user_owned_stream_is_reused_and_not_stopped(self):
        self.stream.active = True
        self.runtime.set_status(LidarStreamStatus.STREAMING)
        definition = self._definition(
            "STREAM-001",
            _PassExecutor,
            requires_stream=True,
        )
        service = self._service([definition])
        service.start([definition.id], self.context)
        self._wait(lambda: not service.running)
        self.assertEqual(self.stream.start_count, 0)
        self.assertEqual(self.stream.stop_count, 0)
        self.assertTrue(service.stream_was_running_before_tests)
        self.assertEqual(self.runtime.stream_status, LidarStreamStatus.STREAMING)

    def test_failed_discovery_skips_dependent_stream_case(self):
        self.context.discovery_result = None
        self.context.network_verification = None
        self.discovery.result = {
            "found": False,
            "status": "DEVICE_NOT_FOUND",
            "reason": "timeout",
        }
        definition = self._definition(
            "STREAM-001",
            _PassExecutor,
            requires_device=True,
            requires_stream=True,
        )
        service = self._service([definition])
        service.start([definition.id], self.context)
        self._wait(lambda: not service.running)
        self.assertEqual(self.discovery.start_count, 1)
        self.assertEqual(service.result(definition.id).status, TestStatus.SKIPPED)
        self.assertEqual(self.stream.start_count, 0)

    def test_disconnected_prerequisite_is_skipped(self):
        self.context.jetson_service.is_connected = False
        self.context.jetson_state.connected = False
        definition = self._definition(
            "JETSON-001",
            _PassExecutor,
            requires_jetson=True,
        )
        service = self._service([definition])
        service.start([definition.id], self.context)
        self._wait(lambda: not service.running)
        self.assertEqual(service.result(definition.id).status, TestStatus.SKIPPED)

    def test_threshold_absent_is_measurement_only_skipped(self):
        executor = ImuRateExecutor()
        outcome = executor.evaluate(
            self.context,
            [
                {"imu_rate_hz": 199.8},
                {"imu_rate_hz": 200.1},
            ],
        )
        self.assertEqual(outcome.status, TestStatus.SKIPPED)
        self.assertTrue(outcome.measurements["measurement_only"])
        self.assertAlmostEqual(
            outcome.measurements["imu_rate"]["average"],
            199.95,
        )

    def test_evidence_store_writes_result_metrics_and_log(self):
        with tempfile.TemporaryDirectory() as directory:
            store = EvidenceStore(Path(directory) / "evidence")
            definition = self._definition("EVIDENCE-001", _PassExecutor)
            result = TestResult(definition.id)
            result.start()
            result.complete(
                TestOutcome(
                    TestStatus.PASS,
                    "Measured result passed.",
                    {"value": 1},
                ),
                0.1,
            )
            session_id = store.begin_session("LiDAR")
            paths = store.write_result(
                session_id,
                "LiDAR",
                definition,
                self.context,
                result,
                ["PASS  measured"],
            )
            self.assertEqual(len(paths), 3)
            test_dir = Path(directory) / "evidence" / "lidar" / session_id / definition.id
            self.assertTrue((test_dir / "result.json").is_file())
            self.assertTrue((test_dir / "metrics.json").is_file())
            self.assertTrue((test_dir / "log.txt").is_file())


if __name__ == "__main__":
    unittest.main()
