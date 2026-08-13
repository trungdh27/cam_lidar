from PySide6.QtCore import QObject, Signal

from desktop_app.services.jetson_connection_service import (
    JetsonConnectionService,
)
from devices.livox.sdk2_backend import LivoxSDK2Backend


class LivoxDiscoveryWorker(QObject):
    success = Signal(dict)
    failed = Signal(str)
    finished = Signal()

    def __init__(self, connection_service: JetsonConnectionService, host_ip: str, model: str, timeout: int = 8, parent=None):
        super().__init__(parent)
        self.connection_service = connection_service
        self.host_ip = host_ip
        self.model = model
        self.timeout = timeout
        self.request_id = None

    def start(self):
        self.connection_service.operation_succeeded.connect(self._on_success)
        self.connection_service.operation_failed.connect(self._on_failed)
        self.request_id = self.connection_service.submit_operation(
            "livox_discovery",
            self._execute,
        )
        if self.request_id is None:
            self._finish_with_error(
                "Jetson is not connected. Connect from Dashboard first."
            )

    async def _execute(self, ssh):
        device = await LivoxSDK2Backend(ssh).discover(
            host_ip=self.host_ip,
            expected_model=self.model,
            timeout=self.timeout,
        )
        return device.to_dict()

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
