from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from threading import Event
from typing import Any, Callable

from core.testing.errors import TestCancelledError, TestTimeoutError


class TestStatus(str, Enum):
    NOT_RUN = "NOT_RUN"
    RUNNING = "RUNNING"
    PASS = "PASS"
    FAIL = "FAIL"
    ERROR = "ERROR"
    BLOCKED = "BLOCKED"
    SKIPPED = "SKIPPED"
    CANCELLED = "CANCELLED"


@dataclass(frozen=True)
class TestCaseDefinition:
    test_id: str
    name: str
    group: str
    automation_key: str
    priority: str
    timeout_s: float
    parameters: dict[str, Any] = field(default_factory=dict)
    rules: tuple[dict[str, Any], ...] = ()
    evidence_metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class TestResult:
    test_case_id: str
    test_name: str
    automation_key: str
    status: TestStatus = TestStatus.NOT_RUN
    started_at: str | None = None
    finished_at: str | None = None
    duration_s: float = 0.0
    device: dict[str, Any] = field(default_factory=dict)
    configuration: dict[str, Any] = field(default_factory=dict)
    measurements: dict[str, Any] = field(default_factory=dict)
    rules: list[dict[str, Any]] = field(default_factory=list)
    rule_results: list[dict[str, Any]] = field(default_factory=list)
    failure_reasons: list[str] = field(default_factory=list)
    error: dict[str, Any] | None = None
    cleanup_errors: list[str] = field(default_factory=list)
    sub_results: list[dict[str, Any]] = field(default_factory=list)
    cycles: list[dict[str, Any]] = field(default_factory=list)
    schema_version: str = "1.0"

    def to_dict(self):
        data = asdict(self)
        data["status"] = self.status.value
        return data


@dataclass
class TestContext:
    services: dict[str, Any]
    device: dict[str, Any]
    base_configuration: dict[str, Any]
    result_root: str
    cancel_event: Event = field(default_factory=Event)
    deadline: float | None = None
    log: Callable[[str, str], None] = lambda _level, _message: None

    def checkpoint(self, monotonic_time: float):
        if self.cancel_event.is_set():
            raise TestCancelledError("Test run was cancelled by the operator.")
        if self.deadline is not None and monotonic_time >= self.deadline:
            raise TestTimeoutError("Test exceeded its configured timeout.")


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")
