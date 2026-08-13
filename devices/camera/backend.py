from abc import ABC, abstractmethod

from devices.camera.models import CameraDeviceInfo, CameraMonitorSnapshot


class CameraBackendError(RuntimeError):
    pass


class CameraBackendUnavailable(CameraBackendError):
    pass


class CameraBackend(ABC):
    """Hardware-facing contract implemented by vendor adapters in later phases."""

    @abstractmethod
    def discover(self, profile_id: str, execution_host: str) -> list[CameraDeviceInfo]:
        raise NotImplementedError

    @abstractmethod
    def connect(self, device: CameraDeviceInfo) -> CameraDeviceInfo:
        raise NotImplementedError

    @abstractmethod
    def disconnect(self) -> None:
        raise NotImplementedError

    @abstractmethod
    def start_stream(self, resolution: str, fps: int, pixel_format: str) -> None:
        raise NotImplementedError

    @abstractmethod
    def stop_stream(self) -> None:
        raise NotImplementedError

    @abstractmethod
    def monitor(self) -> CameraMonitorSnapshot:
        raise NotImplementedError
