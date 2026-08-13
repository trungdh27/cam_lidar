import os
from pathlib import Path

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.remote.ssh_manager import SSHConfig
from desktop_app.services.jetson_connection_service import (
    JetsonConnectionService,
)
from desktop_app.state.jetson_state import (
    JetsonConnectionStatus,
    JetsonState,
)
from desktop_app.ui.widgets import Card, StatusChip


class CurrentPageStack(QStackedWidget):
    """Size the connection card from the currently visible state."""

    def sizeHint(self):
        current = self.currentWidget()
        return current.sizeHint() if current else super().sizeHint()

    def minimumSizeHint(self):
        current = self.currentWidget()
        return current.minimumSizeHint() if current else super().minimumSizeHint()

    def setCurrentIndex(self, index: int):
        super().setCurrentIndex(index)
        self.updateGeometry()


class MetricCard(QFrame):
    def __init__(self, title: str, value: str = "—", note: str = "", parent=None):
        super().__init__(parent)
        self.setObjectName("MetricCard")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(6)

        title_label = QLabel(title)
        title_label.setObjectName("MetricLabel")

        self.value_label = QLabel(value)
        self.value_label.setObjectName("MetricValue")

        self.note_label = QLabel(note)
        self.note_label.setObjectName("MetricNote")

        layout.addWidget(title_label)
        layout.addWidget(self.value_label)
        layout.addWidget(self.note_label)

    def set_value(self, value: str, note: str = ""):
        self.value_label.setText(value)
        self.note_label.setText(note)


class ContentHeightTable(QTableWidget):
    """Prefer the height required by rows instead of an internal scrollbar."""

    def sizeHint(self):
        hint = super().sizeHint()
        content_height = self.horizontalHeader().sizeHint().height()
        content_height += sum(
            self.rowHeight(row) for row in range(self.rowCount())
        )
        content_height += self.frameWidth() * 2 + 2
        return QSize(hint.width(), content_height)

    def minimumSizeHint(self):
        return self.sizeHint()


class DashboardPage(QWidget):
    navigate_requested = Signal(int)
    refresh_requested = Signal()

    SECTION_SPACING = 12
    COMPACT_BREAKPOINT = 920

    DEVICE_TYPES = [
        ("▣", "Camera", 2, False),
        ("◌", "LiDAR", 3, True),
        ("⌘", "IMU", 4, False),
        ("▤", "CAN", 5, False),
        ("⌘", "EtherCAT", 6, False),
    ]

    AVAILABLE_VALUES = {
        "detected": "Detected",
        "not detected": "Not detected",
        "unknown": "Unknown",
    }

    DEVICE_STATUS_VALUES = {
        "ready": "Ready",
        "running": "Running",
        "warning": "Warning",
        "error": "Error",
        "not configured": "Not configured",
    }

    STATUS_UI = {
        JetsonConnectionStatus.DISCONNECTED: ("idle", "Disconnected"),
        JetsonConnectionStatus.CONNECTING: ("idle", "Connecting..."),
        JetsonConnectionStatus.CONNECTED: ("ok", "Connected"),
        JetsonConnectionStatus.FAILED: ("error", "Failed"),
    }

    def __init__(
        self,
        jetson_state: JetsonState,
        jetson_service: JetsonConnectionService,
        parent=None,
    ):
        super().__init__(parent)

        self.jetson_state = jetson_state
        self.jetson_service = jetson_service
        self.metric_cards = {}
        self._device_data = {}
        self._test_summary = None
        self._recent_activity = []
        self._editing_connection = False
        self._compact_layout = None
        self._evidence_path = (
            Path(__file__).resolve().parents[2] / "evidence"
        )
        self._build_ui()
        self._connect_jetson_signals()
        self.reset_view()
        self._render_jetson_state()

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        self.dashboard_scroll = QScrollArea()
        self.dashboard_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.dashboard_scroll.setWidgetResizable(True)

        self.dashboard_content = QWidget()
        root = QVBoxLayout(self.dashboard_content)
        root.setContentsMargins(28, 24, 28, 24)
        root.setSpacing(self.SECTION_SPACING)

        root.addLayout(self._build_header())
        root.addWidget(self._build_jetson_connection_card())
        self.metrics_layout = self._build_metrics()
        root.addLayout(self.metrics_layout)
        root.addWidget(self._build_network_summary_card())
        self.overview_layout = self._build_overview()
        root.addLayout(self.overview_layout)
        self.bottom_layout = self._build_bottom_section()
        root.addLayout(self.bottom_layout)
        root.addStretch()

        self.dashboard_scroll.setWidget(self.dashboard_content)
        outer.addWidget(self.dashboard_scroll)
        self._apply_responsive_layout(self.width())

    def _build_header(self):
        layout = QHBoxLayout()

        title_block = QVBoxLayout()

        title = QLabel("Dashboard")
        title.setObjectName("PageTitle")

        subtitle = QLabel("System-wide hardware test overview")
        subtitle.setObjectName("Muted")

        title_block.addWidget(title)
        title_block.addWidget(subtitle)

        self.updated_label = QLabel("Not refreshed")
        self.updated_label.setObjectName("Muted")

        self.refresh_button = QPushButton("↻  Refresh")
        self.refresh_button.setObjectName("SmallButton")
        self.refresh_button.clicked.connect(self._request_refresh)

        layout.addLayout(title_block)
        layout.addStretch()
        layout.addWidget(self.updated_label)
        layout.addWidget(self.refresh_button)

        return layout

    def _build_jetson_connection_card(self):
        card = Card("Jetson Connection")
        self.jetson_connection_stack = CurrentPageStack()
        self.jetson_connection_stack.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Maximum,
        )

        editor = QWidget()
        self.jetson_editor_layout = QGridLayout(editor)
        self.jetson_editor_layout.setContentsMargins(0, 0, 0, 0)
        self.jetson_editor_layout.setHorizontalSpacing(10)
        self.jetson_editor_layout.setVerticalSpacing(8)

        self.jetson_host_label = QLabel("Host/IP:")
        self.jetson_editor_layout.addWidget(self.jetson_host_label, 0, 0)
        self.jetson_host_input = QLineEdit()
        self.jetson_host_input.setPlaceholderText("192.168.9.169")
        self.jetson_host_input.setSizePolicy(
            QSizePolicy.Policy.Preferred,
            QSizePolicy.Policy.Fixed,
        )
        self.jetson_editor_layout.addWidget(self.jetson_host_input, 0, 1)

        self.jetson_username_label = QLabel("Username:")
        self.jetson_editor_layout.addWidget(
            self.jetson_username_label,
            0,
            2,
        )
        self.jetson_username_input = QLineEdit()
        self.jetson_username_input.setText("huu")
        self.jetson_username_input.setSizePolicy(
            QSizePolicy.Policy.Preferred,
            QSizePolicy.Policy.Fixed,
        )
        self.jetson_editor_layout.addWidget(
            self.jetson_username_input,
            0,
            3,
        )

        self.jetson_password_label = QLabel("Password:")
        self.jetson_editor_layout.addWidget(
            self.jetson_password_label,
            1,
            0,
        )
        self.jetson_password_input = QLineEdit()
        self.jetson_password_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.jetson_password_input.setPlaceholderText(
            "Optional with SSH key"
        )
        self.jetson_password_input.setSizePolicy(
            QSizePolicy.Policy.Preferred,
            QSizePolicy.Policy.Fixed,
        )
        self.jetson_editor_layout.addWidget(
            self.jetson_password_input,
            1,
            1,
            1,
            3,
        )

        self.jetson_status_chip = StatusChip("Disconnected", "idle")
        self.jetson_status_chip.setSizePolicy(
            QSizePolicy.Policy.Maximum,
            QSizePolicy.Policy.Fixed,
        )
        self.jetson_editor_layout.addWidget(
            self.jetson_status_chip,
            0,
            4,
        )

        self.jetson_connect_button = QPushButton("Connect")
        self.jetson_connect_button.setObjectName("PrimaryButton")
        self.jetson_connect_button.clicked.connect(
            self._request_jetson_connect
        )
        self.jetson_editor_layout.addWidget(
            self.jetson_connect_button,
            0,
            5,
        )

        self.jetson_back_button = QPushButton("Back")
        self.jetson_back_button.setObjectName("OutlineButton")
        self.jetson_back_button.clicked.connect(
            self._show_connection_summary
        )
        self.jetson_editor_layout.addWidget(
            self.jetson_back_button,
            1,
            5,
        )

        self.jetson_error_label = QLabel()
        self.jetson_error_label.setWordWrap(True)
        self.jetson_error_label.setStyleSheet("color:#D92D20;")
        self.jetson_editor_layout.addWidget(
            self.jetson_error_label,
            2,
            0,
            1,
            6,
        )

        self.jetson_editor_layout.setColumnStretch(1, 2)
        self.jetson_editor_layout.setColumnStretch(3, 2)
        self.jetson_editor_layout.setColumnStretch(6, 1)

        connected_summary = QWidget()
        self.jetson_summary_layout = QGridLayout(connected_summary)
        self.jetson_summary_layout.setContentsMargins(0, 0, 0, 0)
        self.jetson_summary_layout.setHorizontalSpacing(14)
        self.jetson_summary_layout.setVerticalSpacing(6)

        self.jetson_connected_status_chip = StatusChip("Connected", "ok")
        self.jetson_summary_layout.addWidget(
            self.jetson_connected_status_chip,
            0,
            0,
            2,
            1,
        )

        host_title = QLabel("Host/IP")
        host_title.setObjectName("Muted")
        self.jetson_connected_host = QLabel("—")
        self.jetson_connected_host.setObjectName("ValueLabel")
        self.jetson_summary_layout.addWidget(host_title, 0, 1)
        self.jetson_summary_layout.addWidget(
            self.jetson_connected_host,
            1,
            1,
        )

        username_title = QLabel("Username")
        username_title.setObjectName("Muted")
        self.jetson_connected_username = QLabel("—")
        self.jetson_connected_username.setObjectName("ValueLabel")
        self.jetson_summary_layout.addWidget(username_title, 0, 2)
        self.jetson_summary_layout.addWidget(
            self.jetson_connected_username,
            1,
            2,
        )

        ssh_title = QLabel("SSH")
        ssh_title.setObjectName("Muted")
        self.jetson_connected_ssh = QLabel("Ready")
        self.jetson_connected_ssh.setObjectName("ValueLabel")
        self.jetson_summary_layout.addWidget(ssh_title, 0, 3)
        self.jetson_summary_layout.addWidget(
            self.jetson_connected_ssh,
            1,
            3,
        )

        self.jetson_disconnect_button = QPushButton("Disconnect")
        self.jetson_disconnect_button.setObjectName("DangerButton")
        self.jetson_disconnect_button.clicked.connect(
            self._request_jetson_disconnect
        )
        self.jetson_summary_layout.addWidget(
            self.jetson_disconnect_button,
            0,
            4,
            2,
            1,
        )

        self.jetson_details_button = QPushButton("Connection details")
        self.jetson_details_button.setObjectName("OutlineButton")
        self.jetson_details_button.clicked.connect(
            self._show_connection_editor
        )
        self.jetson_summary_layout.addWidget(
            self.jetson_details_button,
            0,
            5,
            2,
            1,
        )
        self.jetson_summary_layout.setColumnStretch(3, 1)

        self.jetson_connection_stack.addWidget(editor)
        self.jetson_connection_stack.addWidget(connected_summary)
        card.body_layout.addWidget(self.jetson_connection_stack)
        return card

    def _connect_jetson_signals(self):
        self.jetson_service.connecting.connect(self._on_jetson_connecting)
        self.jetson_service.connected.connect(self._on_jetson_connected)
        self.jetson_service.disconnected.connect(
            self._on_jetson_disconnected
        )
        self.jetson_service.connection_failed.connect(
            self._on_jetson_connection_failed
        )
        self.jetson_state.network_snapshot_changed.connect(
            self._on_network_snapshot_changed
        )

    def _request_refresh(self):
        self._update_infrastructure_overview()
        self.refresh_requested.emit()

    def _request_jetson_connect(self):
        host = self.jetson_host_input.text().strip()
        username = self.jetson_username_input.text().strip()

        if not host or not username:
            self.jetson_error_label.setText(
                "Jetson Host/IP and username are required."
            )
            return

        self._editing_connection = False
        self.jetson_error_label.clear()
        self.jetson_service.connect_to(
            SSHConfig(
                host=host,
                username=username,
                password=self.jetson_password_input.text() or None,
            )
        )

    def _request_jetson_disconnect(self):
        self.jetson_disconnect_button.setEnabled(False)
        self.jetson_service.disconnect_from_jetson()

    def _show_connection_editor(self):
        self._editing_connection = True
        self._render_jetson_state()
        self.jetson_host_input.setFocus()

    def _show_connection_summary(self):
        if not self.jetson_state.connected:
            return
        self._editing_connection = False
        self._render_jetson_state()

    def _on_jetson_connecting(self):
        self._render_jetson_state()

    def _on_jetson_connected(self, _state):
        self._editing_connection = False
        self.jetson_password_input.clear()
        self._render_jetson_state()

    def _on_jetson_disconnected(self):
        self._editing_connection = False
        self.jetson_password_input.clear()
        self._render_jetson_state()

    def _on_jetson_connection_failed(self, _error: str):
        self._editing_connection = True
        self._render_jetson_state()

    def _on_network_snapshot_changed(self, _snapshot):
        self._update_infrastructure_overview()

    def _render_jetson_state(self):
        state = self.jetson_state
        if state.host:
            self.jetson_host_input.setText(state.host)
        if state.username:
            self.jetson_username_input.setText(state.username)

        chip_state, status_text = self.STATUS_UI[state.status]
        self.jetson_status_chip.set_state(chip_state, status_text)
        self.jetson_connected_status_chip.set_state(
            chip_state,
            status_text,
        )
        self.jetson_error_label.setText(state.last_error)

        connecting = state.status == JetsonConnectionStatus.CONNECTING
        connected = state.status == JetsonConnectionStatus.CONNECTED
        self.jetson_connect_button.setEnabled(not connecting)
        self.jetson_connect_button.setText(
            "Reconnect" if connected else "Connect"
        )
        self.jetson_back_button.setVisible(connected)
        self.jetson_back_button.setEnabled(not connecting)
        self.jetson_disconnect_button.setEnabled(connected)
        self.jetson_details_button.setEnabled(connected)
        self.jetson_host_input.setEnabled(not connecting)
        self.jetson_username_input.setEnabled(not connecting)
        self.jetson_password_input.setEnabled(not connecting)

        self.jetson_connected_host.setText(state.host or "—")
        self.jetson_connected_username.setText(state.username or "—")
        self.jetson_connected_ssh.setText("Ready" if connected else "—")

        show_summary = connected and not self._editing_connection
        self.jetson_connection_stack.setCurrentIndex(1 if show_summary else 0)
        self._update_quick_actions()
        self._update_infrastructure_overview()

    def _build_metrics(self):
        layout = QGridLayout()
        layout.setHorizontalSpacing(self.SECTION_SPACING)
        layout.setVerticalSpacing(self.SECTION_SPACING)

        definitions = [
            ("devices", "Devices"),
            ("online", "Online"),
            ("running", "Tests Running"),
            ("results", "PASS / FAIL"),
        ]

        for column, (key, title) in enumerate(definitions):
            card = MetricCard(title)
            card.setSizePolicy(
                QSizePolicy.Policy.Expanding,
                QSizePolicy.Policy.Preferred,
            )
            self.metric_cards[key] = card
            layout.addWidget(card, 0, column)
            layout.setColumnStretch(column, 1)

        return layout

    def _build_network_summary_card(self):
        card = Card("Network Summary")

        self.network_jetson_row = self._build_network_row(
            "Jetson",
            "—",
            "SSH",
        )
        self.network_management_row = self._build_network_row(
            "Host Connection",
            "—",
            "—",
        )
        self.network_lidar_row = self._build_network_row(
            "LiDAR Network",
            "—",
            "—",
        )

        card.body_layout.addWidget(self.network_jetson_row["widget"])
        card.body_layout.addWidget(self.network_management_row["widget"])
        card.body_layout.addWidget(self.network_lidar_row["widget"])

        self.network_empty_label = QLabel()
        self.network_empty_label.setObjectName("Muted")
        self.network_empty_label.setWordWrap(True)
        card.body_layout.addWidget(self.network_empty_label)
        return card

    @staticmethod
    def _build_network_row(
        title: str,
        primary: str,
        secondary: str,
    ) -> dict:
        widget = QWidget()
        layout = QHBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        title_label = QLabel(title)
        title_label.setObjectName("KeyLabel")
        title_label.setSizePolicy(
            QSizePolicy.Policy.Preferred,
            QSizePolicy.Policy.Preferred,
        )

        primary_label = QLabel(primary)
        primary_label.setObjectName("ValueLabel")
        primary_label.setSizePolicy(
            QSizePolicy.Policy.Preferred,
            QSizePolicy.Policy.Preferred,
        )

        secondary_label = QLabel(secondary)
        secondary_label.setObjectName("Muted")
        secondary_label.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Preferred,
        )

        status_chip = StatusChip("Unknown", "idle")
        status_chip.setSizePolicy(
            QSizePolicy.Policy.Maximum,
            QSizePolicy.Policy.Fixed,
        )

        layout.addWidget(title_label)
        layout.addWidget(primary_label)
        layout.addWidget(secondary_label)
        layout.addStretch()
        layout.addWidget(status_chip)

        return {
            "widget": widget,
            "primary": primary_label,
            "secondary": secondary_label,
            "status": status_chip,
        }

    def _build_overview(self):
        layout = QGridLayout()
        layout.setHorizontalSpacing(self.SECTION_SPACING)
        layout.setVerticalSpacing(self.SECTION_SPACING)

        self.fleet_card = Card("Device fleet")
        self.fleet_card.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Expanding,
        )

        self.device_table = ContentHeightTable(len(self.DEVICE_TYPES), 6)
        self.device_table.setHorizontalHeaderLabels(
            [
                "Device",
                "Model / SN",
                "Available",
                "Status",
                "Last Update",
                "Action",
            ]
        )
        self._configure_table(self.device_table)
        self.device_table.setWordWrap(True)
        self.device_table.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Expanding,
        )
        self.device_table.verticalHeader().setDefaultSectionSize(44)

        header = self.device_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(
            2,
            QHeaderView.ResizeMode.ResizeToContents,
        )
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(
            5,
            QHeaderView.ResizeMode.ResizeToContents,
        )

        self.device_action_buttons = {}
        for row, (icon, name, nav_index, implemented) in enumerate(
            self.DEVICE_TYPES
        ):
            values = [
                f"{icon}  {name}",
                "—",
                "Unknown",
                "Not configured",
                "—",
            ]
            for column, value in enumerate(values):
                self.device_table.setItem(
                    row,
                    column,
                    QTableWidgetItem(value),
                )

            open_button = QPushButton("Open")
            open_button.setObjectName("SmallButton")
            open_button.setEnabled(implemented)
            if implemented:
                open_button.clicked.connect(
                    lambda _checked=False, index=nav_index: (
                        self.navigate_requested.emit(index)
                    )
                )
            else:
                open_button.setToolTip("Module not implemented yet")
            self.device_action_buttons[name] = open_button
            self.device_table.setCellWidget(row, 5, open_button)

        self.fleet_card.body_layout.addWidget(self.device_table)

        self.readiness_card = Card("System readiness")
        self.readiness_card.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Maximum,
        )

        self.readiness_value = QLabel("0%")
        self.readiness_value.setObjectName("ReadinessValue")

        self.readiness_text = QLabel("No device information")
        self.readiness_text.setObjectName("Muted")
        self.readiness_text.setWordWrap(True)

        self.readiness_bar = QProgressBar()
        self.readiness_bar.setRange(0, 100)
        self.readiness_bar.setValue(0)
        self.readiness_bar.setTextVisible(False)

        self.host_status = QLabel()
        self.remote_status = QLabel()
        self.interface_status = QLabel()
        self.storage_status = QLabel()
        for label in (
            self.host_status,
            self.remote_status,
            self.interface_status,
            self.storage_status,
        ):
            label.setWordWrap(True)

        self.readiness_card.body_layout.addWidget(self.readiness_value)
        self.readiness_card.body_layout.addWidget(self.readiness_text)
        self.readiness_card.body_layout.addWidget(self.readiness_bar)
        self.readiness_card.body_layout.addSpacing(8)
        self.readiness_card.body_layout.addWidget(self.host_status)
        self.readiness_card.body_layout.addWidget(self.remote_status)
        self.readiness_card.body_layout.addWidget(self.interface_status)
        self.readiness_card.body_layout.addWidget(self.storage_status)

        layout.addWidget(self.fleet_card, 0, 0)
        layout.addWidget(
            self.readiness_card,
            0,
            1,
            alignment=Qt.AlignmentFlag.AlignTop,
        )
        layout.setColumnStretch(0, 2)
        layout.setColumnStretch(1, 1)

        return layout

    def _build_bottom_section(self):
        layout = QGridLayout()
        layout.setHorizontalSpacing(self.SECTION_SPACING)
        layout.setVerticalSpacing(self.SECTION_SPACING)

        self.activity_card = Card("Recent test activity")
        self.activity_card.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Preferred,
        )

        self.activity_table = QTableWidget(0, 4)
        self.activity_table.setHorizontalHeaderLabels(
            ["Test suite", "Scope", "Result", "Time"]
        )
        self._configure_table(self.activity_table)
        self.activity_table.verticalHeader().setDefaultSectionSize(38)
        self.activity_table.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Preferred,
        )

        activity_header = self.activity_table.horizontalHeader()
        activity_header.setSectionResizeMode(
            0,
            QHeaderView.ResizeMode.Stretch,
        )
        for column in range(1, 4):
            activity_header.setSectionResizeMode(
                column,
                QHeaderView.ResizeMode.ResizeToContents,
            )

        self.activity_empty_state = QWidget()
        self.activity_empty_state.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Maximum,
        )
        empty_layout = QVBoxLayout(self.activity_empty_state)
        empty_layout.setContentsMargins(8, 10, 8, 10)
        empty_layout.setSpacing(7)

        empty_title = QLabel("No test activity yet.")
        empty_title.setObjectName("CardTitle")
        empty_message = QLabel(
            "Run a test suite to see results here."
        )
        empty_message.setObjectName("Muted")

        self.activity_run_button = QPushButton("▷  Run test suite")
        self.activity_run_button.setObjectName("PrimaryButton")
        self.activity_run_button.setSizePolicy(
            QSizePolicy.Policy.Maximum,
            QSizePolicy.Policy.Fixed,
        )
        self.activity_run_button.clicked.connect(
            lambda: self.navigate_requested.emit(7)
        )

        empty_layout.addWidget(empty_title)
        empty_layout.addWidget(empty_message)
        empty_layout.addWidget(self.activity_run_button)

        self.activity_card.body_layout.addWidget(self.activity_empty_state)
        self.activity_card.body_layout.addWidget(self.activity_table)

        self.actions_card = Card("Quick actions")
        self.actions_card.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Maximum,
        )

        self.quick_run_button = QPushButton("▷  Run test suite")
        self.quick_run_button.setObjectName("PrimaryButton")
        self.quick_run_button.clicked.connect(
            lambda: self.navigate_requested.emit(7)
        )

        self.quick_discover_button = QPushButton("◌  Discover LiDAR")
        self.quick_discover_button.setObjectName("OutlineButton")
        self.quick_discover_button.clicked.connect(
            lambda: self.navigate_requested.emit(3)
        )

        self.quick_reports_button = QPushButton("▤  View reports")
        self.quick_reports_button.setObjectName("OutlineButton")
        self.quick_reports_button.clicked.connect(
            lambda: self.navigate_requested.emit(10)
        )

        self.actions_card.body_layout.addWidget(self.quick_run_button)
        self.actions_card.body_layout.addWidget(self.quick_discover_button)
        self.actions_card.body_layout.addWidget(self.quick_reports_button)

        layout.addWidget(self.activity_card, 0, 0)
        layout.addWidget(
            self.actions_card,
            0,
            1,
            alignment=Qt.AlignmentFlag.AlignTop,
        )
        layout.setColumnStretch(0, 2)
        layout.setColumnStretch(1, 1)

        return layout

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._apply_responsive_layout(event.size().width())

    def _apply_responsive_layout(self, width: int):
        if not hasattr(self, "bottom_layout"):
            return

        compact = width < self.COMPACT_BREAKPOINT
        if compact == self._compact_layout:
            return
        self._compact_layout = compact

        self._layout_jetson_connection(compact)
        self._layout_metric_cards(compact)
        self._layout_overview(compact)
        self._layout_bottom_section(compact)

        self.dashboard_content.updateGeometry()
        self.jetson_connection_stack.updateGeometry()

    def _layout_jetson_connection(self, compact: bool):
        editor = self.jetson_editor_layout
        widgets = (
            self.jetson_host_label,
            self.jetson_host_input,
            self.jetson_username_label,
            self.jetson_username_input,
            self.jetson_password_label,
            self.jetson_password_input,
            self.jetson_status_chip,
            self.jetson_connect_button,
            self.jetson_back_button,
            self.jetson_error_label,
        )
        for widget in widgets:
            editor.removeWidget(widget)

        if compact:
            editor.addWidget(self.jetson_host_label, 0, 0)
            editor.addWidget(self.jetson_host_input, 0, 1, 1, 3)
            editor.addWidget(self.jetson_username_label, 1, 0)
            editor.addWidget(self.jetson_username_input, 1, 1, 1, 3)
            editor.addWidget(self.jetson_password_label, 2, 0)
            editor.addWidget(self.jetson_password_input, 2, 1, 1, 3)
            editor.addWidget(self.jetson_status_chip, 3, 0, 1, 2)
            editor.addWidget(self.jetson_connect_button, 3, 2)
            editor.addWidget(self.jetson_back_button, 3, 3)
            editor.addWidget(self.jetson_error_label, 4, 0, 1, 4)
            editor.setColumnStretch(1, 1)
            editor.setColumnStretch(3, 1)
            editor.setColumnStretch(6, 0)
        else:
            editor.addWidget(self.jetson_host_label, 0, 0)
            editor.addWidget(self.jetson_host_input, 0, 1)
            editor.addWidget(self.jetson_username_label, 0, 2)
            editor.addWidget(self.jetson_username_input, 0, 3)
            editor.addWidget(self.jetson_password_label, 1, 0)
            editor.addWidget(self.jetson_password_input, 1, 1, 1, 3)
            editor.addWidget(self.jetson_status_chip, 0, 4)
            editor.addWidget(self.jetson_connect_button, 0, 5)
            editor.addWidget(self.jetson_back_button, 1, 5)
            editor.addWidget(self.jetson_error_label, 2, 0, 1, 6)
            editor.setColumnStretch(1, 2)
            editor.setColumnStretch(3, 2)
            editor.setColumnStretch(6, 1)

        summary = self.jetson_summary_layout
        summary.removeWidget(self.jetson_disconnect_button)
        summary.removeWidget(self.jetson_details_button)
        if compact:
            summary.addWidget(self.jetson_disconnect_button, 2, 0, 1, 2)
            summary.addWidget(self.jetson_details_button, 2, 2, 1, 2)
        else:
            summary.addWidget(
                self.jetson_disconnect_button,
                0,
                4,
                2,
                1,
            )
            summary.addWidget(
                self.jetson_details_button,
                0,
                5,
                2,
                1,
            )

    def _layout_metric_cards(self, compact: bool):
        cards = list(self.metric_cards.values())
        for card in cards:
            self.metrics_layout.removeWidget(card)

        for index, card in enumerate(cards):
            row = index // 2 if compact else 0
            column = index % 2 if compact else index
            self.metrics_layout.addWidget(card, row, column)

        for column in range(4):
            self.metrics_layout.setColumnStretch(
                column,
                1 if (not compact or column < 2) else 0,
            )

    def _layout_overview(self, compact: bool):
        self.overview_layout.removeWidget(self.fleet_card)
        self.overview_layout.removeWidget(self.readiness_card)
        self.overview_layout.addWidget(self.fleet_card, 0, 0)
        if compact:
            self.overview_layout.addWidget(
                self.readiness_card,
                1,
                0,
                alignment=Qt.AlignmentFlag.AlignTop,
            )
            self.overview_layout.setColumnStretch(0, 1)
            self.overview_layout.setColumnStretch(1, 0)
        else:
            self.overview_layout.addWidget(
                self.readiness_card,
                0,
                1,
                alignment=Qt.AlignmentFlag.AlignTop,
            )
            self.overview_layout.setColumnStretch(0, 2)
            self.overview_layout.setColumnStretch(1, 1)

    def _layout_bottom_section(self, compact: bool):
        self.bottom_layout.removeWidget(self.activity_card)
        self.bottom_layout.removeWidget(self.actions_card)
        self.bottom_layout.addWidget(self.activity_card, 0, 0)
        if compact:
            self.bottom_layout.addWidget(
                self.actions_card,
                1,
                0,
                alignment=Qt.AlignmentFlag.AlignTop,
            )
            self.bottom_layout.setColumnStretch(0, 1)
            self.bottom_layout.setColumnStretch(1, 0)
        else:
            self.bottom_layout.addWidget(
                self.actions_card,
                0,
                1,
                alignment=Qt.AlignmentFlag.AlignTop,
            )
            self.bottom_layout.setColumnStretch(0, 2)
            self.bottom_layout.setColumnStretch(1, 1)

    @staticmethod
    def _configure_table(table: QTableWidget):
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        table.setSelectionMode(
            QAbstractItemView.SelectionMode.NoSelection
        )
        table.setAlternatingRowColors(True)

    def reset_view(self):
        supported = len(self.DEVICE_TYPES)
        self.metric_cards["devices"].set_value(
            f"— / {supported}",
            "No device data",
        )
        self.metric_cards["online"].set_value("—", "No device data")
        self.metric_cards["running"].set_value("—", "No tests running")
        self.metric_cards["results"].set_value("— / —", "No test results")

        self.set_readiness(0, "No device information")
        self._render_device_fleet()
        self.set_recent_activity([])

    def set_device_data(self, devices: list[dict]):
        """Render device data supplied by a real registry or discovery source."""
        supported_names = {
            name.casefold(): name
            for _icon, name, _nav_index, _implemented in self.DEVICE_TYPES
        }
        normalized = {}

        for device in devices:
            raw_name = (
                device.get("device")
                or device.get("name")
                or device.get("type")
                or ""
            )
            name = supported_names.get(str(raw_name).strip().casefold())
            if not name:
                continue

            available = self._normalize_availability(device)
            status_key = str(
                device.get("status") or "not configured"
            ).strip().casefold()
            status = self.DEVICE_STATUS_VALUES.get(
                status_key,
                "Not configured",
            )

            normalized[name] = {
                "model": self._display_value(device.get("model")),
                "serial": self._display_value(device.get("serial")),
                "available": available,
                "status": status,
                "last_update": self._display_value(
                    device.get("last_update")
                ),
            }

        self._device_data = normalized
        self._render_device_fleet()
        self._render_device_summary()

    def set_test_summary(
        self,
        running: int | None,
        passed: int | None,
        failed: int | None,
    ):
        """Render test counts supplied by a test-session data source."""
        self._test_summary = {
            "running": self._non_negative_count(running),
            "passed": self._non_negative_count(passed),
            "failed": self._non_negative_count(failed),
        }
        self._render_test_summary()

    def set_recent_activity(self, activities: list[dict]):
        """Render activity supplied by a real test-history data source."""
        normalized = []
        result_values = {"PASS", "FAIL", "RUNNING", "ERROR"}

        for activity in activities:
            raw_result = str(activity.get("result") or "").strip().upper()
            result = raw_result if raw_result in result_values else "—"
            normalized.append(
                {
                    "suite": self._display_value(
                        activity.get("suite")
                        or activity.get("test_suite")
                    ),
                    "scope": self._display_value(activity.get("scope")),
                    "result": result,
                    "time": self._display_value(
                        activity.get("time")
                        or activity.get("timestamp")
                    ),
                }
            )

        self._recent_activity = normalized
        self._render_recent_activity()

    def _render_recent_activity(self):
        if not hasattr(self, "activity_table"):
            return

        has_activity = bool(self._recent_activity)
        self.activity_empty_state.setVisible(not has_activity)
        self.activity_table.setVisible(has_activity)
        self.activity_table.setRowCount(len(self._recent_activity))

        result_states = {
            "PASS": "ok",
            "FAIL": "error",
            "RUNNING": "warning",
            "ERROR": "error",
            "—": "idle",
        }
        for row, activity in enumerate(self._recent_activity):
            self.activity_table.setItem(
                row,
                0,
                QTableWidgetItem(activity["suite"]),
            )
            self.activity_table.setItem(
                row,
                1,
                QTableWidgetItem(activity["scope"]),
            )
            result = activity["result"]
            result_chip = StatusChip(
                result,
                result_states[result],
            )
            self.activity_table.setCellWidget(row, 2, result_chip)
            self.activity_table.setItem(
                row,
                3,
                QTableWidgetItem(activity["time"]),
            )

    def _update_quick_actions(self):
        if not hasattr(self, "quick_discover_button"):
            return
        connected = self.jetson_state.connected
        self.quick_discover_button.setEnabled(connected)
        self.quick_discover_button.setToolTip(
            ""
            if connected
            else "Connect Jetson from Dashboard before LiDAR discovery"
        )

    def _render_device_fleet(self):
        if not hasattr(self, "device_table"):
            return

        for row, (_icon, name, _nav_index, _implemented) in enumerate(
            self.DEVICE_TYPES
        ):
            device = self._device_data.get(name)
            if device is None:
                values = ["—", "Unknown", "Not configured", "—"]
            else:
                values = [
                    self._model_serial_text(device),
                    device["available"],
                    device["status"],
                    device["last_update"],
                ]

            for column, value in enumerate(values, start=1):
                self.device_table.item(row, column).setText(value)

        self.device_table.resizeRowsToContents()
        self.device_table.updateGeometry()
        self.fleet_card.updateGeometry()
        self.overview_layout.invalidate()

    def _render_device_summary(self):
        supported = len(self.DEVICE_TYPES)
        if not self._device_data:
            self.metric_cards["devices"].set_value(
                f"— / {supported}",
                "No device data",
            )
            self.metric_cards["online"].set_value(
                "—",
                "No device data",
            )
            return

        detected = sum(
            device["available"] == "Detected"
            for device in self._device_data.values()
        )
        self.metric_cards["devices"].set_value(
            f"{detected} / {supported}",
            "Detected / supported device types",
        )
        self.metric_cards["online"].set_value(
            str(detected),
            "Detected and available",
        )

    def _render_test_summary(self):
        summary = self._test_summary
        if summary is None:
            self.metric_cards["running"].set_value(
                "—",
                "No tests running",
            )
            self.metric_cards["results"].set_value(
                "— / —",
                "No test results",
            )
            return

        running = summary["running"]
        self.metric_cards["running"].set_value(
            "—" if running is None else str(running),
            (
                "No test runtime data"
                if running is None
                else "No tests running"
                if running == 0
                else "Active test execution"
            ),
        )

        passed = summary["passed"]
        failed = summary["failed"]
        if passed is None and failed is None:
            result_note = "No test results"
        else:
            result_note = "Current test session"
        self.metric_cards["results"].set_value(
            (
                f"{'—' if passed is None else passed} / "
                f"{'—' if failed is None else failed}"
            ),
            result_note,
        )

    def _normalize_availability(self, device: dict) -> str:
        found = device.get("found")
        if isinstance(found, bool):
            return "Detected" if found else "Not detected"

        key = str(device.get("available") or "unknown").strip().casefold()
        return self.AVAILABLE_VALUES.get(key, "Unknown")

    @staticmethod
    def _display_value(value) -> str:
        if value is None:
            return "—"
        text = str(value).strip()
        return text or "—"

    @staticmethod
    def _model_serial_text(device: dict) -> str:
        model = device["model"]
        serial = device["serial"]
        if model == "—" and serial == "—":
            return "—"
        if serial == "—":
            return model
        if model == "—":
            return f"SN: {serial}"
        return f"{model}\nSN: {serial}"

    @staticmethod
    def _non_negative_count(value: int | None) -> int | None:
        if value is None:
            return None
        return max(0, int(value))

    def _update_infrastructure_overview(self):
        if not hasattr(self, "readiness_bar"):
            return
        self._update_system_readiness()
        self._render_network_summary()

    def _update_system_readiness(self):
        host_ready = True
        jetson_ready = self.jetson_state.connected
        interface = self._lidar_interface()
        interface_ready = self._interface_is_up(interface)
        storage_ready = (
            self._evidence_path.is_dir()
            and os.access(self._evidence_path, os.W_OK)
        )

        ready_count = sum(
            (host_ready, jetson_ready, interface_ready, storage_ready)
        )
        percent = ready_count * 25
        if ready_count == 4:
            readiness = "Ready"
        elif ready_count > 0:
            readiness = "Partially ready"
        else:
            readiness = "Not ready"
        self.set_readiness(
            percent,
            f"{readiness} · {ready_count} of 4 prerequisites ready",
        )

        self._set_readiness_item(
            self.host_status,
            "Host application",
            "Ready",
            host_ready,
        )
        _chip_state, jetson_text = self.STATUS_UI[
            self.jetson_state.status
        ]
        self._set_readiness_item(
            self.remote_status,
            "Jetson / Remote target",
            jetson_text,
            jetson_ready,
        )

        if interface_ready:
            interface_text = f"Ready ({interface.get('name')})"
        elif interface is None:
            interface_text = "Unknown"
        else:
            state = self._interface_state(interface)
            interface_text = state if state != "Unknown" else "Detected"
        self._set_readiness_item(
            self.interface_status,
            "Device interfaces",
            interface_text,
            interface_ready,
        )
        self._set_readiness_item(
            self.storage_status,
            "Evidence storage",
            "Available" if storage_ready else "Unavailable",
            storage_ready,
        )

    @staticmethod
    def _set_readiness_item(
        label: QLabel,
        title: str,
        value: str,
        ready: bool,
    ):
        marker = "✓" if ready else "!"
        label.setText(f"{marker}  {title}: {value}")

    def _render_network_summary(self):
        state = self.jetson_state
        chip_state, status_text = self.STATUS_UI[state.status]
        self.network_jetson_row["primary"].setText(state.host or "—")
        self.network_jetson_row["secondary"].setText("SSH")
        self.network_jetson_row["status"].set_state(
            chip_state,
            status_text,
        )

        snapshot = state.network_snapshot or {}
        management = snapshot.get("management") or {}
        management_name = management.get("interface")
        management_interface = self._find_interface(management_name)
        management_ip = self._interface_ip(management_interface)
        if management_ip == "—":
            management_ip = (
                management.get("source_ip")
                or management.get("server_ip")
                or "—"
            )

        has_management = bool(management_name)
        self.network_management_row["widget"].setVisible(has_management)
        if has_management:
            self.network_management_row["primary"].setText(
                management_name
            )
            self.network_management_row["secondary"].setText(
                management_ip
            )
            self._set_interface_chip(
                self.network_management_row["status"],
                management_interface,
            )

        candidate_name = snapshot.get("lidar_candidate")
        candidate_interface = self._find_interface(candidate_name)
        has_candidate = bool(candidate_name)
        self.network_lidar_row["widget"].setVisible(has_candidate)
        if has_candidate:
            self.network_lidar_row["primary"].setText(candidate_name)
            self.network_lidar_row["secondary"].setText(
                self._interface_ip(candidate_interface)
            )
            self._set_interface_chip(
                self.network_lidar_row["status"],
                candidate_interface,
            )

        if state.status == JetsonConnectionStatus.FAILED:
            empty_text = state.last_error or "Network inspection failed."
        elif not snapshot:
            empty_text = (
                "Connect Jetson to inspect host and LiDAR interfaces."
            )
        elif not has_candidate:
            empty_text = "No LiDAR network interface detected."
        else:
            empty_text = ""
        self.network_empty_label.setText(empty_text)
        self.network_empty_label.setVisible(bool(empty_text))

    def _find_interface(self, name: str | None) -> dict | None:
        if not name:
            return None
        snapshot = self.jetson_state.network_snapshot or {}
        for interface in snapshot.get("interfaces", []):
            if interface.get("name") == name:
                return interface
        return None

    def _lidar_interface(self) -> dict | None:
        snapshot = self.jetson_state.network_snapshot or {}
        return self._find_interface(snapshot.get("lidar_candidate"))

    @classmethod
    def _interface_is_up(cls, interface: dict | None) -> bool:
        if interface is None:
            return False
        return (
            cls._interface_state(interface) == "UP"
            or interface.get("carrier") is True
        )

    @staticmethod
    def _interface_state(interface: dict | None) -> str:
        if interface is None:
            return "Unknown"
        state = str(interface.get("operstate") or "").strip().upper()
        if state:
            return state
        carrier = interface.get("carrier")
        if carrier is True:
            return "UP"
        if carrier is False:
            return "DOWN"
        return "Unknown"

    @staticmethod
    def _interface_ip(interface: dict | None) -> str:
        if not interface:
            return "—"
        addresses = interface.get("ipv4_addresses") or []
        return str(addresses[0]) if addresses else "—"

    @classmethod
    def _set_interface_chip(
        cls,
        chip: StatusChip,
        interface: dict | None,
    ):
        state = cls._interface_state(interface)
        if state == "UP":
            chip.set_state("ok", "UP")
        elif state == "DOWN":
            chip.set_state("error", "DOWN")
        else:
            chip.set_state("idle", state)

    def set_readiness(self, percent: int, message: str):
        value = max(0, min(percent, 100))
        self.readiness_bar.setValue(value)
        self.readiness_value.setText(f"{value}%")
        self.readiness_text.setText(message)
