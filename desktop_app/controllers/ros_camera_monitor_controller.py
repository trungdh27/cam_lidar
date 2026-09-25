from PySide6.QtCore import QObject, QTimer, Signal

from devices.camera.remote_ros_monitor_adapter import RemoteRosMonitorAdapter


class RosCameraMonitorController(QObject):
    """Schedules bounded read-only ROS samples through the shared Jetson queue."""

    sample_received = Signal(dict)
    failed = Signal(str)
    stopped = Signal()

    def __init__(self, jetson_service, parent=None, adapter=None):
        super().__init__(parent)
        self.jetson_service = jetson_service
        self.adapter = adapter or RemoteRosMonitorAdapter()
        self.payload = None
        self.pending_request_id = None
        self.timer = QTimer(self)
        self.timer.setInterval(700)
        self.timer.timeout.connect(self._sample)
        jetson_service.operation_succeeded.connect(self._operation_succeeded)
        jetson_service.operation_failed.connect(self._operation_failed)

    @property
    def active(self):
        return self.timer.isActive()

    def start(self, payload):
        self.payload = dict(payload)
        self.timer.start()
        self._sample()

    def stop(self):
        self.timer.stop()
        self.payload = None
        self.stopped.emit()

    def _sample(self):
        if self.pending_request_id is not None or not self.payload:
            return
        request_id = self.jetson_service.submit_operation(
            "camera_ros_monitor_sample",
            lambda ssh: self.adapter.sample_with_ssh(ssh, self.payload),
        )
        if request_id is None:
            self.timer.stop()
            self.failed.emit("Jetson is not connected. Connect from Dashboard first.")
            return
        self.pending_request_id = request_id

    def _operation_succeeded(self, request_id, result):
        if request_id != self.pending_request_id:
            return
        self.pending_request_id = None
        self.sample_received.emit(result or {})

    def _operation_failed(self, request_id, error):
        if request_id != self.pending_request_id:
            return
        self.pending_request_id = None
        # A missed bounded sample is health information, not a request to alter
        # the externally-owned publisher.
        self.failed.emit(str(error))
