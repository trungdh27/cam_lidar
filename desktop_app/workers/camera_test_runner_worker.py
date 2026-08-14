import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from threading import Condition, Event

from PySide6.QtCore import QObject, QThread, Signal, Slot

from core.testing import TestContext, TestEvaluator, TestRunner, TestStatus
from core.testing.errors import TestCancelledError, TestTimeoutError


class SharedJetsonOperationClient(QObject):
    """Blocking facade used only in the test QThread; every SSH job remains short."""

    def __init__(self, service, camera_service, cancel_event):
        super().__init__()
        self.service, self.camera_service, self.cancel_event = service, camera_service, cancel_event
        self._condition, self._results = Condition(), {}
        service.operation_succeeded.connect(self._succeeded)
        service.operation_failed.connect(self._failed)

    @property
    def connected(self):
        return self.service.is_connected

    def camera(self, action, payload, timeout=15, cleanup=False):
        return self.call(
            f"camera_test_{action}",
            lambda ssh: self.camera_service.execute_with_ssh(ssh, action, payload), timeout, cleanup,
        )

    def daemon_active(self):
        async def operation(ssh):
            result = await ssh.run("systemctl is-active zed_x_daemon", timeout=5)
            return {"active": result.stdout.strip() == "active", "raw": result.stdout.strip() or result.stderr.strip()}
        return bool(self.call("camera_test_daemon_status", operation, 7).get("active"))

    def call(self, name, operation, timeout, ignore_cancel=False):
        request_id = self.service.submit_operation(name, operation)
        if request_id is None:
            raise RuntimeError("JETSON_NOT_CONNECTED: Jetson is not connected.")
        deadline = time.monotonic() + timeout
        with self._condition:
            while request_id not in self._results:
                if self.cancel_event.is_set() and not ignore_cancel:
                    raise TestCancelledError("Test run was cancelled.")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TestTimeoutError(f"{name} exceeded {timeout} seconds.")
                self._condition.wait(min(0.1, remaining))
            ok, value = self._results.pop(request_id)
        if not ok: raise RuntimeError(value)
        return value or {}

    @Slot(str, object)
    def _succeeded(self, request_id, result):
        with self._condition:
            self._results[request_id] = (True, result); self._condition.notify_all()

    @Slot(str, str)
    def _failed(self, request_id, error):
        with self._condition:
            self._results[request_id] = (False, error); self._condition.notify_all()

    def wake(self):
        with self._condition: self._condition.notify_all()


class CameraTestRunnerWorker(QThread):
    test_started = Signal(str)
    test_finished = Signal(str, str, object)
    log_event = Signal(str, str)
    suite_finished = Signal(object, str)

    def __init__(self, definitions, registry, service, camera_service, device, configuration, parent=None):
        super().__init__(parent)
        self.definitions, self.registry = list(definitions), registry
        self.cancel_event = Event()
        self.client = SharedJetsonOperationClient(service, camera_service, self.cancel_event)
        self.device, self.configuration = device, configuration

    def cancel(self):
        self.cancel_event.set(); self.client.wake()

    def run(self):
        run_stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        serial = str(self.device.get("serial") or "unknown")
        root = Path("evidence") / "camera" / run_stamp / serial
        runner = TestRunner(self.registry, TestEvaluator())
        results = []
        for definition in self.definitions:
            if self.cancel_event.is_set(): break
            self.test_started.emit(definition.test_id)
            self.log_event.emit("INFO", f"[{definition.test_id}] Starting {definition.name}.")
            context = TestContext(
                services={"remote_client": self.client}, device=self.device,
                base_configuration=self.configuration, result_root=str(root),
                cancel_event=self.cancel_event, log=lambda level, message: self.log_event.emit(level, message),
            )
            result = runner.run_one(definition, context); results.append(result)
            self.test_finished.emit(definition.test_id, result.status.value, result.to_dict())
            level = (
                "PASS" if result.status == TestStatus.PASS else
                "FAIL" if result.status == TestStatus.FAIL else
                "WARNING" if result.status in (TestStatus.BLOCKED, TestStatus.CANCELLED, TestStatus.SKIPPED) else
                "ERROR"
            )
            self.log_event.emit(level, f"[{definition.test_id}] {result.status.value}.")
            if result.status == TestStatus.CANCELLED: break
        counts = Counter(item.status.value for item in results)
        summary = {"total": len(results), **counts}
        self.suite_finished.emit(summary, str(root))
