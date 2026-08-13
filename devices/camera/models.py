from dataclasses import dataclass, field
from enum import Enum


class CameraConnectionState(str, Enum):
    DISCONNECTED = "DISCONNECTED"
    DISCOVERING = "DISCOVERING"
    CONNECTING = "CONNECTING"
    CONNECTED = "CONNECTED"
    STREAMING = "STREAMING"
    ERROR = "ERROR"


@dataclass(frozen=True)
class StreamProfile:
    key: str
    label: str
    resolution: str
    fps: tuple[int, ...]
    formats: tuple[str, ...]

    @property
    def display_name(self) -> str:
        return f"{self.label} - {self.resolution}"


@dataclass(frozen=True)
class CameraDeviceInfo:
    profile_id: str
    model: str
    serial_number: str = "-"
    firmware: str = "-"
    interface: str = "-"
    device_path: str = "-"
    sdk_driver: str = "-"
    stream_profiles: tuple[StreamProfile, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class CameraMonitorSnapshot:
    actual_fps: float | None = None
    frame_interval_ms: float | None = None
    dropped_frames: int = 0
    frame_counter: int = 0
    exposure: str = "-"
    gain: str = "-"
    temperature_c: float | None = None
    timestamp: str = "-"
