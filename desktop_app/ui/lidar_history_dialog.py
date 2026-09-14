"""Read-only LiDAR test history dialog."""

from __future__ import annotations

import json

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
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

from devices.livox.testing.history_store import LidarHistoryStore


class LidarHistoryDialog(QDialog):
    """Browse LiDAR evidence sessions without editing stored data."""

    SESSION_COLUMNS = (
        "Session",
        "Model / SN",
        "Started",
        "Duration",
        "PASS",
        "FAIL",
        "ERROR",
        "SKIPPED",
        "Status",
    )
    RESULT_COLUMNS = ("Test ID", "Status", "Duration", "Actual Result")

    def __init__(self, history_store=None, parent=None):
        super().__init__(parent)
        self.history_store = history_store or LidarHistoryStore()
        self._sessions: list[dict] = []
        self._results: list[dict] = []
        self.setWindowTitle("LiDAR Test History")
        self.resize(1050, 720)
        self._build_ui()
        self.refresh()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        header = QHBoxLayout()
        title = QLabel("LiDAR Test History")
        title.setObjectName("DialogTitle")
        header.addWidget(title)
        header.addStretch()
        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.clicked.connect(self.refresh)
        header.addWidget(self.refresh_button)
        layout.addLayout(header)

        self.session_table = self._table(self.SESSION_COLUMNS)
        self.session_table.setObjectName("LidarHistorySessionTable")
        self.session_table.itemSelectionChanged.connect(
            self._load_selected_session
        )
        layout.addWidget(self.session_table, 2)

        layout.addWidget(QLabel("Test Results"))
        self.result_table = self._table(self.RESULT_COLUMNS)
        self.result_table.setObjectName("LidarHistoryResultTable")
        self.result_table.itemSelectionChanged.connect(
            self._show_selected_result
        )
        layout.addWidget(self.result_table, 2)

        layout.addWidget(QLabel("Result Detail"))
        self.detail_view = QTextEdit()
        self.detail_view.setObjectName("LidarHistoryDetail")
        self.detail_view.setReadOnly(True)
        self.detail_view.setMinimumHeight(150)
        layout.addWidget(self.detail_view, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.close)
        layout.addWidget(buttons)

    @staticmethod
    def _table(columns: tuple[str, ...]) -> QTableWidget:
        table = QTableWidget(0, len(columns))
        table.setHorizontalHeaderLabels(list(columns))
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        table.setAlternatingRowColors(True)
        table.verticalHeader().setVisible(False)
        table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )
        table.horizontalHeader().setStretchLastSection(True)
        return table

    def refresh(self) -> None:
        self._sessions = self.history_store.list_sessions()
        self.session_table.setRowCount(len(self._sessions))
        for row, session in enumerate(self._sessions):
            counts = session.get("counts") or {}
            model = (
                session.get("detected_model")
                or session.get("selected_model")
                or "UNKNOWN"
            )
            serial = session.get("serial") or "UNKNOWN"
            values = (
                session.get("session_id") or "UNKNOWN",
                f"{model} / {serial}",
                session.get("started_at") or "UNKNOWN",
                _duration(session.get("duration_sec")),
                str(counts.get("PASS", 0)),
                str(counts.get("FAIL", 0)),
                str(counts.get("ERROR", 0)),
                str(counts.get("SKIPPED", 0)),
                session.get("status") or "UNKNOWN",
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self.session_table.setItem(row, column, item)
        if self._sessions:
            self.session_table.selectRow(0)
        else:
            self._clear_results("No LiDAR test history found.")

    def _load_selected_session(self) -> None:
        row = self.session_table.currentRow()
        if not 0 <= row < len(self._sessions):
            self._clear_results("")
            return
        session_id = self._sessions[row]["session_id"]
        try:
            self._results = self.history_store.list_results(session_id)
        except (OSError, ValueError) as exc:
            self._clear_results(f"Unable to read session: {exc}")
            return
        self.result_table.setRowCount(len(self._results))
        for result_row, result in enumerate(self._results):
            values = (
                result.get("test_id") or "UNKNOWN",
                result.get("status") or "UNKNOWN",
                _duration(result.get("duration_sec")),
                result.get("actual_result") or "UNKNOWN",
            )
            for column, value in enumerate(values):
                self.result_table.setItem(
                    result_row,
                    column,
                    QTableWidgetItem(str(value)),
                )
        if self._results:
            self.result_table.selectRow(0)
        else:
            self.detail_view.setPlainText("No readable results in this session.")

    def _show_selected_result(self) -> None:
        row = self.result_table.currentRow()
        if not 0 <= row < len(self._results):
            self.detail_view.clear()
            return
        result = self._results[row]
        measurements = json.dumps(
            result.get("measurements") or {},
            indent=2,
            sort_keys=True,
        )
        evidence = result.get("evidence") or []
        self.detail_view.setPlainText(
            "\n".join(
                (
                    f"Test ID: {result.get('test_id') or 'UNKNOWN'}",
                    f"Status: {result.get('status') or 'UNKNOWN'}",
                    "Actual Result: "
                    f"{result.get('actual_result') or 'UNKNOWN'}",
                    f"Measurements:\n{measurements}",
                    f"Error: {result.get('error') or '-'}",
                    "Evidence Paths:\n"
                    + ("\n".join(map(str, evidence)) or "-"),
                )
            )
        )

    def _clear_results(self, message: str) -> None:
        self._results = []
        self.result_table.setRowCount(0)
        self.detail_view.setPlainText(message)


def _duration(value) -> str:
    if not isinstance(value, (int, float)):
        return "UNKNOWN"
    return f"{value:.3f} s"
