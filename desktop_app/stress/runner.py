from __future__ import annotations

import json
import time
from pathlib import Path

from PySide6.QtCore import QObject, QThread, QTimer, Signal

from .baseline import SystemBaselineCollector, write_baseline_capture
from .collectors import EvidenceManager
from .load_strategy import (
    SUPPORTED_ADAPTIVE_CPU_IDS,
    LoadStrategy,
    decision_for_definition,
)
from .models import CollectorStatus, EnvironmentStatus, RuntimeStatus, StressTestDefinition
from .session import StressSessionManager, atomic_write_json, utc_now


class _ImmediateBaselineThread(QThread):
    completed = Signal(object)
    failed = Signal(str)

    def __init__(self, collector, parent=None):
        super().__init__(parent)
        self.collector = collector

    def run(self) -> None:
        try:
            capture = self.collector.collect_local(self.isInterruptionRequested)
            if not self.isInterruptionRequested():
                self.completed.emit(capture)
        except Exception as exc:
            self.failed.emit(f"{type(exc).__name__}: {exc}")


class StressTestRunner(QObject):
    queue_changed = Signal(object)
    confirmation_required = Signal(object, object)
    test_prepared = Signal(object, object)
    prepared_discarded = Signal(object)
    test_started = Signal(object, object)
    test_updated = Signal(object)
    test_finished = Signal(object, object)
    queue_finished = Signal()
    error = Signal(str)
    immediate_baseline_started = Signal(object, object)
    strategy_ready = Signal(object, object)

    def __init__(self, session_manager: StressSessionManager, *, platform: str = "VD", remote_service=None, baseline_collector=None, parent=None):
        super().__init__(parent)
        self.session_manager = session_manager
        self.platform = platform.upper()
        self.remote_service = remote_service
        self.baseline_collector = baseline_collector or SystemBaselineCollector()
        self.queue: list[StressTestDefinition] = []
        self.current: StressTestDefinition | None = None
        self.current_paths = None
        self.evidence_manager: EvidenceManager | None = None
        self.started_monotonic: float | None = None
        self.started_at_iso: str | None = None
        self._pending_final_status: RuntimeStatus | None = None
        self.current_strategy = None

        # Session-wide baseline captured by PRE-TEST ENVIRONMENT.
        # Selecting a test must not trigger another immediate baseline.
        self.pretest_baseline: dict = {}
        self.pretest_stress_ng_available: bool | None = None

        self._baseline_thread = None
        self._baseline_request_id = None
        self.elapsed_timer = QTimer(self)
        self.elapsed_timer.setInterval(1000)
        self.elapsed_timer.timeout.connect(self._emit_update)
        if self.remote_service is not None:
            self.remote_service.operation_succeeded.connect(self._remote_baseline_succeeded)
            self.remote_service.operation_failed.connect(self._remote_baseline_failed)

    @property
    def running(self) -> bool:
        return self.current is not None and self.current.runtime_status in {
            RuntimeStatus.STARTING,
            RuntimeStatus.RUNNING,
            RuntimeStatus.STOPPING,
        }

    def enqueue(
        self,
        definitions: list[StressTestDefinition],
        *,
        environment_status: EnvironmentStatus,
        override: bool = False,
        blocking_reasons: list[str] | None = None,
        baseline: dict | None = None,
        stress_ng_available: bool | None = None,
    ) -> bool:
        if self.running:
            self.error.emit(
                "A test is currently running. Stop or finish it before selecting another test."
            )
            return False
        if not definitions:
            self.error.emit("Select one or more test cases")
            return False
        reasons = list(blocking_reasons or [])
        if environment_status != EnvironmentStatus.READY and not override:
            self.error.emit("Environment is not READY. Run relevant checks or deliberately override.")
            return False
        valid, detail = self.session_manager.validate_evidence_root()
        if not valid:
            self.error.emit(detail)
            return False
        if self.current is not None:
            if self.current.runtime_status == RuntimeStatus.PREPARED:
                self.discard_prepared()
            else:
                self.error.emit("The current test cannot be replaced in its present state.")
                return False

        self.pretest_baseline = dict(baseline or {})
        self.pretest_stress_ng_available = stress_ng_available
        self.session_manager.create_session(self.platform)
        self.session_manager.update_environment(environment_status, reasons, override=override)
        self.session_manager.set_selected_tests([item.test_id for item in definitions])
        self.queue = list(definitions)
        for definition in self.queue:
            definition.runtime_status = RuntimeStatus.WAITING
        self.queue_changed.emit(self.queue)
        self._prepare_next()
        return True

    def discard_prepared(self) -> bool:
        """Discard an unstarted selection without creating an execution result."""
        if self.current is None or self.current.runtime_status != RuntimeStatus.PREPARED:
            return False

        discarded = self.current
        for definition in (self.current, *self.queue):
            if definition.runtime_status in {RuntimeStatus.PREPARED, RuntimeStatus.WAITING}:
                definition.runtime_status = RuntimeStatus.NOT_RUN

        self.current = None
        self.queue = []
        self.current_paths = None
        self.current_strategy = None
        self.evidence_manager = None
        self.started_monotonic = None
        self.started_at_iso = None
        self._pending_final_status = None
        if self.session_manager.session_dir is not None:
            self.session_manager.set_selected_tests([])
        self.queue_changed.emit([])
        self.prepared_discarded.emit(discarded)
        return True

    def _prepare_next(self) -> None:
        if not self.queue:
            self.current = None
            self.current_paths = None

            if self.session_manager.session_dir is not None:
                self.session_manager.finish_session()

            self.queue_finished.emit()
            return

        self.current = self.queue.pop(0)
        self.current.runtime_status = RuntimeStatus.PREPARED
        self.current_paths = None
        self.evidence_manager = None
        self.current_strategy = None

        self.queue_changed.emit(
            ([self.current] if self.current else []) + self.queue
        )

        # ----------------------------------------------------
        # Strategy source
        # ----------------------------------------------------
        # PRE-TEST ENVIRONMENT is the authoritative baseline
        # for this selected test. Selecting a test never starts
        # another measurement and never starts artificial load.
        self.current_strategy = decision_for_definition(
            self.current,
            self.pretest_baseline,
            self.pretest_stress_ng_available,
        )

        self.strategy_ready.emit(
            self.current,
            self.current_strategy,
        )

        self.test_prepared.emit(
            self.current,
            None,
        )

        self.test_updated.emit(self.current)

    def _begin_immediate_baseline(self) -> None:
        if self.current is None or self.current_paths is None:
            return
        self.immediate_baseline_started.emit(self.current, self.current_paths)
        if self.remote_service is not None:
            if not getattr(self.remote_service, "is_connected", False):
                self._baseline_failed("DUT is not connected; immediate baseline is unavailable")
                return
            operation = lambda ssh: self.baseline_collector.collect_remote(ssh)
            self._baseline_request_id = self.remote_service.submit_operation("stress_pre_run_baseline", operation)
            if self._baseline_request_id is None:
                self._baseline_failed("DUT became unavailable before immediate baseline")
            return
        self._baseline_thread = _ImmediateBaselineThread(self.baseline_collector, self)
        self._baseline_thread.completed.connect(self._baseline_completed)
        self._baseline_thread.failed.connect(self._baseline_failed)
        self._baseline_thread.finished.connect(self._baseline_thread_finished)
        self._baseline_thread.finished.connect(self._baseline_thread.deleteLater)
        self._baseline_thread.start()

    def _baseline_thread_finished(self) -> None:
        self._baseline_thread = None

    def _remote_baseline_succeeded(self, request_id: str, capture) -> None:
        if request_id == self._baseline_request_id:
            self._baseline_request_id = None
            self._baseline_completed(capture)

    def _remote_baseline_failed(self, request_id: str, error: str) -> None:
        if request_id == self._baseline_request_id:
            self._baseline_request_id = None
            self._baseline_failed(error)

    def _baseline_completed(self, capture) -> None:
        if self.current is None or self.current_paths is None or self.current.runtime_status != RuntimeStatus.STARTING:
            return
        write_baseline_capture(
            capture,
            json_path=self.current_paths.attempt_dir / "pre_run_baseline.json",
            metrics_path=self.current_paths.logs_dir / "pre_run_system_metrics.log",
            tegrastats_path=self.current_paths.logs_dir / "pre_run_tegrastats.log",
        )
        self.current_strategy = decision_for_definition(
            self.current, capture.data, capture.stress_ng_available
        )
        atomic_write_json(
            self.current_paths.attempt_dir / "load_strategy.json",
            self.current_strategy.to_dict(),
        )
        info_path = self.current_paths.attempt_dir / "test_info.json"
        info = json.loads(info_path.read_text(encoding="utf-8"))
        info.update(
            immediate_baseline="pre_run_baseline.json",
            load_strategy=self.current_strategy.strategy.value,
            target_cpu_percent=self.current_strategy.target_percent,
        )
        atomic_write_json(info_path, info)
        self.strategy_ready.emit(self.current, self.current_strategy)
        self.confirmation_required.emit(self.current, self.current_paths)

    def _baseline_failed(self, error: str) -> None:
        if self.current is None or self.current_paths is None or self.current.runtime_status != RuntimeStatus.STARTING:
            return
        self.current_strategy = decision_for_definition(self.current, {}, False)
        atomic_write_json(self.current_paths.attempt_dir / "load_strategy.json", self.current_strategy.to_dict())
        self.error.emit(f"Immediate baseline failed: {error}")
        self.strategy_ready.emit(self.current, self.current_strategy)
        self.confirmation_required.emit(self.current, self.current_paths)

    def start_current(self) -> bool:
        if (
            self.current is None
            or self.current.runtime_status != RuntimeStatus.PREPARED
        ):
            return False
        if self.current.test_id in SUPPORTED_ADAPTIVE_CPU_IDS:
            if self.current_strategy is None or not self.current_strategy.can_start:
                reason = self.current_strategy.reason if self.current_strategy else "Immediate baseline is not complete"
                if self.current_strategy and self.current_strategy.strategy == LoadStrategy.LOAD_ASSIST and self.current_strategy.stress_ng_available is not True:
                    reason = "stress-ng is required for LOAD_ASSIST but is not available on the DUT"
                self.error.emit(reason)
                return False
        self.current.runtime_status = RuntimeStatus.STARTING
        try:
            self.current_paths = self.session_manager.create_attempt(self.current)
            atomic_write_json(
                self.current_paths.attempt_dir / "load_strategy.json",
                self.current_strategy.to_dict() if self.current_strategy else {},
            )
            atomic_write_json(
                self.current_paths.attempt_dir / "pretest_baseline.json",
                self.pretest_baseline,
            )
            self.evidence_manager = EvidenceManager(
                self.current,
                self.current_paths.attempt_dir,
                remote_service=self.remote_service,
                strategy_decision=self.current_strategy,
                parent=self,
            )
            self.evidence_manager.evidence_changed.connect(self._on_evidence_changed)
            self.evidence_manager.all_stopped.connect(self._on_all_stopped)
            self.session_manager.update_manifest(
                self.current_paths,
                self.evidence_manager.records,
                self.current.test_id,
            )
        except Exception as exc:
            self.current.runtime_status = RuntimeStatus.PREPARED
            self.current_paths = None
            self.evidence_manager = None
            self.error.emit(f"Unable to create stress test attempt: {type(exc).__name__}: {exc}")
            return False

        self.started_monotonic = time.monotonic()
        self.started_at_iso = utc_now()
        self.current.runtime_status = RuntimeStatus.RUNNING
        self._pending_final_status = None
        info_path = self.current_paths.attempt_dir / "test_info.json"
        info = json.loads(info_path.read_text(encoding="utf-8"))
        info.update(
            start_time=self.started_at_iso,
            status=RuntimeStatus.RUNNING.value,
            baseline_source="PRE_TEST",
            baseline_file="pretest_baseline.json",
            load_strategy=self.current_strategy.strategy.value if self.current_strategy else None,
            target_cpu_percent=self.current_strategy.target_percent if self.current_strategy else None,
        )
        atomic_write_json(info_path, info)
        self.elapsed_timer.start()
        self.test_started.emit(self.current, self.evidence_manager)
        self.evidence_manager.start_all()
        return True

    def finish_for_review(self) -> None:
        if self.current is None or self.current.runtime_status != RuntimeStatus.RUNNING:
            return
        self._pending_final_status = RuntimeStatus.NEEDS_REVIEW
        self.current.runtime_status = RuntimeStatus.STOPPING
        if self.evidence_manager:
            self.evidence_manager.stop_all()
        else:
            self._finalize_current(RuntimeStatus.NEEDS_REVIEW)

    def stop(self) -> None:
        if self.current is None or self.current.runtime_status not in {RuntimeStatus.STARTING, RuntimeStatus.RUNNING}:
            return
        if self._baseline_thread and self._baseline_thread.isRunning():
            self._baseline_thread.requestInterruption()
        self._pending_final_status = RuntimeStatus.STOPPED
        self.current.runtime_status = RuntimeStatus.STOPPING
        self.test_updated.emit(self.current)
        if self.evidence_manager:
            self.evidence_manager.stop_all()
        else:
            self._finalize_current(RuntimeStatus.STOPPED)

    def _on_evidence_changed(self, record) -> None:
        if self.current_paths and self.evidence_manager and self.current:
            self.session_manager.update_manifest(self.current_paths, self.evidence_manager.records, self.current.test_id)
        self.test_updated.emit(self.current)
        if (
            self.current_strategy is not None
            and self.current_strategy.strategy == LoadStrategy.LOAD_ASSIST
            and record.id != "workload"
            and record.status == CollectorStatus.ERROR
            and self.current
            and self.current.runtime_status == RuntimeStatus.RUNNING
        ):
            self._pending_final_status = RuntimeStatus.ERROR
            self.current.runtime_status = RuntimeStatus.STOPPING
            self.evidence_manager.stop_all()
            return
        if (
            self.current
            and self.current.execution_type.value == "AUTO"
            and record.id == "workload"
            and record.status in {CollectorStatus.COMPLETED, CollectorStatus.ERROR}
            and self.current.runtime_status == RuntimeStatus.RUNNING
        ):
            self._pending_final_status = RuntimeStatus.NEEDS_REVIEW if record.status == CollectorStatus.COMPLETED else RuntimeStatus.ERROR
            self.current.runtime_status = RuntimeStatus.STOPPING
            self.evidence_manager.stop_all()

    def _on_all_stopped(self) -> None:
        if self.current and self.current.runtime_status == RuntimeStatus.STOPPING:
            self._finalize_current(self._pending_final_status or RuntimeStatus.NEEDS_REVIEW)

    def _finalize_current(self, status: RuntimeStatus) -> None:
        if self.current is None or self.current_paths is None:
            return
        self.elapsed_timer.stop()
        elapsed = max(0.0, time.monotonic() - self.started_monotonic) if self.started_monotonic is not None else 0.0
        end_time = utc_now()
        self.current.runtime_status = status
        records = self.evidence_manager.records if self.evidence_manager else []
        self.session_manager.update_manifest(self.current_paths, records, self.current.test_id)
        warnings = [record.error or record.name for record in records if record.status == CollectorStatus.WARNING]
        errors = [record.error or record.name for record in records if record.status == CollectorStatus.ERROR]
        # A completed execution always owns a portable snapshot, even when the
        # runner is used without the Qt dashboard.  The UI enriches this same
        # file from its already-parsed live state when it handles test_finished.
        atomic_write_json(
            self.current_paths.attempt_dir / "final_metrics.json",
            {
                "captured_at": end_time,
                "elapsed_sec": round(elapsed, 3),
                "test": {
                    "test_id": self.current.test_id,
                    "test_name": self.current.test_name,
                    "group": self.current.group,
                    "duration_sec": self.current.duration_seconds,
                    "execution_type": self.current.execution_type.value,
                },
                "case_monitor": {},
                "system": {},
                "trends": {},
                "warning_count": len(warnings),
                "error_count": len(errors),
                "warnings": warnings,
                "errors": errors,
            },
        )
        result = {
            "test_id": self.current.test_id,
            "attempt": self.current_paths.attempt,
            "status": status.value,
            "start_time": self.started_at_iso,
            "end_time": end_time,
            "elapsed_sec": round(elapsed, 3),
            "acceptance_summary": "Manual/external evidence review required; no automatic PASS assigned.",
            "warnings": warnings,
            "errors": errors,
            "evidence_manifest": "evidence_manifest.json",
            "load_strategy": self.current_strategy.strategy.value if self.current_strategy else None,
            "review_status": None,
            "review_comment": "",
            "reviewed_at": None,
            "final_metrics_file": "final_metrics.json",
        }
        self.session_manager.write_result(self.current_paths, result)
        info_path = self.current_paths.attempt_dir / "test_info.json"
        info = json.loads(info_path.read_text(encoding="utf-8"))
        info.update(end_time=end_time, status=status.value, final_metrics_file="final_metrics.json")
        atomic_write_json(info_path, info)
        finished_definition = self.current
        finished_paths = self.current_paths
        finished_manager = self.evidence_manager
        self.evidence_manager = None
        self.started_monotonic = None
        self.started_at_iso = None
        self.test_finished.emit(finished_definition, finished_paths)
        if finished_manager is not None:
            finished_manager.deleteLater()
        QTimer.singleShot(0, self._prepare_next)

    def shutdown(self) -> None:
        if self._baseline_thread and self._baseline_thread.isRunning():
            self._baseline_thread.requestInterruption()
            self._baseline_thread.wait(2000)

    def elapsed_seconds(self) -> int:
        if self.started_monotonic is None:
            return 0
        return int(max(0.0, time.monotonic() - self.started_monotonic))

    def _emit_update(self) -> None:
        if self.current:
            self.test_updated.emit(self.current)


def scan_history(evidence_root: str | Path, test_id: str) -> list[dict]:
    root = Path(evidence_root).expanduser()
    items = []
    if not root.is_dir():
        return items
    for session_dir in sorted(root.glob("*_*"), reverse=True):
        test_dir = session_dir / "tests" / test_id
        if not test_dir.is_dir():
            continue
        for attempt_dir in sorted(test_dir.glob("attempt_*"), reverse=True):
            result_path = attempt_dir / "result.json"
            info_path = attempt_dir / "test_info.json"
            data = {}
            for path in (info_path, result_path):
                try:
                    data.update(json.loads(path.read_text(encoding="utf-8")))
                except (OSError, json.JSONDecodeError):
                    pass
            items.append(
                {
                    "session": session_dir.name,
                    "attempt": data.get("attempt", attempt_dir.name.removeprefix("attempt_")),
                    "start_time": data.get("start_time"),
                    "end_time": data.get("end_time"),
                    "status": data.get("status", "UNKNOWN"),
                    "evidence_folder": str(attempt_dir),
                }
            )
    return items
