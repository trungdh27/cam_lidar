from dataclasses import asdict, dataclass, field
from enum import Enum
import hashlib
import re


class CameraConnectionState(str, Enum):
    DISCONNECTED = "DISCONNECTED"
    DISCOVERING = "DISCOVERING"
    CONNECTING = "CONNECTING"
    CONNECTED = "CONNECTED"
    STREAMING = "STREAMING"
    ERROR = "ERROR"


class CameraTransport(str, Enum):
    GMSL = "GMSL"
    USB = "USB"
    UNKNOWN = "UNKNOWN"


class UsbSpeed(str, Enum):
    USB_2 = "USB 2.0"
    USB_3 = "USB 3.x"
    UNKNOWN = "UNKNOWN"


class CameraPhysicalStatus(str, Enum):
    NOT_DETECTED = "NOT_DETECTED"
    DETECTED = "DETECTED"
    PARTIAL = "PARTIAL"
    BUSY = "BUSY"
    ERROR = "ERROR"


class CameraRosReadiness(str, Enum):
    READY = "READY"
    DRIVER_MISSING = "DRIVER_MISSING"
    UNSUPPORTED_MODEL = "UNSUPPORTED_MODEL"
    PARTIAL = "PARTIAL"
    UNKNOWN = "UNKNOWN"


class CameraProfileStatus(str, Enum):
    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"
    NOT_APPLICABLE = "NOT_APPLICABLE"


@dataclass(frozen=True)
class CameraStreamProfile:
    """A profile actually reported by a connected device/runtime."""

    stream: str
    format: str | None = None
    width: int | None = None
    height: int | None = None
    fps: int | None = None


@dataclass(frozen=True)
class CameraDevice:
    """Vendor-independent, Qt-free physical camera inventory record."""

    device_uid: str
    vendor: str
    family: str
    model: str
    normalized_model: str
    serial: str
    transport: CameraTransport = CameraTransport.UNKNOWN
    physical_port: str | None = None
    usb_speed: UsbSpeed | None = None
    sdk_backend: str | None = None
    ros_driver: str | None = None
    ros_camera_model: str | None = None
    ros_namespace_hint: str | None = None
    capabilities: dict[str, bool] = field(default_factory=dict)
    stream_profiles: tuple[CameraStreamProfile, ...] = field(default_factory=tuple)
    profile_status: CameraProfileStatus = CameraProfileStatus.NOT_APPLICABLE
    physical_status: CameraPhysicalStatus = CameraPhysicalStatus.DETECTED
    ros_readiness: CameraRosReadiness = CameraRosReadiness.UNKNOWN
    discovery_source: str = "unknown"
    discovery_errors: tuple[str, ...] = field(default_factory=tuple)
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        payload = asdict(self)
        for key in (
            "transport", "usb_speed", "profile_status", "physical_status",
            "ros_readiness",
        ):
            value = payload.get(key)
            payload[key] = value.value if isinstance(value, Enum) else value
        return payload


def _slug(value: object, separator: str = "-") -> str:
    return re.sub(rf"{re.escape(separator)}+", separator, re.sub(
        r"[^a-z0-9]+", separator, str(value).strip().lower()
    )).strip(separator)


def make_camera_device_uid(
    vendor: str,
    serial: object,
    *,
    model: str = "camera",
    physical_port: str | None = None,
) -> str:
    """Use vendor + serial; use a deterministic port fingerprint only as fallback."""

    vendor_key = _slug(vendor) or "unknown-vendor"
    serial_text = str(serial or "").strip()
    if serial_text and serial_text not in {"-", "unknown", "UNKNOWN", "None"}:
        return f"{vendor_key}:{_slug(serial_text)}"
    fallback = f"{vendor_key}|{model}|{physical_port or 'unknown-port'}"
    digest = hashlib.sha256(fallback.encode("utf-8")).hexdigest()[:12]
    return f"{vendor_key}:port-{digest}"


def make_ros_namespace_hint(
    model_token: str,
    serial: object,
    *,
    device_uid: str | None = None,
) -> str:
    model = re.sub(r"_+", "_", re.sub(
        r"[^a-z0-9]+", "_", str(model_token).strip().lower()
    )).strip("_") or "camera"
    serial_token = re.sub(r"_+", "_", re.sub(
        r"[^a-zA-Z0-9]+", "_", str(serial or "").strip()
    )).strip("_")
    if not serial_token:
        serial_token = hashlib.sha256(
            str(device_uid or model).encode("utf-8")
        ).hexdigest()[:8]
    return f"/cameras/{model}_{serial_token.lower()}"


def normalize_usb_speed(value: object) -> UsbSpeed:
    text = str(value or "").strip().lower()
    if not text:
        return UsbSpeed.UNKNOWN
    if any(token in text for token in ("super", "usb 3", "usb3")):
        return UsbSpeed.USB_3
    if any(token in text for token in ("high-speed", "high speed", "usb 2", "usb2")):
        return UsbSpeed.USB_2
    try:
        speed = float(re.search(r"\d+(?:\.\d+)?", text).group(0))
    except (AttributeError, ValueError):
        return UsbSpeed.UNKNOWN
    if speed >= 5000 or 3.0 <= speed < 100:
        return UsbSpeed.USB_3
    if speed >= 480 or 2.0 <= speed < 3.0:
        return UsbSpeed.USB_2
    return UsbSpeed.UNKNOWN


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
