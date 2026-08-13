from dataclasses import dataclass
from typing import Optional


@dataclass
class LivoxDeviceInfo:
    found: bool
    model: Optional[str] = None
    serial: Optional[str] = None
    lidar_ip: Optional[str] = None
    dev_type: Optional[int] = None
    handle: Optional[int] = None
    sdk_version: Optional[str] = None
    host_ip: Optional[str] = None
    profile: Optional[str] = None
    raw_output: str = ""

    def to_dict(self) -> dict:
        return {
            "found": self.found,
            "model": self.model,
            "serial": self.serial,
            "lidar_ip": self.lidar_ip,
            "dev_type": self.dev_type,
            "handle": self.handle,
            "sdk_version": self.sdk_version,
            "host_ip": self.host_ip,
            "profile": self.profile,
            "raw_output": self.raw_output,
        }
