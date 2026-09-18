"""Asynchronous coordinator for BT-1 Bluetooth readiness tests."""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from core.bluetooth.evidence_store import BluetoothEvidenceStore
from core.bluetooth.models import (
    BluetoothTestCase, BluetoothTestMode, BluetoothTestResult,
    BluetoothTestStatus,
)


def initial_bluetooth_test_cases() -> tuple[BluetoothTestCase, ...]:
    """BT-1 catalog; future phases may load these from testcases/bluetooth."""
    return (
        BluetoothTestCase("TC-JBT-ENV-001", "Environment", "Bluetooth Controller Detection",
                          "Verify that the Jetson Bluetooth controller is detected and available.", BluetoothTestMode.AUTO),
        BluetoothTestCase("TC-JBT-ENV-002", "Environment", "Bluetooth Service Ready",
                          "Verify that the Bluetooth service on Jetson is installed, active, and ready.", BluetoothTestMode.AUTO),
        BluetoothTestCase("TC-JBT-ROLE-001", "BLE Role", "BLE Peripheral Role Detection",
                          "Verify that Jetson can operate as the expected BLE Peripheral role.", BluetoothTestMode.AUTO),
    )


class BluetoothTestRunner(QObject):
    """Schedule one remote diagnostic at a time without blocking the Qt UI."""

    status_changed = Signal(str, object)
    result_ready = Signal(object)
    log = Signal(str)
    running_changed = Signal(bool)
    plan_finished = Signal(object, str)

    def __init__(self, manager=None, test_cases: Iterable[BluetoothTestCase] | None = None,
                 evidence_root: str | Path | None = None, parent=None):
        super().__init__(parent)
        self.manager = manager
        self.test_cases = tuple(test_cases or initial_bluetooth_test_cases())
        self._by_id = {case.test_id: case for case in self.test_cases}
        self.statuses = {case.test_id: BluetoothTestStatus.READY for case in self.test_cases}
        self.results: dict[str, BluetoothTestResult] = {}
        self.running = False
        self._queue: deque[str] = deque()
        self._current_id: str | None = None
        self._stop_requested = False
        self._session_id: str | None = None
        self.evidence_store = BluetoothEvidenceStore(evidence_root)
        if manager is not None:
            manager.test_completed.connect(self._on_test_completed)

    def status_for(self, test_case_id: str) -> BluetoothTestStatus:
        return self.statuses[test_case_id]

    def summary(self) -> dict[BluetoothTestStatus, int]:
        return {status: sum(value == status for value in self.statuses.values())
                for status in BluetoothTestStatus}

    def run_selected(self, test_case_ids: Iterable[str]) -> None:
        if self.running:
            self._log("A Bluetooth test plan is already running.")
            return
        selected = [test_id for test_id in test_case_ids if test_id in self._by_id]
        if not selected:
            self._log("No Bluetooth test cases were selected.")
            return
        self._queue = deque(selected)
        self._stop_requested = False
        self.running = True
        try:
            self._session_id = self.evidence_store.begin_session()
        except Exception as exc:
            self._session_id = None
            self._log(f"Bluetooth evidence session could not be created: {type(exc).__name__}: {exc}")
        self.running_changed.emit(True)
        self._start_next()

    def run_all(self) -> None:
        self.run_selected(case.test_id for case in self.test_cases if case.mode == BluetoothTestMode.AUTO)

    def stop(self) -> None:
        if not self.running:
            self._log("Stop requested; no Bluetooth execution is active.")
            return
        self._stop_requested = True
        self._queue.clear()
        if self._current_id is not None:
            test_id = self._current_id
            self._current_id = None
            self._set_status(test_id, BluetoothTestStatus.SKIPPED)
            self._log(f"{test_id}: skipped after stop was requested.")
        self._finish_plan()

    def _start_next(self) -> None:
        if self._stop_requested or not self._queue:
            self._finish_plan()
            return
        if self.manager is None:
            test_id = self._queue.popleft()
            self._complete_local_error(test_id, "Bluetooth manager is not configured.")
            return
        self._current_id = self._queue.popleft()
        self._set_status(self._current_id, BluetoothTestStatus.RUNNING)
        self._log(f"Running {self._current_id}")
        self.manager.start_test(self._current_id)

    def _on_test_completed(self, test_case_id: str, result: BluetoothTestResult) -> None:
        if test_case_id != self._current_id:
            return  # A stopped plan deliberately ignores its bounded in-flight probe.
        self._current_id = None
        self._persist_and_publish(result)
        self._start_next()

    def _complete_local_error(self, test_id: str, message: str) -> None:
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        self._current_id = None
        self._persist_and_publish(BluetoothTestResult(
            test_case_id=test_id, status=BluetoothTestStatus.ERROR, message=message,
            started_at=now, finished_at=now,
        ))
        self._start_next()

    def _persist_and_publish(self, result: BluetoothTestResult) -> None:
        definition = self._by_id[result.test_case_id]
        try:
            if self._session_id is None:
                raise RuntimeError("Bluetooth evidence session is unavailable")
            stored = self.evidence_store.write_result(self._session_id, definition.name, result)
        except Exception as exc:
            stored = BluetoothTestResult(
                test_case_id=result.test_case_id, status=BluetoothTestStatus.ERROR,
                message=f"Evidence write failed: {type(exc).__name__}: {exc}",
                started_at=result.started_at, finished_at=result.finished_at,
                duration_s=result.duration_s, observations=result.observations,
                command_results=result.command_results,
            )
        self.results[stored.test_case_id] = stored
        self._set_status(stored.test_case_id, stored.status)
        self.result_ready.emit(stored)
        self._log_observations(stored)
        self._log(f"{stored.test_case_id} {stored.status.value}: {stored.message}")

    def _set_status(self, test_id: str, status: BluetoothTestStatus) -> None:
        self.statuses[test_id] = status
        self.status_changed.emit(test_id, status)

    def _finish_plan(self) -> None:
        if not self.running:
            return
        self.running = False
        self.running_changed.emit(False)
        self.plan_finished.emit(self.summary(), str(self.evidence_store.root / "bluetooth" / str(self._session_id)))
        self._log("Bluetooth test plan stopped." if self._stop_requested else "Bluetooth test plan finished.")

    def _log(self, message: str) -> None:
        self.log.emit(message)

    def _log_observations(self, result: BluetoothTestResult) -> None:
        observed = result.observations
        if result.test_case_id == "TC-JBT-ENV-001" and observed.get("controller_detected"):
            self._log(f"Controller detected: {observed.get('interface') or observed.get('controller_name') or 'controller'}")
        elif result.test_case_id == "TC-JBT-ENV-002":
            self._log(f"bluetooth.service: {observed.get('active_state') or 'unknown'}")
            powered = observed.get("adapter_powered")
            if powered is not None:
                self._log(f"Adapter powered: {'yes' if powered else 'no'}")
        elif result.test_case_id == "TC-JBT-ROLE-001":
            if observed.get("ble_supported") is True:
                self._log("BLE support detected")
            if observed.get("actual_role") in {"Peripheral", "Peripheral Capable"}:
                self._log("Peripheral role supported")
