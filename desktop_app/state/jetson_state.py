from enum import Enum

from PySide6.QtCore import QObject, Signal


class JetsonConnectionStatus(str, Enum):
    DISCONNECTED = "DISCONNECTED"
    CONNECTING = "CONNECTING"
    CONNECTED = "CONNECTED"
    FAILED = "FAILED"


class JetsonState(QObject):
    state_changed = Signal(object)
    network_snapshot_changed = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.host = ""
        self.username = ""
        self.port = 22
        self.status = JetsonConnectionStatus.DISCONNECTED
        self.last_error = ""
        self.jetson_info = None
        self.network_snapshot = None

    @property
    def connected(self) -> bool:
        return self.status == JetsonConnectionStatus.CONNECTED

    def set_connecting(self, host: str, username: str, port: int) -> None:
        self.host = host
        self.username = username
        self.port = port
        self.status = JetsonConnectionStatus.CONNECTING
        self.last_error = ""
        self.jetson_info = None
        self.network_snapshot = None
        self.state_changed.emit(self)
        self.network_snapshot_changed.emit(None)

    def set_connected(self, jetson_info: dict, network_snapshot: dict) -> None:
        self.status = JetsonConnectionStatus.CONNECTED
        self.last_error = ""
        self.jetson_info = jetson_info
        self.network_snapshot = network_snapshot
        self.state_changed.emit(self)
        self.network_snapshot_changed.emit(network_snapshot)

    def set_failed(self, error: str) -> None:
        self.status = JetsonConnectionStatus.FAILED
        self.last_error = error
        self.jetson_info = None
        self.network_snapshot = None
        self.state_changed.emit(self)
        self.network_snapshot_changed.emit(None)

    def set_disconnected(self) -> None:
        self.status = JetsonConnectionStatus.DISCONNECTED
        self.last_error = ""
        self.jetson_info = None
        self.network_snapshot = None
        self.state_changed.emit(self)
        self.network_snapshot_changed.emit(None)

    def set_network_snapshot(self, network_snapshot: dict) -> None:
        self.network_snapshot = network_snapshot
        self.network_snapshot_changed.emit(network_snapshot)
        self.state_changed.emit(self)
