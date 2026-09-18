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
from PySide6.QtCore import QTimer

from desktop_app.services.jetson_connection_service import (
    JetsonConnectionService,
)
from desktop_app.services.camera_inventory_service import CameraInventoryService
from desktop_app.services.lidar_discovery_service import (
    LidarDiscoveryService,
)
from desktop_app.services.lidar_stream_service import LidarStreamService
from desktop_app.state.device_registry import DeviceRegistry
from desktop_app.state.jetson_state import JetsonState
from desktop_app.state.lidar_runtime_state import LidarRuntimeState
from desktop_app.ui.camera_page import CameraPage
from desktop_app.ui.audio_page import AudioPage
from desktop_app.ui.ai_page import AiPage
from desktop_app.ui.bluetooth_page import BluetoothPage
from desktop_app.ui.dashboard_page import DashboardPage
from desktop_app.ui.lidar_page import LidarPage
from desktop_app.ui.system_stress_page import SystemStressPage
from desktop_app.ui.wifi_page import WifiPage
from devices.livox.profile import load_default_livox_profile
from devices.livox.testing import (
    LidarTestExecutionService,
    build_lidar_test_registry,
)
from core.bluetooth import BluetoothManager, BluetoothTestRunner


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
        ("♫", "Audio"),
        ("✦", "AI"),
        ("◌", "LiDAR"),
        ("◉", "Bluetooth"),
        ("⌘", "IMU"),
        ("▤", "CAN"),
        ("⌘", "EtherCAT"),
        ("▷", "Test Runner"),
        ("◔", "History"),
        ("□", "Evidence"),
        ("▤", "Reports"),
        ("⚙", "Settings"),
        ("▰", "Wi-Fi"),
        ("◫", "Stress Test"),
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
        # Bluetooth receives this existing shared Jetson service; it owns no
        # separate SSH transport.
        self.bluetooth_manager = BluetoothManager(self.jetson_service)
        self.bluetooth_test_runner = BluetoothTestRunner(
            self.bluetooth_manager, parent=self
        )
        self.camera_inventory_service = CameraInventoryService(
            self.jetson_service, parent=self
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
        self.test_execution_service = LidarTestExecutionService(
            self.test_registry,
            parent=self,
        )
        self._shutdown_after_camera_stop = False
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
            if index == len(self.NAV_ITEMS) - 1:
                button.setToolTip("System Stress Test")
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
            6: ("IMU", "IMU module."),
            7: ("CAN", "CAN module."),
            8: ("EtherCAT", "EtherCAT module."),
            9: ("Test Runner", "Cross-device test runner."),
            10: ("History", "Test session history."),
            11: ("Evidence", "Evidence storage."),
            12: ("Reports", "Test reports."),
            13: ("Settings", "Application settings."),
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
                    camera_inventory_service=self.camera_inventory_service,
                )
                self.camera_page.shutdown_ready.connect(self._finish_close)
                self.pages.addWidget(self.camera_page)
            elif index == 3:
                self.audio_page = AudioPage(
                    self.jetson_state,
                    self.jetson_service,
                )
                self.pages.addWidget(self.audio_page)
            elif index == 4:
                self.ai_page = AiPage(self.jetson_state, self.jetson_service)
                self.pages.addWidget(self.ai_page)
            elif index == 5:
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
            elif index == 6:
                self.bluetooth_page = BluetoothPage(
                    self.jetson_state,
                    self.jetson_service,
                    self.bluetooth_manager,
                    self.bluetooth_test_runner,
                )
                self.pages.addWidget(self.bluetooth_page)
            elif index == len(self.NAV_ITEMS) - 1:
                self.system_stress_page = SystemStressPage(
                    remote_service=self.jetson_service,
                )
                self.system_stress_page.navigate_requested.connect(
                    self.navigate_to
                )
                self.pages.addWidget(self.system_stress_page)
            elif self.NAV_ITEMS[index][1] == "Wi-Fi":
                self.wifi_page = WifiPage(self.jetson_service)
                self.pages.addWidget(self.wifi_page)
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
        if hasattr(self, "lidar_page"):
            self.lidar_page.shutdown()
        if hasattr(self, "system_stress_page"):
            self.system_stress_page.shutdown()
        if hasattr(self, "audio_page"):
            self.audio_page.shutdown()
        if hasattr(self, "ai_page") and self.ai_page._worker and self.ai_page._worker.isRunning():
            event.ignore()
            self.ai_page.cancel()
            QTimer.singleShot(17000, self.close)
            return
        if (
            hasattr(self, "camera_page")
            and (
                self.camera_page.connection_state.value == "STREAMING"
                or (
                    self.camera_page.test_runner_worker is not None
                    and self.camera_page.test_runner_worker.isRunning()
                )
                or (
                    self.camera_page.ros_test_runner_worker is not None
                    and self.camera_page.ros_test_runner_worker.isRunning()
                )
            )
            and not self._shutdown_after_camera_stop
        ):
            event.ignore()
            self.camera_page.shutdown_stream()
            QTimer.singleShot(17000, self._finish_close)
            return
        self.jetson_service.shutdown()
        super().closeEvent(event)

    def _finish_close(self):
        if self._shutdown_after_camera_stop:
            return
        self._shutdown_after_camera_stop = True
        self.close()
