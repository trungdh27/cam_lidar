from __future__ import annotations

from collections import deque
import time

import numpy as np
import pyqtgraph.opengl as gl

from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


class LidarPointCloudPage(QWidget):
    def __init__(
        self,
        stream_service=None,
        runtime_state=None,
        parent=None,
    ):
        super().__init__(parent)

        self.stream_service = stream_service
        self.runtime_state = runtime_state
        self._paused = False
        self._has_cloud = False
        self._xyz = np.empty((0, 3), dtype=np.float32)
        self._frame_times = deque(maxlen=12)
        self._preview_fps = 0.0
        self._point_count = 0
        self._timestamp = None
        self._data_type = None

        self._build_ui()
        if self.stream_service is not None:
            self.stream_service.point_preview_received.connect(
                self._on_point_preview
            )
        if self.runtime_state is not None:
            self.runtime_state.changed.connect(self._on_runtime_state_changed)
            self._on_runtime_state_changed(self.runtime_state.snapshot())

    def _build_ui(self):
        root = QVBoxLayout(self)

        header = QHBoxLayout()

        title = QLabel("LiDAR Point Cloud")
        title.setStyleSheet(
            "font-size: 18px; font-weight: 700;"
        )

        self.status_label = QLabel("IDLE")

        header.addWidget(title)
        header.addStretch()
        header.addWidget(self.status_label)

        root.addLayout(header)

        self.view = gl.GLViewWidget()

        self.view.setCameraPosition(
            distance=12,
            elevation=20,
            azimuth=45,
        )

        grid = gl.GLGridItem()
        grid.setSize(20, 20)
        grid.setSpacing(1, 1)

        self.view.addItem(grid)

        self.scatter = gl.GLScatterPlotItem(
            pos=np.empty((0, 3), dtype=np.float32),
            size=3.0,
            pxMode=True,
        )

        self.view.addItem(self.scatter)

        root.addWidget(self.view, 1)

        bottom = QHBoxLayout()

        self.stats_label = QLabel("Points: 0  |  Preview FPS: 0.0")
        self.metadata_label = QLabel("Timestamp: --  |  Data type: --")
        self.metadata_label.setObjectName("Muted")

        self.pause_button = QPushButton("PAUSE")
        self.pause_button.setCheckable(True)
        self.clear_button = QPushButton("CLEAR")
        self.fit_button = QPushButton("FIT VIEW")

        self.pause_button.toggled.connect(self._set_paused)
        self.clear_button.clicked.connect(
            self.clear
        )

        self.fit_button.clicked.connect(
            self.fit_view
        )

        bottom.addWidget(self.stats_label)
        bottom.addWidget(self.metadata_label)
        bottom.addStretch()

        bottom.addWidget(self.pause_button)
        bottom.addWidget(self.clear_button)
        bottom.addWidget(self.fit_button)

        root.addLayout(bottom)

    def _on_point_preview(self, payload: dict):
        now = time.monotonic()
        self._frame_times.append(now)
        if len(self._frame_times) > 1:
            elapsed = self._frame_times[-1] - self._frame_times[0]
            if elapsed > 0:
                self._preview_fps = (len(self._frame_times) - 1) / elapsed

        if self._paused:
            self._update_details()
            return

        points = np.asarray(payload["points"], dtype=np.float32)
        if points.size == 0:
            points = np.empty((0, 4), dtype=np.float32)
        else:
            points = points.reshape((-1, 4))
        self._xyz = points[:, :3]
        reflectivity = points[:, 3] / 255.0
        colors = np.empty((len(points), 4), dtype=np.float32)
        colors[:, 0] = reflectivity
        colors[:, 1] = 1.0 - np.abs(2.0 * reflectivity - 1.0)
        colors[:, 2] = 1.0 - reflectivity
        colors[:, 3] = 0.9
        self.scatter.setData(
            pos=self._xyz,
            color=colors,
            size=3.0,
        )
        self._has_cloud = len(points) > 0
        self._point_count = int(payload["count"])
        self._timestamp = payload["timestamp"]
        self._data_type = payload["data_type"]
        self._update_details()

    def _set_paused(self, paused: bool):
        self._paused = paused
        self.pause_button.setText("RESUME" if paused else "PAUSE")
        self._update_details()

    def _on_runtime_state_changed(self, snapshot: dict):
        state = snapshot.get("stream_status", "IDLE")
        if state == "IDLE":
            text = "No point cloud data. Start stream from Monitor."
            if self._has_cloud:
                self.clear()
        elif state == "STARTING":
            text = "Waiting for point cloud data..."
        elif state == "STALE":
            text = "STALE - showing last point cloud."
        elif state == "ERROR":
            text = snapshot.get("last_error") or "Point cloud stream error."
        elif state == "STOPPING":
            text = "Stopping stream..."
        else:
            text = "Live point cloud"
        self.status_label.setText(f"{state}: {text}")

    def _update_details(self):
        paused = "  |  PAUSED" if self._paused else ""
        self.stats_label.setText(
            f"Points: {self._point_count:,}  |  "
            f"Preview FPS: {self._preview_fps:.1f}{paused}"
        )
        self.metadata_label.setText(
            f"Timestamp: {self._timestamp if self._timestamp is not None else '--'}"
            "  |  "
            f"Data type: {self._data_type if self._data_type is not None else '--'}"
        )

    def clear(self):
        self.scatter.setData(
            pos=np.empty(
                (0, 3),
                dtype=np.float32,
            )
        )
        self._xyz = np.empty((0, 3), dtype=np.float32)
        self._has_cloud = False
        self._point_count = 0
        self._timestamp = None
        self._data_type = None
        self._update_details()

    def fit_view(self):
        distance = 12.0
        if self._has_cloud:
            radius = float(np.max(np.linalg.norm(self._xyz, axis=1)))
            distance = max(3.0, min(200.0, radius * 2.5))
        self.view.setCameraPosition(
            distance=distance,
            elevation=20,
            azimuth=45,
        )
