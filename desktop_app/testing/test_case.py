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
        return tuple(values)
