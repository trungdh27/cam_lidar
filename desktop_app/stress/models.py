from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class RuntimeStatus(str, Enum):
    NOT_RUN = "NOT_RUN"
    WAITING = "WAITING"
    STARTING = "STARTING"
    PREPARED = "PREPARED"
    RUNNING = "RUNNING"
    STOPPING = "STOPPING"
    PASS = "PASS"
    FAIL = "FAIL"
    WARNING = "WARNING"
    ERROR = "ERROR"
    STOPPED = "STOPPED"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    BLOCKED = "BLOCKED"


class ExecutionType(str, Enum):
    AUTO = "AUTO"
    GUIDED = "GUIDED"
    MANUAL = "MANUAL"


class CollectorStatus(str, Enum):
    WAITING = "WAITING"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    WARNING = "WARNING"
    ERROR = "ERROR"
    STOPPED = "STOPPED"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    MANUAL_REQUIRED = "MANUAL_REQUIRED"


class PreTestStatus(str, Enum):
    NOT_RUN = "NOT_RUN"
    RUNNING = "RUNNING"
    PASS = "PASS"
    WARNING = "WARNING"
    FAIL = "FAIL"
    MISSING = "MISSING"
    NOT_CONFIGURED = "NOT_CONFIGURED"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class EnvironmentStatus(str, Enum):
    NOT_CHECKED = "NOT_CHECKED"
    READY = "READY"
    NOT_READY = "NOT_READY"


@dataclass(frozen=True)
class EvidenceDefinition:
    id: str
    name: str
    type: str
    collector: str
    command_description: str
    sample_interval: float | None = None
    manual_required: bool = False


@dataclass
class EvidenceRecord:
    id: str
    name: str
    type: str
    collector: str
    status: CollectorStatus
    file: str
    command_description: str
    sample_interval: float | None = None
    started_at: str | None = None
    ended_at: str | None = None
    last_update: str | None = None
    error: str | None = None
    manual_required: bool = False

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["status"] = self.status.value
        return value


@dataclass
class EvidenceManifest:
    test_id: str
    attempt: int
    evidence: list[EvidenceRecord]

    def to_dict(self) -> dict[str, Any]:
        return {
            "test_id": self.test_id,
            "attempt": self.attempt,
            "evidence": [record.to_dict() for record in self.evidence],
        }


@dataclass
class StressTestDefinition:
    test_id: str
    group: str
    test_name: str
    source_fields: dict[str, str]
    duration_text: str = ""
    duration_seconds: int | None = None
    severity: str = ""
    runtime_status: RuntimeStatus = RuntimeStatus.NOT_RUN
    execution_type: ExecutionType = ExecutionType.GUIDED
    dependencies: frozenset[str] = field(default_factory=frozenset)
    evidence_plan: tuple[EvidenceDefinition, ...] = field(default_factory=tuple)
    workload_program: str | None = None
    workload_arguments: tuple[str, ...] = field(default_factory=tuple)
    workload_description: str | None = None
    target_cpu_percent: float | None = None


@dataclass
class PreTestCheck:
    id: str
    name: str
    category: str
    actual: str = "Not run"
    status: PreTestStatus = PreTestStatus.NOT_RUN
    dependency: str | None = None
    blocking: bool = False

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["status"] = self.status.value
        return value


@dataclass(frozen=True)
class AttemptPaths:
    session_dir: Path
    attempt_dir: Path
    logs_dir: Path
    artifacts_dir: Path
    attempt: int
