from PySide6.QtCore import QObject, Signal

from devices.camera.inventory import CameraInventory


class CameraInventoryService(QObject):
    """Qt coordinator that runs inventory through the shared Jetson connection."""

    started = Signal()
    completed = Signal(object)
    failed = Signal(str)
    cleared = Signal()

    def __init__(self, jetson_service, inventory=None, parent=None):
        super().__init__(parent)
        self.jetson_service = jetson_service
        self.inventory = inventory or CameraInventory()
        self._request_id = None
        jetson_service.operation_succeeded.connect(self._on_operation_succeeded)
        jetson_service.operation_failed.connect(self._on_operation_failed)
        jetson_service.disconnected.connect(self.clear_stale_inventory)
        jetson_service.connection_failed.connect(
            lambda _error: self.clear_stale_inventory()
        )

    @property
    def busy(self) -> bool:
        return self._request_id is not None

    def discover_all(self) -> bool:
        if self.busy:
            return False
        if not self.jetson_service.is_connected:
            self.failed.emit(
                "BLOCKED: Jetson is not connected. Connect from Dashboard first."
            )
            return False
        request_id = self.jetson_service.submit_operation(
            "camera_inventory",
            lambda ssh: self.inventory.discover_all(ssh),
        )
        if request_id is None:
            self.failed.emit(
                "BLOCKED: Jetson is not connected. Connect from Dashboard first."
            )
            return False
        self._request_id = request_id
        self.started.emit()
        return True

    def refresh(self) -> bool:
        return self.discover_all()

    def get_devices(self):
        return self.inventory.get_devices()

    def get_device(self, device_uid):
        return self.inventory.get_device(device_uid)

    def get_by_vendor(self, vendor):
        return self.inventory.get_by_vendor(vendor)

    def get_by_serial(self, serial):
        return self.inventory.get_by_serial(serial)

    def clear_stale_inventory(self) -> None:
        self.inventory.clear_stale_inventory()
        self.cleared.emit()

    def _on_operation_succeeded(self, request_id, result):
        if request_id != self._request_id:
            return
        self._request_id = None
        self.completed.emit(result)

    def _on_operation_failed(self, request_id, error):
        if request_id != self._request_id:
            return
        self._request_id = None
        self.failed.emit(error)
