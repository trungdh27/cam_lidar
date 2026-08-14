from __future__ import annotations

from PySide6.QtCore import QObject, Signal

from desktop_app.services.jetson_connection_service import (
    JetsonConnectionService,
)
from desktop_app.workers.livox_discovery_worker import LivoxDiscoveryWorker
from devices.livox.profile import LivoxNetworkProfile


class LidarDiscoveryService(QObject):
    """Share one non-blocking Livox discovery workflow across UI and tests."""

    started = Signal(str)
    progress = Signal(str, str)
    stage_changed = Signal(str)
    preflight_updated = Signal(dict)
    completed = Signal(dict)
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
        self.worker = None
        self.current_stage = "IDLE"

    @property
    def busy(self) -> bool:
        return self.worker is not None

    def start(self, model: str, timeout: int = 8) -> bool:
        if self.busy or not self.connection_service.is_connected:
            return False
        normalized_model = str(model).strip().upper()
        self.network_profile.model(normalized_model)
        worker = LivoxDiscoveryWorker(
            connection_service=self.connection_service,
            network_profile=self.network_profile,
            model=normalized_model,
            timeout=timeout,
        )
        self.worker = worker
        worker.success.connect(self.completed.emit)
        worker.failed.connect(self.failed.emit)
        worker.progress.connect(self.progress.emit)
        worker.preflight_updated.connect(self.preflight_updated.emit)
        worker.stage_changed.connect(self._on_stage_changed)
        worker.finished.connect(self._on_finished)
        self.started.emit(normalized_model)
        worker.start()
        return True

    def _on_stage_changed(self, stage: str) -> None:
        self.current_stage = stage
        self.stage_changed.emit(stage)

    def _on_finished(self) -> None:
        self.worker = None
        self.finished.emit()
