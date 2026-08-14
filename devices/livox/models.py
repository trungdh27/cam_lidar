from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class LivoxDiscoveryStatus(str, Enum):
    FOUND = "FOUND"
    DEVICE_NOT_FOUND = "DEVICE_NOT_FOUND"
    IP_MISMATCH = "IP_MISMATCH"
    MODEL_MISMATCH = "MODEL_MISMATCH"
    SERIAL_MISMATCH = "SERIAL_MISMATCH"


@dataclass
class LivoxDeviceInfo:
    found: bool
    status: LivoxDiscoveryStatus
    model: Optional[str] = None
    serial: Optional[str] = None
    lidar_ip: Optional[str] = None
    dev_type: Optional[int] = None
    handle: Optional[int] = None
    sdk_version: Optional[str] = None
    host_ip: Optional[str] = None
    profile: Optional[str] = None
    expected_lidar_ip: Optional[str] = None
    expected_serial: Optional[str] = None
    serial_match: Optional[bool] = None
    strict_serial_verification: bool = False
    reason: Optional[str] = None
    exit_code: Optional[int] = None
    raw_result: dict[str, Any] = field(default_factory=dict)
    raw_output: str = ""

    def to_dict(self) -> dict:
        return {
            "found": self.found,
            "status": self.status.value,
            "model": self.model,
            "serial": self.serial,
            "lidar_ip": self.lidar_ip,
            "dev_type": self.dev_type,
            "handle": self.handle,
            "sdk_version": self.sdk_version,
            "host_ip": self.host_ip,
            "profile": self.profile,
            "expected_lidar_ip": self.expected_lidar_ip,
            "expected_serial": self.expected_serial,
            "serial_match": self.serial_match,
            "strict_serial_verification": self.strict_serial_verification,
            "reason": self.reason,
            "exit_code": self.exit_code,
            "raw_result": self.raw_result,
            "raw_output": self.raw_output,
        }
