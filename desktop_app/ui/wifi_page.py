"""Single-device Wi-Fi validation UI."""
from __future__ import annotations

import json
import mmap
import re
import shutil
from datetime import datetime
from pathlib import Path
import shiboken6

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPen, QFont, QTextDocument
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QComboBox, QFrame, QGridLayout, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QMessageBox, QPlainTextEdit,
    QProgressBar, QPushButton, QScrollArea, QSizePolicy, QSplitter, QStackedWidget, QTabWidget,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from desktop_app.wifi.catalog import ENVIRONMENTS, WifiTestCase, load_catalog, load_config
from desktop_app.wifi.runtime import EVIDENCE_ROOT, METRIC_FIELDS, WifiAttempt, WifiRuntime, metric_kind

DETAIL_METRICS = {
    "baseline": ("Interface", "Driver/PHY", "NetworkManager", "Device Type", "Kernel Device Errors", "Role", "Hotspot profile", "AP capability", "Supported Bands", "Mode", "SSID", "Security", "IPv4 method", "Autoconnect", "Band", "Channel"),
    "activation": ("AP state", "Runtime mode", "SSID", "Band", "Channel", "Frequency", "Client visibility", "Activation elapsed"),
    "client": ("Authentication state", "Assigned IPv4", "Gateway", "DHCP state", "Connection elapsed"),
    "security_exposure": ("Security configuration", "Secret exposure", "Service exposure", "Warnings", "Findings"),
    "ap_recovery": ("Recovery step", "AP state", "NetworkManager state", "SSID recovered", "Client recovered", "Recovery elapsed", "Manual intervention"),
    "boot": ("Cycle 1", "Cycle 2", "Cycle 3", "Reboot start", "SSID visible", "DHCP ready", "Wi-Fi ready time", "Average ready time", "Max ready time"),
    "system": ("Ethernet route", "Wi-Fi AP", "Production route", "NetworkManager", "CPU/RAM", "Errors", "Warnings", "Acceptance checklist"),
    "band": ("Band", "Channel", "Frequency", "SSID", "RSSI", "Client association", "DHCP state", "Station bitrate", "Regulatory state", "DFS state"),
    "band_throughput": ("2.4 GHz RSSI", "2.4 GHz Run 1", "2.4 GHz Run 2", "2.4 GHz Run 3", "2.4 GHz Average", "2.4 GHz Minimum", "2.4 GHz Retransmits", "5 GHz RSSI", "5 GHz Run 1", "5 GHz Run 2", "5 GHz Run 3", "5 GHz Average", "5 GHz Minimum", "5 GHz Retransmits", "Requirement"),
    "wifi6": ("Jetson HE", "Client HE", "Runtime mode", "HE-MCS", "HE-NSS", "Tx bitrate", "Rx bitrate"),
    "range": ("Distance", "Environment", "SSID visible", "RSSI", "Bitrate", "Connection state", "Disconnects"),
    "security_matrix": ("WPA2 valid", "WPA2 invalid", "WPA2 IP assigned", "WPA3 valid", "WPA3 invalid", "WPA3 IP assigned", "Key management", "Runtime security", "Production restored"),
    "throughput": ("Current upload", "Average upload", "Min upload", "Max upload",
                   "Current download", "Average download", "Min download", "Max download", "Retransmits", "RSSI"),
    "latency": ("Current RTT", "Min RTT", "Avg RTT", "Max RTT", "Jitter", "Packet loss", "RSSI"),
    "rf": ("SSID", "BSSID", "Band", "Channel", "Frequency", "Width", "RSSI", "Security"),
    "dhcp": ("Client IP", "Prefix", "Gateway", "DNS", "DHCP time", "Address state", "Reconnect count"),
    "authentication": ("Attempts", "Successful", "Rejected", "Auth time", "Assigned IP"),
    "recovery": ("Current cycle", "Completed cycles", "Total cycles", "Successful cycles", "Failed cycles",
                 "Current recovery", "Avg recovery", "Max recovery", "Manual intervention"),
    "endurance": ("Elapsed", "Target duration", "Target seconds", "Disconnects", "Reconnects", "Packet loss",
                  "Min RSSI", "Service errors", "Last update", "RSSI"),
    "interface": ("Interface", "State", "Address", "SSID", "RSSI"),
}

def metric_text(name: str, value) -> str:
    if value is None:
        return "—"
    if not isinstance(value, (int, float)):
        return str(value)
    if "upload" in name.lower() or "download" in name.lower() or "ghz" in name.lower() and any(
            part in name.lower() for part in ("run", "average", "minimum")) or name in {"Upload", "Download"}:
        unit = "Mbps"
    elif "RSSI" in name:
        unit = "dBm"
    elif name in {"RTT", "Average RTT", "Current RTT", "Min RTT", "Avg RTT", "Max RTT", "Jitter"}:
        unit = "ms"
    elif name == "Packet loss":
        unit = "%"
    elif name in {"Current recovery", "Avg recovery", "Max recovery"}:
        unit = "s"
    else:
        unit = ""
    return f"{value:g} {unit}".strip()


def redact_secrets(raw: str) -> str:
    return re.sub(r"(?im)^(\s*(?:802-11-wireless-security\.psk|psk|password|passphrase)\s*[:=]\s*)(\S.*)$",
                  r"\1[REDACTED]", raw)


def button(title: str) -> QPushButton:
    result = QPushButton(title)
    result.setObjectName("SmallButton")
    return result


def field(label: str, value: str) -> QWidget:
    widget = QWidget()
    row = QHBoxLayout(widget)
    row.setContentsMargins(0, 2, 0, 2)
    name = QLabel(label)
    name.setMinimumWidth(160)
    text = QLabel(value or "—")
    text.setWordWrap(True)
    text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    row.addWidget(name)
    row.addWidget(text, 1)
    return widget


def text_area(text: str) -> QPlainTextEdit:
    area = QPlainTextEdit()
    area.setPlainText(text)
    area.setReadOnly(True)
    return area


def procedure_steps(text: str) -> list[str]:
    return [re.sub(r"^\s*(?:\d+[.)]|[-•])\s*", "", line).strip()
            for line in text.splitlines() if line.strip()]


class WifiPreTestPage(QWidget):
    CHECKS = (
        "Dashboard Jetson Connection", "Wi-Fi interface", "NetworkManager", "Hotspot profile",
        "nmcli", "iw", "iperf3", "Wi-Fi capability", "Evidence path", "Disk free", "Backup Ethernet",
    )

    def __init__(self, runtime: WifiRuntime, parent=None):
        super().__init__(parent)
        self.runtime = runtime
        self.config = load_config()
        self.rows = {name: index for index, name in enumerate(self.CHECKS)}
        self.pending: set[str] = set()
        root = QVBoxLayout(self)
        top = QHBoxLayout()
        title = QLabel("PRE-TEST ENVIRONMENT")
        title.setObjectName("CardTitle")
        self.overall = QLabel("NOT READY")
        self.run_button = button("RUN ALL CHECKS")
        self.refresh_button = button("REFRESH")
        for widget in (title, self.overall, self.run_button, self.refresh_button):
            top.addWidget(widget)
        top.addStretch()
        root.addLayout(top)
        self.table = QTableWidget(len(self.CHECKS), 3)
        self.table.setHorizontalHeaderLabels(["Check", "Actual", "Status"])
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        for row, check in enumerate(self.CHECKS):
            for col, value in enumerate((check, "—", "NOT RUN")):
                self.table.setItem(row, col, QTableWidgetItem(value))
        root.addWidget(self.table, 1)
        self.detail = QLabel("Select a check for details.")
        self.detail.setWordWrap(True)
        root.addWidget(self.detail)
        self.table.currentCellChanged.connect(self._selected)
        self.run_button.clicked.connect(self.run_all)
        self.refresh_button.clicked.connect(self.refresh)
        service = runtime.services["Jetson"]
        if service:
            service.operation_succeeded.connect(self._operation_succeeded)
            service.operation_failed.connect(self._operation_failed)
        self.refresh()

    def _set(self, check: str, actual: str, status: str) -> None:
        row = self.rows[check]
        self.table.item(row, 1).setText(actual)
        self.table.item(row, 2).setText(status)
        self.overall.setText("READY" if all(self.table.item(i, 2).text() == "PASS"
                                            for i in range(self.table.rowCount())) else "NOT READY")

    def _selected(self, row: int, _col: int, _old_row: int, _old_col: int) -> None:
        if row >= 0:
            self.detail.setText(f"{self.table.item(row, 0).text()}: {self.table.item(row, 1).text()} "
                                f"({self.table.item(row, 2).text()})")

    def refresh(self) -> None:
        service = self.runtime.services["Jetson"]
        connected = bool(service and service.is_connected)
        self._set("Dashboard Jetson Connection", "Connected" if connected else "Connect from Dashboard",
                  "PASS" if connected else "NOT READY")
        for check in ("Wi-Fi interface", "NetworkManager", "Hotspot profile", "nmcli", "iw", "iperf3", "Wi-Fi capability"):
            if not connected:
                self._set(check, "Shared Jetson connection unavailable", "NOT RUN")
        root = self.runtime.evidence_root
        parent = root if root.exists() else root.parent
        writable = parent.is_dir() and __import__("os").access(parent, __import__("os").W_OK)
        self._set("Evidence path", str(root), "PASS" if writable else "NOT READY")
        disk = shutil.disk_usage(parent if parent.is_dir() else Path.cwd())
        self._set("Disk free", f"{disk.free / 1024**3:.1f} GiB", "PASS" if disk.free > 1024**3 else "NOT READY")
        self._set("Backup Ethernet", ", ".join(self.config.get(k, "NOT CONFIGURED") for k in
                  ("Ethernet interface 1", "Ethernet interface 2")), "NOT RUN" if connected else "NOT READY")

    def run_all(self) -> None:
        self.refresh()
        service = self.runtime.services["Jetson"]
        if not service or not service.is_connected:
            return
        interface = self.config.get("Wi-Fi interface", "NOT CONFIGURED")
        profile = self.config.get("Hotspot profile", "NOT CONFIGURED")
        ethernet = [self.config.get(k, "NOT CONFIGURED") for k in ("Ethernet interface 1", "Ethernet interface 2")]
        if any(value == "NOT CONFIGURED" or not re.fullmatch(r"[A-Za-z0-9_.-]+", value)
               for value in (interface, *ethernet)) or profile == "NOT CONFIGURED":
            self._set("Wi-Fi interface", "NOT CONFIGURED", "NOT READY")
            return
        import shlex
        commands = (
            f"nmcli -t -f DEVICE,TYPE,STATE device status",
            "systemctl is-active NetworkManager",
            f"nmcli -g connection.id connection show {shlex.quote(profile)}",
            "command -v nmcli", "command -v iw", "command -v iperf3", "iw list",
            *(f"ip -brief link show {shlex.quote(name)}" for name in ethernet),
        )
        async def inspect(ssh):
            return [await ssh.run(command, timeout=15) for command in commands]
        request_id = service.submit_operation("wifi-pretest", inspect)
        if request_id:
            self.pending.add(request_id)
            for check in self.CHECKS[1:8]:
                self._set(check, "Checking…", "RUNNING")

    def _operation_succeeded(self, request_id: str, results: object) -> None:
        if request_id not in self.pending:
            return
        self.pending.remove(request_id)
        interface = self.config["Wi-Fi interface"]
        profile = self.config["Hotspot profile"]
        device = results[0]
        line = next((line for line in device.stdout.splitlines() if line.startswith(interface + ":wifi:")), "")
        self._set("Wi-Fi interface", interface if line else device.stdout.strip() or "Missing", "PASS" if line else "NOT READY")
        self._set("NetworkManager", results[1].stdout.strip(), "PASS" if results[1].stdout.strip() == "active" else "NOT READY")
        self._set("Hotspot profile", profile if results[2].exit_status == 0 else "Missing", "PASS" if results[2].exit_status == 0 else "NOT READY")
        for check, result in zip(("nmcli", "iw", "iperf3"), results[3:6]):
            self._set(check, result.stdout.strip() or "Missing", "PASS" if result.exit_status == 0 else "NOT READY")
        supported = "* AP" in results[6].stdout
        self._set("Wi-Fi capability", "AP supported" if supported else "AP unavailable", "PASS" if supported else "NOT READY")
        links = results[7:9]
        available = any(result.exit_status == 0 and "UP" in result.stdout for result in links)
        self._set("Backup Ethernet", ", ".join(self.config[k] for k in ("Ethernet interface 1", "Ethernet interface 2")),
                  "PASS" if available else "NOT READY")

    def _operation_failed(self, request_id: str, error: str) -> None:
        if request_id in self.pending:
            self.pending.remove(request_id)
            for check in self.CHECKS[1:8]:
                self._set(check, error, "NOT READY")


class MetricCard(QFrame):
    def __init__(self, title: str):
        super().__init__()
        self.title = title
        self.setObjectName("WifiMetricCard")
        self.setFixedHeight(94)
        self.setMaximumWidth(220)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 7, 10, 7)
        layout.addWidget(QLabel(title))
        self.value = QLabel("—")
        self.value.setObjectName("WifiMetricValue")
        layout.addWidget(self.value)
        self.gauge = QProgressBar()
        self.gauge.setTextVisible(False)
        self.gauge.setMaximum(100)
        self.gauge.hide()
        layout.addWidget(self.gauge)

    def set_metric(self, value) -> None:
        self.value.setText(metric_text(self.title, value))
        show_gauge = self.title in {"RSSI", "Min RSSI"} and isinstance(value, (float, int))
        self.gauge.setVisible(show_gauge)
        if show_gauge:
            # Presentation scale only; pass/fail comes from the case definition and tester.
            self.gauge.setValue(max(0, min(100, int(2 * (value + 100)))))


class TrendChart(QWidget):
    def __init__(self, title: str):
        super().__init__()
        self.title = title
        self.points: list[tuple[int, float]] = []
        self.setFixedHeight(120)

    def set_points(self, points) -> None:
        self.points = list(points)[-120:]
        self.update()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor("#ffffff"))
        painter.setPen(QColor("#344054"))
        painter.drawText(10, 18, self.title)
        if len(self.points) < 2:
            painter.setPen(QColor("#98a2b3"))
            painter.drawText(10, 55, "Waiting for samples")
            return
        values = [value for _, value in self.points]
        low, high = min(values), max(values)
        span = max(0.001, high - low)
        painter.setPen(QPen(QColor("#155eef"), 2))
        width = max(1, self.width() - 28)
        height = max(1, self.height() - 38)
        for index in range(1, len(values)):
            x1 = 12 + width * (index - 1) / (len(values) - 1)
            x2 = 12 + width * index / (len(values) - 1)
            y1 = 28 + height * (1 - (values[index - 1] - low) / span)
            y2 = 28 + height * (1 - (values[index] - low) / span)
            painter.drawLine(int(x1), int(y1), int(x2), int(y2))


class WifiMonitor(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        self.cards_layout = QHBoxLayout()
        root.addLayout(self.cards_layout)
        self.cards: dict[str, MetricCard] = {}
        self.rssi_bars = {}
        for name in ("RSSI", "Min RSSI", "2.4 GHz RSSI", "5 GHz RSSI"):
            bar_row = QWidget()
            bar_layout = QHBoxLayout(bar_row)
            bar_layout.setContentsMargins(0, 0, 0, 0)
            label = QLabel(name)
            value_label = QLabel("—")
            bar = QProgressBar()
            bar.setMaximum(100)
            bar.setTextVisible(False)
            bar_layout.addWidget(label)
            bar_layout.addWidget(value_label)
            bar_layout.addWidget(bar, 1)
            root.addWidget(bar_row)
            self.rssi_bars[name] = (bar_row, value_label, bar)
            bar_row.hide()
        self.group_title = QLabel()
        self.group_title.setObjectName("CardTitle")
        root.addWidget(self.group_title)
        self.group_layout = QGridLayout()
        root.addLayout(self.group_layout)
        self.group_fields: dict[str, QLabel] = {}
        self.group_cells: dict[str, QWidget] = {}
        self.matrix = QTableWidget()
        self.matrix.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.matrix.verticalHeader().hide()
        self.matrix.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.matrix.hide()
        root.addWidget(self.matrix)
        self.current_kind = ""
        self.progress = QProgressBar()
        self.progress.hide()
        root.addWidget(self.progress)
        self.cycles = QLabel()
        self.cycles.setWordWrap(True)
        self.cycles.hide()
        root.addWidget(self.cycles)
        self.charts_layout = QHBoxLayout()
        root.addLayout(self.charts_layout)
        self.charts: dict[str, TrendChart] = {}
        root.setAlignment(Qt.AlignmentFlag.AlignTop)

    def render(self, attempt) -> None:
        kind = metric_kind(attempt.case)
        fields = ("Interface", "Driver/PHY", "NetworkManager", "Kernel Device Errors") if attempt.case.test_id == "TC-WIFI-C01" else METRIC_FIELDS[kind]
        if tuple(self.cards) != fields:
            for card in self.cards.values():
                self.cards_layout.removeWidget(card)
                card.deleteLater()
            self.cards = {name: MetricCard(name) for name in fields}
            for card in self.cards.values():
                self.cards_layout.addWidget(card)
        for name, card in self.cards.items():
            if name == "Elapsed":
                seconds = attempt.elapsed_seconds
                card.set_metric(f"{seconds // 3600:02d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}")
            else:
                card.set_metric(attempt.metrics.get(name))
        for name, (row, label, bar) in self.rssi_bars.items():
            value = attempt.metrics.get(name)
            row.setVisible(isinstance(value, (int, float)))
            if isinstance(value, (int, float)):
                label.setText(metric_text(name, value))
                bar.setValue(max(0, min(100, int(2 * (value + 100)))))
        self.group_title.setText("LIVE / CAPTURED METRICS")
        self.group_title.setVisible(attempt.case.test_id != "TC-WIFI-C01")
        names = () if attempt.case.test_id == "TC-WIFI-C01" else DETAIL_METRICS[kind]
        kind_key = attempt.case.test_id if attempt.case.test_id == "TC-WIFI-C01" else kind
        if kind_key != self.current_kind:
            while self.group_layout.count():
                item = self.group_layout.takeAt(0)
                if item.widget():
                    item.widget().deleteLater()
            self.group_fields = {}
            self.group_cells = {}
            for index, name in enumerate(names):
                value = QLabel("—")
                value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
                cell = QWidget()
                cell.setFixedHeight(28)
                cell_layout = QHBoxLayout(cell)
                cell_layout.setContentsMargins(3, 1, 3, 1)
                cell_layout.addWidget(QLabel(name))
                cell_layout.addWidget(value, 1)
                self.group_layout.addWidget(cell, index // 2, index % 2)
                self.group_fields[name] = value
                self.group_cells[name] = cell
            for chart in self.charts.values():
                self.charts_layout.removeWidget(chart)
                chart.deleteLater()
            trend_names = {"throughput": ("Upload", "Download"),
                           "latency": ("RTT",), "rf": ("RSSI",),
                           "endurance": ("RSSI", "RTT"),
                           "band_throughput": ("2.4 GHz Average", "5 GHz Average")}.get(kind, ())
            self.charts = {name: TrendChart(f"{name} over time") for name in trend_names}
            for chart in self.charts.values():
                self.charts_layout.addWidget(chart)
            self.current_kind = kind_key
        for name, label in self.group_fields.items():
            if name == "Elapsed":
                seconds = attempt.elapsed_seconds
                label.setText(f"{seconds // 3600:02d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}")
            else:
                alias = {"Current upload": "Upload", "Current download": "Download",
                         "Current RTT": "RTT", "Avg RTT": "Average RTT",
                         "Service errors": "Errors", "Failed cycles": "Failures"}.get(name)
                value = attempt.metrics.get(name, attempt.metrics.get(alias) if alias else None)
                label.setText(metric_text(name, value))
            self.group_cells[name].setVisible(label.text() != "—")
        self.group_title.setVisible(bool(self.group_fields) and any(not cell.isHidden() for cell in self.group_cells.values()))
        matrix_specs = {
            "boot": ("Cycle", "Reboot start", "SSID visible", "DHCP ready", "Wi-Fi ready time", "Result"),
            "band_throughput": ("Band", "RSSI", "Run 1", "Run 2", "Run 3", "Average", "Minimum", "Retransmits"),
            "security_matrix": ("Mode", "Valid credential", "Invalid credential", "IP assigned"),
            "recovery": ("Cycle", "State", "Reconnect time", "IP restored", "Result"),
        }
        columns = matrix_specs.get(kind)
        self.matrix.setVisible(bool(columns))
        if columns:
            rows = ("WPA2", "WPA3") if kind == "security_matrix" else (
                "2.4 GHz", "5 GHz") if kind == "band_throughput" else (
                "Cycle 1", "Cycle 2", "Cycle 3") if kind == "boot" else tuple(str(i) for i in range(1, int(attempt.metrics.get("Total cycles", 0)) + 1))
            self.matrix.setColumnCount(len(columns))
            self.matrix.setHorizontalHeaderLabels(columns)
            self.matrix.setRowCount(len(rows))
            self.matrix.setFixedHeight(min(250, 34 + 31 * len(rows)))
            for row, prefix in enumerate(rows):
                for col, name in enumerate(columns):
                    key = f"{prefix} {name}"
                    value = prefix if col == 0 else metric_text(key, attempt.metrics.get(key))
                    self.matrix.setItem(row, col, QTableWidgetItem(value))
        self.progress.setVisible(kind in {"recovery", "ap_recovery", "boot", "endurance"})
        if kind == "recovery":
            total = attempt.metrics.get("Total cycles", 0)
            done = attempt.metrics.get("Completed cycles", 0)
            self.progress.setValue(int(100 * done / total) if total else 0)
        elif kind == "endurance":
            target = attempt.metrics.get("Target seconds", 0)
            self.progress.setValue(min(100, int(100 * attempt.elapsed_seconds / target)) if target else 0)
        cycles = attempt.metrics.get("Cycles", [])
        self.cycles.setVisible(kind == "recovery" and bool(cycles))
        if kind == "recovery" and cycles:
            self.cycles.setText("    ".join(cycles))
        for name, chart in self.charts.items():
            chart.set_points(attempt.trends.get(name, ()))


class WifiEnvironmentPage(QWidget):
    """One environment owns one persistent list widget and its view state."""

    def __init__(self, environment: str, runtime: WifiRuntime, parent=None):
        super().__init__(parent)
        self.environment = environment
        self.runtime = runtime
        self.cases = load_catalog(environment)
        self.by_id = {case.test_id: case for case in self.cases}
        self.selected: set[str] = set()
        self.current_case: WifiTestCase | None = None
        self._view_attempt: WifiAttempt | None = None
        self._updating_table = False
        self._list_scroll = 0
        self._table_statuses: dict[str, str] = {}
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 8, 12, 10)
        self.stack = QStackedWidget()
        self.stack.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Ignored)
        root.addWidget(self.stack)
        self.list_view = self._build_list()
        self.detail_view = self._build_detail()
        self.execution_view = self._build_execution()
        self.history_view = self._build_history()
        for widget in (self.list_view, self.detail_view, self.execution_view, self.history_view):
            self.stack.addWidget(widget)
        self.runtime.changed.connect(self._runtime_changed)
        self.runtime.message.connect(self._message)
        self._populate_table()

    def _build_list(self) -> QWidget:
        view = QWidget()
        root = QVBoxLayout(view)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(7)
        self.mini = QFrame()
        self.mini.setObjectName("WifiMiniMonitor")
        mini_row = QHBoxLayout(self.mini)
        self.mini_text = QLabel()
        self.view_running = button("VIEW RUNNING TEST")
        self.view_running.clicked.connect(self.show_execution)
        mini_row.addWidget(self.mini_text, 1)
        mini_row.addWidget(self.view_running)
        self.mini.hide()
        root.addWidget(self.mini)
        filters = QHBoxLayout()
        self.filters: dict[str, QComboBox] = {}
        for name, values in (
            ("Group", ("All", *sorted({case.category for case in self.cases}))),
            ("Status", ("All", "NOT RUN", "RUNNING", "PASS", "FAIL", "NEEDS REVIEW")),
            ("Mode", ("All", "AUTO", "GUIDED", "MANUAL")),
        ):
            combo = QComboBox()
            combo.addItems(values)
            combo.currentTextChanged.connect(self._populate_table)
            self.filters[name] = combo
            filters.addWidget(QLabel(name))
            filters.addWidget(combo)
        reset = button("RESET")
        reset.clicked.connect(self.reset_filters)
        filters.addWidget(reset)
        filters.addStretch()
        root.addLayout(filters)
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["Select", "Test ID", "Group", "Test Name", "Mode", "Status"])
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().hide()
        header = self.table.horizontalHeader()
        for col, width in ((0, 46), (1, 128), (2, 165), (4, 85), (5, 105)):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.Fixed)
            self.table.setColumnWidth(col, width)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.table.itemChanged.connect(self._item_changed)
        self.table.cellClicked.connect(self._cell_clicked)
        self.table.cellDoubleClicked.connect(self._cell_double_clicked)
        root.addWidget(self.table, 1)
        self.empty = QLabel(f"No {self.environment} Wi-Fi test catalog loaded.")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.setVisible(not self.cases)
        root.addWidget(self.empty)
        bottom = QHBoxLayout()
        self.counts = QLabel()
        self.run_selected = button("RUN SELECTED")
        self.run_selected.clicked.connect(self._run_selected)
        history = button("HISTORY")
        history.clicked.connect(self.show_history)
        bottom.addWidget(self.counts)
        bottom.addStretch()
        bottom.addWidget(self.run_selected)
        bottom.addWidget(history)
        root.addLayout(bottom)
        return view

    def _visible_cases(self) -> list[WifiTestCase]:
        def matches(case):
            values = {
                "Group": case.category,
                "Mode": case.mode, "Status": self.runtime.latest_status(self.environment, case.test_id),
            }
            return all(combo.currentText() == "All" or values[name] == combo.currentText()
                       for name, combo in self.filters.items())
        return [case for case in self.cases if matches(case)]

    def _populate_table(self, *_args) -> None:
        if not hasattr(self, "table"):
            return
        scroll = self.table.verticalScrollBar().value() if self.list_view.isVisible() else self._list_scroll
        self._updating_table = True
        visible = self._visible_cases()
        self.table.setRowCount(len(visible))
        for row, case in enumerate(visible):
            values = ("", case.test_id, case.category, case.name, case.mode,
                      self.runtime.latest_status(self.environment, case.test_id))
            for col, value in enumerate(values):
                item = QTableWidgetItem(value)
                if col == 0:
                    item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable)
                    item.setCheckState(Qt.CheckState.Checked if case.test_id in self.selected else Qt.CheckState.Unchecked)
                self.table.setItem(row, col, item)
        self._updating_table = False
        self._table_statuses = {case.test_id: self.runtime.latest_status(self.environment, case.test_id)
                                for case in self.cases}
        self.table.verticalScrollBar().setValue(scroll)
        if self.list_view.isVisible():
            self._list_scroll = self.table.verticalScrollBar().value()
        self.counts.setText(f"Selected: {len(self.selected)}     Visible: {len(visible)}     Total: {len(self.cases)}")
        self._update_mini()

    def _remember_list_scroll(self) -> None:
        if self.stack.currentWidget() is self.list_view:
            self._list_scroll = self.table.verticalScrollBar().value()

    def hideEvent(self, event) -> None:
        self._remember_list_scroll()
        super().hideEvent(event)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        QTimer.singleShot(0, self._restore_list_scroll)

    def _restore_list_scroll(self) -> None:
        if shiboken6.isValid(self.table):
            self.table.verticalScrollBar().setValue(self._list_scroll)

    def _item_changed(self, item: QTableWidgetItem) -> None:
        if self._updating_table or item.column() != 0:
            return
        test_id = self.table.item(item.row(), 1).text()
        if item.checkState() == Qt.CheckState.Checked:
            self.selected.add(test_id)
        else:
            self.selected.discard(test_id)
        self.counts.setText(f"Selected: {len(self.selected)}     Visible: {self.table.rowCount()}     Total: {len(self.cases)}")

    def _cell_clicked(self, row: int, col: int) -> None:
        if col in (1, 3):
            self.show_detail(self.table.item(row, 1).text())

    def _cell_double_clicked(self, row: int, col: int) -> None:
        if col != 0:
            self.show_detail(self.table.item(row, 1).text())

    def reset_filters(self) -> None:
        for combo in self.filters.values():
            combo.setCurrentIndex(0)

    def _build_detail(self) -> QWidget:
        view = QWidget()
        root = QVBoxLayout(view)
        root.setContentsMargins(0, 0, 0, 0)
        back = button("← BACK TO TCs LIST")
        back.clicked.connect(self.back_to_list)
        root.addWidget(back, alignment=Qt.AlignmentFlag.AlignLeft)
        self.detail_id = QLabel()
        self.detail_id.setObjectName("PageTitle")
        self.detail_name = QLabel()
        self.detail_name.setWordWrap(True)
        self.detail_meta = QLabel()
        root.addWidget(self.detail_id)
        root.addWidget(self.detail_name)
        root.addWidget(self.detail_meta)
        self.detail_tabs = QTabWidget()
        self.detail_pages = {}
        for title in ("OVERVIEW", "PROCEDURE", "EXPECTED", "EVIDENCE"):
            area = QScrollArea()
            area.setWidgetResizable(True)
            content = QWidget()
            content_layout = QVBoxLayout(content)
            content_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
            area.setWidget(content)
            self.detail_pages[title] = content_layout
            self.detail_tabs.addTab(area, title)
        root.addWidget(self.detail_tabs, 1)
        self.run_this = button("RUN THIS TEST")
        self.run_this.clicked.connect(lambda: self._start_case(self.current_case))
        root.addWidget(self.run_this, alignment=Qt.AlignmentFlag.AlignRight)
        return view

    def _clear_layout(self, layout) -> None:
        while layout.count():
            item = layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def show_detail(self, test_id: str) -> None:
        self._remember_list_scroll()
        case = self.by_id[test_id]
        self.current_case = case
        self.detail_id.setText(case.test_id)
        self.detail_name.setText(case.name)
        self.detail_meta.setText(f"{case.category.upper()}     {case.mode}     {case.phase}     "
                                 f"{self.runtime.latest_status(self.environment, test_id)}")
        for layout in self.detail_pages.values():
            self._clear_layout(layout)
        overview = self.detail_pages["OVERVIEW"]
        for name, value in (("Test ID", case.test_id), ("Test Name", case.name),
                            ("Purpose", case.purpose), ("Group", case.category),
                            ("Requirement", case.requirement), ("Mode", case.mode),
                            ("Previous runs", str(len(self.runtime.attempts(self.environment, test_id))))):
            overview.addWidget(field(name, value))
        overview.addWidget(QLabel("Preconditions"))
        preconditions = procedure_steps(case.preconditions)
        for item in preconditions:
            label = QLabel(f"□  {item}")
            label.setWordWrap(True)
            overview.addWidget(label)
        if not preconditions:
            overview.addWidget(QLabel("None supplied"))
        procedure = self.detail_pages["PROCEDURE"]
        steps = procedure_steps(case.procedure)
        for index, step in enumerate(steps, 1):
            label = QLabel(f"{index}.  {step}")
            label.setWordWrap(True)
            procedure.addWidget(label)
            commands = [part for part in re.findall(r"`([^`]+)`", step)
                        if re.match(r"^(?:nmcli|iw|ip|journalctl|iperf3|systemctl|ping)\b", part)]
            for command in commands:
                block = QPlainTextEdit()
                block.setObjectName("WifiCommand")
                block.setPlainText(command)
                block.setReadOnly(True)
                block.setMaximumHeight(56)
                procedure.addWidget(block)
                copy = button("COPY COMMAND")
                copy.clicked.connect(lambda _checked=False, value=command:
                                     QApplication.clipboard().setText(value))
                procedure.addWidget(copy, alignment=Qt.AlignmentFlag.AlignLeft)
        if not steps:
            procedure.addWidget(QLabel("No procedure supplied."))
        expected = self.detail_pages["EXPECTED"]
        expected.addWidget(text_area(case.expected or "No expected result supplied"))
        requirements = load_config()
        for key in ("Wi-Fi Standard Requirement", "Supported Bands Requirement", "Throughput @10m",
                    "Factory Range Requirement", "Local RTT Requirement", "Security Requirement"):
            if key.lower().split()[0] in case.name.lower() or (case.test_id == "TC-WIFI-C23" and key == "Throughput @10m"):
                expected.addWidget(field(key, requirements[key]))
        evidence = self.detail_pages["EVIDENCE"]
        evidence.addWidget(field("AUTO evidence", "Read-only shared Jetson capture" if case.mode == "AUTO" else "None"))
        evidence.addWidget(field("GUIDED evidence", "Tester observations and imported command output"))
        evidence.addWidget(field("Manual observations", "Record during execution"))
        evidence.addWidget(field("Raw files", f"wifi/{self.environment}/<session>/{case.test_id}/attempt_###/raw/"))
        previous = self.runtime.attempts(self.environment, test_id)
        evidence.addWidget(field("Previous attempts", str(len(previous))))
        if previous:
            latest = previous[-1].parent
            evidence.addWidget(field("Latest evidence", str(latest)))
            open_previous = button("OPEN LATEST ATTEMPT")
            open_previous.clicked.connect(lambda _checked=False, path=previous[-1]: self._open_attempt(path))
            evidence.addWidget(open_previous)
        self.detail_tabs.setCurrentIndex(0)
        self.stack.setCurrentWidget(self.detail_view)

    def _build_execution(self) -> QWidget:
        view = QWidget()
        root = QVBoxLayout(view)
        root.setContentsMargins(0, 0, 0, 0)
        back = button("← BACK TO TCs LIST")
        back.clicked.connect(self.back_to_list)
        root.addWidget(back, alignment=Qt.AlignmentFlag.AlignLeft)
        self.execution_title = QLabel()
        self.execution_title.setObjectName("CardTitle")
        self.execution_status = QLabel()
        self.auto_result_label = QLabel()
        self.auto_result_label.setObjectName("CardTitle")
        self.result_reason_label = QLabel()
        self.result_reason_label.setWordWrap(True)
        self.execution_elapsed = QLabel()
        self.execution_expected = QLabel()
        self.execution_expected.setWordWrap(True)
        root.addWidget(self.execution_title)
        root.addWidget(self.execution_status)
        root.addWidget(self.auto_result_label)
        root.addWidget(self.result_reason_label)
        root.addWidget(self.execution_elapsed)
        self.execution_expected.hide()
        self.splitter = QSplitter(Qt.Orientation.Vertical)
        self.layers = QTabWidget()
        top_panel = QWidget()
        top_layout = QVBoxLayout(top_panel)
        top_layout.setContentsMargins(0, 0, 0, 0)
        evaluation_title = QLabel("TEST EVALUATION")
        evaluation_title.setObjectName("CardTitle")
        top_layout.addWidget(evaluation_title)
        self.criteria_table = QTableWidget(0, 4)
        self.criteria_table.setHorizontalHeaderLabels(("Criterion", "Expected", "Actual", "Result"))
        self.criteria_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.criteria_table.verticalHeader().hide()
        self.criteria_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.criteria_table.setMaximumHeight(215)
        self.criteria_table.setMinimumHeight(185)
        top_layout.addWidget(self.criteria_table)
        self.issue_summary = QLabel()
        self.issue_summary.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        top_layout.addWidget(self.issue_summary)
        self.issue_details = QLabel()
        self.issue_details.setWordWrap(True)
        top_layout.addWidget(self.issue_details)
        self.monitor = WifiMonitor()
        monitor_scroll = QScrollArea()
        monitor_scroll.setWidgetResizable(True)
        monitor_scroll.setWidget(self.monitor)
        monitor_scroll.setMinimumHeight(105)
        top_layout.addWidget(monitor_scroll, 1)
        self.splitter.addWidget(top_panel)
        self.summary_view = QTableWidget(0, 2)
        self.summary_view.setHorizontalHeaderLabels(("Summary", "Value"))
        self.summary_view.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.summary_view.verticalHeader().hide()
        self.summary_view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.raw_view = text_area("")
        self.raw_view.setMinimumHeight(0)
        self.raw_view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.raw_view.setFont(QFont("monospace"))
        self._raw_displayed = None
        self._log_window_start = 0
        self._log_paged = False
        raw_tab = QWidget()
        raw_layout = QVBoxLayout(raw_tab)
        raw_layout.setContentsMargins(0, 0, 0, 0)
        log_controls = QHBoxLayout()
        self.wrap_log = button("WRAP")
        self.wrap_log.setCheckable(True)
        self.wrap_log.toggled.connect(lambda checked: self.raw_view.setLineWrapMode(
            QPlainTextEdit.LineWrapMode.WidgetWidth if checked else QPlainTextEdit.LineWrapMode.NoWrap))
        self.auto_scroll = button("AUTO SCROLL")
        self.auto_scroll.setCheckable(True)
        self.auto_scroll.setChecked(True)
        self.find_log = QLineEdit()
        self.find_log.setPlaceholderText("Find in log")
        self.find_log.returnPressed.connect(lambda: self._find_log(False))
        earlier_log = button("EARLIER")
        earlier_log.clicked.connect(lambda: self._page_log(-1))
        later_log = button("LATER")
        later_log.clicked.connect(lambda: self._page_log(1))
        next_log = button("NEXT")
        next_log.clicked.connect(lambda: self._find_log(False))
        previous_log = button("PREVIOUS")
        previous_log.clicked.connect(lambda: self._find_log(True))
        self.maximize_log = button("MAXIMIZE LOG")
        self.maximize_log.clicked.connect(self._toggle_log_maximize)
        for control in (self.wrap_log, self.auto_scroll, self.find_log, next_log, previous_log,
                        earlier_log, later_log, self.maximize_log):
            log_controls.addWidget(control)
        raw_layout.addLayout(log_controls)
        self.log_position = QLabel()
        raw_layout.addWidget(self.log_position)
        raw_layout.addWidget(self.raw_view, 1)
        import_row = QHBoxLayout()
        self.output_direction = QComboBox()
        self.output_direction.addItems(("Upload", "Download"))
        self.output_direction.hide()
        self.output_band = QComboBox()
        self.output_band.addItems(("2.4 GHz", "5 GHz"))
        self.output_band.hide()
        self.raw_input = QPlainTextEdit()
        self.raw_input.setPlaceholderText("Paste original command output for this guided attempt")
        self.raw_input.setFixedHeight(42)
        self.import_output = button("IMPORT OUTPUT")
        self.import_output.clicked.connect(self._import_output)
        import_row.addWidget(self.output_direction)
        import_row.addWidget(self.output_band)
        import_row.addWidget(self.import_output)
        raw_layout.addWidget(self.raw_input)
        raw_layout.addLayout(import_row)
        self.import_status = QLabel()
        raw_layout.addWidget(self.import_status)
        self.evidence_view = QTableWidget(0, 3)
        self.evidence_view.setHorizontalHeaderLabels(("Evidence", "Status", "Source"))
        self.evidence_view.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.evidence_view.verticalHeader().hide()
        self.evidence_view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.evidence_view.cellClicked.connect(self._show_evidence)
        self.layers.addTab(self.summary_view, "SUMMARY")
        self.layers.addTab(raw_tab, "RAW LOG")
        self.layers.addTab(self.evidence_view, "EVIDENCE")
        self.layers.currentChanged.connect(lambda _index: self._render_execution())
        self.splitter.addWidget(self.layers)
        self.splitter.setStretchFactor(0, 3)
        self.splitter.setStretchFactor(1, 2)
        root.addWidget(self.splitter, 1)
        measurement = QHBoxLayout()
        measurement.addWidget(QLabel("Record measured value:"))
        self.metric_name = QComboBox()
        self.metric_value = QLineEdit()
        self.metric_value.setPlaceholderText("Observed value (number or text)")
        self.record_metric = button("RECORD")
        self.record_metric.clicked.connect(self._record_metric)
        measurement.addWidget(self.metric_name)
        measurement.addWidget(self.metric_value, 1)
        measurement.addWidget(self.record_metric)
        root.addLayout(measurement)
        review_row = QHBoxLayout()
        review_row.addWidget(QLabel("Comment:"))
        self.review_comment = QLineEdit()
        review_row.addWidget(self.review_comment, 1)
        self.review_buttons = []
        for status in ("PASS", "FAIL", "NEEDS REVIEW"):
            control = button(status)
            control.clicked.connect(lambda _checked=False, result=status: self._review(result))
            review_row.addWidget(control)
            self.review_buttons.append(control)
        root.addLayout(review_row)
        self.override_button = button("OVERRIDE RESULT")
        self.override_button.clicked.connect(self._enable_override)
        review_row.addWidget(self.override_button)
        self._override_enabled = False
        self._log_maximized = False
        self.clock = QTimer(self)
        self.clock.setInterval(1000)
        self.clock.timeout.connect(self._render_execution)
        self.clock.start()
        return view

    def _render_execution(self) -> None:
        attempt = self._view_attempt or self.runtime.active
        if not attempt or attempt.environment != self.environment:
            return
        if self.stack.currentWidget() != self.execution_view:
            self._update_mini()
            return
        self.execution_title.setText(f"{attempt.case.test_id} — {attempt.case.name}")
        self.execution_status.setText(f"Execution: ● {attempt.status}")
        self.auto_result_label.setText(f"Auto Result: {attempt.auto_result}     Final Result: {attempt.final_result}")
        self.result_reason_label.setText("Evaluation pending; criteria update as evidence arrives." if attempt.status == "RUNNING"
                                         else f"Reason: {attempt.result_reason}")
        self.execution_expected.setText(f"Requirement: {attempt.case.expected}")
        seconds = attempt.elapsed_seconds
        target = attempt.metrics.get("Target duration")
        self.execution_elapsed.setText(f"Elapsed: {seconds // 3600:02d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"
                                       f"{f' / {target}' if target else ''}     "
                                       f"Attempt: {attempt.number:03d}     Target: {attempt.case.target}")
        kind = metric_kind(attempt.case)
        names = DETAIL_METRICS[kind]
        if tuple(self.metric_name.itemText(i) for i in range(self.metric_name.count())) != names:
            self.metric_name.clear()
            self.metric_name.addItems(names)
        self.record_metric.setEnabled(attempt.status == "RUNNING" and self._view_attempt is None)
        self.import_output.setEnabled(attempt.status == "RUNNING" and self._view_attempt is None)
        self.output_direction.setVisible(kind in {"throughput", "band_throughput"})
        self.output_band.setVisible(kind == "band_throughput")
        for control in self.review_buttons:
            control.setVisible(attempt.case.mode != "AUTO" or self._override_enabled)
            control.setEnabled(self._view_attempt is None and ((attempt.status == "RUNNING" and not self.runtime.capture_pending and attempt.case.mode != "AUTO")
                               or (attempt.status in {"COMPLETED", "ERROR"} and self._override_enabled)))
        self.override_button.setVisible(self._view_attempt is None and attempt.case.mode == "AUTO" and attempt.status in {"COMPLETED", "ERROR"})
        self.override_button.setEnabled(not self._override_enabled)
        self.criteria_table.setRowCount(len(attempt.criteria))
        for row, criterion in enumerate(attempt.criteria):
            for col, value in enumerate((f"{criterion['criterion_id']} {criterion['name']}",
                                         criterion['expected'], metric_text(criterion['name'], criterion['actual']),
                                         "MANUAL" if criterion['status'] == "MANUAL_REQUIRED" else
                                         "UNKNOWN" if criterion['status'] == "NOT_COLLECTED" else criterion['status'])):
                item = QTableWidgetItem(str(value))
                item.setToolTip(criterion['reason'] + "\nEvidence: " + ", ".join(criterion['evidence_reference']))
                self.criteria_table.setItem(row, col, item)
        errors = sum(row["status"] == "FAIL" for row in attempt.criteria)
        unknown = sum(row["required"] and row["status"] in {"UNKNOWN", "NOT_COLLECTED"} for row in attempt.criteria)
        warnings = sum(item["status"] == "UNAVAILABLE" for item in attempt.evidence.values())
        self.issue_summary.setText(f"Errors: {errors}     Warnings: {warnings}     Unknown criteria: {unknown}")
        self.issue_details.setText("  •  ".join(
            [row["reason"] for row in attempt.criteria if row["status"] == "FAIL"] +
            [f"{name}: {item['command']} unavailable" for name, item in attempt.evidence.items() if item["status"] == "UNAVAILABLE"]))
        self.monitor.render(attempt)
        if self.layers.currentIndex() == 0:
            counts = {state: sum(row["required"] and row["status"] == state for row in attempt.criteria)
                      for state in ("PASS", "FAIL", "UNKNOWN", "MANUAL_REQUIRED", "NOT_COLLECTED")}
            rows = [("AUTO RESULT", attempt.auto_result), ("Reason", attempt.result_reason),
                    ("Passed", counts["PASS"]), ("Failed", counts["FAIL"]),
                    ("Unknown", counts["UNKNOWN"] + counts["NOT_COLLECTED"]),
                    ("Manual", counts["MANUAL_REQUIRED"]),
                    ("Optional / info unknown", sum(not row["required"] and row["status"] in {"UNKNOWN", "NOT_COLLECTED"}
                                                     for row in attempt.criteria))]
            rows.extend((name, metric_text(name, attempt.metrics[name])) for name in
                        ("Interface", "Driver/PHY", "NetworkManager", "Device Type", "Kernel Device Errors")
                        if name in attempt.metrics)
            self.summary_view.setRowCount(len(rows))
            for row, pair in enumerate(rows):
                for col, value in enumerate(pair):
                    self.summary_view.setItem(row, col, QTableWidgetItem(str(value)))
        elif self.layers.currentIndex() == 1:
            if attempt.raw_log != self._raw_displayed and (not self._log_paged or self.auto_scroll.isChecked()):
                displayed = redact_secrets(attempt.raw_log)
                if self._raw_displayed is not None and attempt.raw_log.startswith(self._raw_displayed) and not self._log_paged:
                    prior_scroll = self.raw_view.verticalScrollBar().value()
                    prior_cursor = self.raw_view.textCursor()
                    self.raw_view.moveCursor(self.raw_view.textCursor().MoveOperation.End)
                    self.raw_view.insertPlainText(redact_secrets(attempt.raw_log[len(self._raw_displayed):]))
                    if not self.auto_scroll.isChecked():
                        self.raw_view.setTextCursor(prior_cursor)
                        self.raw_view.verticalScrollBar().setValue(prior_scroll)
                else:
                    self.raw_view.setPlainText(displayed)
                if self.auto_scroll.isChecked():
                    self.raw_view.verticalScrollBar().setValue(self.raw_view.verticalScrollBar().maximum())
                self._raw_displayed = attempt.raw_log
                path = attempt.directory / "raw" / "commands.log"
                size = path.stat().st_size if path.is_file() else 0
                self._log_window_start = max(0, size - len(attempt.raw_log.encode("utf-8")))
                self._log_paged = self._log_window_start > 0
                self.log_position.setText(f"Showing captured bytes {self._log_window_start:,}–{size:,} of {size:,}"
                                          if self._log_paged else "Full captured log")
        else:
            self.evidence_view.setRowCount(len(attempt.evidence))
            for row, (name, item) in enumerate(attempt.evidence.items()):
                for col, value in enumerate((name, item["status"], item["command"])):
                    self.evidence_view.setItem(row, col, QTableWidgetItem(str(value)))
            self._evidence_names = list(attempt.evidence)
        self._update_mini()

    def _find_log(self, backward: bool) -> None:
        query = self.find_log.text()
        if not query:
            return
        flags = QTextDocument.FindFlag.FindBackward if backward else QTextDocument.FindFlag(0)
        if self.raw_view.find(query, flags):
            return
        cursor = self.raw_view.textCursor()
        cursor.movePosition(cursor.MoveOperation.End if backward else cursor.MoveOperation.Start)
        self.raw_view.setTextCursor(cursor)
        if self.raw_view.find(query, flags):
            return
        attempt = self._view_attempt or self.runtime.active
        path = attempt.directory / "raw" / "commands.log" if attempt else None
        if not path or not path.is_file() or path.stat().st_size == 0:
            return
        with path.open("rb") as source, mmap.mmap(source.fileno(), 0, access=mmap.ACCESS_READ) as captured:
            prefix = self.raw_view.toPlainText()[:self.raw_view.textCursor().position()]
            current = self._log_window_start + len(prefix.encode("utf-8"))
            needle = query.encode("utf-8")
            location = captured.rfind(needle, 0, max(0, current - 1)) if backward else captured.find(needle, min(current + 1, len(captured)))
            if location < 0:
                location = captured.rfind(needle) if backward else captured.find(needle)
        if location >= 0:
            self._load_log_window(max(0, location - 1024))
            if backward:
                cursor = self.raw_view.textCursor()
                cursor.movePosition(cursor.MoveOperation.End)
                self.raw_view.setTextCursor(cursor)
            self.raw_view.find(query, flags if backward else QTextDocument.FindFlag(0))

    def _load_log_window(self, start: int) -> None:
        attempt = self._view_attempt or self.runtime.active
        if not attempt:
            return
        path = attempt.directory / "raw" / "commands.log"
        if not path.is_file():
            return
        size = path.stat().st_size
        start = min(max(0, start), max(0, size - 1))
        with path.open("rb") as source:
            source.seek(start)
            chunk = source.read(262144)
        self.raw_view.setPlainText(redact_secrets(chunk.decode("utf-8", errors="replace")))
        self._log_window_start = start
        self._log_paged = True
        self.auto_scroll.setChecked(False)
        self.log_position.setText(f"Showing captured bytes {start:,}–{start + len(chunk):,} of {size:,}")

    def _page_log(self, direction: int) -> None:
        attempt = self._view_attempt or self.runtime.active
        if not attempt:
            return
        path = attempt.directory / "raw" / "commands.log"
        if not path.is_file():
            return
        size = path.stat().st_size
        start = max(0, self._log_window_start + direction * 262144)
        if start < size:
            self._load_log_window(start)

    def _toggle_log_maximize(self) -> None:
        self._log_maximized = not self._log_maximized
        self.splitter.widget(0).setVisible(not self._log_maximized)
        self.layers.setCurrentIndex(1)
        self.layers.tabBar().setVisible(not self._log_maximized)
        self.maximize_log.setText("RESTORE" if self._log_maximized else "MAXIMIZE LOG")

    def _show_evidence(self, row: int, _col: int) -> None:
        attempt = self._view_attempt or self.runtime.active
        if not attempt or row >= len(self._evidence_names):
            return
        path = attempt.directory / "raw" / self._evidence_names[row]
        if path.is_file():
            self.layers.setCurrentIndex(1)
            self.find_log.setText(attempt.evidence[self._evidence_names[row]]["command"])
            self._find_log(False)

    def _record_metric(self) -> None:
        value = self.metric_value.text().strip()
        if not value:
            return
        numeric = re.fullmatch(r"(-?\d+(?:\.\d+)?)\s*(?:Mbps|dBm|ms|%|s)?", value, re.IGNORECASE)
        measurement = float(numeric.group(1)) if numeric else value
        name = self.metric_name.currentText()
        values = {name: measurement}
        alias = {"Current upload": "Upload", "Current download": "Download",
                 "Current RTT": "RTT", "Avg RTT": "Average RTT",
                 "Failed cycles": "Failures", "Service errors": "Errors"}.get(name)
        if alias:
            values[alias] = measurement
        self.runtime.add_metrics(values)
        self.metric_value.clear()

    def _import_output(self) -> None:
        raw = self.raw_input.toPlainText()
        if not raw.strip():
            return
        try:
            metrics = self.runtime.ingest_output(raw, self.output_direction.currentText(), self.output_band.currentText())
        except ValueError as error:
            self.import_status.setText(str(error))
            return
        self.import_status.setText(f"Original output saved; {len(metrics)} metric(s) parsed.")
        self.raw_input.clear()

    def _open_attempt(self, path: Path) -> None:
        if self._log_maximized:
            self._toggle_log_maximize()
        data = json.loads(path.read_text(encoding="utf-8"))
        info = json.loads((path.parent / "test_info.json").read_text(encoding="utf-8"))
        case = self.by_id[info["test_id"]]
        snapshot = WifiAttempt(self.environment, case, info["attempt"], path.parent,
                               started=datetime.fromisoformat(data.get("started", info["started"])))
        summary_path = path.parent / "summary.json"
        summary = json.loads(summary_path.read_text(encoding="utf-8")) if summary_path.is_file() else {}
        snapshot.status = data.get("execution_state", "COMPLETED")
        snapshot.auto_result = data.get("auto_result", "NOT EVALUATED")
        snapshot.final_result = data.get("final_result", data.get("status", "NEEDS REVIEW"))
        snapshot.result_reason = data.get("result_reason", "Historical attempt")
        snapshot.criteria = data.get("criteria", summary.get("criteria", []))
        snapshot.evidence = data.get("evidence", summary.get("evidence", {}))
        snapshot.metrics = data.get("actual_result", summary.get("metrics", {}))
        raw_path = path.parent / "raw" / "commands.log"
        if raw_path.is_file():
            with raw_path.open("rb") as source:
                source.seek(max(0, raw_path.stat().st_size - 262144))
                snapshot.raw_log = source.read().decode("utf-8", errors="replace")
        self._view_attempt = snapshot
        self._raw_displayed = None
        self._log_paged = False
        self.auto_scroll.setChecked(True)
        self.raw_view.clear()
        self.stack.setCurrentWidget(self.execution_view)
        self._render_execution()

    def show_execution(self) -> None:
        attempt = self.runtime.active
        if attempt and attempt.environment == self.environment:
            self._view_attempt = None
            self._remember_list_scroll()
            self.stack.setCurrentWidget(self.execution_view)
            self._render_execution()

    def _start_case(self, case: WifiTestCase | None) -> None:
        if case is None:
            return
        attempt = self.runtime.start(self.environment, case)
        if attempt:
            if self._log_maximized:
                self._toggle_log_maximize()
            self._override_enabled = False
            self._view_attempt = None
            self._raw_displayed = None
            self._log_paged = False
            self.auto_scroll.setChecked(True)
            self.raw_view.clear()
            self.layers.setCurrentIndex(0)
            self.review_comment.clear()
            self.show_execution()

    def _run_selected(self) -> None:
        if self.runtime.active and self.runtime.active.status == "RUNNING":
            self._message("A Wi-Fi test is already running.")
            return
        if len(self.selected) != 1:
            self._message("Select one Wi-Fi test to run at a time.")
            return
        self._start_case(self.by_id[next(iter(self.selected))])

    def _review(self, status: str) -> None:
        attempt = self.runtime.active
        if attempt and attempt.environment == self.environment and attempt.status in {"RUNNING", "COMPLETED", "ERROR"}:
            try:
                self.runtime.finish(status, self.review_comment.text())
            except ValueError as error:
                self._message(str(error))
                return
            self._render_execution()

    def _enable_override(self) -> None:
        self._override_enabled = True
        self.review_comment.setPlaceholderText("Required reason for override")
        self._render_execution()

    def _build_history(self) -> QWidget:
        view = QWidget()
        root = QVBoxLayout(view)
        root.setContentsMargins(0, 0, 0, 0)
        back = button("← BACK TO TCs LIST")
        back.clicked.connect(self.back_to_list)
        root.addWidget(back, alignment=Qt.AlignmentFlag.AlignLeft)
        title = QLabel(f"{self.environment} Wi-Fi HISTORY")
        title.setObjectName("CardTitle")
        root.addWidget(title)
        self.history_table = QTableWidget(0, 4)
        self.history_table.setHorizontalHeaderLabels(["Test ID", "Attempt", "Date", "Result"])
        self.history_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.history_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.history_table.cellClicked.connect(self._history_selected)
        root.addWidget(self.history_table, 1)
        self.history_details = text_area("")
        root.addWidget(self.history_details, 1)
        self._history_paths: list[Path] = []
        return view

    def show_history(self) -> None:
        self._remember_list_scroll()
        self._history_paths = sorted(
            (p for case in self.cases for p in self.runtime.attempts(self.environment, case.test_id)),
            key=lambda path: path.stat().st_mtime, reverse=True,
        )
        self.history_table.setRowCount(len(self._history_paths))
        for row, path in enumerate(self._history_paths):
            try:
                result = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                result = {}
            status = result.get("status", "NEEDS REVIEW") if path.name == "result.json" else "NEEDS REVIEW"
            for col, value in enumerate((path.parent.parent.name, path.parent.name,
                                         result.get("started", "—"), status)):
                self.history_table.setItem(row, col, QTableWidgetItem(value))
        self.history_details.clear()
        self.stack.setCurrentWidget(self.history_view)

    def _history_selected(self, row: int, _col: int) -> None:
        path = self._history_paths[row]
        result = json.loads(path.read_text(encoding="utf-8"))
        summary_path = path.parent / "summary.json"
        summary = json.loads(summary_path.read_text(encoding="utf-8")) if summary_path.is_file() else {}
        files = [str(p.relative_to(path.parent)) for p in path.parent.rglob("*") if p.is_file()]
        raw_path = path.parent / "raw" / "commands.log"
        raw_text = ""
        if raw_path.is_file():
            with raw_path.open("rb") as source:
                source.seek(0, 2)
                size = source.tell()
                source.seek(max(0, size - 131072))
                raw_text = source.read().decode("utf-8", errors="replace")
                if size > 131072:
                    raw_text = f"Showing last 128 KiB of {raw_path}\n" + raw_text
        self.history_details.setPlainText(
            f"SUMMARY\n{json.dumps(summary, ensure_ascii=False, indent=2)}\n\n"
            f"ACTUAL RESULT\n{json.dumps(result, ensure_ascii=False, indent=2)}\n\n"
            f"EVIDENCE / RAW LOGS\n" + "\n".join(files) + f"\n\nRAW LOG\n{redact_secrets(raw_text)}"
        )

    def back_to_list(self) -> None:
        saved_scroll = self._list_scroll
        self._view_attempt = None
        self.stack.setCurrentWidget(self.list_view)
        self._update_mini()
        self._list_scroll = saved_scroll
        QTimer.singleShot(0, self._restore_list_scroll)

    def _update_mini(self) -> None:
        attempt = self.runtime.active
        active_here = bool(attempt and attempt.environment == self.environment and attempt.status == "RUNNING")
        self.mini.setVisible(active_here)
        self.run_selected.setEnabled(not (self.runtime.active and self.runtime.active.status == "RUNNING"))
        if active_here:
            metrics = [f"{name}: {metric_text(name, attempt.metrics[name])}" for name in METRIC_FIELDS[metric_kind(attempt.case)]
                       if name in attempt.metrics][:4]
            self.mini_text.setText(f"● {attempt.case.test_id} RUNNING   Elapsed {attempt.elapsed_seconds // 60:02d}:"
                                   f"{attempt.elapsed_seconds % 60:02d}   " + "    ".join(metrics))

    def _runtime_changed(self) -> None:
        statuses = {case.test_id: self.runtime.latest_status(self.environment, case.test_id)
                    for case in self.cases}
        if statuses != self._table_statuses:
            self._populate_table()
        else:
            self._update_mini()
        if self.stack.currentWidget() == self.execution_view:
            self._render_execution()

    def _message(self, message: str) -> None:
        if self.isVisible():
            QMessageBox.information(self, "Wi-Fi", message)


class WifiPage(QWidget):
    def __init__(self, jetson_service, evidence_root=None, parent=None):
        super().__init__(parent)
        self.runtime = WifiRuntime(jetson_service, evidence_root=evidence_root or EVIDENCE_ROOT, parent=self)
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 12, 16, 12)
        self.tabs = QTabWidget()
        self.pretest = WifiPreTestPage(self.runtime)
        self.tabs.addTab(self.pretest, "PRE-TEST")
        self.environments = {}
        for environment in ENVIRONMENTS:
            page = WifiEnvironmentPage(environment, self.runtime)
            self.environments[environment] = page
            self.tabs.addTab(page, environment)
        root.addWidget(self.tabs)
