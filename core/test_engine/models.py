from dataclasses import dataclass, field

from core.test_engine.status import TestStatus


@dataclass
class TestDefinition:
    test_id: str
    name: str
    group: str
    automation: str = "AUTO"


@dataclass
class TestResult:
    test_id: str
    status: TestStatus = TestStatus.NOT_RUN
    message: str = ""
    metrics: dict = field(default_factory=dict)
