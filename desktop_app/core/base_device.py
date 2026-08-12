from abc import ABC, abstractmethod


class BaseDevice(ABC):
    """
    Base interface for every hardware device supported by the application.
    """

    device_type = "unknown"
    vendor = "unknown"
    model = "unknown"

    def __init__(self, connection=None, config=None):
        self.connection = connection
        self.config = config or {}

    @abstractmethod
    def detect(self):
        """
        Detect whether the device is available.
        """
        raise NotImplementedError

    @abstractmethod
    def get_status(self):
        """
        Return current device status.
        """
        raise NotImplementedError
