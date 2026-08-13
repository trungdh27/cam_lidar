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

from desktop_app.ui.lidar_page import LidarPage


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
            if index == 3:
                self.pages.addWidget(LidarPage())
            else:
                title, subtitle = placeholders[index]
                self.pages.addWidget(PlaceholderPage(title, subtitle))

        self.nav_group.idClicked.connect(self.pages.setCurrentIndex)

        self.nav_buttons[3].setChecked(True)
        self.pages.setCurrentIndex(3)

        layout.addWidget(sidebar)
        layout.addWidget(self.pages, 1)

        self.setCentralWidget(root)
