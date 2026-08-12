from dataclasses import dataclass
from enum import Enum


class TestStatus(str, Enum):
    NOT_RUN = "NOT_RUN"
    RUNNING = "RUNNING"
    PASS = "PASS"
    FAIL = "FAIL"
    ERROR = "ERROR"
    TIMEOUT = "TIMEOUT"


@dataclass
class TestResult:
    test_id: str
    status: TestStatus

    actual_result: str = ""
    stdout: str = ""
    stderr: str = ""

    duration: float = 0.0
    evidence_path: str = ""
