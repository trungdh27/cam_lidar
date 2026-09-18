"""Bluetooth status overview and BT-0 test workspace container."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from core.bluetooth import (
    BluetoothManager,
    BluetoothTestRunner,
    BluetoothTestStatus,
)
from desktop_app.state.jetson_state import JetsonState
from desktop_app.ui.bluetooth_test_page import BluetoothTestPage
from desktop_app.ui.widgets import Card, StatusChip


class BluetoothPage(QWidget):
    """A presentation layer that only consumes the shared Jetson service."""

    def __init__(
        self,
        jetson_state: JetsonState,
        jetson_service,
        bluetooth_manager: BluetoothManager,
        test_runner: BluetoothTestRunner,
        parent=None,
    ):
        super().__init__(parent)
        self.jetson_state = jetson_state
        self.jetson_service = jetson_service
        self.bluetooth_manager = bluetooth_manager
        self.test_runner = test_runner
        self._opened_logged = False
        self._discovery_request_id = None
        self._build_ui()
        self.jetson_state.state_changed.connect(self._on_jetson_state_changed)
        self.bluetooth_manager.discovery_completed.connect(
            self._on_discovery_completed
        )
        self.test_page.summary_changed.connect(self._render_test_summary)
        self._refresh_status(log_message=None)
        self._render_test_summary(self.test_runner.summary())
        self.test_page.append_log("Shared Jetson connection detected")

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        self.pages = QStackedWidget()
        self.main_page = self._build_main_page()
        self.test_page = BluetoothTestPage(self.test_runner)
        self.test_page.back_requested.connect(lambda: self.pages.setCurrentIndex(0))
        self.pages.addWidget(self.main_page)
        self.pages.addWidget(self.test_page)
        root.addWidget(self.pages)

    def _build_main_page(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(12)

        title = QLabel("Bluetooth")
        title.setObjectName("PageTitle")
        subtitle = QLabel("Jetson Bluetooth status and automated test foundation")
        subtitle.setObjectName("Muted")
        layout.addWidget(title)
        layout.addWidget(subtitle)

        device_card = Card("Bluetooth Device Status")
        grid = QGridLayout()
        grid.setHorizontalSpacing(24)
        grid.setVerticalSpacing(10)
        labels = (
            "Jetson Connection",
            "Controller",
            "Controller Name",
            "Bluetooth Service",
            "Adapter Powered",
            "BLE Supported",
            "Expected Role",
            "Actual Role",
        )
        for row, label in enumerate(labels):
            key = QLabel(f"{label}:")
            key.setObjectName("KeyLabel")
            grid.addWidget(key, row, 0)
        self.jetson_chip = StatusChip("Disconnected", "idle")
        self.controller_chip = StatusChip("Unknown", "idle")
        self.controller_name_label = QLabel("--")
        self.controller_name_label.setObjectName("ValueLabel")
        self.service_chip = StatusChip("Unknown", "idle")
        self.adapter_chip = StatusChip("Unknown", "idle")
        self.ble_chip = StatusChip("Unknown", "idle")
        self.expected_role_label = QLabel("Peripheral")
        self.expected_role_label.setObjectName("ValueLabel")
        self.actual_role_chip = StatusChip("Unknown", "idle")
        for row, widget in enumerate(
            (
                self.jetson_chip,
                self.controller_chip,
                self.controller_name_label,
                self.service_chip,
                self.adapter_chip,
                self.ble_chip,
                self.expected_role_label,
                self.actual_role_chip,
            )
        ):
            grid.addWidget(widget, row, 1)
        grid.setColumnStretch(1, 1)
        device_card.body_layout.addLayout(grid)
        actions = QHBoxLayout()
        self.discover_button = QPushButton("Discover Bluetooth")
        self.discover_button.setObjectName("PrimaryButton")
        self.discover_button.clicked.connect(self._discover)
        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.setObjectName("OutlineButton")
        self.refresh_button.clicked.connect(lambda: self._refresh_status("Status refreshed"))
        actions.addWidget(self.discover_button)
        actions.addWidget(self.refresh_button)
        actions.addStretch()
        device_card.body_layout.addLayout(actions)
        layout.addWidget(device_card)

        summary_card = Card("Bluetooth Test Summary")
        summary_grid = QGridLayout()
        self.summary_labels = {}
        for column, (label, status) in enumerate(
            (
                ("Total Tests", None),
                ("Ready", BluetoothTestStatus.READY),
                ("Running", BluetoothTestStatus.RUNNING),
                ("Passed", BluetoothTestStatus.PASS),
                ("Failed", BluetoothTestStatus.FAIL),
                ("Errors", BluetoothTestStatus.ERROR),
                ("Skipped", BluetoothTestStatus.SKIPPED),
            )
        ):
            caption = QLabel(label)
            caption.setObjectName("MetricLabel")
            value = QLabel("0")
            value.setObjectName("MetricValue")
            summary_grid.addWidget(caption, 0, column)
            summary_grid.addWidget(value, 1, column)
            self.summary_labels[status] = value
        summary_card.body_layout.addLayout(summary_grid)
        self.open_tests_button = QPushButton("Open Bluetooth Automated Tests")
        self.open_tests_button.setObjectName("PrimaryButton")
        self.open_tests_button.clicked.connect(self.open_automated_tests)
        summary_card.body_layout.addWidget(
            self.open_tests_button, alignment=Qt.AlignmentFlag.AlignLeft
        )
        layout.addWidget(summary_card)
        layout.addStretch()
        scroll.setWidget(content)
        return scroll

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if not self._opened_logged:
            self.test_page.append_log("Bluetooth page opened")
            self._opened_logged = True

    def open_automated_tests(self) -> None:
        self.pages.setCurrentIndex(1)
        self.test_page.append_log("Automated test page opened")

    def _discover(self) -> None:
        self.discover_button.setEnabled(False)
        self._discovery_request_id = self.bluetooth_manager.start_discovery()
        self.test_page.append_log("Bluetooth discovery requested on the shared Jetson connection.")

    def _refresh_status(self, log_message: str | None) -> None:
        snapshot = self.bluetooth_manager.refresh()
        connected = snapshot.jetson_connected
        self.jetson_chip.set_state(
            "ok" if connected else "idle",
            "Connected" if connected else "Disconnected",
        )
        self.discover_button.setEnabled(connected)
        if log_message:
            self.test_page.append_log(log_message)

    def _on_discovery_completed(self, result) -> None:
        self._discovery_request_id = None
        device = result.device
        self._render_device_status(device)
        self.discover_button.setEnabled(self.jetson_service.is_connected)
        self.test_page.append_log(result.message)
        if device.controller.detected:
            self.test_page.append_log(
                "Controller detected: "
                + (device.controller.interface or device.controller.name or "controller")
            )
        if device.service_active is not None:
            self.test_page.append_log(
                "bluetooth.service: " + ("active" if device.service_active else "inactive")
            )

    def _render_device_status(self, device) -> None:
        controller = device.controller
        self.jetson_chip.set_state(
            "ok" if device.jetson_connected else "idle",
            "Connected" if device.jetson_connected else "Disconnected",
        )
        self._set_boolean_chip(
            self.controller_chip,
            controller.detected,
            yes_text=controller.interface or "Detected",
            no_text="Not Detected",
        )
        self.controller_name_label.setText(controller.name or "--")
        self._set_boolean_chip(
            self.service_chip,
            device.service_active,
            yes_text="Active",
            no_text="Inactive",
        )
        self._set_boolean_chip(self.adapter_chip, controller.powered)
        self._set_boolean_chip(self.ble_chip, controller.ble_supported)
        role = controller.actual_role or "Unknown"
        self.actual_role_chip.set_state(
            "ok" if role in {"Peripheral", "Peripheral Capable"}
            else "error" if role == "Unsupported" else "idle",
            role,
        )

    @staticmethod
    def _set_boolean_chip(chip, value, yes_text="Yes", no_text="No") -> None:
        chip.set_state(
            "ok" if value is True else "error" if value is False else "idle",
            yes_text if value is True else no_text if value is False else "Unknown",
        )

    def _on_jetson_state_changed(self, _state) -> None:
        self._refresh_status(
            "Shared Jetson connection is "
            + ("connected" if self.jetson_service.is_connected else "disconnected")
        )
        if not self.jetson_service.is_connected:
            self._render_device_status(self.bluetooth_manager.status_snapshot())

    def _render_test_summary(self, summary: dict) -> None:
        self.summary_labels[None].setText(str(len(self.test_runner.test_cases)))
        for status, label in self.summary_labels.items():
            if status is not None:
                label.setText(str(summary.get(status, 0)))
