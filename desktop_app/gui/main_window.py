
import json
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from connection.ssh_manager import SSHManager


BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = BASE_DIR / "config" / "devices.json"
STYLE_PATH = Path(__file__).resolve().parent / "styles.qss"


class SSHConnectThread(QThread):
    success = Signal(str)
    failed = Signal(str)

    def __init__(self, host, port, username, password):
        super().__init__()
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.manager = None

    def run(self):
        try:
            manager = SSHManager()
            manager.connect(
                host=self.host,
                port=self.port,
                username=self.username,
                password=self.password,
            )
            result = manager.execute("hostname && whoami && uname -a")
            if result["exit_code"] != 0:
                manager.disconnect()
                raise RuntimeError(result["stderr"] or "Remote command failed.")

            self.manager = manager
            self.success.emit(result["stdout"].strip())
        except Exception as exc:
            self.failed.emit(str(exc))


class CommandThread(QThread):
    success = Signal(str)
    failed = Signal(str)

    def __init__(self, manager, command):
        super().__init__()
        self.manager = manager
        self.command = command

    def run(self):
        try:
            result = self.manager.execute(self.command)
            text = (
                f"$ {result['command']}\n"
                f"exit_code={result['exit_code']}\n\n"
                f"{result['stdout']}"
            )
            if result["stderr"]:
                text += f"\n[stderr]\n{result['stderr']}"
            self.success.emit(text)
        except Exception as exc:
            self.failed.emit(str(exc))


def make_card(title, value, status=None):
    card = QFrame()
    card.setObjectName("Card")
    layout = QVBoxLayout(card)
    layout.setContentsMargins(16, 14, 16, 14)
    layout.setSpacing(6)

    title_label = QLabel(title)
    title_label.setObjectName("CardTitle")
    layout.addWidget(title_label)

    value_label = QLabel(value)
    value_label.setObjectName("CardValue")
    layout.addWidget(value_label)

    status_label = None
    if status is not None:
        status_label = QLabel(status)
        status_label.setObjectName(
            "StatusConnected" if "Connected" in status or "Online" in status else "StatusDisconnected"
        )
        layout.addWidget(status_label)

    return card, value_label, status_label


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Hardware Test Automation")
        self.resize(1180, 760)
        self.setMinimumSize(1000, 650)

        self.ssh = None
        self.connect_thread = None
        self.command_thread = None

        self.host = ""
        self.username = ""
        self.port = 22
        self._load_config()

        if STYLE_PATH.exists():
            self.setStyleSheet(STYLE_PATH.read_text(encoding="utf-8"))

        self.pages = QStackedWidget()
        self.nav_buttons = {}

        self.dashboard_page = self._build_dashboard()
        self.connection_page = self._build_connection_page()
        self.camera_page = self._build_test_page(
            "Camera Tests",
            "Run automated validation cases for ZED cameras.",
            [
                ("CAM_001", "Camera Detection", "Detect connected ZED camera"),
                ("CAM_002", "Camera Open", "Open camera with ZED SDK"),
                ("CAM_003", "Camera Streaming", "Validate camera stream"),
            ],
        )
        self.lidar_page = self._build_test_page(
            "LiDAR Tests",
            "Run automated validation cases for Livox LiDAR.",
            [
                ("LIDAR_001", "Device Detection", "Detect connected LiDAR"),
                ("LIDAR_002", "Data Stream", "Validate LiDAR data stream"),
                ("LIDAR_003", "Health Check", "Validate device health"),
            ],
        )
        self.history_page = self._build_history_page()
        self.config_page = self._build_config_page()

        for p in [
            self.dashboard_page,
            self.connection_page,
            self.camera_page,
            self.lidar_page,
            self.history_page,
            self.config_page,
        ]:
            self.pages.addWidget(p)

        root = QWidget()
        root.setObjectName("Root")
        outer = QVBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        outer.addWidget(self._build_topbar())

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        body.addWidget(self._build_sidebar())
        body.addWidget(self.pages, 1)

        body_container = QWidget()
        body_container.setLayout(body)
        outer.addWidget(body_container, 1)

        self.setCentralWidget(root)

        self.nav_buttons["Dashboard"].setChecked(True)
        self.pages.setCurrentIndex(0)
        self._refresh_connection_ui()

    def _load_config(self):
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            ssh = data.get("jetson", {}).get("ssh", {})
            self.host = str(ssh.get("host", ""))
            self.username = str(ssh.get("username", ""))
            self.port = int(ssh.get("port", 22))
        except Exception:
            pass

    def _build_topbar(self):
        bar = QWidget()
        bar.setObjectName("TopBar")
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(20, 12, 20, 12)

        brand = QVBoxLayout()
        brand.setSpacing(1)
        title = QLabel("HARDWARE TEST APPLICATION")
        title.setObjectName("BrandTitle")
        subtitle = QLabel("Camera & LiDAR Validation Platform")
        subtitle.setObjectName("BrandSubtitle")
        brand.addWidget(title)
        brand.addWidget(subtitle)

        layout.addLayout(brand)
        layout.addStretch()

        self.jetson_pill = QLabel("● Jetson DISCONNECTED")
        self.jetson_pill.setObjectName("JetsonPill")
        layout.addWidget(self.jetson_pill)

        return bar

    def _build_sidebar(self):
        side = QWidget()
        side.setObjectName("Sidebar")
        side.setFixedWidth(215)

        layout = QVBoxLayout(side)
        layout.setContentsMargins(12, 18, 12, 16)
        layout.setSpacing(6)

        items = [
            ("Dashboard", 0),
            ("Jetson Connection", 1),
            ("Camera Tests", 2),
            ("LiDAR Tests", 3),
            ("Test History", 4),
            ("Configuration", 5),
        ]

        for text, idx in items:
            btn = QPushButton(text)
            btn.setObjectName("NavButton")
            btn.setCheckable(True)
            btn.clicked.connect(lambda checked=False, i=idx, t=text: self._navigate(i, t))
            layout.addWidget(btn)
            self.nav_buttons[text] = btn

        layout.addStretch()

        version = QLabel("v0.1.1 Desktop")
        version.setStyleSheet("color:#90a4ae; font-size:11px; padding:8px;")
        layout.addWidget(version)

        return side

    def _navigate(self, index, name):
        self.pages.setCurrentIndex(index)
        for text, btn in self.nav_buttons.items():
            btn.setChecked(text == name)

    def _page_shell(self, title, subtitle):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(24, 20, 24, 22)
        layout.setSpacing(14)

        title_label = QLabel(title)
        title_label.setObjectName("PageTitle")
        subtitle_label = QLabel(subtitle)
        subtitle_label.setObjectName("PageSubtitle")

        layout.addWidget(title_label)
        layout.addWidget(subtitle_label)
        return page, layout

    def _build_dashboard(self):
        page, layout = self._page_shell(
            "Dashboard",
            "Overview of Jetson, Camera and LiDAR status.",
        )

        cards = QGridLayout()
        cards.setHorizontalSpacing(14)
        cards.setVerticalSpacing(14)

        self.ssh_card, _, self.ssh_status = make_card(
            "JETSON / SSH", f"{self.username}@{self.host}", "● Disconnected"
        )
        self.camera_card, _, self.camera_status = make_card(
            "CAMERA", "ZED Camera", "● Not checked"
        )
        self.lidar_card, _, self.lidar_status = make_card(
            "LIDAR", "Livox LiDAR", "● Not checked"
        )

        cards.addWidget(self.ssh_card, 0, 0)
        cards.addWidget(self.camera_card, 0, 1)
        cards.addWidget(self.lidar_card, 0, 2)

        layout.addLayout(cards)

        action_card = QFrame()
        action_card.setObjectName("Card")
        action_layout = QVBoxLayout(action_card)
        action_layout.setContentsMargins(16, 14, 16, 14)

        action_title = QLabel("QUICK ACTIONS")
        action_title.setObjectName("CardTitle")
        action_layout.addWidget(action_title)

        row = QHBoxLayout()
        connect_btn = QPushButton("Open Jetson Connection")
        connect_btn.setObjectName("PrimaryButton")
        connect_btn.clicked.connect(lambda: self._navigate(1, "Jetson Connection"))

        cam_btn = QPushButton("Camera Tests")
        cam_btn.setObjectName("SecondaryButton")
        cam_btn.clicked.connect(lambda: self._navigate(2, "Camera Tests"))

        lidar_btn = QPushButton("LiDAR Tests")
        lidar_btn.setObjectName("SecondaryButton")
        lidar_btn.clicked.connect(lambda: self._navigate(3, "LiDAR Tests"))

        row.addWidget(connect_btn)
        row.addWidget(cam_btn)
        row.addWidget(lidar_btn)
        row.addStretch()
        action_layout.addLayout(row)

        layout.addWidget(action_card)

        log_card = QFrame()
        log_card.setObjectName("Card")
        log_layout = QVBoxLayout(log_card)
        log_layout.setContentsMargins(14, 12, 14, 14)

        log_title = QLabel("LIVE LOG")
        log_title.setObjectName("CardTitle")
        log_layout.addWidget(log_title)

        self.global_log = QPlainTextEdit()
        self.global_log.setObjectName("Terminal")
        self.global_log.setReadOnly(True)
        self.global_log.setMinimumHeight(235)
        self.global_log.setPlainText(
            "[APP] Hardware Test Application started.\n"
            "[INFO] Connect to Jetson to begin hardware validation."
        )
        log_layout.addWidget(self.global_log)

        layout.addWidget(log_card, 1)
        return page

    def _build_connection_page(self):
        page, layout = self._page_shell(
            "Jetson Connection",
            "Configure and verify SSH connection to the Jetson target.",
        )

        content = QHBoxLayout()
        content.setSpacing(14)

        config_card = QFrame()
        config_card.setObjectName("Card")
        config_layout = QVBoxLayout(config_card)
        config_layout.setContentsMargins(18, 16, 18, 18)
        config_layout.setSpacing(10)

        label = QLabel("SSH CONFIGURATION")
        label.setObjectName("CardTitle")
        config_layout.addWidget(label)

        config_layout.addWidget(QLabel("Jetson IP / Host"))
        self.host_edit = QLineEdit(self.host)
        config_layout.addWidget(self.host_edit)

        config_layout.addWidget(QLabel("SSH Port"))
        self.port_spin = QSpinBox()
        self.port_spin.setRange(1, 65535)
        self.port_spin.setValue(self.port)
        config_layout.addWidget(self.port_spin)

        config_layout.addWidget(QLabel("Username"))
        self.user_edit = QLineEdit(self.username)
        config_layout.addWidget(self.user_edit)

        config_layout.addWidget(QLabel("Password (optional when SSH key is configured)"))
        self.password_edit = QLineEdit()
        self.password_edit.setEchoMode(QLineEdit.Password)
        config_layout.addWidget(self.password_edit)

        buttons = QHBoxLayout()
        self.connect_button = QPushButton("CONNECT")
        self.connect_button.setObjectName("PrimaryButton")
        self.connect_button.clicked.connect(self.connect_ssh)

        self.disconnect_button = QPushButton("DISCONNECT")
        self.disconnect_button.setObjectName("DangerButton")
        self.disconnect_button.clicked.connect(self.disconnect_ssh)

        buttons.addWidget(self.connect_button)
        buttons.addWidget(self.disconnect_button)
        config_layout.addLayout(buttons)
        config_layout.addStretch()

        content.addWidget(config_card, 1)

        info_card = QFrame()
        info_card.setObjectName("Card")
        info_layout = QVBoxLayout(info_card)
        info_layout.setContentsMargins(18, 16, 18, 18)

        info_title = QLabel("DEVICE INFORMATION")
        info_title.setObjectName("CardTitle")
        info_layout.addWidget(info_title)

        self.connection_big_status = QLabel("● DISCONNECTED")
        self.connection_big_status.setObjectName("StatusDisconnected")
        self.connection_big_status.setStyleSheet("font-size:18px; font-weight:700;")
        info_layout.addWidget(self.connection_big_status)

        self.remote_info = QLabel(
            "Host: -\n"
            "User: -\n"
            "System: -"
        )
        self.remote_info.setWordWrap(True)
        self.remote_info.setStyleSheet("line-height:1.5; color:#546e7a;")
        info_layout.addWidget(self.remote_info)

        self.test_button = QPushButton("RUN SSH TEST")
        self.test_button.setObjectName("SecondaryButton")
        self.test_button.clicked.connect(self.run_ssh_test)
        info_layout.addWidget(self.test_button)
        info_layout.addStretch()

        content.addWidget(info_card, 1)
        layout.addLayout(content)

        terminal_card = QFrame()
        terminal_card.setObjectName("Card")
        terminal_layout = QVBoxLayout(terminal_card)
        terminal_layout.setContentsMargins(14, 12, 14, 14)

        terminal_title = QLabel("SSH CONSOLE")
        terminal_title.setObjectName("CardTitle")
        terminal_layout.addWidget(terminal_title)

        self.connection_log = QPlainTextEdit()
        self.connection_log.setObjectName("Terminal")
        self.connection_log.setReadOnly(True)
        self.connection_log.setMinimumHeight(245)
        self.connection_log.setPlainText("[SSH] Ready.")
        terminal_layout.addWidget(self.connection_log)

        layout.addWidget(terminal_card, 1)
        return page

    def _build_test_page(self, title, subtitle, cases):
        page, layout = self._page_shell(title, subtitle)

        table = QTableWidget(len(cases), 5)
        table.setHorizontalHeaderLabels(["Select", "Test ID", "Test Name", "Description", "Result"])
        table.verticalHeader().setVisible(False)
        table.setAlternatingRowColors(False)
        table.setColumnWidth(0, 65)
        table.setColumnWidth(1, 110)
        table.setColumnWidth(2, 180)
        table.setColumnWidth(3, 390)
        table.setColumnWidth(4, 100)

        for row, (test_id, name, desc) in enumerate(cases):
            check = QCheckBox()
            check.setChecked(True)
            wrap = QWidget()
            wl = QHBoxLayout(wrap)
            wl.setContentsMargins(0, 0, 0, 0)
            wl.setAlignment(Qt.AlignCenter)
            wl.addWidget(check)
            table.setCellWidget(row, 0, wrap)
            table.setItem(row, 1, QTableWidgetItem(test_id))
            table.setItem(row, 2, QTableWidgetItem(name))
            table.setItem(row, 3, QTableWidgetItem(desc))
            result = QTableWidgetItem("NOT RUN")
            result.setTextAlignment(Qt.AlignCenter)
            table.setItem(row, 4, result)

        layout.addWidget(table)

        buttons = QHBoxLayout()
        run = QPushButton("RUN SELECTED")
        run.setObjectName("PrimaryButton")
        run.clicked.connect(
            lambda: self._append_global(
                f"[TEST] {title}: test runner UI is ready; automation engine will be connected next."
            )
        )

        stop = QPushButton("STOP")
        stop.setObjectName("DangerButton")

        buttons.addWidget(run)
        buttons.addWidget(stop)
        buttons.addStretch()
        layout.addLayout(buttons)

        result_card = QFrame()
        result_card.setObjectName("Card")
        rl = QVBoxLayout(result_card)
        rl.setContentsMargins(14, 12, 14, 14)

        rt = QLabel("RESULT / LIVE LOG")
        rt.setObjectName("CardTitle")
        rl.addWidget(rt)

        log = QPlainTextEdit()
        log.setObjectName("Terminal")
        log.setReadOnly(True)
        log.setMinimumHeight(180)
        log.setPlainText(
            "[TEST] Select test cases and press RUN SELECTED.\n"
            "[INFO] PASS / FAIL / TIMEOUT / ERROR will be displayed here."
        )
        rl.addWidget(log)

        layout.addWidget(result_card, 1)
        return page

    def _build_history_page(self):
        page, layout = self._page_shell(
            "Test History",
            "Review previous Camera and LiDAR test executions.",
        )

        table = QTableWidget(0, 6)
        table.setHorizontalHeaderLabels(
            ["Date", "Run ID", "Test ID", "Device", "Result", "Duration"]
        )
        table.verticalHeader().setVisible(False)
        layout.addWidget(table, 1)

        note = QLabel("History storage will be connected to SQLite in the next implementation step.")
        note.setObjectName("PageSubtitle")
        layout.addWidget(note)
        return page

    def _build_config_page(self):
        page, layout = self._page_shell(
            "Configuration",
            "Application and device configuration.",
        )

        card = QFrame()
        card.setObjectName("Card")
        cl = QVBoxLayout(card)
        cl.setContentsMargins(18, 16, 18, 18)

        t = QLabel("CURRENT CONFIGURATION")
        t.setObjectName("CardTitle")
        cl.addWidget(t)

        cl.addWidget(QLabel(f"Config file: {CONFIG_PATH}"))
        cl.addWidget(QLabel(f"Jetson: {self.username}@{self.host}:{self.port}"))
        cl.addWidget(QLabel("Scope v0.1.x: Camera + LiDAR"))
        cl.addStretch()

        layout.addWidget(card, 1)
        return page

    def _append_global(self, text):
        self.global_log.appendPlainText(text)

    def _append_connection(self, text):
        self.connection_log.appendPlainText(text)
        self._append_global(text)

    def _refresh_connection_ui(self):
        connected = self.ssh is not None

        if connected:
            self.jetson_pill.setText("● Jetson CONNECTED")
            self.connection_big_status.setText("● CONNECTED")
            self.connection_big_status.setObjectName("StatusConnected")
            self.ssh_status.setText("● Connected")
            self.ssh_status.setObjectName("StatusConnected")
        else:
            self.jetson_pill.setText("● Jetson DISCONNECTED")
            self.connection_big_status.setText("● DISCONNECTED")
            self.connection_big_status.setObjectName("StatusDisconnected")
            self.ssh_status.setText("● Disconnected")
            self.ssh_status.setObjectName("StatusDisconnected")

        for widget in [self.connection_big_status, self.ssh_status]:
            widget.style().unpolish(widget)
            widget.style().polish(widget)

        self.connect_button.setEnabled(not connected)
        self.disconnect_button.setEnabled(connected)
        self.test_button.setEnabled(connected)

    def connect_ssh(self):
        host = self.host_edit.text().strip()
        username = self.user_edit.text().strip()
        password = self.password_edit.text()
        port = self.port_spin.value()

        if not host or not username:
            QMessageBox.warning(self, "Missing data", "Host and username are required.")
            return

        self._append_connection(f"[SSH] Connecting to {username}@{host}:{port} ...")
        self.connect_button.setEnabled(False)

        self.connect_thread = SSHConnectThread(host, port, username, password)
        self.connect_thread.success.connect(self._on_connect_success)
        self.connect_thread.failed.connect(self._on_connect_failed)
        self.connect_thread.start()

    def _on_connect_success(self, remote_info):
        self.ssh = self.connect_thread.manager
        lines = remote_info.splitlines()
        host = lines[0] if len(lines) > 0 else "-"
        user = lines[1] if len(lines) > 1 else "-"
        system = lines[2] if len(lines) > 2 else "-"

        self.remote_info.setText(
            f"Host: {host}\n"
            f"User: {user}\n"
            f"System: {system}"
        )
        self._append_connection("[SSH] Connected successfully.")
        self._append_connection(remote_info)
        self._refresh_connection_ui()

    def _on_connect_failed(self, error):
        self._append_connection(f"[SSH][ERROR] {error}")
        self._refresh_connection_ui()

    def disconnect_ssh(self):
        if self.ssh:
            self.ssh.disconnect()
            self.ssh = None

        self.remote_info.setText("Host: -\nUser: -\nSystem: -")
        self._append_connection("[SSH] Disconnected.")
        self._refresh_connection_ui()

    def run_ssh_test(self):
        if not self.ssh:
            return

        self.test_button.setEnabled(False)
        self._append_connection("[SSH] Running remote test command ...")

        cmd = (
            "echo '=== HOST ==='; hostname; "
            "echo '=== USER ==='; whoami; "
            "echo '=== UPTIME ==='; uptime"
        )

        self.command_thread = CommandThread(self.ssh, cmd)
        self.command_thread.success.connect(self._on_command_success)
        self.command_thread.failed.connect(self._on_command_failed)
        self.command_thread.start()

    def _on_command_success(self, text):
        self._append_connection(text)
        self.test_button.setEnabled(True)

    def _on_command_failed(self, error):
        self._append_connection(f"[COMMAND][ERROR] {error}")
        self.test_button.setEnabled(True)

    def closeEvent(self, event):
        if self.ssh:
            self.ssh.disconnect()
        event.accept()
