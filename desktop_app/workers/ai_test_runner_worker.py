from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from threading import Event

from PySide6.QtCore import QThread, Signal

from core.testing import TestContext, TestEvaluator, TestRunner, TestStatus
from desktop_app.workers.camera_test_runner_worker import SharedJetsonOperationClient
from devices.ai import AiRemoteProcessManager, AiRuntimeAdapter


class AiTestRunnerWorker(QThread):
    """Runs AI tests in one worker using the existing shared Jetson queue."""
    test_started = Signal(str)
    test_finished = Signal(str, str, object)
    log_event = Signal(str, str)
    suite_finished = Signal(object, str)

    def __init__(self, definitions, registry, jetson_service, modules, target_scope, parent=None):
        super().__init__(parent)
        self.definitions, self.registry = list(definitions), registry
        self.modules, self.target_scope = tuple(modules), str(target_scope)
        self.cancel_event = Event()
        self.client = SharedJetsonOperationClient(jetson_service, None, self.cancel_event)
        self.adapter = AiRuntimeAdapter()
        self.manager = AiRemoteProcessManager(self.client)

    def cancel(self):
        self.cancel_event.set(); self.client.wake()

    def run(self):
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        target = self.modules[0].module_uid if len(self.modules) == 1 else "ALL_MODULES"
        root = Path("evidence") / "ai" / stamp / target
        runner, results = TestRunner(self.registry, TestEvaluator()), []
        snapshot = {"target_scope": self.target_scope, "ai_modules": [item.to_dict() for item in self.modules]}
        for definition in self.definitions:
            if self.cancel_event.is_set(): break
            self.test_started.emit(definition.test_id)
            self.log_event.emit("INFO", f"[{definition.test_id}] Starting {definition.name}.")
            context = TestContext(
                services={"remote_client": self.client, "selected_ai_modules": self.modules,
                          "ai_runtime_adapter": self.adapter, "ai_process_manager": self.manager},
                device=snapshot, base_configuration={"target_scope": self.target_scope},
                result_root=str(root), cancel_event=self.cancel_event,
                log=lambda level, message: self.log_event.emit(level, message),
            )
            result = runner.run_one(definition, context); results.append(result)
            self.test_finished.emit(definition.test_id, result.status.value, result.to_dict())
            level = "PASS" if result.status == TestStatus.PASS else "FAIL" if result.status == TestStatus.FAIL else "WARNING" if result.status in (TestStatus.BLOCKED, TestStatus.CANCELLED, TestStatus.SKIPPED) else "ERROR"
            self.log_event.emit(level, f"[{definition.test_id}] {result.status.value}.")
            if result.status == TestStatus.CANCELLED: break
        self.suite_finished.emit({"total": len(results), **Counter(item.status.value for item in results)}, str(root))
