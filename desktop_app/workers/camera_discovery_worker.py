from PySide6.QtCore import QThread, Signal

from devices.camera.service import CameraService


class CameraDiscoveryWorker(QThread):
    succeeded = Signal(dict)
    failed = Signal(str)

    def __init__(self, service: CameraService, payload: dict, parent=None):
        super().__init__(parent)
        self.service = service
        self.payload = payload

    def run(self):
        try:
            self.succeeded.emit(self.service.execute("discover", self.payload))
        except Exception as exc:
            self.failed.emit(f"{type(exc).__name__}: {exc}")
