from __future__ import annotations

import json
import shlex

from PySide6.QtCore import QObject, Signal

from desktop_app.services.jetson_connection_service import (
    JetsonConnectionService,
)
from desktop_app.state.lidar_runtime_state import (
    LidarRuntimeState,
    LidarStreamStatus,
)
from devices.livox.profile import LivoxNetworkProfile


class LidarStreamService(QObject):
    """Manage one Livox runtime helper on the shared Jetson SSH session."""

    STREAM_HELPER = "$HOME/.cam_lidar/bin/livox_stream"
    METRICS_MARKER = "LIVOX_STREAM_METRICS="
    EVENT_MARKER = "LIVOX_STREAM_EVENT="

    log = Signal(str, str)

    def __init__(
        self,
        connection_service: JetsonConnectionService,
        runtime_state: LidarRuntimeState,
        network_profile: LivoxNetworkProfile,
        parent=None,
    ):
        super().__init__(parent)
        self.connection_service = connection_service
        self.runtime_state = runtime_state
        self.network_profile = network_profile
        self.request_id = None
        self._stop_requested = False
        self._first_point_reported = False
        self._imu_active_reported = False
        self._streaming_reported = False
        self._sdk_uninitialized = False
        self._last_stream_metric_state = None
        self._last_imu_status = None
        self._stderr_lines = []

        connection_service.remote_process_started.connect(
            self._on_process_started
        )
        connection_service.remote_process_output.connect(
            self._on_process_output
        )
        connection_service.remote_process_finished.connect(
            self._on_process_finished
        )
        connection_service.remote_process_failed.connect(
            self._on_process_failed
        )
        connection_service.disconnected.connect(self._on_disconnected)

    @property
    def active(self) -> bool:
        return self.request_id is not None

    def start(self, model: str) -> bool:
        if self.active or self.runtime_state.stream_status in {
            LidarStreamStatus.STARTING,
            LidarStreamStatus.STREAMING,
            LidarStreamStatus.STALE,
            LidarStreamStatus.STOPPING,
        }:
            self.log.emit("WARNING", "LiDAR stream process is already active")
            return False
        if not self.connection_service.is_connected:
            self.runtime_state.set_status(
                LidarStreamStatus.ERROR,
                error="Jetson is not connected",
            )
            self.log.emit("ERROR", "Jetson is not connected")
            return False

        normalized_model = str(model).strip().upper()
        self.network_profile.model(normalized_model)
        self._stop_requested = False
        self._first_point_reported = False
        self._imu_active_reported = False
        self._streaming_reported = False
        self._sdk_uninitialized = False
        self._last_stream_metric_state = None
        self._last_imu_status = None
        self._stderr_lines = []
        self.runtime_state.set_status(LidarStreamStatus.STARTING)

        host_ip = str(self.network_profile.jetson.ip)
        lidar_ip = str(self.network_profile.lidar.ip)
        command = self._build_command(
            host_ip=host_ip,
            lidar_ip=lidar_ip,
            model=normalized_model,
        )
        self.log.emit("INFO", f"Starting {normalized_model} stream")
        self.log.emit("INFO", f"Host {host_ip}")
        self.log.emit("INFO", f"LiDAR {lidar_ip}")
        request_id = self.connection_service.start_remote_process(
            "livox_stream",
            command,
        )
        if request_id is None:
            self.runtime_state.set_status(
                LidarStreamStatus.ERROR,
                error="Jetson is not connected",
            )
            return False
        self.request_id = request_id
        return True

    def stop(self) -> bool:
        if not self.active:
            self.log.emit("INFO", "LiDAR stream is already idle")
            return False
        if self.runtime_state.stream_status is LidarStreamStatus.STOPPING:
            return False
        self._stop_requested = True
        self.runtime_state.set_status(LidarStreamStatus.STOPPING)
        self.log.emit("INFO", "Stopping LiDAR stream")
        self.connection_service.stop_remote_process(self.request_id)
        return True

    def shutdown(self) -> None:
        if self.active:
            self.stop()

    def _build_command(self, host_ip: str, lidar_ip: str, model: str) -> str:
        helper = '"$HOME/.cam_lidar/bin/livox_stream"'
        missing = (
            "Livox stream helper is missing on Jetson. Deploy it with: "
            "./scripts/deploy_livox_stream_helper.sh <user>@<jetson-ip>"
        )
        return (
            f"if ! test -x {helper}; then "
            f"printf '%s\\n' {shlex.quote(missing)} >&2; exit 127; fi; "
            f"exec {helper} "
            f"--host-ip {shlex.quote(host_ip)} "
            f"--expected-lidar-ip {shlex.quote(lidar_ip)} "
            f"--model {shlex.quote(model)} "
            "--metrics-interval-ms 500"
        )

    def _on_process_started(self, request_id: str) -> None:
        if request_id != self.request_id:
            return
        self.log.emit("INFO", "Livox stream helper started")

    def _on_process_output(
        self,
        request_id: str,
        stream_name: str,
        line: str,
    ) -> None:
        if request_id != self.request_id:
            return
        if stream_name == "stderr":
            if line.strip():
                self._stderr_lines.append(line.strip())
                self._stderr_lines = self._stderr_lines[-20:]
            return
        if line.startswith(self.METRICS_MARKER):
            self._handle_metrics(line[len(self.METRICS_MARKER) :])
        elif line.startswith(self.EVENT_MARKER):
            self._handle_event(line[len(self.EVENT_MARKER) :])

    def _handle_metrics(self, payload_text: str) -> None:
        try:
            payload = json.loads(payload_text)
        except json.JSONDecodeError:
            self._fail("Invalid JSON from Livox stream helper")
            return
        if not isinstance(payload, dict):
            self._fail("Livox stream metrics payload is not an object")
            return

        self.runtime_state.update_metrics(payload)
        metric_state = payload.get("state")
        if metric_state != self._last_stream_metric_state:
            if metric_state == "STALE":
                self.log.emit("WARNING", "Point cloud stream is stale")
            elif (
                metric_state == "STREAMING"
                and self._last_stream_metric_state == "STALE"
            ):
                self.log.emit("PASS", "Point cloud stream recovered")
            self._last_stream_metric_state = metric_state

        imu_status = payload.get("imu_status")
        if imu_status != self._last_imu_status:
            if imu_status == "STALE":
                self.log.emit("WARNING", "IMU data is stale")
            elif imu_status == "ACTIVE" and self._last_imu_status == "STALE":
                self.log.emit("PASS", "IMU data recovered")
            self._last_imu_status = imu_status
        if payload.get("point_counter", 0) > 0 and not self._first_point_reported:
            self._first_point_reported = True
            self.log.emit("PASS", "First point cloud data received")
        if payload.get("imu_status") == "ACTIVE" and not self._imu_active_reported:
            self._imu_active_reported = True
            self.log.emit("PASS", "IMU data active")
        if payload.get("state") == "STREAMING" and not self._streaming_reported:
            self._streaming_reported = True
            self.log.emit("INFO", "Stream state STREAMING")

    def _handle_event(self, payload_text: str) -> None:
        try:
            payload = json.loads(payload_text)
        except json.JSONDecodeError:
            self._fail("Invalid event JSON from Livox stream helper")
            return
        event = payload.get("event")
        if event == "sdk_ready":
            version = payload.get("sdk_version") or "unknown"
            self.log.emit("INFO", f"SDK {version}")
        elif event == "sdk_uninitialized":
            self._sdk_uninitialized = True
            self.log.emit("PASS", "Livox SDK uninitialized")
        elif event == "error":
            self._fail(payload.get("error") or "Livox stream helper error")

    def _on_process_finished(self, request_id: str, exit_status: int) -> None:
        if request_id != self.request_id:
            return
        expected_stop = self._stop_requested
        self.request_id = None
        if expected_stop:
            if exit_status == 0 and not self._sdk_uninitialized:
                self.log.emit("WARNING", "Stream exited without SDK stop event")
            elif exit_status != 0:
                self.log.emit(
                    "WARNING",
                    f"Stream required forced termination (exit {exit_status})",
                )
            self.runtime_state.set_status(LidarStreamStatus.IDLE)
            self.log.emit("PASS", "Stream stopped")
            return
        if exit_status == 0:
            self._fail("Livox stream process exited unexpectedly")
            return
        detail = self._stderr_lines[-1] if self._stderr_lines else "no stderr"
        self._fail(
            f"Livox stream process exited with code {exit_status}: {detail}"
        )

    def _on_process_failed(self, request_id: str, error: str) -> None:
        if request_id != self.request_id:
            return
        self.request_id = None
        self._fail(error)

    def _on_disconnected(self) -> None:
        if not self.active:
            return
        self.request_id = None
        self._fail("Jetson disconnected while LiDAR stream was active")

    def _fail(self, error: str) -> None:
        self.runtime_state.set_status(LidarStreamStatus.ERROR, error=error)
        self.log.emit("ERROR", error)
