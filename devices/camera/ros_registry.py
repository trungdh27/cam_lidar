from dataclasses import dataclass, replace

from devices.camera.models import (
    CameraDevice,
    CameraRosReadiness,
    make_ros_namespace_hint,
)


@dataclass(frozen=True)
class RosCameraDriverMetadata:
    vendor: str
    normalized_model: str
    driver_package: str
    ros_camera_model: str


class RosCameraDriverRegistry:
    """Phase 8.3-0 metadata only; it never launches or manages ROS nodes."""

    def __init__(self):
        entries = (
            RosCameraDriverMetadata(
                "Stereolabs", "zed_x_mini", "zed_wrapper", "zedxm"
            ),
            RosCameraDriverMetadata(
                "Stereolabs", "zed_x_one_4k", "zed_wrapper", "zedxone4k"
            ),
            RosCameraDriverMetadata(
                "Intel RealSense", "d435i", "realsense2_camera", "d435i"
            ),
        )
        self._entries = {
            (entry.vendor.casefold(), entry.normalized_model): entry
            for entry in entries
        }

    def resolve(self, device: CameraDevice) -> RosCameraDriverMetadata | None:
        return self._entries.get(
            (device.vendor.casefold(), device.normalized_model)
        )

    def expected_driver(self, device: CameraDevice) -> str | None:
        entry = self.resolve(device)
        if entry:
            return entry.driver_package
        if device.vendor.casefold() == "stereolabs":
            return "zed_wrapper"
        if device.vendor.casefold() == "intel realsense":
            return "realsense2_camera"
        return None

    def packages(self) -> tuple[str, ...]:
        return tuple(sorted({item.driver_package for item in self._entries.values()}))

    def map_device(
        self,
        device: CameraDevice,
        package_availability: dict[str, bool | None] | None = None,
    ) -> CameraDevice:
        entry = self.resolve(device)
        expected = self.expected_driver(device)
        if entry is None:
            readiness = (
                CameraRosReadiness.UNSUPPORTED_MODEL
                if expected else CameraRosReadiness.UNKNOWN
            )
            model_token = device.normalized_model
            ros_model = None
        else:
            installed = (package_availability or {}).get(entry.driver_package)
            readiness = (
                CameraRosReadiness.READY if installed is True else
                CameraRosReadiness.DRIVER_MISSING if installed is False else
                CameraRosReadiness.UNKNOWN
            )
            model_token = entry.ros_camera_model
            ros_model = entry.ros_camera_model
        namespace = make_ros_namespace_hint(
            model_token, device.serial, device_uid=device.device_uid
        )
        return replace(
            device,
            ros_driver=expected,
            ros_camera_model=ros_model,
            ros_namespace_hint=namespace,
            ros_readiness=readiness,
        )
