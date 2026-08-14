from PySide6.QtCore import QObject, Signal

from core.network.network_manager import NetworkManager
from desktop_app.services.jetson_connection_service import (
    JetsonConnectionService,
)
from devices.livox.profile import LivoxNetworkProfile


class LidarNetworkVerificationWorker(QObject):
    success = Signal(dict)
    failed = Signal(str)
    finished = Signal()

    def __init__(
        self,
        connection_service: JetsonConnectionService,
        network_profile: LivoxNetworkProfile,
        parent=None,
    ):
        super().__init__(parent)
        self.connection_service = connection_service
        self.network_profile = network_profile
        self.request_id = None

    def start(self):
        self.connection_service.operation_succeeded.connect(self._on_success)
        self.connection_service.operation_failed.connect(self._on_failed)
        self.request_id = self.connection_service.submit_operation(
            "lidar_network_verification",
            self._execute,
        )
        if self.request_id is None:
            self._finish_with_error(
                "Jetson is not connected. Connect from Dashboard first."
            )

    async def _execute(self, ssh):
        manager = NetworkManager(ssh)
        snapshot = await manager.inspect()
        verification = manager.verify_lidar_network(
            snapshot=snapshot,
            interface_name=self.network_profile.jetson.interface,
            expected_jetson_cidr=self.network_profile.jetson.cidr,
            expected_lidar_ip=str(self.network_profile.lidar.ip),
        )
        return {
            "network": snapshot.to_dict(),
            "verification": verification.to_dict(),
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
