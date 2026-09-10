import json
import shlex
from dataclasses import dataclass, replace
from datetime import datetime, timezone

from devices.camera.discovery import (
    RealSenseCameraDiscoveryAdapter,
    ZedCameraDiscoveryAdapter,
)
from devices.camera.models import CameraDevice, CameraPhysicalStatus, UsbSpeed
from devices.camera.ros_registry import RosCameraDriverRegistry


ROS_DRIVER_PROBE = r'''
import json
import shutil
import subprocess
import sys

packages = json.loads(sys.argv[1])
ros2 = shutil.which("ros2")
availability = {}
if ros2 is None:
    availability = {name: None for name in packages}
else:
    for name in packages:
        try:
            result = subprocess.run(
                [ros2, "pkg", "prefix", name], capture_output=True,
                text=True, timeout=4, check=False,
            )
            availability[name] = result.returncode == 0
        except Exception:
            availability[name] = None
print("CAMERA_ROS_PACKAGES_JSON=" + json.dumps({
    "ros2_available": ros2 is not None,
    "availability": availability,
}, separators=(",", ":")))
'''


@dataclass(frozen=True)
class CameraInventorySnapshot:
    devices: tuple[CameraDevice, ...]
    adapter_errors: dict[str, str]
    adapter_warnings: dict[str, tuple[str, ...]]
    discovered_at: str
    ros2_available: bool | None = None

    def to_dict(self) -> dict:
        return {
            "devices": [item.to_dict() for item in self.devices],
            "adapter_errors": dict(self.adapter_errors),
            "adapter_warnings": {
                key: list(value) for key, value in self.adapter_warnings.items()
            },
            "discovered_at": self.discovered_at,
            "ros2_available": self.ros2_available,
        }


class RosDriverPackageProbe:
    marker = "CAMERA_ROS_PACKAGES_JSON="

    async def probe(self, ssh, packages: tuple[str, ...]) -> tuple[dict[str, bool | None], bool | None]:
        command = (
            "python3 -c " + shlex.quote(ROS_DRIVER_PROBE) + " "
            + shlex.quote(json.dumps(list(packages)))
        )
        result = await ssh.run(command, timeout=max(6, len(packages) * 5))
        for line in reversed(str(result.stdout).splitlines()):
            if line.startswith(self.marker):
                payload = json.loads(line[len(self.marker):])
                return dict(payload.get("availability") or {}), payload.get("ros2_available")
        raise RuntimeError("Jetson ROS package probe returned no structured result")


def merge_camera_devices(first: CameraDevice, second: CameraDevice) -> CameraDevice:
    """Merge duplicate observations without turning one physical camera into two rows."""
    profiles = tuple(dict.fromkeys(first.stream_profiles + second.stream_profiles))
    errors = tuple(dict.fromkeys(first.discovery_errors + second.discovery_errors))
    capabilities = dict(first.capabilities)
    capabilities.update(second.capabilities)
    metadata = dict(first.metadata)
    metadata.update({key: value for key, value in second.metadata.items() if value not in (None, "", [], {})})
    status = (
        CameraPhysicalStatus.DETECTED
        if CameraPhysicalStatus.DETECTED in (first.physical_status, second.physical_status)
        else second.physical_status
    )
    richer = second if len(second.stream_profiles) > len(first.stream_profiles) else first
    known_usb_speed = next(
        (
            value for value in (richer.usb_speed, first.usb_speed, second.usb_speed)
            if value not in (None, UsbSpeed.UNKNOWN)
        ),
        richer.usb_speed or first.usb_speed or second.usb_speed,
    )
    return replace(
        richer,
        physical_port=richer.physical_port or first.physical_port or second.physical_port,
        usb_speed=known_usb_speed,
        capabilities=capabilities,
        stream_profiles=profiles,
        physical_status=status,
        discovery_source=" + ".join(dict.fromkeys((first.discovery_source, second.discovery_source))),
        discovery_errors=errors,
        metadata=metadata,
    )


def deduplicate_camera_devices(devices) -> tuple[CameraDevice, ...]:
    by_uid = {}
    for device in devices:
        if device.device_uid in by_uid:
            by_uid[device.device_uid] = merge_camera_devices(
                by_uid[device.device_uid], device
            )
        else:
            by_uid[device.device_uid] = device
    return tuple(sorted(
        by_uid.values(),
        key=lambda item: (item.vendor.casefold(), item.model.casefold(), item.serial),
    ))


class CameraInventory:
    """Single Qt-free source of truth for currently detected physical cameras."""

    def __init__(self, adapters=None, ros_registry=None, driver_probe=None):
        self.adapters = tuple(adapters or (
            ZedCameraDiscoveryAdapter(), RealSenseCameraDiscoveryAdapter()
        ))
        self.ros_registry = ros_registry or RosCameraDriverRegistry()
        self.driver_probe = driver_probe or RosDriverPackageProbe()
        self._devices: tuple[CameraDevice, ...] = ()
        self._snapshot = CameraInventorySnapshot((), {}, {}, "")

    async def discover_all(self, ssh) -> CameraInventorySnapshot:
        devices = []
        errors = {}
        warnings = {}
        for adapter in self.adapters:
            name = getattr(adapter, "name", type(adapter).__name__)
            try:
                result = await adapter.discover(ssh)
                devices.extend(result.devices)
                if result.warnings:
                    warnings[name] = tuple(result.warnings)
            except Exception as exc:
                errors[name] = f"{type(exc).__name__}: {exc}"

        devices = list(deduplicate_camera_devices(devices))
        availability = {}
        ros2_available = None
        try:
            availability, ros2_available = await self.driver_probe.probe(
                ssh, self.ros_registry.packages()
            )
        except Exception as exc:
            warnings["ros_packages"] = (
                f"ROS driver readiness unavailable: {type(exc).__name__}: {exc}",
            )
        mapped = tuple(
            self.ros_registry.map_device(device, availability)
            for device in devices
        )
        snapshot = CameraInventorySnapshot(
            devices=mapped,
            adapter_errors=errors,
            adapter_warnings=warnings,
            discovered_at=datetime.now(timezone.utc).isoformat(),
            ros2_available=ros2_available,
        )
        self._devices = mapped
        self._snapshot = snapshot
        return snapshot

    async def refresh(self, ssh) -> CameraInventorySnapshot:
        return await self.discover_all(ssh)

    def get_devices(self) -> tuple[CameraDevice, ...]:
        return self._devices

    def get_device(self, device_uid: str) -> CameraDevice | None:
        return next(
            (item for item in self._devices if item.device_uid == device_uid), None
        )

    def get_by_vendor(self, vendor: str) -> tuple[CameraDevice, ...]:
        target = vendor.casefold()
        return tuple(item for item in self._devices if item.vendor.casefold() == target)

    def get_by_serial(self, serial: object) -> tuple[CameraDevice, ...]:
        target = str(serial)
        return tuple(item for item in self._devices if item.serial == target)

    def clear_stale_inventory(self) -> None:
        self._devices = ()
        self._snapshot = CameraInventorySnapshot((), {}, {}, "")

    @property
    def snapshot(self) -> CameraInventorySnapshot:
        return self._snapshot


class CameraTargetSelection:
    """Stable UID selection state for future data-driven ROS tests."""

    ALL_CAMERAS = None

    def __init__(self):
        self.selected_device_uid: str | None = self.ALL_CAMERAS

    def select(self, device_uid: str | None) -> None:
        self.selected_device_uid = device_uid or self.ALL_CAMERAS

    def retain_after_refresh(self, devices) -> str | None:
        uids = {item.device_uid for item in devices}
        if self.selected_device_uid is not None and self.selected_device_uid not in uids:
            self.selected_device_uid = self.ALL_CAMERAS
        return self.selected_device_uid

    def selected_devices(self, devices) -> tuple[CameraDevice, ...]:
        items = tuple(devices)
        if self.selected_device_uid is None:
            return items
        return tuple(item for item in items if item.device_uid == self.selected_device_uid)
