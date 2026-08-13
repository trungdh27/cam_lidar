from PySide6.QtCore import QThread, Signal

from devices.camera.service import CameraService


class CameraConnectionWorker(QThread):
    succeeded = Signal(str, dict)
    failed = Signal(str, str)

    def __init__(self, service: CameraService, action: str, payload: dict, parent=None):
        super().__init__(parent)
        if action not in ("connect", "disconnect"):
            raise ValueError(f"Unsupported connection action: {action}")
        self.service = service
        self.action = action
        self.payload = payload

    def run(self):
        try:
            result = self.service.execute(self.action, self.payload)
            self.succeeded.emit(self.action, result)
        except Exception as exc:
            self.failed.emit(self.action, f"{type(exc).__name__}: {exc}")
