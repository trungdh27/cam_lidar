from __future__ import annotations

import re

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from devices.livox.testing.ros2_parsers import (
    parse_ros2_hz,
    parse_sectioned_output,
)


class LidarRos2Page(QWidget):
    """Internal, diagnostic-only view for the production ROS2 target."""

    _MAX_OUTPUT_CHARS = 16000

    def __init__(self, profile, service, parent=None):
        super().__init__(parent)
        self.profile = profile
        self.service = service
        self._active_request_id = None
        self._active_action = None
        self._build_ui()
        self.service.request_succeeded.connect(self._on_request_succeeded)
        self.service.request_failed.connect(self._on_request_failed)
        self.service.request_cancelled.connect(self._on_request_cancelled)

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 14, 18, 12)
        root.setSpacing(10)

        target_group = QGroupBox("ROS2 Target")
        target_layout = QVBoxLayout(target_group)
        target_grid = QGridLayout()
        target_values = (
            ("Host", self.profile.host),
            ("Username", self.profile.username),
            ("Port", str(self.profile.port)),
            ("ROS Distro", self.profile.ros_distro),
            ("Container", self.profile.container_name),
            ("Production Domain ID", str(self.profile.production_ros_domain_id)),
            ("LiDAR interface", self.profile.lidar_interface),
            ("Host IP", self.profile.host_cidr),
            ("LiDAR IP", self.profile.lidar_ip),
        )
        for row, (label, value) in enumerate(target_values):
            target_grid.addWidget(QLabel(f"{label}:"), row // 3, (row % 3) * 2)
            value_label = QLabel(value)
            value_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            target_grid.addWidget(value_label, row // 3, (row % 3) * 2 + 1)
        target_layout.addLayout(target_grid)

        actions = QHBoxLayout()
        self.environment_status = QLabel("NOT CHECKED")
        self.environment_status.setStyleSheet("font-weight:700;")
        self.check_environment_button = QPushButton("CHECK ENVIRONMENT")
        self.refresh_graph_button = QPushButton("REFRESH ROS GRAPH")
        self.check_environment_button.clicked.connect(self.check_environment)
        self.refresh_graph_button.clicked.connect(self.refresh_graph)
        actions.addWidget(QLabel("Status:"))
        actions.addWidget(self.environment_status)
        actions.addStretch()
        actions.addWidget(self.check_environment_button)
        actions.addWidget(self.refresh_graph_button)
        target_layout.addLayout(actions)
        root.addWidget(target_group)

        graph_group = QGroupBox("ROS Graph")
        graph_layout = QHBoxLayout(graph_group)
        self.node_list = QListWidget()
        self.topic_list = QListWidget()
        self.topic_list.currentTextChanged.connect(self._topic_selected)
        graph_layout.addWidget(self._list_panel("Nodes", self.node_list))
        graph_layout.addWidget(self._list_panel("Topics", self.topic_list))
        root.addWidget(graph_group)

        inspector_group = QGroupBox("Topic Inspector")
        inspector_layout = QVBoxLayout(inspector_group)
        selected_row = QHBoxLayout()
        selected_row.addWidget(QLabel("Selected Topic:"))
        self.selected_topic_label = QLabel("-")
        self.selected_topic_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        selected_row.addWidget(self.selected_topic_label, 1)
        inspector_layout.addLayout(selected_row)

        inspector_actions = QHBoxLayout()
        self.type_button = QPushButton("TYPE")
        self.info_button = QPushButton("INFO")
        self.hz_button = QPushButton("HZ")
        self.echo_button = QPushButton("ECHO ONCE")
        for button, callback in (
            (self.type_button, self.topic_type),
            (self.info_button, self.topic_info),
            (self.hz_button, self.topic_hz),
            (self.echo_button, self.topic_echo_once),
        ):
            button.clicked.connect(callback)
            inspector_actions.addWidget(button)
        inspector_actions.addStretch()
        inspector_layout.addLayout(inspector_actions)

        self.summary_table = QTableWidget(0, 2)
        self.summary_table.setHorizontalHeaderLabels(["Field", "Value"])
        self.summary_table.verticalHeader().setVisible(False)
        self.summary_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.summary_table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        inspector_layout.addWidget(self.summary_table)
        root.addWidget(inspector_group)

        output_group = QGroupBox("Output / Details")
        output_layout = QVBoxLayout(output_group)
        self.output_text = QTextEdit()
        self.output_text.setReadOnly(True)
        self.output_text.setMinimumHeight(150)
        output_layout.addWidget(self.output_text)
        root.addWidget(output_group, 1)

        self._operation_buttons = [
            self.check_environment_button,
            self.refresh_graph_button,
            self.type_button,
            self.info_button,
            self.hz_button,
            self.echo_button,
        ]
        self._set_topic_actions_enabled(False)

    @staticmethod
    def _list_panel(title: str, widget: QListWidget) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(QLabel(title))
        layout.addWidget(widget)
        return panel

    def _set_topic_actions_enabled(self, enabled: bool):
        for button in (self.type_button, self.info_button, self.hz_button, self.echo_button):
            button.setEnabled(enabled and self._active_request_id is None)

    def _set_busy(self, busy: bool, action: str = ""):
        for button in self._operation_buttons:
            button.setEnabled(not busy)
        if not busy:
            self._set_topic_actions_enabled(self.topic_list.currentRow() >= 0)
        if busy:
            self.environment_status.setText("CHECKING" if action in {"environment", "graph"} else "RUNNING")

    def _submit(self, action: str, request_id: str | None):
        if request_id is None:
            self.environment_status.setText("ERROR")
            self.output_text.setPlainText("ROS2 target is disabled or unavailable.")
            return
        self._active_request_id = request_id
        self._active_action = action
        self._set_busy(True, action)

    def check_environment(self):
        self._submit("environment", self.service.check_environment())

    def refresh_graph(self):
        self._submit("graph", self.service.refresh_graph())

    def _selected_topic(self) -> str | None:
        topic = self.topic_list.currentItem()
        return topic.text().strip() if topic is not None and topic.text().strip() else None

    def topic_type(self):
        topic = self._selected_topic()
        if topic:
            self._submit("type", self.service.topic_type(topic))

    def topic_info(self):
        topic = self._selected_topic()
        if topic:
            self._submit("info", self.service.topic_info(topic))

    def topic_hz(self):
        topic = self._selected_topic()
        if topic:
            self._submit("hz", self.service.topic_hz(topic))

    def topic_echo_once(self):
        topic = self._selected_topic()
        if topic:
            self._submit("echo", self.service.topic_echo_once(topic))

    def _topic_selected(self, topic: str):
        self.selected_topic_label.setText(topic or "-")
        self.summary_table.setRowCount(0)
        self._set_topic_actions_enabled(bool(topic))

    def _on_request_succeeded(self, request_id: str, result: object):
        if request_id != self._active_request_id:
            return
        action = self._active_action
        self._active_request_id = None
        self._active_action = None
        result = result if isinstance(result, dict) else {}
        exit_status = int(result.get("exit_status", -1))
        bounded_hz_timeout = action == "hz" and exit_status == 124
        if exit_status != 0 and not bounded_hz_timeout:
            self._show_error(result.get("stderr") or "ROS2 command failed.")
        elif action in {"environment", "graph"}:
            self._render_environment(result)
        else:
            self._render_topic_result(action, result)
        self._set_busy(False)

    def _on_request_failed(self, request_id: str, error: str):
        if request_id != self._active_request_id:
            return
        self._active_request_id = None
        self._active_action = None
        self._show_error(error)
        self._set_busy(False)

    def _on_request_cancelled(self, request_id: str):
        if request_id != self._active_request_id:
            return
        self._active_request_id = None
        self._active_action = None
        self._show_error("ROS2 operation cancelled.")
        self._set_busy(False)

    def _render_environment(self, result: dict):
        stdout = str(result.get("stdout") or "")
        stderr = str(result.get("stderr") or "")
        sections = parse_sectioned_output(stdout)
        address = sections.get("ADDR", "")
        container = sections.get("CONTAINER", "")
        ros_available = bool(
            sections.get("ROS_VERSION")
            or sections.get("NODES")
            or sections.get("TOPICS")
        )
        environment_ok = bool(
            sections.get("ARCH")
            and ros_available
            and container == "true"
            and self.profile.host_cidr.split("/")[0] in address
        )
        ros_distro = sections.get("ROS_DISTRO") or self.profile.ros_distro
        domain_id = sections.get("DOMAIN") or str(self.profile.production_ros_domain_id)
        rows = [
            ("Target reachable", "yes", "OK"),
            ("Hostname", sections.get("HOSTNAME", "not reported"), "INFO"),
            ("Architecture", sections.get("ARCH", "not reported"), "OK" if sections.get("ARCH") else "ERROR"),
            ("Kernel", sections.get("KERNEL", "not reported"), "INFO"),
            ("ROS2 available", "yes" if ros_available else "not confirmed", "OK" if ros_available else "ERROR"),
            ("ROS Distro", ros_distro, "OK" if sections.get("ROS_DISTRO") else "CONFIGURED"),
            ("Container", f"{self.profile.container_name}: {container or 'not reported'}", "OK" if container == "true" else "ERROR"),
            ("Domain ID", domain_id, "OK" if sections.get("DOMAIN") else "CONFIGURED"),
            ("Interface", self.profile.lidar_interface, "CONFIGURED"),
            ("Host IP", self.profile.host_cidr, "OK" if self.profile.host_cidr.split("/")[0] in address else "CHECK"),
            ("LiDAR IP", self.profile.lidar_ip, "CONFIGURED"),
        ]
        self._set_environment_rows(rows)
        self._fill_list(self.node_list, sections.get("NODES", ""))
        self._fill_list(self.topic_list, sections.get("TOPICS", ""))
        self.environment_status.setText("READY" if environment_ok else "ERROR")
        details = stdout
        if stderr:
            details += f"\n\nSTDERR:\n{stderr}"
        self.output_text.setPlainText(self._bounded_text(details)[0])

    def _set_environment_rows(self, rows):
        self.environment_table = getattr(self, "environment_table", None)
        if self.environment_table is None:
            self.environment_table = QTableWidget(0, 3)
            self.environment_table.setHorizontalHeaderLabels(["Parameter", "Value", "Status"])
            self.environment_table.verticalHeader().setVisible(False)
            self.environment_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
            self.environment_table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
            self.layout().insertWidget(1, self.environment_table)
        self.environment_table.setRowCount(len(rows))
        for row, values in enumerate(rows):
            for column, value in enumerate(values):
                self.environment_table.setItem(row, column, QTableWidgetItem(str(value)))

    def _fill_list(self, widget: QListWidget, text: str):
        widget.clear()
        for line in text.splitlines():
            value = line.strip()
            if value and not value.lower().startswith("/bin/"):
                widget.addItem(value)

    def _render_topic_result(self, action: str, result: dict):
        stdout = str(result.get("stdout") or "")
        bounded, truncated = self._bounded_text(stdout)
        self.output_text.setPlainText(bounded + ("\n\nOutput truncated" if truncated or result.get("output_truncated") else ""))
        if action == "type":
            self._set_summary({"Topic": self._selected_topic() or "-", "Type": stdout.strip() or "unknown"})
        elif action == "info":
            self._set_summary(self._parse_topic_info(stdout))
        elif action == "hz":
            metrics = parse_ros2_hz(stdout)
            self._set_summary({
                "Topic": self._selected_topic() or "-",
                "Measured Hz": metrics["mean_hz"] if metrics["mean_hz"] is not None else "not reported",
                "Min interval": metrics["min_interval_sec"] if metrics["min_interval_sec"] is not None else "not reported",
                "Max interval": metrics["max_interval_sec"] if metrics["max_interval_sec"] is not None else "not reported",
                "Std dev": metrics["mean_stddev_sec"] if metrics["mean_stddev_sec"] is not None else "not reported",
                "Sample count": metrics["sample_count"],
            })
        elif action == "echo":
            self._set_summary({"Topic": self._selected_topic() or "-", "Echo": "one message"})
            frame = re.search(r"frame_id:\s*([^\n]+)", stdout)
            if frame:
                self._set_summary({"Topic": self._selected_topic() or "-", "Frame ID": frame.group(1).strip()})

    @staticmethod
    def _parse_topic_info(text: str) -> dict[str, str]:
        fields = {"Topic": "-"}
        patterns = {
            "Type": r"(?:Type|Topic type):\s*(\S+)",
            "Publishers": r"Publisher count:\s*(\d+)",
            "Subscribers": r"Subscription count:\s*(\d+)",
            "Reliability": r"Reliability:\s*(\S+)",
            "Durability": r"Durability:\s*(\S+)",
            "History / depth": r"History \(Depth\):\s*([^\n]+)",
        }
        for label, pattern in patterns.items():
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                fields[label] = match.group(1).strip()
        nodes = re.findall(r"Node name:\s*([^\n]+)", text, re.IGNORECASE)
        if nodes:
            fields["Nodes"] = ", ".join(dict.fromkeys(item.strip() for item in nodes))
        return fields

    def _set_summary(self, fields: dict[str, object]):
        self.summary_table.setRowCount(len(fields))
        for row, (key, value) in enumerate(fields.items()):
            self.summary_table.setItem(row, 0, QTableWidgetItem(str(key)))
            self.summary_table.setItem(row, 1, QTableWidgetItem(str(value)))

    def _show_error(self, error: str):
        self.environment_status.setText("ERROR")
        self.output_text.setPlainText(self._bounded_text(str(error))[0])

    def _bounded_text(self, text: str) -> tuple[str, bool]:
        if len(text) <= self._MAX_OUTPUT_CHARS:
            return text, False
        return text[: self._MAX_OUTPUT_CHARS], True
