"""UI-agnostic contracts for LiDAR physical and visual test workflows."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Protocol


class GuidedDecision(str, Enum):
    CONFIRM = "CONFIRM"
    REJECT = "REJECT"
    SKIP = "SKIP"


@dataclass(frozen=True)
class GuidedRequest:
    test_id: str
    name: str
    purpose: str
    precondition: str
    procedure: str
    expected_result: str
    parameters: tuple[str, ...]
    timeout_sec: float


@dataclass(frozen=True)
class GuidedSubmission:
    decision: GuidedDecision
    notes: str = ""
    measurements: dict[str, Any] = field(default_factory=dict)
    evidence_references: tuple[str, ...] = field(default_factory=tuple)


GuidedCallback = Callable[[GuidedSubmission], None]


class GuidedProvider(Protocol):
    def request(self, request: GuidedRequest, callback: GuidedCallback) -> None: ...

    def cancel(self, test_id: str) -> None: ...
