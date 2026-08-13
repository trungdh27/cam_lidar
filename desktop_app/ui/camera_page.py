import html
from datetime import datetime, timezone

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from desktop_app.ui.dialogs import SSHConnectionDialog
from desktop_app.ui.widgets import Card, StatusChip
from desktop_app.workers.camera_connection_worker import CameraConnectionWorker
from desktop_app.workers.camera_discovery_worker import CameraDiscoveryWorker
from desktop_app.workers.camera_worker import CameraActionWorker
from devices.camera.models import CameraConnectionState
from devices.camera.service import CameraService


class CameraPage(QWidget):
    discovery_requested = Signal(dict)
    connection_requested = Signal(dict)
    disconnection_requested = Signal(dict)
    stream_start_requested = Signal(dict)
    stream_stop_requested = Signal(dict)
    tests_requested = Signal(list)

    TEST_CASES = (
        ("CAM-CON-001", "Device Discovery", "Connectivity", "10 s"),
        ("CAM-CON-002", "Open Camera Device", "Connectivity", "10 s"),
        ("CAM-STR-001", "Start and Stop Stream", "Streaming", "20 s"),
        ("CAM-STR-002", "Validate Resolution and FPS", "Streaming", "30 s"),
        ("CAM-STR-003", "Frame Continuity / Drop Check", "Reliability", "60 s"),
        ("CAM-IMG-001", "Basic Image Availability", "Image Quality", "20 s"),
    )

    def __init__(self, parent=None, camera_service=None):
        super().__init__(parent)
        self.camera_service = camera_service or CameraService()
        self.connection_state = CameraConnectionState.DISCONNECTED
        self.camera_worker = None
        self.ssh_config = None

        self._build_ui()
        self._load_profiles()
        self._load_test_cases()
        self._reset_runtime_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 14, 18, 14)
        root.setSpacing(10)

        header = QHBoxLayout()
        title_block = QVBoxLayout()
        title = QLabel("Camera Tests")
        title.setObjectName("PageTitle")
        breadcrumb = QLabel("Devices  /  Camera")
        breadcrumb.setObjectName("Muted")
        title_block.addWidget(title)
        title_block.addWidget(breadcrumb)
        header.addLayout(title_block)
        header.addStretch()

        self.connection_chip = StatusChip("Disconnected", "idle")
        self.device_chip = StatusChip("Device Not Ready", "idle")
        self.stream_chip = StatusChip("Stream Idle", "idle")
        header.addWidget(self.connection_chip)
        header.addWidget(self.device_chip)
        header.addWidget(self.stream_chip)
        root.addLayout(header)

        top_row = QHBoxLayout()
        top_row.setSpacing(10)
        top_row.addWidget(self._build_device_card(), 1)
        top_row.addWidget(self._build_overview_card(), 1)
        root.addLayout(top_row)

        middle_row = QHBoxLayout()
        middle_row.setSpacing(10)
        middle_row.addWidget(self._build_stream_card(), 1)
        middle_row.addWidget(self._build_monitor_card(), 1)
        root.addLayout(middle_row)

        root.addWidget(self._build_test_card())
        root.addWidget(self._build_log_card(), 1)

    def _build_device_card(self):
        card = Card("Camera Device")
        grid = QGridLayout()
        grid.addWidget(QLabel("Model:"), 0, 0)
        self.model_combo = QComboBox()
        self.model_combo.currentIndexChanged.connect(self._on_profile_changed)
        grid.addWidget(self.model_combo, 0, 1, 1, 3)

        grid.addWidget(QLabel("Connection:"), 1, 0)
        self.connection_status_label = QLabel("DISCONNECTED")
        self.connection_status_label.setObjectName("Muted")
        grid.addWidget(self.connection_status_label, 1, 1, 1, 3)
        grid.setColumnStretch(3, 1)
        card.body_layout.addLayout(grid)

        actions = QHBoxLayout()
        self.discover_button = QPushButton("⌕  AUTO DISCOVER")
        self.discover_button.setObjectName("OutlineButton")
        self.connect_button = QPushButton("↔  CONNECT")
        self.connect_button.setObjectName("PrimaryButton")
        self.disconnect_button = QPushButton("DISCONNECT")
        self.disconnect_button.setObjectName("DangerButton")
        actions.addWidget(self.discover_button)
        actions.addWidget(self.connect_button)
        actions.addWidget(self.disconnect_button)
        actions.addStretch()
        card.body_layout.addLayout(actions)

        self.discover_button.clicked.connect(lambda: self._request_action("discover"))
        self.connect_button.clicked.connect(lambda: self._request_action("connect"))
        self.disconnect_button.clicked.connect(lambda: self._request_action("disconnect"))
        return card

    def _build_overview_card(self):
        card = Card("Device Overview")
        overview_fields = (
            "Model", "SDK Model", "Serial Number", "Interface",
            "Execution Host", "Resolution", "FPS", "Camera State",
        )
        self.overview_table = QTableWidget(len(overview_fields), 2)
        self.overview_table.setHorizontalHeaderLabels(["Parameter", "Value"])
        self._configure_read_only_table(self.overview_table)
        self.overview_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.overview_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        for row, name in enumerate(overview_fields):
            self.overview_table.setItem(row, 0, QTableWidgetItem(name))
            self.overview_table.setItem(row, 1, QTableWidgetItem("-"))
        card.body_layout.addWidget(self.overview_table)
        return card

    def _build_stream_card(self):
        card = Card("Stream Configuration")
        grid = QGridLayout()
        self.execution_host_combo = QComboBox()
        self.execution_host_combo.currentTextChanged.connect(
            self._on_execution_host_changed
        )
        self.jetson_target_label = QLabel("Not configured")
        self.jetson_target_label.setObjectName("Muted")
        self.configure_jetson_button = QPushButton("Configure Jetson")
        self.configure_jetson_button.setObjectName("SmallButton")
        self.configure_jetson_button.clicked.connect(self._configure_jetson)
        self.device_combo = QComboBox()
        self.resolution_combo = QComboBox()
        self.fps_combo = QComboBox()
        self.format_combo = QComboBox()

        fields = (
            ("Execution Host:", self.execution_host_combo),
            ("Device / Serial:", self.device_combo),
            ("Resolution:", self.resolution_combo),
            ("FPS:", self.fps_combo),
            ("Pixel / Stream Format:", self.format_combo),
        )
        for row, (label, widget) in enumerate(fields):
            grid.addWidget(QLabel(label), row, 0)
            grid.addWidget(widget, row, 1)
        jetson_row = QHBoxLayout()
        jetson_row.addWidget(self.jetson_target_label)
        jetson_row.addStretch()
        jetson_row.addWidget(self.configure_jetson_button)
        grid.addWidget(QLabel("Jetson Target:"), len(fields), 0)
        grid.addLayout(jetson_row, len(fields), 1)
        grid.setColumnStretch(1, 1)
        card.body_layout.addLayout(grid)

        self.resolution_combo.currentIndexChanged.connect(self._update_stream_options)
        self.fps_combo.currentTextChanged.connect(self._on_fps_changed)
        actions = QHBoxLayout()
        self.start_stream_button = QPushButton("▶  START STREAM")
        self.start_stream_button.setObjectName("PrimaryButton")
        self.stop_stream_button = QPushButton("■  STOP STREAM")
        self.stop_stream_button.setObjectName("DangerButton")
        actions.addWidget(self.start_stream_button)
        actions.addWidget(self.stop_stream_button)
        actions.addStretch()
        card.body_layout.addLayout(actions)

        self.start_stream_button.clicked.connect(lambda: self._request_action("start_stream"))
        self.stop_stream_button.clicked.connect(lambda: self._request_action("stop_stream"))
        return card

    def _build_monitor_card(self):
        card = Card("Live Monitor")
        self.monitor_table = QTableWidget(8, 3)
        self.monitor_table.setHorizontalHeaderLabels(["Parameter", "Value", "Unit"])
        self._configure_read_only_table(self.monitor_table)
        header = self.monitor_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        rows = (
            ("Actual FPS", "--", "fps"),
            ("Frame Interval", "--", "ms"),
            ("Dropped Frames", "0", "frames"),
            ("Frame Counter", "0", "frames"),
            ("Exposure", "--", ""),
            ("Gain", "--", ""),
            ("Temperature", "--", "°C"),
            ("Timestamp", "--", ""),
        )
        for row, values in enumerate(rows):
            for column, value in enumerate(values):
                self.monitor_table.setItem(row, column, QTableWidgetItem(value))
        card.body_layout.addWidget(self.monitor_table)
        return card

    def _build_test_card(self):
        card = Card()
        header = QHBoxLayout()
        title = QLabel("Test Case List")
        title.setObjectName("CardTitle")
        self.selected_tests_label = QLabel("0 / 0 Selected")
        self.selected_tests_label.setStyleSheet("color:#155EEF; font-weight:700;")
        self.run_tests_button = QPushButton("▶  RUN SELECTED TESTS")
        self.run_tests_button.setObjectName("PrimaryButton")
        self.run_tests_button.clicked.connect(self._run_selected_tests)
        header.addWidget(title)
        header.addStretch()
        header.addWidget(self.selected_tests_label)
        header.addWidget(self.run_tests_button)
        card.body_layout.addLayout(header)

        self.test_table = QTableWidget(0, 6)
        self.test_table.setHorizontalHeaderLabels(
            ["Select", "Test Case ID", "Test Name", "Category", "Estimated Time", "Status"]
        )
        self.test_table.verticalHeader().setVisible(False)
        self.test_table.setAlternatingRowColors(True)
        self.test_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table_header = self.test_table.horizontalHeader()
        for column in (0, 1, 3, 4, 5):
            table_header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        table_header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.test_table.itemChanged.connect(self._update_selected_test_count)
        card.body_layout.addWidget(self.test_table)
        return card

    def _build_log_card(self):
        card = Card()
        header = QHBoxLayout()
        title = QLabel("Live Log")
        title.setObjectName("CardTitle")
        self.auto_scroll_check = QCheckBox("Auto Scroll")
        self.auto_scroll_check.setChecked(True)
        clear_button = QPushButton("Clear")
        clear_button.setObjectName("SmallButton")
        clear_button.clicked.connect(self.live_log_clear)
        export_button = QPushButton("Export")
        export_button.setObjectName("SmallButton")
        export_button.clicked.connect(self.export_log)
        header.addWidget(title)
        header.addStretch()
        header.addWidget(self.auto_scroll_check)
        header.addWidget(clear_button)
        header.addWidget(export_button)
        card.body_layout.addLayout(header)

        self.live_log = QTextEdit()
        self.live_log.setObjectName("LiveLog")
        self.live_log.setReadOnly(True)
        self.live_log.setMinimumHeight(110)
        card.body_layout.addWidget(self.live_log)
        return card

    @staticmethod
    def _configure_read_only_table(table):
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        table.setAlternatingRowColors(True)

    def _load_profiles(self):
        self.model_combo.blockSignals(True)
        for profile in self.camera_service.profiles():
            self.model_combo.addItem(profile.display_name, profile.profile_id)
        self.model_combo.blockSignals(False)
        self._on_profile_changed()

    def _on_profile_changed(self, *_):
        profile_id = self.model_combo.currentData()
        if not profile_id:
            return
        profile = self.camera_service.profile(profile_id)

        self.execution_host_combo.clear()
        self.execution_host_combo.addItems(profile.execution_hosts)
        if profile.backend == "zed" and "Jetson" in profile.execution_hosts:
            self.execution_host_combo.setCurrentText("Jetson")
        self.device_combo.clear()
        self.device_combo.addItem("Auto discover required", None)
        self.resolution_combo.clear()
        self._load_stream_capabilities(profile.profile_id)
        self._set_overview(
            {
                "Model": profile.display_name,
                "Interface": profile.interface_hint,
                "SDK / Driver": profile.sdk_driver,
            }
        )
        self._set_state(CameraConnectionState.DISCONNECTED)
        self._on_execution_host_changed(self.execution_host_combo.currentText())
        self.append_log("INFO", f"Camera profile selected: {profile.display_name}.")

    def _load_stream_capabilities(self, profile_id):
        profile = self.camera_service.profile(profile_id)
        self.resolution_combo.blockSignals(True)
        self.resolution_combo.clear()
        for stream_profile in profile.stream_profiles:
            self.resolution_combo.addItem(
                stream_profile.display_name, stream_profile.key
            )
        self.resolution_combo.blockSignals(False)
        self.resolution_combo.setCurrentIndex(0)
        self._update_stream_options()

    def _on_execution_host_changed(self, execution_host):
        is_jetson = execution_host == "Jetson"
        self.jetson_target_label.setVisible(is_jetson)
        self.configure_jetson_button.setVisible(is_jetson)
        if is_jetson:
            if self.ssh_config:
                self.jetson_target_label.setText(
                    f"{self.ssh_config.username}@{self.ssh_config.host}:{self.ssh_config.port}"
                )
            else:
                self.jetson_target_label.setText("Not configured")

    def _configure_jetson(self):
        dialog = SSHConnectionDialog(self.ssh_config, self)
        dialog.setWindowTitle("Camera Jetson Connection")
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return False
        config = dialog.get_config()
        if not config.host or not config.username:
            self.append_log("ERROR", "Jetson host and username are required.")
            return False
        self.ssh_config = config
        self._on_execution_host_changed("Jetson")
        self.append_log(
            "INFO",
            f"Jetson target configured: {config.username}@{config.host}:{config.port}.",
        )
        return True

    def _update_stream_options(self, *_):
        profile_id = self.model_combo.currentData()
        index = self.resolution_combo.currentIndex()
        if not profile_id or index < 0:
            return
        stream_profile = self.camera_service.profile(profile_id).stream_profiles[index]
        previous_fps = self.fps_combo.currentText()
        self.fps_combo.blockSignals(True)
        self.fps_combo.clear()
        self.fps_combo.addItems([str(value) for value in stream_profile.fps])
        if previous_fps in [str(value) for value in stream_profile.fps]:
            self.fps_combo.setCurrentText(previous_fps)
        self.fps_combo.blockSignals(False)
        self.format_combo.clear()
        self.format_combo.addItems(stream_profile.formats)
        self.append_log(
            "INFO",
            f"Resolution selected: {stream_profile.label} "
            f"({stream_profile.resolution.replace(' ', '')})",
        )
        self.append_log("INFO", f"Available FPS: {list(stream_profile.fps)}")
        self._on_fps_changed(self.fps_combo.currentText())

    def _on_fps_changed(self, fps):
        if fps:
            self.append_log("INFO", f"FPS selected: {fps}")

    def _action_payload(self):
        return {
            "profile_id": self.model_combo.currentData(),
            "execution_host": self.execution_host_combo.currentText(),
            "device_id": self.device_combo.currentData(),
            "resolution_key": self.resolution_combo.currentData(),
            "resolution": self.resolution_combo.currentText(),
            "fps": int(self.fps_combo.currentText() or 0),
            "pixel_format": self.format_combo.currentText(),
            "ssh_config": self.ssh_config,
        }

    def _request_action(self, action):
        if self.camera_worker and self.camera_worker.isRunning():
            self.append_log("WARNING", "A camera operation is already running.")
            return

        if (
            action in ("discover", "connect")
            and self.execution_host_combo.currentText() == "Jetson"
            and not self.ssh_config
            and not self._configure_jetson()
        ):
            self.append_log("WARNING", "Jetson camera operation cancelled: SSH is not configured.")
            return

        payload = self._action_payload()
        signals = {
            "discover": self.discovery_requested,
            "connect": self.connection_requested,
            "disconnect": self.disconnection_requested,
            "start_stream": self.stream_start_requested,
            "stop_stream": self.stream_stop_requested,
        }
        signals[action].emit(payload)

        if action == "discover":
            self._set_state(CameraConnectionState.DISCOVERING)
        elif action == "connect":
            self._set_state(CameraConnectionState.CONNECTING)
            self.append_log(
                "INFO", f"Connecting camera SN={payload.get('device_id') or '-'}"
            )

        self.append_log("INFO", f"{action.replace('_', ' ').title()} requested.")
        if payload["execution_host"] == "Jetson" and self.ssh_config:
            self.append_log(
                "INFO",
                f"Executing on Jetson {self.ssh_config.host} through SSH.",
            )
        self._set_actions_enabled(False)
        if action == "discover":
            self.camera_worker = CameraDiscoveryWorker(self.camera_service, payload, self)
            self.camera_worker.succeeded.connect(self._on_discovery_succeeded)
            self.camera_worker.failed.connect(
                lambda error: self._on_action_failed("discover", error)
            )
        elif action in ("connect", "disconnect"):
            self.camera_worker = CameraConnectionWorker(
                self.camera_service, action, payload, self
            )
            self.camera_worker.succeeded.connect(self._on_action_succeeded)
            self.camera_worker.failed.connect(self._on_action_failed)
        else:
            self.camera_worker = CameraActionWorker(self.camera_service, action, payload, self)
            self.camera_worker.succeeded.connect(self._on_action_succeeded)
            self.camera_worker.failed.connect(self._on_action_failed)
        self.camera_worker.finished.connect(lambda: self._set_actions_enabled(True))
        self.camera_worker.start()

    def _on_discovery_succeeded(self, result):
        devices = result.get("devices", [])
        raw_devices = result.get("raw_devices", devices)
        if result.get("execution_host"):
            self.append_log(
                "INFO", f"Remote ZED devices detected: {len(raw_devices)}"
            )
            for device in raw_devices:
                self.append_log(
                    "DEBUG",
                    "Raw device: "
                    f"model={device.get('raw_model', device.get('model', '-'))}, "
                    f"serial={device.get('serial_number', '-')}, "
                    f"id={device.get('camera_id', '-')}, "
                    f"state={device.get('state', '-')}, "
                    f"type={device.get('interface', '-')}, "
                    f"path={device.get('device_path', '-')}, "
                    f"api={device.get('api', '-')}",
                )
            if result.get("discovery_note"):
                self.append_log("WARNING", result["discovery_note"])
        self.device_combo.clear()
        for device in devices:
            serial = device.get("serial_number", "-")
            model = device.get("model", "ZED Camera")
            self.device_combo.addItem(f"{model} — SN {serial}", serial)
        if devices:
            detected_family = devices[0].get("model_family")
            if detected_family and detected_family != "unknown_zed":
                try:
                    self._load_stream_capabilities(detected_family)
                except KeyError:
                    self.append_log(
                        "WARNING",
                        f"No GUI capability profile for detected family {detected_family}; "
                        "using selected profile capabilities.",
                    )
            self._set_overview(devices[0])
            self.device_chip.set_state("ok", f"{len(devices)} Device(s) Found")
            self.connection_status_label.setText("DISCOVERED")
            sdk_version = result.get("sdk_version", "Unknown")
            self.append_log(
                "INFO",
                f"Discovered {len(devices)} ZED camera(s) on "
                f"{result.get('execution_host', self.execution_host_combo.currentText())}; "
                f"SDK {sdk_version} available.",
            )
            self.append_log(
                "INFO", f"Camera detected: {self.model_combo.currentText()}"
            )
            self.append_log(
                "INFO", f"Serial Number: {devices[0].get('serial_number', '-')}"
            )
        self._set_state(CameraConnectionState.DISCONNECTED)
        if devices:
            self.device_chip.set_state("ok", f"{len(devices)} Device(s) Found")
            self.connection_status_label.setText("DISCOVERED")

    def _on_action_succeeded(self, action, result):
        if action == "connect":
            device = result.get("device", {})
            if device:
                self._set_overview(
                    {
                        **device,
                        "Model": self.model_combo.currentText(),
                        "SDK Model": device.get("raw_model", device.get("model", "-")),
                        "Execution Host": self.execution_host_combo.currentText(),
                        "Resolution": result.get("resolution", self.resolution_combo.currentText()),
                        "FPS": result.get("fps", self.fps_combo.currentText()),
                        "Camera State": "CONNECTED",
                    }
                )
            self._set_state(CameraConnectionState.CONNECTED)
            self.append_log(
                "INFO",
                f"Connected to {device.get('model', 'ZED camera')} "
                f"(SN: {device.get('serial_number', '-')}).",
            )
            if result.get("access_validated"):
                self.append_log(
                    "INFO",
                    "Remote camera access validated; the probe handle was closed safely. "
                    "Streaming is not active.",
                )
            self.append_log("INFO", "Camera opened successfully")
        elif action == "disconnect":
            self._set_state(CameraConnectionState.DISCONNECTED)
            self.append_log("INFO", "ZED camera closed safely.")
        elif action == "start_stream":
            self._set_state(CameraConnectionState.STREAMING)
        elif action == "stop_stream":
            self._set_state(CameraConnectionState.CONNECTED)
        if action not in ("connect", "disconnect"):
            self.append_log("INFO", f"{action.replace('_', ' ').title()} completed.")

    def _on_action_failed(self, action, error):
        if action == "disconnect" and self.camera_service.is_connected("zed"):
            self._set_state(CameraConnectionState.CONNECTED)
        else:
            self._set_state(CameraConnectionState.DISCONNECTED)
        self.append_log("ERROR", f"{action.replace('_', ' ').title()} failed: {error}")
        if action == "connect":
            self.append_log("ERROR", f"Failed to open {self.model_combo.currentText()}")
            self.append_log(
                "ERROR", f"Configuration: {self.resolution_combo.currentText()} "
                f"@ {self.fps_combo.currentText()} FPS",
            )
            self.append_log("ERROR", f"SDK error: {error}")

    def _set_state(self, state):
        self.connection_state = state
        label = state.value.replace("_", " ")
        self.connection_status_label.setText(label)
        if state == CameraConnectionState.CONNECTED:
            self.connection_chip.set_state("ok", "Connected")
            self.device_chip.set_state("ok", "Device Ready")
            self.stream_chip.set_state("idle", "Stream Idle")
        elif state == CameraConnectionState.STREAMING:
            self.connection_chip.set_state("ok", "Connected")
            self.device_chip.set_state("ok", "Device Ready")
            self.stream_chip.set_state("ok", "Streaming")
        elif state in (CameraConnectionState.DISCOVERING, CameraConnectionState.CONNECTING):
            self.connection_chip.set_state("warning", label.title())
            self.device_chip.set_state("idle", "Device Not Ready")
            self.stream_chip.set_state("idle", "Stream Idle")
        else:
            self.connection_chip.set_state("idle", "Disconnected")
            self.device_chip.set_state("idle", "Device Not Ready")
            self.stream_chip.set_state("idle", "Stream Idle")
        self._update_button_states()

    def _update_button_states(self):
        connected = self.connection_state in (
            CameraConnectionState.CONNECTED,
            CameraConnectionState.STREAMING,
        )
        streaming = self.connection_state == CameraConnectionState.STREAMING
        self.disconnect_button.setEnabled(connected)
        self.model_combo.setEnabled(not connected)
        self.execution_host_combo.setEnabled(not connected)
        self.device_combo.setEnabled(not connected)
        self.resolution_combo.setEnabled(not connected)
        self.fps_combo.setEnabled(not connected)
        self.start_stream_button.setEnabled(connected and not streaming)
        self.stop_stream_button.setEnabled(streaming)

    def _set_actions_enabled(self, enabled):
        self.discover_button.setEnabled(enabled)
        self.connect_button.setEnabled(enabled)
        if enabled:
            self._update_button_states()
        else:
            self.disconnect_button.setEnabled(False)
            self.start_stream_button.setEnabled(False)
            self.stop_stream_button.setEnabled(False)

    def _set_overview(self, values):
        normalized = {
            "Model": values.get("Model", values.get("model", "-")),
            "SDK Model": values.get(
                "SDK Model", values.get("raw_model", values.get("model", "-"))
            ),
            "Serial Number": values.get(
                "Serial Number", values.get("serial_number", "-")
            ),
            "Firmware": values.get("Firmware", values.get("firmware", "-")),
            "Interface": values.get("Interface", values.get("interface", "-")),
            "Device Path / Port": values.get(
                "Device Path / Port", values.get("device_path", "-")
            ),
            "SDK / Driver": values.get(
                "SDK / Driver", values.get("sdk_driver", "-")
            ),
            "Execution Host": values.get("Execution Host", "-"),
            "Resolution": values.get("Resolution", "-"),
            "FPS": values.get("FPS", "-"),
            "Camera State": values.get("Camera State", values.get("state", "-")),
        }
        for row in range(self.overview_table.rowCount()):
            name = self.overview_table.item(row, 0).text()
            self.overview_table.item(row, 1).setText(str(normalized[name]))

    def _load_test_cases(self):
        self.test_table.blockSignals(True)
        self.test_table.setRowCount(len(self.TEST_CASES))
        for row, values in enumerate(self.TEST_CASES):
            select_item = QTableWidgetItem()
            select_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable)
            select_item.setCheckState(Qt.CheckState.Unchecked)
            self.test_table.setItem(row, 0, select_item)
            for column, value in enumerate(values, start=1):
                self.test_table.setItem(row, column, QTableWidgetItem(value))
            self.test_table.setItem(row, 5, QTableWidgetItem("NOT RUN"))
        self.test_table.blockSignals(False)
        self._update_selected_test_count()

    def _update_selected_test_count(self, *_):
        selected = sum(
            self.test_table.item(row, 0).checkState() == Qt.CheckState.Checked
            for row in range(self.test_table.rowCount())
        )
        total = self.test_table.rowCount()
        self.selected_tests_label.setText(f"{selected} / {total} Selected")
        self.run_tests_button.setEnabled(selected > 0)

    def _run_selected_tests(self):
        selected = [
            self.test_table.item(row, 1).text()
            for row in range(self.test_table.rowCount())
            if self.test_table.item(row, 0).checkState() == Qt.CheckState.Checked
        ]
        if not selected:
            return
        self.tests_requested.emit(selected)
        self.append_log("INFO", "Run selected tests requested: " + ", ".join(selected))
        self.append_log("WARNING", "Camera test execution is prepared but not implemented in Phase 1.")

    def append_log(self, level, message):
        timestamp = datetime.now(timezone.utc).strftime("%H:%M:%S.%f")[:-3]
        level = level.upper()
        colors = {
            "INFO": "#3FB950",
            "WARNING": "#D29922",
            "ERROR": "#F85149",
            "DEBUG": "#58A6FF",
        }
        line = (
            f'<span style="color:#8B949E">{timestamp}</span>&nbsp;&nbsp;'
            f'<span style="color:{colors.get(level, "#E6EDF3")}; font-weight:700">'
            f'{html.escape(level):7s}</span>&nbsp;&nbsp;'
            f'<span style="color:#E6EDF3">{html.escape(message)}</span>'
        )
        self.live_log.append(line)
        if self.auto_scroll_check.isChecked():
            bar = self.live_log.verticalScrollBar()
            bar.setValue(bar.maximum())

    def live_log_clear(self):
        self.live_log.clear()

    def export_log(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Export Camera Log", "camera_live.log", "Log files (*.log);;Text files (*.txt);;All files (*)"
        )
        if not path:
            return
        with open(path, "w", encoding="utf-8") as file:
            file.write(self.live_log.toPlainText())
        self.append_log("INFO", f"Log exported to {path}")

    def _reset_runtime_ui(self):
        self._set_state(CameraConnectionState.DISCONNECTED)
        self.run_tests_button.setEnabled(False)
        self.append_log("INFO", "Camera workspace ready. Select a profile to begin.")
