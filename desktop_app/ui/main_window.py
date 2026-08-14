from PySide6.QtWidgets import (
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from desktop_app.services.jetson_connection_service import (
    JetsonConnectionService,
)
from desktop_app.services.lidar_discovery_service import (
    LidarDiscoveryService,
)
from desktop_app.services.lidar_stream_service import LidarStreamService
from desktop_app.state.device_registry import DeviceRegistry
from desktop_app.state.jetson_state import JetsonState
from desktop_app.state.lidar_runtime_state import LidarRuntimeState
from desktop_app.ui.camera_page import CameraPage
from desktop_app.ui.dashboard_page import DashboardPage
from desktop_app.ui.lidar_page import LidarPage
from desktop_app.testing.lidar_tests import build_lidar_test_registry
from desktop_app.testing.test_execution_service import TestExecutionService
from devices.livox.profile import load_default_livox_profile


class PlaceholderPage(QWidget):
    def __init__(self, title: str, subtitle: str = "", parent=None):
        super().__init__(parent)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)

        title_label = QLabel(title)
        title_label.setObjectName("PageTitle")

        subtitle_label = QLabel(subtitle or "Coming soon")
        subtitle_label.setObjectName("Muted")

        layout.addWidget(title_label)
        layout.addWidget(subtitle_label)
        layout.addStretch()


class MainWindow(QMainWindow):
    NAV_ITEMS = [
        ("◉", "Dashboard"),
        ("▦", "Devices"),
        ("▣", "Camera"),
        ("◌", "LiDAR"),
        ("⌘", "IMU"),
        ("▤", "CAN"),
        ("⌘", "EtherCAT"),
        ("▷", "Test Runner"),
        ("◔", "History"),
        ("□", "Evidence"),
        ("▤", "Reports"),
        ("⚙", "Settings"),
    ]

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Hardware Test Automation")
        self.resize(1500, 920)
        self.setMinimumSize(1180, 720)
        self.jetson_state = JetsonState(self)
        self.device_registry = DeviceRegistry(self)
        self.jetson_service = JetsonConnectionService(
            self.jetson_state,
            self,
        )
        self.livox_network_profile = load_default_livox_profile()
        self.lidar_runtime_state = LidarRuntimeState(self)
        self.lidar_stream_service = LidarStreamService(
            self.jetson_service,
            self.lidar_runtime_state,
            self.livox_network_profile,
            self,
        )
        self.lidar_discovery_service = LidarDiscoveryService(
            self.jetson_service,
            self.livox_network_profile,
            self,
        )
        self.test_registry = build_lidar_test_registry(
            self.livox_network_profile
        )
        self.test_execution_service = TestExecutionService(
            self.test_registry,
            parent=self,
        )
        self._build_ui()

    def _build_ui(self):
        root = QWidget()
        root.setObjectName("AppRoot")

        layout = QHBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        sidebar = QFrame()
        sidebar.setObjectName("Sidebar")
        sidebar.setFixedWidth(180)

        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(10, 18, 10, 14)
        sidebar_layout.setSpacing(5)

        brand = QLabel("⬡  Hardware Test")
        brand.setObjectName("Brand")
        sidebar_layout.addWidget(brand)

        self.nav_group = QButtonGroup(self)
        self.nav_group.setExclusive(True)
        self.nav_buttons = []

        for index, (icon, title) in enumerate(self.NAV_ITEMS):
            button = QPushButton(f"{icon}   {title}")
            button.setObjectName("NavButton")
            button.setCheckable(True)
            button.setMinimumHeight(40)

            self.nav_group.addButton(button, index)
            self.nav_buttons.append(button)
            sidebar_layout.addWidget(button)

        sidebar_layout.addStretch()

        version = QLabel("v0.1.0")
        version.setObjectName("Muted")
        sidebar_layout.addWidget(version)

        self.pages = QStackedWidget()

        placeholders = {
            0: ("Dashboard", "Hardware test overview."),
            1: ("Devices", "Connected device inventory."),
            2: ("Camera", "Camera module."),
            4: ("IMU", "IMU module."),
            5: ("CAN", "CAN module."),
            6: ("EtherCAT", "EtherCAT module."),
            7: ("Test Runner", "Cross-device test runner."),
            8: ("History", "Test session history."),
            9: ("Evidence", "Evidence storage."),
            10: ("Reports", "Test reports."),
            11: ("Settings", "Application settings."),
        }

        for index in range(len(self.NAV_ITEMS)):
            if index == 0:
                self.dashboard_page = DashboardPage(
                    self.jetson_state,
                    self.jetson_service,
                    self.device_registry,
                )
                self.dashboard_page.navigate_requested.connect(
                    self.navigate_to
                )
                self.dashboard_page.refresh_requested.connect(
                    self.refresh_dashboard
                )
                self.pages.addWidget(self.dashboard_page)
            elif index == 2:
                self.camera_page = CameraPage(
                    self.jetson_state,
                    self.jetson_service,
                )
                self.pages.addWidget(self.camera_page)
            elif index == 3:
                self.lidar_page = LidarPage(
                    self.jetson_state,
                    self.jetson_service,
                    self.device_registry,
                    self.lidar_runtime_state,
                    self.lidar_stream_service,
                    self.lidar_discovery_service,
                    self.test_registry,
                    self.test_execution_service,
                    network_profile=self.livox_network_profile,
                )
                self.pages.addWidget(self.lidar_page)
            else:
                title, subtitle = placeholders[index]
                self.pages.addWidget(PlaceholderPage(title, subtitle))

        self.nav_group.idClicked.connect(self.navigate_to)

        self.nav_buttons[0].setChecked(True)
        self.pages.setCurrentIndex(0)

        layout.addWidget(sidebar)
        layout.addWidget(self.pages, 1)

        self.setCentralWidget(root)

    def navigate_to(self, index: int):
        if not 0 <= index < self.pages.count():
            return

        self.pages.setCurrentIndex(index)
        self.nav_buttons[index].setChecked(True)

    def refresh_dashboard(self):
        # Giai đoạn đầu chỉ xác nhận thao tác.
        # Sau này lấy dữ liệu từ AppState hoặc DashboardController.
        self.dashboard_page.updated_label.setText("Updated just now")

    def closeEvent(self, event):
        if self.test_execution_service.running:
            self.test_execution_service.cancel()
        self.lidar_stream_service.shutdown()
        self.jetson_service.shutdown()
        super().closeEvent(event)
