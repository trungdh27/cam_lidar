import html
from datetime import datetime, timezone

from PySide6.QtCore import Qt
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
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from desktop_app.ui.dialogs import (
    DeviceOverviewDialog,
    NetworkProtocolDialog,
    NetworkSummaryDialog,
    TemporaryIPDialog,
)
from desktop_app.services.jetson_connection_service import (
    JetsonConnectionService,
)
from desktop_app.state.jetson_state import JetsonState
from desktop_app.ui.widgets import Card, StatusChip
from desktop_app.workers.livox_discovery_worker import LivoxDiscoveryWorker
from desktop_app.workers.temporary_ip_worker import TemporaryIPWorker


class LidarPage(QWidget):
    def __init__(
        self,
        jetson_state: JetsonState,
        jetson_service: JetsonConnectionService,
        parent=None,
    ):
        super().__init__(parent)

        self.jetson_state = jetson_state
        self.jetson_service = jetson_service
        self.temporary_ip_state = None
        self.livox_device_info = None

        self.device_overview_data = {}
        self.network_summary_data = {}
        self.protocol_data = {}

        self.temp_ip_worker = None
        self.livox_worker = None
        self._temporary_sudo_password = None

        self._build_ui()
        self._load_test_cases()
        self._reset_runtime_ui()
        self.jetson_state.state_changed.connect(
            self._on_jetson_state_changed
        )
        self._on_jetson_state_changed(self.jetson_state)

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 14, 18, 14)
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

        middle = QHBoxLayout()
        middle.setSpacing(10)

        self.suite_card = self._build_suite_card()
        self.monitor_card = self._build_monitor_card()

        middle.addWidget(self.suite_card, 1)
        middle.addWidget(self.monitor_card, 1)
        root.addLayout(middle)

        self.test_card = self._build_test_card()
        root.addWidget(self.test_card)

        self.log_card = self._build_log_card()
        root.addWidget(self.log_card, 1)

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
        self.model_combo.addItem("MID-360", "MID360")
        self.model_combo.addItem("MID-360S", "MID360S")
        self.model_combo.setMaximumWidth(190)

        model_row.addWidget(self.model_combo)
        model_row.addStretch()

        card.body_layout.addLayout(model_row)

        info_buttons = QHBoxLayout()

        self.device_overview_button = QPushButton("Device Overview")
        self.network_summary_button = QPushButton("Network Summary")
        self.protocol_button = QPushButton("Network Protocol")

        for button in (
            self.device_overview_button,
            self.network_summary_button,
            self.protocol_button,
        ):
            button.setObjectName("OutlineButton")
            info_buttons.addWidget(button)

        info_buttons.addStretch()

        self.device_overview_button.clicked.connect(self.show_device_overview)
        self.network_summary_button.clicked.connect(self.show_network_summary)
        self.protocol_button.clicked.connect(self.show_network_protocol)

        card.body_layout.addLayout(info_buttons)

        action_buttons = QHBoxLayout()

        self.discover_button = QPushButton("⌕  AUTO DISCOVER")
        self.discover_button.setObjectName("OutlineButton")

        self.configure_ip_button = QPushButton("CONFIGURE IP")
        self.configure_ip_button.setObjectName("OutlineButton")

        self.restore_ip_button = QPushButton("RESTORE IP")
        self.restore_ip_button.setObjectName("OutlineButton")

        self.start_stream_button = QPushButton("▶  START STREAM")
        self.start_stream_button.setObjectName("PrimaryButton")

        self.stop_button = QPushButton("■  STOP")
        self.stop_button.setObjectName("DangerButton")

        self.run_test_button = QPushButton("▶  RUN SELECTED")
        self.run_test_button.setObjectName("PrimaryButton")

        for button in (
            self.discover_button,
            self.configure_ip_button,
            self.restore_ip_button,
            self.start_stream_button,
            self.stop_button,
            self.run_test_button,
        ):
            action_buttons.addWidget(button)

        self.discover_button.clicked.connect(self.auto_discover)
        self.configure_ip_button.clicked.connect(self.configure_temporary_ip)
        self.restore_ip_button.clicked.connect(self.restore_temporary_ip)
        self.start_stream_button.clicked.connect(self.start_stream)
        self.stop_button.clicked.connect(self.stop_stream)
        self.run_test_button.clicked.connect(self.run_selected_tests)

        card.body_layout.addLayout(action_buttons)

        return card

    def _build_suite_card(self):
        card = Card("Test Suites")

        self.suite_table = QTableWidget(0, 6)
        self.suite_table.setHorizontalHeaderLabels(
            ["Group", "Total", "PASS", "FAIL", "Not Run", "Progress"]
        )
        self.suite_table.verticalHeader().setVisible(False)
        self.suite_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.suite_table.setSelectionMode(
            QAbstractItemView.SelectionMode.NoSelection
        )
        self.suite_table.setAlternatingRowColors(True)

        header = self.suite_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in range(1, 6):
            header.setSectionResizeMode(
                column,
                QHeaderView.ResizeMode.ResizeToContents,
            )

        groups = [
            ("Connectivity, Network & Protocol", 19),
            ("Power, Startup & Electrical", 8),
            ("Detection Range & Reflectivity", 15),
            ("Measurement Accuracy & FOV", 8),
            ("Optical / Environmental", 3),
            ("Dynamic Object & Motion", 6),
            ("Reliability & Recovery", 5),
            ("SLAM & Mapping", 9),
        ]

        self.suite_table.setRowCount(len(groups))
        for row, (name, total) in enumerate(groups):
            values = [name, str(total), "0", "0", str(total), "0%"]
            for col, value in enumerate(values):
                self.suite_table.setItem(
                    row,
                    col,
                    QTableWidgetItem(value),
                )

        card.body_layout.addWidget(self.suite_table)
        return card

    def _build_monitor_card(self):
        card = Card()

        header = QHBoxLayout()

        title = QLabel("Live Monitor")
        title.setObjectName("CardTitle")

        self.streaming_label = QLabel("● Stream Idle")
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
            QHeaderView.ResizeMode.ResizeToContents,
        )
        table_header.setSectionResizeMode(
            2,
            QHeaderView.ResizeMode.ResizeToContents,
        )
        table_header.setSectionResizeMode(
            3,
            QHeaderView.ResizeMode.ResizeToContents,
        )

        rows = [
            ("Cloud Rate", "--", "Hz", "IDLE"),
            ("Point Count", "--", "pts/s", "IDLE"),
            ("IMU Status", "--", "", "IDLE"),
            ("Packet Loss", "--", "%", "IDLE"),
            ("Timestamp (LiDAR)", "--", "", "IDLE"),
            ("Power", "--", "W", "IDLE"),
            ("Temperature", "--", "°C", "IDLE"),
            ("Uptime", "--", "hh:mm:ss", "IDLE"),
        ]

        self.monitor_table.setRowCount(len(rows))
        for row_index, values in enumerate(rows):
            for column_index, value in enumerate(values):
                self.monitor_table.setItem(
                    row_index,
                    column_index,
                    QTableWidgetItem(value),
                )

        card.body_layout.addWidget(self.monitor_table)
        return card

    def _build_test_card(self):
        card = Card()

        header = QHBoxLayout()

        title = QLabel("Test Case List")
        title.setObjectName("CardTitle")

        self.selected_tests_label = QLabel("0 / 5 Selected")
        self.selected_tests_label.setStyleSheet(
            "color:#155EEF; font-weight:700;"
        )

        header.addWidget(title)
        header.addStretch()
        header.addWidget(self.selected_tests_label)

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

    def show_device_overview(self):
        DeviceOverviewDialog(
            self.device_overview_data,
            self,
        ).exec()

    def show_network_summary(self):
        self.update_network_summary_data()

        NetworkSummaryDialog(
            self.network_summary_data,
            self,
        ).exec()

    def show_network_protocol(self):
        NetworkProtocolDialog(
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
        self.discover_button.setEnabled(connected)
        self.configure_ip_button.setEnabled(connected)
        self.restore_ip_button.setEnabled(
            connected
            and bool(
                self.temporary_ip_state
                and self.temporary_ip_state.get("active")
                and self.temporary_ip_state.get("added_by_app")
            )
        )
        self.update_network_summary_data()

    def _valid_lidar_interfaces(self) -> list[str]:
        network_snapshot = self.jetson_state.network_snapshot
        if not network_snapshot:
            return []

        result = []

        for interface in network_snapshot.get("interfaces", []):
            if interface.get("protected"):
                continue
            if interface.get("wireless"):
                continue
            if not interface.get("physical"):
                continue

            result.append(interface["name"])

        return result

    def configure_temporary_ip(self):
        if not self._require_jetson_connection(
            "configure the LiDAR network"
        ):
            return

        network_snapshot = self.jetson_state.network_snapshot
        interfaces = self._valid_lidar_interfaces()

        if not interfaces:
            QMessageBox.warning(
                self,
                "Network Configuration",
                "No safe physical Ethernet interface is available.",
            )
            return

        dialog = TemporaryIPDialog(
            interfaces=interfaces,
            candidate=network_snapshot.get("lidar_candidate"),
            parent=self,
        )

        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        values = dialog.values()

        if not values["ip_address"]:
            QMessageBox.warning(
                self,
                "Network Configuration",
                "Jetson LiDAR IP is required.",
            )
            return

        self._temporary_sudo_password = values["sudo_password"]
        self.configure_ip_button.setEnabled(False)

        self.append_log(
            "INFO",
            f"Applying temporary IP {values['ip_address']}/"
            f"{values['prefix']} to {values['interface']}...",
        )

        self.temp_ip_worker = TemporaryIPWorker(
            connection_service=self.jetson_service,
            action="apply",
            interface=values["interface"],
            ip_address=values["ip_address"],
            prefix=values["prefix"],
            sudo_password=values["sudo_password"],
        )
        self.temp_ip_worker.success.connect(self._on_temp_ip_success)
        self.temp_ip_worker.failed.connect(self._on_temp_ip_failed)
        self.temp_ip_worker.finished.connect(
            lambda: self.configure_ip_button.setEnabled(True)
        )
        self.temp_ip_worker.start()

    def restore_temporary_ip(self):
        if not self._require_jetson_connection(
            "restore the LiDAR network"
        ):
            return
        if not self.temporary_ip_state:
            return

        self.restore_ip_button.setEnabled(False)

        self.temp_ip_worker = TemporaryIPWorker(
            connection_service=self.jetson_service,
            action="restore",
            state=self.temporary_ip_state,
            sudo_password=self._temporary_sudo_password,
        )
        self.temp_ip_worker.success.connect(self._on_temp_ip_success)
        self.temp_ip_worker.failed.connect(self._on_temp_ip_failed)
        self.temp_ip_worker.start()

    def _on_temp_ip_success(self, result: dict):
        self.jetson_service.update_network_snapshot(result["network"])
        state = result["state"]

        if result["action"] == "apply":
            self.temporary_ip_state = state

            self.append_log(
                "INFO",
                f"Temporary IP active: {state['cidr']} "
                f"on {state['interface']}.",
            )
        else:
            self.temporary_ip_state = None
            self._temporary_sudo_password = None
            self.append_log("INFO", "Temporary IP restored.")

        self.restore_ip_button.setEnabled(
            bool(
                self.temporary_ip_state
                and self.temporary_ip_state.get("active")
                and self.temporary_ip_state.get("added_by_app")
            )
        )

        self.update_network_summary_data()

    def _on_temp_ip_failed(self, error: str):
        self.append_log("ERROR", error)

        QMessageBox.critical(
            self,
            "Network Configuration Error",
            error,
        )

    def _jetson_lidar_ip(self) -> str | None:
        if (
            self.temporary_ip_state
            and self.temporary_ip_state.get("active")
        ):
            return self.temporary_ip_state["cidr"].split("/", 1)[0]

        network_snapshot = self.jetson_state.network_snapshot
        if not network_snapshot:
            return None

        candidate = network_snapshot.get("lidar_candidate")

        for interface in network_snapshot.get("interfaces", []):
            if interface.get("name") != candidate:
                continue

            addresses = interface.get("ipv4_addresses", [])
            if addresses:
                return addresses[0].split("/", 1)[0]

        return None

    def update_network_summary_data(self):
        network_snapshot = self.jetson_state.network_snapshot
        if not network_snapshot:
            self.network_summary_data = {}
            return

        management = network_snapshot.get("management", {})
        candidate = network_snapshot.get("lidar_candidate")

        lidar_ip = "-"
        if self.livox_device_info:
            lidar_ip = self.livox_device_info.get("lidar_ip") or "-"

        self.network_summary_data = {
            "management_interface": management.get("interface") or "-",
            "management_status": (
                "PROTECTED"
                if management.get("interface")
                else "-"
            ),
            "ssh_ip": management.get("server_ip") or "-",
            "ssh_status": (
                "ONLINE"
                if self.jetson_service.is_connected
                else "OFFLINE"
            ),
            "lidar_interface": candidate or "-",
            "interface_status": "UP" if candidate else "NOT FOUND",
            "jetson_lidar_ip": self._jetson_lidar_ip() or "-",
            "subnet_status": (
                "CONFIGURED"
                if self._jetson_lidar_ip()
                else "NOT CONFIGURED"
            ),
            "lidar_ip": lidar_ip,
            "lidar_ip_status": (
                "DETECTED"
                if lidar_ip != "-"
                else "UNKNOWN"
            ),
            "ping": "-",
            "ping_status": "NOT RUN",
            "link": "UP" if candidate else "-",
            "link_status": "PASS" if candidate else "NOT RUN",
        }

    # ------------------------------------------------------------------
    # Livox
    # ------------------------------------------------------------------

    def auto_discover(self):
        if not self._require_jetson_connection("discover the LiDAR"):
            return

        host_ip = self._jetson_lidar_ip()

        if not host_ip:
            QMessageBox.warning(
                self,
                "LiDAR Network",
                "Configure the Jetson LiDAR-side IP first.",
            )
            return

        model = self.model_combo.currentData()

        self.discover_button.setEnabled(False)
        self.lidar_chip.set_state("warning", "Discovering Device")

        self.append_log(
            "INFO",
            f"Starting Livox SDK2 discovery from {host_ip} for {model}...",
        )

        self.livox_worker = LivoxDiscoveryWorker(
            connection_service=self.jetson_service,
            host_ip=host_ip,
            model=model,
            timeout=8,
        )

        self.livox_worker.success.connect(self._on_livox_success)
        self.livox_worker.failed.connect(self._on_livox_failed)
        self.livox_worker.finished.connect(
            lambda: self.discover_button.setEnabled(True)
        )
        self.livox_worker.start()

    def _on_livox_success(self, result: dict):
        if not result.get("found"):
            self.livox_device_info = None
            self.lidar_chip.set_state("warning", "Device Not Found")
            self.current_status_label.setText("NOT FOUND")
            self.append_log("WARNING", "Livox device was not discovered.")
            return

        self.livox_device_info = result

        model = result.get("model") or "UNKNOWN"
        serial = result.get("serial") or "-"
        lidar_ip = result.get("lidar_ip") or "-"
        sdk = result.get("sdk_version") or "-"
        dev_type = result.get("dev_type") or "-"

        self.current_device_label.setText(f"Livox {model}")
        self.current_serial_label.setText(f"SN: {serial}")
        self.current_status_label.setText("READY")
        self.current_status_label.setStyleSheet(
            "color:#16883F; font-weight:700;"
        )

        self.lidar_chip.set_state("ok", "Device Ready")
        self.start_stream_button.setEnabled(True)

        self.device_overview_data = {
            "Vendor": "Livox",
            "Device Family": "Livox LiDAR",
            "Selected Model": self.model_combo.currentText(),
            "Detected Model": model,
            "Serial Number": serial,
            "Firmware Version": "-",
            "SDK Version": sdk,
            "Device Type": dev_type,
            "Status": "Ready",
        }

        self.protocol_data = {
            "LiDAR IP": lidar_ip,
            "Jetson LiDAR Host IP": self._jetson_lidar_ip() or "-",
            "LiDAR Control Port": "56100",
            "LiDAR Push Message Port": "56200",
            "LiDAR Point Data Port": "56300",
            "LiDAR IMU Data Port": "56400",
            "LiDAR Log Data Port": "56500",
            "Host Control Port": "56101",
            "Host Push Message Port": "56201",
            "Host Point Data Port": "56301",
            "Host IMU Data Port": "56401",
            "Host Log Data Port": "56501",
            "Gateway Address": "-",
            "Subnet Mask": "-",
            "Firmware Version": "-",
            "Work Mode": "-",
            "Scan Pattern": "-",
            "Data Type": "-",
            "Time Sync Type": "-",
            "FOV Enable": "-",
        }

        self.update_network_summary_data()
        self._update_selected_test_count()

        self.append_log(
            "INFO",
            f"LiDAR detected: Livox {model} "
            f"(SN: {serial}) at {lidar_ip}.",
        )

    def _on_livox_failed(self, error: str):
        self.livox_device_info = None
        self.lidar_chip.set_state("error", "Discovery Error")
        self.append_log("ERROR", error)

        QMessageBox.critical(
            self,
            "Livox Discovery Error",
            error,
        )

    # ------------------------------------------------------------------
    # Monitor
    # ------------------------------------------------------------------

    def _set_monitor_row(
        self,
        row_name: str,
        value: str,
        unit: str = "",
        status: str = "OK",
    ):
        for row in range(self.monitor_table.rowCount()):
            item = self.monitor_table.item(row, 0)
            if item and item.text() == row_name:
                self.monitor_table.setItem(
                    row,
                    1,
                    QTableWidgetItem(str(value)),
                )
                self.monitor_table.setItem(
                    row,
                    2,
                    QTableWidgetItem(str(unit)),
                )
                self.monitor_table.setItem(
                    row,
                    3,
                    QTableWidgetItem(str(status)),
                )
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
                "Cloud Rate",
                f"{cloud_rate_hz:.2f}",
                "Hz",
                "OK",
            )

        if point_count is not None:
            self._set_monitor_row(
                "Point Count",
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

        if sensor_timestamp is not None:
            self._set_monitor_row(
                "Timestamp (LiDAR)",
                sensor_timestamp,
                "",
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

        if uptime is not None:
            self._set_monitor_row(
                "Uptime",
                uptime,
                "hh:mm:ss",
                "OK",
            )

        self.streaming_label.setText("● Streaming")
        self.streaming_label.setStyleSheet(
            "color:#16883F; font-weight:700;"
        )
        self.stream_chip.set_state("ok", "Streaming")

    # ------------------------------------------------------------------
    # Test UI
    # ------------------------------------------------------------------

    def _load_test_cases(self):
        tests = [
            ("LID-CON-001", "Connectivity", "Ethernet Interface Detection"),
            ("LID-CON-002", "Connectivity", "Network Configuration"),
            ("LID-CON-003", "Connectivity", "LiDAR Ping"),
            ("LID-CON-004", "Connectivity", "Device Discovery"),
            ("LID-STR-001", "Streaming", "Point Cloud Start"),
        ]

        self.test_table.blockSignals(True)
        self.test_table.setRowCount(len(tests))

        for row, (test_id, group, name) in enumerate(tests):
            select_item = QTableWidgetItem()
            select_item.setFlags(
                Qt.ItemFlag.ItemIsEnabled
                | Qt.ItemFlag.ItemIsUserCheckable
            )
            select_item.setCheckState(Qt.CheckState.Unchecked)

            self.test_table.setItem(row, 0, select_item)
            self.test_table.setItem(row, 1, QTableWidgetItem(test_id))
            self.test_table.setItem(row, 2, QTableWidgetItem(group))
            self.test_table.setItem(row, 3, QTableWidgetItem(name))
            self.test_table.setItem(row, 4, QTableWidgetItem("NOT RUN"))

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
        self.run_test_button.setEnabled(
            selected > 0 and self.livox_device_info is not None
        )

    def run_selected_tests(self):
        selected = []

        for row in range(self.test_table.rowCount()):
            item = self.test_table.item(row, 0)
            if item and item.checkState() == Qt.CheckState.Checked:
                selected.append(
                    self.test_table.item(row, 1).text()
                )

        if not selected:
            return

        self.append_log(
            "INFO",
            "RUN SELECTED requested: " + ", ".join(selected),
        )

        QMessageBox.information(
            self,
            "Test Engine",
            "Test table UI is ready. Connect this action to TestEngine "
            "in the next milestone.",
        )

    # ------------------------------------------------------------------
    # Stream placeholders
    # ------------------------------------------------------------------

    def start_stream(self):
        if not self.livox_device_info:
            return

        self.append_log(
            "INFO",
            "START STREAM requested. Livox stream backend is not yet connected.",
        )

        QMessageBox.information(
            self,
            "Stream Backend",
            "The UI is ready. Point-cloud/IMU stream backend is the next step.",
        )

    def stop_stream(self):
        self.streaming_label.setText("● Stream Idle")
        self.streaming_label.setStyleSheet(
            "color:#667085; font-weight:700;"
        )
        self.stream_chip.set_state("idle", "Stream Idle")
        self.append_log("INFO", "STOP requested.")

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
        self.configure_ip_button.setEnabled(False)
        self.restore_ip_button.setEnabled(False)
        self.start_stream_button.setEnabled(False)
        self.stop_button.setEnabled(False)
        self.run_test_button.setEnabled(False)

        self.append_log(
            "INFO",
            "LiDAR workspace ready. Connect to Jetson to begin.",
        )
