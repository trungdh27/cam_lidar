from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from threading import Event

from PySide6.QtCore import QThread, Signal

from core.testing import TestContext, TestEvaluator, TestRunner, TestStatus
from desktop_app.workers.camera_test_runner_worker import SharedJetsonOperationClient
from devices.camera.ros_automation.registry import RosCameraAdapterRegistry
from devices.camera.ros_automation.remote import RosRemoteProcessManager


class RosCameraTestRunnerWorker(QThread):
    """Runs Phase 8.3A/B through the existing generic TestRunner in a QThread."""

    test_started = Signal(str)
    test_finished = Signal(str, str, object)
    log_event = Signal(str, str)
    suite_finished = Signal(object, str)

    def __init__(
        self,
        definitions,
        registry,
        jetson_service,
        devices,
        target_scope,
        busy_serials=(),
        adapter_registry=None,
        process_manager=None,
        parent=None,
    ):
        super().__init__(parent)
        self.definitions = list(definitions)
        self.registry = registry
        self.devices = tuple(devices)
        self.target_scope = str(target_scope)
        self.busy_serials = tuple(busy_serials)
        self.cancel_event = Event()
        self.client = SharedJetsonOperationClient(
            jetson_service, None, self.cancel_event
        )
        self.adapter_registry = adapter_registry or RosCameraAdapterRegistry()
        self.process_manager = process_manager or RosRemoteProcessManager(self.client)

    def cancel(self):
        self.cancel_event.set()
        self.client.wake()

    def run(self):
        run_stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        target = self.devices[0].serial if len(self.devices) == 1 else "GLOBAL"
        root = Path("evidence") / "camera" / run_stamp / (target or "GLOBAL")
        runner = TestRunner(self.registry, TestEvaluator())
        results = []
        device_snapshot = {
            "target_scope": self.target_scope,
            "cameras": [device.to_dict() for device in self.devices],
        }
        for definition in self.definitions:
            if self.cancel_event.is_set():
                break
            self.test_started.emit(definition.test_id)
            self.log_event.emit(
                "INFO", f"[{definition.test_id}] Starting {definition.name}."
            )
            context = TestContext(
                services={
                    "remote_client": self.client,
                    "selected_devices": self.devices,
                    "ros_adapter_registry": self.adapter_registry,
                    "ros_process_manager": self.process_manager,
                },
                device=device_snapshot,
                base_configuration={
                    "target_scope": self.target_scope,
                    "busy_serials": list(self.busy_serials),
                },
                result_root=str(root),
                cancel_event=self.cancel_event,
                log=lambda level, message: self.log_event.emit(level, message),
            )
            result = runner.run_one(definition, context)
            results.append(result)
            self.test_finished.emit(
                definition.test_id, result.status.value, result.to_dict()
            )
            level = (
                "PASS" if result.status == TestStatus.PASS else
                "FAIL" if result.status == TestStatus.FAIL else
                "WARNING" if result.status in (
                    TestStatus.BLOCKED,
                    TestStatus.CANCELLED,
                    TestStatus.SKIPPED,
                ) else
                "ERROR"
            )
            self.log_event.emit(
                level, f"[{definition.test_id}] {result.status.value}."
            )
            if result.status == TestStatus.CANCELLED:
                break
        counts = Counter(item.status.value for item in results)
        self.suite_finished.emit(
            {"total": len(results), **counts}, str(root)
        )
