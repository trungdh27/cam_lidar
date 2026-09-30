import json
import re
import shlex
from dataclasses import dataclass

from devices.camera.jetson_realsense_probe import JETSON_REALSENSE_PROBE
from devices.camera.models import (
    CameraAccessMode,
    CameraDevice,
    CameraPhysicalStatus,
    CameraProfileStatus,
    CameraStreamProfile,
    CameraTransport,
    make_camera_device_uid,
    normalize_usb_speed,
)
from devices.camera.jetson_ros_camera_probe import JETSON_ROS_CAMERA_PROBE
from devices.camera.remote_zed_adapter import RemoteZedAdapter
from devices.camera.zed_models import canonical_zed_model, zed_model_family


@dataclass(frozen=True)
class AdapterDiscoveryResult:
    devices: tuple[CameraDevice, ...]
    warnings: tuple[str, ...] = ()
    raw_candidate_count: int | None = None


def _clean_serial(value: object) -> str:
    text = str(value or "").strip()
    return "" if text in {"", "-", "None", "unknown", "UNKNOWN"} else text


def normalize_zed_device(raw: dict) -> CameraDevice:
    raw_model = raw.get("model") or raw.get("raw_model") or "Unknown ZED"
    family = raw.get("model_family") or zed_model_family(raw_model)
    # A generic X One family token does not prove a 4K or GS marketing model.
    model = str(raw_model).strip() if family == "zed_x_one" else canonical_zed_model(raw_model)
    serial = _clean_serial(raw.get("serial_number") or raw.get("serial"))
    port = str(raw.get("device_path") or raw.get("physical_port") or "").strip() or None
    state = str(raw.get("state") or "").strip()
    state_key = state.upper().replace("_", " ")
    direct_sdk_available = False if state_key == "NOT AVAILABLE" else True
    errors = tuple(filter(None, raw.get("warnings") or ()))
    status = CameraPhysicalStatus.DETECTED if serial else CameraPhysicalStatus.PARTIAL
    capabilities = {
        "mono": family.startswith("zed_x_one"),
        "stereo": family in {"zed_x_mini", "zed_x"},
        "color": family in {"zed_x_mini", "zed_x"},
        "depth": family in {"zed_x_mini", "zed_x"},
        "imu": family in {"zed_x_mini", "zed_x", "zed_x_one_4k", "zed_x_one_gs", "zed_x_one"},
        "camera_info": True,
    }
    return CameraDevice(
        device_uid=make_camera_device_uid(
            "Stereolabs", serial, model=model, physical_port=port
        ),
        vendor="Stereolabs",
        family="ZED",
        model=model,
        normalized_model=family,
        serial=serial,
        transport=CameraTransport.GMSL,
        physical_port=port,
        sdk_backend="zed_sdk",
        direct_sdk_available=direct_sdk_available,
        access_mode=(
            CameraAccessMode.DIRECT_SDK
            if direct_sdk_available else CameraAccessMode.UNAVAILABLE
        ),
        busy=not direct_sdk_available,
        capabilities=capabilities,
        profile_status=CameraProfileStatus.NOT_APPLICABLE,
        physical_status=status,
        discovery_source=f"zed_sdk:{raw.get('api', 'device_list')}",
        discovery_errors=errors,
        metadata={
            "availability_classification": (
                "CAMERA_DIRECT_ACCESS_BUSY" if not direct_sdk_available
                else "DIRECT_SDK_AVAILABLE"
            ),
            "sdk_model": str(raw_model),
            "sdk_version": raw.get("sdk_version"),
            "camera_id": raw.get("camera_id"),
            "state": raw.get("state"),
            "raw_interface": raw.get("interface"),
            "port": raw.get("port"),
            "direct_sdk_status": (
                "CAMERA_DIRECT_ACCESS_BUSY" if not direct_sdk_available
                else "DIRECT_SDK_AVAILABLE"
            ),
        },
    )


def normalize_ros_camera_device(raw: dict) -> CameraDevice:
    """Turn a production ROS observation into an inventory device.

    ``device_info`` is runtime authority: it intentionally wins over stale SDK
    data and any prior UI selection.
    """
    vendor = str(raw.get("vendor") or "Unknown").strip()
    model = str(raw.get("model") or "ROS Camera").strip()
    serial = _clean_serial(raw.get("serial_number") or raw.get("serial"))
    is_zed = "stereo" in vendor.lower() or "zed" in model.lower()
    family = zed_model_family(model) if is_zed else re.sub(r"[^a-z0-9]+", "_", model.lower()).strip("_")
    canonical_model = (
        model if family == "zed_x_one" else canonical_zed_model(model)
    ) if is_zed else model
    resolution = str(raw.get("resolution") or "").strip()
    width, height = None, None
    match = re.match(r"^(\d+)\s*[xX]\s*(\d+)$", resolution)
    if match:
        width, height = int(match.group(1)), int(match.group(2))
    target_fps = raw.get("target_fps")
    try:
        target_fps = int(float(target_fps)) if target_fps is not None else None
    except (TypeError, ValueError):
        target_fps = None
    rgb_topic = raw.get("rgb_topic") or None
    profiles = ()
    if rgb_topic and (width or height or target_fps):
        profiles = (CameraStreamProfile("rgb", raw.get("encoding"), width, height, target_fps),)
    transport = CameraTransport.GMSL if str(raw.get("input_type", "")).upper().startswith("GMSL") else CameraTransport.UNKNOWN
    capabilities = {
        key: True for key in str(raw.get("capabilities") or "").replace(",", " ").split()
    }
    capabilities["camera_info"] = bool(raw.get("device_info_topic"))
    return CameraDevice(
        device_uid=make_camera_device_uid(vendor, serial, model=canonical_model, physical_port=raw.get("ros_node")),
        vendor=vendor,
        family="ZED" if is_zed else "ROS",
        model=canonical_model,
        normalized_model=family or "ros_camera",
        serial=serial,
        transport=transport,
        sdk_backend="zed_sdk" if is_zed else None,
        stream_profiles=profiles,
        capabilities=capabilities,
        physical_status=CameraPhysicalStatus.DETECTED if serial else CameraPhysicalStatus.PARTIAL,
        physical_detected=bool(serial or raw.get("device_info_topic") or raw.get("ros_node")),
        direct_sdk_available=False if raw.get("ros_node") else None,
        ros_available=True,
        ros_node=raw.get("ros_node") or None,
        device_info_topic=raw.get("device_info_topic") or None,
        rgb_topic=rgb_topic,
        access_mode=(
            CameraAccessMode.ROS_READ_ONLY
            if rgb_topic and bool(raw.get("stream_active"))
            else CameraAccessMode.UNAVAILABLE
        ),
        owner=raw.get("ros_node") or None,
        busy=bool(raw.get("ros_node")),
        stream_active=bool(raw.get("stream_active")),
        discovery_source="production_ros",
        discovery_errors=tuple(raw.get("warnings") or ()),
        metadata={
            "availability_classification": (
                "ROS_CAMERA_AVAILABLE" if rgb_topic and raw.get("stream_active")
                else "ROS_STREAM_UNAVAILABLE" if rgb_topic
                else "ROS_DEVICE_INFO_UNAVAILABLE"
            ),
            "direct_sdk_status": "CAMERA_DIRECT_ACCESS_BUSY" if raw.get("ros_node") else "UNKNOWN",
            "driver_name": raw.get("driver_name"),
            "driver_version": raw.get("driver_version"),
            "input_type": raw.get("input_type"),
            "resolution": resolution or None,
            "target_fps": raw.get("target_fps"),
            "firmware_version": raw.get("firmware_version"),
            "sensors_firmware_version": raw.get("sensors_firmware_version"),
            "variant": raw.get("variant"),
            "encoding": raw.get("encoding"),
            "ros_camera_name": raw.get("ros_camera_name"),
            "metadata_complete": bool(raw.get("metadata_complete", bool(raw.get("vendor") and raw.get("model") and serial))),
        },
    )


def canonical_realsense_model(value: object) -> tuple[str, str]:
    text = str(value or "Intel RealSense").strip()
    compact = re.sub(r"[^a-z0-9]", "", text.lower())
    if "d435i" in compact or ("435i" in compact and "realsense" in compact):
        return "D435i", "d435i"
    match = re.search(r"\bD?([A-Z]?\d{3}[A-Z]?)\b", text, re.I)
    if match:
        token = match.group(1)
        model = token.upper() if token[-1:].isdigit() else token[:-1].upper() + token[-1].lower()
        if not model.startswith("D"):
            model = "D" + model
        return model, re.sub(r"[^a-z0-9]+", "_", model.lower()).strip("_")
    normalized = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return text, normalized or "unknown_realsense"


def normalize_realsense_device(raw: dict) -> CameraDevice:
    model, normalized = canonical_realsense_model(raw.get("model"))
    serial = _clean_serial(raw.get("serial") or raw.get("serial_number"))
    port = str(raw.get("physical_port") or "").strip() or None
    speed_raw = raw.get("usb_type_descriptor") or raw.get("usb_sysfs_speed")
    usb_speed = normalize_usb_speed(speed_raw)
    profiles = []
    for item in raw.get("stream_profiles") or ():
        try:
            profiles.append(CameraStreamProfile(
                stream=str(item.get("stream") or "unknown").lower(),
                format=str(item["format"]) if item.get("format") is not None else None,
                width=int(item["width"]) if item.get("width") is not None else None,
                height=int(item["height"]) if item.get("height") is not None else None,
                fps=int(item["fps"]) if item.get("fps") is not None else None,
            ))
        except (TypeError, ValueError):
            continue
    profiles = tuple(dict.fromkeys(profiles))
    warnings = list(raw.get("warnings") or ())
    profile_status = (
        CameraProfileStatus.AVAILABLE
        if raw.get("profile_status") == "AVAILABLE"
        else CameraProfileStatus.UNAVAILABLE
    )
    capabilities = {
        "color": normalized == "d435i",
        "depth": normalized == "d435i",
        "infrared": normalized == "d435i",
        "accelerometer": normalized == "d435i",
        "gyroscope": normalized == "d435i",
    }
    status = CameraPhysicalStatus.DETECTED if serial else CameraPhysicalStatus.PARTIAL
    return CameraDevice(
        device_uid=make_camera_device_uid(
            "Intel RealSense", serial, model=model, physical_port=port
        ),
        vendor="Intel RealSense",
        family="RealSense",
        model=model,
        normalized_model=normalized,
        serial=serial,
        transport=CameraTransport.USB,
        physical_port=port,
        usb_speed=usb_speed,
        sdk_backend=(
            "librealsense" if raw.get("backend") in {"pyrealsense2", "librealsense_cli"}
            else "usb_sysfs"
        ),
        capabilities=capabilities,
        stream_profiles=profiles,
        profile_status=profile_status,
        physical_status=status,
        discovery_source=str(raw.get("backend") or "usb_sysfs"),
        discovery_errors=tuple(warnings),
        metadata={
            "firmware": raw.get("firmware"),
            "sensors": list(raw.get("sensors") or ()),
            "raw_model": raw.get("model"),
            "raw_usb_speed": speed_raw,
            "product_id": raw.get("product_id"),
        },
    )


class ZedCameraDiscoveryAdapter:
    name = "zed"

    def __init__(self, remote_adapter=None):
        self.remote_adapter = remote_adapter or RemoteZedAdapter()

    async def discover(self, ssh) -> AdapterDiscoveryResult:
        result = await self.remote_adapter.discover_all_with_ssh(ssh)
        sdk_version = result.get("sdk_version")
        devices = []
        for raw in result.get("devices") or ():
            enriched = dict(raw)
            enriched["sdk_version"] = sdk_version
            devices.append(normalize_zed_device(enriched))
        warnings = tuple(filter(None, (result.get("discovery_note"),)))
        return AdapterDiscoveryResult(
            tuple(devices), warnings, len(result.get("devices") or ())
        )


class RealSenseCameraDiscoveryAdapter:
    name = "realsense"
    marker = "CAMERA_REALSENSE_JSON="

    def __init__(self, command_timeout: float = 12.0):
        self.command_timeout = command_timeout

    async def discover(self, ssh) -> AdapterDiscoveryResult:
        command = "python3 -c " + shlex.quote(JETSON_REALSENSE_PROBE)
        result = await ssh.run(command, timeout=self.command_timeout)
        payload = self._parse_payload(result.stdout)
        if not payload.get("ok"):
            raise RuntimeError(payload.get("error") or "RealSense discovery failed")
        devices = tuple(
            normalize_realsense_device(item)
            for item in payload.get("devices") or ()
        )
        return AdapterDiscoveryResult(
            devices, tuple(str(item) for item in payload.get("warnings") or ()),
            len(payload.get("devices") or ()),
        )

    @classmethod
    def _parse_payload(cls, output: str) -> dict:
        for line in reversed(str(output).splitlines()):
            if line.startswith(cls.marker):
                try:
                    return json.loads(line[len(cls.marker):])
                except json.JSONDecodeError as exc:
                    raise RuntimeError("Jetson returned invalid RealSense JSON") from exc
        raise RuntimeError("Jetson RealSense probe returned no structured result")


class RosProductionCameraDiscoveryAdapter:
    """Bounded, read-only discovery of externally-owned ROS cameras."""

    name = "production_ros"
    marker = "CAMERA_ROS_PRODUCTION_JSON="

    def __init__(self, command_timeout: float = 14.0):
        self.command_timeout = command_timeout

    async def discover(self, ssh) -> AdapterDiscoveryResult:
        command = "bash -lc " + shlex.quote(
            "source /opt/ros/humble/setup.bash >/dev/null 2>&1; "
            "for setup in /opt/vindynamics/setup.bash /opt/vindynamics/local_setup.bash "
            "/opt/vindynamics/sensors/*/setup.bash /opt/vindynamics/sensors/*/local_setup.bash; do "
            "[ -r \"$setup\" ] && source \"$setup\" >/dev/null 2>&1; done; "
            "python3 -c " + shlex.quote(JETSON_ROS_CAMERA_PROBE)
        )
        result = await ssh.run(command, timeout=self.command_timeout)
        payload = self._parse_payload(result.stdout)
        if not payload.get("ok"):
            return AdapterDiscoveryResult((), (payload.get("error") or "ROS production discovery unavailable",))
        return AdapterDiscoveryResult(
            tuple(normalize_ros_camera_device(item) for item in payload.get("devices") or ()),
            tuple(str(item) for item in payload.get("warnings") or ()),
            len(payload.get("devices") or ()),
        )

    @classmethod
    def _parse_payload(cls, output: str) -> dict:
        for line in reversed(str(output).splitlines()):
            if line.startswith(cls.marker):
                try:
                    return json.loads(line[len(cls.marker):])
                except json.JSONDecodeError as exc:
                    raise RuntimeError("Jetson ROS camera probe returned invalid JSON") from exc
        raise RuntimeError("Jetson ROS camera probe returned no structured result")
