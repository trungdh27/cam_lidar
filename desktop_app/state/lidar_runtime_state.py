from __future__ import annotations

from enum import Enum

from PySide6.QtCore import QObject, Signal


class LidarStreamStatus(str, Enum):
    IDLE = "IDLE"
    STARTING = "STARTING"
    STREAMING = "STREAMING"
    STALE = "STALE"
    STOPPING = "STOPPING"
    ERROR = "ERROR"


class LidarRuntimeState(QObject):
    """Widget-free runtime metrics for the active LiDAR stream session."""

    changed = Signal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.stream_status = LidarStreamStatus.IDLE
        self.cloud_rate_hz = None
        self.point_count = None
        self.point_count_unit = "pts/s"
        self.point_packet_rate_hz = None
        self.imu_status = "IDLE"
        self.imu_rate_hz = None
        self.packet_loss_percent = None
        self.packet_loss_supported = False
        self.lidar_timestamp = None
        self.lidar_time_type = None
        self.last_packet_age_ms = None
        self.packet_counter = 0
        self.point_packet_counter = 0
        self.lost_point_packet_counter = 0
        self.point_counter = 0
        self.uptime_sec = 0.0
        self.last_error = None

    def snapshot(self) -> dict:
        return {
            "stream_status": self.stream_status.value,
            "cloud_rate_hz": self.cloud_rate_hz,
            "point_count": self.point_count,
            "point_count_unit": self.point_count_unit,
            "point_packet_rate_hz": self.point_packet_rate_hz,
            "imu_status": self.imu_status,
            "imu_rate_hz": self.imu_rate_hz,
            "packet_loss_percent": self.packet_loss_percent,
            "packet_loss_supported": self.packet_loss_supported,
            "lidar_timestamp": self.lidar_timestamp,
            "lidar_time_type": self.lidar_time_type,
            "last_packet_age_ms": self.last_packet_age_ms,
            "packet_counter": self.packet_counter,
            "point_packet_counter": self.point_packet_counter,
            "lost_point_packet_counter": self.lost_point_packet_counter,
            "point_counter": self.point_counter,
            "uptime_sec": self.uptime_sec,
            "last_error": self.last_error,
        }

    def set_status(
        self,
        status: LidarStreamStatus | str,
        *,
        error: str | None = None,
    ) -> None:
        self.stream_status = LidarStreamStatus(status)
        self.last_error = error
        if self.stream_status is LidarStreamStatus.IDLE:
            self.imu_status = "IDLE"
        self.changed.emit(self.snapshot())

    def update_metrics(self, metrics: dict) -> None:
        state = metrics.get("state")
        if state:
            self.stream_status = LidarStreamStatus(state)
        for field in (
            "cloud_rate_hz",
            "point_count",
            "point_count_unit",
            "point_packet_rate_hz",
            "imu_status",
            "imu_rate_hz",
            "packet_loss_percent",
            "packet_loss_supported",
            "lidar_timestamp",
            "lidar_time_type",
            "last_packet_age_ms",
            "packet_counter",
            "point_packet_counter",
            "lost_point_packet_counter",
            "point_counter",
            "uptime_sec",
        ):
            if field in metrics:
                setattr(self, field, metrics[field])
        self.last_error = metrics.get("error")
        self.changed.emit(self.snapshot())
