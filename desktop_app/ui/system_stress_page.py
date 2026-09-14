from __future__ import annotations

from datetime import datetime
import html
import re
from pathlib import Path

from PySide6.QtCore import QThread, QTimer, QUrl, Qt, Signal
from PySide6.QtGui import QAction, QDesktopServices, QTextCursor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextBrowser,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from desktop_app.stress.catalog import StressCatalog, StressCatalogError
from desktop_app.stress.baseline import baseline_summary_text
from desktop_app.stress.evidence_summary import (
    KernelWarningSummary,
    SystemMetricsSummary,
    target_cpu_percent,
)
from desktop_app.stress.log_tail import IncrementalLogTail
from desktop_app.stress.load_strategy import SUPPORTED_ADAPTIVE_CPU_IDS, LoadStrategy
from desktop_app.stress.models import CollectorStatus, EnvironmentStatus, PreTestStatus, RuntimeStatus
from desktop_app.stress.pretest import (
    BASELINE_DEPENDENCIES,
    PreTestEnvironmentRunner,
    ReadinessResult,
    dependencies_for_tests,
    evaluate_readiness,
)
from desktop_app.stress.runner import StressTestRunner, scan_history
from desktop_app.stress.session import StressSessionManager


def _button(text: str, object_name: str = "OutlineButton") -> QPushButton:
    button = QPushButton(text)
    button.setObjectName(object_name)
    return button


def _set_button_style(button: QPushButton, object_name: str) -> None:
    if button.objectName() == object_name:
        return
    button.setObjectName(object_name)
    button.style().unpolish(button)
    button.style().polish(button)


def _safe_paragraphs(value: str) -> str:
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", value or "") if part.strip()]
    return "".join(f"<p>{html.escape(part).replace(chr(10), '<br>')}</p>" for part in paragraphs) or "<p>—</p>"


def _safe_procedure(value: str) -> str:
    lines = [line.strip() for line in (value or "").splitlines() if line.strip()]
    if not lines:
        return "<p>—</p>"
    return "".join(f"<div class='step'>{html.escape(line)}</div>" for line in lines)


def _format_seconds(seconds: int | None) -> str:
    if seconds is None:
        return "—"
    hours, remainder = divmod(max(0, seconds), 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def _display_status(status) -> str:
    value = status.value if hasattr(status, "value") else str(status)
    return value.replace("_", " ")


def _status_tone(status) -> str:
    value = _display_status(status).upper()
    if value in {"READY", "PASS", "PASSED", "COMPLETED", "CONNECTED", "BASELINE READY", "READY FOR SELECTED TEST"}:
        return "success"
    if value in {"NOT READY", "FAIL", "FAILED", "ERROR", "MISSING", "BLOCKED", "DISCONNECTED", "BASELINE NOT READY", "NOT READY FOR SELECTED TEST"}:
        return "error"
    if value in {"WARNING", "NEEDS REVIEW", "MANUAL REQUIRED"}:
        return "warning"
    if value in {"RUNNING", "STARTING", "STOPPING", "CONNECTING"}:
        return "running"
    if value == "STOPPED":
        return "stopped"
    return "neutral"


class StatusBadge(QLabel):
    def __init__(self, status="NOT RUN", parent=None):
        super().__init__(parent)
        self.setObjectName("StatusBadge")
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.set_status(status)

    def set_status(self, status) -> None:
        text = _display_status(status)
        self.setText(text)
        self.setProperty("tone", _status_tone(text))
        self.style().unpolish(self)
        self.style().polish(self)


def _badge_cell(status) -> QWidget:
    container = QWidget()
    layout = QHBoxLayout(container)
    layout.setContentsMargins(4, 2, 4, 2)
    badge = StatusBadge(status)
    layout.addWidget(badge)
    layout.addStretch()
    return container


class SortableTableItem(QTableWidgetItem):
    def __init__(self, text: str, sort_value=None):
        super().__init__(text)
        self.sort_value = text.casefold() if sort_value is None else sort_value

    def __lt__(self, other) -> bool:
        if isinstance(other, SortableTableItem):
            return self.sort_value < other.sort_value
        return super().__lt__(other)


class StressEvidenceViewer(QWidget):
    REFRESH_INTERVAL_MS = 10000

    def __init__(self, parent=None):
        super().__init__(parent)
        self.attempt_dir: Path | None = None
        self.records = {}
        self.tail = IncrementalLogTail(initial_lines=500)
        self._selected_id: str | None = None
        self._definition = None
        self._summary_parser = None
        self._build_ui()
        self.refresh_timer = QTimer(self)
        self.refresh_timer.setInterval(self.REFRESH_INTERVAL_MS)
        self.refresh_timer.timeout.connect(self.refresh_now)
        self.refresh_timer.start()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        title = QLabel("EVIDENCE LIVE VIEWER")
        title.setObjectName("CardTitle")
        root.addWidget(title)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.evidence_list = QListWidget()
        self.evidence_list.setMinimumWidth(230)
        self.evidence_list.currentItemChanged.connect(self._selection_changed)
        splitter.addWidget(self.evidence_list)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(8, 0, 0, 0)
        self.selected_label = QLabel("Selected: —")
        self.status_label = QLabel("Status: —")
        self.file_label = QLabel("File: —")
        self.file_label.setWordWrap(True)
        self.updated_label = QLabel("Updated: —    Refresh: 10 seconds")
        for widget in (self.selected_label, self.status_label, self.file_label, self.updated_label):
            right_layout.addWidget(widget)
        modes = QHBoxLayout()
        self.summary_button = _button("SUMMARY", "SmallButton")
        self.raw_button = _button("RAW LOG", "SmallButton")
        self.summary_button.setCheckable(True)
        self.raw_button.setCheckable(True)
        self.summary_button.clicked.connect(lambda: self._set_mode(True))
        self.raw_button.clicked.connect(lambda: self._set_mode(False))
        modes.addWidget(self.summary_button)
        modes.addWidget(self.raw_button)
        modes.addStretch()
        right_layout.addLayout(modes)
        controls = QHBoxLayout()
        self.auto_scroll = QCheckBox("AUTO SCROLL")
        self.auto_scroll.setChecked(True)
        self.lines_combo = QComboBox()
        self.lines_combo.addItems(["500", "1000", "2000"])
        self.lines_combo.currentTextChanged.connect(self._reload_selected)
        self.refresh_button = _button("REFRESH NOW", "SmallButton")
        self.copy_button = _button("COPY", "SmallButton")
        self.open_file_button = _button("OPEN FILE", "SmallButton")
        self.refresh_button.clicked.connect(self.refresh_now)
        self.copy_button.clicked.connect(self.copy_log)
        self.open_file_button.clicked.connect(self.open_file)
        controls.addWidget(self.auto_scroll)
        controls.addWidget(QLabel("Lines:"))
        controls.addWidget(self.lines_combo)
        controls.addStretch()
        controls.addWidget(self.refresh_button)
        controls.addWidget(self.copy_button)
        controls.addWidget(self.open_file_button)
        right_layout.addLayout(controls)
        self.log_view = QPlainTextEdit()
        self.log_view.setObjectName("LiveLog")
        self.log_view.setReadOnly(True)
        self.log_view.document().setMaximumBlockCount(2000)
        self.summary_view = QPlainTextEdit()
        self.summary_view.setObjectName("EvidenceSummary")
        self.summary_view.setReadOnly(True)
        self.summary_view.document().setMaximumBlockCount(500)
        self.content_stack = QStackedWidget()
        self.content_stack.addWidget(self.summary_view)
        self.content_stack.addWidget(self.log_view)
        right_layout.addWidget(self.content_stack, 1)
        self.open_artifact_folder_button = _button("OPEN ARTIFACT FOLDER", "SmallButton")
        self.open_artifact_folder_button.clicked.connect(self.open_artifact_folder)
        self.open_artifact_folder_button.hide()
        right_layout.addWidget(self.open_artifact_folder_button)
        splitter.addWidget(right)
        splitter.setStretchFactor(1, 1)
        root.addWidget(splitter, 1)

    def set_evidence(self, records, attempt_dir: str | Path, definition=None) -> None:
        self.attempt_dir = Path(attempt_dir)
        self._definition = definition
        self.records = {record.id: record for record in records}
        prior = self._selected_id
        self.evidence_list.clear()
        for record in records:
            item = QListWidgetItem(self._item_text(record))
            item.setData(Qt.ItemDataRole.UserRole, record.id)
            self.evidence_list.addItem(item)
            if record.id == prior:
                self.evidence_list.setCurrentItem(item)
        if self.evidence_list.currentItem() is None and self.evidence_list.count():
            self.evidence_list.setCurrentRow(0)

    def update_evidence(self, record) -> None:
        self.records[record.id] = record
        for index in range(self.evidence_list.count()):
            item = self.evidence_list.item(index)
            if item.data(Qt.ItemDataRole.UserRole) == record.id:
                item.setText(self._item_text(record))
                break
        if record.id == self._selected_id:
            self._update_metadata(record)
            self._render_summary(record)

    @staticmethod
    def _item_text(record) -> str:
        glyph = {
            CollectorStatus.WARNING: "⚠",
            CollectorStatus.ERROR: "✕",
            CollectorStatus.MANUAL_REQUIRED: "◇",
            CollectorStatus.RUNNING: "●",
            CollectorStatus.STARTING: "●",
            CollectorStatus.COMPLETED: "●",
        }.get(record.status, "○")
        return f"{glyph}  {record.name}    {_display_status(record.status)}"

    def _selection_changed(self, current, _previous) -> None:
        if current is None or self.attempt_dir is None:
            return
        self._selected_id = current.data(Qt.ItemDataRole.UserRole)
        record = self.records[self._selected_id]
        self._update_metadata(record)
        path = self.attempt_dir / record.file
        content = self.tail.select(path, initial_lines=int(self.lines_combo.currentText()))
        self.log_view.setPlainText(content)
        self._configure_summary(record, content)
        self._scroll_to_end()

    def _reload_selected(self) -> None:
        item = self.evidence_list.currentItem()
        if item is not None:
            self._selection_changed(item, None)

    def _update_metadata(self, record) -> None:
        path = self.attempt_dir / record.file if self.attempt_dir else Path(record.file)
        updated = record.last_update
        try:
            modified = datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat(timespec="seconds")
            updated = max(updated or "", modified)
        except OSError:
            pass
        self.selected_label.setText(f"Selected: {record.name}")
        self.status_label.setText(f"Status: {record.status.value}")
        self.file_label.setText(f"File: {path}")
        self.updated_label.setText(f"Updated: {updated or '—'}    Refresh: 10 seconds")

    def refresh_now(self) -> None:
        if self._selected_id is None:
            return
        content = self.tail.read_new()
        if content:
            self.log_view.moveCursor(QTextCursor.MoveOperation.End)
            self.log_view.insertPlainText(content)
            if self._summary_parser is not None:
                self._summary_parser.feed(content)
            record = self.records.get(self._selected_id)
            if record:
                self._update_metadata(record)
                self._render_summary(record)
            self._scroll_to_end()

    def _configure_summary(self, record, content: str) -> None:
        self._summary_parser = None
        supported = False
        if record.collector == "system_metrics":
            self._summary_parser = SystemMetricsSummary(
                target_cpu_percent=target_cpu_percent(self._definition)
            )
            supported = True
        elif record.collector == "dmesg":
            self._summary_parser = KernelWarningSummary()
            supported = True
        if self._summary_parser is not None:
            self._summary_parser.feed(content)
        manual = bool(record.manual_required or record.status == CollectorStatus.MANUAL_REQUIRED)
        self.summary_button.setVisible(supported or manual)
        self.raw_button.setVisible(supported or manual)
        self.open_artifact_folder_button.setVisible(manual)
        if manual:
            self.summary_view.setPlainText(
                "MANUAL EVIDENCE REQUIRED\n\n"
                "This evidence is not collected automatically.\n\n"
                f"Evidence: {record.name}\n\n"
                "Use OPEN ARTIFACT FOLDER to review or add approved supporting files."
            )
            self._set_mode(True)
        elif supported:
            self._render_summary(record)
            self._set_mode(True)
        else:
            self.summary_view.clear()
            self._set_mode(False)

    def _render_summary(self, record) -> None:
        if isinstance(self._summary_parser, SystemMetricsSummary):
            text = self._summary_parser.render(
                _display_status(record.status),
                refresh_seconds=self.REFRESH_INTERVAL_MS // 1000,
            )
        elif isinstance(self._summary_parser, KernelWarningSummary):
            text = self._summary_parser.render(_display_status(record.status))
        else:
            return
        self.summary_view.setPlainText(text)

    def _set_mode(self, summary: bool) -> None:
        self.summary_button.setChecked(summary)
        self.raw_button.setChecked(not summary)
        self.content_stack.setCurrentIndex(0 if summary else 1)

    def _scroll_to_end(self) -> None:
        if self.auto_scroll.isChecked():
            bar = self.log_view.verticalScrollBar()
            bar.setValue(bar.maximum())

    def copy_log(self) -> None:
        view = self.summary_view if self.content_stack.currentIndex() == 0 else self.log_view
        cursor = view.textCursor()
        QApplication.clipboard().setText(cursor.selectedText() if cursor.hasSelection() else view.toPlainText())

    def open_file(self) -> None:
        if self.tail.path and self.tail.path.exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.tail.path)))

    def open_artifact_folder(self) -> None:
        if self.attempt_dir is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.attempt_dir / "artifacts")))


class _LocalPreTestThread(QThread):
    completed = Signal(object)
    failed = Signal(str)

    def __init__(self, runner, output_dir, dependencies, parent=None):
        super().__init__(parent)
        self.runner = runner
        self.output_dir = output_dir
        self.dependencies = dependencies

    def run(self) -> None:
        try:
            self.completed.emit(self.runner.run_local(self.output_dir, self.dependencies))
        except Exception as exc:
            self.failed.emit(f"{type(exc).__name__}: {exc}")


class PreTestEnvironmentPage(QWidget):
    result_changed = Signal(object)
    override_changed = Signal(bool)
    dashboard_requested = Signal()

    def __init__(self, session_manager: StressSessionManager, remote_service=None, parent=None):
        super().__init__(parent)
        self.session_manager = session_manager
        self.remote_service = remote_service
        self.required_dependencies = set(BASELINE_DEPENDENCIES)
        self.selected_test_ids: tuple[str, ...] = ()
        self.result: ReadinessResult | None = None
        self.override = False
        self._thread = None
        self._remote_request_id = None
        self._build_ui()
        if remote_service is not None:
            remote_service.operation_succeeded.connect(self._remote_succeeded)
            remote_service.operation_failed.connect(self._remote_failed)

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(8)
        summary = QFrame()
        summary.setObjectName("StressPanel")
        summary_layout = QHBoxLayout(summary)
        summary_layout.setContentsMargins(12, 8, 12, 8)
        summary_layout.setSpacing(12)
        summary_layout.addWidget(QLabel("READINESS"))
        self.readiness_label = StatusBadge("NOT CHECKED")
        summary_layout.addWidget(self.readiness_label)
        self.count_label = QLabel("Passed 0    Warning 0    Failed 0    Missing 0")
        summary_layout.addWidget(self.count_label)
        summary_layout.addStretch()
        self.reason_label = QLabel("Run the baseline checks before executing stress tests.")
        self.reason_label.setObjectName("Muted")
        self.reason_label.setMaximumWidth(430)
        self.reason_label.setToolTip(self.reason_label.text())
        summary_layout.addWidget(self.reason_label)
        root.addWidget(summary)

        baseline_panel = QFrame()
        baseline_panel.setObjectName("StressPanel")
        baseline_layout = QVBoxLayout(baseline_panel)
        baseline_layout.setContentsMargins(10, 6, 10, 6)
        baseline_header = QHBoxLayout()
        baseline_title = QLabel("SYSTEM BASELINE")
        baseline_title.setObjectName("CardTitle")
        self.baseline_status = StatusBadge("NOT MEASURED")
        baseline_header.addWidget(baseline_title)
        baseline_header.addWidget(self.baseline_status)
        baseline_header.addStretch()
        baseline_layout.addLayout(baseline_header)
        self.baseline_summary = QPlainTextEdit()
        self.baseline_summary.setReadOnly(True)
        self.baseline_summary.setMaximumHeight(170)
        self.baseline_summary.setPlainText("Run baseline checks to measure the current DUT operating state.")
        baseline_layout.addWidget(self.baseline_summary)
        root.addWidget(baseline_panel)

        self.dut_label = QLabel("DUT INFORMATION\nNot checked", self)
        self.dut_label.hide()
        content_splitter = QSplitter(Qt.Orientation.Horizontal)

        checks_panel = QFrame()
        checks_panel.setObjectName("StressPanel")
        checks_layout = QVBoxLayout(checks_panel)
        checks_layout.setContentsMargins(10, 8, 10, 8)
        checks_layout.setSpacing(6)
        checks_title = QLabel("CHECK LIST")
        checks_title.setObjectName("CardTitle")
        checks_layout.addWidget(checks_title)
        self.check_table = QTableWidget(0, 3)
        self.check_table.setObjectName("StressTable")
        self.check_table.setHorizontalHeaderLabels(["Check", "Actual", "Status"])
        self.check_table.setWordWrap(False)
        self.check_table.verticalHeader().setVisible(False)
        self.check_table.verticalHeader().setDefaultSectionSize(31)
        self.check_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.check_table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.check_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.check_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.check_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.check_table.currentCellChanged.connect(self._show_check_detail)
        self.check_table.itemSelectionChanged.connect(self._update_action_hierarchy)
        checks_layout.addWidget(self.check_table)
        checks_layout.addStretch(1)

        detail_panel = QFrame()
        detail_panel.setObjectName("StressPanel")
        detail_layout = QVBoxLayout(detail_panel)
        detail_layout.setContentsMargins(10, 8, 10, 8)
        detail_layout.setSpacing(6)
        detail_title = QLabel("CHECK DETAILS")
        detail_title.setObjectName("CardTitle")
        detail_layout.addWidget(detail_title)
        self.pretest_detail_tabs = QTabWidget()
        self.pretest_detail_tabs.setObjectName("StressDetailTabs")
        check_detail_page = QWidget()
        check_detail_layout = QVBoxLayout(check_detail_page)
        check_detail_layout.setContentsMargins(4, 6, 4, 4)
        self.detail_area = QPlainTextEdit()
        self.detail_area.setReadOnly(True)
        self.detail_area.setPlaceholderText("Select a check to view its full result.")
        self.detail_area.setPlainText("Select a check to view details and remediation.")
        check_detail_layout.addWidget(self.detail_area, 1)
        self.go_dashboard_button = _button("GO TO DASHBOARD / CONNECT NOW", "WarningButton")
        self.go_dashboard_button.setToolTip("Open Dashboard and use the existing DUT connection controls.")
        self.go_dashboard_button.clicked.connect(self.dashboard_requested.emit)
        self.go_dashboard_button.hide()
        check_detail_layout.addWidget(self.go_dashboard_button)
        self.dut_info_view = QTextBrowser()
        self.dut_info_view.setObjectName("StressRichText")
        self.dut_info_view.setPlainText("Run the baseline checks to view DUT information.")
        self.pretest_detail_tabs.addTab(check_detail_page, "SELECTED CHECK")
        self.pretest_detail_tabs.addTab(self.dut_info_view, "DUT INFORMATION")
        detail_layout.addWidget(self.pretest_detail_tabs, 1)

        content_splitter.addWidget(checks_panel)
        content_splitter.addWidget(detail_panel)
        content_splitter.setStretchFactor(0, 3)
        content_splitter.setStretchFactor(1, 2)
        content_splitter.setSizes([720, 430])
        root.addWidget(content_splitter, 1)

        actions = QHBoxLayout()
        self.run_all_button = _button("RUN ALL CHECKS", "PrimaryButton")
        self.run_selected_button = _button("RUN SELECTED")
        self.run_selected_button.setEnabled(False)
        self.export_button = _button("EXPORT BASELINE")
        self.view_details_button = _button("VIEW DETAILS")
        self.override_button = _button("OVERRIDE AND CONTINUE", "WarningButton")
        self.override_button.setVisible(False)
        self.run_all_button.clicked.connect(self.run_checks)
        self.run_selected_button.clicked.connect(self.run_checks)
        self.export_button.clicked.connect(self.open_baseline)
        self.view_details_button.clicked.connect(self.show_environment_details)
        self.override_button.clicked.connect(self.apply_override)
        for button in (self.run_all_button, self.run_selected_button, self.export_button, self.view_details_button, self.override_button):
            actions.addWidget(button)
        actions.addStretch()
        root.addLayout(actions)

    def _update_action_hierarchy(self) -> None:
        has_selection = bool(self.check_table.selectionModel().selectedRows())
        self.run_selected_button.setEnabled(has_selection)
        _set_button_style(self.run_selected_button, "PrimaryButton" if has_selection else "OutlineButton")
        _set_button_style(self.run_all_button, "OutlineButton" if has_selection else "PrimaryButton")

    @property
    def environment_status(self) -> EnvironmentStatus:
        return self.result.status if self.result else EnvironmentStatus.NOT_CHECKED

    @property
    def blocking_reasons(self) -> list[str]:
        return list(self.result.blocking_reasons) if self.result else ["Pre-test environment has not been checked"]

    def set_required_tests(self, definitions) -> None:
        definitions = list(definitions)
        self.selected_test_ids = tuple(definition.test_id for definition in definitions)
        self.required_dependencies = set(BASELINE_DEPENDENCIES)
        self.required_dependencies.update(dependencies_for_tests(definitions))
        workload_definitions = [definition for definition in definitions if "workload" in definition.dependencies]
        if workload_definitions and all(definition.test_id in SUPPORTED_ADAPTIVE_CPU_IDS for definition in workload_definitions):
            # stress-ng is conditional on the immediate baseline decision.
            self.required_dependencies.discard("workload")
        if self.result:
            status, reasons = evaluate_readiness(self.result.checks, self.required_dependencies)
            self.result.status = status
            self.result.blocking_reasons = reasons
            self.override = False
            if self.session_manager.session_dir:
                self.session_manager.update_environment(status, reasons)
            self._render(self.result)
            self.result_changed.emit(self.result)

    def run_checks(self) -> None:
        valid, detail = self.session_manager.validate_evidence_root()
        if not valid:
            self.reason_label.setText(detail)
            return
        if self.session_manager.session_dir is None:
            self.session_manager.create_session("VD")
        output_dir = self.session_manager.pretest_dir()
        self.readiness_label.set_status("RUNNING")
        self.run_all_button.setEnabled(False)
        if self.remote_service is not None:
            if getattr(self.remote_service, "is_connected", False):
                operation = lambda ssh: PreTestEnvironmentRunner.remote_operation(ssh, self.required_dependencies)
                self._remote_request_id = self.remote_service.submit_operation("stress_pretest", operation)
                if self._remote_request_id is None:
                    self._failed("Connected target became unavailable")
            else:
                self._completed(PreTestEnvironmentRunner.disconnected_result(output_dir))
        else:
            self._thread = _LocalPreTestThread(PreTestEnvironmentRunner(), output_dir, set(self.required_dependencies), self)
            self._thread.completed.connect(self._completed)
            self._thread.failed.connect(self._failed)
            self._thread.finished.connect(self._thread.deleteLater)
            self._thread.start()

    def _remote_succeeded(self, request_id: str, payload) -> None:
        if request_id != self._remote_request_id:
            return
        try:
            result = PreTestEnvironmentRunner.result_from_remote(payload, self.session_manager.pretest_dir(), self.required_dependencies)
        except Exception as exc:
            self._failed(f"{type(exc).__name__}: {exc}")
            return
        self._completed(result)

    def _remote_failed(self, request_id: str, error: str) -> None:
        if request_id == self._remote_request_id:
            self._failed(error)

    def _completed(self, result: ReadinessResult) -> None:
        self.result = result
        self.override = False
        self.run_all_button.setEnabled(True)
        self.session_manager.update_environment(result.status, result.blocking_reasons)
        self._render(result)
        self.result_changed.emit(result)

    def _failed(self, error: str) -> None:
        self.readiness_label.set_status("NOT READY")
        self.reason_label.setText("Baseline failed. Open details for the full error.")
        self.reason_label.setToolTip(error)
        self.detail_area.setPlainText(error)
        self.run_all_button.setEnabled(True)

    def _render(self, result: ReadinessResult) -> None:
        if self.selected_test_ids:
            readiness_text = (
                "READY FOR SELECTED TEST"
                if result.status == EnvironmentStatus.READY
                else "NOT READY FOR SELECTED TEST"
            )
        else:
            readiness_text = "BASELINE READY" if result.status == EnvironmentStatus.READY else "BASELINE NOT READY"
        self.readiness_label.set_status(readiness_text)
        counts = {status: 0 for status in PreTestStatus}
        for check in result.checks:
            counts[check.status] += 1
        self.count_label.setText(
            f"Passed {counts[PreTestStatus.PASS]}    Warning {counts[PreTestStatus.WARNING]}    "
            f"Failed {counts[PreTestStatus.FAIL]}    Missing {counts[PreTestStatus.MISSING]}"
        )
        if result.blocking_reasons:
            summary = f"{len(result.blocking_reasons)} blocking issue(s). Select a row or View Details."
            tooltip = "\n".join(result.blocking_reasons)
        elif self.selected_test_ids:
            summary = "Dependencies for the selected test are ready."
            tooltip = summary
        else:
            summary = "General baseline is ready. Select a test case for scoped readiness."
            tooltip = summary
        self.reason_label.setText(summary)
        self.reason_label.setToolTip(tooltip)
        if result.baseline:
            self.baseline_status.set_status("COMPLETED")
            self.baseline_summary.setPlainText(baseline_summary_text(result.baseline))
        else:
            self.baseline_status.set_status("NOT MEASURED")
            self.baseline_summary.setPlainText("No valid system baseline is available.")
        self.dut_label.setText("DUT INFORMATION\n" + "    ".join(f"{key}: {value}" for key, value in result.dut_info.items()))
        if result.dut_info:
            self.dut_info_view.setHtml(
                "<h3>DUT Information</h3>"
                + "".join(
                    f"<p><b>{html.escape(str(key))}:</b> {html.escape(str(value))}</p>"
                    for key, value in result.dut_info.items()
                )
            )
        else:
            self.dut_info_view.setPlainText("No DUT information was available.")
        self.check_table.setSortingEnabled(False)
        self.check_table.setRowCount(len(result.checks))
        for row, check in enumerate(result.checks):
            self.check_table.setItem(row, 0, QTableWidgetItem(check.name))
            actual = QTableWidgetItem(check.actual)
            actual.setToolTip(check.actual)
            self.check_table.setItem(row, 1, actual)
            status_item = SortableTableItem(_display_status(check.status))
            self.check_table.setItem(row, 2, status_item)
            self.check_table.setCellWidget(row, 2, _badge_cell(check.status))
        header_height = self.check_table.horizontalHeader().height() or 30
        rows_height = min(max(len(result.checks), 1), 10) * self.check_table.verticalHeader().defaultSectionSize()
        self.check_table.setMaximumHeight(header_height + rows_height + 8)
        self.override_button.setVisible(result.status == EnvironmentStatus.NOT_READY)
        self.go_dashboard_button.hide()
        self.detail_area.setPlainText("Select a check to view details and remediation.")
        self._update_action_hierarchy()

    def _show_check_detail(self, row: int, _column: int, _old_row: int, _old_column: int) -> None:
        if self.result is None or not 0 <= row < len(self.result.checks):
            self.detail_area.setPlainText("Select a check to view details and remediation.")
            self.go_dashboard_button.hide()
            return
        check = self.result.checks[row]
        self.pretest_detail_tabs.setCurrentIndex(0)
        remediation = self._remediation_for(check)
        self.detail_area.setPlainText(
            f"Check: {check.name}\nStatus: {_display_status(check.status)}\n\n"
            f"Actual / Reason\n{check.actual}\n\nRemediation\n{remediation}"
        )
        self.go_dashboard_button.setVisible(
            check.id == "target_connection" and check.status == PreTestStatus.FAIL
        )

    @staticmethod
    def _remediation_for(check) -> str:
        if check.id == "target_connection" and check.status == PreTestStatus.FAIL:
            return "Go to Dashboard and connect using the existing DUT connection controls."
        if check.status == PreTestStatus.MISSING:
            return "Install or provide the required tool through the approved system setup process, then run checks again."
        if check.status == PreTestStatus.NOT_CONFIGURED:
            return "Configure this dependency for the selected target before running dependent tests."
        if check.status == PreTestStatus.FAIL:
            return "Resolve the reported condition, then run the selected check again."
        return "No remediation is currently required."

    def show_environment_details(self) -> None:
        if self.result is None:
            self.dut_info_view.setPlainText("Run the baseline checks to view environment details.")
        else:
            dut = "\n".join(f"{key}: {value}" for key, value in self.result.dut_info.items())
            reasons = "\n".join(self.result.blocking_reasons) or "None"
            self.dut_info_view.setPlainText(
                f"DUT INFORMATION\n{dut}\n\nBLOCKING REASONS\n{reasons}"
            )
        self.go_dashboard_button.hide()
        self.pretest_detail_tabs.setCurrentIndex(1)

    def apply_override(self) -> None:
        if self.result is None or self.result.status != EnvironmentStatus.NOT_READY:
            return
        self.override = True
        if self.session_manager.session_dir:
            self.session_manager.update_environment(self.result.status, self.result.blocking_reasons, override=True)
        self.reason_label.setText("OVERRIDE RECORDED — blocking reasons remain saved.")
        self.override_changed.emit(True)

    def open_baseline(self) -> None:
        if self.session_manager.session_dir:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.session_manager.pretest_dir())))


class PlaceholderStressPage(QWidget):
    def __init__(self, platform: str, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        title = QLabel(f"{platform} SYSTEM STRESS TEST")
        title.setObjectName("PageTitle")
        message = QLabel(f"No {platform} Test Case Catalog loaded.")
        message.setObjectName("Muted")
        layout.addWidget(title)
        layout.addWidget(message)
        layout.addStretch()


class VDCatalogPage(QWidget):
    selected_changed = Signal(object)
    run_requested = Signal(object)
    start_requested = Signal()
    stop_requested = Signal()
    finish_requested = Signal()

    def __init__(self, catalog: StressCatalog, parent=None):
        super().__init__(parent)
        self.catalog = catalog
        self.visible_definitions = []
        self._selected_ids: set[str] = set()
        self._column_visibility_user_set = False
        self._build_ui()
        if self.catalog.source_warnings:
            self.message_label.setText(f"Source warning ({self.catalog.encoding})")
            self.message_label.setToolTip("\n".join(self.catalog.source_warnings))
        self.refresh_table()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(6)
        title = QLabel("VD SYSTEM STRESS TEST")
        title.setObjectName("PageTitle")
        root.addWidget(title)
        self.message_label = QLabel("")
        self.message_label.setObjectName("Muted")
        root.addWidget(self.message_label)
        self.view_stack = QStackedWidget()
        catalog_view = QWidget()
        catalog_layout = QVBoxLayout(catalog_view)
        catalog_layout.setContentsMargins(0, 0, 0, 0)
        catalog_layout.setSpacing(6)

        self.active_banner = QFrame()
        self.active_banner.setObjectName("StressPanel")
        active_layout = QHBoxLayout(self.active_banner)
        active_layout.setContentsMargins(10, 6, 10, 6)
        self.active_test_label = QLabel("● —")
        self.active_test_label.setObjectName("CardTitle")
        self.active_status = StatusBadge("RUNNING")
        self.active_metrics_label = QLabel("Elapsed: 00:00:00 / —    Evidence: 0 Active    Warnings: 0    Errors: 0")
        self.view_running_button = _button("VIEW RUNNING TEST", "PrimaryButton")
        self.view_running_button.clicked.connect(self._view_running_test)
        active_layout.addWidget(self.active_test_label)
        active_layout.addWidget(self.active_status)
        active_layout.addWidget(self.active_metrics_label)
        active_layout.addStretch()
        active_layout.addWidget(self.view_running_button)
        self.active_banner.hide()
        catalog_layout.addWidget(self.active_banner)

        filter_bar = QFrame()
        filter_bar.setObjectName("StressFilterBar")
        filters = QHBoxLayout(filter_bar)
        filters.setContentsMargins(10, 7, 10, 7)
        filters.setSpacing(7)
        self.group_combo = QComboBox()
        self.group_combo.addItem("All Groups")
        self.group_combo.addItems(sorted({item.group for item in self.catalog.definitions}))
        self.status_combo = QComboBox()
        self.status_combo.addItem("All")
        self.status_combo.addItems([status.value for status in RuntimeStatus])
        self.severity_combo = QComboBox()
        self.severity_combo.addItem("All")
        self.severity_combo.addItems(sorted({item.severity for item in self.catalog.definitions if item.severity}))

        filters.addWidget(QLabel("Group"))
        filters.addWidget(self.group_combo, 2)
        filters.addWidget(QLabel("Status"))
        filters.addWidget(self.status_combo, 1)
        filters.addWidget(QLabel("Severity"))
        filters.addWidget(self.severity_combo, 1)
        filters.addStretch()
        self.reset_button = _button("RESET", "SmallButton")
        self.columns_button = QToolButton()
        self.columns_button.setText("COLUMNS ▾")
        self.columns_button.setObjectName("SmallButton")
        self.columns_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.columns_menu = QMenu(self.columns_button)
        self.columns_button.setMenu(self.columns_menu)
        self.refresh_button = _button("REFRESH", "SmallButton")
        filters.addWidget(self.reset_button)
        filters.addWidget(self.columns_button)
        filters.addWidget(self.refresh_button)
        catalog_layout.addWidget(filter_bar)

        self.select_visible = QCheckBox("Select All Visible")
        self.select_visible.toggled.connect(self._select_all_visible)
        catalog_layout.addWidget(self.select_visible)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.catalog_splitter = splitter
        self.table = QTableWidget(0, 7)
        self.table.setObjectName("StressTable")
        self.table.setHorizontalHeaderLabels(["Select", "Test ID", "Group", "Test Name", "Duration", "Severity", "Status"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setWordWrap(False)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(30)
        header = self.table.horizontalHeader()
        header.setSectionsClickable(True)
        header.setSortIndicatorShown(True)
        header.setMinimumSectionSize(62)
        for column in range(self.table.columnCount()):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.table.setColumnWidth(0, 66)
        self.table.setColumnWidth(1, 112)
        self.table.setColumnWidth(2, 175)
        self.table.setColumnWidth(4, 78)
        self.table.setColumnWidth(5, 82)
        self.table.setColumnWidth(6, 110)
        self.table.setSortingEnabled(True)
        self.table.itemSelectionChanged.connect(self.render_details)
        self.table.itemChanged.connect(self._selection_count_changed)
        splitter.addWidget(self.table)

        details = QFrame()
        details.setObjectName("StressPanel")
        details_layout = QVBoxLayout(details)
        details_layout.setContentsMargins(10, 8, 10, 8)
        details_layout.setSpacing(6)
        details_title = QLabel("TEST CASE DETAILS")
        details_title.setObjectName("CardTitle")
        details_layout.addWidget(details_title)

        self.details_stack = QStackedWidget()
        placeholder = QLabel("Select a test case to view details.")
        placeholder.setObjectName("Muted")
        placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.details_stack.addWidget(placeholder)
        self.detail_tabs = QTabWidget()
        self.detail_tabs.setObjectName("StressDetailTabs")
        self.overview_view = self._rich_text_view()
        self.procedure_view = self._rich_text_view()
        self.criteria_view = self._rich_text_view()
        evidence_page = QWidget()
        evidence_layout = QVBoxLayout(evidence_page)
        evidence_layout.setContentsMargins(4, 6, 4, 4)
        self.evidence_plan_list = QListWidget()
        evidence_layout.addWidget(self.evidence_plan_list)
        for label, widget in (
            ("OVERVIEW", self.overview_view),
            ("PROCEDURE", self.procedure_view),
            ("CRITERIA", self.criteria_view),
            ("EVIDENCE", evidence_page),
        ):
            self.detail_tabs.addTab(widget, label)
        self.details_stack.addWidget(self.detail_tabs)
        details_layout.addWidget(self.details_stack, 1)
        self.details_view = self.overview_view
        self.evidence_plan_box = evidence_page
        splitter.addWidget(details)
        splitter.setChildrenCollapsible(False)
        splitter.setStretchFactor(0, 5)
        splitter.setStretchFactor(1, 3)
        splitter.setSizes([820, 440])
        catalog_layout.addWidget(splitter, 1)

        footer = QHBoxLayout()
        self.selected_label = QLabel("Selected 0  •  Visible 0  •  Total 0")
        self.environment_caption = QLabel("Environment")
        self.environment_label = StatusBadge("NOT CHECKED")
        self.details_button = _button("TEST DETAILS")
        self.history_button = _button("HISTORY")
        self.run_button = _button("RUN SELECTED", "PrimaryButton")
        self.details_button.clicked.connect(self.render_details)
        self.history_button.clicked.connect(self.show_history)
        self.run_button.clicked.connect(lambda: self.run_requested.emit(self.selected_definitions()))
        footer.addWidget(self.selected_label)
        footer.addSpacing(8)
        footer.addWidget(self.environment_caption)
        footer.addWidget(self.environment_label)
        footer.addStretch()
        footer.addWidget(self.details_button)
        footer.addWidget(self.history_button)
        footer.addWidget(self.run_button)
        catalog_layout.addLayout(footer)
        self.view_stack.addWidget(catalog_view)
        self.confirmation_view = self._build_confirmation()
        self.view_stack.addWidget(self.confirmation_view)
        self.execution_view = self._build_execution()
        self.view_stack.addWidget(self.execution_view)
        self.history_view = self._build_history()
        self.view_stack.addWidget(self.history_view)
        root.addWidget(self.view_stack, 1)

        self._build_column_menu()
        self.group_combo.currentTextChanged.connect(self.refresh_table)
        self.severity_combo.currentTextChanged.connect(self.refresh_table)
        self.status_combo.currentTextChanged.connect(self.refresh_table)
        self.reset_button.clicked.connect(self.reset_filters)
        self.refresh_button.clicked.connect(self.refresh_table)

    @staticmethod
    def _rich_text_view() -> QTextBrowser:
        view = QTextBrowser()
        view.setObjectName("StressRichText")
        view.setOpenExternalLinks(False)
        view.setPlaceholderText("Select a test case to view details.")
        return view

    def _build_column_menu(self) -> None:
        labels = ("Select", "Test ID", "Group", "Test Name", "Duration", "Severity", "Status")
        essential = {0, 1, 3, 6}
        self.column_actions = {}
        for column, label in enumerate(labels):
            action = QAction(label, self.columns_menu)
            action.setCheckable(True)
            action.setChecked(True)
            if column in essential:
                action.setEnabled(False)
            else:
                action.toggled.connect(lambda visible, value=column: self._set_optional_column(value, visible))
            self.columns_menu.addAction(action)
            self.column_actions[column] = action

    def _set_optional_column(self, column: int, visible: bool) -> None:
        self._column_visibility_user_set = True
        self.table.setColumnHidden(column, not visible)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self._column_visibility_user_set:
            return
        show_optional = event.size().width() >= 1080
        for column in (2, 4, 5):
            action = self.column_actions.get(column)
            if action is not None:
                action.blockSignals(True)
                action.setChecked(show_optional)
                action.blockSignals(False)
            self.table.setColumnHidden(column, not show_optional)

    def reset_filters(self) -> None:
        for combo in (self.group_combo, self.status_combo, self.severity_combo):
            combo.blockSignals(True)
        self.group_combo.setCurrentIndex(0)
        self.status_combo.setCurrentText("All")
        self.severity_combo.setCurrentIndex(0)
        for combo in (self.group_combo, self.status_combo, self.severity_combo):
            combo.blockSignals(False)
        self.refresh_table()

    def _build_confirmation(self) -> QWidget:
        view = QWidget()
        layout = QVBoxLayout(view)
        heading = QLabel("RUN CONFIRMATION")
        heading.setObjectName("PageTitle")
        self.confirmation_text = QPlainTextEdit()
        self.confirmation_text.setReadOnly(True)
        self.confirmation_plan = QListWidget()
        actions = QHBoxLayout()
        back = _button("BACK")
        self.confirmation_cancel_button = _button("CANCEL ATTEMPT", "DangerButton")
        self.confirmation_start_button = _button("START TEST", "PrimaryButton")
        back.clicked.connect(lambda: self.view_stack.setCurrentIndex(0))
        self.confirmation_cancel_button.clicked.connect(self.stop_requested.emit)
        self.confirmation_start_button.clicked.connect(self.start_requested.emit)
        actions.addWidget(back)
        actions.addWidget(self.confirmation_cancel_button)
        actions.addStretch()
        actions.addWidget(self.confirmation_start_button)
        layout.addWidget(heading)
        layout.addWidget(self.confirmation_text)
        layout.addWidget(QLabel("Evidence Plan"))
        layout.addWidget(self.confirmation_plan)
        layout.addLayout(actions)
        return view

    def _build_execution(self) -> QWidget:
        view = QWidget()
        layout = QVBoxLayout(view)
        self.execution_title = QLabel("—")
        self.execution_title.setObjectName("PageTitle")
        self.execution_status = StatusBadge("NOT RUNNING")
        self.execution_times = QLabel("Elapsed: 00:00:00")
        self.execution_counts = QLabel("Evidence: 0 Active    Warnings: 0    Errors: 0")
        strategy_panel = QFrame()
        strategy_panel.setObjectName("StressPanel")
        strategy_layout = QVBoxLayout(strategy_panel)
        strategy_layout.setContentsMargins(10, 6, 10, 6)
        strategy_title = QLabel("BASELINE / TARGET")
        strategy_title.setObjectName("CardTitle")
        self.strategy_summary_label = QLabel("Not applicable")
        self.strategy_summary_label.setWordWrap(True)
        strategy_layout.addWidget(strategy_title)
        strategy_layout.addWidget(self.strategy_summary_label)
        self.strategy_panel = strategy_panel
        self.queue_list = QListWidget()
        self.queue_list.setMaximumHeight(100)
        actions = QHBoxLayout()
        self.back_to_list_button = _button("← BACK TO TEST LIST")
        stop = _button("STOP TEST", "DangerButton")
        finish = _button("FINISH / REVIEW")
        self.open_test_folder_button = _button("OPEN TEST FOLDER")
        stop.clicked.connect(self.stop_requested.emit)
        finish.clicked.connect(self.finish_requested.emit)
        self.back_to_list_button.clicked.connect(self._back_to_test_list)
        self.open_test_folder_button.clicked.connect(self._open_active_folder)
        actions.addWidget(self.back_to_list_button)
        actions.addWidget(stop)
        actions.addWidget(finish)
        actions.addWidget(self.open_test_folder_button)
        actions.addStretch()
        self.evidence_viewer = StressEvidenceViewer()
        layout.addWidget(self.execution_title)
        layout.addWidget(self.execution_status)
        layout.addWidget(self.execution_times)
        layout.addWidget(self.execution_counts)
        layout.addWidget(strategy_panel)
        layout.addWidget(QLabel("EXECUTION QUEUE"))
        layout.addWidget(self.queue_list)
        layout.addLayout(actions)
        layout.addWidget(self.evidence_viewer, 1)
        return view

    def _build_history(self) -> QWidget:
        view = QWidget()
        layout = QVBoxLayout(view)
        heading = QLabel("TEST HISTORY")
        heading.setObjectName("PageTitle")
        self.history_table = QTableWidget(0, 6)
        self.history_table.setHorizontalHeaderLabels(["Session", "Attempt", "Start", "End", "Status", "Evidence Folder"])
        self.history_table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        back = _button("BACK")
        back.clicked.connect(lambda: self.view_stack.setCurrentIndex(0))
        layout.addWidget(heading)
        layout.addWidget(self.history_table)
        layout.addWidget(back)
        return view

    def refresh_table(self, *_args) -> None:
        header = self.table.horizontalHeader()
        sort_column = header.sortIndicatorSection()
        sort_order = header.sortIndicatorOrder()
        self.visible_definitions = self.catalog.filter(
            group=self.group_combo.currentText(), status=self.status_combo.currentText(), severity=self.severity_combo.currentText()
        )
        sorting = self.table.isSortingEnabled()
        self.table.setSortingEnabled(False)
        self.table.blockSignals(True)
        self.table.setRowCount(len(self.visible_definitions))
        for row, definition in enumerate(self.visible_definitions):
            check = QTableWidgetItem()
            check.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable)
            check.setCheckState(Qt.CheckState.Checked if definition.test_id in self._selected_ids else Qt.CheckState.Unchecked)
            check.setData(Qt.ItemDataRole.UserRole, definition.test_id)
            self.table.setItem(row, 0, check)
            values = (
                SortableTableItem(definition.test_id),
                SortableTableItem(definition.group),
                SortableTableItem(definition.test_name),
                SortableTableItem(definition.duration_text, definition.duration_seconds if definition.duration_seconds is not None else float("inf")),
                SortableTableItem(definition.severity),
                SortableTableItem(_display_status(definition.runtime_status)),
            )
            for column, item in enumerate(values, start=1):
                item.setData(Qt.ItemDataRole.UserRole, definition.test_id)
                self.table.setItem(row, column, item)
            self.table.setCellWidget(row, 6, _badge_cell(definition.runtime_status))
        self.table.blockSignals(False)
        self.table.setSortingEnabled(sorting)
        if sorting and sort_column >= 0:
            self.table.sortItems(sort_column, sort_order)
        self.clear_details()
        self._selection_count_changed()

    def selected_definitions(self):
        return [item for item in self.catalog.definitions if item.test_id in self._selected_ids]

    def _select_all_visible(self, checked: bool) -> None:
        for row in range(self.table.rowCount()):
            self.table.item(row, 0).setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)

    def _selection_count_changed(self, *_args) -> None:
        visible_ids = {definition.test_id for definition in getattr(self, "visible_definitions", [])}
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            test_id = item.data(Qt.ItemDataRole.UserRole) if item else None
            if item and item.checkState() == Qt.CheckState.Checked:
                self._selected_ids.add(test_id)
            elif test_id in visible_ids:
                self._selected_ids.discard(test_id)
        selected = self.selected_definitions()
        self.selected_label.setText(
            f"Selected {len(selected)}  •  Visible {len(self.visible_definitions)}  •  Total {len(self.catalog.definitions)}"
        )
        self.selected_changed.emit(selected)

    def _definition_for_row(self, row: int):
        if row < 0:
            return None
        item = self.table.item(row, 1)
        if item is None:
            return None
        try:
            return self.catalog.get(item.data(Qt.ItemDataRole.UserRole) or item.text())
        except KeyError:
            return None

    def render_details(self) -> None:
        row = self.table.currentRow()
        definition = self._definition_for_row(row)
        if definition is None:
            self.clear_details()
            return
        fields = definition.source_fields
        self.overview_view.setHtml(
            f"<h3>{html.escape(definition.test_id)} — {html.escape(definition.test_name)}</h3>"
            f"<p><b>Group:</b> {html.escape(definition.group)}<br>"
            f"<b>Duration:</b> {html.escape(definition.duration_text or '—')}<br>"
            f"<b>Severity:</b> {html.escape(definition.severity or '—')}<br>"
            f"<b>Execution Type:</b> {html.escape(definition.execution_type.value)}</p>"
            f"<h4>Purpose</h4>{_safe_paragraphs(fields.get('Purpose', ''))}"
            f"<h4>Pre-condition</h4>{_safe_paragraphs(fields.get('Pre-condition', ''))}"
            f"<h4>Equipment</h4>{_safe_paragraphs(fields.get('Test Equipment', ''))}"
        )
        self.procedure_view.setHtml(
            f"<h3>Test Procedure</h3>{_safe_procedure(fields.get('Test Procedure', ''))}"
        )
        self.criteria_view.setHtml(
            f"<h3>Acceptance Criteria</h3>{_safe_paragraphs(fields.get('Acceptance Criteria', ''))}"
            f"<h3>Expected Result</h3>{_safe_paragraphs(fields.get('Expected result', ''))}"
        )
        self.evidence_plan_list.clear()
        for evidence in definition.evidence_plan:
            suffix = " — MANUAL REQUIRED" if evidence.manual_required else ""
            self.evidence_plan_list.addItem(f"{evidence.name}{suffix}\n{evidence.command_description}")
        self.details_stack.setCurrentIndex(1)

    def clear_details(self) -> None:
        self.overview_view.clear()
        self.procedure_view.clear()
        self.criteria_view.clear()
        self.evidence_plan_list.clear()
        self.details_stack.setCurrentIndex(0)

    def show_confirmation(self, definition, paths, decision=None) -> None:
        text = (
            f"Test ID: {definition.test_id}\nTest Name: {definition.test_name}\nDuration: {definition.duration_text or 'Unparsed'}\n"
            f"Execution Type: {definition.execution_type.value}\nEvidence Root: {paths.session_dir.parent}\n"
            f"Session: {paths.session_dir.name}\nAttempt: {paths.attempt:03d}\n\nNo workload starts until START TEST is pressed."
        )
        if decision is not None:
            baseline = "—" if decision.baseline_percent is None else f"{decision.baseline_percent:.1f} %"
            target = "—" if decision.target_percent is None else f"{decision.target_percent:g} %"
            added = "0 %" if decision.strategy == LoadStrategy.OBSERVE_ONLY else "Adaptive" if decision.strategy == LoadStrategy.LOAD_ASSIST else "NONE"
            text += (
                f"\n\nImmediate Baseline CPU: {baseline}\nTarget CPU: {target}\n"
                f"Strategy: {decision.strategy.value.replace('_', ' ')}\nAdditional Load: {added}\nReason: {decision.reason}"
            )
            if decision.strategy == LoadStrategy.LOAD_ASSIST and decision.stress_ng_available is not True:
                text += "\n\nBLOCKED: stress-ng is required for LOAD_ASSIST but is unavailable."
            self.confirmation_start_button.setEnabled(decision.can_start)
            self.confirmation_start_button.setText("START MONITORING" if decision.strategy == LoadStrategy.OBSERVE_ONLY else "START TEST")
        else:
            self.confirmation_start_button.setEnabled(True)
            self.confirmation_start_button.setText("START TEST")
        self.confirmation_text.setPlainText(text)
        self.confirmation_plan.clear()
        self.confirmation_plan.addItems([item.name for item in definition.evidence_plan])
        self.view_stack.setCurrentIndex(1)

    def show_immediate_baseline_pending(self, definition, paths) -> None:
        self.run_button.setEnabled(False)
        self.run_button.setToolTip("A stress test is already running.")
        self.confirmation_start_button.setEnabled(False)
        self.confirmation_start_button.setText("MEASURING BASELINE…")
        self.confirmation_text.setPlainText(
            f"{definition.test_id} — {definition.test_name}\n\n"
            "Collecting a bounded immediate DUT baseline before choosing an execution strategy.\n"
            "No artificial workload has started."
        )
        self.confirmation_plan.clear()
        self.view_stack.setCurrentIndex(1)

    def show_execution(self, definition, manager, paths, queue, decision=None) -> None:
        self._active_definition = definition
        self._active_manager = manager
        self._active_paths = paths
        self._active_strategy = decision
        self.execution_title.setText(f"{definition.test_id} — {definition.test_name}")
        self.evidence_viewer.set_evidence(manager.records, paths.attempt_dir, definition)
        manager.evidence_changed.connect(self.evidence_viewer.update_evidence)
        self.set_queue(queue)
        self._execution_metrics = SystemMetricsSummary(
            target_cpu_percent=decision.target_percent if decision else None
        )
        self._execution_metrics_tail = IncrementalLogTail(initial_lines=100)
        self._workload_tail = IncrementalLogTail(initial_lines=100)
        self._injected_load = None
        self._load_state = "MONITORING" if decision and decision.strategy == LoadStrategy.OBSERVE_ONLY else "RAMPING"
        metrics_record = next((record for record in manager.records if record.collector == "system_metrics"), None)
        if metrics_record:
            content = self._execution_metrics_tail.select(paths.attempt_dir / metrics_record.file)
            self._execution_metrics.feed(content)
        workload_record = next((record for record in manager.records if record.collector == "workload"), None)
        if workload_record:
            self._workload_tail.select(paths.attempt_dir / workload_record.file)
        self._render_strategy_summary()
        self.update_active_execution(definition, 0, manager)
        self.view_stack.setCurrentIndex(2)

    def _back_to_test_list(self) -> None:
        self.view_stack.setCurrentIndex(0)

    def _view_running_test(self) -> None:
        if getattr(self, "_active_definition", None) is not None and getattr(self, "_active_manager", None) is not None:
            self.view_stack.setCurrentIndex(2)

    def update_active_execution(self, definition, elapsed: int, manager) -> None:
        active = definition.runtime_status in {
            RuntimeStatus.STARTING,
            RuntimeStatus.RUNNING,
            RuntimeStatus.STOPPING,
        }
        self.active_banner.setVisible(active)
        self.run_button.setEnabled(not active)
        self.run_button.setToolTip("A stress test is already running." if active else "")
        if not active:
            return
        target = definition.duration_seconds
        target_text = _format_seconds(target) if target is not None else "—"
        self.active_test_label.setText(f"● {definition.test_id} — {definition.test_name}")
        self.active_status.set_status(definition.runtime_status)
        self.active_metrics_label.setText(
            f"Elapsed: {_format_seconds(elapsed)} / {target_text}    "
            f"Evidence: {manager.active_count()} Active    "
            f"Warnings: {manager.warning_count()}    Errors: {manager.error_count()}"
        )

    def clear_active_execution(self) -> None:
        self.active_banner.hide()
        self.run_button.setEnabled(True)
        self.run_button.setToolTip("")
        self._active_definition = None
        self._active_manager = None

    def _open_active_folder(self) -> None:
        paths = getattr(self, "_active_paths", None)
        if paths is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(paths.attempt_dir)))

    def update_execution(self, definition, elapsed: int, manager) -> None:
        self.execution_status.set_status(definition.runtime_status)
        target = definition.duration_seconds
        remaining = max(0, target - elapsed) if target is not None else None
        text = f"Elapsed: {_format_seconds(elapsed)}"
        if target is not None:
            text += f"    Target: {_format_seconds(target)}    Remaining: {_format_seconds(remaining)}"
        self.execution_times.setText(text)
        self.execution_counts.setText(f"Evidence: {manager.active_count()} Active    Warnings: {manager.warning_count()}    Errors: {manager.error_count()}")
        if hasattr(self, "_execution_metrics_tail"):
            content = self._execution_metrics_tail.read_new()
            if content:
                self._execution_metrics.feed(content)
        if hasattr(self, "_workload_tail"):
            workload = self._workload_tail.read_new()
            for injected in re.findall(r"injected=([\d.]+)%", workload):
                self._injected_load = float(injected)
            states = re.findall(r"state=([A-Z_]+)", workload)
            if states:
                self._load_state = states[-1].replace("_", " ")
        self._render_strategy_summary()
        self.update_active_execution(definition, elapsed, manager)

    def _render_strategy_summary(self) -> None:
        decision = getattr(self, "_active_strategy", None)
        if decision is None:
            self.strategy_panel.hide()
            return
        self.strategy_panel.show()
        metrics = getattr(self, "_execution_metrics", None)
        current = metrics.current_cpu if metrics else None
        injected = "NONE" if decision.strategy == LoadStrategy.OBSERVE_ONLY else "—" if self._injected_load is None else f"{self._injected_load:.1f} %"

        def value(number):
            return "—" if number is None else f"{number:.1f} %"

        self.strategy_summary_label.setText(
            f"Baseline CPU: {value(decision.baseline_percent)}    Target CPU: {value(decision.target_percent)}    "
            f"Strategy: {decision.strategy.value.replace('_', ' ')}    Injected Load: {injected}\n"
            f"Current CPU: {value(current)}    Average CPU: {value(metrics.average_cpu if metrics else None)}    "
            f"Min CPU: {value(metrics.minimum_cpu if metrics else None)}    Max CPU: {value(metrics.maximum_cpu if metrics else None)}    "
            f"State: {self._load_state}"
        )

    def set_queue(self, definitions) -> None:
        self.queue_list.clear()
        for definition in definitions:
            self.queue_list.addItem(f"{definition.test_id}    {definition.runtime_status.value}")

    def show_history(self) -> None:
        row = self.table.currentRow()
        if row < 0:
            self.message_label.setText("Select a test row to view history.")
            return
        definition = self._definition_for_row(row)
        if definition is None:
            self.message_label.setText("Select a test row to view history.")
            return
        root = self.window().system_stress_page.session_manager.evidence_root if hasattr(self.window(), "system_stress_page") else Path.home() / "Stress_Test_Logs"
        history = scan_history(root, definition.test_id)
        self.history_table.setRowCount(len(history))
        for row_index, item in enumerate(history):
            values = (item["session"], item["attempt"], item["start_time"], item["end_time"], item["status"], item["evidence_folder"])
            for column, value in enumerate(values):
                self.history_table.setItem(row_index, column, QTableWidgetItem(str(value or "—")))
        self.view_stack.setCurrentIndex(3)


class SystemStressPage(QWidget):
    TAB_NAMES = ("PRE-TEST ENVIRONMENT", "VD", "VR", "VMO")
    navigate_requested = Signal(int)

    def __init__(self, remote_service=None, parent=None, *, catalog_path=None, evidence_root=None, baseline_collector=None):
        super().__init__(parent)
        self.remote_service = remote_service
        self.session_manager = StressSessionManager(evidence_root)
        self.catalog_error = None
        try:
            self.catalog = StressCatalog.load_vd(catalog_path) if catalog_path else StressCatalog.load_vd()
        except StressCatalogError as exc:
            self.catalog = StressCatalog("VD", [])
            self.catalog_error = str(exc)
        self.runner = StressTestRunner(
            self.session_manager,
            platform=self.catalog.platform,
            remote_service=remote_service,
            baseline_collector=baseline_collector,
            parent=self,
        )
        self._build_ui()
        self._connect_runner()
        self.refresh_header()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 18, 24, 18)
        heading = QLabel("System Stress Test")
        heading.setObjectName("PageTitle")
        subtitle = QLabel("Stress / System")
        subtitle.setObjectName("Muted")
        root.addWidget(heading)
        root.addWidget(subtitle)
        header = QFrame()
        header.setObjectName("StressPanel")
        header.setMaximumHeight(108)
        summary = QGridLayout(header)
        summary.setContentsMargins(12, 8, 12, 8)
        summary.setHorizontalSpacing(12)
        summary.setVerticalSpacing(5)
        session_title = QLabel("SESSION")
        session_title.setObjectName("CardTitle")
        summary.addWidget(session_title, 0, 0, 2, 1)
        self.session_label = QLabel("NOT CREATED")
        self.environment_label = StatusBadge("NOT CHECKED")
        self.dut_status_label = StatusBadge("DISCONNECTED")
        self.root_label = QLabel()
        self.root_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.disk_label = QLabel("—")

        def add_summary_field(caption: str, value: QLabel, column: int, stretch: int = 0) -> None:
            field = QWidget()
            field_layout = QVBoxLayout(field)
            field_layout.setContentsMargins(0, 0, 0, 0)
            field_layout.setSpacing(1)
            caption_label = QLabel(caption)
            caption_label.setObjectName("Muted")
            field_layout.addWidget(caption_label)
            field_layout.addWidget(value)
            summary.addWidget(field, 0, column)
            if stretch:
                summary.setColumnStretch(column, stretch)

        add_summary_field("Session", self.session_label, 1)
        add_summary_field("Environment", self.environment_label, 2)
        add_summary_field("DUT", self.dut_status_label, 3)
        add_summary_field("Evidence Root", self.root_label, 4, 1)
        add_summary_field("Disk Free", self.disk_label, 5)
        self.select_folder_button = _button("SELECT FOLDER")
        self.default_folder_button = _button("USE DEFAULT")
        self.open_folder_button = _button("OPEN FOLDER")
        self.select_folder_button.setToolTip("Select Evidence Root folder")
        self.default_folder_button.setToolTip("Use default Evidence Root")
        self.open_folder_button.setToolTip("Open Evidence Root folder")
        button_row = QHBoxLayout()
        button_row.setSpacing(8)
        button_row.addStretch()
        button_row.addWidget(self.select_folder_button)
        button_row.addWidget(self.default_folder_button)
        button_row.addWidget(self.open_folder_button)
        summary.addLayout(button_row, 1, 1, 1, 5)
        self.select_folder_button.clicked.connect(self.select_folder)
        self.default_folder_button.clicked.connect(self.use_default)
        self.open_folder_button.clicked.connect(self.open_folder)
        root.addWidget(header)
        self.tabs = QTabWidget()
        self.tabs.setObjectName("StressTabs")
        self.pretest_page = PreTestEnvironmentPage(self.session_manager, self.remote_service)
        self.vd_page = VDCatalogPage(self.catalog)
        if self.catalog_error:
            self.vd_page.message_label.setText(f"SOURCE ERROR: {self.catalog_error}")
            self.vd_page.run_button.setEnabled(False)
        self.vr_page = PlaceholderStressPage("VR")
        self.vmo_page = PlaceholderStressPage("VMO")
        for name, page in zip(self.TAB_NAMES, (self.pretest_page, self.vd_page, self.vr_page, self.vmo_page)):
            self.tabs.addTab(page, name)
        root.addWidget(self.tabs, 1)
        self.vd_page.selected_changed.connect(self.pretest_page.set_required_tests)
        self.vd_page.run_requested.connect(self.run_selected)
        self.pretest_page.result_changed.connect(self._environment_changed)
        self.pretest_page.override_changed.connect(lambda _value: self.refresh_header())
        self.pretest_page.dashboard_requested.connect(lambda: self.navigate_requested.emit(0))
        if self.remote_service is not None:
            for signal_name in ("connected", "disconnected", "connection_failed", "connecting"):
                signal = getattr(self.remote_service, signal_name, None)
                if signal is not None:
                    signal.connect(self._update_dut_status)

    def _connect_runner(self) -> None:
        self.runner.confirmation_required.connect(self._confirmation)
        self.runner.test_started.connect(self._test_started)
        self.runner.test_updated.connect(self._test_updated)
        self.runner.test_finished.connect(self._test_finished)
        self.runner.immediate_baseline_started.connect(self.vd_page.show_immediate_baseline_pending)
        self.runner.queue_changed.connect(self.vd_page.set_queue)
        self.runner.error.connect(self._show_error)
        self.vd_page.start_requested.connect(self.runner.start_current)
        self.vd_page.stop_requested.connect(self.runner.stop)
        self.vd_page.finish_requested.connect(self.runner.finish_for_review)

    def refresh_header(self) -> None:
        self.session_label.setText(self.session_manager.session_id or "NOT CREATED")
        status = self.pretest_page.environment_status.value.replace("_", " ")
        if self.pretest_page.override:
            status += " (OVERRIDE)"
        self.environment_label.set_status(status)
        self.vd_page.environment_label.set_status(status)
        self._update_dut_status()
        self.root_label.setText(str(self.session_manager.evidence_root))
        free = self.session_manager.disk_free()
        self.disk_label.setText(f"{free / (1024 ** 3):.1f} GiB" if free is not None else "Unavailable")
        root_locked = self.session_manager.session_dir is not None
        self.select_folder_button.setEnabled(not root_locked)
        self.default_folder_button.setEnabled(not root_locked)

    def _update_dut_status(self, *_args) -> None:
        if self.remote_service is None:
            status = "NOT CONFIGURED"
        elif getattr(self.remote_service, "is_connected", False):
            status = "CONNECTED"
        else:
            status = "DISCONNECTED"
        self.dut_status_label.set_status(status)

    def select_folder(self) -> None:
        selected = QFileDialog.getExistingDirectory(self, "Select Stress Evidence Root", str(self.session_manager.evidence_root))
        if not selected:
            return
        try:
            self.session_manager.set_evidence_root(selected)
        except (ValueError, RuntimeError) as exc:
            self._show_error(str(exc))
        self.refresh_header()

    def use_default(self) -> None:
        try:
            self.session_manager.set_evidence_root(Path.home() / "Stress_Test_Logs")
        except (ValueError, RuntimeError) as exc:
            self._show_error(str(exc))
        self.refresh_header()

    def open_folder(self) -> None:
        valid, detail = self.session_manager.validate_evidence_root()
        if not valid:
            self._show_error(detail)
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.session_manager.evidence_root)))

    def run_selected(self, definitions) -> None:
        if self.runner.running:
            self.vd_page.message_label.setText("A stress test is already running.")
            self.vd_page.view_stack.setCurrentIndex(0)
            return
        self.runner.enqueue(
            definitions,
            environment_status=self.pretest_page.environment_status,
            override=self.pretest_page.override,
            blocking_reasons=self.pretest_page.blocking_reasons,
        )
        self.refresh_header()

    def _environment_changed(self, result) -> None:
        self.refresh_header()

    def _confirmation(self, definition, paths) -> None:
        self.vd_page.show_confirmation(definition, paths, self.runner.current_strategy)
        self.refresh_header()

    def _test_started(self, definition, manager) -> None:
        self.vd_page.show_execution(definition, manager, self.runner.current_paths, [definition, *self.runner.queue], self.runner.current_strategy)
        self._test_updated(definition)

    def _test_updated(self, definition) -> None:
        if definition and self.runner.evidence_manager:
            self.vd_page.update_execution(definition, self.runner.elapsed_seconds(), self.runner.evidence_manager)
        self.vd_page.refresh_table()

    def _test_finished(self, definition, _paths) -> None:
        self.vd_page.message_label.setText(f"{definition.test_id} finished: {definition.runtime_status.value}")
        self.vd_page.execution_status.set_status(definition.runtime_status)
        self.vd_page.clear_active_execution()
        self.vd_page.refresh_table()

    def _show_error(self, message: str) -> None:
        self.vd_page.message_label.setText(message)
        self.vd_page.view_stack.setCurrentIndex(0)

    def shutdown(self) -> None:
        if self.runner.running:
            self.runner.stop()
        if self.pretest_page._thread and self.pretest_page._thread.isRunning():
            self.pretest_page._thread.wait(3000)
        self.runner.shutdown()
