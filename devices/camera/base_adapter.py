from abc import ABC, abstractmethod


class CameraAdapterError(RuntimeError):
    """Base error surfaced by hardware adapters."""


class CameraSDKUnavailableError(CameraAdapterError):
    pass


class CameraNotDetectedError(CameraAdapterError):
    pass


class CameraAlreadyInUseError(CameraAdapterError):
    pass


class CameraOpenError(CameraAdapterError):
    pass


class CameraOperationTimeout(CameraAdapterError):
    pass


class BaseCameraAdapter(ABC):
    @abstractmethod
    def discover(self, payload: dict) -> dict:
        raise NotImplementedError

    @abstractmethod
    def connect(self, payload: dict) -> dict:
        raise NotImplementedError

    @abstractmethod
    def disconnect(self, payload: dict | None = None) -> dict:
        raise NotImplementedError

    @property
    @abstractmethod
    def connected(self) -> bool:
        raise NotImplementedError
