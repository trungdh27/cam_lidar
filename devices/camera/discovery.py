import json
import re
import shlex
from dataclasses import dataclass

from devices.camera.jetson_realsense_probe import JETSON_REALSENSE_PROBE
from devices.camera.models import (
    CameraDevice,
    CameraPhysicalStatus,
    CameraProfileStatus,
    CameraStreamProfile,
    CameraTransport,
    make_camera_device_uid,
    normalize_usb_speed,
)
from devices.camera.remote_zed_adapter import RemoteZedAdapter
from devices.camera.zed_models import canonical_zed_model, zed_model_family


@dataclass(frozen=True)
class AdapterDiscoveryResult:
    devices: tuple[CameraDevice, ...]
    warnings: tuple[str, ...] = ()


def _clean_serial(value: object) -> str:
    text = str(value or "").strip()
    return "" if text in {"", "-", "None", "unknown", "UNKNOWN"} else text


def normalize_zed_device(raw: dict) -> CameraDevice:
    raw_model = raw.get("model") or raw.get("raw_model") or "Unknown ZED"
    family = raw.get("model_family") or zed_model_family(raw_model)
    model = canonical_zed_model(raw_model)
    serial = _clean_serial(raw.get("serial_number") or raw.get("serial"))
    port = str(raw.get("device_path") or raw.get("physical_port") or "").strip() or None
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
        capabilities=capabilities,
        profile_status=CameraProfileStatus.NOT_APPLICABLE,
        physical_status=status,
        discovery_source=f"zed_sdk:{raw.get('api', 'device_list')}",
        discovery_errors=errors,
        metadata={
            "sdk_model": str(raw_model),
            "sdk_version": raw.get("sdk_version"),
            "camera_id": raw.get("camera_id"),
            "state": raw.get("state"),
            "raw_interface": raw.get("interface"),
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
        return AdapterDiscoveryResult(tuple(devices), warnings)


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
            devices, tuple(str(item) for item in payload.get("warnings") or ())
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
