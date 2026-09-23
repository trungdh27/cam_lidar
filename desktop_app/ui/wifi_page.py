"""Single-device Wi-Fi validation UI."""
from __future__ import annotations

import json
import mmap
import re
import shutil
from datetime import datetime
from pathlib import Path
import shiboken6

from PySide6.QtCore import Qt, QTimer, Signal, QUrl, QSize, QSignalBlocker
from PySide6.QtGui import QColor, QPainter, QPen, QFont, QTextDocument, QTextCharFormat, QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QFrame, QGridLayout, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QMessageBox, QPlainTextEdit,
    QProgressBar, QPushButton, QScrollArea, QSizePolicy, QSplitter, QStackedWidget, QTabWidget,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from desktop_app.wifi.catalog import (ENVIRONMENTS, WifiTestCase, load_auto_catalog,
                                      load_catalog, load_config)
from desktop_app.wifi.runtime import EVIDENCE_ROOT, METRIC_FIELDS, WifiAttempt, WifiRuntime, metric_kind
from desktop_app.wifi.auto_suite import PROJECT_OPTIONAL, readiness_status, recovery_phases
from desktop_app.wifi.auto_suite import criteria_for_auto
from desktop_app.wifi.presentation import presentation_for, resolved_metrics


def navigate_dashboard(widget):
    parent = widget
    while parent:
        if hasattr(parent, "navigate_to") and hasattr(parent, "nav_buttons"):
            parent.navigate_to(0)
            return
        parent = parent.parentWidget()
    page = widget.window()
    if hasattr(page, "dashboard_requested"):
        page.dashboard_requested.emit()


class SetupRequiredView(QWidget):
    """One actionable setup surface; dependency internals remain optional."""
    def __init__(self, runtime, action, parent=None):
        super().__init__(parent)
        self.runtime, self.action, self.case = runtime, action, None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        self.title = QLabel()
        self.title.setObjectName("CardTitle")
        layout.addWidget(self.title)
        self.note = QLabel("This test has not started.")
        layout.addWidget(self.note)
        self.items = QWidget()
        self.items_layout = QVBoxLayout(self.items)
        self.items_layout.setContentsMargins(0, 0, 0, 0)
        self.items_layout.setSpacing(5)
        layout.addWidget(self.items)
        self.technical_toggle = QCheckBox("Show technical dependencies")
        layout.addWidget(self.technical_toggle)
        self.dependencies = DependencyView()
        self.dependencies.show_blocker_summary = False
        self.dependencies.hide()
        self.technical_toggle.toggled.connect(self.dependencies.setVisible)
        layout.addWidget(self.dependencies)

    def render(self, case):
        from desktop_app.wifi.setup_guidance import blocker_guidance
        self.case = case
        report = self.runtime.dependency_report(case)
        blockers = [dep for dep in report if dep.required and not dep.satisfied]
        ssh_blocker = next((dep for dep in blockers if dep.key == "shared_ssh_ready"), None)
        if ssh_blocker:
            blockers = [ssh_blocker]  # Remote discovery resumes once control is available.
        elif any(dep.key == "full_wifi_qual_path" for dep in blockers):
            blockers = [dep for dep in blockers if dep.key not in {"network_path_ready", "client_association_ready"}]
        human_keys = {"distance_1m_confirmed", "distance_10m_confirmed", "allow_disruptive"}
        human_blockers = [dep for dep in blockers if dep.key in human_keys]
        state = "SETUP REQUIRED" if human_blockers else "PREFLIGHT ERROR" if blockers else "READY"
        self.title.setText(f"{state} — {case.test_id}" if blockers else "READY — No setup action required")
        emphasize(self.title, "BLOCKED" if blockers else "READY")
        self.note.setText(("This test has not started. Confirm only the physical/authorization step below."
                           if human_blockers else
                           "This test has not started. Automatic discovery could not satisfy a machine-detectable prerequisite. No manual value entry is available."
                           if blockers else "READY — No setup action required"))
        self.note.setVisible(True)
        while self.items_layout.count():
            widget = self.items_layout.takeAt(0).widget()
            widget.hide()
            widget.deleteLater()
        # Human-only setup is shown first. Machine-detectable failures retain a
        # specific diagnostic/recheck action but never turn into text fields.
        for dep in human_blockers + [dep for dep in blockers if dep not in human_blockers]:
            guidance = blocker_guidance(dep, case)
            item = QFrame()
            item.setObjectName("SetupRequiredItem")
            body = QVBoxLayout(item)
            body.setContentsMargins(6, 3, 6, 3)
            body.setSpacing(2)
            title = QLabel(f"{guidance['title']} — {guidance['current']}")
            title.setWordWrap(True)
            emphasize(title, "BLOCKED")
            body.addWidget(title)
            for text in ("Why: " + guidance["why"], "How to fix: " + guidance["fix"]):
                label = QLabel(text)
                label.setWordWrap(True)
                body.addWidget(label)
            actions = QHBoxLayout()
            for token, label in guidance["actions"]:
                control = button(label)
                control.clicked.connect(lambda _checked=False, name=token, key=dep.key: self.action(name, key, self.case))
                actions.addWidget(control)
            actions.addStretch()
            body.addLayout(actions)
            self.items_layout.addWidget(item)
            item.show()
        self.dependencies.render(report)
        self.dependencies.blockers.hide()  # Guidance is the sole blocker explanation.


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
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if not isinstance(value, (int, float)):
        return str(value)
    if "upload" in name.lower() or "download" in name.lower() or "ghz" in name.lower() and any(
            part in name.lower() for part in ("run", "average", "minimum")) or name in {"Upload", "Download"} or name.endswith("Mbps"):
        unit = "Mbps"
    elif "RSSI" in name:
        unit = "dBm"
    elif name in {"RTT", "Average RTT", "Current RTT", "Min RTT", "Avg RTT", "Max RTT", "Jitter", "RTT avg", "RTT min", "RTT max", "RTT mdev", "UDP jitter", "UDP 10 jitter"}:
        unit = "ms"
    elif name in {"Frequency", "Current frequency"}:
        unit = "MHz"
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


ERROR_COLOR = "#B42318"
WARNING_COLOR = "#B54708"


def emphasize(widget, status: str) -> None:
    tone = "error" if status in {"FAIL", "BLOCKED", "BLOCKING", "ERROR", "MEASUREMENT ERROR"} else "warning" if status in {"NEEDS REVIEW", "WARNING", "RECONFIG REQUIRED"} else "normal"
    widget.setProperty("tone", tone)
    widget.setStyleSheet(f"color: {ERROR_COLOR if tone == 'error' else WARNING_COLOR};" if tone != "normal" else "")


class WifiTable(QTableWidget):
    """Prefer the height of populated rows, while allowing small windows to shrink."""
    def sizeHint(self):
        hint = super().sizeHint()
        height = self.property("contentHeight")
        if height is not None:
            hint.setHeight(min(height, self.maximumHeight()))
        return hint

    def minimumSizeHint(self):
        return QSize(0, 32)


def compact_table(table, stretch=0, maximum=300) -> None:
    table.verticalHeader().hide()
    table.verticalHeader().setMinimumSectionSize(24)
    table.verticalHeader().setDefaultSectionSize(26)
    header = table.horizontalHeader()
    header.setMinimumSectionSize(28)
    header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
    header.setSectionResizeMode(stretch, QHeaderView.ResizeMode.Stretch)
    table.setMinimumHeight(0)
    table.setMaximumHeight(maximum)
    table.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)


def fit_table(table, maximum=300) -> None:
    height = table.horizontalHeader().height() + 2 * table.frameWidth()
    height += sum(table.rowHeight(row) for row in range(table.rowCount()) if not table.isRowHidden(row))
    # A horizontal scrollbar must not obscure the final row.
    if table.horizontalScrollBar().isVisible():
        height += table.horizontalScrollBar().sizeHint().height()
    height = min(maximum, max(32, height))
    table.setProperty("contentHeight", height)
    table.setMinimumHeight(0)
    table.setMaximumHeight(height)
    table.updateGeometry()


class DependencyView(QWidget):
    """The same relevant-first dependency presentation in list, detail and result."""
    def __init__(self, parent=None):
        super().__init__(parent)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(4)
        self.blockers = QLabel()
        self.blockers.setWordWrap(True)
        emphasize(self.blockers, "BLOCKED")
        root.addWidget(self.blockers)
        self.show_all = QCheckBox("Show all dependencies")
        self.show_all.toggled.connect(self._populate)
        root.addWidget(self.show_all)
        self.table = WifiTable(0, 4)
        self.table.setObjectName("AutoDependencyTable")
        self.table.setHorizontalHeaderLabels(("Dependency", "Expected / Requirement", "Actual", "State"))
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        compact_table(self.table, 2, 260)
        root.addWidget(self.table)
        self.dependencies = []

    def render(self, dependencies):
        self.dependencies = list(dependencies)
        self._populate()

    def _populate(self, *_args):
        blockers = [dep.reason for dep in self.dependencies if dep.required and not dep.satisfied]
        self.blockers.setText(f"BLOCKERS ({len(blockers)})\n• " + "\n• ".join(blockers))
        self.blockers.setVisible(bool(blockers) and getattr(self, "show_blocker_summary", True))
        visible = [dep for dep in self.dependencies if self.show_all.isChecked() or dep.required]
        self.table.setRowCount(len(visible))
        for row, dep in enumerate(visible):
            state = "RECONFIG REQUIRED" if dep.key == "current_band" and dep.required and not dep.satisfied else dep.state
            for col, value in enumerate((dep.label, dep.expected_requirement, dep.current_state, state)):
                item = QTableWidgetItem(value)
                item.setToolTip(f"{dep.reason if dep.required and not dep.satisfied else dep.action}\nSource: {dep.source}")
                if col == 3 and state in {"BLOCKING", "RECONFIG REQUIRED"}:
                    item.setForeground(QColor(ERROR_COLOR if state == "BLOCKING" else WARNING_COLOR))
                self.table.setItem(row, col, item)
        fit_table(self.table, 260)

def button(title: str, role: str = "secondary") -> QPushButton:
    result = QPushButton(title)
    result.setObjectName({"primary": "PrimaryButton", "pass": "SuccessButton",
                          "fail": "DangerButton", "review": "WarningButton"}.get(role, "SmallButton"))
    result.setMinimumHeight(34 if role != "secondary" else 30)
    result.setProperty("actionRole", role)
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
        "Dashboard Jetson Connection", "Wi-Fi interface", "Laptop Wi-Fi interface",
        "NetworkManager", "nmcli", "iw", "iperf3 local", "iperf3 Jetson",
        "Current SSID", "Current mode", "Current band", "Current channel",
        "Current frequency", "Channel width", "Jetson AP IP", "Current route", "Gateway",
        "Hotspot profile", "Saved Wi-Fi profile", "Wi-Fi capability", "Backup Ethernet",
        "Management path",
        "Evidence path", "Disk free",
    )
    OPTIONAL_CHECKS = {"Backup Ethernet", "iperf3 local", "iperf3 Jetson", "Hotspot profile", "Saved Wi-Fi profile"}

    def __init__(self, runtime: WifiRuntime, parent=None):
        super().__init__(parent)
        self.runtime, self.config = runtime, load_config()
        self.context_case = None
        self.context_cases = load_auto_catalog()
        self.rows = {name: index for index, name in enumerate(self.CHECKS)}
        self.pending = set()
        root = QVBoxLayout(self)
        root.setSpacing(5)
        top = QHBoxLayout()
        title = QLabel("PRE-TEST ENVIRONMENT")
        title.setObjectName("CardTitle")
        self.overall, self.summary = QLabel(), QLabel()
        self.run_button, self.refresh_button = button("RUN ALL CHECKS"), button("REFRESH")
        for widget in (title, self.overall, self.run_button, self.refresh_button):
            top.addWidget(widget)
        top.addStretch()
        root.addLayout(top)
        root.addWidget(self.summary)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setMinimumHeight(0)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget()
        body = QVBoxLayout(content)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(5)
        body.setAlignment(Qt.AlignmentFlag.AlignTop)
        readiness_title = QLabel("ENVIRONMENT READINESS")
        readiness_title.setObjectName("CardTitle")
        body.addWidget(readiness_title)
        self.table = WifiTable(len(self.CHECKS), 3)
        self.table.setObjectName("WifiEnvironmentReadiness")
        self.table.setHorizontalHeaderLabels(("Check", "Detected Value", "State"))
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        compact_table(self.table, 1, 530)
        labels = {"Dashboard Jetson Connection": "Shared Jetson control", "Wi-Fi interface": "Jetson Wi-Fi interface", "Jetson AP IP": "Jetson Wi-Fi IP", "Hotspot profile": "DUT NM profile"}
        for row, check in enumerate(self.CHECKS):
            for col, value in enumerate((labels.get(check, check), "Not detected", "NOT READY")):
                self.table.setItem(row, col, QTableWidgetItem(value))
        body.addWidget(self.table)
        self.discovery_table = self.table  # Compatibility alias; one authoritative view.
        self.detail = QLabel()
        self.detail.hide()
        body.addWidget(self.detail)
        self.unresolved_title = QLabel("UNRESOLVED SETUP")
        self.unresolved_title.setObjectName("CardTitle")
        body.addWidget(self.unresolved_title)
        self.unresolved_setup = QWidget()
        user_grid = QVBoxLayout(self.unresolved_setup)
        user_grid.setContentsMargins(0, 0, 0, 0)
        user_grid.setSpacing(3)
        body.addWidget(self.unresolved_setup)
        self.advanced_toggle = QCheckBox("Advanced / Optional thresholds")
        body.addWidget(self.advanced_toggle)
        self.advanced_setup = QWidget()
        advanced_grid = QVBoxLayout(self.advanced_setup)
        advanced_grid.setContentsMargins(0, 0, 0, 0)
        self.advanced_setup.hide()
        body.addWidget(self.advanced_setup)
        self.advanced_toggle.toggled.connect(self.advanced_setup.setVisible)
        self.advanced_toggle.toggled.connect(lambda _checked: self._sync_detected_fields())
        self.auto_fields, self.field_rows = {}, {}
        self.detected_auto_fields = {"local_iperf3_available", "jetson_iperf3_available", "backup_ethernet_ready"}
        self.external_keys = {"wifi_credential", "external_ssid_24g", "external_ssid_5g", "external_credential", "distance_1m_confirmed", "distance_10m_confirmed", "allow_disruptive", "security_requirement"}
        definitions = (
            ("wifi_credential", "Valid Wi-Fi credential"), ("external_ssid_24g", "External 2.4 GHz STA SSID"),
            ("external_ssid_5g", "External 5 GHz STA SSID"), ("external_credential", "External STA credential"),
            ("distance_1m_confirmed", "Position confirmed >=1 m"), ("distance_10m_confirmed", "Position confirmed 10 m"),
            ("allow_disruptive", "Allow disruptive Wi-Fi tests"), ("security_requirement", "Security requirement"),
            ("packet_loss_threshold_percent", "Max packet loss (%)"), ("udp_loss_threshold_percent", "Max UDP loss (%)"),
            ("udp_jitter_threshold_ms", "Max UDP jitter (ms)"), ("udp_min_receiver_mbps", "Min UDP receiver (Mbps)"),
            ("channel_24g", "Project 2.4 GHz channel (optional)"), ("frequency_24g_mhz", "Project 2.4 GHz frequency (optional MHz)"),
            ("rf_project_lock_24g", "Lock 2.4 GHz RF values for compliance"),
            ("channel_5g", "Project 5 GHz channel (optional)"), ("frequency_5g_mhz", "Project 5 GHz frequency (optional MHz)"),
            ("rf_project_lock_5g", "Lock 5 GHz RF values for compliance"),
            ("test_ssid_24g", "Expected 2.4 GHz test SSID"), ("test_ssid_5g", "Expected 5 GHz test SSID"),
            ("recovery_cycles", "Recovery cycles (>=5)"), ("recovery_off_seconds", "Recovery OFF interval (s)"),
            ("recorded_distance_1m", "Recorded distance (>=1 m)"), ("recorded_distance_10m", "Recorded distance (10 m)"),
            ("jetson_wifi_interface", "Jetson Wi-Fi interface"), ("client_wifi_interface", "Laptop Wi-Fi interface"),
            ("jetson_ap_ip", "Jetson Wi-Fi IP"), ("ap_profile", "DUT NM profile"),
        )
        confirmations = {"distance_1m_confirmed", "distance_10m_confirmed", "allow_disruptive",
                         "rf_project_lock_24g", "rf_project_lock_5g"}
        discovered = {"jetson_wifi_interface", "client_wifi_interface", "jetson_ap_ip", "ap_profile"}
        for key, label in definitions:
            row = QWidget()
            layout = QHBoxLayout(row)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.addWidget(QLabel(label))
            value = getattr(runtime.auto_setup, key)
            if key in confirmations:
                control = QComboBox()
                control.addItems(("NOT READY", "READY"))
                control.setCurrentIndex(int(bool(value)))
            else:
                control = QLineEdit()
                if key in {"wifi_credential", "external_credential"}:
                    control.setEchoMode(QLineEdit.EchoMode.Password)
                    control.setPlaceholderText("Configured (hidden)" if value else "Enter credential")
                else:
                    control.setText("" if value is None else str(value))
            layout.addWidget(control, 1)
            self.auto_fields[key], self.field_rows[key] = control, row
            if key in discovered:
                control.setReadOnly(True)
                row.hide()  # Discoverable values belong only in Environment Readiness.
            elif key in self.external_keys:
                user_grid.addWidget(row)
            else:
                advanced_grid.addWidget(row)
        for key in self.detected_auto_fields:
            control = QComboBox(self)
            control.addItems(("NOT DETECTED", "READY (DETECTED)"))
            control.setEnabled(False)
            control.hide()
            self.auto_fields[key] = control
        self.setup_empty = QLabel("No unresolved user input for the selected test.")
        user_grid.addWidget(self.setup_empty)
        self.save_auto_setup = button("SAVE SETUP")
        self.save_auto_setup.clicked.connect(self._save_auto_setup)
        user_grid.addWidget(self.save_auto_setup, alignment=Qt.AlignmentFlag.AlignLeft)
        self.join_sta_button = button("JOIN DISCOVERED DUT STA WI-FI")
        self.join_sta_button.clicked.connect(runtime.join_discovered_sta_wifi)
        user_grid.addWidget(self.join_sta_button, alignment=Qt.AlignmentFlag.AlignLeft)
        # Backend project configuration remains supported, but normal testers
        # see only discovered, read-only readiness.  Human-only confirmations
        # are presented by the selected TC's Setup Required flow.
        self.unresolved_title.hide()
        self.unresolved_setup.hide()
        self.advanced_toggle.hide()
        self.advanced_setup.hide()
        scroll.setWidget(content)
        content.setAutoFillBackground(False)
        root.addWidget(scroll, 1)
        self.setup_scroll = scroll
        self.run_button.clicked.connect(self.run_all)
        self.refresh_button.clicked.connect(self.refresh)
        self.table.currentCellChanged.connect(self._selected)
        service = runtime.services["Jetson"]
        if service:
            service.operation_succeeded.connect(self._operation_succeeded)
            service.operation_failed.connect(self._operation_failed)
        for key, control in self.auto_fields.items():
            if isinstance(control, QLineEdit) and key not in discovered:
                control.textChanged.connect(self._auto_field_changed)
            elif isinstance(control, QComboBox) and key not in self.detected_auto_fields:
                control.currentTextChanged.connect(self._auto_field_changed)
        self.runtime.changed.connect(self._sync_detected_fields)
        self.refresh()

    def set_context(self, case=None, cases=None):
        self.context_case = case
        self.context_cases = [case] if case else list(cases or load_auto_catalog())
        self._sync_detected_fields()

    def _navigate_dashboard(self):
        navigate_dashboard(self)

    def showEvent(self, event):
        super().showEvent(event)
        if hasattr(self.runtime.services.get("Jetson"), "connected"):
            self.runtime.refresh_auto_discovery()

    def _auto_values(self) -> dict:
        numeric_int = {"channel_24g", "frequency_24g_mhz", "channel_5g", "frequency_5g_mhz",
                       "recovery_cycles", "recovery_off_seconds"}
        numeric_float = {"packet_loss_threshold_percent", "udp_loss_threshold_percent",
                         "udp_jitter_threshold_ms", "udp_min_receiver_mbps"}
        values = {}
        for key, control in self.auto_fields.items():
            if key in self.detected_auto_fields:
                continue
            if isinstance(control, QComboBox):
                values[key] = control.currentText() == "READY"
                continue
            text = control.text().strip()
            if key in {"wifi_credential", "external_credential"} and not text:
                continue
            if key in numeric_int:
                try: values[key] = int(text) if text else None if key.startswith(("channel_", "frequency_")) else getattr(self.runtime.auto_setup, key)
                except ValueError: continue
            elif key in numeric_float:
                try: values[key] = float(text) if text else None
                except ValueError: continue
            else:
                values[key] = text
        return values

    def _auto_field_changed(self, *_args) -> None:
        self.runtime.update_auto_setup(persist=False, **self._auto_values())

    def _sync_detected_fields(self) -> None:
        setup = self.runtime.effective_auto_setup()
        values = {
            "Dashboard Jetson Connection": (f"{setup.ssh_transport} / {setup.control_interface}" if setup.shared_ssh_ready else "Not available", setup.shared_ssh_ready),
            "Wi-Fi interface": (setup.jetson_wifi_interface, bool(setup.jetson_wifi_interface)),
            "Laptop Wi-Fi interface": (setup.client_wifi_interface, bool(setup.client_wifi_interface)),
            "NetworkManager": (setup.network_manager_state or ("Active" if setup.network_manager_ready else "Inactive"), setup.network_manager_ready),
            "nmcli": ("Available" if setup.nmcli_available else "Not available", setup.nmcli_available),
            "iw": ("Available" if setup.iw_available else "Not available", setup.iw_available),
            "iperf3 local": (shutil.which("iperf3") or ("Available" if setup.local_iperf3_available else "Not available"), setup.local_iperf3_available),
            "iperf3 Jetson": ("Available" if setup.jetson_iperf3_available else "Not available", setup.jetson_iperf3_available),
            "Current SSID": (setup.current_ssid, bool(setup.current_ssid)),
            "Current mode": (setup.current_mode, bool(setup.current_mode)),
            "Current band": (setup.current_band, bool(setup.current_band)),
            "Current channel": (str(setup.current_channel or "Not detected"), bool(setup.current_channel)),
            "Current frequency": (f"{setup.current_frequency_mhz} MHz" if setup.current_frequency_mhz else "Not detected", bool(setup.current_frequency_mhz)),
            "Channel width": (setup.current_channel_width, bool(setup.current_channel_width)),
            "Jetson AP IP": (setup.current_ipv4 or setup.jetson_ap_ip, bool(setup.current_ipv4 or setup.jetson_ap_ip)),
            "Current route": (setup.current_route, bool(setup.current_route)),
            "Gateway": (setup.dut_gateway or setup.laptop_gateway, bool(setup.dut_gateway or setup.laptop_gateway)),
            "Hotspot profile": (setup.dut_active_profile or setup.ap_profile, bool(setup.dut_active_profile or setup.ap_profile)),
            "Saved Wi-Fi profile": (setup.saved_wifi_profile or "Not available", setup.saved_wifi_profile_available),
            "Wi-Fi capability": ("AP supported" if setup.ap_capable else "Not detected", setup.ap_capable),
            "Backup Ethernet": (setup.backup_ethernet_interface or "Not available", setup.backup_ethernet_ready),
            "Management path": (f"{setup.management_transport} / {setup.management_route_dev or setup.control_interface}" if setup.control_interface else "Not detected", bool(setup.control_interface)),
        }
        for check, (value, ready) in values.items():
            row = self.rows[check]
            state = "READY" if ready else "OPTIONAL" if check in self.OPTIONAL_CHECKS else "NOT READY"
            if check == "Dashboard Jetson Connection" and self.runtime.discovery_pending:
                state = "CHECKING"
            self.table.item(row, 1).setText(str(value or "Not detected"))
            self.table.item(row, 2).setText(state)
            self.table.item(row, 2).setForeground(QColor(ERROR_COLOR if state == "NOT READY" else "#344054"))
        for key, control in self.auto_fields.items():
            if key in self.detected_auto_fields:
                control.blockSignals(True)
                control.setCurrentIndex(int(bool(getattr(setup, key))))
                control.blockSignals(False)
            elif key in {"jetson_wifi_interface", "client_wifi_interface", "jetson_ap_ip", "ap_profile"}:
                control.setText(str(getattr(setup, key) or ""))
        missing_keys = {dep.key for case in self.context_cases for dep in self.runtime.dependency_report(case) if dep.required and not dep.satisfied}
        if any(case.wifi_role == "AP" and case.test_id.endswith("-001") and setup.current_mode.lower() == "ap" and setup.current_band and setup.current_band != ("2.4 GHz" if case.band == "2.4G" else "5 GHz") for case in self.context_cases) and not setup.allow_disruptive and (setup.primary_non_wifi_management or setup.backup_ethernet_ready):
            missing_keys.add("allow_disruptive")
        from desktop_app.wifi.auto_suite import threshold_configs_for
        optional_keys = {key for case in self.context_cases for key, config in threshold_configs_for(case).items() if config.classification == PROJECT_OPTIONAL}
        count = 0
        for key, row in self.field_rows.items():
            if key in self.external_keys:
                visible = key in missing_keys or self.auto_fields[key].hasFocus()
                row.setVisible(visible)
                count += int(visible)
            elif "threshold" in key or key == "udp_min_receiver_mbps":
                row.setVisible(key in optional_keys)
            elif key.startswith(("channel_", "frequency_", "test_ssid_", "rf_project_lock_")) and self.context_case:
                row.setVisible(("24g" in key) == (self.context_case.band == "2.4G"))
        self.unresolved_title.setText("UNRESOLVED SETUP" + (f" — {self.context_case.test_id}" if self.context_case else ""))
        self.setup_empty.setVisible(count == 0)
        self.save_auto_setup.setVisible(count > 0 or self.advanced_toggle.isChecked())
        self.join_sta_button.setVisible(setup.current_mode.lower() in {"managed", "station"} and not setup.full_wifi_qual_path)
        self.join_sta_button.setEnabled(setup.shared_ssh_ready and (setup.primary_non_wifi_management or setup.backup_ethernet_ready))
        fit_table(self.table, 530)
        self._update_summary()

    def _update_summary(self) -> None:
        states = [self.table.item(i, 2).text() for i in range(self.table.rowCount())]
        passed = sum(state in {"PASS", "READY"} for state in states)
        not_ready = sum(state in {"NOT READY", "NOT RUN", "RUNNING"} for state in states)
        failed = states.count("FAIL")
        optional = states.count("OPTIONAL")
        self.summary.setText(f"PASS {passed}   NOT READY {not_ready}   FAIL {failed}   OPTIONAL {optional}")
        # Optional backup readiness must not turn primary remote control red.
        required_states = [state for name, state in zip(self.CHECKS, states)
                           if name not in self.OPTIONAL_CHECKS]
        self.overall.setText("READY" if all(state in {"PASS", "READY", "NOT REQUIRED", "OPTIONAL"}
                                            for state in required_states) else "NOT READY")
        emphasize(self.overall, "ERROR" if failed else "WARNING" if not_ready else "READY")

    def _save_auto_setup(self) -> None:
        self.runtime.update_auto_setup(**self._auto_values())
        for key in ("wifi_credential", "external_credential"):
            control = self.auto_fields[key]
            control.blockSignals(True)
            control.clear()
            control.setPlaceholderText("Configured (hidden)" if getattr(self.runtime.auto_setup, key) else "NOT CONFIGURED")
            control.blockSignals(False)

    def _set(self, check: str, actual: str, status: str) -> None:
        row = self.rows[check]
        self.table.item(row, 1).setText(actual)
        self.table.item(row, 2).setText(status)
        self.table.item(row, 2).setForeground(QColor(ERROR_COLOR if status in {"FAIL", "BLOCKED", "ERROR", "NOT READY"} else "#344054"))
        self._update_summary()
        mapping = {
            "Wi-Fi interface": ("jetson_wifi_interface", actual if status == "PASS" else ""),
            "Hotspot profile": ("ap_profile", actual if status == "PASS" else ""),
            "iperf3 local": ("local_iperf3_available", status == "PASS"),
            "iperf3 Jetson": ("jetson_iperf3_available", status == "PASS"),
            "Jetson AP IP": ("jetson_ap_ip", actual if status == "PASS" else ""),
            "NetworkManager": ("network_manager_ready", status == "PASS"),
            "nmcli": ("nmcli_available", status == "PASS"),
            "iw": ("iw_available", status == "PASS"),
            "Wi-Fi capability": ("ap_capable", status == "PASS"),
        }
        if check in mapping and status not in {"RUNNING", "NOT RUN"}:
            key, value = mapping[check]
            self.runtime.update_detected_auto_setup(**{key: value})

    def _selected(self, row: int, _col: int, _old_row: int, _old_col: int) -> None:
        if row >= 0:
            self.detail.setText(f"{self.table.item(row, 0).text()}: {self.table.item(row, 1).text()} "
                                f"({self.table.item(row, 2).text()})")

    def refresh(self) -> None:
        service = self.runtime.services["Jetson"]
        connected = self.runtime._service_connected()
        self._set("Dashboard Jetson Connection",
                  "Verifying command execution…" if connected else "Connect from Dashboard",
                  "RUNNING" if connected else "NOT READY")
        local_iperf = shutil.which("iperf3")
        self._set("iperf3 local", local_iperf or "Missing", "PASS" if local_iperf else "NOT READY")
        for check in ("Wi-Fi interface", "NetworkManager", "Hotspot profile", "nmcli", "iw",
                      "iperf3 Jetson", "Wi-Fi capability", "Jetson AP IP"):
            if not connected:
                self._set(check, "Shared Jetson connection unavailable", "NOT RUN")
        root = self.runtime.evidence_root
        parent = root if root.exists() else root.parent
        writable = parent.is_dir() and __import__("os").access(parent, __import__("os").W_OK)
        self._set("Evidence path", str(root), "PASS" if writable else "NOT READY")
        disk = shutil.disk_usage(parent if parent.is_dir() else Path.cwd())
        self._set("Disk free", f"{disk.free / 1024**3:.1f} GiB", "PASS" if disk.free > 1024**3 else "NOT READY")
        self._sync_detected_fields()
        if service is not None and hasattr(service, "connected"):
            self.runtime.refresh_auto_discovery()

    def run_all(self) -> None:
        # One runtime-discovery path owns every value in this read-only table.
        # Do not reintroduce project/default interface or profile literals here.
        self.refresh()
        self.runtime.refresh_auto_discovery()

    def _operation_succeeded(self, request_id: str, results: object) -> None:
        if request_id not in self.pending:
            return
        self.pending.remove(request_id)
        interface = self.runtime.effective_auto_setup().jetson_wifi_interface or self.config["Wi-Fi interface"]
        profile = self.runtime.effective_auto_setup().ap_profile or self.config["Hotspot profile"]
        device = results[0]
        line = next((line for line in device.stdout.splitlines() if line.startswith(interface + ":wifi:")), "")
        self._set("Wi-Fi interface", interface if line else device.stdout.strip() or "Missing", "PASS" if line else "NOT READY")
        self._set("NetworkManager", results[1].stdout.strip(), "PASS" if results[1].stdout.strip() == "active" else "NOT READY")
        self._set("Hotspot profile", profile if results[2].exit_status == 0 else "Missing", "PASS" if results[2].exit_status == 0 else "NOT READY")
        for check, result in zip(("nmcli", "iw", "iperf3 Jetson"), results[3:6]):
            self._set(check, result.stdout.strip() or "Missing", "PASS" if result.exit_status == 0 else "NOT READY")
        supported = "* AP" in results[6].stdout
        self._set("Wi-Fi capability", "AP supported" if supported else "AP unavailable", "PASS" if supported else "NOT READY")
        ip_match = re.search(r"\binet\s+(\d+(?:\.\d+){3})(?:/\d+)?", results[7].stdout)
        self._set("Jetson AP IP", ip_match.group(1) if ip_match else "Missing",
                  "PASS" if ip_match else "NOT READY")
        self._sync_detected_fields()
        self.runtime.refresh_auto_discovery()

    def _operation_failed(self, request_id: str, error: str) -> None:
        if request_id in self.pending:
            self.pending.remove(request_id)
            for check in ("Wi-Fi interface", "NetworkManager", "Hotspot profile", "nmcli", "iw",
                          "iperf3 Jetson", "Wi-Fi capability", "Jetson AP IP", "Backup Ethernet"):
                self._set(check, error, "NOT READY")


class MetricCard(QFrame):
    def __init__(self, title: str, display_name: str | None = None):
        super().__init__()
        self.title = title
        self.setObjectName("WifiMetricCard")
        self.setFixedHeight(66)
        self.setMaximumWidth(220)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 7, 10, 7)
        layout.addWidget(QLabel(display_name or title))
        self.value = QLabel("—")
        self.value.setObjectName("WifiMetricValue")
        layout.addWidget(self.value)
        self.gauge = QProgressBar()
        self.gauge.setTextVisible(False)
        self.gauge.setMaximum(100)
        self.gauge.hide()
        layout.addWidget(self.gauge)

    def set_metric(self, value) -> None:
        text = metric_text(self.title, value)
        if self.value.text() != text:
            self.value.setText(text)
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
        self.cards_layout = QGridLayout()
        self.cards_layout.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
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
        self.matrix = WifiTable()
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
        if attempt.case.suite == "AUTO":
            presentation = presentation_for(attempt.case)
            resolved = resolved_metrics(presentation.summary_metrics, attempt.metrics)
            fields = tuple(source for _label, source, _value in resolved)
            values = {source: value for _label, source, value in resolved}
            semantic_names = {source: label for label, source, _value in resolved}
        else:
            values = attempt.metrics
            semantic_names = {}
        fields = tuple(name for name in fields if attempt.metrics.get(name) not in (None, "", "—") or name == "Elapsed" and attempt.elapsed_seconds)
        if tuple(self.cards) != fields:
            cache = getattr(self, "_metric_card_cache", {})
            cache.update(self.cards)
            self._metric_card_cache = cache
            for card in self.cards.values():
                self.cards_layout.removeWidget(card)
                card.hide()
            display_names = semantic_names if attempt.case.suite == "AUTO" else {}
            self.cards = {}
            for index, name in enumerate(fields):
                if name not in cache:
                    cache[name] = MetricCard(name, display_names.get(name))
                self.cards[name] = cache[name]
                card = self.cards[name]
                self.cards_layout.addWidget(card, index // 5, index % 5)
                card.show()
                if attempt.case.suite == "AUTO" and kind in {"throughput", "latency"}:
                    card.setFixedHeight(88)
        for name in fields:
            card = self.cards[name]
            if name == "Elapsed":
                seconds = attempt.elapsed_seconds
                card.set_metric(f"{seconds // 3600:02d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}")
            else:
                card.set_metric(values.get(name))
        for name, (row, label, bar) in self.rssi_bars.items():
            value = attempt.metrics.get(name)
            row.setVisible(isinstance(value, (int, float)))
            if isinstance(value, (int, float)):
                label.setText(metric_text(name, value))
                bar.setValue(max(0, min(100, int(2 * (value + 100)))))
        self.group_title.setText("LIVE / CAPTURED METRICS")
        self.group_title.setVisible(attempt.case.test_id != "TC-WIFI-C01")
        names = () if attempt.case.test_id == "TC-WIFI-C01" or attempt.case.suite == "AUTO" else DETAIL_METRICS[kind]
        kind_key = attempt.case.test_id if attempt.case.test_id == "TC-WIFI-C01" else kind
        if kind_key != self.current_kind:
            while self.group_layout.count():
                item = self.group_layout.takeAt(0)
                if item.widget():
                    item.widget().hide()
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
                chart.hide()
                chart.deleteLater()
            trend_names = {"throughput": ("Upload", "Download"),
                           "latency": ("RTT",), "rf": ("RSSI",),
                           "endurance": ("RSSI", "RTT"),
                           "band_throughput": ("2.4 GHz Average", "5 GHz Average")}.get(kind, ())
            if attempt.case.suite == "AUTO":
                trend_names = {"throughput": ("Forward receiver Mbps", "Reverse receiver Mbps"), "latency": ("RTT avg",), "rf": ("RSSI",), "endurance": ("RSSI", "RTT avg")}.get(kind, ())
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
            rows = tuple(prefix for prefix in rows if any(f"{prefix} {name}" in attempt.metrics for name in columns[1:]))
            self.matrix.setVisible(bool(rows))
            compact_table(self.matrix, 0, 200)
            self.matrix.setRowCount(len(rows))
            fit_table(self.matrix, 200)
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
            chart.setVisible(len(chart.points) >= 2 if attempt.case.suite == "AUTO" else bool(chart.points))
        if attempt.case.suite == "AUTO":
            self.group_title.hide()
            for cell in self.group_cells.values():
                cell.hide()
        self.setVisible(bool(self.cards) or any(not cell.isHidden() for cell in self.group_cells.values()) or
                        not self.progress.isHidden() or not self.cycles.isHidden() or
                        any(not chart.isHidden() for chart in self.charts.values()))


class WifiEnvironmentPage(QWidget):
    """One environment owns one persistent list widget and its view state."""

    def __init__(self, environment: str, runtime: WifiRuntime, cases=None,
                 suite: str = "EXISTING", parent=None):
        super().__init__(parent)
        self.environment = environment
        self.suite = suite
        self.runtime = runtime
        self.cases = list(load_catalog(environment) if cases is None else cases)
        self.by_id = {case.test_id: case for case in self.cases}
        self.selected: set[str] = set()
        self.current_case: WifiTestCase | None = None
        self._view_attempt: WifiAttempt | None = None
        self._console_case: WifiTestCase | None = None
        self._preflight_blocked_case: str | None = None
        self._launch_pending: str | None = None
        self._updating_table = False
        self._list_scroll = 0
        self._table_statuses: dict[str, str] = {}
        self.auto_queue: list[str] = []
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 8, 12, 10)
        if self.suite == "AUTO":
            batch_row = QHBoxLayout()
            self.batch_text = QLabel()
            self.batch_text.setWordWrap(True)
            self.batch_stop = button("STOP AUTO SUITE", "danger")
            self.batch_stop.clicked.connect(self.runtime.stop_batch)
            batch_row.addWidget(self.batch_text, 1)
            batch_row.addWidget(self.batch_stop)
            root.addLayout(batch_row)
            self.batch_text.hide()
            self.batch_stop.hide()
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
        self.runtime.auto_attempt_started.connect(self._auto_attempt_started)
        self.runtime.preflight_blocked.connect(self._preflight_blocked)
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
        self.view_running = button("VIEW RUNNING TEST", "primary")
        self.view_running.clicked.connect(self.show_execution)
        mini_row.addWidget(self.mini_text, 1)
        mini_row.addWidget(self.view_running)
        self.mini.hide()
        root.addWidget(self.mini)
        filters = QHBoxLayout()
        self.filters: dict[str, QComboBox] = {}
        definitions = [
            ("Category" if self.suite == "AUTO" else "Group", ("All", *sorted({case.category for case in self.cases}))),
            ("Status", ("All", "NOT RUN", "BLOCKED", "READY", "READY_WITH_RECONFIG", "READY_WITH_RECONNECT", "WAITING", "PREPARING", "RUNNING", "RESTORING", "STOPPED", "PASS", "FAIL", "ERROR", "NEEDS REVIEW")),
        ]
        if self.suite != "AUTO":
            definitions.append(("Mode", ("All", "AUTO", "GUIDED", "MANUAL")))
        for name, values in definitions:
            combo = QComboBox()
            combo.addItems(values)
            combo.currentTextChanged.connect(self._populate_table)
            self.filters[name] = combo
            filters.addWidget(QLabel(name))
            filters.addWidget(combo)
        reset = button("RESET")
        self.reset_button = reset
        reset.clicked.connect(self.reset_filters)
        filters.addWidget(reset)
        filters.addStretch()
        root.addLayout(filters)
        if self.suite == "AUTO":
            self.filter_summary = QLabel()
            root.addWidget(self.filter_summary)
        columns = ["", "Test ID", "Category" if self.suite == "AUTO" else "Group", "Test Name"]
        if self.suite != "AUTO": columns.append("Mode")
        columns.append("Status")
        if self.suite == "AUTO": columns.append("Block Reason")
        self.table = WifiTable(0, len(columns))
        self.table.setHorizontalHeaderLabels(columns)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().hide()
        header = self.table.horizontalHeader()
        fixed = ((0, 32), (1, 170 if self.suite == "AUTO" else 128), (2, 105),
                 (4, 105)) if self.suite == "AUTO" else ((0, 46), (1, 128), (2, 165), (4, 85), (5, 105))
        for col, width in fixed:
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.Fixed)
            self.table.setColumnWidth(col, width)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        if self.suite == "AUTO":
            header.setSectionResizeMode(5, QHeaderView.ResizeMode.Fixed)
            self.table.setColumnWidth(5, 220)
        compact_table(self.table, 3, 720)
        for col, width in fixed:
            self.table.horizontalHeader().setSectionResizeMode(col, QHeaderView.ResizeMode.Fixed)
            self.table.setColumnWidth(col, width)
        if self.suite == "AUTO":
            self.table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.Fixed)
            self.table.setColumnWidth(5, 220)
        self.table.itemChanged.connect(self._item_changed)
        self.table.cellClicked.connect(self._cell_clicked)
        self.table.cellDoubleClicked.connect(self._cell_double_clicked)
        if self.suite == "AUTO":
            self.list_splitter = QSplitter(Qt.Orientation.Vertical)
            self.list_splitter.setHandleWidth(7)
            self.list_splitter.setChildrenCollapsible(False)
            self.list_splitter.addWidget(self.table)
            self.selection_summary = QWidget()
            summary_layout = QVBoxLayout(self.selection_summary)
            summary_layout.setContentsMargins(0, 2, 0, 0)
            self.selection_title = QLabel("SELECTED TC DEPENDENCIES")
            summary_layout.addWidget(self.selection_title)
            self.selection_runtime = QLabel()
            self.selection_runtime.setWordWrap(True)
            summary_layout.addWidget(self.selection_runtime)
            self.selection_configuration = QLabel()
            self.selection_configuration.setWordWrap(True)
            summary_layout.addWidget(self.selection_configuration)
            self.selection_setup = SetupRequiredView(self.runtime, self._setup_action)
            self.selection_dependency_view = self.selection_setup.dependencies
            self.selection_dependencies = self.selection_dependency_view.table
            self.selection_scroll = QScrollArea()
            self.selection_scroll.setWidgetResizable(True)
            self.selection_scroll.setMinimumHeight(0)
            self.selection_scroll.setMaximumHeight(320)
            self.selection_scroll.setWidget(self.selection_setup)
            summary_layout.addWidget(self.selection_scroll)
            self.list_splitter.addWidget(self.selection_summary)
            self.list_splitter.setStretchFactor(0, 2)
            self.list_splitter.setStretchFactor(1, 1)
            self.selection_summary.hide()
            self._dependency_case_id = None
            self._summary_case_id = None
            self.table.currentCellChanged.connect(self._selection_dependency_changed)
            root.addWidget(self.list_splitter, 10)
        else:
            root.addWidget(self.table, 1)
        self.empty = QLabel(f"No {self.environment} Wi-Fi test catalog loaded.")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.setVisible(not self.cases)
        root.addWidget(self.empty)
        bottom = QHBoxLayout()
        self.counts = QLabel()
        self.run_selected = button("RUN SELECTED", "primary")
        self.run_selected.clicked.connect(self._run_selected)
        history = button("HISTORY")
        history.clicked.connect(self.show_history)
        bottom.addWidget(self.counts)
        bottom.addStretch()
        if self.suite != "AUTO":
            bottom.addWidget(self.run_selected)
        if self.suite == "AUTO":
            self.start_auto_suite = button("START AUTO SUITE", "primary")
            self.start_auto_suite.clicked.connect(self._run_all_ready)
            self.run_all_ready = button("RUN ALL READY", "primary")
            self.run_all_ready.clicked.connect(self._run_all_ready)

            self.reset_current_results = button("RESET CURRENT RESULTS")
            self.reset_current_results.clicked.connect(self._reset_current_results)

            bottom.addWidget(self.run_selected)
            bottom.addWidget(self.start_auto_suite)
            bottom.addWidget(self.run_all_ready)
            bottom.addWidget(self.reset_current_results)
        bottom.addWidget(history)
        root.addLayout(bottom)
        if self.suite == "AUTO":
            root.addStretch(1)
        return view

    def _case_status(self, case: WifiTestCase) -> str:
        """Status projected onto the normal TC list.

        AUTO uses Current Session only. Historical attempts remain available
        through History/Evidence and must not repopulate this list.
        """
        if self.suite == "AUTO":
            return self.runtime.current_session_status(
                self.environment,
                case.test_id,
            )
        return self.runtime.latest_status(
            self.environment,
            case.test_id,
        )

    def _visible_cases(self) -> list[WifiTestCase]:
        def matches(case):
            values = {
                "Group": case.category, "Category": case.category,
                "Mode": case.mode, "Status": self._case_status(case),
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
            values = ["", case.test_id, case.category, case.name]
            if self.suite != "AUTO": values.append(case.mode)
            status = self._case_status(case)
            reasons = self.runtime.block_reasons(case) if self.suite == "AUTO" else []
            optional = [dep.label for dep in self.runtime.dependency_report(case)
                        if dep.configuration_type == PROJECT_OPTIONAL and not dep.satisfied] if self.suite == "AUTO" else []
            values.append(status)
            if self.suite == "AUTO":
                short_reason = self._short_block_reason(case) if reasons else ""
                if reasons and case.wifi_role == "AP" and case.test_id.endswith(("-011", "-012")):
                    if any("Restarting the AP" in reason for reason in reasons):
                        short_reason = "AP restart control unavailable"
                    elif any("Client recovery intentionally" in reason for reason in reasons):
                        short_reason = "Client recovery control unavailable"
                    elif any("profile was not found" in reason for reason in reasons):
                        short_reason = "Credential/profile unavailable"
                    elif any("authorized" in reason for reason in reasons):
                        short_reason = "Disruptive execution not authorized"
                values.append(short_reason)
            for col, value in enumerate(values):
                item = QTableWidgetItem(value)
                if col == 0:
                    item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable)
                    item.setCheckState(Qt.CheckState.Checked if case.test_id in self.selected else Qt.CheckState.Unchecked)
                if self.suite == "AUTO" and col in {4, 5} and (reasons or optional):
                    blocking = "• " + "\n• ".join(reasons) if reasons else "None"
                    detail = "Blocking dependencies:\n" + blocking
                    if optional:
                        detail += "\n\nOptional missing configuration (does not block execution):\n• " + "\n• ".join(optional)
                    item.setToolTip(detail)
                if value in {"FAIL", "BLOCKED", "BLOCKING", "ERROR"} or (self.suite == "AUTO" and col == 5 and reasons):
                    item.setForeground(QColor(ERROR_COLOR))
                self.table.setItem(row, col, item)
        if self.suite == "AUTO" and self._dependency_case_id:
            selected_row = next((row for row, case in enumerate(visible)
                                 if case.test_id == self._dependency_case_id), None)
            if selected_row is not None:
                self.table.setCurrentCell(selected_row, 2)
            else:
                self._dependency_case_id = None
        self._updating_table = False
        self._table_statuses = {
            case.test_id: self._case_status(case)
            for case in self.cases
        }
        self.table.verticalScrollBar().setValue(scroll)
        if self.list_view.isVisible():
            self._list_scroll = self.table.verticalScrollBar().value()
        fit_table(self.table, 720)
        self._update_counts()
        if self.suite == "AUTO":
            self._update_selection_dependencies()
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
        if self.suite == "AUTO" and hasattr(self.runtime.services.get("Jetson"), "connected"):
            self.runtime.refresh_auto_discovery()

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
        self._update_counts()

    def _update_counts(self) -> None:
        active = [f"{name}: {combo.currentText()}" for name, combo in self.filters.items()
                  if combo.currentText() != "All"]
        filter_text = " · ".join(active) or "All"
        self.counts.setText(f"Visible: {self.table.rowCount()} / Total: {len(self.cases)}    "
                            f"Filter: {filter_text}    Selected: {len(self.selected)}")
        if self.suite == "AUTO":
            self.filter_summary.setText(f"Active filter: {filter_text}    Visible {self.table.rowCount()} of {len(self.cases)}")
            self.reset_button.setStyleSheet("font-weight: bold; color: #d29b36;" if active else "")

    def _selection_dependency_changed(self, row: int, *_args) -> None:
        if self._updating_table:
            return
        item = self.table.item(row, 1) if row >= 0 else None
        self._dependency_case_id = item.text() if item else None
        self._update_selection_dependencies()

    def _update_selection_dependencies(self) -> None:
        case = self.by_id.get(self._dependency_case_id)
        visible_ids = {self.table.item(row, 1).text() for row in range(self.table.rowCount())}
        if not case or case.test_id not in visible_ids:
            self.selection_summary.hide()
            self._summary_case_id = None
            height = min(720, 32 + 26 * self.table.rowCount())
            self.table.setMaximumHeight(height)
            self.list_splitter.setMaximumHeight(height)
            return
        blocked = bool(self.runtime.block_reasons(case))
        self.selection_title.hide()
        self.selection_runtime.hide()
        self.selection_configuration.hide()
        self.selection_setup.render(case)
        self.selection_summary.setVisible(blocked)
        table_height = min(720, 32 + 26 * self.table.rowCount())
        self.table.setMaximumHeight(table_height)
        summary_height = min(320, self.selection_setup.sizeHint().height() + 10) if blocked else 0
        self.list_splitter.setMaximumHeight(table_height + summary_height + 10)
        if self._summary_case_id != case.test_id:
            self.list_splitter.setSizes([table_height, summary_height])
        self._summary_case_id = case.test_id

    def _short_block_reason(self, case):
        labels = {"shared_ssh_ready": "SSH unavailable", "jetson_wifi_interface": "Wi-Fi interface not detected",
                  "external_credential": "Credential required", "distance_10m_confirmed": "10 m not confirmed",
                  "distance_1m_confirmed": "1 m not confirmed", "auto_reconnect": "Reconnect strategy unavailable"}
        blocking = [dep for dep in self.runtime.dependency_report(case) if dep.required and not dep.satisfied]
        if not blocking:
            return ""
        first = blocking[0]
        reason = labels.get(first.key, first.reason)
        return reason[0].upper() + reason[1:]

    def _cell_clicked(self, row: int, col: int) -> None:
        # Single click only selects/highlights.  Checkbox state remains owned by
        # _item_changed and never causes navigation.
        if self.suite != "AUTO" and col != 0:
            self.show_detail(self.table.item(row, 1).text())

    def _cell_double_clicked(self, row: int, col: int) -> None:
        if self.suite == "AUTO" and col in {1, 3}:
            self.show_test_console(self.table.item(row, 1).text())
        elif self.suite != "AUTO" and col != 0:
            self.show_detail(self.table.item(row, 1).text())

    def _reset_current_results(self) -> None:
        """Reset Current Session projection only; preserve History/Evidence."""
        if self.suite != "AUTO":
            return

        answer = QMessageBox.question(
            self,
            "Reset Current Results",
            "Reset current statuses?\n\n"
            "Historical attempts and evidence will be preserved.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )

        if answer != QMessageBox.StandardButton.Yes:
            return

        test_ids = {case.test_id for case in self.cases}

        self.runtime.reset_current_results(
            self.environment,
            test_ids,
        )

        self.selected.clear()
        self._populate_table()

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
        if self.suite == "AUTO":
            self.detail_tabs = None
            self.detail_pages = {}
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QFrame.Shape.NoFrame)
            content = QWidget()
            body = QVBoxLayout(content)
            body.setContentsMargins(0, 0, 0, 0)
            body.setAlignment(Qt.AlignmentFlag.AlignTop)
            title = QLabel("WHAT THIS TEST CHECKS")
            title.setObjectName("CardTitle")
            body.addWidget(title)
            self.auto_checks = QLabel()
            self.auto_checks.setWordWrap(True)
            body.addWidget(self.auto_checks)
            self.detail_setup = SetupRequiredView(self.runtime, self._setup_action)
            self.detail_dependency_view = self.detail_setup.dependencies
            body.addWidget(self.detail_setup)
            self.reconfigure_note = QLabel("The runner will temporarily change Wi-Fi configuration and restore it after the test.")
            self.reconfigure_note.setWordWrap(True)
            body.addWidget(self.reconfigure_note)
            self.run_this = button("RUN THIS TEST", "primary")
            self.run_this.clicked.connect(self._resolve_or_run)
            body.addWidget(self.run_this, alignment=Qt.AlignmentFlag.AlignLeft)
            source_toggle = QCheckBox("SOURCE DETAILS")
            body.addWidget(source_toggle)
            self.source_details = text_area("")
            self.source_details.setMaximumHeight(220)
            self.source_details.hide()
            source_toggle.toggled.connect(self.source_details.setVisible)
            body.addWidget(self.source_details)
            scroll.setWidget(content)
            content.setAutoFillBackground(False)
            root.addWidget(scroll, 1)
            return view
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
        self.run_this = button("RUN THIS TEST", "primary")
        self.run_this.clicked.connect(lambda: self._start_case(self.current_case))
        root.addWidget(self.run_this, alignment=Qt.AlignmentFlag.AlignRight)
        return view

    def _clear_layout(self, layout) -> None:
        while layout.count():
            item = layout.takeAt(0)
            if item.widget():
                item.widget().hide()  # Deferred deletion must not overlay the next TC.
                item.widget().deleteLater()

    def show_detail(self, test_id: str) -> None:
        self._remember_list_scroll()
        self._console_case = None
        case = self.by_id[test_id]
        self.current_case = case
        self.detail_id.setText(case.test_id)
        self.detail_name.setText(case.name)
        if self.suite == "AUTO":
            self._render_auto_detail()
            self.stack.setCurrentWidget(self.detail_view)
            self._set_pretest_context(case)
            return
        self.detail_meta.setText(f"{case.category.upper()}     {case.mode}     {case.phase}     "
                                 f"{self.runtime.latest_status(self.environment, test_id)}")
        for layout in self.detail_pages.values():
            self._clear_layout(layout)
        overview = self.detail_pages["OVERVIEW"]
        for name, value in (("Test ID", case.test_id), ("Test Name", case.name),
                            ("Purpose", case.purpose), ("Group", case.category),
                            ("Requirement", case.requirement), ("Mode", case.mode),
                            ("Suite", case.suite), ("Band / Role", f"{case.band} {case.wifi_role}".strip()),
                            ("Required Equipment", case.required_equipment),
                            ("Previous runs", str(len(self.runtime.attempts(self.environment, test_id))))):
            overview.addWidget(field(name, value))
        if case.suite == "AUTO":
            setup = self.runtime.effective_auto_setup()
            reasons = self.runtime.block_reasons(case)

            for phase in recovery_phases(case, setup):
                overview.addWidget(field(phase["name"], phase["status"] + "\n" +
                                         ("\n".join(phase["reasons"]) or "Required phase dependencies are ready.")))
            if case.wifi_role == "AP" and case.test_id.endswith(("-011", "-012")):
                overview.addWidget(field("Saved valid access", f"{setup.saved_wifi_profile or 'Not found'} / {setup.valid_access_source}\nPlaintext credential read: NO"))
                overview.addWidget(field("Temporary invalid profile", "Created only during C11 execution; valid saved profile is never modified."))
                if case.test_id.endswith("-012"):
                    overview.addWidget(field("AP restart control", f"Primary SSH: {setup.ssh_transport} / {setup.control_interface or 'unknown'}\nVerified non-Wi-Fi management: {'YES' if setup.primary_non_wifi_management or setup.backup_ethernet_ready else 'NO'}\nJetson detached self-recovery: NOT IMPLEMENTED" +
                                             ("\nA tested TC-specific recovery workflow is registered." if case.test_id in setup.auto_reconnect_cases else "")))
            dependency_title = QLabel("REQUIRED DEPENDENCIES")
            dependency_title.setObjectName("CardTitle")
            overview.addWidget(dependency_title)
            self.detail_dependency_view = DependencyView()
            self.detail_dependency_view.render(self.runtime.dependency_report(case))
            overview.addWidget(self.detail_dependency_view)
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
        evidence.addWidget(field("AUTO evidence", case.evidence_commands or ("Read-only shared Jetson capture" if case.mode == "AUTO" else "None")))
        evidence.addWidget(field("GUIDED evidence", "Tester observations and imported command output"))
        evidence.addWidget(field("Manual observations", "Record during execution"))
        suite_path = f"auto/{case.catalog_group}" if case.suite == "AUTO" else "existing"
        evidence.addWidget(field("Raw files", f"wifi/{self.environment}/{suite_path}/<session>/{case.test_id}/attempt_###/raw/"))
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

    def _set_pretest_context(self, case):
        parent = self.parentWidget()
        while parent and not isinstance(parent, WifiPage):
            parent = parent.parentWidget()
        if parent:
            parent.pretest.set_context(case)

    def _render_auto_detail(self):
        case = self.current_case
        if not case:
            return
        readiness = readiness_status(case, self.runtime.effective_auto_setup())
        self.detail_meta.setText(f"AUTO    {case.wifi_role} {'2.4 GHz' if case.band == '2.4G' else '5 GHz'}")
        specs = [spec for spec in criteria_for_auto(case, self.runtime.effective_auto_setup()) if spec.importance == "CRITICAL"]
        self.auto_checks.setText("This test automatically verifies:\n" + "\n".join(f"• {spec.name}: {spec.expected if spec.expected != 'NOT CONFIGURED' else 'Available / usable'}" for spec in specs))
        self.detail_setup.render(case)
        blocked = readiness == "BLOCKED"
        self.run_this.setText("RESOLVE SETUP" if blocked else "OPEN TEST CONSOLE")
        self.run_this.setObjectName("WarningButton" if blocked else "PrimaryButton")
        self.run_this.setProperty("actionRole", "review" if blocked else "primary")
        self.run_this.style().unpolish(self.run_this)
        self.run_this.style().polish(self.run_this)
        self.run_this.setEnabled(not self.runtime.wifi_busy)
        self.reconfigure_note.setVisible(readiness == "READY_WITH_RECONFIG")
        self.source_details.setPlainText(f"Source: {case.source}\nPreconditions: {case.preconditions}\nProcedure: {case.procedure}\nExpected: {case.expected}\nEvidence: {case.evidence_commands}")

    def _resolve_or_run(self):
        if self.current_case and self.runtime.block_reasons(self.current_case):
            self.detail_setup.render(self.current_case)
            self.runtime.refresh_auto_discovery()
        else:
            self.show_test_console(self.current_case.test_id)

    def _preflight_blocked(self, case):
        self._launch_pending = None
        if self.runtime.batch and self.runtime.batch.running:
            return  # Batch skips are recorded without navigating the UI.
        if self.suite == "AUTO" and case.test_id in self.by_id:
            self.auto_queue.clear()
            self.current_case = case
            self._console_case = case
            self._preflight_blocked_case = case.test_id
            self.stack.setCurrentWidget(self.execution_view)
            self._render_console()

    def _setup_action(self, action, key, case):
        if action == "dashboard":
            navigate_dashboard(self)
        elif action == "confirm":
            self.runtime.update_auto_setup(**{key: True})
            if self._console_case is case:
                # start() performs the lightweight fresh discovery again before
                # allocating an attempt, then proceeds automatically.
                QTimer.singleShot(0, lambda: self._start_case(case))
        elif action == "setup":
            self._set_pretest_context(case)
            self._navigate_pretest()
        else:
            self.runtime.refresh_auto_discovery()

    def _build_execution(self) -> QWidget:
        view = QWidget()
        root = QVBoxLayout(view)
        root.setContentsMargins(0, 0, 0, 0)
        back = button("← BACK TO TCs LIST")
        back.clicked.connect(self.back_to_list)
        nav = QHBoxLayout()
        nav.addWidget(back)
        self.go_pretest = button("GO TO PRE-TEST")
        self.go_pretest.clicked.connect(lambda: self._navigate_pretest())
        nav.addWidget(self.go_pretest)
        nav.addStretch()
        self.refresh_status = button("REFRESH STATUS")
        self.refresh_status.clicked.connect(self._refresh_console_status)
        self.stop_test = button("STOP TEST", "fail")
        self.stop_test.clicked.connect(self._stop_test)
        self.run_again = button("RUN AGAIN", "primary")
        self.run_again.clicked.connect(self._run_console_or_again)
        self.completed_folder = button("OPEN TEST FOLDER")
        self.completed_folder.clicked.connect(self._open_test_folder)
        for control in (self.refresh_status, self.stop_test, self.run_again, self.completed_folder):
            control.hide()
            nav.addWidget(control)
        root.addLayout(nav)
        overview_title = QLabel("RESULT OVERVIEW")
        overview_title.setObjectName("CardTitle")
        root.addWidget(overview_title)
        self.overview_title = overview_title
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
        status_row = QHBoxLayout()
        status_row.addWidget(self.execution_status)
        status_row.addWidget(self.auto_result_label)
        status_row.addStretch()
        root.addLayout(status_row)
        root.addWidget(self.result_reason_label)
        root.addWidget(self.execution_elapsed)
        self.execution_phase = QLabel()
        self.execution_phase.setObjectName("CardTitle")
        root.addWidget(self.execution_phase)
        self.execution_progress = QProgressBar()
        self.execution_progress.setMinimumHeight(20)
        self.execution_progress.setStyleSheet("QProgressBar { min-height: 20px; max-height: 20px; text-align: center; }")
        self.execution_progress.hide()
        root.addWidget(self.execution_progress)
        self.execution_expected.hide()
        self.result_counts = QLabel()
        root.addWidget(self.result_counts)
        self.execution_dependencies = DependencyView()
        root.addWidget(self.execution_dependencies)
        self.console_setup = SetupRequiredView(self.runtime, self._setup_action)
        self.console_setup.hide()
        root.addWidget(self.console_setup)
        self.splitter = QSplitter(Qt.Orientation.Vertical)
        self.layers = QTabWidget()
        top_panel = QWidget()
        top_layout = QVBoxLayout(top_panel)
        top_layout.setContentsMargins(0, 0, 0, 0)
        self.evaluation_title = evaluation_title = QLabel("CRITICAL CRITERIA")
        evaluation_title.setObjectName("CardTitle")
        top_layout.addWidget(evaluation_title)
        self.criteria_table = WifiTable(0, 5)
        self.criteria_table.setHorizontalHeaderLabels(("Criterion", "Expected", "Actual", "Result", "Action"))
        self.criteria_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.criteria_table.verticalHeader().hide()
        self.criteria_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        compact_table(self.criteria_table, 0, 250)
        top_layout.addWidget(self.criteria_table)
        self.issue_summary = QLabel()
        self.issue_summary.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        top_layout.addWidget(self.issue_summary)
        self.issue_details = QLabel()
        self.issue_details.setWordWrap(True)
        top_layout.addWidget(self.issue_details)
        self.monitor = WifiMonitor()
        self.supporting_toggle = QCheckBox("SUPPORTING INFORMATION")
        self.supporting_toggle.toggled.connect(lambda checked: self.supporting_table.setVisible(checked))
        top_layout.addWidget(self.supporting_toggle)
        self.supporting_table = WifiTable(0, 4)
        self.supporting_table.setHorizontalHeaderLabels(("Criterion", "Expected", "Actual", "Result"))
        self.supporting_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        compact_table(self.supporting_table, 0, 160)
        self.supporting_table.hide()
        top_layout.addWidget(self.supporting_table)
        self.disconnect_events_toggle = QCheckBox("DISCONNECT EVENTS")
        self.disconnect_events_view = QPlainTextEdit()
        self.disconnect_events_view.setReadOnly(True)
        self.disconnect_events_view.setMaximumHeight(120)
        self.disconnect_events_view.hide()
        self.disconnect_events_toggle.hide()
        self.disconnect_events_toggle.toggled.connect(self.disconnect_events_view.setVisible)
        top_layout.addWidget(self.disconnect_events_toggle)
        top_layout.addWidget(self.disconnect_events_view)
        top_layout.addWidget(self.monitor)
        if self.suite == "AUTO":
            top_layout.removeWidget(self.monitor)
            top_layout.insertWidget(0, self.monitor)
        top_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.top_scroll = QScrollArea()
        self.top_scroll.setWidgetResizable(True)
        self.top_scroll.setWidget(top_panel)
        self.top_scroll.setMinimumHeight(0)
        self.splitter.addWidget(self.top_scroll)
        self.summary_view = WifiTable(0, 2)
        self.summary_view.setHorizontalHeaderLabels(("Measurement", "Value"))
        self.summary_view.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.summary_view.verticalHeader().hide()
        self.summary_view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        compact_table(self.summary_view, 0, 420)
        self.measurements_view = self.summary_view
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
        helper = QLabel("Detailed command output for troubleshooting. Most users can use Result and Measurements without reading this log.")
        helper.setWordWrap(True)

        help_button = button("?")
        help_text = ("Technical Log contains the commands and detailed output collected during the test. "
                     "It is mainly useful for troubleshooting or evidence review. PASS/FAIL decisions are shown in Result and Evaluation.")
        help_button.setToolTip(help_text)
        help_button.clicked.connect(lambda: QMessageBox.information(self, "What is Technical Log?", help_text))
        modes = QHBoxLayout()
        self.log_modes = QTabWidget()
        self.events_view = text_area("")
        self.events_view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.log_modes.addTab(self.events_view, "EVENTS")
        self.log_modes.addTab(self.raw_view, "FULL OUTPUT")
        self.log_modes.currentChanged.connect(lambda _index: self._render_execution())
        help_button.setAccessibleName("What is Technical Log?")
        modes.addWidget(helper, 1)
        modes.addWidget(help_button)
        raw_layout.addLayout(modes)
        log_controls = QHBoxLayout()
        self.wrap_log = button("Wrap")
        self.wrap_log.setCheckable(True)
        self.wrap_log.toggled.connect(self._set_log_wrap)
        self.auto_scroll = button("Auto-scroll")
        self.auto_scroll.setCheckable(True)
        self.auto_scroll.setChecked(True)
        self.find_log = QLineEdit()
        self.find_log.setPlaceholderText("Find in log")
        self.find_log.returnPressed.connect(lambda: self._find_log(False))
        earlier_log = button("Older")
        earlier_log.setToolTip("Load the previous portion of a large captured output")
        earlier_log.clicked.connect(lambda: self._page_log(-1))
        later_log = button("Newer")
        later_log.setToolTip("Load the next portion of a large captured output")
        later_log.clicked.connect(lambda: self._page_log(1))
        next_log = button("Next")
        next_log.clicked.connect(lambda: self._find_log(False))
        previous_log = button("Prev")
        previous_log.clicked.connect(lambda: self._find_log(True))
        self.maximize_log = button("Expand")
        self.maximize_log.clicked.connect(self._toggle_log_maximize)
        self.chunk_controls = (earlier_log, later_log)
        for control in (self.wrap_log, self.auto_scroll, QLabel("Find:"), self.find_log, previous_log, next_log,
                        earlier_log, later_log, self.maximize_log):
            log_controls.addWidget(control)
        raw_layout.addLayout(log_controls)
        self.log_position = QLabel()
        raw_layout.addWidget(self.log_position)
        raw_layout.addWidget(self.log_modes, 1)
        self.import_widget = self.raw_input = self.import_output = self.import_status = None
        self.output_direction = self.output_band = None
        if self.suite != "AUTO":
            self.import_widget = QWidget()
            import_layout = QVBoxLayout(self.import_widget)
            import_layout.setContentsMargins(0, 0, 0, 0)
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
            import_layout.addWidget(self.raw_input)
            import_layout.addLayout(import_row)
            raw_layout.addWidget(self.import_widget)
            self.import_status = QLabel()
            raw_layout.addWidget(self.import_status)
        self.evidence_view = WifiTable(0, 3)
        self.evidence_view.setHorizontalHeaderLabels(("Evidence item", "Purpose", "Status"))
        self.evidence_view.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.evidence_view.verticalHeader().hide()
        self.evidence_view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.evidence_view.cellClicked.connect(self._show_evidence)
        compact_table(self.evidence_view, 1, 420)
        measurements_tab = QWidget()
        measurements_layout = QVBoxLayout(measurements_tab)
        measurements_layout.setContentsMargins(0, 0, 0, 0)
        measurements_layout.addWidget(self.summary_view)
        self.measurements_empty = QLabel("No measurements collected for this test.")
        measurements_layout.addWidget(self.measurements_empty)
        measurements_layout.addStretch()
        evidence_tab = QWidget()
        evidence_layout = QVBoxLayout(evidence_tab)
        evidence_layout.setContentsMargins(0, 0, 0, 0)
        self.evidence_location = QLabel()
        self.evidence_location.setWordWrap(True)
        evidence_layout.addWidget(self.evidence_location)
        self.open_test_folder = button("OPEN TEST FOLDER")
        self.open_test_folder.clicked.connect(self._open_test_folder)
        evidence_layout.addWidget(self.open_test_folder, alignment=Qt.AlignmentFlag.AlignLeft)
        evidence_layout.addWidget(self.evidence_view)
        evidence_layout.addStretch()
        self.layers.addTab(measurements_tab, "MEASUREMENTS")
        self.layers.addTab(raw_tab, "TECHNICAL LOG")
        if self.suite != "AUTO":
            self.layers.addTab(evidence_tab, "EVIDENCE")
        else:
            evidence_tab.setParent(view)
            evidence_tab.hide()
        self.layers.currentChanged.connect(lambda _index: self._render_execution())
        self.splitter.addWidget(self.layers)
        self.splitter.setStretchFactor(0, 45)
        self.splitter.setStretchFactor(1, 55)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.splitterMoved.connect(lambda _position, _index: setattr(self.runtime, "execution_splitter_sizes", self.splitter.sizes()))
        root.addWidget(self.splitter, 1)
        self.measurement_widget = QWidget()
        measurement = QHBoxLayout(self.measurement_widget)
        measurement.setContentsMargins(0, 0, 0, 0)
        measurement.addWidget(QLabel("Record measured value:"))
        self.metric_name = QComboBox()
        self.metric_value = QLineEdit()
        self.metric_value.setPlaceholderText("Observed value (number or text)")
        self.record_metric = button("RECORD")
        self.record_metric.clicked.connect(self._record_metric)
        measurement.addWidget(self.metric_name)
        measurement.addWidget(self.metric_value, 1)
        measurement.addWidget(self.record_metric)
        root.addWidget(self.measurement_widget)
        review_row = QHBoxLayout()
        review_row.addWidget(QLabel("Comment:"))
        self.review_comment = QLineEdit()
        review_row.addWidget(self.review_comment, 1)
        self.review_buttons = []
        for status in ("PASS", "FAIL", "NEEDS REVIEW"):
            role = {"PASS": "pass", "FAIL": "fail", "NEEDS REVIEW": "review"}[status]
            control = button(status, role)
            control.clicked.connect(lambda _checked=False, result=status: self._review(result))
            review_row.addWidget(control)
            self.review_buttons.append(control)
        self.review_widget = QWidget()
        self.review_widget.setLayout(review_row)
        root.addWidget(self.review_widget)
        self.result_review_toggle = QCheckBox("Review / override result")
        self.result_review_toggle.toggled.connect(lambda _checked: self._render_execution())
        self.result_review_toggle.hide()
        root.addWidget(self.result_review_toggle)
        self.override_button = button("OVERRIDE RESULT")
        self.override_button.clicked.connect(self._enable_override)
        review_row.addWidget(self.override_button)
        self._override_enabled = False
        self._log_maximized = False
        self.clock = QTimer(self)
        self.clock.setInterval(1000)
        self.clock.timeout.connect(self._tick_elapsed)
        self.clock.start()
        if self.suite != "AUTO":
            root.addStretch()
        return view

    def _stop_test(self):
        if not self.stop_test.isEnabled():
            return
        self.stop_test.setEnabled(False)
        self.stop_test.setText("STOPPING...")
        self.execution_status.setText("STOPPING...")
        self.auto_queue.clear()
        self.runtime.stop_batch()

    def _refresh_console_status(self):
        """Refresh discovery/readiness without starting or allocating an attempt."""
        if self.runtime.discovery_pending:
            return
        self.refresh_status.setEnabled(False)
        self.execution_status.setText("REFRESHING...")
        request_id = self.runtime.refresh_auto_discovery()
        if request_id is None:
            self.refresh_status.setEnabled(True)
            self._render_execution()

    def _manual_review(self, criterion_id: str, status: str, actual: QLineEdit, note: QLineEdit) -> None:
        try:
            self.runtime.review_criterion(criterion_id, status, actual.text().strip(), note.text().strip())
        except ValueError as error:
            self._message(str(error))

    def _tick_elapsed(self):
        """Clock updates never render criteria, measurements, or logs."""
        self._update_batch_ui()
        attempt = self._view_attempt or self.runtime.active
        if not attempt or attempt.environment != self.environment:
            return
        seconds = attempt.elapsed_seconds
        duration = f"{seconds // 3600:02d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"
        if attempt.case.suite == "AUTO":
            busy = attempt.status in {"RUNNING", "PREPARING", "STOPPING", "RESTORING"}
            self.execution_elapsed.setText(f"Started: {attempt.started.astimezone().strftime('%H:%M:%S')}    {'Elapsed' if busy else 'Duration'}: {duration}" +
                                          (f"    Completed: {attempt.finished.astimezone().strftime('%H:%M:%S')}" if attempt.finished else ""))
        else:
            target = attempt.metrics.get("Target duration")
            self.execution_elapsed.setText(f"Elapsed: {duration}{f' / {target}' if target else ''}     Attempt: {attempt.number:03d}     Target: {attempt.case.target}")

    def show_test_console(self, test_id: str) -> None:
        """Open a readiness-aware console without allocating an attempt."""
        self._remember_list_scroll()
        self.current_case = self.by_id[test_id]
        self._view_attempt = None
        self._console_case = self.current_case
        self._preflight_blocked_case = None
        self.stack.setCurrentWidget(self.execution_view)
        self._render_console()

    def _run_console_or_again(self) -> None:
        case = self._console_case
        if case is None:
            attempt = self._view_attempt or self.runtime.active
            case = attempt.case if attempt else None
        self._start_case(case)

    def _render_console(self) -> None:
        case = self._console_case
        if not case or self.stack.currentWidget() != self.execution_view:
            return
        readiness = readiness_status(case, self.runtime.effective_auto_setup()) if case.suite == "AUTO" else "READY"
        blocked = readiness == "BLOCKED"
        reported = self._preflight_blocked_case == case.test_id
        blocker_deps = [dep for dep in self.runtime.dependency_report(case) if dep.required and not dep.satisfied] if blocked else []
        human_blocked = any(dep.key in {"distance_1m_confirmed", "distance_10m_confirmed", "allow_disruptive"}
                            for dep in blocker_deps)
        preparing = self._launch_pending == case.test_id
        self.overview_title.setText("TEST CONSOLE")
        self.execution_title.setText(f"{case.test_id} — {case.name}")
        console_state = ("PREPARING..." if preparing else "SETUP REQUIRED" if reported and human_blocked
                         else "ERROR" if reported and blocked else readiness if not blocked else "READY FOR PREFLIGHT")
        self.execution_status.setText(console_state)
        emphasize(self.execution_status, "PREPARING" if preparing else "BLOCKED" if reported and blocked else "READY")
        self.auto_result_label.hide()
        reasons = self.runtime.block_reasons(case) if blocked and case.suite == "AUTO" else []
        self.result_reason_label.setText(("Physical/authorization setup is required before an attempt can start."
                                          if reported and human_blocked else
                                          "Automatic preflight failed: " + "; ".join(reasons)
                                          if reported and reasons else
                                          "Run will refresh discovery and evaluate this TC's prerequisites; no attempt has been created."
                                          if blocked else "READY — No setup action required"))
        self.result_reason_label.show()
        emphasize(self.result_reason_label, "BLOCKED" if reported and blocked else "READY")
        self.execution_elapsed.hide()
        self.execution_phase.hide()
        self.execution_progress.hide()
        self.stop_test.hide()
        self.completed_folder.hide()
        self.refresh_status.show()
        self.refresh_status.setEnabled(not self.runtime.discovery_pending and not self.runtime.wifi_busy)
        self.run_again.setText("PREPARING..." if preparing else "RETRY PREFLIGHT" if reported and blocked and not human_blocked else "RUN TEST")
        self.run_again.setVisible(not (reported and human_blocked))
        self.run_again.setEnabled(not preparing and not self.runtime.wifi_busy)
        self.go_pretest.setText("GO TO SETUP / READINESS")
        self.go_pretest.setVisible(True)
        self.execution_dependencies.setVisible(reported and blocked and case.suite == "AUTO" and not human_blocked)
        if reported and blocked and case.suite == "AUTO" and not human_blocked:
            self.execution_dependencies.render(self.runtime.dependency_report(case))
        self.console_setup.setVisible(reported and blocked and case.suite == "AUTO")
        if reported and blocked and case.suite == "AUTO":
            self.console_setup.render(case)
        specs = [spec for spec in criteria_for_auto(case, self.runtime.effective_auto_setup())
                 if spec.importance == "CRITICAL"] if case.suite == "AUTO" else []
        self.criteria_table.clearContents()
        self.criteria_table.setColumnCount(4)
        self.criteria_table.setHorizontalHeaderLabels(("Criterion", "Expected", "Actual", "Result"))
        self.criteria_table.setRowCount(len(specs))
        for row, spec in enumerate(specs):
            for col, value in enumerate((spec.name, spec.expected, "Not started", "WAITING")):
                self.criteria_table.setItem(row, col, QTableWidgetItem(str(value)))
        self.criteria_table.setVisible(bool(specs) and not (reported and blocked))
        self.evaluation_title.setVisible(bool(specs) and not (reported and blocked))
        fit_table(self.criteria_table, 250)
        self.monitor.hide()
        self.supporting_toggle.hide()
        self.supporting_table.hide()
        self.disconnect_events_toggle.hide()
        self.disconnect_events_view.hide()
        self.top_scroll.setVisible(bool(specs) and not (reported and blocked))
        self.result_counts.setText(f"Critical checks: 0 / {len(specs)} passed — test not started")
        emphasize(self.result_counts, "BLOCKED" if reported and blocked else "READY")
        self.layers.setCurrentIndex(0)
        self.summary_view.setRowCount(0)
        self.summary_view.hide()
        self.measurements_empty.setText("No measurements collected — test has not started.")
        self.measurements_empty.show()
        self.measurement_widget.hide()
        self.review_widget.hide()
        self.result_review_toggle.hide()

    def _render_execution(self) -> None:
        if self._console_case is not None:
            self._render_console()
            return
        attempt = self._view_attempt or self.runtime.active
        if not attempt or attempt.environment != self.environment:
            return
        if self.stack.currentWidget() != self.execution_view:
            self._update_mini()
            return
        self.execution_title.setText(f"{attempt.case.test_id} — {attempt.case.name}")
        self.console_setup.hide()
        self.execution_elapsed.show()
        self.run_again.setText("RUN AGAIN")
        self.go_pretest.setText("GO TO PRE-TEST")
        execution = attempt.status if attempt.status in {"RUNNING", "BLOCKED", "ERROR"} else "COMPLETED"
        decisions = {"PASS", "FAIL", "BLOCKED", "NEEDS REVIEW", "STOPPED", "ERROR", "MEASUREMENT ERROR"}
        decision = attempt.final_result if attempt.final_result in decisions else attempt.auto_result
        if decision not in decisions and any(row["status"] == "FAIL" and row.get("importance", "CRITICAL") == "CRITICAL" for row in attempt.criteria):
            decision = "FAIL"
        result = decision if decision in decisions else "NEEDS REVIEW"
        self.execution_status.setText(f"Execution: {execution}")
        self.auto_result_label.setText(f"Result: {result}")
        emphasize(self.execution_status, execution)
        emphasize(self.auto_result_label, "ERROR" if attempt.status == "ERROR" else result)
        emphasize(self.result_reason_label, "ERROR" if attempt.status == "ERROR" else result)
        self.result_reason_label.setVisible(result != "PASS" or attempt.status in {"RUNNING", "ERROR"} or bool(attempt.override_reason))
        blocked = attempt.status == "BLOCKED"
        self.go_pretest.setVisible(blocked or attempt.status == "ERROR" or result == "NEEDS REVIEW" and attempt.status != "RUNNING")
        self.execution_dependencies.setVisible(blocked and attempt.case.suite == "AUTO")
        if blocked and attempt.case.suite == "AUTO":
            self.execution_dependencies.render(self.runtime.dependency_report(attempt.case))
        self.result_reason_label.setText("Evaluation pending; criteria update as evidence arrives." if attempt.status == "RUNNING"
                                         else f"Reason: {attempt.result_reason}")
        if result == "FAIL":
            reasons = [row["reason"] if row["reason"].startswith(row["name"]) else f"{row['name']}: {row['reason']}"
                       for row in attempt.criteria if row["status"] == "FAIL" and row.get("importance", "CRITICAL") == "CRITICAL"]
            if reasons:
                self.result_reason_label.setText("; ".join(reasons))
        elif result == "NEEDS REVIEW" and (attempt.status not in {"RUNNING", "ERROR"} or attempt.status == "RUNNING" and not self.runtime.capture_pending and any(row.get("check") == "manual" for row in attempt.criteria)):
            pending = [row["name"] for row in attempt.criteria if row.get("importance", "CRITICAL") == "CRITICAL" and (row["status"] not in {"PASS", "FAIL"} or row.get("qualitative", False))]
            if pending:
                self.result_reason_label.setText("Review required: " + ", ".join(pending))
        if attempt.override_reason:
            self.result_reason_label.setText(f"Tester override: {attempt.override_reason} (automatic result: {attempt.auto_result})")
        self.auto_result_label.setToolTip(f"Automatic result: {attempt.auto_result}")
        auto = attempt.case.suite == "AUTO"
        busy = attempt.status in {"RUNNING", "PREPARING", "STOPPING", "RESTORING"}
        self.execution_phase.setVisible(auto and busy)
        self.execution_phase.setText(f"Phase: {attempt.phase} — {attempt.phase_detail}")
        self.stop_test.setVisible(auto and busy and self._view_attempt is None)
        self.stop_test.setEnabled(attempt.status in {"RUNNING", "PREPARING"})
        self.stop_test.setText("STOPPING..." if attempt.status == "STOPPING" else "STOPPING / RESTORING…" if attempt.status == "RESTORING" else "STOP TEST")
        self.refresh_status.setVisible(auto and not busy and self._view_attempt is None)
        self.refresh_status.setEnabled(not self.runtime.discovery_pending and not self.runtime.wifi_busy)
        self.run_again.setVisible(auto and not busy)
        self.completed_folder.setVisible(auto and not busy)
        recovery_progress = re.search(r"Reconnect cycle (\d+) / (\d+)", attempt.phase_detail)
        self.execution_progress.setVisible(auto and busy and (attempt.phase == "MEASURING" and attempt.progress_total > 0 or bool(recovery_progress)))
        if attempt.phase_detail.startswith("Endurance"):
            self.execution_progress.setRange(0, 7200)
            self.execution_progress.setValue(min(7200, attempt.elapsed_seconds))
            self.execution_progress.setFormat("%v / 7200 seconds")
        elif recovery_progress:
            cycle, total = map(int, recovery_progress.groups())
            self.execution_progress.setRange(0, total)
            self.execution_progress.setValue(max(0, cycle - 1))
            self.execution_progress.setFormat(f"Cycle {cycle} / {total}")
        else:
            self.execution_progress.setRange(0, max(1, attempt.progress_total))
            self.execution_progress.setValue(attempt.progress_done)
            self.execution_progress.setFormat(f"Measurement steps {attempt.progress_done} / {attempt.progress_total}")
        if auto:
            self.overview_title.setText("EXECUTION" if busy else "RESULT")
            self.go_pretest.setVisible(not busy and result in {"ERROR", "NEEDS REVIEW"})
            state = (attempt.status if busy else "STOPPED" if attempt.status == "STOPPED"
                     else "ERROR" if attempt.status == "ERROR"
                     else f"COMPLETED | {result}")
            if busy and attempt.phase == "PREPARING":
                state = "PREPARING"
            self.execution_status.setText(f"● {state}" if busy else f"✓ {state}" if state.startswith("COMPLETED") else state)
            emphasize(self.execution_status, attempt.status)
            self.execution_status.setStyleSheet(self.execution_status.styleSheet() + "font-size: 18px; font-weight: 700;")
            self.auto_result_label.setVisible(not busy or result == "FAIL")
            self.result_reason_label.setVisible(result in {"FAIL", "ERROR", "MEASUREMENT ERROR", "NEEDS REVIEW", "STOPPED"} and not busy or busy and result == "FAIL")
            if result == "STOPPED":
                self.result_reason_label.setText(attempt.result_reason)
        self.execution_expected.setText(f"Requirement: {attempt.case.expected}")
        seconds = attempt.elapsed_seconds
        target = attempt.metrics.get("Target duration")
        self.execution_elapsed.setText(f"Elapsed: {seconds // 3600:02d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"
                                       f"{f' / {target}' if target else ''}     "
                                       f"Attempt: {attempt.number:03d}     Target: {attempt.case.target}")
        if auto:
            stamp = attempt.started.astimezone().strftime("%H:%M:%S")
            label = "Elapsed" if busy else "Duration"
            self.execution_elapsed.setText(f"Started: {stamp}    {label}: {seconds // 3600:02d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}" +
                                          (f"    Completed: {attempt.finished.astimezone().strftime('%H:%M:%S')}" if attempt.finished else ""))
        kind = metric_kind(attempt.case)
        names = DETAIL_METRICS[kind]
        if tuple(self.metric_name.itemText(i) for i in range(self.metric_name.count())) != names:
            self.metric_name.clear()
            self.metric_name.addItems(names)
        self.record_metric.setEnabled(attempt.status == "RUNNING" and self._view_attempt is None)
        if self.import_widget is not None:
            self.import_output.setEnabled(attempt.status == "RUNNING" and self._view_attempt is None)
            manual_import = attempt.case.mode in {"GUIDED", "MANUAL"} and attempt.case.suite != "AUTO" and self._view_attempt is None and attempt.status == "RUNNING"
            self.import_widget.setVisible(manual_import)
            self.raw_input.setVisible(manual_import)
            self.import_output.setVisible(manual_import)
            self.import_status.setVisible(manual_import and bool(self.import_status.text()))
            self.output_direction.setVisible(manual_import and kind in {"throughput", "band_throughput"})
            self.output_band.setVisible(manual_import and kind == "band_throughput")
        has_manual_criteria = any(row.get("check") == "manual" for row in attempt.criteria)
        self.measurement_widget.setVisible(not has_manual_criteria and attempt.case.mode != "AUTO" and self._view_attempt is None and attempt.status in {"RUNNING", "PREPARING", "STOPPING", "RESTORING"})
        for control in self.review_buttons:
            control.setVisible((attempt.case.mode != "AUTO" and not has_manual_criteria) or self._override_enabled)
            control.setEnabled(self._view_attempt is None and ((attempt.status == "RUNNING" and not self.runtime.capture_pending and attempt.case.mode != "AUTO")
                               or (attempt.status in {"COMPLETED", "ERROR"} and self._override_enabled)))
        self.override_button.setVisible(self._view_attempt is None and attempt.case.mode == "AUTO" and attempt.status in {"COMPLETED", "ERROR"})
        self.override_button.setEnabled(not self._override_enabled)
        critical = [row for row in attempt.criteria if row.get("importance", "CRITICAL" if row["required"] else "INFORMATIONAL") == "CRITICAL"]
        supporting = [row for row in attempt.criteria if row not in critical and row.get("actual") not in (None, "", "—")]
        if auto:
            presentation = presentation_for(attempt.case)
            existing_metrics = {row.get("metric") for row in critical + supporting}
            for index, (label, source, value) in enumerate(
                    resolved_metrics(presentation.supporting_metrics, attempt.metrics)):
                if source in existing_metrics:
                    continue
                supporting.append({
                    "criterion_id": f"{attempt.case.test_id}-support-{index:02d}",
                    "name": label, "expected": "Diagnostic only", "actual": value,
                    "status": "INFO", "metric": source, "importance": "INFORMATIONAL",
                    "reason": "Attempt-local supporting information; does not determine PASS/FAIL",
                    "expected_rule": None,
                })
        counts = {state: sum(row["status"] == state for row in critical) for state in ("PASS", "FAIL")}
        blocking_count = len(self.runtime.block_reasons(attempt.case)) if blocked and attempt.case.suite == "AUTO" else 0
        review_count = sum(row["status"] not in {"PASS", "FAIL", "BLOCKED"} or row.get("qualitative", False) for row in critical)
        if blocked:
            self.result_reason_label.setText(f"Missing {blocking_count} required dependencies. Resolve setup in Pre-Test.")
        self.result_counts.setText(f"Passed  {counts['PASS']}     Failed  {counts['FAIL']}     Blocking  {blocking_count}     Review  {review_count}")
        if auto:
            self.result_counts.setText(f"Critical checks: {counts['PASS']} / {len(critical)} passed     Failed  {counts['FAIL']}" + (f"     Review  {review_count}" if not busy and result == "NEEDS REVIEW" else ""))
        emphasize(self.result_counts, "BLOCKED" if blocking_count else "FAIL" if counts['FAIL'] else "READY")
        self.review_widget.setVisible(any(not control.isHidden() for control in self.review_buttons) or not self.override_button.isHidden())
        self.result_review_toggle.setVisible(auto and not busy and result != "STOPPED" and self._view_attempt is None)
        if auto:
            self.review_widget.setVisible(self.result_review_toggle.isChecked() or self._override_enabled or result == "NEEDS REVIEW" and not busy)
        has_manual_actions = has_manual_criteria and self._view_attempt is None and attempt.status == "RUNNING"
        criteria_signals = QSignalBlocker(self.criteria_table)
        signature = (str(attempt.directory), tuple(row["criterion_id"] for row in critical), has_manual_actions)
        if signature != getattr(self, "_criteria_signature", None):
            self._criteria_signature = signature
            self._criteria_build_count = getattr(self, "_criteria_build_count", 0) + 1
            self._criteria_rows = {row["criterion_id"]: index for index, row in enumerate(critical)}
            self._last_rendered_criteria = {}
            self.criteria_table.clearContents()  # Structural build only, never live data updates.
            self.criteria_table.setColumnCount(5 if has_manual_actions else 4)
            self.criteria_table.setHorizontalHeaderLabels(("Criterion", "Expected", "Actual", "Result", "Action") if has_manual_actions else ("Criterion", "Expected", "Actual", "Result"))
            self.criteria_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
            self.criteria_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
            self.criteria_table.setColumnWidth(3, 135)
            if has_manual_actions:
                self.criteria_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
            available = max(500, self.criteria_table.viewport().width() - (340 if has_manual_actions else 135))
            for col, portion in enumerate((.35, .325, .325)):
                self.criteria_table.setColumnWidth(col, int(available * portion))
            self.criteria_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
            self.criteria_table.setRowCount(len(critical))
            self.criteria_table.setVisible(bool(critical) and not blocked)
            self.evaluation_title.setVisible(bool(critical) and not blocked)
            for row, criterion in enumerate(critical):
                self.criteria_table.setRowHeight(row, 26)
                display_status = "UNREVIEWED" if criterion['status'] == "MANUAL_REQUIRED" else (
                    "UNKNOWN" if criterion['status'] == "NOT_COLLECTED" else criterion['status'])
                if auto and busy and display_status == "WAITING":
                    display_status = "RUNNING"
                for col, value in enumerate((criterion["name"],
                                             criterion['expected'], "Pending" if auto and busy and criterion['actual'] is None else metric_text(criterion.get('metric') or criterion['name'], criterion['actual']),
                                             display_status)):
                    item = QTableWidgetItem(str(value))
                    item.setToolTip(criterion["criterion_id"] + "\n" + criterion['reason'] + "\nImportance: CRITICAL\nSource: " + criterion.get("source", criterion.get("configuration_type", "Source requirement")) + "\nEvidence: " + ", ".join(criterion.get('evidence_reference', [])))
                    if display_status in {"FAIL", "BLOCKED", "ERROR"} and col in {2, 3}:
                        item.setForeground(QColor(ERROR_COLOR))
                    self.criteria_table.setItem(row, col, item)
                if criterion.get("check") == "manual" and has_manual_actions:
                    actual = QLineEdit()
                    actual.setText("" if criterion["actual"] is None else str(criterion["actual"]))
                    actual.setPlaceholderText("Enter actual value if collection is unavailable")
                    actual.setObjectName("ManualCriterionActual")
                    self.criteria_table.setCellWidget(row, 2, actual)
                    actions = QWidget()
                    action_layout = QVBoxLayout(actions)
                    action_layout.setContentsMargins(2, 2, 2, 2)
                    buttons = QHBoxLayout()
                    note = QLineEdit()
                    note.setPlaceholderText("Optional note")
                    review = attempt.manual_reviews.get(criterion["criterion_id"], {})
                    note.setText(review.get("note", ""))
                    for status, label, role in (("PASS", "PASS", "pass"), ("FAIL", "FAIL", "fail"),
                                                ("NEEDS REVIEW", "NEEDS REVIEW", "review")):
                        control = button(label, role)
                        control.setObjectName({"pass": "SuccessButton", "fail": "DangerButton", "review": "WarningButton"}[role])
                        control.setEnabled(self._view_attempt is None)
                        control.clicked.connect(lambda _checked=False, cid=criterion["criterion_id"], result=status,
                                                        actual_field=actual, note_field=note:
                                                self._manual_review(cid, result, actual_field, note_field))
                        buttons.addWidget(control)
                    action_layout.addLayout(buttons)
                    action_layout.addWidget(note)
                    self.criteria_table.setCellWidget(row, 4, actions)
                    self.criteria_table.setRowHeight(row, 76)
            fit_table(self.criteria_table, 250)
        self.criteria_table.setVisible(bool(critical) and not blocked)
        self.evaluation_title.setVisible(bool(critical) and not blocked)
        with QSignalBlocker(self.criteria_table):
            for criterion in critical:
                cid = criterion["criterion_id"]
                display_status = {"MANUAL_REQUIRED": "UNREVIEWED", "NOT_COLLECTED": "UNKNOWN"}.get(criterion["status"], criterion["status"])
                if auto and busy and display_status == "WAITING":
                    display_status = "RUNNING"
                actual = "Pending" if auto and busy and criterion["actual"] is None else metric_text(criterion.get("metric") or criterion["name"], criterion["actual"])
                if criterion.get("metric") == "Host identity valid" and attempt.metrics.get("Actual host"):
                    actual = attempt.metrics["Actual host"]
                expected = attempt.metrics.get("Expected host", criterion["expected"]) if criterion.get("metric") == "Host identity valid" else criterion["expected"]
                values = (criterion["name"], expected, actual, display_status)
                tooltip = cid + "\n" + criterion["reason"] + "\nImportance: CRITICAL\nSource: " + criterion.get("source", criterion.get("configuration_type", "Source requirement")) + "\nEvidence: " + ", ".join(criterion.get("evidence_reference", []))
                if criterion.get("metric") == "Disconnects":
                    tooltip += "\n" + self._disconnect_evidence(attempt)
                rendered = (values, tooltip)
                if self._last_rendered_criteria.get(cid) == rendered:
                    continue
                row = self._criteria_rows[cid]
                for col, value in enumerate(values):
                    item = self.criteria_table.item(row, col)
                    if item.text() != str(value):
                        item.setText(str(value))
                        self._criteria_cell_update_count = getattr(self, "_criteria_cell_update_count", 0) + 1
                    if col == 3 and item.toolTip() != tooltip:
                        item.setToolTip(tooltip)
                    color = ERROR_COLOR if display_status in {"FAIL", "BLOCKED", "ERROR", "MEASUREMENT ERROR"} and col in {2, 3} else "#067647" if display_status == "PASS" and col == 3 else "#344054"
                    if item.foreground().color() != QColor(color):
                        item.setForeground(QColor(color))
                self._last_rendered_criteria[cid] = rendered
        del criteria_signals
        support_issues = sum(row["status"] in {"FAIL", "ERROR", "MEASUREMENT ERROR"} for row in supporting)
        summaries = []
        if "Disconnects" in attempt.metrics:
            summaries.append(f"{attempt.metrics['Disconnects']} disconnect events")
        if "RF samples" in attempt.metrics:
            summaries.append(f"{attempt.metrics['RF samples']} RF samples")
        route_checks = sum(key in attempt.metrics for key in ("Route interface", "Forward Wi-Fi path", "Reverse Wi-Fi path"))
        if route_checks:
            summaries.append(f"{route_checks} route checks")
        suffix = f" ({support_issues} warning{'s' if support_issues != 1 else ''})" if support_issues else f" — {' · '.join(summaries)}" if summaries else ""
        self.supporting_toggle.setText("SUPPORTING INFORMATION" + suffix)
        emphasize(self.supporting_toggle, "WARNING" if support_issues else "READY")
        self.supporting_toggle.setVisible(bool(supporting) and not blocked)
        self.supporting_table.setVisible(bool(supporting) and self.supporting_toggle.isChecked() and not blocked)
        support_signature = (str(attempt.directory), tuple(row["criterion_id"] for row in supporting))
        support_structure_changed = support_signature != getattr(self, "_support_structure", None)
        if support_structure_changed:
            self._support_structure = support_signature
            self.supporting_table.setRowCount(len(supporting))
        for row, criterion in enumerate(supporting):
            expected = criterion["expected"]
            rule = criterion.get("expected_rule") or {}
            if rule.get("kind") == "frequency_matches_channel":
                expected = f"Consistent with channel {attempt.metrics.get('Channel', '?')} / {attempt.metrics.get('Band', 'band')}"
            for col, value in enumerate((criterion["name"], expected, metric_text(criterion["metric"], criterion["actual"]), criterion["status"])):
                item = self.supporting_table.item(row, col)
                if item is None:
                    item = QTableWidgetItem(str(value))
                    self.supporting_table.setItem(row, col, item)
                elif item.text() != str(value):
                    item.setText(str(value))
                tooltip = criterion.get("importance", "INFORMATIONAL") + "\n" + criterion["reason"]
                if criterion["metric"] == "Disconnects":
                    tooltip += "\n" + self._disconnect_evidence(attempt)
                if item.toolTip() != tooltip:
                    item.setToolTip(tooltip)
                color = QColor(ERROR_COLOR if col in {2, 3} and criterion["status"] in {"FAIL", "ERROR", "MEASUREMENT ERROR"} else "#344054")
                if item.foreground().color() != color:
                    item.setForeground(color)
        if support_structure_changed:
            width = max(600, self.supporting_table.viewport().width() - 135)
            for col, portion in enumerate((.35, .325, .325)):
                self.supporting_table.horizontalHeader().setSectionResizeMode(col, QHeaderView.ResizeMode.Interactive)
                self.supporting_table.setColumnWidth(col, int(width * portion))
            self.supporting_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
            self.supporting_table.setColumnWidth(3, 135)
            fit_table(self.supporting_table, 160)
        self.issue_summary.hide()
        self.issue_details.hide()
        has_disconnect_metric = "Disconnects" in attempt.metrics and not blocked
        self.disconnect_events_toggle.setVisible(has_disconnect_metric)
        self.disconnect_events_toggle.setText(f"DISCONNECT EVENTS ({attempt.metrics.get('Disconnects', 0)} during test)")
        self.disconnect_events_view.setVisible(has_disconnect_metric and self.disconnect_events_toggle.isChecked())
        disconnect_text = self._disconnect_evidence(attempt)
        if self.disconnect_events_view.toPlainText() != disconnect_text:
            self.disconnect_events_view.setPlainText(disconnect_text)
        monitor_signature = (str(attempt.directory), json.dumps(attempt.metrics, sort_keys=True, default=str),
                             repr(attempt.trends), attempt.status)
        if monitor_signature != getattr(self, "_monitor_signature", None):
            self._monitor_signature = monitor_signature
            self.monitor.render(attempt)
        if blocked:
            self.monitor.hide()
        self.top_scroll.setVisible(not blocked and not self._log_maximized and (bool(critical) or not self.monitor.isHidden()))
        top_height = self.top_scroll.widget().sizeHint().height() + 4
        if self.top_scroll.widget().minimumHeight() != top_height:
            self.top_scroll.widget().setMinimumHeight(top_height)
        if auto:
            self.top_scroll.setMaximumHeight(16777215)
            self.layers.setMaximumHeight(16777215)
            self.splitter.setMaximumHeight(16777215)
            self.splitter.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
            if getattr(self, "_splitter_attempt", None) != str(attempt.directory):
                self._splitter_attempt = str(attempt.directory)
                sizes = getattr(self.runtime, "execution_splitter_sizes", None)
                height = max(400, self.splitter.height())
                self.splitter.setSizes(sizes or [int(height * .45), int(height * .55)])
        else:
            desired_top = min(480, max(32, top_height))
            self.top_scroll.setMaximumHeight(desired_top)
            if getattr(self, "_top_content_height", None) != desired_top and not self._log_maximized:
                self.splitter.setSizes([desired_top, max(150, self.splitter.height() - desired_top)])
            self._top_content_height = desired_top
        self._render_events(attempt)
        for control in self.chunk_controls:
            control.setVisible(self.log_modes.currentIndex() == 1 and self._log_paged)
        self.log_position.setVisible(self.log_modes.currentIndex() == 1 and self._log_paged)
        if self.layers.currentIndex() == 0:
            if auto:
                presentation = presentation_for(attempt.case)
                rows = [(label, metric_text(source, value))
                        for label, source, value in resolved_metrics(
                            presentation.measurement_metrics, attempt.metrics)]
            else:
                rows = [(name, metric_text(name, value)) for name, value in attempt.metrics.items()
                        if value not in (None, "", "—") and not isinstance(value, (dict, list))]
            measurement_signature = (str(attempt.directory), tuple(name for name, _value in rows))
            structure_changed = measurement_signature != getattr(self, "_measurement_structure", None)
            if structure_changed:
                self._measurement_structure = measurement_signature
                self.summary_view.setRowCount(len(rows))
            self.summary_view.setVisible(bool(rows))
            self.measurements_empty.setVisible(not rows)
            for row, pair in enumerate(rows):
                for col, value in enumerate(pair):
                    item = self.summary_view.item(row, col)
                    if item is None:
                        self.summary_view.setItem(row, col, QTableWidgetItem(str(value)))
                    elif item.text() != str(value):
                        item.setText(str(value))
            if structure_changed:
                fit_table(self.summary_view, 420)
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
            items = [(name, self._evidence_purpose(name, item), item["status"], attempt.directory / "raw" / name)
                     for name, item in attempt.evidence.items()]
            for filename, purpose in (("result.json", "Structured final result"), ("summary.json", "Captured measurements"), ("raw/commands.log", "Audit/debug command output")):
                path = attempt.directory / filename
                if path.is_file() and (filename != "raw/commands.log" or "commands.log" not in attempt.evidence):
                    items.append((filename, purpose, "CAPTURED", path))
            self.evidence_location.setText(f"Saved test evidence: {attempt.directory}")
            self.evidence_view.setRowCount(len(items))
            self._evidence_paths = []
            for row, (name, purpose, state, path) in enumerate(items):
                self._evidence_paths.append(path)
                for col, value in enumerate((name, purpose, state)):
                    item = QTableWidgetItem(str(value))
                    item.setToolTip(str(path))
                    if col == 2 and state in {"ERROR", "UNAVAILABLE"}:
                        item.setForeground(QColor(ERROR_COLOR))
                    self.evidence_view.setItem(row, col, item)
            fit_table(self.evidence_view, 420)
        self._update_mini()

    def _set_log_wrap(self, checked):
        mode = QPlainTextEdit.LineWrapMode.WidgetWidth if checked else QPlainTextEdit.LineWrapMode.NoWrap
        self.raw_view.setLineWrapMode(mode)
        self.events_view.setLineWrapMode(mode)

    def _find_log(self, backward: bool) -> None:
        query = self.find_log.text()
        if not query:
            return
        flags = QTextDocument.FindFlag.FindBackward if backward else QTextDocument.FindFlag(0)
        view = self.events_view if self.log_modes.currentIndex() == 0 else self.raw_view
        if view.find(query, flags):
            return
        cursor = view.textCursor()
        cursor.movePosition(cursor.MoveOperation.End if backward else cursor.MoveOperation.Start)
        view.setTextCursor(cursor)
        if view.find(query, flags):
            return
        if view is self.events_view:
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
        self.log_modes.setCurrentIndex(1)
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
        self.maximize_log.setText("Restore" if self._log_maximized else "Expand")
        self._render_execution()

    def _navigate_pretest(self):
        parent = self.parentWidget()
        while parent and not isinstance(parent, WifiPage):
            parent = parent.parentWidget()
        if parent:
            parent.tabs.setCurrentIndex(0)

    def _open_test_folder(self):
        attempt = self._view_attempt or self.runtime.active
        if attempt:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(attempt.directory)))

    @staticmethod
    def _evidence_purpose(name, item):
        command = item.get("command", "")
        if "journal" in command or "nm_log" in name:
            return "Troubleshooting service/kernel evidence"
        if any(part in command for part in ("iw ", "nmcli", "ip ")):
            return "Runtime Wi-Fi/interface evidence"
        if "iperf" in command or "ping" in command:
            return "Performance/reachability measurement"
        return "Audit/debug command output"

    def _render_events(self, attempt):
        events = getattr(attempt, "events", [])
        lines = []
        for event in events:
            if event["level"] == "COMMAND":
                continue  # Full Output groups authoritative commands/output.
            stamp = event["timestamp"]
            try:
                stamp = datetime.fromisoformat(stamp).astimezone().strftime("%H:%M:%S")
            except ValueError:
                pass
            lines.append((event["level"], f"{stamp}  {event['level']}  {event['message']}"))
        if not lines:
            lines = [("INFO", "INFO  This saved attempt has no event timeline. Full Output contains its captured commands.")]
        text = "\n".join(line for _, line in lines)
        if self.events_view.toPlainText() != text:
            prior_scroll = self.events_view.verticalScrollBar().value()
            previous = getattr(self, "_event_lines", [])
            append = (getattr(self, "_event_attempt", None) == str(attempt.directory)
                      and lines[:len(previous)] == previous)
            if not append:
                self.events_view.clear()  # Attempt/filter change, not a live append.
            cursor = self.events_view.textCursor()
            cursor.movePosition(cursor.MoveOperation.End)
            for index, (level, line) in enumerate(lines[len(previous):] if append else lines, start=len(previous) if append else 0):
                fmt = QTextCharFormat()
                fmt.setForeground(QColor(ERROR_COLOR if level == "ERROR" else WARNING_COLOR if level == "WARNING" else "#344054"))
                cursor.insertText(("\n" if index else "") + line, fmt)
            self._event_lines, self._event_attempt = lines, str(attempt.directory)
            self.events_view.verticalScrollBar().setValue(self.events_view.verticalScrollBar().maximum() if self.auto_scroll.isChecked() else prior_scroll)

    @staticmethod
    def _disconnect_evidence(attempt):
        events = attempt.metrics.get("Disconnect events", [])
        if not isinstance(events, list):
            return "Disconnect event collector data unavailable."
        return "\n".join(f"{event.get('timestamp', 'UNKNOWN')} {event.get('interface', 'UNKNOWN')} {event.get('previous_state', 'UNKNOWN')} -> {event.get('new_state', 'UNKNOWN')} ({event.get('source', 'UNKNOWN')})"
                         for event in events if isinstance(event, dict)) or "No disconnect transitions during this attempt."

    def _show_evidence(self, row: int, _col: int) -> None:
        if row >= len(getattr(self, "_evidence_paths", [])):
            return
        path = self._evidence_paths[row]
        if path.is_file():
            dialog = QMessageBox(self)
            dialog.setWindowTitle(path.name)
            dialog.setText(str(path))
            dialog.setDetailedText(redact_secrets(path.read_text(encoding="utf-8", errors="replace")))
            dialog.exec()

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
        if self.raw_input is None:
            return
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
        if case.suite == "AUTO" and snapshot.status == "BLOCKED":
            self.show_detail(case.test_id)
            return  # Legacy preflight archives never represent a started test.
        snapshot.auto_result = data.get("auto_result", "NOT EVALUATED")
        snapshot.final_result = data.get("final_result", data.get("status", "NEEDS REVIEW"))
        snapshot.result_reason = data.get("result_reason", "Historical attempt")
        snapshot.criteria = data.get("criteria", summary.get("criteria", []))
        snapshot.evidence = data.get("evidence", summary.get("evidence", {}))
        snapshot.manual_reviews = data.get("manual_reviews", summary.get("manual_reviews", {}))
        snapshot.metrics = data.get("actual_result", summary.get("metrics", {}))
        snapshot.events = data.get("events", summary.get("events", []))
        snapshot.phase = data.get("phase", "COMPLETED")
        snapshot.phase_detail = data.get("phase_detail", "Saved attempt")
        if data.get("finished"):
            snapshot.finished = datetime.fromisoformat(data["finished"])
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
        self.log_modes.setCurrentIndex(0)
        self.layers.setCurrentIndex(0)
        self.stack.setCurrentWidget(self.execution_view)
        self._render_execution()

    def show_execution(self) -> None:
        attempt = self.runtime.active
        if attempt and attempt.environment == self.environment:
            if attempt.case.test_id not in self.by_id:
                parent = self.parentWidget()
                while parent and not isinstance(parent, WifiPage):
                    parent = parent.parentWidget()
                if parent and attempt.case.suite == "AUTO":
                    parent.tabs.setCurrentIndex(1)
                    parent.vd.suite_tabs.setCurrentIndex(1)
                    owner = parent.vd.auto_pages[attempt.case.catalog_group]
                    parent.vd.auto_tabs.setCurrentWidget(owner)
                    owner.show_execution()
                elif parent:
                    parent.vd.suite_tabs.setCurrentIndex(0)
                    parent.vd.existing.show_execution()
                return
            self._view_attempt = None
            self._console_case = None
            self._remember_list_scroll()
            self.stack.setCurrentWidget(self.execution_view)
            self._render_execution()

    def _start_case(self, case: WifiTestCase | None) -> None:
        if case is None:
            return
        if self.runtime.wifi_busy:
            return
        self.current_case = case
        self._preflight_blocked_case = None
        if case.suite == "AUTO":
            self._launch_pending = case.test_id
            self.run_again.setEnabled(False)
            self.run_again.setText("PREPARING...")
            self.execution_status.setText("PREPARING...")
        attempt = self.runtime.start(self.environment, case)
        if attempt:
            self._launch_pending = None
            self._console_case = None
            if self._log_maximized:
                self._toggle_log_maximize()
            self._override_enabled = False
            self.result_review_toggle.setChecked(False)
            self._view_attempt = None
            self._raw_displayed = None
            self._log_paged = False
            self.auto_scroll.setChecked(True)
            self.raw_view.clear()
            self.log_modes.setCurrentIndex(0)
            self.supporting_toggle.setChecked(False)
            self.layers.setCurrentIndex(1 if self.suite == "AUTO" else 0)
            self.review_comment.clear()
            self.show_execution()

    def _auto_attempt_started(self, attempt: WifiAttempt) -> None:
        if attempt.environment != self.environment or attempt.case.test_id not in self.by_id:
            return
        self._console_case = None
        self._launch_pending = None
        if self.stack.currentWidget() != self.execution_view:
            self._view_attempt = None
            self.layers.setCurrentIndex(1)
            self._raw_displayed = None
            self._log_paged = False
            self.stack.setCurrentWidget(self.execution_view)
        self._render_execution()

    def _run_selected(self) -> None:
        if self.runtime.wifi_busy:
            self._message("A Wi-Fi test is already running.")
            return
        if self.suite == "AUTO":
            cases = [case for case in self.cases if case.test_id in self.selected]
            if not cases:
                self._message("Select at least one AUTO test.")
                return
            self.runtime.start_batch(self.environment, cases)
        else:
            if len(self.selected) != 1:
                self._message("Select one Wi-Fi test to run at a time.")
                return
            self._start_case(self.by_id[next(iter(self.selected))])

    def _run_all_ready(self) -> None:
        # The subgroup catalog is deliberately independent of visible filters,
        # selection and previous results. Fresh per-test preflight builds the queue.
        self.runtime.start_batch(self.environment, self.cases)

    def _update_batch_ui(self):
        if self.suite != "AUTO":
            return
        state = self.runtime.batch
        here = bool(state and state.environment == self.environment and
                    any(case.catalog_group == state.group for case in self.cases))
        self.batch_text.setVisible(here)
        self.batch_stop.setVisible(here and state.running)
        if not here:
            return
        attempt = self.runtime.active
        elapsed = attempt.elapsed_seconds if state.current and attempt and state.current.attempt == str(attempt.directory) else 0
        current = state.current.case.test_id if state.current else "—"
        counts = "   ".join(f"{name}: {value}" for name, value in state.counts.items())
        self.batch_text.setText(f"AUTO batch {state.status} · {state.position} / {len(state.queue)} · {current} · {state.phase}\n"
                                f"Test elapsed: {elapsed}s · Batch elapsed: {state.elapsed}s   {counts}" +
                                (f"\n{state.reason}" if state.reason else ""))
        self.batch_text.setToolTip("\n".join(f"{entry.case.test_id}: {entry.status} — {entry.reason}" for entry in state.entries))
        self.batch_stop.setEnabled(not state.stop_requested)

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
        self.history_table = WifiTable(0, 7)
        self.history_table.setHorizontalHeaderLabels(["Test ID", "Suite", "Band", "Mode", "Attempt", "Date", "Result"])
        self.history_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.history_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        compact_table(self.history_table, 5, 360)
        self.history_table.cellClicked.connect(self._history_selected)
        root.addWidget(self.history_table, 1)
        self.history_empty = QLabel("No saved attempts for this suite.")
        root.addWidget(self.history_empty)
        self.history_details = text_area("")
        self.history_details.setMaximumHeight(500)
        self.history_details.hide()
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
            info_path = path.parent / "test_info.json"
            info = json.loads(info_path.read_text(encoding="utf-8")) if info_path.is_file() else {}
            for col, value in enumerate((info.get("test_id", path.parent.parent.name),
                                         info.get("suite", self.suite), info.get("band", ""),
                                         info.get("mode", ""), path.parent.name,
                                         result.get("started", "—"), status)):
                item = QTableWidgetItem(value)
                if value in {"FAIL", "BLOCKED", "ERROR"}:
                    item.setForeground(QColor(ERROR_COLOR))
                self.history_table.setItem(row, col, item)
        fit_table(self.history_table, 360)
        self.history_table.setVisible(bool(self._history_paths))
        self.history_empty.setText("Select an attempt to view saved results and evidence." if self._history_paths else "No saved attempts for this suite.")
        self.history_details.clear()
        self.history_details.hide()
        self.stack.setCurrentWidget(self.history_view)

    def _history_selected(self, row: int, _col: int) -> None:
        self.history_details.show()
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
            f"EVIDENCE / TECHNICAL OUTPUT\n" + "\n".join(files) + f"\n\nTECHNICAL LOG\n{redact_secrets(raw_text)}"
        )

    def back_to_list(self) -> None:
        saved_scroll = self._list_scroll
        self._view_attempt = None
        self._console_case = None
        self.stack.setCurrentWidget(self.list_view)
        self._update_mini()
        self._list_scroll = saved_scroll
        QTimer.singleShot(0, self._restore_list_scroll)

    def _update_mini(self) -> None:
        attempt = self.runtime.active
        active_here = bool(attempt and attempt.environment == self.environment and attempt.status in {"RUNNING", "PREPARING", "STOPPING", "RESTORING"})
        self.mini.setVisible(active_here)
        self.run_selected.setEnabled(not self.runtime.wifi_busy)
        if self.suite == "AUTO":
            self.run_all_ready.setEnabled(not self.runtime.wifi_busy)
            self.start_auto_suite.setEnabled(not self.runtime.wifi_busy)
            if hasattr(self, "reset_current_results"):
                self.reset_current_results.setEnabled(not self.runtime.wifi_busy)
        self._update_batch_ui()
        if active_here:
            metrics = [f"{name}: {metric_text(name, attempt.metrics[name])}" for name in METRIC_FIELDS[metric_kind(attempt.case)]
                       if name in attempt.metrics][:4]
            state = "PREPARING" if attempt.phase == "PREPARING" else attempt.status
            self.mini_text.setText(f"● {attempt.case.test_id} {state}   Elapsed {attempt.elapsed_seconds // 60:02d}:"
                                   f"{attempt.elapsed_seconds % 60:02d}   " + "    ".join(metrics))

    def _runtime_changed(self) -> None:
        statuses = {
            case.test_id: self._case_status(case)
            for case in self.cases
        }
        if self.suite == "AUTO" or statuses != self._table_statuses:
            self._populate_table()
        else:
            self._update_mini()
        if self.stack.currentWidget() == self.execution_view:
            self._render_execution()
        elif self.suite == "AUTO" and self.stack.currentWidget() == self.detail_view:
            self._render_auto_detail()

    def _message(self, message: str) -> None:
        if self.isVisible():
            QMessageBox.information(self, "Wi-Fi", message)


class WifiVDPage(QWidget):
    """Second-level VD navigation keeps existing and AUTO catalogs isolated."""
    GROUPS = (("AP_24G", "AP 2.4 GHz"), ("AP_5G", "AP 5 GHz"),
              ("STA_24G", "STA 2.4 GHz"), ("STA_5G", "STA 5 GHz"))

    def __init__(self, runtime: WifiRuntime, parent=None):
        super().__init__(parent)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        self.suite_tabs = QTabWidget()
        self.suite_tabs.setObjectName("WifiSuiteTabs")
        self.existing = WifiEnvironmentPage("VD", runtime, suite="EXISTING")
        self.suite_tabs.addTab(self.existing, "EXISTING TCs")
        auto_widget = QWidget()
        auto_layout = QVBoxLayout(auto_widget)
        auto_layout.setContentsMargins(4, 4, 4, 4)
        self.auto_tabs = QTabWidget()
        all_cases = load_auto_catalog()
        self.auto_pages = {}
        for group, label in self.GROUPS:
            page = WifiEnvironmentPage("VD", runtime,
                                       cases=[case for case in all_cases if case.catalog_group == group],
                                       suite="AUTO")
            self.auto_pages[group] = page
            self.auto_tabs.addTab(page, f"{label} ({len(page.cases)})")
        auto_layout.addWidget(self.auto_tabs)
        self.suite_tabs.addTab(auto_widget, "AUTO TCs")
        root.addWidget(self.suite_tabs)


class WifiPage(QWidget):
    dashboard_requested = Signal()
    def __init__(self, jetson_service, evidence_root=None, parent=None):
        super().__init__(parent)
        self.runtime = WifiRuntime(jetson_service, evidence_root=evidence_root or EVIDENCE_ROOT, parent=self)
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 12, 16, 12)
        self.tabs = QTabWidget()
        self.pretest = WifiPreTestPage(self.runtime)
        self.tabs.addTab(self.pretest, "PRE-TEST")
        self.environments = {}
        self.vd = WifiVDPage(self.runtime)
        self.environments["VD"] = self.vd.existing
        self.auto_environments = self.vd.auto_pages
        self.tabs.addTab(self.vd, "VD")
        for environment in ("VMO", "VR"):
            page = WifiEnvironmentPage(environment, self.runtime)
            self.environments[environment] = page
            self.tabs.addTab(page, environment)
        root.addWidget(self.tabs)
