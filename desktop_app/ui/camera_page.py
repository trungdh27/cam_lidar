import html
from datetime import datetime, timezone

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QPixmap
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
    QLineEdit,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from desktop_app.services.jetson_connection_service import (
    JetsonConnectionService,
)
from desktop_app.services.camera_inventory_service import CameraInventoryService
from desktop_app.controllers.camera_stream_controller import CameraStreamController
from desktop_app.state.jetson_state import JetsonState
from desktop_app.ui.widgets import Card, StatusChip
from desktop_app.workers.camera_connection_worker import CameraConnectionWorker
from desktop_app.workers.camera_discovery_worker import CameraDiscoveryWorker
from desktop_app.workers.camera_worker import CameraActionWorker
from desktop_app.workers.camera_preview_receiver import CameraPreviewReceiver
from desktop_app.workers.gstreamer_preview_receiver import (
    GStreamerPreviewReceiver,
    inspect_host_gstreamer,
)
from devices.camera.inventory import CameraTargetSelection
from devices.camera.models import CameraConnectionState
from devices.camera.service import CameraService
from devices.camera.preview_config import (
    H264_PREVIEW_PORT, PREVIEW_HEIGHT, PREVIEW_MODE, PREVIEW_PORT, PREVIEW_WIDTH,
)
from core.testing.definitions import load_definitions
from core.testing.registry import TestRegistry
from devices.camera.testing import register_camera_handlers
from devices.camera.ros_automation import register_ros_camera_handlers
from desktop_app.workers.camera_test_runner_worker import CameraTestRunnerWorker
from desktop_app.workers.ros_camera_test_runner_worker import (
    RosCameraTestRunnerWorker,
)


class CameraPreviewLabel(QLabel):
    def __init__(self, parent=None):
        super().__init__("Preview unavailable", parent)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setMinimumHeight(220)
        self.setStyleSheet("background:#090E17; border:1px solid #30363D; color:#8B949E;")
        self._source = None

    def show_image(self, image):
        self._source = image
        self._render()

    def show_message(self, message):
        self._source = None
        self.clear()
        self.setText(message)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._render()

    def _render(self):
        if self._source is None:
            return
        pixmap = QPixmap.fromImage(self._source)
        self.setPixmap(pixmap.scaled(
            self.size(), Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        ))


class CameraPage(QWidget):
    discovery_requested = Signal(dict)
    connection_requested = Signal(dict)
    disconnection_requested = Signal(dict)
    stream_start_requested = Signal(dict)
    stream_stop_requested = Signal(dict)
    tests_requested = Signal(list)
    shutdown_ready = Signal()

    def __init__(
        self,
        jetson_state: JetsonState,
        jetson_service: JetsonConnectionService,
        parent=None,
        camera_service=None,
        camera_inventory_service=None,
    ):
        super().__init__(parent)
        self.jetson_state = jetson_state
        self.jetson_service = jetson_service
        self.camera_service = camera_service or CameraService()
        self.camera_inventory_service = camera_inventory_service or CameraInventoryService(
            self.jetson_service, parent=self
        )
        self.camera_target_selection = CameraTargetSelection()
        self._selected_inventory_detail_uid = None
        self.connection_state = CameraConnectionState.DISCONNECTED
        self.camera_worker = None
        self.remote_request_id = None
        self.remote_action = None
        self._last_jetson_connected = None
        self._disconnect_after_stop = False
        self._shutdown_pending = False
        self._previous_frame_count = None
        self._unchanged_frame_polls = 0
        self._stall_warning_active = False
        self._low_fps_samples = 0
        self._low_fps_warning_active = False
        self._stable_fps_samples = 0
        self._stable_fps_logged = False
        self.preview_receiver = None
        self.preview_state = "OFF"
        self.preview_frames_received = 0
        self.preview_frames_displayed = 0
        self.preview_frames_dropped = 0
        self._preview_first_frame = False
        self.host_gstreamer = inspect_host_gstreamer()
        self.preview_backend = None
        self._preview_fallback_used = False
        self.test_registry = TestRegistry()
        register_camera_handlers(self.test_registry)
        register_ros_camera_handlers(self.test_registry)
        self.test_definitions = []
        self.test_statuses = {}
        self.test_results = {}
        self.test_runner_worker = None
        self.ros_test_definitions = []
        self.ros_test_statuses = {}
        self.ros_test_results = {}
        self.ros_test_runner_worker = None
        self._selected_ros_test_id = None
        self._inventory_expanded = False
        self._ros_log_entries = []
        self._ros_environment_values = {
            "ROS Distro": "NOT CHECKED",
            "ROS Environment": "NOT CHECKED",
            "zed_wrapper": "NOT CHECKED",
            "realsense2_camera": "NOT CHECKED",
            "Workspace": "NOT CHECKED",
            "Last Check": "NEVER",
        }
        self._ros_environment_dialog = None
        self._test_definition_error = None
        self._ros_test_definition_error = None
        try:
            self.test_definitions = load_definitions(
                "testcases/camera/definitions/phase8_1a.json", self.test_registry
            )
        except Exception as exc:
            self._test_definition_error = str(exc)
        try:
            self.ros_test_definitions = []
            for definition_path in (
                "testcases/camera/definitions/phase8_3a.json",
                "testcases/camera/definitions/phase8_3b.json",
            ):
                self.ros_test_definitions.extend(
                    load_definitions(definition_path, self.test_registry)
                )
        except Exception as exc:
            self._ros_test_definition_error = str(exc)
        self.stream_controller = CameraStreamController(
            self.jetson_service, self.camera_service, self
        )

        self._build_ui()
        self._load_profiles()
        self._load_test_cases()
        self._load_ros_test_cases()
        self._reset_runtime_ui()
        self.jetson_state.state_changed.connect(self._on_jetson_state_changed)
        self.jetson_service.operation_succeeded.connect(
            self._on_remote_operation_succeeded
        )
        self.jetson_service.operation_failed.connect(
            self._on_remote_operation_failed
        )
        self.camera_inventory_service.started.connect(
            self._on_inventory_discovery_started
        )
        self.camera_inventory_service.completed.connect(
            self._on_inventory_discovery_completed
        )
        self.camera_inventory_service.failed.connect(
            self._on_inventory_discovery_failed
        )
        self.camera_inventory_service.cleared.connect(
            self._on_inventory_cleared
        )
        self.stream_controller.started.connect(self._on_stream_started)
        self.stream_controller.stopped.connect(self._on_stream_stopped)
        self.stream_controller.metrics_received.connect(self._update_monitor)
        self.stream_controller.failed.connect(self._on_stream_failed)
        self.stream_controller.preview_fallback_ready.connect(self._start_jpeg_fallback)
        self._on_jetson_state_changed(self.jetson_state)

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 14, 18, 14)
        root.setSpacing(10)

        header = QHBoxLayout()
        title_block = QVBoxLayout()
        title = QLabel("Camera")
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

        subnav = QHBoxLayout()
        self.monitor_tab_button = QPushButton("MONITOR")
        self.tests_tab_button = QPushButton("AUTOMATED TESTS")
        self.ros_automation_tab_button = QPushButton("ROS AUTOMATION")
        for button in (
            self.monitor_tab_button,
            self.tests_tab_button,
            self.ros_automation_tab_button,
        ):
            button.setObjectName("OutlineButton")
            button.setCheckable(True)
            button.setMinimumWidth(145)
            subnav.addWidget(button)
        subnav.addStretch()
        root.addLayout(subnav)

        self.camera_pages = QStackedWidget()
        self.monitor_page = self._build_monitor_page()
        self.automated_tests_page = self._build_automated_tests_page()
        self.ros_automation_page = self._build_ros_automation_page()
        self.camera_pages.addWidget(self.monitor_page)
        self.camera_pages.addWidget(self.automated_tests_page)
        self.camera_pages.addWidget(self.ros_automation_page)
        root.addWidget(self.camera_pages, 1)
        self.monitor_tab_button.clicked.connect(lambda: self._set_camera_subpage(0))
        self.tests_tab_button.clicked.connect(lambda: self._set_camera_subpage(1))
        self.ros_automation_tab_button.clicked.connect(
            lambda: self._set_camera_subpage(2)
        )
        self._set_camera_subpage(0)

    def _build_monitor_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        top_row = QHBoxLayout()
        top_row.setSpacing(10)
        top_row.addWidget(self._build_device_card(), 1)
        top_row.addWidget(self._build_overview_card(), 1)
        layout.addLayout(top_row)

        middle_row = QHBoxLayout()
        middle_row.setSpacing(10)
        middle_row.addWidget(self._build_stream_card(), 1)
        middle_row.addWidget(self._build_monitor_card(), 1)
        middle_row.addWidget(self._build_preview_card(), 2)
        layout.addLayout(middle_row)
        layout.addWidget(self._build_automation_summary_card())
        layout.addWidget(self._build_log_card(), 1)
        return page

    def _build_automated_tests_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        layout.addWidget(self._build_runner_status_card())
        test_card = self._build_test_card()
        layout.addWidget(self._build_test_filter_card())
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(test_card)
        splitter.addWidget(self._build_test_detail_card())
        splitter.setStretchFactor(0, 7)
        splitter.setStretchFactor(1, 3)
        splitter.setSizes([900, 400])
        layout.addWidget(splitter, 5)
        layout.addWidget(self._build_test_control_card())
        layout.addWidget(self._build_test_log_card(), 2)
        return page

    def _build_ros_automation_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.addWidget(self._build_ros_status_card())
        layout.addWidget(self._build_inventory_card())

        self.ros_workspace_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.ros_workspace_splitter.setChildrenCollapsible(False)
        self.ros_workspace_splitter.addWidget(self._build_ros_tests_card())
        self.ros_workspace_splitter.addWidget(self._build_ros_log_card())
        self.ros_workspace_splitter.setStretchFactor(0, 64)
        self.ros_workspace_splitter.setStretchFactor(1, 36)
        self.ros_workspace_splitter.setSizes([820, 460])
        layout.addWidget(self.ros_workspace_splitter, 1)
        layout.addWidget(self._build_ros_run_bar())
        return page

    def _build_ros_status_card(self):
        card = Card()
        row = QHBoxLayout()
        row.setSpacing(8)
        title = QLabel("ROS AUTOMATION")
        title.setObjectName("CardTitle")
        target_label = QLabel("Target Camera")
        target_label.setObjectName("Muted")
        self.target_camera_combo = QComboBox()
        self.target_camera_combo.setMinimumWidth(280)
        self.target_camera_combo.addItem("All Cameras", None)
        self.inventory_discover_button = QPushButton("DISCOVER ALL")
        self.inventory_discover_button.setObjectName("PrimaryButton")
        self.ros_environment_details_button = QPushButton("ENVIRONMENT DETAILS")
        self.ros_environment_status_label = QLabel("ROS NOT CHECKED")
        self.ros_driver_status_label = QLabel("Driver UNKNOWN")
        self.ros_camera_status_label = QLabel("Camera UNKNOWN")
        self.ros_runner_status_label = QLabel("Runner IDLE")
        self.ros_node_status_label = self.ros_camera_status_label
        for label in (
            self.ros_environment_status_label,
            self.ros_driver_status_label,
            self.ros_camera_status_label,
            self.ros_runner_status_label,
        ):
            label.setObjectName("StatusBadge")
            label.setContentsMargins(7, 3, 7, 3)
        row.addWidget(title)
        row.addSpacing(8)
        row.addWidget(target_label)
        row.addWidget(self.target_camera_combo, 1)
        row.addWidget(self.inventory_discover_button)
        row.addWidget(self.ros_environment_details_button)
        row.addStretch()
        for label in (
            self.ros_environment_status_label,
            self.ros_driver_status_label,
            self.ros_camera_status_label,
            self.ros_runner_status_label,
        ):
            row.addWidget(label)
        card.body_layout.addLayout(row)
        self.target_camera_combo.currentIndexChanged.connect(
            self._on_target_camera_changed
        )
        self.inventory_discover_button.clicked.connect(
            self._request_inventory_discovery
        )
        self.ros_environment_details_button.clicked.connect(
            self._show_ros_environment_details
        )
        self.ros_status_card = card
        self.ros_target_card = card
        return card

    def _build_target_camera_card(self):
        return self.ros_status_card

    def _build_inventory_card(self):
        card = Card("Camera Inventory")
        header = QHBoxLayout()
        self.inventory_count_label = QLabel("0 Detected")
        self.inventory_count_label.setObjectName("Muted")
        self.inventory_state_label = QLabel("Not discovered")
        self.inventory_state_label.setObjectName("Muted")
        self.inventory_toggle_button = QPushButton("SHOW DETAILS")
        header.addWidget(self.inventory_count_label)
        header.addWidget(QLabel("|"))
        header.addWidget(self.inventory_state_label)
        header.addStretch()
        header.addWidget(self.inventory_toggle_button)
        card.body_layout.addLayout(header)

        self.inventory_details_widget = QWidget()
        details_layout = QHBoxLayout(self.inventory_details_widget)
        details_layout.setContentsMargins(0, 0, 0, 0)
        details_layout.setSpacing(8)
        details_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.inventory_table = QTableWidget(0, 8)
        self.inventory_table.setHorizontalHeaderLabels(
            ["Model", "Serial", "Vendor", "Transport", "USB", "ROS Driver", "ROS Readiness", "Status"]
        )
        self.inventory_table.verticalHeader().setVisible(False)
        self.inventory_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.inventory_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.inventory_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.inventory_table.setAlternatingRowColors(True)
        self.inventory_table.verticalHeader().setDefaultSectionSize(30)
        self.inventory_table.setMinimumHeight(125)
        inventory_header = self.inventory_table.horizontalHeader()
        inventory_header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in range(1, 8):
            inventory_header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        self.inventory_empty_label = QLabel(
            "Jetson is disconnected. Connect from Dashboard first."
        )
        self.inventory_empty_label.setObjectName("Muted")
        self.inventory_empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        inventory_table_panel = QWidget()
        inventory_table_layout = QVBoxLayout(inventory_table_panel)
        inventory_table_layout.setContentsMargins(0, 0, 0, 0)
        inventory_table_layout.addWidget(self.inventory_empty_label)
        inventory_table_layout.addWidget(self.inventory_table, 1)
        details_splitter.addWidget(inventory_table_panel)
        details_splitter.addWidget(self._build_ros_device_detail_card())
        details_splitter.setStretchFactor(0, 7)
        details_splitter.setStretchFactor(1, 3)
        details_splitter.setSizes([900, 380])
        details_layout.addWidget(details_splitter)
        card.body_layout.addWidget(self.inventory_details_widget)

        self.inventory_toggle_button.clicked.connect(self._toggle_inventory_details)
        self.inventory_table.cellClicked.connect(self._show_inventory_row_details)
        self.ros_inventory_card = card
        self._set_inventory_expanded(False)
        return card

    def _build_ros_device_detail_card(self):
        card = Card("ROS Device Details")
        self.inventory_detail_text = QTextEdit()
        self.inventory_detail_text.setReadOnly(True)
        self.inventory_detail_text.setPlainText(
            "Select a camera to view ROS device details."
        )
        card.body_layout.addWidget(self.inventory_detail_text, 1)
        self.ros_device_detail_card = card
        return card

    def _build_ros_environment_card(self):
        card = Card("ROS Environment")
        card.setVisible(False)
        self.ros_environment_card = card
        return card

    def _build_ros_tests_card(self):
        panel = QWidget()
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(0, 0, 0, 0)
        panel_layout.setSpacing(0)
        self.ros_tests_splitter = QSplitter(Qt.Orientation.Vertical)
        self.ros_tests_splitter.setChildrenCollapsible(False)

        table_card = Card("ROS Automated Tests")
        self.ros_test_table = QTableWidget(0, 6)
        self.ros_test_table.setHorizontalHeaderLabels(
            ["Select", "ID", "Test Name", "Target", "Duration", "Status"]
        )
        self.ros_test_table.verticalHeader().setVisible(False)
        self.ros_test_table.verticalHeader().setDefaultSectionSize(34)
        self.ros_test_table.setAlternatingRowColors(True)
        self.ros_test_table.setMinimumHeight(382)
        self.ros_test_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.ros_test_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        ros_header = self.ros_test_table.horizontalHeader()
        ros_header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        self.ros_test_table.setColumnWidth(0, 45)
        ros_header.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        self.ros_test_table.setColumnWidth(1, 90)
        ros_header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        ros_header.setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
        self.ros_test_table.setColumnWidth(3, 155)
        ros_header.setSectionResizeMode(4, QHeaderView.ResizeMode.Fixed)
        self.ros_test_table.setColumnWidth(4, 90)
        ros_header.setSectionResizeMode(5, QHeaderView.ResizeMode.Fixed)
        self.ros_test_table.setColumnWidth(5, 100)
        self.ros_test_table.cellClicked.connect(self._on_ros_test_row_clicked)
        table_card.body_layout.addWidget(self.ros_test_table, 1)

        detail_card = Card("Selected Test Detail")
        self.ros_test_detail_text = QTextEdit()
        self.ros_test_detail_text.setReadOnly(True)
        self.ros_test_detail_text.setPlainText(
            "Select ROS-001 through ROS-004 to view definition and result details."
        )
        self.ros_view_result_button = QPushButton("VIEW RESULT")
        self.ros_view_result_button.setEnabled(False)
        detail_card.body_layout.addWidget(self.ros_test_detail_text, 1)
        detail_actions = QHBoxLayout()
        detail_actions.addStretch()
        detail_actions.addWidget(self.ros_view_result_button)
        detail_card.body_layout.addLayout(detail_actions)

        self.ros_tests_splitter.addWidget(table_card)
        self.ros_tests_splitter.addWidget(detail_card)
        self.ros_tests_splitter.setStretchFactor(0, 78)
        self.ros_tests_splitter.setStretchFactor(1, 22)
        self.ros_tests_splitter.setSizes([620, 175])
        panel_layout.addWidget(self.ros_tests_splitter)
        self.ros_test_table.itemChanged.connect(self._update_ros_selected_count)
        self.ros_view_result_button.clicked.connect(self._view_selected_ros_result)
        self.ros_tests_card = panel
        return panel

    def _build_ros_log_card(self):
        card = Card("ROS Execution Log")
        controls = QHBoxLayout()
        controls.addWidget(QLabel("Log Filter"))
        self.ros_log_filter_combo = QComboBox()
        self.ros_log_filter_combo.addItems(
            ["ALL", "INFO", "PASS", "WARNING", "ERROR"]
        )
        self.ros_log_clear_button = QPushButton("CLEAR")
        self.ros_log_auto_scroll_check = QCheckBox("AUTO SCROLL")
        self.ros_log_auto_scroll_check.setChecked(True)
        controls.addWidget(self.ros_log_filter_combo)
        controls.addStretch()
        controls.addWidget(self.ros_log_clear_button)
        controls.addWidget(self.ros_log_auto_scroll_check)
        card.body_layout.addLayout(controls)
        self.ros_execution_log = QTextEdit()
        self.ros_execution_log.setObjectName("LiveLog")
        self.ros_execution_log.setReadOnly(True)
        self.ros_execution_log.setMinimumWidth(360)
        card.body_layout.addWidget(self.ros_execution_log, 1)
        self.ros_log_filter_combo.currentTextChanged.connect(
            self._render_ros_log
        )
        self.ros_log_clear_button.clicked.connect(self._clear_ros_log)
        self.ros_log_card = card
        return card

    def _build_ros_run_bar(self):
        card = Card()
        row = QHBoxLayout()
        row.setSpacing(14)
        self.ros_total_label = QLabel("Total 0")
        self.ros_selected_tests_label = QLabel("Selected 0")
        self.ros_pass_label = QLabel("PASS 0")
        self.ros_fail_label = QLabel("FAIL 0")
        self.ros_error_label = QLabel("ERROR 0")
        self.ros_blocked_label = QLabel("BLOCKED 0")
        for label in (
            self.ros_total_label,
            self.ros_selected_tests_label,
            self.ros_pass_label,
            self.ros_fail_label,
            self.ros_error_label,
            self.ros_blocked_label,
        ):
            label.setObjectName("Muted")
            row.addWidget(label)
        row.addStretch()
        self.ros_run_tests_button = QPushButton("▶  RUN SELECTED")
        self.ros_run_tests_button.setObjectName("PrimaryButton")
        self.ros_cancel_tests_button = QPushButton("■  CANCEL")
        self.ros_cancel_tests_button.setObjectName("DangerButton")
        self.ros_cancel_tests_button.setEnabled(False)
        row.addWidget(self.ros_run_tests_button)
        row.addWidget(self.ros_cancel_tests_button)
        card.body_layout.addLayout(row)
        self.ros_run_tests_button.clicked.connect(self._run_selected_ros_tests)
        self.ros_cancel_tests_button.clicked.connect(self._cancel_ros_test_run)
        self.ros_run_bar = card
        return card

    def _toggle_inventory_details(self):
        self._set_inventory_expanded(not self._inventory_expanded)

    def _set_inventory_expanded(self, expanded):
        self._inventory_expanded = bool(expanded)
        if hasattr(self, "inventory_details_widget"):
            self.inventory_details_widget.setVisible(self._inventory_expanded)
        if hasattr(self, "inventory_toggle_button"):
            self.inventory_toggle_button.setText(
                "HIDE DETAILS" if self._inventory_expanded else "SHOW DETAILS"
            )

    def _set_ros_badge(self, label, text, state="idle"):
        colors = {
            "ok": ("#16883F", "#ECFDF3"),
            "warning": ("#B54708", "#FFFAEB"),
            "error": ("#D92D20", "#FEF3F2"),
            "running": ("#155EEF", "#EFF4FF"),
            "idle": ("#667085", "#F2F4F7"),
        }
        foreground, background = colors.get(state, colors["idle"])
        label.setText(text)
        label.setStyleSheet(
            f"color:{foreground}; background:{background};"
            "border-radius:8px; padding:3px 7px; font-weight:600;"
        )

    def _show_ros_environment_details(self):
        dialog = self._ros_environment_dialog
        if dialog is not None and dialog.isVisible():
            dialog.raise_()
            dialog.activateWindow()
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("ROS Environment Details")
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        dialog.resize(680, 330)
        layout = QVBoxLayout(dialog)
        table = QTableWidget(len(self._ros_environment_values), 2)
        table.setHorizontalHeaderLabels(["Parameter", "Value"])
        self._configure_read_only_table(table)
        table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.ResizeToContents
        )
        table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Stretch
        )
        for row, (field, value) in enumerate(self._ros_environment_values.items()):
            table.setItem(row, 0, QTableWidgetItem(field))
            table.setItem(row, 1, QTableWidgetItem(str(value)))
        close = QPushButton("CLOSE")
        close.clicked.connect(dialog.close)
        actions = QHBoxLayout()
        actions.addStretch()
        actions.addWidget(close)
        layout.addWidget(table, 1)
        layout.addLayout(actions)
        self.ros_environment_table = table
        self._ros_environment_dialog = dialog
        dialog.destroyed.connect(self._on_ros_environment_dialog_destroyed)
        dialog.show()

    def _on_ros_environment_dialog_destroyed(self, *_):
        self._ros_environment_dialog = None
        self.ros_environment_table = None

    def _update_open_ros_environment_dialog(self):
        table = getattr(self, "ros_environment_table", None)
        if table is None:
            return
        for row, value in enumerate(self._ros_environment_values.values()):
            item = table.item(row, 1)
            if item is not None:
                item.setText(str(value))

    def _view_selected_ros_result(self):
        test_id = self.ros_view_result_button.property("test_id")
        result = self.ros_test_results.get(test_id)
        if not result:
            return
        dialog = QDialog(self)
        dialog.setWindowTitle(f"ROS Test Result — {test_id}")
        dialog.resize(760, 620)
        layout = QVBoxLayout(dialog)
        text = QTextEdit()
        text.setReadOnly(True)
        sections = []
        for key in (
            "schema_version", "status", "started_at", "finished_at", "duration_s",
            "device",
            "configuration", "measurements", "rule_results",
            "sub_results", "failure_reasons", "error", "cleanup_errors",
        ):
            sections.append(
                f"{key.replace('_', ' ').title()}\n"
                f"{self._readable_result_value(result.get(key))}"
            )
        rendered = "\n\n".join(sections)
        if len(rendered) > 40000:
            rendered = rendered[:40000] + "\n\n… result display truncated"
        text.setPlainText(rendered)
        close = QPushButton("CLOSE")
        close.clicked.connect(dialog.accept)
        layout.addWidget(text, 1)
        layout.addWidget(close)
        dialog.exec()

    def _clear_ros_log(self):
        self._ros_log_entries.clear()
        self.ros_execution_log.clear()

    @staticmethod
    def _ros_log_filter_matches(selected_filter, level):
        if selected_filter == "ALL":
            return True
        if selected_filter == "ERROR":
            return level in {"ERROR", "FAIL"}
        return selected_filter == level

    def _render_ros_log(self, *_):
        if not hasattr(self, "ros_execution_log"):
            return
        selected_filter = self.ros_log_filter_combo.currentText()
        self.ros_execution_log.clear()
        for timestamp, level, message in self._ros_log_entries:
            if self._ros_log_filter_matches(selected_filter, level):
                self._append_log_line(
                    self.ros_execution_log, level, message, timestamp, False
                )
        if self.ros_log_auto_scroll_check.isChecked():
            bar = self.ros_execution_log.verticalScrollBar()
            bar.setValue(bar.maximum())

    def _request_inventory_discovery(self):
        if not self.camera_inventory_service.discover_all():
            if self.camera_inventory_service.busy:
                self._append_ros_log(
                    "WARNING", "ROS Camera inventory discovery is already running."
                )

    def _on_inventory_discovery_started(self):
        self.inventory_discover_button.setEnabled(False)
        self.inventory_discover_button.setText("DISCOVERING...")
        self.inventory_state_label.setText("Discovering...")
        self._append_ros_log("INFO", "ROS Camera inventory discovery started.")

    def _on_inventory_discovery_completed(self, snapshot):
        devices = tuple(snapshot.devices)
        self.inventory_discover_button.setText("DISCOVER ALL")
        self.inventory_discover_button.setEnabled(self.jetson_service.is_connected)
        self.inventory_count_label.setText(f"{len(devices)} Detected")
        if snapshot.adapter_errors:
            inventory_state = "Partial" if devices else "Discovery errors"
        else:
            inventory_state = "Ready" if devices else "No cameras detected"
        self.inventory_state_label.setText(inventory_state)
        self._populate_target_camera_selector(devices)
        self._populate_inventory_table(devices)
        self._sync_inventory_detail_after_refresh(devices)
        self._refresh_ros_table_targets()
        self._update_ros_status(snapshot)
        if not snapshot.adapter_errors:
            self._set_inventory_expanded(False)
        for adapter, error in snapshot.adapter_errors.items():
            self._append_ros_log(
                "WARNING", f"{adapter} discovery unavailable: {error}"
            )
        for adapter, warnings in snapshot.adapter_warnings.items():
            for warning in warnings:
                self._append_ros_log("WARNING", f"{adapter}: {warning}")
        family_counts = {}
        for device in devices:
            family_counts[device.family] = family_counts.get(device.family, 0) + 1
            transport = device.transport.value
            self._append_ros_log(
                "INFO",
                f"{device.model} SN{device.serial or '-'} detected via {transport}.",
            )
            if device.usb_speed is not None:
                self._append_ros_log(
                    "INFO", f"{device.model} actual USB connection: {device.usb_speed.value}."
                )
            if device.ros_driver:
                self._append_ros_log(
                    "INFO", f"ROS mapping: {device.model} -> {device.ros_driver}."
                )
            for warning in device.discovery_errors:
                self._append_ros_log(
                    "WARNING", f"{device.model} SN{device.serial or '-'}: {warning}"
                )
        for family, count in family_counts.items():
            self._append_ros_log(
                "INFO", f"{family} discovery: {count} device(s) found."
            )
        self._append_ros_log(
            "INFO", f"Camera inventory complete: {len(devices)} device(s)."
        )

    def _on_inventory_discovery_failed(self, error):
        self.inventory_discover_button.setText("DISCOVER ALL")
        self.inventory_discover_button.setEnabled(self.jetson_service.is_connected)
        blocked = str(error).startswith("BLOCKED:")
        self.inventory_state_label.setText("BLOCKED" if blocked else "ERROR")
        self._append_ros_log(
            "WARNING" if blocked else "ERROR", f"Camera inventory: {error}"
        )
        self._update_ros_status()

    def _on_inventory_cleared(self):
        if not hasattr(self, "inventory_table"):
            return
        self._populate_inventory_table(())
        self._populate_target_camera_selector(())
        self._refresh_ros_table_targets()
        self.inventory_count_label.setText("0 Detected")
        self.inventory_state_label.setText("BLOCKED — Jetson disconnected")
        self._selected_inventory_detail_uid = None
        self.inventory_detail_text.setPlainText(
            "Select a camera to view ROS device details."
        )
        self._update_ros_status()

    def _populate_inventory_table(self, devices):
        self.inventory_table.setRowCount(len(devices))
        for row, device in enumerate(devices):
            values = (
                device.model,
                device.serial or "-",
                device.vendor,
                device.transport.value,
                device.usb_speed.value if device.usb_speed is not None else "-",
                device.ros_driver or "-",
                device.ros_readiness.value,
                device.physical_status.value,
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, device.device_uid)
                self.inventory_table.setItem(row, column, item)
        self._update_inventory_empty_state(devices)

    def _populate_target_camera_selector(self, devices):
        selected = self.camera_target_selection.retain_after_refresh(devices)
        self.target_camera_combo.blockSignals(True)
        self.target_camera_combo.clear()
        self.target_camera_combo.addItem("All Cameras", None)
        selected_index = 0
        for index, device in enumerate(devices, start=1):
            self.target_camera_combo.addItem(
                f"{device.model} — SN {device.serial or '-'}", device.device_uid
            )
            if device.device_uid == selected:
                selected_index = index
        self.target_camera_combo.setCurrentIndex(selected_index)
        self.target_camera_combo.blockSignals(False)

    def _on_target_camera_changed(self, _index):
        device_uid = self.target_camera_combo.currentData()
        self.camera_target_selection.select(device_uid)
        if device_uid:
            self._selected_inventory_detail_uid = device_uid
            self._select_inventory_row(device_uid)
            self._render_inventory_details(device_uid)
        if self._selected_ros_test_id:
            self._show_ros_test_details(self._selected_ros_test_id)
        self._refresh_ros_table_targets()

    def _show_inventory_row_details(self, row, _column):
        item = self.inventory_table.item(row, 0)
        device_uid = item.data(Qt.ItemDataRole.UserRole) if item else ""
        self._selected_inventory_detail_uid = device_uid or None
        self._render_inventory_details(device_uid)

    def _render_inventory_details(self, device_uid):
        device = self.camera_inventory_service.get_device(device_uid)
        if device is None:
            self.inventory_detail_text.setPlainText(
                "Select a camera to view ROS device details."
            )
            return
        capabilities = ", ".join(
            name for name, available in device.capabilities.items() if available
        ) or "-"
        warnings = "\n".join(f"  {value}" for value in device.discovery_errors) or "  -"
        self.inventory_detail_text.setPlainText(
            f"Device UID: {device.device_uid}\nVendor: {device.vendor}\n"
            f"Model: {device.model}\nSerial: {device.serial or '-'}\n"
            f"Transport: {device.transport.value}\nPhysical Port: {device.physical_port or '-'}\n"
            f"USB Speed: {device.usb_speed.value if device.usb_speed is not None else '-'}\n"
            f"SDK Backend: {device.sdk_backend or '-'}\nROS Driver: {device.ros_driver or '-'}\n"
            f"ROS Camera Model: {device.ros_camera_model or '-'}\n"
            f"Suggested Namespace: {device.ros_namespace_hint or '-'}\n"
            f"Capabilities: {capabilities}\nROS Readiness: {device.ros_readiness.value}\n"
            f"Discovery Warnings:\n{warnings}"
        )

    def _sync_inventory_detail_after_refresh(self, devices):
        available = {device.device_uid for device in devices}
        detail_uid = self._selected_inventory_detail_uid
        if detail_uid not in available:
            detail_uid = self.camera_target_selection.selected_device_uid
        if detail_uid not in available:
            detail_uid = None
        self._selected_inventory_detail_uid = detail_uid
        if detail_uid:
            self._select_inventory_row(detail_uid)
            self._render_inventory_details(detail_uid)
        else:
            self.inventory_table.clearSelection()
            self.inventory_detail_text.setPlainText(
                "Select a camera to view ROS device details."
            )

    def _select_inventory_row(self, device_uid):
        for row in range(self.inventory_table.rowCount()):
            item = self.inventory_table.item(row, 0)
            if item and item.data(Qt.ItemDataRole.UserRole) == device_uid:
                self.inventory_table.selectRow(row)
                return

    def _update_inventory_empty_state(self, devices=None):
        devices = tuple(
            self.camera_inventory_service.get_devices()
            if devices is None else devices
        )
        self.inventory_empty_label.setVisible(not devices)
        if devices:
            return
        if self.jetson_service.is_connected:
            message = "No cameras discovered. Connect Jetson and click DISCOVER ALL."
        else:
            message = "Jetson is disconnected. Connect from Dashboard first."
        self.inventory_empty_label.setText(message)

    @staticmethod
    def _aggregate_driver_readiness(devices):
        statuses = {device.ros_readiness.value for device in devices}
        if not statuses:
            return "UNKNOWN"
        if statuses == {"READY"}:
            return "READY"
        if "DRIVER_MISSING" in statuses:
            return "DRIVER_MISSING"
        if len(statuses) > 1:
            return "PARTIAL"
        return next(iter(statuses))

    @staticmethod
    def _driver_package_status(devices, package):
        matching = [device for device in devices if device.ros_driver == package]
        return CameraPage._aggregate_driver_readiness(matching)

    def _update_ros_status(self, snapshot=None):
        snapshot = snapshot or self.camera_inventory_service.inventory.snapshot
        devices = tuple(snapshot.devices)
        if not self.jetson_service.is_connected:
            environment = "DISCONNECTED"
        elif not snapshot.discovered_at:
            environment = "NOT CHECKED"
        elif snapshot.ros2_available is True:
            environment = "AVAILABLE"
        elif snapshot.ros2_available is False:
            environment = "NOT AVAILABLE"
        else:
            environment = "UNKNOWN"
        driver = self._aggregate_driver_readiness(devices)
        environment_state = (
            "ok" if environment == "AVAILABLE" else
            "error" if environment in {"DISCONNECTED", "NOT AVAILABLE"} else
            "idle"
        )
        driver_state = (
            "ok" if driver == "READY" else
            "error" if driver == "DRIVER_MISSING" else
            "warning" if driver == "PARTIAL" else
            "idle"
        )
        camera_ready = bool(devices) and all(
            device.physical_status.value == "DETECTED" for device in devices
        )
        self._set_ros_badge(
            self.ros_environment_status_label,
            "ROS " + environment,
            environment_state,
        )
        selected_drivers = sorted(
            {device.ros_driver for device in devices if device.ros_driver}
        )
        driver_text = (
            f"{selected_drivers[0]} {driver}"
            if len(selected_drivers) == 1 else f"Drivers {driver}"
        )
        self._set_ros_badge(self.ros_driver_status_label, driver_text, driver_state)
        self._set_ros_badge(
            self.ros_camera_status_label,
            "Camera READY" if camera_ready else "Camera NOT READY",
            "ok" if camera_ready else "idle",
        )
        self._ros_environment_values.update({
            "ROS Environment": environment,
            "zed_wrapper": self._driver_package_status(devices, "zed_wrapper"),
            "realsense2_camera": self._driver_package_status(
                devices, "realsense2_camera"
            ),
            "Last Check": snapshot.discovered_at or "NEVER",
        })
        self._update_open_ros_environment_dialog()

    def _set_camera_subpage(self, index):
        self.camera_pages.setCurrentIndex(index)
        self.monitor_tab_button.setChecked(index == 0)
        self.tests_tab_button.setChecked(index == 1)
        self.ros_automation_tab_button.setChecked(index == 2)
        self._refresh_runner_status()

    def _build_automation_summary_card(self):
        card = Card("Automated Tests")
        row = QHBoxLayout()
        self.automation_runner_label = QLabel("Runner: IDLE")
        row.addWidget(self.automation_runner_label)
        self.automation_summary_label = QLabel("Total 0  |  PASS 0  |  FAIL 0  |  ERROR 0  |  BLOCKED 0  |  NOT RUN 0")
        self.automation_summary_label.setObjectName("Muted")
        row.addWidget(self.automation_summary_label)
        row.addStretch()
        open_button = QPushButton("OPEN TEST MANAGER")
        open_button.setObjectName("OutlineButton")
        open_button.clicked.connect(lambda: self._set_camera_subpage(1))
        self.open_test_manager_button = open_button
        row.addWidget(open_button)
        card.body_layout.addLayout(row)
        return card

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
        self.jetson_status_label = QLabel("Not connected")
        self.jetson_status_label.setObjectName("Muted")
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
        self.jetson_target_title = QLabel("Jetson Target:")
        grid.addWidget(self.jetson_target_title, len(fields), 0)
        grid.addLayout(jetson_row, len(fields), 1)
        self.jetson_status_title = QLabel("Status:")
        grid.addWidget(self.jetson_status_title, len(fields) + 1, 0)
        grid.addWidget(self.jetson_status_label, len(fields) + 1, 1)
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
        self.monitor_table = QTableWidget(10, 3)
        self.monitor_table.setHorizontalHeaderLabels(["Parameter", "Value", "Unit"])
        self._configure_read_only_table(self.monitor_table)
        header = self.monitor_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        rows = (
            ("Configured FPS", "--", "fps"),
            ("Actual FPS", "--", "fps"),
            ("Frame Interval", "--", "ms"),
            ("Frame Counter", "0", "frames"),
            ("Dropped Frames (Estimated)", "0", "frames"),
            ("Stream Duration", "00:00:00", ""),
            ("Timestamp", "--", ""),
            ("Exposure", "--", ""),
            ("Gain", "--", ""),
            ("Temperature", "--", "°C"),
        )
        for row, values in enumerate(rows):
            for column, value in enumerate(values):
                self.monitor_table.setItem(row, column, QTableWidgetItem(value))
        card.body_layout.addWidget(self.monitor_table)
        return card

    def _build_test_card(self):
        card = Card("Test Case Table")
        card.setMinimumWidth(600)
        self.selected_tests_label = QLabel("0 / 0 Selected")
        self.selected_tests_label.setStyleSheet("color:#155EEF; font-weight:700;")

        self.test_table = QTableWidget(0, 6)
        self.test_table.setHorizontalHeaderLabels(
            ["Select", "Test Case ID", "Test Name", "Category", "Estimated Time", "Status"]
        )
        self.test_table.verticalHeader().setVisible(False)
        self.test_table.setAlternatingRowColors(True)
        self.test_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.test_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.test_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.test_table.verticalHeader().setDefaultSectionSize(34)
        self.test_table.setMinimumHeight(280)
        table_header = self.test_table.horizontalHeader()
        table_header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        self.test_table.setColumnWidth(0, 52)
        for column in (1, 3, 4, 5):
            table_header.setSectionResizeMode(column, QHeaderView.ResizeMode.Interactive)
        self.test_table.setColumnWidth(1, 105)
        self.test_table.setColumnWidth(3, 165)
        self.test_table.setColumnWidth(4, 110)
        self.test_table.setColumnWidth(5, 110)
        table_header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.test_table.itemChanged.connect(self._update_selected_test_count)
        self.test_table.cellClicked.connect(self._on_test_row_clicked)
        card.body_layout.addWidget(self.test_table)
        return card

    def _build_runner_status_card(self):
        card = Card("Test Runner")
        row = QHBoxLayout()
        self.runner_state_label = QLabel("● IDLE")
        self.runner_jetson_label = QLabel("Jetson: DISCONNECTED")
        self.runner_camera_label = QLabel("Camera: NOT READY")
        self.runner_device_label = QLabel("Device: --")
        self.runner_serial_label = QLabel("SN: --")
        for label in (self.runner_state_label, self.runner_jetson_label, self.runner_camera_label,
                      self.runner_device_label, self.runner_serial_label):
            row.addWidget(label)
        row.addStretch()
        card.body_layout.addLayout(row)
        return card

    def _build_test_filter_card(self):
        card = Card()
        row = QHBoxLayout()
        self.test_search = QLineEdit()
        self.test_search.setPlaceholderText("Search by Test ID or name...")
        self.test_search.textChanged.connect(self._apply_test_filters)
        self.test_category_filter = QComboBox()
        self.test_category_filter.addItem("All")
        self.test_category_filter.addItems(sorted({item.group for item in self.test_definitions}))
        self.test_category_filter.currentTextChanged.connect(self._apply_test_filters)
        self.test_status_filter = QComboBox()
        self.test_status_filter.addItems(("All", "NOT RUN", "RUNNING", "PASS", "FAIL", "ERROR", "BLOCKED", "CANCELLED"))
        self.test_status_filter.currentTextChanged.connect(self._apply_test_filters)
        self.select_all_tests_button = QPushButton("Select All")
        self.select_all_tests_button.setObjectName("SmallButton")
        self.select_all_tests_button.clicked.connect(self._select_all_visible_tests)
        self.clear_test_selection_button = QPushButton("Clear Selection")
        self.clear_test_selection_button.setObjectName("SmallButton")
        self.clear_test_selection_button.clicked.connect(self._clear_test_selection)
        row.addWidget(self.test_search, 2)
        row.addWidget(QLabel("Category:"))
        row.addWidget(self.test_category_filter)
        row.addWidget(QLabel("Status:"))
        row.addWidget(self.test_status_filter)
        row.addWidget(self.select_all_tests_button)
        row.addWidget(self.clear_test_selection_button)
        row.addWidget(self.selected_tests_label)
        card.body_layout.addLayout(row)
        return card

    def _build_test_detail_card(self):
        card = Card("Test Details")
        card.setMinimumWidth(300)
        self.test_detail_text = QTextEdit()
        self.test_detail_text.setReadOnly(True)
        self.test_detail_text.setText("Select a test case to view details.")
        self.view_result_button = QPushButton("VIEW RESULT")
        self.view_result_button.setObjectName("OutlineButton")
        self.view_result_button.setEnabled(False)
        self.view_result_button.clicked.connect(self._view_selected_test_result)
        card.body_layout.addWidget(self.test_detail_text, 1)
        card.body_layout.addWidget(self.view_result_button)
        return card

    def _build_test_control_card(self):
        card = Card()
        row = QHBoxLayout()
        self.test_run_summary_label = QLabel("Total 0 | Selected 0 | PASS 0 | FAIL 0 | ERROR 0 | BLOCKED 0")
        row.addWidget(self.test_run_summary_label)
        row.addStretch()
        self.run_tests_button = QPushButton("▶  RUN SELECTED TESTS")
        self.run_tests_button.setObjectName("PrimaryButton")
        self.run_tests_button.clicked.connect(self._run_selected_tests)
        self.cancel_tests_button = QPushButton("■  CANCEL TEST RUN")
        self.cancel_tests_button.setObjectName("DangerButton")
        self.cancel_tests_button.setEnabled(False)
        self.cancel_tests_button.clicked.connect(self._cancel_test_run)
        row.addWidget(self.run_tests_button)
        row.addWidget(self.cancel_tests_button)
        card.body_layout.addLayout(row)
        return card

    def _build_test_log_card(self):
        card = Card("Test Execution Log")
        self.test_execution_log = QTextEdit()
        self.test_execution_log.setObjectName("LiveLog")
        self.test_execution_log.setReadOnly(True)
        self.test_execution_log.setMinimumHeight(120)
        card.body_layout.addWidget(self.test_execution_log)
        return card

    def _build_preview_card(self):
        card = Card("Live Preview")
        self.preview_label = CameraPreviewLabel()
        card.body_layout.addWidget(self.preview_label)
        footer = QHBoxLayout()
        self.preview_state_label = QLabel("OFF")
        self.preview_state_label.setObjectName("Muted")
        self.preview_resolution_label = QLabel(f"{PREVIEW_WIDTH}x{PREVIEW_HEIGHT}")
        self.preview_resolution_label.setObjectName("Muted")
        self.preview_fps_label = QLabel("Preview FPS: --")
        self.preview_fps_label.setObjectName("Muted")
        footer.addWidget(self.preview_state_label)
        footer.addStretch()
        footer.addWidget(self.preview_resolution_label)
        footer.addWidget(self.preview_fps_label)
        card.body_layout.addLayout(footer)
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
        self.jetson_target_title.setVisible(is_jetson)
        self.jetson_target_label.setVisible(is_jetson)
        self.jetson_status_title.setVisible(is_jetson)
        self.jetson_status_label.setVisible(is_jetson)
        if is_jetson:
            self._render_jetson_state()
            if not self.jetson_service.is_connected:
                self.append_log(
                    "WARNING", "Connect Jetson from Dashboard first."
                )

    def _on_jetson_state_changed(self, _state):
        was_connected = self._last_jetson_connected
        self._render_jetson_state()
        is_connected = self.jetson_service.is_connected
        self._last_jetson_connected = is_connected
        if (
            was_connected is True
            and not is_connected
            and self.execution_host_combo.currentText() == "Jetson"
        ):
            if self.connection_state == CameraConnectionState.STREAMING:
                self.stream_controller.timer.stop()
                self._stop_preview("LOST", "Preview unavailable")
                self._set_state(CameraConnectionState.ERROR)
                self.append_log("ERROR", "Jetson disconnected while camera streaming; remote stream status is unknown.")
            self.append_log(
                "WARNING", "Connect Jetson from Dashboard first."
            )
        self._refresh_runner_status()
        if hasattr(self, "inventory_discover_button"):
            self.inventory_discover_button.setEnabled(
                self.jetson_service.is_connected
                and not self.camera_inventory_service.busy
            )
            if (
                not self.jetson_service.is_connected
                and not self.camera_inventory_service.get_devices()
            ):
                self.inventory_state_label.setText(
                    "BLOCKED — Jetson disconnected"
                )
            elif (
                self.jetson_service.is_connected
                and not self.camera_inventory_service.get_devices()
                and not self.camera_inventory_service.busy
            ):
                self.inventory_state_label.setText("Not discovered")
            self._update_inventory_empty_state()
            self._update_ros_status()
        if hasattr(self, "run_tests_button"):
            self._update_selected_test_count()

    def _render_jetson_state(self):
        if self.jetson_service.is_connected:
            self.jetson_target_label.setText(
                f"{self.jetson_state.username}@{self.jetson_state.host}:"
                f"{self.jetson_state.port}"
            )
            self.jetson_status_label.setText("Connected via Dashboard")
        else:
            self.jetson_target_label.setText("Not connected")
            self.jetson_status_label.setText("Not connected")

    def _require_jetson_connection(self, action: str) -> bool:
        if self.jetson_service.is_connected:
            return True
        self.append_log(
            "WARNING", f"Cannot {action}: Jetson is not connected."
        )
        self.append_log("WARNING", "Connect Jetson from Dashboard first.")
        return False

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
        profile = self.camera_service.profile(self.model_combo.currentData())
        stream = next(
            (item for item in profile.stream_profiles if item.key == self.resolution_combo.currentData()),
            None,
        )
        return {
            "profile_id": self.model_combo.currentData(),
            "execution_host": self.execution_host_combo.currentText(),
            "device_id": self.device_combo.currentData(),
            "resolution_key": self.resolution_combo.currentData(),
            "resolution": stream.resolution if stream else self.resolution_combo.currentText(),
            "fps": int(self.fps_combo.currentText() or 0),
            "pixel_format": self.format_combo.currentText(),
            "preview_mode": PREVIEW_MODE,
            "host_gstreamer_available": self.host_gstreamer.get("available", False),
            "host_h264_decoder": self.host_gstreamer.get("decoder"),
        }

    def _request_action(self, action):
        if (
            (self.camera_worker and self.camera_worker.isRunning())
            or self.remote_request_id is not None
            or self.stream_controller.pending_request_id is not None
        ):
            self.append_log("WARNING", "A camera operation is already running.")
            return

        remote = self.execution_host_combo.currentText() == "Jetson"
        if remote and action == "start_stream":
            if self.connection_state != CameraConnectionState.CONNECTED:
                self.append_log("WARNING", "Start Stream requires a connected camera.")
                return
            payload = self._action_payload()
            if not payload.get("device_id"):
                self.append_log("ERROR", "Start Stream failed: select a camera serial number.")
                return
            if not self._require_jetson_connection("start stream"):
                return
            self.stream_start_requested.emit(payload)
            self.append_log("INFO", "Start Stream requested")
            self.append_log("INFO", f"Camera SN={payload['device_id']}")
            self.append_log("INFO", f"Configuration: {payload['resolution_key']} {payload['resolution']} @ {payload['fps']} FPS")
            self.append_log("INFO", "Starting remote CameraOne stream worker" if payload["profile_id"].startswith("zed_x_one") else "Starting remote stereo ZED stream worker")
            self.append_log("INFO", f"Preview mode: {PREVIEW_MODE}.")
            self.append_log("INFO", "Checking GStreamer capabilities.")
            if self.host_gstreamer.get("available"):
                self.append_log("INFO", f"Host GStreamer {self.host_gstreamer.get('version')} with decoder {self.host_gstreamer.get('decoder')}.")
            else:
                self.append_log("WARNING", f"GStreamer H.264 Preview unavailable on Host: {self.host_gstreamer.get('error')}.")
                self.append_log("INFO", "Falling back to JPEG/TCP Preview.")
            self._reset_stream_metrics(payload["fps"])
            self._set_actions_enabled(False)
            self.stream_controller.start(payload)
            return
        if remote and action == "stop_stream":
            if self.connection_state != CameraConnectionState.STREAMING:
                return
            self.stream_stop_requested.emit(self._action_payload())
            self.append_log("INFO", "Stop Stream requested")
            self.append_log("INFO", "Waiting for remote stream worker to stop")
            self._stop_preview("STOPPING", "Preview stopped")
            self._set_actions_enabled(False)
            self.stream_controller.stop()
            return
        if remote and action == "disconnect" and self.connection_state == CameraConnectionState.STREAMING:
            self._disconnect_after_stop = True
            self.append_log("INFO", "Disconnect requested; stopping camera stream first.")
            self._stop_preview("STOPPING", "Preview stopped")
            self._set_actions_enabled(False)
            self.stream_controller.stop()
            return

        action_text = action.replace("_", " ")
        if remote and not self._require_jetson_connection(action_text):
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
        if remote:
            self.append_log(
                "INFO",
                f"Executing on shared Jetson connection {self.jetson_state.host}.",
            )
        self._set_actions_enabled(False)
        if remote:
            self.remote_action = action
            self.remote_request_id = self.jetson_service.submit_operation(
                f"camera_{action}",
                lambda ssh: self.camera_service.execute_with_ssh(
                    ssh, action, payload
                ),
            )
            if self.remote_request_id is None:
                self.remote_action = None
                self._set_actions_enabled(True)
            return
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

    def _on_remote_operation_succeeded(self, request_id, result):
        if request_id != self.remote_request_id:
            return
        action = self.remote_action
        self.remote_request_id = None
        self.remote_action = None
        self._set_actions_enabled(True)
        if action == "discover":
            self._on_discovery_succeeded(result)
        else:
            self._on_action_succeeded(action, result or {})

    def _on_remote_operation_failed(self, request_id, error):
        if request_id != self.remote_request_id:
            return
        action = self.remote_action
        self.remote_request_id = None
        self.remote_action = None
        self._set_actions_enabled(True)
        self._on_action_failed(action, error)

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
        if action in ("start_stream", "stop_stream"):
            self._set_state(CameraConnectionState.CONNECTED)
        elif action == "disconnect" and self.camera_service.is_connected("zed"):
            self._set_state(CameraConnectionState.CONNECTED)
        else:
            self._set_state(CameraConnectionState.DISCONNECTED)
        if action == "stream_status":
            self.append_log("ERROR", f"Stream metrics unavailable: {error}")
            self.append_log("INFO", "Stream metrics monitoring stopped.")
        else:
            self.append_log("ERROR", f"{action.replace('_', ' ').title()} failed: {error}")
        if action == "connect":
            self.append_log("ERROR", f"Failed to open {self.model_combo.currentText()}")
            self.append_log(
                "ERROR", f"Configuration: {self.resolution_combo.currentText()} "
                f"@ {self.fps_combo.currentText()} FPS",
            )
            self.append_log("ERROR", f"SDK error: {error}")

    def _on_stream_started(self, result):
        self._set_actions_enabled(True)
        self._set_state(CameraConnectionState.STREAMING)
        self.append_log("INFO", f"Remote stream PID: {result.get('pid', '-')}")
        self.append_log("INFO", "Camera opened successfully on Jetson")
        self.append_log("INFO", "Stream started")
        self.append_log("INFO", "Stream metrics monitoring started.")
        self.append_log("INFO", f"Configured stream: {self.resolution_combo.currentData()} @ {self.fps_combo.currentText()} FPS.")
        self._update_monitor(result.get("status", {}))
        self._start_preview(result.get("status", {}))

    def _on_stream_stopped(self, result):
        self._set_actions_enabled(True)
        self._set_state(CameraConnectionState.CONNECTED)
        if result.get("sigterm_used"):
            self.append_log("WARNING", "Graceful stop timed out; SIGTERM fallback was used.")
        self.append_log("INFO", "Camera closed safely")
        self.append_log("INFO", "Stream stopped")
        self.append_log("INFO", "Stream metrics monitoring stopped.")
        self._stop_preview("OFF", "Preview stopped")
        if self._shutdown_pending:
            self._shutdown_pending = False
            self.shutdown_ready.emit()
            return
        if self._disconnect_after_stop:
            self._disconnect_after_stop = False
            self._request_action("disconnect")

    def _on_stream_failed(self, action, error):
        if action == "preview_fallback":
            self.append_log("WARNING", f"JPEG Preview fallback request failed: {error}")
            self.append_log("WARNING", "Camera stream remains active without Live Preview.")
            return
        self._stop_preview("LOST", "Preview unavailable")
        self._set_actions_enabled(True)
        if action == "stop_stream":
            self._set_state(CameraConnectionState.STREAMING)
        elif self.jetson_service.is_connected:
            self._set_state(CameraConnectionState.CONNECTED)
        else:
            self._set_state(CameraConnectionState.ERROR)
        self.append_log("ERROR", f"{action.replace('_', ' ').title()} failed: {error}")
        if self._shutdown_pending:
            self._shutdown_pending = False
            self.shutdown_ready.emit()

    def _update_monitor(self, status):
        values = (
            status.get("configured_fps"), self._decimal_metric(status.get("actual_fps")),
            self._decimal_metric(status.get("frame_interval_ms")), status.get("frame_count", 0),
            status.get("dropped_frames", 0), self._format_duration(status.get("stream_duration_s")),
            status.get("timestamp"), status.get("exposure"), status.get("gain"),
            status.get("temperature"),
        )
        for row, value in enumerate(values):
            if value is None or value == "": value = "--"
            self.monitor_table.item(row, 1).setText(str(value))
        if self.connection_state == CameraConnectionState.STREAMING:
            self._check_stream_health(status)

    def _reset_stream_metrics(self, configured_fps):
        values = (configured_fps, "--", "--", 0, 0, "00:00:00", "--", "--", "--", "--")
        for row, value in enumerate(values):
            self.monitor_table.item(row, 1).setText(str(value))
        self._previous_frame_count = None
        self._unchanged_frame_polls = 0
        self._stall_warning_active = False
        self._low_fps_samples = 0
        self._low_fps_warning_active = False
        self._stable_fps_samples = 0
        self._stable_fps_logged = False

    @staticmethod
    def _decimal_metric(value):
        try:
            return f"{float(value):.2f}"
        except (TypeError, ValueError):
            return "--"

    @staticmethod
    def _format_duration(seconds):
        try:
            total = max(0, int(float(seconds)))
        except (TypeError, ValueError):
            total = 0
        hours, remainder = divmod(total, 3600)
        minutes, seconds = divmod(remainder, 60)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"

    def _check_stream_health(self, status):
        frame_count = status.get("frame_count")
        if frame_count == self._previous_frame_count:
            self._unchanged_frame_polls += 1
        else:
            self._unchanged_frame_polls = 0
            self._stall_warning_active = False
        self._previous_frame_count = frame_count
        if self._unchanged_frame_polls >= 6 and not self._stall_warning_active:
            self.append_log("WARNING", "Stream appears stalled: frame counter has not changed for 3 seconds.")
            self._stall_warning_active = True

        actual, configured = status.get("actual_fps"), status.get("configured_fps")
        if isinstance(actual, (int, float)) and isinstance(configured, (int, float)) and configured > 0:
            if actual < configured * 0.9:
                self._low_fps_samples += 1
                self._stable_fps_samples = 0
            else:
                self._low_fps_samples = 0
                self._low_fps_warning_active = False
                self._stable_fps_samples += 1
            if self._low_fps_samples >= 3 and not self._low_fps_warning_active:
                self.append_log("WARNING", f"Stream FPS below expected range: {actual:.1f} / {configured} FPS.")
                self._low_fps_warning_active = True
            if self._stable_fps_samples >= 3 and not self._stable_fps_logged:
                self.append_log("INFO", f"Stream acquisition stable: Actual FPS {actual:.1f}.")
                self._stable_fps_logged = True

    def _start_preview(self, status):
        self._stop_preview("OFF", "Starting preview...")
        self.preview_state = "STARTING"
        self.preview_state_label.setText("STARTING")
        self.preview_frames_received = 0
        self.preview_frames_displayed = 0
        self.preview_frames_dropped = 0
        self._preview_first_frame = False
        backend = status.get("preview_backend", "jpeg_tcp")
        self.preview_backend = backend
        self._preview_fallback_used = backend == "jpeg_tcp" and PREVIEW_MODE != "JPEG_TCP"
        self.append_log("INFO", "Live Preview initialization requested.")
        self.append_log("INFO", f"Preview configuration: {PREVIEW_WIDTH}x{PREVIEW_HEIGHT} @ target {status.get('preview_target_fps', '--')} FPS.")
        if backend == "gstreamer_h264":
            port = int(status.get("h264_preview_port") or H264_PREVIEW_PORT)
            decoder = self.host_gstreamer.get("decoder")
            self.append_log("INFO", "GStreamer H.264 Preview supported.")
            self.append_log("INFO", f"H.264 encoder: {status.get('preview_encoder', '--')}.")
            self.append_log("INFO", f"Starting Host H.264 receiver on UDP port {port}.")
            receiver = GStreamerPreviewReceiver(port, decoder, self)
        else:
            port = int(status.get("preview_port") or PREVIEW_PORT)
            reason = status.get("preview_error")
            if reason:
                self.append_log("WARNING", f"GStreamer H.264 Preview unavailable: {reason}.")
            if PREVIEW_MODE != "JPEG_TCP": self.append_log("INFO", "Falling back to JPEG/TCP Preview.")
            self.append_log("INFO", f"Jetson JPEG preview server: {self.jetson_state.host}:{port} (worker binds 0.0.0.0).")
            receiver = CameraPreviewReceiver(self.jetson_state.host, port, self)
        receiver.connected.connect(self._on_preview_connected)
        receiver.frame_received.connect(self._on_preview_frame)
        receiver.failed.connect(self._on_preview_failed)
        self.preview_receiver = receiver
        receiver.start()

    def _on_preview_connected(self):
        codec = "H.264" if self.preview_backend == "gstreamer_h264" else "JPEG"
        self.append_log("INFO", f"{codec} Preview receiver connected.")

    def _on_preview_frame(self):
        receiver = self.preview_receiver
        if receiver is None:
            return
        frame = receiver.take_latest_frame()
        if frame is None:
            return
        image, fps, jpeg_size, received = frame
        self.preview_frames_received = received
        self.preview_frames_displayed += 1
        self.preview_frames_dropped = max(0, received - self.preview_frames_displayed)
        self.preview_label.show_image(image)
        self.preview_fps_label.setText(f"Preview FPS: {fps:.2f}")
        if not self._preview_first_frame:
            self._preview_first_frame = True
            self.preview_state = "LIVE"
            codec = "H.264" if self.preview_backend == "gstreamer_h264" else "JPEG"
            self.preview_state_label.setText(f"LIVE | {codec}")
            self.append_log("INFO", f"First {codec} Preview frame received.")
            self.append_log("INFO", f"Live Preview started using {codec}.")

    def _on_preview_failed(self, error):
        if self.preview_state in ("STOPPING", "OFF"):
            return
        self.preview_state = "LOST"
        self.preview_state_label.setText("LOST")
        self.preview_label.show_message("Preview unavailable")
        self.preview_fps_label.setText("Preview FPS: --")
        self.append_log("WARNING", f"Preview connection lost: {error}")
        if self.preview_backend == "gstreamer_h264" and not self._preview_fallback_used:
            self._preview_fallback_used = True
            self.append_log("INFO", "Requesting clean JPEG/TCP fallback on Jetson.")
            self.stream_controller.request_preview_fallback()
            return
        self.append_log("WARNING", "Camera stream is active but Live Preview is unavailable.")

    def _start_jpeg_fallback(self):
        if self.connection_state != CameraConnectionState.STREAMING:
            return
        self._stop_preview("FALLBACK", "Starting JPEG fallback...")
        self.preview_backend = "jpeg_tcp"
        self.preview_state = "STARTING"
        self.preview_state_label.setText("FALLBACK")
        self.append_log("INFO", "Falling back to JPEG/TCP Preview.")
        receiver = CameraPreviewReceiver(self.jetson_state.host, PREVIEW_PORT, self)
        receiver.connected.connect(self._on_preview_connected)
        receiver.frame_received.connect(self._on_preview_frame)
        receiver.failed.connect(self._on_preview_failed)
        self.preview_receiver = receiver
        receiver.start()

    def _stop_preview(self, state="OFF", message="Preview unavailable"):
        receiver = self.preview_receiver
        self.preview_receiver = None
        was_running = receiver is not None and receiver.isRunning()
        if was_running:
            self.append_log("INFO", "Stopping Live Preview.")
            receiver.stop()
            if not receiver.wait(1500):
                self.append_log("WARNING", "Preview receiver did not stop within timeout.")
            else:
                self.append_log("INFO", "Preview receiver stopped.")
        self.preview_state = state
        self.preview_state_label.setText(state)
        self.preview_label.show_message(message)
        self.preview_fps_label.setText("Preview FPS: --")

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
        self._refresh_runner_status()
        if hasattr(self, "run_tests_button"):
            self._update_selected_test_count()

    def _update_button_states(self):
        connected = self.connection_state in (
            CameraConnectionState.CONNECTED,
            CameraConnectionState.STREAMING,
        )
        streaming = self.connection_state == CameraConnectionState.STREAMING
        self.disconnect_button.setEnabled(connected)
        self.connect_button.setEnabled(not connected)
        self.discover_button.setEnabled(not connected)
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
        self.test_table.setRowCount(len(self.test_definitions))
        for row, definition in enumerate(self.test_definitions):
            self.test_statuses.setdefault(definition.test_id, "NOT RUN")
            select_item = QTableWidgetItem()
            select_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable)
            select_item.setCheckState(Qt.CheckState.Unchecked)
            self.test_table.setItem(row, 0, select_item)
            values = (
                definition.test_id, definition.name, definition.group,
                f"{int(definition.timeout_s)} s",
            )
            for column, value in enumerate(values, start=1):
                self.test_table.setItem(row, column, QTableWidgetItem(value))
            self.test_table.setItem(row, 5, QTableWidgetItem(self.test_statuses[definition.test_id]))
        self.test_table.blockSignals(False)
        self._update_selected_test_count()
        if self._test_definition_error:
            self.append_log("ERROR", f"Camera test definition configuration error: {self._test_definition_error}")
        self._refresh_test_summaries()
        self._apply_test_filters()

    def _load_ros_test_cases(self):
        self.ros_test_table.blockSignals(True)
        self.ros_test_table.setRowCount(len(self.ros_test_definitions))
        for row, definition in enumerate(self.ros_test_definitions):
            self.ros_test_statuses.setdefault(definition.test_id, "NOT RUN")
            select_item = QTableWidgetItem()
            select_item.setFlags(
                Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable
            )
            select_item.setCheckState(Qt.CheckState.Unchecked)
            self.ros_test_table.setItem(row, 0, select_item)
            for column, value in enumerate(
                (
                    definition.test_id,
                    definition.name,
                    self._current_ros_target_label(),
                    f"{int(definition.timeout_s)} s",
                    self.ros_test_statuses[definition.test_id],
                ),
                start=1,
            ):
                self.ros_test_table.setItem(row, column, QTableWidgetItem(value))
        self.ros_test_table.blockSignals(False)
        if self._ros_test_definition_error:
            self._append_ros_log(
                "ERROR",
                "ROS test definition configuration error: "
                + self._ros_test_definition_error,
            )
        self._update_ros_selected_count()

    def _current_ros_target_label(self):
        if self.camera_target_selection.selected_device_uid is None:
            return "All Cameras"
        devices = self.camera_target_selection.selected_devices(
            self.camera_inventory_service.get_devices()
        )
        if not devices:
            return "Unavailable"
        device = devices[0]
        return f"{device.model} / {device.serial or '-'}"

    def _refresh_ros_table_targets(self):
        if not hasattr(self, "ros_test_table"):
            return
        target = self._current_ros_target_label()
        for row in range(self.ros_test_table.rowCount()):
            item = self.ros_test_table.item(row, 3)
            if item is not None:
                item.setText(target)

    def _update_ros_selected_count(self, *_):
        selected = sum(
            self.ros_test_table.item(row, 0).checkState()
            == Qt.CheckState.Checked
            for row in range(self.ros_test_table.rowCount())
        )
        self.ros_selected_tests_label.setText(f"Selected {selected}")
        self._refresh_ros_test_summary()
        self._update_ros_test_controls()

    def _on_ros_test_row_clicked(self, row, _column):
        item = self.ros_test_table.item(row, 1)
        if item:
            self._selected_ros_test_id = item.text()
            self._show_ros_test_details(item.text())

    def _show_ros_test_details(self, test_id):
        definition = next(
            (item for item in self.ros_test_definitions if item.test_id == test_id),
            None,
        )
        if definition is None:
            return
        selected = self.camera_target_selection.selected_devices(
            self.camera_inventory_service.get_devices()
        )
        if self.camera_target_selection.selected_device_uid is None:
            target = "All Cameras"
        elif selected:
            target = f"{selected[0].model} — SN{selected[0].serial or '-'}"
        else:
            target = "Selected camera unavailable"
        drivers = ", ".join(
            sorted({device.ros_driver or "UNSUPPORTED" for device in selected})
        ) or "--"
        parameters = "  |  ".join(
            f"{key.replace('_', ' ').title()}: {self._format_detail_value(value)}"
            for key, value in definition.parameters.items()
        ) or "--"
        result = self.ros_test_results.get(test_id)
        latest = self.ros_test_statuses.get(test_id, "NOT RUN")
        measurement_lines = []
        rule_lines = []
        if result:
            measurements = result.get("measurements") or {}
            for key in (
                "ros_distro",
                "all_required_drivers_installed",
                "all_devices_pass",
                "selected_camera_count",
            ):
                if key in measurements:
                    measurement_lines.append(
                        f"{key.replace('_', ' ').title()}: "
                        f"{self._format_detail_value(measurements[key])}"
                    )
            for sub_result in result.get("sub_results") or ():
                device_measurements = sub_result.get("measurements") or {}
                prefix = f"SN{sub_result.get('serial', '-')}"
                summary = []
                if test_id == "ROS-005":
                    image = device_measurements.get("image") or {}
                    info = device_measurements.get("camera_info") or {}
                    summary.extend((
                        f"image={image.get('width')}x{image.get('height')}",
                        f"CameraInfo={info.get('width')}x{info.get('height')}",
                        f"distortion={info.get('distortion_model') or '--'}",
                        f"fx/fy={device_measurements.get('fx')}/{device_measurements.get('fy')}",
                        f"finite={device_measurements.get('finite_values')}",
                        f"frame relationship={device_measurements.get('frame_relationship_valid')}",
                    ))
                elif test_id == "ROS-006":
                    topics = device_measurements.get("sensor_topics") or []
                    summary.append("sensors=" + ", ".join(
                        f"{item.get('capability')}:{item.get('availability_status')}"
                        f"@{item.get('measured_rate_hz', 0)}Hz"
                        for item in topics
                    ))
                    summary.extend((
                        f"temperature={device_measurements.get('temperature_availability')}",
                        f"rollbacks={device_measurements.get('mandatory_timestamp_rollback_count')}",
                        f"non-finite={device_measurements.get('mandatory_non_finite_value_count')}",
                    ))
                elif test_id == "ROS-007":
                    summary.extend(
                        f"{item.get('topic')}: compatible={item.get('compatible_result')}, "
                        f"negative={item.get('negative_actual_result')}"
                        for item in device_measurements.get("qos_matrix") or []
                    )
                elif test_id == "ROS-008":
                    summary.extend((
                        f"record duration={device_measurements.get('record_duration_s')}s",
                        f"bag size={device_measurements.get('bag_size_bytes')} bytes",
                        f"topics={len(device_measurements.get('recorded_topics') or [])}",
                        f"metadata={device_measurements.get('metadata_valid')}",
                        f"replay={device_measurements.get('mandatory_replay_messages_received')}",
                        f"deserialize errors={device_measurements.get('deserialize_error_count')}",
                    ))
                else:
                    for key in (
                        "node_alive", "mandatory_topics_present",
                        "mandatory_messages_received", "width", "height",
                        "encoding", "calculated_fps", "cleanup_success",
                    ):
                        if key in device_measurements:
                            summary.append(
                                f"{key.replace('_', ' ')}="
                                f"{self._format_detail_value(device_measurements[key])}"
                            )
                summary.append(f"status={sub_result.get('status', '--')}")
                measurement_lines.append(
                    prefix + (": " + ", ".join(summary) if summary else "")
                )
                for rule in sub_result.get("rule_results") or ():
                    rule_lines.append(
                        ("PASS" if rule.get("passed") else "FAIL")
                        + "  " + str(rule.get("metric"))
                        + f" {rule.get('operator')} {rule.get('expected')}"
                    )
            if not rule_lines:
                for rule in result.get("rule_results") or ():
                    rule_lines.append(
                        ("PASS" if rule.get("passed") else "FAIL")
                        + "  " + str(rule.get("metric"))
                        + f" {rule.get('operator')} {rule.get('expected')}"
                    )
        if not rule_lines:
            rule_lines = [
                f"{rule['metric']} {rule['operator']} {rule['expected']}"
                for rule in definition.rules
            ]
        self.ros_test_detail_text.setPlainText(
            f"Test ID: {definition.test_id}  |  Test Name: {definition.name}\n"
            f"Target: {target}  |  Driver: {drivers}  |  "
            f"Automation Key: {definition.automation_key}\n"
            f"Parameters: {parameters}\n"
            "Latest Measurements: "
            + ("; ".join(measurement_lines) if measurement_lines else "--")
            + "\nAcceptance / Rule Results: " + "; ".join(rule_lines)
            + f"\nLatest Result: {latest}"
        )
        self.ros_view_result_button.setEnabled(result is not None)
        self.ros_view_result_button.setProperty("test_id", test_id)

    def _run_selected_ros_tests(self):
        if self.test_runner_worker and self.test_runner_worker.isRunning():
            self._append_ros_log(
                "WARNING", "Camera-level automated tests are already running."
            )
            return
        selected_ids = [
            self.ros_test_table.item(row, 1).text()
            for row in range(self.ros_test_table.rowCount())
            if self.ros_test_table.item(row, 0).checkState()
            == Qt.CheckState.Checked
        ]
        if not selected_ids:
            return
        definitions = [
            item for item in self.ros_test_definitions
            if item.test_id in selected_ids
        ]
        devices = tuple(
            self.camera_target_selection.selected_devices(
                self.camera_inventory_service.get_devices()
            )
        )
        target_scope = (
            "ALL_CAMERAS"
            if self.camera_target_selection.selected_device_uid is None
            else "INDIVIDUAL"
        )
        busy_serials = []
        if self.connection_state == CameraConnectionState.STREAMING:
            serial = self.device_combo.currentData()
            if serial:
                busy_serials.append(str(serial))
        worker = RosCameraTestRunnerWorker(
            definitions,
            self.test_registry,
            self.jetson_service,
            devices,
            target_scope,
            busy_serials,
            parent=self,
        )
        worker.test_started.connect(self._on_ros_test_started)
        worker.test_finished.connect(self._on_ros_test_finished)
        worker.log_event.connect(self._append_ros_log)
        worker.suite_finished.connect(self._on_ros_test_suite_finished)
        worker.finished.connect(self._on_ros_test_worker_finished)
        self.ros_test_runner_worker = worker
        self.tests_requested.emit(selected_ids)
        self._append_ros_log(
            "INFO",
            f"Captured {target_scope} target snapshot with {len(devices)} camera(s).",
        )
        self.ros_test_table.setEnabled(False)
        self.target_camera_combo.setEnabled(False)
        self.inventory_discover_button.setEnabled(False)
        self._update_ros_test_controls()
        worker.start()

    def _cancel_ros_test_run(self):
        worker = self.ros_test_runner_worker
        if worker and worker.isRunning():
            self.ros_cancel_tests_button.setEnabled(False)
            self._append_ros_log(
                "WARNING",
                "Cancelling ROS test run; owned-node cleanup will finish first.",
            )
            worker.cancel()

    def _on_ros_test_started(self, test_id):
        self._set_ros_test_status(test_id, "RUNNING")
        self._set_ros_badge(
            self.ros_runner_status_label, f"Runner {test_id}", "running"
        )

    def _on_ros_test_finished(self, test_id, status, result):
        self.ros_test_results[test_id] = result
        self._set_ros_test_status(test_id, status)
        if test_id == "ROS-001":
            self._render_ros_environment_result(result)

    def _on_ros_test_suite_finished(self, summary, result_root):
        self._append_ros_log(
            "INFO",
            "ROS Test Run Complete — " + ", ".join(
                f"{name}: {summary.get(name, 0)}"
                for name in (
                    "total", "PASS", "FAIL", "ERROR", "BLOCKED", "CANCELLED"
                )
            ),
        )
        self._append_ros_log("INFO", f"Structured results: {result_root}")
        self._refresh_ros_test_summary()

    def _on_ros_test_worker_finished(self):
        self.ros_test_runner_worker = None
        self.ros_test_table.setEnabled(True)
        self.target_camera_combo.setEnabled(True)
        self.inventory_discover_button.setEnabled(
            self.jetson_service.is_connected
            and not self.camera_inventory_service.busy
        )
        self._update_ros_test_controls()
        if self._shutdown_pending:
            self._shutdown_pending = False
            self.shutdown_ready.emit()

    def _set_ros_test_status(self, test_id, status):
        normalized = status.replace("_", " ")
        self.ros_test_statuses[test_id] = normalized
        for row in range(self.ros_test_table.rowCount()):
            if self.ros_test_table.item(row, 1).text() == test_id:
                item = self.ros_test_table.item(row, 5)
                item.setText(normalized)
                colors = {
                    "RUNNING": "#155EEF",
                    "PASS": "#16883F",
                    "FAIL": "#D92D20",
                    "ERROR": "#912018",
                    "BLOCKED": "#B54708",
                    "CANCELLED": "#667085",
                    "NOT RUN": "#667085",
                }
                item.setForeground(QColor(colors.get(normalized, "#667085")))
                break
        self._refresh_ros_test_summary()
        if self._selected_ros_test_id == test_id:
            self._show_ros_test_details(test_id)

    def _refresh_ros_test_summary(self):
        if not hasattr(self, "ros_total_label"):
            return
        counts = {
            name: 0
            for name in (
                "PASS", "FAIL", "ERROR", "BLOCKED", "CANCELLED",
                "RUNNING", "NOT RUN",
            )
        }
        for status in self.ros_test_statuses.values():
            counts[status] = counts.get(status, 0) + 1
        selected = sum(
            self.ros_test_table.item(row, 0).checkState()
            == Qt.CheckState.Checked
            for row in range(self.ros_test_table.rowCount())
        )
        self.ros_total_label.setText(f"Total {len(self.ros_test_definitions)}")
        self.ros_selected_tests_label.setText(f"Selected {selected}")
        self.ros_pass_label.setText(f"PASS {counts['PASS']}")
        self.ros_fail_label.setText(f"FAIL {counts['FAIL']}")
        self.ros_error_label.setText(f"ERROR {counts['ERROR']}")
        self.ros_blocked_label.setText(f"BLOCKED {counts['BLOCKED']}")

    def _update_ros_test_controls(self):
        if not hasattr(self, "ros_run_tests_button"):
            return
        running = bool(
            self.ros_test_runner_worker
            and self.ros_test_runner_worker.isRunning()
        )
        selected = any(
            self.ros_test_table.item(row, 0).checkState()
            == Qt.CheckState.Checked
            for row in range(self.ros_test_table.rowCount())
        )
        camera_tests_running = bool(
            self.test_runner_worker and self.test_runner_worker.isRunning()
        )
        self.ros_run_tests_button.setEnabled(
            selected and not running and not camera_tests_running
        )
        self.ros_cancel_tests_button.setEnabled(running)
        self._set_ros_badge(
            self.ros_runner_status_label,
            "Runner RUNNING" if running else "Runner IDLE",
            "running" if running else "idle",
        )

    def _render_ros_environment_result(self, result):
        measurements = result.get("measurements") or {}
        driver_results = measurements.get("driver_results") or {}
        self._ros_environment_values.update({
            "ROS Distro": measurements.get("ros_distro") or "UNAVAILABLE",
            "ROS Environment": (
                "LOADED" if measurements.get("ros_environment_loaded")
                else "UNAVAILABLE"
            ),
            "zed_wrapper": (
                "FOUND" if driver_results.get("zed_wrapper", {}).get("installed")
                else "NOT REQUIRED" if "zed_wrapper" not in driver_results
                else "MISSING"
            ),
            "realsense2_camera": (
                "FOUND"
                if driver_results.get("realsense2_camera", {}).get("installed")
                else "NOT REQUIRED" if "realsense2_camera" not in driver_results
                else "MISSING"
            ),
            "Workspace": measurements.get("workspace_setup") or "NONE",
            "Last Check": result.get("finished_at") or "UNKNOWN",
        })
        status = result.get("status") or "UNKNOWN"
        distro = measurements.get("ros_distro")
        self._set_ros_badge(
            self.ros_environment_status_label,
            f"ROS {str(distro).title()}" if distro else f"ROS {status}",
            "ok" if status == "PASS" else "error",
        )
        required_drivers = sorted(driver_results)
        driver_name = required_drivers[0] if len(required_drivers) == 1 else "Drivers"
        drivers_ready = bool(measurements.get("all_required_drivers_installed"))
        self._set_ros_badge(
            self.ros_driver_status_label,
            f"{driver_name} {'READY' if drivers_ready else 'MISSING'}",
            "ok" if drivers_ready else "error",
        )
        self._update_open_ros_environment_dialog()

    def _update_selected_test_count(self, *_):
        selected = sum(
            self.test_table.item(row, 0).checkState() == Qt.CheckState.Checked
            for row in range(self.test_table.rowCount())
        )
        total = self.test_table.rowCount()
        self.selected_tests_label.setText(f"{selected} / {total} Selected")
        running = self.test_runner_worker is not None and self.test_runner_worker.isRunning()
        prerequisites = self.jetson_service.is_connected and self.connection_state == CameraConnectionState.CONNECTED
        self.run_tests_button.setEnabled(selected > 0 and not running and prerequisites)
        self._refresh_test_summaries()

    def _apply_test_filters(self, *_):
        if not hasattr(self, "test_table"):
            return
        query = self.test_search.text().strip().lower() if hasattr(self, "test_search") else ""
        category = self.test_category_filter.currentText() if hasattr(self, "test_category_filter") else "All"
        status = self.test_status_filter.currentText() if hasattr(self, "test_status_filter") else "All"
        for row in range(self.test_table.rowCount()):
            test_id = self.test_table.item(row, 1).text()
            name = self.test_table.item(row, 2).text()
            group = self.test_table.item(row, 3).text()
            row_status = self.test_statuses.get(test_id, "NOT RUN")
            visible = (
                (not query or query in test_id.lower() or query in name.lower())
                and (category == "All" or category == group)
                and (status == "All" or status == row_status)
            )
            self.test_table.setRowHidden(row, not visible)

    def _select_all_visible_tests(self):
        self.test_table.blockSignals(True)
        for row in range(self.test_table.rowCount()):
            if not self.test_table.isRowHidden(row):
                self.test_table.item(row, 0).setCheckState(Qt.CheckState.Checked)
        self.test_table.blockSignals(False)
        self._update_selected_test_count()

    def _clear_test_selection(self):
        self.test_table.blockSignals(True)
        for row in range(self.test_table.rowCount()):
            self.test_table.item(row, 0).setCheckState(Qt.CheckState.Unchecked)
        self.test_table.blockSignals(False)
        self._update_selected_test_count()

    def _on_test_row_clicked(self, row, _column):
        test_id = self.test_table.item(row, 1).text()
        self._show_test_details(test_id)

    def _show_test_details(self, test_id):
        definition = next((item for item in self.test_definitions if item.test_id == test_id), None)
        if definition is None:
            self.test_detail_text.setText("Select a test case to view details.")
            return
        parameter_lines = "\n".join(
            f"  {key.replace('_', ' ').title()}: {self._format_detail_value(value)}"
            for key, value in definition.parameters.items()
        ) or "  --"
        rule_lines = "\n".join(
            f"  {rule['metric'].replace('_', ' ').title()} {rule['operator']} {self._format_detail_value(rule['expected'])}"
            for rule in definition.rules
        ) or "  --"
        if definition.automation_key == "camera.resolution_fps":
            p = definition.parameters
            derived = [
                f"  Width == {p.get('width')}",
                f"  Height == {p.get('height')}",
                "  Average FPS Ratio >= 0.95",
            ]
            if "max_drop_ratio" in p:
                derived.append(f"  Drop Ratio <= {p['max_drop_ratio']}")
            if p.get("require_timestamp_integrity"):
                derived.extend(("  Duplicate Timestamp Count == 0", "  Timestamp Rollback Count == 0"))
            rule_lines = "\n".join(derived)
        status = self.test_statuses.get(test_id, "NOT RUN")
        result = self.test_results.get(test_id)
        measurement_lines = ""
        if result:
            measurements = result.get("measurements") or {}
            measurement_lines = "\n\nMeasurements\n" + (
                "\n".join(f"  {key.replace('_', ' ').title()}: {self._format_detail_value(value)}"
                          for key, value in measurements.items() if not isinstance(value, (dict, list))) or "  --"
            )
        self.test_detail_text.setPlainText(
            f"{definition.test_id}\n{definition.name}\n\n"
            f"Category\n  {definition.group}\n\nAutomation Key\n  {definition.automation_key}\n\n"
            f"Handler Type\n  {type(self.test_registry.resolve(definition.automation_key)).__name__}\n\n"
            f"Priority\n  {definition.priority}\n\nEstimated Timeout\n  {definition.timeout_s:g} seconds\n\n"
            f"Configuration / Parameters\n{parameter_lines}\n\nAcceptance Criteria\n{rule_lines}\n\n"
            f"Latest Result\n  {status}{measurement_lines}"
        )
        self.view_result_button.setEnabled(result is not None)
        self.view_result_button.setProperty("test_id", test_id)

    @staticmethod
    def _format_detail_value(value):
        if isinstance(value, list):
            return ", ".join(str(item) for item in value)
        if isinstance(value, bool):
            return "true" if value else "false"
        return "--" if value is None else str(value)

    def _view_selected_test_result(self):
        test_id = self.view_result_button.property("test_id")
        result = self.test_results.get(test_id)
        if not result:
            return
        dialog = QDialog(self)
        dialog.setWindowTitle(f"Test Result — {test_id}")
        dialog.resize(720, 620)
        layout = QVBoxLayout(dialog)
        text = QTextEdit()
        text.setReadOnly(True)
        sections = []
        for key in ("status", "started_at", "finished_at", "duration_s", "device", "configuration",
                    "measurements", "rule_results", "failure_reasons", "error"):
            sections.append(f"{key.replace('_', ' ').title()}\n{self._readable_result_value(result.get(key))}")
        text.setPlainText("\n\n".join(sections))
        close = QPushButton("CLOSE")
        close.setObjectName("PrimaryButton")
        close.clicked.connect(dialog.accept)
        layout.addWidget(text, 1)
        layout.addWidget(close)
        dialog.exec()

    @staticmethod
    def _readable_result_value(value, indent=0):
        prefix = "  " * indent
        if isinstance(value, dict):
            return "\n".join(f"{prefix}{key}: {CameraPage._readable_result_value(item, indent + 1)}" for key, item in value.items()) or "--"
        if isinstance(value, list):
            return "\n".join(f"{prefix}- {CameraPage._readable_result_value(item, indent + 1)}" for item in value) or "--"
        return "--" if value is None else str(value)

    def _run_selected_tests(self):
        if self.ros_test_runner_worker and self.ros_test_runner_worker.isRunning():
            self._append_test_log(
                "WARNING", "ROS automated tests are already running."
            )
            return
        selected = [
            self.test_table.item(row, 1).text()
            for row in range(self.test_table.rowCount())
            if self.test_table.item(row, 0).checkState() == Qt.CheckState.Checked
        ]
        if not selected:
            return
        self.tests_requested.emit(selected)
        definitions = [item for item in self.test_definitions if item.test_id in selected]
        self._append_test_log("INFO", f"Test run started: {len(definitions)} selected cases.")
        device = {
            "profile_id": self.model_combo.currentData(),
            "model": self.model_combo.currentText(),
            "serial": self.device_combo.currentData(),
        }
        configuration = {
            "manual_stream_active": self.connection_state == CameraConnectionState.STREAMING,
            "camera_connected": self.connection_state == CameraConnectionState.CONNECTED,
        }
        worker = CameraTestRunnerWorker(
            definitions, self.test_registry, self.jetson_service,
            self.camera_service, device, configuration, self,
        )
        worker.test_started.connect(self._on_test_started)
        worker.test_finished.connect(self._on_test_finished)
        worker.log_event.connect(self._append_test_log)
        worker.suite_finished.connect(self._on_test_suite_finished)
        worker.finished.connect(self._on_test_worker_finished)
        self.test_runner_worker = worker
        self.run_tests_button.setEnabled(False)
        self.cancel_tests_button.setEnabled(True)
        self.test_table.setEnabled(False)
        self.select_all_tests_button.setEnabled(False)
        self.clear_test_selection_button.setEnabled(False)
        self._set_actions_enabled(False)
        worker.start()
        self._refresh_test_summaries()

    def _cancel_test_run(self):
        if self.test_runner_worker and self.test_runner_worker.isRunning():
            self._append_test_log("WARNING", "Cancelling Camera test run; cleanup will complete first.")
            self.cancel_tests_button.setEnabled(False)
            self.test_runner_worker.cancel()
            self._refresh_test_summaries()

    def _on_test_started(self, test_id):
        self._set_test_status(test_id, "RUNNING")

    def _on_test_finished(self, test_id, status, _result):
        self.test_results[test_id] = _result
        self._set_test_status(test_id, status)

    def _on_test_suite_finished(self, summary, result_root):
        self._append_test_log(
            "INFO", "Test Run Complete — " + ", ".join(
                f"{name}: {summary.get(name, 0)}"
                for name in ("total", "PASS", "FAIL", "ERROR", "BLOCKED", "CANCELLED")
            )
        )
        self._append_test_log("INFO", f"Structured results: {result_root}")

    def _on_test_worker_finished(self):
        self.test_runner_worker = None
        self.cancel_tests_button.setEnabled(False)
        self.test_table.setEnabled(True)
        self.select_all_tests_button.setEnabled(True)
        self.clear_test_selection_button.setEnabled(True)
        self._set_actions_enabled(True)
        self._update_selected_test_count()
        self._update_ros_test_controls()
        if self._shutdown_pending:
            self._shutdown_pending = False
            self.shutdown_ready.emit()

    def _set_test_status(self, test_id, status):
        normalized = status.replace("_", " ")
        self.test_statuses[test_id] = normalized
        for row in range(self.test_table.rowCount()):
            if self.test_table.item(row, 1).text() == test_id:
                item = self.test_table.item(row, 5)
                item.setText(normalized)
                colors = {
                    "RUNNING": "#155EEF", "PASS": "#16883F", "FAIL": "#D92D20",
                    "ERROR": "#912018", "BLOCKED": "#B54708", "CANCELLED": "#667085",
                    "NOT RUN": "#667085",
                }
                item.setForeground(QColor(colors.get(normalized, "#667085")))
                break
        self._apply_test_filters()
        self._refresh_test_summaries()
        if self.view_result_button.property("test_id") == test_id:
            self._show_test_details(test_id)

    def _refresh_test_summaries(self):
        if not hasattr(self, "automation_summary_label"):
            return
        counts = {name: 0 for name in ("PASS", "FAIL", "ERROR", "BLOCKED", "RUNNING", "CANCELLED", "NOT RUN")}
        for status in self.test_statuses.values():
            counts[status] = counts.get(status, 0) + 1
        total = len(self.test_definitions)
        selected = 0
        if hasattr(self, "test_table"):
            selected = sum(
                self.test_table.item(row, 0).checkState() == Qt.CheckState.Checked
                for row in range(self.test_table.rowCount())
            )
        running = self.test_runner_worker is not None and self.test_runner_worker.isRunning()
        runner = "RUNNING" if running else "IDLE"
        self.automation_runner_label.setText(f"Runner: {runner}")
        self.automation_summary_label.setText(
            f"Total {total}  |  PASS {counts['PASS']}  |  FAIL {counts['FAIL']}  |  "
            f"ERROR {counts['ERROR']}  |  BLOCKED {counts['BLOCKED']}  |  NOT RUN {counts['NOT RUN']}"
        )
        self.test_run_summary_label.setText(
            f"Total {total} | Selected {selected} | PASS {counts['PASS']} | FAIL {counts['FAIL']} | "
            f"ERROR {counts['ERROR']} | BLOCKED {counts['BLOCKED']}"
        )
        self._refresh_runner_status()

    def _refresh_runner_status(self):
        if not hasattr(self, "runner_state_label"):
            return
        running = self.test_runner_worker is not None and self.test_runner_worker.isRunning()
        cancelling = running and self.test_runner_worker.cancel_event.is_set()
        state = "CANCELLING" if cancelling else "RUNNING" if running else "IDLE"
        self.runner_state_label.setText(f"● {state}")
        self.runner_jetson_label.setText(f"Jetson: {'CONNECTED' if self.jetson_service.is_connected else 'DISCONNECTED'}")
        ready = self.connection_state == CameraConnectionState.CONNECTED
        self.runner_camera_label.setText(f"Camera: {'READY' if ready else 'NOT READY'}")
        model = self.model_combo.currentText() if hasattr(self, "model_combo") else "--"
        serial = self.device_combo.currentData() if hasattr(self, "device_combo") else None
        self.runner_device_label.setText(f"Device: {model or '--'}")
        self.runner_serial_label.setText(f"SN: {serial or '--'}")

    def _append_test_log(self, level, message):
        self._append_log_widget(self.test_execution_log, level, message)

    def _append_ros_log(self, level, message):
        timestamp = datetime.now(timezone.utc).strftime("%H:%M:%S.%f")[:-3]
        level = level.upper()
        self._ros_log_entries.append((timestamp, level, message))
        selected_filter = self.ros_log_filter_combo.currentText()
        if self._ros_log_filter_matches(selected_filter, level):
            self._append_log_line(
                self.ros_execution_log,
                level,
                message,
                timestamp,
                self.ros_log_auto_scroll_check.isChecked(),
            )

    def append_log(self, level, message):
        self._append_log_widget(self.live_log, level, message)

    def _append_log_widget(self, widget, level, message):
        timestamp = datetime.now(timezone.utc).strftime("%H:%M:%S.%f")[:-3]
        level = level.upper()
        auto_scroll = widget is not self.live_log or self.auto_scroll_check.isChecked()
        self._append_log_line(widget, level, message, timestamp, auto_scroll)

    @staticmethod
    def _append_log_line(widget, level, message, timestamp, auto_scroll):
        colors = {
            "INFO": "#3FB950",
            "PASS": "#3FB950",
            "FAIL": "#F85149",
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
        widget.append(line)
        if auto_scroll:
            bar = widget.verticalScrollBar()
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
        self._append_ros_log(
            "INFO",
            "ROS Automation workspace ready. Discover cameras and run ROS-001 through ROS-008.",
        )

    def shutdown_stream(self):
        if self.ros_test_runner_worker and self.ros_test_runner_worker.isRunning():
            self._shutdown_pending = True
            self._append_ros_log(
                "INFO", "Application shutdown: cancelling ROS Camera test run."
            )
            self.ros_test_runner_worker.cancel()
        elif self.test_runner_worker and self.test_runner_worker.isRunning():
            self._shutdown_pending = True
            self.append_log("INFO", "Application shutdown: cancelling Camera test run.")
            self.test_runner_worker.cancel()
        elif self.connection_state == CameraConnectionState.STREAMING:
            self._shutdown_pending = True
            self.append_log("INFO", "Application shutdown: stopping camera stream.")
            self._stop_preview("STOPPING", "Preview stopped")
            self._set_actions_enabled(False)
            self.stream_controller.stop()
        else:
            self.shutdown_ready.emit()
