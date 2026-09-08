from devices.camera.ros_automation.adapters import (
    RealSenseRosAdapter,
    ZedRosAdapter,
)


class UnsupportedRosCameraAdapterError(LookupError):
    code = "ROS_ADAPTER_UNSUPPORTED"


class RosCameraAdapterRegistry:
    """Resolve ROS behavior by mapped driver, never by page or model branching."""

    def __init__(self, adapters=None):
        adapters = adapters or (ZedRosAdapter(), RealSenseRosAdapter())
        self._adapters = {adapter.driver: adapter for adapter in adapters}

    def resolve(self, device):
        driver = getattr(device, "ros_driver", None)
        try:
            return self._adapters[driver]
        except KeyError as exc:
            raise UnsupportedRosCameraAdapterError(
                f"No ROS camera adapter is registered for driver {driver or '-'}"
            ) from exc

    def required_packages(self, devices) -> tuple[str, ...]:
        return tuple(sorted({self.resolve(device).required_package(device) for device in devices}))
