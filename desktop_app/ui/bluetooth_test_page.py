"""Dedicated BT-0 Bluetooth automated test page."""

from __future__ import annotations

from datetime import datetime, timezone

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.bluetooth import BluetoothTestRunner, BluetoothTestStatus
from desktop_app.ui.widgets import Card


class BluetoothTestPage(QWidget):
    """Display the BT-0 catalog and preserve a future runner interaction seam."""

    back_requested = Signal()
    summary_changed = Signal(dict)

    def __init__(self, runner: BluetoothTestRunner, parent=None):
        super().__init__(parent)
        self.runner = runner
        self._build_ui()
        self._populate_table()
        self.runner.status_changed.connect(self._on_runner_status_changed)
        self.runner.log.connect(self.append_log)
        self.runner.running_changed.connect(self._on_runner_running_changed)
        self._on_runner_running_changed(self.runner.running)
        self.append_log("Module initialized")
        self.append_log("Waiting for test execution...")

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(28, 24, 28, 24)
        root.setSpacing(12)

        header = QHBoxLayout()
        title_block = QVBoxLayout()
        title = QLabel("Bluetooth Automated Tests")
        title.setObjectName("PageTitle")
        subtitle = QLabel("Bluetooth / Automated Tests")
        subtitle.setObjectName("Muted")
        title_block.addWidget(title)
        title_block.addWidget(subtitle)
        self.back_button = QPushButton("←  Bluetooth Overview")
        self.back_button.setObjectName("OutlineButton")
        self.back_button.clicked.connect(self.back_requested.emit)
        header.addLayout(title_block)
        header.addStretch()
        header.addWidget(self.back_button)
        root.addLayout(header)

        catalog_card = Card("Bluetooth Test Catalog")
        filters = QHBoxLayout()
        filters.addWidget(QLabel("Group Filter:"))
        self.group_filter = QComboBox()
        self.group_filter.addItem("All", None)
        for group in sorted({case.group for case in self.runner.test_cases}):
            self.group_filter.addItem(group, group)
        self.group_filter.currentIndexChanged.connect(self._apply_filters)
        filters.addWidget(self.group_filter)
        filters.addWidget(QLabel("Status Filter:"))
        self.status_filter = QComboBox()
        self.status_filter.addItem("All", None)
        for status in BluetoothTestStatus:
            self.status_filter.addItem(
                status.value.replace("_", " ").title(), status.value
            )
        self.status_filter.currentIndexChanged.connect(self._apply_filters)
        filters.addWidget(self.status_filter)
        filters.addStretch()
        catalog_card.body_layout.addLayout(filters)

        self.test_table = QTableWidget(0, 5)
        self.test_table.setHorizontalHeaderLabels(
            ["Select", "Test Case ID", "Test Name", "Mode", "Status"]
        )
        self.test_table.verticalHeader().setVisible(False)
        self.test_table.setAlternatingRowColors(True)
        self.test_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.test_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.test_table.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        table_header = self.test_table.horizontalHeader()
        for column in (0, 1, 3, 4):
            table_header.setSectionResizeMode(
                column, QHeaderView.ResizeMode.ResizeToContents
            )
        table_header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.test_table.itemChanged.connect(self._emit_summary)
        catalog_card.body_layout.addWidget(self.test_table)

        controls = QHBoxLayout()
        self.select_all_button = QPushButton("Select All")
        self.select_all_button.setObjectName("OutlineButton")
        self.select_all_button.clicked.connect(self.select_all_visible)
        self.run_selected_button = QPushButton("Run Selected")
        self.run_selected_button.setObjectName("PrimaryButton")
        self.run_selected_button.clicked.connect(self.run_selected)
        self.run_all_button = QPushButton("Run All Auto Tests")
        self.run_all_button.setObjectName("OutlineButton")
        self.run_all_button.clicked.connect(self.run_all)
        self.stop_button = QPushButton("Stop")
        self.stop_button.setObjectName("DangerButton")
        self.stop_button.clicked.connect(self.stop)
        controls.addWidget(self.select_all_button)
        controls.addWidget(self.run_selected_button)
        controls.addWidget(self.run_all_button)
        controls.addWidget(self.stop_button)
        controls.addStretch()
        catalog_card.body_layout.addLayout(controls)
        root.addWidget(catalog_card)

        log_card = Card("Live Log")
        log_header = QHBoxLayout()
        self.auto_scroll_check = QCheckBox("Auto Scroll")
        self.auto_scroll_check.setChecked(True)
        clear_button = QPushButton("Clear")
        clear_button.setObjectName("SmallButton")
        log_header.addStretch()
        log_header.addWidget(self.auto_scroll_check)
        log_header.addWidget(clear_button)
        log_card.body_layout.addLayout(log_header)
        self.live_log = QPlainTextEdit()
        self.live_log.setObjectName("LiveLog")
        self.live_log.setReadOnly(True)
        self.live_log.setMinimumHeight(180)
        self.live_log.document().setMaximumBlockCount(500)
        clear_button.clicked.connect(self.live_log.clear)
        log_card.body_layout.addWidget(self.live_log)
        root.addWidget(log_card, 1)

    def _populate_table(self) -> None:
        checked_ids = self.selected_test_ids()
        self.test_table.blockSignals(True)
        self.test_table.setRowCount(len(self.runner.test_cases))
        for row, test_case in enumerate(self.runner.test_cases):
            select_item = QTableWidgetItem()
            select_item.setFlags(
                Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable
            )
            select_item.setCheckState(
                Qt.CheckState.Checked
                if test_case.test_id in checked_ids
                else Qt.CheckState.Unchecked
            )
            self.test_table.setItem(row, 0, select_item)
            self.test_table.setItem(row, 1, QTableWidgetItem(test_case.test_id))
            name_item = QTableWidgetItem(test_case.name)
            name_item.setToolTip(
                f"Group: {test_case.group}\nPurpose: {test_case.purpose}"
            )
            self.test_table.setItem(row, 2, name_item)
            self.test_table.setItem(row, 3, QTableWidgetItem(test_case.mode.value))
            status = self.runner.status_for(test_case.test_id)
            status_item = QTableWidgetItem(status.value.replace("_", " "))
            status_item.setData(Qt.ItemDataRole.UserRole, status.value)
            self.test_table.setItem(row, 4, status_item)
        self.test_table.blockSignals(False)
        self._apply_filters()
        self._emit_summary()

    def selected_test_ids(self) -> list[str]:
        return [
            self.test_table.item(row, 1).text()
            for row in range(self.test_table.rowCount())
            if self.test_table.item(row, 0).checkState() == Qt.CheckState.Checked
        ]

    def select_all_visible(self) -> None:
        self.test_table.blockSignals(True)
        for row in range(self.test_table.rowCount()):
            if not self.test_table.isRowHidden(row):
                self.test_table.item(row, 0).setCheckState(Qt.CheckState.Checked)
        self.test_table.blockSignals(False)
        self._emit_summary()

    def run_selected(self) -> None:
        selected = self.selected_test_ids()
        if not selected:
            self.append_log("No test cases selected.")
            return
        self.append_log(f"Execution requested for {len(selected)} test(s).")
        self.runner.run_selected(selected)

    def run_all(self) -> None:
        self.append_log("Execution requested for all AUTO tests.")
        self.runner.run_all()

    def stop(self) -> None:
        self.runner.stop()
        self._emit_summary()

    def _on_runner_status_changed(self, _test_case_id, _status) -> None:
        self._populate_table()

    def _on_runner_running_changed(self, running: bool) -> None:
        self.select_all_button.setEnabled(not running)
        self.run_selected_button.setEnabled(not running)
        self.run_all_button.setEnabled(not running)
        self.stop_button.setEnabled(running)

    def _apply_filters(self, *_args) -> None:
        group = self.group_filter.currentData()
        status = self.status_filter.currentData()
        for row, test_case in enumerate(self.runner.test_cases):
            current_status = self.runner.status_for(test_case.test_id).value
            self.test_table.setRowHidden(
                row,
                (group is not None and test_case.group != group)
                or (status is not None and current_status != status),
            )

    def _emit_summary(self, *_args) -> None:
        self.summary_changed.emit(self.runner.summary())

    def append_log(self, message: str) -> None:
        timestamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
        self.live_log.appendPlainText(f"[{timestamp}] [Bluetooth] {message}")
        if self.auto_scroll_check.isChecked():
            scrollbar = self.live_log.verticalScrollBar()
            scrollbar.setValue(scrollbar.maximum())
