import html
from datetime import datetime, timezone

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from desktop_app.ui.dialogs import (
    LidarDeviceInformationDialog,
    TestDetailsDialog,
)
from desktop_app.services.lidar_discovery_service import (
    LidarDiscoveryService,
)
from desktop_app.services.jetson_connection_service import (
    JetsonConnectionService,
)
from desktop_app.services.lidar_stream_service import LidarStreamService
from desktop_app.state.device_registry import DeviceRegistry
from desktop_app.state.jetson_state import JetsonState
from desktop_app.state.lidar_runtime_state import (
    LidarRuntimeState,
    LidarStreamStatus,
)
from desktop_app.testing.lidar_tests import build_lidar_test_registry
from desktop_app.testing.test_context import TestContext
from desktop_app.testing.test_execution_service import TestExecutionService
from desktop_app.testing.test_result import TestResult
from desktop_app.ui.widgets import Card, StatusChip
from devices.livox.profile import (
    LivoxNetworkProfile,
    load_default_livox_profile,
)


class LidarPage(QWidget):
    def __init__(
        self,
        jetson_state: JetsonState,
        jetson_service: JetsonConnectionService,
        device_registry: DeviceRegistry,
        runtime_state: LidarRuntimeState,
        stream_service: LidarStreamService,
        discovery_service: LidarDiscoveryService | None = None,
        test_registry=None,
        test_execution_service: TestExecutionService | None = None,
        parent=None,
        network_profile: LivoxNetworkProfile | None = None,
    ):
        super().__init__(parent)

        self.jetson_state = jetson_state
        self.jetson_service = jetson_service
        self.device_registry = device_registry
        self.runtime_state = runtime_state
        self.stream_service = stream_service
        self.network_profile = (
            network_profile or load_default_livox_profile()
        )
        self.discovery_service = discovery_service or LidarDiscoveryService(
            jetson_service,
            self.network_profile,
            self,
        )
        self.test_registry = test_registry or build_lidar_test_registry(
            self.network_profile
        )
        self.test_execution_service = (
            test_execution_service
            or TestExecutionService(self.test_registry, parent=self)
        )
        self.livox_device_info = None
        self.network_verification = None
        self.ping_result = None

        self.device_overview_data = {
            "Vendor": "Livox",
            "Selected Model": "-",
            "Detected Model": "-",
            "Serial": "-",
            "LiDAR IP": str(self.network_profile.lidar.ip),
            "Jetson Host IP": str(self.network_profile.jetson.ip),
            "SDK Version": "-",
            "Status": "NOT DISCOVERED",
        }
        self.network_summary_data = {}
        self.protocol_data = self._profile_protocol_data()
        self.test_case_catalog = self.test_registry.get_tests("lidar")
        self.test_results = {
            test_case.id: TestResult(test_case.id)
            for test_case in self.test_case_catalog
        }

        self.discovery_running = False
        self._last_jetson_connected = self.jetson_service.is_connected

        self._build_ui()
        self._load_test_cases()
        self._reset_runtime_ui()
        self.jetson_state.state_changed.connect(
            self._on_jetson_state_changed
        )
        self.runtime_state.changed.connect(self._on_runtime_state_changed)
        self.stream_service.log.connect(self.append_log)
        self.discovery_service.started.connect(self._on_discovery_started)
        self.discovery_service.completed.connect(self._on_livox_success)
        self.discovery_service.failed.connect(self._on_livox_failed)
        self.discovery_service.progress.connect(self.append_log)
        self.discovery_service.preflight_updated.connect(
            self._on_livox_preflight_updated
        )
        self.discovery_service.stage_changed.connect(
            self._on_livox_stage_changed
        )
        self.discovery_service.finished.connect(self._on_livox_finished)
        self.test_execution_service.log.connect(self.append_log)
        self.test_execution_service.case_status_changed.connect(
            self._on_test_status_changed
        )
        self.test_execution_service.result_ready.connect(
            self._on_test_result_ready
        )
        self.test_execution_service.running_changed.connect(
            self._on_test_running_changed
        )
        self.test_execution_service.plan_finished.connect(
            self._on_test_plan_finished
        )
        self._on_jetson_state_changed(self.jetson_state)
        self._on_runtime_state_changed(self.runtime_state.snapshot())

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.page_scroll = QScrollArea()
        self.page_scroll.setWidgetResizable(True)
        self.page_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.page_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.page_content = QWidget()
        root = QVBoxLayout(self.page_content)
        root.setContentsMargins(18, 14, 18, 12)
        root.setSpacing(10)

        header = QHBoxLayout()

        title_block = QVBoxLayout()
        self.page_title = QLabel("LiDAR Tests")
        self.page_title.setObjectName("PageTitle")
        breadcrumb = QLabel("Devices  /  LiDAR")
        breadcrumb.setObjectName("Muted")

        title_block.addWidget(self.page_title)
        title_block.addWidget(breadcrumb)

        header.addLayout(title_block)
        header.addStretch()

        self.lidar_chip = StatusChip("Device Not Ready", "idle")
        self.stream_chip = StatusChip("Stream Idle", "idle")

        for chip in (
            self.lidar_chip,
            self.stream_chip,
        ):
            header.addWidget(chip)

        root.addLayout(header)

        self.control_card = self._build_control_card()
        root.addWidget(self.control_card)

        self.monitor_card = self._build_monitor_card()
        root.addWidget(self.monitor_card)

        self.test_card = self._build_test_card()
        root.addWidget(self.test_card, 1)

        self.log_card = self._build_log_card()
        self.log_card.setMaximumHeight(210)
        root.addWidget(self.log_card)

        self.page_scroll.setWidget(self.page_content)
        outer.addWidget(self.page_scroll)

    def _build_control_card(self):
        card = Card()

        top = QHBoxLayout()

        self.current_device_label = QLabel("Livox LiDAR")
        self.current_device_label.setStyleSheet(
            "font-size:17px; font-weight:700;"
        )

        self.current_serial_label = QLabel("SN: -")
        self.current_serial_label.setObjectName("Muted")

        self.current_status_label = QLabel("NOT READY")
        self.current_status_label.setStyleSheet(
            "color:#667085; font-weight:700;"
        )

        top.addWidget(self.current_device_label)
        top.addWidget(self.current_serial_label)
        top.addStretch()
        top.addWidget(self.current_status_label)

        card.body_layout.addLayout(top)

        model_row = QHBoxLayout()
        model_row.addWidget(QLabel("Model:"))

        self.model_combo = QComboBox()
        for model_name, model_profile in self.network_profile.models.items():
            self.model_combo.addItem(
                model_profile.display_name,
                model_name,
            )
        self.model_combo.setMaximumWidth(190)

        model_row.addWidget(self.model_combo)
        self.network_profile_chip = StatusChip(
            "Network Not Checked",
            "idle",
        )
        model_row.addWidget(self.network_profile_chip)
        expected_network = QLabel(
            f"Interface {self.network_profile.jetson.interface} · "
            f"Host {self.network_profile.jetson.cidr} · "
            f"LiDAR {self.network_profile.lidar.ip}"
        )
        expected_network.setObjectName("Muted")
        expected_network.setToolTip(
            "Fixed read-only network profile. Verification does not change "
            "the Jetson or LiDAR IP configuration."
        )
        model_row.addWidget(expected_network)
        model_row.addStretch()

        card.body_layout.addLayout(model_row)

        info_buttons = QHBoxLayout()

        self.device_information_button = QPushButton("DEVICE INFORMATION")
        self.device_information_button.setObjectName("OutlineButton")
        info_buttons.addWidget(self.device_information_button)

        info_buttons.addStretch()

        self.device_information_button.clicked.connect(
            self.show_device_information
        )

        card.body_layout.addLayout(info_buttons)

        action_buttons = QHBoxLayout()

        self.discover_button = QPushButton("⌕  AUTO DISCOVER")
        self.discover_button.setObjectName("OutlineButton")

        self.stream_button = QPushButton("▶  START STREAM")
        self.stream_button.setObjectName("PrimaryButton")

        for button in (
            self.discover_button,
            self.stream_button,
        ):
            action_buttons.addWidget(button)

        self.discover_button.clicked.connect(self.auto_discover)
        self.stream_button.clicked.connect(self.toggle_stream)

        card.body_layout.addLayout(action_buttons)

        return card

    def _build_monitor_card(self):
        card = Card()

        header = QHBoxLayout()

        title = QLabel("Live Monitor")
        title.setObjectName("CardTitle")

        self.streaming_label = QLabel("● Idle")
        self.streaming_label.setStyleSheet(
            "color:#667085; font-weight:700;"
        )

        header.addWidget(title)
        header.addStretch()
        header.addWidget(self.streaming_label)
        card.body_layout.addLayout(header)

        self.monitor_table = QTableWidget(0, 4)
        self.monitor_table.setHorizontalHeaderLabels(
            ["Parameter", "Value", "Unit", "Status"]
        )
        self.monitor_table.verticalHeader().setVisible(False)
        self.monitor_table.verticalHeader().setMinimumSectionSize(22)
        self.monitor_table.verticalHeader().setDefaultSectionSize(23)
        self.monitor_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.monitor_table.setSelectionMode(
            QAbstractItemView.SelectionMode.NoSelection
        )
        self.monitor_table.setAlternatingRowColors(True)

        table_header = self.monitor_table.horizontalHeader()
        table_header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        table_header.setSectionResizeMode(
            1,
            QHeaderView.ResizeMode.Interactive,
        )
        table_header.setSectionResizeMode(
            2,
            QHeaderView.ResizeMode.ResizeToContents,
        )
        table_header.setSectionResizeMode(
            3,
            QHeaderView.ResizeMode.ResizeToContents,
        )
        self.monitor_table.setColumnWidth(1, 132)
        self.monitor_table.setMinimumHeight(145)

        rows = [
            ("Point Packet Rate", "--", "pkt/s", "IDLE"),
            ("Point Rate", "--", "pts/s", "IDLE"),
            ("IMU Status", "IDLE", "—", "IDLE"),
            ("IMU Rate", "--", "Hz", "IDLE"),
            ("Packet Loss", "N/A", "%", "IDLE"),
        ]

        tooltips = {
            "Point Packet Rate": (
                "Measured point-data UDP packets received per second. "
                "This is not a logical cloud/frame rate."
            ),
            "Point Rate": "Measured sum of SDK dot_num values per second.",
            "IMU Rate": "Measured IMU callback packets per second.",
            "Packet Loss": (
                "Calculated from gaps in the Livox point-packet udp_cnt "
                "sequence, with frame reset/wrap handling."
            ),
        }

        self.monitor_table.setRowCount(len(rows))
        for row_index, values in enumerate(rows):
            for column_index, value in enumerate(values):
                item = QTableWidgetItem(value)
                tooltip = tooltips.get(values[0])
                if tooltip:
                    item.setToolTip(tooltip)
                self.monitor_table.setItem(
                    row_index,
                    column_index,
                    item,
                )

        card.body_layout.addWidget(self.monitor_table)
        return card

    def _build_test_card(self):
        card = Card()

        header = QHBoxLayout()

        title = QLabel("Test Case List")
        title.setObjectName("CardTitle")

        self.selected_tests_label = QLabel(
            f"0 / {len(self.test_case_catalog)} Selected"
        )
        self.selected_tests_label.setStyleSheet(
            "color:#155EEF; font-weight:700;"
        )

        header.addWidget(title)
        header.addStretch()
        header.addWidget(self.selected_tests_label)
        self.run_test_button = QPushButton("▶  RUN SELECTED")
        self.run_test_button.setObjectName("PrimaryButton")
        self.run_test_button.clicked.connect(self.run_selected_tests)
        header.addWidget(self.run_test_button)

        card.body_layout.addLayout(header)

        self.test_table = QTableWidget(0, 5)
        self.test_table.setHorizontalHeaderLabels(
            ["Select", "ID", "Group", "Test Case", "Status"]
        )
        self.test_table.verticalHeader().setVisible(False)
        self.test_table.setAlternatingRowColors(True)
        self.test_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.test_table.setMinimumHeight(150)

        table_header = self.test_table.horizontalHeader()
        table_header.setSectionResizeMode(
            0,
            QHeaderView.ResizeMode.ResizeToContents,
        )
        table_header.setSectionResizeMode(
            1,
            QHeaderView.ResizeMode.ResizeToContents,
        )
        table_header.setSectionResizeMode(
            2,
            QHeaderView.ResizeMode.ResizeToContents,
        )
        table_header.setSectionResizeMode(
            3,
            QHeaderView.ResizeMode.Stretch,
        )
        table_header.setSectionResizeMode(
            4,
            QHeaderView.ResizeMode.ResizeToContents,
        )

        self.test_table.itemChanged.connect(self._update_selected_test_count)
        self.test_table.cellDoubleClicked.connect(self._show_test_details)
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
        self.live_log.setMinimumHeight(140)

        card.body_layout.addWidget(self.live_log)
        return card

    # ------------------------------------------------------------------
    # Dialogs
    # ------------------------------------------------------------------

    def show_device_information(self):
        self.update_network_summary_data()
        LidarDeviceInformationDialog(
            self.device_overview_data,
            self.network_summary_data,
            self.protocol_data,
            self,
        ).exec()

    # ------------------------------------------------------------------
    # Network
    # ------------------------------------------------------------------

    def _require_jetson_connection(self, action: str) -> bool:
        if self.jetson_service.is_connected:
            return True

        message = (
            f"Cannot {action}: Jetson is not connected. "
            "Connect Jetson from Dashboard first."
        )
        self.append_log("WARNING", message)
        QMessageBox.warning(self, "Jetson Connection Required", message)
        return False

    def _on_jetson_state_changed(self, _state):
        connected = self.jetson_service.is_connected
        was_connected = self._last_jetson_connected
        self._last_jetson_connected = connected
        self.discover_button.setEnabled(
            connected and not self.discovery_running
        )
        if not connected:
            self.network_verification = None
            self.ping_result = None
            self.network_profile_chip.set_state(
                "idle",
                "Network Not Checked",
            )
            if was_connected:
                self.device_registry.mark_unavailable(
                    "LiDAR",
                    reason="Jetson disconnected",
                )
                if self.livox_device_info:
                    self.device_overview_data["Status"] = "OFFLINE"
                    self.current_status_label.setText("OFFLINE")
                    self.lidar_chip.set_state("idle", "Device Offline")
        elif self._network_ready():
            self.network_profile_chip.set_state(
                "ok",
                "NETWORK READY",
            )
        else:
            status = (
                self.network_verification.get("status")
                if self.network_verification
                else "Network Not Verified"
            )
            self.network_profile_chip.set_state(
                "error" if self.network_verification else "idle",
                status,
            )
        self.update_network_summary_data()
        self._sync_stream_controls()

    def _profile_interface(self) -> dict | None:
        network_snapshot = self.jetson_state.network_snapshot
        if not network_snapshot:
            return None
        for interface in network_snapshot.get("interfaces", []):
            if interface.get("name") == self.network_profile.jetson.interface:
                return interface
        return None

    def _network_ready(self) -> bool:
        return bool(
            self.network_verification
            and self.network_verification.get("ready") is True
            and self.network_verification.get("status") == "NETWORK_READY"
        )

    def _ping_ready(self) -> bool:
        return bool(
            self.ping_result
            and self.ping_result.get("reachable") is True
        )

    def _device_ready(self) -> bool:
        return bool(
            self.livox_device_info
            and self.livox_device_info.get("found") is True
            and self.livox_device_info.get("status") == "FOUND"
            and self._network_ready()
            and self._ping_ready()
        )

    def _profile_protocol_data(self) -> dict:
        profile = self.network_profile
        return {
            "LiDAR IP": str(profile.lidar.ip),
            "LiDAR IP Access": profile.lidar.ip_access.upper(),
            "Expected Serial": profile.lidar.expected_serial,
            "Jetson LiDAR Interface": profile.jetson.interface,
            "Jetson LiDAR Host IP": profile.jetson.cidr,
            "Discovery Port": str(profile.discovery_port),
            "LiDAR Control Port": str(profile.lidar_ports.command),
            "LiDAR Push Message Port": str(profile.lidar_ports.push),
            "LiDAR Point Data Port": str(profile.lidar_ports.point_cloud),
            "LiDAR IMU Data Port": str(profile.lidar_ports.imu),
            "LiDAR Log Data Port": str(profile.lidar_ports.log),
            "Host Control Port": str(profile.host_ports.command),
            "Host Push Message Port": str(profile.host_ports.push),
            "Host Point Data Port": str(profile.host_ports.point_cloud),
            "Host IMU Data Port": str(profile.host_ports.imu),
            "Host Log Data Port": str(profile.host_ports.log),
            "Gateway Address": "None / Not Required",
        }

    def update_network_summary_data(self):
        network_snapshot = self.jetson_state.network_snapshot or {}
        interface = self._profile_interface()
        addresses = interface.get("ipv4_addresses", []) if interface else []
        verification = self.network_verification or {}
        carrier = verification.get(
            "carrier",
            interface.get("carrier") if interface else None,
        )
        operstate = verification.get(
            "operstate",
            interface.get("operstate") if interface else "NOT FOUND",
        )
        flags = {
            str(flag).upper()
            for flag in verification.get(
                "flags",
                interface.get("flags", []) if interface else [],
            )
        }
        displayed_state = str(operstate or "-").upper()
        if displayed_state != "UP" and {"UP", "LOWER_UP"} <= flags:
            displayed_state = "UP / LOWER_UP"
        observed_addresses = verification.get("ipv4_addresses", addresses)
        expected_cidr = self.network_profile.jetson.cidr
        jetson_ip = (
            expected_cidr
            if expected_cidr in observed_addresses
            else ", ".join(observed_addresses) or "-"
        )
        ping = self.ping_result or {}
        if ping.get("reachable"):
            average_rtt = ping.get("average_rtt_ms")
            ping_value = (
                f"{average_rtt:.2f} ms"
                if average_rtt is not None
                else "Response received"
            )
            ping_status = "PASS"
        elif self.ping_result:
            ping_value = "No response"
            ping_status = "FAIL"
        else:
            ping_value = "Not run"
            ping_status = "NOT RUN"
        link_ready = carrier is True and (
            displayed_state == "UP" or "LOWER_UP" in displayed_state
        )
        detected_lidar_ip = (
            self.livox_device_info.get("lidar_ip")
            if self.livox_device_info
            else None
        )
        if self.ping_result and not self._ping_ready():
            overall_status = "PING FAIL"
        elif self._network_ready():
            overall_status = "NETWORK READY"
        else:
            overall_status = verification.get(
                "status",
                "NOT_VERIFIED" if self.jetson_service.is_connected else "OFFLINE",
            )

        self.network_summary_data = {
            "interface": self.network_profile.jetson.interface,
            "mac_address": verification.get(
                "mac_address",
                interface.get("mac_address") if interface else "-",
            ) or "-",
            "physical": verification.get(
                "physical",
                interface.get("physical") if interface else False,
            ),
            "state": displayed_state,
            "carrier": (
                "UP"
                if carrier is True
                else "DOWN"
                if carrier is False
                else "UNKNOWN"
            ),
            "jetson_ip": jetson_ip,
            "expected_jetson_cidr": expected_cidr,
            "expected_lidar_ip": str(self.network_profile.lidar.ip),
            "lidar_ip": detected_lidar_ip or str(self.network_profile.lidar.ip),
            "network": self.network_profile.jetson.network,
            "gateway": "None / Not Required",
            "ping": ping_value,
            "ping_status": ping_status,
            "link": "UP" if link_ready else displayed_state,
            "link_ready": link_ready,
            "verification_status": verification.get(
                "status",
                "NOT_VERIFIED" if self.jetson_service.is_connected else "OFFLINE",
            ),
            "status": overall_status,
        }

    # ------------------------------------------------------------------
    # Livox
    # ------------------------------------------------------------------

    def auto_discover(self):
        if not self._require_jetson_connection("discover the LiDAR"):
            return
        if self.test_execution_service.running:
            self.append_log("WARNING", "A test plan is currently running.")
            return
        if self.runtime_state.stream_status not in {
            LidarStreamStatus.IDLE,
            LidarStreamStatus.ERROR,
        }:
            self.append_log(
                "WARNING",
                "Stop the active LiDAR stream before discovery.",
            )
            return

        model = self.model_combo.currentData()
        if not self.discovery_service.start(model, timeout=8):
            self.append_log(
                "WARNING",
                "Auto Discover is already running or Jetson is disconnected.",
            )

    def _on_discovery_started(self, model: str):
        host_ip = str(self.network_profile.jetson.ip)
        expected_lidar_ip = str(self.network_profile.lidar.ip)

        self.discovery_running = True
        self.discover_button.setEnabled(False)
        self.model_combo.setEnabled(False)
        self.network_verification = None
        self.ping_result = None
        self.livox_device_info = None
        self.stream_button.setEnabled(False)
        self.update_network_summary_data()
        self.network_profile_chip.set_state(
            "warning",
            "VERIFYING NETWORK",
        )
        self.lidar_chip.set_state("warning", "Network Checking")
        self.current_status_label.setText("DISCOVERING")

        self.device_overview_data.update(
            {
                "Selected Model": self.model_combo.currentText(),
                "Jetson Host IP": host_ip,
                "Status": "DISCOVERING",
            }
        )
        self.device_registry.update_device(
            {
                "device": "LiDAR",
                "available": "unknown",
                "status": "running",
                "last_error": None,
            }
        )
        self.append_log("INFO", "Auto Discover started")
        self.append_log("INFO", "Verifying LiDAR network")
        self.append_log("INFO", f"Using Jetson LiDAR host IP {host_ip}")
        self.append_log("INFO", f"Expected LiDAR IP {expected_lidar_ip}")

    def _on_livox_finished(self):
        self.discovery_running = False
        self.model_combo.setEnabled(True)
        self._sync_stream_controls()

    def _on_livox_stage_changed(self, stage: str):
        state = (
            "ok"
            if stage in {"LiDAR Reachable", "Device Ready"}
            else "warning"
        )
        self.lidar_chip.set_state(state, stage)

    def _on_livox_preflight_updated(self, result: dict):
        network = result.get("network")
        if network:
            self.jetson_service.update_network_snapshot(network)
        verification = result.get("network_verification")
        if verification:
            self.network_verification = verification
            ready = self._network_ready()
            self.network_profile_chip.set_state(
                "ok" if ready else "error",
                "NETWORK READY" if ready else verification["status"],
            )
        if "ping" in result:
            self.ping_result = result.get("ping")
        self.update_network_summary_data()

    def _on_livox_success(self, result: dict):
        self._on_livox_preflight_updated(result)
        if not result.get("found"):
            self.livox_device_info = result
            status = result.get("status") or "DEVICE_NOT_FOUND"
            display_status = self._orchestration_error_label(status)
            error_status = status != "DEVICE_NOT_FOUND"
            self.lidar_chip.set_state(
                "error" if error_status else "warning",
                display_status,
            )
            self.current_status_label.setText(display_status)
            self.stream_button.setEnabled(False)
            detected_model = result.get("model")
            detected_serial = result.get("serial")
            if detected_model:
                self.current_device_label.setText(f"Livox {detected_model}")
            if detected_serial:
                self.current_serial_label.setText(f"SN: {detected_serial}")
            environment = result.get("environment") or {}
            self.device_overview_data = {
                "Vendor": "Livox",
                "Selected Model": self.model_combo.currentText(),
                "Detected Model": detected_model or "-",
                "Serial": detected_serial or "-",
                "LiDAR IP": result.get("lidar_ip")
                or str(self.network_profile.lidar.ip),
                "Jetson Host IP": str(self.network_profile.jetson.ip),
                "SDK Version": result.get("sdk_version")
                or environment.get("sdk_version")
                or "-",
                "Status": display_status,
            }
            self._publish_discovery_failure(result, status)
            network_failure = status in {
                "NETWORK_ERROR",
                "INTERFACE_NOT_FOUND",
                "LINK_DOWN",
                "JETSON_IP_MISMATCH",
                "SUBNET_MISMATCH",
            }
            if network_failure:
                self.network_profile_chip.set_state(
                    "error",
                    display_status,
                )
                self.append_log(
                    "ERROR",
                    "Auto Discover aborted: LiDAR network is not ready. "
                    f"{result.get('reason') or status}",
                )
            else:
                self.append_log(
                    "ERROR" if error_status else "WARNING",
                    f"Livox discovery status: {status}; "
                    f"reason={result.get('reason') or '-'}.",
                )
            self.update_network_summary_data()
            return

        self.livox_device_info = result

        model = result.get("model") or "UNKNOWN"
        serial = result.get("serial") or "-"
        lidar_ip = result.get("lidar_ip") or "-"
        sdk = result.get("sdk_version") or "-"
        dev_type = result.get("dev_type") or "-"
        expected_lidar_ip = str(self.network_profile.lidar.ip)
        expected_serial = self.network_profile.lidar.expected_serial
        strict_serial = self.network_profile.lidar.strict_serial_verification
        expected_model = self.model_combo.currentData()
        ip_matches = lidar_ip == expected_lidar_ip
        serial_matches = serial == expected_serial
        model_matches = model == expected_model
        identity_matches = (
            self._network_ready()
            and self._ping_ready()
            and result.get("status") == "FOUND"
            and ip_matches
            and model_matches
            and (serial_matches or not strict_serial)
        )

        self.current_device_label.setText(f"Livox {model}")
        self.current_serial_label.setText(f"SN: {serial}")
        self.current_status_label.setText(
            "DETECTED" if identity_matches else "PROFILE MISMATCH"
        )
        self.current_status_label.setStyleSheet(
            f"color:{'#16883F' if identity_matches else '#D92D20'}; "
            "font-weight:700;"
        )

        self.lidar_chip.set_state(
            "ok" if identity_matches else "error",
            "Device Ready" if identity_matches else "Profile Mismatch",
        )
        self._sync_stream_controls()

        self.device_overview_data = {
            "Vendor": "Livox",
            "Selected Model": self.model_combo.currentText(),
            "Detected Model": model,
            "Serial": serial,
            "LiDAR IP": lidar_ip,
            "Jetson Host IP": str(self.network_profile.jetson.ip),
            "SDK Version": sdk,
            "Status": "READY" if identity_matches else "PROFILE MISMATCH",
            "Model Check": "PASS" if model_matches else "FAIL",
            "Serial Check": (
                "PASS"
                if serial_matches
                else "FAIL"
                if strict_serial
                else "WARNING (advisory)"
            ),
            "LiDAR IP Check": "PASS" if ip_matches else "FAIL",
            "Device Type": dev_type,
        }

        self.protocol_data = {
            **self._profile_protocol_data(),
            "Detected LiDAR IP": lidar_ip,
        }

        self.update_network_summary_data()
        self._update_selected_test_count()

        if identity_matches:
            self.device_registry.update_device(
                {
                    "device": "LiDAR",
                    "available": "detected",
                    "model": model,
                    "serial": serial,
                    "status": "ready",
                    "last_error": None,
                }
            )
            self.append_log("INFO", f"Livox SDK2 version {sdk}")
            self.append_log("INFO", f"Serial {serial}")
            self.append_log("INFO", f"LiDAR IP {lidar_ip}")
            self.append_log("PASS", "Device ready")
        else:
            self._publish_discovery_failure(result, "MODEL_MISMATCH")

        self.append_log(
            "INFO" if identity_matches else "ERROR",
            f"LiDAR detected: Livox {model} "
            f"(SN: {serial}) at {lidar_ip}; fixed profile "
            f"{'matched' if identity_matches else 'mismatched'}.",
        )

    def _on_livox_failed(self, error: str):
        self.livox_device_info = None
        stage = self.discovery_service.current_stage
        display_status = {
            "Network Checking": "NETWORK ERROR",
            "LiDAR Reachable": "PING FAIL",
            "SDK Checking": "SDK ERROR",
            "Discovering Device": "SDK DISCOVERY ERROR",
        }.get(stage, "DISCOVERY ERROR")
        self.lidar_chip.set_state("error", display_status)
        self.current_status_label.setText(display_status)
        self.stream_button.setEnabled(False)
        self.device_registry.mark_unavailable(
            "LiDAR",
            status="error",
            reason=error,
        )
        self.append_log("ERROR", error)
        self.append_log("ERROR", "Auto Discover aborted")

        QMessageBox.critical(
            self,
            "Livox Discovery Error",
            error,
        )

    def _publish_discovery_failure(self, result: dict, status: str) -> None:
        update = {
            "device": "LiDAR",
            "available": "not detected",
            "status": "warning" if status == "DEVICE_NOT_FOUND" else "error",
            "last_error": result.get("reason") or status,
        }
        model = result.get("model")
        serial = result.get("serial")
        if model:
            update["model"] = model
        if serial:
            update["serial"] = serial
        self.device_registry.update_device(update)

    @staticmethod
    def _orchestration_error_label(status: str) -> str:
        if status in {
            "INTERFACE_NOT_FOUND",
            "LINK_DOWN",
            "JETSON_IP_MISMATCH",
            "SUBNET_MISMATCH",
        }:
            return "NETWORK ERROR"
        return {
            "PING_FAILED": "PING FAIL",
            "SDK_HELPER_MISSING": "SDK HELPER MISSING",
            "SDK_VERSION_ERROR": "SDK VERSION ERROR",
            "SDK_INIT_ERROR": "SDK INIT ERROR",
            "SDK_DISCOVERY_ERROR": "SDK DISCOVERY ERROR",
            "DEVICE_NOT_FOUND": "DEVICE NOT FOUND",
            "IP_MISMATCH": "IP MISMATCH",
            "MODEL_MISMATCH": "MODEL MISMATCH",
            "SERIAL_MISMATCH": "SERIAL MISMATCH",
        }.get(status, status.replace("_", " "))

    # ------------------------------------------------------------------
    # Monitor
    # ------------------------------------------------------------------

    def _set_monitor_row(
        self,
        row_name: str,
        value: str,
        unit: str = "",
        status: str = "OK",
        tooltip: str | None = None,
    ):
        for row in range(self.monitor_table.rowCount()):
            item = self.monitor_table.item(row, 0)
            if item and item.text() == row_name:
                for column, text in enumerate(
                    (str(value), str(unit), str(status)),
                    start=1,
                ):
                    value_item = QTableWidgetItem(text)
                    if tooltip:
                        value_item.setToolTip(tooltip)
                    self.monitor_table.setItem(row, column, value_item)
                return

    def update_monitor(
        self,
        cloud_rate_hz: float | None = None,
        point_count: int | None = None,
        imu_ok: bool | None = None,
        packet_loss_percent: float | None = None,
        sensor_timestamp: str | None = None,
        power_w: float | None = None,
        temperature_c: float | None = None,
        uptime: str | None = None,
    ):
        if cloud_rate_hz is not None:
            self._set_monitor_row(
                "Point Packet Rate",
                f"{cloud_rate_hz:.2f}",
                "pkt/s",
                "OK",
            )

        if point_count is not None:
            self._set_monitor_row(
                "Point Rate",
                f"{point_count:,}",
                "pts/s",
                "OK",
            )

        if imu_ok is not None:
            self._set_monitor_row(
                "IMU Status",
                "OK" if imu_ok else "ERROR",
                "",
                "OK" if imu_ok else "ERROR",
            )

        if packet_loss_percent is not None:
            self._set_monitor_row(
                "Packet Loss",
                f"{packet_loss_percent:.2f}",
                "%",
                "OK",
            )

        if power_w is not None:
            self._set_monitor_row(
                "Power",
                f"{power_w:.1f}",
                "W",
                "OK",
            )

        if temperature_c is not None:
            self._set_monitor_row(
                "Temperature",
                f"{temperature_c:.1f}",
                "°C",
                "OK",
            )

        self.streaming_label.setText("● Live")
        self.streaming_label.setStyleSheet(
            "color:#16883F; font-weight:700;"
        )
        self.stream_chip.set_state("ok", "Streaming")

    def _on_runtime_state_changed(self, runtime: dict):
        status = runtime["stream_status"]
        status_display = status.title()
        if status == "IDLE":
            self.streaming_label.setText("● Idle")
            self.streaming_label.setStyleSheet(
                "color:#667085; font-weight:700;"
            )
            self.stream_chip.set_state("idle", "Stream Idle")
        elif status == "STARTING":
            self.streaming_label.setText("● Starting")
            self.streaming_label.setStyleSheet(
                "color:#D29922; font-weight:700;"
            )
            self.stream_chip.set_state("warning", "Stream Starting")
        elif status == "STREAMING":
            self.streaming_label.setText("● Live")
            self.streaming_label.setStyleSheet(
                "color:#16883F; font-weight:700;"
            )
            self.stream_chip.set_state("ok", "Streaming")
        elif status == "STALE":
            self.streaming_label.setText("● Stale")
            self.streaming_label.setStyleSheet(
                "color:#D29922; font-weight:700;"
            )
            self.stream_chip.set_state("warning", "Stream Stale")
        elif status == "STOPPING":
            self.streaming_label.setText("● Stopping")
            self.streaming_label.setStyleSheet(
                "color:#667085; font-weight:700;"
            )
            self.stream_chip.set_state("idle", "Stream Stopping")
        else:
            self.streaming_label.setText("● Error")
            self.streaming_label.setStyleSheet(
                "color:#D92D20; font-weight:700;"
            )
            self.stream_chip.set_state("error", "Stream Error")

        metric_status = {
            "STREAMING": "LIVE",
            "STALE": "STALE",
            "ERROR": "ERROR",
        }.get(status, status_display.upper())
        point_packet_rate = runtime.get("point_packet_rate_hz")
        if point_packet_rate is not None:
            self._set_monitor_row(
                "Point Packet Rate",
                f"{point_packet_rate:.2f}",
                "pkt/s",
                metric_status,
            )
        if runtime.get("point_count") is not None:
            self._set_monitor_row(
                "Point Rate",
                f"{runtime['point_count']:,}",
                runtime.get("point_count_unit") or "pts/s",
                metric_status,
            )

        imu_status = runtime.get("imu_status") or "IDLE"
        self._set_monitor_row(
            "IMU Status",
            imu_status,
            "—",
            "LIVE" if imu_status == "ACTIVE" else imu_status,
        )
        imu_rate = runtime.get("imu_rate_hz")
        if imu_rate is not None:
            imu_metric_status = (
                "LIVE"
                if status == "STREAMING" and imu_status == "ACTIVE"
                else "STALE"
                if imu_status == "STALE"
                else metric_status
            )
            self._set_monitor_row(
                "IMU Rate",
                f"{imu_rate:.2f}",
                "Hz",
                imu_metric_status,
            )
        if runtime.get("packet_loss_supported"):
            packet_loss = runtime.get("packet_loss_percent")
            value = f"{packet_loss:.3f}" if packet_loss is not None else "N/A"
            self._set_monitor_row(
                "Packet Loss",
                value,
                "%",
                metric_status,
            )
        else:
            self._set_monitor_row(
                "Packet Loss",
                "N/A",
                "%",
                "UNSUPPORTED" if status != "IDLE" else "IDLE",
            )
        self._sync_stream_controls()

    def _sync_stream_controls(self):
        status = self.runtime_state.stream_status
        tests_running = self.test_execution_service.running
        active = status in {
            LidarStreamStatus.STARTING,
            LidarStreamStatus.STREAMING,
            LidarStreamStatus.STALE,
            LidarStreamStatus.STOPPING,
        }
        can_start = (
            self.jetson_service.is_connected
            and self._device_ready()
            and not tests_running
            and status in {LidarStreamStatus.IDLE, LidarStreamStatus.ERROR}
        )
        if status in {LidarStreamStatus.IDLE, LidarStreamStatus.ERROR}:
            self.stream_button.setText("▶  START STREAM")
            self.stream_button.setEnabled(can_start)
        elif status is LidarStreamStatus.STARTING:
            self.stream_button.setText("…  STARTING")
            self.stream_button.setEnabled(False)
        elif status in {
            LidarStreamStatus.STREAMING,
            LidarStreamStatus.STALE,
        }:
            self.stream_button.setText("■  STOP STREAM")
            self.stream_button.setEnabled(True)
        else:
            self.stream_button.setText("…  STOPPING")
            self.stream_button.setEnabled(False)
        if tests_running:
            self.stream_button.setEnabled(False)
        self.discover_button.setEnabled(
            self.jetson_service.is_connected
            and not self.discovery_running
            and not active
            and not tests_running
        )
        if active or tests_running:
            self.model_combo.setEnabled(False)
        elif not self.discovery_running:
            self.model_combo.setEnabled(True)

    # ------------------------------------------------------------------
    # Test UI
    # ------------------------------------------------------------------

    def _load_test_cases(self):
        self.test_table.blockSignals(True)
        self.test_table.setRowCount(len(self.test_case_catalog))

        for row, test_case in enumerate(self.test_case_catalog):
            test_id = test_case.id
            select_item = QTableWidgetItem()
            select_item.setFlags(
                Qt.ItemFlag.ItemIsEnabled
                | Qt.ItemFlag.ItemIsUserCheckable
            )
            select_item.setCheckState(Qt.CheckState.Unchecked)

            self.test_table.setItem(row, 0, select_item)
            self.test_table.setItem(row, 1, QTableWidgetItem(test_id))
            self.test_table.setItem(
                row,
                2,
                QTableWidgetItem(test_case.group),
            )
            name_item = QTableWidgetItem(test_case.name)
            name_item.setToolTip(
                f"{test_case.description}\n"
                f"Type: {test_case.automation_level.value} · "
                f"Priority: {test_case.priority} · "
                f"Timeout: {test_case.timeout_sec:.1f} s\n"
                "Prerequisites: "
                + (", ".join(test_case.prerequisites) or "None")
            )
            self.test_table.setItem(row, 3, name_item)
            status = self.test_results[test_id].status.value.replace("_", " ")
            self.test_table.setItem(row, 4, QTableWidgetItem(status))

        self.test_table.blockSignals(False)
        self._update_selected_test_count()

    def _update_selected_test_count(self, *_):
        selected = 0
        total = self.test_table.rowCount()

        for row in range(total):
            item = self.test_table.item(row, 0)
            if item and item.checkState() == Qt.CheckState.Checked:
                selected += 1

        self.selected_tests_label.setText(
            f"{selected} / {total} Selected"
        )
        if self.test_execution_service.running:
            self.run_test_button.setText("■  STOP TEST")
            self.run_test_button.setEnabled(True)
        else:
            self.run_test_button.setText("▶  RUN SELECTED")
            self.run_test_button.setEnabled(selected > 0)

    def run_selected_tests(self):
        if self.test_execution_service.running:
            self.test_execution_service.cancel()
            return
        selected = []

        for row in range(self.test_table.rowCount()):
            item = self.test_table.item(row, 0)
            if item and item.checkState() == Qt.CheckState.Checked:
                selected.append(
                    self.test_table.item(row, 1).text()
                )

        if not selected:
            return

        context = TestContext(
            device="LiDAR",
            jetson_state=self.jetson_state,
            jetson_service=self.jetson_service,
            device_registry=self.device_registry,
            lidar_runtime_state=self.runtime_state,
            lidar_stream_service=self.stream_service,
            lidar_discovery_service=self.discovery_service,
            network_profile=self.network_profile,
            selected_model=self.model_combo.currentData(),
            discovery_result=(
                dict(self.livox_device_info)
                if self.livox_device_info
                else None
            ),
            network_verification=(
                dict(self.network_verification)
                if self.network_verification
                else None
            ),
            ping_result=dict(self.ping_result) if self.ping_result else None,
        )
        self.test_execution_service.start(selected, context)

    def _on_test_status_changed(self, test_id: str, status: str):
        row = self._test_row(test_id)
        if row is not None:
            self.test_table.setItem(
                row,
                4,
                QTableWidgetItem(status.replace("_", " ")),
            )

    def _on_test_result_ready(self, result: TestResult):
        self.test_results[result.test_id] = result
        row = self._test_row(result.test_id)
        if row is not None:
            status_item = self.test_table.item(row, 4)
            if status_item is not None:
                status_item.setToolTip(
                    f"Duration: {result.duration_sec or 0.0:.3f} s\n"
                    f"Actual: {result.actual_result}\n"
                    f"Evidence: {', '.join(result.evidence) or '-'}"
                )

    def _on_test_running_changed(self, _running: bool):
        self._update_selected_test_count()
        self._sync_stream_controls()

    def _on_test_plan_finished(self, summary: dict):
        self.append_log(
            "INFO",
            f"Evidence session: {summary.get('session_id')}",
        )
        self._update_selected_test_count()

    def _test_row(self, test_id: str) -> int | None:
        for row in range(self.test_table.rowCount()):
            item = self.test_table.item(row, 1)
            if item and item.text() == test_id:
                return row
        return None

    def _show_test_details(self, row: int, _column: int):
        test_id_item = self.test_table.item(row, 1)
        if test_id_item is None:
            return
        test_id = test_id_item.text()
        TestDetailsDialog(
            self.test_registry.get(test_id),
            self.test_results.get(test_id),
            self,
        ).exec()

    # ------------------------------------------------------------------
    # Stream runtime
    # ------------------------------------------------------------------

    def toggle_stream(self):
        status = self.runtime_state.stream_status
        if status in {
            LidarStreamStatus.STREAMING,
            LidarStreamStatus.STALE,
        }:
            self.stop_stream()
        elif status in {
            LidarStreamStatus.IDLE,
            LidarStreamStatus.ERROR,
        }:
            self.start_stream()

    def start_stream(self):
        if self.test_execution_service.running:
            self.append_log("WARNING", "A test plan is currently running.")
            return
        if not self._require_jetson_connection("start the LiDAR stream"):
            return
        if self.stream_service.active:
            self.append_log("WARNING", "LiDAR stream is already active")
            return
        if not self._device_ready():
            self.append_log(
                "ERROR",
                "Cannot start stream: LiDAR discovery and network must be ready.",
            )
            return

        selected_model = self.model_combo.currentData()
        detected_model = self.livox_device_info.get("model")
        if selected_model != detected_model:
            self.append_log(
                "ERROR",
                f"Cannot start stream: selected model {selected_model} "
                f"does not match detected model {detected_model}.",
            )
            return
        expected_host = str(self.network_profile.jetson.ip)
        detected_host = self.livox_device_info.get("host_ip") or expected_host
        if detected_host != expected_host:
            self.append_log(
                "ERROR",
                f"Cannot start stream: host IP {detected_host} does not "
                f"match fixed profile {expected_host}.",
            )
            return
        self.stream_service.start(selected_model)

    def stop_stream(self):
        self.stream_service.stop()

    # ------------------------------------------------------------------
    # Log
    # ------------------------------------------------------------------

    def append_log(self, level: str, message: str):
        timestamp = datetime.now(timezone.utc).strftime(
            "%H:%M:%S.%f"
        )[:-3]

        level = level.upper()

        colors = {
            "INFO": "#3FB950",
            "WARNING": "#D29922",
            "ERROR": "#F85149",
            "DEBUG": "#58A6FF",
            "PASS": "#3FB950",
            "FAIL": "#F85149",
        }

        color = colors.get(level, "#E6EDF3")

        line = (
            f'<span style="color:#8B949E">{timestamp}</span>&nbsp;&nbsp;'
            f'<span style="color:{color}; font-weight:700">'
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
            self,
            "Export Live Log",
            "lidar_live.log",
            "Log files (*.log);;Text files (*.txt);;All files (*)",
        )

        if not path:
            return

        with open(path, "w", encoding="utf-8") as file:
            file.write(self.live_log.toPlainText())

        self.append_log("INFO", f"Log exported to {path}")

    # ------------------------------------------------------------------
    # Initial state
    # ------------------------------------------------------------------

    def _reset_runtime_ui(self):
        self.lidar_chip.set_state("idle", "Device Not Ready")
        self.stream_chip.set_state("idle", "Stream Idle")

        self.discover_button.setEnabled(False)
        self.stream_button.setEnabled(False)
        self.run_test_button.setEnabled(False)

        self.append_log(
            "INFO",
            "LiDAR workspace ready with fixed read-only network profile. "
            "Connect to Jetson to validate it.",
        )
