from PySide6.QtCore import QObject, Signal

from core.network.models import TemporaryIPState
from core.network.network_manager import NetworkManager
from core.network.temporary_ip_manager import TemporaryIPManager
from desktop_app.services.jetson_connection_service import (
    JetsonConnectionService,
)


class TemporaryIPWorker(QObject):
    success = Signal(dict)
    failed = Signal(str)
    finished = Signal()

    def __init__(self, connection_service: JetsonConnectionService, action: str, interface: str | None = None,
                 ip_address: str | None = None, prefix: int = 24, sudo_password: str | None = None,
                 state: dict | None = None, parent=None):
        super().__init__(parent)
        self.connection_service = connection_service
        self.action = action
        self.interface = interface
        self.ip_address = ip_address
        self.prefix = prefix
        self.sudo_password = sudo_password
        self.state = state
        self.request_id = None

    def start(self):
        self.connection_service.operation_succeeded.connect(self._on_success)
        self.connection_service.operation_failed.connect(self._on_failed)
        self.request_id = self.connection_service.submit_operation(
            "temporary_ip",
            self._execute,
        )
        if self.request_id is None:
            self._finish_with_error(
                "Jetson is not connected. Connect from Dashboard first."
            )

    async def _execute(self, ssh):
        manager = TemporaryIPManager(ssh)
        if self.action == "apply":
            if not self.interface or not self.ip_address:
                raise RuntimeError("Temporary IP apply parameters are incomplete")
            state = await manager.apply(
                interface_name=self.interface,
                ip_address=self.ip_address,
                prefix=self.prefix,
                sudo_password=self.sudo_password,
            )
        elif self.action == "restore":
            if not self.state:
                raise RuntimeError("Temporary IP state is missing")
            state = await manager.restore(
                TemporaryIPState.from_dict(self.state),
                sudo_password=self.sudo_password,
            )
        else:
            raise RuntimeError(f"Unknown action: {self.action}")
        network = await NetworkManager(ssh).inspect()
        return {
            "action": self.action,
            "state": state.to_dict(),
            "network": network.to_dict(),
        }

    def _on_success(self, request_id: str, result: object):
        if request_id != self.request_id:
            return
        self.success.emit(result)
        self._finish()

    def _on_failed(self, request_id: str, error: str):
        if request_id != self.request_id:
            return
        self._finish_with_error(error)

    def _finish_with_error(self, error: str):
        self.failed.emit(error)
        self._finish()

    def _finish(self):
        try:
            self.connection_service.operation_succeeded.disconnect(
                self._on_success
            )
            self.connection_service.operation_failed.disconnect(
                self._on_failed
            )
        except RuntimeError:
            pass
        self.finished.emit()
