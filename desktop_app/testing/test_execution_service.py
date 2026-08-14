from __future__ import annotations

import time
from collections import Counter

from PySide6.QtCore import QObject, QTimer, Signal

from desktop_app.state.lidar_runtime_state import LidarStreamStatus
from desktop_app.testing.evidence_store import EvidenceStore
from desktop_app.testing.test_result import (
    TestOutcome,
    TestResult,
    TestStatus,
)


class TestExecutionService(QObject):
    """Sequential, signal-driven test runner with shared resource ownership."""

    plan_started = Signal(str, list)
    case_status_changed = Signal(str, str)
    result_ready = Signal(object)
    plan_finished = Signal(dict)
    running_changed = Signal(bool)
    log = Signal(str, str)

    def __init__(self, registry, evidence_store=None, parent=None):
        super().__init__(parent)
        self.registry = registry
        self.evidence_store = evidence_store or EvidenceStore()
        self.running = False
        self.session_id = None
        self.context = None
        self._definitions = []
        self._pending = []
        self._results = {}
        self._current_definition = None
        self._current_executor = None
        self._current_started = None
        self._current_logs = []
        self._cancel_requested = False
        self._phase = "IDLE"
        self._stream_was_running_before_tests = False
        self._runner_started_stream = False
        self._stream_failure = None
        self._case_timeout = QTimer(self)
        self._case_timeout.setSingleShot(True)
        self._case_timeout.timeout.connect(self._on_case_timeout)
        self._resource_timeout = QTimer(self)
        self._resource_timeout.setSingleShot(True)
        self._resource_timeout.timeout.connect(self._on_resource_timeout)

    @property
    def runner_started_stream(self) -> bool:
        return self._runner_started_stream

    @property
    def stream_was_running_before_tests(self) -> bool:
        return self._stream_was_running_before_tests

    def result(self, test_id: str) -> TestResult | None:
        return self._results.get(test_id)

    def results(self) -> dict[str, TestResult]:
        return dict(self._results)

    def start(self, test_ids: list[str], context) -> bool:
        if self.running:
            self._emit_log("WARNING", "A test plan is already running")
            return False
        definitions = self.registry.resolve(test_ids)
        if not definitions:
            return False
        try:
            session_id = self.evidence_store.begin_session(context.device)
        except Exception as exc:
            self._emit_log(
                "ERROR",
                f"Unable to create evidence session: {type(exc).__name__}: {exc}",
            )
            return False

        self.running = True
        self.session_id = session_id
        self.context = context
        self._definitions = definitions
        self._pending = list(definitions)
        self._results = {}
        self._current_definition = None
        self._current_executor = None
        self._cancel_requested = False
        self._phase = "PREPARING"
        self._stream_was_running_before_tests = context.stream_process_active
        self._runner_started_stream = False
        self._stream_failure = None
        for definition in definitions:
            result = TestResult(definition.id)
            result.queue()
            self._results[definition.id] = result
            self.case_status_changed.emit(
                definition.id,
                TestStatus.QUEUED.value,
            )
            self._emit_log("INFO", f"{definition.id} queued")
        self.running_changed.emit(True)
        self.plan_started.emit(session_id, [item.id for item in definitions])
        self._emit_log("INFO", f"Test plan started: {len(definitions)} cases")
        QTimer.singleShot(0, self._prepare_discovery)
        return True

    def cancel(self) -> bool:
        if not self.running or self._cancel_requested:
            return False
        self._cancel_requested = True
        self._emit_log("WARNING", "Test plan cancellation requested")
        self._case_timeout.stop()
        self._resource_timeout.stop()
        if self._phase == "DISCOVERY":
            self._disconnect_discovery()
        if self._current_executor is not None:
            self._current_executor.cancel()
            self._complete_current(
                TestOutcome(
                    TestStatus.CANCELLED,
                    "Test cancelled by user.",
                )
            )
            return True
        self._cancel_pending()
        self._cleanup_resources()
        return True

    def _prepare_discovery(self) -> None:
        if self._cancel_requested:
            self._cancel_pending()
            self._cleanup_resources()
            return
        needs_device = any(
            item.requires_device or item.requires_stream
            for item in self._definitions
        )
        if not needs_device or self.context.device_ready:
            self._prepare_stream()
            return
        if not self.context.jetson_connected:
            self._emit_log(
                "WARNING",
                "Auto Discover skipped because Jetson is disconnected",
            )
            self._prepare_stream()
            return

        service = self.context.lidar_discovery_service
        self._phase = "DISCOVERY"
        service.completed.connect(self._on_discovery_completed)
        service.failed.connect(self._on_discovery_failed)
        self._emit_log("INFO", "Runner requesting shared Auto Discover")
        if service.busy:
            self._emit_log("INFO", "Reusing active Auto Discover workflow")
            return
        if not service.start(self.context.selected_model, timeout=8):
            self._disconnect_discovery()
            self._emit_log("ERROR", "Unable to start shared Auto Discover")
            self._prepare_stream()

    def _on_discovery_completed(self, result: dict) -> None:
        if self._phase != "DISCOVERY":
            return
        self._disconnect_discovery()
        self.context.update_discovery(result)
        if result.get("found"):
            self._emit_log("PASS", "Shared Auto Discover completed")
        else:
            self._emit_log(
                "WARNING",
                "Shared Auto Discover did not produce a ready device: "
                f"{result.get('status') or 'UNKNOWN'}",
            )
        QTimer.singleShot(0, self._prepare_stream)

    def _on_discovery_failed(self, error: str) -> None:
        if self._phase != "DISCOVERY":
            return
        self._disconnect_discovery()
        self.context.update_discovery(
            {
                "found": False,
                "status": "DISCOVERY_ERROR",
                "reason": error,
            }
        )
        self._emit_log("ERROR", f"Shared Auto Discover failed: {error}")
        QTimer.singleShot(0, self._prepare_stream)

    def _disconnect_discovery(self) -> None:
        service = self.context.lidar_discovery_service
        for signal, slot in (
            (service.completed, self._on_discovery_completed),
            (service.failed, self._on_discovery_failed),
        ):
            try:
                signal.disconnect(slot)
            except (RuntimeError, TypeError):
                pass

    def _prepare_stream(self) -> None:
        if self._cancel_requested:
            self._cancel_pending()
            self._cleanup_resources()
            return
        needs_stream = any(item.requires_stream for item in self._definitions)
        if not needs_stream:
            self._run_next()
            return
        if self._stream_was_running_before_tests:
            self._emit_log("INFO", "Reusing user-owned LiDAR stream")
            self._run_next()
            return
        if not self.context.device_ready:
            self._stream_failure = "LiDAR device is not ready"
            self._emit_log("WARNING", self._stream_failure)
            self._run_next()
            return

        self._phase = "STARTING_STREAM"
        self.context.lidar_runtime_state.changed.connect(
            self._on_runtime_changed
        )
        started = self.context.lidar_stream_service.start(
            self.context.selected_model
        )
        if not started:
            self._disconnect_runtime()
            self._stream_failure = "LiDAR stream failed to start"
            self._emit_log("ERROR", self._stream_failure)
            self._run_next()
            return
        self._runner_started_stream = True
        timeout_ms = int(
            self.context.network_profile.testing.stream_start_timeout_sec
            * 1000
        )
        self._resource_timeout.start(timeout_ms)
        self._emit_log("INFO", "Runner started one shared LiDAR stream")

    def _on_runtime_changed(self, runtime: dict) -> None:
        status = runtime.get("stream_status")
        if self._phase == "STARTING_STREAM":
            if status == LidarStreamStatus.STREAMING.value:
                self._resource_timeout.stop()
                self._disconnect_runtime()
                self._emit_log("PASS", "Runner-owned stream is receiving data")
                QTimer.singleShot(0, self._run_next)
            elif status == LidarStreamStatus.ERROR.value:
                self._resource_timeout.stop()
                self._disconnect_runtime()
                self._stream_failure = (
                    runtime.get("last_error") or "LiDAR stream entered ERROR"
                )
                self._emit_log("ERROR", self._stream_failure)
                QTimer.singleShot(0, self._run_next)
        elif self._phase == "STOPPING_STREAM" and status in {
            LidarStreamStatus.IDLE.value,
            LidarStreamStatus.ERROR.value,
        }:
            self._resource_timeout.stop()
            self._disconnect_runtime()
            self._finalize_plan()

    def _on_resource_timeout(self) -> None:
        if self._phase == "STARTING_STREAM":
            self._disconnect_runtime()
            self._stream_failure = "Timed out waiting for first stream data"
            self._emit_log("ERROR", self._stream_failure)
            if self._runner_started_stream:
                self.context.lidar_stream_service.stop()
            self._run_next()
        elif self._phase == "STOPPING_STREAM":
            self._disconnect_runtime()
            self._emit_log("ERROR", "Timed out waiting for stream cleanup")
            self._finalize_plan()

    def _disconnect_runtime(self) -> None:
        try:
            self.context.lidar_runtime_state.changed.disconnect(
                self._on_runtime_changed
            )
        except (RuntimeError, TypeError):
            pass

    def _run_next(self) -> None:
        self._phase = "EXECUTING"
        if self._cancel_requested:
            self._cancel_pending()
            self._cleanup_resources()
            return
        if not self._pending:
            self._cleanup_resources()
            return
        definition = self._pending.pop(0)
        prerequisite = self._missing_prerequisite(definition)
        if prerequisite:
            self._current_definition = definition
            self._current_started = time.monotonic()
            self._complete_current(
                TestOutcome(
                    TestStatus.SKIPPED,
                    f"Skipped: {prerequisite}.",
                    {"missing_prerequisite": prerequisite},
                )
            )
            return

        self._current_definition = definition
        self._current_started = time.monotonic()
        self._current_logs = []
        result = self._results[definition.id]
        result.start()
        self.case_status_changed.emit(definition.id, TestStatus.RUNNING.value)
        self._emit_log("INFO", f"Starting {definition.id} {definition.name}")
        executor = definition.executor(self)
        self._current_executor = executor
        executor.log.connect(self._emit_log)
        executor.finished.connect(self._complete_current)
        self._case_timeout.start(max(1, int(definition.timeout_sec * 1000)))
        try:
            executor.start(definition, self.context)
        except Exception as exc:
            self._complete_current(
                TestOutcome(
                    TestStatus.ERROR,
                    f"Executor failed to start: {type(exc).__name__}: {exc}",
                    error=f"{type(exc).__name__}: {exc}",
                )
            )

    def _missing_prerequisite(self, definition) -> str | None:
        if definition.requires_jetson and not self.context.jetson_connected:
            return "Jetson is not connected"
        if definition.requires_network and not self.context.network_ready:
            return "LiDAR network is not ready"
        if definition.requires_device and not self.context.device_ready:
            return "LiDAR device is not discovered"
        if definition.requires_stream and not self.context.stream_running:
            return self._stream_failure or "LiDAR stream is not receiving data"
        return None

    def _on_case_timeout(self) -> None:
        if self._current_executor is not None:
            self._current_executor.cancel()
        timeout = self._current_definition.timeout_sec
        self._complete_current(
            TestOutcome(
                TestStatus.ERROR,
                f"Test timed out after {timeout:.1f} seconds.",
                {"timeout_sec": timeout},
                error=f"Timeout after {timeout:.1f} seconds",
            )
        )

    def _complete_current(self, outcome: TestOutcome) -> None:
        definition = self._current_definition
        if definition is None:
            return
        self._case_timeout.stop()
        elapsed = max(
            0.0,
            time.monotonic()
            - (self._current_started or time.monotonic()),
        )
        result = self._results[definition.id]
        result.complete(outcome, elapsed)
        level = {
            TestStatus.PASS: "PASS",
            TestStatus.FAIL: "FAIL",
            TestStatus.ERROR: "ERROR",
            TestStatus.SKIPPED: "WARNING",
            TestStatus.CANCELLED: "WARNING",
        }[result.status]
        final_message = (
            f"{definition.id} {definition.name}: {result.actual_result}"
        )
        self._current_logs.append(f"{level}  {final_message}")
        try:
            self.evidence_store.write_result(
                self.session_id,
                self.context.device,
                definition,
                self.context,
                result,
                self._current_logs,
            )
        except Exception as exc:
            evidence_error = f"Evidence write failed: {type(exc).__name__}: {exc}"
            result.status = TestStatus.ERROR
            result.error = evidence_error
            result.actual_result = evidence_error
            level = "ERROR"
            final_message = evidence_error
            self._emit_log("ERROR", evidence_error)
        self.case_status_changed.emit(definition.id, result.status.value)
        self.result_ready.emit(result)
        self._emit_log(level, final_message)
        self._current_definition = None
        self._current_executor = None
        self._current_started = None
        self._current_logs = []
        if self._cancel_requested:
            self._cancel_pending()
            self._cleanup_resources()
        else:
            QTimer.singleShot(0, self._run_next)

    def _cancel_pending(self) -> None:
        pending = list(self._pending)
        self._pending.clear()
        for definition in pending:
            result = self._results[definition.id]
            result.start()
            result.complete(
                TestOutcome(
                    TestStatus.CANCELLED,
                    "Test cancelled before execution.",
                ),
                0.0,
            )
            try:
                self.evidence_store.write_result(
                    self.session_id,
                    self.context.device,
                    definition,
                    self.context,
                    result,
                    ["WARNING  Test cancelled before execution."],
                )
            except Exception as exc:
                evidence_error = (
                    f"Evidence write failed: {type(exc).__name__}: {exc}"
                )
                result.status = TestStatus.ERROR
                result.error = evidence_error
                result.actual_result = evidence_error
            self.case_status_changed.emit(definition.id, result.status.value)
            self.result_ready.emit(result)

    def _cleanup_resources(self) -> None:
        if not self.running:
            return
        if self._runner_started_stream and self.context.lidar_stream_service.active:
            self._phase = "STOPPING_STREAM"
            self.context.lidar_runtime_state.changed.connect(
                self._on_runtime_changed
            )
            self._resource_timeout.start(10000)
            self._emit_log("INFO", "Stopping runner-owned LiDAR stream")
            self.context.lidar_stream_service.stop()
            return
        self._finalize_plan()

    def _finalize_plan(self) -> None:
        if not self.running:
            return
        counts = Counter(result.status.value for result in self._results.values())
        summary = {
            "session_id": self.session_id,
            "counts": dict(counts),
            "stream_was_running_before_tests": (
                self._stream_was_running_before_tests
            ),
            "runner_started_stream": self._runner_started_stream,
            "cancelled": self._cancel_requested,
        }
        self._emit_log(
            "INFO",
            "Test plan finished: "
            + ", ".join(
                f"{status}={count}" for status, count in sorted(counts.items())
            ),
        )
        self.running = False
        self._phase = "IDLE"
        self.running_changed.emit(False)
        self.plan_finished.emit(summary)

    def _emit_log(self, level: str, message: str) -> None:
        if self._current_definition is not None:
            self._current_logs.append(f"{level.upper()}  {message}")
        self.log.emit(level, message)
