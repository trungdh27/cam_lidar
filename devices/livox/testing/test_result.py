from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class TestStatus(str, Enum):
    NOT_RUN = "NOT_RUN"
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    PASS = "PASS"
    FAIL = "FAIL"
    ERROR = "ERROR"
    SKIPPED = "SKIPPED"
    CANCELLED = "CANCELLED"

    @property
    def terminal(self) -> bool:
        return self in {
            TestStatus.PASS,
            TestStatus.FAIL,
            TestStatus.ERROR,
            TestStatus.SKIPPED,
            TestStatus.CANCELLED,
        }


@dataclass(frozen=True)
class TestOutcome:
    status: TestStatus
    actual_result: str
    measurements: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    def __post_init__(self) -> None:
        if not self.status.terminal:
            raise ValueError("Executor outcomes must use a terminal status")
        if self.status is TestStatus.ERROR and not self.error:
            raise ValueError("ERROR outcomes require an error message")


@dataclass
class TestResult:
    test_id: str
    status: TestStatus = TestStatus.NOT_RUN
    started_at: str | None = None
    ended_at: str | None = None
    duration_sec: float | None = None
    actual_result: str = "Test has not been executed."
    error: str | None = None
    measurements: dict[str, Any] = field(default_factory=dict)
    evidence: list[str] = field(default_factory=list)

    def queue(self) -> None:
        self.status = TestStatus.QUEUED
        self.actual_result = "Queued for execution."

    def start(self) -> None:
        self.status = TestStatus.RUNNING
        self.started_at = _now()
        self.ended_at = None
        self.duration_sec = None
        self.error = None
        self.actual_result = "Test is running."

    def complete(self, outcome: TestOutcome, duration_sec: float) -> None:
        self.status = outcome.status
        if self.started_at is None:
            self.started_at = _now()
        self.ended_at = _now()
        self.duration_sec = max(0.0, float(duration_sec))
        self.actual_result = outcome.actual_result
        self.error = outcome.error
        self.measurements = dict(outcome.measurements)

    def to_dict(self) -> dict[str, Any]:
        return {
            "test_id": self.test_id,
            "status": self.status.value,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "duration_sec": self.duration_sec,
            "actual_result": self.actual_result,
            "error": self.error,
            "measurements": dict(self.measurements),
            "evidence": list(self.evidence),
        }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")
