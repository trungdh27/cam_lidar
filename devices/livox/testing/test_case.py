from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class AutomationLevel(str, Enum):
    AUTO = "AUTO"
    GUIDED = "GUIDED"
    MANUAL = "MANUAL"


@dataclass(frozen=True)
class TestCaseDefinition:
    id: str
    device: str
    name: str
    group: str
    description: str
    automation_level: AutomationLevel
    priority: str
    timeout_sec: float
    executor: type[Any]
    requires_jetson: bool = False
    requires_network: bool = False
    requires_device: bool = False
    requires_stream: bool = False
    enabled: bool = True
    tags: tuple[str, ...] = field(default_factory=tuple)
    order: int = 0
    purpose: str = ""
    precondition: str = ""
    requirements: str = ""
    procedure: str = ""
    expected_result: str = ""
    parameters: tuple[str, ...] = field(default_factory=tuple)
    source: str = ""
    source_test_id: str = ""
    source_status: str = ""
    source_actual_result: str = ""
    source_executed_by: str = ""
    source_executed_date: str = ""
    source_note: str = ""
    implemented_by: str = ""
    related_existing_tests: tuple[str, ...] = field(default_factory=tuple)
    requires_ros2_target: bool = False

    def __post_init__(self) -> None:
        if not self.id.strip():
            raise ValueError("Test case id is required")
        if not self.device.strip():
            raise ValueError("Test case device is required")
        if not self.name.strip():
            raise ValueError("Test case name is required")
        if self.timeout_sec <= 0:
            raise ValueError("Test case timeout_sec must be positive")
        if not isinstance(self.automation_level, AutomationLevel):
            raise TypeError("automation_level must be an AutomationLevel")
        if not isinstance(self.executor, type):
            raise TypeError("executor must be an executor class")

    @property
    def prerequisites(self) -> tuple[str, ...]:
        values = []
        if self.requires_jetson:
            values.append("Jetson connected")
        if self.requires_network:
            values.append("LiDAR network ready")
        if self.requires_device:
            values.append("LiDAR discovered")
        if self.requires_stream:
            values.append("LiDAR stream active")
        if self.requires_ros2_target:
            values.append("Production ROS2 target configured")
        return tuple(values)
