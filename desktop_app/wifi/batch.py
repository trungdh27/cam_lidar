"""Runtime-owned sequential AUTO queue. Skips are not test attempts."""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field

from PySide6.QtCore import QObject, QTimer

from .auto_suite import readiness_status, missing_prerequisites
from .catalog import WifiTestCase


ELIGIBLE = frozenset({"READY", "READY_WITH_RECONFIG"})


@dataclass
class BatchEntry:
    case: WifiTestCase
    readiness: str = ""
    status: str = "WAITING"
    reason: str = ""
    attempt: str = ""


@dataclass
class BatchState:
    environment: str
    group: str
    entries: list[BatchEntry]
    identifier: str = field(default_factory=lambda: uuid.uuid4().hex)
    phase: str = "PRECHECK"
    status: str = "RUNNING"
    started: float = field(default_factory=time.monotonic)
    finished: float | None = None
    current: BatchEntry | None = None
    queue: tuple[BatchEntry, ...] = ()
    position: int = 0
    stop_requested: bool = False
    reason: str = ""
    original: dict = field(default_factory=dict)

    @property
    def running(self):
        return self.finished is None

    @property
    def elapsed(self):
        return int((self.finished or time.monotonic()) - self.started)

    @property
    def counts(self):
        names = ("PASS", "FAIL", "ERROR", "MEASUREMENT ERROR", "SKIPPED", "STOPPED", "NEEDS REVIEW")
        counts = {name: sum(entry.status == name for entry in self.entries) for name in names}
        counts["REMAINING"] = sum(entry.status == "WAITING" for entry in self.entries)
        return counts


class WifiBatchRunner(QObject):
    """Advance on discovery/operation completion, never on UI repaint signals."""

    def __init__(self, runtime, environment, cases, hooks=None):
        super().__init__(runtime)
        groups = {case.catalog_group for case in cases}
        if len(groups) != 1 or any(case.suite != "AUTO" for case in cases):
            raise ValueError("An AUTO batch must belong to one current subgroup")
        self.runtime = runtime
        self.state = BatchState(environment, groups.pop(), [BatchEntry(case) for case in cases])
        if hooks is None:
            from .batch_network import BatchNetwork
            hooks = BatchNetwork(runtime)
        self.hooks = hooks
        self._stage = "initial"
        self._operation_id = None
        self._operation_done = None
        runtime.auto_discovery_completed.connect(self._discovered)
        runtime.auto_attempt_completed.connect(self._completed)
        service = runtime.services.get("Jetson")
        if service:
            service.operation_succeeded.connect(self._succeeded)
            service.operation_failed.connect(self._failed)

    def publish(self):
        state = self.state
        directory = (self.runtime.evidence_root / state.environment / "auto" / state.group /
                     self.runtime.session / ("batch_" + state.identifier))
        directory.mkdir(parents=True, exist_ok=True)
        record = {"group": state.group, "status": state.status, "phase": state.phase,
                  "elapsed": state.elapsed, "reason": state.reason, "counts": state.counts,
                  "original_network": state.original,
                  "membership": [entry.case.test_id for entry in state.queue],
                  "entries": [{"test_id": entry.case.test_id, "readiness": entry.readiness,
                               "status": entry.status, "reason": entry.reason,
                               "attempt": entry.attempt} for entry in state.entries]}
        (directory / "batch.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
        self.runtime.changed.emit()

    def begin(self):
        self.publish()
        self._refresh("initial")

    def _preflight(self, entry, setup):
        try:
            ready = readiness_status(entry.case, setup)
            reason = "; ".join(missing_prerequisites(entry.case, setup)) if ready not in ELIGIBLE else ""
            check = getattr(self.hooks, "preflight_reason", None)
            if ready in ELIGIBLE and check:
                reason = check(entry.case, setup) or ""
                if reason:
                    ready = "BLOCKED"
            return ready, reason or (ready if ready not in ELIGIBLE else "")
        except Exception as error:
            return "ERROR", f"Preflight evaluation error ({type(error).__name__}); no attempt started"

    def _refresh(self, stage):
        self._stage = stage
        self.state.phase = "PRECHECK"
        self.publish()
        request = self.runtime.refresh_auto_discovery()
        if not request:
            QTimer.singleShot(0, lambda: self._discovered(False, "Shared control channel unavailable"))

    def _discovered(self, success, reason):
        if not self.state.running or self._stage not in {"initial", "next"}:
            return
        stage, self._stage = self._stage, ""
        if self.state.stop_requested:
            self._finish("STOPPED", "Stopped by user")
            return
        if not success:
            self._finish("ABORTED", reason or "Runtime refresh failed; control path unavailable")
            return
        setup = self.runtime.effective_auto_setup()
        if stage == "initial":
            for entry in self.state.entries:
                entry.readiness, reason = self._preflight(entry, setup)
                if entry.readiness not in ELIGIBLE:
                    entry.status = "SKIPPED"
                    entry.reason = reason
            # Membership is immutable even if preparation changes readiness later.
            self.state.queue = tuple(entry for entry in self.state.entries if entry.status == "WAITING")
            self.state.phase = "PREPARING"
            self.publish()
            if not self.state.queue:
                self._finish("COMPLETED", "No eligible tests in this subgroup")
                return
            self._operation("snapshot", self.hooks.snapshot, self._snapshot_done)
        else:
            entry = self.state.current
            ready, reason = self._preflight(entry, setup)
            if ready not in ELIGIBLE:
                entry.status = "SKIPPED"
                entry.reason = reason
                self.publish()
                QTimer.singleShot(0, self._next)
                return
            self.state.phase = "PREPARING"
            self._stage = "attempt"
            self.publish()
            attempt = self.runtime.start(self.state.environment, entry.case,
                                         _discovery_refreshed=True, _batch_owned=True)
            if attempt:
                entry.attempt = str(attempt.directory)
                if entry.status == "WAITING":
                    entry.status = "RUNNING"
                self.publish()
            elif self._stage == "attempt":
                entry.status, entry.reason = "SKIPPED", "; ".join(self.runtime.block_reasons(entry.case)) or "Preflight no longer ready"
                self._stage = ""
                QTimer.singleShot(0, self._next)

    def _operation(self, name, operation, done):
        self._operation_done = done
        service = self.runtime.services.get("Jetson")
        self._operation_id = service.submit_operation("wifi-batch-" + name, operation) if service else None
        if not self._operation_id:
            self._operation_done = None
            self._operation_error("Shared control channel unavailable during " + name)

    def _succeeded(self, request_id, result):
        if request_id != self._operation_id or not self._operation_id:
            return
        done = self._operation_done
        self._operation_id = self._operation_done = None
        done(result)

    def _failed(self, request_id, _reason):
        if request_id == self._operation_id and self._operation_id:
            self._operation_id = self._operation_done = None
            # Do not publish an arbitrary SSH exception which may contain credentials.
            self._operation_error("Batch network operation failed; original state restoration requires review")

    def _operation_error(self, reason):
        if self._stage == "restoring":
            self.state.reason = (self.state.reason + "; " + reason).strip("; ")
            self.state.status = "ERROR"
            self._restored(None)
        else:
            self._finish("ABORTED", reason)

    def _snapshot_done(self, original):
        self.state.original = original or {}
        if self.state.stop_requested:
            self._finish("STOPPED", "Stopped by user")
            return
        setup = self.runtime.effective_auto_setup()
        for entry in self.state.queue:
            ready, reason = self._preflight(entry, setup)
            if ready not in ELIGIBLE:
                entry.status, entry.reason = "SKIPPED", reason
        self.state.phase = "CONFIGURING"
        self.publish()
        self._operation("prepare", lambda ssh: self.hooks.prepare(ssh, self.state), self._prepared)

    def _prepared(self, _result):
        if self.state.stop_requested:
            self._finish("STOPPED", "Stopped by user")
        else:
            QTimer.singleShot(0, self._next)

    def _next(self):
        if not self.state.running or self._stage == "restoring":
            return
        self.state.current = None
        if self.state.stop_requested:
            self._finish("STOPPED", "Stopped by user")
        elif self.runtime._batch_control_lost.is_set():
            self._finish("ABORTED", "Shared control path could not be restored")
        elif self.state.position == len(self.state.queue):
            self._finish("COMPLETED", "Batch finished; individual results are listed separately")
        else:
            self.state.current = self.state.queue[self.state.position]
            self.state.position += 1
            if self.state.current.status == "SKIPPED":
                self.publish()
                QTimer.singleShot(0, self._next)
            else:
                self._refresh("next")

    def _completed(self, attempt):
        entry = self.state.current
        if not self.state.running or self._stage != "attempt" or not entry or attempt.case.test_id != entry.case.test_id:
            return
        self._stage = ""
        entry.attempt = str(attempt.directory)
        entry.status, entry.reason = attempt.final_result, attempt.result_reason
        self.state.phase = "CLEANUP"
        self.publish()
        # Collector completion is emitted only after its finally/owned cleanup.
        QTimer.singleShot(0, self._next)

    def stop(self):
        if not self.state.running:
            return
        self.state.stop_requested = True
        self.publish()
        if self._stage == "attempt":
            self.runtime.stop_auto()
        elif not self._operation_id and not self.runtime.discovery_pending:
            self._finish("STOPPED", "Stopped by user")

    def _finish(self, status, reason):
        if self._stage == "restoring" or not self.state.running:
            return
        self.state.status, self.state.reason = status, reason
        for entry in self.state.entries:
            if entry.status == "WAITING":
                entry.status, entry.reason = "SKIPPED", reason
        self._stage = "restoring"
        self.state.phase = "RESTORING"
        self.publish()
        self._operation("restore", lambda ssh: self.hooks.restore(ssh, self.state.original), self._restored)

    def _restored(self, _result):
        self.runtime._batch_setup_overrides.clear()
        self.state.current = None
        self.state.finished = time.monotonic()
        self.state.phase = "COMPLETED"
        self._stage = ""
        self.runtime.auto_discovery_completed.disconnect(self._discovered)
        self.runtime.auto_attempt_completed.disconnect(self._completed)
        service = self.runtime.services.get("Jetson")
        if service:
            service.operation_succeeded.disconnect(self._succeeded)
            service.operation_failed.disconnect(self._failed)
        self.publish()

    def update_phase(self, phase):
        if self.state.running and (self._stage == "attempt" or
                                   self._stage in {"initial", "next"} and phase in {"RECOVERING", "PRECHECK"}):
            self.state.phase = {"FINALIZING": "EVALUATING"}.get(phase, phase)
