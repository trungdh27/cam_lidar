"""Reusable Bluetooth data structures for present and future test phases."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class BluetoothTestStatus(str, Enum):
    READY = "READY"
    RUNNING = "RUNNING"
    PASS = "PASS"
    FAIL = "FAIL"
    SKIPPED = "SKIPPED"
    ERROR = "ERROR"
    NOT_IMPLEMENTED = "NOT_IMPLEMENTED"


class BluetoothTestMode(str, Enum):
    AUTO = "AUTO"
    GUIDED = "GUIDED"
    MANUAL = "MANUAL"


@dataclass(frozen=True)
class BluetoothControllerInfo:
    detected: bool | None = None
    interface: str | None = None
    address: str | None = None
    name: str | None = None
    manufacturer: str | None = None
    version: str | None = None
    powered: bool | None = None
    ble_supported: bool | None = None
    roles: tuple[str, ...] = ()
    advertising_supported: bool | None = None
    supported_instances: int | None = None
    active_instances: int | None = None
    actual_role: str | None = None


@dataclass(frozen=True)
class BluetoothDeviceInfo:
    jetson_connected: bool = False
    controller: BluetoothControllerInfo = field(
        default_factory=BluetoothControllerInfo
    )
    service_active: bool | None = None
    service_load_state: str | None = None
    service_active_state: str | None = None
    service_sub_state: str | None = None
    expected_role: str = "Peripheral"


@dataclass(frozen=True)
class BluetoothTestCase:
    test_id: str
    group: str
    name: str
    purpose: str
    mode: BluetoothTestMode


@dataclass(frozen=True)
class BluetoothCommandResult:
    """Sanitized, structured record of a bounded Jetson diagnostic command."""

    command: str
    return_code: int | None
    stdout: str = ""
    stderr: str = ""
    duration_s: float = 0.0
    timed_out: bool = False
    error: str | None = None

    @property
    def success(self) -> bool:
        return (
            not self.timed_out
            and self.error is None
            and self.return_code == 0
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self) | {"success": self.success}


@dataclass(frozen=True)
class BluetoothDiscoveryResult:
    device: BluetoothDeviceInfo
    commands: tuple[BluetoothCommandResult, ...] = ()
    message: str = ""


@dataclass(frozen=True)
class BluetoothTestResult:
    test_case_id: str
    status: BluetoothTestStatus
    message: str = ""
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_s: float = 0.0
    observations: dict[str, Any] = field(default_factory=dict)
    command_results: tuple[BluetoothCommandResult, ...] = ()
    evidence_paths: tuple[str, ...] = ()

    @property
    def details(self) -> dict[str, Any]:
        """BT-0 compatibility alias for older presentation callers."""
        return self.observations

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["status"] = self.status.value
        payload["started_at"] = (
            self.started_at.isoformat() if self.started_at else None
        )
        payload["finished_at"] = (
            self.finished_at.isoformat() if self.finished_at else None
        )
        payload["command_results"] = [
            item.to_dict() for item in self.command_results
        ]
        return payload
