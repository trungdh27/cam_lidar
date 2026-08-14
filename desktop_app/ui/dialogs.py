import json

from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)


class _BaseInfoDialog(QDialog):
    def __init__(
        self,
        title: str,
        columns: list[str],
        rows: list[tuple],
        parent=None,
    ):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(760, 520)

        root = QVBoxLayout(self)

        title_label = QLabel(title)
        title_label.setStyleSheet("font-size:17px; font-weight:700;")
        root.addWidget(title_label)

        self.table = QTableWidget(len(rows), len(columns))
        self.table.setHorizontalHeaderLabels(columns)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.table.setSelectionMode(
            QAbstractItemView.SelectionMode.NoSelection
        )
        self.table.setAlternatingRowColors(True)

        header = self.table.horizontalHeader()
        for index in range(len(columns)):
            mode = (
                QHeaderView.ResizeMode.Stretch
                if index < len(columns) - 1
                else QHeaderView.ResizeMode.ResizeToContents
            )
            header.setSectionResizeMode(index, mode)

        for row_index, row_values in enumerate(rows):
            for column_index, value in enumerate(row_values):
                self.table.setItem(
                    row_index,
                    column_index,
                    QTableWidgetItem(str(value)),
                )

        root.addWidget(self.table)

        close_button = QPushButton("Close")
        close_button.setObjectName("SmallButton")
        close_button.clicked.connect(self.close)
        root.addWidget(close_button)


class DeviceOverviewDialog(_BaseInfoDialog):
    def __init__(self, device_data: dict | None = None, parent=None):
        data = {
            "Vendor": "Livox",
            "Selected Model": "-",
            "Detected Model": "-",
            "Serial": "-",
            "LiDAR IP": "-",
            "SDK Version": "-",
            "Status": "-",
        }
        data.update(device_data or {})

        super().__init__(
            title="Device Overview",
            columns=["Parameter", "Value"],
            rows=[(key, value) for key, value in data.items()],
            parent=parent,
        )


class NetworkSummaryDialog(_BaseInfoDialog):
    def __init__(self, network_data: dict | None = None, parent=None):
        data = network_data or {}

        rows = [
            (
                "Interface",
                data.get("interface", "-"),
                "PHYSICAL" if data.get("physical") else "NOT PHYSICAL",
            ),
            (
                "MAC",
                data.get("mac_address", "-"),
                "OBSERVED",
            ),
            (
                "State",
                data.get("state", "-"),
                "UP / LOWER_UP REQUIRED",
            ),
            (
                "Carrier",
                data.get("carrier", "-"),
                "UP REQUIRED",
            ),
            (
                "Jetson IP",
                data.get("jetson_ip", "-"),
                f"EXPECTED {data.get('expected_jetson_cidr', '-')}",
            ),
            (
                "LiDAR IP",
                data.get("lidar_ip", "-"),
                "READ-ONLY",
            ),
            (
                "Network",
                data.get("network", "-"),
                "EXPECTED",
            ),
            (
                "Gateway",
                data.get("gateway", "None / Not Required"),
                "NOT REQUIRED",
            ),
            (
                "Ping",
                data.get("ping", "Not run"),
                data.get("ping_status", "NOT RUN"),
            ),
            (
                "Link",
                data.get("link", "-"),
                "PASS" if data.get("link_ready") else "FAIL",
            ),
            (
                "Verification",
                data.get("verification_status", "NOT_VERIFIED"),
                "PASS"
                if data.get("verification_status") == "NETWORK_READY"
                else "FAIL",
            ),
            (
                "Status",
                data.get("status", "NOT_VERIFIED"),
                "PASS"
                if data.get("status")
                in {"NETWORK READY", "LIDAR REACHABLE", "READY"}
                else "FAIL",
            ),
        ]

        super().__init__(
            title="Network Summary",
            columns=["Parameter", "Value", "Status"],
            rows=rows,
            parent=parent,
        )


class NetworkProtocolDialog(_BaseInfoDialog):
    def __init__(self, protocol_data: dict | None = None, parent=None):
        data = {
            "LiDAR IP": "-",
            "LiDAR IP Access": "-",
            "Expected Serial": "-",
            "Jetson LiDAR Interface": "-",
            "Jetson LiDAR Host IP": "-",
            "Discovery Port": "-",
            "LiDAR Control Port": "-",
            "LiDAR Push Message Port": "-",
            "LiDAR Point Data Port": "-",
            "LiDAR IMU Data Port": "-",
            "LiDAR Log Data Port": "-",
            "Host Control Port": "-",
            "Host Push Message Port": "-",
            "Host Point Data Port": "-",
            "Host IMU Data Port": "-",
            "Host Log Data Port": "-",
            "Gateway Address": "-",
        }
        data.update(protocol_data or {})

        super().__init__(
            title="Network & Protocol",
            columns=["Parameter", "Value"],
            rows=[(key, value) for key, value in data.items()],
            parent=parent,
        )


class LidarDeviceInformationDialog(QDialog):
    """Present LiDAR identity, network, and protocol data in one dialog."""

    def __init__(
        self,
        device_data: dict | None = None,
        network_data: dict | None = None,
        protocol_data: dict | None = None,
        parent=None,
    ):
        super().__init__(parent)
        self.setWindowTitle("LiDAR Device Information")
        self.resize(780, 570)

        root = QVBoxLayout(self)
        title_label = QLabel("LiDAR Device Information")
        title_label.setStyleSheet("font-size:17px; font-weight:700;")
        root.addWidget(title_label)

        self.tabs = QTabWidget()
        self.device_table = self._add_tab(
            "Device", self._device_rows(device_data or {})
        )
        self.network_table = self._add_tab(
            "Network", self._network_rows(network_data or {})
        )
        self.protocol_table = self._add_tab(
            "Protocol",
            [(key, value) for key, value in (protocol_data or {}).items()],
        )
        root.addWidget(self.tabs)

        close_button = QPushButton("Close")
        close_button.setObjectName("SmallButton")
        close_button.clicked.connect(self.close)
        root.addWidget(close_button)

    def _add_tab(self, title: str, rows: list[tuple]) -> QTableWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        table = QTableWidget(len(rows), 2)
        table.setHorizontalHeaderLabels(["Parameter", "Value"])
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        table.setAlternatingRowColors(True)
        table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.ResizeToContents
        )
        table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Stretch
        )
        for row_index, row_values in enumerate(rows):
            for column_index, value in enumerate(row_values):
                table.setItem(
                    row_index,
                    column_index,
                    QTableWidgetItem(str(value)),
                )
        layout.addWidget(table)
        self.tabs.addTab(page, title)
        return table

    @staticmethod
    def _device_rows(data: dict) -> list[tuple]:
        ordered_keys = (
            "Vendor",
            "Selected Model",
            "Detected Model",
            "Serial",
            "LiDAR IP",
            "Jetson Host IP",
            "SDK Version",
            "Status",
            "Model Check",
            "Serial Check",
            "LiDAR IP Check",
            "Device Type",
        )
        return [(key, data.get(key, "-")) for key in ordered_keys]

    @staticmethod
    def _network_rows(data: dict) -> list[tuple]:
        return [
            ("Jetson LiDAR Interface", data.get("interface", "-")),
            ("MAC", data.get("mac_address", "-")),
            ("State", data.get("state", "-")),
            ("Carrier", data.get("carrier", "-")),
            ("Jetson Host IP", data.get("jetson_ip", "-")),
            ("LiDAR IP", data.get("lidar_ip", "-")),
            ("Network", data.get("network", "-")),
            ("Gateway", data.get("gateway", "None / Not Required")),
            (
                "Ping",
                f"{data.get('ping', 'Not run')} "
                f"({data.get('ping_status', 'NOT RUN')})",
            ),
            (
                "Network Verification",
                data.get("verification_status", "NOT_VERIFIED"),
            ),
        ]


class TestDetailsDialog(_BaseInfoDialog):
    def __init__(self, definition, result=None, parent=None):
        result_data = result.to_dict() if result is not None else {}
        rows = [
            ("ID", definition.id),
            ("Name", definition.name),
            ("Group", definition.group),
            ("Description", definition.description),
            ("Automation", definition.automation_level.value),
            ("Priority", definition.priority),
            ("Timeout", f"{definition.timeout_sec:.1f} s"),
            (
                "Preconditions",
                ", ".join(definition.prerequisites) or "None",
            ),
            ("Last Result", result_data.get("status", "NOT_RUN")),
            (
                "Duration",
                f"{result_data.get('duration_sec'):.3f} s"
                if result_data.get("duration_sec") is not None
                else "-",
            ),
            ("Actual Result", result_data.get("actual_result", "-")),
            (
                "Measurements",
                json.dumps(
                    result_data.get("measurements", {}),
                    sort_keys=True,
                ),
            ),
            ("Error", result_data.get("error") or "-"),
            (
                "Evidence",
                "\n".join(result_data.get("evidence", [])) or "-",
            ),
        ]
        super().__init__(
            title=f"Test Details — {definition.id}",
            columns=["Parameter", "Value"],
            rows=rows,
            parent=parent,
        )
