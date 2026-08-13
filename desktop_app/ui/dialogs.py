from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QComboBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from core.remote.ssh_manager import SSHConfig


class SSHConnectionDialog(QDialog):
    def __init__(self, current: SSHConfig | None = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Jetson Connection")
        self.setMinimumWidth(420)

        root = QVBoxLayout(self)
        form = QFormLayout()

        self.host_input = QLineEdit()
        self.host_input.setPlaceholderText("192.168.9.169")

        self.user_input = QLineEdit()
        self.user_input.setText("huu")

        self.port_input = QSpinBox()
        self.port_input.setRange(1, 65535)
        self.port_input.setValue(22)

        self.password_input = QLineEdit()
        self.password_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.password_input.setPlaceholderText("Leave empty when using an SSH key")

        if current:
            self.host_input.setText(current.host)
            self.user_input.setText(current.username)
            self.port_input.setValue(current.port)
            if current.password:
                self.password_input.setText(current.password)

        form.addRow("Jetson SSH IP:", self.host_input)
        form.addRow("Username:", self.user_input)
        form.addRow("Port:", self.port_input)
        form.addRow("Password:", self.password_input)
        root.addLayout(form)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Connect")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def get_config(self) -> SSHConfig:
        return SSHConfig(
            host=self.host_input.text().strip(),
            username=self.user_input.text().strip(),
            port=self.port_input.value(),
            password=self.password_input.text() or None,
        )


class TemporaryIPDialog(QDialog):
    def __init__(
        self,
        interfaces: list[str],
        candidate: str | None = None,
        parent=None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Configure Jetson LiDAR IP")
        self.setMinimumWidth(420)

        root = QVBoxLayout(self)
        form = QFormLayout()

        self.interface_combo = QComboBox()
        ordered = list(interfaces)
        if candidate in ordered:
            ordered.remove(candidate)
            ordered.insert(0, candidate)
        self.interface_combo.addItems(ordered)

        self.ip_input = QLineEdit()
        self.ip_input.setPlaceholderText("Example: 192.168.1.20")

        self.prefix_input = QSpinBox()
        self.prefix_input.setRange(1, 32)
        self.prefix_input.setValue(24)

        self.sudo_input = QLineEdit()
        self.sudo_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.sudo_input.setPlaceholderText(
            "Leave empty if sudo NOPASSWD is configured"
        )

        form.addRow("Interface:", self.interface_combo)
        form.addRow("Jetson LiDAR IP:", self.ip_input)
        form.addRow("Prefix:", self.prefix_input)
        form.addRow("sudo password:", self.sudo_input)
        root.addLayout(form)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText(
            "Apply Temporary IP"
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def values(self) -> dict:
        return {
            "interface": self.interface_combo.currentText(),
            "ip_address": self.ip_input.text().strip(),
            "prefix": self.prefix_input.value(),
            "sudo_password": self.sudo_input.text() or None,
        }


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
            "Device Family": "Livox LiDAR",
            "Selected Model": "-",
            "Detected Model": "-",
            "Serial Number": "-",
            "Firmware Version": "-",
            "SDK Version": "-",
            "Device Type": "-",
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
                "SSH Management Interface",
                data.get("management_interface", "-"),
                data.get("management_status", "-"),
            ),
            (
                "Jetson SSH IP",
                data.get("ssh_ip", "-"),
                data.get("ssh_status", "-"),
            ),
            (
                "LiDAR Interface",
                data.get("lidar_interface", "-"),
                data.get("interface_status", "-"),
            ),
            (
                "Jetson LiDAR IP",
                data.get("jetson_lidar_ip", "-"),
                data.get("subnet_status", "-"),
            ),
            (
                "LiDAR IP",
                data.get("lidar_ip", "-"),
                data.get("lidar_ip_status", "-"),
            ),
            (
                "Ping LiDAR",
                data.get("ping", "-"),
                data.get("ping_status", "NOT RUN"),
            ),
            (
                "Link Status",
                data.get("link", "-"),
                data.get("link_status", "-"),
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
            "Jetson LiDAR Host IP": "-",
            "LiDAR Control Port": "56100",
            "LiDAR Push Message Port": "56200",
            "LiDAR Point Data Port": "56300",
            "LiDAR IMU Data Port": "56400",
            "LiDAR Log Data Port": "56500",
            "Host Control Port": "56101",
            "Host Push Message Port": "56201",
            "Host Point Data Port": "56301",
            "Host IMU Data Port": "56401",
            "Host Log Data Port": "56501",
            "Gateway Address": "-",
            "Subnet Mask": "-",
            "Firmware Version": "-",
            "Work Mode": "-",
            "Scan Pattern": "-",
            "Data Type": "-",
            "Time Sync Type": "-",
            "FOV Enable": "-",
        }
        data.update(protocol_data or {})

        super().__init__(
            title="Network & Protocol",
            columns=["Parameter", "Value"],
            rows=[(key, value) for key, value in data.items()],
            parent=parent,
        )
