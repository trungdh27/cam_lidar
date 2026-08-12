from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QGroupBox,
    QFormLayout,
    QLineEdit,
    QSpinBox,
    QTextEdit,
)

from desktop_app.workers.ssh_probe_worker import SSHProbeWorker


class LidarPage(QWidget):
    def __init__(self):
        super().__init__()

        self.selected_model = None
        self.worker = None

        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)

        title = QLabel("LiDAR Tests")
        title.setStyleSheet(
            "font-size: 24px; font-weight: 700;"
        )

        root.addWidget(title)

        subtitle = QLabel(
            "Select a Livox LiDAR model to start testing."
        )
        root.addWidget(subtitle)

        # --------------------------------------------------
        # Device selector
        # --------------------------------------------------

        device_layout = QHBoxLayout()

        self.mid360_button = QPushButton("Livox MID360")
        self.mid360s_button = QPushButton("Livox MID360S")

        self.mid360_button.setMinimumHeight(100)
        self.mid360s_button.setMinimumHeight(100)

        self.mid360_button.clicked.connect(
            lambda: self.select_model("MID360")
        )

        self.mid360s_button.clicked.connect(
            lambda: self.select_model("MID360S")
        )

        device_layout.addWidget(self.mid360_button)
        device_layout.addWidget(self.mid360s_button)

        root.addLayout(device_layout)

        # --------------------------------------------------
        # Selected model
        # --------------------------------------------------

        self.model_label = QLabel("Selected device: None")
        self.model_label.setStyleSheet(
            "font-weight: 600; margin-top: 12px;"
        )

        root.addWidget(self.model_label)

        # --------------------------------------------------
        # SSH group
        # --------------------------------------------------

        ssh_group = QGroupBox("Jetson SSH")

        form = QFormLayout(ssh_group)

        self.host_input = QLineEdit()
        self.host_input.setPlaceholderText(
            "Example: 192.168.9.169"
        )

        self.username_input = QLineEdit()
        self.username_input.setText("huu")

        self.port_input = QSpinBox()
        self.port_input.setRange(1, 65535)
        self.port_input.setValue(22)

        self.password_input = QLineEdit()
        self.password_input.setEchoMode(
            QLineEdit.Password
        )
        self.password_input.setPlaceholderText(
            "Leave empty when using SSH key"
        )

        form.addRow("Jetson IP:", self.host_input)
        form.addRow("Username:", self.username_input)
        form.addRow("Port:", self.port_input)
        form.addRow("Password:", self.password_input)

        root.addWidget(ssh_group)

        # --------------------------------------------------
        # Connect
        # --------------------------------------------------

        self.connect_button = QPushButton(
            "Connect Jetson"
        )

        self.connect_button.setMinimumHeight(40)
        self.connect_button.clicked.connect(
            self.connect_jetson
        )

        root.addWidget(self.connect_button)

        self.status_label = QLabel(
            "SSH Status: DISCONNECTED"
        )

        root.addWidget(self.status_label)

        # --------------------------------------------------
        # Result
        # --------------------------------------------------

        self.output = QTextEdit()
        self.output.setReadOnly(True)
        self.output.setPlaceholderText(
            "Jetson information will appear here..."
        )

        root.addWidget(self.output)

    def select_model(self, model: str):
        self.selected_model = model

        self.model_label.setText(
            f"Selected device: Livox {model}"
        )

    def connect_jetson(self):
        if self.selected_model is None:
            self.status_label.setText(
                "Select MID360 or MID360S first."
            )
            return

        host = self.host_input.text().strip()
        username = self.username_input.text().strip()
        password = self.password_input.text()
        port = self.port_input.value()

        if not host:
            self.status_label.setText(
                "Jetson IP is required."
            )
            return

        if not username:
            self.status_label.setText(
                "SSH username is required."
            )
            return

        self.connect_button.setEnabled(False)

        self.status_label.setText(
            "SSH Status: CONNECTING..."
        )

        self.output.clear()

        self.worker = SSHProbeWorker(
            host=host,
            username=username,
            password=password or None,
            port=port,
        )

        self.worker.success.connect(
            self.on_ssh_success
        )

        self.worker.failed.connect(
            self.on_ssh_failed
        )

        self.worker.finished.connect(
            lambda: self.connect_button.setEnabled(True)
        )

        self.worker.start()

    def on_ssh_success(self, info: dict):
        self.status_label.setText(
            "SSH Status: CONNECTED"
        )

        text = (
            f"Device model: Livox {self.selected_model}\n\n"
            f"Jetson hostname: {info['hostname']}\n"
            f"Architecture: {info['architecture']}\n"
            f"Kernel: {info['kernel']}\n\n"
            "Network interfaces:\n"
            "-----------------------------------\n"
            f"{info['network']}"
        )

        self.output.setPlainText(text)

    def on_ssh_failed(self, error: str):
        self.status_label.setText(
            "SSH Status: ERROR"
        )

        self.output.setPlainText(
            f"SSH connection failed:\n\n{error}"
        )
