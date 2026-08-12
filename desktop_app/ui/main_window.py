from PySide6.QtWidgets import (
    QMainWindow,
    QWidget,
    QHBoxLayout,
    QListWidget,
    QStackedWidget,
    QLabel,
)

from desktop_app.ui.lidar_page import LidarPage


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()

        self.setWindowTitle(
            "Hardware Test Automation"
        )

        self.resize(1200, 760)

        self._build_ui()

    def _build_ui(self):
        central = QWidget()

        layout = QHBoxLayout(central)

        self.sidebar = QListWidget()

        self.sidebar.setFixedWidth(220)

        self.sidebar.addItems([
            "Dashboard",
            "Camera Tests",
            "LiDAR Tests",
            "Test History",
            "Settings",
        ])

        self.pages = QStackedWidget()

        self.dashboard_page = QLabel(
            "Hardware Test Automation Dashboard"
        )

        self.camera_page = QLabel(
            "Camera Tests - Coming later"
        )

        self.lidar_page = LidarPage()

        self.history_page = QLabel(
            "Test History - Coming later"
        )

        self.settings_page = QLabel(
            "Settings - Coming later"
        )

        self.pages.addWidget(self.dashboard_page)
        self.pages.addWidget(self.camera_page)
        self.pages.addWidget(self.lidar_page)
        self.pages.addWidget(self.history_page)
        self.pages.addWidget(self.settings_page)

        self.sidebar.currentRowChanged.connect(
            self.pages.setCurrentIndex
        )

        self.sidebar.setCurrentRow(0)

        layout.addWidget(self.sidebar)
        layout.addWidget(self.pages)

        self.setCentralWidget(central)
