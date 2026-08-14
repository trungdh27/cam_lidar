from PySide6.QtCore import QObject, QTimer, Signal


class CameraStreamController(QObject):
    """Coordinates short stream lifecycle/status jobs on the shared Jetson queue."""

    started = Signal(dict)
    stopped = Signal(dict)
    metrics_received = Signal(dict)
    failed = Signal(str, str)
    preview_fallback_ready = Signal()

    def __init__(self, jetson_service, camera_service, parent=None):
        super().__init__(parent)
        self.jetson_service = jetson_service
        self.camera_service = camera_service
        self.payload = None
        self.pending_request_id = None
        self.pending_action = None
        self.timer = QTimer(self)
        self.timer.setInterval(500)
        self.timer.timeout.connect(self._poll)
        jetson_service.operation_succeeded.connect(self._operation_succeeded)
        jetson_service.operation_failed.connect(self._operation_failed)

    @property
    def active(self):
        return self.timer.isActive()

    def start(self, payload):
        self.payload = dict(payload)
        self._submit("start_stream")

    def stop(self):
        self.timer.stop()
        if self.payload:
            self._submit("stop_stream")

    def stop_for_shutdown(self):
        """Best-effort asynchronous stop queued before service shutdown."""
        if self.payload and self.active:
            self.stop()

    def request_preview_fallback(self):
        return self._submit("preview_fallback")

    def _poll(self):
        if self.pending_request_id is None and self.jetson_service.is_connected:
            self._submit("stream_status")

    def _submit(self, action):
        if self.pending_request_id is not None:
            return False
        if not self.jetson_service.is_connected:
            self.failed.emit(action, "Jetson is not connected. Connect from Dashboard first.")
            return False
        request_id = self.jetson_service.submit_operation(
            f"camera_{action}",
            lambda ssh: self.camera_service.execute_with_ssh(ssh, action, self.payload),
        )
        if request_id is None:
            self.failed.emit(action, "Jetson is not connected.")
            return False
        self.pending_request_id, self.pending_action = request_id, action
        return True

    def _operation_succeeded(self, request_id, result):
        if request_id != self.pending_request_id:
            return
        action = self.pending_action
        self.pending_request_id = self.pending_action = None
        result = result or {}
        if action == "start_stream":
            self.timer.start()
            self.started.emit(result)
        elif action == "stop_stream":
            status = result.get("status", {})
            if status:
                self.metrics_received.emit(status)
            self.payload = None
            self.stopped.emit(result)
        elif action == "preview_fallback":
            self.preview_fallback_ready.emit()
        else:
            status = result.get("status", {})
            if status.get("state") in ("failed", "stopped") or not status.get("process_alive", True):
                self.timer.stop()
                self.failed.emit("stream_status", status.get("last_error") or "Remote stream worker exited.")
            else:
                self.metrics_received.emit(status)

    def _operation_failed(self, request_id, error):
        if request_id != self.pending_request_id:
            return
        action = self.pending_action
        self.pending_request_id = self.pending_action = None
        if action in ("start_stream", "stream_status"):
            self.timer.stop()
        self.failed.emit(action, error)
