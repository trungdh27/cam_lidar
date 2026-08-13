from PySide6.QtCore import QThread, Signal

from devices.camera.service import CameraService


class CameraActionWorker(QThread):
    """Signal boundary for camera operations that may block in later phases."""

    succeeded = Signal(str, dict)
    failed = Signal(str, str)

    def __init__(
        self,
        service: CameraService,
        action: str,
        payload: dict,
        parent=None,
    ):
        super().__init__(parent)
        self.service = service
        self.action = action
        self.payload = payload

    def run(self):
        try:
            result = self.service.execute(self.action, self.payload)
            self.succeeded.emit(self.action, result or {})
        except Exception as exc:
            self.failed.emit(self.action, f"{type(exc).__name__}: {exc}")
