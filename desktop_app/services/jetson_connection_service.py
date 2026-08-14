from collections.abc import Awaitable, Callable
from itertools import count

from PySide6.QtCore import QObject, Signal

from core.remote.ssh_manager import SSHConfig, SSHManager
from desktop_app.state.jetson_state import (
    JetsonConnectionStatus,
    JetsonState,
)
from desktop_app.workers.ssh_probe_worker import SSHProbeWorker


RemoteOperation = Callable[[SSHManager], Awaitable[object]]


class JetsonConnectionService(QObject):
    connecting = Signal()
    connected = Signal(object)
    disconnected = Signal()
    connection_failed = Signal(str)
    operation_succeeded = Signal(str, object)
    operation_failed = Signal(str, str)
    remote_process_started = Signal(str)
    remote_process_output = Signal(str, str, str)
    remote_process_finished = Signal(str, int)
    remote_process_failed = Signal(str, str)

    def __init__(self, state: JetsonState, parent=None):
        super().__init__(parent)
        self.state = state
        self._request_ids = count(1)
        self._worker = SSHProbeWorker(self)
        self._worker.connected.connect(self._on_connected)
        self._worker.connection_failed.connect(self._on_connection_failed)
        self._worker.disconnected.connect(self._on_disconnected)
        self._worker.operation_succeeded.connect(
            self.operation_succeeded.emit
        )
        self._worker.operation_failed.connect(self.operation_failed.emit)
        self._worker.remote_process_started.connect(
            self.remote_process_started.emit
        )
        self._worker.remote_process_output.connect(
            self.remote_process_output.emit
        )
        self._worker.remote_process_finished.connect(
            self.remote_process_finished.emit
        )
        self._worker.remote_process_failed.connect(
            self.remote_process_failed.emit
        )
        self._worker.start()

    @property
    def is_connected(self) -> bool:
        return self.state.connected

    def connect_to(self, config: SSHConfig) -> None:
        if self.state.status == JetsonConnectionStatus.CONNECTING:
            return

        self.state.set_connecting(
            host=config.host,
            username=config.username,
            port=config.port,
        )
        self.connecting.emit()
        self._worker.connect_to(config)

    def disconnect_from_jetson(self) -> None:
        if self.state.status == JetsonConnectionStatus.DISCONNECTED:
            return
        self._worker.disconnect_from_jetson()

    def submit_operation(
        self,
        name: str,
        operation: RemoteOperation,
    ) -> str | None:
        if not self.is_connected:
            return None

        request_id = f"{name}:{next(self._request_ids)}"
        self._worker.submit_operation(request_id, operation)
        return request_id

    def start_remote_process(self, name: str, command: str) -> str | None:
        """Start a managed process without occupying the operation lock."""
        if not self.is_connected:
            return None
        request_id = f"{name}:{next(self._request_ids)}"
        self._worker.start_remote_process(request_id, command)
        return request_id

    def stop_remote_process(self, request_id: str) -> None:
        self._worker.stop_remote_process(request_id)

    def update_network_snapshot(self, network_snapshot: dict) -> None:
        if self.is_connected:
            self.state.set_network_snapshot(network_snapshot)

    def shutdown(self) -> None:
        if not self._worker.isRunning():
            return
        self._worker.shutdown()
        self._worker.wait(5000)

    def _on_connected(self, result: dict) -> None:
        self.state.set_connected(
            jetson_info=result["jetson"],
            network_snapshot=result["network"],
        )
        self.connected.emit(self.state)

    def _on_connection_failed(self, error: str) -> None:
        self.state.set_failed(error)
        self.connection_failed.emit(error)

    def _on_disconnected(self) -> None:
        self.state.set_disconnected()
        self.disconnected.emit()
