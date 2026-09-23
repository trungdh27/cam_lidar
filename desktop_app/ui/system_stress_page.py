from __future__ import annotations

from collections import deque
from copy import copy
from dataclasses import dataclass
from datetime import datetime
import html
import re
from pathlib import Path
from types import SimpleNamespace

from PySide6.QtCore import QThread, QTimer, QUrl, Qt, Signal
from PySide6.QtGui import QAction, QDesktopServices, QTextCursor
import pyqtgraph as pg

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
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QStyle,
    QStyleOptionButton,
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
    format_bytes,
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
from desktop_app.stress.results import (
    latest_result_attempt,
    load_result_snapshot,
    persist_final_metrics,
    review_attempt,
)
from desktop_app.stress.session import StressSessionManager, utc_now
from desktop_app.ui.theme import STRESS_CHART_COLORS


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
    if value == "PREPARED":
        return "prepared"
    if value in {"RUNNING", "STARTING", "STOPPING", "CONNECTING"}:
        return "running"
    if value == "STOPPED":
        return "stopped"
    return "neutral"


@dataclass(frozen=True)
class CasePresentationContext:
    """Presentation-only classification derived from existing test metadata."""

    primary_domain: str
    preferred_trend: str
    relevant_metric_cards: tuple[str, ...]
    purpose: str
    has_cpu_target: bool
    has_manual_evidence: bool


def case_presentation_context(definition) -> CasePresentationContext:
    """Return a safe dashboard presentation for any catalog definition.

    This helper deliberately does not infer targets, strategies, or verdicts.
    Those remain owned by the runner and source catalog.
    """

    group = str(getattr(definition, "group", "") or "")
    name = str(getattr(definition, "test_name", "") or "")
    fields = dict(getattr(definition, "source_fields", {}) or {})
    evidence = tuple(getattr(definition, "evidence_plan", ()) or ())
    collectors = {str(getattr(item, "collector", "") or "").casefold() for item in evidence}
    text = f"{group} {name}".casefold()

    if "memory" in text or "ram" in text or "vmstat" in collectors:
        domain = "MEMORY"
    elif any(word in text for word in ("storage", "disk", "i/o")) or "iostat" in collectors:
        domain = "STORAGE"
    elif "thermal" in text:
        domain = "THERMAL"
    elif "power" in text or "battery" in text:
        domain = "POWER"
    elif "motor" in text or "actuator" in text:
        domain = "MOTOR"
    elif "motion" in text or "walking" in text:
        domain = "MOTION"
    elif any(word in text for word in ("sensor", "camera", "lidar", "imu")):
        domain = "SENSOR"
    elif any(word in text for word in ("network", "communication", "ethernet", "wi-fi", "wifi", "can")):
        domain = "COMMUNICATION"
    elif any(word in text for word in ("endurance", "full system", "integration", "long run", "long-run")):
        domain = "ENDURANCE"
    elif (
        getattr(definition, "target_cpu_percent", None) is not None
        or "performance_auto" in collectors
        or any(word in text for word in ("cpu", "gpu", " ai "))
    ):
        domain = "CPU"
    else:
        domain = "GENERAL"

    preferred_trend = "temperature" if domain == "THERMAL" else "power" if domain == "POWER" else "performance"
    relevant = {
        "CPU": ("performance",),
        "MEMORY": ("performance",),
        "STORAGE": ("performance",),
        "THERMAL": ("thermal", "thermal_details"),
        "POWER": ("battery", "thermal_details"),
        "MOTOR": ("motor", "thermal_details"),
        "MOTION": ("motor", "sensor", "battery"),
        "SENSOR": ("sensor",),
        "COMMUNICATION": ("sensor",),
        "ENDURANCE": ("performance", "thermal", "battery", "motor", "sensor"),
        "GENERAL": (),
    }[domain]
    purpose = str(fields.get("Purpose") or fields.get("Expected result") or group or "—").strip()
    manual = any(bool(getattr(item, "manual_required", False)) for item in evidence)
    return CasePresentationContext(
        primary_domain=domain,
        preferred_trend=preferred_trend,
        relevant_metric_cards=relevant,
        purpose=purpose or "—",
        has_cpu_target=getattr(definition, "target_cpu_percent", None) is not None,
        has_manual_evidence=manual,
    )


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


class CheckBoxHeader(QHeaderView):
    """Compact select-all checkbox for the first table column."""

    toggled = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(Qt.Orientation.Horizontal, parent)
        self._check_state = Qt.CheckState.Unchecked

    def setCheckState(self, state: Qt.CheckState) -> None:
        if self._check_state == state:
            return
        self._check_state = state
        self.updateSection(0)

    def checkState(self) -> Qt.CheckState:
        return self._check_state

    def paintSection(self, painter, rect, logical_index: int) -> None:
        super().paintSection(painter, rect, logical_index)
        if logical_index != 0:
            return
        option = QStyleOptionButton()
        indicator = self.style().subElementRect(
            QStyle.SubElement.SE_CheckBoxIndicator, option, self
        )
        option.rect = indicator.translated(
            rect.center().x() - indicator.center().x(),
            rect.center().y() - indicator.center().y(),
        )
        option.state = QStyle.StateFlag.State_Enabled
        if self._check_state == Qt.CheckState.Checked:
            option.state |= QStyle.StateFlag.State_On
        elif self._check_state == Qt.CheckState.PartiallyChecked:
            option.state |= QStyle.StateFlag.State_NoChange
        else:
            option.state |= QStyle.StateFlag.State_Off
        self.style().drawControl(QStyle.ControlElement.CE_CheckBox, option, painter, self)

    def mousePressEvent(self, event) -> None:
        if self.logicalIndexAt(event.position().toPoint()) == 0:
            checked = self._check_state != Qt.CheckState.Checked
            self.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
            self.toggled.emit(checked)
            event.accept()
            return
        super().mousePressEvent(event)


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
        root.setSpacing(6)
        summary = QFrame()
        summary.setObjectName("StressPanel")
        summary.setMaximumHeight(46)
        summary_layout = QHBoxLayout(summary)
        summary_layout.setContentsMargins(10, 5, 10, 5)
        summary_layout.setSpacing(9)
        summary_layout.addWidget(QLabel("READINESS"))
        self.readiness_label = StatusBadge("NOT CHECKED")
        summary_layout.addWidget(self.readiness_label)
        self.count_label = QLabel("Passed 0    Warning 0    Failed 0    Missing 0")
        summary_layout.addWidget(self.count_label)
        summary_layout.addStretch()
        self.reason_label = QLabel("Run environment checks before executing stress tests.")
        self.reason_label.setObjectName("Muted")
        self.reason_label.setMaximumWidth(430)
        self.reason_label.setToolTip(self.reason_label.text())
        summary_layout.addWidget(self.reason_label)
        root.addWidget(summary)

        baseline_panel = QFrame()
        baseline_panel.setObjectName("StressPanel")
        baseline_panel.setMaximumHeight(82)
        baseline_layout = QVBoxLayout(baseline_panel)
        baseline_layout.setContentsMargins(10, 5, 10, 6)
        baseline_layout.setSpacing(4)
        baseline_header = QHBoxLayout()
        baseline_title = QLabel("SYSTEM BASELINE")
        baseline_title.setObjectName("CardTitle")
        self.baseline_status = StatusBadge("NOT CHECKED")
        baseline_header.addWidget(baseline_title)
        baseline_header.addWidget(self.baseline_status)
        baseline_header.addStretch()
        baseline_layout.addLayout(baseline_header)
        baseline_row = QHBoxLayout()
        baseline_row.setSpacing(10)
        self.baseline_summary = QLabel()
        self.baseline_summary.setObjectName("Muted")
        self.baseline_summary.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self._baseline_summary_text = "Run CHECKS ALL to collect the current DUT baseline."
        self.baseline_summary.setToolTip(self._baseline_summary_text)
        self.baseline_summary.setText(self._baseline_summary_text)
        self.run_baseline_button = _button("RUN BASELINE", "PrimaryButton")
        self.run_baseline_button.clicked.connect(self.run_checks)
        self.run_baseline_button.hide()
        baseline_row.addWidget(self.baseline_summary, 1)
        baseline_layout.addLayout(baseline_row)
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
        checks_layout.addWidget(self.check_table, 1)

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
        self.dut_info_view.setPlainText("Run CHECKS ALL to view DUT information.")
        self.pretest_detail_tabs.addTab(check_detail_page, "SELECTED CHECK")
        self.pretest_detail_tabs.addTab(self.dut_info_view, "DUT INFORMATION")
        detail_layout.addWidget(self.pretest_detail_tabs, 1)

        content_splitter.addWidget(checks_panel)
        content_splitter.addWidget(detail_panel)
        content_splitter.setChildrenCollapsible(False)
        content_splitter.setStretchFactor(0, 63)
        content_splitter.setStretchFactor(1, 37)
        content_splitter.setSizes([760, 440])
        root.addWidget(content_splitter, 1)

        actions = QHBoxLayout()
        self.run_all_button = _button("CHECKS ALL", "PrimaryButton")
        self.run_selected_button = _button("SELECT")
        self.run_selected_button.setEnabled(False)
        self.export_button = _button("EXPORT BASELINE")
        self.export_button.hide()
        self.override_button = _button("OVERRIDE AND CONTINUE", "WarningButton")
        self.override_button.setVisible(False)
        self.run_all_button.clicked.connect(self.run_checks)
        self.run_selected_button.clicked.connect(self.run_checks)
        self.export_button.clicked.connect(self.open_baseline)
        self.override_button.clicked.connect(self.apply_override)
        for button in (self.run_all_button, self.run_selected_button, self.override_button):
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
        self.run_baseline_button.setEnabled(False)
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
        self.run_baseline_button.setEnabled(True)
        self.session_manager.update_environment(result.status, result.blocking_reasons)
        self._render(result)
        self.result_changed.emit(result)

    def _failed(self, error: str) -> None:
        self.readiness_label.set_status("NOT READY")
        self.reason_label.setText("Environment check failed. Open details for the full error.")
        self.reason_label.setToolTip(error)
        self.detail_area.setPlainText(error)
        self.run_all_button.setEnabled(True)
        self.run_baseline_button.setEnabled(True)

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
            summary = f"{len(result.blocking_reasons)} blocking issue(s). Select a row for details."
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
            self._set_baseline_summary(baseline_summary_text(result.baseline))
        else:
            self.baseline_status.set_status("NOT MEASURED")
            self._set_baseline_summary("No valid system baseline is available.")
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
            status_item = SortableTableItem("", _display_status(check.status))
            status_item.setToolTip(_display_status(check.status))
            self.check_table.setItem(row, 2, status_item)
            self.check_table.setCellWidget(row, 2, _badge_cell(check.status))
        self.override_button.setVisible(result.status == EnvironmentStatus.NOT_READY)
        self.go_dashboard_button.hide()
        self.detail_area.setPlainText("Select a check to view details and remediation.")
        self._update_action_hierarchy()

    def _set_baseline_summary(self, text: str) -> None:
        self._baseline_summary_text = "  •  ".join(
            line.strip() for line in text.splitlines() if line.strip()
        ) or "—"
        self.baseline_summary.setToolTip(text)
        self._elide_baseline_summary()

    def _elide_baseline_summary(self) -> None:
        width = max(80, self.baseline_summary.width() - 4)
        self.baseline_summary.setText(
            self.baseline_summary.fontMetrics().elidedText(
                self._baseline_summary_text, Qt.TextElideMode.ElideRight, width
            )
        )

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._elide_baseline_summary()

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
            self.dut_info_view.setPlainText("Run CHECKS ALL to view environment details.")
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
    review_requested = Signal(object, str)
    view_result_requested = Signal(object)
    result_folder_requested = Signal(str)

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
        root.setSpacing(4)
        title_row = QHBoxLayout()
        title_row.setSpacing(8)
        title = QLabel("VD SYSTEM STRESS TEST")
        title.setObjectName("CardTitle")
        title_row.addWidget(title)
        self.message_label = QLabel("")
        self.message_label.setObjectName("Muted")
        title_row.addWidget(self.message_label)
        title_row.addStretch()
        root.addLayout(title_row)
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
        filter_bar.setMaximumHeight(44)
        filters = QHBoxLayout(filter_bar)
        filters.setContentsMargins(8, 4, 8, 4)
        filters.setSpacing(6)
        self.group_combo = QComboBox()
        self.group_combo.setMinimumWidth(130)
        self.group_combo.setMaximumWidth(220)
        self.group_combo.addItem("All Groups")
        self.group_combo.addItems(sorted({item.group for item in self.catalog.definitions}))
        self.status_combo = QComboBox()
        self.status_combo.setMinimumWidth(92)
        self.status_combo.setMaximumWidth(140)
        self.status_combo.addItem("All")
        self.status_combo.addItems([status.value for status in RuntimeStatus])
        self.severity_combo = QComboBox()
        self.severity_combo.setMinimumWidth(88)
        self.severity_combo.setMaximumWidth(140)
        self.severity_combo.addItem("All")
        self.severity_combo.addItems(sorted({item.severity for item in self.catalog.definitions if item.severity}))

        filters.addWidget(QLabel("Group"))
        filters.addWidget(self.group_combo)
        filters.addWidget(QLabel("Status"))
        filters.addWidget(self.status_combo)
        filters.addWidget(QLabel("Severity"))
        filters.addWidget(self.severity_combo)
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

        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.catalog_splitter = splitter
        self.table = QTableWidget(0, 7)
        self.table.setObjectName("StressTable")
        self.select_all_header = CheckBoxHeader(self.table)
        self.table.setHorizontalHeader(self.select_all_header)
        self.table.setHorizontalHeaderLabels(["", "Test ID", "Test Name", "Group", "Duration", "Severity", "Status"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setWordWrap(False)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(32)
        header = self.table.horizontalHeader()
        header.setSectionsClickable(True)
        header.setSortIndicatorShown(True)
        header.setFixedHeight(34)
        header.setMinimumSectionSize(30)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        for column in range(1, self.table.columnCount()):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.setColumnWidth(0, 32)
        self.table.setColumnWidth(1, 110)
        self.table.setColumnWidth(3, 155)
        self.table.setColumnWidth(4, 78)
        self.table.setColumnWidth(5, 82)
        self.table.setColumnWidth(6, 110)
        self.table.setSortingEnabled(True)
        self.table.itemSelectionChanged.connect(self.render_details)
        self.table.itemChanged.connect(self._selection_count_changed)
        self.select_all_header.toggled.connect(self._select_all_visible)
        splitter.addWidget(self.table)

        self.details_container = QWidget()
        details_container_layout = QVBoxLayout(self.details_container)
        details_container_layout.setContentsMargins(0, 0, 0, 0)
        details_container_layout.setSpacing(0)
        self.details_panel = QFrame()
        self.details_panel.setObjectName("StressPanel")
        details_layout = QVBoxLayout(self.details_panel)
        details_layout.setContentsMargins(10, 8, 10, 8)
        details_layout.setSpacing(4)
        details_header = QHBoxLayout()
        details_title = QLabel("TEST CASE DETAILS")
        details_title.setObjectName("CardTitle")
        self.collapse_details_button = QToolButton()
        self.collapse_details_button.setObjectName("DetailsToggle")
        self.collapse_details_button.setText("<")
        self.collapse_details_button.setToolTip("Collapse test case details")
        self.collapse_details_button.setFixedSize(28, 28)
        self.collapse_details_button.clicked.connect(self.toggle_details)
        details_header.addWidget(details_title)
        details_header.addStretch()
        details_header.addWidget(self.collapse_details_button)
        details_layout.addLayout(details_header)

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
        self.expand_details_button = QToolButton()
        self.expand_details_button.setObjectName("DetailsToggle")
        self.expand_details_button.setText(">")
        self.expand_details_button.setToolTip("Expand test case details")
        self.expand_details_button.setFixedSize(28, 28)
        self.expand_details_button.clicked.connect(self.toggle_details)
        self.expand_details_button.hide()
        details_container_layout.addWidget(self.details_panel, 1)
        details_container_layout.addWidget(
            self.expand_details_button, 0, Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignRight
        )
        splitter.addWidget(self.details_container)
        splitter.setChildrenCollapsible(False)
        splitter.setStretchFactor(0, 64)
        splitter.setStretchFactor(1, 36)
        splitter.setSizes([780, 440])
        self._details_collapsed = False
        self._expanded_splitter_sizes = [780, 440]
        catalog_layout.addWidget(splitter, 1)

        footer = QHBoxLayout()
        self.selected_label = QLabel("Selected 0  •  Visible 0  •  Total 0")
        self.environment_caption = QLabel("Environment")
        self.environment_label = StatusBadge("NOT CHECKED")
        self.history_button = _button("HISTORY")
        self.view_result_button = _button("VIEW RESULT")
        self.view_result_button.setEnabled(False)
        self.run_button = _button("RUN SELECTED", "PrimaryButton")
        self.history_button.clicked.connect(self.show_history)
        self.view_result_button.clicked.connect(self._request_selected_result)
        self.run_button.clicked.connect(lambda: self.run_requested.emit(self.selected_definitions()))
        footer.addWidget(self.selected_label)
        footer.addSpacing(8)
        footer.addWidget(self.environment_caption)
        footer.addWidget(self.environment_label)
        footer.addStretch()
        footer.addWidget(self.history_button)
        footer.addWidget(self.view_result_button)
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
        self.table.itemDoubleClicked.connect(lambda _item: self._request_selected_result())

    @staticmethod
    def _rich_text_view() -> QTextBrowser:
        view = QTextBrowser()
        view.setObjectName("StressRichText")
        view.setOpenExternalLinks(False)
        view.setPlaceholderText("Select a test case to view details.")
        return view

    def _build_column_menu(self) -> None:
        labels = ("", "Test ID", "Test Name", "Group", "Duration", "Severity", "Status")
        essential = {1, 2, 6}
        self.column_actions = {}
        for column, label in enumerate(labels[1:], start=1):
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
        for column in (3, 4, 5):
            action = self.column_actions.get(column)
            if action is not None:
                action.blockSignals(True)
                action.setChecked(show_optional)
                action.blockSignals(False)
            self.table.setColumnHidden(column, not show_optional)

    def toggle_details(self) -> None:
        self._set_details_collapsed(not self._details_collapsed)

    def _set_details_collapsed(self, collapsed: bool) -> None:
        if collapsed == self._details_collapsed:
            return
        if collapsed:
            sizes = self.catalog_splitter.sizes()
            if len(sizes) == 2 and sizes[1] > 40:
                self._expanded_splitter_sizes = sizes
            self.details_panel.hide()
            self.expand_details_button.show()
            self.catalog_splitter.setStretchFactor(0, 1)
            self.catalog_splitter.setStretchFactor(1, 0)
            total = max(sum(sizes), self.catalog_splitter.width())
            self.catalog_splitter.setSizes([max(0, total - 32), 32])
        else:
            self.expand_details_button.hide()
            self.details_panel.show()
            self.catalog_splitter.setStretchFactor(0, 64)
            self.catalog_splitter.setStretchFactor(1, 36)
            self.catalog_splitter.setSizes(self._expanded_splitter_sizes)
        self._details_collapsed = collapsed

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
        back.clicked.connect(self._back_from_confirmation)
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
        view.setObjectName("StressExecutionView")
        layout = QVBoxLayout(view)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        scroll = QScrollArea()
        scroll.setObjectName("StressExecutionScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        execution_content = QWidget()
        execution_content.setObjectName("StressExecutionContent")
        content = QVBoxLayout(execution_content)
        content.setContentsMargins(2, 2, 2, 2)
        content.setSpacing(7)

        # Active test header shared by every VD definition.
        active_card = QFrame()
        active_card.setObjectName("ActiveTestCard")
        active_card.setMinimumHeight(92)
        active_card.setMaximumHeight(98)
        active = QHBoxLayout(active_card)
        active.setContentsMargins(14, 10, 14, 10)
        active.setSpacing(10)

        active_icon = QLabel("▣")
        active_icon.setObjectName("ActiveTestIcon")
        active_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        active_icon.setFixedSize(46, 46)
        active.addWidget(active_icon)

        title_block = QVBoxLayout()
        title_block.setSpacing(3)
        title_row = QHBoxLayout()
        title_row.setSpacing(8)
        self.execution_title = QLabel("—")
        self.execution_title.setObjectName("ActiveTestTitle")
        self.execution_title.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.execution_status = StatusBadge("NOT RUNNING")
        title_row.addWidget(self.execution_title)
        title_row.addWidget(self.execution_status)
        title_row.addStretch()
        self.execution_subtitle = QLabel("—")
        self.execution_subtitle.setObjectName("Muted")
        self.execution_subtitle.setWordWrap(False)
        self.execution_subtitle.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        title_block.addLayout(title_row)
        title_block.addWidget(self.execution_subtitle)
        active.addLayout(title_block, 5)

        self.active_summary_values = {}

        def add_active_summary(icon: str, label: str, key: str) -> None:
            field = QWidget()
            field.setObjectName("ActiveSummaryField")
            field_layout = QVBoxLayout(field)
            field_layout.setContentsMargins(7, 0, 7, 0)
            field_layout.setSpacing(0)
            caption = QLabel(f"{icon}  {label}")
            caption.setObjectName("MetricCaption")
            value = QLabel("—")
            value.setObjectName("ActiveSummaryValue")
            value.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            field_layout.addWidget(caption)
            field_layout.addWidget(value)
            active.addWidget(field, 1)
            self.active_summary_values[key] = value

        for index, (icon, label, key) in enumerate(
            (
                ("◷", "Elapsed", "elapsed"),
                ("◴", "Remaining", "remaining"),
                ("◎", "Target Duration", "target"),
                ("◆", "Evidence", "evidence"),
                ("△", "Warnings", "warnings"),
                ("×", "Errors", "errors"),
            )
        ):
            if index:
                separator = QFrame()
                separator.setObjectName("DashboardSeparator")
                separator.setFrameShape(QFrame.Shape.VLine)
                active.addWidget(separator)
            add_active_summary(icon, label, key)
        content.addWidget(active_card)

        # Compatibility text remains available to callers/tests, but the visible
        # header uses individual aligned fields.
        self.execution_times = QLabel("Elapsed: 00:00:00")
        self.execution_counts = QLabel("Evidence: 0 Active    Warnings: 0    Errors: 0")
        self.execution_times.hide()
        self.execution_counts.hide()

        # Dynamic case monitor. Slots are reused across domains so a definition
        # with incomplete metadata always falls back safely.
        strategy_panel = QFrame()
        strategy_panel.setObjectName("CaseMonitorPanel")
        strategy_panel.setMinimumHeight(126)
        strategy_panel.setMaximumHeight(152)
        strategy_layout = QVBoxLayout(strategy_panel)
        strategy_layout.setContentsMargins(12, 8, 12, 8)
        strategy_layout.setSpacing(4)
        strategy_header = QHBoxLayout()
        strategy_title = QLabel("CASE MONITOR")
        strategy_title.setObjectName("SectionTitle")
        self.case_domain_badge = QLabel("GENERAL")
        self.case_domain_badge.setObjectName("DomainBadge")
        strategy_header.addWidget(strategy_title)
        strategy_header.addWidget(self.case_domain_badge)
        strategy_header.addStretch()
        strategy_layout.addLayout(strategy_header)
        self.strategy_summary_label = QLabel("Not applicable")
        self.strategy_summary_label.hide()
        self.case_monitor_grid = QGridLayout()
        self.case_monitor_grid.setContentsMargins(0, 0, 0, 0)
        self.case_monitor_grid.setHorizontalSpacing(0)
        self.case_monitor_grid.setVerticalSpacing(3)
        self.case_monitor_fields = []
        for index in range(12):
            field = QFrame()
            field.setObjectName("CaseMetricField")
            field_layout = QVBoxLayout(field)
            field_layout.setContentsMargins(9, 1, 9, 1)
            field_layout.setSpacing(0)
            label = QLabel("—")
            label.setObjectName("MetricCaption")
            value = QLabel("—")
            value.setObjectName("CaseMetricValue")
            value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            field_layout.addWidget(label)
            field_layout.addWidget(value)
            self.case_monitor_grid.addWidget(field, index // 4, index % 4)
            self.case_monitor_fields.append((field, label, value))
        for column in range(4):
            self.case_monitor_grid.setColumnStretch(column, 1)
        strategy_layout.addLayout(self.case_monitor_grid)
        self.strategy_panel = strategy_panel
        content.addWidget(strategy_panel)

        # Main 58/42 dashboard split.
        dashboard_row = QHBoxLayout()
        dashboard_row.setSpacing(8)
        self.live_metrics_panel = QFrame()
        self.live_metrics_panel.setObjectName("StressPanel")
        self.live_metrics_panel.setMinimumHeight(358)
        self.live_metrics_panel.setMaximumHeight(390)
        live_root = QVBoxLayout(self.live_metrics_panel)
        live_root.setContentsMargins(10, 8, 10, 8)
        live_root.setSpacing(6)

        self.live_metrics_title = QLabel("Live Metrics Dashboard")
        self.live_metrics_title.setObjectName("SectionTitle")
        live_root.addWidget(self.live_metrics_title)

        def metric_card(title: str, rows: tuple[tuple[str, str], ...], key: str):
            card = QFrame()
            card.setObjectName("MetricDashboardCard")
            card.setProperty("relevant", False)
            card.setProperty("state", "no_data")
            card.setMinimumHeight(151)
            card.setMaximumHeight(170)
            card_layout = QVBoxLayout(card)
            card_layout.setContentsMargins(9, 7, 9, 7)
            card_layout.setSpacing(2)
            heading = QLabel(title)
            heading.setObjectName("DashboardCardTitle")
            card_layout.addWidget(heading)
            values = {}
            containers = {}
            for label_text, row_key in rows:
                row_widget = QWidget()
                row_layout = QHBoxLayout(row_widget)
                row_layout.setContentsMargins(0, 0, 0, 0)
                row_layout.setSpacing(5)
                label = QLabel(label_text)
                label.setObjectName("DashboardMetricLabel")
                value = QLabel("—")
                value.setObjectName("DashboardMetricValue")
                value.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
                row_layout.addWidget(label, 1)
                row_layout.addWidget(value)
                card_layout.addWidget(row_widget)
                values[row_key] = value
                containers[row_key] = row_widget
            card_layout.addStretch()
            self.metric_cards[key] = card
            self.metric_values[key] = values
            self.metric_rows[key] = containers
            return card

        self.metric_cards = {}
        self.metric_values = {}
        self.metric_rows = {}
        cards = QGridLayout()
        cards.setContentsMargins(0, 0, 0, 0)
        cards.setHorizontalSpacing(6)
        cards.setVerticalSpacing(6)
        self.performance_card = metric_card(
            "▦  CPU PERFORMANCE",
            (("CPU Target", "target"), ("CPU Usage", "cpu"), ("CPU Avg / Min / Max", "cpu_range"),
             ("RAM Used", "ram"), ("Load 1m / 5m / 15m", "load"), ("GPU Usage", "gpu"),
             ("AI Runtime / Nodes", "ai")),
            "performance",
        )
        self.thermal_card = metric_card(
            "♨  THERMAL",
            (("CPU Temp", "cpu"), ("GPU Temp", "gpu"), ("SoC0 Temp", "soc0"),
             ("SoC1 Temp", "soc1"), ("SoC2 Temp", "soc2"), ("TJ Temp", "tj")),
            "thermal",
        )
        self.battery_card = metric_card(
            "ϟ  BATTERY / POWER",
            (("SoC / SOH", "soc_soh"), ("Pack Voltage", "voltage"), ("Current", "current"),
             ("Motor Bus Voltage", "bus_voltage"), ("Battery Temp Min / Avg / Max", "temp_range")),
            "battery",
        )
        self.motor_card = metric_card(
            "⚙  MOTOR",
            (("Status", "status"), ("Alive Motors", "alive"), ("Motor Errors", "errors"),
             ("Temperature Min / Avg / Max", "temp_range"), ("Thermal State", "thermal"),
             ("Error Code", "error_code")),
            "motor",
        )
        self.sensor_card = metric_card(
            "◉  SENSORS / ROS HEALTH",
            (("ROS Health", "health"), ("Camera FPS", "camera"), ("LiDAR Hz", "lidar"),
             ("IMU Hz", "imu"), ("ROS Nodes", "nodes")),
            "sensor",
        )
        self.thermal_details_card = metric_card(
            "≋  THERMAL DETAILS",
            (("Battery Temperatures", "battery"), ("Motor Temperatures", "motor")),
            "thermal_details",
        )
        for index, card in enumerate(
            (self.performance_card, self.thermal_card, self.battery_card,
             self.motor_card, self.sensor_card, self.thermal_details_card)
        ):
            cards.addWidget(card, index // 3, index % 3)
        for column in range(3):
            cards.setColumnStretch(column, 1)
        live_root.addLayout(cards, 1)
        dashboard_row.addWidget(self.live_metrics_panel, 58)

        self.trends_panel = QFrame()
        self.trends_panel.setObjectName("StressPanel")
        self.trends_panel.setMinimumHeight(358)
        self.trends_panel.setMaximumHeight(390)
        trends_layout = QVBoxLayout(self.trends_panel)
        trends_layout.setContentsMargins(10, 8, 10, 8)
        trends_layout.setSpacing(5)
        trends_header = QHBoxLayout()
        self.trends_title = QLabel("Live Trends — Last 120 Seconds")
        self.trends_title.setObjectName("SectionTitle")
        trends_header.addWidget(self.trends_title)
        trends_header.addStretch()
        self.trend_buttons = {}
        for mode, text in (("performance", "CPU / GPU"), ("temperature", "Temperature"), ("power", "Power")):
            button = _button(text, "SegmentButton")
            button.setCheckable(True)
            button.clicked.connect(lambda _checked=False, selected=mode: self._set_trend_mode(selected))
            trends_header.addWidget(button)
            self.trend_buttons[mode] = button
        trends_layout.addLayout(trends_header)
        self.trend_chart_title = QLabel("CPU / GPU UTILIZATION")
        self.trend_chart_title.setObjectName("DashboardCardTitle")
        trends_layout.addWidget(self.trend_chart_title)

        self.performance_plot = pg.PlotWidget()
        self.performance_plot.setObjectName("StressTrendPlot")
        self.performance_plot.setMinimumHeight(300)
        self.performance_plot.setBackground("w")
        self.performance_plot.showGrid(x=True, y=True, alpha=0.25)
        self.performance_plot.setLabel("left", "Utilization", units="%")
        self.performance_plot.setLabel("bottom", "History")
        self.performance_plot.setYRange(0, 100)
        self.performance_plot.setXRange(-120, 0)
        self.performance_plot.getAxis("bottom").setTicks(
            [[(-120, "-120s"), (-90, "-90s"), (-60, "-60s"), (-30, "-30s"), (0, "Now")]]
        )
        self.trend_legend = self.performance_plot.addLegend(offset=(8, 8))
        self.cpu_trend_curve = self.performance_plot.plot(
            [], [], pen=pg.mkPen(STRESS_CHART_COLORS["blue"], width=2), name="CPU",
        )
        self.gpu_trend_curve = self.performance_plot.plot(
            [], [], pen=pg.mkPen(STRESS_CHART_COLORS["orange"], width=2), name="GPU",
        )
        self.cpu_temp_curve = self.performance_plot.plot(
            [], [], pen=pg.mkPen(STRESS_CHART_COLORS["red"], width=2), name="CPU Temp",
        )
        self.gpu_temp_curve = self.performance_plot.plot(
            [], [], pen=pg.mkPen(STRESS_CHART_COLORS["purple"], width=2), name="GPU Temp",
        )
        self.tj_temp_curve = self.performance_plot.plot(
            [], [], pen=pg.mkPen(STRESS_CHART_COLORS["green"], width=2), name="TJ Temp",
        )
        self.voltage_trend_curve = self.performance_plot.plot(
            [], [], pen=pg.mkPen(STRESS_CHART_COLORS["blue"], width=2), name="Voltage",
        )
        self.current_trend_curve = self.performance_plot.plot(
            [], [], pen=pg.mkPen(STRESS_CHART_COLORS["orange"], width=2), name="Current",
        )
        self.power_trend_curve = self.performance_plot.plot(
            [], [], pen=pg.mkPen(STRESS_CHART_COLORS["green"], width=2), name="Power",
        )
        # Alias retained for integrations which previously referenced the
        # separate thermal plot.
        self.thermal_plot = self.performance_plot
        trends_layout.addWidget(self.performance_plot, 1)
        dashboard_row.addWidget(self.trends_panel, 42)
        content.addLayout(dashboard_row)

        queue_panel = QFrame()
        queue_panel.setObjectName("StressPanel")
        queue_panel.setMinimumHeight(92)
        queue_panel.setMaximumHeight(110)
        queue_layout = QVBoxLayout(queue_panel)
        queue_layout.setContentsMargins(10, 6, 10, 7)
        queue_layout.setSpacing(3)
        queue_title = QLabel("Execution Queue")
        queue_title.setObjectName("SectionTitle")
        queue_layout.addWidget(queue_title)
        self.queue_table = QTableWidget(0, 6)
        self.queue_table.setObjectName("ExecutionQueueTable")
        self.queue_table.setHorizontalHeaderLabels(["#", "Test ID", "Status", "Duration", "Start Time", "Progress"])
        self.queue_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.queue_table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.queue_table.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.queue_table.verticalHeader().hide()
        self.queue_table.verticalHeader().setDefaultSectionSize(27)
        queue_header = self.queue_table.horizontalHeader()
        queue_header.setFixedHeight(25)
        queue_header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        queue_header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        for column in range(2, 6):
            queue_header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        self.queue_table.setColumnWidth(0, 34)
        queue_layout.addWidget(self.queue_table)
        self.queue_list = self.queue_table
        content.addWidget(queue_panel)

        scroll.setWidget(execution_content)
        self.execution_scroll = scroll
        layout.addWidget(scroll, 1)

        action_bar = QFrame()
        action_bar.setObjectName("ExecutionActionBar")
        action_bar.setMinimumHeight(48)
        action_bar.setMaximumHeight(52)
        actions = QHBoxLayout()
        actions.setContentsMargins(8, 5, 8, 5)
        actions.setSpacing(8)
        action_bar.setLayout(actions)
        self.back_to_list_button = _button("← BACK TO TEST LIST")
        self.stop_button = _button("STOP TEST")
        self.continue_button = _button("START TEST", "PrimaryButton")
        self.continue_button.setEnabled(False)
        self.finish_button = _button("FINISH / REVIEW")
        self.pass_button = _button("PASS", "PrimaryButton")
        self.fail_button = _button("FAIL", "DangerButton")
        self.blocked_button = _button("BLOCKED")
        self.open_test_folder_button = _button("OPEN TEST FOLDER")
        self.stop_button.clicked.connect(self.stop_requested.emit)
        self.continue_button.clicked.connect(self.start_requested.emit)
        self.finish_button.clicked.connect(self.finish_requested.emit)
        self.pass_button.clicked.connect(lambda: self._request_review(RuntimeStatus.PASS))
        self.fail_button.clicked.connect(lambda: self._request_review(RuntimeStatus.FAIL))
        self.blocked_button.clicked.connect(lambda: self._request_review(RuntimeStatus.BLOCKED))
        self.back_to_list_button.clicked.connect(self._back_to_test_list)
        self.open_test_folder_button.clicked.connect(self._open_active_folder)
        actions.addWidget(self.back_to_list_button)
        actions.addWidget(self.stop_button)
        actions.addWidget(self.continue_button)
        actions.addWidget(self.finish_button)
        actions.addWidget(self.pass_button)
        actions.addWidget(self.fail_button)
        actions.addWidget(self.blocked_button)
        actions.addStretch()
        self.review_summary_label = QLabel("Not reviewed")
        self.review_summary_label.setObjectName("Muted")
        self.review_summary_label.setMaximumWidth(360)
        actions.addWidget(self.review_summary_label)
        actions.addWidget(self.open_test_folder_button)
        layout.addWidget(action_bar)

        self.evidence_viewer = StressEvidenceViewer()
        self.evidence_viewer.hide()
        self._set_trend_mode("performance")
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
        self.history_table.cellDoubleClicked.connect(self._open_history_result)
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
                SortableTableItem(definition.test_name),
                SortableTableItem(definition.group),
                SortableTableItem(definition.duration_text, definition.duration_seconds if definition.duration_seconds is not None else float("inf")),
                SortableTableItem(definition.severity),
                SortableTableItem("", _display_status(definition.runtime_status)),
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
        self.table.blockSignals(True)
        for row in range(self.table.rowCount()):
            self.table.item(row, 0).setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
        self.table.blockSignals(False)
        self._selection_count_changed()

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
        checked_count = sum(
            self.table.item(row, 0).checkState() == Qt.CheckState.Checked
            for row in range(self.table.rowCount())
            if self.table.item(row, 0) is not None
        )
        if checked_count == 0:
            header_state = Qt.CheckState.Unchecked
        elif checked_count == self.table.rowCount():
            header_state = Qt.CheckState.Checked
        else:
            header_state = Qt.CheckState.PartiallyChecked
        self.select_all_header.setCheckState(header_state)
        self.selected_label.setText(
            f"Selected {len(selected)}  •  Visible {len(self.visible_definitions)}  •  Total {len(self.catalog.definitions)}"
        )
        active = getattr(self, "_active_definition", None)
        runtime_busy = active is not None and active.runtime_status in {
            RuntimeStatus.STARTING,
            RuntimeStatus.RUNNING,
            RuntimeStatus.STOPPING,
        }
        self.run_button.setEnabled(bool(selected) and not runtime_busy)
        self.run_button.setToolTip(
            "A test is currently running. Stop or finish it before selecting another test."
            if runtime_busy
            else ""
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
            self.view_result_button.setEnabled(False)
            self.clear_details()
            return
        self.view_result_button.setEnabled(True)
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

    def _back_from_confirmation(self) -> None:
        """Cancel the pending confirmation/baseline attempt and return to the test list."""
        self.stop_requested.emit()
        self.view_stack.setCurrentIndex(0)
        self.run_button.setEnabled(True)
        self.run_button.setToolTip("")
        self._selection_count_changed()

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

    def _update_execution_action_buttons(self, status) -> None:
        value = status.value if hasattr(status, "value") else str(status)
        value = (value or "").upper()

        is_running = value == "RUNNING"
        is_prepared = value == "PREPARED"
        is_review = value == "NEEDS_REVIEW"
        if hasattr(self, "stop_button"):
            self.stop_button.setVisible(is_running or is_prepared)
            self.stop_button.setEnabled(is_running)
            _set_button_style(
                self.stop_button,
                "DangerButton" if is_running else "OutlineButton",
            )

        if hasattr(self, "continue_button"):
            self.continue_button.setVisible(is_running or is_prepared)
            self.continue_button.setText("START TEST")
            self.continue_button.setEnabled(is_prepared)
            _set_button_style(
                self.continue_button,
                "PrimaryButton" if is_prepared else "OutlineButton",
            )

        if hasattr(self, "finish_button"):
            # Runner.finish_for_review() is meaningful only while RUNNING.
            self.finish_button.setVisible(is_running)
            self.finish_button.setEnabled(is_running)
        for button_name in ("pass_button", "fail_button", "blocked_button"):
            button = getattr(self, button_name, None)
            if button is not None:
                button.setVisible(is_review)
                button.setEnabled(is_review)
        if hasattr(self, "review_summary_label"):
            self.review_summary_label.setVisible(not (is_running or is_prepared))

    def _request_review(self, status: RuntimeStatus) -> None:
        title = f"Review result — {status.value}"
        prompt = "Review comment (optional):"
        comment, accepted = QInputDialog.getMultiLineText(self, title, prompt)
        if accepted:
            self.review_requested.emit(status, comment)

    def _request_selected_result(self) -> None:
        definition = self._definition_for_row(self.table.currentRow())
        if definition is None:
            self.message_label.setText("Select a test row to view its latest result.")
            return
        self.view_result_requested.emit(definition)

    def _open_history_result(self, row: int, _column: int) -> None:
        item = self.history_table.item(row, 5)
        if item is not None:
            folder = item.data(Qt.ItemDataRole.UserRole) or item.text()
            self.result_folder_requested.emit(str(folder))

    def _set_case_monitor_fields(self, fields: list[tuple[str, str]]) -> None:
        for index, (container, label, value) in enumerate(self.case_monitor_fields):
            if index < len(fields):
                label_text, value_text = fields[index]
                label.setText(label_text)
                value.setText(value_text)
                value.setToolTip(value_text if len(value_text) > 36 else "")
                container.show()
            else:
                container.hide()

    def _set_trend_mode(self, mode: str) -> None:
        if mode not in {"performance", "temperature", "power"}:
            mode = "performance"
        self._trend_mode = mode
        for name, button in self.trend_buttons.items():
            button.setChecked(name == mode)

        groups = {
            "performance": (self.cpu_trend_curve, self.gpu_trend_curve),
            "temperature": (self.cpu_temp_curve, self.gpu_temp_curve, self.tj_temp_curve),
            "power": (self.voltage_trend_curve, self.current_trend_curve, self.power_trend_curve),
        }
        for curves in groups.values():
            for curve in curves:
                curve.setVisible(False)
        for curve in groups[mode]:
            curve.setVisible(True)

        title, axis, units = {
            "performance": ("CPU / GPU UTILIZATION", "Utilization", "%"),
            "temperature": ("TEMPERATURE", "Temperature", "°C"),
            "power": ("POWER / BATTERY", "Value", ""),
        }[mode]
        self.trend_chart_title.setText(title)
        self.performance_plot.setLabel("left", axis, units=units or None)
        if mode == "performance":
            self.performance_plot.setYRange(0, 100)
            self.performance_plot.enableAutoRange(axis="y", enable=False)
        else:
            self.performance_plot.enableAutoRange(axis="y", enable=True)

        # Keep the legend limited to the selected segment.
        self.trend_legend.clear()
        names = {
            "performance": ("CPU", "GPU"),
            "temperature": ("CPU", "GPU", "TJ"),
            "power": ("Voltage", "Current", "Power"),
        }[mode]
        for curve, name in zip(groups[mode], names, strict=True):
            self.trend_legend.addItem(curve, name)

    def show_execution(
        self,
        definition,
        manager,
        paths,
        queue,
        decision=None,
        baseline=None,
        prepared_folder=None,
    ) -> None:
        self._active_definition = definition
        self._active_manager = manager
        self._active_paths = paths
        self._active_folder = paths.attempt_dir if paths is not None else prepared_folder
        self._active_strategy = decision
        self._display_warning_count = 0
        self._display_error_count = 0
        self.live_metrics_title.setText("Live Metrics Dashboard")
        self.trends_title.setText("Live Trends — Last 120 Seconds")
        self._pretest_baseline = dict(baseline or {})
        self._case_context = case_presentation_context(definition)
        self.execution_title.setText(f"{definition.test_id} — {definition.test_name}")
        self.execution_subtitle.setText(self._case_context.purpose)
        self.execution_subtitle.setToolTip(self._case_context.purpose)
        self.case_domain_badge.setText(self._case_context.primary_domain)
        for key, card in self.metric_cards.items():
            card.setProperty("relevant", key in self._case_context.relevant_metric_cards)
            card.style().unpolish(card)
            card.style().polish(card)
        self._set_trend_mode(self._case_context.preferred_trend)
        if manager is not None and paths is not None:
            self.evidence_viewer.set_evidence(manager.records, paths.attempt_dir, definition)
            manager.evidence_changed.connect(self.evidence_viewer.update_evidence)
        self._update_execution_action_buttons(definition.runtime_status)
        self.set_queue(queue)
        self._execution_metrics = SystemMetricsSummary(
            target_cpu_percent=decision.target_percent if decision else None
        )
        self._execution_metrics_tail = IncrementalLogTail(initial_lines=100)
        self._tegrastats_tail = IncrementalLogTail(initial_lines=100)
        self._thermal_zones_tail = IncrementalLogTail(initial_lines=100)
        self._robot_metrics_tail = IncrementalLogTail(initial_lines=100)
        self._sensor_health_tail = IncrementalLogTail(initial_lines=100)
        self._performance_auto_tail = IncrementalLogTail(initial_lines=100)
        self._workload_tail = IncrementalLogTail(initial_lines=100)

        self._gpu_usage = None
        self._cpu_temp = None
        self._gpu_temp = None
        self._thermal_zones = {}
        self._robot_metrics = {}
        self._sensor_health = {}
        self._performance_auto = {}

        # Live trend history: one point per execution refresh.
        # Old samples are automatically discarded outside the 120 s window.
        self._trend_history = {
            "time": deque(),
            "cpu": deque(),
            "gpu": deque(),
            "cpu_temp": deque(),
            "gpu_temp": deque(),
            "tj_temp": deque(),
            "voltage": deque(),
            "current": deque(),
            "power": deque(),
        }
        for curve in (
            self.cpu_trend_curve,
            self.gpu_trend_curve,
            self.cpu_temp_curve,
            self.gpu_temp_curve,
            self.tj_temp_curve,
            self.voltage_trend_curve,
            self.current_trend_curve,
            self.power_trend_curve,
        ):
            curve.setData([], [])

        # Case-specific runtime tracking.
        self._case_monitor_last_elapsed = None
        self._case_target_seconds = 0
        self._case_outside_seconds = 0
        self._case_target_samples = 0
        self._case_outside_samples = 0
        self._ram_usage_samples = []
        self._ram_used_samples = []
        self._ram_available_samples = []
        self._last_memory_sample_elapsed = None

        self._injected_load = None
        self._load_state = "MONITORING" if decision and decision.strategy == LoadStrategy.OBSERVE_ONLY else "RAMPING"
        records = manager.records if manager is not None else ()
        metrics_record = next((record for record in records if record.collector == "system_metrics"), None)
        if metrics_record:
            content = self._execution_metrics_tail.select(paths.attempt_dir / metrics_record.file)
            self._execution_metrics.feed(content)
        tegrastats_record = next(
            (record for record in records if record.collector == "tegrastats"),
            None,
        )
        if tegrastats_record:
            content = self._tegrastats_tail.select(paths.attempt_dir / tegrastats_record.file)
            self._feed_tegrastats(content)

        thermal_record = next(
            (record for record in records if record.collector == "thermal_zones"),
            None,
        )
        if thermal_record:
            content = self._thermal_zones_tail.select(paths.attempt_dir / thermal_record.file)
            self._feed_thermal_zones(content)

        robot_record = next(
            (record for record in records if record.collector == "robot_metrics"),
            None,
        )
        if robot_record:
            content = self._robot_metrics_tail.select(paths.attempt_dir / robot_record.file)
            self._feed_robot_metrics(content)

        sensor_record = next(
            (record for record in records if record.collector == "sensor_health_auto"),
            None,
        )
        if sensor_record:
            content = self._sensor_health_tail.select(paths.attempt_dir / sensor_record.file)
            self._feed_sensor_health(content)

        performance_record = next(
            (record for record in records if record.collector == "performance_auto"),
            None,
        )
        if performance_record:
            content = self._performance_auto_tail.select(
                paths.attempt_dir / performance_record.file
            )
            self._feed_performance_auto(content)

        workload_record = next((record for record in records if record.collector == "workload"), None)
        if workload_record:
            self._workload_tail.select(paths.attempt_dir / workload_record.file)
        self._render_strategy_summary()
        self._render_live_metrics_dashboard()
        self._render_case_monitor(definition, 0)
        self.update_active_execution(definition, 0, manager)
        self.view_stack.setCurrentIndex(2)

    def _back_to_test_list(self) -> None:
        self.view_stack.setCurrentIndex(0)

    def _view_running_test(self) -> None:
        if getattr(self, "_active_definition", None) is not None:
            self.view_stack.setCurrentIndex(2)

    def update_active_execution(self, definition, elapsed: int, manager) -> None:
        active = definition.runtime_status in {
            RuntimeStatus.STARTING,
            RuntimeStatus.PREPARED,
            RuntimeStatus.RUNNING,
            RuntimeStatus.STOPPING,
        }
        self.active_banner.setVisible(active)
        runtime_busy = definition.runtime_status in {
            RuntimeStatus.STARTING,
            RuntimeStatus.RUNNING,
            RuntimeStatus.STOPPING,
        }
        self.run_button.setEnabled(bool(self.selected_definitions()) and not runtime_busy)
        self.run_button.setToolTip(
            "A test is currently running. Stop or finish it before selecting another test."
            if runtime_busy
            else ""
        )
        if not active:
            return
        self.view_running_button.setText(
            "VIEW PREPARED TEST"
            if definition.runtime_status == RuntimeStatus.PREPARED
            else "VIEW RUNNING TEST"
        )
        target = definition.duration_seconds
        target_text = _format_seconds(target) if target is not None else "—"
        self.active_test_label.setText(f"● {definition.test_id} — {definition.test_name}")
        self.active_status.set_status(definition.runtime_status)
        active_count = manager.active_count() if manager is not None else 0
        warning_count = manager.warning_count() if manager is not None else 0
        error_count = manager.error_count() if manager is not None else 0
        self.active_metrics_label.setText(
            f"Elapsed: {_format_seconds(elapsed)} / {target_text}    "
            f"Evidence: {active_count} Active    "
            f"Warnings: {warning_count}    Errors: {error_count}"
        )

    def clear_active_execution(self) -> None:
        self.active_banner.hide()
        self._active_definition = None
        self._active_manager = None
        self._active_paths = None
        self._active_folder = None
        self._selection_count_changed()

    def _open_active_folder(self) -> None:
        folder = getattr(self, "_active_folder", None)
        if folder is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def update_execution(self, definition, elapsed: int, manager) -> None:
        self.execution_status.set_status(definition.runtime_status)
        self._update_execution_action_buttons(definition.runtime_status)
        target = definition.duration_seconds
        remaining = max(0, target - elapsed) if target is not None else None
        text = f"Elapsed: {_format_seconds(elapsed)}"
        if target is not None:
            text += f"    Target: {_format_seconds(target)}    Remaining: {_format_seconds(remaining)}"
        self.execution_times.setText(text)
        active_count = manager.active_count() if manager is not None else 0
        warning_count = manager.warning_count() if manager is not None else getattr(self, "_display_warning_count", 0)
        error_count = manager.error_count() if manager is not None else getattr(self, "_display_error_count", 0)
        self.execution_counts.setText(f"Evidence: {active_count} Active    Warnings: {warning_count}    Errors: {error_count}")
        self.active_summary_values["elapsed"].setText(_format_seconds(elapsed))
        self.active_summary_values["remaining"].setText(_format_seconds(remaining))
        self.active_summary_values["target"].setText(_format_seconds(target))
        self.active_summary_values["evidence"].setText(f"{active_count} Active")
        self.active_summary_values["warnings"].setText(str(warning_count))
        self.active_summary_values["errors"].setText(str(error_count))
        if hasattr(self, "_execution_metrics_tail"):
            content = self._execution_metrics_tail.read_new()
            if content:
                self._execution_metrics.feed(content)
        if hasattr(self, "_tegrastats_tail"):
            tegra_content = self._tegrastats_tail.read_new()
            if tegra_content:
                self._feed_tegrastats(tegra_content)

        if hasattr(self, "_thermal_zones_tail"):
            thermal_content = self._thermal_zones_tail.read_new()
            if thermal_content:
                self._feed_thermal_zones(thermal_content)

        if hasattr(self, "_robot_metrics_tail"):
            robot_content = self._robot_metrics_tail.read_new()
            if robot_content:
                self._feed_robot_metrics(robot_content)

        if hasattr(self, "_sensor_health_tail"):
            sensor_content = self._sensor_health_tail.read_new()
            if sensor_content:
                self._feed_sensor_health(sensor_content)

        if hasattr(self, "_performance_auto_tail"):
            performance_content = self._performance_auto_tail.read_new()
            if performance_content:
                self._feed_performance_auto(performance_content)

        if hasattr(self, "_workload_tail"):
            workload = self._workload_tail.read_new()
            for injected in re.findall(r"injected=([\d.]+)%", workload):
                self._injected_load = float(injected)
            states = re.findall(r"state=([A-Z_]+)", workload)
            if states:
                self._load_state = states[-1].replace("_", " ")
        if definition.runtime_status == RuntimeStatus.RUNNING and getattr(self, "_last_memory_sample_elapsed", None) != elapsed:
            state = self._execution_metrics.state
            if state.ram_usage_percent is not None:
                self._ram_usage_samples.append(state.ram_usage_percent)
            if state.ram_used is not None:
                self._ram_used_samples.append(state.ram_used)
            if state.ram_available is not None:
                self._ram_available_samples.append(state.ram_available)
            self._last_memory_sample_elapsed = elapsed
        self._render_strategy_summary()
        self._render_live_metrics_dashboard()
        self._render_case_monitor(definition, elapsed)
        self._update_live_trends(elapsed)
        self._update_queue_rows(elapsed)
        self.update_active_execution(definition, elapsed, manager)

    @staticmethod
    def _plain_mapping(value) -> dict:
        return dict(value) if isinstance(value, dict) else {}

    def build_final_metrics_snapshot(
        self,
        definition,
        elapsed: int,
        manager=None,
    ) -> dict:
        """Serialize the already parsed dashboard state; no collector is rerun."""

        metrics = getattr(self, "_execution_metrics", None)
        state = metrics.state if metrics is not None else None
        records = tuple(getattr(manager, "records", ()) or ())
        warnings = [
            record.error or record.name
            for record in records
            if record.status == CollectorStatus.WARNING
        ]
        errors = [
            record.error or record.name
            for record in records
            if record.status == CollectorStatus.ERROR
        ]
        cpu = {}
        memory = {}
        if metrics is not None:
            cpu = {
                "current_percent": metrics.current_cpu,
                "avg_percent": metrics.average_cpu,
                "min_percent": metrics.minimum_cpu,
                "max_percent": metrics.maximum_cpu,
                "usage_by_cpu": dict(state.cpu_usage),
                "samples": list(metrics._overall_samples),
            }
        if state is not None:
            memory = {
                "total_bytes": state.ram_total,
                "used_bytes": state.ram_used,
                "available_bytes": state.ram_available,
                "usage_percent": state.ram_usage_percent,
                "swap_total_bytes": state.swap_total,
                "swap_used_bytes": state.swap_used,
                "loads": list(state.loads) if state.loads is not None else None,
                "uptime_seconds": state.uptime_seconds,
                "timestamp": state.timestamp,
                "usage_samples": list(getattr(self, "_ram_usage_samples", [])),
                "used_samples": list(getattr(self, "_ram_used_samples", [])),
                "available_samples": list(getattr(self, "_ram_available_samples", [])),
            }
        robot = dict(getattr(self, "_robot_metrics", {}) or {})
        thermal = dict(getattr(self, "_thermal_zones", {}) or {})
        if getattr(self, "_cpu_temp", None) is not None:
            thermal.setdefault("cpu_thermal", self._cpu_temp)
        if getattr(self, "_gpu_temp", None) is not None:
            thermal.setdefault("gpu_thermal", self._gpu_temp)
        performance = dict(getattr(self, "_performance_auto", {}) or {})
        if getattr(self, "_gpu_usage", None) is not None:
            performance.setdefault("gpu_usage", self._gpu_usage)

        case_monitor = {
            label.text(): value.text()
            for container, label, value in self.case_monitor_fields
            if not container.isHidden()
        }
        trends = {
            key: list(values)
            for key, values in getattr(self, "_trend_history", {}).items()
        }
        system = {}
        for key, value in (
            ("cpu", cpu),
            ("memory", memory),
            ("thermal", thermal),
            ("battery", {k: v for k, v in robot.items() if k.startswith("battery_")}),
            ("motor", {k: v for k, v in robot.items() if k.startswith("motor_")}),
            ("sensor_health", dict(getattr(self, "_sensor_health", {}) or {})),
            ("performance", performance),
        ):
            if value:
                system[key] = value
        return {
            "captured_at": utc_now(),
            "elapsed_sec": int(max(0, elapsed)),
            "test": {
                "test_id": definition.test_id,
                "test_name": definition.test_name,
                "group": definition.group,
                "duration_sec": definition.duration_seconds,
                "execution_type": definition.execution_type.value,
            },
            "case_monitor": case_monitor,
            "system": system,
            "robot_metrics": robot,
            "trends": trends,
            "warning_count": len(warnings),
            "error_count": len(errors),
            "warnings": warnings,
            "errors": errors,
        }

    def finalize_execution_display(self, definition, paths, elapsed: int, manager) -> dict:
        """Freeze the final live state while releasing runtime ownership."""

        self._load_state = "FINAL / STOPPED"
        self.update_execution(definition, elapsed, manager)
        snapshot = self.build_final_metrics_snapshot(definition, elapsed, manager)
        persist_final_metrics(paths.attempt_dir, snapshot)
        self._display_snapshot = snapshot
        self._display_warning_count = snapshot["warning_count"]
        self._display_error_count = snapshot["error_count"]
        self._active_manager = None
        self._active_paths = paths
        self._active_folder = paths.attempt_dir
        self.active_banner.hide()
        self.execution_status.set_status(definition.runtime_status)
        self._update_execution_action_buttons(definition.runtime_status)
        self.review_summary_label.setText("Reviewer Decision: Not reviewed")
        self.live_metrics_title.setText("Final Runtime Metrics")
        self.trends_title.setText("Final Trends — Last 120 Seconds")
        self.view_stack.setCurrentIndex(2)
        return snapshot

    def _render_strategy_summary(self) -> None:
        definition = getattr(self, "_active_definition", None)
        if definition is None:
            self.strategy_summary_label.setText("Not applicable")
            return
        context = getattr(self, "_case_context", case_presentation_context(definition))
        decision = getattr(self, "_active_strategy", None)
        if context.has_cpu_target and decision is not None:
            strategy = decision.strategy.value.replace("_", " ")
            target = "—" if decision.target_percent is None else f"{decision.target_percent:.1f} %"
            baseline = "—" if decision.baseline_percent is None else f"{decision.baseline_percent:.1f} %"
            self.strategy_summary_label.setText(
                f"PRE-TEST Baseline CPU: {baseline} | Target CPU: {target} | Strategy: {strategy}"
            )
        else:
            self.strategy_summary_label.setText(
                f"{context.primary_domain} | {definition.execution_type.value} | "
                f"{len(definition.evidence_plan)} evidence items"
            )

    def _feed_performance_auto(self, content: str) -> None:
        if not content:
            return

        for raw_line in content.splitlines():
            line = raw_line.strip()
            if not line:
                continue

            for name, value in re.findall(
                r"([a-zA-Z0-9_]+)=([-\d.]+)",
                line,
            ):
                try:
                    self._performance_auto[name] = float(value)
                except ValueError:
                    continue

    def _feed_sensor_health(self, content: str) -> None:
        if not content:
            return

        for raw_line in content.splitlines():
            line = raw_line.strip()
            if not line:
                continue

            for name, value in re.findall(r"([a-zA-Z0-9_]+)=([-\d.]+)", line):
                try:
                    self._sensor_health[name] = float(value)
                except ValueError:
                    continue

    def _feed_robot_metrics(self, content: str) -> None:
        if not content:
            return

        for raw_line in content.splitlines():
            line = raw_line.strip()
            if not line:
                continue

            for name, value in re.findall(r"([a-zA-Z0-9_]+)=([-\d.]+)", line):
                try:
                    self._robot_metrics[name] = float(value)
                except ValueError:
                    continue

    def _feed_thermal_zones(self, content: str) -> None:
        if not content:
            return

        for raw_line in content.splitlines():
            line = raw_line.strip()
            if not line:
                continue

            for name, value in re.findall(r"([a-zA-Z0-9_]+)=([\d.]+)", line):
                try:
                    self._thermal_zones[name] = float(value)
                except ValueError:
                    continue

    def _feed_tegrastats(self, content: str) -> None:
        if not content:
            return

        for raw_line in content.splitlines():
            line = raw_line.strip()
            if not line:
                continue

            gpu = re.search(r"GR3D_FREQ\s+(\d+(?:\.\d+)?)%", line)
            if gpu:
                self._gpu_usage = float(gpu.group(1))

            cpu_temp = re.search(
                r"CPU(?:@|\s+temp=)([\d.]+)C",
                line,
                re.IGNORECASE,
            )
            if cpu_temp:
                self._cpu_temp = float(cpu_temp.group(1))

            gpu_temp = re.search(
                r"GPU(?:@|\s+temp=)([\d.]+)C",
                line,
                re.IGNORECASE,
            )
            if gpu_temp:
                self._gpu_temp = float(gpu_temp.group(1))

            # Jetson tegrastats format:
            # cpu@69.5C soc2@64.031C soc0@63.875C
            # gpu@63.906C tj@69.5C soc1@64.281C
            thermal_names = {
                "cpu": "cpu_thermal",
                "gpu": "gpu_thermal",
                "soc0": "soc0_thermal",
                "soc1": "soc1_thermal",
                "soc2": "soc2_thermal",
                "tj": "tj_thermal",
            }

            for source_name, target_name in thermal_names.items():
                match = re.search(
                    rf"\b{source_name}@([\d.]+)C",
                    line,
                    re.IGNORECASE,
                )
                if match:
                    self._thermal_zones[target_name] = float(match.group(1))

    @staticmethod
    def _apply_metric_card_state(card, state: str) -> None:
        """Apply visual state only; this does not decide test PASS/FAIL."""
        value = (state or "NO DATA").upper()
        if value in {"NORMAL", "OK", "TARGET OK", "DETECTED"}:
            tone = "ok"
        elif value in {"WARNING", "BELOW TARGET", "ABOVE TARGET", "NOT DETECTED"}:
            tone = "warning"
        elif value in {"CRITICAL", "ERROR", "FAULT"}:
            tone = "error"
        elif value == "SUSPECT":
            tone = "suspect"
        else:
            tone = "no_data"
        card.setProperty("state", tone)
        card.style().unpolish(card)
        card.style().polish(card)

    @classmethod
    def _aggregate_thermal_status(cls, values) -> str:
        states = [
            cls._thermal_status(value)
            for value in values
            if value is not None
        ]

        if not states:
            return "NO DATA"

        priority = (
            "CRITICAL",
            "SUSPECT",
            "WARNING",
            "NORMAL",
        )

        for state in priority:
            if state in states:
                return state

        return "NO DATA"

    @staticmethod
    def _thermal_status(value):
        if value is None:
            return "NO DATA"

        if value < 5.0:
            return "SUSPECT"

        if value >= 90.0:
            return "CRITICAL"

        if value >= 80.0:
            return "WARNING"

        return "NORMAL"

    @staticmethod
    def _cpu_target_status(current, target):
        if current is None or target is None:
            return "NO TARGET"

        tolerance = 3.0

        if current < target - tolerance:
            return "BELOW TARGET"
        if current > target + tolerance:
            return "ABOVE TARGET"
        return "TARGET OK"

    @staticmethod
    def _temperature_detail_text(
        values: dict,
        prefix: str,
        label_prefix: str,
        *,
        items_per_line: int = 4,
    ) -> str:
        indexed = []

        for key, value in values.items():
            match = re.fullmatch(
                re.escape(prefix) + r"(\d+)",
                key,
            )
            if not match:
                continue

            try:
                index = int(match.group(1))
                temperature = float(value)
            except (TypeError, ValueError):
                continue

            indexed.append((index, temperature))

        if not indexed:
            return "Waiting for data..."

        indexed.sort(key=lambda item: item[0])

        items = []

        for index, temperature in indexed:
            state = VDCatalogPage._thermal_status(temperature)

            if state == "CRITICAL":
                color = "#d9534f"
            elif state == "WARNING":
                color = "#d89b22"
            elif state == "SUSPECT":
                color = "#c06bcf"
            elif state == "NORMAL":
                color = "#2e9d59"
            else:
                color = "#6f7782"

            items.append(
                f'<span style="color:{color}; font-weight:600;">'
                f'{label_prefix}{index}: {temperature:.1f} °C '
                f'[{state}]'
                f'</span>'
            )

        lines = [
            " &nbsp;&nbsp; | &nbsp;&nbsp; ".join(
                items[pos:pos + items_per_line]
            )
            for pos in range(0, len(items), items_per_line)
        ]

        return "<br>".join(lines)

    def _render_case_monitor(self, definition, elapsed: int) -> None:
        context = getattr(self, "_case_context", case_presentation_context(definition))
        metrics = getattr(self, "_execution_metrics", None)
        state = metrics.state if metrics is not None else None
        robot = getattr(self, "_robot_metrics", {})
        sensor = getattr(self, "_sensor_health", {})
        zones = getattr(self, "_thermal_zones", {})
        decision = getattr(self, "_active_strategy", None)
        baseline = getattr(self, "_pretest_baseline", {})
        manager = getattr(self, "_active_manager", None)

        def pct(value):
            return "—" if value is None else f"{value:.1f} %"

        def number(value, suffix="", decimals=1):
            return "—" if value is None else f"{float(value):.{decimals}f}{suffix}"

        def temperature(value):
            return number(value, " °C")

        status = _display_status(definition.runtime_status)
        duration = _format_seconds(definition.duration_seconds)
        evidence_count = len(definition.evidence_plan)
        manual_state = "MANUAL REQUIRED" if context.has_manual_evidence else "NOT REQUIRED"

        if context.has_cpu_target:
            target = decision.target_percent if decision is not None else definition.target_cpu_percent
            baseline_cpu = decision.baseline_percent if decision is not None else None
            strategy = decision.strategy.value.replace("_", " ") if decision is not None else "N/A"
            current = metrics.current_cpu if metrics is not None else None
            fields = [
                ("PRE-TEST Baseline CPU", pct(baseline_cpu)),
                ("Target CPU", pct(target)),
                ("Strategy", strategy),
                ("Injected Load", "—" if self._injected_load is None else f"{self._injected_load:.1f} %"),
                ("Current CPU", pct(current)),
                ("Average CPU", pct(metrics.average_cpu if metrics else None)),
                ("Min CPU", pct(metrics.minimum_cpu if metrics else None)),
                ("Max CPU", pct(metrics.maximum_cpu if metrics else None)),
                ("Measured State", self._cpu_target_status(current, target)),
                ("Controller State", self._load_state),
            ]
        elif context.primary_domain == "MEMORY":
            base = dict(baseline.get("memory", {}) or {})
            ram_used = state.ram_used if state else None
            ram_available = state.ram_available if state else None
            swap_used = state.swap_used if state else None
            swap_available = max(0, state.swap_total - state.swap_used) if state and state.swap_total is not None and state.swap_used is not None else None
            usage_samples = getattr(self, "_ram_usage_samples", [])
            used_samples = getattr(self, "_ram_used_samples", [])
            available_samples = getattr(self, "_ram_available_samples", [])
            fields = [
                ("PRE-TEST Memory Used", number(base.get("used_gib"), " GiB", 2)),
                ("PRE-TEST Available", number(base.get("available_gib"), " GiB", 2)),
                ("Target", "N/A"),
                ("Current Status", status),
                ("Current RAM", format_bytes(ram_used)),
                ("Memory Available", format_bytes(ram_available)),
                ("Memory Usage", pct(state.ram_usage_percent if state else None)),
                ("Swap Used / Available", f"{format_bytes(swap_used)} / {format_bytes(swap_available)}"),
                ("Average RAM", pct(sum(usage_samples) / len(usage_samples) if usage_samples else None)),
                ("Peak RAM", format_bytes(max(used_samples) if used_samples else None)),
                ("Minimum Available RAM", format_bytes(min(available_samples) if available_samples else None)),
            ]
        elif context.primary_domain == "THERMAL":
            temperatures = [
                zones.get("cpu_thermal", getattr(self, "_cpu_temp", None)),
                zones.get("gpu_thermal", getattr(self, "_gpu_temp", None)),
                zones.get("soc0_thermal"), zones.get("soc1_thermal"),
                zones.get("soc2_thermal"), zones.get("tj_thermal"),
            ]
            present = [value for value in temperatures if value is not None]
            fields = [
                ("CPU Temp", temperature(temperatures[0])), ("GPU Temp", temperature(temperatures[1])),
                ("SoC0 Temp", temperature(temperatures[2])), ("SoC1 Temp", temperature(temperatures[3])),
                ("SoC2 Temp", temperature(temperatures[4])), ("TJ Temp", temperature(temperatures[5])),
                ("Min / Avg / Max", "—" if not present else f"{min(present):.1f} / {sum(present)/len(present):.1f} / {max(present):.1f} °C"),
                ("Thermal State", self._aggregate_thermal_status(present)),
                ("Throttling State", "—"),
            ]
        elif context.primary_domain == "POWER":
            voltage = robot.get("battery_pack_voltage")
            current = robot.get("battery_pack_current")
            fields = [
                ("SOC", number(robot.get("battery_soc"), " %", 0)), ("SOH", number(robot.get("battery_soh"), " %", 0)),
                ("Pack Voltage", number(voltage, " V", 2)), ("Current", number(current, " A", 2)),
                ("Battery Temp Min", temperature(robot.get("battery_temp_min"))),
                ("Battery Temp Avg", temperature(robot.get("battery_temp_avg"))),
                ("Battery Temp Max", temperature(robot.get("battery_temp_max"))),
                ("Power", "—" if voltage is None or current is None else f"{voltage * current:.1f} W"),
                ("Error State", "—" if robot.get("motor_bus_error_code") is None else "OK" if robot.get("motor_bus_error_code") == 0 else "ERROR"),
            ]
        elif context.primary_domain in {"MOTOR", "MOTION"}:
            alive, total = robot.get("motor_alive"), robot.get("motor_total")
            fields = [
                ("Motor Alive Count", "—" if alive is None else f"{int(alive)} / {int(total) if total is not None else '—'}"),
                ("Motor Errors", "—" if robot.get("motor_errors") is None else str(int(robot["motor_errors"]))),
                ("Bus Voltage", number(robot.get("motor_bus_voltage"), " V", 2)),
                ("Bus Current", number(robot.get("motor_bus_current"), " A", 2)),
                ("Motor Temp Min", temperature(robot.get("motor_temp_min"))),
                ("Motor Temp Avg", temperature(robot.get("motor_temp_avg"))),
                ("Motor Temp Max", temperature(robot.get("motor_temp_max"))),
                ("Thermal State", self._thermal_status(robot.get("motor_temp_max"))),
                ("Error Code", "—" if robot.get("motor_bus_error_code") is None else str(int(robot["motor_bus_error_code"]))),
            ]
        elif context.primary_domain == "SENSOR":
            fields = [
                ("ROS Health", "—" if sensor.get("ros_health") is None else "OK" if sensor.get("ros_health") >= 1 else "ERROR"),
                ("ROS Node Count", "—" if sensor.get("ros_nodes") is None else str(int(sensor["ros_nodes"]))),
            ]
            dependencies = set(definition.dependencies)
            for dependency, label, key, suffix in (
                ("camera", "Camera FPS", "camera_fps", " FPS"),
                ("lidar", "LiDAR Hz", "lidar_hz", " Hz"),
                ("imu", "IMU Hz", "imu_hz", " Hz"),
            ):
                if dependency in dependencies:
                    fields.append((label, number(sensor.get(key), suffix)))
            fields.extend((("Required Evidence", str(evidence_count)), ("Manual Evidence", manual_state), ("Current Status", status)))
        elif context.primary_domain == "ENDURANCE":
            target = definition.duration_seconds
            remaining = max(0, target - elapsed) if target is not None else None
            progress = min(100.0, 100.0 * elapsed / target) if target else None
            fields = [
                ("Elapsed", _format_seconds(elapsed)), ("Remaining", _format_seconds(remaining)),
                ("Progress", pct(progress)), ("Runtime Errors", str(manager.error_count() if manager else 0)),
                ("Warnings", str(manager.warning_count() if manager else 0)), ("Recovery Count", "—"),
                ("Checkpoints", "MANUAL REQUIRED" if context.has_manual_evidence else "N/A"),
                ("System Stability", "NO DATA" if elapsed == 0 else "MONITORING"),
            ]
        elif context.primary_domain == "STORAGE":
            fields = [
                ("Execution Type", definition.execution_type.value),
                ("Workload", definition.workload_description or "MANUAL / GUIDED"),
                ("Duration", duration), ("I/O Telemetry", "—"), ("Current BW / IOPS", "— / —"),
                ("Latency", "—"), ("Storage Errors", "—"), ("Filesystem Errors", "—"),
                ("Kernel Storage Errors", "—"), ("Current Status", status),
            ]
        else:
            fields = [
                ("Execution Type", definition.execution_type.value), ("Test Scope", definition.group or "—"),
                ("Required Evidence", str(evidence_count)), ("Manual Evidence", manual_state),
                ("Duration", duration), ("Severity", definition.severity or "—"), ("Current Status", status),
            ]
        self._set_case_monitor_fields(fields)

    def _update_live_trends(self, elapsed: int) -> None:
        history = getattr(self, "_trend_history", None)
        metrics = getattr(self, "_execution_metrics", None)
        definition = getattr(self, "_active_definition", None)

        if (
            history is None
            or metrics is None
            or definition is None
            or definition.runtime_status != RuntimeStatus.RUNNING
        ):
            return

        zones = getattr(self, "_thermal_zones", {})
        perf = getattr(self, "_performance_auto", {})

        cpu = metrics.current_cpu

        gpu = perf.get(
            "gpu_usage",
            getattr(self, "_gpu_usage", None),
        )

        cpu_temp = zones.get(
            "cpu_thermal",
            getattr(self, "_cpu_temp", None),
        )

        gpu_temp = zones.get(
            "gpu_thermal",
            getattr(self, "_gpu_temp", None),
        )

        tj_temp = zones.get("tj_thermal")
        robot = getattr(self, "_robot_metrics", {})
        voltage = robot.get("battery_pack_voltage")
        current = robot.get("battery_pack_current")
        power = voltage * current if voltage is not None and current is not None else None

        history["time"].append(float(elapsed))
        history["cpu"].append(cpu)
        history["gpu"].append(gpu)
        history["cpu_temp"].append(cpu_temp)
        history["gpu_temp"].append(gpu_temp)
        history["tj_temp"].append(tj_temp)
        history["voltage"].append(voltage)
        history["current"].append(current)
        history["power"].append(power)

        cutoff = float(elapsed) - 120.0

        while history["time"] and history["time"][0] < cutoff:
            for values in history.values():
                values.popleft()

        self._render_trend_history()

    def _render_trend_history(self) -> None:
        history = getattr(self, "_trend_history", None)

        if not history or not history.get("time"):
            for curve in (
                self.cpu_trend_curve, self.gpu_trend_curve,
                self.cpu_temp_curve, self.gpu_temp_curve, self.tj_temp_curve,
                self.voltage_trend_curve, self.current_trend_curve, self.power_trend_curve,
            ):
                curve.setData([], [])
            return

        latest = history["time"][-1]
        x = [value - latest for value in history["time"]]

        def plot_values(curve, values):
            filtered_x = []
            filtered_y = []

            for x_value, y_value in zip(x, values):
                if y_value is None:
                    continue
                filtered_x.append(x_value)
                filtered_y.append(float(y_value))

            curve.setData(filtered_x, filtered_y)

        plot_values(self.cpu_trend_curve, history["cpu"])
        plot_values(self.gpu_trend_curve, history["gpu"])
        plot_values(self.cpu_temp_curve, history["cpu_temp"])
        plot_values(self.gpu_temp_curve, history["gpu_temp"])
        plot_values(self.tj_temp_curve, history["tj_temp"])
        plot_values(self.voltage_trend_curve, history["voltage"])
        plot_values(self.current_trend_curve, history["current"])
        plot_values(self.power_trend_curve, history["power"])

    def show_result(self, definition, loaded: dict) -> None:
        """Render a persisted attempt without creating collectors or workload."""

        result = dict(loaded.get("result") or {})
        info = dict(loaded.get("test_info") or {})
        snapshot = dict(loaded.get("final_metrics") or {})
        attempt_dir = Path(loaded["attempt_dir"])
        display_definition = copy(definition)
        try:
            display_definition.runtime_status = RuntimeStatus(
                result.get("status") or info.get("status") or RuntimeStatus.STOPPED.value
            )
        except ValueError:
            display_definition.runtime_status = RuntimeStatus.ERROR

        strategy_data = dict(loaded.get("load_strategy") or {})
        strategy = None
        if strategy_data:
            try:
                strategy_value = LoadStrategy(strategy_data.get("strategy"))
            except (TypeError, ValueError):
                strategy_value = LoadStrategy.NEEDS_REVIEW
            strategy = SimpleNamespace(
                strategy=strategy_value,
                target_percent=strategy_data.get("target_percent"),
                baseline_percent=strategy_data.get("baseline_percent"),
            )
        paths = SimpleNamespace(
            attempt_dir=attempt_dir,
            session_dir=attempt_dir.parents[2] if len(attempt_dir.parents) > 2 else attempt_dir.parent,
            attempt=result.get("attempt", info.get("attempt", 0)),
        )
        self.show_execution(
            display_definition,
            None,
            paths,
            [display_definition],
            strategy,
            loaded.get("pretest_baseline") or {},
        )
        self._display_snapshot = snapshot
        self._display_warning_count = int(snapshot.get("warning_count", len(result.get("warnings") or [])))
        self._display_error_count = int(snapshot.get("error_count", len(result.get("errors") or [])))

        system = dict(snapshot.get("system") or {})
        cpu = dict(system.get("cpu") or {})
        memory = dict(system.get("memory") or {})
        state = self._execution_metrics.state
        state.cpu_usage = dict(cpu.get("usage_by_cpu") or {})
        if cpu.get("current_percent") is not None:
            state.cpu_usage.setdefault("cpu", cpu["current_percent"])
        self._execution_metrics._overall_samples = list(cpu.get("samples") or [])
        if not self._execution_metrics._overall_samples and cpu.get("avg_percent") is not None:
            self._execution_metrics._overall_samples = [float(cpu["avg_percent"])]
        state.ram_total = memory.get("total_bytes")
        state.ram_used = memory.get("used_bytes")
        state.ram_available = memory.get("available_bytes")
        state.swap_total = memory.get("swap_total_bytes")
        state.swap_used = memory.get("swap_used_bytes")
        loads = memory.get("loads")
        state.loads = tuple(loads) if isinstance(loads, list) and len(loads) == 3 else None
        state.uptime_seconds = memory.get("uptime_seconds")
        state.timestamp = memory.get("timestamp")
        self._ram_usage_samples = list(memory.get("usage_samples") or [])
        self._ram_used_samples = list(memory.get("used_samples") or [])
        self._ram_available_samples = list(memory.get("available_samples") or [])
        self._thermal_zones = dict(system.get("thermal") or {})
        self._robot_metrics = dict(snapshot.get("robot_metrics") or {})
        if not self._robot_metrics:
            self._robot_metrics.update(system.get("battery") or {})
            self._robot_metrics.update(system.get("motor") or {})
        self._sensor_health = dict(system.get("sensor_health") or {})
        self._performance_auto = dict(system.get("performance") or {})
        self._gpu_usage = self._performance_auto.get("gpu_usage")
        self._cpu_temp = self._thermal_zones.get("cpu_thermal")
        self._gpu_temp = self._thermal_zones.get("gpu_thermal")
        self._load_state = "FINAL / STOPPED"

        saved_trends = dict(snapshot.get("trends") or {})
        for key in self._trend_history:
            self._trend_history[key] = deque(saved_trends.get(key) or [])
        self._render_live_metrics_dashboard()
        elapsed = int(float(snapshot.get("elapsed_sec", result.get("elapsed_sec", 0)) or 0))
        self._render_case_monitor(display_definition, elapsed)
        self._render_trend_history()
        self.update_execution(display_definition, elapsed, None)
        reviewed = result.get("review_status")
        comment = result.get("review_comment") or ""
        if reviewed:
            summary = f"Reviewer Decision: {reviewed}"
            if comment:
                summary += f" — {comment}"
        else:
            summary = "Reviewer Decision: Not reviewed"
        self.review_summary_label.setText(summary)
        self.review_summary_label.setToolTip(comment)
        self.live_metrics_title.setText("Final Runtime Metrics")
        self.trends_title.setText("Final Trends — Last 120 Seconds")
        self._active_manager = None
        self.active_banner.hide()
        self.view_stack.setCurrentIndex(2)

    def _render_live_metrics_dashboard(self) -> None:
        metrics = getattr(self, "_execution_metrics", None)
        if metrics is None:
            return

        def pct(value):
            return "—" if value is None else f"{value:.1f} %"

        def number(value, suffix="", decimals=1):
            return "—" if value is None else f"{float(value):.{decimals}f}{suffix}"

        def set_value(card: str, key: str, value) -> None:
            self.metric_values[card][key].setText(str(value))

        state = metrics.state
        perf = getattr(self, "_performance_auto", {})
        zones = getattr(self, "_thermal_zones", {})
        robot = getattr(self, "_robot_metrics", {})
        sensor = getattr(self, "_sensor_health", {})
        context = getattr(self, "_case_context", None)
        target = getattr(getattr(self, "_active_strategy", None), "target_percent", None)
        if context is None or not context.has_cpu_target:
            target = None
        self.metric_rows["performance"]["target"].setVisible(target is not None)
        set_value("performance", "target", pct(target))
        set_value("performance", "cpu", pct(metrics.current_cpu))
        set_value(
            "performance", "cpu_range",
            f"{pct(metrics.average_cpu)} / {pct(metrics.minimum_cpu)} / {pct(metrics.maximum_cpu)}",
        )
        set_value("performance", "ram", pct(perf.get("ram_usage", state.ram_usage_percent)))
        loads = state.loads or (None, None, None)
        set_value("performance", "load", " / ".join("—" if item is None else f"{item:.2f}" for item in loads))
        gpu = perf.get("gpu_usage", getattr(self, "_gpu_usage", None))
        set_value("performance", "gpu", pct(gpu))
        ai_runtime = perf.get("ai_runtime_detected")
        ai_nodes = perf.get("ai_nodes")
        ai_text = "—" if ai_runtime is None else "DETECTED" if ai_runtime >= 1 else "NOT DETECTED"
        set_value("performance", "ai", f"{ai_text} / {'—' if ai_nodes is None else int(ai_nodes)}")
        performance_state = self._cpu_target_status(metrics.current_cpu, target) if target is not None else "NORMAL" if metrics.current_cpu is not None else "NO DATA"
        self._apply_metric_card_state(self.performance_card, performance_state)

        def temperature(value):
            return number(value, " °C")

        thermal_values = {
            "cpu": zones.get("cpu_thermal", getattr(self, "_cpu_temp", None)),
            "gpu": zones.get("gpu_thermal", getattr(self, "_gpu_temp", None)),
            "soc0": zones.get("soc0_thermal"),
            "soc1": zones.get("soc1_thermal"),
            "soc2": zones.get("soc2_thermal"),
            "tj": zones.get("tj_thermal"),
        }
        for key, value in thermal_values.items():
            set_value("thermal", key, temperature(value))
        self._apply_metric_card_state(self.thermal_card, self._aggregate_thermal_status(thermal_values.values()))

        def robot_number(key, suffix="", decimals=1):
            return number(robot.get(key), suffix, decimals)

        set_value("battery", "soc_soh", f"{robot_number('battery_soc', ' %', 0)} / {robot_number('battery_soh', ' %', 0)}")
        set_value("battery", "voltage", robot_number("battery_pack_voltage", " V", 2))
        set_value("battery", "current", robot_number("battery_pack_current", " A", 2))
        set_value("battery", "bus_voltage", robot_number("motor_bus_voltage", " V", 2))
        set_value(
            "battery", "temp_range",
            f"{robot_number('battery_temp_min', ' °C')} / {robot_number('battery_temp_avg', ' °C')} / {robot_number('battery_temp_max', ' °C')}",
        )
        bus_error = robot.get("motor_bus_error_code")
        battery_state = "ERROR" if bus_error not in (None, 0) else self._thermal_status(robot.get("battery_temp_max"))
        if battery_state == "NO DATA" and robot.get("battery_soc") is not None:
            battery_state = "NORMAL"
        self._apply_metric_card_state(self.battery_card, battery_state)

        errors = robot.get("motor_errors")
        motor_state = "NO DATA" if errors is None else "OK" if errors == 0 else "ERROR"
        alive, total = robot.get("motor_alive"), robot.get("motor_total")
        set_value("motor", "status", motor_state)
        set_value("motor", "alive", "—" if alive is None else f"{int(alive)} / {int(total) if total is not None else '—'}")
        set_value("motor", "errors", "—" if errors is None else str(int(errors)))
        set_value(
            "motor", "temp_range",
            f"{robot_number('motor_temp_min', ' °C')} / {robot_number('motor_temp_avg', ' °C')} / {robot_number('motor_temp_max', ' °C')}",
        )
        motor_thermal = self._thermal_status(robot.get("motor_temp_max"))
        set_value("motor", "thermal", motor_thermal)
        set_value("motor", "error_code", "—" if bus_error is None else str(int(bus_error)))
        self._apply_metric_card_state(self.motor_card, "ERROR" if motor_state == "ERROR" else motor_thermal if motor_thermal != "NO DATA" else motor_state)

        ros_health = sensor.get("ros_health")
        ros_state = "NO DATA" if ros_health is None else "OK" if ros_health >= 1 else "ERROR"
        set_value("sensor", "health", ros_state)
        set_value("sensor", "camera", number(sensor.get("camera_fps"), " FPS"))
        set_value("sensor", "lidar", number(sensor.get("lidar_hz"), " Hz"))
        set_value("sensor", "imu", number(sensor.get("imu_hz"), " Hz"))
        set_value("sensor", "nodes", "—" if sensor.get("ros_nodes") is None else str(int(sensor["ros_nodes"])))
        self._apply_metric_card_state(self.sensor_card, ros_state)

        def compact_temperatures(prefix: str, label: str) -> tuple[str, str]:
            values = []
            for key, value in robot.items():
                match = re.fullmatch(re.escape(prefix) + r"(\d+)", key)
                if match:
                    values.append((int(match.group(1)), float(value)))
            values.sort()
            if not values:
                return "Waiting for data...", ""
            full = "  |  ".join(f"{label}{index}: {value:.1f} °C" for index, value in values)
            short = "  |  ".join(f"{label}{index}: {value:.1f}°" for index, value in values[:4])
            if len(values) > 4:
                short += f"  |  +{len(values) - 4} more"
            return short, full

        battery_short, battery_full = compact_temperatures("battery_temp_", "B")
        motor_short, motor_full = compact_temperatures("motor_temp_", "M")
        set_value("thermal_details", "battery", battery_short)
        set_value("thermal_details", "motor", motor_short)
        self.metric_values["thermal_details"]["battery"].setToolTip(battery_full)
        self.metric_values["thermal_details"]["motor"].setToolTip(motor_full)
        detail_values = [value for key, value in robot.items() if re.fullmatch(r"(?:battery|motor)_temp_\d+", key)]
        self._apply_metric_card_state(self.thermal_details_card, self._aggregate_thermal_status(detail_values))

    def set_queue(self, definitions) -> None:
        self._queue_definitions = list(definitions)
        self.queue_table.setRowCount(len(self._queue_definitions))
        self._update_queue_rows(0)

    def _update_queue_rows(self, elapsed: int) -> None:
        definitions = getattr(self, "_queue_definitions", [])
        self.queue_table.setRowCount(len(definitions))
        self.queue_table.clearContents()
        active = getattr(self, "_active_definition", None)
        manager = getattr(self, "_active_manager", None)
        start_time = "—"
        if manager is not None:
            started = next((record.started_at for record in manager.records if record.started_at), None)
            if started:
                try:
                    start_time = datetime.fromisoformat(started.replace("Z", "+00:00")).astimezone().strftime("%H:%M:%S")
                except ValueError:
                    start_time = started

        for row, definition in enumerate(definitions):
            duration = _format_seconds(definition.duration_seconds) if definition.duration_seconds is not None else definition.duration_text or "—"
            values = (str(row + 1), definition.test_id, None, duration, start_time if definition is active and definition.runtime_status == RuntimeStatus.RUNNING else "—")
            for column, value in enumerate(values):
                if column == 2:
                    self.queue_table.setCellWidget(row, column, _badge_cell(definition.runtime_status))
                    continue
                item = QTableWidgetItem(value)
                if column in {0, 3, 4}:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.queue_table.setItem(row, column, item)

            if definition.duration_seconds is None:
                progress_widget = QLabel("—")
                progress_widget.setObjectName("Muted")
                progress_widget.setAlignment(Qt.AlignmentFlag.AlignCenter)
            else:
                progress_widget = QProgressBar()
                progress_widget.setRange(0, 100)
                progress = 0
                if definition is active and definition.runtime_status == RuntimeStatus.RUNNING:
                    progress = min(100, int(100 * elapsed / max(1, definition.duration_seconds)))
                elif definition.runtime_status in {RuntimeStatus.PASS, RuntimeStatus.FAIL, RuntimeStatus.WARNING, RuntimeStatus.NEEDS_REVIEW}:
                    progress = 100
                progress_widget.setValue(progress)
                progress_widget.setFormat(f"{progress}%")
                progress_widget.setTextVisible(True)
            self.queue_table.setCellWidget(row, 5, progress_widget)

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
                cell = QTableWidgetItem(str(value or "—"))
                if column == 5:
                    cell.setData(Qt.ItemDataRole.UserRole, item["evidence_folder"])
                self.history_table.setItem(row_index, column, cell)
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
        root.setContentsMargins(18, 14, 18, 14)
        root.setSpacing(8)
        header = QHBoxLayout()
        header.setSpacing(14)
        title_block = QVBoxLayout()
        title_block.setSpacing(1)
        heading = QLabel("System Stress Test")
        heading.setObjectName("PageTitle")
        subtitle = QLabel("Stress / System")
        subtitle.setObjectName("Muted")
        title_block.addWidget(heading)
        title_block.addWidget(subtitle)
        header.addLayout(title_block)
        header.addStretch(1)

        self.environment_label = StatusBadge("NOT CHECKED")
        self.dut_status_label = StatusBadge("DISCONNECTED")
        self.root_label = QLabel()
        self.root_label.setMinimumWidth(190)
        self.root_label.setMaximumWidth(330)
        self.root_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.root_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.disk_label = QLabel("—")

        def add_summary_field(caption: str, value: QWidget) -> None:
            field = QWidget()
            field_layout = QVBoxLayout(field)
            field_layout.setContentsMargins(0, 0, 0, 0)
            field_layout.setSpacing(1)
            caption_label = QLabel(caption)
            caption_label.setObjectName("Muted")
            field_layout.addWidget(caption_label)
            field_layout.addWidget(value)
            header.addWidget(field, 0, Qt.AlignmentFlag.AlignVCenter)

        def add_separator() -> None:
            separator = QFrame()
            separator.setObjectName("HeaderSeparator")
            separator.setFrameShape(QFrame.Shape.VLine)
            separator.setFrameShadow(QFrame.Shadow.Plain)
            header.addWidget(separator)

        add_summary_field("Environment", self.environment_label)
        add_separator()
        add_summary_field("DUT", self.dut_status_label)
        add_separator()

        evidence_value = QWidget()
        evidence_layout = QHBoxLayout(evidence_value)
        evidence_layout.setContentsMargins(0, 0, 0, 0)
        evidence_layout.setSpacing(4)
        self.folder_button = QToolButton()
        self.folder_button.setObjectName("HeaderFolderButton")
        self.folder_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_DirOpenIcon))
        self.folder_button.setText("▾")
        self.folder_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.folder_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.folder_button.setToolTip("Evidence folder actions")
        self.folder_menu = QMenu(self.folder_button)
        self.change_folder_action = self.folder_menu.addAction("Change Folder")
        self.default_folder_action = self.folder_menu.addAction("Use Default Folder")
        self.folder_menu.addSeparator()
        self.open_folder_action = self.folder_menu.addAction("Open Folder")
        self.folder_button.setMenu(self.folder_menu)
        self.change_folder_action.triggered.connect(self.select_folder)
        self.default_folder_action.triggered.connect(self.use_default)
        self.open_folder_action.triggered.connect(self.open_folder)
        evidence_layout.addWidget(self.root_label, 1)
        evidence_layout.addWidget(self.folder_button)
        add_summary_field("Evidence Root", evidence_value)
        add_separator()
        add_summary_field("Disk Free", self.disk_label)
        root.addLayout(header)

        self.tabs = QTabWidget()
        self.tabs.setObjectName("StressTabs")
        self.tabs.tabBar().setExpanding(False)
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
        self.runner.test_prepared.connect(self._test_prepared)
        self.runner.prepared_discarded.connect(self._prepared_discarded)
        self.runner.test_started.connect(self._test_started)
        self.runner.test_updated.connect(self._test_updated)
        self.runner.test_finished.connect(self._test_finished)
        self.runner.queue_changed.connect(self.vd_page.set_queue)
        self.runner.error.connect(self._show_error)
        self.vd_page.start_requested.connect(self.runner.start_current)
        self.vd_page.stop_requested.connect(self.runner.stop)
        self.vd_page.finish_requested.connect(self.runner.finish_for_review)
        self.vd_page.review_requested.connect(self._review_result)
        self.vd_page.view_result_requested.connect(self._view_latest_result)
        self.vd_page.result_folder_requested.connect(self._view_result_folder)

    def refresh_header(self) -> None:
        status = self.pretest_page.environment_status.value.replace("_", " ")
        if self.pretest_page.override:
            status += " (OVERRIDE)"
        self.environment_label.set_status(status)
        self.vd_page.environment_label.set_status(status)
        self._update_dut_status()
        self._evidence_root_text = str(self.session_manager.evidence_root)
        self.root_label.setToolTip(self._evidence_root_text)
        self._elide_evidence_root()
        free = self.session_manager.disk_free()
        self.disk_label.setText(f"{free / (1024 ** 3):.1f} GiB" if free is not None else "Unavailable")
        root_locked = self.session_manager.session_dir is not None
        self.change_folder_action.setEnabled(not root_locked)
        self.default_folder_action.setEnabled(not root_locked)

    def _elide_evidence_root(self) -> None:
        path = getattr(self, "_evidence_root_text", "")
        width = max(80, self.root_label.width() - 4)
        self.root_label.setText(
            self.root_label.fontMetrics().elidedText(
                path, Qt.TextElideMode.ElideMiddle, width
            )
        )

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._elide_evidence_root()

    def _update_dut_status(self, *_args) -> None:
        if self.remote_service is None:
            status = "DISCONNECTED"
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
            self.vd_page.message_label.setText(
                "A test is currently running. Stop or finish it before selecting another test."
            )
            self.vd_page.view_stack.setCurrentIndex(0)
            return
        if self.runner.current is not None and self.runner.current.runtime_status == RuntimeStatus.PREPARED:
            prepared_ids = {
                item.test_id
                for item in (self.runner.current, *self.runner.queue)
            }
            replacement = [item for item in definitions if item.test_id not in prepared_ids]
            if replacement:
                definitions = replacement
                self.vd_page._selected_ids = {item.test_id for item in replacement}
        pretest_result = getattr(self.pretest_page, "result", None)
        pretest_baseline = (
            dict(pretest_result.baseline or {})
            if pretest_result is not None
            else {}
        )

        # stress-ng capability comes from PRE-TEST, not from a second
        # immediate-baseline measurement.
        stress_ng_available = None
        if pretest_result is not None:
            for check in getattr(pretest_result, "checks", []):
                check_id = str(getattr(check, "id", "")).casefold()
                check_name = str(getattr(check, "name", "")).casefold()

                if "stress-ng" not in check_id and "stress-ng" not in check_name:
                    continue

                status = getattr(check, "status", None)
                status_value = (
                    status.value
                    if hasattr(status, "value")
                    else str(status or "")
                ).upper()

                if status_value == "PASS":
                    stress_ng_available = True
                elif status_value in {
                    "FAIL",
                    "MISSING",
                    "NOT_CONFIGURED",
                }:
                    stress_ng_available = False

                break

        self.runner.enqueue(
            definitions,
            environment_status=self.pretest_page.environment_status,
            override=self.pretest_page.override,
            blocking_reasons=self.pretest_page.blocking_reasons,
            baseline=pretest_baseline,
            stress_ng_available=stress_ng_available,
        )
        self.refresh_header()

    def _environment_changed(self, result) -> None:
        self.refresh_header()

    def _test_prepared(self, definition, manager) -> None:
        self.vd_page.show_execution(
            definition,
            manager,
            None,
            [definition, *self.runner.queue],
            self.runner.current_strategy,
            self.runner.pretest_baseline,
            self.session_manager.session_dir,
        )
        self.vd_page.update_execution(
            definition,
            0,
            manager,
        )
        self.refresh_header()

    def _prepared_discarded(self, definition) -> None:
        if getattr(self.vd_page, "_active_definition", None) is definition:
            self.vd_page.clear_active_execution()
        self.vd_page.refresh_table()

    def _test_started(self, definition, manager) -> None:
        self.vd_page.show_execution(
            definition,
            manager,
            self.runner.current_paths,
            [definition, *self.runner.queue],
            self.runner.current_strategy,
            self.runner.pretest_baseline,
        )
        self._test_updated(definition)

    def _test_updated(self, definition) -> None:
        if definition and getattr(self.vd_page, "_active_definition", None) is definition:
            self.vd_page.update_execution(
                definition,
                self.runner.elapsed_seconds(),
                self.runner.evidence_manager,
            )
        self.vd_page.refresh_table()

    def _test_finished(self, definition, paths) -> None:
        self.vd_page.message_label.setText(f"{definition.test_id} finished: {definition.runtime_status.value}")
        loaded = load_result_snapshot(paths.attempt_dir)
        result = loaded.get("result") or {}
        elapsed = int(float(result.get("elapsed_sec", 0) or 0))
        manager = getattr(self.vd_page, "_active_manager", None)
        self.vd_page.finalize_execution_display(definition, paths, elapsed, manager)
        self.vd_page.refresh_table()

    def _view_latest_result(self, definition) -> None:
        attempt_dir = latest_result_attempt(self.session_manager.evidence_root, definition.test_id)
        if attempt_dir is None:
            self.vd_page.message_label.setText(f"No completed result exists for {definition.test_id}.")
            return
        self._show_result_folder(definition, attempt_dir)

    def _view_result_folder(self, folder: str) -> None:
        loaded = load_result_snapshot(folder)
        data = loaded.get("test_info") or loaded.get("result") or {}
        test_id = data.get("test_id")
        try:
            definition = self.catalog.get(test_id)
        except KeyError:
            self.vd_page.message_label.setText(f"The historical test {test_id or 'UNKNOWN'} is not in the current catalog.")
            return
        self._show_result_folder(definition, Path(folder), loaded)

    def _show_result_folder(self, definition, folder: Path, loaded: dict | None = None) -> None:
        payload = loaded or load_result_snapshot(folder)
        if not payload.get("result"):
            self.vd_page.message_label.setText(f"No execution result exists in {folder}.")
            return
        self.vd_page.show_result(definition, payload)

    def _review_result(self, status, comment: str) -> None:
        folder = getattr(self.vd_page, "_active_folder", None)
        definition = getattr(self.vd_page, "_active_definition", None)
        if folder is None or definition is None:
            self._show_error("No result is open for review.")
            return
        try:
            review_attempt(folder, status, comment)
        except (OSError, ValueError) as exc:
            self._show_error(str(exc))
            return
        definition.runtime_status = status
        try:
            catalog_definition = self.catalog.get(definition.test_id)
        except KeyError:
            catalog_definition = None
        if catalog_definition is not None:
            catalog_definition.runtime_status = status
        self.vd_page.show_result(definition, load_result_snapshot(folder))
        self.vd_page.message_label.setText(f"{definition.test_id} reviewed: {status.value}")
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
